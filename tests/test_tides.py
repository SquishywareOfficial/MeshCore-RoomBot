from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import patch

from room_bot.clock import SessionClock
from room_bot.data import DataError, OfflineStore, read_bundle
from room_bot.tides import convert_csv, local_instant, tide_reply


ROOT=Path(__file__).resolve().parents[1]
URL='https://static.charts.linz.govt.nz/tide-tables/maj-ports/csv/Raglan%202026.csv'
RETRIEVED='2026-10-12T21:40:00+00:00'  # Synthetic test metadata, not acquisition evidence.


class TideTests(unittest.TestCase):
    def test_real_source_example_three_event_day_and_adjacent_date(self):
        raw=(ROOT/'datasets/tides/raw/Raglan-2026.csv').read_bytes()
        result=convert_csv(raw,'Raglan',URL,RETRIEVED)
        records=result['records']
        day=[r for r in records if r['time'].startswith('2026-06-03')]
        self.assertEqual(['05:40','11:55','17:52'],[r['time'][11:16] for r in day])
        self.assertEqual(['low','high','low'],[r['event'] for r in day])
        self.assertEqual([0.7,2.9,0.6],[r['height_m'] for r in day])
        next_day=[r for r in records if r['time'].startswith('2026-06-04')]
        self.assertEqual('high',next_day[0]['event'])
        self.assertEqual('00:14',next_day[0]['time'][11:16])

    def test_source_local_times_get_offsets_without_dst_added_twice(self):
        zone=ZoneInfo('Pacific/Auckland')
        summer=local_instant(date(2026,1,1),'09:01',zone)
        winter=local_instant(date(2026,6,3),'11:55',zone)
        self.assertEqual(9,summer.hour)
        self.assertEqual(13*3600,summer.utcoffset().total_seconds())
        self.assertEqual(11,winter.hour)
        self.assertEqual(12*3600,winter.utcoffset().total_seconds())
        with self.assertRaisesRegex(DataError,'Ambiguous'):
            local_instant(date(2027,4,4),'02:09',zone)
        with self.assertRaises(DataError):
            local_instant(date(2026,9,27),'02:30',zone)

    def test_unresolved_2027_fallback_fold_is_not_guessed(self):
        raw=(ROOT/'datasets/tides/raw/Raglan-2027.csv').read_bytes()
        with self.assertRaisesRegex(DataError,'Ambiguous'):
            convert_csv(raw,'Raglan',URL,RETRIEVED)
        result=convert_csv(raw,'Raglan',URL,RETRIEVED,end=date(2027,1,3))
        self.assertEqual('2027-01-03',result['dataset']['coverage_end'])
        self.assertEqual(11,len(result['records']))

    def test_queries_are_offline_clock_guarded_and_match_real_prediction_examples(self):
        with tempfile.TemporaryDirectory() as temp:
            store=OfflineStore(Path(temp)/'offline.sqlite3')
            try:
                for path in ('raglan-2026.json','raglan-2027-jan-prefix.json'):
                    store.import_validated(*read_bundle(ROOT/'datasets/tides'/path))
                clock=SessionClock(True,wall=lambda:datetime(2026,6,3,10,tzinfo=ZoneInfo('Pacific/Auckland')).timestamp(),monotonic=lambda:0)
                with patch('socket.create_connection',side_effect=AssertionError('Network accessed')):
                    answer=' '.join(tide_reply(store,'RAGLAN',clock,'Pacific/Auckland'))
                self.assertIn('2026-06-03 11:55 NZST',answer)
                self.assertIn('2026-06-03 17:52 NZST',answer)
                self.assertIn('2.9 m',answer)
                self.assertIn('Clock unconfirmed',tide_reply(store,'Raglan',SessionClock(),'Pacific/Auckland'))
                self.assertIn('Usage:',tide_reply(store,'',clock,'Pacific/Auckland'))
                self.assertIn('No saved predictions',tide_reply(store,'No such coast',clock,'Pacific/Auckland'))
                unsupported=SessionClock(True,wall=lambda:datetime(2028,1,1,tzinfo=timezone.utc).timestamp(),monotonic=lambda:0)
                self.assertIn('No saved tide coverage',tide_reply(store,'Raglan',unsupported,'Pacific/Auckland'))
                boundary=SessionClock(True,wall=lambda:datetime(2026,12,31,23,tzinfo=ZoneInfo('Pacific/Auckland')).timestamp(),monotonic=lambda:0)
                self.assertIn('2027-01-01',' '.join(tide_reply(store,'Raglan',boundary,'Pacific/Auckland')))
            finally:
                store.close()

    def test_malformed_csv_cannot_produce_partial_valid_import(self):
        raw=(ROOT/'datasets/tides/raw/Raglan-2026.csv').read_bytes()
        with self.assertRaises(DataError):
            convert_csv(raw,'Another harbour',URL,RETRIEVED)
        with self.assertRaises(DataError):
            convert_csv(raw.replace(b'09:01,3.0',b'09:01,'), 'Raglan',URL,RETRIEVED)
        with self.assertRaises(DataError):
            convert_csv(raw.replace(b'2,Fr,1,2026',b'3,Fr,1,2026'),'Raglan',URL,RETRIEVED)

    def test_valid_utc_z_imports_preserve_instants_for_python39_compatibility(self):
        metadata,records=read_bundle(ROOT/'datasets/tides/raglan-2026.json')
        for record in records:
            record['time']=datetime.fromisoformat(record['time']).astimezone(timezone.utc).isoformat().replace('+00:00','Z')
        with tempfile.TemporaryDirectory() as temp:
            store=OfflineStore(Path(temp)/'offline.sqlite3')
            try:
                store.import_validated(metadata,records)
                clock=SessionClock(True,wall=lambda:datetime(2026,6,2,22,tzinfo=timezone.utc).timestamp(),monotonic=lambda:0)
                self.assertIn('2026-06-03 11:55 NZST',' '.join(tide_reply(store,'Raglan',clock,'Pacific/Auckland')))
            finally:store.close()
