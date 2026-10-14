"""One bounded read from a permitted official LINZ tide source."""
import json
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def allowed(url):
    p = urlparse(url)
    return p.scheme == 'https' and not p.username and not p.password and not p.query and (
        p.netloc == 'static.charts.linz.govt.nz' and p.path.startswith('/tide-tables/maj-ports/csv/') and p.path.endswith('.csv') or
        p.netloc == 'www.linz.govt.nz' and (p.path == '/api/v1/tp' or
        p.path == '/products-services/tides-and-tidal-streams/tide-predictions' or
        p.path.startswith('/sites/default/files/') and p.path.endswith('.csv')))


def main():
    r = json.loads(sys.stdin.buffer.read(4097))
    if not allowed(r['url']) or not .5 <= r['timeout'] <= 30:
        raise ValueError('Invalid tide request')
    limit = r['limit']
    if type(limit) is not int or not 1000 <= limit <= 400000:
        raise ValueError('Invalid response bound')
    with urlopen(Request(r['url'],headers={'User-Agent':'MeshCore-RoomBot-tides/1'}),timeout=r['timeout']) as response:
        if not allowed(response.geturl()):raise ValueError('Invalid redirect')
        raw = response.read(limit+1)
    if not raw or len(raw) > limit:raise ValueError('Invalid response size')
    sys.stdout.buffer.write(raw)


if __name__ == '__main__':
    try:main()
    except Exception:
        sys.stderr.write('Official tide source unavailable\n');raise SystemExit(1)
