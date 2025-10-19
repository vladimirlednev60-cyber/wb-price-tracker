import logging
import re
import requests
import psycopg2
import threading
import time
import asyncio
import os
from datetime import datetime, timezone, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# --- 🔐 ЗАЩИТА КОДА ---
TOKEN = os.getenv("WB_BOT_TOKEN")
if not TOKEN:
    raise ValueError("Токен не задан! Установите переменную окружения WB_BOT_TOKEN")

if "8330838475" in TOKEN and "AAHt2IXITb62-IfAwr8ZLKpGACSRAL15BlA" in TOKEN:
    pass
else:
    raise ValueError("Неверный токен! Этот бот защищён от кражи.")

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Настройка Moscow Time (MSK)
MSK = timezone(timedelta(hours=3))  # UTC+3

# Инициализация базы данных (PostgreSQL)
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
        chat_id INTEGER,
        article TEXT,
        name TEXT,
        price REAL,
        last_checked TEXT,
        active INTEGER DEFAULT 1,
        last_notified_price REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
        chat_id INTEGER PRIMARY KEY,
        check_interval INTEGER DEFAULT 300  -- по умолчанию 5 минут
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats (
        id SERIAL PRIMARY KEY,
        event_type TEXT,
        chat_id INTEGER,
        article TEXT,
        old_price REAL,
        new_price REAL,
        timestamp TIMESTAMP DEFAULT NOW()
    )''')
    conn.commit()
    conn.close()

# Получение соединения с базой данных
def get_db_connection():
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не задан!")
    return psycopg2.connect(DATABASE_URL)

# Добавить подписку
def add_subscription(chat_id: int, article: str, name: str, price: float):
    now_msk = datetime.now(MSK).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO subscriptions (chat_id, article, name, price, last_checked, active, last_notified_price)
        VALUES (%s, %s, %s, %s, %s, 1, %s)
    ''', (chat_id, article, name, price, now_msk, price))
    conn.commit()
    conn.close()

# Получить все активные подписки для пользователя
def get_user_subscriptions(chat_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT article, name, price, last_checked, last_notified_price FROM subscriptions WHERE chat_id = %s AND active = 1', (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# Удалить подписку
def remove_subscription(chat_id: int, article: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM subscriptions WHERE chat_id = %s AND article = %s', (chat_id, article))
    conn.commit()
    conn.close()

# Деактивировать подписку
def deactivate_subscription(article: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE subscriptions SET active = 0 WHERE article = %s', (article,))
    conn.commit()
    conn.close()

# Получить все активные подписки
def get_all_active_subscriptions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM subscriptions WHERE active = 1')
    rows = c.fetchall()
    conn.close()
    return rows

# Обновить цену и дату проверки + записать статистику
def update_price_and_check_time(article: str, new_price: float, old_price: float, chat_id: int, last_notified_price: float):
    now_msk = datetime.now(MSK).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE subscriptions
        SET price = %s, last_checked = %s, last_notified_price = %s
        WHERE article = %s
    ''', (new_price, now_msk, new_price, article))
    # Записываем событие в статистику
    c.execute('''
        INSERT INTO stats (event_type, chat_id, article, old_price, new_price)
        VALUES (%s, %s, %s, %s, %s)
    ''', ('price_change', chat_id, article, old_price, new_price))
    conn.commit()
    conn.close()

# Получить настройки пользователя
def get_user_settings(chat_id: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT check_interval FROM user_settings WHERE chat_id = %s', (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    else:
        set_user_settings(chat_id, 300)  # 5 минут по умолчанию
        return 300

# Установить настройки пользователя
def set_user_settings(chat_id: int, interval: int):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO user_settings (chat_id, check_interval) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET check_interval = EXCLUDED.check_interval', (chat_id, interval))
    conn.commit()
    conn.close()

# Извлечение артикула из ссылки
def extract_article_from_url(url: str) -> str | None:
    match = re.search(r'/catalog/(\d+)/detail', url)
    if match:
        return match.group(1)
    return None

# Получение цены с Wildberries
def get_price_from_wb(article: str) -> dict | None:
    try:
        url = f"https://card.wb.ru/cards/v2/detail?nm={article}&dest=-1257786&locale=ru&lang=ru"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.wildberries.ru/"
        }
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None

        data = response.json()
        if not data.get("data") or not data["data"].get("products"):
            return None

        product = data["data"]["products"][0]
        name = product.get("name", "Без названия")
        sizes = product.get("sizes", [])
        if not sizes:
            return None

        size = sizes[0]
        price_data = size.get("price", {})
        total_price_raw = price_data.get("total")
        if total_price_raw is None:
            return None

        return {
            "name": name,
            "price": total_price_raw / 100
        }

    except Exception as e:
        logging.error(f"Ошибка при получении цены: {e}")
        return None

# Главное меню
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("➕ Добавить товар"), KeyboardButton("📋 Мои товары")],
        [KeyboardButton("🗑️ Удалить товар"), KeyboardButton("⚙️ Настройки")],
        [KeyboardButton("💬 Поддержка")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Обработчик /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = (
        "👋 Привет! Я — ваш персональный помощник по экономии на Wildberries!\n\n"
        "📌 Суть бота:\n"
        "- Отправляешь ссылку на товар → я запоминаю его\n"
        "- Раз в N минут проверяю цену\n"
        "- Если цена снижается — отправляю тебе уведомление!\n\n"
        "ℹ️ Цены могут отличаться при оплате через WB Кошельёк.\n"
        "🕒 Все временные метки в боте указаны по Московскому времени (MSK).\n"
        "🔔 Не забудьте включить уведомления от бота — иначе вы можете пропустить скидку!\n\n"
        "📩 По всем вопросам пишите сюда: https://t.me/+8M7L0tXjoV9mMGYy\n\n"
        "👇 Начнём? Выберите действие:"
    )
    await update.message.reply_text(welcome_msg)
    await show_main_menu(update, context)

# Обработчик нажатия кнопок и текста
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.message.chat_id

    # Если пользователь нажал "Добавить товар"
    if text == "➕ Добавить товар":
        await update.message.reply_text(
            "🔗 Отправь мне ссылку на товар Wildberries (например, https://www.wildberries.ru/catalog/12345678/detail.aspx)"
        )
        return

    # Если пользователь нажал "📋 Мои товары"
    elif text == "📋 Мои товары":
        subs = get_user_subscriptions(chat_id)
        if not subs:
            await update.message.reply_text("У вас нет отслеживаемых товаров.")
            await show_main_menu(update, context)
            return

        message = "📌 Ваши отслеживаемые товары:\n\n"
        for article, name, price, last_checked, last_notified_price in subs:
            message += f"📦 {name}\n"
            message += f"💰 Цена: {price:,.0f} ₽\n"
            message += f"🕒 Последняя проверка: {last_checked} (MSK)\n"
            message += f"🔄 Последнее уведомление: {last_notified_price:,.0f} ₽\n"
            message += f"🔗 https://www.wildberries.ru/catalog/{article}/detail.aspx\n\n"

        await update.message.reply_text(message)
        await show_main_menu(update, context)
        return

    # Если пользователь нажал "🗑️ Удалить товар"
    elif text == "🗑️ Удалить товар":
        subs = get_user_subscriptions(chat_id)
        if not subs:
            await update.message.reply_text("У вас нет отслеживаемых товаров.")
            await show_main_menu(update, context)
            return

        message = "Выберите товар для удаления:\n\n"
        for i, (article, name, price, _, _) in enumerate(subs, 1):
            message += f"{i}. {name} — {price:,.0f} ₽\n"
        
        message += "\nНапишите номер товара, который хотите удалить."

        context.user_data['subscriptions'] = subs
        await update.message.reply_text(message)
        return

    # Если пользователь нажал "⚙️ Настройки"
    elif text == "⚙️ Настройки":
        interval = get_user_settings(chat_id)
        minutes = interval // 60
        keyboard = [
            [InlineKeyboardButton(f"⏱️ {minutes} мин", callback_data=f"set_interval_{interval}")]
        ]
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏱️ 5 минут", callback_data="set_interval_300")],
            [InlineKeyboardButton("⏱️ 10 минут", callback_data="set_interval_600")],
            [InlineKeyboardButton("⏱️ 30 минут", callback_data="set_interval_1800")],
            [InlineKeyboardButton("⏱️ 1 час", callback_data="set_interval_3600")]
        ])
        await update.message.reply_text(
            f"Текущая частота проверки: каждые {minutes} минут\n\n"
            "Выберите новую частоту:",
            reply_markup=reply_markup
        )
        return

    # Если пользователь нажал "💬 Поддержка"
    elif text == "💬 Поддержка":
        await update.message.reply_text(
            "📩 По всем вопросам пишите сюда:\n"
            "https://t.me/+8M7L0tXjoV9mMGYy"
        )
        await show_main_menu(update, context)
        return

    # Если пользователь отправил ссылку
    elif "wildberries.ru" in text:
        article = extract_article_from_url(text)
        if not article:
            await update.message.reply_text(
                "Не удалось найти артикул. Убедитесь, что ссылка вида:\n"
                "https://www.wildberries.ru/catalog/12345678/detail.aspx"
            )
            await show_main_menu(update, context)
            return

        product_info = get_price_from_wb(article)
        if not product_info:
            await update.message.reply_text("Не удалось получить цену. Попробуйте позже.")
            await show_main_menu(update, context)
            return

        name = product_info["name"]
        price = product_info["price"]

        add_subscription(chat_id, article, name, price)

        await update.message.reply_text(
            f"✅ Товар: {name}\n"
            f"💰 Текущая цена: {price:,.0f} ₽\n"
            f"ℹ️ При оплате через WB Кошельёк цена может быть ниже.\n\n"
            f"🔔 Я начну следить за этим товаром. Уведомлю, если цена снизится!\n"
            f"💡 Не забудьте включить уведомления от бота — иначе вы можете пропустить скидку!"
        )
        await show_main_menu(update, context)
        return

    # Если пользователь вводит номер для удаления
    elif 'subscriptions' in context.user_data and text.isdigit():
        subs = context.user_data['subscriptions']
        index = int(text) - 1
        if 0 <= index < len(subs):
            article = subs[index][0]
            remove_subscription(chat_id, article)
            await update.message.reply_text(f"✅ Товар с артикулом {article} удалён из отслеживания.")
            del context.user_data['subscriptions']
            await show_main_menu(update, context)
        else:
            await update.message.reply_text("Неверный номер. Попробуйте ещё раз.")
        return

    else:
        await update.message.reply_text(
            "Я не понял ваш запрос.\nИспользуйте кнопки или отправьте ссылку на товар."
        )
        await show_main_menu(update, context)

# Обработчик нажатий на inline-кнопки (настройки)
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("set_interval_"):
        interval = int(data.split("_")[2])
        chat_id = query.from_user.id
        set_user_settings(chat_id, interval)
        minutes = interval // 60
        await query.edit_message_text(f"✅ Частота проверки установлена: каждые {minutes} минут")

# Команда /stats — только для админа
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверяем, является ли пользователь админом
    admin_chat_id = int(os.getenv("ADMIN_CHAT_ID", "0"))
    if update.message.chat_id != admin_chat_id:
        await update.message.reply_text("У вас нет доступа к статистике.")
        return

    conn = get_db_connection()
    c = conn.cursor()

    # Общее количество пользователей
    c.execute('SELECT COUNT(DISTINCT chat_id) FROM subscriptions WHERE active = 1')
    total_users = c.fetchone()[0]

    # Общее количество товаров
    c.execute('SELECT COUNT(*) FROM subscriptions WHERE active = 1')
    total_items = c.fetchone()[0]

    # Количество уведомлений о снижении цены
    c.execute("SELECT COUNT(*) FROM stats WHERE event_type = 'price_change'")
    price_changes = c.fetchone()[0]

    # Топ-5 самых популярных товаров
    c.execute('''
        SELECT article, name, COUNT(*) as count
        FROM subscriptions
        WHERE active = 1
        GROUP BY article, name
        ORDER BY count DESC
        LIMIT 5
    ''')
    top_items = c.fetchall()

    conn.close()

    message = "📊 Статистика бота:\n\n"
    message += f"👥 Всего пользователей: {total_users}\n"
    message += f"📦 Всего отслеживаемых товаров: {total_items}\n"
    message += f"📉 Изменений цен: {price_changes}\n\n"

    if top_items:
        message += "🔥 Топ-5 самых популярных товаров:\n"
        for article, name, count in top_items:
            message += f"• {name} ({count} раз)\n"

    await update.message.reply_text(message)

# Фоновая задача: проверка цен
async def check_prices(app: Application):
    while True:
        subscriptions = get_all_active_subscriptions()
        for sub in subscriptions:
            chat_id, article, name, old_price, _, last_notified_price = sub
            new_price_info = get_price_from_wb(article)
            if not new_price_info:
                deactivate_subscription(article)
                continue

            new_price = new_price_info["price"]

            # Если цена снизилась — отправляем уведомление
            if new_price < old_price:
                # Расчёт процента снижения
                percent_drop = ((old_price - new_price) / old_price) * 100
                message = (
                    f"📉 Цена на товар снизилась!\n"
                    f"Товар: {name}\n"
                    f"Старая цена: {old_price:,.0f} ₽\n"
                    f"Новая цена: {new_price:,.0f} ₽\n"
                    f"📉 Снижение: {percent_drop:.1f}%\n"
                    f"ℹ️ При оплате через WB Кошельёк цена может быть ещё ниже.\n"
                    f"🕒 Время уведомления: {datetime.now(MSK).strftime('%H:%M %d.%m.%Y')} (MSK)\n"
                    f"🔔 Это лучший момент для покупки!"
                )
                try:
                    await app.bot.send_message(chat_id=chat_id, text=message)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление: {e}")

            # Если цена повысилась — отправляем предупреждение
            elif new_price > old_price:
                percent_increase = ((new_price - old_price) / old_price) * 100
                message = (
                    f"📈 Цена на товар повысилась!\n"
                    f"Товар: {name}\n"
                    f"Старая цена: {old_price:,.0f} ₽\n"
                    f"Новая цена: {new_price:,.0f} ₽\n"
                    f"📈 Рост: {percent_increase:.1f}%\n"
                    f"ℹ️ Возможно, стоит подождать — цена может снова снизиться.\n"
                    f"🕒 Время уведомления: {datetime.now(MSK).strftime('%H:%M %d.%m.%Y')} (MSK)"
                )
                try:
                    await app.bot.send_message(chat_id=chat_id, text=message)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление: {e}")

            # Обновляем цену и записываем статистику
            update_price_and_check_time(article, new_price, old_price, chat_id, last_notified_price)

        # Ждём минимальный интервал (5 минут)
        await asyncio.sleep(300)

# Запуск
def main():
    if not TOKEN or len(TOKEN) < 10:
        raise ValueError("Токен недействителен!")

    # Проверка админ-чата
    admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    if not admin_chat_id:
        raise ValueError("ADMIN_CHAT_ID не задан! Установите переменную окружения ADMIN_CHAT_ID")

    # Создаём базу данных
    init_db()

    app = Application.builder().token(TOKEN).build()

    # Добавляем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(CommandHandler("stats", show_stats))  # Команда для админа

    # Запускаем фоновую задачу
    threading.Thread(target=lambda: asyncio.run(check_prices(app)), daemon=True).start()

    print("✅ Бот запущен и ждёт действий!")
    app.run_polling()

if __name__ == '__main__':
    main()
