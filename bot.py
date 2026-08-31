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
from collections import deque

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

# Инициализация Gemini
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

# Хранилища
user_filters: Dict[int, Dict] = {}
user_subscriptions: Dict[int, bool] = {}
sent_signals: Dict[int, Dict[str, datetime]] = {}
user_signal_delay: Dict[int, int] = {}

# Кэш результатов Gemini
gemini_cache: Dict[str, Dict[str, any]] = {}

# Очередь проверенных сигналов для каждого пользователя
user_verified_signals_queue: Dict[int, deque] = {}

# Флаг анализа для пользователя
user_analysis_in_progress: Dict[int, bool] = {}

# Лимиты Gemini (бесплатный тариф)
GEMINI_RATE_LIMIT_PER_MINUTE = 15  # запросов в минуту
GEMINI_RATE_LIMIT_PER_DAY = 500    # запросов в день
GEMINI_REQUEST_COUNT_MINUTE = 0
GEMINI_REQUEST_COUNT_DAY = 0
GEMINI_LAST_RESET_MINUTE = datetime.now()
GEMINI_LAST_RESET_DAY = datetime.now()

# ========== ОПРЕДЕЛЕНИЕ DATACLASSES ==========

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

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

# Функция для проверки квоты Gemini
def check_gemini_quota() -> bool:
    """Проверяет, не превышен ли лимит запросов к Gemini"""
    global GEMINI_REQUEST_COUNT_MINUTE, GEMINI_REQUEST_COUNT_DAY
    global GEMINI_LAST_RESET_MINUTE, GEMINI_LAST_RESET_DAY
    
    now = datetime.now()
    
    # Сброс минутного лимита
    if (now - GEMINI_LAST_RESET_MINUTE).seconds >= 60:
        GEMINI_REQUEST_COUNT_MINUTE = 0
        GEMINI_LAST_RESET_MINUTE = now
        logger.info("🔄 Сброс минутного лимита Gemini")
    
    # Сброс дневного лимита
    if (now - GEMINI_LAST_RESET_DAY).days >= 1:
        GEMINI_REQUEST_COUNT_DAY = 0
        GEMINI_LAST_RESET_DAY = now
        logger.info("🔄 Сброс дневного лимита Gemini")
    
    if GEMINI_REQUEST_COUNT_MINUTE >= GEMINI_RATE_LIMIT_PER_MINUTE:
        logger.warning(f"⚠️ Превышен минутный лимит Gemini ({GEMINI_RATE_LIMIT_PER_MINUTE} запросов/мин)")
        return False
    
    if GEMINI_REQUEST_COUNT_DAY >= GEMINI_RATE_LIMIT_PER_DAY:
        logger.warning(f"⚠️ Превышен дневной лимит Gemini ({GEMINI_RATE_LIMIT_PER_DAY} запросов/день)")
        return False
    
    return True

def increment_gemini_count():
    """Увеличивает счетчик запросов к Gemini"""
    global GEMINI_REQUEST_COUNT_MINUTE, GEMINI_REQUEST_COUNT_DAY
    GEMINI_REQUEST_COUNT_MINUTE += 1
    GEMINI_REQUEST_COUNT_DAY += 1
    remaining_minute = GEMINI_RATE_LIMIT_PER_MINUTE - GEMINI_REQUEST_COUNT_MINUTE
    remaining_day = GEMINI_RATE_LIMIT_PER_DAY - GEMINI_REQUEST_COUNT_DAY
    logger.info(f"📊 Gemini: {GEMINI_REQUEST_COUNT_MINUTE}/{GEMINI_RATE_LIMIT_PER_MINUTE} в минуту, {GEMINI_REQUEST_COUNT_DAY}/{GEMINI_RATE_LIMIT_PER_DAY} в день")

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

def check_word_in_text(word: str, text: str) -> bool:
    escaped_word = re.escape(word)
    if len(word) <= 3:
        pattern = rf'\b{escaped_word}\b'
    else:
        pattern = rf'{escaped_word}'
    return bool(re.search(pattern, text, re.IGNORECASE))

async def safe_send_message(message: Message, text: str):
    try:
        await message.answer(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Ошибка HTML-парсинга, отправляем обычный текст: {e}")
        await message.answer(text.replace('<', '[').replace('>', ']'))

# ========== ФУНКЦИЯ ПАКЕТНОГО АНАЛИЗА GEMINI ==========

async def analyze_signals_batch(signals: List[Tuple[ArbitrageSignal, int]], user_id: int) -> Dict[str, Dict[str, any]]:
    """
    Анализирует несколько сигналов в одном запросе к Gemini.
    signals: список кортежей (ArbitrageSignal, user_id)
    Возвращает словарь с результатами по signal_id
    """
    if not gemini_client or not signals:
        return {}
    
    # Проверяем квоту
    if not check_gemini_quota():
        logger.warning("⚠️ Квота Gemini исчерпана, пропускаем пакетный анализ")
        return {}
    
    # Формируем промпт для всех сигналов (полный remark, без обрезки!)
    prompt_parts = []
    for idx, (signal, _) in enumerate(signals):
        seller_remark = signal.seller.remark if signal.seller.remark else ""
        buyer_remark = signal.buyer.remark if signal.buyer.remark else ""
        
        prompt_parts.append(f"""
СИГНАЛ #{idx + 1}:
Продавец: {signal.seller.merchant_name}
Текст продавца: "{seller_remark}"
Покупатель: {signal.buyer.merchant_name}
Текст покупателя: "{buyer_remark}"
""")
    
    full_prompt = f"""
Проанализируй следующие объявления и определи для КАЖДОГО, готов ли мерчант принимать платежи от ТРЕТЬИХ ЛИЦ.

Правила:
- Если есть "только от себя", "только свои карты", "не принимаю от третьих лиц", "строго 1 лицо", "первые лица" -> НЕ ГОТОВ (false)
- Если есть "принимаю от третьих лиц", "можно от друзей", "3-и лица" -> ГОТОВ (true)
- Если нет упоминаний о третьих лицах -> ГОТОВ (true)

Верни ответ ТОЛЬКО в формате JSON:
{{
    "results": [
        {{
            "index": 1,
            "seller_ready": true/false,
            "buyer_ready": true/false
        }},
        ...
    ]
}}

Объявления для анализа:
{''.join(prompt_parts)}
"""
    
    try:
        logger.info(f"🧠 Отправка ПАКЕТНОГО запроса в Gemini для {len(signals)} сигналов")
        
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: gemini_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    top_p=0.8,
                    max_output_tokens=800,  # Увеличиваем для полных remark
                )
            )
        )
        
        result_text = response.text.strip()
        logger.info(f"📥 Получен ответ от Gemini: {result_text[:200]}...")
        
        # Увеличиваем счетчик запросов
        increment_gemini_count()
        
        # Парсим JSON
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            results = data.get("results", [])
            
            # Сопоставляем результаты с сигналами
            result_dict = {}
            for res in results:
                idx = res.get("index") - 1
                if 0 <= idx < len(signals):
                    signal, _ = signals[idx]
                    signal_id = signal.signal_id
                    
                    # Проверяем seller и buyer
                    seller_ready = res.get("seller_ready", True)
                    buyer_ready = res.get("buyer_ready", True)
                    
                    # Если оба готовы - сигнал проходит
                    if seller_ready and buyer_ready:
                        result_dict[signal_id] = {
                            "passed": True,
                            "seller_ready": seller_ready,
                            "buyer_ready": buyer_ready
                        }
                    else:
                        result_dict[signal_id] = {
                            "passed": False,
                            "seller_ready": seller_ready,
                            "buyer_ready": buyer_ready
                        }
                    
                    # Сохраняем в кэш
                    seller_cache_key = f"{signal.seller.item_id}_{signal.seller.merchant_name}"
                    buyer_cache_key = f"{signal.buyer.item_id}_{signal.buyer.merchant_name}"
                    gemini_cache[seller_cache_key] = {"third_party_ready": seller_ready}
                    gemini_cache[buyer_cache_key] = {"third_party_ready": buyer_ready}
            
            logger.info(f"✅ Пакетный анализ завершен: {len(result_dict)} сигналов")
            return result_dict
        else:
            logger.warning(f"⚠️ Не удалось распарсить ответ Gemini: {result_text[:200]}")
            return {}
            
    except Exception as e:
        logger.error(f"❌ Ошибка при пакетном анализе Gemini: {e}")
        return {}

# ========== КЛАСС BYBIT P2P КЛИЕНТ ==========

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
                    third_party_ready=True
                )
                offers.append(offer)
            except (ValueError, KeyError) as e:
                logger.warning(f"Ошибка парсинга объявления: {e}")
                continue
        
        logger.info(f"Получено {len(offers)} объявлений для {side}")
        return offers

# ========== ОСНОВНОЙ КЛАСС БОТА ==========

class P2PArbitrageBot:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.is_running = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.bybit_client = None
        self._stop_requested = False
        
        if BYBIT_API_KEY and BYBIT_API_SECRET:
            self.bybit_client = BybitP2PClient(BYBIT_API_KEY, BYBIT_API_SECRET)
            logger.info("✅ Bybit клиент инициализирован")
        else:
            logger.warning("⚠️ Bybit клиент не инициализирован (нет API ключей)")
    
    async def start(self):
        self.is_running = True
        self._stop_requested = False
        self.monitor_task = asyncio.create_task(self._monitor_loop())
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
        logger.info("Бот остановлен")
    
    def _fetch_p2p_offers_sync(self, side: str) -> List[P2POffer]:
        if not self.bybit_client:
            return []
        try:
            return self.bybit_client.get_online_ads(side, page=1, size=50)
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
                    return False, f"Найдено запрещенное слово '{word}'"
        
        if filters.get("exact_amount"):
            if not (offer.min_amount <= filters["exact_amount"] <= offer.max_amount):
                return False, f"Сумма не входит в лимиты"
        
        if filters.get("min_amount"):
            if offer.max_amount < filters["min_amount"]:
                return False, f"Макс. сумма < {filters['min_amount']:.0f}₽"
        
        if filters.get("max_amount"):
            if offer.min_amount > filters["max_amount"]:
                return False, f"Мин. сумма > {filters['max_amount']:.0f}₽"
        
        return True, "OK"
    
    def _generate_profile_url(self, user_mask_id: str) -> str:
        if not user_mask_id or user_mask_id == "0" or user_mask_id == "":
            return "Ссылка недоступна"
        return f"https://www.bybit.com/ru-RU/p2p/profile/{user_mask_id}/USDT/RUB/item"
    
    def _find_best_signals(self, sellers: List[P2POffer], buyers: List[P2POffer],
                           filters: Dict, max_signals: int = 10) -> List[ArbitrageSignal]:
        """Находит лучшие сигналы без проверки Gemini"""
        if not sellers or not buyers:
            return []
        
        filtered_sellers = []
        for seller in sellers:
            passes, _ = self._check_offer_conditions(seller, filters)
            if passes:
                filtered_sellers.append(seller)
        
        filtered_buyers = []
        for buyer in buyers:
            passes, _ = self._check_offer_conditions(buyer, filters)
            if passes:
                filtered_buyers.append(buyer)
        
        if not filtered_sellers or not filtered_buyers:
            return []
        
        filtered_sellers.sort(key=lambda x: x.price)
        filtered_buyers.sort(key=lambda x: x.price, reverse=True)
        
        signals = []
        min_spread = filters.get("min_spread", 0.5)
        
        for seller in filtered_sellers[:15]:
            for buyer in filtered_buyers[:15]:
                if seller.price >= buyer.price:
                    continue
                
                spread = ((buyer.price / seller.price) - 1) * 100
                if spread < min_spread:
                    continue
                
                max_trade_amount = min(seller.max_amount, buyer.max_amount)
                min_trade_amount = max(seller.min_amount, buyer.min_amount)
                
                if max_trade_amount < min_trade_amount:
                    continue
                
                if filters.get("min_amount") and max_trade_amount < filters["min_amount"]:
                    continue
                
                if filters.get("max_amount") and min_trade_amount > filters["max_amount"]:
                    continue
                
                trade_amount = max_trade_amount
                usdt_amount = trade_amount / seller.price if seller.price > 0 else 0
                profit_per_usdt = buyer.price - seller.price
                total_profit_rub = usdt_amount * profit_per_usdt
                
                signal_id = f"{seller.item_id}_{buyer.item_id}_{seller.price}_{buyer.price}"
                
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
        return signals[:max_signals]
    
    async def _refill_verified_queue(self, user_id: int):
        """Пополняет очередь проверенных сигналов"""
        # Проверяем, не идет ли уже анализ
        if user_analysis_in_progress.get(user_id, False):
            logger.info(f"⏳ Анализ уже идет для пользователя {user_id}")
            return
        
        # Проверяем, нужно ли пополнять очередь
        queue = user_verified_signals_queue.get(user_id, deque())
        if len(queue) >= 3:
            logger.info(f"📦 В очереди {len(queue)} сигналов, пополнение не требуется")
            return
        
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        
        # Получаем свежие объявления
        sellers = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_p2p_offers_sync, "SELL"
        )
        buyers = await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_p2p_offers_sync, "BUY"
        )
        
        if not sellers or not buyers:
            return
        
        filters = user_filters.get(user_id, {})
        
        # Если Gemini выключен - просто добавляем сигналы без проверки
        if not gemini_enabled or not gemini_client:
            signals = self._find_best_signals(sellers, buyers, filters, max_signals=5)
            
            if signals:
                queue = user_verified_signals_queue.get(user_id, deque())
                for signal in signals:
                    if signal.signal_id not in sent_signals.get(user_id, {}):
                        queue.append((signal, True))
                user_verified_signals_queue[user_id] = queue
                logger.info(f"✅ Добавлено {len(signals)} сигналов в очередь (Gemini выключен)")
            return
        
        # Если Gemini включен - делаем пакетный анализ
        user_analysis_in_progress[user_id] = True
        
        try:
            # Находим топ-5 потенциальных сигналов
            candidates = self._find_best_signals(sellers, buyers, filters, max_signals=5)
            
            if not candidates:
                logger.info(f"ℹ️ Нет потенциальных сигналов для пользователя {user_id}")
                return
            
            # Проверяем кэш
            signals_to_analyze = []
            signals_already_checked = []
            
            for signal in candidates:
                seller_cache_key = f"{signal.seller.item_id}_{signal.seller.merchant_name}"
                buyer_cache_key = f"{signal.buyer.item_id}_{signal.buyer.merchant_name}"
                
                if seller_cache_key in gemini_cache and buyer_cache_key in gemini_cache:
                    seller_ready = gemini_cache[seller_cache_key].get("third_party_ready", True)
                    buyer_ready = gemini_cache[buyer_cache_key].get("third_party_ready", True)
                    if seller_ready and buyer_ready:
                        signals_already_checked.append(signal)
                    continue
                
                signals_to_analyze.append(signal)
            
            # Если есть сигналы для анализа - отправляем пакетный запрос
            if signals_to_analyze:
                batch_signals = [(s, user_id) for s in signals_to_analyze]
                results = await analyze_signals_batch(batch_signals, user_id)
                
                for signal in signals_to_analyze:
                    if signal.signal_id in results:
                        result = results[signal.signal_id]
                        if result.get("passed", False):
                            signals_already_checked.append(signal)
            
            # Добавляем проверенные сигналы в очередь
            queue = user_verified_signals_queue.get(user_id, deque())
            added_count = 0
            
            for signal in signals_already_checked:
                if signal.signal_id not in sent_signals.get(user_id, {}):
                    queue.append((signal, True))
                    added_count += 1
            
            user_verified_signals_queue[user_id] = queue
            logger.info(f"✅ Очередь пополнена: +{added_count} сигналов (всего {len(queue)})")
            
        except Exception as e:
            logger.error(f"❌ Ошибка при пополнении очереди: {e}")
        finally:
            user_analysis_in_progress[user_id] = False
    
    async def _monitor_loop(self):
        """Основной цикл мониторинга"""
        while self.is_running and not self._stop_requested:
            try:
                if not self.bybit_client:
                    await asyncio.sleep(30)
                    continue
                
                active_users = [
                    user_id for user_id, is_active in user_subscriptions.items()
                    if is_active and not self._stop_requested
                ]
                
                if not active_users:
                    await asyncio.sleep(15)
                    continue
                
                for user_id in active_users:
                    if not self.is_running or self._stop_requested:
                        return
                    
                    if not user_subscriptions.get(user_id, False):
                        continue
                    
                    # Пополняем очередь сигналов
                    await self._refill_verified_queue(user_id)
                    
                    # Берем сигнал из очереди и отправляем
                    queue = user_verified_signals_queue.get(user_id, deque())
                    
                    if queue:
                        signal, verified = queue.popleft()
                        user_verified_signals_queue[user_id] = queue
                        
                        await self._send_signal(user_id, signal)
                        if user_id not in sent_signals:
                            sent_signals[user_id] = {}
                        sent_signals[user_id][signal.signal_id] = datetime.now()
                        
                        delay = user_signal_delay.get(user_id, 4)
                        logger.info(f"📤 Отправлен сигнал: {signal.seller.merchant_name} -> {signal.buyer.merchant_name}")
                        await asyncio.sleep(delay)
                    else:
                        logger.info(f"ℹ️ Очередь пуста для пользователя {user_id}")
                    
                    await asyncio.sleep(1)
                
                await asyncio.sleep(10)
                
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
        
        logger.info(f"📝 SIGNAL for user {user_id}:")
        logger.info(f"   SELLER: {signal.seller.merchant_name}")
        logger.info(f"   REMARK: {signal.seller.remark[:200]}..." if signal.seller.remark and len(signal.seller.remark) > 200 else f"   REMARK: {signal.seller.remark}")
        logger.info(f"   BUYER: {signal.buyer.merchant_name}")
        logger.info(f"   REMARK: {signal.buyer.remark[:200]}..." if signal.buyer.remark and len(signal.buyer.remark) > 200 else f"   REMARK: {signal.buyer.remark}")
        
        seller_third_party = "✅ Готов к платежам от 3-их лиц" if signal.seller.third_party_ready else "❌ Не готов к платежам от 3-их лиц"
        buyer_third_party = "✅ Готов к платежам от 3-их лиц" if signal.buyer.third_party_ready else "❌ Не готов к платежам от 3-их лиц"
        
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
            await self.bot.send_message(user_id, message, parse_mode=None, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Ошибка отправки сигнала: {e}")
    
    async def get_filter_settings(self, user_id: int) -> str:
        filters = user_filters.get(user_id, {})
        delay = user_signal_delay.get(user_id, 4)
        gemini_enabled = gemini_enabled_for_user.get(user_id, False)
        cache_size = len(gemini_cache)
        queue_size = len(user_verified_signals_queue.get(user_id, deque()))
        
        remaining_minute = GEMINI_RATE_LIMIT_PER_MINUTE - GEMINI_REQUEST_COUNT_MINUTE
        remaining_day = GEMINI_RATE_LIMIT_PER_DAY - GEMINI_REQUEST_COUNT_DAY
        
        if not filters:
            return f"""🔧 Фильтры не настроены.

⏱ Задержка: {delay}с
🤖 Gemini: {'✅ Включен' if gemini_enabled else '❌ Выключен'}
📦 Сигналов в очереди: {queue_size}
💾 Кэш: {cache_size} записей

📊 Квота Gemini:
• В минуту: {GEMINI_REQUEST_COUNT_MINUTE}/{GEMINI_RATE_LIMIT_PER_MINUTE} (осталось {remaining_minute})
• В день: {GEMINI_REQUEST_COUNT_DAY}/{GEMINI_RATE_LIMIT_PER_DAY} (осталось {remaining_day})"""
        
        settings = ["📋 <b>Текущие настройки:</b>", ""]
        
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
        settings.append(f"⏱ Задержка: {delay}с")
        settings.append(f"🤖 Gemini: {'✅ Включен' if gemini_enabled else '❌ Выключен'}")
        settings.append(f"📦 Сигналов в очереди: {queue_size}")
        settings.append(f"💾 Кэш: {cache_size} записей")
        settings.append("")
        settings.append(f"📊 <b>Квота Gemini:</b>")
        settings.append(f"• В минуту: {GEMINI_REQUEST_COUNT_MINUTE}/{GEMINI_RATE_LIMIT_PER_MINUTE} (осталось {remaining_minute})")
        settings.append(f"• В день: {GEMINI_REQUEST_COUNT_DAY}/{GEMINI_RATE_LIMIT_PER_DAY} (осталось {remaining_day})")
        
        return "\n".join(settings)


# ========== НАСТРОЙКА КОМАНД БОТА ==========

async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="settings", description="📋 Показать настройки"),
        BotCommand(command="status", description="📊 Статус мониторинга"),
        BotCommand(command="start_monitoring", description="▶️ Запустить мониторинг"),
        BotCommand(command="stop_monitoring", description="⏹ Остановить мониторинг"),
        BotCommand(command="delay", description="⏱ Задержка между сигналами (сек)"),
        BotCommand(command="clear_filters", description="🧹 Очистить все фильтры"),
        BotCommand(command="gemini_on", description="🤖 Включить анализ 3-их лиц"),
        BotCommand(command="gemini_off", description="🤖 Выключить анализ 3-их лиц"),
        BotCommand(command="help", description="❓ Помощь"),
    ]
    await bot.set_my_commands(commands)
    logger.info("✅ Меню команд установлено")


# ========== ИНИЦИАЛИЗАЦИЯ БОТА ==========

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
arbitrage_bot = P2PArbitrageBot(bot)

# ========== ОБРАБОТЧИКИ КОМАНД ==========

@dp.message(Command("start"))
async def cmd_start(message: Message):
    welcome_text = """
🚀 Добро пожаловать в P2P Арбитраж Бот!

<b>Доступные команды:</b>
/help - Настройка фильтров
/settings - Текущие настройки
/status - Статус мониторинга
/start_monitoring - Запустить мониторинг
/stop_monitoring - Остановить мониторинг
/delay - Задержка между сигналами
/clear_filters - Очистить все фильтры
/gemini_on - Включить анализ 3-их лиц
/gemini_off - Выключить анализ 3-их лиц

<b>⚡ Оптимизация квоты Gemini:</b>
• Пакетный анализ: 1 запрос = 3-5 сигналов
• Кэширование результатов
• Очередь проверенных сигналов
• Экономия квоты до 80%
    """
    await safe_send_message(message, welcome_text)
    
    if message.from_user.id not in user_filters:
        user_filters[message.from_user.id] = {}
        user_subscriptions[message.from_user.id] = False
        sent_signals[message.from_user.id] = {}
        user_signal_delay[message.from_user.id] = 4
        gemini_enabled_for_user[message.from_user.id] = False
        user_verified_signals_queue[message.from_user.id] = deque()
        user_analysis_in_progress[message.from_user.id] = False

@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = """
📖 <b>Помощь по фильтрам</b>

1. <b>Сумма сделки</b>
   /set_exact 28000 - строго 28 000 ₽
   /set_min 25000 - минимум 25 000 ₽
   /set_max 30000 - максимум 30 000 ₽

2. <b>Черный список</b>
   /add_blacklist "Имя" - добавить мерчанта
   /remove_blacklist "Имя" - удалить

3. <b>Спред</b>
   /set_spread 0.5 - минимальный спред 0.5%

4. <b>Задержка</b>
   /delay 5 - задержка 5 секунд

5. <b>Управление</b>
   /start_monitoring - запуск
   /stop_monitoring - остановка
   /status - текущий статус
   /clear_filters - очистить все фильтры

6. <b>🤖 Gemini (3-и лица)</b>
   /gemini_on - Включить проверку 3-их лиц
   /gemini_off - Выключить проверку 3-их лиц

<b>⚡ Оптимизация:</b>
• 1 запрос = 3-5 сигналов
• Кэширование результатов
• Очередь проверенных сигналов
• Экономия квоты до 80%
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
        await safe_send_message(message, "⚠️ Сначала настройте фильтры через /help")
        return
    
    sent_signals[user_id] = {}
    user_verified_signals_queue[user_id] = deque()
    user_analysis_in_progress[user_id] = False
    user_subscriptions[user_id] = True
    
    delay = user_signal_delay.get(user_id, 4)
    gemini_enabled = gemini_enabled_for_user.get(user_id, False)
    
    await safe_send_message(
        message,
        f"✅ Мониторинг запущен!\n"
        f"⏱ Задержка: {delay}с\n"
        f"🤖 Gemini: {'Включен' if gemini_enabled else 'Выключен'}\n"
        f"⚡ 1 запрос Gemini = 3-5 сигналов"
    )

@dp.message(Command("stop_monitoring"))
async def cmd_stop_monitoring(message: Message):
    user_id = message.from_user.id
    user_subscriptions[user_id] = False
    sent_signals[user_id] = {}
    user_verified_signals_queue[user_id] = deque()
    
    await safe_send_message(message, "⏹ Мониторинг остановлен.")

@dp.message(Command("delay"))
async def cmd_delay(message: Message):
    try:
        args = parse_args_with_quotes(message.text)
        if len(args) != 2:
            await safe_send_message(message, "❌ Использование: /delay <секунды>\nПример: /delay 5")
            return
        
        delay = float(args[1])
        if delay < 1 or delay > 60:
            await safe_send_message(message, "❌ Задержка должна быть от 1 до 60 секунд")
            return
        
        user_id = message.from_user.id
        user_signal_delay[user_id] = delay
        
        await safe_send_message(message, f"⏱ Задержка установлена: {delay}с")
    except ValueError:
        await safe_send_message(message, "❌ Введите корректное число")

@dp.message(Command("clear_filters"))
async def cmd_clear_filters(message: Message):
    user_id = message.from_user.id
    user_filters[user_id] = {}
    user_subscriptions[user_id] = False
    sent_signals[user_id] = {}
    user_verified_signals_queue[user_id] = deque()
    
    await safe_send_message(message, "🧹 Все фильтры очищены. Мониторинг остановлен.")

@dp.message(Command("gemini_on"))
async def cmd_gemini_on(message: Message):
    user_id = message.from_user.id
    
    if not gemini_client:
        await safe_send_message(message, "❌ Gemini недоступен! Проверьте API ключ.")
        return
    
    gemini_enabled_for_user[user_id] = True
    global gemini_cache
    gemini_cache = {}
    user_verified_signals_queue[user_id] = deque()
    
    await safe_send_message(
        message,
        "🤖 Gemini ВКЛЮЧЕН!\n\n"
        "Теперь бот будет проверять готовность к 3-им лицам.\n"
        "⚡ Пакетный режим: 1 запрос = 3-5 сигналов\n"
        "📦 Результаты кэшируются\n"
        "📊 Экономия квоты до 80%"
    )

@dp.message(Command("gemini_off"))
async def cmd_gemini_off(message: Message):
    user_id = message.from_user.id
    gemini_enabled_for_user[user_id] = False
    user_verified_signals_queue[user_id] = deque()
    
    await safe_send_message(message, "🤖 Gemini ВЫКЛЮЧЕН!")

# ========== КОМАНДЫ НАСТРОЙКИ ФИЛЬТРОВ ==========

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
        await safe_send_message(message, "❌ Использование: /add_blacklist <ник>")
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
        await safe_send_message(message, f"⚠️ Слово '{word}' не найдено")


# ========== ЗАПУСК БОТА ==========

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
