# -*- coding: utf-8 -*-
# Stock Signal Bot (Polygon Free plan: previous market day)
# - Pulls grouped aggs of the most recent trading day (yesterday or earlier if weekend)
# - Filters by % change, price, volume
# - Sends summary to Telegram
# - Runs periodically to avoid spamming free API

import os
import time
import json
import requests
from datetime import datetime, timedelta

# ========= ENV (Render → Environment Variables) =========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "").strip()

# ========= BASIC SETTINGS (ปรับได้ตามชอบ) =========
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "1800"))  # เช็คทุก 30 นาที
ALERT_PCT = float(os.environ.get("ALERT_PCT", "10"))       # % เปลี่ยนแปลงขั้นต่ำ
MIN_PRICE = float(os.environ.get("MIN_PRICE", "0.30"))      # กันหุ้นถูกจัด
MIN_VOLUME = int(os.environ.get("MIN_VOLUME", "0"))         # ถ้าจะกรอง volume ใส่ค่าที่นี่ (เช่น 300000)
INCLUDE_LOSERS = os.environ.get("INCLUDE_LOSERS", "false").lower() == "true"  # แจ้งฝั่งลงด้วยไหม

# ========= SMALL UTILS =========
def tg(text: str):
    """Send a Telegram text message."""
    if not (BOT_TOKEN and CHAT_ID):
        print("Telegram env missing.")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text}
        r = requests.post(url, json=payload, timeout=20)
        print("TG send:", r.status_code, r.text[:200])
    except Exception as e:
        print("TG error:", e)

def eastern_today_date():
    """
    ประมาณวันที่ปัจจุบันในโซนเวลานิวยอร์ก (EST/EDT)
    ใช้ UTC-4 เป็นค่าใกล้เคียง (พอเพียงสำหรับการดึง 'วันก่อนหน้า')
    """
    return (datetime.utcnow() - timedelta(hours=4)).date()

def prev_market_day():
    """คืนค่า 'วันทำการล่าสุด' ก่อนวันนี้ (เลี่ยงเสาร์/อาทิตย์)"""
    d = eastern_today_date() - timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d

def fetch_grouped_aggs(day):
    """เรียก grouped aggs ของวันทำการที่ระบุ (ฟรีได้)"""
    date_str = day.isoformat()
    url = (
        f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
        f"{date_str}?adjusted=true&apiKey={POLYGON_API_KEY}"
    )
    r = requests.get(url, timeout=60)
    print("Polygon grouped:", r.status_code)
    try:
        data = r.json()
    except Exception:
        data = {"status": "ERR", "message": "invalid json", "raw": r.text[:300]}
    return data

def analyze(results):
    """
    แปลงข้อมูลเป็นลิสต์ของสัญลักษณ์ที่ผ่านเกณฑ์
    โครงสร้าง object ที่ Polygon ส่งกลับ (สำคัญ ๆ):
      T=ticker, o=open, c=close, v=volume, h=high, l=low
    """
    movers_up = []
    movers_dn = []

    for it in results or []:
        try:
            sym = it.get("T")
            o = float(it.get("o", 0) or 0)
            c = float(it.get("c", 0) or 0)
            v = int(it.get("v", 0) or 0)
            if not sym or o <= 0:
                continue
            price = c
            pct = (c - o) / o * 100.0

            # basic filters
            if price < MIN_PRICE:
                continue
            if v < MIN_VOLUME:
                continue

            rec = {
                "sym": sym,
                "price": price,
                "pct": pct,
                "vol": v,
                "open": o,
                "close": c
            }
            if pct >= ALERT_PCT:
                movers_up.append(rec)
            elif INCLUDE_LOSERS and (-pct) >= ALERT_PCT:
                movers_dn.append(rec)
        except Exception:
            continue

    # จัดอันดับ
    movers_up.sort(key=lambda x: x["pct"], reverse=True)
    movers_dn.sort(key=lambda x: x["pct"], reverse=True)  # (ค่านี้จะเป็นขาลง มีค่าเป็นลบ)

    return movers_up, movers_dn

def fmt_list(items, label, limit=20):
    if not items:
        return f"• ไม่มี {label} ผ่านเกณฑ์"
    lines = [f"• {x['sym']}  {x['pct']:+.1f}%  @{x['price']:.2f}  Vol:{x['vol']:,}" for x in items[:limit]]
    return "\n".join(lines)

def run_once():
    if not POLYGON_API_KEY:
        tg("❌ ไม่พบ POLYGON_API_KEY ใน Environment Variables")
        return

    mday = prev_market_day()
    data = fetch_grouped_aggs(mday)

    status = data.get("status", "")
    if status != "OK":
        # ข้อความผิดพลาดของเรทฟรี ขอวันนี้จะขึ้น NOT_AUTHORIZED
        msg = data.get("message", str(data)[:300])
        tg(f"⚠️ Polygon (free) ปฏิเสธคำขอ\nวันที่: {mday.isoformat()}\nstatus: {status}\nmessage: {msg}")
        return

    results = data.get("results", [])
    up, dn = analyze(results)

    header = f"✅ Top Movers (ฟรี, ย้อนหลังวันล่าสุด)\nวันที่อ้างอิง: {mday.isoformat()}\nเกณฑ์: ≥{ALERT_PCT:.1f}% | ราคา ≥{MIN_PRICE} | Vol ≥{MIN_VOLUME:,}\n"
    body_up = "📈 ขึ้นแรง:\n" + fmt_list(up, "ขึ้น")
    if INCLUDE_LOSERS:
        body_dn = "\n\n📉 ลงแรง:\n" + fmt_list(dn, "ลง")
    else:
        body_dn = ""
    tg(header + "\n" + body_up + body_dn)

# ========= Flask (ให้ UptimeRobot เคาะ) =========
from flask import Flask
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot (Polygon Free) is running. Last market day: " + prev_market_day().isoformat()

def main_loop():
    # ส่งทันทีรอบแรก
    try:
        tg("🟢 เริ่มทำงานโหมดฟรี (ดึงข้อมูลวันทำการล่าสุดจาก Polygon)")
        run_once()
    except Exception as e:
        tg(f"❗ Startup error: {e}")

    # วนรอบแบบประหยัด API
    while True:
        try:
            time.sleep(CHECK_INTERVAL_SEC)
            run_once()
        except Exception as e:
            print("Loop error:", e)
            tg(f"❗ Loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    # รัน main loop แบบ background ด้วยวิธีง่าย ๆ (ไม่ใช้ thread แยกเพราะ Render ฟรีโอเคกับลูปยาว)
    # แล้วเปิดเว็บเซิร์ฟเวอร์ทิ้งไว้ให้ UptimeRobot เคาะ
    import threading
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=10000)
