# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v114.1
SPY 0-DTE | MULTI-LEG ENGINE | PAPER ONLY
=========================================================
CALL | PUT | BULL CALL SPREAD | BULL PUT SPREAD
=========================================================
"""

import os, sys, json, time, uuid, math, re, logging, hashlib
import threading
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# =========================================================
# 1) CONFIGURATION
# =========================================================

VERSION = "114.1"
UNDERLYING = "SPY"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "."))
BASE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = BASE_DIR / "safaa_v1141_state.json"
LEDGER_FILE = BASE_DIR / "safaa_v1141_ledger.json"
INIT_FILE = BASE_DIR / "safaa_v1141_initialized.flag"
LOCK_FILE = BASE_DIR / "safaa_v1141.lock"

API_KEY = os.getenv("APCA_API_KEY_ID", "").strip()
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()

TRADING_URL = os.getenv(
    "APCA_API_BASE_URL", "https://paper-api.alpaca.markets"
).rstrip("/")
DATA_URL = os.getenv(
    "APCA_DATA_URL", "https://data.alpaca.markets"
).rstrip("/")
OPTIONS_DATA_URL = os.getenv(
    "APCA_OPTIONS_DATA_URL", "https://data.alpaca.markets"
).rstrip("/")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

if "paper" not in TRADING_URL.lower():
    raise RuntimeError("v114 تعمل على Paper فقط. عنوان التداول غير مقبول.")

if os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true":
    raise RuntimeError("التداول الحقيقي ممنوع في v114.")

STRATEGY_MODE = os.getenv("STRATEGY_MODE", "AUTO").upper()

VALID_STRATEGIES = {
    "AUTO", "CALL", "PUT", "BULL_CALL_SPREAD", "BULL_PUT_SPREAD"
}

if STRATEGY_MODE not in VALID_STRATEGIES:
    raise RuntimeError(f"STRATEGY_MODE غير صالح: {STRATEGY_MODE}")

# Risk controls
RISK_PER_TRADE = 0.02
MAX_DAILY_DRAWDOWN = 0.03
MAX_PREMIUM_FRACTION = 0.02
MAX_OPEN_POSITIONS = 1
MAX_TRADES_PER_DAY = 4
MAX_CONSECUTIVE_LOSSES = 2
MAX_CONTRACTS = 10

# Option selection (single leg)
TARGET_DELTA_MIN = 0.40
TARGET_DELTA_MAX = 0.65
TARGET_DELTA = 0.52

# Option selection (spread legs: نطاق أوسع لأن رجل البيع أقل دلتا)
SPREAD_DELTA_MIN = 0.15
SPREAD_DELTA_MAX = 0.70
SHORT_LEG_TARGET_DELTA = 0.25

MAX_SPREAD_PERCENT = 0.12
MIN_OPTION_ASK = 0.20
MAX_OPTION_ASK = 8.00
STRIKE_WINDOW = 15.0

# Spread selection
SPREAD_MIN_WIDTH = 1.00
SPREAD_MAX_WIDTH = 5.00
SPREAD_MAX_DEBIT = 3.00
SPREAD_MIN_CREDIT = 0.10

# Position management
STOP_LOSS_PERCENT = 0.30
TAKE_PROFIT_PERCENT = 0.60
TRAIL_ACTIVATION_PERCENT = 0.35
TRAIL_DISTANCE_PERCENT = 0.18

# Trading hours - New York
ENTRY_START = (9, 45)
ENTRY_END = (14, 0)
FORCE_EXIT_TIME = (15, 40)

# Execution
SCAN_SECONDS = 20
REQUEST_TIMEOUT = 15
MAX_API_RETRIES = 3
MAX_ENTRY_SLIPPAGE_PERCENT = 0.08
MAX_OPTION_QUOTE_AGE_SECONDS = 60
RECONCILIATION_INTERVAL = 20
ENTRY_ORDER_TIMEOUT_SECONDS = 90
EXIT_ORDER_TIMEOUT_SECONDS = 45

# Indicators
FAST_EMA = 9
SLOW_EMA = 21
RSI_PERIOD = 14
VOLUME_LOOKBACK = 20
MIN_SIGNAL_SCORE = 3

# Market data
MIN_SPY_BARS = 40
SPY_DATA_RETRIES = 3
SPY_RETRY_DELAY = 2.5
SPY_MAX_BAR_AGE_SECONDS = 180
SPY_LOOKBACK_HOURS = 6

DIAGNOSTICS_ENABLED = True

# =========================================================
# 2) LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BASE_DIR / "safaa_v1141.log", encoding="utf-8"),
    ],
)

log = logging.getLogger("SAFAA_V1141")


def diagnostic(stage, message, *args):
    if DIAGNOSTICS_ENABLED:
        log.info("[DIAGNOSTIC:%s] " + message, stage, *args)


def reject(message, *args):
    log.warning("[REJECT] " + message, *args)


# =========================================================
# 3) EXCEPTIONS
# =========================================================

class APIError(Exception):
    pass


class SafetyHalt(Exception):
    """خطأ خطير يوقف البوت."""


class EntryBlocked(Exception):
    """سبب مؤقت يمنع الدخول فقط، لا يوقف البوت."""


class MarketDataUnavailable(Exception):
    pass


# =========================================================
# 4) HTTP SESSION
# =========================================================

SESSION = requests.Session()
SESSION.headers.update({
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
})


def api_request(method, url, params=None, payload=None):
    method = method.upper()
    attempts = 1 if method == "POST" else MAX_API_RETRIES
    last_error = None

    for attempt in range(attempts):
        try:
            response = SESSION.request(
                method=method, url=url, params=params,
                json=payload, timeout=REQUEST_TIMEOUT,
            )

            if response.status_code >= 400:
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text[:1000]
                raise APIError(
                    f"{method} {url} HTTP {response.status_code}: {detail}"
                )

            return response.json() if response.text else {}

        except APIError:
            raise

        except requests.RequestException as exc:
            last_error = exc
            if method == "POST":
                raise APIError(
                    "نتيجة POST غير مؤكدة؛ لن تتم إعادة إرسال الأمر تلقائياً."
                )
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))

    raise APIError(str(last_error))


def trading_api(method, path, params=None, payload=None):
    return api_request(
        method, f"{TRADING_URL}{path}", params=params, payload=payload
    )


def data_api(method, path, params=None):
    return api_request(method, f"{DATA_URL}{path}", params=params)


def options_data_api(method, path, params=None):
    return api_request(method, f"{OPTIONS_DATA_URL}{path}", params=params)


# =========================================================
# 5) TIME AND GENERAL HELPERS
# =========================================================

def utc_now():
    """يرجع datetime (UTC)."""
    return datetime.now(UTC)


def utc_iso():
    return utc_now().isoformat()


def ny_now():
    return datetime.now(NY)


def today_ny():
    """يرجع date."""
    return ny_now().date()


def today_str():
    return today_ny().isoformat()


def make_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def sha256_text(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def safe_float(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def parse_market_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        return None


def seconds_since(iso_value):
    ts = parse_market_timestamp(iso_value)
    if not ts:
        return 10 ** 9
    return (utc_now() - ts).total_seconds()


def slippage_pad(price):
    return max(0.01, round(abs(price) * MAX_ENTRY_SLIPPAGE_PERCENT, 2))


def atomic_write_json(path, data):
    temp = Path(str(path) + ".tmp")
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)


def read_json(path):
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


# =========================================================
# 6) SINGLE INSTANCE LOCK
# =========================================================

_INSTANCE_LOCK_HANDLE = None


def acquire_instance_lock():
    global _INSTANCE_LOCK_HANDLE

    try:
        import fcntl
    except ImportError:
        raise SafetyHalt("تعذر استخدام قفل النظام.")

    _INSTANCE_LOCK_HANDLE = open(LOCK_FILE, "a+", encoding="utf-8")

    try:
        fcntl.flock(
            _INSTANCE_LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
        )
    except BlockingIOError:
        raise SafetyHalt("هناك نسخة أخرى تعمل بالفعل.")

    _INSTANCE_LOCK_HANDLE.seek(0)
    _INSTANCE_LOCK_HANDLE.truncate()
    _INSTANCE_LOCK_HANDLE.write(str(os.getpid()))
    _INSTANCE_LOCK_HANDLE.flush()


# =========================================================
# 7) STATE AND LEDGER
# =========================================================

def default_state():
    now = utc_iso()
    return {
        "version": VERSION,
        "created_at": now,
        "updated_at": now,
        "halted": False,
        "halt_reason": None,
        "halt_scope": None,
        "trading_day": today_str(),
        "starting_equity": None,
        "daily_pnl": 0.0,
        "trades_today": 0,
        "consecutive_losses": 0,
        "last_candle_id": None,
        "last_reconciliation": None,
        "last_positions": {},
        "telegram_offset": None,
        "position_meta": {},
    }


def default_ledger():
    return {
        "version": VERSION,
        "orders": {},
        "fills": {},
        "closed_trades": [],
        "events": [],
    }


STATE = None
LEDGER = None
STATE_MUTEX = threading.RLock()


def save_state():
    with STATE_MUTEX:
        STATE["updated_at"] = utc_iso()
        atomic_write_json(STATE_FILE, STATE)


def save_ledger():
    with STATE_MUTEX:
        atomic_write_json(LEDGER_FILE, LEDGER)


def ledger_event(event_type, data=None):
    LEDGER.setdefault("events", []).append({
        "id": make_id("EVT"),
        "type": event_type,
        "timestamp": utc_iso(),
        "data": data or {},
    })
    LEDGER["events"] = LEDGER["events"][-10000:]
    save_ledger()


def halt_bot(reason, scope="MANUAL"):
    STATE["halted"] = True
    STATE["halt_reason"] = str(reason)
    STATE["halt_scope"] = scope
    save_state()
    log.critical("TRADING HALTED (%s): %s", scope, reason)


def ensure_not_halted():
    if STATE.get("halted"):
        raise SafetyHalt(STATE.get("halt_reason") or "البوت متوقف.")


# =========================================================
# 8) STORAGE INITIALIZATION
# =========================================================

def initialize_storage():
    global STATE, LEDGER

    if INIT_FILE.exists():
        if not STATE_FILE.exists() or not LEDGER_FILE.exists():
            raise SafetyHalt("ملفات الحالة أو دفتر الصفقات مفقودة.")

        try:
            STATE = read_json(STATE_FILE)
            LEDGER = read_json(LEDGER_FILE)
        except Exception as exc:
            raise SafetyHalt(f"تعذر قراءة ملفات الحالة: {exc}")

        if not isinstance(STATE, dict) or not isinstance(LEDGER, dict):
            raise SafetyHalt("ملفات الحالة غير صالحة.")

        if not isinstance(LEDGER.get("orders"), dict):
            raise SafetyHalt("بنية سجل الأوامر غير صالحة.")

        for key, value in default_state().items():
            STATE.setdefault(key, value)

        for key, value in default_ledger().items():
            LEDGER.setdefault(key, value)

        if STATE.get("version") != VERSION:
            raise SafetyHalt("إصدار ملفات الحالة لا يطابق الإصدار الحالي.")

        save_state()
        log.info("تم تحميل ملفات الحالة السابقة.")
        return

    account = trading_api("GET", "/v2/account")

    if account.get("status") != "ACTIVE":
        raise SafetyHalt(f"الحساب غير نشط: {account.get('status')}")

    STATE = default_state()
    LEDGER = default_ledger()
    STATE["starting_equity"] = safe_float(account.get("equity"))

    if not STATE["starting_equity"]:
        raise SafetyHalt("تعذر تحديد رأس المال.")

    save_state()
    save_ledger()

    with open(INIT_FILE, "w", encoding="utf-8") as file:
        file.write(f"Initialized {utc_iso()} | version={VERSION}\n")
        file.flush()
        os.fsync(file.fileno())

    log.info("تم إنشاء ملفات الحالة | Equity=%.2f", STATE["starting_equity"])


# =========================================================
# 9) ACCOUNT HELPERS
# =========================================================

def get_account():
    return trading_api("GET", "/v2/account")


def get_account_equity():
    equity = safe_float(get_account().get("equity"))
    if equity is None or equity <= 0:
        raise SafetyHalt("قيمة Equity غير صالحة.")
    return equity


def get_clock():
    return trading_api("GET", "/v2/clock")


def market_is_open():
    return bool(get_clock().get("is_open", False))


def any_pending_orders():
    return any(
        m.get("status") in ("PENDING_ENTRY", "PENDING_EXIT")
        for m in STATE.get("position_meta", {}).values()
    )


def refresh_daily_baseline():
    current_day = today_str()

    if STATE.get("trading_day") == current_day:
        return

    if any_pending_orders():
        raise SafetyHalt("تغير يوم التداول مع وجود أمر غير محسوم.")

    if get_option_positions():
        raise SafetyHalt(
            "تغير يوم التداول مع وجود مراكز مفتوحة؛ يلزم التحقق."
        )

    # عقود 0-DTE انتهت؛ نظف أي سجلات قديمة
    if STATE.get("position_meta"):
        ledger_event("STALE_META_CLEARED", dict(STATE["position_meta"]))
        STATE["position_meta"] = {}

    equity = get_account_equity()

    STATE.update({
        "trading_day": current_day,
        "starting_equity": equity,
        "daily_pnl": 0.0,
        "trades_today": 0,
        "consecutive_losses": 0,
    })

    # الإيقاف اليومي يُرفع تلقائياً؛ الإيقاف اليدوي يبقى
    if STATE.get("halted") and STATE.get("halt_scope") == "DAY":
        STATE["halted"] = False
        STATE["halt_reason"] = None
        STATE["halt_scope"] = None
        log.info("تم رفع الإيقاف اليومي تلقائياً.")

    save_state()
    log.info("بداية يوم جديد | Equity=%.2f", equity)


def check_daily_risk():
    ensure_not_halted()

    starting = safe_float(STATE.get("starting_equity"))

    if not starting or starting <= 0:
        raise SafetyHalt("رأس المال المرجعي غير صالح.")

    pnl = get_account_equity() - starting
    STATE["daily_pnl"] = pnl
    save_state()

    if pnl <= -(starting * MAX_DAILY_DRAWDOWN):
        halt_bot(f"تم بلوغ حد الخسارة اليومية: {pnl:.2f} USD", "DAY")
        raise SafetyHalt("تجاوز حد الخسارة اليومية.")

    if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        halt_bot("تم بلوغ حد الخسائر المتتالية.", "DAY")
        raise SafetyHalt("توقف بعد خسائر متتالية.")


# =========================================================
# 10) MARKET DATA
# =========================================================

def get_spy_bars(limit=120):
    feed = os.getenv("ALPACA_STOCK_FEED", "iex").lower().strip()

    if feed not in ("iex", "sip", "boats", "otc"):
        raise MarketDataUnavailable("مصدر بيانات SPY غير صالح.")

    last_error = None

    for attempt in range(1, SPY_DATA_RETRIES + 1):
        try:
            now = datetime.now(UTC)

            params = {
                "timeframe": "1Min",
                "limit": int(limit),
                "adjustment": "raw",
                "feed": feed,
                "sort": "desc",
                "start": (now - timedelta(hours=SPY_LOOKBACK_HOURS)).isoformat(),
                "end": now.isoformat(),
            }

            response = data_api("GET", f"/v2/stocks/{UNDERLYING}/bars", params)
            raw = response.get("bars") or []

            if not isinstance(raw, list) or not raw:
                raise MarketDataUnavailable("لم تصل شموع SPY.")

            bars, seen = [], set()

            for item in raw:
                try:
                    ts = parse_market_timestamp(item.get("t"))
                    if not ts or ts.isoformat() in seen:
                        continue
                    seen.add(ts.isoformat())

                    bar = {
                        "timestamp": ts.isoformat(),
                        "_time": ts,
                        "open": float(item["o"]),
                        "high": float(item["h"]),
                        "low": float(item["l"]),
                        "close": float(item["c"]),
                        "volume": float(item["v"]),
                    }

                    values = [bar["open"], bar["high"], bar["low"],
                              bar["close"], bar["volume"]]

                    if not all(math.isfinite(x) for x in values):
                        continue
                    if min(values[:4]) <= 0:
                        continue
                    if bar["high"] < bar["low"] or bar["volume"] < 0:
                        continue
                    if bar["high"] < max(bar["open"], bar["close"]):
                        continue
                    if bar["low"] > min(bar["open"], bar["close"]):
                        continue

                    bars.append(bar)

                except (TypeError, ValueError, KeyError):
                    continue

            bars.sort(key=lambda x: x["_time"])

            completed = [
                b for b in bars if b["_time"] + timedelta(minutes=1) <= now
            ]

            if len(completed) < MIN_SPY_BARS:
                raise MarketDataUnavailable(
                    f"شموع غير كافية: {len(completed)}/{MIN_SPY_BARS}"
                )

            age = (
                now - (completed[-1]["_time"] + timedelta(minutes=1))
            ).total_seconds()

            if age < -5 or age > SPY_MAX_BAR_AGE_SECONDS:
                raise MarketDataUnavailable(
                    f"عمر بيانات SPY غير صالح: {age:.1f}s"
                )

            for b in completed:
                b.pop("_time", None)

            log.info(
                "SPY DATA OK | Feed=%s | Bars=%s | Last=%s | Age=%.1fs",
                feed, len(completed), completed[-1]["timestamp"], age,
            )

            return completed

        except (APIError, MarketDataUnavailable) as exc:
            last_error = exc
            log.warning("SPY DATA RETRY %s/%s: %s",
                        attempt, SPY_DATA_RETRIES, exc)

        except Exception as exc:
            last_error = exc
            log.exception("خطأ غير متوقع في بيانات SPY.")

        if attempt < SPY_DATA_RETRIES:
            time.sleep(SPY_RETRY_DELAY * attempt)

    raise MarketDataUnavailable(f"تعذر جلب بيانات SPY: {last_error}")


# =========================================================
# 11) TECHNICAL INDICATORS
# =========================================================

def average(values):
    return sum(values) / len(values) if values else None


def ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = value * multiplier + result * (1 - multiplier)
    return result


def calculate_rsi(values, period=RSI_PERIOD):
    if len(values) <= period:
        return None

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = average(gains[:period])
    avg_loss = average(losses[:period])

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    return 100 - (100 / (1 + avg_gain / avg_loss))


def calculate_vwap(bars):
    """VWAP للجلسة العادية الحالية فقط (من 9:30 نيويورك)."""
    today = today_ny()
    pv = volume = 0.0

    for b in bars:
        t = parse_market_timestamp(b["timestamp"]).astimezone(NY)

        if t.date() != today or (t.hour, t.minute) < (9, 30):
            continue

        typical = (b["high"] + b["low"] + b["close"]) / 3
        vol = max(b["volume"], 0)
        pv += typical * vol
        volume += vol

    return pv / volume if volume > 0 else None


def calculate_atr(bars, period=14):
    if len(bars) <= period:
        return None

    ranges = []
    for i in range(1, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        ranges.append(max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        ))

    return average(ranges[-period:])


def calculate_market_indicators(bars):
    if len(bars) < max(SLOW_EMA + 1, RSI_PERIOD + 1, VOLUME_LOOKBACK + 2):
        raise MarketDataUnavailable("عدد الشموع غير كافٍ للمؤشرات.")

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]

    fast = ema(closes, FAST_EMA)
    slow = ema(closes, SLOW_EMA)
    rsi = calculate_rsi(closes)
    vwap = calculate_vwap(bars)
    atr = calculate_atr(bars)

    if any(x is None for x in (fast, slow, rsi, vwap, atr)):
        raise MarketDataUnavailable("تعذر حساب المؤشرات.")

    last, previous = bars[-1], bars[-2]

    avg_volume = average(volumes[-(VOLUME_LOOKBACK + 1):-1])
    volume_ratio = (
        last["volume"] / avg_volume if avg_volume and avg_volume > 0 else 0.0
    )

    return {
        "price": last["close"],
        "previous_close": previous["close"],
        "fast_ema": fast,
        "slow_ema": slow,
        "rsi": rsi,
        "vwap": vwap,
        "atr": atr,
        "volume_ratio": volume_ratio,
        "timestamp": last["timestamp"],
    }


# =========================================================
# 12) MARKET SIGNAL ENGINE
# =========================================================

def get_market_signal(ind):
    price, fast, slow = ind["price"], ind["fast_ema"], ind["slow_ema"]
    rsi, vwap = ind["rsi"], ind["vwap"]
    previous, volume_ratio = ind["previous_close"], ind["volume_ratio"]

    bull = bear = 0

    if fast > slow:
        bull += 1
    elif fast < slow:
        bear += 1

    if price > vwap:
        bull += 1
    elif price < vwap:
        bear += 1

    if price > previous:
        bull += 1
    elif price < previous:
        bear += 1

    if 50 <= rsi <= 68:
        bull += 1
    elif 32 <= rsi < 50:
        bear += 1

    if volume_ratio >= 1.10:
        if price > previous:
            bull += 1
        elif price < previous:
            bear += 1

    if bull >= MIN_SIGNAL_SCORE and bull > bear:
        direction = "CALL"
    elif bear >= MIN_SIGNAL_SCORE and bear > bull:
        direction = "PUT"
    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "score": max(bull, bear),
        "bullish_score": bull,
        "bearish_score": bear,
        "confidence_score": (max(bull, bear) / 5) * 100,
    }


# =========================================================
# 13) TIME AND CANDLE HELPERS
# =========================================================

def make_candle_id(timestamp):
    return sha256_text(f"{UNDERLYING}|{timestamp}")[:24]


def entry_time_allowed():
    now = ny_now()
    if now.weekday() >= 5:
        return False
    current = now.hour * 60 + now.minute
    start = ENTRY_START[0] * 60 + ENTRY_START[1]
    end = ENTRY_END[0] * 60 + ENTRY_END[1]
    return start <= current <= end


def force_exit_time_reached():
    now = ny_now()
    current = now.hour * 60 + now.minute
    return current >= FORCE_EXIT_TIME[0] * 60 + FORCE_EXIT_TIME[1]


# =========================================================
# 14) STRATEGY ROUTER
# =========================================================

def choose_strategy(signal):
    direction = signal.get("direction")

    if direction == "NEUTRAL":
        return None

    if STRATEGY_MODE == "CALL":
        return "CALL" if direction == "CALL" else None

    if STRATEGY_MODE == "PUT":
        return "PUT" if direction == "PUT" else None

    if STRATEGY_MODE == "BULL_CALL_SPREAD":
        return "BULL_CALL_SPREAD" if direction == "CALL" else None

    if STRATEGY_MODE == "BULL_PUT_SPREAD":
        return "BULL_PUT_SPREAD" if direction == "CALL" else None

    # AUTO
    if direction == "PUT":
        return "PUT"
    if direction == "CALL":
        return "BULL_CALL_SPREAD"

    return None


# =========================================================
# 15) OPTIONS CONTRACT DISCOVERY
# =========================================================

OPTION_SYMBOL_RE = re.compile(r"^SPY\d{6}[CP]\d{8}$")


def contract_expiration(contract):
    try:
        return date.fromisoformat(str(contract.get("expiration_date", "")))
    except ValueError:
        return None


def contract_strike(contract):
    try:
        strike = float(contract.get("strike_price"))
        return strike if strike > 0 else None
    except (TypeError, ValueError):
        return None


def contract_type(contract):
    return str(contract.get("type", "")).lower().strip()


def contract_symbol(contract):
    return str(contract.get("symbol", "")).strip()


def valid_spy_contract(contract, option_type):
    if not isinstance(contract, dict):
        return False
    if contract_type(contract) != option_type.lower():
        return False
    if not contract_symbol(contract):
        return False
    if contract.get("underlying_symbol") != UNDERLYING:
        return False
    if contract.get("status") != "active":
        return False
    if contract.get("tradable") is False:
        return False
    if contract_expiration(contract) != today_ny():
        return False
    return contract_strike(contract) is not None


def get_spy_option_contracts(option_type, market_price):
    if option_type not in ("call", "put"):
        raise ValueError("نوع العقد يجب أن يكون call أو put.")

    params = {
        "underlying_symbols": UNDERLYING,
        "status": "active",
        "type": option_type,
        "expiration_date": today_str(),
        "strike_price_gte": f"{market_price - STRIKE_WINDOW:.2f}",
        "strike_price_lte": f"{market_price + STRIKE_WINDOW:.2f}",
        "limit": 1000,
    }

    response = trading_api("GET", "/v2/options/contracts", params)
    contracts = response.get("option_contracts") or []

    if not isinstance(contracts, list):
        raise APIError("بيانات العقود غير صالحة.")

    return [c for c in contracts if valid_spy_contract(c, option_type)]


# =========================================================
# 16) OPTIONS SNAPSHOTS AND QUOTES
# =========================================================

def get_option_snapshots(symbols):
    symbols = list(dict.fromkeys(str(s).strip() for s in symbols if s))

    if not symbols:
        return {}

    result = {}

    for start in range(0, len(symbols), 100):
        batch = symbols[start:start + 100]

        response = options_data_api(
            "GET",
            "/v1beta1/options/snapshots",
            {
                "symbols": ",".join(batch),
                "feed": os.getenv("ALPACA_OPTIONS_FEED", "indicative"),
                "limit": 1000,
            },
        )

        snapshots = response.get("snapshots", {})

        if isinstance(snapshots, dict):
            result.update(snapshots)

    return result


def extract_option_quote(snapshot, enforce_spread=True):
    if not isinstance(snapshot, dict):
        return None

    quote = snapshot.get("latestQuote", {})

    if not isinstance(quote, dict):
        return None

    try:
        bid = float(quote.get("bp", 0))
        ask = float(quote.get("ap", 0))
        bid_size = float(quote.get("bs", 0) or 0)
        ask_size = float(quote.get("as", 0) or 0)

        timestamp = parse_market_timestamp(quote.get("t"))

        if not timestamp:
            return None

        age = (datetime.now(UTC) - timestamp).total_seconds()

        if age < -5 or age > MAX_OPTION_QUOTE_AGE_SECONDS:
            return None

        if not all(math.isfinite(x) for x in (bid, ask, bid_size, ask_size)):
            return None

        if ask <= 0 or bid < 0 or ask < bid:
            return None

        spread = ask - bid
        midpoint = (ask + bid) / 2
        spread_percent = spread / midpoint if midpoint > 0 else 1

        if enforce_spread:
            if bid <= 0 or bid_size <= 0 or ask_size <= 0:
                return None
            if spread_percent > MAX_SPREAD_PERCENT:
                return None

        return {
            "bid": bid,
            "ask": ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "mid": midpoint,
            "spread": spread,
            "spread_percent": spread_percent,
            "timestamp": timestamp.isoformat(),
            "age_seconds": age,
        }

    except (TypeError, ValueError):
        return None


# =========================================================
# 17) OPTION GREEKS
# =========================================================

def extract_option_greeks(snapshot):
    if not isinstance(snapshot, dict):
        return {}

    greeks = snapshot.get("greeks", {})

    if not isinstance(greeks, dict):
        return {}

    return {
        name: safe_float(greeks.get(name))
        for name in ("delta", "gamma", "theta", "vega", "rho")
    }


def delta_matches(snapshot, option_type, dmin, dmax):
    delta = extract_option_greeks(snapshot).get("delta")

    if delta is None:
        return False

    if not dmin <= abs(delta) <= dmax:
        return False

    if option_type == "call" and delta <= 0:
        return False

    if option_type == "put" and delta >= 0:
        return False

    return True


# =========================================================
# 18) OPTION CANDIDATE BUILDER
# =========================================================

def build_option_candidates(option_type, market_price, dmin, dmax):
    contracts = get_spy_option_contracts(option_type, market_price)

    if not contracts:
        log.warning("لا توجد عقود %s متاحة.", option_type.upper())
        return []

    symbols = [contract_symbol(c) for c in contracts]
    snapshots = get_option_snapshots(symbols)

    candidates = []

    for contract in contracts:
        symbol = contract_symbol(contract)
        snapshot = snapshots.get(symbol)
        quote = extract_option_quote(snapshot)

        if not quote:
            continue
        if quote["ask"] < MIN_OPTION_ASK or quote["ask"] > MAX_OPTION_ASK:
            continue
        if not delta_matches(snapshot, option_type, dmin, dmax):
            continue

        candidates.append({
            "symbol": symbol,
            "type": option_type,
            "strike": contract_strike(contract),
            "expiration": contract_expiration(contract).isoformat(),
            "quote": quote,
            "greeks": extract_option_greeks(snapshot),
        })

    candidates.sort(key=lambda x: (
        abs(abs(x["greeks"]["delta"]) - TARGET_DELTA),
        x["quote"]["spread_percent"],
        x["quote"]["age_seconds"],
    ))

    return candidates


# =========================================================
# 19) SINGLE LEG SELECTION
# =========================================================

def select_single_option(option_type, market_price):
    candidates = build_option_candidates(
        option_type, market_price, TARGET_DELTA_MIN, TARGET_DELTA_MAX
    )

    if not candidates:
        reject("لم نجد عقد %s مطابقاً لشروط السعر والدلتا والسيولة.",
               option_type.upper())
        return None

    best = candidates[0]

    log.info(
        "OPTION SELECTED | %s | Strike=%.2f | Bid=%.2f Ask=%.2f Delta=%.3f",
        best["symbol"], best["strike"], best["quote"]["bid"],
        best["quote"]["ask"], best["greeks"]["delta"],
    )

    return best


# =========================================================
# 20) SPREAD PAIR SELECTION
# =========================================================

def spread_width_allowed(width):
    return SPREAD_MIN_WIDTH <= width <= SPREAD_MAX_WIDTH


def build_bull_call_pairs(market_price):
    calls = build_option_candidates(
        "call", market_price, SPREAD_DELTA_MIN, SPREAD_DELTA_MAX
    )

    pairs = []

    for long_call in calls:
        for short_call in calls:
            if long_call["symbol"] == short_call["symbol"]:
                continue

            width = short_call["strike"] - long_call["strike"]

            if width <= 0 or not spread_width_allowed(width):
                continue

            debit = long_call["quote"]["ask"] - short_call["quote"]["bid"]

            if debit <= 0 or debit > SPREAD_MAX_DEBIT or debit >= width:
                continue

            long_delta = abs(long_call["greeks"]["delta"])
            short_delta = abs(short_call["greeks"]["delta"])

            if short_delta >= long_delta:
                continue

            score = (
                abs(long_delta - TARGET_DELTA) * 3
                + abs(short_delta - SHORT_LEG_TARGET_DELTA)
                + long_call["quote"]["spread_percent"]
                + short_call["quote"]["spread_percent"]
            )

            pairs.append({
                "strategy": "BULL_CALL_SPREAD",
                "long_leg": long_call,
                "short_leg": short_call,
                "width": width,
                "entry_price": round(debit, 2),
                "max_loss_per_share": debit,
                "max_gain_per_share": width - debit,
                "breakeven": long_call["strike"] + debit,
                "score": score,
            })

    pairs.sort(key=lambda x: x["score"])
    return pairs


def build_bull_put_pairs(market_price):
    puts = build_option_candidates(
        "put", market_price, SPREAD_DELTA_MIN, SPREAD_DELTA_MAX
    )

    pairs = []

    for short_put in puts:
        for long_put in puts:
            if short_put["symbol"] == long_put["symbol"]:
                continue

            width = short_put["strike"] - long_put["strike"]

            if width <= 0 or not spread_width_allowed(width):
                continue

            credit = short_put["quote"]["bid"] - long_put["quote"]["ask"]

            if credit < SPREAD_MIN_CREDIT or credit >= width:
                continue

            short_delta = abs(short_put["greeks"]["delta"])
            long_delta = abs(long_put["greeks"]["delta"])

            if long_delta >= short_delta:
                continue

            score = (
                abs(short_delta - TARGET_DELTA) * 3
                + abs(long_delta - SHORT_LEG_TARGET_DELTA)
                + short_put["quote"]["spread_percent"]
                + long_put["quote"]["spread_percent"]
            )

            pairs.append({
                "strategy": "BULL_PUT_SPREAD",
                "short_leg": short_put,
                "long_leg": long_put,
                "width": width,
                "entry_price": round(credit, 2),
                "max_gain_per_share": credit,
                "max_loss_per_share": width - credit,
                "breakeven": short_put["strike"] - credit,
                "score": score,
            })

    pairs.sort(key=lambda x: x["score"])
    return pairs


def select_spread(strategy, market_price):
    if strategy == "BULL_CALL_SPREAD":
        pairs = build_bull_call_pairs(market_price)
    elif strategy == "BULL_PUT_SPREAD":
        pairs = build_bull_put_pairs(market_price)
    else:
        raise ValueError(f"استراتيجية سبريد غير مدعومة: {strategy}")

    if not pairs:
        reject("لم نجد زوج عقود صالحاً للاستراتيجية %s.", strategy)
        return None

    best = pairs[0]

    log.info(
        "SPREAD SELECTED | %s | Width=%.2f | Entry=%.2f | "
        "MaxLoss=%.2f | MaxGain=%.2f",
        strategy, best["width"], best["entry_price"],
        best["max_loss_per_share"], best["max_gain_per_share"],
    )

    return best


# =========================================================
# 21) POSITION SIZING AND RISK ENGINE
# =========================================================

def get_available_buying_power():
    account = get_account()

    buying_power = safe_float(
        account.get("options_buying_power"),
        safe_float(account.get("buying_power"), 0.0),
    )

    if buying_power is None or buying_power <= 0:
        raise EntryBlocked("Buying power غير كافٍ.")

    return buying_power


def calculate_contract_quantity(cost_per_share, max_loss_per_share, equity):
    if cost_per_share <= 0 or max_loss_per_share <= 0:
        return 0

    buying_power = get_available_buying_power()

    risk_budget = min(
        equity * RISK_PER_TRADE, equity * MAX_DAILY_DRAWDOWN
    )
    premium_budget = equity * MAX_PREMIUM_FRACTION

    quantity = min(
        math.floor(risk_budget / (max_loss_per_share * 100)),
        math.floor(premium_budget / (cost_per_share * 100)),
        math.floor(buying_power / (cost_per_share * 100)),
        MAX_CONTRACTS,
    )

    return max(0, int(quantity))


def calculate_single_leg_quantity(option, equity):
    price = option["quote"]["ask"]
    price = price + slippage_pad(price)
    return calculate_contract_quantity(price, price, equity)


def calculate_spread_quantity(spread, equity):
    entry = spread["entry_price"]
    max_loss = spread["max_loss_per_share"]

    if spread["strategy"] == "BULL_CALL_SPREAD":
        cost = entry + slippage_pad(entry)
        max_loss = max_loss + slippage_pad(entry)
    elif spread["strategy"] == "BULL_PUT_SPREAD":
        # الكفالة المطلوبة = أقصى خسارة، وليس الائتمان المستلم
        cost = max_loss
    else:
        return 0

    return calculate_contract_quantity(cost, max_loss, equity)


# =========================================================
# 22) ORDER VALIDATION
# =========================================================

def validate_order_quantity(quantity):
    if not isinstance(quantity, int):
        raise ValueError("كمية الأمر يجب أن تكون عدداً صحيحاً.")

    if quantity < 1 or quantity > MAX_CONTRACTS:
        raise SafetyHalt("كمية العقد خارج حدود الأمان.")

    return True


def validate_option_symbol(symbol):
    symbol = str(symbol or "").strip()

    if not OPTION_SYMBOL_RE.match(symbol):
        raise SafetyHalt(f"رمز عقد خيارات غير صالح أو خارج SPY: {symbol}")

    return True


def validate_entry_window():
    if not market_is_open():
        raise EntryBlocked("السوق مغلق.")

    if not entry_time_allowed():
        raise EntryBlocked("خارج وقت السماح بالدخول.")

    if force_exit_time_reached():
        raise EntryBlocked("وصل وقت إيقاف الدخول.")

    return True


def validate_entry_conditions():
    ensure_not_halted()
    validate_entry_window()
    check_daily_risk()

    if len(STATE.get("position_meta", {})) >= MAX_OPEN_POSITIONS:
        raise EntryBlocked("يوجد مركز أو أمر قائم (الحد الأقصى للمراكز).")

    if STATE.get("trades_today", 0) >= MAX_TRADES_PER_DAY:
        raise EntryBlocked("تم بلوغ الحد اليومي للصفقات.")

    if STATE.get("consecutive_losses", 0) >= MAX_CONSECUTIVE_LOSSES:
        raise EntryBlocked("تم بلوغ حد الخسائر المتتالية.")

    return True


# =========================================================
# 23) ORDER LEDGER
# =========================================================

def register_order(order_data, strategy, symbols, legs=None):
    order_id = order_data.get("id")

    if not order_id:
        raise APIError("استجابة الوسيط لا تحتوي order ID.")

    LEDGER.setdefault("orders", {})[order_id] = {
        "id": order_id,
        "client_order_id": order_data.get("client_order_id"),
        "status": order_data.get("status"),
        "created_at": utc_iso(),
        "updated_at": utc_iso(),
        "strategy": strategy,
        "order_class": order_data.get("order_class", "simple"),
        "symbols": symbols,
        "legs": legs or [],
        "requested_qty": int(safe_float(order_data.get("qty"), 0) or 0),
        "filled_qty": 0,
        "average_fill_price": None,
    }

    save_ledger()
    return order_id


def update_order_record(order_id, order):
    record = LEDGER.setdefault("orders", {}).get(order_id)

    if not record:
        return

    record["status"] = order.get("status")
    record["filled_qty"] = int(safe_float(order.get("filled_qty"), 0) or 0)
    record["average_fill_price"] = safe_float(order.get("filled_avg_price"))
    record["updated_at"] = utc_iso()

    save_ledger()


# =========================================================
# 24) ORDER SUBMISSION
# =========================================================

def get_order_by_client_id(client_order_id):
    return trading_api(
        "GET", "/v2/orders:by_client_order_id",
        {"client_order_id": client_order_id},
    )


def submit_order_payload(payload):
    validate_order_quantity(int(payload.get("qty", 0)))

    client_order_id = (
        f"SAFAA1141-{today_ny().strftime('%Y%m%d')}-{uuid.uuid4().hex[:12]}"
    )
    payload["client_order_id"] = client_order_id

    try:
        response = trading_api("POST", "/v2/orders", payload=payload)

    except APIError as post_error:
        # قد يكون الأمر وصل رغم فشل الاتصال: نبحث عنه بدل تجاهله
        try:
            response = get_order_by_client_id(client_order_id)
            log.warning("تم العثور على الأمر بعد فشل POST: %s",
                        response.get("id"))
        except APIError:
            raise post_error

    if not isinstance(response, dict) or not response.get("id"):
        raise APIError("لم يؤكد الوسيط إنشاء الأمر.")

    log.info("ORDER SUBMITTED | ID=%s | Status=%s",
             response["id"], response.get("status"))

    return response


# =========================================================
# 25) SINGLE LEG ORDER
# =========================================================

def submit_single_leg(option, direction, quantity):
    validate_entry_conditions()
    validate_order_quantity(quantity)

    if direction not in ("CALL", "PUT"):
        raise ValueError("اتجاه الصفقة غير صالح.")

    validate_option_symbol(option["symbol"])

    if option["type"] != direction.lower():
        raise SafetyHalt("نوع العقد لا يتوافق مع اتجاه الصفقة.")

    quote = option["quote"]

    if quote["age_seconds"] > MAX_OPTION_QUOTE_AGE_SECONDS:
        raise EntryBlocked("سعر العقد قديم.")

    limit_price = round(quote["ask"] + slippage_pad(quote["ask"]), 2)

    if limit_price > MAX_OPTION_ASK:
        raise EntryBlocked("سعر الدخول تجاوز الحد الأعلى.")

    payload = {
        "symbol": option["symbol"],
        "qty": str(quantity),
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{limit_price:.2f}",
        "position_intent": "buy_to_open",
    }

    response = submit_order_payload(payload)
    order_id = register_order(response, direction, [option["symbol"]])

    STATE["position_meta"][order_id] = {
        "kind": "SINGLE",
        "strategy": direction,
        "status": "PENDING_ENTRY",
        "symbol": option["symbol"],
        "quantity": quantity,
        "entry_limit": limit_price,
        "entry_filled": 0,
        "entry_average_price": None,
        "exit_order_id": None,
        "exit_attempts": 0,
        "realized_pnl": 0.0,
        "created_at": utc_iso(),
    }

    save_state()
    ledger_event("ENTRY_SUBMITTED", {"order_id": order_id, "strategy": direction})

    return response


# =========================================================
# 26) MULTI-LEG ORDER BUILDER
# =========================================================

def build_mleg_payload(strategy, long_leg, short_leg, quantity,
                       net_debit_limit, closing=False):
    """
    net_debit_limit بإشارة Alpaca: موجب = debit (دفع)، سالب = credit (استلام).
    الدخول: شراء الطويلة + بيع القصيرة (في Bull Call و Bull Put).
    الإغلاق: بيع الطويلة + شراء القصيرة.
    """
    validate_order_quantity(quantity)
    validate_option_symbol(long_leg["symbol"])
    validate_option_symbol(short_leg["symbol"])

    if long_leg["symbol"] == short_leg["symbol"]:
        raise SafetyHalt("لا يمكن استخدام العقد نفسه للرجلين.")

    if long_leg["expiration"] != short_leg["expiration"]:
        raise SafetyHalt("تاريخ انتهاء الرجلين غير متطابق.")

    if strategy == "BULL_CALL_SPREAD":
        expected_type = "call"
    elif strategy == "BULL_PUT_SPREAD":
        expected_type = "put"
    else:
        raise ValueError(f"استراتيجية MLeg غير مدعومة: {strategy}")

    if long_leg["type"] != expected_type or short_leg["type"] != expected_type:
        raise SafetyHalt("نوع عقود السبريد غير صحيح.")

    if long_leg["strike"] >= short_leg["strike"]:
        raise SafetyHalt("ترتيب strikes غير صحيح (الطويلة يجب أن تكون الأدنى).")

    if closing:
        legs = [
            {"symbol": long_leg["symbol"], "ratio_qty": "1",
             "side": "sell", "position_intent": "sell_to_close"},
            {"symbol": short_leg["symbol"], "ratio_qty": "1",
             "side": "buy", "position_intent": "buy_to_close"},
        ]
    else:
        legs = [
            {"symbol": long_leg["symbol"], "ratio_qty": "1",
             "side": "buy", "position_intent": "buy_to_open"},
            {"symbol": short_leg["symbol"], "ratio_qty": "1",
             "side": "sell", "position_intent": "sell_to_open"},
        ]

    price = round(float(net_debit_limit), 2)

    if not math.isfinite(price):
        raise SafetyHalt("سعر السبريد غير صالح.")

    if price == 0:
        price = 0.01

    return {
        "order_class": "mleg",
        "qty": str(quantity),
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{price:.2f}",
        "legs": legs,
    }


# =========================================================
# 27) SUBMIT SPREAD ENTRY
# =========================================================

def submit_spread_entry(spread, quantity):
    validate_entry_conditions()
    validate_order_quantity(quantity)

    strategy = spread["strategy"]
    long_leg, short_leg = spread["long_leg"], spread["short_leg"]

    # صافي الدفع: شراء الطويلة - بيع القصيرة
    net_debit = long_leg["quote"]["ask"] - short_leg["quote"]["bid"]
    limit_price = round(net_debit + slippage_pad(net_debit), 2)

    if strategy == "BULL_CALL_SPREAD":
        if limit_price <= 0 or limit_price >= spread["width"]:
            raise EntryBlocked("Debit الـ Bull Call غير صالح.")
    elif strategy == "BULL_PUT_SPREAD":
        if -limit_price < SPREAD_MIN_CREDIT:
            raise EntryBlocked("Credit الـ Bull Put غير كافٍ.")
    else:
        raise ValueError("استراتيجية السبريد غير صالحة.")

    payload = build_mleg_payload(
        strategy, long_leg, short_leg, quantity, limit_price, closing=False
    )

    response = submit_order_payload(payload)

    symbols = [long_leg["symbol"], short_leg["symbol"]]
    order_id = register_order(response, strategy, symbols, payload["legs"])

    STATE["position_meta"][order_id] = {
        "kind": "SPREAD",
        "strategy": strategy,
        "status": "PENDING_ENTRY",
        "option_type": long_leg["type"],
        "long_symbol": long_leg["symbol"],
        "short_symbol": short_leg["symbol"],
        "long_strike": long_leg["strike"],
        "short_strike": short_leg["strike"],
        "expiration": long_leg["expiration"],
        "quantity": quantity,
        "entry_limit": abs(limit_price),
        "entry_filled": 0,
        "entry_average_price": None,
        "exit_order_id": None,
        "exit_attempts": 0,
        "realized_pnl": 0.0,
        "created_at": utc_iso(),
    }

    save_state()
    ledger_event("ENTRY_SUBMITTED", {"order_id": order_id, "strategy": strategy})

    log.info("SPREAD SUBMITTED | %s | Qty=%s | Limit=%s | ID=%s",
             strategy, quantity, payload["limit_price"], order_id)

    return response


# =========================================================
# 28) ENTRY DISPATCHER
# =========================================================

def execute_selected_strategy(strategy, market_price, equity):
    if strategy in ("CALL", "PUT"):
        option = select_single_option(strategy.lower(), market_price)

        if not option:
            return None

        quantity = calculate_single_leg_quantity(option, equity)

        if quantity < 1:
            reject("رأس المال لا يسمح بعقد واحد ضمن حدود المخاطرة.")
            return None

        return submit_single_leg(option, strategy, quantity)

    if strategy in ("BULL_CALL_SPREAD", "BULL_PUT_SPREAD"):
        spread = select_spread(strategy, market_price)

        if not spread:
            return None

        quantity = calculate_spread_quantity(spread, equity)

        if quantity < 1:
            reject("رأس المال لا يسمح بسبريد واحد ضمن المخاطرة.")
            return None

        return submit_spread_entry(spread, quantity)

    return None


# =========================================================
# 29) BROKER ORDER MANAGEMENT
# =========================================================

FINAL_ORDER_STATUSES = {
    "filled", "canceled", "expired", "rejected", "done_for_day"
}


def get_order(order_id):
    if not order_id:
        return None
    return trading_api("GET", f"/v2/orders/{order_id}")


def cancel_order(order_id):
    if not order_id:
        return False

    try:
        trading_api("DELETE", f"/v2/orders/{order_id}")
        log.warning("CANCEL REQUESTED | Order=%s", order_id)
        return True

    except APIError as exc:
        log.error("CANCEL FAILED | Order=%s | %s", order_id, exc)
        return False


# =========================================================
# 30) BROKER OPTION POSITIONS
# =========================================================

def get_option_positions():
    response = trading_api("GET", "/v2/positions")

    if not isinstance(response, list):
        raise APIError("تعذر قراءة مراكز الحساب.")

    result = []

    for position in response:
        if not isinstance(position, dict):
            continue

        if str(position.get("asset_class", "")).lower() != "us_option":
            continue

        symbol = str(position.get("symbol", "")).strip()

        if not OPTION_SYMBOL_RE.match(symbol):
            continue

        try:
            quantity = abs(float(position.get("qty", 0)))

            if quantity <= 0:
                continue

            side = str(position.get("side", "")).lower()

            result.append({
                "symbol": symbol,
                "qty": quantity,
                "signed_qty": quantity if side == "long" else -quantity,
                "side": side,
                "avg_entry_price": safe_float(position.get("avg_entry_price")),
                "market_value": safe_float(position.get("market_value")),
                "unrealized_pl": safe_float(position.get("unrealized_pl")),
                "current_price": safe_float(position.get("current_price")),
            })

        except (TypeError, ValueError):
            log.warning("POSITION PARSE ERROR | %s", symbol)

    return result


# =========================================================
# 31) ORDER LIFECYCLE (ENTRY / EXIT FILLS)
# =========================================================

def trade_pnl_per_share(meta, exit_avg):
    entry = safe_float(meta.get("entry_average_price"), 0.0)

    if meta.get("strategy") == "BULL_PUT_SPREAD":
        return entry - exit_avg  # استلمنا credit ثم دفعنا للإغلاق

    return exit_avg - entry


def handle_entry_order(order_id, meta):
    order = get_order(order_id)

    if not order:
        return

    update_order_record(order_id, order)

    status = str(order.get("status", "")).lower()
    filled = int(safe_float(order.get("filled_qty"), 0) or 0)

    if status in FINAL_ORDER_STATUSES:
        if filled > 0:
            avg = abs(safe_float(order.get("filled_avg_price"),
                                 meta.get("entry_limit", 0.0)))

            meta.update({
                "status": "OPEN",
                "entry_filled": filled,
                "entry_average_price": avg,
                "opened_at": utc_iso(),
            })

            STATE["trades_today"] = int(STATE.get("trades_today", 0)) + 1
            save_state()

            ledger_event("ENTRY_FILLED", {
                "order_id": order_id, "filled": filled, "avg": avg
            })

            telegram_send(
                "✅ تم تنفيذ الدخول\n"
                f"الاستراتيجية: {meta['strategy']}\n"
                f"الكمية: {filled} | السعر: {avg:.2f}\n"
                "الحساب: PAPER"
            )

        else:
            STATE["position_meta"].pop(order_id, None)
            save_state()
            ledger_event("ENTRY_NOT_FILLED", {
                "order_id": order_id, "status": status
            })
            log.info("ENTRY NOT FILLED | %s | %s", order_id, status)

        return

    # الأمر ما زال مفتوحاً: ألغه عند انتهاء المهلة أو وقت الإغلاق
    if (
        seconds_since(meta.get("created_at")) > ENTRY_ORDER_TIMEOUT_SECONDS
        or force_exit_time_reached()
    ):
        cancel_order(order_id)


def handle_exit_order(order_id, meta):
    exit_id = meta.get("exit_order_id")

    if not exit_id:
        meta["status"] = "OPEN"
        save_state()
        return

    order = get_order(exit_id)

    if not order:
        return

    update_order_record(exit_id, order)

    status = str(order.get("status", "")).lower()
    filled = int(safe_float(order.get("filled_qty"), 0) or 0)

    if status in FINAL_ORDER_STATUSES:
        if filled > 0:
            exit_avg = abs(safe_float(order.get("filled_avg_price"), 0.0))
            pnl = trade_pnl_per_share(meta, exit_avg) * filled * 100
            meta["realized_pnl"] = meta.get("realized_pnl", 0.0) + pnl

        remaining = int(meta.get("entry_filled", 0)) - filled

        if remaining <= 0:
            total = meta.get("realized_pnl", 0.0)

            if total < 0:
                STATE["consecutive_losses"] = (
                    int(STATE.get("consecutive_losses", 0)) + 1
                )
            else:
                STATE["consecutive_losses"] = 0

            LEDGER.setdefault("closed_trades", []).append({
                "entry_order_id": order_id,
                "strategy": meta.get("strategy"),
                "quantity": meta.get("quantity"),
                "pnl": total,
                "closed_at": utc_iso(),
            })

            STATE["position_meta"].pop(order_id, None)
            save_state()
            save_ledger()

            ledger_event("TRADE_CLOSED", {"order_id": order_id, "pnl": total})

            telegram_send(
                "🔔 تم إغلاق الصفقة\n"
                f"الاستراتيجية: {meta.get('strategy')}\n"
                f"النتيجة: {format_money(total)}\n"
                "الحساب: PAPER"
            )

        else:
            # إغلاق جزئي أو لم يُنفذ: أعد المحاولة بسعر أكثر عدوانية
            meta["entry_filled"] = remaining if filled > 0 else meta["entry_filled"]
            meta["exit_order_id"] = None
            meta["status"] = "OPEN"
            meta["exit_attempts"] = int(meta.get("exit_attempts", 0)) + 1
            save_state()
            log.warning("EXIT INCOMPLETE | %s | remaining=%s", order_id, remaining)

        return

    if seconds_since(meta.get("exit_submitted_at")) > EXIT_ORDER_TIMEOUT_SECONDS:
        cancel_order(exit_id)


def process_orders():
    for order_id, meta in list(STATE.get("position_meta", {}).items()):
        try:
            status = meta.get("status")

            if status == "PENDING_ENTRY":
                handle_entry_order(order_id, meta)
            elif status == "PENDING_EXIT":
                handle_exit_order(order_id, meta)

        except APIError as exc:
            log.error("ORDER PROCESSING API ERROR | %s | %s", order_id, exc)
        except SafetyHalt:
            raise
        except Exception:
            log.exception("ORDER PROCESSING ERROR | %s", order_id)

    save_state()


# =========================================================
# 32) POSITION RECONCILIATION
# =========================================================

def expected_positions_from_state():
    expected = {}

    for meta in STATE.get("position_meta", {}).values():
        if meta.get("status") != "OPEN":
            continue

        quantity = int(meta.get("entry_filled", 0))

        if quantity <= 0:
            continue

        if meta.get("kind") == "SINGLE":
            symbol = meta.get("symbol")
            expected[symbol] = expected.get(symbol, 0) + quantity

        elif meta.get("kind") == "SPREAD":
            long_symbol = meta.get("long_symbol")
            short_symbol = meta.get("short_symbol")
            expected[long_symbol] = expected.get(long_symbol, 0) + quantity
            expected[short_symbol] = expected.get(short_symbol, 0) - quantity

    return expected


def reconcile_broker_positions():
    # أثناء وجود أوامر معلقة قد تكون المراكز في منتصف التنفيذ
    if any_pending_orders():
        return True

    positions = get_option_positions()

    actual = {p["symbol"]: int(round(p["signed_qty"])) for p in positions}
    expected = expected_positions_from_state()

    mismatches = []

    for symbol in sorted(set(actual) | set(expected)):
        exp_qty = expected.get(symbol, 0)
        act_qty = actual.get(symbol, 0)

        if exp_qty != act_qty:
            mismatches.append({
                "symbol": symbol, "expected": exp_qty, "actual": act_qty
            })

    STATE["last_positions"] = actual
    STATE["last_reconciliation"] = utc_iso()
    save_state()

    if mismatches:
        for item in mismatches:
            log.critical(
                "POSITION MISMATCH | %s | Expected=%s | Actual=%s",
                item["symbol"], item["expected"], item["actual"],
            )

        halt_bot("اختلاف بين المراكز الفعلية والسجل المحلي.")
        telegram_send("⛔ اختلاف في المراكز، تم الإيقاف الاحترازي.")
        return False

    log.info("POSITION RECONCILIATION OK | Positions=%s", len(actual))
    return True


def periodic_reconciliation():
    previous = parse_market_timestamp(STATE.get("last_reconciliation"))

    if previous:
        elapsed = (utc_now() - previous).total_seconds()

        if elapsed < RECONCILIATION_INTERVAL:
            return True

    return reconcile_broker_positions()


# =========================================================
# 33) QUOTES AND RETURNS FOR OPEN POSITIONS
# =========================================================

def get_fresh_quote(symbol):
    validate_option_symbol(symbol)
    snapshot = get_option_snapshots([symbol]).get(symbol)

    if not snapshot:
        return None

    # للخروج لا نفرض حد السبريد حتى لا نعجز عن الإغلاق
    return extract_option_quote(snapshot, enforce_spread=False)


def calculate_single_position_return(meta, quote):
    entry = safe_float(
        meta.get("entry_average_price"), meta.get("entry_limit", 0.0)
    )

    if not entry or entry <= 0 or not quote:
        return None

    return (quote["bid"] - entry) / entry


def calculate_spread_return(meta, snapshots):
    strategy = meta.get("strategy")

    long_quote = extract_option_quote(
        snapshots.get(meta.get("long_symbol")), enforce_spread=False
    )
    short_quote = extract_option_quote(
        snapshots.get(meta.get("short_symbol")), enforce_spread=False
    )

    if not long_quote or not short_quote:
        return None

    entry = safe_float(
        meta.get("entry_average_price"), meta.get("entry_limit", 0.0)
    )

    if not entry or entry <= 0:
        return None

    if strategy == "BULL_CALL_SPREAD":
        current_value = long_quote["bid"] - short_quote["ask"]
        pnl_per_share = current_value - entry

    elif strategy == "BULL_PUT_SPREAD":
        current_value = short_quote["ask"] - long_quote["bid"]  # تكلفة الإغلاق
        pnl_per_share = entry - current_value

    else:
        return None

    return {
        "long_quote": long_quote,
        "short_quote": short_quote,
        "pnl_per_share": pnl_per_share,
        "return_percent": pnl_per_share / entry,
        "current_value": current_value,
    }


# =========================================================
# 34) EXIT ORDERS
# =========================================================

def submit_single_leg_exit(order_id, meta, quantity, quote):
    if meta.get("exit_order_id"):
        return None

    symbol = meta["symbol"]
    validate_option_symbol(symbol)
    validate_order_quantity(quantity)

    pad = slippage_pad(quote["bid"]) * (1 + int(meta.get("exit_attempts", 0)))
    limit_price = max(0.01, round(quote["bid"] - pad, 2))

    payload = {
        "symbol": symbol,
        "qty": str(quantity),
        "side": "sell",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{limit_price:.2f}",
        "position_intent": "sell_to_close",
    }

    response = submit_order_payload(payload)
    exit_id = register_order(response, f"EXIT_{meta['strategy']}", [symbol])

    meta["exit_order_id"] = exit_id
    meta["exit_submitted_at"] = utc_iso()
    meta["status"] = "PENDING_EXIT"
    save_state()

    ledger_event("EXIT_SUBMITTED", {"entry_order_id": order_id, "exit_order_id": exit_id})
    log.warning("SINGLE EXIT SUBMITTED | %s | Qty=%s | ID=%s",
                symbol, quantity, exit_id)

    return response


def submit_spread_exit(order_id, meta, quantity, snapshots):
    if meta.get("exit_order_id"):
        return None

    strategy = meta["strategy"]

    long_quote = extract_option_quote(
        snapshots.get(meta["long_symbol"]), enforce_spread=False
    )
    short_quote = extract_option_quote(
        snapshots.get(meta["short_symbol"]), enforce_spread=False
    )

    if not long_quote or not short_quote:
        reject("تعذر الحصول على أسعار إغلاق السبريد.")
        return None

    long_leg = {
        "symbol": meta["long_symbol"], "type": meta["option_type"],
        "strike": meta["long_strike"], "expiration": meta["expiration"],
    }
    short_leg = {
        "symbol": meta["short_symbol"], "type": meta["option_type"],
        "strike": meta["short_strike"], "expiration": meta["expiration"],
    }

    # الإغلاق: بيع الطويلة (bid) وشراء القصيرة (ask)
    # الناتج موجب = دفع، سالب = استلام
    net_debit = short_quote["ask"] - long_quote["bid"]
    pad = slippage_pad(net_debit) * (1 + int(meta.get("exit_attempts", 0)))
    limit_price = round(net_debit + pad, 2)

    payload = build_mleg_payload(
        strategy, long_leg, short_leg, quantity, limit_price, closing=True
    )

    response = submit_order_payload(payload)

    exit_id = register_order(
        response, f"EXIT_{strategy}",
        [meta["long_symbol"], meta["short_symbol"]], payload["legs"],
    )

    meta["exit_order_id"] = exit_id
    meta["exit_submitted_at"] = utc_iso()
    meta["status"] = "PENDING_EXIT"
    save_state()

    ledger_event("EXIT_SUBMITTED", {"entry_order_id": order_id, "exit_order_id": exit_id})
    log.warning("SPREAD EXIT SUBMITTED | %s | Qty=%s | Price=%s | ID=%s",
                strategy, quantity, payload["limit_price"], exit_id)

    return response


# =========================================================
# 35) POSITION RISK MANAGEMENT
# =========================================================

def should_exit_position(return_percent, meta):
    if return_percent is None:
        if force_exit_time_reached():
            return True, "TIME_EXIT"
        return False, "NO_VALID_MARK"

    peak = max(safe_float(meta.get("peak_return"), return_percent),
               return_percent)
    meta["peak_return"] = peak

    if return_percent <= -STOP_LOSS_PERCENT:
        return True, "STOP_LOSS"

    if return_percent >= TAKE_PROFIT_PERCENT:
        return True, "TAKE_PROFIT"

    if peak >= TRAIL_ACTIVATION_PERCENT:
        if peak - return_percent >= TRAIL_DISTANCE_PERCENT:
            return True, "TRAILING_STOP"

    if force_exit_time_reached():
        return True, "TIME_EXIT"

    return False, "HOLD"


def alert_no_quote(meta):
    if force_exit_time_reached() and not meta.get("no_quote_alerted"):
        meta["no_quote_alerted"] = True
        save_state()
        telegram_send("⚠️ لا يوجد سعر حديث لإغلاق مركز وقت الإغلاق. راجع الحساب يدوياً.")


def manage_single_position(order_id, meta):
    quantity = int(meta.get("entry_filled", 0))

    if quantity <= 0:
        return

    quote = get_fresh_quote(meta["symbol"])

    if not quote:
        log.warning("SINGLE POSITION QUOTE UNAVAILABLE | %s", meta["symbol"])
        alert_no_quote(meta)
        return

    result = calculate_single_position_return(meta, quote)
    should_exit, reason = should_exit_position(result, meta)

    log.info("SINGLE POSITION | %s | Return=%s | Decision=%s",
             meta["symbol"],
             f"{result * 100:.2f}%" if result is not None else "N/A",
             reason)

    save_state()

    if should_exit:
        log.warning("EXIT TRIGGERED | %s | %s", meta["symbol"], reason)
        submit_single_leg_exit(order_id, meta, quantity, quote)


def manage_spread_position(order_id, meta):
    quantity = int(meta.get("entry_filled", 0))

    if quantity <= 0:
        return

    snapshots = get_option_snapshots([meta["long_symbol"], meta["short_symbol"]])
    result = calculate_spread_return(meta, snapshots)

    if not result:
        log.warning("SPREAD MARK UNAVAILABLE | %s", order_id)
        alert_no_quote(meta)
        if not force_exit_time_reached():
            return

    return_percent = result["return_percent"] if result else None
    should_exit, reason = should_exit_position(return_percent, meta)

    if result:
        log.info(
            "SPREAD POSITION | %s | Return=%.2f%% | PnL/share=%.3f | Decision=%s",
            meta["strategy"], result["return_percent"] * 100,
            result["pnl_per_share"], reason,
        )

    save_state()

    if should_exit and result:
        log.warning("SPREAD EXIT TRIGGERED | %s", reason)
        submit_spread_exit(order_id, meta, quantity, snapshots)


def manage_positions():
    for order_id, meta in list(STATE.get("position_meta", {}).items()):
        if meta.get("status") != "OPEN":
            continue

        try:
            if meta.get("kind") == "SINGLE":
                manage_single_position(order_id, meta)
            elif meta.get("kind") == "SPREAD":
                manage_spread_position(order_id, meta)

        except SafetyHalt as exc:
            halt_bot(str(exc))
            return
        except APIError as exc:
            log.error("POSITION MANAGEMENT API ERROR | %s | %s", order_id, exc)
        except Exception:
            log.exception("POSITION MANAGEMENT ERROR | %s", order_id)


# =========================================================
# 36) TELEGRAM
# =========================================================

def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not configured.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": str(message),
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()

        if not result.get("ok"):
            log.error("Telegram rejected message: %s", result)
            return False

        return True

    except Exception:
        log.exception("Telegram delivery failed.")
        return False


def format_money(value):
    if value is None:
        return "غير متاح"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "غير متاح"


def status_message():
    try:
        account = get_account()
        positions = get_option_positions()
        halted = STATE.get("halted", False)

        lines = [
            f"💎 صَفاء التحليل v{VERSION}",
            "SPY 0-DTE | PAPER ONLY",
            "",
            f"الحالة: {'متوقف احترازياً' if halted else 'يعمل'}",
        ]

        if halted:
            lines.append(f"السبب: {STATE.get('halt_reason')}")

        lines += [
            f"Equity: {format_money(safe_float(account.get('equity')))}",
            f"Buying Power: {format_money(safe_float(account.get('buying_power')))}",
            f"صافي اليوم: {format_money(STATE.get('daily_pnl', 0))}",
            f"صفقات اليوم: {STATE.get('trades_today', 0)}",
            f"الخسائر المتتالية: {STATE.get('consecutive_losses', 0)}",
            f"مراكز الخيارات: {len(positions)}",
            f"صفقات قائمة: {len(STATE.get('position_meta', {}))}",
            "",
            "تحليل احتمالي — لا يوجد ضمان للربح.",
        ]

        return "\n".join(lines)

    except Exception as exc:
        log.exception("Status message error.")
        return f"💎 صَفاء التحليل v{VERSION}\nتعذر استخراج الحالة: {exc}"


def telegram_get_updates(offset=None):
    if not TELEGRAM_BOT_TOKEN:
        return []

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 1}

    if offset is not None:
        params["offset"] = offset

    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return data.get("result", []) if data.get("ok") else []

    except Exception:
        log.exception("Telegram updates error.")
        return []


def handle_telegram_command(text, chat_id):
    if str(chat_id) != TELEGRAM_CHAT_ID:
        log.warning("Rejected unauthorized Telegram chat: %s", chat_id)
        return

    command = str(text or "").strip().lower().split("@")[0]

    if command in ("/start", "/help"):
        telegram_send(
            "أوامر صَفاء التحليل:\n"
            "/status - حالة الحساب والبوت\n"
            "/risk - حدود المخاطر\n"
            "/halt - إيقاف احترازي (لا دخول جديد)\n"
            "/resume - رفع الإيقاف\n"
            "/help - المساعدة"
        )

    elif command == "/status":
        telegram_send(status_message())

    elif command == "/risk":
        telegram_send(
            "حدود المخاطر الحالية:\n"
            f"Risk/Trade: {RISK_PER_TRADE * 100:.1f}%\n"
            f"Daily Drawdown: {MAX_DAILY_DRAWDOWN * 100:.1f}%\n"
            f"Premium Limit: {MAX_PREMIUM_FRACTION * 100:.1f}%\n"
            f"Max Trades: {MAX_TRADES_PER_DAY}\n"
            f"Max Positions: {MAX_OPEN_POSITIONS}\n"
            f"Strategy: {STRATEGY_MODE}\n"
            "الوضع: PAPER ONLY"
        )

    elif command == "/halt":
        halt_bot("تم طلب الإيقاف من Telegram.")
        telegram_send("⛔ تم تفعيل الإيقاف الاحترازي.\nلن يفتح البوت صفقات جديدة.")

    elif command == "/resume":
        STATE["halted"] = False
        STATE["halt_reason"] = None
        STATE["halt_scope"] = None
        save_state()
        telegram_send("▶️ تم رفع الإيقاف. ستُعاد المصالحة والفحوص تلقائياً.")


def poll_telegram_commands():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    updates = telegram_get_updates(STATE.get("telegram_offset"))

    for update in updates:
        try:
            STATE["telegram_offset"] = int(update.get("update_id", 0)) + 1

            message = update.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            text = message.get("text", "")

            if text:
                handle_telegram_command(text, chat_id)

        except Exception:
            log.exception("Telegram command processing error.")

    save_state()


# =========================================================
# 37) ENTRY SIGNAL EVALUATION
# =========================================================

def evaluate_entry_signal():
    if STATE.get("halted"):
        return None

    if not entry_time_allowed() or force_exit_time_reached():
        return None

    if STATE.get("position_meta"):
        return None

    if STATE.get("trades_today", 0) >= MAX_TRADES_PER_DAY:
        return None

    bars = get_spy_bars()
    indicators = calculate_market_indicators(bars)
    candle_id = make_candle_id(indicators["timestamp"])

    if candle_id == STATE.get("last_candle_id"):
        return None

    STATE["last_candle_id"] = candle_id
    save_state()

    signal = get_market_signal(indicators)
    strategy = choose_strategy(signal)

    log.info(
        "SPY %.2f | Signal=%s | Score=%s | RSI=%.1f | "
        "EMA9=%.2f | EMA21=%.2f | VWAP=%.2f",
        indicators["price"], signal["direction"], signal["score"],
        indicators["rsi"], indicators["fast_ema"],
        indicators["slow_ema"], indicators["vwap"],
    )

    if not strategy:
        log.info("No qualifying strategy.")
        return None

    if signal["score"] < MIN_SIGNAL_SCORE:
        reject("درجة الإشارة أقل من الحد المطلوب.")
        return None

    return {"strategy": strategy, "signal": signal, "indicators": indicators}


# =========================================================
# 38) ENTRY EXECUTION
# =========================================================

def run_entry_engine():
    candidate = evaluate_entry_signal()

    if not candidate:
        return None

    validate_entry_conditions()

    equity = get_account_equity()
    strategy = candidate["strategy"]
    indicators = candidate["indicators"]

    log.info("ENTRY CANDIDATE | Strategy=%s | Price=%.2f | Score=%s",
             strategy, indicators["price"], candidate["signal"]["score"])

    result = execute_selected_strategy(strategy, indicators["price"], equity)

    if result:
        telegram_send(
            "🚨 أمر دخول جديد\n"
            f"الاستراتيجية: {strategy}\n"
            f"SPY: {indicators['price']:.2f}\n"
            f"النتيجة: {candidate['signal']['score']}\n"
            f"Order ID: {result.get('id')}\n"
            "الحساب: PAPER"
        )

    return result


# =========================================================
# 39) STARTUP SAFETY CHECKS
# =========================================================

def validate_configuration():
    if not API_KEY or not API_SECRET:
        raise SafetyHalt("أضف APCA_API_KEY_ID و APCA_API_SECRET_KEY.")

    if TARGET_DELTA_MIN >= TARGET_DELTA_MAX:
        raise SafetyHalt("نطاق الدلتا غير صالح.")

    if SPREAD_MIN_WIDTH >= SPREAD_MAX_WIDTH:
        raise SafetyHalt("نطاق عرض السبريد غير صالح.")

    if not 0 < RISK_PER_TRADE <= MAX_DAILY_DRAWDOWN:
        raise SafetyHalt("حد المخاطرة لكل صفقة يجب ألا يتجاوز حد الخسارة اليومية.")

    if ENTRY_START >= ENTRY_END or ENTRY_END >= FORCE_EXIT_TIME:
        raise SafetyHalt("أوقات الدخول/الخروج غير مرتبة.")

    if MAX_CONTRACTS < 1:
        raise SafetyHalt("MAX_CONTRACTS غير صالح.")

    return True


def startup_safety_checks():
    validate_configuration()

    if "paper" not in TRADING_URL.lower():
        raise SafetyHalt("رفض التشغيل: عنوان التداول ليس Paper.")

    if os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true":
        raise SafetyHalt("رفض التشغيل: تم تفعيل ALLOW_LIVE_TRADING.")

    account = get_account()

    if account.get("status") != "ACTIVE":
        raise SafetyHalt("حساب Alpaca ليس ACTIVE.")

    if account.get("trading_blocked"):
        raise SafetyHalt("الحساب محظور من التداول.")

    if account.get("account_blocked"):
        raise SafetyHalt("الحساب محظور.")

    if not isinstance(get_clock(), dict):
        raise SafetyHalt("تعذر التحقق من ساعة السوق.")

    equity = safe_float(account.get("equity"), 0.0)

    if equity <= 0:
        raise SafetyHalt("Equity غير صالح.")

    log.info("STARTUP CHECKS PASSED | Equity=%.2f | Paper=True", equity)
    return True


# =========================================================
# 40) MAIN CYCLE
# =========================================================

def run_one_cycle():
    try:
        poll_telegram_commands()
        refresh_daily_baseline()

        if not get_clock().get("is_open", False):
            return

        # عند الإيقاف: لا دخول جديد، لكن نتابع الأوامر والمراكز القائمة
        if STATE.get("halted"):
            process_orders()
            manage_positions()
            return

        check_daily_risk()

        process_orders()
        manage_positions()

        if STATE.get("halted"):
            return

        if not periodic_reconciliation():
            return

        if force_exit_time_reached() or not entry_time_allowed():
            return

        run_entry_engine()

    except EntryBlocked as exc:
        log.info("ENTRY BLOCKED: %s", exc)

    except SafetyHalt as exc:
        if not STATE.get("halted"):
            halt_bot(str(exc))

    except MarketDataUnavailable as exc:
        log.warning("MARKET DATA UNAVAILABLE: %s", exc)

    except APIError as exc:
        log.error("BROKER API ERROR: %s", exc)

    except Exception:
        log.exception("UNHANDLED MAIN CYCLE ERROR.")


# =========================================================
# 41) MAIN
# =========================================================

def main():
    log.warning("SAFAA v%s | PAPER TRADING ONLY | STRATEGY=%s",
                VERSION, STRATEGY_MODE)
    log.info("=" * 55)
    log.info("صَفاء التحليل v%s STARTING", VERSION)
    log.info("=" * 55)

    acquire_instance_lock()
    startup_safety_checks()
    initialize_storage()

    # المصالحة الأولية: عند الاختلاف يتوقف الدخول لكن البوت يبقى يعمل
    reconcile_broker_positions()

    telegram_send(
        f"💎 صَفاء التحليل v{VERSION} بدأ العمل.\n"
        "SPY 0-DTE\nMULTI-LEG ENGINE\nPAPER ONLY"
    )

    log.info("MAIN LOOP STARTED.")

    try:
        while True:
            started = time.monotonic()

            run_one_cycle()

            elapsed = time.monotonic() - started
            time.sleep(max(1, SCAN_SECONDS - elapsed))

    except KeyboardInterrupt:
        log.warning("Manual shutdown requested.")

    telegram_send(f"⛔ صَفاء التحليل v{VERSION} توقف.")
    log.warning("BOT STOPPED.")


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        log.warning("Keyboard interrupt.")

    except Exception as exc:
        log.critical("FATAL STARTUP ERROR: %s", exc, exc_info=True)

        try:
            telegram_send(f"⛔ خطأ قاتل أثناء تشغيل صَفاء التحليل:\n{exc}")
        except Exception:
            pass

        raise

# =========================================================
# END OF SAFAA v114.1
# ========================================================