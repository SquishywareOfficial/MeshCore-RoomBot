"""On-demand NZ weather/forecast lookup, durable cache and short room responses."""
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unicodedata
from zoneinfo import ZoneInfo

from .clock import ClockUnavailable

MAX_BYTES = 131072
CONDITIONS = {0: 'clear', 1: 'mainly clear', 2: 'partly cloudy', 3: 'overcast',
              45: 'fog', 48: 'freezing fog', 51: 'light drizzle', 53: 'drizzle',
              55: 'heavy drizzle', 56: 'freezing drizzle', 57: 'freezing drizzle',
              61: 'light rain', 63: 'rain', 65: 'heavy rain', 66: 'freezing rain',
              67: 'heavy freezing rain', 71: 'light snow', 73: 'snow', 75: 'heavy snow',
              77: 'snow grains', 80: 'light showers', 81: 'showers', 82: 'heavy showers',
              85: 'snow showers', 86: 'heavy snow showers', 95: 'thunderstorms',
              96: 'thunderstorms/hail', 97: 'heavy thunderstorms', 99: 'thunderstorms/heavy hail'}
CURRENT = ('temperature_2m', 'apparent_temperature', 'relative_humidity_2m', 'weather_code',
           'wind_speed_10m', 'wind_direction_10m', 'wind_gusts_10m', 'precipitation')
DAILY = ('weather_code', 'temperature_2m_max', 'temperature_2m_min',
         'precipitation_probability_max', 'precipitation_sum', 'wind_speed_10m_max')
UNITS = {'temperature_2m': '°C', 'apparent_temperature': '°C', 'relative_humidity_2m': '%',
         'weather_code': 'wmo code', 'wind_speed_10m': 'km/h', 'wind_direction_10m': '°',
         'wind_gusts_10m': 'km/h', 'precipitation': 'mm', 'temperature_2m_max': '°C',
         'temperature_2m_min': '°C', 'precipitation_probability_max': '%',
         'precipitation_sum': 'mm', 'wind_speed_10m_max': 'km/h'}


class WeatherError(ValueError):
    pass


def normalized(text):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                            if not unicodedata.combining(c)).split())


def text(value, limit=80):
    if (not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise WeatherError('Invalid provider location')
    return value


def number(value, low, high, optional=False):
    if value is None and optional:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise WeatherError('Invalid provider value')
    return value


def condition(value):
    return CONDITIONS.get(value, 'conditions unavailable')


def fetch_json(endpoint, parameters, timeout):
    """Real hard deadline includes DNS/connect/read; a timeout kills the helper."""
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('weather_http.py'))],
            input=json.dumps({'endpoint': endpoint, 'parameters': parameters, 'timeout': timeout}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 1, check=True,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}))
        if len(result.stdout) > MAX_BYTES:
            raise WeatherError('Weather response exceeds limit')
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get('error'):
            raise WeatherError('Weather provider returned an error')
        return value
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise WeatherError('Weather service unavailable') from exc


def place_record(row):
    if not isinstance(row, dict) or row.get('country_code') != 'NZ':
        raise WeatherError('Not an NZ location')
    name = text(row.get('name'), 60)
    region = text(row.get('admin1', 'New Zealand'), 60)
    lat = number(row.get('latitude'), -90, 90)
    lon = number(row.get('longitude'), -180, 180)
    tz = text(row.get('timezone'), 60)
    if tz not in ('Pacific/Auckland', 'Pacific/Chatham'):
        raise WeatherError('Unsupported NZ timezone')
    return {'name': name, 'admin1': region, 'latitude': lat, 'longitude': lon,
            'timezone': tz, 'country_code': 'NZ'}


def select_place(response, query):
    rows = response.get('results', [])
    if not isinstance(rows, list) or len(rows) > 20:
        raise WeatherError('Invalid location response')
    term, sep, region = query.partition(',')
    matches = []
    suggestions = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get('feature_code', '')).startswith('P'):
            continue
        p = place_record(row)
        suggestions.append(f'{p["name"]}, {p["admin1"]}')
        if normalized(p['name']) != normalized(term):
            continue
        if sep and normalized(region) not in ('nz', 'new zealand', normalized(p['admin1'])):
            continue
        if p not in matches:
            matches.append(p)
    if len(matches) == 1:
        return matches[0]
    if matches:
        choices = list(dict.fromkeys(f'{p["name"]}, {p["admin1"]}' for p in matches))[:3]
        raise WeatherError('Which location? Try ' + ' or '.join(choices) + '.')
    if suggestions:
        raise WeatherError('No exact NZ town match. Try ' + ' or '.join(dict.fromkeys(suggestions[:3])) + '.')
    raise WeatherError('NZ town not found. Try its full name, optionally followed by , REGION.')


def checked_value(name, value):
    bounds = (-100, 65) if name.startswith(('temperature', 'apparent')) else (0, 10000)
    if name in ('relative_humidity_2m', 'precipitation_probability_max'):
        bounds = (0, 100)
    elif name == 'wind_direction_10m':
        bounds = (0, 360)
    elif name == 'weather_code':
        if value is not None and type(value) is not int:
            raise WeatherError('Invalid weather code')
        bounds = (0, 999)
    return number(value, *bounds, optional=True)


def snapshot(response, place, fetched):
    """Validate units/shape before atomically replacing the last good result."""
    current, daily = response.get('current'), response.get('daily')
    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise WeatherError('Incomplete weather response')
    for section, names in (('current', CURRENT), ('daily', DAILY)):
        units = response.get(section + '_units')
        if not isinstance(units, dict) or units.get('time') != 'unixtime':
            raise WeatherError('Unsupported provider time units')
        if any(units.get(name) != UNITS[name] for name in names):
            raise WeatherError('Unsupported provider units')
    if response.get('timezone') != place['timezone']:
        raise WeatherError('Provider timezone mismatch')
    # Open-Meteo snaps to a model cell; reject an unrelated returned location.
    if abs(number(response.get('latitude'), -90, 90) - place['latitude']) > 1 or abs(
            ((number(response.get('longitude'), -180, 180) - place['longitude'] + 180) % 360) - 180) > 1:
        raise WeatherError('Provider returned another location')
    valid = number(current.get('time'), fetched - 7200, fetched + 900)
    interval = number(current.get('interval'), 1, 3600)
    values = {name: checked_value(name, current.get(name)) for name in CURRENT}
    if not any(values[name] is not None for name in ('temperature_2m', 'wind_speed_10m', 'precipitation')):
        raise WeatherError('Current conditions unavailable')
    times = daily.get('time')
    if not isinstance(times, list) or not 3 <= len(times) <= 4:
        raise WeatherError('Incomplete daily forecast')
    if any(not isinstance(daily.get(name), list) or len(daily[name]) != len(times) for name in DAILY):
        raise WeatherError('Mismatched daily forecast arrays')
    days = []
    tz = ZoneInfo(place['timezone'])
    today = datetime.fromtimestamp(fetched, tz).date()
    for index, timestamp in enumerate(times):
        date = datetime.fromtimestamp(number(timestamp, fetched - 86400, fetched + 5 * 86400), tz).date()
        if date != today + timedelta(days=index):
            raise WeatherError('Forecast dates are not consecutive local days')
        values_day = {name: checked_value(name, daily[name][index]) for name in DAILY}
        lo, hi = values_day['temperature_2m_min'], values_day['temperature_2m_max']
        if lo is not None and hi is not None and lo > hi:
            raise WeatherError('Invalid forecast temperature range')
        days.append({'date': date.isoformat(), **values_day})
    return {'version': 1, 'place': place, 'fetched': fetched, 'valid': valid,
            'interval': interval, 'current': values, 'days': days}


class Cache:
    def __init__(self, path, limit):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2)
        self.limit = limit
        self.db.execute('CREATE TABLE IF NOT EXISTS weather_cache (key TEXT PRIMARY KEY, saved REAL NOT NULL, value TEXT NOT NULL)')

    def get(self, key, now, age):
        row = self.db.execute('SELECT saved,value FROM weather_cache WHERE key=?', (key,)).fetchone()
        if row is None or not 0 <= now - row[0] <= age or len(row[1].encode()) > MAX_BYTES:
            return None
        try:
            value = json.loads(row[1])
            return value if isinstance(value, dict) else None
        except ValueError:
            return None

    def put(self, key, now, value):
        raw = json.dumps(value, allow_nan=False, ensure_ascii=False)
        if len(raw.encode()) > MAX_BYTES:
            raise WeatherError('Cache value too large')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO weather_cache VALUES (?,?,?)', (key, now, raw))
            self.db.execute('DELETE FROM weather_cache WHERE key NOT IN (SELECT key FROM weather_cache ORDER BY saved DESC,key LIMIT ?)', (self.limit,))

    def close(self):
        self.db.close()


def show(value, unit='', decimals=0):
    return 'n/a' if value is None else f'{value:.{decimals}f}{unit}'


def wind(direction):
    return 'n/a' if direction is None else ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW')[int((direction + 22.5) // 45) % 8]


def format_reply(data, verb, now, stale):
    p = data['place']
    tz = ZoneInfo(p['timezone'])
    local = datetime.fromtimestamp(data['valid'] if verb == 'weather' else data['fetched'], tz)
    # Exact-name resolution happens before formatting; no metadata footer is needed.
    name = p['name']
    age = int((now - data['fetched']) / 60)
    status = f'STALE cached {age}m; refresh failed. ' if stale else ''
    when = local.strftime('%d %b %H:%M %Z')
    if verb == 'weather':
        c = data['current']
        return [f'{name}: {condition(c["weather_code"])}; {show(c["temperature_2m"], "C", 1)}. {status}Model {when}; Open-Meteo.',
                f'Feels {show(c["apparent_temperature"], "C", 1)}, humidity {show(c["relative_humidity_2m"], "%")}. '
                f'Wind {wind(c["wind_direction_10m"])} {show(c["wind_speed_10m"], "km/h")}, gust {show(c["wind_gusts_10m"], "km/h")}; '
                f'rain {show(c["precipitation"], "mm", 1)}/{int(data["interval"] / 60)}min.']
    today = datetime.fromtimestamp(now, tz).date().isoformat()
    days = [d for d in data['days'] if d['date'] >= today][:3]
    if not days:
        raise WeatherError('Saved forecast has expired')
    header = f'{name} forecast; Open-Meteo. {status}Fetched {when}.'
    lines = [f'{datetime.fromisoformat(d["date"]).strftime("%a %d %b")}: {condition(d["weather_code"])}; '
             f'{show(d["temperature_2m_min"], "", 0)}..{show(d["temperature_2m_max"], "C")}; '
             f'rain {show(d["precipitation_probability_max"], "%")} {show(d["precipitation_sum"], "mm", 1)}; '
             f'wind max {show(d["wind_speed_10m_max"], "km/h")}. ' for d in days]
    return [header + '\n' + '\n'.join(lines)]


class WeatherService:
    def __init__(self, data_path, settings, fetch=fetch_json, monotonic=time.monotonic):
        self.path, self.settings, self.fetch, self.monotonic = Path(data_path), settings, fetch, monotonic
        self.failures = OrderedDict()

    def request(self, endpoint, parameters, cache_key):
        previous = self.failures.get(cache_key)
        if previous is not None and self.monotonic() - previous < 60:
            raise WeatherError('Weather service temporarily unavailable')
        try:
            return self.fetch(endpoint, parameters, self.settings.timeout_seconds)
        except (WeatherError, OSError, ValueError) as exc:
            self.failures[cache_key] = self.monotonic()
            self.failures.move_to_end(cache_key)
            while len(self.failures) > self.settings.cache_entries:
                self.failures.popitem(last=False)
            raise WeatherError('Weather service unavailable') from exc

    def reply(self, ctx, args, verb):
        if args and args[0].casefold() in ('help', 'emergency'):
            return ('Weather alerts are not implemented yet.' if args[0].casefold() == 'emergency' else
                    f'Usage: /bot {verb} [NZ town, optional region]. Default: {ctx.config.default_location}.')
        query = ' '.join(args) if args else ctx.config.default_location
        if len(query.encode('utf-8')) > 80 or len(query) < 2 or query.count(',') > 1:
            return f'Usage: /bot {verb} [NZ town, optional region].'
        if not self.settings.enabled:
            return 'Online weather is disabled. Ask the operator to enable weather in the bot config.'
        try:
            now = ctx.clock.now().timestamp()
        except ClockUnavailable:
            return 'Weather needs a checked host clock. Operator: restart with --clock-trusted after checking the date.'
        cache = None
        try:
            cache = Cache(self.path / 'weather.sqlite3', self.settings.cache_entries)
            location_key = 'place:' + normalized(query)
            p = cache.get(location_key, now, 30 * 86400)
            if p is not None:
                p = place_record(p)
            else:
                response = self.request('geocode', {'name': query, 'count': 20, 'language': 'en', 'countryCode': 'NZ'}, location_key)
                p = select_place(response, query)
                cache.put(location_key, now, p)
            weather_key = f'weather:{p["latitude"]:.6f},{p["longitude"]:.6f}:{p["timezone"]}'
            maximum = self.settings.stale_weather_seconds if verb == 'weather' else self.settings.stale_forecast_seconds
            data = cache.get(weather_key, now, maximum)
            stale = False
            if data is not None and (data.get('version') != 1 or data.get('place') != p or
                    not isinstance(data.get('fetched'), (int, float)) or not 0 <= now - data['fetched'] <= maximum):
                data = None
            current_usable = data is not None and (verb != 'weather' or 0 <= now - data['valid'] <= maximum)
            if not current_usable or now - data['fetched'] > self.settings.cache_seconds:
                try:
                    response = self.request('forecast', {'latitude': p['latitude'], 'longitude': p['longitude'],
                        'current': ','.join(CURRENT), 'daily': ','.join(DAILY), 'forecast_days': 4,
                        'timezone': p['timezone'], 'timeformat': 'unixtime', 'temperature_unit': 'celsius',
                        'wind_speed_unit': 'kmh', 'precipitation_unit': 'mm'}, weather_key)
                    data = snapshot(response, p, now)
                    cache.put(weather_key, now, data)
                except WeatherError:
                    # Invalid provider data also gets backoff; keep the last good snapshot.
                    self.failures[weather_key] = self.monotonic()
                    self.failures.move_to_end(weather_key)
                    while len(self.failures) > self.settings.cache_entries:
                        self.failures.popitem(last=False)
                    if not current_usable:
                        return 'Weather service unavailable; no recent saved result for that location. Try again later.'
                    stale = True
            return format_reply(data, verb, now, stale)
        except WeatherError as exc:
            return str(exc)
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
            logging.error('Weather data/cache unavailable')
            return 'Weather data unavailable; try again later.'
        finally:
            if cache is not None:
                cache.close()


def install_weather_commands(dispatcher, data_path, fetch=fetch_json):
    service = WeatherService(data_path, dispatcher.config.weather, fetch=fetch)
    for verb in ('weather', 'forecast'):
        dispatcher.register(verb, f'/bot {verb} [LOCATION]',
                            lambda ctx, args, verb=verb: service.reply(ctx, args, verb))
    return service
