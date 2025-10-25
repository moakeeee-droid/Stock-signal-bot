# main.py
# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import signal
from datetime import datetime, timezone

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# Logging
# =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("stock-signal-bot")


# =========================
# Telegram command handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\n\n"
        "คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals - สัญญาณจำลอง (วันนี้)\n"
        "/outlook - มุมมองตลาดวันนี้\n"
        "/picks - หุ้นน่าสนใจ (ตัวอย่าง)\n"
        "/movers - หุ้นเด่นเคลื่อนไหวมาก (ตัวอย่าง)\n"
    )
    await update.message.reply_text(text)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong 🏓")


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ตัวอย่างสรุปสัญญาณจำลอง
    # (ในโปรดักชันคุณสามารถดึงข้อมูลจริงมาใส่แทนได้)
    strong_call = 15
    strong_put = 22
    msg = f"🔮 Signals (จำลอง)\nStrong CALL: {strong_call} | Strong PUT: {strong_put}"
    await update.message.reply_text(msg)


async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ตัวอย่างข้อความ outlook
    await update.message.reply_text("📈 Outlook วันนี้: โมเมนตัมกลางๆ")


async def _format_pick_detail(symbol: str) -> str:
    # ที่นี่เป็น placeholder; ถ้าต้องการเชื่อม Yahoo/Finhub จริงค่อยเติมภายหลัง
    # คืนข้อความว่าข้อมูลยังไม่พร้อม
    return f"⚠️ {symbol}: ข้อมูลไม่พร้อม"


async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ตัวอย่างรายชื่อ
    picks = ["BYND", "KUKE", "GSIT"]
    await update.message.reply_text("⏳ กำลังดึงข้อมูลหุ้น...")

    details = [await _format_pick_detail(s) for s in picks]
    header = "🧾 Picks (รายละเอียด)\n"
    await update.message.reply_text(header + "\n".join(details))


async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ตัวอย่าง (placeholder)
    await update.message.reply_text("📊 Movers: (ตัวอย่าง) AAPL, NVDA, TSLA")


# =========================
# aiohttp healthcheck server
# =========================
async def handle_root(request: web.Request) -> web.Response:
    now = datetime.now(timezone.utc).isoformat()
    return web.Response(text=f"✅ Bot is running — {now}\n", content_type="text/plain")


def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_root)
    return app


# =========================
# Telegram bot lifecycle (Polling แบบ async)
# =========================
async def bot_run(application: Application, stop_event: asyncio.Event) -> None:
    """
    รันบอทด้วยลำดับ initialize -> start -> updater.start_polling()
    โดยไม่เรียก run_polling() เพื่อหลีกเลี่ยงการปิด/เปิด event loop ซ้ำ
    """
    log.info("Starting Telegram bot (polling mode)")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    # รอจนกว่าได้รับสัญญาณหยุด
    await stop_event.wait()

    log.info("Stopping Telegram bot...")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    log.info("Telegram bot stopped")


# =========================
# Main entry
# =========================
async def main_async():
    bot_token = os.environ.get("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("Environment variable BOT_TOKEN is required")

    port = int(os.environ.get("PORT", "10000"))
    log.info("Config | PORT=%s", port)

    # สร้าง Telegram Application และผูก handler
    application = Application.builder().token(bot_token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(CommandHandler("signals", cmd_signals))
    application.add_handler(CommandHandler("outlook", cmd_outlook))
    application.add_handler(CommandHandler("picks", cmd_picks))
    application.add_handler(CommandHandler("movers", cmd_movers))

    # stop_event ใช้ประสานหยุดทั้ง web และ bot อย่างเรียบร้อย
    stop_event = asyncio.Event()

    # สร้าง aiohttp app สำหรับ healthcheck และเปิดพอร์ตให้ Render เห็น
    web_app = build_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("HTTP server started on 0.0.0.0:%d", port)

    # จัดการสัญญาณ OS (SIGINT/SIGTERM) ให้หยุดงานสวย ๆ
    loop = asyncio.get_running_loop()

    def _graceful_stop():
        if not stop_event.is_set():
            log.info("Shutdown signal received")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _graceful_stop)
        except NotImplementedError:
            # บางแพลตฟอร์ม (เช่น Windows) อาจไม่รองรับ
            pass

    # รัน Telegram bot เป็นงานคู่ขนาน
    bot_task = asyncio.create_task(bot_run(application, stop_event), name="tg-bot")

    # รอจนกว่าจะถูกสั่งหยุด
    try:
        await bot_task
    finally:
        # ปิดเว็บเซิร์ฟเวอร์
        await runner.cleanup()
        log.info("HTTP server stopped")

    log.info("Application terminated")


def main():
    # ใช้ asyncio.run เป็น entrypoint เดียว คุม event loop ทั้งหมด
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
