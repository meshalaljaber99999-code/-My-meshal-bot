import os
import sys
import time
import functools
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus, ExerciseStyle
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

def clean_env(val):
    if not val:
        return val
    return "".join(c for c in val if ord(c) < 128).strip()

print = functools.partial(print, flush=True)

# ============ إعدادات السرعة الفائقة ============ #
API_KEY = clean_env(os.environ.get("API_KEY"))
SECRET_KEY = clean_env(os.environ.get("SECRET_KEY"))

TARGET_SYMBOLS = ["SPX", "SPY", "TSLA", "NVDA", "AAPL"]

TAKE_PROFIT_PCT = 3.0           # 300% جني أرباح
STOP_LOSS_PCT = 0.4             # 40% وقف خسارة
CHECK_INTERVAL_SECONDS = 1      # فحص العقد كل ثانية بدقة
SCAN_INTERVAL_SECONDS = 5       # سرعة فائقة في مسح السوق والبحث (كل 5 ثوانٍ فقط!)
MAX_CONTRACT_PRICE = 10.0       
MAX_TRADES_PER_DAY = 8          # السماح بفرص أكثر نظراً لسرعة القنص

PAPER_MODE = True

if not API_KEY or not SECRET_KEY:
    sys.exit("API_KEY / SECRET_KEY غير موجودة في متغيرات البيئة")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_MODE)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def get_account_info():
    try:
        account = trading_client.get_account()
        print(f"⚡ [بوت الصواريخ] الرصيد: {account.cash} - القوة الشرائية: {account.buying_power}")
    except Exception as e:
        print(f"خطأ في الحساب: {e}")


def is_market_open():
    try:
        return trading_client.get_clock().is_open
    except Exception:
        return True


def get_fast_stock_price(symbol):
    try:
        sym = "SPY" if symbol == "SPX" else symbol
        req = StockLatestTradeRequest(symbol_or_symbols=sym)
        trade = stock_data_client.get_stock_latest_trade(req)
        p = float(trade[sym].price)
        return p * 10.0 if symbol == "SPX" else p
    except Exception:
        return 0.0


def ultra_fast_momentum_check(symbol):
    """
    تحليل فائق السرعة للزخم اللحظي: يقارن السعر الحالي بالسعر قبل ثانية واحدة فقط
    لاتخاذ قرار فوري بدون انتظار.
    """
    try:
        p1 = get_fast_stock_price(symbol)
        if p1 <= 0:
            return None, False
            
        time.sleep(0.5) # نصف ثانية فقط لقياس النبض السعري السريع
        p2 = get_fast_stock_price(symbol)
        
        diff = p2 - p1
        
        # إذا كانت الحركة سريعة للأعلى أو الأسفل
        if diff >= 0.03:
            return "CALL", True
        elif diff <= -0.03:
            return "PUT", True
            
        return None, False
    except Exception as e:
        return None, False


def find_lightning_option(symbol, direction):
    """البحث السريع جداً عن العقد التنفيذي المناسب"""
    price = get_fast_stock_price(symbol)
    if price <= 0:
        return None
        
    today = datetime.now().date()
    c_type = ContractType.CALL if direction == "CALL" else ContractType.PUT
    
    if symbol == "SPX":
        strike_min = str(round(price * (1.001 if direction == "CALL" else 0.99), 2))
        strike_max = str(round(price * (1.015 if direction == "CALL" else 0.999), 2))
        style = ExerciseStyle.EUROPEAN
    else:
        strike_min = str(round(price * (1.002 if direction == "CALL" else 0.985), 2))
        strike_max = str(round(price * (1.02 if direction == "CALL" else 0.998), 2))
        style = ExerciseStyle.AMERICAN

    request = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=today + timedelta(days=1),
        expiration_date_lte=today + timedelta(days=14),
        type=c_type,
        style=style,
        limit=50, # جلب أسرع وأقل ازدحاما للأوامر
    )
    
    try:
        contracts = trading_client.get_option_contracts(request).option_contracts
        if not contracts:
            return None

        symbols = [c.symbol for c in contracts[:25]] # فحص أقرب العقود سرعة
        quotes = option_data_client.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbols)
        )

        best = None
        for s_code in symbols:
            q = quotes.get(s_code)
            if not q:
                continue
            bid = float(q.bid_price or 0)
            ask = float(q.ask_price or 0)
            if bid <= 0 or ask <= 0 or ask > MAX_CONTRACT_PRICE:
                continue
            if ask < 0.10 or (ask - bid) / ask > 0.30: 
                continue
            if best is None or ask > best["ask"]:
                best = {"symbol": s_code, "bid": bid, "ask": ask, "underlying": symbol, "type": direction}

        if best:
            print(f"⚡ [قنص سريع جداً] [{symbol} - {direction}]: {best['symbol']} بسعر {best['ask']}")
        return best
    except Exception as e:
        return None


def buy_option_instantly(symbol, ask):
    """تنفيذ شراء فوري بأمر محدود سريع"""
    try:
        order = trading_client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=1,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(ask, 2),
            )
        )
        for _ in range(15): # انتظار أسرع للتنفيذ (15 ثانية كحد أقصى)
            time.sleep(0.5)
            o = trading_client.get_order_by_id(order.id)
            status = getattr(o.status, "value", str(o.status))
            if status == "filled":
                print(f"🚀 تم تنفيذ الشراء الخاطف بسعر: {o.filled_avg_price}")
                return float(o.filled_avg_price)
            if status in ("canceled", "expired", "rejected"):
                return None
        return None
    except Exception:
        return None


def monitor_and_sell_fast(symbol, entry_price):
    """مراقبة سرعة البرق للصفقة للخروج عند الربح أو الوقف"""
    tp_price = entry_price * (1 + TAKE_PROFIT_PCT)
    sl_price = entry_price * (1 - STOP_LOSS_PCT)
    
    print(f"🎯 [حراسة فائقة] العقد: {symbol} | الدخول: {entry_price} | الهدف: {tp_price:.2f} | الوقف: {sl_price:.2f}")

    while True:
        try:
            if not is_market_open():
                time.sleep(5)
                
            req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
            q = option_data_client.get_option_latest_quote(req)[symbol]
            bid = float(q.bid_price or 0)
            ask = float(q.ask_price or 0)
            current_price = bid if bid > 0 else ask
            
            if current_price <= 0:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            if current_price >= tp_price:
                print(f"💰 تم جني الربح بنجاح عند السعر: {current_price}!")
                execute_sell_fast(symbol)
                break
            elif current_price <= sl_price:
                print(f"🛡️ تفعيل وقف الخسارة السريع عند السعر: {current_price} لحماية الرصيد.")
                execute_sell_fast(symbol)
                break
                
            time.sleep(CHECK_INTERVAL_SECONDS)
        except Exception:
            time.sleep(1)


def execute_sell_fast(symbol):
    try:
        trading_client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=1,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
        )
        print(f"تم إغلاق الصفقة وبيع العقد {symbol} فوراً.")
    except Exception:
        pass


def main():
    print("🔥 تشغيل بوت القنص فائق السرعة (Ultra-Fast Scalper) جاهز ومستنفر...")
    get_account_info()
    
    trades_today = 0
    
    while True:
        try:
            if not is_market_open():
                print("💤 السوق مغلق. البوت ينتظر بفارغ الصبر ويراقب النبض...")
                time.sleep(15)
                continue
                
            if trades_today >= MAX_TRADES_PER_DAY:
                print("🛑 وصل البوت للحد اليومي للصفقات السريعة. راحة قصيرة...")
                time.sleep(120)
                continue

            # المرور السريع جداً على الأصول
            for symbol in TARGET_SYMBOLS:
                direction, ready = ultra_fast_momentum_check(symbol)
                
                if ready and direction:
                    best_contract = find_lightning_option(symbol, direction)
                    if best_contract:
                        filled = buy_option_instantly(best_contract['symbol'], best_contract['ask'])
                        if filled:
                            trades_today += 1
                            monitor_and_sell_fast(best_contract['symbol'], filled)
                            break
                
                time.sleep(1)
                
            time.sleep(SCAN_INTERVAL_SECONDS)
            
        except Exception:
            time.sleep(5)

if __name__ == "__main__":
    main()
