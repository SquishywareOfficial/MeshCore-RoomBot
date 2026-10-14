"""Bounded persistent community records with durable per-input action outcomes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from .clock import ClockUnavailable, SessionClock
from .config import ConfigError, load_config
from .data import DataError, text


def key(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise DataError('Room/author must be a full public key')
    return value


class CommunityStore:
    def __init__(self,path,room_key=None):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path)
        self.db.row_factory=sqlite3.Row
        try:
            tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables:
                if tables!={'metadata','notices','checkins','outcomes'}:
                    raise DataError('Unknown community database; preserve it')
                version=self.db.execute("SELECT value FROM metadata WHERE key='version'").fetchone()
                if not version or version[0]!='1': raise DataError('Unsupported community schema')
            self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
            with self.db:
                if not tables:
                    if room_key is None: raise DataError('New community store requires verified room identity')
                    key(room_key)
                    self.db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                    self.db.executemany('INSERT INTO metadata VALUES (?,?)',[('version','1'),('room_key',room_key),('floor','0')])
                    self.db.execute('CREATE TABLE notices (id INTEGER PRIMARY KEY AUTOINCREMENT,author TEXT NOT NULL,text TEXT NOT NULL,created REAL)')
                    self.db.execute('CREATE TABLE checkins (author TEXT PRIMARY KEY,text TEXT NOT NULL,created REAL)')
                    self.db.execute('CREATE TABLE outcomes (sequence TEXT PRIMARY KEY,author TEXT NOT NULL,request TEXT NOT NULL,response TEXT NOT NULL)')
                saved=self.db.execute("SELECT value FROM metadata WHERE key='room_key'").fetchone()
                if not saved or room_key is not None and saved[0]!=key(room_key):
                    raise DataError('Community data belongs to another room; use a separate data directory')
                self.room_key=saved[0]
        except BaseException:
            self.db.close();raise

    def close(self):
        self.db.close()

    def expire(self,settings,now):
        if now is None: return
        for table,days in (('notices',settings.notice_days),('checkins',settings.checkin_days)):
            if days:
                self.db.execute(f'DELETE FROM {table} WHERE created IS NOT NULL AND created<?',(now-days*86400,))

    def mutation(self,post,verb,args,settings,now):
        key(post.author)
        if type(post.sequence) is not int or not 0<post.sequence<2**64:
            raise DataError('Invalid input sequence')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            saved=self.db.execute('SELECT * FROM outcomes WHERE sequence=?',(str(post.sequence),)).fetchone()
            if saved:
                if saved['author']!=post.author or saved['request']!=post.text:
                    raise DataError('Community action sequence conflict; preserve data')
                return json.loads(saved['response'])
            floor=int(self.db.execute("SELECT value FROM metadata WHERE key='floor'").fetchone()[0])
            if post.sequence<=floor:
                return 'Old action is outside retained retry history; it cannot safely be repeated.'
            self.expire(settings,now)
            if verb=='checkin':
                body=' '.join(args) or 'Checked in'
                try: text(body,'check-in',500)
                except DataError: return 'Check-in text is invalid or too long; use plain text without control characters.'
                existing=self.db.execute('SELECT 1 FROM checkins WHERE author=?',(post.author,)).fetchone()
                count=self.db.execute('SELECT count(*) FROM checkins').fetchone()[0]
                if not existing and count>=settings.checkin_limit: response='Check-in list full; operator must purge old users.'
                else:
                    self.db.execute('INSERT OR REPLACE INTO checkins VALUES (?,?,?)',(post.author,body,now))
                    response='Check-in saved. This records your statement, not a safety confirmation.'
            elif args and args[0].casefold()=='add':
                body=' '.join(args[1:])
                if not body: response='Usage: /bot notice add TEXT'
                else:
                    try: text(body,'notice',1000)
                    except DataError: return 'Notice text is invalid or too long; use plain text without control characters.'
                    if self.db.execute('SELECT count(*) FROM notices').fetchone()[0]>=settings.notice_limit:
                        response='Noticeboard full; withdraw a notice or ask the operator to purge.'
                    else:
                        identifier=self.db.execute('INSERT INTO notices (author,text,created) VALUES (?,?,?)',(post.author,body,now)).lastrowid
                        response=f'Community notice {identifier} saved; report is unverified.'
            elif args and args[0].casefold()=='withdraw':
                if len(args)!=2 or not args[1].isdigit(): response='Usage: /bot notice withdraw ID'
                else:
                    identifier=args[1]
                    found=self.db.execute('SELECT author FROM notices WHERE id=?',(identifier,)).fetchone()
                    if not found: response='No such active notice.'
                    elif found['author']!=post.author: response='Only the notice author can withdraw it through the room.'
                    else:
                        self.db.execute('DELETE FROM notices WHERE id=?',(identifier,))
                        response=f'Community notice {identifier} withdrawn.'
            else: response='Usage: /bot notice add TEXT | list | show ID | withdraw ID'
            if now is None and ('saved' in response): response+=' Time unknown.'
            if self.db.execute('SELECT count(*) FROM outcomes').fetchone()[0]>=4096:
                old=[r[0] for r in self.db.execute('SELECT sequence FROM outcomes ORDER BY length(sequence),sequence LIMIT 128')]
                self.db.executemany('DELETE FROM outcomes WHERE sequence=?',[(seq,) for seq in old])
                self.db.execute("UPDATE metadata SET value=? WHERE key='floor'",(str(max(floor,*map(int,old))),))
            self.db.execute('INSERT INTO outcomes VALUES (?,?,?,?)',(str(post.sequence),post.author,post.text,json.dumps(response)))
            return response

    def read(self,verb,args,config,now):
        with self.db: self.expire(config.community,now)
        if verb=='lastseen':
            if not args: return 'Usage: /bot lastseen ALIAS_OR_KEY_PREFIX'
            aliases={' '.join(name.split()).casefold():value for name,value in config.aliases}
            name=' '.join(args).casefold()
            selector=aliases.get(name,name)
            if not re.fullmatch('[0-9a-f]{8,64}',selector): return 'Use a saved alias or at least 8 hexadecimal public-key characters.'
            rows=self.db.execute('SELECT * FROM checkins WHERE author LIKE ? LIMIT 2',(selector+'%',)).fetchall()
            if len(rows)>1: return 'Ambiguous public-key prefix; use more characters.'
            if not rows: return 'No retained voluntary check-in for that user.'
            row=rows[0]
            return f'Last checked in {row["author"][:12]}: {self.when(row["created"])}. {row["text"]}'+(' Current clock unconfirmed.' if now is None else '')
        action=args[0].casefold() if args else 'list'
        if action=='list' and len(args)<=1:
            rows=self.db.execute('SELECT * FROM notices ORDER BY id DESC LIMIT ?',(min(20,config.limits.query_results),)).fetchall()
        elif action=='show' and len(args)==2 and args[1].isdigit():
            rows=self.db.execute('SELECT * FROM notices WHERE id=?',(args[1],)).fetchall()
        else: return 'Usage: /bot notice list | show ID | add TEXT | withdraw ID'
        if not rows: return 'No matching retained notices.'
        return [('Community reports, unverified; clock unconfirmed so age-based expiry paused.' if now is None else 'Community reports, unverified.')]+[
            f'Notice {r["id"]}; author {r["author"][:12]}; {self.when(r["created"])}: '
            +(r['text'] if action=='show' else r['text'].encode('utf-8')[:60].decode('utf-8','ignore')+' [show ID for detail]') for r in rows]

    @staticmethod
    def when(value):
        return datetime.fromtimestamp(value,timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if value is not None else 'time unknown'


def install_community_commands(dispatcher,data_path):
    def handle(verb,ctx,args):
        if ctx.room_key is None: return 'Community storage requires a verified room identity.'
        try: now=ctx.clock.now().timestamp()
        except ClockUnavailable: now=None
        store=CommunityStore(data_path/'community.sqlite3',ctx.room_key)
        try:
            if verb=='checkin' or verb=='notice' and args and args[0].casefold() in ('add','withdraw'):
                return store.mutation(ctx.post,verb,args,ctx.config.community,now)
            return store.read(verb,args,ctx.config,now)
        finally: store.close()
    for verb,usage in [('notice','/bot notice add/list/show/withdraw'),('checkin','/bot checkin [TEXT]'),('lastseen','/bot lastseen ALIAS_OR_KEY')]:
        dispatcher.register(verb,usage,lambda ctx,args,verb=verb:handle(verb,ctx,args))


def main(argv=None):
    parser=argparse.ArgumentParser(description='Local operator maintenance of an existing community DB; never publishes')
    parser.add_argument('--config',type=Path);parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--clock-trusted',action='store_true')
    actions=parser.add_subparsers(dest='action',required=True)
    actions.add_parser('list')
    withdraw=actions.add_parser('withdraw');withdraw.add_argument('id',type=int)
    purge=actions.add_parser('purge');purge.add_argument('--records',choices=('notices','checkins'),required=True)
    selection=purge.add_mutually_exclusive_group(required=True)
    selection.add_argument('--all',action='store_true');selection.add_argument('--older-than',type=int)
    args=parser.parse_args(argv);store=None
    try:
        config=load_config(args.config)
        root=args.data_dir.expanduser().resolve() if args.data_dir else config.data_path(args.config)
        if not (root/'community.sqlite3').exists(): raise DataError('No existing community database')
        store=CommunityStore(root/'community.sqlite3')
        if args.action=='list':
            print(json.dumps({'notices':store.db.execute('SELECT count(*) FROM notices').fetchone()[0],
                              'checkins':store.db.execute('SELECT count(*) FROM checkins').fetchone()[0],
                              'room_key':store.room_key}))
        elif args.action=='withdraw':
            if not 1<=args.id<2**63: raise DataError('Invalid notice ID')
            with store.db: count=store.db.execute('DELETE FROM notices WHERE id=?',(args.id,)).rowcount
            print(f'Withdrawn: {count}')
        else:
            if args.all:
                with store.db: count=store.db.execute(f'DELETE FROM {args.records}').rowcount
            else:
                if args.older_than<1 or args.older_than>3650: raise DataError('Days must be 1..3650')
                cutoff=SessionClock(args.clock_trusted).now().timestamp()-args.older_than*86400
                with store.db: count=store.db.execute(f'DELETE FROM {args.records} WHERE created IS NOT NULL AND created<?',(cutoff,)).rowcount
            print(f'Purged: {count}; durable action retry history preserved')
        return 0
    except (ConfigError,DataError,ClockUnavailable,OSError,sqlite3.Error) as exc:
        print(f'Community operation failed: {exc}');return 1
    finally:
        if store is not None: store.close()


if __name__=='__main__':
    raise SystemExit(main())
