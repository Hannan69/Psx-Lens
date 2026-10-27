import json,sys,tempfile,threading,unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
from market_service import availability,guidance,market_snapshot,MarketService
from engine import KARACHI
NOW=datetime(2026,10,19,11,tzinfo=KARACHI)
def market(open=True):return {'is_open':open,'checked_at':NOW.isoformat(),'demo_mode':False,'override_mode':'AUTO','error':None}
def meta():return {'interval':'5m','synced_at':NOW.isoformat(),'environment':'paper'}
def available(**kwargs):return availability(kwargs.get('source','pyPSX · 5m'),kwargs.get('time',(NOW-timedelta(minutes=6)).isoformat()),kwargs.get('meta',meta()),kwargs.get('market',market()),kwargs.get('error'),NOW)
class FreshnessTests(unittest.TestCase):
    def test_fresh_completed_candle(self):self.assertTrue(available()['actionable'])
    def test_simulated_data_never_current(self):self.assertFalse(available(source='Simulated')['actionable'])
    def test_imported_data_never_current(self):self.assertFalse(available(source='CSV import')['actionable'])
    def test_stale_candle_blocks(self):self.assertEqual(available(time=(NOW-timedelta(minutes=25)).isoformat())['state'],'stale')
    def test_future_candle_blocks(self):self.assertFalse(available(time=(NOW+timedelta(minutes=5)).isoformat())['actionable'])
    def test_sync_failure_blocks_even_recent_data(self):self.assertEqual(available(error='Update failed')['state'],'error')
    def test_market_closed_is_historical(self):self.assertEqual(available(market=market(False))['state'],'closed')
    def test_expired_market_status_blocks(self):
        m=market();m['checked_at']=(NOW-timedelta(minutes=4)).isoformat()
        self.assertEqual(available(market=m)['state'],'unknown')
    def test_demo_market_blocks(self):
        m=market();m['demo_mode']=True
        self.assertFalse(available(market=m)['actionable'])
        with self.assertRaises(ValueError):market_snapshot(m,NOW)
    def test_forced_market_blocks(self):
        m=market();m['override_mode']='OPEN'
        with self.assertRaises(ValueError):market_snapshot(m,NOW)
    def test_daily_no_intraday_guidance(self):
        m=meta();m['interval']='1d'
        self.assertEqual(available(meta=m)['state'],'historical')
    def test_old_refresh_blocks(self):
        m=meta();m['synced_at']=(NOW-timedelta(minutes=5)).isoformat()
        self.assertEqual(available(meta=m)['state'],'stale')
    def test_guidance_never_buys_on_stale_high_score(self):
        self.assertEqual(guidance({'score':100},available(error='Network failed'))['action'],'WAIT FOR DATA')
    def test_weak_setup_does_not_recommend_short(self):
        g=guidance({'score':10},available());self.assertEqual(g['action'],'AVOID NEW ENTRY');self.assertIn('not a short',g['new_buyer'])

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=server.DB;server.DB=Path(self.temp.name)/'test.sqlite3';server.init()
    def tearDown(self):server.DB=self.old;self.temp.cleanup()
    def bars(self,count=100):
        bars=server.demo_bars('BOP',count)
        delta=NOW-timedelta(minutes=6)-datetime.fromisoformat(bars[-1]['time'])
        return [{**b,'time':(datetime.fromisoformat(b['time'])+delta).isoformat()} for b in bars]
    def test_no_synthetic_seed_in_real_workspace(self):
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM instruments').fetchone()[0],0)
            s=server.state(db);self.assertTrue(all(x['price'] is None for x in s['stocks']))
    def test_synthetic_legacy_rows_not_displayed(self):
        server.init(demo=True)
        with server.connect() as db:
            server.set_value(db,'demo_mode',0)
            self.assertTrue(all(x['source']=='Unavailable' for x in server.state(db)['stocks']))
    def test_repeat_sync_no_duplicate_alerts(self):
        bars=self.bars()
        with server.connect() as db:
            server.apply_snapshot(db,'BOP',bars,meta());server.apply_snapshot(db,'BOP',bars,meta())
            self.assertEqual(db.execute('SELECT count(*) FROM alerts').fetchone()[0],0)
            self.assertEqual(server.instrument(db,'BOP')[0]['history_count'],100)
    def test_open_position_accepts_same_source_updates(self):
        bars=self.bars()
        with server.connect() as db:
            server.apply_snapshot(db,'BOP',bars,meta())
            # Model stop above next bar's low, but below previous entry.
            price=bars[-1]['close'];stop=price-.001
            db.execute('INSERT INTO positions(symbol,quantity,entry,stop,target,cost,source,opened) VALUES(?,?,?,?,?,?,?,?)',('BOP',10,price,stop,price+10,10*price,'pyPSX · 5m',bars[-1]['time']))
            nextbar={**bars[-1],'time':(datetime.fromisoformat(bars[-1]['time'])+timedelta(minutes=5)).isoformat(),'open':price,'high':price+.01,'low':price-.02,'close':price}
            server.apply_snapshot(db,'BOP',bars+[nextbar],meta())
            p=db.execute('SELECT * FROM positions').fetchone();self.assertIsNotNone(p['closed']);self.assertEqual(p['reason'],'Stop loss')
            first=p['pnl'];server.apply_snapshot(db,'BOP',bars+[nextbar],meta())
            self.assertEqual(db.execute('SELECT pnl FROM positions').fetchone()[0],first)
    def test_provider_correction_blocked_with_open_position(self):
        bars=self.bars()
        with server.connect() as db:
            server.apply_snapshot(db,'BOP',bars,meta())
            db.execute('INSERT INTO positions(symbol,quantity,entry,stop,target,cost,source,opened) VALUES(?,?,?,?,?,?,?,?)',('BOP',1,10,9,12,10,'pyPSX · 5m',bars[-1]['time']))
            changed=[dict(b) for b in bars];changed[-2]['volume']+=1
            with self.assertRaisesRegex(ValueError,'revised'):server.apply_snapshot(db,'BOP',changed,meta())
    def test_background_failure_preserves_data_and_sets_error(self):
        with server.connect() as db:server.apply_snapshot(db,'BOP',self.bars(),meta())
        service=MarketService(server.connect,server.LOCK,server.apply_snapshot)
        with patch('market_service.pypsx_provider.status',return_value={'configured':True}),patch('market_service.pypsx_provider.get_json',return_value={**market(),'status_text':'Market Open'}),patch('market_service.pypsx_provider.fetch_history',side_effect=ValueError('Provider unavailable')):
            service.cycle()
        with server.connect() as db:
            self.assertEqual(server.instrument(db,'BOP')[0]['history_count'],100)
            self.assertEqual(db.execute("SELECT error FROM watchlist WHERE symbol='BOP'").fetchone()[0],'Provider unavailable')
    def test_backwards_data_rejected(self):
        bars=self.bars()
        with server.connect() as db:
            server.apply_snapshot(db,'BOP',bars,meta())
            with self.assertRaisesRegex(ValueError,'backwards'):server.apply_snapshot(db,'BOP',bars[:-1],meta())
    def test_long_downtime_without_position_resets_baseline(self):
        bars=self.bars()
        with server.connect() as db:
            server.apply_snapshot(db,'BOP',bars,meta())
            later=[{**b,'time':(datetime.fromisoformat(b['time'])+timedelta(days=40)).isoformat()} for b in bars]
            server.apply_snapshot(db,'BOP',later,meta())
            self.assertEqual(server.instrument(db,'BOP')[1][-1]['time'],later[-1]['time'])
