import os
import time
import requests
from datetime import datetime
from flask import Flask

# === ENVIRONMENT VARIABLES ===
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
POLYGON_API_KEY = os.environ["POLYGON_API_KEY"]

# === CONFIG ===
CHECK_INTERVAL_SEC = 60          # ความถี่ในการสแกน (วินาที)
ALERT_PCT = 15.0                 # เปอร์เซ็นต์เปลี่ยนแปลงขั้นต่ำที่จะเด้งแจ้งเตือนรายตัว
MIN_PRICE = 1.0                  # ราคาต่ำสุดของหุ้นที่จะแจ้ง
INCLUDE_LOSERS = True            # แจ้งฝั่งลงด้วยไหม
HEARTBEAT_INTERVAL = 1800        # 30 นาที ส่ง heartbeat
SUMMARY_INTERVAL = 3600          # 60 นาที ส่งสรุป Top 3
SESSION_MODE = "extended"        # pre / post / extended (ใช้ประกอบข้อความ)

last_heartbeat = 0
last_summary = 0

# === TELEGRAM FUNCTION ===
def tg(text: str):
    try:
        requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": text},
            timeout=10
        )
    except Exception as e:
        print("Telegram send error:", e)

# === POLYGON HELPERS ===
def _fetch(url):
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("Polygon fetch error:", e)
        return {}

def get_gainers():
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers?apiKey={POLYGON_API_KEY}"
    data = _fetch(url).get("tickers", []) or []
    # return list of tuples: (symbol, pct, price, volume)
    out = []
    for d in data:
        try:
            out.append((d["ticker"], d["todaysChangePerc"], d["day"]["c"], d["day"]["v"]))
        except Exception:
            continue
    return out

def get_losers():
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/losers?apiKey={POLYGON_API_KEY}"
    data = _fetch(url).get("tickers", []) or []
    out = []
    for d in data:
        try:
            out.append((d["ticker"], d["todaysChangePerc"], d["day"]["c"], d["day"]["v"]))
        except Exception:
            continue
    return out

def fmt_row(rank, sym, pct, price, vol):
    return f"{rank}. {sym}  {pct:+.1f}%  (${price:.2f})  Vol {int(vol):,}"

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
            # ดึงรายการล่าสุด
            gainers = get_gainers()
            losers = get_losers() if INCLUDE_LOSERS else []

            # === แจ้งเตือนรายตัวเมื่อแรงเกินเกณฑ์ ===
            hits = []
            for sym, pct, price, vol in gainers + losers:
                if abs(pct) >= ALERT_PCT and price >= MIN_PRICE:
                    now = time.time()
                    if sym not in last_alert_time or now - last_alert_time[sym] > 3600:
                        label = "🟢 GAIN" if pct > 0 else "🔴 LOSS"
                        msg = (
                            f"{label} {sym}\n"
                            f"Change: {pct:+.1f}% | Price: ${price:.2f}\n"
                            f"Volume: {int(vol):,}\n"
                            f"Time: {datetime.now().strftime('%H:%M:%S')}"
                        )
                        tg(msg)
                        hits.append(sym)
                        last_alert_time[sym] = now

            print(f"[{datetime.now().strftime('%H:%M:%S')}] hits: {len(hits)}")

            now = time.time()

            # === 💓 HEARTBEAT ทุก 30 นาที ===
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                tg(f"💓 Heartbeat — bot still running at {datetime.now().strftime('%H:%M:%S')}")
                last_heartbeat = now

            # === 🧾 SUMMARY Top 3 ทุก 60 นาที ===
            if now - last_summary >= SUMMARY_INTERVAL:
                try:
                    # จัดอันดับ Top 3 gainers
                    top_g = sorted(gainers, key=lambda x: x[1], reverse=True)[:3]
                    # จัดอันดับ Top 3 losers (ถ้าเปิดใช้งาน)
                    top_l = sorted(losers, key=lambda x: x[1])[:3] if INCLUDE_LOSERS else []

                    lines = [f"🧾 Hourly Summary ({datetime.now().strftime('%H:%M')})"]
                    if top_g:
                        lines.append("\nTop Gainers:")
                        for i, (sym, pct, price, vol) in enumerate(top_g, 1):
                            lines.append(fmt_row(i, sym, pct, price, vol))
                    else:
                        lines.append("\nTop Gainers: -")

                    if INCLUDE_LOSERS:
                        if top_l:
                            lines.append("\nTop Losers:")
                            for i, (sym, pct, price, vol) in enumerate(top_l, 1):
                                lines.append(fmt_row(i, sym, pct, price, vol))
                        else:
                            lines.append("\nTop Losers: -")

                    tg("\n".join(lines))
                except Exception as e:
                    print("Summary error:", e)
                last_summary = now

        except Exception as e:
            print("Loop error:", e)
            tg(f"❗Scanner error: {e}")

        time.sleep(CHECK_INTERVAL_SEC)

# === FLASK SERVER (กัน Render ดับจาก no-open-port) ===
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running fine."

if __name__ == "__main__":
    import threading
    threading.Thread(target=main, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
