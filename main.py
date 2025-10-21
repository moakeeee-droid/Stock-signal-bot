# main.py  — Stock Signal Bot (Polygon free mode + Telegram commands + Webhook/Flask)
import os
import json
import time
import math
import queue
import threading
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify

# ========= ENV =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID_DEFAULT = os.getenv("CHAT_ID", "").strip()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
ET = ZoneInfo("America/New_York")

# ========= Utils =========
def _now_et():
    return datetime.now(tz=ET)

def _yesterday_et():
    """คืนค่า YYYY-MM-DD ของ 'วันทำการก่อนหน้า' สำหรับ Polygon free (prev day grouped)"""
    d = _now_et().date() - timedelta(days=1)
    # ถ้าวันเสาร์/อาทิตย์ ให้เลื่อนกลับไปวันศุกร์
    while d.weekday() >= 5:  # 5=Sat,6=Sun
        d -= timedelta(days=1)
    return d.isoformat()

def _fmt_num(x, nd=2):
    try:
        if x is None: return "-"
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)

def _close_near_high(o, c, h, tol=0.05):
    try:
        if h is None or c is None or h == 0: return False
        return (h - c) / h <= tol
    except Exception:
        return False

def _close_near_low(o, c, l, tol=0.05):
    try:
        if l is None or c is None: return False
        diff = c - l
        rng = max(1e-9, (c if c>l else l))
        return diff / rng <= tol
    except Exception:
        return False

def _body_strong(o, c, h, l):
    try:
        rng = max(1e-9, h - l)
        body = abs(c - o)
        return body / rng >= 0.6  # real-body >=60% ของช่วงทั้งวัน
    except Exception:
        return False

def _pct_change(o, c):
    try:
        if not o: return 0.0
        return (c - o) / o * 100.0
    except Exception:
        return 0.0

def tg_send_text(text, chat_id=None, disable_web_page_preview=True):
    cid = str(chat_id or CHAT_ID_DEFAULT).strip()
    if not BOT_TOKEN or not cid:
        print("Telegram config missing.")
        return
    try:
        r = requests.post(
            TG_API + "/sendMessage",
            json={
                "chat_id": int(cid),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_web_page_preview,
            },
            timeout=20,
        )
        if r.status_code != 200:
            print("TG send:", r.status_code, r.text)
    except Exception as e:
        print("TG error:", e)

# ========= Polygon (free) =========
def polygon_grouped_prevday(date_iso: str):
    """
    ใช้ endpoint ฟรี: /v2/aggs/grouped/locale/us/market/stocks/{date}
    คืน list ของ dict: {T, v, o, c, h, l, ...}
    """
    url = (
        f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_iso}"
        f"?adjusted=true&apiKey={POLYGON_API_KEY}"
    )
    r = requests.get(url, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"Polygon HTTP {r.status_code}: {r.text}")
    data = r.json()
    if data.get("status") != "OK":
        # free plan: ถ้าเรียกวันปัจจุบันจะได้ NOT_AUTHORIZED
        raise RuntimeError(json.dumps(data))
    return data.get("results", [])

# ========= Classifier =========
def classify_signals_from_grouped(results, min_price=0.30, min_vol=0):
    """
    จัดสัญญาณ 4 กลุ่ม:
      Strong CALL, Watch CALL, Strong PUT, Watch PUT
    เกณฑ์เน้นเข้าใจง่ายและไม่พึ่งข้อมูลเรียลไทม์ (ยึด prev-day)
    """
    strong_call, watch_call, strong_put, watch_put = [], [], [], []

    for item in results:
        s = item.get("T")   # symbol
        v = float(item.get("v", 0))
        o = float(item.get("o", 0))
        c = float(item.get("c", 0))
        h = float(item.get("h", 0))
        l = float(item.get("l", 0))
        if not s or c <= 0 or c < min_price or v < min_vol or o <= 0:
            continue

        pct = _pct_change(o, c)
        note = []

        # คุณสมบัติเพิ่มเติมสำหรับข้อความประกอบ
        if _close_near_high(o, c, h): note.append("close near H")
        if _close_near_low(o, c, l):  note.append("close near L")
        if _body_strong(o, c, h, l):  note.append("strong body")

        # Heuristics
        if pct >= 15.0:
            # เขียวแรง
            if _close_near_high(o, c, h) or _body_strong(o, c, h, l):
                strong_call.append((s, pct, c, v, ", ".join(note)))
            else:
                watch_call.append((s, pct, c, v, ", ".join(note)))
        elif 7.0 <= pct < 15.0:
            watch_call.append((s, pct, c, v, ", ".join(note)))

        if pct <= -15.0:
            if _close_near_low(o, c, l) or _body_strong(o, c, h, l):
                strong_put.append((s, pct, c, v, ", ".join(note)))
            else:
                watch_put.append((s, pct, c, v, ", ".join(note)))
        elif -15.0 < pct <= -7.0:
            watch_put.append((s, pct, c, v, ", ".join(note)))

    # เรียงลำดับเพื่อแสดงสวย ๆ
    strong_call.sort(key=lambda x: (-x[1], -x[3]))
    watch_call.sort(key=lambda x: (-x[1], -x[3]))
    strong_put.sort(key=lambda x: (x[1], -x[3]))   # pct ติดลบเยอะก่อน
    watch_put.sort(key=lambda x: (x[1], -x[3]))

    return {
        "strong_call": strong_call,
        "watch_call": watch_call,
        "strong_put": strong_put,
        "watch_put": watch_put,
    }

# ========= Feature: Movers (free) =========
def do_movers_free(chat_id=None, min_pct=10.0, min_price=0.30, min_vol=0):
    ref = _yesterday_et()
    tg_send_text("🕰️ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...", chat_id)
    results = polygon_grouped_prevday(ref)

    # คัดเฉพาะขึ้นแรง
    ups = []
    for it in results:
        s = it.get("T")
        v = float(it.get("v", 0))
        o = float(it.get("o", 0))
        c = float(it.get("c", 0))
        if not s or c < min_price or v < min_vol or o <= 0:
            continue
        pct = _pct_change(o, c)
        if pct >= min_pct:
            ups.append((s, pct, c, v))
    ups.sort(key=lambda x: (-x[1], -x[3]))

    lines = [f"✅ <b>Top Movers</b> (ฟรี, ย้อนหลังวันล่าสุด)\nวันที่อ้างอิง: {ref}\nเกณฑ์: ≥{min_pct:.1f}% | ราคา ≥{min_price} | Vol ≥{min_vol}\n\n📈 <b>ขาขึ้น:</b>"]
    if not ups:
        lines.append("• (ไม่พบ)")
    else:
        for s, pct, c, v in ups[:40]:
            lines.append(f"• <b>{s}</b> +{_fmt_num(pct,1)}% @{_fmt_num(c,2)}  Vol:{int(v):,}")

    tg_send_text("\n".join(lines), chat_id)

# ========= Feature: Signals (lists) =========
def do_signals(chat_id=None):
    ref = _yesterday_et()
    results = polygon_grouped_prevday(ref)
    groups = classify_signals_from_grouped(results)

    msgs = []
    msgs.append("🟢 <b>Strong CALL</b>\n" + "\n".join(
        [f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}, {n}" if n else
         f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}"
         for (s, p, c, v, n) in groups["strong_call"][:30]
        ] or ["• (ไม่มีตัวเด่น)"]
    ))
    msgs.append("🟢 <b>Watch CALL</b>\n" + "\n".join(
        [f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}, {n}" if n else
         f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}"
         for (s, p, c, v, n) in groups["watch_call"][:30]
        ] or ["• (ไม่มีตัวเด่น)"]
    ))
    msgs.append("🔴 <b>Strong PUT</b>\n" + "\n".join(
        [f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}, {n}" if n else
         f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}"
         for (s, p, c, v, n) in groups["strong_put"][:30]
        ] or ["• (ไม่มีตัวเด่น)"]
    ))
    msgs.append("🔴 <b>Watch PUT</b>\n" + "\n".join(
        [f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}, {n}" if n else
         f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}"
         for (s, p, c, v, n) in groups["watch_put"][:30]
        ] or ["• (ไม่มีตัวเด่น)"]
    ))

    tg_send_text("\n\n".join(msgs), chat_id)

# ========= Feature: Outlook (summary + examples) =========
def do_outlook(chat_id=None):
    ref = _yesterday_et()
    tg_send_text("⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...", chat_id)
    results = polygon_grouped_prevday(ref)
    g = classify_signals_from_grouped(results)

    def _eg(lst, n=12):
        return ", ".join([x[0] for x in lst[:n]]) if lst else "-"

    text = (
        "🔮 <b>คาดการณ์แนวโน้มวันนี้</b> (อิงจากข้อมูลเมื่อวาน)\n"
        f"• <b>Momentum ขาขึ้น:</b> Strong CALL 30 — ตัวอย่าง: { _eg(g['strong_call']) }\n"
        f"• <b>ลุ้นเบรกขึ้น:</b> Watch CALL 30 — ตัวอย่าง: { _eg(g['watch_call']) }\n"
        f"• <b>Momentum ขาลง:</b> Strong PUT 30 — ตัวอย่าง: { _eg(g['strong_put']) }\n"
        f"• <b>ระวังอ่อนแรง:</b> Watch PUT 30 — ตัวอย่าง: { _eg(g['watch_put']) }\n\n"
        "💡 <b>แนวคิด:</b>\n"
        "• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าจ่อจุดหนุน\n"
        "• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม\n"
        "• Strong PUT ลงต่อหรือรีบาวน์สั้น\n"
        "• Watch PUT ระวังหลุดแนวรับ"
    )
    tg_send_text(text, chat_id)

# ========= Feature: Picks (quick ideas)
def do_picks(chat_id=None):
    ref = _yesterday_et()
    results = polygon_grouped_prevday(ref)
    g = classify_signals_from_grouped(results)

    picks = []

    def _pick_side(title, lst, take=5, side="CALL"):
        if not lst:
            picks.append(f"{title}: -")
            return
        lines = []
        for s, pct, c, v, note in lst[:take]:
            reason = []
            if side == "CALL":
                if "close near H" in (note or ""): reason.append("close≈H")
                if "strong body" in (note or ""): reason.append("body▲")
                if pct >= 20: reason.append("mom▲")
            else:
                if "close near L" in (note or ""): reason.append("close≈L")
                if "strong body" in (note or ""): reason.append("body▼")
                if pct <= -20: reason.append("mom▼")
            lines.append(f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(pct,1)}%, Vol {int(v):,}  ({', '.join(reason)})")
        picks.append(f"{title}\n" + "\n".join(lines))

    _pick_side("✅ <b>ไอเดีย CALL</b> (เน้นแรงสุดเมื่อวาน)", g["strong_call"], side="CALL")
    _pick_side("🟡 <b>รอเบรก CALL</b>", g["watch_call"], side="CALL")
    _pick_side("⛔ <b>ไอเดีย PUT</b> (ลงแรงเมื่อวาน)", g["strong_put"], side="PUT")
    _pick_side("🔻 <b>ระวังอ่อนแรง PUT</b>", g["watch_put"], side="PUT")

    tg_send_text("\n\n".join(picks), chat_id)

# ========= Telegram Webhook router =========
def _handle_command(text: str, chat_id: str):
    t = (text or "").strip().lower()
    if t.startswith("/help"):
        tg_send_text(
            "👋 ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\n"
            "คำสั่งที่ใช้ได้\n"
            "• <b>/movers</b> – ดู Top Movers (ฟรี)\n"
            "• <b>/signals</b> – จัดกลุ่ม Watch/Strong (CALL/PUT)\n"
            "• <b>/outlook</b> – คาดการณ์โมเมนตัมวันนี้ (อิงเมื่อวาน)\n"
            "• <b>/picks</b> – ชุดไอเดียสั้น ๆ พร้อมเหตุผลประกอบ\n"
            f"\nเกณฑ์เริ่มต้น: pct ≥ 10.0%, ราคา ≥ 0.30, Vol ≥ 0\n", chat_id
        )
    elif t.startswith("/movers"):
        do_movers_free(chat_id)
    elif t.startswith("/signals"):
        do_signals(chat_id)
    elif t.startswith("/outlook"):
        do_outlook(chat_id)
    elif t.startswith("/picks") or t.startswith("/pick"):
        do_picks(chat_id)
    else:
        tg_send_text("พิมพ์ /help เพื่อดูคำสั่งที่ใช้ได้ครับ", chat_id)

# ========= Flask App =========
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running fine."

@app.route("/health")
def health():
    return jsonify(ok=True, time=str(datetime.utcnow()))

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg = data.get("message") or data.get("edited_message") or {}
        chat = msg.get("chat", {})
        text = msg.get("text", "")
        chat_id = str(chat.get("id", CHAT_ID_DEFAULT))
        # ถ้ากำหนด CHAT_ID ไว้ ให้ตอบเฉพาะห้องนั้น
        if CHAT_ID_DEFAULT and str(chat_id) != str(CHAT_ID_DEFAULT):
            # ไม่ตอบห้องอื่น
            return jsonify(status="ignored"), 200
        if text:
            _handle_command(text, chat_id)
    except Exception as e:
        print("webhook error:", e, traceback.format_exc())
    return jsonify(ok=True)

@app.route("/set-webhook")
def set_webhook():
    """เรียกสักครั้งหลัง deploy เพื่อชี้ Webhook → /telegram"""
    try:
        if not PUBLIC_URL:
            return "PUBLIC_URL is required", 400
        # base URL (กรณีผู้ใช้เผลอใส่เป็น .../set-webhook)
        base = PUBLIC_URL
        if base.endswith("/set-webhook") or base.endswith("/telegram"):
            base = base.rsplit("/", 1)[0]
        url = f"{base}/telegram"
        r = requests.get(f"{TG_API}/setWebhook", params={"url": url}, timeout=20)
        return f"setWebhook → {url} : {r.status_code} {r.text}"
    except Exception as e:
        return f"Error: {e}", 500

# ========= main =========
if __name__ == "__main__":
    print("Starting Flask on 0.0.0.0:", PORT)
    app.run(host="0.0.0.0", port=PORT)
