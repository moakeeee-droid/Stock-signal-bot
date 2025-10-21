# -*- coding: utf-8 -*-
# Stock Momentum & Signal Bot (Free mode, Polygon.io + Telegram + Webhook commands)
# - โหมดฟรี: ดึง "วันทำการล่าสุด" ที่เข้าถึงได้
# - คำสั่ง Telegram:
#   /start, /help  : แสดงเมนูคำสั่ง
#   /movers        : สรุป Top Movers (ฟรี)
#   /signals       : จัดกลุ่ม Watch/Strong (CALL/PUT)
#   /outlook       : คาดการณ์โมเมนตัมวันนี้ (อิงเมื่อวาน)
#
# จำเป็นต้องตั้ง ENV บน Render:
#   BOT_TOKEN, CHAT_ID (ถ้าอยากจำกัดแชทเดียวให้ตอบ), POLYGON_API_KEY, PUBLIC_URL(=Primary URL ของ Render เช่น https://stock-signal-bot-1.onrender.com)
#   PORT=10000 (หรือค่าที่ใช้อยู่)
#
# วิธีตั้ง Webhook (ทำครั้งแรกครั้งเดียว):
#   เปิดเบราว์เซอร์ไปที่: https://<PRIMARY_DOMAIN>/set-webhook
#   หากสำเร็จ จะมีข้อความ "Webhook set OK"

import os
import math
import json
import time
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

# ========= ENV =========
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
CHAT_ID_LIMIT    = os.environ.get("CHAT_ID", "")   # ถ้าอยากให้ตอบเฉพาะห้อง, ใส่ chat id; ถ้าเว้นว่างจะตอบได้ทุกห้อง
POLYGON_API_KEY  = os.environ.get("POLYGON_API_KEY", "")
PUBLIC_URL       = os.environ.get("PUBLIC_URL", "")  # เช่น https://stock-signal-bot-1.onrender.com
PORT             = int(os.environ.get("PORT", "10000"))

# ======== Params (ปรับได้) ========
ALERT_PCT   = float(os.environ.get("ALERT_PCT", "10.0"))
MIN_PRICE   = float(os.environ.get("MIN_PRICE", "0.30"))
MIN_VOLUME  = int(os.environ.get("MIN_VOLUME", "0"))
TOP_N_SHOW  = int(os.environ.get("TOP_N_SHOW", "30"))

# ======== Telegram helpers ========
def send_message(text: str, chat_id: str=None):
    """ส่งข้อความไป Telegram"""
    if not BOT_TOKEN:
        print("⚠️ Missing BOT_TOKEN")
        return
    if chat_id is None:
        chat_id = CHAT_ID_LIMIT or ""
    if not chat_id:
        print("⚠️ Missing CHAT_ID to send")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=20)
        if r.status_code != 200:
            print("TG send:", r.status_code, r.text[:300])
    except Exception as e:
        print("TG error:", e)

def fmt_num(n):
    try:
        if n >= 1_000_000_000:
            return f"{n/1_000_000_000:.2f}B"
        if n >= 1_000_000:
            return f"{n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n/1_000:.2f}K"
        return f"{n:.0f}"
    except:
        return str(n)

# ======== Date helper ========
def previous_us_trading_day(from_utc=None):
    if from_utc is None:
        from_utc = datetime.utcnow()
    d = from_utc.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

# ======== Polygon fetch (โหมดฟรี auto fallback) ========
def fetch_polygon_grouped(date_str):
    attempts = 0
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    while attempts < 5:
        url = (f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{d}"
               f"?adjusted=true&apiKey={POLYGON_API_KEY}")
        print("GET", url)
        r = requests.get(url, timeout=30)
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code == 200 and data.get("results"):
            return d.strftime("%Y-%m-%d"), data["results"]

        msg = (data.get("message") or "").lower()
        if "attempted to request today's data before end of day" in msg:
            d = d - timedelta(days=1)
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            attempts += 1
            continue

        d = d - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        attempts += 1

    return None, []

# ======== Analysis ========
def classify_row(row):
    try:
        sym  = row.get("T", "")
        o    = float(row.get("o", 0.0))
        c    = float(row.get("c", 0.0))
        h    = float(row.get("h", 0.0))
        l    = float(row.get("l", 0.0))
        v    = int(row.get("v", 0))
    except Exception:
        return None

    if o <= 0 or c <= 0 or h <= 0 or l <= 0:
        return None
    if c < MIN_PRICE or v < MIN_VOLUME:
        return None

    pct = (c - o) / o * 100.0
    rng = max(h - l, 1e-6)
    close_near_high = (h - c) / rng <= 0.15
    close_near_low  = (c - l) / rng <= 0.15
    body            = abs(c - o)
    strong_body     = body / rng >= 0.60

    label = None
    if pct >= ALERT_PCT:
        if close_near_high and strong_body:
            label = "Strong CALL"
        else:
            label = "Watch CALL"
    elif pct <= -ALERT_PCT:
        if close_near_low and strong_body:
            label = "Strong PUT"
        else:
            label = "Watch PUT"

    return {
        "symbol": sym,
        "o": o, "c": c, "h": h, "l": l, "v": v,
        "pct": pct,
        "close_near_high": close_near_high,
        "close_near_low": close_near_low,
        "strong_body": strong_body,
        "label": label
    }

def bucketize(results):
    buckets = {
        "Strong CALL": [],
        "Watch CALL":  [],
        "Strong PUT":  [],
        "Watch PUT":   [],
    }
    for r in results:
        if r and r.get("label"):
            buckets[r["label"]].append(r)

    for k in buckets:
        if "CALL" in k:
            buckets[k].sort(key=lambda x: (x["pct"], x["v"]), reverse=True)
        else:
            buckets[k].sort(key=lambda x: (abs(x["pct"]), x["v"]), reverse=True)
        buckets[k] = buckets[k][:TOP_N_SHOW]
    return buckets

def build_section(title_emoji, title_text, rows):
    if not rows:
        return ""
    lines = [f"{title_emoji} <u>{title_text}</u>"]
    for r in rows:
        lines.append(
            f"• <b>{r['symbol']}</b> @{r['c']:.2f} — pct "
            f"{'+' if r['pct']>=0 else ''}{r['pct']:.1f}%, "
            f"close {'near H' if r['close_near_high'] else ('near L' if r['close_near_low'] else '')}, "
            f"${r['c']:.2f}, Vol {fmt_num(r['v'])}"
        )
    return "\n".join(lines)

def summarize_today_forecast(buckets):
    strong_call = [r["symbol"] for r in buckets.get("Strong CALL", [])][:10]
    watch_call  = [r["symbol"] for r in buckets.get("Watch CALL",  [])][:10]
    strong_put  = [r["symbol"] for r in buckets.get("Strong PUT",  [])][:10]
    watch_put   = [r["symbol"] for r in buckets.get("Watch PUT",   [])][:10]

    tc = len(buckets.get("Strong CALL", []))
    wc = len(buckets.get("Watch CALL",  []))
    tp = len(buckets.get("Strong PUT", []))
    wp = len(buckets.get("Watch PUT",  []))

    lines = []
    lines.append("🔮 <b>คาดการณ์แนวโน้มวันนี้</b> (อิงจากข้อมูลเมื่อวาน)")
    lines.append(f"• Momentum <b>ขาขึ้น</b>: Strong CALL {tc} — ตัวอย่าง: " + (", ".join(strong_call) or "-"))
    lines.append(f"• <b>ลุ้นเบรกขึ้น</b>: Watch CALL {wc} — ตัวอย่าง: "   + (", ".join(watch_call)  or "-"))
    lines.append(f"• Momentum <b>ขาลง</b>: Strong PUT {tp} — ตัวอย่าง: "  + (", ".join(strong_put)  or "-"))
    lines.append(f"• <b>ระวังอ่อนแรง</b>: Watch PUT {wp} — ตัวอย่าง: "   + (", ".join(watch_put)   or "-"))
    lines.append("")
    lines.append("💡 แนวคิด:")
    lines.append("• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าวอลุ่มหนุน")
    lines.append("• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม")
    lines.append("• Strong PUT ลงต่อหรือรีบาวน์สั้น")
    lines.append("• Watch PUT ระวังหลุดแนวรับ")
    return "\n".join(lines)

# ---------- Builders (ข้อความพร้อมส่ง) ----------
def build_reports():
    date_try = previous_us_trading_day()
    used_date, raw = fetch_polygon_grouped(date_try)
    if not used_date or not raw:
        return {"error": "no_data"}

    parsed = []
    for row in raw:
        info = classify_row(row)
        if info:
            parsed.append(info)

    movers = [x for x in parsed if abs(x["pct"]) >= ALERT_PCT]
    movers.sort(key=lambda x: (abs(x["pct"]), x["v"]), reverse=True)
    movers = movers[:TOP_N_SHOW]

    buckets = bucketize(parsed)

    # Top Movers
    lines_tm = []
    header = f"✅ <b>Top Movers</b> (ฟรี, ย้อนหลังวันล่าสุด)\nวันที่อ้างอิง: <b>{used_date}</b>\n" \
             f"เกณฑ์: ≥{ALERT_PCT:.1f}% | ราคา ≥{MIN_PRICE:.1f} | Vol ≥{MIN_VOLUME}\n"
    lines_tm.append(header)
    ups = [x for x in movers if x["pct"] > 0]
    downs = [x for x in movers if x["pct"] < 0]
    if ups:
        lines_tm.append("📈 <u>ขึ้นแรง:</u>")
        for r in ups:
            lines_tm.append(f"• {r['symbol']} +{r['pct']:.1f}% @{r['c']:.2f}  Vol:{fmt_num(r['v'])}")
    if downs:
        lines_tm.append("\n📉 <u>ลงแรง:</u>")
        for r in downs:
            lines_tm.append(f"• {r['symbol']} {r['pct']:.1f}% @{r['c']:.2f}  Vol:{fmt_num(r['v'])}")
    msg_top_movers = "\n".join(lines_tm)

    # Signals buckets
    msg_watch_call  = build_section("💚", "Watch CALL",  buckets.get("Watch CALL"))
    msg_strong_call = build_section("💚", "Strong CALL", buckets.get("Strong CALL"))
    msg_watch_put   = build_section("💔", "Watch PUT",   buckets.get("Watch PUT"))
    msg_strong_put  = build_section("💔", "Strong PUT",  buckets.get("Strong PUT"))

    # Outlook
    msg_outlook = summarize_today_forecast(buckets)

    return {
        "used_date": used_date,
        "top_movers": msg_top_movers,
        "watch_call": msg_watch_call,
        "strong_call": msg_strong_call,
        "watch_put": msg_watch_put,
        "strong_put": msg_strong_put,
        "outlook": msg_outlook
    }

# ======== Flask (Webhook & Health) ========
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running. Use /set-webhook to register Telegram webhook."

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat()})

@app.route("/set-webhook")
def set_webhook():
    """กดครั้งเดียวเพื่อตั้ง webhook ให้ Telegram"""
    if not PUBLIC_URL or not BOT_TOKEN:
        return "Missing PUBLIC_URL or BOT_TOKEN", 400
    webhook_url = f"{PUBLIC_URL}/webhook/{BOT_TOKEN}"
    r = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
        params={"url": webhook_url}, timeout=20
    )
    try:
        data = r.json()
    except:
        data = {"raw": r.text}
    if data.get("ok"):
        return f"Webhook set OK: {webhook_url}"
    return f"Set webhook failed: {data}", 500

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    """รับอัพเดตจาก Telegram แล้วแยกคำสั่ง"""
    payload = request.get_json(force=True, silent=True) or {}
    msg = payload.get("message") or payload.get("edited_message") or {}
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))

    # จำกัดห้อง (ถ้าตั้ง CHAT_ID_LIMIT)
    if CHAT_ID_LIMIT and chat_id and chat_id != str(CHAT_ID_LIMIT):
        # ข้ามเงียบ ๆ หรือจะส่งปฏิเสธก็ได้
        return jsonify({"ok": True})

    text = (msg.get("text") or "").strip()
    if not text:
        return jsonify({"ok": True})

    # normalize
    cmd = text.split()[0].lower()

    if cmd in ("/start", "/help"):
        menu = (
            "👋 ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\n"
            "<b>คำสั่งที่ใช้ได้</b>\n"
            "• /movers – ดู Top Movers (ฟรี)\n"
            "• /signals – จัดกลุ่ม Watch/Strong (CALL/PUT)\n"
            "• /outlook – คาดการณ์โมเมนตัมวันนี้ (อิงเมื่อวาน)\n"
            "• /help – ดูเมนูนี้อีกครั้ง\n"
            f"\nเกณฑ์: pct ≥ {ALERT_PCT:.1f}%, ราคา ≥ {MIN_PRICE:.2f}, Vol ≥ {MIN_VOLUME}"
        )
        send_message(menu, chat_id)
        return jsonify({"ok": True})

    if cmd == "/movers" or cmd == "/signals" or cmd == "/outlook":
        send_message("⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon…", chat_id)
        reports = build_reports()
        if reports.get("error"):
            send_message("❌ ไม่พบข้อมูลจาก Polygon (ฟรี) — ลองใหม่พรุ่งนี้ครับ", chat_id)
            return jsonify({"ok": True})

        if cmd == "/movers":
            send_message(reports["top_movers"], chat_id)
        elif cmd == "/signals":
            for part in [reports["watch_call"], reports["strong_call"], reports["watch_put"], reports["strong_put"]]:
                if part:
                    send_message(part, chat_id)
        elif cmd == "/outlook":
            send_message(reports["outlook"], chat_id)

        return jsonify({"ok": True})

    # ค่าอื่น ๆ
    send_message("พิมพ์ /help เพื่อดูคำสั่งที่ใช้ได้ครับ", chat_id)
    return jsonify({"ok": True})

# ======== Optional: push รายงานหนึ่งรอบเมื่อสตาร์ท ========
def push_once_on_start():
    try:
        reports = build_reports()
        if reports.get("error"):
            send_message("❌ ไม่พบข้อมูลจาก Polygon (ฟรี) — ลองใหม่พรุ่งนี้ครับ")
            return
        # ส่งสรุปครบชุดเหมือนเดิม
        send_message("🟢 เริ่มทำงานโหมดฟรี (ดึงข้อมูลวันทำการล่าสุดจาก Polygon)")
        send_message(reports["top_movers"])
        for part in [reports["watch_call"], reports["strong_call"], reports["watch_put"], reports["strong_put"]]:
            if part:
                send_message(part)
        send_message(reports["outlook"])
    except Exception as e:
        print("Push-on-start error:", e)
        send_message(f"❗️Scanner error: {e}")

if __name__ == "__main__":
    # ส่งรายงานรอบเดียวตอนสตาร์ท (อยากปิดก็คอมเมนต์บรรทัดนี้)
    push_once_on_start()
    # เปิดเว็บเซิร์ฟเวอร์รับ webhook/health
    app.run(host="0.0.0.0", port=PORT)
