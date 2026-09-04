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
gemini_enabled_for_user: Dict[int, bool] = {}
gemini_available = True

if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("✅ Gemini клиент инициализирован (модель: gemini-3.5-flash-lite)")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Gemini: {e}")
        gemini_client = None
        gemini_available = False
else:
    logger.warning("⚠️ API ключ Gemini не найден! Функция анализа 3-их лиц будет недоступна.")
    gemini_available = False

# Хранилище для фильтров пользователей
user_filters: Dict[int, Dict] = {}
user_subscriptions: Dict[int, bool] = {}

# Хранилище для отправленных сигналов (с временем)
sent_signals: Dict[int, Dict[str, datetime]] = {}

# Хранилище для настроек задержки между сигналами (по умолчанию 4 секунды)
user_signal_delay: Dict[int, int] = {}

# Кэш для результатов Gemini (с временем жизни)
gemini_cache: Dict[str, Dict[str, any]] = {}
gemini_cache_ttl: Dict[str, datetime] = {}
CACHE_TTL_MINUTES = 60

# Для контроля лимитов Gemini (15 запросов в минуту)
gemini_request_timestamps: List[datetime] = []
GEMINI_MAX_REQUESTS_PER_MINUTE = 12

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
    global gemini_request_timestamps
    
    now = datetime.now()
    gemini_request_timestamps = [ts for ts in gemini_request_timestamps if now - ts < timedelta(seconds=60)]
    
    if len(gemini_request_timestamps) >= GEMINI_MAX_REQUESTS_PER_MINUTE:
        oldest = min(gemini_request_timestamps)
        wait_seconds = 60 - (now - oldest).seconds + 2
        if wait_seconds > 0:
            logger.warning(f"⏳ Достигнут лимит Gemini ({GEMINI_MAX_REQUESTS_PER_MINUTE}/мин), ждем {wait_seconds}с")
            await asyncio.sleep(wait_seconds)

# Функция проверки доступности Gemini (при старте и раз в 2 часа)
async def check_gemini_availability():
    """Проверяет, доступна ли модель Gemini"""
    global gemini_available
    
    if not gemini_client:
        gemini_available = False
        return False
    
    try:
        test_prompt = "Ответь только: ok"
        
        response = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: gemini_client.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=test_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=10,
                    )
                )
            ),
            timeout=5.0
        )
        
        gemini_available = True
        logger.info("✅ Gemini доступен и работает")
        return True
        
    except asyncio.TimeoutError:
        logger.error("⏰ Таймаут проверки Gemini")
        gemini_available = False
        return False
    except Exception as e:
        error_msg = str(e)
        if "404" in error_msg or "NOT_FOUND" in error_msg:
            logger.error(f"❌ Модель Gemini недоступна (404). Проверьте название модели.")
        elif "429" in error_msg:
            logger.error(f"❌ Превышен лимит Gemini (429). Квота будет восстановлена позже.")
        else:
            logger.error(f"❌ Ошибка проверки Gemini: {error_msg[:100]}")
        
        gemini_available = False
        return False

# Функция для массового анализа объявлений через Gemini (батчинг)
async def analyze_batch_with_gemini(offers: List[Tuple[str, str, str]]) -> Dict[str, Dict[str, any]]:
    """
    Анализирует несколько объявлений через Gemini в одном запросе.
    offers: список кортежей (item_id, merchant_name, remark)
    Возвращает словарь с результатами по каждому item_id
    """
    if not offers or not gemini_client:
        return {}
    
    # Формируем запрос для анализа всех объявлений
    offers_text = "\n\n".join([
        f"Объявление {i+1} (ID: {item_id}, Мерчант: {merchant_name}):\nТекст: \"{remark}\""
        for i, (item_id, merchant_name, remark) in enumerate(offers)
    ])
    
    prompt = f"""
Твоя задача — проанализировать каждое объявление и определить, готов ли мерчант принимать платежи от ТРЕТЬИХ ЛИЦ.

Третьи лица — это ситуация, когда платеж совершает не сам покупатель, а другое лицо.

Важно: в тексте могут быть опечатки. Твоя задача — понять СМЫСЛ написанного.

Правила для каждого объявления:
1. НЕ ГОТОВ к 3-им лицам, если есть:
   - "только от себя", "только свои карты", "только личная карта"
   - "не принимаю от третьих лиц", "без 3-их лиц"
   - "только 1 лица", "строго 1 лица", "только первые лица"
   - "только от 1 лица" (включая опечатки типа "лаца")
   - "не работаю с треугольниками"

2. ГОТОВ к 3-им лицам, если есть:
   - "принимаю от третьих лиц", "можно от друзей"
   - "принимаю от 3 лиц", "от 3-х лиц принимаю"

3. Если нет упоминаний — ГОТОВ (по умолчанию).

{offers_text}

Верни ТОЛЬКО JSON-массив с результатами для каждого объявления в том же порядке.
Используй числовые id (1, 2, 3...) в том же порядке, как в запросе.

Пример ответа:
[
  {{"id": 1, "ready": true, "comment": "Нет запретов"}},
  {{"id": 2, "ready": false, "comment": "Указано 'ТОЛЬКО 1 ЛИЦА'"}}
]

ВЕРНИ ТОЛЬКО JSON, БЕЗ ДРУГОГО ТЕКСТА!
"""
    
    try:
        logger.info(f"📤 Отправка батча из {len(offers)} объявлений в Gemini")
        
        response = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: gemini_client.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        top_p=0.8,
                        max_output_tokens=1200,  # Увеличено для батча из 20 объявлений
                    )
                )
            ),
            timeout=25.0  # Больше времени на 20 объявлений
        )
        
        gemini_request_timestamps.append(datetime.now())
        
        result_text = response.text.strip()
        logger.info(f"📥 Ответ Gemini (батч): {result_text[:200]}...")
        
        # Удаляем маркеры кода
        result_text = re.sub(r'```json\s*', '', result_text)
        result_text = re.sub(r'```\s*', '', result_text)
        result_text = result_text.strip()
        
        try:
            data = json.loads(result_text)
            results = {}
            if isinstance(data, list):
                for item in data:
                    item_id = str(item.get("id"))
                    if item_id:
                        results[item_id] = {
                            "ready": item.get("ready", True),
                            "comment": item.get("comment", "")
                        }
                return results
        except json.JSONDecodeError:
            logger.warning(f"Не удалось распарсить JSON: {result_text[:100]}")
            return {}
            
    except asyncio.TimeoutError:
        logger.error(f"⏰ Таймаут батч-запроса Gemini (25 сек)")
        return {}
    except Exception as e:
        logger.error(f"❌ Ошибка батч-анализа Gemini: {e}")
        return {}

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
    third_party_analyzed: bool = False
    gemini_error: bool = False
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
                    third_party_analyzed=False,
                    gemini_error=False
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
        self._stop_requested = False
        self._gemini_check_task: Optional[asyncio.Task] = None
        self._last_gemini_check: Optional[datetime] = None
        
        if BYBIT_API_KEY and BYBIT_API_SECRET:
            self.bybit_client = BybitP2PClient(BYBIT_API_KEY, BYBIT_API_SECRET)
            logger.info("✅ Bybit клиент инициализирован")
        else:
            logger.warning("⚠️ Bybit клиент не инициализирован (нет API ключей)")
        
    async def start(self):
        self.is_running = True
        self._stop_requested = False
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        
        # Проверяем Gemini при старте
        if gemini_client:
            await check_gemini_availability()
            self._last_gemini_check = datetime.now()
            # Запускаем периодическую проверку (раз в 2 часа)
            self._gemini_check_task = asyncio.create_task(self._periodic_gemini_check())
        
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
        
        if self._gemini_check_task:
            self._gemini_check_task.cancel()
            try:
                await self._gemini_check_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Бот остановлен")
    
    async def _periodic_gemini_check(self):
        """Периодическая проверка доступности Gemini - 1 раз в 2 часа"""
        while self.is_running and not self._stop_requested:
            await asyncio.sleep(7200)  # 2 часа
            
            if not self.is_running or self._stop_requested:
                break
            
            if gemini_client:
                now = datetime.now()
                if not gemini_available or (self._last_gemini_check and (now - self._last_gemini_check).seconds > 7200):
                    logger.info("🔄 Периодическая проверка Gemini (раз в 2 часа)")
                    await check_gemini_availability()
                    self._last_gemini_check = now
    
    async def stop_for_user(self, user_id: int):
        user_subscriptions[user_id] = False
        if user_id in sent_signals:
            sent_signals[user_id].clear()
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
                    logger.info(f"Найдено запрещенное слово '{word}' в нике мерчанта {offer.merchant_name}")
                    return False, f"Найдено запрещенное слово '{word}' в нике мерчанта {offer.merchant_name}"
        
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
        if not user_mask_id or user_mask_id == "0" or user_mask_id == "":
            return "Ссылка недоступна"
        return f"https://www.bybit.com/ru-RU/p2p/profile/{user_mask_id}/USDT/RUB/item"
    
    async def _analyze_offers_with_gemini(self, sellers: List[P2POffer], buyers: List[P2POffer], user_id: int) -> Tuple[List[P2POffer], List[P2POffer]]:
        """Анализирует объявления через Gemini с батчингом (20 объявлений за запрос)"""
        global gemini_available
        
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        
        if not gemini_enabled or not gemini_client or not gemini_available:
            for seller in sellers:
                seller.third_party_ready = True
                seller.third_party_analyzed = True
                seller.gemini_error = not gemini_available
            for buyer in buyers:
                buyer.third_party_ready = True
                buyer.third_party_analyzed = True
                buyer.gemini_error = not gemini_available
            return sellers, buyers
        
        temp_filters = user_filters.get(user_id, {})
        
        potential_sellers = []
        for seller in sellers:
            passes, _ = self._check_offer_conditions(seller, temp_filters)
            if passes:
                potential_sellers.append(seller)
        
        potential_buyers = []
        for buyer in buyers:
            passes, _ = self._check_offer_conditions(buyer, temp_filters)
            if passes:
                potential_buyers.append(buyer)
        
        potential_sellers.sort(key=lambda x: x.price)
        potential_buyers.sort(key=lambda x: x.price, reverse=True)
        
        # Берем больше объявлений для батчинга (40 с каждой стороны)
        top_sellers = potential_sellers[:30]
        top_buyers = potential_buyers[:30]
        
        # Собираем объявления для анализа
        offers_to_analyze = []
        
        for seller in top_sellers:
            cache_key = f"{seller.item_id}_{seller.merchant_name}"
            if cache_key in gemini_cache:
                cache_time = gemini_cache_ttl.get(cache_key)
                if cache_time and datetime.now() - cache_time < timedelta(minutes=CACHE_TTL_MINUTES):
                    result = gemini_cache[cache_key]
                    seller.third_party_ready = result.get("ready", True)
                    seller.third_party_analyzed = result.get("analyzed", True)
                    seller.gemini_error = result.get("gemini_error", False)
                    continue
            if seller.remark and seller.remark.strip():
                offers_to_analyze.append(seller)
        
        for buyer in top_buyers:
            cache_key = f"{buyer.item_id}_{buyer.merchant_name}"
            if cache_key in gemini_cache:
                cache_time = gemini_cache_ttl.get(cache_key)
                if cache_time and datetime.now() - cache_time < timedelta(minutes=CACHE_TTL_MINUTES):
                    result = gemini_cache[cache_key]
                    buyer.third_party_ready = result.get("ready", True)
                    buyer.third_party_analyzed = result.get("analyzed", True)
                    buyer.gemini_error = result.get("gemini_error", False)
                    continue
            if buyer.remark and buyer.remark.strip():
                offers_to_analyze.append(buyer)
        
        # Батчинг: отправляем по 20 объявлений за запрос
        BATCH_SIZE = 20
        MAX_BATCHES_PER_CYCLE = 2  # Максимум 2 батча за цикл (40 объявлений)
        
        if offers_to_analyze:
            # Сортируем по приоритету
            priority_offers = []
            other_offers = []
            
            for offer in offers_to_analyze:
                remark_lower = offer.remark.lower()
                if any(word in remark_lower for word in ['3 лиц', 'треть', '1 лиц', 'перв', 'однофамил']):
                    priority_offers.append(offer)
                else:
                    other_offers.append(offer)
            
            sorted_offers = priority_offers + other_offers
            
            # Берем максимум BATCH_SIZE * MAX_BATCHES_PER_CYCLE объявлений
            max_offers = BATCH_SIZE * MAX_BATCHES_PER_CYCLE
            if len(sorted_offers) > max_offers:
                sorted_offers = sorted_offers[:max_offers]
                logger.info(f"📊 Взято {len(sorted_offers)} объявлений из {len(offers_to_analyze)} для батчинга")
            
            # Разбиваем на батчи по 20
            for i in range(0, len(sorted_offers), BATCH_SIZE):
                batch = sorted_offers[i:i+BATCH_SIZE]
                
                # Подготавливаем данные для батч-запроса
                batch_data = [(offer.item_id, offer.merchant_name, offer.remark) for offer in batch]
                
                # Отправляем батч-запрос
                batch_results = await analyze_batch_with_gemini(batch_data)
                
                # Применяем результаты к объявлениям
                for idx, offer in enumerate(batch, 1):
                    idx_str = str(idx)
                    if idx_str in batch_results:
                        result = batch_results[idx_str]
                        offer.third_party_ready = result.get("ready", True)
                        offer.third_party_analyzed = True
                        offer.gemini_error = False
                        
                        # Сохраняем в кэш
                        cache_key = f"{offer.item_id}_{offer.merchant_name}"
                        gemini_cache[cache_key] = {
                            "ready": offer.third_party_ready,
                            "analyzed": True,
                            "gemini_error": False
                        }
                        gemini_cache_ttl[cache_key] = datetime.now()
                        
                        logger.info(f"✅ Gemini анализ для {offer.merchant_name}: готов={offer.third_party_ready}")
                    else:
                        # Если не получили результат - помечаем как ошибку
                        offer.third_party_ready = True
                        offer.third_party_analyzed = True
                        offer.gemini_error = True
                        logger.warning(f"⚠️ Нет результата для {offer.merchant_name} в батче")
                
                # Задержка между батчами для соблюдения лимитов
                if i + BATCH_SIZE < len(sorted_offers):
                    await asyncio.sleep(2.0)  # Чуть больше задержка для 20 объявлений
        
        # Остальные объявления помечаем как не проанализированные
        for seller in sellers:
            if seller not in top_sellers and seller not in offers_to_analyze:
                cache_key = f"{seller.item_id}_{seller.merchant_name}"
                if cache_key in gemini_cache:
                    cache_time = gemini_cache_ttl.get(cache_key)
                    if cache_time and datetime.now() - cache_time < timedelta(minutes=CACHE_TTL_MINUTES):
                        result = gemini_cache[cache_key]
                        seller.third_party_ready = result.get("ready", True)
                        seller.third_party_analyzed = result.get("analyzed", True)
                        seller.gemini_error = result.get("gemini_error", False)
                    else:
                        seller.third_party_ready = False
                        seller.third_party_analyzed = False
                        seller.gemini_error = False
                else:
                    seller.third_party_ready = False
                    seller.third_party_analyzed = False
                    seller.gemini_error = False
        
        for buyer in buyers:
            if buyer not in top_buyers and buyer not in offers_to_analyze:
                cache_key = f"{buyer.item_id}_{buyer.merchant_name}"
                if cache_key in gemini_cache:
                    cache_time = gemini_cache_ttl.get(cache_key)
                    if cache_time and datetime.now() - cache_time < timedelta(minutes=CACHE_TTL_MINUTES):
                        result = gemini_cache[cache_key]
                        buyer.third_party_ready = result.get("ready", True)
                        buyer.third_party_analyzed = result.get("analyzed", True)
                        buyer.gemini_error = result.get("gemini_error", False)
                    else:
                        buyer.third_party_ready = False
                        buyer.third_party_analyzed = False
                        buyer.gemini_error = False
                else:
                    buyer.third_party_ready = False
                    buyer.third_party_analyzed = False
                    buyer.gemini_error = False
        
        total_batches = (len(offers_to_analyze) + BATCH_SIZE - 1) // BATCH_SIZE if offers_to_analyze else 0
        logger.info(f"✅ Проанализировано Gemini (батчинг 20): {len(offers_to_analyze)} объявлений за {total_batches} запросов, всего в кэше: {len(gemini_cache)}")
        
        return sellers, buyers
    
    def _find_all_arbitrage_signals(self, sellers: List[P2POffer], buyers: List[P2POffer],
                                     user_filters: Dict, user_id: int) -> List[ArbitrageSignal]:
        if not sellers or not buyers:
            return []
        
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        
        filtered_sellers = []
        for seller in sellers:
            passes, _ = self._check_offer_conditions(seller, user_filters)
            if passes:
                if gemini_enabled and gemini_client:
                    if not seller.third_party_analyzed:
                        logger.debug(f"⏳ Seller {seller.merchant_name} еще не проанализирован Gemini - пропускаем")
                        continue
                    if seller.gemini_error:
                        logger.debug(f"⚠️ Seller {seller.merchant_name} - ошибка Gemini, пропускаем")
                        continue
                    if not seller.third_party_ready:
                        logger.debug(f"❌ Seller {seller.merchant_name} не готов к 3-им лицам - пропускаем")
                        continue
                filtered_sellers.append(seller)
        
        filtered_buyers = []
        for buyer in buyers:
            passes, _ = self._check_offer_conditions(buyer, user_filters)
            if passes:
                if gemini_enabled and gemini_client:
                    if not buyer.third_party_analyzed:
                        logger.debug(f"⏳ Buyer {buyer.merchant_name} еще не проанализирован Gemini - пропускаем")
                        continue
                    if buyer.gemini_error:
                        logger.debug(f"⚠️ Buyer {buyer.merchant_name} - ошибка Gemini, пропускаем")
                        continue
                    if not buyer.third_party_ready:
                        logger.debug(f"❌ Buyer {buyer.merchant_name} не готов к 3-им лицам - пропускаем")
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
        while self.is_running and not self._stop_requested:
            try:
                if not self.bybit_client:
                    logger.warning("Пропуск цикла: Bybit клиент не инициализирован")
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
                        logger.info(f"Остановка мониторинга по запросу пользователя {user_id}")
                        return
                    
                    if not user_subscriptions.get(user_id, False):
                        continue
                    
                    filters = user_filters.get(user_id, {})
                    if not filters:
                        continue
                    
                    self._clean_old_signals(user_id)
                    
                    sellers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "SELL"
                    )
                    
                    if not self.is_running or self._stop_requested:
                        logger.info(f"Остановка мониторинга после получения SELL объявлений")
                        return
                    
                    buyers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "BUY"
                    )
                    
                    if not self.is_running or self._stop_requested:
                        logger.info(f"Остановка мониторинга после получения BUY объявлений")
                        return
                    
                    if not sellers or not buyers:
                        continue
                    
                    sellers, buyers = await self._analyze_offers_with_gemini(sellers, buyers, user_id)
                    
                    signals = self._find_all_arbitrage_signals(sellers, buyers, filters, user_id)
                    
                    if signals:
                        logger.info(f"Найдено {len(signals)} сигналов для пользователя {user_id}")
                        
                        if user_id not in sent_signals:
                            sent_signals[user_id] = {}
                        
                        delay_seconds = user_signal_delay.get(user_id, 4)
                        
                        sent_count = 0
                        skipped_count = 0
                        
                        for signal in signals[:30]:
                            if not self.is_running or self._stop_requested:
                                logger.info(f"Остановка мониторинга во время отправки сигналов")
                                return
                            
                            if not user_subscriptions.get(user_id, False):
                                logger.info(f"Пользователь {user_id} отключил мониторинг, пропускаем сигналы")
                                break
                            
                            if signal.signal_id not in sent_signals[user_id]:
                                await self._send_signal(user_id, signal)
                                sent_signals[user_id][signal.signal_id] = datetime.now()
                                sent_count += 1
                                logger.info(f"Отправлен сигнал #{sent_count}: SELL={signal.seller.merchant_name} {signal.seller.price:.2f}₽, BUY={signal.buyer.merchant_name} {signal.buyer.price:.2f}₽, прибыль={signal.profit_rub:.2f}₽")
                                
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
                
                await asyncio.sleep(25)
                
            except asyncio.CancelledError:
                logger.info("Цикл мониторинга отменен")
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга: {e}")
                await asyncio.sleep(30)
    
    async def _send_signal(self, user_id: int, signal: ArbitrageSignal):
        def format_number(num):
            if num >= 1000:
                return f"{num:,.0f}".replace(",", " ")
            return f"{num:.0f}"
        
        trade_amount = min(signal.seller.max_amount, signal.buyer.max_amount)
        usdt_amount = trade_amount / signal.seller.price if signal.seller.price > 0 else 0
        
        seller_profile_url = self._generate_profile_url(signal.seller.user_mask_id)
        buyer_profile_url = self._generate_profile_url(signal.buyer.user_mask_id)
        
        logger.info(f"📝 SIGNAL REMARKS for user {user_id}:")
        logger.info(f"   SELLER (merchant: {signal.seller.merchant_name}, ID: {signal.seller.item_id}) REMARK: {signal.seller.remark[:300]}...")
        logger.info(f"   BUYER (merchant: {signal.buyer.merchant_name}, ID: {signal.buyer.item_id}) REMARK: {signal.buyer.remark[:300]}...")
        
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        
        message_lines = ["🔥 АРБИТРАЖНЫЙ СИГНАЛ 🔥", ""]
        
        # Продавец
        message_lines.append("🟢 ПРОДАВЕЦ (SELLER)")
        message_lines.append(f"• Курс: {signal.seller.price:.2f}₽")
        message_lines.append(f"• Лимиты: {format_number(signal.seller.min_amount)} - {format_number(signal.seller.max_amount)}₽")
        message_lines.append(f"• Мерчант: {signal.seller.merchant_name}")
        
        if gemini_enabled and gemini_client:
            if signal.seller.gemini_error:
                message_lines.append("• 🤖 Нейросеть временно недоступна")
            else:
                status = "✅ Готов" if signal.seller.third_party_ready else "❌ Не готов"
                message_lines.append(f"• Платежи от 3-их лиц: {status}")
        
        message_lines.append(f"• Ссылка на профиль: {seller_profile_url}")
        message_lines.append("")
        
        # Покупатель
        message_lines.append("🔴 ПОКУПАТЕЛЬ (BUYER)")
        message_lines.append(f"• Курс: {signal.buyer.price:.2f}₽")
        message_lines.append(f"• Лимиты: {format_number(signal.buyer.min_amount)} - {format_number(signal.buyer.max_amount)}₽")
        message_lines.append(f"• Мерчант: {signal.buyer.merchant_name}")
        
        if gemini_enabled and gemini_client:
            if signal.buyer.gemini_error:
                message_lines.append("• 🤖 Нейросеть временно недоступна")
            else:
                status = "✅ Готов" if signal.buyer.third_party_ready else "❌ Не готов"
                message_lines.append(f"• Платежи от 3-их лиц: {status}")
        
        message_lines.append(f"• Ссылка на профиль: {buyer_profile_url}")
        message_lines.append("")
        
        # Расчет прибыли
        message_lines.append("📊 РАСЧЕТ ПРИБЫЛИ")
        message_lines.append(f"• Спред: {signal.spread:.2f}%")
        message_lines.append(f"• Прибыль с 1 USDT: {signal.profit:.2f}₽")
        message_lines.append(f"• Сумма сделки: {format_number(trade_amount)}₽")
        message_lines.append(f"• USDT: {usdt_amount:.2f}")
        message_lines.append(f"• Потенциальная прибыль: {signal.profit_rub:,.2f}₽")
        
        message = "\n".join(message_lines)
        
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
        
        gemini_status = "❌ Недоступен"
        if gemini_client and gemini_available:
            gemini_status = "✅ Доступен"
        elif gemini_client and not gemini_available:
            gemini_status = "⚠️ Ошибка (проверьте модель)"
        
        if not filters:
            return f"🔧 Фильтры не настроены. Используйте /help для настройки.\n\n⏱ Задержка между сигналами: {delay}с\n🤖 Gemini: {'✅ Включен' if gemini_enabled else '❌ Выключен'} ({gemini_status})\n💾 Кэш: {cache_size} записей"
        
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
        settings.append(f"🤖 <b>Gemini (3-и лица):</b> {'✅ Включен' if gemini_enabled else '❌ Выключен'} ({gemini_status})")
        settings.append(f"💾 <b>Кэш Gemini:</b> {cache_size} записей")
        
        if len(settings) == 3:
            settings.append("⚠️ Фильтры настроены, но неактивны (запустите /start_monitoring)")
        
        return "\n".join(settings)


async def set_bot_commands(bot: Bot):
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
            "Используйте /help для настройки и /settings для просмотра."
        )
        return
    
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
    user_id = message.from_user.id
    user_subscriptions[user_id] = False
    
    if user_id in sent_signals:
        sent_signals[user_id].clear()
    
    await safe_send_message(
        message, 
        "⏹ Мониторинг остановлен.\n"
        "Все активные задачи для вас отменены."
    )

@dp.message(Command("delay"))
async def cmd_delay(message: Message):
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
    user_id = message.from_user.id
    user_filters[user_id] = {}
    user_subscriptions[user_id] = False
    sent_signals[user_id] = {}
    await safe_send_message(message, "🧹 Все фильтры очищены. Мониторинг остановлен. Задержка и Gemini сохранены.")

@dp.message(Command("gemini_on"))
async def cmd_gemini_on(message: Message):
    user_id = message.from_user.id
    
    if not gemini_client:
        await safe_send_message(
            message,
            "❌ Gemini недоступен! Проверьте API ключ в настройках бота."
        )
        return
    
    await check_gemini_availability()
    
    if not gemini_available:
        await safe_send_message(
            message,
            "⚠️ Gemini включен, но модель недоступна!\n"
            "Проверьте название модели в настройках бота.\n"
            "Попробуйте использовать: gemini-3.5-flash-lite"
        )
        return
    
    gemini_enabled_for_user[user_id] = True
    global gemini_cache, gemini_cache_ttl
    gemini_cache = {}
    gemini_cache_ttl = {}
    
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
