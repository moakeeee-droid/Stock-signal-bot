import os
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any

import requests
from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}" if PUBLIC_URL else ""
# รายการหุ้นสำหรับ /picks (ตั้งใน Render: env PICKS=BYND,KUKE,GSIT)
PICKS = [s.strip().upper() for s in os.getenv("PICKS", "BYND,KUKE,GSIT").split(",") if s.strip()]

# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# ================== FLASK ==================
app = Flask(__name__)

@app.get("/")
def home():
    return "✅ Stock Signal Bot is running (webhook mode)."

@app.get("/setwebhook")
async def set_webhook_route():
    """เรียกครั้งเดียวหลัง deploy (หรือเวลาเปลี่ยน PUBLIC_URL)"""
    if not WEBHOOK_URL:
        return "❌ PUBLIC_URL is empty", 400
    await tg_app.bot.delete_webhook(drop_pending_updates=True)
    ok = await tg_app.bot.set_webhook(url=WEBHOOK_URL, allowed_updates=["message"])
    if ok:
        return f"✅ Webhook set to {WEBHOOK_URL}", 200
    return "❌ Failed to set webhook", 500

@app.post(WEBHOOK_PATH)
async def webhook():
    """รับอัปเดตจาก Telegram"""
    data = request.get_json(force=True, silent=True)
    if not data:
        return "No data", 400
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return "OK", 200

# ================== Yahoo Finance Helpers ==================
YF_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

def yf_quote(symbols: List[str]) -> Dict[str, Any]:
    """ดึงข้อมูลแบบ bulk จาก Yahoo (ไม่ต้องใช้ API key)"""
    params = {"symbols": ",".join(symbols)}
    r = requests.get(YF_QUOTE_URL, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def fmt_num(n) -> str:
    if n is None:
        return "-"
    try:
        return f"{n:,.2f}"
    except Exception:
        try:
            return f"{float(n):,.2f}"
        except Exception:
            return str(n)

def human_cap(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ["", "K", "M", "B", "T"]:
        if abs(n) < 1000.0:
            return f"{n:,.2f}{unit}"
        n /= 1000.0
    return f"{n:,.2f}P"

def build_pick_message(ticker: str, q: Dict[str, Any]) -> str:
    name = q.get("shortName") or q.get("longName") or ticker
    price = q.get("regularMarketPrice")
    chg = q.get("regularMarketChange")
    chg_pct = q.get("regularMarketChangePercent")
    high = q.get("regularMarketDayHigh")
    low = q.get("regularMarketDayLow")
    openp = q.get("regularMarketOpen")
    prev = q.get("regularMarketPreviousClose")
    vol = q.get("regularMarketVolume")
    avgvol = q.get("averageDailyVolume3Month")
    cap = q.get("marketCap")
    pe = q.get("trailingPE")

    arrow = "🟢" if (chg or 0) > 0 else ("🔴" if (chg or 0) < 0 else "⚪")
    chg_str = f"{fmt_num(chg)} ({fmt_num(chg_pct)}%)" if chg is not None else "-"

    lines = [
        f"💡 *{name}* ({ticker})",
        f"{arrow} ล่าสุด: *{fmt_num(price)}*  | เปลี่ยนแปลง: *{chg_str}*",
        f"📊 เปิด: {fmt_num(openp)}  | ปิดก่อนหน้า: {fmt_num(prev)}",
        f"📈 สูงสุดวัน: {fmt_num(high)}  | ต่ำสุดวัน: {fmt_num(low)}",
        f"📦 ปริมาณ: {fmt_num(vol)}  | Avg 3M: {fmt_num(avgvol)}",
        f"🏢 มาร์เก็ตแคป: {human_cap(cap)}  | P/E: {fmt_num(pe)}",
    ]
    return "\n".join(lines)

# ================== Telegram Handlers ==================
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """แสดงรายละเอียดหุ้นตามรายการ PICKS ด้วยข้อมูลจาก Yahoo"""
    try:
        data = yf_quote(PICKS)
        results = {r["symbol"].upper(): r for r in data.get("quoteResponse", {}).get("result", [])}
        msgs = []
        for t in PICKS:
            q = results.get(t.upper())
            if not q:
                msgs.append(f"⚠️ ไม่พบข้อมูล: {t}")
                continue
            msgs.append(build_pick_message(t.upper(), q))
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        header = f"📝 *Picks อัปเดต:* _{ts}_"
        await update.message.reply_text(
            header + "\n\n" + "\n\n".join(msgs),
            parse_mode="Markdown"
        )
    except Exception as e:
        log.exception("picks error")
        await update.message.reply_text(f"❌ เกิดข้อผิดพลาด: {e}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "คำสั่งที่ใช้ได้:\n"
        "/ping - ทดสอบบอท\n"
        "/picks - หุ้นน่าสนใจ (ดึงรายละเอียดจาก Yahoo)\n"
        "/help - เมนูนี้\n"
    )
    await update.message.reply_text(txt)

# ================== Build Telegram App ==================
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")

    app_tg = Application.builder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("ping", cmd_ping))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    return app_tg

# สร้างตัวแปร global ให้ Flask เรียกใช้
tg_app: Application = build_app()

# ================== ENTRYPOINT ==================
if __name__ == "__main__":
    log.info("Starting Flask + Telegram (webhook mode)")
    # หมายเหตุ: เปิดเว็บแล้วเรียก /setwebhook หนึ่งครั้งเพื่อผูก webhook กับ Telegram
    app.run(host="0.0.0.0", port=PORT)
