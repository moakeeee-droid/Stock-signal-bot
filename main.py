# -*- coding: utf-8 -*-
"""
Stock Signal Bot on Render (Flask + Telegram)
- Telegram (python-telegram-bot 21.x) รันบน main thread
- Flask รันบน background thread
"""

import os
import logging
import threading
from datetime import datetime

from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ========= ENV / CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("❌ Missing BOT_TOKEN environment variable")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# =========== Flask ============
app = Flask(__name__)

@app.get("/")
def root():
    return "✅ Stock Signal Bot is running", 200

@app.get("/healthz")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()}), 200

def run_flask():
    log.info("🌐 Starting Flask on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT)

# ======== Telegram handlers ========
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

def build_tg_app():
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", cmd_help))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(CommandHandler("ping", cmd_ping))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))
    return app_tg

# ========== Entrypoint ==========
if __name__ == "__main__":
    # 1) เปิด Flask ใน background thread
    threading.Thread(target=run_flask, daemon=True).start()

    # 2) รัน Telegram บน main thread (ให้ PTB จัดการ signal handlers ได้ถูกต้อง)
    import asyncio
    app_tg = build_tg_app()
    log.info("🤖 Starting Telegram long-polling on main thread…")
    # ไม่ต้องส่ง handle_signals/close_loop — ค่าเริ่มต้นทำงานได้ใน main thread
    asyncio.run(app_tg.run_polling())
