"""Prepare four offline route estimates with finite explicit operator requests."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from room_bot.data import DataError, read_bundle, validate_bundle

BASE = 'https://valhalla1.openstreetmap.de/route'


def route(a, b, mode):
    request = {'locations': [{'lat':p['latitude'], 'lon':p['longitude']} for p in (a,b)],
               'costing': 'pedestrian' if mode == 'walk' else 'auto',
               'directions_options': {'units':'kilometers'}}
    req = Request(BASE, data=json.dumps(request).encode('utf-8'),
                  headers={'Content-Type':'application/json',
                           'User-Agent':'MeshCore-RoomBot-offline-data-preparation/1',
                           'X-Client-Id':'squishyware.net-roombot-data-preparation'})
    with urlopen(req, timeout=30) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise DataError('Route response exceeds limit')
    value = json.loads(raw)
    trip = value.get('trip', {})
    if trip.get('status') != 0 or trip.get('units') != 'kilometers' or not trip.get('legs'):
        raise DataError('Provider returned an invalid/unsupported route')
    maneuvers = [m for leg in trip['legs'] for m in leg['maneuvers']]
    expected = 'pedestrian' if mode == 'walk' else 'drive'
    if not maneuvers or any(m.get('travel_mode') != expected for m in maneuvers):
        raise DataError('Route has a different travel mode; do not relabel it')
    names = []
    for maneuver in maneuvers:
        for street in maneuver.get('street_names', []):
            if street not in names:
                names.append(street)
    record = {'id':f'{a["id"]}-{b["id"]}-{mode}', 'origin':a['id'], 'destination':b['id'],
              'mode':mode, 'distance_m':round(trip['summary']['length'] * 1000),
              'origin_label':a['name']+' representative point, router-snapped',
              'destination_label':b['name']+' representative point, router-snapped',
              'profile':'Valhalla '+request['costing']+' / OpenStreetMap snapshot',
              'summary':'Map-derived route; no safety/closure check. Major named roads: '+', '.join(names[:8]),
              'waypoints':names[:32]}
    return record, value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('places', type=Path)
    parser.add_argument('origin')
    parser.add_argument('destination')
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists; choose a new snapshot filename')
    _, places = read_bundle(args.places)
    selected = []
    for name in (args.origin, args.destination):
        matches = [p for p in places if p['name'].casefold() == name.casefold()]
        if len(matches) != 1:
            parser.error('Origin/destination must uniquely match the supplied place catalogue')
        selected.append(matches[0])
    records, evidence = [], []
    for a,b in (selected, selected[::-1]):
        for mode in ('walk','road'):
            record, raw = route(a,b,mode)
            records.append(record)
            evidence.append({'origin':a['id'], 'destination':b['id'], 'mode':mode, 'response':raw})
            print(f'Prepared {a["name"]} -> {b["name"]} {mode}: {record["distance_m"]} m')
            time.sleep(1)
    value = {'format_version':1,'dataset':{
        'id':'route-pair-'+selected[0]['id']+'-'+selected[1]['id'], 'kind':'routes',
        'title':'OpenStreetMap contributors / Valhalla FOSSGIS route estimates',
        'source_url':'https://valhalla.openstreetmap.de/',
        'license':'OpenStreetMap-derived route data, ODbL 1.0; https://www.openstreetmap.org/copyright . LINZ endpoint coordinates CC BY 4.0.',
        'revision':'Prepared '+datetime.now(timezone.utc).isoformat()+'; underlying OSM data revision not exposed',
        'retrieved_at':datetime.now(timezone.utc).isoformat(),
        'coverage':f'{args.origin}<->{args.destination}, pedestrian and driving; Gazetteer representative points snapped by router. Map-derived estimates, not field-verified routes.'},
        'records':records}
    validate_bundle(value)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.write('\n')
    with args.output.with_suffix('.evidence.json').open('x',encoding='utf-8') as stream:
        json.dump(evidence,stream,ensure_ascii=False,indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
