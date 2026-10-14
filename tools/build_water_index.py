"""Index NZ water/path/barrier geometry; approaches are calculated on demand.

Desktop preparation uses pyosmium and Shapely. Runtime water uses Shapely but
never pyosmium, PBF parsing or national approach-point precalculation.
"""
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from room_bot.data import DataError
from room_bot.water import APP_ID
from room_bot.water_geometry import water_kind,path_allowed,blocked,segment_approach


def schema(db):
    db.executescript('''
        CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE features (id INTEGER PRIMARY KEY,source_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,kind TEXT NOT NULL,intermittent TEXT NOT NULL,geometry BLOB NOT NULL);
        CREATE TABLE paths (id INTEGER PRIMARY KEY,geometry BLOB NOT NULL);
        CREATE TABLE blockers (id INTEGER PRIMARY KEY,geometry BLOB NOT NULL);
    ''')


def add_feature(db,source_id,name,kind,intermittent,geometry):
    if kind not in ('lake','river','stream') or intermittent not in ('yes','no','unknown'):
        raise DataError('Invalid water classification')
    if not geometry.is_valid or geometry.is_empty:raise DataError('Invalid source water geometry')
    if not isinstance(name,str) or not name.strip() or len(name)>200 or any(ord(c)<32 for c in name):
        raise DataError('Invalid source water name')
    west,south,east,north=geometry.bounds
    if not (-180<=west<=east<=180 and -90<=south<=north<=90):raise DataError('Source geometry is not WGS84')
    db.execute('INSERT INTO features(source_id,name,kind,intermittent,geometry) VALUES (?,?,?,?,?)',
               (source_id,name,kind,intermittent,geometry.wkb))


def geometry_parts(geometry):
    """Bounded original vertex chunks with shared ends; no generated approaches."""
    from shapely.geometry import LineString
    if geometry.geom_type=='Point':yield geometry;return
    if geometry.geom_type in ('Polygon','MultiPolygon'):
        yield from geometry_parts(geometry.boundary);return
    if geometry.geom_type in ('LineString','LinearRing'):
        coords=list(geometry.coords)
        for i in range(0,len(coords)-1,63):
            part=LineString(coords[i:i+64])
            if part.is_empty or not part.is_valid:raise DataError('Invalid geometry part')
            if part.bounds[2]-part.bounds[0]>180:raise DataError('Dateline-spanning geometry unsupported')
            yield part
        return
    if hasattr(geometry,'geoms'):
        for geom in geometry.geoms:yield from geometry_parts(geom)
        return
    raise DataError('Unsupported source geometry')


def index_geometry(db,stats):
    from shapely import wkb
    for table,source,identifier in (('water_parts','features','feature_id'),
                                    ('path_parts','paths','path_id'),('blocker_parts','blockers','source_id')):
        db.executescript(f'''DROP TABLE IF EXISTS {table}; DROP TABLE IF EXISTS {table}_bounds;
            CREATE TABLE {table}(id INTEGER PRIMARY KEY,{identifier} INTEGER NOT NULL,geometry BLOB NOT NULL);
            CREATE VIRTUAL TABLE {table}_bounds USING rtree(id,min_lon,max_lon,min_lat,max_lat);''')
        count=0
        for key,raw in db.execute(f'SELECT id,geometry FROM {source} ORDER BY id'):
            geometry=wkb.loads(raw)
            if geometry.is_empty or not geometry.is_valid:raise DataError('Invalid extracted geometry')
            for part in geometry_parts(geometry):
                west,south,east,north=part.bounds
                if not (-180<=west<=east<=180 and -90<=south<=north<=90):raise DataError('Source geometry is not WGS84')
                cursor=db.execute(f'INSERT INTO {table}({identifier},geometry) VALUES (?,?)',(key,part.wkb))
                db.execute(f'INSERT INTO {table}_bounds VALUES (?,?,?,?,?)',(cursor.lastrowid,west,east,south,north));count+=1
            if count and count%50000==0:db.commit();print(f'Indexed {table}: {count}',flush=True)
        stats[table]=count;db.commit();print(f'Completed {table}: {count}',flush=True)


def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):digest.update(block)
    return digest.hexdigest()


def extract(db,args,stats):
    import osmium
    from shapely import wkb
    factory=osmium.geom.WKBFactory()
    class Water(osmium.SimpleHandler):
        def node(self,node):
            if not len(node.tags) or not blocked(dict(node.tags)):return
            from shapely.geometry import Point
            point=Point(node.location.lon,node.location.lat)
            db.execute('INSERT INTO blockers(geometry) VALUES (?)',(point.wkb,))

        def way(self,way):
            tags=dict(way.tags)
            try:
                kind=water_kind(tags)
                if kind:
                    geom=wkb.loads(factory.create_linestring(way),hex=True)
                    add_feature(db,f'way/{way.id}',tags.get('name',f'Unnamed {kind}'),kind,
                        tags.get('intermittent') if tags.get('intermittent') in ('yes','no') else 'unknown',geom)
                if path_allowed(tags):
                    db.execute('INSERT INTO paths VALUES (?,?)',(way.id,wkb.loads(factory.create_linestring(way),hex=True).wkb))
                if blocked(tags):
                    db.execute('INSERT INTO blockers(geometry) VALUES (?)',(wkb.loads(factory.create_linestring(way),hex=True).wkb,))
            except (RuntimeError,DataError,ValueError):stats['invalid_way_geometry']+=1
            stats['ways_read']+=1
            if stats['ways_read']%50000==0:db.commit();print(f'OSM ways read: {stats["ways_read"]}',flush=True)

        def area(self,area):
            tags=dict(area.tags);kind=water_kind(tags,area=True)
            if kind is None:return
            source_id=('way/' if area.from_way() else 'relation/')+str(area.orig_id())
            try:
                geom=wkb.loads(factory.create_multipolygon(area),hex=True)
                add_feature(db,source_id,tags.get('name',f'Unnamed {kind}'),kind,
                    tags.get('intermittent') if tags.get('intermittent') in ('yes','no') else 'unknown',geom)
            except (RuntimeError,DataError,ValueError):stats['invalid_area_geometry']+=1
    Water().apply_file(str(args.input),locations=True,idx='sparse_mem_array');db.commit()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source-url',required=True);parser.add_argument('--revision',required=True)
    parser.add_argument('--resume-extracted',action='store_true',help='Resume verified extraction at OUTPUT.building')
    args=parser.parse_args();stage=args.output.with_name(args.output.name+'.building')
    if args.output.exists() or (stage.exists() != args.resume_extracted):
        parser.error('Output exists or staging/resume option does not match; use a new output or explicit resume')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    source={'source_sha256':file_hash(args.input),'source_url':args.source_url,'revision':args.revision}
    db=sqlite3.connect(stage);db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=NORMAL');stats=Counter()
    try:
        if args.resume_extracted:
            checkpoint=json.loads(db.execute("SELECT value FROM metadata WHERE key='extraction'").fetchone()[0])
            if any(checkpoint.get(k)!=v for k,v in source.items()):raise DataError('Extraction checkpoint source mismatch')
            stats.update(checkpoint.get('statistics',{}))
        else:
            schema(db);extract(db,args,stats);checkpoint=dict(source,statistics=dict(stats))
            db.execute('INSERT INTO metadata VALUES (?,?)',('extraction',json.dumps(checkpoint)));db.commit()
        print('Extraction checkpoint verified; indexing original geometry only',flush=True)
        index_geometry(db,stats)
        stats['features']=db.execute('SELECT count(*) FROM features').fetchone()[0]
        metadata=dict(source,title='OSM water/path geometry for on-demand approaches',prepared_at=datetime.now(timezone.utc).isoformat(),
            license='OpenStreetMap contributors, ODbL 1.0; https://www.openstreetmap.org/copyright',
            coverage='NZ extract; typed lake/river boundaries, stream centrelines and eligible paths',
            policy='Original geometry in <=64-vertex pieces; approaches calculated only for local requests',
            statistics=dict(stats),extraction_notes=checkpoint.get('notes','Complete callback statistics retained'))
        db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('source',json.dumps(metadata)))
        db.executescript('DROP TABLE paths; DROP TABLE blockers; DELETE FROM metadata WHERE key="extraction";')
        db.execute(f'PRAGMA application_id={APP_ID}');db.execute('PRAGMA user_version=2');db.commit();db.execute('VACUUM')
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise DataError('Geometry index failed integrity check')
    finally:db.close()
    stage.rename(args.output);print(json.dumps({'bytes':args.output.stat().st_size,'statistics':dict(stats)}),flush=True)


if __name__=='__main__':main()
