import asyncio
import sqlite3
import os
import re
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.executor import start_polling
import threading

# ========== КОНФИГУРАЦИЯ ==========
CLIENT_BOT_TOKEN = os.environ.get('CLIENT_BOT_TOKEN')
ADMIN_BOT_TOKEN = os.environ.get('ADMIN_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', 0))

# Часовой пояс Минска (UTC+3)
MINSK_TZ = timezone(timedelta(hours=3))

# ========== БАЗА ДАННЫХ ==========
DB_NAME = "orders.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER,
            client_name TEXT,
            client_username TEXT,
            phone TEXT,
            model_name TEXT,
            quantity INTEGER,
            image_url TEXT,
            price_per_unit REAL DEFAULT 0,
            total_price REAL DEFAULT 0,
            paid_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'новый',
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

def create_order(client_id, client_name, client_username, phone, model_name, quantity, image_url=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now = datetime.now(MINSK_TZ).isoformat()
    cursor.execute('''
        INSERT INTO orders (client_id, client_name, client_username, phone, model_name, quantity, image_url, created_at, updated_at)
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
    quantity = cursor.fetchone()[0]
    total_price = quantity * price_per_unit
    cursor.execute('''
        UPDATE orders SET price_per_unit = ?, total_price = ?, updated_at = ? WHERE id = ?
    ''', (price_per_unit, total_price, datetime.now(MINSK_TZ).isoformat(), order_id))
    conn.commit()
    conn.close()

def update_order_status(order_id, status):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE orders SET status = ?, updated_at = ? WHERE id = ?
    ''', (status, datetime.now(MINSK_TZ).isoformat(), order_id))
    conn.commit()
    conn.close()
    print(f"✅ Заказ {order_id} → статус '{status}'")

def get_order(order_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
    order = cursor.fetchone()
    conn.close()
    return order

def get_orders_by_client(client_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM orders WHERE client_id = ? ORDER BY id DESC', (client_id,))
    orders = cursor.fetchall()
    conn.close()
    return orders

def get_all_orders(status=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if status:
        cursor.execute('SELECT * FROM orders WHERE status = ? ORDER BY id DESC', (status,))
    else:
        cursor.execute('SELECT * FROM orders ORDER BY id DESC')
    orders = cursor.fetchall()
    conn.close()
    return orders

# ========== КЛИЕНТСКИЙ БОТ ==========
client_bot = Bot(token=CLIENT_BOT_TOKEN)
client_storage = MemoryStorage()
client_dp = Dispatcher(client_bot, storage=client_storage)
client_dp.middleware.setup(LoggingMiddleware())

class OrderForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_model = State()
    waiting_for_quantity = State()

client_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
client_keyboard.add(KeyboardButton("🛒 Сделать заказ"))
client_keyboard.add(KeyboardButton("📋 Мои заказы"))

@client_dp.message_handler(commands=['start'])
async def client_start(message: types.Message):
    await message.answer(
        "🖨️ *3Dprinti*\n\n"
        "Сервис 3D-печати на заказ.\n"
        "Нажмите «🛒 Сделать заказ», чтобы оформить заказ.",
        reply_markup=client_keyboard,
        parse_mode="MarkdownV2"
    )

@client_dp.message_handler(lambda message: message.text == "🛒 Сделать заказ")
async def make_order(message: types.Message):
    await OrderForm.waiting_for_name.set()
    await message.answer("Введите ваше имя:")

@client_dp.message_handler(state=OrderForm.waiting_for_name)
async def get_name(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['name'] = message.text
    await OrderForm.next()
    await message.answer("Введите ваш номер телефона:\nПример: +375291234567")

@client_dp.message_handler(state=OrderForm.waiting_for_phone)
async def get_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    async with state.proxy() as data:
        data['phone'] = phone
    await OrderForm.next()
    await message.answer("Введите название модели или описание:")

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
        order_id = create_order(
            client_id=message.from_user.id,
            client_name=data['name'],
            client_username=message.from_user.username or "нет",
            phone=data['phone'],
            model_name=data['model'],
            quantity=quantity
        )
        
        await message.answer(
            f"✅ *Заказ №{order_id} создан!*\n\n"
            f"📦 Модель: {data['model']}\n"
            f"🔢 Количество: {quantity} шт.\n\n"
            f"Статус заказа можно отслеживать в разделе «Мои заказы».",
            parse_mode="MarkdownV2"
        )
        
        # Уведомление админу
        await client_bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 *НОВЫЙ ЗАКАЗ #{order_id}*\n\n"
            f"👤 {data['name']}\n"
            f"📱 {data['phone']}\n"
            f"📦 {data['model']}\n"
            f"🔢 {quantity} шт.",
            parse_mode="MarkdownV2"
        )
        await state.finish()
    except ValueError:
        await message.answer("❌ Ошибка! Введите число.")

@client_dp.message_handler(lambda message: message.text == "📋 Мои заказы")
async def my_orders(message: types.Message):
    orders = get_orders_by_client(message.from_user.id)
    if not orders:
        await message.answer("📭 У вас пока нет заказов.")
        return
    
    status_emoji = {
        'новый': '🟡', 'принят': '🟢', 'отказ': '🔴',
        'подготовка модели': '🟠', 'цена выставлена': '💰',
        'оплачено наличными': '✅', 'печать': '🖨️', 'готова': '🎉'
    }
    
    text = "📋 *МОИ ЗАКАЗЫ*\n\n"
    for order in orders:
        emoji = status_emoji.get(order[9], '⚪')
        total = order[8] if order[8] else 0
        text += f"{emoji} *Заказ №{order[0]}*\n"
        text += f"   📦 {order[5]}\n"
        text += f"   🔢 {order[6]} шт.\n"
        if total > 0:
            text += f"   💰 {total} руб.\n"
        text += f"   📍 {order[9]}\n\n"
    
    await message.answer(text, parse_mode="MarkdownV2")

# ========== АДМИНСКИЕ КОМАНДЫ (ПИСЬМЕННАЯ ВЕРСИЯ) ==========
admin_bot = Bot(token=ADMIN_BOT_TOKEN)

@client_dp.message_handler(commands=['admin_orders'], chat_id=ADMIN_CHAT_ID)
async def admin_all_orders(message: types.Message):
    orders = get_all_orders()
    if not orders:
        await message.answer("📭 Заказов нет")
        return
    text = "📋 *ВСЕ ЗАКАЗЫ*\n\n"
    for order in orders[:20]:
        text += f"#{order[0]} | {order[2]} | {order[5]} | {order[9]} | {order[8]} руб.\n"
    await message.answer(text, parse_mode="MarkdownV2")

@client_dp.message_handler(commands=['admin_new'], chat_id=ADMIN_CHAT_ID)
async def admin_new_orders(message: types.Message):
    orders = get_all_orders(status="новый")
    if not orders:
        await message.answer("🟡 Новых заказов нет")
        return
    for order in orders:
        text = (
            f"🆕 *ЗАКАЗ #{order[0]}*\n\n"
            f"👤 {order[2]}\n📱 {order[4]}\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт.\n\n"
            f"➡️ /accept_{order[0]} — принять\n"
            f"➡️ /reject_{order[0]} — отказать"
        )
        await message.answer(text, parse_mode="MarkdownV2")

@client_dp.message_handler(commands=['accept'], chat_id=ADMIN_CHAT_ID)
async def admin_accept_order(message: types.Message):
    match = re.match(r"/accept_(\d+)", message.text)
    if not match:
        await message.answer("Формат: /accept_123")
        return
    order_id = int(match.group(1))
    update_order_status(order_id, "принят")
    order = get_order(order_id)
    await client_bot.send_message(order[1], f"🟢 *Заказ №{order_id} принят в работу!*", parse_mode="MarkdownV2")
    await message.answer(f"✅ Заказ #{order_id} принят")

@client_dp.message_handler(commands=['reject'], chat_id=ADMIN_CHAT_ID)
async def admin_reject_order(message: types.Message):
    match = re.match(r"/reject_(\d+)", message.text)
    if not match:
        await message.answer("Формат: /reject_123")
        return
    order_id = int(match.group(1))
    update_order_status(order_id, "отказ")
    order = get_order(order_id)
    await client_bot.send_message(order[1], f"🔴 *Заказ №{order_id} отклонён*", parse_mode="MarkdownV2")
    await message.answer(f"❌ Заказ #{order_id} отклонён")

@client_dp.message_handler(commands=['price'], chat_id=ADMIN_CHAT_ID)
async def admin_price_order(message: types.Message):
    match = re.match(r"/price_(\d+)\s+(\d+)", message.text)
    if not match:
        await message.answer("Формат: /price_123 500")
        return
    order_id = int(match.group(1))
    price = float(match.group(2))
    set_order_price(order_id, price)
    order = get_order(order_id)
    total = price * order[6]
    await client_bot.send_message(
        order[1],
        f"💰 *Стоимость заказа №{order_id}*\n\n"
        f"📦 {order[5]}\n🔢 {order[6]} шт.\n"
        f"💵 Цена за шт.: {price} руб.\n"
        f"💲 *Итого: {total} руб.*",
        parse_mode="MarkdownV2"
    )
    await message.answer(f"✅ Цена {price} руб. для заказа #{order_id}")

@client_dp.message_handler(commands=['status'], chat_id=ADMIN_CHAT_ID)
async def admin_status_order(message: types.Message):
    match = re.match(r"/status_(\d+)\s+(.+)", message.text)
    if not match:
        await message.answer("Формат: /status_123 подготовка модели")
        return
    order_id = int(match.group(1))
    new_status = match.group(2)
    update_order_status(order_id, new_status)
    order = get_order(order_id)
    await client_bot.send_message(order[1], f"🔄 *Статус заказа №{order_id}:* {new_status}", parse_mode="MarkdownV2")
    await message.answer(f"✅ Заказ #{order_id} → {new_status}")

# ========== FLASK ДЛЯ MINI APP ==========
flask_app = Flask(__name__)
CORS(flask_app)

@flask_app.route('/webapp_order_file', methods=['POST'])
def webapp_order_file():
    name = request.form.get('name')
    phone = request.form.get('phone')
    model = request.form.get('model')
    quantity = int(request.form.get('quantity', 1))
    user_id = request.form.get('user_id')
    username = request.form.get('username', 'нет')
    
    order_id = create_order(
        client_id=user_id,
        client_name=name,
        client_username=username,
        phone=phone,
        model_name=model,
        quantity=quantity
    )
    
    async def notify():
        await client_bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 *НОВЫЙ ЗАКАЗ #{order_id} из Mini App!*\n\n"
            f"👤 {name}\n📱 {phone}\n📦 {model}\n🔢 {quantity} шт.",
            parse_mode="MarkdownV2"
        )
    asyncio.run(notify())
    
    return jsonify({'ok': True, 'order_id': order_id})

@flask_app.route('/webapp_orders', methods=['POST'])
def webapp_orders():
    data = request.json
    user_id = data.get('user_id')
    orders = get_orders_by_client(user_id)
    result = []
    for order in orders:
        result.append({
            'id': order[0],
            'model_name': order[5],
            'quantity': order[6],
            'total_price': order[8],
            'status': order[9],
            'created_at': order[10]
        })
    return jsonify({'orders': result})

@flask_app.route('/admin_orders', methods=['GET'])
def admin_orders():
    status = request.args.get('status', 'all')
    if status == 'new':
        orders = get_all_orders(status='новый')
    elif status == 'active':
        active_statuses = ["принят", "цена выставлена", "подготовка модели", "оплачено наличными", "печать"]
        orders = []
        for s in active_statuses:
            orders.extend(get_all_orders(status=s))
        orders = sorted(orders, key=lambda x: x[0], reverse=True)
    else:
        orders = get_all_orders()
    
    result = []
    for order in orders:
        result.append({
            'id': order[0],
            'client_name': order[2],
            'phone': order[4],
            'model_name': order[5],
            'quantity': order[6],
            'total_price': order[8],
            'status': order[9],
            'created_at': order[10]
        })
    return jsonify({'orders': result})

@flask_app.route('/admin_accept', methods=['POST'])
def admin_accept():
    data = request.json
    order_id = data.get('order_id')
    update_order_status(order_id, "принят")
    order = get_order(order_id)
    
    async def notify():
        await client_bot.send_message(order[1], f"🟢 *Заказ №{order_id} принят в работу!*", parse_mode="MarkdownV2")
    asyncio.run(notify())
    
    return jsonify({'ok': True})

@flask_app.route('/admin_reject', methods=['POST'])
def admin_reject():
    data = request.json
    order_id = data.get('order_id')
    update_order_status(order_id, "отказ")
    order = get_order(order_id)
    
    async def notify():
        await client_bot.send_message(order[1], f"🔴 *Заказ №{order_id} отклонён*", parse_mode="MarkdownV2")
    asyncio.run(notify())
    
    return jsonify({'ok': True})

@flask_app.route('/admin_price', methods=['POST'])
def admin_price():
    data = request.json
    order_id = data.get('order_id')
    price = float(data.get('price'))
    set_order_price(order_id, price)
    order = get_order(order_id)
    total = price * order[6]
    
    async def notify():
        await client_bot.send_message(
            order[1],
            f"💰 *Стоимость заказа №{order_id}*\n\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт.\n"
            f"💵 Цена за шт.: {price} руб.\n"
            f"💲 *Итого: {total} руб.*",
            parse_mode="MarkdownV2"
        )
    asyncio.run(notify())
    
    return jsonify({'ok': True})

@flask_app.route('/admin_status', methods=['POST'])
def admin_status():
    data = request.json
    order_id = data.get('order_id')
    new_status = data.get('status')
    update_order_status(order_id, new_status)
    order = get_order(order_id)
    
    async def notify():
        await client_bot.send_message(order[1], f"🔄 *Статус заказа №{order_id}:* {new_status}", parse_mode="MarkdownV2")
    asyncio.run(notify())
    
    return jsonify({'ok': True})

@flask_app.route('/')
def health_check():
    return "3Dprinti бот работает!"

def run_web_server():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

def run_bot(dp):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    start_polling(dp, skip_updates=True)

if __name__ == '__main__':
    init_db()
    print("🤖 Запуск клиентского бота...")
    print("🤖 Запуск админских команд...")
    print("🌐 Запуск веб-сервера...")
    
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    
    bot_thread = threading.Thread(target=run_bot, args=(client_dp,))
    bot_thread.start()
    
    bot_thread.join()
