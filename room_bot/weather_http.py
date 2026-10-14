"""Bounded Open-Meteo HTTP helper, isolated so the parent can kill a stalled request."""
import json
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ENDPOINTS = {'geocode': 'https://geocoding-api.open-meteo.com/v1/search',
             'forecast': 'https://api.open-meteo.com/v1/forecast'}
MAX_BYTES = 131072


def main():
    request = json.loads(sys.stdin.buffer.read(4097))
    endpoint = ENDPOINTS[request['endpoint']]
    timeout = request['timeout']
    if type(timeout) not in (float, int) or not 1 <= timeout <= 15:
        raise ValueError('Invalid timeout')
    url = endpoint + '?' + urlencode(request['parameters'])
    with urlopen(Request(url, headers={'User-Agent': 'MeshCore-RoomBot-weather/1'}), timeout=timeout) as response:
        if not response.geturl().startswith(endpoint + '?'):
            raise ValueError('Unexpected provider redirect')
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Provider response too large')
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get('error'):
        raise ValueError('Provider returned an error')
    sys.stdout.buffer.write(raw)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Raw provider errors/URLs do not belong in the room or public logs.
        sys.stderr.write('Weather provider request failed\n')
        raise SystemExit(1)
