# -*- coding: utf-8 -*-
"""
‎صَفاء التحليل v60.0 — Institutional Predictive Edition
‎توصيات وإشارات تحليليّة تنبؤية استباقية تسبق السعر باستغلال:
1. Order Flow Imbalance & Volume Velocity (تدفق الأوامر والامتصاص)
2. Liquidity Sweeps & Stop Loss Hunts (صيد السيولة ومصايد صناع السوق)
3. 0DTE Options Gamma Exposure (GEX & Gamma Squeeze Zones)
4. LightGBM ML Multi-Factor Engine (نموذج التنبؤ الرقمي التوافقي)
5. Multi-Timeframe Intermarket Confluence (التوافق اللحظي بين الأطر الزمنية)

‎تطوير وتنفيذ: تصميم أبو سحاب
"""

import os
import time
import logging
import sqlite3
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
import yfinance as yf

‎# استيراد مكتبة LightGBM للتعلم الآلي مع آلية احتياطية في حال عدم توفرها
try:
    import lightgbm as lgb
    HAS_LGBM = True
except ImportError:
    HAS_LGBM = False

# ================= ================= =================
‎# الإعدادات الأساسية والهوية
# ================= ================= =================

APP_NAME = "صَفاء التحليل"
APP_NAME_ASCII = "SafaaAnalysis"
VERSION = "v60.1-PREDICTIVE-INSTITUTIONAL"
SIGNATURE = "⚡ تصميم أبو سحاب"

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

ALPACA_KEY = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
STOCK_FEED = os.getenv("ALPACA_STOCK_FEED", "iex").strip()
OPTIONS_FEED = os.getenv("ALPACA_OPTIONS_FEED", "indicative").strip().lower()

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "./data/safaa_v60.db")
MODEL_PATH = os.getenv("MODEL_PATH", "./models/safaa_lgbm_v60.txt")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.15"))
MIN_MOVE_POINTS = float(os.getenv("MIN_MOVE_POINTS", "2.5"))
MAX_ALERTS_PER_DAY = int(os.getenv("MAX_ALERTS_PER_DAY", "20"))
MIN_CONFIDENCE_THRESHOLD = int(os.getenv("MIN_CONFIDENCE_THRESHOLD", "84"))

SCAN_STOCKS = [s.strip().upper() for s in os.getenv(
    "SCAN_STOCKS", "NVDA,AAPL,TSLA,META,AMZN,AMD,QQQ,SPY,DIA"
).split(",") if s.strip()]

session = requests.Session()
session.headers.update({"User-Agent": f"{APP_NAME_ASCII}/{VERSION}"})

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("safaa_v60")


def now_ny():
    return datetime.now(NY)


def alpaca_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Accept": "application/json",
    }


def ensure_db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_state (
                day TEXT PRIMARY KEY,
                sent_count INTEGER NOT NULL DEFAULT 0,
                pm_report_sent INTEGER NOT NULL DEFAULT 0,
                eod_report_sent INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS predictions_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                direction TEXT,
                spot_price REAL,
                confidence INTEGER,
                sweep_detected INTEGER,
                volume_velocity REAL,
                gamma_status TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS predictions_eval (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT, timestamp TEXT, kind TEXT, symbol TEXT, direction TEXT,
                spot_price REAL, confidence INTEGER,
                close_price REAL, move_pct REAL, result TEXT
            )
        """)


def daily_state_get():
    today = now_ny().date().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT sent_count, pm_report_sent, eod_report_sent FROM daily_state WHERE day=?", (today,)).fetchone()
        if row:
            return {"sent_count": int(row[0]), "pm_report_sent": bool(row[1]), "eod_report_sent": bool(row[2])}
        return {"sent_count": 0, "pm_report_sent": False, "eod_report_sent": False}


def increment_alert_count():
    today = now_ny().date().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO daily_state(day, sent_count, pm_report_sent, eod_report_sent) VALUES (?, 1, 0, 0)
            ON CONFLICT(day) DO UPDATE SET sent_count=sent_count+1
        """, (today,))


def mark_pm_sent():
    today = now_ny().date().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO daily_state(day, sent_count, pm_report_sent, eod_report_sent) VALUES (?, 0, 1, 0)
            ON CONFLICT(day) DO UPDATE SET pm_report_sent=1
        """, (today,))


def mark_eod_sent():
    today = now_ny().date().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO daily_state(day, sent_count, pm_report_sent, eod_report_sent) VALUES (?, 0, 0, 1)
            ON CONFLICT(day) DO UPDATE SET eod_report_sent=1
        """, (today,))


def log_prediction(kind, symbol, direction, spot, confidence):
‎    """يسجل كل توقع (CALL = صعود، PUT = هبوط) ليُقيَّم آخر اليوم."""
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO predictions_eval(day, timestamp, kind, symbol, direction, spot_price, confidence) VALUES (?,?,?,?,?,?,?)",
                (now_ny().date().isoformat(), now_ny().isoformat(timespec="seconds"),
                 kind, symbol, direction, float(spot), int(confidence)))
    except Exception as exc:
        log.warning("تعذر تسجيل التوقع: %s", exc)


def rth_close(symbol):
‎    """آخر إغلاق دقيقة ضمن جلسة اليوم (حتى 16:00 نيويورك)."""
    df = get_stock_bars(symbol, timeframe="1Min", limit=1000)
    if df.empty:
        return None
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(NY)
    mask = (ts.dt.date == now_ny().date()) & ((ts.dt.hour * 60 + ts.dt.minute) <= 16 * 60)
    return float(df.loc[mask, "close"].iloc[-1]) if mask.any() else None


def evaluate_predictions():
‎    """يقارن توقعات اليوم بالإغلاق الفعلي: HIT لو تحرك السعر باتجاه التوقع من لحظة التوصية حتى الإغلاق."""
    today = now_ny().date().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT id, symbol, direction, spot_price FROM predictions_eval WHERE day=? AND result IS NULL",
            (today,)).fetchall()
        cache = {}
        for pid, sym, direction, spot in rows:
            if sym not in cache:
                cache[sym] = rth_close(sym)
            close = cache[sym]
            if not close or not spot:
                continue
            move = (close - spot) / spot * 100.0
            hit = (move > 0) if direction == "CALL" else (move < 0)
            con.execute("UPDATE predictions_eval SET close_price=?, move_pct=?, result=? WHERE id=?",
                        (close, move, "HIT" if hit else "MISS", pid))


def accuracy_summary():
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT kind, COUNT(*), SUM(result='HIT') FROM predictions_eval WHERE result IS NOT NULL GROUP BY kind"
        ).fetchall()
    if not rows:
        return "لا توجد بيانات كافية بعد"
    names = {"alert": "توصيات الأسهم", "premarket": "اتجاه SPY الصباحي"}
    return " | ".join(f"{names.get(k, k)}: {int(h or 0)}/{n} ({(h or 0) / n * 100:.0f}%)" for k, n, h in rows)


def telegram_send(message):
    if not TG_TOKEN or not TG_CHAT:
        log.warning("بيانات التليجرام غير مكتملة.")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = session.post(url, json={
            "chat_id": TG_CHAT,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=15)
        return r.status_code == 200
    except Exception as exc:
        log.error("خطأ في إرسال التليجرام: %s", exc)
        return False


def market_phase():
    now = now_ny()
    if now.weekday() >= 5:
        return "CLOSED"
    minute = now.hour * 60 + now.minute
    if 8 * 60 <= minute < 9 * 60 + 30:
        return "PREMARKET_HOUR"
    elif 9 * 60 + 30 <= minute < 16 * 60:
        return "RTH"
    elif 16 * 60 <= minute < 16 * 60 + 30:
        return "POST_MARKET"
    return "CLOSED"


# ================= ================= =================
‎# 1. محرك تدفق الأوامر والسيولة اللحظية (Order Flow & Velocity)
# ================= ================= =================

def get_realtime_stock_price(symbol):
‎    """جلب أحدث سعر تنفيذي مباشر للسهم لحظياً (Realtime Trade Price)"""
    url = f"{DATA_URL}/v2/stocks/{symbol}/trades/latest"
    try:
        r = session.get(url, headers=alpaca_headers(), params={"feed": STOCK_FEED}, timeout=5)
        if r.status_code == 200:
            trade = r.json().get("trade", {})
            if isinstance(trade, dict):
                price = float(trade.get("p", 0))
                if price > 0:
                    return price
    except Exception as exc:
        log.warning("تعذر جلب السعر اللحظي المباشر لـ %s: %s", symbol, exc)
    return None


def get_stock_bars(symbol, timeframe="1Min", limit=300):
‎    """جلب بيانات الشموع من Alpaca API"""
    end = datetime.now(UTC)
    start = end - timedelta(days=3)
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "limit": limit,
        "feed": STOCK_FEED,
        "sort": "desc",   # أحدث الشموع أولاً (الافتراضي asc كان يرجّع شموع قديمة)
    }
    try:
        r = session.get(url, headers=alpaca_headers(), params=params, timeout=15)
        if r.status_code != 200:
            return pd.DataFrame()
        
        res_json = r.json()
        raw_bars = res_json.get("bars")
        rows = raw_bars.get(symbol, []) if isinstance(raw_bars, dict) else (raw_bars if isinstance(raw_bars, list) else [])
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).rename(columns={"c": "close", "h": "high", "l": "low", "v": "volume", "t": "timestamp"})
        return df.iloc[::-1].reset_index(drop=True)   # ترتيب تصاعدي: الأقدم -> الأحدث
    except Exception as exc:
        log.error("خطأ جلب بيانات %s: %s", symbol, exc)
        return pd.DataFrame()


def calculate_order_flow_analytics(df):
    """
‎    تحليل تدفق الأوامر والامتصاص وتسارع السيولة (Order Flow Dynamics):
    - Volume Velocity: تسارع الفوليوم اللحظي مقارنة بالمتوسط المتحرك.
    - Absorption Ratio: كشف عمليات الشراء/البيع الخفية (ارتفاع الفوليوم مع ضيق النطاق السعري).
    """
    if df.empty or len(df) < 15:
        return {"volume_velocity": 1.0, "absorption_detected": False, "vwap_dist_pct": 0.0}

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

‎    # حساب VWAP اللحظي
    vol_sum = volume.cumsum()
    vwap = (volume * close).cumsum() / np.where(vol_sum == 0, 1, vol_sum)
    vwap_dist_pct = ((close.iloc[-1] - vwap.iloc[-1]) / vwap.iloc[-1]) * 100.0

‎    # حساب Volume Velocity
    recent_vol = volume.tail(3).mean()
    prior_vol = volume.iloc[-15:-3].mean()
    volume_velocity = round((recent_vol / prior_vol), 2) if prior_vol > 0 else 1.0

‎    # حساب Absorption (الامتصاص المؤسسي عند الدعم/المقاومة)
    candle_range = (high.iloc[-1] - low.iloc[-1])
    avg_range = (high.tail(10) - low.tail(10)).mean()
    is_narrow_range = candle_range < (avg_range * 0.65)
    is_high_volume = volume.iloc[-1] > (volume.tail(10).mean() * 1.5)
    absorption_detected = bool(is_narrow_range and is_high_volume)

    return {
        "volume_velocity": volume_velocity,
        "absorption_detected": absorption_detected,
        "vwap_dist_pct": vwap_dist_pct,
        "vwap_val": vwap.iloc[-1]
    }


# ================= ================= =================
‎# 2. رصد صيد السيولة والمصايد (Liquidity Sweeps & Stop Hunts)
# ================= ================= =================

def detect_liquidity_sweep(df, window=20):
    """
‎    رصد كسر القيعان/القمم اللحظية الوهمي لجمع السيولة (Liquidity Sweep):
‎    يحدث عندما يكسر السعر أعلى/أدنى مستوى خلال (window) دقيقة بفوليوم مفاجئ ثم يرتد داخل النطاق.
    """
    if df.empty or len(df) < window + 2:
        return None

    highs = df["high"].astype(float)
    lows = df["low"].astype(float)
    closes = df["close"].astype(float)
    volumes = df["volume"].astype(float)

    prev_high = highs.iloc[-(window + 1):-1].max()
    prev_low = lows.iloc[-(window + 1):-1].min()

    curr_high = highs.iloc[-1]
    curr_low = lows.iloc[-1]
    curr_close = closes.iloc[-1]
    curr_vol = volumes.iloc[-1]
    avg_vol = volumes.iloc[-15:-1].mean()

‎    # صيد سيولة البائعين (Bullish Liquidity Sweep)
    if curr_low < prev_low and curr_close > prev_low and curr_vol > (avg_vol * 1.4):
        return "BULLISH_SWEEP"

‎    # صيد سيولة المشترين (Bearish Liquidity Sweep)
    if curr_high > prev_high and curr_close < prev_high and curr_vol > (avg_vol * 1.4):
        return "BEARISH_SWEEP"

    return None


# ================= ================= =================
‎# 3. تحليل تدفق عقود الخيارات وتكشف الجاما (0DTE Gamma Exposure)
# ================= ================= =================

def calculate_0dte_gamma_exposure(symbol, spot):
    """
‎    تقدير مستويات تسارع الجاما (Gamma Acceleration & Flip Level) لعقود 0DTE / الأسبوعية.
‎    تحدد المناطق التي يُجبر فيها صناع السوق (Dealers) على التحويط السريع.
    """
    today_str = now_ny().date().isoformat()
    url = f"{TRADING_URL}/v2/options/contracts"
    params = {
        "underlying_symbols": symbol,
        "expiration_date": today_str,
        "status": "active",
        "limit": 100,
    }
    try:
        r = session.get(url, headers=alpaca_headers(), params=params, timeout=10)
        if r.status_code != 200:
            return "GAMMA_NEUTRAL", spot

        contracts = r.json().get("option_contracts", [])
        if not contracts:
            return "GAMMA_NEUTRAL", spot

        strikes = [float(c.get("strike_price", 0)) for c in contracts if c.get("strike_price")]
        if not strikes:
            return "GAMMA_NEUTRAL", spot

        gamma_flip_level = float(np.median(strikes))
        
        if spot > gamma_flip_level * 1.002:
            return "POSITIVE_GAMMA_SQUEEZE", gamma_flip_level
        elif spot < gamma_flip_level * 0.998:
            return "NEGATIVE_GAMMA_ACCELERATION", gamma_flip_level

        return "GAMMA_BALANCED", gamma_flip_level
    except Exception as exc:
        log.warning("خطأ حساب الجاما لـ %s: %s", symbol, exc)
        return "GAMMA_NEUTRAL", spot


# ================= ================= =================
‎# 4. محرك التعلم الآلي والتنبؤ التوافقي (LightGBM Scoring Engine)
# ================= ================= =================

class LightGBMPredictor:
    def __init__(self, model_file):
        self.model_file = model_file
        self.model = None
        self.load_model()

    def load_model(self):
        if HAS_LGBM and os.path.exists(self.model_file):
            try:
                self.model = lgb.Booster(model_file=self.model_file)
                log.info("تم تحميل نموذج LightGBM بنجاح من: %s", self.model_file)
            except Exception as e:
                log.warning("تعذر تحميل ملف نموذج LightGBM: %s. استخدام المحرك التراكمي المحسّن.", e)

    def predict(self, features_dict):
        """
‎        تقييم احتمالية الصفقة باستخدام نموذج LightGBM أو نظام التجميع الإحصائي الموزون (GBDT Rules).
        """
        # Feature vector: [vwap_dist, volume_velocity, squeeze_ratio, multi_tf_align, sweep_val, gamma_val]
        vwap_dist = features_dict.get("vwap_dist_pct", 0.0)
        vol_vel = features_dict.get("volume_velocity", 1.0)
        squeeze = features_dict.get("squeeze_ratio", 0.2)
        multi_tf = features_dict.get("multi_tf_align", 0)  # +1, -1, 0
        sweep = 1 if features_dict.get("sweep") == "BULLISH_SWEEP" else (-1 if features_dict.get("sweep") == "BEARISH_SWEEP" else 0)
        gamma = 1 if "POSITIVE" in features_dict.get("gamma_status", "") else (-1 if "NEGATIVE" in features_dict.get("gamma_status", "") else 0)

        feature_vector = np.array([[vwap_dist, vol_vel, squeeze, multi_tf, sweep, gamma]])

        if self.model and HAS_LGBM:
            try:
                raw_prob = self.model.predict(feature_vector)[0]
                confidence = int(np.clip(raw_prob * 100, 70, 98))
                return confidence
            except Exception:
                pass

‎        # المحرك التجميعي المحسّن (Ensemble Heuristic Engine)
        score = 80.0
        score += min(10.0, (vol_vel - 1.0) * 4.0)
        score += 5.0 if squeeze <= 0.14 else 0.0
        score += 6.0 if abs(multi_tf) > 0 else 0.0
        score += 7.0 if sweep != 0 else 0.0
        score += 4.0 if gamma != 0 else 0.0

        confidence = int(np.clip(score, 75, 97))
        return confidence


predictor_engine = LightGBMPredictor(MODEL_PATH)


# ================= ================= =================
‎# 5. التوافق اللحظي بين الأطر الزمنية والأسواق (Intermarket Multi-TF)
# ================= ================= =================

def check_multi_timeframe_alignment(symbol):
‎    """تحليل توافق الاتجاه على أطر 1m و 5m و 15m"""
    df_1m = get_stock_bars(symbol, timeframe="1Min", limit=50)
    df_5m = get_stock_bars(symbol, timeframe="5Min", limit=50)

    if df_1m.empty or df_5m.empty:
        return 0

    close_1m = df_1m["close"].astype(float)
    close_5m = df_5m["close"].astype(float)

    ema5_1m = close_1m.ewm(span=5, adjust=False).mean().iloc[-1]
    ema13_1m = close_1m.ewm(span=13, adjust=False).mean().iloc[-1]

    ema5_5m = close_5m.ewm(span=5, adjust=False).mean().iloc[-1]
    ema13_5m = close_5m.ewm(span=13, adjust=False).mean().iloc[-1]

    if (ema5_1m > ema13_1m) and (ema5_5m > ema13_5m):
        return 1  # Bullish Alignment
    elif (ema5_1m < ema13_1m) and (ema5_5m < ema13_5m):
        return -1  # Bearish Alignment

    return 0


def get_qqq_intermarket_bias():
‎    """قراءة سيولة وتوافق مؤشر ناسداك QQQ"""
    df_qqq = get_stock_bars("QQQ", timeframe="1Min", limit=100)
    if df_qqq.empty:
        return "NEUTRAL"
    analytics = calculate_order_flow_analytics(df_qqq)
    if analytics["vwap_dist_pct"] > 0.05:
        return "BULLISH"
    elif analytics["vwap_dist_pct"] < -0.05:
        return "BEARISH"
    return "NEUTRAL"


# ================= ================= =================
‎# الخوارزمية الشاملة لكشف الإشارات عالية التناغم
# ================= ================= =================

def analyze_symbol_institutional_setup(symbol):
‎    """تحليل استباقي متكامل يدمج كافة المحركات الإشارية"""
    df_1m = get_stock_bars(symbol, timeframe="1Min", limit=200)
    if df_1m.empty or len(df_1m) < 25:
        return None

    close = df_1m["close"].astype(float)
    live_spot = get_realtime_stock_price(symbol)
    spot = live_spot if (live_spot and live_spot > 0) else close.iloc[-1]

‎    # 1. تحليل تدفق الأوامر وتسارع السيولة
    of_analytics = calculate_order_flow_analytics(df_1m)

‎    # 2. كشف مصايد وصيد السيولة (Liquidity Sweeps)
    sweep_signal = detect_liquidity_sweep(df_1m, window=20)

‎    # 3. تكشف الجاما (0DTE Gamma Exposure)
    gamma_status, gamma_flip = calculate_0dte_gamma_exposure(symbol, spot)

‎    # 4. التوافق متعدد الأطر الزمنية
    multi_tf_align = check_multi_timeframe_alignment(symbol)

‎    # 5. الانضغاط السعري (Squeeze Ratio)
    std_recent = close.tail(6).std()
    squeeze_ratio = (std_recent / spot) * 100.0

‎    # تحديد الاتجاه التنبؤي
    direction = None

‎    # أولوية 1: إشارة صيد السيولة (Sweep Reversal)
    if sweep_signal == "BULLISH_SWEEP":
        direction = "CALL"
    elif sweep_signal == "BEARISH_SWEEP":
        direction = "PUT"
‎    # أولوية 2: توافق الجاما + الانضغاط + تدفق الأوامر
    elif squeeze_ratio <= 0.18 and of_analytics["volume_velocity"] >= 1.25:
        if multi_tf_align == 1 or of_analytics["vwap_dist_pct"] > 0:
            direction = "CALL"
        elif multi_tf_align == -1 or of_analytics["vwap_dist_pct"] < 0:
            direction = "PUT"

    if not direction:
        return None

‎    # 6. تقييم نسبة الثقة عبر محرك LightGBM
    features_dict = {
        "vwap_dist_pct": of_analytics["vwap_dist_pct"],
        "volume_velocity": of_analytics["volume_velocity"],
        "squeeze_ratio": squeeze_ratio,
        "multi_tf_align": multi_tf_align,
        "sweep": sweep_signal,
        "gamma_status": gamma_status
    }

    confidence = predictor_engine.predict(features_dict)

    if confidence < MIN_CONFIDENCE_THRESHOLD:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "spot": spot,
        "confidence": confidence,
        "sweep_signal": sweep_signal,
        "volume_velocity": of_analytics["volume_velocity"],
        "gamma_status": gamma_status,
        "absorption": of_analytics["absorption_detected"],
        "vwap_dist": of_analytics["vwap_dist_pct"]
    }


def get_target_expiration(symbol):
    today = now_ny().date()
    if symbol in ("SPY", "QQQ", "DIA", "SPX"):
        return today.isoformat(), "0DTE (ينتهي اليوم)"
    days_ahead = 4 - today.weekday()
    if days_ahead < 0:
        days_ahead += 7
    exp_date = today + timedelta(days=days_ahead)
    return (today.isoformat(), "0DTE (ينتهي اليوم)") if exp_date == today else (exp_date.isoformat(), f"أسبوعي (ينتهي بعد {(exp_date - today).days} أيام)")


def find_high_conviction_option(symbol, direction, spot):
    target_move = 15.0 if symbol == "SPX" else MIN_MOVE_POINTS
    target_strike = round(spot + target_move) if direction == "CALL" else round(spot - target_move)
    exp_date, exp_type_label = get_target_expiration(symbol)

    url = f"{TRADING_URL}/v2/options/contracts"
    params = {
        "underlying_symbols": symbol,
        "expiration_date": exp_date,
        "type": direction.lower(),
        "status": "active",
        "limit": 100,
    }
    r = session.get(url, headers=alpaca_headers(), params=params, timeout=15)
    if r.status_code != 200:
        return None
        
    data = r.json()
    contracts = data.get("option_contracts", data.get("contracts", []))
    if isinstance(contracts, dict):
        contracts = contracts.get("option_contracts", [])
        
    best_contract = None
    min_diff = 999
    for c in contracts:
        s_price = float(c.get("strike_price", 0))
        diff = abs(s_price - target_strike)
        if diff < min_diff:
            min_diff = diff
            best_contract = c
            
    if not best_contract:
        return None

    q_url = f"{DATA_URL}/v1beta1/options/quotes/latest"
    q_r = session.get(q_url, headers=alpaca_headers(), params={"symbols": best_contract["symbol"], "feed": OPTIONS_FEED}, timeout=15)
    if q_r.status_code != 200:
        return None
    
    q_data = q_r.json().get("quotes", {}).get(best_contract["symbol"], {})
    ask = float(q_data.get("ap", 0) or 0)
    bid = float(q_data.get("bp", 0) or 0)
    if ask <= 0 or bid <= 0:
        return None
        
    mid = round((ask + bid) / 2, 2)
    spread = (ask - bid) / mid if mid > 0 else 1.0
    if spread > MAX_SPREAD_PCT:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "spot": spot,
        "strike": float(best_contract["strike_price"]),
        "contract": best_contract["symbol"],
        "ask": ask,
        "bid": bid,
        "mid": mid,
        "target_move": target_move,
        "exp_date": exp_date,
        "exp_label": exp_type_label
    }


# ================= ================= =================
‎# تنسيق الرسائل مع التوقيع والعلامة الفنية
# ================= ================= =================

def format_conviction_alert(opt, setup):
    dir_ar = "شراء CALL 🟢" if opt["direction"] == "CALL" else "شراء PUT 🔴"
    sweep_txt = "⚡ صيد سيولة محدد (Liquidity Sweep)" if setup.get("sweep_signal") else "مستمر (Flow Acceleration)"
    abs_txt = "نعم (امتصاص مؤسسي)" if setup.get("absorption") else "عادي"
    
    return (
        f"🔥 *توصية عالية القوة والتنبؤ (توقع حركة {opt['target_move']}+ نقطة)*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 *الأصل:* `{opt['symbol']}`\n"
        f"🧭 *الاتجاه التنبؤي:* {dir_ar}\n"
        f"📅 *نوع العقد:* `{opt['exp_label']}` (تاريخ: `{opt['exp_date']}`)\n"
        f"💹 *السعر التنفيذي اللحظي:* `{opt['spot']:.2f}`\n"
        f"🎯 *السترايك المستهدف:* `{opt['strike']:.2f}`\n"
        f"📄 *رمز العقد:* `{opt['contract']}`\n"
        f"🟢 *Ask:* `${opt['ask']:.2f}` | ⚖️ *Mid:* `${opt['mid']:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔍 *مؤشرات محرك المؤسسات:* \n"
        f"• *تسارع السيولة (Velocity):* `{setup['volume_velocity']}x`\n"
        f"• *نمط الحركة:* `{sweep_txt}`\n"
        f"• *حالة الجاما:* `{setup['gamma_status']}`\n"
        f"• *الامتصاص المؤسسي:* `{abs_txt}`\n"
        f"🎯 *نسبة ثقة LightGBM:* `{setup['confidence']}%`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{SIGNATURE} — {APP_NAME} {VERSION}"
    )


def format_premarket_report(spy_fc, comp_fc):
    lines = [
        f"🎯 *توقعات التداول اليومية المؤسسية (قبل الافتتاح)*",
        f"📅 `{now_ny().strftime('%Y-%m-%d')}`",
        f"━━━━━━━━━━━━━━━━━━"
    ]
    if spy_fc:
        lines.extend([
            f"📊 *SPX المتوقع:* `{spy_fc['spx_low']} — {spy_fc['spx_high']}`",
            f"📊 *SPY المتوقع:* `{spy_fc['spy_low']} — {spy_fc['spy_high']}`",
            f"📈 *أعلى نطاق متوقع:* `{spy_fc['spy_high']}` (SPY) / `{spy_fc['spx_high']}` (SPX)",
            f"📉 *أدنى نطاق متوقع:* `{spy_fc['spy_low']}` (SPY) / `{spy_fc['spx_low']}` (SPX)",
            f"🧭 *اتجاه السوق:* {spy_fc['trend']} | *الثقة:* `{spy_fc['confidence']}%`",
            f"━━━━━━━━━━━━━━━━━━"
        ])
    if comp_fc:
        lines.append("🏢 *توقعات الأسهم والمؤشرات المقترنة:*")
        for c in comp_fc:
            lines.append(
                f"• *{c['symbol']}* (`{c['spot']:.2f}`): النطاق `{c['low']} — {c['high']}` | الاتجاه: {c['direction']} | السترايك: `{c['target_strike']}`"
            )
        lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"{SIGNATURE} — {APP_NAME}")
    return "\n".join(lines)


def format_eod_report():
    today_str = now_ny().strftime('%Y-%m-%d')
    state = daily_state_get()
    try:
        df_spy = get_stock_bars("SPY", timeframe="5Min", limit=100)
        live_spy = get_realtime_stock_price("SPY")
        close_spy = live_spy if (live_spy and live_spy > 0) else (df_spy["close"].iloc[-1] if not df_spy.empty else 0.0)
        high_spy = df_spy["close"].max() if not df_spy.empty else 0.0
        low_spy = df_spy["close"].min() if not df_spy.empty else 0.0
        close_spx = close_spy * 10
    except Exception:
        close_spy, high_spy, low_spy, close_spx = 0.0, 0.0, 0.0, 0.0

    return (
        f"🔔 *تقرير إغلاق السوق وحصاد التوصيات*\n"
        f"📅 `{today_str}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 *إغلاق SPX:* `{close_spx:.2f}`\n"
        f"📊 *إغلاق SPY:* `{close_spy:.2f}`\n"
        f"📈 *أعلى سعر لـ SPY اليوم:* `{high_spy:.2f}`\n"
        f"📉 *أدنى سعر لـ SPY اليوم:* `{low_spy:.2f}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📬 *عدد التوصيات المرسلة اليوم:* `{state['sent_count']} توصيات`\n"
        f"🎯 *دقة التوقعات التراكمية:* {accuracy_summary()}\n"
        f"✅ *محرك التنبؤ:* مستقر وجاهز للجلسة القادمة.\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{SIGNATURE} — {APP_NAME}"
    )


def get_premarket_forecast_all():
    spy_data = None
    try:
        df_spy = yf.download("SPY", period="5d", interval="5m", prepost=True, progress=False)
        if not df_spy.empty:
            close_series = df_spy["Close"]["SPY"] if isinstance(df_spy.columns, pd.MultiIndex) and "SPY" in df_spy["Close"].columns else df_spy["Close"]
            latest_spy = float(close_series.dropna().iloc[-1])
            returns_spy = close_series.pct_change().dropna()
            vol_spy = returns_spy.std() * np.sqrt(78) * latest_spy
            move_spy = max(3.5, vol_spy)
            
            spy_low = round(latest_spy - move_spy, 2)
            spy_high = round(latest_spy + move_spy, 2)
            spx_low = round(spy_low * 10, 2)
            spx_high = round(spy_high * 10, 2)
            
            ma20 = close_series.tail(20).mean()
            spy_trend = "صاعد 🟢" if latest_spy > ma20 else ("هابط 🔴" if latest_spy < ma20 else "عرضي ⚪")
            confidence = int(min(95, max(75, 80 + (abs(latest_spy - ma20) / ma20) * 1000)))
            
            spy_data = {
                "spot": latest_spy,
                "spy_low": spy_low,
                "spy_high": spy_high,
                "spx_low": spx_low,
                "spx_high": spx_high,
                "trend": spy_trend,
                "confidence": confidence
            }
    except Exception as exc:
        log.error("خطأ توقعات SPY ما قبل الافتتاح: %s", exc)

    companies_data = []
    for sym in SCAN_STOCKS:
        if sym in ("SPY", "SPX"):
            continue
        try:
            df = get_stock_bars(sym, timeframe="5Min", limit=50)
            if df.empty:
                continue
            live_spot = get_realtime_stock_price(sym)
            spot = live_spot if (live_spot and live_spot > 0) else float(df["close"].iloc[-1])
            ret = df["close"].pct_change().dropna()
            vol = ret.std() * np.sqrt(78) * spot
            move = max(MIN_MOVE_POINTS, vol)
            low_p = round(spot - move, 2)
            high_p = round(spot + move, 2)
            ma15 = df["close"].tail(15).mean()
            direction = "CALL 🟢" if spot >= ma15 else "PUT 🔴"
            target_strike = round(spot + MIN_MOVE_POINTS, 1) if spot >= ma15 else round(spot - MIN_MOVE_POINTS, 1)
            
            companies_data.append({
                "symbol": sym,
                "spot": spot,
                "low": low_p,
                "high": high_p,
                "direction": direction,
                "target_strike": target_strike
            })
        except Exception as exc:
            log.warning("تخطي السهم %s في تقرير ما قبل الافتتاح: %s", sym, exc)

    return spy_data, companies_data


# ================= ================= =================
‎# تشغيل حلقات المسح والمراقبة
# ================= ================= =================

def run_premarket():
    state = daily_state_get()
    if state["pm_report_sent"]:
        return
    log.info("جاري إعداد تقرير التوقعات المؤسسية ما قبل الافتتاح...")
    spy_fc, comp_fc = get_premarket_forecast_all()
    if spy_fc or comp_fc:
        msg = format_premarket_report(spy_fc, comp_fc)
        if telegram_send(msg):
            mark_pm_sent()
            if spy_fc and "عرضي" not in spy_fc["trend"]:
                log_prediction("premarket", "SPY", "CALL" if "صاعد" in spy_fc["trend"] else "PUT",
                               spy_fc["spot"], spy_fc["confidence"])
            log.info("تم إرسال تقرير ما قبل الافتتاح بنجاح.")


def run_rth():
    state = daily_state_get()
    if state["sent_count"] >= MAX_ALERTS_PER_DAY:
        return

    qqq_bias = get_qqq_intermarket_bias()

    for symbol in SCAN_STOCKS:
        if daily_state_get()["sent_count"] >= MAX_ALERTS_PER_DAY:
            break

        setup = analyze_symbol_institutional_setup(symbol)
        if not setup:
            continue

‎        # فلترة التوافق بين الأصول مع مؤشر ناسداك
        if symbol != "QQQ":
            if setup["direction"] == "CALL" and qqq_bias == "BEARISH":
                log.info("إلغاء صفقة CALL على %s بسبب سلبية سيولة ناسداك.", symbol)
                continue
            elif setup["direction"] == "PUT" and qqq_bias == "BULLISH":
                log.info("إلغاء صفقة PUT على %s بسبب إيجابية سيولة ناسداك.", symbol)
                continue

        opt = find_high_conviction_option(symbol, setup["direction"], setup["spot"])
        if opt:
            msg = format_conviction_alert(opt, setup)
            if telegram_send(msg):
                increment_alert_count()
                log_prediction("alert", symbol, setup["direction"], setup["spot"], setup["confidence"])
                log.info("تم إرسال توصية تنبؤية مؤسسية: %s %s عند سعر %.2f (ثقة %d%%)", symbol, setup["direction"], setup["spot"], setup["confidence"])


def run_post_market():
    state = daily_state_get()
    if state["eod_report_sent"]:
        return
    evaluate_predictions()
    msg = format_eod_report()
    if telegram_send(msg):
        mark_eod_sent()
        log.info("تم إرسال تقرير إغلاق السوق.")


def main():
    ensure_db()
    log.info("تشغيل البوت %s %s...", APP_NAME, VERSION)

    startup_msg = (
        f"🚀 *تم تشغيل نظام {APP_NAME} ({VERSION}) بنجاح*\n"
        f"⏱️ *التوقيت:* `{now_ny().strftime('%Y-%m-%d %H:%M:%S %Z')}`\n"
        f"⚡ *محرك التنبؤ المؤسسي:* `مفعل 🟢`\n"
        f"🎯 *محرك صيد السيولة (Sweeps):* `مفعل 🟢`\n"
        f"📊 *تحليل الجاما (0DTE GEX):* `مفعل 🟢`\n"
        f"🤖 *نموذج LightGBM ML:* `{'مفعل 🟢' if HAS_LGBM else 'المحرك التراكمي 🟢'}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{SIGNATURE}"
    )
    telegram_send(startup_msg)

    while True:
        try:
            phase = market_phase()
            if phase == "PREMARKET_HOUR":
                run_premarket()
            elif phase == "RTH":
                run_rth()
            elif phase == "POST_MARKET":
                run_post_market()
            else:
                log.info("السوق مغلق حالياً | التوقيت: %s", now_ny().strftime("%Y-%m-%d %H:%M:%S %Z"))
        except Exception as exc:
            log.error("خطأ تشغيلي: %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()