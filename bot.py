import asyncio
import os
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
import requests

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import ParseMode
from aiogram.utils import executor
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

class P2PArbitrageBot:
    """Основной класс бота для P2P арбитража"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.is_running = False
        self.monitor_task: Optional[asyncio.Task] = None
        
    async def start(self):
        """Запуск бота"""
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
        """Получение P2P-объявлений с Bybit (синхронно)"""
        try:
            params = {
                "side": side,
                "fiat": fiat,
                "symbol": "USDT",
                "page": "1",
                "size": "50"
            }
            
            response = requests.get(BYBIT_P2P_URL, params=params, timeout=10)
            
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
        message = f"""
🔥 <b>АРБИТРАЖНЫЙ СИГНАЛ</b> 🔥

<b>🟢 ПРОДАВЕЦ (SELLER)</b>
• Курс: {signal.seller.price:.2f}₽
• Сумма: {signal.seller.amount:,.0f}₽
• Лимиты: {signal.seller.min_amount:,.0f} - {signal.seller.max_amount:,.0f}₽
• Платежи: {', '.join(signal.seller.payment_methods) if signal.seller.payment_methods else 'Любые'}
• Мерчант: {signal.seller.merchant_name} {'✅' if signal.seller.is_verified else '❌'}

<b>🔴 ПОКУПАТЕЛЬ (BUYER)</b>
• Курс: {signal.buyer.price:.2f}₽
• Сумма: {signal.buyer.amount:,.0f}₽
• Лимиты: {signal.buyer.min_amount:,.0f} - {signal.buyer.max_amount:,.0f}₽
• Платежи: {', '.join(signal.buyer.payment_methods) if signal.buyer.payment_methods else 'Любые'}
• Мерчант: {signal.buyer.merchant_name} {'✅' if signal.buyer.is_verified else '❌'}

<b>📊 РАСЧЕТ</b>
• Спред: <b>{signal.spread:.2f}%</b>
• Прибыль с 1 USDT: {signal.profit:.2f}₽

<b>🔗 Ссылки:</b>
Продавец: {signal.seller.link if signal.seller.link else 'Нет ссылки'}
Покупатель: {signal.buyer.link if signal.buyer.link else 'Нет ссылки'}

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
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())
arbitrage_bot = P2PArbitrageBot(bot)

# --- Обработчики команд ---

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    """Команда /start"""
    welcome_text = """
🚀 <b>Добро пожаловать в P2P Арбитраж Бот!</b>

Я ищу арбитражные связки на Bybit P2P и присылаю тебе сигналы.

<b>Доступные команды:</b>
/start - Показать это сообщение
/settings - Настройка фильтров
/status - Статус мониторинга
/start_monitoring - Запустить мониторинг
/stop_monitoring - Остановить мониторинг
/clear_filters - Очистить все фильтры
/help - Помощь

<b>Как это работает:</b>
1. Настрой фильтры через /settings
2. Запусти мониторинг /start_monitoring
3. Бот будет искать выгодные связки
4. При найденной связке получишь сигнал
    """
    await message.answer(welcome_text, parse_mode=ParseMode.HTML)
    
    if message.from_user.id not in user_filters:
        user_filters[message.from_user.id] = {}
        user_subscriptions[message.from_user.id] = False

@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    """Команда /help"""
    help_text = """
📖 <b>Помощь по фильтрам</b>

<b>Что можно настраивать:</b>

1. <b>Сумма сделки</b>
   • /set_exact 28000 - строго 28 000 ₽
   • /set_min 25000 - минимум 25 000 ₽
   • /set_max 30000 - максимум 30 000 ₽

2. <b>Текстовые условия</b>
   • /add_blacklist СБП - исключить объявления с "СБП"
   • /add_whitelist Т-Банк - искать только с "Т-Банк"
   • /remove_blacklist СБП - убрать из черного списка
   • /remove_whitelist Т-Банк - убрать из белого списка

3. <b>Платежные системы</b>
   • /add_payment Т-Банк - добавить платежную систему
   • /remove_payment Т-Банк - убрать платежную систему

4. <b>Спред</b>
   • /set_spread 0.5 - минимальный спред 0.5%

5. <b>Управление</b>
   • /start_monitoring - запуск поиска
   • /stop_monitoring - остановка поиска
   • /status - текущий статус
   • /clear_filters - очистить все фильтры

<b>Пример настройки:</b>
1. /set_exact 28000
2. /add_blacklist СБП
3. /add_whitelist Т-Банк
4. /set_spread 0.5
5. /start_monitoring
    """
    await message.answer(help_text, parse_mode=ParseMode.HTML)

@dp.message_handler(commands=['settings'])
async def cmd_settings(message: types.Message):
    """Показать текущие настройки"""
    settings_text = await arbitrage_bot.get_filter_settings(message.from_user.id)
    await message.answer(settings_text, parse_mode=ParseMode.HTML)

@dp.message_handler(commands=['status'])
async def cmd_status(message: types.Message):
    """Статус мониторинга"""
    user_id = message.from_user.id
    is_active = user_subscriptions.get(user_id, False)
    status_emoji = "🟢" if is_active else "🔴"
    status_text = "Активен" if is_active else "Остановлен"
    
    settings_preview = await arbitrage_bot.get_filter_settings(user_id)
    
    await message.answer(
        f"<b>Статус мониторинга:</b> {status_emoji} {status_text}\n\n"
        f"{settings_preview}",
        parse_mode=ParseMode.HTML
    )

@dp.message_handler(commands=['start_monitoring'])
async def cmd_start_monitoring(message: types.Message):
    """Запуск мониторинга"""
    user_id = message.from_user.id
    filters = user_filters.get(user_id, {})
    
    if not filters:
        await message.answer(
            "⚠️ Сначала настройте фильтры!\n"
            "Используйте /settings для просмотра и /help для инструкций."
        )
        return
    
    user_subscriptions[user_id] = True
    await message.answer(
        "✅ Мониторинг запущен!\n"
        "Бот будет присылать сигналы при нахождении выгодных связок.\n"
        "Для остановки используйте /stop_monitoring"
    )

@dp.message_handler(commands=['stop_monitoring'])
async def cmd_stop_monitoring(message: types.Message):
    """Остановка мониторинга"""
    user_id = message.from_user.id
    user_subscriptions[user_id] = False
    await message.answer("⏹ Мониторинг остановлен.")

@dp.message_handler(commands=['clear_filters'])
async def cmd_clear_filters(message: types.Message):
    """Очистка всех фильтров"""
    user_id = message.from_user.id
    user_filters[user_id] = {}
    user_subscriptions[user_id] = False
    await message.answer("🧹 Все фильтры очищены. Мониторинг остановлен.")

# --- Команды для настройки фильтров ---

@dp.message_handler(commands=['set_exact'])
async def cmd_set_exact(message: types.Message):
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("❌ Использование: /set_exact <сумма>\nПример: /set_exact 28000")
            return
        
        amount = float(args[1])
        if amount <= 0:
            await message.answer("❌ Сумма должна быть положительной")
            return
        
        user_id = message.from_user.id
        if user_id not in user_filters:
            user_filters[user_id] = {}
        
        user_filters[user_id].pop("min_amount", None)
        user_filters[user_id].pop("max_amount", None)
        user_filters[user_id]["exact_amount"] = amount
        
        await message.answer(f"✅ Установлена точная сумма: {amount:,.0f}₽")
    except ValueError:
        await message.answer("❌ Введите корректное число")

@dp.message_handler(commands=['set_min'])
async def cmd_set_min(message: types.Message):
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("❌ Использование: /set_min <сумма>\nПример: /set_min 25000")
            return
        
        amount = float(args[1])
        if amount <= 0:
            await message.answer("❌ Сумма должна быть положительной")
            return
        
        user_id = message.from_user.id
        if user_id not in user_filters:
            user_filters[user_id] = {}
        
        user_filters[user_id].pop("exact_amount", None)
        user_filters[user_id]["min_amount"] = amount
        
        await message.answer(f"✅ Установлена минимальная сумма: {amount:,.0f}₽")
    except ValueError:
        await message.answer("❌ Введите корректное число")

@dp.message_handler(commands=['set_max'])
async def cmd_set_max(message: types.Message):
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("❌ Использование: /set_max <сумма>\nПример: /set_max 30000")
            return
        
        amount = float(args[1])
        if amount <= 0:
            await message.answer("❌ Сумма должна быть положительной")
            return
        
        user_id = message.from_user.id
        if user_id not in user_filters:
            user_filters[user_id] = {}
        
        user_filters[user_id].pop("exact_amount", None)
        user_filters[user_id]["max_amount"] = amount
        
        await message.answer(f"✅ Установлена максимальная сумма: {amount:,.0f}₽")
    except ValueError:
        await message.answer("❌ Введите корректное число")

@dp.message_handler(commands=['set_spread'])
async def cmd_set_spread(message: types.Message):
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("❌ Использование: /set_spread <процент>\nПример: /set_spread 0.5")
            return
        
        spread = float(args[1])
        if spread < 0:
            await message.answer("❌ Спред должен быть положительным")
            return
        
        user_id = message.from_user.id
        if user_id not in user_filters:
            user_filters[user_id] = {}
        
        user_filters[user_id]["min_spread"] = spread
        
        await message.answer(f"✅ Установлен минимальный спред: {spread}%")
    except ValueError:
        await message.answer("❌ Введите корректное число")

@dp.message_handler(commands=['add_blacklist'])
async def cmd_add_blacklist(message: types.Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Использование: /add_blacklist <слово>\nПример: /add_blacklist СБП")
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters:
        user_filters[user_id] = {}
    
    if "blacklist" not in user_filters[user_id]:
        user_filters[user_id]["blacklist"] = []
    
    if word not in user_filters[user_id]["blacklist"]:
        user_filters[user_id]["blacklist"].append(word)
        await message.answer(f"✅ Добавлено в черный список: {word}")
    else:
        await message.answer(f"⚠️ Слово '{word}' уже в черном списке")

@dp.message_handler(commands=['remove_blacklist'])
async def cmd_remove_blacklist(message: types.Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Использование: /remove_blacklist <слово>\nПример: /remove_blacklist СБП")
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters or "blacklist" not in user_filters[user_id]:
        await message.answer("⚠️ Черный список пуст")
        return
    
    if word in user_filters[user_id]["blacklist"]:
        user_filters[user_id]["blacklist"].remove(word)
        await message.answer(f"✅ Удалено из черного списка: {word}")
        if not user_filters[user_id]["blacklist"]:
            del user_filters[user_id]["blacklist"]
    else:
        await message.answer(f"⚠️ Слово '{word}' не найдено в черном списке")

@dp.message_handler(commands=['add_whitelist'])
async def cmd_add_whitelist(message: types.Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Использование: /add_whitelist <слово>\nПример: /add_whitelist Т-Банк")
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters:
        user_filters[user_id] = {}
    
    if "whitelist" not in user_filters[user_id]:
        user_filters[user_id]["whitelist"] = []
    
    if word not in user_filters[user_id]["whitelist"]:
        user_filters[user_id]["whitelist"].append(word)
        await message.answer(f"✅ Добавлено в белый список: {word}")
    else:
        await message.answer(f"⚠️ Слово '{word}' уже в белом списке")

@dp.message_handler(commands=['remove_whitelist'])
async def cmd_remove_whitelist(message: types.Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Использование: /remove_whitelist <слово>\nПример: /remove_whitelist Т-Банк")
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters or "whitelist" not in user_filters[user_id]:
        await message.answer("⚠️ Белый список пуст")
        return
    
    if word in user_filters[user_id]["whitelist"]:
        user_filters[user_id]["whitelist"].remove(word)
        await message.answer(f"✅ Удалено из белого списка: {word}")
        if not user_filters[user_id]["whitelist"]:
            del user_filters[user_id]["whitelist"]
    else:
        await message.answer(f"⚠️ Слово '{word}' не найдено в белом списке")

@dp.message_handler(commands=['add_payment'])
async def cmd_add_payment(message: types.Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Использование: /add_payment <система>\nПример: /add_payment Т-Банк")
        return
    
    payment = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters:
        user_filters[user_id] = {}
    
    if "payment_methods" not in user_filters[user_id]:
        user_filters[user_id]["payment_methods"] = []
    
    if payment not in user_filters[user_id]["payment_methods"]:
        user_filters[user_id]["payment_methods"].append(payment)
        await message.answer(f"✅ Добавлена платежная система: {payment}")
    else:
        await message.answer(f"⚠️ Платежная система '{payment}' уже добавлена")

@dp.message_handler(commands=['remove_payment'])
async def cmd_remove_payment(message: types.Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("❌ Использование: /remove_payment <система>\nПример: /remove_payment Т-Банк")
        return
    
    payment = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters or "payment_methods" not in user_filters[user_id]:
        await message.answer("⚠️ Список платежных систем пуст")
        return
    
    if payment in user_filters[user_id]["payment_methods"]:
        user_filters[user_id]["payment_methods"].remove(payment)
        await message.answer(f"✅ Удалена платежная система: {payment}")
        if not user_filters[user_id]["payment_methods"]:
            del user_filters[user_id]["payment_methods"]
    else:
        await message.answer(f"⚠️ Платежная система '{payment}' не найдена")


async def on_startup(dp):
    await arbitrage_bot.start()
    logger.info("Бот запущен и готов к работе!")

async def on_shutdown(dp):
    await arbitrage_bot.stop()
    logger.info("Бот остановлен")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
