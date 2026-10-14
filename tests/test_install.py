"""Setup integrity and preservation tests; no actual USB/service is started."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import tarfile
import unittest
from unittest.mock import patch
import zipfile

from room_bot.install import (MAP_NAMES, SOURCE, SetupError, download, install,
                              load_manifest, safe_child, sha256, unpack_bundle, unpack_routino)


def record(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, extra=None):
        p = self.root/'data.zip'
        files = {name: record(name.encode()) for name in MAP_NAMES}
        with zipfile.ZipFile(p, 'w') as bundle:
            for name in files:
                bundle.writestr(name, name.encode())
            if extra:
                bundle.writestr(extra, b'bad')
        manifest = {'version': 1, 'id': 'nz-fixture', 'files': files,
                    'archive': {'bytes': p.stat().st_size, 'sha256': sha256(p), 'url': None}}
        return p, manifest

    def test_valid_archive_expands_only_whitelisted_verified_assets(self):
        archive, manifest = self.archive()
        path = self.root/'manifest.json';path.write_text(json.dumps(manifest))
        self.assertEqual(manifest, load_manifest(path))
        with redirect_stdout(io.StringIO()):
            unpack_bundle(archive, self.root/'new-data', manifest)
        for name in MAP_NAMES:
            self.assertEqual(name.encode(), safe_child(self.root/'new-data', name).read_bytes())

    def test_unsafe_extra_and_corrupted_archives_never_write_state(self):
        state = self.root/'state.sqlite3';state.write_bytes(b'original-private-state')
        for extra in ('../state.sqlite3', 'config.json', 'water.sqlite3'):
            archive, manifest = self.archive(extra)
            with self.assertRaisesRegex(SetupError, 'unexpected'):
                unpack_bundle(archive, self.root/'bad-stage', manifest)
        archive, manifest = self.archive()
        archive.write_bytes(b'corrupted download')
        with self.assertRaisesRegex(SetupError, 'checksum'):
            unpack_bundle(archive, self.root/'bad-stage', manifest)
        self.assertEqual(b'original-private-state', state.read_bytes())

    def test_paths_and_manifest_are_checked_before_extracting(self):
        for name in ('../state.sqlite3', '/absolute', 'C:/absolute', 'folder\\file', 'a//b'):
            with self.assertRaises(SetupError):
                safe_child(self.root, name)
        archive, manifest = self.archive()
        manifest['files']['../state.sqlite3'] = manifest['files'].pop('water.sqlite3')
        path = self.root/'bad.json';path.write_text(json.dumps(manifest))
        with self.assertRaises(SetupError):
            load_manifest(path)
        for value in ([], {'version': 1, 'id': []}, {'version': 1, 'id': 'nz-test', 'files': []}):
            path.write_text(json.dumps(value))
            with self.assertRaises(SetupError):
                load_manifest(path)

    def test_source_symlink_cannot_escape_engine_directory(self):
        archive = self.root/'source.tgz'
        with tarfile.open(archive, 'w:gz') as source:
            entry = tarfile.TarInfo('routino-3.4.4/README.txt')
            entry.type = tarfile.SYMTYPE
            entry.linkname = '../../state.sqlite3'
            source.addfile(entry)
        with self.assertRaisesRegex(SetupError, 'escapes'):
            unpack_routino(archive, self.root/'native')

    @unittest.skipUnless(__import__('os').name == 'posix', 'Native Routino setup is Linux-only')
    def test_stock_style_internal_source_symlink_is_preserved(self):
        archive = self.root/'source.tgz'
        with tarfile.open(archive, 'w:gz') as source:
            entry = tarfile.TarInfo('routino-3.4.4/doc/README.txt');entry.size = 4
            source.addfile(entry, io.BytesIO(b'test'))
            link = tarfile.TarInfo('routino-3.4.4/README.txt')
            link.type = tarfile.SYMTYPE;link.linkname = 'doc/README.txt';source.addfile(link)
        tree = unpack_routino(archive, self.root/'native')
        self.assertTrue((tree/'README.txt').is_symlink())
        self.assertEqual(b'test', (tree/'README.txt').read_bytes())

    def test_download_retries_checks_hash_and_reuses_complete_cache(self):
        raw = b'prepared-data'
        class Response(io.BytesIO):
            def geturl(self):return 'https://example.invalid/data.zip'
        path = self.root/'cache/data.zip'
        with patch('room_bot.install.urlopen', side_effect=[OSError('network lost'), Response(raw)]), \
                patch('room_bot.install.time.sleep'), redirect_stdout(io.StringIO()):
            download('https://example.invalid/data.zip', path, record(raw))
        self.assertEqual(raw, path.read_bytes())
        self.assertFalse(path.with_name('data.zip.part').exists())
        with patch('room_bot.install.urlopen', side_effect=AssertionError('Redownloaded cache')), redirect_stdout(io.StringIO()):
            download('https://example.invalid/data.zip', path, record(raw))

    def test_failed_download_does_not_replace_previous_file(self):
        path = self.root/'cache.zip';path.write_bytes(b'previous')
        class Response(io.BytesIO):
            def geturl(self):return 'https://example.invalid/data.zip'
        with patch('room_bot.install.urlopen', return_value=Response(b'wrong')), redirect_stdout(io.StringIO()):
            with self.assertRaises(SetupError):
                download('https://example.invalid/data.zip', path, record(b'expected'), attempts=1)
        self.assertEqual(b'previous', path.read_bytes())

    def test_setup_preserves_config_state_community_and_can_repeat(self):
        import sys
        target = self.root/'installation with spaces'
        target.mkdir()
        config = target/'config.json';config.write_bytes(b'original-operator-config')
        state = target/'state.sqlite3';state.write_bytes(b'original-cursor-and-outbox')
        community = target/'community.sqlite3';community.write_bytes(b'original-community-records')
        args = SimpleNamespace(root=target, profile='basic', manifest=None, bundle_file=None,
                               bundle_url=None, jobs=1)
        with patch('room_bot.install.setup_python', return_value=Path(sys.executable)), \
                patch('room_bot.install.load_tide_manifest',return_value={'id':'nz-tides-fixture','archive':{'bytes':1},'files':{}}), \
                patch('room_bot.install.install_tides'), \
                redirect_stdout(io.StringIO()):
            install(args, SOURCE)
            install(args, SOURCE)
        self.assertEqual(b'original-operator-config', config.read_bytes())
        self.assertEqual(b'original-cursor-and-outbox', state.read_bytes())
        self.assertEqual(b'original-community-records', community.read_bytes())
        self.assertTrue((target/'run_bot.py').is_file())
        self.assertTrue((target/'installation.json').is_file())
        self.assertTrue(list(target.glob('config.*.proposed.json')))
        self.assertFalse((target/'.setup.lock').exists())
