# -*- coding: utf-8 -*-
# Stock Signal Bot (Free mode: previous business day via Polygon /aggs/grouped)
# Adds CALL/PUT signal classification from daily OHLCV (approx. momentum)

import os
import time
import json
import math
import threading
from datetime import datetime, timedelta, timezone
import requests
from flask import Flask

# ===== ENV =====
BOT_TOKEN        = os.environ["BOT_TOKEN"].strip()
CHAT_ID          = os.environ["CHAT_ID"].strip()
POLYGON_API_KEY  = os.environ["POLYGON_API_KEY"].strip()

# ===== SETTINGS (ปรับได้) =====
CHECK_INTERVAL_SEC = 60 * 20   # โหมดฟรี: ดึงทุก ~20 นาทีพอ (ข้อมูลวันเดิม)
ALERT_PCT          = 10.0      # เกณฑ์คัด Top Movers ขั้นต่ำ (±%)
MIN_PRICE          = 0.30
MIN_VOL_FREE       = 0         # ไม่กรองวอลุ่มสำหรับหน้า Top Movers สรุปภาพรวม
# เกณฑ์สำหรับสัญญาณ
STRONG_CALL_MIN_PCT   = 15.0
STRONG_PUT_MAX_PCT    = -12.0
WATCH_CALL_MIN_PCT    = 5.0
WATCH_PUT_MAX_PCT     = -5.0
NEAR_HIGH_CUTOFF      = 0.20   # ปิดใกล้ high (ส่วนที่ห่างจาก high ไม่เกิน 20% ของช่วง)
NEAR_LOW_CUTOFF       = 0.20   # ปิดใกล้ low
THICK_BODY_RATIO      = 0.60   # |(close-open)| / (high-low) >= 0.60
MIN_PRICE_FOR_OPTIONS = 1.00
MIN_VOL_FOR_OPTIONS   = 200_000

TZ_NY = timezone(timedelta(hours=-4))  # EDT (พอใช้สำหรับข้อความบอท)

# ===== Helpers =====
def tg(text: str):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print("TG error:", e)

def fmt_num(x):
    try:
        if x >= 1_000_000_000: return f"{x/1_000_000_000:.2f}B"
        if x >= 1_000_000:     return f"{x/1_000_000:.2f}M"
        if x >= 1_000:         return f"{x/1_000:.2f}K"
        return f"{x:.0f}"
    except:
        return str(x)

def last_business_day_utc():
    # ใช้วันที่ US ล่าสุด (ศุกร์-จันทร์ปรับให้)
    now = datetime.now(timezone.utc)
    d = now.date()
    # ถ้าตอนนี้ก่อนปิดวันในมุม UTC ก็ยังถือว่าวันล่าสุดคือวันก่อนหน้า
    # แต่เพื่อความง่าย เราจะเดินถอยหลังจนกว่าจะเป็นวันจันทร์-ศุกร์
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d

def fetch_grouped(date_iso: str):
    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_iso}?adjusted=true&apiKey={POLYGON_API_KEY}"
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Polygon {r.status_code}: {r.text}")
    data = r.json()
    if data.get("status") != "OK":
        # ใน free จะได้ NOT_AUTHORIZED เมื่อเรียกวันวันนี้ ให้ถอยไปวันก่อนหน้าแล้วใช้
        raise RuntimeError(f"Polygon status: {data.get('status')} | {data.get('message')}")
    return data.get("results", [])

def pct_change_from_open(o, c):
    try:
        if o and o > 0:
            return (c - o) / o * 100.0
    except:
        pass
    return None

def close_position_in_range(c, h, l):
    """return closeness_to_high, closeness_to_low in 0..1 of range (h-l). smaller is 'closer'."""
    rng = max(1e-8, h - l)
    near_high = (h - c) / rng    # 0 => at high, 1 => at low
    near_low  = (c - l) / rng    # 0 => at low, 1 => at high
    return near_high, near_low

def body_ratio(o, c, h, l):
    rng = max(1e-8, h - l)
    return abs(c - o) / rng

def classify_signal(bar):
    """
    รับ bar ที่มี: T(symbol), o,h,l,c,v
    คืน (label, reason) เช่น ("Strong CALL", "pct +23.4%, close near H, thick body, vol 2.3M")
    ถ้าไม่เข้าเกณฑ์คืน (None, None)
    """
    sym = bar.get("T")
    o   = bar.get("o", 0.0)
    h   = bar.get("h", 0.0)
    l   = bar.get("l", 0.0)
    c   = bar.get("c", 0.0)
    v   = int(bar.get("v", 0))

    if c <= 0 or h <= 0 or l <= 0: 
        return (None, None)

    pct = pct_change_from_open(o, c)
    if pct is None:
        return (None, None)

    nh, nl = close_position_in_range(c, h, l)
    br = body_ratio(o, c, h, l)

    # เงื่อนไขเพื่อออก Option signal (ต้องไมโครเพนนี)
    good_for_opt = (c >= MIN_PRICE_FOR_OPTIONS and v >= MIN_VOL_FOR_OPTIONS)

    # Strong CALL
    if pct >= STRONG_CALL_MIN_PCT and nh <= NEAR_HIGH_CUTOFF and br >= THICK_BODY_RATIO and good_for_opt:
        reason = f"pct +{pct:.1f}%, close near H, strong body, ${c:.2f}, Vol {fmt_num(v)}"
        return ("Strong CALL", reason)

    # Watch CALL
    if pct >= WATCH_CALL_MIN_PCT and nh <= (NEAR_HIGH_CUTOFF + 0.10):  # ผ่อนปรนเล็กน้อย
        reason = f"pct +{pct:.1f}%, close near H, ${c:.2f}, Vol {fmt_num(v)}"
        return ("Watch CALL", reason)

    # Strong PUT
    if pct <= STRONG_PUT_MAX_PCT and nl <= NEAR_LOW_CUTOFF and br >= THICK_BODY_RATIO and good_for_opt:
        reason = f"pct {pct:.1f}%, close near L, strong body, ${c:.2f}, Vol {fmt_num(v)}"
        return ("Strong PUT", reason)

    # Watch PUT
    if pct <= WATCH_PUT_MAX_PCT and nl <= (NEAR_LOW_CUTOFF + 0.10):
        reason = f"pct {pct:.1f}%, close near L, ${c:.2f}, Vol {fmt_num(v)}"
        return ("Watch PUT", reason)

    return (None, None)

def summarize_top_movers(bars, date_used):
    # กรองพื้นฐานสำหรับรายการ Top Movers ดูภาพรวม
    lst = []
    for b in bars:
        c, o, v, sym = b.get("c", 0.0), b.get("o", 0.0), int(b.get("v", 0)), b.get("T")
        if c is None or o in (None, 0): 
            continue
        pct = pct_change_from_open(o, c)
        if pct is None:
            continue
        if abs(pct) >= ALERT_PCT and c >= MIN_PRICE and v >= MIN_VOL_FREE:
            lst.append((pct, sym, c, v))
    # เรียงจากขึ้นแรง → ลงแรง
    lst.sort(key=lambda x: -x[0])
    gainers = [x for x in lst if x[0] >= ALERT_PCT]
    losers  = [x for x in lst if x[0] <= -ALERT_PCT]

    lines = []
    lines.append("✅ <b>Top Movers</b> (ฟรี, ย้อนหลังวันล่าสุด)")
    lines.append(f"วันที่อ้างอิง: <code>{date_used}</code>")
    lines.append(f"เกณฑ์: ≥{ALERT_PCT:.1f}% | ราคา ≥{MIN_PRICE} | Vol ≥{MIN_VOL_FREE}")
    if gainers:
        lines.append("\n📈 <u>ขึ้นแรง:</u>")
        for pct, sym, c, v in gainers[:20]:
            lines.append(f"• {sym} +{pct:.1f}% @{c:.2f} Vol:{fmt_num(v)}")
    if losers:
        lines.append("\n📉 <u>ลงแรง:</u>")
        for pct, sym, c, v in losers[:20]:
            lines.append(f"• {sym} {pct:.1f}% @{c:.2f} Vol:{fmt_num(v)}")
    return "\n".join(lines)

def summarize_option_signals(bars, date_used):
    groups = {"Strong CALL": [], "Watch CALL": [], "Strong PUT": [], "Watch PUT": []}
    for b in bars:
        label, reason = classify_signal(b)
        if label:
            sym = b.get("T")
            c   = b.get("c", 0.0)
            groups[label].append((sym, c, reason))

    if not any(groups.values()):
        return f"🧭 <b>Option Signals (ฟรี)</b>\nวันที่อ้างอิง: <code>{date_used}</code>\nยังไม่พบสัญญาณที่เข้าเกณฑ์"

    order = ["Strong CALL", "Watch CALL", "Strong PUT", "Watch PUT"]
    title = f"🧭 <b>Option Signals (ฟรี)</b>\nวันที่อ้างอิง: <code>{date_used}</code>"
    lines = [title]
    for key in order:
        arr = groups[key]
        if not arr: 
            continue
        icon = "💚" if "CALL" in key else "❤️"
        lines.append(f"\n{icon} <u>{key}</u>")
        # จำกัดแสดงกลุ่มละ 15 ตัว
        for sym, c, reason in arr[:15]:
            lines.append(f"• {sym} @{c:.2f} — {reason}")
    return "\n".join(lines)

def scan_free_once():
    # ใช้วันทำการล่าสุดที่ Polygon ให้เรียกฟรี (ต้องไม่ใช่วันวันนี้)
    d = last_business_day_utc()
    date_iso = d.isoformat()
    # ถ้าเรียกวันนี้เจอ NOT_AUTHORIZED ให้ถอยเอง 1 วัน
    for _ in range(3):
        try:
            bars = fetch_grouped(date_iso)
            return date_iso, bars
        except Exception as e:
            msg = str(e)
            if "NOT_AUTHORIZED" in msg or "Attempted to request today's data" in msg:
                d = d - timedelta(days=1)
                date_iso = d.isoformat()
                continue
            raise

def worker_loop():
    tg("🟢 เริ่มทำงานโหมดฟรี (ดึงข้อมูลวันทำการล่าสุดจาก Polygon)")
    last_sent_date = None
    while True:
        try:
            date_iso, bars = scan_free_once()
            # ส่ง Top movers (ถ้ายังไม่ส่งของวันนั้น)
            if last_sent_date != date_iso:
                txt = summarize_top_movers(bars, date_iso)
                tg(txt)
                txt2 = summarize_option_signals(bars, date_iso)
                tg(txt2)
                last_sent_date = date_iso
            else:
                # วันเดียวกันแล้ว ข้ามเพื่อไม่สแปม (โหมดฟรีข้อมูลไม่เปลี่ยน)
                pass
        except Exception as e:
            print("Loop error:", e)
            tg(f"❗️Scanner error (free): {e}")
        time.sleep(CHECK_INTERVAL_SEC)

# ===== Flask (keep alive) =====
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running fine."

def run_flask():
    app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    t1 = threading.Thread(target=worker_loop, daemon=True)
    t1.start()
    run_flask()
