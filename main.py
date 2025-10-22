# -*- coding: utf-8 -*-
"""
Stock Signal Bot (Free mode with Polygon.io previous-day data)
- /help       : เมนูคำสั่ง
- /movers     : Top movers (โหมดฟรี อ้างอิงวันก่อนหน้า)
- /signals    : จัดกลุ่ม Strong/Watch (CALL/PUT) + แนวคิดการเล่น
- /outlook    : สรุปแนวโน้มวันนี้จากข้อมูลเมื่อวาน
- health      : หน้าสถานะ Flask + webhook สำหรับ Render

ต้องตั้ง ENV บน Render:
BOT_TOKEN, POLYGON_API_KEY, PUBLIC_URL, PORT
(ออปชัน) CHAT_ID  ถ้าอยาก broadcast ไปห้องเดียวอัตโนมัติ
"""

import os
import json
import time
import math
import logging
import traceback
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify
from telegram import Update, ParseMode
from telegram.ext import (
    Updater, Dispatcher, CommandHandler, CallbackContext
)

# -------------------- ตั้งค่า logger --------------------
logger = logging.getLogger("stock-bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# -------------------- ENV --------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
POLYGON_API_KEY = os.environ["POLYGON_API_KEY"]
PUBLIC_URL = os.environ["PUBLIC_URL"]  # eg. https://your-app.onrender.com
PORT = int(os.environ.get("PORT", "10000"))
DEFAULT_CHAT_ID = os.environ.get("CHAT_ID")  # optional

# เกณฑ์ค่าเริ่มต้น (แก้ได้ตามสะดวก)
DEFAULT_MIN_PCT   = 10.0   # เปลี่ยนแปลง (%) ขั้นต่ำ
DEFAULT_MIN_PRICE = 0.30   # ราคาปิดขั้นต่ำ
DEFAULT_MIN_VOL   = 0      # ปริมาณขั้นต่ำ

# -------------------- Utilities --------------------
def _fmt_num(x, nd=2):
    try:
        if x is None: return "-"
        if isinstance(x, (int, float)):
            if abs(x) >= 1e9:  # billions
                return f"{x/1e9:.{nd}f}B"
            if abs(x) >= 1e6:  # millions
                return f"{x/1e6:.{nd}f}M"
            if abs(x) >= 1e3:  # thousands
                return f"{x/1e3:.{nd}f}K"
            return f"{x:.{nd}f}".rstrip("0").rstrip(".")
        return str(x)
    except:
        return str(x)

def _chunk_send(bot, chat_id, text, preview=False):
    CHUNK = 3900
    if len(text) <= CHUNK:
        bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=not preview)
    else:
        for i in range(0, len(text), CHUNK):
            bot.send_message(chat_id, text[i:i+CHUNK], parse_mode=ParseMode.HTML, disable_web_page_preview=not preview)

def _us_prev_trading_date_rough():
    # แบบรวดเร็ว: เอา "เมื่อวาน" ของ UTC (โหมดฟรี Polygon จะเปิดให้ดึงได้ถึง T-1)
    return (datetime.utcnow().date() - timedelta(days=1)).isoformat()

# -------------------- ดึงข้อมูล Polygon (โหมดฟรี: previous-day grouped) --------------------
def polygon_grouped_day(date_iso):
    """
    เรียก grouped aggs (T-1) จาก Polygon (โหมดฟรีใช้ข้อมูลถึง 'เมื่อวาน')
    """
    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_iso}"
    params = {
        "adjusted": "true",
        "apiKey": POLYGON_API_KEY
    }
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Polygon {r.status_code}: {r.text}")
    data = r.json()
    if data.get("status") != "OK":
        # โหมดฟรี ถ้าขอวันปัจจุบันจะได้ NOT_AUTHORIZED
        # ให้โยน error ขึ้นไปเพื่อให้โค้ดไปดึงวันก่อนแทน
        raise RuntimeError(data.get("message") or str(data))
    return data.get("results", [])

def fetch_top_movers_free(date_iso=None, min_pct=DEFAULT_MIN_PCT, min_price=DEFAULT_MIN_PRICE, min_vol=DEFAULT_MIN_VOL):
    """
    คืน list ของ dict ต่อหุ้น: { 'T':symbol, 'o','h','l','c','v','pct','close_near_high','close_near_low' }
    กรองด้วยเกณฑ์ที่กำหนด
    """
    if not date_iso:
        date_iso = _us_prev_trading_date_rough()

    try:
        rows = polygon_grouped_day(date_iso)
    except Exception as e:
        # ถ้าเจอ NOT_AUTHORIZED เพราะขอวันปัจจุบัน ลองถอยไปอีกวัน
        logger.warning("Primary fetch failed (%s), retry with date-1", e)
        d = (datetime.fromisoformat(date_iso) - timedelta(days=1)).date().isoformat()
        rows = polygon_grouped_day(d)
        date_iso = d

    out = []
    for r in rows:
        # polygon fields: T=Ticker, o=open, h=high, l=low, c=close, v=volume
        T = r.get("T")
        o = r.get("o")
        h = r.get("h")
        l = r.get("l")
        c = r.get("c")
        v = r.get("v")
        if not (T and o and c and h and l and v is not None):
            continue
        if o <= 0 or c <= 0:
            continue

        price_ok = c >= min_price
        vol_ok   = (v or 0) >= min_vol
        pct = (c - o) * 100.0 / o
        pct_ok = abs(pct) >= min_pct

        if price_ok and vol_ok and pct_ok:
            # ความใกล้ High/Low
            near_high = (h > 0 and (h - c) / h <= 0.02)  # ปิดใกล้ High <=2%
            near_low  = (l > 0 and (c - l) / max(c, 1e-9) <= 0.02)  # ปิดใกล้ Low <=2%
            out.append({
                "T": T, "o": o, "h": h, "l": l, "c": c, "v": v,
                "pct": pct,
                "close_near_high": near_high,
                "close_near_low": near_low
            })
    # เรียงเลือกเด่นสุด (เอาฝั่งบวกก่อน ตามสัดส่วนการเปลี่ยนแปลง)
    out.sort(key=lambda x: (-x["pct"], -x["v"]))
    return out

# -------------------- จัดกลุ่มสัญญาณ --------------------
def build_signals_from_day(rows, header=""):
    """
    แบ่งเป็น Strong CALL / Watch CALL / Strong PUT / Watch PUT
    criteria คร่าว ๆ:
      Strong CALL: pct >= 15, ปิดใกล้ High
      Watch  CALL: pct >= 10, ไม่ถึงเงื่อนไข Strong
      Strong PUT : pct <= -15, ปิดใกล้ Low
      Watch  PUT : pct <= -10, ไม่ถึงเงื่อนไข Strong
    """
    strong_call, watch_call, strong_put, watch_put = [], [], [], []

    for r in rows:
        sym, pct, c, h, l, v = r["T"], r["pct"], r["c"], r["h"], r["l"], r["v"]
        near_h, near_l = r["close_near_high"], r["close_near_low"]

        if pct >= 15 and near_h:
            strong_call.append((sym, pct, c, v, h))
        elif pct >= 10:
            watch_call.append((sym, pct, c, v, h))
        elif pct <= -15 and near_l:
            strong_put.append((sym, pct, c, v, l))
        elif pct <= -10:
            watch_put.append((sym, pct, c, v, l))

    def _line(name, bucket):
        if not bucket:
            return f"• {name}: -"
        # จำกัดโชว์ 30
        bucket = bucket[:30]
        s = [f"{t[0]} @{_fmt_num(t[2])} — pct {_fmt_num(t[1],1)}%, Vol {_fmt_num(t[3])}" for t in bucket]
        return "• " + name + ": " + ", ".join(s)

    lines = []
    if header:
        lines.append(header.strip())
    lines += [
        "🟣 <b>คาดการณ์แนวโน้มวันนี้</b> (อิงจากข้อมูลเมื่อวาน)",
        _line("Momentum ขาขึ้น — <b>Strong CALL 30</b>", strong_call),
        _line("ลุ้นเบรกขึ้น — <b>Watch CALL 30</b>", watch_call),
        _line("Momentum ขาลง — <b>Strong PUT 30</b>", strong_put),
        _line("ระวังอ่อนแรง — <b>Watch PUT 30</b>", watch_put),
        "",
        "💡 <b>แนวคิด:</b>",
        "• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าจ่อจุดหนุน",
        "• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม",
        "• Strong PUT ลงต่อหรือรีบาวน์สั้น",
        "• Watch PUT ระวังหลุดแนวรับ",
    ]
    return "\n".join(lines)

# -------------------- Cache ง่าย ๆ กันเรียกซ้ำ --------------------
_cache = {
    "signals": {"date": None, "text": None},
    "movers": {"date": None, "text": None},
    "outlook": {"date": None, "text": None},
}

# -------------------- Telegram Handlers --------------------
def cmd_help(update: Update, context: CallbackContext):
    text = (
        "👋 ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\n"
        "คำสั่งที่ใช้ได้\n"
        "• /movers – ดู Top Movers (ฟรี)\n"
        "• /signals – จัดกลุ่ม Watch/Strong (CALL/PUT)\n"
        "• /outlook – คาดการณ์โมเมนตัมวันนี้ (อิงจากเมื่อวาน)\n"
        "• /help – ดูเมนูนี้อีกครั้ง\n\n"
        f"เกณฑ์: pct ≥ {DEFAULT_MIN_PCT:.1f}%, ราคา ≥ {DEFAULT_MIN_PRICE:.2f}, Vol ≥ {DEFAULT_MIN_VOL}"
    )
    update.message.reply_text(text)

def cmd_movers(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    try:
        update.message.reply_text("⏳ กำลังดึง Top Movers (โหมดฟรี)…")

        d = _us_prev_trading_date_rough()

        # ใช้แคช
        if _cache["movers"]["date"] == d and _cache["movers"]["text"]:
            _chunk_send(context.bot, chat_id, _cache["movers"]["text"])
            return

        rows = fetch_top_movers_free(date_iso=d)
        if not rows:
            context.bot.send_message(chat_id, "⚠️ ไม่มีรายการที่ผ่านเกณฑ์ในโหมดฟรี")
            return

        # สรุปแสดง TOP 20 ขาขึ้น
        ups = [r for r in rows if r["pct"] > 0][:20]
        lines = [
            f"✅ <b>Top Movers</b> (ฟรี, ย้อนหลังวันล่าสุด)\nวันอ้างอิง: {d}\n"
            f"เกณฑ์: ≥ {DEFAULT_MIN_PCT:.1f}% | ราคา ≥ {DEFAULT_MIN_PRICE} | Vol ≥ {DEFAULT_MIN_VOL}\n",
            "📈 <b>ขึ้นแรง:</b>"
        ]
        for r in ups:
            lines.append(f"• {r['T']} @{_fmt_num(r['c'])} — pct {_fmt_num(r['pct'],1)}%, Vol {_fmt_num(r['v'])}")

        text = "\n".join(lines)
        _cache["movers"]["date"] = d
        _cache["movers"]["text"] = text
        _chunk_send(context.bot, chat_id, text)

    except Exception as e:
        logger.error("movers error: %s\n%s", e, traceback.format_exc())
        context.bot.send_message(chat_id, f"❌ Movers error: {e}")

def cmd_signals(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    try:
        context.bot.send_message(chat_id, "⏳ กำลังคัดสัญญาณจากข้อมูลวันล่าสุด (โหมดฟรี)…")

        d = _us_prev_trading_date_rough()
        if _cache["signals"]["date"] == d and _cache["signals"]["text"]:
            _chunk_send(context.bot, chat_id, _cache["signals"]["text"])
            return

        rows = fetch_top_movers_free(date_iso=d)
        if not rows:
            context.bot.send_message(chat_id, "⚠️ วันนี้ยังไม่มีข้อมูลที่ตรงเกณฑ์จากโหมดฟรี")
            return

        text = build_signals_from_day(
            rows,
            header=f"🔮 คัดสัญญาณจาก {d} (โหมดฟรีจาก Polygon)"
        )
        _cache["signals"]["date"] = d
        _cache["signals"]["text"] = text
        _chunk_send(context.bot, chat_id, text)

    except Exception as e:
        logger.error("signals error: %s\n%s", e, traceback.format_exc())
        context.bot.send_message(chat_id, f"❌ Signals error: {e}")

def cmd_outlook(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    try:
        context.bot.send_message(chat_id, "⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon…")

        d = _us_prev_trading_date_rough()
        if _cache["outlook"]["date"] == d and _cache["outlook"]["text"]:
            _chunk_send(context.bot, chat_id, _cache["outlook"]["text"])
            return

        rows = fetch_top_movers_free(date_iso=d)
        if not rows:
            context.bot.send_message(chat_id, "⚠️ ยังไม่มีข้อมูลผ่านเกณฑ์")
            return

        # คร่าว ๆ: ใช้ build_signals เดิมแล้วบรรทัดหัว + แนวคิด
        text = build_signals_from_day(
            rows,
            header="⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon…"
        )
        _cache["outlook"]["date"] = d
        _cache["outlook"]["text"] = text
        _chunk_send(context.bot, chat_id, text)

    except Exception as e:
        logger.error("outlook error: %s\n%s", e, traceback.format_exc())
        context.bot.send_message(chat_id, f"❌ Outlook error: {e}")

# -------------------- Telegram Setup (Webhook) --------------------
updater = Updater(BOT_TOKEN, use_context=True)
dispatcher: Dispatcher = updater.dispatcher

# คำสั่ง
dispatcher.add_handler(CommandHandler("help", cmd_help))
dispatcher.add_handler(CommandHandler("movers", cmd_movers))
dispatcher.add_handler(CommandHandler(["signals", "signal"], cmd_signals))
dispatcher.add_handler(CommandHandler("outlook", cmd_outlook))

# -------------------- Flask (health + webhook) --------------------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bot is running fine.", 200

@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    url = f"{PUBLIC_URL}/webhook"
    ok = updater.bot.set_webhook(url=url, max_connections=40)
    return jsonify({"ok": ok, "webhook": url})

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), updater.bot)
        dispatcher.process_update(update)
    except Exception as e:
        logger.error("webhook error: %s\n%s", e, traceback.format_exc())
    return "ok", 200

# -------------------- Entry --------------------
if __name__ == "__main__":
    # ตั้ง webhook ตอนสตาร์ท (เผื่อกรณีต้องการ)
    try:
        url = f"{PUBLIC_URL}/webhook"
        updater.bot.set_webhook(url=url, max_connections=40)
        logger.info("Webhook set to %s", url)
    except Exception as e:
        logger.warning("Set webhook failed: %s", e)

    # รัน Flask เพื่อให้ Render ผูกพอร์ตได้
    app.run(host="0.0.0.0", port=PORT)
