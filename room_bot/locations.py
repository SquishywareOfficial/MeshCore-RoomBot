"""Shared place/street/coordinate origins for routing and future water lookup."""
import math
from pathlib import Path
import re

from .geography import PlaceError,resolve_place,straight_distance
from .data import normalize
from .streets import StreetStore

DECIMAL=r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
COORDINATES=re.compile(r'^\s*('+DECIMAL+r')\s*,\s*('+DECIMAL+r')\s*$')


def place_near(store, name, anchor, radius_m=20000):
    """Resolve an exact place name using a nearby town as a hint, not a boundary."""
    wanted = normalize(name)
    rows = []
    for record, source in store.iter_kind('places', term=name):
        names = (record['name'], record['id'], *record['aliases'])
        if (wanted in (normalize(value) for value in names) and
                straight_distance(record, anchor) <= radius_m):
            rows.append((record, source))
    if len(rows) > 1:
        choices = '; '.join(f'{p["name"]} ({p["id"]})' for p, _ in rows[:5])
        raise PlaceError('Several places match near ' + anchor['name'] + '. Use a listed ID: ' + choices)
    return rows[0] if rows else None


def resolve_location(store,name,allow_street_point=False):
    name=name.strip()
    if not name or len(name)>200 or any(ord(c)<32 for c in name):
        raise PlaceError('Origin must be nonempty text, at most 200 characters, without controls.')
    match=COORDINATES.fullmatch(name)
    if match:
        lat,lon=map(float,match.groups())
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:
            raise PlaceError('Use valid WGS84 latitude, longitude in that order (-90..90, -180..180).')
        # Native router uses eight decimals; canonicalize IDs and avoid signed zero.
        lat,lon=round(lat,8)+0.0,round(lon,8)+0.0
        return {'id':f'coord:{lat:.8f},{lon:.8f}','name':f'{lat:.6f},{lon:.6f}',
                'latitude':lat,'longitude':lon,'region':'User coordinates','feature_type':'User coordinate',
                'aliases':[],'_origin_kind':'coordinate'}, {
                'title':'User-supplied WGS84 coordinate','source_url':'User input; accuracy unknown'}
    if ',' in name and all(re.fullmatch(r'[0-9.eE+\-\s]+|[+-]?(?:nan|inf|infinity)',part,re.I) for part in name.split(',')):
        raise PlaceError('Use decimal WGS84 latitude, longitude; NaN/infinity/exponents are not supported.')
    opted=bool(re.search(r'@point$',name,re.I))
    query=re.sub(r'\s*@point$','',name,flags=re.I).strip()
    try:
        return resolve_place(store,query)
    except PlaceError as exc:
        if str(exc).startswith('Ambiguous place:'):raise
        place_error=exc
    anchor=None;road=query
    if ',' in query:
        road,hint=query.split(',',1)
        anchor,_=resolve_place(store,hint.strip())
    elif not re.fullmatch(r'street:\d+',query):
        words=query.split()
        for i in range(1,len(words)):
            try:anchor,_=resolve_place(store,' '.join(words[i:]))
            except PlaceError as exc:
                if str(exc).startswith('Ambiguous place:'):raise
                continue
            road=' '.join(words[:i]);break
    if anchor is not None:
        place = place_near(store, road.strip(), anchor)
        if place is not None:
            return place
    path=Path(store.path).parent/'streets.sqlite3'
    if not path.exists():raise place_error
    streets=StreetStore(path)
    try:
        rows=streets.find(road.strip(),anchor)
        metadata=dict(streets.metadata)
    finally:streets.close()
    if not rows:
        if re.match(r'^\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?\s',road):
            raise PlaceError('House numbers are not supported. Try the street name and town, or latitude,longitude.')
        raise PlaceError('No matching street near that town. Check the name, or use latitude,longitude.')
    if len(rows)>1:
        choices='\n'.join(f'street:{r["id"]}: {r["name"]} ({r["latitude"]:.5f},{r["longitude"]:.5f})' for r in rows[:5])
        raise PlaceError('Ambiguous street: several sections match. Choose one with /bot location place ID:\n'+choices)
    row=rows[0]
    if not opted and not allow_street_point:
        raise PlaceError(f'{row["name"]} is a street, not one position. Add @point to explicitly use its approximate '
                         f'representative point ({row["latitude"]:.6f},{row["longitude"]:.6f}), or supply your coordinates. '
                         f'ID: street:{row["id"]}.')
    hint=f' near {anchor["name"]}' if anchor else ''
    record={'id':f'street:{row["id"]}','name':row['name']+hint,
            'latitude':row['latitude'],'longitude':row['longitude'],'region':anchor['name'] if anchor else 'Locality not specified',
            'feature_type':'Street representative point','aliases':[], '_origin_kind':'street',
            '_street_length_m':row['length_m']}
    return record,metadata


def describe_origin(record):
    kind=record.get('_origin_kind')
    if kind=='coordinate':return 'user coordinate (GPS accuracy unknown)'
    if kind=='street':return 'street representative point, not your position'
    return 'named-place representative point'
