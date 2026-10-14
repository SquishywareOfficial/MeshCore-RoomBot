from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

from room_bot.commands import Dispatcher, Pager, split_utf8
from room_bot.config import Config, Limits
from room_bot import BotState, BotWorker, RoomClient
from test_room_bot import FakeUSB, ROOM


def post(text, sequence=1, author='a' * 64):
    return SimpleNamespace(text=text, sequence=sequence, author=author)


class DispatchTests(unittest.TestCase):
    def test_radio_chunks_preserve_text_and_do_not_split_words_when_possible(self):
        original='Useful words and readable boundaries. '*12
        chunks=split_utf8(original,64)
        self.assertEqual(original,''.join(chunks));self.assertTrue(all(len(c.encode())<=64 for c in chunks))
        self.assertTrue(all(c[-1].isspace() for c in chunks[:-1]))
        self.assertEqual('x'*150,''.join(split_utf8('x'*150,64)))

    def test_lookup_details_are_opt_in_per_user_expiring_and_bounded(self):
        now=[0]
        dispatch=Dispatcher(replace(Config(),limits=Limits(paging_users=1)),monotonic=lambda:now[0])
        dispatch.remember_details('a',['Only on request']);self.assertEqual(['Only on request'],dispatch.details('a'))
        self.assertIn('No saved details',dispatch.details('b'))
        now[0]=301;self.assertIn('No saved details',dispatch.details('a'))
        dispatch.remember_details('b',['Second']);dispatch.remember_details('c',['Third'])
        self.assertEqual(1,len(dispatch.detail_records));self.assertIn('No saved details',dispatch.details('b'))

    def test_builtin_compatibility_and_disabled_commands(self):
        dispatch = Dispatcher()
        self.assertEqual(['pong'], dispatch(post(' /BOT\tPING ')))
        self.assertEqual(['Commands: /bot ping | /bot help | /bot about'], dispatch(post('/bot', 2)))
        for text in ('/ping', '/botping', 'try /bot ping', 'ordinary conversation'):
            self.assertIsNone(dispatch(post(text)))
        self.assertEqual(['Usage: /bot ping | /bot help | /bot about'], dispatch(post('/bot ping extra')))
        dispatch = Dispatcher(replace(Config(), enabled_commands=('help',)))
        self.assertIn('disabled', dispatch(post('/bot ping'))[0])
        self.assertEqual(['Commands: /bot help'], dispatch(post('/bot help')))

    def test_expensive_work_is_limited_before_execution_but_retry_keeps_result(self):
        clock = [0]
        dispatch = Dispatcher(replace(Config(), enabled_commands=('directory',)), monotonic=lambda: clock[0])
        calls = []
        dispatch.register('directory', '/bot directory TOPIC', lambda ctx, args: calls.append(args) or 'Found')
        request = post('/bot directory Hamilton East')
        self.assertEqual(['Found'], dispatch(request))
        self.assertEqual(['Found'], dispatch(request))
        self.assertIn('wait', dispatch(post(request.text, 2))[0])
        self.assertEqual([['Hamilton', 'East']], calls)
        clock[0] = 4
        self.assertEqual(['Found'], dispatch(post(request.text, 2)))
        self.assertEqual(2, len(calls))

    def test_failed_callback_does_not_consume_admission_or_memo(self):
        dispatch = Dispatcher(replace(Config(), enabled_commands=('directory',)))
        calls = []
        def callback(ctx, args):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('lookup failed')
            return 'Recovered'
        dispatch.register('directory', '/bot directory TOPIC', callback)
        with self.assertRaises(RuntimeError):
            dispatch(post('/bot directory topic'))
        self.assertEqual(['Recovered'], dispatch(post('/bot directory topic')))

    def test_multibyte_prefix_and_paging_are_bounded_and_isolated(self):
        clock = [0]
        config = replace(Config(), enabled_commands=('directory',),
                         limits=Limits(paging_users=1, request_cooldown_seconds=0))
        prefix = '[Squishy🦑Bot] '
        dispatch = Dispatcher(config, prefix, monotonic=lambda: clock[0])
        source = 'Māori 🦑 ' * 100
        dispatch.register('directory', '/bot directory TOPIC', lambda ctx, args: source)
        initial = dispatch(post('/bot directory subject'))
        self.assertEqual(4, len(initial))
        self.assertTrue(all(len((prefix + part).encode()) <= 151 for part in initial))
        self.assertEqual(initial, dispatch(post('/bot directory subject')))
        self.assertIn('No saved page', dispatch(post('/bot next', 2, 'b' * 64))[0])
        chunks = initial[:3]
        for sequence in range(3, 30):
            result = dispatch(post('/bot next', sequence))
            if result[-1].startswith('More:'):
                chunks.extend(result[:-1])
            else:
                chunks.extend(result)
                break
        self.assertEqual(source, ''.join(chunks))
        dispatch(post('/bot directory subject', 50))
        clock[0] = 301
        self.assertIn('No saved page', dispatch(post('/bot next', 51))[0])

    def test_storage_caps_large_results_and_user_maps(self):
        config = replace(Config(), enabled_commands=('directory',),
                         limits=Limits(paging_users=2, request_cooldown_seconds=0))
        dispatch = Dispatcher(config)
        dispatch.register('directory', '/bot directory TOPIC', lambda ctx, args: 'x' * 1000)
        for n in range(10):
            dispatch(post('/bot directory topic', n, str(n)))
        self.assertEqual(2, len(dispatch.pager.pages))
        self.assertLessEqual(len(dispatch.admissions), 2)
        self.assertLessEqual(len(dispatch.memo), 8)
        pager = Pager('[Bot] ', Limits())
        self.assertIn('too large', pager.start('author', 'x' * 20000)[0])

    def test_invalid_output_is_not_silently_changed(self):
        with self.assertRaises(ValueError):
            split_utf8('x\0y', 100)
        with self.assertRaises(ValueError):
            split_utf8('🦑', 1)
        with self.assertRaises(ValueError):
            Pager('[Bot] ', Limits()).start('user', [1])

    def test_paged_results_use_existing_durable_worker_and_retry_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            state = BotState(Path(temp) / 'state.sqlite3')
            try:
                transport = FakeUSB()
                config = replace(Config(), enabled_commands=('directory',),
                                 limits=Limits(request_cooldown_seconds=0))
                dispatch = Dispatcher(config)
                calls = []
                dispatch.register('directory', '/bot directory TOPIC',
                                  lambda ctx, args: calls.append(ctx.post.sequence) or ('Māori ' * 100))
                worker = BotWorker(RoomClient(transport), state, dispatch, reply_interval=0)
                transport.add('/bot directory topic')
                transport.lose_receipt = True
                with self.assertRaises(TimeoutError):
                    worker.step()
                self.assertEqual(1, state.cursor)
                self.assertEqual(4, len(state.pending()))
                worker.step()
                worker.step()
                self.assertEqual([1], calls)
                self.assertEqual([], state.pending())
                replies = [p for p in transport.posts if p['author'] == ROOM]
                self.assertEqual(4, len(replies))
                self.assertTrue(all(len(bytes.fromhex(p['text_hex'])) <= 151 for p in replies))
            finally:
                state.close()
