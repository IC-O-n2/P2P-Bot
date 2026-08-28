import asyncio
import os
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
import requests
import html
import hashlib
import hmac
import time

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не найден в переменных окружения")

# URL API Bybit
BYBIT_P2P_URL = "https://api.bybit.com/v5/market/p2p/orderbook"

# Хранилище для фильтров пользователей
user_filters: Dict[int, Dict] = {}
user_subscriptions: Dict[int, bool] = {}

@dataclass
class P2POffer:
    """Класс для хранения данных P2P-объявления"""
    side: str
    price: float
    amount: float
    min_amount: float
    max_amount: float
    payment_methods: List[str]
    description: str
    link: str
    merchant_name: str
    is_verified: bool

@dataclass
class ArbitrageSignal:
    """Класс для хранения сигнала арбитража"""
    seller: P2POffer
    buyer: P2POffer
    spread: float
    profit: float
    timestamp: datetime

class BybitAPI:
    """Класс для работы с Bybit API с авторизацией"""
    
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bybit.com"
    
    def _generate_signature(self, params: Dict, timestamp: int) -> str:
        """Генерация подписи для запроса"""
        # Сортируем параметры
        sorted_params = sorted(params.items())
        query_string = "&".join([f"{k}={v}" for k, v in sorted_params])
        
        # Формируем строку для подписи
        sign_str = f"{timestamp}{self.api_key}{query_string}"
        
        # Генерируем HMAC-SHA256 подпись
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            sign_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    
    def _get_headers(self, params: Dict) -> Dict:
        """Получение заголовков для запроса"""
        timestamp = int(time.time() * 1000)
        signature = self._generate_signature(params, timestamp)
        
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": str(timestamp),
            "X-BAPI-SIGN": signature,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
    
    def get_p2p_offers(self, side: str, fiat: str = "RUB") -> List[P2POffer]:
        """Получение P2P-объявлений с Bybit с авторизацией"""
        try:
            params = {
                "side": side,
                "fiat": fiat,
                "symbol": "USDT",
                "page": "1",
                "size": "50"
            }
            
            headers = self._get_headers(params)
            
            response = requests.get(
                BYBIT_P2P_URL,
                params=params,
                headers=headers,
                timeout=10
            )
            
            if response.status_code != 200:
                logger.error(f"Ошибка API Bybit: {response.status_code}")
                logger.debug(f"Response: {response.text[:200]}")
                return []
            
            data = response.json()
            if data.get("retCode") != 0:
                logger.error(f"Ошибка Bybit: {data.get('retMsg')}")
                return []
            
            offers = []
            for item in data.get("result", {}).get("items", []):
                try:
                    price_str = item.get("price", "0")
                    amount_str = item.get("amount", "0")
                    min_amount_str = item.get("minAmount", "0")
                    max_amount_str = item.get("maxAmount", "0")
                    
                    offer = P2POffer(
                        side=side,
                        price=float(price_str) if price_str else 0,
                        amount=float(amount_str) if amount_str else 0,
                        min_amount=float(min_amount_str) if min_amount_str else 0,
                        max_amount=float(max_amount_str) if max_amount_str else 0,
                        payment_methods=[p.get("name", "") for p in item.get("payment", [])],
                        description=item.get("description", ""),
                        link=item.get("link", ""),
                        merchant_name=item.get("merchant", {}).get("name", "Аноним"),
                        is_verified=item.get("merchant", {}).get("verified", False)
                    )
                    offers.append(offer)
                except (ValueError, KeyError) as e:
                    logger.warning(f"Ошибка парсинга объявления: {e}")
                    continue
            
            logger.info(f"Получено {len(offers)} объявлений для {side}")
            return offers
                
        except Exception as e:
            logger.error(f"Ошибка при запросе к Bybit: {e}")
            return []

class P2PArbitrageBot:
    """Основной класс бота для P2P арбитража"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.is_running = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.bybit_api = BybitAPI(BYBIT_API_KEY, BYBIT_API_SECRET) if BYBIT_API_KEY and BYBIT_API_SECRET else None
        
    async def start(self):
        """Запуск бота"""
        if not self.bybit_api:
            logger.warning("Bybit API ключи не настроены! Бот будет работать в публичном режиме.")
        
        self.is_running = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Бот успешно запущен")
        
    async def stop(self):
        """Остановка бота"""
        self.is_running = False
        if self.monitor_task:
            self.monitor_task.cancel()
        logger.info("Бот остановлен")
    
    def _fetch_p2p_offers_sync(self, side: str, fiat: str = "RUB") -> List[P2POffer]:
        """Получение P2P-объявлений с Bybit"""
        if self.bybit_api:
            # Используем авторизованный запрос
            return self.bybit_api.get_p2p_offers(side, fiat)
        else:
            # Используем публичный запрос
            return self._fetch_p2p_offers_public(side, fiat)
    
    def _fetch_p2p_offers_public(self, side: str, fiat: str = "RUB") -> List[P2POffer]:
        """Получение P2P-объявлений с Bybit (публичный запрос)"""
        try:
            params = {
                "side": side,
                "fiat": fiat,
                "symbol": "USDT",
                "page": "1",
                "size": "50"
            }
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            
            response = requests.get(BYBIT_P2P_URL, params=params, headers=headers, timeout=10)
            
            if response.status_code != 200:
                logger.error(f"Ошибка API Bybit: {response.status_code}")
                return []
            
            data = response.json()
            if data.get("retCode") != 0:
                logger.error(f"Ошибка Bybit: {data.get('retMsg')}")
                return []
            
            offers = []
            for item in data.get("result", {}).get("items", []):
                try:
                    price_str = item.get("price", "0")
                    amount_str = item.get("amount", "0")
                    min_amount_str = item.get("minAmount", "0")
                    max_amount_str = item.get("maxAmount", "0")
                    
                    offer = P2POffer(
                        side=side,
                        price=float(price_str) if price_str else 0,
                        amount=float(amount_str) if amount_str else 0,
                        min_amount=float(min_amount_str) if min_amount_str else 0,
                        max_amount=float(max_amount_str) if max_amount_str else 0,
                        payment_methods=[p.get("name", "") for p in item.get("payment", [])],
                        description=item.get("description", ""),
                        link=item.get("link", ""),
                        merchant_name=item.get("merchant", {}).get("name", "Аноним"),
                        is_verified=item.get("merchant", {}).get("verified", False)
                    )
                    offers.append(offer)
                except (ValueError, KeyError) as e:
                    logger.warning(f"Ошибка парсинга объявления: {e}")
                    continue
            
            logger.info(f"Получено {len(offers)} объявлений для {side}")
            return offers
                
        except Exception as e:
            logger.error(f"Ошибка при запросе к Bybit: {e}")
            return []
    
    def _check_offer_conditions(self, offer: P2POffer, filters: Dict) -> Tuple[bool, str]:
        """Проверка условий мейкера для объявления"""
        if not filters:
            return True, "OK"
        
        # Проверка суммы
        if filters.get("exact_amount"):
            if abs(offer.amount - filters["exact_amount"]) > 0.01:
                return False, f"Сумма {offer.amount:.0f}₽ ≠ {filters['exact_amount']:.0f}₽"
        
        if filters.get("min_amount"):
            if offer.amount < filters["min_amount"]:
                return False, f"Сумма {offer.amount:.0f}₽ < {filters['min_amount']:.0f}₽"
        
        if filters.get("max_amount"):
            if offer.amount > filters["max_amount"]:
                return False, f"Сумма {offer.amount:.0f}₽ > {filters['max_amount']:.0f}₽"
        
        # Проверка текстовых условий
        description_lower = offer.description.lower()
        
        # Черный список (исключаем)
        for word in filters.get("blacklist", []):
            if word.lower() in description_lower:
                return False, f"Найдено запрещенное слово: {word}"
        
        # Белый список (требуем)
        whitelist = filters.get("whitelist", [])
        if whitelist:
            found = any(word.lower() in description_lower for word in whitelist)
            if not found:
                return False, f"Нет обязательных слов из: {', '.join(whitelist)}"
        
        # Проверка платежных систем
        if filters.get("payment_methods"):
            offer_methods = [m.lower() for m in offer.payment_methods]
            required = [m.lower() for m in filters["payment_methods"]]
            if not any(m in offer_methods for m in required):
                return False, f"Нет доступных платежных систем: {', '.join(filters['payment_methods'])}"
        
        return True, "OK"
    
    def _find_arbitrage(self, sellers: List[P2POffer], buyers: List[P2POffer],
                        user_filters: Dict) -> Optional[ArbitrageSignal]:
        """Поиск арбитражной связки"""
        if not sellers or not buyers:
            return None
        
        # Фильтруем продавцов и покупателей по условиям
        filtered_sellers = []
        for seller in sellers:
            passes, _ = self._check_offer_conditions(seller, user_filters)
            if passes:
                filtered_sellers.append(seller)
        
        filtered_buyers = []
        for buyer in buyers:
            passes, _ = self._check_offer_conditions(buyer, user_filters)
            if passes:
                filtered_buyers.append(buyer)
        
        if not filtered_sellers or not filtered_buyers:
            return None
        
        # Берем лучшего продавца (самый дешевый) и покупателя (самый дорогой)
        best_seller = min(filtered_sellers, key=lambda x: x.price)
        best_buyer = max(filtered_buyers, key=lambda x: x.price)
        
        # Проверяем, что продавец дешевле покупателя
        if best_seller.price >= best_buyer.price:
            return None
        
        spread = ((best_buyer.price / best_seller.price) - 1) * 100
        
        # Проверка минимального спреда
        min_spread = user_filters.get("min_spread", 0.5)
        if spread < min_spread:
            return None
        
        profit = best_buyer.price - best_seller.price
        
        return ArbitrageSignal(
            seller=best_seller,
            buyer=best_buyer,
            spread=spread,
            profit=profit,
            timestamp=datetime.now()
        )
    
    async def _monitor_loop(self):
        """Основной цикл мониторинга"""
        while self.is_running:
            try:
                for user_id, filters in user_filters.items():
                    if not user_subscriptions.get(user_id, False):
                        continue
                    
                    # Получаем объявления (синхронно, но в отдельном потоке)
                    sellers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "Sell"
                    )
                    buyers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "Buy"
                    )
                    
                    if not sellers or not buyers:
                        continue
                    
                    # Ищем арбитраж
                    signal = self._find_arbitrage(sellers, buyers, filters)
                    
                    if signal:
                        await self._send_signal(user_id, signal)
                
                # Ждем перед следующим циклом
                await asyncio.sleep(10)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(30)
    
    async def _send_signal(self, user_id: int, signal: ArbitrageSignal):
        """Отправка сигнала пользователю"""
        # Экранируем специальные символы для HTML
        def escape_html(text):
            return html.escape(str(text))
        
        seller_payments = ', '.join(signal.seller.payment_methods) if signal.seller.payment_methods else 'Любые'
        buyer_payments = ', '.join(signal.buyer.payment_methods) if signal.buyer.payment_methods else 'Любые'
        
        message = f"""
🔥 <b>АРБИТРАЖНЫЙ СИГНАЛ</b> 🔥

<b>🟢 ПРОДАВЕЦ (SELLER)</b>
• Курс: {signal.seller.price:.2f}₽
• Сумма: {signal.seller.amount:,.0f}₽
• Лимиты: {signal.seller.min_amount:,.0f} - {signal.seller.max_amount:,.0f}₽
• Платежи: {escape_html(seller_payments)}
• Мерчант: {escape_html(signal.seller.merchant_name)} {'✅' if signal.seller.is_verified else '❌'}

<b>🔴 ПОКУПАТЕЛЬ (BUYER)</b>
• Курс: {signal.buyer.price:.2f}₽
• Сумма: {signal.buyer.amount:,.0f}₽
• Лимиты: {signal.buyer.min_amount:,.0f} - {signal.buyer.max_amount:,.0f}₽
• Платежи: {escape_html(buyer_payments)}
• Мерчант: {escape_html(signal.buyer.merchant_name)} {'✅' if signal.buyer.is_verified else '❌'}

<b>📊 РАСЧЕТ</b>
• Спред: <b>{signal.spread:.2f}%</b>
• Прибыль с 1 USDT: {signal.profit:.2f}₽

<b>🔗 Ссылки:</b>
Продавец: {escape_html(signal.seller.link) if signal.seller.link else 'Нет ссылки'}
Покупатель: {escape_html(signal.buyer.link) if signal.buyer.link else 'Нет ссылки'}

⏰ {signal.timestamp.strftime('%H:%M:%S')}
        """
        
        try:
            await self.bot.send_message(
                user_id,
                message,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Сигнал отправлен пользователю {user_id}")
        except Exception as e:
            logger.error(f"Ошибка отправки сигнала: {e}")
    
    async def get_filter_settings(self, user_id: int) -> str:
        """Получение текущих настроек фильтров"""
        filters = user_filters.get(user_id, {})
        if not filters:
            return "🔧 Фильтры не настроены. Используйте /settings для настройки."
        
        settings = []
        settings.append("📋 <b>Текущие настройки фильтров:</b>")
        settings.append("")
        
        if filters.get("exact_amount"):
            settings.append(f"• Точная сумма: {filters['exact_amount']:.0f}₽")
        if filters.get("min_amount"):
            settings.append(f"• Мин. сумма: {filters['min_amount']:.0f}₽")
        if filters.get("max_amount"):
            settings.append(f"• Макс. сумма: {filters['max_amount']:.0f}₽")
        if filters.get("min_spread"):
            settings.append(f"• Мин. спред: {filters['min_spread']}%")
        if filters.get("blacklist"):
            settings.append(f"• Черный список: {', '.join(filters['blacklist'])}")
        if filters.get("whitelist"):
            settings.append(f"• Белый список: {', '.join(filters['whitelist'])}")
        if filters.get("payment_methods"):
            settings.append(f"• Платежные системы: {', '.join(filters['payment_methods'])}")
        
        if len(settings) == 2:
            settings.append("⚠️ Фильтры настроены, но неактивны (запустите /start_monitoring)")
        
        return "\n".join(settings)


# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
arbitrage_bot = P2PArbitrageBot(bot)

# Функция для безопасной отправки сообщений
async def safe_send_message(message: Message, text: str):
    """Отправка сообщения с безопасной обработкой HTML"""
    try:
        await message.answer(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        # Если HTML не проходит, отправляем без форматирования
        logger.warning(f"Ошибка HTML-парсинга, отправляем обычный текст: {e}")
        await message.answer(text.replace('<', '[').replace('>', ']'))

# --- Обработчики команд ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start"""
    welcome_text = """
🚀 Добро пожаловать в P2P Арбитраж Бот!

Я ищу арбитражные связки на Bybit P2P и присылаю тебе сигналы.

Доступные команды:
/start - Показать это сообщение
/settings - Настройка фильтров
/status - Статус мониторинга
/start_monitoring - Запустить мониторинг
/stop_monitoring - Остановить мониторинг
/clear_filters - Очистить все фильтры
/help - Помощь

Как это работает:
1. Настрой фильтры через /settings
2. Запусти мониторинг /start_monitoring
3. Бот будет искать выгодные связки
4. При найденной связке получишь сигнал
    """
    await safe_send_message(message, welcome_text)
    
    if message.from_user.id not in user_filters:
        user_filters[message.from_user.id] = {}
        user_subscriptions[message.from_user.id] = False

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help"""
    help_text = """
📖 Помощь по фильтрам

Что можно настраивать:

1. Сумма сделки
   /set_exact 28000 - строго 28 000 ₽
   /set_min 25000 - минимум 25 000 ₽
   /set_max 30000 - максимум 30 000 ₽

2. Текстовые условия
   /add_blacklist СБП - исключить объявления с "СБП"
   /add_whitelist Т-Банк - искать только с "Т-Банк"
   /remove_blacklist СБП - убрать из черного списка
   /remove_whitelist Т-Банк - убрать из белого списка

3. Платежные системы
   /add_payment Т-Банк - добавить платежную систему
   /remove_payment Т-Банк - убрать платежную систему

4. Спред
   /set_spread 0.5 - минимальный спред 0.5%

5. Управление
   /start_monitoring - запуск поиска
   /stop_monitoring - остановка поиска
   /status - текущий статус
   /clear_filters - очистить все фильтры

Пример настройки:
1. /set_exact 28000
2. /add_blacklist СБП
3. /add_whitelist Т-Банк
4. /set_spread 0.5
5. /start_monitoring
    """
    await safe_send_message(message, help_text)

@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    """Показать текущие настройки"""
    settings_text = await arbitrage_bot.get_filter_settings(message.from_user.id)
    await safe_send_message(message, settings_text)

@dp.message(Command("status"))
async def cmd_status(message: Message):
    """Статус мониторинга"""
    user_id = message.from_user.id
    is_active = user_subscriptions.get(user_id, False)
    status_emoji = "🟢" if is_active else "🔴"
    status_text = "Активен" if is_active else "Остановлен"
    
    settings_preview = await arbitrage_bot.get_filter_settings(user_id)
    
    status_message = f"""
Статус мониторинга: {status_emoji} {status_text}

{settings_preview}
    """
    await safe_send_message(message, status_message)

@dp.message(Command("start_monitoring"))
async def cmd_start_monitoring(message: Message):
    """Запуск мониторинга"""
    user_id = message.from_user.id
    filters = user_filters.get(user_id, {})
    
    if not filters:
        await safe_send_message(
            message,
            "⚠️ Сначала настройте фильтры!\n"
            "Используйте /settings для просмотра и /help для инструкций."
        )
        return
    
    user_subscriptions[user_id] = True
    await safe_send_message(
        message,
        "✅ Мониторинг запущен!\n"
        "Бот будет присылать сигналы при нахождении выгодных связок.\n"
        "Для остановки используйте /stop_monitoring"
    )

@dp.message(Command("stop_monitoring"))
async def cmd_stop_monitoring(message: Message):
    """Остановка мониторинга"""
    user_id = message.from_user.id
    user_subscriptions[user_id] = False
    await safe_send_message(message, "⏹ Мониторинг остановлен.")

@dp.message(Command("clear_filters"))
async def cmd_clear_filters(message: Message):
    """Очистка всех фильтров"""
    user_id = message.from_user.id
    user_filters[user_id] = {}
    user_subscriptions[user_id] = False
    await safe_send_message(message, "🧹 Все фильтры очищены. Мониторинг остановлен.")

# --- Команды для настройки фильтров ---

@dp.message(Command("set_exact"))
async def cmd_set_exact(message: Message):
    try:
        args = message.text.split()
        if len(args) != 2:
            await safe_send_message(message, "❌ Использование: /set_exact <сумма>\nПример: /set_exact 28000")
            return
        
        amount = float(args[1])
        if amount <= 0:
            await safe_send_message(message, "❌ Сумма должна быть положительной")
            return
        
        user_id = message.from_user.id
        if user_id not in user_filters:
            user_filters[user_id] = {}
        
        user_filters[user_id].pop("min_amount", None)
        user_filters[user_id].pop("max_amount", None)
        user_filters[user_id]["exact_amount"] = amount
        
        await safe_send_message(message, f"✅ Установлена точная сумма: {amount:,.0f}₽")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("set_min"))
async def cmd_set_min(message: Message):
    try:
        args = message.text.split()
        if len(args) != 2:
            await safe_send_message(message, "❌ Использование: /set_min <сумма>\nПример: /set_min 25000")
            return
        
        amount = float(args[1])
        if amount <= 0:
            await safe_send_message(message, "❌ Сумма должна быть положительной")
            return
        
        user_id = message.from_user.id
        if user_id not in user_filters:
            user_filters[user_id] = {}
        
        user_filters[user_id].pop("exact_amount", None)
        user_filters[user_id]["min_amount"] = amount
        
        await safe_send_message(message, f"✅ Установлена минимальная сумма: {amount:,.0f}₽")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("set_max"))
async def cmd_set_max(message: Message):
    try:
        args = message.text.split()
        if len(args) != 2:
            await safe_send_message(message, "❌ Использование: /set_max <сумма>\nПример: /set_max 30000")
            return
        
        amount = float(args[1])
        if amount <= 0:
            await safe_send_message(message, "❌ Сумма должна быть положительной")
            return
        
        user_id = message.from_user.id
        if user_id not in user_filters:
            user_filters[user_id] = {}
        
        user_filters[user_id].pop("exact_amount", None)
        user_filters[user_id]["max_amount"] = amount
        
        await safe_send_message(message, f"✅ Установлена максимальная сумма: {amount:,.0f}₽")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("set_spread"))
async def cmd_set_spread(message: Message):
    try:
        args = message.text.split()
        if len(args) != 2:
            await safe_send_message(message, "❌ Использование: /set_spread <процент>\nПример: /set_spread 0.5")
            return
        
        spread = float(args[1])
        if spread < 0:
            await safe_send_message(message, "❌ Спред должен быть положительным")
            return
        
        user_id = message.from_user.id
        if user_id not in user_filters:
            user_filters[user_id] = {}
        
        user_filters[user_id]["min_spread"] = spread
        
        await safe_send_message(message, f"✅ Установлен минимальный спред: {spread}%")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("add_blacklist"))
async def cmd_add_blacklist(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await safe_send_message(message, "❌ Использование: /add_blacklist <слово>\nПример: /add_blacklist СБП")
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters:
        user_filters[user_id] = {}
    
    if "blacklist" not in user_filters[user_id]:
        user_filters[user_id]["blacklist"] = []
    
    if word not in user_filters[user_id]["blacklist"]:
        user_filters[user_id]["blacklist"].append(word)
        await safe_send_message(message, f"✅ Добавлено в черный список: {word}")
    else:
        await safe_send_message(message, f"⚠️ Слово '{word}' уже в черном списке")

@dp.message(Command("remove_blacklist"))
async def cmd_remove_blacklist(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await safe_send_message(message, "❌ Использование: /remove_blacklist <слово>\nПример: /remove_blacklist СБП")
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters or "blacklist" not in user_filters[user_id]:
        await safe_send_message(message, "⚠️ Черный список пуст")
        return
    
    if word in user_filters[user_id]["blacklist"]:
        user_filters[user_id]["blacklist"].remove(word)
        await safe_send_message(message, f"✅ Удалено из черного списка: {word}")
        if not user_filters[user_id]["blacklist"]:
            del user_filters[user_id]["blacklist"]
    else:
        await safe_send_message(message, f"⚠️ Слово '{word}' не найдено в черном списке")

@dp.message(Command("add_whitelist"))
async def cmd_add_whitelist(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await safe_send_message(message, "❌ Использование: /add_whitelist <слово>\nПример: /add_whitelist Т-Банк")
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters:
        user_filters[user_id] = {}
    
    if "whitelist" not in user_filters[user_id]:
        user_filters[user_id]["whitelist"] = []
    
    if word not in user_filters[user_id]["whitelist"]:
        user_filters[user_id]["whitelist"].append(word)
        await safe_send_message(message, f"✅ Добавлено в белый список: {word}")
    else:
        await safe_send_message(message, f"⚠️ Слово '{word}' уже в белом списке")

@dp.message(Command("remove_whitelist"))
async def cmd_remove_whitelist(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await safe_send_message(message, "❌ Использование: /remove_whitelist <слово>\nПример: /remove_whitelist Т-Банк")
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters or "whitelist" not in user_filters[user_id]:
        await safe_send_message(message, "⚠️ Белый список пуст")
        return
    
    if word in user_filters[user_id]["whitelist"]:
        user_filters[user_id]["whitelist"].remove(word)
        await safe_send_message(message, f"✅ Удалено из белого списка: {word}")
        if not user_filters[user_id]["whitelist"]:
            del user_filters[user_id]["whitelist"]
    else:
        await safe_send_message(message, f"⚠️ Слово '{word}' не найдено в белом списке")

@dp.message(Command("add_payment"))
async def cmd_add_payment(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await safe_send_message(message, "❌ Использование: /add_payment <система>\nПример: /add_payment Т-Банк")
        return
    
    payment = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters:
        user_filters[user_id] = {}
    
    if "payment_methods" not in user_filters[user_id]:
        user_filters[user_id]["payment_methods"] = []
    
    if payment not in user_filters[user_id]["payment_methods"]:
        user_filters[user_id]["payment_methods"].append(payment)
        await safe_send_message(message, f"✅ Добавлена платежная система: {payment}")
    else:
        await safe_send_message(message, f"⚠️ Платежная система '{payment}' уже добавлена")

@dp.message(Command("remove_payment"))
async def cmd_remove_payment(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await safe_send_message(message, "❌ Использование: /remove_payment <система>\nПример: /remove_payment Т-Банк")
        return
    
    payment = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters or "payment_methods" not in user_filters[user_id]:
        await safe_send_message(message, "⚠️ Список платежных систем пуст")
        return
    
    if payment in user_filters[user_id]["payment_methods"]:
        user_filters[user_id]["payment_methods"].remove(payment)
        await safe_send_message(message, f"✅ Удалена платежная система: {payment}")
        if not user_filters[user_id]["payment_methods"]:
            del user_filters[user_id]["payment_methods"]
    else:
        await safe_send_message(message, f"⚠️ Платежная система '{payment}' не найдена")


async def on_startup():
    """Действия при запуске бота"""
    await arbitrage_bot.start()
    logger.info("Бот запущен и готов к работе!")

async def on_shutdown():
    """Действия при остановке бота"""
    await arbitrage_bot.stop()
    logger.info("Бот остановлен")

async def main():
    """Главная функция"""
    try:
        await on_startup()
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        await on_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
