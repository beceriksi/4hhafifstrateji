import os, time, requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN"); CHAT_ID=os.getenv("CHAT_ID")
MEXC="https://futures.mexc.com"   # ✅ yeni base URL
BINANCE="https://api.binance.com"
COINGECKO="https://api.coingecko.com/api/v3/global"

def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def jget(url, params=None, retries=3, timeout=12):
    for _ in range(retries):
        try:
            r=requests.get(url, params=params, timeout=timeout)
            if r.status_code==200: return r.json()
        except: time.sleep(0.5)
    return None

def telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID: print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown"})
    except: pass

# ----- indikatörler -----
def ema(x,n): return x.ewm(span=n, adjust=False).mean()
def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    rs=up.ewm(alpha=1/n, adjust=False).mean()/(dn.ewm(alpha=1/n, adjust=False).mean()+1e-12)
    return 100-(100/(1+rs))
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

def volume_spike(df, n=20, r=1.20):
    """turnover yok, volume (‘v’) ile çalışıyor"""
    t=df['v'].astype(float)
    base_ema=t.ewm(span=n, adjust=False).mean()
    ratio=float(t.iloc[-1]/(base_ema.iloc[-2]+1e-12))
    roll=t.rolling(n)
    mu=np.log((roll.median().iloc[-1] or 1e-12)+1e-12)
    sd=np.log((roll.std().iloc[-1] or 1e-12)+1e-12)
    z=(np.log(t.iloc[-1]+1e-12)-mu)/(sd+1e-12)
    ramp=float(t.iloc[-3:].sum()/((roll.mean().iloc[-1]*3)+1e-12))
    ok=(ratio>=r) or (z>=1.0) or (ramp>=1.5)
    return ok, {"ratio":ratio,"z":z,"ramp":ramp}

# ----- piyasa bilgisi -----
def market_note():
    g=jget(COINGECKO)
    try:
        total=float(g["data"]["market_cap_change_percentage_24h_usd"])
        btcd=float(g["data"]["market_cap_percentage"]["btc"])
        usdt=float(g["data"]["market_cap_percentage"]["usdt"])
    except: return "Piyasa: veri alınamadı."
    tkr=jget(f"{BINANCE}/api/v3/ticker/24hr",{"symbol":"BTCUSDT"})
    btc=float(tkr["priceChangePercent"]) if tkr and "priceChangePercent" in tkr else None
    arrow="↑" if (btc is not None and btc>total) else ("↓" if (btc is not None and btc<total) else "→")
    dirb ="↑" if (btc is not None and btc>0) else ("↓" if (btc is not None and btc<0) else "→")
    total2="↑ (Altlara giriş)" if arrow=="↓" and total>=0 else ("↓ (Çıkış)" if arrow=="↑" and total<=0 else "→ (Karışık)")
    usdt_note=f"{usdt:.1f}%"
    if usdt>=7: usdt_note+=" (riskten kaçış)"
    elif usdt<=5: usdt_note+=" (risk alımı)"
    return f"Piyasa: BTC {dirb} + BTC.D {arrow} (BTC.D {btcd:.1f}%) | Total2: {total2} | USDT.D: {usdt_note}"

# ----- MEXC verileri -----
def mexc_symbols():
    d=jget(f"{MEXC}/api/v1/contract/detail")
    if not d or "data" not in d: return []
    return [s["symbol"] for s in d["data"] if s.get("quoteCoin")=="USDT"]

def klines(sym, interval="4h", limit=200):
    """✅ yeni endpoint"""
    d=jget(f"{MEXC}/api/v1/contract/kline", {"symbol":sym, "interval":interval, "limit":limit})
    if not d or "data" not in d: return None
    try:
        df=pd.DataFrame(d["data"],columns=["ts","open","high","low","close","v"]).astype(
            {"open":"float64","high":"float64","low":"float64","close":"float64","v":"float64"})
        df=df.rename(columns={"close":"c"})
        return df
    except: return None

def funding(sym):
    d=jget(f"{MEXC}/api/v1/contract/funding_rate",{"symbol":sym})
    try: return float(d["data"]["fundingRate"])
    except: return None

def gap_ok(c,pct=0.08): 
    if len(c)<2: return False
    return abs(float(c.iloc[-1]/c.iloc[-2]-1))<=pct

# ----- analiz -----
def analyze(sym):
    df=klines(sym,"4h",200)
    if df is None or len(df)<100: return None,"short"
    if float(df["v"].iloc[-1])<200_000: return None,"lowliq"
    c,h,l=df['c'],df['high'],df['low']
    if not gap_ok(c,0.08): return None,"gap"

    rr=float(rsi(c,14).iloc[-1]); e20,e50=ema(c,20).iloc[-1], ema(c,50).iloc[-1]
    trend_up=e20>e50
    adx_val=float(adx(pd.DataFrame({'high':h,'low':l,'close':c}),14).iloc[-1])
    v_ok, v = volume_spike(df, n=20, r=1.20)
    if not v_ok: return None,"novol"

    side=None
    if trend_up and rr>50: side="BUY"
    elif (not trend_up) and rr<50: side="SELL"
    else: return None,None

    fr=funding(sym); frtxt=""
    if fr is not None:
        if fr>0.01: frtxt=f" | Funding:+{fr:.3f}"
        elif fr<-0.01: frtxt=f" | Funding:{fr:.3f}"

    line=(f"{sym} | 4H | Trend:{'↑' if trend_up else '↓'} | RSI:{rr:.1f} | "
          f"Hacim x{v['ratio']:.2f} z:{v['z']:.2f} ramp:{v['ramp']:.2f} | "
          f"ADX:{adx_val:.0f}{frtxt}")
    return (side,line),None

def main():
    note=market_note()
    syms=mexc_symbols()
    if not syms: telegram("⚠️ Sembol listesi alınamadı (MEXC)."); return
    buys,sells=[],[]
    skipped={"short":0,"lowliq":0,"gap":0,"novol":0}
    for i,s in enumerate(syms):
        try:
            res,flag=analyze(s)
            if flag in skipped: skipped[flag]+=1
            if res:
                side,line=res
                (buys if side=='BUY' else sells).append(f"- {line}")
        except: pass
        if i%15==0: time.sleep(0.25)

    parts=[f"🟣 *4H Sinyaller*\n⏱ {ts()}\n{note}"]
    if buys: parts+=["\n🟢 *BUY:*"]+buys[:25]
    if sells: parts+=["\n🔴 *SELL:*"]+sells[:25]
    if not buys and not sells: parts.append("\nℹ️ Şu an 4H kriterlerine uyan sinyal yok.")
    parts.append(f"\n📊 Özet: BUY:{len(buys)} | SELL:{len(sells)} | Atlanan (likidite:{skipped['lowliq']}, gap:{skipped['gap']}, hacim:{skipped['novol']})")
    telegram("\n".join(parts))

if __name__=="__main__": main()
