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
PICKS = [s.strip().upper() for s in os.getenv("PICKS", "BYND, KUKE, GSIT").split(",")]

# ─────────────────────────────
# Flask (background thread)
# ─────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "Stock Signal Bot is running",
        "time_utc": datetime.utcnow().isoformat()
    })

def start_flask():
    log.info(f"🌐 Flask running on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ─────────────────────────────
# Yahoo Finance helpers
# ─────────────────────────────
def _yahoo_get_json(url: str, payload: dict | None = None):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }
    r = requests.get(url, params=payload or {}, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()

def get_stock_detail(symbol: str) -> str:
    try:
        url = "https://query1.finance.yahoo.com/v7/finance/quote"
        data = _yahoo_get_json(url, {"symbols": symbol})
        q = data["quoteResponse"]["result"][0]

        price = q.get("regularMarketPrice", 0)
        chg = q.get("regularMarketChange", 0)
        pct = q.get("regularMarketChangePercent", 0)
        hi = q.get("regularMarketDayHigh", 0)
        lo = q.get("regularMarketDayLow", 0)
        wkhi = q.get("fiftyTwoWeekHigh", 0)
        wklo = q.get("fiftyTwoWeekLow", 0)
        vol = q.get("regularMarketVolume", 0)
        avgvol = q.get("averageDailyVolume3Month", 0)
        mcap = q.get("marketCap", 0)
        exch = q.get("fullExchangeName") or q.get("exchange")

        link = f"https://finance.yahoo.com/quote/{symbol}"
        arrow = "🟢" if chg > 0 else ("🔴" if chg < 0 else "⚪")

        def _fmt_vol(v):
            if v >= 1e9: return f"{v/1e9:.2f}B"
            if v >= 1e6: return f"{v/1e6:.2f}M"
            if v >= 1e3: return f"{v/1e3:.2f}K"
            return str(int(v))

        return (
            f"{arrow} *{symbol}* ({exch})  {price:.2f}  ({chg:+.2f} | {pct:+.2f}%)\n"
            f"Vol: {_fmt_vol(vol)} / Avg3m: {_fmt_vol(avgvol)} | MktCap: {mcap/1e9:.2f}B\n"
            f"Day {lo:.2f} → {hi:.2f} | 52W {wklo:.2f} → {wkhi:.2f}\n"
            f"[ดูกราฟ]({link})"
        )
    except Exception as e:
        log.warning(f"{symbol} fetch error: {e}")
        return f"⚠️ {symbol}: ข้อมูลไม่พร้อม"

def get_predefined_list(kind: str, count: int = 10) -> list[str]:
    """
    kind: 'gainers' | 'losers' | 'actives'
    """
    scr_id_map = {
        "gainers": "day_gainers",
        "losers": "day_losers",
        "actives": "most_actives"
    }
    scr = scr_id_map.get(kind, "day_gainers")
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    data = _yahoo_get_json(url, {"scrIds": scr, "count": count})
    items = data["finance"]["result"][0]["quotes"]
    symbols = [it["symbol"] for it in items if "symbol" in it]
    return symbols[:count]

# ─────────────────────────────
# Telegram commands
# ─────────────────────────────
async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_outlook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 Outlook วันนี้: โมเมนตัมกลางๆ")

async def cmd_picks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ กำลังดึงข้อมูลหุ้น...")
    lines = [get_stock_detail(sym) for sym in PICKS]
    msg = "🧾 *Picks (รายละเอียด)*\n\n" + "\n\n".join(lines)
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    ใช้ได้หลายแบบ:
      /signals
      /signals losers
      /signals actives 15
    """
    args = [a.lower() for a in (ctx.args or [])]
    kind = "gainers"
    limit = 10

    if args:
        # arg1 = kind หรือจำนวน
        if args[0].isdigit():
            limit = max(3, min(30, int(args[0])))
        elif args[0] in {"gainers", "losers", "actives"}:
            kind = args[0]
        # arg2 (ถ้ามี) = จำนวน
        if len(args) > 1 and args[1].isdigit():
            limit = max(3, min(30, int(args[1])))

    title_map = {
        "gainers": "📈 Gainers (บวกแรง)",
        "losers": "📉 Losers (ลบแรง)",
        "actives": "🔥 Most Active (Vol สูง)"
    }
    await update.message.reply_text(f"⏳ กำลังค้นหา {title_map[kind]} ...")

    try:
        symbols = get_predefined_list(kind, count=limit)
        if not symbols:
            await update.message.reply_text("ไม่พบสัญญาณนะครับ ลองใหม่อีกครั้ง")
            return

        details = [get_stock_detail(s) for s in symbols]
        text = f"{title_map[kind]} — *{len(symbols)} ตัว*\n\n" + "\n\n".join(details)
        await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

    except Exception as e:
        log.exception("signals failed")
        await update.message.reply_text(f"เกิดข้อผิดพลาด: {e}")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📊 คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals [gainers|losers|actives] [จำนวน] - สแกนสัญญาณ\n"
        "/outlook - มุมมองตลาด\n"
        "/picks - หุ้นน่าสนใจ\n"
        "/help - เมนูนี้"
    )
    await update.message.reply_text(text)

# ─────────────────────────────
# Main entry
# ─────────────────────────────
if __name__ == "__main__":
    # Start Flask first (background thread) → ให้ Render จับ port ได้
    threading.Thread(target=start_flask, daemon=True).start()

    # Telegram polling ใน main thread (แก้ปัญหา set_wakeup_fd)
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("ping", cmd_ping))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))
    app_tg.add_handler(CommandHandler("help", cmd_help))

    log.info("✅ Starting Telegram polling (main thread)...")
    app_tg.run_polling()
