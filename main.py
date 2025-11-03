# main.py  (v3.3 — Polygon + /fundamentals)
import os
import asyncio
import random
from datetime import datetime, timezone, timedelta
from aiohttp import web, ClientSession
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN or not POLYGON_API_KEY:
    raise RuntimeError("❌ Missing BOT_TOKEN or POLYGON_API_KEY")

POLY_BASE = "https://api.polygon.io"

# ====== HELPERS ======
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

# ====== POLYGON CLIENT ======
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

    # --- Snapshots (top movers) ---
    async def gainers(self):
        data = await self.get("/v2/snapshot/locale/us/markets/stocks/gainers")
        return data.get("tickers", [])

    async def losers(self):
        data = await self.get("/v2/snapshot/locale/us/markets/stocks/losers")
        return data.get("tickers", [])

    # --- Daily aggregates ---
    async def daily_bars(self, ticker: str, days=30, asc=True):
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days)
        data = await self.get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            {"adjusted": "true", "sort": "asc" if asc else "desc", "limit": 5000},
        )
        return data.get("results", [])

    # --- Ticker details (reference) ---
    async def ticker_details(self, ticker: str):
        # v3 reference returns { results: {...} }
        data = await self.get(f"/v3/reference/tickers/{ticker}")
        return data.get("results", {}) if isinstance(data, dict) else {}

    # --- Snapshot for a single ticker (price/volume today) ---
    async def snapshot_ticker(self, ticker: str):
        # Use "all tickers" snapshot then pick (works under free tier)
        data = await self.get("/v2/snapshot/locale/us/markets/stocks/tickers", {"tickers": ticker})
        tickers = data.get("tickers", [])
        return tickers[0] if tickers else {}

poly = PolygonClient(POLYGON_API_KEY)

# ====== COMMANDS ======
async def cmd_ping(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_movers(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        gainers = await poly.gainers()
        losers = await poly.losers()
        top_g = [f"{x['ticker']} ({fmt_pct(x.get('todaysChangePerc', 0))})" for x in gainers[:3]]
        top_l = [f"{x['ticker']} ({fmt_pct(x.get('todaysChangePerc', 0))})" for x in losers[:3]]
        msg = "📊 *Movers (Polygon)*\n" + \
              "↑ " + ", ".join(top_g) + "\n" + \
              "↓ " + ", ".join(top_l)
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
                lines.append(f"• {t}: — (ไม่มีข้อมูล)")
                continue
            chg = (bars[-1]["c"] - bars[-2]["c"]) / bars[-2]["c"] * 100
            lines.append(f"• {t}: {fmt_pct(chg)}")
            score += 1 if chg >= 0 else -1
        summary = "ขาขึ้นอ่อนๆ" if score > 0 else ("ขาลงอ่อนๆ" if score < 0 else "โมเมนตัมกลางๆ")
        msg = "📈 *Outlook วันนี้:*\n" + "\n".join(lines) + f"\nสรุปโมเมนตัม: {summary}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_signals(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        gainers = await poly.gainers()
        losers = await poly.losers()
        strong_call = len([x for x in gainers if x.get("todaysChangePerc", 0) > 2])
        strong_put = len([x for x in losers if x.get("todaysChangePerc", 0) < -2])
        msg = (
            "🔮 *Signals (Polygon)*\n"
            f"Strong CALL: {strong_call} | Strong PUT: {strong_put}\n"
            f"(จาก snapshot ล่าสุด)"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_picks(update: Update, _: ContextTypes.DEFAULT_TYPE):
    try:
        gainers = await poly.gainers()
        random.shuffle(gainers)
        picks = gainers[:3]
        if not picks:
            await update.message.reply_text("⚠️ ข้อมูลไม่พร้อม")
            return
        lines = [
            f"• {p['ticker']}  ({fmt_pct(p.get('todaysChangePerc', 0))})  "
            f"Vol: {fmt_number(p.get('volume', 0))}"
            for p in picks
        ]
        msg = "🧾 *Picks (Polygon)*\n" + "\n".join(lines)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def cmd_fundamentals(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """
    ใช้: /fundamentals TSLA
    แสดงราคา/ปริมาณวันนี้ + 52W high/low + Avg Vol + Market Cap (ถ้ามี) + ข้อมูลบริษัท
    """
    try:
        parts = (update.message.text or "").strip().split()
        if len(parts) < 2:
            await update.message.reply_text("พิมพ์แบบนี้นะครับ: `/fundamentals TSLA`", parse_mode=ParseMode.MARKDOWN)
            return
        ticker = parts[1].upper()

        # ข้อมูลวันนี้
        snap = await poly.snapshot_ticker(ticker)
        todays_change_pct = snap.get("todaysChangePerc", 0.0)
        day_c = safe_get(snap, "day", "c", default=None)
        day_o = safe_get(snap, "day", "o", default=None)
        day_h = safe_get(snap, "day", "h", default=None)
        day_l = safe_get(snap, "day", "l", default=None)
        day_v = safe_get(snap, "day", "v", default=None)

        # ย้อนหลัง 365 วัน เพื่อ 52W hi/lo + Avg Volume (20 วันล่าสุด)
        bars_1y = await poly.daily_bars(ticker, days=370, asc=True)
        if bars_1y:
            highs = [b["h"] for b in bars_1y]
            lows = [b["l"] for b in bars_1y]
            hi_52w = max(highs)
            lo_52w = min(lows)
        else:
            hi_52w = lo_52w = None

        last20 = bars_1y[-20:] if len(bars_1y) >= 1 else []
        avg_vol_20 = sum(b.get("v", 0) for b in last20) / len(last20) if last20 else None

        # รายละเอียดบริษัท (ถ้ามี)
        info = await poly.ticker_details(ticker)
        comp_name = info.get("name") or info.get("description") or ""
        primary_exchange = info.get("primary_exchange") or info.get("primary_exchange_symbol") or ""
        market_cap = info.get("market_cap")  # บางบัญชี/สัญลักษณ์อาจไม่มี

        # สร้างข้อความ
        lines = [f"📌 *{ticker}*  {comp_name}".strip()]
        sub = []
        if primary_exchange:
            sub.append(primary_exchange)
        if market_cap:
            sub.append(f"MC: {fmt_number(float(market_cap))}")
        if sub:
            lines.append(" · " + " | ".join(sub))

        # ราคา/ปริมาณวันนี้
        if day_c is not None:
            lines.append(f"• Today O/H/L/C: {day_o} / {day_h} / {day_l} / {day_c}")
        if day_v is not None:
            lines.append(f"• Volume (today): {fmt_number(day_v)}")
        lines.append(f"• Change: {fmt_pct(float(todays_change_pct or 0))}")

        # 52W & AvgVol
        if hi_52w is not None and lo_52w is not None:
            lines.append(f"• 52W Range: {lo_52w:.2f} – {hi_52w:.2f}")
        if avg_vol_20 is not None:
            lines.append(f"• Avg Vol (20d): {fmt_number(avg_vol_20)}")

        msg = "\n".join(lines)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

# ====== HEALTHCHECK + MAIN ======
async def health(_request):
    return web.Response(text=f"✅ Bot live {datetime.now(timezone.utc).isoformat()}")

async def start_services():
    # healthcheck server
    app = web.Application()
    app.add_routes([web.get("/", health)])
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
    except OSError as e:
        print(f"⚠️ Port {PORT} already in use: {e}")

    # telegram bot (polling)
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(CommandHandler("movers", cmd_movers))
    application.add_handler(CommandHandler("signals", cmd_signals))
    application.add_handler(CommandHandler("outlook", cmd_outlook))
    application.add_handler(CommandHandler("picks", cmd_picks))
    application.add_handler(CommandHandler("fundamentals", cmd_fundamentals))

    print("🚀 Telegram bot started with Polygon API")
    await application.run_polling(close_loop=False)

def main():
    try:
        asyncio.run(start_services())
    except RuntimeError:
        # fallback when loop already running
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_services())

if __name__ == "__main__":
    main()
