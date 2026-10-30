# -*- coding: utf-8 -*-
"""
صَفاء التحليل v64.4 — Smart Entry Pro
- SPY Options: 0DTE إلى 3DTE
- Paper Trading افتراضيًا
- دخول انتقائي مع تسجيل أسباب الرفض
- لا يلغي أوامر المستخدم الأخرى
- لا يدير إلا الصفقات التي فتحها هذا البوت
- إغلاق الصفقات قبل نهاية جلسة نيويورك
"""

import os
import json
import time
import math
import logging
import uuid
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import numpy as np
import pandas as pd

from sklearn.ensemble import ExtraTreesClassifier, GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline


# =========================================================
# 1) الإعدادات العامة
# =========================================================

NAME = "صَفاء التحليل - Smart Entry Pro"
VERSION = "v64.4"

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

BASE_DIR = Path(".")
STATE_FILE = BASE_DIR / "safaa_v644_positions.json"
JOURNAL_FILE = BASE_DIR / "safaa_v644_journal.csv"

SYMBOL = "SPY"
BENCHMARK = "QQQ"

TIMEFRAME = "1Min"
HISTORY_DAYS = 90
LIVE_DAYS = 5

MIN_RAW_BARS = 1200
MIN_TRAIN_ROWS = 500
FORECAST_HORIZON = 5

# =========================================================
# 2) Alpaca — Paper افتراضيًا
# =========================================================

API_KEY = os.getenv("ALPACA_API_KEY", "").strip()
API_SECRET = os.getenv("ALPACA_API_SECRET", "").strip()

DATA_URL = os.getenv(
    "ALPACA_DATA_URL",
    "https://data.alpaca.markets"
).rstrip("/")

TRADING_URL = os.getenv(
    "ALPACA_TRADING_URL",
    "https://paper-api.alpaca.markets"
).rstrip("/")

STOCK_FEED = os.getenv("ALPACA_STOCK_FEED", "iex")
OPTION_FEED = os.getenv("ALPACA_OPTION_FEED", "indicative")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

# لا يبدأ البوت إذا لم توجد المفاتيح
if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "ضع ALPACA_API_KEY و ALPACA_API_SECRET في متغيرات البيئة."
    )


# =========================================================
# 3) إعدادات الدخول والخيارات
# =========================================================

POLL_SECONDS = 15
POSITION_POLL_SECONDS = 3
SCAN_SECONDS = 20

MIN_CONFIDENCE_THRESHOLD = 0.56
MIN_VOLUME_VELOCITY = 1.20
REQUIRED_CONFIRMATIONS = 2

MIN_DTE = 0
MAX_DTE = 3

MIN_DELTA = 0.40
MAX_DELTA = 0.65

MAX_SPREAD_PCT = 0.025
MIN_OPEN_INTEREST = 300
MAX_IMPLIED_VOLATILITY = 0.45

MAX_CONTRACTS_TO_CHECK = 25

# =========================================================
# 4) إدارة المخاطر
# =========================================================

MAX_DAILY_LOSS_PCT = 0.02
MAX_RISK_CAP = 0.02
RISK_PER_TRADE_PCT = 0.01

MAX_TRADES_PER_DAY = 6
MAX_CONSECUTIVE_LOSSES = 3

COOLDOWN_AFTER_LOSS_MIN = 15
COOLDOWN_AFTER_WIN_MIN = 3

STOP_LOSS_PCT = 0.10
TAKE_PROFIT_PCT = 1.50

SCALE_OUT_PCT = 0.25
BREAKEVEN_BUFFER_PCT = 0.02

MAX_TRADE_MINUTES = 20

# إغلاق إجباري قبل نهاية الجلسة
EOD_CLOSE_TIME = dtime(15, 50)

# =========================================================
# 5) فلاتر السوق
# =========================================================

MIN_REALIZED_VOL = 0.006
MAX_BAR_AGE_SECONDS = 240

LUNCH_START = dtime(11, 45)
LUNCH_END = dtime(13, 15)

SIGNAL_START = dtime(9, 45)
SIGNAL_END = dtime(15, 30)

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

# =========================================================
# 6) التسجيل
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(NAME)


# =========================================================
# 7) أدوات الوقت والملفات
# =========================================================

def now_ny():
    return datetime.now(NY)


def today_ny():
    return now_ny().date().isoformat()


def stamp():
    return now_ny().isoformat(timespec="seconds")


def is_market_open():
    now = now_ny()

    if now.weekday() >= 5:
        return False

    return MARKET_OPEN <= now.time() < MARKET_CLOSE


def in_signal_window():
    now = now_ny()
    t = now.time()

    if not is_market_open():
        return False

    if not (SIGNAL_START <= t <= SIGNAL_END):
        return False

    if LUNCH_START <= t <= LUNCH_END:
        return False

    return True


def save_json(path, data):
    temp = Path(str(path) + ".tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    temp.replace(path)


def load_json(path, default):
    try:
        if not path.exists():
            return default

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as exc:
        log.error("تعذر قراءة الملف %s: %s", path, exc)
        return default


def empty_state():
    return {
        "date": today_ny(),
        "positions": {},
        "daily_trades": 0,
        "consecutive_losses": 0,
        "last_trade_time": None,
        "last_loss_time": None,
        "last_win_time": None,
        "daily_start_equity": None,
        "confirmation": {
            "direction": None,
            "count": 0,
        },
    }


state = load_json(STATE_FILE, empty_state())


def reset_daily_state_if_needed():
    global state

    if state.get("date") != today_ny():
        old_positions = state.get("positions", {})

        state = empty_state()

        # لا نمسح تتبع الصفقات المفتوحة عند تغير التاريخ
        state["positions"] = old_positions

        save_json(STATE_FILE, state)
        log.info("تم تحديث بيانات اليوم الجديد.")


def save_state():
    save_json(STATE_FILE, state)


# =========================================================
# 8) طلبات Alpaca العامة
# =========================================================

def alpaca_request(
    method,
    url,
    *,
    params=None,
    payload=None,
    timeout=15,
):
    response = requests.request(
        method=method,
        url=url,
        headers=HEADERS,
        params=params,
        json=payload,
        timeout=timeout,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Alpaca HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    if not response.text:
        return {}

    return response.json()


def trading_get(path, params=None):
    return alpaca_request(
        "GET",
        TRADING_URL + path,
        params=params,
    )


def trading_post(path, payload):
    return alpaca_request(
        "POST",
        TRADING_URL + path,
        payload=payload,
    )


def trading_delete(path):
    return alpaca_request(
        "DELETE",
        TRADING_URL + path,
    )


def data_get(path, params=None):
    return alpaca_request(
        "GET",
        DATA_URL + path,
        params=params,
    )


# =========================================================
# 9) بيانات الحساب
# =========================================================

def get_account():
    return trading_get("/v2/account")


def get_equity():
    account = get_account()
    return float(account.get("equity", 0) or 0)


def get_clock():
    return trading_get("/v2/clock")


def get_open_orders():
    result = trading_get(
        "/v2/orders",
        params={
            "status": "open",
            "limit": 500,
            "nested": "true",
        },
    )

    return result if isinstance(result, list) else []


def get_positions():
    result = trading_get("/v2/positions")
    return result if isinstance(result, list) else []


# =========================================================
# 10) أوامر البوت فقط
# =========================================================

BOT_ORDER_PREFIX = "SAFAA644_"


def is_bot_order(order):
    client_id = str(order.get("client_order_id", ""))
    return client_id.startswith(BOT_ORDER_PREFIX)


def get_bot_open_orders():
    return [
        order
        for order in get_open_orders()
        if is_bot_order(order)
    ]


def cancel_bot_open_orders():
    """
    يلغي أوامر هذا البوت فقط.
    لا يلمس أوامر المستخدم اليدوية أو البوتات الأخرى.
    """
    for order in get_bot_open_orders():
        order_id = order.get("id")

        if not order_id:
            continue

        try:
            trading_delete(f"/v2/orders/{order_id}")
            log.info("تم إلغاء أمر تابع للبوت: %s", order_id)

        except Exception as exc:
            log.warning(
                "تعذر إلغاء أمر البوت %s: %s",
                order_id,
                exc,
            )


def make_client_order_id(prefix="ENTRY"):
    return (
        BOT_ORDER_PREFIX
        + prefix
        + "_"
        + uuid.uuid4().hex[:12]
    )


# =========================================================
# 11) جلب بيانات الأسهم
# =========================================================

def fetch_stock_bars(
    symbol,
    days=LIVE_DAYS,
    limit=10000,
):
    end = datetime.now(UTC)
    start = end - timedelta(days=days)

    params = {
        "timeframe": TIMEFRAME,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": limit,
        "adjustment": "raw",
        "feed": STOCK_FEED,
        "sort": "asc",
    }

    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"

    try:
        result = alpaca_request(
            "GET",
            url,
            params=params,
        )

    except Exception as first_error:
        if STOCK_FEED == "iex":
            raise

        log.warning(
            "تعذر جلب %s عبر %s، تجربة IEX: %s",
            symbol,
            STOCK_FEED,
            first_error,
        )

        params["feed"] = "iex"

        result = alpaca_request(
            "GET",
            url,
            params=params,
        )

    bars = result.get("bars", [])

    if not bars:
        return pd.DataFrame()

    df = pd.DataFrame(bars)

    df = df.rename(
        columns={
            "t": "timestamp",
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
            "n": "trade_count",
            "vw": "vwap",
        }
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        utc=True,
    )

    df = df.set_index("timestamp").sort_index()

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce",
            )

    df = df.dropna(subset=["close", "volume"])

    return df


def fetch_live_market_data():
    spy = fetch_stock_bars(SYMBOL, LIVE_DAYS)
    qqq = fetch_stock_bars(BENCHMARK, LIVE_DAYS)

    if spy.empty:
        raise RuntimeError("بيانات SPY فارغة.")

    if len(spy) < MIN_RAW_BARS:
        raise RuntimeError(
            f"بيانات SPY غير كافية: {len(spy)} شمعة."
        )

    return spy, qqq


# =========================================================
# 12) التحقق من حداثة البيانات
# =========================================================

def check_bar_freshness(df):
    if df.empty:
        return False, "لا توجد بيانات"

    last_time = df.index[-1]

    if last_time.tzinfo is None:
        last_time = last_time.tz_localize(UTC)

    age = (
        datetime.now(UTC) - last_time.to_pydatetime()
    ).total_seconds()

    if age > MAX_BAR_AGE_SECONDS:
        return False, f"الشمعة قديمة: {age:.0f} ثانية"

    return True, "البيانات حديثة"


# =========================================================
# 13) مؤشرات السوق الأساسية
# =========================================================

def calculate_rsi(series, period=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - (100 / (1 + rs))


def generate_institutional_features(spy, qqq):
    df = spy.copy()

    close = df["close"]
    volume = df["volume"].replace(0, np.nan)

    df["ret_1"] = close.pct_change(1)
    df["ret_3"] = close.pct_change(3)
    df["ret_5"] = close.pct_change(5)
    df["ret_10"] = close.pct_change(10)

    df["ema_9"] = close.ewm(
        span=9,
        adjust=False,
    ).mean()

    df["ema_21"] = close.ewm(
        span=21,
        adjust=False,
    ).mean()

    df["ema_50"] = close.ewm(
        span=50,
        adjust=False,
    ).mean()

    df["rsi"] = calculate_rsi(close)

    ema12 = close.ewm(
        span=12,
        adjust=False,
    ).mean()

    ema26 = close.ewm(
        span=26,
        adjust=False,
    ).mean()

    df["macd"] = ema12 - ema26

    df["macd_signal"] = df["macd"].ewm(
        span=9,
        adjust=False,
    ).mean()

    # VWAP-Z الصحيح:
    # الانحراف عن VWAP، ثم الانحراف المعياري لهذا الانحراف
    if "vwap" in df.columns:
        vwap = df["vwap"].replace(0, np.nan)
    else:
        typical = (
            df["high"] + df["low"] + df["close"]
        ) / 3

        cumulative_volume = volume.fillna(0).cumsum()

        vwap = (
            typical * volume
        ).fillna(0).cumsum() / cumulative_volume.replace(
            0,
            np.nan,
        )

    df["vwap"] = vwap

    vwap_deviation = close - vwap

    vwap_std = vwap_deviation.rolling(
        30,
        min_periods=10,
    ).std()

    df["vwap_z"] = (
        vwap_deviation
        / vwap_std.replace(0, np.nan)
    )

    # سرعة الحجم
    average_volume = volume.rolling(
        20,
        min_periods=10,
    ).mean()

    df["volume_velocity"] = (
        volume / average_volume.replace(0, np.nan)
    )

    # التقلب المحقق
    df["realized_vol"] = (
        df["ret_1"]
        .rolling(20, min_periods=10)
        .std()
        * math.sqrt(390)
    )

    # اتجاه الخمس دقائق
    five_min = close.resample("5min").last().dropna()

    ema9_5m = five_min.ewm(
        span=9,
        adjust=False,
    ).mean()

    ema21_5m = five_min.ewm(
        span=21,
        adjust=False,
    ).mean()

    trend_5m = pd.Series(
        np.where(ema9_5m > ema21_5m, 1, -1),
        index=five_min.index,
    )

    df["trend_5m"] = trend_5m.reindex(
        df.index,
        method="ffill",
    )

    # QQQ كعامل تأكيد للسوق
    if not qqq.empty:
        qqq_close = qqq["close"].reindex(
            df.index,
            method="ffill",
        )

        qqq_return = qqq_close.pct_change(5)

        df["qqq_ret_5"] = qqq_return
        df["relative_strength"] = (
            df["ret_5"] - qqq_return
        )

    else:
        df["qqq_ret_5"] = 0.0
        df["relative_strength"] = 0.0

    # نطاق الشمعة
    df["bar_range"] = (
        df["high"] - df["low"]
    ) / close.replace(0, np.nan)

    # موقع الإغلاق داخل الشمعة
    df["close_location"] = (
        (close - df["low"])
        / (df["high"] - df["low"]).replace(0, np.nan)
    )

    # إزالة القيم غير المنطقية
    df = df.replace(
        [np.inf, -np.inf],
        np.nan,
    )

    return df


# =========================================================
# 14) أعمدة النموذج
# =========================================================

FEATURES = [
    "ret_1",
    "ret_3",
    "ret_5",
    "ret_10",
    "ema_9",
    "ema_21",
    "ema_50",
    "rsi",
    "macd",
    "macd_signal",
    "vwap_z",
    "volume_velocity",
    "realized_vol",
    "trend_5m",
    "qqq_ret_5",
    "relative_strength",
    "bar_range",
    "close_location",
]

TARGET = "target"
# =========================================================
# 15) بناء بيانات التدريب
# =========================================================

def build_training_dataset(spy, qqq):
    df = generate_institutional_features(spy, qqq)

    future_return = (
        df["close"].shift(-FORECAST_HORIZON)
        / df["close"]
    ) - 1

    # تصنيف الحركة المستقبلية
    #  1 = صعود
    # -1 = هبوط
    #  0 = حركة غير واضحة

    threshold = 0.00035

    df[TARGET] = np.where(
        future_return > threshold,
        1,
        np.where(
            future_return < -threshold,
            -1,
            0,
        ),
    )

    df = df.dropna(
        subset=FEATURES + [TARGET]
    ).copy()

    df[TARGET] = df[TARGET].astype(int)

    # حذف الفترات المحايدة من التدريب
    df = df[df[TARGET] != 0]

    return df


# =========================================================
# 16) تدريب النموذج
# =========================================================

model = None
model_trained = False
last_training_time = None


def train_model():
    global model
    global model_trained
    global last_training_time

    log.info("بدء تدريب نموذج SPY...")

    try:
        spy = fetch_stock_bars(
            SYMBOL,
            HISTORY_DAYS,
            limit=10000,
        )

        qqq = fetch_stock_bars(
            BENCHMARK,
            HISTORY_DAYS,
            limit=10000,
        )

        if spy.empty:
            raise RuntimeError("بيانات SPY غير متاحة.")

        if len(spy) < MIN_RAW_BARS:
            raise RuntimeError(
                f"عدد شموع SPY غير كافٍ: {len(spy)}"
            )

        dataset = build_training_dataset(spy, qqq)

        if len(dataset) < MIN_TRAIN_ROWS:
            raise RuntimeError(
                f"بيانات التدريب غير كافية: {len(dataset)}"
            )

        X = dataset[FEATURES]
        y = dataset[TARGET]

        if y.nunique() < 2:
            raise RuntimeError(
                "بيانات التدريب تحتوي على اتجاه واحد فقط."
            )

        # تقسيم زمني للتحقق، دون خلط المستقبل بالماضي
        split = int(len(dataset) * 0.75)

        X_train = X.iloc[:split]
        y_train = y.iloc[:split]

        X_test = X.iloc[split:]
        y_test = y.iloc[split:]

        if y_train.nunique() < 2:
            raise RuntimeError(
                "بيانات التدريب لا تحتوي الاتجاهين."
            )

        candidate = make_pipeline(
            SimpleImputer(strategy="median"),
            ExtraTreesClassifier(
                n_estimators=250,
                max_depth=8,
                min_samples_leaf=8,
                class_weight="balanced",
                random_state=42,
                n_jobs=-1,
            ),
        )

        candidate.fit(X_train, y_train)

        # تقييم زمني بسيط
        test_predictions = candidate.predict(X_test)

        accuracy = float(
            np.mean(test_predictions == y_test)
        )

        log.info(
            "نتيجة التحقق الزمني | Accuracy: %.2f%% | "
            "عينات الاختبار: %d",
            accuracy * 100,
            len(X_test),
        )

        # بعد التحقق نعيد التدريب على كل البيانات المتاحة
        # لا يوجد Meta Model ولا خلط احتمالات مصطنع
        candidate.fit(X, y)

        model = candidate
        model_trained = True
        last_training_time = stamp()

        log.info(
            "اكتمل التدريب | عدد العينات: %d | "
            "دقة الاختبار التاريخي: %.2f%%",
            len(dataset),
            accuracy * 100,
        )

        return True

    except Exception as exc:
        log.exception("فشل تدريب النموذج: %s", exc)
        model_trained = False
        return False


# =========================================================
# 17) استخراج توقع النموذج
# =========================================================

def predict_direction(features_df):
    if not model_trained or model is None:
        return None

    if features_df.empty:
        return None

    latest = features_df.iloc[-1]

    X_live = pd.DataFrame(
        [latest[FEATURES].to_dict()],
        columns=FEATURES,
    )

    probabilities = model.predict_proba(X_live)[0]
    classes = model.classes_

    best_index = int(np.argmax(probabilities))

    direction = int(classes[best_index])
    confidence = float(probabilities[best_index])

    # هذا رقم ثقة النموذج وليس احتمال ربح مضمونًا
    return {
        "direction": direction,
        "confidence": confidence,
        "probabilities": {
            str(int(cls)): float(prob)
            for cls, prob in zip(classes, probabilities)
        },
    }


# =========================================================
# 18) تحليل السوق المباشر
# =========================================================

def analyze_market():
    spy, qqq = fetch_live_market_data()

    fresh, freshness_reason = check_bar_freshness(spy)

    if not fresh:
        raise RuntimeError(freshness_reason)

    features_df = generate_institutional_features(
        spy,
        qqq,
    )

    if features_df.empty:
        raise RuntimeError("تعذر بناء مؤشرات السوق.")

    latest = features_df.iloc[-1]

    prediction = predict_direction(features_df)

    if prediction is None:
        raise RuntimeError("النموذج غير جاهز للتوقع.")

    direction = prediction["direction"]
    confidence = prediction["confidence"]

    if direction == 1:
        signal = "CALL"
        direction_ar = "صعود"
    elif direction == -1:
        signal = "PUT"
        direction_ar = "هبوط"
    else:
        signal = "NEUTRAL"
        direction_ar = "محايد"

    result = {
        "signal": signal,
        "direction": direction,
        "direction_ar": direction_ar,
        "confidence": confidence,
        "price": float(latest["close"]),
        "rsi": float(latest["rsi"]),
        "vwap_z": float(latest["vwap_z"]),
        "volume_velocity": float(
            latest["volume_velocity"]
        ),
        "realized_vol": float(
            latest["realized_vol"]
        ),
        "trend_5m": int(latest["trend_5m"]),
        "relative_strength": float(
            latest["relative_strength"]
        ),
        "timestamp": stamp(),
    }

    return result, features_df


# =========================================================
# 19) فلاتر سياق الدخول
# =========================================================

def entry_context_ok(analysis):
    direction = analysis["direction"]

    if direction not in (-1, 1):
        return False, "اتجاه النموذج محايد"

    if analysis["confidence"] < MIN_CONFIDENCE_THRESHOLD:
        return False, (
            f"الثقة أقل من الحد: "
            f"{analysis['confidence']:.1%}"
        )

    if analysis["volume_velocity"] < MIN_VOLUME_VELOCITY:
        return False, (
            f"سرعة الحجم ضعيفة: "
            f"{analysis['volume_velocity']:.2f}"
        )

    if analysis["realized_vol"] < MIN_REALIZED_VOL:
        return False, (
            f"التذبذب غير كافٍ: "
            f"{analysis['realized_vol']:.4f}"
        )

    # اتجاه 5 دقائق يجب أن يتفق مع النموذج
    if analysis["trend_5m"] != direction:
        return False, "اتجاه 5 دقائق لا يؤكد الإشارة"

    # CALL: السعر فوق VWAP
    if direction == 1 and analysis["vwap_z"] < 0:
        return False, "CALL مرفوض: السعر أسفل VWAP"

    # PUT: السعر أسفل VWAP
    if direction == -1 and analysis["vwap_z"] > 0:
        return False, "PUT مرفوض: السعر أعلى VWAP"

    # لا ندخل عند ابتعاد السعر بشدة عن VWAP
    if abs(analysis["vwap_z"]) > 2.0:
        return False, "السعر بعيد جدًا عن VWAP"

    # لا نشتري CALL عند تشبع RSI
    if direction == 1 and analysis["rsi"] >= 75:
        return False, "CALL مرفوض: تشبع شرائي"

    # لا نشتري PUT عند تشبع بيعي قوي
    if direction == -1 and analysis["rsi"] <= 25:
        return False, "PUT مرفوض: تشبع بيعي"

    return True, "اجتاز فلاتر السوق"


# =========================================================
# 20) تأكيد الإشارة على أكثر من مسح
# =========================================================

def confirm_signal(direction):
    confirmation = state.get(
        "confirmation",
        {
            "direction": None,
            "count": 0,
        },
    )

    if confirmation.get("direction") == direction:
        confirmation["count"] = (
            int(confirmation.get("count", 0)) + 1
        )
    else:
        confirmation = {
            "direction": direction,
            "count": 1,
        }

    state["confirmation"] = confirmation
    save_state()

    count = confirmation["count"]

    log.info(
        "تأكيد الإشارة: %s | %d/%d",
        "CALL" if direction == 1 else "PUT",
        count,
        REQUIRED_CONFIRMATIONS,
    )

    return count >= REQUIRED_CONFIRMATIONS


def reset_confirmation():
    state["confirmation"] = {
        "direction": None,
        "count": 0,
    }

    save_state()


# =========================================================
# 21) التحقق من صلاحية التداول
# =========================================================

def trading_allowed():
    reset_daily_state_if_needed()

    try:
        account = get_account()

        if account.get("status") != "ACTIVE":
            return False, "حالة حساب Alpaca ليست ACTIVE"

        if account.get("trading_blocked"):
            return False, "التداول محظور على الحساب"

        equity = float(account.get("equity", 0) or 0)

        if equity <= 0:
            return False, "قيمة الحساب غير صالحة"

        if state.get("daily_start_equity") is None:
            state["daily_start_equity"] = equity
            save_state()

        start_equity = float(
            state["daily_start_equity"]
        )

        daily_change = (
            equity - start_equity
        ) / start_equity

        if daily_change <= -MAX_DAILY_LOSS_PCT:
            return False, (
                f"تم الوصول إلى حد الخسارة اليومية "
                f"{daily_change:.2%}"
            )

        if state["daily_trades"] >= MAX_TRADES_PER_DAY:
            return False, "تم بلوغ الحد اليومي للصفقات"

        if (
            state["consecutive_losses"]
            >= MAX_CONSECUTIVE_LOSSES
        ):
            return False, "تم بلوغ حد الخسائر المتتالية"

        last_loss = state.get("last_loss_time")

        if last_loss:
            last_loss_dt = datetime.fromisoformat(last_loss)

            if last_loss_dt.tzinfo is None:
                last_loss_dt = last_loss_dt.replace(tzinfo=NY)

            elapsed = (
                now_ny() - last_loss_dt.astimezone(NY)
            ).total_seconds() / 60

            if elapsed < COOLDOWN_AFTER_LOSS_MIN:
                return False, (
                    f"فترة تهدئة بعد خسارة: "
                    f"{elapsed:.1f} دقيقة"
                )

        last_win = state.get("last_win_time")

        if last_win:
            last_win_dt = datetime.fromisoformat(last_win)

            if last_win_dt.tzinfo is None:
                last_win_dt = last_win_dt.replace(tzinfo=NY)

            elapsed = (
                now_ny() - last_win_dt.astimezone(NY)
            ).total_seconds() / 60

            if elapsed < COOLDOWN_AFTER_WIN_MIN:
                return False, "فترة تهدئة بعد صفقة رابحة"

        save_state()
        return True, "مسموح"

    except Exception as exc:
        log.exception("تعذر التحقق من الحساب: %s", exc)
        return False, "تعذر التحقق من صلاحية التداول"


# =========================================================
# 22) جلب عقود SPY المتاحة
# =========================================================

def fetch_option_contracts(option_type):
    today = now_ny().date()

    expiration_start = today + timedelta(days=MIN_DTE)
    expiration_end = today + timedelta(days=MAX_DTE)

    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "expiration_date_gte": expiration_start.isoformat(),
        "expiration_date_lte": expiration_end.isoformat(),
        "type": (
            "call" if option_type == "CALL" else "put"
        ),
        "limit": 1000,
    }

    result = trading_get(
        "/v2/options/contracts",
        params=params,
    )

    contracts = result.get("option_contracts", [])

    return contracts if isinstance(contracts, list) else []


# =========================================================
# 23) جلب بيانات عقد محدد
# =========================================================

def fetch_option_snapshot(symbol):
    url = (
        f"{DATA_URL}/v1beta1/options/snapshots/"
        f"{symbol}"
    )

    params = {
        "feed": OPTION_FEED,
    }

    result = alpaca_request(
        "GET",
        url,
        params=params,
    )

    if isinstance(result, dict):
        if symbol in result:
            return result[symbol]

        if "latestQuote" in result or "latest_quote" in result:
            return result

    return {}


def extract_quote(snapshot):
    quote = (
        snapshot.get("latestQuote")
        or snapshot.get("latest_quote")
        or {}
    )

    bid = float(
        quote.get("bp")
        or quote.get("bid_price")
        or 0
    )

    ask = float(
        quote.get("ap")
        or quote.get("ask_price")
        or 0
    )

    return bid, ask


def extract_greeks(snapshot):
    greeks = snapshot.get("greeks") or {}

    delta = greeks.get("delta")
    iv = (
        snapshot.get("impliedVolatility")
        or snapshot.get("implied_volatility")
        or snapshot.get("iv")
    )

    try:
        delta = float(delta)
    except (TypeError, ValueError):
        delta = None

    try:
        iv = float(iv)
    except (TypeError, ValueError):
        iv = None

    return delta, iv


# =========================================================
# 24) فحص العقد
# =========================================================

def evaluate_contract(contract):
    symbol = contract.get("symbol")

    if not symbol:
        return None, "رمز العقد غير متاح"

    try:
        snapshot = fetch_option_snapshot(symbol)

        if not snapshot:
            return None, "بيانات العقد غير متاحة"

        bid, ask = extract_quote(snapshot)

        if bid <= 0 or ask <= 0 or ask < bid:
            return None, "سعر العرض أو الطلب غير صالح"

        mid = (bid + ask) / 2

        if mid <= 0:
            return None, "السعر المتوسط غير صالح"

        spread_pct = (ask - bid) / mid

        if spread_pct > MAX_SPREAD_PCT:
            return None, (
                f"السبريد مرتفع: {spread_pct:.2%}"
            )

        delta, iv = extract_greeks(snapshot)

        # لا نسمح بتمرير العقد إذا كانت Greeks غير متاحة
        if delta is None:
            return None, "Delta غير متاحة"

        if not (MIN_DELTA <= abs(delta) <= MAX_DELTA):
            return None, (
                f"Delta خارج النطاق: {delta:.3f}"
            )

        if iv is None:
            return None, "IV غير متاحة"

        # Alpaca عادة يعيد IV كقيمة عشرية
        if iv > MAX_IMPLIED_VOLATILITY:
            return None, f"IV مرتفعة: {iv:.2%}"

        open_interest = (
            contract.get("open_interest")
            or snapshot.get("open_interest")
            or snapshot.get("openInterest")
        )

        if open_interest is not None:
            try:
                open_interest = int(open_interest)
            except (TypeError, ValueError):
                open_interest = None

        if (
            open_interest is not None
            and open_interest < MIN_OPEN_INTEREST
        ):
            return None, (
                f"الفائدة المفتوحة منخفضة: {open_interest}"
            )

        # لا ندّعي وجود OI إذا لم يرسله المصدر
        if open_interest is None:
            log.info(
                "تنبيه: OI غير متاح للعقد %s؛ "
                "سيعتمد الفحص على السبريد والسعر.",
                symbol,
            )

        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": spread_pct,
            "delta": delta,
            "iv": iv,
            "open_interest": open_interest,
            "snapshot": snapshot,
        }, "مقبول"

    except Exception as exc:
        return None, f"خطأ في فحص العقد: {exc}"


# =========================================================
# 25) اختيار أفضل عقد
# =========================================================

def select_best_contract(option_type):
    contracts = fetch_option_contracts(option_type)

    if not contracts:
        log.warning("لم يتم العثور على عقود %s.", option_type)
        return None

    candidates = []

    # ترتيب العقود الأقرب إلى دلتا الهدف
    contracts = sorted(
        contracts,
        key=lambda c: abs(
            float(c.get("strike_price", 0) or 0)
        ),
    )

    for contract in contracts[:MAX_CONTRACTS_TO_CHECK]:
        evaluated, reason = evaluate_contract(contract)

        if evaluated is None:
            log.info(
                "رفض العقد %s | السبب: %s",
                contract.get("symbol", "?"),
                reason,
            )
            continue

        delta_score = abs(
            abs(evaluated["delta"]) - 0.52
        )

        spread_score = evaluated["spread_pct"]

        # الأقل أفضل: دلتا قريبة من 0.52 وسبريد ضيق
        score = (
            delta_score * 2
            + spread_score
        )

        evaluated["score"] = score
        candidates.append(evaluated)

    if not candidates:
        log.warning(
            "لم يجتز أي عقد فلاتر السيولة والسعر."
        )
        return None

    candidates.sort(
        key=lambda item: item["score"]
    )

    best = candidates[0]

    log.info(
        "أفضل عقد: %s | Bid %.2f | Ask %.2f | "
        "Delta %.3f | Spread %.2f%%",
        best["symbol"],
        best["bid"],
        best["ask"],
        best["delta"],
        best["spread_pct"] * 100,
    )

    return best


# =========================================================
# 26) حساب حجم الصفقة
# =========================================================

def calculate_position_qty(equity, premium):
    """
    حساب محافظ لحجم الصفقة.
    الحد الأقصى للمخاطرة محسوب على كامل قيمة العقد
    لأن مشتري الخيار قد يخسر كامل قيمة العلاوة.
    """

    if equity <= 0 or premium <= 0:
        return 0

    risk_budget = equity * min(
        RISK_PER_TRADE_PCT,
        MAX_RISK_CAP,
    )

    contract_cost = premium * 100

    if contract_cost <= 0:
        return 0

    qty = int(risk_budget // contract_cost)

    # لا نرفع العدد قسرًا إلى عقد واحد إذا تجاوز المخاطرة
    if qty < 1:
        return 0

    return min(qty, 30)


# =========================================================
# 27) طباعة ملخص الإشارة
# =========================================================

def print_analysis(analysis):
    log.info(
        "💓 SPY $%.2f | الإشارة: %s | الثقة: %.1f%% | "
        "RSI: %.1f | اتجاه 5د: %s | VWAP-Z: %.2f | "
        "سرعة الحجم: %.2f",
        analysis["price"],
        analysis["direction_ar"],
        analysis["confidence"] * 100,
        analysis["rsi"],
        analysis["trend_5m"],
        analysis["vwap_z"],
        analysis["volume_velocity"],
    )
    # =========================================================
# 28) سجل الصفقات
# =========================================================

def journal_trade(action, symbol, qty, price, reason, pnl=None):
    row = {
        "timestamp": stamp(),
        "action": action,
        "symbol": symbol,
        "qty": qty,
        "price": price,
        "reason": reason,
        "pnl": pnl,
    }

    df = pd.DataFrame([row])

    write_header = not JOURNAL_FILE.exists()

    df.to_csv(
        JOURNAL_FILE,
        mode="a",
        header=write_header,
        index=False,
        encoding="utf-8-sig",
    )


# =========================================================
# 29) جلب أمر محدد
# =========================================================

def get_order(order_id):
    return trading_get(f"/v2/orders/{order_id}")


def wait_for_order(order_id, timeout=5):
    start = time.time()

    while time.time() - start < timeout:
        try:
            order = get_order(order_id)
            status = order.get("status")

            if status in (
                "filled",
                "canceled",
                "rejected",
                "expired",
            ):
                return order

        except Exception as exc:
            log.warning("تعذر تحديث حالة الأمر: %s", exc)

        time.sleep(0.5)

    return get_order(order_id)


# =========================================================
# 30) تنفيذ أمر شراء عقد
# =========================================================

def submit_entry_order(contract, qty):
    if qty <= 0:
        return None

    symbol = contract["symbol"]

    # نستخدم سعرًا قريبًا من منتصف السبريد
    limit_price = round(
        (contract["bid"] + contract["ask"]) / 2,
        2,
    )

    if limit_price <= 0:
        return None

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": f"{limit_price:.2f}",
        "client_order_id": make_client_order_id("ENTRY"),
    }

    log.info(
        "إرسال أمر شراء | %s | الكمية %d | السعر %.2f",
        symbol,
        qty,
        limit_price,
    )

    try:
        order = trading_post(
            "/v2/orders",
            payload,
        )

        order_id = order.get("id")

        if not order_id:
            log.error("لم يرجع Alpaca رقم الأمر.")
            return None

        final_order = wait_for_order(
            order_id,
            timeout=EXECUTION_TIMEOUT,
        )

        status = final_order.get("status")

        if status != "filled":
            # نلغي هذا الأمر فقط
            try:
                trading_delete(f"/v2/orders/{order_id}")
            except Exception:
                pass

            # نعيد قراءة حالة الأمر بعد الإلغاء
            try:
                final_order = get_order(order_id)
            except Exception:
                pass

        filled_qty = int(
            float(final_order.get("filled_qty", 0) or 0)
        )

        filled_price = float(
            final_order.get("filled_avg_price", 0) or 0
        )

        if filled_qty <= 0 or filled_price <= 0:
            log.warning(
                "لم يتم تنفيذ أمر الشراء | الحالة: %s",
                final_order.get("status"),
            )
            return None

        return {
            "symbol": symbol,
            "qty": filled_qty,
            "entry_price": filled_price,
            "order_id": order_id,
        }

    except Exception as exc:
        log.exception("فشل إرسال أمر الشراء: %s", exc)
        return None


# =========================================================
# 31) تنفيذ أمر بيع عقد
# =========================================================

def submit_exit_order(symbol, qty, reason):
    if qty <= 0:
        return None

    try:
        snapshot = fetch_option_snapshot(symbol)
        bid, ask = extract_quote(snapshot)

        if bid <= 0:
            log.error(
                "لا يوجد Bid صالح للبيع: %s",
                symbol,
            )
            return None

        # أمر بيع محدد السعر قرب Bid
        limit_price = round(bid, 2)

        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "limit",
            "time_in_force": "day",
            "limit_price": f"{limit_price:.2f}",
            "client_order_id": make_client_order_id("EXIT"),
        }

        order = trading_post(
            "/v2/orders",
            payload,
        )

        order_id = order.get("id")

        if not order_id:
            return None

        final_order = wait_for_order(
            order_id,
            timeout=EXECUTION_TIMEOUT,
        )

        if final_order.get("status") != "filled":
            try:
                trading_delete(f"/v2/orders/{order_id}")
            except Exception:
                pass

            try:
                final_order = get_order(order_id)
            except Exception:
                pass

        filled_qty = int(
            float(final_order.get("filled_qty", 0) or 0)
        )

        filled_price = float(
            final_order.get("filled_avg_price", 0) or 0
        )

        # لا نرسل أمرًا سوقيًا تلقائيًا عند فشل الأمر المحدد.
        # نعيد المحاولة في دورة إدارة الصفقة التالية.
        if filled_qty <= 0 or filled_price <= 0:
            log.warning(
                "لم ينفذ أمر الخروج %s | الحالة: %s",
                symbol,
                final_order.get("status"),
            )
            return None

        log.info(
            "تم تنفيذ بيع %s | الكمية %d | السعر %.2f | %s",
            symbol,
            filled_qty,
            filled_price,
            reason,
        )

        return {
            "qty": filled_qty,
            "price": filled_price,
            "reason": reason,
        }

    except Exception as exc:
        log.exception("خطأ في أمر البيع: %s", exc)
        return None


# =========================================================
# 32) قراءة مراكز Alpaca
# =========================================================

def get_position_map():
    positions = get_positions()

    result = {}

    for position in positions:
        symbol = position.get("symbol")

        if not symbol:
            continue

        result[symbol] = position

    return result


def get_tracked_positions():
    """
    لا نتابع إلا المراكز المسجلة في ملف البوت.
    المراكز اليدوية تبقى خارج إدارة البوت.
    """

    current_positions = get_position_map()
    tracked = state.get("positions", {})

    result = []

    for symbol, metadata in list(tracked.items()):
        if symbol not in current_positions:
            log.info(
                "المركز لم يعد موجودًا في الحساب: %s",
                symbol,
            )

            tracked.pop(symbol, None)
            continue

        result.append(
            (
                symbol,
                metadata,
                current_positions[symbol],
            )
        )

    state["positions"] = tracked
    save_state()

    return result


# =========================================================
# 33) حساب الربح والخسارة
# =========================================================

def get_option_mark(symbol):
    snapshot = fetch_option_snapshot(symbol)

    bid, ask = extract_quote(snapshot)

    if bid <= 0 or ask <= 0:
        return None

    return {
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2,
    }


def calculate_position_pnl(entry_price, current_price):
    if entry_price <= 0:
        return 0.0

    return (
        current_price - entry_price
    ) / entry_price


# =========================================================
# 34) تحديث نتائج الصفقات
# =========================================================

def register_closed_trade(pnl_amount):
    if pnl_amount < 0:
        state["consecutive_losses"] = (
            int(state.get("consecutive_losses", 0)) + 1
        )

        state["last_loss_time"] = stamp()

    elif pnl_amount > 0:
        state["consecutive_losses"] = 0
        state["last_win_time"] = stamp()

    save_state()


# =========================================================
# 35) إدارة مركز مفتوح
# =========================================================

def manage_one_position(symbol, metadata, alpaca_position):
    try:
        qty = int(
            abs(float(alpaca_position.get("qty", 0) or 0))
        )

        if qty <= 0:
            return

        entry_price = float(
            metadata.get("entry_price", 0) or 0
        )

        if entry_price <= 0:
            log.error(
                "سعر الدخول غير صالح للمركز %s",
                symbol,
            )
            return

        quote = get_option_mark(symbol)

        if quote is None:
            log.warning(
                "تعذر جلب سعر المركز %s",
                symbol,
            )
            return

        bid = quote["bid"]

        pnl_pct = calculate_position_pnl(
            entry_price,
            bid,
        )

        peak_bid = float(
            metadata.get("peak_bid", entry_price)
        )

        if bid > peak_bid:
            peak_bid = bid
            metadata["peak_bid"] = peak_bid

        metadata["last_bid"] = bid
        metadata["last_pnl_pct"] = pnl_pct

        entry_time = datetime.fromisoformat(
            metadata["entry_time"]
        )

        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=NY)

        elapsed_minutes = (
            now_ny() - entry_time.astimezone(NY)
        ).total_seconds() / 60

        reason = None

        # -------------------------------------------------
        # وقف الخسارة
        # -------------------------------------------------

        if pnl_pct <= -STOP_LOSS_PCT:
            reason = "وقف خسارة"

        # -------------------------------------------------
        # جني الربح
        # -------------------------------------------------

        elif pnl_pct >= TAKE_PROFIT_PCT:
            reason = "تحقيق الهدف"

        # -------------------------------------------------
        # نقطة التعادل بعد ارتفاع العقد
        # -------------------------------------------------

        elif (
            peak_bid >= entry_price * 1.25
            and bid <= entry_price * (1 + BREAKEVEN_BUFFER_PCT)
        ):
            reason = "حماية رأس المال"

        # -------------------------------------------------
        # تتبع الأرباح المتدرج
        # -------------------------------------------------

        elif peak_bid >= entry_price * 1.40:
            trailing_floor = max(
                entry_price * 1.10,
                peak_bid * 0.75,
            )

            if bid <= trailing_floor:
                reason = "خروج تتبع الأرباح"

        # -------------------------------------------------
        # إغلاق عند انتهاء الوقت
        # -------------------------------------------------

        elif elapsed_minutes >= MAX_TRADE_MINUTES:
            reason = "انتهاء مدة الصفقة"

        # -------------------------------------------------
        # إغلاق نهاية الجلسة
        # -------------------------------------------------

        elif now_ny().time() >= EOD_CLOSE_TIME:
            reason = "إغلاق نهاية الجلسة"

        # -------------------------------------------------
        # تخفيض جزئي بعد ارتفاع مناسب
        # -------------------------------------------------

        if (
            reason is None
            and not metadata.get("scaled_out", False)
            and pnl_pct >= 0.50
            and qty >= 2
        ):
            scale_qty = max(
                1,
                int(math.floor(qty * SCALE_OUT_PCT)),
            )

            scale_qty = min(scale_qty, qty - 1)

            result = submit_exit_order(
                symbol,
                scale_qty,
                "جني ربح جزئي",
            )

            if result:
                actual_qty = result["qty"]

                pnl_amount = (
                    result["price"] - entry_price
                ) * 100 * actual_qty

                metadata["scaled_out"] = True
                metadata["realized_pnl"] = (
                    float(metadata.get("realized_pnl", 0))
                    + pnl_amount
                )

                journal_trade(
                    "PARTIAL_EXIT",
                    symbol,
                    actual_qty,
                    result["price"],
                    "جني ربح جزئي",
                    pnl_amount,
                )

                save_state()

                log.info(
                    "تخفيض جزئي %s | الربح %.2f%%",
                    symbol,
                    pnl_pct * 100,
                )

        # -------------------------------------------------
        # تنفيذ الخروج الكامل
        # -------------------------------------------------

        if reason:
            result = submit_exit_order(
                symbol,
                qty,
                reason,
            )

            if result:
                actual_qty = result["qty"]

                pnl_amount = (
                    result["price"] - entry_price
                ) * 100 * actual_qty

                metadata["realized_pnl"] = (
                    float(metadata.get("realized_pnl", 0))
                    + pnl_amount
                )

                journal_trade(
                    "EXIT",
                    symbol,
                    actual_qty,
                    result["price"],
                    reason,
                    pnl_amount,
                )

                # إذا بقي جزء من المركز فلا نحذف التتبع
                remaining_qty = max(
                    0,
                    qty - actual_qty,
                )

                if remaining_qty == 0:
                    total_pnl = float(
                        metadata.get("realized_pnl", 0)
                    )

                    register_closed_trade(total_pnl)

                    state["positions"].pop(symbol, None)

                else:
                    metadata["qty"] = remaining_qty

                save_state()

                log.info(
                    "إغلاق %s | السبب: %s | الربح: %.2f",
                    symbol,
                    reason,
                    pnl_amount,
                )

        else:
            log.info(
                "📊 متابعة %s | Bid %.2f | P/L %.2f%% | "
                "أعلى Bid %.2f | الوقت %.1f دقيقة",
                symbol,
                bid,
                pnl_pct * 100,
                peak_bid,
                elapsed_minutes,
            )

        save_state()

    except Exception as exc:
        log.exception(
            "خطأ في إدارة المركز %s: %s",
            symbol,
            exc,
        )


# =========================================================
# 36) إدارة كل المراكز المسجلة
# =========================================================

def manage_open_positions():
    tracked = get_tracked_positions()

    for symbol, metadata, alpaca_position in tracked:
        manage_one_position(
            symbol,
            metadata,
            alpaca_position,
        )


# =========================================================
# 37) منع تكرار الدخول
# =========================================================

def has_bot_position_or_order():
    if state.get("positions"):
        return True

    if get_bot_open_orders():
        return True

    return False


# =========================================================
# 38) تنفيذ صفقة جديدة
# =========================================================

def open_new_trade():
    if not in_signal_window():
        return

    allowed, reason = trading_allowed()

    if not allowed:
        log.info("لا يوجد دخول | %s", reason)
        return

    # مركز واحد للبوت في نفس الوقت
    if has_bot_position_or_order():
        log.info("هناك صفقة أو أمر مفتوح تابع للبوت.")
        return

    if not model_trained:
        log.info("النموذج لم يكتمل تدريبه.")
        return

    try:
        analysis, features_df = analyze_market()

        print_analysis(analysis)

        if analysis["direction"] == 0:
            log.info("السوق محايد؛ لا دخول.")
            reset_confirmation()
            return

        passed, reason = entry_context_ok(analysis)

        if not passed:
            log.info("رفض الدخول | %s", reason)
            reset_confirmation()
            return

        if not confirm_signal(analysis["direction"]):
            log.info("الإشارة تحتاج تأكيدًا إضافيًا.")
            return

        option_type = (
            "CALL"
            if analysis["direction"] == 1
            else "PUT"
        )

        contract = select_best_contract(option_type)

        if contract is None:
            log.info("لا يوجد عقد مناسب حاليًا.")
            reset_confirmation()
            return

        equity = get_equity()

        qty = calculate_position_qty(
            equity,
            contract["ask"],
        )

        if qty <= 0:
            log.info(
                "قيمة العقد تتجاوز ميزانية المخاطرة؛ "
                "لن يتم الدخول."
            )
            reset_confirmation()
            return

        # إعادة فحص المراكز قبل الإرسال
        if has_bot_position_or_order():
            log.info("تم إلغاء الدخول: ظهر مركز أو أمر للبوت.")
            return

        # إرسال أمر الشراء
        execution = submit_entry_order(
            contract,
            qty,
        )

        if execution is None:
            log.warning("لم يتم تنفيذ صفقة جديدة.")
            reset_confirmation()
            return

        symbol = execution["symbol"]
        filled_qty = execution["qty"]
        entry_price = execution["entry_price"]

        # تسجيل المركز الذي فتحه البوت فقط
        state["positions"][symbol] = {
            "symbol": symbol,
            "qty": filled_qty,
            "entry_price": entry_price,
            "entry_time": stamp(),
            "peak_bid": entry_price,
            "last_bid": entry_price,
            "last_pnl_pct": 0.0,
            "scaled_out": False,
            "realized_pnl": 0.0,
            "direction": option_type,
            "confidence": analysis["confidence"],
            "underlying_price": analysis["price"],
            "order_id": execution["order_id"],
        }

        state["daily_trades"] = (
            int(state.get("daily_trades", 0)) + 1
        )

        state["last_trade_time"] = stamp()

        reset_confirmation()
        save_state()

        journal_trade(
            "ENTRY",
            symbol,
            filled_qty,
            entry_price,
            (
                f"{option_type} | "
                f"Confidence {analysis['confidence']:.3f}"
            ),
        )

        log.info(
            "✅ تم فتح الصفقة | %s | %s | "
            "الكمية %d | الدخول %.2f | الثقة %.1f%%",
            symbol,
            option_type,
            filled_qty,
            entry_price,
            analysis["confidence"] * 100,
        )

    except Exception as exc:
        log.exception("خطأ أثناء محاولة الدخول: %s", exc)


# =========================================================
# 39) تدريب النموذج عند الحاجة
# =========================================================

def should_train():
    global last_training_time

    if not model_trained:
        return True

    if last_training_time is None:
        return True

    try:
        last = datetime.fromisoformat(last_training_time)

        if last.tzinfo is None:
            last = last.replace(tzinfo=NY)

        elapsed = (
            now_ny() - last.astimezone(NY)
        ).total_seconds()

        # إعادة التدريب كل 4 ساعات
        return elapsed >= 4 * 60 * 60

    except Exception:
        return True


# =========================================================
# 40) تقرير حالة البوت
# =========================================================

def print_status():
    try:
        account = get_account()

        equity = float(
            account.get("equity", 0) or 0
        )

        buying_power = float(
            account.get("buying_power", 0) or 0
        )

        log.info(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        log.info(
            "💎 %s | %s",
            NAME,
            VERSION,
        )

        log.info(
            "Equity: $%.2f | Buying Power: $%.2f",
            equity,
            buying_power,
        )

        log.info(
            "صفقات اليوم: %d/%d | "
            "الخسائر المتتالية: %d",
            state.get("daily_trades", 0),
            MAX_TRADES_PER_DAY,
            state.get("consecutive_losses", 0),
        )

        log.info(
            "المراكز التي يتابعها البوت: %d",
            len(state.get("positions", {})),
        )

        log.info(
            "حالة النموذج: %s",
            "جاهز" if model_trained else "غير جاهز",
        )

        log.info(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    except Exception as exc:
        log.warning("تعذر إنشاء تقرير الحالة: %s", exc)


# =========================================================
# 41) حلقة التشغيل الرئيسية
# =========================================================

def main():
    log.info(
        "🚀 بدء تشغيل %s | %s",
        NAME,
        VERSION,
    )

    log.info(
        "السوق: %s | الحساب: %s",
        SYMBOL,
        (
            "PAPER"
            if "paper-api" in TRADING_URL
            else "غير تجريبي — تحقق من الإعدادات"
        ),
    )

    if "paper-api" not in TRADING_URL:
        raise RuntimeError(
            "تم إيقاف التشغيل: رابط التداول ليس Paper. "
            "هذه النسخة مصممة للاختبار التجريبي."
        )

    reset_daily_state_if_needed()

    last_status_time = 0

    while True:
        try:
            reset_daily_state_if_needed()

            # تدريب النموذج
            if should_train():
                train_model()

            # متابعة المراكز المفتوحة حتى خارج نافذة الدخول
            if state.get("positions"):
                manage_open_positions()

            # الدخول فقط خلال وقت الإشارات
            if in_signal_window():
                open_new_trade()

            # تقرير دوري
            if time.time() - last_status_time >= 300:
                print_status()
                last_status_time = time.time()

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("تم إيقاف البوت يدويًا.")
            break

        except Exception as exc:
            log.exception(
                "خطأ في الحلقة الرئيسية: %s",
                exc,
            )

            time.sleep(10)


# =========================================================
# 42) نقطة التشغيل
# =========================================================

if __name__ == "__main__":
    main()