from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from room_bot.commands import Dispatcher
from room_bot.config import Config, Limits
from room_bot.data import DataError, OfflineStore, read_bundle, validate_bundle
from room_bot.geography import (PlaceError, distance_reply, initial_bearing, towns_reply,
                                parse_pair, resolve_place, straight_distance, walktime_reply)
from room_bot.offline import install_offline_commands
from test_commands import post
from test_data import bundle


def town(identifier, name, latitude, longitude, region='Synthetic test area'):
    return {'id': identifier, 'name': name, 'latitude': latitude, 'longitude': longitude,
            'region': region, 'feature_type': 'Town', 'aliases': []}


class GeographyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = OfflineStore(self.root / 'offline.sqlite3')
        self.places = bundle('places')
        self.places['records'] = [town('a', 'Synthetic Alpha', 0, 0), town('b', 'Synthetic Beta', 0, 1)]
        self.store.import_validated(*validate_bundle(self.places))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_known_spherical_distances_and_dateline(self):
        a, b = self.places['records']
        self.assertAlmostEqual(111.195, straight_distance(a, b) / 1000, places=2)
        self.assertEqual(0, straight_distance(a, a))
        self.assertAlmostEqual(222.39, straight_distance(town('a','a',0,179), town('b','b',0,-179)) / 1000, places=2)
        self.assertAlmostEqual(20015.11, straight_distance(a, town('b','b',0,180)) / 1000, places=2)

    def test_multiword_places_and_default_walking_mode(self):
        self.assertEqual(('Synthetic Alpha', 'Synthetic Beta', 'walk'),
                         parse_pair('Synthetic Alpha to Synthetic Beta'.split()))
        self.assertEqual('road', parse_pair('Synthetic Alpha TO Synthetic Beta ROAD'.split())[2])
        with self.assertRaises(PlaceError):
            parse_pair(['Alpha','Beta'])

    def test_missing_walking_route_is_never_silently_straight_or_reverse(self):
        result = ' '.join(distance_reply(self.store, 'Synthetic Alpha to Synthetic Beta'.split()))
        self.assertIn('No saved walk route', result)
        self.assertIn('straight-line approx 111.2 km', result)
        self.assertIn('not a walking route', result)
        routes = bundle('routes')
        routes['dataset']['id'] = 'test-routes'
        routes['records'] = [{'id':'ab','origin':'a','destination':'b','mode':'walk','distance_m':123000,
                              'origin_label':'Synthetic Alpha test point','destination_label':'Synthetic Beta test point',
                              'profile':'synthetic pedestrian','summary':'Fixture only; not a real walking route'}]
        self.store.import_validated(*validate_bundle(routes))
        self.assertIn('map-derived walk route estimate 123.0 km', ' '.join(distance_reply(self.store, 'Synthetic Alpha to Synthetic Beta'.split())))
        self.assertIn('No saved walk route', ' '.join(distance_reply(self.store, 'Synthetic Beta to Synthetic Alpha'.split())))
        self.assertIn('No saved road route', ' '.join(distance_reply(self.store, 'Synthetic Alpha to Synthetic Beta road'.split())))

    def test_ambiguity_macrons_region_and_unknown_names(self):
        data = bundle('places')
        data['dataset']['id'] = 'other'
        data['records'] = [town('c', 'Synthetic Alpha', 0, 5, 'Another area'), town('d', 'Whangārei Test', 0, 3)]
        self.store.import_validated(*validate_bundle(data))
        with self.assertRaisesRegex(PlaceError, 'Ambiguous'):
            resolve_place(self.store, 'Synthetic Alpha')
        self.assertEqual('c', resolve_place(self.store, 'Synthetic Alpha, Another area')[0]['id'])
        self.assertEqual('c', resolve_place(self.store, 'c')[0]['id'])
        self.assertEqual('d', resolve_place(self.store, 'WHANGAREI TEST')[0]['id'])
        with self.assertRaises(PlaceError):
            resolve_place(self.store, 'Alpha')

    def test_handlers_are_offline_and_output_fits_worker_limits(self):
        dispatch = Dispatcher(replace(Config(), enabled_commands=('place','distance'),
                                      limits=Limits(request_cooldown_seconds=0)))
        install_offline_commands(dispatch, self.root)
        with patch('socket.create_connection', side_effect=AssertionError('Network accessed')):
            result = dispatch(post('/bot distance Synthetic Alpha to Synthetic Beta'))
        self.assertLessEqual(len(result), 4)
        self.assertTrue(all(len(('[Bot] '+part).encode()) <= 151 for part in result))
        self.assertIn('No saved walk route', ''.join(result))

    def test_real_linz_snapshot_is_valid_and_contains_requested_places(self):
        path = Path(__file__).resolve().parents[1] / 'datasets/places/linz-settlements-2026-10-13.json'
        metadata, records = read_bundle(path)
        self.assertGreater(len(records), 1000)
        self.assertEqual(1, len([r for r in records if r['name'] == 'Hamilton']))
        self.assertEqual(1, len([r for r in records if r['name'] == 'Raglan']))
        self.assertIn('CC BY 4.0', metadata['license'])

    def test_national_city_resolves_despite_more_than_1000_area_matches(self):
        path = Path(__file__).resolve().parents[1] / 'datasets/places/linz-settlements-2026-10-13.json'
        self.store.import_validated(*read_bundle(path))
        self.assertEqual(1000,len(self.store.query('places','Auckland',1000)))
        self.assertEqual('Auckland',resolve_place(self.store,'Auckland')[0]['name'])

    def test_real_prepared_pair_answers_offline_with_source_and_mode_labels(self):
        root = Path(__file__).resolve().parents[1]
        self.store.import_validated(*read_bundle(root / 'datasets/places/linz-settlements-2026-10-13.json'))
        self.store.import_validated(*read_bundle(root / 'datasets/routes/hamilton-raglan-2026-10-13.json'))
        with patch('socket.create_connection', side_effect=AssertionError('Offline query made a network request')):
            forward = ' '.join(distance_reply(self.store, ['Hamilton','to','Raglan']))
            reverse = ' '.join(distance_reply(self.store, ['Raglan','to','Hamilton']))
        self.assertIn('walk route estimate 45.3 km', forward)
        self.assertIn('walk route estimate 45.2 km', reverse)
        self.assertIn('access/safety unverified', forward)
        self.assertNotIn('OpenStreetMap', forward)
        self.assertNotIn('https://', forward)
        self.assertIn('SH 23', forward)

    def test_cardinal_bearings_and_undefined_points(self):
        a = town('a','a',0,0)
        self.assertEqual(90,initial_bearing(a,town('b','b',0,1)))
        self.assertEqual(0,initial_bearing(a,town('b','b',1,0)))
        self.assertEqual(270,initial_bearing(a,town('b','b',0,-1)))
        self.assertIsNone(initial_bearing(a,a))
        self.assertIsNone(initial_bearing(a,town('b','b',0,180)))

    def test_towns_ranking_uses_complete_data_not_a_truncated_slice(self):
        self.places['records'] += [town('c','Synthetic Gamma',0,0.1), town('d','Synthetic Delta',0,2)]
        self.store.import_validated(*validate_bundle(self.places))
        result = towns_reply(self.store,['Synthetic','Alpha'],limit=1)
        self.assertIn('straight-line',result)
        self.assertIn('Synthetic Gamma',result)
        self.assertNotIn('Synthetic Beta',result)
        with self.assertRaisesRegex(DataError,'ranking limit'):
            list(self.store.iter_kind('places',max_records=1))

    def test_walking_time_is_a_labelled_pace_estimate_with_validated_inputs(self):
        self.assertIn('10.0 h',walktime_reply(['40']))
        self.assertIn('assumed 4 km/h',walktime_reply(['40']))
        self.assertIn('8.0 h',walktime_reply(['40','5']))
        for args in (['-1'],['nan'],['40','0'],['40','inf'],['40','20']):
            self.assertIn('Enter distance',walktime_reply(args))
