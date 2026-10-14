"""Bounded, ASCII USB protocol with lossless message bytes and checked receipts."""
from dataclasses import dataclass
import re
import threading
import time

U64 = (1 << 64) - 1
U32 = (1 << 32) - 1

class RoomError(Exception):
    pass

class ProtocolError(RoomError):
    pass

class HistoryGap(RoomError):
    pass

def sequence(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,20}', value) or int(value) > U64:
        raise ProtocolError('Invalid sequence field')
    return int(value)

def number(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProtocolError('Invalid numeric field')
    return value

def key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise ProtocolError('Invalid public key')
    return value.lower()

@dataclass(frozen=True)
class Post:
    sequence: int
    timestamp: int
    sender_timestamp: int
    author: str
    kind: int
    raw_text: bytes

    @property
    def text(self):
        return self.raw_text.decode('utf-8', errors='replace')

    @classmethod
    def parse(cls, row):
        text = row.get('text_hex')
        if not isinstance(text, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}){1,151}', text):
            raise ProtocolError('Invalid post text')
        raw = bytes.fromhex(text)
        if b'\0' in raw:
            raise ProtocolError('NUL in post text')
        seq = sequence(row.get('sequence'))
        if not seq:
            raise ProtocolError('Zero post sequence')
        return cls(seq, number(row.get('timestamp'), U32), number(row.get('sender_timestamp'), U32),
                   key(row.get('author')), number(row.get('kind'), 1), raw)

class SerialTransport:
    """One owner of the USB port; commands never run concurrently."""
    def __init__(self, port, timeout=5):
        import serial  # Only the real transport needs pyserial.
        import os
        self.timeout = timeout
        self.lock = threading.Lock()
        self.port = serial.Serial(port=None, baudrate=115200, timeout=0.1, write_timeout=timeout,
                                  **({'exclusive': True} if os.name == 'posix' else {}))
        self.port.dtr = True
        self.port.rts = False
        self.port.port = port
        self.port.open()

    def close(self):
        self.port.close()

    def exchange(self, command, terminal):
        import json
        if not command.startswith('bot.') or '\n' in command or '\r' in command or '\0' in command or len(command) > 383:
            raise ValueError('Invalid USB command')
        with self.lock:
            self.port.reset_input_buffer()
            self.port.write((command + '\r\n').encode('ascii'))
            self.port.flush()
            deadline = time.monotonic() + self.timeout
            rows, pending = [], bytearray()
            while time.monotonic() < deadline:
                pending.extend(self.port.read(self.port.in_waiting or 1))
                if len(pending) > 8192:
                    raise ProtocolError('USB line too long')
                while b'\n' in pending:
                    line, _, tail = pending.partition(b'\n')
                    pending = bytearray(tail)
                    # CR-separated command echo or boot diagnostics may precede a response.
                    line = line.rstrip(b'\r').split(b'\r')[-1]
                    if not line.startswith(b'@bot '):
                        continue
                    if len(line) > 640:
                        raise ProtocolError('Bot response exceeds limit')
                    try:
                        row = json.loads(line[5:].decode('ascii'))
                    except (ValueError, UnicodeError) as exc:
                        raise ProtocolError('Malformed bot JSON') from exc
                    if not isinstance(row, dict):
                        raise ProtocolError('Bot response is not an object')
                    if row.get('type') == 'error':
                        raise RoomError(str(row.get('error', 'Unknown room error')))
                    rows.append(row)
                    if len(rows) > 9:
                        raise ProtocolError('Too many response records')
                    if row.get('type') == terminal:
                        return rows
            raise TimeoutError('Room USB response timed out; keep pending replies for retry')

class RoomClient:
    def __init__(self, transport):
        self.transport = transport

    def info(self):
        rows = self.transport.exchange('bot.info', 'info')
        if len(rows) != 1 or rows[0].get('type') != 'info':
            raise ProtocolError('Expected one room info response')
        row = rows[0]
        if type(row.get('api')) is not int or row.get('api') != 1 or row.get('max_page') != 8 or row.get('max_text_bytes') != 151:
            raise ProtocolError('Unsupported bot API; install the Enhanced USB bot firmware')
        result = dict(row, room_key=key(row.get('room_key')))
        for field in ('oldest', 'latest', 'high_water'):
            result[field] = sequence(row.get(field))
        result['count'] = number(row.get('count'), 2000)
        if not result['oldest'] <= result['latest'] <= result['high_water']:
            raise ProtocolError('Invalid archive bounds')
        if bool(result['count']) != bool(result['oldest']):
            raise ProtocolError('Invalid archive count')
        return result

    def read(self, after, limit=4, allow_gap=False):
        number(after, U64)
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError('Page limit must be 1..8')
        rows = self.transport.exchange(f'bot.read {after} {limit}', 'end')
        if not rows or rows[-1].get('type') != 'end' or any(row.get('type') != 'post' for row in rows[:-1]):
            raise ProtocolError('Incomplete or mixed history page')
        end = rows[-1]
        posts = [Post.parse(row) for row in rows[:-1]]
        if sequence(end.get('after')) != after or number(end.get('count'), 8) != len(posts) or len(posts) > limit:
            raise ProtocolError('History page does not match request')
        latest, oldest, high = (sequence(end.get(field)) for field in ('latest', 'oldest', 'high_water'))
        if not oldest <= latest <= high or after > high or type(end.get('gap')) is not bool or type(end.get('more')) is not bool:
            raise ProtocolError('Invalid page bounds')
        previous = after
        for post in posts:
            if not previous < post.sequence <= latest:
                raise ProtocolError('Unordered history page')
            previous = post.sequence
        if sequence(end.get('next')) != previous or end['more'] != (latest > previous):
            raise ProtocolError('Invalid page continuation')
        if end['more'] and not posts:
            raise ProtocolError('Empty page cannot claim more unread posts')
        if end['gap'] != bool(oldest and after < oldest - 1):
            raise ProtocolError('Invalid retention-gap flag')
        if end['gap'] and not allow_gap:
            raise HistoryGap('Saved cursor predates retained history; use --allow-gap only after reviewing the loss')
        return posts, dict(end, next=previous, oldest=oldest, latest=latest, high_water=high)

    def post(self, request_id, text):
        number(request_id, U32)
        if not request_id:
            raise ValueError('Request ID must be nonzero')
        if not isinstance(text, str):
            raise ValueError('Reply must be a string')
        raw = text.encode('utf-8')
        if not raw or b'\0' in raw or len(raw) > 151:
            raise ValueError('Reply must be 1..151 UTF-8 bytes without NUL')
        rows = self.transport.exchange(f'bot.post {request_id} {raw.hex()}', 'posted')
        if len(rows) != 1 or rows[0].get('type') != 'posted':
            raise ProtocolError('Expected one post receipt')
        row = rows[0]
        if sequence(row.get('request_id')) != request_id or not sequence(row.get('sequence')) or type(row.get('duplicate')) is not bool:
            raise ProtocolError('Receipt does not match pending reply')
        number(row.get('timestamp'), U32)
        return row
