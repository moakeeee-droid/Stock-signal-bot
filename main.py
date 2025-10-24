# -*- coding: utf-8 -*-
"""
Stock Signal Bot — Render/Telegram version
Stack:
  - Flask (healthcheck/เปิดพอร์ต)
  - python-telegram-bot v21 (long-polling ใน thread)
  - Yahoo Finance (quote + historical candles) ผ่าน public endpoints (no key)

Environment variables (required):
  BOT_TOKEN   : Telegram Bot Token
  PORT        : Port จาก Render (เช่น 10000)

Optional:
  LOG_LEVEL   : DEBUG/INFO/WARNING/ERROR (default: INFO)

Author: you :)
"""

from __future__ import annotations

import os
import re
import json
import math
import time
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional

import requests
from flask import Flask, Response

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# -----------------------------------------------------------------------------
# CONFIG & LOGGING
# -----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is required")

PORT = int(os.getenv("PORT", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("stock-signal-bot")


# -----------------------------------------------------------------------------
# UTILITIES — SAFE HTTP & TIME
# -----------------------------------------------------------------------------
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/117.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})
TIMEOUT = (8, 15)  # (connect, read) seconds


def utcnow_iso() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()


def http_get(url: str, params: Dict | None = None) -> Dict:
    """GET JSON with retries."""
    for i in range(3):
        try:
            r = SESSION.get(url, params=params or {}, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            log.warning("GET %s -> %s, body=%s", url, r.status_code, r.text[:200])
        except Exception as e:
            log.warning("GET error (%s/%s): %s", i + 1, 3, e)
        time.sleep(0.6 * (i + 1))
    return {}


# -----------------------------------------------------------------------------
# DATA FETCHERS — Yahoo Finance (no key, public endpoints)
# -----------------------------------------------------------------------------
def yf_quote(symbols: List[str]) -> Dict[str, Dict]:
    """
    Fetch real-time quotes for symbols.
    Endpoint: https://query1.finance.yahoo.com/v7/finance/quote?symbols=...
    """
    syms = ",".join(symbols)
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    js = http_get(url, {"symbols": syms})
    out: Dict[str, Dict] = {}
    for item in js.get("quoteResponse", {}).get("result", []):
        sym = item.get("symbol")
        if not sym:
            continue
        out[sym.upper()] = {
            "symbol": sym.upper(),
            "name": item.get("shortName") or item.get("longName") or sym,
            "price": item.get("regularMarketPrice"),
            "change": item.get("regularMarketChange"),
            "changePct": item.get("regularMarketChangePercent"),
            "prevClose": item.get("regularMarketPreviousClose"),
            "marketState": item.get("marketState"),
            "volume": item.get("regularMarketVolume"),
            "avgVolume": item.get("averageDailyVolume3Month")
            or item.get("averageDailyVolume10Day"),
            "currency": item.get("currency"),
        }
    return out


def yf_candles(
    symbol: str,
    period: str = "6mo",
    interval: str = "1d",
) -> Tuple[List[int], List[float], List[float], List[float], List[float], List[int]]:
    """
    Historical candles for indicator calc.
    Endpoint: https://query1.finance.yahoo.com/v8/finance/chart/{symbol}
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": period, "interval": interval, "includePrePost": "false"}
    js = http_get(url, params)
    result = js.get("chart", {}).get("result", [])
    if not result:
        return [], [], [], [], [], []
    r0 = result[0]
    ts = r0.get("timestamp") or []
    ind = r0.get("indicators", {}).get("quote", [{}])[0]
    opens = ind.get("open") or []
    highs = ind.get("high") or []
    lows = ind.get("low") or []
    closes = ind.get("close") or []
    vols = ind.get("volume") or []
    # cleanse none
    def fix(a: List[Optional[float]]) -> List[float]:
        b: List[float] = []
        last = None
        for x in a:
            if x is None:
                b.append(last if last is not None else math.nan)
            else:
                b.append(float(x))
                last = float(x)
        return b

    return ts, fix(opens), fix(highs), fix(lows), fix(closes), [int(v or 0) for v in vols]


# -----------------------------------------------------------------------------
# TECHNICAL INDICATORS (no numpy)
# -----------------------------------------------------------------------------
def ema(values: List[float], period: int) -> List[float]:
    k = 2.0 / (period + 1.0)
    out: List[float] = []
    ema_prev = None
    for v in values:
        if math.isnan(v):
            out.append(math.nan)
            continue
        if ema_prev is None:
            ema_prev = v
        else:
            ema_prev = v * k + ema_prev * (1.0 - k)
        out.append(ema_prev)
    return out


def rsi(values: List[float], period: int = 14) -> List[float]:
    gains: List[float] = [0.0]
    losses: List[float] = [0.0]
    for i in range(1, len(values)):
        if math.isnan(values[i]) or math.isnan(values[i - 1]):
            gains.append(0.0)
            losses.append(0.0)
            continue
        ch = values[i] - values[i - 1]
        gains.append(max(0.0, ch))
        losses.append(max(0.0, -ch))

    def sma(a: List[float], n: int, idx: int) -> float:
        if idx + 1 < n:
            return math.nan
        s = sum(a[idx - n + 1 : idx + 1])
        return s / float(n)

    out: List[float] = []
    avg_g = math.nan
    avg_l = math.nan
    for i in range(len(values)):
        if i == period:
            avg_g = sma(gains, period, i)
            avg_l = sma(losses, period, i)
        elif i > period:
            # smoothed moving average
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period

        if i < period:
            out.append(math.nan)
        else:
            if avg_l == 0:
                out.append(100.0)
            else:
                rs = avg_g / avg_l
                out.append(100.0 - (100.0 / (1.0 + rs)))
    return out


def macd(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line: List[float] = []
    for a, b in zip(ema_fast, ema_slow):
        if math.isnan(a) or math.isnan(b):
            macd_line.append(math.nan)
        else:
            macd_line.append(a - b)
    signal_line = ema(macd_line, signal)
    hist = []
    for m, s in zip(macd_line, signal_line):
        hist.append(m - s if (not math.isnan(m) and not math.isnan(s)) else math.nan)
    return macd_line, signal_line, hist


# -----------------------------------------------------------------------------
# NEW SCORING / SUMMARY LOGIC (Thai-friendly output)
# -----------------------------------------------------------------------------
def classify_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if re.search(r"\^", symbol):
        return "index"
    if re.match(r"^\w{1,5}$", symbol):
        return "equity"
    return "unknown"


def analyze_symbol(symbol: str) -> Dict:
    # fetch realtime & history
    q = yf_quote([symbol]).get(symbol.upper())
    ts, o, h, l, c, v = yf_candles(symbol, period="6mo", interval="1d")
    if not c or all(math.isnan(x) for x in c):
        raise RuntimeError(f"No candles for {symbol}")

    # indicators
    ema20 = ema(c, 20)
    ema50 = ema(c, 50)
    ema200 = ema(c, 200)
    rsi14 = rsi(c, 14)
    macd_l, macd_s, macd_h = macd(c)

    # pick latest non-nan
    def last_valid(a: List[float]) -> float:
        for x in reversed(a):
            if not math.isnan(x):
                return x
        return math.nan

    price = (q or {}).get("price", last_valid(c))
    ema20v, ema50v, ema200v = last_valid(ema20), last_valid(ema50), last_valid(ema200)
    rsi_v = last_valid(rsi14)
    macd_v, sig_v, hist_v = last_valid(macd_l), last_valid(macd_s), last_valid(macd_h)
    vol = (q or {}).get("volume") or (v[-1] if v else None)
    vol_avg = (q or {}).get("avgVolume") or (sum(v[-20:]) // max(1, len(v[-20:]))) if v else None

    # trend by EMA ladder
    trend = "Sideways"
    if price > ema20v > ema50v > ema200v:
        trend = "Uptrend"
    elif price < ema20v < ema50v < ema200v:
        trend = "Downtrend"

    # RSI zones (35/65)
    if math.isnan(rsi_v):
        rsi_zone = "N/A"
    elif rsi_v >= 65:
        rsi_zone = "Overbought↑"
    elif rsi_v <= 35:
        rsi_zone = "Oversold↓"
    else:
        rsi_zone = "Neutral"

    # MACD status
    macd_status = "N/A"
    if not math.isnan(macd_v) and not math.isnan(sig_v):
        if macd_v > sig_v:
            macd_status = "Bullish"
        elif macd_v < sig_v:
            macd_status = "Bearish"
        else:
            macd_status = "Flat"

    # Volume boost
    vol_boost = None
    if isinstance(vol, int) and isinstance(vol_avg, int) and vol_avg > 0:
        vol_ratio = vol / max(1, vol_avg)
        if vol_ratio >= 1.5:
            vol_boost = f"Vol↑ x{vol_ratio:.2f}"
        elif vol_ratio <= 0.6:
            vol_boost = f"Vol↓ x{vol_ratio:.2f}"

    # Score (0–100)
    score = 50
    if trend == "Uptrend":
        score += 20
    elif trend == "Downtrend":
        score -= 20

    if rsi_zone == "Overbought↑":
        score -= 10
    elif rsi_zone == "Oversold↓":
        score += 10

    if macd_status == "Bullish":
        score += 10
    elif macd_status == "Bearish":
        score -= 10

    if vol_boost and vol_boost.startswith("Vol↑"):
        score += 5

    score = max(0, min(100, score))

    # Decision (CALL/PUT/HOLD) — conservative thresholds
    decision = "HOLD"
    reason = []
    if trend == "Uptrend" and macd_status == "Bullish" and rsi_v < 70:
        decision = "CALL"
        reason.append("Uptrend+MACD Bullish, RSI < 70")
    elif trend == "Downtrend" and macd_status == "Bearish" and rsi_v > 30:
        decision = "PUT"
        reason.append("Downtrend+MACD Bearish, RSI > 30")
    else:
        reason.append("เฝ้ารอจังหวะต่อไป")

    # Risk (text)
    risk = "ปานกลาง"
    if rsi_v >= 70 or rsi_v <= 30:
        risk = "สูง"
    if trend == "Sideways":
        risk = "ปานกลาง/สูง (แกว่ง)"

    # Entry/Exit (simple)
    entry_hint = "-"
    exit_hint = "-"
    if decision == "CALL":
        entry_hint = f"รอใกล้ EMA20 ~ {ema20v:.2f}" if not math.isnan(ema20v) else "-"
        exit_hint = "ทยอยขายเมื่อ RSI > 70 / สูญเสีย > -3%"
    elif decision == "PUT":
        entry_hint = f"รอดีดใกล้ EMA20 ~ {ema20v:.2f}" if not math.isnan(ema20v) else "-"
        exit_hint = "ซื้อคืนเมื่อ RSI < 30 / ขาดทุน > -3%"

    return {
        "symbol": symbol.upper(),
        "name": (q or {}).get("name", symbol.upper()),
        "price": price,
        "changePct": (q or {}).get("changePct"),
        "trend": trend,
        "rsi": rsi_v,
        "rsi_zone": rsi_zone,
        "macd": macd_v,
        "macd_sig": sig_v,
        "macd_status": macd_status,
        "vol": vol,
        "vol_avg": vol_avg,
        "vol_note": vol_boost,
        "ema20": ema20v,
        "ema50": ema50v,
        "ema200": ema200v,
        "score": score,
        "decision": decision,
        "risk": risk,
        "entry": entry_hint,
        "exit": exit_hint,
        "time_utc": utcnow_iso(),
    }


def fmt_card(x: Dict) -> str:
    p = x.get("price")
    cp = x.get("changePct")
    cp_txt = f"{cp:+.2f}%" if isinstance(cp, (int, float)) else "—"
    vol_note = f" | {x['vol_note']}" if x.get("vol_note") else ""
    name = x.get("name") or x["symbol"]
    return (
        f"📈 <b>{x['symbol']}</b> — {name}\n"
        f"ราคา: <b>{p:.2f}</b> ({cp_txt})  |  แนวโน้ม: <b>{x['trend']}</b>\n"
        f"RSI(14): <b>{x['rsi']:.1f}</b> ({x['rsi_zone']})  |  MACD: <b>{x['macd_status']}</b>\n"
        f"EMA20/50/200: <code>{x['ema20']:.2f} / {x['ema50']:.2f} / {x['ema200']:.2f}</code>\n"
        f"วอลุ่ม: {x.get('vol')} / ค่าเฉลี่ย: {x.get('vol_avg')}{vol_note}\n"
        f"คะแนนสัญญาณ: <b>{x['score']}</b>/100  → <b>{x['decision']}</b>\n"
        f"แผนเข้า: {x['entry']}  |  แผนออก: {x['exit']}\n"
        f"ความเสี่ยง: <b>{x['risk']}</b>  •  เวลา (UTC): {x['time_utc']}\n"
    )


# -----------------------------------------------------------------------------
# TELEGRAM COMMANDS
# -----------------------------------------------------------------------------
HELP_TEXT = (
    "สวัสดีครับ! ผมคือ Stock Signal Bot 🤖\n\n"
    "คำสั่งที่ใช้ได้:\n"
    "/ping – ทดสอบบอท\n"
    "/signals TICKER1 TICKER2 ... – ขอวิเคราะห์ เช่น /signals AAPL TSLA NVDA\n"
    "/help – แสดงเมนูนี้\n\n"
    "เกณฑ์สรุปเวอร์ชันใหม่: RSI 35/65, แนวโน้มจาก EMA20/50/200, MACD cross, วอลุ่มเทียบค่าเฉลี่ย, "
    "พร้อมคะแนน/แผนเข้า-ออก/ความเสี่ยง"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("พิมพ์รูปแบบ: /signals AAPL TSLA NVDA")
        return

    raw_syms = [re.sub(r"[^A-Za-z0-9^.-]", "", s).upper() for s in context.args]
    syms = [s for s in raw_syms if s]
    if not syms:
        await update.message.reply_text("รูปแบบสัญลักษณ์ไม่ถูกต้อง")
        return

    # จำกัดครั้งละไม่เกิน 8 ตัว เพื่อไม่ให้แบนด์วิธเยอะเกิน
    syms = syms[:8]

    await update.message.reply_text(
        f"⌛ กำลังวิเคราะห์: {', '.join(syms)} ..."
    )

    cards: List[str] = []
    errors: List[str] = []
    for s in syms:
        try:
            res = analyze_symbol(s)
            cards.append(fmt_card(res))
        except Exception as e:
            log.exception("analyze error for %s", s)
            errors.append(f"{s}: {e}")

    msg = ""
    if cards:
        # เรียงตามคะแนน
        scored = []
        for c, s in zip(cards, syms):
            m = re.search(r"คะแนนสัญญาณ: <b>(\d+)</b>", c)
            score = int(m.group(1)) if m else 0
            scored.append((score, c))
        scored.sort(reverse=True)
        msg += "\n\n".join([c for _, c in scored])

    if errors:
        msg += "\n\n⚠️ เกิดข้อผิดพลาดบางตัว:\n- " + "\n- ".join(errors)

    if not msg:
        msg = "ไม่มีข้อมูลที่แสดงได้ในขณะนี้ครับ"

    await update.message.reply_html(msg, disable_web_page_preview=True)


# -----------------------------------------------------------------------------
# BUILD TELEGRAM APP & RUN (POLLING IN BACKGROUND THREAD)
# -----------------------------------------------------------------------------
def run_telegram_in_thread():
    """
    Run PTB v21 Application.run_polling() in a dedicated thread
    with stop_signals=() to avoid set_wakeup_fd error on non-main thread.
    """
    async def main_coro():
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", cmd_start))
        application.add_handler(CommandHandler("help", cmd_help))
        application.add_handler(CommandHandler("ping", cmd_ping))
        application.add_handler(CommandHandler("signals", cmd_signals))
        # run_polling is a *sync* helper, but we’ll call it via loop.run_in_executor?
        # Simpler: call from here directly using PTB API (async start/idle):
        await application.initialize()
        await application.start()
        log.info("Telegram bot started (polling)")
        try:
            # Manually start polling with Application.start + Application.updater
            # PTB v21: use application.updater.start_polling() — handled inside run_polling normally.
            await application.run_polling(
    poll_interval=1.5,
    allowed_updates=Update.ALL_TYPES,
    stop_signals=(),
            )
        finally:
            await application.stop()
            await application.shutdown()
            log.info("Telegram bot stopped")

    # Each thread can own its own loop:
    asyncio.run(main_coro())


# -----------------------------------------------------------------------------
# FLASK APP (HEALTHCHECK + KEEP PORT OPEN ON RENDER)
# -----------------------------------------------------------------------------
app = Flask(__name__)

@app.get("/")
def health() -> Response:
    return Response(
        f"✅ Bot is running — {utcnow_iso()}",
        content_type="text/plain; charset=utf-8",
        status=200,
    )


# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
def main():
    # Start Telegram thread
    t = threading.Thread(target=run_telegram_in_thread, name="tg-thread", daemon=True)
    t.start()

    log.info("Starting Flask on 0.0.0.0:%s", PORT)
    # IMPORTANT: debug=False, use_reloader=False to avoid double threads on Render
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
