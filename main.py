import os
import sys
import time
import functools
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetStatus
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestTradeRequest

# دالة لتنظيف النصوص من أي أحرف خفية أو غير مורئية
def clean_env(val):
    if not val:
        return val
    # إزالة المسافات والأحرف الخاصة الخفية مثل RLM
    return "".join(c for c in val if ord(c) < 128).strip()

# يظهر الطباعة فورا في سجلات Railway
print = functools.partial(print, flush=True)

# ============ الإعدادات ============ #
API_KEY = clean_env(os.environ.get("API_KEY"))
SECRET_KEY = clean_env(os.environ.get("SECRET_KEY"))

SYMBOL_UNDERLYING = "SPY"       
TAKE_PROFIT_PCT = 4.0           
STOP_LOSS_PCT = 0.5             
CHECK_INTERVAL_SECONDS = 1      
SCAN_INTERVAL_SECONDS = 30      
MAX_CONTRACT_PRICE = 2.0        
MAX_TRADES_PER_DAY = 3          

PAPER_MODE = True

# ============ الاتصال ============ #
if not API_KEY or not SECRET_KEY:
    sys.exit("API_KEY / SECRET_KEY غير موجودة في متغيرات البيئة أو تحتوي على رموز غير صالحة")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_MODE)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def get_account_info():
    account = trading_client.get_account()
    print(f"تم الاتصال. الرصيد: {account.cash} - القوة الشرائية: {account.buying_power}")
    return account


def is_market_open():
    return trading_client.get_clock().is_open


def get_underlying_price():
    req = StockLatestTradeRequest(symbol_or_symbols=SYMBOL_UNDERLYING)
    trade = stock_data_client.get_stock_latest_trade(req)
    return float(trade[SYMBOL_UNDERLYING].price)


def find_cheap_call_option():
    price = get_underlying_price()
    today = datetime.now().date()

    request = GetOptionContractsRequest(
        underlying_symbols=[SYMBOL_UNDERLYING],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=today + timedelta(days=7),
        expiration_date_lte=today + timedelta(days=30),
        type=ContractType.CALL,
        strike_price_gte=str(round(price * 1.01, 2)),
        strike_price_lte=str(round(price * 1.05, 2)),
        limit=1000,
    )
    contracts = trading_client.get_option_contracts(request).option_contracts
    if not contracts:
        print("ما فيه عقود متاحة")
        return None

    contracts = sorted(contracts, key=lambda c: float(c.strike_price))[:300]
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
        if ask < 0.10 or (ask - bid) / ask > 0.25:  
            continue
        if best is None or ask > best["ask"]:
            best = {"symbol": symbol, "bid": bid, "ask": ask}

    if best:
        print(f"تم اختيار: {best['symbol']} - سعر الشراء {best['ask']}")
    else:
        print("لا يوجد عقد مناسب ضمن الحد الأقصى للسعر")
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
            return float(o.filled_avg_price)
        if status in ("canceled", "expired", "rejected"):
            return None


def main():
    print("بدء تشغيل السكربت على Railway...")
    get_account_info()
    
    trades_today = 0
    
    while True:
        try:
            if not is_market_open():
                print("السوق مغلق حالياً. جاري الانتظار 60 ثانية...")
                time.sleep(60)
                continue
                
            if trades_today >= MAX_TRADES_PER_DAY:
                print("تم الوصول للحد الأقصى من الصفقات اليومية. جاري الانتظار...")
                time.sleep(300)
                continue

            print("جاري البحث عن عقد مناسب...")
            best_contract = find_cheap_call_option()
            
            if best_contract:
                print(f"تم العثور على عقد: {best_contract['symbol']}. جاري التنفيذ...")
                filled_price = buy_option(best_contract['symbol'], best_contract['ask'])
                if filled_price:
                    print(f"تم الشراء بنجاح بسعر: {filled_price}")
                    trades_today += 1
                else:
                    print("فشل تنفيذ أمر الشراء.")
            
            time.sleep(SCAN_INTERVAL_SECONDS)
            
        except Exception as e:
            print(f"حدث خطأ: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
