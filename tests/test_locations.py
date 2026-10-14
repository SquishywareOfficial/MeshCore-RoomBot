from dataclasses import replace
from contextlib import redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from room_bot.commands import Dispatcher
from room_bot.config import Config,Limits,RoutingSettings
from room_bot.data import OfflineStore,validate_bundle
from room_bot.geography import PlaceError,distance_reply,place_reply,straight_distance
from room_bot.locations import resolve_location
from room_bot.offline import install_offline_commands
from room_bot.routing import RoutinoRouter,RoutingError,parse_route
from room_bot.streets import StreetStore,representative,street_key
from tools.build_street_index import create_index,components
from test_commands import post
from test_data import bundle
from test_geography import town


class LocationTests(unittest.TestCase):
    def test_suburb_city_hint_resolves_suburb_without_needing_street_data(self):
        data = bundle('places')
        data['records'] = [town('city', 'Synthetic City', -37, 175),
                           dict(town('near', 'Synthetic Suburb', -37.01, 175), feature_type='Suburb'),
                           dict(town('far', 'Synthetic Suburb', -40, 175), feature_type='Suburb')]
        self.store.import_validated(*validate_bundle(data))
        (self.root/'streets.sqlite3').unlink()
        for text in ('Synthetic Suburb Synthetic City', 'Synthetic Suburb, Synthetic City'):
            result, _ = resolve_location(self.store, text)
            self.assertEqual('near', result['id'])
            self.assertAlmostEqual(-37.01, result['latitude'])
        with self.assertRaisesRegex(PlaceError, 'Ambiguous place'):
            resolve_location(self.store, 'Synthetic Suburb')
        # Two nearby exact matches must not be silently resolved to the nearest.
        data['records'].append(dict(town('another', 'Synthetic Suburb', -37.02, 175), feature_type='Suburb'))
        self.store.import_validated(*validate_bundle(data))
        with self.assertRaisesRegex(PlaceError, 'Several places match'):
            resolve_location(self.store, 'Synthetic Suburb Synthetic City')

    def test_location_towns_accepts_the_same_street_found_by_place(self):
        config = replace(Config(), enabled_commands=('location',),
                         limits=Limits(request_cooldown_seconds=0))
        dispatch = Dispatcher(config, '[SquishyBot] ')
        install_offline_commands(dispatch, self.root)
        place = dispatch(post('/bot location place Synthetic Street Synthetic City'))
        self.assertIn('street:10@point', ''.join(place))
        nearby = dispatch(post('/bot location towns Synthetic Street Synthetic City', 2))
        self.assertIn('Synthetic City: 0.1 km', ''.join(nearby))
        self.assertIn('street reference point', ''.join(nearby))
        for noise in ('Usage:', 'No matching street', 'TYPE', 'Source:'):
            self.assertNotIn(noise, ''.join(nearby))
        self.assertTrue(all(len(('[SquishyBot] '+r).encode()) <= 151 for r in nearby))

    def test_normal_chat_place_answer_is_one_message_with_no_internal_metadata(self):
        dispatch=Dispatcher(replace(Config(),enabled_commands=('place','details'),limits=Limits(request_cooldown_seconds=0)),prefix='[SquishyBot] ')
        install_offline_commands(dispatch,self.root)
        street=dispatch(post('/bot place Synthetic Street, Synthetic City',1))
        self.assertEqual(1,len(street))
        self.assertIn('-37.001000,175.000000',street[0]);self.assertIn('street:10@point',street[0])
        for noise in ('Source:','https://','WGS84','municipal','source area','accuracy unknown'):
            self.assertNotIn(noise,street[0])
        self.assertIn('house address',''.join(dispatch(post('/bot details',2))))
        coordinate=dispatch(post('/bot place -37.7313255,175.2215203',3))
        self.assertEqual(1,len(coordinate));self.assertEqual(1,coordinate[0].count('-37.731325'))
        self.assertNotIn('User input',coordinate[0]);self.assertNotIn('WGS84',coordinate[0])
        self.assertTrue(all(len(('[SquishyBot] '+p).encode())<=151 for p in street+coordinate))

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=OfflineStore(self.root/'offline.sqlite3')
        data=bundle('places');data['records']=[town('a','Synthetic City',-37,175),town('b','Other City',-38,175)]
        self.store.import_validated(*validate_bundle(data))
        rows=[{'id':10,'name':'Synthetic Street','path':[[1,-37,175],[2,-37.001,175]]},
              {'id':11,'name':'Synthetic Street','path':[[2,-37.001,175],[3,-37.002,175]]},
              {'id':20,'name':'Synthetic Street','path':[[4,-38,175],[5,-38.001,175]]}]
        self.metadata={'title':'Synthetic street test','source_url':'https://example.invalid/fixture','revision':'test'}
        with redirect_stdout(io.StringIO()):create_index(self.root/'streets.sqlite3',rows,self.metadata)

    def tearDown(self):self.store.close();self.temp.cleanup()

    def test_coordinates_validated_canonicalized_and_not_confused_with_area_hint(self):
        a,_=resolve_location(self.store,' -37.7313255 , 175.2215203 ')
        self.assertEqual('coordinate',a['_origin_kind']);self.assertAlmostEqual(-37.7313255,a['latitude'])
        self.assertEqual('coord:0.00000000,0.00000000',resolve_location(self.store,'-0,0')[0]['id'])
        for value in ('175,-37','91,0','-37,181','nan,175','-37,inf','1e2,175','1,2,3','-37,175\nreboot'):
            with self.subTest(value=value),self.assertRaises(PlaceError):resolve_location(self.store,value)

    def test_components_merge_by_node_identity_not_name_or_coordinate_proximity(self):
        rows=[{'id':1,'path':[[1,0,0],[2,0,1]]},{'id':2,'path':[[2,0,1],[3,0,2]]},
              {'id':3,'path':[[4,0,2],[5,0,3]]}]
        groups=components(rows)
        self.assertEqual([1,2],sorted([len(g) for g in groups]))
        self.assertEqual('maui street',street_key('MĀUI St.'))

    def test_street_locality_and_explicit_point_are_shared_for_place_distance(self):
        for name in ('Synthetic St Synthetic City','Synthetic Street, Synthetic City'):
            with self.assertRaisesRegex(PlaceError,'not one position'):
                resolve_location(self.store,name)
            a,_=resolve_location(self.store,name+' @point')
            self.assertEqual('street:10',a['id']);self.assertAlmostEqual(-37.001,a['latitude'])
        with self.assertRaisesRegex(PlaceError,'Ambiguous street'):
            resolve_location(self.store,'Synthetic Street @point')
        point,_=resolve_location(self.store,'street:20@point')
        self.assertEqual('street:20',point['id'])
        self.assertIn('street reference point',place_reply(self.store,'Synthetic Street, Synthetic City'))
        self.assertNotIn('https://',place_reply(self.store,'Synthetic Street, Synthetic City'))
        self.assertNotIn('Source:',place_reply(self.store,'Synthetic Street, Synthetic City'))
        details=[]
        result=' '.join(distance_reply(self.store,'Synthetic Street Synthetic City @point to -37.003,175 straight'.split(),details=details))
        self.assertIn('street representative point',' '.join(details));self.assertIn('user coordinate',' '.join(details))
        self.assertNotIn('Origins:',result)
        self.assertIn('not a walking route',result)

    def test_no_street_index_keeps_existing_place_commands_and_coordinates(self):
        (self.root/'streets.sqlite3').unlink()
        self.assertEqual('a',resolve_location(self.store,'Synthetic City')[0]['id'])
        self.assertEqual('coordinate',resolve_location(self.store,'-37,175')[0]['_origin_kind'])
        with self.assertRaises(PlaceError):resolve_location(self.store,'Synthetic Street Synthetic City')

    def test_dataset_is_readonly_and_does_not_overwrite_previous_import(self):
        path=self.root/'streets.sqlite3';before=path.read_bytes()
        streets=StreetStore(path)
        try:
            with self.assertRaises(sqlite3.OperationalError):streets.db.execute('DELETE FROM streets')
        finally:streets.close()
        with self.assertRaises(Exception):create_index(path,[],self.metadata)
        self.assertEqual(before,path.read_bytes())

    def test_invalid_street_id_is_reported_instead_of_sql_integer_overflow(self):
        dispatch=Dispatcher(replace(Config(),enabled_commands=('place',)))
        install_offline_commands(dispatch,self.root)
        result=dispatch(post('/bot place street:'+'9'*100))
        self.assertIn('unavailable',''.join(result))

    def test_precise_snap_policy_is_stricter_and_separate_per_endpoint(self):
        router=RoutinoRouter(RoutingSettings(),self.root)
        coord=resolve_location(self.store,'-37,175')[0];place=resolve_location(self.store,'Synthetic City')[0]
        self.assertEqual((100,1000),router.snap_bounds(coord,place))
        from io import StringIO
        output='-37.002 175 1 Waypt#1 0.000 0.0 0.00 0.0\n-37.010 175 2 Waypt#2 1.500 1.0 1.50 1.0\n'
        with self.assertRaisesRegex(RoutingError,'100 m'):
            parse_route(StringIO(output),coord,dict(place,latitude=-37.010),(100,1000))

    def test_dispatcher_allows_coordinate_street_parity_offline_and_keeps_reply_bounds(self):
        config=replace(Config(),enabled_commands=('distance','place','bearing','towns'),limits=Limits(request_cooldown_seconds=0))
        dispatch=Dispatcher(config);install_offline_commands(dispatch,self.root)
        with patch('socket.create_connection',side_effect=AssertionError('Network')):
            result=dispatch(post('/bot distance Synthetic St Synthetic City @point to -37.003,175 straight'))
            bearing=dispatch(post('/bot location bearing true -37,175 to -37.003,175',2))
            nearby=dispatch(post('/bot location towns -37,175',3))
        self.assertLessEqual(len(result),4)
        self.assertTrue(all(len(('[Bot] '+p).encode())<=151 for p in result))
        self.assertIn('True:',''.join(bearing));self.assertIn('straight-line',''.join(nearby))

    def test_representative_is_on_geometry_even_across_dateline(self):
        lat,lon,_=representative([[[1,0,179.9],[2,0,-179.9]]])
        self.assertEqual(0,lat);self.assertAlmostEqual(180,abs(lon))
