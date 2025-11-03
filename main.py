# main.py (v3.4 — Fixed Event Loop Error + Polygon + /fundamentals)
import os
import asyncio
import random
from datetime import datetime, timezone, timedelta
from aiohttp import web, ClientSession
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN or not POLYGON_API_KEY:
    raise RuntimeError("❌ Missing BOT_TOKEN or POLYGON_API_KEY")

POLY_BASE = "https://api.polygon.io"

def fmt_pct(x: float) -> str:
    sign = "↑" if x >= 0 else "↓"
    return f"{sign} {abs(x):.2f}%"

def fmt_number(x: float) -> str:
    if x >= 1e12:
        return f"{x/1e12:.2f}T"
    if x >= 1e9:
        return f"{x/1e9:.2f}B"
    if x >= 1e6:
        return f"{x/1e6:.2f}M"
    if x >= 1e3:
        return f"{x/1e3:.2f}K"
    return f"{x:.0f}"

def safe_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

# ========== POLYGON CLIENT ==========
class PolygonClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: ClientSession | None = None

    async def ensure(self):
        if self.session is None or self.session.closed:
            self.session = ClientSession()

    async def get(self, endpoint: str, params=None):
        await self.ensure()
        params = (params or {}) | {"apiKey": self.api_key}
        async with self.session.get(f"{POLY_BASE}{endpoint}", params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def gainers(self):
        data = await self.get("/v2/snapshot/locale/us/markets/stocks/gainers")
        return data.get("tickers", [])

    async def losers(self):
        data = await self.get("/v2/snapshot/locale/us/markets/stocks/losers")
        return data.get("tickers", [])

    async def daily_bars(self, ticker: str, days=30):
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days)
        data = await self.get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            {"adjusted": "true", "sort": "asc", "limit": 5000},
        )
        return data.get("results", [])

    async def ticker_details(self, ticker: str):
        data = await self.get(f"/v3/reference/tickers/{ticker}")
        return data.get("results", {}) if isinstance(data, dict) else {}

    async def snapshot_ticker(self, ticker: str):
        data = await self.get("/v2/snapshot/locale/us/markets/stocks/tickers", {"tickers": ticker})
        tickers = data.get("tickers", [])
        return tickers[0] if tickers else {}

poly = PolygonClient(POLYGON_API_KEY)

# ========== COMMANDS ==========
async def cmd_ping(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_movers(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        g = await poly.gainers()
        l = await poly.losers()
        msg = (
            "📊 *Movers (Polygon)*\n"
            + "↑ " + ", ".join(f"{x['ticker']} ({fmt_pct(x.get('todaysChangePerc',0))})" for x in g[:3])
            + "\n↓ "
            + ", ".join(f"{x['ticker']} ({fmt_pct(x.get('todaysChangePerc',0))})" for x in l[:3])
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_outlook(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        benchmarks = ["SPY", "QQQ", "IWM"]
        lines, score = [], 0
        for t in benchmarks:
            bars = await poly.daily_bars(t, days=3)
            if len(bars) < 2:
                lines.append(f"• {t}: —")
                continue
            chg = (bars[-1]["c"] - bars[-2]["c"]) / bars[-2]["c"] * 100
            lines.append(f"• {t}: {fmt_pct(chg)}")
            score += 1 if chg >= 0 else -1
        summary = "ขาขึ้นอ่อนๆ" if score > 0 else ("ขาลงอ่อนๆ" if score < 0 else "โมเมนตัมกลางๆ")
        msg = "📈 *Outlook วันนี้:*\n" + "\n".join(lines) + f"\nสรุป: {summary}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_signals(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        g = await poly.gainers()
        l = await poly.losers()
        strong_call = len([x for x in g if x.get("todaysChangePerc", 0) > 2])
        strong_put = len([x for x in l if x.get("todaysChangePerc", 0) < -2])
        msg = f"🔮 *Signals (Polygon)*\nCALL: {strong_call} | PUT: {strong_put}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_picks(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        gainers = await poly.gainers()
        picks = [x for x in gainers if x.get("todaysChangePerc", 0) > 2 and x.get("lastTrade", {}).get("p", 0) > 5][:3]
        if not picks:
            await update.message.reply_text("⚠️ ไม่มีหุ้นที่ผ่านเกณฑ์ตอนนี้")
            return
        lines = [
            f"• {p['ticker']} ({fmt_pct(p.get('todaysChangePerc',0))}) Vol: {fmt_number(p.get('volume',0))}"
            for p in picks
        ]
        await update.message.reply_text("🧾 *Picks*\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_fundamentals(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.strip().split()
        if len(parts) < 2:
            await update.message.reply_text("พิมพ์แบบนี้นะครับ: `/fundamentals TSLA`", parse_mode=ParseMode.MARKDOWN)
            return
        ticker = parts[1].upper()
        snap = await poly.snapshot_ticker(ticker)
        info = await poly.ticker_details(ticker)
        bars = await poly.daily_bars(ticker, days=365)

        chg = snap.get("todaysChangePerc", 0)
        price = safe_get(snap, "day", "c", default=None)
        vol = safe_get(snap, "day", "v", default=None)
        name = info.get("name", "")
        market_cap = info.get("market_cap")
        highs = [b["h"] for b in bars] if bars else []
        lows = [b["l"] for b in bars] if bars else []
        hi52, lo52 = max(highs) if highs else None, min(lows) if lows else None

        msg = f"📌 *{ticker}* {name}\n"
        msg += f"• Price: {price} ({fmt_pct(chg)})\n"
        if market_cap:
            msg += f"• Market Cap: {fmt_number(float(market_cap))}\n"
        if vol:
            msg += f"• Volume: {fmt_number(vol)}\n"
        if hi52 and lo52:
            msg += f"• 52W Range: {lo52:.2f} – {hi52:.2f}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

# ========== HEALTH & STARTUP ==========
async def health(_request):
    return web.Response(text=f"✅ Bot running {datetime.now(timezone.utc).isoformat()}")

async def run_bot():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
    except OSError as e:
        print(f"⚠️ Port {PORT} busy: {e}")

    tg = Application.builder().token(BOT_TOKEN).build()
    tg.add_handler(CommandHandler("ping", cmd_ping))
    tg.add_handler(CommandHandler("movers", cmd_movers))
    tg.add_handler(CommandHandler("signals", cmd_signals))
    tg.add_handler(CommandHandler("outlook", cmd_outlook))
    tg.add_handler(CommandHandler("picks", cmd_picks))
    tg.add_handler(CommandHandler("fundamentals", cmd_fundamentals))

    print("🚀 Telegram bot started successfully!")
    await tg.run_polling(close_loop=False)

# === main entry ===
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(run_bot())
    else:
        loop.run_until_complete(run_bot())
