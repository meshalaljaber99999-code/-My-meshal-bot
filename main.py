# -*- coding: utf-8 -*-
"""
صَفاء التحليل v115.0 | SPY 0-DTE | ADAPTIVE ENGINE | PAPER ONLY
- Conviction score + graded sizing     - Analog forecasting (kNN)
- EV engine (edge vs option price)     - Meta-labeling (online logistic)
- Thompson sampling over strategies    - Volatility engine (straddle vs realized)
- Put-call parity + gamma walls        - Levels / ORB / heavy-weights
- Smart exits (partial/BE/trail/time/reversal/level/invalidation)
- Mid-price entry escalation           - Shadow mode + daily report
"""
import os, sys, json, time, uuid, math, re, logging, hashlib, random, shutil
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
import requests

# ========================= 1) CONFIG =========================
VERSION = "115.0"
UNDERLYING = "SPY"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "."))
BASE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = BASE_DIR / "safaa_v115_state.json"
LEDGER_FILE = BASE_DIR / "safaa_v115_ledger.json"
INIT_FILE = BASE_DIR / "safaa_v115_initialized.flag"
LOCK_FILE = BASE_DIR / "safaa_v115.lock"

API_KEY = os.getenv("APCA_API_KEY_ID", "").strip()
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
TRADING_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

if "paper" not in TRADING_URL.lower():
    raise RuntimeError("v115 تعمل على Paper فقط.")
if os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true":
    raise RuntimeError("التداول الحقيقي ممنوع.")

ALL_STRATEGIES = ["CALL", "PUT", "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD",
                  "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"]
STRATEGY_MODE = os.getenv("STRATEGY_MODE", "SINGLES").upper()
if STRATEGY_MODE not in ("AUTO", "SINGLES") and STRATEGY_MODE not in ALL_STRATEGIES:
    raise RuntimeError(f"STRATEGY_MODE غير صالح: {STRATEGY_MODE}")

# --- Risk (مرنة، تتدرج حسب القناعة)
RISK_BASE = 0.02
RISK_MAX = 0.04
MAX_DAILY_DRAWDOWN = 0.06
MAX_PREMIUM_FRACTION = 0.05
MAX_OPEN_POSITIONS = 2
MAX_TRADES_PER_DAY = 8
MAX_CONSECUTIVE_LOSSES = 4
MAX_CONTRACTS = 20
SINGLE_RISK_FRACTION = 0.60
ADD_ON_MIN_RETURN = 0.25

# --- Conviction / edge
CONV_MIN = 30
MIN_EV_RATIO = 0.02
META_MIN_N = 10**9
META_MIN_P = 0.42
SHADOW_MIN_SCORE = 20
SHADOW_HORIZON = 15
SHADOW_WIN_ATR = 0.25

# --- Options selection
TARGET_DELTA_MIN, TARGET_DELTA_MAX, TARGET_DELTA = 0.38, 0.68, 0.50
SPREAD_DELTA_MIN, SPREAD_DELTA_MAX = 0.10, 0.70
MAX_SPREAD_PERCENT = 0.15
MIN_OPTION_ASK, MAX_OPTION_ASK = 0.15, 9.00
STRIKE_WINDOW = 15.0
SPREAD_MIN_WIDTH, SPREAD_MAX_WIDTH = 1.0, 5.0
SPREAD_MAX_DEBIT, SPREAD_MIN_CREDIT = 3.5, 0.12
MAX_OPTION_QUOTE_AGE = 90

# --- Exits
HARD_STOP = 0.45
STOP_ATR_MULT = 0.9          # إبطال الفكرة على سعر SPY بوحدات ATR15
BREAKEVEN_AFTER = 0.20
PARTIAL_AT = 0.30
TP_FULL = 0.90
TRAIL_ACTIVATE = 0.40
TIME_STOP_SEC = 900
REVERSAL_CONV = 55

# --- Times (دقائق من منتصف الليل بتوقيت نيويورك)
ENTRY_START = 9 * 60 + 55
ENTRY_END = 14 * 60
LATE_SPREAD_AFTER = 14 * 60
FORCE_EXIT = 15 * 60 + 45
DEAD_ZONE = (11 * 60 + 30, 13 * 60 + 30)

# --- Execution
SCAN_SECONDS = 10
REQUEST_TIMEOUT = 15
MAX_API_RETRIES = 3
ENTRY_STEPS = [0.30, 0.55, 0.80, 1.00]   # نسبة من bid إلى ask
ENTRY_STEP_SECONDS = 12
EXIT_ORDER_TIMEOUT = 20
RECONCILE_INTERVAL = 30

# --- Indicators
RSI_PERIOD = 14
MIN_SESSION_BARS = 26
SPY_MAX_BAR_AGE = 150
ANALOG_K = 150
ANALOG_MIN_ROWS = 400
HEAVY = ["AAPL", "MSFT", "NVDA", "AMZN"]
FEED = os.getenv("ALPACA_STOCK_FEED", "iex").lower().strip()
OPT_FEED = os.getenv("ALPACA_OPTIONS_FEED", "indicative")

# أحداث الأخبار بتوقيت نيويورك: "2026-11-12 08:30,2026-12-09 14:00"
NEWS_EVENTS_RAW = os.getenv("NEWS_EVENTS_NY", "")

# ========================= 2) LOGGING / EXCEPTIONS =========================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(BASE_DIR / "safaa_v115.log", encoding="utf-8")])
log = logging.getLogger("SAFAA115")


class APIError(Exception): pass
class SafetyHalt(Exception): pass
class EntryBlocked(Exception): pass
class MarketDataUnavailable(Exception): pass


# ========================= 3) HELPERS =========================
def utc_now(): return datetime.now(UTC)
def utc_iso(): return utc_now().isoformat()
def ny_now(): return datetime.now(NY)
def today_ny(): return ny_now().date()
def today_str(): return today_ny().isoformat()
def minutes_ny(): n = ny_now(); return n.hour * 60 + n.minute
def make_id(p): return f"{p}-{uuid.uuid4().hex[:20]}"
def clamp(x, lo, hi): return max(lo, min(hi, x))
def sign(x): return 1 if x > 0 else -1 if x < 0 else 0
def average(v): return sum(v) / len(v) if v else None


def safe_float(v, default=None):
    try:
        r = float(v)
        return r if math.isfinite(r) else default
    except (TypeError, ValueError):
        return default


def parse_ts(value):
    if not value: return None
    try:
        p = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if p.tzinfo is None: p = p.replace(tzinfo=UTC)
        return p.astimezone(UTC)
    except Exception:
        return None


def seconds_since(iso):
    t = parse_ts(iso)
    return (utc_now() - t).total_seconds() if t else 10 ** 9


def std(v):
    if len(v) < 2: return 0.0
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def atomic_write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_money(v):
    try: return f"${float(v):,.2f}"
    except (TypeError, ValueError): return "غير متاح"


# ========================= 4) HTTP =========================
SESSION = requests.Session()
SESSION.headers.update({"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET,
                        "Content-Type": "application/json"})


def api_request(method, url, params=None, payload=None):
    method = method.upper()
    attempts = 1 if method == "POST" else MAX_API_RETRIES
    last = None
    for a in range(attempts):
        try:
            r = SESSION.request(method, url, params=params, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code >= 400:
                try: detail = r.json()
                except Exception: detail = r.text[:500]
                raise APIError(f"{method} {url} HTTP {r.status_code}: {detail}")
            return r.json() if r.text else {}
        except APIError:
            raise
        except requests.RequestException as exc:
            last = exc
            if method == "POST":
                raise APIError("نتيجة POST غير مؤكدة.")
            if a + 1 < attempts: time.sleep(1.2 * (a + 1))
    raise APIError(str(last))


def trading_api(m, path, params=None, payload=None):
    return api_request(m, f"{TRADING_URL}{path}", params, payload)


def data_api(m, path, params=None):
    return api_request(m, f"{DATA_URL}{path}", params)


# ========================= 5) LOCK / STATE / LEDGER =========================
_LOCK = None


def acquire_instance_lock():
    global _LOCK
    try:
        import fcntl
    except ImportError:
        raise SafetyHalt("تعذر استخدام قفل النظام.")
    _LOCK = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(_LOCK.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SafetyHalt("نسخة أخرى تعمل بالفعل.")
    _LOCK.seek(0); _LOCK.truncate(); _LOCK.write(str(os.getpid())); _LOCK.flush()


def default_state():
    now = utc_iso()
    return {"version": VERSION, "created_at": now, "updated_at": now,
            "halted": False, "halt_reason": None, "halt_scope": None,
            "trading_day": today_str(), "starting_equity": None, "daily_pnl": 0.0,
            "trades_today": 0, "consecutive_losses": 0, "last_candle_id": None,
            "last_reconciliation": None, "telegram_offset": None, "position_meta": {},
            "meta_w": [0.0] * 8, "meta_n": 0, "ts": {}, "last_decision": "لا قرار بعد.",
            "report_day": None, "day_rejects": {}}


def default_ledger():
    return {"version": VERSION, "orders": {}, "closed_trades": [], "events": [], "shadow": []}


STATE = None
LEDGER = None
CTX = {}          # آخر سياق سوق
ANALOG = {"day": None, "rows": [], "retry_at": 0}
_CACHE = {"clock": (0, None), "account": (0, None), "levels": (None, None), "heavy": (0, 0.0)}


def save_state():
    STATE["updated_at"] = utc_iso()
    atomic_write_json(STATE_FILE, STATE)


def save_ledger():
    atomic_write_json(LEDGER_FILE, LEDGER)


def ledger_event(kind, data=None):
    LEDGER["events"].append({"id": make_id("EVT"), "type": kind, "timestamp": utc_iso(), "data": data or {}})
    LEDGER["events"] = LEDGER["events"][-3000:]
    save_ledger()


def halt_bot(reason, scope="MANUAL"):
    STATE["halted"] = True; STATE["halt_reason"] = str(reason); STATE["halt_scope"] = scope
    save_state()
    log.critical("TRADING HALTED (%s): %s", scope, reason)


def ensure_not_halted():
    if STATE.get("halted"):
        raise SafetyHalt(STATE.get("halt_reason") or "البوت متوقف.")


def note_reject(reason):
    d = STATE.setdefault("day_rejects", {})
    d[reason] = d.get(reason, 0) + 1
    log.info("[REJECT] %s", reason)


def initialize_storage():
    global STATE, LEDGER
    if INIT_FILE.exists():
        if not STATE_FILE.exists() or not LEDGER_FILE.exists():
            raise SafetyHalt("ملفات الحالة مفقودة.")
        try:
            STATE = read_json(STATE_FILE); LEDGER = read_json(LEDGER_FILE)
        except Exception as exc:
            raise SafetyHalt(f"تعذر قراءة الحالة: {exc}")
        for k, v in default_state().items(): STATE.setdefault(k, v)
        for k, v in default_ledger().items(): LEDGER.setdefault(k, v)
        STATE["version"] = VERSION
        save_state()
        return
    acc = trading_api("GET", "/v2/account")
    if acc.get("status") != "ACTIVE":
        raise SafetyHalt(f"الحساب غير نشط: {acc.get('status')}")
    STATE, LEDGER = default_state(), default_ledger()
    STATE["starting_equity"] = safe_float(acc.get("equity"))
    if not STATE["starting_equity"]:
        raise SafetyHalt("تعذر تحديد رأس المال.")
    save_state(); save_ledger()
    INIT_FILE.write_text(f"Initialized {utc_iso()} v{VERSION}\n")


# ========================= 6) ACCOUNT / CLOCK =========================
def get_account(max_age=8):
    t, v = _CACHE["account"]
    if v is not None and time.time() - t < max_age: return v
    v = trading_api("GET", "/v2/account")
    _CACHE["account"] = (time.time(), v)
    return v


def get_equity():
    e = safe_float(get_account().get("equity"))
    if not e or e <= 0: raise SafetyHalt("Equity غير صالح.")
    return e


def get_clock():
    t, v = _CACHE["clock"]
    if v is not None and time.time() - t < 15: return v
    v = trading_api("GET", "/v2/clock")
    _CACHE["clock"] = (time.time(), v)
    return v


def market_open(): return bool(get_clock().get("is_open", False))


def entry_time_allowed():
    n = ny_now()
    return n.weekday() < 5 and ENTRY_START <= minutes_ny() <= ENTRY_END


def force_exit_time_reached(): return minutes_ny() >= FORCE_EXIT


def any_pending_orders():
    return any(m.get("status") in ("PENDING_ENTRY", "PENDING_LEG1", "PENDING_LEG2", "PENDING_EXIT")
           for m in STATE["position_meta"].values())


def parse_events():
    out = []
    for item in NEWS_EVENTS_RAW.split(","):
        item = item.strip()
        if not item: continue
        try: out.append(datetime.strptime(item, "%Y-%m-%d %H:%M").replace(tzinfo=NY))
        except ValueError: log.warning("حدث خبر غير صالح: %s", item)
    return out


NEWS_EVENTS = parse_events()


def news_state():
    now = ny_now()
    for ev in NEWS_EVENTS:
        d = (ev - now).total_seconds() / 60
        if 0 < d <= 3: return "FLAT"
        if -3 <= d <= 10: return "BLOCK"
    return None


def refresh_daily_baseline():
    day = today_str()
    if STATE["trading_day"] == day: return
    process_orders()
    if any_pending_orders(): return
    if get_option_positions():
        raise SafetyHalt("تغير اليوم مع وجود مراكز مفتوحة؛ تحقق يدوياً.")
    if STATE["position_meta"]:
        ledger_event("STALE_META_CLEARED", dict(STATE["position_meta"]))
        STATE["position_meta"] = {}
    eq = get_equity()
    STATE.update({"trading_day": day, "starting_equity": eq, "daily_pnl": 0.0, "trades_today": 0,
                  "consecutive_losses": 0, "day_rejects": {}})
    if STATE.get("halted") and STATE.get("halt_scope") == "DAY":
        STATE["halted"] = False; STATE["halt_reason"] = None; STATE["halt_scope"] = None
    save_state()
    log.info("يوم جديد | Equity=%.2f", eq)


def check_daily_risk():
    ensure_not_halted()
    start = safe_float(STATE.get("starting_equity"))
    if not start or start <= 0: raise SafetyHalt("رأس مال مرجعي غير صالح.")
    pnl = get_equity() - start
    STATE["daily_pnl"] = pnl
    if pnl <= -(start * MAX_DAILY_DRAWDOWN):
        halt_bot(f"حد الخسارة اليومية: {pnl:.2f}", "DAY"); raise SafetyHalt("حد الخسارة اليومية")
    if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        halt_bot("خسائر متتالية.", "DAY"); raise SafetyHalt("خسائر متتالية")


# ========================= 7) MARKET DATA =========================
def parse_bar(item):
    try:
        t = parse_ts(item.get("t"))
        b = {"t": t, "open": float(item["o"]), "high": float(item["h"]),
             "low": float(item["l"]), "close": float(item["c"]), "volume": float(item["v"])}
    except (TypeError, ValueError, KeyError):
        return None
    if not t or min(b["open"], b["high"], b["low"], b["close"]) <= 0: return None
    if b["high"] < b["low"] or b["volume"] < 0: return None
    if b["high"] < max(b["open"], b["close"]) or b["low"] > min(b["open"], b["close"]): return None
    return b


def fetch_bars_paged(symbol, timeframe, start, end):
    out, token = [], None
    for _ in range(10):
        params = {"timeframe": timeframe, "start": start.isoformat(), "end": end.isoformat(),
                  "limit": 10000, "adjustment": "raw", "feed": FEED, "sort": "asc"}
        if token: params["page_token"] = token
        r = data_api("GET", f"/v2/stocks/{symbol}/bars", params)
        out += [parse_bar(x) for x in (r.get("bars") or [])]
        token = r.get("next_page_token")
        if not token: break
    return [b for b in out if b]


def get_spy_bars():
    last = None
    for attempt in range(2):
        try:
            now = utc_now()
            r = data_api("GET", f"/v2/stocks/{UNDERLYING}/bars", {
                "timeframe": "1Min", "limit": 1000, "adjustment": "raw", "feed": FEED,
                "sort": "desc", "start": (now - timedelta(hours=8)).isoformat(), "end": now.isoformat()})
            bars = [parse_bar(x) for x in (r.get("bars") or [])]
            bars = sorted({b["t"]: b for b in bars if b}.values(), key=lambda x: x["t"])
            done = [b for b in bars if b["t"] + timedelta(minutes=1) <= now]
            if len(done) < MIN_SESSION_BARS: raise MarketDataUnavailable("شموع غير كافية.")
            age = (now - (done[-1]["t"] + timedelta(minutes=1))).total_seconds()
            if age > SPY_MAX_BAR_AGE: raise MarketDataUnavailable(f"بيانات قديمة {age:.0f}s")
            return done
        except (APIError, MarketDataUnavailable) as exc:
            last = exc
            time.sleep(1.5)
    raise MarketDataUnavailable(str(last))


def session_bars(bars, day=None):
    day = day or today_ny()
    out = []
    for b in bars:
        t = b["t"].astimezone(NY)
        m = t.hour * 60 + t.minute
        if t.date() == day and 570 <= m < 960: out.append(b)
    return out


def daily_levels():
    day = today_str()
    if _CACHE["levels"][0] == day: return _CACHE["levels"][1]
    res = None
    try:
        now = utc_now()
        bars = fetch_bars_paged(UNDERLYING, "1Day", now - timedelta(days=12), now)
        prev = [b for b in bars if b["t"].astimezone(NY).date() < today_ny()]
        if prev:
            b = prev[-1]
            res = {"high": b["high"], "low": b["low"], "close": b["close"], "range": b["high"] - b["low"]}
    except Exception as exc:
        log.warning("تعذر جلب الشمعة اليومية: %s", exc)
    _CACHE["levels"] = (day, res)
    return res


def heavy_bias():
    t, v = _CACHE["heavy"]
    if time.time() - t < 60: return v
    val = 0.0
    try:
        now = utc_now()
        r = data_api("GET", "/v2/stocks/bars", {
            "symbols": ",".join(HEAVY), "timeframe": "5Min", "limit": 1000, "adjustment": "raw",
            "feed": FEED, "sort": "asc", "start": (now - timedelta(hours=3)).isoformat()})
        vals = []
        for sym, arr in (r.get("bars") or {}).items():
            bs = [parse_bar(x) for x in arr]
            bs = [b for b in bs if b]
            if len(bs) >= 4:
                ch = (bs[-1]["close"] - bs[-4]["close"]) / bs[-4]["close"]
                vals.append(clamp(ch / 0.002, -1, 1))
        val = average(vals) or 0.0
    except Exception as exc:
        log.warning("heavy bias: %s", exc)
    _CACHE["heavy"] = (time.time(), val)
    return val


# ========================= 8) INDICATORS =========================
def ema(values, period):
    if len(values) < period: return None
    k = 2.0 / (period + 1)
    r = sum(values[:period]) / period
    for x in values[period:]: r = x * k + r * (1 - k)
    return r


def calc_rsi(values, period=RSI_PERIOD):
    if len(values) <= period: return None
    ch = [values[i] - values[i - 1] for i in range(1, len(values))]
    g = [max(x, 0) for x in ch]; l = [max(-x, 0) for x in ch]
    ag, al = average(g[:period]), average(l[:period])
    for i in range(period, len(ch)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + l[i]) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def calc_atr(bars, period=14):
    if len(bars) <= period: return None
    trs = []
    for i in range(1, len(bars)):
        c, p = bars[i], bars[i - 1]
        trs.append(max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"])))
    return average(trs[-period:])


def vwap_sigma(sb):
    pv = vv = 0.0
    for b in sb:
        v = max(b["volume"], 0.0)
        pv += (b["high"] + b["low"] + b["close"]) / 3 * v; vv += v
    if vv <= 0: return None, None
    vwap = pv / vv
    var = sum(max(b["volume"], 0.0) * (((b["high"] + b["low"] + b["close"]) / 3) - vwap) ** 2 for b in sb) / vv
    return vwap, max(math.sqrt(var), 0.10)


def five_min_closes(sb):
    g = {}
    for b in sb:
        t = b["t"].astimezone(NY)
        g[(t.hour * 60 + t.minute) // 5] = b["close"]
    return [g[k] for k in sorted(g)]


def compute_indicators(sb):
    closes = [b["close"] for b in sb]
    e9, e21, rsi = ema(closes, 9), ema(closes, 21), calc_rsi(closes)
    vwap, sigma = vwap_sigma(sb)
    atr1 = calc_atr(sb)
    if None in (e9, e21, rsi, vwap, atr1) or atr1 <= 0:
        raise MarketDataUnavailable("تعذر حساب المؤشرات.")
    vols = [b["volume"] for b in sb]
    avg_v = average(vols[-21:-1])
    vr = vols[-1] / avg_v if avg_v and avg_v > 0 else 1.0
    orb = [b for b in sb if 570 <= (b["t"].astimezone(NY).hour * 60 + b["t"].astimezone(NY).minute) < 585]
    or_ok = minutes_ny() >= 585 and len(orb) >= 10
    last = closes[-31:]
    path = sum(abs(last[i] - last[i - 1]) for i in range(1, len(last)))
    er = abs(last[-1] - last[0]) / path if path > 0 else 0.0
    sides = [sign(c - vwap) for c in last]
    crosses = sum(1 for i in range(1, len(sides)) if sides[i] and sides[i - 1] and sides[i] != sides[i - 1])
    return {"price": closes[-1], "prev": closes[-2], "closes": closes, "ema9": e9, "ema21": e21,
            "rsi": rsi, "vwap": vwap, "sigma": sigma, "atr1": atr1, "scale": atr1 * math.sqrt(15),
            "vr": vr, "day_open": sb[0]["open"], "day_hi": max(b["high"] for b in sb),
            "day_lo": min(b["low"] for b in sb), "or_hi": max(b["high"] for b in orb) if or_ok else None,
            "or_lo": min(b["low"] for b in orb) if or_ok else None, "closes5": five_min_closes(sb),
            "er": er, "crosses": crosses, "t": sb[-1]["t"].isoformat()}


def classify_regime(ind):
    if ind["er"] >= 0.30: return "TREND"
    if ind["er"] <= 0.15 and ind["crosses"] >= 3: return "RANGE"
    return "MIXED"


# ========================= 9) ANALOG FORECASTING (kNN) =========================
KNN_W = [1.2, 1.0, 0.6, 1.1, 0.7, 0.4]


def day_feature_rows(bars):
    rows, closes, trs, vols = [], [], [], []
    e9 = e21 = None
    pv = vv = 0.0
    gains, losses, ag, al = [], [], None, None
    for i, b in enumerate(bars):
        c = b["close"]; closes.append(c); vols.append(b["volume"])
        e9 = c if e9 is None else c * 0.2 + e9 * 0.8
        e21 = c if e21 is None else c * (2 / 22) + e21 * (1 - 2 / 22)
        v = max(b["volume"], 0.0)
        pv += (b["high"] + b["low"] + c) / 3 * v; vv += v
        vwap = pv / vv if vv > 0 else c
        pc = bars[i - 1]["close"] if i > 0 else c
        trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
        if i > 0:
            ch = c - pc; g = max(ch, 0.0); l = max(-ch, 0.0)
            if ag is None:
                gains.append(g); losses.append(l)
                if len(gains) == RSI_PERIOD:
                    ag, al = sum(gains) / RSI_PERIOD, sum(losses) / RSI_PERIOD
            else:
                ag = (ag * (RSI_PERIOD - 1) + g) / RSI_PERIOD
                al = (al * (RSI_PERIOD - 1) + l) / RSI_PERIOD
        if i < 25 or ag is None: continue
        atr1 = sum(trs[-14:]) / 14
        if atr1 <= 0: continue
        rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        scale = atr1 * math.sqrt(15)
        avg_v = sum(vols[-21:-1]) / 20
        vr = v / avg_v if avg_v > 0 else 1.0
        t = b["t"].astimezone(NY)
        mins = t.hour * 60 + t.minute - 570
        feat = [clamp((c - vwap) / scale, -4, 4), clamp((e9 - e21) / scale, -3, 3) * 2,
                (rsi - 50) / 25, clamp((c - closes[i - 5]) / (atr1 * math.sqrt(5)), -4, 4),
                mins / 390.0, clamp(vr, 0, 3) - 1]
        rows.append({"i": i, "feat": feat, "scale": scale, "close": c})
    return rows


def attach_forward(rows, bars, horizon=SHADOW_HORIZON):
    out, j, n = [], 0, len(bars)
    for r in rows:
        target = bars[r["i"]]["t"] + timedelta(minutes=horizon)
        while j < n and bars[j]["t"] < target: j += 1
        if j >= n: break
        if (bars[j]["t"] - target).total_seconds() > 120: continue
        out.append((r["feat"], (bars[j]["close"] - r["close"]) / r["scale"]))
    return out


def build_analog_db():
    now = utc_now()
    raw = fetch_bars_paged(UNDERLYING, "1Min", now - timedelta(days=32), now)
    by_day = {}
    for b in raw:
        by_day.setdefault(b["t"].astimezone(NY).date(), []).append(b)
    rows = []
    for d, bars in by_day.items():
        if d >= today_ny(): continue
        sb = session_bars(bars, d)
        if len(sb) < 200: continue
        rows += attach_forward(day_feature_rows(sb), sb)
    ANALOG["rows"] = rows
    ANALOG["day"] = today_str()
    log.info("ANALOG DB | rows=%s | days=%s", len(rows), len(by_day))


def ensure_analog():
    if ANALOG["day"] == today_str() or time.time() < ANALOG["retry_at"]: return
    try:
        build_analog_db()
    except Exception as exc:
        ANALOG["retry_at"] = time.time() + 900
        log.warning("ANALOG BUILD FAILED: %s", exc)


def analog_forecast(feat):
    rows = ANALOG["rows"]
    if not feat or len(rows) < ANALOG_MIN_ROWS: return None
    scored = []
    for f, fwd in rows:
        d = 0.0
        for a, b, w in zip(feat, f, KNN_W): d += w * (a - b) ** 2
        scored.append((d, fwd))
    scored.sort(key=lambda x: x[0])
    top = scored[:ANALOG_K]
    wts = [1 / (1 + math.sqrt(d)) for d, _ in top]
    sw = sum(wts)
    return {"p_up": sum(w for w, (_, fw) in zip(wts, top) if fw > 0) / sw,
            "mean": sum(w * fw for w, (_, fw) in zip(wts, top)) / sw,
            "fwds": [fw for _, fw in top], "n": len(top)}


# ========================= 10) OPTIONS CHAIN =========================
OPTION_RE = re.compile(r"^SPY\d{6}[CP]\d{8}$")


def get_option_snapshots(symbols):
    symbols = list(dict.fromkeys(str(s).strip() for s in symbols if s))
    out = {}
    for i in range(0, len(symbols), 100):
        r = data_api("GET", "/v1beta1/options/snapshots",
                     {"symbols": ",".join(symbols[i:i + 100]), "feed": OPT_FEED, "limit": 1000})
        snaps = r.get("snapshots", {})
        if isinstance(snaps, dict): out.update(snaps)
    return out


def extract_quote(snapshot):
    if not isinstance(snapshot, dict): return None
    q = snapshot.get("latestQuote")
    if not isinstance(q, dict): return None
    try:
        bid, ask = float(q.get("bp", 0)), float(q.get("ap", 0))
        bs, as_ = float(q.get("bs", 0) or 0), float(q.get("as", 0) or 0)
        t = parse_ts(q.get("t"))
        if not t: return None
        age = (utc_now() - t).total_seconds()
        if age < -5 or age > MAX_OPTION_QUOTE_AGE: return None
        if ask <= 0 or bid < 0 or ask < bid: return None
        mid = (ask + bid) / 2
        return {"bid": bid, "ask": ask, "bid_size": bs, "ask_size": as_, "mid": mid,
                "spread": ask - bid, "spread_percent": (ask - bid) / mid if mid > 0 else 1.0, "age": age}
    except (TypeError, ValueError):
        return None


def extract_greeks(snapshot):
    g = (snapshot or {}).get("greeks") or {}
    return {n: safe_float(g.get(n)) for n in ("delta", "gamma", "theta", "vega")}


def get_contracts(opt_type, price):
    r = trading_api("GET", "/v2/options/contracts", {
        "underlying_symbols": UNDERLYING, "status": "active", "type": opt_type,
        "expiration_date": today_str(), "strike_price_gte": f"{price - STRIKE_WINDOW:.2f}",
        "strike_price_lte": f"{price + STRIKE_WINDOW:.2f}", "limit": 1000})
    out = []
    for c in r.get("option_contracts") or []:
        sym = str(c.get("symbol", "")).strip()
        if OPTION_RE.match(sym) and c.get("status") == "active" and c.get("tradable") is not False:
            out.append(c)
    return out


def build_chain(price):
    raw = {"call": get_contracts("call", price), "put": get_contracts("put", price)}
    syms = [c["symbol"] for t in raw for c in raw[t]]
    if not syms: raise EntryBlocked("لا عقود 0DTE متاحة.")
    snaps = get_option_snapshots(syms)
    chain = {"call": [], "put": []}
    for t in ("call", "put"):
        for c in raw[t]:
            snap = snaps.get(c["symbol"])
            q = extract_quote(snap)
            if not q: continue
            chain[t].append({"symbol": c["symbol"], "type": t, "strike": float(c["strike_price"]),
                             "expiration": c["expiration_date"], "quote": q, "greeks": extract_greeks(snap),
                             "oi": int(safe_float(c.get("open_interest"), 0) or 0)})
    by_k = {}
    for t in ("call", "put"):
        for l in chain[t]: by_k.setdefault(l["strike"], {})[t] = l
    both = [k for k, v in by_k.items() if len(v) == 2]
    chain["atm"] = chain["straddle"] = chain["parity"] = None
    if both:
        k = min(both, key=lambda x: abs(x - price))
        c, p = by_k[k]["call"]["quote"]["mid"], by_k[k]["put"]["quote"]["mid"]
        chain.update(atm=k, straddle=c + p, parity=k + c - p)
    calls_up = [l for l in chain["call"] if l["strike"] >= price and l["oi"] > 0]
    puts_dn = [l for l in chain["put"] if l["strike"] <= price and l["oi"] > 0]
    chain["call_wall"] = max(calls_up, key=lambda l: l["oi"])["strike"] if calls_up else None
    chain["put_wall"] = max(puts_dn, key=lambda l: l["oi"])["strike"] if puts_dn else None
    return chain


def liquid_legs(chain, typ, dmin, dmax):
    out = []
    for l in chain[typ]:
        q, d = l["quote"], l["greeks"].get("delta")
        if d is None or not dmin <= abs(d) <= dmax: continue
        if (typ == "call" and d <= 0) or (typ == "put" and d >= 0): continue
        if q["bid"] <= 0 or q["bid_size"] <= 0 or q["ask_size"] <= 0: continue
        if q["spread_percent"] > MAX_SPREAD_PERCENT: continue
        out.append(l)
    return out


# ========================= 11) VOLATILITY / LEVELS / CONVICTION =========================
def vol_state(ind, chain):
    rem = max(1, 960 - minutes_ny())
    c = ind["closes"][-31:]
    rets = [(c[i] - c[i - 1]) / c[i - 1] for i in range(1, len(c))]
    proj = std(rets) * ind["price"] * math.sqrt(rem) * 0.8
    straddle = chain.get("straddle") if chain else None
    iv_ratio = straddle / proj if straddle and proj > 0 else 1.0
    dl = daily_levels()
    prev_range = dl["range"] if dl else None
    range_used = (ind["day_hi"] - ind["day_lo"]) / max(prev_range, 1.0) if prev_range else 0.5
    return {"iv_ratio": iv_ratio, "range_used": range_used, "straddle": straddle, "remaining": rem}


def collect_levels(price, ind, chain, scale):
    lv = []
    dl = daily_levels()
    if dl: lv += [dl["high"], dl["low"], dl["close"]]
    if ind["or_hi"]: lv += [ind["or_hi"], ind["or_lo"]]
    lv += [ind["day_hi"], ind["day_lo"]]
    base = math.floor(price / 5) * 5
    lv += [base, base + 5]
    if chain:
        lv += [x for x in (chain.get("call_wall"), chain.get("put_wall")) if x]
    up = sorted(x for x in lv if x > price + 0.03)
    dn = sorted((x for x in lv if x < price - 0.03), reverse=True)
    room_up = (up[0] - price) / scale if up else 3.0
    room_dn = (price - dn[0]) / scale if dn else 3.0
    tgt_up = next((x for x in up if x - price >= 0.8 * scale), None)
    tgt_dn = next((x for x in dn if price - x >= 0.8 * scale), None)
    return {"room_up": room_up, "room_dn": room_dn, "target_up": tgt_up, "target_dn": tgt_dn}


W = {"trend1": 10, "trend5": 12, "vwap": 10, "mom": 6, "rsi": 5, "vol": 7, "orb": 10,
     "heavy": 8, "parity": 6, "analog": 16}


def score_market(ind, chain, vs, regime, heavy, analog, levels):
    p, scale = ind["price"], ind["scale"]
    c = {}
    c["trend1"] = clamp((ind["ema9"] - ind["ema21"]) / (0.35 * scale), -1, 1)
    e3, e8 = ema(ind["closes5"], 3), ema(ind["closes5"], 8)
    if e3 is None or e8 is None: c["trend5"] = 0.0
    elif e3 > e8 and p > e8: c["trend5"] = 1.0
    elif e3 < e8 and p < e8: c["trend5"] = -1.0
    else: c["trend5"] = 0.3 if e3 > e8 else -0.3
    z = (p - ind["vwap"]) / ind["sigma"]
    c["vwap"] = clamp(z / 1.5, -1, 1) if abs(z) <= 2.2 else -0.3 * sign(z)
    cl = ind["closes"]
    c["mom"] = clamp((cl[-1] - cl[-4]) / (ind["atr1"] * math.sqrt(3)) / 1.5, -1, 1)
    r = ind["rsi"]
    c["rsi"] = 0.3 if r < 22 else -0.3 if r > 78 else clamp((r - 50) / 20, -1, 1)
    c["vol"] = clamp(ind["vr"] - 1, 0, 1) * sign(p - ind["prev"])
    orv = 0.0
    if ind["or_hi"]:
        if p > ind["or_hi"]: orv = 1.0 if p - ind["or_hi"] < 1.2 * scale else 0.4
        elif p < ind["or_lo"]: orv = -1.0 if ind["or_lo"] - p < 1.2 * scale else -0.4
    c["orb"] = orv
    c["heavy"] = heavy
    c["parity"] = clamp((chain["parity"] - p) / (0.3 * scale), -1, 1) if chain and chain.get("parity") else 0.0
    if analog:
        v = clamp((analog["p_up"] - 0.5) / 0.15, -1, 1)
        c["analog"] = v if sign(analog["mean"]) == sign(analog["p_up"] - 0.5) else v * 0.5
    else:
        c["analog"] = 0.0
    w = dict(W)
    if regime == "RANGE":
        for k in ("trend1", "mom", "vol", "orb", "trend5"): w[k] = w[k] * 0.3
        c["mr"] = clamp(-z / 2.0, -1, 1) if abs(z) >= 1.2 else 0.0
        w["mr"] = 14
    S = sum(w[k] * c[k] for k in c)
    d = 1 if S >= 10 else -1 if S <= -10 else 0
    adj = 0.0
    if d:
        room = levels["room_up"] if d > 0 else levels["room_dn"]
        adj = 10 * clamp((room - 0.7) / 1.0, -1, 1)
        pen = 0.0
    near_hi = (ind["day_hi"] - p) < 0.3 * scale
    near_lo = (p - ind["day_lo"]) < 0.3 * scale
    if (d > 0 and near_hi) or (d < 0 and near_lo):
        if vs["range_used"] > 1.8: pen += 14
        elif vs["range_used"] > 1.3: pen += 8
    if DEAD_ZONE[0] <= minutes_ny() <= DEAD_ZONE[1]: pen += 4
    conv = clamp(abs(S) + adj - pen, 0, 100)
    return {"S": S, "dir": d, "conv": conv, "parts": {k: round(w[k] * c[k], 1) for k in c},
            "adj": adj, "pen": pen, "z": z}


# ========================= 12) META MODEL + THOMPSON =========================
def meta_features(sc, analog, ev_ratio, vs, regime):
    d = sc["dir"] or 1
    edge = (analog["p_up"] - 0.5) * d * 4 if analog else 0.0
    return [1.0, sc["conv"] / 100, edge, clamp(ev_ratio if ev_ratio is not None else 0.0, -0.2, 0.5) * 2,
            clamp(vs["range_used"], 0, 2) - 1, 1.0 if regime == "RANGE" else 0.0,
            1.0 if minutes_ny() >= 13 * 60 + 30 else 0.0, clamp(vs["iv_ratio"], 0.5, 1.8) - 1]


def meta_predict(x):
    z = sum(w * v for w, v in zip(STATE["meta_w"], x))
    return 1 / (1 + math.exp(-clamp(z, -30, 30)))


def meta_train(x, y):
    p = meta_predict(x)
    for i in range(len(x)):
        STATE["meta_w"][i] += 0.05 * ((y - p) * x[i] - 0.001 * STATE["meta_w"][i])
    STATE["meta_n"] += 1


def ts_update(strategy, win):
    if not strategy: return
    st = STATE["ts"].setdefault(strategy, {"w": 0.0, "l": 0.0})
    st["w" if win else "l"] += 1
    if st["w"] + st["l"] > 200: st["w"] *= 0.9; st["l"] *= 0.9


def ts_draw(strategy):
    st = STATE["ts"].get(strategy, {"w": 0, "l": 0})
    return random.betavariate(st["w"] + 2, st["l"] + 2)


def perf_mult():
    t = LEDGER["closed_trades"][-20:]
    if len(t) < 10: return 1.0
    wr = sum(1 for x in t if x["pnl"] > 0) / len(t)
    return clamp(0.6 + wr * 0.8, 0.6, 1.3)


# ========================= 13) CANDIDATES + EV =========================
def leg_change(leg, move, horizon):
    g = leg["greeks"]
    delta, gamma, theta = g.get("delta") or 0.0, g.get("gamma") or 0.0, g.get("theta") or 0.0
    decay = min(leg["quote"]["mid"] * 0.4, abs(theta) * horizon / 390.0)
    return delta * move + 0.5 * gamma * move * move - decay


def candidate_ev(c, moves):
    if not moves: return None
    pn = []
    for m in moves:
        v = sum(s * leg_change(l, m, SHADOW_HORIZON) for l, s in c["legs_signed"])
        v = clamp(v, c["clip_lo"], c["clip_hi"])
        pn.append(v)
    friction = 0.6 * sum(l["quote"]["spread"] for l, _ in c["legs_signed"])
    return (sum(pn) / len(pn) - friction) / c["max_loss_ps"]


def build_single(typ, chain, moves):
    legs = [l for l in liquid_legs(chain, typ, TARGET_DELTA_MIN, TARGET_DELTA_MAX)
            if MIN_OPTION_ASK <= l["quote"]["ask"] <= MAX_OPTION_ASK]
    legs.sort(key=lambda l: abs(abs(l["greeks"]["delta"]) - TARGET_DELTA) + l["quote"]["spread_percent"])
    best = None
    for l in legs[:5]:
        ask = l["quote"]["ask"]
        c = {"strategy": typ.upper(), "kind": "SINGLE", "legs_signed": [(l, 1)], "credit": False,
             "max_loss_ps": ask * SINGLE_RISK_FRACTION, "cost_ps": ask, "width": None,
             "clip_lo": -ask, "clip_hi": 1e9, "limit_cap": ask * 1.15}
        c["ev"] = candidate_ev(c, moves)
        if best is None or (c["ev"] or -9) > (best["ev"] or -9): best = c
    return best


def build_vertical(kind, chain, moves):
    typ = "call" if kind in ("BULL_CALL_SPREAD", "BEAR_CALL_SPREAD") else "put"
    debit = kind in ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD")
    legs = liquid_legs(chain, typ, SPREAD_DELTA_MIN, SPREAD_DELTA_MAX)
    pairs = []
    for a in legs:
        for b in legs:
            if a is b: continue
            if kind == "BULL_CALL_SPREAD": lo, sh, ok = a, b, b["strike"] > a["strike"]
            elif kind == "BEAR_PUT_SPREAD": lo, sh, ok = a, b, b["strike"] < a["strike"]
            elif kind == "BULL_PUT_SPREAD": sh, lo, ok = a, b, a["strike"] > b["strike"]
            else: sh, lo, ok = a, b, a["strike"] < b["strike"]
            if not ok: continue
            width = abs(lo["strike"] - sh["strike"])
            if not SPREAD_MIN_WIDTH <= width <= SPREAD_MAX_WIDTH: continue
            net = lo["quote"]["ask"] - sh["quote"]["bid"]          # موجب = دفع
            dl, ds = abs(lo["greeks"]["delta"]), abs(sh["greeks"]["delta"])
            if debit:
                if not 0 < net <= SPREAD_MAX_DEBIT or net >= width * 0.8 or ds >= dl: continue
                score = abs(dl - TARGET_DELTA) * 3 + abs(ds - 0.25)
                ml, mg = net, width - net
            else:
                cr = -net
                if cr < SPREAD_MIN_CREDIT or cr >= width * 0.8 or dl >= ds or ds > 0.42: continue
                score = abs(ds - 0.30) * 3 + abs(dl - 0.12)
                ml, mg = width - cr, cr
            score += lo["quote"]["spread_percent"] + sh["quote"]["spread_percent"]
            pairs.append((score, lo, sh, width, net, ml, mg))
    pairs.sort(key=lambda x: x[0])
    best = None
    for score, lo, sh, width, net, ml, mg in pairs[:10]:
        c = {"strategy": kind, "kind": "SPREAD", "legs_signed": [(lo, 1), (sh, -1)], "credit": not debit,
             "max_loss_ps": ml, "cost_ps": ml, "width": width, "clip_lo": -ml, "clip_hi": mg,
             "limit_cap": net + max(0.02, abs(net) * 0.15)}
        c["ev"] = candidate_ev(c, moves)
        if best is None or (c["ev"] or -9) > (best["ev"] or -9): best = c
    return best


def candidate_strategies(d, vs, regime):
    if STRATEGY_MODE == "SINGLES":
        return ["CALL"] if d > 0 else ["PUT"]
    c = ["CALL", "BULL_CALL_SPREAD", "BULL_PUT_SPREAD"] if d > 0 else ["PUT", "BEAR_PUT_SPREAD", "BEAR_CALL_SPREAD"]
    if STRATEGY_MODE != "AUTO": return [x for x in c if x == STRATEGY_MODE]
    if minutes_ny() >= LATE_SPREAD_AFTER: c = [x for x in c if x not in ("CALL", "PUT")]
    if vs["iv_ratio"] > 1.3: c = [x for x in c if x not in ("CALL", "PUT")]
    if vs["iv_ratio"] < 0.8: c = [x for x in c if "PUT_SPREAD" not in x or "BEAR" in x or "BULL_CALL" in x or True and x.startswith(("BEAR_PUT", "BULL_CALL")) or x in ("CALL", "PUT")]
    return c


def build_candidate(strategy, chain, moves):
    if strategy in ("CALL", "PUT"): return build_single(strategy.lower(), chain, moves)
    return build_vertical(strategy, chain, moves)


# ========================= 14) SIZING =========================
def get_buying_power():
    a = get_account()
    bp = safe_float(a.get("options_buying_power"), safe_float(a.get("buying_power"), 0.0))
    if not bp or bp <= 0: raise EntryBlocked("Buying power غير كافٍ.")
    return bp


def size_position(c, equity, conv, meta_mult):
    frac = RISK_BASE + (RISK_MAX - RISK_BASE) * clamp((conv - CONV_MIN) / (100 - CONV_MIN), 0, 1)
    frac = min(frac * meta_mult * perf_mult(), RISK_MAX * 1.3)
    q = min(math.floor(equity * frac / (c["max_loss_ps"] * 100)),
            math.floor(equity * MAX_PREMIUM_FRACTION / (c["cost_ps"] * 100)),
            math.floor(get_buying_power() / (c["cost_ps"] * 100)), MAX_CONTRACTS)
    return max(0, int(q))


# ========================= 15) ORDERS =========================
FINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def get_order(oid): return trading_api("GET", f"/v2/orders/{oid}") if oid else None


def cancel_order(oid):
    try:
        trading_api("DELETE", f"/v2/orders/{oid}")
        return True
    except APIError as exc:
        log.error("CANCEL FAILED %s: %s", oid, exc)
        return False


def validate_qty(q):
    if not isinstance(q, int) or q < 1 or q > MAX_CONTRACTS:
        raise SafetyHalt("كمية خارج الحدود.")


def validate_symbol(s):
    if not OPTION_RE.match(str(s or "")): raise SafetyHalt(f"رمز خيارات غير صالح: {s}")


def submit_order_payload(payload):
    validate_qty(int(payload.get("qty", 0)))
    cid = f"SAFAA115-{today_ny().strftime('%Y%m%d')}-{uuid.uuid4().hex[:12]}"
    payload["client_order_id"] = cid
    try:
        r = trading_api("POST", "/v2/orders", payload=payload)
    except APIError as err:
        try:
            r = trading_api("GET", "/v2/orders:by_client_order_id", {"client_order_id": cid})
        except APIError:
            raise err
    if not isinstance(r, dict) or not r.get("id"): raise APIError("لم يؤكد الوسيط الأمر.")
    LEDGER["orders"][r["id"]] = {"id": r["id"], "status": r.get("status"), "created_at": utc_iso(),
                                 "updated_at": utc_iso(), "filled_qty": 0, "avg": None}
    save_ledger()
    return r


def update_order_record(oid, order):
    rec = LEDGER["orders"].get(oid)
    if rec:
        rec.update(status=order.get("status"), filled_qty=int(safe_float(order.get("filled_qty"), 0) or 0),
                   avg=safe_float(order.get("filled_avg_price")), updated_at=utc_iso())
        save_ledger()


def mleg_payload(meta, qty, price, closing, market=False):
    long_sym, short_sym = meta["long_symbol"], meta["short_symbol"]
    validate_symbol(long_sym); validate_symbol(short_sym)
    if long_sym == short_sym: raise SafetyHalt("نفس العقد للرجلين.")
    if closing:
        legs = [{"symbol": long_sym, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
                {"symbol": short_sym, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"}]
    else:
        legs = [{"symbol": long_sym, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
                {"symbol": short_sym, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"}]
    p = {"order_class": "mleg", "qty": str(qty), "time_in_force": "day", "legs": legs}
    if market:
        p["type"] = "market"
    else:
        price = round(float(price), 2)
        if price == 0: price = 0.01
        p.update(type="limit", limit_price=f"{price:.2f}")
    return p


def entry_frac_price(meta, snaps, frac):
    if meta["kind"] == "SINGLE":
        q = extract_quote(snaps.get(meta["symbol"]))
        if not q: raise EntryBlocked("لا سعر حديث للعقد.")
        if q["ask"] > meta["limit_cap"]: raise EntryBlocked("السعر ابتعد عن الحد.")
        return round(max(0.01, q["bid"] + frac * (q["ask"] - q["bid"])), 2)
    lq = extract_quote(snaps.get(meta["long_symbol"]))
    sq = extract_quote(snaps.get(meta["short_symbol"]))
    if not lq or not sq: raise EntryBlocked("لا أسعار حديثة للسبريد.")
    best, worst = lq["bid"] - sq["ask"], lq["ask"] - sq["bid"]
    if worst > meta["limit_cap"]: raise EntryBlocked("سعر السبريد ابتعد.")
    price = round(best + frac * (worst - best), 2)
    if meta["credit"] and price > -0.01: raise EntryBlocked("Credit غير كافٍ.")
    if not meta["credit"] and price < 0.01: price = 0.01
    return price


def submit_entry(meta, step):
    syms = [meta["symbol"]] if meta["kind"] == "SINGLE" else [meta["long_symbol"], meta["short_symbol"]]
    for s in syms: validate_symbol(s)
    snaps = get_option_snapshots(syms)
    
    # 1. حالة العقد المفرد (لم تتغير)
    if meta["kind"] == "SINGLE":
        price = entry_frac_price(meta, snaps, ENTRY_STEPS[step])
        payload = {"symbol": meta["symbol"], "qty": str(meta["quantity"]), "side": "buy", "type": "limit",
                   "time_in_force": "day", "limit_price": f"{price:.2f}", "position_intent": "buy_to_open"}
        r = submit_order_payload(payload)
        meta.update(status="PENDING_ENTRY", entry_step=step, step_at=utc_iso(), entry_limit=abs(price),
                    entry_filled=0, entry_average_price=None, exit_order_id=None, exit_attempts=0,
                    realized_pnl=meta.get("realized_pnl", 0.0), order_id=r["id"])
        
    # 2. حالة السبريد (نظام الـ Legging الجديد)
    else:
        lq = extract_quote(snaps.get(meta["long_symbol"]))
        if not lq: raise EntryBlocked("لا سعر حديث للعقد الأول (Long).")
        
        # نحاول نصطاد العقد الأول بسعر أقرب للـ Bid لتقليل التكلفة
        leg1_price = round(lq["bid"] + ENTRY_STEPS[step] * (lq["ask"] - lq["bid"]), 2)
        leg1_price = max(0.01, leg1_price)
        
        payload = {"symbol": meta["long_symbol"], "qty": str(meta["quantity"]), "side": "buy", "type": "limit",
                   "time_in_force": "day", "limit_price": f"{leg1_price:.2f}", "position_intent": "buy_to_open"}
        
        r = submit_order_payload(payload)
        meta.update(status="PENDING_LEG1", entry_step=step, step_at=utc_iso(), leg1_limit=leg1_price,
                    leg1_order_id=r["id"], entry_filled=0, exit_order_id=None, exit_attempts=0,
                    realized_pnl=meta.get("realized_pnl", 0.0), order_id=r["id"])

    meta.setdefault("created_at", utc_iso())
    STATE["position_meta"][r["id"]] = meta
    save_state()
    ledger_event("ENTRY_SUBMITTED", {"order": r["id"], "strategy": meta["strategy"], "step": step})
    log.info("ENTRY SUBMITTED | %s | Qty=%s | Step=%s", meta["strategy"], meta["quantity"], step)
    return r



def entry_window_ok():
    return (not STATE.get("halted") and entry_time_allowed() and not force_exit_time_reached()
            and not news_state() and market_open())


def handle_entry_order(oid, meta):
    # حالة العقود المفردة القديمة
    if meta.get("status") == "PENDING_ENTRY":
        order = get_order(meta.get("order_id", oid))
        if not order: return
        update_order_record(oid, order)
        status = str(order.get("status", "")).lower()
        filled = int(safe_float(order.get("filled_qty"), 0) or 0)
        
        if status in FINAL:
            if filled > 0:
                avg = abs(safe_float(order.get("filled_avg_price"), meta.get("entry_limit", 0.0)))
                meta.update(status="OPEN", entry_filled=filled, entry_average_price=avg, risk_base=avg,
                            opened_at=utc_iso(), peak_return=0.0, partial_done=False)
                STATE["trades_today"] += 1
                save_state()
                telegram_send(f"✅ تم الدخول\n{meta['strategy']} × {filled} @ {avg:.2f}\nPAPER")
            else:
                STATE["position_meta"].pop(oid, None)
                save_state()
            return
        if seconds_since(meta.get("step_at")) > ENTRY_STEP_SECONDS or force_exit_time_reached():
            cancel_order(oid)

    # المرحلة الأولى من السبريد (شراء الـ Long)
    elif meta.get("status") == "PENDING_LEG1":
        leg1_id = meta.get("leg1_order_id")
        order = get_order(leg1_id)
        if not order: return
        status = str(order.get("status", "")).lower()
        filled = int(safe_float(order.get("filled_qty"), 0) or 0)
        
        if status in FINAL:
            if filled > 0:
                meta["leg1_avg"] = safe_float(order.get("filled_avg_price"), meta.get("leg1_limit", 0.0))
                meta["leg1_filled"] = filled
                meta["status"] = "PENDING_LEG2"
                
                # إطلاق العقد الثاني (البيع) فوراً لضمان اكتمال السبريد!
                snaps = get_option_snapshots([meta["short_symbol"]])
                sq = extract_quote(snaps.get(meta["short_symbol"]))
                leg2_price = round(sq["bid"] + 0.1 * (sq["ask"] - sq["bid"]), 2) if sq else 0.01
                
                p2 = {"symbol": meta["short_symbol"], "qty": str(filled), "side": "sell", "type": "limit",
                      "time_in_force": "day", "limit_price": f"{max(0.01, leg2_price):.2f}", "position_intent": "sell_to_open"}
                
                r2 = submit_order_payload(p2)
                meta.update(leg2_order_id=r2["id"], leg2_limit=leg2_price, step_at=utc_iso())
                save_state()
                log.info("LEG 1 FILLED! Firing LEG 2 (Short) -> %s", meta["short_symbol"])
            else:
                STATE["position_meta"].pop(oid, None)
                save_state()
            return
        if seconds_since(meta.get("step_at")) > ENTRY_STEP_SECONDS or force_exit_time_reached():
            cancel_order(leg1_id)

    # المرحلة الثانية من السبريد (تأكيد اكتمال الصفقة)
    elif meta.get("status") == "PENDING_LEG2":
        leg2_id = meta.get("leg2_order_id")
        order2 = get_order(leg2_id)
        if not order2: return
        status2 = str(order2.get("status", "")).lower()
        filled2 = int(safe_float(order2.get("filled_qty"), 0) or 0)
        
        if status2 in FINAL:
            if filled2 > 0:
                # اكتمل السبريد بنجاح! نقوم بحساب التكلفة الإجمالية بدقة
                leg2_avg = safe_float(order2.get("filled_avg_price"), meta.get("leg2_limit", 0.0))
                net_avg = meta["leg1_avg"] - leg2_avg  # التكلفة الصافية
                risk_base = meta["width"] - abs(net_avg) if meta.get("credit") else abs(net_avg)
                
                meta.update(status="OPEN", entry_filled=filled2, entry_average_price=abs(net_avg),
                            risk_base=risk_base, opened_at=utc_iso(), peak_return=0.0, partial_done=False)
                STATE["trades_today"] += 1
                save_state()
                telegram_send(f"✅ تم اصطياد السبريد (Legging)\n{meta['strategy']} × {filled2}\nصافي التكلفة: {abs(net_avg):.2f}\nPAPER")
            else:
                # لم يتنفذ العقد الثاني؟ نرسل أمر ماركت فوراً لمنع الانكشاف (Naked Long Risk)
                log.warning("LEG 2 FAILED TO FILL! Converting to MARKET order to lock spread.")
                p2_market = {"symbol": meta["short_symbol"], "qty": str(meta["leg1_filled"]), "side": "sell", 
                             "type": "market", "time_in_force": "day", "position_intent": "sell_to_open"}
                r_market = submit_order_payload(p2_market)
                meta["leg2_order_id"] = r_market["id"]
                save_state()
            return
        
        # إذا تأخر التنفيذ لأكثر من 5 ثوانٍ، نعيد إرساله بسعر السوق
        if seconds_since(meta.get("step_at")) > 5:
            cancel_order(leg2_id)


def pnl_per_share(meta, exit_avg):
    e = safe_float(meta.get("entry_average_price"), 0.0)
    return (e - exit_avg) if meta.get("credit") else (exit_avg - e)


def handle_exit_order(oid, meta):
    xid = meta.get("exit_order_id")
    if not xid:
        meta["status"] = "OPEN"; save_state(); return
    order = get_order(xid)
    if not order: return
    update_order_record(xid, order)
    status = str(order.get("status", "")).lower()
    filled = int(safe_float(order.get("filled_qty"), 0) or 0)
    if status in FINAL:
        reason = meta.get("exit_reason")
        if filled > 0:
            avg = abs(safe_float(order.get("filled_avg_price"), 0.0))
            meta["realized_pnl"] = meta.get("realized_pnl", 0.0) + pnl_per_share(meta, avg) * filled * 100
            meta["entry_filled"] = int(meta["entry_filled"]) - filled
        meta["exit_order_id"] = None; meta["exit_qty"] = None
        if meta["entry_filled"] <= 0:
            total = meta.get("realized_pnl", 0.0)
            STATE["consecutive_losses"] = STATE["consecutive_losses"] + 1 if total < 0 else 0
            ts_update(meta["strategy"], total > 0)
            LEDGER["closed_trades"].append({"order": oid, "strategy": meta["strategy"], "pnl": total,
                                            "reason": reason, "closed_at": utc_iso()})
            STATE["position_meta"].pop(oid, None)
            save_state(); save_ledger()
            ledger_event("TRADE_CLOSED", {"order": oid, "pnl": total, "reason": reason})
            telegram_send(f"🔔 إغلاق {meta['strategy']}\nالسبب: {reason}\nالنتيجة: {format_money(total)}\nPAPER")
        else:
            meta["status"] = "OPEN"
            if reason == "PARTIAL" and filled > 0: meta["partial_done"] = True
            else: meta["exit_attempts"] = int(meta.get("exit_attempts", 0)) + 1
            save_state()
        return
    if seconds_since(meta.get("exit_submitted_at")) > EXIT_ORDER_TIMEOUT:
        cancel_order(xid)


def process_orders():
    for oid, meta in list(STATE["position_meta"].items()):
        try:
            if meta.get("status") in ("PENDING_ENTRY", "PENDING_LEG1", "PENDING_LEG2"):
                handle_entry_order(oid, meta)
            elif meta.get("status") == "PENDING_EXIT":
                handle_exit_order(oid, meta)
        except APIError as exc:
            log.error("ORDER API ERROR %s: %s", oid, exc)
        except SafetyHalt:
            raise
        except Exception:
            log.exception("ORDER ERROR %s", oid)
    save_state()



# ========================= 16) POSITIONS / RECONCILIATION =========================
def get_option_positions():
    r = trading_api("GET", "/v2/positions")
    if not isinstance(r, list): raise APIError("تعذر قراءة المراكز.")
    out = []
    for p in r:
        if str(p.get("asset_class", "")).lower() != "us_option": continue
        sym = str(p.get("symbol", "")).strip()
        qty = safe_float(p.get("qty"), 0.0)
        if not OPTION_RE.match(sym) or not qty: continue
        out.append({"symbol": sym, "signed": int(round(abs(qty) if str(p.get("side")).lower() == "long" else -abs(qty)))})
    return out


def expected_positions():
    exp = {}
    for m in STATE["position_meta"].values():
        q = int(m.get("entry_filled", 0))
        if m.get("status") != "OPEN" or q <= 0: continue
        if m["kind"] == "SINGLE":
            exp[m["symbol"]] = exp.get(m["symbol"], 0) + q
        else:
            exp[m["long_symbol"]] = exp.get(m["long_symbol"], 0) + q
            exp[m["short_symbol"]] = exp.get(m["short_symbol"], 0) - q
    return exp


def reconcile():
    if any_pending_orders(): return True
    actual = {p["symbol"]: p["signed"] for p in get_option_positions()}
    exp = expected_positions()
    bad = [(s, exp.get(s, 0), actual.get(s, 0)) for s in set(actual) | set(exp) if exp.get(s, 0) != actual.get(s, 0)]
    STATE["last_reconciliation"] = utc_iso()
    save_state()
    if bad:
        for s, e, a in bad: log.critical("MISMATCH %s expected=%s actual=%s", s, e, a)
        halt_bot("اختلاف بين المراكز الفعلية والسجل المحلي.")
        telegram_send("⛔ اختلاف في المراكز، إيقاف احترازي.")
        return False
    return True


def periodic_reconcile():
    prev = parse_ts(STATE.get("last_reconciliation"))
    if prev and (utc_now() - prev).total_seconds() < RECONCILE_INTERVAL: return True
    return reconcile()


# ========================= 17) POSITION MANAGEMENT =========================
def position_mark(meta, snaps):
    if meta["kind"] == "SINGLE":
        q = extract_quote(snaps.get(meta["symbol"]))
        if not q: return None
        return {"ret": (q["bid"] - meta["entry_average_price"]) / meta["entry_average_price"]}
    lq = extract_quote(snaps.get(meta["long_symbol"]))
    sq = extract_quote(snaps.get(meta["short_symbol"]))
    if not lq or not sq: return None
    e = meta["entry_average_price"]
    pnl = (lq["bid"] - sq["ask"]) - (-e if meta["credit"] else e)
    return {"ret": pnl / meta["risk_base"]}


def decide_exit(meta, ret, held, price):
    peak = max(meta.get("peak_return", 0.0), ret)
    meta["peak_return"] = peak; meta["last_return"] = ret
    d = meta.get("dir", 1)
    if ret <= -HARD_STOP: return "HARD_STOP"
    inval = meta.get("inval")
    if price and inval and held >= 90 and (price - inval) * d < 0: return "INVALIDATION"
    if peak >= BREAKEVEN_AFTER and ret < 0.02: return "BREAKEVEN_STOP"
    if ret >= TP_FULL: return "TAKE_PROFIT"
    if peak >= TRAIL_ACTIVATE and peak - ret >= max(0.15, 0.35 * peak): return "TRAILING"
    tgt = meta.get("target")
    if price and tgt and ret > 0.05 and (price - tgt) * d >= -0.02: return "TARGET_LEVEL"
    sc = CTX.get("score")
    if sc and held >= 120 and sc["dir"] == -d and sc["conv"] >= REVERSAL_CONV and ret < 0.15: return "REVERSAL"
    if held > TIME_STOP_SEC and abs(ret) < 0.08 and not meta.get("partial_done"): return "TIME_STOP"
    if not meta.get("partial_done") and int(meta["entry_filled"]) >= 2 and ret >= PARTIAL_AT: return "PARTIAL"
    return None


def exit_position(oid, meta, qty, reason, snaps, market=False):
    if meta.get("exit_order_id"): return
    attempts = int(meta.get("exit_attempts", 0))
    if attempts >= 3 or (reason in ("TIME_EXIT", "NEWS_EXIT") and attempts >= 1): market = True
    if meta["kind"] == "SINGLE":
        validate_symbol(meta["symbol"])
        q = extract_quote(snaps.get(meta["symbol"]))
        p = {"symbol": meta["symbol"], "qty": str(qty), "side": "sell", "time_in_force": "day",
             "position_intent": "sell_to_close"}
        if market or not q:
            p["type"] = "market"
        else:
            pad = max(0.01, q["bid"] * 0.04) * (1 + attempts)
            p.update(type="limit", limit_price=f"{max(0.01, round(q['bid'] - pad, 2)):.2f}")
    else:
        lq = extract_quote(snaps.get(meta["long_symbol"]))
        sq = extract_quote(snaps.get(meta["short_symbol"]))
        if market or not lq or not sq:
            p = mleg_payload(meta, qty, 0, closing=True, market=True)
        else:
            net = sq["ask"] - lq["bid"]
            pad = max(0.01, abs(net) * 0.05) * (1 + attempts)
            p = mleg_payload(meta, qty, net + pad, closing=True)
    r = submit_order_payload(p)
    meta.update(exit_order_id=r["id"], exit_submitted_at=utc_iso(), exit_qty=qty, exit_reason=reason,
                status="PENDING_EXIT")
    save_state()
    ledger_event("EXIT_SUBMITTED", {"entry": oid, "exit": r["id"], "reason": reason, "market": p.get("type") == "market"})
    log.warning("EXIT %s | %s | Qty=%s | %s", reason, meta["strategy"], qty, p.get("type"))


def ctx_price():
    ind = CTX.get("ind")
    if ind and CTX.get("ts", 0) > time.time() - 180: return ind["price"]
    return None


def manage_one(oid, meta):
    qty = int(meta.get("entry_filled", 0))
    if qty <= 0: return
    syms = [meta["symbol"]] if meta["kind"] == "SINGLE" else [meta["long_symbol"], meta["short_symbol"]]
    snaps = get_option_snapshots(syms)
    mark = position_mark(meta, snaps)
    forced = "TIME_EXIT" if force_exit_time_reached() else ("NEWS_EXIT" if news_state() == "FLAT" else None)
    if forced:
        exit_position(oid, meta, qty, forced, snaps, market=(mark is None))   # إصلاح: لا نترك المركز بلا خروج
        return
    if mark is None:
        log.warning("لا سعر حديث للمركز %s", oid)
        return
    held = seconds_since(meta.get("opened_at"))
    reason = decide_exit(meta, mark["ret"], held, ctx_price())
    log.info("POSITION %s | ret=%.1f%% | peak=%.1f%% | %s", meta["strategy"], mark["ret"] * 100,
             meta.get("peak_return", 0) * 100, reason or "HOLD")
    save_state()
    if reason == "PARTIAL":
        exit_position(oid, meta, max(1, qty // 2), "PARTIAL", snaps)
    elif reason:
        exit_position(oid, meta, qty, reason, snaps)


def manage_positions():
    for oid, meta in list(STATE["position_meta"].items()):
        if meta.get("status") != "OPEN": continue
        try:
            manage_one(oid, meta)
        except SafetyHalt as exc:
            halt_bot(str(exc)); return
        except APIError as exc:
            log.error("MANAGE API ERROR %s: %s", oid, exc)
        except Exception:
            log.exception("MANAGE ERROR %s", oid)


# ========================= 18) CONTEXT + SHADOW =========================
def build_context():
    bars = get_spy_bars()
    sb = session_bars(bars)
    if len(sb) < MIN_SESSION_BARS: raise MarketDataUnavailable("جلسة قصيرة.")
    ind = compute_indicators(sb)
    rows = day_feature_rows(sb)
    feat = rows[-1]["feat"] if rows and rows[-1]["i"] == len(sb) - 1 else None
    CTX.update(ind=ind, sb=sb, feat=feat, regime=classify_regime(ind), ts=time.time(),
               candle_id=hashlib.sha256(f"{UNDERLYING}|{ind['t']}".encode()).hexdigest()[:24])
    return CTX


def record_shadow(sc, ind, pick, mx, taken, why):
    LEDGER["shadow"].append({"id": make_id("SH"), "t": ind["t"], "price": ind["price"], "scale": ind["scale"],
                             "dir": sc["dir"], "conv": round(sc["conv"], 1), "pick": pick, "mx": mx,
                             "taken": taken, "why": why, "resolved": False,
                             "hour": ny_now().hour})
    LEDGER["shadow"] = LEDGER["shadow"][-1500:]
    save_ledger()


def resolve_shadow():
    sb = CTX.get("sb")
    if not sb: return
    now, changed = utc_now(), False
    for rec in LEDGER["shadow"][-250:]:
        if rec.get("resolved"): continue
        t0 = parse_ts(rec["t"])
        if t0.astimezone(NY).date() != today_ny():
            rec.update(resolved=True, y=None); changed = True; continue
        target = t0 + timedelta(minutes=SHADOW_HORIZON)
        if now < target + timedelta(minutes=1): continue
        later = [b for b in sb if b["t"] >= target]
        if not later: continue
        fwd = (later[0]["close"] - rec["price"]) / rec["scale"]
        dret = rec["dir"] * fwd
        y = 1 if dret > SHADOW_WIN_ATR else 0
        rec.update(resolved=True, fwd=round(fwd, 3), dret=round(dret, 3), y=y)
        meta_train(rec["mx"], y)
        ts_update(rec.get("pick"), y)
        changed = True
    if changed:
        save_ledger(); save_state()


# ========================= 19) DECISION ENGINE =========================
def validate_entry(d):
    ensure_not_halted()
    if not market_open(): raise EntryBlocked("السوق مغلق.")
    if not entry_time_allowed(): raise EntryBlocked("خارج وقت الدخول.")
    if force_exit_time_reached(): raise EntryBlocked("وقت الإغلاق.")
    if news_state(): raise EntryBlocked("نافذة خبر اقتصادي.")
    check_daily_risk()
    metas = STATE["position_meta"]
    if len(metas) >= MAX_OPEN_POSITIONS: raise EntryBlocked("حد المراكز.")
    if len(metas) == 1:
        m = next(iter(metas.values()))
        if not (m.get("status") == "OPEN" and m.get("dir") == d and m.get("peak_return", 0) >= ADD_ON_MIN_RETURN):
            raise EntryBlocked("لا إضافة: المركز الحالي لم يثبت.")
    if STATE["trades_today"] >= MAX_TRADES_PER_DAY: raise EntryBlocked("حد صفقات اليوم.")
    if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES: raise EntryBlocked("خسائر متتالية.")


def evaluate_and_enter():
    ind, regime = CTX["ind"], CTX["regime"]
    price, scale = ind["price"], ind["scale"]
    chain = None
    try:
        chain = build_chain(price)
    except (APIError, EntryBlocked) as exc:
        note_reject(f"لا سلسلة خيارات: {exc}")
    analog = analog_forecast(CTX.get("feat"))
    vs = vol_state(ind, chain)
    levels = collect_levels(price, ind, chain, scale)
    sc = score_market(ind, chain, vs, regime, heavy_bias(), analog, levels)
    CTX["score"] = sc
    d, conv = sc["dir"], sc["conv"]
    why = (f"SPY {price:.2f} | {regime} | dir={d:+d} conv={conv:.0f} | VWAPσ={sc['z']:+.2f} "
           f"| IV/RV={vs['iv_ratio']:.2f} | range={vs['range_used']:.2f} | "
           f"analog={'%.0f%%' % (analog['p_up'] * 100) if analog else 'n/a'} | parts={sc['parts']}")
    log.info(why)
    STATE["last_decision"] = why
    save_state()
    if d == 0 or not chain:
        return None
    mx0 = meta_features(sc, analog, None, vs, regime)
    need = CONV_MIN + (0 if analog else 10)
    moves = [f * scale * 0.7 for f in analog["fwds"]] if analog else None
    best, best_score = None, -1e9
    for strat in candidate_strategies(d, vs, regime):
        try:
            c = build_candidate(strat, chain, moves)
        except Exception:
            log.exception("candidate %s", strat); continue
        if not c: continue
        ev = c["ev"]
        if analog and (ev is None or ev < MIN_EV_RATIO): continue
        s = (ev if ev is not None else 0.01) * (0.6 + ts_draw(strat))
        if s > best_score: best, best_score = c, s
    pick = best["strategy"] if best else None
    ev = best["ev"] if best else None
    mx = meta_features(sc, analog, ev, vs, regime) if best else mx0
    p_meta = meta_predict(mx)
    reason = None
    if conv < need: reason = f"قناعة {conv:.0f} < {need}"
    elif not best: reason = "لا استراتيجية بحافة EV كافية"
    elif STATE["meta_n"] >= META_MIN_N and p_meta < META_MIN_P: reason = f"النموذج الثاني يرفض (p={p_meta:.2f})"
    if not reason:
        try:
            validate_entry(d)
            equity = get_equity()
            mult = clamp(0.5 + (p_meta - 0.4) * 3, 0.5, 1.6) if STATE["meta_n"] >= META_MIN_N else 1.0
            qty = size_position(best, equity, conv, mult)
            if qty < 1: raise EntryBlocked("رأس المال لا يسمح بحجم دخول.")
            lg = best["legs_signed"]
            meta = {"kind": best["kind"], "strategy": best["strategy"], "dir": d, "credit": best["credit"],
                    "quantity": qty, "width": best["width"], "limit_cap": best["limit_cap"],
                    "option_type": lg[0][0]["type"], "expiration": lg[0][0]["expiration"],
                    "inval": price - d * STOP_ATR_MULT * scale,
                    "target": levels["target_up"] if d > 0 else levels["target_dn"],
                    "conv": conv, "ev_text": f"{ev:.2f}" if ev is not None else "-"}
            if best["kind"] == "SINGLE":
                meta["symbol"] = lg[0][0]["symbol"]
            else:
                meta.update(long_symbol=lg[0][0]["symbol"], short_symbol=lg[1][0]["symbol"],
                            long_strike=lg[0][0]["strike"], short_strike=lg[1][0]["strike"])
            r = submit_entry(meta, 0)
            telegram_send(f"🚨 أمر دخول\n{best['strategy']} × {qty}\nSPY {price:.2f} | {regime}\n"
                          f"قناعة {conv:.0f} | EV {meta['ev_text']} | meta {p_meta:.2f}\nPAPER")
            record_shadow(sc, ind, pick, mx, True, "ENTERED")
            return r
        except EntryBlocked as exc:
            reason = str(exc)
    note_reject(reason)
    STATE["last_decision"] += f"\n→ مرفوض: {reason}"
    if conv >= SHADOW_MIN_SCORE: record_shadow(sc, ind, pick, mx, False, reason)
    return None


# ========================= 20) TELEGRAM / REPORTS =========================
def telegram_send(msg):
    """بدون Telegram: تسجيل في الشاشة وفي ملف التنبيهات."""
    text = str(msg)
    log.info("📣 %s", text.replace("\n", " | "))
    try:
        with open(BASE_DIR / "safaa_notifications.log", "a", encoding="utf-8") as f:
            f.write(f"{utc_iso()}\n{text}\n\n")
    except Exception:
        pass
    return True


def hit_rates(recs):
    by_pick, by_hour = {}, {}
    for r in recs:
        for table, key in ((by_pick, r.get("pick") or "-"), (by_hour, r.get("hour"))):
            w, n = table.get(key, (0, 0)); table[key] = (w + r["y"], n + 1)
    return by_pick, by_hour


def stats_message(today_only=False):
    recs = [r for r in LEDGER["shadow"] if r.get("resolved") and r.get("y") is not None]
    if today_only:
        recs = [r for r in recs if parse_ts(r["t"]).astimezone(NY).date() == today_ny()]
    closed = LEDGER["closed_trades"]
    if today_only:
        closed = [t for t in closed if parse_ts(t["closed_at"]).astimezone(NY).date() == today_ny()]
    lines = [f"📊 {'تقرير اليوم' if today_only else 'إحصائيات'} v{VERSION}"]
    wins = sum(1 for t in closed if t["pnl"] > 0)
    lines.append(f"صفقات مغلقة: {len(closed)} | رابحة: {wins} | صافي: {format_money(sum(t['pnl'] for t in closed))}")
    if recs:
        bp, bh = hit_rates(recs)
        lines.append(f"إشارات الظل: {len(recs)} | إصابة: {sum(r['y'] for r in recs) / len(recs) * 100:.0f}%")
        lines.append("حسب الاستراتيجية: " + " | ".join(f"{k} {w / n * 100:.0f}% ({n})" for k, (w, n) in bp.items()))
        lines.append("حسب الساعة: " + " | ".join(f"{k}h {w / n * 100:.0f}% ({n})" for k, (w, n) in sorted(bh.items())))
    lines.append(f"عينات النموذج الثاني: {STATE['meta_n']}")
    if STATE.get("day_rejects"):
        top = sorted(STATE["day_rejects"].items(), key=lambda x: -x[1])[:5]
        lines.append("أكثر أسباب الرفض: " + " | ".join(f"{k} ({v})" for k, v in top))
    return "\n".join(lines)


def status_message():
    try:
        a = get_account()
        halted = STATE.get("halted")
        return "\n".join([f"💎 صَفاء التحليل v{VERSION} | PAPER",
                          f"الحالة: {'متوقف: ' + str(STATE.get('halt_reason')) if halted else 'يعمل'}",
                          f"Equity: {format_money(a.get('equity'))}",
                          f"صافي اليوم: {format_money(STATE.get('daily_pnl', 0))}",
                          f"صفقات اليوم: {STATE['trades_today']} | خسائر متتالية: {STATE['consecutive_losses']}",
                          f"مراكز قائمة: {len(STATE['position_meta'])}",
                          "تحليل احتمالي — لا ضمان للربح."])
    except Exception as exc:
        return f"تعذر استخراج الحالة: {exc}"


def handle_command(text, chat_id):
    if str(chat_id) != TELEGRAM_CHAT_ID: return
    cmd = str(text or "").strip().lower().split("@")[0]
    if cmd in ("/start", "/help"):
        telegram_send("/status الحالة\n/why سبب آخر قرار\n/stats إحصائيات\n/report تقرير اليوم\n"
                      "/risk الحدود\n/halt إيقاف\n/resume استئناف")
    elif cmd == "/status": telegram_send(status_message())
    elif cmd == "/why": telegram_send(STATE.get("last_decision", "-"))
    elif cmd == "/stats": telegram_send(stats_message())
    elif cmd == "/report": telegram_send(stats_message(True))
    elif cmd == "/risk":
        telegram_send(f"Risk {RISK_BASE * 100:.0f}-{RISK_MAX * 100:.0f}% | DD {MAX_DAILY_DRAWDOWN * 100:.0f}% | "
                      f"Trades {MAX_TRADES_PER_DAY} | Contracts {MAX_CONTRACTS} | Mode {STRATEGY_MODE} | PAPER")
    elif cmd == "/halt":
        halt_bot("طلب من Telegram."); telegram_send("⛔ إيقاف: لا دخول جديد.")
    elif cmd == "/resume":
        STATE.update(halted=False, halt_reason=None, halt_scope=None); save_state()
        telegram_send("▶️ تم رفع الإيقاف.")


def poll_telegram():
    """إيقاف/استئناف عن طريق إنشاء ملف في مجلد البوت."""
    halt_file = BASE_DIR / "HALT"
    resume_file = BASE_DIR / "RESUME"
    try:
        if resume_file.exists():
            STATE.update(halted=False, halt_reason=None, halt_scope=None)
            save_state()
            resume_file.unlink()
            if halt_file.exists():
                halt_file.unlink()
            log.warning("تم رفع الإيقاف عبر ملف RESUME.")
        elif halt_file.exists() and not STATE.get("halted"):
            halt_bot("ملف HALT موجود.")
    except Exception:
        log.exception("File command error.")


def maybe_daily_report():
    n = ny_now()
    if n.weekday() >= 5 or minutes_ny() < 16 * 60 + 5 or STATE.get("report_day") == today_str(): return
    telegram_send(stats_message(True))
    bdir = BASE_DIR / "backup"; bdir.mkdir(exist_ok=True)
    for f in (STATE_FILE, LEDGER_FILE):
        if f.exists(): shutil.copy(f, bdir / f"{today_str()}_{f.name}")
    STATE["report_day"] = today_str()
    save_state()


# ========================= 21) MAIN CYCLE =========================
def run_one_cycle():
    try:
        poll_telegram()
        refresh_daily_baseline()
        maybe_daily_report()
        if not market_open(): return
        ensure_analog()
        try:
            build_context()
        except MarketDataUnavailable as exc:
            log.warning("MARKET DATA: %s", exc)
        if not STATE.get("halted"): check_daily_risk()
        process_orders()
        manage_positions()
        resolve_shadow()
        if STATE.get("halted"): return
        if not periodic_reconcile(): return
        if force_exit_time_reached() or not entry_time_allowed(): return
        if CTX.get("ts", 0) < time.time() - SPY_MAX_BAR_AGE: return
        if CTX.get("candle_id") == STATE.get("last_candle_id"): return
        STATE["last_candle_id"] = CTX["candle_id"]
        save_state()
        evaluate_and_enter()
    except EntryBlocked as exc:
        note_reject(str(exc))
    except SafetyHalt as exc:
        if not STATE.get("halted"): halt_bot(str(exc))
    except MarketDataUnavailable as exc:
        log.warning("MARKET DATA: %s", exc)
    except APIError as exc:
        log.error("BROKER API ERROR: %s", exc)
    except Exception:
        log.exception("UNHANDLED CYCLE ERROR.")


def startup_checks():
    if not API_KEY or not API_SECRET: raise SafetyHalt("أضف APCA_API_KEY_ID و APCA_API_SECRET_KEY.")
    a = get_account(0)
    if a.get("status") != "ACTIVE": raise SafetyHalt("الحساب ليس ACTIVE.")
    if a.get("trading_blocked") or a.get("account_blocked"): raise SafetyHalt("الحساب محظور.")
    if safe_float(a.get("equity"), 0) <= 0: raise SafetyHalt("Equity غير صالح.")
    log.info("STARTUP OK | Equity=%s | Paper", a.get("equity"))


def main():
    log.warning("SAFAA v%s | PAPER ONLY | MODE=%s", VERSION, STRATEGY_MODE)
    acquire_instance_lock()
    startup_checks()
    initialize_storage()
    ensure_analog()
    reconcile()
    telegram_send(f"💎 صَفاء التحليل v{VERSION} بدأ.\nAdaptive Engine | PAPER ONLY")
    try:
        while True:
            t0 = time.monotonic()
            run_one_cycle()
            time.sleep(max(1, SCAN_SECONDS - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        log.warning("Manual shutdown.")
    telegram_send(f"⛔ صَفاء التحليل v{VERSION} توقف.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        STATE = read_json(STATE_FILE)
        LEDGER = read_json(LEDGER_FILE)
        print(stats_message())
        print("\nآخر قرار:\n" + str(STATE.get("last_decision")))
        sys.exit(0)
    try:
        main()
    except Exception as exc:
        log.critical("FATAL: %s", exc, exc_info=True)
        raise
