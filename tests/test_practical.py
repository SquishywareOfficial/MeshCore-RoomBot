from datetime import date,datetime,timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from room_bot.clock import SessionClock
from room_bot.data import OfflineStore,read_bundle
from room_bot.practical import calc_reply,daylight,sun_reply


class PracticalTests(unittest.TestCase):
    def test_exact_unit_conversions_and_explicit_battery_assumptions(self):
        self.assertIn('1 miles',calc_reply(['km-miles','1.609344']))
        self.assertIn('1.60934 km',calc_reply(['miles-km','1']))
        self.assertIn('32 F',calc_reply(['c-f','0']))
        self.assertIn('100 C',calc_reply(['f-c','212']))
        self.assertIn('3.7 Wh',calc_reply(['energy','1000','3.7']))
        self.assertIn('Ideal runtime 100 h',calc_reply(['runtime','1000','10']))
        self.assertIn('240 mAh',calc_reply(['daily-load','10']))
        self.assertIn('500 mAh',calc_reply(['solar-input','100','5']))
        self.assertIn('Not a net solar-power budget',calc_reply(['solar-input','100','5']))

    def test_invalid_and_code_like_inputs_are_not_executed(self):
        for values in (['runtime','1000','0'],['energy','-1','3.7'],['daily-load','nan'],
                       ['solar-input','100','25'],['c-f','inf'],['runtime','1000','__import__("os")']):
            self.assertIn('Use finite',calc_reply(values))
        self.assertIn('Calc:',calc_reply(['eval','1+1']))

    @unittest.skipUnless(importlib.util.find_spec('astral'),'Optional astronomy dependency not installed')
    def test_published_astral_example_matches_without_network(self):
        with patch('socket.create_connection',side_effect=AssertionError('Network accessed')):
            rise,setting=daylight(51.5,-0.116,date(2009,4,22),'UTC')
        expected_rise=datetime(2009,4,22,4,50,tzinfo=timezone.utc)
        expected_set=datetime(2009,4,22,19,8,tzinfo=timezone.utc)
        self.assertLess(abs((rise-expected_rise).total_seconds()),120)
        self.assertLess(abs((setting-expected_set).total_seconds()),120)

    @unittest.skipUnless(importlib.util.find_spec('astral'),'Optional astronomy dependency not installed')
    def test_nz_default_location_and_unconfirmed_clock_behaviour(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            store=OfflineStore(Path(temp)/'offline.sqlite3')
            try:
                store.import_validated(*read_bundle(root/'datasets/places/linz-settlements-2026-10-13.json'))
                clock=SessionClock(True,wall=lambda:datetime(2026,1,1,0,tzinfo=timezone.utc).timestamp(),monotonic=lambda:0)
                result=' '.join(sun_reply(store,'Hamilton',clock,'Pacific/Auckland'))
                self.assertIn('Hamilton, calculated 2026-01-01',result)
                self.assertIn('NZDT',result)
                self.assertIn('weather/terrain not modelled',result)
                self.assertIn('Clock unconfirmed',sun_reply(store,'Hamilton',SessionClock(),'Pacific/Auckland'))
            finally:store.close()
