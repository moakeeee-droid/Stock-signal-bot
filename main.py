# -*- coding: utf-8 -*-
"""
Stock-signal bot (Render)
- แยก Telegram bot ไปทำงานใน 'thread' ของมันเอง
- Flask รันใน main thread (Render health check / หน้าสถานะ)
- รองรับคำสั่งพื้นฐาน: /start /help /ping /movers /signals /outlook /picks
- ใช้ PTB v20.3 + httpx 0.24.1 (ดู requirements.txt)
"""

import os
import logging
from threading import Thread
import asyncio
from datetime import datetime, timezone

from flask import Flask, jsonify

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ---------------------- ตั้งค่า Log ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("stock-signal-bot")

# ---------------------- ENV ----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "")  # ไม่ได้ใช้ก็ได้ แต่เก็บไว้เผื่ออนาคต

if not BOT_TOKEN:
    log.error("❌ ENV BOT_TOKEN ว่างอยู่! ใส่ค่าให้ถูกใน Render > Environment")
    # อย่าทำ sys.exit() ปล่อยให้ Flask ขึ้นเพื่อดูข้อความเตือนในหน้าเว็บ
else:
    log.info("✅ BOT_TOKEN loaded (length=%s)", len(BOT_TOKEN))

# ---------------------- Flask app ----------------------
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify(
        ok=True,
        service="stock-signal-bot",
        now_utc=datetime.now(timezone.utc).isoformat(),
        telegram="long-polling thread alive?"  # แค่บอกสถานะคร่าวๆ
    )

# ---------------------- Telegram Handlers ----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "สวัสดีครับ! พิมพ์ /help เพื่อดูคำสั่งที่ใช้ได้ 😊"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "คำสั่งที่ใช้ได้:\n"
        "• /ping – ทดสอบบอท\n"
        "• /movers – Top movers (เดโม)\n"
        "• /signals – จัดกลุ่มสัญญาณ (เดโม)\n"
        "• /outlook – มุมมองวันนี้ (เดโม)\n"
        "• /picks – หุ้นน่าสนใจ (เดโม)\n"
    )
    await update.message.reply_text(text)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 movers (เดโม) – ระบบตอบกลับได้ปกติแล้ว")

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧭 signals (เดโม) – ระบบตอบกลับได้ปกติแล้ว")

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔭 outlook (เดโม) – ระบบตอบกลับได้ปกติแล้ว")

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎯 picks (เดโม) – ระบบตอบกลับได้ปกติแล้ว")

# ---------------------- ตัวบอท (async) ----------------------
async def build_app():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN ไม่พร้อม จึงยังไม่ start telegram app")
        return None

    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    # register handlers
    app_tg.add_handler(CommandHandler("start", cmd_start))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(CommandHandler("ping", cmd_ping))
    app_tg.add_handler(CommandHandler("movers", cmd_movers))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))

    # ลอง getMe เพื่อ log ให้เห็นว่า token ใช้ได้จริง
    me = await app_tg.bot.get_me()
    log.info("🤖 Authorized as @%s (id=%s)", me.username, me.id)

    return app_tg

async def run_telegram():
    """
    รันบอทแบบ long-polling ใน event loop ของเธรดนี้
    ใช้ asyncio.run(run_telegram()) เมื่อสตาร์ทเธรด
    """
    app_tg = await build_app()
    if app_tg is None:
        return

    log.info("🚀 Starting telegram long-polling…")
    # ไม่ต้องกำหนด stop_signals เพื่อหลีกเลี่ยงการปิด loop ผิดที่
    await app_tg.run_polling(close_loop=False)

def start_telegram_thread():
    """
    สร้างเธรดใหม่แล้วรัน asyncio.run(run_telegram())
    แยกจาก main thread ที่ใช้รัน Flask
    """
    def _target():
        try:
            asyncio.run(run_telegram())
        except Exception as e:
            log.exception("❌ Telegram thread crashed: %s", e)

    th = Thread(target=_target, name="telegram-thread", daemon=True)
    th.start()
    log.info("🧵 started telegram-thread")

# ---------------------- Entry point ----------------------
if __name__ == "__main__":
    # สตาร์ท Telegram ในเธรดแยก
    start_telegram_thread()

    # แล้วค่อยเปิด Flask (เธรดหลัก)
    log.info("🌐 Starting Flask on 0.0.0.0:%s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
