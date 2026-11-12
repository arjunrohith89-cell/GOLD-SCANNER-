import os
import sys
import time
import datetime as dt
import requests
import pandas as pd

# ---------- SETTINGS ----------
RISK_USD = 50          # risk per trade (1% of $5k). Change as you like
RR = 2                 # target = 2 x stop distance
ATR_MULT_MAX = 3.0     # skip if stop is wider than 3 x ATR
SESSION_START = 13 * 60 + 30   # 1:30 PM IST (London open)
SESSION_END = 23 * 60 + 30     # 11:30 PM IST
MAX_SIGNAL_AGE_MIN = 25        # ignore stale candles
HEARTBEAT_HOURS = 2
PATTERNS = "both"      # both / engulf / pin
SPREAD = 0.30          # assumed cost in $ per oz (spread+commission)

# Put these in environment variables, NOT in the code
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
# ------------------------------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"})


def now_ist():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5, minutes=30)).replace(tzinfo=None)


def get_candles(days="5d"):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
    r = SESSION.get(url, params={"interval": "5m", "range": days}, timeout=10)
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "ts": pd.to_datetime(res["timestamp"], unit="s", utc=True)
              .tz_convert("Asia/Kolkata").tz_localize(None),
        "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
    }).dropna().reset_index(drop=True)

    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))
    return df


def check_signal(df, i):
    """Evaluate the CLOSED candle at index i. Returns dict or None."""
    if i < 55:
        return None
    c, p = df.iloc[i], df.iloc[i - 1]
    mins = c["ts"].hour * 60 + c["ts"].minute
    if not (SESSION_START <= mins <= SESSION_END):
        return None

    atr = c["atr"]
    body = abs(c["close"] - c["open"])
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]

    up_trend = c["ema20"] > c["ema50"] and c["close"] > c["ema50"]
    dn_trend = c["ema20"] < c["ema50"] and c["close"] < c["ema50"]

    bull_engulf = (p["close"] < p["open"] and c["close"] > c["open"]
                   and c["close"] > p["open"] and c["open"] <= p["close"])
    bear_engulf = (p["close"] > p["open"] and c["close"] < c["open"]
                   and c["close"] < p["open"] and c["open"] >= p["close"])
    hammer = body > 0 and lower >= 2 * body and upper <= body
    star = body > 0 and upper >= 2 * body and lower <= body

    if PATTERNS == "engulf":
        hammer = star = False
    elif PATTERNS == "pin":
        bull_engulf = bear_engulf = False

    side = None
    if up_trend and (bull_engulf or hammer) and c["low"] <= c["ema20"] + 0.2 * atr and c["rsi"] < 65:
        side = "BUY"
        entry = c["close"]
        sl = min(c["low"], p["low"]) - 0.1 * atr
    elif dn_trend and (bear_engulf or star) and c["high"] >= c["ema20"] - 0.2 * atr and c["rsi"] > 35:
        side = "SELL"
        entry = c["close"]
        sl = max(c["high"], p["high"]) + 0.1 * atr
    if not side:
        return None

    risk = abs(entry - sl)
    if risk < 0.5 * atr:
        risk = 0.5 * atr
        sl = entry - risk if side == "BUY" else entry + risk
    if risk > ATR_MULT_MAX * atr:
        return None

    target = entry + RR * risk if side == "BUY" else entry - RR * risk
    lots = max(0.01, round(RISK_USD / (risk * 100), 2))  # 1 lot = 100 oz
    pattern = ("Engulfing" if (bull_engulf if side == "BUY" else bear_engulf)
               else ("Hammer" if side == "BUY" else "Shooting Star"))
    return {
        "ID": f"{c['ts']:%Y-%m-%d %H:%M}_{side}", "Side": side, "Time": f"{c['ts']:%H:%M}",
        "ts": c["ts"], "Entry": round(entry, 2), "SL": round(sl, 2), "Target": round(target, 2),
        "Lots": lots, "Risk": round(risk * 100 * lots, 2), "Pattern": pattern,
    }


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        print("Telegram not configured:", text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)


def fmt(s):
    icon = "🟢 GOLD BUY" if s["Side"] == "BUY" else "🔴 GOLD SELL"
    return (f"{icon} ({s['Time']} IST) - {s['Pattern']}\n"
            f"Entry: ${s['Entry']}\nSL: ${s['SL']}\nTarget: ${s['Target']}\n"
            f"Lots: {s['Lots']} | Risk: ${s['Risk']}")


def run_backtest(df):
    """Returns list of (win, r_after_costs)."""
    out, i = [], 55
    while i < len(df) - 1:
        s = check_signal(df, i)
        if not s:
            i += 1
            continue
        result, j = None, i + 1
        while j < len(df) and j < i + 60:
            h, l = df.at[j, "high"], df.at[j, "low"]
            if s["Side"] == "BUY":
                hit_sl, hit_tp = l <= s["SL"], h >= s["Target"]
            else:
                hit_sl, hit_tp = h >= s["SL"], l <= s["Target"]
            if hit_sl:            # both in one candle = loss (conservative)
                result = -1
                break
            if hit_tp:
                result = RR
                break
            j += 1
        if result is not None:
            risk = abs(s["Entry"] - s["SL"])
            out.append((result > 0, result - SPREAD / risk))
        i = j + 1
    return out


def backtest():
    t = run_backtest(get_candles("30d"))
    if not t:
        print("No trades found")
        return
    wins = sum(1 for w, _ in t if w)
    print(f"Trades: {len(t)} | Wins: {wins} | Win rate: {wins / len(t) * 100:.1f}%")
    print(f"Net after costs: {sum(r for _, r in t):+.1f} R")


def show_signals(last_n=15):
    """Prints recent signals with entry, SL, target and outcome."""
    df = get_candles("30d")
    rows, i = [], 55
    while i < len(df) - 1:
        s = check_signal(df, i)
        if not s:
            i += 1
            continue
        res, j = "OPEN", i + 1
        while j < len(df) and j < i + 60:
            h, l = df.at[j, "high"], df.at[j, "low"]
            if s["Side"] == "BUY":
                hit_sl, hit_tp = l <= s["SL"], h >= s["Target"]
            else:
                hit_sl, hit_tp = h >= s["SL"], l <= s["Target"]
            if hit_sl:
                res = "LOSS"
                break
            if hit_tp:
                res = "WIN"
                break
            j += 1
        rows.append((s, res))
        i = j + 1
    for s, res in rows[-last_n:]:
        print(f"{s['ts']:%d-%b %H:%M} {s['Side']} {s['Pattern']}\n"
              f"  Entry {s['Entry']} | SL {s['SL']} | TP {s['Target']} | {res}")


def sweep():
    global RR, PATTERNS, SESSION_START, SESSION_END
    df = get_candles("30d")
    sessions = {"London": (810, 1200), "NewYork": (1200, 1410), "Full": (810, 1410)}
    rows = []
    for rr in (1.5, 2, 3):
        for pat in ("both", "engulf", "pin"):
            for name, (a, b) in sessions.items():
                RR, PATTERNS, SESSION_START, SESSION_END = rr, pat, a, b
                t = run_backtest(df)
                if len(t) < 10:
                    continue
                w = sum(1 for x, _ in t if x)
                rows.append((sum(r for _, r in t), len(t), w / len(t) * 100, rr, pat, name))
    rows.sort(reverse=True)
    print("NetR | Trades | Win% | RR | Pattern | Session")
    for net, n, wr, rr, pat, name in rows[:10]:
        print(f"{net:+.1f} | {n} | {wr:.0f}% | {rr} | {pat} | {name}")


def run_live():
    send_telegram("🚀 Gold scanner started (EMA trend + candlestick patterns)")
    notified, last_beat = set(), time.time()
    while True:
        try:
            now = now_ist()
            if now.weekday() < 5:
                df = get_candles("5d")
                s = check_signal(df, len(df) - 2)  # last CLOSED candle
                if s and s["ID"] not in notified:
                    age = (now - s["ts"]).total_seconds() / 60
                    notified.add(s["ID"])
                    if age <= MAX_SIGNAL_AGE_MIN:
                        send_telegram(fmt(s))
                        print(fmt(s))
                if time.time() - last_beat > HEARTBEAT_HOURS * 3600:
                    last_beat = time.time()
                    send_telegram(f"✅ Scanner alive | Gold ${df.iloc[-1]['close']:.2f} | no new signal")
            else:
                time.sleep(300)
        except Exception as e:
            print("Loop error:", e)
        time.sleep(60)


STATE_FILE = "last_signal.txt"


def run_once():
    """One check, then exit. For GitHub Actions (runs every 5 min)."""
    now = now_ist()
    if now.weekday() >= 5:
        return
    df = get_candles("5d")
    s = check_signal(df, len(df) - 2)  # last CLOSED candle
    last_id = open(STATE_FILE).read().strip() if os.path.exists(STATE_FILE) else ""
    if s and s["ID"] != last_id:
        age = (now - s["ts"]).total_seconds() / 60
        if age <= MAX_SIGNAL_AGE_MIN:
            send_telegram(fmt(s))
        with open(STATE_FILE, "w") as f:
            f.write(s["ID"])
    print(f"{now:%H:%M} checked | signal: {s['ID'] if s else 'none'}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backtest":
        backtest()
    elif len(sys.argv) > 1 and sys.argv[1] == "show":
        show_signals()
    elif len(sys.argv) > 1 and sys.argv[1] == "sweep":
        sweep()
    elif len(sys.argv) > 1 and sys.argv[1] == "once":
        run_once()
    else:
        run_live()
        
