import asyncio
import sqlite3
import os
import re
import base64
import uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.executor import start_polling
import threading
import traceback

# Pending admin confirmations
admin_requests = {}  # request_id -> {user_id, user_name, status: 'pending'}

# ========== КОНФИГУРАЦИЯ ==========
CLIENT_BOT_TOKEN = os.environ.get('CLIENT_BOT_TOKEN')
ADMIN_BOT_TOKEN  = os.environ.get('ADMIN_BOT_TOKEN')
ADMIN_CHAT_ID    = int(os.environ.get('ADMIN_CHAT_ID', 0))

MINSK_TZ = timezone(timedelta(hours=3))

# ========== БАЗА ДАННЫХ ==========
DB_NAME = "orders.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id       INTEGER,
            client_name     TEXT,
            client_username TEXT,
            phone           TEXT,
            model_name      TEXT,
            quantity        INTEGER,
            image_url       TEXT,
            price_per_unit  REAL DEFAULT 0,
            total_price     REAL DEFAULT 0,
            paid_amount     REAL DEFAULT 0,
            status          TEXT DEFAULT 'новый',
            created_at      TEXT,
            updated_at      TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id    INTEGER PRIMARY KEY,
            name       TEXT,
            phone      TEXT,
            avatar_url TEXT,
            updated_at TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")


def get_current_time():
    return datetime.now(MINSK_TZ).strftime('%Y-%m-%d %H:%M:%S')


def create_order(client_id, client_name, client_username, phone, model_name, quantity, image_url=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now = get_current_time()
    cursor.execute('''
        INSERT INTO orders
            (client_id, client_name, client_username, phone, model_name, quantity, image_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (client_id, client_name, client_username, phone, model_name, quantity, image_url, now, now))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id


def set_order_price(order_id, price_per_unit):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT quantity FROM orders WHERE id = ?', (order_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return
    total_price = row[0] * price_per_unit
    cursor.execute('''
        UPDATE orders SET price_per_unit=?, total_price=?, updated_at=? WHERE id=?
    ''', (price_per_unit, total_price, get_current_time(), order_id))
    conn.commit()
    conn.close()


def update_order_status(order_id, status):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE orders SET status=?, updated_at=? WHERE id=?',
                   (status, get_current_time(), order_id))
    conn.commit()
    conn.close()


def get_order(order_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM orders WHERE id=?', (order_id,))
    order = cursor.fetchone()
    conn.close()
    return order


def get_orders_by_client(client_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM orders WHERE client_id=? ORDER BY id DESC', (client_id,))
    orders = cursor.fetchall()
    conn.close()
    return orders


def get_all_orders(status=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if status:
        cursor.execute('SELECT * FROM orders WHERE status=? ORDER BY id DESC', (status,))
    else:
        cursor.execute('SELECT * FROM orders ORDER BY id DESC')
    orders = cursor.fetchall()
    conn.close()
    return orders


# ========== ПРОФИЛИ ==========
def save_user_profile(user_id, name, phone, avatar_url=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now = get_current_time()
    if avatar_url is not None:
        cursor.execute('''
            INSERT OR REPLACE INTO user_profiles (user_id, name, phone, avatar_url, updated_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, name, phone, avatar_url, now))
    else:
        cursor.execute('''
            INSERT INTO user_profiles (user_id, name, phone, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET name=?, phone=?, updated_at=?
        ''', (user_id, name, phone, now, name, phone, now))
    conn.commit()
    conn.close()


def get_user_profile(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT name, phone, avatar_url FROM user_profiles WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'name': row[0], 'phone': row[1], 'avatar_url': row[2]}
    return None


def get_all_profiles():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, name, phone, avatar_url, updated_at FROM user_profiles ORDER BY updated_at DESC')
    rows = cursor.fetchall()
    conn.close()
    return rows


# ========== УТИЛИТЫ ==========
STATUS_EMOJI = {
    'новый': '🟡', 'принят': '🟢', 'отказ': '🔴',
    'подготовка модели': '🟠', 'цена выставлена': '💰',
    'оплачено наличными': '✅', 'печать': '🖨️', 'готова': '🎉',
    'завершён': '🏁'
}

STATUS_MSG = {
    'принят': '🟢 Ваш заказ №{id} принят в работу!',
    'отказ': '🔴 Ваш заказ №{id} отклонён.',
    'подготовка модели': '🟠 Ваш заказ №{id} — подготовка модели.',
    'цена выставлена': '💰 Цена за заказ №{id} выставлена.',
    'оплачено наличными': '✅ Заказ №{id} оплачен.',
    'печать': '🖨️ Ваш заказ №{id} начали печатать!',
    'готова': '🎉 Ваш заказ №{id} готов к выдаче!',
    'завершён': '🏁 Заказ №{id} завершён.'
}


def safe_send(chat_id, text, bot_token=None, reply_markup=None):
    if bot_token is None:
        bot_token = CLIENT_BOT_TOKEN
    try:
        async def _do():
            b = Bot(token=bot_token)
            try:
                kwargs = {'chat_id': chat_id, 'text': text}
                if reply_markup:
                    kwargs['reply_markup'] = reply_markup
                await b.send_message(**kwargs)
            finally:
                await b.session.close()
        asyncio.run(_do())
    except Exception as e:
        print(f"❌ Ошибка отправки в {chat_id}: {e}")
        traceback.print_exc()


# ========== КЛИЕНТСКИЙ БОТ ==========
client_bot     = Bot(token=CLIENT_BOT_TOKEN)
client_storage = MemoryStorage()
client_dp      = Dispatcher(client_bot, storage=client_storage)
client_dp.middleware.setup(LoggingMiddleware())


class OrderForm(StatesGroup):
    waiting_for_name     = State()
    waiting_for_phone    = State()
    waiting_for_model    = State()
    waiting_for_quantity = State()


class RegForm(StatesGroup):
    waiting_for_name  = State()
    waiting_for_phone = State()


def validate_phone(phone):
    digits = re.sub(r'[^\d]', '', phone)
    if digits.startswith('375'):
        digits = digits[3:]
    elif digits.startswith('7'):
        digits = digits[1:]
    elif digits.startswith('8'):
        digits = digits[1:]
    if len(digits) == 9:
        return f"+375 {digits[:2]} {digits[2:5]}-{digits[5:7]}-{digits[7:9]}"
    return None


client_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
client_keyboard.add(KeyboardButton("🛒 Сделать заказ"))
client_keyboard.add(KeyboardButton("📋 Мои заказы"))


def main_menu_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📦 Новый заказ", callback_data="new_order"),
        InlineKeyboardButton("📋 Мои заказы", callback_data="my_orders"),
    )
    kb.add(InlineKeyboardButton("❓ Помощь", callback_data="help"))
    return kb


@client_dp.message_handler(commands=['start'])
async def client_start(message: types.Message):
    profile = get_user_profile(message.from_user.id)
    if profile and profile['name']:
        await message.answer(
            f"Привет, {profile['name']}!\n\n"
            "Выберите действие:",
            reply_markup=main_menu_kb()
        )
    else:
        await message.answer(
            "Добро пожаловать!\n\n"
            "Для начала нужно зарегистрироваться.\n"
            "Введите ваше имя:"
        )
        await RegForm.waiting_for_name.set()


@client_dp.message_handler(state=RegForm.waiting_for_name)
async def reg_get_name(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['name'] = message.text.strip()
    await RegForm.next()
    await message.answer("Введите ваш номер телефона:\nПример: +375291234567")


@client_dp.message_handler(state=RegForm.waiting_for_phone)
async def reg_get_phone(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    phone = validate_phone(raw)
    if not phone:
        await message.answer(
            "❌ Неверный номер телефона!\n\n"
            "Нужно 9 цифр — номер в Беларуси.\n\n"
            "Примеры:\n"
            "291234567\n"
            "+375291234567\n"
            "375 29 123-45-67\n\n"
            "Введите номер ещё раз:"
        )
        return
    async with state.proxy() as data:
        data['phone'] = phone
    profile = get_user_profile(message.from_user.id)
    avatar = profile['avatar_url'] if profile else None
    save_user_profile(message.from_user.id, data['name'], phone, avatar)
    await state.finish()
    await message.answer(
        f"✅ Регистрация завершена!\n\n"
        f"👤 {data['name']}\n"
        f"📱 {phone}\n\n"
        "Теперь вы можете оформлять заказы.",
        reply_markup=main_menu_kb()
    )


@client_dp.message_handler(commands=['register'])
async def cmd_register(message: types.Message):
    profile = get_user_profile(message.from_user.id)
    if profile and profile['name']:
        await message.answer(
            f"Вы уже зарегистрированы:\n\n"
            f"👤 {profile['name']}\n"
            f"📱 {profile['phone']}\n\n"
            "Хотите изменить данные? Введите новое имя:"
        )
    else:
        await message.answer("Введите ваше имя:")
    await RegForm.waiting_for_name.set()


@client_dp.message_handler(commands=['profile'])
async def cmd_profile(message: types.Message):
    profile = get_user_profile(message.from_user.id)
    if not profile or not profile['name']:
        await message.answer("Вы ещё не зарегистрированы.\nВведите /register")
        return
    text = (
        f"👤 Ваш профиль\n\n"
        f"Имя: {profile['name']}\n"
        f"Телефон: {profile['phone']}\n"
    )
    if profile['avatar_url']:
        text += f"Аватар: есть\n"
    text += "\nДля изменения: /register"
    await message.answer(text)


@client_dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    await message.answer(
        "Команды бота:\n\n"
        "/start — Главное меню\n"
        "/register — Регистрация / изменение данных\n"
        "/profile — Мой профиль\n"
        "/new_order — Создать заказ\n"
        "/my_orders — Мои заказы\n"
        "/order_123 — Детали заказа\n"
        "/cancel_123 — Отменить заказ\n"
        "/help — Эта справка"
    )


@client_dp.callback_query_handler(lambda c: c.data == "new_order")
async def cb_new_order(callback_query: types.CallbackQuery):
    await callback_query.answer()
    profile = get_user_profile(callback_query.from_user.id)
    if not profile or not profile['name']:
        await callback_query.message.answer("Сначала зарегистрируйтесь: /register")
        return
    await OrderForm.waiting_for_model.set()
    await callback_query.message.answer(
        f"Вы: {profile['name']} ({profile['phone']})\n\n"
        "Введите название модели или описание:"
    )


@client_dp.callback_query_handler(lambda c: c.data == "my_orders")
async def cb_my_orders(callback_query: types.CallbackQuery):
    await callback_query.answer()
    orders = get_orders_by_client(callback_query.from_user.id)
    if not orders:
        await callback_query.message.answer("📭 У вас пока нет заказов.")
        return
    text = "📋 МОИ ЗАКАЗЫ\n\n"
    for order in orders[:10]:
        emoji = STATUS_EMOJI.get(order[11], '⚪')
        total = order[9] if order[9] else 0
        text += f"{emoji} #{order[0]} — {order[5]}\n"
        text += f"   {order[6]} шт."
        if total > 0:
            text += f" | {total} руб."
        text += f" | {order[11]}\n"
    await callback_query.message.answer(text)


@client_dp.callback_query_handler(lambda c: c.data == "help")
async def cb_help(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await callback_query.message.answer(
        "Команды:\n\n"
        "/register — Регистрация\n"
        "/new_order — Создать заказ\n"
        "/my_orders — Мои заказы\n"
        "/order_123 — Детали заказа\n"
        "/cancel_123 — Отменить заказ"
    )


@client_dp.message_handler(commands=['new_order'])
async def cmd_new_order(message: types.Message):
    profile = get_user_profile(message.from_user.id)
    if not profile or not profile['name']:
        await message.answer("Сначала зарегистрируйтесь: /register")
        return
    await OrderForm.waiting_for_model.set()
    await message.answer(
        f"Вы: {profile['name']} ({profile['phone']})\n\n"
        "Введите название модели или описание:"
    )


@client_dp.message_handler(state=OrderForm.waiting_for_model)
async def get_model(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['model'] = message.text
    await OrderForm.next()
    await message.answer("Введите количество деталей (цифрой):")


@client_dp.message_handler(state=OrderForm.waiting_for_quantity)
async def get_quantity(message: types.Message, state: FSMContext):
    try:
        quantity = int(message.text)
        if quantity < 1:
            await message.answer("Количество должно быть больше 0")
            return
        data = await state.get_data()
        profile = get_user_profile(message.from_user.id)
        name = profile['name'] if profile else "Клиент"
        phone = profile['phone'] if profile else ""
        order_id = create_order(
            client_id=message.from_user.id,
            client_name=name,
            client_username=message.from_user.username or "нет",
            phone=phone,
            model_name=data['model'],
            quantity=quantity
        )
        await message.answer(
            f"✅ Заказ №{order_id} создан!\n\n"
            f"📦 {data['model']}\n"
            f"🔢 {quantity} шт.\n\n"
            "Статус в «📋 Мои заказы»."
        )
        safe_send(
            ADMIN_CHAT_ID,
            f"🆕 НОВЫЙ ЗАКАЗ #{order_id}\n\n"
            f"👤 {name}\n"
            f"📱 {phone}\n"
            f"📦 {data['model']}\n"
            f"🔢 {quantity} шт."
        )
        await state.finish()
    except ValueError:
        await message.answer("❌ Ошибка! Введите число.")


@client_dp.message_handler(commands=['my_orders'])
async def cmd_my_orders(message: types.Message):
    orders = get_orders_by_client(message.from_user.id)
    if not orders:
        await message.answer("📭 У вас пока нет заказов.")
        return
    text = "📋 МОИ ЗАКАЗЫ\n\n"
    for order in orders[:10]:
        emoji = STATUS_EMOJI.get(order[11], '⚪')
        total = order[9] if order[9] else 0
        text += f"{emoji} #{order[0]} — {order[5]}\n"
        text += f"   {order[6]} шт."
        if total > 0:
            text += f" | {total} руб."
        text += f" | {order[11]}\n"
    await message.answer(text)


@client_dp.message_handler(lambda m: re.match(r'/order_(\d+)', m.text or ''))
async def cmd_order_detail(message: types.Message):
    match = re.match(r'/order_(\d+)', message.text)
    order_id = int(match.group(1))
    order = get_order(order_id)
    if not order:
        await message.answer("❌ Заказ не найден.")
        return
    if order[1] != message.from_user.id:
        await message.answer("❌ Это не ваш заказ.")
        return
    emoji = STATUS_EMOJI.get(order[11], '⚪')
    total = order[9] if order[9] else 0
    price = order[8] if order[8] else 0
    text = (
        f"{emoji} Заказ #{order[0]}\n\n"
        f"📦 {order[5]}\n"
        f"🔢 {order[6]} шт.\n"
    )
    if price > 0:
        text += f"💵 {price} руб. / шт.\n"
    if total > 0:
        text += f"💰 Итого: {total} руб.\n"
    text += f"📍 {order[11]}\n"
    text += f"📅 {order[12] or '—'}"
    await message.answer(text)


@client_dp.message_handler(lambda m: re.match(r'/cancel_(\d+)', m.text or ''))
async def cmd_cancel_order(message: types.Message):
    match = re.match(r'/cancel_(\d+)', message.text)
    order_id = int(match.group(1))
    order = get_order(order_id)
    if not order:
        await message.answer("❌ Заказ не найден.")
        return
    if order[1] != message.from_user.id:
        await message.answer("❌ Это не ваш заказ.")
        return
    if order[11] != 'новый':
        await message.answer("❌ Отменить можно только заказ со статусом «новый».")
        return
    update_order_status(order_id, "отказ")
    await message.answer(f"❌ Заказ №{order_id} отменён.")
    safe_send(ADMIN_CHAT_ID, f"❌ Клиент отменил заказ #{order_id}", bot_token=ADMIN_BOT_TOKEN)


@client_dp.message_handler(lambda m: m.text == "🛒 Сделать заказ")
async def make_order(message: types.Message):
    profile = get_user_profile(message.from_user.id)
    if not profile or not profile['name']:
        await message.answer("Сначала зарегистрируйтесь: /register")
        return
    await OrderForm.waiting_for_model.set()
    await message.answer(
        f"Вы: {profile['name']} ({profile['phone']})\n\n"
        "Введите название модели или описание:"
    )


@client_dp.message_handler(lambda m: m.text == "📋 Мои заказы")
async def my_orders_btn(message: types.Message):
    orders = get_orders_by_client(message.from_user.id)
    if not orders:
        await message.answer("📭 У вас пока нет заказов.")
        return
    text = "📋 МОИ ЗАКАЗЫ\n\n"
    for order in orders[:10]:
        emoji = STATUS_EMOJI.get(order[11], '⚪')
        total = order[9] if order[9] else 0
        text += f"{emoji} #{order[0]} — {order[5]}\n"
        text += f"   {order[6]} шт."
        if total > 0:
            text += f" | {total} руб."
        text += f" | {order[11]}\n"
    await message.answer(text)


# ========== АДМИНСКИЕ КОМАНДЫ (через клиентский бот) ==========
@client_dp.message_handler(commands=['admin_orders'])
async def admin_all_orders(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    orders = get_all_orders()
    if not orders:
        await message.answer("📭 Заказов нет")
        return
    text = "📋 ВСЕ ЗАКАЗЫ\n\n"
    for order in orders[:20]:
        text += f"#{order[0]} | {order[2]} | {order[5]} | {order[11]} | {order[9]} руб.\n"
    await message.answer(text)


@client_dp.message_handler(commands=['admin_new'])
async def admin_new_orders(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    orders = get_all_orders(status="новый")
    if not orders:
        await message.answer("🟡 Новых заказов нет")
        return
    for order in orders:
        text = (
            f"🆕 ЗАКАЗ #{order[0]}\n\n"
            f"👤 {order[2]}\n📱 {order[4]}\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт.\n\n"
            f"➡️ /accept_{order[0]} — принять\n"
            f"➡️ /reject_{order[0]} — отказать"
        )
        await message.answer(text)


@client_dp.message_handler(commands=['active'])
async def admin_active_orders(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    active_statuses = ["принят", "цена выставлена", "подготовка модели", "оплачено наличными", "печать"]
    orders = []
    for s in active_statuses:
        orders.extend(get_all_orders(status=s))
    orders = sorted(orders, key=lambda x: x[0], reverse=True)
    if not orders:
        await message.answer("⚡ Активных заказов нет")
        return
    text = "⚡ АКТИВНЫЕ ЗАКАЗЫ\n\n"
    for order in orders[:20]:
        emoji = STATUS_EMOJI.get(order[11], '⚪')
        text += f"{emoji} #{order[0]} | {order[2]} | {order[11]} | {order[9]} руб.\n"
    await message.answer(text)


@client_dp.message_handler(lambda m: re.match(r'/accept_\d+', m.text or ''))
async def admin_accept_order(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/accept_(\d+)', message.text)
    order_id = int(match.group(1))
    update_order_status(order_id, "принят")
    order = get_order(order_id)
    if order:
        safe_send(order[1], f"🟢 Заказ №{order_id} принят в работу!")
    await message.answer(f"✅ Заказ #{order_id} принят")


@client_dp.message_handler(lambda m: re.match(r'/reject_\d+', m.text or ''))
async def admin_reject_order(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/reject_(\d+)', message.text)
    order_id = int(match.group(1))
    update_order_status(order_id, "отказ")
    order = get_order(order_id)
    if order:
        safe_send(order[1], f"🔴 Заказ №{order_id} отклонён")
    await message.answer(f"❌ Заказ #{order_id} отклонён")


@client_dp.message_handler(lambda m: re.match(r'/price_\d+\s+\d+', m.text or ''))
async def admin_price_order(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/price_(\d+)\s+(\d+(?:\.\d+)?)', message.text)
    if not match:
        await message.answer("Формат: /price_123 500")
        return
    order_id = int(match.group(1))
    price = float(match.group(2))
    set_order_price(order_id, price)
    order = get_order(order_id)
    if order:
        total = price * order[6]
        safe_send(
            order[1],
            f"💰 Стоимость заказа №{order_id}\n\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт.\n"
            f"💵 Цена за шт.: {price} руб.\n"
            f"💲 Итого: {total} руб."
        )
    await message.answer(f"✅ Цена {price} руб. для заказа #{order_id}")


@client_dp.message_handler(lambda m: re.match(r'/status_\d+\s+.+', m.text or ''))
async def admin_status_order(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/status_(\d+)\s+(.+)', message.text)
    if not match:
        await message.answer("Формат: /status_123 подготовка модели")
        return
    order_id  = int(match.group(1))
    new_status = match.group(2).strip()
    update_order_status(order_id, new_status)
    order = get_order(order_id)
    if order:
        msg = STATUS_MSG.get(new_status, f"🔄 Статус заказа №{order_id}: {new_status}")
        msg = msg.format(id=order_id)
        safe_send(order[1], msg)
    await message.answer(f"✅ Заказ #{order_id} → {new_status}")


# ========== АДМИНСКИЙ БОТ (отдельный) ==========
admin_bot  = Bot(token=ADMIN_BOT_TOKEN)
admin_dp   = Dispatcher(admin_bot, storage=MemoryStorage())
admin_dp.middleware.setup(LoggingMiddleware())


def admin_main_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🆕 Новые", callback_data="adm_new"),
        InlineKeyboardButton("⚡ Активные", callback_data="adm_active"),
    )
    kb.add(
        InlineKeyboardButton("📋 Все заказы", callback_data="adm_all"),
        InlineKeyboardButton("👥 Пользователи", callback_data="adm_users"),
    )
    return kb


@admin_dp.message_handler(commands=['start'])
async def admin_start(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "🛠 Админ-панель 3Dprinti\n\n"
        "Выберите действие:",
        reply_markup=admin_main_kb()
    )


@admin_dp.message_handler(commands=['help'])
async def admin_help_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    await message.answer(
        "Команды админа:\n\n"
        "/start — Меню\n"
        "/orders — Все заказы\n"
        "/new — Новые заказы\n"
        "/active — Активные\n"
        "/users — Пользователи\n"
        "/accept_123 — Принять заказ\n"
        "/reject_123 — Отказать\n"
        "/price_123 500 — Установить цену\n"
        "/status_123 печать — Изменить статус"
    )


@admin_dp.callback_query_handler(lambda c: c.data == "adm_new")
async def cb_admin_new(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_CHAT_ID:
        await callback_query.answer("Нет доступа", show_alert=True)
        return
    await callback_query.answer()
    orders = get_all_orders(status="новый")
    if not orders:
        await callback_query.message.answer("🟡 Новых заказов нет")
        return
    for order in orders:
        text = (
            f"🆕 ЗАКАЗ #{order[0]}\n\n"
            f"👤 {order[2]}\n📱 {order[4]}\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт.\n\n"
            f"➡️ /accept_{order[0]} — принять\n"
            f"➡️ /reject_{order[0]} — отказать"
        )
        await callback_query.message.answer(text)


@admin_dp.callback_query_handler(lambda c: c.data == "adm_active")
async def cb_admin_active(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_CHAT_ID:
        await callback_query.answer("Нет доступа", show_alert=True)
        return
    await callback_query.answer()
    active_statuses = ["принят", "цена выставлена", "подготовка модели", "оплачено наличными", "печать"]
    orders = []
    for s in active_statuses:
        orders.extend(get_all_orders(status=s))
    orders = sorted(orders, key=lambda x: x[0], reverse=True)
    if not orders:
        await callback_query.message.answer("⚡ Активных заказов нет")
        return
    text = "⚡ АКТИВНЫЕ ЗАКАЗЫ\n\n"
    for order in orders[:20]:
        emoji = STATUS_EMOJI.get(order[11], '⚪')
        text += f"{emoji} #{order[0]} | {order[2]} | {order[11]} | {order[9]} руб.\n"
    await callback_query.message.answer(text)


@admin_dp.callback_query_handler(lambda c: c.data == "adm_all")
async def cb_admin_all(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_CHAT_ID:
        await callback_query.answer("Нет доступа", show_alert=True)
        return
    await callback_query.answer()
    orders = get_all_orders()
    if not orders:
        await callback_query.message.answer("📭 Заказов нет")
        return
    text = "📋 ВСЕ ЗАКАЗЫ\n\n"
    for order in orders[:20]:
        text += f"#{order[0]} | {order[2]} | {order[5]} | {order[11]} | {order[9]} руб.\n"
    await callback_query.message.answer(text)


@admin_dp.callback_query_handler(lambda c: c.data == "adm_users")
async def cb_admin_users(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_CHAT_ID:
        await callback_query.answer("Нет доступа", show_alert=True)
        return
    await callback_query.answer()
    profiles = get_all_profiles()
    if not profiles:
        await callback_query.message.answer("👥 Пользователей пока нет")
        return
    text = f"👥 ПОЛЬЗОВАТЕЛИ ({len(profiles)})\n\n"
    for p in profiles[:30]:
        uid, name, phone, avatar, updated = p
        has_ava = "📸" if avatar and avatar != "None" and avatar.strip() else "👤"
        text += f"{has_ava} {name}\n📱 {phone}\n🆔 {uid}\n📅 {updated or '—'}\n\n"
    await callback_query.message.answer(text)


@admin_dp.message_handler(commands=['users'])
async def admin_users_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    profiles = get_all_profiles()
    if not profiles:
        await message.answer("👥 Пользователей пока нет")
        return
    text = f"👥 ПОЛЬЗОВАТЕЛИ ({len(profiles)})\n\n"
    for p in profiles[:30]:
        uid, name, phone, avatar, updated = p
        has_ava = "📸" if avatar and avatar != "None" and avatar.strip() else "👤"
        text += f"{has_ava} {name}\n📱 {phone}\n🆔 {uid}\n📅 {updated or '—'}\n\n"
    await message.answer(text)


@admin_dp.message_handler(commands=['orders'])
async def admin_orders_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    orders = get_all_orders()
    if not orders:
        await message.answer("📭 Заказов нет")
        return
    text = "📋 ВСЕ ЗАКАЗЫ\n\n"
    for order in orders[:20]:
        text += f"#{order[0]} | {order[2]} | {order[5]} | {order[11]} | {order[9]} руб.\n"
    await message.answer(text)


@admin_dp.message_handler(commands=['new'])
async def admin_new_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    orders = get_all_orders(status="новый")
    if not orders:
        await message.answer("🟡 Новых заказов нет")
        return
    for order in orders:
        text = (
            f"🆕 ЗАКАЗ #{order[0]}\n\n"
            f"👤 {order[2]}\n📱 {order[4]}\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт.\n\n"
            f"➡️ /accept_{order[0]} — принять\n"
            f"➡️ /reject_{order[0]} — отказать"
        )
        await message.answer(text)


@admin_dp.message_handler(commands=['active'])
async def admin_active_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    active_statuses = ["принят", "цена выставлена", "подготовка модели", "оплачено наличными", "печать"]
    orders = []
    for s in active_statuses:
        orders.extend(get_all_orders(status=s))
    orders = sorted(orders, key=lambda x: x[0], reverse=True)
    if not orders:
        await message.answer("⚡ Активных заказов нет")
        return
    text = "⚡ АКТИВНЫЕ ЗАКАЗЫ\n\n"
    for order in orders[:20]:
        emoji = STATUS_EMOJI.get(order[11], '⚪')
        text += f"{emoji} #{order[0]} | {order[2]} | {order[11]} | {order[9]} руб.\n"
    await message.answer(text)


@admin_dp.message_handler(lambda m: re.match(r'/accept_\d+', m.text or ''))
async def admin_bot_accept(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/accept_(\d+)', message.text)
    order_id = int(match.group(1))
    update_order_status(order_id, "принят")
    order = get_order(order_id)
    if order:
        safe_send(order[1], f"🟢 Заказ №{order_id} принят в работу!")
    await message.answer(f"✅ Заказ #{order_id} принят")


@admin_dp.message_handler(lambda m: re.match(r'/reject_\d+', m.text or ''))
async def admin_bot_reject(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/reject_(\d+)', message.text)
    order_id = int(match.group(1))
    update_order_status(order_id, "отказ")
    order = get_order(order_id)
    if order:
        safe_send(order[1], f"🔴 Заказ №{order_id} отклонён")
    await message.answer(f"❌ Заказ #{order_id} отклонён")


@admin_dp.message_handler(lambda m: re.match(r'/price_\d+\s+\d+', m.text or ''))
async def admin_bot_price(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/price_(\d+)\s+(\d+(?:\.\d+)?)', message.text)
    if not match:
        await message.answer("Формат: /price_123 500")
        return
    order_id = int(match.group(1))
    price = float(match.group(2))
    set_order_price(order_id, price)
    order = get_order(order_id)
    if order:
        total = price * order[6]
        safe_send(
            order[1],
            f"💰 Стоимость заказа №{order_id}\n\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт.\n"
            f"💵 Цена за шт.: {price} руб.\n"
            f"💲 Итого: {total} руб."
        )
    await message.answer(f"✅ Цена {price} руб. для заказа #{order_id}")


@admin_dp.message_handler(lambda m: re.match(r'/status_\d+\s+.+', m.text or ''))
async def admin_bot_status(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/status_(\d+)\s+(.+)', message.text)
    if not match:
        await message.answer("Формат: /status_123 подготовка модели")
        return
    order_id  = int(match.group(1))
    new_status = match.group(2).strip()
    update_order_status(order_id, new_status)
    order = get_order(order_id)
    if order:
        msg = STATUS_MSG.get(new_status, f"🔄 Статус заказа №{order_id}: {new_status}")
        msg = msg.format(id=order_id)
        safe_send(order[1], msg)
    await message.answer(f"✅ Заказ #{order_id} → {new_status}")


@admin_dp.callback_query_handler(lambda c: c.data.startswith('admin_confirm_'))
async def cb_admin_confirm(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_CHAT_ID:
        await callback_query.answer("Нет доступа", show_alert=True)
        return
    req_id = callback_query.data.replace('admin_confirm_', '')
    req = admin_requests.get(req_id)
    if not req:
        await callback_query.answer("Запрос не найден", show_alert=True)
        return
    req['status'] = 'confirmed'
    await callback_query.answer("✅ Доступ подтверждён")
    await callback_query.message.edit_text(
        f"✅ Доступ подтверждён\n\n"
        f"👤 {req['user_name']}\n🆔 {req['user_id']}"
    )
    safe_send(req['user_id'], "✅ Доступ администратора подтверждён! Перезайди в настройки разработчика.")


@admin_dp.callback_query_handler(lambda c: c.data.startswith('admin_deny_'))
async def cb_admin_deny(callback_query: types.CallbackQuery):
    if callback_query.from_user.id != ADMIN_CHAT_ID:
        await callback_query.answer("Нет доступа", show_alert=True)
        return
    req_id = callback_query.data.replace('admin_deny_', '')
    req = admin_requests.get(req_id)
    if not req:
        await callback_query.answer("Запрос не найден", show_alert=True)
        return
    req['status'] = 'denied'
    await callback_query.answer("❌ Доступ отклонён")
    await callback_query.message.edit_text(
        f"❌ Доступ отклонён\n\n"
        f"👤 {req['user_name']}\n🆔 {req['user_id']}"
    )
    safe_send(req['user_id'], "❌ Запрос на доступ администратора отклонён.")


# ========== FLASK ==========
flask_app = Flask(__name__)
CORS(flask_app)

os.makedirs('avatars', exist_ok=True)


@flask_app.route('/avatars/<filename>')
def get_avatar(filename):
    return send_from_directory('avatars', filename)


@flask_app.route('/upload_avatar', methods=['POST'])
def upload_avatar():
    data     = request.json
    user_id  = data.get('user_id')
    img_data = data.get('image_data', '')
    if not img_data:
        return jsonify({'ok': False, 'error': 'Нет фото'})
    if ',' in img_data:
        img_data = img_data.split(',')[1]
    avatar_url = f"data:image/jpeg;base64,{img_data}"
    conn   = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT name, phone FROM user_profiles WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    name  = row[0] if row else None
    phone = row[1] if row else None
    cursor.execute('''
        INSERT OR REPLACE INTO user_profiles (user_id, name, phone, avatar_url, updated_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, name, phone, avatar_url, get_current_time()))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'avatar_url': avatar_url})


@flask_app.route('/get_profile', methods=['POST'])
def get_profile():
    data    = request.json
    user_id = data.get('user_id')
    profile = get_user_profile(user_id)
    return jsonify({'ok': True, 'profile': profile})


@flask_app.route('/save_profile', methods=['POST'])
def save_profile():
    data    = request.json
    user_id = data.get('user_id')
    name    = data.get('name')
    phone   = data.get('phone')
    avatar  = data.get('avatar_url')
    save_user_profile(user_id, name, phone, avatar)
    return jsonify({'ok': True})


@flask_app.route('/order_files/<filename>')
def get_order_file(filename):
    return send_from_directory('order_files', filename)


@flask_app.route('/webapp_order_file', methods=['POST'])
def webapp_order_file():
    name     = request.form.get('name')
    phone    = request.form.get('phone')
    model    = request.form.get('model')
    quantity = int(request.form.get('quantity', 1))
    user_id  = request.form.get('user_id')
    username = request.form.get('username', 'нет')

    file_url = None
    uploaded = request.files.get('file')
    if uploaded and uploaded.filename:
        os.makedirs('order_files', exist_ok=True)
        ext      = os.path.splitext(uploaded.filename)[1].lower() or '.bin'
        filename = f"order_{user_id}_{uuid.uuid4().hex[:8]}{ext}"
        filepath = os.path.join('order_files', filename)
        uploaded.save(filepath)
        file_url = f"/order_files/{filename}"

    order_id = create_order(
        client_id=user_id,
        client_name=name,
        client_username=username,
        phone=phone,
        model_name=model,
        quantity=quantity,
        image_url=file_url
    )
    order = get_order(order_id)
    created_at = order[12] if order else get_current_time()

    source = request.form.get('source', 'Mini App')
    file_note = "\n📎 Файл: да" if file_url else ""
    safe_send(
        ADMIN_CHAT_ID,
        f"🆕 НОВЫЙ ЗАКАЗ #{order_id}\n\n"
        f"👤 {name}\n📱 {phone}\n📦 {model}\n🔢 {quantity} шт.{file_note}\n📱 Источник: {source}",
        bot_token=ADMIN_BOT_TOKEN
    )

    return jsonify({'ok': True, 'order_id': order_id, 'created_at': created_at})


@flask_app.route('/webapp_orders', methods=['POST'])
def webapp_orders():
    data    = request.json
    user_id = data.get('user_id')
    orders  = get_orders_by_client(user_id)
    result  = []
    for order in orders:
        created_at = order[12] or get_current_time()
        result.append({
            'id':          order[0],
            'model_name':  order[5],
            'quantity':    order[6],
            'total_price': order[9],
            'status':      order[11],
            'created_at':  created_at
        })
    return jsonify({'orders': result})


@flask_app.route('/admin_orders', methods=['GET'])
def admin_orders():
    status = request.args.get('status', 'all')
    if status == 'new':
        orders = get_all_orders(status='новый')
    elif status == 'active':
        active = ["принят", "цена выставлена", "подготовка модели", "оплачено наличными", "печать"]
        orders = []
        for s in active:
            orders.extend(get_all_orders(status=s))
        orders = sorted(orders, key=lambda x: x[0], reverse=True)
    else:
        orders = get_all_orders()

    result = []
    for order in orders:
        result.append({
            'id':          order[0],
            'client_id':   order[1],
            'client_name': order[2],
            'username':    order[3],
            'phone':       order[4],
            'model_name':  order[5],
            'quantity':    order[6],
            'image_url':   order[7] or None,
            'price_per_unit': order[8],
            'total_price': order[9],
            'status':      order[11],
            'created_at':  order[12] or 'Дата не указана',
            'updated_at':  order[13] or ''
        })
    return jsonify({'orders': result})


@flask_app.route('/admin_users', methods=['GET'])
def admin_users():
    profiles = get_all_profiles()
    result = []
    for p in profiles:
        result.append({
            'user_id':   p[0],
            'name':      p[1],
            'phone':     p[2],
            'avatar_url': p[3],
            'updated_at': p[4]
        })
    return jsonify({'users': result})


@flask_app.route('/admin_user_orders/<int:user_id>', methods=['GET'])
def admin_user_orders(user_id):
    orders = get_orders_by_client(user_id)
    result = []
    for order in orders:
        result.append({
            'id':          order[0],
            'model_name':  order[5],
            'quantity':    order[6],
            'price_per_unit': order[8],
            'total_price': order[9],
            'status':      order[11],
            'created_at':  order[12] or '—',
            'updated_at':  order[13] or ''
        })
    return jsonify({'orders': result})


@flask_app.route('/admin_accept', methods=['POST'])
def admin_accept():
    data     = request.json
    order_id = data.get('order_id')
    update_order_status(order_id, "принят")
    order = get_order(order_id)
    if order:
        safe_send(order[1], f"🟢 Заказ №{order_id} принят в работу!")
    return jsonify({'ok': True})


@flask_app.route('/admin_reject', methods=['POST'])
def admin_reject():
    data     = request.json
    order_id = data.get('order_id')
    update_order_status(order_id, "отказ")
    order = get_order(order_id)
    if order:
        safe_send(order[1], f"🔴 Заказ №{order_id} отклонён")
    return jsonify({'ok': True})


@flask_app.route('/admin_price', methods=['POST'])
def admin_price():
    data     = request.json
    order_id = data.get('order_id')
    price    = float(data.get('price'))
    set_order_price(order_id, price)
    order = get_order(order_id)
    if order:
        total = price * order[6]
        safe_send(
            order[1],
            f"💰 Стоимость заказа №{order_id}\n\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт.\n"
            f"💵 Цена за шт.: {price} руб.\n"
            f"💲 Итого: {total} руб."
        )
    return jsonify({'ok': True})


@flask_app.route('/admin_status', methods=['POST'])
def admin_status():
    data       = request.json
    order_id   = data.get('order_id')
    new_status = data.get('status')
    update_order_status(order_id, new_status)
    order = get_order(order_id)
    if order:
        msg = STATUS_MSG.get(new_status, f"🔄 Статус заказа №{order_id}: {new_status}")
        msg = msg.format(id=order_id)
        safe_send(order[1], msg)
    return jsonify({'ok': True})


@flask_app.route('/')
def health_check():
    return "3Dprinti бот работает! ✅"


@flask_app.route('/request_admin', methods=['POST'])
def request_admin():
    data = request.json
    user_id = data.get('user_id')
    password = data.get('password', '')
    user_name = data.get('name', 'Неизвестный')

    if password != '458307pP':
        return jsonify({'ok': False, 'error': 'Неверный пароль'})

    req_id = f"req_{user_id}_{int(datetime.now().timestamp())}"
    admin_requests[req_id] = {'user_id': user_id, 'user_name': user_name, 'status': 'pending'}

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_confirm_{req_id}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"admin_deny_{req_id}")
    )

    safe_send(
        ADMIN_CHAT_ID,
        f"🔑 Запрос на доступ админа\n\n"
        f"👤 Пользователь: {user_name}\n"
        f"🆔 ID: {user_id}\n\n"
        f"Подтвердить доступ?",
        bot_token=ADMIN_BOT_TOKEN,
        reply_markup=kb
    )
    return jsonify({'ok': True, 'req_id': req_id})


@flask_app.route('/check_admin_status', methods=['POST'])
def check_admin_status():
    data = request.json
    req_id = data.get('req_id', '')
    req = admin_requests.get(req_id)
    if not req:
        return jsonify({'ok': True, 'status': 'denied'})
    return jsonify({'ok': True, 'status': req['status']})


# ========== ЗАПУСК ==========
def run_web_server():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)


def run_bot(dp):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    start_polling(dp, skip_updates=True)


if __name__ == '__main__':
    init_db()
    print("🌐 Запуск веб-сервера...")
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    print("🤖 Запуск клиентского бота...")
    client_thread = threading.Thread(target=run_bot, args=(client_dp,), daemon=True)
    client_thread.start()
    print("🛠 Запуск админского бота...")
    admin_thread = threading.Thread(target=run_bot, args=(admin_dp,), daemon=True)
    admin_thread.start()
    print("✅ Все сервисы запущены!")
    # Главный поток остаётся живым
    try:
        while True:
            threading.Event().wait(timeout=60)
    except KeyboardInterrupt:
        print("⛔ Остановка...")
