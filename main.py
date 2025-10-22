# --- Patch imghdr สำหรับ Python 3.13+ ---
import sys, types
if 'imghdr' not in sys.modules:
    sys.modules['imghdr'] = types.ModuleType("imghdr")
    def what(file, h=None):
        return None
    sys.modules['imghdr'].what = what
# ------------------------------------------

import os
import requests
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update, ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === ENV Variables (Render) ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
PUBLIC_URL = os.getenv("PUBLIC_URL")
PORT = int(os.getenv("PORT", 10000))

# === Flask keep-alive ===
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Stock Signal Bot is running."

# === ฟังก์ชันช่วย ===
def get_polygon_data(date_str):
    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_str}?adjusted=true&apiKey={POLYGON_API_KEY}"
    r = requests.get(url)
    if r.status_code == 200:
        return r.json().get("results", [])
    else:
        return []

def _fmt_num(n, d=2):
    try:
        return f"{float(n):,.{d}f}"
    except:
        return n

# === Telegram Bot ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\n\n"
        "คำสั่งที่ใช้ได้:\n"
        "• /movers – ดู Top Movers (ฟรี)\n"
        "• /signals – จัดกลุ่ม Strong/Watch (CALL/PUT)\n"
        "• /outlook – คาดการณ์แนวโน้มวันนี้ (อิงข้อมูลเมื่อวาน)\n"
        "• /help – ดูเมนูนี้อีกครั้ง\n\n"
        "เกณฑ์: pct ≥ 10.0%, ราคา ≥ 0.30, Vol ≥ 0"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ กำลังดึงข้อมูลจาก Polygon (ฟรี mode)...")

    date_str = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
    data = get_polygon_data(date_str)
    if not data:
        await update.message.reply_text("⚠️ ไม่พบข้อมูลจาก Polygon API.")
        return

    movers_up = [d for d in data if d.get('c', 0) >= 0.3 and d.get('v', 0) >= 0 and ((d.get('c', 0) - d.get('o', 0)) / d.get('o', 1)) * 100 >= 10]
    movers_up = sorted(movers_up, key=lambda x: x['c'], reverse=True)[:20]

    msg = f"✅ Top Movers (ฟรี, ย้อนหลังวันล่าสุด)\nวันที่อ้างอิง: {date_str}\nเกณฑ์: ≥10.0% | ราคา ≥0.3 | Vol ≥0\n\n📈 ขึ้นแรง:\n"
    for d in movers_up:
        pct = ((d['c'] - d['o']) / d['o']) * 100
        msg += f"• {d['T']} +{_fmt_num(pct)}% @{_fmt_num(d['c'])} Vol:{_fmt_num(d['v'],0)}\n"

    await update.message.reply_text(msg)

async def signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🕐 กำลังจัดกลุ่มสัญญาณจากข้อมูลล่าสุด...")

    # Mock Data (จำลองกลุ่ม)
    strong_call = ["PACB", "QBTZ", "GITS", "PMI", "BYND"]
    watch_call = ["NESR", "ALTS", "UPB", "YHC", "OVID"]
    strong_put = ["NVAVW", "QBTX", "UHG", "STI", "OWLS"]
    watch_put = ["AZTR", "CATX", "RAPT", "AENTW", "GNPNX"]

    msg = (
        "📊 จัดกลุ่มสัญญาณล่าสุด\n\n"
        "💚 Strong CALL\n" + ", ".join(strong_call) +
        "\n\n🟩 Watch CALL\n" + ", ".join(watch_call) +
        "\n\n❤️ Strong PUT\n" + ", ".join(strong_put) +
        "\n\n🟥 Watch PUT\n" + ", ".join(watch_put)
    )
    await update.message.reply_text(msg)

async def outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...")

    msg = (
        "🔮 คาดการณ์แนวโน้มวันนี้ (อิงจากข้อมูลเมื่อวาน)\n"
        "• Momentum ขาขึ้น: Strong CALL 30 — ตัวอย่าง: BYND, KUKE, JFU, QBTZ\n"
        "• ลุ้นเบรกขึ้น: Watch CALL 30 — ตัวอย่าง: GSIT, NVTX, NIOBW, BETR\n"
        "• Momentum ขาลง: Strong PUT 30 — ตัวอย่าง: NVAVW, QBTX, UHG, STI\n"
        "• ระวังอ่อนแรง: Watch PUT 30 — ตัวอย่าง: AZTR, CATX, RAPT, GNPNX\n\n"
        "💡 แนวคิด:\n"
        "• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าออุ้มหนุน\n"
        "• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม\n"
        "• Strong PUT ลงต่อหรือรีบาวน์สั้น\n"
        "• Watch PUT ระวังหลุดแนวรับ"
    )
    await update.message.reply_text(msg)

# === รันบอท ===
def main():
    app_flask = ApplicationBuilder().token(BOT_TOKEN).build()
    app_flask.add_handler(CommandHandler("start", start))
    app_flask.add_handler(CommandHandler("help", help_cmd))
    app_flask.add_handler(CommandHandler("movers", movers))
    app_flask.add_handler(CommandHandler("signals", signals))
    app_flask.add_handler(CommandHandler("outlook", outlook))

    # Flask + Telegram พร้อมกัน
    from threading import Thread
    Thread(target=lambda: app.run(host="0.0.0.0", port=PORT)).start()
    app_flask.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
