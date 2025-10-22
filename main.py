# Stock-signal bot (Render friendly, async)
# Features: /movers /signals /outlook /picks /setrules /rules /subscribe /unsubscribe
# Data source: Polygon.io (free mode uses previous trading day)
# Env: BOT_TOKEN, POLYGON_API_KEY, CHAT_ID(optional), PORT

import os
import io
import re
import json
import math
import time
import asyncio
import logging
import datetime as dt
from typing import List, Dict, Tuple, Optional, Set

import requests
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# =========================
# ENV & CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "").strip()
DEFAULT_CHAT_ID = os.getenv("CHAT_ID", "").strip()  # optional broadcast default
PORT = int(os.environ.get("PORT", 10000))

# runtime rules (ปรับได้ผ่าน /setrules)
RULES = {
    "pct_min": 10.0,    # ขั้นต่ำ %change
    "price_min": 0.30,  # ขั้นต่ำราคา
    "vol_min": 0,       # ขั้นต่ำปริมาณ
}

# subscribers (in-memory)
SUBSCRIBERS: Set[int] = set()
if DEFAULT_CHAT_ID.isdigit():
    SUBSCRIBERS.add(int(DEFAULT_CHAT_ID))

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("stock-signal-bot")

# =========================
# FLASK (healthcheck)
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "Stock-signal bot is alive ✅"


# =========================
# UTILS
# =========================
def last_trading_day_usa(today_utc: Optional[dt.date] = None) -> dt.date:
    """หา 'last trading day' แบบง่าย (จ.-ศ.)"""
    d = today_utc or dt.datetime.utcnow().date()
    d -= dt.timedelta(days=1)
    while d.weekday() > 4:  # 5,6 = Sat, Sun
        d -= dt.timedelta(days=1)
    return d

def fmt_num(n: Optional[float], p: int = 2) -> str:
    if n is None:
        return "-"
    try:
        s = f"{n:,.{p}f}"
        if p == 0:
            s = s.split(".")[0]
        return s
    except Exception:
        return str(n)

def near_high(c: float, h: float, tol: float = 0.02) -> bool:
    return h > 0 and (h - c) / h <= tol

def near_low(c: float, l: float, tol: float = 0.02) -> bool:
    return l > 0 and (c - l) / l <= tol

def bullish(o: float, c: float) -> bool:
    return c > o

def bearish(o: float, c: float) -> bool:
    return c < o

def parse_kv_args(text: str) -> Dict[str, float]:
    """แปลงสตริงรูปแบบ 'pct=8 price=0.5 vol=200000' เป็น dict"""
    out = {}
    for m in re.finditer(r"(\w+)\s*=\s*([0-9]*\.?[0-9]+)", text):
        k, v = m.group(1).lower(), float(m.group(2))
        out[k] = v
    return out


# =========================
# DATA (Polygon free mode)
# =========================
def fetch_polygon_grouped(date_: dt.date, retries: int = 3) -> Tuple[Optional[List[Dict]], str]:
    """ดึง grouped bars ของวันก่อนหน้า (free) พร้อม backoff"""
    if not POLYGON_API_KEY:
        return None, "ยังไม่ได้ตั้งค่า POLYGON_API_KEY ใน Render"

    url = (
        f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
        f"{date_.isoformat()}?adjusted=true&apiKey={POLYGON_API_KEY}"
    )
    delay = 2
    for i in range(retries):
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 429:
                if i < retries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                return None, "429 Too Many Requests (ชน rate limit – ลองใหม่อีกครั้งภายหลัง)"
            if r.status_code >= 400:
                return None, f"Polygon error {r.status_code}: {r.text[:200]}"
            data = r.json()
            results = data.get("results")
            if not results:
                return None, "ไม่มีผลลัพธ์จาก Polygon (อาจเป็นวันหยุด/ไม่มีข้อมูล)"
            return results, ""
        except Exception as e:
            if i < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return None, f"ดึงข้อมูล Polygon ล้มเหลว: {e}"
    return None, "ไม่ทราบสาเหตุ"

def filter_movers(rows: List[Dict]) -> List[Dict]:
    """คัดหุ้นตาม RULES"""
    pct_min = float(RULES["pct_min"])
    price_min = float(RULES["price_min"])
    vol_min = float(RULES["vol_min"])

    out = []
    for x in rows:
        try:
            sym = x.get("T")
            o = float(x.get("o", 0))
            h = float(x.get("h", 0))
            l = float(x.get("l", 0))
            c = float(x.get("c", 0))
            v = float(x.get("v", 0))
            if c <= 0 or h <= 0 or l <= 0:
                continue
            pct = (c - o) / o * 100 if o > 0 else 0.0
            if sym and c >= price_min and v >= vol_min and abs(pct) >= pct_min:
                out.append(dict(T=sym, o=o, h=h, l=l, c=c, v=v, pct=pct))
        except Exception:
            continue
    out.sort(key=lambda r: abs(r["pct"]), reverse=True)
    return out

def classify_signals(rows: List[Dict]) -> Dict[str, List[Dict]]:
    """จัดกลุ่ม Strong/Watch — CALL/PUT"""
    buckets = {"STRONG_CALL": [], "WATCH_CALL": [], "STRONG_PUT": [], "WATCH_PUT": []}
    for r in rows:
        o, h, l, c, pct = r["o"], r["h"], r["l"], r["c"], r["pct"]
        if pct >= 15 and near_high(c, h, 0.02) and bullish(o, c):
            buckets["STRONG_CALL"].append(r)
        elif pct >= 5 and near_high(c, h, 0.02):
            buckets["WATCH_CALL"].append(r)
        elif pct <= -15 and near_low(c, l, 0.02) and bearish(o, c):
            buckets["STRONG_PUT"].append(r)
        elif pct <= -5 and near_low(c, l, 0.02):
            buckets["WATCH_PUT"].append(r)
    for k in buckets:
        buckets[k] = buckets[k][:50]
    return buckets

def score_daytrade_call(r: Dict) -> float:
    """ให้คะแนนเข้า CALL intraday อย่างง่าย"""
    # ใกล้ High + เขียว + pct สูง + volume สูง
    s = 0.0
    if bullish(r["o"], r["c"]): s += 1.0
    if near_high(r["c"], r["h"], 0.015): s += 1.0
    s += max(0.0, r["pct"]/10)               # 10% => +1
    s += math.log10(max(r["v"], 1)) / 10.0   # วอลุ่มช่วยเล็กน้อย
    return s

def score_daytrade_put(r: Dict) -> float:
    """ให้คะแนนเข้า PUT intraday อย่างง่าย"""
    s = 0.0
    if bearish(r["o"], r["c"]): s += 1.0
    if near_low(r["c"], r["l"], 0.015): s += 1.0
    s += max(0.0, (-r["pct"])/10)            # -10% => +1
    s += math.log10(max(r["v"], 1)) / 10.0
    return s

def format_symbol_line(r: Dict) -> str:
    return f"• <b>{r['T']}</b> @{fmt_num(r['c'],2)} — pct {fmt_num(r['pct'],1)}%, Vol {fmt_num(r['v'],0)}"

def fmt_group(title: str, rows: List[Dict]) -> str:
    if not rows:
        return f"{title}\n(ว่าง)"
    return title + "\n" + "\n".join(format_symbol_line(r) for r in rows)


# =========================
# TELEGRAM COMMANDS
# =========================
def rules_text() -> str:
    return (f"เกณฑ์ที่ใช้: pct ≥ {RULES['pct_min']:.1f}%, "
            f"ราคา ≥ {RULES['price_min']}, Vol ≥ {fmt_num(RULES['vol_min'],0)}")

HELP_TEXT = (
    "🤖 <b>Stock Signal Bot</b>\n"
    "คำสั่งที่ใช้ได้:\n"
    "• /movers – ดู Top Movers (ฟรี: ข้อมูลเมื่อวาน)\n"
    "• /signals – จัดกลุ่ม Strong/Watch (CALL/PUT)\n"
    "• /outlook – ภาพรวมแนวโน้มวันนี้\n"
    "• /picks – คัดหุ้นน่าเล่นเข้า–ออกวันเดียว (CALL/PUT อย่างละ 5)\n"
    "• /setrules pct=8 price=0.5 vol=200000 – ปรับเกณฑ์คัดกรอง\n"
    "• /rules – ดูเกณฑ์ปัจจุบัน\n"
    "• /subscribe – ติดตามสรุปอัตโนมัติ\n"
    "• /unsubscribe – ยกเลิกสรุปอัตโนมัติ\n"
    f"\n{rules_text()}"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("สวัสดีครับ 👋\n" + HELP_TEXT)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(HELP_TEXT)

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(rules_text())

async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = " ".join(context.args) if context.args else ""
    if not args and update.message:
        # รองรับกรอกต่อท้ายแบบไม่มีช่อง args
        parts = update.message.text.split(maxsplit=1)
        if len(parts) == 2:
            args = parts[1]
    if not args:
        await update.message.reply_text("วิธีใช้: /setrules pct=8 price=0.5 vol=200000")
        return
    kv = parse_kv_args(args)
    changed = []
    if "pct" in kv or "pct_min" in kv:
        RULES["pct_min"] = float(kv.get("pct", kv.get("pct_min")))
        changed.append(f"pct={RULES['pct_min']}")
    if "price" in kv or "price_min" in kv:
        RULES["price_min"] = float(kv.get("price", kv.get("price_min")))
        changed.append(f"price={RULES['price_min']}")
    if "vol" in kv or "vol_min" in kv:
        RULES["vol_min"] = float(kv.get("vol", kv.get("vol_min")))
        changed.append(f"vol={fmt_num(RULES['vol_min'],0)}")
    if not changed:
        await update.message.reply_text("ไม่พบคีย์ที่ปรับได้ (pct, price, vol)")
        return
    await update.message.reply_text("อัปเดตรูปแบบเรียบร้อย: " + ", ".join(changed))

async def _load_rows_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[List[Dict]]:
    ref_day = last_trading_day_usa()
    await update.message.reply_html(f"⌛️ กำลังดึงข้อมูลโหมดฟรีจาก Polygon (อิง {ref_day})…")
    rows, err = fetch_polygon_grouped(ref_day)
    if err:
        await update.message.reply_text(f"ขออภัย: {err}")
        return None
    movers = filter_movers(rows)
    if not movers:
        await update.message.reply_text("ไม่เจอหุ้นตามเกณฑ์")
        return None
    context.user_data["ref_day"] = ref_day
    return movers

async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movers = await _load_rows_and_reply(update, context)
    if movers is None: return
    ref_day = context.user_data.get("ref_day")
    up = [r for r in movers if r["pct"] > 0][:20]
    dn = [r for r in movers if r["pct"] < 0][:20]
    text = (
        f"✅ <b>Top Movers</b> (ฟรี, อิง {ref_day})\n"
        f"{rules_text()}\n\n"
        + fmt_group("📈 ขึ้นแรง:", up) + "\n\n"
        + fmt_group("📉 ลงแรง:", dn)
    )
    await update.message.reply_html(text, disable_web_page_preview=True)

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movers = await _load_rows_and_reply(update, context)
    if movers is None: return
    ref_day = context.user_data.get("ref_day")
    buckets = classify_signals(movers)
    txt = [
        f"🔮 <b>คาดการณ์แนวโน้มวันนี้</b> (อิง {ref_day})",
        fmt_group("💚 <u>Strong CALL</u>", buckets["STRONG_CALL"]),
        fmt_group("💚 <u>Watch CALL</u>", buckets["WATCH_CALL"]),
        fmt_group("🔴 <u>Strong PUT</u>", buckets["STRONG_PUT"]),
        fmt_group("🔴 <u>Watch PUT</u>", buckets["WATCH_PUT"]),
        "\n💡 แนวคิด:\n"
        "• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าวอลุ่มหนุน\n"
        "• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม\n"
        "• Strong PUT ลงต่อหรือรีบาวน์สั้น\n"
        "• Watch PUT ระวังหลุดแนวรับ"
    ]
    await update.message.reply_html("\n\n".join(txt), disable_web_page_preview=True)

async def cmd_outlook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movers = await _load_rows_and_reply(update, context)
    if movers is None: return
    ref_day = context.user_data.get("ref_day")
    buckets = classify_signals(movers)
    c1 = len(buckets["STRONG_CALL"]); c2 = len(buckets["WATCH_CALL"])
    p1 = len(buckets["STRONG_PUT"]);  p2 = len(buckets["WATCH_PUT"])
    bias = "กลาง"
    if c1 + c2 > p1 + p2: bias = "เอียงบวก"
    elif c1 + c2 < p1 + p2: bias = "เอียงลบ"
    text = (
        f"📊 <b>Outlook</b> (อิง {ref_day})\n"
        f"• Strong CALL: {c1} | Watch CALL: {c2}\n"
        f"• Strong PUT: {p1} | Watch PUT: {p2}\n"
        f"→ <b>โมเมนตัมรวม:</b> {bias}\n\n"
        "พอร์ตสั้นวันเดียว: ตามน้ำกลุ่ม Strong, รอจังหวะใน Watch"
    )
    await update.message.reply_html(text)

async def cmd_picks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movers = await _load_rows_and_reply(update, context)
    if movers is None: return
    ref_day = context.user_data.get("ref_day")

    # จัดอันดับด้วย scoring
    ups = sorted([r for r in movers if r["pct"] > 0], key=score_daytrade_call, reverse=True)[:5]
    dns = sorted([r for r in movers if r["pct"] < 0], key=score_daytrade_put, reverse=True)[:5]

    def idea_call(r):
        return f"{r['T']} — ไอเดีย: เล่นเหนือ High เดิม {fmt_num(r['h'],2)} วาง cut ถ้าหลุด {fmt_num(r['o'],2)}"
    def idea_put(r):
        return f"{r['T']} — ไอเดีย: เล่นหลุด Low เดิม {fmt_num(r['l'],2)} รีบาวน์ไม่ผ่าน {fmt_num(r['o'],2)} คัท"

    text = (
        f"🎯 <b>Day-trade Picks</b> (อิง {ref_day})\n{rules_text()}\n\n"
        "💚 <u>CALL candidates</u>\n" +
        "\n".join([f"• {idea_call(r)} | pct {fmt_num(r['pct'],1)}%, Vol {fmt_num(r['v'],0)}" for r in ups]) +
        "\n\n🔴 <u>PUT candidates</u>\n" +
        "\n".join([f"• {idea_put(r)} | pct {fmt_num(r['pct'],1)}%, Vol {fmt_num(r['v'],0)}" for r in dns]) +
        "\n\n⚠️ เป็นสถิติจากวันก่อนหน้า — ใช้ประกอบการตัดสินใจและวางแผนความเสี่ยงเองด้วยครับ"
    )
    await update.message.reply_html(text, disable_web_page_preview=True)

# --- subscribe / unsubscribe + jobqueue broadcast ---
async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    SUBSCRIBERS.add(chat_id)
    await update.message.reply_text("บันทึกแล้วครับ — จะส่งสรุปให้เมื่อมีการเรียกใช้งาน/ตามรอบงาน")

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in SUBSCRIBERS:
        SUBSCRIBERS.remove(chat_id)
        await update.message.reply_text("ยกเลิกการส่งสรุปอัตโนมัติแล้วครับ")
    else:
        await update.message.reply_text("ยังไม่ได้สมัครไว้ครับ")

async def job_broadcast(context: ContextTypes.DEFAULT_TYPE):
    """งานส่งสรุปสั้นๆ ให้ผู้ติดตามทุก 60 นาที (ระหว่างคอนเทนเนอร์ยังรัน)"""
    if not SUBSCRIBERS:
        return
    ref_day = last_trading_day_usa()
    rows, err = fetch_polygon_grouped(ref_day)
    if err:
        return
    movers = filter_movers(rows)
    if not movers:
        return
    buckets = classify_signals(movers)
    c1, c2 = len(buckets["STRONG_CALL"]), len(buckets["WATCH_CALL"])
    p1, p2 = len(buckets["STRONG_PUT"]), len(buckets["WATCH_PUT"])
    bias = "กลาง"
    if c1 + c2 > p1 + p2: bias = "เอียงบวก"
    elif c1 + c2 < p1 + p2: bias = "เอียงลบ"
    msg = (f"⏰ อัปเดตอัตโนมัติ (อิง {ref_day}) — Strong CALL {c1}, Watch CALL {c2}, "
           f"Strong PUT {p1}, Watch PUT {p2} → โทน {bias}")
    for cid in list(SUBSCRIBERS):
        try:
            await context.bot.send_message(cid, msg)
        except Exception:
            # ถ้าส่งไม่เข้า (เช่น user block) ก็ลบทิ้ง
            SUBSCRIBERS.discard(cid)

# =========================
# RUN TELEGRAM (async)
# =========================
async def run_telegram():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set in environment")
        return
    app_tg = ApplicationBuilder().token(BOT_TOKEN).build()

    app_tg.add_handler(CommandHandler("start", cmd_start))
    app_tg.add_handler(CommandHandler("help", cmd_help))
    app_tg.add_handler(CommandHandler("rules", cmd_rules))
    app_tg.add_handler(CommandHandler("setrules", cmd_setrules))
    app_tg.add_handler(CommandHandler("movers", cmd_movers))
    app_tg.add_handler(CommandHandler("signals", cmd_signals))
    app_tg.add_handler(CommandHandler("outlook", cmd_outlook))
    app_tg.add_handler(CommandHandler("picks", cmd_picks))
    app_tg.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app_tg.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))

    # jobqueue ส่งสรุปสั้นทุก 60 นาที (ปรับได้)
    app_tg.job_queue.run_repeating(job_broadcast, interval=60*60, first=60*5)

    log.info("Starting telegram long-polling…")
    await app_tg.run_polling(stop_signals=None)

# =========================
# ENTRYPOINT
# =========================
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(run_telegram())
    app.run(host="0.0.0.0", port=PORT, debug=False)
