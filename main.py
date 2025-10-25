# -*- coding: utf-8 -*-
"""
Stock-signal-bot (Render · Web Service)
Mode: Polling + aiohttp health server (same asyncio loop)

- ใช้ PTB v21.x (Application.run_polling)
- ไม่มีการปิด loop เอง, ไม่ใช้ updater.wait()/idle()
- เปิด HTTP health server บนพอร์ต PORT สำหรับ Render

Env ที่ต้องมี:
  - BOT_TOKEN         : Telegram bot token
  - PORT              : พอร์ตที่ Render โยนมา (เช่น 10000)
OPTIONAL:
  - PUBLIC_URL        : สำหรับ log แสดงผล (ไม่ใช้ webhook)
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

import anyio
import requests
from aiohttp import web

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s | %(levelname)8s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
log = logging.getLogger("stock-signal-bot")

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is required.")

PORT = int(os.environ.get("PORT", "10000"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip()

# -----------------------------------------------------------------------------
# Toy data / helpers (ตัวอย่าง mock ให้บอทตอบได้ทันที)
# คุณจะต่อข้อมูลจริงจาก Yahoo/Finnhub ภายหลังได้เลย — ส่วน handler ไม่ต้องแก้
# -----------------------------------------------------------------------------
def mock_today_outlook() -> str:
    return "โมเมนตัมกลางๆ"

def mock_signals_summary() -> Tuple[int, int]:
    # (strong_call, strong_put)
    return (15, 22)

def mock_top_picks() -> List[str]:
    return ["BYND", "KUKE", "GSIT"]

def mock_pick_detail(symbol: str) -> str:
    # แก้ทีหลังให้ดึงจาก Yahoo/Finnhub ได้
    # ตอบโครงสร้างให้เหมือนเวอร์ชันก่อนๆ
    return (
        f"• {symbol}\n"
        f"  ├─ แนวโน้ม: Neutral/Up\n"
        f"  ├─ %เปลี่ยนวันนี้: +1.2%\n"
        f"  ├─ ปริมาณ: สูงกว่าค่าเฉลี่ย\n"
        f"  └─ ความเห็น: รอจังหวะย่อสะสม"
    )

def mock_movers() -> List[str]:
    return ["NVDA +4.3%", "AMD +3.1%", "TSLA -2.2%"]

# -----------------------------------------------------------------------------
# Telegram command handlers
# -----------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "👋 ยินดีต้อนรับสู่ Stock Signal Bot\n"
        "คำสั่งที่ใช้ได้:\n"
        "/ping – ทดสอบบอท\n"
        "/signals – สรุปสัญญาณ\n"
        "/outlook – มุมมองตลาดวันนี้\n"
        "/picks – หุ้นแนะนำ (ตัวอย่าง)\n"
        "/movers – หุ้นเคลื่อนไหวเด่น\n"
    )
    await update.message.reply_text(txt)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    strong_call, strong_put = mock_signals_summary()
    txt = f"🔮 Signals (จำลอง)\nStrong CALL: {strong_call} | Strong PUT: {strong_put}"
    await update.message.reply_text(txt)

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    outlook = mock_today_outlook()
    await update.message.reply_text(f"📈 Outlook วันนี้: {outlook}")

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    picks = mock_top_picks()
    await update.message.reply_text("⌛ กำลังดึงข้อมูลหุ้น…")
    # รายละเอียดแบบก่อนหน้า
    lines = ["🧾 Picks (รายละเอียด)"]
    for s in picks:
        detail = mock_pick_detail(s)
        lines.append(detail)
    await update.message.reply_text("\n\n".join(lines))

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movers = mock_movers()
    txt = "🚀 Movers วันนี้\n" + "\n".join(f"• {m}" for m in movers)
    await update.message.reply_text(txt)

# -----------------------------------------------------------------------------
# Build Application
# -----------------------------------------------------------------------------
def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ping",    cmd_ping))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("picks",   cmd_picks))
    app.add_handler(CommandHandler("movers",  cmd_movers))

    return app

# -----------------------------------------------------------------------------
# HTTP health server (aiohttp)
# -----------------------------------------------------------------------------
async def health(request: web.Request):
    now = datetime.now(timezone.utc).isoformat()
    return web.Response(text=f"✅ Bot is running — {now}", content_type="text/plain")

async def build_http_server() -> web.AppRunner:
    app = web.Application()
    app.add_routes([web.get("/", health), web.get("/healthz", health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP health server started on port %s", PORT)
    return runner

# -----------------------------------------------------------------------------
# Main orchestration
# -----------------------------------------------------------------------------
async def main():
    log.info("Starting Stock-signal-bot in POLLING mode")
    if PUBLIC_URL:
        log.info("PUBLIC_URL=%s (info only; webhook not used)", PUBLIC_URL)

    # 1) start HTTP health server (non-blocking)
    runner = await build_http_server()

    # 2) start Telegram bot (blocking until cancelled)
    application = build_application()
    # IMPORTANT: stop_signals=() เพื่อไม่ให้ PTB จัดการสัญญาณเองบน Render
    await application.run_polling(
        poll_interval=1.5,
        allowed_updates=Update.ALL_TYPES,
        stop_signals=(),
        close_loop=False,  # อย่าปิด loop (เราอาจมี task อื่น)
    )

    # ถ้าหลุดจาก run_polling (เช่น ถูกสั่งปิด) -> ปิด HTTP server ให้เรียบร้อย
    log.info("Application stopped; shutting down HTTP server")
    await runner.cleanup()

# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted, bye.")
