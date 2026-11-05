# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v112.0
SPY 0-DTE | PAPER FIRST | FAIL-CLOSED EXECUTION
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
from decimal import Decimal, ROUND_DOWN

import requests


# =========================================================
# 1) CONFIGURATION
# =========================================================

VERSION = "112.0"

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

BASE_DIR = Path(os.getenv("BOT_DATA_DIR", "."))
BASE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = BASE_DIR / "safaa_v112_state.json"
LEDGER_FILE = BASE_DIR / "safaa_v112_ledger.json"
INIT_FILE = BASE_DIR / "safaa_v112_initialized.flag"
LOCK_FILE = BASE_DIR / "safaa_v112.lock"

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

# Risk controls
RISK_PER_TRADE = 0.02
MAX_DAILY_DRAWDOWN = 0.03
MAX_PREMIUM_FRACTION = 0.02
MAX_OPEN_POSITIONS = 1
MAX_TRADES_PER_DAY = 4
MAX_CONSECUTIVE_LOSSES = 2

# Contract selection
TARGET_DELTA_MIN = 0.40
TARGET_DELTA_MAX = 0.65
MAX_SPREAD_PERCENT = 0.12
MIN_OPTION_ASK = 0.20
MAX_OPTION_ASK = 8.00
MIN_DAYS_TO_EXPIRY = 0
MAX_DAYS_TO_EXPIRY = 0

# Position management
STOP_LOSS_PERCENT = 0.30
TAKE_PROFIT_PERCENT = 0.60
TRAIL_ACTIVATION_PERCENT = 0.35
TRAIL_DISTANCE_PERCENT = 0.18

# Trading hours (New York)
ENTRY_START = (9, 45)
ENTRY_END = (14, 0)
FORCE_EXIT_TIME = (15, 40)

SCAN_SECONDS = 20
REQUEST_TIMEOUT = 15
MAX_API_RETRIES = 3

# Execution safety
MAX_ENTRY_SLIPPAGE_PERCENT = 0.08
ORDER_LOOKUP_WAIT_SECONDS = 3
RECONCILIATION_INTERVAL = 20

# Strategy
FAST_EMA = 9
SLOW_EMA = 21
RSI_PERIOD = 14
VOLUME_LOOKBACK = 20
MIN_SIGNAL_SCORE = 3

# Never run multiple Railway replicas against the same account.
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
            BASE_DIR / "safaa_v112.log",
            encoding="utf-8"
        )
    ]
)

log = logging.getLogger("SAFAA_V112")


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
        "تم طلب Live دون تأكيد مستقل. "
        "أوقف التشغيل حتى تضبط CONFIRM_LIVE_TRADING=true."
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
        "شغّل نسخة واحدة فقط من البوت على الحساب نفسه."
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


def api_request(
    method,
    url,
    params=None,
    payload=None,
    timeout=REQUEST_TIMEOUT
):
    """
    لا يعيد إرسال POST تلقائياً.
    إعادة POST بعد نتيجة غامضة قد تنشئ أمراً مكرراً.
    """

    method = method.upper()

    if method == "POST":
        attempts = 1
    else:
        attempts = MAX_API_RETRIES

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
                    "ممنوع إعادة إرسال الأمر تلقائياً. "
                    f"التفاصيل: {exc}"
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
# 5) ATOMIC FILE STORAGE
# =========================================================

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
            "تعذر استخدام قفل النظام. "
            "لا يمكن ضمان تشغيل نسخة واحدة."
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
            "هناك نسخة أخرى من البوت تعمل على هذا المسار."
        )

    _INSTANCE_LOCK_HANDLE.seek(0)
    _INSTANCE_LOCK_HANDLE.truncate()
    _INSTANCE_LOCK_HANDLE.write(str(os.getpid()))
    _INSTANCE_LOCK_HANDLE.flush()


# =========================================================
# 7) DEFAULT STATE
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
        "last_known_orders": {}
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

    log.critical("TRADING HALTED: %s", reason)


def ensure_not_halted():
    if STATE.get("halted"):
        raise SafetyHalt(
            STATE.get("halt_reason") or "البوت متوقف لأسباب السلامة."
        )


# =========================================================
# 8) INITIALIZATION
# =========================================================

def initialize_storage():
    global STATE, LEDGER

    if INIT_FILE.exists():
        if not STATE_FILE.exists():
            raise SafetyHalt(
                "ملف الحالة مفقود بعد التهيئة السابقة. "
                "لن يتم إنشاء حالة جديدة تلقائياً."
            )

        if not LEDGER_FILE.exists():
            raise SafetyHalt(
                "دفتر الصفقات مفقود بعد التهيئة السابقة. "
                "لن يتم إنشاء دفتر فارغ."
            )

        try:
            STATE = read_json(STATE_FILE)
            LEDGER = read_json(LEDGER_FILE)
        except Exception as exc:
            raise SafetyHalt(
                f"تعذر قراءة ملفات الحالة: {exc}"
            )

        if not isinstance(STATE, dict):
            raise SafetyHalt("ملف الحالة غير صالح.")

        if not isinstance(LEDGER, dict):
            raise SafetyHalt("دفتر الصفقات غير صالح.")

        if "orders" not in LEDGER or "fills" not in LEDGER:
            raise SafetyHalt("بنية دفتر الصفقات غير مكتملة.")

        log.info("تم تحميل الحالة والدفتر السابقين.")
        return

    # First-ever initialization only.
    account = trading_api("GET", "/v2/account")

    if account.get("status") != "ACTIVE":
        raise SafetyHalt(
            f"حالة حساب Alpaca ليست ACTIVE: {account.get('status')}"
        )

    STATE = default_state()
    LEDGER = default_ledger()

    STATE["starting_equity"] = float(
        account.get("equity", 0) or 0
    )

    # Create data files before marker.
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
# 9) ACCOUNT HELPERS
# =========================================================

def get_account():
    return trading_api("GET", "/v2/account")


def get_account_equity():
    account = get_account()
    return float(account.get("equity", 0) or 0)


def get_clock():
    return trading_api("GET", "/v2/clock")


def market_is_open():
    clock = get_clock()
    return bool(clock.get("is_open", False))


def refresh_daily_baseline():
    current_day = today_ny()

    if STATE.get("trading_day") == current_day:
        return

    # Do not reset day while any bot order is unresolved.
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

    max_loss = start_equity * MAX_DAILY_DRAWDOWN

    if pnl <= -max_loss:
        halt_bot(
            f"تم بلوغ حد الخسارة اليومية: "
            f"{pnl:.2f} USD"
        )
        raise SafetyHalt("تجاوز حد الخسارة اليومية.")

    if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
        halt_bot("تم بلوغ حد الخسائر المتتالية.")
        raise SafetyHalt("توقف بعد خسائر متتالية.")


# =========================================================
# 10) MARKET DATA — SMART RETRY & FAIL-SOFT
# =========================================================

MIN_SPY_BARS = 40
SPY_DATA_RETRIES = 3
SPY_RETRY_DELAY = 2.5
SPY_MAX_BAR_AGE_SECONDS = 180


class MarketDataUnavailable(Exception):
    """نقص مؤقت في بيانات السوق، وليس عطلاً يستوجب إيقاف البوت."""
    pass


def get_spy_bars(limit=120):
    """
    جلب شموع SPY مع إعادة المحاولة.
    لا يتم إيقاف البوت عند نقص البيانات.
    لا يتم تخفيض الحد الأدنى للشموع.
    """

    last_error = None

    for attempt in range(1, SPY_DATA_RETRIES + 1):

        try:
            params = {
                "timeframe": "1Min",
                "limit": limit,
                "adjustment": "raw",
                "feed": os.getenv(
                    "ALPACA_STOCK_FEED", "iex"
                ),
                "sort": "asc"
            }

            result = data_api(
                "GET",
                f"/v2/stocks/{UNDERLYING}/bars",
                params=params
            )

            bars = result.get("bars", [])

            if not isinstance(bars, list):
                raise MarketDataUnavailable(
                    "استجابة شموع SPY ليست قائمة."
                )

            normalized = []

            for bar in bars:
                try:
                    timestamp = datetime.fromisoformat(
                        bar["t"].replace("Z", "+00:00")
                    )

                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=UTC)

                    normalized.append({
                        "timestamp": bar["t"],
                        "_datetime": timestamp,
                        "open": float(bar["o"]),
                        "high": float(bar["h"]),
                        "low": float(bar["l"]),
                        "close": float(bar["c"]),
                        "volume": float(bar["v"])
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

            # استبعاد الشمعة الحالية غير المكتملة.
            now = datetime.now(UTC)

            completed = []

            for bar in normalized:

                candle_end = (
                    bar["_datetime"]
                    + timedelta(minutes=1)
                )

                if candle_end <= now:
                    completed.append(bar)

            # التحقق من الأسعار.
            valid = []

            for bar in completed:

                if (
                    bar["open"] <= 0
                    or bar["high"] <= 0
                    or bar["low"] <= 0
                    or bar["close"] <= 0
                ):
                    continue

                if bar["high"] < bar["low"]:
                    continue

                if bar["volume"] < 0:
                    continue

                valid.append(bar)

            if len(valid) < MIN_SPY_BARS:

                raise MarketDataUnavailable(
                    f"شموع SPY غير كافية: "
                    f"{len(valid)}/{MIN_SPY_BARS}"
                )

            # التأكد من حداثة آخر شمعة.
            last_bar = valid[-1]

            age = (
                now - last_bar["_datetime"]
            ).total_seconds()

            if age > SPY_MAX_BAR_AGE_SECONDS:

                raise MarketDataUnavailable(
                    f"بيانات SPY قديمة: "
                    f"{age:.0f} ثانية."
                )

            # إزالة الحقل الداخلي.
            for bar in valid:
                bar.pop("_datetime", None)

            log.info(
                "SPY DATA OK | Candles=%s | Last=%s | Attempt=%s",
                len(valid),
                valid[-1]["timestamp"],
                attempt
            )

            return valid

        except MarketDataUnavailable as exc:

            last_error = exc

            log.warning(
                "SPY DATA RETRY %s/%s: %s",
                attempt,
                SPY_DATA_RETRIES,
                exc
            )

        except APIError as exc:

            last_error = exc

            log.warning(
                "SPY API RETRY %s/%s: %s",
                attempt,
                SPY_DATA_RETRIES,
                exc
            )

        except Exception as exc:

            last_error = exc

            log.exception(
                "خطأ أثناء جلب بيانات SPY."
            )

        if attempt < SPY_DATA_RETRIES:
            time.sleep(
                SPY_RETRY_DELAY * attempt
            )

    raise MarketDataUnavailable(
        f"تعذر الحصول على بيانات SPY الكافية "
        f"بعد {SPY_DATA_RETRIES} محاولات. "
        f"آخر خطأ: {last_error}"
    )

# =========================================================
# 11) TECHNICAL INDICATORS
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
        change = values[index] - values[index - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for index in range(period, len(gains)):
        avg_gain = (
            (avg_gain * (period - 1))
            + gains[index]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + losses[index]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100.0 - (100.0 / (1.0 + rs))


def calculate_vwap(bars):
    cumulative_pv = 0.0
    cumulative_volume = 0.0

    for bar in bars:
        typical_price = (
            bar["high"]
            + bar["low"]
            + bar["close"]
        ) / 3.0

        volume = max(bar["volume"], 0)

        cumulative_pv += typical_price * volume
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
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        )

        ranges.append(true_range)

    return average(ranges[-period:])


def calculate_market_indicators(bars):
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]

    fast = ema(closes, FAST_EMA)
    slow = ema(closes, SLOW_EMA)
    rsi = calculate_rsi(closes)
    vwap = calculate_vwap(bars)
    atr = calculate_atr(bars)

    if None in (fast, slow, rsi, vwap, atr):
        raise APIError("تعذر حساب المؤشرات الفنية.")

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
# 12) MARKET SIGNAL
# =========================================================

def get_market_signal(indicators):
    """
    نظام نقاط بسيط.
    لا يعتبر الإشارة ضماناً لاتجاه السوق.
    """

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
    elif 32 <= rsi <= 50:
        bearish_score += 1

    if volume_ratio >= 1.10:
        if price > indicators["previous_close"]:
            bullish_score += 1
        elif price < indicators["previous_close"]:
            bearish_score += 1

    if bullish_score >= MIN_SIGNAL_SCORE and (
        bullish_score > bearish_score
    ):
        direction = "CALL"
        score = bullish_score

    elif bearish_score >= MIN_SIGNAL_SCORE and (
        bearish_score > bullish_score
    ):
        direction = "PUT"
        score = bearish_score

    else:
        direction = "NEUTRAL"
        score = max(bullish_score, bearish_score)

    return {
        "direction": direction,
        "score": score,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score
    }


# =========================================================
# 13) CANDLE IDENTIFICATION
# =========================================================

def make_candle_id(timestamp):
    """
    معرف ثابت للشمعة لمنع الدخول مرتين على الشمعة نفسها.
    """

    return sha256_text(
        f"{UNDERLYING}|{timestamp}"
    )[:24]


def entry_time_allowed():
    now = ny_now()

    if now.weekday() >= 5:
        return False

    current_minutes = now.hour * 60 + now.minute

    start_minutes = (
        ENTRY_START[0] * 60 + ENTRY_START[1]
    )

    end_minutes = (
        ENTRY_END[0] * 60 + ENTRY_END[1]
    )

    return (
        start_minutes <= current_minutes <= end_minutes
    )


def force_exit_time_reached():
    now = ny_now()

    current_minutes = now.hour * 60 + now.minute

    exit_minutes = (
        FORCE_EXIT_TIME[0] * 60
        + FORCE_EXIT_TIME[1]
    )

    return current_minutes >= exit_minutes


# =========================================================
# 14) OPTION CONTRACT DISCOVERY
# =========================================================

def get_option_contracts(option_type):
    """
    جلب عقود SPY النشطة المنتهية اليوم فقط.
    نستخدم endpoint العقود بدلاً من تخمين رموز الخيارات.
    """

    expiry = today_ny()

    params = {
        "underlying_symbols": UNDERLYING,
        "expiration_date": expiry,
        "type": option_type.lower(),
        "status": "active",
        "limit": 1000
    }

    all_contracts = []
    page_token = None

    for _ in range(5):
        request_params = dict(params)

        if page_token:
            request_params["page_token"] = page_token

        result = trading_api(
            "GET",
            "/v2/options/contracts",
            params=request_params
        )

        contracts = result.get("option_contracts", [])

        all_contracts.extend(contracts)

        page_token = result.get("next_page_token")

        if not page_token:
            break

    valid = []

    for contract in all_contracts:
        try:
            if contract.get("underlying_symbol") != UNDERLYING:
                continue

            if contract.get("type", "").lower() != option_type.lower():
                continue

            if contract.get("expiration_date") != expiry:
                continue

            if contract.get("tradable") is False:
                continue

            symbol = contract.get("symbol")

            strike = float(contract.get("strike_price"))

            if not symbol or strike <= 0:
                continue

            valid.append({
                "symbol": symbol,
                "strike": strike,
                "type": option_type.lower(),
                "expiry": expiry
            })

        except (TypeError, ValueError):
            continue

    return valid


# =========================================================
# 15) OPTION SNAPSHOTS
# =========================================================

def get_option_snapshots(symbols):
    """
    طلب بيانات العقود على دفعات.
    إذا فشل الطلب، لا نعتبر ذلك عدم وجود عقود.
    """

    snapshots = {}

    unique_symbols = list(dict.fromkeys(symbols))

    for start in range(0, len(unique_symbols), 50):
        batch = unique_symbols[start:start + 50]

        if not batch:
            continue

                result = options_data_api(
    "GET",
    "/v1beta1/options/snapshots",
    params={
        "symbols": ",".join(batch)
    }
)

batch_snapshots = result.get("snapshots", {})

if not isinstance(batch_snapshots, dict):
    raise APIError(
        "استجابة بيانات الخيارات غير صالحة."
    )

snapshots.update(batch_snapshots)

return snapshots

def extract_snapshot_values(snapshot):
    quote = snapshot.get("latestQuote") or {}
    trade = snapshot.get("latestTrade") or {}
    greeks = snapshot.get("greeks") or {}

    bid = float(quote.get("bp") or 0)
    ask = float(quote.get("ap") or 0)

    delta_value = greeks.get("delta")

    try:
        delta = float(delta_value)
    except (TypeError, ValueError):
        delta = None

    last_price = float(trade.get("p") or 0)

    return {
        "bid": bid,
        "ask": ask,
        "delta": delta,
        "last": last_price
    }


# =========================================================
# 16) CONTRACT SELECTION
# =========================================================

def select_option_contract(direction, underlying_price):
    if direction not in ("CALL", "PUT"):
        return None

    option_type = (
        "call" if direction == "CALL" else "put"
    )

    contracts = get_option_contracts(option_type)

    if not contracts:
        log.warning(
            "لا توجد عقود %s منتهية اليوم.",
            direction
        )
        return None

    symbols = [
        contract["symbol"]
        for contract in contracts
    ]

    snapshots = get_option_snapshots(symbols)

    candidates = []

    for contract in contracts:
        symbol = contract["symbol"]

        snapshot = snapshots.get(symbol)

        if not snapshot:
            continue

        try:
            values = extract_snapshot_values(snapshot)

            bid = values["bid"]
            ask = values["ask"]
            delta = values["delta"]

            if bid <= 0 or ask <= 0:
                continue

            if ask < MIN_OPTION_ASK:
                continue

            if ask > MAX_OPTION_ASK:
                continue

            if ask <= bid:
                continue

            midpoint = (bid + ask) / 2.0

            if midpoint <= 0:
                continue

            spread_percent = (
                (ask - bid) / midpoint
            )

            if spread_percent > MAX_SPREAD_PERCENT:
                continue

            if delta is None:
                continue

            absolute_delta = abs(delta)

            if not (
                TARGET_DELTA_MIN
                <= absolute_delta
                <= TARGET_DELTA_MAX
            ):
                continue

            strike_distance = abs(
                contract["strike"] - underlying_price
            )

            target_delta = 0.52

            delta_distance = abs(
                absolute_delta - target_delta
            )

            score = (
                delta_distance * 3.0
                + spread_percent * 2.0
                + (strike_distance / max(underlying_price, 1)) * 2.0
            )

            candidates.append({
                **contract,
                "bid": bid,
                "ask": ask,
                "midpoint": midpoint,
                "delta": delta,
                "spread_percent": spread_percent,
                "score": score
            })

        except (TypeError, ValueError, KeyError):
            continue

    if not candidates:
        log.info(
            "لم نجد عقداً يطابق شروط السيولة والدلتا."
        )
        return None

    candidates.sort(
        key=lambda item: item["score"]
    )

    selected = candidates[0]

    log.info(
        "العقد المختار: %s | Bid %.2f | Ask %.2f "
        "| Delta %.3f | Spread %.1f%%",
        selected["symbol"],
        selected["bid"],
        selected["ask"],
        selected["delta"],
        selected["spread_percent"] * 100
    )

    return selected


# =========================================================
# 17) ORDER STATUS HELPERS
# =========================================================

TERMINAL_ORDER_STATUSES = {
    "filled",
    "canceled",
    "expired",
    "rejected",
    "replaced"
}

ACTIVE_ORDER_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "partially_filled",
    "pending_cancel",
    "pending_replace",
    "accepted_for_bidding",
    "stopped",
    "suspended"
}


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


def lookup_order_safely(client_order_id):
    """
    البحث عن الأمر بعد نتيجة POST غامضة.
    لا ننشئ أمراً جديداً إذا تعذر إثبات مصير الأمر السابق.
    """

    try:
        return get_order_by_client_id(client_order_id)

    except APIError as exc:
        message = str(exc).lower()

        # 404 فقط قد تعني أن الأمر غير موجود.
        if "404" in message or "not found" in message:
            return None

        raise SafetyHalt(
            "تعذر التحقق من الأمر السابق. "
            "يجب إيقاف التداول بدلاً من إعادة إرساله."
        )


# =========================================================
# 18) ORDER INTENT LEDGER
# =========================================================

def create_order_intent(
    client_order_id,
    symbol,
    side,
    qty,
    candle_id,
    order_role
):
    if client_order_id in LEDGER["orders"]:
        raise SafetyHalt(
            "محاولة إنشاء intent بمعرف مستخدم مسبقاً."
        )

    intent = {
        "client_order_id": client_order_id,
        "broker_order_id": None,
        "symbol": symbol,
        "side": side,
        "qty": int(qty),
        "order_role": order_role,
        "candle_id": candle_id,
        "status": "INTENT",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "filled_qty_reported": 0,
        "filled_qty_ledger": 0,
        "average_fill_price_reported": None,
        "last_broker_status": None
    }

    LEDGER["orders"][client_order_id] = intent

    save_ledger()

    return intent


def update_order_record(client_order_id, **updates):
    if client_order_id not in LEDGER["orders"]:
        raise SafetyHalt(
            f"الأمر غير موجود في الدفتر: {client_order_id}"
        )

    record = LEDGER["orders"][client_order_id]

    record.update(updates)
    record["updated_at"] = utc_now()

    save_ledger()


def record_event(event_type, details):
    LEDGER["events"].append({
        "id": make_id("EVT"),
        "type": event_type,
        "details": details,
        "timestamp": utc_now()
    })

    # الاحتفاظ بآخر 2000 حدث.
    LEDGER["events"] = LEDGER["events"][-2000:]

    save_ledger()


# =========================================================
# 19) SUBMIT ORDER ONCE
# =========================================================

def submit_order_once(
    symbol,
    side,
    qty,
    candle_id,
    order_role="ENTRY"
):
    """
    ينشئ intent محلياً قبل POST.
    لا يعيد إرسال POST عند انقطاع الاتصال.
    """

    ensure_not_halted()

    if int(qty) <= 0:
        raise ValueError("كمية الأمر يجب أن تكون أكبر من صفر.")

    if STATE.get("active_client_order_id"):
        raise SafetyHalt(
            "يوجد أمر سابق غير محسوم."
        )

    client_order_id = make_id("SAFAA112")

    # حفظ رابط الشمعة قبل إرسال الأمر.
    STATE["candle_orders"][candle_id] = client_order_id
    STATE["active_client_order_id"] = client_order_id
    save_state()

    create_order_intent(
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        qty=int(qty),
        candle_id=candle_id,
        order_role=order_role
    )

    payload = {
        "symbol": symbol,
        "qty": str(int(qty)),
        "side": side,
        "type": "limit",
        "time_in_force": "day",
        "limit_price": None,
        "client_order_id": client_order_id
    }

    # لا نرسل limit_price=None إلى Alpaca.
    payload.pop("limit_price")

    # السعر المحدد يضاف بواسطة الدالة التي تستدعي التنفيذ.
    raise RuntimeError(
        "استخدم submit_limit_order_once لإرسال أمر محدد السعر."
    )


def submit_limit_order_once(
    symbol,
    side,
    qty,
    limit_price,
    candle_id,
    order_role="ENTRY"
):
    ensure_not_halted()

    if STATE.get("active_client_order_id"):
        raise SafetyHalt(
            "هناك أمر نشط أو غير محسوم."
        )

    qty = int(qty)
    limit_price = round(float(limit_price), 2)

    if qty <= 0 or limit_price <= 0:
        raise ValueError("كمية أو سعر غير صالح.")

    client_order_id = make_id("SAFAA112")

    # حفظ الربط والـ intent قبل الاتصال بالوسيط.
    STATE["candle_orders"][candle_id] = client_order_id
    STATE["active_client_order_id"] = client_order_id
    save_state()

    create_order_intent(
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        qty=qty,
        candle_id=candle_id,
        order_role=order_role
    )

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{limit_price:.2f}",
        "client_order_id": client_order_id
    }

    try:
        result = trading_api(
            "POST",
            "/v2/orders",
            payload=payload
        )

        update_order_record(
            client_order_id,
            broker_order_id=result.get("id"),
            status="SUBMITTED",
            last_broker_status=result.get("status")
        )

        log.info(
            "تم إرسال الأمر مرة واحدة: %s | %s | %s | Qty=%s",
            client_order_id,
            symbol,
            side,
            qty
        )

        return result

    except Exception as exc:
        log.error(
            "نتيجة إرسال الأمر غير مؤكدة: %s",
            exc
        )

        # لا نعيد POST.
        # نحاول فقط الاستعلام عن client_order_id نفسه.
        time.sleep(ORDER_LOOKUP_WAIT_SECONDS)

        try:
            existing = lookup_order_safely(client_order_id)

            if existing:
                update_order_record(
                    client_order_id,
                    broker_order_id=existing.get("id"),
                    status="FOUND_AFTER_AMBIGUOUS_POST",
                    last_broker_status=existing.get("status")
                )

                return existing

        except Exception as lookup_exc:
            halt_bot(
                "تعذر حسم نتيجة POST أو العثور على الأمر: "
                f"{lookup_exc}"
            )

            raise SafetyHalt(
                "توقف البوت بسبب نتيجة أمر غير محسومة."
            )

        halt_bot(
            "تم حفظ intent لكن لم يمكن إثبات وجود الأمر "
            "أو عدم وجوده لدى الوسيط."
        )

        raise SafetyHalt(
            "لا تعِد تشغيل التداول قبل مراجعة الأمر في Alpaca."
        )


# =========================================================
# 20) FETCH BROKER FILL ACTIVITIES
# =========================================================

def fetch_fill_activities(max_pages=10):
    """
    يجلب تنفيذات الحساب من Activities.
    لا يعتمد على endpoint افتراضي لتفاصيل trades.
    """

    all_activities = []
    page_token = None

    for _ in range(max_pages):
        params = {
            "page_size": 100,
            "direction": "desc"
        }

        if page_token:
            params["page_token"] = page_token

        result = trading_api(
            "GET",
            "/v2/account/activities/FILL",
            params=params
        )

        if not isinstance(result, list):
            raise APIError(
                "استجابة Activities ليست قائمة."
            )

        if not result:
            break

        all_activities.extend(result)

        if len(result) < 100:
            break

        next_token = result[-1].get("id")

        if not next_token or next_token == page_token:
            break

        page_token = next_token

    return all_activities


# =========================================================
# 21) UNIQUE FILL RECORDING
# =========================================================

def record_fill_activity(activity):
    """
    كل activity.id يسجل مرة واحدة فقط.
    لا نستخدم filled_qty التراكمي كسجل fill جديد.
    """

    activity_id = str(activity.get("id") or "")

    if not activity_id:
        raise SafetyHalt(
            "تنفيذ من الوسيط دون activity id."
        )

    if activity_id in LEDGER["fills"]:
        return False

    order_id = str(activity.get("order_id") or "")

    matched_client_id = None

    for client_id, order in LEDGER["orders"].items():
        if str(order.get("broker_order_id") or "") == order_id:
            matched_client_id = client_id
            break

    if not matched_client_id:
        raise SafetyHalt(
            "وصل تنفيذ لا يمكن ربطه بأمر معروف. "
            f"Broker order id={order_id}"
        )

    try:
        qty = float(activity.get("qty"))
        price = float(activity.get("price"))
    except (TypeError, ValueError):
        raise SafetyHalt(
            "بيانات تنفيذ غير صالحة من الوسيط."
        )

    if qty <= 0 or price <= 0:
        raise SafetyHalt(
            "كمية أو سعر تنفيذ غير صالح."
        )

    side = str(activity.get("side") or "").lower()

    if side not in ("buy", "sell"):
        raise SafetyHalt(
            f"اتجاه تنفيذ غير معروف: {side}"
        )

    fill = {
        "activity_id": activity_id,
        "client_order_id": matched_client_id,
        "broker_order_id": order_id,
        "symbol": activity.get("symbol"),
        "side": side,
        "qty": qty,
        "price": price,
        "timestamp": activity.get("transaction_time")
        or activity.get("date")
        or utc_now()
    }

    LEDGER["fills"][activity_id] = fill

    order = LEDGER["orders"][matched_client_id]

    order["filled_qty_ledger"] = round(
        sum(
            float(item["qty"])
            for item in LEDGER["fills"].values()
            if item["client_order_id"] == matched_client_id
        ),
        6
    )

    order["updated_at"] = utc_now()

    save_ledger()

    log.info(
        "FILL مسجل | %s | %s | Qty %.4f | Price %.2f",
        matched_client_id,
        side,
        qty,
        price
    )

    return True


# =========================================================
# 22) SYNC FILLS FOR ONE ORDER
# =========================================================

def sync_order_fills(client_order_id):
    order = LEDGER["orders"].get(client_order_id)

    if not order:
        raise SafetyHalt(
            "لا يمكن مزامنة أمر غير موجود في الدفتر."
        )

    broker_order_id = order.get("broker_order_id")

    if not broker_order_id:
        raise SafetyHalt(
            "الأمر لا يحمل broker_order_id."
        )

    activities = fetch_fill_activities()

    matching = [
        activity
        for activity in activities
        if str(activity.get("order_id") or "")
        == str(broker_order_id)
    ]

    for activity in matching:
        record_fill_activity(activity)

    # قراءة حالة الأمر من المصدر الأساسي.
    broker_order = get_order_by_id(broker_order_id)

    reported_qty = float(
        broker_order.get("filled_qty") or 0
    )

    ledger_qty = float(
        LEDGER["orders"][client_order_id].get(
            "filled_qty_ledger", 0
        )
    )

    update_order_record(
        client_order_id,
        filled_qty_reported=reported_qty,
        average_fill_price_reported=(
            broker_order.get("filled_avg_price")
        ),
        last_broker_status=broker_order.get("status"),
        broker_order_id=broker_order_id
    )

    if abs(reported_qty - ledger_qty) > 0.0001:
        raise SafetyHalt(
            "عدم تطابق كمية التنفيذ بين Alpaca والدفتر: "
            f"broker={reported_qty}, ledger={ledger_qty}. "
            "قد تكون Activities لم تصل بعد."
        )

    return broker_order


# =========================================================
# 23) ORDER FINALIZATION
# =========================================================

def finalize_order_if_terminal(client_order_id, broker_order):
    status = str(
        broker_order.get("status") or ""
    ).lower()

    if status in ACTIVE_ORDER_STATUSES:
        update_order_record(
            client_order_id,
            status="ACTIVE",
            last_broker_status=status
        )
        return False

    if status not in TERMINAL_ORDER_STATUSES:
        raise SafetyHalt(
            f"حالة أمر غير معروفة: {status}"
        )

    # يجب مزامنة التنفيذات قبل فك القفل.
    sync_order_fills(client_order_id)

    order = LEDGER["orders"][client_order_id]

    reported_qty = float(
        order.get("filled_qty_reported") or 0
    )

    ledger_qty = float(
        order.get("filled_qty_ledger") or 0
    )

    if abs(reported_qty - ledger_qty) > 0.0001:
        raise SafetyHalt(
            "لا يمكن إنهاء الأمر مع اختلاف كمية التنفيذ."
        )

    update_order_record(
        client_order_id,
        status=status.upper(),
        last_broker_status=status
    )

    if STATE.get("active_client_order_id") == client_order_id:
        STATE["active_client_order_id"] = None
        save_state()

    record_event(
        "ORDER_FINALIZED",
        {
            "client_order_id": client_order_id,
            "status": status,
            "filled_qty": ledger_qty
        }
    )

    log.info(
        "اكتمل حسم الأمر: %s | status=%s | filled=%.4f",
        client_order_id,
        status,
        ledger_qty
    )

    return True


# =========================================================
# 24) WAIT AND MONITOR ORDER
# =========================================================

def monitor_order(client_order_id, max_wait=8):
    deadline = time.time() + max_wait

    while time.time() < deadline:
        ensure_not_halted()

        order = LEDGER["orders"].get(client_order_id)

        if not order:
            raise SafetyHalt(
                "الأمر اختفى من دفتر الصفقات."
            )

        broker_id = order.get("broker_order_id")

        if not broker_id:
            broker_order = lookup_order_safely(
                client_order_id
            )

            if not broker_order:
                halt_bot(
                    "لم يتم العثور على الأمر بعد إرساله."
                )
                raise SafetyHalt(
                    "الأمر غير موجود لدى الوسيط."
                )

            broker_id = broker_order.get("id")

            update_order_record(
                client_order_id,
                broker_order_id=broker_id
            )

        broker_order = sync_order_fills(
            client_order_id
        )

        status = str(
            broker_order.get("status") or ""
        ).lower()

        if status in TERMINAL_ORDER_STATUSES:
            finalize_order_if_terminal(
                client_order_id,
                broker_order
            )
            return broker_order

        time.sleep(1)

    log.warning(
        "الأمر ما زال نشطاً بعد انتهاء مدة الانتظار: %s",
        client_order_id
    )

    return get_order_by_id(
        LEDGER["orders"][client_order_id]["broker_order_id"]
    )


# =========================================================
# 25) CANCEL ORDER SAFELY
# =========================================================

def cancel_order_safely(client_order_id):
    order = LEDGER["orders"].get(client_order_id)

    if not order:
        raise SafetyHalt(
            "لا يمكن إلغاء أمر غير معروف."
        )

    broker_id = order.get("broker_order_id")

    if not broker_id:
        raise SafetyHalt(
            "لا يوجد broker_order_id لإلغاء الأمر."
        )

    # طلب الإلغاء لا يعني أن الإلغاء تم.
    try:
        trading_api(
            "DELETE",
            f"/v2/orders/{broker_id}"
        )

    except APIError as exc:
        log.warning(
            "طلب الإلغاء لم يؤكد النتيجة: %s",
            exc
        )

    # نتحقق من الحالة الفعلية بعد طلب الإلغاء.
    time.sleep(1)

    broker_order = get_order_by_id(broker_id)

    status = str(
        broker_order.get("status") or ""
    ).lower()

    if status in ACTIVE_ORDER_STATUSES:
        update_order_record(
            client_order_id,
            status="CANCEL_PENDING",
            last_broker_status=status
        )

        raise SafetyHalt(
            "الأمر لا يزال نشطاً أو الإلغاء قيد المعالجة. "
            "لن يتم إرسال أمر بديل."
        )

    sync_order_fills(client_order_id)

    finalize_order_if_terminal(
        client_order_id,
        broker_order
    )

    return broker_order


# =========================================================
# 26) LEDGER POSITION CALCULATION
# =========================================================

def calculate_ledger_positions():
    """
    حساب صافي العقود من التنفيذات الفعلية المسجلة.
    لا يعتمد على filled_qty التراكمي للأوامر.
    """

    positions = {}

    for fill in LEDGER["fills"].values():
        symbol = fill.get("symbol")

        if not symbol:
            raise SafetyHalt(
                "تنفيذ دون رمز عقد."
            )

        positions.setdefault(symbol, {
            "qty": 0.0,
            "buy_value": 0.0,
            "buy_qty": 0.0,
            "sell_qty": 0.0,
            "sell_value": 0.0
        })

        item = positions[symbol]

        qty = float(fill["qty"])
        price = float(fill["price"])

        if fill["side"] == "buy":
            item["qty"] += qty
            item["buy_qty"] += qty
            item["buy_value"] += qty * price

        elif fill["side"] == "sell":
            item["qty"] -= qty
            item["sell_qty"] += qty
            item["sell_value"] += qty * price

    result = {}

    for symbol, item in positions.items():
        net_qty = round(item["qty"], 6)

        if net_qty < -0.0001:
            raise SafetyHalt(
                f"تم اكتشاف مركز بيع غير مدعوم: {symbol}"
            )

        if net_qty <= 0.0001:
            continue

        if item["buy_qty"] <= 0:
            raise SafetyHalt(
                f"لا توجد عمليات شراء مسجلة للعقد: {symbol}"
            )

        average_entry = (
            item["buy_value"] / item["buy_qty"]
        )

        result[symbol] = {
            "qty": net_qty,
            "average_entry": average_entry,
            "buy_qty": item["buy_qty"],
            "sell_qty": item["sell_qty"]
        }

    return result


# =========================================================
# 27) BROKER POSITION RECONCILIATION
# =========================================================

def get_broker_option_positions():
    positions = trading_api(
        "GET",
        "/v2/positions"
    )

    if not isinstance(positions, list):
        raise APIError(
            "استجابة مراكز الحساب غير صالحة."
        )

    result = {}

    for position in positions:
        asset_class = str(
            position.get("asset_class") or ""
        ).lower()

        if asset_class not in (
            "us_option",
            "option"
        ):
            continue

        symbol = position.get("symbol")

        if not symbol:
            raise SafetyHalt(
                "مركز خيارات لدى الوسيط دون رمز."
            )

        try:
            qty = float(position.get("qty") or 0)
        except (TypeError, ValueError):
            raise SafetyHalt(
                f"كمية مركز غير صالحة: {symbol}"
            )

        if qty < 0:
            raise SafetyHalt(
                f"مركز خيارات سالب غير مدعوم: {symbol}"
            )

        if qty > 0:
            result[symbol] = {
                "qty": qty,
                "average_entry": float(
                    position.get("avg_entry_price") or 0
                ),
                "market_value": float(
                    position.get("market_value") or 0
                ),
                "unrealized_pl": float(
                    position.get("unrealized_pl") or 0
                )
            }

    return result


def reconcile_positions():
    ledger_positions = calculate_ledger_positions()
    broker_positions = get_broker_option_positions()

    all_symbols = (
        set(ledger_positions.keys())
        | set(broker_positions.keys())
    )

    mismatches = []

    for symbol in all_symbols:
        local_qty = float(
            ledger_positions.get(symbol, {}).get("qty", 0)
        )

        broker_qty = float(
            broker_positions.get(symbol, {}).get("qty", 0)
        )

        if abs(local_qty - broker_qty) > 0.0001:
            mismatches.append({
                "symbol": symbol,
                "ledger_qty": local_qty,
                "broker_qty": broker_qty
            })

    if mismatches:
        raise SafetyHalt(
            "اختلاف المراكز بين الدفتر وAlpaca: "
            + json.dumps(
                mismatches,
                ensure_ascii=False
            )
        )

    STATE["last_known_positions"] = broker_positions
    save_state()

    return ledger_positions


# =========================================================
# 28) ORDER RECONCILIATION
# =========================================================

def reconcile_all_orders():
    """
    فحص جميع intents والأوامر.
    لا يتم إرسال أي أمر جديد خلال هذه العملية.
    """

    unresolved = []

    active_ids = []

    for client_id, order in list(
        LEDGER["orders"].items()
    ):
        broker_id = order.get("broker_order_id")

        if not broker_id:
            broker_order = lookup_order_safely(
                client_id
            )

            if not broker_order:
                unresolved.append(client_id)
                continue

            broker_id = broker_order.get("id")

            update_order_record(
                client_id,
                broker_order_id=broker_id
            )

        try:
            broker_order = sync_order_fills(client_id)

        except Exception as exc:
            raise SafetyHalt(
                f"فشل التحقق من تنفيذ الأمر {client_id}: {exc}"
            )

        status = str(
            broker_order.get("status") or ""
        ).lower()

        if status in ACTIVE_ORDER_STATUSES:
            active_ids.append(client_id)

            update_order_record(
                client_id,
                status="ACTIVE",
                last_broker_status=status
            )

        elif status in TERMINAL_ORDER_STATUSES:
            finalize_order_if_terminal(
                client_id,
                broker_order
            )

        else:
            raise SafetyHalt(
                f"حالة أمر غير معروفة: {status}"
            )

    if unresolved:
        raise SafetyHalt(
            "هناك أوامر محلية لم يمكن العثور عليها لدى Alpaca: "
            + ", ".join(unresolved)
        )

    if len(active_ids) > 1:
        raise SafetyHalt(
            "تم العثور على أكثر من أمر نشط."
        )

    if active_ids:
        STATE["active_client_order_id"] = active_ids[0]
    else:
        STATE["active_client_order_id"] = None

    save_state()

    # لا ننتقل إلى التداول قبل مطابقة المراكز.
    reconcile_positions()

    STATE["last_reconciliation"] = utc_now()
    save_state()

    return True


# =========================================================
# 29) POSITION METADATA
# =========================================================

def ensure_position_metadata():
    if "position_meta" not in STATE:
        STATE["position_meta"] = {}

    save_state()


def get_position_metadata(symbol, average_entry):
    ensure_position_metadata()

    metadata = STATE["position_meta"].get(symbol)

    if not metadata:
        metadata = {
            "average_entry": average_entry,
            "highest_bid": average_entry,
            "opened_at": utc_now(),
            "last_exit_attempt": None
        }

        STATE["position_meta"][symbol] = metadata
        save_state()

    return metadata


# =========================================================
# 30) LIVE OPTION QUOTE
# =========================================================

def get_single_option_quote(symbol):
    snapshots = get_option_snapshots([symbol])

    snapshot = snapshots.get(symbol)

    if not snapshot:
        raise APIError(
            f"لا توجد بيانات سوق للعقد: {symbol}"
        )

    values = extract_snapshot_values(snapshot)

    bid = values["bid"]
    ask = values["ask"]

    if bid <= 0 or ask <= 0 or ask < bid:
        raise APIError(
            f"سعر عرض غير صالح للعقد: {symbol}"
        )

    return {
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2.0
    }


# =========================================================
# 31) CONTRACT QUANTITY / RISK
# =========================================================

def calculate_contract_quantity(ask_price):
    """
    الحد الأعلى للقيمة المدفوعة:
    الأصغر بين 2% من رأس المال و2% من رأس المال.
    """

    equity = get_account_equity()

    if equity <= 0 or ask_price <= 0:
        return 0

    risk_budget = equity * RISK_PER_TRADE

    premium_budget = (
        equity * MAX_PREMIUM_FRACTION
    )

    allowed_budget = min(
        risk_budget,
        premium_budget
    )

    contract_cost = ask_price * 100.0

    if contract_cost <= 0:
        return 0

    quantity = int(
        math.floor(
            allowed_budget / contract_cost
        )
    )

    return max(quantity, 0)


# =========================================================
# 32) ENTRY ORDER
# =========================================================

def open_new_position(
    candle_id,
    direction,
    contract
):
    ensure_not_halted()

    if STATE.get("active_client_order_id"):
        log.info("تجاهل الدخول: يوجد أمر غير محسوم.")
        return False

    if STATE.get("trades_today", 0) >= MAX_TRADES_PER_DAY:
        log.info("تم الوصول إلى الحد اليومي للصفقات.")
        return False

    current_positions = reconcile_positions()

    if len(current_positions) >= MAX_OPEN_POSITIONS:
        log.info("يوجد مركز مفتوح بالفعل.")
        return False

    if candle_id in STATE.get("candle_orders", {}):
        log.info("تمت معالجة هذه الشمعة مسبقاً.")
        return False

    quote = get_single_option_quote(
        contract["symbol"]
    )

    ask = quote["ask"]
    bid = quote["bid"]

    if ask <= 0 or bid <= 0:
        return False

    # أمر الدخول لا يتجاوز سعر العرض الحالي.
    limit_price = round(ask, 2)

    quantity = calculate_contract_quantity(
        limit_price
    )

    if quantity < 1:
        log.info(
            "رأس المال المتاح لا يسمح بشراء عقد واحد "
            "وفق حد المخاطرة."
        )
        return False

    estimated_premium = (
        quantity * limit_price * 100
    )

    equity = get_account_equity()

    if estimated_premium > equity * MAX_PREMIUM_FRACTION:
        raise SafetyHalt(
            "القيمة التقديرية للعقد تجاوزت حد رأس المال."
        )

    log.info(
        "محاولة دخول %s | %s | Qty=%s | Limit=%.2f",
        direction,
        contract["symbol"],
        quantity,
        limit_price
    )

    order = submit_limit_order_once(
        symbol=contract["symbol"],
        side="buy",
        qty=quantity,
        limit_price=limit_price,
        candle_id=candle_id,
        order_role="ENTRY"
    )

    client_id = order.get("client_order_id")

    if not client_id:
        raise SafetyHalt(
            "استجابة إنشاء الأمر لا تحتوي client_order_id."
        )

    STATE["trades_today"] += 1
    save_state()

    monitor_order(client_id)

    reconcile_all_orders()

    return True


# =========================================================
# 33) EXIT ORDER
# =========================================================

def close_position(symbol, quantity, bid, reason):
    ensure_not_halted()

    if STATE.get("active_client_order_id"):
        log.warning(
            "تأجيل الإغلاق لوجود أمر غير محسوم."
        )
        return False

    quantity = int(quantity)

    if quantity <= 0:
        return False

    if bid <= 0:
        log.error(
            "لا يوجد Bid صالح لإغلاق العقد %s.",
            symbol
        )
        return False

    # نضع الحد عند Bid الحالي.
    # عدم التنفيذ لا يعني أن المركز أغلق.
    limit_price = round(bid, 2)

    candle_id = sha256_text(
        f"EXIT|{symbol}|{today_ny()}|{reason}|"
        f"{int(time.time() // 60)}"
    )[:24]

    log.warning(
        "إرسال أمر إغلاق | %s | Qty=%s | Price=%.2f | %s",
        symbol,
        quantity,
        limit_price,
        reason
    )

    order = submit_limit_order_once(
        symbol=symbol,
        side="sell",
        qty=quantity,
        limit_price=limit_price,
        candle_id=candle_id,
        order_role="EXIT"
    )

    client_id = order.get("client_order_id")

    if not client_id:
        raise SafetyHalt(
            "أمر الإغلاق لا يحمل client_order_id."
        )

    monitor_order(client_id)

    reconcile_all_orders()

    remaining = reconcile_positions()

    if symbol not in remaining:
        STATE["position_meta"].pop(symbol, None)
        save_state()

        log.info(
            "تم تأكيد إغلاق المركز بالكامل: %s",
            symbol
        )

        return True

    log.warning(
        "ما زال هناك مركز مفتوح بعد محاولة الإغلاق: %s | Qty=%s",
        symbol,
        remaining[symbol]["qty"]
    )

    return False


# =========================================================
# 34) POSITION MANAGEMENT
# =========================================================

def manage_open_positions():
    ensure_not_halted()

    positions = reconcile_positions()

    if not positions:
        return

    for symbol, position in positions.items():
        if STATE.get("active_client_order_id"):
            return

        quantity = int(position["qty"])

        if quantity <= 0:
            continue

        average_entry = float(
            position["average_entry"]
        )

        quote = get_single_option_quote(symbol)

        bid = quote["bid"]

        if bid <= 0 or average_entry <= 0:
            continue

        metadata = get_position_metadata(
            symbol,
            average_entry
        )

        highest_bid = max(
            float(metadata.get("highest_bid", average_entry)),
            bid
        )

        metadata["highest_bid"] = highest_bid

        pnl_percent = (
            (bid - average_entry) / average_entry
        )

        trailing_stop = (
            highest_bid
            * (1.0 - TRAIL_DISTANCE_PERCENT)
        )

        stop_price = (
            average_entry
            * (1.0 - STOP_LOSS_PERCENT)
        )

        take_profit_price = (
            average_entry
            * (1.0 + TAKE_PROFIT_PERCENT)
        )

        reason = None

        if force_exit_time_reached():
            reason = "EOD_FORCE_EXIT"

        elif bid <= stop_price:
            reason = "STOP_LOSS"

        elif bid >= take_profit_price:
            reason = "TAKE_PROFIT"

        elif (
            highest_bid
            >= average_entry * (
                1.0 + TRAIL_ACTIVATION_PERCENT
            )
            and bid <= trailing_stop
        ):
            reason = "TRAILING_STOP"

        metadata["last_pnl_percent"] = pnl_percent
        metadata["last_bid"] = bid
        metadata["last_checked_at"] = utc_now()

        save_state()

        log.info(
            "مركز %s | Qty=%s | Entry=%.2f | Bid=%.2f "
            "| PnL=%.1f%%",
            symbol,
            quantity,
            average_entry,
            bid,
            pnl_percent * 100
        )

        if reason:
            close_position(
                symbol=symbol,
                quantity=quantity,
                bid=bid,
                reason=reason
            )

            # لا نحاول إغلاق مركز آخر قبل المصالحة.
            return


# =========================================================
# 35) MARKET SCAN — FAIL-SOFT DATA HANDLING
# =========================================================

def scan_for_entry():

    ensure_not_halted()

    if not market_is_open():
        log.info("السوق مغلق.")
        return

    if not entry_time_allowed():
        log.info("خارج نافذة الدخول.")
        return

    if STATE.get("active_client_order_id"):
        log.info("يوجد أمر غير محسوم؛ لا دخول جديد.")
        return

    check_daily_risk()

    positions = reconcile_positions()

    if positions:
        log.info("يوجد مركز مفتوح؛ لا دخول جديد.")
        return

    # جلب البيانات دون إيقاف البوت عند النقص.
    try:

        bars = get_spy_bars()

    except MarketDataUnavailable as exc:

        log.warning(
            "تأجيل تحليل الدخول بسبب نقص بيانات SPY: %s",
            exc
        )

        return

    except APIError as exc:

        log.warning(
            "تعذر الوصول إلى بيانات SPY مؤقتاً: %s",
            exc
        )

        return

    # لا نسمح بتحليل بيانات ناقصة.
    if not bars or len(bars) < MIN_SPY_BARS:

        log.warning(
            "تأجيل الدخول: عدد الشموع غير كافٍ."
        )

        return

    try:

        indicators = calculate_market_indicators(bars)

        signal = get_market_signal(indicators)

    except (APIError, ValueError, ZeroDivisionError) as exc:

        log.warning(
            "تعذر حساب المؤشرات؛ تخطي دورة الدخول: %s",
            exc
        )

        return

    candle_id = make_candle_id(
        indicators["timestamp"]
    )

    log.info(
        "SPY %.2f | Signal=%s | Score=%s "
        "| RSI=%.1f | EMA%d=%.2f | EMA%d=%.2f "
        "| VWAP=%.2f | ATR=%.3f | VolumeRatio=%.2f",
        indicators["price"],
        signal["direction"],
        signal["score"],
        indicators["rsi"],
        FAST_EMA,
        indicators["fast_ema"],
        SLOW_EMA,
        indicators["slow_ema"],
        indicators["vwap"],
        indicators["atr"],
        indicators["volume_ratio"]
    )

    if signal["direction"] == "NEUTRAL":
        return

    if candle_id in STATE.get("candle_orders", {}):
        log.info("هذه الشمعة تمت معالجتها مسبقاً.")
        return

    try:

        contract = select_option_contract(
            signal["direction"],
            indicators["price"]
        )

    except APIError as exc:

        log.warning(
            "تعذر جلب عقود الخيارات: %s",
            exc
        )

        return

    if not contract:
        return

    open_new_position(
        candle_id=candle_id,
        direction=signal["direction"],
        contract=contract
    )


# =========================================================
# 36) PERIODIC RECONCILIATION
# =========================================================

def reconciliation_due():
    last = STATE.get("last_reconciliation")

    if not last:
        return True

    try:
        last_dt = datetime.fromisoformat(last)

        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)

        elapsed = (
            datetime.now(UTC) - last_dt
        ).total_seconds()

        return elapsed >= RECONCILIATION_INTERVAL

    except Exception:
        return True


def run_reconciliation_if_due():
    if reconciliation_due():
        log.info("بدء المصالحة الدورية.")
        reconcile_all_orders()


# =========================================================
# 37) MAIN LOOP
# =========================================================

def run_bot():

    # تشغيل قفل النسخة الواحدة
    acquire_instance_lock()

    # تحميل الحالة ودفتر الصفقات
    initialize_storage()

    # تجهيز بيانات إدارة المراكز
    ensure_position_metadata()

    log.info("=" * 55)
    log.info("SAFAA ANALYSIS v%s STARTING", VERSION)
    log.info("PAPER MODE: %s", PAPER_REQUIRED)
    log.info("UNDERLYING: %s", UNDERLYING)
    log.info("=" * 55)

    # مصالحة إجبارية قبل السماح بأي تداول
    reconcile_all_orders()

    log.info("اكتملت مصالحة البداية. بدء حلقة التشغيل.")

    while True:

        try:

            ensure_not_halted()

            refresh_daily_baseline()

            check_daily_risk()

            # المصالحة الدورية
            run_reconciliation_if_due()

            # إدارة المراكز أولاً دائماً
            positions = reconcile_positions()

            if positions:

                try:
                    manage_open_positions()

                except APIError as exc:

                    log.error(
                        "تعذر تحديث بيانات المركز: %s",
                        exc
                    )

                    # لا نحاول فتح مركز جديد عند تعذر إدارة مركز قائم.

            elif market_is_open():

                try:
                    scan_for_entry()

                except MarketDataUnavailable as exc:

                    log.warning(
                        "تخطي الدخول مؤقتاً: %s",
                        exc
                    )

            time.sleep(SCAN_SECONDS)

        except SafetyHalt as exc:

            halt_bot(str(exc))

            log.critical(
                "توقف البوت لأسباب السلامة: %s",
                exc
            )

            break

        except KeyboardInterrupt:

            log.warning(
                "تم إيقاف البوت يدوياً."
            )

            break

        except Exception as exc:

            log.exception(
                "خطأ في دورة التشغيل: %s",
                exc
            )

            halt_bot(
                f"خطأ غير متوقع: {exc}"
            )

            break


# =========================================================
# 38) ENTRY POINT
# =========================================================

if __name__ == "__main__":

    try:
        run_bot()

    except SafetyHalt as exc:

        log.critical(
            "SAFETY HALT: %s",
            exc
        )

        sys.exit(2)

    except Exception as exc:

        log.exception(
            "FATAL ERROR: %s",
            exc
        )

        sys.exit(1)