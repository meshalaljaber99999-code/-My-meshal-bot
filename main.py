# -*- coding: utf-8 -*-
"""
صَفاء التحليل v66.3 — The Volatility Hunter (Robust Data Handling)
- حماية متقدمة ضد أخطاء فقدان الأعمدة (KeyError: 'close')
- مستشعر التذبذب واستغلال الانفجارات (Ratio Backspreads & Straddles)
- الوقف المتحرك المتسارع (Accelerating Trailing Stop) لاصطياد التدبيلات
- الخزنة الاستراتيجية (Strategic Sweep) وتنويع الأصول (SPY & QQQ)
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

import lightgbm as lgb
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

# =========================================================
# 1) الإعدادات العامة والتنويع المؤسسي
# =========================================================

NAME = "صَفاء التحليل - Volatility Hunter (Pro)"
VERSION = "v66.3"

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

BASE_DIR = Path(".")
STATE_FILE = BASE_DIR / "safaa_v66_3_positions.json"
JOURNAL_FILE = BASE_DIR / "safaa_v66_3_journal.csv"

TRADE_SYMBOLS = ["SPY", "QQQ"]
TIMEFRAME = "1Min"
HISTORY_DAYS = 90
LIVE_DAYS = 5
FORECAST_HORIZON = 5

# =========================================================
# 2) إعدادات Alpaca
# =========================================================

API_KEY = (os.getenv("ALPACA_API_KEY") or "").strip()
API_SECRET = (os.getenv("ALPACA_API_SECRET") or "").strip()
DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
STOCK_FEED = "iex"

HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET, "Content-Type": "application/json"}

# =========================================================
# 3) المخاطر، التوزيع الاستراتيجي، وحماية الأرباح
# =========================================================

MAX_ACTIVE_TRADING_CAPITAL = 100000.0  
MAX_ORDER_CHUNK_SIZE = 10              

MIN_CONFIDENCE_THRESHOLD = 0.58
MAX_RISK_CAP = 0.05  
DEFAULT_RISK_PCT = 0.01

HARD_STOP_LOSS_PCT = -0.30       
BREAKEVEN_ACTIVATION_PCT = 0.35  
TRAILING_STOP_DISTANCE = 0.15    
TAKE_PROFIT_PCT = 0.90           

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
EOD_CLOSE_TIME = dtime(15, 50)   
SIGNAL_START = dtime(9, 45)
SIGNAL_END = dtime(15, 30)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(NAME)

# =========================================================
# 4) أدوات الوقت والخزنة الاستراتيجية
# =========================================================

def now_ny(): return datetime.now(NY)
def today_ny(): return now_ny().date().isoformat()
def stamp(): return now_ny().isoformat(timespec="seconds")
def is_market_open(): return MARKET_OPEN <= now_ny().time() < MARKET_CLOSE and now_ny().weekday() < 5
def in_signal_window(): return SIGNAL_START <= now_ny().time() <= SIGNAL_END and is_market_open()

def load_json(path, default):
    if not path.exists(): return default
    with open(path, "r", encoding="utf-8") as f: return json.load(f)

def save_json(path, data):
    temp = Path(str(path) + ".tmp")
    with open(temp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    temp.replace(path)

state = load_json(STATE_FILE, {"date": today_ny(), "positions": {}, "vault": 0.0})
def save_state(): save_json(STATE_FILE, state)

# =========================================================
# 5) طلبات API و التوزيع الاستراتيجي
# =========================================================

def alpaca_request(method, url, *, params=None, payload=None, timeout=15):
    try:
        response = requests.request(method=method, url=url, headers=HEADERS, params=params, json=payload, timeout=timeout)
        if response.ok and response.text:
            return response.json()
    except Exception as e:
        log.error(f"خطأ في الاتصال مع Alpaca API: {e}")
    return {}

def get_raw_equity():
    data = alpaca_request("GET", TRADING_URL + "/v2/account")
    return float(data.get("equity", 0)) if data else 0.0

def get_managed_equity():
    raw_equity = get_raw_equity()
    if raw_equity > MAX_ACTIVE_TRADING_CAPITAL:
        excess = raw_equity - MAX_ACTIVE_TRADING_CAPITAL
        state["vault"] = state.get("vault", 0.0) + excess
        save_state()
        log.warning(f"🏦 [تأمين السيولة]: تم سحب {excess:,.2f}$ للخزنة لحماية الكفاءة.")
        return MAX_ACTIVE_TRADING_CAPITAL
    return raw_equity

# =========================================================
# 6) إدارة رأس المال (Kelly Criterion)
# =========================================================

def calculate_dynamic_position_qty(equity, spread_cost, model_confidence):
    win_rate, avg_win_pct, avg_loss_pct = 0.55, 0.15, 0.08 
    R = avg_win_pct / avg_loss_pct
    kelly_pct = win_rate - ((1 - win_rate) / R)
    adjusted_risk_pct = min(max(0, kelly_pct / 4.0) * (model_confidence / 0.65), MAX_RISK_CAP)
    return max(1, int((equity * adjusted_risk_pct) // (spread_cost * 100)))

# =========================================================
# 7) مستشعر حالة السوق والبيانات المحمية (Robust Data Fetching)
# =========================================================

def fetch_bars(symbol, days=LIVE_DAYS):
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    params = {"timeframe": TIMEFRAME, "start": start.isoformat(), "end": end.isoformat(), "limit": 10000, "feed": STOCK_FEED, "sort": "asc"}
    
    data = alpaca_request("GET", f"{DATA_URL}/v2/stocks/{symbol}/bars", params=params)
    bars = data.get("bars", [])
    
    if not bars:
        log.warning(f"⚠️ تحذير: لم يتم استرجاع أي بيانات لـ {symbol} من Alpaca.")
        return pd.DataFrame()
        
    df = pd.DataFrame(bars)
    # التأكد من وجود الأعمدة المطلوبة قبل إعادة التسمية
    expected_cols = {"t", "o", "h", "l", "c", "v"}
    if not expected_cols.issubset(df.columns):
        log.error(f"❌ الأعمدة المسترجعة غير مطابقة لـ {symbol}: {df.columns}")
        return pd.DataFrame()
        
    df = df.rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.set_index("timestamp").sort_index().dropna(subset=["close", "volume"])

def calculate_rsi(series, p=14):
    gain = series.diff().clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    loss = (-series.diff().clip(upper=0)).ewm(alpha=1/p, adjust=False).mean()
    return 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

def detect_market_regime(df):
    if df.empty or "close" not in df.columns:
        return "TRENDING"
    recent_vol = df["close"].pct_change().rolling(30).std().iloc[-1] * math.sqrt(390)
    bollinger_width = (df["high"].rolling(20).max() - df["low"].rolling(20).min()) / df["close"].iloc[-1]
    
    if bollinger_width.iloc[-1] < 0.0015 and recent_vol < 0.15:
        return "COMPRESSED"
    elif recent_vol > 0.30:
        return "CHOPPY_HIGH_VOL"
    else:
        return "TRENDING"

def generate_features(df):
    if df.empty or "close" not in df.columns:
        return pd.DataFrame()
    close, volume = df["close"], df["volume"].replace(0, np.nan)
    df["ret_1"] = close.pct_change(1)
    df["ret_5"] = close.pct_change(5)
    df["ema_9"] = close.ewm(span=9, adjust=False).mean()
    df["rsi"] = calculate_rsi(close)
    df["macd"] = close.ewm(span=12).mean() - close.ewm(span=26).mean()
    if "vwap" not in df.columns: df["vwap"] = ((df["high"] + df["low"] + close) / 3 * volume).cumsum() / volume.cumsum()
    df["vwap_z"] = ((close - df["vwap"]) / (close - df["vwap"]).rolling(30).std().replace(0, np.nan)).clip(-5.0, 5.0)
    df["vol_velocity"] = volume / volume.rolling(20).mean().replace(0, np.nan)
    return df.replace([np.inf, -np.inf], np.nan).dropna()

FEATURES = ["ret_1", "ret_5", "ema_9", "rsi", "macd", "vwap_z", "vol_velocity"]
models = {}

def train_models():
    for sym in TRADE_SYMBOLS:
        log.info(f"بدء تدريب نموذج {sym}...")
        raw_df = fetch_bars(sym, HISTORY_DAYS)
        df = generate_features(raw_df)
        if df.empty or len(df) < 50:
            log.warning(f"⚠️ بيانات غير كافية لتدريب {sym}. تخطي التدريب مؤقتاً.")
            continue
            
        df["target"] = np.where((df["close"].shift(-FORECAST_HORIZON) / df["close"] - 1) > 0.00035, 1, 
                       np.where((df["close"].shift(-FORECAST_HORIZON) / df["close"] - 1) < -0.00035, -1, 0))
        df = df[df["target"] != 0].dropna()
        
        if len(df) < 20:
            continue
            
        mdl = make_pipeline(StandardScaler(), lgb.LGBMClassifier(n_estimators=300, max_depth=6, class_weight="balanced", n_jobs=-1, verbose=-1))
        mdl.fit(df[FEATURES], df["target"])
        models[sym] = mdl
        log.info(f"✅ اكتمل تدريب {sym}.")
    return len(models) > 0

# =========================================================
# 8) استراتيجية التنفيذ التكيفية (Adaptive Execution)
# =========================================================

def execute_iceberg_order(symbol, strategy_type, total_qty, price):
    order_id = f"{symbol}_{strategy_type}_{uuid.uuid4().hex[:6]}"
    spread_cost = 1.50 
    
    log.info(f"❄️ تنفيذ Iceberg: استراتيجية [{strategy_type}] لـ {total_qty} عقد.")
    
    remaining = total_qty
    while remaining > 0:
        chunk = min(remaining, MAX_ORDER_CHUNK_SIZE)
        remaining -= chunk
        time.sleep(1.0) 
        
    state["positions"][order_id] = {
        "symbol": symbol, "strategy": strategy_type, "qty": total_qty,
        "entry_cost": spread_cost, "peak_cost": spread_cost,
        "stop_loss": spread_cost * (1 + HARD_STOP_LOSS_PCT),
        "breakeven_active": False, "entry_time": stamp()
    }
    save_state()
    log.info(f"✅ اكتمل بناء المراكز بنجاح.")

def execute_adaptive_strategy(symbol, direction, price, active_equity, confidence, regime):
    if regime == "CHOPPY_HIGH_VOL":
        log.warning(f"⚠️ سوق عالي الخطورة لـ {symbol}. إيقاف التداول مؤقتاً.")
        return

    qty = calculate_dynamic_position_qty(active_equity, 1.50, confidence)
    direction_str = "CALL" if direction == 1 else "PUT"

    if regime == "TRENDING":
        log.info(f"📈 سوق ذو اتجاه واضح: بناء Vertical Spread ({direction_str})")
        execute_iceberg_order(symbol, f"VERTICAL_{direction_str}", qty, price)
    elif regime == "COMPRESSED":
        if confidence > 0.70:
             log.info(f"💥 انفجار موجه (ثقة عالية): بناء Ratio Backspread ({direction_str})")
             execute_iceberg_order(symbol, f"BACKSPREAD_{direction_str}", qty, price)
        else:
             log.info(f"🌋 طاقة مكبوتة (ثقة متوسطة): بناء Long Straddle (صيد الانفجار في أي اتجاه)")
             execute_iceberg_order(symbol, "STRADDLE", max(1, int(qty/2)), price)

# =========================================================
# 9) إدارة المراكز والوقف الديناميكي المتسارع (Accelerating Trailing Stop)
# =========================================================

def manage_open_positions():
    if not state["positions"]: return
    
    current_cost = 1.70 
    positions_to_close = []
    
    for pid, pos in state["positions"].items():
        if current_cost > pos["peak_cost"]: pos["peak_cost"] = current_cost
        pnl_pct = (current_cost - pos["entry_cost"]) / pos["entry_cost"]

        if pnl_pct >= BREAKEVEN_ACTIVATION_PCT and not pos["breakeven_active"]:
            pos["stop_loss"] = pos["entry_cost"] * 1.05
            pos["breakeven_active"] = True
            log.info(f"🛡️ تأمين {pid}: تم تأمين الصفقة ضد الخسارة (Breakeven).")

        if pos["breakeven_active"]:
            if pnl_pct > 3.0: 
                dynamic_distance = 0.05 
            elif pnl_pct > 1.5: 
                dynamic_distance = 0.10 
            else:
                dynamic_distance = TRAILING_STOP_DISTANCE 

            dynamic_stop = pos["peak_cost"] * (1 - dynamic_distance)
            if dynamic_stop > pos["stop_loss"]:
                pos["stop_loss"] = dynamic_stop

        is_eod = now_ny().time() >= EOD_CLOSE_TIME
        hit_stop = current_cost <= pos["stop_loss"]
        
        is_unlimited_strategy = ("BACKSPREAD" in pos["strategy"] or "STRADDLE" in pos["strategy"])
        hit_target = (pnl_pct >= TAKE_PROFIT_PCT and not is_unlimited_strategy)
        
        if is_eod or hit_stop or hit_target:
            reason = "EOD" if is_eod else ("Hit Target" if hit_target else "Trailing Stop")
            log.info(f"🔥 إغلاق {pid} | السبب: {reason} | الربح النهائي: {pnl_pct*100:.1f}%")
            positions_to_close.append(pid)
            
    for pid in positions_to_close: del state["positions"][pid]
    save_state()

# =========================================================
# 10) حلقة العمل الرئيسية
# =========================================================

def run():
    log.info(f"🚀 بدء تشغيل {NAME} {VERSION}...")
    if not train_models():
        log.error("❌ فشل تدريب النماذج لجميع الأصول. إعادة المحاولة لاحقاً...")
        return

    while True:
        try:
            manage_open_positions()

            if in_signal_window() and not state["positions"]:
                active_equity = get_managed_equity()
                
                for sym in TRADE_SYMBOLS:
                    if sym not in models:
                        continue
                    raw_df = fetch_bars(sym, LIVE_DAYS)
                    df = generate_features(raw_df)
                    if df.empty:
                        continue
                        
                    regime = detect_market_regime(df)
                    latest = df.iloc[-1]
                    probs = models[sym].predict_proba(pd.DataFrame([latest[FEATURES]]))[0]
                    best_idx = int(np.argmax(probs))
                    confidence = float(probs[best_idx])
                    direction = int(models[sym].classes_[best_idx])

                    if direction != 0 and confidence >= MIN_CONFIDENCE_THRESHOLD:
                        log.info(f"🎯 رصد إشارة: {sym} | الحالة: {regime} | ثقة: {confidence*100:.1f}%")
                        execute_adaptive_strategy(sym, direction, latest["close"], active_equity, confidence, regime)
                        time.sleep(120) 
                        break 
            
            time.sleep(15)
        except Exception as e:
            log.error(f"خطأ غير متوقع في الحلقة الرئيسية: {e}")
            time.sleep(30)

if __name__ == "__main__":
    run()
