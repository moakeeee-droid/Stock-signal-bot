# -*- coding: utf-8 -*-
"""
Stock-signal-bot — Render-friendly / Polling + aiohttp health
Mode: Swing (2–5 วัน) พร้อม TP/SL อัตโนมัติ

Env ต้องมี:
  BOT_TOKEN       : โทเคน Telegram Bot
  PORT            : พอร์ตจาก Render (เช่น 10000)
แนะนำ:
  DATA_SOURCE     : 'yahoo' (ค่าเริ่มต้น) หรือ 'demo'
  TZ              : 'Asia/Bangkok'
  LOG_LEVEL       : 'INFO' (default), 'DEBUG'
"""

from __future__ import annotations

import os
import re
import math
import asyncio
import logging
import signal
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

import aiohttp
from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# =========================
# Config / Logging
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("stock-signal-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is required")

PORT = int(os.getenv("PORT", "10000"))
DATA_SOURCE = os.getenv("DATA_SOURCE", "yahoo").lower()  # yahoo | demo
TZ_NAME = os.getenv("TZ", "Asia/Bangkok")

# ค่ามาตรฐานสำหรับคำสั่งตัวอย่าง
DEFAULT_PICKS = ["BYND", "KUKE", "GSIT"]
DEFAULT_MOVERS = ["AAPL", "NVDA", "TSLA"]

# =========================
# Yahoo Clients (no API key)
# =========================
YF_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YF_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Connection": "keep-alive",
}

async def fetch_yahoo_quotes(symbols: List[str], timeout: float = 10.0, retries: int = 2) -> Dict[str, Dict[str, Any]]:
    params = {"symbols": ",".join(s.upper() for s in symbols)}
    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession(headers=YF_HEADERS) as session:
                async with session.get(YF_QUOTE_URL, params=params, timeout=timeout) as r:
                    if r.status != 200:
                        raise RuntimeError(f"Yahoo quote HTTP {r.status}")
                    js = await r.json()
                    out: Dict[str, Dict[str, Any]] = {}
                    for q in js.get("quoteResponse", {}).get("result", []):
                        s = (q.get("symbol") or "").upper()
                        if not s:
                            continue
                        out[s] = {
                            "symbol": s,
                            "name": q.get("shortName") or q.get("longName") or s,
                            "price": q.get("regularMarketPrice"),
                            "change": q.get("regularMarketChange"),
                            "changePct": q.get("regularMarketChangePercent"),
                            "prevClose": q.get("regularMarketPreviousClose"),
                            "currency": q.get("currency") or "",
                            "marketState": q.get("marketState") or "",
                        }
                    return out
        except Exception as e:
            log.warning("fetch_yahoo_quotes attempt %s: %s", attempt+1, e)
            if attempt >= retries:
                return {}
            await asyncio.sleep(1.2 * (attempt + 1))
    return {}

async def fetch_yahoo_candles(symbol: str, period: str = "6mo", interval: str = "1d",
                              timeout: float = 12.0, retries: int = 2) -> Dict[str, List[float]]:
    params = {"range": period, "interval": interval, "includePrePost": "false"}
    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession(headers=YF_HEADERS) as session:
                async with session.get(YF_CHART_URL.format(symbol=symbol), params=params, timeout=timeout) as r:
                    if r.status != 200:
                        raise RuntimeError(f"Yahoo chart HTTP {r.status}")
                    js = await r.json()
                    result = js.get("chart", {}).get("result", [])
                    if not result:
                        return {"close": [], "volume": []}
                    q = result[0]["indicators"]["quote"][0]
                    ts = result[0].get("timestamp", [])
                    closes = q.get("close", [])
                    vols = q.get("volume", [])
                    # clean None by dropping
                    rows = [(t, c, v) for t, c, v in zip(ts, closes, vols) if c is not None]
                    closes = [float(c) for _, c, _ in rows]
                    vols = [int(v or 0) for _, _, v in rows]
                    return {"close": closes, "volume": vols}
        except Exception as e:
            log.warning("fetch_yahoo_candles %s attempt %s: %s", symbol, attempt+1, e)
            if attempt >= retries:
                return {"close": [], "volume": []}
            await asyncio.sleep(1.2 * (attempt + 1))
    return {"close": [], "volume": []}

# =========================
# TA (EMA / RSI / MACD) – pure python
# =========================
def ema(seq: List[float], period: int) -> List[float]:
    if not seq:
        return []
    k = 2 / (period + 1)
    out: List[float] = []
    ema_prev = None
    for v in seq:
        if ema_prev is None:
            ema_prev = v
        else:
            ema_prev = v * k + ema_prev * (1 - k)
        out.append(ema_prev)
    return out

def rsi(seq: List[float], period: int = 14) -> List[float]:
    if len(seq) < period + 1:
        return [50.0] * len(seq)
    gains, losses = [], []
    for i in range(1, len(seq)):
        ch = seq[i] - seq[i - 1]
        gains.append(max(0.0, ch))
        losses.append(max(0.0, -ch))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = [50.0] * period
    r = 100.0 if avg_loss == 0 else 100 - (100/(1+avg_gain/avg_loss))
    rsis.append(r)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        r = 100.0 if avg_loss == 0 else 100 - (100/(1+avg_gain/avg_loss))
        rsis.append(r)
    return rsis if len(rsis) == len(seq) else ([50.0]*(len(seq)-len(rsis)) + rsis)

def macd(seq: List[float]) -> Dict[str, List[float]]:
    ema12 = ema(seq, 12)
    ema26 = ema(seq, 26)
    macd_line = [a-b for a,b in zip(ema12, ema26)]
    signal = ema(macd_line, 9)
    hist = [m - s for m, s in zip(macd_line, signal)]
    return {"macd": macd_line, "signal": signal, "hist": hist}

# =========================
# Swing mode scoring & plan
# =========================
def swing_score_and_bias(closes: List[float]) -> Dict[str, Any]:
    """
    คืนค่า:
      - price, ema20, ema50, ema200, rsi14, macd, signal, hist, hist_slope
      - trend: Uptrend/Downtrend/Sideways
      - bias: CALL/PUT/NEUTRAL
      - score: int
    """
    if len(closes) < 60:
        # ข้อมูลน้อย -> กลาง ๆ
        p = closes[-1] if closes else float("nan")
        return {"price": p, "ema20": p, "ema50": p, "ema200": p,
                "rsi14": 50.0, "macd": 0.0, "signal": 0.0, "hist": 0.0, "hist_slope": 0.0,
                "trend": "Sideways", "bias": "NEUTRAL", "score": 0}

    ema20v = ema(closes, 20)
    ema50v = ema(closes, 50)
    ema200v = ema(closes, 200)
    rsi14v = rsi(closes, 14)
    mac = macd(closes)

    p = closes[-1]
    e20 = ema20v[-1]; e50 = ema50v[-1]; e200 = ema200v[-1]
    r = rsi14v[-1]
    m = mac["macd"][-1]; s = mac["signal"][-1]; h = mac["hist"][-1]
    h_prev = mac["hist"][-2] if len(mac["hist"]) >= 2 else h
    h_slope = h - h_prev

    trend = "Uptrend" if (p > e20 > e50 > e200) else ("Downtrend" if (p < e20 < e50 < e200) else "Sideways")
    score = 0
    # โหมด Swing: ให้ความสำคัญกับโครงสร้างและโมเมนตัมปานกลาง
    if p > e50: score += 2
    if e20 > e50: score += 2
    if p > e20: score += 1
    if m > s: score += 1
    if h > 0 and h_slope > 0: score += 1
    # RSI sweet zone สำหรับ swing (45–65)
    if 45 <= r <= 65: score += 1
    # Penalty ร้อน/เย็นเกินไป
    if r >= 70: score -= 2
    if r <= 30: score -= 2

    # ตีความ bias
    if score >= 5 and trend != "Downtrend":
        bias = "CALL"
    elif score <= -2 and trend != "Uptrend":
        bias = "PUT"
    else:
        bias = "NEUTRAL"

    return {
        "price": p, "ema20": e20, "ema50": e50, "ema200": e200,
        "rsi14": r, "macd": m, "signal": s, "hist": h, "hist_slope": h_slope,
        "trend": trend, "bias": bias, "score": score
    }

def swing_plan(sig: Dict[str, Any]) -> Dict[str, str]:
    """
    ให้ TP/SL อัตโนมัติ (ช่วงโดยประมาณสำหรับถือ 2–5 วัน)
    CALL:
      - TP1 ~ +2.5% | TP2 ~ +5.0%
      - SL  ~ -2.0% (หรือหลุด EMA20)
    PUT:
      - TP1 ~ +2.5% | TP2 ~ +4.5% (ทางลง)
      - SL  ~ +2.0% (ราคาเด้งสวน)
    NEUTRAL: รอทรงชัด
    """
    bias = sig["bias"]
    if bias == "CALL":
        return {
            "tp1": "+2.5%", "tp2": "+5.0%",
            "sl": "-2.0% หรือหลุด EMA20", "note": "ย่อไม่หลุด EMA20 / MACD ยังบวก"
        }
    if bias == "PUT":
        return {
            "tp1": "+2.5%", "tp2": "+4.5%",
            "sl": "+2.0% (เด้งสวนแรง)", "note": "เด้งไม่ผ่านแนวต้าน / MACD ยังลบ"
        }
    return {"tp1": "-", "tp2": "-", "sl": "-", "note": "รอเบรกหรือยืนยันโมเมนตัม"}

# =========================
# Helpers
# =========================
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def fmt_num(x: Optional[float], digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    fmt = f"{{:.{digits}f}}"
    return fmt.format(x)

def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}%"

# =========================
# Command Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "👋 ยินดีต้อนรับสู่ Stock Signal Bot — Swing Mode (2–5 วัน)\n\n"
        "คำสั่งที่ใช้ได้:\n"
        "/ping — ทดสอบบอท\n"
        "/signals [TICKERS] — สัญญาณ Swing พร้อม TP/SL (เช่น /signals TSLA NVDA AAPL)\n"
        "/outlook — มุมมองตลาด (สรุป)\n"
        "/picks — หุ้นตัวอย่าง\n"
        "/movers — หุ้นเด่นเคลื่อนไหวมาก\n\n"
        f"⏱️ {utc_iso()}"
    )
    await update.message.reply_text(txt)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # เวอร์ชันสั้น (demo): สามารถผูก breadth/advance-decline ภายหลังได้
    await update.message.reply_text("📈 Outlook วันนี้: โมเมนตัมปานกลาง เหมาะกับ Swing ตามแนวรับ/ต้าน")

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ กำลังดึงข้อมูลหุ้น (Picks)...")
    syms = DEFAULT_PICKS
    quotes: Dict[str, Dict[str, Any]] = {}
    if DATA_SOURCE == "yahoo":
        quotes = await fetch_yahoo_quotes(syms)
    lines = ["🧾 Picks (รายละเอียด)"]
    for s in syms:
        q = quotes.get(s) if quotes else None
        if not q or q.get("price") is None:
            lines.append(f"⚠️ {s}: ข้อมูลไม่พร้อม")
        else:
            lines.append(f"✅ {s}: {q['name']} — {fmt_num(q['price'])} {q.get('currency','')} ({fmt_pct(q.get('changePct'))})")
    await update.message.reply_text("\n".join(lines))

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = DEFAULT_MOVERS
    quotes: Dict[str, Dict[str, Any]] = {}
    if DATA_SOURCE == "yahoo":
        quotes = await fetch_yahoo_quotes(syms)
    lines = ["🚀 Movers วันนี้"]
    for s in syms:
        q = quotes.get(s) if quotes else None
        if not q or q.get("price") is None:
            lines.append(f"• {s}: ข้อมูลไม่พร้อม")
        else:
            lines.append(f"• {s}: {fmt_num(q['price'])} ({fmt_pct(q.get('changePct'))})")
    await update.message.reply_text("\n".join(lines))

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ใช้ ticker จาก args หรือถ้าไม่ระบุ ใช้ DEFAULT_PICKS
    tickers = [re.sub(r"[^A-Za-z0-9^.-]", "", t).upper() for t in (context.args or DEFAULT_PICKS)]
    tickers = [t for t in tickers if t][:8]  # จำกัดไม่เกิน 8 ตัว
    await update.message.reply_text("🔎 กำลังคำนวณสัญญาณ Swing...")

    # ดึง quote ล่วงหน้า (เพื่อชื่อ/ราคา)
    quotes: Dict[str, Dict[str, Any]] = {}
    if DATA_SOURCE == "yahoo":
        quotes = await fetch_yahoo_quotes(tickers)

    blocks: List[str] = []
    for t in tickers:
        # ดึงแท่งเทียนไปคำนวณอินดิเคเตอร์
        if DATA_SOURCE == "yahoo":
            cd = await fetch_yahoo_candles(t, period="6mo", interval="1d")
            closes = cd.get("close", [])
        else:
            # DEMO: ทำราคาจำลองง่าย ๆ
            closes = [100 + math.sin(i/5)*2 for i in range(120)]

        if not closes or len(closes) < 30:
            blocks.append(f"❌ {t}: ข้อมูลแท่งเทียนไม่พอ")
            continue

        sig = swing_score_and_bias(closes)
        plan = swing_plan(sig)

        q = quotes.get(t) if quotes else None
        name = (q or {}).get("name") or t
        price = (q or {}).get("price") or sig["price"]
        ch_pct = (q or {}).get("changePct")

        lines = [
            f"📌 <b>{t}</b> — {name}",
            f"ราคา: <b>{fmt_num(price)}</b> ({fmt_pct(ch_pct)})",
            f"แนวโน้ม: <b>{sig['trend']}</b>  |  RSI14: <b>{fmt_num(sig['rsi14'],1)}</b>",
            f"EMA20/50/200: <code>{fmt_num(sig['ema20'])} / {fmt_num(sig['ema50'])} / {fmt_num(sig['ema200'])}</code>",
            f"MACD: <code>{fmt_num(sig['macd'],3)} / {fmt_num(sig['signal'],3)} | Hist {fmt_num(sig['hist'],3)} (Δ {fmt_num(sig['hist_slope'],3)})</code>",
            f"สรุป (Swing): <b>{sig['bias']}</b>  |  คะแนน: <b>{sig['score']}</b>",
            f"แผน: TP1 {plan['tp1']} · TP2 {plan['tp2']} · SL {plan['sl']}  — {plan['note']}",
        ]
        blocks.append("\n".join(lines))

    msg = "\n\n".join(blocks)
    await update.message.reply_html(msg, disable_web_page_preview=True)

# =========================
# aiohttp health server
# =========================
async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text=f"✅ Bot is running — {utc_iso()}\n", content_type="text/plain")

def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_root)
    return app

# =========================
# Lifecycle (Polling without run_polling)
# =========================
async def bot_run(application: Application, stop_event: asyncio.Event) -> None:
    log.info("Starting Telegram bot (polling mode)")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    await stop_event.wait()
    log.info("Stopping Telegram bot...")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    log.info("Telegram bot stopped")

# =========================
# Main
# =========================
async def main_async():
    log.info("Config | PORT=%s | DATA_SOURCE=%s | TZ=%s", PORT, DATA_SOURCE, TZ_NAME)

    # สร้าง Telegram application + handlers
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ping",    cmd_ping))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("picks",   cmd_picks))
    app.add_handler(CommandHandler("movers",  cmd_movers))
    app.add_handler(CommandHandler("signals", cmd_signals))

    # aiohttp health server (Render ต้องการให้เปิดพอร์ต)
    web_app = build_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("HTTP server started on 0.0.0.0:%d", PORT)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _graceful_stop():
        if not stop_event.is_set():
            log.info("Shutdown signal received")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _graceful_stop)
        except NotImplementedError:
            pass

    bot_task = asyncio.create_task(bot_run(app, stop_event), name="tg-bot")

    try:
        await bot_task
    finally:
        await runner.cleanup()
        log.info("HTTP server stopped")
    log.info("Application terminated")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
