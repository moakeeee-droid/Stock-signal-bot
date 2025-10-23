import os
import asyncio
import threading
import logging
from datetime import datetime, time
from typing import List, Dict, Any

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
PORT = int(os.getenv("PORT", 10000))
TZ = os.getenv("TZ", "Asia/Bangkok")
PICKS_ENV = os.getenv("PICKS", "BYND, KUKE, GSIT")

# ─────────────────────────────
# Flask App (health check & keep-alive)
# ─────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"status": "ok", "time_utc": datetime.utcnow().isoformat()})

# ─────────────────────────────
# Utilities for formatting
# ─────────────────────────────
def _fmt_num(n: Any) -> str:
    try:
        x = float(n)
    except Exception:
        return "-"
    absx = abs(x)
    if absx >= 1_000_000_000:
        return f"{x/1_000_000_000:.2f}B"
    if absx >= 1_000_000:
        return f"{x/1_000_000:.2f}M"
    if absx >= 1_000:
        return f"{x/1_000:.2f}K"
    return f"{x:.2f}"

def _fmt_pct(p: Any) -> str:
    try:
        return f"{float(p):+.2f}%"
    except Exception:
        return "+0.00%"

def _clean_symbols(raw: str) -> List[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]

# ─────────────────────────────
# Yahoo Finance quote fetcher (no API key)
# ─────────────────────────────
YF_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

def fetch_quotes_yahoo(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Return dict keyed by symbol -> quote dict
    Fields we use: regularMarketPrice, regularMarketChange, regularMarketChangePercent,
                   regularMarketDayHigh/Low, fiftyTwoWeekHigh/Low,
                   regularMarketVolume, marketCap, currency, shortName
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not symbols:
        return out
    try:
        resp = requests.get(YF_URL, params={"symbols": ",".join(symbols)}, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("quoteResponse", {}).get("result", [])
        for q in data:
            sym = q.get("symbol")
            if not sym:
                continue
            out[sym.upper()] = {
                "name": q.get("shortName") or q.get("longName") or sym,
                "price": q.get("regularMarketPrice"),
                "chg": q.get("regularMarketChange"),
                "chg_pct": q.get("regularMarketChangePercent"),
                "high": q.get("regularMarketDayHigh"),
                "low": q.get("regularMarketDayLow"),
                "vol": q.get("regularMarketVolume"),
                "mktcap": q.get("marketCap"),
                "wk52h": q.get("fiftyTwoWeekHigh"),
                "wk52l": q.get("fiftyTwoWeekLow"),
                "currency": q.get("currency") or "",
            }
    except Exception as e:
        log.exception(f"fetch_quotes_yahoo error: {e}")
    return out

def build_quote_lines(symbols: List[str]) -> str:
    quotes = fetch_quotes_yahoo(symbols)
    lines = []
    for sym in symbols:
        q = quotes.get(sym)
        if not q:
            lines.append(f"• {sym} — ไม่พบข้อมูล")
            continue
        price = q["price"]
        chg = q["chg"]
        chg_pct = q["chg_pct"]
        vol = q["vol"]
        mktcap = q["mktcap"]
        hi = q["high"]; lo = q["low"]
        wk52h = q["wk52h"]; wk52l = q["wk52l"]
        ccy = q["currency"]

        arrow = "🟢" if (chg or 0) > 0 else ("🔴" if (chg or 0) < 0 else "⚪")
        name = q["name"]
        y_link = f"https://finance.yahoo.com/quote/{sym}"
        lines.append(
            f"{arrow} *{sym}* — {name}\n"
            f"  ราคา: *{price:.2f}* {ccy}  ({_fmt_num(chg)} | {_fmt_pct(chg_pct)})\n"
            f"  Day: {_fmt_num(lo)} → {_fmt_num(hi)}   |  52W: {_fmt_num(wk52l)} → {_fmt_num(wk52h)}\n"
            f"  Vol: {_fmt_num(vol)}   MktCap: {_fmt_num(mktcap)}\n"
            f"  แผนภูมิ: {y_link}"
        )
    return "\n\n".join(lines)

# ─────────────────────────────
# Telegram Commands
# ─────────────────────────────
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🪄 Signals (จำลอง)\nStrong CALL: 15 | Strong PUT: 22")

async def outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 Outlook วันนี้: โมเมนตัมกลางๆ")

async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbols = _clean_symbols(PICKS_ENV)
    # ดึงข้อมูลทันทีแบบ synchronous แล้วส่งข้อความครั้งเดียว (PTB v21 handler เป็น async)
    text = "🧾 *Picks (รายละเอียด)*\n\n" + build_quote_lines(symbols)
    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📊 คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals - สัญญาณจำลอง\n"
        "/outlook - มุมมองตลาด\n"
        "/picks - หุ้นน่าสนใจ (แสดงรายละเอียด)\n"
        "/help - เมนูนี้"
    )
    await update.message.reply_text(text)

# ─────────────────────────────
# Telegram runner
# ─────────────────────────────
def start_telegram():
    try:
        app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

        app_tg.add_handler(CommandHandler("ping", ping))
        app_tg.add_handler(CommandHandler("signals", signals))
        app_tg.add_handler(CommandHandler("outlook", outlook))
        app_tg.add_handler(CommandHandler("picks", picks))
        app_tg.add_handler(CommandHandler("help", help_cmd))

        # JobQueue (optional) — ถ้าไม่ติดตั้งก็ไปต่อได้
        try:
            if app_tg.job_queue:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(TZ)

                async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
                    log.info("Daily summary executed")

                app_tg.job_queue.run_daily(
                    daily_summary,
                    time=time(hour=9, minute=0, tzinfo=tz),
                    name="daily_summary"
                )
                log.info("✅ JobQueue started successfully.")
        except Exception as e:
            log.warning(f"⚠️ JobQueue unavailable: {e}")

        log.info("✅ Starting Telegram polling ...")
        asyncio.run(app_tg.run_polling(close_loop=False))
    except Exception as e:
        log.error(f"❌ Telegram bot failed: {e}")

# ─────────────────────────────
# Entrypoint: run Telegram in a thread + Flask on $PORT
# ─────────────────────────────
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set.")

    threading.Thread(target=start_telegram, daemon=True).start()
    log.info(f"🌐 Flask running on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
