# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v85.0 (PROTECTION & DEEP OTM SNIPE EDITION)
Platform: Alpaca Paper Trading
Deployment: Railway (Includes Flask Keep-Alive Port)
=========================================================
"""

import os
import csv
import json
import time
import logging
import uuid
from pathlib import Path
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
import requests
from threading import Thread
from flask import Flask

# ---------------------------------------------------------
# 1. خادم الويب لإبقاء الحاوية حية على Railway (Keep-Alive)
# ---------------------------------------------------------
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Safaa Quant Compound Bot v85.0 is Active!", 200

def start_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

Thread(target=start_flask, daemon=True).start()

# ---------------------------------------------------------
# 2. الإعدادات والمتغيرات الأساسية
# ---------------------------------------------------------
VERSION = "v85.0-ProfitProtect"
SYMBOL = "SPY"
NY = ZoneInfo("America/New_York")

def env_float(name, default):
    return float(os.getenv(name, default))

def env_int(name, default):
    return int(os.getenv(name, default))

def env_first(*names, default=""):
    for name in names:
        val = os.getenv(name)
        if val is not None and str(val).strip():
            return str(val).strip()
    return default

# ---------------------------------------------------------
# الشرط الثالث: تقييد رأس المال بـ 1000 دولار
# ---------------------------------------------------------
VIRTUAL_CAPITAL = 1000.0  # الاعتماد على 1000 دولار كقاعدة أساسية لرأس المال

# إعدادات إدارة المخاطر والفائدة المركبة
RISK_PER_TRADE_PCT = env_float("RISK_PER_TRADE_PCT", "0.02")   # المخاطرة بـ 2% من 1000$ (20$)
MAX_PREMIUM_PCT = env_float("MAX_PREMIUM_PCT", "0.20")         # أقصى تخصيص للصفقة 20% من رأس المال (200$)
ATR_MULTIPLIER = env_float("ATR_MULTIPLIER", "1.5")
COMMISSION_PER_CONTRACT = 0.65

# الشرط الأول: قواعد حماية الأرباح (Profit Protection)
UNDERLYING_TRAIL_DIST = 0.35   # إذا تراجع المؤشر 0.35$ عن أعلى قمة وصل لها يخرج لحماية الربح
BREAKEVEN_TRIGGER = 0.15       # نقل الوقف لنقطة الدخول عند ربح 15% على العقد
OPTION_TRAIL_ACTIVATION = 0.30 # تفعيل الوقف المتحرك للعقد عند ربح 30%
OPTION_TRAIL_DISTANCE = 0.10   # تراجع 10% من أوج سعر للعقد يُغلق الصفقة

# الشرط الثاني: إعدادات العقود الرخيصة بعيدة السترايك (Deep OTM High Confidence)
DEEP_OTM_MIN_PRICE = 0.15      # الحد الأدنى لسعر العقد البعيد
DEEP_OTM_MAX_PRICE = 0.40      # الحد الأقصى لسعر العقد البعيد
QUICK_PROFIT_TARGET_USD = 0.50 # جني أرباح سريع إذا ارتفع العقد 0.50$ (مثال: من 0.20 إلى 0.70)

MAX_SPREAD_PCT = 0.08          # أقصى فرق مقبول بين العرض والطلب (8%)
DELTA_PROXY_PCT = 0.0025
LIMIT_TIMEOUT = 3

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
STATE_FILE = DATA_DIR / "safaa_v85_state.json"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("SAFAA_QUANT")

def now_ny(): return datetime.now(NY)
def today(): return now_ny().date().isoformat()
def iso_now(): return now_ny().isoformat(timespec="seconds")

# ---------------------------------------------------------
# 3. محرك الاتصال بـ API
# ---------------------------------------------------------
def api_get(url, params=None, retries=3):
    backoff = 0.3
    for _ in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=8)
            data = r.json() if r.content else {}
            if r.ok:
                return True, data
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff)
                backoff *= 1.5
                continue
            return False, {"status_code": r.status_code, "body": data}
        except Exception as e:
            time.sleep(backoff)
            backoff *= 1.5
    return False, {"error": "request_failed"}

def api_post(url, body):
    try:
        r = requests.post(url, headers=HEADERS, json=body, timeout=8)
        data = r.json() if r.content else {}
        return r.ok, data, r.status_code
    except Exception as e:
        return False, {"error": str(e)}, None

# ---------------------------------------------------------
# 4. حفظ واسترجاع الحالة العامة
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
        "halted": False,
        "position": None,
    }

state = load_state()

# ---------------------------------------------------------
# 5. جلب بيانات أسعار المؤشر والشموع
# ---------------------------------------------------------
def get_stock_bars(symbol, start, end, timeframe="1Min"):
    bars = []
    token = None
    for _ in range(4):
        params = {
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 1000,
            "feed": STOCK_FEED,
        }
        if token:
            params["page_token"] = token
            
        ok, data = api_get(f"{DATA_URL}/v2/stocks/{symbol}/bars", params=params)
        if not ok or not data or not isinstance(data, dict):
            break
            
        fetched_bars = data.get("bars")
        if fetched_bars and isinstance(fetched_bars, list):
            bars.extend(fetched_bars)
            
        token = data.get("next_page_token")
        if not token:
            break
    return bars

def session_bars():
    n = now_ny()
    start = n.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    end = n.astimezone(ZoneInfo("UTC"))
    return get_stock_bars(SYMBOL, start, end)

# ---------------------------------------------------------
# 6. حساب حجم الصفقة بناءً على 1000 دولار (الشرط الثالث)
# ---------------------------------------------------------
def calculate_position_size(ask_price):
    capital = state.get("virtual_capital", VIRTUAL_CAPITAL)
    
    # 1. تخصيص مبلغ بناءً على نسبة 20% كحد أقصى للصفقة الواحدة
    max_alloc_dollars = capital * MAX_PREMIUM_PCT
    
    # 2. عدد العقود التي يمكن شراؤها بهذه الميزانية
    qty = int(max_alloc_dollars // (ask_price * 100))
    
    log.info("📊 CAPITAL ALLOCATION | Capital: $%.2f | Max Trade Alloc: $%.2f | Qty: %d", 
             capital, max_alloc_dollars, qty)
    return max(1, qty)

# ---------------------------------------------------------
# 7. محرك اختيار العقود (الشرط الثاني: دعم العقود البعيدة الرخيصة)
# ---------------------------------------------------------
def get_option_quote(symbol):
    ok, data = api_get(f"{DATA_URL}/v1beta1/options/quotes/latest", params={"symbols": symbol, "feed": OPTIONS_FEED})
    if not ok or not isinstance(data, dict):
        return None
    q = (data.get("quotes") or {}).get(symbol)
    if not q:
        return None
    bid, ask = float(q.get("bp", 0)), float(q.get("ap", 0))
    if bid > 0 and ask > 0 and ask > bid:
        return (bid, ask)
    return None

def choose_contract(direction, underlying_price, high_confidence=False):
    """
    اختيار العقود:
    - إذا كانت الثقة عالية (high_confidence): يختار عقود بعيدة (Deep OTM) وأسعارها رخيصة (0.15$ - 0.40$).
    - الإعداد العادي: يختار عقود قريبة من السعر (ATM / Near OTM).
    """
    opt_type = "call" if direction == "up" else "put"
    
    if high_confidence:
        # البحث عن سترايك أبعد بمقدار 1.5% إلى 3% لرخص السعر
        offset = underlying_price * 0.02
        target_strike = underlying_price + offset if direction == "up" else underlying_price - offset
        log.info("🎯 DEEP OTM MODE ACTIVATED | Target Strike around: $%.2f", target_strike)
    else:
        target_strike = underlying_price * (1 + DELTA_PROXY_PCT if direction == "up" else 1 - DELTA_PROXY_PCT)
    
    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "type": opt_type,
        "expiration_date": today(),
        "limit": 25,
        "strike_price_gte": f"{target_strike - 5:.2f}",
        "strike_price_lte": f"{target_strike + 5:.2f}",
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
        
        if high_confidence:
            # تصفية العقود الرخيصة البعيدة
            if DEEP_OTM_MIN_PRICE <= ask <= DEEP_OTM_MAX_PRICE and spread_pct <= MAX_SPREAD_PCT:
                return sym, ask
        else:
            if ask >= 0.30 and spread_pct <= MAX_SPREAD_PCT:
                return sym, ask

    return None, None

# ---------------------------------------------------------
# 8. إدارة الصفقة وحماية الأرباح (الشرط الأول)
# ---------------------------------------------------------
def open_position(direction, underlying_price, high_confidence=False):
    if state.get("position") or state.get("halted"):
        return

    contract, ask = choose_contract(direction, underlying_price, high_confidence)
    if not contract or not ask:
        log.warning("⚠️ No suitable contract found matching current criteria.")
        return

    qty = calculate_position_size(ask)
    if qty < 1:
        return

    client_id = f"cmp-{uuid.uuid4().hex[:14]}"
    st, data = api_post(f"{TRADING_URL}/v2/orders", {
        "symbol": contract,
        "qty": str(qty),
        "side": "buy",
        "type": "limit",
        "limit_price": f"{ask:.2f}",
        "time_in_force": "day",
        "client_order_id": client_id,
    })

    if not st:
        log.error("Order submission failed: %s", data)
        return

    pos = {
        "contract": contract,
        "direction": direction,
        "qty": qty,
        "entry": ask,
        "peak_option_price": ask,
        "entry_underlying": underlying_price,
        "peak_underlying": underlying_price,  # تتبع قمة المؤشر لحماية الأرباح
        "stop": ask * 0.75,
        "high_confidence": high_confidence,
        "entry_time": iso_now(),
    }
    state["position"] = pos
    save_state()
    log.info("🚀 POSITION OPENED | Contract: %s | Qty: %d | Entry Option: $%.2f | SPY Price: $%.2f",
             contract, qty, ask, underlying_price)

def manage_position(current_underlying_price):
    pos = state.get("position")
    if not pos:
        return

    q = get_option_quote(pos["contract"])
    if not q:
        return
    bid = q[0]

    direction = pos["direction"]
    
    # --- 1. تحديث أعلى قمة للمؤشر الأساسي SPY (الشرط الأول) ---
    if direction == "up":
        if current_underlying_price > pos["peak_underlying"]:
            pos["peak_underlying"] = current_underlying_price
    else:
        if current_underlying_price < pos["peak_underlying"]:
            pos["peak_underlying"] = current_underlying_price

    # --- 2. تحديث أوج سعر للعقد ---
    if bid > pos["peak_option_price"]:
        pos["peak_option_price"] = bid

    option_gain = bid - pos["entry"]
    gain_pct = option_gain / pos["entry"]

    # =========================================================
    # الشرط الأول: حماية الأرباح عند تراجع المؤشر قبل الوصول للسترايك
    # =========================================================
    underlying_pullback = (pos["peak_underlying"] - current_underlying_price) if direction == "up" else (current_underlying_price - pos["peak_underlying"])

    if underlying_pullback >= UNDERLYING_TRAIL_DIST and gain_pct > 0.10:
        log.info("🛡️ PROTECTING PROFIT: SPY Pulled back $%.2f from peak ($%.2f). Selling now to secure gains!",
                 underlying_pullback, pos["peak_underlying"])
        close_position(pos, bid, "UNDERLYING_PROFIT_PROTECTION")
        return

    # =========================================================
    # الشرط الثاني: بيع العقود البعيدة الرخيصة عند ارتفاعها بقيمة نص دولار
    # =========================================================
    if pos.get("high_confidence") and option_gain >= QUICK_PROFIT_TARGET_USD:
        log.info("🎯 QUICK PROFIT TARGET REACHED ($+0.50)! Exiting OTM Contract.")
        close_position(pos, bid, "QUICK_OTM_TARGET")
        return

    # --- 3. إدارة الوقف المتحرك النمطي للعقد ---
    if gain_pct >= BREAKEVEN_TRIGGER and pos["stop"] < pos["entry"]:
        pos["stop"] = pos["entry"] * 1.02
        log.info("🔒 BREAKEVEN SET | New Stop: $%.2f", pos["stop"])

    if (pos["peak_option_price"] - pos["entry"]) / pos["entry"] >= OPTION_TRAIL_ACTIVATION:
        new_stop = pos["peak_option_price"] * (1 - OPTION_TRAIL_DISTANCE)
        if new_stop > pos["stop"]:
            pos["stop"] = new_stop
            log.info("📈 OPTION TRAILING STOP UPDATED | New Stop: $%.2f", pos["stop"])

    # شرط الخروج بالوقف أو نهاية اليوم
    if bid <= pos["stop"] or now_ny().time() >= EOD_CLOSE:
        close_position(pos, bid, "STOP_OR_EOD")

def close_position(pos, exit_price, reason):
    qty = pos["qty"]
    api_post(f"{TRADING_URL}/v2/orders", {
        "symbol": pos["contract"],
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    })

    gross_pnl = (exit_price - pos["entry"]) * 100 * qty
    net_pnl = gross_pnl - (qty * COMMISSION_PER_CONTRACT * 2)

    # تحديث رأس المال الافتراضي (1000 دولار + الأرباح/الخسائر)
    state["virtual_capital"] += net_pnl
    state["net_pnl"] += net_pnl
    if net_pnl > 0:
        state["wins"] += 1

    log.info("🏁 POSITION CLOSED [%s] | Exit Option Price: $%.2f | Net PnL: $%.2f | New Virtual Capital: $%.2f",
             reason, exit_price, net_pnl, state["virtual_capital"])
    
    state["position"] = None
    save_state()

# ---------------------------------------------------------
# 9. محرك التحليل والتشغيل الرئيسي
# ---------------------------------------------------------
def run():
    log.info("⚡ SAFAA QUANT %s STARTED | Virtual Capital Fixed to $%.2f", VERSION, VIRTUAL_CAPITAL)

    if not state:
        state.update(new_state())
        save_state()

    while True:
        try:
            ok, clock = api_get(f"{TRADING_URL}/v2/clock")
            if not ok or not (clock or {}).get("is_open"):
                time.sleep(15)
                continue

            bars = session_bars()
            if bars:
                last_price = float(bars[-1]["c"])
                
                # إدارة الصفقة القائمة بمراقبة سعر المؤشر الحاضر SPY
                if state.get("position"):
                    manage_position(last_price)
                
                # البحث عن صفقات جديدة
                elif not state.get("halted") and SIGNAL_START <= now_ny().time() <= SIGNAL_END:
                    # حساب مؤشر الفواب (VWAP) والزخم
                    pv = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0 * float(b["v"]) for b in bars)
                    vol = sum(float(b["v"]) for b in bars)
                    vwap = pv / vol if vol > 0 else last_price

                    # تقييم قوة الثقة بالتحليل الكمي (High Confidence Detection)
                    momentum = (last_price - float(bars[0]["c"])) / float(bars[0]["c"])
                    high_confidence = abs(momentum) >= 0.004  # صعود/هبوط قوي بـ 0.4%+ في الجلسة

                    if last_price > vwap * 1.0015:
                        open_position("up", last_price, high_confidence=high_confidence)
                    elif last_price < vwap * 0.9985:
                        open_position("down", last_price, high_confidence=high_confidence)

            time.sleep(2) # فحص متواصل كل ثانيتين

        except Exception as e:
            log.exception("Error in main loop: %s", e)
            time.sleep(5)

if __name__ == "__main__":
    run()
