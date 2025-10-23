# main.py
# Stock-signal-bot (Flask + python-telegram-bot v21)
# รองรับดึงราคาจริงจาก Finnhub หรือ Alpha Vantage และแสดงรายละเอียดหุ้นแบบเต็ม

import os
import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from typing import List, Tuple, Dict

import requests
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# =========================
# Flask (healthcheck)
# =========================
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify(ok=True, service="stock-signal-bot"), 200

# =========================
# Config from ENV
# =========================
TZ = ZoneInfo("Asia/Bangkok")
BOT_TOKEN = os.environ["BOT_TOKEN"]  # ต้องมี
DATA_PROVIDER = os.getenv("DATA_PROVIDER", "finnhub").lower()  # finnhub | alphavantage
DATA_API_KEY = os.getenv("DATA_API_KEY", "")  # finnhub token หรือ alpha-vantage key
WATCHLIST = [s.strip().upper() for s in os.getenv("WATCHLIST", "BYND,GSIT,KUKE,AAPL,MSFT,TSLA,NVDA").split(",") if s.strip()]
CHAT_ID = os.getenv("CHAT_ID")  # ถ้าจะให้ส่งสรุปรายวันอัตโนมัติ

# =========================
# Data models / helpers
# =========================
@dataclass
class StockRow:
    symbol: str
    price: float
    pct: float           # เปอร์เซ็นต์เปลี่ยนแปลง (เช่น 2.5 = +2.5%)
    near: str            # "near H" / "near L" / "-"
    vol: float           # ปริมาณดิบ (จะ format ตอนแสดง)
    high: float | None = None
    low: float | None = None

def fmt_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"

def fmt_vol(v: float | None) -> str:
    if v is None:
        return "-"
    n = float(v)
    for unit in ("", "K", "M", "B", "T"):
        if abs(n) < 1000.0:
            return f"{n:.2f}{unit}".rstrip("0").rstrip(".") + unit
        n /= 1000.0
    return f"{n:.2f}P"

def judge_near(price: float, high: float | None, low: float | None, thr: float = 0.01) -> str:
    # ใกล้ High/Low ถ้าอยู่ใน 1% (ปรับได้ด้วย thr)
    try:
        if high and high > 0 and price >= high * (1 - thr):
            return "near H"
        if low and low > 0 and price <= low * (1 + thr):
            return "near L"
    except Exception:
        pass
    return "-"

def row_line(row: StockRow, up: bool) -> str:
    dot = "🟢" if up else "🔴"
    return f"{dot} • {row.symbol} @{row.price:.2f} — pct {fmt_pct(row.pct)}, close {row.near}, Vol {fmt_vol(row.vol)}"

def chunk_lines(rows: List[StockRow], up: bool, limit: int = 10) -> str:
    return "\n".join(row_line(r, up) for r in rows[:limit])

def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

# =========================
# Data fetchers (providers)
# =========================
# หมายเหตุ:
# - Finnhub quote: https://finnhub.io/api/v1/quote?symbol=TSLA&token=YOUR_TOKEN
#   fields: c=current, pc=prevClose, h=high, l=low, v=volume
# - AlphaVantage global quote: https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=TSLA&apikey=YOUR_KEY
#   fields: 05. price, 03. high, 04. low, 06. volume, 10. change percent ("1.23%")
#
# โค้ดด้านล่างรวม rate-limit 429 แบบง่าย ๆ และ timeout

HTTP_TIMEOUT = 10
RETRY_STATUS = {429, 502, 503, 504}

def _get_json(url: str, params: Dict[str, str]) -> dict:
    for i in range(4):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code in RETRY_STATUS:
                backoff = 1.5 ** i
                log.warning("HTTP %s from %s, retry in %.1fs", r.status_code, url, backoff)
                asyncio.sleep(0)  # hint cooperatively
                import time; time.sleep(backoff)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == 3:
                log.exception("HTTP error: %s", e)
                return {}
            import time; time.sleep(1.5 * (i + 1))
    return {}

def fetch_finnhub(symbol: str) -> StockRow | None:
    if not DATA_API_KEY:
        log.warning("DATA_API_KEY is empty for Finnhub")
    url = "https://finnhub.io/api/v1/quote"
    data = _get_json(url, {"symbol": symbol, "token": DATA_API_KEY}) or {}
    try:
        c = float(data.get("c") or 0.0)
        pc = float(data.get("pc") or 0.0)
        h = float(data.get("h") or 0.0)
        l = float(data.get("l") or 0.0)
        v = float(data.get("v") or 0.0)
        pct = ((c - pc) / pc * 100.0) if pc else 0.0
        return StockRow(symbol=symbol, price=c, pct=pct, near=judge_near(c, h, l), vol=v, high=h, low=l)
    except Exception:
        return None

def fetch_alpha(symbol: str) -> StockRow | None:
    if not DATA_API_KEY:
        log.warning("DATA_API_KEY is empty for Alpha Vantage")
    url = "https://www.alphavantage.co/query"
    data = _get_json(url, {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": DATA_API_KEY}) or {}
    q = data.get("Global Quote") or {}
    try:
        price = float(q.get("05. price") or 0.0)
        high = float(q.get("03. high") or 0.0)
        low  = float(q.get("04. low") or 0.0)
        vol  = float(q.get("06. volume") or 0.0)
        pct_s = q.get("10. change percent") or "0%"
        pct = float(pct_s.strip().replace("%", "") or 0.0)
        return StockRow(symbol=symbol, price=price, pct=pct, near=judge_near(price, high, low), vol=vol, high=high, low=low)
    except Exception:
        return None

def fetch_row(symbol: str) -> StockRow | None:
    if DATA_PROVIDER == "alphavantage":
        return fetch_alpha(symbol)
    # ค่าเริ่มต้นเป็น finnhub
    return fetch_finnhub(symbol)

def fetch_rows(symbols: List[str]) -> List[StockRow]:
    out: List[StockRow] = []
    for s in symbols:
        r = fetch_row(s)
        if r:
            out.append(r)
    return out

def build_signals_from_rows(rows: List[StockRow]) -> Dict[str, List[StockRow]]:
    # จัดอันดับ: ขึ้น (pct desc) = CALL / ลง (pct asc) = PUT
    ups  = sorted([r for r in rows if r.pct >= 0], key=lambda x: x.pct, reverse=True)
    downs = sorted([r for r in rows if r.pct < 0], key=lambda x: x.pct)

    strong_call = ups[:5]
    watch_call  = ups[5:10]
    strong_put  = downs[:5]
    watch_put   = downs[5:10]

    return {
        "strong_call": strong_call,
        "watch_call": watch_call,
        "strong_put": strong_put,
        "watch_put": watch_put,
    }

def get_live_signals() -> Dict[str, List[StockRow]]:
    rows = fetch_rows(WATCHLIST)
    return build_signals_from_rows(rows)

def get_live_outlook() -> dict:
    sig = get_live_signals()
    return {
        "date": today_str(),
        "strong_call": len(sig["strong_call"]),
        "watch_call": len(sig["watch_call"]),
        "strong_put": len(sig["strong_put"]),
        "watch_put": len(sig["watch_put"]),
        "comment": "โมเมนตัมกลาง" if (len(sig["strong_call"]) and len(sig["strong_put"])) else ("เอียงบวก" if len(sig["strong_call"]) else "เอียงลบ"),
    }

def get_live_picks() -> List[StockRow]:
    # picks = top 3 ฝั่งขึ้น
    sig = get_live_signals()
    return sig["strong_call"][:3]

# =========================
# Telegram handlers
# =========================
HELP_TEXT = (
    "📊 คำสั่งที่ใช้ได้:\n"
    "/ping - ทดสอบบอท\n"
    "/signals - สัญญาณแบบละเอียด (CALL/PUT)\n"
    "/outlook - มุมมองตลาดวันนี้\n"
    "/picks - หุ้นน่าสนใจ\n"
    "/help - แสดงเมนูนี้"
)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # fetch แบบขนานใน thread เพื่อไม่บล็อก event loop
    sig = await asyncio.to_thread(get_live_signals)

    lines: List[str] = []
    lines.append("🔮 *Signals*")
    lines.append(f"Strong CALL: {len(sig['strong_call'])} | Strong PUT: {len(sig['strong_put'])}")
    lines.append("")

    if sig["strong_call"]:
        lines.append("🟢 *Strong CALL*")
        lines.append(chunk_lines(sig["strong_call"], up=True))
        lines.append("")
    if sig["watch_call"]:
        lines.append("🟢 *Watch CALL*")
        lines.append(chunk_lines(sig["watch_call"], up=True))
        lines.append("")
    if sig["strong_put"]:
        lines.append("🔴 *Strong PUT*")
        lines.append(chunk_lines(sig["strong_put"], up=False))
        lines.append("")
    if sig["watch_put"]:
        lines.append("🔴 *Watch PUT*")
        lines.append(chunk_lines(sig["watch_put"], up=False))

    await update.message.reply_markdown_v2("\n".join(lines) or "—")

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    o = await asyncio.to_thread(get_live_outlook)
    text = (
        f"📈 *Outlook* (ถึง {o['date']})\n"
        f"*Strong CALL*: {o['strong_call']} | *Watch CALL*: {o['watch_call']}\n"
        f"*Strong PUT*: {o['strong_put']} | *Watch PUT*: {o['watch_put']}\n"
        f"→ *โมเมนตัมรวม*: {o['comment']}\n\n"
        "พอร์ตสั้นวันเดียว: ตามน้ำกลุ่ม Strong, รอจังหวะใน Watch"
    )
    await update.message.reply_markdown_v2(text)

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(get_live_picks)
    body = "\n".join(f"• {r.symbol} @{r.price:.2f} — pct {fmt_pct(r.pct)}, {r.near}, Vol {fmt_vol(r.vol)}" for r in rows)
    await update.message.reply_text(f"📝 Picks:\n{body or '—'}")

# =========================
# Daily summary (09:00 Asia/Bangkok)
# =========================
async def daily_summary(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID:
        return
    o = get_live_outlook()
    text = (
        f"🗓️ สรุปประจำวัน {o['date']}\n"
        f"Strong CALL: {o['strong_call']} | Watch CALL: {o['watch_call']}\n"
        f"Strong PUT: {o['strong_put']} | Watch PUT: {o['watch_put']}\n"
        f"→ โมเมนตัมรวม: {o['comment']}"
    )
    try:
        await context.bot.send_message(chat_id=int(CHAT_ID), text=text)
    except Exception as e:
        log.exception("ส่งสรุปรายวันล้มเหลว: %s", e)

# =========================
# Build Telegram app
# =========================
def build_application():
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("ping", cmd_ping))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))

    # summary 09:00 BKK
    app_tg.job_queue.run_daily(
        daily_summary,
        time=dtime(hour=9, minute=0, tzinfo=TZ),
        name="daily_summary",
    )
    return app_tg

# =========================
# Entrypoint
# - Flask background thread
# - Telegram polling on main thread (หลีกเลี่ยงปัญหา signal handler)
# =========================
def run_flask():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)

async def run_telegram():
    app_tg = build_application()
    log.info("BOT_TOKEN loaded (len=%d), provider=%s, watchlist=%s", len(BOT_TOKEN), DATA_PROVIDER, ",".join(WATCHLIST))
    await app_tg.run_polling(close_loop=False)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_telegram())
