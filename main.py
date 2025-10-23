import os
import math
import time
import json
import logging
from typing import Any, Dict, List, Tuple
from datetime import datetime, timezone
import asyncio
import aiohttp

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# Config & Logging
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN env var")
if not PUBLIC_URL:
    raise RuntimeError("Please set PUBLIC_URL env var (e.g., https://<your>.onrender.com)")

TIMEOUT = aiohttp.ClientTimeout(total=15)
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=6mo&interval=1d"


# =========================
# Helpers: math / TA
# =========================
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
        gains.append(max(0.0, ch))
        losses.append(max(0.0, -ch))
    # average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = [50.0] * (period)  # pad
    r = 100.0 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))
    rsis.append(r)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        r = 100.0 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))
        rsis.append(r)
    return rsis if len(rsis) == len(series) else ([50.0]*(len(series)-len(rsis)) + rsis)

def macd(series: List[float]) -> Dict[str, List[float]]:
    if len(series) < 35:
        # ให้ความยาวพอประมาณ
        series = ([series[0]] * (35 - len(series))) + series
    ema12 = ema(series, 12)
    ema26 = ema(series, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    signal = ema(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line, signal)]
    return {"macd": macd_line, "signal": signal, "hist": hist}

def fmt_num(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    if abs(x) >= 100:
        return f"{x:,.0f}"
    if abs(x) >= 10:
        return f"{x:,.2f}"
    return f"{x:,.3f}"


# =========================
# Yahoo fetchers
# =========================
async def fetch_json(session: aiohttp.ClientSession, url: str) -> Dict[str, Any]:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.json()

async def get_prices(symbol: str) -> Tuple[List[float], List[int]]:
    """Return (closes, timestamps) daily for ~6 months."""
    url = YAHOO_CHART_URL.format(symbol=symbol.upper())
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        data = await fetch_json(s, url)
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo chart no data for {symbol}")
    r0 = result[0]
    ts = r0.get("timestamp", [])
    closes = r0.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    # บางวันอาจ None ให้ถูไปด้วยการถอดออก
    clean_closes, clean_ts = [], []
    for t, c in zip(ts, closes):
        if c is not None:
            clean_closes.append(float(c))
            clean_ts.append(int(t))
    if not clean_closes:
        raise RuntimeError(f"No close values for {symbol}")
    return clean_closes, clean_ts

async def get_quote(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    url = f"{YAHOO_QUOTE_URL}?symbols={','.join([s.upper() for s in symbols])}"
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        data = await fetch_json(s, url)
    out: Dict[str, Dict[str, Any]] = {}
    for item in data.get("quoteResponse", {}).get("result", []):
        sym = item.get("symbol", "").upper()
        out[sym] = {
            "name": item.get("shortName") or item.get("longName") or sym,
            "price": item.get("regularMarketPrice"),
            "change": item.get("regularMarketChange"),
            "changePercent": item.get("regularMarketChangePercent"),
            "currency": item.get("currency") or "",
            "marketState": item.get("marketState") or "",
        }
    return out


# =========================
# Conservative Scoring
# =========================
def judge_signal(closes: List[float]) -> Dict[str, Any]:
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    mac = macd(closes)
    rsi14 = rsi(closes, 14)

    p = closes[-1]
    e20 = ema20[-1]
    e50 = ema50[-1]
    m = mac["macd"][-1]
    s = mac["signal"][-1]
    h = mac["hist"][-1]
    h_prev = mac["hist"][-2] if len(mac["hist"]) >= 2 else h
    h_slope = h - h_prev
    r = rsi14[-1]

    trend = "ขาขึ้น" if (p > e50 and e20 > e50) else ("ขาลง" if (p < e50 and e20 < e50) else "แกว่งตัว")
    mac_sig = "Bullish" if (m > s and h > 0) else ("Bearish" if (m < s and h < 0) else "Neutral")
    rsi_sig = "Overbought" if r >= 70 else ("Oversold" if r <= 30 else "Neutral")

    score = 0
    # Trend/Structure
    if p > e50: score += 1
    if e20 > e50: score += 1
    if p > e20: score += 1
    # Momentum
    if m > s: score += 1
    if h > 0 and h_slope > 0: score += 1
    # RSI sweet spot
    if 45 <= r <= 60: score += 1
    # Penalties
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
        "price": p,
        "ema20": e20,
        "ema50": e50,
        "macd": m,
        "signal": s,
        "hist": h,
        "hist_slope": h_slope,
        "rsi14": r,
        "trend": trend,
        "macd_state": mac_sig,
        "rsi_state": rsi_sig,
        "bias": bias,
        "score": score,
    }

def build_signal_block(ticker: str, sig: Dict[str, Any], meta: Dict[str, Any] | None) -> str:
    title = f"*{ticker}* — {meta.get('name','') if meta else ''}".strip()
    price_line = f"• ราคา: *{fmt_num(sig['price'])}*"
    if meta and meta.get("changePercent") is not None:
        ch = meta["change"]
        chp = meta["changePercent"]
        price_line += f" ({fmt_num(ch)} / {fmt_num(chp)}%)"

    lines = [
        f"🔎 {title}",
        f"{price_line}",
        f"• EMA20: {fmt_num(sig['ema20'])} | EMA50: {fmt_num(sig['ema50'])}",
        f"• MACD: {fmt_num(sig['macd'])} | Signal: {fmt_num(sig['signal'])} | Hist: {fmt_num(sig['hist'])} (Δ {fmt_num(sig.get('hist_slope', 0))}) → *{sig['macd_state']}*",
        f"• RSI(14): *{fmt_num(sig['rsi14'])}* → *{sig['rsi_state']}*",
        f"• แนวโน้ม: *{sig['trend']}*",
        f"• สรุป (Conservative): *{sig['bias']}*  _(score={sig.get('score',0)})_",
    ]
    return "\n".join(lines)


# =========================
# Bot Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "สวัสดีครับ 👋\n"
        "พิมพ์ /signals <สัญลักษณ์...> เพื่อดูสรุปเชิงเทคนิคแบบเข้ม\n\n"
        "ตัวอย่าง: `/signals TSLA AAPL NVDA`\n"
        "คำสั่งอื่น: /ping /help",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals <tickers> - สรุปสัญญาณแบบ conservative (รองรับหลายตัว)\n"
        "/help - แสดงเมนูนี้",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("พิมพ์แบบนี้นะครับ: `/signals TSLA AAPL`", parse_mode=ParseMode.MARKDOWN)
        return

    tickers = [a.upper() for a in context.args[:10]]  # จำกัด 10 ตัวต่อครั้ง
    quote_map = {}
    try:
        quote_map = await get_quote(tickers)
    except Exception as e:
        log.warning(f"quote fetch error: {e}")

    parts: List[str] = []
    for tk in tickers:
        try:
            closes, _ = await get_prices(tk)
            sig = judge_signal(closes)
            meta = quote_map.get(tk)
            parts.append(build_signal_block(tk, sig, meta))
        except Exception as e:
            log.error(f"signals error {tk}: {e}")
            parts.append(f"❌ *{tk}* : ไม่พบข้อมูลหรือเกิดข้อผิดพลาด")

    msg = "\n\n".join(parts)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


# =========================
# Health route (via PTB web_app)
# =========================
async def health_handler(request):
    from aiohttp import web
    # ใช้สำหรับ Render health/เปิดหน้า root
    body = {
        "ok": True,
        "time_utc": datetime.utcnow().isoformat(),
        "service": "stock-signal-bot",
        "mode": "webhook",
        "webhook_path": WEBHOOK_PATH,
    }
    return web.json_response(body)


# =========================
# Entry
# =========================
async def main():
    log.info("Starting Telegram bot (Webhook mode)")
    app = Application.builder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("signals", cmd_signals))

    # health route on PTB's aiohttp app
    app.web_app.add_routes([])  # ensure web_app exists
    app.web_app.router.add_get("/", health_handler)

    # run webhook server (bind Render port)
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    log.info(f"| time_utc: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} | PORT={PORT} | PUBLIC_URL={PUBLIC_URL} | WEBHOOK_PATH={WEBHOOK_PATH}")
    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url,
        stop_signals=None,        # ไม่ใช้สัญญาณ OS (บน Render)
        drop_pending_updates=True,
        close_loop=False,         # ป้องกัน error ปิด event loop
    )


if __name__ == "__main__":
    asyncio.run(main())
