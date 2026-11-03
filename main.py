# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v83.1 (INTEGRITY PATCH + FLEX CREDENTIALS)
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

VERSION = "v83.1"
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
LIMIT_TIMEOUT = 4
CANCEL_WAIT = 12
PROTECT_CONFIRM_WAIT = 8
QUOTE_MAX_AGE_SECS = env_int("QUOTE_MAX_AGE_SECS", 5)
ORDER_LOOKUP_GRACE_SECS = 15

SIGNAL_START = dtime(9, 45)
SIGNAL_END = dtime(14, 0)
THETA_HARD_CUTOFF = dtime(14, 30)
EOD_CLOSE = dtime(15, 45)

KEY_NAMES = [
    "ALPACA_API_KEY",
    "APCA_API_KEY_ID",
    "ALPACA_KEY",
    "API_KEY",
    "APCA_API_KEY",
]
SECRET_NAMES = [
    "ALPACA_API_SECRET",
    "APCA_API_SECRET_KEY",
    "ALPACA_SECRET",
    "API_SECRET",
    "APCA_API_SECRET",
]

API_KEY = env_first(*KEY_NAMES)
API_SECRET = env_first(*SECRET_NAMES)
TRADING_URL = env_first(
    "ALPACA_TRADING_URL",
    "APCA_API_BASE_URL",
    "TRADING_URL",
    default="https://paper-api.alpaca.markets",
).rstrip("/")
DATA_URL = env_first(
    "ALPACA_DATA_URL",
    "APCA_DATA_URL",
    "DATA_URL",
    default="https://data.alpaca.markets",
).rstrip("/")
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

def api_get(url, params=None, retries=3):
    backoff = 0.5
    last_err = None
    for _ in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=12)
            data = r.json() if r.content else {}
            if r.ok:
                return True, data
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff)
                backoff *= 2
                continue
            return False, {"status_code": r.status_code, "body": data}
        except Exception as e:
            last_err = str(e)
            time.sleep(backoff)
            backoff *= 2
    return False, {"error": "request_failed", "detail": last_err or "unknown"}

def api_delete(url):
    try:
        r = requests.delete(url, headers=HEADERS, timeout=12)
        return r.status_code in (200, 204), (r.json() if r.content else {})
    except Exception as e:
        return False, {"error": str(e)}

def api_post_once(url, body):
    try:
        r = requests.post(url, headers=HEADERS, json=body, timeout=12)
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
        local.get("broker_stop_id"),
        local.get("broker_stop_client_id"),
        intent.get("order_id"),
        intent.get("client_order_id"),
        intent.get("protect_client_id"),
        intent.get("protect_order_id"),
        intent.get("flatten_client_id"),
        intent.get("flatten_order_id"),
    }
    for pe in local.get("pending_exits") or []:
        known.add(pe.get("order_id"))
        known.add(pe.get("client_order_id"))
    for pe in intent.get("pending_exits") or []:
        known.add(pe.get("order_id"))
        known.add(pe.get("client_order_id"))
    return order.get("id") in known or cid in known

def get_order(order_id):
    if not order_id:
        return None
    ok, data = api_get(f"{TRADING_URL}/v2/orders/{order_id}")
    return data if ok else None

def get_order_by_client_id(client_order_id):
    if not client_order_id:
        return None
    ok, data = api_get(
        f"{TRADING_URL}/v2/orders:by_client_order_id",
        params={"client_order_id": client_order_id},
    )
    if ok and isinstance(data, dict) and data.get("id"):
        return data
    for st in ("open", "closed"):
        token = None
        for _ in range(5):
            params = {"status": st, "limit": 100, "direction": "desc"}
            if token:
                params["page_token"] = token
            ok, orders = api_get(f"{TRADING_URL}/v2/orders", params=params)
            if not ok:
                break
            if isinstance(orders, dict):
                lst = orders.get("orders") or []
                token = orders.get("next_page_token")
            else:
                lst = orders if isinstance(orders, list) else []
                token = None
            for o in lst:
                if o.get("client_order_id") == client_order_id:
                    return o
            if not token:
                break
    return None

def filled_qty(order):
    if not order:
        return 0
    try:
        return int(float(order.get("filled_qty") or 0))
    except Exception:
        return 0

def avg_fill(order):
    if not order:
        return None
    raw = order.get("filled_avg_price")
    if raw in (None, "", 0, "0"):
        return None
    try:
        p = float(raw)
        return p if p > 0 else None
    except Exception:
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
        time.sleep(0.35)
    return last

def cancel_and_confirm(order_id, timeout=CANCEL_WAIT):
    if not order_id:
        return True, None
    api_delete(f"{TRADING_URL}/v2/orders/{order_id}")
    final = wait_terminal(order_id, timeout)
    if is_terminal(final):
        return True, final
    api_delete(f"{TRADING_URL}/v2/orders/{order_id}")
    final = wait_terminal(order_id, 4)
    return is_terminal(final), final

def wait_fill_then_force_terminal(order_id, timeout=LIMIT_TIMEOUT):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = get_order(order_id)
        if is_terminal(last):
            return last
        time.sleep(0.35)
    ok, final = cancel_and_confirm(order_id, CANCEL_WAIT)
    return final or last

def list_orders(status="open", max_pages=5):
    out = []
    token = None
    for _ in range(max_pages):
        params = {"status": status, "limit": 100, "direction": "desc"}
        if token:
            params["page_token"] = token
        ok, data = api_get(f"{TRADING_URL}/v2/orders", params=params)
        if not ok:
            return None
        if isinstance(data, dict) and "orders" in data:
            out.extend(data.get("orders") or [])
            token = data.get("next_page_token")
        elif isinstance(data, list):
            out.extend(data)
            token = None
        else:
            break
        if not token:
            break
    return out

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
    if code and 400 <= code < 500:
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
    if order.get("side") != "sell":
        return 0
    if order.get("symbol") != pos.get("contract"):
        log.error("Ledger symbol mismatch order=%s pos=%s", order.get("symbol"), pos.get("contract"))
        halt("LEDGER_SYMBOL_MISMATCH")
        return 0

    oid = order["id"]
    fq = filled_qty(order)
    px = avg_fill(order)
    rec = pos["fill_ledger"].setdefault(oid, {
        "last_filled_qty": 0,
        "last_filled_value": 0.0,
        "accounted_qty": 0,
        "pending_price": False,
        "tag": tag,
        "side": "sell",
        "symbol": pos.get("contract"),
        "client_order_id": order.get("client_order_id"),
        "type": order.get("type"),
    })

    if rec.get("symbol") and rec["symbol"] != pos.get("contract"):
        halt("LEDGER_REC_SYMBOL_DRIFT")
        return 0
    if rec.get("side") and rec["side"] != "sell":
        halt("LEDGER_REC_SIDE_DRIFT")
        return 0

    prev_q = int(rec.get("last_filled_qty") or 0)
    if fq < prev_q:
        halt(f"FILL_QTY_DECREASED {oid}")
        return 0

    rec["last_filled_qty"] = fq
    rec["tag"] = tag
    if order.get("client_order_id"):
        rec["client_order_id"] = order.get("client_order_id")
    if order.get("type"):
        rec["type"] = order.get("type")

    if fq == 0:
        rec["pending_price"] = False
        return 0

    if px is None:
        rec["pending_price"] = True
        return 0

    new_total_value = px * fq
    old_total_value = float(rec.get("last_filled_value") or 0.0)
    already_q = int(rec.get("accounted_qty") or 0)

    if already_q > 0 and new_total_value + 1e-6 < old_total_value and fq >= already_q:
        if old_total_value - new_total_value > 0.01:
            halt(f"LEDGER_VALUE_REGRESS {oid}")
            return 0

    need_account = rec.get("pending_price") or already_q < fq
    if not need_account:
        rec["last_filled_value"] = new_total_value
        rec["pending_price"] = False
        return 0

    if already_q == 0:
        qty_delta = fq
        value_delta = new_total_value
    else:
        qty_delta = fq - already_q
        value_delta = new_total_value - old_total_value

    if qty_delta < 0:
        halt(f"LEDGER_QTY_DELTA_NEG {oid}")
        return 0
    if qty_delta == 0 and abs(value_delta) < 1e-9:
        rec["pending_price"] = False
        rec["last_filled_value"] = new_total_value
        return 0

    pos["exit_qty_accum"] = int(pos.get("exit_qty_accum") or 0) + qty_delta
    pos["exit_value_accum"] = float(pos.get("exit_value_accum") or 0.0) + value_delta
    rec["accounted_qty"] = fq
    rec["last_filled_value"] = new_total_value
    rec["pending_price"] = False
    pos.setdefault("exit_tags", []).append({
        "id": oid,
        "qty_delta": qty_delta,
        "value_delta": value_delta,
        "avg": px,
        "tag": tag,
        "total_filled": fq,
    })
    log.info("LEDGER %s +qty=%d +val=%.4f exit_qty=%d", tag, qty_delta, value_delta, pos["exit_qty_accum"])
    return qty_delta

def order_fully_accounted(pos, order):
    if not order or not order.get("id"):
        return False
    ensure_ledger(pos)
    rec = pos["fill_ledger"].get(order["id"])
    fq = filled_qty(order)
    if fq == 0 and is_terminal(order):
        return True
    if not rec or rec.get("pending_price"):
        return False
    return int(rec.get("accounted_qty") or 0) == fq

def ledger_has_pending(pos):
    ensure_ledger(pos)
    return any(r.get("pending_price") for r in pos["fill_ledger"].values())

def validate_protective_order(order, symbol, qty, stop_price):
    if not order:
        return False, "no_order"
    if order.get("symbol") != symbol:
        return False, "symbol"
    if order.get("side") != "sell":
        return False, "side"
    if (order.get("type") or "").lower() not in ("stop", "stop_limit"):
        return False, "type"
    try:
        oq = int(float(order.get("qty") or 0))
        fq = int(float(order.get("filled_qty") or 0))
        remaining = max(0, oq - fq)
    except Exception:
        return False, "qty_parse"
    if remaining != int(qty):
        return False, f"rem:{remaining}!={qty}"
    try:
        sp = float(order.get("stop_price") or 0)
    except Exception:
        return False, "stop_parse"
    if abs(sp - float(stop_price)) > 0.02:
        return False, "stop_px"
    if order.get("status") not in ARMED:
        return False, f"status:{order.get('status')}"
    return True, "ok"

def owned_working_sells(symbol):
    orders = list_orders("open")
    if orders is None:
        return None
    out = []
    for o in orders:
        if o.get("symbol") != symbol or o.get("side") != "sell":
            continue
        if not is_working(o):
            continue
        if is_owned_order(o):
            out.append(o)
        else:
            halt(f"FOREIGN_SELL {o.get('id')}")
            return None
    return out

def no_working_sells(symbol):
    orders = list_orders("open")
    if orders is None:
        return False
    return not any(
        o.get("symbol") == symbol and o.get("side") == "sell" and is_working(o)
        for o in orders
    )

def place_protection(pos):
    symbol = pos["contract"]
    bq = broker_qty(symbol)
    if bq is None:
        return False
    if bq <= 0:
        pos["qty_open"] = 0
        return True
    pos["qty_open"] = bq
    qty = bq
    stop_px = round(float(pos["stop"]), 2)

    old_id = pos.get("broker_stop_id")
    cid = f"prot-{uuid.uuid4().hex[:16]}"
    track_client_id(cid)
    pos["protect_intent"] = {"client_order_id": cid, "qty": qty, "stop": stop_px, "time": iso_now()}
    save_state()

    st, data = submit_order({
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "stop",
        "stop_price": f"{stop_px:.2f}",
        "time_in_force": "day",
        "client_order_id": cid,
    })
    if st == "ambiguous":
        pos["protect_intent"]["ambiguous"] = True
        save_state()
        halt("PROT_SUBMIT_AMBIGUOUS")
        return False
    if st != "ok":
        pos.pop("protect_intent", None)
        save_state()
        return False

    new_id = data["id"]
    pos["protect_intent"]["order_id"] = new_id
    save_state()

    armed = False
    deadline = time.time() + PROTECT_CONFIRM_WAIT
    while time.time() < deadline:
        order = get_order(new_id)
        sync_order_fills(pos, order, "prot_watch")
        live = broker_qty(symbol)
        if live is None:
            break
        pos["qty_open"] = live
        if live <= 0:
            pos.pop("protect_intent", None)
            save_state()
            return True
        ok, why = validate_protective_order(order, symbol, live, stop_px)
        if ok:
            armed = True
            break
        if order and filled_qty(order) > 0 and why.startswith("rem:"):
            log.warning("Prot partial while arming — rebuild for live qty=%s", live)
            cancel_and_confirm(new_id)
            sync_order_fills(pos, get_order(new_id), "prot_partial")
            pos.pop("protect_intent", None)
            save_state()
            return place_protection(pos)
        if order and order.get("status") in ("rejected", "canceled", "expired"):
            break
        time.sleep(0.35)

    if not armed:
        cancel_and_confirm(new_id)
        sync_order_fills(pos, get_order(new_id), "prot_failed")
        pos.pop("protect_intent", None)
        save_state()
        return False

    if old_id and old_id != new_id:
        old_o = get_order(old_id)
        sync_order_fills(pos, old_o, "old_prot")
        cok, old_final = cancel_and_confirm(old_id)
        sync_order_fills(pos, old_final or old_o, "old_prot")
        if not cok:
            halt("OLD_STOP_CANCEL_FAIL")
            return False

    pos["broker_stop_id"] = new_id
    pos["broker_stop_client_id"] = cid
    pos["broker_stop_price"] = stop_px
    pos.pop("protect_intent", None)
    save_state()
    return True

def protection_guardian(pos):
    if not pos:
        return True
    live = broker_qty(pos["contract"])
    if live is None:
        return False
    pos["qty_open"] = live
    if live <= 0:
        return True
    sync_order_fills(pos, get_order(pos.get("broker_stop_id")), "protective")

    pi = pos.get("protect_intent")
    if pi:
        o = get_order(pi.get("order_id")) if pi.get("order_id") else None
        if o is None and pi.get("client_order_id"):
            o = get_order_by_client_id(pi["client_order_id"])
        if o is None and pi.get("ambiguous"):
            halt("PROT_INTENT_UNRESOLVED")
            return False
        if o:
            sync_order_fills(pos, o, "prot_intent")
            live = broker_qty(pos["contract"])
            if live is None:
                return False
            pos["qty_open"] = live
            if live <= 0:
                pos.pop("protect_intent", None)
                save_state()
                return True
            ok, _ = validate_protective_order(o, pos["contract"], live, pos["stop"])
            if ok:
                pos["broker_stop_id"] = o.get("id")
                pos["broker_stop_client_id"] = pi.get("client_order_id")
                pos.pop("protect_intent", None)
                save_state()
            elif is_working(o):
                return False
            else:
                pos.pop("protect_intent", None)
                save_state()

    sells = owned_working_sells(pos["contract"])
    if sells is None:
        return False
    prot = get_order(pos.get("broker_stop_id"))
    ok, why = validate_protective_order(prot, pos["contract"], pos["qty_open"], pos["stop"])
    if ok:
        extras = [s for s in sells if s.get("id") != pos.get("broker_stop_id")]
        if extras:
            halt(f"EXTRA_OWNED_SELLS {[e.get('id') for e in extras]}")
            return False
        return True
    return place_protection(pos)

def get_stock_bars(symbol, start, end, timeframe="1Min"):
    bars = []
    token = None
    for _ in range(5):
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
        
        # حماية مشددة لمنع الانهيار إذا كانت الاستجابة فارغة أو None
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
    end = n.replace(second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    return get_stock_bars(SYMBOL, start, end)

def calculate_vwap(bars):
    pv = 0.0
    vol = 0.0
    for b in bars:
        typ = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        v = float(b["v"])
        pv += typ * v
        vol += v
    return pv / vol if vol else None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains = [max(0, prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    losses = [max(0, prices[i - 1] - prices[i]) for i in range(1, len(prices))]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(prices) - 1):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + ag / al))

def calculate_intraday_atr(bars, period=30):
    if len(bars) < period + 1:
        return None
    tr = []
    for i in range(1, len(bars)):
        h = float(bars[i]["h"])
        l = float(bars[i]["l"])
        pc = float(bars[i - 1]["c"])
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(tr[-period:]) / period

def get_previous_close():
    n = now_ny()
    bars = get_stock_bars(
        SYMBOL,
        (n - timedelta(days=12)).astimezone(ZoneInfo("UTC")),
        n.astimezone(ZoneInfo("UTC")),
        "1Day",
    )
    done = [b for b in bars if parse_time(b["t"]).date() < n.date()]
    return float(done[-1]["c"]) if done else None

def get_trend_score():
    n = now_ny()
    bars = get_stock_bars(
        SYMBOL,
        (n - timedelta(days=40)).astimezone(ZoneInfo("UTC")),
        n.astimezone(ZoneInfo("UTC")),
        "1Day",
    )
    closes = [float(b["c"]) for b in bars if parse_time(b["t"]).date() < n.date()]
    if len(closes) < 20:
        return 0
    return 1 if closes[-1] > sum(closes[-20:]) / 20 else -1

def refresh_bias():
    n = now_ny()
    if state.get("last_bias_refresh"):
        if (n - datetime.fromisoformat(state["last_bias_refresh"])).total_seconds() < BIAS_REFRESH_MINUTES * 60:
            return
    prev = get_previous_close()
    if prev is None:
        return
    pm_start = n.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    pm_end = n.replace(hour=9, minute=29, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    pm = get_stock_bars(SYMBOL, pm_start, pm_end, "1Min")
    pm_vol = sum(float(b["v"]) for b in pm) if pm else 0
    ref = float(pm[-1]["c"]) if pm else prev
    gap = (ref - prev) / prev
    score = 0.0
    if gap > 0.0015:
        score += 1
    elif gap < -0.0015:
        score -= 1
    if pm_vol > 1_000_000:
        score += 1 if gap > 0 else -1
    score += get_trend_score()
    bars = session_bars()
    atr = calculate_intraday_atr(bars, 30) if bars else prev * 0.001
    if bars:
        vwap = calculate_vwap(bars)
        c = float(bars[-1]["c"])
        if vwap:
            if c > vwap * 1.0015:
                score += 0.5
            elif c < vwap * 0.9985:
                score -= 0.5
    bias = "bull" if score >= 2 else "bear" if score <= -2 else "neutral"
    state.update({
        "bias": bias,
        "bias_score": score,
        "atr": atr or 1.0,
        "last_bias_refresh": iso_now(),
    })
    save_state()

def get_option_quote(symbol):
    ok, data = api_get(
        f"{DATA_URL}/v1beta1/options/quotes/latest",
        params={"symbols": symbol, "feed": OPTIONS_FEED},
    )
    if not ok:
        return None
    q = (data.get("quotes") or {}).get(symbol)
    if not q:
        return None
    ts = q.get("t") or q.get("timestamp") or data.get("updated")
    if ts:
        try:
            qt = parse_time(ts) if isinstance(ts, str) else now_ny()
            age = (now_ny() - qt.astimezone(NY)).total_seconds()
            if age > QUOTE_MAX_AGE_SECS:
                log.warning("Stale quote %s age=%.1fs", symbol, age)
                return None
        except Exception:
            pass
    b = float(q.get("bp", 0))
    a = float(q.get("ap", 0))
    if not (b > 0 and a > 0 and a > b):
        return None
    return (b, a)

def choose_contract(direction, underlying):
    target = underlying * (1 + DELTA_PROXY_PCT if direction == "up" else 1 - DELTA_PROXY_PCT)
    opt = "call" if direction == "up" else "put"
    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "type": opt,
        "expiration_date": today(),
        "limit": 20,
        "strike_price_gte": f"{target - 2:.2f}",
        "strike_price_lte": f"{target + 2:.2f}",
    }
    ok, data = api_get(f"{TRADING_URL}/v2/options/contracts", params=params)
    if not ok:
        return None, None
    contracts = sorted(
        data.get("option_contracts") or [],
        key=lambda x: abs(float(x["strike_price"]) - target),
    )
    for c in contracts:
        sym = c["symbol"]
        q = get_option_quote(sym)
        if not q:
            continue
        bid, ask = q
        mid = (bid + ask) / 2
        if ((ask - bid) / mid) <= MAX_SPREAD_PCT and ask >= MIN_OPTION_ASK:
            return sym, (ask - bid) / mid
    return None, None

def is_cooling_down():
    if state.get("pause_until"):
        if now_ny() < datetime.fromisoformat(state["pause_until"]):
            return True
        state["pause_until"] = None
        save_state()
    le = state.get("last_entry_time")
    if le and (now_ny() - datetime.fromisoformat(le)).total_seconds() < ENTRY_COOLDOWN_MINUTES * 60:
        return True
    return False

def opening_range_signal():
    if is_cooling_down():
        return None
    n = now_ny()
    mopen = n.replace(hour=9, minute=30, second=0, microsecond=0)
    if (n - mopen).total_seconds() > OR_MAX_AGE_MINUTES * 60:
        return None
    bars = session_bars()
    if len(bars) < 35:
        return None
    rend = mopen + timedelta(minutes=30)
    opening = [b for b in bars if parse_time(b["t"]) < rend]
    after = [b for b in bars if parse_time(b["t"]) >= rend]
    if len(opening) < 10 or not after:
        return None
    last = after[-2] if len(after) >= 2 else after[-1]
    if state.get("last_signal_bar") == last["t"]:
        return None
    c = float(last["c"])
    hi = max(float(b["h"]) for b in opening)
    lo = min(float(b["l"]) for b in opening)
    width = hi - lo
    if width < OR_MIN_WIDTH or width > OR_MAX_WIDTH:
        return None
    vwap = calculate_vwap(bars)
    rsi = calculate_rsi([float(b["c"]) for b in bars])
    bias = state.get("bias")
    vol_ratio = float(last["v"]) / (sum(float(b["v"]) for b in bars[-10:-1]) / 9) if len(bars) > 10 else 1.0
    meta = {"vwap": vwap, "rsi": rsi, "vol_ratio": vol_ratio, "bias": bias, "bar_t": last["t"]}
    if bias in ("bull", "bear") and vol_ratio >= VOLUME_MULT and vwap:
        if c > hi and c > vwap and bias == "bull":
            return "up", c, "trend", meta
        if c < lo and c < vwap and bias == "bear":
            return "down", c, "trend", meta
    if bias == "neutral" and rsi is not None and vwap and abs(c - vwap) / vwap < 0.004:
        if c >= hi - width * 0.20 and rsi > 65:
            return "down", c, "scalp", meta
        if c <= lo + width * 0.20 and rsi < 35:
            return "up", c, "scalp", meta
    return None

def recover_pending_exits(pos):
    still = []
    for item in list(pos.get("pending_exits") or []):
        oid = item.get("order_id")
        cid = item.get("client_order_id")
        order = get_order(oid) if oid else None
        if order is None and cid:
            order = get_order_by_client_id(cid)
            if order:
                item["order_id"] = order.get("id")
        if order is None:
            still.append(item)
            continue
        sync_order_fills(pos, order, item.get("tag") or "pending_exit")
        if is_terminal(order) and order_fully_accounted(pos, order):
            item["resolved"] = True
        else:
            still.append(item)
    pos["pending_exits"] = still

def settlement_ready(pos):
    if ledger_has_pending(pos):
        return False, "pending_price"
    if pos.get("pending_exits"):
        return False, "pending_exits"
    bq = broker_qty(pos["contract"])
    if bq is None:
        return False, "broker_qty_unknown"
    if bq != 0:
        return False, f"broker_qty={bq}"
    if not no_working_sells(pos["contract"]):
        return False, "working_sells"
    entry_qty = int(pos.get("qty") or 0)
    exit_qty = int(pos.get("exit_qty_accum") or 0)
    if entry_qty <= 0 or exit_qty != entry_qty:
        return False, f"qty_mismatch {exit_qty}/{entry_qty}"
    return True, "ok"

def write_journal(pos, exit_avg, gross, net, reason, settled):
    newf = not TRADES_FILE.exists()
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if newf:
            w.writerow([
                "Time", "Contract", "Side", "Mode", "EntryQty", "ExitQty",
                "Entry", "ExitAvg", "Gross", "Net", "Reason", "Settled",
            ])
        w.writerow([
            iso_now(), pos["contract"], pos["direction"], pos["mode"], pos.get("qty"),
            pos.get("exit_qty_accum"), round(pos["entry"], 2), round(exit_avg or 0, 2),
            round(gross, 2), round(net, 2), reason, settled,
        ])

def settle_full_trade(pos, reason):
    recover_pending_exits(pos)
    sync_order_fills(pos, get_order(pos.get("broker_stop_id")), "protective")
    ok, why = settlement_ready(pos)
    if not ok:
        return False, why
    exit_qty = int(pos["exit_qty_accum"])
    exit_avg = pos["exit_value_accum"] / exit_qty
    gross = (exit_avg - pos["entry"]) * 100 * exit_qty
    net = gross - exit_qty * COMMISSION_PER_CONTRACT * 2
    state["net_pnl"] = float(state.get("net_pnl") or 0) + net
    write_journal(pos, exit_avg, gross, net, reason, True)
    if net > 0:
        state["wins"] += 1
        state["consec_losses"] = 0
    else:
        state["consec_losses"] += 1
        if state["consec_losses"] >= MAX_CONSEC_LOSSES:
            state["pause_until"] = (now_ny() + timedelta(minutes=LOSS_PAUSE_MINUTES)).isoformat()
    check_daily_limit()
    log.info("SETTLED net=%.2f qty=%d %s", net, exit_qty, reason)
    return True, "ok"

def try_settle(pos, reason):
    ok, why = settle_full_trade(pos, reason)
    if ok:
        state["position"] = None
        save_state()
        return True
    log.warning("Settle deferred: %s", why)
    return False

def unrealized_pnl(pos):
    if not pos:
        return 0.0, True
    q = get_option_quote(pos["contract"])
    if not q:
        return 0.0, False
    return (q[0] - pos["entry"]) * 100 * pos.get("qty_open", 0) - pos.get("qty_open", 0) * COMMISSION_PER_CONTRACT, True

def day_total_pnl():
    realized = float(state.get("net_pnl") or 0)
    pos = state.get("position")
    if not pos:
        return realized, True
    urg, ok = unrealized_pnl(pos)
    if not ok:
        return realized, False
    return realized + urg, True

def check_daily_limit():
    equity = float(state.get("start_equity") or 0)
    if equity <= 0:
        return
    total, ok = day_total_pnl()
    if not ok:
        state["broker_uncertain"] = True
        save_state()
        return
    if total <= -(equity * DAILY_LOSS_LIMIT):
        halt(f"DAILY_LOSS_LIMIT total={total:.2f}")

def harvest_flatten_from_intent(intent, pos):
    oid = intent.get("flatten_order_id")
    cid = intent.get("flatten_client_id")
    order = get_order(oid) if oid else None
    if order is None and cid:
        order = get_order_by_client_id(cid)
    if order:
        sync_order_fills(pos, order, "flatten")
        return order
    for pe in intent.get("pending_exits") or []:
        o = get_order(pe.get("order_id")) if pe.get("order_id") else None
        if o is None and pe.get("client_order_id"):
            o = get_order_by_client_id(pe["client_order_id"])
        if o:
            sync_order_fills(pos, o, pe.get("tag") or "flatten")
    return None

def resolve_entry_intent():
    intent = state.get("entry_intent")
    if not intent:
        return True

    stage = intent.get("stage")
    cid = intent.get("client_order_id")
    oid = intent.get("order_id")
    symbol = intent.get("contract")

    order = get_order(oid) if oid else None
    if order is None and cid:
        order = get_order_by_client_id(cid)
        if order:
            intent["order_id"] = order.get("id")
            save_state()

    if stage == "flatten" or intent.get("flatten_client_id") or intent.get("flatten_order_id"):
        if order is None:
            halt("FLATTEN_ENTRY_ORDER_UNKNOWN")
            return False
        fq = filled_qty(order)
        fill = avg_fill(order) or intent.get("ask") or 0.0
        bq = broker_qty(symbol)
        if bq is None:
            halt("FLATTEN_BROKER_QTY_UNKNOWN")
            return False
        if not state.get("position"):
            state["position"] = {
                "contract": symbol,
                "direction": intent.get("direction"),
                "mode": intent.get("mode"),
                "qty": fq if fq > 0 else int(intent.get("qty") or 0),
                "qty_open": bq,
                "entry": fill,
                "peak": fill,
                "stop": float(intent.get("stop_px") or 0),
                "broker_stop_id": intent.get("protect_order_id"),
                "entry_time": intent.get("time") or iso_now(),
                "meta": intent.get("meta") or {},
                "exit_qty_accum": 0,
                "exit_value_accum": 0.0,
                "fill_ledger": {},
                "pending_exits": list(intent.get("pending_exits") or []),
                "exit_phase": None,
                "trail_active": False,
                "breakeven_active": False,
            }
        pos = state["position"]
        harvest_flatten_from_intent(intent, pos)
        recover_pending_exits(pos)
        if pos.get("broker_stop_id"):
            sync_order_fills(pos, get_order(pos["broker_stop_id"]), "protective")
        bq = broker_qty(symbol)
        if bq is None:
            halt("FLATTEN_BROKER_QTY_UNKNOWN")
            return False
        pos["qty_open"] = bq
        if bq == 0:
            if try_settle(pos, "FLATTEN_RECOVERED"):
                state["entry_intent"] = None
                save_state()
                return True
            log.warning("Flatten recovery settle pending")
            return False
        state["entry_intent"] = intent
        save_state()
        return True

    if order is None:
        halt("ENTRY_INTENT_UNRESOLVED")
        return False

    if is_working(order):
        ok, order = cancel_and_confirm(order.get("id"))
        if not ok:
            halt("ENTRY_CANNOT_TERMINATE")
            return False
    if not is_terminal(order):
        halt("ENTRY_NOT_TERMINAL")
        return False

    fq = filled_qty(order)
    symbol = order.get("symbol") or symbol
    bq = broker_qty(symbol)
    if bq is None:
        halt("ENTRY_BROKER_QTY_UNKNOWN")
        return False

    if fq > 0 and bq == 0:
        pos = state.get("position")
        if not pos:
            pos = {
                "contract": symbol,
                "direction": intent.get("direction"),
                "mode": intent.get("mode"),
                "qty": fq,
                "qty_open": 0,
                "entry": avg_fill(order) or intent.get("ask") or 0.0,
                "peak": avg_fill(order) or 0.0,
                "stop": float(intent.get("stop_px") or 0),
                "broker_stop_id": intent.get("protect_order_id"),
                "broker_stop_client_id": intent.get("protect_client_id"),
                "entry_time": intent.get("time") or iso_now(),
                "meta": intent.get("meta") or {},
                "exit_qty_accum": 0,
                "exit_value_accum": 0.0,
                "fill_ledger": {},
                "pending_exits": list(intent.get("pending_exits") or []),
                "exit_phase": None,
                "trail_active": False,
                "breakeven_active": False,
            }
            state["position"] = pos
        if intent.get("protect_order_id") or intent.get("protect_client_id"):
            po = get_order(intent.get("protect_order_id")) if intent.get("protect_order_id") else None
            if po is None and intent.get("protect_client_id"):
                po = get_order_by_client_id(intent["protect_client_id"])
            sync_order_fills(pos, po, "protective")
        harvest_flatten_from_intent(intent, pos)
        recover_pending_exits(pos)
        if try_settle(pos, "ENTRY_FILLED_THEN_FLAT"):
            state["entry_intent"] = None
            save_state()
            return True
        halt("ENTRY_FILLED_BUT_FLAT_UNSETTLED")
        return False

    if fq <= 0 and bq == 0:
        state["entry_intent"] = None
        save_state()
        return True

    if bq > 0:
        if fq > 0 and bq != fq:
            halt(f"ENTRY_QTY_MISMATCH fill={fq} broker={bq}")
            return False
        fill = avg_fill(order) or intent.get("ask") or 0.0
        if not state.get("position"):
            state["position"] = {
                "contract": symbol,
                "direction": intent.get("direction"),
                "mode": intent.get("mode"),
                "qty": bq,
                "qty_open": bq,
                "entry": fill,
                "peak": fill,
                "stop": float(intent.get("stop_px") or fill * 0.7),
                "broker_stop_id": intent.get("protect_order_id"),
                "broker_stop_client_id": intent.get("protect_client_id"),
                "entry_time": intent.get("time") or iso_now(),
                "meta": intent.get("meta") or {},
                "exit_qty_accum": 0,
                "exit_value_accum": 0.0,
                "fill_ledger": {},
                "pending_exits": list(intent.get("pending_exits") or []),
                "exit_phase": None,
                "trail_active": False,
                "breakeven_active": False,
            }
        state["entry_intent"] = None
        save_state()
        if not protection_guardian(state["position"]):
            halt("RECOVER_PROT_FAIL")
            return False
        return True

    halt("ENTRY_INTENT_INCONSISTENT")
    return False

def reconcile():
    if state.get("entry_intent"):
        if not resolve_entry_intent():
            return False
    positions = list_spy_option_positions()
    if positions is None:
        state["broker_uncertain"] = True
        save_state()
        return False
    state["broker_uncertain"] = False
    local = state.get("position")
    by = {p["symbol"]: p for p in positions}

    if local:
        recover_pending_exits(local)
        sync_order_fills(local, get_order(local.get("broker_stop_id")), "protective")
        sym = local["contract"]
        if sym not in by:
            if not try_settle(local, local.get("exit_reason") or "BROKER_EXIT"):
                halt("FLAT_BUT_UNSETTLED")
                return False
        else:
            bq = abs(int(float(by[sym].get("qty") or 0)))
            local["qty_open"] = bq
            if bq > 0 and not local.get("exit_phase"):
                if not protection_guardian(local):
                    halt("RECON_PROT_FAIL")
                    return False
            save_state()
    else:
        if by:
            halt(f"UNKNOWN_POSITIONS {list(by.keys())}")
            return False

    orders = list_orders("open")
    if orders is None:
        state["broker_uncertain"] = True
        save_state()
        return False
    for o in orders:
        if is_spy_option(o.get("symbol", "")) and not is_owned_order(o):
            halt(f"UNMANAGED_ORDER {o.get('id')}")
            return False
    return not state.get("halted")

def get_equity():
    if not API_KEY or not API_SECRET:
        log.error(
            "Missing API credentials | tried keys=%s | tried secrets=%s",
            KEY_NAMES, SECRET_NAMES,
        )
        return 0.0
    ok, acc = api_get(f"{TRADING_URL}/v2/account")
    if not ok:
        log.error("Account API failed | url=%s | response=%s", TRADING_URL, acc)
        return 0.0
    try:
        eq = float(acc.get("equity") or 0)
        log.info(
            "Account OK | equity=%.2f | status=%s | account=%s",
            eq, acc.get("status"), acc.get("account_number"),
        )
        return eq
    except Exception as e:
        log.error("Equity parse error: %s | body=%s", e, acc)
        return 0.0

def roll_day():
    if state.get("date") == today():
        return True
    if state.get("position") or state.get("entry_intent"):
        halt("DAY_ROLL_OPEN_RISK")
        return False
    orders = list_orders("open")
    if orders is None:
        return False
    if any(is_spy_option(o.get("symbol", "")) for o in orders):
        halt("DAY_ROLL_OPEN_ORDERS")
        return False
    eq = get_equity()
    state.clear()
    state.update(new_state(eq))
    save_state()
    return True

def open_position(direction, underlying, mode, meta):
    if state.get("position") or state.get("entry_intent") or state.get("halted"):
        return
    equity = float(state.get("start_equity") or get_equity() or 0)
    if equity <= 0:
        return
    contract, spread = choose_contract(direction, underlying)
    if not contract:
        return
    q = get_option_quote(contract)
    if not q:
        return
    bid, ask = q
    atr = state.get("atr") or underlying * 0.001
    stop_pct = max(0.18, min(0.38, (ATR_MULTIPLIER * atr * 0.45) / ask))
    qty = int(min(
        (equity * RISK_PER_TRADE) // (ask * 100 * stop_pct),
        (equity * MAX_PREMIUM_PCT) // (ask * 100),
    ))
    if qty < 1:
        return

    client_id = f"entry-{uuid.uuid4().hex[:18]}"
    state["entry_intent"] = {
        "client_order_id": client_id,
        "contract": contract,
        "direction": direction,
        "mode": mode,
        "qty": qty,
        "ask": ask,
        "stop_px": ask * (1 - stop_pct),
        "meta": meta,
        "time": iso_now(),
        "stage": "pre_submit",
        "pending_exits": [],
    }
    track_client_id(client_id)
    save_state()

    st, data = submit_order({
        "symbol": contract,
        "qty": str(qty),
        "side": "buy",
        "type": "limit",
        "limit_price": f"{ask:.2f}",
        "time_in_force": "day",
        "client_order_id": client_id,
    })
    if st == "rejected":
        state["entry_intent"] = None
        save_state()
        return
    if st == "ambiguous":
        state["entry_intent"]["stage"] = "ambiguous_submit"
        save_state()
        halt("ENTRY_SUBMIT_AMBIGUOUS")
        return

    oid = data["id"]
    state["entry_intent"]["order_id"] = oid
    state["entry_intent"]["stage"] = "waiting"
    save_state()

    order = wait_fill_then_force_terminal(oid, LIMIT_TIMEOUT)
    if not is_terminal(order):
        halt("ENTRY_NOT_TERMINAL")
        return
    fq = filled_qty(order)
    if fq <= 0:
        state["entry_intent"] = None
        save_state()
        return

    fill = avg_fill(order) or ask
    stop_px = fill * (1 - stop_pct)
    bq = broker_qty(contract)
    if bq is None:
        halt("ENTRY_BROKER_QTY_UNKNOWN")
        return
    if bq != fq:
        halt(f"ENTRY_QTY_MISMATCH fill={fq} broker={bq}")
        return

    pos = {
        "contract": contract,
        "direction": direction,
        "mode": mode,
        "qty": fq,
        "qty_open": fq,
        "entry": fill,
        "peak": fill,
        "stop": stop_px,
        "broker_stop_id": None,
        "entry_time": iso_now(),
        "meta": {**(meta or {}), "spread": spread},
        "exit_qty_accum": 0,
        "exit_value_accum": 0.0,
        "fill_ledger": {},
        "pending_exits": [],
        "exit_phase": None,
        "trail_active": False,
        "breakeven_active": False,
    }
    state["position"] = pos
    state["entry_intent"]["stage"] = "position_saved"
    save_state()

    if not place_protection(pos):
        live = broker_qty(contract)
        if live is None:
            halt("POST_PROT_QTY_UNKNOWN")
            return
        pos["qty_open"] = live
        if live <= 0:
            state["entry_intent"]["stage"] = "flatten"
            save_state()
            if try_settle(pos, "STOPPED_DURING_ARM"):
                state["entry_intent"] = None
            else:
                halt("ARM_FLAT_UNSETTLED")
            save_state()
            return

        state["entry_intent"]["stage"] = "flatten"
        flat_cid = f"flat-{uuid.uuid4().hex[:12]}"
        state["entry_intent"]["flatten_client_id"] = flat_cid
        pe = {"client_order_id": flat_cid, "order_id": None, "tag": "flatten", "qty": live, "time": iso_now()}
        pos.setdefault("pending_exits", []).append(pe)
        state["entry_intent"].setdefault("pending_exits", []).append(pe)
        track_client_id(flat_cid)
        save_state()

        stf, datf = submit_order({
            "symbol": contract,
            "qty": str(live),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": flat_cid,
        })
        if stf == "ambiguous":
            halt("FLAT_AMBIGUOUS")
            return
        if stf == "ok":
            pe["order_id"] = datf["id"]
            state["entry_intent"]["flatten_order_id"] = datf["id"]
            save_state()
            fo = wait_fill_then_force_terminal(datf["id"], 6)
            sync_order_fills(pos, fo, "flatten")
        else:
            existing = get_order_by_client_id(flat_cid)
            if existing:
                pe["order_id"] = existing.get("id")
                sync_order_fills(pos, existing, "flatten")

        recover_pending_exits(pos)
        left = broker_qty(contract)
        if left is None:
            halt("FLATTEN_QTY_UNKNOWN")
            return
        pos["qty_open"] = left
        if left > 0:
            halt("FLATTEN_INCOMPLETE")
            return
        recover_pending_exits(pos)
        if try_settle(pos, "FLATTEN"):
            state["entry_intent"] = None
        else:
            time.sleep(0.5)
            recover_pending_exits(pos)
            if try_settle(pos, "FLATTEN"):
                state["entry_intent"] = None
            else:
                halt("FLATTEN_UNSETTLED")
        save_state()
        return

    state["entry_intent"] = None
    state["trades"] += 1
    state["last_entry_time"] = iso_now()
    state["last_signal_bar"] = (meta or {}).get("bar_t")
    save_state()
    log.info("OPEN [%s] qty=%d fill=%.2f", mode, fq, fill)

def begin_exit(pos, reason):
    pos["exit_phase"] = {"reason": reason, "stage": "start", "time": iso_now()}
    save_state()

def _age_seconds(iso_ts):
    try:
        return (now_ny() - datetime.fromisoformat(iso_ts)).total_seconds()
    except Exception:
        return 9999

def manage_position():
    pos = state.get("position")
    if not pos:
        return
    check_daily_limit()
    if state.get("halted"):
        return

    recover_pending_exits(pos)
    sync_order_fills(pos, get_order(pos.get("broker_stop_id")), "protective")

    bq = broker_qty(pos["contract"])
    if bq is None:
        state["broker_uncertain"] = True
        save_state()
        return
    state["broker_uncertain"] = False
    pos["qty_open"] = bq

    if bq <= 0:
        if not try_settle(pos, (pos.get("exit_phase") or {}).get("reason") or "BROKER_OR_STOP"):
            recover_pending_exits(pos)
            if not try_settle(pos, "BROKER_OR_STOP"):
                halt("FLAT_UNSETTLED_IN_MANAGE")
        save_state()
        return

    if pos.get("exit_phase") and pos["exit_phase"].get("stage") not in (None, "done"):
        _continue_exit(pos)
        return

    if not protection_guardian(pos):
        return

    q = get_option_quote(pos["contract"])
    if not q:
        return
    bid = q[0]
    entry = pos["entry"]
    if bid > pos["peak"]:
        pos["peak"] = bid
    gain = (bid - entry) / entry if entry else 0
    peak_gain = (pos["peak"] - entry) / entry if entry else 0
    mode = pos["mode"]
    is_scalp = mode == "scalp"
    trig_be = SCALP_BREAKEVEN if is_scalp else BREAKEVEN_TRIGGER
    trig_tr = SCALP_TRAIL_ACTIVATION if is_scalp else TRAIL_ACTIVATION
    dist = SCALP_TRAIL_DISTANCE if is_scalp else TRAIL_DISTANCE
    if now_ny().time() >= dtime(13, 0):
        dist *= 0.6

    moved = False
    if gain >= trig_be and not pos.get("breakeven_active"):
        ns = max(pos["stop"], entry * 1.05)
        if ns > pos["stop"]:
            pos["stop"] = ns
            moved = True
        pos["breakeven_active"] = True
    if peak_gain >= trig_tr:
        pos["trail_active"] = True
        ns = max(pos["stop"], entry, pos["peak"] * (1 - dist))
        if ns > pos["stop"] + 0.01:
            pos["stop"] = ns
            moved = True
    if moved and not place_protection(pos):
        halt("TRAIL_PROT_FAIL")
        return

    t = now_ny().time()
    dur = (now_ny() - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 60.0
    reason = None
    if t >= THETA_HARD_CUTOFF and gain < 0.15:
        reason = "THETA_CUTOFF"
    elif t >= EOD_CLOSE:
        reason = "EOD"
    elif bid <= pos["stop"]:
        reason = "TRAIL" if pos.get("trail_active") else "STOP"
    elif is_scalp and dur > SCALP_TIME_LIMIT_MINS and gain < trig_be:
        reason = "TIME_STOP"
    if not reason:
        save_state()
        return
    begin_exit(pos, reason)
    _continue_exit(pos)

def _continue_exit(pos):
    phase = pos.setdefault("exit_phase", {"stage": "start", "reason": "EXIT"})
    stage = phase.get("stage") or "start"
    reason = phase.get("reason") or "EXIT"
    symbol = pos["contract"]

    if stage == "start":
        phase["stage"] = "cancel_protect"
        save_state()
        stage = "cancel_protect"

    if stage == "cancel_protect":
        sync_order_fills(pos, get_order(pos.get("broker_stop_id")), "protective")
        sells = owned_working_sells(symbol)
        if sells is None:
            return
        for o in sells:
            sync_order_fills(pos, o, "exit_cancel")
            ok, fin = cancel_and_confirm(o.get("id"))
            sync_order_fills(pos, fin or o, "exit_cancel")
            if not ok:
                halt("EXIT_CANCEL_FAIL")
                return
        if not no_working_sells(symbol):
            return
        phase["stage"] = "submit_limit"
        save_state()
        stage = "submit_limit"

    if stage == "submit_limit":
        cid = phase.get("limit_client_id")
        if cid:
            existing = get_order_by_client_id(cid)
            if existing:
                phase["limit_order_id"] = existing.get("id")
                phase["stage"] = "limit_wait"
                save_state()
                stage = "limit_wait"
            else:
                submitted_at = phase.get("limit_submit_time") or phase.get("time")
                if _age_seconds(submitted_at) < ORDER_LOOKUP_GRACE_SECS:
                    log.info("Waiting for limit order visibility cid=%s", cid)
                    return
                if phase.get("limit_submit_attempted"):
                    halt("EXIT_LIMIT_CID_UNRESOLVED")
                    return

        if stage == "submit_limit":
            bq = broker_qty(symbol)
            if bq is None:
                halt("EXIT_QTY_UNKNOWN")
                return
            if bq <= 0:
                phase["stage"] = "finalize"
                save_state()
                stage = "finalize"
            else:
                q = get_option_quote(symbol)
                if not q:
                    return
                bid = q[0]
                if not phase.get("limit_client_id"):
                    phase["limit_client_id"] = f"exit-{uuid.uuid4().hex[:14]}"
                cid = phase["limit_client_id"]
                track_client_id(cid)
                pe = next((p for p in pos.get("pending_exits") or [] if p.get("client_order_id") == cid), None)
                if not pe:
                    pe = {
                        "client_order_id": cid,
                        "order_id": None,
                        "tag": "exit_limit",
                        "qty": bq,
                        "time": iso_now(),
                    }
                    pos.setdefault("pending_exits", []).append(pe)
                phase["limit_submit_time"] = iso_now()
                phase["limit_submit_attempted"] = True
                save_state()
                st, data = submit_order({
                    "symbol": symbol,
                    "qty": str(bq),
                    "side": "sell",
                    "type": "limit",
                    "limit_price": f"{bid:.2f}",
                    "time_in_force": "day",
                    "client_order_id": cid,
                })
                if st == "ambiguous":
                    halt("EXIT_LIMIT_AMBIGUOUS")
                    return
                if st != "ok":
                    existing = get_order_by_client_id(cid)
                    if not existing:
                        return
                    data = existing
                pe["order_id"] = data["id"]
                phase["limit_order_id"] = data["id"]
                phase["stage"] = "limit_wait"
                save_state()
                stage = "limit_wait"

    if stage == "limit_wait":
        oid = phase.get("limit_order_id")
        order = get_order(oid) if oid else None
        if order is None and phase.get("limit_client_id"):
            order = get_order_by_client_id(phase["limit_client_id"])
            if order:
                phase["limit_order_id"] = order.get("id")
                save_state()
        if order is None:
            if _age_seconds(phase.get("limit_submit_time") or phase.get("time")) < ORDER_LOOKUP_GRACE_SECS:
                return
            halt("EXIT_LIMIT_MISSING")
            return
        if not is_terminal(order):
            order = wait_fill_then_force_terminal(order.get("id"), LIMIT_TIMEOUT)
        sync_order_fills(pos, order, "exit_limit")
        if not is_terminal(order):
            halt("EXIT_LIMIT_NOT_TERMINAL")
            return
        phase["stage"] = "after_limit"
        save_state()
        stage = "after_limit"

    if stage == "after_limit":
        bq = broker_qty(symbol)
        if bq is None:
            halt("EXIT_QTY_UNKNOWN")
            return
        if bq <= 0:
            phase["stage"] = "finalize"
            save_state()
            stage = "finalize"
        else:
            cidm = phase.get("market_client_id")
            if cidm:
                existing = get_order_by_client_id(cidm)
                if existing:
                    phase["market_order_id"] = existing.get("id")
                    phase["stage"] = "market_wait"
                    save_state()
                    stage = "market_wait"
                else:
                    if phase.get("market_submit_attempted") and _age_seconds(phase.get("market_submit_time") or phase.get("time")) < ORDER_LOOKUP_GRACE_SECS:
                        return
                    if phase.get("market_submit_attempted") and _age_seconds(phase.get("market_submit_time") or phase.get("time")) >= ORDER_LOOKUP_GRACE_SECS:
                        halt("EXIT_MKT_CID_UNRESOLVED")
                        return

            if stage == "after_limit":
                if not phase.get("market_client_id"):
                    phase["market_client_id"] = f"exitm-{uuid.uuid4().hex[:12]}"
                cidm = phase["market_client_id"]
                track_client_id(cidm)
                pe = next((p for p in pos.get("pending_exits") or [] if p.get("client_order_id") == cidm), None)
                if not pe:
                    pe = {
                        "client_order_id": cidm,
                        "order_id": None,
                        "tag": "exit_market",
                        "qty": bq,
                        "time": iso_now(),
                    }
                    pos.setdefault("pending_exits", []).append(pe)
                phase["market_submit_time"] = iso_now()
                phase["market_submit_attempted"] = True
                save_state()
                stm, datm = submit_order({
                    "symbol": symbol,
                    "qty": str(bq),
                    "side": "sell",
                    "type": "market",
                    "time_in_force": "day",
                    "client_order_id": cidm,
                })
                if stm == "ambiguous":
                    halt("EXIT_MKT_AMBIGUOUS")
                    return
                if stm != "ok":
                    existing = get_order_by_client_id(cidm)
                    if not existing:
                        return
                    datm = existing
                pe["order_id"] = datm["id"]
                phase["market_order_id"] = datm["id"]
                phase["stage"] = "market_wait"
                save_state()
                stage = "market_wait"

    if stage == "market_wait":
        oid = phase.get("market_order_id")
        order = get_order(oid) if oid else None
        if order is None and phase.get("market_client_id"):
            order = get_order_by_client_id(phase["market_client_id"])
            if order:
                phase["market_order_id"] = order.get("id")
                save_state()
        if order is None:
            if _age_seconds(phase.get("market_submit_time") or phase.get("time")) < ORDER_LOOKUP_GRACE_SECS:
                return
            halt("EXIT_MKT_MISSING")
            return
        if not is_terminal(order):
            order = wait_fill_then_force_terminal(order.get("id"), 6)
        sync_order_fills(pos, order, "exit_market")
        if not is_terminal(order):
            halt("EXIT_MKT_NOT_TERMINAL")
            return
        phase["stage"] = "finalize"
        save_state()
        stage = "finalize"

    if stage == "finalize":
        recover_pending_exits(pos)
        bq = broker_qty(symbol)
        if bq is None:
            halt("FINAL_QTY_UNKNOWN")
            return
        if bq > 0:
            pos["qty_open"] = bq
            pos["exit_phase"] = None
            if not place_protection(pos):
                halt("REPROT_FAIL")
            save_state()
            return
        pos["qty_open"] = 0
        if not try_settle(pos, reason):
            recover_pending_exits(pos)
            if not try_settle(pos, reason):
                halt("FINAL_UNSETTLED")
        save_state()

def run():
    log.info("SAFAA %s", VERSION)
    log.info(
        "TRADING_URL=%s | key_set=%s | secret_set=%s | key_from=%s | secret_from=%s",
        TRADING_URL,
        bool(API_KEY),
        bool(API_SECRET),
        env_name_found(KEY_NAMES),
        env_name_found(SECRET_NAMES),
    )
    eq = get_equity()
    if eq <= 0:
        log.error("No equity (see Account API logs above)")
        return
    if not state:
        state.update(new_state(eq))
        save_state()
    elif not state.get("start_equity"):
        state["start_equity"] = eq
        save_state()
    reconcile()

    last = 0
    while True:
        try:
            ok, clock = api_get(f"{TRADING_URL}/v2/clock")
            if not ok or not (clock or {}).get("is_open"):
                time.sleep(20)
                continue

            if not roll_day():
                time.sleep(20)
                continue

            if time.time() - last > 30:
                reconcile()
                last = time.time()

            refresh_bias()
            check_daily_limit()

            if state.get("position"):
                manage_position()

            if (
                not state.get("halted")
                and not state.get("broker_uncertain")
                and not state.get("position")
                and not state.get("entry_intent")
            ):
                n = now_ny().time()
                if SIGNAL_START <= n <= SIGNAL_END:
                    sig = opening_range_signal()
                    if sig:
                        open_position(*sig)

            time.sleep(2)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.exception("Loop %s", e)
            time.sleep(5)

if __name__ == "__main__":
    run()