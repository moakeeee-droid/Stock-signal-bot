# -*- coding: utf-8 -*-
"""
Stock Signal Bot on Render (Flask + Telegram long-polling)
- python-telegram-bot 21.x
- Flask รันบน main thread
- Telegram รันใน background thread พร้อม event loop ส่วนตัว (Python 3.13 friendly)
"""

import os
import logging
import threading
import inspect
from datetime import datetime

from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
if not BOT_TOKEN:
    raise RuntimeError("❌ Missing BOT_TOKEN env var")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# ================== FLASK ===================
app = Flask(__name__)

@app.get("/")
def root():
    return "✅ Stock Signal Bot is running", 200

@app.get("/healthz")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()}), 200

# ============ TELEGRAM COMMANDS =============
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

# ============ TELEGRAM APPLICATION ===========
def build_tg_app():
    tg = ApplicationBuilder().token(BOT_TOKEN).build()
    tg.add_handler(CommandHandler("start", cmd_help))
    tg.add_handler(CommandHandler("help", cmd_help))
    tg.add_handler(CommandHandler("ping", cmd_ping))
    tg.add_handler(CommandHandler("signals", cmd_signals))
    tg.add_handler(CommandHandler("outlook", cmd_outlook))
    tg.add_handler(CommandHandler("picks", cmd_picks))
    return tg

def run_telegram():
    """Run PTB in a dedicated thread with its own event loop (Python 3.13-safe)."""
    import asyncio

    # สร้าง loop ใหม่และตั้งให้ thread นี้ใช้
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    tg_app = build_tg_app()
    log.info("🚀 Starting telegram long-polling…")

    # เผื่อความต่างของเวอร์ชัน PTB: handle_signals/close_loop
    sig = inspect.signature(tg_app.run_polling).parameters
    kwargs = {}
    if "close_loop" in sig:
        kwargs["close_loop"] = False  # เราคุม loop เอง อย่าปิดเอง
    if "handle_signals" in sig:
        kwargs["handle_signals"] = False  # ห้ามแตะ signal ใน thread

    try:
        loop.run_until_complete(tg_app.run_polling(**kwargs))
    finally:
        try:
            loop.run_until_complete(tg_app.shutdown())
        except Exception:
            pass
        loop.close()
        log.info("🛑 Telegram polling stopped")

# ================= ENTRYPOINT ================
if __name__ == "__main__":
    log.info("Starting Flask + Telegram bot")
    threading.Thread(target=run_telegram, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
