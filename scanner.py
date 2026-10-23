import io
import time
import datetime as dt
import requests
import pandas as pd

# ---------- SETTINGS ($5k FundingPips Account) ----------
MAX_LOSS = 125       # Max risk limit for $5k account
CAPITAL = 5000       # $5,000 account size
RR = 2
MAX_RISK_PCT = 1.0
USE_VWAP_FILTER = True
VOL_MULTIPLIER = 1.2

# Your Verified Telegram Credentials:
TG_TOKEN = "8905260026:AAHXExujNUHsGbGbldHiEsGETsk-OMTXc0w"
TG_CHAT_ID = "945498377"
# --------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"
})

def get_ist_now():
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5, minutes=30)

def get_gold_candles():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    resp = SESSION.get(url, params={"interval": "5m", "range": "2d"}, timeout=8)
    result = resp.json().get("chart", {}).get("result")
    if not result:
        return pd.DataFrame()

    res = result[0]
    ts = res.get("timestamp", [])
    quote = res.get("indicators", {}).get("quote", [{}])[0]
    if not ts or not quote:
        return pd.DataFrame()

    df = pd.DataFrame({
        "ts": pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata").tz_localize(None),
        "open": quote.get("open"),
        "high": quote.get("high"),
        "low": quote.get("low"),
        "close": quote.get("close"),
        "volume": quote.get("volume"),
    }).dropna()

    if df.empty:
        return pd.DataFrame()

    latest_day = df["ts"].dt.date.max()
    df = df[df["ts"].dt.date == latest_day].copy().reset_index(drop=True)
    
    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (tp * df["volume"]).cumsum() / df["volume"].replace(0, 1).cumsum()
    return df

def evaluate_gold_signal(df):
    if df.empty or len(df) < 4:
        return None
    
    last = df.iloc[-1]
    last_close, last_vwap, last_vol = float(last["close"]), float(last["vwap"]), float(last["volume"])
    avg_vol = float(df["volume"].mean())
    prev_close = float(df.iloc[-2]["close"])
    
    opening = df.head(3)
    r_high, r_low = opening["high"].max(), opening["low"].min()

    if last_close > r_high and prev_close <= r_high:
        if USE_VWAP_FILTER and last_close < last_vwap:
            return None
        side, entry, sl = "BUY", r_high, r_low
    elif last_close < r_low and prev_close >= r_low:
        if USE_VWAP_FILTER and last_close > last_vwap:
            return None
        side, entry, sl = "SELL", r_low, r_high
    else:
        return None

    if VOL_MULTIPLIER > 0 and last_vol < (avg_vol * VOL_MULTIPLIER):
        return None

    risk = abs(entry - sl)
    if risk == 0 or (risk / entry * 100) > MAX_RISK_PCT:
        return None

    qty = round(min(MAX_LOSS / risk, (CAPITAL * 0.02) / entry), 2)
    if qty < 0.01:
        qty = 0.01

    target = entry + RR * risk if side == "BUY" else entry - RR * risk
    timestamp_str = last["ts"].strftime("%Y-%m-%d %H:%M")
    
    return {
        "ID": f"{timestamp_str}_{side}",
        "Symbol": "GOLD (XAUUSD)", "Side": side, "Time": last["ts"].strftime("%H:%M"),
        "Entry": round(entry, 2), "SL": round(sl, 2), "Target": round(target, 2),
        "Qty": qty, "Risk": round(risk * qty, 2), "VWAP": round(last_vwap, 2)
    }

def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

def fmt(s):
    icon = "🟢 GOLD BUY" if s["Side"] == "BUY" else "🔴 GOLD SELL"
    return f"{icon} ({s['Time']})\nEntry: ${s['Entry']} | SL: ${s['SL']} | Target: ${s['Target']}\nLots: {s['Qty']} | Risk: ${s['Risk']}"

if __name__ == "__main__":
    send_telegram("🚀 Multi-Signal Gold Scanner Active ($5k FundingPips Account)")
    notified_signals = set()

    while True:
        now_ist = get_ist_now()
        if now_ist.weekday() >= 5:
            time.sleep(300)
            continue

        df = get_gold_candles()
        sig = evaluate_gold_signal(df)
        
        if sig and sig["ID"] not in notified_signals:
            notified_signals.add(sig["ID"])
            msg = fmt(sig)
            print(msg)
            send_telegram(msg)
            
        time.sleep(60)
