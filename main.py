# ============================================================
# صَفاء التحليل v33.1-SPY-UltraTitan | Super-Quant Engine
# الميزات: تداول حصري لـ SPY + حماية الأرباح المفتوحة (تجاوز الدبل)
# تأمين أرباح +10% + دمج زخم ناسداك (QQQ) عبر Alpaca + ذكاء كمي معزز
# ============================================================
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

# -------------------- CONFIGURATION --------------------
NAME, VERSION = "صَفاء التحليل", "33.1-SPY-UltraTitan"
NY, UTC = ZoneInfo("America/New_York"), timezone.utc
TF = "5Min"

HYPER_ASSET = "SPY"
NASDAQ_ASSET = "QQQ"     # استخدام QQQ كمؤشر مرجعي لقطاع التكنولوجيا وناسداك

DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
KEY = (os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or "").strip()
SECRET = (os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY") or "").strip()
STOCK_FEED = os.getenv("ALPACA_DATA_FEED", "iex").lower()
OPTION_FEED = os.getenv("OPTIONS_FEED", "indicative").lower()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HISTORY_DAYS = 45         
MIN_ROWS = 250
HORIZON = 5

MAX_SPREAD_PCT = 0.045       # أقصى فارق بين العرض والطلب 4.5%
POLL_SECONDS = 10            # فحص السوق كل 10 ثوانٍ

MAX_DAILY_LOSS_PCT = 0.02    # كاسر الحماية اليومي (-2% من المحفظة)
MAX_RISK_CAP = 0.065         # أقصى مخاطرة لكل صفقة (6.5% من إجمالي المحفظة المركبة)

ROOT = Path(os.getenv("DATA_DIR", "."))
ROOT.mkdir(parents=True, exist_ok=True)
ACTIVE_POSITIONS_FILE = ROOT / "spy_ultratitan_positions.json"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("safaa_spy_ultratitan")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": f"SafaaUltraTitan/{VERSION}"})

# -------------------- HELPERS & API --------------------
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

def send_telegram(msg):
    """إرسال إشعارات مباشرة إلى تلجرام مع توقيع البوت"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        signature = "\n\n—\n✨ <b>تصميم أبو سحاب</b> ✨"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg + signature, "parse_mode": "HTML"}
        HTTP.post(url, json=payload, timeout=5)
    except Exception as e:
        log.error(f"⚠️ فشل إرسال إشعار تلجرام: {e}")

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
        log.exception(f"Failed to save {filepath}")

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
                time.sleep(0.8 * (attempt + 1))
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
                time.sleep(0.8 * (attempt + 1))
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

def in_signal_window():
    n = now_ny()
    return n.weekday() < 5 and (n.hour, n.minute) >= (9, 45) and (n.hour, n.minute) < (15, 30)

def is_eod_close_time():
    n = now_ny()
    return n.weekday() < 5 and (n.hour, n.minute) >= (15, 50)

# -------------------- DATA FETCHING (SPY + NASDAQ BAR ALIGNMENT) --------------------
def fetch_bars(symbol=HYPER_ASSET, days=HISTORY_DAYS):
    end = datetime.now(UTC)
    params = {
        "timeframe": TF,
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
    for _ in range(15):
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

def get_nasdaq_regime_info(spy_bars, qqq_bars):
    """تحليل الحركة الاستباقية والقوة النسبية لمؤشر ناسداك (QQQ)"""
    if spy_bars.empty or qqq_bars.empty:
        return {"nq_ret1": 0.0, "nq_rel_strength": 0.0, "nasdaq_regime": "NEUTRAL"}
    
    spy_ret1 = float(spy_bars["close"].pct_change().iloc[-1])
    qqq_ret1 = float(qqq_bars["close"].pct_change().iloc[-1])
    nq_rel_strength = qqq_ret1 - spy_ret1
    
    regime = "NEUTRAL"
    if qqq_ret1 > 0.0012 and nq_rel_strength > 0.0003:
        regime = "BULLISH_SURGE"     # اندفاع صاعد قوي في القطاع التكنولوجي وناسداك
    elif qqq_ret1 < -0.0012 and nq_rel_strength < -0.0003:
        regime = "BEARISH_SURGE"    # هبوط حاد وضغط بيعي في ناسداك
        
    return {
        "nq_ret1": qqq_ret1,
        "nq_rel_strength": nq_rel_strength,
        "nasdaq_regime": regime
    }

# -------------------- ADVANCED QUANT FEATURES & MODELS --------------------
FEATURES = [
    "ret1", "ret3", "ret6", "ret12", 
    "ema_9_21", "ema_21_50", "trend_200", 
    "vwap_zscore", "vwap_band_pos", "rsi", "rsi_slope",
    "macd_hist", "bollinger_pos", "atr_norm", "vol_surge", "regime_efficiency",
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
    
    # VWAP & Bands
    f['trade_date'] = f.index.date
    f['pv'] = c * v
    cum_pv = f.groupby('trade_date')['pv'].cumsum()
    cum_vol = f.groupby('trade_date')['volume'].cumsum()
    vwap = cum_pv / cum_vol.replace(0, np.nan)
    
    f['sq_diff'] = ((c - vwap) ** 2) * v
    cum_sq_diff = f.groupby('trade_date')['sq_diff'].cumsum()
    vwap_std = np.sqrt(cum_sq_diff / cum_vol.replace(0, np.nan))
    
    f["vwap_zscore"] = ((c - vwap) / vwap_std.replace(0, np.nan)).fillna(0)
    f["vwap_band_pos"] = (c - vwap) / c
    
    # Bollinger Bands Position
    ma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    f["bollinger_pos"] = ((c - (ma20 - 2 * std20)) / (4 * std20.replace(0, np.nan))).fillna(0.5)
    
    # MACD Histogram
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    f["macd_hist"] = (macd - signal) / c

    # Kaufman Efficiency Ratio
    change = (c - c.shift(10)).abs()
    volatility = c.diff().abs().rolling(10).sum()
    f["regime_efficiency"] = (change / volatility.replace(0, np.nan)).fillna(0)
    
    # ATR & Volume
    prev = c.shift()
    tr = pd.concat([f["high"] - f["low"], (f["high"] - prev).abs(), (f["low"] - prev).abs()], axis=1).max(axis=1)
    f["atr_norm"] = tr.ewm(alpha=1/14, adjust=False).mean() / c
    f["vol_surge"] = v / v.rolling(30).mean().replace(0, np.nan)
    
    # RSI & Momentum Slope
    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = (100 - 100 / (1 + rs)) / 100
    f["rsi"] = rsi.fillna(0.5)
    f["rsi_slope"] = rsi.diff(3).fillna(0)
    
    # دمج مؤشر ناسداك المحاذي زمنياً
    if qqq_bars is not None and not qqq_bars.empty:
        qqq_c = qqq_bars["close"].reindex(f.index).ffill()
        f["nq_lead_ret"] = qqq_c.pct_change().fillna(0)
        f["nq_rel_strength"] = (f["nq_lead_ret"] - f["ret1"]).fillna(0)
    else:
        f["nq_lead_ret"] = 0.0
        f["nq_rel_strength"] = 0.0
    
    f = f.drop(columns=['trade_date', 'pv', 'sq_diff'], errors='ignore')
    return f.replace([np.inf, -np.inf], np.nan)

def train_institutional_quant_model(spy_bars, qqq_bars):
    if spy_bars.empty or len(spy_bars) < MIN_ROWS:
        return {"ok": False}
        
    f = generate_institutional_features(spy_bars, qqq_bars)
    future_ret = (spy_bars["close"].shift(-HORIZON) - spy_bars["close"]) / spy_bars["close"]
    
    f["primary_target"] = np.where(future_ret > 0.0014, 1, (np.where(future_ret < -0.0014, -1, 0)))
    
    ds = f[FEATURES + ["primary_target"]].dropna()
    ds_valid = ds[ds["primary_target"] != 0].copy()
    
    if len(ds_valid) < MIN_ROWS or ds_valid["primary_target"].nunique() < 2:
        return {"ok": False}
        
    # اختيار النموذج الرياضي المتكيف
    if HAS_LGB:
        clf = lgb.LGBMClassifier(n_estimators=150, learning_rate=0.035, max_depth=4, random_state=42, verbose=-1)
    else:
        clf = GradientBoostingClassifier(n_estimators=150, learning_rate=0.035, max_depth=4, random_state=42)

    primary_pipeline = make_pipeline(SimpleImputer(strategy="median"), clf)
    primary_pipeline.fit(ds_valid[FEATURES], ds_valid["primary_target"])
    
    latest_feat = f[FEATURES].iloc[[-1]]
    primary_pred = primary_pipeline.predict(latest_feat)[0]
    probs = primary_pipeline.predict_proba(latest_feat)[0]
    
    classes = list(primary_pipeline.named_steps[clf.__class__.__name__.lower()].classes_)
    prob_max = probs[classes.index(primary_pred)] if primary_pred in classes else 0.5

    # Meta-Labeling Model (فلترة الإشارات الزائفة باستعمال Random Forest)
    ds_valid["pred_signal"] = primary_pipeline.predict(ds_valid[FEATURES])
    ds_valid["meta_target"] = np.where((ds_valid["pred_signal"] == ds_valid["primary_target"]), 1, 0)
    
    meta_prob = 0.50
    if ds_valid["meta_target"].nunique() >= 2:
        meta_pipeline = make_pipeline(
            SimpleImputer(strategy="median"),
            RandomForestClassifier(n_estimators=100, max_depth=4, random_state=42)
        )
        meta_pipeline.fit(ds_valid[FEATURES], ds_valid["meta_target"])
        meta_classes = list(meta_pipeline.named_steps["randomforestclassifier"].classes_)
        if 1 in meta_classes:
            idx1 = meta_classes.index(1)
            meta_prob = float(meta_pipeline.predict_proba(latest_feat)[0, idx1])

    curr_price = float(spy_bars["close"].iloc[-1])
    zscore = float(f["vwap_zscore"].iloc[-1])
    trend_200 = float(f["trend_200"].iloc[-1])
    
    nasdaq_info = get_nasdaq_regime_info(spy_bars, qqq_bars)
    nasdaq_regime = nasdaq_info.get("nasdaq_regime", "NEUTRAL")
    
    signal = "محايد ⏳"
    
    # تأكيد الذكاء الاصطناعي مع زخم مؤشر ناسداك
    if (primary_pred == 1 or nasdaq_regime == "BULLISH_SURGE") and prob_max >= 0.54 and meta_prob >= 0.51 and trend_200 > -0.003:
        if -1.8 <= zscore <= 2.2 and nasdaq_regime != "BEARISH_SURGE":
            signal = "صعود 🚀"
            
    elif (primary_pred == -1 or nasdaq_regime == "BEARISH_SURGE") and prob_max >= 0.54 and meta_prob >= 0.51 and trend_200 < 0.003:
        if -2.2 <= zscore <= 1.8 and nasdaq_regime != "BULLISH_SURGE":
            signal = "نزول 📉"

    return {
        "ok": True,
        "signal": signal,
        "primary_prob": prob_max,
        "meta_prob": meta_prob,
        "current_price": curr_price,
        "nasdaq_regime": nasdaq_regime
    }

# -------------------- COMPOUNDING CAPITAL ALLOCATION --------------------
def calculate_half_kelly_qty(option_mid_price, win_probability):
    """حساب أحجام العقود بنسبة الفائدة المركبة الذكية"""
    try:
        acc = alpaca_get(f"{TRADING_URL}/v2/account")
        equity = num(acc.get("equity"), 1000.0)

        p = max(0.51, min(win_probability, 0.85))
        q = 1.0 - p
        b = 2.0  
        
        kelly_f = (p * b - q) / b
        half_kelly = max(0.015, min(kelly_f * 0.5, MAX_RISK_CAP))
        
        trade_budget = equity * half_kelly
        contract_cost = option_mid_price * 100

        if contract_cost <= 0:
            return 0

        if contract_cost > (equity * (MAX_RISK_CAP * 1.5)):
            log.warning(f"⚠️ تكلفة العقد (${contract_cost:.2f}) تتجاوز ميزانية المحفظة المتاحة (${trade_budget:.2f}).")
            return 0

        qty = math.floor(trade_budget / contract_cost)
        final_qty = max(1 if contract_cost <= trade_budget * 1.5 else 0, min(qty, 50))
        
        log.info(f"🧠 [Half-Kelly Compounding] السيولة الحالية: ${equity:,.2f} | نسبة المخاطرة: {half_kelly*100:.2f}% | الكمية: {final_qty} عقود")
        return final_qty
    except Exception as e:
        log.error(f"⚠️ خطأ في حساب كمية التداول المركبة: {e}")
        return 0

# -------------------- ORDERS & UNLIMITED PROFIT TRAILING STOP --------------------
def execute_limit_order(symbol, qty=1, side="buy", limit_price=None):
    if qty <= 0:
        return None
    try:
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "limit",
            "limit_price": str(round(limit_price, 2)),
            "time_in_force": "day"
        }
        res = alpaca_post(f"{TRADING_URL}/v2/orders", payload)
        log.info(f"✅ تنفيذ أمر ({side.upper()}) بسعر ${limit_price:.2f} | العقد: {symbol} | كمية: {qty}")
        return res
    except Exception as e:
        log.error(f"❌ فشل تنفيذ الأمر لـ {symbol}: {e}")
        return None

def fetch_spy_contracts(direction, spy_price):
    """جلب عقود SPY القريبة من السعر الحالي (ATM) بأعلى سيولة"""
    today = now_ny().date()
    target_exp_end = (now_ny() + timedelta(days=3)).date()
    params = {
        "underlying_symbols": HYPER_ASSET,
        "status": "active",
        "type": "call" if direction == "صعود 🚀" else "put",
        "expiration_date_gte": today.isoformat(),
        "expiration_date_lte": target_exp_end.isoformat(),
        "limit": 25,
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
                execute_limit_order(sym, qty=abs(int(qty)), side="sell", limit_price=price)
        save_json(ACTIVE_POSITIONS_FILE, {})
        send_telegram("🛑 <b>تنبيه نظام:</b> تم إغلاق جميع صفقات SPY فوراً عبر كاسر الحماية.")
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
    """
    نظام حماية الأرباح المتدرج المفتوح:
    - لا يتم إغلاق العقد عند تحقيق الدبل (+100%)، بل يترك ليمتد مع حماية 88% من أعلى قمة وصل إليها.
    - جدار حماية +10%: يضمن أن الصفقة لا تنزل عن +10% ربح صافي بمجرد تجاوز أرباحها الـ 10%.
    """
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

                log.info(f"📊 [متابعة SPY] {sym} | الدخول: ${entry_price:.2f} | الحالي: ${current_price:.2f} | القمة: +{peak_pnl_pct*100:.1f}% | صافي الربح الحالي: {pnl_pct*100:+.2f}%")

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

                # 0. إغلاق إجباري قبل إغلاق السوق
                if force_eod_close:
                    should_close = True
                    reason = "🌆 إغلاق نهاية اليوم (EOD Exit)"

                # 1. أرباح استثنائية دبل وما فوق (+100%+): ترك الصفقة مفتوحة وحماية 88% من أعلى قمة (Trailing Shield)
                elif peak_pnl_pct >= 1.00 and pnl_pct <= (peak_pnl_pct * 0.88):
                    should_close = True
                    reason = f"🚀 حماية أرباح انفجارية دبل+ (قمة: +{peak_pnl_pct*100:.1f}%) | الربح المحقق: +{pnl_pct*100:.2f}%"

                # 2. أرباح مرتفعة (+50% إلى +100%): تأمين 78% من أقصى قمة أرباح
                elif peak_pnl_pct >= 0.50 and pnl_pct <= (peak_pnl_pct * 0.78):
                    should_close = True
                    reason = f"🔥 حماية أرباح عالية (قمة: +{peak_pnl_pct*100:.1f}%) | الربح المحقق: +{pnl_pct*100:.2f}%"

                # 3. أرباح متوسطة (+25% إلى +50%): تأمين 68% من قمة الأرباح
                elif peak_pnl_pct >= 0.25 and pnl_pct <= (peak_pnl_pct * 0.68):
                    should_close = True
                    reason = f"🏆 حماية أرباح ممتازة | الربح المحقق: +{pnl_pct*100:.2f}%"

                # 4. جدار حماية +10% الصارم: عند الوصول لـ +10% أو أكثر والتراجع تحت +10%
                elif peak_pnl_pct >= 0.10 and pnl_pct < 0.10:
                    should_close = True
                    reason = f"🛡️ جدار حماية أرباح +10% الصارم (تحقيق قمة +{peak_pnl_pct*100:.1f}%)"

                # 5. تفعيل Breakeven/أرباح مبكرة (+5% إلى +10% والتراجع لـ +2.5%)
                elif peak_pnl_pct >= 0.05 and pnl_pct <= 0.025:
                    should_close = True
                    reason = f"🔒 تأمين أرباح مبكرة (+2.5%+) بعد قمة +{peak_pnl_pct*100:.1f}%"

                # 6. وقف خسارة خاطف وصارم (-5.5%)
                elif pnl_pct <= -0.055:
                    should_close = True
                    reason = f"🛑 وقف خسارة خاطف (-5.5%) لـ {sym}"

                if should_close:
                    log.info(f"{reason} | العقد: {sym}")
                    execute_limit_order(sym, qty=qty, side="sell", limit_price=exit_price)
                    send_telegram(f"📉 <b>إغلاق صفقة SPY:</b> <code>{sym}</code>\nالسبب: {reason}\nصافي الربح/الخسارة: <b>{pnl_pct*100:+.2f}%</b>")

        # تنظيف الصفقات المغلقة دون مسح الأوامر المطلوبة التي تحضر في الانتظار (Pending Buy Orders)
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
    
    # حظر فتح صفقة جديدة إذا كانت هناك صفقة مفتوحة أو أمر شراء معلق
    if len(positions) >= 1 or len(open_orders) >= 1:
        return

    spy_bars = fetch_bars(HYPER_ASSET)
    if spy_bars.empty:
        return
        
    qqq_bars = fetch_bars(NASDAQ_ASSET)
    pred = train_institutional_quant_model(spy_bars, qqq_bars)
    
    if not pred.get("ok") or pred["signal"] == "محايد ⏳":
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

        win_prob = (pred["primary_prob"] + pred["meta_prob"]) / 2.0
        trade_qty = calculate_half_kelly_qty(q["mid"], win_prob)

        if trade_qty <= 0:
            continue

        cancel_all_open_orders()
        order = execute_limit_order(sym, qty=trade_qty, side="buy", limit_price=q["mid"])
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
            msg = (
                f"🚀 <b>فتح صفقة جديدة ({NAME}):</b>\n"
                f"الأصل: <b>SPY</b> | العقد: <code>{sym}</code>\n"
                f"الكمية المركبة: {trade_qty} | السعر: ${q['mid']:.2f}\n"
                f"الإشارة: {pred['signal']} (نسبة الثقة: {win_prob*100:.1f}%)\n"
                f"حركة ناسداك القيادية: <code>{pred['nasdaq_regime']}</code>"
            )
            log.info(msg)
            send_telegram(msg)
            return

# -------------------- MAIN LOOP --------------------
if __name__ == "__main__":
    log.info(f"🚀 بدء تشغيل {NAME} v{VERSION} (محرك تداول SPY والأرباح المفتوحة)")
    
    while True:
        try:
            if market_open():
                manage_active_positions()
                if in_signal_window() and not is_eod_close_time():
                    open_new_trade()
            else:
                log.info("⏳ السوق مغلق حالياً.. البوت في وضع الانتظار والترقب.")
                
            time.sleep(POLL_SECONDS)
            
        except KeyboardInterrupt:
            log.info("تم إيقاف البوت يدوياً.")
            break
        except Exception as e:
            log.exception(f"⚠️ خطأ غير متوقع في حلقة التشغيل: {e}")
            time.sleep(15)
