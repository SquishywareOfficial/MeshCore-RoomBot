"""Explicit operator download of a bounded LINZ settlement subset (not runtime)."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from room_bot.data import DataError, validate_bundle

BASE = 'https://services.arcgis.com/xdsHIIxuCWByZiCB/arcgis/rest/services/LINZ_NZ_Place_Names/FeatureServer/0'
SOURCE = 'https://data.linz.govt.nz/layer/51681-nz-place-names-nzgb/'
WHERE = "feat_type IN ('City','Town','Village','Suburb','Locality')"


def fetch(url, parameters):
    request = Request(url + '?' + urlencode(parameters), headers={'User-Agent': 'MeshCore-RoomBot-data-preparation/1'})
    with urlopen(request, timeout=30) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise DataError('Source page exceeds download limit')
    value = json.loads(raw)
    if 'error' in value:
        raise DataError(f'Source error: {value["error"]}')
    return value


def feature_record(feature):
    row, point = feature['attributes'], feature.get('geometry')
    if not isinstance(point,dict) or 'x' not in point or 'y' not in point:
        raise DataError('Source settlement lacks WGS84 point geometry; do not substitute rounded coordinate attributes')
    longitude, latitude = point['x'], point['y']
    if type(latitude) not in (float,int) or type(longitude) not in (float,int) or not -90<=latitude<=90 or not -180<=longitude<=180:
        raise DataError('Invalid WGS84 geometry coordinates')
    if not -49 <= latitude <= -33:
        return None
    return {'id': f'linz-nzgb:{row["name_id"]}', 'name': row['name'],
            'region': row.get('region') or row.get('land_district') or 'Region not provided',
            'feature_type': row['feat_type'], 'latitude': latitude,
            'longitude': longitude, 'aliases': []}


def acquire():
    metadata = fetch(BASE, {'f': 'json'})
    edit = metadata['editingInfo']['lastEditDate']
    records, offset = [], 0
    while True:
        result = fetch(BASE + '/query', {'f': 'json', 'where': WHERE,
                       'outFields': 'name_id,name,feat_type,region,land_district,crd_latitude,crd_longitude',
                       'returnGeometry': 'true', 'outSR':4326, 'orderByFields': 'name_id',
                       'resultOffset': offset, 'resultRecordCount': 2000})
        features = result.get('features', [])
        if features and result.get('spatialReference',{}).get('wkid') != 4326:
            raise DataError('Source did not return requested WGS84 geometry')
        if not features:
            if result.get('exceededTransferLimit'):
                raise DataError('Source returned an empty incomplete page')
            break
        for feature in features:
            record=feature_record(feature)
            if record is not None:
                records.append(record)
        if len(records) > 20000:
            raise DataError('Settlement subset exceeds import limit')
        offset += len(features)
        if not result.get('exceededTransferLimit'):
            break
    if fetch(BASE, {'f': 'json'})['editingInfo']['lastEditDate'] != edit:
        raise DataError('Source changed during download; retry instead of mixing revisions')
    result = {'format_version': 1, 'dataset': {
        'id': 'linz-nzgb-settlements', 'kind': 'places', 'title': 'Land Information New Zealand / NZGB settlements',
        'source_url': SOURCE, 'license': 'Contains data sourced from the LINZ Data Service and licensed for reuse under CC BY 4.0.',
        'revision': f'ArcGIS lastEditDate {edit}; WGS84 point geometry (not coordinate attributes)',
        'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'coverage': 'City/Town/Village/Suburb/Locality at latitude -49..-33; Gazetteer representative points, not exact user locations or walking routes'},
        'records': records}
    validate_bundle(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    value = acquire()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a reviewed import automatically.
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(f'Prepared {len(value["records"])} LINZ settlement records: {args.output}')


if __name__ == '__main__':
    main()
