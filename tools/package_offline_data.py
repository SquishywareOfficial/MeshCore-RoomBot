"""Prepare a distributable NZ map archive from verified, immutable snapshots."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from room_bot.config import RoutingSettings
from room_bot.install import GRAPH_NAMES, sha256
from room_bot.routing import RoutinoRouter
from room_bot.streets import StreetStore
from room_bot.water import WaterStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', type=Path, required=True)
    parser.add_argument('--streets', type=Path, required=True)
    parser.add_argument('--water', type=Path, required=True)
    parser.add_argument('--router', required=True)
    parser.add_argument('--output', type=Path, required=True, help='New output directory')
    parser.add_argument('--id', default='nz-20261011-v2')
    parser.add_argument('--url', default=None, help='Published HTTPS asset URL, if available')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    settings = RoutingSettings(backend='routino', graph_dir=str(args.graph.resolve()), executable=args.router)
    graph = RoutinoRouter(settings, args.output).verify()
    streets, water = StreetStore(args.streets), WaterStore(args.water)
    try:
        metadata = {'streets': dict(streets.metadata), 'water': dict(water.metadata), 'routing': graph}
        source_hash = graph['source_sha256']
        for name in ('streets', 'water'):
            if metadata[name].get('source_sha256') != source_hash:
                raise ValueError('Graph/street/water sources differ; do not package a mixed snapshot')
    finally:
        streets.close(); water.close()
    # Whitelist only public map datasets and necessary graph/license files.
    files = {'streets.sqlite3': args.streets, 'water.sqlite3': args.water}
    files.update({'routing/nz/'+name: args.graph/name for name in GRAPH_NAMES if name != 'manifest.json'})
    public_graph = dict(graph)
    public_graph['build_command'] = ['See tools/build_routing_graph.py and the retained source revision/profile']
    metadata['routing'] = public_graph
    manifest_file = args.output/'public-graph-manifest.json'
    manifest_file.write_text(json.dumps(public_graph, indent=2)+'\n', encoding='utf-8')
    files['routing/nz/manifest.json'] = manifest_file
    notices = args.output/'MAP-NOTICES.md'
    notices.write_text('''# New Zealand offline map data

Map data: OpenStreetMap contributors, Open Database License (ODbL) 1.0.
https://www.openstreetmap.org/copyright
https://opendatacommons.org/licenses/odbl/1-0/
This archive contains OSM-derived databases, separate from the MIT bot code.
Preserve attribution, source revision and ODbL database obligations on reuse.

Routino-derived XML: AGPL-3.0-or-later; original notices and COPYING-Routino
are included. The installer downloads the unmodified Routino 3.4.4 source,
preserves it and compiles the native routing executable on the target.
https://www.routino.org/download/routino-3.4.4.tgz

These maps do not establish present access, potable water, open services or
safe walking routes. See the bot's water/origins/routing documentation.
Pinned input: '''+graph['source_url']+'\nInput SHA256: '+source_hash+'\n', encoding='utf-8')
    files['MAP-NOTICES.md'] = notices
    records = {}
    archive = args.output/(args.id+'.zip')
    partial = archive.with_suffix('.zip.part')
    with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
        for name, path in sorted(files.items()):
            print('Packaging ' + name, flush=True)
            records[name] = {'bytes': path.stat().st_size, 'sha256': sha256(path)}
            bundle.write(path, name)
    partial.rename(archive)
    descriptor = {'version': 1, 'id': args.id, 'prepared_at': datetime.now(timezone.utc).isoformat(),
                  'platform': '64-bit little-endian Linux; graph tested on x86_64 and aarch64',
                  'archive': {'url': args.url, 'bytes': archive.stat().st_size, 'sha256': sha256(archive)},
                  'files': records, 'sources': metadata}
    path = args.output/'nz-offline-data.json'
    path.write_text(json.dumps(descriptor, indent=2)+'\n', encoding='utf-8')
    from room_bot.install import load_manifest
    load_manifest(path)
    print(json.dumps({'archive': str(archive), 'manifest': str(path), 'bytes': archive.stat().st_size,
                      'sha256': descriptor['archive']['sha256']}, indent=2), flush=True)


if __name__ == '__main__':
    main()
