#!/usr/bin/env python3
"""Gold (15m) MACD cross alert -> Telegram + email.

Same maths as the CM_MacD_Ult_MTF Pine script:
  macd   = EMA(close, 12) - EMA(close, 26)
  signal = SMA(macd, 9)          (simple moving average, as in the script)
Buy  = MACD crosses above signal   (Pine: crossover)
Sell = MACD crosses below signal   (Pine: crossunder)
"""
import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

SYMBOL = os.environ.get("SYMBOL", "XAUUSD=X")      # spot gold (CFD); GC=F is futures
INTERVAL_SEC = 15 * 60
FAST, SLOW, SIGNAL = 12, 26, 9
LOOKBACK_CANDLES = 3                                # recheck last N closed candles
MAX_AGE_SEC = 20 * 60                               # alert only if the cross is newer than this
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
LOCAL_TZ = ZoneInfo("Africa/Algiers")


def fetch_candles(symbol):
    last_err = None
    for host in ("query1", "query2", "query1"):
        url = (
            f"https://{host}.finance.yahoo.com/v8/finance/chart/"
            + urllib.parse.quote(symbol)
            + "?interval=15m&range=10d"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            res = data["chart"]["result"][0]
            stamps = res["timestamp"]
            closes = res["indicators"]["quote"][0]["close"]
            candles = [(t, c) for t, c in zip(stamps, closes) if c is not None]
            now = time.time()
            candles = [(t, c) for t, c in candles if t + INTERVAL_SEC <= now]
            if len(candles) < SLOW + SIGNAL + 5:
                raise RuntimeError(f"Not enough candles for {symbol}: {len(candles)}")
            return candles
        except Exception as e:  # try next host
            last_err = e
            print(f"fetch failed ({host}, {symbol}): {e!r}")
            time.sleep(2)
    raise last_err


def fetch_twelvedata(api_key):
    """Spot gold XAU/USD 15-minute candles from Twelve Data (free API key)."""
    url = (
        "https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=15min"
        "&outputsize=300&order=ASC&timezone=UTC&apikey=" + urllib.parse.quote(api_key)
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("status") == "error" or "values" not in data:
        raise RuntimeError(f"TwelveData: {data.get('message', data)}")
    candles = []
    for v in data["values"]:
        dt = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        candles.append((int(dt.timestamp()), float(v["close"])))
    candles.sort()
    now = time.time()
    candles = [(t, c) for t, c in candles if t + INTERVAL_SEC <= now]
    if len(candles) < SLOW + SIGNAL + 5:
        raise RuntimeError(f"TwelveData: not enough candles ({len(candles)})")
    return candles


def get_candles():
    """Spot gold (XAU/USD). Futures (GC=F) only if ALLOW_FUTURES=true."""
    errors = []
    api_key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
    if api_key:
        try:
            return "XAU/USD (TwelveData)", fetch_twelvedata(api_key)
        except Exception as e:
            errors.append(str(e))
    symbols = [SYMBOL]
    if os.environ.get("ALLOW_FUTURES") == "true":
        symbols.append("GC=F")
    for sym in dict.fromkeys(symbols):
        try:
            return sym, fetch_candles(sym)
        except Exception as e:
            errors.append(f"{sym}: {e}")
    raise RuntimeError(" | ".join(errors))


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
    """Return [(ts, 'buy'|'sell', close)] for crosses on the last closed candles."""
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
        if time.time() - (ts + INTERVAL_SEC) > MAX_AGE_SEC:
            continue  # cross is too old
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


def send_email(subject, text):
    """Optional email via SMTP (default Gmail). Needs EMAIL_USER + EMAIL_PASSWORD secrets."""
    user = os.environ.get("EMAIL_USER", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    if not user or not password:
        return False
    to = os.environ.get("EMAIL_TO", "").strip() or user
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    return True


def notify(subject, text):
    """Send to Telegram and email; one failing does not block the other."""
    errors, sent = [], 0
    try:
        send_telegram(text)
        sent += 1
    except Exception as e:
        errors.append(f"Telegram: {e}")
    try:
        if send_email(subject, text):
            sent += 1
    except Exception as e:
        errors.append(f"Email: {e}")
    if errors:
        print("notify errors:", " | ".join(errors))
    if sent == 0:
        raise RuntimeError(" | ".join(errors) or "nothing sent")


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


def run():
    if os.environ.get("SEND_TEST") == "true":
        notify("اختبار تنبيه الذهب", "✅ اختبار: تنبيه MACD للذهب (15 دقيقة) يعمل.")
        print("Test message sent")
        return 0

    symbol, candles = get_candles()

    if os.environ.get("HEARTBEAT") == "true":
        send_telegram(
            f"⏱ فحص تجريبي: {fmt_time(int(time.time()))}\n"
            f"آخر سعر: {candles[-1][1]:.2f} ({symbol})"
        )

    last_ts = load_state()
    if last_ts is None:
        save_state(candles[-1][0])
        print("Initialised state; no alert on first run")
        return 0

    signals = find_signals(candles, last_ts)
    for ts, kind, close in signals:
        icon, label = ("🟢", "تقاطع شراء") if kind == "buy" else ("🔴", "تقاطع بيع")
        notify(
            f"{icon} {label} - الذهب 15 دقيقة",
            f"{icon} {label} (MACD) — الذهب 15 دقيقة\n"
            f"السعر: {close:.2f}\n"
            f"إغلاق الشمعة: {fmt_time(ts)} (توقيت الجزائر)\n"
            f"المصدر: {symbol}"
        )
        print(f"Sent {kind} at {ts}")
    if signals:
        save_state(max(s[0] for s in signals))
    print(f"Done. symbol={symbol}, signals={len(signals)}")
    return 0


def main():
    try:
        return run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        if os.environ.get("HEARTBEAT") == "true":
            try:
                send_telegram(f"⚠️ خطأ في السكربت: {str(e)[:300]}")
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
