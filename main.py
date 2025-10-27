# -*- coding: utf-8 -*-
"""
Stock-signal-bot (Render + Telegram + Yahoo)
- โหมดหลัก: Webhook (ต้องตั้ง PUBLIC_URL, BOT_TOKEN)
- แหล่งข้อมูล: Yahoo (ไม่ใช้ API key) | ถ้าเรียกไม่ได้จะ fallback เป็นโหมดจำลอง
- คำสั่ง: /ping, /signals, /outlook, /picks, /movers
"""

from __future__ import annotations

import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# Config / ENV
# =========================
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
PUBLIC_URL: str = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "/webhook")
PORT: int = int(os.getenv("PORT", "10000"))

DATA_SOURCE: str = os.getenv("DATA_SOURCE", "yahoo").lower()  # yahoo | demo
TZ_NAME: str = os.getenv("TZ", "Asia/Bangkok")

# สัญลักษณ์ตัวอย่างสำหรับคำสั่ง /movers และ /picks
DEFAULT_MOVERS = ["AAPL", "NVDA", "TSLA"]
DEFAULT_PICKS = ["BYND", "KUKE", "GSIT"]

# =========================
# Yahoo Quote Client
# =========================
YF_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Connection": "keep-alive",
}


async def fetch_yahoo_quotes(
    symbols: List[str],
    timeout: float = 8.0,
    retries: int = 2,
) -> Dict[str, Dict]:
    """ดึงราคาจาก Yahoo; ถ้าล้มเหลวจะคืน {}"""
    params = {"symbols": ",".join(symbols)}
    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession(headers=YF_HEADERS) as session:
                async with session.get(YF_URL, params=params, timeout=timeout) as r:
                    if r.status != 200:
                        raise RuntimeError(f"Yahoo status {r.status}")
                    data = await r.json()
                    result = data.get("quoteResponse", {}).get("result", [])
                    quotes: Dict[str, Dict] = {}
                    for q in result:
                        s = q.get("symbol")
                        if not s:
                            continue
                        quotes[s] = {
                            "symbol": s,
                            "name": q.get("shortName") or q.get("longName") or s,
                            "price": q.get("regularMarketPrice"),
                            "change": q.get("regularMarketChange"),
                            "changePct": q.get("regularMarketChangePercent"),
                            "prevClose": q.get("regularMarketPreviousClose"),
                            "currency": q.get("currency"),
                            "marketState": q.get("marketState"),
                        }
                    return quotes
        except Exception:
            if attempt >= retries:
                return {}
            await asyncio.sleep(1.2 * (attempt + 1))
    return {}


# =========================
# Helpers
# =========================
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_change(ch: Optional[float]) -> str:
    if ch is None:
        return "—"
    return f"{ch:+.2f}"


def fmt_pct(pct: Optional[float]) -> str:
    if pct is None:
        return "—"
    return f"{pct:+.2f}%"


def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "—"
    # ราคาหุ้น US ส่วนใหญ่ทศนิยม 2 ตำแหน่ง
    return f"{p:.2f}"


async def get_quotes(symbols: List[str]) -> Dict[str, Dict]:
    """สวิตช์แหล่งข้อมูลตาม DATA_SOURCE"""
    if DATA_SOURCE == "yahoo":
        return await fetch_yahoo_quotes(symbols)
    # โหมดจำลอง
    out: Dict[str, Dict] = {}
    for s in symbols:
        out[s] = {
            "symbol": s,
            "name": s,
            "price": 100.0,
            "change": 0.0,
            "changePct": 0.0,
            "prevClose": 100.0,
            "currency": "USD",
            "marketState": "REG",
        }
    return out


def badge_ready(ok: bool) -> str:
    return "✅" if ok else "⚠️"


# =========================
# Commands
# =========================
async def cmd_ping(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong 🏓")


async def cmd_signals(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    # ตัวอย่างคงที่ (ยังไม่ผูกสัญญาณจริง)
    text = "🟣 Signals (จำลอง)\nStrong CALL: 15 | Strong PUT: 22"
    await update.message.reply_text(text)


async def cmd_outlook(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    # สรุป sentiment อย่างสั้น (จำลอง)
    text = "📈 Outlook วันนี้: โมเมนตัมกลางๆ"
    await update.message.reply_text(text)


async def _build_line_from_quote(q: Optional[Dict], symbol: str) -> str:
    if not q or q.get("price") is None:
        return f"⚠️ {symbol}: ข้อมูลไม่พร้อม"
    p = fmt_price(q.get("price"))
    ch = fmt_change(q.get("change"))
    pct = fmt_pct(q.get("changePct"))
    name = q.get("name") or symbol
    cur = q.get("currency") or ""
    return f"✅ {symbol}: {name} — {p} {cur} ({ch}, {pct})"


async def cmd_picks(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ กำลังดึงข้อมูลหุ้น...")

    syms = DEFAULT_PICKS
    quotes = {}
    try:
        quotes = await get_quotes(syms)
    except Exception:
        quotes = {}

    lines = ["🧾 Picks (รายละเอียด)"]
    for s in syms:
        q = quotes.get(s)
        lines.append(await _build_line_from_quote(q, s))
    msg = "\n".join(lines)
    await update.message.reply_text(msg)


async def cmd_movers(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    syms = DEFAULT_MOVERS
    quotes = {}
    ok = True
    try:
        quotes = await get_quotes(syms)
        ok = len(quotes) > 0
    except Exception:
        ok = False

    if not ok:
        # แสดงเฉพาะสัญลักษณ์ตัวอย่างเมื่อไม่มีข้อมูลจริง
        await update.message.reply_text("📊 Movers: (ตัวอย่าง) AAPL, NVDA, TSLA")
        return

    lines = [f"📊 Movers {badge_ready(True)}:"]
    for s in syms:
        q = quotes.get(s)
        if not q:
            lines.append(f"⚠️ {s}: ข้อมูลไม่พร้อม")
            continue
        p = fmt_price(q.get("price"))
        pct = fmt_pct(q.get("changePct"))
        lines.append(f"• {s}: {p} ({pct})")
    await update.message.reply_text("\n".join(lines))


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\n"
        "คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/signals - สัญญาณจำลอง\n"
        "/outlook - มุมมองตลาด\n"
        "/picks - หุ้นน่าสนใจ\n"
        "/movers - หุ้นเคลื่อนไหวเด่น\n\n"
        f"⏱️ {utc_iso()}"
    )
    await update.message.reply_text(text)


# =========================
# Application / Webhook
# =========================
def require_env() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not PUBLIC_URL:
        missing.append("PUBLIC_URL")
    if missing:
        raise RuntimeError(
            "Missing env: " + ", ".join(missing) + ". "
            "Set them in Render → Environment."
        )


def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    # register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("picks", cmd_picks))
    app.add_handler(CommandHandler("movers", cmd_movers))
    return app


async def run_webhook(application: Application) -> None:
    """
    รัน webhook ของ PTB บนพอร์ตเดียว (Render จะสแกนเจอพอร์ตนี้)
    ไม่ปิด event loop (close_loop=False) เพื่อเลี่ยง RuntimeError
    """
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    await application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH.lstrip("/"),
        webhook_url=webhook_url,
        drop_pending_updates=True,
        close_loop=False,  # สำคัญ: อย่าปิด loop ของ asyncio
        # bootstrap_retries=0  # ใช้ค่า default ก็พอ
    )


async def main_async() -> None:
    require_env()
    app = build_application()
    await run_webhook(app)


def main() -> None:
    """
    Entry point สำหรับ Render: python main.py
    ถ้า event loop นี้ยังไม่เริ่ม → run_until_complete
    ถ้ามีคนไปเรียกซ้ำและ loop เริ่มแล้ว → สร้าง task แล้ว run_forever
    """
    try:
        asyncio.get_event_loop().run_until_complete(main_async())
    except RuntimeError:
        # กรณี loop กำลังรันอยู่ (เช่น ถูกเรียกซ้ำ) → สร้าง task แล้วค้างรอ
        loop = asyncio.get_event_loop()
        loop.create_task(main_async())
        loop.run_forever()


if __name__ == "__main__":
    main()
