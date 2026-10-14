"""National tide source, offset, clock, offline, refresh and preservation checks."""
from datetime import date,datetime,timezone
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from room_bot.clock import SessionClock
from room_bot.config import Config,TideSettings,Limits,ConfigError
from room_bot.commands import Dispatcher
from room_bot.data import DataError
from room_bot.offline import install_offline_commands
from room_bot.tide_data import (catalogue_sites,secondary_rows,parse_annual,instants,minutes,
                                create_store,import_annual,TideStore,norm)
from room_bot.tide_service import (TideService,select_site,publication,format_event)
from room_bot.tide_http import allowed
from room_bot.install import load_tide_manifest,SetupError
from test_commands import post

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'tests/fixtures'
CAT=(FIX/'linz-tide-sites-20261014.json').read_bytes()
SEC=(FIX/'linz-secondary-2026-27.csv').read_bytes()
URL='https://static.charts.linz.govt.nz/tide-tables/maj-ports/csv/Raglan%202026.csv'
RETRIEVED='2026-01-01T00:00:00Z'  # Synthetic fixture receipt, not source acquisition evidence.
SITES=catalogue_sites(CAT,secondary_rows(SEC))
RAGLAN=next(s for s in SITES if s['name']=='Raglan')
RAW=(ROOT/'datasets/tides/raw/Raglan-2026.csv').read_bytes()


def clock(year=2026,month=6,day=3,hour=10,zone='Pacific/Auckland'):
    value=datetime(year,month,day,hour,tzinfo=ZoneInfo(zone)).timestamp()
    return SessionClock(True,wall=lambda:value,monotonic=lambda:0)


def base(root):
    meta={'years':[2026],'secondary_review_after':'2027-07-01','secondary_revision':'2026-27'}
    db=create_store(root/'tides.sqlite3',SITES,meta)
    import_annual(db,RAW,RAGLAN,2026,URL,RETRIEVED)
    db.close()


class NationalTideTests(unittest.TestCase):
    def test_all_catalogued_sites_have_reviewed_direct_or_secondary_mapping(self):
        self.assertEqual(322,len(SITES))
        self.assertEqual(88,sum(s['type']=='daily' for s in SITES))
        self.assertEqual(234,sum(s['type']=='offset' for s in SITES))
        ocean=next(s for s in SITES if s['name']=='Ocean Bay')
        self.assertEqual('wellington',ocean['reference_id'])
        self.assertEqual('Lyttelton',ocean['catalogue_reference'])
        paparoa=next(s for s in SITES if s['name']=='Paparoa Point')
        self.assertAlmostEqual(173.0166667,paparoa['longitude'],places=5)
        chatham=next(s for s in SITES if s['name']=='Waitangi - Chatham Island')
        self.assertEqual('Pacific/Chatham',chatham['zone'])

    def test_annual_2027_preserves_dst_ambiguity_and_full_year(self):
        raw=(ROOT/'datasets/tides/raw/Raglan-2027.csv').read_bytes()
        rows=parse_annual(raw,RAGLAN,2027)
        ambiguous=[r for r in rows if r[1]!=r[2]]
        self.assertTrue(ambiguous)
        self.assertEqual(3600,ambiguous[0][2]-ambiguous[0][1])
        text=format_event(ambiguous[0][3],ambiguous[0][1:3]+(ambiguous[0][4],),RAGLAN,ambiguous[0][1]-60)
        self.assertIn('NZDT/NZST (DST uncertain)',text)
        self.assertIn('2027-12-31',datetime.fromtimestamp(rows[-1][1],ZoneInfo('Pacific/Auckland')).date().isoformat())

    def test_variable_six_event_rows_and_equal_heights_keep_all_extrema(self):
        rows=[]
        for i in range(365):
            day=date(2026,1,1)+__import__('datetime').timedelta(days=i)
            values=['00:00','3','06:00','1','12:00','3','18:00','1']
            if i==5:values=['00:00','3','02:00','2','04:00','2','06:00','1','12:00','3','18:00','1']
            rows.append(','.join([str(day.day),'X',str(day.month),'2026',*values]))
        raw=('0,Raglan\nBased on constituents\nLocal Std or Daylight Time,Tidal heights in metres.\n'+'\n'.join(rows)).encode()
        result=parse_annual(raw,RAGLAN,2026)
        self.assertEqual(1462,len(result))
        self.assertEqual(['high','low','high','low','high','low'],[r[3] for r in result[20:26]])

    def test_nonexistent_time_missing_year_and_identity_are_rejected(self):
        with self.assertRaises(DataError):instants(date(2026,9,27),'02:30',ZoneInfo('Pacific/Auckland'))
        with self.assertRaises(DataError):parse_annual(RAW,{**RAGLAN,'name':'Another Site'},2026)
        with self.assertRaises(DataError):parse_annual(RAW,RAGLAN,2027)
        with self.assertRaises(DataError):parse_annual(b'bad header\n'+RAW.split(b'\n',1)[1],RAGLAN,2026)
        with self.assertRaises(DataError):parse_annual(RAW.replace(b'2,Fr,1,2026',b'3,Fr,1,2026'),RAGLAN,2026)

    def test_matching_macrons_aliases_and_canonical_waitangi_are_unambiguous(self):
        self.assertEqual('Piha Beach',select_site(SITES,'PIHA')['name'])
        self.assertEqual('Riverton / Aparima',select_site(SITES,'Riverton')['name'])
        self.assertEqual('Waitangi',select_site(SITES,'Waitangi')['name'])
        self.assertEqual('Whangārei',select_site(SITES,'Whangarei')['name'])
        with self.assertRaisesRegex(DataError,'No exact'):select_site(SITES,'Bay')

    def test_signed_offsets_and_missing_heights_do_not_become_zero(self):
        self.assertEqual(-22,minutes('-0022'))
        self.assertEqual(92,minutes('+0132'))
        self.assertIsNone(minutes('-'))
        with self.assertRaises(DataError):minutes('+0065')
        missing=next(s for s in SITES if s['name']=='Maraetai')['offset']
        self.assertIsNone(missing['msl'])
        self.assertIsNone(missing['ratio'])
        with self.assertRaises(DataError):secondary_rows(SEC.replace(b',1.8,1.04,',b',1.8,-1.04,',1))

    def test_offline_read_does_not_require_offline_content_db_or_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base(root)
            cfg=replace(Config(),enabled_commands=('tides',),limits=Limits(request_cooldown_seconds=0))
            d=Dispatcher(cfg,'[SquishyBot] ',clock())
            install_offline_commands(d,root)
            with patch('room_bot.tide_service.subprocess.run',side_effect=AssertionError('Fresh tide lookup accessed network')):
                replies=d(post('/bot location tides Raglan'))
            answer=''.join(replies)
            self.assertIn('High: Wed 03 Jun 11:55 NZST 2.9m',answer)
            self.assertIn('Low: Wed 03 Jun 17:52 NZST 0.6m',answer)
            self.assertTrue(all(len(('[SquishyBot] '+r).encode())<=151 for r in replies))
            self.assertNotIn('http',answer)
            self.assertFalse((root/'offline.sqlite3').exists())

    def test_location_list_works_without_clock_while_predictions_require_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base(root);s=TideService(root,TideSettings())
            self.assertIn('tide sites match',''.join(s.reply(['locations','Raglan'],SessionClock())))
            self.assertIn('Clock unconfirmed',s.reply(['Raglan'],SessionClock()))

    def test_failed_near_expiry_refresh_keeps_data_and_persists_attempt_backoff(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base(root);before=hashlib.sha256((root/'tides.sqlite3').read_bytes()).hexdigest()
            calls=[]
            def fail(*args):calls.append(1);raise DataError('offline')
            for _ in range(2):
                replies=TideService(root,TideSettings(),fail).reply(['Raglan'],clock(2026,12,20))
                self.assertIn('LINZ predictions',''.join(replies))
            self.assertEqual(1,len(calls))
            self.assertEqual(before,hashlib.sha256((root/'tides.sqlite3').read_bytes()).hexdigest())

    def test_refresh_updates_overlay_atomically_and_cross_year_lookup_works(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base(root);before=(root/'tides.sqlite3').read_bytes()
            page=b'<script type="application/json" data-drupal-selector="drupal-settings-json">{"linz_map_snapshot_tp":{"end":2027}}</script><a href="https://www.linz.govt.nz/sites/default/files/2026-06/NZNA_Secondary-ports-2026-27.csv">x</a>'
            raw2027=(ROOT/'datasets/tides/raw/Raglan-2027.csv').read_bytes()
            def fetch(url,*args):
                if url.endswith('tide-predictions'):return page
                if url.endswith('/api/v1/tp'):return CAT
                if 'Secondary-ports' in url:return SEC
                if 'Raglan' in url and '2027.csv' in url:return raw2027
                self.fail('Unexpected source request '+url)
            s=TideService(root,TideSettings(),fetch)
            result=s.reply(['Raglan'],clock(2026,12,31,23))
            self.assertIn('01 Jan',''.join(result))
            self.assertEqual(before,(root/'tides.sqlite3').read_bytes())
            overlay=(root/'tide-updates.sqlite3').read_bytes()
            def bad(url,*args):raise DataError('broken update')
            with self.assertRaises(DataError):TideService(root,TideSettings(),bad).refresh('raglan',clock().now())
            self.assertEqual(overlay,(root/'tide-updates.sqlite3').read_bytes())
            self.assertFalse(list(root.glob('.tide-update-*')))

    def test_expired_predictions_are_not_extrapolated_and_refresh_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);base(root)
            def fail(*args):self.fail('Disabled refresh accessed network')
            s=TideService(root,TideSettings(auto_refresh=False),fail)
            self.assertIn('No extrapolation',s.reply(['Raglan'],clock(2030,1,1)))
            self.assertIn('LINZ predictions',''.join(s.reply(['Raglan'],clock(2026,12,20))))

    def test_chatham_conversion_and_secondary_formula_match_official_example(self):
        now=datetime(2026,1,1,0,tzinfo=timezone.utc).timestamp()
        site={'zone':'Pacific/Chatham'}
        result=format_event('high',(now,now,2.3),site,now-1)
        self.assertIn('13:45 +1345',result)
        self.assertAlmostEqual(2.3,round(1.3+(3.3-1.92)*.74,1))
        self.assertAlmostEqual(.2,round(1.3+(.4-1.92)*.74,1))

    def test_source_requests_cannot_target_arbitrary_hosts_or_paths(self):
        self.assertTrue(allowed(URL))
        for url in ('http://www.linz.govt.nz/api/v1/tp','https://evil.example/data.csv',
                    'https://www.linz.govt.nz/other','https://www.linz.govt.nz/api/v1/tp?secret=1'):
            self.assertFalse(allowed(url))

    def test_settings_and_published_manifest_are_validated(self):
        cfg=Config.from_dict({'tides':{'auto_refresh':False,'retry_hours':24}})
        self.assertFalse(cfg.tides.auto_refresh)
        with self.assertRaises(ConfigError):Config.from_dict({'tides':{'retry_hours':0}})
        m=load_tide_manifest(ROOT/'installer/nz-tides.json')
        self.assertEqual(322,m['coverage']['sites'])
        with tempfile.TemporaryDirectory() as folder:
            m['files']['../state.sqlite3']=m['files'].pop('tides.sqlite3')
            p=Path(folder)/'bad.json';p.write_text(json.dumps(m))
            with self.assertRaises(SetupError):load_tide_manifest(p)
