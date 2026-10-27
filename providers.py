"""Provider-neutral boundary. A vendor adapter maps licensed feed messages here.
No vendor endpoint, authentication, or exchange calendar is assumed.
"""
from dataclasses import dataclass
from typing import Iterable, Protocol
from datetime import datetime
import json
import math
from engine import KARACHI

@dataclass(frozen=True)
class MarketFrame:
    symbol: str
    provider: str
    candle: dict
    book: dict | None = None

class MarketDataProvider(Protocol):
    def completed_frames(self) -> Iterable[MarketFrame]: ...

def normalize_frame(payload):
    symbol=str(payload['symbol']).upper()
    provider=str(payload['provider']).strip()
    if not symbol or len(symbol)>12 or not symbol.replace('-','').isalnum():
        raise ValueError('Invalid symbol.')
    if not provider or len(provider)>80 or provider in ('Simulated','CSV import'):
        raise ValueError('Supply a distinct provider name.')
    if payload.get('completed') is not True:
        raise ValueError('Only finalized candles may be ingested.')
    candle=payload['candle']
    timestamp=datetime.fromisoformat(candle['time'].replace('Z','+00:00'))
    if timestamp.tzinfo is None:raise ValueError('Timestamp needs a UTC offset.')
    bar={k:float(candle[k]) for k in ('open','high','low','close','volume')}
    if not all(math.isfinite(v) for v in bar.values()) or min(bar[k] for k in ('open','high','low','close'))<=0 or bar['volume']<0:
        raise ValueError('Invalid price or volume.')
    if bar['high']<max(bar['open'],bar['close']) or bar['low']>min(bar['open'],bar['close']) or bar['low']>bar['high']:
        raise ValueError('Inconsistent OHLC range.')
    bar['time']=timestamp.astimezone(KARACHI).isoformat()
    book=payload.get('book')
    if book is not None:
        bids=book['bids'];asks=book['asks']
        if not 1<=len(bids)<=10 or not 1<=len(asks)<=10:raise ValueError('Depth requires 1–10 levels per side.')
        for side in (bids,asks):
            for level in side:
                p=float(level['price']);s=float(level['size'])
                if not math.isfinite(p) or not math.isfinite(s) or p<=0 or s<0:raise ValueError('Invalid depth price/size.')
                level['price']=p;level['size']=s
        if any(bids[i]['price']<=bids[i+1]['price'] for i in range(len(bids)-1)) or any(asks[i]['price']>=asks[i+1]['price'] for i in range(len(asks)-1)) or bids[0]['price']>=asks[0]['price']:
            raise ValueError('Depth must be sorted, unique, and uncrossed.')
        # Adapter must align its depth snapshot to the completed candle timestamp.
        if book.get('time')!=candle['time']:raise ValueError('Depth snapshot time must match candle time.')
    return MarketFrame(symbol,provider,bar,book)

def ingest_frame(db,frame):
    """Persist a finalized frame; caller runs paper checks + signal transitions."""
    row=db.execute('SELECT * FROM instruments WHERE symbol=?',(frame.symbol,)).fetchone()
    if not row: raise ValueError('Import at least 35 historical candles for this symbol first.')
    bars=json.loads(row['bars'])[:row['cursor']]
    last=datetime.fromisoformat(bars[-1]['time'])
    incoming=datetime.fromisoformat(frame.candle['time'])
    if incoming<=last:raise ValueError('Feed candles must strictly follow stored history.')
    if row['source']!=frame.provider and db.execute('SELECT 1 FROM positions WHERE symbol=? AND closed IS NULL',(frame.symbol,)).fetchone():
        raise ValueError('Close open paper positions before changing a symbol’s data source.')
    if row['source']!=frame.provider:
        db.execute('DELETE FROM signals WHERE symbol=?',(frame.symbol,))
    bars.append(frame.candle)
    if len(bars)>20000:bars=bars[-20000:]
    db.execute('UPDATE instruments SET source=?,bars=?,cursor=? WHERE symbol=?',(frame.provider,json.dumps(bars),len(bars),frame.symbol))
    if frame.book:db.execute('INSERT OR REPLACE INTO books VALUES(?,?,?)',(frame.symbol,frame.candle['time'],json.dumps(frame.book)))
    else:db.execute('DELETE FROM books WHERE symbol=?',(frame.symbol,))
