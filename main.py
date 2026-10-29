# -*- coding: utf-8 -*-
"""
صَفاء التحليل v64.1 — محرك التداول التنفيذي النهائي (إصلاح دالة num + حماية متكاملة)
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

from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier, GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline

# ================= الإعدادات المتقدمة =================

NAME = "صَفاء التحليل - Ultra Engine"
VERSION = "v64.1-ProductionReady"

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
TF = "1Min"

HYPER_ASSET = "SPY"
NASDAQ_ASSET = "QQQ"

DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
KEY = (os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or "").strip()
SECRET = (os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY") or "").strip()
STOCK_FEED = os.getenv("ALPACA_DATA_FEED", "sip").lower()
OPTION_FEED = os.getenv("OPTIONS_FEED", "indicative").lower()

HISTORY_DAYS = 90             
LIVE_DAYS = 5
MIN_ROWS = 1200
HORIZON = 5                   

MAX_SPREAD_PCT = 0.025        
MIN_OPEN_INTEREST = 300       
POLL_SECONDS = 15
POSITION_POLL_SECONDS = 3
SCAN_SECONDS = 20
EXECUTION_TIMEOUT = 5

MIN_DTE = 1                   
MAX_DTE = 3
MIN_DELTA = 0.40
MAX_DELTA = 0.65
MAX_IV_THRESHOLD = 0.45       

MAX_TIME_IN_TRADE_MINUTES = 20  
MAX_DAILY_LOSS_PCT = 0.02
MAX_RISK_CAP = 0.05
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "0.06"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.35"))
MIN_CONFIDENCE_THRESHOLD = float(os.getenv("MIN_CONFIDENCE", "0.68"))
MIN_VOL_VELOCITY = float(os.getenv("MIN_VOL_VELOCITY", "1.20"))

ROOT = Path(os.getenv("DATA_DIR", "./data"))
ROOT.mkdir(parents=True, exist_ok=True)
ACTIVE_POSITIONS_FILE = ROOT / "spy_ultratitan_positions.json"
JOURNAL_FILE = ROOT / "trade_journal.csv"

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


# ================= 1. نظام تتبع الأداء =================

def log_completed_trade(symbol, entry_price, exit_price, qty, pnl_pct, reason):
    pnl_usd = (exit_price - entry_price) * qty * 100
    r_multiple = pnl_pct / STOP_LOSS_PCT
    row = {
        "timestamp": stamp(),
        "symbol": symbol,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "qty": qty,
        "pnl_usd": round(pnl_usd, 2),
        "pnl_pct": round(pnl_pct, 4),
        "r_multiple": round(r_multiple, 2),
        "exit_reason": reason,
    }
    df_row = pd.DataFrame([row])
    if not JOURNAL_FILE.exists():
        df_row.to_csv(JOURNAL_FILE, index=False)
    else:
        df_row.to_csv(JOURNAL_FILE, mode="a", header=False, index=False)

    calculate_and_log_metrics()


def calculate_and_log_metrics():
    if not JOURNAL_FILE.exists():
        return
    try:
        df = pd.read_csv(JOURNAL_FILE)
        if df.empty:
            return
        total_trades = len(df)
        wins = df[df["pnl_usd"] > 0]
        losses = df[df["pnl_usd"] <= 0]
        
        win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
        avg_r = df["r_multiple"].mean() if total_trades > 0 else 0.0
        
        avg_win_r = wins["r_multiple"].mean() if len(wins) > 0 else 0.0
        avg_loss_r = abs(losses["r_multiple"].mean()) if len(losses) > 0 else 1.0
        
        expectancy = (win_rate * avg_win_r) - ((1.0 - win_rate) * avg_loss_r)
        total_pnl = df["pnl_usd"].sum()
        
        log.info(
            f"📊 [سجل الأداء] صفقات: {total_trades} | نسبة النجاح: {win_rate*100:.1f}% | "
            f"متوسط R: {avg_r:.2f} | Expectancy: {expectancy:.2f}R | إجمالي الـ PnL: ${total_pnl:+.2f}"
        )
    except Exception as e:
        log.error(f"خطأ في حساب مقاييس الأداء: {e}")


# ================= 2. Snapshots لليونانيات =================

def fetch_option_snapshots_specific(symbols_list):
    if not symbols_list:
        return {}
    symbols_param = ",".join(symbols_list)
    url = f"{DATA_URL}/v1beta1/options/snapshots"
    try:
        res = alpaca_get(url, {"symbols": symbols_param, "feed": OPTION_FEED})
        return res.get("snapshots") or {}
    except Exception as e:
        log.error(f"فشل جلب Snapshots للعقود المحددة: {e}")
        return {}


def fetch_spy_contracts_with_greeks(direction, spy_price):
    today = now_ny().date()
    min_exp = today + timedelta(days=MIN_DTE)
    max_exp = today + timedelta(days=MAX_DTE)

    params = {
        "underlying_symbols": HYPER_ASSET,
        "status": "active",
        "type": "call" if direction == "صعود 🚀" else "put",
        "expiration_date_gte": min_exp.isoformat(),
        "expiration_date_lte": max_exp.isoformat(),
        "strike_price_gte": str(math.floor(spy_price * 0.985)),
        "strike_price_lte": str(math.ceil(spy_price * 1.015)),
        "limit": 50,
    }
    try:
        payload = alpaca_get(f"{TRADING_URL}/v2/options/contracts", params)
        contracts = payload.get("option_contracts") or payload.get("contracts") or []
    except Exception as e:
        log.error(f"خطأ في استعلام العقود: {e}")
        return []

    if not contracts:
        return []

    candidate_symbols = [c.get("symbol") for c in contracts[:15] if c.get("symbol")]
    snapshots = fetch_option_snapshots_specific(candidate_symbols)

    valid_contracts = []
    for c in contracts:
        sym = c.get("symbol")
        strike = num(c.get("strike_price"))
        exp_str = c.get("expiration_date")
        if not sym or not math.isfinite(strike) or not exp_str:
            continue

        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (exp_date - today).days

        if dte < MIN_DTE or dte > MAX_DTE:
            continue

        snap = snapshots.get(sym, {})
        greeks = snap.get("greeks") or {}
        delta = abs(num(greeks.get("delta"), 0.0))
        iv = num(snap.get("impliedVolatility"), num(c.get("implied_volatility"), 0.0))

        if delta > 0 and (delta < MIN_DELTA or delta > MAX_DELTA):
            continue

        if iv > MAX_IV_THRESHOLD:
            log.info(f"🛑 [تخطي العقد {sym}] IV مرتفع جداً ({iv*100:.1f}%) معرض لـ IV Crush")
            continue

        dist = abs(strike - spy_price)
        valid_contracts.append((dist, dte, c, snap))

    valid_contracts.sort(key=lambda x: (x[0], x[1]))
    return [(item[2], item[3]) for item in valid_contracts[:3]]


# ================= 3. الفلترة الصارمة =================

def ultra_strict_filter(pred, option_quote, contract_info):
    bid, ask = option_quote["bid"], option_quote["ask"]
    spread_pct = (ask - bid) / option_quote["mid"]
    if spread_pct > MAX_SPREAD_PCT:
        log.info(f"🛑 [مرفوض] الـ Spread واسع جداً ({spread_pct*100:.2f}% > الحد {MAX_SPREAD_PCT*100:.1f}%)")
        return False

    open_interest = num(contract_info.get("open_interest"), 0)
    if open_interest < MIN_OPEN_INTEREST:
        log.info(f"🛑 [مرفوض] العقود المفتوحة ضئيلة ({open_interest} < {MIN_OPEN_INTEREST})")
        return False

    rsi_val = pred.get("rsi", 0.5)
    vwap_z = pred.get("vwap_zscore", 0.0)

    if pred["signal"] == "صعود 🚀":
        if rsi_val > 0.72:
            log.info(f"🛑 [مرفوض] تشبع شرائي (RSI: {rsi_val*100:.1f}%)")
            return False
        if vwap_z > 2.0:
            log.info(f"🛑 [مرفوض] السعر مبتعد فوق الـ VWAP (Z-Score: {vwap_z:.2f})")
            return False
    elif pred["signal"] == "نزول 📉":
        if rsi_val < 0.28:
            log.info(f"🛑 [مرفوض] تشبع بيعي (RSI: {rsi_val*100:.1f}%)")
            return False
        if vwap_z < -2.0:
            log.info(f"🛑 [مرفوض] السعر مبتعد تحت الـ VWAP (Z-Score: {vwap_z:.2f})")
            return False

    return True


# ================= 4. محرك التعلم الآلي =================

FEATURES = [
    "ret1", "ret3", "ret6", "ret12",
    "ema_9_21", "ema_21_50", "trend_200",
    "vwap_zscore", "rsi", "rsi_slope",
    "macd_hist", "vol_surge", "vol_velocity",
    "sweep_signal", "absorption", "realized_vol",
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

    day = pd.Series(f.index.tz_convert(NY).date, index=f.index)
    cum_pv = (c * v).groupby(day).cumsum()
    cum_v = v.groupby(day).cumsum().replace(0, np.nan)
    vwap = cum_pv / cum_v
    f["vwap_zscore"] = (c - vwap) / (c.rolling(15).std() + 1e-6)

    f["realized_vol"] = f["ret1"].rolling(20).std() * np.sqrt(390)

    recent_vol = v.rolling(3).mean()
    prior_vol = v.shift(3).rolling(12).mean().replace(0, np.nan)
    f["vol_velocity"] = (recent_vol / prior_vol).fillna(1.0)
    f["vol_surge"] = v / v.rolling(20).mean().replace(0, np.nan)

    rng = h - l
    f["absorption"] = (
        (rng < rng.rolling(10).mean() * 0.65) & (v > v.rolling(10).mean() * 1.5)
    ).astype(int)

    prev_high = h.shift(1).rolling(20).max()
    prev_low = l.shift(1).rolling(20).min()
    avg_vol = v.shift(1).rolling(14).mean()
    big_vol = v > avg_vol * 1.35
    sweep_up = (l < prev_low) & (c > prev_low) & big_vol
    sweep_down = (h > prev_high) & (c < prev_high) & big_vol
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


class AdvancedInstitutionalEngine:
    def __init__(self):
        self.primary_model = None
        self.meta_model = None
        self.is_trained = False
        self.last_trained_time = None
        self.last_attempt = 0.0

    def needs_training(self):
        if time.time() - self.last_attempt < 300:
            return False
        if not self.is_trained:
            return True
        return self.last_trained_time.date() != now_ny().date()

    def train_model(self):
        self.last_attempt = time.time()
        log.info(f"🧠 جاري تدريب المحرك على نافذة {HISTORY_DAYS} يوماً...")
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

        day = pd.Series(spy_bars.index.tz_convert(NY).date, index=spy_bars.index)
        same_day = day.shift(-HORIZON) == day

        target = pd.Series(0, index=f.index)
        target[future_ret > 0.0015] = 1
        target[future_ret < -0.0015] = -1
        target[~same_day] = 0
        f["primary_target"] = target

        ds = f[FEATURES + ["primary_target"]].dropna()
        ds_valid = ds[ds["primary_target"] != 0].copy()

        if len(ds_valid) < MIN_ROWS or ds_valid["primary_target"].nunique() < 2:
            log.warning("⚠️ عينات البيانات غير كافية للتدريب.")
            return False

        split = int(len(ds_valid) * 0.75)
        train_df = ds_valid.iloc[:split]
        hold_df = ds_valid.iloc[split:].copy()

        if HAS_LGB:
            clf = lgb.LGBMClassifier(n_estimators=250, learning_rate=0.02, max_depth=5,
                                     subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1)
        else:
            clf = GradientBoostingClassifier(n_estimators=200, learning_rate=0.03, max_depth=4, random_state=42)

        primary = make_pipeline(SimpleImputer(strategy="median"), clf)
        primary.fit(train_df[FEATURES], train_df["primary_target"])

        meta = None
        if len(hold_df) >= 100:
            hold_df["pred"] = primary.predict(hold_df[FEATURES])
            hold_df["meta_target"] = (hold_df["pred"] == hold_df["primary_target"]).astype(int)
            if hold_df["meta_target"].nunique() >= 2:
                meta = make_pipeline(
                    SimpleImputer(strategy="median"),
                    ExtraTreesClassifier(n_estimators=150, max_depth=5, min_samples_split=5, random_state=42),
                )
                meta.fit(hold_df[FEATURES], hold_df["meta_target"])

        self.primary_model = primary
        self.meta_model = meta
        self.is_trained = True
        self.last_trained_time = now_ny()
        log.info(f"✅ تم التدريب بنجاح | عينات: {len(ds_valid)} | Meta-Model: {'مفعل' if meta else 'غير متوفر'}")
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
        rsi_val = float(f["rsi"].iloc[-1])
        vwap_z = float(f["vwap_zscore"].iloc[-1])
        real_vol = float(f["realized_vol"].iloc[-1])

        confidence = (prob_max * 0.4) + (meta_prob * 0.6)
        signal = "محايد ⏳"

        if (primary_pred == 1 or sweep_sig == 1) and confidence >= MIN_CONFIDENCE_THRESHOLD:
            if vol_vel >= MIN_VOL_VELOCITY:
                signal = "صعود 🚀"
        elif (primary_pred == -1 or sweep_sig == -1) and confidence >= MIN_CONFIDENCE_THRESHOLD:
            if vol_vel >= MIN_VOL_VELOCITY:
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
            "rsi": rsi_val,
            "vwap_zscore": vwap_z,
            "realized_vol": real_vol,
        }


model_engine = AdvancedInstitutionalEngine()


# ================= 5. إدارة التنفيذ والصفقات =================

def execute_exit_order(symbol, qty, quotes_or_bid, is_emergency=False):
    if qty <= 0:
        return None

    bid = quotes_or_bid.get("bid", 0.01) if isinstance(quotes_or_bid, dict) else float(quotes_or_bid or 0.01)
    if bid <= 0:
        bid = 0.01

    target_price = round(bid, 2)
    try:
        order = alpaca_post(f"{TRADING_URL}/v2/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "limit",
            "limit_price": str(target_price),
            "time_in_force": "day",
        })
        oid = order.get("id")
        log.info(f"📤 [خروج 1/3] {symbol} | كمية {qty} | Limit على Bid: ${target_price:.2f}")

        for _ in range(2):
            time.sleep(1)
            chk = alpaca_get(f"{TRADING_URL}/v2/orders/{oid}")
            if chk.get("status") == "filled":
                log.info(f"✅ تم الخروج بنجاح بسعر ${target_price:.2f}")
                return chk

        alpaca_delete(f"{TRADING_URL}/v2/orders/{oid}")
        time.sleep(0.3)

        aggressive_bid = max(0.01, round(bid - 0.02, 2))
        second_order = alpaca_post(f"{TRADING_URL}/v2/orders", {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "limit",
            "limit_price": str(aggressive_bid),
            "time_in_force": "day",
        })
        second_oid = second_order.get("id")

        for _ in range(2):
            time.sleep(1)
            chk = alpaca_get(f"{TRADING_URL}/v2/orders/{second_oid}")
            if chk.get("status") == "filled":
                log.info(f"✅ تم الخروج المباشر بسعر ${aggressive_bid:.2f}")
                return chk

        alpaca_delete(f"{TRADING_URL}/v2/orders/{second_oid}")
        time.sleep(0.2)

        if is_emergency:
            log.warning(f"🚨 [خروج 3/3 طوارئ] إرسال أمر Market مباشر لـ {symbol}")
            return alpaca_post(f"{TRADING_URL}/v2/orders", {
                "symbol": symbol,
                "qty": str(qty),
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
            })

        return second_order

    except Exception as e:
        log.error(f"❌ فشل حرج في أمر الخروج لـ {symbol}: {e}")
        return None


def manage_active_positions():
    saved_positions = read_json(ACTIVE_POSITIONS_FILE)
    try:
        r = HTTP.get(f"{TRADING_URL}/v2/positions", headers=_headers(), timeout=10)
        positions_payload = r.json() if r.status_code == 200 else []
        active_alpaca_symbols = set()

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

                if entry_price <= 0 or current_price <= 0:
                    continue

                q = _get_option_quote(sym)
                live_bid = q["bid"] if q else current_price
                live_mid = q["mid"] if q else current_price

                pnl_pct = (live_bid - entry_price) / entry_price
                pos_data = saved_positions.get(sym, {})
                
                entry_time_str = pos_data.get("time", stamp())
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str)
                    minutes_in_trade = (datetime.now(UTC) - entry_dt).total_seconds() / 60.0
                except Exception:
                    minutes_in_trade = 0.0

                highest_price = max(pos_data.get("highest_price", live_mid), live_mid)
                peak_pnl_pct = (highest_price - entry_price) / entry_price

                saved_positions[sym] = {
                    "symbol": sym,
                    "entry_price": entry_price,
                    "highest_price": highest_price,
                    "qty": qty,
                    "time": entry_time_str,
                }

                should_close = False
                is_emergency = False
                reason = ""

                if force_eod_close:
                    should_close = True
                    is_emergency = True
                    reason = "إغلاق نهاية اليوم (EOD Exit)"

                elif pnl_pct >= TAKE_PROFIT_PCT:
                    should_close = True
                    reason = f"🎯 هدف الربح الثابت الرئيسي (+{pnl_pct*100:.1f}%)"

                elif peak_pnl_pct >= 1.00 and pnl_pct <= (peak_pnl_pct * 0.85):
                    should_close = True
                    reason = f"🚀 حماية أرباح خرافية (قمة: +{peak_pnl_pct*100:.1f}%) | الربح المضمون: +{pnl_pct*100:.1f}%"

                elif peak_pnl_pct >= 0.50 and pnl_pct <= (peak_pnl_pct * 0.75):
                    should_close = True
                    reason = f"💎 حماية أرباح عالية (قمة: +{peak_pnl_pct*100:.1f}%) | الربح المضمون: +{pnl_pct*100:.1f}%"

                elif peak_pnl_pct >= 0.25 and pnl_pct <= (peak_pnl_pct * 0.65):
                    should_close = True
                    reason = f"📈 حماية أرباح ممتازة (قمة: +{peak_pnl_pct*100:.1f}%) | الربح المضمون: +{pnl_pct*100:.1f}%"

                elif peak_pnl_pct >= 0.15 and pnl_pct <= (peak_pnl_pct * 0.50):
                    should_close = True
                    reason = f"⚖️ حماية أرباح متوسطة (قمة: +{peak_pnl_pct*100:.1f}%) | الربح المضمون: +{pnl_pct*100:.1f}%"

                elif peak_pnl_pct >= 0.08 and pnl_pct <= 0.01:
                    should_close = True
                    reason = f"🛡️ تأمين رأس المال بعد قمة +{peak_pnl_pct*100:.1f}% (الخروج بسعر التكلفة)"

                elif minutes_in_trade >= MAX_TIME_IN_TRADE_MINUTES and pnl_pct < 0.05:
                    should_close = True
                    is_emergency = False
                    reason = f"⏳ إغلاق زمني (Time Stop): مضى {minutes_in_trade:.0f} دقيقة دون حركة حاسمة"

                elif pnl_pct <= -STOP_LOSS_PCT:
                    should_close = True
                    is_emergency = True
                    reason = f"🛑 وقف خسارة صارم (-{STOP_LOSS_PCT*100:.1f}%) لـ {sym}"

                if should_close:
                    log.info(f"📉 [إغلاق صفقة] {sym} | السبب: {reason} | PnL النهائي: {pnl_pct*100:+.2f}%")
                    execute_exit_order(sym, qty, quotes_or_bid=q if q else live_bid, is_emergency=is_emergency)
                    log_completed_trade(sym, entry_price, live_bid, qty, pnl_pct, reason)

        to_remove = [s for s in saved_positions if s not in active_alpaca_symbols]
        for s in to_remove:
            saved_positions.pop(s, None)

        save_json(ACTIVE_POSITIONS_FILE, saved_positions)
    except Exception as e:
        log.error(f"⚠️ خطأ أثناء إدارة الصفقات: {e}")


def open_new_trade():
    positions = read_json(ACTIVE_POSITIONS_FILE)
    open_orders = get_open_orders()

    if len(positions) >= 1 or len(open_orders) >= 1:
        return

    if time.time() - STATE["last_scan"] < SCAN_SECONDS:
        return
    STATE["last_scan"] = time.time()

    if not model_engine.is_trained:
        return

    try:
        spy_bars = fetch_bars(HYPER_ASSET, days=LIVE_DAYS)
        if spy_bars.empty or len(spy_bars) < 60:
            return
        qqq_bars = fetch_bars(NASDAQ_ASSET, days=LIVE_DAYS)
        pred = model_engine.predict_fast(spy_bars, qqq_bars)
    except Exception as e:
        log.error(f"⚠️ خطأ أثناء التحليل: {e}")
        return

    log_every(
        "heartbeat", 60,
        f"💓 [Safaa Engine] SPY ${pred['current_price']:.2f} | الإشارة: {pred['signal']} | "
        f"الثقة: {pred['confidence']*100:.1f}% | RSI: {pred['rsi']*100:.1f}%"
    )

    if pred["signal"] == "محايد ⏳":
        return

    contracts_with_snap = fetch_spy_contracts_with_greeks(pred["signal"], pred["current_price"])
    if not contracts_with_snap:
        log.info("ℹ️ لا توجد عقود تلبّي شروط الـ Greeks والـ DTE.")
        return

    for c, snap in contracts_with_snap:
        sym = c.get("symbol")
        if not sym or sym in positions:
            continue
        q = _get_option_quote(sym)
        if not q:
            continue

        spread_pct = (q["ask"] - q["bid"]) / q["mid"]
        if spread_pct > MAX_SPREAD_PCT:
            log.info(f"🛑 [تخطي {sym}] الـ Spread اللحظي واسع ({spread_pct*100:.2f}% > {MAX_SPREAD_PCT*100:.1f}%)")
            continue

        if not ultra_strict_filter(pred, q, c):
            continue

        trade_qty = calculate_half_kelly_qty(q["mid"], pred["confidence"])
        if trade_qty <= 0:
            continue

        cancel_all_open_orders()
        order = execute_smart_limit_order(sym, qty=trade_qty, side="buy", quotes=q)
        if order:
            positions[sym] = {
                "symbol": sym,
                "entry_price": q["mid"],
                "highest_price": q["mid"],
                "qty": trade_qty,
                "time": stamp(),
            }
            save_json(ACTIVE_POSITIONS_FILE, positions)
            log.info(f"🚀 [صفقة آمنة جديدة] {sym} | كمية: {trade_qty} | بسعر ${q['mid']:.2f}")
            return


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
    for _ in range(12):
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


def _get_option_quote(symbol):
    try:
        payload = alpaca_get(f"{DATA_URL}/v1beta1/options/quotes/latest",
                             {"symbols": symbol, "feed": OPTION_FEED})
    except Exception:
        return None
    q = (payload.get("quotes") or {}).get(symbol, {})
    bid, ask = num(q.get("bp")), num(q.get("ap"))
    if not math.isfinite(bid) or bid <= 0 or not math.isfinite(ask) or ask < bid:
        return None
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}


def calculate_half_kelly_qty(option_mid_price, win_probability):
    try:
        acc = alpaca_get(f"{TRADING_URL}/v2/account")
        equity = num(acc.get("equity"), 1000.0)

        p = max(0.51, min(win_probability, 0.85))
        q = 1.0 - p
        b = 1.8
        kelly_f = (p * b - q) / b
        half_kelly = max(0.01, min(kelly_f * 0.5, MAX_RISK_CAP))

        trade_budget = equity * half_kelly
        contract_cost = option_mid_price * 100

        if contract_cost <= 0 or contract_cost > (equity * MAX_RISK_CAP):
            return 0

        qty = math.floor(trade_budget / contract_cost)
        return max(1 if contract_cost <= trade_budget * 1.2 else 0, min(qty, 30))
    except Exception as e:
        log.error(f"⚠️ خطأ في حساب حجم الصفقة: {e}")
        return 0


def execute_smart_limit_order(symbol, qty, side, quotes):
    if qty <= 0 or not quotes:
        return None
    mid_price = quotes["mid"]
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
                log.info(f"⚡ تم التنفيذ عند سعر المنتصف ${mid_price:.2f}")
                return chk

        alpaca_delete(f"{TRADING_URL}/v2/orders/{order_id}")
        return None
    except Exception as e:
        log.error(f"❌ خطأ أثناء إرسال الأمر: {e}")
        return None


# ================= 6. الحلقة الرئيسية =================

if __name__ == "__main__":
    log.info(f"🚀 تشغيل {NAME} {VERSION}")
    calculate_and_log_metrics()

    while True:
        try:
            if (is_premarket() or market_open()) and model_engine.needs_training():
                model_engine.train_model()

            if market_open():
                manage_active_positions()
                if in_signal_window() and not is_eod_close_time():
                    open_new_trade()
            else:
                log_every("closed", 300, "⏳ السوق مغلق حالياً.. المحرك في وضع الانتظار.")

            log_every("metrics", 3600, "📊 تحديث محفظة الأداء وسجل الصفقات:")
            if time.time() - _LAST_LOG.get("metrics", 0.0) < 5:
                calculate_and_log_metrics()

            time.sleep(POSITION_POLL_SECONDS if read_json(ACTIVE_POSITIONS_FILE) else POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("تم إيقاف المحرك يدوياً.")
            break
        except Exception as e:
            log.exception(f"⚠️ خطأ في حلقة التشغيل: {e}")
            time.sleep(15)
