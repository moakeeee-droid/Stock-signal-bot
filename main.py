# -*- coding: utf-8 -*-
# Stock Signal Bot (Render Web Service + Telegram Alert)
# - สแกน Top Movers จาก Polygon
# - ส่งแจ้งเตือนเข้า Telegram
# - เปิด Flask หน้า "/" สำหรับ Render/UptimeRobot ให้ออนไลน์ตลอด

import os
import time
import requests
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask

# ========== ค่า Environment (ต้องใส่ใน Render -> Environment Variables) ==========
BOT_TOKEN        = os.environ["BOT_TOKEN"]          # เช่น 8085xxxx:AA....
CHAT_ID          = os.environ["CHAT_ID"]            # เช่น 5263482152
POLYGON_API_KEY  = os.environ["POLYGON_API_KEY"]    # เช่น BqQ5kLFE0m...

# ========== การตั้งค่า (ปรับได้ตลอด) ==========
CHECK_INTERVAL_SEC = 60         # สแกนทุกกี่วินาที
ALERT_PCT          = 15.0       # เปอร์เซ็นต์เปลี่ยนแปลงขั้นต่ำถึงจะเตือน (ขึ้น)
INCLUDE_LOSERS     = False      # แจ้งฝั่งลงด้วยหรือไม่
MIN_PRICE          = 0.30       # กรองหุ้นราคาต่ำเกินไป
MIN_VOLUME         = 0          # กรอง Volume (0 = ไม่กรอง)
REPEAT_AFTER_MIN   = 60         # เตือนซ้ำชื่อเดิมได้อีกครั้งหลัง X นาที
SESSION_MODE       = "extended" # "regular" หรือ "extended" (ไว้แสดงในข้อความ)

# ========== ฟังก์ชันส่งข้อความ Telegram ==========
def tg(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
        if r.status_code >= 400:
            print("TG send error:", r.status_code, r.text)
    except Exception as e:
        print("TG exception:", e)

# ========== ดึง Top Movers จาก Polygon ==========
# หมายเหตุ: ใช้ snapshot gainers/losers
# เอกสามารถปรับ endpoint เพิ่มได้ภายหลังตามต้องการ
def fetch_movers(kind="gainers"):
    """
    kind: "gainers" หรือ "losers"
    return: list[dict] -> [{sym, price, pct, volume}]
    """
    assert kind in ("gainers", "losers")

    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/{kind}"
    params = {"apiKey": POLYGON_API_KEY}

    try:
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 403:
            # API Key/สิทธิ์ไม่พอ
            print("Polygon 403 Forbidden:", r.text)
            return []
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print("Polygon request error:", e)
        return []

    items = []
    for t in (data.get("tickers") or []):
        sym = t.get("ticker") or "-"
        # ราคาสุดท้าย
        price = None
        last_trade = t.get("lastTrade") or {}
        if isinstance(last_trade, dict):
            price = last_trade.get("p")
        if price is None:
            # บางเคสไม่มี lastTrade ให้ลองเอาราคาอื่น ๆ
            price = (t.get("day") or {}).get("c") or (t.get("prevDay") or {}).get("c") or 0.0

        # % เปลี่ยนแปลงวันนี้
        pct = t.get("todaysChangePerc")
        if pct is None:
            day = t.get("day") or {}
            prev = (t.get("prevDay") or {}).get("c")
            cur = day.get("c")
            if prev and cur:
                try:
                    pct = (cur - prev) / prev * 100.0
                except Exception:
                    pct = 0.0
            else:
                pct = 0.0

        # ปริมาณ
        volume = (t.get("day") or {}).get("v") or 0

        items.append({
            "sym": sym,
            "price": float(price or 0),
            "pct": float(pct or 0),
            "volume": int(volume or 0),
        })

    return items

# ========== ตัวช่วยกรอง & ฟอร์แมตข้อความ ==========
def pass_filters(row: dict, up: bool):
    # กรองราคา
    if row["price"] < MIN_PRICE:
        return False
    # กรอง volume
    if MIN_VOLUME and row["volume"] < MIN_VOLUME:
        return False
    # กรอง % เปลี่ยน
    if up:
        return row["pct"] >= ALERT_PCT
    else:
        return abs(row["pct"]) >= ALERT_PCT

def fmt_row(label: str, row: dict):
    return (
        f"{label} ⚡ <b>{row['sym']}</b>\n"
        f"+{row['pct']:.1f}% | ${row['price']:.2f}\n"
        f"Vol: {row['volume']:,}\n"
        f"<i>mode: {SESSION_MODE} • {datetime.now().strftime('%H:%M:%S')}</i>"
    )

def fmt_row_down(label: str, row: dict):
    return (
        f"{label} 🔻 <b>{row['sym']}</b>\n"
        f"{row['pct']:.1f}% | ${row['price']:.2f}\n"
        f"Vol: {row['volume']:,}\n"
        f"<i>mode: {SESSION_MODE} • {datetime.now().strftime('%H:%M:%S')}</i>"
    )

# ========== งานหลัก ==========
def main():
    tg(f"✅ เริ่มสแกน Top Movers (≥{ALERT_PCT:.1f}% | mode: {SESSION_MODE})")
    last_alert_time = {}  # sym -> datetime ล่าสุดที่เตือน

    while True:
        try:
            # ฝั่งขึ้น
            ups = fetch_movers("gainers")
            hits = 0
            for row in ups:
                if not pass_filters(row, up=True):
                    continue
                sym = row["sym"]
                # กันสแปม: เตือนซ้ำได้หลัง X นาที
                tlast = last_alert_time.get(sym)
                if tlast and datetime.now() - tlast < timedelta(minutes=REPEAT_AFTER_MIN):
                    continue
                tg(fmt_row("Gainer", row))
                last_alert_time[sym] = datetime.now()
                hits += 1

            # ฝั่งลง (ถ้าต้องการ)
            if INCLUDE_LOSERS:
                downs = fetch_movers("losers")
                for row in downs:
                    if not pass_filters(row, up=False):
                        continue
                    sym = row["sym"]
                    tlast = last_alert_time.get(sym)
                    if tlast and datetime.now() - tlast < timedelta(minutes=REPEAT_AFTER_MIN):
                        continue
                    tg(fmt_row_down("Loser", row))
                    last_alert_time[sym] = datetime.now()
                    hits += 1

            print(f"[{datetime.now().strftime('%H:%M:%S')}] hits: {hits}")
        except Exception as e:
            print("Loop error:", e)
            tg(f"❗ Scanner error: {e}")

        time.sleep(CHECK_INTERVAL_SEC)

# ========== Flask (ทำให้ Render/UptimeRobot เรียกได้) ==========
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running fine."

def run_flask():
    # พอร์ตคงที่ 10000 เพื่อให้ UptimeRobot/Render ตรวจเจอ
    app.run(host="0.0.0.0", port=10000)

# ========== Entry ==========
if __name__ == "__main__":
    # รัน Flask เป็น thread แยก แล้วจึงรันบอท
    Thread(target=run_flask, daemon=True).start()
    main()
