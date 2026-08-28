import os
import requests
import json
import time
import hashlib
import hmac

# --- 1. Получение ключей из переменных окружения Railway ---
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    print("❌ Ошибка: API ключи не найдены!")
    print("   Убедитесь, что BYBIT_API_KEY и BYBIT_API_SECRET установлены в Railway.")
    exit()

print("🚀 Запуск P2P мониторинга Bybit...")
print("="*60)

# --- 2. Функция для подписанного POST запроса (согласно примеру) ---

def bybit_signed_post(endpoint, params):
    """Выполняет подписанный POST запрос к API Bybit"""
    
    base_url = "https://api.bybit.com"
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    
    # Преобразуем параметры в JSON строку без пробелов
    json_body = json.dumps(params, separators=(',', ':'))
    
    # Строка для подписи: timestamp + api_key + recv_window + jsonBodyString
    sign_str = timestamp + BYBIT_API_KEY + recv_window + json_body
    
    # Создание подписи HMAC-SHA256
    signature = hmac.new(
        bytes(BYBIT_API_SECRET, "utf-8"),
        bytes(sign_str, "utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    # Заголовки
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json"
    }
    
    url = base_url + endpoint
    
    # Отладка
    print(f"   🔑 Подпись: {signature[:20]}...")
    print(f"   📝 JSON: {json_body}")
    
    try:
        response = requests.post(url, json=params, headers=headers, timeout=10)
        return response.json()
    except Exception as e:
        print(f"   ❌ Ошибка запроса: {e}")
        return None

# --- 3. Функция для получения P2P объявлений ---

def get_p2p_orders(side, coin="USDT", fiat="RUB", limit=5):
    """Получает P2P объявления с Bybit"""
    
    # Параметры для P2P запроса
    params = {
        "coinId": "USDT",
        "currencyId": "RUB",
        "side": side,  # "BUY" или "SELL"
        "page": "1",
        "size": str(limit)
    }
    
    print(f"\n📡 Запрос {side} объявлений...")
    
    response = bybit_signed_post("/v5/p2p/item/online", params)
    
    if response:
        ret_code = response.get("retCode")
        ret_msg = response.get("retMsg", "")
        
        print(f"   📊 Ответ: retCode={ret_code}, retMsg={ret_msg}")
        
        if ret_code == 0 or ret_msg.lower() in ["ok", "success"]:
            items = response.get("result", {}).get("items", [])
            print(f"   ✅ Получено {len(items)} объявлений")
            return items
        else:
            print(f"   ❌ Ошибка: {ret_msg}")
            return []
    else:
        print("   ❌ Нет ответа от API")
        return []

# --- 4. Проверка соединения с API ---

print("📊 Проверка соединения с Bybit API...")

try:
    # Проверяем время сервера
    time_response = requests.get("https://api.bybit.com/v5/market/time")
    if time_response.status_code == 200:
        time_data = time_response.json()
        print(f"✅ Сервер Bybit доступен")
        print(f"   Время сервера: {time_data.get('result', {}).get('timeSecond')}")
    else:
        print(f"❌ Ошибка доступа: {time_response.status_code}")
except Exception as e:
    print(f"❌ Ошибка: {e}")

# --- 5. Получаем объявления ---

print("\n" + "="*60)
print("📊 Запрос P2P данных с Bybit...")

# Получаем объявления на покупку USDT (BUY) - продавцы RUB
buy_orders = get_p2p_orders("BUY", limit=5)
# Получаем объявления на продажу USDT (SELL) - покупатели RUB
sell_orders = get_p2p_orders("SELL", limit=5)

# --- 6. Выводим результаты в нужном формате ---

print("\n" + "="*60)

if buy_orders and sell_orders:
    print("✅ УСПЕШНОЕ ПОДКЛЮЧЕНИЕ К BYBIT P2P")
    print("="*60)
    print("📋 ПЕРВЫЕ 5 ОБЪЯВЛЕНИЙ (BUY и SELL):\n")
    
    # Выводим объявления
    max_orders = min(5, len(buy_orders), len(sell_orders))
    
    for i in range(max_orders):
        buy = buy_orders[i]
        sell = sell_orders[i]
        
        # Информация из объявлений
        buy_price = buy.get("price", "N/A")
        sell_price = sell.get("price", "N/A")
        
        buy_seller = buy.get("advertiser", {}).get("nickName", "N/A")
        sell_seller = sell.get("advertiser", {}).get("nickName", "N/A")
        
        # Сумма в RUB (берем минимальную для примера)
        buy_min = float(buy.get("minAmount", 0))
        buy_avg = buy_min
        
        # Количество USDT
        if buy_price != "N/A" and float(buy_price) > 0:
            usdt_amount = buy_avg / float(buy_price)
        else:
            usdt_amount = 0
        
        print(f"🏷️  Объявление #{i+1}")
        print(f"   BUY:  {buy_price} RUB y {buy_seller}")
        print(f"   SELL: {sell_price} RUB y {sell_seller}")
        print(f"   Сумма: {buy_avg:.2f} RUB (~{usdt_amount:.2f} USDT)")
        print("-" * 40)
    
    print(f"\n✅ Всего получено: {len(buy_orders)} BUY и {len(sell_orders)} SELL объявлений")
    print("🎯 ДАННЫЕ ОТ BYBIT ПОЛУЧЕНЫ УСПЕШНО!")
    
elif buy_orders:
    print("⚠️ Получены только BUY объявления")
    print(f"   BUY: {len(buy_orders)} объявлений")
    
    print("\n📋 BUY объявления:")
    for i, buy in enumerate(buy_orders[:5], 1):
        price = buy.get("price", "N/A")
        seller = buy.get("advertiser", {}).get("nickName", "N/A")
        print(f"   {i}. {price} RUB y {seller}")
    
elif sell_orders:
    print("⚠️ Получены только SELL объявления")
    print(f"   SELL: {len(sell_orders)} объявлений")
    
    print("\n📋 SELL объявления:")
    for i, sell in enumerate(sell_orders[:5], 1):
        price = sell.get("price", "N/A")
        seller = sell.get("advertiser", {}).get("nickName", "N/A")
        print(f"   {i}. {price} RUB y {seller}")
    
else:
    print("❌ НЕ УДАЛОСЬ ПОЛУЧИТЬ ДАННЫЕ ОТ BYBIT")
    print("\n💡 Возможные причины:")
    print("   1. Нет активных объявлений в паре USDT/RUB")
    print("   2. API ключи не имеют прав на P2P запросы")
    print("   3. Проблемы с сетью или API Bybit")

print("\n" + "="*60)
print("🏁 Завершение работы")
