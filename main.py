# -*- coding: utf-8 -*-
# Stock-signal-bot — โหมดฟรี (ใช้ข้อมูลย้อนหลังวันล่าสุดจาก Polygon)
# คำสั่งหลัก:
# /movers   : Top movers (ฟรี)
# /signals  : แบ่งกลุ่ม Strong/Watch (CALL/PUT)
# /outlook  : สรุปแนวโน้มวันนี้ (อิงข้อมูลเมื่อวาน)
# /picks    : คัด 5 ตัวที่เด่น (intraday idea)
# /help     : ดูเมนู
#
# ENV ที่ต้องมีบน Render:
# BOT_TOKEN, POLYGON_API_KEY    (บังคับ)
# CHAT_ID                       (ไม่บังคับ; จะใช้ broadcast ได้)
# PORT                          (Render จะส่งให้อัตโนมัติ)
#
# โหมดรัน: Long-polling (บ็อต) + Flask (health check เพื่อให้ Render เห็นพอร์ตเปิด)

import os
import threading
import asyncio
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# -----------------------------
# ตั้งค่าจาก ENV
# -----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip() or None
PORT = int(os.environ.get("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("กรุณาตั้งค่า ENV: BOT_TOKEN")
if not POLYGON_API_KEY:
    raise RuntimeError("กรุณาตั้งค่า ENV: POLYGON_API_KEY")

# -----------------------------
# Utils & Data fetch (Polygon free mode: grouped bars of previous market day)
# -----------------------------
US_EAST = timezone(timedelta(hours=-4))  # EDT (ถ้าเป็น EST จะ -5 แต่เราใช้ย้อนหลัง + API จะจัดการให้)

def _prev_market_date_utc():
    # เอาวันเมื่อวาน (ตามนิวยอร์ก) เพราะ free plan ขอ today's grouped ไม่ได้
    ny_now = datetime.now(US_EAST)
    d = ny_now.date() - timedelta(days=1)
    # ข้ามเสาร์อาทิตย์
    while d.weekday() >= 5:  # 5=Sat,6=Sun
        d -= timedelta(days=1)
    return d.isoformat()

def _fmt_num(n, p=0):
    try:
        if p is None:
            p = 0
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
    """
    ดึง top movers ของวันก่อนหน้า (ฟรี)
    คืนค่า list ของ dict: {T, c, o, h, l, v, pct}
    """
    day = _prev_market_date_utc()
    url = (
        f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
        f"{day}?adjusted={'true' if adjusted else 'false'}&apiKey={POLYGON_API_KEY}"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    results = data.get("results", []) or []

    out = []
    for it in results:
        t = it.get("T")
        c = it.get("c")
        o = it.get("o")
        h = it.get("h")
        l = it.get("l")
        v = it.get("v")
        if not (t and c and o and h and l and v):
            continue
        pct = _safe_pct(c, o)
        out.append(
            {
                "T": t,
                "c": float(c),
                "o": float(o),
                "h": float(h),
                "l": float(l),
                "v": float(v),
                "pct": float(pct),
            }
        )
    # เรียงตาม % เปลี่ยนแปลงมากสุด
    out.sort(key=lambda x: abs(x["pct"]), reverse=True)
    return out[:limit]

# -----------------------------
# Rules: จัดกลุ่ม Strong/Watch (CALL/PUT)
# -----------------------------
def _close_pos_in_range(c, l, h):
    rng = max(h - l, 1e-9)
    return (c - l) / rng  # 0 ใกล้ Low, 1 ใกล้ High

def build_signal_buckets(rows):
    """
    คืนค่า dict:
    {
      'strong_call': [...], 'watch_call': [...],
      'strong_put':  [...], 'watch_put':  [...]
    }
    แต่ละตัวเป็น tuple (sym, c, pct, v, note)
    """
    strong_call, watch_call, strong_put, watch_put = [], [], [], []

    for x in rows:
        sym, c, o, h, l, v, pct = x["T"], x["c"], x["o"], x["h"], x["l"], x["v"], x["pct"]
        if c < 0.30 or v <= 0:  # ตัด penny ที่บางมาก ๆ ออกบ้าง
            continue

        pos = _close_pos_in_range(c, l, h)  # ใกล้ high?
        body = abs(c - o) / max(h - l, 1e-9)  # body size

        # -------- ฝั่งขึ้น (CALL)
        if pct >= 8.0 and pos >= 0.8 and body >= 0.55:
            strong_call.append((sym, c, pct, v, "close near H, strong body"))
        elif pct >= 5.0 and pos >= 0.7:
            watch_call.append((sym, c, pct, v, "close near H"))

        # -------- ฝั่งลง (PUT)
        if pct <= -8.0 and pos <= 0.2 and body >= 0.55:
            strong_put.append((sym, c, pct, v, "close near L, strong body"))
        elif pct <= -5.0 and pos <= 0.3:
            watch_put.append((sym, c, pct, v, "close near L"))

    # จำกัดจำนวนรายการเพื่อความอ่านง่าย
    def _top(lst, n=30):
        return sorted(lst, key=lambda z: abs(z[2]), reverse=True)[:n]

    return {
        "strong_call": _top(strong_call),
        "watch_call": _top(watch_call),
        "strong_put": _top(strong_put),
        "watch_put": _top(watch_put),
    }

def picks_intraday(buckets, n=5):
    # คัด 5 ตัวเด่นจาก strong_call ก่อน ถ้าไม่พอไปดู watch_call
    base = buckets["strong_call"] + buckets["watch_call"]
    return base[:n]

# -----------------------------
# Formatters
# -----------------------------
def fmt_list(title, items):
    if not items:
        return f"• <b>{title}</b>: -"
    lines = [f"• <b>{title}</b>"]
    for sym, c, pct, v, note in items:
        lines.append(
            f"  • <b>{sym}</b> @{_fmt_num(c,2)} — pct {_fmt_num(pct,1)}%, Vol {_fmt_num(v,0)}"
            + (f", {note}" if note else "")
        )
    return "\n".join(lines)

def fmt_movers(rows, min_pct=10.0, min_price=0.30, min_vol=0, top=20):
    up = [x for x in rows if x["pct"] >= min_pct and x["c"] >= min_price and x["v"] >= min_vol]
    up = sorted(up, key=lambda z: z["pct"], reverse=True)[:top]
    dn = [x for x in rows if x["pct"] <= -min_pct and x["c"] >= min_price and x["v"] >= min_vol]
    dn = sorted(dn, key=lambda z: z["pct"])[:top]

    hdr = "✅ <b>Top Movers</b> (ฟรี, ย้อนหลังวันล่าสุด)\n"
    ref = _prev_market_date_utc()
    hdr += f"<i>วันที่อ้างอิง: {ref}</i>\nเกณฑ์: ≥{min_pct:.1f}% | ราคา ≥{min_price:.2f} | Vol ≥{min_vol}\n"

    def _side(label, lst):
        if not lst:
            return f"\n📉 {label}: -"
        lines = [f"\n📈 {label if label=='ขึ้นแรง' else label}:"]
        for x in lst:
            lines.append(
                f"• <b>{x['T']}</b> +{_fmt_num(x['pct'],1)}% @{_fmt_num(x['c'],2)} "
                f"Vol:{_fmt_num(x['v'],0)}"
                if label == "ขึ้นแรง"
                else f"• <b>{x['T']}</b> {_fmt_num(x['pct'],1)}% @{_fmt_num(x['c'],2)} "
                     f"Vol:{_fmt_num(x['v'],0)}"
            )
        return "\n".join(lines)

    msg = hdr + _side("ขึ้นแรง", up) + _side("ลงแรง", dn)
    return msg

def fmt_outlook(buckets):
    # สรุปแนวโน้ม (อิงจากข้อมูลเมื่อวาน)
    examples = lambda key: ", ".join([t[0] for t in buckets[key][:12]]) or "-"
    lines = []
    lines.append("🔮 <b>คาดการณ์แนวโน้มวันนี้</b> (อิงจากข้อมูลเมื่อวาน)")
    lines.append(f"• <b>Momentum ขาขึ้น:</b> Strong CALL 30 — ตัวอย่าง: {examples('strong_call')}")
    lines.append(f"• <b>ลุ้นเบรกขึ้น:</b> Watch CALL 30 — ตัวอย่าง: {examples('watch_call')}")
    lines.append(f"• <b>Momentum ขาลง:</b> Strong PUT 30 — ตัวอย่าง: {examples('strong_put')}")
    lines.append(f"• <b>ระวังอ่อนแรง:</b> Watch PUT 30 — ตัวอย่าง: {examples('watch_put')}")
    lines.append("\n💡 <b>แนวคิด:</b>\n"
                 "• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าจ่อจุดหนุน\n"
                 "• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม\n"
                 "• Strong PUT ลงต่อหรือรีบาวด์สั้น\n"
                 "• Watch PUT ระวังหลุดแนวรับ")
    return "\n".join(lines)

# -----------------------------
# Telegram Handlers (async)
# -----------------------------
async def _load_free_data():
    # เรียก requests ใน thread เพื่อไม่บล็อก event loop
    rows = await asyncio.to_thread(fetch_grouped_bars_yesterday)
    return rows, build_signal_buckets(rows)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\n"
        "พิมพ์ /help เพื่อดูคำสั่งทั้งหมด",
        parse_mode=ParseMode.HTML,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "คำสั่งที่ใช้ได้\n"
        "• /movers – ดู Top Movers (ฟรี)\n"
        "• /signals – จัดกลุ่ม Strong/Watch (CALL/PUT)\n"
        "• /outlook – คาดการณ์โมเมนตัมวันนี้ (อิงเมื่อวาน)\n"
        "• /picks – คัด 5 ตัวสำหรับเก็งกำไรในวันถัดไป\n"
        "\nเกณฑ์เริ่มต้น: pct ≥ 10.0%, ราคา ≥ 0.30, Vol ≥ 0",
        parse_mode=ParseMode.HTML,
    )

async def cmd_movers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⌛ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...", parse_mode=ParseMode.HTML)
    try:
        rows, _ = await _load_free_data()
        txt = fmt_movers(rows)
    except Exception as e:
        txt = f"ขออภัย ดึงข้อมูลไม่สำเร็จ: {e}"
    await msg.edit_text(txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⌛ กำลังจัดกลุ่มสัญญาณ...", parse_mode=ParseMode.HTML)
    try:
        _, buckets = await _load_free_data()
        parts = [
            "📊 <b>สัญญาณ (อิงข้อมูลเมื่อวาน)</b>",
            fmt_list("Strong CALL", buckets["strong_call"]),
            fmt_list("Watch  CALL",  buckets["watch_call"]),
            fmt_list("Strong PUT",  buckets["strong_put"]),
            fmt_list("Watch  PUT",   buckets["watch_put"]),
        ]
        txt = "\n\n".join(parts)
    except Exception as e:
        txt = f"ขออภัย จัดกลุ่มไม่สำเร็จ: {e}"
    await msg.edit_text(txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_outlook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⌛ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...", parse_mode=ParseMode.HTML)
    try:
        _, buckets = await _load_free_data()
        txt = fmt_outlook(buckets)
    except Exception as e:
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
        txt = f"ขออภัย เลือกตัวเด่นไม่สำเร็จ: {e}"
    await msg.edit_text(txt, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# -----------------------------
# Application & Flask
# -----------------------------
app = Flask(__name__)
_tele_app = ApplicationBuilder().token(BOT_TOKEN).build()

# ลงทะเบียนคำสั่ง
_tele_app.add_handler(CommandHandler("start",   cmd_start))
_tele_app.add_handler(CommandHandler("help",    cmd_help))
_tele_app.add_handler(CommandHandler("movers",  cmd_movers))
_tele_app.add_handler(CommandHandler("signals", cmd_signals))
_tele_app.add_handler(CommandHandler("outlook", cmd_outlook))
_tele_app.add_handler(CommandHandler("picks",   cmd_picks))

# หน้า health check / root
@app.get("/")
def home():
    return "Bot is running fine."

@app.get("/healthz")
def healthz():
    return "ok"

def run_polling():
    # รันบอทแบบ long polling ใน thread แยก
    asyncio.run(_tele_app.run_polling(close_loop=False))

if __name__ == "__main__":
    # สตาร์ทบอทใน background thread
    t = threading.Thread(target=run_polling, daemon=True)
    t.start()
    # เปิด Flask เพื่อให้ Render ตรวจเจอพอร์ต
    app.run(host="0.0.0.0", port=PORT)
