# ============================================================
# صَفاء التحليل v28.1-HyperYield-Fixed | Live Trading Engine
# البوت المكتمل والمصحح برمجياً: التداول المتعدد + الفائدة المركبة + Breakeven
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
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline

# -------------------- CONFIG --------------------
NAME, VERSION = "صَفاء التحليل", "28.1-HyperYield-Fixed"
NY, UTC = ZoneInfo("America/New_York"), timezone.utc
TF = "5Min"

HYPER_ASSETS = ["SPY", "QQQ", "NVDA", "TSLA"]

DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
KEY = (os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY") or "").strip()
SECRET = (os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY") or "").strip()
STOCK_FEED = os.getenv("ALPACA_DATA_FEED", "iex").lower()
OPTION_FEED = os.getenv("OPTIONS_FEED", "indicative").lower()

HISTORY_DAYS = 120         
MIN_ROWS = 300
HORIZON = 6

MAX_SPREAD_PCT = 0.15          
POLL_SECONDS = 20

ROOT = Path(os.getenv("DATA_DIR", "."))
ROOT.mkdir(parents=True, exist_ok=True)
ACTIVE_POSITIONS_FILE = ROOT / "safa_active_positions.json"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("safaa_hyperyield")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": f"SafaaAnalysisLive/{VERSION}"})

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
        raise RuntimeError("مفاتيح APCA_API_KEY_ID / APCA_API_SECRET_KEY غير مضبوطة")
    last = None
    for attempt in range(3):
        try:
            r = HTTP.get(
                url,
                headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET},
                params=params,
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Alpaca request failed: {last}")

def alpaca_post(url, payload=None):
    if not KEY or not SECRET:
        raise RuntimeError("مفاتيح APCA_API_KEY_ID / APCA_API_SECRET_KEY غير مضبوطة")
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
    return n.weekday() < 5 and (n.hour, n.minute) >= (9, 30) and (n.hour, n.minute) < (16, 0)

def in_signal_window():
    n = now_ny()
    return n.weekday() < 5 and (n.hour, n.minute) >= (9, 35) and (n.hour, n.minute) < (15, 45)

# -------------------- COMPOUND ENGINE --------------------
def calculate_dynamic_qty(option_mid_price):
    try:
        acc = alpaca_get(f"{TRADING_URL}/v2/account")
        equity = num(acc.get("equity"), 1000.0)

        if equity < 10000:
            risk_pct = 0.18      
        elif equity < 100000:
            risk_pct = 0.12      
        elif equity < 1000000:
            risk_pct = 0.07      
        else:
            risk_pct = 0.03      

        trade_budget = equity * risk_pct
        contract_cost = option_mid_price * 100

        qty = math.floor(trade_budget / contract_cost)
        final_qty = min(max(1, qty), 60)  
        
        log.info(f"💰 رأس المال: ${equity:,.2f} | نسبة الصفقة: {risk_pct*100}% | عدد العقود: {final_qty}")
        return final_qty
    except Exception as e:
        log.error(f"⚠️ فشل حساب حجم الصفقة التلقائي: {e}")
        return 1

# -------------------- ORDERS & DATA --------------------
def execute_limit_order(symbol, qty=1, side="buy", limit_price=None):
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
        log.info(f"✅ أمر محدد ({side.upper()}) بسعر ${limit_price:.2f} | العقد: {symbol} (كمية: {qty})")
        return res
    except Exception as e:
        log.error(f"❌ فشل تنفيذ الأمر لـ {symbol}: {e}")
        return None

def fetch_bars(symbol, days=HISTORY_DAYS):
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

FEATURES = ["ret1", "ret3", "ret6", "ret12", "ema_spread", "ema_trend", "rsi", "atr_pct", "body_pct"]

def features_for(bars):
    f = bars.copy()
    c = f["close"]
    f["ret1"] = c.pct_change()
    f["ret3"] = c.pct_change(3)
    f["ret6"] = c.pct_change(6)
    f["ret12"] = c.pct_change(12)
    e9 = c.ewm(span=9, adjust=False).mean()
    e20 = c.ewm(span=20, adjust=False).mean()
    e50 = c.ewm(span=50, adjust=False).mean()
    f["ema_spread"] = (e9 - e20) / c
    f["ema_trend"] = (e20 - e50) / c
    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    f["rsi"] = ((100 - 100 / (1 + rs)) / 100).fillna(0.5)
    prev = c.shift()
    tr = pd.concat([f["high"] - f["low"], (f["high"] - prev).abs(), (f["low"] - prev).abs()], axis=1).max(axis=1)
    f["atr_pct"] = tr.ewm(alpha=1/14, adjust=False).mean() / c
    f["body_pct"] = (f["close"] - f["open"]) / c
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
    
    model = make_pipeline(SimpleImputer(strategy="median"), RandomForestClassifier(n_estimators=200, max_depth=6, class_weight="balanced", random_state=42, n_jobs=-1))
    model.fit(ds[FEATURES], ds["target"].astype(int))
    prob_up = float(model.predict_proba(f[FEATURES].iloc[[-1]])[0, 1])
    
    return {
        "ok": True,
        "signal": "صعود 🚀" if prob_up >= 0.58 else ("نزول 📉" if prob_up <= 0.42 else "محايد ⏳"),
        "probability": prob_up,
        "current_price": float(bars["close"].iloc[-1]),
    }

# -------------------- OPTIONS CONTRACTS --------------------
def fetch_contracts(symbol, direction, price):
    target_exp = (now_ny() + timedelta(days=4)).date()
    params = {
        "underlying_symbols": symbol,
        "status": "active",
        "type": "call" if direction == "صعود 🚀" else "put",
        "expiration_date_gte": target_exp.isoformat(),
        "limit": 30,
    }
    payload = alpaca_get(f"{TRADING_URL}/v2/options/contracts", params)
    contracts = payload.get("option_contracts") or payload.get("contracts") or []
    candidates = []
    for c in contracts:
        strike = num(c.get("strike_price"))
        sym = c.get("symbol")
        if sym and math.isfinite(strike) and abs(strike - price) <= (price * 0.03):
            candidates.append((abs(strike - price), c))
    candidates.sort(key=lambda x: x[0])
    return [c for _, c in candidates[:3]]

def _get_option_quote(symbol):
    payload = alpaca_get(f"{DATA_URL}/v1beta1/options/quotes/latest", {"symbols": symbol, "feed": OPTION_FEED})
    q = (payload.get("quotes") or {}).get(symbol, {})
    bid, ask = num(q.get("bp")), num(q.get("ap"))
    if not math.isfinite(bid) or bid <= 0 or not math.isfinite(ask) or ask < bid:
        return None
    return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}

# -------------------- POSITION MANAGEMENT --------------------
def manage_active_positions():
    saved_positions = read_json(ACTIVE_POSITIONS_FILE)
    try:
        r = HTTP.get(f"{TRADING_URL}/v2/positions", headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}, timeout=15)
        positions_payload = r.json() if r.status_code == 200 else []
        active_alpaca_symbols = set()
        
        if isinstance(positions_payload, list):
            for pos in positions_payload:
                sym = pos.get("symbol")
                if sym:
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

                    log.info(f"📊 صفقة {sym} | دخول: ${entry_price:.2f} | حالي: ${current_price:.2f} | الربح: {pnl_pct*100:+.2f}%")

                    saved_positions[sym] = {
                        "symbol": sym,
                        "entry_price": entry_price,
                        "highest_price": highest_price,
                        "qty": qty,
                        "time": pos_data.get("time", stamp())
                    }

                    q = _get_option_quote(sym)
                    exit_limit_price = q["mid"] if q else current_price

                    # 1. اقتناص الربح الخرافي (+100% ثم هبوط 30% من القمة)
                    if peak_pnl_pct >= 1.00 and pnl_pct <= (peak_pnl_pct - 0.30):
                        log.info(f"🚀 جني أرباح خرافية (+100%) لـ {sym}!")
                        execute_limit_order(sym, qty=qty, side="sell", limit_price=exit_limit_price)
                        saved_positions.pop(sym, None)

                    # 2. تأمين +35% أرباح عند الوصول لـ +50%
                    elif peak_pnl_pct >= 0.50 and pnl_pct <= 0.35:
                        log.info(f"📈 تأمين ربح ممتاز (+35%) لـ {sym}!")
                        execute_limit_order(sym, qty=qty, side="sell", limit_price=exit_limit_price)
                        saved_positions.pop(sym, None)

                    # 3. تفعيل Breakeven عند الوصول لـ +15%
                    elif peak_pnl_pct >= 0.15 and pnl_pct <= 0.02:
                        log.info(f"🛡️ تفعيل Breakeven لـ {sym}! إغلاق بدون خسارة.")
                        execute_limit_order(sym, qty=qty, side="sell", limit_price=exit_limit_price)
                        saved_positions.pop(sym, None)

                    # 4. وقف خسارة صارم (-10%)
                    elif pnl_pct <= -0.10:
                        log.info(f"🛑 وقف خسارة خاطف (-10%) لـ {sym}! حماية الحساب.")
                        execute_limit_order(sym, qty=qty, side="sell", limit_price=exit_limit_price)
                        saved_positions.pop(sym, None)

        to_remove = [s for s in saved_positions if s not in active_alpaca_symbols]
        for s in to_remove:
            saved_positions.pop(s, None)

        save_json(ACTIVE_POSITIONS_FILE, saved_positions)
    except Exception as e:
        log.error(f"⚠️ خطأ أثناء متابعة الصفقات: {e}")

def open_new_trade():
    positions = read_json(ACTIVE_POSITIONS_FILE)
    if len(positions) >= 2: 
        return

    # استخراج الأصول التي توجد لها صفقات مفتوحة حالياً لمنع التكرار على نفس الأصل
    active_underlying_symbols = set(pos_data.get("underlying", "") for pos_data in positions.values())

    for asset in HYPER_ASSETS:
        if asset in active_underlying_symbols:
            continue # تجنب فتح صفتين على نفس الأصل

        bars = fetch_bars(asset)
        if bars.empty:
            continue
        pred = train_predict(bars)
        if not pred.get("ok") or pred["signal"] == "محايد ⏳":
            continue

        contracts = fetch_contracts(asset, pred["signal"], pred["current_price"])
        for c in contracts:
            sym = c.get("symbol")
            if not sym or sym in positions:
                continue
            q = _get_option_quote(sym)
            if not q:
                continue
            
            spread = (q["ask"] - q["bid"]) / q["mid"]
            if spread > MAX_SPREAD_PCT:
                continue

            trade_qty = calculate_dynamic_qty(q["mid"])

            order = execute_limit_order(sym, qty=trade_qty, side="buy", limit_price=q["mid"])
            if order:
                positions[sym] = {
                    "symbol": sym,
                    "underlying": asset,
                    "entry_price": q["mid"],
                    "highest_price": q["mid"],
                    "qty": trade_qty,
                    "time": stamp()
                }
                save_json(ACTIVE_POSITIONS_FILE, positions)
                log.info(f"🚀 فتح صفقة لـ {sym} (أصل: {asset}) | كمية: {trade_qty} | Mid: ${q['mid']:.2f} | إشارة: {pred['signal']}")
                return 

# -------------------- MAIN LOOP --------------------
if __name__ == "__main__":
    log.info(f"🚀 بدء تشغيل {NAME} v{VERSION} (الكود المصحح والجاهز بالكامل)")
    
    while True:
        try:
            if market_open():
                manage_active_positions()
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
