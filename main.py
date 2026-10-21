# ============================================================
# صَفاء التحليل v31.0-SPY-TrendRider | Live Quantitative Engine
# الإصدار الكامل: صائد تحركات الهبوط والصعود الحادة (ORB) + تتبع الأرباح المفتوحة
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
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline

# -------------------- CONFIGURATION --------------------
NAME, VERSION = "صَفاء التحليل", "31.0-SPY-TrendRider"
NY, UTC = ZoneInfo("America/New_York"), timezone.utc
TF = "5Min"

HYPER_ASSET = "SPY"

DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
KEY = (os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or "").strip()
SECRET = (os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY") or "").strip()
STOCK_FEED = os.getenv("ALPACA_DATA_FEED", "iex").lower()
OPTION_FEED = os.getenv("OPTIONS_FEED", "indicative").lower()

HISTORY_DAYS = 45         
MIN_ROWS = 250
HORIZON = 5

MAX_SPREAD_PCT = 0.06        # أقصى فارق بين العرض والطلب 6%
POLL_SECONDS = 10            # فحص السوق كل 10 ثوانٍ

MAX_DAILY_LOSS_PCT = 0.02    # كاسر الحماية الصارم (إغلاق وتجميد عند -2%)
MAX_RISK_CAP = 0.05          # أقصى مخاطرة لكل صفقة (5% من المحفظة)

ROOT = Path(os.getenv("DATA_DIR", "."))
ROOT.mkdir(parents=True, exist_ok=True)
ACTIVE_POSITIONS_FILE = ROOT / "spy_trendrider_positions.json"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("safaa_trend_rider")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": f"SafaaTrendRider/{VERSION}"})

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
    # فتح الصفقات مسموح به من 9:45 AM إلى 3:30 PM بتوقيت نيويورك
    return n.weekday() < 5 and (n.hour, n.minute) >= (9, 45) and (n.hour, n.minute) < (15, 30)

def is_eod_close_time():
    n = now_ny()
    return n.weekday() < 5 and (n.hour, n.minute) >= (15, 50)

# -------------------- ADVANCED QUANT ALPHA & FEATURES --------------------
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

FEATURES = [
    "ret1", "ret3", "ret6", "ema_9_21", "ema_21_50", "trend_200", 
    "vwap_zscore", "vwap_band_pos", "rsi", "atr_norm", "vol_surge", "regime_efficiency"
]

def generate_institutional_features(bars):
    f = bars.copy()
    c, v = f["close"], f["volume"]
    
    # 1. Momentum Returns
    f["ret1"] = c.pct_change()
    f["ret3"] = c.pct_change(3)
    f["ret6"] = c.pct_change(6)
    
    # 2. Institutional Moving Averages
    e9 = c.ewm(span=9, adjust=False).mean()
    e21 = c.ewm(span=21, adjust=False).mean()
    e50 = c.ewm(span=50, adjust=False).mean()
    e200 = c.ewm(span=200, adjust=False).mean()
    
    f["ema_9_21"] = (e9 - e21) / c
    f["ema_21_50"] = (e21 - e50) / c
    f["trend_200"] = (c - e200) / c
    
    # 3. Intraday VWAP & StDev Bands
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
    
    # 4. Kaufman Efficiency Ratio (Market Regime)
    change = (c - c.shift(10)).abs()
    volatility = c.diff().abs().rolling(10).sum()
    f["regime_efficiency"] = (change / volatility.replace(0, np.nan)).fillna(0)
    
    # 5. Volatility & ATR
    prev = c.shift()
    tr = pd.concat([f["high"] - f["low"], (f["high"] - prev).abs(), (f["low"] - prev).abs()], axis=1).max(axis=1)
    f["atr_norm"] = tr.ewm(alpha=1/14, adjust=False).mean() / c
    f["vol_surge"] = v / v.rolling(30).mean().replace(0, np.nan)
    
    # RSI
    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    f["rsi"] = ((100 - 100 / (1 + rs)) / 100).fillna(0.5)
    
    f.drop(columns=['trade_date', 'pv', 'sq_diff'], inplace=True, errors='ignore')
    return f.replace([np.inf, -np.inf], np.nan)

# -------------------- OPENING RANGE BREAKOUT (ORB) --------------------
def detect_opening_range_breakout(bars):
    """رصد كسر نطاق أول 15 دقيقة مصحوباً بضخ سيولة مؤسسية للسيطرة على الأيام الحادة"""
    if bars.empty or len(bars) < 15:
        return "NONE"
        
    f = bars.copy()
    f['ny_time'] = f.index.tz_convert(NY)
    today_date = now_ny().date()
    today_bars = f[f['ny_time'].dt.date == today_date]
    
    # تحديد نطاق أول 15 دقيقة (9:30 AM - 9:45 AM)
    orb_bars = today_bars[(today_bars['ny_time'].dt.hour == 9) & (today_bars['ny_time'].dt.minute >= 30) & (today_bars['ny_time'].dt.minute < 45)]
    if len(orb_bars) < 3:
        return "NONE"
        
    orb_high = orb_bars['high'].max()
    orb_low = orb_bars['low'].min()
    
    latest_close = float(today_bars['close'].iloc[-1])
    vol_mean = today_bars['volume'].rolling(20).mean().iloc[-1]
    vol_surge = float(today_bars['volume'].iloc[-1] / vol_mean) if vol_mean > 0 else 1.0

    # كسر القاع وسيولة عالية (مؤشر هبوط حاد)
    if latest_close < orb_low and vol_surge >= 2.0:
        log.info(f"⚡ [ORB Breakout] كسر قاع أول 15 دقيقة (${orb_low:.2f}) مع ضخ سيولة ({vol_surge:.1f}x)!")
        return "DOWN"
    # كسر القمة وسيولة عالية (مؤشر صعود حاد)
    elif latest_close > orb_high and vol_surge >= 2.0:
        log.info(f"⚡ [ORB Breakout] كسر قمة أول 15 دقيقة (${orb_high:.2f}) مع ضخ سيولة ({vol_surge:.1f}x)!")
        return "UP"
        
    return "NONE"

# -------------------- DUAL MODEL META-LABELING AI --------------------
def train_institutional_quant_model(bars):
    if bars.empty or len(bars) < MIN_ROWS:
        return {"ok": False}
        
    f = generate_institutional_features(bars)
    future_ret = (bars["close"].shift(-HORIZON) - bars["close"]) / bars["close"]
    
    f["primary_target"] = np.where(future_ret > 0.0015, 1, (np.where(future_ret < -0.0015, -1, 0)))
    
    ds = f[FEATURES + ["primary_target"]].dropna()
    ds_valid = ds[ds["primary_target"] != 0]
    
    if len(ds_valid) < MIN_ROWS:
        return {"ok": False}
        
    # 1. Primary Directional Classifier
    primary_model = GradientBoostingClassifier(n_estimators=150, learning_rate=0.04, max_depth=4, random_state=42)
    primary_model.fit(ds_valid[FEATURES], ds_valid["primary_target"])
    
    latest_feat = f[FEATURES].iloc[[-1]]
    primary_pred = primary_model.predict(latest_feat)[0]
    probs = primary_model.predict_proba(latest_feat)[0]
    
    classes = list(primary_model.classes_)
    prob_max = probs[classes.index(primary_pred)] if primary_pred in classes else 0.5

    # 2. Meta-Labeler
    ds_valid = ds_valid.copy()
    ds_valid["pred_signal"] = primary_model.predict(ds_valid[FEATURES])
    ds_valid["meta_target"] = np.where((ds_valid["pred_signal"] == ds_valid["primary_target"]), 1, 0)
    
    meta_prob = 0.50
    if ds_valid["meta_target"].nunique() >= 2:
        meta_model = RandomForestClassifier(n_estimators=80, max_depth=3, random_state=42)
        meta_model.fit(ds_valid[FEATURES], ds_valid["meta_target"])
        meta_classes = list(meta_model.classes_)
        if 1 in meta_classes:
            idx1 = meta_classes.index(1)
            meta_prob = float(meta_model.predict_proba(latest_feat)[0, idx1])

    curr_price = float(bars["close"].iloc[-1])
    efficiency = float(f["regime_efficiency"].iloc[-1])
    zscore = float(f["vwap_zscore"].iloc[-1])
    trend_200 = float(f["trend_200"].iloc[-1])
    
    # فحص صائد الكسر اليومي ORB
    orb_signal = detect_opening_range_breakout(bars)
    
    signal = "محايد ⏳"
    
    # دمج إشارات التعلم الآلي مع الكسر اليومي ORB
    if (primary_pred == 1 or orb_signal == "UP") and prob_max >= 0.55 and meta_prob >= 0.52 and trend_200 > -0.003:
        if -1.8 <= zscore <= 2.2:
            signal = "صعود 🚀"
            
    elif (primary_pred == -1 or orb_signal == "DOWN") and prob_max >= 0.55 and meta_prob >= 0.52 and trend_200 < 0.003:
        if -2.2 <= zscore <= 1.8:
            signal = "نزول 📉"

    return {
        "ok": True,
        "signal": signal,
        "primary_prob": prob_max,
        "meta_prob": meta_prob,
        "current_price": curr_price,
    }

# -------------------- HALF-KELLY CAPITAL ALLOCATION --------------------
def calculate_half_kelly_qty(option_mid_price, win_probability):
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
            log.warning(f"⚠️ تكلفة العقد (${contract_cost:.2f}) تتجاوز حد المخاطرة الأقصى لميزانية الحساب (${trade_budget:.2f}). إلغاء الصفقة.")
            return 0

        qty = math.floor(trade_budget / contract_cost)
        final_qty = max(1 if contract_cost <= trade_budget * 1.5 else 0, min(qty, 30))
        
        log.info(f"🧠 [Half-Kelly] رأس المال: ${equity:,.2f} | نسبة التخصيص: {half_kelly*100:.2f}% | عقود SPY: {final_qty}")
        return final_qty
    except Exception as e:
        log.error(f"⚠️ خطأ أثناء حساب حجم Half-Kelly: {e}")
        return 0

# -------------------- ORDERS & POSITION MANAGEMENT --------------------
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
        if sym and math.isfinite(strike) and abs(strike - spy_price) <= (spy_price * 0.015):
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
                price = q["mid"] if q else num(pos.get("current_price"), 0.1)
                execute_limit_order(sym, qty=abs(int(qty)), side="sell", limit_price=price)
        save_json(ACTIVE_POSITIONS_FILE, {})
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
            log.warning(f"🛑 [Circuit Breaker] خسارة المحفظة اليوم ({daily_pnl_pct*100:.2f}%). تفعيل الإغلاق الفوري وتجميد التداول.")
            close_all_positions_immediately()
            return True
    except Exception as e:
        log.error(f"خطأ أثناء فحص كاسر الحماية: {e}")
    return False

def manage_active_positions():
    """إدارة الصفقات بآلية تتبع الأرباح المفتوحة للاستفادة من الأيام الخارقة"""
    saved_positions = read_json(ACTIVE_POSITIONS_FILE)
    try:
        r = HTTP.get(f"{TRADING_URL}/v2/positions", headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}, timeout=10)
        positions_payload = r.json() if r.status_code == 200 else []
        active_alpaca_symbols = set()
        
        open_orders = get_open_orders()
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

                log.info(f"📊 [متابعة SPY] {sym} | الدخول: ${entry_price:.2f} | الحالي: ${current_price:.2f} | القمة: +{peak_pnl_pct*100:.1f}% | الربح الصافي: {pnl_pct*100:+.2f}%")

                saved_positions[sym] = {
                    "symbol": sym,
                    "entry_price": entry_price,
                    "highest_price": highest_price,
                    "qty": qty,
                    "time": pos_data.get("time", stamp())
                }

                if sym in pending_sell_symbols:
                    log.info(f"⏳ أمر بيع قائم بالفعل لـ {sym} بانتظار التنفيذ في السوق.")
                    continue

                q = _get_option_quote(sym)
                exit_price = q["mid"] if q else current_price

                should_close = False
                reason = ""

                # 0. إغلاق إجباري نهاية اليوم
                if force_eod_close:
                    should_close = True
                    reason = "🌆 إغلاق نهاية اليوم (EOD Hard Exit)"

                # 1. اقتناص الأرباح الانفجارية الخارقة (+100% فأكثر مع تراجع 12% من القمة)
                elif peak_pnl_pct >= 1.00 and pnl_pct <= (peak_pnl_pct - 0.12):
                    should_close = True
                    reason = f"🚀 اقتناص أرباح انفجارية (+100%+) | الربح المحقق: {pnl_pct*100:.2f}%"

                # 2. تتبع الأرباح المرتفعة (+30% إلى +100% مع تراجع 10% من القمة)
                elif peak_pnl_pct >= 0.30 and pnl_pct <= (peak_pnl_pct - 0.10):
                    should_close = True
                    reason = f"🏆 حجز أرباح ممتازة | الربح المحقق: {pnl_pct*100:.2f}%"

                # 3. تأمين +10% ربح بعد الوصول لـ +18%
                elif peak_pnl_pct >= 0.18 and pnl_pct <= 0.10:
                    should_close = True
                    reason = f"🛡️ تأمين أرباح (+10%) بعد قمة +18%"

                # 4. تفعيل Breakeven عند ربح +8% والتراجع إلى +1%
                elif peak_pnl_pct >= 0.08 and pnl_pct <= 0.01:
                    should_close = True
                    reason = "🔒 تفعيل Breakeven (إغلاق بدون خسارة)"

                # 5. وقف خسارة خاطف وصارم (-6.5%)
                elif pnl_pct <= -0.065:
                    should_close = True
                    reason = f"🛑 وقف خسارة خاطف (-6.5%) لـ {sym}"

                if should_close:
                    log.info(f"{reason} | العقد: {sym}")
                    execute_limit_order(sym, qty=qty, side="sell", limit_price=exit_price)

        to_remove = [s for s in saved_positions if s not in active_alpaca_symbols]
        for s in to_remove:
            saved_positions.pop(s, None)

        save_json(ACTIVE_POSITIONS_FILE, saved_positions)
    except Exception as e:
        log.error(f"⚠️ خطأ أثناء إدارة الصفقات: {e}")

def open_new_trade():
    if check_daily_circuit_breaker():
        return

    positions = read_json(ACTIVE_POSITIONS_FILE)
    if len(positions) >= 1:
        return

    bars = fetch_bars(HYPER_ASSET)
    if bars.empty:
        return
        
    pred = train_institutional_quant_model(bars)
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
            log.info(f"🚀 [صفقة TrendRider جديدة] {sym} | كمية: {trade_qty} | السعر: ${q['mid']:.2f} | إشارة: {pred['signal']} (ثقة: {win_prob*100:.1f}%)")
            return

# -------------------- MAIN LOOP --------------------
if __name__ == "__main__":
    log.info(f"🚀 بدء تشغيل {NAME} v{VERSION} (إصدار تتبع الأرباح المفتوحة وORB)")
    
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
