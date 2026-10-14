"""Prepare a national offline road-name index from an already downloaded OSM PBF.

Requires pyosmium ONLY on preparation host. Bot runtime uses SQLite/stdlib.
Connected same-name ways form components by actual shared OSM node IDs.
"""
import argparse
from collections import defaultdict
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from room_bot.streets import APP_ID,representative,street_key
from room_bot.data import DataError

HIGHWAYS={'residential','living_street','unclassified','tertiary','secondary','primary','trunk',
          'motorway','service','pedestrian','footway','path','cycleway','track','steps'}


def components(rows):
    parent=list(range(len(rows))); owners={}
    def find(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]];i=parent[i]
        return i
    for i,row in enumerate(rows):
        for node,_,_ in row['path']:
            if node in owners:parent[find(i)]=find(owners[node])
            else:owners[node]=i
    groups=defaultdict(list)
    for i,row in enumerate(rows):groups[find(i)].append(row)
    return list(groups.values())


def create_index(path,rows,metadata):
    """Reusable offline builder, also exercised by deterministic tests."""
    path=Path(path)
    if path.exists():raise DataError('Refusing to overwrite an existing street index')
    path.parent.mkdir(parents=True,exist_ok=True)
    stage=path.with_name(path.name+'.building')
    if stage.exists():raise DataError('Prior unfinished index exists; choose a new output name')
    db=sqlite3.connect(stage)
    try:
        db.execute('PRAGMA journal_mode=OFF')
        db.execute('CREATE TABLE raw (id INTEGER PRIMARY KEY, name TEXT, name_key TEXT, path TEXT)')
        db.execute('CREATE INDEX raw_name ON raw(name_key,id)')
        count=0
        for row in rows:
            if len(row['path'])<2:continue
            if not isinstance(row['name'],str) or not row['name'].strip() or len(row['name'])>200:continue
            if any(ord(c)<32 for c in row['name']):continue
            for node,lat,lon in row['path']:
                if not isinstance(node,int) or node<=0 or not (-90<=lat<=90 and -180<=lon<=180):
                    raise DataError('Invalid source road geometry')
            db.execute('INSERT INTO raw VALUES (?,?,?,?)',(row['id'],row['name'],street_key(row['name']),json.dumps(row['path'],separators=(',',':'))))
            count+=1
            if count%10000==0:db.commit();print(f'Extracted named ways: {count}',flush=True)
        db.commit()
        db.execute('CREATE TABLE streets (id INTEGER PRIMARY KEY, name TEXT, name_key TEXT, latitude REAL, longitude REAL, length_m REAL, paths TEXT, way_ids TEXT)')
        db.execute('CREATE INDEX streets_name ON streets(name_key,id)')
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        names=[r[0] for r in db.execute('SELECT DISTINCT name_key FROM raw ORDER BY name_key')]
        total=0
        for key in names:
            source=[{'id':r[0],'name':r[1],'path':json.loads(r[2])} for r in db.execute('SELECT id,name,path FROM raw WHERE name_key=? ORDER BY id',(key,))]
            for group in components(source):
                group.sort(key=lambda r:r['id']);paths=[r['path'] for r in group]
                try:lat,lon,length=representative(paths)
                except DataError:continue
                db.execute('INSERT INTO streets VALUES (?,?,?,?,?,?,?,?)',(group[0]['id'],group[0]['name'],key,lat,lon,length,
                    json.dumps(paths,separators=(',',':')),json.dumps([r['id'] for r in group])))
                total+=1
        db.execute('DROP TABLE raw')
        metadata=dict(metadata,components=total,named_ways=count,
                      representative_policy='Point on source geometry at half aggregate length of ways sorted by ID; not a user position or unique midpoint')
        db.execute('INSERT INTO metadata VALUES (?,?)',('source',json.dumps(metadata,ensure_ascii=False)))
        db.execute(f'PRAGMA application_id={APP_ID}');db.execute('PRAGMA user_version=1');db.commit();db.execute('VACUUM')
    finally:db.close()
    stage.rename(path)
    print(json.dumps({'index':str(path),'components':total,'named_ways':count,'bytes':path.stat().st_size}),flush=True)


def main():
    import osmium
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source-url',required=True)
    parser.add_argument('--revision',required=True)
    args=parser.parse_args()
    # Streaming callback writes the raw rows to a spool DB; no national list in RAM.
    spool=args.output.with_name(args.output.name+'.extracting')
    if spool.exists() or args.output.exists():parser.error('Output/staging file already exists')
    spool.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(spool);conn.execute('CREATE TABLE ways (id INTEGER,name TEXT,path TEXT)')
    class Roads(osmium.SimpleHandler):
        def __init__(self):super().__init__();self.count=0
        def way(self,way):
            name=way.tags.get('name'); highway=way.tags.get('highway')
            if not name or highway not in HIGHWAYS:return
            try:points=[[n.ref,n.lat,n.lon] for n in way.nodes]
            except osmium.InvalidLocationError as exc:raise DataError('Named road has missing node geometry') from exc
            conn.execute('INSERT INTO ways VALUES (?,?,?)',(way.id,name,json.dumps(points,separators=(',',':'))))
            self.count+=1
            if self.count%10000==0:conn.commit();print(f'Read named ways: {self.count}',flush=True)
    try:
        Roads().apply_file(str(args.input),locations=True,idx='sparse_mem_array');conn.commit()
        digest=hashlib.sha256()
        with args.input.open('rb') as stream:
            for block in iter(lambda:stream.read(1048576),b''):digest.update(block)
        metadata={'title':'OpenStreetMap named road/path components','source_url':args.source_url,'revision':args.revision,
                  'source_sha256':digest.hexdigest(),'retrieved_at':datetime.now(timezone.utc).isoformat(),
                  'license':'OpenStreetMap contributors, ODbL 1.0; https://www.openstreetmap.org/copyright',
                  'coverage':'Named highways in NZ extract; locality hints use 20 km proximity, not municipal boundaries'}
        rows=({'id':r[0],'name':r[1],'path':json.loads(r[2])} for r in conn.execute('SELECT * FROM ways ORDER BY id'))
        create_index(args.output,rows,metadata)
    finally:conn.close()
    spool.unlink()


if __name__=='__main__':main()
