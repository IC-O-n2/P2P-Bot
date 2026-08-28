import os
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# --- 1. Получение ключей из переменных окружения Railway ---
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    print("❌ Ошибка: API ключи не найдены!")
    print("   Убедитесь, что BYBIT_API_KEY и BYBIT_API_SECRET установлены в Railway.")
    exit()

print("🚀 Запуск P2P мониторинга Bybit...")
print("="*60)

# --- 2. Функция для подписанного POST запроса (как в bybit_client.py) ---

def post_signed(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Выполняет подписанный POST запрос к Bybit API"""
    
    base_url = "https://api.bybit.com"
    api_key = BYBIT_API_KEY
    api_secret = BYBIT_API_SECRET
    recv_window_ms = 5000
    timeout_seconds = 15
    
    timestamp = str(int(time.time() * 1000))
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    
    # Строка для подписи: timestamp + api_key + recv_window + body
    signature_payload = f"{timestamp}{api_key}{recv_window_ms}{body}"
    
    signature = hmac.new(
        api_secret.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    
    request = Request(
        f"{base_url}{path}",
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": str(recv_window_ms),
            "X-BAPI-SIGN": signature,
        },
        method="POST",
    )
    
    print(f"   🔑 Подпись создана")
    print(f"   📝 Тело: {body}")
    
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            http_status_code = response.getcode()
            response_body = response.read().decode("utf-8")
    except HTTPError as error:
        try:
            response_body = error.read().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            response_body = ""
        print(f"   ❌ HTTP ошибка: {error.code}")
        print(f"   Ответ: {response_body[:200]}")
        return {}
    except (URLError, TimeoutError, OSError) as error:
        print(f"   ❌ Ошибка соединения: {error}")
        return {}
    
    try:
        parsed = json.loads(response_body)
        print(f"   📊 Ответ получен, код: {parsed.get('retCode', parsed.get('ret_code', 'N/A'))}")
        return parsed
    except json.JSONDecodeError as error:
        print(f"   ❌ Ошибка парсинга JSON: {error}")
        print(f"   Ответ: {response_body[:200]}")
        return {}

# --- 3. Функция для получения P2P объявлений (как в bybit_client.py) ---

def get_online_ads(action: str, page: int = 1, size: int = 5) -> list:
    """Получает P2P объявления с Bybit"""
    
    # Bybit side: 0 - покупка USDT (продажа RUB), 1 - продажа USDT (покупка RUB)
    if action == "BUY":
        bybit_side = 0
    elif action == "SELL":
        bybit_side = 1
    else:
        raise ValueError("action must be BUY or SELL")
    
    print(f"\n📡 Запрос {action} объявлений (side={bybit_side})...")
    
    result = post_signed(
        "/v5/p2p/item/online",
        {
            "tokenId": "USDT",      # Токен (вместо coinId)
            "currencyId": "RUB",    # Валюта (вместо currencyId)
            "side": str(bybit_side),
            "page": str(page),
            "size": str(size),
        },
    )
    
    # Извлекаем объявления из ответа
    items = result.get("result", {}).get("items", [])
    if not items:
        print(f"   ⚠️ Объявлений не найдено")
        return []
    
    print(f"   ✅ Получено {len(items)} объявлений")
    return items

# --- 4. Проверка соединения ---

print("📊 Проверка соединения с Bybit API...")

try:
    # Простой GET запрос для проверки
    from urllib.request import urlopen
    time_response = urlopen("https://api.bybit.com/v5/market/time", timeout=10)
    if time_response.getcode() == 200:
        print("✅ Сервер Bybit доступен")
except Exception as e:
    print(f"⚠️ Ошибка проверки: {e}")

print("\n" + "="*60)
print("📊 Запрос P2P данных с Bybit...")

# --- 5. Получаем объявления ---

buy_orders = get_online_ads("BUY", page=1, size=5)
sell_orders = get_online_ads("SELL", page=1, size=5)

# --- 6. Выводим результаты в нужном формате ---

print("\n" + "="*60)

if buy_orders and sell_orders:
    print("✅ УСПЕШНОЕ ПОДКЛЮЧЕНИЕ К BYBIT P2P")
    print("="*60)
    print("📋 ПЕРВЫЕ 5 ОБЪЯВЛЕНИЙ (BUY и SELL):\n")
    
    max_orders = min(5, len(buy_orders), len(sell_orders))
    
    for i in range(max_orders):
        buy = buy_orders[i]
        sell = sell_orders[i]
        
        # Извлекаем данные из объявлений
        buy_price = buy.get("price", "N/A")
        sell_price = sell.get("price", "N/A")
        
        buy_seller = buy.get("nickName", buy.get("nickname", "N/A"))
        sell_seller = sell.get("nickName", sell.get("nickname", "N/A"))
        
        # Сумма и USDT
        buy_min = Decimal(str(buy.get("minAmount", 0)))
        buy_max = Decimal(str(buy.get("maxAmount", 0)))
        
        # Берем среднюю сумму для примера
        if buy_min > 0 and buy_max > 0:
            trade_amount = (buy_min + buy_max) / 2
        else:
            trade_amount = buy_min
        
        # Количество USDT
        if buy_price != "N/A" and Decimal(str(buy_price)) > 0:
            usdt_amount = trade_amount / Decimal(str(buy_price))
        else:
            usdt_amount = Decimal("0")
        
        # Форматирование как на скриншоте
        def fmt(value):
            if isinstance(value, Decimal):
                return f"{value:,.2f}".replace(",", " ").replace(".", ",")
            return str(value).replace(".", ",")
        
        print(f"🏷️  Объявление #{i+1}")
        print(f"   BUY:  {fmt(buy_price)} RUB y {buy_seller}")
        print(f"   SELL: {fmt(sell_price)} RUB y {sell_seller}")
        print(f"   Сумма: {fmt(trade_amount)} RUB (~{fmt(usdt_amount)} USDT)")
        
        # Дополнительная информация из объявлений
        buy_methods = buy.get("paymentMethods", [])
        if buy_methods:
            methods = [m.get("name", "N/A") for m in buy_methods[:2]]
            print(f"   Оплата: {', '.join(methods)}")
        
        print("-" * 40)
    
    print(f"\n✅ Всего получено: {len(buy_orders)} BUY и {len(sell_orders)} SELL объявлений")
    print("🎯 ДАННЫЕ ОТ BYBIT ПОЛУЧЕНЫ УСПЕШНО!")
    
elif buy_orders:
    print("⚠️ Получены только BUY объявления")
    print(f"   BUY: {len(buy_orders)} объявлений")
    
    print("\n📋 BUY объявления:")
    for i, buy in enumerate(buy_orders[:5], 1):
        price = buy.get("price", "N/A")
        seller = buy.get("nickName", "N/A")
        print(f"   {i}. {price} RUB y {seller}")
    
elif sell_orders:
    print("⚠️ Получены только SELL объявления")
    print(f"   SELL: {len(sell_orders)} объявлений")
    
    print("\n📋 SELL объявления:")
    for i, sell in enumerate(sell_orders[:5], 1):
        price = sell.get("price", "N/A")
        seller = sell.get("nickName", "N/A")
        print(f"   {i}. {price} RUB y {seller}")
    
else:
    print("❌ НЕ УДАЛОСЬ ПОЛУЧИТЬ ДАННЫЕ ОТ BYBIT")
    print("\n💡 Возможные причины:")
    print("   1. Нет активных объявлений в паре USDT/RUB")
    print("   2. API ключи не имеют прав на P2P запросы")
    print("   3. Проблемы с сетью или API Bybit")

# --- 7. ДЕТАЛЬНЫЙ ВЫВОД ВСЕЙ ИНФОРМАЦИИ ПО МЕЙКЕРАМ ---

print("\n" + "="*80)
print("🔍 ПОДРОБНАЯ ИНФОРМАЦИЯ ПО ВСЕМ МЕЙКЕРАМ (MAKERS)")
print("="*80)

# Функция для безопасного получения значения
def safe_get(data, *keys, default="N/A"):
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key, default)
        else:
            return default
    return data

# Вывод BUY объявлений
if buy_orders:
    print("\n📈 BUY ОБЪЯВЛЕНИЯ (ПОКУПКА USDT ЗА RUB):")
    print("-"*80)
    for idx, order in enumerate(buy_orders, 1):
        print(f"\n🏷️  BUY МЕЙКЕР #{idx}")
        print("   " + "="*76)
        
        # Основная информация
        print(f"   👤 Nickname: {safe_get(order, 'nickName', default='N/A')}")
        print(f"   🆔 UID: {safe_get(order, 'uid', default='N/A')}")
        print(f"   🔑 User ID: {safe_get(order, 'userId', default='N/A')}")
        print(f"   📧 Email: {safe_get(order, 'email', default='N/A')}")
        
        # Цены и суммы
        print(f"   💰 Цена: {safe_get(order, 'price', default='N/A')} RUB")
        print(f"   📊 Мин. сумма: {safe_get(order, 'minAmount', default='N/A')} RUB")
        print(f"   📊 Макс. сумма: {safe_get(order, 'maxAmount', default='N/A')} RUB")
        print(f"   📦 Количество: {safe_get(order, 'quantity', default='N/A')} USDT")
        
        # Статистика мейкера
        print(f"   ⭐ Рейтинг: {safe_get(order, 'rating', default='N/A')}")
        print(f"   📈 Кол-во сделок: {safe_get(order, 'tradeCount', default='N/A')}")
        print(f"   ⏱️  Время онлайн: {safe_get(order, 'onlineTime', default='N/A')}")
        
        # Платежные методы
        payment_methods = order.get('paymentMethods', [])
        if payment_methods:
            print("   💳 Платежные методы:")
            for pm in payment_methods:
                print(f"      • {safe_get(pm, 'name', default='N/A')} " +
                      f"(ID: {safe_get(pm, 'id', default='N/A')})")
        else:
            print("   💳 Платежные методы: Не указаны")
        
        # Дополнительная информация
        print(f"   🌐 Страна: {safe_get(order, 'countryCode', default='N/A')}")
        print(f"   📱 Телефон: {safe_get(order, 'phone', default='N/A')}")
        print(f"   🏢 Регистрация: {safe_get(order, 'registerTime', default='N/A')}")
        
        # Полный JSON для отладки
        print(f"   📄 Полные данные: {json.dumps(order, ensure_ascii=False, indent=4)}")
        print("   " + "-"*76)

# Вывод SELL объявлений
if sell_orders:
    print("\n📉 SELL ОБЪЯВЛЕНИЯ (ПРОДАЖА USDT ЗА RUB):")
    print("-"*80)
    for idx, order in enumerate(sell_orders, 1):
        print(f"\n🏷️  SELL МЕЙКЕР #{idx}")
        print("   " + "="*76)
        
        # Основная информация
        print(f"   👤 Nickname: {safe_get(order, 'nickName', default='N/A')}")
        print(f"   🆔 UID: {safe_get(order, 'uid', default='N/A')}")
        print(f"   🔑 User ID: {safe_get(order, 'userId', default='N/A')}")
        print(f"   📧 Email: {safe_get(order, 'email', default='N/A')}")
        
        # Цены и суммы
        print(f"   💰 Цена: {safe_get(order, 'price', default='N/A')} RUB")
        print(f"   📊 Мин. сумма: {safe_get(order, 'minAmount', default='N/A')} RUB")
        print(f"   📊 Макс. сумма: {safe_get(order, 'maxAmount', default='N/A')} RUB")
        print(f"   📦 Количество: {safe_get(order, 'quantity', default='N/A')} USDT")
        
        # Статистика мейкера
        print(f"   ⭐ Рейтинг: {safe_get(order, 'rating', default='N/A')}")
        print(f"   📈 Кол-во сделок: {safe_get(order, 'tradeCount', default='N/A')}")
        print(f"   ⏱️  Время онлайн: {safe_get(order, 'onlineTime', default='N/A')}")
        
        # Платежные методы
        payment_methods = order.get('paymentMethods', [])
        if payment_methods:
            print("   💳 Платежные методы:")
            for pm in payment_methods:
                print(f"      • {safe_get(pm, 'name', default='N/A')} " +
                      f"(ID: {safe_get(pm, 'id', default='N/A')})")
        else:
            print("   💳 Платежные методы: Не указаны")
        
        # Дополнительная информация
        print(f"   🌐 Страна: {safe_get(order, 'countryCode', default='N/A')}")
        print(f"   📱 Телефон: {safe_get(order, 'phone', default='N/A')}")
        print(f"   🏢 Регистрация: {safe_get(order, 'registerTime', default='N/A')}")
        
        # Полный JSON для отладки
        print(f"   📄 Полные данные: {json.dumps(order, ensure_ascii=False, indent=4)}")
        print("   " + "-"*76)

# Сводка по всем мейкерам
print("\n" + "="*80)
print("📊 СВОДКА ПО ВСЕМ МЕЙКЕРАМ")
print("="*80)

all_makers = []
if buy_orders:
    for order in buy_orders:
        all_makers.append({
            'type': 'BUY',
            'nickname': safe_get(order, 'nickName'),
            'uid': safe_get(order, 'uid'),
            'price': safe_get(order, 'price'),
            'rating': safe_get(order, 'rating'),
            'trades': safe_get(order, 'tradeCount')
        })
if sell_orders:
    for order in sell_orders:
        all_makers.append({
            'type': 'SELL',
            'nickname': safe_get(order, 'nickName'),
            'uid': safe_get(order, 'uid'),
            'price': safe_get(order, 'price'),
            'rating': safe_get(order, 'rating'),
            'trades': safe_get(order, 'tradeCount')
        })

if all_makers:
    print(f"\n{'Тип':<6} {'Никнейм':<20} {'UID':<15} {'Цена RUB':<12} {'Рейтинг':<8} {'Сделок':<8}")
    print("-"*80)
    for maker in all_makers:
        print(f"{maker['type']:<6} {maker['nickname']:<20} {maker['uid']:<15} "
              f"{maker['price']:<12} {maker['rating']:<8} {maker['trades']:<8}")
    print(f"\n📌 Всего мейкеров: {len(all_makers)}")
    print(f"   BUY мейкеров: {len(buy_orders) if buy_orders else 0}")
    print(f"   SELL мейкеров: {len(sell_orders) if sell_orders else 0}")
else:
    print("\n❌ Нет данных о мейкерах")

print("\n" + "="*80)
print("🏁 Завершение работы")
