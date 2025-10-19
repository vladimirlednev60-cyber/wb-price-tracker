import logging
import re
import requests
import sqlite3
import threading
import time
import asyncio
import os
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

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('wb_prices.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
        chat_id INTEGER,
        article TEXT,
        name TEXT,
        price REAL,
        last_checked TEXT,
        active INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
        chat_id INTEGER PRIMARY KEY,
        check_interval INTEGER DEFAULT 1800
    )''')
    conn.commit()
    conn.close()

# Добавить подписку
def add_subscription(chat_id: int, article: str, name: str, price: float):
    conn = sqlite3.connect('wb_prices.db')
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO subscriptions (chat_id, article, name, price, last_checked, active)
        VALUES (?, ?, ?, ?, datetime('now'), 1)
    ''', (chat_id, article, name, price))
    conn.commit()
    conn.close()

# Получить все активные подписки для пользователя
def get_user_subscriptions(chat_id: int):
    conn = sqlite3.connect('wb_prices.db')
    c = conn.cursor()
    c.execute('SELECT article, name, price, last_checked FROM subscriptions WHERE chat_id = ? AND active = 1', (chat_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# Удалить подписку
def remove_subscription(chat_id: int, article: str):
    conn = sqlite3.connect('wb_prices.db')
    c = conn.cursor()
    c.execute('DELETE FROM subscriptions WHERE chat_id = ? AND article = ?', (chat_id, article))
    conn.commit()
    conn.close()

# Деактивировать подписку
def deactivate_subscription(article: str):
    conn = sqlite3.connect('wb_prices.db')
    c = conn.cursor()
    c.execute('UPDATE subscriptions SET active = 0 WHERE article = ?', (article,))
    conn.commit()
    conn.close()

# Получить все активные подписки
def get_all_active_subscriptions():
    conn = sqlite3.connect('wb_prices.db')
    c = conn.cursor()
    c.execute('SELECT * FROM subscriptions WHERE active = 1')
    rows = c.fetchall()
    conn.close()
    return rows

# Обновить цену и дату проверки
def update_price_and_check_time(article: str, new_price: float):
    conn = sqlite3.connect('wb_prices.db')
    c = conn.cursor()
    c.execute('''
        UPDATE subscriptions
        SET price = ?, last_checked = datetime('now')
        WHERE article = ?
    ''', (new_price, article))
    conn.commit()
    conn.close()

# Получить настройки пользователя
def get_user_settings(chat_id: int):
    conn = sqlite3.connect('wb_prices.db')
    c = conn.cursor()
    c.execute('SELECT check_interval FROM user_settings WHERE chat_id = ?', (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    else:
        set_user_settings(chat_id, 1800)
        return 1800

# Установить настройки пользователя
def set_user_settings(chat_id: int, interval: int):
    conn = sqlite3.connect('wb_prices.db')
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO user_settings (chat_id, check_interval) VALUES (?, ?)', (chat_id, interval))
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
        "ℹ️ Цены могут отличаться при оплате через WB Кошельёк.\n\n"
        "📩 По всем вопросам пишите сюда: https://t.me/NordStorm_Seller\n\n"
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
        for article, name, price, last_checked in subs:
            message += f"📦 {name}\n"
            message += f"💰 Цена: {price:,.0f} ₽\n"
            message += f"🕒 Последняя проверка: {last_checked}\n"
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
        for i, (article, name, price, _) in enumerate(subs, 1):
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
            [InlineKeyboardButton("⏱️ 1 час", callback_data="set_interval_3600")],
            [InlineKeyboardButton("⏱️ 6 часов", callback_data="set_interval_21600")],
            [InlineKeyboardButton("⏱️ 12 часов", callback_data="set_interval_43200")],
            [InlineKeyboardButton("⏱️ 24 часа", callback_data="set_interval_86400")]
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
            "https://t.me/NordStorm_Seller"
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
            f"🔔 Я начну следить за этим товаром. Уведомлю, если цена снизится!"
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

# Фоновая задача: проверка цен
async def check_prices(app: Application):
    while True:
        subscriptions = get_all_active_subscriptions()
        for sub in subscriptions:
            chat_id, article, name, old_price, _, _ = sub
            new_price_info = get_price_from_wb(article)
            if not new_price_info:
                deactivate_subscription(article)
                continue

            new_price = new_price_info["price"]
            if new_price < old_price:
                message = (
                    f"📉 Цена на товар снизилась!\n"
                    f"Товар: {name}\n"
                    f"Старая цена: {old_price:,.0f} ₽\n"
                    f"Новая цена: {new_price:,.0f} ₽\n"
                    f"ℹ️ При оплате через WB Кошельёк цена может быть ещё ниже."
                )
                try:
                    await app.bot.send_message(chat_id=chat_id, text=message)
                except Exception as e:
                    logging.error(f"Не удалось отправить уведомление: {e}")

            update_price_and_check_time(article, new_price)

        await asyncio.sleep(1800)

# Запуск
def main():
    if not TOKEN or len(TOKEN) < 10:
        raise ValueError("Токен недействителен!")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message))
    app.add_handler(CallbackQueryHandler(button_callback))

    threading.Thread(target=lambda: asyncio.run(check_prices(app)), daemon=True).start()

    print("✅ Бот запущен и ждёт действий!")
    app.run_polling()

if __name__ == '__main__':
    main()