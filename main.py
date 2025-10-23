import os
import asyncio
import logging
from typing import List, Dict, Any
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# -------------------- Logging --------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# -------------------- Yahoo helpers --------------------
YF_QUOTE = "https://query1.finance.yahoo.com/v7/finance/quote"
YF_TRENDING = "https://query1.finance.yahoo.com/v1/finance/trending/US"

def _human_int(n: float | int | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ["", "K", "M", "B", "T"]:
        if abs(n) < 1000.0:
            return f"{n:,.0f}{unit}"
        n /= 1000.0
    return f"{n:.1f}P"

def fetch_trending_symbols(limit: int = 10) -> List[str]:
    try:
        r = requests.get(YF_TRENDING, timeout=10)
        r.raise_for_status()
        data = r.json()
        symbols = [x["symbol"] for x in data["finance"]["result"][0]["quotes"]]
        return symbols[:limit]
    except Exception as e:
        log.warning(f"fetch_trending_symbols failed: {e}")
        return ["AAPL", "NVDA", "TSLA", "AMZN", "MSFT", "META"]

def fetch_quotes(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    params = {"symbols": ",".join(symbols)}
    r = requests.get(YF_QUOTE, params=params, timeout=12)
    r.raise_for_status()
    res = r.json()["quoteResponse"]["result"]
    out = {}
    for q in res:
        sym = q.get("symbol")
        out[sym] = q
    return out

def format_stock_card(q: Dict[str, Any]) -> str:
    name = q.get("longName") or q.get("shortName") or q.get("symbol")
    sym = q.get("symbol")
    px = q.get("regularMarketPrice")
    chg_pct = q.get("regularMarketChangePercent")
    day_low = q.get("regularMarketDayLow")
    day_high = q.get("regularMarketDayHigh")
    vol = q.get("regularMarketVolume")
    avgvol = q.get("averageDailyVolume3Month")
    mktcap = q.get("marketCap")
    wk52l = q.get("fiftyTwoWeekLow")
    wk52h = q.get("fiftyTwoWeekHigh")

    arrow = "🟢" if (chg_pct or 0) > 0 else "🔴" if (chg_pct or 0) < 0 else "⚪️"

    lines = [
        f"{arrow} *{sym}* — {name}",
        f"ราคา: {px:.2f}  |  เปลี่ยนแปลง: {chg_pct:+.2f}%",
        f"Day Range: {day_low:.2f} - {day_high:.2f}",
        f"52W Range: {wk52l:.2f} - {wk52h:.2f}",
        f"Vol: {_human_int(vol)}  |  AvgVol(3M): {_human_int(avgvol)}",
        f"Market Cap: {_human_int(mktcap)}",
    ]
    return "\n".join(lines)

# -------------------- Bot handlers --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_cmd(update, context)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📊 คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals - สัญญาณจำลอง (จากราคา/ปริมาณ)\n"
        "/outlook - มุมมองตลาดวันนี้ (ง่าย ๆ)\n"
        "/picks - หุ้นน่าสนใจ (พร้อมรายละเอียดแบบการ์ด)\n"
    )
    await update.message.reply_text(txt)

def simple_signal_label(q: Dict[str, Any]) -> str:
    chg = (q.get("regularMarketChangePercent") or 0.0)
    vol = (q.get("regularMarketVolume") or 0)
    avgv = (q.get("averageDailyVolume3Month") or 1)
    vol_ratio = vol / avgv if avgv else 0
    if chg > 3 and vol_ratio > 1.2:
        return "Strong CALL"
    if chg > 0.8:
        return "Watch CALL"
    if chg < -3 and vol_ratio > 1.2:
        return "Strong PUT"
    if chg < -0.8:
        return "Watch PUT"
    return "Neutral"

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = fetch_trending_symbols(limit=20)
    quotes = fetch_quotes(syms)
    counts = {"Strong CALL":0,"Watch CALL":0,"Strong PUT":0,"Watch PUT":0,"Neutral":0}
    for q in quotes.values():
        counts[simple_signal_label(q)] += 1

    msg = (
        "🔮 *Signals (จำลอง)*\n"
        f"Strong CALL: {counts['Strong CALL']} | Strong PUT: {counts['Strong PUT']}\n"
        f"Watch CALL: {counts['Watch CALL']} | Watch PUT: {counts['Watch PUT']}\n"
        "→ ใช้เพื่อสแกนเบื้องต้นเท่านั้น"
    )
    await update.message.reply_markdown(msg)

async def outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = fetch_trending_symbols(limit=30)
    quotes = fetch_quotes(syms).values()
    if not quotes:
        await update.message.reply_text("Outlook วันนี้: ข้อมูลไม่พร้อม ลองใหม่อีกครั้งครับ")
        return
    avg_chg = sum([(q.get("regularMarketChangePercent") or 0.0) for q in quotes]) / len(list(quotes))
    mood = "ขาขึ้นเล็กน้อย" if avg_chg > 0.3 else "ขาลงเล็กน้อย" if avg_chg < -0.3 else "โมเมนตัมกลาง ๆ"
    await update.message.reply_text(f"🧭 Outlook วันนี้: {mood}")

async def picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = fetch_trending_symbols(limit=12)
    qmap = fetch_quotes(syms)
    if not qmap:
        await update.message.reply_text("ยังดึงข้อมูลหุ้นไม่ได้ ลองอีกครั้งครับ")
        return

    top = sorted(qmap.values(), key=lambda x: x.get("regularMarketChangePercent") or 0, reverse=True)[:3]
    txt_blocks = [format_stock_card(q) for q in top]
    msg = "📝 *Picks วันนี้* (จาก Yahoo)\n" + "\n\n".join(txt_blocks)
    await update.message.reply_markdown(msg)

# -------------------- App (Webhook) --------------------
def build_app():
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Environment BOT_TOKEN is required")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("signals", signals))
    app.add_handler(CommandHandler("outlook", outlook))
    app.add_handler(CommandHandler("picks", picks))
    return app

async def run_webhook():
    app = build_app()
    port = int(os.environ.get("PORT", "10000"))
    public_url = os.environ.get("PUBLIC_URL", "").rstrip("/")
    webhook_path = os.environ.get("WEBHOOK_PATH", "/webhook")

    if not public_url.startswith("http"):
        raise RuntimeError("PUBLIC_URL ต้องเป็น URL เต็ม เช่น https://stock-signal-bot-1.onrender.com")

    log.info(f"Starting in WEBHOOK mode | PORT={port} | PUBLIC_URL={public_url}{webhook_path}")

    await app.run_webhook(
        listen="0.0.0.0",
        port=port,
        webhook_url=f"{public_url}{webhook_path}",
    )

def main():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            log.warning("Existing event loop detected — using create_task instead of asyncio.run")
            loop.create_task(run_webhook())
            loop.run_forever()
        else:
            loop.run_until_complete(run_webhook())
    except RuntimeError:
        asyncio.run(run_webhook())

if __name__ == "__main__":
    main()
