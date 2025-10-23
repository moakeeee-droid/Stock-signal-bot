import os
import threading
import logging
import datetime as dt
import asyncio
from typing import Optional

from flask import Flask, jsonify

# ── PTB v21.x
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes
)

# ─────────────────────────────
# Logging
# ─────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# ─────────────────────────────
# Config / ENV
# ─────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()
PORT = int(os.getenv("PORT", "10000"))
TZ = os.getenv("TZ", "Asia/Bangkok")

if not BOT_TOKEN:
    log.warning("BOT_TOKEN is empty!")

# ─────────────────────────────
# Flask (healthcheck & keep-alive)
# ─────────────────────────────
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify(ok=True, time=dt.datetime.utcnow().isoformat() + "Z")

# ─────────────────────────────
# Telegram Commands
# ─────────────────────────────
async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # ตัวอย่างข้อมูลจำลอง
    await update.message.reply_text("🪄 Signals (จำลอง)\nStrong CALL: 15 | Strong PUT: 22")

async def cmd_outlook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 Outlook วันนี้: โมเมนตัมกลางๆ")

async def cmd_picks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧾 Picks: BYND, KUKE, GSIT")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📊 คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals - สัญญาณจำลอง\n"
        "/outlook - มุมมองตลาด\n"
        "/picks - หุ้นน่าสนใจ\n"
        "/help - เมนูนี้"
    )
    await update.message.reply_text(text)

# ─────────────────────────────
# Build PTB Application (กันพังถ้า job_queue ใช้ไม่ได้)
# ─────────────────────────────
def build_application() -> Optional[Application]:
    if not BOT_TOKEN:
        log.error("Missing BOT_TOKEN → skip Telegram init")
        return None

    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    # register command handlers
    app_tg.add_handler(CommandHandler("ping", cmd_ping))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))
    app_tg.add_handler(CommandHandler("help", cmd_help))

    # schedule งานรายวันแบบไม่ให้ล้มถ้าไม่มี jobqueue
    try:
        if app_tg.job_queue:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(TZ)
            async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
                # ตัวอย่างส่งข้อความไปยังตัวเอง ถ้ามี chat_id ที่จะส่งประจำให้ใส่ตรงนี้
                pass

            # ตัวอย่าง: 09:00 ทุกวัน (ปรับได้)
            app_tg.job_queue.run_daily(
                daily_summary,
                time=dt.time(hour=9, minute=0, tzinfo=tz),
                name="daily_summary",
            )
            log.info("JobQueue scheduled daily_summary at 09:00 %s", TZ)
        else:
            log.warning("PTB job_queue not available (but app will still run)")
    except Exception as e:
        log.warning("Schedule jobs skipped: %s", e)

    return app_tg

# ─────────────────────────────
# Run Telegram in background thread
# ─────────────────────────────
def run_telegram_bg():
    try:
        app_tg = build_application()
        if app_tg is None:
            log.warning("Telegram not started.")
            return

        # สำคัญ: อย่าให้ไปยุ่ง signal ใน thread (บางโฮสต์จะล้ม)
        log.info("Starting Telegram polling in background …")
        # close_loop=False เพื่อไม่ให้ PTB ปิด event loop ของ main thread
        asyncio.run(app_tg.run_polling(close_loop=False))
    except Exception as e:
        log.exception("Telegram thread crashed: %s", e)

# ─────────────────────────────
# Entrypoint
# ─────────────────────────────
if __name__ == "__main__":
    # start telegram in a safe background thread
    tg_thread = threading.Thread(target=run_telegram_bg, name="telegram", daemon=True)
    tg_thread.start()

    # run Flask and bind to Render's PORT (must be 0.0.0.0)
    log.info("Starting Flask on 0.0.0.0:%s", PORT)
    # use_reloader=False ป้องกันการรันซ้ำสองโปรเซส
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
