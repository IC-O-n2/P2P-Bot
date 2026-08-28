import os
from pybit.unified_trading import HTTP
from datetime import datetime

# --- 1. Получение ключей из переменных окружения Railway ---
# Для локального запуска можно создать файл .env с этими переменными
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
# TELEGRAM_TOKEN нам здесь не понадобится, это для бота

if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    print("❌ Ошибка: API ключи не найдены в переменных окружения.")
    print("   Убедитесь, что BYBIT_API_KEY и BYBIT_API_SECRET установлены в Railway.")
    exit()

# --- 2. Инициализация клиента для Bybit P2P API ---
# Используем класс HTTP из библиотеки pybit (рекомендована для V5 API)
# Документация: https://github.com/bybit-exchange/pybit
session = HTTP(
    testnet=False,  # False для реального Mainnet
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
)

print("✅ Соединение с Bybit P2P установлено.")
print("⏳ Запрос первых 5 публичных объявлений (USDT/RUB)...\n")

# --- 3. Запрос публичных P2P-объявлений ---
# Эндпоинт /v5/p2p/item/online [citation:1][citation:3][citation:5]
# Запрашиваем объявления на покупку USDT за RUB
try:
    response = session.get_online_ads(
        coinId="USDT",       # Криптовалюта
        currencyId="RUB",    # Фиатная валюта
        side="BUY",          # "BUY" - покупка USDT (продажа RUB)
        # Можно добавить другие фильтры: paymentMethod, page, size и т.д.
    )

    # --- 4. Проверка ответа и вывод ---
    if response.get("retCode") != 0:
        print(f"❌ Ошибка API: {response.get('retMsg')}")
        print(f"   Код: {response.get('retCode')}")
        exit()

    data = response.get("result", {}).get("items", [])

    if not data:
        print("⚠️ Объявлений не найдено.")
        print("   Возможно, по указанным параметрам нет активных объявлений.")
    else:
        print(f"📊 Найдено объявлений: {len(data)}")
        print("--- Первые 5 объявлений (краткий формат) ---\n")
        
        # Берем первые 5 объявлений
        for i, ad in enumerate(data[:5], 1):
            # Извлекаем нужные поля
            price = ad.get("price", "N/A")
            min_amount = ad.get("minAmount", "N/A")
            max_amount = ad.get("maxAmount", "N/A")
            payment_methods = ad.get("paymentMethods", [])
            payment_name = payment_methods[0].get("name", "N/A") if payment_methods else "N/A"
            user_nick = ad.get("advertiser", {}).get("nickName", "N/A")
            # trades = ad.get("advertiser", {}).get("tradeCount", "N/A")  # Кол-во сделок
            # completion = ad.get("advertiser", {}).get("tradeRate", "N/A")  # % выполнения

            print(f"🏷️  Объявление #{i}")
            print(f"   👤 Продавец: {user_nick}")
            print(f"   💵 Цена: {price} RUB за USDT")
            print(f"   💰 Лимиты: {min_amount} - {max_amount} RUB")
            print(f"   💳 Способ оплаты: {payment_name}")
            print("-" * 40)

except Exception as e:
    print(f"❌ Произошла ошибка при запросе к API: {e}")
    print("   Проверьте правильность API ключей и их разрешения.")
    print("   Важно: P2P API доступен только для P2P-мейкеров (Advertisers). [citation:7][citation:8]")
