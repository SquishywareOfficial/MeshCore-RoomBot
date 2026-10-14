from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from room_bot.data import DataError, OfflineStore, main, read_bundle, validate_bundle


def bundle(kind='directory'):
    return {'format_version': 1,
            'dataset': {'id': 'test', 'kind': kind, 'title': 'Synthetic test fixture',
                        'source_url': 'https://example.invalid/fixture', 'license': 'Test fixture only',
                        'revision': '1', 'retrieved_at': '2026-10-13T00:00:00Z',
                        'coverage': 'Synthetic data; no real-world services'},
            'records': [{'id': 'one', 'title': 'Whangārei test', 'location': 'Test region',
                         'text': 'Synthetic directory record', 'aliases': ['test']} ]}


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'offline.sqlite3'
        self.store = OfflineStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_repeatable_import_replaces_only_selected_dataset_and_survives_restart(self):
        first = bundle()
        second = bundle()
        second['dataset']['id'] = 'other'
        self.store.import_validated(*validate_bundle(first))
        self.store.import_validated(*validate_bundle(second))
        first['records'][0]['text'] = 'Updated record'
        self.store.import_validated(*validate_bundle(first))
        self.assertEqual([1, 1], [d['record_count'] for d in self.store.datasets()])
        self.store.close()
        self.store = OfflineStore(self.path)
        self.assertEqual('Updated record', self.store.query('directory', 'updated')[0][0]['text'])

    def test_partial_validation_or_database_failure_preserves_old_dataset(self):
        old = bundle()
        self.store.import_validated(*validate_bundle(old))
        invalid = deepcopy(old)
        invalid['records'].append({'id': 'bad'})
        with self.assertRaises(DataError):
            self.store.import_validated(invalid['dataset'], invalid['records'])
        self.store.db.execute("CREATE TRIGGER reject_record BEFORE INSERT ON records BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        self.store.db.commit()
        old['records'][0]['text'] = 'Should not replace old record'
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.import_validated(old['dataset'], old['records'])
        self.assertEqual('Synthetic directory record', self.store.query('directory')[0][0]['text'])

    def test_schema_and_version_are_not_silently_adopted(self):
        other = self.path.parent / 'unknown.sqlite3'
        with sqlite3.connect(other) as db:
            db.execute('CREATE TABLE other (x INTEGER)')
        db.close()
        before = other.read_bytes()
        with self.assertRaises(DataError):
            OfflineStore(other)
        self.assertEqual(before, other.read_bytes())
        self.store.db.execute("UPDATE schema_meta SET value='2'")
        self.store.db.commit()
        self.store.close()
        with self.assertRaises(DataError):
            OfflineStore(self.path)
        # Restore known test schema so tearDown has an open store to close.
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE schema_meta SET value='1'")
        db.close()
        self.store = OfflineStore(self.path)

    def test_search_is_local_bounded_literal_and_macron_insensitive(self):
        self.store.import_validated(*validate_bundle(bundle()))
        with patch('socket.create_connection', side_effect=AssertionError('Network must not be used')):
            self.assertEqual(1, len(self.store.query('directory', 'WHANGAREI', 1)))
            self.assertEqual([], self.store.query('directory', '%'))
            self.assertEqual([], self.store.query('directory', "' OR 1=1 --"))
            self.assertEqual([], self.store.query('places', 'Hamilton'))

    def test_malformed_metadata_records_and_duplicate_ids_are_rejected(self):
        values = []
        for field, value in [('format_version', True), ('format_version', 2), ('records', [])]:
            data = bundle(); data[field] = value; values.append(data)
        for field, value in [('source_url', 'file:///private'), ('license', ''),
                             ('retrieved_at', '2026-10-13'), ('kind', 'unknown')]:
            data = bundle(); data['dataset'][field] = value; values.append(data)
        data = bundle(); data['records'] *= 2; values.append(data)
        data = bundle(); data['records'][0]['text'] = 'bad\0value'; values.append(data)
        for data in values:
            with self.subTest(data=data), self.assertRaises(DataError):
                validate_bundle(data)

    def test_bad_cli_import_does_not_create_database_or_open_usb(self):
        path = self.path.parent / 'invalid.json'
        destination = self.path.parent / 'new-dir'
        path.write_text('{"format_version":1,"format_version":2}')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(1, main(['--data-dir', str(destination), 'import', str(path)]))
        self.assertFalse(destination.exists())

    def test_large_or_invalid_json_file_is_rejected(self):
        path = self.path.parent / 'invalid.json'
        for raw in (b'\xff', b'{', b'{"format_version":NaN}'):
            path.write_bytes(raw)
            with self.assertRaises(DataError):
                read_bundle(path)
        with patch('room_bot.data.MAX_IMPORT_BYTES', 10):
            path.write_bytes(b' ' * 11)
            with self.assertRaises(DataError):
                read_bundle(path)

    def test_geometry_and_route_validation_do_not_accept_guessed_modes_or_units(self):
        data = bundle('places')
        data['records'] = [{'id': 'town', 'name': 'Synthetic town', 'region': 'Test',
                            'feature_type': 'Town', 'latitude': -37.0, 'longitude': 175.0, 'aliases': []}]
        validate_bundle(data)
        for bad in (True, float('nan'), -91):
            data['records'][0]['latitude'] = bad
            with self.assertRaises(DataError):
                validate_bundle(data)
        data = bundle('routes')
        data['records'] = [{'id': 'route', 'origin': 'a', 'destination': 'b', 'mode': 'walk',
                            'distance_m': 1000, 'origin_label': 'a centre', 'destination_label': 'b centre',
                            'profile': 'synthetic walking test', 'summary': 'No real route'}]
        validate_bundle(data)
        data['records'][0]['mode'] = 'straight'
        with self.assertRaises(DataError):
            validate_bundle(data)
