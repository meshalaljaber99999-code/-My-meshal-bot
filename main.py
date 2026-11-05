
# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v114.0
SPY 0-DTE | MULTI-LEG ENGINE | PAPER ONLY
=========================================================
CALL | PUT | BULL CALL SPREAD | BULL PUT SPREAD
=========================================================
"""

import os, sys, json, time, uuid, math, logging, hashlib
import threading
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# =========================================================
# 1) CONFIGURATION
# =========================================================

VERSION = "114.0"
UNDERLYING = "SPY"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc

BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "."))
BASE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = BASE_DIR / "safaa_v114_state.json"
LEDGER_FILE = BASE_DIR / "safaa_v114_ledger.json"
INIT_FILE = BASE_DIR / "safaa_v114_initialized.flag"
LOCK_FILE = BASE_DIR / "safaa_v114.lock"

API_KEY = os.getenv("APCA_API_KEY_ID", "").strip()
API_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()

TRADING_URL = os.getenv(
    "APCA_API_BASE_URL",
    "https://paper-api.alpaca.markets"
).rstrip("/")

DATA_URL = os.getenv(
    "APCA_DATA_URL",
    "https://data.alpaca.markets"
).rstrip("/")

OPTIONS_DATA_URL = os.getenv(
    "APCA_OPTIONS_DATA_URL",
    "https://data.alpaca.markets"
).rstrip("/")

# Paper-only hard lock
if "paper" not in TRADING_URL.lower():
    raise RuntimeError("v114 تعمل على Paper فقط. عنوان التداول غير مقبول.")

if os.getenv("ALLOW_LIVE_TRADING", "false").lower() == "true":
    raise RuntimeError("التداول الحقيقي ممنوع في v114.")

STRATEGY_MODE = os.getenv("STRATEGY_MODE", "AUTO").upper()

VALID_STRATEGIES = {
    "AUTO",
    "CALL",
    "PUT",
    "BULL_CALL_SPREAD",
    "BULL_PUT_SPREAD"
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

# Option selection
TARGET_DELTA_MIN = 0.40
TARGET_DELTA_MAX = 0.65
TARGET_DELTA = 0.52

MAX_SPREAD_PERCENT = 0.12
MIN_OPTION_ASK = 0.20
MAX_OPTION_ASK = 8.00

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
ORDER_LOOKUP_WAIT_SECONDS = 3

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

# Diagnostics
DIAGNOSTICS_ENABLED = True
REQUIRE_SINGLE_INSTANCE = True

# =========================================================
# 2) LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            BASE_DIR / "safaa_v114.log",
            encoding="utf-8"
        )
    ]
)

log = logging.getLogger("SAFAA_V114")


def diagnostic(stage, message, *args):
    if DIAGNOSTICS_ENABLED:
        log.info("[DIAGNOSTIC:%s] " + message, stage, *args)


def reject(stage, message, *args):
    log.warning("[REJECT:%s] " + message, stage, *args)


# =========================================================
# 3) VALIDATION
# =========================================================

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "أضف APCA_API_KEY_ID و APCA_API_SECRET_KEY في Railway."
    )

if "paper" not in TRADING_URL.lower():
    raise RuntimeError("رفض التشغيل: Paper API مطلوب.")

log.warning("SAFAA v%s | PAPER TRADING ONLY", VERSION)
log.warning("STRATEGY MODE: %s", STRATEGY_MODE)


# =========================================================
# 4) HTTP SESSION
# =========================================================

SESSION = requests.Session()

SESSION.headers.update({
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json"
})


class APIError(Exception):
    pass


class SafetyHalt(Exception):
    pass


class MarketDataUnavailable(Exception):
    pass


def api_request(method, url, params=None, payload=None):
    method = method.upper()
    attempts = 1 if method == "POST" else MAX_API_RETRIES
    last_error = None

    for attempt in range(attempts):
        try:
            response = SESSION.request(
                method=method,
                url=url,
                params=params,
                json=payload,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code >= 400:
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text[:1000]

                raise APIError(
                    f"{method} {url} HTTP "
                    f"{response.status_code}: {detail}"
                )

            return response.json() if response.text else {}

        except APIError:
            raise

        except requests.RequestException as exc:
            last_error = exc

            if method == "POST":
                raise APIError(
                    "نتيجة POST غير مؤكدة؛ "
                    "لن تتم إعادة إرسال الأمر تلقائياً."
                )

            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))

    raise APIError(str(last_error))


def trading_api(method, path, params=None, payload=None):
    return api_request(
        method,
        f"{TRADING_URL}{path}",
        params=params,
        payload=payload
    )


def data_api(method, path, params=None):
    return api_request(
        method,
        f"{DATA_URL}{path}",
        params=params
    )


def options_data_api(method, path, params=None):
    return api_request(
        method,
        f"{OPTIONS_DATA_URL}{path}",
        params=params
    )


# =========================================================
# 5) TIME AND GENERAL HELPERS
# =========================================================

def utc_now():
    return datetime.now(UTC).isoformat()


def ny_now():
    return datetime.now(NY)


def today_ny():
    return ny_now().date().isoformat()


def make_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def sha256_text(value):
    return hashlib.sha256(
        str(value).encode("utf-8")
    ).hexdigest()


def safe_float(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def parse_market_timestamp(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)

        return parsed.astimezone(UTC)

    except Exception:
        return None


def atomic_write_json(path, data):
    temp = Path(str(path) + ".tmp")

    with open(temp, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )
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

    _INSTANCE_LOCK_HANDLE = open(
        LOCK_FILE,
        "a+",
        encoding="utf-8"
    )

    try:
        fcntl.flock(
            _INSTANCE_LOCK_HANDLE.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB
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
    now = utc_now()

    return {
        "version": VERSION,
        "created_at": now,
        "updated_at": now,
        "halted": False,
        "halt_reason": None,

        "trading_day": today_ny(),
        "starting_equity": None,
        "daily_pnl": 0.0,
        "trades_today": 0,
        "consecutive_losses": 0,

        "last_scan_candle": None,
        "last_reconciliation": None,

        "active_client_order_id": None,
        "last_known_positions": {},
        "position_meta": {},
        "spread_meta": {}
    }


def default_ledger():
    return {
        "version": VERSION,
        "orders": {},
        "fills": {},
        "events": []
    }


STATE = None
LEDGER = None
STATE_MUTEX = threading.RLock()


def save_state():
    with STATE_MUTEX:
        STATE["updated_at"] = utc_now()
        atomic_write_json(STATE_FILE, STATE)


def save_ledger():
    with STATE_MUTEX:
        atomic_write_json(LEDGER_FILE, LEDGER)


def ledger_event(event_type, data=None):
    LEDGER.setdefault("events", []).append({
        "id": make_id("EVT"),
        "type": event_type,
        "timestamp": utc_now(),
        "data": data or {}
    })

    LEDGER["events"] = LEDGER["events"][-10000:]
    save_ledger()


def halt_bot(reason):
    STATE["halted"] = True
    STATE["halt_reason"] = str(reason)

    save_state()

    log.critical("TRADING HALTED: %s", reason)


def ensure_not_halted():
    if STATE.get("halted"):
        raise SafetyHalt(
            STATE.get("halt_reason") or "البوت متوقف."
        )


# =========================================================
# 8) STORAGE INITIALIZATION
# =========================================================

def initialize_storage():
    global STATE, LEDGER

    if INIT_FILE.exists():
        if not STATE_FILE.exists() or not LEDGER_FILE.exists():
            raise SafetyHalt(
                "ملفات الحالة أو دفتر الصفقات مفقودة."
            )

        try:
            STATE = read_json(STATE_FILE)
            LEDGER = read_json(LEDGER_FILE)
        except Exception as exc:
            raise SafetyHalt(
                f"تعذر قراءة ملفات v114: {exc}"
            )

        if not isinstance(STATE, dict):
            raise SafetyHalt("ملف الحالة غير صالح.")

        if not isinstance(LEDGER, dict):
            raise SafetyHalt("دفتر الصفقات غير صالح.")

        if not isinstance(LEDGER.get("orders"), dict):
            raise SafetyHalt("بنية سجل الأوامر غير صالحة.")

        if not isinstance(LEDGER.get("fills"), dict):
            raise SafetyHalt("بنية سجل التنفيذ غير صالحة.")

        for key, value in default_state().items():
            STATE.setdefault(key, value)

        LEDGER.setdefault("events", [])

        if STATE.get("version") != VERSION:
            raise SafetyHalt(
                "إصدار ملفات الحالة لا يطابق v114."
            )

        save_state()
        log.info("تم تحميل ملفات v114 السابقة.")
        return

    account = trading_api("GET", "/v2/account")

    if account.get("status") != "ACTIVE":
        raise SafetyHalt(
            f"الحساب غير نشط: {account.get('status')}"
        )

    STATE = default_state()
    LEDGER = default_ledger()

    STATE["starting_equity"] = safe_float(
        account.get("equity")
    )

    if not STATE["starting_equity"]:
        raise SafetyHalt("تعذر تحديد رأس المال.")

    save_state()
    save_ledger()

    with open(INIT_FILE, "w", encoding="utf-8") as file:
        file.write(
            f"Initialized {utc_now()} | version={VERSION}\n"
        )
        file.flush()
        os.fsync(file.fileno())

    log.info(
        "تم إنشاء ملفات v114 | Equity=%.2f",
        STATE["starting_equity"]
    )


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


def refresh_daily_baseline():
    current_day = today_ny()

    if STATE.get("trading_day") == current_day:
        return

    if STATE.get("active_client_order_id"):
        raise SafetyHalt(
            "تغير يوم التداول مع وجود أمر غير محسوم."
        )

    if get_positions():
        raise SafetyHalt(
            "تغير يوم التداول مع وجود مراكز مفتوحة؛ "
            "يلزم التحقق قبل تحديث خط الأساس."
        )

    equity = get_account_equity()

    STATE.update({
        "trading_day": current_day,
        "starting_equity": equity,
        "daily_pnl": 0.0,
        "trades_today": 0,
        "consecutive_losses": 0
    })

    save_state()

    log.info(
        "بداية يوم جديد | Equity=%.2f",
        equity
    )


def check_daily_risk():
    ensure_not_halted()

    starting = safe_float(
        STATE.get("starting_equity")
    )

    if not starting or starting <= 0:
        raise SafetyHalt("رأس المال المرجعي غير صالح.")

    current = get_account_equity()
    pnl = current - starting

    STATE["daily_pnl"] = pnl
    save_state()

    max_loss = starting * MAX_DAILY_DRAWDOWN

    if pnl <= -max_loss:
        halt_bot(
            f"تم بلوغ حد الخسارة اليومية: {pnl:.2f} USD"
        )
        raise SafetyHalt("تجاوز حد الخسارة اليومية.")

    if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        halt_bot("تم بلوغ حد الخسائر المتتالية.")
        raise SafetyHalt("توقف بعد خسائر متتالية.")


# =========================================================
# نهاية الجزء الأول
# =========================================================

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
                "start": (
                    now - timedelta(hours=SPY_LOOKBACK_HOURS)
                ).isoformat(),
                "end": now.isoformat()
            }

            response = data_api(
                "GET",
                f"/v2/stocks/{UNDERLYING}/bars",
                params
            )

            raw = response.get("bars", [])

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
                        "volume": float(item["v"])
                    }

                    values = [
                        bar["open"], bar["high"], bar["low"],
                        bar["close"], bar["volume"]
                    ]

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
                b for b in bars
                if b["_time"] + timedelta(minutes=1) <= now
            ]

            if len(completed) < MIN_SPY_BARS:
                raise MarketDataUnavailable(
                    f"شموع غير كافية: {len(completed)}/{MIN_SPY_BARS}"
                )

            age = (
                now - (
                    completed[-1]["_time"] +
                    timedelta(minutes=1)
                )
            ).total_seconds()

            if age < -5 or age > SPY_MAX_BAR_AGE_SECONDS:
                raise MarketDataUnavailable(
                    f"عمر بيانات SPY غير صالح: {age:.1f}s"
                )

            for b in completed:
                b.pop("_time", None)

            log.info(
                "SPY DATA OK | Feed=%s | Bars=%s | Last=%s | Age=%.1fs",
                feed, len(completed),
                completed[-1]["timestamp"], age
            )

            return completed

        except (APIError, MarketDataUnavailable) as exc:
            last_error = exc
            log.warning(
                "SPY DATA RETRY %s/%s: %s",
                attempt, SPY_DATA_RETRIES, exc
            )

        except Exception as exc:
            last_error = exc
            log.exception("خطأ غير متوقع في بيانات SPY.")

        if attempt < SPY_DATA_RETRIES:
            time.sleep(SPY_RETRY_DELAY * attempt)

    raise MarketDataUnavailable(
        f"تعذر جلب بيانات SPY: {last_error}"
    )


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
        result = (
            value * multiplier +
            result * (1 - multiplier)
        )

    return result


def calculate_rsi(values, period=RSI_PERIOD):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = average(gains[:period])
    avg_loss = average(losses[:period])

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def calculate_vwap(bars):
    # VWAP يبدأ من جلسة التداول الحالية فقط
    today = today_ny()

    session = [
        b for b in bars
        if parse_market_timestamp(
            b["timestamp"]
        ).astimezone(NY).date().isoformat() == today
    ]

    pv = volume = 0.0

    for b in session:
        typical = (
            b["high"] + b["low"] + b["close"]
        ) / 3

        vol = max(b["volume"], 0)

        pv += typical * vol
        volume += vol

    return pv / volume if volume > 0 else None


def calculate_atr(bars, period=14):
    if len(bars) <= period:
        return None

    ranges = []

    for i in range(1, len(bars)):
        current = bars[i]
        previous = bars[i - 1]

        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))

    return average(ranges[-period:])


def calculate_market_indicators(bars):
    if len(bars) < max(
        SLOW_EMA + 1,
        RSI_PERIOD + 1,
        VOLUME_LOOKBACK + 2
    ):
        raise MarketDataUnavailable(
            "عدد الشموع غير كافٍ للمؤشرات."
        )

    closes = [b["close"] for b in bars]
    volumes = [b["volume"] for b in bars]

    fast = ema(closes, FAST_EMA)
    slow = ema(closes, SLOW_EMA)
    rsi = calculate_rsi(closes)
    vwap = calculate_vwap(bars)
    atr = calculate_atr(bars)

    if any(x is None for x in (fast, slow, rsi, vwap, atr)):
        raise MarketDataUnavailable(
            "تعذر حساب المؤشرات."
        )

    last = bars[-1]
    previous = bars[-2]

    avg_volume = average(
        volumes[-(VOLUME_LOOKBACK + 1):-1]
    )

    volume_ratio = (
        last["volume"] / avg_volume
        if avg_volume and avg_volume > 0
        else 0.0
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
        "timestamp": last["timestamp"]
    }


# =========================================================
# 12) MARKET SIGNAL ENGINE
# =========================================================

def get_market_signal(ind):
    price = ind["price"]
    fast = ind["fast_ema"]
    slow = ind["slow_ema"]
    rsi = ind["rsi"]
    vwap = ind["vwap"]
    previous = ind["previous_close"]
    volume_ratio = ind["volume_ratio"]

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
        "confidence_score": (
            max(bull, bear) / 5
        ) * 100
    }


# =========================================================
# 13) TIME AND CANDLE HELPERS
# =========================================================

def make_candle_id(timestamp):
    return sha256_text(
        f"{UNDERLYING}|{timestamp}"
    )[:24]


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
    target = FORCE_EXIT_TIME[0] * 60 + FORCE_EXIT_TIME[1]

    return current >= target


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
        return (
            "BULL_CALL_SPREAD"
            if direction == "CALL"
            else None
        )

    if STRATEGY_MODE == "BULL_PUT_SPREAD":
        return (
            "BULL_PUT_SPREAD"
            if direction == "CALL"
            else None
        )

    # AUTO
    if direction == "PUT":
        return "PUT"

    if direction == "CALL":
        return "BULL_CALL_SPREAD"

    return None


# =========================================================
# نهاية الجزء الثاني
# =========================================================

# =========================================================
# 15) OPTIONS CONTRACT DISCOVERY
# =========================================================

def get_option_contracts(expiration_date=None):
    params = {
        "underlying_symbols": UNDERLYING,
        "status": "active",
        "type": "call",
        "limit": 1000
    }

    if expiration_date:
        params["expiration_date"] = expiration_date

    response = trading_api(
        "GET",
        "/v2/options/contracts",
        params
    )

    contracts = response.get("option_contracts", [])

    if not isinstance(contracts, list):
        raise APIError("صيغة قائمة عقود الخيارات غير صحيحة.")

    return contracts


def contract_expiration(contract):
    try:
        return date.fromisoformat(
            str(contract.get("expiration_date", ""))
        )
    except ValueError:
        return None


def contract_strike(contract):
    try:
        strike = float(contract.get("strike_price"))
        return strike if strike > 0 else None
    except (TypeError, ValueError):
        return None


def contract_type(contract):
    return str(
        contract.get("type", "")
    ).lower().strip()


def contract_symbol(contract):
    return str(
        contract.get("symbol", "")
    ).strip()


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

    expiration = contract_expiration(contract)

    if not expiration or expiration < today_ny():
        return False

    if expiration != today_ny():
        return False

    strike = contract_strike(contract)

    return strike is not None


def get_spy_option_contracts(option_type):
    if option_type not in ("call", "put"):
        raise ValueError("نوع العقد يجب أن يكون call أو put.")

    params = {
        "underlying_symbols": UNDERLYING,
        "status": "active",
        "type": option_type,
        "expiration_date": today_ny().isoformat(),
        "limit": 1000
    }

    response = trading_api(
        "GET",
        "/v2/options/contracts",
        params
    )

    contracts = response.get("option_contracts", [])

    if not isinstance(contracts, list):
        raise APIError("بيانات العقود غير صالحة.")

    valid = [
        c for c in contracts
        if valid_spy_contract(c, option_type)
    ]

    return valid


# =========================================================
# 16) OPTIONS SNAPSHOTS
# =========================================================

def get_option_snapshots(symbols):
    symbols = list(dict.fromkeys(
        str(s).strip() for s in symbols if s
    ))

    if not symbols:
        return {}

    result = {}

    # تقسيم الطلب لتجنب طول URL
    for start in range(0, len(symbols), 100):
        batch = symbols[start:start + 100]

        response = options_data_api(
            "GET",
            f"/v1beta1/options/snapshots/{UNDERLYING}",
            {
                "symbols": ",".join(batch
                ),
                "feed": os.getenv(
                    "ALPACA_OPTIONS_FEED",
                    "indicative"
                )
            }
        )

        snapshots = response.get("snapshots", {})

        if isinstance(snapshots, dict):
            result.update(snapshots)

    return result


def extract_option_quote(snapshot):
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

        timestamp = parse_market_timestamp(
            quote.get("t")
        )

        if not timestamp:
            return None

        age = (
            datetime.now(UTC) - timestamp
        ).total_seconds()

        if age < -5 or age > MAX_QUOTE_AGE_SECONDS:
            return None

        if not all(math.isfinite(x) for x in (
            bid, ask, bid_size, ask_size
        )):
            return None

        if bid <= 0 or ask <= 0 or ask < bid:
            return None

        if bid_size <= 0 or ask_size <= 0:
            return None

        spread = ask - bid
        midpoint = (ask + bid) / 2

        spread_percent = (
            spread / midpoint
            if midpoint > 0 else 1
        )

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
            "age_seconds": age
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

    def number(name):
        try:
            value = float(greeks.get(name))
            return value if math.isfinite(value) else None
        except (TypeError, ValueError):
            return None

    return {
        "delta": number("delta"),
        "gamma": number("gamma"),
        "theta": number("theta"),
        "vega": number("vega"),
        "rho": number("rho")
    }


def delta_matches(snapshot, option_type):
    greeks = extract_option_greeks(snapshot)
    delta = greeks.get("delta")

    if delta is None:
        return False

    absolute_delta = abs(delta)

    if not MIN_DELTA <= absolute_delta <= MAX_DELTA:
        return False

    if option_type == "call" and delta <= 0:
        return False

    if option_type == "put" and delta >= 0:
        return False

    return True


# =========================================================
# 18) OPTION CANDIDATE BUILDER
# =========================================================

def build_option_candidates(option_type, market_price):
    contracts = get_spy_option_contracts(option_type)

    if not contracts:
        log.warning(
            "لا توجد عقود %s متاحة.",
            option_type.upper()
        )
        return []

    selected = []

    for contract in contracts:
        strike = contract_strike(contract)

        if strike is None:
            continue

        # نطاق أولي لتقليل عدد طلبات الأسعار
        if abs(strike - market_price) > 15:
            continue

        selected.append(contract)

    if not selected:
        return []

    symbols = [
        contract_symbol(c)
        for c in selected
    ]

    snapshots = get_option_snapshots(symbols)

    candidates = []

    for contract in selected:
        symbol = contract_symbol(contract)
        snapshot = snapshots.get(symbol)

        quote = extract_option_quote(snapshot)

        if not quote:
            continue

        if quote["ask"] < MIN_OPTION_ASK:
            continue

        if quote["ask"] > MAX_OPTION_ASK:
            continue

        if not delta_matches(snapshot, option_type):
            continue

        greeks = extract_option_greeks(snapshot)

        candidates.append({
            "symbol": symbol,
            "type": option_type,
            "strike": contract_strike(contract),
            "expiration": contract_expiration(
                contract
            ).isoformat(),
            "quote": quote,
            "greeks": greeks,
            "snapshot": snapshot
        })

    candidates.sort(
        key=lambda x: (
            abs(
                abs(x["greeks"]["delta"]) -
                TARGET_DELTA
            ),
            x["quote"]["spread_percent"],
            x["quote"]["age_seconds"]
        )
    )

    return candidates


# =========================================================
# 19) SINGLE LEG SELECTION
# =========================================================

def select_single_option(option_type, market_price):
    candidates = build_option_candidates(
        option_type,
        market_price
    )

    if not candidates:
        reject(
            f"لم نجد عقد {option_type.upper()} "
            "مطابقاً لشروط السعر والدلتا والسيولة."
        )
        return None

    best = candidates[0]

    log.info(
        "OPTION SELECTED | %s | Strike=%.2f | "
        "Bid=%.2f Ask=%.2f Delta=%.3f",
        best["symbol"],
        best["strike"],
        best["quote"]["bid"],
        best["quote"]["ask"],
        best["greeks"]["delta"]
    )

    return best


# =========================================================
# 20) SPREAD PAIR SELECTION
# =========================================================

def spread_width_allowed(width):
    return MIN_SPREAD_WIDTH <= width <= MAX_SPREAD_WIDTH


def build_bull_call_pairs(market_price):
    calls = build_option_candidates(
        "call",
        market_price
    )

    pairs = []

    for long_call in calls:
        for short_call in calls:
            if (
                long_call["symbol"] ==
                short_call["symbol"]
            ):
                continue

            width = (
                short_call["strike"] -
                long_call["strike"]
            )

            if not spread_width_allowed(width):
                continue

            if short_call["strike"] <= long_call["strike"]:
                continue

            debit = (
                long_call["quote"]["ask"] -
                short_call["quote"]["bid"]
            )

            if debit <= 0 or debit > MAX_SPREAD_DEBIT:
                continue

            if debit >= width:
                continue

            # اختيار الرجل الطويلة أقرب إلى الدلتا المستهدفة
            long_delta = abs(
                long_call["greeks"]["delta"]
            )

            short_delta = abs(
                short_call["greeks"]["delta"]
            )

            score = (
                abs(long_delta - TARGET_DELTA) * 3
                + abs(short_delta - 0.25)
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
                "breakeven": (
                    long_call["strike"] + debit
                ),
                "score": score
            })

    pairs.sort(key=lambda x: x["score"])

    return pairs


def build_bull_put_pairs(market_price):
    puts = build_option_candidates(
        "put",
        market_price
    )

    pairs = []

    for short_put in puts:
        for long_put in puts:
            if (
                short_put["symbol"] ==
                long_put["symbol"]
            ):
                continue

            width = (
                short_put["strike"] -
                long_put["strike"]
            )

            if not spread_width_allowed(width):
                continue

            if short_put["strike"] <= long_put["strike"]:
                continue

            credit = (
                short_put["quote"]["bid"] -
                long_put["quote"]["ask"]
            )

            if credit < MIN_SPREAD_CREDIT:
                continue

            if credit >= width:
                continue

            short_delta = abs(
                short_put["greeks"]["delta"]
            )

            long_delta = abs(
                long_put["greeks"]["delta"]
            )

            score = (
                abs(short_delta - TARGET_DELTA) * 3
                + abs(long_delta - 0.25)
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
                "breakeven": (
                    short_put["strike"] - credit
                ),
                "score": score
            })

    pairs.sort(key=lambda x: x["score"])

    return pairs


def select_spread(strategy, market_price):
    if strategy == "BULL_CALL_SPREAD":
        pairs = build_bull_call_pairs(market_price)

    elif strategy == "BULL_PUT_SPREAD":
        pairs = build_bull_put_pairs(market_price)

    else:
        raise ValueError(
            f"استراتيجية سبريد غير مدعومة: {strategy}"
        )

    if not pairs:
        reject(
            f"لم نجد زوج عقود صالحاً للاستراتيجية {strategy}."
        )
        return None

    best = pairs[0]

    log.info(
        "SPREAD SELECTED | %s | Width=%.2f | "
        "Entry=%.2f | MaxLoss=%.2f | MaxGain=%.2f",
        strategy,
        best["width"],
        best["entry_price"],
        best["max_loss_per_share"],
        best["max_gain_per_share"]
    )

    return best


# =========================================================
# نهاية الجزء الثالث
# =========================================================

# =========================================================
# 21) POSITION SIZING AND RISK ENGINE
# =========================================================

def get_available_buying_power():
    account = get_account()

    buying_power = safe_float(
        account.get("buying_power"), 0
    )

    if buying_power <= 0:
        raise SafetyHalt("Buying power غير صالح.")

    return buying_power


def calculate_contract_quantity(
    entry_price,
    max_loss_per_share,
    equity
):
    if entry_price <= 0 or max_loss_per_share <= 0:
        return 0

    buying_power = get_available_buying_power()

    risk_budget = equity * RISK_PER_TRADE
    premium_budget = equity * MAX_PREMIUM_FRACTION

    risk_budget = min(
        risk_budget,
        equity * MAX_DAILY_DRAWDOWN
    )

    max_contracts_by_risk = math.floor(
        risk_budget / (max_loss_per_share * 100)
    )

    max_contracts_by_premium = math.floor(
        premium_budget / (entry_price * 100)
    )

    max_contracts_by_buying_power = math.floor(
        buying_power / (entry_price * 100)
    )

    quantity = min(
        max_contracts_by_risk,
        max_contracts_by_premium,
        max_contracts_by_buying_power,
        10
    )

    return max(0, int(quantity))


def calculate_single_leg_quantity(option, equity):
    ask = option["quote"]["ask"]

    quantity = calculate_contract_quantity(
        entry_price=ask,
        max_loss_per_share=ask,
        equity=equity
    )

    return quantity


def calculate_spread_quantity(spread, equity):
    entry = spread["entry_price"]

    if spread["strategy"] == "BULL_CALL_SPREAD":
        max_loss = spread["max_loss_per_share"]

    elif spread["strategy"] == "BULL_PUT_SPREAD":
        max_loss = spread["max_loss_per_share"]

    else:
        return 0

    return calculate_contract_quantity(
        entry_price=entry,
        max_loss_per_share=max_loss,
        equity=equity
    )


# =========================================================
# 22) ORDER VALIDATION
# =========================================================

def validate_order_quantity(quantity):
    if not isinstance(quantity, int):
        raise ValueError("كمية الأمر يجب أن تكون عدداً صحيحاً.")

    if quantity < 1 or quantity > 10:
        raise SafetyHalt("كمية العقد خارج حدود الأمان.")

    return True


def validate_option_symbol(symbol):
    symbol = str(symbol or "").strip()

    if not symbol.startswith("SPY"):
        raise SafetyHalt(
            f"رفض عقد خارج الأصل الأساسي: {symbol}"
        )

    if len(symbol) < 15:
        raise SafetyHalt(
            f"رمز عقد الخيارات غير مكتمل: {symbol}"
        )

    return True


def validate_entry_window():
    if not market_is_open():
        raise SafetyHalt("السوق مغلق.")

    if not entry_time_allowed():
        raise SafetyHalt("خارج وقت السماح بالدخول.")

    if force_exit_time_reached():
        raise SafetyHalt("وصل وقت إيقاف الدخول.")

    return True


def validate_entry_conditions():
    ensure_not_halted()
    validate_entry_window()
    check_daily_risk()

    if state.get("active_order_id"):
        raise SafetyHalt(
            "يوجد أمر سابق لم تتم تسويته."
        )

    if len(state.get("position_meta", {})) >= MAX_OPEN_POSITIONS:
        raise SafetyHalt("تم بلوغ الحد الأقصى للمراكز.")

    if state.get("trades_today", 0) >= MAX_TRADES_PER_DAY:
        raise SafetyHalt("تم بلوغ الحد اليومي للصفقات.")

    if state.get("consecutive_losses", 0) >= MAX_CONSECUTIVE_LOSSES:
        raise SafetyHalt("تم بلوغ حد الخسائر المتتالية.")

    return True


# =========================================================
# 23) ORDER LEDGER
# =========================================================

def register_order(order_data):
    order_id = order_data.get("id")

    if not order_id:
        raise APIError("استجابة الوسيط لا تحتوي order ID.")

    ledger.setdefault("orders", {})[order_id] = {
        "id": order_id,
        "client_order_id": order_data.get(
            "client_order_id"
        ),
        "status": order_data.get("status"),
        "created_at": utc_now().isoformat(),
        "updated_at": utc_now().isoformat(),
        "strategy": order_data.get(
            "_strategy", "UNKNOWN"
        ),
        "order_class": order_data.get(
            "order_class", "simple"
        ),
        "symbols": order_data.get(
            "_symbols", []
        ),
        "legs": order_data.get(
            "_legs", []
        ),
        "requested_qty": int(
            order_data.get("qty", 0) or 0
        ),
        "filled_qty": 0,
        "average_fill_price": None
    }

    save_ledger()

    return order_id


def update_order_record(order_id, order):
    record = ledger.setdefault(
        "orders", {}
    ).get(order_id)

    if not record:
        return

    record["status"] = order.get("status")
    record["filled_qty"] = int(
        safe_float(order.get("filled_qty"), 0)
    )

    record["average_fill_price"] = safe_float(
        order.get("filled_avg_price")
    )

    record["updated_at"] = utc_now().isoformat()

    save_ledger()


# =========================================================
# 24) ORDER SUBMISSION
# =========================================================

def submit_order_payload(payload):
    validate_order_quantity(
        int(payload.get("qty", 0))
    )

    client_order_id = (
        f"SAFAA114-{today_ny().strftime('%Y%m%d')}-"
        f"{uuid.uuid4().hex[:12]}"
    )

    payload["client_order_id"] = client_order_id

    response = trading_api(
        "POST",
        "/v2/orders",
        payload
    )

    if not isinstance(response, dict) or not response.get("id"):
        raise APIError(
            "لم يؤكد الوسيط إنشاء الأمر."
        )

    log.info(
        "ORDER SUBMITTED | ID=%s | Status=%s",
        response["id"],
        response.get("status")
    )

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
        raise SafetyHalt(
            "نوع العقد لا يتوافق مع اتجاه الصفقة."
        )

    quote = option["quote"]

    if quote["age_seconds"] > MAX_QUOTE_AGE_SECONDS:
        raise SafetyHalt("سعر العقد قديم.")

    limit_price = round(
        quote["ask"] + MAX_ENTRY_SLIPPAGE,
        2
    )

    if limit_price > MAX_OPTION_ASK:
        raise SafetyHalt("سعر الدخول تجاوز الحد الأعلى.")

    payload = {
        "symbol": option["symbol"],
        "qty": str(quantity),
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{limit_price:.2f}",
        "position_intent": "buy_to_open"
    }

    response = submit_order_payload(payload)

    response["_strategy"] = direction
    response["_symbols"] = [option["symbol"]]

    order_id = register_order(response)

    state["active_order_id"] = order_id

    state["position_meta"][order_id] = {
        "strategy": direction,
        "symbol": option["symbol"],
        "quantity": quantity,
        "entry_limit": limit_price,
        "entry_reference": quote["ask"],
        "entry_filled": 0,
        "exit_order_id": None,
        "created_at": utc_now().isoformat()
    }

    save_state()

    return response


# =========================================================
# 26) MULTI-LEG ORDER BUILDER
# =========================================================

def build_mleg_payload(
    strategy,
    long_leg,
    short_leg,
    quantity,
    limit_price,
    closing=False
):
    validate_order_quantity(quantity)

    validate_option_symbol(long_leg["symbol"])
    validate_option_symbol(short_leg["symbol"])

    if long_leg["symbol"] == short_leg["symbol"]:
        raise SafetyHalt("لا يمكن استخدام العقد نفسه للرجلين.")

    if long_leg["expiration"] != short_leg["expiration"]:
        raise SafetyHalt("تاريخ انتهاء الرجلين غير متطابق.")

    if strategy == "BULL_CALL_SPREAD":
        if long_leg["type"] != "call" or short_leg["type"] != "call":
            raise SafetyHalt("نوع عقود Bull Call غير صحيح.")

        if long_leg["strike"] >= short_leg["strike"]:
            raise SafetyHalt("ترتيب strikes في Bull Call غير صحيح.")

        if closing:
            legs = [
                {
                    "symbol": long_leg["symbol"],
                    "ratio_qty": 1,
                    "side": "sell",
                    "position_intent": "sell_to_close"
                },
                {
                    "symbol": short_leg["symbol"],
                    "ratio_qty": 1,
                    "side": "buy",
                    "position_intent": "buy_to_close"
                }
            ]
        else:
            legs = [
                {
                    "symbol": long_leg["symbol"],
                    "ratio_qty": 1,
                    "side": "buy",
                    "position_intent": "buy_to_open"
                },
                {
                    "symbol": short_leg["symbol"],
                    "ratio_qty": 1,
                    "side": "sell",
                    "position_intent": "sell_to_open"
                }
            ]

    elif strategy == "BULL_PUT_SPREAD":
        if long_leg["type"] != "put" or short_leg["type"] != "put":
            raise SafetyHalt("نوع عقود Bull Put غير صحيح.")

        if short_leg["strike"] <= long_leg["strike"]:
            raise SafetyHalt("ترتيب strikes في Bull Put غير صحيح.")

        if closing:
            legs = [
                {
                    "symbol": short_leg["symbol"],
                    "ratio_qty": 1,
                    "side": "buy",
                    "position_intent": "buy_to_close"
                },
                {
                    "symbol": long_leg["symbol"],
                    "ratio_qty": 1,
                    "side": "sell",
                    "position_intent": "sell_to_close"
                }
            ]
        else:
            legs = [
                {
                    "symbol": short_leg["symbol"],
                    "ratio_qty": 1,
                    "side": "sell",
                    "position_intent": "sell_to_open"
                },
                {
                    "symbol": long_leg["symbol"],
                    "ratio_qty": 1,
                    "side": "buy",
                    "position_intent": "buy_to_open"
                }
            ]

    else:
        raise ValueError(
            f"استراتيجية MLeg غير مدعومة: {strategy}"
        )

    price = round(float(limit_price), 2)

    if not math.isfinite(price) or price == 0:
        raise SafetyHalt("سعر السبريد غير صالح.")

    # Bull Call entry: debit موجب
    # Bull Put entry: credit سالب في طلب Alpaca
    # عند الإغلاق تُحسب القيمة وفق اتجاه الرجلين.
    if not closing and strategy == "BULL_PUT_SPREAD":
        price = -abs(price)

    elif not closing and strategy == "BULL_CALL_SPREAD":
        price = abs(price)

    return {
        "order_class": "mleg",
        "qty": str(quantity),
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{price:.2f}",
        "legs": legs
    }


# =========================================================
# 27) SUBMIT SPREAD ENTRY
# =========================================================

def submit_spread_entry(spread, quantity):
    validate_entry_conditions()
    validate_order_quantity(quantity)

    strategy = spread["strategy"]

    long_leg = spread["long_leg"]
    short_leg = spread["short_leg"]

    if strategy == "BULL_CALL_SPREAD":
        limit_price = (
            long_leg["quote"]["ask"]
            - short_leg["quote"]["bid"]
            + MAX_ENTRY_SLIPPAGE
        )

    elif strategy == "BULL_PUT_SPREAD":
        limit_price = (
            short_leg["quote"]["bid"]
            - long_leg["quote"]["ask"]
            - MAX_ENTRY_SLIPPAGE
        )

    else:
        raise ValueError("استراتيجية السبريد غير صالحة.")

    if limit_price <= 0 and strategy == "BULL_CALL_SPREAD":
        raise SafetyHalt("Debit Bull Call غير صالح.")

    if limit_price <= 0 and strategy == "BULL_PUT_SPREAD":
        raise SafetyHalt("Credit Bull Put غير كافٍ.")

    payload = build_mleg_payload(
        strategy=strategy,
        long_leg=long_leg,
        short_leg=short_leg,
        quantity=quantity,
        limit_price=limit_price,
        closing=False
    )

    response = submit_order_payload(payload)

    symbols = [
        long_leg["symbol"],
        short_leg["symbol"]
    ]

    response["_strategy"] = strategy
    response["_symbols"] = symbols
    response["_legs"] = payload["legs"]

    order_id = register_order(response)

    state["active_order_id"] = order_id

    state["spread_meta"][order_id] = {
        "strategy": strategy,
        "long_symbol": long_leg["symbol"],
        "short_symbol": short_leg["symbol"],
        "long_strike": long_leg["strike"],
        "short_strike": short_leg["strike"],
        "expiration": long_leg["expiration"],
        "quantity": quantity,
        "entry_limit": abs(limit_price),
        "entry_filled": 0,
        "exit_order_id": None,
        "created_at": utc_now().isoformat()
    }

    save_state()

    log.info(
        "SPREAD SUBMITTED | %s | Qty=%s | Limit=%s | ID=%s",
        strategy,
        quantity,
        payload["limit_price"],
        order_id
    )

    return response


# =========================================================
# 28) ENTRY DISPATCHER
# =========================================================

def execute_selected_strategy(strategy, market_price, equity):
    if strategy in ("CALL", "PUT"):
        option = select_single_option(
            strategy.lower(),
            market_price
        )

        if not option:
            return None

        quantity = calculate_single_leg_quantity(
            option,
            equity
        )

        if quantity < 1:
            reject("رأس المال لا يسمح بعقد واحد ضمن حدود المخاطرة.")
            return None

        return submit_single_leg(
            option,
            strategy,
            quantity
        )

    if strategy in (
        "BULL_CALL_SPREAD",
        "BULL_PUT_SPREAD"
    ):
        spread = select_spread(
            strategy,
            market_price
        )

        if not spread:
            return None

        quantity = calculate_spread_quantity(
            spread,
            equity
        )

        if quantity < 1:
            reject("رأس المال لا يسمح بسبريد واحد ضمن المخاطرة.")
            return None

        return submit_spread_entry(
            spread,
            quantity
        )

    return None


# =========================================================
# نهاية الجزء الرابع
# =========================================================

# =========================================================
# 29) BROKER ORDER MANAGEMENT
# =========================================================

OPEN_ORDER_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "partially_filled",
    "accepted_for_bidding",
    "pending_replace",
    "pending_cancel"
}

FINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "expired",
    "rejected",
    "done_for_day"
}


def get_order(order_id):
    if not order_id:
        return None

    return trading_api(
        "GET",
        f"/v2/orders/{order_id}"
    )


def cancel_order(order_id):
    if not order_id:
        return False

    try:
        trading_api(
            "DELETE",
            f"/v2/orders/{order_id}"
        )

        log.warning(
            "CANCEL REQUESTED | Order=%s",
            order_id
        )

        return True

    except APIError as exc:
        log.error(
            "CANCEL FAILED | Order=%s | %s",
            order_id,
            exc
        )

        return False


def list_open_orders():
    response = trading_api(
        "GET",
        "/v2/orders",
        {
            "status": "open",
            "limit": 500,
            "nested": "true"
        }
    )

    return response if isinstance(response, list) else []


def wait_for_order_update(order_id, attempts=3):
    latest = None

    for _ in range(attempts):
        try:
            latest = get_order(order_id)

            if not latest:
                continue

            status = str(
                latest.get("status", "")
            ).lower()

            if status in FINAL_ORDER_STATUSES:
                break

        except Exception:
            log.exception(
                "ORDER UPDATE ERROR | %s",
                order_id
            )

        time.sleep(ORDER_LOOKUP_INTERVAL)

    return latest


# =========================================================
# 30) BROKER OPTION POSITIONS
# =========================================================

def get_option_positions():
    response = trading_api(
        "GET",
        "/v2/positions"
    )

    if not isinstance(response, list):
        raise APIError(
            "تعذر قراءة مراكز الحساب."
        )

    result = []

    for position in response:
        if not isinstance(position, dict):
            continue

        # لا نعتبر أسهم SPY مراكز خيارات
        if str(
            position.get("asset_class", "")
        ).lower() != "us_option":
            continue

        symbol = str(
            position.get("symbol", "")
        ).strip()

        if not symbol.startswith("SPY"):
            continue

        try:
            quantity = abs(
                float(position.get("qty", 0))
            )

            if quantity <= 0:
                continue

            side = str(
                position.get("side", "")
            ).lower()

            signed_qty = (
                quantity if side == "long"
                else -quantity
            )

            result.append({
                "symbol": symbol,
                "qty": quantity,
                "signed_qty": signed_qty,
                "side": side,
                "avg_entry_price": safe_float(
                    position.get("avg_entry_price")
                ),
                "market_value": safe_float(
                    position.get("market_value")
                ),
                "unrealized_pl": safe_float(
                    position.get("unrealized_pl")
                ),
                "current_price": safe_float(
                    position.get("current_price")
                )
            })

        except (TypeError, ValueError):
            log.warning(
                "POSITION PARSE ERROR | %s",
                symbol
            )

    return result


def positions_by_symbol(positions):
    return {
        p["symbol"]: p
        for p in positions
    }


# =========================================================
# 31) ORDER FILL RECONCILIATION
# =========================================================

def reconcile_order_fills(order_id):
    record = ledger.get(
        "orders", {}
    ).get(order_id)

    if not record:
        return None

    order = get_order(order_id)

    if not order:
        return None

    update_order_record(order_id, order)

    filled_qty = int(
        safe_float(order.get("filled_qty"), 0)
    )

    status = str(
        order.get("status", "")
    ).lower()

    record["filled_qty"] = filled_qty
    record["status"] = status

    if filled_qty > 0:
        record["average_fill_price"] = safe_float(
            order.get("filled_avg_price")
        )

    record["updated_at"] = utc_now().isoformat()

    save_ledger()

    if status in FINAL_ORDER_STATUSES:
        state["active_order_id"] = None
        save_state()

    return {
        "order": order,
        "status": status,
        "filled_qty": filled_qty,
        "average_fill_price": record.get(
            "average_fill_price"
        )
    }


def reconcile_active_order():
    order_id = state.get("active_order_id")

    if not order_id:
        return None

    result = reconcile_order_fills(order_id)

    if not result:
        halt_bot(
            "تعذر التحقق من حالة الأمر النشط."
        )
        return None

    status = result["status"]

    if status in OPEN_ORDER_STATUSES:
        return result

    if status in FINAL_ORDER_STATUSES:
        log.info(
            "ORDER FINALIZED | ID=%s | Status=%s | Filled=%s",
            order_id,
            status,
            result["filled_qty"]
        )

    return result


# =========================================================
# 32) POSITION RECONCILIATION
# =========================================================

def expected_positions_from_state():
    expected = {}

    for meta in state.get(
        "position_meta", {}
    ).values():

        symbol = meta.get("symbol")
        quantity = int(
            meta.get("entry_filled", 0)
        )

        if symbol and quantity > 0:
            expected[symbol] = (
                expected.get(symbol, 0) + quantity
            )

    for meta in state.get(
        "spread_meta", {}
    ).values():

        quantity = int(
            meta.get("entry_filled", 0)
        )

        if quantity <= 0:
            continue

        strategy = meta.get("strategy")

        long_symbol = meta.get("long_symbol")
        short_symbol = meta.get("short_symbol")

        if not long_symbol or not short_symbol:
            continue

        if strategy == "BULL_CALL_SPREAD":
            expected[long_symbol] = (
                expected.get(long_symbol, 0) + quantity
            )

            expected[short_symbol] = (
                expected.get(short_symbol, 0) - quantity
            )

        elif strategy == "BULL_PUT_SPREAD":
            expected[short_symbol] = (
                expected.get(short_symbol, 0) - quantity
            )

            expected[long_symbol] = (
                expected.get(long_symbol, 0) + quantity
            )

    return expected


def reconcile_broker_positions():
    positions = get_option_positions()

    actual = {
        p["symbol"]: int(round(p["signed_qty"]))
        for p in positions
    }

    expected = expected_positions_from_state()

    all_symbols = set(actual) | set(expected)

    mismatches = []

    for symbol in sorted(all_symbols):
        expected_qty = expected.get(symbol, 0)
        actual_qty = actual.get(symbol, 0)

        if expected_qty != actual_qty:
            mismatches.append({
                "symbol": symbol,
                "expected": expected_qty,
                "actual": actual_qty
            })

    state["last_positions"] = actual
    state["last_reconciliation"] = utc_now().isoformat()

    save_state()

    if mismatches:
        for item in mismatches:
            log.critical(
                "POSITION MISMATCH | %s | Expected=%s | Actual=%s",
                item["symbol"],
                item["expected"],
                item["actual"]
            )

        halt_bot(
            "اختلاف بين المراكز الفعلية والسجل المحلي."
        )

        return False

    log.info(
        "POSITION RECONCILIATION OK | Positions=%s",
        len(actual)
    )

    return True


# =========================================================
# 33) FILL STATE UPDATES
# =========================================================

def update_entry_fill_metadata(order_id, order):
    filled_qty = int(
        safe_float(order.get("filled_qty"), 0)
    )

    if order_id in state.get("position_meta", {}):
        meta = state["position_meta"][order_id]

        old_qty = int(meta.get("entry_filled", 0))

        if filled_qty > old_qty:
            state["trades_today"] = (
                int(state.get("trades_today", 0)) + 1
            )

        meta["entry_filled"] = filled_qty

        meta["entry_average_price"] = safe_float(
            order.get("filled_avg_price")
        )

    elif order_id in state.get("spread_meta", {}):
        meta = state["spread_meta"][order_id]

        old_qty = int(meta.get("entry_filled", 0))

        if filled_qty > old_qty:
            state["trades_today"] = (
                int(state.get("trades_today", 0)) + 1
            )

        meta["entry_filled"] = filled_qty

        meta["entry_average_price"] = safe_float(
            order.get("filled_avg_price")
        )

    save_state()


# =========================================================
# 34) QUOTE REFRESH FOR OPEN POSITIONS
# =========================================================

def get_fresh_quote(symbol):
    validate_option_symbol(symbol)

    snapshots = get_option_snapshots([symbol])

    snapshot = snapshots.get(symbol)

    if not snapshot:
        return None

    return extract_option_quote(snapshot)


def calculate_single_position_return(meta, quote):
    entry = safe_float(
        meta.get("entry_average_price"),
        meta.get("entry_limit", 0)
    )

    if entry <= 0 or not quote:
        return None

    current = quote["bid"]

    if current <= 0:
        return None

    return (current - entry) / entry


def calculate_spread_return(meta, snapshots):
    strategy = meta.get("strategy")

    long_symbol = meta.get("long_symbol")
    short_symbol = meta.get("short_symbol")

    long_quote = extract_option_quote(
        snapshots.get(long_symbol)
    )

    short_quote = extract_option_quote(
        snapshots.get(short_symbol)
    )

    if not long_quote or not short_quote:
        return None

    entry = safe_float(
        meta.get("entry_average_price"),
        meta.get("entry_limit", 0)
    )

    if entry <= 0:
        return None

    if strategy == "BULL_CALL_SPREAD":
        # قيمة التصفية التقريبية للسبريد
        current_value = (
            long_quote["bid"] -
            short_quote["ask"]
        )

        pnl_per_share = current_value - entry

    elif strategy == "BULL_PUT_SPREAD":
        # تكلفة إغلاق السبريد
        close_cost = (
            short_quote["ask"] -
            long_quote["bid"]
        )

        pnl_per_share = entry - close_cost

    else:
        return None

    return {
        "long_quote": long_quote,
        "short_quote": short_quote,
        "pnl_per_share": pnl_per_share,
        "return_percent": pnl_per_share / entry,
        "current_value": current_value
        if strategy == "BULL_CALL_SPREAD"
        else close_cost
    }


# =========================================================
# 35) EXIT ORDER BUILDER
# =========================================================

def submit_single_leg_exit(meta, quantity, quote):
    if meta.get("exit_order_id"):
        return None

    symbol = meta["symbol"]

    validate_option_symbol(symbol)
    validate_order_quantity(quantity)

    limit_price = max(
        0.01,
        round(
            quote["bid"] - MAX_ENTRY_SLIPPAGE,
            2
        )
    )

    payload = {
        "symbol": symbol,
        "qty": str(quantity),
        "side": "sell",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{limit_price:.2f}",
        "position_intent": "sell_to_close"
    }

    response = submit_order_payload(payload)

    response["_strategy"] = (
        f"EXIT_{meta.get('strategy', 'UNKNOWN')}"
    )

    response["_symbols"] = [symbol]

    order_id = register_order(response)

    meta["exit_order_id"] = order_id

    save_state()

    log.warning(
        "SINGLE EXIT SUBMITTED | %s | Qty=%s | ID=%s",
        symbol,
        quantity,
        order_id
    )

    return response


def submit_spread_exit(meta, quantity, snapshots):
    if meta.get("exit_order_id"):
        return None

    strategy = meta.get("strategy")

    long_symbol = meta["long_symbol"]
    short_symbol = meta["short_symbol"]

    long_quote = extract_option_quote(
        snapshots.get(long_symbol)
    )

    short_quote = extract_option_quote(
        snapshots.get(short_symbol)
    )

    if not long_quote or not short_quote:
        reject("تعذر الحصول على أسعار إغلاق السبريد.")
        return None

    long_leg = {
        "symbol": long_symbol,
        "type": (
            "call" if strategy == "BULL_CALL_SPREAD"
            else "put"
        ),
        "strike": meta["long_strike"],
        "expiration": meta["expiration"]
    }

    short_leg = {
        "symbol": short_symbol,
        "type": (
            "call" if strategy == "BULL_CALL_SPREAD"
            else "put"
        ),
        "strike": meta["short_strike"],
        "expiration": meta["expiration"]
    }

    if strategy == "BULL_CALL_SPREAD":
        # البيع لإغلاق السبريد ينتج ائتماناً غالباً
        close_price = (
            short_quote["ask"] -
            long_quote["bid"]
        )

    elif strategy == "BULL_PUT_SPREAD":
        # الشراء لإغلاق السبريد ينتج تكلفة غالباً
        close_price = (
            short_quote["ask"] -
            long_quote["bid"]
        )

    else:
        raise SafetyHalt("نوع السبريد غير صالح للإغلاق.")

    payload = build_mleg_payload(
        strategy=strategy,
        long_leg=long_leg,
        short_leg=short_leg,
        quantity=quantity,
        limit_price=close_price,
        closing=True
    )

    response = submit_order_payload(payload)

    response["_strategy"] = f"EXIT_{strategy}"
    response["_symbols"] = [
        long_symbol,
        short_symbol
    ]
    response["_legs"] = payload["legs"]

    order_id = register_order(response)

    meta["exit_order_id"] = order_id

    save_state()

    log.warning(
        "SPREAD EXIT SUBMITTED | %s | Qty=%s | Price=%s | ID=%s",
        strategy,
        quantity,
        payload["limit_price"],
        order_id
    )

    return response


# =========================================================
# 36) POSITION RISK MANAGEMENT
# =========================================================

def should_exit_position(return_percent, meta):
    if return_percent is None:
        return False, "NO_VALID_MARK"

    if return_percent <= -STOP_LOSS_PERCENT:
        return True, "STOP_LOSS"

    if return_percent >= TAKE_PROFIT_PERCENT:
        return True, "TAKE_PROFIT"

    peak = safe_float(
        meta.get("peak_return"),
        return_percent
    )

    peak = max(peak, return_percent)

    meta["peak_return"] = peak

    if peak >= TRAIL_ACTIVATION_PERCENT:
        drawdown = peak - return_percent

        if drawdown >= TRAIL_DISTANCE_PERCENT:
            return True, "TRAILING_STOP"

    if force_exit_time_reached():
        return True, "TIME_EXIT"

    return False, "HOLD"


def manage_single_position(order_id, meta):
    if meta.get("exit_order_id"):
        return

    quantity = int(meta.get("entry_filled", 0))

    if quantity <= 0:
        return

    quote = get_fresh_quote(meta["symbol"])

    if not quote:
        log.warning(
            "SINGLE POSITION QUOTE UNAVAILABLE | %s",
            meta["symbol"]
        )
        return

    result = calculate_single_position_return(
        meta,
        quote
    )

    if result is None:
        return

    should_exit, reason = should_exit_position(
        result,
        meta
    )

    log.info(
        "SINGLE POSITION | %s | Return=%.2f%% | Decision=%s",
        meta["symbol"],
        result * 100,
        reason
    )

    save_state()

    if should_exit:
        log.warning(
            "EXIT TRIGGERED | %s | %s",
            meta["symbol"],
            reason
        )

        submit_single_leg_exit(
            meta,
            quantity,
            quote
        )


def manage_spread_position(order_id, meta):
    if meta.get("exit_order_id"):
        return

    quantity = int(meta.get("entry_filled", 0))

    if quantity <= 0:
        return

    symbols = [
        meta["long_symbol"],
        meta["short_symbol"]
    ]

    snapshots = get_option_snapshots(symbols)

    result = calculate_spread_return(
        meta,
        snapshots
    )

    if not result:
        log.warning(
            "SPREAD MARK UNAVAILABLE | %s",
            order_id
        )
        return

    should_exit, reason = should_exit_position(
        result["return_percent"],
        meta
    )

    log.info(
        "SPREAD POSITION | %s | Return=%.2f%% | "
        "PnL/share=%.3f | Decision=%s",
        meta["strategy"],
        result["return_percent"] * 100,
        result["pnl_per_share"],
        reason
    )

    save_state()

    if should_exit:
        log.warning(
            "SPREAD EXIT TRIGGERED | %s",
            reason
        )

        submit_spread_exit(
            meta,
            quantity,
            snapshots
        )


# =========================================================
# 37) MANAGE ALL OPEN POSITIONS
# =========================================================

def manage_all_positions():
    if state.get("active_order_id"):
        result = reconcile_active_order()

        if result:
            order_id = state.get("active_order_id")

            if order_id:
                update_entry_fill_metadata(
                    order_id,
                    result["order"]
                )

        # لا نبدأ إدارة خروج جديدة حتى نتأكد
        # من تسوية الأمر النشط
        if state.get("active_order_id"):
            return

    for order_id, meta in list(
        state.get("position_meta", {}).items()
    ):
        try:
            manage_single_position(
                order_id,
                meta
            )
        except SafetyHalt as exc:
            halt_bot(str(exc))
            return
        except Exception:
            log.exception(
                "SINGLE POSITION MANAGEMENT ERROR | %s",
                order_id
            )

    for order_id, meta in list(
        state.get("spread_meta", {}).items()
    ):
        try:
            manage_spread_position(
                order_id,
                meta
            )
        except SafetyHalt as exc:
            halt_bot(str(exc))
            return
        except Exception:
            log.exception(
                "SPREAD POSITION MANAGEMENT ERROR | %s",
                order_id
            )


# =========================================================
# 38) PERIODIC RECONCILIATION
# =========================================================

def periodic_reconciliation():
    now = utc_now()

    previous = parse_market_timestamp(
        state.get("last_reconciliation")
    )

    if previous:
        elapsed = (
            now - previous
        ).total_seconds()

        if elapsed < RECONCILIATION_INTERVAL:
            return True

    if not reconcile_broker_positions():
        return False

    state["last_reconciliation"] = now.isoformat()

    save_state()

    return True


# =========================================================
# 39) END OF DAY SAFETY
# =========================================================

def check_end_of_day():
    if not force_exit_time_reached():
        return

    log.warning(
        "END OF DAY EXIT WINDOW REACHED."
    )

    if state.get("active_order_id"):
        order_id = state["active_order_id"]

        cancel_order(order_id)

        log.warning(
            "ACTIVE ORDER CANCELLATION REQUESTED | %s",
            order_id
        )

        return

    manage_all_positions()


# =========================================================
# نهاية الجزء الخامس
# =========================================================

# =========================================================
# 40) TELEGRAM NOTIFICATIONS
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", ""
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID", ""
).strip()


def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not configured.")
        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": str(message),
                "disable_web_page_preview": True
            },
            timeout=REQUEST_TIMEOUT
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

        equity = safe_float(account.get("equity"))
        buying_power = safe_float(
            account.get("buying_power")
        )

        positions = get_option_positions()

        active_order = state.get("active_order_id")
        halted = state.get("halted", False)

        lines = [
            "💎 صَفاء التحليل v114.0",
            "SPY 0-DTE | PAPER ONLY",
            "",
            f"الحالة: {'متوقف احترازياً' if halted else 'يعمل'}",
            f"Equity: {format_money(equity)}",
            f"Buying Power: {format_money(buying_power)}",
            f"صافي اليوم: {format_money(state.get('daily_pnl', 0))}",
            f"صفقات اليوم: {state.get('trades_today', 0)}",
            f"الخسائر المتتالية: {state.get('consecutive_losses', 0)}",
            f"مراكز الخيارات: {len(positions)}",
            f"أمر نشط: {'نعم' if active_order else 'لا'}",
            "",
            "تحليل احتمالي — لا يوجد ضمان للربح."
        ]

        return "\n".join(lines)

    except Exception as exc:
        log.exception("Status message error.")
        return (
            "💎 صَفاء التحليل v114.0\n"
            f"تعذر استخراج الحالة: {exc}"
        )


# =========================================================
# 41) TELEGRAM COMMANDS
# =========================================================

def telegram_get_updates(offset=None):
    if not TELEGRAM_BOT_TOKEN:
        return []

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/getUpdates"
    )

    params = {
        "timeout": 1
    }

    if offset is not None:
        params["offset"] = offset

    try:
        response = requests.get(
            url,
            params=params,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("ok"):
            return []

        return data.get("result", [])

    except Exception:
        log.exception("Telegram updates error.")
        return []


def handle_telegram_command(text, chat_id):
    if str(chat_id) != TELEGRAM_CHAT_ID:
        log.warning(
            "Rejected unauthorized Telegram chat: %s",
            chat_id
        )
        return

    command = str(text or "").strip().lower()

    if command in ("/start", "/help"):
        telegram_send(
            "أوامر صَفاء التحليل:\n"
            "/status - حالة الحساب والبوت\n"
            "/risk - حدود المخاطر\n"
            "/halt - إيقاف احترازي\n"
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
        halt_bot(
            "تم طلب الإيقاف من Telegram."
        )

        telegram_send(
            "⛔ تم تفعيل الإيقاف الاحترازي.\n"
            "لن يفتح البوت صفقات جديدة."
        )


def poll_telegram_commands():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    offset = state.get("telegram_offset")

    updates = telegram_get_updates(offset)

    for update in updates:
        try:
            update_id = int(update.get("update_id", 0))

            state["telegram_offset"] = update_id + 1

            message = update.get("message", {})
            chat = message.get("chat", {})

            chat_id = chat.get("id")
            text = message.get("text", "")

            if text:
                handle_telegram_command(text, chat_id)

        except Exception:
            log.exception("Telegram command processing error.")

    save_state()


# =========================================================
# 42) ENTRY SIGNAL EVALUATION
# =========================================================

def evaluate_entry_signal():
    if state.get("halted"):
        return None

    if not entry_time_allowed():
        return None

    if force_exit_time_reached():
        return None

    if state.get("active_order_id"):
        return None

    if state.get("trades_today", 0) >= MAX_TRADES_PER_DAY:
        return None

    bars = get_spy_bars()

    indicators = calculate_market_indicators(bars)

    candle_id = make_candle_id(
        indicators["timestamp"]
    )

    if candle_id == state.get("last_candle_id"):
        return None

    # نسجل الشمعة حتى لا تتكرر الإشارة عليها
    state["last_candle_id"] = candle_id
    save_state()

    signal = get_market_signal(indicators)

    strategy = choose_strategy(signal)

    log.info(
        "SPY %.2f | Signal=%s | Score=%s | "
        "RSI=%.1f | EMA9=%.2f | EMA21=%.2f | VWAP=%.2f",
        indicators["price"],
        signal["direction"],
        signal["score"],
        indicators["rsi"],
        indicators["fast_ema"],
        indicators["slow_ema"],
        indicators["vwap"]
    )

    if not strategy:
        log.info("No qualifying strategy.")
        return None

    if signal["score"] < MIN_SIGNAL_SCORE:
        reject("درجة الإشارة أقل من الحد المطلوب.")
        return None

    return {
        "strategy": strategy,
        "signal": signal,
        "indicators": indicators
    }


# =========================================================
# 43) ENTRY EXECUTION
# =========================================================

def run_entry_engine():
    candidate = evaluate_entry_signal()

    if not candidate:
        return None

    validate_entry_conditions()

    equity = get_account_equity()

    if equity <= 0:
        raise SafetyHalt("Equity غير صالح.")

    strategy = candidate["strategy"]

    indicators = candidate["indicators"]

    log.info(
        "ENTRY CANDIDATE | Strategy=%s | Price=%.2f | "
        "Score=%s",
        strategy,
        indicators["price"],
        candidate["signal"]["score"]
    )

    result = execute_selected_strategy(
        strategy,
        indicators["price"],
        equity
    )

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
# 44) STARTUP SAFETY CHECKS
# =========================================================

def startup_safety_checks():
    validate_configuration()

    if "paper" not in TRADING_URL.lower():
        raise SafetyHalt(
            "رفض التشغيل: عنوان التداول ليس Paper."
        )

    if os.getenv(
        "ALLOW_LIVE_TRADING", "false"
    ).lower() == "true":
        raise SafetyHalt(
            "رفض التشغيل: تم تفعيل ALLOW_LIVE_TRADING."
        )

    account = get_account()

    if account.get("status") != "ACTIVE":
        raise SafetyHalt(
            "حساب Alpaca ليس ACTIVE."
        )

    if account.get("trading_blocked"):
        raise SafetyHalt(
            "الحساب محظور من التداول."
        )

    if account.get("account_blocked"):
        raise SafetyHalt(
            "الحساب محظور."
        )

    clock = get_clock()

    if not isinstance(clock, dict):
        raise SafetyHalt(
            "تعذر التحقق من ساعة السوق."
        )

    equity = safe_float(account.get("equity"))

    if equity <= 0:
        raise SafetyHalt(
            "Equity غير صالح."
        )

    log.info(
        "STARTUP CHECKS PASSED | Equity=%.2f | Paper=True",
        equity
    )

    return True


# =========================================================
# 45) SAFE MAIN CYCLE
# =========================================================

def run_one_cycle():
    try:
        poll_telegram_commands()

        if state.get("halted"):
            return

        check_daily_risk()

        # متابعة أوامر الدخول والمراكز القائمة
        manage_all_positions()

        if state.get("halted"):
            return

        if state.get("active_order_id"):
            return

        # المصالحة قبل فتح أي صفقة جديدة
        if not periodic_reconciliation():
            return

        # لا تفتح صفقة إذا توجد مراكز غير مسجلة
        if get_option_positions() and not (
            state.get("position_meta")
            or state.get("spread_meta")
        ):
            halt_bot(
                "مراكز خيارات موجودة دون سجل محلي."
            )
            return

        if force_exit_time_reached():
            check_end_of_day()
            return

        if not entry_time_allowed():
            return

        run_entry_engine()

    except SafetyHalt as exc:
        halt_bot(str(exc))

    except MarketDataUnavailable as exc:
        log.warning(
            "MARKET DATA UNAVAILABLE: %s",
            exc
        )

    except APIError as exc:
        log.error(
            "BROKER API ERROR: %s",
            exc
        )

    except Exception:
        log.exception("UNHANDLED MAIN CYCLE ERROR.")


# =========================================================
# 46) MAIN
# =========================================================

def main():
    global state, ledger

    log.info("=" * 55)
    log.info("صَفاء التحليل v114.0 STARTING")
    log.info("SPY 0-DTE | MULTI-LEG | PAPER ONLY")
    log.info("=" * 55)

    acquire_instance_lock()

    startup_safety_checks()

    initialize_storage()

    # إعادة التحقق بعد تحميل الحالة
    ensure_not_halted()

    # لا نسمح ببدء دورة تداول قبل فحص المراكز الفعلية
    if not reconcile_broker_positions():
        raise SafetyHalt(
            "فشلت المصالحة الأولية للمراكز."
        )

    telegram_send(
        "💎 صَفاء التحليل v114.0 بدأ العمل.\n"
        "SPY 0-DTE\n"
        "MULTI-LEG ENGINE\n"
        "PAPER ONLY"
    )

    log.info("MAIN LOOP STARTED.")

    while True:
        started = time.monotonic()

        try:
            run_one_cycle()

        except KeyboardInterrupt:
            log.warning("Manual shutdown requested.")
            break

        except Exception:
            log.exception("MAIN LOOP FAILURE.")

        elapsed = time.monotonic() - started

        sleep_time = max(
            1,
            SCAN_INTERVAL - elapsed
        )

        time.sleep(sleep_time)

    telegram_send(
        "⛔ صَفاء التحليل v114.0 توقف."
    )

    log.warning("BOT STOPPED.")


# =========================================================
# 47) PROGRAM ENTRY
# =========================================================

if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        log.warning("Keyboard interrupt.")

    except Exception as exc:
        log.critical(
            "FATAL STARTUP ERROR: %s",
            exc,
            exc_info=True
        )

        try:
            telegram_send(
                "⛔ خطأ قاتل أثناء تشغيل صَفاء التحليل:\n"
                f"{exc}"
            )
        except Exception:
            pass

        raise

# =========================================================
# END OF SAFAA v114.0
# =========================================================
