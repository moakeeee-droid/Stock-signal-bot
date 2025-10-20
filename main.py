# -*- coding: utf-8 -*-
# Stock-signal-bot (Render/Telegram/Polygon)
# - โปรไฟล์สำเร็จรูป (conservative/balanced/momentum/reversal)
# - สัญญาณเตรียมเข้า Options: CALL/PUT
# - กรองคุณภาพเบื้องต้น (ราคา/วอลุ่ม)
# - กันสแปมสัญญาณซ้ำด้วย REPEAT_AFTER_MIN
# - Flask keepalive สำหรับ Render (พอร์ตอ่านจาก PORT หรือ 10000)

import os
import time
import json
import requests
from datetime import datetime, timedelta
from flask import Flask

# ========= ENV (ต้องตั้งใน Render: Environment Variables) =========
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID          = os.environ.get("CHAT_ID", "").strip()
POLYGON_API_KEY  = os.environ.get("POLYGON_API_KEY", "").strip()

# ========= โปรไฟล์ให้เลือก (ตั้งค่าเดียวพอ) =========
PROFILE = "balanced"  # เลือก: "conservative" / "balanced" / "momentum" / "reversal"

_profiles = {
    # เน้นคุณภาพ กรองหนัก
    "conservative": dict(
        CHECK_INTERVAL_SEC=90,
        ALERT_PCT=20.0,
        INCLUDE_LOSERS=True,
        MIN_PRICE=5.0,
        MIN_VOLUME=500_000,
        REPEAT_AFTER_MIN=90,
        SESSION_MODE="extended",
        CALL_PCT=7.0,
        PUT_PCT=-7.0,
        MIN_OPTION_PRICE=5.0,
        SEND_DAILY_SUMMARY=True, SUMMARY_HOUR=8, SUMMARY_MINUTE=0,
        WHITELIST=[],
    ),
    # สมดุล (แนะนำเริ่มต้น)
    "balanced": dict(
        CHECK_INTERVAL_SEC=60,
        ALERT_PCT=15.0,
        INCLUDE_LOSERS=True,
        MIN_PRICE=2.0,
        MIN_VOLUME=200_000,
        REPEAT_AFTER_MIN=60,
        SESSION_MODE="extended",
        CALL_PCT=5.0,
        PUT_PCT=-5.0,
        MIN_OPTION_PRICE=1.0,
        SEND_DAILY_SUMMARY=True, SUMMARY_HOUR=8, SUMMARY_MINUTE=0,
        WHITELIST=[],
    ),
    # โมเมนตัมแรง (หาเบรคเอาต์)
    "momentum": dict(
        CHECK_INTERVAL_SEC=45,
        ALERT_PCT=10.0,
        INCLUDE_LOSERS=True,
        MIN_PRICE=1.0,
        MIN_VOLUME=100_000,
        REPEAT_AFTER_MIN=45,
        SESSION_MODE="extended",
        CALL_PCT=4.0,
        PUT_PCT=-4.0,
        MIN_OPTION_PRICE=1.0,
        SEND_DAILY_SUMMARY=True, SUMMARY_HOUR=8, SUMMARY_MINUTE=0,
        WHITELIST=[],
    ),
    # จับรีเวอร์ส/สวิงแรง (เสี่ยงสูงกว่า)
    "reversal": dict(
        CHECK_INTERVAL_SEC=60,
        ALERT_PCT=12.0,
        INCLUDE_LOSERS=True,
        MIN_PRICE=1.0,
        MIN_VOLUME=150_000,
        REPEAT_AFTER_MIN=60,
        SESSION_MODE="extended",
        CALL_PCT=3.5,
        PUT_PCT=-3.5,
        MIN_OPTION_PRICE=1.0,
        SEND_DAILY_SUMMARY=True, SUMMARY_HOUR=8, SUMMARY_MINUTE=0,
        WHITELIST=[],
    ),
}
cfg = _profiles[PROFILE]

CHECK_INTERVAL_SEC = cfg["CHECK_INTERVAL_SEC"]
ALERT_PCT          = cfg["ALERT_PCT"]
INCLUDE_LOSERS     = cfg["INCLUDE_LOSERS"]
MIN_PRICE          = cfg["MIN_PRICE"]
MIN_VOLUME         = cfg["MIN_VOLUME"]
REPEAT_AFTER_MIN   = cfg["REPEAT_AFTER_MIN"]
SESSION_MODE       = cfg["SESSION_MODE"]
CALL_PCT           = cfg["CALL_PCT"]
PUT_PCT            = cfg["PUT_PCT"]
MIN_OPTION_PRICE   = cfg["MIN_OPTION_PRICE"]
SEND_DAILY_SUMMARY = cfg["SEND_DAILY_SUMMARY"]
SUMMARY_HOUR       = cfg["SUMMARY_HOUR"]
SUMMARY_MINUTE     = cfg["SUMMARY_MINUTE"]
WHITELIST          = set([s.upper() for s in cfg["WHITELIST"]])

# ========= Utils =========
def tg(text: str):
    """ส่งข้อความเข้า Telegram"""
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured.")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text}
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print("TG send:", r.status_code, r.text[:200])
    except Exception as e:
        print("TG error:", e)

def label_session():
    """ป้ายบอกช่วงตลาด (แบบง่าย ๆ)"""
    # แค่เป็นข้อความประกอบ ไม่ผูกกับ timezone ตลาดจริง
    return {
        "extended": "🟢 Live / 🟡 Pre / 🔵 After",
        "regular":  "🟢 Live only",
    }.get(SESSION_MODE, "🟢 Live")

def option_tag(pct: float, last_price: float):
    """
    ข้อเสนอไอเดีย Options คร่าว ๆ จาก % เปลี่ยนแปลง และราคาหุ้น
    - CALL เมื่อ pct >= CALL_PCT และราคาหุ้น >= MIN_OPTION_PRICE
    - PUT  เมื่อ pct <= PUT_PCT  และราคาหุ้น >= MIN_OPTION_PRICE
    """
    if last_price is None:
        return ""
    if last_price < MIN_OPTION_PRICE:
        return ""  # หุ้นเล็กราคาเตี้ย ไม่แนะนำออปชั่น
    if pct is None:
        return ""
    if pct >= CALL_PCT:
        return " | Options idea: CALL 📈"
    if pct <= PUT_PCT:
        return " | Options idea: PUT 📉"
    return ""

def _fmt_num(x):
    try:
        return f"{x:,.0f}"
    except:
        return str(x)

def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

# ========= Polygon fetchers =========
BASE = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks"

def fetch_movers(side="gainers"):
    """
    ดึง Top gainers/losers จาก Polygon
    โครงสร้างที่ใช้: item["ticker"], item["todaysChangePerc"], item.get("lastTrade",{}).get("p"), item.get("day",{}).get("v")
    """
    url = f"{BASE}/{side}?apiKey={POLYGON_API_KEY}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    results = data.get("tickers", []) or []
    out = []
    for it in results:
        sym = it.get("ticker")
        pct = it.get("todaysChangePerc")
        last_price = None
        vol = None
        try:
            last_price = it.get("lastTrade", {}).get("p", None)
        except:
            pass
        try:
            vol = it.get("day", {}).get("v", None)
        except:
            pass
        out.append((sym, last_price, pct, vol))
    return out

# ========= Core scan loop =========
def main():
    tg(f"✅ เริ่มสแกน Top Movers (≥{ALERT_PCT:.1f}% | mode: {SESSION_MODE})\n{label_session()}")
    last_alert_time = {}  # sym -> datetime

    while True:
        try:
            # ---- ดึงข้อมูลจาก Polygon
            movers = []
            try:
                g = fetch_movers("gainers")
                movers.extend(g)
            except Exception as e:
                print("Polygon gainers error:", e)

            if INCLUDE_LOSERS:
                try:
                    l = fetch_movers("losers")
                    movers.extend(l)
                except Exception as e:
                    print("Polygon losers error:", e)

            # ---- กรองคุณภาพ & ส่งสัญญาณ
            hits = 0
            for sym, price, pct, vol in movers:
                if not sym:
                    continue
                u_sym = sym.upper()

                # whitelist (ถ้าใส่มา)
                if WHITELIST and u_sym not in WHITELIST:
                    continue

                # กรองราคา/วอลุ่ม
                if price is None or price < MIN_PRICE:
                    continue
                if vol is not None and vol < MIN_VOLUME:
                    continue

                # เกณฑ์ % เปลี่ยนแปลง
                if pct is None:
                    continue
                if abs(pct) < ALERT_PCT:
                    continue

                # กันสแปมซ้ำ
                now = datetime.utcnow()
                last_t = last_alert_time.get(u_sym)
                if last_t and now - last_t < timedelta(minutes=REPEAT_AFTER_MIN):
                    continue
                last_alert_time[u_sym] = now

                # ทำข้อความ
                side = "🔺GAINER" if pct >= 0 else "🔻LOSER"
                opt = option_tag(pct, price)
                msg = (
                    f"{side} ⚠️ QUALIFIED\n"
                    f"{u_sym}  +{pct:.1f}% | ${price:.2f}\n"
                    f"Vol: {_fmt_num(vol) if vol is not None else '-'}\n"
                    f"{opt}\n"
                    f"{now_str()}"
                )
                print("ALERT:", u_sym, pct, price, vol)
                tg(msg)
                hits += 1

            print("hits:", hits)

        except Exception as e:
            print("Loop error:", e)
            tg(f"❗Scanner error: {e}")

        time.sleep(CHECK_INTERVAL_SEC)

# ========= Flask keepalive =========
from flask import Response
app = Flask(__name__)

@app.route("/")
def home():
    return Response("Bot is running fine.", 200)

if __name__ == "__main__":
    # รันสแกนเนอร์ในโปรเซสหลัก และเปิด Flask บนพอร์ต Render
    # หมายเหตุ: บน Render “Web Service” จะรันโปรเซสเดียว
    # เราจึงรันสแกนในเทรดหลัก แล้วให้ Flask เป็นตัวรับ health check ผ่าน waitress แบบง่าย
    import threading

    t = threading.Thread(target=main, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", "10000"))
    # ใช้ werkzeug เดิมก็ได้ (Render ทำ health check ด้วย GET /)
    app.run(host="0.0.0.0", port=port)
