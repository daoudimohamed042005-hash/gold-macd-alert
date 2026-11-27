#!/usr/bin/env python3
"""Gold (15m) MACD cross alert -> Telegram.

macd   = EMA(close, 12) - EMA(close, 26)
signal = SMA(macd, 9)
Buy  = MACD crosses above signal, Sell = MACD crosses below signal.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SYMBOL = os.environ.get("SYMBOL", "GC=F")
INTERVAL_SEC = 15 * 60
FAST, SLOW, SIGNAL = 12, 26, 9
LOOKBACK_CANDLES = 12
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
LOCAL_TZ = ZoneInfo("Africa/Algiers")


def fetch_candles(symbol):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + "?interval=15m&range=10d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    res = data["chart"]["result"][0]
    stamps = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    candles = [(t, c) for t, c in zip(stamps, closes) if c is not None]
    now = time.time()
    return [(t, c) for t, c in candles if t + INTERVAL_SEC <= now]


def ema(values, n):
    k = 2.0 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def compute(closes):
    fast, slow = ema(closes, FAST), ema(closes, SLOW)
    macd = [a - b for a, b in zip(fast, slow)]
    signal = [None] * (SIGNAL - 1)
    for i in range(SIGNAL - 1, len(macd)):
        signal.append(sum(macd[i - SIGNAL + 1 : i + 1]) / SIGNAL)
    return macd, signal


def find_signals(candles, since_ts):
    closes = [c for _, c in candles]
    if len(closes) < SLOW + SIGNAL + 5:
        raise RuntimeError("Not enough candles")
    macd, signal = compute(closes)
    found = []
    for i in range(len(candles) - LOOKBACK_CANDLES, len(candles)):
        if i < 1 or signal[i - 1] is None:
            continue
        ts, close = candles[i]
        if ts <= since_ts:
            continue
        if macd[i] > signal[i] and macd[i - 1] <= signal[i - 1]:
            found.append((ts, "buy", close))
        elif macd[i] < signal[i] and macd[i - 1] >= signal[i - 1]:
            found.append((ts, "sell", close))
    return found


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"Telegram error {r.status}")


def fmt_time(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("last_ts")
    except (FileNotFoundError, ValueError):
        return None


def save_state(ts):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_ts": ts}, f)


def main():
    if os.environ.get("SEND_TEST") == "true":
        send_telegram("✅ اختبار: تنبيه MACD للذهب (15 دقيقة) يعمل.")
        print("Test message sent")
        return 0

    candles = fetch_candles(SYMBOL)

    if os.environ.get("HEARTBEAT") == "true":
        send_telegram(f"⏱ فحص تجريبي: {fmt_time(int(time.time()))} - آخر سعر {candles[-1][1]:.2f}")

    last_ts = load_state()
    if last_ts is None:
        save_state(candles[-1][0])
        print("Initialised state; no alert on first run")
        return 0

    signals = find_signals(candles, last_ts)
    for ts, kind, close in signals:
        icon, label = ("🟢", "تقاطع شراء") if kind == "buy" else ("🔴", "تقاطع بيع")
        send_telegram(
            f"{icon} {label} (MACD) — الذهب 15 دقيقة\n"
            f"السعر: {close:.2f}\n"
            f"إغلاق الشمعة: {fmt_time(ts)} (توقيت الجزائر)\n"
            f"المصدر: {SYMBOL}"
        )
        print(f"Sent {kind} at {ts}")
    save_state(max([candles[-1][0]] + [s[0] for s in signals]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
