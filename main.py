# -*- coding: utf-8 -*-
"""
Stock Signal Bot — Free mode (Polygon.io)
คำสั่ง:
  /help      : เมนูคำสั่ง
  /ping      : ทดสอบบอท
  /movers    : Top movers (โหมดฟรี — อิงข้อมูลวันทำการล่าสุดที่ฟรี)
  /signals   : จัดกลุ่ม Strong/Watch (CALL/PUT)
  /outlook   : มุมมองรวมจากข้อมูลล่าสุด
  /picks     : รายการคัดสั้น ๆ (เข้าออกวันเดียว)

ต้องตั้ง Environment vars บน Render:
  BOT_TOKEN, POLYGON_API_KEY [, CHAT_ID]

หมายเหตุ: ใช้ long-polling (no webhook) และมี Flask health route สำหรับ Render
"""

import os
import math
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Dict, Any, List, Tuple

import requests
from flask import Flask

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, ContextTypes
)

# ------------ LOGGING ------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("stock-signal-bot")

# ------------ CONFIG ------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()  # optional broadcast room

# เกณฑ์ default (ปรับได้ตามชอบ)
MIN_PCT = 10.0     # เปลี่ยนแปลง % ขั้นต่ำ
MIN_PRICE = 0.30   # ราคาปิดขั้นต่ำ
MIN_VOL = 0        # วอลุ่มขั้นต่ำ (ชิ้น)
TOP_LIMIT = 30     # จำนวนสูงสุดที่แสดงในแต่ละกลุ่ม

# ------------ UTIL ------------
def _fmt_num(n, d=2):
    try:
        return f"{n:,.{d}f}"
    except Exception:
        try:
            return f"{float(n):,.{d}f}"
        except Exception:
            return str(n)

def _short_vol(v: float) -> str:
    try:
        v = float(v)
        if v >= 1_000_000_000:
            return f"{v/1_000_000_000:.2f}B"
        if v >= 1_000_000:
            return f"{v/1_000_000:.2f}M"
        if v >= 1_000:
            return f"{v/1_000:.2f}K"
        return str(int(v))
    except Exception:
        return str(v)

def _is_weekend(dt: datetime) -> bool:
    return dt.weekday() >= 5  # Sat(5), Sun(6)

def last_free_trading_date(today_utc: datetime) -> str:
    """
    โหมดฟรีของ Polygon บางแผนจะอิงข้อมูลไม่ใช่แบบ real-time
    เราจะถอยกลับไปเรื่อย ๆ จน API ยอมตอบ (หรืออย่างน้อยวันทำการล่าสุด)
    คืนค่าเป็น YYYY-MM-DD
    """
    d = today_utc.astimezone(timezone.utc).date()
    # ถ้าเป็นเสาร์/อาทิตย์ ถอยไปวันศุกร์
    if _is_weekend(datetime(d.year, d.month, d.day)):
        while _is_weekend(datetime(d.year, d.month, d.day)):
            d = d - timedelta(days=1)
        return d.isoformat()
    return d.isoformat()

# ------------ POLYGON FETCH (FREE) ------------
_cache: Dict[str, Any] = {"date": None, "items": None}

def fetch_grouped_by_date(date_str: str) -> Tuple[str, List[Dict[str, Any]], str]:
    """
    ดึง grouped bars (US stocks) จาก Polygon สำหรับ date_str (YYYY-MM-DD)
    จะพยายามถอยวันอัตโนมัติถ้าเจอ NOT_AUTHORIZED ของโหมดฟรี
    """
    base = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks"
    attempts = 0
    last_err = ""
    cur = datetime.strptime(date_str, "%Y-%m-%d").date()

    while attempts < 5:
        url = f"{base}/{cur.isoformat()}?adjusted=true&apiKey={POLYGON_API_KEY}"
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                data = r.json()
                if data.get("resultsCount", 0) > 0 and isinstance(data.get("results"), list):
                    return (cur.isoformat(), data["results"], "")
                else:
                    last_err = "No results from Polygon."
            else:
                try:
                    js = r.json()
                    msg = js.get("message", "")
                    last_err = f"{r.status_code}: {msg}"
                    # โหมดฟรีถ้าขอวันปัจจุบันอาจได้ NOT_AUTHORIZED ให้ถอยวัน
                    if "NOT_AUTHORIZED" in js.get("status", "") or "Attempted to request today's data" in msg:
                        cur = cur - timedelta(days=1)
                        attempts += 1
                        time.sleep(0.5)
                        continue
                except Exception:
                    last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"Exception: {e}"

        # ถ้าไม่สำเร็จให้ถอยวันทำการหนึ่งวัน
        cur = cur - timedelta(days=1)
        attempts += 1
        time.sleep(0.4)

    return (date_str, [], last_err or "Failed to fetch data.")

def load_market() -> Tuple[str, List[Dict[str, Any]], str]:
    """
    ดึงข้อมูลและแคชไว้ (ป้องกันโดน rate limit)
    """
    global _cache
    today = datetime.now(timezone.utc)
    want = last_free_trading_date(today)
    if _cache["date"] == want and isinstance(_cache["items"], list):
        return (_cache["date"], _cache["items"], "")

    date_used, items, err = fetch_grouped_by_date(want)
    if not err and items:
        _cache["date"] = date_used
        _cache["items"] = items
    return (date_used, items, err)

# ------------ SCORING / GROUPING ------------
def qualify(item: Dict[str, Any]) -> bool:
    """
    กรองเบื้องต้นตามเกณฑ์
    """
    c = item.get("c", 0.0)  # close
    o = item.get("o", 0.0)  # open
    h = item.get("h", 0.0)
    l = item.get("l", 0.0)
    v = item.get("v", 0.0)
    # บางรายการไม่มีเปอร์เซ็นต์ ต้องคำนวณเองจาก OHLC
    pct = 0.0
    try:
        if o and isinstance(o, (int, float)) and o > 0:
            pct = (c - o) / o * 100.0
    except Exception:
        pct = 0.0

    sym = item.get("T", "")
    if not sym or "." in sym:  # ตัด .W, .U ฯลฯ ออกบ้าง (แล้วแต่ชอบ)
        pass

    return (
        (pct >= MIN_PCT or (-pct) >= MIN_PCT) and
        (c >= MIN_PRICE) and
        (v >= MIN_VOL)
    )

def classify(items: List[Dict[str, Any]]) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
    """
    จัดกลุ่มเป็น Strong/Watch (CALL/PUT) แบบ heuristic ง่าย ๆ
    - Strong CALL: pct ≥ 15%, close ใกล้ High (≥ 98%), body เขียว (c>o)
    - Watch CALL : pct 10–15% หรือ close ใกล้ High (≥ 97%) และเขียว
    - Strong PUT : pct ≤ −15%, close ใกล้ Low (≤ 102% ของ Low), body แดง (c<o)
    - Watch PUT  : pct −10% ถึง −15% หรือ close ใกล้ Low (≤ 103%) และแดง
    """
    out = {"strong_call": [], "watch_call": [], "strong_put": [], "watch_put": []}

    for it in items:
        if not qualify(it):
            continue
        sym = it.get("T", "")
        o = it.get("o", 0.0)
        c = it.get("c", 0.0)
        h = it.get("h", 0.0)
        l = it.get("l", 0.0)
        v = it.get("v", 0.0)

        pct = 0.0
        try:
            pct = (c - o) / o * 100.0 if o else 0.0
        except Exception:
            pass

        near_high = (h > 0 and c >= 0.98 * h)
        near_high_loose = (h > 0 and c >= 0.97 * h)
        near_low = (l > 0 and c <= 1.02 * l)
        near_low_loose = (l > 0 and c <= 1.03 * l)

        if pct >= 15 and c > o and near_high:
            out["strong_call"].append((sym, it))
        elif pct >= 10 and c > o and (near_high_loose or pct >= 12):
            out["watch_call"].append((sym, it))
        elif pct <= -15 and c < o and near_low:
            out["strong_put"].append((sym, it))
        elif pct <= -10 and c < o and (near_low_loose or pct <= -12):
            out["watch_put"].append((sym, it))

    # sort by absolute momentum
    out["strong_call"].sort(key=lambda x: (x[1]["c"] - x[1]["o"]) / (x[1]["o"] or 1), reverse=True)
    out["watch_call"].sort(key=lambda x: (x[1]["c"] - x[1]["o"]) / (x[1]["o"] or 1), reverse=True)
    out["strong_put"].sort(key=lambda x: (x[1]["c"] - x[1]["o"]) / (x[1]["o"] or 1))
    out["watch_put"].sort(key=lambda x: (x[1]["c"] - x[1]["o"]) / (x[1]["o"] or 1))

    # limit
    for k in out:
        out[k] = out[k][:TOP_LIMIT]
    return out

def picks_from_groups(groups: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> List[Tuple[str, Dict[str, Any], str]]:
    """
    เลือก pick สั้น ๆ จาก Strong CALL/PUT อย่างละไม่กี่ตัว
    """
    picks: List[Tuple[str, Dict[str, Any], str]] = []
    for sym, it in groups.get("strong_call", [])[:7]:
        picks.append((sym, it, "CALL"))
    for sym, it in groups.get("strong_put", [])[:7]:
        picks.append((sym, it, "PUT"))
    return picks

# ------------ TEXT BUILDERS ------------
def line_from_item(sym: str, it: Dict[str, Any]) -> str:
    o, c, h, l, v = it.get("o", 0.0), it.get("c", 0.0), it.get("h", 0.0), it.get("l", 0.0), it.get("v", 0.0)
    pct = ( (c - o) / o * 100.0 ) if (o) else 0.0
    return f"• <b>{sym}</b> @{_fmt_num(c, 2)} — pct {_fmt_num(pct,1)}%, close near {'H' if h and c>=0.98*h else ('L' if l and c<=1.02*l else 'mid')}, Vol {_short_vol(v)}"

def build_movers_text(date_used: str, items: List[Dict[str, Any]]) -> str:
    # เอาเฉพาะขาขึ้นตามเกณฑ์ และเรียง pct จากมากไปน้อย
    rows = []
    for it in items:
        if not qualify(it):
            continue
        o, c = it.get("o", 0.0), it.get("c", 0.0)
        pct = ( (c - o) / o * 100.0 ) if (o) else 0.0
        if pct >= MIN_PCT:
            rows.append((it.get("T", ""), pct, it))
    rows.sort(key=lambda x: x[1], reverse=True)
    rows = rows[:TOP_LIMIT]

    msg = [f"✅ <b>Top Movers</b> (โหมดฟรี, ข้อมูลอิงวัน: <code>{date_used}</code>)"]
    msg.append(f"เกณฑ์: pct ≥ {MIN_PCT:.1f}% | ราคา ≥ {MIN_PRICE} | Vol ≥ {MIN_VOL}")
    if not rows:
        msg.append("• ไม่มีรายการตามเกณฑ์")
        return "\n".join(msg)

    msg.append("\n📈 <b>ขึ้นแรง:</b>")
    for sym, pct, it in rows:
        c, v = it.get("c", 0.0), it.get("v", 0.0)
        msg.append(f"• {sym} +{_fmt_num(pct,1)}% @{_fmt_num(c,2)} Vol:{_short_vol(v)}")
    return "\n".join(msg)

def build_signals_text(date_used: str, groups: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> str:
    def short_list(arr: List[Tuple[str, Dict[str, Any]]]) -> str:
        syms = [s for s,_ in arr[:30]]
        return ", ".join(syms) if syms else "-"

    msg = [f"🔮 <b>คาดการณ์แนวโน้มวันนี้</b> (อิงจากข้อมูล <code>{date_used}</code>)"]
    msg.append(f"• <b>Momentum ขาขึ้น</b>: <u>Strong CALL {TOP_LIMIT}</u> — ตัวอย่าง: {short_list(groups['strong_call'])}")
    msg.append(f"• <b>ลุ้นเบรกขึ้น</b>: <u>Watch CALL {TOP_LIMIT}</u> — ตัวอย่าง: {short_list(groups['watch_call'])}")
    msg.append(f"• <b>Momentum ขาลง</b>: <u>Strong PUT {TOP_LIMIT}</u> — ตัวอย่าง: {short_list(groups['strong_put'])}")
    msg.append(f"• <b>ระวังอ่อนแรง</b>: <u>Watch PUT {TOP_LIMIT}</u> — ตัวอย่าง: {short_list(groups['watch_put'])}")

    msg.append("\n💡 <b>แนวคิด:</b>")
    msg.append("• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าจ่อจุดหนุน")
    msg.append("• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม")
    msg.append("• Strong PUT ลงต่อหรือรีบาวน์สั้น")
    msg.append("• Watch PUT ระวังหลุดแนวรับ")
    return "\n".join(msg)

def build_picks_text(date_used: str, picks: List[Tuple[str, Dict[str, Any], str]]) -> str:
    if not picks:
        return "ยังไม่มี pick จากเงื่อนไขวันนี้"
    msg = [f"🎯 <b>Picks (เข้า-ออกวันเดียว)</b> จากข้อมูล <code>{date_used}</code>"]
    for sym, it, side in picks:
        msg.append(f"{'🟢' if side=='CALL' else '🔴'} {line_from_item(sym, it)}")
    return "\n".join(msg)

def build_outlook_text(date_used: str, groups: Dict[str, List[Tuple[str, Dict[str, Any]]]]) -> str:
    sc, wc = len(groups["strong_call"]), len(groups["watch_call"])
    sp, wp = len(groups["strong_put"]), len(groups["watch_put"])
    bias = "กลาง"
    if (sc + wc) > (sp + wp) * 1.3:
        bias = "เป็นบวก (เอียง CALL)"
    elif (sp + wp) > (sc + wc) * 1.3:
        bias = "เป็นลบ (เอียง PUT)"
    msg = [
        f"🧭 <b>Outlook</b> (อิง <code>{date_used}</code>)",
        f"• Strong CALL: {sc} | Watch CALL: {wc}",
        f"• Strong PUT : {sp} | Watch PUT : {wp}",
        f"→ <b>โมเมนตัมรวม:</b> {bias}",
        "",
        "พอร์ตสั้นวันเดียว: ตามน้ำกลุ่ม Strong, รอจังหวะใน Watch",
    ]
    return "\n".join(msg)

# ------------ TELEGRAM HANDLERS ------------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "คำสั่งที่ใช้ได้\n"
        "• /movers – ดู Top Movers (โหมดฟรี)\n"
        "• /signals – จัดกลุ่ม Watch/Strong (CALL/PUT)\n"
        "• /outlook – มองภาพรวมโมเมนตัม\n"
        "• /picks – รายชื่อ pick สั้น ๆ เข้าออกวันเดียว\n"
        "• /ping – ทดสอบบอท\n\n"
        f"เกณฑ์: pct ≥ {MIN_PCT:.1f}%, ราคา ≥ {MIN_PRICE}, Vol ≥ {MIN_VOL}"
    )
    await update.message.reply_text(text)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def ensure_market() -> Tuple[str, List[Dict[str, Any]], str]:
    date_used, items, err = load_market()
    return date_used, items, err

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...")
    date_used, items, err = await ensure_market()
    if err:
        await m.edit_text(f"ขออภัย: {err}")
        return
    await m.edit_text(build_movers_text(date_used, items), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...")
    date_used, items, err = await ensure_market()
    if err:
        await m.edit_text(f"ขออภัย: {err}")
        return
    groups = classify(items)
    await m.edit_text(build_signals_text(date_used, groups), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...")
    date_used, items, err = await ensure_market()
    if err:
        await m.edit_text(f"ขออภัย: {err}")
        return
    groups = classify(items)
    await m.edit_text(build_outlook_text(date_used, groups), parse_mode=ParseMode.HTML)

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("⏳ คัดรายการจากสัญญาณ...")
    date_used, items, err = await ensure_market()
    if err:
        await m.edit_text(f"ขออภัย: {err}")
        return
    groups = classify(items)
    picks = picks_from_groups(groups)
    await m.edit_text(build_picks_text(date_used, picks), parse_mode=ParseMode.HTML)

def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Missing BOT_TOKEN env.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("movers", cmd_movers))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("outlook", cmd_outlook))
    app.add_handler(CommandHandler("picks", cmd_picks))
    return app

tele_app: Application = build_app()

# ------------ RUN TELEGRAM (LONG-POLLING IN THREAD) ------------
def start_telegram_polling():
    """
    แยกเธรด+อีเวนต์ลูปใหม่ เพื่อเลี่ยง RuntimeError 'no current event loop'
    เมื่อรันคู่กับ Flask บน Render
    """
    log.info("Starting telegram long-polling…")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(
            tele_app.run_polling(stop_signals=None, close_loop=True)
        )
    except Exception as e:
        log.exception("Polling crashed")
    finally:
        try:
            loop.stop()
            loop.close()
        except Exception:
            pass

# ------------ FLASK HEALTH (RENDER) ------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is running! (health OK)"

# ------------ MAIN ------------
if __name__ == "__main__":
    # start telegram in background thread
    Thread(target=start_telegram_polling, daemon=True).start()

    # run flask (blocking)
    port = int(os.environ.get("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port)
