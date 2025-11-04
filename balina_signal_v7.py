import os, time, requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# === Ayarlar ===
TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN")
CHAT_ID=os.getenv("CHAT_ID")
BINANCE="https://api.binance.com"
COINGECKO="https://api.coingecko.com/api/v3/global"

SCAN_LIMIT=180        # ilk 180 likit coin
TF_LIST=["1h","4h"]   # 1H + 4H
WHALE_USD=1_000_000   # balina hacim eşiği
MIN_TURNOVER=150_000   # minimum işlem hacmi
VOL_R_BUY=1.20         # BUY hacim oranı
VOL_R_SELL=1.15        # SELL hacim oranı
RSI_BUY_MIN=50.0
RSI_SELL_MAX=60.0

def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# --- Yardımcı Fonksiyonlar ---
def jget(url, params=None, retries=3, timeout=8):
    for _ in range(retries):
        try:
            r=requests.get(url, params=params, timeout=timeout)
            if r.status_code==200: return r.json()
        except: time.sleep(0.3)
    return None

def telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID: print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown"})
    except: pass

# --- İndikatörler ---
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

# --- Veriler ---
def binance_top_symbols(limit=SCAN_LIMIT):
    d=jget(f"{BINANCE}/api/v3/ticker/24hr")
    if not d: return []
    rows=[x for x in d if x.get("symbol","").endswith("USDT")]
    rows.sort(key=lambda x: float(x.get("quoteVolume","0")), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def klines(symbol,interval="1h",limit=200):
    d=jget(f"{BINANCE}/api/v3/klines",{"symbol":symbol,"interval":interval,"limit":limit})
    if not d: return None
    try:
        df=pd.DataFrame(d,columns=["open_time","open","high","low","close","volume","ct","quote_volume","t","tb","tq","i"])
        df=df.astype({"open":"float","high":"float","low":"float","close":"float","volume":"float","quote_volume":"float"})
        df.rename(columns={"close":"c"},inplace=True)
        df["turnover"]=df["quote_volume"]
        return df
    except: return None

def market_note():
    g=jget(COINGECKO)
    try:
        total=float(g["data"]["market_cap_change_percentage_24h_usd"])
        btcd=float(g["data"]["market_cap_percentage"]["btc"])
        usdt=float(g["data"]["market_cap_percentage"]["usdt"])
    except: return "Piyasa: veri alınamadı.",0
    tkr=jget(f"{BINANCE}/api/v3/ticker/24hr",{"symbol":"BTCUSDT"})
    btc=float(tkr["priceChangePercent"]) if tkr and "priceChangePercent" in tkr else None
    arrow="↑" if (btc and btc>total) else ("↓" if (btc and btc<total) else "→")
    dirb ="↑" if (btc and btc>0) else ("↓" if (btc and btc<0) else "→")
    total2="↑ (Altlara giriş)" if arrow=="↓" and total>=0 else ("↓ (Çıkış)" if arrow=="↑" and total<=0 else "→ (Karışık)")
    usdt_note=f"{usdt:.1f}%"
    if usdt>=7: usdt_note+=" (riskten kaçış)"
    elif usdt<=5: usdt_note+=" (risk alımı)"
    return f"Piyasa: BTC {dirb} + BTC.D {arrow} (BTC.D {btcd:.1f}%) | Total2: {total2} | USDT.D: {usdt_note}", btc

# --- Güven Puanı ---
def confidence(side,rr,trend_up,v_ratio,adx_val,btc_pct):
    s=0
    if side=="BUY" and trend_up and rr>=RSI_BUY_MIN: s+=25
    if side=="SELL" and (not trend_up) and rr<=RSI_SELL_MAX: s+=25
    if side=="BUY" and v_ratio>=VOL_R_BUY: s+=35
    if side=="SELL" and v_ratio>=VOL_R_SELL: s+=30
    if adx_val>=25: s+=10
    if btc_pct is not None and ((side=="BUY" and btc_pct>0) or (side=="SELL" and btc_pct<0)): s+=10
    return int(min(100,s))

# --- Analiz ---
def analyze(symbol,interval,btc_pct):
    df=klines(symbol,interval)
    if df is None or len(df)<80: return None
    if df["turnover"].iloc[-1]<MIN_TURNOVER: return None
    c,h,l,t=df["c"],df["high"],df["low"],df["turnover"]
    rr=float(rsi(c).iloc[-1]); e20,e50=ema(c,20).iloc[-1],ema(c,50).iloc[-1]; trend_up=e20>e50
    v_ratio=volume_ratio(t,10); adx_val=float(adx(pd.DataFrame({"high":h,"low":l,"close":c}),14).iloc[-1])
    last_dir=(c.iloc[-1]-c.iloc[-2])>=0
    whale=t.iloc[-1]>=WHALE_USD
    whale_side="BUY" if last_dir else "SELL"
    side=None
    if trend_up and rr>=RSI_BUY_MIN and v_ratio>=VOL_R_BUY: side="BUY"
    elif (not trend_up) and rr<=RSI_SELL_MAX and v_ratio>=VOL_R_SELL: side="SELL"
    conf=confidence(side,rr,trend_up,v_ratio,adx_val,btc_pct) if side else 0
    return {
        "symbol":symbol,"tf":interval.upper(),"side":side,"whale":whale,"whale_side":whale_side,
        "turnover":t.iloc[-1],"rsi":rr,"adx":adx_val,"trend":"↑" if trend_up else "↓",
        "v_ratio":v_ratio,"conf":conf
    }

# --- Ana ---
def main():
    note,btc_pct=market_note()
    syms=binance_top_symbols(SCAN_LIMIT)
    if not syms: telegram("⚠️ Sembol alınamadı (Binance)."); return
    results=[]; start=time.time()
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures=[ex.submit(analyze,s,tf,btc_pct) for s in syms for tf in TF_LIST]
        for f in as_completed(futures):
            try: r=f.result()
            except: r=None
            if r: results.append(r)
    buys=[x for x in results if x["side"]=="BUY"]; sells=[x for x in results if x["side"]=="SELL"]
    whales=[x for x in results if x["whale"]]
    conf_avg=int(sum([x["conf"] for x in results if x["conf"]])/max(1,len(results)))
    msg=[f"⚡ *Spot 1H / 4H Balina Taraması*\n⏱ {ts()}\nTarama: {len(syms)} coin | Süre: {int(time.time()-start)} sn\n📉 {note}\n🛡️ Güven Ort.: {conf_avg}/100"]
    if whales:
        msg.append("\n💰 *Balina Hacimleri* (≥1M USD):")
        for w in sorted(whales,key=lambda x:x["turnover"],reverse=True)[:6]:
            tag="🟢 BUY" if w["whale_side"]=="BUY" else "🔴 SELL"
            msg.append(f"- {w['symbol']} | {w['tf']} | {tag} | Hacim:{w['turnover']:.1f} USD | RSI:{w['rsi']:.1f}")
    if buys or sells:
        msg.append("\n📈 *Sinyaller*")
        if buys: msg.append("🟢 *BUY:*"); [msg.append(f"- {x['symbol']} | {x['tf']} | Güven:{x['conf']} | RSI:{x['rsi']:.1f}") for x in sorted(buys,key=lambda x:x['conf'],reverse=True)[:10]]
        if sells: msg.append("🔴 *SELL:*"); [msg.append(f"- {x['symbol']} | {x['tf']} | Güven:{x['conf']} | RSI:{x['rsi']:.1f}") for x in sorted(sells,key=lambda x:x['conf'],reverse=True)[:10]]
    else: msg.append("\nℹ️ Şu an sinyal yok. Piyasa özet üstte.")
    telegram("\n".join(msg))

if __name__=="__main__": main()
