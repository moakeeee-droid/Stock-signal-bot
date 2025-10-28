# main.py
import os
import asyncio
import logging
from datetime import datetime, timezone
from random import sample, shuffle

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# ตั้งค่าทั่วไป
# =========================
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
MODE        = os.environ.get("MODE", "webhook").lower().strip()
PUBLIC_URL  = os.environ.get("PUBLIC_URL", "").rstrip("/")  # ใช้ใน webhook
PORT        = int(os.environ.get("PORT", "10000"))

# พอร์ตสำหรับ healthcheck (กันชนจากพอร์ตหลัก)
HEALTH_PORT = PORT + 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("stock-signal-bot")


# =========================
# ส่วนข้อมูลจำลอง (เดโม่)
# =========================
INDEX_OUTLOOK = {
    "SPY": "— (→)",
    "QQQ": "— (→)",
    "IWM": "— (→)",
}

GAINERS = ["TSLA (+5.59%)", "GOOGL (+3.00%)", "INTC (+2.76%)"]
LOSERS  = ["ORCL (-0.93%)", "XOM (-0.08%)", "IWM (+0.18%)"]

UNIVERSE = [
    "AAPL","MSFT","NVDA","TSLA","META","AMZN","GOOGL","AMD","INTC","ASML",
    "CRM","ADBE","NFLX","MU","AVGO","COST","V","MA","PYPL","SHOP",
    "BYND","KUKE","GSIT","PLTR","SNOW","NET","DDOG","ZS","CRWD","MDB",
]

def pick_symbols(n=3) -> list[str]:
    # เลือกแบบสุ่ม ถ้ากังวลซ้ำ ให้ shuffle ก่อนทุกครั้ง
    pool = UNIVERSE[:]
    shuffle(pool)
    return sample(pool, k=n)


# =========================
# Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "สวัสดีครับ 👋\n"
        "คำสั่งที่ใช้ได้:\n"
        "/ping – ทดสอบบอท\n"
        "/signals – สรุปสัญญาณรวม\n"
        "/outlook – มุมมองตลาด\n"
        "/movers – หุ้นเด่นขึ้น/ลง\n"
        "/picks – หุ้นน่าสนใจ (สุ่ม)"
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong 🏓")

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # เดโม่: สุ่มจำนวน CALL/PUT
    strong_call = 15
    strong_put  = 1
    total_scanned = 20
    text = (
        "🔮 Signals (ชุดสแกน)\n"
        f"Strong CALL: {strong_call} | Strong PUT: {strong_put}\n"
        f"(ตรวจจ {total_scanned} ตัวจากชุดสแกน)\n"
    )
    await update.message.reply_text(text)

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "📉 Outlook วันนี้:\n"
        f"• SPY: {INDEX_OUTLOOK.get('SPY','–')}\n"
        f"• QQQ: {INDEX_OUTLOOK.get('QQQ','–')}\n"
        f"• IWM: {INDEX_OUTLOOK.get('IWM','–')}\n"
        "สรุปโมเมนตัม: ขาขึ้นอ่อน ๆ"
    )
    await update.message.reply_text(txt)

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "📊 Movers (จากชุดสแกน)\n"
        f"↑ Gainers: {', '.join(GAINERS)}\n"
        f"↓ Losers: {', '.join(LOSERS)}"
    )
    await update.message.reply_text(txt)

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ กำลังดึงข้อมูลหุ้น…")
    # เดโม่: สุ่ม 3 ตัว เพื่อไม่ให้ซ้ำเดิม
    syms = pick_symbols(3)
    # คุณสามารถเติม logic ไปเรียก API จริง แล้ว format รายละเอียดได้ที่นี่
    details = [f"• {s}: ข้อมูลพร้อม ✅" for s in syms]
    txt = "🧾 Picks (รายละเอียด)\n" + "\n".join(details)
    await update.message.reply_text(txt)


# =========================
# Health server (aiohttp)
# =========================
async def health_handler(request: web.Request) -> web.Response:
    return web.Response(
        text=f"✅ Bot is running – {datetime.now(timezone.utc).isoformat()}",
        content_type="text/plain",
    )

async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()
    log.info(f"Healthcheck started on :{HEALTH_PORT}")
    return runner


# =========================
# Run modes
# =========================
async def run_webhook(application: Application) -> None:
    """
    รันแบบ webhook:
    - set/delete webhook ให้เรียบร้อย
    - เริ่ม telegram updater + health server แยกพอร์ต
    - loop ค้างไว้เพื่อกัน Render ปิดโปรเซส
    """
    if not PUBLIC_URL:
        raise RuntimeError("PUBLIC_URL is required in webhook mode.")

    # ล้าง webhook เดิม + ตัดคิวเก่า
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    webhook_url = f"{PUBLIC_URL}/webhook"
    await application.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    log.info(f"Webhook set: {webhook_url}")

    # init / start app
    await application.initialize()
    await application.start()

    # start telegram webhook listener (บน PORT หลัก)
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )
    log.info(f"Telegram webhook listener on :{PORT}")

    # health server แยกพอร์ต
    health_runner = await start_health_server()

    # keep alive
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        # ปิดให้เรียบร้อย
        await application.updater.stop()
        await application.stop()
        await health_runner.cleanup()
        log.info("Webhook stopped cleanly.")

async def run_polling(application: Application) -> None:
    """
    รันแบบ polling:
    - delete webhook ก่อน (กัน conflict)
    - start polling ด้วย updater
    """
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    await application.initialize()
    await application.start()

    await application.updater.start_polling(
        poll_interval=1.5,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    log.info("Polling started.")

    # health server ให้ด้วย (สะดวกเช็คสถานะ)
    health_runner = await start_health_server()

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await health_runner.cleanup()
        log.info("Polling stopped cleanly.")


# =========================
# สร้างแอป + ผูกคำสั่ง
# =========================
def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("ping",    cmd_ping))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("movers",  cmd_movers))
    app.add_handler(CommandHandler("picks",   cmd_picks))

    return app


# =========================
# Entry point
# =========================
async def main_async():
    log.info(f"Starting stock-signal-bot | MODE={MODE} | PORT={PORT}")
    application = build_application()

    if MODE == "webhook":
        await run_webhook(application)
    else:
        # ค่าอื่นๆ ทั้งหมด จะถือเป็น polling
        await run_polling(application)

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Shutting down...")

if __name__ == "__main__":
    main()    url = f"{Y_BASE}/v8/finance/chart/{symbol}?{params}"
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
