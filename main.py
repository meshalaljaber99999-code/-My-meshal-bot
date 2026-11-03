# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v84.0 (QUANT COMPOUND EDITION - SPY 0-DTE)
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
    return "Safaa Quant Compound Bot is Active and Running!", 200

def start_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# تشغيل خادم الويب في الخلفية لمنع Railway من إغلاق الحاوية
Thread(target=start_flask, daemon=True).start()

# ---------------------------------------------------------
# 2. الإعدادات والمتغيرات الأساسية
# ---------------------------------------------------------
VERSION = "v84.0-Compound"
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

# إعدادات الفائدة المركبة وإدارة المخاطر
RISK_PER_TRADE_PCT = env_float("RISK_PER_TRADE_PCT", "0.015")  # المخاطرة بـ 1.5% من رأس المال الحالي لكل صفقة
MAX_PREMIUM_PCT = env_float("MAX_PREMIUM_PCT", "0.10")        # أقصى مبلغ مخصص لعقد واحد 10% من رأس المال
DAILY_LOSS_LIMIT_PCT = env_float("DAILY_LOSS_LIMIT_PCT", "0.04") # إيقاف البوت اليومي عند خسارة 4% من رأس المال
ATR_MULTIPLIER = env_float("ATR_MULTIPLIER", "1.5")
COMMISSION_PER_CONTRACT = 0.65

LOSS_PAUSE_MINUTES = env_int("LOSS_PAUSE_MINUTES", 30)
MAX_CONSEC_LOSSES = env_int("MAX_CONSEC_LOSSES", 2)
ENTRY_COOLDOWN_MINUTES = env_int("ENTRY_COOLDOWN_MINUTES", 15)

BREAKEVEN_TRIGGER = 0.20        # نقل الوقف لنقطة الدخول عند ربح 20%
TRAIL_ACTIVATION = 0.35         # تفعيل الوقف المتحرك عند ربح 35%
TRAIL_DISTANCE = 0.12          # مسافة الوقف المتحرك 12%

MAX_SPREAD_PCT = 0.06           # أقصى فرق مقبول بين العرض والطلب (6%)
MIN_OPTION_ASK = 0.30
DELTA_PROXY_PCT = 0.0025
LIMIT_TIMEOUT = 3
CANCEL_WAIT = 5

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
STATE_FILE = DATA_DIR / "safaa_v84_state.json"
TRADES_FILE = DATA_DIR / "safaa_v84_journal.csv"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("SAFAA_QUANT")

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}
ARMED = {"new", "partially_filled"}
WORKING = ARMED | {"accepted", "held", "pending_new", "pending_cancel", "pending_replace"}

def now_ny(): return datetime.now(NY)
def today(): return now_ny().date().isoformat()
def iso_now(): return now_ny().isoformat(timespec="seconds")

# ---------------------------------------------------------
# 3. محرك الاتصال بـ API بالكامل مع حماية الـ NoneType
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

def api_delete(url):
    try:
        r = requests.delete(url, headers=HEADERS, timeout=8)
        return r.status_code in (200, 204), (r.json() if r.content else {})
    except Exception as e:
        return False, {"error": str(e)}

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

def new_state(equity):
    return {
        "date": today(),
        "start_equity": equity,
        "current_equity": equity,
        "net_pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "consec_losses": 0,
        "halted": False,
        "halt_reason": None,
        "pause_until": None,
        "last_entry_time": None,
        "last_signal_bar": None,
        "bias": "neutral",
        "position": None,
    }

state = load_state()

def halt(reason):
    state["halted"] = True
    state["halt_reason"] = reason
    log.error("🛑 HALT SYSTEM: %s", reason)
    save_state()

# ---------------------------------------------------------
# 5. جلب بيانات رأس المال والشموع بأمان تام
# ---------------------------------------------------------
def get_equity():
    ok, acc = api_get(f"{TRADING_URL}/v2/account")
    if ok and isinstance(acc, dict):
        try:
            eq = float(acc.get("equity") or 0)
            state["current_equity"] = eq
            save_state()
            return eq
        except Exception:
            pass
    return float(state.get("current_equity") or 0.0)

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
        
        # حماية من الانهيار في حال كانت الـ Data فارغة أو None
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
# 6. محرك حساب الفائدة المركبة المقترن برأس المال
# ---------------------------------------------------------
def calculate_compound_position_size(ask_price, stop_loss_pct):
    """
    حساب عدد العقود وفق مبدأ الفائدة المركبة:
    المخاطرة تتناسب طردياً مع النمو اللحظي لرأس المال الحالي.
    """
    equity = get_equity()
    if equity <= 0:
        return 0

    # 1. إجمالي المبلغ المسموح بالمخاطرة به في الصفقة بناءً على رأس المال الحالي
    max_risk_dollars = equity * RISK_PER_TRADE_PCT

    # 2. المخاطرة الفعلية لكل عقد واحد بروموز (سعر العقد * 100 * نسبة الوقف)
    risk_per_contract = (ask_price * 100) * stop_loss_pct
    if risk_per_contract <= 0:
        return 0

    # 3. حساب عدد العقود الممكن شراؤها بناءً على الفائدة المركبة
    compound_qty = int(max_risk_dollars // risk_per_contract)

    # 4. سقف أمان: عدم تخصيص أكثر من MAX_PREMIUM_PCT (10%) من المحفظة لعقد واحد
    max_capital_allowed = equity * MAX_PREMIUM_PCT
    max_contracts_cap = int(max_capital_allowed // (ask_price * 100))

    final_qty = min(compound_qty, max_contracts_cap)
    
    log.info(
        "📈 COMPOUND CALCULATOR | Equity: $%.2f | Risk Alloc: $%.2f | Calculated Qty: %d",
        equity, max_risk_dollars, final_qty
    )
    return max(1, final_qty)

# ---------------------------------------------------------
# 7. قنص العقود والأسعار
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

def choose_best_contract(direction, underlying_price):
    target_strike = underlying_price * (1 + DELTA_PROXY_PCT if direction == "up" else 1 - DELTA_PROXY_PCT)
    opt_type = "call" if direction == "up" else "put"
    
    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "type": opt_type,
        "expiration_date": today(),
        "limit": 15,
        "strike_price_gte": f"{target_strike - 2:.2f}",
        "strike_price_lte": f"{target_strike + 2:.2f}",
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
        spread_pct = (ask - bid) / mid
        
        if spread_pct <= MAX_SPREAD_PCT and ask >= MIN_OPTION_ASK:
            return sym, ask

    return None, None

# ---------------------------------------------------------
# 8. إدارة فتح الصفقات وإغلاقها
# ---------------------------------------------------------
def open_compound_position(direction, underlying_price, meta):
    if state.get("position") or state.get("halted"):
        return

    contract, ask = choose_best_contract(direction, underlying_price)
    if not contract or not ask:
        log.warning("⚠️ No valid contract found matching liquidity criteria.")
        return

    stop_loss_pct = 0.25 # وقف خسارة افتراضي 25% على سعر العقد
    qty = calculate_compound_position_size(ask, stop_loss_pct)
    if qty < 1:
        log.warning("⚠️ Compounded position size evaluated to < 1 contract.")
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
        log.error("Failed to submit entry order: %s", data)
        return

    fill_price = ask
    pos = {
        "contract": contract,
        "direction": direction,
        "qty": qty,
        "entry": fill_price,
        "peak": fill_price,
        "stop": fill_price * (1 - stop_loss_pct),
        "entry_time": iso_now(),
        "trail_active": False,
    }
    state["position"] = pos
    state["trades"] += 1
    state["last_entry_time"] = iso_now()
    save_state()
    log.info("🚀 COMPOUND ENTRY | Symbol: %s | Qty: %d | Entry: $%.2f", contract, qty, fill_price)

def manage_position():
    pos = state.get("position")
    if not pos:
        return

    q = get_option_quote(pos["contract"])
    if not q:
        return
    bid = q[0]

    # تحديث أعلى سعر وصل له العقد (Peak Price)
    if bid > pos["peak"]:
        pos["peak"] = bid

    gain_pct = (bid - pos["entry"]) / pos["entry"]
    peak_gain_pct = (pos["peak"] - pos["entry"]) / pos["entry"]

    # 1. تفعيل الوقف عند نقطة الدخول (Breakeven)
    if gain_pct >= BREAKEVEN_TRIGGER and pos["stop"] < pos["entry"]:
        pos["stop"] = pos["entry"] * 1.02
        log.info("🛡️ BREAKEVEN ACTIVATED | New Stop: $%.2f", pos["stop"])

    # 2. تفعيل الوقف المتحرك (Trailing Stop)
    if peak_gain_pct >= TRAIL_ACTIVATION:
        new_trail_stop = pos["peak"] * (1 - TRAIL_DISTANCE)
        if new_trail_stop > pos["stop"]:
            pos["stop"] = new_trail_stop
            pos["trail_active"] = True
            log.info("📈 TRAILING STOP UPDATED | Peak: $%.2f | New Stop: $%.2f", pos["peak"], pos["stop"])

    # 3. شرط الخروج (الوصول للوقف أو نهاية اليوم)
    if bid <= pos["stop"] or now_ny().time() >= EOD_CLOSE:
        reason = "TRAILING_STOP" if pos.get("trail_active") else ("EOD_CLOSE" if now_ny().time() >= EOD_CLOSE else "STOP_LOSS")
        close_position(pos, bid, reason)

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

    state["net_pnl"] += net_pnl
    if net_pnl > 0:
        state["wins"] += 1
        state["consec_losses"] = 0
    else:
        state["consec_losses"] += 1

    log.info("🏁 POSITION CLOSED [%s] | Exit: $%.2f | Net PnL: $%.2f", reason, exit_price, net_pnl)
    state["position"] = None
    get_equity() # تحديث رأس المال للفائدة المركبة التالية
    save_state()

# ---------------------------------------------------------
# 9. الحلقة الرئيسية (Execution Engine)
# ---------------------------------------------------------
def run():
    log.info("⚡ SAFAA QUANT %s STARTED", VERSION)
    eq = get_equity()
    log.info("💰 STARTING CAPITAL: $%.2f", eq)

    if not state:
        state.update(new_state(eq))
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
                
                # إدارة الصفقة القائمة
                if state.get("position"):
                    manage_position()
                
                # البحث عن إشارات دخول جديدة إذا لم تكن هناك صفقة مفتوحة
                elif not state.get("halted") and SIGNAL_START <= now_ny().time() <= SIGNAL_END:
                    # استراتيجية اختراق بسيطة وعالية الزخم (VWAP Breakout)
                    pv = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0 * float(b["v"]) for b in bars)
                    vol = sum(float(b["v"]) for b in bars)
                    vwap = pv / vol if vol > 0 else last_price

                    if last_price > vwap * 1.001:
                        open_compound_position("up", last_price, {"signal": "vwap_bull"})
                    elif last_price < vwap * 0.999:
                        open_compound_position("down", last_price, {"signal": "vwap_bear"})

            time.sleep(2) # تحديث كل ثانيتين لضمان استجابة فائقة السرعة

        except Exception as e:
            log.exception("Error in main loop: %s", e)
            time.sleep(5)

if __name__ == "__main__":
    run()
