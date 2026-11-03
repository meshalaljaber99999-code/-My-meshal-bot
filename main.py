# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v86.2 (RATE-LIMIT SAFE & STABLE EXECUTION)
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
VERSION = "v86.2-RateLimitSafe"
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

# زيادة زمن الفحص لعدم تجاوز حد 200 طلب/دقيقة في Alpaca
LOOP_INTERVAL_SEC = 0.4         # فحص كل 400 مللي ثانية (آمن تماماً)
MAX_SPREAD_PCT = 0.12
MAX_PREMIUM_PCT = 0.20

UNDERLYING_TRAIL_DIST = 0.25
BREAKEVEN_TRIGGER = 0.12
OPTION_TRAIL_ACTIVATION = 0.25
OPTION_TRAIL_DISTANCE = 0.08

DEEP_OTM_MIN_PRICE = 0.10
DEEP_OTM_MAX_PRICE = 0.60
QUICK_PROFIT_TARGET_USD = 0.40

SIGNAL_START = dtime(9, 45)
SIGNAL_END = dtime(14, 0)
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
    """إرسال الطلبات مع إعادة المحاولة تلقائياً عند ظهور خطأ Too Many Requests (429)"""
    for attempt in range(max_retries):
        try:
            if method == "GET":
                r = session.get(url, params=params, timeout=4)
            elif method == "POST":
                r = session.post(url, json=json_body, timeout=4)
            elif method == "DELETE":
                r = session.delete(url, timeout=5)

            if r.status_code == 429:  # Too Many Requests
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
# 5. تنظيف وتصفية الصفقات القوي والتجزيئي (Batched Flush)
# ---------------------------------------------------------
def close_all_old_positions_and_orders():
    """تصفية إجبارية وتجزيئية لكافة الصفقات والأوامر القديمة"""
    log.info("🧹 FLUSHING OLD POSITIONS & ORDERS SAFELY...")
    
    # 1. إلغاء جميع الأوامر المعلقة
    api_delete(f"{TRADING_URL}/v2/orders")
    
    # 2. جلب جميع الصفقات المفتوحة في الحساب
    ok, positions = api_get(f"{TRADING_URL}/v2/positions")
    if ok and isinstance(positions, list):
        for pos in positions:
            symbol = pos.get("symbol")
            total_qty = abs(int(float(pos.get("qty", 0))))
            if total_qty == 0:
                continue

            log.info("Closing open position: %s | Total Qty: %d", symbol, total_qty)

            # إغلاق على دفعات لتجنب الرفض بسبب الكمية الكبيرة
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
                    log.info("✅ Batch of %d units sent for %s", current_batch, symbol)
                    remaining -= current_batch
                else:
                    log.warning("⚠️ Failed batch sell for %s: %s", symbol, res)
                    break
                time.sleep(0.5)

    # 3. محاولة تصفية شامّلة إضافية عبر API
    api_delete(f"{TRADING_URL}/v2/positions")

    # 4. تصفير الذاكرة المحفوظة
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
# 7. اقتناص وشراء العقود
# ---------------------------------------------------------
def choose_contract_fast(direction, underlying_price, high_confidence=False):
    opt_type = "call" if direction == "up" else "put"
    offset = underlying_price * (0.015 if high_confidence else 0.002)
    target_strike = underlying_price + offset if direction == "up" else underlying_price - offset

    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "type": opt_type,
        "expiration_date": today(),
        "limit": 15,
        "strike_price_gte": f"{target_strike - 3:.2f}",
        "strike_price_lte": f"{target_strike + 3:.2f}",
    }
    
    ok, data = api_get(f"{TRADING_URL}/v2/options/contracts", params=params)
    if not ok or not isinstance(data, dict):
        return None, None

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
        
        if spread_pct <= MAX_SPREAD_PCT:
            if high_confidence and DEEP_OTM_MIN_PRICE <= ask <= DEEP_OTM_MAX_PRICE:
                return sym, ask
            elif not high_confidence and ask >= 0.20:
                return sym, ask

    return None, None

def open_position_fast(direction, underlying_price, high_confidence=False):
    if state.get("position"):
        return

    contract, ask = choose_contract_fast(direction, underlying_price, high_confidence)
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
        "peak_underlying": underlying_price,
        "stop": ask * 0.80,
        "high_confidence": high_confidence,
        "entry_time": iso_now(),
    }
    state["position"] = pos
    save_state()
    log.info("⚡ FAST ENTRY | Contract: %s | Qty: %d | Ask: $%.2f | SPY: $%.2f", contract, qty, ask, underlying_price)

# ---------------------------------------------------------
# 8. إدارة الصفقة
# ---------------------------------------------------------
def manage_position_fast(current_underlying_price):
    pos = state.get("position")
    if not pos:
        return

    q = get_option_quote(pos["contract"])
    if not q:
        return
    bid = q[0]
    direction = pos["direction"]

    if direction == "up":
        if current_underlying_price > pos["peak_underlying"]:
            pos["peak_underlying"] = current_underlying_price
    else:
        if current_underlying_price < pos["peak_underlying"]:
            pos["peak_underlying"] = current_underlying_price

    if bid > pos["peak_option_price"]:
        pos["peak_option_price"] = bid

    option_gain = bid - pos["entry"]
    gain_pct = option_gain / pos["entry"]

    pullback = (pos["peak_underlying"] - current_underlying_price) if direction == "up" else (current_underlying_price - pos["peak_underlying"])
    if pullback >= UNDERLYING_TRAIL_DIST and gain_pct > 0.08:
        log.info("⚡ FAST PROFIT PROTECT: SPY Pulled back $%.2f. Closing immediately!", pullback)
        close_position_fast(pos, bid, "FAST_UNDERLYING_PROTECT")
        return

    if pos.get("high_confidence") and option_gain >= QUICK_PROFIT_TARGET_USD:
        log.info("⚡ QUICK TARGET HIT ($+0.40)! Closing OTM Position.")
        close_position_fast(pos, bid, "QUICK_OTM_TARGET")
        return

    if gain_pct >= BREAKEVEN_TRIGGER and pos["stop"] < pos["entry"]:
        pos["stop"] = pos["entry"] * 1.01

    if (pos["peak_option_price"] - pos["entry"]) / pos["entry"] >= OPTION_TRAIL_ACTIVATION:
        new_stop = pos["peak_option_price"] * (1 - OPTION_TRAIL_DISTANCE)
        if new_stop > pos["stop"]:
            pos["stop"] = new_stop

    if bid <= pos["stop"] or now_ny().time() >= EOD_CLOSE:
        close_position_fast(pos, bid, "STOP_OR_EOD")

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

    # تنظيف وتصفية الصفقات والأوامر بشكل آمن
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

                    momentum = (last_price - float(bars[0]["c"])) / float(bars[0]["c"])
                    high_conf = abs(momentum) >= 0.003

                    if last_price > vwap * 1.0010:
                        open_position_fast("up", last_price, high_confidence=high_conf)
                    elif last_price < vwap * 0.9990:
                        open_position_fast("down", last_price, high_confidence=high_conf)

            time.sleep(LOOP_INTERVAL_SEC)

        except Exception as e:
            log.exception("Loop Error: %s", e)
            time.sleep(1)

if __name__ == "__main__":
    run()
