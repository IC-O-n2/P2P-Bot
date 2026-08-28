import asyncio
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
import os

# ============== НАСТРОЙКИ ==============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")  # Если нужен для приватных эндпоинтов
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не найден в переменных окружения!")

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ============== ХРАНИЛИЩЕ НАСТРОЕК ПОЛЬЗОВАТЕЛЕЙ ==============
class UserSettings:
    def __init__(self):
        self.blacklist_words: List[str] = []
        self.whitelist_words: List[str] = []
        self.min_amount: Optional[float] = None
        self.max_amount: Optional[float] = None
        self.exact_amount: Optional[float] = None
        self.min_spread: float = 1.0  # минимальный спред в %
        self.payment_methods: List[str] = []
        self.is_active: bool = True
        self.last_signal_time: Optional[datetime] = None
        self.cooldown_seconds: int = 60  # минимальный интервал между сигналами

# Словарь для хранения настроек пользователей
user_settings: Dict[int, UserSettings] = {}

def get_user_settings(user_id: int) -> UserSettings:
    if user_id not in user_settings:
        user_settings[user_id] = UserSettings()
    return user_settings[user_id]

# ============== FSM СОСТОЯНИЯ ==============
class FilterStates(StatesGroup):
    waiting_for_blacklist = State()
    waiting_for_whitelist = State()
    waiting_for_min_amount = State()
    waiting_for_max_amount = State()
    waiting_for_exact_amount = State()
    waiting_for_spread = State()
    waiting_for_payment = State()

# ============== КЛАВИАТУРЫ ==============
def get_main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Настройки", callback_data="settings"),
        InlineKeyboardButton(text="🔍 Найти связку сейчас", callback_data="find_now"),
    )
    builder.row(
        InlineKeyboardButton(text="📈 Статистика", callback_data="stats"),
        InlineKeyboardButton(text="🔄 Обновить данные", callback_data="refresh"),
    )
    builder.row(
        InlineKeyboardButton(text="⏸ Пауза", callback_data="toggle_active"),
    )
    return builder.as_markup()

def get_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🚫 Черный список", callback_data="set_blacklist"),
        InlineKeyboardButton(text="✅ Белый список", callback_data="set_whitelist"),
    )
    builder.row(
        InlineKeyboardButton(text="💰 Сумма (мин)", callback_data="set_min_amount"),
        InlineKeyboardButton(text="💰 Сумма (макс)", callback_data="set_max_amount"),
    )
    builder.row(
        InlineKeyboardButton(text="🎯 Точная сумма", callback_data="set_exact_amount"),
        InlineKeyboardButton(text="📊 Мин. спред", callback_data="set_spread"),
    )
    builder.row(
        InlineKeyboardButton(text="💳 Платежные системы", callback_data="set_payment"),
        InlineKeyboardButton(text="🔄 Сбросить все", callback_data="reset_settings"),
    )
    builder.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"),
    )
    return builder.as_markup()

# ============== API BYBIT ==============
class BybitP2PAPI:
    BASE_URL = "https://api.bybit.com/v5/market/p2p"
    
    @staticmethod
    async def get_orders(side: str, fiat: str = "RUB", symbol: str = "USDT") -> List[Dict]:
        """
        Получение P2P объявлений с Bybit
        side: 'Sell' или 'Buy'
        """
        url = f"{BybitP2PAPI.BASE_URL}/orderbook"
        params = {
            "side": side,
            "fiat": fiat,
            "symbol": symbol
        }
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("retCode") == 0:
                            return data.get("result", {}).get("items", [])
                    logger.error(f"API Error: {response.status}")
                    return []
        except Exception as e:
            logger.error(f"Request error: {e}")
            return []

    @staticmethod
    async def find_arbitrage(
        user_id: int,
        settings: UserSettings,
        fiat: str = "RUB",
        symbol: str = "USDT"
    ) -> Optional[Dict]:
        """
        Поиск арбитражной связки с учетом фильтров пользователя
        """
        # Получаем продавцов и покупателей
        sellers = await BybitP2PAPI.get_orders("Sell", fiat, symbol)
        buyers = await BybitP2PAPI.get_orders("Buy", fiat, symbol)
        
        if not sellers or not buyers:
            return None
        
        # Фильтруем по условиям
        filtered_sellers = await BybitP2PAPI._filter_orders(sellers, settings, "seller")
        filtered_buyers = await BybitP2PAPI._filter_orders(buyers, settings, "buyer")
        
        if not filtered_sellers or not filtered_buyers:
            return None
        
        # Находим лучшие цены
        best_seller = min(filtered_sellers, key=lambda x: float(x.get("price", 0)))
        best_buyer = max(filtered_buyers, key=lambda x: float(x.get("price", 0)))
        
        seller_price = float(best_seller.get("price", 0))
        buyer_price = float(best_buyer.get("price", 0))
        
        if seller_price == 0 or buyer_price <= seller_price:
            return None
        
        # Считаем спред
        spread = ((buyer_price / seller_price) - 1) * 100
        
        if spread < settings.min_spread:
            return None
        
        # Формируем результат
        return {
            "seller_price": seller_price,
            "buyer_price": buyer_price,
            "spread": round(spread, 2),
            "seller_amount": best_seller.get("quantity", "0"),
            "buyer_amount": best_buyer.get("quantity", "0"),
            "seller_link": best_seller.get("link", ""),
            "buyer_link": best_buyer.get("link", ""),
            "seller_conditions": best_seller.get("description", ""),
            "buyer_conditions": best_buyer.get("description", ""),
            "seller_payment": best_seller.get("payment", []),
            "buyer_payment": best_buyer.get("payment", []),
            "timestamp": datetime.now()
        }

    @staticmethod
    async def _filter_orders(orders: List[Dict], settings: UserSettings, role: str) -> List[Dict]:
        """
        Фильтрация объявлений по настройкам пользователя
        """
        filtered = []
        
        for order in orders:
            try:
                # Проверка суммы
                amount = float(order.get("quantity", 0))
                if settings.exact_amount and amount != settings.exact_amount:
                    continue
                if settings.min_amount and amount < settings.min_amount:
                    continue
                if settings.max_amount and amount > settings.max_amount:
                    continue
                
                # Проверка текстовых условий
                description = order.get("description", "").lower()
                
                # Черный список
                if settings.blacklist_words:
                    if any(word.lower() in description for word in settings.blacklist_words):
                        continue
                
                # Белый список (если задан)
                if settings.whitelist_words:
                    if not any(word.lower() in description for word in settings.whitelist_words):
                        continue
                
                # Проверка платежных систем (если заданы)
                if settings.payment_methods:
                    order_payments = [p.get("payment_name", "").lower() for p in order.get("payment", [])]
                    if not any(payment in order_payments for payment in settings.payment_methods):
                        continue
                
                filtered.append(order)
                
            except Exception as e:
                logger.error(f"Filter error: {e}")
                continue
        
        return filtered

# ============== ФОРМАТИРОВАНИЕ СООБЩЕНИЙ ==============
def format_signal(signal: Dict) -> str:
    """Форматирование сигнала в красивое сообщение"""
    msg = f"🚀 <b>АРБИТРАЖНЫЙ СИГНАЛ</b> 🚀\n\n"
    msg += f"📊 <b>Спред:</b> {signal['spread']}%\n"
    msg += f"💰 <b>Сумма:</b> ~{signal['seller_amount']} USDT\n\n"
    
    msg += f"🟢 <b>SELLER</b>\n"
    msg += f"💵 Курс: <b>{signal['seller_price']} ₽</b>\n"
    msg += f"💳 Платеж: {', '.join(signal.get('seller_payment', ['Не указано']))}\n"
    if signal.get('seller_conditions'):
        msg += f"📝 Условия: {signal['seller_conditions'][:100]}...\n"
    msg += f"🔗 {signal['seller_link']}\n\n"
    
    msg += f"🔴 <b>BUYER</b>\n"
    msg += f"💵 Курс: <b>{signal['buyer_price']} ₽</b>\n"
    msg += f"💳 Платеж: {', '.join(signal.get('buyer_payment', ['Не указано']))}\n"
    if signal.get('buyer_conditions'):
        msg += f"📝 Условия: {signal['buyer_conditions'][:100]}...\n"
    msg += f"🔗 {signal['buyer_link']}\n\n"
    
    msg += f"⏰ {signal['timestamp'].strftime('%H:%M')}\n"
    msg += f"📈 Потенциальная прибыль: ~{signal['spread'] * 0.95:.2f}% (с учетом комиссий)"
    
    return msg

# ============== КОМАНДЫ БОТА ==============
@dp.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    settings.is_active = True
    
    welcome_text = (
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        f"Я бот для поиска P2P арбитражных связок на Bybit.\n\n"
        f"🔍 Я ищу разницу между ценой покупки и продажи USDT\n"
        f"📊 Учитываю твои фильтры (сумма, платежки, условия)\n"
        f"⚡️ Отправляю сигналы с высоким спредом\n\n"
        f"Настрой бота под себя и начни зарабатывать!"
    )
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    """Показать настройки"""
    await show_settings(message)

@dp.callback_query(F.data == "settings")
async def callback_settings(callback: CallbackQuery):
    await callback.answer()
    await show_settings(callback.message)

async def show_settings(message: Message):
    """Отображение текущих настроек"""
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    
    text = (
        "⚙️ <b>ТЕКУЩИЕ НАСТРОЙКИ</b>\n\n"
        f"🚫 Черный список: {', '.join(settings.blacklist_words) if settings.blacklist_words else 'Не задан'}\n"
        f"✅ Белый список: {', '.join(settings.whitelist_words) if settings.whitelist_words else 'Не задан'}\n"
        f"💰 Мин. сумма: {settings.min_amount if settings.min_amount else 'Не ограничена'}\n"
        f"💰 Макс. сумма: {settings.max_amount if settings.max_amount else 'Не ограничена'}\n"
        f"🎯 Точная сумма: {settings.exact_amount if settings.exact_amount else 'Не задана'}\n"
        f"📊 Мин. спред: {settings.min_spread}%\n"
        f"💳 Платежки: {', '.join(settings.payment_methods) if settings.payment_methods else 'Все'}\n"
        f"⏸ Статус: {'🟢 Активен' if settings.is_active else '🔴 Приостановлен'}\n"
    )
    
    await message.answer(text, reply_markup=get_settings_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "back_to_main")
async def callback_back_to_main(callback: CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await callback.message.answer("Главное меню", reply_markup=get_main_keyboard())

@dp.callback_query(F.data == "find_now")
async def callback_find_now(callback: CallbackQuery):
    await callback.answer("🔍 Ищу связку...")
    await find_arbitrage_now(callback.message)

@dp.message(Command("find"))
async def cmd_find(message: Message):
    await find_arbitrage_now(message)

async def find_arbitrage_now(message: Message):
    """Поиск арбитража прямо сейчас"""
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    
    if not settings.is_active:
        await message.answer("⚠️ Бот приостановлен. Включи в настройках.")
        return
    
    await message.answer("⏳ Ищу арбитражную связку... Пожалуйста, подожди.")
    
    try:
        signal = await BybitP2PAPI.find_arbitrage(user_id, settings)
        
        if signal:
            formatted_msg = format_signal(signal)
            await message.answer(formatted_msg, parse_mode="HTML")
            
            # Сохраняем время последнего сигнала
            settings.last_signal_time = datetime.now()
        else:
            await message.answer(
                "❌ Связка не найдена.\n\n"
                "Попробуй:\n"
                "• Уменьшить минимальный спред\n"
                "• Расширить фильтры (сумма, платежки)\n"
                "• Убрать часть условий"
            )
    except Exception as e:
        logger.error(f"Find error: {e}")
        await message.answer(f"❌ Ошибка поиска: {str(e)}")

# ============== НАСТРОЙКА ФИЛЬТРОВ ==============
@dp.callback_query(F.data == "set_blacklist")
async def callback_set_blacklist(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "🚫 Введите слова для черного списка через запятую\n"
        "Пример: СБП, Сбер, ВТБ\n\n"
        "Эти слова будут исключать объявления\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(FilterStates.waiting_for_blacklist)

@dp.message(FilterStates.waiting_for_blacklist)
async def process_blacklist(message: Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return
    
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    
    words = [w.strip() for w in message.text.split(",") if w.strip()]
    settings.blacklist_words = words
    
    await state.clear()
    await message.answer(
        f"✅ Черный список обновлен: {', '.join(words) if words else 'Пуст'}",
        reply_markup=get_settings_keyboard()
    )

@dp.callback_query(F.data == "set_whitelist")
async def callback_set_whitelist(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "✅ Введите слова для белого списка через запятую\n"
        "Пример: Т-Банк, Почта, Альфа\n\n"
        "Объявления без этих слов будут игнорироваться\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(FilterStates.waiting_for_whitelist)

@dp.message(FilterStates.waiting_for_whitelist)
async def process_whitelist(message: Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return
    
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    
    words = [w.strip() for w in message.text.split(",") if w.strip()]
    settings.whitelist_words = words
    
    await state.clear()
    await message.answer(
        f"✅ Белый список обновлен: {', '.join(words) if words else 'Пуст'}",
        reply_markup=get_settings_keyboard()
    )

@dp.callback_query(F.data == "set_min_amount")
async def callback_set_min_amount(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "💰 Введите минимальную сумму в RUB\n"
        "Пример: 10000\n\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(FilterStates.waiting_for_min_amount)

@dp.message(FilterStates.waiting_for_min_amount)
async def process_min_amount(message: Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return
    
    try:
        amount = float(message.text.replace(" ", ""))
        if amount <= 0:
            raise ValueError()
        
        user_id = message.from_user.id
        settings = get_user_settings(user_id)
        settings.min_amount = amount
        
        await state.clear()
        await message.answer(
            f"✅ Минимальная сумма: {amount} RUB",
            reply_markup=get_settings_keyboard()
        )
    except ValueError:
        await message.answer("❌ Некорректная сумма. Введите число больше 0")

@dp.callback_query(F.data == "set_max_amount")
async def callback_set_max_amount(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "💰 Введите максимальную сумму в RUB\n"
        "Пример: 50000\n\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(FilterStates.waiting_for_max_amount)

@dp.message(FilterStates.waiting_for_max_amount)
async def process_max_amount(message: Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return
    
    try:
        amount = float(message.text.replace(" ", ""))
        if amount <= 0:
            raise ValueError()
        
        user_id = message.from_user.id
        settings = get_user_settings(user_id)
        settings.max_amount = amount
        
        await state.clear()
        await message.answer(
            f"✅ Максимальная сумма: {amount} RUB",
            reply_markup=get_settings_keyboard()
        )
    except ValueError:
        await message.answer("❌ Некорректная сумма. Введите число больше 0")

@dp.callback_query(F.data == "set_exact_amount")
async def callback_set_exact_amount(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "🎯 Введите точную сумму в RUB\n"
        "Пример: 28000\n\n"
        "Будут показаны только объявления с этой суммой\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(FilterStates.waiting_for_exact_amount)

@dp.message(FilterStates.waiting_for_exact_amount)
async def process_exact_amount(message: Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return
    
    try:
        amount = float(message.text.replace(" ", ""))
        if amount <= 0:
            raise ValueError()
        
        user_id = message.from_user.id
        settings = get_user_settings(user_id)
        settings.exact_amount = amount
        
        await state.clear()
        await message.answer(
            f"✅ Точная сумма: {amount} RUB",
            reply_markup=get_settings_keyboard()
        )
    except ValueError:
        await message.answer("❌ Некорректная сумма. Введите число больше 0")

@dp.callback_query(F.data == "set_spread")
async def callback_set_spread(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "📊 Введите минимальный спред в %\n"
        "Пример: 1.5\n\n"
        "Будут показаны только связки с большим спредом\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(FilterStates.waiting_for_spread)

@dp.message(FilterStates.waiting_for_spread)
async def process_spread(message: Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return
    
    try:
        spread = float(message.text.replace("%", "").replace(",", "."))
        if spread <= 0:
            raise ValueError()
        
        user_id = message.from_user.id
        settings = get_user_settings(user_id)
        settings.min_spread = spread
        
        await state.clear()
        await message.answer(
            f"✅ Минимальный спред: {spread}%",
            reply_markup=get_settings_keyboard()
        )
    except ValueError:
        await message.answer("❌ Некорректное значение. Введите число больше 0")

@dp.callback_query(F.data == "set_payment")
async def callback_set_payment(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "💳 Введите платежные системы через запятую\n"
        "Пример: Т-Банк, Сбер, Альфа\n\n"
        "Будут показаны только объявления с этими платежками\n"
        "Для отмены отправьте /cancel"
    )
    await state.set_state(FilterStates.waiting_for_payment)

@dp.message(FilterStates.waiting_for_payment)
async def process_payment(message: Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return
    
    user_id = message.from_user.id
    settings = get_user_settings(user_id)
    
    payments = [p.strip() for p in message.text.split(",") if p.strip()]
    settings.payment_methods = payments
    
    await state.clear()
    await message.answer(
        f"✅ Платежные системы: {', '.join(payments) if payments else 'Все'}",
        reply_markup=get_settings_keyboard()
    )

@dp.callback_query(F.data == "reset_settings")
async def callback_reset_settings(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    settings = get_user_settings(user_id)
    
    settings.blacklist_words = []
    settings.whitelist_words = []
    settings.min_amount = None
    settings.max_amount = None
    settings.exact_amount = None
    settings.min_spread = 1.0
    settings.payment_methods = []
    
    await callback.message.answer(
        "🔄 Все настройки сброшены!",
        reply_markup=get_settings_keyboard()
    )

@dp.callback_query(F.data == "toggle_active")
async def callback_toggle_active(callback: CallbackQuery):
    user_id = callback.from_user.id
    settings = get_user_settings(user_id)
    settings.is_active = not settings.is_active
    
    status = "🟢 активирован" if settings.is_active else "🔴 приостановлен"
    await callback.answer(f"Бот {status}")
    await callback.message.answer(
        f"✅ Бот {status}",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "stats")
async def callback_stats(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    settings = get_user_settings(user_id)
    
    text = (
        "📈 <b>СТАТИСТИКА БОТА</b>\n\n"
        f"🟢 Статус: {'Активен' if settings.is_active else 'Приостановлен'}\n"
        f"⏰ Последний сигнал: {settings.last_signal_time.strftime('%H:%M:%S') if settings.last_signal_time else 'Нет'}\n"
        f"📊 Мин. спред: {settings.min_spread}%\n"
        f"🚫 Слов в черном списке: {len(settings.blacklist_words)}\n"
        f"✅ Слов в белом списке: {len(settings.whitelist_words)}\n"
        f"💰 Ограничение суммы: {'Да' if settings.min_amount or settings.max_amount else 'Нет'}\n"
        f"💳 Платежных систем: {len(settings.payment_methods)}\n"
    )
    
    await callback.message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "refresh")
async def callback_refresh(callback: CallbackQuery):
    await callback.answer("🔄 Обновление...")
    await callback.message.answer("✅ Данные обновлены", reply_markup=get_main_keyboard())

# ============== ФОНОВЫЙ МОНИТОРИНГ ==============
async def background_monitor():
    """
    Фоновый процесс для автоматического поиска сигналов
    """
    logger.info("Запуск фонового мониторинга...")
    
    while True:
        try:
            for user_id, settings in user_settings.items():
                if not settings.is_active:
                    continue
                
                # Проверяем кулдаун
                if settings.last_signal_time:
                    delta = (datetime.now() - settings.last_signal_time).total_seconds()
                    if delta < settings.cooldown_seconds:
                        continue
                
                # Ищем связку
                signal = await BybitP2PAPI.find_arbitrage(user_id, settings)
                
                if signal:
                    # Отправляем сигнал
                    try:
                        await bot.send_message(
                            user_id,
                            format_signal(signal),
                            parse_mode="HTML"
                        )
                        settings.last_signal_time = datetime.now()
                        logger.info(f"Signal sent to user {user_id}")
                    except Exception as e:
                        logger.error(f"Send error to {user_id}: {e}")
                
                # Небольшая задержка между пользователями
                await asyncio.sleep(2)
                
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        
        # Ждем перед следующим циклом
        await asyncio.sleep(30)  # Проверка каждые 30 секунд

# ============== ЗАПУСК БОТА ==============
async def main():
    """Главная функция запуска"""
    logger.info("Запуск бота...")
    
    # Запускаем фоновый мониторинг
    asyncio.create_task(background_monitor())
    
    # Запускаем поллинг
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
