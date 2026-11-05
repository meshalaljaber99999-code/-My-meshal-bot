# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v113.0
SPY 0-DTE | PAPER FIRST | DIAGNOSTIC EXECUTION
=========================================================
"""

import os
import sys
import json
import time
import uuid
import math
import logging
import hashlib
import threading
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests


# =========================================================
# 1) CONFIGURATION
# =========================================================

VERSION = "113.0"

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "."))
BASE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = BASE_DIR / "safaa_v113_state.json"
LEDGER_FILE = BASE_DIR / "safaa_v113_ledger.json"
INIT_FILE = BASE_DIR / "safaa_v113_initialized.flag"
LOCK_FILE = BASE_DIR / "safaa_v113.lock"

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

ALLOW_LIVE = os.getenv(
    "ALLOW_LIVE_TRADING", "false"
).lower() == "true"

CONFIRM_LIVE = os.getenv(
    "CONFIRM_LIVE_TRADING", "false"
).lower() == "true"

UNDERLYING = "SPY"

PAPER_REQUIRED = not (ALLOW_LIVE and CONFIRM_LIVE)

# -------------------------
# Risk controls
# -------------------------

RISK_PER_TRADE = 0.02
MAX_DAILY_DRAWDOWN = 0.03
MAX_PREMIUM_FRACTION = 0.02

MAX_OPEN_POSITIONS = 1
MAX_TRADES_PER_DAY = 4
MAX_CONSECUTIVE_LOSSES = 2

# -------------------------
# Contract selection
# -------------------------

TARGET_DELTA_MIN = 0.40
TARGET_DELTA_MAX = 0.65
TARGET_DELTA = 0.52

MAX_SPREAD_PERCENT = 0.12

MIN_OPTION_ASK = 0.20
MAX_OPTION_ASK = 8.00

# -------------------------
# Position management
# -------------------------

STOP_LOSS_PERCENT = 0.30
TAKE_PROFIT_PERCENT = 0.60

TRAIL_ACTIVATION_PERCENT = 0.35
TRAIL_DISTANCE_PERCENT = 0.18

# -------------------------
# Trading hours - New York
# -------------------------

ENTRY_START = (9, 45)
ENTRY_END = (14, 0)

FORCE_EXIT_TIME = (15, 40)

SCAN_SECONDS = 20
REQUEST_TIMEOUT = 15
MAX_API_RETRIES = 3

# -------------------------
# Execution safety
# -------------------------

MAX_ENTRY_SLIPPAGE_PERCENT = 0.08

ORDER_LOOKUP_WAIT_SECONDS = 3
RECONCILIATION_INTERVAL = 20

# تأخير السماح بمقارنة التنفيذات
FILL_SYNC_GRACE_SECONDS = 12
FILL_SYNC_RETRIES = 3

# -------------------------
# Strategy
# -------------------------

FAST_EMA = 9
SLOW_EMA = 21

RSI_PERIOD = 14
VOLUME_LOOKBACK = 20

MIN_SIGNAL_SCORE = 3

# -------------------------
# Market data
# -------------------------

MIN_SPY_BARS = 40

SPY_DATA_RETRIES = 3
SPY_RETRY_DELAY = 2.5

SPY_MAX_BAR_AGE_SECONDS = 180
SPY_LOOKBACK_HOURS = 6

# -------------------------
# Diagnostics
# -------------------------

DIAGNOSTICS_ENABLED = True
LOG_REJECTED_CONTRACTS = True

# لا نغير شروط الاختيار تلقائياً
MAX_CONTRACT_DIAGNOSTIC_LINES = 30

# -------------------------
# Instance safety
# -------------------------

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
            BASE_DIR / "safaa_v113.log",
            encoding="utf-8"
        )
    ]
)

log = logging.getLogger("SAFAA_V113")


def diagnostic(stage, message, *args):
    if DIAGNOSTICS_ENABLED:
        log.info(
            "[DIAGNOSTIC:%s] " + message,
            stage,
            *args
        )


def reject(stage, reason, *args):
    log.warning(
        "[REJECT:%s] " + reason,
        stage,
        *args
    )


# =========================================================
# 3) BASIC VALIDATION
# =========================================================

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "مفاتيح Alpaca غير موجودة. "
        "أضف APCA_API_KEY_ID و APCA_API_SECRET_KEY."
    )

if ALLOW_LIVE and not CONFIRM_LIVE:
    raise RuntimeError(
        "تم طلب Live دون تأكيد مستقل."
    )

if not ALLOW_LIVE and "paper" not in TRADING_URL.lower():
    raise RuntimeError(
        "إعداد Paper مطلوب. عنوان التداول لا يبدو Paper."
    )

if ALLOW_LIVE and CONFIRM_LIVE:
    log.critical(
        "LIVE TRADING ENABLED. REAL MONEY IS AT RISK."
    )

if REQUIRE_SINGLE_INSTANCE:
    log.warning(
        "يجب تشغيل نسخة واحدة فقط من البوت على الحساب."
    )


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


def api_request(
    method,
    url,
    params=None,
    payload=None,
    timeout=REQUEST_TIMEOUT
):
    method = method.upper()

    # لا نكرر POST تلقائياً أبداً
    attempts = 1 if method == "POST" else MAX_API_RETRIES

    last_error = None

    for attempt in range(attempts):

        try:
            response = SESSION.request(
                method=method,
                url=url,
                params=params,
                json=payload,
                timeout=timeout
            )

            if response.status_code >= 400:

                try:
                    detail = response.json()
                except Exception:
                    detail = response.text[:1000]

                raise APIError(
                    f"{method} {url} "
                    f"HTTP {response.status_code}: {detail}"
                )

            if not response.text:
                return {}

            return response.json()

        except APIError:
            raise

        except requests.RequestException as exc:

            last_error = exc

            if method == "POST":
                raise APIError(
                    "نتيجة POST غير مؤكدة. "
                    "لن تتم إعادة إرسال الأمر تلقائياً. "
                    f"{exc}"
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
# 5) TIME / STORAGE HELPERS
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


def atomic_write_json(path, data):
    temp_path = Path(str(path) + ".tmp")

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )

        file.flush()
        os.fsync(file.fileno())

    os.replace(temp_path, path)


def read_json(path):
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


# =========================================================
# 6) INSTANCE LOCK
# =========================================================

_INSTANCE_LOCK_HANDLE = None


def acquire_instance_lock():
    global _INSTANCE_LOCK_HANDLE

    try:
        import fcntl
    except ImportError:
        raise SafetyHalt(
            "تعذر استخدام قفل النظام."
        )

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
        raise SafetyHalt(
            "هناك نسخة أخرى تعمل على المسار نفسه."
        )

    _INSTANCE_LOCK_HANDLE.seek(0)
    _INSTANCE_LOCK_HANDLE.truncate()

    _INSTANCE_LOCK_HANDLE.write(
        str(os.getpid())
    )

    _INSTANCE_LOCK_HANDLE.flush()


# =========================================================
# 7) STATE AND LEDGER
# =========================================================

def default_state():
    return {
        "version": VERSION,
        "created_at": utc_now(),
        "updated_at": utc_now(),

        "halted": False,
        "halt_reason": None,

        "trading_day": today_ny(),
        "starting_equity": None,
        "daily_pnl": 0.0,

        "trades_today": 0,
        "consecutive_losses": 0,

        "last_scan_candle": None,
        "last_reconciliation": None,

        "candle_orders": {},
        "active_client_order_id": None,

        "last_known_positions": {},
        "last_known_orders": {},

        "position_meta": {}
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


def halt_bot(reason):
    STATE["halted"] = True
    STATE["halt_reason"] = str(reason)

    save_state()

    log.critical(
        "TRADING HALTED: %s",
        reason
    )


def ensure_not_halted():
    if STATE.get("halted"):
        raise SafetyHalt(
            STATE.get("halt_reason")
            or "البوت متوقف لأسباب السلامة."
        )


def initialize_storage():
    global STATE, LEDGER

    if INIT_FILE.exists():

        if not STATE_FILE.exists():
            raise SafetyHalt(
                "ملف الحالة مفقود."
            )

        if not LEDGER_FILE.exists():
            raise SafetyHalt(
                "دفتر الصفقات مفقود."
            )

        try:
            STATE = read_json(STATE_FILE)
            LEDGER = read_json(LEDGER_FILE)

        except Exception as exc:
            raise SafetyHalt(
                f"تعذر قراءة الملفات: {exc}"
            )

        if not isinstance(STATE, dict):
            raise SafetyHalt("ملف الحالة غير صالح.")

        if not isinstance(LEDGER, dict):
            raise SafetyHalt("دفتر الصفقات غير صالح.")

        if "orders" not in LEDGER or "fills" not in LEDGER:
            raise SafetyHalt(
                "بنية دفتر الصفقات غير مكتملة."
            )

        # ترقية الحقول الجديدة دون تصفير البيانات القديمة
        defaults = default_state()

        for key, value in defaults.items():
            if key not in STATE:
                STATE[key] = value

        STATE["version"] = VERSION

        save_state()

        log.info(
            "تم تحميل الحالة والدفتر السابقين."
        )

        return

    account = trading_api(
        "GET",
        "/v2/account"
    )

    if account.get("status") != "ACTIVE":
        raise SafetyHalt(
            f"حالة الحساب ليست ACTIVE: {account.get('status')}"
        )

    STATE = default_state()
    LEDGER = default_ledger()

    STATE["starting_equity"] = float(
        account.get("equity", 0) or 0
    )

    save_state()
    save_ledger()

    with open(INIT_FILE, "w", encoding="utf-8") as file:
        file.write(
            f"Initialized {utc_now()} | version={VERSION}\n"
        )

        file.flush()
        os.fsync(file.fileno())

    log.info(
        "تم إنشاء الحالة لأول مرة. Equity=%.2f",
        STATE["starting_equity"]
    )


# =========================================================
# 8) ACCOUNT HELPERS
# =========================================================

def get_account():
    return trading_api(
        "GET",
        "/v2/account"
    )


def get_account_equity():
    account = get_account()

    equity = float(
        account.get("equity", 0) or 0
    )

    if not math.isfinite(equity) or equity <= 0:
        raise SafetyHalt(
            "قيمة Equity غير صالحة."
        )

    return equity


def get_clock():
    return trading_api(
        "GET",
        "/v2/clock"
    )


def market_is_open():
    clock = get_clock()

    return bool(
        clock.get("is_open", False)
    )


def refresh_daily_baseline():

    current_day = today_ny()

    if STATE.get("trading_day") == current_day:
        return

    if STATE.get("active_client_order_id"):
        raise SafetyHalt(
            "تغير يوم التداول مع وجود أمر غير محسوم."
        )

    equity = get_account_equity()

    STATE["trading_day"] = current_day
    STATE["starting_equity"] = equity

    STATE["daily_pnl"] = 0.0
    STATE["trades_today"] = 0
    STATE["consecutive_losses"] = 0

    save_state()

    log.info(
        "بداية يوم تداول جديد. Equity=%.2f",
        equity
    )


def check_daily_risk():

    ensure_not_halted()

    start_equity = float(
        STATE.get("starting_equity") or 0
    )

    if start_equity <= 0:
        raise SafetyHalt(
            "قيمة رأس المال المرجعية غير صالحة."
        )

    current_equity = get_account_equity()

    pnl = current_equity - start_equity

    STATE["daily_pnl"] = pnl
    save_state()

    max_loss = (
        start_equity * MAX_DAILY_DRAWDOWN
    )

    if pnl <= -max_loss:

        halt_bot(
            f"تم بلوغ حد الخسارة اليومية: {pnl:.2f} USD"
        )

        raise SafetyHalt(
            "تجاوز حد الخسارة اليومية."
        )

    if (
        STATE["consecutive_losses"]
        >= MAX_CONSECUTIVE_LOSSES
    ):

        halt_bot(
            "تم بلوغ حد الخسائر المتتالية."
        )

        raise SafetyHalt(
            "توقف بعد خسائر متتالية."
        )


# =========================================================
# 9) MARKET DATA
# =========================================================

def get_spy_bars(limit=120):

    last_error = None

    feed = os.getenv(
        "ALPACA_STOCK_FEED",
        "iex"
    ).strip().lower()

    if feed not in ("iex", "sip", "boats", "otc"):
        raise MarketDataUnavailable(
            f"مصدر بيانات غير صالح: {feed}"
        )

    for attempt in range(1, SPY_DATA_RETRIES + 1):

        try:
            request_now = datetime.now(UTC)

            start_time = (
                request_now
                - timedelta(hours=SPY_LOOKBACK_HOURS)
            )

            params = {
                "timeframe": "1Min",
                "limit": int(limit),
                "adjustment": "raw",
                "feed": feed,
                "sort": "desc",
                "start": start_time.isoformat(),
                "end": request_now.isoformat()
            }

            result = data_api(
                "GET",
                f"/v2/stocks/{UNDERLYING}/bars",
                params=params
            )

            bars = result.get("bars", [])

            if not isinstance(bars, list) or not bars:
                raise MarketDataUnavailable(
                    "لم تصل شموع SPY."
                )

            normalized = []
            seen = set()

            now = datetime.now(UTC)

            for bar in bars:

                try:
                    timestamp = datetime.fromisoformat(
                        bar["t"].replace("Z", "+00:00")
                    )

                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(
                            tzinfo=UTC
                        )

                    timestamp = timestamp.astimezone(UTC)

                    candle_id = timestamp.isoformat()

                    if candle_id in seen:
                        continue

                    seen.add(candle_id)

                    values = {
                        "open": float(bar["o"]),
                        "high": float(bar["h"]),
                        "low": float(bar["l"]),
                        "close": float(bar["c"]),
                        "volume": float(bar["v"])
                    }

                    if not all(
                        math.isfinite(v)
                        for v in values.values()
                    ):
                        continue

                    if min(
                        values["open"],
                        values["high"],
                        values["low"],
                        values["close"]
                    ) <= 0:
                        continue

                    if values["high"] < values["low"]:
                        continue

                    if values["volume"] < 0:
                        continue

                    normalized.append({
                        "timestamp": bar["t"],
                        "_datetime": timestamp,
                        **values
                    })

                except (
                    KeyError,
                    TypeError,
                    ValueError
                ):
                    continue

            normalized.sort(
                key=lambda x: x["_datetime"]
            )

            completed = []

            for bar in normalized:

                candle_end = (
                    bar["_datetime"]
                    + timedelta(minutes=1)
                )

                if candle_end <= now:
                    completed.append(bar)

            if len(completed) < MIN_SPY_BARS:
                raise MarketDataUnavailable(
                    f"شموع مكتملة غير كافية: "
                    f"{len(completed)}/{MIN_SPY_BARS}"
                )

            last_bar = completed[-1]

            candle_end = (
                last_bar["_datetime"]
                + timedelta(minutes=1)
            )

            age = (
                now - candle_end
            ).total_seconds()

            if age < -5:
                raise MarketDataUnavailable(
                    "توقيت الشمعة يقع في المستقبل."
                )

            if age > SPY_MAX_BAR_AGE_SECONDS:
                raise MarketDataUnavailable(
                    f"بيانات SPY قديمة: {age:.1f}s"
                )

            for bar in completed:
                bar.pop("_datetime", None)

            log.info(
                "SPY DATA OK | Feed=%s | Candles=%s "
                "| Last=%s | Age=%.1fs | Attempt=%s/%s",
                feed,
                len(completed),
                completed[-1]["timestamp"],
                age,
                attempt,
                SPY_DATA_RETRIES
            )

            return completed

        except (
            MarketDataUnavailable,
            APIError
        ) as exc:

            last_error = exc

            log.warning(
                "SPY DATA RETRY %s/%s: %s",
                attempt,
                SPY_DATA_RETRIES,
                exc
            )

        except Exception as exc:

            last_error = exc

            log.exception(
                "خطأ غير متوقع في بيانات SPY."
            )

        if attempt < SPY_DATA_RETRIES:
            time.sleep(
                SPY_RETRY_DELAY * attempt
            )

    raise MarketDataUnavailable(
        f"تعذر جلب بيانات SPY. آخر خطأ: {last_error}"
    )


# =========================================================
# 10) TECHNICAL INDICATORS
# =========================================================

def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1.0)

    current = sum(values[:period]) / period

    for value in values[period:]:
        current = (
            value * multiplier
            + current * (1.0 - multiplier)
        )

    return current


def calculate_rsi(values, period=RSI_PERIOD):

    if len(values) <= period:
        return None

    gains = []
    losses = []

    for index in range(1, len(values)):

        change = (
            values[index] - values[index - 1]
        )

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for index in range(period, len(gains)):

        avg_gain = (
            avg_gain * (period - 1)
            + gains[index]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[index]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (
        100.0 / (1.0 + rs)
    )


def calculate_vwap(bars):

    cumulative_pv = 0.0
    cumulative_volume = 0.0

    for bar in bars:

        typical_price = (
            bar["high"]
            + bar["low"]
            + bar["close"]
        ) / 3.0

        volume = max(
            bar["volume"],
            0
        )

        cumulative_pv += (
            typical_price * volume
        )

        cumulative_volume += volume

    if cumulative_volume <= 0:
        return None

    return cumulative_pv / cumulative_volume


def average(values):

    if not values:
        return None

    return sum(values) / len(values)


def calculate_atr(bars, period=14):

    if len(bars) <= period:
        return None

    ranges = []

    for index in range(1, len(bars)):

        current = bars[index]
        previous = bars[index - 1]

        true_range = max(
            current["high"] - current["low"],
            abs(
                current["high"] - previous["close"]
            ),
            abs(
                current["low"] - previous["close"]
            )
        )

        ranges.append(true_range)

    return average(
        ranges[-period:]
    )


def calculate_market_indicators(bars):

    closes = [
        bar["close"]
        for bar in bars
    ]

    volumes = [
        bar["volume"]
        for bar in bars
    ]

    fast = ema(closes, FAST_EMA)
    slow = ema(closes, SLOW_EMA)

    rsi = calculate_rsi(closes)
    vwap = calculate_vwap(bars)
    atr = calculate_atr(bars)

    if None in (fast, slow, rsi, vwap, atr):
        raise APIError(
            "تعذر حساب المؤشرات الفنية."
        )

    last = bars[-1]
    previous = bars[-2]

    average_volume = average(
        volumes[-(VOLUME_LOOKBACK + 1):-1]
    )

    if not average_volume or average_volume <= 0:
        volume_ratio = 0.0
    else:
        volume_ratio = (
            last["volume"] / average_volume
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
# 11) MARKET SIGNAL
# =========================================================

def get_market_signal(indicators):

    price = indicators["price"]
    fast = indicators["fast_ema"]
    slow = indicators["slow_ema"]
    rsi = indicators["rsi"]
    vwap = indicators["vwap"]
    volume_ratio = indicators["volume_ratio"]

    bullish_score = 0
    bearish_score = 0

    if fast > slow:
        bullish_score += 1
    elif fast < slow:
        bearish_score += 1

    if price > vwap:
        bullish_score += 1
    elif price < vwap:
        bearish_score += 1

    if price > indicators["previous_close"]:
        bullish_score += 1
    elif price < indicators["previous_close"]:
        bearish_score += 1

    if 50 <= rsi <= 68:
        bullish_score += 1
    elif 32 <= rsi < 50:
        bearish_score += 1

    if volume_ratio >= 1.10:

        if price > indicators["previous_close"]:
            bullish_score += 1

        elif price < indicators["previous_close"]:
            bearish_score += 1

    if (
        bullish_score >= MIN_SIGNAL_SCORE
        and bullish_score > bearish_score
    ):
        direction = "CALL"
        score = bullish_score

    elif (
        bearish_score >= MIN_SIGNAL_SCORE
        and bearish_score > bullish_score
    ):
        direction = "PUT"
        score = bearish_score

    else:
        direction = "NEUTRAL"
        score = max(
            bullish_score,
            bearish_score
        )

    return {
        "direction": direction,
        "score": score,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score
    }


# =========================================================
# 12) CANDLE / TIME HELPERS
# =========================================================

def make_candle_id(timestamp):
    return sha256_text(
        f"{UNDERLYING}|{timestamp}"
    )[:24]


def entry_time_allowed():

    now = ny_now()

    if now.weekday() >= 5:
        return False

    current_minutes = (
        now.hour * 60 + now.minute
    )

    start_minutes = (
        ENTRY_START[0] * 60
        + ENTRY_START[1]
    )

    end_minutes = (
        ENTRY_END[0] * 60
        + ENTRY_END[1]
    )

    return (
        start_minutes
        <= current_minutes
        <= end_minutes
    )


def force_exit_time_reached():

    now = ny_now()

    current_minutes = (
        now.hour * 60 + now.minute
    )

    exit_minutes = (
        FORCE_EXIT_TIME[0] * 60
        + FORCE_EXIT_TIME[1]
    )

    return current_minutes >= exit_minutes
    # =========================================================
# 13) OPTIONS CONTRACT DISCOVERY
# =========================================================

def get_today_expiration():

    return ny_now().date().isoformat()


def get_option_contracts(option_type):

    if option_type not in ("call", "put"):
        raise ValueError(
            f"نوع العقد غير صالح: {option_type}"
        )

    expiration = get_today_expiration()

    params = {
        "underlying_symbols": UNDERLYING,
        "status": "active",
        "expiration_date": expiration,
        "type": option_type,
        "limit": 1000
    }

    result = trading_api(
        "GET",
        "/v2/options/contracts",
        params=params
    )

    contracts = result.get("option_contracts", [])

    if not isinstance(contracts, list):
        raise APIError(
            "استجابة قائمة العقود غير صالحة."
        )

    return contracts


# =========================================================
# 14) OPTIONS SNAPSHOTS
# =========================================================

def get_option_snapshots(symbols):

    if not symbols:
        return {}

    snapshots = {}

    # حد الدفعة محافظ لتجنب طلب طويل جداً
    batch_size = 50

    for start in range(0, len(symbols), batch_size):

        batch = symbols[start:start + batch_size]

        params = {
            "symbols": ",".join(batch)
        }

        result = options_data_api(
            "GET",
            "/v1beta1/options/snapshots",
            params=params
        )

        data = result.get("snapshots", {})

        if not isinstance(data, dict):
            raise APIError(
                "استجابة snapshots غير صالحة."
            )

        snapshots.update(data)

    return snapshots


# =========================================================
# 15) SNAPSHOT FIELD HELPERS
# =========================================================

def safe_float(value):

    try:
        result = float(value)

        if not math.isfinite(result):
            return None

        return result

    except (TypeError, ValueError):
        return None


def get_nested(data, *keys):

    current = data

    for key in keys:

        if not isinstance(current, dict):
            return None

        current = current.get(key)

        if current is None:
            return None

    return current


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


def extract_quote(snapshot):

    quote = snapshot.get("latestQuote", {})

    if not isinstance(quote, dict):
        quote = {}

    bid = safe_float(
        quote.get("bp")
    )

    ask = safe_float(
        quote.get("ap")
    )

    bid_size = safe_float(
        quote.get("bs")
    )

    ask_size = safe_float(
        quote.get("as")
    )

    timestamp = parse_market_timestamp(
        quote.get("t")
    )

    return {
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "timestamp": timestamp
    }


def extract_delta(snapshot):

    greeks = snapshot.get("greeks", {})

    if not isinstance(greeks, dict):
        return None

    return safe_float(
        greeks.get("delta")
    )


# =========================================================
# 16) QUOTE FRESHNESS
# =========================================================

def quote_freshness(quote):

    timestamp = quote.get("timestamp")

    if timestamp is None:
        return False, None, "QUOTE_TIMESTAMP_MISSING"

    age = (
        datetime.now(UTC) - timestamp
    ).total_seconds()

    # رفض الطابع الزمني المستقبلي
    if age < -5:
        return False, age, "QUOTE_TIMESTAMP_FUTURE"

    # حد محافظ لعمر عرض السعر
    max_age = float(
        os.getenv(
            "MAX_OPTION_QUOTE_AGE_SECONDS",
            "60"
        )
    )

    if age > max_age:
        return False, age, "QUOTE_STALE"

    return True, age, "QUOTE_FRESH"


# =========================================================
# 17) CONTRACT DIAGNOSTICS
# =========================================================

def new_contract_stats():

    return {
        "discovered": 0,
        "snapshot_missing": 0,
        "quote_missing": 0,
        "quote_stale": 0,
        "invalid_bid": 0,
        "invalid_ask": 0,
        "ask_below_min": 0,
        "ask_above_max": 0,
        "spread_too_wide": 0,
        "delta_missing": 0,
        "delta_outside_range": 0,
        "wrong_type": 0,
        "wrong_expiration": 0,
        "passed": 0,
        "budget_rejected": 0
    }


def log_contract_stats(stats):

    log.info(
        "CONTRACT DIAGNOSTICS | "
        "Found=%s | SnapshotMissing=%s | "
        "QuoteMissing=%s | Stale=%s | "
        "BadBid=%s | BadAsk=%s | "
        "AskLow=%s | AskHigh=%s | "
        "WideSpread=%s | DeltaMissing=%s | "
        "DeltaOut=%s | Passed=%s | BudgetRejected=%s",
        stats["discovered"],
        stats["snapshot_missing"],
        stats["quote_missing"],
        stats["quote_stale"],
        stats["invalid_bid"],
        stats["invalid_ask"],
        stats["ask_below_min"],
        stats["ask_above_max"],
        stats["spread_too_wide"],
        stats["delta_missing"],
        stats["delta_outside_range"],
        stats["passed"],
        stats["budget_rejected"]
    )


# =========================================================
# 18) CONTRACT VALIDATION
# =========================================================

def validate_contract_metadata(contract, option_type):

    symbol = contract.get("symbol")

    if not symbol:
        return False, "SYMBOL_MISSING"

    contract_type = str(
        contract.get("type", "")
    ).lower()

    if contract_type and contract_type != option_type:
        return False, "WRONG_TYPE"

    expiration = str(
        contract.get("expiration_date", "")
    )

    if expiration != get_today_expiration():
        return False, "WRONG_EXPIRATION"

    status = str(
        contract.get("status", "active")
    ).lower()

    if status not in ("active", ""):
        return False, "CONTRACT_NOT_ACTIVE"

    return True, "OK"


# =========================================================
# 19) SPREAD CALCULATION
# =========================================================

def calculate_spread_percent(bid, ask):

    if bid is None or ask is None:
        return None

    if bid <= 0 or ask <= 0:
        return None

    if ask < bid:
        return None

    midpoint = (
        bid + ask
    ) / 2.0

    if midpoint <= 0:
        return None

    return (
        (ask - bid) / midpoint
    )


# =========================================================
# 20) CONTRACT FILTERING
# =========================================================

def inspect_contract(
    contract,
    snapshot,
    option_type,
    stats
):

    stats["discovered"] += 1

    valid, reason = validate_contract_metadata(
        contract,
        option_type
    )

    if not valid:

        if reason == "WRONG_TYPE":
            stats["wrong_type"] += 1

        elif reason == "WRONG_EXPIRATION":
            stats["wrong_expiration"] += 1

        return None, reason

    if not isinstance(snapshot, dict):

        stats["snapshot_missing"] += 1

        return None, "SNAPSHOT_MISSING"

    quote = extract_quote(snapshot)

    bid = quote["bid"]
    ask = quote["ask"]

    if bid is None or ask is None:

        stats["quote_missing"] += 1

        return None, "QUOTE_MISSING"

    fresh, age, freshness_reason = quote_freshness(
        quote
    )

    if not fresh:

        stats["quote_stale"] += 1

        return None, freshness_reason

    if bid <= 0:

        stats["invalid_bid"] += 1

        return None, "INVALID_BID"

    if ask <= 0:

        stats["invalid_ask"] += 1

        return None, "INVALID_ASK"

    if ask < bid:

        stats["invalid_ask"] += 1

        return None, "ASK_BELOW_BID"

    if ask < MIN_OPTION_ASK:

        stats["ask_below_min"] += 1

        return None, "ASK_BELOW_MIN"

    if ask > MAX_OPTION_ASK:

        stats["ask_above_max"] += 1

        return None, "ASK_ABOVE_MAX"

    spread = calculate_spread_percent(
        bid,
        ask
    )

    if spread is None:

        stats["spread_too_wide"] += 1

        return None, "INVALID_SPREAD"

    if spread > MAX_SPREAD_PERCENT:

        stats["spread_too_wide"] += 1

        return None, "SPREAD_TOO_WIDE"

    delta = extract_delta(snapshot)

    if delta is None:

        stats["delta_missing"] += 1

        return None, "DELTA_MISSING"

    absolute_delta = abs(delta)

    if not (
        TARGET_DELTA_MIN
        <= absolute_delta
        <= TARGET_DELTA_MAX
    ):

        stats["delta_outside_range"] += 1

        return None, "DELTA_OUTSIDE_RANGE"

    # لا نقبل عقداً دون حجم عرض شراء ظاهر
    bid_size = quote["bid_size"]
    ask_size = quote["ask_size"]

    if bid_size is not None and bid_size <= 0:
        return None, "NO_BID_SIZE"

    if ask_size is not None and ask_size <= 0:
        return None, "NO_ASK_SIZE"

    stats["passed"] += 1

    return {
        "symbol": contract["symbol"],
        "type": option_type,
        "expiration": contract.get(
            "expiration_date"
        ),
        "strike": safe_float(
            contract.get("strike_price")
        ),
        "bid": bid,
        "ask": ask,
        "spread_percent": spread,
        "delta": delta,
        "absolute_delta": absolute_delta,
        "quote_age_seconds": age,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "snapshot": snapshot
    }, "OK"


# =========================================================
# 21) CONTRACT RANKING
# =========================================================

def rank_contract(candidate):

    delta_distance = abs(
        candidate["absolute_delta"]
        - TARGET_DELTA
    )

    spread_score = (
        candidate["spread_percent"]
        / MAX_SPREAD_PERCENT
    )

    # الأفضلية للدلتا الأقرب للهدف
    # ثم السبريد الأقل
    return (
        delta_distance * 0.65
        + spread_score * 0.35
    )


# =========================================================
# 22) CONTRACT DISCOVERY ENGINE
# =========================================================

def discover_option_candidates(option_type):

    stats = new_contract_stats()

    log.info(
        "بدء البحث عن عقود %s | Expiration=%s",
        option_type.upper(),
        get_today_expiration()
    )

    contracts = get_option_contracts(
        option_type
    )

    if not contracts:

        log.warning(
            "لم تُرجع Alpaca أي عقود %s لهذا اليوم.",
            option_type.upper()
        )

        log_contract_stats(stats)

        return [], stats

    # تحقق أولي من الرموز قبل طلب snapshots
    valid_contracts = []

    for contract in contracts:

        valid, reason = validate_contract_metadata(
            contract,
            option_type
        )

        if valid:
            valid_contracts.append(contract)

        elif reason == "WRONG_TYPE":
            stats["wrong_type"] += 1

        elif reason == "WRONG_EXPIRATION":
            stats["wrong_expiration"] += 1

    symbols = [
        contract["symbol"]
        for contract in valid_contracts
        if contract.get("symbol")
    ]

    log.info(
        "OPTION CONTRACTS | Retrieved=%s | ValidMetadata=%s",
        len(contracts),
        len(symbols)
    )

    if not symbols:

        log_contract_stats(stats)

        return [], stats

    snapshots = get_option_snapshots(
        symbols
    )

    candidates = []

    rejected_examples = []

    for contract in valid_contracts:

        symbol = contract.get("symbol")

        snapshot = snapshots.get(symbol)

        candidate, reason = inspect_contract(
            contract,
            snapshot,
            option_type,
            stats
        )

        if candidate:

            candidates.append(candidate)

        elif (
            LOG_REJECTED_CONTRACTS
            and len(rejected_examples)
            < MAX_CONTRACT_DIAGNOSTIC_LINES
        ):

            rejected_examples.append(
                (symbol, reason)
            )

    for symbol, reason in rejected_examples:

        log.info(
            "CONTRACT REJECTED | %s | %s",
            symbol,
            reason
        )

    candidates.sort(
        key=rank_contract
    )

    log_contract_stats(stats)

    log.info(
        "العقود المقبولة بعد فلاتر الجودة: %s",
        len(candidates)
    )

    return candidates, stats


# =========================================================
# 23) BUDGET ENGINE
# =========================================================

def calculate_entry_budget():

    equity = get_account_equity()

    risk_budget = (
        equity * RISK_PER_TRADE
    )

    premium_budget = (
        equity * MAX_PREMIUM_FRACTION
    )

    budget = min(
        risk_budget,
        premium_budget
    )

    log.info(
        "ENTRY BUDGET | Equity=$%.2f | "
        "RiskLimit=$%.2f | PremiumLimit=$%.2f | "
        "AllowedBudget=$%.2f",
        equity,
        risk_budget,
        premium_budget,
        budget
    )

    return {
        "equity": equity,
        "risk_budget": risk_budget,
        "premium_budget": premium_budget,
        "allowed_budget": budget
    }


def calculate_contract_quantity(
    candidate,
    budget
):

    ask = candidate["ask"]

    if ask <= 0:
        return 0

    cost_per_contract = ask * 100.0

    allowed_budget = budget["allowed_budget"]

    quantity = int(
        math.floor(
            allowed_budget / cost_per_contract
        )
    )

    if quantity < 1:

        log.info(
            "BUDGET REJECTED | %s | "
            "ContractCost=$%.2f | Budget=$%.2f",
            candidate["symbol"],
            cost_per_contract,
            allowed_budget
        )

        return 0

    # لا نتجاوز الميزانية لأي سبب
    total_cost = (
        quantity * cost_per_contract
    )

    if total_cost > allowed_budget + 0.000001:
        return 0

    return quantity


# =========================================================
# 24) SELECT BEST CONTRACT
# =========================================================

def select_option_contract(option_type):

    candidates, stats = discover_option_candidates(
        option_type
    )

    if not candidates:

        log.warning(
            "لا يوجد عقد اجتاز فلاتر السوق. "
            "Direction=%s | Discovered=%s | "
            "SnapshotMissing=%s | Stale=%s | "
            "SpreadRejected=%s | DeltaRejected=%s",
            option_type.upper(),
            stats["discovered"],
            stats["snapshot_missing"],
            stats["quote_stale"],
            stats["spread_too_wide"],
            stats["delta_outside_range"]
        )

        return None

    budget = calculate_entry_budget()

    for candidate in candidates:

        quantity = calculate_contract_quantity(
            candidate,
            budget
        )

        if quantity <= 0:

            stats["budget_rejected"] += 1

            continue

        candidate["quantity"] = quantity

        candidate["estimated_cost"] = (
            candidate["ask"]
            * 100.0
            * quantity
        )

        candidate["budget"] = budget

        log.info(
            "SELECTED CONTRACT | %s | "
            "Type=%s | Strike=%s | "
            "Bid=%.2f | Ask=%.2f | "
            "Delta=%.3f | Spread=%.2f%% | "
            "Qty=%s | Cost=$%.2f",
            candidate["symbol"],
            candidate["type"].upper(),
            candidate["strike"],
            candidate["bid"],
            candidate["ask"],
            candidate["delta"],
            candidate["spread_percent"] * 100,
            quantity,
            candidate["estimated_cost"]
        )

        log_contract_stats(stats)

        return candidate

    log.warning(
        "العقود اجتازت شروط السوق، "
        "لكن الميزانية لا تسمح بشراء عقد واحد. "
        "Equity=$%.2f | Budget=$%.2f",
        budget["equity"],
        budget["allowed_budget"]
    )

    log_contract_stats(stats)

    return None


# =========================================================
# 25) PRE-ENTRY QUOTE RECHECK
# =========================================================

def refresh_selected_contract(candidate):

    symbol = candidate["symbol"]

    snapshots = get_option_snapshots([
        symbol
    ])

    snapshot = snapshots.get(symbol)

    if not isinstance(snapshot, dict):
        raise APIError(
            f"تعذر تحديث Snapshot للعقد {symbol}"
        )

    quote = extract_quote(snapshot)

    fresh, age, reason = quote_freshness(
        quote
    )

    if not fresh:
        raise APIError(
            f"Quote غير صالح قبل الدخول: {reason}"
        )

    bid = quote["bid"]
    ask = quote["ask"]

    if bid is None or ask is None:
        raise APIError(
            "بيانات Bid/Ask غير مكتملة قبل الدخول."
        )

    if bid <= 0 or ask <= 0 or ask < bid:
        raise APIError(
            "أسعار العقد غير صالحة قبل الدخول."
        )

    spread = calculate_spread_percent(
        bid,
        ask
    )

    if spread is None or spread > MAX_SPREAD_PERCENT:
        raise APIError(
            "اتساع السبريد قبل الدخول."
        )

    old_ask = candidate["ask"]

    if old_ask <= 0:
        raise APIError(
            "السعر المرجعي للعقد غير صالح."
        )

    slippage = (
        (ask - old_ask) / old_ask
    )

    if slippage > MAX_ENTRY_SLIPPAGE_PERCENT:
        raise APIError(
            f"ارتفاع سعر الدخول عن السعر المرجعي: "
            f"{slippage * 100:.2f}%"
        )

    if slippage < -MAX_ENTRY_SLIPPAGE_PERCENT:
        log.info(
            "السعر تحسن عن السعر المرجعي: %.2f%%",
            slippage * 100
        )

    delta = extract_delta(snapshot)

    if delta is None:
        raise APIError(
            "Greeks/Delta غير متوفرة عند إعادة الفحص."
        )

    if not (
        TARGET_DELTA_MIN
        <= abs(delta)
        <= TARGET_DELTA_MAX
    ):
        raise APIError(
            f"Delta تغيرت خارج النطاق: {delta:.3f}"
        )

    candidate["bid"] = bid
    candidate["ask"] = ask
    candidate["delta"] = delta
    candidate["absolute_delta"] = abs(delta)
    candidate["spread_percent"] = spread
    candidate["quote_age_seconds"] = age
    candidate["snapshot"] = snapshot

    # إعادة حساب الكمية بالسعر المحدث
    budget = candidate["budget"]

    quantity = calculate_contract_quantity(
        candidate,
        budget
    )

    if quantity <= 0:
        raise APIError(
            "السعر المحدث لا يسمح بشراء عقد واحد ضمن الميزانية."
        )

    candidate["quantity"] = quantity

    candidate["estimated_cost"] = (
        ask * 100.0 * quantity
    )

    log.info(
        "PRE-ENTRY RECHECK OK | %s | "
        "Bid=%.2f | Ask=%.2f | "
        "Delta=%.3f | Spread=%.2f%% | "
        "QuoteAge=%.1fs | Qty=%s | Cost=$%.2f",
        symbol,
        bid,
        ask,
        delta,
        spread * 100,
        age,
        quantity,
        candidate["estimated_cost"]
    )

    return candidate
    # =========================================================
# 26) ORDER / POSITION API
# =========================================================

def get_orders(status="open", limit=500):

    return trading_api(
        "GET",
        "/v2/orders",
        params={
            "status": status,
            "limit": limit,
            "direction": "desc",
            "nested": "true"
        }
    )


def get_positions():

    result = trading_api(
        "GET",
        "/v2/positions"
    )

    if not isinstance(result, list):
        raise APIError(
            "استجابة المراكز غير صالحة."
        )

    return result


def get_order_by_id(order_id):

    return trading_api(
        "GET",
        f"/v2/orders/{order_id}"
    )


def get_order_by_client_id(client_order_id):

    return trading_api(
        "GET",
        "/v2/orders:by_client_order_id",
        params={
            "client_order_id": client_order_id
        }
    )


# =========================================================
# 27) LEDGER EVENTS
# =========================================================

def ledger_event(event_type, data=None):

    event = {
        "id": make_id("EVT"),
        "type": event_type,
        "timestamp": utc_now(),
        "data": data or {}
    }

    LEDGER.setdefault(
        "events", []
    ).append(event)

    # الاحتفاظ بسجل الأحداث دون حذف سجل الأوامر
    if len(LEDGER["events"]) > 10000:
        LEDGER["events"] = LEDGER["events"][-10000:]

    save_ledger()


def register_order_intent(
    client_order_id,
    symbol,
    quantity,
    side,
    limit_price,
    signal
):

    if client_order_id in LEDGER["orders"]:
        raise SafetyHalt(
            "محاولة تسجيل client_order_id مكرر."
        )

    intent = {
        "client_order_id": client_order_id,
        "broker_order_id": None,
        "symbol": symbol,
        "quantity": int(quantity),
        "side": side,
        "limit_price": float(limit_price),
        "signal": signal,

        "status": "INTENT_CREATED",
        "created_at": utc_now(),
        "updated_at": utc_now(),

        "filled_qty": 0.0,
        "filled_avg_price": None,

        "submitted": False,
        "terminal": False,
        "submission_unknown": False,

        "last_broker_status": None,
        "last_error": None
    }

    LEDGER["orders"][client_order_id] = intent

    save_ledger()

    ledger_event(
        "ORDER_INTENT_CREATED",
        {
            "client_order_id": client_order_id,
            "symbol": symbol,
            "quantity": quantity,
            "side": side,
            "limit_price": limit_price
        }
    )

    return intent


def update_order_record(
    client_order_id,
    **updates
):

    record = LEDGER["orders"].get(
        client_order_id
    )

    if record is None:
        raise SafetyHalt(
            "سجل الأمر غير موجود."
        )

    record.update(updates)

    record["updated_at"] = utc_now()

    save_ledger()

    return record


# =========================================================
# 28) ORDER STATUS HELPERS
# =========================================================

TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "expired",
    "rejected"
}


def order_is_terminal(status):

    return str(status).lower() in TERMINAL_ORDER_STATUSES


def normalize_order(order):

    if not isinstance(order, dict):
        raise APIError(
            "بيانات الأمر غير صالحة."
        )

    return {
        "id": order.get("id"),
        "client_order_id": order.get("client_order_id"),
        "symbol": order.get("symbol"),
        "status": str(
            order.get("status", "")
        ).lower(),

        "qty": safe_float(
            order.get("qty")
        ) or 0.0,

        "filled_qty": safe_float(
            order.get("filled_qty")
        ) or 0.0,

        "filled_avg_price": safe_float(
            order.get("filled_avg_price")
        ),

        "side": order.get("side"),
        "limit_price": safe_float(
            order.get("limit_price")
        ),

        "submitted_at": order.get("submitted_at"),
        "updated_at": order.get("updated_at")
    }


def apply_broker_order_update(order):

    normalized = normalize_order(order)

    client_id = normalized["client_order_id"]

    if not client_id:
        raise SafetyHalt(
            "أمر لدى الوسيط بلا client_order_id."
        )

    record = LEDGER["orders"].get(client_id)

    if record is None:
        raise SafetyHalt(
            f"وجد أمر غير مسجل في الدفتر: {client_id}"
        )

    broker_filled = normalized["filled_qty"]

    previous_filled = float(
        record.get("filled_qty", 0) or 0
    )

    # الكمية لدى الوسيط لا يجوز أن تتناقص
    if broker_filled + 0.000001 < previous_filled:
        raise SafetyHalt(
            f"تراجع كمية التنفيذ المسجلة: {client_id}"
        )

    record.update({
        "broker_order_id": normalized["id"],
        "status": normalized["status"],
        "last_broker_status": normalized["status"],
        "filled_qty": broker_filled,
        "filled_avg_price": normalized["filled_avg_price"],
        "submitted": True,
        "terminal": order_is_terminal(
            normalized["status"]
        ),
        "submission_unknown": False,
        "updated_at": utc_now()
    })

    save_ledger()

    return normalized


# =========================================================
# 29) CLIENT ORDER ID GENERATION
# =========================================================

def create_client_order_id(symbol, candle_id):

    digest = sha256_text(
        f"{VERSION}|{symbol}|{candle_id}"
    )[:18]

    return f"SF113-{digest}"


def order_intent_exists(client_order_id):

    return (
        client_order_id in LEDGER["orders"]
    )


def active_entry_intent_exists():

    for record in LEDGER["orders"].values():

        if record.get("side") != "buy":
            continue

        status = str(
            record.get("status", "")
        ).lower()

        if record.get("submission_unknown"):
            return True

        if not record.get("terminal", False):
            return True

        if status in (
            "new",
            "accepted",
            "pending_new",
            "partially_filled",
            "pending_cancel",
            "pending_replace"
        ):
            return True

    return False


# =========================================================
# 30) BROKER ORDER LOOKUP
# =========================================================

def lookup_order_after_submit(client_order_id):

    last_error = None

    for attempt in range(
        ORDER_LOOKUP_WAIT_SECONDS
    ):

        try:

            order = get_order_by_client_id(
                client_order_id
            )

            if order and order.get("id"):
                return order

        except Exception as exc:
            last_error = exc

        time.sleep(1)

    log.warning(
        "ORDER LOOKUP INCONCLUSIVE | "
        "ClientID=%s | LastError=%s",
        client_order_id,
        last_error
    )

    return None


# =========================================================
# 31) SAFE ORDER SUBMISSION
# =========================================================

def submit_limit_order_once(
    candidate,
    signal
):

    ensure_not_halted()

    if active_entry_intent_exists():
        raise SafetyHalt(
            "يوجد أمر شراء سابق غير محسوم."
        )

    symbol = candidate["symbol"]
    quantity = int(candidate["quantity"])

    if quantity <= 0:
        raise APIError(
            "كمية الأمر غير صالحة."
        )

    limit_price = round(
        float(candidate["ask"]),
        2
    )

    if limit_price <= 0:
        raise APIError(
            "سعر الأمر غير صالح."
        )

    estimated_cost = (
        limit_price * 100 * quantity
    )

    budget = candidate["budget"]["allowed_budget"]

    if estimated_cost > budget + 0.000001:
        raise SafetyHalt(
            "قيمة الأمر تتجاوز الميزانية المسموحة."
        )

    candle_id = make_candle_id(
        candidate.get(
            "signal_timestamp",
            candidate.get("timestamp", utc_now())
        )
    )

    client_order_id = create_client_order_id(
        symbol,
        candle_id
    )

    if order_intent_exists(client_order_id):
        raise SafetyHalt(
            "تم تسجيل نية لهذا العقد والشمعة سابقاً."
        )

    # تسجيل النية وحفظها قبل إرسال POST
    register_order_intent(
        client_order_id=client_order_id,
        symbol=symbol,
        quantity=quantity,
        side="buy",
        limit_price=limit_price,
        signal=signal
    )

    STATE["active_client_order_id"] = client_order_id

    save_state()

    payload = {
        "symbol": symbol,
        "qty": str(quantity),
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{limit_price:.2f}",
        "client_order_id": client_order_id
    }

    log.info(
        "SUBMITTING BUY | %s | Qty=%s | "
        "Limit=$%.2f | EstimatedCost=$%.2f | "
        "ClientID=%s",
        symbol,
        quantity,
        limit_price,
        estimated_cost,
        client_order_id
    )

    try:

        response = trading_api(
            "POST",
            "/v2/orders",
            payload=payload
        )

        if not response.get("id"):
            raise APIError(
                "استجابة إنشاء الأمر لا تحتوي معرفاً."
            )

        apply_broker_order_update(
            response
        )

        ledger_event(
            "ORDER_SUBMITTED",
            {
                "client_order_id": client_order_id,
                "broker_order_id": response.get("id")
            }
        )

        log.info(
            "ORDER ACCEPTED | %s | %s",
            symbol,
            response.get("status")
        )

        return response

    except APIError as exc:

        # قد يكون الخطأ رفضاً مؤكداً أو انقطاعاً
        # لا نعيد POST في الحالتين.

        update_order_record(
            client_order_id,
            status="SUBMISSION_UNRESOLVED",
            submission_unknown=True,
            last_error=str(exc)
        )

        log.error(
            "ORDER SUBMISSION UNCERTAIN | %s | %s",
            client_order_id,
            exc
        )

        found = lookup_order_after_submit(
            client_order_id
        )

        if found:

            apply_broker_order_update(
                found
            )

            log.info(
                "ORDER FOUND AFTER LOOKUP | %s",
                client_order_id
            )

            return found

        halt_bot(
            "نتيجة إرسال الأمر غير محسومة. "
            f"ClientID={client_order_id}"
        )

        raise SafetyHalt(
            "لم نتمكن من تأكيد حالة أمر الشراء. "
            "لن تتم إعادة إرساله."
        )

    except Exception as exc:

        update_order_record(
            client_order_id,
            status="SUBMISSION_UNRESOLVED",
            submission_unknown=True,
            last_error=str(exc)
        )

        halt_bot(
            f"خطأ غير متوقع أثناء إرسال الأمر: {exc}"
        )

        raise SafetyHalt(
            "تم إيقاف التداول لحماية الحساب."
        )


# =========================================================
# 32) ORDER POLLING
# =========================================================

def refresh_order_status(client_order_id):

    record = LEDGER["orders"].get(
        client_order_id
    )

    if not record:
        raise SafetyHalt(
            "لا يوجد سجل للأمر المطلوب."
        )

    try:

        if record.get("broker_order_id"):

            order = get_order_by_id(
                record["broker_order_id"]
            )

        else:

            order = get_order_by_client_id(
                client_order_id
            )

        if not order:
            return None

        normalized = apply_broker_order_update(
            order
        )

        log.info(
            "ORDER STATUS | %s | %s | "
            "Filled=%s/%s | AvgPrice=%s",
            client_order_id,
            normalized["status"],
            normalized["filled_qty"],
            normalized["qty"],
            normalized["filled_avg_price"]
        )

        return normalized

    except Exception as exc:

        log.error(
            "ORDER STATUS CHECK FAILED | %s | %s",
            client_order_id,
            exc
        )

        return None


# =========================================================
# 33) CANCEL ORDER SAFELY
# =========================================================

def cancel_order_safely(client_order_id):

    record = LEDGER["orders"].get(
        client_order_id
    )

    if not record:
        raise SafetyHalt(
            "محاولة إلغاء أمر غير مسجل."
        )

    if record.get("terminal"):
        return True

    order_id = record.get("broker_order_id")

    if not order_id:
        order = get_order_by_client_id(
            client_order_id
        )

        if not order:
            raise SafetyHalt(
                "تعذر تحديد الأمر قبل الإلغاء."
            )

        order_id = order.get("id")

    try:

        trading_api(
            "DELETE",
            f"/v2/orders/{order_id}"
        )

        ledger_event(
            "CANCEL_REQUESTED",
            {
                "client_order_id": client_order_id,
                "broker_order_id": order_id
            }
        )

        log.info(
            "CANCEL REQUESTED | %s",
            client_order_id
        )

        return True

    except APIError as exc:

        # قد يكون الأمر نُفذ أثناء محاولة الإلغاء
        log.warning(
            "CANCEL RESULT UNCERTAIN | %s | %s",
            client_order_id,
            exc
        )

        refresh_order_status(
            client_order_id
        )

        return False


# =========================================================
# 34) FILL ACTIVITY RETRIEVAL
# =========================================================

def fetch_fill_activities(
    after=None,
    page_token=None,
    max_pages=10
):

    activities = []

    params = {
        "activity_types": "FILL",
        "direction": "asc",
        "page_size": 100
    }

    if after:
        params["after"] = after

    if page_token:
        params["page_token"] = page_token

    for _ in range(max_pages):

        result = trading_api(
            "GET",
            "/v2/account/activities",
            params=params
        )

        if not isinstance(result, list):
            raise APIError(
                "بيانات أنشطة التنفيذ غير صالحة."
            )

        if not result:
            break

        activities.extend(result)

        if len(result) < 100:
            break

        last = result[-1]

        next_id = last.get("id")

        if not next_id:
            break

        params["page_token"] = next_id

    return activities


# =========================================================
# 35) FILL LEDGER
# =========================================================

def apply_fill_activity(activity):

    activity_id = str(
        activity.get("id", "")
    )

    if not activity_id:
        return False

    if activity_id in LEDGER["fills"]:
        return False

    order_id = activity.get("order_id")

    client_order_id = activity.get(
        "client_order_id"
    )

    if not client_order_id:

        for cid, record in LEDGER["orders"].items():

            if record.get("broker_order_id") == order_id:
                client_order_id = cid
                break

    if not client_order_id:
        log.warning(
            "FILL WITHOUT LOCAL ORDER | %s",
            activity_id
        )

        return False

    record = LEDGER["orders"].get(
        client_order_id
    )

    if record is None:
        log.warning(
            "FILL ORDER NOT IN LEDGER | %s",
            client_order_id
        )

        return False

    qty = safe_float(
        activity.get("qty")
    )

    price = safe_float(
        activity.get("price")
    )

    if qty is None or price is None:
        raise SafetyHalt(
            "بيانات التنفيذ غير مكتملة."
        )

    if qty <= 0 or price <= 0:
        raise SafetyHalt(
            "بيانات التنفيذ تحتوي كمية أو سعراً غير صالح."
        )

    side = str(
        activity.get("side", "")
    ).lower()

    if side not in ("buy", "sell"):
        raise SafetyHalt(
            "نوع تنفيذ غير معروف."
        )

    fill_record = {
        "activity_id": activity_id,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "symbol": activity.get("symbol"),
        "qty": qty,
        "price": price,
        "side": side,
        "timestamp": activity.get("transaction_time"),
        "recorded_at": utc_now()
    }

    LEDGER["fills"][activity_id] = fill_record

    save_ledger()

    ledger_event(
        "FILL_RECORDED",
        fill_record
    )

    return True


# =========================================================
# 36) FILL RECONCILIATION
# =========================================================

def reconcile_order_fills(client_order_id):

    record = LEDGER["orders"].get(
        client_order_id
    )

    if not record:
        raise SafetyHalt(
            "لا يوجد أمر لمزامنة تنفيذاته."
        )

    broker_filled = float(
        record.get("filled_qty", 0) or 0
    )

    if broker_filled <= 0:
        return True

    # لا نقارن مباشرة مع Activities القديمة.
    # نبحث أولاً عن التنفيذات الحديثة ثم نعيد المحاولة.

    for attempt in range(
        FILL_SYNC_RETRIES
    ):

        try:

            after = record.get(
                "submitted_at"
            )

            activities = fetch_fill_activities(
                after=after,
                max_pages=10
            )

            for activity in activities:

                if (
                    activity.get("order_id")
                    == record.get("broker_order_id")
                ):
                    apply_fill_activity(
                        activity
                    )

            recorded_qty = 0.0

            for fill in LEDGER["fills"].values():

                if (
                    fill.get("client_order_id")
                    == client_order_id
                    and fill.get("side") == "buy"
                ):
                    recorded_qty += float(
                        fill.get("qty", 0)
                    )

            difference = abs(
                broker_filled - recorded_qty
            )

            if difference <= 0.000001:

                log.info(
                    "FILL RECONCILED | %s | Qty=%.4f",
                    client_order_id,
                    recorded_qty
                )

                return True

            log.warning(
                "FILL SYNC PENDING | %s | "
                "Broker=%.4f | Activities=%.4f | "
                "Attempt=%s/%s",
                client_order_id,
                broker_filled,
                recorded_qty,
                attempt + 1,
                FILL_SYNC_RETRIES
            )

        except Exception as exc:

            log.warning(
                "FILL ACTIVITY RETRY | %s | %s",
                client_order_id,
                exc
            )

        if attempt + 1 < FILL_SYNC_RETRIES:
            time.sleep(
                FILL_SYNC_GRACE_SECONDS
            )

    # لا ندخل صفقة جديدة ما دام التنفيذ غير محسوم.
    # لكن لا نعلن اختلافاً نهائياً لمجرد تأخر Activities.
    record["fill_sync_pending"] = True

    record["last_fill_sync_attempt"] = utc_now()

    save_ledger()

    log.warning(
        "FILL SYNC STILL PENDING | %s | "
        "Trading remains blocked until reconciliation.",
        client_order_id
    )

    return False


# =========================================================
# 37) GLOBAL FILL SYNC
# =========================================================

def sync_all_pending_fills():

    pending = []

    for client_id, record in LEDGER["orders"].items():

        if float(
            record.get("filled_qty", 0) or 0
        ) <= 0:
            continue

        if record.get("fill_sync_pending"):
            pending.append(client_id)

        elif not record.get("fill_sync_checked"):
            pending.append(client_id)

    all_ok = True

    for client_id in pending:

        try:

            result = reconcile_order_fills(
                client_id
            )

            if result:

                record = LEDGER["orders"][client_id]

                record["fill_sync_pending"] = False
                record["fill_sync_checked"] = True

                save_ledger()

            else:
                all_ok = False

        except Exception as exc:

            all_ok = False

            log.error(
                "GLOBAL FILL SYNC ERROR | %s | %s",
                client_id,
                exc
            )

    return all_ok


# =========================================================
# 38) RECONCILE ALL ORDERS
# =========================================================


def reconcile_all_orders():

    log.info("بدء المصالحة الدورية للأوامر.")

    try:
        broker_orders = get_orders(
            status="all",
            limit=500
        )

    except Exception as exc:
        log.error("تعذر جلب أوامر الوسيط: %s", exc)
        return False

    known_ids = set(LEDGER["orders"].keys())

    active_statuses = {
        "new",
        "accepted",
        "pending_new",
        "partially_filled",
        "pending_cancel",
        "pending_replace",
        "held",
        "stopped",
        "calculated",
    }

    for broker_order in broker_orders:

        client_id = broker_order.get("client_order_id")
        status = str(
            broker_order.get("status", "")
        ).lower()

        if not client_id:
            continue

        if client_id not in known_ids:

            # الأمر المجهول المكتمل أو الملغي لا يوقف البوت
            if status not in active_statuses:
                log.info(
                    "تجاهل أمر تاريخي غير نشط وغير مسجل محلياً | %s | %s",
                    client_id,
                    status
                )
                continue

            # الأمر المجهول النشط يظل سبباً لإيقاف التداول
            log.critical(
                "UNKNOWN ACTIVE BROKER ORDER | %s | %s",
                client_id,
                status
            )

            halt_bot(
                f"أمر نشط غير معروف لدى الوسيط: {client_id}"
            )

            return False

        apply_broker_order_update(broker_order)

    # فحص الأوامر المحلية ذات نتيجة الإرسال غير المحسومة
    for client_id, record in LEDGER["orders"].items():

        if (
            record.get("submission_unknown")
            and not record.get("broker_order_id")
        ):

            found = lookup_order_after_submit(client_id)

            if found:
                apply_broker_order_update(found)

            else:
                log.warning(
                    "UNRESOLVED ORDER | %s",
                    client_id
                )
                return False

    fills_ok = sync_all_pending_fills()

    if not fills_ok:
        log.warning(
            "المصالحة لم تكتمل: توجد تنفيذات قيد المزامنة."
        )
        return False

    STATE["last_reconciliation"] = utc_now()

    save_state()

    log.info("اكتملت المصالحة الدورية للأوامر.")

    return True


# =========================================================
# 39) ACTIVE POSITION HELPERS
# =========================================================

def get_option_positions():

    positions = get_positions()

    result = []

    for position in positions:

        symbol = str(
            position.get("symbol", "")
        )

        asset_class = str(
            position.get("asset_class", "")
        ).lower()

        if (
            asset_class == "us_option"
            or symbol.startswith("SPY")
        ):

            result.append(position)

    return result


def position_quantity(position):

    qty = safe_float(
        position.get("qty")
    )

    return qty or 0.0


def reconcile_positions():

    log.info(
        "بدء مصالحة المراكز."
    )

    broker_positions = get_option_positions()

    broker_map = {}

    for position in broker_positions:

        symbol = position.get("symbol")

        if not symbol:
            continue

        qty = position_quantity(position)

        if abs(qty) > 0:
            broker_map[symbol] = {
                "qty": qty,
                "avg_entry_price": safe_float(
                    position.get("avg_entry_price")
                ),
                "market_value": safe_float(
                    position.get("market_value")
                )
            }

    known_symbols = set()

    for record in LEDGER["orders"].values():

        if (
            float(record.get("filled_qty", 0) or 0) > 0
            and record.get("symbol")
        ):
            known_symbols.add(
                record["symbol"]
            )

    for symbol, broker_position in broker_map.items():

        if symbol not in known_symbols:

            log.critical(
                "UNKNOWN OPTION POSITION | %s | Qty=%s",
                symbol,
                broker_position["qty"]
            )

            halt_bot(
                f"مركز خيارات غير معروف: {symbol}"
            )

            return False

    STATE["last_known_positions"] = broker_map

    save_state()

    log.info(
        "POSITION RECONCILIATION OK | Positions=%s",
        len(broker_map)
    )

    return True


# =========================================================
# 40) ENTRY COUNTER
# =========================================================

def count_filled_entries_today():

    current_day = today_ny()

    count = 0

    for record in LEDGER["orders"].values():

        if record.get("side") != "buy":
            continue

        submitted_at = record.get(
            "created_at"
        )

        if not submitted_at:
            continue

        parsed = parse_market_timestamp(
            submitted_at
        )

        if not parsed:
            continue

        if parsed.astimezone(NY).date().isoformat() != current_day:
            continue

        if float(
            record.get("filled_qty", 0) or 0
        ) > 0:
            count += 1

    STATE["trades_today"] = count

    save_state()

    return count


def can_open_new_trade():

    ensure_not_halted()

    if not market_is_open():
        reject(
            "MARKET",
            "السوق مغلق."
        )

        return False

    if not entry_time_allowed():
        reject(
            "TIME",
            "خارج نافذة الدخول."
        )

        return False

    if active_entry_intent_exists():
        reject(
            "ORDER",
            "هناك أمر دخول غير محسوم."
        )

        return False

    if not reconcile_all_orders():
        reject(
            "RECONCILIATION",
            "تعذر إكمال مصالحة الأوامر."
        )

        return False

    if not reconcile_positions():
        reject(
            "POSITIONS",
            "تعذر إكمال مصالحة المراكز."
        )

        return False

    positions = get_option_positions()

    if len(positions) >= MAX_OPEN_POSITIONS:
        reject(
            "POSITION_LIMIT",
            "تم بلوغ الحد الأقصى للمراكز المفتوحة."
        )

        return False

    trades = count_filled_entries_today()

    if trades >= MAX_TRADES_PER_DAY:
        reject(
            "TRADE_LIMIT",
            "تم بلوغ الحد اليومي للصفقات المنفذة."
        )

        return False

    try:
        check_daily_risk()

    except SafetyHalt:
        raise

    return True


# =========================================================
# 41) PROTECTION PRICE HELPERS
# =========================================================

def calculate_stop_price(entry_price):

    return round(
        entry_price * (1 - STOP_LOSS_PERCENT),
        2
    )


def calculate_target_price(entry_price):

    return round(
        entry_price * (1 + TAKE_PROFIT_PERCENT),
        2
    )


def calculate_trailing_stop(
    entry_price,
    highest_price
):

    activation_price = (
        entry_price
        * (1 + TRAIL_ACTIVATION_PERCENT)
    )

    if highest_price < activation_price:
        return None

    trail_price = (
        highest_price
        * (1 - TRAIL_DISTANCE_PERCENT)
    )

    return round(
        max(
            trail_price,
            entry_price
        ),
        2
    )


def initialize_position_metadata(
    symbol,
    entry_price,
    quantity
):

    STATE["position_meta"][symbol] = {
        "entry_price": float(entry_price),
        "quantity": float(quantity),
        "highest_price": float(entry_price),
        "stop_price": calculate_stop_price(
            entry_price
        ),
        "target_price": calculate_target_price(
            entry_price
        ),
        "created_at": utc_now(),
        "last_update": utc_now()
    }

    save_state()


def get_position_metadata(symbol):

    return STATE.get(
        "position_meta", {}
    ).get(symbol)


# =========================================================
# 42) POSITION MONITORING DATA
# =========================================================

def get_option_latest_quote(symbol):

    snapshots = get_option_snapshots([
        symbol
    ])

    snapshot = snapshots.get(symbol)

    if not isinstance(snapshot, dict):
        return None

    quote = extract_quote(snapshot)

    fresh, age, reason = quote_freshness(
        quote
    )

    if not fresh:
        log.warning(
            "POSITION QUOTE NOT FRESH | %s | %s",
            symbol,
            reason
        )

        return None

    bid = quote["bid"]
    ask = quote["ask"]

    if bid is None or ask is None:
        return None

    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    return {
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
        "age": age
    }


# =========================================================
# 43) EXIT ORDER INTENT
# =========================================================

def submit_exit_limit_order(
    symbol,
    quantity,
    limit_price
):

    ensure_not_halted()

    if quantity <= 0 or limit_price <= 0:
        raise APIError(
            "قيم أمر الإغلاق غير صالحة."
        )

    client_id = (
        "SF113-EXIT-"
        + sha256_text(
            f"{symbol}|{quantity}|{utc_now()}"
        )[:16]
    )

    register_order_intent(
        client_order_id=client_id,
        symbol=symbol,
        quantity=quantity,
        side="sell",
        limit_price=limit_price,
        signal="POSITION_EXIT"
    )

    payload = {
        "symbol": symbol,
        "qty": str(quantity),
        "side": "sell",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{limit_price:.2f}",
        "client_order_id": client_id
    }

    try:

        response = trading_api(
            "POST",
            "/v2/orders",
            payload=payload
        )

        if not response.get("id"):
            raise APIError(
                "لم يتم تأكيد أمر الإغلاق."
            )

        apply_broker_order_update(
            response
        )

        ledger_event(
            "EXIT_ORDER_SUBMITTED",
            {
                "client_order_id": client_id,
                "symbol": symbol,
                "quantity": quantity,
                "limit_price": limit_price
            }
        )

        log.info(
            "EXIT ORDER ACCEPTED | %s | Qty=%s | Price=%.2f",
            symbol,
            quantity,
            limit_price
        )

        return response

    except Exception as exc:

        update_order_record(
            client_id,
            status="SUBMISSION_UNRESOLVED",
            submission_unknown=True,
            last_error=str(exc)
        )

        halt_bot(
            f"نتيجة أمر الإغلاق غير محسومة: {symbol}"
        )

        raise SafetyHalt(
            f"تعذر تأكيد أمر الإغلاق: {exc}"
        )
        # =========================================================
# 44) FINAL FILL RECONCILIATION PATCH
# =========================================================

def reconcile_order_fills(client_order_id):

    record = LEDGER["orders"].get(
        client_order_id
    )

    if not record:
        raise SafetyHalt(
            "سجل الأمر غير موجود."
        )

    broker_filled = float(
        record.get("filled_qty", 0) or 0
    )

    if broker_filled <= 0:
        return True

    for attempt in range(FILL_SYNC_RETRIES):

        try:

            activities = fetch_fill_activities(
                after=record.get("submitted_at"),
                max_pages=10
            )

            for activity in activities:

                if (
                    activity.get("order_id")
                    == record.get("broker_order_id")
                ):
                    apply_fill_activity(activity)

            recorded_qty = 0.0

            for fill in LEDGER["fills"].values():

                if (
                    fill.get("client_order_id")
                    == client_order_id
                ):
                    recorded_qty += float(
                        fill.get("qty", 0) or 0
                    )

            if abs(
                broker_filled - recorded_qty
            ) <= 0.000001:

                record["fill_sync_pending"] = False
                record["fill_sync_checked"] = True

                save_ledger()

                log.info(
                    "FILL RECONCILED | %s | %s | Qty=%.4f",
                    client_order_id,
                    record.get("side"),
                    recorded_qty
                )

                return True

            log.warning(
                "FILL SYNC PENDING | %s | "
                "Broker=%.4f | Activities=%.4f | "
                "Attempt=%s/%s",
                client_order_id,
                broker_filled,
                recorded_qty,
                attempt + 1,
                FILL_SYNC_RETRIES
            )

        except Exception as exc:

            log.warning(
                "FILL SYNC RETRY | %s | %s",
                client_order_id,
                exc
            )

        if attempt + 1 < FILL_SYNC_RETRIES:
            time.sleep(
                FILL_SYNC_GRACE_SECONDS
            )

    record["fill_sync_pending"] = True
    record["last_fill_sync_attempt"] = utc_now()

    save_ledger()

    return False


# =========================================================
# 45) POSITION ACCOUNTING
# =========================================================

def get_local_net_quantity(symbol):

    quantity = 0.0

    for fill in LEDGER["fills"].values():

        if fill.get("symbol") != symbol:
            continue

        qty = float(
            fill.get("qty", 0) or 0
        )

        if fill.get("side") == "buy":
            quantity += qty

        elif fill.get("side") == "sell":
            quantity -= qty

    return quantity


def get_local_average_entry(symbol):

    buy_qty = 0.0
    buy_value = 0.0

    for fill in LEDGER["fills"].values():

        if (
            fill.get("symbol") == symbol
            and fill.get("side") == "buy"
        ):

            qty = float(fill.get("qty", 0) or 0)
            price = float(fill.get("price", 0) or 0)

            buy_qty += qty
            buy_value += qty * price

    if buy_qty <= 0:
        return None

    return buy_value / buy_qty


def update_position_metadata_from_broker():

    positions = get_option_positions()

    broker_map = {}

    for position in positions:

        symbol = position.get("symbol")

        if not symbol:
            continue

        qty = position_quantity(position)

        if qty <= 0:
            continue

        avg = safe_float(
            position.get("avg_entry_price")
        )

        if avg is None or avg <= 0:
            raise SafetyHalt(
                f"سعر دخول المركز غير صالح: {symbol}"
            )

        broker_map[symbol] = {
            "qty": qty,
            "avg_entry_price": avg
        }

        metadata = STATE["position_meta"].get(
            symbol
        )

        if metadata is None:

            initialize_position_metadata(
                symbol,
                avg,
                qty
            )

        else:

            metadata["quantity"] = qty
            metadata["entry_price"] = avg
            metadata["last_update"] = utc_now()

    # لا نحذف بيانات المركز إلا بعد التأكد من عدم وجوده
    for symbol in list(
        STATE["position_meta"].keys()
    ):

        if symbol not in broker_map:

            STATE["position_meta"].pop(
                symbol,
                None
            )

    save_state()

    return broker_map


# =========================================================
# 46) OPEN EXIT ORDER CHECK
# =========================================================

def has_open_sell_order(symbol):

    for record in LEDGER["orders"].values():

        if (
            record.get("symbol") != symbol
            or record.get("side") != "sell"
        ):
            continue

        if record.get("submission_unknown"):
            return True

        if not record.get("terminal", False):
            return True

    return False


def get_open_sell_quantity(symbol):

    total = 0.0

    for record in LEDGER["orders"].values():

        if (
            record.get("symbol") != symbol
            or record.get("side") != "sell"
        ):
            continue

        if record.get("terminal", False):
            continue

        original = float(
            record.get("quantity", 0) or 0
        )

        filled = float(
            record.get("filled_qty", 0) or 0
        )

        total += max(
            0.0,
            original - filled
        )

    return total


# =========================================================
# 47) POSITION EXIT ENGINE
# =========================================================

def request_position_exit(
    symbol,
    quantity,
    reason
):

    if quantity <= 0:
        return None

    if has_open_sell_order(symbol):

        log.info(
            "EXIT SKIPPED | Existing sell order | %s",
            symbol
        )

        return None

    broker_positions = get_option_positions()

    position = next(
        (
            p for p in broker_positions
            if p.get("symbol") == symbol
        ),
        None
    )

    if position is None:
        log.warning(
            "EXIT SKIPPED | Position no longer exists | %s",
            symbol
        )

        return None

    broker_qty = position_quantity(position)

    if broker_qty <= 0:
        raise SafetyHalt(
            f"كمية المركز غير صالحة للإغلاق: {symbol}"
        )

    # لا نبيع أكثر من الكمية الفعلية
    quantity = min(
        quantity,
        broker_qty
    )

    quote = get_option_latest_quote(
        symbol
    )

    if quote is None:
        raise APIError(
            f"لا توجد أسعار حديثة للإغلاق: {symbol}"
        )

    bid = quote["bid"]

    # سعر حد قريب من Bid لتفضيل التنفيذ
    limit_price = round(
        max(0.01, bid),
        2
    )

    log.warning(
        "POSITION EXIT | %s | Qty=%s | "
        "Bid=%.2f | Reason=%s",
        symbol,
        quantity,
        bid,
        reason
    )

    response = submit_exit_limit_order(
        symbol,
        quantity,
        limit_price
    )

    ledger_event(
        "POSITION_EXIT_REASON",
        {
            "symbol": symbol,
            "quantity": quantity,
            "reason": reason
        }
    )

    return response


# =========================================================
# 48) UPDATE TRAILING POSITION
# =========================================================

def update_position_high(symbol, current_price):

    metadata = get_position_metadata(
        symbol
    )

    if metadata is None:
        return None

    previous_high = float(
        metadata.get(
            "highest_price",
            metadata["entry_price"]
        )
    )

    if current_price > previous_high:

        metadata["highest_price"] = current_price
        metadata["last_update"] = utc_now()

        save_state()

    return metadata


# =========================================================
# 49) POSITION MANAGEMENT
# =========================================================

def manage_single_position(position):

    symbol = position.get("symbol")

    if not symbol:
        return

    qty = position_quantity(position)

    if qty <= 0:
        return

    metadata = get_position_metadata(
        symbol
    )

    if metadata is None:

        log.warning(
            "POSITION METADATA MISSING | %s | "
            "Rebuilding from broker.",
            symbol
        )

        avg = safe_float(
            position.get("avg_entry_price")
        )

        if avg is None or avg <= 0:
            raise SafetyHalt(
                f"تعذر معرفة سعر دخول {symbol}"
            )

        initialize_position_metadata(
            symbol,
            avg,
            qty
        )

        metadata = get_position_metadata(
            symbol
        )

    if has_open_sell_order(symbol):

        log.info(
            "POSITION MONITOR | %s | "
            "Exit order already active.",
            symbol
        )

        return

    quote = get_option_latest_quote(
        symbol
    )

    if quote is None:

        log.warning(
            "POSITION MONITOR | %s | "
            "Quote unavailable; keeping position unchanged.",
            symbol
        )

        return

    # نعتمد Bid لتقييم الخروج المحافظ
    current_price = quote["bid"]

    entry_price = float(
        metadata["entry_price"]
    )

    if entry_price <= 0:
        raise SafetyHalt(
            "سعر الدخول غير صالح."
        )

    metadata = update_position_high(
        symbol,
        current_price
    )

    highest_price = float(
        metadata["highest_price"]
    )

    pnl_percent = (
        current_price - entry_price
    ) / entry_price

    stop_price = calculate_stop_price(
        entry_price
    )

    target_price = calculate_target_price(
        entry_price
    )

    trailing_price = calculate_trailing_stop(
        entry_price,
        highest_price
    )

    log.info(
        "POSITION | %s | Entry=%.2f | "
        "Bid=%.2f | PnL=%.2f%% | "
        "Stop=%.2f | Target=%.2f | Trail=%s",
        symbol,
        entry_price,
        current_price,
        pnl_percent * 100,
        stop_price,
        target_price,
        (
            f"{trailing_price:.2f}"
            if trailing_price is not None
            else "OFF"
        )
    )

    if force_exit_time_reached():

        request_position_exit(
            symbol,
            qty,
            "END_OF_DAY"
        )

        return

    if current_price <= stop_price:

        request_position_exit(
            symbol,
            qty,
            "STOP_LOSS"
        )

        return

    if current_price >= target_price:

        request_position_exit(
            symbol,
            qty,
            "TAKE_PROFIT"
        )

        return

    if (
        trailing_price is not None
        and current_price <= trailing_price
    ):

        request_position_exit(
            symbol,
            qty,
            "TRAILING_STOP"
        )

        return


# =========================================================
# 50) ALL POSITIONS MONITOR
# =========================================================

def manage_all_positions():

    positions = get_option_positions()

    update_position_metadata_from_broker()

    for position in positions:

        try:

            manage_single_position(
                position
            )

        except SafetyHalt:
            raise

        except Exception as exc:

            log.exception(
                "POSITION MANAGEMENT ERROR | %s | %s",
                position.get("symbol"),
                exc
            )

            # لا نرسل أوامر إضافية إذا فشلت الحماية
            raise SafetyHalt(
                f"خطأ في إدارة المركز: {position.get('symbol')}"
            )


# =========================================================
# 51) ENTRY SCAN ENGINE
# =========================================================

def scan_for_entry():

    ensure_not_halted()

    log.info(
        "================ ENTRY SCAN ================"
    )

    if not can_open_new_trade():
        return False

    bars = get_spy_bars(
        limit=120
    )

    indicators = calculate_market_indicators(
        bars
    )

    signal = get_market_signal(
        indicators
    )

    log.info(
        "SPY %.2f | Signal=%s | Score=%s | "
        "Bull=%s | Bear=%s | RSI=%.1f | "
        "EMA9=%.2f | EMA21=%.2f | "
        "VWAP=%.2f | ATR=%.3f | VolumeRatio=%.2f",
        indicators["price"],
        signal["direction"],
        signal["score"],
        signal["bullish_score"],
        signal["bearish_score"],
        indicators["rsi"],
        indicators["fast_ema"],
        indicators["slow_ema"],
        indicators["vwap"],
        indicators["atr"],
        indicators["volume_ratio"]
    )

    if signal["direction"] == "NEUTRAL":

        reject(
            "SIGNAL",
            "الإشارة محايدة."
        )

        return False

    if signal["score"] < MIN_SIGNAL_SCORE:

        reject(
            "SIGNAL",
            "قوة الإشارة أقل من الحد المطلوب."
        )

        return False

    candle_id = make_candle_id(
        indicators["timestamp"]
    )

    if STATE.get("last_scan_candle") == candle_id:

        reject(
            "DUPLICATE_CANDLE",
            "تم فحص شمعة الإشارة سابقاً."
        )

        return False

    # نحفظ الشمعة حتى لا تتكرر محاولة الدخول عليها
    STATE["last_scan_candle"] = candle_id

    save_state()

    option_type = (
        "call"
        if signal["direction"] == "CALL"
        else "put"
    )

    candidate = select_option_contract(
        option_type
    )

    if candidate is None:

        reject(
            "CONTRACT_SELECTION",
            "لم يوجد عقد يجتاز شروط السوق والميزانية."
        )

        return False

    candidate["signal_timestamp"] = (
        indicators["timestamp"]
    )

    candidate["signal"] = signal["direction"]

    # إعادة التحقق من السعر والدلتا قبل الإرسال
    candidate = refresh_selected_contract(
        candidate
    )

    # فحص أخير للمخاطر والمراكز
    if not can_open_new_trade():
        return False

    response = submit_limit_order_once(
        candidate,
        signal["direction"]
    )

    if response:

        log.info(
            "ENTRY ORDER SENT | %s | "
            "Direction=%s | Score=%s | "
            "OrderID=%s | Status=%s",
            candidate["symbol"],
            signal["direction"],
            signal["score"],
            response.get("id"),
            response.get("status")
        )

        return True

    return False


# =========================================================
# 52) PENDING ORDER MANAGEMENT
# =========================================================

def manage_pending_orders():

    for client_id, record in list(
        LEDGER["orders"].items()
    ):

        if record.get("terminal", False):
            continue

        if record.get("submission_unknown"):
            log.warning(
                "PENDING ORDER UNKNOWN | %s",
                client_id
            )

            continue

        order = refresh_order_status(
            client_id
        )

        if order is None:
            continue

        if order["status"] == "partially_filled":

            log.warning(
                "PARTIAL FILL | %s | "
                "Filled=%.4f/%.4f",
                client_id,
                order["filled_qty"],
                order["qty"]
            )

            # لا نلغي الجزء المنفذ.
            # يبقى المركز الفعلي خاضعاً للمراقبة.

            reconcile_order_fills(
                client_id
            )

        elif order["status"] == "filled":

            reconcile_order_fills(
                client_id
            )

            if record.get("side") == "buy":

                log.info(
                    "BUY FILLED | %s | Qty=%.4f | Avg=%s",
                    client_id,
                    order["filled_qty"],
                    order["filled_avg_price"]
                )

        elif order["status"] in (
            "canceled",
            "expired",
            "rejected"
        ):

            log.info(
                "ORDER TERMINAL | %s | Status=%s | "
                "Filled=%.4f",
                client_id,
                order["status"],
                order["filled_qty"]
            )

            if record.get("side") == "buy":

                STATE["active_client_order_id"] = None
                save_state()

                if order["filled_qty"] > 0:

                    reconcile_order_fills(
                        client_id
                    )


# =========================================================
# 53) ACTIVE ORDER STATE REFRESH
# =========================================================

def refresh_active_order_state():

    active = None

    for client_id, record in LEDGER["orders"].items():

        if (
            record.get("side") == "buy"
            and not record.get("terminal", False)
        ):

            active = client_id
            break

    STATE["active_client_order_id"] = active

    save_state()


# =========================================================
# 54) TELEGRAM STATUS (OPTIONAL)
# =========================================================

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
).strip()


def send_telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        response = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=10
        )

        response.raise_for_status()

        return True

    except Exception as exc:

        log.warning(
            "TELEGRAM ERROR | %s",
            exc
        )

        return False


def status_message():

    account = get_account()

    equity = safe_float(
        account.get("equity")
    ) or 0.0

    cash = safe_float(
        account.get("cash")
    ) or 0.0

    positions = get_option_positions()

    trades = count_filled_entries_today()

    message = (
        f"صَفاء التحليل v{VERSION}\n"
        f"الحساب: {account.get('status')}\n"
        f"Equity: ${equity:.2f}\n"
        f"Cash: ${cash:.2f}\n"
        f"صفقات منفذة اليوم: {trades}\n"
        f"المراكز المفتوحة: {len(positions)}\n"
        f"حالة البوت: "
        f"{'متوقف' if STATE.get('halted') else 'يعمل'}\n"
        f"السبب: {STATE.get('halt_reason') or 'لا يوجد'}"
    )

    return message


# =========================================================
# 55) STARTUP RECONCILIATION
# =========================================================

def startup_reconciliation():

    log.info(
        "بدء فحوص ما قبل التشغيل."
    )

    account = get_account()

    log.info(
        "ACCOUNT | Status=%s | "
        "Equity=%s | BuyingPower=%s | "
        "Paper=%s",
        account.get("status"),
        account.get("equity"),
        account.get("buying_power"),
        "YES" if "paper" in TRADING_URL.lower() else "NO"
    )

    if account.get("status") != "ACTIVE":
        raise SafetyHalt(
            "الحساب غير نشط."
        )

    if not reconcile_all_orders():
        raise SafetyHalt(
            "فشلت مصالحة الأوامر عند بدء التشغيل."
        )

    if not reconcile_positions():
        raise SafetyHalt(
            "فشلت مصالحة المراكز عند بدء التشغيل."
        )

    update_position_metadata_from_broker()

    refresh_active_order_state()

    log.info(
        "اكتملت فحوص ما قبل التشغيل."
    )


# =========================================================
# 56) MAIN LOOP
# =========================================================

def run_bot():

    log.info(
        "================================================="
    )

    log.info(
        "صَفاء التحليل v%s بدأ التشغيل.",
        VERSION
    )

    log.info(
        "Underlying=%s | PaperRequired=%s | "
        "EntryWindow=%s-%s | Scan=%ss",
        UNDERLYING,
        PAPER_REQUIRED,
        ENTRY_START,
        ENTRY_END,
        SCAN_SECONDS
    )

    acquire_instance_lock()

    initialize_storage()

    startup_reconciliation()

    send_telegram(
        status_message()
    )

    last_reconciliation_time = 0

    while True:

        try:

            ensure_not_halted()

            refresh_daily_baseline()

            now = time.time()

            # -------------------------------------
            # 1. متابعة الأوامر
            # -------------------------------------

            manage_pending_orders()

            refresh_active_order_state()

            # -------------------------------------
            # 2. مصالحة دورية
            # -------------------------------------

            if (
                now - last_reconciliation_time
                >= RECONCILIATION_INTERVAL
            ):

                reconcile_all_orders()

                reconcile_positions()

                last_reconciliation_time = now

            # -------------------------------------
            # 3. إدارة المراكز المفتوحة
            # -------------------------------------

            manage_all_positions()

            # -------------------------------------
            # 4. الإغلاق قبل نهاية الجلسة
            # -------------------------------------

            if force_exit_time_reached():

                log.info(
                    "وصل وقت الإغلاق الإجباري."
                )

                time.sleep(SCAN_SECONDS)

                continue

            # -------------------------------------
            # 5. البحث عن دخول جديد
            # -------------------------------------

            if entry_time_allowed():

                scan_for_entry()

            else:

                diagnostic(
                    "TIME",
                    "خارج نافذة الدخول. NY=%s",
                    ny_now().strftime("%H:%M:%S")
                )

            time.sleep(
                SCAN_SECONDS
            )

        except SafetyHalt as exc:

            halt_bot(str(exc))

            send_telegram(
                "🚨 صَفاء التحليل توقف لأسباب السلامة:\n"
                + str(exc)
            )

            log.critical(
                "SAFETY HALT: %s",
                exc
            )

            break

        except MarketDataUnavailable as exc:

            log.warning(
                "MARKET DATA TEMPORARILY UNAVAILABLE: %s",
                exc
            )

            time.sleep(
                SCAN_SECONDS
            )

        except APIError as exc:

            log.error(
                "API ERROR: %s",
                exc
            )

            time.sleep(
                SCAN_SECONDS
            )

        except KeyboardInterrupt:

            log.warning(
                "تم إيقاف البوت يدوياً."
            )

            break

        except Exception as exc:

            log.exception(
                "UNEXPECTED MAIN LOOP ERROR"
            )

            halt_bot(
                f"خطأ غير متوقع: {exc}"
            )

            send_telegram(
                "🚨 توقف البوت بسبب خطأ غير متوقع:\n"
                + str(exc)
            )

            break

    log.warning(
        "انتهى تشغيل صَفاء التحليل."
    )


# =========================================================
# 57) ENTRY POINT
# =========================================================

if __name__ == "__main__":

    try:

        run_bot()

    except Exception as exc:

        log.exception(
            "STARTUP FAILED: %s",
            exc
        )

        sys.exit(1)