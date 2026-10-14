from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from room_bot.commands import Dispatcher
from room_bot.config import Config,ConfigError,Limits,RoutingSettings,WaterSettings
from room_bot.data import DataError,OfflineStore,validate_bundle
from room_bot.offline import install_offline_commands
from room_bot.routing import RoutinoRouter,RouteResult,RoutingError
from room_bot.water import APP_ID,WARNING,WaterStore,bounds,water_reply
from tools.build_water_index import schema,path_allowed,water_kind,index_geometry
from test_commands import post
from test_data import bundle
from test_geography import town

try:
    from shapely.geometry import LineString,Polygon
except ImportError:
    LineString=Polygon=None
try:
    import osmium
except ImportError:
    osmium=None


@unittest.skipUnless(LineString is not None,'Optional on-demand water Shapely dependency missing')
class WaterTests(unittest.TestCase):
    def test_water_accepts_a_street_without_point_suffix_and_uses_its_origin(self):
        from tools.build_street_index import create_index
        rows = [{'id': 10, 'name': 'Synthetic Street',
                 'path': [[1, -37, 175], [2, -37.0002, 175]]}]
        with redirect_stdout(io.StringIO()):
            create_index(self.root/'streets.sqlite3', rows, {
                'title': 'Synthetic streets', 'source_url': 'https://example.invalid', 'revision': 'test'})
        router = self.fake_router()
        actual_origins = []
        route = router.route
        def record_origin(origin, destination, **kwargs):
            actual_origins.append(origin)
            return route(origin, destination, **kwargs)
        with patch.object(router, 'route', side_effect=record_origin):
            replies = water_reply(self.store, ['Synthetic', 'Street', 'Synthetic', 'City'],
                                  self.root, WaterSettings(), router)
        self.assertTrue(actual_origins)
        self.assertTrue(all(o['id'] == 'street:10' for o in actual_origins))
        self.assertAlmostEqual(-37.0001, actual_origins[0]['latitude'])
        text = ' '.join(replies)
        self.assertTrue(replies[0].startswith(WARNING))
        self.assertIn('Approx street origin.', text)
        self.assertIn('Far lake:', text)
        self.assertNotIn('@point', text)
        self.assertNotIn('Usage:', text)

    def test_water_suburb_city_uses_suburb_instead_of_city_centre(self):
        data = bundle('places')
        data['records'] = [town('city', 'Synthetic City', -37, 175),
                           dict(town('suburb', 'Synthetic Suburb', -37.003, 175), feature_type='Suburb')]
        self.store.import_validated(*validate_bundle(data))
        from room_bot.locations import resolve_location
        origin, _ = resolve_location(self.store, 'Synthetic Suburb Synthetic City')
        self.assertEqual('suburb', origin['id'])
        router = self.fake_router()
        with patch.object(router, 'route', wraps=router.route) as route:
            water_reply(self.store, ['Synthetic', 'Suburb', 'Synthetic', 'City'],
                        self.root, WaterSettings(), router)
        self.assertTrue(route.call_args_list)
        self.assertTrue(all(call.args[0]['id'] == 'suburb' for call in route.call_args_list))

    def test_ambiguous_street_asks_for_a_choice_before_searching_water(self):
        from tools.build_street_index import create_index
        rows = [{'id': 10, 'name': 'Synthetic Street',
                 'path': [[1, -37, 175], [2, -37.0002, 175]]},
                {'id': 20, 'name': 'Synthetic Street',
                 'path': [[3, -37.001, 175], [4, -37.0012, 175]]}]
        with redirect_stdout(io.StringIO()):
            create_index(self.root/'streets.sqlite3', rows, {
                'title': 'Synthetic streets', 'source_url': 'https://example.invalid', 'revision': 'test'})
        dispatch = Dispatcher(replace(Config(), enabled_commands=('location',)))
        install_offline_commands(dispatch, self.root)
        with patch('room_bot.water.WaterStore', side_effect=AssertionError('Ambiguous origin used')):
            replies = dispatch(post('/bot location water Synthetic Street Synthetic City'))
        text = ''.join(replies)
        self.assertTrue(replies[0].startswith(WARNING))
        self.assertIn('Ambiguous street', text)
        self.assertIn('street:10', text)
        self.assertIn('street:20', text)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=OfflineStore(self.root/'offline.sqlite3')
        data=bundle('places');data['records']=[town('city','Synthetic City',-37,175)]
        self.store.import_validated(*validate_bundle(data))
        self.db=sqlite3.connect(self.root/'water.sqlite3');schema(self.db)
        self.metadata={'title':'Synthetic water geometry','source_url':'https://example.invalid/fixture',
                       'revision':'fixture-v1','source_sha256':'a'*64}
        self.db.execute('INSERT INTO metadata VALUES (?,?)',('source',json.dumps(self.metadata)))
        self.db.execute(f'PRAGMA application_id={APP_ID}');self.db.execute('PRAGMA user_version=2')
        self.add(1,'Near stream',-37.001,175)
        self.add(2,'Far lake',-37.002,175,kind='lake')
        self.refresh()

    def refresh(self):
        with redirect_stdout(io.StringIO()):index_geometry(self.db,{})

    def add(self,key,name,lat,lon,kind='stream'):
        geom=(LineString([(lon+0.0001,lat),(lon+0.0005,lat)]) if kind=='stream' else
              Polygon([(lon+0.0001,lat-0.0001),(lon+0.0005,lat-0.0001),
                       (lon+0.0005,lat+0.0001),(lon+0.0001,lat+0.0001)]))
        path=LineString([(lon,lat),(lon+0.00004,lat)])
        self.db.execute('INSERT INTO features VALUES (?,?,?,?,?,?)',(key,f'way/{key}',name,kind,'unknown',geom.wkb))
        self.db.execute('INSERT INTO paths VALUES (?,?)',(key,path.wkb));self.db.commit()

    def tearDown(self):
        self.db.close();self.store.close();self.temp.cleanup()

    def fake_router(self,distances=None,fail=(),snap=0):
        distances=distances or {1:900,2:300}
        class Router:
            calls=[]
            def verify(self):return {'revision':'test'}
            def route(self,origin,destination,timeout_seconds=None):
                key=destination['_feature_id'];self.calls.append((key,timeout_seconds,destination))
                if key in fail:raise RoutingError('no path')
                return RouteResult(distances[key],0,snap,'test','https://example.invalid',2)
        return Router()

    def test_walk_ranks_actual_route_not_straight_line_shortlist(self):
        router=self.fake_router()
        details=[]
        replies=water_reply(self.store,['Synthetic','City'],self.root,WaterSettings(),router,details=details)
        text=' '.join(replies)
        self.assertTrue(replies[0].startswith(WARNING))
        self.assertIn('Far lake: about 0.30 km walking',text)
        self.assertIn('best tested approach',text);self.assertIn('global nearest water not established',' '.join(details))
        self.assertIn('5 m gap to water excluded',text);self.assertIn('Water/access unverified',text)
        self.assertLessEqual(len(replies),2);self.assertNotIn('On-demand geometry:',text);self.assertNotIn('Route endpoint snaps:',text)
        self.assertEqual(2,len(router.calls))
        self.assertTrue(all(0<call[1]<=30 for call in router.calls))
        self.assertEqual('water_approach',router.calls[0][2]['_origin_kind'])
        self.assertNotIn('https://',text)
        self.assertNotIn('Source:',text)
        from room_bot.sources import source_reply
        explicit=' '.join(source_reply(self.root,['water']))
        self.assertIn('https://example.invalid/fixture',explicit)

    def test_explicit_straight_mode_never_uses_router(self):
        router=self.fake_router()
        with patch.object(router,'route',side_effect=AssertionError('router used')):
            text=' '.join(water_reply(self.store,['-37,175','straight'],self.root,WaterSettings(),router))
        self.assertIn('Near stream',text);self.assertIn('not walking',text)
        self.assertNotIn('user coordinate',text)

    def test_missing_route_is_not_silently_a_straight_route(self):
        result=water_reply(self.store,['Synthetic','City'],self.root,WaterSettings())
        self.assertTrue(result[0].startswith(WARNING));self.assertIn('Walking graph unavailable',result[1])

    def test_unreachable_and_wrong_side_large_snap_are_not_success(self):
        for router in (self.fake_router(fail=(1,2)),self.fake_router(snap=11)):
            text=' '.join(water_reply(self.store,['Synthetic','City'],self.root,WaterSettings(),router))
            self.assertIn('Walking route unavailable',text);self.assertNotIn('km walking',text)
        router=self.fake_router(fail=(2,))
        details=[]
        text=' '.join(water_reply(self.store,['Synthetic','City'],self.root,WaterSettings(),router,details=details))
        self.assertIn('Near stream',text);self.assertIn('1 unavailable',' '.join(details))

    def test_radius_density_and_candidate_limits_are_enforced(self):
        water=WaterStore(self.root/'water.sqlite3')
        try:
            rows,total,stats=water.candidates({'latitude':-37,'longitude':175},WaterSettings(radius_metres=100))
            self.assertEqual(([],0),(rows,total))
            rows,total,stats=water.candidates({'latitude':-37,'longitude':175},WaterSettings(candidate_limit=1))
            self.assertEqual((1,2),(len(rows),total))
            water.close()
            for key in range(3,103):self.add(key,'Extra stream',-37.001,175)
            self.refresh()
            water=WaterStore(self.root/'water.sqlite3')
            # Dense water features have an explicit per-section cap; never omit
            # known barriers silently to make a route appear available.
            rows,total,stats=water.candidates({'latitude':-37,'longitude':175},WaterSettings(scan_limit=100,geometry_budget_seconds=15))
            self.assertTrue(stats['limited']);self.assertLessEqual(stats['segments'],100)
        finally:water.close()

    def test_route_budget_stops_before_next_subprocess(self):
        router=self.fake_router()
        ticks=iter([0,0.2,31,31])
        text=' '.join(water_reply(self.store,['Synthetic','City'],self.root,WaterSettings(),router,lambda:next(ticks)))
        self.assertEqual(1,len(router.calls));self.assertIn('Limited search',text)

    def test_readonly_schema_metadata_and_bad_geometry(self):
        water=WaterStore(self.root/'water.sqlite3')
        try:
            with self.assertRaises(sqlite3.OperationalError):water.db.execute('DELETE FROM features')
        finally:water.close()
        self.db.execute('UPDATE path_parts SET geometry=? WHERE id=1',(b'invalid geometry',));self.db.commit()
        water=WaterStore(self.root/'water.sqlite3')
        try:
            with self.assertRaises(DataError):water.candidates({'latitude':-37,'longitude':175},WaterSettings())
        finally:water.close()
        self.db.execute('PRAGMA user_version=9');self.db.commit()
        with self.assertRaises(DataError):WaterStore(self.root/'water.sqlite3')

    def test_missing_data_warning_and_bounded_unicode_paging_offline(self):
        cfg=replace(Config(),enabled_commands=('water',),limits=Limits(request_cooldown_seconds=0))
        dispatcher=Dispatcher(cfg,'[SquishyBot] ');install_offline_commands(dispatcher,self.root)
        with patch('socket.create_connection',side_effect=AssertionError('network')):
            result=dispatcher(post('/bot water -37,175 straight'))
        self.assertTrue(result[0].startswith(WARNING))
        self.assertLessEqual(len(result),4)
        self.assertTrue(all(len(('[SquishyBot] '+line).encode())<=151 for line in result))
        dispatcher=Dispatcher(cfg,'[SquishyBot] ');install_offline_commands(dispatcher,self.root)
        self.db.close()
        (self.root/'water.sqlite3').rename(self.root/'uninstalled.sqlite3')
        self.assertTrue(dispatcher(post('/bot water -37,175'))[0].startswith(WARNING))
        self.assertTrue(dispatcher(post('/bot water 91,175'))[0].startswith(WARNING))

    def test_settings_validated_and_water_off_by_default(self):
        self.assertNotIn('water',Config().enabled_commands)
        self.assertEqual(Config(),Config.from_dict(Config().to_dict()))
        for value in ({'radius_metres':True},{'candidate_limit':51},{'scan_limit':99},
                      {'route_budget_seconds':float('nan')},{'unknown':3}):
            with self.subTest(value=value),self.assertRaises(ConfigError):Config.from_dict({'water':value})

    def test_water_endpoint_snap_is_strict_and_cache_key_policy_changes(self):
        router=RoutinoRouter(RoutingSettings(),self.root)
        origin={'_origin_kind':'coordinate'};water={'_origin_kind':'water_approach'}
        self.assertEqual((100,10),router.snap_bounds(origin,water))
        with self.assertRaises(RoutingError):router.route({}, {},timeout_seconds=float('nan'))

    def test_dateline_bounds_cover_chatham_and_wrap(self):
        boxes=bounds({'latitude':-44,'longitude':179.999},5000)
        self.assertEqual(2,len(boxes));self.assertEqual(180,boxes[0][3]);self.assertEqual(-180,boxes[1][2])

    def test_dense_barriers_skip_whole_section_without_ignoring_known_barriers(self):
        from shapely.geometry import Point
        for key in range(1,131):
            self.db.execute('INSERT INTO blockers VALUES (?,?)',(key,Point(175.00005,-37.001).wkb))
        self.db.commit();self.refresh()
        water=WaterStore(self.root/'water.sqlite3')
        try:
            rows,total,stats=water.candidates({'latitude':-37,'longitude':175},WaterSettings())
            self.assertTrue(stats['limited']);self.assertGreater(stats['dense_sections_skipped'],0)
            self.assertTrue(all(r['name']!='Near stream' for r in rows))
            self.assertTrue(any(r['name']=='Far lake' for r in rows))
        finally:water.close()

    def test_preparation_excludes_saline_industrial_and_restricted_features(self):
        self.assertEqual('stream',water_kind({'waterway':'stream'}))
        self.assertEqual('lake',water_kind({'natural':'water','water':'lake'},area=True))
        for extra in ({'tidal':'yes'},{'salt':'yes'},{'salinity':'brackish'},{'usage':'industrial'},
                      {'foot':'no'},{'access':'private'},{'water':'wastewater'}):
            self.assertIsNone(water_kind(dict(waterway='stream',**extra)))
        for kind in ('ditch','drain','canal','river','tidal_channel'):
            self.assertIsNone(water_kind({'waterway':kind}))
        for extra in ({'bridge':'yes'},{'tunnel':'yes'},{'ford':'yes'},{'access':'private'},
                      {'access:conditional':'yes @ summer'},{'foot':'no'},{'motorroad':'yes'}):
            self.assertFalse(path_allowed(dict(highway='path',**extra)))
        self.assertTrue(path_allowed({'highway':'path','access':'permissive'}))


@unittest.skipUnless(LineString is not None,'Preparation-only Shapely not installed on runtime host')
class WaterGeometryTests(unittest.TestCase):
    @unittest.skipUnless(osmium is not None,'Preparation-only pyosmium not installed')
    def test_actual_multipolygon_extraction_and_atomic_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'fixture.osm';output=root/'water.sqlite3'
            source.write_text('''<osm version="0.6">
              <node id="1" lat="-37" lon="175"/><node id="2" lat="-37" lon="175.002"/>
              <node id="3" lat="-37.002" lon="175.002"/><node id="4" lat="-37.002" lon="175"/>
              <node id="5" lat="-37.0005" lon="174.9999"/><node id="6" lat="-37.0015" lon="174.9999"/>
              <node id="7" lat="-37.01" lon="175"/><node id="8" lat="-37.012" lon="175"/>
              <node id="9" lat="-37.0008" lon="175.0008"/><node id="10" lat="-37.0008" lon="175.0012"/>
              <node id="11" lat="-37.0012" lon="175.0012"/><node id="12" lat="-37.0012" lon="175.0008"/>
              <way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/></way>
              <way id="101"><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
              <way id="102"><nd ref="9"/><nd ref="10"/><nd ref="11"/><nd ref="12"/><nd ref="9"/></way>
              <way id="200"><nd ref="5"/><nd ref="6"/><tag k="highway" v="path"/></way>
              <way id="300"><nd ref="7"/><nd ref="8"/><tag k="waterway" v="stream"/><tag k="tidal" v="yes"/></way>
              <relation id="400"><member type="way" ref="100" role="outer"/>
                <member type="way" ref="101" role="outer"/><member type="way" ref="102" role="inner"/>
                <tag k="type" v="multipolygon"/><tag k="natural" v="water"/>
                <tag k="water" v="lake"/><tag k="name" v="Synthetic Lake"/>
              </relation></osm>''',encoding='utf-8')
            command=[sys.executable,str(Path(__file__).resolve().parents[1]/'tools/build_water_index.py'),
                     '--input',str(source),'--output',str(output),'--source-url','https://example.invalid/fixture',
                     '--revision','fixture']
            result=subprocess.run(command,capture_output=True,text=True,timeout=30)
            self.assertEqual(0,result.returncode,result.stdout+result.stderr)
            db=sqlite3.connect(output)
            try:
                self.assertEqual([('relation/400','lake')],db.execute('SELECT source_id,kind FROM features').fetchall())
                from shapely import wkb
                geometry=wkb.loads(db.execute('SELECT geometry FROM features').fetchone()[0])
                self.assertEqual(1,len(geometry.geoms[0].interiors))
                self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='approaches'").fetchall())
                water=WaterStore(output)
                try:
                    rows,total,stats=water.candidates({'latitude':-37.001,'longitude':174.9999},WaterSettings())
                    self.assertTrue(rows)
                    self.assertTrue(all(abs(r['longitude']-174.9999)<1e-10 and r['water_longitude']==175 for r in rows))
                finally:water.close()
                metadata=json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
                self.assertEqual(1,metadata['statistics']['features'])
            finally:db.close()
            before=output.read_bytes()
            refused=subprocess.run(command,capture_output=True,text=True,timeout=30)
            self.assertNotEqual(0,refused.returncode);self.assertEqual(before,output.read_bytes())

    def test_polygon_target_is_boundary_not_centroid(self):
        from tools.build_water_index import segment_approach
        lake=Polygon([(175,-37),(175.002,-37),(175.002,-37.002),(175,-37.002)])
        path=LineString([(174.9999,-37.0005),(174.9999,-37.0015)])
        point,water,gap=segment_approach(path,lake)
        self.assertEqual(175,water['longitude']);self.assertLess(gap,10)
        self.assertEqual(174.9999,point['longitude'])

    def test_inside_on_water_and_crossing_path_are_rejected(self):
        from tools.build_water_index import segment_approach
        lake=Polygon([(175,-37),(175.002,-37),(175.002,-37.002),(175,-37.002)])
        for path in (LineString([(175.0005,-37.0005),(175.0015,-37.0015)]),
                     LineString([(174.9999,-37.001),(175.003,-37.001)])):
            self.assertIsNone(segment_approach(path,lake))
        stream=LineString([(175,-37),(175,-37.002)])
        self.assertIsNone(segment_approach(LineString([(174.9999,-37.001),(175.0001,-37.001)]),stream))

    def test_cliff_barrier_or_bridge_near_endpoint_excludes_candidate(self):
        from tools.build_water_index import segment_approach
        stream=LineString([(175,-37),(175,-37.002)])
        path=LineString([(174.9999,-37.0005),(174.9999,-37.0015)])
        cliff=LineString([(174.99995,-37),(174.99995,-37.002)])
        self.assertIsNotNone(segment_approach(path,stream))
        self.assertIsNone(segment_approach(path,stream,[cliff]))

    def test_distant_geometry_is_not_an_approach(self):
        from tools.build_water_index import segment_approach
        path=LineString([(174.99,-37.0005),(174.99,-37.0015)])
        stream=LineString([(175,-37),(175,-37.002)])
        self.assertIsNone(segment_approach(path,stream))


if __name__=='__main__':unittest.main()
