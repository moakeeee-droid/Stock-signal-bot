# main.py
# =============================
# Stock Signal Bot (Yahoo data)
# Async + PTB v21 + aiohttp webhook
# =============================

import os
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import aiohttp
from aiohttp import web

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("stock-signal-bot")

# -----------------------------
# Environment
# -----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MODE = os.environ.get("MODE", "webhook").lower().strip()  # webhook | polling
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
PORT = int(os.environ.get("PORT", "10000"))
REQ_TIMEOUT = int(os.environ.get("TIMEOUT", "8"))

DEFAULT_PICKS = [s.strip().upper() for s in os.environ.get("PICKS", "BYND,KUKE,GSIT").split(",") if s.strip()]
DEFAULT_UNIVERSE = [
    s.strip().upper()
    for s in os.environ.get(
        "UNIVERSE",
        # ดัชนี/หุ้นตัวใหญ่ๆ + ETF ที่คนเล่นบ่อย
        "AAPL,MSFT,NVDA,GOOGL,AMZN,META,TSLA,AMD,AVGO,CRM,ADBE,COST,LIN,"
        "NFLX,ORCL,INTC,PEP,AMAT,TSM,TMUS,"
        "SPY,QQQ,IWM,XLK,XLF,XLE,XLY,XLV,XLI,XLB"
    ).split(",")
    if s.strip()
]

# -----------------------------
# HTTP Client
# -----------------------------
_http_session: Optional[aiohttp.ClientSession] = None


def http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQ_TIMEOUT),
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; StockSignalBot/1.0; +https://t.me/)"
            },
        )
    return _http_session


# -----------------------------
# Yahoo helpers (unofficial)
# -----------------------------
Y_BASE = "https://query1.finance.yahoo.com"

async def yf_quote(symbols: List[str]) -> Dict[str, dict]:
    """Fetch quotes for symbols from Yahoo (some may fail)."""
    out: Dict[str, dict] = {}
    if not symbols:
        return out

    # Yahoo supports up to ~50 per request; we chunk to be safe
    chunk = 30
    for i in range(0, len(symbols), chunk):
        batch = ",".join(symbols[i:i+chunk])
        url = f"{Y_BASE}/v7/finance/quote?symbols={batch}"
        try:
            async with http_session().get(url) as r:
                if r.status != 200:
                    log.warning("quote HTTP %s on %s", r.status, batch)
                    continue
                data = await r.json()
                for row in data.get("quoteResponse", {}).get("result", []):
                    sym = row.get("symbol")
                    if sym:
                        out[sym.upper()] = row
        except Exception as e:
            log.exception("quote error: %s", e)
    return out


async def yf_chart(symbol: str, rng: str = "6mo", interval: str = "1d") -> Tuple[List[int], List[float]]:
    """
    Return (timestamps, close_prices). If fail -> empty.
    """
    params = f"range={rng}&interval={interval}&includePrePost=false&events=div|split&corsDomain=finance.yahoo.com"
    url = f"{Y_BASE}/v8/finance/chart/{symbol}?{params}"
    try:
        async with http_session().get(url) as r:
            if r.status != 200:
                log.warning("chart HTTP %s on %s", r.status, symbol)
                return [], []
            data = await r.json()
            res = data.get("chart", {}).get("result", [])
            if not res:
                return [], []
            series = res[0]
            ts = series.get("timestamp", []) or []
            closes = series.get("indicators", {}).get("quote", [{}])[0].get("close", []) or []
            # filter None
            t2, c2 = [], []
            for t, c in zip(ts, closes):
                if c is not None:
                    t2.append(t)
                    c2.append(float(c))
            return t2, c2
    except Exception as e:
        log.exception("chart error %s: %s", symbol, e)
        return [], []


# -----------------------------
# Indicators
# -----------------------------
def sma(vals: List[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def rsi14(vals: List[float]) -> Optional[float]:
    if len(vals) < 15:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-14, 0):
        chg = vals[i] - vals[i-1]
        gains += chg if chg > 0 else 0.0
        losses += -chg if chg < 0 else 0.0
    if losses == 0:
        return 100.0
    rs = (gains / 14.0) / (losses / 14.0)
    return 100.0 - 100.0 / (1.0 + rs)


def pct(a: float, b: float) -> Optional[float]:
    try:
        if b == 0:
            return None
        return (a - b) / b * 100.0
    except Exception:
        return None


# -----------------------------
# Formatting
# -----------------------------
def fmt_pct(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.{digits}f}%"

def fmt_num(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:.{digits}f}"

def market_cap_str(x: Optional[float]) -> str:
    if not x:
        return "—"
    # billions / trillions
    n = float(x)
    if n >= 1e12:
        return f"{n/1e12:.2f}T"
    if n >= 1e9:
        return f"{n/1e9:.2f}B"
    if n >= 1e6:
        return f"{n/1e6:.2f}M"
    return f"{int(n)}"

# -----------------------------
# Picks Auto (Momentum-ish)
# -----------------------------
async def choose_dynamic_picks(n: int = 3) -> List[str]:
    """
    เลือกหุ้นเด่นจาก DEFAULT_UNIVERSE ตามกติกา:
    - มีข้อมูลอย่างน้อย 50 แท่ง
    - ราคาล่าสุด > SMA50
    - RSI14 > 55
    - เรียงตาม % เปลี่ยนของวันล่าสุด (ล่าสุดเทียบวันก่อน)
    """
    scores: List[Tuple[str, float]] = []
    # เพื่อความเร็ว: ขอ quote รอบเดียวเอาชื่อไว้ทำ sanity (ไม่บังคับ)
    for sym in DEFAULT_UNIVERSE:
        _, closes = await yf_chart(sym)
        if len(closes) < 50:
            continue
        price = closes[-1]
        sm50 = sma(closes, 50)
        rsi = rsi14(closes)
        if sm50 is None or rsi is None:
            continue
        if price > sm50 and rsi > 55:
            dchg = pct(price, closes[-2]) if len(closes) >= 2 else 0.0
            scores.append((sym, dchg or 0.0))

    scores.sort(key=lambda x: x[1], reverse=True)
    picks = [s for s, _ in scores[:n]]
    log.info("AUTO PICKS -> %s", picks)
    return picks


# -----------------------------
# Telegram Handlers
# -----------------------------
def parse_symbols_from_args(args: List[str]) -> List[str]:
    if not args:
        return []
    joined = " ".join(args).replace(",", " ")
    syms = [s.strip().upper() for s in joined.split() if s.strip()]
    return syms[:12]


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong 🏓")


async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # เอา top movers จาก universe แบบง่าย: เทียบ %day change แล้วเลือก 3 ตัวบน/ล่าง
    universe = DEFAULT_UNIVERSE[:20]  # กันยาวเกิน
    quotes = await yf_quote(universe)

    rows = []
    for sym in universe:
        q = quotes.get(sym)
        if not q:
            continue
        rows.append(
            (sym, q.get("regularMarketChangePercent"))
        )
    rows = [(s, c if isinstance(c, (int, float)) else None) for s, c in rows]

    # top gainers/losers
    top = sorted([r for r in rows if r[1] is not None], key=lambda x: x[1], reverse=True)
    gainers = top[:3]
    losers = top[-3:][::-1] if len(top) >= 3 else []

    lines = ["📊 Movers (จากชุดสแกน)"]
    if gainers:
        gtxt = ", ".join([f"{s} ({fmt_pct(p)})" for s, p in gainers])
        lines.append(f"↑ Gainers: {gtxt}")
    if losers:
        ltxt = ", ".join([f"{s} ({fmt_pct(p)})" for s, p in losers])
        lines.append(f"↓ Losers: {ltxt}")

    await update.message.reply_text("\n".join(lines))


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # นับสัญญาณจาก universe: CALL ถ้าปิด>50sma & RSI>55, PUT ถ้าปิด<50sma & RSI<45
    call, put = 0, 0
    checked = 0
    for sym in DEFAULT_UNIVERSE[:40]:  # จำกัดเพื่อความเร็ว
        _, closes = await yf_chart(sym)
        if len(closes) < 50:
            continue
        checked += 1
        last = closes[-1]
        sm50 = sma(closes, 50) or last
        rsi = rsi14(closes) or 50
        if last > sm50 and rsi >= 55:
            call += 1
        elif last < sm50 and rsi <= 45:
            put += 1

    await update.message.reply_text(
        f"🔮 Signals (ชุดสแกน)\nStrong CALL: {call} | Strong PUT: {put}\n(ตรวจ {checked} ตัวจากชุดสแกน)"
    )


async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # สรุปตลาดด้วย ETF: SPY / QQQ / IWM
    bench = ["SPY", "QQQ", "IWM"]
    quotes = await yf_quote(bench)
    lines = ["📉 Outlook วันนี้:"]
    for sym in bench:
        q = quotes.get(sym, {})
        pct_day = q.get("regularMarketChangePercent")
        arrow = "↑" if (pct_day or 0) > 0 else "↓" if (pct_day or 0) < 0 else "→"
        lines.append(f"• {sym}: {arrow} ({fmt_pct(pct_day)})")
    # สรุปโทนเมื่อเทียบสัญญาณ
    await update.message.reply_text("\n".join(lines))


async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /picks                -> ใช้ DEFAULT_PICKS (หรือ ENV PICKS)
    /picks AAPL NVDA     -> รายชื่อเอง
    /picks auto          -> ให้บอทคัด Top 3 จาก universe
    """
    args = parse_symbols_from_args(context.args or [])
    if len(args) == 1 and args[0] == "AUTO":
        picks = await choose_dynamic_picks(3)
        if not picks:
            picks = DEFAULT_PICKS  # fallback
        header = "🧾 Picks (auto จากสแกน)"
    elif args:
        picks = args
        header = "🧾 Picks (ผู้ใช้กำหนด)"
    else:
        picks = DEFAULT_PICKS
        header = "🧾 Picks (ค่าเริ่มต้น)"

    quotes = await yf_quote(picks)
    lines = [header]

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

        _, closes = await yf_chart(sym)
        if len(closes) < 50:
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

    if not args:
        lines.append("\n💡 `/picks auto` ให้บอทคัดเอง, หรือ `/picks AAPL NVDA TSLA` ระบุเอง")

    await update.message.reply_text("\n".join(lines))


# -----------------------------
# Healthcheck (aiohttp)
# -----------------------------
async def health(_request: web.Request) -> web.Response:
    return web.Response(
        text=f"✅ Bot is running – {datetime.utcnow().isoformat()}Z",
        content_type="text/plain"
    )


# -----------------------------
# Runner
# -----------------------------
async def run_polling(app: Application) -> None:
    log.info("Starting Telegram bot (polling mode)")
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    # keep running
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()


async def run_webhook(app: Application) -> None:
    log.info("Starting Flask + Telegram (webhook mode)")
    if not PUBLIC_URL:
        raise RuntimeError("PUBLIC_URL env required in webhook mode")

    # aiohttp app
    web_app = web.Application()
    web_app.router.add_get("/", health)

    # hook PTB to aiohttp
    await app.initialize()
    await app.start()

    # build webhook URL
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    await app.bot.set_webhook(url=webhook_url, drop_pending_updates=True)

    # mount telegram webhook handler
    app.webhook_app = web_app
    app.webhook_path = WEBHOOK_PATH
    await app.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP server started on 0.0.0.0:%s", PORT)

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.stop()
        await runner.cleanup()


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # allow concurrency
        .build()
    )

    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(CommandHandler("movers", cmd_movers))
    application.add_handler(CommandHandler("signals", cmd_signals))
    application.add_handler(CommandHandler("outlook", cmd_outlook))
    application.add_handler(CommandHandler("picks", cmd_picks))

    return application


async def main_async() -> None:
    application = build_application()
    if MODE == "polling":
        await run_polling(application)
    else:
        await run_webhook(application)


def main() -> None:
    try:
        asyncio.get_event_loop().run_until_complete(main_async())
    except RuntimeError:
        # already running loop (Render บางช่วง)
        loop = asyncio.get_event_loop()
        loop.create_task(main_async())
        loop.run_forever()
    finally:
        # close http session
        if _http_session and not _http_session.closed:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(_http_session.close())


if __name__ == "__main__":
    main()
