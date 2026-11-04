# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v91.0 (PROTECTED MOMENTUM ENGINE)
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
    return "Safaa Quant Protected Momentum Active! (v91.0)", 200

def start_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

Thread(target=start_flask, daemon=True).start()

# ---------------------------------------------------------
# 2. الإعدادات
# ---------------------------------------------------------
VERSION = "v91.0-Protected-Momentum"
SYMBOL = "SPY"
NY = ZoneInfo("America/New_York")

def env_first(*names, default=""):
    for name in names:
        val = os.getenv(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    return default

VIRTUAL_CAPITAL = 1000.0
MAX_PREMIUM_PCT = 0.20          # تخصيص 20% لكل صفقة
MAX_OPEN_POSITIONS = 2          # التركيز على صفقتين كحد أقصى لدقة المتابعة

LOOP_INTERVAL_SEC = 0.5         # سرعة فحص ممتازة (نصف ثانية)
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
TRADING_URL = env_first("ALPACA_TRADING_URL", default="https://paper-api.alpaca.markets").rstrip("/")
DATA_URL = env_first("ALPACA_DATA_URL", default="https://data.alpaca.markets").rstrip("/")
STOCK_FEED = "iex"
OPTIONS_FEED = "indicative"

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "safaa_v91_state.json"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

session = requests.Session()
session.headers.update(HEADERS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("SAFAA_MOMENTUM")

def now_ny(): return datetime.now(NY)
def today(): return now_ny().date().isoformat()
def iso_now(): return now_ny().isoformat(timespec="seconds")

# ---------------------------------------------------------
# 3. محرك الاتصال الآمن
# ---------------------------------------------------------
def safe_api_call(method, url, params=None, json_body=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            if method == "GET": r = session.get(url, params=params, timeout=4)
            elif method == "POST": r = session.post(url, json=json_body, timeout=4)
            elif method == "DELETE": r = session.delete(url, timeout=5)

            if r.status_code == 429:  
                time.sleep(2)
                continue

            return r.ok, (r.json() if r.content else {}), r.status_code
        except Exception as e:
            log.error("API Error: %s", e)
            time.sleep(1)
    return False, {}, 429

def api_get(url, params=None): return safe_api_call("GET", url, params=params)[:2]
def api_post(url, body): return safe_api_call("POST", url, json_body=body)
def api_delete(url): return safe_api_call("DELETE", url)

# ---------------------------------------------------------
# 4. إدارة الحالة
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
            if "positions" not in st: st["positions"] = {}
            return st
    except Exception: return {}

def new_state():
    return {"date": today(), "virtual_capital": VIRTUAL_CAPITAL, "net_pnl": 0.0, "trades": 0, "wins": 0, "positions": {}}

state = load_state()

# ---------------------------------------------------------
# 5. تنظيف الصفقات القديمة
# ---------------------------------------------------------
def close_all_old_positions():
    log.info("🧹 FLUSHING OLD POSITIONS...")
    api_delete(f"{TRADING_URL}/v2/orders")
    ok, positions = api_get(f"{TRADING_URL}/v2/positions")
    if ok and isinstance(positions, list):
        for pos in positions:
            api_post(f"{TRADING_URL}/v2/orders", {
                "symbol": pos.get("symbol"), "qty": pos.get("qty"), "side": "sell", "type": "market", "time_in_force": "day"
            })
    api_delete(f"{TRADING_URL}/v2/positions")
    state["positions"] = {}
    save_state()

# ---------------------------------------------------------
# 6. جلب بيانات السوق
# ---------------------------------------------------------
def get_stock_bars(symbol, start, end):
    params = {"timeframe": "1Min", "start": start.isoformat(), "end": end.isoformat(), "limit": 100, "feed": STOCK_FEED}
    ok, data = api_get(f"{DATA_URL}/v2/stocks/{symbol}/bars", params=params)
    return data.get("bars") or [] if ok and isinstance(data, dict) else []

def session_bars():
    n = now_ny()
    start = n.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    return get_stock_bars(SYMBOL, start, n.astimezone(ZoneInfo("UTC")))

def get_option_quote(symbol):
    ok, data = api_get(f"{DATA_URL}/v1beta1/options/quotes/latest", params={"symbols": symbol, "feed": OPTIONS_FEED})
    if not ok or not isinstance(data, dict): return None
    q = (data.get("quotes") or {}).get(symbol)
    if not q: return None
    bid, ask = float(q.get("bp", 0)), float(q.get("ap", 0))
    return (bid, ask) if bid > 0 and ask > 0 else None

# ---------------------------------------------------------
# 7. فتح الصفقات
# ---------------------------------------------------------
def open_position_fast(direction, underlying_price):
    if len(state["positions"]) >= MAX_OPEN_POSITIONS: return
    
    target_strike = underlying_price + 0.50 if direction == "up" else underlying_price - 0.50
    params = {
        "underlying_symbols": SYMBOL, "status": "active", "type": "call" if direction == "up" else "put",
        "expiration_date": today(), "limit": 10,
        "strike_price_gte": f"{target_strike - 1.00:.2f}", "strike_price_lte": f"{target_strike + 1.00:.2f}",
    }
    
    ok, data = api_get(f"{TRADING_URL}/v2/options/contracts", params=params)
    if not ok: return

    contracts = sorted(data.get("option_contracts") or [], key=lambda x: abs(float(x["strike_price"]) - target_strike))
    for c in contracts:
        sym = c["symbol"]
        if sym in state["positions"]: continue
        q = get_option_quote(sym)
        if not q: continue
        bid, ask = q
        
        if MIN_CONTRACT_PRICE <= ask <= MAX_CONTRACT_PRICE:
            max_alloc = state.get("virtual_capital", VIRTUAL_CAPITAL) * MAX_PREMIUM_PCT
            qty = max(1, int(max_alloc // (ask * 100)))

            st, _, _ = api_post(f"{TRADING_URL}/v2/orders", {
                "symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day"
            })
            if st:
                state["positions"][sym] = {
                    "contract": sym, "qty": qty, "entry": ask, "peak": ask, 
                    "stop": ask - 0.10 # وقف أولي: خسارة 10 سنت فقط للعقد
                }
                save_state()
                log.info("⚡ OPENED [%s] | Qty: %d | Entry: $%.2f", sym, qty, ask)
                return

# ---------------------------------------------------------
# 8. محرك الإدارة (مراقبة السعر الفردي للعقد)
# ---------------------------------------------------------
def manage_all_positions():
    for contract in list(state["positions"].keys()):
        pos = state["positions"].get(contract)
        q = get_option_quote(contract)
        if not q: continue
            
        bid = q[0] # سعر البيع اللحظي المتاح في السوق الآن
        entry = pos["entry"]
        
        # 1. تحديث القمة
        if bid > pos.get("peak", entry):
            pos["peak"] = bid

        peak = pos["peak"]

        # 2. الدرع الحديدي (تأمين التعادل + رسوم العمولات)
        # إذا ارتفع العقد 5 سنتات ($0.05)، نرفع الوقف لسعر الدخول + 2 سنت (عمولات)
        if bid >= (entry + 0.05) and pos["stop"] < (entry + 0.02):
            pos["stop"] = entry + 0.02
            log.info("🛡️ IRON SHIELD ON [%s]: Stop locked at $%.2f (Zero Risk)", contract, pos["stop"])

        # 3. الهدف الحقيقي (البيع الفوري عند مكسب 20 سنت للعقد)
        if bid >= (entry + 0.20):
            log.info("💰 TARGET HIT [%s]! Selling at $%.2f", contract, bid)
            close_position(pos, bid, "REAL_PROFIT_20CENTS")
            continue

        # 4. تتبع الأرباح (Trailing Stop): إذا ارتفع عالياً ثم هبط بمقدار 15% من القمة
        if peak >= (entry + 0.10): # تفعل فقط بعد ربح 10 سنتات
            drop_pct = (peak - bid) / peak
            if drop_pct >= 0.15:
                log.info("⚡ TRAILING STOP [%s]! Secured profit at $%.2f", contract, bid)
                close_position(pos, bid, "TRAILING_PROFIT")
                continue

        # 5. وقف الخسارة أو نهاية اليوم
        if bid <= pos["stop"] or now_ny().time() >= EOD_CLOSE:
            close_position(pos, bid, "STOP_OR_EOD")

def close_position(pos, exit_price, reason):
    contract = pos["contract"]
    api_post(f"{TRADING_URL}/v2/orders", {
        "symbol": contract, "qty": str(pos["qty"]), "side": "sell", "type": "market", "time_in_force": "day"
    })
    
    net_pnl = (exit_price - pos["entry"]) * 100 * pos["qty"] - (pos["qty"] * 1.30)
    state["virtual_capital"] += net_pnl
    state["net_pnl"] += net_pnl
    
    log.info("🏁 CLOSED [%s] | Reason: %s | Net PnL: $%.2f", contract, reason, net_pnl)
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

    close_all_old_positions()

    while True:
        try:
            ok, clock = api_get(f"{TRADING_URL}/v2/clock")
            if not ok or not (clock or {}).get("is_open"):
                time.sleep(10)
                continue

            manage_all_positions()

            bars = session_bars()
            if bars and SIGNAL_START <= now_ny().time() <= SIGNAL_END:
                if len(state["positions"]) < MAX_OPEN_POSITIONS:
                    last_price = float(bars[-1]["c"])
                    pv = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0 * float(b["v"]) for b in bars)
                    vol = sum(float(b["v"]) for b in bars)
                    vwap = pv / vol if vol > 0 else last_price

                    if last_price > vwap * 1.0006:  # دخول أقوى للزخم
                        open_position_fast("up", last_price)
                    elif last_price < vwap * 0.9994:
                        open_position_fast("down", last_price)

            time.sleep(LOOP_INTERVAL_SEC)
        except Exception as e:
            log.exception("Loop Error: %s", e)
            time.sleep(1)

if __name__ == "__main__":
    run()
