import asyncio
import sqlite3
import logging
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# Пытаемся импортировать библиотеки для Google Таблиц
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSHEETS_AVAILABLE = True
except ImportError:
    GSHEETS_AVAILABLE = False

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_ОТ_BOTFATHER")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))  # Замените на ваш Telegram ID (число)

# Настройки Google Таблиц (опционально)
CREDENTIALS_FILE = "credentials.json"  # Путь к JSON-ключу сервисного аккаунта
SPREADSHEET_NAME = "Записи клиентов"   # Точное название вашей Google Таблицы

# Список услуг
SERVICES = ["Консультация", "Стрижка / Укладка", "Маникюр", "Диагностика"]

# Доступные часы для записи на каждый день
TIME_SLOTS = ["10:00", "12:00", "14:00", "16:00", "18:00"]

# На сколько дней вперед показывать расписание
DAYS_AHEAD = 7
# =============================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Работа с базой данных SQLite ---
def init_db():
    conn = sqlite3.connect("bookings.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            service TEXT,
            booking_date TEXT,
            booking_time TEXT,
            phone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(booking_date, booking_time)
        )
    """)
    conn.commit()
    conn.close()

def get_booked_times(booking_date: str) -> list:
    """Возвращает список уже занятых часов на конкретную дату."""
    conn = sqlite3.connect("bookings.db")
    cursor = conn.cursor()
    cursor.execute("SELECT booking_time FROM appointments WHERE booking_date = ?", (booking_date,))
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

def save_booking_db(user_id: int, username: str, service: str, b_date: str, b_time: str, phone: str) -> bool:
    """Сохраняет запись. Возвращает False, если слот уже занят."""
    conn = sqlite3.connect("bookings.db")
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO appointments (user_id, username, service, booking_date, booking_time, phone)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, username, service, b_date, b_time, phone))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def get_user_bookings(user_id: int) -> list:
    """Возвращает активные записи пользователя."""
    conn = sqlite3.connect("bookings.db")
    cursor = conn.cursor()
    today_str = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("""
        SELECT id, service, booking_date, booking_time 
        FROM appointments 
        WHERE user_id = ? AND booking_date >= ?
        ORDER BY booking_date, booking_time
    """, (user_id, today_str))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_booking_db(booking_id: int, user_id: int = None) -> tuple:
    """Удаляет запись по ID и возвращает детали удаленной записи."""
    conn = sqlite3.connect("bookings.db")
    cursor = conn.cursor()
    if user_id:
        cursor.execute("SELECT service, booking_date, booking_time, username FROM appointments WHERE id = ? AND user_id = ?", (booking_id, user_id))
    else:
        cursor.execute("SELECT service, booking_date, booking_time, username FROM appointments WHERE id = ?", (booking_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("DELETE FROM appointments WHERE id = ?", (booking_id,))
        conn.commit()
    conn.close()
    return row

# --- Работа с Google Таблицами ---
def append_to_gsheet(username: str, service: str, b_date: str, b_time: str, phone: str):
    """Добавляет строку в Google Таблицу."""
    if not GSHEETS_AVAILABLE or not os.path.exists(CREDENTIALS_FILE):
        logging.info("Google Таблицы не настроены (нет файла credentials.json). Запись сохранена только в локальную БД.")
        return

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open(SPREADSHEET_NAME).sheet1
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([now_str, b_date, b_time, service, username, phone])
    except Exception as e:
        logging.error(f"Ошибка при записи в Google Таблицу: {e}")

# --- Машина состояний (FSM) ---
class BookingState(StatesGroup):
    choosing_service = State()
    choosing_date = State()
    choosing_time = State()
    entering_phone = State()

# --- Клавиатуры ---
def get_main_menu_kb():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📅 Записаться")
    builder.button(text="📋 Мои записи")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def get_services_kb():
    builder = InlineKeyboardBuilder()
    for service in SERVICES:
        builder.button(text=service, callback_data=f"srv:{service}")
    builder.adjust(1)
    return builder.as_markup()

def get_dates_kb():
    builder = InlineKeyboardBuilder()
    today = datetime.now()
    for i in range(DAYS_AHEAD):
        day = today + timedelta(days=i)
        date_str = day.strftime("%Y-%m-%d")
        display_str = day.strftime("%d.%m (%a)")
        
        booked = get_booked_times(date_str)
        if len(booked) < len(TIME_SLOTS):
            builder.button(text=display_str, callback_data=f"date:{date_str}")
            
    builder.button(text="⬅️ К выбору услуги", callback_data="back_to_services")
    builder.adjust(2)
    return builder.as_markup()

def get_times_kb(booking_date: str):
    builder = InlineKeyboardBuilder()
    booked_times = get_booked_times(booking_date)
    
    for slot in TIME_SLOTS:
        if slot not in booked_times:
            builder.button(text=f"🕒 {slot}", callback_data=f"time:{slot}")
            
    builder.button(text="⬅️ Назад к датам", callback_data="back_to_dates")
    builder.adjust(3)
    return builder.as_markup()

def get_phone_kb():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📱 Отправить мой номер", request_contact=True)
    builder.button(text="❌ Отмена")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)

# --- Обработчики команд и кнопок ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Добро пожаловать в бот онлайн-записи!

"
        "Выберите действие в меню ниже или приступите к выбору услуги:",
        reply_markup=get_main_menu_kb()
    )
    await message.answer("Выберите услугу:", reply_markup=get_services_kb())
    await state.set_state(BookingState.choosing_service)

@dp.message(F.text == "📅 Записаться")
async def start_booking_btn(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Выберите услугу для записи:", reply_markup=get_services_kb())
    await state.set_state(BookingState.choosing_service)

@dp.message(F.text == "❌ Отмена")
async def cancel_booking_btn(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Запись отменена.", reply_markup=get_main_menu_kb())

@dp.message(F.text == "📋 Мои записи")
@dp.message(Command("mybookings"))
async def show_my_bookings(message: types.Message):
    bookings = get_user_bookings(message.from_user.id)
    if not bookings:
        await message.answer("У вас пока нет активных записей.", reply_markup=get_main_menu_kb())
        return

    await message.answer("📋 **Ваши активные записи:**", parse_mode="Markdown")
    for b_id, service, b_date, b_time in bookings:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отменить запись", callback_data=f"cancel_book:{b_id}")
        await message.answer(
            f"📌 **Услуга:** {service}
📅 **Дата:** {b_date}
🕒 **Время:** {b_time}",
            reply_markup=kb.as_markup(),
            parse_mode="Markdown"
        )

@dp.callback_query(F.data.startswith("cancel_book:"))
async def cancel_user_booking(callback: types.CallbackQuery):
    booking_id = int(callback.data.split(":")[1])
    deleted = delete_booking_db(booking_id, callback.from_user.id)
    
    if deleted:
        service, b_date, b_time, username = deleted
        await callback.message.edit_text(
            f"❌ Запись на **{service}** ({b_date} в {b_time}) успешно отменена.",
            parse_mode="Markdown"
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ **Отмена записи клиентом!**

"
                f"👤 **Клиент:** {username}
"
                f"📌 **Услуга:** {service}
"
                f"📅 **Освободился слот:** {b_date} в **{b_time}**",
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Ошибка уведомления админа об отмене: {e}")
    else:
        await callback.answer("Запись уже была отменена или не найдена.", show_alert=True)

# --- Шаги FSM для записи ---

@dp.callback_query(F.data == "back_to_services")
async def back_to_services(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выберите услугу для записи:", reply_markup=get_services_kb())
    await state.set_state(BookingState.choosing_service)
    await callback.answer()

@dp.callback_query(BookingState.choosing_service, F.data.startswith("srv:"))
async def service_chosen(callback: types.CallbackQuery, state: FSMContext):
    service = callback.data.split(":", 1)[1]
    await state.update_data(service=service)
    
    await callback.message.edit_text(
        f"📌 Услуга: **{service}**
Выберите удобную дату:",
        reply_markup=get_dates_kb(),
        parse_mode="Markdown"
    )
    await state.set_state(BookingState.choosing_date)
    await callback.answer()

@dp.callback_query(BookingState.choosing_date, F.data.startswith("date:"))
async def date_chosen(callback: types.CallbackQuery, state: FSMContext):
    b_date = callback.data.split(":")[1]
    await state.update_data(booking_date=b_date)
    
    await callback.message.edit_text(
        f"📅 Дата: **{b_date}**
Выберите свободное время:",
        reply_markup=get_times_kb(b_date),
        parse_mode="Markdown"
    )
    await state.set_state(BookingState.choosing_time)
    await callback.answer()

@dp.callback_query(BookingState.choosing_time, F.data == "back_to_dates")
async def back_to_dates(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_text(
        f"📌 Услуга: **{data.get('service', '')}**
Выберите удобную дату:",
        reply_markup=get_dates_kb(),
        parse_mode="Markdown"
    )
    await state.set_state(BookingState.choosing_date)
    await callback.answer()

@dp.callback_query(BookingState.choosing_time, F.data.startswith("time:"))
async def time_chosen(callback: types.CallbackQuery, state: FSMContext):
    b_time = callback.data.split(":", 1)[1]
    await state.update_data(booking_time=b_time)
    
    await callback.message.delete()
    await callback.message.answer(
        f"🕒 Вы выбрали время: **{b_time}**.

"
        "Пожалуйста, поделитесь номером телефона (кнопкой ниже) или напишите его в чат:",
        reply_markup=get_phone_kb(),
        parse_mode="Markdown"
    )
    await state.set_state(BookingState.entering_phone)
    await callback.answer()

@dp.message(BookingState.entering_phone)
async def phone_entered(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    data = await state.get_data()
    
    service = data['service']
    b_date = data['booking_date']
    b_time = data['booking_time']
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name

    success = save_booking_db(message.from_user.id, username, service, b_date, b_time, phone)
    
    if not success:
        await message.answer(
            "⚠️ К сожалению, это время только что заняли! Пожалуйста, выберите другое время:",
            reply_markup=get_main_menu_kb()
        )
        await message.answer(
            f"📅 Дата: **{b_date}**
Свободные слоты:",
            reply_markup=get_times_kb(b_date),
            parse_mode="Markdown"
        )
        await state.set_state(BookingState.choosing_time)
        return

    # Выгружаем в Google Таблицу асинхронно
    await asyncio.to_thread(append_to_gsheet, username, service, b_date, b_time, phone)

    await message.answer(
        f"✅ **Запись подтверждена!**

"
        f"📌 **Услуга:** {service}
"
        f"📅 **Дата:** {b_date}
"
        f"🕒 **Время:** {b_time}
"
        f"📞 **Телефон:** {phone}

"
        f"Управлять своими записями можно через кнопку «📋 Мои записи».",
        reply_markup=get_main_menu_kb(),
        parse_mode="Markdown"
    )

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 **Новая запись!**

"
            f"👤 **Клиент:** {username} (ID: `{message.from_user.id}`)
"
            f"📌 **Услуга:** {service}
"
            f"📅 **Дата:** {b_date} в **{b_time}**
"
            f"📞 **Телефон:** `{phone}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка отправки админу: {e}")

    await state.clear()

# --- Панель администратора ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("У вас нет прав администратора.")
        return

    conn = sqlite3.connect("bookings.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, username, service, booking_date, booking_time, phone 
        FROM appointments 
        ORDER BY booking_date DESC, booking_time DESC 
        LIMIT 20
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await message.answer("📋 В базе пока нет записей.")
        return

    text = "📋 **Последние 20 записей в базе:**

"
    for r_id, user, srv, b_date, b_time, phone in rows:
        text += f"🔹 **#{r_id}** | {b_date} в **{b_time}**
📌 {srv} | 👤 {user} | 📞 `{phone}`

"

    await message.answer(text, parse_mode="Markdown")

# --- Запуск бота ---
async def main():
    init_db()
    logging.info("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
