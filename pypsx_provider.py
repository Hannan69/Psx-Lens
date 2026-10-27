"""Read-only adapter for pyPSX's documented REST API. No trading endpoints."""
import csv
import io
import json
import os
import re
import socket
from datetime import datetime, timedelta, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from engine import parse_csv, KARACHI

ROOT=Path(__file__).resolve().parent
HOSTS={'paper':'https://paper-api.pypsx.com','live':'https://api.pypsx.com'}
INTERVALS={'1m':1,'5m':5,'15m':15,'1h':60,'1d':1440}

def config():
    # Only named provider settings are read; credentials never reach browser JSON.
    keys=('PYPSX_API_KEY_ID','PYPSX_API_SECRET_KEY','PYPSX_ENVIRONMENT')
    values={}
    path=ROOT/'.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#') or '=' not in line:continue
            k,v=line.split('=',1)
            if k.strip() in keys:values[k.strip()]=v.strip().strip('"').strip("'")
    for k in keys:
        if os.environ.get(k):values[k]=os.environ[k]
    env=values.get('PYPSX_ENVIRONMENT','paper')
    if env not in HOSTS:raise ValueError('PYPSX_ENVIRONMENT must be paper or live.')
    return values,env

def status():
    values,env=config()
    configured=bool(values.get('PYPSX_API_KEY_ID') and values.get('PYPSX_API_SECRET_KEY'))
    return {'configured':configured,'environment':env,'mode':'Read-only market data','message':'Credentials configured; feed access still requires a successful sync.' if configured else 'Add pyPSX key and secret to the local .env file. No feed is connected.'}

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def get_json(path,params):
    values,env=config()
    if not status()['configured']:raise ValueError('pyPSX credentials are missing. Follow the local setup guide; do not paste secrets into chat.')
    request=Request(HOSTS[env]+path+'?'+urlencode(params),headers={'Accept':'application/json','User-Agent':'PSX-Lens-Research/0.2','PYPSX-API-KEY-ID':values['PYPSX_API_KEY_ID'],'PYPSX-API-SECRET-KEY':values['PYPSX_API_SECRET_KEY']})
    try:
        with build_opener(NoRedirect()).open(request,timeout=20) as response:
            body=response.read(10000001)
        if len(body)>10000000:raise ValueError('pyPSX response exceeds 10 MB. Use a shorter date range.')
        return json.loads(body)
    except HTTPError as e:
        if e.code in (401,403):raise ValueError(f'pyPSX returned HTTP {e.code}. Check account activation, key permissions, and provider access; no data was changed.') from None
        if e.code==429:raise ValueError('pyPSX rate limit reached. Wait at least 60 seconds before retrying.') from None
        raise ValueError(f'pyPSX returned HTTP {e.code}; no data was changed.') from None
    except (URLError,TimeoutError,socket.timeout):raise ValueError('Cannot reach pyPSX. Check connectivity or provider availability; no data was changed.') from None
    except (json.JSONDecodeError,UnicodeError):raise ValueError('pyPSX returned an invalid JSON response; no data was changed.') from None

def normalize_history(payload,symbol,interval,now=None):
    """Accept documented intraday tuples and named OHLCV daily records; fail closed."""
    now=now or datetime.now(KARACHI)
    if isinstance(payload,dict) and 'results' in payload:
        results=[r for r in payload['results'] if r.get('symbol')==symbol and r.get('interval',interval)==interval]
        if len(results)!=1:raise ValueError('pyPSX response does not contain exactly one matching symbol/interval.')
        block=results[0]
        if block.get('gaps'):raise ValueError('pyPSX reports gaps in this history. Choose a date range outside the gaps.')
        rows=block.get('candles')
    elif isinstance(payload,list):rows=payload
    elif isinstance(payload,dict):
        rows=payload.get('candles',payload.get('bars',payload.get('data')))
        if isinstance(rows,dict):rows=rows.get('candles',rows.get('bars'))
    else:rows=None
    if not isinstance(rows,list) or len(rows)>20000:raise ValueError('Unsupported pyPSX candle response. Expected up to 20,000 OHLCV records.')
    normalized=[];omitted=0
    for raw in rows:
        if isinstance(raw,(list,tuple)) and len(raw)==6:
            row=dict(zip(('time','open','high','low','close','volume'),raw))
        elif isinstance(raw,dict):
            row={str(k).lower():v for k,v in raw.items()}
            row['time']=row.get('time',row.get('timestamp',row.get('date')))
            if row.get('symbol',symbol)!=symbol:raise ValueError('pyPSX returned a different symbol.')
        else:raise ValueError('Unsupported pyPSX candle record.')
        raw_time=str(row['time'])
        timestamp=datetime.fromisoformat(raw_time.replace('Z','+00:00'))
        if interval=='1d':
            # Daily date is a session label, not an intraday execution timestamp.
            if len(raw_time)==10:timestamp=datetime.combine(timestamp.date(),time(0),KARACHI)
            elif timestamp.tzinfo is None:timestamp=timestamp.replace(tzinfo=KARACHI)
            timestamp=timestamp.astimezone(KARACHI)
            if timestamp.date()>=now.astimezone(KARACHI).date():omitted+=1;continue
        else:
            if timestamp.tzinfo is None:raise ValueError('Intraday candle timestamp must include a UTC offset.')
            timestamp=timestamp.astimezone(KARACHI)
            # Conservative: allow a full bar duration plus one-minute publication buffer.
            if timestamp+timedelta(minutes=INTERVALS[interval]+1)>now:omitted+=1;continue
        if 'high' not in row or 'low' not in row:raise ValueError('pyPSX history lacks high/low prices. Cannot calculate ATR or test stops.')
        try:normalized.append({'symbol':symbol,'time':timestamp.isoformat(),**{k:row[k] for k in ('open','high','low','close','volume')}})
        except KeyError:raise ValueError('pyPSX history lacks required OHLCV fields.') from None
    if len(normalized)<35:raise ValueError(f'pyPSX returned only {len(normalized)} completed candles for {symbol}. At least 35 are needed; check the ticker or request a longer history.')
    normalized.sort(key=lambda r:r['time'])
    stream=io.StringIO();w=csv.DictWriter(stream,fieldnames=['symbol','time','open','high','low','close','volume']);w.writeheader();w.writerows(normalized)
    _,bars=parse_csv(stream.getvalue())
    return bars,omitted

def fetch_history(symbol,interval='5m',days=20):
    symbol=symbol.strip().upper()
    if not re.fullmatch(r'[A-Z0-9-]{1,12}',symbol):raise ValueError('Invalid PSX symbol.')
    if interval not in INTERVALS:raise ValueError('Choose 1m, 5m, 15m, 1h, or 1d.')
    days=int(days)
    if not 7<=days<=365:raise ValueError('Choose 7–365 calendar days.')
    now=datetime.now(KARACHI);start=now-timedelta(days=days)
    if interval=='1d':payload=get_json('/historical/'+symbol,{'start':start.date().isoformat(),'end':now.date().isoformat(),'interval':'1d'})
    else:payload=get_json('/api/v1/market/historical-intraday',{'symbol':symbol,'from':start.isoformat(),'to':now.isoformat(),'interval':interval})
    bars,omitted=normalize_history(payload,symbol,interval,now)
    return bars,{'interval':interval,'synced_at':now.isoformat(),'omitted_incomplete':omitted,'environment':config()[1]}
