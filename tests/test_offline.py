from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from room_bot.commands import Dispatcher
from room_bot.config import Config, Limits
from room_bot.data import OfflineStore, validate_bundle
from room_bot.offline import install_offline_commands
from test_commands import post
from test_data import bundle


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = replace(Config(), enabled_commands=('directory', 'emergency', 'guide'),
                              limits=Limits(request_cooldown_seconds=0))
        self.dispatch = Dispatcher(self.config)
        install_offline_commands(self.dispatch, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def import_bundle(self, value):
        store = OfflineStore(self.root / 'offline.sqlite3')
        try:
            store.import_validated(*validate_bundle(value))
        finally:
            store.close()

    def test_no_data_no_match_and_missing_topic_are_explicit(self):
        self.assertIn('Usage:', self.dispatch(post('/bot directory'))[0])
        self.assertIn('No offline data', self.dispatch(post('/bot directory hospitals', 2))[0])
        self.import_bundle(bundle())
        self.assertIn('No saved match', self.dispatch(post('/bot directory missing', 3))[0])

    def test_directory_is_offline_and_does_not_claim_current_availability(self):
        self.import_bundle(bundle())
        with patch('socket.create_connection', side_effect=AssertionError('Offline lookup accessed network')):
            replies = self.dispatch(post('/bot directory WHANGAREI'))
        result = ''.join(replies)
        self.assertIn('availability unconfirmed', result)
        self.assertIn('Synthetic directory record', result)
        self.assertNotIn('Source:', result)
        self.assertNotIn('https://example.invalid/fixture', result)
        self.assertTrue(all(len(('[Bot] ' + part).encode()) <= 151 for part in replies))

    def test_source_is_explicit_offline_and_keeps_metadata_intact(self):
        self.import_bundle(bundle())
        config=replace(self.config,enabled_commands=self.config.enabled_commands+('source',))
        dispatch=Dispatcher(config);install_offline_commands(dispatch,self.root)
        with patch('socket.create_connection',side_effect=AssertionError('Source lookup used network')):
            normal=''.join(dispatch(post('/bot directory WHANGAREI',1)))
            listing=''.join(dispatch(post('/bot source',2)))
            explicit=''.join(dispatch(post('/bot source directory',3)))
        self.assertNotIn('https://',normal)
        self.assertNotIn('https://',listing)
        self.assertIn('https://example.invalid/fixture',explicit)
        self.assertIn('Test fixture only',explicit)
        self.assertTrue(all(len(('[Bot] '+p).encode())<=151 for p in dispatch(post('/bot source directory',4))))
        store=OfflineStore(self.root/'offline.sqlite3')
        try:self.assertEqual('https://example.invalid/fixture',store.datasets()[0]['source_url'])
        finally:store.close()

    def test_reference_search_has_source_and_no_generated_missing_answer(self):
        value = bundle('emergency')
        value['records'] = [{'id': 'test', 'title': 'Synthetic reference',
                             'topics': ['test-only'], 'text': 'Synthetic reference text; not emergency advice.'}]
        self.import_bundle(value)
        with patch('socket.create_connection', side_effect=AssertionError('Network accessed')):
            result = ''.join(self.dispatch(post('/bot emergency test-only')))
        self.assertIn('Saved reference', result)
        self.assertIn('Synthetic reference text', result)
        self.assertIn('No saved match', self.dispatch(post('/bot emergency real-event', 2))[0])

    def test_bundled_reference_files_pass_schema_and_carry_license_and_sources(self):
        from room_bot.data import read_bundle
        paths = list((Path(__file__).resolve().parents[1] / 'datasets/references').glob('*.json'))
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            meta, records = read_bundle(path)
            self.assertEqual('emergency', meta['kind'])
            self.assertIn('National Emergency Management Agency', meta['title'])
            self.assertIn('CC BY 4.0', meta['license'])
            self.assertTrue(meta['source_url'].startswith('https://getready.govt.nz/'))
            self.assertTrue(records)

    def test_existing_unknown_db_is_not_reset_by_a_request(self):
        import sqlite3
        path = self.root / 'offline.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE unknown (value TEXT)')
        db.close()
        before = path.read_bytes()
        with self.assertLogs(level='ERROR'):
            replies = self.dispatch(post('/bot directory anything'))
        self.assertIn('unavailable', replies[0])
        self.assertEqual(before, path.read_bytes())

    def test_pinned_practical_guides_import_and_return_instructions_without_executing_them(self):
        from room_bot.data import read_bundle
        path=Path(__file__).resolve().parents[1]/'datasets/guides/xiao-enhanced-sq7.json'
        metadata,records=read_bundle(path)
        store=OfflineStore(self.root/'offline.sqlite3')
        try: store.import_validated(metadata,records)
        finally: store.close()
        reply=self.dispatch(post('/bot guide dht11'))
        self.assertIn('set environment.sensor dht11',''.join(reply[:3]))
        self.assertTrue(all(len(('[Bot] '+part).encode())<=151 for part in reply))
