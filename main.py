# -*- coding: utf-8 -*-
"""
صَفاء التحليل v65.0 — Quant Pro System
- LightGBM لسرعة ودقة التحليل
- Fractional Kelly Criterion لإدارة حجم الصفقات
- Vertical Spreads (Multi-leg) لتحييد التآكل الزمني وتحديد أقصى خسارة
"""

import os
import json
import time
import math
import logging
import uuid
import csv
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import numpy as np
import pandas as pd

# المكتبات الجديدة للذكاء الاصطناعي
import lightgbm as lgb
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer

# =========================================================
# 1) الإعدادات العامة
# =========================================================

NAME = "صَفاء التحليل - Quant Pro"
VERSION = "v65.0"

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

BASE_DIR = Path(".")
STATE_FILE = BASE_DIR / "safaa_v65_positions.json"
JOURNAL_FILE = BASE_DIR / "safaa_v65_journal.csv"

SYMBOL = "SPY"
BENCHMARK = "QQQ"

TIMEFRAME = "1Min"
HISTORY_DAYS = 90
LIVE_DAYS = 5

MIN_RAW_BARS = 1200
FORECAST_HORIZON = 5

# =========================================================
# 2) إعدادات Alpaca
# =========================================================

API_KEY = (os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID") or "").strip()
API_SECRET = (os.getenv("ALPACA_API_SECRET") or os.getenv("APCA_API_SECRET_KEY") or "").strip()

DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")

STOCK_FEED = os.getenv("ALPACA_STOCK_FEED", "iex")

if not API_KEY or not API_SECRET:
    raise RuntimeError("مفاتيح Alpaca غير موجودة.")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

# =========================================================
# 3) إعدادات الإشارات والمخاطر
# =========================================================

MIN_CONFIDENCE_THRESHOLD = 0.56
HIGH_CONFIDENCE_THRESHOLD = 0.65  
MIN_VOLUME_VELOCITY = 1.20
BREAKOUT_VOLUME_VELOCITY = 2.00   

MAX_RISK_CAP = 0.05  # أقصى مخاطرة مسموحة للمحفظة في الصفقة الواحدة (5%)
DEFAULT_RISK_PCT = 0.01

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
SIGNAL_START = dtime(9, 45)
SIGNAL_END = dtime(15, 30)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(NAME)

# =========================================================
# 4) أدوات الوقت والحالة
# =========================================================

def now_ny(): return datetime.now(NY)
def today_ny(): return now_ny().date().isoformat()
def stamp(): return now_ny().isoformat(timespec="seconds")

def is_market_open():
    now = now_ny()
    if now.weekday() >= 5: return False
    return MARKET_OPEN <= now.time() < MARKET_CLOSE

def in_signal_window():
    now = now_ny()
    if not is_market_open(): return False
    return SIGNAL_START <= now.time() <= SIGNAL_END

def save_json(path, data):
    temp = Path(str(path) + ".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    temp.replace(path)

def load_json(path, default):
    if not path.exists(): return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

state = load_json(STATE_FILE, {"date": today_ny(), "positions": {}, "confirmation": {"direction": None, "count": 0}})

def save_state(): save_json(STATE_FILE, state)

# =========================================================
# 5) طلبات Alpaca العامة
# =========================================================

def alpaca_request(method, url, *, params=None, payload=None, timeout=15):
    response = requests.request(method=method, url=url, headers=HEADERS, params=params, json=payload, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"Alpaca HTTP {response.status_code}: {response.text[:500]}")
    return response.json() if response.text else {}

def trading_get(path, params=None): return alpaca_request("GET", TRADING_URL + path, params=params)
def trading_post(path, payload): return alpaca_request("POST", TRADING_URL + path, payload=payload)
def get_equity(): return float(trading_get("/v2/account").get("equity", 0))

# =========================================================
# 6) إدارة المخاطر: إحصائيات السجل ومعيار كيلي
# =========================================================

def get_historical_stats():
    """قراءة السجل لحساب نسبة النجاح ومتوسط الربح/الخسارة لتغذية معيار كيلي"""
    if not JOURNAL_FILE.exists():
        return 0.55, 0.15, 0.08  # قيم افتراضية مبدئية

    wins, losses = 0, 0
    total_win_pct, total_loss_pct = 0.0, 0.0

    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("action") == "EXIT":
                    pnl = float(row.get("pnl_pct", 0))
                    if pnl > 0:
                        wins += 1
                        total_win_pct += pnl
                    else:
                        losses += 1
                        total_loss_pct += abs(pnl)
    except Exception as e:
        log.error("خطأ في قراءة السجل: %s", e)
        return 0.55, 0.15, 0.08

    total_trades = wins + losses
    if total_trades < 5:
        return 0.55, 0.15, 0.08

    win_rate = wins / total_trades
    avg_win = (total_win_pct / wins) if wins > 0 else 0.10
    avg_loss = (total_loss_pct / losses) if losses > 0 else 0.05
    return win_rate, avg_win, avg_loss

def calculate_dynamic_position_qty(equity, spread_cost, model_confidence):
    win_rate, avg_win_pct, avg_loss_pct = get_historical_stats()
    
    if win_rate <= 0 or avg_win_pct <= 0 or avg_loss_pct <= 0:
        risk_amount = equity * DEFAULT_RISK_PCT
        return max(1, int(risk_amount // (spread_cost * 100)))

    R = avg_win_pct / avg_loss_pct
    kelly_pct = win_rate - ((1 - win_rate) / R)
    
    # الربع كيلي لتقليل التقلبات الشديدة
    fractional_kelly = max(0, kelly_pct / 4.0)
    
    # ضبط المخاطرة بناءً على ثقة الذكاء الاصطناعي اللحظية
    confidence_multiplier = model_confidence / HIGH_CONFIDENCE_THRESHOLD
    adjusted_risk_pct = min(fractional_kelly * confidence_multiplier, MAX_RISK_CAP)
    
    risk_amount = equity * adjusted_risk_pct
    qty = int(risk_amount // (spread_cost * 100))
    
    log.info("معيار كيلي | Win Rate: %.1f%% | Risk Pct: %.2f%% | Qty: %d", win_rate*100, adjusted_risk_pct*100, qty)
    return max(1, qty)

# =========================================================
# 7) جلب البيانات والذكاء الاصطناعي (LightGBM)
# =========================================================

def fetch_stock_bars(symbol, days=LIVE_DAYS, limit=10000):
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    params = {"timeframe": TIMEFRAME, "start": start.isoformat(), "end": end.isoformat(), "limit": limit, "feed": STOCK_FEED, "sort": "asc"}
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    bars = alpaca_request("GET", url, params=params).get("bars", [])
    if not bars: return pd.DataFrame()

    df = pd.DataFrame(bars).rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    for col in ["open", "high", "low", "close", "volume", "vwap"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["close", "volume"])

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def generate_institutional_features(spy, qqq):
    df = spy.copy()
    close, volume = df["close"], df["volume"].replace(0, np.nan)

    df["ret_1"] = close.pct_change(1)
    df["ret_5"] = close.pct_change(5)
    df["ema_9"] = close.ewm(span=9, adjust=False).mean()
    df["ema_21"] = close.ewm(span=21, adjust=False).mean()
    df["rsi"] = calculate_rsi(close)
    
    df["macd"] = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()

    # معالجة VWAP وتقييد الشذوذ
    if "vwap" not in df.columns:
        df["vwap"] = ((df["high"] + df["low"] + close) / 3 * volume).cumsum() / volume.cumsum()
    vwap_std = (close - df["vwap"]).rolling(30, min_periods=10).std()
    df["vwap_z"] = ((close - df["vwap"]) / vwap_std.replace(0, np.nan)).clip(-5.0, 5.0)

    df["volume_velocity"] = volume / volume.rolling(20, min_periods=10).mean().replace(0, np.nan)
    df["realized_vol"] = df["ret_1"].rolling(20, min_periods=10).std() * math.sqrt(390)

    five_min = close.resample("5min").last().dropna()
    trend_5m = pd.Series(np.where(five_min.ewm(span=9).mean() > five_min.ewm(span=21).mean(), 1, -1), index=five_min.index)
    df["trend_5m"] = trend_5m.reindex(df.index, method="ffill")

    return df.replace([np.inf, -np.inf], np.nan)

FEATURES = ["ret_1", "ret_5", "ema_9", "ema_21", "rsi", "macd", "macd_signal", "vwap_z", "volume_velocity", "realized_vol", "trend_5m"]
TARGET = "target"

def build_training_dataset(spy, qqq):
    df = generate_institutional_features(spy, qqq)
    future_return = (df["close"].shift(-FORECAST_HORIZON) / df["close"]) - 1
    df[TARGET] = np.where(future_return > 0.00035, 1, np.where(future_return < -0.00035, -1, 0))
    df = df.dropna(subset=FEATURES + [TARGET]).copy()
    return df[df[TARGET] != 0]

model, model_trained = None, False

def train_model():
    global model, model_trained
    log.info("بدء تدريب الذكاء الاصطناعي (LightGBM)...")
    try:
        spy = fetch_stock_bars(SYMBOL, HISTORY_DAYS)
        qqq = fetch_stock_bars(BENCHMARK, HISTORY_DAYS)
        dataset = build_training_dataset(spy, qqq)
        X, y = dataset[FEATURES], dataset[TARGET]

        model = make_pipeline(
            StandardScaler(),
            lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, max_depth=6, class_weight="balanced", random_state=42, n_jobs=-1, force_col_wise=True)
        )
        model.fit(X, y)
        model_trained = True
        log.info("✅ اكتمل تدريب نموذج LightGBM بنجاح.")
        return True
    except Exception as exc:
        log.error("❌ فشل التدريب: %s", exc)
        return False

def analyze_market():
    spy, qqq = fetch_stock_bars(SYMBOL, LIVE_DAYS), fetch_stock_bars(BENCHMARK, LIVE_DAYS)
    features_df = generate_institutional_features(spy, qqq)
    latest = features_df.iloc[-1]
    
    probabilities = model.predict_proba(pd.DataFrame([latest[FEATURES]]))[0]
    best_index = int(np.argmax(probabilities))
    direction = int(model.classes_[best_index])
    
    return {
        "direction": direction,
        "confidence": float(probabilities[best_index]),
        "price": float(latest["close"]),
        "rsi": float(latest["rsi"]),
        "vwap_z": float(latest["vwap_z"]),
        "volume_velocity": float(latest["volume_velocity"]),
        "trend_5m": int(latest["trend_5m"]),
    }

# =========================================================
# 8) استراتيجية التنفيذ والمركبات (Vertical Spreads)
# =========================================================

def get_options_chain(underlying_price, is_call):
    """محاكاة جلب سلسلة الخيارات وتصفيتها (تحتاج ربط مع نقطة نهاية الخيارات في Alpaca)"""
    # في التطبيق الحقيقي، سيتم طلب العقود المتاحة لليوم 0DTE من Alpaca v2 Options API
    # سنقوم بإنشاء هيكل تجريبي متوافق مع المنطق
    log.info("جلب سلسلة الخيارات الحية لـ %s...", "CALL" if is_call else "PUT")
    chain = []
    # محاكاة لعقود حول السعر الحالي
    base_strike = round(underlying_price)
    for offset in range(-3, 4):
        strike = base_strike + offset
        delta = 0.50 if is_call else -0.50 # تقريبي
        chain.append({
            "symbol": f"{SYMBOL}261031{'C' if is_call else 'P'}00{int(strike*1000)}",
            "strike": strike,
            "ask": 1.50, # محاكاة
            "bid": 1.40,
            "delta": delta + (offset * 0.05 * (-1 if is_call else 1)) 
        })
    return chain

def find_contract_closest_to_delta(chain, target_delta):
    return min(chain, key=lambda x: abs(x.get("delta", 0) - target_delta), default=None)

def submit_spread_entry_order(direction, underlying_price, qty):
    is_call = (direction == 1)
    chain = get_options_chain(underlying_price, is_call)
    
    target_delta = 0.50 if is_call else -0.50
    buy_leg = find_contract_closest_to_delta(chain, target_delta)
    
    if not buy_leg: return None
    
    sell_strike = buy_leg["strike"] + 1.0 if is_call else buy_leg["strike"] - 1.0
    sell_leg = next((c for c in chain if c["strike"] == sell_strike), None)

    if not sell_leg: return None

    spread_cost = buy_leg["ask"] - sell_leg["bid"]
    
    payload = {
        "class": "multileg",
        "time_in_force": "day",
        "order_class": "simple",
        "legs": [
            {"symbol": buy_leg["symbol"], "side": "buy", "ratio_qty": str(qty)},
            {"symbol": sell_leg["symbol"], "side": "sell", "ratio_qty": str(qty)}
        ]
    }
    
    try:
        # response = trading_post("/v2/orders", payload=payload)
        log.info(f"تم إرسال أمر {'Bull Call' if is_call else 'Bear Put'} Spread. التكلفة التقديرية للعقد: ${spread_cost*100:.2f}")
        # return response
        return {"order_id": "TEST_SPREAD_123", "cost": spread_cost} # محاكاة لنجاح الأمر
    except Exception as e:
        log.error(f"فشل إرسال أمر Spread: {e}")
        return None

# =========================================================
# 9) دورة العمل الرئيسية
# =========================================================

def run():
    log.info(f"تشغيل {NAME} {VERSION}...")
    if not train_model():
        log.error("لا يمكن الاستمرار بدون نموذج مدرب.")
        return

    while True:
        try:
            if not in_signal_window():
                log.info("السوق مغلق أو خارج نافذة التداول.")
                time.sleep(60)
                continue

            analysis = analyze_market()
            direction = analysis["direction"]
            
            if direction != 0 and analysis["confidence"] >= MIN_CONFIDENCE_THRESHOLD:
                log.info(f"إشارة {('CALL' if direction==1 else 'PUT')} محتملة | ثقة: {analysis['confidence']:.2f}")
                
                # حساب حجم الصفقة الديناميكي
                equity = get_equity()
                qty = calculate_dynamic_position_qty(equity, 1.0, analysis["confidence"])
                
                # إرسال صفقة الحماية المركبة
                submit_spread_entry_order(direction, analysis["price"], qty)
                
                log.info("تم معالجة الإشارة بنجاح. استراحة لمدة دقيقة...")
                time.sleep(60)
            else:
                time.sleep(15)

        except Exception as e:
            log.error(f"خطأ غير متوقع في الدورة الرئيسية: {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
