import os, time, requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

MEXC_FAPI = "https://contract.mexc.com"
BINANCE = "https://api.binance.com"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"

def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def jget(url, params=None, retries=3, timeout=12):
    for _ in range(retries):
        try:
            r=requests.get(url, params=params, timeout=timeout)
            if r.status_code==200: return r.json()
        except: time.sleep(0.6)
    return None

def telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID: print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown"})
    except: pass

# ---- indic ----
def ema(x,n): return x.ewm(span=n, adjust=False).mean()
def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    rs=up.ewm(alpha=1/n, adjust=False).mean()/(dn.ewm(alpha=1/n, adjust=False).mean()+1e-12)
    return 100-(100/(1+rs))
def macd(s,f=12,m=26,sig=9):
    fast=ema(s,f); slow=ema(s,m); line=fast-slow; signal=line.ewm(span=sig, adjust=False).mean()
    return line, signal, line-signal
def adx(df,n=14):
    up=df['high'].diff(); dn=-df['low'].diff()
    plus=np.where((up>dn)&(up>0),up,0.0); minus=np.where((dn>up)&(dn>0),dn,0.0)
    tr1=df['high']-df['low']; tr2=(df['high']-df['close'].shift()).abs(); tr3=(df['low']-df['close'].shift()).abs()
    tr=pd.DataFrame({'a':tr1,'b':tr2,'c':tr3}).max(axis=1)
    atr=tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di=100*pd.Series(plus).ewm(alpha=1/n, adjust=False).mean()/(atr+1e-12)
    minus_di=100*pd.Series(minus).ewm(alpha=1/n, adjust=False).mean()/(atr+1e-12)
    dx=((plus_di-minus_di).abs()/((plus_di+minus_di)+1e-12))*100
    return dx.ewm(alpha=1/n, adjust=False).mean()
def bos_up(df,look=40,excl=2):
    hh=df['high'][:-excl].tail(look).max()
    return df['close'].iloc[-1]>hh
def bos_dn(df,look=40,excl=2):
    ll=df['low'][:-excl].tail(look).min()
    return df['close'].iloc[-1]<ll
def volume_spike(df,n=20,r=1.5):
    if len(df)<n+2: return False,1.0
    last=df['volume'].iloc[-1]; base=df['volume'].iloc[-(n+1):-1].mean()
    ratio=last/(base+1e-12); return ratio>=r, ratio

# ---- market notes ----
def coin_state(interval="4h", symbol="BTCUSDT"):
    d=jget(f"{BINANCE}/api/v3/klines",{"symbol":symbol,"interval":interval,"limit":300})
    if not d: return "NÖTR"
    df=pd.DataFrame(d,columns=["t","o","h","l","c","v","ct","x1","x2","x3","x4","x5"]).astype(float)
    c=df['c']; e20,e50=ema(c,20).iloc[-1], ema(c,50).iloc[-1]; rr=rsi(c,14).iloc[-1]
    if e20>e50 and rr>50: return "GÜÇLÜ"
    if e20<e50 and rr<50: return "ZAYIF"
    return "NÖTR"

def btc_eth_state(): return coin_state("4h","BTCUSDT"), coin_state("4h","ETHUSDT")

def market_note():
    g=jget(COINGECKO_GLOBAL)
    try:
        total_pct=float(g["data"]["market_cap_change_percentage_24h_usd"])
        btc_dom=float(g["data"]["market_cap_percentage"]["btc"])
        usdt_dom=float(g["data"]["market_cap_percentage"]["usdt"])
    except: return "Piyasa: veri alınamadı."
    tkr=jget(f"{BINANCE}/api/v3/ticker/24hr",{"symbol":"BTCUSDT"})
    try: btc_pct=float(tkr["priceChangePercent"])
    except: btc_pct=None
    arrow="↑" if (btc_pct is not None and btc_pct>total_pct) else ("↓" if (btc_pct is not None and btc_pct<total_pct) else "→")
    dirb="↑" if (btc_pct is not None and btc_pct>0) else ("↓" if (btc_pct is not None and btc_pct<0) else "→")
    total2 = "↑ (Altlara giriş)" if arrow=="↓" and total_pct>=0 else ("↓ (Çıkış)" if arrow=="↑" and total_pct<=0 else "→ (Karışık)")
    usdt_note=f"{usdt_dom:.1f}%"
    if usdt_dom>=7.0: usdt_note+=" (riskten kaçış)"
    elif usdt_dom<=5.0: usdt_note+=" (risk alımı)"
    return f"Piyasa: BTC {dirb} + BTC.D {arrow} (BTC.D {btc_dom:.1f}%) | Total2: {total2} | USDT.D: {usdt_note}"

# ---- mexc ----
def mexc_symbols():
    d=jget(f"{MEXC_FAPI}/api/v1/contract/detail")
    if not d or "data" not in d: return []
    return [s["symbol"] for s in d["data"] if s.get("quoteCoin")=="USDT"]

def klines_mexc(sym, interval="4h", limit=260):
    d=jget(f"{MEXC_FAPI}/api/v1/contract/kline/{sym}",{"interval":interval,"limit":limit})
    if not d or "data" not in d: return None
    df=pd.DataFrame(d["data"],columns=["ts","open","high","low","close","volume","turnover"]).astype(
        {"open":"float64","high":"float64","low":"float64","close":"float64","volume":"float64","turnover":"float64"}
    ); return df

def funding_rate(sym):
    d=jget(f"{MEXC_FAPI}/api/v1/contract/funding_rate",{"symbol":sym})
    try: return float(d["data"]["fundingRate"])
    except: return None

# ---- analysis ----
def analyze(sym):
    df=klines_mexc(sym,"4h",260)
    if df is None or len(df)<120: return None,"short"
    if float(df["turnover"].iloc[-1])<1_000_000: return None,"lowliq"

    c=df['close']; h=df['high']; l=df['low']
    # GAP filtresi %8
    if abs(float(c.iloc[-1]/c.iloc[-2]-1))>0.08: return None,"gap"

    e20,e50=ema(c,20).iloc[-1], ema(c,50).iloc[-1]
    trend_up=e20>e50
    rr=float(rsi(c,14).iloc[-1])
    m,ms,_=macd(c); macd_up=m.iloc[-1]>ms.iloc[-1]; macd_dn=not macd_up
    av=float(adx(pd.DataFrame({'high':h,'low':l,'close':c}),14).iloc[-1]); strong=av>=15
    bosU,bosD=bos_up(df), bos_dn(df)
    v_ok, v_ratio=volume_spike(df, n=20, r=1.5)
    if not v_ok: return None,"novol"

    last_down=float(c.iloc[-1])<float(c.iloc[-2]); sell_vol=last_down and v_ok

    side=None
    if trend_up and rr>52 and macd_up and strong:
        side="BUY"; bos_flag=bosU
    elif (not trend_up) and rr<48 and macd_dn and strong and (bosD or sell_vol):
        side="SELL"; bos_flag=bosD
    else: return None,None

    fr=funding_rate(sym); frtxt=""
    if fr is not None:
        if fr>0.01: frtxt=f" | Funding:+{fr:.3f}"
        elif fr<-0.01: frtxt=f" | Funding:{fr:.3f}"

    line=f"{sym} | Trend:{'↑' if trend_up else '↓'} | RSI:{rr:.1f} | Hacim x{v_ratio:.2f} | ADX:{av:.0f} | BoS:{'↑' if bosU else ('↓' if bosD else '-')} | Fiyat:{float(c.iloc[-1])}{frtxt}"
    return (side,line),None

def main():
    btc_s, eth_s = btc_eth_state()
    note = market_note()

    syms=mexc_symbols()
    if not syms: telegram("⚠️ Sembol listesi alınamadı (MEXC)."); return

    buys,sells=[],[]
    skipped={"lowliq":0,"gap":0,"novol":0,"short":0}
    for i,s in enumerate(syms):
        try:
            res,flag=analyze(s)
            if flag in skipped: skipped[flag]+=1
            if res:
                side,line=res
                (buys if side=="BUY" else sells).append(f"- {line}")
        except: pass
        if i%15==0: time.sleep(0.3)

    parts=[f"🟣 *4H Orta Vade Sinyaller*\n⏱ {ts()}\nBTC: {btc_s} | ETH: {eth_s}\n{note}"]
    if buys: parts+=["\n🟢 *BUY:*"]+buys[:25]
    if sells: parts+=["\n🔴 *SELL:*"]+sells[:25]
    if not buys and not sells: parts.append("\nℹ️ Şu an 4H kriterlerine uyan sinyal yok.")
    parts.append(f"\n📊 Özet: BUY:{len(buys)} | SELL:{len(sells)} | Atlanan (likidite:{skipped['lowliq']}, gap:{skipped['gap']}, hacim:{skipped['novol']})")
    telegram("\n".join(parts))

if __name__=="__main__": main()
