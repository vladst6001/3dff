import asyncio
import re
import sqlite3
import os
import threading
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.executor import start_polling
from flask import Flask

# ========== КОНФИГУРАЦИЯ ==========
CLIENT_BOT_TOKEN = os.environ.get('CLIENT_BOT_TOKEN')
ADMIN_BOT_TOKEN = os.environ.get('ADMIN_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', 0))

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
            price_per_unit REAL DEFAULT 0,
            total_price REAL DEFAULT 0,
            paid_amount REAL DEFAULT 0,
            payment_status TEXT DEFAULT 'не оплачен',
            status TEXT DEFAULT 'новый',
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

def create_order(client_id, client_name, client_username, phone, model_name, quantity):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute('''
        INSERT INTO orders (client_id, client_name, client_username, phone, model_name, quantity, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (client_id, client_name, client_username, phone, model_name, quantity, now, now))
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
    ''', (price_per_unit, total_price, datetime.now().isoformat(), order_id))
    conn.commit()
    conn.close()

def update_payment(order_id, paid_amount, payment_status):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE orders SET paid_amount = ?, payment_status = ?, updated_at = ? WHERE id = ?
    ''', (paid_amount, payment_status, datetime.now().isoformat(), order_id))
    conn.commit()
    conn.close()

def update_order_status(order_id, status):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE orders SET status = ?, updated_at = ? WHERE id = ?
    ''', (status, datetime.now().isoformat(), order_id))
    conn.commit()
    conn.close()

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

def get_new_orders_count():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM orders WHERE status = "новый"')
    count = cursor.fetchone()[0]
    conn.close()
    return count

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
    waiting_for_model_name = State()
    waiting_for_quantity = State()

client_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
client_keyboard.add(KeyboardButton("🛒 Сделать заказ"))
client_keyboard.add(KeyboardButton("📋 Мои заказы"))

def payment_cash_keyboard(order_id, total_price):
    buttons = [
        InlineKeyboardButton(text="✅ Подтверждаю оплату наличными", callback_data=f"cash_confirm_{order_id}"),
        InlineKeyboardButton(text="❌ Отмена заказа (нет денег)", callback_data=f"cash_cancel_{order_id}"),
    ]
    keyboard_inline = InlineKeyboardMarkup(row_width=1)
    keyboard_inline.add(*buttons)
    return keyboard_inline

def validate_belarus_phone(phone):
    phone_clean = re.sub(r'[\s\-\(\)]+', '', phone)
    pattern = r'^(\+375|80|375)?(29|25|33|44|17|16)\d{7}$'
    if re.match(pattern, phone_clean):
        if phone_clean.startswith('80'):
            phone_clean = '+375' + phone_clean[2:]
        elif phone_clean.startswith('375'):
            phone_clean = '+' + phone_clean
        elif not phone_clean.startswith('+'):
            phone_clean = '+375' + phone_clean
        return phone_clean
    return None

@client_dp.message_handler(commands=['start'])
async def client_start(message: types.Message):
    await message.answer("🖨️ Добро пожаловать в сервис 3D-печати!\n\nВыберите действие:", reply_markup=client_keyboard)

@client_dp.message_handler(lambda message: message.text == "🛒 Сделать заказ")
async def make_order(message: types.Message):
    await OrderForm.waiting_for_name.set()
    await message.answer("Введите ваше Имя и Фамилию:")

@client_dp.message_handler(state=OrderForm.waiting_for_name)
async def get_name(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['name'] = message.text
    await OrderForm.next()
    await message.answer("Введите номер телефона (белорусский):\nПример: +375291234567")

@client_dp.message_handler(state=OrderForm.waiting_for_phone)
async def get_phone(message: types.Message, state: FSMContext):
    phone = validate_belarus_phone(message.text)
    if not phone:
        await message.answer("❌ Неверный формат! Пример: +375291234567")
        return
    async with state.proxy() as data:
        data['phone'] = phone
    await OrderForm.next()
    await message.answer("Введите название модели:")

@client_dp.message_handler(state=OrderForm.waiting_for_model_name)
async def get_model(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['model_name'] = message.text
    await OrderForm.next()
    await message.answer("Введите количество (цифрой):")

@client_dp.message_handler(state=OrderForm.waiting_for_quantity)
async def get_quantity(message: types.Message, state: FSMContext):
    try:
        quantity = int(message.text)
        if quantity < 1:
            await message.answer("Количество должно быть больше 0")
            return
        
        data = await state.get_data()
        order_id = create_order(
            message.from_user.id, 
            data['name'],
            message.from_user.username or "нет", 
            data['phone'],
            data['model_name'], 
            quantity
        )
        
        await message.answer(
            f"✅ Заказ #{order_id} создан!\n\n"
            f"📦 Модель: {data['model_name']}\n"
            f"🔢 Количество: {quantity} шт.\n\n"
            f"⏳ Заказ поставлен в очередь. Статус можно отслеживать в 'Мои заказы'",
            reply_markup=client_keyboard
        )
        
        # Уведомление админу (через админ-бота)
        admin_bot_temp = Bot(token=ADMIN_BOT_TOKEN)
        await admin_bot_temp.send_message(
            ADMIN_CHAT_ID,
            f"🆕 НОВЫЙ ЗАКАЗ #{order_id}!\n\n"
            f"👤 Клиент: {data['name']}\n"
            f"📱 Телефон: {data['phone']}\n"
            f"📦 Модель: {data['model_name']}\n"
            f"🔢 Количество: {quantity} шт."
        )
        await admin_bot_temp.close()
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
        'подготовка модели': '🟠', 'ожидает оплаты наличными': '💰',
        'оплачено наличными': '✅', 'подготовка принтера': '🔧',
        'печать': '🖨️', 'готова': '🎉'
    }
    
    text = "📋 ВАШИ ЗАКАЗЫ:\n\n"
    for order in orders:
        emoji = status_emoji.get(order[9], '⚪')
        total = order[8] if order[8] else 0
        text += f"{emoji} Заказ #{order[0]}\n"
        text += f"   📦 {order[5]}\n"
        text += f"   🔢 {order[6]} шт.\n"
        if total > 0:
            text += f"   💰 {total} руб.\n"
        text += f"   📍 {order[9]}\n\n"
    
    await message.answer(text, reply_markup=client_keyboard)

@client_dp.callback_query_handler(lambda c: c.data.startswith('cash_confirm_'))
async def cash_confirm(callback_query: types.CallbackQuery):
    order_id = int(callback_query.data.split("_")[2])
    order = get_order(order_id)
    
    update_payment(order_id, order[8], "полная")
    update_order_status(order_id, "оплачено наличными")
    
    await client_bot.edit_message_text(
        f"✅ Заказ #{order_id}\n\n"
        f"Вы подтвердили оплату наличными.\n"
        f"Спасибо! Заказ передан в печать.\n\n"
        f"Сумма: {order[8]} руб.",
        callback_query.message.chat.id,
        callback_query.message.message_id
    )
    
    admin_bot_temp = Bot(token=ADMIN_BOT_TOKEN)
    await admin_bot_temp.send_message(
        ADMIN_CHAT_ID,
        f"💰 ЗАКАЗ #{order_id} ОПЛАЧЕН НАЛИЧНЫМИ!\n\n"
        f"👤 {order[2]}\n📱 {order[4]}\nСумма: {order[8]} руб."
    )
    await admin_bot_temp.close()
    await callback_query.answer()

@client_dp.callback_query_handler(lambda c: c.data.startswith('cash_cancel_'))
async def cash_cancel(callback_query: types.CallbackQuery):
    order_id = int(callback_query.data.split("_")[2])
    update_order_status(order_id, "отказ")
    await client_bot.edit_message_text(
        f"❌ Заказ #{order_id} отменён.",
        callback_query.message.chat.id,
        callback_query.message.message_id
    )
    admin_bot_temp = Bot(token=ADMIN_BOT_TOKEN)
    await admin_bot_temp.send_message(ADMIN_CHAT_ID, f"❌ Заказ #{order_id} отменён клиентом")
    await admin_bot_temp.close()
    await callback_query.answer()

# ========== АДМИНСКИЙ БОТ ==========
admin_bot = Bot(token=ADMIN_BOT_TOKEN)
admin_dp = Dispatcher(admin_bot)
admin_dp.middleware.setup(LoggingMiddleware())

def admin_main_menu():
    buttons = [
        [InlineKeyboardButton(text="📋 Новые заказы", callback_data="admin_new_orders")],
        [InlineKeyboardButton(text="📊 Все заказы", callback_data="admin_all_orders")],
        [InlineKeyboardButton(text="🔄 Активные заказы", callback_data="admin_active_orders")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_order_actions(order_id):
    buttons = [
        [InlineKeyboardButton(text="🟢 Принять", callback_data=f"admin_accept_{order_id}")],
        [InlineKeyboardButton(text="🔴 Отказать", callback_data=f"admin_reject_{order_id}")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"admin_price_{order_id}")],
        [InlineKeyboardButton(text="📊 Статус", callback_data=f"admin_status_menu_{order_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_status_menu(order_id):
    buttons = [
        [InlineKeyboardButton(text="🟠 Подготовка модели", callback_data=f"admin_status_{order_id}_подготовка модели")],
        [InlineKeyboardButton(text="🔧 Подготовка принтера", callback_data=f"admin_status_{order_id}_подготовка принтера")],
        [InlineKeyboardButton(text="🖨️ Печать", callback_data=f"admin_status_{order_id}_печать")],
        [InlineKeyboardButton(text="🎉 Готово", callback_data=f"admin_status_{order_id}_готова")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_back_{order_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

temp_price_order = {}

@admin_dp.message_handler(commands=['start'])
async def admin_start(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("❌ Доступ запрещён")
        return
    await message.answer("👋 Панель администратора 3D-печати", reply_markup=admin_main_menu())

@admin_dp.callback_query_handler(lambda c: c.data == "admin_new_orders")
async def admin_new_orders(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ Доступ запрещён")
        return
    orders = get_all_orders(status="новый")
    if not orders:
        await callback.message.edit_text("🟡 Новых заказов нет", reply_markup=admin_main_menu())
        await callback.answer()
        return
    for order in orders:
        text = f"🆕 ЗАКАЗ #{order[0]}\n\n👤 {order[2]}\n📱 {order[4]}\n📦 {order[5]}\n🔢 {order[6]} шт."
        await callback.message.answer(text, reply_markup=admin_order_actions(order[0]))
    await callback.answer()

@admin_dp.callback_query_handler(lambda c: c.data == "admin_all_orders")
async def admin_all_orders(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ Доступ запрещён")
        return
    orders = get_all_orders()
    if not orders:
        await callback.message.edit_text("📭 Заказов нет", reply_markup=admin_main_menu())
        await callback.answer()
        return
    text = "📋 ВСЕ ЗАКАЗЫ:\n\n"
    for order in orders[:20]:
        text += f"#{order[0]} | {order[2]} | {order[5]} | {order[9]} | {order[8]} руб.\n"
    await callback.message.edit_text(text, reply_markup=admin_main_menu())
    await callback.answer()

@admin_dp.callback_query_handler(lambda c: c.data == "admin_active_orders")
async def admin_active_orders(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ Доступ запрещён")
        return
    statuses = ["принят", "цена выставлена", "подготовка модели", "ожидает оплаты наличными", "оплачено наличными", "подготовка принтера", "печать"]
    active = []
    for s in statuses:
        active.extend(get_all_orders(status=s))
    if not active:
        await callback.message.edit_text("🟡 Нет активных заказов", reply_markup=admin_main_menu())
        await callback.answer()
        return
    for order in active:
        text = f"🔄 ЗАКАЗ #{order[0]}\n\n👤 {order[2]}\n📦 {order[5]}\n📍 {order[9]}\n💰 {order[8]} руб."
        await callback.message.answer(text, reply_markup=admin_order_actions(order[0]))
    await callback.answer()

@admin_dp.callback_query_handler(lambda c: c.data.startswith("admin_accept_"))
async def admin_accept(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ Доступ запрещён")
        return
    order_id = int(callback.data.split("_")[2])
    update_order_status(order_id, "принят")
    order = get_order(order_id)
    await client_bot.send_message(order[1], f"🟢 Ваш заказ #{order_id} ПРИНЯТ в работу!")
    await callback.message.edit_text(f"✅ Заказ #{order_id} принят", reply_markup=admin_order_actions(order_id))
    await callback.answer()

@admin_dp.callback_query_handler(lambda c: c.data.startswith("admin_reject_"))
async def admin_reject(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ Доступ запрещён")
        return
    order_id = int(callback.data.split("_")[2])
    update_order_status(order_id, "отказ")
    order = get_order(order_id)
    await client_bot.send_message(order[1], f"🔴 Ваш заказ #{order_id} ОТКЛОНЁН")
    await callback.message.edit_text(f"❌ Заказ #{order_id} отклонён", reply_markup=admin_main_menu())
    await callback.answer()

@admin_dp.callback_query_handler(lambda c: c.data.startswith("admin_price_"))
async def admin_price(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ Доступ запрещён")
        return
    order_id = int(callback.data.split("_")[2])
    temp_price_order['order_id'] = order_id
    await callback.message.answer(f"💰 Введите цену для заказа #{order_id} (за 1 шт.):\nПример: 500")
    await callback.answer()

@admin_dp.callback_query_handler(lambda c: c.data.startswith("admin_status_menu_"))
async def admin_status_menu(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ Доступ запрещён")
        return
    order_id = int(callback.data.split("_")[3])
    await callback.message.edit_text(f"📊 Выберите статус для заказа #{order_id}:", reply_markup=admin_status_menu(order_id))
    await callback.answer()

@admin_dp.callback_query_handler(lambda c: c.data.startswith("admin_status_"))
async def admin_status_change(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ Доступ запрещён")
        return
    parts = callback.data.split("_")
    order_id = int(parts[2])
    new_status = parts[3]
    update_order_status(order_id, new_status)
    order = get_order(order_id)
    await client_bot.send_message(order[1], f"🔄 Статус вашего заказа #{order_id}: {new_status}")
    
    if new_status == "подготовка модели":
        await client_bot.send_message(
            order[1],
            f"🟠 ЗАКАЗ #{order_id} ГОТОВ К ОПЛАТЕ\n\n"
            f"📦 Модель: {order[5]}\n🔢 {order[6]} шт.\n"
            f"💰 Сумма к оплате: {order[8]} руб.\n\n"
            f"💵 ОПЛАТА ТОЛЬКО НАЛИЧНЫМИ\n\n"
            f"✅ При готовности оплатить - нажмите кнопку ниже",
            reply_markup=payment_cash_keyboard(order_id, order[8])
        )
    
    await callback.message.edit_text(f"✅ Заказ #{order_id} → {new_status}", reply_markup=admin_order_actions(order_id))
    await callback.answer()

@admin_dp.callback_query_handler(lambda c: c.data.startswith("admin_back_"))
async def admin_back(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("❌ Доступ запрещён")
        return
    order_id = int(callback.data.split("_")[2])
    await callback.message.edit_text(f"🔙 Заказ #{order_id}", reply_markup=admin_order_actions(order_id))
    await callback.answer()

@admin_dp.message_handler(content_types=['text'], chat_id=ADMIN_CHAT_ID)
async def handle_price_input(message: types.Message):
    if 'order_id' in temp_price_order:
        order_id = temp_price_order['order_id']
        try:
            price = float(message.text.strip())
            order = get_order(order_id)
            if order[9] != "принят":
                await message.answer(f"⚠️ Заказ #{order_id} в статусе '{order[9]}'. Сначала примите заказ.")
                del temp_price_order['order_id']
                return
            set_order_price(order_id, price)
            update_order_status(order_id, "цена выставлена")
            total = price * order[6]
            await client_bot.send_message(
                order[1],
                f"💵 МЕНЕДЖЕР ВЫСТАВИЛ СТОИМОСТЬ\n\n"
                f"📦 Заказ #{order_id}\n"
                f"🔢 Модель: {order[5]}\n"
                f"💰 Количество: {order[6]} шт.\n"
                f"💵 Цена за шт.: {price} руб.\n"
                f"💲 ИТОГО: {total} руб."
            )
            await message.answer(f"✅ Цена {price} руб. для заказа #{order_id}\n💲 Итого: {total} руб.")
        except ValueError:
            await message.answer("❌ Введите число!")
        del temp_price_order['order_id']

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Бот работает! Клиентский и админский боты запущены."

def run_web_server():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port)

# ========== ЗАПУСК ВСЕГО ==========
def run_client_bot():
    start_polling(client_dp, skip_updates=True)

def run_admin_bot():
    start_polling(admin_dp, skip_updates=True)

if __name__ == '__main__':
    init_db()
    print("🤖 Запуск клиентского бота...")
    print("🤖 Запуск админского бота...")
    print("🌐 Запуск веб-сервера для Render...")
    
    # Запускаем веб-сервер в отдельном потоке
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    
    # Запускаем ботов в отдельных потоках
    client_thread = threading.Thread(target=run_client_bot)
    admin_thread = threading.Thread(target=run_admin_bot)
    
    client_thread.start()
    admin_thread.start()
    
    client_thread.join()
    admin_thread.join()
