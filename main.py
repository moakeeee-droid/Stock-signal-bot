# -*- coding: utf-8 -*-
"""
Stock-signal bot (Free mode with Polygon.io)
- /movers   : Top movers (free, previous trading day)
- /signals  : Group CALL/PUT watch/strong (from previous day)
- /outlook  : Summary outlook for today (derived from yesterday)
- /help     : Show menu

NEW:
- API call caching (default 10 min) -> ENV: CACHE_TTL_MIN
- Simple rate-limit guard (default 5 calls / 60s) -> ENV: API_MAX_CALLS_PER_MIN
- Retry with backoff on 429/5xx, graceful fallback to cached data
"""

import os
import time
import json
import math
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask

# telegram v13 (long-polling)
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# -------------------- ENV & Globals --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # optional broadcast room
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()

CACHE_TTL_MIN = int(os.getenv("CACHE_TTL_MIN", "10"))
API_MAX_CALLS_PER_MIN = int(os.getenv("API_MAX_CALLS_PER_MIN", "5"))

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env")
if not POLYGON_API_KEY:
    raise RuntimeError("Missing POLYGON_API_KEY env")

# Simple in-memory cache
_cache = {}  # key -> {"ts": epoch_sec, "data": any}
_cache_lock = threading.Lock()

# Simple rate limiter (token-bucket-ish)
_call_times = deque()  # timestamps of API calls (epoch_sec)
_call_lock = threading.Lock()

# Flask (health)
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running. ✅"


# -------------------- Utils --------------------
def _now():
    return time.time()


def _cache_get(key, ttl_sec):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        if _now() - entry["ts"] <= ttl_sec:
            return entry["data"]
        # expired
        _cache.pop(key, None)
        return None


def _cache_put(key, data):
    with _cache_lock:
        _cache[key] = {"ts": _now(), "data": data}


def _rate_guard():
    """Ensure we don't exceed API_MAX_CALLS_PER_MIN within 60s window."""
    if API_MAX_CALLS_PER_MIN <= 0:
        return
    with _call_lock:
        now = _now()
        # purge older than 60s
        while _call_times and now - _call_times[0] > 60:
            _call_times.popleft()
        if len(_call_times) >= API_MAX_CALLS_PER_MIN:
            # sleep until first timestamp exits 60s window
            wait_s = 60 - (now - _call_times[0]) + 0.2
            if wait_s > 0:
                time.sleep(wait_s)
        _call_times.append(_now())


def _fetch_json_with_retry(url, params=None, timeout=20, max_retry=3):
    """GET with retries on 429/5xx + rate guard. Return (ok, data or error_str)."""
    backoff = 3.0
    for attempt in range(1, max_retry + 1):
        _rate_guard()
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except Exception as e:
            err = f"network error: {e}"
            if attempt == max_retry:
                return False, err
            time.sleep(backoff)
            backoff *= 1.8
            continue

        if r.status_code == 200:
            try:
                return True, r.json()
            except Exception as e:
                return False, f"bad json: {e}"

        # 429 or server error -> backoff
        if r.status_code in (429, 500, 502, 503, 504):
            if attempt == max_retry:
                # give caller a last chance to use cache
                return False, f"HTTP {r.status_code}"
            time.sleep(backoff)
            backoff *= 1.8
            continue

        # other client error
        try:
            msg = r.json().get("message", r.text)
        except Exception:
            msg = r.text
        return False, f"HTTP {r.status_code}: {msg}"


def previous_us_trading_day(base_dt=None):
    """Rough previous day (free mode uses 'previous calendar day' => good enough)."""
    tz = timezone.utc
    d = (base_dt or datetime.now(tz)).date() - timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# -------------------- Polygon free endpoint --------------------
def fetch_grouped_prev_day():
    """
    Free mode: use yesterday's grouped bars.
    Cache key: 'grouped_prev_day::<date>'
    """
    date = previous_us_trading_day()
    cache_key = f"grouped_prev_day::{date}"
    ttl_sec = CACHE_TTL_MIN * 60

    cached = _cache_get(cache_key, ttl_sec)
    if cached is not None:
        return True, cached, True  # (ok, data, from_cache)

    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date}"
    params = {"adjusted": "true", "apiKey": POLYGON_API_KEY}
    ok, payload = _fetch_json_with_retry(url, params=params)
    if ok and isinstance(payload, dict) and payload.get("results"):
        _cache_put(cache_key, payload)
        return True, payload, False
    else:
        # try last cached (even if expired) as graceful fallback
        fallback = None
        with _cache_lock:
            fallback = _cache.get(cache_key)
        if fallback:
            return True, fallback["data"], True
        return False, payload, False


# -------------------- Business logic (simple demo rules) --------------------
def parse_results(payload):
    """Return list of items with fields: T (symbol), c (close), h, l, o, v, pct."""
    items = []
    for r in payload.get("results", []):
        try:
            T = r.get("T")
            c = float(r.get("c"))
            o = float(r.get("o"))
            h = float(r.get("h"))
            l = float(r.get("l"))
            v = float(r.get("v"))
            pct = 100.0 * (c - o) / o if o else 0.0
        except Exception:
            continue
        items.append({"T": T, "c": c, "o": o, "h": h, "l": l, "v": v, "pct": pct})
    return items


def fmt_num(x, d=2):
    try:
        return f"{x:,.{d}f}"
    except Exception:
        return str(x)


def movers_text(items, min_pct=10.0, min_price=0.30, min_vol=0):
    up = [it for it in items if it["pct"] >= min_pct and it["c"] >= min_price and it["v"] >= min_vol]
    up.sort(key=lambda x: (-x["pct"], -x["v"]))
    lines = ["✅ Top Movers (ฟรี, ย้อนหลังวันล่าสุด)",
             f"เกณฑ์: ≥{min_pct:.1f}% | ราคา ≥{min_price} | Vol ≥{min_vol}",
             "📈 ขึ้นแรง:"]
    for it in up[:30]:
        lines.append(f"• {it['T']} @{fmt_num(it['c'])} — pct {fmt_num(it['pct'],1)}%, Vol:{fmt_num(it['v'],0)}")
    if len(up) == 0:
        lines.append("• (ไม่พบตามเกณฑ์)")
    return "\n".join(lines)


def classify_signals(items):
    """Very simple rules to group Watch/Strong CALL/PUT by pct & close near H/L."""
    def close_near(top, price):
        # within 2% of top
        return abs(top - price) <= max(0.02 * top, 1e-9)

    strong_call, watch_call, strong_put, watch_put = [], [], [], []
    for it in items:
        T, c, o, h, l, v, pct = it["T"], it["c"], it["o"], it["h"], it["l"], it["v"], it["pct"]
        # CALL side
        if pct >= 10 and close_near(h, c):
            strong_call.append(T)
        elif pct >= 5 and close_near(h, c):
            watch_call.append(T)
        # PUT side
        if pct <= -10 and close_near(l, c):
            strong_put.append(T)
        elif pct <= -5 and close_near(l, c):
            watch_put.append(T)
    return strong_call, watch_call, strong_put, watch_put


def outlook_text(sc, wc, sp, wp):
    lines = ["🧭 Outlook (อิงข้อมูลเมื่อวาน)",
             f"• Strong CALL: {len(sc)} | Watch CALL: {len(wc)}",
             f"• Strong PUT : {len(sp)} | Watch PUT : {len(wp)}"]
    # quick view
    bias = "กลาง"
    if len(sc) + len(wc) > len(sp) + len(wp) + 10:
        bias = "เอียงขึ้น"
    elif len(sp) + len(wp) > len(sc) + len(wc) + 10:
        bias = "เอียงลง"
    lines.append(f"→ โมเมนตัมรวม: {bias}")
    lines.append("")
    lines.append("พอร์ตสั้นวันเดียว: ตามน้ำกลุ่ม Strong, รอจังหวะใน Watch")
    return "\n".join(lines)


# -------------------- Telegram Handlers --------------------
def send_rate_note(context: CallbackContext, used_cache: bool, ok: bool, err=None):
    if used_cache:
        context.bot.send_message(
            chat_id=context._chat_id_and_data[0],
            text="(เปิดจากแคชล่าสุดเพื่อเลี่ยงลิมิต/429)"
        )
    elif not ok and isinstance(err, str) and "429" in err:
        context.bot.send_message(
            chat_id=context._chat_id_and_data[0],
            text="ขออภัย: 429 (เรียกข้อมูลบ่อยเกินไป) — รอสักครู่แล้วลองอีกครั้ง หรือใช้แคชด้วยคำสั่งเดิมภายใน 10 นาที"
        )


def cmd_movers(update: Update, context: CallbackContext):
    msg_wait = context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...")
    ok, payload, from_cache = fetch_grouped_prev_day()
    if not ok:
        context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg_wait.message_id,
                                      text=f"ขออภัย ดึงข้อมูลไม่สำเร็จ: {payload}")
        send_rate_note(context, from_cache, ok, payload)
        return
    items = parse_results(payload)
    text = movers_text(items)
    context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg_wait.message_id, text=text)
    send_rate_note(context, from_cache, ok)


def cmd_signals(update: Update, context: CallbackContext):
    msg_wait = context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ กำลังจัดกลุ่มสัญญาณ (ฟรี, ย้อนหลังวันล่าสุด)...")
    ok, payload, from_cache = fetch_grouped_prev_day()
    if not ok:
        context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg_wait.message_id,
                                      text=f"ขออภัย ดึงข้อมูลไม่สำเร็จ: {payload}")
        send_rate_note(context, from_cache, ok, payload)
        return
    items = parse_results(payload)
    sc, wc, sp, wp = classify_signals(items)
    def show(title, arr):
        hemi = ", ".join(arr[:30]) if arr else "(ไม่มี)"
        return f"• {title} — ตัวอย่าง: {hemi}"
    text = "🔮 คาดการณ์แนวโน้มวันนี้ (อิงจากข้อมูลเมื่อวาน)\n" + \
           show("Momentum ขาขึ้น: Strong CALL 30", sc) + "\n" + \
           show("ลุ้นเบรกขึ้น: Watch CALL 30", wc) + "\n" + \
           show("Momentum ขาลง: Strong PUT 30", sp) + "\n" + \
           show("ระวังอ่อนแรง: Watch PUT 30", wp) + "\n\n" + \
           "💡 แนวคิด:\n• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าจ่อจุดหนุน\n" + \
           "• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม\n" + \
           "• Strong PUT ลงต่อหรือรีบาวน์สั้น\n" + \
           "• Watch PUT ระวังหลุดแนวรับ"
    context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg_wait.message_id, text=text)
    send_rate_note(context, from_cache, ok)


def cmd_outlook(update: Update, context: CallbackContext):
    msg_wait = context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...")
    ok, payload, from_cache = fetch_grouped_prev_day()
    if not ok:
        context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg_wait.message_id,
                                      text=f"ขออภัย ดึงข้อมูลไม่สำเร็จ: {payload}")
        send_rate_note(context, from_cache, ok, payload)
        return
    items = parse_results(payload)
    sc, wc, sp, wp = classify_signals(items)
    text = outlook_text(sc, wc, sp, wp)
    context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=msg_wait.message_id, text=text)
    send_rate_note(context, from_cache, ok)


def cmd_help(update: Update, context: CallbackContext):
    text = (
        "👋 ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\n"
        "คำสั่งที่ใช้ได้\n"
        "• /movers  – ดู Top Movers (ฟรี)\n"
        "• /signals – จัดกลุ่ม Strong/Watch (CALL/PUT)\n"
        "• /outlook – คาดการณ์โมเมนตัมวันนี้ (อิงเมื่อวาน)\n"
        "• /help    – ดูเมนูนี้อีกครั้ง\n\n"
        f"เกณฑ์เบื้องต้น: pct ≥ 10.0%, ราคา ≥ 0.30, Vol ≥ 0\n"
        f"(cache {CACHE_TTL_MIN} นาที • limit {API_MAX_CALLS_PER_MIN}/นาที)"
    )
    context.bot.send_message(chat_id=update.effective_chat.id, text=text)


# -------------------- Runner --------------------
def run_telegram_longpoll():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("movers", cmd_movers))
    dp.add_handler(CommandHandler("signals", cmd_signals))
    dp.add_handler(CommandHandler("outlook", cmd_outlook))
    dp.add_handler(CommandHandler("help", cmd_help))

    print("[info] starting telegram long-polling…")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()


if __name__ == "__main__":
    # Start Telegram in a background thread
    t = threading.Thread(target=run_telegram_longpoll, daemon=True)
    t.start()

    # Start Flask to keep Render happy (port binding)
    app.run(host="0.0.0.0", port=PORT, debug=False)
