import os, time, json, traceback
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple
import requests
from flask import Flask, request, jsonify

# ========= ENV =========
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ.get("CHAT_ID", "").strip() or None
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip()  # https://<your>.onrender.com/set-webhook
PORT = int(os.environ.get("PORT", "10000"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
HEADERS = {"User-Agent": "stock-signal-bot/1.0"}

# ========= FLASK =========
app = Flask(__name__)

# ========= UTIL: Telegram =========
def tg_send_text(text: str, chat_id: str = None, disable_web_page_preview: bool = True):
    cid = chat_id or CHAT_ID
    if not cid:
        return
    data = {
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_web_page_preview,
    }
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=data, timeout=15)
    except Exception:
        pass

# ========= Polygon (โหมดฟรี: ย้อนหลังวันล่าสุด) =========
def polygon_grouped_prevday(date_iso: str) -> List[Dict]:
    """
    ดึงกลุ่มหุ้นทั้งหมดของ 'วันก่อนหน้า' (free plan)
    date_iso = YYYY-MM-DD ของ 'วันที่ต้องการเทียบ'—API จะให้ข้อมูลของวันก่อนหน้าจริง
    """
    url = (
        f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_iso}"
        f"?adjusted=true&apiKey={POLYGON_API_KEY}"
    )
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Polygon HTTP {r.status_code}: {r.text[:300]}")
    js = r.json()
    if js.get("status") != "OK":
        # ตัวอย่าง error free plan: NOT_AUTHORIZED when same-day
        raise RuntimeError(f"Polygon status: {js.get('status')} {js.get('message')}")
    # fields: T(sym), o,h,l,c,v, vw, n (#trades), etc.
    return js.get("results", [])

def _fmt_num(n, digits=2):
    try:
        return f"{float(n):.{digits}f}"
    except Exception:
        return str(n)

def _yesterday_et() -> str:
    # ตลาด US อ้างอิง America/New_York; ใช้ง่าย ๆ ด้วย UTC ลบ 4/5 ชม. แบบคร่าว ๆ
    # ใช้ yesterday UTC แล้วให้ API จัดการวันตลาดเอง (เพราะ endpoint เป็น "grouped prev day")
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

# ========= จัดกลุ่มสัญญาณจากข้อมูลวันก่อน =========
def classify_signals_from_grouped(results: List[Dict]) -> Dict[str, List[Tuple]]:
    """
    คืน dict: {
      'watch_call': [(sym, pct, close, vol, notes), ...],
      'strong_call': [...],
      'watch_put': [...],
      'strong_put': [...]
    }
    หลัก ๆ ใช้ % เปลี่ยนแปลง (c/o-1), close ใกล้ high, รูปแท่ง (body) ฯลฯ อย่างเรียบง่าย
    """
    out = {"watch_call": [], "strong_call": [], "watch_put": [], "strong_put": []}
    for it in results:
        sym = it.get("T")
        o, h, l, c = it.get("o"), it.get("h"), it.get("l"), it.get("c")
        v = it.get("v")
        if not all(x is not None for x in [o, h, l, c, v]): 
            continue
        if c <= 0 or v <= 0:
            continue

        pct = (c / o - 1.0) * 100.0 if o else 0.0
        # ความใกล้ high / low
        near_high = (h - c) <= max(0.01, 0.02 * c)   # ปิดใกล้ high
        near_low  = (c - l) <= max(0.01, 0.02 * c)   # ปิดใกล้ low
        strong_body = abs(c - o) >= 0.6 * (h - l) if (h - l) > 0 else False

        price_ok = c >= 0.30
        vol_ok = v >= 1e5   # 100k ชิ้น (หยาบ ๆ)

        if not (price_ok and vol_ok):
            continue

        note = []
        if near_high: note.append("close near H")
        if near_low:  note.append("close near L")
        if strong_body: note.append("strong body")
        notes = ", ".join(note) if note else ""

        row = (sym, pct, c, v, notes)

        # จัดเป็นกรุ๊ป
        if pct >= 7.0:
            # ฝั่ง CALL
            if near_high and strong_body:
                out["strong_call"].append(row)
            else:
                out["watch_call"].append(row)
        elif pct <= -7.0:
            # ฝั่ง PUT
            if near_low and strong_body:
                out["strong_put"].append(row)
            else:
                out["watch_put"].append(row)

    # คัดอันดับในแต่ละกอง (เรียงตาม % แรงสุดก่อน)
    for k in out.keys():
        out[k].sort(key=lambda x: x[1], reverse=("call" in k))
    return out

def format_group(title_icon: str, title: str, rows: List[Tuple], limit: int = 20) -> str:
    if not rows:
        return f"{title_icon} <u>{title}</u>\n• (ไม่มีตัวเด่นตามเงื่อนไข)\n"
    lines = [f"{title_icon} <u>{title}</u>"]
    for sym, pct, c, v, notes in rows[:limit]:
        line = f"• <b>{sym}</b> @{_fmt_num(c, 2)} — pct {_fmt_num(pct,1)}%, Vol {int(v):,}"
        if notes:
            line += f" ({notes})"
        lines.append(line)
    return "\n".join(lines) + "\n"

# ========= Intraday Picks (ดึงจากสัญญาณวันก่อนหน้า) =========
def make_intraday_picks(results: List[Dict]) -> Dict[str, List[str]]:
    """
    สร้างชุด 'Intraday picks' อย่างย่อ:
    - buy_the_dip: หุ้นฝั่ง CALL เมื่อวานแรง (strong_call) → ลุ้นจังหวะย่อตอนเช้า
    - breakout_watch: หุ้นฝั่ง CALL ที่ close near H (watch_call) → รอเบรก High เดิม
    - short_the_rip: หุ้นฝั่ง PUT เมื่อวานแรง (strong_put) → เด้งเช้าให้หาจังหวะ short
    - support_watch: หุ้นฝั่ง PUT (watch_put) → ระวังหลุดแนวรับ
    """
    groups = classify_signals_from_grouped(results)

    def pick_syms(rows: List[Tuple], n=12) -> List[str]:
        return [r[0] for r in rows[:n]]

    return {
        "buy_the_dip": pick_syms(groups["strong_call"]),
        "breakout_watch": pick_syms(groups["watch_call"]),
        "short_the_rip": pick_syms(groups["strong_put"]),
        "support_watch": pick_syms(groups["watch_put"]),
    }

def format_picks_block(picks: Dict[str, List[str]]) -> str:
    def line(name, syms, icon):
        if not syms: 
            return f"{icon} <u>{name}</u> — (ไม่มีตัวเข้าข่าย)\n"
        return f"{icon} <u>{name}</u>\n• " + ", ".join(syms) + "\n"

    txt = "🧭 <b>Intraday Picks (จากข้อมูลเมื่อวาน)</b>\n"
    txt += line("BUY the dip", picks.get("buy_the_dip", []), "🟢")
    txt += line("Breakout watch", picks.get("breakout_watch", []), "🟢")
    txt += line("SHORT the rip", picks.get("short_the_rip", []), "🔴")
    txt += line("Support watch", picks.get("support_watch", []), "🔴")
    txt += "\n💡ไอเดีย: <i>เลือก 3–6 ตัวที่คุ้นเคย / มีวอลุ่ม / สเปรดดี แล้วรอรูปแบบเข้าเทรดจริง</i>"
    return txt

# ========= Top Movers (ฟรี) =========
def make_movers(results: List[Dict], min_pct=10.0, min_price=0.30, min_vol=0):
    winners = []
    for it in results:
        sym = it.get("T")
        o, h, l, c = it.get("o"), it.get("h"), it.get("l"), it.get("c")
        v = it.get("v")
        if not all(x is not None for x in [o, h, l, c, v]):
            continue
        if c < min_price or v < min_vol:
            continue
        pct = (c / o - 1.0) * 100.0 if o else 0.0
        if pct >= min_pct:
            winners.append((sym, pct, c, v))
    winners.sort(key=lambda x: x[1], reverse=True)
    return winners

def format_movers_block(winners: List[Tuple], ref_date: str) -> str:
    if not winners:
        return "⚠️ ไม่พบ Top Movers ตรงตามเกณฑ์\n"
    lines = [f"✅ <b>Top Movers</b> (ฟรี, ย้อนหลังวันล่าสุด)\nวันที่อ้างอิง: <b>{ref_date}</b>\nเกณฑ์: ≥10.0% | ราคา ≥0.3 | Vol ≥0\n", "📈 <u>ขึ้นแรง:</u>"]
    for sym, pct, c, v in winners[:25]:
        lines.append(f"• {sym} +{_fmt_num(pct,1)}% @{_fmt_num(c,2)}  Vol:{int(v):,}")
    return "\n".join(lines)

# ========= Handlers =========
HELP_TEXT = (
    "👋 ยินดีต้อนรับสู่ Stock Signal Bot (โหมดฟรี)\n"
    "<b>คำสั่งที่ใช้ได้</b>\n"
    "• <b>/movers</b> – ดู Top Movers (ฟรี)\n"
    "• <b>/signals</b> – จัดกลุ่ม Watch/Strong (CALL/PUT)\n"
    "• <b>/outlook</b> – คาดการณ์โมเมนตัมวันนี้ (อิงเมื่อวาน)\n"
    "• <b>/picks</b> – อินทราเดย์พิคส์แบบสรุป (BUY dip / Breakout / SHORT / Support)\n"
    "\nเกณฑ์เริ่มต้น: pct ≥ 10.0%, ราคา ≥ 0.30, Vol ≥ 0"
)

def do_movers(chat_id=None):
    ref = _yesterday_et()
    results = polygon_grouped_prevday(ref)
    winners = make_movers(results)
    tg_send_text(format_movers_block(winners, ref), chat_id)

def do_signals(chat_id=None):
    ref = _yesterday_et()
    results = polygon_grouped_prevday(ref)
    groups = classify_signals_from_grouped(results)

    msgs = []
    msgs.append("🟢 <b>Strong CALL</b>\n" + "\n".join(
        [f"• <b>{s}</b} @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}, {n}" if n else
         f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}"
         for (s, p, c, v, n) in groups["strong_call"][:20]
        ] or ["• (ไม่มีตัวเด่น)"]
    ))
    msgs.append("🟢 <b>Watch CALL</b>\n" + "\n".join(
        [f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}, {n}" if n else
         f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}"
         for (s, p, c, v, n) in groups["watch_call"][:20]
        ] or ["• (ไม่มีตัวเด่น)"]
    ))
    msgs.append("🔴 <b>Strong PUT</b>\n" + "\n".join(
        [f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}, {n}" if n else
         f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}"
         for (s, p, c, v, n) in groups["strong_put"][:20]
        ] or ["• (ไม่มีตัวเด่น)"]
    ))
    msgs.append("🔴 <b>Watch PUT</b>\n" + "\n".join(
        [f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}, {n}" if n else
         f"• <b>{s}</b> @{_fmt_num(c,2)} — pct {_fmt_num(p,1)}%, Vol {int(v):,}"
         for (s, p, c, v, n) in groups["watch_put"][:20]
        ] or ["• (ไม่มีตัวเด่น)"]
    ))
    tg_send_text("\n\n".join(msgs), chat_id)

def do_outlook(chat_id=None):
    tg_send_text("⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...", chat_id)
    ref = _yesterday_et()
    results = polygon_grouped_prevday(ref)
    picks = make_intraday_picks(results)

    # ส่วนหัว + แนวคิด
    head = ("🔮 <b>คาดการณ์แนวโน้มวันนี้</b> (อิงจากข้อมูลเมื่อวาน)\n"
            "• <b>Momentum ขาขึ้น:</b> Strong CALL 30 — ตัวอย่าง: {A}\n"
            "• <b>ลุ้นเบรกขึ้น:</b> Watch CALL 30 — ตัวอย่าง: {B}\n"
            "• <b>Momentum ขาลง:</b> Strong PUT 30 — ตัวอย่าง: {C}\n"
            "• <b>ระวังอ่อนแรง:</b> Watch PUT 30 — ตัวอย่าง: {D}\n\n"
            "💡 แนวคิด:\n"
            "• Strong CALL มักเปิดบวก/ลุ้นทำ High ใหม่ ถ้าวอลุ่มหนุน\n"
            "• Watch CALL รอเบรก High เดิม + วอลุ่มเพิ่ม\n"
            "• Strong PUT ลงต่อหรือรีบาวน์สั้น\n"
            "• Watch PUT ระวังหลุดแนวรับ").format(
        A=", ".join(picks["buy_the_dip"][:12]) or "-",
        B=", ".join(picks["breakout_watch"][:12]) or "-",
        C=", ".join(picks["short_the_rip"][:12]) or "-",
        D=", ".join(picks["support_watch"][:12]) or "-",
    )

    tg_send_text(head, chat_id)

def do_picks(chat_id=None):
    """คำสั่งใหม่: /picks สรุปอินทราเดย์พิคส์อย่างย่อ"""
    tg_send_text("⏳ กำลังดึงข้อมูลโหมดฟรีจาก Polygon...", chat_id)
    ref = _yesterday_et()
    results = polygon_grouped_prevday(ref)
    picks = make_intraday_picks(results)
    tg_send_text(format_picks_block(picks), chat_id)

# ========= Telegram Webhook =========
def handle_command(cmd: str, chat_id: str):
    try:
        if cmd == "/start" or cmd == "/help":
            tg_send_text(HELP_TEXT, chat_id)
        elif cmd == "/movers":
            do_movers(chat_id)
        elif cmd == "/signals":
            do_signals(chat_id)
        elif cmd == "/outlook":
            do_outlook(chat_id)
        elif cmd == "/picks":
            do_picks(chat_id)
        else:
            tg_send_text("พิมพ์ /help เพื่อดูคำสั่งที่ใช้ได้ครับ", chat_id)
    except Exception as e:
        tg_send_text(f"⚠️ Error: {e}", chat_id)

@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    # ตั้ง webhook ให้บอท (ต้องตั้งค่า PUBLIC_URL ไว้แล้ว)
    if not PUBLIC_URL:
        return "Set PUBLIC_URL env first", 400
    url = PUBLIC_URL
    r = requests.get(f"{TELEGRAM_API}/setWebhook", params={"url": url}, timeout=15)
    return jsonify(r.json())

@app.route("/", methods=["GET"])
def home():
    return "Bot is running fine."

@app.route("/set-webhook", methods=["POST"])
def webhook():
    try:
        update = request.get_json(force=True)
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = str(((msg.get("chat") or {}).get("id", "")))
        text = (msg.get("text") or "").strip()
        if not text:
            return "OK"
        # เอาเฉพาะคำสั่ง (คำแรก)
        cmd = text.split()[0].lower()
        handle_command(cmd, chat_id)
    except Exception:
        traceback.print_exc()
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
