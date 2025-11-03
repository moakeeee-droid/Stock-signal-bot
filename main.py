import os
import asyncio
import random
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

from aiohttp import web, ClientSession, ClientTimeout
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")
PORT = int(os.environ.get("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is required")
if not POLYGON_API_KEY:
    raise RuntimeError("ENV POLYGON_API_KEY is required")

POLY_BASE = "https://api.polygon.io"
SNAP_GAINERS = f"{POLY_BASE}/v2/snapshot/locale/us/markets/stocks/gainers"
SNAP_LOSERS = f"{POLY_BASE}/v2/snapshot/locale/us/markets/stocks/losers"
SNAP_SINGLE = f"{POLY_BASE}/v2/snapshot/locale/us/markets/stocks/tickers"
AGG_RANGE = f"{POLY_BASE}/v2/aggs/ticker"  # /{ticker}/range/{mult}/{timespan}/{from}/{to}


# =========================================================
# HELPERS
# =========================================================
def fmt_pct(x: float) -> str:
    sign = "↑" if x >= 0 else "↓"
    return f"{sign} {abs(x):.2f}%"

def rsi_from_closes(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[-(i)] - closes[-(i+1)]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(-diff)
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr_from_ohlc(hlc: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(hlc) < period + 1:
        return None
    trs = []
    prev_close = hlc[-(period+1)]["c"]
    for i in range(period):
        bar = hlc[-(i+1)]
        high, low, close = bar["h"], bar["l"], bar["c"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    return sum(trs) / period if trs else None


# =========================================================
# POLYGON CLIENT
# =========================================================
class PolygonClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[ClientSession] = None

    async def ensure(self):
        if self.session is None or self.session.closed:
            self.session = ClientSession(timeout=ClientTimeout(total=20))

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, url: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        await self.ensure()
        params = params.copy() if params else {}
        params["apiKey"] = self.api_key
        async with self.session.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def gainers(self) -> List[Dict[str, Any]]:
        data = await self._get(SNAP_GAINERS)
        return data.get("tickers", []) or data.get("results", []) or []

    async def losers(self) -> List[Dict[str, Any]]:
        data = await self._get(SNAP_LOSERS)
        return data.get("tickers", []) or data.get("results", []) or []

    async def daily_bars(self, ticker: str, days: int = 60) -> List[Dict[str, Any]]:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days + 10)
        url = f"{AGG_RANGE}/{ticker}/range/1/day/{start}/{end}"
        data = await self._get(url, {"adjusted": "true", "sort": "asc", "limit": 5000})
        results = data.get("results", []) or []
        bars = []
        for r in results:
            bars.append({
                "o": float(r["o"]),
                "h": float(r["h"]),
                "l": float(r["l"]),
                "c": float(r["c"]),
                "v": float(r.get("v", 0)),
                "t": int(r["t"])
            })
        return bars


# =========================================================
# BOT COMMANDS
# =========================================================
poly = PolygonClient(POLYGON_API_KEY)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        gainers = await poly.gainers()
        losers = await poly.losers()

        top_g = [
            ((x.get("ticker") or x.get("T")),
             float(x.get("todaysChangePerc", x.get("todays_change_percent", 0.0))))
            for x in gainers
        ]
        top_l = [
            ((x.get("ticker") or x.get("T")),
             float(x.get("todaysChangePerc", x.get("todays_change_percent", 0.0))))
            for x in losers
        ]

        random.shuffle(top_g)
        random.shuffle(top_l)
        g_show = top_g[:3]
        l_show = top_l[:3]

        g_txt = ", ".join([f"{t} ({fmt_pct(p)})" for t, p in g_show]) if g_show else "—"
        l_txt = ", ".join([f"{t} ({fmt_pct(p)})" for t, p in l_show]) if l_show else "—"

        msg = (
            "📊 *Movers (จากชุดสแกน)*\n"
            f"↑ Gainers: {g_txt}\n"
            f"↓ Losers: {l_txt}"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"เกิดข้อผิดพลาดดึง movers: {e}")

async def _compute_signal_for(ticker: str) -> Optional[Dict[str, Any]]:
    bars = await poly.daily_bars(ticker, days=120)
    if len(bars) < 20:
        return None

    closes = [b["c"] for b in bars]
    rsi = rsi_from_closes(closes, period=14)
    hlc = [{"h": b["h"], "l": b["l"], "c": b["c"]} for b in bars]
    atr = atr_from_ohlc(hlc, period=14)
    last = bars[-1]
    prev = bars[-2]
    chg = (last["c"] - prev["c"]) / prev["c"] * 100.0
    return {"ticker": ticker, "close": last["c"], "chg": chg, "rsi": rsi, "atr": atr}

async def _pick_universe() -> List[str]:
    g = await poly.gainers()
    l = await poly.losers()
    gg = [x.get("ticker") or x.get("T") for x in g if x.get("ticker") or x.get("T")]
    ll = [x.get("ticker") or x.get("T") for x in l if x.get("ticker") or x.get("T")]
    random.shuffle(gg)
    random.shuffle(ll)
    universe = gg[:10] + ll[:10]
    random.shuffle(universe)
    return list(dict.fromkeys(universe))

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        universe = await _pick_universe()
        results = []
        sem = asyncio.Semaphore(6)
        async def worker(sym):
            async with sem:
                sig = await _compute_signal_for(sym)
                if sig:
                    results.append(sig)
        await asyncio.gather(*[worker(t) for t in universe])
        strong_call = [r for r in results if (r["rsi"] and r["rsi"] > 60 and r["chg"] > 0.5)]
        strong_put = [r for r in results if (r["rsi"] and r["rsi"] < 40 and r["chg"] < -0.5)]
        msg = (
            "🔮 *Signals (ชุดสแกน)*\n"
            f"Strong CALL: {len(strong_call)} | Strong PUT: {len(strong_put)}\n"
            f"(ตรวจกับ {len(results)} ตัวจากชุดสแกน)"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"เกิดข้อผิดพลาดคำนวณ signals: {e}")

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bench = ["SPY", "QQQ", "IWM"]
    info = []
    try:
        for t in bench:
            bars = await poly.daily_bars(t, days=3)
            if len(bars) < 2:
                info.append((t, "—"))
                continue
            chg = (bars[-1]["c"] - bars[-2]["c"]) / bars[-2]["c"] * 100.0
            info.append((t, fmt_pct(chg)))
        lines = [f"• {t}: {p}" for t, p in info]
        summary = "ขาขึ้นอ่อน ๆ" if sum([(1 if "↑" in p else -1) for _, p in info]) > 0 else "โมเมนตัมกลางๆ"
        msg = "📈 *Outlook วันนี้:*\n" + "\n".join(lines) + f"\nสรุปโมเมนตัม: {summary}"
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"เกิดข้อผิดพลาดคำนวณ outlook: {e}")

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        universe = await _pick_universe()
        candidates: List[Dict[str, Any]] = []
        sem = asyncio.Semaphore(6)
        async def worker(sym):
            async with sem:
                sig = await _compute_signal_for(sym)
                if not sig or sig["rsi"] is None:
                    return
                price = sig["close"]
                atr = sig["atr"] or 0.0
                atr_pct = (atr / price * 100.0) if price else 0.0
                if 5 <= price <= 200 and 45 <= sig["rsi"] <= 65 and 0.5 <= atr_pct <= 5.0:
                    candidates.append(sig)
        await asyncio.gather(*[worker(t) for t in universe])
        random.shuffle(candidates)
        picks = candidates[:3] if candidates else []
        if not picks:
            await update.message.reply_text("⚠️ Picks: ข้อมูลไม่พร้อม/ไม่เข้าเงื่อนไข ลองใหม่อีกครั้งภายหลัง")
            return
        lines = [f"• {p['ticker']}: close {p['close']:.2f} | RSI {p['rsi']:.0f} | Δ {p['chg']:.2f}%" for p in picks]
        msg = "🧾 *Picks (รายละเอียด)*\n" + "\n".join(lines)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"เกิดข้อผิดพลาดคำนวณ picks: {e}")


# =========================================================
# HEALTHCHECK + MAIN LOOP
# =========================================================
async def handle_health(request):
    return web.Response(text=f"✅ Bot is running — {datetime.utcnow().isoformat()}")

async def start_services():
    app = web.Application()
    app.add_routes([web.get("/", handle_health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("ping", cmd_ping))
    application.add_handler(CommandHandler("movers", cmd_movers))
    application.add_handler(CommandHandler("signals", cmd_signals))
    application.add_handler(CommandHandler("outlook", cmd_outlook))
    application.add_handler(CommandHandler("picks", cmd_picks))

    await application.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)

async def on_shutdown():
    await poly.close()

def main():
    try:
        asyncio.get_event_loop().run_until_complete(start_services())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        loop.create_task(start_services())
        loop.run_forever()

if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            asyncio.get_event_loop().run_until_complete(on_shutdown())
        except Exception:
            pass
