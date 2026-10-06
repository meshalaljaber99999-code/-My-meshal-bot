import os
import sys
import time
import math
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
    ContractType,
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
    OptionChainRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, OptionsFeed


# ============================================================
# CONFIG
# ============================================================

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    print("❌ ALPACA_API_KEY / ALPACA_SECRET_KEY غير موجودة.")
    sys.exit(1)


# ------------------------------------------------------------
# SAFETY
# ------------------------------------------------------------

# Paper ONLY
PAPER_MODE = True

if not PAPER_MODE:
    print("❌ هذا الإصدار مخصص للـ PAPER فقط.")
    sys.exit(1)


# ------------------------------------------------------------
# DATA FEEDS
# ------------------------------------------------------------

STOCK_FEED = DataFeed.IEX

# Paper testing
OPTIONS_FEED = OptionsFeed.INDICATIVE


# ------------------------------------------------------------
# UNDERLYINGS
# ------------------------------------------------------------

# SPY يعمل
# SPX متوقف مؤقتاً لأن Alpaca لا يوفر SPX Spot
# المطلوب للإشارة بدون مصدر خارجي.
UNDERLYINGS = ["SPY"]

SPX_ENABLED = False


# ------------------------------------------------------------
# SIGNAL
# ------------------------------------------------------------

SIGNAL_LOOKBACK_MINUTES = 5

# 0.10% = 0.001
MIN_SIGNAL_MOVE = 0.0010


# ------------------------------------------------------------
# OPTION FILTERS
# ------------------------------------------------------------

MIN_DTE = 1
MAX_DTE = 14

MIN_DELTA = 0.40
MAX_DELTA = 0.60

MAX_PREMIUM_SPY = 15.00

MAX_SPREAD_PCT = 0.10

MIN_OPEN_INTEREST = 100


# ------------------------------------------------------------
# RISK
# ------------------------------------------------------------

CAPITAL_ALLOCATION_PCT = 0.01

DAILY_LOSS_LIMIT_PCT = 0.03

MAX_TRADES_PER_DAY = 5

MAX_OPEN_POSITIONS = 2

MAX_CONTRACTS_PER_TRADE = 5


# ------------------------------------------------------------
# EXIT
# ------------------------------------------------------------

STOP_LOSS_PCT = 0.30

TAKE_PROFIT_PCT = 0.60

TRAILING_STOP_PCT = 0.15


# ------------------------------------------------------------
# LOOP
# ------------------------------------------------------------

SCAN_INTERVAL_SECONDS = 60

ERROR_SLEEP_SECONDS = 30

SYMBOL_DELAY_SECONDS = 2


# ------------------------------------------------------------
# ORDER
# ------------------------------------------------------------

ORDER_FILL_TIMEOUT_SECONDS = 15

ORDER_FILL_CHECK_INTERVAL = 2


# ------------------------------------------------------------
# STATE
# ------------------------------------------------------------

STATE_FILE = "bot_state.json"


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
# ACCOUNT
# ============================================================

def print_account():
    try:
        account = trading_client.get_account()

        print(
            f"ACCOUNT | "
            f"Status={account.status} | "
            f"Equity=${float(account.equity):,.2f} | "
            f"BuyingPower=${float(account.buying_power):,.2f} | "
            f"OptionsLevel={getattr(account, 'options_trading_level', 'N/A')}"
        )

        return account

    except Exception as e:
        print(f"❌ Account error: {e}")
        return None


# ============================================================
# MARKET TIME
# ============================================================

ET = ZoneInfo("America/New_York")


def is_market_open():
    now = datetime.now(ET)

    if now.weekday() >= 5:
        return False

    market_open = dt_time(9, 30)
    market_close = dt_time(16, 0)

    return market_open <= now.time() <= market_close


# ============================================================
# SPY SIGNAL
# ============================================================

def get_spy_price_history():
    end = datetime.now(ET)
    start = end - timedelta(minutes=15)

    request = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=STOCK_FEED,
    )

    bars = stock_data_client.get_stock_bars(request)

    if bars is None:
        return []

    try:
        spy_bars = bars["SPY"]
    except Exception:
        return []

    return spy_bars


def get_spy_signal():
    try:
        bars = get_spy_price_history()

        if not bars or len(bars) < 2:
            return {
                "signal": None,
                "direction": None,
                "move": 0.0,
                "from_price": None,
                "to_price": None,
                "reason": "Not enough bars",
            }

        lookback = min(SIGNAL_LOOKBACK_MINUTES, len(bars) - 1)

        from_price = float(bars[-lookback - 1].close)
        to_price = float(bars[-1].close)

        if from_price <= 0:
            return {
                "signal": None,
                "direction": None,
                "move": 0.0,
                "from_price": from_price,
                "to_price": to_price,
                "reason": "Invalid price",
            }

        move = (to_price - from_price) / from_price

        if move >= MIN_SIGNAL_MOVE:
            return {
                "signal": "CALL",
                "direction": "UP",
                "move": move,
                "from_price": from_price,
                "to_price": to_price,
                "reason": "Threshold reached",
            }

        if move <= -MIN_SIGNAL_MOVE:
            return {
                "signal": "PUT",
                "direction": "DOWN",
                "move": move,
                "from_price": from_price,
                "to_price": to_price,
                "reason": "Threshold reached",
            }

        return {
            "signal": None,
            "direction": "UP" if move > 0 else "DOWN",
            "move": move,
            "from_price": from_price,
            "to_price": to_price,
            "reason": "Below threshold",
        }

    except Exception as e:
        return {
            "signal": None,
            "direction": None,
            "move": 0.0,
            "from_price": None,
            "to_price": None,
            "reason": f"Error: {e}",
        }


# ============================================================
# PRINT SIGNAL
# ============================================================

def print_signal(symbol, result):
    move = result.get("move", 0.0)

    direction = result.get("direction") or "-"

    from_price = result.get("from_price")
    to_price = result.get("to_price")

    signal = result.get("signal")

    if from_price is not None and to_price is not None:

        if signal:
            icon = "🟢" if signal == "CALL" else "🔴"

            print(
                f"{symbol}: {icon} SIGNAL={signal} | "
                f"Direction={direction} | "
                f"5m Move={move:+.3%} | "
                f"From=${from_price:.2f} "
                f"To=${to_price:.2f}"
            )

        else:
            icon = "⚪"

            needed = max(
                0,
                MIN_SIGNAL_MOVE - abs(move)
            )

            print(
                f"{symbol}: {icon} No signal | "
                f"Direction={direction} | "
                f"5m Move={move:+.3%} | "
                f"From=${from_price:.2f} "
                f"To=${to_price:.2f} | "
                f"{result.get('reason')} | "
                f"Need ≈ {needed:.3%} more movement"
            )

    else:
        print(
            f"{symbol}: ⚪ No signal | "
            f"{result.get('reason')}"
        )


# ============================================================
# OPTION CONTRACTS
# ============================================================

def get_option_chain(symbol):
    try:
        request = OptionChainRequest(
            underlying_symbol=symbol,
            type=None,
            feed=OPTIONS_FEED,
        )

        return option_data_client.get_option_chain(request)

    except Exception as e:
        print(f"⚠️ Option chain error for {symbol}: {e}")
        return {}


def get_contracts(symbol):
    try:
        request = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            limit=1000,
        )

        response = trading_client.get_option_contracts(request)

        if hasattr(response, "option_contracts"):
            return response.option_contracts

        return response

    except Exception as e:
        print(f"⚠️ Option contracts error for {symbol}: {e}")
        return []


# ============================================================
# OPTION QUOTE
# ============================================================

def get_option_quote(symbol):
    try:
        request = OptionLatestQuoteRequest(
            symbol_or_symbols=symbol,
            feed=OPTIONS_FEED,
        )

        response = option_data_client.get_option_latest_quote(request)

        quote = response[symbol]

        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)

        if bid <= 0 or ask <= 0:
            return None

        mid = (bid + ask) / 2

        spread_pct = (ask - bid) / mid if mid > 0 else 999

        return {
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": spread_pct,
        }

    except Exception:
        return None


# ============================================================
# OPTION SELECTION
# ============================================================

def select_option(symbol, direction):
    try:
        contracts = get_contracts(symbol)

        if not contracts:
            print(f"⚠️ No option contracts found for {symbol}")
            return None

        today = datetime.now(ET).date()

        candidates = []

        for contract in contracts:

            try:
                expiration = contract.expiration_date

                if isinstance(expiration, str):
                    expiration = datetime.fromisoformat(
                        expiration
                    ).date()

                dte = (expiration - today).days

                if dte < MIN_DTE or dte > MAX_DTE:
                    continue

                contract_type = str(
                    contract.type
                ).lower()

                if direction == "CALL":
                    if "call" not in contract_type:
                        continue

                elif direction == "PUT":
                    if "put" not in contract_type:
                        continue

                else:
                    continue

                strike = float(contract.strike_price)

                candidates.append(
                    (
                        contract,
                        dte,
                        strike,
                    )
                )

            except Exception:
                continue

        if not candidates:
            print(f"⚠️ No suitable {direction} contracts for {symbol}")
            return None

        # ----------------------------------------------------
        # Get underlying price
        # ----------------------------------------------------

        bars = get_spy_price_history()

        if not bars:
            return None

        underlying_price = float(bars[-1].close)

        # ----------------------------------------------------
        # Rank by closeness to ATM
        # ----------------------------------------------------

        candidates.sort(
            key=lambda x: (
                abs(x[2] - underlying_price),
                x[1],
            )
        )

        # ----------------------------------------------------
        # Check quotes
        # ----------------------------------------------------

        checked = 0

        for contract, dte, strike in candidates:

            if checked >= 100:
                break

            checked += 1

            symbol_option = contract.symbol

            quote = get_option_quote(symbol_option)

            if not quote:
                continue

            bid = quote["bid"]
            ask = quote["ask"]
            mid = quote["mid"]
            spread_pct = quote["spread_pct"]

            if mid <= 0:
                continue

            if mid > MAX_PREMIUM_SPY:
                continue

            if spread_pct > MAX_SPREAD_PCT:
                continue

            delta = None

            # Try to obtain delta if available
            try:
                chain = get_option_chain(symbol)

                if chain and symbol_option in chain:

                    item = chain[symbol_option]

                    if hasattr(item, "greeks") and item.greeks:

                        delta_value = getattr(
                            item.greeks,
                            "delta",
                            None
                        )

                        if delta_value is not None:
                            delta = abs(float(delta_value))

            except Exception:
                delta = None

            # If delta is available, enforce range
            if delta is not None:
                if delta < MIN_DELTA or delta > MAX_DELTA:
                    continue

            # Open interest
            oi = getattr(
                contract,
                "open_interest",
                None
            )

            if oi is not None:

                try:
                    if float(oi) < MIN_OPEN_INTEREST:
                        continue
                except Exception:
                    pass

            return {
                "contract": contract,
                "symbol": symbol_option,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_pct": spread_pct,
                "delta": delta,
                "dte": dte,
                "strike": strike,
            }

        print(
            f"⚠️ No option passed filters for {symbol} {direction}"
        )

        return None

    except Exception as e:
        print(f"⚠️ select_option error: {e}")
        return None


# ============================================================
# OPEN POSITIONS
# ============================================================

def get_open_option_positions():
    try:
        positions = trading_client.get_all_positions()

        result = []

        for position in positions:

            try:
                asset_class = str(
                    position.asset_class
                ).lower()

                if "option" in asset_class:
                    result.append(position)

            except Exception:
                continue

        return result

    except Exception as e:
        print(f"⚠️ Position error: {e}")
        return []


def get_open_position_count():
    return len(get_open_option_positions())


# ============================================================
# QUANTITY
# ============================================================

def calculate_quantity(option_mid):
    try:
        account = trading_client.get_account()

        equity = float(account.equity)

        allocation = (
            equity *
            CAPITAL_ALLOCATION_PCT
        )

        # Option multiplier = 100
        cost_per_contract = option_mid * 100

        if cost_per_contract <= 0:
            return 0

        quantity = math.floor(
            allocation / cost_per_contract
        )

        quantity = min(
            quantity,
            MAX_CONTRACTS_PER_TRADE
        )

        return max(quantity, 0)

    except Exception as e:
        print(f"⚠️ Quantity error: {e}")
        return 0


# ============================================================
# TRADE PERMISSION
# ============================================================

def can_trade():
    try:
        if state["trades_today"] >= MAX_TRADES_PER_DAY:
            print(
                f"⛔ Max trades reached "
                f"({MAX_TRADES_PER_DAY})"
            )
            return False

        open_positions = get_open_position_count()

        if open_positions >= MAX_OPEN_POSITIONS:
            print(
                f"⛔ Max open positions reached "
                f"({MAX_OPEN_POSITIONS})"
            )
            return False

        account = trading_client.get_account()

        equity = float(account.equity)

        max_daily_loss = (
            equity *
            DAILY_LOSS_LIMIT_PCT
        )

        if state["estimated_daily_pnl"] <= -max_daily_loss:
            print(
                f"⛔ Daily loss limit reached: "
                f"${state['estimated_daily_pnl']:.2f}"
            )
            return False

        return True

    except Exception as e:
        print(f"⚠️ can_trade error: {e}")
        return False


# ============================================================
# WAIT FOR FILL
# ============================================================

def wait_for_fill(order_id):
    start = time.time()

    while (
        time.time() - start
        < ORDER_FILL_TIMEOUT_SECONDS
    ):

        try:
            order = trading_client.get_order_by_id(
                order_id
            )

            status = str(order.status).lower()

            if status == "filled":
                return order

            if status in (
                "canceled",
                "cancelled",
                "rejected",
                "expired",
            ):
                return None

        except Exception:
            pass

        time.sleep(
            ORDER_FILL_CHECK_INTERVAL
        )

    # Timeout
    try:
        trading_client.cancel_order_by_id(
            order_id
        )
    except Exception:
        pass

    return None


# ============================================================
# ENTER POSITION
# ============================================================

def enter_position(symbol, direction):
    if not can_trade():
        return False

    option = select_option(
        symbol,
        direction
    )

    if not option:
        return False

    option_symbol = option["symbol"]

    mid = option["mid"]

    quantity = calculate_quantity(mid)

    if quantity <= 0:
        print(
            f"⚠️ Quantity=0 | "
            f"{option_symbol} | "
            f"Mid=${mid:.2f}"
        )
        return False

    print(
        f"🟡 ENTRY | "
        f"{symbol} | "
        f"{direction} | "
        f"{option_symbol} | "
        f"Qty={quantity} | "
        f"Mid=${mid:.2f}"
    )

    try:
        order = LimitOrderRequest(
            symbol=option_symbol,
            qty=quantity,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(mid, 2),
        )

        submitted = trading_client.submit_order(
            order
        )

        order_id = str(submitted.id)

        print(
            f"📤 Order submitted | "
            f"ID={order_id}"
        )

        filled_order = wait_for_fill(
            order_id
        )

        if not filled_order:

            print(
                f"⚪ Order NOT filled | "
                f"{option_symbol} | "
                f"Trade not counted"
            )

            return False

        # ----------------------------------------------------
        # Actual fill price
        # ----------------------------------------------------

        filled_qty = float(
            getattr(
                filled_order,
                "filled_qty",
                quantity
            )
            or quantity
        )

        filled_avg = getattr(
            filled_order,
            "filled_avg_price",
            None
        )

        if filled_avg is not None:
            entry_price = float(
                filled_avg
            )
        else:
            entry_price = mid

        # ----------------------------------------------------
        # Record only after fill
        # ----------------------------------------------------

        state["trades_today"] += 1

        state["positions"][option_symbol] = {
            "underlying": symbol,
            "direction": direction,
            "qty": filled_qty,
            "entry_price": entry_price,
            "highest_price": entry_price,
            "order_id": order_id,
            "opened_at": datetime.now(
                ET
            ).isoformat(),
        }

        save_state()

        print(
            f"✅ FILLED | "
            f"{option_symbol} | "
            f"Qty={filled_qty} | "
            f"Entry=${entry_price:.2f} | "
            f"TradesToday={state['trades_today']}"
        )

        return True

    except Exception as e:
        print(
            f"❌ Entry order error: {e}"
        )
        return False


# ============================================================
# EXIT POSITION
# ============================================================

def exit_position(
    position,
    reason,
    current_price,
):
    symbol = position.symbol

    try:
        qty = abs(
            float(position.qty)
        )

        if qty <= 0:
            return False

        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )

        submitted = trading_client.submit_order(
            order
        )

        order_id = str(submitted.id)

        print(
            f"📤 EXIT submitted | "
            f"{symbol} | "
            f"Qty={qty} | "
            f"Reason={reason}"
        )

        filled = wait_for_fill(
            order_id
        )

        if not filled:
            print(
                f"⚠️ EXIT not filled | "
                f"{symbol}"
            )
            return False

        avg_entry = float(
            position.avg_entry_price
        )

        filled_exit = getattr(
            filled,
            "filled_avg_price",
            None
        )

        if filled_exit is not None:
            exit_price = float(
                filled_exit
            )
        else:
            exit_price = current_price

        pnl = (
            exit_price - avg_entry
        ) * qty * 100

        state["estimated_daily_pnl"] += pnl

        state["positions"].pop(
            symbol,
            None
        )

        save_state()

        print(
            f"✅ EXIT FILLED | "
            f"{symbol} | "
            f"Entry=${avg_entry:.2f} | "
            f"Exit=${exit_price:.2f} | "
            f"P&L=${pnl:+.2f} | "
            f"Reason={reason}"
        )

        return True

    except Exception as e:
        print(
            f"❌ Exit error {symbol}: {e}"
        )
        return False


# ============================================================
# MANAGE POSITIONS
# ============================================================

def manage_positions():
    positions = get_open_option_positions()

    if not positions:
        return

    active_symbols = set()

    for position in positions:

        symbol = position.symbol

        active_symbols.add(symbol)

        try:
            avg_entry = float(
                position.avg_entry_price
            )

            qty = abs(
                float(position.qty)
            )

            quote = get_option_quote(
                symbol
            )

            if not quote:
                continue

            current_price = quote["mid"]

            pnl_pct = (
                current_price - avg_entry
            ) / avg_entry

            # ------------------------------------------------
            # State recovery
            # ------------------------------------------------

            if symbol not in state["positions"]:

                state["positions"][symbol] = {
                    "underlying": "UNKNOWN",
                    "direction": "UNKNOWN",
                    "qty": qty,
                    "entry_price": avg_entry,
                    "highest_price": avg_entry,
                    "recovered": True,
                    "opened_at": datetime.now(
                        ET
                    ).isoformat(),
                }

                save_state()

                print(
                    f"🔄 Recovered position | "
                    f"{symbol} | "
                    f"Entry=${avg_entry:.2f}"
                )

            position_state = state[
                "positions"
            ][symbol]

            highest_price = float(
                position_state.get(
                    "highest_price",
                    avg_entry
                )
            )

            if current_price > highest_price:
                highest_price = current_price

                position_state[
                    "highest_price"
                ] = highest_price

                save_state()

            # ------------------------------------------------
            # Stop Loss
            # ------------------------------------------------

            if pnl_pct <= -STOP_LOSS_PCT:

                exit_position(
                    position,
                    "STOP LOSS",
                    current_price,
                )

                continue

            # ------------------------------------------------
            # Take Profit
            # ------------------------------------------------

            if pnl_pct >= TAKE_PROFIT_PCT:

                exit_position(
                    position,
                    "TAKE PROFIT",
                    current_price,
                )

                continue

            # ------------------------------------------------
            # Trailing Stop
            # ------------------------------------------------

            if highest_price > avg_entry:

                trailing_stop = (
                    highest_price *
                    (1 - TRAILING_STOP_PCT)
                )

                if current_price <= trailing_stop:

                    exit_position(
                        position,
                        "TRAILING STOP",
                        current_price,
                    )

                    continue

            print(
                f"📊 POSITION | "
                f"{symbol} | "
                f"Entry=${avg_entry:.2f} | "
                f"Now=${current_price:.2f} | "
                f"P&L={pnl_pct:+.2%} | "
                f"High=${highest_price:.2f}"
            )

        except Exception as e:
            print(
                f"⚠️ Manage position error "
                f"{symbol}: {e}"
            )

    # --------------------------------------------------------
    # Remove stale state positions
    # --------------------------------------------------------

    stale = []

    for symbol in list(
        state["positions"].keys()
    ):
        if symbol not in active_symbols:
            stale.append(symbol)

    for symbol in stale:
        state["positions"].pop(
            symbol,
            None
        )

    if stale:
        save_state()


# ============================================================
# PROCESS SPY
# ============================================================

def process_spy():

    result = get_spy_signal()

    print_signal(
        "SPY",
        result
    )

    if result["signal"]:

        print(
            f"🚨 SPY SIGNAL DETECTED: "
            f"{result['signal']}"
        )

        enter_position(
            "SPY",
            result["signal"]
        )


# ============================================================
# SPX DISABLED
# ============================================================

def process_spx():

    if not SPX_ENABLED:
        return

    print(
        "⏸️ SPX disabled: "
        "No real SPX Spot feed configured."
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("🚀 TRADING BOT V3.7")
    print("=" * 70)

    print(
        "MODE      : PAPER ONLY"
    )

    print(
        "STOCK FEED: IEX"
    )

    print(
        "OPTIONS   : INDICATIVE"
    )

    print(
        "SPY       : ENABLED"
    )

    print(
        "SPX       : DISABLED"
    )

    print(
        f"Signal    : "
        f"{MIN_SIGNAL_MOVE:.2%} / "
        f"{SIGNAL_LOOKBACK_MINUTES}m"
    )

    print(
        f"SL        : "
        f"{STOP_LOSS_PCT:.0%}"
    )

    print(
        f"TP        : "
        f"{TAKE_PROFIT_PCT:.0%}"
    )

    print(
        f"Trailing  : "
        f"{TRAILING_STOP_PCT:.0%}"
    )

    print("=" * 70)

    print_account()

    print()

    while True:

        try:

            # ------------------------------------------------
            # Manage existing positions FIRST
            # ------------------------------------------------

            manage_positions()

            # ------------------------------------------------
            # Market closed
            # ------------------------------------------------

            if not is_market_open():

                print(
                    f"[{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"⏸️ Market closed"
                )

                time.sleep(
                    SCAN_INTERVAL_SECONDS
                )

                continue

            # ------------------------------------------------
            # SPY
            # ------------------------------------------------

            process_spy()

            time.sleep(
                SYMBOL_DELAY_SECONDS
            )

            # ------------------------------------------------
            # SPX
            # ------------------------------------------------

            process_spx()

            # ------------------------------------------------
            # Wait
            # ------------------------------------------------

            time.sleep(
                SCAN_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            print(
                "\n🛑 Bot stopped by user."
            )

            break

        except Exception as e:

            print(
                f"❌ MAIN ERROR: {e}"
            )

            time.sleep(
                ERROR_SLEEP_SECONDS
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()