from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from room_bot import BotState,BotWorker,RoomClient
from room_bot.clock import SessionClock
from room_bot.commands import Dispatcher
from room_bot.community import CommunityStore,install_community_commands
from room_bot.community import main
from room_bot.config import Config,CommunitySettings,Limits
from room_bot.data import DataError
from test_commands import post
from test_room_bot import FakeUSB,ROOM,USER


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=CommunityStore(self.root/'community.sqlite3',ROOM)
        self.settings=CommunitySettings(notice_days=7,checkin_days=30,notice_limit=2,checkin_limit=2)
        self.config=replace(Config(),community=self.settings,aliases=(('TestUser','a'*64),))

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def mutate(self,sequence,verb,args,author='a'*64,now=1791800000):
        return self.store.mutation(post('/bot '+verb+' '+' '.join(args),sequence,author),verb,args,self.settings,now)

    def test_actions_are_durable_idempotent_and_conflicts_rejected(self):
        result=self.mutate(1,'notice',['add','Synthetic report'])
        self.store.close();self.store=CommunityStore(self.root/'community.sqlite3',ROOM)
        self.assertEqual(result,self.mutate(1,'notice',['add','Synthetic report']))
        self.assertEqual(1,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])
        with self.assertRaises(DataError):self.mutate(1,'notice',['add','Different text'])

    def test_only_author_can_withdraw_and_ids_do_not_reuse(self):
        self.assertIn('notice 1 saved',self.mutate(1,'notice',['add','one']))
        self.assertIn('Only the notice author',self.mutate(2,'notice',['withdraw','1'],author='b'*64))
        self.assertIn('withdrawn',self.mutate(3,'notice',['withdraw','1']))
        self.assertIn('notice 2 saved',self.mutate(4,'notice',['add','two']))

    def test_latest_voluntary_checkin_alias_and_ambiguous_prefix(self):
        self.mutate(1,'checkin',[],author='a'*64)
        self.mutate(2,'checkin',['Latest statement'],author='a'*64)
        self.assertEqual(1,self.store.db.execute('SELECT count(*) FROM checkins').fetchone()[0])
        answer=self.store.read('lastseen',['TestUser'],self.config,1791800001)
        self.assertIn('Last checked in',answer);self.assertIn('Latest statement',answer)
        self.mutate(3,'checkin',[],author='a'*8+'b'*56)
        self.assertIn('Ambiguous',self.store.read('lastseen',['a'*8],self.config,1791800001))
        self.assertIn('No retained',self.store.read('lastseen',['f'*8],self.config,1791800001))

    def test_time_unknown_retention_is_not_invented_and_caps_apply(self):
        self.assertIn('Time unknown',self.mutate(1,'notice',['add','one'],now=None))
        self.mutate(2,'notice',['add','two'],now=None)
        self.assertIn('full',self.mutate(3,'notice',['add','three'],now=None))
        self.assertIn('clock unconfirmed',' '.join(self.store.read('notice',['list'],self.config,None)))
        self.store.expire(self.settings,2000000000)
        self.assertEqual(2,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])

    def test_known_time_expiry_does_not_erase_retry_history(self):
        result=self.mutate(1,'notice',['add','old'],now=1791800000)
        self.mutate(2,'checkin',[],now=1791800000)
        with self.store.db:self.store.expire(self.settings,1791800000+31*86400)
        self.assertEqual(0,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])
        self.assertEqual(0,self.store.db.execute('SELECT count(*) FROM checkins').fetchone()[0])
        self.assertEqual(result,self.mutate(1,'notice',['add','old'],now=1791800000+32*86400))
        self.assertEqual(0,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])

    def test_room_identity_mismatch_does_not_adopt_another_rooms_data(self):
        self.mutate(1,'checkin',[])
        with self.assertRaises(DataError):CommunityStore(self.root/'community.sqlite3',USER)
        self.assertEqual(ROOM,self.store.room_key)

    def test_retained_sequence_floor_prevents_repeating_pruned_actions(self):
        with self.store.db:self.store.db.execute("UPDATE metadata SET value='100' WHERE key='floor'")
        self.assertIn('cannot safely',self.mutate(50,'notice',['add','too old']))
        self.assertEqual(0,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])
        self.assertIn('saved',self.mutate(2**64-1,'checkin',[]))

    def test_crash_after_domain_commit_before_delivery_state_commit_cannot_duplicate_notice(self):
        state=BotState(self.root/'state.sqlite3');transport=FakeUSB()
        config=replace(Config(),enabled_commands=('notice',),limits=Limits(request_cooldown_seconds=0))
        def handler():
            dispatch=Dispatcher(config);dispatch.bind_room(ROOM)
            install_community_commands(dispatch,self.root)
            return dispatch
        try:
            worker=BotWorker(RoomClient(transport),state,handler(),reply_interval=0)
            transport.add('/bot notice add Synthetic retry test')
            state.db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'simulated crash'); END")
            state.db.commit()
            with self.assertRaises(sqlite3.IntegrityError):worker.step()
            self.assertEqual(0,state.cursor)
            self.assertEqual(1,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])
            state.db.execute('DROP TRIGGER fail_outbox');state.db.commit()
            state.close();state=BotState(self.root/'state.sqlite3')
            BotWorker(RoomClient(transport),state,handler(),reply_interval=0).step()
            self.assertEqual(1,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])
            self.assertEqual([],state.pending())
        finally:state.close()

    def test_retry_ledger_is_bounded_and_pruning_rejects_old_replayed_mutations(self):
        with self.store.db:
            self.store.db.executemany('INSERT INTO outcomes VALUES (?,?,?,?)',
                                     [(str(n),'a'*64,'synthetic old action','"old"') for n in range(1,4097)])
        self.mutate(5000,'checkin',[])
        self.assertLessEqual(self.store.db.execute('SELECT count(*) FROM outcomes').fetchone()[0],4096)
        self.assertIn('cannot safely',self.mutate(1,'notice',['add','old replay']))
        self.assertEqual(0,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])

    def test_local_operator_purge_preserves_outcomes_and_requires_clock_for_age(self):
        from contextlib import redirect_stdout
        import io
        self.mutate(1,'notice',['add','unknown time'],now=None)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(1,main(['--data-dir',str(self.root),'purge','--records','notices','--older-than','1']))
            self.assertEqual(0,main(['--data-dir',str(self.root),'purge','--records','notices','--all']))
        self.assertEqual(0,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])
        self.assertEqual(1,self.store.db.execute('SELECT count(*) FROM outcomes').fetchone()[0])

    def test_invalid_user_controls_get_a_response_and_multiword_aliases_work(self):
        self.assertIn('invalid',self.mutate(1,'notice',['add','bad\x01text']))
        self.assertEqual(0,self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0])
        self.mutate(2,'checkin',[])
        config=replace(self.config,aliases=(('Synthetic User','a'*64),))
        self.assertIn('Last checked in',self.store.read('lastseen',['Synthetic','User'],config,1791800001))
