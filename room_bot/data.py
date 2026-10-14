"""Local, versioned content imports and bounded lookups; no network operations."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
import unicodedata
from urllib.parse import urlparse

from .config import ConfigError, bounded, checked_object, load_config, unique_keys


MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_IMPORT_RECORDS = 20000
MAX_STORED_RECORDS = 100000
KINDS = ('directory', 'emergency', 'guide', 'places', 'routes', 'tides')


class DataError(ValueError):
    pass


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold()
                   if not unicodedata.combining(c))


def text(value, field, limit=200, multiline=False):
    allowed = '\n\r\t' if multiline else ''
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any((ord(c) < 32 and c not in allowed) or ord(c) == 127 for c in value)):
        raise DataError(f'{field} must be nonempty text (max {limit}) without invalid controls')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise DataError(f'{field} contains invalid Unicode') from exc
    return value


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - set(value) or set(value) - set(required) - set(optional):
        raise DataError(f'Expected fields: {", ".join(required)}; optional: {", ".join(optional)}')
    return value


def number(value, field, low, high):
    try:
        return bounded(value, field, low, high, integer=False)
    except ConfigError as exc:
        raise DataError(str(exc)) from exc


def timestamp(value, field):
    text(value, field, 50)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('Timezone is required')
    except ValueError as exc:
        raise DataError(f'{field} must be an ISO timestamp with an explicit UTC offset') from exc
    return value


def string_list(value, field, max_items=32):
    if not isinstance(value, list) or len(value) > max_items:
        raise DataError(f'{field} must be a list of at most {max_items} strings')
    for item in value:
        text(item, field)


def validate_record(kind, record):
    if kind == 'directory':
        fields(record, ('id', 'title', 'location', 'text'), ('aliases',))
    elif kind in ('emergency', 'guide'):
        fields(record, ('id', 'title', 'topics', 'text'))
    elif kind == 'places':
        fields(record, ('id', 'name', 'region', 'feature_type', 'latitude', 'longitude', 'aliases'))
    elif kind == 'routes':
        fields(record, ('id', 'origin', 'destination', 'mode', 'distance_m',
                        'origin_label', 'destination_label', 'profile', 'summary'), ('waypoints',))
        if record['mode'] not in ('walk', 'road'):
            raise DataError('Stored routes must specify walk or road')
        number(record['distance_m'], 'distance_m', 0, 10000000)
    elif kind == 'tides':
        fields(record, ('id', 'place_name', 'time', 'event', 'height_m', 'datum'))
        if record['event'] not in ('high', 'low'):
            raise DataError('Tide event must be high or low')
        timestamp(record['time'], 'time')
        number(record['height_m'], 'height_m', -20, 30)
    else:
        raise DataError('Unsupported dataset kind')
    for name, value in record.items():
        if name in ('aliases', 'topics', 'waypoints'):
            string_list(value, name)
        elif name == 'latitude':
            number(value, name, -90, 90)
        elif name == 'longitude':
            number(value, name, -180, 180)
        elif name not in ('height_m', 'distance_m'):
            text(value, name, 8192 if name == 'text' else 500,
                 multiline=name == 'text')
    return dict(record)


def validate_bundle(bundle):
    fields(bundle, ('format_version', 'dataset', 'records'))
    if type(bundle['format_version']) is not int or bundle['format_version'] != 1:
        raise DataError('Unsupported import format; expected version 1')
    meta = fields(bundle['dataset'], ('id', 'kind', 'title', 'source_url', 'license',
                                      'revision', 'retrieved_at', 'coverage'),
                  ('timezone', 'coverage_start', 'coverage_end'))
    for field, value in meta.items():
        text(value, field, 2000 if field in ('source_url', 'license', 'coverage') else 200)
    if meta['kind'] not in KINDS:
        raise DataError('Unsupported dataset kind')
    extra = set(meta) & {'timezone','coverage_start','coverage_end'}
    if extra and (meta['kind'] != 'tides' or extra != {'timezone','coverage_start','coverage_end'}):
        raise DataError('Tide timezone/start/end metadata must be supplied together')
    if extra:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(meta['timezone'])
            if date.fromisoformat(meta['coverage_start']) > date.fromisoformat(meta['coverage_end']):
                raise ValueError('Reversed date coverage')
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise DataError('Invalid tide timezone/date coverage') from exc
    source = urlparse(meta['source_url'])
    if source.scheme not in ('http', 'https') or not source.hostname:
        raise DataError('source_url must be an HTTP(S) provenance URL')
    timestamp(meta['retrieved_at'], 'retrieved_at')
    records = bundle['records']
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_IMPORT_RECORDS:
        raise DataError(f'Import must contain 1..{MAX_IMPORT_RECORDS} records')
    validated, identifiers = [], set()
    for record in records:
        record = validate_record(meta['kind'], record)
        if record['id'] in identifiers:
            raise DataError('Duplicate record ID within dataset')
        identifiers.add(record['id'])
        validated.append(record)
    return dict(meta), validated


def read_bundle(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            raise DataError('Import exceeds 16 MiB; preprocess a smaller supported subset')
        def invalid_constant(value):
            raise DataError(f'Invalid JSON constant {value}')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_keys,
                           parse_constant=invalid_constant)
        return validate_bundle(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ConfigError, RecursionError) as exc:
        raise DataError(f'Cannot read import: {exc}') from exc


class OfflineStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables:
                if tables != {'schema_meta', 'datasets', 'records'}:
                    raise DataError('Unknown content DB schema; preserve the database')
                version = self.db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
                if version is None or version[0] != '1':
                    raise DataError('Unsupported content DB version; preserve the database')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            if not tables:
                with self.db:
                    self.db.execute('CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    self.db.execute("INSERT INTO schema_meta VALUES ('version','1')")
                    self.db.execute('CREATE TABLE datasets (id TEXT PRIMARY KEY, kind TEXT NOT NULL, metadata TEXT NOT NULL)')
                    self.db.execute('CREATE TABLE records (dataset TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE, id TEXT NOT NULL, body TEXT NOT NULL, search TEXT NOT NULL, PRIMARY KEY(dataset,id))')
                    self.db.execute('CREATE INDEX dataset_kind ON datasets(kind)')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def import_validated(self, metadata, records):
        # Recheck callers that bypass read_bundle; validation finishes before writes.
        metadata, records = validate_bundle({'format_version': 1, 'dataset': metadata, 'records': records})
        identifier = metadata['id']
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            count = self.db.execute('SELECT count(*) FROM records WHERE dataset != ?', (identifier,)).fetchone()[0]
            if count + len(records) > MAX_STORED_RECORDS:
                raise DataError('Content store would exceed 100,000 records')
            existing = self.db.execute('SELECT kind FROM datasets WHERE id=?', (identifier,)).fetchone()
            if existing and existing[0] != metadata['kind']:
                raise DataError('Cannot change an existing dataset kind; choose another dataset ID')
            self.db.execute('DELETE FROM datasets WHERE id=?', (identifier,))
            self.db.execute('INSERT INTO datasets VALUES (?,?,?)',
                            (identifier, metadata['kind'], json.dumps(metadata, ensure_ascii=False)))
            for record in records:
                search = ' '.join(str(value) if not isinstance(value, list) else ' '.join(value)
                                  for name, value in record.items() if name not in ('latitude', 'longitude', 'distance_m', 'height_m'))
                self.db.execute('INSERT INTO records VALUES (?,?,?,?)',
                                (identifier, record['id'], json.dumps(record, ensure_ascii=False), normalize(search)))
        return len(records)

    def datasets(self):
        return [dict(json.loads(row['metadata']), record_count=row['count']) for row in self.db.execute(
            'SELECT d.metadata, count(r.id) AS count FROM datasets d LEFT JOIN records r ON d.id=r.dataset GROUP BY d.id ORDER BY d.id')]

    def query(self, kind, term='', limit=100):
        if kind not in KINDS or type(limit) is not int or not 1 <= limit <= 1000:
            raise DataError('Unsupported kind or query limit (1..1000)')
        if not isinstance(term, str) or len(term) > 200:
            raise DataError('Search term must be at most 200 characters')
        parts = normalize(term).split()
        if len(parts) > 20:
            raise DataError('Search has too many words')
        conditions, values = ['d.kind=?'], [kind]
        for part in parts:
            conditions.append("r.search LIKE ? ESCAPE '\\'")
            values.append('%' + part.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        values.append(limit)
        rows = self.db.execute('SELECT r.body,d.metadata FROM records r JOIN datasets d ON d.id=r.dataset WHERE '
                               + ' AND '.join(conditions) + ' ORDER BY d.id,r.id LIMIT ?', values)
        return [(json.loads(row['body']), json.loads(row['metadata'])) for row in rows]

    def iter_kind(self, kind, max_records=20000, term=None):
        """Stream a complete bounded kind for rankings; never rank a silent slice."""
        if kind not in KINDS or type(max_records) is not int or not 1 <= max_records <= 100000:
            raise DataError('Invalid streaming query bounds')
        if term is not None and (not isinstance(term,str) or len(term)>200):
            raise DataError('Invalid streaming search term')
        condition,values = 'd.kind=?',[kind]
        if term:
            condition += " AND r.search LIKE ? ESCAPE '\\'"
            needle=normalize(term).replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            values.append('%'+needle+'%')
        values.append(max_records+1)
        cursor = self.db.execute('SELECT r.body,d.metadata FROM records r JOIN datasets d ON d.id=r.dataset '
                                 'WHERE '+condition+' ORDER BY d.id,r.id LIMIT ?', values)
        try:
            for count, row in enumerate(cursor):
                if count >= max_records:
                    raise DataError('Dataset exceeds complete-ranking limit; import a smaller supported subset')
                yield json.loads(row['body']), json.loads(row['metadata'])
        finally:
            cursor.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Manage local bot data; does not connect to the room')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--data-dir', type=Path)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('list')
    importer = commands.add_parser('import')
    importer.add_argument('file', type=Path)
    args = parser.parse_args(argv)
    store = None
    try:
        config = load_config(args.config)
        data_dir = args.data_dir.expanduser().resolve() if args.data_dir else config.data_path(args.config)
        validated = read_bundle(args.file) if args.action == 'import' else None
        store = OfflineStore(data_dir / 'offline.sqlite3')
        if validated:
            count = store.import_validated(*validated)
            print(f'Imported {count} records into {validated[0]["id"]}')
        else:
            print(json.dumps(store.datasets(), ensure_ascii=False, indent=2))
        return 0
    except (ConfigError, DataError, OSError, sqlite3.Error) as exc:
        print(f'Data operation failed: {exc}')
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
