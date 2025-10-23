import os
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============== CONFIG ==============
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://stock-signal-bot-1.onrender.com")
WEBHOOK_PATH = "/webhook"

# ============== LOGGING ==============
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============== FLASK APP ==============
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Stock Signal Bot is running!"

@app.route(WEBHOOK_PATH, methods=["POST"])
async def webhook():
    update = Update.de_json(request.get_json(force=True), bot_app.bot)
    await bot_app.process_update(update)
    return "OK", 200

# ============== TELEGRAM BOT ==============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("สวัสดีครับ! บอทพร้อมทำงานแล้ว 📈")

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("กำลังประมวลผลสัญญาณหุ้นล่าสุดครับ 🔍")

def main():
    global bot_app

    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("signals", signals))

    # ตั้งค่า webhook URL
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    logger.info(f"Setting webhook to: {webhook_url}")

    # ใช้ event loop เดียว (ไม่ใช้ asyncio.run ซ้ำ)
    loop = bot_app.run_polling(close_loop=False)  # เผื่อ fallback polling ถ้า render ปิด port

# ============== RUN FLASK SERVER ==============
if __name__ == "__main__":
    logger.info("Starting Flask & Telegram bot (Webhook mode)")
    app.run(host="0.0.0.0", port=PORT)
    main()
