import os, time, requests, pandas as pd, numpy as np
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# === Ayarlar ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")
BINANCE        = "https://api.binance.com"
COINGECKO      = "https://api.coingecko.com/api/v3/global"

SCAN_LIMIT     = 150          # en likit ilk 150 parite
TF_LIST        = ["1h","4h"]  # 1H + 4H tarama
WHALE_USD      = 1_000_000    # balina eşiği
MIN_TURNOVER   = 200_000      # minimum likidite filtresi
VOL_R_BUY      = 1.20         # BUY için hacim oranı
VOL_R_SELL     = 1.15         # SELL için hacim oranı (hafif gevşek)
RSI_BUY_MIN    = 50.0
RSI_SELL_MAX   = 60.0

def ts(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# --- HTTP yardımcı ---
def jget(url, params=None, retries=2, timeout=6):
    for _ in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 200: return r.json()
        except: time.sleep(0.25)
    return None

def telegram(text: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(text); return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"})
    except: pass

# --- indikatörler ---
def ema(x,n): return x.ewm(span=n, adjust=False).mean()
def rsi(s, n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    rs = up.ewm(alpha=1/n, adjust=False).mean() / (dn.ewm(alpha=1/n, adjust=False).mean() + 1e-12)
    return 100 - (100/(1+rs))
def adx(df, n=14):
    up = df['high'].diff(); dn = -df['low'].diff()
    plus  = np.where((up>dn)&(up>0), up, 0.0)
    minus = np.where((dn>up)&(dn>0), dn, 0.0)
    tr = pd.DataFrame({
        'a': df['high']-df['low'],
        'b': (df['high']-df['close'].shift()).abs(),
        'c': (df['low']-df['close'].shift()).abs()
    }).max(axis=1)
    atr = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di  = 100*pd.Series(plus).ewm(alpha=1/n, adjust=False).mean()  / (atr+1e-12)
    minus_di = 100*pd.Series(minus).ewm(alpha=1/n, adjust=False).mean() / (atr+1e-12)
    dx = ((plus_di - minus_di).abs() / ((plus_di + minus_di)+1e-12)) * 100
    return dx.ewm(alpha=1/n, adjust=False).mean()

def volume_ratio(turnover, n=10):
    base = turnover.ewm(span=n, adjust=False).mean()
    return float(turnover.iloc[-1] / (base.iloc[-2] + 1e-12))

# --- semboller & veriler ---
def binance_top_symbols(limit=SCAN_LIMIT):
    t = jget(f"{BINANCE}/api/v3/ticker/24hr")
    if not t: return []
    rows = [x for x in t if x.get("symbol","").endswith("USDT")]
    rows.sort(key=lambda x: float(x.get("quoteVolume","0")), reverse=True)
    return [x["symbol"] for x in rows[:limit]]

def klines(symbol, interval="1h", limit=200):
    d = jget(f"{BINANCE}/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not d: return None
    try:
        df = pd.DataFrame(d, columns=[
            "open_time","open","high","low","close","volume","close_time",
            "quote_volume","trades","taker_base","taker_quote","ignore"
        ])
        df = df.astype({"open":"float64","high":"float64","low":"float64","close":"float64",
                        "volume":"float64","quote_volume":"float64"})
        df.rename(columns={"close":"c"}, inplace=True)
        df["turnover"] = df["quote_volume"]  # USDT cinsinden
        return df[["open","c","high","low","turnover"]]
    except:
        return None

def market_note():
    g = jget(COINGECKO)
    btcd = usdt_d = None; total_pct = None
    try:
        total_pct = float(g["data"]["market_cap_change_percentage_24h_usd"])
        btcd = float(g["data"]["market_cap_percentage"]["btc"])
        usdt_d = float(g["data"]["market_cap_percentage"].get("usdt", 0.0))
    except: pass
    btc24 = jget(f"{BINANCE}/api/v3/ticker/24hr", {"symbol":"BTCUSDT"})
    btc_pct = float(btc24["priceChangePercent"]) if btc24 and "priceChangePercent" in btc24 else None
    arrow = "→"
    if btc_pct is not None and total_pct is not None:
        arrow = "↑" if btc_pct > total_pct else ("↓" if btc_pct < total_pct else "→")
    dirb = "→"
    if btc_pct is not None:
        dirb = "↑" if btc_pct>0 else ("↓" if btc_pct<0 else "→")
    t2 = "→ (Karışık)"
    if arrow=="↓" and (total_pct is not None and total_pct>=0): t2="↑ (Altlara giriş)"
    if arrow=="↑" and (total_pct is not None and total_pct<=0): t2="↓ (Çıkış)"
    usdt_note = f"{usdt_d:.1f}%" if usdt_d is not None else "?"
    if usdt_d is not None:
        if usdt_d>=7: usdt_note += " (riskten kaçış)"
        elif usdt_d<=5: usdt_note += " (risk alımı)"
    btcd_note = f"{btcd:.1f}%" if btcd is not None else "?"
    return f"Piyasa: BTC {dirb} + BTC.D {arrow} (BTC.D {btcd_note}) | Total2: {t2} | USDT.D: {usdt_note}", btc_pct

# --- güven puanı (0-100) ---
def confidence_score(side, rr, trend_up, v_ratio, adx_val, btc_pct):
    score = 0.0
    # RSI/EMA uyumu (30)
    if side=="BUY"  and trend_up and rr>=RSI_BUY_MIN:  score += 24
    if side=="SELL" and (not trend_up) and rr<=RSI_SELL_MAX: score += 24
    # hacim anomalisi (40)
    if side=="BUY" and v_ratio>=VOL_R_BUY: score += min(40, (v_ratio-1.0)*80)  # r=1.20 -> 16 puan+, r>1.7 ~ 56 cap to 40
    if side=="SELL" and v_ratio>=VOL_R_SELL: score += min(40, (v_ratio-0.9)*80) # r=1.15 -> 20 puan civarı
    score = min(score, 64)  # üst limit; kalanları ekle
    # trend gücü (20)
    if adx_val>=20: score += 12
    if adx_val>=30: score += 5
    if adx_val>=40: score += 3
    # BTC bağlamı (10)
    if btc_pct is not None:
        if side=="BUY" and btc_pct>0: score += 6
        if side=="SELL" and btc_pct<0: score += 6
    return int(max(0,min(100,score)))

# --- tek analiz ---
def analyze_one(symbol, interval, btc_pct):
    df = klines(symbol, interval, 200)
    if df is None or len(df) < 80: return None, "short"
    if df["turnover"].iloc[-1] < MIN_TURNOVER: return None, "lowliq"

    o, c, h, l, t = df["open"], df["c"], df["high"], df["low"], df["turnover"]
    rr = float(rsi(c).iloc[-1]); e20 = float(ema(c,20).iloc[-1]); e50 = float(ema(c,50).iloc[-1])
    trend_up = e20 > e50
    v_ratio = volume_ratio(t, n=10)
    adx_val = float(adx(pd.DataFrame({"high":h,"low":l,"close":c}),14).iloc[-1])

    # mum yönü (önceki kapanışa göre)
    last_change = float(c.iloc[-1] - c.iloc[-2])
    candle_up = last_change >= 0

    # Balina tespiti
    whale = t.iloc[-1] >= WHALE_USD
    whale_side = "BUY" if candle_up else "SELL" if last_change < 0 else None

    # Sinyal kuralları
    side = None
    if trend_up and rr >= RSI_BUY_MIN and v_ratio >= VOL_R_BUY:
        side = "BUY"
    elif (not trend_up) and rr <= RSI_SELL_MAX and v_ratio >= VOL_R_SELL:
        side = "SELL"

    # güven puanı
    conf = None
    if side:
        conf = confidence_score(side, rr, trend_up, v_ratio, adx_val, btc_pct)

    # çıktı metni
    info = {
        "symbol": symbol, "tf": interval.upper(), "rsi": rr, "adx": adx_val,
        "v_ratio": v_ratio, "trend": ("↑" if trend_up else "↓"),
        "whale": whale, "whale_side": whale_side, "turnover": float(t.iloc[-1]),
        "side": side, "conf": conf
    }
    return info, None

# --- ana akış ---
def main():
    note, btc_pct = market_note()
    symbols = binance_top_symbols(limit=SCAN_LIMIT)
    scanned = 0; skipped = {"short":0,"lowliq":0,"error":0}
    results = []

    if not symbols:
        telegram("⛔ Sembol alınamadı (Binance)."); return

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = []
        for s in symbols:
            for tf in TF_LIST:
                futures.append(ex.submit(analyze_one, s, tf, btc_pct))
        for f in as_completed(futures):
            try:
                info, flag = f.result()
                if flag:
                    skipped[flag] = skipped.get(flag,0)+1
                else:
                    results.append(info)
                scanned += 1
            except:
                skipped["error"] = skipped.get("error",0)+1

    # sınıflandır
    whales = [x for x in results if x and x["whale"]]
    signals_buy  = [x for x in results if x and x["side"]=="BUY"]
    signals_sell = [x for x in results if x and x["side"]=="SELL"]

    # güven ortalaması
    conf_vals = [x["conf"] for x in results if x and x["conf"] is not None]
    conf_avg = int(sum(conf_vals)/len(conf_vals)) if conf_vals else 0

    # Mesaj
    lines = []
    lines.append(f"⚡ *1H / 4H Balina Taraması*\n⏱ {ts()}")
    lines.append(f"Tarama: {len(symbols)} parite | İncelenen TF sayısı: {len(TF_LIST)} | Toplam iş: {scanned}")
    lines.append(f"📉 {note}")
    lines.append(f"🛡️ Güven ortalaması: {conf_avg}/100 | Atlanan: short:{skipped.get('short',0)}, lowliq:{skipped.get('lowliq',0)}, error:{skipped.get('error',0)}")

    if whales:
        lines.append("\n💰 *Balina Tespiti* (≥ 1M USD):")
        # en yüksek turnover ilk 6
        whales_sorted = sorted(whales, key=lambda x: x["turnover"], reverse=True)[:6]
        for x in whales_sorted:
            tag = "🟢 BUY" if x["whale_side"]=="BUY" else ("🔴 SELL" if x["whale_side"]=="SELL" else "•")
            lines.append(f"- {x['symbol']} | {x['tf']} | {tag} | Hacim:{x['turnover']:.1f} USD | RSI:{x['rsi']:.1f} | Trend:{x['trend']}")

    if signals_buy or signals_sell:
        lines.append("\n📈 *Sinyaller*")
        if signals_buy:
            buys = sorted(signals_buy, key=lambda x: (x["conf"] or 0), reverse=True)[:10]
            lines.append("🟢 *BUY:*")
            for x in buys:
                lines.append(f"- {x['symbol']} | {x['tf']} | Güven:{x['conf']} | RSI:{x['rsi']:.1f} | ADX:{x['adx']:.0f} | Hacim x{x['v_ratio']:.2f}")
        if signals_sell:
            sells = sorted(signals_sell, key=lambda x: (x["conf"] or 0), reverse=True)[:10]
            lines.append("🔴 *SELL:*")
            for x in sells:
                lines.append(f"- {x['symbol']} | {x['tf']} | Güven:{x['conf']} | RSI:{x['rsi']:.1f} | ADX:{x['adx']:.0f} | Hacim x{x['v_ratio']:.2f}")
    else:
        lines.append("\nℹ️ Şu an net sinyal yok (balina ve piyasa özeti üstte).")

    telegram("\n".join(lines))

if __name__ == "__main__":
    main()
