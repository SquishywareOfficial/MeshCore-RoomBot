"""Offline place resolution and explicitly typed distance answers."""
import math
import heapq

from .data import DataError, normalize


EARTH_RADIUS_M = 6371008.8


class PlaceError(DataError):
    pass


def resolve_place(store, name):
    name = name.strip()
    base, separator, area = name.partition(',')
    # A common source area (e.g. Auckland) can match >1,000 records even
    # though one place has that exact name. Stream all bounded candidates.
    rows = store.iter_kind('places', term=base.strip())
    exact = [(p, source) for p, source in rows
             if normalize(base.strip()) in [normalize(p['name']), normalize(p['id'])] + [normalize(a) for a in p['aliases']]
             and (not separator or normalize(area.strip()) in normalize(p['region']))]
    if not exact:
        raise PlaceError('No exact saved place match. Use its full name, optionally followed by comma and source area.')
    if len(exact) > 1:
        options = [f'{p["name"]}, {p["region"]} ({p["id"]})' for p, _ in exact[:5]]
        raise PlaceError('Ambiguous place: ' + '; '.join(options) + '. Use name, area or a listed ID.')
    return exact[0]


def straight_distance(a, b):
    lat1, lat2 = map(math.radians, (a['latitude'], b['latitude']))
    dlat = lat2 - lat1
    dlon = math.radians(b['longitude'] - a['longitude'])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(max(0, min(1, h))))


def parse_pair(args):
    if not args:
        raise PlaceError('Usage: /bot location distance FROM to TO [walk|road|straight]')
    mode = args[-1].casefold() if args[-1].casefold() in ('walk', 'road', 'straight') else 'walk'
    words = args[:-1] if args[-1].casefold() in ('walk', 'road', 'straight') else args
    separators = [i for i, word in enumerate(words) if word.casefold() == 'to']
    if len(separators) != 1 or separators[0] == 0 or separators[0] == len(words) - 1:
        raise PlaceError('Usage: /bot location distance FROM to TO [walk|road|straight]')
    split = separators[0]
    return ' '.join(words[:split]), ' '.join(words[split + 1:]), mode


def place_reply(store, name, details=None):
    from .locations import resolve_location
    place, source = resolve_location(store, name,allow_street_point=True)
    if place.get('_origin_kind')=='street':
        if details is not None:
            details.extend([f'{place["name"]}: street component approx {place["_street_length_m"]:.0f} m long. '
                            'The reference point is not your exact position or a house address.',
                            'Locality hints search within 20 km; they are not municipal boundaries.'])
        return (f'{place["name"]}: {place["latitude"]:.6f},{place["longitude"]:.6f} '
                f'(street reference point). For routes: {place["id"]}@point.')
    if place.get('_origin_kind')=='coordinate':
        if details is not None:details.append('User-supplied coordinate; no address reverse-geocoding is available.')
        return f'Coordinates: {place["latitude"]:.6f},{place["longitude"]:.6f}. Use with /bot location distance or /bot location water.'
    if details is not None:details.append(f'{place["feature_type"]}; saved area {place["region"]}; ID {place["id"]}. This is a representative point.')
    return f'{place["name"]}: {place["latitude"]:.6f},{place["longitude"]:.6f} (reference point).'


def distance_reply(store, args, router=None, details=None):
    from .locations import resolve_location,describe_origin
    from_name, to_name, mode = parse_pair(args)
    a, a_source = resolve_location(store, from_name)
    b, b_source = resolve_location(store, to_name)
    label = f'{a["name"]} to {b["name"]}'
    distance = straight_distance(a, b)
    if details is not None:details.append(f'Origins: {describe_origin(a)} / {describe_origin(b)}.')
    straight = f'{label}: straight-line approx {distance / 1000:.1f} km; not a walking route.'
    if mode == 'straight':
        return [straight]
    if mode == 'walk' and router is not None:
        from .routing import RoutingError
        if a['id'] == b['id']:
            return f'{label}: same selected endpoint, 0 km.'
        try:
            route = router.route(a, b)
        except RoutingError as exc:
            return f'{label}: {exc}'
        if details is not None:details.append(f'Endpoints snapped {route.start_snap_m:.0f} m / {route.end_snap_m:.0f} m. '
                                              'Snap gaps are not walking directions or included in route distance.')
        return [f'{label}: mapped walking route approx {route.distance_m / 1000:.1f} km. Access/safety unverified; ferries excluded.']
    candidates = store.query('routes', a['id'] + ' ' + b['id'], 1000)
    if len(candidates) == 1000:
        return 'Too many saved route candidates; ask the operator to select a dataset.'
    exact = [(r, source) for r, source in candidates
             if r['origin'] == a['id'] and r['destination'] == b['id'] and r['mode'] == mode]
    if not exact:
        return [f'No saved {mode} route for {label}.', straight]
    if len(exact) > 1:
        return 'Multiple saved routes match; ask the operator to select a route dataset.'
    route, source = exact[0]
    if details is not None:details.extend([f'Endpoints: {route["origin_label"]} to {route["destination_label"]}.',
                                          f'{route["summary"]} Profile: {route["profile"]}.'])
    road_note=' Includes SH 23.' if 'SH 23' in route['summary'] else ''
    return [f'{label}: map-derived {mode} route estimate {route["distance_m"] / 1000:.1f} km; access/safety unverified.'+road_note]


def initial_bearing(a, b):
    if (straight_distance(a,b) < 0.001 or abs(a['latitude']) == 90
            or (abs(a['latitude'] + b['latitude']) < 1e-9
                and abs(abs((b['longitude'] - a['longitude'] + 180) % 360 - 180) - 180) < 1e-9)):
        return None
    lat1,lat2 = map(math.radians, (a['latitude'],b['latitude']))
    lon = math.radians(b['longitude']-a['longitude'])
    y = math.sin(lon)*math.cos(lat2)
    x = math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(lon)
    return math.degrees(math.atan2(y,x)) % 360


BEARING_USAGE = 'Usage: /bot location bearing [compass|true|both] FROM to TO'


def bearing_angle(angle):
    heading = int(math.floor(angle % 360 + 0.5)) % 360
    compass = ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
               'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW')
    direction = compass[int((heading + 11.25) // 22.5) % 16]
    return f'{heading}° {direction}'


def bearing_reply(store, args, clock=None, details=None):
    from .locations import resolve_location, describe_origin
    from .clock import ClockUnavailable
    from .magnetic import MagneticUnavailable, decimal_year, declination, compass_bearing
    args = list(args)
    modes = {'compass': 'compass', 'magnetic': 'compass', 'true': 'true', 'both': 'both'}
    mode, selected = 'compass', False
    if args and args[0].casefold() in modes:
        mode = modes[args.pop(0).casefold()]
        selected = True
    if args and args[-1].casefold() in modes:
        if selected:
            return BEARING_USAGE
        mode = modes[args.pop().casefold()]
    if args and args[-1].casefold() in ('walk', 'road', 'straight'):
        return BEARING_USAGE
    try:
        from_name, to_name, _ = parse_pair(args)
    except PlaceError:
        return BEARING_USAGE
    a, _ = resolve_location(store, from_name)
    b, _ = resolve_location(store, to_name)
    angle = initial_bearing(a, b)
    if angle is None:
        return 'Bearing is undefined for these reference points.'
    label = f'{a["name"]} to {b["name"]}'
    true_text = f'True: {bearing_angle(angle)}'
    if details is not None:
        details.append(f'Initial direct bearing, not road directions. Origins: {describe_origin(a)} / {describe_origin(b)}.')
    if mode == 'true':
        return f'{label}: {true_text}.'
    try:
        if clock is None:
            raise ClockUnavailable('clock unconfirmed')
        day = clock.now()
        variation = declination(a['latitude'], a['longitude'], decimal_year(day))
        magnetic = compass_bearing(angle, variation)
    except ClockUnavailable:
        return f'{label}: Compass unavailable (clock unconfirmed). {true_text}.'
    except MagneticUnavailable as exc:
        return f'{label}: Compass unavailable ({exc}). {true_text}.'
    if details is not None:
        details.append(f'WMM2025 at origin, {day:%Y-%m-%d}; declination {abs(variation):.2f}° '
                       f'{"east" if variation >= 0 else "west"}. Zero ellipsoid-height approximation. '
                       'Compass means an unadjusted magnetic compass; use true if your compass already corrects declination.')
    result = f'Compass: {bearing_angle(magnetic)}'
    if mode == 'both':
        result += f'; true: {bearing_angle(angle)}'
    return f'{label}: {result}.'


def towns_reply(store, args, limit=3):
    from .locations import resolve_location
    if not args:
        return 'Usage: /bot location towns LOCATION. Example: /bot location towns Hamilton.'
    origin, _ = resolve_location(store, ' '.join(args), allow_street_point=True)
    settlement_types = ('city', 'town', 'village', 'locality')
    def candidates():
        for place, source in store.iter_kind('places'):
            if place['id'] != origin['id'] and normalize(place['feature_type']) in settlement_types:
                yield straight_distance(origin, place), place
    rows = heapq.nsmallest(limit, candidates(), key=lambda row: row[0])
    if not rows:
        return 'No towns or localities found in the installed catalogue.'
    reference = ' (street reference point)' if origin.get('_origin_kind') == 'street' else ''
    header = f'Nearest towns/localities to {origin["name"]}{reference}, straight-line:'
    return header + '\n' + '\n'.join(f'{p["name"]}: {distance / 1000:.1f} km' for distance, p in rows)


def walktime_reply(args):
    if not 1 <= len(args) <= 2:
        return 'Usage: /bot location walktime KM [PACE_KM_PER_HOUR]; default pace 4 km/h'
    try:
        distance = float(args[0])
        pace = float(args[1]) if len(args) == 2 else 4.0
        if not math.isfinite(distance) or not 0 <= distance <= 20000 or not math.isfinite(pace) or not 0.5 <= pace <= 10:
            raise ValueError()
    except ValueError:
        return 'Enter distance 0..20000 km and assumed walking pace 0.5..10 km/h.'
    return f'Rough walking-time estimate: {distance/pace:.1f} h for {distance:g} km at assumed {pace:g} km/h, excluding stops, terrain and weather. No route/access assessment.'
