"""Explicit, offline preparation of an immutable pedestrian Routino snapshot.

Run on Linux with a matching Routino source/build and an already downloaded
national OSM PBF. No downloads or routing service calls happen in this tool.
The derived upstream XML files retain their AGPL notices inside the snapshot.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET


PROFILE = 'nz-foot-v1'
GRAPH_FILES = ('nodes.mem', 'segments.mem', 'ways.mem', 'relations.mem')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def prepare_xml(source, output):
    # Keep the original copyright/license comments, not just parsed elements.
    raw = (source / 'xml/routino-profiles.xml').read_text()
    root = ET.fromstring(raw)
    foot = root.find("./profile[@transport='foot']")
    foot.set('name', PROFILE)
    for item in foot.findall('./preferences/preference'):
        item.set('percent', '0' if item.get('highway') in ('motorway', 'ferry') else '100')
    for item in foot.findall('./properties/property'):
        item.set('percent', '50')
    # Copy the license comment verbatim from the upstream file.
    header = raw[:raw.index('<routino-profiles')]
    (output / 'profiles.xml').write_text(header + '<routino-profiles>\n' +
        ET.tostring(foot, encoding='unicode') + '\n</routino-profiles>\n', encoding='utf-8')
    tags = (source / 'xml/routino-tagging.xml').read_text()
    # Stock rules already resolve specific foot access and physical barriers.
    # Do not attempt conditional access evaluation without the required context.
    extra = '\n<!-- MeshCore-RoomBot pedestrian-only, conservative conditional-access exclusions. -->\n'
    for conditional in ('access:conditional', 'foot:conditional'):
        extra += f'<if k="{conditional}"><output k="foot" v="no"/></if>\n'
    for mode in ('horse', 'wheelchair', 'bicycle', 'moped', 'motorcycle', 'motorcar', 'goods', 'hgv', 'psv'):
        extra += f'<if><output k="{mode}" v="no"/></if>\n'
    for section in ('node', 'way'):
        tags = tags.replace(f'</{section}>', extra + f'</{section}>')
    (output / 'tagging.xml').write_text(tags, encoding='utf-8')
    ET.parse(output / 'tagging.xml')
    shutil.copyfile(source / 'agpl-3.0.txt', output / 'COPYING-Routino')
    shutil.copyfile(source / 'xml/routino-translations.xml', output / 'translations.xml')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Built Routino source tree')
    parser.add_argument('--input', type=Path, required=True, help='Downloaded .osm.pbf or test .osm')
    parser.add_argument('--output', type=Path, required=True, help='New snapshot directory; must not exist')
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--coverage', default='New Zealand, Geofabrik national extract; mapped pedestrian network')
    parser.add_argument('--sort-ram-mb', type=int, default=1024)
    args = parser.parse_args()
    if not 64 <= args.sort_ram_mb <= 8192:
        parser.error('sort-ram-mb must be 64..8192')
    source, input_path, output = args.source.resolve(), args.input.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    prepare_xml(source, output)
    version = subprocess.check_output([str(source / 'src/router'), '--version'], stderr=subprocess.STDOUT, text=True).strip()
    command = [str(source / 'src/planetsplitter'), f'--dir={output}',
               f'--tagging={output / "tagging.xml"}', f'--sort-ram-size={args.sort_ram_mb}',
               '--sort-threads=2', '--prune-isolated=0', '--prune-short=0',
               '--loggable', '--logtime', '--logmemory', str(input_path)]
    with (output / 'build.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    files = {name: {'sha256': sha256(output / name), 'bytes': (output / name).stat().st_size}
             for name in GRAPH_FILES + ('profiles.xml', 'tagging.xml', 'translations.xml')}
    manifest = {'version': 1, 'engine': 'routino', 'engine_version': version,
                'profile': PROFILE, 'revision': args.revision, 'coverage': args.coverage,
                'source_url': args.source_url, 'source_sha256': sha256(input_path),
                'license': 'OpenStreetMap contributors, ODbL 1.0; https://www.openstreetmap.org/copyright',
                'profile_license': 'Derived Routino XML: AGPL-3.0-or-later; see COPYING-Routino and XML notices',
                'prepared_at': datetime.now(timezone.utc).isoformat(),
                'build_command': command, 'files': files,
                'limitations': 'Mapped pedestrian estimates, not verified access/safety; no ferries or motorways; conditional access excluded.'}
    # A manifest marks completion; interrupted snapshots must not be enabled.
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'snapshot': str(output), 'graph_bytes': sum(files[n]['bytes'] for n in GRAPH_FILES),
                      'engine_version': version, 'revision': args.revision}, indent=2))


if __name__ == '__main__':
    main()
