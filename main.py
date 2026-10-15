# ============================================================
# صَفاء التحليل v25.4-Points-Speed-MegaCaps | Live Trading Mode
# التنفيذ الآلي الحقيقي (Paper) • بدون تيليجرام • متابعة مستمرة للصفقات
# ============================================================
import os
import sys
import csv
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
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.metrics import roc_auc_score, brier_score_loss
import yfinance as yf

# -------------------- CONFIG --------------------
NAME, VERSION = "صَفاء التحليل", "25.4-Points-Speed-MegaCaps-Live"
NY, UTC = ZoneInfo("America/New_York"), timezone.utc
SPY, TF = "SPY", "5Min"
BAR_MINUTES = 5

TOP_LEADERS = {
    "AAPL": 0.070,
    "MSFT": 0.065,
    "NVDA": 0.060,
    "AMZN": 0.040,
    "GOOGL": 0.035,
    "META": 0.030,
    "TSLA": 0.020,
    "BRK-B": 0.020,
    "AVGO": 0.020,
    "LLY": 0.020,
    "JPM": 0.015
}

DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
KEY = (os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or "").strip()
SECRET = (os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY") or "").strip()
STOCK_FEED = os.getenv("ALPACA_DATA_FEED", "iex").lower()
OPTION_FEED = os.getenv("OPTIONS_FEED", "indicative").lower()

HISTORY_DAYS = 120         
MIN_ROWS = 300
HORIZON = 6

MIN_PROB = 0.52            
MIN_AUC = 0.47             
MIN_SIGNAL_ACCURACY = 0.48 
MAX_BRIER = 0.32           
MOVE_POINTS = 3.0          
SPIKE_THRESHOLD_POINTS = 5.0 

MAX_STRIKE_DISTANCE = 10.0  
MAX_SPREAD_PCT = 0.35      
COOLDOWN_MINUTES = 2       # تقليل وقت الانتظار للسماح بصفقات متكررة

POLL_SECONDS = 30
PAPER_TAKE_PROFIT_PCT = 0.30  # ربح مستهدف ابتدائي 30%
PAPER_STOP_LOSS_PCT = 0.25    # وقف خسارة ابتدائي 25%
TRAILING_LOCK_PCT = 0.20      # حماية الأرباح المتصاعدة (ترك مسافة 20% تراجع عن أعلى قمة)

ROOT = Path(os.getenv("DATA_DIR", "."))
ROOT.mkdir(parents=True, exist_ok=True)
STATE_FILE = ROOT / "safa_state_live_v254.json"
ACTIVE_POSITIONS_FILE = ROOT / "safa_active_positions.json"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("safaa_live")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": f"SafaaAnalysisLive/{VERSION}"})

# -------------------- GENERAL HELPERS --------------------
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
        log.exception(f"Failed to save {filepath}")

def parse_ts(value):
    try:
        t = pd.Timestamp(value)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return t.tz_convert("UTC")
    except Exception:
        return None

def alpaca_get(url, params=None):
    if not KEY or not SECRET:
        raise RuntimeError("APCA_API_KEY_ID / APCA_API_SECRET_KEY غير مضبوطين")
    last = None
    for attempt in range(3):
        try:
            r = HTTP.get(
                url,
                headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET},
                params=params,
                timeout=15,
            )
            if r.status_code in (401, 403):
                raise RuntimeError(f"Alpaca authentication error: {r.status_code}")
            r.raise_for_status()
            result = r.json()
            if not isinstance(result, dict):
                raise RuntimeError("Unexpected Alpaca response")
            return result
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Alpaca request failed: {last}")

def alpaca_post(url, payload=None):
    if not KEY or not SECRET:
        raise RuntimeError("APCA_API_KEY_ID / APCA_API_SECRET_KEY غير مضبوطين")
    last = None
    for attempt in range(3):
        try:
            r = HTTP.post(
                url,
                headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET},
                json=payload,
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Alpaca POST request failed: {last}")

def market_open():
    n = now_ny()
    return (
        n.weekday() < 5
        and (n.hour, n.minute) >= (9, 30)
        and (n.hour, n.minute) < (16, 0)
    )

def in_signal_window():
    n = now_ny()
    return (
        n.weekday() < 5
        and (n.hour, n.minute) >= (9, 40)
        and (n.hour, n.minute) < (15, 50)
    )

# -------------------- BROKER ORDER EXECUTION --------------------
def execute_order(symbol, qty=1, side="buy", order_type="market"):
    """تنفيذ أمر حقيقي في حساب Paper Trading"""
    try:
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": "day"
        }
        res = alpaca_post(f"{TRADING_URL}/v2/orders", payload)
        log.info(f"✅ تم تنفيذ أمر {side.upper()} بنجاح لعقد {symbol} - الكمية: {qty}")
        return res
    except Exception as e:
        log.error(f"❌ فشل تنفيذ الأمر لـ {symbol}: {e}")
        return None

# -------------------- STOCK & DATA --------------------
def fetch_bars(symbol=SPY, days=HISTORY_DAYS):
    end = datetime.now(UTC)
    params = {
        "timeframe": TF,
        "start": (end - timedelta(days=days)).isoformat(),
        "end": end.isoformat(),
        "limit": 10000,
        "adjustment": "raw",
        "feed": STOCK_FEED,
        "sort": "asc",
    }
    url = f"{DATA_URL}/v2/stocks/{symbol}/bars"
    all_bars = []
    token = None
    for _ in range(20):
        p = dict(params)
        if token:
            p["page_token"] = token
        data = alpaca_get(url, p)
        all_bars.extend(data.get("bars") or [])
        token = data.get("next_page_token")
        if not token:
            break
    if not all_bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(all_bars).rename(
        columns={"t": "timestamp", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["timestamp", "open", "high", "low", "close"]).drop_duplicates("timestamp", keep="last").sort_values("timestamp").set_index("timestamp")

FEATURES = ["ret1", "ret3", "ret6", "ret12", "ema_spread", "ema_trend", "rsi", "atr_pct", "range_pct", "body_pct", "close_location", "volume_ratio"]

def features_for(bars):
    f = bars.copy()
    c = f["close"]
    f["ret1"] = c.pct_change()
    f["ret3"] = c.pct_change(3)
    f["ret6"] = c.pct_change(6)
    f["ret12"] = c.pct_change(12)
    e9 = c.ewm(span=9, adjust=False, min_periods=9).mean()
    e20 = c.ewm(span=20, adjust=False, min_periods=20).mean()
    e50 = c.ewm(span=50, adjust=False, min_periods=50).mean()
    f["ema_spread"] = (e9 - e20) / c
    f["ema_trend"] = (e20 - e50) / c
    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    f["rsi"] = ((100 - 100 / (1 + rs)) / 100).fillna(0.5)
    prev = c.shift()
    tr = pd.concat([f["high"] - f["low"], (f["high"] - prev).abs(), (f["low"] - prev).abs()], axis=1).max(axis=1)
    f["atr_pct"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / c
    rng = (f["high"] - f["low"]).clip(lower=0)
    f["range_pct"] = rng / c
    f["body_pct"] = (f["close"] - f["open"]) / c
    f["close_location"] = ((c - f["low"]) / rng.replace(0, np.nan)).fillna(0.5)
    f["volume_ratio"] = f["volume"] / f["volume"].rolling(20, min_periods=20).mean().replace(0, np.nan)
    return f.replace([np.inf, -np.inf], np.nan)

def train_predict(bars):
    if bars.empty or len(bars) < MIN_ROWS:
        return {"ok": False}
    f = features_for(bars)
    future = bars["close"].shift(-HORIZON)
    f["target"] = np.where(future.notna(), (future > bars["close"]).astype(float), np.nan)
    ds = f[FEATURES + ["target"]].dropna()
    if len(ds) < MIN_ROWS or ds["target"].nunique() < 2:
        return {"ok": False}
    
    model = make_pipeline(SimpleImputer(strategy="median"), RandomForestClassifier(n_estimators=180, max_depth=7, min_samples_leaf=4, class_weight="balanced", random_state=42, n_jobs=-1))
    model.fit(ds[FEATURES], ds["target"].astype(int))
    prob_up = float(model.predict_proba(f[FEATURES].iloc[[-1]])[0, 1])
    
    return {
        "ok": True,
        "signal": "صعود 🚀" if prob_up >= 0.5 else "نزول 📉",
        "probability": max(prob_up, 1 - prob_up) * 100,
        "current_price": float(bars["close"].iloc[-1]),
    }

# -------------------- OPTIONS & ACTIVE MANAGEMENT --------------------
def fetch_contracts(symbol, direction, price):
    today = now_ny().date()
    params = {
        "underlying_symbols": symbol,
        "status": "active",
        "type": "call" if direction == "صعود 🚀" else "put",
        "expiration_date": today.isoformat(),
        "limit": 200,
    }
    payload = alpaca_get(f"{TRADING_URL}/v2/options/contracts", params)
    contracts = payload.get("option_contracts") or payload.get("contracts") or []
    candidates = []
    for c in contracts:
        strike = num(c.get("strike_price"))
        sym = c.get("symbol")
        if sym and math.isfinite(strike) and abs(strike - price) <= MAX_STRIKE_DISTANCE:
            candidates.append((abs(strike - price), c))
    candidates.sort(key=lambda x: x[0])
    return [c for _, c in candidates[:5]]

def _get_option_quote(symbol):
    payload = alpaca_get(f"{DATA_URL}/v1beta1/options/quotes/latest", {"symbols": symbol, "feed": OPTION_FEED})
    q = (payload.get("quotes") or {}).get(symbol, {})
    bid, ask = num(q.get("bp")), num(q.get("ap"))
    if not math.isfinite(bid) or bid <= 0 or not math.isfinite(ask) or ask < bid:
        return None
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}

def manage_active_positions():
    """متابعة الصفقات المفتوحة مع نظام تتبع القمة (Trailing High-Water Mark) وجني الأرباح التلقائي"""
    saved_positions = read_json(ACTIVE_POSITIONS_FILE)
    
    try:
        # سحب الصفقات الحقيقية المفتوحة من منصة Alpaca مباشرة لضمان المزامنة الدقيقة
        positions_payload = alpaca_get(f"{TRADING_URL}/v2/positions")
        active_alpaca_symbols = set()
        
        if isinstance(positions_payload, list):
            for pos in positions_payload:
                sym = pos.get("symbol")
                if sym:
                    active_alpaca_symbols.add(sym)
                    qty = num(pos.get("qty"))
                    entry_price = num(pos.get("avg_entry_price"))
                    current_price = num(pos.get("current_price"))
                    
                    if not math.isfinite(entry_price) or entry_price <= 0 or not math.isfinite(current_price) or current_price <= 0:
                        continue
                    
                    # استرجاع أو تهيئة أعلى سعر وصل إليه العقد لتحديث الهدف والوقف بشكل تصاعدي
                    pos_data = saved_positions.get(sym, {})
                    highest_price = max(pos_data.get("highest_price", current_price), current_price)
                    
                    pnl_pct = (current_price - entry_price) / entry_price
                    peak_pnl_pct = (highest_price - entry_price) / entry_price

                    log.info(f"📊 متابعة صفقة {sym} | الدخول: ${entry_price:.2f} | الحالي: ${current_price:.2f} | الأعلى: ${highest_price:.2f} | الربح الحالي: {pnl_pct*100:+.2f}%")

                    # تحديث البيانات المحفوظة للقمة الجديدة
                    saved_positions[sym] = {
                        "symbol": sym,
                        "entry_price": entry_price,
                        "highest_price": highest_price,
                        "qty": qty,
                        "time": pos_data.get("time", stamp())
                    }

                    # شرط 1: تحقيق الأهداف أو التراجع بنسبة 20% من القمة المحققة (Trailing Stop)
                    if pnl_pct >= PAPER_TAKE_PROFIT_PCT:
                        log.info(f"🎯 تم تحقيق الهدف الرئيسي على {sym}! جاري بيع الصفقة جني أرباح...")
                        execute_order(sym, qty=abs(int(qty)), side="sell" if qty > 0 else "buy", order_type="market")
                        saved_positions.pop(sym, None)
                    elif peak_pnl_pct >= 0.15 and pnl_pct <= (peak_pnl_pct - TRAILING_LOCK_PCT):
                        log.info(f"📈 تفعيل جني الأرباح المتحرك (Trailing) بعد الوصول لقمة ربح على {sym}! جاري الإغلاق...")
                        execute_order(sym, qty=abs(int(qty)), side="sell" if qty > 0 else "buy", order_type="market")
                        saved_positions.pop(sym, None)
                    # شرط 2: وقف الخسارة التقليدي (-25%)
                    elif pnl_pct <= -PAPER_STOP_LOSS_PCT:
                        log.info(f"🛑 بلوغ حد وقف الخسارة (-25%) على {sym}! جاري إغلاق الصفقة حفاظاً على رأس المال...")
                        execute_order(sym, qty=abs(int(qty)), side="sell" if qty > 0 else "buy", order_type="market")
                        saved_positions.pop(sym, None)

        # تنظيف الملف من الصفقات التي أُغلقت من المنصة
        to_remove = [s for s in saved_positions if s not in active_alpaca_symbols]
        for s in to_remove:
            saved_positions.pop(s, None)

        save_json(ACTIVE_POSITIONS_FILE, saved_positions)

    except Exception as e:
        log.error(f"⚠️ خطأ أثناء إدارة الصفقات النشطة: {e}")

def open_new_trade():
    bars = fetch_bars(SPY)
    if bars.empty:
        return
    pred = train_predict(bars)
    if not pred.get("ok"):
        return

    contracts = fetch_contracts(SPY, pred["signal"], pred["current_price"])
    for c in contracts:
        sym = c.get("symbol")
        if not sym:
            continue
        q = _get_option_quote(sym)
        if not q:
            continue
        
        spread = (q["ask"] - q["bid"]) / q["mid"]
        if spread > MAX_SPREAD_PCT:
            continue

        # تنفيذ شراء العقد
        order = execute_order(sym, qty=1, side="buy")
        if order:
            positions = read_json(ACTIVE_POSITIONS_FILE)
            positions[sym] = {
                "symbol": sym,
                "entry_price": q["ask"],
                "highest_price": q["ask"],
                "qty": 1,
                "time": stamp()
            }
            save_json(ACTIVE_POSITIONS_FILE, positions)
            log.info(f"🚀 تم فتح صفقة جديدة لـ {sym} بسعر ${q['ask']:.2f} بناءً على توقع: {pred['signal']}")
            break

# -------------------- MAIN LOOP --------------------
if __name__ == "__main__":
    log.info(f"🚀 بدء تشغيل نظام التنفيذ الذكي والمتابعة المستمرة {NAME} v{VERSION}")
    
    while True:
        try:
            if market_open():
                # 1. إدارة ومتابعة الصفقات المفتوحة لكل توقع جديد ومحاولة جني الأرباح وقف الخسارة المتحرك
                manage_active_positions()
                
                # 2. البحث عن فرص جديدة وفتح صفقات (عدد لامحدود حسب التوقعات)
                if in_signal_window():
                    open_new_trade()
            else:
                log.info("⏳ السوق مغلق حالياً.. البوت في وضع الانتظار.")
                
            time.sleep(POLL_SECONDS)
            
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.exception(f"⚠️ خطأ في حلقة التشغيل: {e}")
            time.sleep(30)