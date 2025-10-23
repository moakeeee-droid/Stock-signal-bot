import os
import asyncio
import threading
import logging
from datetime import datetime
import requests
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ─────────────────────────────
# Logging
# ─────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("stock-signal-bot")

# ─────────────────────────────
# Environment
# ─────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))
TZ = os.getenv("TZ", "Asia/Bangkok")
PICKS = [s.strip().upper() for s in os.getenv("PICKS", "BYND, KUKE, GSIT").split(",")]

# ─────────────────────────────
# Flask (Render health check)
# ─────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "Stock Signal Bot is live",
        "time_utc": datetime.utcnow().isoformat()
    })

def start_flask():
    log.info(f"🌐 Flask running on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ─────────────────────────────
# Yahoo Finance fetcher
# ─────────────────────────────
def get_stock_detail(symbol: str) -> str:
    try:
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}"
        r = requests.get(url, timeout=8)
        q = r.json()["quoteResponse"]["result"][0]
        price = q.get("regularMarketPrice", 0)
        chg = q.get("regularMarketChange", 0)
        pct = q.get("regularMarketChangePercent", 0)
        hi = q.get("regularMarketDayHigh", 0)
        lo = q.get("regularMarketDayLow", 0)
        wkhi = q.get("fiftyTwoWeekHigh", 0)
        wklo = q.get("fiftyTwoWeekLow", 0)
        mcap = q.get("marketCap", 0)
        link = f"https://finance.yahoo.com/quote/{symbol}"
        arrow = "🟢" if chg > 0 else ("🔴" if chg < 0 else "⚪")

        return (
            f"{arrow} *{symbol}*  ราคา {price:.2f} ({chg:+.2f} | {pct:+.2f}%)\n"
            f"Day {lo:.2f} → {hi:.2f} | 52W {wklo:.2f} → {wkhi:.2f}\n"
            f"MktCap: {mcap/1e9:.2f}B\n"
            f"[ดูกราฟ]({link})"
        )
    except Exception as e:
        log.warning(f"{symbol} fetch error: {e}")
        return f"⚠️ {symbol}: ข้อมูลไม่พร้อม"

# ─────────────────────────────
# Telegram bot
# ─────────────────────────────
async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🪄 Signals (จำลอง)\nStrong CALL: 15 | Strong PUT: 22")

async def cmd_outlook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 Outlook วันนี้: โมเมนตัมกลางๆ")

async def cmd_picks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ กำลังดึงข้อมูลหุ้น...")
    lines = [get_stock_detail(sym) for sym in PICKS]
    msg = "🧾 *Picks (รายละเอียด)*\n\n" + "\n\n".join(lines)
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

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

def start_telegram():
    try:
        async def runner():
            app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
            app_tg.add_handler(CommandHandler("ping", cmd_ping))
            app_tg.add_handler(CommandHandler("signals", cmd_signals))
            app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
            app_tg.add_handler(CommandHandler("picks", cmd_picks))
            app_tg.add_handler(CommandHandler("help", cmd_help))
            log.info("✅ Starting Telegram polling ...")
            await app_tg.run_polling(close_loop=False)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner())

    except Exception as e:
        log.error(f"❌ Telegram bot failed: {e}")

# ─────────────────────────────
# Run both
# ─────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=start_telegram, daemon=True).start()
    start_flask()
