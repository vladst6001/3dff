import asyncio
import re
import sqlite3
import os
from datetime import datetime
from threading import Thread
from flask import Flask

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ========== КОНФИГУРАЦИЯ ==========
CLIENT_BOT_TOKEN = os.environ.get('CLIENT_BOT_TOKEN')
ADMIN_BOT_TOKEN = os.environ.get('ADMIN_BOT_TOKEN')
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', 0))

# Проверка токенов при запуске
if not CLIENT_BOT_TOKEN or not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
    print("❌ ОШИБКА: Не все переменные окружения заданы!")
    print(f"CLIENT_BOT_TOKEN: {'✅' if CLIENT_BOT_TOKEN else '❌'}")
    print(f"ADMIN_BOT_TOKEN: {'✅' if ADMIN_BOT_TOKEN else '❌'}")
    print(f"ADMIN_CHAT_ID: {'✅' if ADMIN_CHAT_ID else '❌'}")
    exit(1)

print("✅ Все переменные окружения найдены")

# Flask для health check
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Бот работает!", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# Запускаем Flask в отдельном потоке
Thread(target=run_flask, daemon=True).start()

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
    print("✅ База данных инициализирована")

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

# ========== СОЗДАНИЕ БОТОВ ==========
client_bot = Bot(token=CLIENT_BOT_TOKEN)
admin_bot = Bot(token=ADMIN_BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== КЛИЕНТСКАЯ ЧАСТЬ ==========
class OrderForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_model_name = State()
    waiting_for_quantity = State()

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🛒 Сделать заказ")],
        [KeyboardButton(text="📋 Мои заказы")]
    ],
    resize_keyboard=True
)

def payment_cash_keyboard(order_id, total_price):
    buttons = [
        [InlineKeyboardButton(text="✅ Подтверждаю оплату наличными", callback_data=f"cash_confirm_{order_id}")],
        [InlineKeyboardButton(text="❌ Отмена заказа (нет денег)", callback_data=f"cash_cancel_{order_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

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

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("🖨️ Добро пожаловать в сервис 3D-печати!\n\nВыберите действие:", reply_markup=keyboard)

@dp.message(lambda message: message.text == "🛒 Сделать заказ")
async def make_order(message: types.Message, state: FSMContext):
    await state.set_state(OrderForm.waiting_for_name)
    await message.answer("Введите ваше Имя и Фамилию:")

@dp.message(StateFilter(OrderForm.waiting_for_name))
async def get_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(OrderForm.waiting_for_phone)
    await message.answer("Введите номер телефона (белорусский):\nПример: +375291234567")

@dp.message(StateFilter(OrderForm.waiting_for_phone))
async def get_phone(message: types.Message, state: FSMContext):
    phone = validate_belarus_phone(message.text)
    if not phone:
        await message.answer("❌ Неверный формат! Пример: +375291234567")
        return
    await state.update_data(phone=phone)
    await state.set_state(OrderForm.waiting_for_model_name)
    await message.answer("Введите название модели:")

@dp.message(StateFilter(OrderForm.waiting_for_model_name))
async def get_model(message: types.Message, state: FSMContext):
    await state.update_data(model_name=message.text)
    await state.set_state(OrderForm.waiting_for_quantity)
    await message.answer("Введите количество (цифрой):")

@dp.message(StateFilter(OrderForm.waiting_for_quantity))
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
            reply_markup=keyboard
        )
        
        new_count = get_new_orders_count()
        await admin_bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 НОВЫЙ ЗАКАЗ #{order_id}!\n\n"
            f"👤 Клиент: {data['name']}\n"
            f"📱 Телефон: {data['phone']}\n"
            f"📦 Модель: {data['model_name']}\n"
            f"🔢 Количество: {quantity} шт.\n\n"
            f"📊 Новых заказов в очереди: {new_count}\n\n"
            f"Команды для админа:\n"
            f"/price_{order_id} 500 - выставить цену\n"
            f"/status_{order_id} принят - изменить статус\n"
            f"/orders - все заказы"
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Ошибка! Введите число.")

@dp.message(lambda message: message.text == "📋 Мои заказы")
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
    
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith('cash_confirm_'))
async def cash_confirm(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    order = get_order(order_id)
    
    update_payment(order_id, order[8], "полная")
    update_order_status(order_id, "оплачено наличными")
    
    await callback.message.edit_text(
        f"✅ Заказ #{order_id}\n\n"
        f"Вы подтвердили оплату наличными.\n"
        f"Спасибо! Заказ передан в печать.\n\n"
        f"Сумма: {order[8]} руб."
    )
    
    await admin_bot.send_message(
        ADMIN_CHAT_ID,
        f"💰 ЗАКАЗ #{order_id} ОПЛАЧЕН НАЛИЧНЫМИ!\n\n"
        f"👤 {order[2]}\n📱 {order[4]}\nСумма: {order[8]} руб."
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith('cash_cancel_'))
async def cash_cancel(callback: types.CallbackQuery):
    order_id = int(callback.data.split("_")[2])
    update_order_status(order_id, "отказ")
    await callback.message.edit_text(f"❌ Заказ #{order_id} отменён.")
    await admin_bot.send_message(ADMIN_CHAT_ID, f"❌ Заказ #{order_id} отменён клиентом")
    await callback.answer()

# ========== АДМИНСКАЯ ЧАСТЬ ==========
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        await message.answer("❌ Доступ запрещён")
        return
    await message.answer("👋 Панель администратора\n\nДоступные команды:\n/orders - все заказы\n/new - новые заказы")

@dp.message(Command("orders"))
async def list_orders(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    orders = get_all_orders()
    if not orders:
        await message.answer("📭 Заказов нет")
        return
    text = "📋 ВСЕ ЗАКАЗЫ:\n\n"
    for o in orders[:20]:
        text += f"#{o[0]} | {o[2]} | {o[5]} | {o[9]} | {o[8]} руб.\n"
    await message.answer(text)

@dp.message(Command("new"))
async def new_orders(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    orders = get_all_orders(status="новый")
    if not orders:
        await message.answer("🟡 Новых заказов нет")
        return
    for o in orders:
        text = (
            f"🆕 ЗАКАЗ #{o[0]}\n\n"
            f"👤 {o[2]}\n📱 {o[4]}\n🆔 @{o[3]}\n"
            f"📦 {o[5]}\n🔢 {o[6]} шт.\n\n"
            f"👉 /price_{o[0]} 500 - выставить цену"
        )
        await message.answer(text)

@dp.message(Command("price"))
async def set_price(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r"/price_(\d+)\s+(\d+(?:\.\d+)?)", message.text)
    if not match:
        await message.answer("❌ Формат: /price_123 500\n(где 500 - цена за 1 модель)")
        return
    
    order_id = int(match.group(1))
    price = float(match.group(2))
    
    order = get_order(order_id)
    if order[9] != "новый":
        await message.answer(f"⚠️ Заказ #{order_id} уже не новый. Статус: {order[9]}")
        return
    
    set_order_price(order_id, price)
    update_order_status(order_id, "цена выставлена")
    order = get_order(order_id)
    total = order[8]
    
    await client_bot.send_message(
        order[1],
        f"💵 МЕНЕДЖЕР ВЫСТАВИЛ СТОИМОСТЬ\n\n"
        f"📦 Заказ #{order_id}\n"
        f"🔢 Модель: {order[5]}\n"
        f"💰 Количество: {order[6]} шт.\n"
        f"💵 Цена за шт.: {price} руб.\n"
        f"💲 ИТОГО: {total} руб.\n\n"
        f"Ожидайте подтверждения."
    )
    
    await message.answer(f"✅ Цена {price} руб./шт. для заказа #{order_id}\n💲 Итого: {total} руб.")

@dp.message(Command("status"))
async def change_status(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r"/status_(\d+)\s+(.+)", message.text)
    if not match:
        await message.answer("Формат: /status_123 подготовка модели")
        return
    
    order_id = int(match.group(1))
    new_status = match.group(2)
    
    update_order_status(order_id, new_status)
    order = get_order(order_id)
    
    await client_bot.send_message(
        order[1],
        f"🔄 СТАТУС ЗАКАЗА #{order_id}\n\n📍 {new_status}"
    )
    
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
    
    await message.answer(f"✅ Заказ #{order_id} → {new_status}")

# ========== ЗАПУСК ==========
async def main():
    init_db()
    print("🤖 Бот запущен на Render!")
    print(f"👤 Клиентский бот: {CLIENT_BOT_TOKEN[:10]}...")
    print("📋 Команды админа: /admin, /orders, /new, /price_123 500, /status_123 подготовка модели")
    
    # Запускаем polling
    await dp.start_polling(client_bot)

if __name__ == '__main__':
    asyncio.run(main())
