# -*- coding: utf-8 -*-
"""
صَفاء التحليل v64.5 — Smart Entry Pro (Enhanced Execution & Safety)
- معالجة التنفيذ الجزئي (Partial Fill Protection)
- إضافة أمر سريان الخروج بالسوق (Market Fallback) لعقود 0DTE
- SPY Options: 0DTE إلى 3DTE
- Paper Trading افتراضيًا
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

NAME = "صَفاء التحليل - Smart Entry Pro"
VERSION = "v64.5-Robust"

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

BASE_DIR = Path(".")
STATE_FILE = BASE_DIR / "safaa_v645_positions.json"
JOURNAL_FILE = BASE_DIR / "safaa_v645_journal.csv"

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
# 2) Alpaca — Paper افتراضيًا
# =========================================================

API_KEY = os.getenv("ALPACA_API_KEY", "").strip()
API_SECRET = os.getenv("ALPACA_API_SECRET", "").strip()

DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")

STOCK_FEED = os.getenv("ALPACA_STOCK_FEED", "iex")
OPTION_FEED = os.getenv("ALPACA_OPTION_FEED", "indicative")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

if not API_KEY or not API_SECRET:
    raise RuntimeError("ضع ALPACA_API_KEY و ALPACA_API_SECRET في متغيرات البيئة.")

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

def get_clock():
    return trading_get("/v2/clock")

def get_open_orders():
    result = trading_get("/v2/orders", params={"status": "open", "limit": 500, "nested": "true"})
    return result if isinstance(result, list) else []

def get_positions():
    result = trading_get("/v2/positions")
    return result if isinstance(result, list) else []

BOT_ORDER_PREFIX = "SAFAA644_"

def is_bot_order(order):
    client_id = str(order.get("client_order_id", ""))
    return client_id.startswith(BOT_ORDER_PREFIX)

def get_bot_open_orders():
    return [order for order in get_open_orders() if is_bot_order(order)]

def cancel_bot_open_orders():
    for order in get_bot_open_orders():
        order_id = order.get("id")
        if not order_id:
            continue
        try:
            trading_delete(f"/v2/orders/{order_id}")
            log.info("تم إلغاء أمر تابع للبوت: %s", order_id)
        except Exception as exc:
            log.warning("تعذر إلغاء أمر البوت %s: %s", order_id, exc)

def make_client_order_id(prefix="ENTRY"):
    return BOT_ORDER_PREFIX + prefix + "_" + uuid.uuid4().hex[:12]

# =========================================================
# 11) جلب بيانات الأسهم والنموذج
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
    if spy.empty:
        raise RuntimeError("بيانات SPY فارغة.")
    if len(spy) < MIN_RAW_BARS:
        raise RuntimeError(f"بيانات SPY غير كافية: {len(spy)} شمعة.")
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
    df["vwap_z"] = vwap_deviation / vwap_std.replace(0, np.nan)

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
        if spy.empty or len(spy) < MIN_RAW_BARS:
            raise RuntimeError("بيانات SPY غير متاحة أو غير كافية.")

        dataset = build_training_dataset(spy, qqq)
        if len(dataset) < MIN_TRAIN_ROWS:
            raise RuntimeError(f"بيانات التدريب غير كافية: {len(dataset)}")

        X, y = dataset[FEATURES], dataset[TARGET]
        if y.nunique() < 2:
            raise RuntimeError("بيانات التدريب تحتوي على اتجاه واحد فقط.")

        split = int(len(dataset) * 0.75)
        X_train, y_train = X.iloc[:split], y.iloc[:split]
        X_test, y_test = X.iloc[split:], y.iloc[split:]

        candidate = make_pipeline(
            SimpleImputer(strategy="median"),
            ExtraTreesClassifier(
                n_estimators=250, max_depth=8, min_samples_leaf=8,
                class_weight="balanced", random_state=42, n_jobs=-1
            ),
        )
        candidate.fit(X_train, y_train)
        accuracy = float(np.mean(candidate.predict(X_test) == y_test))
        log.info("دقة التحقق الزمني: %.2f%%", accuracy * 100)

        candidate.fit(X, y)
        model = candidate
        model_trained = True
        last_training_time = stamp()
        log.info("اكتمل التدريب على كامل عينات الداتا (%d عينة).", len(dataset))
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
        "probabilities": {str(int(c)): float(p) for c, p in zip(classes, probabilities)},
    }

def analyze_market():
    spy, qqq = fetch_live_market_data()
    fresh, freshness_reason = check_bar_freshness(spy)
    if not fresh:
        raise RuntimeError(freshness_reason)
    features_df = generate_institutional_features(spy, qqq)
    if features_df.empty:
        raise RuntimeError("تعذر بناء مؤشرات السوق.")
    latest = features_df.iloc[-1]
    prediction = predict_direction(features_df)
    if prediction is None:
        raise RuntimeError("النموذج غير جاهز للتوقع.")

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
        "relative_strength": float(latest["relative_strength"]),
        "timestamp": stamp(),
    }, features_df

def entry_context_ok(analysis):
    direction = analysis["direction"]
    if direction not in (-1, 1):
        return False, "اتجاه النموذج محايد"
    if analysis["confidence"] < MIN_CONFIDENCE_THRESHOLD:
        return False, f"الثقة أقل من الحد: {analysis['confidence']:.1%}"
    if analysis["volume_velocity"] < MIN_VOLUME_VELOCITY:
        return False, f"سرعة الحجم ضعيفة: {analysis['volume_velocity']:.2f}"
    if analysis["realized_vol"] < MIN_REALIZED_VOL:
        return False, f"التذبذب غير كافٍ: {analysis['realized_vol']:.4f}"
    if analysis["trend_5m"] != direction:
        return False, "اتجاه 5 دقائق لا يؤكد الإشارة"
    if direction == 1 and analysis["vwap_z"] < 0:
        return False, "CALL مرفوض: السعر أسفل VWAP"
    if direction == -1 and analysis["vwap_z"] > 0:
        return False, "PUT مرفوض: السعر أعلى VWAP"
    if abs(analysis["vwap_z"]) > 2.0:
        return False, "السعر بعيد جدًا عن VWAP"
    if direction == 1 and analysis["rsi"] >= 75:
        return False, "CALL مرفوض: تشبع شرائي"
    if direction == -1 and analysis["rsi"] <= 25:
        return False, "PUT مرفوض: تشبع بيعي"
    return True, "اجتاز فلاتر السوق"

def confirm_signal(direction):
    confirmation = state.get("confirmation", {"direction": None, "count": 0})
    if confirmation.get("direction") == direction:
        confirmation["count"] = int(confirmation.get("count", 0)) + 1
    else:
        confirmation = {"direction": direction, "count": 1}
    state["confirmation"] = confirmation
    save_state()
    log.info(
        "تأكيد الإشارة: %s | %d/%d",
        "CALL" if direction == 1 else "PUT", confirmation["count"], REQUIRED_CONFIRMATIONS
    )
    return confirmation["count"] >= REQUIRED_CONFIRMATIONS

def reset_confirmation():
    state["confirmation"] = {"direction": None, "count": 0}
    save_state()

def trading_allowed():
    reset_daily_state_if_needed()
    try:
        account = get_account()
        if account.get("status") != "ACTIVE" or account.get("trading_blocked"):
            return False, "الحساب غير نشط أو التداول محظور"
        equity = float(account.get("equity", 0) or 0)
        if equity <= 0:
            return False, "قيمة الحساب غير صالحة"
        if state.get("daily_start_equity") is None:
            state["daily_start_equity"] = equity
            save_state()

        start_equity = float(state["daily_start_equity"])
        daily_change = (equity - start_equity) / start_equity
        if daily_change <= -MAX_DAILY_LOSS_PCT:
            return False, f"تم الوصول إلى حد الخسارة اليومية {daily_change:.2%}"
        if state["daily_trades"] >= MAX_TRADES_PER_DAY:
            return False, "تم بلوغ الحد اليومي للصفقات"
        if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            return False, "تم بلوغ حد الخسائر المتتالية"

        last_loss = state.get("last_loss_time")
        if last_loss:
            last_loss_dt = datetime.fromisoformat(last_loss)
            if last_loss_dt.tzinfo is None:
                last_loss_dt = last_loss_dt.replace(tzinfo=NY)
            elapsed = (now_ny() - last_loss_dt.astimezone(NY)).total_seconds() / 60
            if elapsed < COOLDOWN_AFTER_LOSS_MIN:
                return False, f"فترة تهدئة بعد خسارة: {elapsed:.1f} دقيقة"

        save_state()
        return True, "مسموح"
    except Exception as exc:
        log.exception("تعذر التحقق من الحساب: %s", exc)
        return False, "تعذر التحقق من صلاحية التداول"

def fetch_option_contracts(option_type):
    today = now_ny().date()
    expiration_start = today + timedelta(days=MIN_DTE)
    expiration_end = today + timedelta(days=MAX_DTE)
    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "expiration_date_gte": expiration_start.isoformat(),
        "expiration_date_lte": expiration_end.isoformat(),
        "type": "call" if option_type == "CALL" else "put",
        "limit": 1000,
    }
    result = trading_get("/v2/options/contracts", params=params)
    contracts = result.get("option_contracts", [])
    return contracts if isinstance(contracts, list) else []

def fetch_option_snapshot(symbol):
    url = f"{DATA_URL}/v1beta1/options/snapshots/{symbol}"
    result = alpaca_request("GET", url, params={"feed": OPTION_FEED})
    if isinstance(result, dict):
        if symbol in result:
            return result[symbol]
        if "latestQuote" in result or "latest_quote" in result:
            return result
    return {}

def extract_quote(snapshot):
    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    bid = float(quote.get("bp") or quote.get("bid_price") or 0)
    ask = float(quote.get("ap") or quote.get("ask_price") or 0)
    return bid, ask

def extract_greeks(snapshot):
    greeks = snapshot.get("greeks") or {}
    delta = greeks.get("delta")
    iv = snapshot.get("impliedVolatility") or snapshot.get("implied_volatility") or snapshot.get("iv")
    try:
        delta = float(delta)
    except (TypeError, ValueError):
        delta = None
    try:
        iv = float(iv)
    except (TypeError, ValueError):
        iv = None
    return delta, iv

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
        spread_pct = (ask - bid) / mid
        if spread_pct > MAX_SPREAD_PCT:
            return None, f"السبريد مرتفع: {spread_pct:.2%}"

        delta, iv = extract_greeks(snapshot)
        if delta is None:
            return None, "Delta غير متاحة"
        if not (MIN_DELTA <= abs(delta) <= MAX_DELTA):
            return None, f"Delta خارج النطاق: {delta:.3f}"
        if iv is None:
            return None, "IV غير متاحة"
        if iv > MAX_IMPLIED_VOLATILITY:
            return None, f"IV مرتفعة: {iv:.2%}"

        open_interest = contract.get("open_interest") or snapshot.get("open_interest") or snapshot.get("openInterest")
        if open_interest is not None:
            try:
                open_interest = int(open_interest)
            except (TypeError, ValueError):
                open_interest = None
        if open_interest is not None and open_interest < MIN_OPEN_INTEREST:
            return None, f"الفائدة المفتوحة منخفضة: {open_interest}"

        return {
            "symbol": symbol, "bid": bid, "ask": ask, "mid": mid,
            "spread_pct": spread_pct, "delta": delta, "iv": iv,
            "open_interest": open_interest, "snapshot": snapshot,
        }, "مقبول"
    except Exception as exc:
        return None, f"خطأ في فحص العقد: {exc}"

def select_best_contract(option_type):
    contracts = fetch_option_contracts(option_type)
    if not contracts:
        log.warning("لم يتم العثور على عقود %s.", option_type)
        return None
    candidates = []
    contracts = sorted(contracts, key=lambda c: abs(float(c.get("strike_price", 0) or 0)))
    for contract in contracts[:MAX_CONTRACTS_TO_CHECK]:
        evaluated, reason = evaluate_contract(contract)
        if evaluated is None:
            continue
        score = (abs(abs(evaluated["delta"]) - 0.52) * 2) + evaluated["spread_pct"]
        evaluated["score"] = score
        candidates.append(evaluated)

    if not candidates:
        log.warning("لم يجتز أي عقد فلاتر السيولة والسعر.")
        return None

    candidates.sort(key=lambda item: item["score"])
    best = candidates[0]
    log.info("أفضل عقد: %s | Bid %.2f | Ask %.2f | Delta %.3f", best["symbol"], best["bid"], best["ask"], best["delta"])
    return best

def calculate_position_qty(equity, premium):
    if equity <= 0 or premium <= 0:
        return 0
    risk_budget = equity * min(RISK_PER_TRADE_PCT, MAX_RISK_CAP)
    contract_cost = premium * 100
    if contract_cost <= 0:
        return 0
    qty = int(risk_budget // contract_cost)
    return min(max(qty, 0), 30)

def print_analysis(analysis):
    log.info(
        "💓 SPY $%.2f | الإشارة: %s | الثقة: %.1f%% | RSI: %.1f | اتجاه 5د: %s | VWAP-Z: %.2f",
        analysis["price"], analysis["direction_ar"], analysis["confidence"] * 100,
        analysis["rsi"], analysis["trend_5m"], analysis["vwap_z"]
    )

def journal_trade(action, symbol, qty, price, reason, pnl=None):
    row = {
        "timestamp": stamp(), "action": action, "symbol": symbol,
        "qty": qty, "price": price, "reason": reason, "pnl": pnl,
    }
    df = pd.DataFrame([row])
    write_header = not JOURNAL_FILE.exists()
    df.to_csv(JOURNAL_FILE, mode="a", header=write_header, index=False, encoding="utf-8-sig")

def get_order(order_id):
    return trading_get(f"/v2/orders/{order_id}")

def wait_for_order(order_id, timeout=EXECUTION_TIMEOUT):
    start = time.time()
    while time.time() - start < timeout:
        try:
            order = get_order(order_id)
            if order.get("status") in ("filled", "canceled", "rejected", "expired"):
                return order
        except Exception as exc:
            log.warning("تعذر تحديث حالة الأمر: %s", exc)
        time.sleep(0.5)
    return get_order(order_id)

# =========================================================
# 12) معالجة التنفيذ المتقدمة (Fixed Partial Fill)
# =========================================================

def submit_entry_order(contract, qty):
    if qty <= 0:
        return None
    symbol = contract["symbol"]
    limit_price = round((contract["bid"] + contract["ask"]) / 2, 2)
    if limit_price <= 0:
        return None
    payload = {
        "symbol": symbol, "qty": str(qty), "side": "buy", "type": "limit",
        "time_in_force": "day", "limit_price": f"{limit_price:.2f}",
        "client_order_id": make_client_order_id("ENTRY"),
    }
    log.info("إرسال أمر شراء | %s | الكمية %d | السعر %.2f", symbol, qty, limit_price)
    try:
        order = trading_post("/v2/orders", payload)
        order_id = order.get("id")
        if not order_id:
            return None
        final_order = wait_for_order(order_id, timeout=EXECUTION_TIMEOUT)
        
        # إذا لم يكتمل بالكامل، نلغي المتبقي ونلتقط الشراء الجزئي
        if final_order.get("status") not in ("filled", "canceled", "rejected"):
            try:
                trading_delete(f"/v2/orders/{order_id}")
            except Exception:
                pass
            time.sleep(0.5)
            try:
                final_order = get_order(order_id)
            except Exception:
                pass

        filled_qty = int(float(final_order.get("filled_qty", 0) or 0))
        filled_price = float(final_order.get("filled_avg_price", 0) or 0)
        
        if filled_qty <= 0 or filled_price <= 0:
            log.warning("لم يتم تنفيذ أمر الشراء | الحالة: %s", final_order.get("status"))
            return None
            
        log.info("تم الشراء فعليًا: %d عقد بسعر %.2f", filled_qty, filled_price)
        return {"symbol": symbol, "qty": filled_qty, "entry_price": filled_price, "order_id": order_id}
    except Exception as exc:
        log.exception("فشل إرسال أمر الشراء: %s", exc)
        return None

def submit_exit_order(symbol, qty, reason):
    if qty <= 0:
        return None
    try:
        snapshot = fetch_option_snapshot(symbol)
        bid, _ = extract_quote(snapshot)
        
        # استخدام أمر Limit بسعر الـ Bid لتسريع التنفيذ
        limit_price = round(bid, 2) if bid > 0 else 0.01
        payload = {
            "symbol": symbol, "qty": str(qty), "side": "sell", "type": "limit",
            "time_in_force": "day", "limit_price": f"{limit_price:.2f}",
            "client_order_id": make_client_order_id("EXIT"),
        }
        order = trading_post("/v2/orders", payload)
        order_id = order.get("id")
        if not order_id:
            return None
            
        final_order = wait_for_order(order_id, timeout=EXECUTION_TIMEOUT)
        
        # إذا فشل أمر الـ Limit، تحويل تلقائي إلى أمر بسعر السوق (Market Fallback) لمنع تعليق 0DTE
        if final_order.get("status") != "filled":
            try:
                trading_delete(f"/v2/orders/{order_id}")
            except Exception:
                pass
            
            log.warning("تنبيه: فشل أمر إغلاق Limit لـ %s. تحويل إلى Market Order لضمان الخروج.", symbol)
            mkt_payload = {
                "symbol": symbol, "qty": str(qty), "side": "sell", "type": "market",
                "time_in_force": "day", "client_order_id": make_client_order_id("EXIT_MKT"),
            }
            mkt_order = trading_post("/v2/orders", mkt_payload)
            final_order = wait_for_order(mkt_order.get("id"), timeout=EXECUTION_TIMEOUT)

        filled_qty = int(float(final_order.get("filled_qty", 0) or 0))
        filled_price = float(final_order.get("filled_avg_price", 0) or 0)
        
        if filled_qty <= 0:
            return None
            
        return {"qty": filled_qty, "price": filled_price, "reason": reason}
    except Exception as exc:
        log.exception("خطأ في أمر البيع: %s", exc)
        return None

def get_position_map():
    positions = get_positions()
    return {p.get("symbol"): p for p in positions if p.get("symbol")}

def get_tracked_positions():
    current_positions = get_position_map()
    tracked = state.get("positions", {})
    result = []
    for symbol, metadata in list(tracked.items()):
        if symbol not in current_positions:
            tracked.pop(symbol, None)
            continue
        result.append((symbol, metadata, current_positions[symbol]))
    state["positions"] = tracked
    save_state()
    return result

def get_option_mark(symbol):
    snapshot = fetch_option_snapshot(symbol)
    bid, ask = extract_quote(snapshot)
    if bid <= 0 or ask <= 0:
        return None
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}

def calculate_position_pnl(entry_price, current_price):
    return (current_price - entry_price) / entry_price if entry_price > 0 else 0.0

def register_closed_trade(pnl_amount):
    if pnl_amount < 0:
        state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
        state["last_loss_time"] = stamp()
    elif pnl_amount > 0:
        state["consecutive_losses"] = 0
        state["last_win_time"] = stamp()
    save_state()

def manage_one_position(symbol, metadata, alpaca_position):
    try:
        qty = int(abs(float(alpaca_position.get("qty", 0) or 0)))
        if qty <= 0:
            return
        entry_price = float(metadata.get("entry_price", 0) or 0)
        if entry_price <= 0:
            return
        quote = get_option_mark(symbol)
        if quote is None:
            return

        bid = quote["bid"]
        pnl_pct = calculate_position_pnl(entry_price, bid)
        peak_bid = max(float(metadata.get("peak_bid", entry_price)), bid)
        metadata["peak_bid"] = peak_bid
        metadata["last_bid"] = bid
        metadata["last_pnl_pct"] = pnl_pct

        entry_time = datetime.fromisoformat(metadata["entry_time"])
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=NY)
        elapsed_minutes = (now_ny() - entry_time.astimezone(NY)).total_seconds() / 60

        reason = None
        if pnl_pct <= -STOP_LOSS_PCT:
            reason = "وقف خسارة"
        elif pnl_pct >= TAKE_PROFIT_PCT:
            reason = "تحقيق الهدف"
        elif peak_bid >= entry_price * 1.25 and bid <= entry_price * (1 + BREAKEVEN_BUFFER_PCT):
            reason = "حماية رأس المال"
        elif peak_bid >= entry_price * 1.40 and bid <= max(entry_price * 1.10, peak_bid * 0.75):
            reason = "خروج تتبع الأرباح"
        elif elapsed_minutes >= MAX_TRADE_MINUTES:
            reason = "انتهاء مدة الصفقة"
        elif now_ny().time() >= EOD_CLOSE_TIME:
            reason = "إغلاق نهاية الجلسة"

        if reason is None and not metadata.get("scaled_out", False) and pnl_pct >= 0.50 and qty >= 2:
            scale_qty = min(max(1, int(math.floor(qty * SCALE_OUT_PCT))), qty - 1)
            result = submit_exit_order(symbol, scale_qty, "جني ربح جزئي")
            if result:
                pnl_amount = (result["price"] - entry_price) * 100 * result["qty"]
                metadata["scaled_out"] = True
                metadata["realized_pnl"] = float(metadata.get("realized_pnl", 0)) + pnl_amount
                journal_trade("PARTIAL_EXIT", symbol, result["qty"], result["price"], "جني ربح جزئي", pnl_amount)
                save_state()

        if reason:
            result = submit_exit_order(symbol, qty, reason)
            if result:
                pnl_amount = (result["price"] - entry_price) * 100 * result["qty"]
                metadata["realized_pnl"] = float(metadata.get("realized_pnl", 0)) + pnl_amount
                journal_trade("EXIT", symbol, result["qty"], result["price"], reason, pnl_amount)
                remaining_qty = max(0, qty - result["qty"])
                if remaining_qty == 0:
                    register_closed_trade(float(metadata.get("realized_pnl", 0)))
                    state["positions"].pop(symbol, None)
                else:
                    metadata["qty"] = remaining_qty
                save_state()
                log.info("إغلاق %s | السبب: %s | الربح: %.2f", symbol, reason, pnl_amount)
        else:
            log.info("📊 متابعة %s | Bid %.2f | P/L %.2f%%", symbol, bid, pnl_pct * 100)

        save_state()
    except Exception as exc:
        log.exception("خطأ في إدارة المركز %s: %s", symbol, exc)

def manage_open_positions():
    for symbol, metadata, alpaca_position in get_tracked_positions():
        manage_one_position(symbol, metadata, alpaca_position)

def has_bot_position_or_order():
    return bool(state.get("positions")) or bool(get_bot_open_orders())

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
        if not passed or not confirm_signal(analysis["direction"]):
            if not passed:
                reset_confirmation()
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

def should_train():
    if not model_trained or last_training_time is None:
        return True
    try:
        last = datetime.fromisoformat(last_training_time)
        if last.tzinfo is None:
            last = last.replace(tzinfo=NY)
        return (now_ny() - last.astimezone(NY)).total_seconds() >= 4 * 3600
    except Exception:
        return True

def print_status():
    try:
        account = get_account()
        log.info(
            "💎 %s %s | Equity: $%.2f | صفقات اليوم: %d/%d",
            NAME, VERSION, float(account.get("equity", 0) or 0),
            state.get("daily_trades", 0), MAX_TRADES_PER_DAY
        )
    except Exception as exc:
        log.warning("تعذر إنشاء تقرير الحالة: %s", exc)

def main():
    log.info("🚀 بدء تشغيل %s | %s", NAME, VERSION)
    if "paper-api" not in TRADING_URL:
        raise RuntimeError("تم إيقاف التشغيل: رابط التداول ليس Paper.")

    reset_daily_state_if_needed()
    last_status_time = 0

    while True:
        try:
            reset_daily_state_if_needed()
            if should_train():
                train_model()
            if state.get("positions"):
                manage_open_positions()
            if in_signal_window():
                open_new_trade()
            if time.time() - last_status_time >= 300:
                print_status()
                last_status_time = time.time()
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            log.info("تم إيقاف البوت يدويًا.")
            break
        except Exception as exc:
            log.exception("خطأ في الحلقة الرئيسية: %s", exc)
            time.sleep(10)

if __name__ == "__main__":
    main()
