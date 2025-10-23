import os
import math
import logging
import asyncio
import aiohttp
from aiohttp import web
from typing import List, Dict, Any
from datetime import datetime
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ===============================
# CONFIG
# ===============================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN")
if not PUBLIC_URL:
    raise RuntimeError("Missing PUBLIC_URL")

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=6mo&interval=1d"
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"


# ===============================
# MATH HELPERS
# ===============================
def ema(series: List[float], period: int) -> List[float]:
    if len(series) < 1:
        return []
    k = 2 / (period + 1)
    out = [series[0]]
    for x in series[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out

def rsi(series: List[float], period: int = 14) -> List[float]:
    if len(series) < period + 1:
        return [50.0] * len(series)
    gains, losses = [], []
    for i in range(1, len(series)):
        ch = series[i] - series[i - 1]
        gains.append(max(0, ch))
        losses.append(max(0, -ch))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = [50.0] * (period)
    r = 100.0 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))
    rsis.append(r)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        r = 100.0 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))
        rsis.append(r)
    return rsis

def macd(series: List[float]) -> Dict[str, List[float]]:
    ema12 = ema(series, 12)
    ema26 = ema(series, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal = ema(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line, signal)]
    return {"macd": macd_line, "signal": signal, "hist": hist}

def fmt(x: float) -> str:
    if x is None:
        return "-"
    if abs(x) >= 100:
        return f"{x:,.0f}"
    elif abs(x) >= 10:
        return f"{x:,.2f}"
    return f"{x:,.3f}"


# ===============================
# DATA FETCHERS (Yahoo)
# ===============================
async def fetch_json(session: aiohttp.ClientSession, url: str) -> Dict[str, Any]:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.json()

async def get_prices(symbol: str) -> List[float]:
    url = YAHOO_CHART_URL.format(symbol=symbol.upper())
    async with aiohttp.ClientSession() as s:
        data = await fetch_json(s, url)
    result = data.get("chart", {}).get("result", [{}])[0]
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    return [c for c in closes if c is not None]

async def get_quote(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    url = f"{YAHOO_QUOTE_URL}?symbols={','.join(symbols)}"
    async with aiohttp.ClientSession() as s:
        data = await fetch_json(s, url)
    out = {}
    for item in data.get("quoteResponse", {}).get("result", []):
        sym = item.get("symbol", "").upper()
        out[sym] = {
            "price": item.get("regularMarketPrice"),
            "change": item.get("regularMarketChange"),
            "changePercent": item.get("regularMarketChangePercent"),
            "name": item.get("shortName") or item.get("longName") or sym,
        }
    return out


# ===============================
# CONSERVATIVE STRATEGY
# ===============================
def judge_signal(closes: List[float]) -> Dict[str, Any]:
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    mac = macd(closes)
    rsi14 = rsi(closes, 14)
    p = closes[-1]
    e20, e50 = ema20[-1], ema50[-1]
    m, s = mac["macd"][-1], mac["signal"][-1]
    h, h_prev = mac["hist"][-1], mac["hist"][-2]
    h_slope = h - h_prev
    r = rsi14[-1]

    score = 0
    if p > e50: score += 1
    if e20 > e50: score += 1
    if p > e20: score += 1
    if m > s: score += 1
    if h > 0 and h_slope > 0: score += 1
    if 45 <= r <= 60: score += 1
    if r >= 70: score -= 1
    if r <= 30: score -= 1

    if score >= 5:
        bias = "📈📈 Strong Buy"
    elif score >= 3:
        bias = "📈 Buy"
    elif score >= 1:
        bias = "➖ Neutral"
    elif score <= -2:
        bias = "📉📉 Strong Sell"
    else:
        bias = "📉 Sell"

    return {
        "price": p, "ema20": e20, "ema50": e50,
        "macd": m, "signal": s, "hist": h,
        "hist_slope": h_slope, "rsi14": r, "bias": bias, "score": score
    }


# ===============================
# TELEGRAM HANDLERS
# ===============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "สวัสดีครับ 👋\nพิมพ์ /signals <หุ้น> เช่น `/signals TSLA AAPL` เพื่อดูสัญญาณแบบ Conservative",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("พิมพ์แบบนี้นะครับ: `/signals TSLA AAPL`", parse_mode=ParseMode.MARKDOWN)
        return

    tickers = [t.upper() for t in context.args]
    quotes = await get_quote(tickers)
    result_texts = []

    for sym in tickers:
        try:
            closes = await get_prices(sym)
            sig = judge_signal(closes)
            q = quotes.get(sym)
            txt = (
                f"🔎 *{sym}* ({q.get('name','')})\n"
                f"• ราคา: *{fmt(sig['price'])}* ({fmt(q.get('change',0))} / {fmt(q.get('changePercent',0))}%)\n"
                f"• EMA20={fmt(sig['ema20'])} | EMA50={fmt(sig['ema50'])}\n"
                f"• MACD={fmt(sig['macd'])} | Hist={fmt(sig['hist'])} (Δ {fmt(sig['hist_slope'])})\n"
                f"• RSI14={fmt(sig['rsi14'])}\n"
                f"• สรุป: *{sig['bias']}*  (score={sig['score']})"
            )
            result_texts.append(txt)
        except Exception as e:
            log.error(f"{sym}: {e}")
            result_texts.append(f"❌ {sym}: ไม่สามารถดึงข้อมูลได้")

    await update.message.reply_text("\n\n".join(result_texts), parse_mode=ParseMode.MARKDOWN)


# ===============================
# HEALTHCHECK SERVER
# ===============================
async def health(request):
    return web.json_response({
        "ok": True,
        "service": "stock-signal-bot",
        "time": datetime.utcnow().isoformat()
    })


# ===============================
# MAIN (safe loop version)
# ===============================
async def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("signals", cmd_signals))

    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"

    # Telegram bot task
    bot_task = application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

    # Healthcheck task
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    web_task = site.start()

    log.info(f"✅ Service live at {PUBLIC_URL} on port {PORT}")
    await asyncio.gather(bot_task, web_task)


if __name__ == "__main__":
    asyncio.run(main())
