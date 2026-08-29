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
import html

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, BotCommand
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования - более подробный уровень
logging.basicConfig(
    level=logging.DEBUG,  # Меняем на DEBUG для более детальных логов
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не найден в переменных окружения")

if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    logger.warning("⚠️ API ключи Bybit не найдены! Бот будет работать в ограниченном режиме.")

# Хранилище для фильтров пользователей
user_filters: Dict[int, Dict] = {}
user_subscriptions: Dict[int, bool] = {}

# Хранилище для отправленных сигналов (с временем)
sent_signals: Dict[int, Dict[str, datetime]] = {}

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
    item_id: str
    user_id: str
    user_mask_id: str
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
        
        # Логируем первый элемент для отладки
        if items:
            logger.debug(f"Первый элемент ответа: {json.dumps(items[0], indent=2, ensure_ascii=False)[:500]}")
        
        offers = []
        for idx, item in enumerate(items):
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
                
                # ПРОВЕРЯЕМ ВСЕ ВОЗМОЖНЫЕ ПОЛЯ ДЛЯ ВЕРИФИКАЦИИ
                is_verified = False
                
                # Проверяем разные варианты названий поля
                if "isVerified" in item:
                    is_verified = bool(item.get("isVerified", False))
                    logger.debug(f"Мерчант #{idx} {item.get('nickName', 'Unknown')}: isVerified={is_verified} (из isVerified)")
                elif "verified" in item:
                    is_verified = bool(item.get("verified", False))
                    logger.debug(f"Мерчант #{idx} {item.get('nickName', 'Unknown')}: isVerified={is_verified} (из verified)")
                elif "isVerifiedMerchant" in item:
                    is_verified = bool(item.get("isVerifiedMerchant", False))
                    logger.debug(f"Мерчант #{idx} {item.get('nickName', 'Unknown')}: isVerified={is_verified} (из isVerifiedMerchant)")
                else:
                    # Если нет поля, пытаемся найти по другим признакам
                    # Например, может быть поле userStatus или другие
                    user_status = item.get("userStatus", "")
                    if user_status and "verified" in str(user_status).lower():
                        is_verified = True
                        logger.debug(f"Мерчант #{idx} {item.get('nickName', 'Unknown')}: isVerified=True (из userStatus={user_status})")
                    else:
                        # Проверяем наличие других полей, которые могут указывать на верификацию
                        is_merchant = item.get("isMerchant", False)
                        if is_merchant:
                            # Если это мерчант, скорее всего верифицирован
                            is_verified = True
                            logger.debug(f"Мерчант #{idx} {item.get('nickName', 'Unknown')}: isVerified=True (isMerchant=True)")
                        else:
                            logger.debug(f"Мерчант #{idx} {item.get('nickName', 'Unknown')}: isVerified=False (поля не найдены)")
                
                # Дополнительное логирование всех полей для первого мерчанта
                if idx == 0:
                    logger.info(f"Доступные поля в ответе: {list(item.keys())}")
                    logger.info(f"Значение isVerified: {item.get('isVerified', 'НЕТ ТАКОГО ПОЛЯ')}")
                
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
                    is_verified=bool(is_verified),
                    item_id=item_id,
                    user_id=user_id,
                    user_mask_id=user_mask_id
                )
                offers.append(offer)
            except (ValueError, KeyError) as e:
                logger.warning(f"Ошибка парсинга объявления #{idx}: {e}")
                continue
        
        logger.info(f"Получено {len(offers)} объявлений для {side}")
        logger.info(f"Из них верифицированных: {sum(1 for o in offers if o.is_verified)}")
        return offers

class P2PArbitrageBot:
    """Основной класс бота для P2P арбитража"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.is_running = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.bybit_client = None
        
        if BYBIT_API_KEY and BYBIT_API_SECRET:
            self.bybit_client = BybitP2PClient(BYBIT_API_KEY, BYBIT_API_SECRET)
            logger.info("✅ Bybit клиент инициализирован")
        else:
            logger.warning("⚠️ Bybit клиент не инициализирован (нет API ключей)")
        
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
        
        # Проверка верификации
        if filters.get("only_verified", False) and not offer.is_verified:
            return False, f"Мерчант {offer.merchant_name} не верифицирован (is_verified={offer.is_verified})"
        
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
        
        # Проверка текстовых условий
        description_lower = offer.description.lower()
        
        for word in filters.get("blacklist", []):
            if word.lower() in description_lower:
                return False, f"Найдено запрещенное слово: {word}"
        
        whitelist = filters.get("whitelist", [])
        if whitelist:
            found = any(word.lower() in description_lower for word in whitelist)
            if not found:
                return False, f"Нет обязательных слов из: {', '.join(whitelist)}"
        
        if filters.get("payment_methods"):
            offer_methods = [m.lower() for m in offer.payment_methods]
            required = [m.lower() for m in filters["payment_methods"]]
            if not any(m in offer_methods for m in required):
                return False, f"Нет доступных платежных систем: {', '.join(filters['payment_methods'])}"
        
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
    
    def _find_all_arbitrage_signals(self, sellers: List[P2POffer], buyers: List[P2POffer],
                                     user_filters: Dict) -> List[ArbitrageSignal]:
        """Находит ВСЕ арбитражные связки"""
        if not sellers or not buyers:
            return []
        
        # Фильтруем продавцов и покупателей по условиям
        filtered_sellers = []
        for seller in sellers:
            passes, reason = self._check_offer_conditions(seller, user_filters)
            if passes:
                filtered_sellers.append(seller)
            else:
                logger.debug(f"Seller {seller.merchant_name} пропущен: {reason}")
        
        filtered_buyers = []
        for buyer in buyers:
            passes, reason = self._check_offer_conditions(buyer, user_filters)
            if passes:
                filtered_buyers.append(buyer)
            else:
                logger.debug(f"Buyer {buyer.merchant_name} пропущен: {reason}")
        
        if not filtered_sellers or not filtered_buyers:
            logger.info(f"После фильтрации: sellers={len(filtered_sellers)}, buyers={len(filtered_buyers)}")
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
        while self.is_running:
            try:
                if not self.bybit_client:
                    logger.warning("Пропуск цикла: Bybit клиент не инициализирован")
                    await asyncio.sleep(30)
                    continue
                
                for user_id, filters in user_filters.items():
                    if not user_subscriptions.get(user_id, False):
                        continue
                    
                    self._clean_old_signals(user_id)
                    
                    sellers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "SELL"
                    )
                    buyers = await asyncio.get_event_loop().run_in_executor(
                        None, self._fetch_p2p_offers_sync, "BUY"
                    )
                    
                    if not sellers or not buyers:
                        continue
                    
                    signals = self._find_all_arbitrage_signals(sellers, buyers, filters)
                    
                    if signals:
                        logger.info(f"Найдено {len(signals)} сигналов для пользователя {user_id}")
                        
                        if user_id not in sent_signals:
                            sent_signals[user_id] = {}
                        
                        sent_count = 0
                        skipped_count = 0
                        
                        for signal in signals[:30]:
                            if signal.signal_id not in sent_signals[user_id]:
                                await self._send_signal(user_id, signal)
                                sent_signals[user_id][signal.signal_id] = datetime.now()
                                sent_count += 1
                                logger.info(f"Отправлен сигнал #{sent_count}: SELL={signal.seller.merchant_name} (verified={signal.seller.is_verified}) {signal.seller.price:.2f}₽, BUY={signal.buyer.merchant_name} (verified={signal.buyer.is_verified}) {signal.buyer.price:.2f}₽, прибыль={signal.profit_rub:.2f}₽")
                                await asyncio.sleep(4)
                            else:
                                skipped_count += 1
                        
                        if sent_count > 0:
                            logger.info(f"Отправлено {sent_count} новых сигналов пользователю {user_id} (пропущено {skipped_count} дубликатов)")
                        else:
                            logger.info(f"Новых сигналов нет для пользователя {user_id} (все {len(signals)} уже отправлены)")
                
                await asyncio.sleep(15)
                
            except asyncio.CancelledError:
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
        
        # Определяем статус верификации
        seller_verified = "✅" if signal.seller.is_verified else "❌"
        buyer_verified = "✅" if signal.buyer.is_verified else "❌"
        
        # Формируем сообщение
        message = f"""🔥 АРБИТРАЖНЫЙ СИГНАЛ 🔥

🟢 ПРОДАВЕЦ (SELLER)
• Курс: {signal.seller.price:.2f}₽
• Лимиты: {format_number(signal.seller.min_amount)} - {format_number(signal.seller.max_amount)}₽
• Мерчант: {signal.seller.merchant_name} {seller_verified}
• Ссылка на профиль: {seller_profile_url}

🔴 ПОКУПАТЕЛЬ (BUYER)
• Курс: {signal.buyer.price:.2f}₽
• Лимиты: {format_number(signal.buyer.min_amount)} - {format_number(signal.buyer.max_amount)}₽
• Мерчант: {signal.buyer.merchant_name} {buyer_verified}
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
        if not filters:
            return "🔧 Фильтры не настроены. Используйте /help для настройки."
        
        settings = []
        settings.append("📋 <b>Текущие настройки фильтров:</b>")
        settings.append("")
        
        if filters.get("only_verified"):
            settings.append("• ✅ Только верифицированные мерчанты")
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


# Функция для установки команд бота
async def set_bot_commands(bot: Bot):
    """Устанавливает меню команд для бота"""
    commands = [
        BotCommand(command="settings", description="📋 Показать текущие настройки фильтров"),
        BotCommand(command="status", description="📊 Статус мониторинга"),
        BotCommand(command="start_monitoring", description="▶️ Запустить мониторинг арбитража"),
        BotCommand(command="stop_monitoring", description="⏹ Остановить мониторинг"),
        BotCommand(command="clear_filters", description="🧹 Очистить все фильтры"),
        BotCommand(command="help", description="❓ Помощь по настройке фильтров"),
        BotCommand(command="only_verified", description="✅ Только верифицированные мерчанты"),
        BotCommand(command="start", description="🚀 Главное меню"),
    ]
    await bot.set_my_commands(commands)
    logger.info("✅ Меню команд установлено")


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
        logger.warning(f"Ошибка HTML-парсинга, отправляем обычный текст: {e}")
        await message.answer(text.replace('<', '[').replace('>', ']'))

# --- Обработчики команд ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start"""
    welcome_text = """
🚀 Добро пожаловать в P2P Арбитраж Бот!

Я ищу арбитражные связки на Bybit P2P и присылаю тебе сигналы.

<b>Доступные команды:</b>
/start - Показать это сообщение
/settings - Настройка фильтров
/status - Статус мониторинга
/start_monitoring - Запустить мониторинг
/stop_monitoring - Остановить мониторинг
/clear_filters - Очистить все фильтры
/only_verified - Только верифицированные мерчанты
/help - Помощь

<b>Как это работает:</b>
1. Настрой фильтры через /settings
2. Запусти мониторинг /start_monitoring
3. Бот будет искать выгодные связки
4. При найденной связке получишь сигнал со ссылками на профили
    """
    await safe_send_message(message, welcome_text)
    
    if message.from_user.id not in user_filters:
        user_filters[message.from_user.id] = {}
        user_subscriptions[message.from_user.id] = False
        sent_signals[message.from_user.id] = {}

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

2. <b>Черный список (исключаем)</b>
   /add_blacklist СБП - НЕ показывать объявления со словом "СБП"
   /remove_blacklist СБП - убрать из черного списка

3. <b>Белый список (только эти)</b>
   /add_whitelist Т-Банк - ПОКАЗЫВАТЬ только объявления со словом "Т-Банк"
   /remove_whitelist Т-Банк - убрать из белого списка

4. <b>Спред</b>
   /set_spread 0.5 - минимальный спред 0.5%

5. <b>Верификация</b>
   /only_verified - показывать только верифицированных мерчантов

6. <b>Управление</b>
   /start_monitoring - запуск поиска
   /stop_monitoring - остановка поиска
   /status - текущий статус
   /clear_filters - очистить все фильтры

<b>Пример настройки:</b>
1. /set_min 500
2. /set_max 10000
3. /set_spread 0.5
4. /add_blacklist СБП
5. /add_whitelist Т-Банк
6. /only_verified
7. /start_monitoring

<b>Как работают списки:</b>
• <b>Черный список</b> - запрещает показывать объявления с этими словами
  Пример: если добавить "СБП", бот пропустит все объявления где есть "СБП"

• <b>Белый список</b> - разрешает показывать ТОЛЬКО объявления с этими словами
  Пример: если добавить "Т-Банк", бот покажет только объявления с "Т-Банк"
  
<b>Важно!</b> Если белый список пуст - бот показывает всё, кроме черного списка.
Если белый список не пуст - бот показывает ТОЛЬКО то, что есть в белом списке.
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
            "Используйте /settings для просмотра и /help для инструкций."
        )
        return
    
    sent_signals[user_id] = {}
    
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
    sent_signals[user_id] = {}
    await safe_send_message(message, "🧹 Все фильтры очищены. Мониторинг остановлен.")

@dp.message(Command("only_verified"))
async def cmd_only_verified(message: Message):
    """Включить фильтр только верифицированных мерчантов"""
    user_id = message.from_user.id
    if user_id not in user_filters:
        user_filters[user_id] = {}
    
    # Переключаем фильтр
    current = user_filters[user_id].get("only_verified", False)
    user_filters[user_id]["only_verified"] = not current
    
    status = "включен" if user_filters[user_id]["only_verified"] else "выключен"
    await safe_send_message(
        message, 
        f"✅ Фильтр 'Только верифицированные мерчанты' {status}.\n"
        f"Теперь бот будет {'показывать только' if user_filters[user_id]['only_verified'] else 'показывать всех'} верифицированных мерчантов."
    )

# --- Команды для настройки фильтров ---@dp.message(Command("set_exact"))
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
        await safe_send_message(
            message, 
            "❌ Использование: /add_blacklist <слово>\n"
            "Пример: /add_blacklist СБП\n\n"
            "Это исключит все объявления, где есть слово 'СБП'"
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
            f"Теперь бот НЕ будет показывать объявления с этим словом"
        )
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
        await safe_send_message(
            message,
            "❌ Использование: /add_whitelist <слово>\n"
            "Пример: /add_whitelist Т-Банк\n\n"
            "Теперь бот будет ПОКАЗЫВАТЬ только объявления с этим словом"
        )
        return
    
    word = args[1]
    user_id = message.from_user.id
    if user_id not in user_filters:
        user_filters[user_id] = {}
    
    if "whitelist" not in user_filters[user_id]:
        user_filters[user_id]["whitelist"] = []
    
    if word not in user_filters[user_id]["whitelist"]:
        user_filters[user_id]["whitelist"].append(word)
        await safe_send_message(
            message,
            f"✅ Добавлено в БЕЛЫЙ список: {word}\n"
            f"Теперь бот будет ПОКАЗЫВАТЬ только объявления с этим словом"
        )
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
