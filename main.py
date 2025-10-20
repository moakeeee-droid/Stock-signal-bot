import os
import time
import requests
from datetime import datetime, timedelta, timezone

from flask import Flask

# === ENV (ต้องตั้งใน Render) ===
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
POLYGON_API_KEY = os.environ["POLYGON_API_KEY"]

# === CONFIG (ปรับได้) ===
CHECK_INTERVAL_SEC = 60          # สแกนทุก X วิ
ALERT_PCT = 12.0                 # เกณฑ์ % เปลี่ยนขั้นต่ำ (แนะนำเริ่ม 10–15)
MIN_PRICE = 1.0                  # ราคาอย่างน้อย
INCLUDE_LOSERS = True            # แจ้งขาลงด้วยไหม
HEARTBEAT_INTERVAL = 1800        # heartbeat ทุก 30 นาที
SUMMARY_INTERVAL = 3600          # summary ทุก 60 นาที
SESSION_MODE = "extended"        # สำหรับแสดงในข้อความ

last_heartbeat = 0
last_summary = 0

# === Telegram ===
def tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10
        )
    except Exception as e:
        print("Telegram send error:", e)

# === Helpers ===
def now_str():
    return datetime.now().strftime("%H:%M:%S")

def us_latest_trading_date_iso():
    # ใช้ UTC เป็นฐาน: ถ้าวันหยุดเสาร์-อาทิตย์ ให้ถอยไปวันศุกร์
    d = datetime.utcnow().date()
    while d.weekday() >= 5:  # 5=Sat,6=Sun
        d -= timedelta(days=1)
    return d.isoformat()

def _get(url, params=None, timeout=12):
    p = params.copy() if isinstance(params, dict) else {}
    p["apiKey"] = POLYGON_API_KEY
    r = requests.get(url, params=p, timeout=timeout)
    return r

# === Primary: snapshot (intraday), Fallback: grouped aggs (daily) ===
def fetch_snapshot(kind="gainers"):
    """return list of tuples (sym, pct, price, vol) from snapshot; raise for_status"""
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{kind}"
    r = _get(url)
    print("snapshot", kind, r.status_code)
    r.raise_for_status()
    data = r.json() or {}
    rows = data.get("tickers") or []
    out = []
    for d in rows:
        sym = d.get("ticker")
        pct = d.get("todaysChangePerc")
        price = (d.get("lastTrade") or {}).get("p") or (d.get("day") or {}).get("c")
        vol = (d.get("day") or {}).get("v")
        if sym is None or pct is None or price is None:
            continue
        out.append((sym, float(pct), float(price), float(vol or 0)))
    return out

def fetch_grouped(date_iso=None):
    """return list of tuples (sym, pct, price, vol) using grouped aggs (O/C/V)"""
    if not date_iso:
        date_iso = us_latest_trading_date_iso()
    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_iso}"
    r = _get(url, params={"adjusted": "true"}, timeout=20)
    print("grouped", date_iso, r.status_code)
    r.raise_for_status()
    data = r.json() or {}
    results = data.get("results") or []
    out = []
    for it in results:
        sym = it.get("T")
        o = it.get("o"); c = it.get("c"); v = it.get("v")
        if not sym or o is None or c is None:
            continue
        try:
            pct = (float(c) - float(o)) / float(o) * 100.0
            out.append((sym, pct, float(c), float(v or 0)))
        except:
            continue
    return out

def fetch_movers_resilient():
    """ดึง gainers/losers แบบทนทาน: snapshot -> (ถ้าไม่ได้) ใช้ grouped"""
    movers = []
    used = "snapshot"
    try:
        g = fetch_snapshot("gainers")
        movers.extend(g)
        if INCLUDE_LOSERS:
            l = fetch_snapshot("losers")
            movers.extend(l)
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", None)
        print("snapshot HTTPError:", code)
        used = "grouped"
        rows = fetch_grouped()
        # จัดอันดับ: บวกมากสุดเป็น gainers, ลบน้อยสุดเป็น losers
        rows_sorted = sorted(rows, key=lambda x: x[1], reverse=True)
        top_g = rows_sorted[:50]
        top_l = rows_sorted[-50:] if INCLUDE_LOSERS else []
        movers = top_g + top_l
    except Exception as e:
        print("snapshot error:", e)
        used = "grouped"
        rows = fetch_grouped()
        rows_sorted = sorted(rows, key=lambda x: x[1], reverse=True)
        top_g = rows_sorted[:50]
        top_l = rows_sorted[-50:] if INCLUDE_LOSERS else []
        movers = top_g + top_l

    print(f"movers source = {used} | count = {len(movers)}")
    return movers, used

# === Summary text ===
def fmt_row(rank, sym, pct, price, vol):
    return f"{rank}. {sym}  {pct:+.1f}%  (${price:.2f})  Vol {int(vol):,}"

def build_hourly_summary():
    gainers, used1 = [], ""
    losers, used2 = [], ""

    try:
        g = fetch_snapshot("gainers")
        gainers = g[:10]
        used1 = "snapshot"
    except Exception:
        rows = fetch_grouped()
        rows_sorted = sorted(rows, key=lambda x: x[1], reverse=True)
        gainers = rows_sorted[:10]
        used1 = "grouped"

    if INCLUDE_LOSERS:
        try:
            l = fetch_snapshot("losers")
            losers = l[:10]
            used2 = "snapshot"
        except Exception:
            rows = fetch_grouped()
            rows_sorted = sorted(rows, key=lambda x: x[1])
            losers = rows_sorted[:10]
            used2 = "grouped"

    lines = [f"🧾 Hourly Summary ({now_str()})"]
    if gainers:
        lines.append(f"\nTop Gainers (src: {used1}):")
        for i, (sym, pct, price, vol) in enumerate(sorted(gainers, key=lambda x: x[1], reverse=True)[:3], 1):
            lines.append(fmt_row(i, sym, pct, price, vol))
    else:
        lines.append("\nTop Gainers: -")

    if INCLUDE_LOSERS:
        if losers:
            lines.append(f"\nTop Losers (src: {used2}):")
            for i, (sym, pct, price, vol) in enumerate(sorted(losers, key=lambda x: x[1])[:3], 1):
                lines.append(fmt_row(i, sym, pct, price, vol))
        else:
            lines.append("\nTop Losers: -")

    return "\n".join(lines)

# === MAIN LOOP ===
def main():
    tg(
        "✅ Bot started & scanning now\n"
        f"• Mode: {SESSION_MODE}\n"
        f"• Alert ≥ {ALERT_PCT:.1f}% | Min Price ≥ ${MIN_PRICE:.2f}\n"
        f"• Include Losers: {INCLUDE_LOSERS}\n"
        f"• Interval: {CHECK_INTERVAL_SEC}s"
    )

    last_alert_time = {}
    global last_heartbeat, last_summary

    while True:
        try:
            movers, source = fetch_movers_resilient()  # list of (sym, pct, price, vol)

            hits = 0
            for sym, pct, price, vol in movers:
                # กรองเบื้องต้น
                if price is None or price < MIN_PRICE:
                    continue
                if pct is None or abs(pct) < ALERT_PCT:
                    continue
                # กันสแปมซ้ำ 60 นาที
                now_ts = time.time()
                if sym in last_alert_time and now_ts - last_alert_time[sym] < 3600:
                    continue
                last_alert_time[sym] = now_ts

                label = "🟢 GAIN" if pct >= 0 else "🔴 LOSS"
                msg = (
                    f"{label} {sym}\n"
                    f"Change: {pct:+.1f}% | Price: ${price:.2f}\n"
                    f"Volume: {int(vol):,}\n"
                    f"Source: {source}\n"
                    f"Time: {now_str()}"
                )
                tg(msg)
                hits += 1

            print(f"[{now_str()}] hits: {hits}")

            # Heartbeat
            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                tg(f"💓 Heartbeat — bot still running at {now_str()}")
                last_heartbeat = now

            # Hourly Summary
            if now - last_summary >= SUMMARY_INTERVAL:
                try:
                    tg(build_hourly_summary())
                except Exception as e:
                    print("Summary error:", e)
                last_summary = now

        except Exception as e:
            print("Loop error:", e)
            tg(f"❗Scanner error: {e}")

        time.sleep(CHECK_INTERVAL_SEC)

# === Flask (ให้ Render/uptime เช็กพอร์ต) ===
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running fine."

if __name__ == "__main__":
    import threading
    threading.Thread(target=main, daemon=True).start()
    # Render จะกำหนด PORT ให้ใน env; ถ้าไม่มี ให้ใช้ 10000
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
