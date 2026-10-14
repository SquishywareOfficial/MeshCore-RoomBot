from dataclasses import replace
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from room_bot.config import Config, ConfigError, RoutingSettings
from room_bot.geography import distance_reply
from room_bot.routing import (GRAPH_FILES, PROFILE, RouteResult, RoutinoRouter, RoutingError,
                              file_sha256, parse_route)
from room_bot.commands import Dispatcher
from room_bot.offline import install_offline_commands
from test_commands import post
import test_geography


ORIGIN = {'id': 'one', 'latitude': -37.0, 'longitude': 175.0}
DESTINATION = {'id': 'two', 'latitude': -37.01, 'longitude': 175.0}
OUTPUT = '# synthetic Routino all-text fixture\n-37.000000 175.000000 1 Waypt 0.000 0.00 0.00 0.0\n-37.010000 175.000000 2 Waypt 1.500 1.00 1.50 1.0\n'


def graph_fixture(root, revision='test-rev'):
    graph = root / revision
    graph.mkdir()
    for name in GRAPH_FILES:
        (graph / name).write_bytes(b'fake graph, parser unit tests only')
    xml = ET.Element('routino-profiles')
    foot = ET.SubElement(xml, 'profile', {'name': PROFILE, 'transport': 'foot'})
    prefs = ET.SubElement(foot, 'preferences')
    for h in ('motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified',
              'residential', 'service', 'track', 'cycleway', 'path', 'steps', 'ferry'):
        ET.SubElement(prefs, 'preference', {'highway': h, 'percent': '0' if h in ('motorway','ferry') else '100'})
    properties = ET.SubElement(foot, 'properties')
    for name in ('paved', 'multilane', 'bridge', 'tunnel', 'footroute', 'bicycleroute'):
        ET.SubElement(properties, 'property', {'type':name,'percent':'50'})
    (graph / 'profiles.xml').write_bytes(ET.tostring(xml))
    (graph / 'tagging.xml').write_text('<routino-tagging/>')
    (graph / 'translations.xml').write_text('<routino-translations/>')
    manifest = {'version':1, 'engine':'routino', 'engine_version':'Routino test', 'profile':PROFILE,
                'revision': revision, 'source_url':'https://example.invalid/synthetic',
                'files': {name: {'bytes':(graph/name).stat().st_size,'sha256':file_sha256(graph/name)}
                          for name in GRAPH_FILES + ('profiles.xml','tagging.xml','translations.xml')}}
    (graph / 'manifest.json').write_text(json.dumps(manifest))
    return graph


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.graph = graph_fixture(self.root)
        self.settings = RoutingSettings(backend='routino', graph_dir=str(self.graph), executable='synthetic-router')
        self.router = RoutinoRouter(self.settings, self.root)
        self.version_patch = patch('room_bot.routing.subprocess.run', return_value=
            subprocess.CompletedProcess([], 0, stdout=b'', stderr=b'Routino test\n'))
        self.version_patch.start()

    def tearDown(self):
        self.version_patch.stop()
        self.temp.cleanup()

    def test_settings_validate_and_roundtrip_old_configs_remain_off(self):
        self.assertEqual('none', Config.from_dict({'version':1}).routing.backend)
        config = replace(Config(), routing=self.settings)
        self.assertEqual(config, Config.from_dict(config.to_dict()))
        for options in ({'backend':'http'}, {'timeout_seconds':0}, {'max_snap_metres':True},
                        {'output_limit_bytes':1}, {'executable':'router\n'}, {'cache_entries':0}):
            with self.subTest(options=options), self.assertRaises(ConfigError):
                Config.from_dict({'routing':options})

    def test_parser_correct_totals_snaps_and_no_path(self):
        distance,start,end,count = parse_route(io.StringIO(OUTPUT), ORIGIN, DESTINATION, 1000)
        self.assertEqual(1500, distance)
        self.assertEqual((0,0,2),(start,end,count))
        for value in ('', OUTPUT.replace('1.50','nan'), OUTPUT.replace('1.50','0.01'),
                      OUTPUT.replace('175.000000','999.000000'), OUTPUT.replace('Waypt','Badpt'),
                      OUTPUT.replace('1.50','-1.0'), OUTPUT.replace('0.000 0.00 0.00 0.0','0.000 0.00 0.10 0.0')):
            with self.subTest(value=value), self.assertRaises(RoutingError):
                parse_route(io.StringIO(value), ORIGIN, DESTINATION, 1000)
        with self.assertRaisesRegex(RoutingError,'endpoint'):
            parse_route(io.StringIO(OUTPUT), dict(ORIGIN,latitude=-38), DESTINATION, 1000)

    def test_snapshot_hash_version_profile_validation_and_mutations(self):
        self.router.verify()
        (self.graph/'nodes.mem').write_bytes(b'changed')
        with self.assertRaisesRegex(RoutingError,'changed'):
            self.router.verify()
        fresh=RoutinoRouter(self.settings,self.root)
        with self.assertRaisesRegex(RoutingError,'invalid'):
            fresh.verify()
        (self.graph/'nodes.mem').unlink()
        with self.assertRaises(RoutingError):
            self.router.verify()

    def test_incorrect_engine_version_rejected(self):
        with patch('room_bot.routing.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'Routino other',b'')):
            with self.assertRaisesRegex(RoutingError,'version differs'):
                self.router.verify()

    def test_profile_must_exclude_ferries_and_use_neutral_shortest_preferences(self):
        manifest=json.loads((self.graph/'manifest.json').read_text())
        profile=self.graph/'profiles.xml'
        profile.write_text(profile.read_text().replace('highway="ferry" percent="0"', 'highway="ferry" percent="100"'))
        manifest['files']['profiles.xml']={'bytes':profile.stat().st_size,'sha256':file_sha256(profile)}
        (self.graph/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RoutingError,'invalid'):
            self.router.verify()

    def test_actual_numbered_waypoint_format(self):
        numbered=OUTPUT.replace('1 Waypt','1 Waypt#1').replace('2 Waypt','2 Waypt#2')
        self.assertEqual(1500,parse_route(io.StringIO(numbered),ORIGIN,DESTINATION,1000)[0])

    def test_geometry_sections_detect_gaps_and_inconsistent_totals(self):
        with self.assertRaisesRegex(RoutingError,'segment/gap'):
            parse_route(io.StringIO(OUTPUT.replace('1.500 1.00','0.050 1.00')),ORIGIN,DESTINATION,1000)
        with self.assertRaisesRegex(RoutingError,'section totals'):
            parse_route(io.StringIO(OUTPUT.replace('1.500 1.00','2.500 1.00')),ORIGIN,DESTINATION,1000)

    def test_cache_survives_restart_is_directional_bounded_and_revision_specific(self):
        with patch.object(self.router,'_run',return_value=(1500,0,0,2)) as run:
            self.router.route(ORIGIN,DESTINATION)
            self.router.route(ORIGIN,DESTINATION)
            self.assertEqual(1,run.call_count)
        restarted=RoutinoRouter(self.settings,self.root)
        with patch.object(restarted,'_run',side_effect=AssertionError('Cache miss')):
            self.assertEqual(1500,restarted.route(ORIGIN,DESTINATION).distance_m)
        bounded=RoutinoRouter(replace(self.settings,cache_entries=1),self.root)
        with patch.object(bounded,'_run',return_value=(1500,0,0,2)) as run:
            bounded.route(DESTINATION,ORIGIN)
            self.assertEqual(1,run.call_count)
        with closing(sqlite3.connect(self.root/'routing-cache.sqlite3')) as conn:
            self.assertEqual(1,conn.execute('SELECT count(*) FROM routes').fetchone()[0])
        graph=graph_fixture(self.root,'next-revision')
        newer=RoutinoRouter(replace(self.settings,graph_dir=str(graph)),self.root)
        with patch.object(newer,'_run',return_value=(2000,0,0,2)) as run:
            self.assertEqual(2000,newer.route(ORIGIN,DESTINATION).distance_m)
            self.assertEqual(1,run.call_count)

    def test_corrupt_cache_does_not_emit_guessed_distance(self):
        with patch.object(self.router,'_run',return_value=(1500,0,0,2)):
            self.router.route(ORIGIN,DESTINATION)
        with closing(sqlite3.connect(self.root/'routing-cache.sqlite3')) as conn, conn:
            conn.execute('UPDATE routes SET result=?',(json.dumps({'distance_m':float('nan')}),))
        with self.assertRaises(RoutingError):
            self.router.route(ORIGIN,DESTINATION)

    def test_corrected_place_coordinates_invalidate_prior_cached_result(self):
        with patch.object(self.router,'_run',return_value=(1500,0,0,2)) as run:
            self.router.route(ORIGIN,DESTINATION)
            corrected=dict(DESTINATION,latitude=-37.011)
            self.router.route(ORIGIN,corrected)
            self.assertEqual(2,run.call_count)

    def run_child(self, script, settings=None, timeout_seconds=None):
        # Actual child-process I/O/termination is tested portably; fake graphs
        # must not be mistaken for an actual national engine integration test.
        real_popen=subprocess.Popen
        def launch(command, **kwargs):
            self.assertIn('--transport=foot',command)
            self.assertIn('--shortest',command)
            self.assertNotIn('shell',kwargs)
            return real_popen([sys.executable,'-c',script],**kwargs)
        router=RoutinoRouter(settings or self.settings,self.root)
        with patch('room_bot.routing.subprocess.Popen',side_effect=launch):
            return router._run(ORIGIN,DESTINATION,timeout_seconds=timeout_seconds)

    def test_actual_child_output_and_nonzero_errors(self):
        self.assertEqual(1500,self.run_child('print('+repr(OUTPUT)+')')[0])
        with self.assertRaisesRegex(RoutingError,'No connected'):
            self.run_child("import sys; print('Cannot find route',file=sys.stderr); sys.exit(1)")
        with self.assertRaisesRegex(RoutingError,'router failed'):
            self.run_child("import sys; print('secret/internal diagnostic',file=sys.stderr); sys.exit(2)")

    def test_child_timeout_and_output_bound_kill_and_reap(self):
        with self.assertRaisesRegex(RoutingError,'timed out'):
            self.run_child('import time; time.sleep(10)',replace(self.settings,timeout_seconds=1))
        with self.assertRaisesRegex(RoutingError,'output limit'):
            self.run_child("print('x'*10000)",replace(self.settings,output_limit_bytes=4096))

    def test_shared_water_budget_can_shorten_native_subprocess_timeout(self):
        with self.assertRaisesRegex(RoutingError,'timed out'):
            self.run_child('import time; time.sleep(10)',timeout_seconds=0.1)


class RoutingIntegrationTests(unittest.TestCase):
    setUp = test_geography.GeographyTests.setUp
    tearDown = test_geography.GeographyTests.tearDown
    def test_backend_selected_only_for_walking_and_returns_bounded_shared_replies(self):
        class Router:
            def route(self,a,b):
                return RouteResult(123000,10,20,'test-rev','https://example.invalid/test',2)
        router=Router()
        self.assertIn('mapped walking route approx 123.0 km', ' '.join(distance_reply(
            self.store,'Synthetic Alpha to Synthetic Beta'.split(),router)))
        with patch.object(router,'route',side_effect=AssertionError('Wrong mode routed')):
            self.assertIn('straight-line',' '.join(distance_reply(self.store,'Synthetic Alpha to Synthetic Beta straight'.split(),router)))
            self.assertIn('No saved road',' '.join(distance_reply(self.store,'Synthetic Alpha to Synthetic Beta road'.split(),router)))
        dispatch=Dispatcher(replace(Config(),enabled_commands=('distance',),routing=RoutingSettings(backend='routino')))
        with patch('room_bot.routing.RoutinoRouter',return_value=router):
            install_offline_commands(dispatch,self.root)
        with patch('socket.create_connection',side_effect=AssertionError('Network')):
            replies=dispatch(post('/bot distance Synthetic Alpha to Synthetic Beta'))
        self.assertLessEqual(len(replies),4)
        self.assertTrue(all(len(('[Bot] '+p).encode())<=151 for p in replies))

