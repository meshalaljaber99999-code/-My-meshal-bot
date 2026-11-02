# -*- coding: utf-8 -*-
"""
صَفاء التحليل v66.8 — The 0-DTE Sniper (Ultra Aggressive)
- التداول حصراً بعقود الخيارات 0-DTE (نفس يوم الانتهاء) لـ SPY.
- خوارزمية صيد الانفجارات السعرية العنيفة (Scalping).
- تأمين فوري للصفقة (Breakeven) عند 15% ربح.
- وقف متحرك خانق (Trailing Stop) لحلب الأرباح وتدبيلها.
- إغلاق إجباري صارم قبل الإغلاق بـ 15 دقيقة لتجنب التصفية.
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
# 1) الإعدادات العامة
# =========================================================

NAME = "صَفاء التحليل - 0-DTE Sniper"
VERSION = "v66.8"

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

BASE_DIR = Path(".")
STATE_FILE = BASE_DIR / "safaa_v66_8_0dte_positions.json"
JOURNAL_FILE = BASE_DIR / "safaa_v66_8_0dte_journal.csv"

TRADE_SYMBOLS = ["SPY"]
TIMEFRAME = "1Min"
HISTORY_DAYS = 90
LIVE_DAYS = 5
FORECAST_HORIZON = 3 # توقع حركات سريعة جداً (3 دقائق)

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
# 3) المخاطر، الوقف الخانق، وإدارة الـ 0-DTE
# =========================================================

MIN_CONFIDENCE_THRESHOLD = 0.70  # ثقة عالية جداً لفلترة الإشارات الكاذبة
MAX_RISK_CAP = 0.15              # المخاطرة بـ 15% من رأس المال للصفقة (لتحقيق تدبيل حقيقي)

HARD_STOP_LOSS_PCT = -0.20       # قطع الخسارة الصارم عند -20% (ممنوع الخسارة الكبيرة)
BREAKEVEN_ACTIVATION_PCT = 0.15  # تأمين الصفقة فوراً عند +15% ربح
TRAILING_STOP_DISTANCE = 0.10    # ملاحقة السعر بمسافة 10% فقط لجني الأرباح بقسوة
TAKE_PROFIT_PCT = 2.00           # الهدف 200% (تدبيلتين)، لكن الوقف المتحرك سيخرجنا متى ما انتهى الزخم

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)
EOD_CLOSE_TIME = dtime(15, 45)   # الإغلاق قبل نهاية السوق بـ 15 دقيقة (صارم للـ 0-DTE)
SIGNAL_START = dtime(9, 45)      # تجاهل أول 15 دقيقة المجنونة
SIGNAL_END = dtime(15, 10)       # لا تفتح صفقات جديدة في آخر 50 دقيقة

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(NAME)

# =========================================================
# 4) أدوات الوقت والسجلات
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

def log_trade_to_journal(symbol, contract, qty, entry_price, exit_price, pnl_pct, reason):
    if not JOURNAL_FILE.exists():
        with open(JOURNAL_FILE, mode="w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["timestamp", "symbol", "contract", "qty", "entry_price", "exit_price", "pnl_pct", "reason"])
    with open(JOURNAL_FILE, mode="a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([stamp(), symbol, contract, qty, entry_price, exit_price, f"{pnl_pct*100:.2f}%", reason])

# =========================================================
# 5) طلبات API وإدارة رأس المال
# =========================================================

def alpaca_request(method, url, *, params=None, payload=None):
    try:
        res = requests.request(method=method, url=url, headers=HEADERS, params=params, json=payload, timeout=15)
        if res.ok and res.text: return res.json()
    except Exception as e:
        log.error(f"API Error: {e}")
    return {}

def get_raw_equity():
    data = alpaca_request("GET", TRADING_URL + "/v2/account")
    return float(data.get("equity", 0)) if data else 0.0

def calculate_dynamic_position_qty(equity, option_price, model_confidence):
    # الدخول بقوة في الصفقات الموثوقة لتحقيق التدبيل
    adjusted_risk_pct = min(0.05 * (model_confidence / 0.70), MAX_RISK_CAP)
    capital_to_risk = equity * adjusted_risk_pct
    qty = int(capital_to_risk // (option_price * 100))
    return max(1, qty)

# =========================================================
# 6) قناص عقود الـ 0-DTE (الجديد)
# =========================================================

def get_0dte_atm_contract(underlying_symbol, current_price, option_type="call"):
    """البحث حصراً عن عقود تنتهي اليوم (0-DTE) وتكون At-The-Money"""
    log.info(f"🔍 البحث عن عقود 0-DTE لـ {underlying_symbol} (نوع: {option_type})...")
    
    params = {
        "underlying_symbols": underlying_symbol,
        "status": "active",
        "limit": 1000
    }
    
    contracts_data = alpaca_request("GET", f"{TRADING_URL}/v2/options/contracts", params=params)
    contracts = contracts_data.get("option_contracts", [])
    
    if not contracts: return None
        
    valid_contracts = []
    now_date = now_ny().date()
    
    for c in contracts:
        if c["type"] != option_type: continue
        exp_date = datetime.strptime(c["expiration_date"], "%Y-%m-%d").date()
        dte = (exp_date - now_date).days
        
        # 🔥 الشرط الصارم: 0-DTE فقط 🔥
        if dte == 0:
            strike = float(c["strike_price"])
            distance = abs(strike - current_price)
            valid_contracts.append({
                "symbol": c["symbol"],
                "strike": strike,
                "distance": distance
            })
            
    if not valid_contracts:
        log.warning("⚠️ لا توجد عقود 0-DTE متاحة اليوم (قد يكون يوم عطلة للخيارات).")
        return None
        
    # ترتيب العقود لاختيار الأقرب للسعر الحالي (ATM)
    valid_contracts.sort(key=lambda x: x["distance"])
    best_contract = valid_contracts[0]
    
    log.info(f"🎯 تم قنص عقد 0-DTE: {best_contract['symbol']} (Strike: {best_contract['strike']})")
    return best_contract["symbol"]

def get_option_latest_price(occ_symbol):
    params = {"feed": "opra"}
    data = alpaca_request("GET", f"{DATA_URL}/v1beta1/options/quotes/latest?symbols={occ_symbol}", params=params)
    quotes = data.get("quotes", {})
    if occ_symbol in quotes:
        bid = float(quotes[occ_symbol].get("bp", 0))
        ask = float(quotes[occ_symbol].get("ap", 0))
        if bid > 0 and ask > 0: return (bid + ask) / 2
        elif ask > 0: return ask
    return 0.0

# =========================================================
# 7) جلب البيانات والتحليل العميق (المضاربة اللحظية)
# =========================================================

def fetch_bars(symbol, days=LIVE_DAYS):
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    all_bars = []
    page_token = None
    
    while True:
        params = {"timeframe": TIMEFRAME, "start": start.isoformat(), "end": end.isoformat(), "limit": 10000, "feed": STOCK_FEED}
        if page_token: params["page_token"] = page_token
        data = alpaca_request("GET", f"{DATA_URL}/v2/stocks/{symbol}/bars", params=params)
        bars = data.get("bars", [])
        if not bars: break
        all_bars.extend(bars)
        page_token = data.get("next_page_token")
        if not page_token: break
            
    if not all_bars: return pd.DataFrame()
    df = pd.DataFrame(all_bars)
    df = df.rename(columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.set_index("timestamp").sort_index().dropna(subset=["close", "volume"])

def calculate_rsi(series, p=9): # RSI أسرع للمضاربة (9 بدلاً من 14)
    gain = series.diff().clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
    loss = (-series.diff().clip(upper=0)).ewm(alpha=1/p, adjust=False).mean()
    return 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

def detect_market_regime(df):
    recent_vol = df["close"].pct_change().rolling(30).std().iloc[-1] * math.sqrt(390)
    # للـ 0-DTE، نحن نحب التذبذب العالي إذا كان له اتجاه، لذا نخفف القيود هنا قليلاً
    if recent_vol > 0.40: return "EXTREME_VOL"
    return "TRENDING"

def generate_features(df):
    close, volume = df["close"], df["volume"].replace(0, np.nan)
    df["ret_1"] = close.pct_change(1)
    df["ret_3"] = close.pct_change(3) # تركيز على الزخم السريع جداً
    df["ema_5"] = close.ewm(span=5, adjust=False).mean()
    df["rsi"] = calculate_rsi(close)
    df["macd"] = close.ewm(span=8).mean() - close.ewm(span=21).mean() # MACD سريع
    if "vwap" not in df.columns: df["vwap"] = ((df["high"] + df["low"] + close) / 3 * volume).cumsum() / volume.cumsum()
    df["vwap_z"] = ((close - df["vwap"]) / (close - df["vwap"]).rolling(20).std().replace(0, np.nan)).clip(-5.0, 5.0)
    df["vol_velocity"] = volume / volume.rolling(10).mean().replace(0, np.nan)
    return df.replace([np.inf, -np.inf], np.nan).dropna()

FEATURES = ["ret_1", "ret_3", "ema_5", "rsi", "macd", "vwap_z", "vol_velocity"]
models = {}

def train_models():
    for sym in TRADE_SYMBOLS:
        log.info(f"🧠 تدريب عقل القناص لـ {sym}...")
        raw_df = fetch_bars(sym, HISTORY_DAYS)
        df = generate_features(raw_df)
        if len(df) < 50: continue
        # فلترة صارمة: نبحث عن حركات تحقق أكثر من 0.05% خلال 3 دقائق
        df["target"] = np.where((df["close"].shift(-FORECAST_HORIZON) / df["close"] - 1) > 0.0005, 1, 
                       np.where((df["close"].shift(-FORECAST_HORIZON) / df["close"] - 1) < -0.0005, -1, 0))
        df = df[df["target"] != 0].dropna()
        mdl = make_pipeline(StandardScaler(), lgb.LGBMClassifier(n_estimators=400, max_depth=7, learning_rate=0.05, class_weight="balanced", n_jobs=-1, verbose=-1))
        mdl.fit(df[FEATURES], df["target"])
        models[sym] = mdl
    return len(models) > 0

# =========================================================
# 8) استراتيجية التنفيذ 
# =========================================================

def execute_0dte_trade(symbol, direction, stock_price, equity, confidence):
    opt_type = "call" if direction == 1 else "put"
    occ_symbol = get_0dte_atm_contract(symbol, stock_price, opt_type)
    
    if not occ_symbol: return
    
    opt_price = get_option_latest_price(occ_symbol)
    if opt_price <= 0.10: # تجاهل العقود الرخيصة جداً (التراب)
        log.warning(f"⚠️ سعر العقد {occ_symbol} منخفض جداً ({opt_price}$). مخاطرة عالية، تم الإلغاء.")
        return
        
    qty = calculate_dynamic_position_qty(equity, opt_price, confidence)
    
    log.info(f"⚔️ تنفيذ 0-DTE: شراء {qty} عقد {occ_symbol} بسعر {opt_price}$ للعقد. (الهدف: جني أرباح سريع)")
    
    state["positions"][occ_symbol] = {
        "underlying": symbol,
        "contract": occ_symbol,
        "type": opt_type,
        "qty": qty,
        "entry_price": opt_price,
        "peak_price": opt_price,
        "stop_loss": opt_price * (1 + HARD_STOP_LOSS_PCT),
        "breakeven_active": False,
        "entry_time": stamp()
    }
    save_state()

# =========================================================
# 9) إدارة المراكز: الحماية والوقف الخانق 
# =========================================================

def manage_open_positions():
    if not state["positions"]: return
    
    positions_to_close = []
    
    for occ, pos in state["positions"].items():
        current_price = get_option_latest_price(occ)
        if current_price <= 0: continue
        
        if current_price > pos["peak_price"]: 
            pos["peak_price"] = current_price
            
        pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"]

        # 1. تأمين فوري: إذا ربحنا 15%، الوقف يصبح نقطة الدخول (لا مجال للخسارة بعد الآن)
        if pnl_pct >= BREAKEVEN_ACTIVATION_PCT and not pos["breakeven_active"]:
            pos["stop_loss"] = pos["entry_price"] * 1.02 # دخول + 2% لتغطية العمولات
            pos["breakeven_active"] = True
            log.info(f"🛡️ تم تأمين الصفقة (Breakeven Locked)! {occ} يتداول الآن بربح {pnl_pct*100:.1f}%")

        # 2. الملاحقة الخانقة: نطارد السعر بـ 10% للأسفل من أعلى قمة وصلها
        if pos["breakeven_active"]:
            dynamic_stop = pos["peak_price"] * (1 - TRAILING_STOP_DISTANCE)
            if dynamic_stop > pos["stop_loss"]:
                pos["stop_loss"] = dynamic_stop

        # 3. شروط الخروج
        is_eod = now_ny().time() >= EOD_CLOSE_TIME
        hit_stop = current_price <= pos["stop_loss"]
        hit_target = pnl_pct >= TAKE_PROFIT_PCT
        
        if is_eod or hit_stop or hit_target:
            reason = "EOD (Time Decay Protection)" if is_eod else ("Hit Target (Doubled!)" if hit_target else "Trailing Stop / Cut Loss")
            log.info(f"🔥 إغلاق فوري لـ {occ} | السبب: {reason} | الربح/الخسارة: {pnl_pct*100:.1f}%")
            log_trade_to_journal(pos["underlying"], occ, pos["qty"], pos["entry_price"], current_price, pnl_pct, reason)
            positions_to_close.append(occ)
            
    for occ in positions_to_close: del state["positions"][occ]
    if positions_to_close: save_state()

# =========================================================
# 10) حلقة العمل
# =========================================================

def run():
    log.info(f"🚀 بدء تشغيل {NAME} {VERSION}...")
    if not train_models(): return

    while True:
        try:
            manage_open_positions()

            if in_signal_window() and not state["positions"]:
                active_equity = get_raw_equity()
                
                for sym in TRADE_SYMBOLS:
                    if sym not in models: continue
                    raw_df = fetch_bars(sym, LIVE_DAYS)
                    df = generate_features(raw_df)
                    if df.empty: continue
                        
                    regime = detect_market_regime(df)
                    latest = df.iloc[-1]
                    probs = models[sym].predict_proba(pd.DataFrame([latest[FEATURES]]))[0]
                    best_idx = int(np.argmax(probs))
                    confidence = float(probs[best_idx])
                    direction = int(models[sym].classes_[best_idx])

                    log.info(f"📊 تحليل {sym} | الاتجاه: {'صعود' if direction==1 else 'هبوط' if direction==-1 else 'عرضي'} | الثقة: {confidence*100:.1f}%")

                    # الدخول فقط إذا كانت الثقة أعلى من 70%
                    if direction != 0 and confidence >= MIN_CONFIDENCE_THRESHOLD:
                        if regime == "EXTREME_VOL":
                            log.warning("🚫 تجاهل: تذبذب عشوائي شديد الخطورة للـ 0-DTE.")
                        else:
                            execute_0dte_trade(sym, direction, latest["close"], active_equity, confidence)
                            time.sleep(30) # انتظار 30 ثانية بعد التنفيذ
                            
            time.sleep(10) # فحص أسرع للـ 0-DTE (كل 10 ثواني)
        except Exception as e:
            log.error(f"خطأ غير متوقع: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
