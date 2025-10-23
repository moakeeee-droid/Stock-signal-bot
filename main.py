import os
import logging
from datetime import datetime, timezone
from typing import Final

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# -----------------------
# Logging
# -----------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# -----------------------
# Handlers (ปรับตามของเดิมคุณได้)
# -----------------------
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong 🏓")

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ตัวอย่างข้อความ (จะไปดึงของจริง/เพิ่มดีเทลภายหลังได้)
    text = "🟣 Signals (จำลอง)\nStrong CALL: 15 | Strong PUT: 22"
    await update.message.reply_text(text)

async def outlook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = "📉 Outlook วันนี้: โมเมนตัมกลาง"
    await update.message.reply_text(text)

async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # เดี๋ยวเวอร์ชันต่อไปจะเพิ่มรายละเอียดหุ้นเชิงลึกให้ (PE, Float, ATR ฯลฯ)
    text = "📝 Picks: BYND, KUKE, GSIT"
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📊 คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals - สัญญาณจำลอง\n"
        "/outlook - มุมมองตลาด\n"
        "/picks - หุ้นน่าสนใจ\n"
        "/help - เมนูนี้"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# -----------------------
# App builder
# -----------------------
def build_application() -> Application:
    bot_token: Final[str] = os.environ.get("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("Missing BOT_TOKEN environment variable")

    app = Application.builder().token(bot_token).build()

    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("signals", signals))
    app.add_handler(CommandHandler("outlook", outlook))
    app.add_handler(CommandHandler("picks", picks))
    app.add_handler(CommandHandler("help", help_cmd))

    return app

# -----------------------
# Entrypoint (Webhook mode)
# -----------------------
def main() -> None:
    app = build_application()

    # Render จะส่งค่า PORT ให้เสมอ (ถ้าไม่มีให้ fallback เป็น 10000)
    port = int(os.environ.get("PORT", "10000"))

    # Base URL ของ service (Render กำหนด RENDER_EXTERNAL_URL มาให้)
    public_url = (
        os.environ.get("PUBLIC_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
    )
    if not public_url:
        raise RuntimeError(
            "Missing PUBLIC_URL/RENDER_EXTERNAL_URL. "
            "Set PUBLIC_URL to your service URL (e.g. https://your-app.onrender.com)"
        )

    # ปลอดภัยขึ้นด้วย secret token สำหรับ webhook
    secret_token = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not secret_token:
        # ใส่ค่าแบบง่าย ๆ ได้ แต่แนะนำตั้งใน Render → Environment
        secret_token = "change-me-please"

    # path สำหรับ webhook (อย่าใส่ token ลง path)
    url_path = "webhook"

    webhook_url = f"{public_url.rstrip('/')}/{url_path}"

    log.info("Starting in WEBHOOK mode")
    log.info("time_utc: %s", datetime.now(timezone.utc).isoformat())
    log.info("PORT=%s | PUBLIC_URL=%s | WEBHOOK_PATH=/%s", port, public_url, url_path)

    # ใช้เว็บเซิร์ฟเวอร์ aiohttp ภายใน PTB
    # PTB จะ setWebhook ให้อัตโนมัติ พร้อมตรวจ header 'X-Telegram-Bot-Api-Secret-Token'
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=webhook_url,
        secret_token=secret_token,
        # ถ้าอยากใส่ cert/self-signed ให้เพิ่ม param key/cert ได้ (ไม่จำเป็นบน Render)
    )

if __name__ == "__main__":
    main()
