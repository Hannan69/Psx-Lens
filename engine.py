"""Auditable research engine. Scores are heuristic, never probabilities."""
import csv
import io
import math
from datetime import datetime
from zoneinfo import ZoneInfo

KARACHI = ZoneInfo('Asia/Karachi')
STRATEGY = 'research-v1'

def ema(values, period):
    result = [values[0]]
    alpha = 2 / (period + 1)
    for x in values[1:]:
        result.append(result[-1] + alpha * (x - result[-1]))
    return result

def wilder(values, period):
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = (value * (period - 1) + x) / period
    return value

def features(bars, book=None, timeframe="intraday"):
    if len(bars) < 35:
        raise ValueError('At least 35 completed candles are required.')
    close = [b['close'] for b in bars]
    fast, slow = ema(close, 9), ema(close, 21)
    macd = [a-b for a,b in zip(ema(close,12), ema(close,26))]
    hist = macd[-1] - ema(macd,9)[-1]
    diffs = [b-a for a,b in zip(close,close[1:])]
    gain = wilder([max(d,0) for d in diffs],14)
    loss = wilder([max(-d,0) for d in diffs],14)
    rsi = 50 if gain == loss == 0 else (100 if loss == 0 else 100-100/(1+gain/loss))
    tr = [max(b['high']-b['low'],abs(b['high']-p['close']),abs(b['low']-p['close'])) for p,b in zip(bars,bars[1:])]
    atr = wilder(tr,14)
    session = [b for b in bars if b['time'][:10] == bars[-1]['time'][:10]]
    volume = sum(b['volume'] for b in session)
    vwap = sum((b['high']+b['low']+b['close'])/3*b['volume'] for b in session)/volume if volume else None
    if timeframe == "1d": vwap = None
    baseline = sum(b['volume'] for b in bars[-21:-1])/20
    ratio = bars[-1]['volume']/baseline if baseline else None
    support = min(b['low'] for b in bars[-21:-1])
    resistance = max(b['high'] for b in bars[-21:-1])
    imbalance = None
    if book:
        bid = sum(b['size'] for b in book['bids'])
        ask = sum(b['size'] for b in book['asks'])
        if bid+ask: imbalance = bid/(bid+ask)
    components = [
        {'name':'Trend','weight':25,'value':1 if close[-1]>fast[-1]>slow[-1] else (0 if close[-1]<fast[-1]<slow[-1] else .5),'detail':f'EMA 9 {fast[-1]:.2f} · EMA 21 {slow[-1]:.2f}'},
        {'name':'Volume confirmation','weight':15,'value':min(ratio/2,1) if ratio is not None and close[-1]>close[-2] else (0 if ratio is not None else None),'detail':f'{ratio:.2f}× previous 20 bars' if ratio is not None else 'No volume baseline'},
        {'name':'Session VWAP','weight':15,'value':float(close[-1]>vwap) if vwap is not None else None,'detail':f'VWAP {vwap:.2f}' if vwap is not None else ('Unavailable on daily candles' if timeframe == '1d' else 'No session volume')},
        {'name':'Momentum','weight':20,'value':1 if hist>0 and 50<=rsi<75 else (0 if hist<0 and rsi<50 else .5),'detail':f'RSI {rsi:.1f} · MACD histogram {hist:.4f}'},
        {'name':'Breakout','weight':10,'value':1 if close[-1]>resistance else (0 if close[-1]<support else .5),'detail':f'Prior 20-bar range {support:.2f}–{resistance:.2f}'},
        {'name':'Order-book pressure','weight':15,'value':imbalance,'detail':f'{imbalance*100:.1f}% bid depth' if imbalance is not None else 'Unavailable · excluded from score'},
    ]
    available = sum(c['weight'] for c in components if c['value'] is not None)
    score = round(100*sum(c['weight']*c['value'] for c in components if c['value'] is not None)/available)
    signal = 'STRONG SETUP' if score>=80 else 'BUY WATCH' if score>=65 else 'WAIT' if score>=45 else 'EXIT WATCH' if score>=30 else 'BEARISH'
    price = close[-1]
    risk = max(1.5*atr,price*.005)
    return {'strategy':STRATEGY,'score':score,'signal':signal,'coverage':available,'components':components,'price':price,'ema9':fast[-1],'ema21':slow[-1],'rsi':rsi,'macd_hist':hist,'vwap':vwap,'atr':atr,'volume_ratio':ratio,'support':support,'resistance':resistance,'imbalance':imbalance,'risk':{'entry_low':round(price-atr*.25,4),'entry_high':round(price+atr*.25,4),'stop':round(price-risk,4),'target1':round(price+2*risk,4),'target2':round(price+3*risk,4),'risk_per_share':risk}}

def position_size(capital, risk_pct, entry, stop, cost_bps=10):
    if not all(math.isfinite(x) for x in (capital,risk_pct,entry,stop,cost_bps)) or capital<=0 or not 0<risk_pct<=5 or not 0<stop<entry or not 0<=cost_bps<=100:
        raise ValueError('Use positive capital, 0–5% risk, a stop below entry, and 0–100 bps costs.')
    cost = cost_bps/10000
    risk = entry-stop + cost*(entry+stop)
    quantity = max(0,min(math.floor(capital*risk_pct/100/risk), math.floor(capital/(entry*(1+cost)))))
    return {'quantity':quantity,'capital_used':quantity*entry*(1+cost),'risk_amount':quantity*risk,'risk_budget':capital*risk_pct/100}

def parse_csv(content):
    rows = list(csv.DictReader(io.StringIO(content)))
    if not 35<=len(rows)<=20000:
        raise ValueError('Import between 35 and 20,000 candles.')
    result=[]
    prev=None
    symbols=set()
    for i,row in enumerate(rows,2):
        try:
            timestamp=datetime.fromisoformat(row['time'].replace('Z','+00:00'))
            if timestamp.tzinfo is None: raise ValueError('Timestamp needs a UTC offset')
            timestamp=timestamp.astimezone(KARACHI)
            if prev and timestamp<=prev: raise ValueError('Timestamps must be unique and ascending')
            symbol=row['symbol'].strip().upper()
            if not symbol or len(symbol)>12 or not symbol.replace('-','').isalnum(): raise ValueError('Invalid symbol')
            symbols.add(symbol)
            b={k:float(row[k]) for k in ('open','high','low','close','volume')}
            if not all(math.isfinite(v) for v in b.values()) or min(b[k] for k in ('open','high','low','close'))<=0 or b['volume']<0: raise ValueError('Invalid price or volume')
            if b['high']<max(b['open'],b['close']) or b['low']>min(b['open'],b['close']) or b['low']>b['high']: raise ValueError('Inconsistent OHLC range')
            b['time']=timestamp.isoformat()
            result.append(b)
            prev=timestamp
        except (ValueError,KeyError,TypeError) as e:
            raise ValueError(f'CSV row {i}: {e}') from e
    if len(symbols)!=1: raise ValueError('Import one symbol per file.')
    return symbols.pop(),result

def backtest(bars, capital=100000, risk_pct=1, fee_bps=10, slippage_bps=5, entry_score=65, exit_score=45, timeframe='intraday'):
    """Close-based signals execute at next open. Conservative stop-first OHLC fills."""
    if len(bars)<70: raise ValueError('At least 70 candles are required for a backtest.')
    if not 45<=entry_score<=100 or not 0<=exit_score<entry_score: raise ValueError('Invalid signal thresholds.')
    if not all(math.isfinite(x) for x in (capital,risk_pct,fee_bps,slippage_bps)) or not 0<=slippage_bps<=100:
        raise ValueError('Invalid backtest parameters.')
    position_size(capital,risk_pct,bars[0]['close'],bars[0]['close']*.99,fee_bps)
    cash=capital
    position=None
    pending=None
    trades=[]
    equity=[]
    peak=capital
    drawdown=0
    fee=fee_bps/10000
    slip=slippage_bps/10000
    def sell(raw,reason,time):
        nonlocal cash,position
        price=raw*(1-slip)
        proceeds=position['quantity']*price*(1-fee)
        cash+=proceeds
        trades.append({**position,'exit':price,'exit_time':time,'pnl':proceeds-position['cost'],'reason':reason})
        position=None
    for i in range(35,len(bars)):
        b=bars[i]
        if pending=='exit' and position: sell(b['open'],'Signal exit',b['time'])
        elif isinstance(pending,dict) and not position:
            entry=b['open']*(1+slip)
            stop=entry-pending['distance']
            if stop>0:
                # Include exit slippage in the sizing risk.
                quantity=position_size(cash,risk_pct,entry,stop*(1-slip),fee_bps)['quantity']
                if quantity:
                    cost=quantity*entry*(1+fee)
                    cash-=cost
                    position={'entry':entry,'entry_time':b['time'],'quantity':quantity,'stop':stop,'target':entry+2*pending['distance'],'cost':cost}
        pending=None
        if position:
            if b['open']<=position['stop']: sell(b['open'],'Gap below stop',b['time'])
            elif b['low']<=position['stop']: sell(position['stop'],'Stop loss',b['time'])
            elif b['high']>=position['target']: sell(max(b['open'],position['target']),'Target 2R',b['time'])
        f=features(bars[max(0,i-300):i+1],timeframe='1d') if timeframe=='1d' else features(bars[max(0,i-300):i+1]) # Historical book not inferred from candles.
        if position and f['score']<exit_score: pending='exit'
        elif not position and f['score']>=entry_score: pending={'distance':f['risk']['risk_per_share']}
        mark=cash+(position['quantity']*b['close']*(1-slip)*(1-fee) if position else 0)
        peak=max(peak,mark)
        drawdown=max(drawdown,(peak-mark)/peak*100)
        equity.append({'time':b['time'],'value':round(mark,2)})
    if position:
        sell(bars[-1]['close'],'End of sample',bars[-1]['time'])
        equity[-1]['value']=round(cash,2)
    wins=[t['pnl'] for t in trades if t['pnl']>0]
    losses=[-t['pnl'] for t in trades if t['pnl']<0]
    return {'strategy':STRATEGY,'initial':capital,'final':cash,'return_pct':(cash/capital-1)*100,'max_drawdown':drawdown,'win_rate':len(wins)/len(trades)*100 if trades else None,'profit_factor':sum(wins)/sum(losses) if losses else None,'trades':trades,'equity':equity,'bars':len(bars),'start':bars[0]['time'],'end':bars[-1]['time'],'assumptions':'Long-only; no leverage; next-bar open entry/exit; stop-first if stop and target touch the same bar; gap-aware stops; per-side fees and slippage; order book excluded. No corporate-action adjustment or holdout validation.'}
