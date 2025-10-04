import logging
import sys
import os
import calendar
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    filters, ConversationHandler, CallbackQueryHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

# Для работы с PostgreSQL
import psycopg2
from psycopg2.extras import RealDictCursor

# Загружаем переменные окружения из .env
from dotenv import load_dotenv
load_dotenv()

# Глобальная переменная для доступа к application из планировщика
app = None

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Состояния
CHOOSING, TYPING_REPLY, SELECTING_START_DATE, SELECTING_END_DATE, SELECTING_CLEAR_DATE = range(5)

# Предустановленные статусы
PRESET_STATUSES = ["✅ На работе", "🏠 Дома", "🌴 В отпуске", "🤒 Болею", "✈️ В командировке"]

# ========== КАЛЕНДАРЬ ==========
def create_calendar(year=None, month=None):
    now = datetime.now()
    if year is None: year = now.year
    if month is None: month = now.month

    prev_month = (month - 1) if month > 1 else 12
    prev_year = year - 1 if month == 1 else year
    next_month = (month + 1) if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    keyboard = [
        [InlineKeyboardButton(f"{month}/{year}", callback_data="ignore")]
    ]
    keyboard.append([
        InlineKeyboardButton("Пн", callback_data="ignore"),
        InlineKeyboardButton("Вт", callback_data="ignore"),
        InlineKeyboardButton("Ср", callback_data="ignore"),
        InlineKeyboardButton("Чт", callback_data="ignore"),
        InlineKeyboardButton("Пт", callback_data="ignore"),
        InlineKeyboardButton("Сб", callback_data="ignore"),
        InlineKeyboardButton("Вс", callback_data="ignore")
    ])

    first_weekday = datetime(year, month, 1).weekday()
    days_in_month = (datetime(year, month % 12 + 1, 1) - timedelta(days=1)).day if month < 12 else 31

    week = []
    for _ in range(first_weekday):
        week.append(InlineKeyboardButton(" ", callback_data="ignore"))
    for day in range(1, days_in_month + 1):
        week.append(InlineKeyboardButton(str(day), callback_data=f"cal:{year}-{month:02d}-{day:02d}"))
        if len(week) == 7:
            keyboard.append(week)
            week = []
    while len(week) < 7:
        week.append(InlineKeyboardButton(" ", callback_data="ignore"))
    if week:
        keyboard.append(week)

    keyboard.append([
        InlineKeyboardButton("◀️", callback_data=f"prev:{prev_year}-{prev_month:02d}"),
        InlineKeyboardButton("Сегодня", callback_data=f"today"),
        InlineKeyboardButton("▶️", callback_data=f"next:{next_year}-{next_month:02d}")
    ])

    return InlineKeyboardMarkup(keyboard)

# ========== РАБОТА С БД ==========
def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS")
    )

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            chat_id BIGINT,
            is_active BOOLEAN DEFAULT TRUE
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS statuses (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            chat_id BIGINT,
            status_text TEXT NOT NULL,
            date DATE NOT NULL
        )
    ''')
    cur.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_date ON statuses (user_id, date)
    ''')
    conn.commit()
    cur.close()
    conn.close()

def add_user(user_id, username, chat_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO users (user_id, username, chat_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO NOTHING
    ''', (user_id, username, chat_id))
    conn.commit()
    cur.close()
    conn.close()

def get_active_users(chat_id):
    """Возвращает активных пользователей для чата."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('SELECT user_id, username FROM users WHERE chat_id = %s AND is_active = TRUE', (chat_id,))
    result = cur.fetchall()
    cur.close()
    conn.close()
    return [(row['user_id'], row['username']) for row in result]

def save_status_for_date(user_id, chat_id, status_text, target_date):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO statuses (user_id, chat_id, status_text, date)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, date)
        DO UPDATE SET status_text = EXCLUDED.status_text, chat_id = EXCLUDED.chat_id
    ''', (user_id, chat_id, status_text, target_date))
    conn.commit()
    cur.close()
    conn.close()

def save_status_range(user_id, chat_id, status_text, start_date, end_date):
    current = start_date
    while current <= end_date:
        save_status_for_date(user_id, chat_id, status_text, current)
        current += timedelta(days=1)

def delete_user_status_today(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        DELETE FROM statuses
        WHERE user_id = %s AND date = CURRENT_DATE
    ''', (user_id,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return deleted > 0

def delete_user_status_by_date(user_id, target_date):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        DELETE FROM statuses
        WHERE user_id = %s AND date = %s
    ''', (user_id, target_date))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return deleted > 0

def delete_all_user_statuses(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''
        DELETE FROM statuses
        WHERE user_id = %s
    ''', (user_id,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return deleted

def get_statuses_last_week():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('''
        SELECT u.username, s.status_text, s.date
        FROM statuses s
        JOIN users u ON s.user_id = u.user_id
        WHERE s.date >= CURRENT_DATE - INTERVAL '7 days'
        ORDER BY s.date DESC, u.username
    ''')
    result = cur.fetchall()
    cur.close()
    conn.close()
    return [(row['username'], row['status_text'], row['date']) for row in result]

# ========== ЕЖЕДНЕВНЫЙ ОПРОС ==========
async def daily_poll_job():
    """Отправляет ежедневный опрос всем активным пользователям (кроме выходных и если статус уже установлен)."""
    global app
    if app is None:
        logger.error("Application not initialized!")
        return

    try:
        today = date.today()
        # Проверяем, что сегодня не выходной (0=Пн, 6=Вс)
        if today.weekday() >= 5:  # 5=Сб, 6=Вс
            logger.info("Сегодня выходной — опрос не отправляется")
            return

        conn = get_db_connection()
        cur = conn.cursor()
        
        # Получаем всех активных пользователей
        cur.execute('SELECT user_id, chat_id FROM users WHERE is_active = TRUE')
        users = cur.fetchall()
        cur.close()
        conn.close()

        for user_id, chat_id in users:
            try:
                # Проверяем, есть ли уже статус на сегодня
                conn_check = get_db_connection()
                cur_check = conn_check.cursor()
                cur_check.execute('''
                    SELECT 1 FROM statuses 
                    WHERE user_id = %s AND date = %s
                ''', (user_id, today))
                status_exists = cur_check.fetchone() is not None
                cur_check.close()
                conn_check.close()

                if status_exists:
                    logger.info(f"Статус пользователя {user_id} на {today} уже установлен — опрос не отправляется")
                    continue

                # Отправляем опрос
                keyboard = [[status] for status in PRESET_STATUSES] + [["✏️ Написать свой"]]
                reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=False, resize_keyboard=True)
                await app.bot.send_message(
                    chat_id=user_id,
                    text="📆 Как твой статус сегодня?",
                    reply_markup=reply_markup
                )
                logger.info(f"Отправлен опрос пользователю {user_id}")
                
            except Exception as e:
                logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")
                
    except Exception as e:
        logger.error(f"Ошибка в daily_poll_job: {e}")

# ========== ОБРАБОТЧИКИ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    add_user(user.id, user.username or user.first_name, chat_id)

    keyboard = [
        ["/start", "/setstatus"],
        ["/calendar", "/status"],
        ["/clearstatus", "/clearbydate"],
        ["/clearall"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n"
        "🔹 /setstatus — статус на сегодня\n"
        "🔹 /calendar — статус на период\n"
        "🔹 /status — статусы команды за неделю\n"
        "🔹 /clearstatus — удалить статус на сегодня\n"
        "🔹 /clearbydate — удалить статус на дату (через календарь)\n"
        "🔹 /clearall — удалить все статусы",
        reply_markup=reply_markup
    )

async def show_status_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    statuses = get_statuses_last_week()
    if not statuses:
        await update.message.reply_text("Нет статусов за последние 7 дней.")
    else:
        msg = "📅 Статусы за последние 7 дней:\n\n"
        current_date = None
        for username, status, date_val in statuses:
            if current_date != date_val:
                current_date = date_val
                msg += f"\n🗓️ {current_date}:\n"
            msg += f"  👤 {username}: {status}\n"
        await update.message.reply_text(msg)

async def clear_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if delete_user_status_today(user_id):
        await update.message.reply_text("🗑️ Ваш статус на сегодня удалён.")
    else:
        await update.message.reply_text("ℹ️ У вас нет статуса на сегодня.")

# НОВАЯ ФУНКЦИЯ: запуск календаря для удаления
async def clear_by_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = "clear"  # Устанавливаем режим удаления
    await update.message.reply_text("Выбери дату для удаления статуса:", reply_markup=create_calendar())
    return SELECTING_CLEAR_DATE

async def clear_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    deleted_count = delete_all_user_statuses(user_id)
    if deleted_count > 0:
        await update.message.reply_text(f"🗑️ Все ваши статусы удалены ({deleted_count} записей).")
    else:
        await update.message.reply_text("ℹ️ У вас нет сохранённых статусов.")

async def set_status_manually(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[status] for status in PRESET_STATUSES] + [["✏️ Написать свой"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Выбери статус на сегодня:", reply_markup=reply_markup)
    return CHOOSING

async def status_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "✏️ Написать свой":
        await update.message.reply_text("Напиши свой статус:", reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True))
        return TYPING_REPLY
    if text in PRESET_STATUSES:
        save_status_for_date(update.effective_user.id, update.effective_chat.id, text, date.today())
        await update.message.reply_text("✅ Статус на сегодня обновлён!")
        return ConversationHandler.END
    await update.message.reply_text("Пожалуйста, выбери статус из кнопок.")
    return CHOOSING

async def custom_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "Отмена":
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END
    save_status_for_date(update.effective_user.id, update.effective_chat.id, update.message.text, date.today())
    await update.message.reply_text("✅ Статус на сегодня обновлён!")
    return ConversationHandler.END

# ========== КАЛЕНДАРЬ ==========
async def calendar_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Выбери дату начала периода:", reply_markup=create_calendar())
    return SELECTING_START_DATE

async def calendar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "ignore":
        return
    if data == "today":
        today = date.today()
        await query.edit_message_reply_markup(reply_markup=create_calendar(today.year, today.month))
        return
    if data.startswith("prev:") or data.startswith("next:"):
        _, ym = data.split(":")
        year, month = map(int, ym.split("-"))
        await query.edit_message_reply_markup(reply_markup=create_calendar(year, month))
        return
    if data.startswith("cal:"):
        _, date_str = data.split(":", 1)
        selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        
        # Режим удаления статуса
        if context.user_data.get("mode") == "clear":
            user_id = query.from_user.id
            if delete_user_status_by_date(user_id, selected_date):
                await query.edit_message_text(f"🗑️ Ваш статус на {selected_date} удалён.")
            else:
                await query.edit_message_text(f"ℹ️ У вас нет статуса на {selected_date}.")
            context.user_data.clear()
            return ConversationHandler.END
        
        # Режим установки периода (старый код)
        if context.user_data.get("start_date") is None:
            context.user_data["start_date"] = selected_date
            await query.edit_message_text(f"Начало: {selected_date}\nТеперь выбери дату окончания:", reply_markup=create_calendar(selected_date.year, selected_date.month))
            return SELECTING_END_DATE
        else:
            start_date = context.user_data["start_date"]
            end_date = selected_date
            if end_date < start_date:
                await query.edit_message_text("❌ Дата окончания не может быть раньше начала.\nВыбери дату окончания снова:", reply_markup=create_calendar(start_date.year, start_date.month))
                return SELECTING_END_DATE
            context.user_data["end_date"] = end_date
            keyboard = [[status] for status in PRESET_STATUSES] + [["✏️ Написать свой"]]
            reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
            await query.message.reply_text(
                f"Установить статус с {start_date} по {end_date}?\nВыбери статус:",
                reply_markup=reply_markup
            )
            return CHOOSING

async def status_for_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "✏️ Написать свой":
        await update.message.reply_text("Напиши свой статус:", reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True))
        return TYPING_REPLY
    start_date = context.user_data["start_date"]
    end_date = context.user_data["end_date"]
    save_status_range(update.effective_user.id, update.effective_chat.id, text, start_date, end_date)
    await update.message.reply_text(f"✅ Статус обновлён с {start_date} по {end_date}!")
    context.user_data.clear()
    return ConversationHandler.END

async def custom_status_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "Отмена":
        await update.message.reply_text("Отменено.")
        context.user_data.clear()
        return ConversationHandler.END
    start_date = context.user_data["start_date"]
    end_date = context.user_data["end_date"]
    save_status_range(update.effective_user.id, update.effective_chat.id, update.message.text, start_date, end_date)
    await update.message.reply_text(f"✅ Статус обновлён с {start_date} по {end_date}!")
    context.user_data.clear()
    return ConversationHandler.END

# ========== НОВАЯ ФУНКЦИЯ: обработка ответов на опрос ==========
async def handle_poll_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ответ на утренний опрос и другие текстовые сообщения."""
    text = update.message.text
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Если пользователь пишет "Отмена" при вводе кастомного статуса
    if text == "Отмена" and context.user_data.get("awaiting_custom_status"):
        await update.message.reply_text("Отменено.")
        context.user_data.pop("awaiting_custom_status", None)
        return
    
    # Если ожидаем кастомный статус
    if context.user_data.get("awaiting_custom_status"):
        save_status_for_date(user_id, chat_id, text, date.today())
        await update.message.reply_text("✅ Статус на сегодня сохранён!")
        context.user_data.pop("awaiting_custom_status", None)
        return
    
    # Обработка стандартных статусов
    if text in PRESET_STATUSES:
        save_status_for_date(user_id, chat_id, text, date.today())
        await update.message.reply_text("✅ Статус на сегодня сохранён!")
        return
    
    # Обработка кнопки "Написать свой"
    if text == "✏️ Написать свой":
        await update.message.reply_text("Напиши свой статус:", reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True))
        context.user_data["awaiting_custom_status"] = True
        return
    
    # Если ничего не подошло — показываем меню
    keyboard = [
        ["/start", "/setstatus"],
        ["/calendar", "/status"],
        ["/clearstatus", "/clearbydate"],
        ["/clearall"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text(
        "Выбери команду или статус:",
        reply_markup=reply_markup
    )

# ========== ЗАПУСК ==========
async def post_init(application: Application) -> None:
    global app
    app = application
    scheduler = AsyncIOScheduler(timezone=pytz.timezone('Europe/Moscow'))
    scheduler.add_job(daily_poll_job, 'cron', hour=9, minute=0)
    scheduler.start()
    logger.info("Планировщик запущен: ежедневный опрос в 9:00 по Москве")

def main():
    init_db()
    TOKEN = os.getenv("TELEGRAM_TOKEN")

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Обработчик для ручной установки статуса (/setstatus)
    manual_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("setstatus", set_status_manually)],
        states={
            CHOOSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, status_chosen)],
            TYPING_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_status)],
        },
        fallbacks=[],
        per_user=True
    )

    # Обработчики периодов и удаления (без изменений)
    period_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("calendar", calendar_start)],
        states={
            SELECTING_START_DATE: [CallbackQueryHandler(calendar_handler)],
            SELECTING_END_DATE: [CallbackQueryHandler(calendar_handler)],
            CHOOSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, status_for_period)],
            TYPING_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_status_period)],
        },
        fallbacks=[],
        per_user=True
    )

    clear_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("clearbydate", clear_by_date_start)],
        states={
            SELECTING_CLEAR_DATE: [CallbackQueryHandler(calendar_handler)],
        },
        fallbacks=[],
        per_user=True
    )

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", show_status_all))
    application.add_handler(CommandHandler("clearstatus", clear_status))
    application.add_handler(CommandHandler("clearall", clear_all))
    
    # ВАЖНО: сначала специфичные обработчики, потом общий
    application.add_handler(manual_conv_handler)
    application.add_handler(period_conv_handler)
    application.add_handler(clear_conv_handler)
    
    # Общий обработчик для всех текстовых сообщений (включая ответы на опрос)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_poll_response))

    application.run_polling()

if __name__ == '__main__':
    main()
