# -*- coding: utf-8 -*-
"""
Stock Signal Bot (Render + Telegram long-polling)
- ใช้ได้กับ PTB 21.x
- แยก Telegram ออกจาก Flask อย่างปลอดภัย
"""

import os
import logging
import threading
import inspect
from datetime import datetime
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

def run_telegram():
    import asyncio
    app_tg = build_telegram()
    log.info("🚀 Starting telegram long-polling…")

    sig = inspect.signature(app_tg.run_polling).parameters
    kwargs = {}
    if "close_loop" in sig:
        kwargs["close_loop"] = True
    if "handle_signals" in sig:
        kwargs["handle_signals"] = False

    asyncio.run(app_tg.run_polling(**kwargs))

# =============== ENTRYPOINT ===============
if __name__ == "__main__":
    log.info("Starting Flask + Telegram bot")
    threading.Thread(target=run_telegram, daemon=True).start()
    flask_app.run(host="0.0.0.0", port=PORT)
