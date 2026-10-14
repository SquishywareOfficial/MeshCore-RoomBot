"""Official WMM reference results and chat-facing compass bearing acceptance."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from room_bot.clock import SessionClock
from room_bot.commands import Dispatcher
from room_bot.config import Config, Limits
from room_bot.data import OfflineStore, read_bundle
from room_bot.geography import bearing_angle, bearing_reply
from room_bot.magnetic import MagneticUnavailable, compass_bearing, decimal_year, declination
from room_bot.offline import install_offline_commands
from test_commands import post


DAY = datetime(2026, 10, 14, tzinfo=timezone.utc)


def fixed_clock(day=DAY):
    return SessionClock(True, wall=lambda: day.timestamp(), monotonic=lambda: 0)


class MagneticModelTests(unittest.TestCase):
    def test_declination_matches_all_official_noaa_wmm2025_reference_rows(self):
        path = Path(__file__).parent/'fixtures/wmm2025_test_values.txt'
        count = 0
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            row = list(map(float, line.split()))
            year, height, latitude, longitude = row[:4]
            longitude = (longitude + 180) % 360 - 180
            with self.subTest(year=year, latitude=latitude, longitude=longitude, height=height):
                self.assertAlmostEqual(row[10], declination(latitude, longitude, year, height), delta=0.02)
            count += 1
        self.assertEqual(12, count)

    def test_compass_conversion_sign_wrap_and_cardinal_rounding(self):
        self.assertEqual(15, compass_bearing(36, 21))
        self.assertEqual(57, compass_bearing(36, -21))
        self.assertEqual(350, compass_bearing(10, 20))
        self.assertEqual('0° N', bearing_angle(359.8))
        self.assertEqual('15° NNE', bearing_angle(15))
        self.assertEqual('36° NE', bearing_angle(36))
        for value in (float('nan'), float('inf'), True):
            with self.assertRaises(MagneticUnavailable):
                compass_bearing(value, 21)

    def test_validity_leap_year_and_weak_field_are_handled(self):
        self.assertEqual(2026, decimal_year(datetime(2026, 1, 1, tzinfo=timezone.utc)))
        self.assertEqual(2028.5, decimal_year(datetime(2028, 7, 2, tzinfo=timezone.utc)))
        for year in (2024.9, 2030, float('nan')):
            with self.assertRaises(MagneticUnavailable):
                declination(-37, 175, year)
        with self.assertRaisesRegex(MagneticUnavailable, 'too weak'):
            declination(85, 139, 2026)


class BearingCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = OfflineStore(self.root/'offline.sqlite3')
        self.store.import_validated(*read_bundle(
            Path(__file__).resolve().parents[1]/'datasets/places/linz-settlements-2026-10-13.json'))
        config = replace(Config(), enabled_commands=('help', 'location'),
                         limits=Limits(request_cooldown_seconds=0))
        self.dispatch = Dispatcher(config, '[SquishyBot] ', clock=fixed_clock())
        install_offline_commands(self.dispatch, self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_default_compass_prefix_suffix_true_and_both_are_short_and_labelled(self):
        prefix = '/bot location bearing '
        commands = ('Hamilton to Raglan', 'compass Hamilton to Raglan',
                    'Hamilton to Raglan compass', 'true Hamilton to Raglan',
                    'Hamilton to Raglan true', 'both Hamilton to Raglan')
        results = []
        with patch('socket.create_connection', side_effect=AssertionError('Network used')):
            for seq, command in enumerate(commands, 1):
                replies = self.dispatch(post(prefix+command, seq))
                self.assertEqual(1, len(replies))
                self.assertLessEqual(len(('[SquishyBot] '+replies[0]).encode()), 151)
                for noise in ('not a route', 'magnetic compass correction', 'Source:', 'https://'):
                    self.assertNotIn(noise, replies[0])
                results.append(replies[0])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])
        self.assertIn('Compass: 245° WSW', results[0])
        self.assertIn('True: 266° W', results[3])
        self.assertEqual(results[3], results[4])
        self.assertIn('Compass: 245° WSW; true: 266° W', results[5])

    def test_missing_or_untrusted_clock_never_labels_true_as_compass(self):
        text = bearing_reply(self.store, ['Hamilton', 'to', 'Raglan'], SessionClock())
        self.assertIn('Compass unavailable (clock unconfirmed)', text)
        self.assertIn('True: 266° W', text)
        self.assertNotIn('Compass: 266', text)
        with patch('room_bot.magnetic.declination', side_effect=AssertionError('True mode called WMM')):
            text = bearing_reply(self.store, ['true', 'Hamilton', 'to', 'Raglan'])
        self.assertIn('True: 266° W', text)

    def test_expired_model_missing_dependency_and_clock_jump_remain_explicit(self):
        text = bearing_reply(self.store, ['Hamilton', 'to', 'Raglan'],
                             fixed_clock(datetime(2030, 1, 1, tzinfo=timezone.utc)))
        self.assertIn('outside WMM2025 validity', text)
        with patch('room_bot.magnetic.declination', side_effect=MagneticUnavailable('model missing')):
            text = bearing_reply(self.store, ['Hamilton', 'to', 'Raglan'], fixed_clock())
        self.assertIn('Compass unavailable (model missing)', text)
        wall = [DAY.timestamp()]
        clock = SessionClock(True, wall=lambda: wall[0], monotonic=lambda: 0)
        wall[0] += 3600
        text = bearing_reply(self.store, ['both', 'Hamilton', 'to', 'Raglan'], clock)
        self.assertIn('clock unconfirmed', text)

    def test_help_and_bad_mode_syntax_do_not_trigger_model_calculations(self):
        with patch('room_bot.magnetic.declination', side_effect=AssertionError('Unnecessary WMM calculation')):
            for seq, command in enumerate(('help', 'compass', 'true Hamilton to Raglan both',
                                           'Hamilton to Raglan walk'), 1):
                text = ''.join(self.dispatch(post('/bot location bearing '+command, seq)))
                self.assertIn('compass', text)
                self.assertIn('true', text)
                self.assertIn('both', text)

    def test_declination_details_are_opt_in_and_use_the_origin(self):
        self.dispatch(post('/bot location bearing both Hamilton to Raglan'))
        text = ''.join(self.dispatch(post('/bot location details', 2)))
        self.assertIn('WMM2025 at origin', text)
        self.assertIn('2026-10-14', text)
        self.assertIn('declination', text)
        self.assertIn('east', text)
