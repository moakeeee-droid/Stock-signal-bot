# -*- coding: utf-8 -*-
"""
Stock Signal Bot (Render + Telegram long-polling)
- สำหรับ PTB 21.x (ไม่ใช้ thread แยกแล้ว)
- รัน Flask และ Telegram ใน asyncio เดียวกัน
"""

import os
import logging
import asyncio
from datetime import datetime
import inspect
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =============== CONFIG ===============
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
if not BOT_TOKEN:
    raise RuntimeError("❌ Missing BOT_TOKEN environment variable")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# =============== FLASK ===============
flask_app = Flask(__name__)

@flask_app.get("/")
def home():
    return "✅ Stock Signal Bot is running", 200

@flask_app.get("/healthz")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()}), 200

# =============== TELEGRAM COMMANDS ===============
HELP_TEXT = (
    "📊 คำสั่งที่ใช้ได้:\n"
    "/ping - ทดสอบบอท\n"
    "/signals - สัญญาณจำลอง\n"
    "/outlook - มุมมองตลาด\n"
    "/picks - หุ้นน่าสนใจ\n"
    "/help - แสดงเมนูนี้"
)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔮 Signals (จำลอง)\nStrong CALL: 15 | Strong PUT: 22")

async def cmd_outlook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 Outlook วันนี้: โมเมนตัมกลาง")

async def cmd_picks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Picks: BYND, KUKE, GSIT")

# =============== TELEGRAM APP ===============
def build_telegram():
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(CommandHandler("start", cmd_help))
    app_tg.add_handler(CommandHandler("ping", cmd_ping))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))
    return app_tg

async def run_telegram():
    app_tg = build_telegram()
    log.info("🚀 Starting telegram long-polling…")

    sig = inspect.signature(app_tg.run_polling).parameters
    kwargs = {}
    if "close_loop" in sig:
        kwargs["close_loop"] = False
    if "handle_signals" in sig:
        kwargs["handle_signals"] = False

    await app_tg.run_polling(**kwargs)

# =============== RUN BOTH IN ONE LOOP ===============
async def main():
    loop = asyncio.get_running_loop()

    # รัน Flask server ใน background task
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    config = Config()
    config.bind = [f"0.0.0.0:{PORT}"]

    flask_task = asyncio.create_task(serve(flask_app, config))
    tg_task = asyncio.create_task(run_telegram())

    await asyncio.gather(flask_task, tg_task)

if __name__ == "__main__":
    log.info("Starting combined Flask + Telegram app…")
    asyncio.run(main())
