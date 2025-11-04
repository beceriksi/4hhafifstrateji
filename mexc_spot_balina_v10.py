import os, time, requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN")
CHAT_ID=os.getenv("CHAT_ID")
MEXC="https://api.mexc.com"
COINGECKO="https://api.coingecko.com/api/v3/global"

SCAN_LIMIT=200
TF_LIST=["1h","4h"]
WHALE_USD=800_000
MIN_TURNOVER=100_000
VOL_R_BUY=1.15
VOL_R_SELL=1.10
RSI_BUY_MIN=50.0
RSI_SELL_MAX=60.0

def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# ----------------- Yardımcılar -----------------
def jget(url, params=None, retries=3, timeout=10):
    for _ in range(retries):
        try:
            r=requests.get(url, params=params, timeout=timeout)
            if r.status_code==200: return r.json()
        except: time.sleep(0.4)
    return None

def telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID: print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown"})
    except: pass

def ema(x,n): return x.ewm(span=n,adjust=False).mean()
def rsi(s,n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    rs=up.ewm(alpha=1/n,adjust=False).mean()/(dn.ewm(alpha=1/n,adjust=False).mean()+1e-12)
    return 100-(100/(1+rs))
def adx(df,n=14):
    up=df['high'].diff(); dn=-df['low'].diff()
    plus=np.where((up>dn)&(up>0),up,0.0); minus=np.where((dn>up)&(dn>0),dn,0.0)
    tr1=df['high']-df['low']; tr2=(df['high']-df['close'].shift()).abs(); tr3=(df['low']-df['close'].shift()).abs()
    tr=pd.DataFrame({'a':tr1,'b':tr2,'c':tr3}).max(axis=1)
    atr=tr.ewm(alpha=1/n,adjust=False).mean()
    plus_di=100*pd.Series(plus).ewm(alpha=1/n,adjust=False).mean()/(atr+1e-12)
    minus_di=100*pd.Series(minus).ewm(alpha=1/n,adjust=False).mean()/(atr+1e-12)
    dx=((plus_di-minus_di).abs()/((plus_di+minus_di)+1e-12))*100
    return dx.ewm(alpha=1/n,adjust=False).mean()

def volume_ratio(turnover,n=10):
    base=turnover.ewm(span=n,adjust=False).mean()
    return float(turnover.iloc[-1]/(base.iloc[-2]+1e-12))

# ----------------- MEXC Spot -----------------
def mexc_spot_symbols(limit=SCAN_LIMIT):
    d=jget(f"{MEXC}/api/v3/ticker/24hr")
    if not d: return []
    rows=[x for x in d if x.get("symbol","").endswith("USDT")]
    rows.sort(key=lambda x: float(x.get("quoteVolume","0")), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def klines(sym, interval="1h", limit=200):
    d=jget(f"{MEXC}/api/v3/klines", {"symbol":sym, "interval":interval, "limit":limit})
    if not d: return None
    try:
        df=pd.DataFrame(d,columns=["t","o","h","l","c","v","qv","n","t1","t2","ig","ib"])
        df=df.astype({"o":"float","h":"float","l":"float","c":"float","v":"float","qv":"float"})
        df.rename(columns={"c":"close"}, inplace=True)
        df["turnover"]=df["qv"]
        return df
    except: return None

# ----------------- Market Durumu -----------------
def market_note():
    g=jget(COINGECKO)
    try:
        total=float(g["data"]["market_cap_change_percentage_24h_usd"])
        btcd=float(g["data"]["market_cap_percentage"]["btc"])
        usdt=float(g["data"]["market_cap_percentage"]["usdt"])
    except: return "Piyasa: veri alınamadı.",0
    total2="↑ (Altlara giriş)" if total>0 else ("↓ (Çıkış)" if total<0 else "→ (Karışık)")
    usdt_note=f"{usdt:.1f}%"
    if usdt>=7: usdt_note+=" (riskten kaçış)"
    elif usdt<=5: usdt_note+=" (risk alımı)"
    return f"Piyasa: BTC.D {btcd:.1f}% | Total2: {total2} | USDT.D: {usdt_note}", total

# ----------------- Analiz -----------------
def analyze(sym, interval, market_pct):
    df=klines(sym, interval)
    if df is None or len(df)<80: return None
    if df["turnover"].iloc[-1]<MIN_TURNOVER: return None
    c,h,l,t=df["close"],df["h"],df["l"],df["turnover"]
    rr=float(rsi(c).iloc[-1]); e20,e50=ema(c,20).iloc[-1], ema(c,50).iloc[-1]; trend_up=e20>e50
    v_ratio=volume_ratio(t,10); adx_val=float(adx(pd.DataFrame({"high":h,"low":l,"close":c}),14).iloc[-1])
    last_dir=(c.iloc[-1]-c.iloc[-2])>=0
    whale=t.iloc[-1]>=WHALE_USD; whale_side="BUY" if last_dir else "SELL"
    side=None
    if trend_up and rr>=RSI_BUY_MIN and v_ratio>=VOL_R_BUY: side="BUY"
    elif (not trend_up) and rr<=RSI_SELL_MAX and v_ratio>=VOL_R_SELL: side="SELL"
    conf=int(min(100,(v_ratio*25)+(adx_val/3)+(rr/5)))
    return {
        "symbol":sym,"tf":interval.upper(),"side":side,"whale":whale,"whale_side":whale_side,
        "turnover":t.iloc[-1],"rsi":rr,"adx":adx_val,"trend":"↑" if trend_up else "↓",
        "v_ratio":v_ratio,"conf":conf
    }

# ----------------- Ana -----------------
def main():
    note,market_pct=market_note()
    syms=mexc_spot_symbols()
    if not syms: telegram("⚠️ Sembol alınamadı (MEXC Spot)."); return
    results=[]; start=time.time()
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures=[ex.submit(analyze,s,tf,market_pct) for s in syms for tf in TF_LIST]
        for f in as_completed(futures):
            try: r=f.result()
            except: r=None
            if r: results.append(r)
    buys=[x for x in results if x["side"]=="BUY"]; sells=[x for x in results if x["side"]=="SELL"]
    whales=[x for x in results if x["whale"]]
    conf_avg=int(sum([x["conf"] for x in results if x["conf"]])/max(1,len(results)))
    msg=[f"⚡ *MEXC Spot 1H / 4H Tarama Raporu*\n⏱ {ts()}\n📊 Tarama: {len(syms)} coin | Süre: {int(time.time()-start)} sn\n🛡️ Güven Ort.: {conf_avg}/100\n{note}"]
    if whales:
        msg.append("\n💰 *Balina Hacimleri (≥0.8M USD)*")
        for w in sorted(whales,key=lambda x:x["turnover"],reverse=True)[:5]:
            tag="🟢 BUY" if w["whale_side"]=="BUY" else "🔴 SELL"
            msg.append(f"- {w['symbol']} | {w['tf']} | {tag} | Hacim:{w['turnover']:.0f} USD")
    if buys or sells:
        msg.append("\n📈 *Sinyaller*")
        if buys: msg.append("🟢 *BUY:*"); [msg.append(f"- {x['symbol']} | {x['tf']} | Güven:{x['conf']}") for x in sorted(buys,key=lambda x:x['conf'],reverse=True)[:10]]
        if sells: msg.append("🔴 *SELL:*"); [msg.append(f"- {x['symbol']} | {x['tf']} | Güven:{x['conf']}") for x in sorted(sells,key=lambda x:x['conf'],reverse=True)[:10]]
    else:
        msg.append("\nℹ️ Şu an kriterlere uyan sinyal yok.")
    telegram("\n".join(msg))

if __name__=="__main__": main()
