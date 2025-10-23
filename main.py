import os
import asyncio
import logging
import threading
from flask import Flask
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# ---------------- Flask ----------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Stock Signal Bot is running!", 200

# ---------------- Telegram ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip()
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PORT = int(os.environ.get("PORT", "10000"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ยินดีต้อนรับสู่ Stock Signal Bot 📊")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 ตัวอย่างสัญญาณ: Strong CALL 12 | Strong PUT 8")

async def outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌤️ Outlook วันนี้: โมเมนตัมกลาง ๆ")

async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💡 Picks: AAPL, NVDA, TSLA")

def build_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("signals", signals))
    app.add_handler(CommandHandler("outlook", outlook))
    app.add_handler(CommandHandler("picks", picks))
    return app

def run_flask():
    log.info(f"🌐 Flask running on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)

async def run_webhook(bot_app):
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    log.info(f"🚀 Setting Telegram webhook to: {webhook_url}")
    await bot_app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
        stop_signals=None
    )

def main():
    # Start Flask in a background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Run Telegram bot webhook (new event loop)
    bot_app = build_bot()
    asyncio.run(run_webhook(bot_app))

if __name__ == "__main__":
    main()
