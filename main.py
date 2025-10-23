import os
import asyncio
import logging
import threading
import requests
from flask import Flask
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update

# -------------------- Logging --------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# -------------------- Flask Healthcheck --------------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Stock Signal Bot is running!", 200

# -------------------- Telegram Bot --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip()
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PORT = int(os.environ.get("PORT", "10000"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ยินดีต้อนรับสู่ Stock Signal Bot 📈")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📊 คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals - ตัวอย่างสัญญาณ\n"
        "/outlook - มุมมองตลาด\n"
        "/picks - หุ้นแนะนำ\n"
    )

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔮 ตัวอย่างสัญญาณจำลอง: Strong CALL 12 | Strong PUT 8")

async def outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 Outlook วันนี้: โมเมนตัมกลาง ๆ")

async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 Picks: AAPL, NVDA, TSLA")

def build_telegram_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("signals", signals))
    app.add_handler(CommandHandler("outlook", outlook))
    app.add_handler(CommandHandler("picks", picks))
    return app

async def run_webhook():
    app = build_telegram_app()
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    log.info(f"Setting webhook to: {webhook_url}")
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
        close_loop=False
    )

def run_flask():
    log.info(f"Starting Flask on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)

def main():
    # Start Flask (to make Render detect the port)
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    # Run Telegram bot webhook
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_webhook())

if __name__ == "__main__":
    main()
