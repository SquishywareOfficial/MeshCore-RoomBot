"""Persist input progress and pending replies together before sending over USB."""
from pathlib import Path
import secrets
import math
import sqlite3
import time

from .client import RoomError, U32

class BotState:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables and (tables != {'metadata', 'outbox'} or self.get('schema') != '1'):
            self.db.close()
            raise RoomError('Unsupported bot state database version; preserve this file')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        if not tables:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                self.db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                self.db.execute('CREATE TABLE outbox (ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id INTEGER UNIQUE NOT NULL, text TEXT NOT NULL, anchor TEXT, attempted INTEGER NOT NULL DEFAULT 0)')
                self.set('schema', '1')
                self.db.commit()
            except BaseException:
                self.db.rollback()
                self.db.close()
                raise

    def close(self):
        self.db.close()

    def get(self, key):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, str(value)))

    @property
    def cursor(self):
        value = self.get('cursor')
        if value is None or not value.isdecimal():
            raise RoomError('Missing or corrupt bot cursor; preserve this state file')
        return int(value)

    def bind(self, info, replay=False):
        saved = self.get('room_key')
        if saved is not None and saved != info['room_key']:
            raise RoomError('Different room identity; use a separate state file, do not overwrite existing state')
        if saved is None:
            with self.db:
                self.set('room_key', info['room_key'])
                self.set('cursor', 0 if replay else info['high_water'])
        if self.cursor > info['high_water']:
            raise RoomError('Room archive is behind saved bot cursor; inspect rollback/reset before using a new state file')

    def record(self, seq, replies):
        # All replies and input progress commit together. If sending fails, the
        # outbox survives; next connection drains it before fetching more input.
        with self.db:
            for text in replies:
                for attempt in range(32):
                    request_id = secrets.randbelow(U32) + 1
                    if not self.db.execute('SELECT 1 FROM outbox WHERE id=?', (request_id,)).fetchone():
                        break
                else:
                    raise RoomError('Could not allocate a reply request ID')
                self.db.execute('INSERT INTO outbox (id,text) VALUES (?,?)', (request_id, text))
            self.set('cursor', seq)

    def pending(self):
        return self.db.execute('SELECT id,text,anchor,attempted FROM outbox ORDER BY ordinal').fetchall()

class BotWorker:
    def __init__(self, client, state, handler, prefix='[Bot] ', replay=False, allow_gap=False,
                 reply_interval=5, sleep=time.sleep, monotonic=time.monotonic):
        if not math.isfinite(reply_interval) or reply_interval < 0:
            raise ValueError('Reply interval must be finite and nonnegative')
        self.client, self.state, self.handler = client, state, handler
        self.prefix, self.allow_gap = prefix, allow_gap
        self.interval, self.sleep, self.monotonic = reply_interval, sleep, monotonic
        self.next_send = 0
        self.info = client.info()
        state.bind(self.info, replay)

    def drain(self):
        for request_id, text, anchor, attempted in self.state.pending():
            current = self.client.info()
            self.state.bind(current)
            if attempted and current['oldest'] > int(anchor) + 1:
                raise RoomError('Pending reply outlived deduplication retention; inspect the outbox before any manual retry')
            if not attempted:
                with self.state.db:
                    self.state.db.execute('UPDATE outbox SET anchor=?,attempted=1 WHERE id=?',
                                          (str(current['high_water']), request_id))
            delay = self.next_send - self.monotonic()
            if delay > 0:
                self.sleep(delay)
            self.client.post(request_id, text)
            self.next_send = self.monotonic() + self.interval
            with self.state.db:
                self.state.db.execute('DELETE FROM outbox WHERE id=?', (request_id,))

    def step(self, limit=4):
        self.drain()
        posts, page = self.client.read(self.state.cursor, limit, self.allow_gap)
        for post in posts:
            replies = []
            # Room-authored/system messages include our bot replies; never
            # react to them, even after reconnect or when replay was requested.
            if post.kind == 0 and post.author != self.info['room_key']:
                result = self.handler(post)
                if isinstance(result, str):
                    result = [result]
                if result is not None:
                    if not isinstance(result, (list, tuple)) or len(result) > 4:
                        raise ValueError('Handler must return None, a string or up to four strings')
                    for text in result:
                        if not isinstance(text, str):
                            raise ValueError('Handler replies must be strings')
                        full = self.prefix + text
                        raw = full.encode('utf-8')
                        if not text or b'\0' in raw or len(raw) > 151:
                            raise ValueError('Reply with bot prefix must fit 151 UTF-8 bytes, without NUL')
                        replies.append(full)
            self.state.record(post.sequence, replies)
            self.drain()
        return len(posts), page['more']
