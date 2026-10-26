# -*- coding: utf-8 -*-
"""
صَفاء التحليل v60.2-UltraTitan-SilentExecution (Patched Edition)
محرك تداول تنفيذي صامت تنبؤي — تم إصلاح أخطاء التنفيذ والصفقات المعلقة

تطوير وتنفيذ: تصميم أبو سحاب
"""

import os
import sys
import json
import time
import math
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False

from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline

# ================= ================= =================
# الإعدادات الأساسية والهوية
# ================= ================= =================

NAME = "صَفاء التحليل - المحرك التنفيذي"
VERSION = "v60.2-SilentExecution-Patched"

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
TF = "1Min"

HYPER_ASSET = "SPY"
NASDAQ_ASSET = "QQQ"

DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
KEY = (os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or "").strip()
SECRET = (os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY") or "").strip()
STOCK_FEED = os.getenv("ALPACA_DATA_FEED", "iex").lower()
OPTION_FEED = os.getenv("OPTIONS_FEED", "indicative").lower()

HISTORY_DAYS = 30
MIN_ROWS = 300
HORIZON = 5

MAX_SPREAD_PCT = 0.045        # أقصى فارق بين العرض والطلب 4.5%
POLL_SECONDS = 10             # فحص السوق كل 10 ثوانٍ
EXECUTION_TIMEOUT = 5         # حد الانتظار لتنفيذ أمر الحد (ثوانٍ) قبل تحويله للـ Ask/Bid

MAX_DAILY_LOSS_PCT = 0.02     # كاسر الحماية اليومي (-2% من المحفظة)
MAX_RISK_CAP = 0.065          # أقصى مخاطرة لكل صفقة (6.5% من إجمالي المحفظة المركبة)
MIN_CONFIDENCE_THRESHOLD = 0.82  # نسبة الثقة الأدنى للتنفيذ

ROOT = Path(os.getenv("DATA_DIR", "./data"))
ROOT.mkdir(parents=True, exist_ok=True)
ACTIVE_POSITIONS_FILE = ROOT / "spy_ultratitan_positions.json"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("safaa_execution")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": f"SafaaExecution/{VERSION}"})

def now_ny():
    return datetime.now(NY)

def stamp():
    return datetime.now(UTC).isoformat()

def num(value, default=np.nan):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default

def read_json(filepath):
    try:
        if filepath.exists():
            return json.loads(filepath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {}

def save_json(filepath, data):
    try:
        tmp = filepath.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(filepath)
    except Exception:
        log.exception(f"فشل حفظ الملف {filepath}")

def alpaca_get(url, params=None):
    if not KEY or not SECRET:
        raise RuntimeError("مفاتيح API غير مضبوطة في النظام")
    last = None
    for attempt in range(3):
        try:
            r = HTTP.get(
                url,
                headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET},
                params=params,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Alpaca request failed: {last}")

def alpaca_post(url, payload=None):
    if not KEY or not SECRET:
        raise RuntimeError("مفاتيح API غير مضبوطة")
    last = None
    for attempt in range(3):
        try:
            r = HTTP.post(
                url,
                headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET},
                json=payload,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Alpaca POST request failed: {last}")

def alpaca_delete(url):
    try:
        r = HTTP.delete(url, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}, timeout=8)
        return r.status_code == 200
    except Exception:
        return False

def cancel_all_open_orders():
    try:
        alpaca_delete(f"{TRADING_URL}/v2/orders")
    except Exception as e:
        log.error(f"خطأ في إيقاف الأوامر: {e}")

def get_open_orders():
    try:
        return alpaca_get(f"{TRADING_URL}/v2/orders", {"status": "open"})
    except Exception:
        return []

def market_open():
    n = now_ny()
    return n.weekday() < 5 and (n.hour, n.minute) >= (9, 30) and (n.hour, n.minute) < (16, 0)

def is_premarket():
    n = now_ny()
    return n.weekday() < 5 and (8, 0) <= (n.hour, n.minute) < (9, 30)

def in_signal_window():
    n = now_ny()
    return n.weekday() < 5 and (n.hour, n.minute) >= (9, 45) and (n.hour, n.minute) < (15, 30)

def is_eod_close_time():
    n = now_ny()
    return n.weekday() < 5 and (n.hour, n.minute) >= (15, 50)

# ================= ================= =================
# 1. جلب البيانات والمؤشرات المؤسسية (Order Flow & GEX)
# ================= ================= =================

def fetch_bars(symbol=HYPER_ASSET, days=HISTORY_DAYS, timeframe=TF):
    end = datetime.now(UTC)
    params = {
        "timeframe": timeframe,
        "start": (end - timedelta(days=days)).isoformat(),
        "end": end.isoformat(),
        "limit": 10000,
        "adjustment": "split",
        "feed": STOCK_FEED,
        "sort": "asc",
    }
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    all_bars = []
    token = None
    for _ in range(10):
        p = dict(params)
        if token:
            p["page_token"] = token
        data = alpaca_get(url, p)
        all_bars.extend(data.get("bars") or [])
        token = data.get("next_page_token")
        if not token:
            break
    if not all_bars:
        return pd.DataFrame()
    df = pd.DataFrame(all_bars).rename(
        columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["timestamp", "open", "high", "low", "close"]).drop_duplicates("timestamp", keep="last").sort_values("timestamp").set_index("timestamp")

def calculate_order_flow_analytics(df):
    if df.empty or len(df) < 15:
        return {"volume_velocity": 1.0, "absorption_detected": 0, "vwap_zscore": 0.0}

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    vol_sum = volume.cumsum()
    vwap = (volume * close).cumsum() / np.where(vol_sum == 0, 1, vol_sum)
    
    vwap_zscore = float(((close.iloc[-1] - vwap.iloc[-1]) / (close.rolling(15).std().iloc[-1] + 1e-6)))

    recent_vol = volume.tail(3).mean()
    prior_vol = volume.iloc[-15:-3].mean()
    volume_velocity = float(recent_vol / prior_vol) if prior_vol > 0 else 1.0

    candle_range = (high.iloc[-1] - low.iloc[-1])
    avg_range = (high.tail(10) - low.tail(10)).mean()
    is_narrow_range = candle_range < (avg_range * 0.65)
    is_high_volume = volume.iloc[-1] > (volume.tail(10).mean() * 1.5)
    absorption_detected = 1 if (is_narrow_range and is_high_volume) else 0

    return {
        "volume_velocity": volume_velocity,
        "absorption_detected": absorption_detected,
        "vwap_zscore": vwap_zscore,
        "vwap_val": float(vwap.iloc[-1])
    }

def detect_liquidity_sweep(df, window=20):
    if df.empty or len(df) < window + 2:
        return 0

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

    if curr_low < prev_low and curr_close > prev_low and curr_vol > (avg_vol * 1.35):
        return 1

    if curr_high > prev_high and curr_close < prev_high and curr_vol > (avg_vol * 1.35):
        return -1

    return 0

def calculate_0dte_gamma_exposure(symbol, spot):
    today_str = now_ny().date().isoformat()
    try:
        payload = alpaca_get(f"{TRADING_URL}/v2/options/contracts", {
            "underlying_symbols": symbol,
            "expiration_date": today_str,
            "status": "active",
            "limit": 50,
        })
        contracts = payload.get("option_contracts") or payload.get("contracts") or []
        strikes = [num(c.get("strike_price")) for c in contracts if num(c.get("strike_price")) > 0]
        if not strikes:
            return 0, spot

        gamma_flip_level = float(np.median(strikes))
        if spot > gamma_flip_level * 1.0015:
            return 1, gamma_flip_level
        elif spot < gamma_flip_level * 0.9985:
            return -1, gamma_flip_level

        return 0, gamma_flip_level
    except Exception:
        return 0, spot

# ================= ================= =================
# 2. محرك التعلم الآلي والتدريب المسبق (LightGBM Engine)
# ================= ================= =================

FEATURES = [
    "ret1", "ret3", "ret6", "ret12", 
    "ema_9_21", "ema_21_50", "trend_200", 
    "vwap_zscore", "rsi", "rsi_slope",
    "macd_hist", "vol_surge", "vol_velocity",
    "sweep_signal", "absorption", "gamma_signal",
    "nq_lead_ret", "nq_rel_strength"
]

def generate_institutional_features(spy_bars, qqq_bars=None):
    f = spy_bars.copy()
    c, v = f["close"], f["volume"]

    f["ret1"] = c.pct_change()
    f["ret3"] = c.pct_change(3)
    f["ret6"] = c.pct_change(6)
    f["ret12"] = c.pct_change(12)

    e9 = c.ewm(span=9, adjust=False).mean()
    e21 = c.ewm(span=21, adjust=False).mean()
    e50 = c.ewm(span=50, adjust=False).mean()
    e200 = c.ewm(span=200, adjust=False).mean()

    f["ema_9_21"] = (e9 - e21) / c
    f["ema_21_50"] = (e21 - e50) / c
    f["trend_200"] = (c - e200) / c

    of_data = calculate_order_flow_analytics(f)
    f["vwap_zscore"] = of_data["vwap_zscore"]
    f["vol_velocity"] = of_data["volume_velocity"]
    f["absorption"] = of_data["absorption_detected"]

    f["sweep_signal"] = detect_liquidity_sweep(f)
    spot_curr = float(c.iloc[-1])
    gamma_sig, _ = calculate_0dte_gamma_exposure(HYPER_ASSET, spot_curr)
    f["gamma_signal"] = gamma_sig

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    f["macd_hist"] = (ema12 - ema26 - (ema12 - ema26).ewm(span=9, adjust=False).mean()) / c

    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = (100 - 100 / (1 + rs)) / 100
    f["rsi"] = rsi.fillna(0.5)
    f["rsi_slope"] = rsi.diff(3).fillna(0)
    f["vol_surge"] = v / v.rolling(20).mean().replace(0, np.nan)

    if qqq_bars is not None and not qqq_bars.empty:
        qqq_c = qqq_bars["close"].reindex(f.index).ffill()
        f["nq_lead_ret"] = qqq_c.pct_change().fillna(0)
        f["nq_rel_strength"] = (f["nq_lead_ret"] - f["ret1"]).fillna(0)
    else:
        f["nq_lead_ret"] = 0.0
        f["nq_rel_strength"] = 0.0

    return f.replace([np.inf, -np.inf], np.nan)

class PretrainedInstitutionalEngine:
    def __init__(self):
        self.primary_model = None
        self.meta_model = None
        self.is_trained = False
        self.last_trained_time = None

    def train_model(self):
        log.info("🧠 [Pre-Market Training] جاري تدريب خوارزمية LightGBM + Meta-Labeling...")
        spy_bars = fetch_bars(HYPER_ASSET, days=HISTORY_DAYS)
        qqq_bars = fetch_bars(NASDAQ_ASSET, days=HISTORY_DAYS)

        if spy_bars.empty or len(spy_bars) < MIN_ROWS:
            log.warning("⚠️ لا توجد بيانات كافية لتدريب النموذج.")
            return False

        f = generate_institutional_features(spy_bars, qqq_bars)
        future_ret = (spy_bars["close"].shift(-HORIZON) - spy_bars["close"]) / spy_bars["close"]
        f["primary_target"] = np.where(future_ret > 0.0012, 1, (np.where(future_ret < -0.0012, -1, 0)))

        ds = f[FEATURES + ["primary_target"]].dropna()
        ds_valid = ds[ds["primary_target"] != 0].copy()

        if len(ds_valid) < MIN_ROWS or ds_valid["primary_target"].nunique() < 2:
            log.warning("⚠️ عينات البيانات غير كافية للتدريب.")
            return False

        if HAS_LGB:
            clf = lgb.LGBMClassifier(n_estimators=180, learning_rate=0.03, max_depth=4, random_state=42, verbose=-1)
        else:
            clf = GradientBoostingClassifier(n_estimators=180, learning_rate=0.03, max_depth=4, random_state=42)

        self.primary_model = make_pipeline(SimpleImputer(strategy="median"), clf)
        self.primary_model.fit(ds_valid[FEATURES], ds_valid["primary_target"])

        ds_valid["pred_signal"] = self.primary_model.predict(ds_valid[FEATURES])
        ds_valid["meta_target"] = np.where((ds_valid["pred_signal"] == ds_valid["primary_target"]), 1, 0)

        if ds_valid["meta_target"].nunique() >= 2:
            self.meta_model = make_pipeline(
                SimpleImputer(strategy="median"),
                RandomForestClassifier(n_estimators=120, max_depth=4, random_state=42)
            )
            self.meta_model.fit(ds_valid[FEATURES], ds_valid["meta_target"])

        self.is_trained = True
        self.last_trained_time = now_ny()
        log.info("✅ تم تدريب النموذج وتجهيز الأوزان في الذاكرة بنجاح.")
        return True

    def predict_fast(self, spy_bars, qqq_bars):
        if not self.is_trained or self.primary_model is None:
            self.train_model()

        f = generate_institutional_features(spy_bars, qqq_bars)
        latest_feat = f[FEATURES].iloc[[-1]]

        primary_pred = self.primary_model.predict(latest_feat)[0]
        probs = self.primary_model.predict_proba(latest_feat)[0]
        clf_obj = self.primary_model.steps[-1][1]
        classes = list(clf_obj.classes_)
        prob_max = float(probs[classes.index(primary_pred)]) if primary_pred in classes else 0.5

        meta_prob = 0.50
        if self.meta_model:
            rf_obj = self.meta_model.steps[-1][1]
            meta_classes = list(rf_obj.classes_)
            if 1 in meta_classes:
                idx1 = meta_classes.index(1)
                meta_prob = float(self.meta_model.predict_proba(latest_feat)[0, idx1])

        curr_price = float(spy_bars["close"].iloc[-1])
        vol_vel = float(f["vol_velocity"].iloc[-1])
        sweep_sig = int(f["sweep_signal"].iloc[-1])
        gamma_sig = int(f["gamma_signal"].iloc[-1])

        confidence = (prob_max + meta_prob) / 2.0
        signal = "محايد ⏳"

        if (primary_pred == 1 or sweep_sig == 1) and confidence >= MIN_CONFIDENCE_THRESHOLD:
            if gamma_sig >= 0 and vol_vel >= 1.15:
                signal = "صعود 🚀"

        elif (primary_pred == -1 or sweep_sig == -1) and confidence >= MIN_CONFIDENCE_THRESHOLD:
            if gamma_sig <= 0 and vol_vel >= 1.15:
                signal = "نزول 📉"

        return {
            "signal": signal,
            "confidence": confidence,
            "current_price": curr_price,
            "vol_velocity": vol_vel,
            "sweep_signal": sweep_sig,
            "gamma_signal": gamma_sig
        }

model_engine = PretrainedInstitutionalEngine()

# ================= ================= =================
# 3. إدارة رأس المال والتنفيذ الذكي (5-Sec Smart Fallback)
# ================= ================= =================

def calculate_half_kelly_qty(option_mid_price, win_probability):
    try:
        acc = alpaca_get(f"{TRADING_URL}/v2/account")
        equity = num(acc.get("equity"), 1000.0)

        p = max(0.51, min(win_probability, 0.90))
        q = 1.0 - p
        b = 2.0
        kelly_f = (p * b - q) / b
        half_kelly = max(0.015, min(kelly_f * 0.5, MAX_RISK_CAP))

        trade_budget = equity * half_kelly
        contract_cost = option_mid_price * 100

        if contract_cost <= 0 or contract_cost > (equity * (MAX_RISK_CAP * 1.5)):
            return 0

        qty = math.floor(trade_budget / contract_cost)
        return max(1 if contract_cost <= trade_budget * 1.5 else 0, min(qty, 50))
    except Exception as e:
        log.error(f"⚠️ خطأ في حساب كمية التداول: {e}")
        return 0

def execute_smart_limit_order(symbol, qty, side, quotes):
    """
    تعديل التسعير الأولي: استخدام سعر المنتصف (Mid) للشراء والبيع،
    والتحول لسعر العرض المباشر (Ask/Bid) في حال الانقضاء.
    """
    if qty <= 0 or not quotes:
        return None

    mid_price = quotes["mid"]
    ask_price = quotes["ask"]
    bid_price = quotes["bid"]

    # 👈 سعر البداية دائماً سعر المنتصف (Mid)
    target_price = mid_price
    fallback_price = ask_price if side == "buy" else bid_price

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "limit",
        "limit_price": str(round(target_price, 2)),
        "time_in_force": "day"
    }

    try:
        order = alpaca_post(f"{TRADING_URL}/v2/orders", payload)
        order_id = order.get("id")
        log.info(f"⏳ إرسال أمر حد أولي ({side.upper()}) بسعر المنتصف ${target_price:.2f} | العقد: {symbol}")

        start_time = time.time()
        while time.time() - start_time < EXECUTION_TIMEOUT:
            time.sleep(1)
            check_order = alpaca_get(f"{TRADING_URL}/v2/orders/{order_id}")
            if check_order.get("status") in ["filled", "partially_filled"]:
                log.info(f"⚡ تم تنفيذ الأمر بنجاح عند سعر المنتصف ${target_price:.2f}")
                return check_order

        # لم ينفذ خلال 5 ثوانٍ -> إلغاء وتعديل للسعر المباشر
        log.warning(f"⚠️ لم ينفذ الأمر خلال {EXECUTION_TIMEOUT} ثوانٍ. تعديل السعر إلى ${fallback_price:.2f}...")
        alpaca_delete(f"{TRADING_URL}/v2/orders/{order_id}")
        time.sleep(0.5)

        fallback_payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "limit",
            "limit_price": str(round(fallback_price, 2)),
            "time_in_force": "day"
        }
        final_order = alpaca_post(f"{TRADING_URL}/v2/orders", fallback_payload)
        log.info(f"✅ تم تنفيذ الأمر المباشر بسعر (${fallback_price:.2f}) بنجاح.")
        return final_order

    except Exception as e:
        log.error(f"❌ خطأ أثناء تنفيذ الأمر الذكي: {e}")
        return None

def fetch_spy_contracts(direction, spy_price):
    today = now_ny().date()
    target_exp_end = (now_ny() + timedelta(days=2)).date()
    params = {
        "underlying_symbols": HYPER_ASSET,
        "status": "active",
        "type": "call" if direction == "صعود 🚀" else "put",
        "expiration_date_gte": today.isoformat(),
        "expiration_date_lte": target_exp_end.isoformat(),
        "limit": 20,
    }
    payload = alpaca_get(f"{TRADING_URL}/v2/options/contracts", params)
    contracts = payload.get("option_contracts") or payload.get("contracts") or []
    candidates = []

    for c in contracts:
        strike = num(c.get("strike_price"))
        sym = c.get("symbol")
        if sym and math.isfinite(strike) and abs(strike - spy_price) <= (spy_price * 0.012):
            candidates.append((abs(strike - spy_price), c))

    candidates.sort(key=lambda x: x[0])
    return [c for _, c in candidates[:2]]

def _get_option_quote(symbol):
    payload = alpaca_get(f"{DATA_URL}/v1beta1/options/quotes/latest", {"symbols": symbol, "feed": OPTION_FEED})
    q = (payload.get("quotes") or {}).get(symbol, {})
    bid, ask = num(q.get("bp")), num(q.get("ap"))
    if not math.isfinite(bid) or bid <= 0 or not math.isfinite(ask) or ask < bid:
        return None
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}

# ================= ================= =================
# 4. إدارة الأرباح المفتوحة وكاسر الحماية (Trailing Shield)
# ================= ================= =================

def close_all_positions_immediately():
    cancel_all_open_orders()
    try:
        r = HTTP.get(f"{TRADING_URL}/v2/positions", headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}, timeout=10)
        positions = r.json() if r.status_code == 200 else []
        for pos in positions:
            sym = pos.get("symbol")
            qty = pos.get("qty")
            if sym and qty:
                q = _get_option_quote(sym)
                price = q["bid"] if q else num(pos.get("current_price"), 0.1)
                execute_smart_limit_order(sym, qty=abs(int(qty)), side="sell", quotes={"mid": price, "ask": price, "bid": price})
        save_json(ACTIVE_POSITIONS_FILE, {})
        log.warning("🛑 تم إغلاق جميع الصفقات فوراً عبر كاسر الحماية اليومي.")
    except Exception as e:
        log.error(f"خطأ أثناء الإغلاق الشامل للمحفظة: {e}")

def check_daily_circuit_breaker():
    try:
        acc = alpaca_get(f"{TRADING_URL}/v2/account")
        equity = num(acc.get("equity"), 0.0)
        last_equity = num(acc.get("last_equity"), equity)

        if last_equity <= 0:
            return False

        daily_pnl_pct = (equity - last_equity) / last_equity
        if daily_pnl_pct <= -MAX_DAILY_LOSS_PCT:
            log.warning(f"🛑 [Circuit Breaker] خسارة اليوم ({daily_pnl_pct*100:.2f}%). إغلاق وتجميد التداول.")
            close_all_positions_immediately()
            return True
    except Exception as e:
        log.error(f"خطأ أثناء فحص كاسر الحماية: {e}")
    return False

def manage_active_positions():
    saved_positions = read_json(ACTIVE_POSITIONS_FILE)
    try:
        r = HTTP.get(f"{TRADING_URL}/v2/positions", headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}, timeout=10)
        positions_payload = r.json() if r.status_code == 200 else []
        active_alpaca_symbols = set()

        open_orders = get_open_orders()
        pending_buy_symbols = set(o.get("symbol") for o in open_orders if o.get("side") == "buy")
        pending_sell_symbols = set(o.get("symbol") for o in open_orders if o.get("side") == "sell")

        force_eod_close = is_eod_close_time()

        if isinstance(positions_payload, list):
            for pos in positions_payload:
                sym = pos.get("symbol")
                if not sym or not sym.startswith("SPY"):
                    continue

                active_alpaca_symbols.add(sym)
                qty = abs(int(num(pos.get("qty"))))
                entry_price = num(pos.get("avg_entry_price"))
                current_price = num(pos.get("current_price"))

                if not math.isfinite(entry_price) or entry_price <= 0 or not math.isfinite(current_price) or current_price <= 0:
                    continue

                pos_data = saved_positions.get(sym, {})
                highest_price = max(pos_data.get("highest_price", current_price), current_price)

                pnl_pct = (current_price - entry_price) / entry_price
                peak_pnl_pct = (highest_price - entry_price) / entry_price

                saved_positions[sym] = {
                    "symbol": sym,
                    "entry_price": entry_price,
                    "highest_price": highest_price,
                    "qty": qty,
                    "time": pos_data.get("time", stamp())
                }

                if sym in pending_sell_symbols:
                    continue

                q = _get_option_quote(sym)
                exit_price = q["bid"] if q else current_price

                should_close = False
                reason = ""

                if force_eod_close:
                    should_close = True
                    reason = "إغلاق نهاية اليوم (EOD Exit)"

                elif peak_pnl_pct >= 1.00 and pnl_pct <= (peak_pnl_pct * 0.88):
                    should_close = True
                    reason = f"حماية أرباح انفجارية دبل+ (قمة: +{peak_pnl_pct*100:.1f}%) | الربح: +{pnl_pct*100:.2f}%"

                elif peak_pnl_pct >= 0.50 and pnl_pct <= (peak_pnl_pct * 0.78):
                    should_close = True
                    reason = f"حماية أرباح عالية (قمة: +{peak_pnl_pct*100:.1f}%) | الربح: +{pnl_pct*100:.2f}%"

                elif peak_pnl_pct >= 0.25 and pnl_pct <= (peak_pnl_pct * 0.68):
                    should_close = True
                    reason = f"حماية أرباح ممتازة | الربح: +{pnl_pct*100:.2f}%"

                elif peak_pnl_pct >= 0.10 and pnl_pct < 0.10:
                    should_close = True
                    reason = f"جدار حماية +10% الصارم (قمة +{peak_pnl_pct*100:.1f}%)"

                elif peak_pnl_pct >= 0.05 and pnl_pct <= 0.025:
                    should_close = True
                    reason = f"تأمين أرباح مبكرة (+2.5%+) بعد قمة +{peak_pnl_pct*100:.1f}%"

                elif pnl_pct <= -0.055:
                    should_close = True
                    reason = f"وقف خسارة خاطف (-5.5%) لـ {sym}"

                if should_close:
                    log.info(f"📉 [إغلاق صفقة] {sym} | السبب: {reason} | صافي الـ PnL: {pnl_pct*100:+.2f}%")
                    execute_smart_limit_order(sym, qty=qty, side="sell", quotes={"mid": exit_price, "ask": exit_price, "bid": exit_price})

        # 👈 حماية الصفقات المعلقة من الحذف الذاتي أثناء التنفيذ
        to_remove = [
            s for s in saved_positions 
            if s not in active_alpaca_symbols and s not in pending_buy_symbols
        ]
        for s in to_remove:
            saved_positions.pop(s, None)

        save_json(ACTIVE_POSITIONS_FILE, saved_positions)
    except Exception as e:
        log.error(f"⚠️ خطأ أثناء إدارة الصفقات: {e}")

def open_new_trade():
    if check_daily_circuit_breaker():
        return

    positions = read_json(ACTIVE_POSITIONS_FILE)
    open_orders = get_open_orders()

    if len(positions) >= 1 or len(open_orders) >= 1:
        return

    spy_bars = fetch_bars(HYPER_ASSET)
    if spy_bars.empty or len(spy_bars) < 20:
        return

    qqq_bars = fetch_bars(NASDAQ_ASSET)
    pred = model_engine.predict_fast(spy_bars, qqq_bars)

    if pred["signal"] == "محايد ⏳":
        return

    contracts = fetch_spy_contracts(pred["signal"], pred["current_price"])
    for c in contracts:
        sym = c.get("symbol")
        if not sym or sym in positions:
            continue
        q = _get_option_quote(sym)
        if not q:
            continue

        spread = (q["ask"] - q["bid"]) / q["mid"]
        if spread > MAX_SPREAD_PCT:
            log.info(f"تخطي العقد {sym} بسبب اتساع Spread ({spread*100:.1f}%)")
            continue

        trade_qty = calculate_half_kelly_qty(q["mid"], pred["confidence"])
        if trade_qty <= 0:
            continue

        cancel_all_open_orders()
        order = execute_smart_limit_order(sym, qty=trade_qty, side="buy", quotes=q)
        if order:
            positions[sym] = {
                "symbol": sym,
                "underlying": HYPER_ASSET,
                "entry_price": q["mid"],
                "highest_price": q["mid"],
                "qty": trade_qty,
                "time": stamp()
            }
            save_json(ACTIVE_POSITIONS_FILE, positions)
            log.info(f"🚀 [تنفيذ صفقة جديدة] العقد: {sym} | الكمية: {trade_qty} | السعر: ${q['mid']:.2f} | الإشارة: {pred['signal']} | الثقة: {pred['confidence']*100:.1f}%")
            return

# ================= ================= =================
# 5. حلقة التشغيل الرئيسية
# ================= ================= =================

if __name__ == "__main__":
    log.info(f"🚀 بدء تشغيل محرك التداول التنفيذي الصامت {NAME} {VERSION}")

    while True:
        try:
            if is_premarket() and not model_engine.is_trained:
                model_engine.train_model()

            elif market_open():
                manage_active_positions()
                if in_signal_window() and not is_eod_close_time():
                    open_new_trade()

            else:
                log.info("⏳ السوق مغلق حالياً.. المحرك في وضع الانتظار والترقب.")

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("تم إيقاف المحرك التنفيذي يدوياً.")
            break
        except Exception as e:
            log.exception(f"⚠️ خطأ غير متوقع في حلقة التشغيل: {e}")
            time.sleep(15)
