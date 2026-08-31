import asyncio
import os
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import re

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, BotCommand
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не найден в переменных окружения")

if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    logger.warning("⚠️ API ключи Bybit не найдены! Бот будет работать в ограниченном режиме.")

# Инициализация Gemini (если есть ключ)
gemini_client = None
gemini_enabled_for_user: Dict[int, bool] = {}  # Включен ли Gemini для каждого пользователя

if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("✅ Gemini клиент инициализирован (модель: gemini-2.0-flash-lite)")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Gemini: {e}")
        gemini_client = None
else:
    logger.warning("⚠️ API ключ Gemini не найден! Функция анализа 3-их лиц будет недоступна.")

# Хранилище для фильтров пользователей
user_filters: Dict[int, Dict] = {}
user_subscriptions: Dict[int, bool] = {}

# Хранилище для отправленных сигналов (с временем)
sent_signals: Dict[int, Dict[str, datetime]] = {}

# Хранилище для настроек задержки между сигналами (по умолчанию 4 секунды)
user_signal_delay: Dict[int, int] = {}

# Кэш для результатов Gemini (чтобы не анализировать повторно)
gemini_cache: Dict[str, Dict[str, any]] = {}

# Вспомогательная функция для парсинга аргументов с поддержкой кавычек
def parse_args_with_quotes(text: str) -> List[str]:
    """
    Парсит строку аргументов, поддерживая кавычки для фраз с пробелами.
    Поддерживает как одинарные ('), так и двойные (") кавычки.
    """
    args = []
    current_arg = ""
    in_quotes = False
    quote_char = None
    
    i = 0
    while i < len(text):
        char = text[i]
        
        # Проверяем начало или конец кавычек
        if char in ('"', "'") and (i == 0 or text[i-1] != '\\'):
            if not in_quotes:
                # Начало кавычек
                in_quotes = True
                quote_char = char
            elif quote_char == char:
                # Конец кавычек
                in_quotes = False
                quote_char = None
            i += 1
            continue
        
        if not in_quotes and char == ' ':
            # Пробел вне кавычек - разделитель аргументов
            if current_arg:
                args.append(current_arg)
                current_arg = ""
            i += 1
            continue
        
        current_arg += char
        i += 1
    
    # Добавляем последний аргумент
    if current_arg:
        args.append(current_arg)
    
    return args

# Функция для проверки наличия слова в тексте с учетом границ слов
def check_word_in_text(word: str, text: str) -> bool:
    """
    Проверяет наличие слова в тексте с учетом границ слов.
    Использует регулярные выражения для точного поиска.
    """
    # Экранируем специальные символы в слове
    escaped_word = re.escape(word)
    # Создаем паттерн для поиска слова как отдельного слова или части слова
    # Используем границы слов \b, но также проверяем наличие слова в составе других слов
    # если слово короткое (до 3 символов), ищем как отдельное слово
    if len(word) <= 3:
        pattern = rf'\b{escaped_word}\b'
    else:
        # Для длинных слов ищем как часть слова или отдельное слово
        pattern = rf'{escaped_word}'
    
    return bool(re.search(pattern, text, re.IGNORECASE))

# Функция для безопасной отправки сообщений
async def safe_send_message(message: Message, text: str):
    """Отправка сообщения с безопасной обработкой HTML"""
    try:
        await message.answer(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Ошибка HTML-парсинга, отправляем обычный текст: {e}")
        await message.answer(text.replace('<', '[').replace('>', ']'))

# Функция анализа remark через Gemini (улучшенный промпт)
async def analyze_with_gemini(remark: str, merchant_name: str, item_id: str) -> Dict[str, any]:
    """
    Анализирует remark через Gemini для определения готовности к платежам от 3-их лиц.
    Использует кэш для повторных запросов.
    """
    if not gemini_client:
        return {"third_party_ready": True}
    
    # Кэш
    cache_key = f"{item_id}_{merchant_name}"
    if cache_key in gemini_cache:
        logger.info(f"♻️ Использован кэш Gemini для {merchant_name}")
        return gemini_cache[cache_key]
    
    if not remark or not remark.strip():
        result = {"third_party_ready": True}
        gemini_cache[cache_key] = result
        return result
    
    try:
        # Улучшенный промпт с примерами
        prompt = f"""
Ты анализируешь текст объявления (remark) от мерчанта {merchant_name} на P2P платформе Bybit.
Текст объявления: "{remark}"

Твоя задача: определить, готов ли этот мерчант принимать платежи от ТРЕТЬИХ ЛИЦ.
Третьи лица - это когда платеж за USDT совершает не сам покупатель, а другое лицо (друг, родственник, коллега).

ПРИМЕРЫ, КОГДА НЕ ГОТОВ (third_party_ready = false):
- "Платежи только с карты, оформленной на ваше имя"
- "Только переводы от своего имени, без третьих лиц"
- "Не принимаю платежи от третьих лиц"
- "Только свои карты, без перевыпуска"
- "Запрещены переводы от друзей и родственников"

ПРИМЕРЫ, КОГДА ГОТОВ (third_party_ready = true):
- "Принимаю платежи от третьих лиц"
- "Можно переводить с карт друзей и родственников"
- "Платежи от третьих лиц разрешены"
- "Допускаются переводы от любых лиц"
- В тексте НЕТ упоминаний о запрете третьих лиц

ПРАВИЛО: Если в тексте НЕТ явного запрета на платежи от третьих лиц, считай, что мерчант ГОТОВ (true).

Верни ответ ТОЛЬКО в формате JSON:
{{"third_party_ready": true/false}}

Никаких других слов, только JSON!
"""
        
        # Логируем запрос
        logger.info(f"📤 Запрос к Gemini для {merchant_name}")
        logger.info(f"📝 Текст для анализа: {remark[:200]}...")
        
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: gemini_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    top_p=0.8,
                    max_output_tokens=50,  # Минимум токенов
                )
            )
        )
        
        result_text = response.text.strip()
        logger.info(f"📥 Ответ Gemini для {merchant_name}: {result_text}")
        
        # Парсим JSON
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            ready = data.get("third_party_ready")
            if ready is None:
                ready = data.get("ready")
            if ready is None:
                ready = True  # По умолчанию готов
            
            result = {"third_party_ready": bool(ready)}
            gemini_cache[cache_key] = result
            logger.info(f"✅ Gemini анализ для {merchant_name}: third_party_ready={result['third_party_ready']}")
            return result
        else:
            # Если JSON не найден, определяем по тексту
            lower_text = result_text.lower()
            if "false" in lower_text or "не готов" in lower_text or "not ready" in lower_text:
                result = {"third_party_ready": False}
            else:
                result = {"third_party_ready": True}
            
            gemini_cache[cache_key] = result
            logger.info(f"✅ Gemini анализ для {merchant_name} (по тексту): third_party_ready={result['third_party_ready']}")
            return result
        
    except Exception as e:
        logger.error(f"❌ Ошибка Gemini для {merchant_name}: {e}")
        result = {"third_party_ready": True}  # В случае ошибки считаем готовым
        gemini_cache[cache_key] = result
        return result

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
    item_id: str
    user_id: str
    user_mask_id: str
    remark: str = ""
    third_party_ready: bool = True
    token: str = "USDT"
    fiat: str = "RUB"

@dataclass
class ArbitrageSignal:
    """Класс для хранения сигнала арбитража"""
    seller: P2POffer
    buyer: P2POffer
    spread: float
    profit: float
    profit_rub: float
    timestamp: datetime
    signal_id: str

class BybitP2PClient:
    """Клиент для работы с P2P API Bybit"""
    
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bybit.com"
    
    def _post_signed(self, path: str, payload: dict) -> dict:
        """Выполняет подписанный POST запрос к Bybit API"""
        recv_window_ms = 5000
        timeout_seconds = 15
        
        timestamp = str(int(time.time() * 1000))
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        
        signature_payload = f"{timestamp}{self.api_key}{recv_window_ms}{body}"
        
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        
        request = Request(
            f"{self.base_url}{path}",
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": str(recv_window_ms),
                "X-BAPI-SIGN": signature,
            },
            method="POST",
        )
        
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
                return json.loads(response_body)
        except HTTPError as error:
            try:
                response_body = error.read().decode("utf-8")
                logger.error(f"HTTP ошибка {error.code}: {response_body[:200]}")
            except:
                logger.error(f"HTTP ошибка {error.code}")
            return {}
        except (URLError, TimeoutError, OSError) as error:
            logger.error(f"Ошибка соединения: {error}")
            return {}
        except json.JSONDecodeError as error:
            logger.error(f"Ошибка парсинга JSON: {error}")
            return {}
    
    def get_online_ads(self, side: str, page: int = 1, size: int = 50) -> List[P2POffer]:
        """Получает P2P объявления с Bybit"""
        side_map = {"BUY": 0, "SELL": 1}
        bybit_side = side_map.get(side.upper())
        
        if bybit_side is None:
            raise ValueError("side должен быть 'BUY' или 'SELL'")
        
        result = self._post_signed(
            "/v5/p2p/item/online",
            {
                "tokenId": "USDT",
                "currencyId": "RUB",
                "side": str(bybit_side),
                "page": str(page),
                "size": str(size),
            },
        )
        
        items = result.get("result", {}).get("items", [])
        
        if not items:
            return []
        
        offers = []
        for item in items:
            try:
                price = float(item.get("price", 0))
                min_amount = float(item.get("minAmount", 0))
                max_amount = float(item.get("maxAmount", 0))
                amount = (min_amount + max_amount) / 2 if min_amount > 0 and max_amount > 0 else min_amount
                
                payment_methods = []
                for method in item.get("paymentMethods", []):
                    name = method.get("name", "")
                    if name:
                        payment_methods.append(name)
                
                item_id = str(item.get("itemId", ""))
                user_id = str(item.get("uid", ""))
                user_mask_id = str(item.get("userMaskId", ""))
                
                # Получаем remark из объявления
                remark = item.get("remark", "")
                
                offer = P2POffer(
                    side=side,
                    price=price,
                    amount=amount,
                    min_amount=min_amount,
                    max_amount=max_amount,
                    payment_methods=payment_methods,
                    description=item.get("description", ""),
                    link="",
                    merchant_name=item.get("nickName", "Аноним"),
                    item_id=item_id,
                    user_id=user_id,
                    user_mask_id=user_mask_id,
                    remark=remark,
                    third_party_ready=True
                )
                offers.append(offer)
            except (ValueError, KeyError) as e:
                logger.warning(f"Ошибка парсинга объявления: {e}")
                continue
        
        logger.info(f"Получено {len(offers)} объявлений для {side}")
        return offers

class P2PArbitrageBot:
    """Основной класс бота для P2P арбитража"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.is_running = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.bybit_client = None
        self._stop_requested = False  # Флаг для экстренной остановки
        
        if BYBIT_API_KEY and BYBIT_API_SECRET:
            self.bybit_client = BybitP2PClient(BYBIT_API_KEY, BYBIT_API_SECRET)
            logger.info("✅ Bybit клиент инициализирован")
        else:
            logger.warning("⚠️ Bybit клиент не инициализирован (нет API ключей)")
        
    async def start(self):
        """Запуск бота"""
        self.is_running = True
        self._stop_requested = False
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Бот успешно запущен")
        
    async def stop(self):
        """Остановка бота"""
        self.is_running = False
        self._stop_requested = True
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Бот остановлен")
    
    async def stop_for_user(self, user_id: int):
        """Остановка мониторинга для конкретного пользователя"""
        user_subscriptions[user_id] = False
        # Очищаем кэш отправленных сигналов для пользователя
        if user_id in sent_signals:
            sent_signals[user_id].clear()
        logger.info(f"Мониторинг остановлен для пользователя {user_id}")
    
    def _fetch_p2p_offers_sync(self, side: str) -> List[P2POffer]:
        """Получение P2P-объявлений с Bybit"""
        if not self.bybit_client:
            logger.warning("Bybit клиент не доступен")
            return []
        
        try:
            if side.upper() == "BUY":
                return self.bybit_client.get_online_ads("BUY", page=1, size=50)
            else:
                return self.bybit_client.get_online_ads("SELL", page=1, size=50)
        except Exception as e:
            logger.error(f"Ошибка при получении объявлений: {e}")
            return []
    
    def _check_offer_conditions(self, offer: P2POffer, filters: Dict) -> Tuple[bool, str]:
        """Проверка условий мейкера для объявления"""
        if not filters:
            return True, "OK"
        
        # Проверка черного списка (только по нику мерчанта)
        blacklist = filters.get("blacklist", [])
        if blacklist:
            # Проверяем только имя мерчанта
            merchant_name_lower = offer.merchant_name.lower()
            
            # Логируем для отладки
            logger.debug(f"Проверка черного списка для мерчанта {offer.merchant_name}: {blacklist}")
            
            for word in blacklist:
                # Используем улучшенную проверку с регулярными выражениями
                if check_word_in_text(word, merchant_name_lower):
                    logger.info(f"Найдено запрещенное слово '{word}' в нике мерчанта {offer.merchant_name}")
                    return False, f"Найдено запрещенное слово '{word}' в нике мерчанта {offer.merchant_name}"
        
        # Проверка суммы
        if filters.get("exact_amount"):
            if not (offer.min_amount <= filters["exact_amount"] <= offer.max_amount):
                return False, f"Сумма {filters['exact_amount']:.0f}₽ не входит в лимиты {offer.min_amount:.0f}-{offer.max_amount:.0f}₽"
        
        if filters.get("min_amount"):
            if offer.max_amount < filters["min_amount"]:
                return False, f"Макс. сумма {offer.max_amount:.0f}₽ < {filters['min_amount']:.0f}₽"
        
        if filters.get("max_amount"):
            if offer.min_amount > filters["max_amount"]:
                return False, f"Мин. сумма {offer.min_amount:.0f}₽ > {filters['max_amount']:.0f}₽"
        
        return True, "OK"
    
    def _generate_profile_url(self, user_mask_id: str) -> str:
        """Генерирует ссылку на профиль пользователя Bybit используя userMaskId"""
        if not user_mask_id or user_mask_id == "0" or user_mask_id == "":
            return "Ссылка недоступна"
        return f"https://www.bybit.com/ru-RU/p2p/profile/{user_mask_id}/USDT/RUB/item"
    
    def _generate_order_url(self, item_id: str) -> str:
        """Генерирует ссылку на ордер Bybit"""
        if not item_id or item_id == "0" or item_id == "":
            return "Ссылка недоступна"
        return f"https://www.bybit.com/ru-RU/p2p/order/{item_id}"
    
    async def _analyze_offers_with_gemini(self, sellers: List[P2POffer], buyers: List[P2POffer], user_id: int) -> Tuple[List[P2POffer], List[P2POffer]]:
        """Анализирует объявления через Gemini для пользователя"""
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        
        if not gemini_enabled or not gemini_client:
            # Если Gemini выключен - просто помечаем все как готовые
            for seller in sellers:
                seller.third_party_ready = True
            for buyer in buyers:
                buyer.third_party_ready = True
            return sellers, buyers
        
        # Анализируем объявления с remark (только топ-10 для экономии)
        sellers_with_remark = [s for s in sellers if s.remark and s.remark.strip()][:10]
        buyers_with_remark = [b for b in buyers if b.remark and b.remark.strip()][:10]
        
        # Анализируем продавцов
        for seller in sellers_with_remark:
            cache_key = f"{seller.item_id}_{seller.merchant_name}"
            if cache_key not in gemini_cache:
                await analyze_with_gemini(seller.remark, seller.merchant_name, seller.item_id)
        
        # Анализируем покупателей
        for buyer in buyers_with_remark:
            cache_key = f"{buyer.item_id}_{buyer.merchant_name}"
            if cache_key not in gemini_cache:
                await analyze_with_gemini(buyer.remark, buyer.merchant_name, buyer.item_id)
        
        # Применяем результаты ко всем объявлениям
        for seller in sellers:
            cache_key = f"{seller.item_id}_{seller.merchant_name}"
            if cache_key in gemini_cache:
                seller.third_party_ready = gemini_cache[cache_key].get("third_party_ready", True)
            else:
                # Если не анализировали - считаем готовым
                seller.third_party_ready = True
        
        for buyer in buyers:
            cache_key = f"{buyer.item_id}_{buyer.merchant_name}"
            if cache_key in gemini_cache:
                buyer.third_party_ready = gemini_cache[cache_key].get("third_party_ready", True)
            else:
                buyer.third_party_ready = True
        
        return sellers, buyers
    
    def _find_all_arbitrage_signals(self, sellers: List[P2POffer], buyers: List[P2POffer],
                                     user_filters: Dict, user_id: int) -> List[ArbitrageSignal]:
        """Находит ВСЕ арбитражные связки с учетом Gemini"""
        if not sellers or not buyers:
            return []
        
        # Проверяем, включен ли Gemini для пользователя
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        
        # Фильтруем продавцов и покупателей по условиям
        filtered_sellers = []
        for seller in sellers:
            passes, _ = self._check_offer_conditions(seller, user_filters)
            if passes:
                # Если Gemini включен - проверяем seller
                if gemini_enabled and gemini_client:
                    # Если seller не готов к платежам от 3-их лиц - пропускаем
                    if not seller.third_party_ready:
                        logger.debug(f"❌ Seller {seller.merchant_name} не готов к 3-им лицам - пропускаем")
                        continue
                
                filtered_sellers.append(seller)
        
        filtered_buyers = []
        for buyer in buyers:
            passes, _ = self._check_offer_conditions(buyer, user_filters)
            if passes:
                # Если Gemini включен - проверяем buyer
                if gemini_enabled and gemini_client:
                    # Если buyer не готов к платежам от 3-их лиц - пропускаем
                    if not buyer.third_party_ready:
                        logger.debug(f"❌ Buyer {buyer.merchant_name} не готов к 3-им лицам - пропускаем")
                        continue
                
                filtered_buyers.append(buyer)
        
        if not filtered_sellers or not filtered_buyers:
            logger.info(f"⚠️ Нет подходящих объявлений после фильтрации")
            return []
        
        # Сортируем продавцов по возрастанию цены (самые дешевые сверху)
        filtered_sellers.sort(key=lambda x: x.price)
        # Сортируем покупателей по убыванию цены (самые дорогие сверху)
        filtered_buyers.sort(key=lambda x: x.price, reverse=True)
        
        signals = []
        min_spread = user_filters.get("min_spread", 0.5)
        
        # Перебираем всех продавцов и покупателей
        for seller in filtered_sellers[:20]:
            for buyer in filtered_buyers[:20]:
                if seller.price >= buyer.price:
                    continue
                
                spread = ((buyer.price / seller.price) - 1) * 100
                
                if spread < min_spread:
                    continue
                
                # Проверяем пересечение лимитов
                max_trade_amount = min(seller.max_amount, buyer.max_amount)
                min_trade_amount = max(seller.min_amount, buyer.min_amount)
                
                if max_trade_amount < min_trade_amount:
                    continue
                
                if user_filters.get("min_amount"):
                    if max_trade_amount < user_filters["min_amount"]:
                        continue
                
                if user_filters.get("max_amount"):
                    if min_trade_amount > user_filters["max_amount"]:
                        continue
                
                # Расчет прибыли
                trade_amount = max_trade_amount
                usdt_amount = trade_amount / seller.price if seller.price > 0 else 0
                profit_per_usdt = buyer.price - seller.price
                total_profit_rub = usdt_amount * profit_per_usdt
                
                signal_id = f"{seller.item_id}_{seller.price}_{seller.min_amount}_{seller.max_amount}_{buyer.item_id}_{buyer.price}_{buyer.min_amount}_{buyer.max_amount}"
                
                signal = ArbitrageSignal(
                    seller=seller,
                    buyer=buyer,
                    spread=spread,
                    profit=profit_per_usdt,
                    profit_rub=total_profit_rub,
                    timestamp=datetime.now(),
                    signal_id=signal_id
                )
                signals.append(signal)
        
        signals.sort(key=lambda x: x.profit_rub, reverse=True)
        return signals
    
    def _clean_old_signals(self, user_id: int):
        """Очищает старые сигналы (старше 10 минут)"""
        if user_id not in sent_signals:
            sent_signals[user_id] = {}
            return
        
        now = datetime.now()
        old_signals = []
        for signal_id, sent_time in sent_signals[user_id].items():
            if now - sent_time > timedelta(minutes=10):
                old_signals.append(signal_id)
        
        for signal_id in old_signals:
            del sent_signals[user_id][signal_id]
        
        if old_signals:
            logger.debug(f"Очищено {len(old_signals)} старых сигналов для пользователя {user_id}")
    
    async def _monitor_loop(self):
        """Основной цикл мониторинга"""
        while self.is_running and not self._stop_requested:
            try:
                if not self.bybit_client:
                    logger.warning("Пропуск цикла: Bybit клиент не инициализирован")
                    await asyncio.sleep(30)
                    continue
                
                # Получаем список активных пользователей ДО начала цикла
                active_users = []
                for user_id, is_active in user_subscriptions.items():
                    if is_active and not self._stop_requested:
                        active_users.append(user_id)
                
                if not active_users:
                    await asyncio.sleep(15)
                    continue
                
                for user_id in active_users:
                    # Проверяем флаг остановки перед каждым пользователем
                    if not self.is_running or self._stop_requested:
                        logger.info(f"Остановка мониторинга по запросу пользователя {user_id}")
                        return
                    
                    # Проверяем, активен ли пользователь
                    if not user_subscriptions.get(user_id, False):
                        continue
                    
                    filters = user_filters.get(user_id, {})
                    if not filters:
                        continue
                    
                    self._clean_old_signals(user_id)
                    
                    sellers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "SELL"
                    )
                    
                    # Проверяем флаг остановки после получения sellers
                    if not self.is_running or self._stop_requested:
                        logger.info(f"Остановка мониторинга после получения SELL объявлений")
                        return
                    
                    buyers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "BUY"
                    )
                    
                    # Проверяем флаг остановки после получения buyers
                    if not self.is_running or self._stop_requested:
                        logger.info(f"Остановка мониторинга после получения BUY объявлений")
                        return
                    
                    if not sellers or not buyers:
                        continue
                    
                    # Анализируем объявления через Gemini (если включено)
                    sellers, buyers = await self._analyze_offers_with_gemini(sellers, buyers, user_id)
                    
                    signals = self._find_all_arbitrage_signals(sellers, buyers, filters, user_id)
                    
                    if signals:
                        logger.info(f"Найдено {len(signals)} сигналов для пользователя {user_id}")
                        
                        if user_id not in sent_signals:
                            sent_signals[user_id] = {}
                        
                        # Получаем задержку для пользователя (по умолчанию 4 секунды)
                        delay_seconds = user_signal_delay.get(user_id, 4)
                        
                        sent_count = 0
                        skipped_count = 0
                        
                        for signal in signals[:30]:
                            # Проверяем флаг остановки перед отправкой каждого сигнала
                            if not self.is_running or self._stop_requested:
                                logger.info(f"Остановка мониторинга во время отправки сигналов")
                                return
                            
                            # Проверяем, активен ли пользователь
                            if not user_subscriptions.get(user_id, False):
                                logger.info(f"Пользователь {user_id} отключил мониторинг, пропускаем сигналы")
                                break
                            
                            if signal.signal_id not in sent_signals[user_id]:
                                await self._send_signal(user_id, signal)
                                sent_signals[user_id][signal.signal_id] = datetime.now()
                                sent_count += 1
                                logger.info(f"Отправлен сигнал #{sent_count}: SELL={signal.seller.merchant_name} {signal.seller.price:.2f}₽, BUY={signal.buyer.merchant_name} {signal.buyer.price:.2f}₽, прибыль={signal.profit_rub:.2f}₽")
                                
                                # Используем задержку между сигналами
                                if sent_count < len(signals[:30]):
                                    await asyncio.sleep(delay_seconds)
                            else:
                                skipped_count += 1
                        
                        if sent_count > 0:
                            logger.info(f"Отправлено {sent_count} новых сигналов пользователю {user_id} (пропущено {skipped_count} дубликатов, задержка {delay_seconds}с)")
                        else:
                            logger.info(f"Новых сигналов нет для пользователя {user_id} (все {len(signals)} уже отправлены)")
                    else:
                        logger.info(f"ℹ️ Сигналов не найдено для пользователя {user_id}")
                
                await asyncio.sleep(15)
                
            except asyncio.CancelledError:
                logger.info("Цикл мониторинга отменен")
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(30)
    
    async def _send_signal(self, user_id: int, signal: ArbitrageSignal):
        """Отправка сигнала пользователю"""
        def format_number(num):
            if num >= 1000:
                return f"{num:,.0f}".replace(",", " ")
            return f"{num:.0f}"
        
        trade_amount = min(signal.seller.max_amount, signal.buyer.max_amount)
        usdt_amount = trade_amount / signal.seller.price if signal.seller.price > 0 else 0
        
        # Генерируем ссылки на профили используя user_mask_id
        seller_profile_url = self._generate_profile_url(signal.seller.user_mask_id)
        buyer_profile_url = self._generate_profile_url(signal.buyer.user_mask_id)
        
        # Логируем remark и результат Gemini для обоих объявлений
        logger.info(f"📝 SIGNAL REMARKS for user {user_id}:")
        logger.info(f"   SELLER: {signal.seller.merchant_name}")
        logger.info(f"   REMARK: {signal.seller.remark}")
        logger.info(f"   Gemini ответ: third_party_ready={signal.seller.third_party_ready}")
        logger.info(f"   BUYER: {signal.buyer.merchant_name}")
        logger.info(f"   REMARK: {signal.buyer.remark}")
        logger.info(f"   Gemini ответ: third_party_ready={signal.buyer.third_party_ready}")
        
        # Определяем статус third_party (новая формулировка)
        seller_third_party = "✅ Готов к платежам от 3-их лиц" if signal.seller.third_party_ready else "❌ Не готов к платежам от 3-их лиц"
        buyer_third_party = "✅ Готов к платежам от 3-их лиц" if signal.buyer.third_party_ready else "❌ Не готов к платежам от 3-их лиц"
        
        # Формируем сообщение (без комментариев от Gemini)
        message = f"""🔥 АРБИТРАЖНЫЙ СИГНАЛ 🔥

🟢 ПРОДАВЕЦ (SELLER)
• Курс: {signal.seller.price:.2f}₽
• Лимиты: {format_number(signal.seller.min_amount)} - {format_number(signal.seller.max_amount)}₽
• Мерчант: {signal.seller.merchant_name}
• {seller_third_party}
• Ссылка на профиль: {seller_profile_url}

🔴 ПОКУПАТЕЛЬ (BUYER)
• Курс: {signal.buyer.price:.2f}₽
• Лимиты: {format_number(signal.buyer.min_amount)} - {format_number(signal.buyer.max_amount)}₽
• Мерчант: {signal.buyer.merchant_name}
• {buyer_third_party}
• Ссылка на профиль: {buyer_profile_url}

📊 РАСЧЕТ ПРИБЫЛИ
• Спред: {signal.spread:.2f}%
• Прибыль с 1 USDT: {signal.profit:.2f}₽
• Сумма сделки: {format_number(trade_amount)}₽
• USDT: {usdt_amount:.2f}
• Потенциальная прибыль: {signal.profit_rub:,.2f}₽"""
        
        try:
            await self.bot.send_message(
                user_id,
                message,
                parse_mode=None,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сигнала: {e}")
    
    async def get_filter_settings(self, user_id: int) -> str:
        """Получение текущих настроек фильтров"""
        filters = user_filters.get(user_id, {})
        delay = user_signal_delay.get(user_id, 4)
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        cache_size = len(gemini_cache)
        
        if not filters:
            return f"🔧 Фильтры не настроены. Используйте /help для настройки.\n\n⏱ Задержка между сигналами: {delay}с\n🤖 Gemini: {'✅ Включен' if gemini_enabled else '❌ Выключен'}\n💾 Кэш: {cache_size} записей"
        
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
            settings.append(f"• Черный список (ники мерчантов): {', '.join(filters['blacklist'])}")
        
        settings.append("")
        settings.append(f"⏱ <b>Задержка между сигналами:</b> {delay}с")
        settings.append(f"🤖 <b>Gemini (3-и лица):</b> {'✅ Включен' if gemini_enabled else '❌ Выключен'}")
        settings.append(f"💾 <b>Кэш Gemini:</b> {cache_size} записей")
        
        if len(settings) == 3:
            settings.append("⚠️ Фильтры настроены, но неактивны (запустите /start_monitoring)")
        
        return "\n".join(settings)


# Функция для установки команд бота
async def set_bot_commands(bot: Bot):
    """Устанавливает меню команд для бота"""
    commands = [
        BotCommand(command="settings", description="📋 Показать текущие настройки фильтров"),
        BotCommand(command="status", description="📊 Статус мониторинга"),
        BotCommand(command="start_monitoring", description="▶️ Запустить мониторинг арбитража"),
        BotCommand(command="stop_monitoring", description="⏹ Остановить мониторинг"),
        BotCommand(command="delay", description="⏱ Установить задержку между сигналами (сек)"),
        BotCommand(command="clear_filters", description="🧹 Очистить все фильтры"),
        BotCommand(command="gemini_on", description="🤖 Включить анализ 3-их лиц (Gemini)"),
        BotCommand(command="gemini_off", description="🤖 Выключить анализ 3-их лиц (Gemini)"),
        BotCommand(command="help", description="❓ Настройка фильтров"),
    ]
    await bot.set_my_commands(commands)
    logger.info("✅ Меню команд установлено")


# Инициализация бота
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
arbitrage_bot = P2PArbitrageBot(bot)

# --- Обработчики команд ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start"""
    welcome_text = """
🚀 Добро пожаловать в P2P Арбитраж Бот!

Я ищу арбитражные связки на Bybit P2P и присылаю тебе сигналы.

<b>Доступные команды:</b>
/help - Настройка фильтров
/settings - Текущие настройки фильтров
/status - Статус мониторинга
/start_monitoring - Запустить мониторинг
/stop_monitoring - Остановить мониторинг
/delay - Установить задержку между сигналами
/clear_filters - Очистить все фильтры
/gemini_on - Включить анализ 3-их лиц (проверка remark через AI)
/gemini_off - Выключить анализ 3-их лиц

<b>Как это работает:</b>
1. Настрой фильтры через /help
2. Запусти мониторинг /start_monitoring
3. Бот будет искать выгодные связки
4. При найденной связке получишь сигнал со ссылками на профили
5. Если включен Gemini - бот будет проверять готовность к платежам от 3-их лиц
    """
    await safe_send_message(message, welcome_text)
    
    if message.from_user.id not in user_filters:
        user_filters[message.from_user.id] = {}
        user_subscriptions[message.from_user.id] = False
        sent_signals[message.from_user.id] = {}
        user_signal_delay[message.from_user.id] = 4
        gemini_enabled_for_user[message.from_user.id] = False

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help"""
    help_text = """
📖 <b>Помощь по фильтрам</b>

<b>Что можно настраивать:</b>

1. <b>Сумма сделки</b>
   /set_exact 28000 - строго 28 000 ₽
   /set_min 25000 - минимум 25 000 ₽
   /set_max 30000 - максимум 30 000 ₽

2. <b>Черный список (исключаем по никам мерчантов)</b>
   /add_blacklist "Имя Мерчанта" - НЕ показывать объявления этого мерчанта
   /add_blacklist Мошенник - НЕ показывать объявления мерчантов с этим словом в нике
   /remove_blacklist "Имя Мерчанта" - убрать из черного списка
   
   <b>⚠️ ВАЖНО:</b> Черный список работает ТОЛЬКО с никами мерчантов!
   • Bybit API НЕ передает текстовые условия/описания объявлений
   • Черный список НЕ может фильтровать по описанию или условиям мейкера
   • Если хотите исключить мерчанта - добавьте его полный ник или часть ника

3. <b>Спред</b>
   /set_spread 0.5 - минимальный спред 0.5%

4. <b>Задержка между сигналами</b>
   /delay 5 - установить задержку 5 секунд между отправками сигналов
   /delay 2 - установить задержку 2 секунды (быстрее)
   /delay 10 - установить задержку 10 секунд (медленнее)
   
   <b>⚠️ ВАЖНО:</b> Если задержка слишком маленькая (1-2с), 
   вы можете получить много сообщений подряд. Рекомендуем 3-5 секунд.

5. <b>Управление</b>
   /start_monitoring - запуск поиска
   /stop_monitoring - остановка поиска
   /status - текущий статус
   /clear_filters - очистить все фильтры

6. <b>🤖 Gemini (анализ 3-их лиц)</b>
   /gemini_on - Включить анализ remark через AI
   /gemini_off - Выключить анализ remark через AI
   
   <b>Как работает:</b>
   • Gemini анализирует текст объявления (remark)
   • Определяет, готов ли мерчант принимать платежи от 3-их лиц
   • Если в тексте нет явного запрета - считается, что готов
   • В сигнале будет указан статус для продавца и покупателя
   • По умолчанию Gemini ВЫКЛЮЧЕН (нужно включить командой)

<b>Важно про кавычки!</b>
Если имя мерчанта состоит из нескольких слов, заключите его в кавычки:
/add_blacklist "ALL FOR ALL"
/add_blacklist "Иван Петров"

<b>Пример настройки:</b>
1. /set_min 500
2. /set_max 10000
3. /set_spread 0.5
4. /delay 5
5. /add_blacklist "Мошенник Иван"
6. /add_blacklist "ALL FOR ALL"
7. /gemini_on  # Включить анализ 3-их лиц
8. /start_monitoring

<b>Как работает черный список:</b>
• Проверяет только НИК мерчанта
• Регистр не важен
• Можно добавить как полное имя, так и часть
• Пример: /add_blacklist "Мошенник" - исключит всех мерчантов с этим словом в нике
• Пример: /add_blacklist "ALL FOR ALL" - исключит только этого конкретного мерчанта
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
    
    signals_count = len(sent_signals.get(user_id, {}))
    
    status_message = f"""
<b>Статус мониторинга:</b> {status_emoji} {status_text}
<b>Отправлено сигналов:</b> {signals_count}

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
            "Используйте /help для настройки и /settings для просмотра."
        )
        return
    
    # Очищаем старые сигналы при новом запуске
    sent_signals[user_id] = {}
    
    user_subscriptions[user_id] = True
    delay = user_signal_delay.get(user_id, 4)
    gemini_enabled = gemini_enabled_for_user.get(user_id, False)
    
    await safe_send_message(
        message,
        f"✅ Мониторинг запущен!\n"
        f"Бот будет присылать сигналы при нахождении выгодных связок.\n"
        f"⏱ Задержка между сигналами: {delay}с\n"
        f"🤖 Gemini: {'Включен (проверка 3-их лиц)' if gemini_enabled else 'Выключен'}\n"
        f"Для остановки используйте: /stop_monitoring"
    )

@dp.message(Command("stop_monitoring"))
async def cmd_stop_monitoring(message: Message):
    """Остановка мониторинга"""
    user_id = message.from_user.id
    
    # Немедленно останавливаем для этого пользователя
    user_subscriptions[user_id] = False
    
    # Очищаем кэш отправленных сигналов
    if user_id in sent_signals:
        sent_signals[user_id].clear()
    
    await safe_send_message(
        message, 
        "⏹ Мониторинг остановлен.\n"
        "Все активные задачи для вас отменены."
    )

@dp.message(Command("delay"))
async def cmd_delay(message: Message):
    """Установка задержки между сигналами"""
    try:
        args = parse_args_with_quotes(message.text)
        if len(args) != 2:
            await safe_send_message(
                message, 
                "❌ Использование: /delay <секунды>\n"
                "Пример: /delay 5\n\n"
                "Рекомендуемые значения:\n"
                "• 3-5 секунд - оптимально\n"
                "• 1-2 секунды - очень быстро (может быть много сообщений)\n"
                "• 8-10 секунд - медленно (меньше сообщений)"
            )
            return
        
        delay = float(args[1])
        if delay < 1:
            await safe_send_message(
                message, 
                "❌ Задержка должна быть не менее 1 секунды"
            )
            return
        
        if delay > 60:
            await safe_send_message(
                message, 
                "❌ Задержка не может превышать 60 секунд"
            )
            return
        
        user_id = message.from_user.id
        user_signal_delay[user_id] = delay
        
        await safe_send_message(
            message, 
            f"⏱ Задержка между сигналами установлена: {delay}с\n\n"
            f"Теперь бот будет отправлять сигналы с интервалом {delay} секунд.\n"
            f"Это поможет избежать спама при большом количестве сигналов."
        )
    except ValueError:
        await safe_send_message(
            message, 
            "❌ Введите корректное число секунд\n"
            "Пример: /delay 5"
        )

@dp.message(Command("clear_filters"))
async def cmd_clear_filters(message: Message):
    """Очистка всех фильтров"""
    user_id = message.from_user.id
    user_filters[user_id] = {}
    user_subscriptions[user_id] = False
    sent_signals[user_id] = {}
    # Задержку и Gemini НЕ сбрасываем
    await safe_send_message(message, "🧹 Все фильтры очищены. Мониторинг остановлен. Задержка и Gemini сохранены.")

@dp.message(Command("gemini_on"))
async def cmd_gemini_on(message: Message):
    """Включение Gemini"""
    user_id = message.from_user.id
    
    if not gemini_client:
        await safe_send_message(
            message,
            "❌ Gemini недоступен! Проверьте API ключ в настройках бота."
        )
        return
    
    gemini_enabled_for_user[user_id] = True
    # Очищаем кэш при включении
    global gemini_cache
    gemini_cache = {}
    
    await safe_send_message(
        message,
        "🤖 Gemini ВКЛЮЧЕН!\n\n"
        "Теперь бот будет анализировать текст объявлений (remark) через AI.\n"
        "Это позволит определить, готов ли мерчант принимать платежи от 3-их лиц.\n\n"
        "⚠️ Если мерчант НЕ готов к платежам от 3-их лиц - его объявление будет исключено из сигналов.\n"
        "Если в тексте нет упоминаний о 3-их лицах - считается, что мерчант готов.\n\n"
        "Для отключения используйте: /gemini_off"
    )

@dp.message(Command("gemini_off"))
async def cmd_gemini_off(message: Message):
    """Выключение Gemini"""
    user_id = message.from_user.id
    gemini_enabled_for_user[user_id] = False
    
    await safe_send_message(
        message,
        "🤖 Gemini ВЫКЛЮЧЕН!\n\n"
        "Теперь бот НЕ будет анализировать текст объявлений через AI.\n"
        "Все объявления будут показываться без фильтрации по готовности к платежам от 3-их лиц.\n\n"
        "Для включения используйте: /gemini_on"
    )

# --- Команды для настройки фильтров ---

@dp.message(Command("set_exact"))
async def cmd_set_exact(message: Message):
    try:
        args = parse_args_with_quotes(message.text)
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
        args = parse_args_with_quotes(message.text)
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
        args = parse_args_with_quotes(message.text)
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
        args = parse_args_with_quotes(message.text)
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
    args = parse_args_with_quotes(message.text)
    if len(args) != 2:
        await safe_send_message(
            message, 
            "❌ Использование: /add_blacklist <ник мерчанта>\n"
            "Пример: /add_blacklist Мошенник\n"
            "Пример с кавычками: /add_blacklist \"ALL FOR ALL\"\n\n"
            "⚠️ Черный список работает ТОЛЬКО с никами мерчантов!\n"
            "Bybit API не передает описания/условия, поэтому фильтр по ним невозможен."
        )
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters:
        user_filters[user_id] = {}
    
    if "blacklist" not in user_filters[user_id]:
        user_filters[user_id]["blacklist"] = []
    
    if word not in user_filters[user_id]["blacklist"]:
        user_filters[user_id]["blacklist"].append(word)
        await safe_send_message(
            message, 
            f"✅ Добавлено в ЧЕРНЫЙ список: {word}\n"
            f"Теперь бот НЕ будет показывать объявления мерчантов с этим словом в нике\n"
            f"(проверяется только ник мерчанта)\n\n"
            f"⚠️ Напоминание: черный список НЕ фильтрует описание/условия объявлений,\n"
            f"так как Bybit API не предоставляет эту информацию в публичных объявлениях."
        )
    else:
        await safe_send_message(message, f"⚠️ Слово '{word}' уже в черном списке")

@dp.message(Command("remove_blacklist"))
async def cmd_remove_blacklist(message: Message):
    args = parse_args_with_quotes(message.text)
    if len(args) != 2:
        await safe_send_message(message, "❌ Использование: /remove_blacklist <ник>\nПример: /remove_blacklist Мошенник")
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


async def on_startup():
    """Действия при запуске бота"""
    await set_bot_commands(bot)
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
