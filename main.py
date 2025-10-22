# -*- coding: utf-8 -*-
"""
Stock-signal-bot (free mode skeleton)
- Telegram: python-telegram-bot v20+
- Web: Flask (for Render healthcheck / logs)
- Run model: Telegram long-polling in a background thread + Flask main thread
- ENV: BOT_TOKEN, POLYGON_API_KEY (optional), CHAT_ID (optional), PUBLIC_URL (optional), PORT
"""

import os
import sys
import threading
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("stock-signal-bot")

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # optional broadcast
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is empty! Bot will not start until you set it.")

# =========================
# Flask app (health / home)
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "Stock-signal-bot is running."

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat()})


# =========================
# Helpers
# =========================
def _fmt_num(x, digits=2):
    try:
        return f"{float(x):,.{digits}f}"
    except Exception:
        return str(x)

def _yesterday_us():
    # ล่าสุด “วันทำการ” ของ US (แบบหยาบ ๆ: เมื่อวาน)
    tz = timezone.utc
    d = datetime.now(tz) - timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def fetch_polygon_grouped(date_str):
    """
    ดึง Top movers แบบฟรีจาก Polygon (อ้างอิงวันก่อนหน้า)
    ถ้า key ไม่พอ/แผนฟรีไม่ผ่าน จะคืน []
    """
    if not POLYGON_API_KEY:
        return []

    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_str}"
    params = {"adjusted": "true", "apiKey": POLYGON_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            log.warning("Polygon status %s: %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        if not isinstance(data, dict) or data.get("status") != "OK":
            log.warning("Polygon response: %s", str(data)[:200])
            return []
        results = data.get("results", []) or []
        # results: list of {T: ticker, c: close, o: open, h: high, l: low, v: volume, ...}
        movers = []
        for it in results:
            t = it.get("T")
            o = it.get("o")
            c = it.get("c")
            v = it.get("v")
            if t is None or o in (None, 0) or c is None:
                continue
            pct = (c - o) / o * 100.0
            movers.append({
                "ticker": t,
                "open": o,
                "close": c,
                "pct": pct,
                "vol": v,
                "high": it.get("h"),
                "low": it.get("l"),
            })
        # เรียงตาม % เปลี่ยนแปลงมากสุด
        movers.sort(key=lambda x: abs(x["pct"]), reverse=True)
        return movers
    except Exception as e:
        log.exception("fetch_polygon_grouped error: %s", e)
        return []


def build_movers_text(movers, limit=20, min_price=0.3, min_pct=10.0, min_vol=0):
    if not movers:
        return "ยังไม่มีข้อมูล (โหมดฟรี: ต้องรอดึงวันทำการล่าสุดจาก Polygon)\nลองใหม่อีกครั้ง /movers"
    lines = []
    lines.append("✅ Top Movers (ฟรี, ย้อนหลังวันล่าสุด)")
    lines.append(f"เกณฑ์: ≥{_fmt_num(min_pct,1)}% | ราคา ≥{min_price} | Vol ≥{min_vol}")
    lines.append("📈 ขึ้นแรง:")
    cnt = 0
    for m in movers:
        if m["pct"] >= min_pct and m["close"] >= min_price and (m["vol"] or 0) >= min_vol:
            lines.append(f"• {m['ticker']} @{_fmt_num(m['close'])} — pct +{_fmt_num(m['pct'],1)}%, Vol {_fmt_num(m['vol'],0)}")
            cnt += 1
            if cnt >= limit:
                break
    if cnt == 0:
        lines.append("• (ไม่พบที่ตรงเกณฑ์)")
    return "\n".join(lines)


def classify_signals(movers):
    """
    แยกกลุ่มสัญญาณแบบง่าย:
      - Strong CALL: pct >= +15% และปิดใกล้ High
      - Watch CALL : pct >= +7%
      - Strong PUT : pct <= -15% และปิดใกล้ Low
      - Watch PUT  : pct <= -7%
    (เป็นตัวอย่างเบื้องต้น สามารถปรับสูตรทีหลังได้)
    """
    if not movers:
        return {"sc": [], "wc": [], "sp": [], "wp": []}

    sc, wc, sp, wp = [], [], [], []
    for m in movers:
        c, h, l = m["close"], m.get("high"), m.get("low")
        pct = m["pct"]
        near_high = (h is not None and h != 0 and abs(h - c) <= max(0.02*h, 0.02))  # ~ใกล้ high
        near_low  = (l is not None and l != 0 and abs(c - l) <= max(0.02*l, 0.02))  # ~ใกล้ low

        if pct >= 15.0 and near_high:
            sc.append(m["ticker"])
        elif pct >= 7.0:
            wc.append(m["ticker"])
        elif pct <= -15.0 and near_low:
            sp.append(m["ticker"])
        elif pct <= -7.0:
            wp.append(m["ticker"])

    return {"sc": sc[:30], "wc": wc[:30], "sp": sp[:30], "wp": wp[:30]}

def build_signals_text(movers):
    if not movers:
        return "ยังไม่มีข้อมูลสัญญาณ (โหมดฟรีต้องรอดึงข้อมูลวันล่าสุด)\nพิมพ์ /movers เพื่อลองดูรายการ"
    g = classify_signals(movers)
    lines = []
    lines.append("🧠 คาดการณ์แนวโน้มวันนี้ (อิงจากข้อมูลเมื่อวาน)")
    lines.append(f"• Momentum ขาขึ้น: Strong CALL {len(g['sc'])} — ตัวอย่าง: {', '.join(g['sc'][:12]) or '-'}")
    lines.append(f"• ลุ้นเบรกขึ้น: Watch CALL {len(g['wc'])} — ตัวอย่าง: {', '.join(g['wc'][:12]) or '-'}")
    lines.append(f"• Momentum ขาลง: Strong PUT {len(g['sp'])} — ตัวอย่าง: {', '.join(g['sp'][:12]) or '-'}")
    lines.append(f"• ระวังอ่อนแรง: Watch PUT {len(g['wp'])} — ตัวอย่าง: {', '.join(g['wp'][:12]) or '-'}")
    lines.append("")
    lines.append("💡 แนวคิด:")
    lines.append("• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าจ่อจุดหนุน")
    lines.append("• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม")
    lines.append("• Strong PUT ลงต่อหรือรีบาวน์สั้น")
    lines.append("• Watch PUT ระวังหลุดแนวรับ")
    return "\n".join(lines)


# =========================
# Telegram (v20+)
# =========================
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 คำสั่งที่ใช้ได้\n"
        "/movers – ดู Top Movers (ฟรี: ข้อมูลวันก่อนหน้า)\n"
        "/signals – จัดกลุ่ม Strong/Watch (CALL/PUT) จาก movers\n"
        "/outlook – สรุปแนวมาตลาดวันนี้ (อิงเมื่อวาน)\n"
        "/help – เมนูนี้อีกครั้ง\n\n"
        "เกณฑ์เริ่มต้น: pct ≥ 10.0%, ราคา ≥ 0.30, Vol ≥ 0"
    )
    await update.message.reply_text(text)

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...")
    date_ref = _yesterday_us()
    movers = fetch_polygon_grouped(date_ref)
    text = build_movers_text(movers, limit=30, min_price=0.30, min_pct=10.0, min_vol=0)
    await update.message.reply_text(text)

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ กำลังคัดกลุ่มสัญญาณจาก movers (ฟรี: ย้อนหลังวันล่าสุด)...")
    date_ref = _yesterday_us()
    movers = fetch_polygon_grouped(date_ref)
    text = build_signals_text(movers)
    await update.message.reply_text(text)

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_ref = _yesterday_us()
    movers = fetch_polygon_grouped(date_ref)
    g = classify_signals(movers)
    text = (
        f"📟 Outlook (อิง {date_ref})\n"
        f"• Strong CALL: {len(g['sc'])} | Watch CALL: {len(g['wc'])}\n"
        f"• Strong PUT : {len(g['sp'])} | Watch PUT : {len(g['wp'])}\n"
        f"→ โมเมนตัมรวม: "
        f"{'ขาขึ้น' if len(g['sc'])+len(g['wc']) > len(g['sp'])+len(g['wp']) else 'ขาลง' if len(g['sp'])+len(g['wp']) > len(g['sc'])+len(g['wc']) else 'กลาง'}\n\n"
        "พอร์ตสั้นวันเดียว: ตามน้ำกลุ่ม Strong, รอจังหวะใน Watch"
    )
    await update.message.reply_text(text)

def build_telegram_app():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set.")
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    app_tg.add_handler(CommandHandler("help",    cmd_help))
    app_tg.add_handler(CommandHandler("start",   cmd_help))
    app_tg.add_handler(CommandHandler("movers",  cmd_movers))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))

    return app_tg

async def run_telegram():
    app_tg = build_telegram_app()
    log.info("Starting telegram long-polling…")
    await app_tg.run_polling(stop_signals=None)  # เราจัดการหยุดเองเมื่อโปรเซสตาย


# =========================
# ENTRYPOINT (Thread approach)
# =========================
if __name__ == "__main__":
    # รัน Telegram bot ใน thread แยก เพื่อหลีกเลี่ยงปัญหา event-loop บนบางสภาพแวดล้อม (เช่น Render)
    def _bot_runner():
        try:
            asyncio.run(run_telegram())
        except Exception as e:
            log.exception("Telegram runner crashed: %s", e)

    if BOT_TOKEN:
        t = threading.Thread(target=_bot_runner, daemon=True)
        t.start()
        log.info("🚀 Flask + Telegram bot started together")
    else:
        log.warning("🚫 BOT not started because BOT_TOKEN is missing")

    # รัน Flask เป็น process หลัก
    app.run(host="0.0.0.0", port=PORT, debug=False)
