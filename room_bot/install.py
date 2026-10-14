"""Repeatable, staged host setup. Never opens USB or starts a bot/service."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import venv
import zipfile


SOURCE = Path(__file__).resolve().parents[1]
CONTENT = ('references/getready-earthquakes.json', 'references/getready-floods.json',
           'places/linz-settlements-2026-10-13.json', 'routes/hamilton-raglan-2026-10-13.json',
           'tides/raglan-2026.json', 'tides/raglan-2027-jan-prefix.json', 'guides/xiao-enhanced-sq7.json')
GRAPH_NAMES = ('nodes.mem', 'segments.mem', 'ways.mem', 'relations.mem', 'profiles.xml',
               'tagging.xml', 'translations.xml', 'COPYING-Routino', 'manifest.json')
MAP_NAMES = {'streets.sqlite3', 'water.sqlite3', 'MAP-NOTICES.md'} | {
    'routing/nz/' + name for name in GRAPH_NAMES}
ROUTINO = {'url': 'https://www.routino.org/download/routino-3.4.4.tgz', 'bytes': 2518534,
           'sha256': 'a7017ff04313fa4fb53b995de50feda39ea5e7867bb40a3f9c886187af702830'}
PIP = {'url': 'https://files.pythonhosted.org/packages/f3/6e/1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/pip-26.2.1-py3-none-any.whl',
       'bytes': 1816632, 'sha256': '71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e'}


class SetupError(ValueError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def matches(path, record):
    return path.is_file() and path.stat().st_size == record['bytes'] and sha256(path) == record['sha256']


def safe_child(root, name):
    path = PurePosixPath(name)
    if (not name or '\\' in name or path.is_absolute() or '..' in path.parts or
            ':' in name or str(path) != name):
        raise SetupError('Unsafe archive path')
    target = (Path(root) / Path(*path.parts)).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise SetupError('Archive path escapes its staging directory')
    return target


def download(url, path, record, attempts=3):
    """Stream to a private partial file; only verified complete downloads enter cache."""
    path = Path(path)
    if matches(path, record):
        print(f'Using verified cache: {path.name}', flush=True)
        return path
    if urlparse(url).scheme != 'https':
        raise SetupError('Downloads require HTTPS; use --bundle-file for an offline archive')
    path.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(attempts):
        partial = path.with_name(path.name + '.part')
        try:
            print(f'Downloading {path.name} ({record["bytes"] / 1048576:.1f} MiB), attempt {attempt + 1}', flush=True)
            request = Request(url, headers={'User-Agent': 'MeshCore-RoomBot-setup/1'})
            count = 0
            with urlopen(request, timeout=30) as response, partial.open('wb') as output:
                if urlparse(response.geturl()).scheme != 'https':
                    raise SetupError('Download redirected away from HTTPS')
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > record['bytes']:
                        raise SetupError('Download exceeds its pinned size')
                    output.write(block)
            if not matches(partial, record):
                raise SetupError('Download size/checksum mismatch')
            os.replace(partial, path)
            return path
        except (OSError, URLError, SetupError) as exc:
            last = exc
            partial.unlink(missing_ok=True)
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 4))
    raise SetupError(f'Download failed: {path.name}: {last}')


def load_manifest(path):
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise SetupError('Data manifest too large')
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get('version') != 1 or
            not isinstance(manifest.get('id'), str) or
            not re.fullmatch(r'nz-[a-z0-9-]{1,64}', manifest['id']) or
            not isinstance(manifest.get('files'), dict) or
            not isinstance(manifest.get('archive'), dict) or
            set(manifest.get('files', {})) != MAP_NAMES):
        raise SetupError('Unsupported NZ data manifest')
    for record in [manifest['archive'], *manifest['files'].values()]:
        if (not isinstance(record, dict) or type(record.get('bytes')) is not int or not 0 < record['bytes'] <= 2 * 1024 ** 3 or
                not isinstance(record.get('sha256'), str) or
                not re.fullmatch(r'[0-9a-f]{64}', record.get('sha256', ''))):
            raise SetupError('Invalid pinned asset size/hash')
    if sum(r['bytes'] for r in manifest['files'].values()) > 4 * 1024 ** 3:
        raise SetupError('Data bundle exceeds supported size')
    return manifest


def unpack_bundle(archive, destination, manifest):
    if not matches(Path(archive), manifest['archive']):
        raise SetupError('Data archive checksum mismatch')
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) != len(manifest['files']) or {e.filename for e in entries} != set(manifest['files']):
            raise SetupError('Archive contains missing, duplicate or unexpected files')
        for entry in entries:
            record = manifest['files'][entry.filename]
            if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.file_size != record['bytes']:
                raise SetupError('Invalid archive entry')
            target = safe_child(destination, entry.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            print(f'Unpacking {entry.filename}', flush=True)
            with bundle.open(entry) as source, target.open('xb') as output:
                count = 0
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    count += len(block)
                    if count > record['bytes']:
                        raise SetupError('Expanded asset exceeds pinned size')
                    output.write(block)
            if not matches(target, record):
                raise SetupError(f'Expanded asset checksum mismatch: {entry.filename}')


def unpack_routino(archive, stage):
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        if len(members) > 3000 or sum(m.size for m in members) > 80 * 1024 ** 2:
            raise SetupError('Routino source archive exceeds bounds')
        links = []
        for member in members:
            name = member.name.rstrip('/')
            if not name.startswith('routino-3.4.4/') and name != 'routino-3.4.4':
                raise SetupError('Unexpected Routino source directory')
            target = safe_child(stage, name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as input_file, target.open('xb') as output:
                    shutil.copyfileobj(input_file, output, 1024 * 1024)
                target.chmod(member.mode & 0o777)
            elif member.issym():
                link = PurePosixPath(member.linkname)
                resolved = (target.parent/Path(*link.parts)).resolve()
                if (link.is_absolute() or '\\' in member.linkname or ':' in member.linkname or
                        not resolved.is_relative_to((Path(stage)/'routino-3.4.4').resolve())):
                    raise SetupError('Source symlink escapes Routino directory')
                links.append((target, member.linkname, resolved))
            else:
                raise SetupError('Hard links/special files are not accepted in source archives')
        for target, link, resolved in links:
            if not resolved.exists():
                raise SetupError('Source symlink target missing')
            target.symlink_to(link, target_is_directory=resolved.is_dir())
    return Path(stage)/'routino-3.4.4'


def run(command, cwd=None, log=None):
    print('Running: ' + ' '.join(map(str, command)), flush=True)
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    if log:
        with Path(log).open('w', encoding='utf-8') as output:
            result = subprocess.run(list(map(str, command)), cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
        if result.returncode:
            raise SetupError(f'Command failed; see {log}')
    else:
        subprocess.run(list(map(str, command)), cwd=cwd, env=env, check=True)


def setup_python(root, source, profile):
    env = root/'venv'
    python = env/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not python.exists():
        if env.exists():
            raise SetupError('Existing venv is incomplete; preserve it and choose a new setup root')
        # Bootstrap with a verified wheel: works when Linux has venv but lacks ensurepip.
        venv.EnvBuilder(with_pip=False).create(env)
    if subprocess.run([str(python), '-m', 'pip', '--version'], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        wheel = download(PIP['url'], root/'cache/pip-26.2.1-py3-none-any.whl', PIP)
        bootstrap = 'import sys; sys.path.insert(0,sys.argv.pop(1)); from pip._internal.cli.main import main; raise SystemExit(main())'
        run([python, '-c', bootstrap, wheel, 'install', '--no-index', wheel])
    requirements = [source/'requirements.txt', source/'requirements-astronomy.txt']
    if profile == 'nz':
        requirements.append(source/'requirements-water.txt')
    command = [python, '-m', 'pip', 'install']
    for path in requirements:
        command += ['-r', path]
    run(command, log=root/'python-install.log')
    run([python, '-m', 'pip', 'check'])
    return python


def build_router(root, stage, jobs):
    if not shutil.which('make') or not shutil.which('cc'):
        raise SetupError('Routino needs make/C compiler. On Debian/Pi OS: sudo apt install build-essential libz-dev libbz2-dev')
    destination = root/'tools/routino-3.4.4'
    executable = destination/'src/router-slim'
    receipt = destination/'setup-source.json'
    if executable.is_file() and receipt.is_file():
        record = json.loads(receipt.read_text())
        if record.get('source_sha256') == ROUTINO['sha256'] and record.get('binary_sha256') == sha256(executable):
            return executable
    if destination.exists():
        raise SetupError('Existing Routino tree is unmanaged/incomplete; preserve it and choose a new setup root')
    archive = download(ROUTINO['url'], root/'cache/routino-3.4.4.tgz', ROUTINO)
    tree = unpack_routino(archive, stage/'native')
    run(['make', '-C', tree/'src', f'-j{jobs}', 'router-slim'], log=root/'routino-build.log')
    if not (tree/'src/router-slim').is_file():
        raise SetupError('Native router not produced')
    (tree/'setup-source.json').write_text(json.dumps({'source_sha256': ROUTINO['sha256'],
                                                     'binary_sha256': sha256(tree/'src/router-slim')}))
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tree, destination)
    return executable


def app_files(source):
    return sorted([*source.glob('room_bot/*.py'), *source.glob('requirements*.txt'), source/'LICENSE'])


def import_content(python, app, target, source):
    for name in CONTENT:
        run([python, '-m', 'room_bot.data', '--data-dir', target, 'import', source/'datasets'/name], cwd=app)


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    os.replace(temporary, path)


def write_launcher(root):
    path = root/'run_bot.py'
    marker = '# Managed MeshCore-RoomBot launcher v1'
    if path.exists() and not path.read_text(encoding='utf-8').startswith(marker):
        raise SetupError('Existing run_bot.py is not installer-managed; it was preserved')
    raw = marker + '''
from pathlib import Path
import json, os, subprocess, sys
root = Path(__file__).resolve().parent
receipt = json.loads((root/'installation.json').read_text(encoding='utf-8'))
python = root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
command = [str(python), '-m', 'room_bot', '--config', str(root/'config.json'),
           '--state', str(root/'state.sqlite3'), '--mode', 'bot', '--name', 'SquishyBot']
env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
raise SystemExit(subprocess.call(command + sys.argv[1:], cwd=root/receipt['app'], env=env))
'''
    path.write_text(raw, encoding='utf-8')


def install(args, source=SOURCE):
    if sys.version_info < (3, 10):
        raise SetupError('Python 3.10 or newer is required')
    root = args.root.expanduser().resolve()
    if root == source.resolve():
        raise SetupError('Choose an installation directory separate from the checkout')
    manifest = load_manifest(args.manifest) if args.profile == 'nz' else None
    if manifest and (os.name != 'posix' or sys.byteorder != 'little' or sys.maxsize <= 2**32):
        raise SetupError('Full NZ setup requires 64-bit little-endian Linux (Pi OS 64-bit or WSL). Basic setup supports Windows.')
    if manifest and not args.bundle_file and not (args.bundle_url or manifest['archive'].get('url')):
        raise SetupError('NZ bundle is not published yet. Supply --bundle-file or --bundle-url with this pinned manifest.')
    if manifest and args.bundle_file and not matches(args.bundle_file.expanduser().resolve(), manifest['archive']):
        raise SetupError('Supplied data archive size/checksum mismatch')
    root.mkdir(parents=True, exist_ok=True)
    lock = root/'.setup.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SetupError(f'Setup is locked: {lock}. If an earlier setup was interrupted, confirm it has stopped before removing that lock.') from exc
    os.close(fd)
    stage = Path(tempfile.mkdtemp(prefix='.setup-', dir=root))
    try:
        needed = sum(r['bytes'] for r in manifest['files'].values()) * 2 + manifest['archive']['bytes'] if manifest else 0
        if shutil.disk_usage(root).free < needed + 512 * 1024 ** 2:
            raise SetupError('Insufficient free space for dependencies and staged data')
        files = app_files(source)
        identity = hashlib.sha256(''.join(str(p.relative_to(source))+sha256(p) for p in files).encode()).hexdigest()[:16]
        app = root/'app'/identity
        if app.exists() and any(not (app/p.relative_to(source)).is_file() or
                                sha256(app/p.relative_to(source)) != sha256(p) for p in files):
            raise SetupError('Installed application snapshot has changed; it was preserved')
        if not app.exists():
            temporary_app = stage/'app'
            for path in files:
                target = temporary_app/path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            app.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_app, app)
        python = setup_python(root, source, args.profile)
        content_id = hashlib.sha256(''.join(sha256(source/'datasets'/name) for name in CONTENT).encode()).hexdigest()[:16]
        data_id = (manifest['id'] if manifest else 'basic') + '-' + content_id
        target = root/'data'/data_id
        staged_data = stage/'data'
        completed = target/'setup-data.json'
        if target.exists() and not completed.is_file():
            raise SetupError('Existing target dataset is incomplete/unmanaged; it was preserved')
        if not target.exists():
            staged_data.mkdir()
            if manifest:
                if args.bundle_file:
                    archive = args.bundle_file.expanduser().resolve()
                else:
                    archive = download(args.bundle_url or manifest['archive']['url'],
                                       root/'cache'/f'{manifest["id"]}.zip', manifest['archive'])
                unpack_bundle(archive, staged_data, manifest)
            import_content(python, app, staged_data, source)
            atomic_json(staged_data/'setup-data.json', {'id': data_id, 'archive_sha256': manifest['archive']['sha256'] if manifest else None})
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_data, target)
        if manifest:
            saved = json.loads(completed.read_text())
            if saved['archive_sha256'] != manifest['archive']['sha256']:
                raise SetupError('A different bundle uses this data ID; choose a new version, do not overwrite')
            for name, record in manifest['files'].items():
                if not matches(safe_child(target, name), record):
                    raise SetupError('An installed NZ asset has changed; it was preserved')
        run([python, '-m', 'room_bot.data', '--data-dir', target, 'list'], cwd=app,
            log=root/'setup-content-check.log')
        executable = build_router(root, stage, args.jobs) if manifest else None
        commands = ['ping', 'help', 'about', 'place', 'towns', 'distance', 'bearing', 'walktime', 'details',
                    'source', 'directory', 'emergency', 'guide', 'sun', 'tides', 'calc', 'notice', 'checkin', 'lastseen']
        if manifest:
            commands.append('water')
        config = {'version': 1, 'enabled_commands': commands, 'data_dir': str(target)}
        if executable:
            config['routing'] = {'backend': 'routino', 'graph_dir': str(target/'routing/nz'), 'executable': str(executable)}
        proposed = stage/'config.json'
        atomic_json(proposed, config)
        run([python, '-m', 'room_bot', '--config', proposed, '--check-config'], cwd=app, log=root/'setup-config-check.log')
        if manifest:
            run([python, '-m', 'room_bot.routing', '--graph', target/'routing/nz', '--executable', executable], cwd=app)
            check_indexes = 'from room_bot.streets import StreetStore; from room_bot.water import WaterStore; import sys; StreetStore(sys.argv[1]).close(); WaterStore(sys.argv[2]).close()'
            run([python, '-c', check_indexes, target/'streets.sqlite3', target/'water.sqlite3'], cwd=app)
        config_path = root/'config.json'
        if not config_path.exists():
            os.replace(proposed, config_path)
        else:
            proposal = root/f'config.{identity}.{data_id}.proposed.json'
            os.replace(proposed, proposal)
            print(f'Existing config preserved. Review/merge proposed settings in {proposal}', flush=True)
        # No cursor/outbox/community/config data is copied from the preparation machine.
        write_launcher(root)
        atomic_json(root/'installation.json', {'version': 1, 'profile': args.profile, 'app': str(app.relative_to(root)),
                                               'data': str(target), 'prepared_at': datetime.now(timezone.utc).isoformat()})
        print(f'Setup verified. Start explicitly: python "{root / "run_bot.py"}" --port YOUR_SERIAL_PORT', flush=True)
        print('Add --clock-trusted only after checking the host clock. No USB/service was started.', flush=True)
    finally:
        if stage.resolve().is_relative_to(root) and stage.name.startswith('.setup-'):
            shutil.rmtree(stage)
        lock.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home()/'.meshcore-room-bot')
    parser.add_argument('--profile', choices=('basic', 'nz'), default='basic')
    parser.add_argument('--manifest', type=Path, default=SOURCE/'installer/nz-offline-data.json')
    parser.add_argument('--bundle-file', type=Path, help='Previously downloaded archive for offline transfer')
    parser.add_argument('--bundle-url', help='HTTPS mirror; must match the pinned manifest')
    parser.add_argument('--jobs', type=int, choices=range(1, 9), default=1, help='Native compile jobs (1 for Pi Zero)')
    args = parser.parse_args(argv)
    try:
        install(args)
        return 0
    except (SetupError, OSError, ValueError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile, tarfile.TarError) as exc:
        print(f'Setup stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
