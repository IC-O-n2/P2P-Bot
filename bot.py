import os
import requests
import json
from datetime import datetime
import time
import hashlib
import hmac

# --- 1. Получение ключей из переменных окружения Railway ---
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    print("❌ Ошибка: API ключи не найдены в переменных окружения.")
    print("   Убедитесь, что BYBIT_API_KEY и BYBIT_API_SECRET установлены в Railway.")
    exit()

print("✅ Соединение с Bybit P2P установлено.")
print("⏳ Запрос первых 5 публичных объявлений (USDT/RUB)...\n")

# --- 2. Прямой запрос к P2P API Bybit (без библиотеки pybit) ---

def bybit_p2p_request(api_key, api_secret, endpoint, params):
    """Выполняет подписанный запрос к P2P API Bybit"""
    base_url = "https://api.bybit.com"
    timestamp = str(int(time.time() * 1000))
    
    # Сортировка параметров для подписи
    sorted_params = sorted(params.items())
    query_string = "&".join([f"{k}={v}" for k, v in sorted_params])
    
    # Создание подписи
    sign_str = timestamp + api_key + "5000" + query_string
    signature = hmac.new(
        bytes(api_secret, "utf-8"),
        bytes(sign_str, "utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    # Заголовки
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json"
    }
    
    url = base_url + endpoint
    response = requests.post(url, json=params, headers=headers)
    return response.json()

# --- 3. Правильный запрос P2P объявлений ---

# Правильные параметры для P2P API
params = {
    "coinId": "1",  # ID для USDT
    "currencyId": "2",  # ID для RUB
    "side": "0",  # 0 - покупка USDT (продажа RUB), 1 - продажа USDT
    "page": "1",
    "size": "5",  # Количество объявлений
    "paymentMethod": "",  # Пусто для всех методов оплаты
    "sortType": "1"  # 0 - по умолчанию, 1 - по цене (низкая-высокая)
}

try:
    # Пробуем публичный эндпоинт (не требует авторизации)
    public_params = {
        "coinId": "1",
        "currencyId": "2",
        "side": "0",
        "page": "1",
        "size": "5",
        "sortType": "1"
    }
    
    public_response = requests.post(
        "https://api.bybit.com/v5/p2p/item/online",
        json=public_params,
        headers={"Content-Type": "application/json"}
    )
    
    data = public_response.json()
    
    # --- 4. Проверка и вывод ---
    if data.get("retCode") == 0:
        items = data.get("result", {}).get("items", [])
        
        if not items:
            print("⚠️ Объявлений не найдено.")
        else:
            print(f"📊 Найдено объявлений: {len(items)}")
            print("--- Первые 5 объявлений (краткий формат) ---\n")
            
            for i, ad in enumerate(items[:5], 1):
                price = ad.get("price", "N/A")
                min_amount = ad.get("minAmount", "N/A")
                max_amount = ad.get("maxAmount", "N/A")
                
                # Информация о продавце
                advertiser = ad.get("advertiser", {})
                user_nick = advertiser.get("nickName", "N/A")
                
                # Способы оплаты
                payment_methods = ad.get("paymentMethods", [])
                payment_names = []
                if payment_methods:
                    for pm in payment_methods[:3]:  # Первые 3 способа
                        payment_names.append(pm.get("name", "N/A"))
                payment_names_str = ", ".join(payment_names) if payment_names else "N/A"
                
                # Статистика продавца
                month_orders = advertiser.get("monthOrderCount", "N/A")
                month_rate = advertiser.get("monthFinishRate", "N/A")
                
                print(f"🏷️  Объявление #{i}")
                print(f"   👤 Продавец: {user_nick}")
                print(f"   💵 Цена: {price} RUB за USDT")
                print(f"   💰 Лимиты: {min_amount} - {max_amount} RUB")
                print(f"   💳 Способы оплаты: {payment_names_str}")
                print(f"   📊 Заказов за месяц: {month_orders}, выполнено: {month_rate}%")
                print("-" * 50)
    
    else:
        print(f"❌ Ошибка API: {data.get('retMsg', 'Неизвестная ошибка')}")
        print(f"   Код: {data.get('retCode')}")
        print("   Попробуйте другие параметры:")
        print("   - coinId: 1 (USDT), 2 (BTC), 3 (ETH)")
        print("   - currencyId: 1 (USD), 2 (RUB), 3 (EUR)")
        print("   - side: 0 (покупка USDT), 1 (продажа USDT)")

except requests.exceptions.RequestException as e:
    print(f"❌ Ошибка подключения: {e}")
except Exception as e:
    print(f"❌ Неожиданная ошибка: {e}")
