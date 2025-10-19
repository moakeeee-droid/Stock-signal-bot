# -*- coding: utf-8 -*-
# Stock Top Movers -> Telegram Alert (Cloud / Render Background Worker)
# Quality profile + snapshot->grouped fallback

import os, time, requests, datetime
from datetime import datetime as dt

# ===== Read secrets from environment =====
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
POLYGON_API_KEY = os.environ["POLYGON_API_KEY"]

# ===== Tunables (with defaults) =====
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))
ALERT_PCT = float(os.environ.get("ALERT_PCT", "18.0"))
MIN_PRICE = float(os.environ.get("MIN_PRICE", "5.0"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "300000"))
INCLUDE_LOSERS = os.environ.get("INCLUDE_LOSERS", "false").lower() == "true"
SESSION_MODE = os.environ.get("SESSION_MODE", "regular")  # "regular" or "extended"
REPEAT_AFTER_MIN = int(os.environ.get("REPEAT_AFTER_MIN", "90"))

# ----- ETF blacklist -----
ETF_BLACKLIST = {
    "SPY","QQQ","DIA","VOO","IVV","IWM",
    "XLK","XLF","XLE","XLV","XLY","XLP","XLI","XLU","XLB","XLC",
    "XOP","XHB","XME","XBI"
}

# ----- Market hours gate (09:30-16:00 NY) -----
try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:
    NY = None

def us_market_open_now():
    if SESSION_MODE != "regular":
        return True
    if NY is None:
        return True
    now = dt.now(NY)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end   = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= now <= end

# ----------------- Utils -----------------
def tg(text:str):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=10,
    )
    print("TG:", r.status_code, r.text[:120])
    return r

def get_field(d,*keys,default=None):
    for k in keys:
        if d is None: return default
        d = d.get(k)
    return d if d is not None else default

# ---------- Primary: snapshot movers ----------
def fetch_snapshot(direction="gainers"):
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{direction}"
    r = requests.get(url, params={"apiKey": POLYGON_API_KEY}, timeout=15)
    print("Snapshot", direction, r.status_code)
    r.raise_for_status()
    data = r.json() or {}
    items = data.get("tickers", []) or data.get("results", []) or []
    out=[]
    for it in items:
        sym = it.get("ticker") or it.get("T")
        last = get_field(it,"lastTrade","p") or get_field(it,"day","c") or it.get("close") or it.get("c")
        pct  = it.get("todaysChangePerc") or it.get("todays_change_perc") or it.get("percent_change")
        vol  = get_field(it,"day","v") or it.get("volume") or 0
        if not sym or last is None or pct is None:
            continue
        try:
            out.append({"symbol":sym,"price":float(last),"pct":float(pct),"vol":float(vol or 0),"raw":it})
        except:
            pass
    return out

# ---------- Fallback: grouped aggs ----------
def latest_trading_date_usa():
    d = dt.utcnow().date()
    while d.weekday() >= 5:
        d = d - datetime.timedelta(days=1)
    return d.isoformat()

def fetch_grouped_aggs(date_iso=None):
    if not date_iso:
        date_iso = latest_trading_date_usa()
    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_iso}"
    r = requests.get(url, params={"adjusted":"true","apiKey":POLYGON_API_KEY}, timeout=20)
    print("Grouped", date_iso, r.status_code)
    r.raise_for_status()
    results = (r.json() or {}).get("results", []) or []
    out=[]
    for it in results:
        sym = it.get("T"); o = it.get("o"); c = it.get("c"); v = it.get("v")
        if not sym or not o or not c:
            continue
        try:
            price = float(c)
            pct = (float(c)-float(o)) / float(o) * 100.0
            vol = float(v or 0)
            out.append({"symbol":sym,"price":price,"pct":pct,"vol":vol,"raw":it})
        except:
            pass
    out.sort(key=lambda x: x["pct"], reverse=True)
    return out
# -----------------------------------------------

def detect_session_label(item,last_price):
    # snapshot only; grouped has no session split
    pre_c   = get_field(item,"preMarket","c")
    after_c = get_field(item,"afterHours","c")
    try:
        lp = float(last_price)
        if pre_c is not None and abs(float(pre_c)-lp) < 1e-6: return "🟡 Pre"
        if after_c is not None and abs(float(after_c)-lp) < 1e-6: return "🔵 After"
    except:
        pass
    return "🟢 Live"

def fast_entry_zone(p):
    lo = round(p*0.985,2); hi = round(p*1.005,2)
    return lo, hi, round(lo*0.98,2), round(hi*1.10,2)

def scan_once(last_alert_time:dict):
    if not us_market_open_now():
        print("Out of market hours")
        time.sleep(CHECK_INTERVAL_SEC)
        return []

    movers=[]; used_snapshot=True
    try:
        movers = fetch_snapshot("gainers")
        if INCLUDE_LOSERS:
            movers += fetch_snapshot("losers")
    except requests.HTTPError as e:
        used_snapshot=False
        if getattr(e.response,"status_code",None)==403:
            print("Snapshot 403 -> fallback grouped")
            movers = fetch_grouped_aggs()
        else:
            raise
    except Exception as e:
        used_snapshot=False
        print("Snapshot error:", e)
        movers = fetch_grouped_aggs()

    hits=[]
    now_ts=time.time()
    for it in movers:
        sym, price, pct, vol = it["symbol"], it["price"], it["pct"], it["vol"]
        if sym in ETF_BLACKLIST:         continue
        if price < MIN_PRICE:            continue
        if vol   < MIN_VOLUME:           continue
        if pct   < ALERT_PCT:            continue
        if now_ts - last_alert_time.get(sym,0) < REPEAT_AFTER_MIN*60:
            continue
        label = detect_session_label(it.get("raw"), price) if used_snapshot else "🟢 Live"
        lo,hi,cut,t1 = fast_entry_zone(price)
        hits.append((sym, price, pct, vol, label, lo, hi, cut, t1))
    print("hits:", len(hits))
    return hits

def main():
    tg(f"✅ เริ่มสแกน (Quality) ≥{ALERT_PCT:.1f}% | mode: {SESSION_MODE}")
    last_alert_time={}
    while True:
        try:
            for sym, price, pct, vol, label, lo, hi, cut, t1 in scan_once(last_alert_time):
                msg = (
                    f"{label} ⚠️ QUALITY SPIKE — {sym}\n"
                    f"+{pct:.1f}% | ${price:.2f} | Vol: {int(vol):,}\n"
                    f"Fast Entry: {lo}-{hi} | Cut: {cut} | T1: {t1}\n"
                    f"⏱ {dt.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                print("ALERT:", sym, pct)
                tg(msg)
                last_alert_time[sym]=time.time()
        except Exception as e:
            print("Loop error:", e)
            tg(f"❗️Scanner error: {e}")
        time.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    main()
