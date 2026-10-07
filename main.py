import os
import sys
import time
import json
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)
from alpaca.trading.enums import (
    AssetStatus,
    OrderSide,
    TimeInForce,
)
from alpaca.data.historical import (
    StockHistoricalDataClient,
    OptionHistoricalDataClient,
)
from alpaca.data.requests import (
    StockBarsRequest,
    OptionLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, OptionsFeed

# ============================================================
# CONFIG
# ============================================================
ALPACA_API_KEY = (
    os.getenv("APCA_API_KEY_ID")
    or os.getenv("ALPACA_API_KEY")
    or os.getenv("API_KEY")
)
ALPACA_SECRET_KEY = (
    os.getenv("APCA_API_SECRET_KEY")
    or os.getenv("ALPACA_SECRET_KEY")
    or os.getenv("SECRET_KEY")
)
if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    print("❌ Alpaca API credentials are missing.")
    sys.exit(1)

PAPER_MODE = True
if not PAPER_MODE:
    print("❌ This version is PAPER ONLY.")
    sys.exit(1)

STOCK_FEED = DataFeed.IEX
OPTIONS_FEED = OptionsFeed.INDICATIVE

TARGET_UNDERLYINGS = [
    "AAPL",
    "TSLA",
    "NVDA",
    "MSFT",
    "AMZN",
    "AMD",
]

# ============================================================
# TRADE SETTINGS
# ============================================================
FIXED_CONTRACTS_PER_TRADE = 10
TARGET_PROFIT_USD = 70.0
MAX_TRADES_PER_DAY = 5
MAX_OPEN_POSITIONS = 2

# ============================================================
# OPTION FILTERS
# ============================================================
MIN_DTE = 1
MAX_DTE = 14
MIN_DELTA = 0.40
MAX_DELTA = 0.60
MAX_PREMIUM = 15.00
MAX_SPREAD_PCT = 0.10
STOP_LOSS_PCT = 0.30
SIGNAL_THRESHOLD = 0.003
SCAN_INTERVAL_SECONDS = 60
ERROR_SLEEP_SECONDS = 30
STATE_FILE = "smart_options_bot_state.json"

# ============================================================
# CLIENTS
# ============================================================
trading_client = TradingClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    paper=True,
)
stock_data_client = StockHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
)
option_data_client = OptionHistoricalDataClient(
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
)

# ============================================================
# STATE
# ============================================================
def default_state():
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "trades_today": 0,
        "estimated_daily_pnl": 0.0,
        "positions": {},
    }

def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        if state.get("date") != today:
            state = default_state()
        if "positions" not in state:
            state["positions"] = {}
        return state
    except Exception as e:
        print(f"⚠️ State load error: {e}")
        return default_state()

state = load_state()

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"⚠️ State save error: {e}")

# ============================================================
# MARKET TIME
# ============================================================
ET = ZoneInfo("America/New_York")

def is_market_open():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return dt_time(9, 30) <= now.time() <= dt_time(16, 0)

def reset_daily_state():
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state["date"] = today
        state["trades_today"] = 0
        state["estimated_daily_pnl"] = 0.0
        save_state()

# ============================================================
# UNDERLYING MOMENTUM ANALYSIS (بدون أخطاء استيراد)
# ============================================================
def analyze_underlying(symbol):
    try:
        end = datetime.now(ET)
        start = end - timedelta(minutes=15)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
            end=end,
            feed=STOCK_FEED,
        )
        response = stock_data_client.get_stock_bars(request)
        if not response or symbol not in response:
            return {"direction": None, "move_pct": 0, "reason": "No price data"}
        
        bars = response[symbol]
        if len(bars) < 2:
            return {"direction": None, "move_pct": 0, "reason": "Not enough bars"}
        
        first_price = float(bars[0].close)
        last_price = float(bars[-1].close)
        move_pct = (last_price - first_price) / first_price

        if move_pct >= SIGNAL_THRESHOLD:
            return {
                "direction": "CALL",
                "move_pct": move_pct,
                "reason": f"Bullish momentum {move_pct:+.2%}",
            }
        if move_pct <= -SIGNAL_THRESHOLD:
            return {
                "direction": "PUT",
                "move_pct": move_pct,
                "reason": f"Bearish momentum {move_pct:+.2%}",
            }
        return {
            "direction": None,
            "move_pct": move_pct,
            "reason": f"No clear signal {move_pct:+.2%}",
        }
    except Exception as e:
        return {"direction": None, "move_pct": 0, "reason": str(e)}

# ============================================================
# GET OPTION CONTRACTS & QUOTES
# ============================================================
def get_option_contracts(symbol):
    try:
        request = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            limit=500,
        )
        response = trading_client.get_option_contracts(request)
        if hasattr(response, "option_contracts"):
            return response.option_contracts
        return response
    except Exception as e:
        print(f"⚠️ Contracts error {symbol}: {e}")
        return []

def get_option_quote(option_symbol):
    try:
        request = OptionLatestQuoteRequest(
            symbol_or_symbols=option_symbol,
            feed=OPTIONS_FEED,
        )
        response = option_data_client.get_option_latest_quote(request)
        if not response or option_symbol not in response:
            return None
        quote = response[option_symbol]
        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
        if bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid
        return {"bid": bid, "ask": ask, "mid": mid, "spread_pct": spread_pct}
    except Exception:
        return None

def select_smart_option(symbol, direction):
    try:
        contracts = get_option_contracts(symbol)
        if not contracts:
            return None
        today = datetime.now(ET).date()
        candidates = []
        for contract in contracts:
            try:
                expiration = contract.expiration_date
                if isinstance(expiration, str):
                    expiration = datetime.fromisoformat(expiration).date()
                dte = (expiration - today).days
                if dte < MIN_DTE or dte > MAX_DTE:
                    continue
                contract_type = str(contract.type).upper()
                if direction == "CALL" and "CALL" not in contract_type:
                    continue
                elif direction == "PUT" and "PUT" not in contract_type:
                    continue
                
                strike = float(contract.strike_price)
                candidates.append((contract, dte, strike))
            except Exception:
                continue

        if not candidates:
            return None

        valid_options = []
        for contract, dte, strike in candidates:
            option_symbol = contract.symbol
            quote = get_option_quote(option_symbol)
            if not quote:
                continue
            mid = quote["mid"]
            spread_pct = quote["spread_pct"]
            if mid <= 0 or mid > MAX_PREMIUM or spread_pct > MAX_SPREAD_PCT:
                continue

            delta = getattr(contract, "delta", None)
            if delta is not None:
                try:
                    delta = abs(float(delta))
                    if delta < MIN_DELTA or delta > MAX_DELTA:
                        continue
                except Exception:
                    pass

            valid_options.append({
                "contract": contract,
                "symbol": option_symbol,
                "mid": mid,
                "bid": quote["bid"],
                "ask": quote["ask"],
                "spread_pct": spread_pct,
                "dte": dte,
                "strike": strike,
                "delta": delta,
            })

        if not valid_options:
            return None

        valid_options.sort(key=lambda x: (x["spread_pct"], abs(x["dte"] - 7)))
        return valid_options[0]
    except Exception as e:
        print(f"⚠️ Option selection error: {e}")
        return None

def wait_for_fill(order_id, timeout=20):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            order = trading_client.get_order_by_id(order_id)
            status = str(order.status).lower()
            if status == "filled":
                return order
            if status in ["canceled", "cancelled", "expired", "rejected"]:
                return None
        except Exception:
            pass
        time.sleep(1)
    return None

def enter_position(symbol, direction):
    if state["trades_today"] >= MAX_TRADES_PER_DAY or len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return False

    option_info = select_smart_option(symbol, direction)
    if not option_info:
        return False

    option_symbol = option_info["symbol"]
    ask = option_info["ask"]
    qty = FIXED_CONTRACTS_PER_TRADE

    try:
        order = LimitOrderRequest(
            symbol=option_symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(ask, 2),
        )
        submitted = trading_client.submit_order(order)
        order_id = str(submitted.id)
        
        filled_order = wait_for_fill(order_id, timeout=20)
        if not filled_order:
            try:
                trading_client.cancel_order_by_id(order_id)
            except Exception:
                pass
            return False

        fill_price = float(filled_order.filled_avg_price)
        filled_qty = float(getattr(filled_order, "filled_qty", qty) or qty)

        state["trades_today"] += 1
        state["positions"][option_symbol] = {
            "underlying": symbol,
            "direction": direction,
            "qty": filled_qty,
            "entry_price": fill_price,
            "opened_at": datetime.now(ET).isoformat(),
            "order_id": order_id,
        }
        save_state()
        print(f"✅ FILLED | Option: {option_symbol} | Qty: {filled_qty} | Entry: ${fill_price:.2f}")
        return True
    except Exception as e:
        print(f"❌ Entry error: {e}")
        return False

def exit_position(option_symbol, pos, current_price, reason):
    try:
        qty = float(pos["qty"])
        order = MarketOrderRequest(
            symbol=option_symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        submitted = trading_client.submit_order(order)
        filled_order = wait_for_fill(str(submitted.id), timeout=20)
        if not filled_order:
            return False

        exit_price = float(filled_order.filled_avg_price)
        entry_price = float(pos["entry_price"])
        pnl_usd = (exit_price - entry_price) * qty * 100

        state["estimated_daily_pnl"] += pnl_usd
        state["positions"].pop(option_symbol, None)
        save_state()
        print(f"🛑 EXIT FILLED | Option: {option_symbol} | P&L: ${pnl_usd:+.2f} | Reason: {reason}")
        return True
    except Exception as e:
        print(f"❌ Exit error {option_symbol}: {e}")
        return False

def sync_positions():
    try:
        alpaca_positions = trading_client.get_all_positions()
        real_positions = {p.symbol: float(p.qty) for p in alpaca_positions if len(p.symbol) >= 15 and ("C" in p.symbol or "P" in p.symbol)}
        for symbol in list(state["positions"].keys()):
            if symbol not in real_positions:
                state["positions"].pop(symbol, None)
        save_state()
    except Exception as e:
        print(f"⚠️ Position sync error: {e}")

def manage_positions():
    if not state["positions"]:
        return
    for option_symbol, pos in list(state["positions"].items()):
        try:
            quote = get_option_quote(option_symbol)
            if not quote:
                continue
            current_price = quote["mid"]
            entry_price = float(pos["entry_price"])
            qty = float(pos["qty"])
            
            current_pnl_usd = (current_price - entry_price) * qty * 100
            current_pnl_pct = (current_price - entry_price) / entry_price

            print(f"📊 {option_symbol} | Entry=${entry_price:.2f} | Now=${current_price:.2f} | P&L=${current_pnl_usd:+.2f}")

            if current_pnl_usd >= TARGET_PROFIT_USD:
                exit_position(option_symbol, pos, current_price, "TARGET PROFIT REACHED")
                continue

            if current_pnl_pct <= -STOP_LOSS_PCT:
                exit_position(option_symbol, pos, current_price, "STOP LOSS")
                continue
        except Exception as e:
            print(f"⚠️ Management error {option_symbol}: {e}")

def main():
    print("=" * 70)
    print("🤖 SMART OPTIONS BOT V6.1 - RUNNING")
    print("=" * 70)
    while True:
        try:
            reset_daily_state()
            sync_positions()
            manage_positions()

            if not is_market_open():
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            if len(state["positions"]) >= MAX_OPEN_POSITIONS or state["trades_today"] >= MAX_TRADES_PER_DAY:
                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            for symbol in TARGET_UNDERLYINGS:
                if any(p["underlying"] == symbol for p in state["positions"].values()):
                    continue
                analysis = analyze_underlying(symbol)
                direction = analysis["direction"]
                if direction in ["CALL", "PUT"]:
                    if enter_position(symbol, direction):
                        break
                time.sleep(1)

            time.sleep(SCAN_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ MAIN ERROR: {e}")
            time.sleep(ERROR_SLEEP_SECONDS)

if __name__ == "__main__":
    main()
