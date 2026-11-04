# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v100.0 (APEX QUANT ENGINE)
Platform: Alpaca Paper / Live
Architecture: Execution Integrity, Advanced Momentum, Risk Guard
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
# 1. خادم الويب
# ---------------------------------------------------------
app = Flask(__name__)
@app.route('/')
def health_check(): return "Safaa Quant APEX Engine Active! (v100.0)", 200
def start_flask(): app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
Thread(target=start_flask, daemon=True).start()

# ---------------------------------------------------------
# 2. الإعدادات
# ---------------------------------------------------------
VERSION = "v100.0-Apex"
SYMBOL = "SPY"
NY = ZoneInfo("America/New_York")

def env_first(*names, default=""):
    for name in names:
        val = os.getenv(name)
        if val is not None and str(val).strip(): return str(val).strip()
    return default

VIRTUAL_CAPITAL = 1000.0
MAX_PREMIUM_PCT = 0.15          
MAX_OPEN_POSITIONS = 2          
MAX_DAILY_DRAWDOWN_PCT = 0.15   
COOLDOWN_MINUTES = 5            # فترة التبريد لمنع الدخول المتكرر

LOOP_INTERVAL_SEC = 1.0         
MAX_SPREAD_PCT = 0.05           
MIN_CONTRACT_PRICE = 0.25       
MAX_CONTRACT_PRICE = 2.00       

SIGNAL_START = dtime(9, 35)     
SIGNAL_END = dtime(15, 30)
EOD_CLOSE = dtime(15, 45)

API_KEY = env_first("ALPACA_API_KEY", "APCA_API_KEY_ID")
API_SECRET = env_first("ALPACA_API_SECRET", "APCA_API_SECRET_KEY")
TRADING_URL = env_first("ALPACA_TRADING_URL", default="https://paper-api.alpaca.markets").rstrip("/")
DATA_URL = env_first("ALPACA_DATA_URL", default="https://data.alpaca.markets").rstrip("/")

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "safaa_v100_state.json"

HEADERS = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET, "Content-Type": "application/json"}
session = requests.Session()
session.headers.update(HEADERS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("SAFAA_APEX")

def now_ny(): return datetime.now(NY)

# ---------------------------------------------------------
# 3. محرك الاتصال والتنفيذ المؤكد
# ---------------------------------------------------------
def safe_api_call(method, url, params=None, json_body=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            if method == "GET": r = session.get(url, params=params, timeout=5)
            elif method == "POST": r = session.post(url, json=json_body, timeout=5)
            elif method == "DELETE": r = session.delete(url, timeout=5)
            if r.status_code == 429:  
                time.sleep(2)
                continue
            return r.ok, (r.json() if r.content else {}), r.status_code
        except Exception as e:
            time.sleep(1)
    return False, {}, 429

def api_get(url, params=None): return safe_api_call("GET", url, params=params)[:2]
def api_post(url, body): return safe_api_call("POST", url, json_body=body)[:2]
def api_delete(url): return safe_api_call("DELETE", url)[:2]

def get_order_status(order_id):
    ok, data = api_get(f"{TRADING_URL}/v2/orders/{order_id}")
    return data if ok else None

def wait_for_fill(order_id, timeout_sec=10):
    """ يعالج التنفيذ الجزئي بصرامة ويلغي المتبقي """
    start = time.time()
    while time.time() - start < timeout_sec:
        order = get_order_status(order_id)
        if order:
            status = order.get("status")
            filled_qty = int(float(order.get("filled_qty", 0)))
            avg_price = float(order.get("filled_avg_price", 0))
            if status == "filled": return True, avg_price, filled_qty
            if status in ["canceled", "rejected"]: return False, 0.0, 0
        time.sleep(0.5)
    
    # لم يتنفذ بالكامل، إلغاء فوري
    api_delete(f"{TRADING_URL}/v2/orders/{order_id}")
    time.sleep(1) # انتظار تأكيد الإلغاء من المقاصة
    order = get_order_status(order_id)
    if order:
        filled_qty = int(float(order.get("filled_qty", 0)))
        avg_price = float(order.get("filled_avg_price", 0))
        if filled_qty > 0: return True, avg_price, filled_qty
    return False, 0.0, 0

# ---------------------------------------------------------
# 4. إدارة الحالة واستعادة الأعطال
# ---------------------------------------------------------
def save_state():
    tmp = Path(str(STATE_FILE) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
            if "positions" not in st: st["positions"] = {}
            if st.get("date") != now_ny().date().isoformat():
                st["date"] = now_ny().date().isoformat()
                st["daily_start_capital"] = st.get("virtual_capital", VIRTUAL_CAPITAL)
                st["last_trade_time"] = 0
            return st
    except Exception: return {}

def new_state():
    return {"date": now_ny().date().isoformat(), "virtual_capital": VIRTUAL_CAPITAL, "daily_start_capital": VIRTUAL_CAPITAL, "net_pnl": 0.0, "positions": {}, "halted_today": False, "last_trade_time": 0}

state = load_state() if load_state() else new_state()

def sync_with_broker():
    ok, real_positions = api_get(f"{TRADING_URL}/v2/positions")
    if not ok: return
    
    real_symbols = {p["symbol"]: (int(float(p["qty"])), float(p["avg_entry_price"])) for p in real_positions if p.get("asset_class") == "us_option"}
    
    # 1. الاستعادة السليمة (بدون أرباح وهمية)
    for sym, (qty, avg_price) in real_symbols.items():
        if sym not in state["positions"]:
            log.warning("🔄 RECOVERY: Adopting %s at real entry $%.2f", sym, avg_price)
            state["positions"][sym] = {"contract": sym, "qty": qty, "entry": avg_price, "peak": avg_price, "stop": avg_price * 0.8, "adopted": True}
    
    # 2. الحذف الآمن
    for sym in list(state["positions"].keys()):
        if sym not in real_symbols:
            del state["positions"][sym]
    save_state()

# ---------------------------------------------------------
# 5. البيانات الرياضية والمؤشرات الفنية
# ---------------------------------------------------------
def calc_ema(prices, period):
    if len(prices) < period: return prices[-1] if prices else 0
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return 50
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100
    return 100 - (100 / (1 + (avg_gain / avg_loss)))

def get_market_data():
    # تصحيح التوقيت
    start_ny = now_ny().replace(hour=9, minute=30, second=0, microsecond=0)
    start_utc = start_ny.astimezone(ZoneInfo("UTC")).isoformat()
    end_utc = now_ny().astimezone(ZoneInfo("UTC")).isoformat()
    
    ok, data = api_get(f"{DATA_URL}/v2/stocks/{SYMBOL}/bars", params={"timeframe": "1Min", "start": start_utc, "end": end_utc, "limit": 100, "feed": "iex"})
    return data.get("bars") or [] if ok and isinstance(data, dict) else []

def get_option_quote(symbol):
    # في الحسابات الحقيقية يجب استخدام SIP/OPRA. Indicative هي للمحاكاة وتتطلب فحص Spread.
    ok, data = api_get(f"{DATA_URL}/v1beta1/options/quotes/latest", params={"symbols": symbol, "feed": "indicative"})
    q = (data.get("quotes") or {}).get(symbol) if ok and isinstance(data, dict) else None
    if not q: return None
    return float(q.get("bp", 0)), float(q.get("ap", 0))

# ---------------------------------------------------------
# 6. اختيار العقود والتنفيذ
# ---------------------------------------------------------
def open_position(direction, last_price):
    if len(state["positions"]) >= MAX_OPEN_POSITIONS: return
    if time.time() - state.get("last_trade_time", 0) < (COOLDOWN_MINUTES * 60): return # منع التكرار
    
    target_strike = last_price + 0.50 if direction == "up" else last_price - 0.50
    params = {
        "underlying_symbols": SYMBOL, "status": "active", "type": "call" if direction == "up" else "put",
        "expiration_date": now_ny().date().isoformat(), "limit": 20,
    }
    
    ok, data = api_get(f"{TRADING_URL}/v2/options/contracts", params=params)
    contracts = sorted(data.get("option_contracts") or [], key=lambda x: abs(float(x["strike_price"]) - target_strike))
    
    for c in contracts:
        sym = c["symbol"]
        q = get_option_quote(sym)
        if not q: continue
        bid, ask = q
        
        spread_pct = (ask - bid) / ask if ask > 0 else 1.0
        if spread_pct <= MAX_SPREAD_PCT and MIN_CONTRACT_PRICE <= ask <= MAX_CONTRACT_PRICE:
            # حارس رأس المال
            max_budget = state["virtual_capital"] * MAX_PREMIUM_PCT
            qty = int(max_budget // (ask * 100))
            if qty < 1: continue # السعر يتجاوز 15% من رأس المال، تجاهل

            ok_post, order = api_post(f"{TRADING_URL}/v2/orders", {
                "symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day"
            })
            
            if ok_post and order.get("id"):
                filled, avg_price, filled_qty = wait_for_fill(order["id"])
                if filled and filled_qty > 0:
                    state["positions"][sym] = {
                        "contract": sym, "qty": filled_qty, "entry": avg_price, "peak": avg_price, "stop": avg_price - 0.12
                    }
                    state["last_trade_time"] = time.time()
                    save_state()
                    log.info("✅ FILLED [%s] | Qty: %d | Avg Entry: $%.2f", sym, filled_qty, avg_price)
                return

# ---------------------------------------------------------
# 7. الحماية والإغلاق (Position Guardian)
# ---------------------------------------------------------
def close_position(pos, reason):
    sym = pos["contract"]
    log.info("🚨 EXIT SIGNAL [%s] | %s", sym, reason)
    ok, order = api_post(f"{TRADING_URL}/v2/orders", {
        "symbol": sym, "qty": str(pos["qty"]), "side": "sell", "type": "market", "time_in_force": "day"
    })
    
    if ok and order.get("id"):
        filled, avg_price, filled_qty = wait_for_fill(order["id"])
        if filled and filled_qty > 0:
            # تحديث الكمية إذا كان التنفيذ جزئياً قبل الحذف
            entry = pos["entry"]
            net_pnl = (avg_price - entry) * 100 * filled_qty - (filled_qty * 1.30)
            state["virtual_capital"] += net_pnl
            state["net_pnl"] += net_pnl
            log.info("🏁 CLOSED [%s] | Qty: %d | Net PnL: $%.2f", sym, filled_qty, net_pnl)
            
            pos["qty"] -= filled_qty
            if pos["qty"] <= 0:
                del state["positions"][sym]
            save_state()

def manage_all_positions():
    for sym in list(state["positions"].keys()):
        pos = state["positions"].get(sym)
        q = get_option_quote(sym)
        if not q: continue
        bid = q[0]
        
        entry = pos["entry"]
        if bid > pos.get("peak", entry): pos["peak"] = bid
        peak = pos["peak"]

        # 1. درع التعادل
        if bid >= (entry + 0.05) and pos["stop"] < (entry + 0.02):
            pos["stop"] = entry + 0.02
        
        # 2. هدف الربح الحقيقي
        if bid >= (entry + 0.25):
            close_position(pos, "TARGET_PROFIT_25")
            continue
            
        # 3. الوقف المتحرك
        if peak >= (entry + 0.10) and (peak - bid) / peak >= 0.15:
            close_position(pos, "TRAILING_REVERSAL")
            continue

        # 4. وقف الخسارة أو نهاية اليوم
        if bid <= pos["stop"] or now_ny().time() >= EOD_CLOSE:
            close_position(pos, "STOP_OR_EOD")

# ---------------------------------------------------------
# 8. الحلقة الرئيسية
# ---------------------------------------------------------
def run():
    log.info("⚙️ APEX QUANT ENGINE %s STARTED", VERSION)
    sync_with_broker()
    last_sync = time.time()
    
    while True:
        try:
            # 1. إدارة الصفقات المفتوحة أولاً لحمايتها دائماً
            manage_all_positions()

            # 2. فحص الخسارة اليومية (يوقف الدخول الجديد فقط)
            current_cap = state.get("virtual_capital", VIRTUAL_CAPITAL)
            start_cap = state.get("daily_start_capital", VIRTUAL_CAPITAL)
            drawdown = (start_cap - current_cap) / start_cap if start_cap > 0 else 0
            if drawdown >= MAX_DAILY_DRAWDOWN_PCT:
                if not state.get("halted_today"):
                    log.error("🛑 DAILY LIMIT REACHED! Halt new entries.")
                    state["halted_today"] = True
                    save_state()
                time.sleep(60)
                continue

            if time.time() - last_sync > 120:
                sync_with_broker()
                last_sync = time.time()

            ok, clock = api_get(f"{TRADING_URL}/v2/clock")
            if not ok or not (clock or {}).get("is_open"):
                time.sleep(10)
                continue

            # 3. محرك الإشارات
            bars = get_market_data()
            if len(bars) > 25 and SIGNAL_START <= now_ny().time() <= SIGNAL_END:
                closes = [float(b["c"]) for b in bars]
                last_price = closes[-1]
                last_vol = float(bars[-1]["v"])
                
                pv = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0 * float(b["v"]) for b in bars)
                vol = sum(float(b["v"]) for b in bars)
                vwap = pv / vol if vol > 0 else last_price
                avg_vol = vol / len(bars)
                
                ema9 = calc_ema(closes, 9)
                ema21 = calc_ema(closes, 21)
                rsi = calc_rsi(closes)

                # التأكيد: اختراق VWAP + تقاطع EMA + زخم معتدل + فوليوم عالي
                if last_price > vwap and ema9 > ema21 and 50 < rsi < 70 and last_vol > (avg_vol * 1.5):
                    open_position("up", last_price)
                elif last_price < vwap and ema9 < ema21 and 30 < rsi < 50 and last_vol > (avg_vol * 1.5):
                    open_position("down", last_price)

            time.sleep(LOOP_INTERVAL_SEC)
        except Exception as e:
            log.error("Engine Error: %s", e)
            time.sleep(1)

if __name__ == "__main__":
    run()
