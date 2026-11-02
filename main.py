# -*- coding: utf-8 -*-
"""
صَفاء التحليل v68.0 - Bias + Opening-Range Breakout (0-DTE, عقد واحد باتجاه الكسر)

المنطق:
1) الميل (Bias): من فجوة ما قبل الافتتاح مقابل إغلاق أمس (صاعد / هابط / محايد).
2) الدخول: كسر نطاق أول 30 دقيقة + فوق/تحت VWAP + فوليوم أعلى من المتوسط، وبنفس اتجاه الميل.
3) التنفيذ: عقد ATM واحد (Call للصاعد، Put للهابط) بأمر Limit عند ask.
4) الخروج: وقف -30% على القسط، بيع نصف عند +50%، والباقي بوقف متحرك + خروج عند عكس VWAP + إغلاق قبل النهاية.
5) الحمايات: مخاطرة 1.5% لكل صفقة، حد خسارة يومي 3%، إيقاف بعد 3 خسارات متتالية، فلتر spread.

تنبيه: لا يوجد ضمان ربح. شغّله على Paper فقط (الافتراضي) وراقب النتائج شهراً على الأقل.
التقارير: تظهر في Logs، والصفقات تُحفظ في ملف CSV داخل DATA_DIR (استخدم Volume على Railway).
"""

import os
import sys
import csv
import json
import time
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

# =========================================================
# 1) الإعدادات
# =========================================================
NAME = "صَفاء التحليل"
VERSION = "v68.2"
NY = ZoneInfo("America/New_York")

SYMBOL = "SPY"

def env_f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def env_i(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except ValueError:
        return int(default)


def env_b(name, default):
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "on")


# --- المخاطرة ---
RISK_PER_TRADE = env_f("RISK_PER_TRADE", "0.015")        # أقصى خسارة للصفقة (من الحساب) لو ضرب الوقف
MAX_PREMIUM_PCT = env_f("MAX_PREMIUM_PCT", "0.10")        # أقصى قسط يُدفع للصفقة (من الحساب)
STOP_LOSS_PCT = env_f("STOP_LOSS_PCT", "0.30")          # وقف الخسارة على قسط العقد
DAILY_LOSS_LIMIT = env_f("DAILY_LOSS_LIMIT", "0.03")       # إيقاف اليوم عند خسارة 3% من حساب بداية اليوم
MAX_CONSEC_LOSSES = env_i("MAX_CONSEC_LOSSES", "3")         # إيقاف اليوم بعد 3 خسارات متتالية
MAX_TRADES_PER_DAY = env_i("MAX_TRADES_PER_DAY", "6")

# --- الدخول ---
GAP_THRESHOLD = env_f("GAP_THRESHOLD", "0.0015")        # 0.15% فجوة لتحديد الميل
ALLOW_NEUTRAL = env_b("ALLOW_NEUTRAL", "true")         # لو الميل محايد: لا يتداول
ES_THRESHOLD = 0.0015         # حركة ES الليلية 0.15% = نقطة +1/-1
ES_STRONG = 0.004             # 0.4% = نقطتان
VIX_THRESHOLD = 0.05          # تغير VIX 5% يوم أمس
BIAS_SCORE_MIN = env_i("BIAS_SCORE_MIN", "1")            # مجموع النقاط المطلوب لميل صاعد/هابط
OPEN_RANGE_MINUTES = 30
VOLUME_MULT = env_f("VOLUME_MULT", "1.0")             # فوليوم آخر شمعة >= 1.2 × المتوسط
MAX_SPREAD_PCT = env_f("MAX_SPREAD_PCT", "0.10")         # لا يدخل لو (ask-bid)/mid أكثر من 10%
MIN_OPTION_ASK = env_f("MIN_OPTION_ASK", "0.30")         # يتجنب العقود الرخيصة جداً (spread مدمر)

# --- الخروج ---
BREAKEVEN_TRIGGER = env_f("BREAKEVEN_TRIGGER", "0.30")      # عند +30% ينقل الوقف لنقطة الدخول
PARTIAL_TP_PCT = env_f("PARTIAL_TP_PCT", "0.50")         # عند +50% يبيع النصف
TRAIL_DISTANCE = env_f("TRAIL_DISTANCE", "0.20")         # وقف متحرك 20% من القمة بعد الخروج الجزئي

# --- الأوقات (توقيت نيويورك) ---
SIGNAL_START = dtime(10, 0)
SIGNAL_END = dtime(14, 30)
EOD_CLOSE_TIME = dtime(15, 45)

# --- Alpaca ---
API_KEY = (os.getenv("ALPACA_API_KEY") or "").strip()
API_SECRET = (os.getenv("ALPACA_API_SECRET") or "").strip()
DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
STOCK_FEED = os.getenv("ALPACA_STOCK_FEED", "iex")            # iex مجاني، sip يحتاج اشتراك
OPTIONS_FEED = os.getenv("ALPACA_OPTIONS_FEED", "indicative")  # opra يحتاج اشتراك
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").lower() == "true"

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "safaa_v68_state.json"
JOURNAL_FILE = DATA_DIR / "safaa_v68_journal.csv"
BIAS_LOG = DATA_DIR / "safaa_v68_bias_log.csv"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(NAME)


# =========================================================
# 2) أدوات عامة
# =========================================================
def now_ny():
    return datetime.now(NY)


def today_str():
    return now_ny().date().isoformat()


def stamp():
    return now_ny().isoformat(timespec="seconds")


def api(method, url, params=None, body=None):
    """يرجع (ok, json). لا يبتلع الأخطاء بصمت."""
    try:
        r = requests.request(method, url, headers=HEADERS, params=params, json=body, timeout=15)
        data = r.json() if r.text else {}
        if not r.ok:
            log.warning(f"API {method} {url.split('.markets')[-1]} -> {r.status_code}: {str(data)[:200]}")
        return r.ok, data
    except Exception as e:
        log.warning(f"API خطأ اتصال {method} {url}: {e}")
        return False, {}


def load_json(path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"تعذر قراءة {path}: {e}")
    return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def new_day_state(equity):
    return {
        "date": today_str(),
        "start_equity": equity,
        "realized_pnl": 0.0,
        "consec_losses": 0,
        "trades_today": 0,
        "halted": False,
        "bias": None,
        "gap_pct": None,
        "bias_score": None,
        "bias_factors": None,
        "bias_evaluated": False,
        "last_bar_checked": None,
        "position": None,
    }


state = load_json(STATE_FILE, {})


def save_state():
    save_json(STATE_FILE, state)


def log_trade(pos, exit_price, pnl_dollars, reason):
    new_file = not JOURNAL_FILE.exists()
    with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time", "contract", "direction", "qty", "entry", "last_exit", "pnl_usd", "pnl_pct", "reason"])
        pct = pnl_dollars / (pos["entry"] * 100 * pos["qty"]) if pos["entry"] > 0 else 0
        w.writerow([stamp(), pos["contract"], pos["direction"], pos["qty"],
                    f"{pos['entry']:.2f}", f"{exit_price:.2f}", f"{pnl_dollars:.2f}", f"{pct*100:.1f}%", reason])


# =========================================================
# 3) بيانات السوق
# =========================================================
def market_clock():
    ok, d = api("GET", f"{TRADING_URL}/v2/clock")
    return bool(d.get("is_open")) if ok else False


def get_equity():
    ok, d = api("GET", f"{TRADING_URL}/v2/account")
    return float(d.get("equity", 0)) if ok else 0.0


def parse_t(t):
    return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(NY)


def fetch_bars(start, end, timeframe="1Min"):
    bars, token = [], None
    while True:
        params = {"timeframe": timeframe, "start": start.isoformat(), "end": end.isoformat(),
                  "limit": 1000, "feed": STOCK_FEED, "adjustment": "raw"}
        if token:
            params["page_token"] = token
        ok, d = api("GET", f"{DATA_URL}/v2/stocks/{SYMBOL}/bars", params=params)
        if not ok:
            break
        bars += d.get("bars") or []
        token = d.get("next_page_token")
        if not token:
            break
    return bars


def prev_close():
    end = now_ny()
    bars = fetch_bars(end - timedelta(days=10), end, "1Day")
    today = now_ny().date()
    prior = [b for b in bars if parse_t(b["t"]).date() < today]
    return float(prior[-1]["c"]) if prior else None


_bars_cache = {"t": 0, "bars": []}


def session_bars(max_age=20):
    """شموع الدقيقة لجلسة اليوم (من 9:30) مع كاش قصير."""
    if time.time() - _bars_cache["t"] < max_age:
        return _bars_cache["bars"]
    n = now_ny()
    start = n.replace(hour=9, minute=30, second=0, microsecond=0)
    bars = fetch_bars(start, n)
    # نستبعد الشمعة الجارية (غير المكتملة)
    cur_min = n.replace(second=0, microsecond=0)
    bars = [b for b in bars if parse_t(b["t"]) < cur_min]
    _bars_cache.update({"t": time.time(), "bars": bars})
    return bars


def vwap_of(bars):
    pv = vol = 0.0
    for b in bars:
        tp = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3
        pv += tp * float(b["v"])
        vol += float(b["v"])
    return pv / vol if vol > 0 else None


def compute_bias():
    pc = prev_close()
    if not pc:
        return "neutral", None
    n = now_ny()
    pre = fetch_bars(n.replace(hour=4, minute=0, second=0, microsecond=0),
                     n.replace(hour=9, minute=29, second=0, microsecond=0))
    if pre:
        ref = float(pre[-1]["c"])
    else:
        rth = session_bars(max_age=0)
        if not rth:
            return "neutral", None
        ref = float(rth[0]["o"])
    gap = (ref - pc) / pc
    bias = "bull" if gap >= GAP_THRESHOLD else ("bear" if gap <= -GAP_THRESHOLD else "neutral")
    return bias, gap


# =========================================================
# 4) الخيارات والأوامر
# =========================================================
def option_quote(occ):
    ok, d = api("GET", f"{DATA_URL}/v1beta1/options/quotes/latest",
                params={"symbols": occ, "feed": OPTIONS_FEED})
    q = (d.get("quotes") or {}).get(occ) if ok else None
    if not q:
        return None
    bid, ask = float(q.get("bp", 0)), float(q.get("ap", 0))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask


def pick_contract(direction, underlying_price):
    ctype = "call" if direction == "up" else "put"
    params = {
        "underlying_symbols": SYMBOL, "status": "active", "type": ctype,
        "expiration_date": today_str(),
        "strike_price_gte": f"{underlying_price - 3:.2f}",
        "strike_price_lte": f"{underlying_price + 3:.2f}",
        "limit": 100,
    }
    ok, d = api("GET", f"{TRADING_URL}/v2/options/contracts", params=params)
    cs = d.get("option_contracts") or [] if ok else []
    if not cs:
        return None
    cs.sort(key=lambda c: abs(float(c["strike_price"]) - underlying_price))
    return cs[0]["symbol"]


def place_order(symbol, qty, side, otype="market", limit=None):
    body = {"symbol": symbol, "qty": str(qty), "side": side, "type": otype, "time_in_force": "day"}
    if limit is not None:
        body["limit_price"] = f"{limit:.2f}"
    ok, d = api("POST", f"{TRADING_URL}/v2/orders", body=body)
    return d.get("id") if ok else None


def wait_fill(order_id, timeout=20):
    """يرجع (filled_qty, avg_price). عند المهلة يلغي الأمر ويرجع الكمية المنفذة جزئياً."""
    end = time.time() + timeout
    while time.time() < end:
        ok, d = api("GET", f"{TRADING_URL}/v2/orders/{order_id}")
        if ok:
            st = d.get("status")
            if st == "filled":
                return int(float(d["filled_qty"])), float(d["filled_avg_price"])
            if st in ("canceled", "expired", "rejected"):
                break
        time.sleep(1)
    api("DELETE", f"{TRADING_URL}/v2/orders/{order_id}")
    time.sleep(1)
    ok, d = api("GET", f"{TRADING_URL}/v2/orders/{order_id}")
    if ok and float(d.get("filled_qty") or 0) > 0:
        return int(float(d["filled_qty"])), float(d["filled_avg_price"])
    return 0, 0.0


# =========================================================
# 5) الدخول
# =========================================================
def yahoo_chart(symbol, interval, rng):
    """نقاط (وقت نيويورك، إغلاق) من Yahoo. عند أي فشل يرجع قائمة فارغة والعامل يُحتسب 0."""
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                         params={"interval": interval, "range": rng, "includePrePost": "true"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        j = r.json()["chart"]["result"][0]
        closes = j["indicators"]["quote"][0]["close"]
        return [(datetime.fromtimestamp(t, NY), c) for t, c in zip(j["timestamp"], closes) if c is not None]
    except Exception as e:
        log.warning(f"تعذر جلب {symbol} من Yahoo: {e}")
        return []


def es_overnight_pct():
    """حركة عقود ES منذ إغلاق جلسة أمس (16:00 نيويورك) حتى الآن."""
    pts = yahoo_chart("ES=F", "5m", "5d")
    if not pts or (now_ny() - pts[-1][0]).total_seconds() > 1800:
        return None
    today = now_ny().date()
    ref = [c for t, c in pts if t.date() < today and dtime(15, 55) <= t.time() <= dtime(16, 0)]
    if not ref:
        return None
    return (pts[-1][1] - ref[-1]) / ref[-1]


def vix_change_pct():
    today = now_ny().date()
    days = [c for t, c in yahoo_chart("^VIX", "1d", "15d") if t.date() < today]
    return (days[-1] - days[-2]) / days[-2] if len(days) >= 2 else None


def spy_trend_score():
    today = now_ny().date()
    bars = fetch_bars(now_ny() - timedelta(days=60), now_ny(), "1Day")
    closes = [float(b["c"]) for b in bars if parse_t(b["t"]).date() < today]
    if len(closes) < 20:
        return 0
    return 1 if closes[-1] > sum(closes[-20:]) / 20 else -1


def compute_bias_scored():
    """نظام نقاط: ES الليلي + فجوة SPY + VIX + اتجاه 20 يوم. كل عامل ناقص يُحتسب 0."""
    _, gap = compute_bias()
    es, vix = es_overnight_pct(), vix_change_pct()
    f = {}
    f["ES"] = 0 if es is None else (2 if es >= ES_STRONG else 1 if es >= ES_THRESHOLD
                                    else -2 if es <= -ES_STRONG else -1 if es <= -ES_THRESHOLD else 0)
    f["GAP"] = 0 if gap is None else (1 if gap >= GAP_THRESHOLD else -1 if gap <= -GAP_THRESHOLD else 0)
    f["VIX"] = 0 if vix is None else (-1 if vix >= VIX_THRESHOLD else 1 if vix <= -VIX_THRESHOLD else 0)
    f["TREND"] = spy_trend_score()
    score = sum(f.values())
    bias = "bull" if score >= BIAS_SCORE_MIN else ("bear" if score <= -BIAS_SCORE_MIN else "neutral")
    f["raw"] = {"es_pct": es, "gap_pct": gap, "vix_pct": vix}
    return bias, gap, score, f


def ensure_bias():
    if state.get("bias") is not None or now_ny().time() < dtime(9, 35):
        return
    bias, gap, score, f = compute_bias_scored()
    state["bias"], state["gap_pct"] = bias, gap
    state["bias_score"], state["bias_factors"], state["bias_evaluated"] = score, f, False
    save_state()
    log.info(f"🧭 ميل اليوم: {bias} | النقاط: {score:+d} | {f}")


def evaluate_bias():
    """قرب الإغلاق: يقارن ميل اليوم باتجاه SPY الفعلي (افتتاح -> إغلاق) ويطبع الدقة التراكمية."""
    if state.get("bias_evaluated") or state.get("bias") is None or now_ny().time() < dtime(15, 55):
        return
    bars = session_bars(max_age=0)
    if len(bars) < 30:
        return
    o, c = float(bars[0]["o"]), float(bars[-1]["c"])
    actual = "bull" if c > o else "bear"
    bias = state["bias"]
    hit = "" if bias == "neutral" else int(bias == actual)
    is_new = not BIAS_LOG.exists()
    with open(BIAS_LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if is_new:
            w.writerow(["date", "bias", "score", "actual", "hit", "open", "close", "factors"])
        w.writerow([state["date"], bias, state.get("bias_score"), actual, hit, o, c,
                    json.dumps(state.get("bias_factors"), ensure_ascii=False)])
    state["bias_evaluated"] = True
    save_state()
    with open(BIAS_LOG, encoding="utf-8") as fh:
        scored = [r for r in csv.DictReader(fh) if r["hit"] != ""]
    hits = sum(int(r["hit"]) for r in scored)
    acc = f"{hits}/{len(scored)} ({hits / len(scored) * 100:.0f}%)" if scored else "لا بيانات بعد"
    log.info(f"📈 تقييم الميل: {bias} -> الفعلي {actual} | الدقة التراكمية: {acc}")


def try_entry():
    n = now_ny()
    ensure_bias()
    bias = state.get("bias")
    if bias is None:
        return
    if bias == "neutral" and not ALLOW_NEUTRAL:
        return

    bars = session_bars()
    if len(bars) < OPEN_RANGE_MINUTES + 2:
        return
    open_end = n.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=OPEN_RANGE_MINUTES)
    or_bars = [b for b in bars if parse_t(b["t"]) < open_end]
    rest = [b for b in bars if parse_t(b["t"]) >= open_end]
    if not or_bars or not rest:
        return

    last = bars[-1]
    if state["last_bar_checked"] == last["t"]:
        return                       # فحص كل شمعة مرة وحدة فقط
    state["last_bar_checked"] = last["t"]

    or_high = max(float(b["h"]) for b in or_bars)
    or_low = min(float(b["l"]) for b in or_bars)
    close = float(last["c"])
    avg_vol = sum(float(b["v"]) for b in bars[:-1]) / max(1, len(bars) - 1)
    vol_ok = float(last["v"]) >= VOLUME_MULT * avg_vol
    vw = vwap_of(bars)
    if vw is None or not vol_ok:
        return

    direction = None
    if close > or_high and close > vw and bias in ("bull", "neutral"):
        direction = "up"
    elif close < or_low and close < vw and bias in ("bear", "neutral"):
        direction = "down"
    if not direction:
        return

    log.info(f"⚡ إشارة {direction} | close={close:.2f} OR=[{or_low:.2f},{or_high:.2f}] VWAP={vw:.2f}")
    open_position(direction, close)


def open_position(direction, underlying_price):
    occ = pick_contract(direction, underlying_price)
    if not occ:
        log.info("لا يوجد عقد مناسب اليوم")
        return
    q = option_quote(occ)
    if not q:
        log.info("لا يوجد سعر للعقد")
        return
    bid, ask = q
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid
    if spread > MAX_SPREAD_PCT or ask < MIN_OPTION_ASK:
        log.info(f"⛔ تجاهل: spread={spread*100:.1f}% ask={ask:.2f}")
        return

    equity = get_equity()
    if equity <= 0:
        return
    risk_per_contract = ask * 100 * STOP_LOSS_PCT
    qty = int((equity * RISK_PER_TRADE) // risk_per_contract)
    qty = min(qty, int((equity * MAX_PREMIUM_PCT) // (ask * 100)))
    if qty < 1:
        log.info(f"⛔ الحساب صغير على مخاطرة {RISK_PER_TRADE*100:.1f}% بهذا العقد (ask={ask:.2f}) - لا دخول")
        return

    oid = place_order(occ, qty, "buy", "limit", ask)
    if not oid:
        return
    filled, avg = wait_fill(oid, timeout=20)
    if filled < 1:
        log.info("لم يتنفذ أمر الشراء، تم الإلغاء")
        return

    state["position"] = {
        "contract": occ, "direction": direction, "qty": filled, "qty_open": filled,
        "entry": avg, "peak": avg, "stop": avg * (1 - STOP_LOSS_PCT),
        "partial_done": False, "pnl_usd": 0.0, "entry_time": stamp(),
    }
    state["trades_today"] += 1
    save_state()
    log.info(f"✅ دخول {direction.upper()} | {occ} | الكمية {filled} @ {avg:.2f} | وقف {state['position']['stop']:.2f}")


# =========================================================
# 6) إدارة الصفقة
# =========================================================
def sell(pos, qty, reason, limit=None):
    if limit is not None:
        oid = place_order(pos["contract"], qty, "sell", "limit", limit)
    else:
        oid = place_order(pos["contract"], qty, "sell", "market")
    if not oid:
        return False
    filled, avg = wait_fill(oid, timeout=20)
    if filled < 1:
        log.warning(f"لم يتنفذ البيع ({reason})، سيُعاد المحاولة")
        return False
    pnl = (avg - pos["entry"]) * 100 * filled
    pos["pnl_usd"] += pnl
    pos["qty_open"] -= filled
    pos["last_exit"] = avg
    log.info(f"💰 بيع {filled} @ {avg:.2f} | {reason} | ربح/خسارة الجزء: {pnl:+.2f}$")
    save_state()
    return True


def finish_trade(reason):
    pos = state["position"]
    pnl = pos["pnl_usd"]
    log_trade(pos, pos.get("last_exit", pos["entry"]), pnl, reason)
    state["realized_pnl"] += pnl
    state["consec_losses"] = state["consec_losses"] + 1 if pnl < 0 else 0
    state["position"] = None
    log.info(f"📊 انتهت الصفقة: {pnl:+.2f}$ | يومياً: {state['realized_pnl']:+.2f}$ | خسارات متتالية: {state['consec_losses']}")
    check_halt()
    save_state()


def check_halt():
    if state["realized_pnl"] <= -DAILY_LOSS_LIMIT * state["start_equity"]:
        state["halted"] = True
        log.warning("🛑 تم بلوغ حد الخسارة اليومي. إيقاف التداول لباقي اليوم.")
    elif state["consec_losses"] >= MAX_CONSEC_LOSSES:
        state["halted"] = True
        log.warning("🛑 3 خسارات متتالية. إيقاف التداول لباقي اليوم.")


def manage_position():
    pos = state["position"]
    if not pos:
        return
    q = option_quote(pos["contract"])
    n = now_ny()
    eod = n.time() >= EOD_CLOSE_TIME

    if not q and not eod:
        return
    bid = q[0] if q else 0.0
    if bid > pos["peak"]:
        pos["peak"] = bid
    gain = (bid - pos["entry"]) / pos["entry"] if bid else 0.0

    # نقل الوقف لنقطة الدخول
    if gain >= BREAKEVEN_TRIGGER and pos["stop"] < pos["entry"]:
        pos["stop"] = pos["entry"]
        log.info("🛡️ تم نقل الوقف إلى نقطة الدخول")

    # خروج جزئي
    if gain >= PARTIAL_TP_PCT and not pos["partial_done"]:
        half = pos["qty_open"] // 2
        if half >= 1:
            sell(pos, half, "TP جزئي", limit=round(bid, 2))
        pos["partial_done"] = True
        pos["stop"] = max(pos["stop"], pos["entry"])

    # وقف متحرك بعد الخروج الجزئي
    if pos["partial_done"]:
        pos["stop"] = max(pos["stop"], pos["entry"], pos["peak"] * (1 - TRAIL_DISTANCE))

    # عكس VWAP ضد اتجاه الصفقة
    vwap_exit = False
    bars = session_bars()
    if bars:
        vw = vwap_of(bars)
        c = float(bars[-1]["c"])
        if vw and ((pos["direction"] == "up" and c < vw) or (pos["direction"] == "down" and c > vw)):
            vwap_exit = pos["partial_done"] or gain < 0   # يخرج لو عكس VWAP وهو بعد الجزئي أو خاسر

    reason = None
    if eod:
        reason = "EOD"
    elif bid and bid <= pos["stop"]:
        reason = "Stop/Trail"
    elif vwap_exit:
        reason = "VWAP Reversal"

    if reason and pos["qty_open"] > 0:
        if sell(pos, pos["qty_open"], reason):
            finish_trade(reason)
            return
    elif pos["qty_open"] <= 0:
        finish_trade("Closed")
        return
    save_state()


# =========================================================
# 7) الحلقة الرئيسية
# =========================================================
def roll_day():
    global state
    if state.get("date") != today_str():
        eq = get_equity()
        if eq <= 0:
            return False
        old_pos = state.get("position")
        state = new_day_state(eq)
        if old_pos:
            state["position"] = old_pos
            log.warning("⚠️ يوجد مركز مفتوح من يوم سابق، سيتم إدارته/إغلاقه")
        save_state()
        log.info(f"📅 يوم جديد | رصيد البداية: {eq:,.2f}$")
    return True


def run():
    if not API_KEY or not API_SECRET:
        log.error("ALPACA_API_KEY / ALPACA_API_SECRET غير مضبوطة في متغيرات البيئة")
        sys.exit(1)
    if "paper" not in TRADING_URL and not ALLOW_LIVE:
        log.error("الرابط ليس Paper. لتشغيل حساب حقيقي اضبط ALLOW_LIVE=true (غير مُوصى به قبل شهر Paper).")
        sys.exit(1)
    log.info(f"🚀 {NAME} {VERSION} | {'PAPER' if 'paper' in TRADING_URL else 'LIVE'} | بيانات الأسهم: {STOCK_FEED} | الخيارات: {OPTIONS_FEED}")

    while True:
        try:
            if not market_clock():
                time.sleep(30)
                continue
            if not roll_day():
                time.sleep(15)
                continue

            manage_position()
            ensure_bias()
            evaluate_bias()

            t = now_ny().time()
            can_enter = (
                state["position"] is None
                and not state["halted"]
                and state["trades_today"] < MAX_TRADES_PER_DAY
                and SIGNAL_START <= t <= SIGNAL_END
            )
            if can_enter:
                try_entry()
            time.sleep(5)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"خطأ غير متوقع: {e}", exc_info=True)
            time.sleep(10)


if __name__ == "__main__":
    run()
