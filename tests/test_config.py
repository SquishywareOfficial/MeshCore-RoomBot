from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from room_bot.clock import ClockUnavailable, SessionClock
from room_bot.config import Config, ConfigError, Limits, load_config, write_new_config
from room_bot.__main__ import main, parse_options


class ConfigTests(unittest.TestCase):
    def test_template_roundtrip_and_existing_file_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            config = replace(Config(), default_location='Whangārei', data_dir='local-data')
            write_new_config(path, config)
            self.assertEqual(config, load_config(path))
            self.assertEqual(Path(temp) / 'local-data', load_config(path).data_path(path))
            previous = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_new_config(path)
            self.assertEqual(previous, path.read_bytes())

    def test_invalid_schema_commands_timezone_limits_and_control_text(self):
        for change in ({'version': 2}, {'version': True}, {'enabled_commands': ['shell']},
                       {'enabled_commands': ['ping', 'ping']}, {'enabled_commands': 'ping'},
                       {'enabled_commands': [False]}, {'timezone': 'Unknown/Nowhere'},
                       {'default_location': ''}, {'default_location': 'Hamilton\nreboot'},
                       {'data_dir': 5}, {'extra': True}, {'limits': []},
                       {'limits': {'query_results': True}}, {'limits': {'query_results': 1001}},
                       {'limits': {'unknown': 1}}, {'clock_max_jump_seconds': float('inf')},
                       {'limits': {'query_results': 10 ** 1000}},
                       {'community': {'notice_days': True}}, {'community': {'checkin_days': -1}},
                       {'aliases': {'User': 'short'}},
                       {'aliases': {'User':'a'*64,'user':'b'*64}},
                       {'default_location':'bad\ud800'}):
            with self.subTest(change=change), self.assertRaises(ConfigError):
                Config.from_dict(change)

    def test_bad_files_fail_instead_of_silently_using_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bad.json'
            for raw in (b'{', b'\xff', b'[]', b'{"version":1,"version":2}',
                        b'{"clock_max_jump_seconds":NaN}', b' ' * 65537):
                path.write_bytes(raw)
                with self.subTest(raw=raw[:30]), self.assertRaises(ConfigError):
                    load_config(path)
            with self.assertRaises(ConfigError):
                load_config(Path(temp) / 'missing')

    def test_default_cli_paths_and_explicit_state_are_compatible(self):
        options = parse_options(['--port', 'COM26', '--state', 'existing.sqlite3', '--handler', 'custom:reply'])
        self.assertEqual(Path('existing.sqlite3'), options.state)
        self.assertEqual('custom:reply', options.handler)
        self.assertEqual('monitor', options.mode)
        self.assertEqual(('ping', 'help', 'about'), options.config_value.enabled_commands)
        self.assertFalse(options.clock_trusted)
        self.assertEqual(Path.home() / '.meshcore-room-bot/data', options.data_path)

    def test_config_only_actions_never_open_serial_or_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            with patch('room_bot.__main__.SerialTransport') as serial, patch('room_bot.__main__.BotState') as state:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(0, main(['--init-config', str(path)]))
                    self.assertEqual(0, main(['--config', str(path), '--check-config']))
                    self.assertEqual(1, main(['--init-config', str(path)]))
                serial.assert_not_called()
                state.assert_not_called()

    def test_invalid_configuration_rejected_before_usb_access(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            path.write_text('{"version":9}')
            with patch('room_bot.__main__.SerialTransport') as serial, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(['--port', 'COM26', '--config', str(path)])
                serial.assert_not_called()
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_options([])


class ClockTests(unittest.TestCase):
    def clock(self, trusted):
        self.wall = 1791800000.0
        self.mono = 100.0
        return SessionClock(trusted, wall=lambda: self.wall, monotonic=lambda: self.mono)

    def test_trust_is_not_inferred_from_plausible_date_or_saved_config(self):
        clock = self.clock(False)
        with self.assertRaises(ClockUnavailable):
            clock.now()
        self.assertNotIn('trusted', Config().to_dict())
        options = parse_options(['--port', 'COM26', '--clock-trusted'])
        self.assertTrue(options.clock_trusted)
        self.assertFalse(parse_options(['--port', 'COM26']).clock_trusted)

    def test_elapsed_time_and_nz_dst(self):
        from zoneinfo import ZoneInfo
        clock = self.clock(True)
        self.wall += 3600
        self.mono += 3600
        self.assertEqual(self.wall, clock.now().timestamp())
        nz = ZoneInfo(Config().timezone)
        self.assertEqual(13 * 3600, datetime(2026, 1, 1, tzinfo=timezone.utc).astimezone(nz).utcoffset().total_seconds())
        self.assertEqual(12 * 3600, datetime(2026, 7, 1, tzinfo=timezone.utc).astimezone(nz).utcoffset().total_seconds())

    def test_clock_jumps_revoke_trust_permanently_until_a_new_session(self):
        clock = self.clock(True)
        initial = self.wall
        self.wall -= 100
        with self.assertRaises(ClockUnavailable):
            clock.now()
        self.wall = initial
        with self.assertRaises(ClockUnavailable):
            clock.now()
        self.assertEqual(initial, self.clock(True).now().timestamp())

    def test_invalid_clock_values_do_not_create_timestamps(self):
        clock = self.clock(True)
        self.wall = float('nan')
        with self.assertRaises(ClockUnavailable):
            clock.now()
