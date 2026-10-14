"""Validated host configuration; never stores clock trust or bot cursor state."""
from dataclasses import asdict, dataclass, field
import json
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


COMMANDS = frozenset(('ping', 'help', 'about', 'directory', 'emergency', 'notice',
                     'checkin', 'lastseen', 'tides', 'place', 'distance', 'bearing',
                     'nearby', 'walktime', 'sun', 'guide', 'calc', 'weather',
                     'forecast', 'news', 'quakes', 'surf', 'water', 'source', 'details',
                     'location', 'towns'))
DEFAULT_ROOT = Path.home() / '.meshcore-room-bot'


class ConfigError(ValueError):
    pass


def bounded(value, name, low, high, integer=True):
    types = (int,) if integer else (int, float)
    if type(value) not in types or not low <= value <= high or not math.isfinite(value):
        raise ConfigError(f'{name} must be {"an integer" if integer else "a number"} in {low}..{high}')
    return value


def clean_text(value, name, limit=200):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ConfigError(f'{name} must be nonempty text without control characters (max {limit})')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ConfigError(f'{name} contains invalid Unicode') from exc
    return value


def checked_object(value, allowed, name):
    if not isinstance(value, dict):
        raise ConfigError(f'{name} must be an object')
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigError(f'Unknown {name} keys: {", ".join(sorted(unknown))}')
    return value


@dataclass(frozen=True)
class Limits:
    query_results: int = 100
    paging_users: int = 128
    page_ttl_seconds: int = 300
    request_cooldown_seconds: float = 3

    def __post_init__(self):
        bounded(self.query_results, 'query_results', 1, 1000)
        bounded(self.paging_users, 'paging_users', 1, 1024)
        bounded(self.page_ttl_seconds, 'page_ttl_seconds', 10, 3600)
        bounded(self.request_cooldown_seconds, 'request_cooldown_seconds', 0, 300, False)


@dataclass(frozen=True)
class RoutingSettings:
    backend: str = 'none'
    graph_dir: str = 'routing/nz'
    executable: str = 'routino-router-slim'
    timeout_seconds: float = 90
    max_snap_metres: float = 1000
    precise_snap_metres: float = 100
    output_limit_bytes: int = 8388608
    cache_entries: int = 1000

    def __post_init__(self):
        if self.backend not in ('none', 'routino'):
            raise ConfigError('routing.backend must be none or routino')
        clean_text(self.graph_dir, 'routing.graph_dir', 4096)
        clean_text(self.executable, 'routing.executable', 4096)
        bounded(self.timeout_seconds, 'routing.timeout_seconds', 1, 300, False)
        bounded(self.max_snap_metres, 'routing.max_snap_metres', 1, 5000, False)
        bounded(self.precise_snap_metres, 'routing.precise_snap_metres', 1, 1000, False)
        bounded(self.output_limit_bytes, 'routing.output_limit_bytes', 4096, 33554432)
        bounded(self.cache_entries, 'routing.cache_entries', 1, 10000)


@dataclass(frozen=True)
class WaterSettings:
    radius_metres: int = 5000
    candidate_limit: int = 12
    scan_limit: int = 20000
    route_budget_seconds: float = 30
    geometry_budget_seconds: float = 5

    def __post_init__(self):
        bounded(self.radius_metres, 'water.radius_metres', 100, 20000)
        bounded(self.candidate_limit, 'water.candidate_limit', 1, 50)
        bounded(self.scan_limit, 'water.scan_limit', 100, 50000)
        bounded(self.route_budget_seconds, 'water.route_budget_seconds', 1, 90, False)
        bounded(self.geometry_budget_seconds, 'water.geometry_budget_seconds', 0.1, 15, False)


@dataclass(frozen=True)
class CommunitySettings:
    notice_days: int = 7
    checkin_days: int = 30
    notice_limit: int = 1000
    checkin_limit: int = 512

    def __post_init__(self):
        bounded(self.notice_days,'notice_days',0,3650)
        bounded(self.checkin_days,'checkin_days',0,3650)
        bounded(self.notice_limit,'notice_limit',1,5000)
        bounded(self.checkin_limit,'checkin_limit',1,5000)


@dataclass(frozen=True)
class WeatherSettings:
    enabled: bool = False
    timeout_seconds: float = 5
    cache_seconds: int = 600
    stale_weather_seconds: int = 7200
    stale_forecast_seconds: int = 21600
    cache_entries: int = 128

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ConfigError('weather.enabled must be boolean')
        bounded(self.timeout_seconds, 'weather.timeout_seconds', 1, 15, False)
        bounded(self.cache_seconds, 'weather.cache_seconds', 60, 3600)
        bounded(self.stale_weather_seconds, 'weather.stale_weather_seconds', self.cache_seconds, 21600)
        bounded(self.stale_forecast_seconds, 'weather.stale_forecast_seconds', self.cache_seconds, 86400)
        bounded(self.cache_entries, 'weather.cache_entries', 4, 512)


@dataclass(frozen=True)
class Config:
    version: int = 1
    enabled_commands: tuple = ('ping', 'help', 'about')
    default_location: str = 'Hamilton'
    timezone: str = 'Pacific/Auckland'
    data_dir: str = '~/.meshcore-room-bot/data'
    limits: Limits = field(default_factory=Limits)
    clock_max_jump_seconds: float = 5
    community: CommunitySettings = field(default_factory=CommunitySettings)
    aliases: tuple = ()
    routing: RoutingSettings = field(default_factory=RoutingSettings)
    water: WaterSettings = field(default_factory=WaterSettings)
    weather: WeatherSettings = field(default_factory=WeatherSettings)

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ConfigError('Unsupported config version; expected 1')
        if (not isinstance(self.enabled_commands, tuple)
                or any(not isinstance(c, str) or c not in COMMANDS for c in self.enabled_commands)
                or len(set(self.enabled_commands)) != len(self.enabled_commands)):
            raise ConfigError('enabled_commands must contain unique supported command names')
        clean_text(self.default_location, 'default_location')
        clean_text(self.timezone, 'timezone')
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ConfigError('Unknown timezone or missing tzdata; install requirements.txt') from exc
        clean_text(self.data_dir, 'data_dir', 4096)
        if not isinstance(self.limits, Limits):
            raise ConfigError('limits must be validated Limits')
        bounded(self.clock_max_jump_seconds, 'clock_max_jump_seconds', 0.1, 60, False)
        if not isinstance(self.community,CommunitySettings):
            raise ConfigError('community must be validated settings')
        if not isinstance(self.routing, RoutingSettings):
            raise ConfigError('routing must be validated settings')
        if not isinstance(self.water, WaterSettings):
            raise ConfigError('water must be validated settings')
        if not isinstance(self.weather, WeatherSettings):
            raise ConfigError('weather must be validated settings')
        if not isinstance(self.aliases,tuple) or len(self.aliases)>128:
            raise ConfigError('aliases must contain at most 128 saved mappings')
        seen=set()
        for item in self.aliases:
            if not isinstance(item,tuple) or len(item)!=2:
                raise ConfigError('aliases must map names to full public keys')
            name,key=item
            clean_text(name,'alias',32)
            canonical=' '.join(name.split()).casefold()
            if canonical in seen or not isinstance(key,str) or not re.fullmatch(r'[0-9a-f]{64}',key):
                raise ConfigError('Aliases must have unique names and full lowercase hexadecimal public keys')
            seen.add(canonical)

    @classmethod
    def from_dict(cls, value):
        checked_object(value, cls.__dataclass_fields__, 'config')
        options = dict(value)
        if 'enabled_commands' in options:
            if not isinstance(options['enabled_commands'], list):
                raise ConfigError('enabled_commands must be an array')
            options['enabled_commands'] = tuple(options['enabled_commands'])
        if 'limits' in options:
            options['limits'] = Limits(**checked_object(options['limits'], Limits.__dataclass_fields__, 'limits'))
        if 'community' in options:
            options['community']=CommunitySettings(**checked_object(options['community'],CommunitySettings.__dataclass_fields__,'community'))
        if 'routing' in options:
            options['routing'] = RoutingSettings(**checked_object(options['routing'], RoutingSettings.__dataclass_fields__, 'routing'))
        if 'water' in options:
            options['water'] = WaterSettings(**checked_object(options['water'], WaterSettings.__dataclass_fields__, 'water'))
        if 'weather' in options:
            options['weather'] = WeatherSettings(**checked_object(options['weather'], WeatherSettings.__dataclass_fields__, 'weather'))
        if 'aliases' in options:
            if not isinstance(options['aliases'],dict): raise ConfigError('aliases must be an object')
            options['aliases']=tuple(options['aliases'].items())
        return cls(**options)

    def to_dict(self):
        result = asdict(self)
        result['enabled_commands'] = list(self.enabled_commands)
        result['aliases']=dict(self.aliases)
        return result

    def data_path(self, config_path=None):
        path = Path(self.data_dir).expanduser()
        if not path.is_absolute():
            if config_path is None:
                raise ConfigError('Relative data_dir requires an explicit config file')
            path = Path(config_path).resolve().parent / path
        return path.resolve()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f'Duplicate config key: {key}')
        result[key] = value
    return result


def load_config(path=None):
    if path is None:
        return Config()
    path = Path(path).expanduser()
    try:
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ConfigError('Config exceeds 64 KiB')
        def invalid_constant(value):
            raise ConfigError(f'Invalid JSON numeric constant: {value}')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_keys,
                           parse_constant=invalid_constant)
        return Config.from_dict(value)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ConfigError(f'Cannot load config {path}: {exc}') from exc


def write_new_config(path, config=None):
    """Create a template without overwriting an operator's existing config."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump((config or Config()).to_dict(), stream, ensure_ascii=False, indent=2)
        stream.write('\n')
