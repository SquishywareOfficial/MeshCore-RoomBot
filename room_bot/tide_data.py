"""National LINZ tide tables in a separate indexed SQLite store.

Ambiguous DST times retain both possible instants. They are never silently
assigned a fold. Annual files can include five/six extrema or a next-year fragment.
"""
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import unicodedata
from zoneinfo import ZoneInfo

from .data import DataError

APPLICATION_ID = 0x53515444
CATALOGUE_URL = 'https://www.linz.govt.nz/api/v1/tp'
PREDICTIONS_URL = 'https://www.linz.govt.nz/products-services/tides-and-tidal-streams/tide-predictions'
CSV_BASE = 'https://static.charts.linz.govt.nz/tide-tables/maj-ports/csv/'
OFFSET_NAMES = {'bon accord harbour': 'Bon Accord Harbour - Mansion House Bay',
    'bowentown': 'Bowentown - Katikati Entrance', 'croisilles harbour kotiro point': 'Kōtiro Point',
    'fishing bay': 'Coal Island - Fishing Bay', 'flower pott onoua': 'Flower Pot (Onoua)',
    'kauri point': 'Katikati - Kauri Point', 'manawatawhi great island': 'Manawatāwhi / Great Island - North West Bay',
    'ocean bay': 'Te Whanganui / Port Underwood - Ocean Bay', 'omaio bay': 'Omaio Bay - Motunui Island',
    'papakura channel': 'Papakura Channel - LPG Terminal', 'sealers bay': 'Chalky Island - Sealers Bay',
    'snares island tini heke': 'Snares Islands / Tini Heke - North East Island',
    'whanganui island': 'Coromandel Harbour - Whanganui Island'}
HEADER_NAMES = {'Kaituna River Entrance': 'Kaituna River',
                'Rangitaiki River Entrance': 'Rangitaiki River',
                'Town Basin': 'Town Basin - Whangarei'}


def norm(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', value.casefold()) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'[a-z0-9]+', value))


def slug(value):
    return norm(value).replace(' ', '-')


def checked_text(value, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise DataError('Invalid tide source text')
    return value


def finite(value, low=-10, high=20):
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise DataError('Invalid tide number') from exc
    if not math.isfinite(n) or not low <= n <= high:
        raise DataError('Tide number outside supported bounds')
    return n


def minutes(value):
    value = value.strip().replace(' ', '')
    if value in ('', '-'):
        return None
    if not re.fullmatch(r'[+-]?\d{4}', value) or int(value[-2:]) >= 60:
        raise DataError('Invalid secondary-port time difference')
    sign = -1 if value.startswith('-') else 1
    return sign * (int(value.lstrip('+-')[:2]) * 60 + int(value[-2:]))


def optional_number(value):
    return None if value.strip() in ('', '-') else finite(value)


def secondary_rows(raw):
    if len(raw) > 200000:
        raise DataError('Secondary-port file too large')
    rows = csv.reader(io.StringIO(raw.decode('utf-8-sig')))
    reference = None
    found = {}
    for row in rows:
        if len(row) < 16 or not row[0] or not row[0][0].isdigit():
            continue
        name = checked_text(row[1])
        if row[6] == 'hhmm':
            reference = (name, finite(row[14]))
            continue
        if reference is None:
            raise DataError('Secondary port without standard-port group')
        west = 'W' in row[5].upper()
        lat = -(finite(row[2], 0, 90) + finite(row[3], 0, 60) / 60)
        lon = (finite(row[4], 0, 180) + finite(row[5].upper().replace('W', ''), 0, 60) / 60) * (-1 if west else 1)
        ratio = optional_number(row[15])
        if ratio is not None and not 0 < ratio <= 10:raise DataError('Invalid secondary height range ratio')
        found[norm(name)] = {'name': name, 'reference': reference[0], 'standard_msl': reference[1],
            'msl': optional_number(row[14]), 'ratio': ratio,
            'high_minutes': minutes(row[6]), 'low_minutes': minutes(row[8]),
            'high_range': row[7], 'low_range': row[9], 'approximate': '*' in row[16:],
            'latitude': lat, 'longitude': lon}
    if not 250 <= len(found) <= 500:
        raise DataError('Incomplete/unsupported national secondary-port table')
    return found


def catalogue_sites(raw, secondary):
    rows = json.loads(raw)
    if not isinstance(rows, list) or not 250 <= len(rows) <= 500:
        raise DataError('Incomplete/unsupported national tide catalogue')
    sites = []
    for row in rows:
        name = checked_text(row['name'])
        lat, lon = map(float, row['field_location'].split(','))
        finite(lat, -90, 90);finite(lon, -180, 180)
        kind = row['field_tide_type']
        if kind not in ('Daily Predictions', 'Offset'):
            raise DataError('Unsupported tide-site type')
        zone = 'Pacific/Chatham' if -180 < lon < -170 and -46 < lat < -40 else 'Pacific/Auckland'
        site = {'id': slug(name), 'name': name, 'latitude': lat, 'longitude': lon,
                'zone': zone, 'type': 'daily' if kind == 'Daily Predictions' else 'offset',
                'file': row['field_tide_prediction_file'] if kind == 'Daily Predictions' else None,
                'aliases': [name]}
        if name in HEADER_NAMES:site['aliases'].append(HEADER_NAMES[name])
        for separator in (' / ', ' - '):
            if separator in name:site['aliases'].extend(name.split(separator))
        if site['type'] == 'offset':
            table_name = OFFSET_NAMES.get(norm(name), name)
            offset = secondary.get(norm(table_name))
            if offset is None:
                raise DataError('Secondary-port name/reference mismatch: ' + name)
            if norm(offset['reference']) != norm(row['field_tide_reference_port']):
                # Reviewed source discrepancy: the current nautical table places
                # this same Ocean Bay under Wellington; the web map says Lyttelton.
                if norm(name) != 'ocean bay' or norm(row['field_tide_reference_port']) != 'lyttelton' or norm(offset['reference']) != 'wellington':
                    raise DataError('Secondary-port reference mismatch: ' + name)
                site['catalogue_reference'] = row['field_tide_reference_port']
            # Explicit name corrections must also agree geographically.
            if abs(offset['latitude'] - lat) > .12 or abs(((offset['longitude']-lon+180)%360)-180) > .12:
                # Reviewed web-map longitude typo; use the nautical-table point.
                if norm(name) != 'paparoa point' or abs(lon-174.0234) > .001 or abs(offset['longitude']-173.016667) > .001:
                    raise DataError('Secondary-port coordinates mismatch: ' + name)
                site['catalogue_coordinates'] = [lat,lon]
                site['latitude'],site['longitude'] = offset['latitude'],offset['longitude']
            site['offset'] = offset
            site['reference_id'] = slug(offset['reference'])
            site['aliases'].append(offset['name'])
        if ' - Chatham Island' in name:
            site['aliases'].append(name.split(' - ')[0])
        if name == 'Raglan':site['aliases'].append('Raglan Harbour')
        if name == 'Piha Beach':site['aliases'].append('Piha')
        sites.append(site)
    ids = {s['id'] for s in sites}
    if len(ids) != len(sites) or any(s['type']=='offset' and s['reference_id'] not in ids for s in sites):
        raise DataError('Duplicate tide site or missing standard port')
    return sites


def instants(day, clock_text, zone):
    try:
        naive = datetime.combine(day, datetime.strptime(clock_text, '%H:%M').time())
    except ValueError as exc:
        raise DataError('Invalid annual tide time') from exc
    choices = set()
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(zone).replace(tzinfo=None) == naive:
            choices.add(int(candidate.timestamp()))
    if not choices:
        raise DataError('Source time falls in nonexistent daylight-saving hour')
    return sorted(choices)


def parse_annual(raw, site, year):
    if not raw or len(raw) > 100000 or not 2020 <= year <= 2100:
        raise DataError('Unsupported annual prediction file')
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    if len(rows) < 365 or len(rows[0]) < 2 or norm(rows[0][1]) not in (norm(site['name']), norm(HEADER_NAMES.get(site['name'],site['name']))) or 'Local Std or Daylight Time' not in ','.join(rows[2]):
        raise DataError('Annual file identity/time convention mismatch')
    zone = ZoneInfo(site['zone'])
    days, events = [], []
    for row in rows[3:]:
        if not row:continue
        if len(row) < 6 or len(row) > 16 or len(row) % 2:
            raise DataError('Unsupported tide CSV row shape')
        try:day = date(int(row[3]), int(row[2]), int(row[0]))
        except (ValueError, IndexError) as exc:raise DataError('Invalid annual tide date') from exc
        if days and day != days[-1] + timedelta(days=1):
            raise DataError('Annual tide dates not contiguous')
        if day.year not in (year, year+1) or day.year == year+1 and day != date(year+1, 1, 1):
            raise DataError('Annual tide dates outside requested year')
        days.append(day)
        count = 0
        for column in range(4, len(row), 2):
            clock_text, height = row[column:column+2]
            if not clock_text and not height:continue
            if not clock_text or not height:raise DataError('Incomplete tide pair')
            events.append({'choices': instants(day, clock_text, zone), 'height': finite(height), 'day': day})
            count += 1
        minimum = 1 if site['name'] == 'Scott Base' or day.year != year else 2
        if not minimum <= count <= 6:
            raise DataError('Unsupported daily tide event count')
    if days[0] != date(year,1,1) or date(year,12,31) not in days:
        raise DataError('Annual table does not cover the full year')
    # Published rows are successive high/low waters. A significant first height
    # change anchors alternation; equal rounded heights do not remove events.
    anchor = next((i for i in range(len(events)-1) if events[i]['height'] != events[i+1]['height']), None)
    if anchor is None:raise DataError('Cannot establish high/low sequence')
    high_parity = anchor % 2 if events[anchor]['height'] > events[anchor+1]['height'] else 1-anchor%2
    for i, event in enumerate(events):
        event['event'] = 'high' if i % 2 == high_parity else 'low'
        if i+1 < len(events) and event['height'] != events[i+1]['height'] and (
                event['height'] > events[i+1]['height']) != (event['event']=='high'):
            raise DataError('Inconsistent tide height alternation')
    # Eliminate folds only when the source event order itself makes them impossible.
    for sequence in (range(len(events)), range(len(events)-1,-1,-1)):
        for i in sequence:
            choices = [t for t in events[i]['choices'] if
                       (i == 0 or min(events[i-1]['choices']) < t) and
                       (i == len(events)-1 or t < max(events[i+1]['choices']))]
            if not choices:raise DataError('Unordered tide instants')
            events[i]['choices'] = choices
    return [(i, min(e['choices']), max(e['choices']), e['event'], e['height'])
            for i,e in enumerate(events) if e['day'].year == year]


def create_store(path, sites, metadata):
    if Path(path).exists():raise DataError('Choose a new tide database path')
    db = sqlite3.connect(path)
    db.executescript(f'''PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=1;
        CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE sites (id TEXT PRIMARY KEY,name TEXT NOT NULL,body TEXT NOT NULL);
        CREATE TABLE sources (site TEXT,year INTEGER,body TEXT NOT NULL,PRIMARY KEY(site,year));
        CREATE TABLE events (site TEXT,year INTEGER,seq INTEGER,earliest INTEGER,latest INTEGER,event TEXT,height REAL,
                             PRIMARY KEY(site,year,seq));
        CREATE INDEX next_tide ON events(site,event,latest);''')
    with db:
        for key,value in metadata.items():db.execute('INSERT INTO metadata VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))
        for site in sites:db.execute('INSERT INTO sites VALUES (?,?,?)',(site['id'],site['name'],json.dumps(site,ensure_ascii=False)))
    return db


def import_annual(db, raw, site, year, url, retrieved):
    events = parse_annual(raw, site, year)
    source = {'url': url, 'retrieved_at': retrieved, 'sha256': hashlib.sha256(raw).hexdigest(),
              'bytes': len(raw), 'start': f'{year}-01-01', 'end': f'{year}-12-31',
              'ambiguous_events': sum(a != b for _,a,b,_,_ in events)}
    with db:
        db.execute('DELETE FROM events WHERE site=? AND year=?',(site['id'],year))
        db.executemany('INSERT INTO events VALUES (?,?,?,?,?,?,?)',
                      [(site['id'],year,*row) for row in events])
        db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?)',(site['id'],year,json.dumps(source)))
    return source


class TideStore:
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)
        if self.db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or self.db.execute('PRAGMA user_version').fetchone()[0] != 1:
            self.db.close();raise DataError('Unsupported tide database')
        self.metadata = {k:json.loads(v) for k,v in self.db.execute('SELECT * FROM metadata')}

    def close(self):self.db.close()

    def sites(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM sites ORDER BY name')]

    def site(self, identifier):
        row = self.db.execute('SELECT body FROM sites WHERE id=?',(identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def years(self, identifier):
        return {y:json.loads(b) for y,b in self.db.execute('SELECT year,body FROM sources WHERE site=? ORDER BY year',(identifier,))}

    def next(self, identifier, kind, timestamp):
        row = self.db.execute('SELECT earliest,latest,height FROM events WHERE site=? AND event=? AND latest>=? ORDER BY earliest LIMIT 1',
                              (identifier,kind,timestamp)).fetchone()
        return row


def check_store(path):
    store = TideStore(path)
    try:
        if store.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':raise DataError('Tide DB integrity check failed')
        sites = store.sites()
        if not 250 <= len(sites) <= 500:raise DataError('National tide catalogue incomplete')
        years = store.metadata.get('years')
        if not isinstance(years,list) or not years or any(type(y) is not int or not 2020<=y<=2100 for y in years):
            raise DataError('Unsupported tide coverage years')
        for s in sites:
            if s['type']=='daily' and set(store.years(s['id']))!=set(years):raise DataError('Daily port without complete annual coverage')
            if s['type']=='offset' and not store.site(s['reference_id']):raise DataError('Missing reference port')
        return {'sites':len(sites),'daily_sites':sum(s['type']=='daily' for s in sites),
                'events':store.db.execute('SELECT count(*) FROM events').fetchone()[0],
                'annual_files':store.db.execute('SELECT count(*) FROM sources').fetchone()[0],**store.metadata}
    finally:store.close()
