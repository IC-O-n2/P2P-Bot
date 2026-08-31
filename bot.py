import asyncio
import os
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
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
gemini_enabled_for_user: Dict[int, bool] = {}

if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("✅ Gemini клиент инициализирован (модель: gemini-3.5-flash-lite)")
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

# Кэш для результатов Gemini
gemini_cache: Dict[str, bool] = {}
gemini_cache_ttl: Dict[str, datetime] = {}
CACHE_TTL_MINUTES = 60

# Для контроля лимитов Gemini (15 запросов в минуту)
gemini_request_timestamps: List[datetime] = []
GEMINI_MAX_REQUESTS_PER_MINUTE = 12
gemini_request_queue: List[Dict] = []
gemini_processing = False
gemini_queue_lock = asyncio.Lock()

# Флаг, что бот работает в режиме "тишины" для Gemini
gemini_silent_mode = False
gemini_silent_until: Optional[datetime] = None

# Вспомогательная функция для парсинга аргументов с поддержкой кавычек
def parse_args_with_quotes(text: str) -> List[str]:
    args = []
    current_arg = ""
    in_quotes = False
    quote_char = None
    
    i = 0
    while i < len(text):
        char = text[i]
        
        if char in ('"', "'") and (i == 0 or text[i-1] != '\\'):
            if not in_quotes:
                in_quotes = True
                quote_char = char
            elif quote_char == char:
                in_quotes = False
                quote_char = None
            i += 1
            continue
        
        if not in_quotes and char == ' ':
            if current_arg:
                args.append(current_arg)
                current_arg = ""
            i += 1
            continue
        
        current_arg += char
        i += 1
    
    if current_arg:
        args.append(current_arg)
    
    return args

# Функция для проверки наличия слова в тексте с учетом границ слов
def check_word_in_text(word: str, text: str) -> bool:
    escaped_word = re.escape(word)
    if len(word) <= 3:
        pattern = rf'\b{escaped_word}\b'
    else:
        pattern = rf'{escaped_word}'
    return bool(re.search(pattern, text, re.IGNORECASE))

# Функция для безопасной отправки сообщений
async def safe_send_message(message: Message, text: str):
    try:
        await message.answer(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Ошибка HTML-парсинга, отправляем обычный текст: {e}")
        await message.answer(text.replace('<', '[').replace('>', ']'))

# Функция для проверки лимитов Gemini
async def wait_for_gemini_quota():
    global gemini_silent_mode, gemini_silent_until
    
    now = datetime.now()
    
    # Проверяем режим тишины
    if gemini_silent_mode and gemini_silent_until:
        if now < gemini_silent_until:
            wait_seconds = (gemini_silent_until - now).seconds + 1
            logger.warning(f"⏳ Режим тишины Gemini, ждем {wait_seconds}с")
            await asyncio.sleep(wait_seconds)
            gemini_silent_mode = False
            gemini_silent_until = None
            return
    
    gemini_request_timestamps[:] = [ts for ts in gemini_request_timestamps if now - ts < timedelta(seconds=60)]
    
    if len(gemini_request_timestamps) >= GEMINI_MAX_REQUESTS_PER_MINUTE:
        oldest = min(gemini_request_timestamps)
        wait_seconds = 60 - (now - oldest).seconds + 2
        if wait_seconds > 0:
            logger.warning(f"⏳ Достигнут лимит Gemini ({GEMINI_MAX_REQUESTS_PER_MINUTE}/мин), ждем {wait_seconds}с")
            await asyncio.sleep(wait_seconds)

# Функция анализа одного объявления через Gemini с очередью
async def analyze_single_with_gemini(remark: str, merchant_name: str, item_id: str) -> Tuple[bool, bool]:
    """
    Анализирует одно объявление через Gemini.
    Возвращает: (third_party_ready, analyzed)
    analyzed = True если объявление было проанализировано
    """
    global gemini_silent_mode, gemini_silent_until
    
    if not gemini_client:
        return True, False
    
    cache_key = f"{item_id}_{merchant_name}"
    
    # Проверяем кэш
    if cache_key in gemini_cache:
        cache_time = gemini_cache_ttl.get(cache_key)
        if cache_time and datetime.now() - cache_time < timedelta(minutes=CACHE_TTL_MINUTES):
            logger.debug(f"♻️ Использован кэш Gemini для {merchant_name}: {gemini_cache[cache_key]}")
            return gemini_cache[cache_key], True
    
    if not remark or not remark.strip():
        gemini_cache[cache_key] = True
        gemini_cache_ttl[cache_key] = datetime.now()
        return True, True
    
    # Если режим тишины - пропускаем анализ
    if gemini_silent_mode:
        return True, False
    
    try:
        # Ждем освобождения квоты
        await wait_for_gemini_quota()
        
        prompt = f"""
Анализ объявления от {merchant_name}:
Текст: "{remark}"

Готов ли мерчант принимать платежи от ТРЕТЬИХ ЛИЦ?
(Третьи лица - платеж совершает не покупатель, а другое лицо)

Правила:
- Если есть "только от себя", "только свои карты", "не принимаю от третьих лиц", "ТОЛЬКО 1 ЛИЦА", "СТРОГО 1 Лица", "от первого лица", "только первые лица" -> НЕ ГОТОВ
- Если есть "принимаю от третьих лиц", "можно от друзей" -> ГОТОВ
- Если есть "ЛК на руках" - это НЕ значит готовность к 3-им лицам
- Если нет упоминаний о 3-их лицах -> ГОТОВ (по умолчанию)

Верни ТОЛЬКО: true (если готов) или false (если не готов)
"""
        
        logger.info(f"📤 Отправка запроса в Gemini для {merchant_name} (ID: {item_id})")
        
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: gemini_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    top_p=0.8,
                    max_output_tokens=50,
                )
            )
        )
        
        gemini_request_timestamps.append(datetime.now())
        
        result_text = response.text.strip().lower()
        logger.info(f"📥 Ответ Gemini для {merchant_name}: {result_text}")
        
        if "true" in result_text:
            result = True
        elif "false" in result_text:
            result = False
        else:
            result = True
        
        gemini_cache[cache_key] = result
        gemini_cache_ttl[cache_key] = datetime.now()
        
        logger.info(f"✅ Gemini анализ для {merchant_name}: готов={result}")
        logger.info(f"📝 REMARK: {remark[:200]}...")
        
        return result, True
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Ошибка Gemini для {merchant_name}: {error_msg[:100]}")
        
        # Если превышен лимит - включаем режим тишины
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            gemini_silent_mode = True
            gemini_silent_until = datetime.now() + timedelta(seconds=55)
            logger.warning(f"🔇 Включен режим тишины Gemini до {gemini_silent_until}")
        
        # При ошибке - не сохраняем в кэш, возвращаем not analyzed
        return True, False

@dataclass
class P2POffer:
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
    third_party_analyzed: bool = False  # Флаг, что объявление проанализировано
    token: str = "USDT"
    fiat: str = "RUB"

@dataclass
class ArbitrageSignal:
    seller: P2POffer
    buyer: P2POffer
    spread: float
    profit: float
    profit_rub: float
    timestamp: datetime
    signal_id: str

class BybitP2PClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bybit.com"
    
    def _post_signed(self, path: str, payload: dict) -> dict:
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
                    third_party_ready=True,
                    third_party_analyzed=False
                )
                offers.append(offer)
            except (ValueError, KeyError) as e:
                logger.warning(f"Ошибка парсинга объявления: {e}")
                continue
        
        logger.info(f"Получено {len(offers)} объявлений для {side}")
        return offers

class P2PArbitrageBot:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.is_running = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.bybit_client = None
        self._stop_requested = False
        self._signal_queue: Dict[int, List[ArbitrageSignal]] = {}  # Очередь сигналов для отправки
        self._sender_task: Optional[asyncio.Task] = None
        
        if BYBIT_API_KEY and BYBIT_API_SECRET:
            self.bybit_client = BybitP2PClient(BYBIT_API_KEY, BYBIT_API_SECRET)
            logger.info("✅ Bybit клиент инициализирован")
        else:
            logger.warning("⚠️ Bybit клиент не инициализирован (нет API ключей)")
        
    async def start(self):
        self.is_running = True
        self._stop_requested = False
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        self._sender_task = asyncio.create_task(self._signal_sender_loop())
        logger.info("Бот успешно запущен")
        
    async def stop(self):
        self.is_running = False
        self._stop_requested = True
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        if self._sender_task:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
        logger.info("Бот остановлен")
    
    async def stop_for_user(self, user_id: int):
        user_subscriptions[user_id] = False
        if user_id in sent_signals:
            sent_signals[user_id].clear()
        if user_id in self._signal_queue:
            self._signal_queue[user_id] = []
        logger.info(f"Мониторинг остановлен для пользователя {user_id}")
    
    def _fetch_p2p_offers_sync(self, side: str) -> List[P2POffer]:
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
        if not filters:
            return True, "OK"
        
        blacklist = filters.get("blacklist", [])
        if blacklist:
            merchant_name_lower = offer.merchant_name.lower()
            for word in blacklist:
                if check_word_in_text(word, merchant_name_lower):
                    return False, f"Найдено запрещенное слово '{word}' в нике мерчанта"
        
        if filters.get("exact_amount"):
            if not (offer.min_amount <= filters["exact_amount"] <= offer.max_amount):
                return False, f"Сумма {filters['exact_amount']:.0f}₽ не входит в лимиты"
        
        if filters.get("min_amount"):
            if offer.max_amount < filters["min_amount"]:
                return False, f"Макс. сумма {offer.max_amount:.0f}₽ < {filters['min_amount']:.0f}₽"
        
        if filters.get("max_amount"):
            if offer.min_amount > filters["max_amount"]:
                return False, f"Мин. сумма {offer.min_amount:.0f}₽ > {filters['max_amount']:.0f}₽"
        
        return True, "OK"
    
    def _generate_profile_url(self, user_mask_id: str) -> str:
        if not user_mask_id or user_mask_id == "0" or user_mask_id == "":
            return "Ссылка недоступна"
        return f"https://www.bybit.com/ru-RU/p2p/profile/{user_mask_id}/USDT/RUB/item"
    
    async def _analyze_offers_with_gemini(self, sellers: List[P2POffer], buyers: List[P2POffer], user_id: int) -> Tuple[List[P2POffer], List[P2POffer]]:
        """Анализирует объявления через Gemini"""
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        
        if not gemini_enabled or not gemini_client:
            for seller in sellers:
                seller.third_party_ready = True
                seller.third_party_analyzed = True
            for buyer in buyers:
                buyer.third_party_ready = True
                buyer.third_party_analyzed = True
            return sellers, buyers
        
        # Собираем объявления для анализа (только непроанализированные)
        offers_to_analyze = []
        
        for seller in sellers:
            if seller.remark and seller.remark.strip():
                cache_key = f"{seller.item_id}_{seller.merchant_name}"
                if cache_key in gemini_cache:
                    cache_time = gemini_cache_ttl.get(cache_key)
                    if cache_time and datetime.now() - cache_time < timedelta(minutes=CACHE_TTL_MINUTES):
                        seller.third_party_ready = gemini_cache[cache_key]
                        seller.third_party_analyzed = True
                        continue
                offers_to_analyze.append(seller)
        
        for buyer in buyers:
            if buyer.remark and buyer.remark.strip():
                cache_key = f"{buyer.item_id}_{buyer.merchant_name}"
                if cache_key in gemini_cache:
                    cache_time = gemini_cache_ttl.get(cache_key)
                    if cache_time and datetime.now() - cache_time < timedelta(minutes=CACHE_TTL_MINUTES):
                        buyer.third_party_ready = gemini_cache[cache_key]
                        buyer.third_party_analyzed = True
                        continue
                offers_to_analyze.append(buyer)
        
        # Анализируем только первые 6 объявлений за цикл
        MAX_ANALYZE = 6
        analyzed_count = 0
        
        for offer in offers_to_analyze[:MAX_ANALYZE]:
            if not self.is_running or self._stop_requested:
                break
            
            result, analyzed = await analyze_single_with_gemini(
                offer.remark, 
                offer.merchant_name, 
                offer.item_id
            )
            
            if analyzed:
                offer.third_party_ready = result
                offer.third_party_analyzed = True
                analyzed_count += 1
                
                # Сохраняем в кэш
                cache_key = f"{offer.item_id}_{offer.merchant_name}"
                if cache_key not in gemini_cache:
                    gemini_cache[cache_key] = result
                    gemini_cache_ttl[cache_key] = datetime.now()
            
            # Небольшая задержка между запросами
            await asyncio.sleep(1.2)
        
        # Для остальных объявлений - помечаем как не проанализированные
        for seller in sellers:
            if not seller.third_party_analyzed:
                # Не проанализированные объявления не будут использованы в сигналах
                seller.third_party_ready = False
        
        for buyer in buyers:
            if not buyer.third_party_analyzed:
                buyer.third_party_ready = False
        
        logger.info(f"✅ Проанализировано Gemini: {analyzed_count} объявлений, всего в кэше: {len(gemini_cache)}")
        
        return sellers, buyers
    
    def _find_all_arbitrage_signals(self, sellers: List[P2POffer], buyers: List[P2POffer],
                                     user_filters: Dict, user_id: int) -> List[ArbitrageSignal]:
        """Находит арбитражные связки только с проанализированными объявлениями"""
        if not sellers or not buyers:
            return []
        
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        
        filtered_sellers = []
        for seller in sellers:
            # Проверяем условия
            passes, _ = self._check_offer_conditions(seller, user_filters)
            if not passes:
                continue
            
            # Если Gemini включен - проверяем анализ и готовность
            if gemini_enabled and gemini_client:
                if not seller.third_party_analyzed:
                    # Не проанализировано - пропускаем
                    continue
                if not seller.third_party_ready:
                    continue
            
            filtered_sellers.append(seller)
        
        filtered_buyers = []
        for buyer in buyers:
            passes, _ = self._check_offer_conditions(buyer, user_filters)
            if not passes:
                continue
            
            if gemini_enabled and gemini_client:
                if not buyer.third_party_analyzed:
                    continue
                if not buyer.third_party_ready:
                    continue
            
            filtered_buyers.append(buyer)
        
        if not filtered_sellers or not filtered_buyers:
            logger.info(f"⚠️ Нет подходящих объявлений после фильтрации")
            return []
        
        filtered_sellers.sort(key=lambda x: x.price)
        filtered_buyers.sort(key=lambda x: x.price, reverse=True)
        
        signals = []
        min_spread = user_filters.get("min_spread", 0.5)
        
        for seller in filtered_sellers[:20]:
            for buyer in filtered_buyers[:20]:
                if seller.price >= buyer.price:
                    continue
                
                spread = ((buyer.price / seller.price) - 1) * 100
                
                if spread < min_spread:
                    continue
                
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
                
                trade_amount = max_trade_amount
                usdt_amount = trade_amount / seller.price if seller.price > 0 else 0
                profit_per_usdt = buyer.price - seller.price
                total_profit_rub = usdt_amount * profit_per_usdt
                
                signal_id = f"{seller.item_id}_{seller.price}_{buyer.item_id}_{buyer.price}"
                
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
    
    async def _signal_sender_loop(self):
        """Отдельный цикл для отправки сигналов из очереди"""
        while self.is_running and not self._stop_requested:
            try:
                for user_id in list(self._signal_queue.keys()):
                    if not user_subscriptions.get(user_id, False):
                        self._signal_queue[user_id] = []
                        continue
                    
                    if user_id not in self._signal_queue:
                        continue
                    
                    queue = self._signal_queue[user_id]
                    if not queue:
                        continue
                    
                    # Отправляем по одному сигналу с задержкой
                    signal = queue.pop(0)
                    delay = user_signal_delay.get(user_id, 4)
                    
                    await self._send_signal(user_id, signal)
                    sent_signals[user_id][signal.signal_id] = datetime.now()
                    logger.info(f"📤 Отправлен сигнал из очереди: SELL={signal.seller.merchant_name} {signal.seller.price:.2f}₽, BUY={signal.buyer.merchant_name} {signal.buyer.price:.2f}₽, прибыль={signal.profit_rub:.2f}₽")
                    
                    await asyncio.sleep(delay)
                
                await asyncio.sleep(0.5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в отправщике сигналов: {e}")
                await asyncio.sleep(1)
    
    async def _monitor_loop(self):
        """Основной цикл мониторинга"""
        cycle_count = 0
        
        while self.is_running and not self._stop_requested:
            try:
                if not self.bybit_client:
                    await asyncio.sleep(30)
                    continue
                
                active_users = []
                for user_id, is_active in user_subscriptions.items():
                    if is_active and not self._stop_requested:
                        active_users.append(user_id)
                
                if not active_users:
                    await asyncio.sleep(15)
                    continue
                
                for user_id in active_users:
                    if not self.is_running or self._stop_requested:
                        return
                    
                    if not user_subscriptions.get(user_id, False):
                        continue
                    
                    filters = user_filters.get(user_id, {})
                    if not filters:
                        continue
                    
                    self._clean_old_signals(user_id)
                    
                    # Получаем объявления
                    sellers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "SELL"
                    )
                    
                    if not self.is_running or self._stop_requested:
                        return
                    
                    buyers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "BUY"
                    )
                    
                    if not self.is_running or self._stop_requested:
                        return
                    
                    if not sellers or not buyers:
                        continue
                    
                    # Анализируем через Gemini
                    sellers, buyers = await self._analyze_offers_with_gemini(sellers, buyers, user_id)
                    
                    # Находим сигналы
                    signals = self._find_all_arbitrage_signals(sellers, buyers, filters, user_id)
                    
                    if signals:
                        logger.info(f"🎯 Найдено {len(signals)} сигналов для пользователя {user_id}")
                        
                        # Добавляем сигналы в очередь
                        if user_id not in self._signal_queue:
                            self._signal_queue[user_id] = []
                        
                        new_signals = 0
                        for signal in signals[:30]:
                            if signal.signal_id not in sent_signals.get(user_id, {}):
                                self._signal_queue[user_id].append(signal)
                                new_signals += 1
                        
                        logger.info(f"📥 Добавлено {new_signals} сигналов в очередь для пользователя {user_id} (всего в очереди: {len(self._signal_queue[user_id])})")
                    else:
                        logger.info(f"ℹ️ Сигналов не найдено для пользователя {user_id}")
                
                # Увеличиваем интервал между циклами
                cycle_count += 1
                if cycle_count % 3 == 0:
                    # Каждый третий цикл делаем паузу подольше для восстановления Gemini
                    await asyncio.sleep(20)
                else:
                    await asyncio.sleep(12)
                
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
        
        seller_profile_url = self._generate_profile_url(signal.seller.user_mask_id)
        buyer_profile_url = self._generate_profile_url(signal.buyer.user_mask_id)
        
        seller_third_party = "✅ Готов" if signal.seller.third_party_ready else "❌ Не готов"
        buyer_third_party = "✅ Готов" if signal.buyer.third_party_ready else "❌ Не готов"
        
        message = f"""🔥 АРБИТРАЖНЫЙ СИГНАЛ 🔥

🟢 ПРОДАВЕЦ (SELLER)
• Курс: {signal.seller.price:.2f}₽
• Лимиты: {format_number(signal.seller.min_amount)} - {format_number(signal.seller.max_amount)}₽
• Мерчант: {signal.seller.merchant_name}
• Платежи от 3-их лиц: {seller_third_party}
• Ссылка на профиль: {seller_profile_url}

🔴 ПОКУПАТЕЛЬ (BUYER)
• Курс: {signal.buyer.price:.2f}₽
• Лимиты: {format_number(signal.buyer.min_amount)} - {format_number(signal.buyer.max_amount)}₽
• Мерчант: {signal.buyer.merchant_name}
• Платежи от 3-их лиц: {buyer_third_party}
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
        filters = user_filters.get(user_id, {})
        delay = user_signal_delay.get(user_id, 4)
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        cache_size = len(gemini_cache)
        queue_size = len(self._signal_queue.get(user_id, []))
        
        if not filters:
            return f"🔧 Фильтры не настроены. Используйте /help для настройки.\n\n⏱ Задержка между сигналами: {delay}с\n🤖 Gemini: {'✅ Включен' if gemini_enabled else '❌ Выключен'}\n💾 Кэш: {cache_size} записей\n📥 Очередь сигналов: {queue_size}"
        
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
        
        settings.append("")
        settings.append(f"⏱ <b>Задержка между сигналами:</b> {delay}с")
        settings.append(f"🤖 <b>Gemini:</b> {'✅ Включен' if gemini_enabled else '❌ Выключен'}")
        settings.append(f"💾 <b>Кэш Gemini:</b> {cache_size} записей")
        settings.append(f"📥 <b>Очередь сигналов:</b> {queue_size}")
        
        if len(settings) == 3:
            settings.append("⚠️ Фильтры настроены, но неактивны (запустите /start_monitoring)")
        
        return "\n".join(settings)


# Функция для установки команд бота
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="settings", description="📋 Показать текущие настройки"),
        BotCommand(command="status", description="📊 Статус мониторинга"),
        BotCommand(command="start_monitoring", description="▶️ Запустить мониторинг"),
        BotCommand(command="stop_monitoring", description="⏹ Остановить мониторинг"),
        BotCommand(command="delay", description="⏱ Установить задержку между сигналами (сек)"),
        BotCommand(command="clear_filters", description="🧹 Очистить все фильтры"),
        BotCommand(command="gemini_on", description="🤖 Включить анализ 3-их лиц (Gemini)"),
        BotCommand(command="gemini_off", description="🤖 Выключить анализ 3-их лиц"),
        BotCommand(command="help", description="❓ Помощь по настройке"),
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
    welcome_text = """
🚀 Добро пожаловать в P2P Арбитраж Бот!

Я ищу арбитражные связки на Bybit P2P и присылаю тебе сигналы.

<b>Доступные команды:</b>
/help - Настройка фильтров
/settings - Текущие настройки
/status - Статус мониторинга
/start_monitoring - Запустить мониторинг
/stop_monitoring - Остановить мониторинг
/delay - Установить задержку между сигналами
/clear_filters - Очистить все фильтры
/gemini_on - Включить анализ 3-их лиц
/gemini_off - Выключить анализ 3-их лиц

<b>Как это работает:</b>
1. Настрой фильтры через /help
2. Запусти мониторинг /start_monitoring
3. Бот будет искать выгодные связки
4. При найденной связке получишь сигнал
5. Если включен Gemini - бот проверяет готовность к 3-им лицам
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
    help_text = """
📖 <b>Помощь по фильтрам</b>

<b>Что можно настраивать:</b>

1. <b>Сумма сделки</b>
   /set_exact 28000 - строго 28 000 ₽
   /set_min 25000 - минимум 25 000 ₽
   /set_max 30000 - максимум 30 000 ₽

2. <b>Черный список (по никам мерчантов)</b>
   /add_blacklist "Имя Мерчанта" - исключить мерчанта
   /remove_blacklist "Имя Мерчанта" - убрать из списка

3. <b>Спред</b>
   /set_spread 0.5 - минимальный спред 0.5%

4. <b>Задержка между сигналами</b>
   /delay 5 - задержка 5 секунд

5. <b>Управление</b>
   /start_monitoring - запуск поиска
   /stop_monitoring - остановка поиска
   /status - текущий статус
   /clear_filters - очистить все фильтры

6. <b>🤖 Gemini (анализ 3-их лиц)</b>
   /gemini_on - Включить анализ
   /gemini_off - Выключить анализ
   
   <b>Важно:</b> Gemini анализирует только ТОП объявления.
   Непроанализированные объявления НЕ попадают в сигналы.

<b>Пример настройки:</b>
1. /set_min 500
2. /set_max 10000
3. /set_spread 0.5
4. /delay 5
5. /gemini_on
6. /start_monitoring
    """
    await safe_send_message(message, help_text)

@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    settings_text = await arbitrage_bot.get_filter_settings(message.from_user.id)
    await safe_send_message(message, settings_text)

@dp.message(Command("status"))
async def cmd_status(message: Message):
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
    user_id = message.from_user.id
    filters = user_filters.get(user_id, {})
    
    if not filters:
        await safe_send_message(
            message,
            "⚠️ Сначала настройте фильтры!\n"
            "Используйте /help для настройки."
        )
        return
    
    sent_signals[user_id] = {}
    user_subscriptions[user_id] = True
    delay = user_signal_delay.get(user_id, 4)
    gemini_enabled = gemini_enabled_for_user.get(user_id, False)
    
    await safe_send_message(
        message,
        f"✅ Мониторинг запущен!\n"
        f"⏱ Задержка между сигналами: {delay}с\n"
        f"🤖 Gemini: {'Включен' if gemini_enabled else 'Выключен'}\n"
        f"📥 Сигналы будут приходить по мере нахождения.\n"
        f"Для остановки: /stop_monitoring"
    )

@dp.message(Command("stop_monitoring"))
async def cmd_stop_monitoring(message: Message):
    user_id = message.from_user.id
    user_subscriptions[user_id] = False
    
    if user_id in sent_signals:
        sent_signals[user_id].clear()
    if user_id in arbitrage_bot._signal_queue:
        arbitrage_bot._signal_queue[user_id] = []
    
    await safe_send_message(message, "⏹ Мониторинг остановлен.")

@dp.message(Command("delay"))
async def cmd_delay(message: Message):
    try:
        args = parse_args_with_quotes(message.text)
        if len(args) != 2:
            await safe_send_message(
                message, 
                "❌ Использование: /delay <секунды>\n"
                "Пример: /delay 5"
            )
            return
        
        delay = float(args[1])
        if delay < 1 or delay > 60:
            await safe_send_message(message, "❌ Задержка должна быть от 1 до 60 секунд")
            return
        
        user_id = message.from_user.id
        user_signal_delay[user_id] = delay
        
        await safe_send_message(message, f"⏱ Задержка между сигналами: {delay}с")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("clear_filters"))
async def cmd_clear_filters(message: Message):
    user_id = message.from_user.id
    user_filters[user_id] = {}
    user_subscriptions[user_id] = False
    sent_signals[user_id] = {}
    if user_id in arbitrage_bot._signal_queue:
        arbitrage_bot._signal_queue[user_id] = []
    await safe_send_message(message, "🧹 Все фильтры очищены. Мониторинг остановлен.")

@dp.message(Command("gemini_on"))
async def cmd_gemini_on(message: Message):
    user_id = message.from_user.id
    
    if not gemini_client:
        await safe_send_message(message, "❌ Gemini недоступен!")
        return
    
    gemini_enabled_for_user[user_id] = True
    gemini_cache.clear()
    gemini_cache_ttl.clear()
    
    await safe_send_message(
        message,
        "🤖 Gemini ВКЛЮЧЕН!\n\n"
        "Теперь бот будет проверять готовность к 3-им лицам.\n"
        "⚠️ Непроанализированные объявления НЕ попадают в сигналы.\n"
        "Для отключения: /gemini_off"
    )

@dp.message(Command("gemini_off"))
async def cmd_gemini_off(message: Message):
    user_id = message.from_user.id
    gemini_enabled_for_user[user_id] = False
    
    await safe_send_message(
        message,
        "🤖 Gemini ВЫКЛЮЧЕН!\n\n"
        "Все объявления будут показываться без фильтрации.\n"
        "Для включения: /gemini_on"
    )

# --- Команды для настройки фильтров ---

@dp.message(Command("set_exact"))
async def cmd_set_exact(message: Message):
    try:
        args = parse_args_with_quotes(message.text)
        if len(args) != 2:
            await safe_send_message(message, "❌ Использование: /set_exact <сумма>")
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
        
        await safe_send_message(message, f"✅ Точная сумма: {amount:,.0f}₽")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("set_min"))
async def cmd_set_min(message: Message):
    try:
        args = parse_args_with_quotes(message.text)
        if len(args) != 2:
            await safe_send_message(message, "❌ Использование: /set_min <сумма>")
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
        
        await safe_send_message(message, f"✅ Минимальная сумма: {amount:,.0f}₽")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("set_max"))
async def cmd_set_max(message: Message):
    try:
        args = parse_args_with_quotes(message.text)
        if len(args) != 2:
            await safe_send_message(message, "❌ Использование: /set_max <сумма>")
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
        
        await safe_send_message(message, f"✅ Максимальная сумма: {amount:,.0f}₽")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("set_spread"))
async def cmd_set_spread(message: Message):
    try:
        args = parse_args_with_quotes(message.text)
        if len(args) != 2:
            await safe_send_message(message, "❌ Использование: /set_spread <процент>")
            return
        
        spread = float(args[1])
        if spread < 0:
            await safe_send_message(message, "❌ Спред должен быть положительным")
            return
        
        user_id = message.from_user.id
        if user_id not in user_filters:
            user_filters[user_id] = {}
        
        user_filters[user_id]["min_spread"] = spread
        
        await safe_send_message(message, f"✅ Минимальный спред: {spread}%")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("add_blacklist"))
async def cmd_add_blacklist(message: Message):
    args = parse_args_with_quotes(message.text)
    if len(args) != 2:
        await safe_send_message(
            message, 
            "❌ Использование: /add_blacklist <ник мерчанта>\n"
            "Пример: /add_blacklist \"ALL FOR ALL\""
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
        await safe_send_message(message, f"✅ Добавлено в черный список: {word}")
    else:
        await safe_send_message(message, f"⚠️ '{word}' уже в черном списке")

@dp.message(Command("remove_blacklist"))
async def cmd_remove_blacklist(message: Message):
    args = parse_args_with_quotes(message.text)
    if len(args) != 2:
        await safe_send_message(message, "❌ Использование: /remove_blacklist <ник>")
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
        await safe_send_message(message, f"⚠️ '{word}' не найдено")


async def on_startup():
    await set_bot_commands(bot)
    await arbitrage_bot.start()
    logger.info("Бот запущен и готов к работе!")

async def on_shutdown():
    await arbitrage_bot.stop()
    logger.info("Бот остановлен")

async def main():
    try:
        await on_startup()
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        await on_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
