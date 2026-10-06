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
    QueryOrderStatus,
)

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient

from alpaca.data.requests import (
    StockBarsRequest,
    OptionChainRequest,
)

from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import OptionsFeed


# ============================================================
#                      CONFIGURATION
# ============================================================

def clean_env(value):
    if not value:
        return value
    return "".join(c for c in value if ord(c) < 128).strip()


API_KEY = clean_env(os.environ.get("API_KEY"))
SECRET_KEY = clean_env(os.environ.get("SECRET_KEY"))

if not API_KEY or not SECRET_KEY:
    sys.exit("ERROR: API_KEY / SECRET_KEY غير موجودة.")

# ------------------------------------------------------------
# SAFETY
# ------------------------------------------------------------

# مهم جدًا:
# True = Paper Trading
# لا تغيّرها إلى False الآن.
PAPER_MODE = True

# بيانات الخيارات:
# INDICATIVE = متاحة بدون OPRA لكنها متأخرة/مشتقة.
# OPRA = البيانات الأفضل للتداول الحقيقي وتتطلب الاشتراك المناسب.
OPTIONS_FEED = OptionsFeed.INDICATIVE

# ------------------------------------------------------------
# CUSTOMER STRATEGY
# ------------------------------------------------------------
#
# هذه هي المنطقة التي سيقوم الـAI بتعديلها مستقبلًا.
#
# العميل لن يحتاج إلى تعديل منطق التداول نفسه.
#
# ------------------------------------------------------------

STRATEGY = {

    "name": "Customer Strategy V1",

    # الأدوات التي يسمح للروبوت بالتداول عليها
    "underlyings": [
        "SPY",
        "SPX"
    ],

    # ========================================================
    # SIGNAL
    # ========================================================

    "signal": {

        # AUTO:
        # ارتفاع السوق = CALL
        # انخفاض السوق = PUT
        #
        # في V1 نستخدم SPY كمرجع لاتجاه السوق،
        # حتى عندما تكون الصفقة على SPX.
        "mode": "AUTO",

        # عدد دقائق قياس الحركة
        "lookback_minutes": 1,

        # أقل حركة مطلوبة
        # 0.001 = 0.10%
        "minimum_move_pct": 0.001,

    },

    # ========================================================
    # OPTION SELECTION
    # ========================================================

    "option": {

        # مدة العقد
        "min_dte": 3,
        "max_dte": 14,

        # Delta
        "min_delta": 0.40,
        "max_delta": 0.60,

        # أقصى سعر للعقد
        "max_premium": 15.00,

        # أقصى Spread
        # 0.10 = 10%
        "max_spread_pct": 0.10,

        # أقل حجم تداول
        "min_volume": 10,

        # أقل Open Interest
        "min_open_interest": 100,

    },

    # ========================================================
    # RISK MANAGEMENT
    # ========================================================

    "risk": {

        # أقصى مخاطرة لكل صفقة من قيمة الحساب
        "risk_per_trade_pct": 1.0,

        # أقصى خسارة يومية
        "max_daily_loss_pct": 3.0,

        # أقصى عدد صفقات يوميًا
        "max_trades_per_day": 10,

        # أقصى عدد مراكز مفتوحة
        "max_open_positions": 2,

        # أقصى عدد عقود للصفقة الواحدة
        "max_contracts_per_trade": 5,

    },

    # ========================================================
    # EXIT
    # ========================================================

    "exit": {

        # وقف خسارة من سعر العقد
        "stop_loss_pct": 30.0,

        # هدف ربح
        "take_profit_pct": 60.0,

        # Trailing Stop
        "trailing_enabled": True,

        "trailing_stop_pct": 15.0,

    },

}


# ============================================================
#                      CLIENTS
# ============================================================

trading_client = TradingClient(
    API_KEY,
    SECRET_KEY,
    paper=PAPER_MODE
)

stock_data_client = StockHistoricalDataClient(
    API_KEY,
    SECRET_KEY
)

option_data_client = OptionHistoricalDataClient(
    API_KEY,
    SECRET_KEY
)


NY_TZ = ZoneInfo("America/New_York")


# ============================================================
#                      GLOBAL STATE
# ============================================================

trades_today = 0
daily_realized_pnl = 0.0
state_date = datetime.now(NY_TZ).date()

last_signal_time = {}


# ============================================================
#                      LOGGING
# ============================================================

def log(message):
    now = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


# ============================================================
#                      ACCOUNT
# ============================================================

def get_account():

    try:
        return trading_client.get_account()

    except Exception as e:

        log(f"ERROR account: {e}")

        return None


def get_equity():

    account = get_account()

    if not account:
        return 0.0

    try:
        return float(account.equity)

    except Exception:
        return 0.0


# ============================================================
#                      DAILY RESET
# ============================================================

def reset_daily_state_if_needed():

    global trades_today
    global daily_realized_pnl
    global state_date

    today = datetime.now(NY_TZ).date()

    if today != state_date:

        trades_today = 0
        daily_realized_pnl = 0.0
        state_date = today

        log("🔄 تم تصفير إحصائيات اليوم الجديد.")


# ============================================================
#                      MARKET HOURS
# ============================================================

def is_market_open():

    try:

        clock = trading_client.get_clock()

        return bool(clock.is_open)

    except Exception as e:

        log(f"⚠️ مشكلة قراءة Market Clock: {e}")

        return False


def is_safe_trading_time():

    now = datetime.now(NY_TZ)

    current = now.time()

    market_open = dt_time(9, 30)
    first_15_minutes_end = dt_time(9, 45)

    # تجنب أول 15 دقيقة
    if market_open <= current < first_15_minutes_end:

        return False

    return True


# ============================================================
#                  SPY MARKET DIRECTION
# ============================================================

def get_spy_direction():

    """
    نستخدم SPY كمرجع لاتجاه السوق.

    هذا مهم خصوصًا لـSPX لأن Alpaca لا توفر Spot SPX
    ضمن بيانات Index Options الحالية.
    """

    lookback_minutes = (
        STRATEGY["signal"]["lookback_minutes"]
    )

    now = datetime.now(NY_TZ)

    start = now - timedelta(
        minutes=lookback_minutes + 2
    )

    try:

        request = StockBarsRequest(
            symbol_or_symbols="SPY",
            timeframe=TimeFrame.Minute,
            start=start,
            end=now,
        )

        bars_response = stock_data_client.get_stock_bars(
            request
        )

        bars = bars_response.data.get("SPY", [])

        if len(bars) < 2:

            return "NEUTRAL", 0.0

        first_price = float(bars[0].close)
        last_price = float(bars[-1].close)

        if first_price <= 0:

            return "NEUTRAL", 0.0

        move_pct = (
            last_price - first_price
        ) / first_price

        threshold = (
            STRATEGY["signal"]["minimum_move_pct"]
        )

        if move_pct >= threshold:

            return "BULLISH", move_pct

        if move_pct <= -threshold:

            return "BEARISH", move_pct

        return "NEUTRAL", move_pct

    except Exception as e:

        log(f"⚠️ خطأ SPY direction: {e}")

        return "NEUTRAL", 0.0


# ============================================================
#                  OPTION CONTRACTS
# ============================================================

def get_contracts(
    underlying,
    direction
):

    today = datetime.now(NY_TZ).date()

    min_dte = STRATEGY["option"]["min_dte"]
    max_dte = STRATEGY["option"]["max_dte"]

    expiration_min = (
        today + timedelta(days=min_dte)
    )

    expiration_max = (
        today + timedelta(days=max_dte)
    )

    option_type = (
        ContractType.CALL
        if direction == "CALL"
        else ContractType.PUT
    )

    try:

        request = GetOptionContractsRequest(

            underlying_symbols=[underlying],

            status=AssetStatus.ACTIVE,

            expiration_date_gte=expiration_min,

            expiration_date_lte=expiration_max,

            type=option_type,

            limit=500,

        )

        response = trading_client.get_option_contracts(
            request
        )

        contracts = response.option_contracts

        return contracts

    except Exception as e:

        log(
            f"⚠️ خطأ جلب عقود {underlying}: {e}"
        )

        return []


# ============================================================
#                  OPTION CHAIN / GREEKS
# ============================================================

def get_option_chain(
    underlying,
    direction
):

    option_type = (
        ContractType.CALL
        if direction == "CALL"
        else ContractType.PUT
    )

    today = datetime.now(NY_TZ).date()

    min_dte = STRATEGY["option"]["min_dte"]
    max_dte = STRATEGY["option"]["max_dte"]

    expiration_min = (
        today + timedelta(days=min_dte)
    )

    expiration_max = (
        today + timedelta(days=max_dte)
    )

    try:

        request = OptionChainRequest(

            underlying_symbol=underlying,

            feed=OPTIONS_FEED,

            type=option_type,

            expiration_date_gte=expiration_min,

            expiration_date_lte=expiration_max,

        )

        chain = option_data_client.get_option_chain(
            request
        )

        return chain

    except Exception as e:

        log(
            f"⚠️ خطأ Option Chain {underlying}: {e}"
        )

        return {}


# ============================================================
#                  OPTION SELECTION
# ============================================================

def select_best_option(
    underlying,
    direction
):

    contracts = get_contracts(
        underlying,
        direction
    )

    if not contracts:

        log(
            f"❌ لا توجد عقود مناسبة لـ {underlying}"
        )

        return None

    chain = get_option_chain(
        underlying,
        direction
    )

    if not chain:

        log(
            f"❌ لا توجد Option Chain لـ {underlying}"
        )

        return None

    min_delta = STRATEGY["option"]["min_delta"]
    max_delta = STRATEGY["option"]["max_delta"]

    max_premium = STRATEGY["option"]["max_premium"]

    max_spread = STRATEGY["option"]["max_spread_pct"]

    min_volume = STRATEGY["option"]["min_volume"]

    min_oi = STRATEGY["option"]["min_open_interest"]

    candidates = []

    contract_map = {
        c.symbol: c
        for c in contracts
    }

    for symbol, snapshot in chain.items():

        contract = contract_map.get(symbol)

        if not contract:
            continue

        try:

            quote = snapshot.latest_quote

            greeks = snapshot.greeks

            if quote is None:
                continue

            if greeks is None:
                continue

            bid = float(
                quote.bid_price or 0
            )

            ask = float(
                quote.ask_price or 0
            )

            if bid <= 0 or ask <= 0:
                continue

            mid = (
                bid + ask
            ) / 2

            if ask > max_premium:
                continue

            spread_pct = (
                (ask - bid) / ask
            )

            if spread_pct > max_spread:
                continue

            delta = abs(
                float(greeks.delta or 0)
            )

            if not (
                min_delta
                <= delta
                <= max_delta
            ):
                continue

            volume = int(
                getattr(
                    snapshot,
                    "volume",
                    0
                ) or 0
            )

            open_interest = int(
                getattr(
                    snapshot,
                    "open_interest",
                    0
                ) or 0
            )

            if volume < min_volume:
                continue

            if open_interest < min_oi:
                continue

            expiration = contract.expiration_date

            if hasattr(expiration, "date"):

                expiration = expiration.date()

            dte = (
                expiration
                - datetime.now(NY_TZ).date()
            ).days

            # Delta midpoint
            target_delta = (
                min_delta
                + max_delta
            ) / 2

            delta_distance = abs(
                delta - target_delta
            )

            # نريد:
            # Delta قريب من الهدف
            # Spread صغير
            # OI عالي
            # Volume عالي

            score = 0

            score -= (
                delta_distance * 100
            )

            score -= (
                spread_pct * 100
            )

            score += math.log(
                max(open_interest, 1)
            )

            score += math.log(
                max(volume, 1)
            )

            candidates.append({

                "symbol": symbol,

                "underlying": underlying,

                "direction": direction,

                "bid": bid,

                "ask": ask,

                "mid": mid,

                "delta": delta,

                "gamma": float(
                    greeks.gamma or 0
                ),

                "theta": float(
                    greeks.theta or 0
                ),

                "vega": float(
                    greeks.vega or 0
                ),

                "iv": float(
                    getattr(
                        snapshot,
                        "implied_volatility",
                        0
                    ) or 0
                ),

                "volume": volume,

                "open_interest": open_interest,

                "expiration": str(
                    expiration
                ),

                "dte": dte,

                "strike": float(
                    contract.strike_price
                ),

                "score": score,

            })

        except Exception:

            continue

    if not candidates:

        log(
            f"❌ لم نجد عقدًا يطابق الفلاتر لـ {underlying}"
        )

        return None

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    best = candidates[0]

    log(
        "🎯 أفضل عقد:"
    )

    log(
        f"   {best['symbol']}"
    )

    log(
        f"   Type: {best['direction']}"
    )

    log(
        f"   Strike: {best['strike']}"
    )

    log(
        f"   DTE: {best['dte']}"
    )

    log(
        f"   Bid/Ask: "
        f"{best['bid']:.2f}/"
        f"{best['ask']:.2f}"
    )

    log(
        f"   Delta: {best['delta']:.3f}"
    )

    log(
        f"   IV: {best['iv']:.3f}"
    )

    log(
        f"   Volume: {best['volume']}"
    )

    log(
        f"   OI: {best['open_interest']}"
    )

    return best


# ============================================================
#                  RISK MANAGEMENT
# ============================================================

def get_open_option_positions():

    try:

        positions = (
            trading_client.get_all_positions()
        )

        options = []

        for position in positions:

            asset_class = str(
                getattr(
                    position,
                    "asset_class",
                    ""
                )
            ).lower()

            symbol = str(
                position.symbol
            )

            # Options symbols usually have a long OSI format.
            if (
                "option" in asset_class
                or len(symbol) >= 15
            ):

                options.append(position)

        return options

    except Exception as e:

        log(
            f"⚠️ خطأ قراءة المراكز: {e}"
        )

        return []


def calculate_quantity(
    option_price,
    equity
):

    risk_pct = (
        STRATEGY["risk"]
        ["risk_per_trade_pct"]
    )

    max_contracts = (
        STRATEGY["risk"]
        ["max_contracts_per_trade"]
    )

    risk_budget = (
        equity
        * risk_pct
        / 100
    )

    # Long option:
    # contract multiplier = 100
    cost_per_contract = (
        option_price * 100
    )

    if cost_per_contract <= 0:

        return 0

    # محافظ:
    # نعتبر كامل premium معرضًا للخسارة
    quantity = math.floor(
        risk_budget
        / cost_per_contract
    )

    quantity = min(
        quantity,
        max_contracts
    )

    return max(quantity, 0)


def risk_allows_trade():

    global trades_today
    global daily_realized_pnl

    equity = get_equity()

    if equity <= 0:

        return False

    risk = STRATEGY["risk"]

    max_daily_loss = (
        equity
        * risk["max_daily_loss_pct"]
        / 100
    )

    if daily_realized_pnl <= -max_daily_loss:

        log(
            "🛑 تم بلوغ Max Daily Loss."
        )

        return False

    if trades_today >= risk[
        "max_trades_per_day"
    ]:

        log(
            "🛑 تم بلوغ Max Trades/Day."
        )

        return False

    open_positions = (
        get_open_option_positions()
    )

    if len(open_positions) >= risk[
        "max_open_positions"
    ]:

        log(
            "🛑 Max Open Positions."
        )

        return False

    return True


# ============================================================
#                  ORDER EXECUTION
# ============================================================

def submit_buy(
    symbol,
    quantity,
    price
):

    try:

        order_request = LimitOrderRequest(

            symbol=symbol,

            qty=quantity,

            side=OrderSide.BUY,

            time_in_force=TimeInForce.DAY,

            limit_price=round(price, 2),

        )

        order = trading_client.submit_order(
            order_request
        )

        log(
            f"📤 BUY ORDER: "
            f"{symbol} x{quantity} "
            f"@ ${price:.2f}"
        )

        return order

    except Exception as e:

        log(
            f"❌ فشل أمر الشراء: {e}"
        )

        return None


def wait_for_fill(
    order,
    timeout_seconds=20
):

    start = time.time()

    while (
        time.time() - start
        < timeout_seconds
    ):

        try:

            current = (
                trading_client
                .get_order_by_id(order.id)
            )

            status = str(
                getattr(
                    current.status,
                    "value",
                    current.status
                )
            ).lower()

            if status == "filled":

                filled_price = float(
                    current.filled_avg_price
                )

                log(
                    f"✅ تم التنفيذ "
                    f"@ ${filled_price:.2f}"
                )

                return filled_price

            if status in (
                "canceled",
                "expired",
                "rejected"
            ):

                log(
                    f"❌ Order {status}"
                )

                return None

        except Exception as e:

            log(
                f"⚠️ مشكلة متابعة الأمر: {e}"
            )

        time.sleep(1)

    log(
        "⏱️ انتهت مهلة انتظار التنفيذ."
    )

    return None


def submit_sell(
    symbol,
    quantity
):

    try:

        order = trading_client.submit_order(

            MarketOrderRequest(

                symbol=symbol,

                qty=quantity,

                side=OrderSide.SELL,

                time_in_force=TimeInForce.DAY

            )
        )

        log(
            f"📤 SELL: {symbol} x{quantity}"
        )

        return order

    except Exception as e:

        log(
            f"❌ فشل البيع: {e}"
        )

        return None


# ============================================================
#                  POSITION MONITOR
# ============================================================

def get_current_option_price(symbol):

    try:

        chain = option_data_client.get_option_chain(

            # نحاول الحصول على snapshot مباشرة
            # باستخدام symbol عبر OptionChain ليس مضمونًا،
            # لذلك نستخدم OptionLatestQuote في المتابعة.
            None
        )

        return None

    except Exception:
        return None


def monitor_position(
    symbol,
    quantity,
    entry_price
):

    stop_pct = (
        STRATEGY["exit"]
        ["stop_loss_pct"]
    )

    take_profit_pct = (
        STRATEGY["exit"]
        ["take_profit_pct"]
    )

    trailing_enabled = (
        STRATEGY["exit"]
        ["trailing_enabled"]
    )

    trailing_pct = (
        STRATEGY["exit"]
        ["trailing_stop_pct"]
    )

    stop_price = (
        entry_price
        * (1 - stop_pct / 100)
    )

    target_price = (
        entry_price
        * (1 + take_profit_pct / 100)
    )

    highest_price = entry_price

    log(
        f"🛡️ مراقبة {symbol}"
    )

    log(
        f"Entry=${entry_price:.2f} | "
        f"SL=${stop_price:.2f} | "
        f"TP=${target_price:.2f}"
    )

    while True:

        try:

            # جلب quote مباشر للعقد
            from alpaca.data.requests import (
                OptionLatestQuoteRequest
            )

            quote_request = (
                OptionLatestQuoteRequest(
                    symbol_or_symbols=symbol,
                    feed=OPTIONS_FEED
                )
            )

            quotes = (
                option_data_client
                .get_option_latest_quote(
                    quote_request
                )
            )

            quote = quotes.get(symbol)

            if not quote:

                time.sleep(2)

                continue

            bid = float(
                quote.bid_price or 0
            )

            ask = float(
                quote.ask_price or 0
            )

            if bid <= 0:

                time.sleep(2)

                continue

            current_price = bid

            # تحديث أعلى سعر
            if current_price > highest_price:

                highest_price = current_price

                if trailing_enabled:

                    new_stop = (
                        highest_price
                        * (1 - trailing_pct / 100)
                    )

                    if new_stop > stop_price:

                        stop_price = new_stop

                        log(
                            f"📈 Trailing Stop "
                            f"→ ${stop_price:.2f}"
                        )

            log(
                f"📊 {symbol} | "
                f"Bid=${current_price:.2f} | "
                f"SL=${stop_price:.2f} | "
                f"TP=${target_price:.2f}"
            )

            # TAKE PROFIT
            if current_price >= target_price:

                log(
                    "💰 TAKE PROFIT"
                )

                submit_sell(
                    symbol,
                    quantity
                )

                return

            # STOP LOSS
            if current_price <= stop_price:

                log(
                    "🛑 STOP LOSS"
                )

                submit_sell(
                    symbol,
                    quantity
                )

                return

            # إذا السوق أغلق
            if not is_market_open():

                log(
                    "🔔 السوق مغلق."
                )

                # لا نستمر بالمراقبة إلى الأبد
                return

            time.sleep(2)

        except Exception as e:

            log(
                f"⚠️ Monitor error: {e}"
            )

            time.sleep(2)


# ============================================================
#                  SIGNAL ENGINE
# ============================================================

def get_signal_for_underlying(
    underlying
):

    mode = STRATEGY[
        "signal"
    ]["mode"]

    if mode == "AUTO":

        direction, move = (
            get_spy_direction()
        )

        if direction == "BULLISH":

            return "CALL", move

        if direction == "BEARISH":

            return "PUT", move

        return None, move

    if mode == "CALL":

        return "CALL", 0.0

    if mode == "PUT":

        return "PUT", 0.0

    return None, 0.0


# ============================================================
#                  MAIN SCAN
# ============================================================

def scan_for_trade():

    global trades_today

    reset_daily_state_if_needed()

    if not is_market_open():

        return

    if not is_safe_trading_time():

        return

    if not risk_allows_trade():

        return

    for underlying in (
        STRATEGY["underlyings"]
    ):

        direction, move = (
            get_signal_for_underlying(
                underlying
            )
        )

        if not direction:

            continue

        log(
            f"🔎 SIGNAL: "
            f"{underlying} "
            f"{direction} "
            f"move={move * 100:.3f}%"
        )

        contract = (
            select_best_option(
                underlying,
                direction
            )
        )

        if not contract:

            continue

        equity = get_equity()

        quantity = calculate_quantity(
            contract["ask"],
            equity
        )

        if quantity <= 0:

            log(
                "⚠️ رأس المال غير كافٍ "
                "لشراء عقد واحد ضمن Risk Limit."
            )

            continue

        order = submit_buy(
            contract["symbol"],
            quantity,
            contract["ask"]
        )

        if not order:

            continue

        filled_price = wait_for_fill(
            order
        )

        if filled_price is None:

            continue

        trades_today += 1

        log(
            f"🚀 دخلت الصفقة رقم "
            f"{trades_today} اليوم."
        )

        monitor_position(
            contract["symbol"],
            quantity,
            filled_price
        )

        # صفقة واحدة في كل دورة
        break


# ============================================================
#                  STARTUP
# ============================================================

def print_startup():

    log(
        "================================================"
    )

    log(
        "🚀 PROFESSIONAL OPTIONS BOT V1"
    )

    log(
        "================================================"
    )

    log(
        f"Paper Trading: {PAPER_MODE}"
    )

    log(
        f"Options Feed: {OPTIONS_FEED}"
    )

    log(
        f"Underlyings: "
        f"{STRATEGY['underlyings']}"
    )

    log(
        f"Delta: "
        f"{STRATEGY['option']['min_delta']}"
        f" - "
        f"{STRATEGY['option']['max_delta']}"
    )

    log(
        f"DTE: "
        f"{STRATEGY['option']['min_dte']}"
        f" - "
        f"{STRATEGY['option']['max_dte']}"
    )

    log(
        f"Risk/Trade: "
        f"{STRATEGY['risk']['risk_per_trade_pct']}%"
    )

    log(
        f"Daily Loss Limit: "
        f"{STRATEGY['risk']['max_daily_loss_pct']}%"
    )

    log(
        "================================================"
    )

    account = get_account()

    if account:

        log(
            f"💰 Cash: {account.cash}"
        )

        log(
            f"💰 Equity: {account.equity}"
        )

        log(
            f"💰 Buying Power: "
            f"{account.buying_power}"
        )

    else:

        log(
            "❌ لم نستطع قراءة الحساب."
        )


# ============================================================
#                  PROGRAM
# ============================================================

if __name__ == "__main__":

    print_startup()

    while True:

        try:

            scan_for_trade()

            time.sleep(10)

        except KeyboardInterrupt:

            log(
                "🛑 تم إيقاف البوت يدويًا."
            )

            break

        except Exception as e:

            log(
                f"🔥 Main Error: {e}"
            )

            time.sleep(10)