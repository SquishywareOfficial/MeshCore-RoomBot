"""Bounded offline Routino adapter. No network calls or shell execution."""
import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

from .config import RoutingSettings
from .geography import straight_distance


GRAPH_FILES = ('nodes.mem', 'segments.mem', 'ways.mem', 'relations.mem')
PROFILE = 'nz-foot-v1'


class RoutingError(ValueError):
    """An expected operator/data/no-path problem, safe to report without guessing."""


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path):
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns)


@dataclass(frozen=True)
class RouteResult:
    distance_m: float
    start_snap_m: float
    end_snap_m: float
    revision: str
    source_url: str
    points: int


def parse_route(stream, origin, destination, max_snap_metres):
    first = last = None
    previous_total = -1
    section_sum_m = 0
    count = 0
    for line in stream:
        if len(line) > 8192:
            raise RoutingError('Invalid local routing output; ask the operator to check the graph.')
        if not line.strip() or line.startswith('#'):
            continue
        words = line.split(maxsplit=10)
        try:
            latitude, longitude, section_km, total_km = float(words[0]), float(words[1]), float(words[4]), float(words[6])
            kind = re.sub(r'^Waypt#[12]$', 'Waypt', words[3])
            if (len(words) < 8 or kind not in ('Waypt', 'Junct', 'Junct-', 'Change', 'Inter', 'U-turn', 'Mini-RB')
                    or not all(math.isfinite(x) for x in (latitude, longitude, section_km, total_km))
                    or not -90 <= latitude <= 90 or not -180 <= longitude <= 180
                    or not 0 <= total_km <= 20000 or not 0 <= section_km <= 20000 or total_km < previous_total):
                raise ValueError()
        except (ValueError, IndexError):
            raise RoutingError('Invalid local routing output; ask the operator to check the graph.') from None
        point = {'latitude': latitude, 'longitude': longitude}
        section_sum_m += section_km * 1000
        # Simplified graph nodes and rounded lengths can differ by a few metres.
        # National evidence includes legitimate ~6 m deviations after pruning.
        if last is not None and straight_distance(last, point) > section_km * 1000 + 15:
            raise RoutingError('Local route contains an inconsistent segment/gap.')
        if first is None:
            if kind != 'Waypt' or total_km != 0:
                raise RoutingError('Invalid local route start.')
            first = point
        last = point
        previous_total = total_km
        count += 1
    if count < 2 or kind != 'Waypt':
        raise RoutingError('No connected mapped walking route found (ferries excluded).')
    start_snap, end_snap = straight_distance(origin, first), straight_distance(destination, last)
    bounds=max_snap_metres if isinstance(max_snap_metres,tuple) else (max_snap_metres,max_snap_metres)
    for snap,bound in zip((start_snap,end_snap),bounds):
        if snap>bound:
            raise RoutingError(f'No walking endpoint within {bound:g} m of the requested origin/destination. Route unavailable.')
    distance_m = previous_total * 1000
    if abs(section_sum_m - distance_m) > 10 + count * 0.6:
        raise RoutingError('Local route section totals are inconsistent.')
    # Rounded route totals have 10 m precision. Catch grossly inconsistent output.
    if distance_m + 30 < straight_distance(first, last):
        raise RoutingError('Local route distance is inconsistent with its endpoints.')
    return distance_m, start_snap, end_snap, count


class RoutinoRouter:
    def __init__(self, settings, data_path):
        self.settings = settings
        graph = Path(settings.graph_dir).expanduser()
        self.graph = (graph if graph.is_absolute() else Path(data_path) / graph).resolve()
        self.cache_path = Path(data_path) / 'routing-cache.sqlite3'
        self.lock = threading.Lock()
        self.manifest = None

    def _load(self):
        try:
            path = self.graph / 'manifest.json'
            raw = path.read_bytes()
            if len(raw) > 65536:
                raise ValueError()
            manifest = json.loads(raw)
            if (manifest['version'] != 1 or manifest['engine'] != 'routino'
                    or manifest['profile'] != PROFILE):
                raise ValueError()
            for key in ('revision', 'source_url', 'engine_version'):
                if (not isinstance(manifest[key], str) or not 1 <= len(manifest[key]) <= 500
                        or any(ord(c) < 32 for c in manifest[key])):
                    raise ValueError()
            paths = [path]
            for name in GRAPH_FILES + ('profiles.xml', 'tagging.xml', 'translations.xml'):
                record = manifest['files'][name]
                file = self.graph / name
                if (not re.fullmatch('[0-9a-f]{64}', record['sha256'])
                        or file.stat().st_size != record['bytes']
                        or file_sha256(file) != record['sha256']):
                    raise ValueError()
                paths.append(file)
            foot = ET.parse(self.graph / 'profiles.xml').getroot().find(f"./profile[@name='{PROFILE}']")
            if foot is None or foot.get('transport') != 'foot':
                raise ValueError()
            preferences = {p.get('highway'): p.get('percent') for p in foot.findall('./preferences/preference')}
            if preferences != {h: ('0' if h in ('motorway', 'ferry') else '100') for h in
                    ('motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified',
                     'residential', 'service', 'track', 'cycleway', 'path', 'steps', 'ferry')}:
                raise ValueError()
            properties = {p.get('type'): p.get('percent') for p in foot.findall('./properties/property')}
            if properties != {p: '50' for p in ('paved', 'multilane', 'bridge', 'tunnel', 'footroute', 'bicycleroute')}:
                raise ValueError()
            version = subprocess.run([self.settings.executable, '--version'], capture_output=True,
                                     timeout=5, check=True)
            reported = (version.stdout + version.stderr).decode('utf-8').strip()
            if reported != manifest['engine_version']:
                raise RoutingError('Local router version differs from graph builder; install the matching Routino version.')
            self.tokens = {file: fingerprint(file) for file in paths}
            self.namespace = hashlib.sha256(raw + str((self.settings.max_snap_metres,self.settings.precise_snap_metres)).encode()).hexdigest()
            self.manifest = manifest
        except RoutingError:
            raise
        except (OSError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError):
            raise RoutingError('Local walking graph/router missing or invalid; ask the operator to install/verify the snapshot.') from None

    def verify(self):
        with self.lock:
            if self.manifest is None:
                self._load()
            else:
                try:
                    changed = any(fingerprint(path) != saved for path, saved in self.tokens.items())
                except OSError:
                    changed = True
                if changed:
                    raise RoutingError('Routing snapshot changed; restart with a verified immutable snapshot.')
            return self.manifest

    def _cache(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.cache_path, timeout=5)
        conn.execute('CREATE TABLE IF NOT EXISTS routes (key TEXT PRIMARY KEY, result TEXT NOT NULL, used INTEGER NOT NULL)')
        return conn

    def _run(self, origin, destination, timeout_seconds=None):
        command = [self.settings.executable, f'--dir={self.graph}',
                   f'--profiles={self.graph / "profiles.xml"}', f'--profile={PROFILE}',
                   f'--translations={self.graph / "translations.xml"}', '--language=en',
                   '--transport=foot', '--shortest', '--output-text-all', '--output-stdout',
                   f'--lat1={origin["latitude"]:.8f}', f'--lon1={origin["longitude"]:.8f}',
                   f'--lat2={destination["latitude"]:.8f}', f'--lon2={destination["longitude"]:.8f}']
        with tempfile.TemporaryDirectory(prefix='room-bot-route-') as temp:
            out_path, err_path = Path(temp) / 'route.txt', Path(temp) / 'error.txt'
            with out_path.open('wb') as out, err_path.open('wb') as err:
                process = subprocess.Popen(command, stdout=out, stderr=err, cwd=temp)
                deadline = time.monotonic() + (self.settings.timeout_seconds if timeout_seconds is None else timeout_seconds)
                try:
                    while process.poll() is None:
                        if time.monotonic() >= deadline:
                            raise RoutingError('Walking route calculation timed out; try a shorter journey.')
                        if out_path.stat().st_size + err_path.stat().st_size > self.settings.output_limit_bytes:
                            raise RoutingError('Walking route output limit reached; try a shorter journey.')
                        time.sleep(0.05)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            if out_path.stat().st_size + err_path.stat().st_size > self.settings.output_limit_bytes:
                raise RoutingError('Walking route output limit reached; try a shorter journey.')
            if process.returncode:
                # Keep arbitrary external stderr out of shared room messages.
                error = err_path.read_text(encoding='utf-8', errors='replace')
                if any(message in error for message in ('Cannot find route', 'Cannot find node close',
                       'Cannot find initial section of route', 'Cannot find final section of route',
                       'Cannot find super-route compatible')):
                    raise RoutingError('No connected mapped walking route found (ferries excluded).')
                raise RoutingError('Local walking router failed; ask the operator to check its graph/logs.')
            with out_path.open(encoding='utf-8', errors='strict') as stream:
                return parse_route(stream, origin, destination, self.snap_bounds(origin,destination))

    def snap_bounds(self,origin,destination):
        return tuple(min(10,self.settings.precise_snap_metres,self.settings.max_snap_metres)
                     if point.get('_origin_kind') == 'water_approach' else
                     min(self.settings.max_snap_metres,self.settings.precise_snap_metres)
                     if point.get('_origin_kind') in ('coordinate','street') else self.settings.max_snap_metres
                     for point in (origin,destination))

    def route(self, origin, destination, timeout_seconds=None):
        if timeout_seconds is not None:
            if type(timeout_seconds) not in (float,int) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise RoutingError('Invalid walking route time budget.')
            timeout_seconds = min(timeout_seconds,self.settings.timeout_seconds)
        self.verify()
        with self.lock:
            try:
                # Direction, exact coordinates, stable IDs and immutable revision all matter.
                key = hashlib.sha256(json.dumps([self.namespace, origin['id'], destination['id'],
                    origin['latitude'], origin['longitude'], destination['latitude'], destination['longitude'],
                    'walk', PROFILE,self.snap_bounds(origin,destination)], separators=(',', ':')).encode()).hexdigest()
                conn = self._cache()
                try:
                    row = conn.execute('SELECT result FROM routes WHERE key=?', (key,)).fetchone()
                    if row:
                        result = RouteResult(**json.loads(row[0]))
                        if (not all(type(n) in (float, int) and math.isfinite(n) and n >= 0 for n in
                                (result.distance_m, result.start_snap_m, result.end_snap_m))
                                or result.distance_m > 20000000
                                or any(snap>bound for snap,bound in zip((result.start_snap_m,result.end_snap_m),self.snap_bounds(origin,destination)))
                                or result.revision != self.manifest['revision']
                                or result.source_url != self.manifest['source_url']
                                or type(result.points) is not int or result.points < 2):
                            raise RoutingError('Invalid cached walking route; ask the operator to clear the routing cache.')
                        with conn:
                            conn.execute('UPDATE routes SET used=? WHERE key=?', (time.time_ns(), key))
                        return result
                    distance, start, end, count = (self._run(origin, destination) if timeout_seconds is None else
                                                 self._run(origin, destination, timeout_seconds=timeout_seconds))
                    result = RouteResult(distance, start, end, self.manifest['revision'], self.manifest['source_url'], count)
                    with conn:
                        conn.execute('INSERT OR REPLACE INTO routes VALUES (?, ?, ?)',
                                     (key, json.dumps(asdict(result)), time.time_ns()))
                        conn.execute('DELETE FROM routes WHERE key NOT IN (SELECT key FROM routes ORDER BY used DESC LIMIT ?)',
                                     (self.settings.cache_entries,))
                    return result
                finally:
                    conn.close()
            except RoutingError:
                raise
            except (OSError, ValueError, TypeError, sqlite3.Error, UnicodeError, subprocess.SubprocessError):
                raise RoutingError('Offline routing storage/output unavailable; ask the operator to check it.') from None


def main():
    parser = argparse.ArgumentParser(description='Verify a local pedestrian routing snapshot without USB/network.')
    parser.add_argument('--graph', type=Path, required=True)
    parser.add_argument('--executable', default='routino-router-slim')
    args = parser.parse_args()
    router = RoutinoRouter(RoutingSettings(backend='routino', graph_dir=str(args.graph.resolve()),
                          executable=args.executable), args.graph.resolve().parent)
    try:
        print(json.dumps(router.verify(), indent=2))
    except (RoutingError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
