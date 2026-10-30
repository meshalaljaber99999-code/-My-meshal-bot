# -*- coding: utf-8 -*-
"""
صَفاء التحليل v64.6 — Fast Execution & Dynamic Safety
- تسريع استجابة الهبوط القوي (Put Flash Drop)
- دمج حجم التداول مع RSI لمنع تجاوز فرص الهبوط المؤسسي
- تقييد قيم VWAP-Z الشاذة (.clip)
- تأكيد مرن حسب مستوى الثقة (Dynamic Confirmations)
- SPY Options: 0DTE إلى 3DTE
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

from sklearn.ensemble import ExtraTreesClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline

# =========================================================
# 1) الإعدادات العامة
# =========================================================

NAME = "صَفاء التحليل - Fast Execution Pro"
VERSION = "v64.6-Enhanced"

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

BASE_DIR = Path(".")
STATE_FILE = BASE_DIR / "safaa_v646_positions.json"
JOURNAL_FILE = BASE_DIR / "safaa_v646_journal.csv"

SYMBOL = "SPY"
BENCHMARK = "QQQ"

TIMEFRAME = "1Min"
HISTORY_DAYS = 90
LIVE_DAYS = 5

MIN_RAW_BARS = 1200
MIN_TRAIN_ROWS = 500
FORECAST_HORIZON = 5
EXECUTION_TIMEOUT = 5

# =========================================================
# 2) Alpaca
# =========================================================

API_KEY = (
    os.getenv("ALPACA_API_KEY")
    or os.getenv("APCA_API_KEY_ID")
    or ""
).strip()

API_SECRET = (
    os.getenv("ALPACA_API_SECRET")
    or os.getenv("APCA_API_SECRET_KEY")
    or ""
).strip()

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

if not API_KEY or not API_SECRET:
    raise RuntimeError("مفاتيح Alpaca غير موجودة.")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

# =========================================================
# 3) إعدادات الدخول والخيارات (محدثة)
# =========================================================

POLL_SECONDS = 15
POSITION_POLL_SECONDS = 3
SCAN_SECONDS = 20

MIN_CONFIDENCE_THRESHOLD = 0.56
HIGH_CONFIDENCE_THRESHOLD = 0.65  # ثقة عالية للتأكيد الفوري
MIN_VOLUME_VELOCITY = 1.20
BREAKOUT_VOLUME_VELOCITY = 2.00   # حجم تداول كاسر للهبوط الحاد

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
EOD_CLOSE_TIME = dtime(15, 50)

# =========================================================
# 5) فلاتر السوق
# =========================================================

MIN_REALIZED_VOL = 0.006
MAX_BAR_AGE_SECONDS = 900 

LUNCH_START = dtime(11, 45)
LUNCH_END = dtime(13, 15)

SIGNAL_START = dtime(9, 45)
SIGNAL_END = dtime(15, 30)

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
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
        "confirmation": {"direction": None, "count": 0},
    }

state = load_json(STATE_FILE, empty_state())

def reset_daily_state_if_needed():
    global state
    if state.get("date") != today_ny():
        old_positions = state.get("positions", {})
        state = empty_state()
        state["positions"] = old_positions
        save_json(STATE_FILE, state)
        log.info("تم تحديث بيانات اليوم الجديد.")

def save_state():
    save_json(STATE_FILE, state)

# =========================================================
# 8) طلبات Alpaca العامة
# =========================================================

def alpaca_request(method, url, *, params=None, payload=None, timeout=15):
    response = requests.request(
        method=method, url=url, headers=HEADERS, params=params, json=payload, timeout=timeout
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Alpaca HTTP {response.status_code}: {response.text[:500]}")
    if not response.text:
        return {}
    return response.json()

def trading_get(path, params=None):
    return alpaca_request("GET", TRADING_URL + path, params=params)

def trading_post(path, payload):
    return alpaca_request("POST", TRADING_URL + path, payload=payload)

def trading_delete(path):
    return alpaca_request("DELETE", TRADING_URL + path)

def data_get(path, params=None):
    return alpaca_request("GET", DATA_URL + path, params=params)

def get_account():
    return trading_get("/v2/account")

def get_equity():
    account = get_account()
    return float(account.get("equity", 0) or 0)

def get_open_orders():
    result = trading_get("/v2/orders", params={"status": "open", "limit": 500, "nested": "true"})
    return result if isinstance(result, list) else []

def get_positions():
    result = trading_get("/v2/positions")
    return result if isinstance(result, list) else []

BOT_ORDER_PREFIX = "SAFAA646_"

def is_bot_order(order):
    client_id = str(order.get("client_order_id", ""))
    return client_id.startswith(BOT_ORDER_PREFIX)

def get_bot_open_orders():
    return [order for order in get_open_orders() if is_bot_order(order)]

def make_client_order_id(prefix="ENTRY"):
    return BOT_ORDER_PREFIX + prefix + "_" + uuid.uuid4().hex[:12]

# =========================================================
# 11) جلب البيانات والمؤشرات (مع معالجة VWAP-Z)
# =========================================================

def fetch_stock_bars(symbol, days=LIVE_DAYS, limit=10000):
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
        result = alpaca_request("GET", url, params=params)
    except Exception as first_error:
        if STOCK_FEED == "iex":
            raise
        log.warning("تعذر جلب %s عبر %s، تجربة IEX: %s", symbol, STOCK_FEED, first_error)
        params["feed"] = "iex"
        result = alpaca_request("GET", url, params=params)

    bars = result.get("bars", [])
    if not bars:
        return pd.DataFrame()

    df = pd.DataFrame(bars)
    df = df.rename(
        columns={
            "t": "timestamp", "o": "open", "h": "high",
            "l": "low", "c": "close", "v": "volume",
            "n": "trade_count", "vw": "vwap",
        }
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()

    for col in ["open", "high", "low", "close", "volume", "vwap"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.dropna(subset=["close", "volume"])

def fetch_live_market_data():
    spy = fetch_stock_bars(SYMBOL, LIVE_DAYS)
    qqq = fetch_stock_bars(BENCHMARK, LIVE_DAYS)
    if spy.empty or len(spy) < MIN_RAW_BARS:
        raise RuntimeError("بيانات SPY فارغة أو غير كافية.")
    return spy, qqq

def check_bar_freshness(df):
    if df.empty:
        return False, "لا توجد بيانات"
    last_time = df.index[-1]
    if last_time.tzinfo is None:
        last_time = last_time.tz_localize(UTC)
    age = (datetime.now(UTC) - last_time.to_pydatetime()).total_seconds()
    if age > MAX_BAR_AGE_SECONDS:
        return False, f"الشمعة قديمة: {age:.0f} ثانية"
    return True, "البيانات حديثة"

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
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

    df["ema_9"] = close.ewm(span=9, adjust=False).mean()
    df["ema_21"] = close.ewm(span=21, adjust=False).mean()
    df["ema_50"] = close.ewm(span=50, adjust=False).mean()

    df["rsi"] = calculate_rsi(close)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()

    if "vwap" in df.columns:
        vwap = df["vwap"].replace(0, np.nan)
    else:
        typical = (df["high"] + df["low"] + df["close"]) / 3
        cumulative_volume = volume.fillna(0).cumsum()
        vwap = (typical * volume).fillna(0).cumsum() / cumulative_volume.replace(0, np.nan)

    df["vwap"] = vwap
    vwap_deviation = close - vwap
    vwap_std = vwap_deviation.rolling(30, min_periods=10).std()
    
    # ⚡ تعديل هام: تقييد VWAP-Z لمنع الأرقام الشاذة الحادة مثل -22
    raw_vwap_z = vwap_deviation / vwap_std.replace(0, np.nan)
    df["vwap_z"] = raw_vwap_z.clip(-5.0, 5.0)

    average_volume = volume.rolling(20, min_periods=10).mean()
    df["volume_velocity"] = volume / average_volume.replace(0, np.nan)
    df["realized_vol"] = df["ret_1"].rolling(20, min_periods=10).std() * math.sqrt(390)

    five_min = close.resample("5min").last().dropna()
    ema9_5m = five_min.ewm(span=9, adjust=False).mean()
    ema21_5m = five_min.ewm(span=21, adjust=False).mean()
    trend_5m = pd.Series(np.where(ema9_5m > ema21_5m, 1, -1), index=five_min.index)
    df["trend_5m"] = trend_5m.reindex(df.index, method="ffill")

    if not qqq.empty:
        qqq_close = qqq["close"].reindex(df.index, method="ffill")
        qqq_return = qqq_close.pct_change(5)
        df["qqq_ret_5"] = qqq_return
        df["relative_strength"] = df["ret_5"] - qqq_return
    else:
        df["qqq_ret_5"] = 0.0
        df["relative_strength"] = 0.0

    df["bar_range"] = (df["high"] - df["low"]) / close.replace(0, np.nan)
    df["close_location"] = (close - df["low"]) / (df["high"] - df["low"]).replace(0, np.nan)

    return df.replace([np.inf, -np.inf], np.nan)

FEATURES = [
    "ret_1", "ret_3", "ret_5", "ret_10", "ema_9", "ema_21", "ema_50", "rsi",
    "macd", "macd_signal", "vwap_z", "volume_velocity", "realized_vol",
    "trend_5m", "qqq_ret_5", "relative_strength", "bar_range", "close_location"
]

TARGET = "target"

def build_training_dataset(spy, qqq):
    df = generate_institutional_features(spy, qqq)
    future_return = (df["close"].shift(-FORECAST_HORIZON) / df["close"]) - 1
    threshold = 0.00035
    df[TARGET] = np.where(future_return > threshold, 1, np.where(future_return < -threshold, -1, 0))
    df = df.dropna(subset=FEATURES + [TARGET]).copy()
    df[TARGET] = df[TARGET].astype(int)
    return df[df[TARGET] != 0]

model = None
model_trained = False
last_training_time = None

def train_model():
    global model, model_trained, last_training_time
    log.info("بدء تدريب نموذج SPY...")
    try:
        spy = fetch_stock_bars(SYMBOL, HISTORY_DAYS, limit=10000)
        qqq = fetch_stock_bars(BENCHMARK, HISTORY_DAYS, limit=10000)
        dataset = build_training_dataset(spy, qqq)
        X, y = dataset[FEATURES], dataset[TARGET]

        candidate = make_pipeline(
            SimpleImputer(strategy="median"),
            ExtraTreesClassifier(
                n_estimators=250, max_depth=8, min_samples_leaf=8,
                class_weight="balanced", random_state=42, n_jobs=-1
            ),
        )
        candidate.fit(X, y)
        model = candidate
        model_trained = True
        last_training_time = stamp()
        log.info("اكتمل تدريب النموذج بنجاح.")
        return True
    except Exception as exc:
        log.exception("فشل تدريب النموذج: %s", exc)
        model_trained = False
        return False

def predict_direction(features_df):
    if not model_trained or model is None or features_df.empty:
        return None
    latest = features_df.iloc[-1]
    X_live = pd.DataFrame([latest[FEATURES].to_dict()], columns=FEATURES)
    probabilities = model.predict_proba(X_live)[0]
    classes = model.classes_
    best_index = int(np.argmax(probabilities))
    return {
        "direction": int(classes[best_index]),
        "confidence": float(probabilities[best_index]),
    }

def analyze_market():
    spy, qqq = fetch_live_market_data()
    fresh, freshness_reason = check_bar_freshness(spy)
    if not fresh:
        raise RuntimeError(freshness_reason)
    features_df = generate_institutional_features(spy, qqq)
    latest = features_df.iloc[-1]
    prediction = predict_direction(features_df)

    direction = prediction["direction"]
    signal = "CALL" if direction == 1 else "PUT" if direction == -1 else "NEUTRAL"
    direction_ar = "صعود" if direction == 1 else "هبوط" if direction == -1 else "محايد"

    return {
        "signal": signal,
        "direction": direction,
        "direction_ar": direction_ar,
        "confidence": prediction["confidence"],
        "price": float(latest["close"]),
        "rsi": float(latest["rsi"]),
        "vwap_z": float(latest["vwap_z"]),
        "volume_velocity": float(latest["volume_velocity"]),
        "realized_vol": float(latest["realized_vol"]),
        "trend_5m": int(latest["trend_5m"]),
        "timestamp": stamp(),
    }, features_df

# =========================================================
# ⚡ تعديل الشروط الذكية (Dynamic Entry Logic)
# =========================================================

def entry_context_ok(analysis):
    direction = analysis["direction"]

    if direction not in (-1, 1):
        return False, "اتجاه النموذج محايد"

    if analysis["confidence"] < MIN_CONFIDENCE_THRESHOLD:
        return False, "ثقة النموذج غير كافية"

    if analysis["volume_velocity"] < MIN_VOLUME_VELOCITY:
        return False, "حجم التداول ضعيف"

    if analysis["realized_vol"] < MIN_REALIZED_VOL:
        return False, "التذبذب غير كافٍ"

    if analysis["trend_5m"] != direction:
        return False, "تعارض اتجاه النموذج مع اتجاه 5 دقائق"

    # تأكيد VWAP
    if direction == 1 and analysis["vwap_z"] <= 0:
        return False, "CALL مرفوض: السعر أسفل VWAP"

    if direction == -1 and analysis["vwap_z"] >= 0:
        return False, "PUT مرفوض: السعر أعلى VWAP"

    # ⚡ تعديل فلتر RSI المحسّن للـ PUT والـ CALL:
    if direction == 1 and analysis["rsi"] >= 70:
        return False, "CALL مرفوض: تشبع شرائي"

    if direction == -1:
        # إذا كان RSI منخفضاً جداً (تشبع بيعي)، نقبل الصفقة فقط إذا كان هناك حجم تداول بيعي حاد (Breakout)
        if analysis["rsi"] <= 30 and analysis["volume_velocity"] < BREAKOUT_VOLUME_VELOCITY:
            return False, f"PUT مرفوض: تشبع بيعي (RSI {analysis['rsi']:.1f}) وحجم التداول غير كافٍ للكسر"

    return True, "جميع المؤشرات متوافقة"

def confirm_signal(direction, confidence):
    confirmation = state.get("confirmation", {"direction": None, "count": 0})
    
    # ⚡ التأكيد المرن (Dynamic Confirmation):
    # إذا كانت الثقة مرتفعة جداً (>= 65%) تكفي دورة واحدة (1 Scan) للتنفيذ الفوري
    required_confirms = 1 if confidence >= HIGH_CONFIDENCE_THRESHOLD else 2

    if confirmation.get("direction") == direction:
        confirmation["count"] = int(confirmation.get("count", 0)) + 1
    else:
        confirmation = {"direction": direction, "count": 1}

    state["confirmation"] = confirmation
    save_state()
    
    log.info(
        "تأكيد الإشارة: %s | %d/%d (المطلوب: %d بناءً على ثقة %.1f%%)",
        "CALL" if direction == 1 else "PUT", 
        confirmation["count"], required_confirms, required_confirms, confidence * 100
    )
    return confirmation["count"] >= required_confirms

def reset_confirmation():
    state["confirmation"] = {"direction": None, "count": 0}
    save_state()

# =========================================================
# باقي دالات التنفيذ وإدارة المراكز دون تغيير مع الحفاظ على الأمان
# =========================================================

def open_new_trade():
    if not in_signal_window():
        return
    allowed, reason = trading_allowed()
    if not allowed or has_bot_position_or_order() or not model_trained:
        return
    try:
        analysis, features_df = analyze_market()
        print_analysis(analysis)
        if analysis["direction"] == 0:
            reset_confirmation()
            return
            
        passed, reason = entry_context_ok(analysis)
        if not passed:
            log.info("⛔ عدم استيفاء الشروط: %s", reason)
            reset_confirmation()
            return

        # إرسال مستوى الثقة لدالة التأكيد المرن
        if not confirm_signal(analysis["direction"], analysis["confidence"]):
            return

        option_type = "CALL" if analysis["direction"] == 1 else "PUT"
        contract = select_best_contract(option_type)
        if contract is None:
            reset_confirmation()
            return

        qty = calculate_position_qty(get_equity(), contract["ask"])
        if qty <= 0 or has_bot_position_or_order():
            reset_confirmation()
            return

        execution = submit_entry_order(contract, qty)
        if execution is None:
            reset_confirmation()
            return

        symbol, filled_qty, entry_price = execution["symbol"], execution["qty"], execution["entry_price"]
        state["positions"][symbol] = {
            "symbol": symbol, "qty": filled_qty, "entry_price": entry_price,
            "entry_time": stamp(), "peak_bid": entry_price, "last_bid": entry_price,
            "last_pnl_pct": 0.0, "scaled_out": False, "realized_pnl": 0.0,
            "direction": option_type, "confidence": analysis["confidence"],
            "underlying_price": analysis["price"], "order_id": execution["order_id"],
        }
        state["daily_trades"] = int(state.get("daily_trades", 0)) + 1
        state["last_trade_time"] = stamp()
        reset_confirmation()
        save_state()

        journal_trade("ENTRY", symbol, filled_qty, entry_price, f"{option_type} | Conf {analysis['confidence']:.3f}")
        log.info("✅ تم فتح الصفقة | %s | %s | الكمية %d | الدخول %.2f", symbol, option_type, filled_qty, entry_price)
    except Exception as exc:
        log.exception("خطأ أثناء محاولة الدخول: %s", exc)
