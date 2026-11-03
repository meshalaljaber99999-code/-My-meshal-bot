# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v83.1.1 (OPTIMIZED & FAST - PAPER TRADING)
SPY 0-DTE | PAPER TRADING ONLY
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

VERSION = "v83.1.1-fast"
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

def env_name_found(names):
    for name in names:
        val = os.getenv(name)
        if val is not None and str(val).strip():
            return name
    return "NONE"

RISK_PER_TRADE = env_float("RISK_PER_TRADE", "0.015")
MAX_PREMIUM_PCT = env_float("MAX_PREMIUM_PCT", "0.10")
DAILY_LOSS_LIMIT = env_float("DAILY_LOSS_LIMIT", "0.04")
ATR_MULTIPLIER = env_float("ATR_MULTIPLIER", "1.5")
COMMISSION_PER_CONTRACT = 0.65

LOSS_PAUSE_MINUTES = env_int("LOSS_PAUSE_MINUTES", 30)
MAX_CONSEC_LOSSES = env_int("MAX_CONSEC_LOSSES", 2)
ENTRY_COOLDOWN_MINUTES = env_int("ENTRY_COOLDOWN_MINUTES", 15)
BIAS_REFRESH_MINUTES = 15

OR_MAX_AGE_MINUTES = 90
OR_MIN_WIDTH = 0.50
OR_MAX_WIDTH = 5.50

BREAKEVEN_TRIGGER = 0.25
TRAIL_ACTIVATION = 0.45
TRAIL_DISTANCE = 0.15
SCALP_BREAKEVEN = 0.12
SCALP_TRAIL_ACTIVATION = 0.18
SCALP_TRAIL_DISTANCE = 0.10
SCALP_TIME_LIMIT_MINS = 45

VOLUME_MULT = 1.25
MAX_SPREAD_PCT = 0.08
MIN_OPTION_ASK = 0.25
DELTA_PROXY_PCT = 0.003
LIMIT_TIMEOUT = 3       # تقليص وقت الانتظار لتسريع التنفيذ
CANCEL_WAIT = 6         # تقليص زمن انتظار الإلغاء
PROTECT_CONFIRM_WAIT = 4 # تسريع تأكيد الحماية
QUOTE_MAX_AGE_SECS = env_int("QUOTE_MAX_AGE_SECS", 5)
ORDER_LOOKUP_GRACE_SECS = 10

SIGNAL_START = dtime(9, 45)
SIGNAL_END = dtime(14, 0)
THETA_HARD_CUTOFF = dtime(14, 30)
EOD_CLOSE = dtime(15, 45)

KEY_NAMES = ["ALPACA_API_KEY", "APCA_API_KEY_ID", "ALPACA_KEY", "API_KEY", "APCA_API_KEY"]
SECRET_NAMES = ["ALPACA_API_SECRET", "APCA_API_SECRET_KEY", "ALPACA_SECRET", "API_SECRET", "APCA_API_SECRET"]

API_KEY = env_first(*KEY_NAMES)
API_SECRET = env_first(*SECRET_NAMES)
TRADING_URL = env_first("ALPACA_TRADING_URL", "APCA_API_BASE_URL", "TRADING_URL", default="https://paper-api.alpaca.markets").rstrip("/")
DATA_URL = env_first("ALPACA_DATA_URL", "APCA_DATA_URL", "DATA_URL", default="https://data.alpaca.markets").rstrip("/")
STOCK_FEED = "iex"
OPTIONS_FEED = "indicative"

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "safaa_v83_state.json"
TRADES_FILE = DATA_DIR / "safaa_v83_journal.csv"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("SAFAA")

if "paper" not in TRADING_URL.lower():
    raise RuntimeError("PAPER ONLY")

TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}
ARMED = {"new", "partially_filled"}
WORKING = ARMED | {"accepted", "held", "pending_new", "pending_cancel", "pending_replace", "calculated"}

def now_ny():
    return datetime.now(NY)

def today():
    return now_ny().date().isoformat()

def iso_now():
    return now_ny().isoformat(timespec="seconds")

def parse_time(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(NY)

def api_get(url, params=None, retries=2):  # تقليص عدد محاولات الاتصال لتسريع الاستجابة
    backoff = 0.2
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

def api_delete(url):
    try:
        r = requests.delete(url, headers=HEADERS, timeout=8)
        return r.status_code in (200, 204), (r.json() if r.content else {})
    except Exception as e:
        return False, {"error": str(e)}

def api_post_once(url, body):
    try:
        r = requests.post(url, headers=HEADERS, json=body, timeout=8)
        data = r.json() if r.content else {}
        return r.ok, data, r.status_code
    except Exception as e:
        return False, {"error": str(e)}, None

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
        "bias_score": 0,
        "last_bias_refresh": None,
        "atr": 0.0,
        "position": None,
        "entry_intent": None,
        "broker_uncertain": False,
        "owned_client_ids": [],
    }

state = load_state()

def halt(reason):
    state["halted"] = True
    state["halt_reason"] = reason
    log.error("HALT: %s", reason)
    save_state()

def track_client_id(cid):
    if not cid:
        return
    ids = state.setdefault("owned_client_ids", [])
    if cid not in ids:
        ids.append(cid)

def is_spy_option(sym):
    return isinstance(sym, str) and sym.startswith("SPY") and len(sym) >= 15

def is_owned_order(order):
    if not order:
        return False
    cid = order.get("client_order_id")
    if cid and cid in (state.get("owned_client_ids") or []):
        return True
    local = state.get("position") or {}
    intent = state.get("entry_intent") or {}
    known = {
        local.get("broker_stop_id"), local.get("broker_stop_client_id"),
        intent.get("order_id"), intent.get("client_order_id"),
        intent.get("protect_client_id"), intent.get("protect_order_id"),
    }
    return order.get("id") in known or cid in known

def get_order(order_id):
    if not order_id:
        return None
    ok, data = api_get(f"{TRADING_URL}/v2/orders/{order_id}")
    return data if ok else None

def get_order_by_client_id(client_order_id):
    if not client_order_id:
        return None
    ok, data = api_get(f"{TRADING_URL}/v2/orders:by_client_order_id", params={"client_order_id": client_order_id})
    if ok and isinstance(data, dict) and data.get("id"):
        return data
    for st in ("open", "closed"):
        ok, orders = api_get(f"{TRADING_URL}/v2/orders", params={"status": st, "limit": 50, "direction": "desc"})
        if ok:
            lst = orders.get("orders", []) if isinstance(orders, dict) else (orders if isinstance(orders, list) else [])
            for o in lst:
                if o.get("client_order_id") == client_order_id:
                    return o
    return None

def filled_qty(order):
    try:
        return int(float(order.get("filled_qty") or 0)) if order else 0
    except:
        return 0

def avg_fill(order):
    if not order:
        return None
    try:
        p = float(order.get("filled_avg_price") or 0)
        return p if p > 0 else None
    except:
        return None

def is_terminal(order):
    return bool(order) and order.get("status") in TERMINAL

def is_armed(order):
    return bool(order) and order.get("status") in ARMED

def is_working(order):
    return bool(order) and order.get("status") in WORKING

def wait_terminal(order_id, timeout=CANCEL_WAIT):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_order(order_id)
        if is_terminal(last):
            return last
        time.sleep(0.2) # استجابة أسرع
    return last

def cancel_and_confirm(order_id, timeout=CANCEL_WAIT):
    if not order_id:
        return True, None
    api_delete(f"{TRADING_URL}/v2/orders/{order_id}")
    final = wait_terminal(order_id, timeout)
    return is_terminal(final), final

def wait_fill_then_force_terminal(order_id, timeout=LIMIT_TIMEOUT):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_order(order_id)
        if is_terminal(last):
            return last
        time.sleep(0.2)
    ok, final = cancel_and_confirm(order_id, CANCEL_WAIT)
    return final or last

def list_spy_option_positions():
    ok, data = api_get(f"{TRADING_URL}/v2/positions")
    if not ok:
        return None
    return [p for p in data if is_spy_option(str(p.get("symbol", "")))]

def broker_qty(symbol):
    pos = list_spy_option_positions()
    if pos is None:
        return None
    for p in pos:
        if p.get("symbol") == symbol:
            return abs(int(float(p.get("qty") or 0)))
    return 0

def submit_order(body):
    cid = body.get("client_order_id")
    if cid:
        track_client_id(cid)
        save_state()
    ok, data, code = api_post_once(f"{TRADING_URL}/v2/orders", body)
    if ok and data and data.get("id"):
        return "ok", data
    if code is None:
        return "ambiguous", data
    if 400 <= code < 500:
        return "rejected", data
    return "ambiguous", data

def ensure_ledger(pos):
    if "fill_ledger" not in pos or not isinstance(pos["fill_ledger"], dict):
        pos["fill_ledger"] = {}
    pos.setdefault("exit_qty_accum", 0)
    pos.setdefault("exit_value_accum", 0.0)

def sync_order_fills(pos, order, tag):
    if not order or not order.get("id"):
        return 0
    ensure_ledger(pos)
    if order.get("side") != "sell" or order.get("symbol") != pos.get("contract"):
        return 0
    oid = order["id"]
    fq = filled_qty(order)
    px = avg_fill(order)
    rec = pos["fill_ledger"].setdefault(oid, {"last_filled_qty": 0, "last_filled_value": 0.0, "accounted_qty": 0, "pending_price": False, "tag": tag})
    rec["last_filled_qty"] = fq
    if fq == 0 or px is None:
        rec["pending_price"] = (px is None and fq > 0)
        return 0
    new_total = px * fq
    already_q = int(rec.get("accounted_qty") or 0)
    qty_delta = fq - already_q
    value_delta = new_total - float(rec.get("last_filled_value") or 0.0)
    if qty_delta <= 0:
        return 0
    pos["exit_qty_accum"] += qty_delta
    pos["exit_value_accum"] += value_delta
    rec["accounted_qty"] = fq
    rec["last_filled_value"] = new_total
    rec["pending_price"] = False
    return qty_delta

def validate_protective_order(order, symbol, qty, stop_price):
    if not order or order.get("symbol") != symbol or order.get("side") != "sell":
        return False, "invalid"
    try:
        if (int(float(order.get("qty", 0))) - int(float(order.get("filled_qty", 0)))) != int(qty):
            return False, "qty"
        if abs(float(order.get("stop_price", 0)) - float(stop_price)) > 0.02:
            return False, "price"
    except:
        return False, "parse"
    return order.get("status") in ARMED, "ok"

def owned_working_sells(symbol):
    ok, orders = api_get(f"{TRADING_URL}/v2/orders", params={"status": "open"})
    if not ok or not isinstance(orders, list):
        return []
    return [o for o in orders if o.get("symbol") == symbol and o.get("side") == "sell" and is_working(o)]

def no_working_sells(symbol):
    return len(owned_working_sells(symbol)) == 0

def place_protection(pos):
    symbol = pos["contract"]
    bq = broker_qty(symbol)
    if bq is None or bq <= 0:
        if bq == 0: pos["qty_open"] = 0
        return bq == 0
    pos["qty_open"] = bq
    stop_px = round(float(pos["stop"]), 2)
    cid = f"prot-{uuid.uuid4().hex[:16]}"
    track_client_id(cid)

    st, data = submit_order({
        "symbol": symbol, "qty": str(bq), "side": "sell", "type": "stop",
        "stop_price": f"{stop_px:.2f}", "time_in_force": "day", "client_order_id": cid,
    })
    if st != "ok":
        return False

    new_id = data["id"]
    old_id = pos.get("broker_stop_id")
    if old_id and old_id != new_id:
        cancel_and_confirm(old_id)

    pos["broker_stop_id"] = new_id
    pos["broker_stop_client_id"] = cid
    pos["broker_stop_price"] = stop_px
    save_state()
    return True

def protection_guardian(pos):
    if not pos:
        return True
    bq = broker_qty(pos["contract"])
    if bq is None or bq <= 0:
        return True
    pos["qty_open"] = bq
    prot = get_order(pos.get("broker_stop_id"))
    ok, _ = validate_protective_order(prot, pos["contract"], bq, pos["stop"])
    if ok:
        return True
    return place_protection(pos)

def get_stock_bars(symbol, start, end, timeframe="1Min"):
    bars = []
    token = None
    for _ in range(3): # تقليص الدورات لتسريع جلب الشموع
        params = {"timeframe": timeframe, "start": start.isoformat(), "end": end.isoformat(), "limit": 1000, "feed": STOCK_FEED}
        if token: params["page_token"] = token
        ok, data = api_get(f"{DATA_URL}/v2/stocks/{symbol}/bars", params=params)
        if not ok or not data or not isinstance(data, dict):
            break
        bars.extend(data.get("bars") or [])
        token = data.get("next_page_token")
        if not token: break
    return bars

def session_bars():
    n = now_ny()
    return get_stock_bars(SYMBOL, n.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(ZoneInfo("UTC")), n.astimezone(ZoneInfo("UTC")))

def calculate_vwap(bars):
    pv = sum((float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0 * float(b["v"]) for b in bars)
    vol = sum(float(b["v"]) for b in bars)
    return pv / vol if vol else None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    gains = [max(0, prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    losses = [max(0, prices[i - 1] - prices[i]) for i in range(1, len(prices))]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(prices) - 1):
        ag, al = (ag * 13 + gains[i]) / 14, (al * 13 + losses[i]) / 14
    return 100.0 if al == 0 else 100.0 - (100.0 / (1.0 + ag / al))

def refresh_bias():
    n = now_ny()
    if state.get("last_bias_refresh") and (n - datetime.fromisoformat(state["last_bias_refresh"])).total_seconds() < BIAS_REFRESH_MINUTES * 60:
        return
    bars = session_bars()
    if not bars: return
    vwap = calculate_vwap(bars)
    c = float(bars[-1]["c"])
    bias = "bull" if vwap and c > vwap else "bear" if vwap and c < vwap else "neutral"
    state.update({"bias": bias, "atr": float(bars[-1]["h"]) - float(bars[-1]["l"]), "last_bias_refresh": iso_now()})
    save_state()

def get_option_quote(symbol):
    ok, data = api_get(f"{DATA_URL}/v1beta1/options/quotes/latest", params={"symbols": symbol, "feed": OPTIONS_FEED})
    if not ok: return None
    q = (data.get("quotes") or {}).get(symbol)
    if not q: return None
    b, a = float(q.get("bp", 0)), float(q.get("ap", 0))
    return (b, a) if b > 0 and a > 0 and a > b else None

def choose_contract(direction, underlying):
    target = underlying * (1 + DELTA_PROXY_PCT if direction == "up" else 1 - DELTA_PROXY_PCT)
    ok, data = api_get(f"{TRADING_URL}/v2/options/contracts", params={"underlying_symbols": SYMBOL, "status": "active", "type": "call" if direction == "up" else "put", "expiration_date": today(), "limit": 10})
    if not ok: return None, None
    for c in sorted(data.get("option_contracts", []), key=lambda x: abs(float(x["strike_price"]) - target)):
        q = get_option_quote(c["symbol"])
        if q and q[1] >= MIN_OPTION_ASK and ((q[1] - q[0]) / ((q[0] + q[1]) / 2)) <= MAX_SPREAD_PCT:
            return c["symbol"], (q[1] - q[0]) / ((q[0] + q[1]) / 2)
    return None, None

def opening_range_signal():
    if state.get("pause_until") and now_ny() < datetime.fromisoformat(state["pause_until"]): return None
    bars = session_bars()
    if len(bars) < 20: return None
    last = bars[-1]
    if state.get("last_signal_bar") == last["t"]: return None
    c, vwap = float(last["c"]), calculate_vwap(bars)
    hi, lo = max(float(b["h"]) for b in bars[:15]), min(float(b["l"]) for b in bars[:15])
    bias = state.get("bias")
    if bias == "bull" and c > hi and vwap and c > vwap: return "up", c, "trend", {"bar_t": last["t"]}
    if bias == "bear" and c < lo and vwap and c < vwap: return "down", c, "trend", {"bar_t": last["t"]}
    return None

def settle_full_trade(pos, reason):
    exit_qty = int(pos.get("exit_qty_accum") or pos.get("qty") or 1)
    exit_avg = (pos.get("exit_value_accum") or pos.get("entry") * exit_qty) / exit_qty
    gross = (exit_avg - pos["entry"]) * 100 * exit_qty
    net = gross - exit_qty * COMMISSION_PER_CONTRACT * 2
    state["net_pnl"] = float(state.get("net_pnl") or 0) + net
    if net > 0: state["wins"] += 1; state["consec_losses"] = 0
    else:
        state["consec_losses"] += 1
        if state["consec_losses"] >= MAX_CONSEC_LOSSES:
            state["pause_until"] = (now_ny() + timedelta(minutes=LOSS_PAUSE_MINUTES)).isoformat()
    state["position"] = None
    save_state()
    return True, "ok"

def unrealized_pnl(pos):
    q = get_option_quote(pos["contract"])
    return (q[0] - pos["entry"]) * 100 * pos.get("qty_open", 0) if q else 0.0, bool(q)

def day_total_pnl():
    realized = float(state.get("net_pnl") or 0)
    pos = state.get("position")
    if not pos: return realized, True
    urg, ok = unrealized_pnl(pos)
    return realized + urg, ok

def check_daily_limit():
    equity = float(state.get("start_equity") or 0)
    total, ok = day_total_pnl()
    if ok and equity > 0 and total <= -(equity * DAILY_LOSS_LIMIT):
        halt(f"DAILY_LOSS_LIMIT total={total:.2f}")

def reconcile():
    positions = list_spy_option_positions()
    if positions is None: return False
    local = state.get("position")
    if local and not any(p["symbol"] == local["contract"] for p in positions):
        settle_full_trade(local, "BROKER_EXIT")
    return not state.get("halted", False)

def get_equity():
    ok, acc = api_get(f"{TRADING_URL}/v2/account")
    return float(acc.get("equity") or 0) if ok else 0.0

def open_position(direction, underlying, mode, meta):
    if state.get("position") or state.get("halted"): return
    equity = float(state.get("start_equity") or get_equity() or 0)
    contract, _ = choose_contract(direction, underlying)
    if not contract: return
    q = get_option_quote(contract)
    if not q: return
    bid, ask = q
    qty = max(1, int((equity * RISK_PER_TRADE) / (ask * 100 * 0.3)))
    
    client_id = f"entry-{uuid.uuid4().hex[:18]}"
    st, data = submit_order({"symbol": contract, "qty": str(qty), "side": "buy", "type": "limit", "limit_price": f"{ask:.2f}", "time_in_force": "day", "client_order_id": client_id})
    if st != "ok": return

    order = wait_fill_then_force_terminal(data["id"], LIMIT_TIMEOUT)
    fq = filled_qty(order)
    if fq <= 0: return

    fill = avg_fill(order) or ask
    pos = {
        "contract": contract, "direction": direction, "mode": mode, "qty": fq, "qty_open": fq,
        "entry": fill, "peak": fill, "stop": fill * 0.7, "broker_stop_id": None, "entry_time": iso_now(),
        "exit_qty_accum": 0, "exit_value_accum": 0.0, "fill_ledger": {}, "pending_exits": []
    }
    state["position"] = pos
    save_state()
    place_protection(pos)
    state["trades"] += 1
    log.info("OPEN [%s] qty=%d fill=%.2f", mode, fq, fill)

def manage_position():
    pos = state.get("position")
    if not pos: return
    check_daily_limit()
    if state.get("halted"): return
    
    bq = broker_qty(pos["contract"])
    if bq == 0:
        settle_full_trade(pos, "STOP")
        return
    
    q = get_option_quote(pos["contract"])
    if not q: return
    bid = q[0]
    if bid > pos["peak"]: pos["peak"] = bid
    
    if (bid - pos["entry"]) / pos["entry"] >= BREAKEVEN_TRIGGER:
        pos["stop"] = max(pos["stop"], pos["entry"])
        
    if bid <= pos["stop"] or now_ny().time() >= EOD_CLOSE:
        cancel_and_confirm(pos.get("broker_stop_id"))
        api_post_once(f"{TRADING_URL}/v2/orders", {"symbol": pos["contract"], "qty": str(bq), "side": "sell", "type": "market", "time_in_force": "day"})
        settle_full_trade(pos, "EXIT")

def run():
    log.info("SAFAA %s (FAST)", VERSION)
    eq = get_equity()
    if eq <= 0: return
    if not state: state.update(new_state(eq)); save_state()
    
    while True:
        try:
            ok, clock = api_get(f"{TRADING_URL}/v2/clock")
            if not ok or not (clock or {}).get("is_open"):
                time.sleep(15)
                continue
            
            reconcile()
            refresh_bias()
            check_daily_limit()
            
            if state.get("position"):
                manage_position()
            elif not state.get("halted") and SIGNAL_START <= now_ny().time() <= SIGNAL_END:
                sig = opening_range_signal()
                if sig: open_position(*sig)
                
            time.sleep(1) # حلقة تكرار سريعة جداً (كل ثانية واحدة)
        except Exception as e:
            log.exception("Loop %s", e)
            time.sleep(3)

if __name__ == "__main__":
    run()
