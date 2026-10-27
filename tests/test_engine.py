import csv, io, math, sys, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import engine, server

class EngineTests(unittest.TestCase):
    def setUp(self): self.bars=server.demo_bars('BOP',100)
    def csv(self,bars):
        out=io.StringIO();w=csv.DictWriter(out,fieldnames=['symbol','time','open','high','low','close','volume']);w.writeheader()
        for b in bars:w.writerow({'symbol':'BOP',**b})
        return out.getvalue()
    def test_flat_market_is_finite(self):
        bars=[{**b,'open':10,'close':10,'high':10,'low':10,'volume':0} for b in self.bars]
        f=engine.features(bars)
        self.assertEqual(f['rsi'],50)
        self.assertIsNone(f['vwap']);self.assertIsNone(f['volume_ratio'])
        self.assertTrue(math.isfinite(f['score']))
    def test_missing_depth_excluded(self):
        f=engine.features(self.bars)
        self.assertEqual(f['coverage'],85)
        self.assertIsNone(f['components'][-1]['value'])
        expected=round(sum(c['weight']*c['value'] for c in f['components'][:-1])/85*100)
        self.assertEqual(f['score'],expected)
    def test_vwap_resets_at_session(self):
        bars=self.bars[:-1]+[{**self.bars[-1],'time':'2026-09-17T09:30:00+05:00','open':12,'high':14,'low':10,'close':12,'volume':100}]
        self.assertEqual(engine.features(bars)['vwap'],12)
    def test_sizing_respects_cash_and_costs(self):
        r=engine.position_size(100000,1,100,99,10)
        self.assertLessEqual(r['risk_amount'],1000)
        self.assertLessEqual(r['capital_used'],100000)
        self.assertGreater((r['quantity']+1)*(1+199*.001),1000)
    def test_sizing_rejects_nonfinite(self):
        with self.assertRaises(ValueError): engine.position_size(float('nan'),1,10,9)
    def test_csv_roundtrip(self):
        symbol,bars=engine.parse_csv(self.csv(self.bars))
        self.assertEqual(symbol,'BOP');self.assertEqual(bars,self.bars)
    def test_csv_rejects_bad_ohlc(self):
        bars=[dict(b) for b in self.bars];bars[50]['high']=.01
        with self.assertRaisesRegex(ValueError,'OHLC'):engine.parse_csv(self.csv(bars))
    def test_csv_rejects_duplicate_time(self):
        bars=[dict(b) for b in self.bars];bars[50]['time']=bars[49]['time']
        with self.assertRaisesRegex(ValueError,'ascending'):engine.parse_csv(self.csv(bars))
    def test_csv_rejects_naive_timestamp(self):
        bars=[dict(b) for b in self.bars];bars[0]['time']=bars[0]['time'][:-6]
        with self.assertRaisesRegex(ValueError,'offset'):engine.parse_csv(self.csv(bars))
    def test_next_open_and_same_bar_stop_first(self):
        bars=[{**b,'open':100,'high':100,'low':100,'close':100} for b in self.bars]
        bars[36].update(open=101,low=98,high=105,close=102)
        def fake(history,book=None):return {'score':70 if len(history)==36 else 0,'risk':{'risk_per_share':1}}
        with patch('engine.features',fake):r=engine.backtest(bars,fee_bps=0,slippage_bps=0)
        t=r['trades'][0]
        self.assertEqual(t['entry'],101);self.assertEqual(t['entry_time'],bars[36]['time'])
        self.assertEqual(t['exit'],100);self.assertEqual(t['reason'],'Stop loss')
    def test_gap_stop_and_costs(self):
        bars=[{**b,'open':100,'high':100.1,'low':99.9,'close':100} for b in self.bars]
        bars[37].update(open=95,high=96,low=94,close=95)
        def fake(history,book=None):return {'score':70 if len(history)==36 else 50,'risk':{'risk_per_share':2}}
        with patch('engine.features',fake):r=engine.backtest(bars,fee_bps=10,slippage_bps=5)
        t=r['trades'][0]
        self.assertEqual(t['reason'],'Gap below stop');self.assertAlmostEqual(t['exit'],95*.9995)
        self.assertLess(t['pnl'],(95-100)*t['quantity'])
    def test_causal_trade_prefix(self):
        bars=server.demo_bars('BOP',160)
        a=engine.backtest(bars[:100]);b=engine.backtest(bars)
        cutoff=bars[98]['time']
        self.assertEqual([t for t in a['trades'] if t['exit_time']<=cutoff],[t for t in b['trades'] if t['exit_time']<=cutoff])
        self.assertEqual(a['equity'][:-1],b['equity'][:len(a['equity'])-1])
    def test_backtest_accounting(self):
        r=engine.backtest(server.demo_bars('BOP',300))
        self.assertAlmostEqual(r['initial']+sum(t['pnl'] for t in r['trades']),r['final'])
        self.assertTrue(all(t['quantity']>0 for t in r['trades']))
if __name__=='__main__':unittest.main()
