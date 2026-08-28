import os
import requests
import json
import time
import hashlib
import hmac
import logging
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import asyncio

# --- 1. Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. Получение переменных окружения ---
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    logger.error("❌ API ключи Bybit не найдены!")
    exit()

if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN не найден!")
    exit()

# --- 3. Класс для работы с Bybit API ---

class BybitP2PClient:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.bybit.com"
        self.session = requests.Session()
        
    def _sign_request(self, endpoint, params):
        """Подпись запроса согласно документации Bybit"""
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        
        json_body = json.dumps(params, separators=(',', ':'))
        sign_str = timestamp + self.api_key + recv_window + json_body
        
        signature = hmac.new(
            bytes(self.api_secret, "utf-8"),
            bytes(sign_str, "utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json"
        }
        
        return headers
    
    def get_p2p_orders(self, side="BUY", coin="USDT", fiat="RUB", limit=50):
        """Получение P2P объявлений"""
        endpoint = "/v5/p2p/item/online"
        params = {
            "coinId": coin,
            "currencyId": fiat,
            "side": side,
            "page": "1",
            "size": str(limit)
        }
        
        try:
            headers = self._sign_request(endpoint, params)
            url = self.base_url + endpoint
            
            response = self.session.post(url, json=params, headers=headers, timeout=10)
            data = response.json()
            
            if data.get("retCode") == 0:
                return data.get("result", {}).get("items", [])
            else:
                logger.error(f"API Error: {data.get('retMsg')}")
                return []
                
        except Exception as e:
            logger.error(f"Request Error: {e}")
            return []

# --- 4. Класс для поиска связок ---

class P2PArbitrageFinder:
    def __init__(self, client):
        self.client = client
        self.payment_methods = {
            "14": "СБП",
            "18": "Банковский перевод",
            "40": "T-Bank",
            "90": "Сбербанк"
        }
    
    def parse_conditions(self, condition_text):
        """Парсинг условий мейкера из текста"""
        conditions = {
            "exact_amount": None,
            "pdf_required": False,
            "sbp_blocked": False,
            "support_24h": False,
            "min_amount": None,
            "max_amount": None
        }
        
        if not condition_text:
            return conditions
            
        lines = condition_text.lower().split('\n')
        
        for line in lines:
            # Поиск точной суммы
            if 'заходить строго на сумму' in line:
                try:
                    amount = line.replace('заходить строго на сумму', '').strip()
                    if 'k' in amount:
                        amount = amount.replace('k', '').strip()
                        conditions["exact_amount"] = float(amount) * 1000
                    else:
                        conditions["exact_amount"] = float(amount)
                except:
                    pass
            
            # Проверка условий
            if 'пдф напрямую из тбанка' in line:
                conditions["pdf_required"] = True
            if 'с т-банка на сбп' in line and '❌' in line:
                conditions["sbp_blocked"] = True
            if 'на связи круглосуточно' in line:
                conditions["support_24h"] = True
                
        return conditions
    
    def find_arbitrage_opportunities(self, min_spread=0.5, min_amount=1000, max_amount=10000, 
                                    payment_methods=None, conditions_filter=None):
        """
        Поиск арбитражных связок с фильтрацией по условиям
        """
        logger.info("🔍 Поиск арбитражных связок...")
        
        # Получаем объявления
        buy_orders = self.client.get_p2p_orders("BUY", limit=50)
        sell_orders = self.client.get_p2p_orders("SELL", limit=50)
        
        if not buy_orders or not sell_orders:
            logger.warning("❌ Не удалось получить объявления")
            return []
        
        opportunities = []
        
        # Поиск связок
        for buy in buy_orders:
            buy_price = float(buy.get("price", 0))
            if buy_price <= 0:
                continue
                
            # Проверка условий мейкера для BUY
            buy_conditions = self.parse_conditions(buy.get("memo", ""))
            if conditions_filter and not self._match_conditions(buy_conditions, conditions_filter):
                continue
                
            # Проверка платежных методов
            buy_payments = buy.get("paymentMethods", [])
            buy_payment_ids = [str(pm.get("id", "")) for pm in buy_payments]
            
            if payment_methods and not any(pm in buy_payment_ids for pm in payment_methods):
                continue
            
            for sell in sell_orders:
                sell_price = float(sell.get("price", 0))
                if sell_price <= 0:
                    continue
                    
                # Проверка условий мейкера для SELL
                sell_conditions = self.parse_conditions(sell.get("memo", ""))
                if conditions_filter and not self._match_conditions(sell_conditions, conditions_filter):
                    continue
                    
                # Проверка платежных методов для SELL
                sell_payments = sell.get("paymentMethods", [])
                sell_payment_ids = [str(pm.get("id", "")) for pm in sell_payments]
                
                if payment_methods and not any(pm in sell_payment_ids for pm in payment_methods):
                    continue
                
                # Проверка лимитов
                buy_min = float(buy.get("minAmount", 0))
                buy_max = float(buy.get("maxAmount", 0))
                sell_min = float(sell.get("minAmount", 0))
                sell_max = float(sell.get("maxAmount", 0))
                
                max_amount_possible = min(buy_max, sell_max)
                min_amount_possible = max(buy_min, sell_min, min_amount)
                
                if max_amount_possible < min_amount_possible or max_amount_possible < min_amount:
                    continue
                
                # Расчет спреда
                spread = ((sell_price - buy_price) / buy_price) * 100
                
                if spread >= min_spread:
                    # Расчет потенциальной прибыли
                    trade_amount = min(max_amount_possible, max_amount)
                    usdt_amount = trade_amount / buy_price
                    profit = trade_amount * (spread / 100)
                    
                    opportunity = {
                        "buy_price": buy_price,
                        "sell_price": sell_price,
                        "spread": spread,
                        "buy_seller": buy.get("advertiser", {}).get("nickName", "N/A"),
                        "sell_seller": sell.get("advertiser", {}).get("nickName", "N/A"),
                        "buy_conditions": buy_conditions,
                        "sell_conditions": sell_conditions,
                        "buy_payments": [self.payment_methods.get(str(pid), pid) for pid in buy_payment_ids],
                        "sell_payments": [self.payment_methods.get(str(pid), pid) for pid in sell_payment_ids],
                        "min_amount": min_amount_possible,
                        "max_amount": max_amount_possible,
                        "trade_amount": trade_amount,
                        "usdt_amount": usdt_amount,
                        "profit": profit,
                        "buy_order": buy,
                        "sell_order": sell
                    }
                    
                    opportunities.append(opportunity)
        
        # Сортировка по спреду
        opportunities.sort(key=lambda x: x["spread"], reverse=True)
        logger.info(f"✅ Найдено {len(opportunities)} связок")
        
        return opportunities[:10]  # Возвращаем топ-10
    
    def _match_conditions(self, order_conditions, filter_conditions):
        """Проверка соответствия условий фильтру"""
        if not filter_conditions:
            return True
            
        # Проверка точной суммы
        if filter_conditions.get("exact_amount"):
            if order_conditions.get("exact_amount") != filter_conditions["exact_amount"]:
                return False
                
        # Проверка PDF требования
        if filter_conditions.get("pdf_required") and not order_conditions.get("pdf_required"):
            return False
            
        # Проверка блокировки СБП
        if filter_conditions.get("sbp_blocked") and order_conditions.get("sbp_blocked"):
            return False
            
        return True

# --- 5. Telegram бот ---

class P2PBot:
    def __init__(self, token, api_key, api_secret):
        self.token = token
        self.client = BybitP2PClient(api_key, api_secret)
        self.finder = P2PArbitrageFinder(self.client)
        self.user_settings = {}  # Хранение настроек пользователей
        
    async def start(self, update, context):
        """Обработчик команды /start"""
        keyboard = [
            [InlineKeyboardButton("🔍 Найти связки", callback_data="find_opportunities")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🤖 *P2P Арбитраж Бот*\n\n"
            "Я помогаю находить выгодные связки на Bybit P2P.\n\n"
            "🔍 *Найти связки* - поиск арбитражных возможностей\n"
            "⚙️ *Настройки* - настройка фильтров и условий\n"
            "📊 *Статистика* - просмотр статистики\n\n"
            "Для начала настройте фильтры в разделе настроек!",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    async def settings(self, update, context):
        """Настройки бота"""
        keyboard = [
            [InlineKeyboardButton("💰 Минимальный спред", callback_data="set_spread")],
            [InlineKeyboardButton("💵 Диапазон суммы", callback_data="set_amount")],
            [InlineKeyboardButton("💳 Платежные методы", callback_data="set_payments")],
            [InlineKeyboardButton("📝 Условия мейкера", callback_data="set_conditions")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        user_id = update.effective_user.id
        settings = self.user_settings.get(user_id, {})
        
        settings_text = f"⚙️ *Текущие настройки:*\n\n"
        settings_text += f"💰 Минимальный спред: {settings.get('min_spread', 0.5)}%\n"
        settings_text += f"💵 Диапазон: {settings.get('min_amount', 1000)} - {settings.get('max_amount', 10000)} RUB\n"
        settings_text += f"💳 Платежки: {', '.join(settings.get('payment_methods', ['Все']))}\n"
        settings_text += f"📝 Условия мейкера: {'Включены' if settings.get('conditions_filter') else 'Выключены'}"
        
        if update.callback_query:
            await update.callback_query.message.edit_text(
                settings_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            await update.callback_query.answer()
        else:
            await update.message.reply_text(
                settings_text,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    
    async def find_opportunities(self, update, context):
        """Поиск арбитражных связок"""
        user_id = update.effective_user.id
        settings = self.user_settings.get(user_id, {})
        
        # Отправляем сообщение о поиске
        if update.callback_query:
            await update.callback_query.message.edit_text(
                "🔍 *Идет поиск связок...*\n\n"
                "Пожалуйста, подождите, это может занять несколько секунд.",
                parse_mode='Markdown'
            )
            await update.callback_query.answer()
        else:
            await update.message.reply_text(
                "🔍 Идет поиск связок...\nПожалуйста, подождите."
            )
        
        # Поиск связок
        opportunities = self.finder.find_arbitrage_opportunities(
            min_spread=settings.get('min_spread', 0.5),
            min_amount=settings.get('min_amount', 1000),
            max_amount=settings.get('max_amount', 10000),
            payment_methods=settings.get('payment_methods', None),
            conditions_filter=settings.get('conditions_filter', None)
        )
        
        # Форматирование результата
        if not opportunities:
            response = (
                "❌ *Связок не найдено*\n\n"
                f"💰 Минимальный спред: {settings.get('min_spread', 0.5)}%\n"
                f"💵 Диапазон: {settings.get('min_amount', 1000)} - {settings.get('max_amount', 10000)} RUB\n"
                "💡 Попробуйте уменьшить минимальный спред или изменить фильтры."
            )
        else:
            response = "✅ *Найденные связки:*\n\n"
            for i, opp in enumerate(opportunities[:5], 1):
                response += f"*#{i}* Спред: {opp['spread']:.2f}%\n"
                response += f"📈 BUY: {opp['buy_price']:.2f} RUB y {opp['buy_seller']}\n"
                response += f"📉 SELL: {opp['sell_price']:.2f} RUB y {opp['sell_seller']}\n"
                response += f"💵 Сумма: {opp['trade_amount']:.2f} RUB (~{opp['usdt_amount']:.2f} USDT)\n"
                response += f"💰 Прибыль: {opp['profit']:.2f} RUB\n"
                
                if opp.get('buy_payments'):
                    response += f"💳 Платежки BUY: {', '.join(opp['buy_payments'][:3])}\n"
                if opp.get('sell_payments'):
                    response += f"💳 Платежки SELL: {', '.join(opp['sell_payments'][:3])}\n"
                
                # Условия мейкера
                if opp['buy_conditions'].get('exact_amount'):
                    response += f"📝 Точная сумма: {opp['buy_conditions']['exact_amount']} RUB\n"
                if opp['buy_conditions'].get('pdf_required'):
                    response += f"📎 Требуется PDF из Т-Банка\n"
                    
                response += "\n" + "─" * 30 + "\n\n"
            
            response += f"\n📊 Всего найдено: {len(opportunities)} связок"
        
        # Отправка результата
        keyboard = [
            [InlineKeyboardButton("🔄 Обновить", callback_data="find_opportunities")],
            [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.message.edit_text(
                response,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
            await update.callback_query.answer()
        else:
            await update.message.reply_text(
                response,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    
    async def set_spread(self, update, context):
        """Установка минимального спреда"""
        await update.callback_query.message.edit_text(
            "💰 *Установка минимального спреда*\n\n"
            "Введите минимальный спред в процентах (например: 0.5):",
            parse_mode='Markdown'
        )
        await update.callback_query.answer()
        context.user_data['setting'] = 'spread'
    
    async def set_amount(self, update, context):
        """Установка диапазона суммы"""
        await update.callback_query.message.edit_text(
            "💵 *Установка диапазона суммы*\n\n"
            "Введите минимальную и максимальную сумму через пробел (например: 1000 10000):",
            parse_mode='Markdown'
        )
        await update.callback_query.answer()
        context.user_data['setting'] = 'amount'
    
    async def set_payments(self, update, context):
        """Настройка платежных методов"""
        keyboard = [
            [InlineKeyboardButton("✅ СБП", callback_data="pay_14")],
            [InlineKeyboardButton("✅ Банковский перевод", callback_data="pay_18")],
            [InlineKeyboardButton("✅ T-Bank", callback_data="pay_40")],
            [InlineKeyboardButton("✅ Сбербанк", callback_data="pay_90")],
            [InlineKeyboardButton("🗑️ Очистить все", callback_data="pay_clear")],
            [InlineKeyboardButton("🔙 Назад", callback_data="settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        user_id = update.effective_user.id
        settings = self.user_settings.get(user_id, {})
        selected = settings.get('payment_methods', [])
        
        text = "💳 *Выберите платежные методы:*\n\n"
        text += "✅ - метод выбран\n\n"
        text += f"Выбрано: {', '.join(selected) if selected else 'Ничего не выбрано'}"
        
        await update.callback_query.message.edit_text(
            text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        await update.callback_query.answer()
    
    async def set_conditions(self, update, context):
        """Настройка условий мейкера"""
        user_id = update.effective_user.id
        settings = self.user_settings.get(user_id, {})
        current = settings.get('conditions_filter', False)
        
        keyboard = [
            [InlineKeyboardButton(
                f"{'✅' if current else '❌'} Учитывать условия мейкера",
                callback_data="toggle_conditions"
            )],
            [InlineKeyboardButton("📝 Настроить условия", callback_data="edit_conditions")],
            [InlineKeyboardButton("🔙 Назад", callback_data="settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        text = "📝 *Условия мейкера*\n\n"
        text += "Условия мейкера - это дополнительные требования продавца.\n"
        text += "Например: точная сумма сделки, PDF из Т-Банка, и т.д.\n\n"
        text += f"Статус: {'Включены' if current else 'Выключены'}"
        
        await update.callback_query.message.edit_text(
            text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        await update.callback_query.answer()
    
    async def handle_input(self, update, context):
        """Обработка ввода пользователя"""
        user_id = update.effective_user.id
        setting = context.user_data.get('setting')
        
        if setting == 'spread':
            try:
                value = float(update.message.text)
                if value < 0 or value > 100:
                    await update.message.reply_text("❌ Спред должен быть от 0 до 100%")
                    return
                    
                self.user_settings.setdefault(user_id, {})['min_spread'] = value
                await update.message.reply_text(f"✅ Минимальный спред установлен: {value}%")
                
            except ValueError:
                await update.message.reply_text("❌ Пожалуйста, введите число (например: 0.5)")
                
        elif setting == 'amount':
            try:
                parts = update.message.text.split()
                if len(parts) != 2:
                    await update.message.reply_text("❌ Введите два числа через пробел")
                    return
                    
                min_amt = float(parts[0])
                max_amt = float(parts[1])
                
                if min_amt >= max_amt:
                    await update.message.reply_text("❌ Минимальная сумма должна быть меньше максимальной")
                    return
                    
                if min_amt < 0 or max_amt < 0:
                    await update.message.reply_text("❌ Суммы должны быть положительными")
                    return
                    
                self.user_settings.setdefault(user_id, {})['min_amount'] = min_amt
                self.user_settings.setdefault(user_id, {})['max_amount'] = max_amt
                await update.message.reply_text(f"✅ Диапазон установлен: {min_amt} - {max_amt} RUB")
                
            except ValueError:
                await update.message.reply_text("❌ Введите два числа через пробел")
        
        context.user_data['setting'] = None

    async def button_handler(self, update, context):
        """Обработчик кнопок"""
        query = update.callback_query
        data = query.data
        
        if data == "back_to_menu":
            await self.start(update, context)
            
        elif data == "find_opportunities":
            await self.find_opportunities(update, context)
            
        elif data == "settings":
            await self.settings(update, context)
            
        elif data == "set_spread":
            await self.set_spread(update, context)
            
        elif data == "set_amount":
            await self.set_amount(update, context)
            
        elif data == "set_payments":
            await self.set_payments(update, context)
            
        elif data == "set_conditions":
            await self.set_conditions(update, context)
            
        elif data.startswith("pay_"):
            user_id = update.effective_user.id
            settings = self.user_settings.setdefault(user_id, {})
            payment_methods = settings.get('payment_methods', [])
            
            payment_id = data.replace("pay_", "")
            
            if payment_id == "clear":
                settings['payment_methods'] = []
                await query.answer("🗑️ Все платежные методы очищены")
            else:
                payment_name = self.finder.payment_methods.get(payment_id, payment_id)
                if payment_id in payment_methods:
                    payment_methods.remove(payment_id)
                    await query.answer(f"❌ {payment_name} удален")
                else:
                    payment_methods.append(payment_id)
                    await query.answer(f"✅ {payment_name} добавлен")
                    
                settings['payment_methods'] = payment_methods
            
            await self.set_payments(update, context)
            
        elif data == "toggle_conditions":
            user_id = update.effective_user.id
            settings = self.user_settings.setdefault(user_id, {})
            settings['conditions_filter'] = not settings.get('conditions_filter', False)
            await self.set_conditions(update, context)
            
        elif data == "edit_conditions":
            await query.message.edit_text(
                "📝 *Редактирование условий мейкера*\n\n"
                "Введите условия в формате:\n"
                "exact_amount=5000\n"
                "pdf_required=true\n"
                "sbp_blocked=false\n\n"
                "Нажмите /save_conditions для сохранения",
                parse_mode='Markdown'
            )
            await query.answer()
            context.user_data['setting'] = 'conditions'

# --- 6. Основная функция ---

def main():
    """Запуск бота"""
    bot = P2PBot(TELEGRAM_TOKEN, BYBIT_API_KEY, BYBIT_API_SECRET)
    
    # Создание приложения
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CallbackQueryHandler(bot.button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_input))
    
    logger.info("🚀 Бот запущен!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
