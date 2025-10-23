# main.py
# Stock-signal-bot: Flask + python-telegram-bot (v21) พร้อมรายละเอียดหุ้นและสรุปรายวัน
import os
import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from flask import Flask, jsonify

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# -----------------------------
# ตั้งค่า log
# -----------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# -----------------------------
# Flask (healthcheck / root)
# -----------------------------
flask_app = Flask(__name__)

@flask_app.get("/")
def root():
    return jsonify(ok=True, service="stock-signal-bot"), 200

# -----------------------------
# โมเดลข้อมูล/ตัวช่วย format
# -----------------------------
@dataclass
class StockRow:
    symbol: str
    price: float
    pct: float              # เปลี่ยนแปลงเป็น %
    near: str               # "near H" หรือ "near L" หรือ "-"
    vol: str                # ปริมาณ

def fmt_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"

def badge(dot: str) -> str:
    return "🟢" if dot == "up" else "🔴"

def fmt_row(row: StockRow, trend: str) -> str:
    # ตัวอย่าง: 🟢 • ABC @10.25 — pct +2.4%, close near H, Vol 1.2M
    return f"{badge(trend)} • {row.symbol} @{row.price:.2f} — pct {fmt_pct(row.pct)}, close {row.near}, Vol {row.vol}"

def chunk_lines(rows: list[StockRow], trend: str, limit: int = 10) -> str:
    return "\n".join(fmt_row(r, trend) for r in rows[:limit])

# -----------------------------
# ส่วนจำลองข้อมูล (แทน API จริง)
# - ถ้ามี API จริง ค่อยเปลี่ยนเฉพาะฟังก์ชันพวกนี้ได้เลย
# -----------------------------
TZ = ZoneInfo("Asia/Bangkok")

def fake_now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

def fake_rows_up() -> list[StockRow]:
    # ตัวอย่างข้อมูลฝั่งขึ้น (CALL)
    return [
        StockRow("BYND", 6.45, 12.8, "near H", "1.23M"),
        StockRow("CHACR", 0.52, 20.9, "near H", "382.7K"),
        StockRow("GSIT", 3.85, 9.6, "near H", "2.04M"),
        StockRow("KUKE", 1.24, 7.5, "near H", "418.2K"),
        StockRow("CCXCW", 0.42, 15.3, "near H", "91.1K"),
    ]

def fake_rows_down() -> list[StockRow]:
    # ตัวอย่างข้อมูลฝั่งลง (PUT)
    return [
        StockRow("MBVIW", 0.70, -38.6, "near L", "337.1K"),
        StockRow("GSRFR", 2.47, -37.9, "near L", "53.1K"),
        StockRow("HONDW", 7.50, -31.8, "near L", "534.3K"),
        StockRow("QSIAW", 0.51, -31.1, "near L", "141.1K"),
        StockRow("NKLR", 8.85, -28.2, "near L", "5.88M"),
    ]

def get_fake_signals():
    # โครงสร้างผลลัพธ์สำหรับ /signals
    return {
        "strong_call": fake_rows_up()[:3],
        "watch_call": fake_rows_up()[3:],
        "strong_put": fake_rows_down()[:3],
        "watch_put": fake_rows_down()[3:],
    }

def get_fake_outlook():
    # โครงสร้างผลลัพธ์สำหรับ /outlook
    data = get_fake_signals()
    return {
        "date": fake_now_str(),
        "strong_call": len(data["strong_call"]),
        "watch_call": len(data["watch_call"]),
        "strong_put": len(data["strong_put"]),
        "watch_put": len(data["watch_put"]),
        "comment": "โมเมนตัมกลาง",
    }

def get_fake_picks() -> list[StockRow]:
    return [
        StockRow("BYND", 6.45, 12.8, "near H", "1.23M"),
        StockRow("KUKE", 1.24, 7.5, "near H", "418.2K"),
        StockRow("GSIT", 3.85, 9.6, "near H", "2.04M"),
    ]

# -----------------------------
# Telegram bot handlers
# -----------------------------
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

HELP_TEXT = (
    "📊 คำสั่งที่ใช้ได้:\n"
    "/ping - ทดสอบบอท\n"
    "/signals - สัญญาณแบบละเอียด (CALL/PUT)\n"
    "/outlook - มุมมองตลาดวันนี้\n"
    "/picks - หุ้นน่าสนใจ\n"
    "/help - แสดงเมนูนี้"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sig = get_fake_signals()

    text = []
    text.append("🔮 *Signals (จำลอง)*")
    text.append(f"Strong CALL: {len(sig['strong_call'])} | Strong PUT: {len(sig['strong_put'])}")
    text.append("")  # เว้นบรรทัด

    if sig["strong_call"]:
        text.append("🟢 *Strong CALL*")
        text.append(chunk_lines(sig["strong_call"], "up"))
        text.append("")

    if sig["watch_call"]:
        text.append("🟢 *Watch CALL*")
        text.append(chunk_lines(sig["watch_call"], "up"))
        text.append("")

    if sig["strong_put"]:
        text.append("🔴 *Strong PUT*")
        text.append(chunk_lines(sig["strong_put"], "down"))
        text.append("")

    if sig["watch_put"]:
        text.append("🔴 *Watch PUT*")
        text.append(chunk_lines(sig["watch_put"], "down"))

    await update.message.reply_markdown_v2("\n".join(text))

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    o = get_fake_outlook()
    text = (
        f"📈 *Outlook* (ถึง {o['date']})\n"
        f"*Strong CALL*: {o['strong_call']} | *Watch CALL*: {o['watch_call']}\n"
        f"*Strong PUT*: {o['strong_put']} | *Watch PUT*: {o['watch_put']}\n"
        f"→ *โมเมนตัมรวม*: {o['comment']}\n\n"
        "พอร์ตสั้นวันเดียว: ตามน้ำกลุ่ม Strong, รอจังหวะใน Watch"
    )
    await update.message.reply_markdown_v2(text)

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_fake_picks()
    body = "\n".join(f"• {r.symbol} @{r.price:.2f} — pct {fmt_pct(r.pct)}, {r.near}, Vol {r.vol}" for r in rows)
    await update.message.reply_text(f"📝 Picks:\n{body}")

# -----------------------------
# งานสรุปรายวันอัตโนมัติ (09:00 Asia/Bangkok)
# -----------------------------
TARGET_CHAT_ID = os.getenv("CHAT_ID")  # ถ้ามีให้ส่งเข้า group/channel นั้น

async def daily_summary(context: ContextTypes.DEFAULT_TYPE):
    if not TARGET_CHAT_ID:
        return
    o = get_fake_outlook()
    text = (
        f"🗓️ สรุปประจำวัน {o['date']}\n"
        f"Strong CALL: {o['strong_call']} | Watch CALL: {o['watch_call']}\n"
        f"Strong PUT: {o['strong_put']} | Watch PUT: {o['watch_put']}\n"
        f"→ โมเมนตัมรวม: {o['comment']}"
    )
    try:
        await context.bot.send_message(chat_id=int(TARGET_CHAT_ID), text=text)
    except Exception as e:
        log.exception("ส่งสรุปรายวันล้มเหลว: %s", e)

# -----------------------------
# Build Telegram app
# -----------------------------
def build_application() -> "Application":
    token = os.environ["BOT_TOKEN"]
    app_tg = ApplicationBuilder().token(token).build()

    app_tg.add_handler(CommandHandler("ping", cmd_ping))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))

    # ตั้งสรุปรายวันเวลา 09:00 น. (Asia/Bangkok)
    bangkok = ZoneInfo("Asia/Bangkok")
    app_tg.job_queue.run_daily(
        daily_summary,
        time=time(hour=9, minute=0, tzinfo=bangkok),
        name="daily_summary",
    )
    return app_tg

# -----------------------------
# Entrypoint
# - รัน Flask เป็น background thread
# - รัน Telegram polling บน main-thread (เพื่อเลี่ยงปัญหา signal)
# -----------------------------
def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, debug=False)

async def run_telegram():
    app_tg = build_application()
    log.info("BOT_TOKEN loaded (length=%d)", len(os.getenv("BOT_TOKEN", "")))
    # เริ่ม polling (บน main-thread)
    await app_tg.run_polling(close_loop=False)

if __name__ == "__main__":
    # 1) สตาร์ท Flask เป็น background thread
    threading.Thread(target=run_flask, daemon=True).start()
    # 2) สตาร์ท Telegram บน main-thread
    asyncio.run(run_telegram())
