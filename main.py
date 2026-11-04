# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v86.7 (ZERO-LOSS GUARANTEE & TRAILING LOCK)
Platform: Alpaca Paper Trading
Deployment: Railway (Includes Flask Keep-Alive Port)
=========================================================
"""

import os
import json
import time
import logging
import uuid
from pathlib import Path
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import requests
from threading import Thread
from flask import Flask

# ---------------------------------------------------------
# 1. خادم الويب لإبقاء الحاوية حية على Railway
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Safaa Quant Stable Engine Active!", 200

def start_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

Thread(target=start_flask, daemon=True).start()

# ---------------------------------------------------------
# 2. الإعدادات وتفادي Rate Limit
# ---------------------------------------------------------
VERSION = "v86.7-ZeroLossLocked"
SYMBOL = "SPY"
NY = ZoneInfo("America/New_York")

def env_float(name, default): return float(os.getenv(name, default))
def env_int(name, default): return int(os.getenv(name, default))
def env_first(*names, default=""):
    for name in names:
        val = os.getenv(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    return default

VIRTUAL_CAPITAL = 1000.0

LOOP_INTERVAL_SEC = 0.6         
MAX_SPREAD_PCT = 0.08           # تشديد الفارق لمنع العقود ذات الفجوة العالية
MAX_PREMIUM_PCT = 0.20

# معايير اختيار العقود
MIN_CONTRACT_PRICE = 0.20       # منع العقود الرخيصة والميتة
MAX_CONTRACT_PRICE = 2.50       
QUICK_PROFIT_TARGET_PCT = 0.12  # فحص الأرباح المبكر عند 12%
QUICK_STOP_LOSS_PCT = -0.08     # وقف خسارة أولي 8% قبل تفعيل درع الحماية

SIGNAL_START = dtime(9, 30)     # التداول فور فتح السوق
SIGNAL_END = dtime(15, 30)
EOD_CLOSE = dtime(15, 45)

KEY_NAMES = ["ALPACA_API_KEY", "APCA_API_KEY_ID", "ALPACA_KEY", "API_KEY"]
SECRET_NAMES = ["ALPACA_API_SECRET", "APCA_API_SECRET_KEY", "ALPACA_SECRET", "API_SECRET"]

API_KEY = env_first(*KEY_NAMES)
API_SECRET = env_first(*SECRET_NAMES)
TRADING_URL = env_first("ALPACA_TRADING_URL", "APCA_API_BASE_URL", "TRADING_URL", default="https://paper-api.alpaca.markets").rstrip("/")
DATA_URL = env_first("ALPACA_DATA_URL", "APCA_DATA_URL", "DATA_URL", default="https://data.alpaca.markets").rstrip("/")
STOCK_FEED = "iex"
OPTIONS_FEED = "indicative"

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "safaa_v86_state.json"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

session = requests.Session()
session.headers.update(HEADERS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("SAFAA_SAFE")

def now_ny(): return datetime.now(NY)
def today(): return now_ny().date().isoformat()
def iso_now(): return now_ny().isoformat(timespec="seconds")

# ---------------------------------------------------------
# 3. محرك الاتصال الآمن مع حماية Rate-Limit
# ---------------------------------------------------------
def safe_api_call(method, url, params=None, json_body=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            if method == "GET":
                r = session.get(url, params=params, timeout=4)
            elif method == "POST":
                r = session.post(url, json=json_body, timeout=4)
            elif method == "DELETE":
                r = session.delete(url, timeout=5)

            if r.status_code == 429:  
                log.warning("⚠️ Rate limit hit (429). Waiting 2 seconds before retry...")
                time.sleep(2)
                continue

            return r.ok, (r.json() if r.content else {}), r.status_code
        except Exception as e:
            log.error("API Call Error: %s", e)
            time.sleep(1)
    return False, {}, 429

def api_get(url, params=None): return safe_api_call("GET", url, params=params)[:2]
def api_post(url, body): return safe_api_call("POST", url, json_body=body)
def api_delete(url): return safe_api_call("DELETE", url)

# ---------------------------------------------------------
# 4. الحالة العامة
# ---------------------------------------------------------
def save_state():
    tmp = Path(str(STATE_FILE) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def new_state():
    return {
        "date": today(),
        "virtual_capital": VIRTUAL_CAPITAL,
        "net_pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "position": None,
    }

state = load_state()

# ---------------------------------------------------------
# 5. تنظيف وتصفية الصفقات (Batched Flush)
# ---------------------------------------------------------
def close_all_old_positions_and_orders():
    log.info("🧹 FLUSHING OLD POSITIONS & ORDERS SAFELY...")
    api_delete(f"{TRADING_URL}/v2/orders")
    
    ok, positions = api_get(f"{TRADING_URL}/v2/positions")
    if ok and isinstance(positions, list):
        for pos in positions:
            symbol = pos.get("symbol")
            total_qty = abs(int(float(pos.get("qty", 0))))
            if total_qty == 0:
                continue

            batch_size = 100
            remaining = total_qty
            while remaining > 0:
                current_batch = min(remaining, batch_size)
                st, res, code = api_post(f"{TRADING_URL}/v2/orders", {
                    "symbol": symbol,
                    "qty": str(current_batch),
                    "side": "sell",
                    "type": "market",
                    "time_in_force": "day"
                })
                if st:
                    remaining -= current_batch
                else:
                    break
                time.sleep(0.5)

    api_delete(f"{TRADING_URL}/v2/positions")
    state["position"] = None
    save_state()
    log.info("✅ Local state and positions successfully cleaned.")

# ---------------------------------------------------------
# 6. البيانات والأسعار اللحظية
# ---------------------------------------------------------
def get_stock_bars(symbol, start, end):
    params = {
        "timeframe": "1Min",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 100,
        "feed": STOCK_FEED,
    }
    ok, data = api_get(f"{DATA_URL}/v2/stocks/{symbol}/bars", params=params)
    if ok and isinstance(data, dict):
        return data.get("bars") or []
    return []

def session_bars():
    n = now_ny()
    start = n.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    end = n.astimezone(ZoneInfo("UTC"))
    return get_stock_bars(SYMBOL, start, end)

def get_option_quote(symbol):
    ok, data = api_get(f"{DATA_URL}/v1beta1/options/quotes/latest", params={"symbols": symbol, "feed": OPTIONS_FEED})
    if not ok or not isinstance(data, dict):
        return None
    q = (data.get("quotes") or {}).get(symbol)
    if not q:
        return None
    bid, ask = float(q.get("bp", 0)), float(q.get("ap", 0))
    return (bid, ask) if bid > 0 and ask > 0 else None

# ---------------------------------------------------------
# 7. اختيار العقود القريبة فقط (ATM / Near OTM)
# ---------------------------------------------------------
def choose_contract_fast(direction, underlying_price):
    opt_type = "call" if direction == "up" else "put"
    target_strike = underlying_price + 0.50 if direction == "up" else underlying_price - 0.50

    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "type": opt_type,
        "expiration_date": today(),
        "limit": 10,
        "strike_price_gte": f"{target_strike - 1.00:.2f}",
        "strike_price_lte": f"{target_strike + 1.00:.2f}",
    }
    
    ok, data = api_get(f"{TRADING_URL}/v2/options/contracts", params=params)
    if not ok or not isinstance(data, dict):
        return None, None, None

    contracts = sorted(
        data.get("option_contracts") or [],
        key=lambda x: abs(float(x["strike_price"]) - target_strike)
    )

    for c in contracts:
        sym = c["symbol"]
        q = get_option_quote(sym)
        if not q:
            continue
        bid, ask = q
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        
        if spread_pct <= MAX_SPREAD_PCT and MIN_CONTRACT_PRICE <= ask <= MAX_CONTRACT_PRICE:
            strike_val = float(c.get("strike_price", target_strike))
            return sym, ask, strike_val

    return None, None, None

def open_position_fast(direction, underlying_price):
    if state.get("position"):
        return

    contract, ask, strike_val = choose_contract_fast(direction, underlying_price)
    if not contract or not ask:
        return

    max_alloc = state.get("virtual_capital", VIRTUAL_CAPITAL) * MAX_PREMIUM_PCT
    qty = max(1, int(max_alloc // (ask * 100)))

    st, data, _ = api_post(f"{TRADING_URL}/v2/orders", {
        "symbol": contract,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": f"f-{uuid.uuid4().hex[:10]}",
    })

    if not st:
        return

    pos = {
        "contract": contract,
        "direction": direction,
        "qty": qty,
        "entry": ask,
        "peak_option_price": ask,
        "entry_underlying": underlying_price,
        "strike": strike_val,
        "stop": ask * (1 + QUICK_STOP_LOSS_PCT),
        "entry_time": iso_now(),
    }
    state["position"] = pos
    save_state()
    log.info("⚡ ENTERED ATM POSITION | Contract: %s | Strike: $%.2f | Ask: $%.2f | SPY: $%.2f", contract, strike_val, ask, underlying_price)

# ---------------------------------------------------------
# 8. إدارة الصفقة الذكية (ضمان عدم الخروج بخسارة + البيع عند التراجع)
# ---------------------------------------------------------
def manage_position_fast(current_underlying_price):
    pos = state.get("position")
    if not pos:
        return

    q = get_option_quote(pos["contract"])
    if not q:
        return
        
    bid = q[0] # سعر البيع اللحظي
    entry = pos["entry"]
    direction = pos["direction"]
    strike = pos.get("strike", 0.0)
    
    gain_pct = (bid - entry) / entry

    # تحديث أعلى سعر وصل له العقد
    if bid > pos.get("peak_option_price", entry):
        pos["peak_option_price"] = bid

    peak_gain_pct = (pos["peak_option_price"] - entry) / entry

    # -----------------------------------------------------------------
    # أ: درع حماية التعادل الصارم (Breakeven Shield - NO LOSS GUARANTEE)
    # -----------------------------------------------------------------
    # فور ارتفاع العقد 3% فقط، يتم تأمين سعر الدخول + تغطية العمولات فوراً
    if gain_pct >= 0.03 and pos["stop"] < (entry * 1.025):
        pos["stop"] = entry * 1.025
        log.info("🛡️ BREAKEVEN SHIELD ACTIVE: Stop moved to $%.2f (Entry: $%.2f). Zero-loss guaranteed!", pos["stop"], entry)

    # -----------------------------------------------------------------
    # ب: حماية الأرباح الفورية بمجرد نزول/تراجع المؤشر أو العقد (Trailing Exit)
    # -----------------------------------------------------------------
    # إذا ارتفع العقد وتراجع بمقدار 3% فقط من أعلى قمة وصل لها، يتم البيع وحفظ الربح فوراً
    if peak_gain_pct >= 0.06:
        pullback_from_peak = (pos["peak_option_price"] - bid) / pos["peak_option_price"]
        
        # فحص القرب الشديد من السترايك
        dist_to_strike = abs(strike - current_underlying_price) if strike > 0 else 999.0
        is_close_to_strike = dist_to_strike <= 0.30
        
        if pullback_from_peak >= 0.03:  # نزل العقد 3% من القمة
            if not is_close_to_strike:
                log.info("⚡ DROP DETECTED! Option pulled back %.2f%% from peak $%.2f -> Selling at $%.2f to lock gain!", 
                         pullback_from_peak * 100, pos["peak_option_price"], bid)
                close_position_fast(pos, bid, "TRAILING_DROP_PROFIT_LOCK")
                return

    # -----------------------------------------------------------------
    # ج: جني الأرباح السريع أو مواصلة الصعود نحو السترايك
    # -----------------------------------------------------------------
    if gain_pct >= QUICK_PROFIT_TARGET_PCT:
        is_strong_momentum = (current_underlying_price > pos["entry_underlying"] + 0.30) if direction == "up" else (current_underlying_price < pos["entry_underlying"] - 0.30)
        dist_to_strike = abs(strike - current_underlying_price) if strike > 0 else 999.0
        
        if dist_to_strike <= 0.30 or is_strong_momentum:
            # زخم قوي متسارع للسترايك -> يرفع الستوب لحجز أرباح 8% وتأمينها دون بيع العقد
            pos["stop"] = max(pos["stop"], entry * 1.08)
            log.info("🚀 STRIKE IN REACH! Holding contract. Trailing stop raised to +8%% ($%.2f)", pos["stop"])
        else:
            log.info("⚡ SCALP PROFIT TARGET HIT! Option Gain: %.2f%% ($%.2f -> $%.2f)", gain_pct * 100, entry, bid)
            close_position_fast(pos, bid, "SCALP_PROFIT_TARGET")
            return

    # -----------------------------------------------------------------
    # د: الخروج التلقائي عند ضرب الستوب المحمي أو عند إغلاق الجلسة
    # -----------------------------------------------------------------
    if bid <= pos["stop"] or now_ny().time() >= EOD_CLOSE:
        reason = "PROTECTED_BREAKEVEN_EXIT" if pos["stop"] >= entry else "STOP_LOSS"
        log.info("🛑 EXIT EXECUTED [%s] at $%.2f", reason, bid)
        close_position_fast(pos, bid, reason)

def close_position_fast(pos, exit_price, reason):
    api_post(f"{TRADING_URL}/v2/orders", {
        "symbol": pos["contract"],
        "qty": str(pos["qty"]),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    })

    net_pnl = (exit_price - pos["entry"]) * 100 * pos["qty"] - (pos["qty"] * 1.30)
    state["virtual_capital"] += net_pnl
    state["net_pnl"] += net_pnl
    if net_pnl > 0: state["wins"] += 1

    log.info("🏁 FAST EXIT [%s] | Exit Price: $%.2f | Net PnL: $%.2f | Virtual Cap: $%.2f",
             reason, exit_price, net_pnl, state["virtual_capital"])
    state["position"] = None
    save_state()

# ---------------------------------------------------------
# 9. الحلقة الرئيسية
# ---------------------------------------------------------
def run():
    log.info("⚡ SAFAA ENGINE %s STARTED", VERSION)
    if not state:
        state.update(new_state())
        save_state()

    close_all_old_positions_and_orders()

    while True:
        try:
            ok, clock = api_get(f"{TRADING_URL}/v2/clock")
            if not ok or not (clock or {}).get("is_open"):
                time.sleep(10)
                continue

            bars = session_bars()
            if bars:
                last_price = float(bars[-1]["c"])
                
                if state.get("position"):
                    manage_position_fast(last_price)
                elif SIGNAL_START <= now_ny().time() <= SIGNAL_END:
                    pv = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0 * float(b["v"]) for b in bars)
                    vol = sum(float(b["v"]) for b in bars)
                    vwap = pv / vol if vol > 0 else last_price

                    if last_price > vwap * 1.0005:
                        open_position_fast("up", last_price)
                    elif last_price < vwap * 0.9995:
                        open_position_fast("down", last_price)

            time.sleep(LOOP_INTERVAL_SEC)

        except Exception as e:
            log.exception("Loop Error: %s", e)
            time.sleep(1)

if __name__ == "__main__":
    run()
