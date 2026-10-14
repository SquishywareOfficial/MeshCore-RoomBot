"""Chat-facing acceptance checks for the location command group."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from room_bot.commands import Dispatcher
from room_bot.config import Config, Limits
from room_bot.data import OfflineStore, read_bundle
from room_bot.offline import install_offline_commands
from test_commands import post


class LocationCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = OfflineStore(self.root / 'offline.sqlite3')
        self.store.import_validated(*read_bundle(
            Path(__file__).resolve().parents[1] /
            'datasets/places/linz-settlements-2026-10-13.json'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def dispatcher(self, enabled=('help', 'location'), cooldown=0):
        config = replace(Config(), enabled_commands=enabled,
                         limits=Limits(request_cooldown_seconds=cooldown))
        dispatch = Dispatcher(config, '[SquishyBot] ')
        install_offline_commands(dispatch, self.root)
        return dispatch

    def test_main_help_collapses_location_into_one_entry(self):
        dispatch = self.dispatcher(('ping', 'help', 'about', 'location', 'source'))
        replies = dispatch(post('/bot help'))
        self.assertEqual(1, len(replies))
        self.assertIn('/bot location help', replies[0])
        for hidden in ('TYPE', 'nearby', '/bot location towns', '/bot location distance'):
            self.assertNotIn(hidden, replies[0])

    def test_location_help_and_per_command_help_do_not_open_data_or_run_routes(self):
        dispatch = self.dispatcher()
        with patch('room_bot.offline.OfflineStore', side_effect=AssertionError('Help queried data')):
            for seq, text in enumerate(('/bot location', '/bot location help', '/bot help location'), 1):
                replies = dispatch(post(text, seq))
                self.assertIn('towns, water, distance, bearing', ''.join(replies))
            for seq, text in enumerate(('/bot location towns help', '/bot location help towns'), 4):
                text = ''.join(dispatch(post(text, seq)))
                self.assertIn('straight line', text)
                self.assertIn('/bot location towns Hamilton', text)
                self.assertNotIn('TYPE', text)
            self.assertIn('Unknown or disabled location command',
                          ''.join(dispatch(post('/bot location river Waikato', 6))))

    def test_hamilton_towns_is_short_useful_and_has_no_category_argument(self):
        dispatch = self.dispatcher()
        replies = dispatch(post('/bot location towns Hamilton'))
        text = ''.join(replies)
        self.assertIn('Temple View', text)
        self.assertIn('Rotokauri', text)
        self.assertIn('straight-line', text)
        self.assertLessEqual(len(replies), 2)
        for noise in ('More:', 'services/access', 'Source:', 'https://', 'Usage:'):
            self.assertNotIn(noise, text)
        self.assertTrue(all(len(('[SquishyBot] ' + r).encode()) <= 151 for r in replies))

    def test_coordinates_and_full_location_are_not_misread_as_a_category(self):
        dispatch = self.dispatcher()
        replies = dispatch(post('/bot location towns -37.798802,175.245742'))
        self.assertIn('Temple View', ''.join(replies))
        self.assertNotIn('Usage:', ''.join(replies))
        self.assertIn('Coordinates:', ''.join(dispatch(post(
            '/bot location place -37.798802,175.245742', 2))))
        self.assertIn('True:', ''.join(dispatch(post(
            '/bot location bearing true Hamilton to Raglan', 3))))
        self.assertIn('not a walking route', ''.join(dispatch(post(
            '/bot location distance Hamilton to Raglan straight', 4))))

    def test_missing_arguments_explain_the_new_command(self):
        dispatch = self.dispatcher()
        for seq, verb in enumerate(('towns', 'distance', 'bearing', 'water', 'place', 'walktime'), 1):
            replies = dispatch(post('/bot location ' + verb, seq))
            self.assertIn('/bot location ' + verb, ''.join(replies))

    def test_per_feature_enablement_cannot_be_bypassed_with_the_group(self):
        dispatch = self.dispatcher(('help', 'towns'))
        help_text = ''.join(dispatch(post('/bot location help')))
        self.assertIn('towns', help_text)
        self.assertNotIn('water', help_text)
        self.assertIn('disabled', ''.join(dispatch(post('/bot location water Hamilton', 2))))
        self.assertIn('disabled', ''.join(self.dispatcher(('help',))(post('/bot location towns Hamilton'))))

    def test_old_nearby_configuration_exposes_towns_and_a_rename_hint(self):
        dispatch = self.dispatcher(('help', 'nearby'))
        self.assertIn('/bot location help', ''.join(dispatch(post('/bot help'))))
        self.assertIn('Temple View', ''.join(dispatch(post('/bot location towns Hamilton', 2))))
        self.assertIn('Nearby is now towns', ''.join(dispatch(post('/bot nearby towns Hamilton', 3))))

    def test_group_lookups_keep_rate_limits_but_help_remains_available(self):
        dispatch = self.dispatcher(cooldown=300)
        dispatch(post('/bot location towns Hamilton'))
        self.assertIn('Please wait', ''.join(dispatch(post('/bot location towns Raglan', 2))))
        self.assertIn('Example:', ''.join(dispatch(post('/bot location towns help', 3))))
        self.assertIn('Please wait', ''.join(dispatch(post('/bot towns Raglan', 4))))

    def test_multiline_replies_keep_distance_and_unit_on_the_same_message(self):
        from room_bot.commands import split_utf8
        text = ('Nearest towns/localities to Tattersfield Place near Hamilton '
                '(street reference point), straight-line:\n'
                'Temple View: 3.1 km\nHamilton: 3.1 km\nKoromatua: 5.0 km')
        chunks = split_utf8(text, 139)
        self.assertEqual(text, ''.join(chunks))
        self.assertEqual(2, len(chunks))
        self.assertTrue(chunks[0].endswith('\n'))
        for result in ('Temple View: 3.1 km', 'Hamilton: 3.1 km', 'Koromatua: 5.0 km'):
            self.assertTrue(any(result in chunk for chunk in chunks))
        # A multibyte character after a long token must still respect the bound.
        chunks = split_utf8(' ' + 'x' * 63 + '🦑', 64)
        self.assertEqual(' ' + 'x' * 63 + '🦑', ''.join(chunks))
        self.assertTrue(all(len(chunk.encode()) <= 64 for chunk in chunks))
