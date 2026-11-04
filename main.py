# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v89.0 (FIXED TAKE-PROFIT & BREAKEVEN ENGINE)
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
    return "Safaa Quant Multi-Engine Active! (v89.0)", 200

def start_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

Thread(target=start_flask, daemon=True).start()

# ---------------------------------------------------------
# 2. الإعدادات وتفادي Rate Limit
# ---------------------------------------------------------
VERSION = "v89.0-FixedTP-Breakeven"
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
MAX_PREMIUM_PCT = 0.20          # تخصيص 20% لكل صفقة لإنشاء صفقات متعددة بحماية
MAX_OPEN_POSITIONS = 3          # الحد الأقصى للصفقات المفتوحة بوقت واحد

LOOP_INTERVAL_SEC = 0.6         
MAX_SPREAD_PCT = 0.08           
MIN_CONTRACT_PRICE = 0.20       
MAX_CONTRACT_PRICE = 2.50       

SIGNAL_START = dtime(9, 30)     
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
STATE_FILE = DATA_DIR / "safaa_v89_state.json"

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
# 3. محرك الاتصال الآمن
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
# 4. الحالة العامة (دعم صفقات متعددة)
# ---------------------------------------------------------
def save_state():
    tmp = Path(str(STATE_FILE) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
            if "positions" not in st:
                st["positions"] = {}
            return st
    except Exception:
        return {}

def new_state():
    return {
        "date": today(),
        "virtual_capital": VIRTUAL_CAPITAL,
        "net_pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "positions": {}, 
    }

state = load_state()

# ---------------------------------------------------------
# 5. تنظيف وتصفية الصفقات عند البدء
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
    state["positions"] = {}
    save_state()

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
# 7. اختيار العقود القريبة فقط (ATM)
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
        return None, None

    contracts = sorted(
        data.get("option_contracts") or [],
        key=lambda x: abs(float(x["strike_price"]) - target_strike)
    )

    for c in contracts:
        sym = c["symbol"]
        if sym in state["positions"]:
            continue

        q = get_option_quote(sym)
        if not q:
            continue
        bid, ask = q
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        
        if spread_pct <= MAX_SPREAD_PCT and MIN_CONTRACT_PRICE <= ask <= MAX_CONTRACT_PRICE:
            return sym, ask

    return None, None

def open_position_fast(direction, underlying_price):
    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return

    contract, ask = choose_contract_fast(direction, underlying_price)
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
        "stop": ask * 0.92, # وقف خسارة أولي صارم 8%
        "entry_time": iso_now(),
    }
    state["positions"][contract] = pos
    save_state()
    log.info("⚡ OPENED POSITION [%s] | Qty: %d | Ask: $%.2f", contract, qty, ask)

# ---------------------------------------------------------
# 8. إدارة كل الصفقات المفتوحة (هدف ربح ثابت + تأمين نقطة التعادل)
# ---------------------------------------------------------
def manage_all_positions(current_underlying_price):
    active_contracts = list(state["positions"].keys())
    if not active_contracts:
        return

    for contract in active_contracts:
        pos = state["positions"].get(contract)
        if not pos:
            continue

        q = get_option_quote(contract)
        if not q:
            continue
            
        bid = q[0] # سعر البيع اللحظي الفعلي
        entry = pos["entry"]
        qty = pos["qty"]
        dollar_pnl = (bid - entry) * 100 * qty - (qty * 1.30) # حساب صافي الربح بالدولار بعد العمولات

        # 1. تحديث أعلى سعر (قمة) وصل له العقد
        if bid > pos.get("peak_option_price", entry):
            pos["peak_option_price"] = bid

        peak_price = pos["peak_option_price"]

        # 2. هدف ربح ثابت ومجزي: الخروج المباشر والبيع فور تحقيق ربح $20 أو أكثر
        if dollar_pnl >= 20.0:
            log.info("💰 TARGET PROFIT REACHED [%s]! Net PnL: $%.2f. Selling NOW!", contract, dollar_pnl)
            close_position_fast(pos, bid, "TARGET_PROFIT_20")
            continue

        # 3. درع التعادل السريع: إذا وصل الصافي إلى $5، ارفع الوقف فوراً لسعر الدخول + العمولات
        if dollar_pnl >= 5.0 and pos["stop"] < (entry + 0.02):
            pos["stop"] = entry + 0.02
            log.info("🛡️ BREAKEVEN LOCKED [%s]: Stop moved to Entry + Fees", contract)

        # 4. جني الأرباح عند الارتداد من القمة (Trailing Reversal)
        drop_from_peak_pct = (peak_price - bid) / peak_price if peak_price > 0 else 0
        if peak_price > (entry * 1.05) and drop_from_peak_pct >= 0.08:
            log.info("⚡ REVERSAL DETECTED [%s]! Dropped 8%% from peak. Selling NOW!", contract)
            close_position_fast(pos, bid, "REVERSAL_PROFIT_LOCK")
            continue

        # 5. الخروج المباشر عند ضرب وقف الخسارة أو نهاية الجلسة
        if bid <= pos["stop"] or now_ny().time() >= EOD_CLOSE:
            close_position_fast(pos, bid, "STOP_OR_EOD_EXIT")

def close_position_fast(pos, exit_price, reason):
    contract = pos["contract"]
    api_post(f"{TRADING_URL}/v2/orders", {
        "symbol": contract,
        "qty": str(pos["qty"]),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    })

    net_pnl = (exit_price - pos["entry"]) * 100 * pos["qty"] - (pos["qty"] * 1.30)
    state["virtual_capital"] += net_pnl
    state["net_pnl"] += net_pnl
    if net_pnl > 0: state["wins"] += 1

    log.info("🏁 CLOSED POSITION [%s] | Reason: %s | Net PnL: $%.2f", contract, reason, net_pnl)
    if contract in state["positions"]:
        del state["positions"][contract]
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
                
                # 1. إدارة جميع الصفقات المفتوحة وتأمينها
                manage_all_positions(last_price)

                # 2. البحث عن صفقات جديدة إذا لم نتجاوز الحد الأقصى (3 صفقات)
                if SIGNAL_START <= now_ny().time() <= SIGNAL_END:
                    if len(state["positions"]) < MAX_OPEN_POSITIONS:
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
