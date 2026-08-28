import os
import requests
import json

# --- 1. Получение ключей из переменных окружения Railway ---
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

print("🚀 Запуск P2P мониторинга Bybit...")
print("="*60)

# --- 2. Функция для получения объявлений ---

def get_p2p_orders(side, coin="USDT", fiat="RUB"):
    """Получает P2P объявления с Bybit"""
    
    # Параметры для запроса
    params = {
        "coinId": "USDT",
        "currencyId": "RUB",
        "side": side,  # "BUY" или "SELL"
        "page": "1",
        "size": "5"
    }
    
    try:
        response = requests.post(
            "https://api.bybit.com/v5/p2p/item/online",
            json=params,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get("retCode") == 0:
                return data.get("result", {}).get("items", [])
            else:
                print(f"⚠️ Ошибка API: {data.get('retMsg')}")
                return []
        else:
            print(f"⚠️ HTTP ошибка: {response.status_code}")
            return []
            
    except Exception as e:
        print(f"⚠️ Ошибка запроса: {e}")
        return []

# --- 3. Получаем объявления на покупку и продажу ---

print("📊 Запрос данных с Bybit P2P...\n")

# Получаем объявления на покупку USDT (BUY) - продавцы RUB
buy_orders = get_p2p_orders("BUY")
# Получаем объявления на продажу USDT (SELL) - покупатели RUB
sell_orders = get_p2p_orders("SELL")

# --- 4. Выводим результаты в формате как на скриншоте ---

if buy_orders and sell_orders:
    print("✅ УСПЕШНОЕ ПОДКЛЮЧЕНИЕ К BYBIT P2P")
    print("="*60)
    print("📋 ПЕРВЫЕ 5 ОБЪЯВЛЕНИЙ (BUY и SELL):\n")
    
    # Выводим первые 5 объявлений (или сколько есть)
    max_orders = min(5, len(buy_orders), len(sell_orders))
    
    for i in range(max_orders):
        buy = buy_orders[i]
        sell = sell_orders[i]
        
        # Цены
        buy_price = buy.get("price", "N/A")
        sell_price = sell.get("price", "N/A")
        
        # Продавцы/покупатели
        buy_seller = buy.get("advertiser", {}).get("nickName", "N/A")
        sell_seller = sell.get("advertiser", {}).get("nickName", "N/A")
        
        # Сумма (берем среднюю или минимальную)
        buy_min = float(buy.get("minAmount", 0))
        buy_max = float(buy.get("maxAmount", 0))
        sell_min = float(sell.get("minAmount", 0))
        sell_max = float(sell.get("maxAmount", 0))
        
        # Берем среднюю сумму для примера
        buy_avg = (buy_min + buy_max) / 2 if buy_min and buy_max else buy_min
        sell_avg = (sell_min + sell_max) / 2 if sell_min and sell_max else sell_min
        
        print(f"🏷️  Объявление #{i+1}")
        print(f"   BUY: {buy_price} RUB y {buy_seller}")
        print(f"   SELL: {sell_price} RUB y {sell_seller}")
        print(f"   Сумма: {buy_avg:.2f} RUB (~{buy_avg/float(buy_price):.2f} USDT)" if buy_price != "N/A" else "   Сумма: N/A")
        print("-" * 40)
    
    print(f"\n✅ Всего получено: {len(buy_orders)} BUY и {len(sell_orders)} SELL объявлений")
    print("🎯 ДАННЫЕ ОТ BYBIT ПОЛУЧЕНЫ УСПЕШНО!")
    
else:
    print("❌ НЕ УДАЛОСЬ ПОЛУЧИТЬ ДАННЫЕ ОТ BYBIT")
    print("\n🔍 Пробуем альтернативный запрос...")
    
    # Пробуем альтернативные параметры
    try:
        alt_params = {
            "coinId": "1",  # USDT
            "currencyId": "2",  # RUB
            "side": "0",  # BUY
            "page": "1",
            "size": "5"
        }
        
        response = requests.post(
            "https://api.bybit.com/v5/p2p/item/online",
            json=alt_params,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            print(f"📝 Ответ API: {json.dumps(data, indent=2)[:500]}")
        else:
            print(f"⚠️ HTTP статус: {response.status_code}")
            
    except Exception as e:
        print(f"⚠️ Ошибка: {e}")
