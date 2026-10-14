"""Weather integration checks: routing names, usable output, deadlines and outage honesty."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from room_bot.clock import SessionClock
from room_bot.commands import Dispatcher
from room_bot.config import Config, ConfigError, Limits, WeatherSettings
from room_bot.weather import (Cache, CURRENT, DAILY, UNITS, WeatherError, WeatherService,
                              fetch_json, install_weather_commands, select_place)
from test_commands import post


NOW = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc).timestamp()


def place(name='Hamilton', lat=-37.78333, lon=175.28333, region='Waikato'):
    return {'id': 1, 'name': name, 'latitude': lat, 'longitude': lon, 'admin1': region,
            'timezone': 'Pacific/Auckland', 'country_code': 'NZ', 'feature_code': 'PPL'}


def response(now=NOW, p=None):
    p = p or place()
    zone = ZoneInfo(p['timezone'])
    today = datetime.fromtimestamp(now, zone).date()
    days = [datetime.combine(today + timedelta(days=i), datetime.min.time(), zone).timestamp() for i in range(4)]
    return {'latitude': p['latitude'], 'longitude': p['longitude'], 'timezone': p['timezone'],
            'current_units': {'time': 'unixtime', **{k: UNITS[k] for k in CURRENT}},
            'daily_units': {'time': 'unixtime', **{k: UNITS[k] for k in DAILY}},
            'current': {'time': now - 300, 'interval': 900, 'temperature_2m': 17.8,
                        'apparent_temperature': 16.2, 'relative_humidity_2m': 68, 'weather_code': 3,
                        'wind_speed_10m': 12, 'wind_direction_10m': 270, 'wind_gusts_10m': 21,
                        'precipitation': 0.2},
            'daily': {'time': days, 'weather_code': [61, 3, 0, 2], 'temperature_2m_max': [20, 19, 22, 21],
                      'temperature_2m_min': [10, 9, 11, 10], 'precipitation_probability_max': [80, 20, 0, 10],
                      'precipitation_sum': [4.2, 0.0, 0.0, 0.1], 'wind_speed_10m_max': [25, 20, 15, 19]}}


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = NOW
        self.calls = []
        self.config = replace(Config(), enabled_commands=('weather', 'forecast', 'ping', 'help'),
                              limits=Limits(request_cooldown_seconds=0), weather=WeatherSettings(enabled=True))

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, endpoint, parameters, timeout):
        self.calls.append((endpoint, parameters))
        if endpoint == 'geocode':
            return {'results': [place(parameters['name'].split(',')[0].strip())]}
        return response(self.now, place(lat=parameters['latitude'], lon=parameters['longitude']))

    def dispatch(self, config=None, fetch=None, trusted=True):
        clock = SessionClock(trusted, wall=lambda: self.now, monotonic=lambda: self.now)
        d = Dispatcher(config or self.config, '[SquishyBot] ', clock)
        install_weather_commands(d, self.root, fetch or self.fetch)
        return d

    def check_output(self, replies):
        self.assertLessEqual(len(replies), 4)
        self.assertTrue(all(len(('[SquishyBot] ' + r).encode()) <= 151 for r in replies))
        self.assertNotIn('https://', ''.join(replies))

    def test_default_current_has_readable_time_units_and_no_source_footer(self):
        replies = self.dispatch()(post('/bot weather'))
        text = ''.join(replies)
        self.assertIn('Hamilton: overcast; 17.8C', text)
        self.assertIn('Model 14 Oct 13:55 NZDT; Open-Meteo.', text)
        self.assertIn('Wind W 12km/h, gust 21km/h', text)
        self.assertIn('rain 0.2mm/15min', text)
        self.assertEqual(2, len(replies))
        self.assertEqual('Hamilton', self.calls[0][1]['name'])
        self.assertEqual('NZ', self.calls[0][1]['countryCode'])
        self.check_output(replies)

    def test_forecast_three_local_days_is_compact_and_cache_shared_with_weather(self):
        d = self.dispatch()
        d(post('/bot weather'))
        replies = d(post('/bot forecast', 2))
        text = ''.join(replies)
        self.assertIn('Hamilton forecast; Open-Meteo.', text)
        self.assertIn('Wed 14 Oct: light rain; 10..20C; rain 80% 4.2mm', text)
        self.assertIn('Thu 15 Oct', text)
        self.assertIn('Fri 16 Oct', text)
        self.assertNotIn('Sat 17 Oct', text)
        self.assertEqual(2, len(self.calls))
        self.check_output(replies)

    def test_location_override_never_changes_shared_default_and_multiword_kept(self):
        d = self.dispatch()
        self.assertIn('Raglan:', ''.join(d(post('/bot weather Raglan'))))
        self.assertIn('Hamilton forecast', ''.join(d(post('/bot forecast', 2))))
        d(post('/bot weather Mount Maunganui', 3))
        queries = [p['name'] for e, p in self.calls if e == 'geocode']
        self.assertEqual(['Raglan', 'Hamilton', 'Mount Maunganui'], queries)

    def test_exact_name_does_not_select_first_prefix_or_another_country(self):
        other = place('Hamilton Bay')
        other['feature_code'] = 'PPL'
        selected = select_place({'results': [other, place()]}, 'Hamilton')
        self.assertEqual('Hamilton', selected['name'])
        with self.assertRaisesRegex(WeatherError, 'No exact'):
            select_place({'results': [other]}, 'Hamilton')
        foreign = place();foreign['country_code'] = 'CA'
        with self.assertRaises(WeatherError):
            select_place({'results': [foreign]}, 'Hamilton')

    def test_ambiguous_names_require_region_and_never_get_weather_for_guessed_place(self):
        rows = [place('Springfield', region='Canterbury'), place('Springfield', region='Bay of Plenty')]
        with self.assertRaisesRegex(WeatherError, 'Which location'):
            select_place({'results': rows}, 'Springfield')
        self.assertEqual('Canterbury', select_place({'results': rows}, 'Springfield, Canterbury')['admin1'])
        d = self.dispatch(fetch=lambda *args: {'results': rows})
        self.assertIn('Which location', ''.join(d(post('/bot weather Springfield'))))

    def test_missing_place_and_alert_help_do_not_turn_into_hamilton_forecast(self):
        d = self.dispatch(fetch=lambda *args: {})
        self.assertIn('town not found', ''.join(d(post('/bot weather Imaginary'))))
        self.assertIn('not implemented', ''.join(d(post('/bot weather emergency', 2))))
        self.assertIn('Usage:', ''.join(d(post('/bot forecast help', 3))))

    def test_disabled_and_unknown_clock_make_no_network_requests(self):
        fail = lambda *args: self.fail('Network must not be called')
        disabled = replace(self.config, weather=WeatherSettings())
        self.assertIn('disabled', ''.join(self.dispatch(disabled, fail)(post('/bot weather'))))
        self.assertIn('checked host clock', ''.join(self.dispatch(fetch=fail, trusted=False)(post('/bot forecast'))))
        unregistered = replace(self.config, enabled_commands=('ping',))
        self.assertIn('disabled', ''.join(self.dispatch(unregistered, fail)(post('/bot weather'))))

    def test_persisted_cache_survives_restart_then_outage_is_explicit(self):
        self.dispatch()(post('/bot weather'))
        self.now += 1800
        def fail(*args):raise WeatherError('offline')
        d = self.dispatch(fetch=fail)
        replies = d(post('/bot weather', 2))
        self.assertIn('STALE cached 30m; refresh failed', ''.join(replies))
        self.assertIn('17.8C', ''.join(replies))
        self.check_output(replies)
        self.now += 8000
        self.assertIn('no recent saved result', ''.join(self.dispatch(fetch=fail)(post('/bot weather', 3))))

    def test_failed_or_malformed_refresh_never_replaces_good_snapshot(self):
        self.dispatch()(post('/bot weather'))
        db = sqlite3.connect(self.root/'weather.sqlite3')
        previous = db.execute("SELECT value FROM weather_cache WHERE key LIKE 'weather:%'").fetchone()[0]
        self.now += 900
        for malformed in ({}, {'error': True}, response(self.now)):
            if 'current_units' in malformed:malformed['current_units']['temperature_2m'] = '°F'
            replies = self.dispatch(fetch=lambda *args, value=malformed: value)(post('/bot weather', 2))
            self.assertIn('STALE', ''.join(replies))
            self.assertEqual(previous, db.execute("SELECT value FROM weather_cache WHERE key LIKE 'weather:%'").fetchone()[0])
        db.close()

    def test_missing_values_are_na_not_zero_unknown_codes_not_clear(self):
        r = response();r['current']['wind_gusts_10m'] = None;r['current']['weather_code'] = 777
        def fetch(e, p, t):return {'results': [place()]} if e == 'geocode' else r
        replies = self.dispatch(fetch=fetch)(post('/bot weather'))
        self.assertIn('conditions unavailable', ''.join(replies))
        self.assertIn('gust n/a', ''.join(replies))
        self.check_output(replies)

    def test_nan_dates_units_arrays_and_inverted_ranges_rejected(self):
        mutations = [lambda r: r['current'].update(temperature_2m=float('nan')),
                     lambda r: r['current'].update(time=NOW+90000),
                     lambda r: r['daily']['temperature_2m_max'].pop(),
                     lambda r: r['daily']['temperature_2m_min'].__setitem__(0, 25),
                     lambda r: r['daily']['time'].__setitem__(1, r['daily']['time'][0]),
                     lambda r: r.update(timezone='UTC'),
                     lambda r: r.update(latitude=40)]
        for i, mutate in enumerate(mutations):
            with tempfile.TemporaryDirectory() as folder:
                r = response();mutate(r)
                def fetch(e, p, t):return {'results': [place()]} if e == 'geocode' else r
                ctx = SimpleNamespace(clock=SessionClock(True, wall=lambda: NOW), config=self.config)
                self.assertIn('no recent saved result', ''.join(WeatherService(Path(folder), self.config.weather, fetch).reply(ctx, [], 'weather')))

    def test_midnight_rollover_skips_yesterday_and_chatham_timezone_works(self):
        self.now = datetime(2026, 10, 14, 10, 50, tzinfo=timezone.utc).timestamp()  # 23:50 NZDT
        self.dispatch()(post('/bot forecast'))
        self.now += 1200
        def fail(*args):raise WeatherError('offline')
        result = ''.join(self.dispatch(fetch=fail)(post('/bot forecast', 2)))
        self.assertNotIn('Wed 14 Oct:', result)
        self.assertIn('Thu 15 Oct:', result)
        self.assertIn('Sat 17 Oct:', result)
        p = place('Waitangi', lat=-43.95, lon=-176.56);p['timezone'] = 'Pacific/Chatham'
        r = response(self.now, p)
        def fetch(e, params, timeout):return {'results': [p]} if e == 'geocode' else r
        self.assertIn('+1345', ''.join(self.dispatch(fetch=fetch)(post('/bot weather Waitangi', 3))))

    def test_cache_limit_future_rows_and_provider_backoff_are_bounded(self):
        cache = Cache(self.root/'test.sqlite3', 4)
        for i in range(10):cache.put(str(i), NOW+i, {'x': i})
        self.assertEqual(4, cache.db.execute('SELECT count(*) FROM weather_cache').fetchone()[0])
        self.assertIsNone(cache.get('9', NOW, 100))
        cache.close()
        count = []
        def fail(*args):count.append(1);raise WeatherError('offline')
        svc = WeatherService(self.root, self.config.weather, fail, monotonic=lambda: 0)
        for _ in range(2):
            with self.assertRaises(WeatherError):svc.request('geocode', {}, 'same')
        self.assertEqual(1, len(count))

    def test_http_helper_has_hard_deadline_and_discards_oversized_results(self):
        with patch('room_bot.weather.subprocess.run', side_effect=subprocess.TimeoutExpired('helper', 6)) as run:
            with self.assertRaises(WeatherError):fetch_json('geocode', {}, 5)
            self.assertEqual(6, run.call_args.kwargs['timeout'])
        with patch('room_bot.weather.subprocess.run', return_value=SimpleNamespace(stdout=b' ' * 131073)):
            with self.assertRaises(WeatherError):fetch_json('geocode', {}, 5)

    def test_weather_settings_validation_and_old_configuration_compatibility(self):
        self.assertFalse(Config.from_dict({'version': 1}).weather.enabled)
        self.assertTrue(Config.from_dict(self.config.to_dict()).weather.enabled)
        for options in ({'enabled': 'yes'}, {'timeout_seconds': 0}, {'cache_entries': 10000}, {'unknown': True}):
            with self.assertRaises((ConfigError, TypeError)):
                Config.from_dict({'weather': options})


if __name__ == '__main__':
    unittest.main()
