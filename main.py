# -*- coding: utf-8 -*-
"""
Stock Signal Bot (Render + Telegram long-polling)
- รองรับ python-telegram-bot หลายเวอร์ชัน (>=20.x ถึง 21.x) ด้วยการตรวจ args ของ run_polling
- ใช้ Flask เป็น health page
- คำสั่งพร้อมใช้: /help /ping /movers /signals /outlook /picks
  (ตอนนี้ดึงข้อมูลแบบ placeholder เพื่อลงได้ชัวร์ คุณค่อยต่อ API ภายหลังได้)
"""

import os
import logging
import asyncio
import inspect
import threading
from datetime import datetime

from flask import Flask, request

# --- python-telegram-bot (PTB) imports (v20+ / v21+) ---
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =========================
# Config & Logging
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()  # ไม่ได้ใช้ก็ได้
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("ไม่ได้ตั้งค่า BOT_TOKEN ใน Render Environment")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# =========================
# Flask app (health page)
# =========================
flask_app = Flask(__name__)

@flask_app.get("/")
def index():
    return "OK - stock-signal-bot", 200

@flask_app.get("/healthz")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}, 200

# =========================
# Telegram handlers
# =========================
HELP_TEXT = (
    "คำสั่งที่ใช้ได้:\n"
    "• /ping – ทดสอบบอท\n"
    "• /help – เมนูนี้\n"
    "• /movers – (ตัวอย่าง) Top movers วันนี้\n"
    "• /signals – (ตัวอย่าง) จัดกลุ่ม Strong/Watch CALL/PUT\n"
    "• /outlook – (ตัวอย่าง) สรุปโมเมนตัมวันนี้\n"
    "• /picks – (ตัวอย่าง) รายชื่อที่น่าจับตา\n"
    "\n*หมายเหตุ:* ตอนนี้ข้อมูลเป็นตัวอย่างไว้ให้บอททำงานได้ก่อน "
    "ถ้าจะต่อ API จริงให้เพิ่มในฟังก์ชัน fetch_* ภายในไฟล์นี้ครับ"
)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

# --------- ตัวอย่างดึงข้อมูล (ยังเป็น placeholder) ----------
def fetch_movers() -> str:
    # TODO: ต่อ API จริงภายหลัง (เช่น Polygon/อื่น ๆ)
    return (
        "📈 Top Movers (ตัวอย่าง)\n"
        "• ABC @1.23 — pct 18.7%, Vol 12.3M (near H)\n"
        "• XYZ @0.75 — pct -24.1%, Vol 4.5M (near L)\n"
    )

def fetch_signals() -> str:
    return (
        "🔮 Signals (ตัวอย่าง)\n"
        "• Strong CALL: 15 | Watch CALL: 28\n"
        "• Strong PUT : 22 | Watch PUT : 30\n"
        "→ โมเมนตัมรวม: กลาง\n"
    )

def fetch_outlook() -> str:
    return (
        "🧭 Outlook (ตัวอย่าง)\n"
        "พอร์ตสั้นวันเดียว: ตามน้ำกลุ่ม Strong, รอจังหวะใน Watch\n"
    )

def fetch_picks() -> str:
    return (
        "📝 Picks (ตัวอย่าง)\n"
        "• BYND, KUKE, CIIT, GSIT, NVTX\n"
        "• ถ้าหลุดแนวรับให้คัทสั้น ๆ\n"
    )

async def cmd_movers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fetch_movers())

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fetch_signals())

async def cmd_outlook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fetch_outlook())

async def cmd_picks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fetch_picks())

# =========================
# Build Telegram app
# =========================
def build_tg_app():
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(CommandHandler("start", cmd_help))
    app_tg.add_handler(CommandHandler("ping", cmd_ping))

    app_tg.add_handler(CommandHandler("movers", cmd_movers))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))

    return app_tg

# =========================
# Long-polling runner (compat)
# =========================
async def run_telegram():
    app_tg = build_tg_app()
    log.info("Starting telegram long-polling…")

    # ตรวจว่าฟังก์ชัน run_polling รองรับ args อะไรบ้าง (กันพังกข้ามเวอร์ชัน)
    sig = inspect.signature(app_tg.run_polling).parameters
    kwargs = {}
    if "close_loop" in sig:
        kwargs["close_loop"] = False
    if "handle_signals" in sig:
        kwargs["handle_signals"] = False

    await app_tg.run_polling(**kwargs)

def start_telegram_thread():
    # รัน asyncio แยกเธรด
    def _target():
        try:
            asyncio.run(run_telegram())
        except Exception as e:
            log.exception("Telegram thread crashed: %s", e)

    th = threading.Thread(target=_target, name="run_telegram_longpoll", daemon=True)
    th.start()
    return th

# =========================
# Entrypoint
# =========================
if __name__ == "__main__":
    log.info("BOT_TOKEN loaded (length=%d)", len(BOT_TOKEN))
    start_telegram_thread()

    log.info("Starting Flask on 0.0.0.0:%s", PORT)
    # ใช้ Flask dev server สำหรับ Render ฟรีไทร์ได้
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)
