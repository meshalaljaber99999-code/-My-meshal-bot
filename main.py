import os
import sys
import time
import functools
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

def clean_env(val):
    if not val:
        return val
    return "".join(c for c in val if ord(c) < 128).strip()

print = functools.partial(print, flush=True)

# ============ الإعدادات والإستراتيجية ============ #
API_KEY = clean_env(os.environ.get("API_KEY"))
SECRET_KEY = clean_env(os.environ.get("SECRET_KEY"))

SYMBOL_UNDERLYING = "SPY"       
TAKE_PROFIT_PCT = 4.0           # 400% جني ربح
STOP_LOSS_PCT = 0.5             # 50% وقف خسارة
CHECK_INTERVAL_SECONDS = 1      # فحص العقد كل ثانية أثناء الصفقة
SCAN_INTERVAL_SECONDS = 30      # البحث عن فرصة جديدة كل 30 ثانية
MAX_CONTRACT_PRICE = 2.5        # الحد الأقصى لسعر العقد (250 دولار)
MAX_TRADES_PER_DAY = 3          # حد أقصى للتعاملات اليومية

PAPER_MODE = True

if not API_KEY or not SECRET_KEY:
    sys.exit("API_KEY / SECRET_KEY غير موجودة في متغيرات البيئة أو تحتوي على رموز غير صالحة")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_MODE)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def get_account_info():
    try:
        account = trading_client.get_account()
        print(f"تم الاتصال. الرصيد: {account.cash} - القوة الشرائية: {account.buying_power}")
    except Exception as e:
        print(f"خطأ في جلب بيانات الحساب: {e}")


def is_market_open():
    try:
        return trading_client.get_clock().is_open
    except Exception:
        return True


def get_underlying_price():
    req = StockLatestTradeRequest(symbol_or_symbols=SYMBOL_UNDERLYING)
    trade = stock_data_client.get_stock_latest_trade(req)
    return float(trade[SYMBOL_UNDERLYING].price)


def check_market_momentum():
    """
    إستراتيجية الزخم: تتأكد أن السعر الحالي أعلى من إغلاق الشمعة السابقة 
    مما يعني وجود زخم صعودي قوي (Momentum Breakout) قبل الشراء.
    """
    try:
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=15)
        
        request_params = StockBarsRequest(
            symbol_or_symbols=SYMBOL_UNDERLYING,
            timeframe=TimeFrame.Minute,
            start=start_time,
            end=end_time
        )
        bars = stock_data_client.get_stock_bars(request_params)
        df_bars = bars.df
        
        if df_bars.empty or len(df_bars) < 2:
            return True # تجاوز الشرط في حال عدم توفر كفاية البيانات مؤقتاً
            
        last_close = float(df_bars.iloc[-1]['close'])
import os
import sys
import time
import functools
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

def clean_env(val):
    if not val:
        return val
    return "".join(c for c in val if ord(c) < 128).strip()

print = functools.partial(print, flush=True)

# ============ الإعدادات والإستراتيجية ============ #
API_KEY = clean_env(os.environ.get("API_KEY"))
SECRET_KEY = clean_env(os.environ.get("SECRET_KEY"))

SYMBOL_UNDERLYING = "SPY"       
TAKE_PROFIT_PCT = 4.0           # 400% جني ربح
STOP_LOSS_PCT = 0.5             # 50% وقف خسارة
CHECK_INTERVAL_SECONDS = 1      # فحص العقد كل ثانية أثناء الصفقة
SCAN_INTERVAL_SECONDS = 30      # البحث عن فرصة جديدة كل 30 ثانية
MAX_CONTRACT_PRICE = 2.5        # الحد الأقصى لسعر العقد (250 دولار)
MAX_TRADES_PER_DAY = 3          # حد أقصى للتعاملات اليومية

PAPER_MODE = True

if not API_KEY or not SECRET_KEY:
    sys.exit("API_KEY / SECRET_KEY غير موجودة في متغيرات البيئة أو تحتوي على رموز غير صالحة")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_MODE)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def get_account_info():
    try:
        account = trading_client.get_account()
        print(f"تم الاتصال. الرصيد: {account.cash} - القوة الشرائية: {account.buying_power}")
    except Exception as e:
        print(f"خطأ في جلب بيانات الحساب: {e}")


def is_market_open():
    try:
        return trading_client.get_clock().is_open
    except Exception:
        return True


def get_underlying_price():
    req = StockLatestTradeRequest(symbol_or_symbols=SYMBOL_UNDERLYING)
    trade = stock_data_client.get_stock_latest_trade(req)
    return float(trade[SYMBOL_UNDERLYING].price)


def check_market_momentum():
    """إستراتيجية الزخم: تتأكد من وجود حركة صعودية قوية للسهم قبل الشراء"""
    try:
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=10)
        
        request_params = StockBarsRequest(
            symbol_or_symbols=SYMBOL_UNDERLYING,
            timeframe=TimeFrame.Minute,
            start=start_time,
            end=end_time
        )
        bars = stock_data_client.get_stock_bars(request_params)
        df_bars = bars.df
        
        if df_bars.empty or len(df_bars) < 2:
            return True
            
        prev_close = float(df_bars.iloc[-2]['close'])
        current_price = get_underlying_price()
        
        # شرط الزخم: السعر الحالي أعلى من إغلاق الشمعة السابقة
        if current_price >= prev_close:
            print(f"📈 مؤشر الزخم إيجابي: السعر الحالي ({current_price}) أعلى أو مساوٍ للإغلاق السابق ({prev_close})")
            return True
        else:
            print(f"📉 الزخم سلبي حالياً (السعر ينخفض): الحالي {current_price} < السابق {prev_close}")
            return False
    except Exception as e:
        print(f"تجاوز فحص الزخم بسبب خطأ مؤقت: {e}")
        return True


def get_option_quote(symbol):
    req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
    q = option_data_client.get_option_latest_quote(req)[symbol]
    return float(q.bid_price or 0), float(q.ask_price or 0)


def find_momentum_call_option():
    """يبحث عن أفضل عقود Call مستفيداً من شروط الزخم والسيولة"""
    price = get_underlying_price()
    today = datetime.now().date()

    request = GetOptionContractsRequest(
        underlying_symbols=[SYMBOL_UNDERLYING],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=today + timedelta(days=7),
        expiration_date_lte=today + timedelta(days=30),
        type=ContractType.CALL,
        strike_price_gte=str(round(price * 1.005, 2)), # قريباً جداً من السعر الحالي
        strike_price_lte=str(round(price * 1.03, 2)),  # نطاق آمن ومربح
        limit=500,
    )
    contracts = trading_client.get_option_contracts(request).option_contracts
    if not contracts:
        print("ما فيه عقود متاحة ضمن النطاق المستهدف")
        return None

    contracts = sorted(contracts, key=lambda c: float(c.strike_price))[:200]
    symbols = [c.symbol for c in contracts]

    quotes = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        quotes.update(
            option_data_client.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=chunk)
            )
        )

    best = None
    for symbol in symbols:
        q = quotes.get(symbol)
        if not q:
            continue
        bid = float(q.bid_price or 0)
        ask = float(q.ask_price or 0)
        if bid <= 0 or ask <= 0 or ask > MAX_CONTRACT_PRICE:
            continue
        if ask < 0.15 or (ask - bid) / ask > 0.20:  # اشتراط سيولة أعلى وفارق سعر أقل
            continue
        if best is None or ask > best["ask"]:
            best = {"symbol": symbol, "bid": bid, "ask": ask}

    if best:
        print(f"تم اختيار عقد الزخم: {best['symbol']} بسعر {best['ask']}")
    else:
        print("لا يوجد عقد يحقق شروط الزخم والسيولة الصارمة حالياً")
    return best


def buy_option(symbol, ask):
    order = trading_client.submit_order(
        LimitOrderRequest(
            symbol=symbol,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(ask, 2),
        )
    )
    for _ in range(30):
        time.sleep(1)
        o = trading_client.get_order_by_id(order.id)
        status = getattr(o.status, "value", str(o.status))
        if status == "filled":
            print(f"تم تنفيذ شراء العقد بنجاح بسعر: {o.filled_avg_price}")
            return float(o.filled_avg_price)
        if status in ("canceled", "expired", "rejected"):
            return None
    return None


def monitor_and_sell_position(symbol, entry_price):
    """مراقبة لحظية صارمة: جني أرباح 400% أو وقف خسارة 50%"""
    take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT)
    stop_loss_price = entry_price * (1 - STOP_LOSS_PCT)
    
    print(f"بدء نظام المراقبة والحماية لـ {symbol} | الدخول: {entry_price} | الهدف: {take_profit_price:.2f} | الوقف: {stop_loss_price:.2f}")

    while True:
        try:
            if not is_market_open():
                print("السوق أغلق أثناء الصفقة. جاري المتابعة...")
                time.sleep(10)
                
            bid, ask = get_option_quote(symbol)
            current_price = bid if bid > 0 else ask
            
            if current_price <= 0:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue
                
            print(f"متابعة العقد {symbol} - السعر الحالي: {current_price}")

            if current_price >= take_profit_price:
                print(f"🎯 مبروك! تحقق هدف الربح ({current_price}). جاري البيع...")
                execute_sell(symbol)
                break
                
            elif current_price <= stop_loss_price:
                print(f"🛑 تم ضرب وقف الخسارة الحماية ({current_price}). جاري البيع الفوري...")
                execute_sell(symbol)
                break
                
            time.sleep(CHECK_INTERVAL_SECONDS)
            
        except Exception as e:
            print(f"خطأ أثناء حراسة الصفقة: {e}")
            time.sleep(2)


def execute_sell(symbol):
    try:
        trading_client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=1,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
        )
        print(f"تم إغلاق الصفقة وبيع العقد {symbol} بنجاح.")
    except Exception as e:
        print(f"فشل إرسال أمر البيع: {e}")


def main():
    print("بدء تشغيل بوت إستراتيجية الزخم المتقدمة على Railway...")
    get_account_info()
    
    trades_today = 0
    
    while True:
        try:
            if not is_market_open():
                print("السوق مغلق. البوت يعمل في الخلفية بانتظار فتح السوق والتصيد...")
                time.sleep(60)
                continue
                
            if trades_today >= MAX_TRADES_PER_DAY:
                print("تم اكتمال الحد الأقصى للصفقات اليومية (3 صفقات). البوت في وضع الاستراحة...")
                time.sleep(300)
                continue

            # فحص إستراتيجية الزخم قبل البحث عن عقود
            if not check_market_momentum():
                print("الزخم غير مناسب حالياً، سيتم إعادة المحاولة بعد قليل...")
                time.sleep(30)
                continue

            print("الزخم إيجابي! جاري البحث عن أفضل عقد Call...")
            best_contract = find_momentum_call_option()
            
            if best_contract:
                print(f"تم العثور على فرصة: {best_contract['symbol']}. جاري التنفيذ...")
                filled_price = buy_option(best_contract['symbol'], best_contract['ask'])
                
                if filled_price:
                    trades_today += 1
                    monitor_and_sell_position(best_contract['symbol'], filled_price)
                else:
                    print("فشل تنفيذ أمر الشراء، جاري البحث مجدداً.")
            
            time.sleep(SCAN_INTERVAL_SECONDS)
            
        except Exception as e:
            print(f"حدث خطأ في الحلقة الرئيسية: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
