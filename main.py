# -*- coding: utf-8 -*-
"""
=========================================================
صَفاء التحليل v75.2 (MASTERMIND ENGINE - INSTITUTIONAL)
SPY 0-DTE | PAPER TRADING ONLY
=========================================================
Upgrades in v75.2:
1. Proper Slippage Guard with immediate Cancel of pending/partial orders.
2. Theta Decay Hard Cutoff at 14:30 NY (force exit of non-performing positions).
3. Safer Fill Price handling (fallback if filled_avg_price is missing).
=========================================================
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

VERSION = "v75.2"
SYMBOL = "SPY"
NY = ZoneInfo("America/New_York")

def env_float(name, default): return float(os.getenv(name, default))
def env_int(name, default): return int(os.getenv(name, default))

# Risk & Sizing
RISK_PER_TRADE = env_float("RISK_PER_TRADE", "0.015")
MAX_PREMIUM_PCT = env_float("MAX_PREMIUM_PCT", "0.10")
DAILY_LOSS_LIMIT = env_float("DAILY_LOSS_LIMIT", "0.04")
ATR_MULTIPLIER = env_float("ATR_MULTIPLIER", "1.5")
COMMISSION_PER_CONTRACT = 0.65

# Circuit Breakers & Cooldowns
LOSS_PAUSE_MINUTES = env_int("LOSS_PAUSE_MINUTES", 30)
MAX_CONSEC_LOSSES = env_int("MAX_CONSEC_LOSSES", 2)
ENTRY_COOLDOWN_MINUTES = env_int("ENTRY_COOLDOWN_MINUTES", 15)
BIAS_REFRESH_MINUTES = 15

# Opening Range Filters
OR_MAX_AGE_MINUTES = 90
OR_MIN_WIDTH = 0.50
OR_MAX_WIDTH = 5.50

# Profit Runner Controls
BREAKEVEN_TRIGGER = env_float("BREAKEVEN_TRIGGER", "0.25")
TRAIL_ACTIVATION = env_float("TRAIL_ACTIVATION", "0.45")
TRAIL_DISTANCE = env_float("TRAIL_DISTANCE", "0.15")

SCALP_BREAKEVEN = env_float("SCALP_BREAKEVEN", "0.12")
SCALP_TRAIL_ACTIVATION = env_float("SCALP_TRAIL_ACTIVATION", "0.18")
SCALP_TRAIL_DISTANCE = env_float("SCALP_TRAIL_DISTANCE", "0.10")
SCALP_TIME_LIMIT_MINS = env_int("SCALP_TIME_LIMIT_MINS", 45)

# Entry Quality & Execution
VOLUME_MULT = env_float("VOLUME_MULT", "1.25")
MAX_SPREAD_PCT = env_float("MAX_SPREAD_PCT", "0.08")
MIN_OPTION_ASK = env_float("MIN_OPTION_ASK", "0.25")
DELTA_PROXY_PCT = env_float("DELTA_PROXY_PCT", "0.003")
MAX_ENTRY_SLIPPAGE = 0.08
LIMIT_TIMEOUT = 4

# Market Timing
SIGNAL_START = dtime(9, 45)
SIGNAL_END = dtime(14, 00)
THETA_HARD_CUTOFF = dtime(14, 30)   # Force exit of non-performing positions
EOD_CLOSE = dtime(15, 45)

API_KEY = (os.getenv("ALPACA_API_KEY") or "").strip()
API_SECRET = (os.getenv("ALPACA_API_SECRET") or "").strip()
TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets").rstrip("/")
DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")
STOCK_FEED = "iex"
OPTIONS_FEED = "indicative"

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "safaa_v75_state.json"
TRADES_FILE = DATA_DIR / "safaa_v75_journal.csv"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
    "Content-Type": "application/json"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("SAFAA")

if "paper" not in TRADING_URL.lower():
    raise RuntimeError("CRITICAL: v75 is PAPER ONLY.")

# =========================================================
# 2. CORE HELPERS & STATE
# =========================================================

def now_ny():
    return datetime.now(NY)

def today():
    return now_ny().date().isoformat()

def iso_now():
    return now_ny().isoformat(timespec="seconds")

def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(NY)

def api_with_retry(method, url, params=None, body=None, max_retries=3):
    backoff = 0.5
    for attempt in range(max_retries):
        try:
            res = requests.request(method, url, headers=HEADERS, params=params, json=body, timeout=8)
            data = res.json() if res.content else {}
            if res.ok:
                return True, data
            if res.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff)
                backoff *= 2
                continue
            return False, data
        except Exception:
            time.sleep(backoff)
            backoff *= 2
    return False, {}

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
        "realized_pnl": 0.0,
        "net_pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "consec_losses": 0,
        "halted": False,
        "pause_until": None,
        "last_entry_time": None,
        "last_signal_bar": None,
        "bias": "neutral",
        "bias_score": 0,
        "last_bias_refresh": None,
        "atr": 0.0,
        "position": None
    }

state = load_state()

# =========================================================
# 3. TECHNICALS
# =========================================================

def calculate_vwap(bars):
    pv = volume = 0.0
    for b in bars:
        typical = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        vol = float(b["v"])
        pv += typical * vol
        volume += vol
    return pv / volume if volume else None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains = [max(0, prices[i] - prices[i-1]) for i in range(1, len(prices))]
    losses = [max(0, prices[i-1] - prices[i]) for i in range(1, len(prices))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(prices)-1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

def calculate_intraday_atr(bars, period=30):
    if len(bars) < period + 1:
        return None
    tr = []
    for i in range(1, len(bars)):
        h = float(bars[i]["h"])
        l = float(bars[i]["l"])
        prev_c = float(bars[i-1]["c"])
        tr.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(tr[-period:]) / period

# =========================================================
# 4. ROBUST BIAS ENGINE
# =========================================================

def get_stock_bars(symbol, start, end, timeframe="1Min"):
    bars, token = [], None
    for _ in range(5):
        params = {
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 1000,
            "feed": STOCK_FEED
        }
        if token:
            params["page_token"] = token
        ok, data = api_with_retry("GET", f"{DATA_URL}/v2/stocks/{symbol}/bars", params=params)
        if not ok:
            break
        bars.extend(data.get("bars", []))
        token = data.get("next_page_token")
        if not token:
            break
    return bars

def session_bars():
    n = now_ny()
    start = n.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    end = n.replace(second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    return get_stock_bars(SYMBOL, start, end)

def get_trend_score():
    n = now_ny()
    start_d = (n - timedelta(days=30)).astimezone(ZoneInfo("UTC"))
    end_d = n.astimezone(ZoneInfo("UTC"))
    bars = get_stock_bars(SYMBOL, start_d, end_d, "1Day")
    closes = [float(b["c"]) for b in bars if parse_time(b["t"]).date() < n.date()]
    if len(closes) < 20:
        return 0
    ma20 = sum(closes[-20:]) / 20
    return 1 if closes[-1] > ma20 else -1

def refresh_bias():
    n = now_ny()
    if state.get("last_bias_refresh"):
        last_time = datetime.fromisoformat(state["last_bias_refresh"])
        if (n - last_time).total_seconds() < BIAS_REFRESH_MINUTES * 60:
            return

    prev_bars = get_stock_bars(
        SYMBOL,
        (n - timedelta(days=5)).astimezone(ZoneInfo("UTC")),
        n.astimezone(ZoneInfo("UTC")),
        "1Day"
    )
    if not prev_bars:
        return
    prev_close = float(prev_bars[-1]["c"])

    pm_start = n.replace(hour=4, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    pm_end = n.replace(hour=9, minute=29, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    premarket = get_stock_bars(SYMBOL, pm_start, pm_end, "1Min")
    pm_vol = sum(float(b["v"]) for b in premarket) if premarket else 0
    ref_price = float(premarket[-1]["c"]) if premarket else prev_close

    gap_pct = (ref_price - prev_close) / prev_close
    trend = get_trend_score()

    score = 0
    if gap_pct > 0.0015:
        score += 1
    elif gap_pct < -0.0015:
        score -= 1

    if pm_vol > 1_000_000 and gap_pct > 0:
        score += 1
    elif pm_vol > 1_000_000 and gap_pct < 0:
        score -= 1

    score += trend

    bars = session_bars()
    atr_val = calculate_intraday_atr(bars, period=30) if bars else (prev_close * 0.001)
    if bars:
        vwap = calculate_vwap(bars)
        last_close = float(bars[-1]["c"])
        if vwap:
            if last_close > vwap * 1.0015:
                score += 0.5
            elif last_close < vwap * 0.9985:
                score -= 0.5

    bias = "neutral"
    if score >= 2:
        bias = "bull"
    elif score <= -2:
        bias = "bear"

    state["bias"] = bias
    state["bias_score"] = score
    state["atr"] = atr_val or 1.0
    state["last_bias_refresh"] = iso_now()
    save_state()
    log.info("📊 Bias: %s | Score: %+.1f | Gap: %.2f%% | PM Vol: %d", bias, score, gap_pct * 100, pm_vol)

# =========================================================
# 5. OPTIONS & ORDERS
# =========================================================

def get_option_quote(symbol):
    ok, data = api_with_retry(
        "GET",
        f"{DATA_URL}/v1beta1/options/quotes/latest",
        params={"symbols": symbol, "feed": OPTIONS_FEED}
    )
    if not ok:
        return None
    quote = (data.get("quotes") or {}).get(symbol)
    if not quote:
        return None
    b = float(quote.get("bp", 0))
    a = float(quote.get("ap", 0))
    return (b, a) if (b > 0 and a > 0 and a > b) else None

def choose_contract(direction, underlying):
    target_strike = underlying * (1 + DELTA_PROXY_PCT) if direction == "up" else underlying * (1 - DELTA_PROXY_PCT)
    opt_type = "call" if direction == "up" else "put"

    params = {
        "underlying_symbols": SYMBOL,
        "status": "active",
        "type": opt_type,
        "expiration_date": today(),
        "limit": 20,
        "strike_price_gte": f"{target_strike - 2:.2f}",
        "strike_price_lte": f"{target_strike + 2:.2f}"
    }
    ok, data = api_with_retry("GET", f"{TRADING_URL}/v2/options/contracts", params=params)
    if not ok or not data.get("option_contracts"):
        return None, None

    contracts = data["option_contracts"]
    contracts.sort(key=lambda x: abs(float(x["strike_price"]) - target_strike))

    for c in contracts:
        symbol = c["symbol"]
        quote = get_option_quote(symbol)
        if not quote:
            continue
        bid, ask = quote
        mid = (ask + bid) / 2
        spread = (ask - bid) / mid
        if spread <= MAX_SPREAD_PCT and ask >= MIN_OPTION_ASK:
            return symbol, spread
    return None, None

def submit_limit_order(symbol, qty, side, price):
    ok, data = api_with_retry("POST", f"{TRADING_URL}/v2/orders", body={
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "limit",
        "limit_price": f"{price:.2f}",
        "time_in_force": "day"
    })
    return data.get("id") if ok else None

def cancel_order(order_id):
    """Force cancel any pending order."""
    if order_id:
        api_with_retry("DELETE", f"{TRADING_URL}/v2/orders/{order_id}")

def wait_for_order(order_id, timeout=LIMIT_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, order = api_with_retry("GET", f"{TRADING_URL}/v2/orders/{order_id}")
        if ok and order.get("status") in ("filled", "canceled", "expired", "rejected"):
            return order
        time.sleep(0.5)
    # Timeout → cancel
    cancel_order(order_id)
    time.sleep(0.5)
    ok, order = api_with_retry("GET", f"{TRADING_URL}/v2/orders/{order_id}")
    return order if ok else None

def safe_fill_price(order, fallback_price):
    """Safely extract filled_avg_price with fallback."""
    if not order:
        return fallback_price
    raw = order.get("filled_avg_price")
    if raw is None or raw == "" or raw == "0" or raw == 0:
        return fallback_price
    try:
        price = float(raw)
        return price if price > 0 else fallback_price
    except (TypeError, ValueError):
        return fallback_price

# =========================================================
# 6. SIGNAL & FILTERS
# =========================================================

def is_cooling_down():
    if state.get("pause_until"):
        if now_ny() < datetime.fromisoformat(state["pause_until"]):
            return True
        state["pause_until"] = None
        save_state()

    last_entry = state.get("last_entry_time")
    if last_entry:
        if (now_ny() - datetime.fromisoformat(last_entry)).total_seconds() < ENTRY_COOLDOWN_MINUTES * 60:
            return True
    return False

def opening_range_signal():
    if is_cooling_down():
        return None

    n = now_ny()
    market_open = n.replace(hour=9, minute=30, second=0, microsecond=0)

    if (n - market_open).total_seconds() > (OR_MAX_AGE_MINUTES * 60):
        return None

    bars = session_bars()
    if len(bars) < 35:
        return None

    range_end = market_open + timedelta(minutes=30)
    opening = [b for b in bars if parse_time(b["t"]) < range_end]
    after = [b for b in bars if parse_time(b["t"]) >= range_end]
    if len(opening) < 10 or not after:
        return None

    last = after[-1]
    if state.get("last_signal_bar") == last["t"]:
        return None

    c = float(last["c"])
    or_high = max(float(b["h"]) for b in opening)
    or_low = min(float(b["l"]) for b in opening)
    range_size = or_high - or_low

    if range_size < OR_MIN_WIDTH or range_size > OR_MAX_WIDTH:
        return None

    vwap = calculate_vwap(bars)
    rsi = calculate_rsi([float(b["c"]) for b in bars])
    bias = state.get("bias")
    vol_ratio = float(last["v"]) / (sum(float(b["v"]) for b in bars[-10:-1]) / 9) if len(bars) > 10 else 1.0

    meta = {
        "vwap": vwap,
        "rsi": rsi,
        "vol_ratio": vol_ratio,
        "bias": bias
    }

    # Trend Mode
    if bias in ("bull", "bear") and vol_ratio >= VOLUME_MULT:
        if c > or_high and c > vwap and bias == "bull":
            return "up", c, "trend", meta
        if c < or_low and c < vwap and bias == "bear":
            return "down", c, "trend", meta

    # Scalp Mode
    if bias == "neutral" and rsi is not None and vwap:
        vwap_dist = abs(c - vwap) / vwap
        if vwap_dist < 0.004:
            if c >= (or_high - range_size * 0.20) and rsi > 65:
                return "down", c, "scalp", meta
            if c <= (or_low + range_size * 0.20) and rsi < 35:
                return "up", c, "scalp", meta

    return None

# =========================================================
# 7. EXECUTION, RECONCILIATION & JOURNAL
# =========================================================

def get_equity():
    ok, acc = api_with_retry("GET", f"{TRADING_URL}/v2/account")
    return float(acc["equity"]) if ok else 0.0

def roll_day():
    if state.get("date") == today():
        return True
    if state.get("position"):
        log.error("🛑 Position carried overnight. Halting.")
        state["halted"] = True
        save_state()
        return False
    eq = get_equity()
    state.clear()
    state.update(new_state(eq))
    save_state()
    log.info("🌅 NEW TRADING DAY | Equity: $%.2f", eq)
    return True

def reconcile_broker():
    ok, positions = api_with_retry("GET", f"{TRADING_URL}/v2/positions")
    if not ok:
        return
    opts = [
        p for p in positions
        if p.get("asset_class") == "us_option"
        or (str(p.get("symbol", "")).startswith("SPY") and len(str(p.get("symbol", ""))) > 10)
    ]
    local = state.get("position")
    if local is None and opts:
        log.error("🛑 Unknown broker position found. Halting.")
        state["halted"] = True
        save_state()
    elif local is not None:
        match = [p for p in opts if p["symbol"] == local["contract"]]
        if not match or int(float(match[0]["qty"])) != local["qty_open"]:
            log.error("🛑 Position mismatch. Halting.")
            state["halted"] = True
            save_state()

def write_journal(pos):
    file_exists = TRADES_FILE.exists()
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow([
                "Time", "Contract", "Side", "Mode", "Qty", "Entry", "Exit",
                "Realized_PnL", "Net_PnL", "Peak_Gain%", "Bias", "RSI", "VWAP",
                "Vol_Ratio", "Spread", "Slippage", "Duration_Mins", "Exit_Reason"
            ])

        entry_t = datetime.fromisoformat(pos["entry_time"])
        duration = round((now_ny() - entry_t).total_seconds() / 60.0, 1)
        net = pos["realized_pnl"] - (pos["qty"] * COMMISSION_PER_CONTRACT * 2)
        peak_gain = ((pos["peak"] - pos["entry"]) / pos["entry"]) * 100 if pos["entry"] else 0

        w.writerow([
            iso_now(), pos["contract"], pos["direction"], pos["mode"], pos["qty"],
            round(pos["entry"], 2), round(pos["exit_price"], 2),
            round(pos["realized_pnl"], 2), round(net, 2), round(peak_gain, 1),
            pos["meta"].get("bias"),
            round(pos["meta"].get("rsi", 0) or 0, 1),
            round(pos["meta"].get("vwap", 0) or 0, 2),
            round(pos["meta"].get("vol_ratio", 0) or 0, 2),
            round(pos["meta"].get("spread", 0) or 0, 3),
            round(pos["slippage"], 3),
            duration,
            pos["exit_reason"]
        ])

def open_position(direction, underlying, mode, meta):
    if state.get("position") is not None:
        return

    equity = float(state.get("start_equity", 100000))
    contract, spread = choose_contract(direction, underlying)
    if not contract:
        return

    quote = get_option_quote(contract)
    if not quote:
        return
    bid, ask = quote

    atr = state.get("atr", underlying * 0.001)
    stop_dist_underlying = ATR_MULTIPLIER * atr
    option_stop_dist = stop_dist_underlying * 0.45
    dynamic_stop_pct = max(0.18, min(0.38, option_stop_dist / ask))

    qty = int(min(
        (equity * RISK_PER_TRADE) // (ask * 100 * dynamic_stop_pct),
        (equity * MAX_PREMIUM_PCT) // (ask * 100)
    ))
    if qty < 1:
        return

    order_id = submit_limit_order(contract, qty, "buy", ask)
    if not order_id:
        return

    order = wait_for_order(order_id, LIMIT_TIMEOUT)

    # ========== IMPROVED SLIPPAGE GUARD ==========
    if not order or float(order.get("filled_qty", 0) or 0) == 0:
        log.warning("⚠️ Buy limit timed out / not filled. Order cancelled.")
        cancel_order(order_id)
        return

    fill_price = safe_fill_price(order, ask)
    filled_qty = int(float(order.get("filled_qty") or 0))
    slippage = fill_price - ask

    if slippage > MAX_ENTRY_SLIPPAGE:
        log.warning("⚠️ Entry rejected due to high slippage: %.3f. Cancelling any residual.", slippage)
        cancel_order(order_id)

        # If somehow partially/fully filled despite high slippage, force close immediately
        if filled_qty > 0:
            log.warning("⚠️ Partial/Full fill detected with bad slippage → Emergency close.")
            api_with_retry("POST", f"{TRADING_URL}/v2/orders", body={
                "symbol": contract,
                "qty": str(filled_qty),
                "side": "sell",
                "type": "market",
                "time_in_force": "day"
            })
        return
    # ============================================

    meta["spread"] = spread
    state["position"] = {
        "contract": contract,
        "direction": direction,
        "mode": mode,
        "qty": filled_qty,
        "qty_open": filled_qty,
        "entry": fill_price,
        "peak": fill_price,
        "stop": fill_price * (1 - dynamic_stop_pct),
        "trail_active": False,
        "breakeven_active": False,
        "realized_pnl": 0.0,
        "exit_price": 0.0,
        "entry_time": iso_now(),
        "meta": meta,
        "slippage": slippage,
        "exit_reason": None
    }
    state["trades"] += 1
    state["last_entry_time"] = iso_now()
    state["last_signal_bar"] = iso_now()
    save_state()
    log.info("🎯 OPEN [%s] | Qty: %d | Fill: $%.2f | Stop: $%.2f | Slip: %.3f",
             mode.upper(), filled_qty, fill_price, state["position"]["stop"], slippage)

def manage_position():
    pos = state.get("position")
    if not pos:
        return

    quote = get_option_quote(pos["contract"])
    if not quote:
        return
    bid, ask = quote
    entry = pos["entry"]

    if bid > pos["peak"]:
        pos["peak"] = bid

    gain = (bid - entry) / entry
    peak_gain = (pos["peak"] - entry) / entry

    mode = pos["mode"]
    is_scalp = (mode == "scalp")

    trig_be = SCALP_BREAKEVEN if is_scalp else BREAKEVEN_TRIGGER
    trig_tr = SCALP_TRAIL_ACTIVATION if is_scalp else TRAIL_ACTIVATION
    dist_tr = SCALP_TRAIL_DISTANCE if is_scalp else TRAIL_DISTANCE

    if now_ny().time() >= dtime(13, 0):
        dist_tr *= 0.6

    if gain >= trig_be and not pos["breakeven_active"]:
        pos["stop"] = max(pos["stop"], entry * 1.05)
        pos["breakeven_active"] = True
        log.info("[%s] 🛡️ BREAKEVEN ACTIVATED", mode.upper())

    if peak_gain >= trig_tr and not pos["trail_active"]:
        pos["trail_active"] = True
        log.info("[%s] 🏃 TRAIL ACTIVATED", mode.upper())

    if pos["trail_active"]:
        pos["stop"] = max(pos["stop"], entry, pos["peak"] * (1 - dist_tr))

    reason = None
    duration_mins = (now_ny() - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 60.0
    current_time = now_ny().time()

    # ========== THETA DECAY HARD CUTOFF ==========
    if current_time >= THETA_HARD_CUTOFF:
        # Force exit if position is not meaningfully profitable
        if gain < 0.15:  # Less than +15% → exit to avoid late theta crush
            reason = "THETA HARD CUTOFF (14:30)"
    # ============================================

    if reason is None:
        if current_time >= EOD_CLOSE:
            reason = "EOD DECAY"
        elif bid <= pos["stop"]:
            reason = "TRAILING STOP" if pos["trail_active"] else "STOP LOSS"
        elif is_scalp and duration_mins > SCALP_TIME_LIMIT_MINS and gain < trig_be:
            reason = "TIME STOP"

    if reason:
        order_id = submit_limit_order(pos["contract"], pos["qty_open"], "sell", bid)
        order = wait_for_order(order_id, LIMIT_TIMEOUT)

        exit_price = bid
        if not order or float(order.get("filled_qty", 0) or 0) == 0:
            log.warning("⚠️ Exit limit timed out → EMERGENCY MARKET SELL")
            api_with_retry("POST", f"{TRADING_URL}/v2/orders", body={
                "symbol": pos["contract"],
                "qty": str(pos["qty_open"]),
                "side": "sell",
                "type": "market",
                "time_in_force": "day"
            })
            time.sleep(1.5)
            # After market order we still use bid as approximation
            exit_price = bid
        else:
            exit_price = safe_fill_price(order, bid)

        pnl = (exit_price - entry) * 100 * pos["qty_open"]
        pos["realized_pnl"] += pnl
        pos["exit_price"] = exit_price
        pos["exit_reason"] = reason

        net = pnl - (pos["qty"] * COMMISSION_PER_CONTRACT * 2)
        state["net_pnl"] += net

        if net > 0:
            state["wins"] += 1
            state["consec_losses"] = 0
        else:
            state["consec_losses"] += 1
            if state["consec_losses"] >= MAX_CONSEC_LOSSES:
                state["pause_until"] = (now_ny() + timedelta(minutes=LOSS_PAUSE_MINUTES)).isoformat()
                log.warning("🛑 Max consecutive losses reached. Paused %d mins.", LOSS_PAUSE_MINUTES)

        write_journal(pos)
        state["position"] = None

        win_rate = (state["wins"] / state["trades"]) * 100 if state["trades"] > 0 else 0
        log.info("🏁 CLOSED [%s] | Net: $%.2f | Reason: %s | WR: %.1f%%",
                 pos["contract"], net, reason, win_rate)

        if state["net_pnl"] <= -(float(state["start_equity"]) * DAILY_LOSS_LIMIT):
            state["halted"] = True
            log.error("🛑 DAILY LOSS LIMIT HIT. Trading halted.")

    save_state()

# =========================================================
# 8. MAIN ENGINE
# =========================================================

def run():
    log.info("🚀 SAFAA %s | Paper Only", VERSION)
    if not get_equity():
        log.error("Cannot fetch account equity.")

    last_recon = 0
    while True:
        try:
            ok, clock = api_with_retry("GET", f"{TRADING_URL}/v2/clock")
            if not ok or not clock.get("is_open"):
                time.sleep(20)
                continue

            if not roll_day():
                time.sleep(20)
                continue

            if time.time() - last_recon > 60:
                reconcile_broker()
                last_recon = time.time()

            refresh_bias()

            if state.get("position"):
                manage_position()

            if not state.get("halted") and not state.get("position"):
                n = now_ny().time()
                if SIGNAL_START <= n <= SIGNAL_END:
                    sig = opening_range_signal()
                    if sig:
                        open_position(*sig)

            time.sleep(2)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("Loop Exception: %s", e)
            time.sleep(5)

if __name__ == "__main__":
    run()