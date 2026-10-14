"""Offline mapped-path approaches to water; never a potable-water assessment."""
import json
import math
import re
from pathlib import Path
import sqlite3
import time

from .data import DataError
from .geography import straight_distance
from .locations import describe_origin, resolve_location
from .routing import RoutingError

APP_ID = 0x53575741
WARNING = 'Drinking-water safety unknown. Do not assume safe to drink.'


class DenseGeometry(DataError):
    pass


def bounds(point, radius):
    lat, lon = point['latitude'], point['longitude']
    # Conservative box for NZ and other non-polar coordinates. Dateline searches
    # split into two boxes; never silently omit the Chatham Islands.
    dy = math.degrees(radius / 6371008.8)
    maximum_lat = min(89.999, abs(lat) + dy)
    dx = min(180, dy / math.cos(math.radians(maximum_lat)))
    low, high = lon-dx, lon+dx
    if low < -180:
        spans = [(-180,high),(low+360,180)]
    elif high > 180:
        spans = [(low,180),(-180,high-360)]
    else:
        spans = [(low,high)]
    return [(max(-90,lat-dy),min(90,lat+dy),a,b) for a,b in spans]


class WaterStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise DataError('No offline water index installed')
        self.db = sqlite3.connect(self.path.as_uri()+'?mode=ro&immutable=1',uri=True)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute('PRAGMA query_only=ON')
            if (self.db.execute('PRAGMA application_id').fetchone()[0] != APP_ID or
                    self.db.execute('PRAGMA user_version').fetchone()[0] != 2):
                raise DataError('Unsupported water index')
            raw = self.db.execute('SELECT value FROM metadata WHERE key=?',('source',)).fetchone()[0]
            if len(raw) > 65536:
                raise DataError('Invalid water metadata')
            self.metadata = json.loads(raw)
            for key in ('title','source_url','revision','source_sha256'):
                value = self.metadata.get(key)
                if not isinstance(value,str) or not 1 <= len(value) <= 500 or any(ord(c)<32 for c in value):
                    raise DataError('Invalid water source metadata')
            if not re.fullmatch('[0-9a-f]{64}',self.metadata['source_sha256']):
                raise DataError('Invalid water source hash')
        except (TypeError,ValueError,IndexError,KeyError) as exc:
            self.db.close()
            raise DataError('Invalid water metadata') from exc
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def candidates(self, origin, settings):
        try:
            from shapely import wkb
            from shapely.geometry import LineString
            from shapely.prepared import prep
        except ImportError as exc:
            raise DataError('Install optional requirements-water.txt for on-demand water geometry') from exc
        from functools import lru_cache
        from .water_geometry import segment_approach
        deadline=time.monotonic()+settings.geometry_budget_seconds
        steps=[0]
        def interrupted():
            steps[0]+=10000
            return steps[0]>10000000 or time.monotonic()>deadline
        self.db.set_progress_handler(interrupted,10000)
        candidates=[];seen_paths=set();seen_points=set();count=0;segments=0
        stats={'path_parts':0,'segments':0,'limited':False,'complex_features_skipped':0,'dense_sections_skipped':0}
        @lru_cache(maxsize=8)
        def source(key):
            row=self.db.execute('''SELECT source_id,name,kind,intermittent,geometry FROM features
                WHERE id=? AND length(geometry)<=2097152''',(key,)).fetchone()
            if row is None:
                stats['complex_features_skipped']+=1
                return None
            item=dict(row)
            try:geom=wkb.loads(item.pop('geometry'))
            except Exception as exc:raise DataError('Invalid source water geometry') from exc
            if geom.is_empty or not geom.is_valid:raise DataError('Invalid water geometry')
            if item['kind'] not in ('lake','river','stream') or item['intermittent'] not in ('yes','no','unknown'):
                raise DataError('Invalid water classification')
            if not re.fullmatch(r'(way|relation)/[1-9][0-9]{0,18}',item['source_id']):raise DataError('Invalid water ID')
            if not isinstance(item['name'],str) or not 1<=len(item['name'])<=200 or any(ord(c)<32 for c in item['name']):
                raise DataError('Invalid water name')
            return item,prep(geom)
        def decode(raw):
            if len(raw)>8192:raise DataError('Geometry part exceeds query bounds')
            try:geom=wkb.loads(raw)
            except Exception as exc:raise DataError('Invalid local geometry') from exc
            if geom.is_empty or not geom.is_valid:raise DataError('Invalid local geometry')
            west,south,east,north=geom.bounds
            if not (-180<=west<=east<=180 and -90<=south<=north<=90):raise DataError('Local geometry is not WGS84')
            return geom
        def nearby_parts(table,point,limit):
            rows=[]
            for south,north,west,east in bounds(point,70):
                rows.extend(self.db.execute(f'''SELECT p.* FROM {table}_bounds b JOIN {table} p ON p.id=b.id
                    WHERE b.max_lat>=? AND b.min_lat<=? AND b.max_lon>=? AND b.min_lon<=? LIMIT ?''',
                    (south,north,west,east,limit+1)).fetchall())
            if len(rows)>limit:raise DenseGeometry('Local water/barrier density limit reached')
            return rows
        def stopped():
            if time.monotonic()>=deadline or segments>=settings.scan_limit:
                stats['limited']=True;return True
            return False
        for south,north,west,east in bounds(origin,settings.radius_metres):
            rows = self.db.execute('''SELECT p.* FROM path_parts_bounds b JOIN path_parts p ON p.id=b.id
                WHERE b.max_lat>=? AND b.min_lat<=? AND b.max_lon>=? AND b.min_lon<=?
                ORDER BY ((b.min_lat+b.max_lat)/2-?)*((b.min_lat+b.max_lat)/2-?) +
                ((b.min_lon+b.max_lon)/2-?)*((b.min_lon+b.max_lon)/2-?)*? LIMIT ?''',
                (south,north,west,east,origin['latitude'],origin['latitude'],origin['longitude'],origin['longitude'],
                 math.cos(math.radians(origin['latitude']))**2,settings.scan_limit+1))
            for raw in rows:
                if raw['id'] in seen_paths:continue
                seen_paths.add(raw['id']);stats['path_parts']+=1
                if stopped() or stats['path_parts']>settings.scan_limit:stats['limited']=True;break
                path=decode(raw['geometry'])
                if path.geom_type!='LineString' or len(path.coords)>64:raise DataError('Invalid path part')
                coordinates=list(path.coords)
                for first,second in zip(coordinates,coordinates[1:]):
                    if stopped():break
                    a={'latitude':first[1],'longitude':first[0]};b={'latitude':second[1],'longitude':second[0]}
                    parts=max(1,math.ceil(straight_distance(a,b)/50))
                    original=LineString([first,second])
                    for i in range(parts):
                        if stopped():break
                        segments+=1
                        c=(first[0]+(second[0]-first[0])*i/parts,first[1]+(second[1]-first[1])*i/parts)
                        d=(first[0]+(second[0]-first[0])*(i+1)/parts,first[1]+(second[1]-first[1])*(i+1)/parts)
                        midpoint={'latitude':(c[1]+d[1])/2,'longitude':(c[0]+d[0])/2}
                        if straight_distance(origin,midpoint)>settings.radius_metres+25:continue
                        try:
                            water_parts=nearby_parts('water_parts',midpoint,256)
                            if not water_parts:continue
                            obstacles=[decode(r['geometry']) for r in nearby_parts('blocker_parts',midpoint,128)]
                        except DenseGeometry:
                            stats['dense_sections_skipped']+=1;stats['limited']=True
                            continue
                        features={r['feature_id']:source(r['feature_id']) for r in water_parts}
                        if any(info and info[1].intersects(original) for info in features.values()):continue
                        segment=LineString([c,d])
                        for piece in water_parts:
                            if stopped():break
                            info=features[piece['feature_id']]
                            if info is None:continue
                            pair=segment_approach(segment,decode(piece['geometry']),obstacles)
                            if pair is None:continue
                            point,water_point,gap=pair;distance=straight_distance(origin,point)
                            if distance>settings.radius_metres:continue
                            identity=(piece['feature_id'],raw['path_id'],round(point['latitude']*3700),round(point['longitude']*2800))
                            if identity in seen_points:continue
                            seen_points.add(identity);count+=1
                            row=dict(info[0],**point,water_latitude=water_point['latitude'],water_longitude=water_point['longitude'],
                                     gap_m=gap,path_id=raw['path_id'],straight_m=distance,_feature_id=piece['feature_id'],
                                     id=f'{piece["feature_id"]}:{raw["path_id"]}:{point["latitude"]:.8f}:{point["longitude"]:.8f}')
                            candidates.append(row)
                            candidates.sort(key=lambda r:(r['straight_m'],r['id']))
                            del candidates[settings.candidate_limit:]
                if stopped():break
            if stopped():break
        stats['segments']=segments
        self.db.set_progress_handler(None,0)
        return candidates,count,stats


def water_reply(store, args, data_path, settings, router=None, monotonic=time.monotonic, details=None):
    if not args:
        return WARNING+' Usage: /bot location water ORIGIN [walk|straight]'
    mode = args[-1].casefold() if args[-1].casefold() in ('walk','straight') else 'walk'
    words = args[:-1] if args[-1].casefold() in ('walk','straight') else args
    origin,_ = resolve_location(store,' '.join(words),allow_street_point=True)
    water = WaterStore(Path(data_path)/'water.sqlite3')
    try:
        rows,total,geometry_stats = water.candidates(origin,settings)
        metadata = water.metadata
    finally:
        water.close()
    header = WARNING+' Access and water presence today unverified.'
    if not rows:
        return [WARNING+f'\nNo mapped approach found within {settings.radius_metres/1000:g} km'+
                (' (limited search).' if geometry_stats['limited'] else '.'),
                'This does not mean there is no water nearby. Water presence and access are unverified.']
    if mode == 'walk' and router is None:
        return [header,'Walking graph unavailable; install it or explicitly request /bot location water ORIGIN straight.']
    results = []; tested = failed = 0; timed_out = False
    started = monotonic()
    if mode == 'walk':
        router.verify()
    for row in rows:
        if mode == 'straight':
            results.append((row['straight_m'],row,None))
            continue
        remaining = settings.route_budget_seconds-(monotonic()-started)
        if remaining <= 0:
            timed_out = True
            break
        destination = dict(row,id=f'water:{metadata["source_sha256"]}:{row["id"]}',_origin_kind='water_approach')
        tested += 1
        try:
            route = router.route(origin,destination,timeout_seconds=remaining)
        except RoutingError:
            failed += 1
            continue
        # Defend even when a custom/injected router does not implement our policy.
        if (any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in
                (route.distance_m,route.start_snap_m,route.end_snap_m)) or route.end_snap_m > 10):
            failed += 1
            continue
        results.append((route.distance_m,row,route))
    replies = [header]
    if not results:
        replies.append('No verified route result in this bounded search; no claim about water absence or access.')
    else:
        distance,row,route = min(results,key=lambda r:(r[0],r[1]['id']))
        label = 'Shortest tested mapped-path approach' if mode == 'walk' else 'Nearest computed mapped-path approach by straight line'
        replies.append(f'{label}: {row["name"]} ({row["kind"]}), approx {distance/1000:.2f} km '
                       f'{"walking" if mode=="walk" else "straight-line, not a walking route"}.')
        replies.append(f'Path point {row["latitude"]:.6f},{row["longitude"]:.6f}; '
                       f'water {"centreline" if row["kind"]=="stream" else "boundary"} '
                       f'{row["water_latitude"]:.6f},{row["water_longitude"]:.6f}, gap {row["gap_m"]:.0f} m. '
                       'Gap excluded from distance; bank/collection access unverified.')
        replies.append(f'Intermittent: {row["intermittent"]}; salinity unverified where not tagged. '
                       f'Origin: {describe_origin(origin)}.')
        if route is not None:
            replies.append(f'Route endpoint snaps: {route.start_snap_m:.0f} m / {route.end_snap_m:.0f} m, '
                           'excluded from distance; no off-path directions or current access guarantee.')
    replies.append(f'Search: {settings.radius_metres/1000:g} km radius; {len(rows)} closest computed approach points '
                   f'of {total} local candidates; {tested} routes tested, {failed} unavailable; '
                   f'{settings.route_budget_seconds:g} s route budget{" exhausted" if timed_out or monotonic()-started>=settings.route_budget_seconds else ""}. '
                   'Sampled paths only; global nearest water not established.')
    replies.append(f'On-demand geometry: {geometry_stats["path_parts"]} path pieces, {geometry_stats["segments"]} sections; '
                   f'{settings.geometry_budget_seconds:g} s / {settings.scan_limit} scan limit; '
                   f'{"limited search" if geometry_stats["limited"] else "search completed"}; '
                   f'{geometry_stats["complex_features_skipped"]} complex feature loads and '
                   f'{geometry_stats["dense_sections_skipped"]} dense sections skipped.')
    if details is not None:details.extend(replies)
    if not results:
        return [WARNING+'\nWalking route unavailable for the tested approaches.']
    mode_label='walking' if mode=='walk' else 'straight-line (not walking)'
    limited=(geometry_stats['limited'] or geometry_stats['complex_features_skipped'] or failed or
             timed_out or tested<len(rows) and mode=='walk')
    street_note=' Approx street origin.' if origin.get('_origin_kind')=='street' else ''
    amount='' if street_note else 'about '
    return [WARNING+'\n'+f'{row["name"]}: {amount}{distance/1000:.2f} km {mode_label}; best tested approach.',
            f'Approach: {row["latitude"]:.6f},{row["longitude"]:.6f}. Water/access unverified; '
            f'{row["gap_m"]:.0f} m gap to water excluded from distance.'+(' Limited search.' if limited else '')+street_note]
