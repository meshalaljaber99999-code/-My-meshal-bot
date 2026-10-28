# -*- coding: utf-8 -*-
"""
صَفاء التحليل v60.3 — محرك تداول تنفيذي صامت (نسخة مُصلحة)
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

# ================= الإعدادات =================

NAME = "صَفاء التحليل - المحرك التنفيذي"
VERSION = "v60.3-SilentExecution-Fixed"

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

HISTORY_DAYS = 30        # للتدريب
LIVE_DAYS = 5            # للتنبؤ اللحظي (أخف على الـ API)
MIN_ROWS = 300
HORIZON = 5

MAX_SPREAD_PCT = 0.045
POLL_SECONDS = 10
SCAN_SECONDS = 30        # كل كم ثانية يحلل السوق ويبحث عن صفقة
EXECUTION_TIMEOUT = 5

MAX_DAILY_LOSS_PCT = 0.02
MAX_RISK_CAP = 0.065
# يمكن تغييرها من Railway Variables بدون تعديل الكود: MIN_CONFIDENCE=0.70
MIN_CONFIDENCE_THRESHOLD = float(os.getenv("MIN_CONFIDENCE", "0.65"))
MIN_VOL_VELOCITY = float(os.getenv("MIN_VOL_VELOCITY", "1.15"))

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

STATE = {"last_scan": 0.0}
_LAST_LOG = {}


def log_every(key, seconds, msg, level=logging.INFO):
    t = time.time()
    if t - _LAST_LOG.get(key, 0.0) >= seconds:
        _LAST_LOG[key] = t
        log.log(level, msg)


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


def _headers():
    return {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}


def alpaca_get(url, params=None):
    if not KEY or not SECRET:
        raise RuntimeError("مفاتيح API غير مضبوطة في النظام")
    last = None
    for attempt in range(3):
        try:
            r = HTTP.get(url, headers=_headers(), params=params, timeout=10)
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
            r = HTTP.post(url, headers=_headers(), json=payload, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Alpaca POST request failed: {last}")


def alpaca_delete(url):
    try:
        r = HTTP.delete(url, headers=_headers(), timeout=8)
        return r.status_code in (200, 204, 207)
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
    return n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) < (16, 0)


def is_premarket():
    n = now_ny()
    return n.weekday() < 5 and (8, 0) <= (n.hour, n.minute) < (9, 30)


def in_signal_window():
    n = now_ny()
    return n.weekday() < 5 and (9, 45) <= (n.hour, n.minute) < (15, 30)


def is_eod_close_time():
    n = now_ny()
    return n.weekday() < 5 and (n.hour, n.minute) >= (15, 50)


# ================= 1. البيانات والميزات =================

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
    return (
        df.dropna(subset=["timestamp", "open", "high", "low", "close"])
        .drop_duplicates("timestamp", keep="last")
        .sort_values("timestamp")
        .set_index("timestamp")
    )


def calculate_0dte_gamma_exposure(symbol, spot):
    """
    مؤشر تقريبي لمستوى التثبيت (Pin) مبني على الفائدة المفتوحة (Open Interest)
    لعقود اليوم القريبة من السعر. ليس GEX حقيقياً، لكنه أفضل من وسيط السترايكات.
    """
    today_str = now_ny().date().isoformat()
    try:
        payload = alpaca_get(f"{TRADING_URL}/v2/options/contracts", {
            "underlying_symbols": symbol,
            "expiration_date": today_str,
            "status": "active",
            "strike_price_gte": str(math.floor(spot * 0.97)),
            "strike_price_lte": str(math.ceil(spot * 1.03)),
            "limit": 500,
        })
        contracts = payload.get("option_contracts") or payload.get("contracts") or []
        strikes, weights = [], []
        for c in contracts:
            k = num(c.get("strike_price"))
            oi = num(c.get("open_interest"), 0.0)
            if k > 0 and oi > 0:
                strikes.append(k)
                weights.append(oi)
        if not strikes:
            return 0, spot

        pin = float(np.average(strikes, weights=weights))
        if spot > pin * 1.0015:
            return 1, pin
        elif spot < pin * 0.9985:
            return -1, pin
        return 0, pin
    except Exception:
        return 0, spot


# الميزات تُحسب الآن لكل صف (دقيقة) وليس رقماً واحداً منسوخاً
FEATURES = [
    "ret1", "ret3", "ret6", "ret12",
    "ema_9_21", "ema_21_50", "trend_200",
    "vwap_zscore", "rsi", "rsi_slope",
    "macd_hist", "vol_surge", "vol_velocity",
    "sweep_signal", "absorption",
    "nq_lead_ret", "nq_rel_strength",
]


def generate_institutional_features(spy_bars, qqq_bars=None):
    f = spy_bars.copy()
    c, v, h, l = f["close"], f["volume"], f["high"], f["low"]

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

    # VWAP يومي (يعاد حسابه كل يوم تداول) + Z-score
    day = pd.Series(f.index.tz_convert(NY).date, index=f.index)
    cum_pv = (c * v).groupby(day).cumsum()
    cum_v = v.groupby(day).cumsum().replace(0, np.nan)
    vwap = cum_pv / cum_v
    f["vwap_zscore"] = (c - vwap) / (c.rolling(15).std() + 1e-6)

    # سرعة الحجم
    recent_vol = v.rolling(3).mean()
    prior_vol = v.shift(3).rolling(12).mean().replace(0, np.nan)
    f["vol_velocity"] = (recent_vol / prior_vol).fillna(1.0)
    f["vol_surge"] = v / v.rolling(20).mean().replace(0, np.nan)

    # الامتصاص: شمعة ضيقة بحجم عال
    rng = h - l
    f["absorption"] = (
        (rng < rng.rolling(10).mean() * 0.65) & (v > v.rolling(10).mean() * 1.5)
    ).astype(int)

    # كنس السيولة
    prev_high = h.shift(1).rolling(20).max()
    prev_low = l.shift(1).rolling(20).min()
    avg_vol = v.shift(1).rolling(14).mean()
    big_vol = v > avg_vol * 1.35
    sweep_up = (l < prev_low) & (c > prev_low) & big_vol      # كنس قاع ثم ارتداد = صعود
    sweep_down = (h > prev_high) & (c < prev_high) & big_vol  # كنس قمة ثم رفض = نزول
    f["sweep_signal"] = np.where(sweep_up, 1, np.where(sweep_down, -1, 0))

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    f["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / c

    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = (100 - 100 / (1 + rs)) / 100
    f["rsi"] = rsi.fillna(0.5)
    f["rsi_slope"] = rsi.diff(3).fillna(0)

    if qqq_bars is not None and not qqq_bars.empty:
        qqq_c = qqq_bars["close"].reindex(f.index).ffill()
        f["nq_lead_ret"] = qqq_c.pct_change().fillna(0)
        f["nq_rel_strength"] = (f["nq_lead_ret"] - f["ret1"]).fillna(0)
    else:
        f["nq_lead_ret"] = 0.0
        f["nq_rel_strength"] = 0.0

    return f.replace([np.inf, -np.inf], np.nan)


# ================= 2. محرك التعلم الآلي =================

class PretrainedInstitutionalEngine:
    def __init__(self):
        self.primary_model = None
        self.meta_model = None
        self.is_trained = False
        self.last_trained_time = None
        self.last_attempt = 0.0

    def needs_training(self):
        if time.time() - self.last_attempt < 300:  # لا تعيد المحاولة قبل 5 دقائق
            return False
        if not self.is_trained:
            return True
        return self.last_trained_time.date() != now_ny().date()  # إعادة تدريب يومية

    def train_model(self):
        self.last_attempt = time.time()
        log.info("🧠 جاري تدريب LightGBM + Meta-Labeling...")
        try:
            spy_bars = fetch_bars(HYPER_ASSET, days=HISTORY_DAYS)
            qqq_bars = fetch_bars(NASDAQ_ASSET, days=HISTORY_DAYS)
        except Exception as e:
            log.error(f"فشل جلب بيانات التدريب: {e}")
            return False

        if spy_bars.empty or len(spy_bars) < MIN_ROWS:
            log.warning("⚠️ لا توجد بيانات كافية لتدريب النموذج.")
            return False

        f = generate_institutional_features(spy_bars, qqq_bars)
        close = spy_bars["close"]
        future_ret = close.shift(-HORIZON) / close - 1

        # منع الهدف من عبور يومين مختلفين (فجوة الافتتاح)
        day = pd.Series(spy_bars.index.tz_convert(NY).date, index=spy_bars.index)
        same_day = day.shift(-HORIZON) == day

        target = pd.Series(0, index=f.index)
        target[future_ret > 0.0012] = 1
        target[future_ret < -0.0012] = -1
        target[~same_day] = 0
        f["primary_target"] = target

        ds = f[FEATURES + ["primary_target"]].dropna()
        ds_valid = ds[ds["primary_target"] != 0].copy()

        if len(ds_valid) < MIN_ROWS or ds_valid["primary_target"].nunique() < 2:
            log.warning("⚠️ عينات البيانات غير كافية للتدريب.")
            return False

        # تقسيم زمني: 70% للنموذج الأساسي، 30% للنموذج الوصفي (خارج العينة)
        split = int(len(ds_valid) * 0.7)
        train_df = ds_valid.iloc[:split]
        hold_df = ds_valid.iloc[split:].copy()

        if train_df["primary_target"].nunique() < 2:
            log.warning("⚠️ بيانات التدريب لا تحوي الاتجاهين.")
            return False

        if HAS_LGB:
            clf = lgb.LGBMClassifier(n_estimators=180, learning_rate=0.03, max_depth=4,
                                     random_state=42, verbose=-1)
        else:
            clf = GradientBoostingClassifier(n_estimators=180, learning_rate=0.03,
                                             max_depth=4, random_state=42)

        primary = make_pipeline(SimpleImputer(strategy="median"), clf)
        primary.fit(train_df[FEATURES], train_df["primary_target"])

        meta = None
        if len(hold_df) >= 50:
            hold_df["pred"] = primary.predict(hold_df[FEATURES])
            hold_df["meta_target"] = (hold_df["pred"] == hold_df["primary_target"]).astype(int)
            if hold_df["meta_target"].nunique() >= 2:
                meta = make_pipeline(
                    SimpleImputer(strategy="median"),
                    RandomForestClassifier(n_estimators=120, max_depth=4, random_state=42),
                )
                meta.fit(hold_df[FEATURES], hold_df["meta_target"])

        self.primary_model = primary
        self.meta_model = meta
        self.is_trained = True
        self.last_trained_time = now_ny()
        log.info(f"✅ تم التدريب بنجاح | عينات: {len(ds_valid)} | نموذج وصفي: {'نعم' if meta else 'لا'}")
        return True

    def predict_fast(self, spy_bars, qqq_bars):
        f = generate_institutional_features(spy_bars, qqq_bars)
        latest_feat = f[FEATURES].iloc[[-1]]

        primary_pred = self.primary_model.predict(latest_feat)[0]
        probs = self.primary_model.predict_proba(latest_feat)[0]
        classes = list(self.primary_model.steps[-1][1].classes_)
        prob_max = float(probs[classes.index(primary_pred)]) if primary_pred in classes else 0.5

        meta_prob = 0.50
        if self.meta_model:
            meta_classes = list(self.meta_model.steps[-1][1].classes_)
            if 1 in meta_classes:
                meta_prob = float(self.meta_model.predict_proba(latest_feat)[0, meta_classes.index(1)])

        curr_price = float(spy_bars["close"].iloc[-1])
        vol_vel = float(f["vol_velocity"].iloc[-1])
        sweep_sig = int(f["sweep_signal"].iloc[-1])
        gamma_sig, pin = calculate_0dte_gamma_exposure(HYPER_ASSET, curr_price)

        confidence = (prob_max + meta_prob) / 2.0
        signal = "محايد ⏳"

        if (primary_pred == 1 or sweep_sig == 1) and confidence >= MIN_CONFIDENCE_THRESHOLD:
            if gamma_sig >= 0 and vol_vel >= MIN_VOL_VELOCITY:
                signal = "صعود 🚀"
        elif (primary_pred == -1 or sweep_sig == -1) and confidence >= MIN_CONFIDENCE_THRESHOLD:
            if gamma_sig <= 0 and vol_vel >= MIN_VOL_VELOCITY:
                signal = "نزول 📉"

        return {
            "signal": signal,
            "confidence": confidence,
            "primary_pred": int(primary_pred),
            "prob_max": prob_max,
            "meta_prob": meta_prob,
            "current_price": curr_price,
            "vol_velocity": vol_vel,
            "sweep_signal": sweep_sig,
            "gamma_signal": gamma_sig,
            "pin": pin,
        }


model_engine = PretrainedInstitutionalEngine()


# ================= 3. إدارة رأس المال والتنفيذ =================

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
            log.info(f"تخطي: تكلفة العقد ${contract_cost:.0f} أكبر من الحد المسموح (محفظة ${equity:.0f})")
            return 0

        qty = math.floor(trade_budget / contract_cost)
        return max(1 if contract_cost <= trade_budget * 1.5 else 0, min(qty, 50))
    except Exception as e:
        log.error(f"⚠️ خطأ في حساب كمية التداول: {e}")
        return 0


def execute_smart_limit_order(symbol, qty, side, quotes):
    """أمر حد بسعر المنتصف، وإن لم ينفذ خلال المهلة يُلغى ويُرسل بسعر Ask/Bid."""
    if qty <= 0 or not quotes:
        return None

    mid_price = quotes["mid"]
    fallback_price = quotes["ask"] if side == "buy" else quotes["bid"]

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "limit",
        "limit_price": str(round(mid_price, 2)),
        "time_in_force": "day",
    }

    try:
        order = alpaca_post(f"{TRADING_URL}/v2/orders", payload)
        order_id = order.get("id")
        log.info(f"⏳ أمر حد ({side.upper()}) بسعر المنتصف ${mid_price:.2f} | {symbol} | كمية {qty}")

        start_time = time.time()
        while time.time() - start_time < EXECUTION_TIMEOUT:
            time.sleep(1)
            chk = alpaca_get(f"{TRADING_URL}/v2/orders/{order_id}")
            if chk.get("status") == "filled":
                log.info(f"⚡ تم التنفيذ الكامل عند سعر المنتصف ${mid_price:.2f}")
                return chk

        # انتهت المهلة: إلغاء ثم فحص ما نُفذ فعلاً
        alpaca_delete(f"{TRADING_URL}/v2/orders/{order_id}")
        time.sleep(0.5)
        chk = alpaca_get(f"{TRADING_URL}/v2/orders/{order_id}")
        if chk.get("status") == "filled":
            return chk

        filled_qty = int(num(chk.get("filled_qty"), 0))
        remaining = qty - filled_qty
        if remaining <= 0:
            return chk

        log.warning(f"⚠️ لم ينفذ كاملاً. إرسال المتبقي ({remaining}) بسعر ${fallback_price:.2f}")
        fallback_payload = {
            "symbol": symbol,
            "qty": str(remaining),
            "side": side,
            "type": "limit",
            "limit_price": str(round(fallback_price, 2)),
            "time_in_force": "day",
        }
        final_order = alpaca_post(f"{TRADING_URL}/v2/orders", fallback_payload)
        log.info(f"📨 تم إرسال أمر مباشر بسعر ${fallback_price:.2f}")
        return final_order

    except Exception as e:
        log.error(f"❌ خطأ أثناء تنفيذ الأمر الذكي: {e}")
        return None


def fetch_spy_contracts(direction, spy_price):
    today = now_ny().date()
    params = {
        "underlying_symbols": HYPER_ASSET,
        "status": "active",
        "type": "call" if direction == "صعود 🚀" else "put",
        "expiration_date_gte": today.isoformat(),
        "expiration_date_lte": (today + timedelta(days=2)).isoformat(),
        "strike_price_gte": str(math.floor(spy_price * 0.99)),
        "strike_price_lte": str(math.ceil(spy_price * 1.01)),
        "limit": 200,
    }
    payload = alpaca_get(f"{TRADING_URL}/v2/options/contracts", params)
    contracts = payload.get("option_contracts") or payload.get("contracts") or []
    candidates = []
    for c in contracts:
        strike = num(c.get("strike_price"))
        sym = c.get("symbol")
        if sym and math.isfinite(strike):
            candidates.append((round(abs(strike - spy_price), 2), c.get("expiration_date") or "", c))

    candidates.sort(key=lambda x: (x[0], x[1]))
    return [c for _, _, c in candidates[:3]]


def _get_option_quote(symbol):
    payload = alpaca_get(f"{DATA_URL}/v1beta1/options/quotes/latest", {"symbols": symbol, "feed": OPTION_FEED})
    q = (payload.get("quotes") or {}).get(symbol, {})
    bid, ask = num(q.get("bp")), num(q.get("ap"))
    if not math.isfinite(bid) or bid <= 0 or not math.isfinite(ask) or ask < bid:
        return None
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}


# ================= 4. إدارة الصفقات وكاسر الحماية =================

def close_all_positions_immediately():
    cancel_all_open_orders()
    try:
        r = HTTP.get(f"{TRADING_URL}/v2/positions", headers=_headers(), timeout=10)
        positions = r.json() if r.status_code == 200 else []
        for pos in positions:
            sym = pos.get("symbol")
            qty = pos.get("qty")
            if sym and qty:
                q = _get_option_quote(sym)
                price = q["bid"] if q else num(pos.get("current_price"), 0.1)
                execute_smart_limit_order(sym, qty=abs(int(num(qty))), side="sell",
                                          quotes={"mid": price, "ask": price, "bid": price})
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
        r = HTTP.get(f"{TRADING_URL}/v2/positions", headers=_headers(), timeout=10)
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
                qty = abs(int(num(pos.get("qty"), 0)))
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
                    "time": pos_data.get("time", stamp()),
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
                    execute_smart_limit_order(sym, qty=qty, side="sell",
                                              quotes={"mid": exit_price, "ask": exit_price, "bid": exit_price})

        # حماية الصفقات المعلقة من الحذف أثناء التنفيذ
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
        log_every("busy", 300, "ℹ️ توجد صفقة أو أمر مفتوح، لن يتم البحث عن صفقة جديدة.")
        return

    if time.time() - STATE["last_scan"] < SCAN_SECONDS:
        return
    STATE["last_scan"] = time.time()

    if not model_engine.is_trained:
        log_every("untrained", 120, "⚠️ النموذج غير مدرب بعد، بانتظار التدريب.", logging.WARNING)
        return

    try:
        spy_bars = fetch_bars(HYPER_ASSET, days=LIVE_DAYS)
        if spy_bars.empty or len(spy_bars) < 60:
            log_every("nodata", 120, "⚠️ بيانات SPY غير كافية حالياً.", logging.WARNING)
            return
        qqq_bars = fetch_bars(NASDAQ_ASSET, days=LIVE_DAYS)
        pred = model_engine.predict_fast(spy_bars, qqq_bars)
    except Exception as e:
        log.error(f"⚠️ خطأ أثناء التحليل: {e}")
        return

    # نبض كل دقيقة ليظهر لك أن البوت يعمل ويبحث
    log_every(
        "heartbeat", 60,
        f"💓 نبض | SPY ${pred['current_price']:.2f} | الإشارة: {pred['signal']} | "
        f"اتجاه النموذج: {pred['primary_pred']:+d} | الثقة: {pred['confidence']*100:.1f}% "
        f"(الحد {MIN_CONFIDENCE_THRESHOLD*100:.0f}%) | سرعة الحجم: {pred['vol_velocity']:.2f} "
        f"(الحد {MIN_VOL_VELOCITY}) | Sweep: {pred['sweep_signal']} | Gamma: {pred['gamma_signal']}",
    )

    if pred["signal"] == "محايد ⏳":
        return

    contracts = fetch_spy_contracts(pred["signal"], pred["current_price"])
    if not contracts:
        log.info(f"ℹ️ إشارة {pred['signal']} لكن لا توجد عقود مناسبة قريبة من السعر.")
        return

    for c in contracts:
        sym = c.get("symbol")
        if not sym or sym in positions:
            continue
        q = _get_option_quote(sym)
        if not q:
            log.info(f"تخطي {sym}: لا يوجد تسعير صالح (Bid/Ask).")
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
                "time": stamp(),
            }
            save_json(ACTIVE_POSITIONS_FILE, positions)
            log.info(f"🚀 [صفقة جديدة] {sym} | كمية: {trade_qty} | ${q['mid']:.2f} | "
                     f"{pred['signal']} | ثقة: {pred['confidence']*100:.1f}%")
            return


# ================= 5. الحلقة الرئيسية =================

if __name__ == "__main__":
    log.info(f"🚀 بدء تشغيل محرك التداول التنفيذي الصامت {NAME} {VERSION}")
    log.info(f"الإعدادات: ثقة ≥ {MIN_CONFIDENCE_THRESHOLD*100:.0f}% | سرعة حجم ≥ {MIN_VOL_VELOCITY} | {TRADING_URL}")

    while True:
        try:
            if (is_premarket() or market_open()) and model_engine.needs_training():
                model_engine.train_model()

            if market_open():
                manage_active_positions()
                if in_signal_window() and not is_eod_close_time():
                    open_new_trade()
                else:
                    log_every("window", 300, "⏳ السوق مفتوح لكن خارج نافذة الإشارات (9:45–15:30 نيويورك).")
            else:
                log_every("closed", 300, "⏳ السوق مغلق حالياً.. المحرك في وضع الانتظار.")

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("تم إيقاف المحرك التنفيذي يدوياً.")
            break
        except Exception as e:
            log.exception(f"⚠️ خطأ غير متوقع في حلقة التشغيل: {e}")
            time.sleep(15)
