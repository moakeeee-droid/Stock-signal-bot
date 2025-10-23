import os
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any
import requests
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}" if PUBLIC_URL else ""
PICKS = [s.strip().upper() for s in os.getenv("PICKS", "BYND,KUKE,GSIT").split(",") if s.strip()]

# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Stock Signal Bot is running."

@app.route("/setwebhook")
def set_webhook_route():
    """ตั้ง webhook ผ่าน browser"""
    if not WEBHOOK_URL:
        return "❌ PUBLIC_URL is empty", 400
    try:
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        bot.delete_webhook(drop_pending_updates=True)
        ok = bot.set_webhook(url=WEBHOOK_URL, allowed_updates=["message"])
        if ok:
            return f"✅ Webhook set to {WEBHOOK_URL}", 200
        return "❌ Failed to set webhook", 500
    except Exception as e:
        log.exception("Webhook error:")
        return f"❌ Error: {e}", 500

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    """รับอัปเดตจาก Telegram"""
    if not tg_app:
        return "Bot not ready", 500
    data = request.get_json(force=True, silent=True)
    if not data:
        return "No data", 400
    update = Update.de_json(data, tg_app.bot)
    tg_app.create_task(tg_app.process_update(update))
    return "OK", 200

# ================== Yahoo Finance ==================
YF_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"

def yf_quote(symbols: List[str]) -> Dict[str, Any]:
    params = {"symbols": ",".join(symbols)}
    r = requests.get(YF_QUOTE_URL, params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def fmt_num(n) -> str:
    if n is None: return "-"
    try: return f"{float(n):,.2f}"
    except: return str(n)

def human_cap(n) -> str:
    if n is None: return "-"
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
    arrow = "🟢" if (chg or 0) > 0 else ("🔴" if (chg or 0) < 0 else "⚪")
    chg_str = f"{fmt_num(chg)} ({fmt_num(chg_pct)}%)" if chg is not None else "-"
    lines = [
        f"💡 *{name}* ({ticker})",
        f"{arrow} ราคา: *{fmt_num(price)}*  | เปลี่ยนแปลง: *{chg_str}*",
        f"🏢 มาร์เก็ตแคป: {human_cap(q.get('marketCap'))} | P/E: {fmt_num(q.get('trailingPE'))}"
    ]
    return "\n".join(lines)

# ================== Telegram Handlers ==================
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = yf_quote(PICKS)
        results = {r["symbol"].upper(): r for r in data.get("quoteResponse", {}).get("result", [])}
        msgs = []
        for t in PICKS:
            q = results.get(t.upper())
            msgs.append(build_pick_message(t, q) if q else f"⚠️ ไม่พบข้อมูล: {t}")
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        await update.message.reply_text(
            f"🕓 อัปเดต: _{ts}_\n\n" + "\n\n".join(msgs), parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "/ping - ตรวจสอบสถานะบอท\n"
        "/picks - ดูข้อมูลหุ้นจาก Yahoo Finance\n"
        "/help - แสดงคำสั่งทั้งหมด"
    )
    await update.message.reply_text(txt)

# ================== Telegram App ==================
def build_app() -> Application:
    app_tg = Application.builder().token(BOT_TOKEN).build()
    app_tg.add_handler(CommandHandler("ping", cmd_ping))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    return app_tg

tg_app = build_app()

# ================== ENTRYPOINT ==================
if __name__ == "__main__":
    log.info("🚀 Starting Flask + Telegram webhook mode")
    app.run(host="0.0.0.0", port=PORT)
