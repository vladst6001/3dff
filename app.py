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
    """Возвращает текущее время в Минске (UTC+3) — ФИКСИРОВАННОЕ, не меняется."""
    return datetime.now(MINSK_TZ).strftime('%Y-%m-%d %H:%M:%S')


def create_order(client_id, client_name, client_username, phone, model_name, quantity, image_url=None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now = get_current_time()   # ← сохраняется один раз при создании
    print(f"📝 Создание заказа: клиент={client_id}, время={now}")
    cursor.execute('''
        INSERT INTO orders
            (client_id, client_name, client_username, phone, model_name, quantity, image_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (client_id, client_name, client_username, phone, model_name, quantity, image_url, now, now))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    print(f"✅ Заказ {order_id} создан, дата={now}")
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
    print(f"✅ Заказ {order_id} → статус '{status}'")


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
    cursor.execute('''
        INSERT OR REPLACE INTO user_profiles (user_id, name, phone, avatar_url, updated_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, name, phone, avatar_url, now))
    conn.commit()
    conn.close()
    print(f"✅ Профиль пользователя {user_id} сохранён")


def get_user_profile(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT name, phone, avatar_url FROM user_profiles WHERE user_id=?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'name': row[0], 'phone': row[1], 'avatar_url': row[2]}
    return None


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


client_keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
client_keyboard.add(KeyboardButton("🛒 Сделать заказ"))
client_keyboard.add(KeyboardButton("📋 Мои заказы"))


@client_dp.message_handler(commands=['start'])
async def client_start(message: types.Message):
    await message.answer(
        "🖨️ *3Dprinti*\n\n"
        "Сервис 3D\\-печати на заказ\\.\n"
        "Нажмите «🛒 Сделать заказ», чтобы оформить заказ\\.",
        reply_markup=client_keyboard,
        parse_mode="MarkdownV2"
    )


@client_dp.message_handler(lambda m: m.text == "🛒 Сделать заказ")
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
    async with state.proxy() as data:
        data['phone'] = message.text.strip()
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
            f"✅ *Заказ №{order_id} создан\\!*\n\n"
            f"📦 Модель: {data['model']}\n"
            f"🔢 Количество: {quantity} шт\\.\n\n"
            "Статус можно отслеживать в «📋 Мои заказы»\\.",
            parse_mode="MarkdownV2"
        )
        await client_bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 *НОВЫЙ ЗАКАЗ \\#{order_id}*\n\n"
            f"👤 {data['name']}\n"
            f"📱 {data['phone']}\n"
            f"📦 {data['model']}\n"
            f"🔢 {quantity} шт\\.",
            parse_mode="MarkdownV2"
        )
        await state.finish()
    except ValueError:
        await message.answer("❌ Ошибка! Введите число.")


@client_dp.message_handler(lambda m: m.text == "📋 Мои заказы")
async def my_orders(message: types.Message):
    orders = get_orders_by_client(message.from_user.id)
    if not orders:
        await message.answer("📭 У вас пока нет заказов.")
        return
    status_emoji = {
        'новый': '🟡', 'принят': '🟢', 'отказ': '🔴',
        'подготовка модели': '🟠', 'цена выставлена': '💰',
        'оплачено наличными': '✅', 'печать': '🖨️', 'готова': '🎉',
        'завершён': '🏁'
    }
    text = "📋 *МОИ ЗАКАЗЫ*\n\n"
    for order in orders:
        emoji = status_emoji.get(order[11], '⚪')
        total = order[9] if order[9] else 0
        text += f"{emoji} *Заказ №{order[0]}*\n"
        text += f" 📦 {order[5]}\n"
        text += f" 🔢 {order[6]} шт\\.\n"
        if total > 0:
            text += f" 💰 {total} руб\\.\n"
        text += f" 📍 {order[11]}\n\n"
    await message.answer(text, parse_mode="MarkdownV2")


# ========== АДМИНСКИЕ КОМАНДЫ (через клиентский бот) ==========
@client_dp.message_handler(commands=['admin_orders'])
async def admin_all_orders(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    orders = get_all_orders()
    if not orders:
        await message.answer("📭 Заказов нет")
        return
    text = "📋 *ВСЕ ЗАКАЗЫ*\n\n"
    for order in orders[:20]:
        text += f"\\#{order[0]} \\| {order[2]} \\| {order[5]} \\| {order[11]} \\| {order[9]} руб\\.\n"
    await message.answer(text, parse_mode="MarkdownV2")


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
            f"🆕 *ЗАКАЗ \\#{order[0]}*\n\n"
            f"👤 {order[2]}\n📱 {order[4]}\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт\\.\n\n"
            f"➡️ /accept\\_{order[0]} — принять\n"
            f"➡️ /reject\\_{order[0]} — отказать"
        )
        await message.answer(text, parse_mode="MarkdownV2")


@client_dp.message_handler(lambda m: re.match(r'/accept_\d+', m.text or ''))
async def admin_accept_order(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/accept_(\d+)', message.text)
    order_id = int(match.group(1))
    update_order_status(order_id, "принят")
    order = get_order(order_id)
    if order:
        await client_bot.send_message(order[1], f"🟢 *Заказ №{order_id} принят в работу\\!*", parse_mode="MarkdownV2")
    await message.answer(f"✅ Заказ \\#{order_id} принят", parse_mode="MarkdownV2")


@client_dp.message_handler(lambda m: re.match(r'/reject_\d+', m.text or ''))
async def admin_reject_order(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/reject_(\d+)', message.text)
    order_id = int(match.group(1))
    update_order_status(order_id, "отказ")
    order = get_order(order_id)
    if order:
        await client_bot.send_message(order[1], f"🔴 *Заказ №{order_id} отклонён*", parse_mode="MarkdownV2")
    await message.answer(f"❌ Заказ \\#{order_id} отклонён", parse_mode="MarkdownV2")


@client_dp.message_handler(lambda m: re.match(r'/price_\d+\s+\d+', m.text or ''))
async def admin_price_order(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/price_(\d+)\s+(\d+(?:\.\d+)?)', message.text)
    if not match:
        await message.answer("Формат: /price\\_123 500", parse_mode="MarkdownV2")
        return
    order_id = int(match.group(1))
    price = float(match.group(2))
    set_order_price(order_id, price)
    order = get_order(order_id)
    if order:
        total = price * order[6]
        await client_bot.send_message(
            order[1],
            f"💰 *Стоимость заказа №{order_id}*\n\n"
            f"📦 {order[5]}\n🔢 {order[6]} шт\\.\n"
            f"💵 Цена за шт\\.: {price} руб\\.\n"
            f"💲 *Итого: {total} руб\\.*",
            parse_mode="MarkdownV2"
        )
    await message.answer(f"✅ Цена {price} руб\\. для заказа \\#{order_id}", parse_mode="MarkdownV2")


@client_dp.message_handler(lambda m: re.match(r'/status_\d+\s+.+', m.text or ''))
async def admin_status_order(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    match = re.match(r'/status_(\d+)\s+(.+)', message.text)
    if not match:
        await message.answer("Формат: /status\\_123 подготовка модели", parse_mode="MarkdownV2")
        return
    order_id  = int(match.group(1))
    new_status = match.group(2).strip()
    update_order_status(order_id, new_status)
    order = get_order(order_id)
    if order:
        await client_bot.send_message(
            order[1],
            f"🔄 *Статус заказа №{order_id}:* {new_status}",
            parse_mode="MarkdownV2"
        )
    await message.answer(f"✅ Заказ \\#{order_id} → {new_status}", parse_mode="MarkdownV2")


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
    filename = f"avatar_{user_id}_{uuid.uuid4().hex[:8]}.jpg"
    filepath = os.path.join('avatars', filename)
    with open(filepath, 'wb') as f:
        f.write(base64.b64decode(img_data))
    avatar_url = f"/avatars/{filename}"
    # обновляем только avatar_url, сохраняем name/phone
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
    save_user_profile(user_id, name, phone)
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

    # ── Сохраняем файл если есть ──
    file_url = None
    uploaded = request.files.get('file')
    if uploaded and uploaded.filename:
        os.makedirs('order_files', exist_ok=True)
        ext      = os.path.splitext(uploaded.filename)[1].lower() or '.bin'
        filename = f"order_{user_id}_{uuid.uuid4().hex[:8]}{ext}"
        filepath = os.path.join('order_files', filename)
        uploaded.save(filepath)
        file_url = f"/order_files/{filename}"
        print(f"📎 Файл сохранён: {filepath}")

    order_id = create_order(
        client_id=user_id,
        client_name=name,
        client_username=username,
        phone=phone,
        model_name=model,
        quantity=quantity,
        image_url=file_url
    )
    # created_at берётся из БД — фиксированное время создания
    order = get_order(order_id)
    created_at = order[12] if order else get_current_time()

    def notify_sync():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        async def _send():
            file_note = f"\n📎 Файл: да" if file_url else ""
            await client_bot.send_message(
                ADMIN_CHAT_ID,
                f"🆕 *НОВЫЙ ЗАКАЗ \\#{order_id} из Mini App\\!*\n\n"
                f"👤 {name}\n📱 {phone}\n📦 {model}\n🔢 {quantity} шт\\.{file_note}",
                parse_mode="MarkdownV2"
            )
        loop.run_until_complete(_send())
        loop.close()

    threading.Thread(target=notify_sync, daemon=True).start()
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
            'created_at':  created_at   # ← всегда фиксированная дата
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


@flask_app.route('/admin_accept', methods=['POST'])
def admin_accept():
    data     = request.json
    order_id = data.get('order_id')
    update_order_status(order_id, "принят")
    order = get_order(order_id)
    if order:
        def _notify():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def _s():
                await client_bot.send_message(order[1], f"🟢 *Заказ №{order_id} принят в работу\\!*", parse_mode="MarkdownV2")
            loop.run_until_complete(_s())
            loop.close()
        threading.Thread(target=_notify, daemon=True).start()
    return jsonify({'ok': True})


@flask_app.route('/admin_reject', methods=['POST'])
def admin_reject():
    data     = request.json
    order_id = data.get('order_id')
    update_order_status(order_id, "отказ")
    order = get_order(order_id)
    if order:
        def _notify():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def _s():
                await client_bot.send_message(order[1], f"🔴 *Заказ №{order_id} отклонён*", parse_mode="MarkdownV2")
            loop.run_until_complete(_s())
            loop.close()
        threading.Thread(target=_notify, daemon=True).start()
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
        def _notify():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def _s():
                await client_bot.send_message(
                    order[1],
                    f"💰 *Стоимость заказа №{order_id}*\n\n"
                    f"📦 {order[5]}\n🔢 {order[6]} шт\\.\n"
                    f"💵 Цена за шт\\.: {price} руб\\.\n"
                    f"💲 *Итого: {total} руб\\.*",
                    parse_mode="MarkdownV2"
                )
            loop.run_until_complete(_s())
            loop.close()
        threading.Thread(target=_notify, daemon=True).start()
    return jsonify({'ok': True})


@flask_app.route('/admin_status', methods=['POST'])
def admin_status():
    data       = request.json
    order_id   = data.get('order_id')
    new_status = data.get('status')
    update_order_status(order_id, new_status)
    order = get_order(order_id)
    if order:
        def _notify():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def _s():
                await client_bot.send_message(
                    order[1],
                    f"🔄 *Статус заказа №{order_id}:* {new_status}",
                    parse_mode="MarkdownV2"
                )
            loop.run_until_complete(_s())
            loop.close()
        threading.Thread(target=_notify, daemon=True).start()
    return jsonify({'ok': True})


@flask_app.route('/')
def health_check():
    return "3Dprinti бот работает! ✅"


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
    print("🤖 Запуск бота...")
    run_bot(client_dp)
