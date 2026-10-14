import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from room_bot import RoomClient, RoomError, HistoryGap, ProtocolError, BotState, BotWorker, SerialTransport
from room_bot.__main__ import ping

ROOM = '01' + '00' * 31
USER = '02' + '00' * 31

class FakeUSB:
    def __init__(self):
        self.key = ROOM
        self.posts, self.requests, self.calls = [], {}, []
        self.high = 0
        self.lose_receipt = False
        self.post_error = None
        self.transform = lambda rows: rows

    def add(self, text, author=USER, kind=0):
        self.high += 1
        self.posts.append(dict(type='post', sequence=str(self.high), timestamp=1700000000 + self.high,
                               sender_timestamp=1, author=author, kind=kind, text_hex=text.encode().hex()))

    def exchange(self, command, terminal):
        self.calls.append(command)
        args = command.split()
        if args[0] == 'bot.info':
            rows = [dict(type='info', api=1, room_key=self.key, count=len(self.posts),
                         oldest=self.posts[0]['sequence'] if self.posts else '0',
                         latest=self.posts[-1]['sequence'] if self.posts else '0', high_water=str(self.high),
                         max_page=8, max_text_bytes=151)]
        elif args[0] == 'bot.read':
            after, limit = map(int, args[1:])
            if after > self.high:
                raise RoomError('cursor ahead of archive')
            rows = [dict(p) for p in self.posts if int(p['sequence']) > after][:limit]
            oldest = int(self.posts[0]['sequence']) if self.posts else 0
            next_seq = int(rows[-1]['sequence']) if rows else after
            rows.append(dict(type='end', after=str(after), next=str(next_seq), oldest=str(oldest),
                             latest=str(self.high), high_water=str(self.high), count=len(rows),
                             gap=bool(oldest and after < oldest - 1), more=self.high > next_seq))
        elif args[0] == 'bot.post':
            if self.post_error:
                raise RoomError(self.post_error)
            request_id, text = int(args[1]), bytes.fromhex(args[2]).decode()
            duplicate = request_id in self.requests
            if duplicate and self.requests[request_id][0] != text:
                raise RoomError('submission conflict')
            if not duplicate:
                self.add(text, ROOM)
                self.requests[request_id] = (text, self.high)
            if self.lose_receipt:
                self.lose_receipt = False
                raise TimeoutError('Receipt lost after committed reply')
            rows = [dict(type='posted', request_id=str(request_id), sequence=str(self.requests[request_id][1]),
                         timestamp=1700000000 + self.requests[request_id][1], duplicate=duplicate)]
        else:
            raise AssertionError(command)
        return self.transform(rows)

class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite3'
        self.transport = FakeUSB()
        self.client = RoomClient(self.transport)
        self.state = BotState(self.path)
        self.handled = []

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def worker(self, handler=None, **kwargs):
        return BotWorker(self.client, self.state, handler or (lambda post: self.handled.append(post.text)),
                         reply_interval=0, **kwargs)

    def test_first_start_is_future_only_and_restart_resumes_cursor(self):
        self.transport.add('old')
        self.worker().step()
        self.assertEqual([], self.handled)
        self.transport.add('new')
        self.worker().step()
        self.assertEqual(['new'], self.handled)
        self.state.close(); self.state = BotState(self.path)
        self.worker().step()
        self.assertEqual(['new'], self.handled)
        self.assertEqual(2, self.state.cursor)

    def test_explicit_replay_processes_only_user_posts_and_never_reacts_to_own_replies(self):
        self.transport.add('/bot ping')
        self.transport.add('system', ROOM, 1)
        self.transport.add('/bot ping', ROOM)
        worker = self.worker(ping, replay=True)
        worker.step(); worker.step(); worker.step()
        replies = [p for p in self.transport.posts if bytes.fromhex(p['text_hex']).decode() == '[Bot] pong']
        self.assertEqual(1, len(replies))
        self.assertEqual(4, self.state.cursor)

    def test_lost_receipt_after_commit_retries_same_id_without_duplicate_across_restart(self):
        worker = self.worker(ping)
        self.transport.add('/bot ping'); self.transport.lose_receipt = True
        with self.assertRaises(TimeoutError):
            worker.step()
        self.assertEqual(1, self.state.cursor)
        pending = self.state.pending()
        self.assertEqual(1, len(pending))
        self.assertEqual(1, pending[0][3])
        self.state.close(); self.state = BotState(self.path)
        self.worker(ping).step()
        self.assertEqual([], self.state.pending())
        self.assertEqual(2, len(self.transport.posts))
        commands = [c for c in self.transport.calls if c.startswith('bot.post')]
        self.assertEqual(commands[0], commands[1])

    def test_callback_failure_and_oversized_utf8_reply_never_advance_cursor(self):
        def broken(post):
            raise RuntimeError('Handler failed')
        worker = self.worker(broken)
        self.transport.add('input')
        with self.assertRaises(RuntimeError):
            worker.step()
        self.assertEqual(0, self.state.cursor)
        with self.assertRaises(ValueError):
            self.worker(lambda post: 'é' * 80).step()
        self.assertEqual(0, self.state.cursor)
        self.assertEqual([], self.state.pending())

    def test_storage_failure_preserves_outbox_and_stops_more_inputs(self):
        worker = self.worker(ping)
        self.transport.add('/bot ping'); self.transport.add('second')
        self.transport.post_error = 'storage full'
        with self.assertRaises(RoomError):
            worker.step()
        self.assertEqual(1, self.state.cursor)
        self.assertEqual(1, len(self.state.pending()))
        self.transport.post_error = None
        self.worker(ping).step()
        self.assertEqual([], self.state.pending())

    def test_identity_mismatch_and_archive_rollback_preserve_state(self):
        self.transport.add('old'); self.worker()
        self.transport.key = USER
        with self.assertRaises(RoomError):
            self.worker()
        self.assertEqual(ROOM, self.state.get('room_key'))
        self.transport.key = ROOM
        self.transport.posts = []; self.transport.high = 0
        with self.assertRaises(RoomError):
            self.worker()
        self.assertEqual(1, self.state.cursor)

    def test_retention_gap_requires_explicit_opt_in(self):
        worker = self.worker()
        self.transport.high = 100; self.transport.add('newest')
        with self.assertRaises(HistoryGap):
            worker.step()
        self.assertEqual(0, self.state.cursor)
        self.worker(allow_gap=True).step()
        self.assertEqual(['newest'], self.handled)

    def test_pending_ambiguous_retry_outside_retention_does_not_send(self):
        worker = self.worker(ping)
        self.transport.add('/bot ping'); self.transport.lose_receipt = True
        with self.assertRaises(TimeoutError):
            worker.step()
        self.transport.posts = []
        self.transport.high = 5000
        self.transport.add('too late')
        count = len([c for c in self.transport.calls if c.startswith('bot.post')])
        with self.assertRaisesRegex(RoomError, 'outlived'):
            self.worker(ping).step()
        self.assertEqual(count, len([c for c in self.transport.calls if c.startswith('bot.post')]))
        self.assertEqual(1, len(self.state.pending()))

    def test_bot_namespace_ignores_old_commands_and_conversation_mentions(self):
        worker = self.worker(ping)
        for text in ['/ping', '/help', 'try /bot ping', '/botping',
                     ' /BOT\tPiNg ', '/bot about', '/bot', '/bot help']:
            self.transport.add(text)
        for _ in range(10):
            count, more = worker.step()
            if not count and not more:
                break
        replies = [bytes.fromhex(p['text_hex']).decode() for p in self.transport.posts if p['author'] == ROOM]
        self.assertEqual(['[Bot] pong',
                         '[Bot] Squishyware room bot: Python over USB; replies use the room identity.',
                         '[Bot] Commands: /bot ping | /bot help | /bot about',
                         '[Bot] Commands: /bot ping | /bot help | /bot about'], replies)
        self.assertEqual([], self.state.pending())
        self.assertEqual(self.transport.high, self.state.cursor)

    def test_bot_unknown_verbs_and_extra_arguments_return_short_help(self):
        worker = self.worker(ping)
        for text in ['/bot reboot', '/bot ping reboot', '/bot help extra', '/bot about extra']:
            self.transport.add(text)
        worker.step(); worker.step()
        replies = [bytes.fromhex(p['text_hex']).decode() for p in self.transport.posts if p['author'] == ROOM]
        self.assertEqual('[Bot] Unknown bot command. Try /bot help.', replies[0])
        self.assertEqual(['[Bot] Usage: /bot ping | /bot help | /bot about'] * 3, replies[1:])
        self.assertTrue(all(len(reply.encode('utf-8')) <= 151 for reply in replies))

    def test_multiple_replies_keep_handler_order_even_with_random_ids(self):
        worker = self.worker(lambda post: ['first', 'second'])
        self.transport.add('input')
        with patch('room_bot.worker.secrets.randbelow', side_effect=[999, 1]):
            worker.step()
        texts = [bytes.fromhex(p['text_hex']).decode() for p in self.transport.posts[1:]]
        self.assertEqual(['[Bot] first', '[Bot] second'], texts)

    def test_protocol_rejects_malformed_pages_and_receipts(self):
        self.transport.add('message')
        for mutate in [lambda rows: rows[:-1],
                       lambda rows: [dict(rows[0], sequence='0'), rows[-1]],
                       lambda rows: [rows[0], dict(rows[-1], next='99')],
                       lambda rows: [dict(rows[0], text_hex='00'), rows[-1]],
                       lambda rows: [dict(rows[-1], next='0', count=0, more=True)]]:
            self.transport.transform = mutate
            with self.assertRaises(ProtocolError):
                self.client.read(0)
        self.transport.transform = lambda rows: [dict(rows[0], request_id='7')]
        with self.assertRaises(ProtocolError):
            self.client.post(8, 'test')

    def test_send_byte_limits_and_controls_cannot_inject_console_commands(self):
        receipt = self.client.post(1, 'é\nreboot\r"')
        self.assertEqual('1', receipt['sequence'])
        command = self.transport.calls[-1]
        self.assertNotIn('\n', command); self.assertNotIn('\r', command)
        for bad in ['', 'x' * 152, 'é' * 76, 'x\0y']:
            with self.assertRaises(ValueError):
                self.client.post(2, bad)

    def test_reply_rate_limit_is_applied_between_commits(self):
        clock = [0]; slept = []
        def sleep(seconds):
            slept.append(seconds); clock[0] += seconds
        worker = BotWorker(self.client, self.state, lambda post: ['one', 'two'], reply_interval=5,
                           sleep=sleep, monotonic=lambda: clock[0])
        self.transport.add('input'); worker.step()
        self.assertEqual([5], slept)

    def test_unknown_state_schema_is_rejected_without_creating_tables(self):
        other = Path(self.temp.name) / 'unknown.sqlite3'
        with sqlite3.connect(other) as db:
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
            db.execute("INSERT INTO metadata VALUES ('schema','2')")
        db.close()
        with self.assertRaises(RoomError):
            BotState(other)
        with sqlite3.connect(other) as db:
            self.assertEqual([('metadata',)], db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        db.close()

class BytePort:
    def __init__(self, output):
        self.output = bytearray(output); self.sent = []
    @property
    def in_waiting(self):
        return min(7, len(self.output))
    def read(self, count):
        data = bytes(self.output[:count]); del self.output[:count]; return data
    def reset_input_buffer(self):
        pass
    def write(self, data):
        self.sent.append(data)
    def flush(self):
        pass

class SerialTests(unittest.TestCase):
    def transport(self, output):
        import threading
        transport = SerialTransport.__new__(SerialTransport)
        transport.timeout = 0.02
        transport.lock = threading.Lock()
        transport.port = BytePort(output)
        return transport

    def test_fragmented_serial_echo_boot_noise_and_tagged_response(self):
        transport = self.transport(b'boot noise\nbot.info\r@bot {"type":"info","api":1}\r\n')
        self.assertEqual([{'type': 'info', 'api': 1}], transport.exchange('bot.info', 'info'))
        self.assertEqual([b'bot.info\r\n'], transport.port.sent)

    def test_invalid_json_error_incomplete_page_and_command_injection(self):
        with self.assertRaises(ProtocolError):
            self.transport(b'@bot {not-json}\n').exchange('bot.info', 'info')
        with self.assertRaises(RoomError):
            self.transport(b'@bot {"type":"error","error":"recovery required"}\r\n').exchange('bot.info', 'info')
        with self.assertRaises(TimeoutError):
            self.transport(b'@bot {"type":"post"}\n').exchange('bot.read 0', 'end')
        with self.assertRaises(ValueError):
            self.transport(b'').exchange('bot.info\rreboot', 'info')

if __name__ == '__main__':
    unittest.main()
