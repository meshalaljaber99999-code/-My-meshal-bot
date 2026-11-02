# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v71.1 (OPTIMIZED ENGINE)
SPY 0-DTE | PAPER TRADING ONLY
=========================================================

- No fixed daily trade count.
- One open position at a time.
- Dynamic market bias (Alpaca Native Data).
- Opening Range Breakout + VWAP + Volume Ratio.
- Smart Profit Runner:
    +30% : breakeven protection
    +50% : trailing activation
    20%  : trailing drawdown from peak
- Theta Protection: No new entries after 14:00 NY time.
- Fast execution retry (4s limit timeout).
- Broker position reconciliation.
- Experimental paper-trading system.
"""

import os
import csv
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests


# =========================================================
# 1. CONFIGURATION
# =========================================================

VERSION = "v71.1"
SYMBOL = "SPY"
NY = ZoneInfo("America/New_York")

def env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (ValueError, TypeError):
        return float(default)

def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (ValueError, TypeError):
        return int(default)

def env_bool(name, default="false"):
    return str(os.getenv(name, default)).lower() in ("true", "1", "yes", "on")


# Risk Controls
RISK_PER_TRADE = env_float("RISK_PER_TRADE", "0.01")
MAX_PREMIUM_PCT = env_float("MAX_PREMIUM_PCT", "0.10")
DAILY_LOSS_LIMIT = env_float("DAILY_LOSS_LIMIT", "0.03")
INITIAL_STOP = env_float("INITIAL_STOP", "0.30")

# Profit Runner Controls
BREAKEVEN_TRIGGER = env_float("BREAKEVEN_TRIGGER", "0.30")
TRAIL_ACTIVATION = env_float("TRAIL_ACTIVATION", "0.50")
TRAIL_DISTANCE = env_float("TRAIL_DISTANCE", "0.20")

# Entry Quality & Filters
VOLUME_MULT = env_float("VOLUME_MULT", "1.20")
MAX_SPREAD_PCT = env_float("MAX_SPREAD_PCT", "0.08")
MIN_OPTION_ASK = env_float("MIN_OPTION_ASK", "0.30")

# Trade Frequency & Circuit Breakers
MAX_OPEN_POSITIONS = 1
LOSS_PAUSE_MINUTES = 30
MAX_CONSEC_LOSSES = 2
ENTRY_COOLDOWN_MINUTES = 15

# Market Timing (New York)
SIGNAL_START = dtime(9, 45)
SIGNAL_END = dtime(14, 00)  # Cutoff at 14:00 to prevent 0-DTE Theta Decay
EOD_CLOSE = dtime(15, 45)

# Strategy Windows
OPEN_RANGE_MINUTES = 30
BIAS_REFRESH_MINUTES = 15

# Fast Execution Timeout for 0-DTE
LIMIT_ORDER_TIMEOUT = 4  # Reduced from 12s to prevent high slippage

# API Credentials
API_KEY = (os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID") or "").strip()
API_SECRET = (os.getenv("ALPACA_API_SECRET") or os.getenv("APCA_API_SECRET_KEY") or "").strip()

TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")

STOCK_FEED = os.getenv("ALPACA_STOCK_FEED", "iex")
OPTIONS_FEED = os.getenv("ALPACA_OPTIONS_FEED", "indicative")

# Hard Block for Live Trading
ALLOW_LIVE = False

if "paper" not in TRADING_URL.lower():
    raise RuntimeError("Safaa v71.1 is PAPER ONLY. Use https://paper-api.alpaca.markets")

if env_bool("ALLOW_LIVE", "false"):
    raise RuntimeError("Live trading is disabled in this paper version.")

# File Paths
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "safaa_v71_state.json"
TRADES_FILE = DATA_DIR / "safaa_v71_trades.csv"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("SAFAA")


# =========================================================
# 2. GENERAL HELPERS & STATE MANAGEMENT
# =========================================================

def now_ny():
    return datetime.now(NY)

def today():
    return now_ny().date().isoformat()

def iso_now():
    return now_ny().isoformat(timespec="seconds")

def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(NY)

def api(method, url, params=None, body=None):
    try:
        response = requests.request(
            method, url, headers=HEADERS, params=params, json=body, timeout=10
        )
        try:
            data = response.json()
        except Exception:
            data = {}

        if not response.ok:
            log.warning("API error %s: %s", response.status_code, str(data)[:300])

        return response.ok, data
    except Exception as exc:
        log.error("API connection error: %s", exc)
        return False, {}

def save_state():
    tmp = Path(str(STATE_FILE) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return {}

def new_state(equity):
    return {
        "date": today(),
        "start_equity": equity,
        "realized_pnl": 0.0,
        "consec_losses": 0,
        "trades_today": 0,
        "halted": False,
        "pause_until": None,
        "last_entry_time": None,
        "last_signal_bar": None,
        "bias": None,
        "bias_score": 0,
        "bias_updated": None,
        "position": None,
    }

state = load_state()


# =========================================================
# 3. ACCOUNT AND MARKET CLOCK
# =========================================================

def get_account():
    ok, data = api("GET", f"{TRADING_URL}/v2/account")
    return data if ok else None

def get_equity():
    account = get_account()
    if not account:
        return 0.0
    try:
        return float(account["equity"])
    except Exception:
        return 0.0

def market_is_open():
    ok, data = api("GET", f"{TRADING_URL}/v2/clock")
    return bool(data.get("is_open")) if ok else False


# =========================================================
# 4. STOCK MARKET DATA (ALPACA DATA API)
# =========================================================

def get_stock_bars(symbol, start, end, timeframe="1Min"):
    results = []
    token = None

    for _ in range(10):
        params = {
            "timeframe": timeframe,
            "start": start.astimezone(ZoneInfo("UTC")).isoformat(),
            "end": end.astimezone(ZoneInfo("UTC")).isoformat(),
            "limit": 1000,
            "feed": STOCK_FEED,
            "adjustment": "raw",
        }
        if token:
            params["page_token"] = token

        ok, data = api("GET", f"{DATA_URL}/v2/stocks/{symbol}/bars", params=params)

        if not ok:
            break

        results.extend(data.get("bars", []))
        token = data.get("next_page_token")

        if not token:
            break

    return results

def session_bars():
    n = now_ny()
    start = n.replace(hour=9, minute=30, second=0, microsecond=0)
    bars = get_stock_bars(SYMBOL, start, n)

    current_minute = n.replace(second=0, microsecond=0)
    return [bar for bar in bars if parse_time(bar["t"]) < current_minute]

def get_previous_close(symbol=SYMBOL):
    n = now_ny()
    bars = get_stock_bars(symbol, n - timedelta(days=12), n, "1Day")
    previous = [b for b in bars if parse_time(b["t"]).date() < n.date()]

    if not previous:
        return None
    return float(previous[-1]["c"])

def calculate_vwap(bars):
    pv = 0.0
    volume = 0.0

    for bar in bars:
        typical = (float(bar["h"]) + float(bar["l"]) + float(bar["c"])) / 3.0
        vol = float(bar["v"])
        pv += typical * vol
        volume += vol

    return pv / volume if volume else None


# =========================================================
# 5. MARKET TREND & BIAS (REPLACED YAHOO WITH ALPACA API)
# =========================================================

def trend_score():
    n = now_ny()
    bars = get_stock_bars(SYMBOL, n - timedelta(days=60), n, "1Day")
    closes = [float(b["c"]) for b in bars if parse_time(b["t"]).date() < n.date()]

    if len(closes) < 20:
        return 0

    average = sum(closes[-20:]) / 20
    return 1 if closes[-1] > average else -1

def calculate_bias():
    previous_close = get_previous_close(SYMBOL)
    if not previous_close:
        return "neutral", 0

    n = now_ny()
    premarket = get_stock_bars(
        SYMBOL,
        n.replace(hour=4, minute=0, second=0, microsecond=0),
        n.replace(hour=9, minute=29, second=0, microsecond=0),
        "1Min"
    )

    if premarket:
        reference = float(premarket[-1]["c"])
    else:
        bars = session_bars()
        if not bars:
            return "neutral", 0
        reference = float(bars[0]["o"])

    gap = (reference - previous_close) / previous_close
    trend = trend_score()

    score = 0

    if gap > 0.0015:
        score += 1
    elif gap < -0.0015:
        score -= 1

    score += trend

    if score >= 2:
        return "bull", score
    if score <= -2:
        return "bear", score

    return "neutral", score

def refresh_bias():
    last = state.get("bias_updated")
    if last:
        try:
            previous = datetime.fromisoformat(last)
            if now_ny() - previous < timedelta(minutes=BIAS_REFRESH_MINUTES):
                return
        except Exception:
            pass

    bias, score = calculate_bias()
    state["bias"] = bias
    state["bias_score"] = score
    state["bias_updated"] = iso_now()
    save_state()

    log.info("🧭 Market bias: %s | Score: %+d", bias, score)


# =========================================================
# 6. OPTIONS CONTRACTS AND QUOTES
# =========================================================

def get_option_quote(symbol):
    ok, data = api("GET", f"{DATA_URL}/v1beta1/options/quotes/latest", params={"symbols": symbol, "feed": OPTIONS_FEED})
    if not ok:
        return None

    quote = (data.get("quotes") or {}).get(symbol)
    if not quote:
        return None

    try:
        bid = float(quote["bp"])
        ask = float(quote["ap"])
    except Exception:
        return None

    if bid <= 0 or ask <= 0 or ask < bid:
        return None

    timestamp = quote.get("t")
    if timestamp:
        try:
            quote_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            age = (datetime.now(ZoneInfo("UTC")) - quote_time).total_seconds()
            if age > 60:  # Tightened max quote age to 60s
                return None
        except Exception:
            pass

    return bid, ask

def choose_contract(direction, underlying):
    option_type = "call" if direction == "up" else "put"
    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "type": option_type,
        "expiration_date": today(),
        "strike_price_gte": f"{underlying - 3:.2f}",
        "strike_price_lte": f"{underlying + 3:.2f}",
        "limit": 100,
    }

    ok, data = api("GET", f"{TRADING_URL}/v2/options/contracts", params=params)
    if not ok:
        return None

    contracts = data.get("option_contracts", [])
    if not contracts:
        return None

    contracts.sort(key=lambda item: abs(float(item["strike_price"]) - underlying))

    for contract in contracts[:15]:
        symbol = contract["symbol"]
        quote = get_option_quote(symbol)
        if not quote:
            continue

        bid, ask = quote
        midpoint = (bid + ask) / 2
        if midpoint <= 0:
            continue

        spread = (ask - bid) / midpoint
        if spread <= MAX_SPREAD_PCT and ask >= MIN_OPTION_ASK:
            return symbol

    return None


# =========================================================
# 7. ORDERS & FAST EXECUTION
# =========================================================

def submit_limit_order(symbol, quantity, side, price):
    body = {
        "symbol": symbol,
        "qty": str(quantity),
        "side": side,
        "type": "limit",
        "limit_price": f"{price:.2f}",
        "time_in_force": "day",
    }
    ok, data = api("POST", f"{TRADING_URL}/v2/orders", body=body)
    return data.get("id") if ok else None

def get_order(order_id):
    ok, data = api("GET", f"{TRADING_URL}/v2/orders/{order_id}")
    return data if ok else None

def cancel_order(order_id):
    api("DELETE", f"{TRADING_URL}/v2/orders/{order_id}")

def wait_for_order(order_id, timeout=LIMIT_ORDER_TIMEOUT):
    deadline = time.time() + timeout

    while time.time() < deadline:
        order = get_order(order_id)
        if not order:
            time.sleep(0.5)
            continue

        status = order.get("status")
        if status in ("filled", "canceled", "expired", "rejected"):
            return order

        time.sleep(0.5)

    cancel_order(order_id)
    time.sleep(0.5)
    return get_order(order_id)


# =========================================================
# 8. BROKER RECONCILIATION & RISK
# =========================================================

def broker_option_positions():
    ok, data = api("GET", f"{TRADING_URL}/v2/positions")
    if not ok:
        return None

    return [
        p for p in data
        if p.get("asset_class") == "us_option"
        or str(p.get("symbol", "")).startswith("SPY")
        and len(str(p.get("symbol", ""))) >= 15
    ]

def reconcile_broker():
    positions = broker_option_positions()
    if positions is None:
        state["halted"] = True
        save_state()
        log.error("Cannot verify broker positions. Trading halted.")
        return False

    local = state.get("position")

    if local is None and positions:
        state["halted"] = True
        save_state()
        log.error("Unknown option position found at broker. Manual reconciliation required.")
        return False

    if local is not None:
        matching = [p for p in positions if p.get("symbol") == local.get("contract")]
        if not matching:
            state["halted"] = True
            save_state()
            log.error("Saved position not found at broker. Trading halted.")
            return False

        broker_qty = abs(int(float(matching[0].get("qty", 0))))
        if broker_qty != int(local.get("qty_open", 0)):
            state["halted"] = True
            save_state()
            log.error("Position quantity mismatch. Trading halted for manual review.")
            return False

    return True

def write_trade(pos, reason):
    new_file = not TRADES_FILE.exists()
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if new_file:
            writer.writerow(["time", "contract", "direction", "quantity", "entry", "realized_pnl", "peak", "peak_gain_pct", "reason"])

        entry = float(pos["entry"])
        peak = float(pos["peak"])
        peak_gain = ((peak - entry) / entry * 100) if entry else 0

        writer.writerow([
            iso_now(), pos["contract"], pos["direction"], pos["qty"],
            entry, pos["realized_pnl"], peak, peak_gain, reason
        ])

def unrealized_pnl(pos=None, quote=None):
    if pos is None:
        pos = state.get("position")
    if not pos:
        return 0.0

    if quote is None:
        quote = get_option_quote(pos["contract"])
    if not quote:
        return 0.0

    bid = quote[0]
    return (bid - pos["entry"]) * 100 * pos["qty_open"]

def daily_pnl(pos=None, quote=None):
    if pos is None:
        pos = state.get("position")

    result = float(state.get("realized_pnl", 0))
    if pos:
        result += float(pos.get("realized_pnl", 0))
        result += unrealized_pnl(pos, quote)

    return result

def check_daily_risk(pos=None, quote=None):
    equity = float(state.get("start_equity", 0))
    if equity <= 0:
        state["halted"] = True
        save_state()
        return True

    pnl = daily_pnl(pos, quote)
    loss_limit = equity * DAILY_LOSS_LIMIT

    if pnl <= -loss_limit:
        if not state.get("halted"):
            log.error("DAILY LOSS LIMIT REACHED: %.2f", pnl)
        state["halted"] = True
        save_state()

    return bool(state.get("halted"))

def entry_cooldown_active():
    last = state.get("last_entry_time")
    if not last:
        return False
    try:
        last_time = datetime.fromisoformat(last)
        return now_ny() - last_time < timedelta(minutes=ENTRY_COOLDOWN_MINUTES)
    except Exception:
        return False

def pause_active():
    pause = state.get("pause_until")
    if not pause:
        return False
    try:
        until = datetime.fromisoformat(pause)
        if now_ny() < until:
            return True
        state["pause_until"] = None
        save_state()
        return False
    except Exception:
        return False


# =========================================================
# 9. ENTRY SIGNALS & POSITION MANAGEMENT
# =========================================================

def opening_range_signal():
    bars = session_bars()
    if len(bars) < OPEN_RANGE_MINUTES + 3:
        return None

    n = now_ny()
    range_end = n.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=OPEN_RANGE_MINUTES)

    opening = [b for b in bars if parse_time(b["t"]) < range_end]
    after = [b for b in bars if parse_time(b["t"]) >= range_end]

    if len(opening) < 10 or not after:
        return None

    last = after[-1]
    if state.get("last_signal_bar") == last["t"]:
        return None

    close = float(last["c"])
    high = max(float(b["h"]) for b in opening)
    low = min(float(b["l"]) for b in opening)

    vwap = calculate_vwap(bars)
    if not vwap:
        return None

    # Adjusted recent window to 15 bars for finer volume comparison
    recent = bars[-16:-1]
    if len(recent) < 10:
        return None

    average_volume = sum(float(b["v"]) for b in recent) / len(recent)
    latest_volume = float(last["v"])

    volume_ok = (average_volume > 0 and latest_volume >= VOLUME_MULT * average_volume)
    if not volume_ok:
        return None

    bias = state.get("bias")

    if close > high and close > vwap and bias == "bull":
        state["last_signal_bar"] = last["t"]
        save_state()
        return "up", close

    if close < low and close < vwap and bias == "bear":
        state["last_signal_bar"] = last["t"]
        save_state()
        return "down", close

    return None

def open_position(direction, underlying):
    if state.get("position") is not None:
        return

    equity = get_equity()
    if equity <= 0:
        return

    contract = choose_contract(direction, underlying)
    if not contract:
        log.info("No suitable option contract.")
        return

    quote = get_option_quote(contract)
    if not quote:
        return

    bid, ask = quote
    midpoint = (bid + ask) / 2
    if midpoint <= 0:
        return

    spread = (ask - bid) / midpoint
    if spread > MAX_SPREAD_PCT:
        log.info("Rejected: spread too wide.")
        return

    risk_dollars = equity * RISK_PER_TRADE
    risk_per_contract = ask * 100 * INITIAL_STOP
    if risk_per_contract <= 0:
        return

    qty_risk = int(risk_dollars // risk_per_contract)
    premium_budget = equity * MAX_PREMIUM_PCT
    qty_budget = int(premium_budget // (ask * 100))

    quantity = min(qty_risk, qty_budget)
    if quantity < 1:
        log.info("Rejected: position size below one contract.")
        return

    order_id = submit_limit_order(contract, quantity, "buy", ask)
    if not order_id:
        return

    order = wait_for_order(order_id)
    if not order:
        return

    filled_qty = int(float(order.get("filled_qty") or 0))
    if filled_qty <= 0:
        log.info("Buy order not filled within limit timeout.")
        return

    average_fill = float(order.get("filled_avg_price") or ask)

    state["position"] = {
        "contract": contract,
        "direction": direction,
        "qty": filled_qty,
        "qty_open": filled_qty,
        "entry": average_fill,
        "peak": average_fill,
        "stop": average_fill * (1 - INITIAL_STOP),
        "trail_active": False,
        "breakeven_active": False,
        "realized_pnl": 0.0,
        "entry_time": iso_now(),
        "exit_reason": None,
        "pending_exit_order": None,
    }

    state["trades_today"] += 1
    state["last_entry_time"] = iso_now()
    save_state()

    log.info("NEW POSITION | %s | %s | qty=%s | entry=%.2f", direction, contract, filled_qty, average_fill)

def close_position(pos, reason, quote=None):
    if pos["qty_open"] <= 0:
        return True

    if pos.get("pending_exit_order"):
        return False

    if quote is None:
        quote = get_option_quote(pos["contract"])

    if not quote:
        log.warning("No valid quote for exit.")
        return False

    bid, ask = quote
    order_id = submit_limit_order(pos["contract"], pos["qty_open"], "sell", bid)

    if not order_id:
        return False

    pos["pending_exit_order"] = order_id
    save_state()

    order = wait_for_order(order_id)
    pos["pending_exit_order"] = None

    if not order:
        save_state()
        return False

    filled = int(float(order.get("filled_qty") or 0))
    if filled <= 0:
        save_state()
        return False

    average_exit = float(order.get("filled_avg_price") or bid)
    pnl = (average_exit - pos["entry"]) * 100 * filled

    pos["realized_pnl"] += pnl
    pos["qty_open"] -= filled
    pos["last_exit"] = average_exit

    log.info("SELL | %s | qty=%d | exit=%.2f | pnl=%+.2f | reason=%s", pos["contract"], filled, average_exit, pnl, reason)
    save_state()

    return pos["qty_open"] <= 0

def finish_position(reason):
    pos = state.get("position")
    if not pos or pos["qty_open"] > 0:
        return

    pnl = float(pos["realized_pnl"])
    state["realized_pnl"] += pnl

    if pnl < 0:
        state["consec_losses"] += 1
    else:
        state["consec_losses"] = 0

    write_trade(pos, reason)
    log.info("TRADE CLOSED | PNL=%+.2f | DAILY=%+.2f", pnl, state["realized_pnl"])

    state["position"] = None

    if state["consec_losses"] >= MAX_CONSEC_LOSSES:
        state["pause_until"] = (now_ny() + timedelta(minutes=LOSS_PAUSE_MINUTES)).isoformat()
        log.warning("Trading paused after consecutive losses.")

    check_daily_risk()
    save_state()

def manage_position():
    pos = state.get("position")
    if not pos:
        return

    pending_id = pos.get("pending_exit_order")
    if pending_id:
        order = get_order(pending_id)
        if not order:
            state["halted"] = True
            save_state()
            return

        status = order.get("status")
        if status not in ("filled", "canceled", "expired", "rejected"):
            return

        filled = int(float(order.get("filled_qty") or 0))
        if filled > 0:
            average_exit = float(order.get("filled_avg_price") or 0)
            if average_exit > 0:
                pnl = (average_exit - pos["entry"]) * 100 * filled
                pos["realized_pnl"] += pnl
                pos["qty_open"] -= filled

        pos["pending_exit_order"] = None
        save_state()

        if pos["qty_open"] <= 0:
            finish_position(pos.get("exit_reason") or "EXIT")
            return

    quote = get_option_quote(pos["contract"])
    if not quote:
        log.warning("Option quote unavailable; position retained.")
        return

    bid, ask = quote
    entry = float(pos["entry"])
    if entry <= 0:
        state["halted"] = True
        save_state()
        return

    if bid > pos["peak"]:
        pos["peak"] = bid

    gain = (bid - entry) / entry
    peak_gain = (pos["peak"] - entry) / entry

    if gain >= BREAKEVEN_TRIGGER and not pos["breakeven_active"]:
        pos["stop"] = max(pos["stop"], entry)
        pos["breakeven_active"] = True
        log.info("BREAKEVEN PROTECTION ACTIVATED.")

    if peak_gain >= TRAIL_ACTIVATION and not pos["trail_active"]:
        pos["trail_active"] = True
        log.info("RUNNER ACTIVATED | PEAK GAIN=%.1f%%", peak_gain * 100)

    if pos["trail_active"]:
        candidate = pos["peak"] * (1 - TRAIL_DISTANCE)
        pos["stop"] = max(pos["stop"], entry, candidate)

    halted = check_daily_risk(pos, quote)
    n = now_ny()
    reason = None

    if n.time() >= EOD_CLOSE:
        reason = "END OF DAY"
    elif halted:
        reason = "DAILY LOSS LIMIT"
    elif bid <= pos["stop"]:
        reason = "SMART TRAILING PROFIT" if pos["trail_active"] else "STOP LOSS / BREAKEVEN"

    if reason is None and not pos["trail_active"]:
        bars = session_bars()
        if bars:
            vwap = calculate_vwap(bars)
            last_close = float(bars[-1]["c"])
            if vwap:
                if pos["direction"] == "up" and last_close < vwap:
                    reason = "VWAP REVERSAL"
                elif pos["direction"] == "down" and last_close > vwap:
                    reason = "VWAP REVERSAL"

    if reason:
        pos["exit_reason"] = reason
        completed = close_position(pos, reason, quote)
        if completed:
            finish_position(reason)

    save_state()
    log.info(
        "POSITION | bid=%.2f | entry=%.2f | peak=%.2f | stop=%.2f | gain=%+.1f%% | qty=%d",
        bid, entry, pos["peak"], pos["stop"], gain * 100, pos["qty_open"]
    )


# =========================================================
# 10. DAILY ROLL & PERMISSIONS
# =========================================================

def roll_day():
    global state
    if state.get("date") == today():
        return True

    if state.get("position"):
        state["halted"] = True
        save_state()
        log.error("Position carried into new date. Manual reconciliation required.")
        return False

    equity = get_equity()
    if equity <= 0:
        return False

    state = new_state(equity)
    save_state()
    log.info("NEW TRADING DAY | START EQUITY=%.2f", equity)
    return True

def can_enter():
    n = now_ny()

    if not (SIGNAL_START <= n.time() <= SIGNAL_END):
        return False

    if state.get("halted"):
        return False

    if state.get("position") is not None:
        return False

    if state.get("bias") not in ("bull", "bear"):
        return False

    if pause_active():
        return False

    if entry_cooldown_active():
        return False

    if check_daily_risk():
        return False

    return True


# =========================================================
# 11. MAIN ENGINE LOOP
# =========================================================

def run():
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Missing ALPACA_API_KEY or ALPACA_API_SECRET.")

    account = get_account()
    if not account:
        raise RuntimeError("Cannot connect to Alpaca Paper account.")

    equity = float(account.get("equity", 0))
    if equity <= 0:
        raise RuntimeError("Invalid account equity.")

    if not state:
        state.update(new_state(equity))
        save_state()

    log.info("SAFAA %s STARTED | PAPER ONLY | EQUITY=%.2f", VERSION, equity)

    if not reconcile_broker():
        log.error("Startup reconciliation failed. No new trades will be opened.")

    last_reconcile = 0

    while True:
        try:
            if not market_is_open():
                time.sleep(20)
                continue

            if not roll_day():
                time.sleep(20)
                continue

            if time.time() - last_reconcile >= 60:
                if not reconcile_broker():
                    log.error("Broker reconciliation failed.")
                last_reconcile = time.time()

            if state.get("position"):
                manage_position()

            if not state.get("halted"):
                refresh_bias()

            check_daily_risk()

            if can_enter():
                signal = opening_range_signal()
                if signal:
                    direction, underlying = signal
                    log.info("ENTRY SIGNAL | %s | SPY=%.2f", direction, underlying)
                    open_position(direction, underlying)

            time.sleep(3)  # Fast poll rate for options management

        except KeyboardInterrupt:
            log.warning("Bot stopped manually. Any open position remains at broker.")
            break
        except Exception:
            log.exception("Unexpected main-loop error.")
            time.sleep(10)


if __name__ == "__main__":
    run()
