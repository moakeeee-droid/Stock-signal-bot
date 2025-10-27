# main.py
# -*- coding: utf-8 -*-

import os
import asyncio
import logging
import signal
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional

import requests
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("stock-signal-bot")

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN is not set")

PORT = int(os.environ.get("PORT", "10000"))

# รายชื่อหุ้นเริ่มต้น (แก้ไขได้ หรือผ่าน ENV: PICKS=BYND,KUKE,GSIT)
DEFAULT_PICKS = os.environ.get("PICKS", "BYND,KUKE,GSIT").split(",")
DEFAULT_PICKS = [s.strip().upper() for s in DEFAULT_PICKS if s.strip()]

# กลุ่มหุ้นสำหรับสแกนภาพรวม/สัญญาณ (แก้ไขได้ หรือใช้ ENV: UNIVERSE=...)
DEFAULT_UNIVERSE = os.environ.get(
    "UNIVERSE",
    "AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META,SPY,QQQ,IWM,AMD,INTC,NFLX,BA,XOM,JPM,SCHW,ORCL,CRM,ADBE"
).split(",")
DEFAULT_UNIVERSE = [s.strip().upper() for s in DEFAULT_UNIVERSE if s.strip()]

# =========================
# Yahoo helpers (no API key)
# =========================
Y_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={ival}"
Y_QUOTE = "https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}"

def http_get_json(url: str, timeout=10) -> Optional[dict]:
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        log.warning("HTTP %s -> %s", resp.status_code, url)
    except Exception as e:
        log.warning("GET failed: %s | %s", e, url)
    return None

def fetch_quote_batch(symbols: List[str]) -> Dict[str, dict]:
    """ดึงข้อมูล quote หลายตัวทีเดียว"""
    if not symbols:
        return {}
    url = Y_QUOTE.format(symbols=",".join(symbols))
    data = http_get_json(url)
    out = {}
    try:
        for row in data["quoteResponse"]["result"]:
            sym = row.get("symbol", "").upper()
            out[sym] = row
    except Exception:
        pass
    return out

def fetch_chart(sym: str, rng="6mo", ival="1d") -> Tuple[List[int], List[float]]:
    """ดึงแท่งราคา (timestamp, close)"""
    url = Y_CHART.format(sym=sym, rng=rng, ival=ival)
    data = http_get_json(url)
    try:
        c = data["chart"]["result"][0]
        ts = c["timestamp"]
        cl = c["indicators"]["quote"][0]["close"]
        # กรอง None
        pairs = [(t, v) for t, v in zip(ts, cl) if v is not None]
        if not pairs:
            return [], []
        tss, cls = zip(*pairs)
        return list(tss), list(cls)
    except Exception:
        return [], []

# =========================
# Indicators (pure python)
# =========================
def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n

def rsi14(values: List[float]) -> Optional[float]:
    n = 14
    if len(values) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-n, 0):
        chg = values[i] - values[i - 1]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    if gains == 0 and losses == 0:
        return 50.0
    rs = (gains / n) / (losses / n if losses != 0 else 1e-9)
    return 100 - (100 / (1 + rs))

def pct(a: float, b: float) -> Optional[float]:
    if b == 0 or b is None:
        return None
    return (a - b) / b * 100.0

# =========================
# Tiny cache ลด call Yahoo
# =========================
class TTLCache:
    def __init__(self, ttl_sec: int = 60):
        self.ttl = ttl_sec
        self.store: Dict[str, Tuple[float, object]] = {}

    def get(self, key: str):
        ts_val = self.store.get(key)
        if not ts_val:
            return None
        ts, val = ts_val
        if time.time() - ts > self.ttl:
            self.store.pop(key, None)
            return None
        return val

    def set(self, key: str, val):
        self.store[key] = (time.time(), val)

cache_quote = TTLCache(ttl_sec=45)   # quote สดหน่อย
cache_chart = TTLCache(ttl_sec=300)  # กราฟ cache นานขึ้น

async def get_quote(symbols: List[str]) -> Dict[str, dict]:
    key = f"q:{','.join(symbols)}"
    got = cache_quote.get(key)
    if got is not None:
        return got
    data = fetch_quote_batch(symbols)
    cache_quote.set(key, data)
    return data

async def get_chart(sym: str) -> Tuple[List[int], List[float]]:
    key = f"c:{sym}:6mo:1d"
    got = cache_chart.get(key)
    if got is not None:
        return got
    data = fetch_chart(sym, "6mo", "1d")
    cache_chart.set(key, data)
    return data

# =========================
# Healthcheck (aiohttp)
# =========================
async def health(request: web.Request) -> web.Response:
    now = datetime.now(timezone.utc).isoformat()
    return web.Response(text=f"✅ Bot is running — {now}", content_type="text/plain")

async def run_http_server(stop_event: asyncio.Event) -> None:
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP healthcheck started on :%s", PORT)
    await stop_event.wait()
    log.info("HTTP server stopping...")
    await runner.cleanup()

# =========================
# Format helpers
# =========================
def fmt_num(x: Optional[float], digits=2, suffix="") -> str:
    if x is None:
        return "—"
    try:
        return f"{x:.{digits}f}{suffix}"
    except Exception:
        return "—"

def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"

def market_cap_str(v: Optional[float]) -> str:
    if v is None:
        return "—"
    units = [("T", 1e12), ("B", 1e9), ("M", 1e6)]
    for s, k in units:
        if v >= k:
            return f"{v / k:.2f}{s}"
    return f"{v:.0f}"

# =========================
# Telegram Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 ยินดีต้อนรับสู่ Stock Signal Bot (ฟรี/ทดลอง)\n\n"
        "คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals - สัญญาณรวมจากชุดหุ้น\n"
        "/outlook - ภาพรวมตลาด (SPY, QQQ, IWM)\n"
        "/picks - หุ้นเด่น (พร้อมรายละเอียด)\n"
        "/movers - หุ้นเคลื่อนไหว (ตัวอย่างจากชุดสแกน)\n"
    )
    await update.message.reply_text(text)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong 🏓")

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ใช้ ETF เป็นตัวแทนภาพรวม
    bench = ["SPY", "QQQ", "IWM"]
    quotes = await get_quote(bench)
    parts = ["📈 Outlook วันนี้:"]
    for s in bench:
        q = quotes.get(s, {})
        price = q.get("regularMarketPrice")
        chg = q.get("regularMarketChangePercent")
        parts.append(f"• {s}: {fmt_num(price)} ({fmt_pct(chg)})")
    # โมเมนตัมแบบหยาบ ๆ จาก SMA20 เทียบราคา
    mom = []
    for s in bench:
        _, closes = await get_chart(s)
        sm20 = sma(closes, 20)
        if closes and sm20:
            mom.append(1 if closes[-1] > sm20 else -1)
    if mom:
        score = sum(mom)
        if score >= 2:
            mood = "ขาขึ้นอ่อน ๆ"
        elif score <= -2:
            mood = "ขาลงอ่อน ๆ"
        else:
            mood = "โมเมนตัมกลางๆ"
        parts.append(f"สรุปโมเมนตัม: {mood}")
    await update.message.reply_text("\n".join(parts))

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # กติกาอย่างง่าย:
    # CALL  : ปิด > SMA50 และ RSI14 > 55
    # PUT   : ปิด < SMA50 และ RSI14 < 45
    # Neutral: อื่น ๆ
    universe = DEFAULT_UNIVERSE
    strong_call = 0
    strong_put = 0
    checked = 0

    # ดึงทีละล็อตเพื่อลด call (quotes ใช้รวบ, chart ดึงรายตัวเพื่อทำอินดี้)
    quotes = await get_quote(universe)

    for sym in universe:
        checked += 1
        _, closes = await get_chart(sym)
        if not closes:
            continue
        price = closes[-1]
        sm50 = sma(closes, 50)
        rsi = rsi14(closes)
        if sm50 is None or rsi is None:
            continue
        if price > sm50 and rsi > 55:
            strong_call += 1
        elif price < sm50 and rsi < 45:
            strong_put += 1

    txt = (
        "🔮 Signals (ชุดสแกน)\n"
        f"Strong CALL: {strong_call} | Strong PUT: {strong_put}\n"
        f"(ตรวจ {checked} ตัวจากชุดสแกน)"
    )
    await update.message.reply_text(txt)

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    picks = DEFAULT_PICKS
    quotes = await get_quote(picks)
    lines = ["🧾 Picks (รายละเอียด)"]
    for sym in picks:
        q = quotes.get(sym)
        if not q:
            lines.append(f"⚠️ {sym}: ข้อมูลไม่พร้อม")
            continue

        price = q.get("regularMarketPrice")
        chg = q.get("regularMarketChangePercent")
        vol = q.get("regularMarketVolume")
        avg_vol = q.get("averageDailyVolume3Month")
        mcap = q.get("marketCap")
        pe = q.get("trailingPE")

        _, closes = await get_chart(sym)
        if not closes:
            lines.append(f"⚠️ {sym}: ข้อมูลไม่พร้อม")
            continue

        sm20 = sma(closes, 20)
        sm50 = sma(closes, 50)
        rsi = rsi14(closes)

        trend = "⬆️" if (sm20 and sm50 and sm20 > sm50) else "⬇️" if (sm20 and sm50 and sm20 < sm50) else "➡️"
        bias = (
            "Bullish" if (closes[-1] > (sm50 or closes[-1]) and (rsi or 50) >= 55) else
            "Bearish" if (closes[-1] < (sm50 or closes[-1]) and (rsi or 50) <= 45) else
            "Neutral"
        )

        lines.append(
            f"• {sym}: {fmt_num(price)} ({fmt_pct(chg)}) {trend} {bias}\n"
            f"   RSI14: {fmt_num(rsi)} | SMA20/50: {fmt_num(sm20)}/{fmt_num(sm50)}\n"
            f"   Vol: {vol:,} (Avg: {avg_vol:,}) | MCap: {market_cap_str(mcap)} | PE: {fmt_num(pe,2)}"
        )
    await update.message.reply_text("\n".join(lines))

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ใช้ชุด DEFAULT_UNIVERSE หา top เคลื่อนไหวรายวัน (จาก close ล่าสุดเทียบก่อนหน้า)
    lines = ["📊 Movers (จากชุดสแกน)"]
    changes: List[Tuple[str, float]] = []
    for sym in DEFAULT_UNIVERSE:
        _, closes = await get_chart(sym)
        if len(closes) >= 2:
            chg = pct(closes[-1], closes[-2]) or 0.0
            changes.append((sym, chg))
    if not changes:
        await update.message.reply_text("ยังไม่มีข้อมูลเพียงพอครับ")
        return
    # เลือก top gainers/losers อย่างละ 3
    changes.sort(key=lambda x: x[1], reverse=True)
    gainers = changes[:3]
    losers = sorted(changes, key=lambda x: x[1])[:3]
    lines.append("↑ Gainers: " + ", ".join(f"{s} ({fmt_pct(c)})" for s, c in gainers))
    lines.append("↓ Losers: " + ", ".join(f"{s} ({fmt_pct(c)})" for s, c in losers))
    await update.message.reply_text("\n".join(lines))

# =========================
# Application builder
# =========================
def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("picks", cmd_picks))
    app.add_handler(CommandHandler("movers", cmd_movers))
    return app

# =========================
# Bot lifecycle (POLLING + delete_webhook)
# =========================
async def bot_run(application: Application, stop_event: asyncio.Event) -> None:
    log.info("Starting Telegram bot (polling mode)")
    await application.initialize()
    await application.start()

    # สำคัญ: ล้าง webhook เดิม เพื่อกัน Conflict
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted.")
    except Exception as e:
        log.warning("delete_webhook() failed: %s", e)

    await application.updater.start_polling(drop_pending_updates=True, poll_interval=1.5)
    log.info("Polling started.")
    await stop_event.wait()

    log.info("Stopping Telegram bot...")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    log.info("Bot stopped.")

# =========================
# Main
# =========================
async def main_async() -> None:
    log.info("Booting service...")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    application = build_application()
    http_task = asyncio.create_task(run_http_server(stop_event))
    bot_task = asyncio.create_task(bot_run(application, stop_event))

    try:
        await asyncio.gather(http_task, bot_task)
    except asyncio.CancelledError:
        pass
    finally:
        stop_event.set()
        with contextlib.suppress(Exception):
            await asyncio.gather(http_task, bot_task, return_exceptions=True)
        log.info("Service shutdown complete.")

def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    import contextlib
    main()
