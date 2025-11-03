# main.py — v3.6 Render-Safe (Polygon + Fundamentals + no loop conflict)
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
POLY_BASE = "https://api.polygon.io"

if not BOT_TOKEN or not POLYGON_API_KEY:
    raise RuntimeError("❌ Missing BOT_TOKEN or POLYGON_API_KEY")

# ===== UTILITIES =====
def fmt_pct(x: float) -> str:
    return f"{'↑' if x >= 0 else '↓'} {abs(x):.2f}%"

def fmt_number(x: float) -> str:
    if x >= 1e12: return f"{x/1e12:.1f}T"
    if x >= 1e9: return f"{x/1e9:.1f}B"
    if x >= 1e6: return f"{x/1e6:.1f}M"
    if x >= 1e3: return f"{x/1e3:.1f}K"
    return f"{x:.0f}"

def safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

# ===== POLYGON CLIENT =====
class PolygonClient:
    def __init__(self, key: str):
        self.key = key
        self.session: ClientSession | None = None

    async def ensure(self):
        if self.session is None or self.session.closed:
            self.session = ClientSession()

    async def get(self, endpoint: str, params=None):
        await self.ensure()
        params = (params or {}) | {"apiKey": self.key}
        async with self.session.get(f"{POLY_BASE}{endpoint}", params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def gainers(self):
        return (await self.get("/v2/snapshot/locale/us/markets/stocks/gainers")).get("tickers", [])

    async def losers(self):
        return (await self.get("/v2/snapshot/locale/us/markets/stocks/losers")).get("tickers", [])

    async def daily_bars(self, ticker, days=30):
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days)
        res = await self.get(f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
                             {"adjusted": "true", "sort": "asc", "limit": 5000})
        return res.get("results", [])

    async def snapshot_ticker(self, ticker):
        res = await self.get("/v2/snapshot/locale/us/markets/stocks/tickers", {"tickers": ticker})
        return res.get("tickers", [])[0] if res.get("tickers") else {}

    async def ticker_details(self, ticker):
        res = await self.get(f"/v3/reference/tickers/{ticker}")
        return res.get("results", {}) if isinstance(res, dict) else {}

poly = PolygonClient(POLYGON_API_KEY)

# ===== COMMANDS =====
async def cmd_ping(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_movers(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        g, l = await poly.gainers(), await poly.losers()
        msg = (
            "📊 *Movers (Polygon)*\n"
            + "↑ " + ", ".join(f"{x['ticker']} ({fmt_pct(x.get('todaysChangePerc',0))})" for x in g[:3])
            + "\n↓ " + ", ".join(f"{x['ticker']} ({fmt_pct(x.get('todaysChangePerc',0))})" for x in l[:3])
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_outlook(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        tickers = ["SPY", "QQQ", "IWM"]
        lines, score = [], 0
        for t in tickers:
            bars = await poly.daily_bars(t, days=3)
            if len(bars) < 2:
                lines.append(f"• {t}: —")
                continue
            chg = (bars[-1]["c"] - bars[-2]["c"]) / bars[-2]["c"] * 100
            lines.append(f"• {t}: {fmt_pct(chg)}")
            score += 1 if chg > 0 else -1
        summary = "ขาขึ้นอ่อนๆ" if score > 0 else ("ขาลงอ่อนๆ" if score < 0 else "โมเมนตัมกลางๆ")
        msg = "📈 *Outlook วันนี้:*\n" + "\n".join(lines) + f"\nสรุป: {summary}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_signals(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        g, l = await poly.gainers(), await poly.losers()
        call = len([x for x in g if x.get("todaysChangePerc", 0) > 2])
        put = len([x for x in l if x.get("todaysChangePerc", 0) < -2])
        await update.message.reply_text(
            f"🔮 *Signals*\nCALL: {call} | PUT: {put}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_picks(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        g = await poly.gainers()
        picks = [x for x in g if x.get("todaysChangePerc", 0) > 2 and x.get("lastTrade", {}).get("p", 0) > 5][:3]
        if not picks:
            await update.message.reply_text("⚠️ ไม่มีหุ้นที่เข้าเกณฑ์")
            return
        msg = "🧾 *Picks*\n" + "\n".join(
            f"• {p['ticker']} ({fmt_pct(p.get('todaysChangePerc',0))}) Vol: {fmt_number(p.get('volume',0))}"
            for p in picks
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_fundamentals(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.split()
        if len(parts) < 2:
            await update.message.reply_text("พิมพ์แบบนี้ครับ `/fundamentals TSLA`", parse_mode=ParseMode.MARKDOWN)
            return
        t = parts[1].upper()
        snap, info, bars = await poly.snapshot_ticker(t), await poly.ticker_details(t), await poly.daily_bars(t, 365)
        chg = snap.get("todaysChangePerc", 0)
        price = safe_get(snap, "day", "c", default=None)
        vol = safe_get(snap, "day", "v", default=None)
        name = info.get("name", "")
        cap = info.get("market_cap")
        hi = max(b["h"] for b in bars) if bars else None
        lo = min(b["l"] for b in bars) if bars else None

        msg = f"📌 *{t}* {name}\n• Price: {price} ({fmt_pct(chg)})"
        if cap:
            msg += f"\n• Market Cap: {fmt_number(float(cap))}"
        if vol:
            msg += f"\n• Volume: {fmt_number(vol)}"
        if hi and lo:
            msg += f"\n• 52W Range: {lo:.2f} – {hi:.2f}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

# ===== SERVER + BOT =====
async def health(_):
    return web.Response(text=f"✅ Alive {datetime.now(timezone.utc).isoformat()}")

async def main_async():
    # 1️⃣ healthcheck server
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    # 2️⃣ telegram bot
    tg = Application.builder().token(BOT_TOKEN).build()
    tg.add_handler(CommandHandler("ping", cmd_ping))
    tg.add_handler(CommandHandler("movers", cmd_movers))
    tg.add_handler(CommandHandler("signals", cmd_signals))
    tg.add_handler(CommandHandler("outlook", cmd_outlook))
    tg.add_handler(CommandHandler("picks", cmd_picks))
    tg.add_handler(CommandHandler("fundamentals", cmd_fundamentals))

    print("🚀 Bot started successfully (Render-Safe mode)")
    await tg.run_polling()

# ===== ENTRY =====
if __name__ == "__main__":
    asyncio.run(main_async())
