# -*- coding: utf-8 -*-
# Stock-signal-bot — โหมดฟรี (Polygon grouped bars ของวันก่อนหน้า)

import os
import threading
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("stock-signal-bot")

# ---------- ENV ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip() or None
PORT = int(os.environ.get("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("กรุณาตั้งค่า ENV: BOT_TOKEN")
if not POLYGON_API_KEY:
    raise RuntimeError("กรุณาตั้งค่า ENV: POLYGON_API_KEY")

# ---------- Helpers / Data ----------
US_EAST = timezone(timedelta(hours=-4))  # EDT

def _prev_market_date_utc():
    ny_now = datetime.now(US_EAST)
    d = ny_now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()

def _fmt_num(n, p=0):
    try:
        return f"{float(n):,.{p}f}"
    except Exception:
        return str(n)

def _safe_pct(a, b):
    try:
        if b == 0:
            return 0.0
        return (a - b) / b * 100.0
    except Exception:
        return 0.0

def fetch_grouped_bars_yesterday(adjusted=True, limit=1000):
    day = _prev_market_date_utc()
    url = (
        f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
        f"{day}?adjusted={'true' if adjusted else 'false'}&apiKey={POLYGON_API_KEY}"
    )
    log.info(f"Fetching grouped bars for {day}")
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    results = data.get("results", []) or []

    out = []
    for it in results:
        t = it.get("T"); c = it.get("c"); o = it.get("o")
        h = it.get("h"); l = it.get("l"); v = it.get("v")
        if not (t and c and o and h and l and v):
            continue
        pct = _safe_pct(c, o)
        out.append({"T": t, "c": float(c), "o": float(o),
                    "h": float(h), "l": float(l), "v": float(v),
                    "pct": float(pct)})
    out.sort(key=lambda x: abs(x["pct"]), reverse=True)
    log.info(f"Fetched {len(out)} rows")
    return out[:limit]

def _close_pos_in_range(c, l, h):
    rng = max(h - l, 1e-9)
    return (c - l) / rng

def build_signal_buckets(rows):
    strong_call, watch_call, strong_put, watch_put = [], [], [], []
    for x in rows:
        sym, c, o, h, l, v, pct = x["T"], x["c"], x["o"], x["h"], x["l"], x["v"], x["pct"]
        if c < 0.30 or v <= 0:
            continue
        pos = _close_pos_in_range(c, l, h)
        body = abs(c - o) / max(h - l, 1e-9)
        if pct >= 8.0 and pos >= 0.8 and body >= 0.55:
            strong_call.append((sym, c, pct, v, "close near H, strong body"))
        elif pct >= 5.0 and pos >= 0.7:
            watch_call.append((sym, c, pct, v, "close near H"))
        if pct <= -8.0 and pos <= 0.2 and body >= 0.55:
            strong_put.append((sym, c, pct, v, "close near L, strong body"))
        elif pct <= -5.0 and pos <= 0.3:
            watch_put.append((sym, c, pct, v, "close near L"))

    def _top(lst, n=30):
        return sorted(lst, key=lambda z: abs(z[2]), reverse=True)[:n]

    buckets = {
        "strong_call": _top(strong_call),
        "watch_call":  _top(watch_call),
        "strong_put":  _top(strong_put),
        "watch_put":   _top(watch_put),
    }
    log.info(
        "Buckets sizes — SC:%d WC:%d SP:%d WP:%d",
        len(buckets["strong_call"]), len(buckets["watch_call"]),
        len(buckets["strong_put"]),  len(buckets["watch_put"])
    )
    return buckets

def picks_intraday(buckets, n=5):
    base = buckets["strong_call"] + buckets["watch_call"]
    return base[:n]

# ---------- Formatters ----------
def fmt_list(title, items):
    if not items:
        return f"• <b>{title}</b>: -"
    lines = [f"• <b>{title}</b>"]
    for sym, c, pct, v, note in items:
        lines.append(
            f"  • <b>{sym}</b> @{_fmt_num(c,2)} — pct {_fmt_num(pct,1)}%, "
            f"Vol {_fmt_num(v,0)}" + (f", {note}" if note else "")
        )
    return "\n".join(lines)

def fmt_movers(rows, min_pct=10.0, min_price=0.30, min_vol=0, top=20):
    up = [x for x in rows if x["pct"] >= min_pct and x["c"] >= min_price and x["v"] >= min_vol]
    up = sorted(up, key=lambda z: z["pct"], reverse=True)[:top]
    dn = [x for x in rows if x["pct"] <= -min_pct and x["c"] >= min_price and x["v"] >= min_vol]
    dn = sorted(dn, key=lambda z: z["pct"])[:top]

    hdr = "✅ <b>Top Movers</b> (ฟรี, ย้อนหลังวันล่าสุด)\n"
    hdr += f"<i>วันที่อ้างอิง: {_prev_market_date_utc()}</i>\n"
    hdr += f"เกณฑ์: ≥{min_pct:.1f}% | ราคา ≥{min_price:.2f} | Vol ≥{min_vol}\n"

    def _side(label, lst, up_side=True):
        if not lst:
            return f"\n📉 {label}: -"
        lines = [f"\n📈 {label}:" if up_side else f"\n📉 {label}:"]
        for x in lst:
            if up_side:
                lines.append(
                    f"• <b>{x['T']}</b> +{_fmt_num(x['pct'],1)}% @{_fmt_num(x['c'],2)} "
                    f"Vol:{_fmt_num(x['v'],0)}"
                )
            else:
                lines.append(
                    f"• <b>{x['T']}</b> {_fmt_num(x['pct'],1)}% @{_fmt_num(x['c'],2)} "
                    f"Vol:{_fmt_num(x['v'],0)}"
                )
        return "\n".join(lines)

    return hdr + _side("ขึ้นแรง", up, True) + _side("ลงแรง", dn, False)

def fmt_outlook(buckets):
    ex = lambda k: ", ".join([t[0] for t in buckets[k][:12]]) or "-"
    lines = []
    lines.append("🔮 <b>คาดการณ์แนวโน้มวันนี้</b> (อิงจากข้อมูลเมื่อวาน)")
    lines.append(f"• <b>Momentum ขาขึ้น:</b> Strong CALL 30 — ตัวอย่าง: {ex('strong_call')}")
    lines.append(f"• <b>ลุ้นเบรกขึ้น:</b> Watch CALL 30 — ตัวอย่าง: {ex('watch_call')}")
    lines.append(f"• <b>Momentum ขาลง:</b> Strong PUT 30 — ตัวอย่าง: {ex('strong_put')}")
    lines.append(f"• <b>ระวังอ่อนแรง:</b> Watch PUT 30 — ตัวอย่าง: {ex('watch_put')}")
    lines.append(
        "\n💡 <b>แนวคิด:</b>\n"
        "• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าจ่อจุดหนุน\n"
        "• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม\n"
        "• Strong PUT ลงต่อหรือรีบาวด์สั้น\n"
        "• Watch PUT ระวังหลุดแนวรับ"
    )
    return "\n".join(lines)

# ---------- Async helpers ----------
async def _load_free_data():
    rows = await asyncio.to_thread(fetch_grouped_bars_yesterday)
    return rows, build_signal_buckets(rows)

# ---------- Telegram handlers ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\nพิมพ์ /help เพื่อดูคำสั่งทั้งหมด",
        parse_mode=ParseMode.HTML,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "คำสั่งที่ใช้ได้\n"
        "• /movers – ดู Top Movers (ฟรี)\n"
        "• /signals – จัดกลุ่ม Strong/Watch (CALL/PUT)\n"
        "• /outlook – คาดการณ์โมเมนตัมวันนี้ (อิงเมื่อวาน)\n"
        "• /picks – คัด 5 ตัวเด่นสำหรับเก็งกำไรวันถัดไป\n"
        "• /ping – ตรวจชีพบอท",
        parse_mode=ParseMode.HTML,
    )

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_movers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⌛ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...", parse_mode=ParseMode.HTML)
    try:
        rows, _ = await _load_free_data()
        txt = fmt_movers(rows)
    except Exception as e:
        log.exception("movers error")
        txt = f"ขออภัย ดึงข้อมูลไม่สำเร็จ: {e}"
    await msg.edit_text(txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⌛ กำลังจัดกลุ่มสัญญาณ...", parse_mode=ParseMode.HTML)
    try:
        _, buckets = await _load_free_data()
        parts = [
            "📊 <b>สัญญาณ (อิงข้อมูลเมื่อวาน)</b>",
            fmt_list("Strong CALL", buckets["strong_call"]),
            fmt_list("Watch  CALL", buckets["watch_call"]),
            fmt_list("Strong PUT",  buckets["strong_put"]),
            fmt_list("Watch  PUT",   buckets["watch_put"]),
        ]
        txt = "\n\n".join(parts)
    except Exception as e:
        log.exception("signals error")
        txt = f"ขออภัย จัดกลุ่มไม่สำเร็จ: {e}"
    await msg.edit_text(txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_outlook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⌛ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...", parse_mode=ParseMode.HTML)
    try:
        _, buckets = await _load_free_data()
        txt = fmt_outlook(buckets)
    except Exception as e:
        log.exception("outlook error")
        txt = f"ขออภัย สร้าง outlook ไม่สำเร็จ: {e}"
    await msg.edit_text(txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_picks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⌛ กำลังคัดตัวที่เด่น...", parse_mode=ParseMode.HTML)
    try:
        _, buckets = await _load_free_data()
        picks = picks_intraday(buckets, n=5)
        if not picks:
            txt = "ยังไม่มีตัวที่เข้าเกณฑ์ครับ"
        else:
            lines = ["🎯 <b>Picks (5)</b> — สำหรับวางแผนเก็งกำไรวันถัดไป"]
            for sym, c, pct, v, note in picks:
                lines.append(
                    f"• <b>{sym}</b> @{_fmt_num(c,2)} — pct {_fmt_num(pct,1)}%, "
                    f"Vol {_fmt_num(v,0)}" + (f", {note}" if note else "")
                )
            txt = "\n".join(lines)
    except Exception as e:
        log.exception("picks error")
        txt = f"ขออภัย เลือกตัวเด่นไม่สำเร็จ: {e}"
    await msg.edit_text(txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ---------- Build application ----------
app = Flask(__name__)
tele_app = ApplicationBuilder().token(BOT_TOKEN).build()

tele_app.add_handler(CommandHandler("start",   cmd_start))
tele_app.add_handler(CommandHandler("help",    cmd_help))
tele_app.add_handler(CommandHandler("ping",    cmd_ping))
tele_app.add_handler(CommandHandler("movers",  cmd_movers))
tele_app.add_handler(CommandHandler("signals", cmd_signals))
tele_app.add_handler(CommandHandler("outlook", cmd_outlook))
tele_app.add_handler(CommandHandler("picks",   cmd_picks))

@app.get("/")
def home():
    return "Bot is running fine."

@app.get("/healthz")
def healthz():
    return "ok"

def run_polling():
    try:
        log.info("Starting telegram long-polling…")
        # สำคัญ: ปิดการจับสัญญาณเมื่อรันใน thread
        asyncio.run(tele_app.run_polling(stop_signals=None, close_loop=False))
    except Exception:
        log.exception("Polling crashed")

if __name__ == "__main__":
    # สตาร์ทบอทใน background thread
    t = threading.Thread(target=run_polling, daemon=True)
    t.start()
    log.info("Flask starting on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
