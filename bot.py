import logging
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.markdown import hcode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --------------------------
# --- ⚙️ КОНФИГУРАЦИЯ БОТА (ВАШИ ДАННЫЕ) ---
# --------------------------

# 1. Ваш токен бота от @BotFather
BOT_TOKEN = "7613767726:AAFwZ5tA4B1tr14eCYunucJZzRJtl61YyBM"

# 2. ID чата, куда будут приходить обращения (Штаб). 
ADMIN_CHAT_ID_RAW = 3043189571 

# 3. Список ID админов, которые имеют право отвечать.
ADMIN_IDS = [
    812708612, # Ваш ID
    # СЮДА ДОБАВЬТЕ ID ВТОРОГО АДМИНА
]
DB_NAME = 'bot_db.sqlite'

# Конфигурация времени для кнопок мута/бана
MUTE_OPTIONS = {
    "1 час 🤫": 1,
    "3 часа 🔇": 3,
    "12 часов 😐": 12,
}
BAN_OPTIONS = {
    "1 день ❌": 24,
    "7 дней 🚫": 168,
    "Навсегда 💀": 99999,
}
PERMANENT_BAN_MARKER = '9999-12-31 23:59:59'
# --------------------------

# --- FSM (КОНЕЧНЫЕ АВТОМАТЫ СОСТОЯНИЙ) ---
class AdminState(StatesGroup):
    """Состояния для администратора в чате Штаба."""
    waiting_for_unblock_id = State()

# Инициализация
router = Router()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.include_router(router)
ADMIN_CHAT_ID = [int(f'-100{ADMIN_CHAT_ID_RAW}'), -ADMIN_CHAT_ID_RAW]

# --- 🪵 НАСТРОЙКА ЛОГИРОВАНИЯ ---
class AdminLogHandler(logging.Handler):
    """Отправляет логи (только критические ошибки и сообщение о старте) в чат администратора."""
    def __init__(self, bot: Bot, raw_chat_id: int):
        super().__init__()
        self.bot = bot
        self.chat_id = int(f'-100{raw_chat_id}')
        self.setLevel(logging.WARNING) 
        self.formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s', 
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.is_ready = False 

    def emit(self, record):
        if not self.is_ready: return
        
        log_entry = self.format(record)
        is_startup_log = 'Бот Связи запущен' in log_entry or 'Соединение с БД закрыто' in log_entry
        is_critical_log = record.levelno >= logging.CRITICAL or 'Unauthorized' in log_entry
        
        if not (is_startup_log or is_critical_log): return
             
        asyncio.run_coroutine_threadsafe(
            self.send_log(log_entry), asyncio.get_event_loop()
        )

    async def send_log(self, text):
        """Отправка лога в чат админа."""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=hcode(f"🚨 ЛОГ {text}"),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            # Логируем ошибку отправки в консоль, если чат недоступен
            logging.error(f"Не удалось отправить лог в чат админа.")

admin_log_handler = AdminLogHandler(bot, ADMIN_CHAT_ID_RAW)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger().addHandler(admin_log_handler)


# Словари и DB
message_map = {}
db_conn: aiosqlite.Connection = None 

# -------------------------------------------------------------
# --- 💾 ФУНКЦИИ БАЗЫ ДАННЫХ (БЛОКИРОВКА) ---
# -------------------------------------------------------------

async def init_db():
    global db_conn
    db_conn = await aiosqlite.connect(DB_NAME)
    await db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY,
            block_until TEXT,
            reason TEXT
        );
        """
    )
    await db_conn.commit()
    logging.info("База данных блокировок инициализирована.")

async def is_user_blocked(user_id: int):
    now = datetime.now()
    async with db_conn.execute("SELECT block_until, reason FROM blocked_users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        if not row: 
            return False, None, None
        
        block_until_str, reason = row
        
        if block_until_str == PERMANENT_BAN_MARKER:
             return True, "Навсегда", reason

        block_until = datetime.strptime(block_until_str, '%Y-%m-%d %H:%M:%S')

        if block_until > now: 
            return True, block_until.strftime('%d.%m.%Y %H:%M'), reason
        
        # Удаляем просроченную блокировку
        await db_conn.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
        await db_conn.commit()
        return False, None, None

async def block_user(user_id: int, duration_hours: int, reason: str):
    if duration_hours == 99999:
        block_until_str = PERMANENT_BAN_MARKER
        block_until_display = "Навсегда"
    else:
        block_until = datetime.now() + timedelta(hours=duration_hours)
        block_until_str = block_until.strftime('%Y-%m-%d %H:%M:%S')
        block_until_display = block_until.strftime('%d.%m.%Y %H:%M')
    
    await db_conn.execute(
        "REPLACE INTO blocked_users (user_id, block_until, reason) VALUES (?, ?, ?)",
        (user_id, block_until_str, reason)
    )
    await db_conn.commit()
    return block_until_display

async def unblock_user(user_id: int):
    # Получаем причину до удаления для логирования
    async with db_conn.execute("SELECT reason FROM blocked_users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        reason = row[0] if row else "N/A"

    await db_conn.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
    
    async with db_conn.execute("SELECT changes() FROM blocked_users") as cursor:
         rows_changed = (await cursor.fetchone())[0]

    await db_conn.commit()
    return rows_changed > 0, reason

# -------------------------------------------------------------
# --- ⌨️ ИНЛАЙН-КЛАВИАТУРА ---
# -------------------------------------------------------------

def get_admin_actions_keyboard(user_id: int) -> InlineKeyboardBuilder:
    """Генерирует основную инлайн-клавиатуру для управления пользователем."""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="🔇 МУТ", callback_data=f"select_mute:{user_id}")
    builder.button(text="⛔️ БАН", callback_data=f"select_ban:{user_id}")
    builder.button(text="🔓 Размут", callback_data=f"unblock:{user_id}")
    
    builder.button(text="💬 Ответить", callback_data=f"reply_info:{user_id}")
    builder.button(text="📋 Инфо/История", callback_data=f"user_detailed_info:{user_id}")
    
    builder.button(text="➡️ Переслать", callback_data=f"forward_to_admin:{user_id}") 
    builder.button(text="🔍 Поиск ID", callback_data=f"search_id:{user_id}")
    
    builder.adjust(3, 2, 2)
    return builder

def get_time_selection_keyboard(user_id: int, options: dict, prefix: str) -> InlineKeyboardBuilder:
    """Генерирует клавиатуру выбора времени для мута/бана."""
    builder = InlineKeyboardBuilder()
    for text, duration in options.items():
        builder.button(text=text, callback_data=f"{prefix}:{user_id}:{duration}")
    
    builder.button(text="⬅️ Назад", callback_data=f"back_to_main:{user_id}")
    
    builder.adjust(3, repeat=True)
    return builder

# -------------------------------------------------------------
# --- 👑 АДМИН-ЛОГИКА В ШТАБЕ (ЧЕРЕЗ FSM и РОУТЕР) ---
# -------------------------------------------------------------

# --- 1. КОМАНДА /START В ШТАБЕ (Кнопка разблокировки) ---
@router.message(F.text == "/start", F.chat.id.in_(ADMIN_CHAT_ID))
async def command_start_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return 
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🔓 Снять блокировку по ID", callback_data="manual_unblock_start")
    
    await message.answer(
        "👋 **Добро пожаловать в Штаб поддержки!**\n\n"
        "Выберите действие ниже, чтобы разблокировать пользователя, "
        "или ответьте на сообщение клиента, чтобы анонимно отправить ответ.",
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.MARKDOWN
    )

# --- 2. НАЧАЛО ПРОЦЕССА РАЗБЛОКИРОВКИ ПО ID ---
@router.callback_query(F.data == "manual_unblock_start")
async def start_manual_unblock(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("🚫 Нет прав.", show_alert=True)
    
    await state.set_state(AdminState.waiting_for_unblock_id)
    await callback.message.answer(
        "🔓 **Режим разблокировки по ID**\n"
        "Пожалуйста, введите **числовой ID** пользователя, которого нужно разблокировать.\n\n"
        "Напишите `/cancel`, чтобы отменить.",
        # Убираем клавиатуру (если была)
        reply_markup=types.ReplyKeyboardRemove()
    )
    await callback.answer()

# --- 3. ОБРАБОТКА ВВОДА /cancel ---
@router.message(AdminState.waiting_for_unblock_id, F.text.lower() == '/cancel')
async def cancel_unblock(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Отменено. Вы вернулись в обычный режим.", reply_markup=types.ReplyKeyboardRemove())

# --- 4. ОБРАБОТКА ВВОДА ID ДЛЯ РАЗБЛОКИРОВКИ ---
@router.message(AdminState.waiting_for_unblock_id)
async def process_manual_unblock_id(message: types.Message, state: FSMContext):
    await state.clear() 

    try:
        user_id = int(message.text.strip())
    except ValueError:
        return await message.answer("❌ Ошибка! ID должен быть числом. Попробуйте еще раз или напишите `/cancel`.", reply_markup=types.ReplyKeyboardRemove())

    is_unblocked, reason = await unblock_user(user_id)

    if is_unblocked:
        await message.answer(f"✅ Пользователь {hcode(user_id)} **разблокирован** (ранее был заблокирован по причине: *{reason}*).", parse_mode=ParseMode.HTML, reply_markup=types.ReplyKeyboardRemove())
        try:
            await bot.send_message(chat_id=user_id, text="🥳 **Разблокировка!**\n\nТеперь вы снова можете писать администрации.")
        except Exception:
            logging.warning(f"Не удалось уведомить пользователя {user_id} о разблокировке.")
    else:
        await message.answer(f"⚠️ Пользователь {hcode(user_id)} не найден в списке блокировок.", parse_mode=ParseMode.HTML, reply_markup=types.ReplyKeyboardRemove())


# --- 5. ОБРАБОТЧИК АНОНИМНЫХ ОТВЕТОВ ---
@router.message(
    F.chat.id.in_(ADMIN_CHAT_ID), 
    F.reply_to_message, 
    lambda m: m.reply_to_message.message_id in message_map
)
async def handle_admin_reply(message: types.Message):
    """Центральный обработчик анонимного ответа администратора."""
    if message.from_user.id not in ADMIN_IDS: return
    # Отправляем ответ пользователю
    await reply_to_user(message)


# --- 6. КОМАНДЫ BLOCK/UNBLOCK (Для прямого ввода) ---

@router.message(F.chat.id.in_(ADMIN_CHAT_ID), F.text.startswith('/block'))
async def command_block_user(message: types.Message):
    """Логика команды блокировки через /block [ID] [часы] [Причина]."""
    try:
        parts = message.text.split(maxsplit=3)
        if len(parts) < 3:
            return await message.reply("⛔️ **Ошибка!** Использование: `/block [ID] [часы] [Причина]`", parse_mode=ParseMode.MARKDOWN)

        user_id = int(parts[1])
        duration = int(parts[2])
        reason = parts[3] if len(parts) > 3 else "Не указана (через команду)"

        block_until_str = await block_user(user_id, duration, reason)
        
        await message.reply(f"✅ Пользователь {hcode(user_id)} **заблокирован** до {block_until_str}.\nПричина: {reason}", parse_mode=ParseMode.HTML)
        
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🛑 **Уведомление о блокировке!**\n\nВаша возможность писать администрации временно ограничена.\n"
                     f"**Причина:** {reason}\n**До:** {block_until_str}",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            logging.warning(f"Не удалось уведомить пользователя {user_id} о блокировке.")

    except ValueError:
        await message.reply("⛔️ **Ошибка!** Проверьте формат ID (число) и часов (число).", parse_mode=ParseMode.MARKDOWN)

@router.message(F.chat.id.in_(ADMIN_CHAT_ID), F.text.startswith('/unblock'))
async def command_unblock_user(message: types.Message):
    """Логика команды разблокировки через /unblock [ID]."""
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            return await message.reply("⛔️ **Ошибка!** Использование: `/unblock [ID]`", parse_mode=ParseMode.MARKDOWN)

        user_id = int(parts[1])
        is_unblocked, reason = await unblock_user(user_id)
        
        if is_unblocked:
            await message.reply(f"✅ Пользователь {hcode(user_id)} **разблокирован** (Причина блокировки: *{reason}*).", parse_mode=ParseMode.HTML)
            try:
                await bot.send_message(chat_id=user_id, text="🥳 **Разблокировка!**\n\nТеперь вы снова можете писать администрации.")
            except:
                pass
        else:
            await message.reply(f"⚠️ Пользователь {hcode(user_id)} не найден в списке блокировок.", parse_mode=ParseMode.HTML)
            
    except ValueError:
        await message.reply("⛔️ **Ошибка!** Проверьте формат ID (число).", parse_mode=ParseMode.MARKDOWN)

# --- 7. ПУСТЫЕ СООБЩЕНИЯ АДМИНОВ (Игнорируем) ---
@router.message(F.chat.id.in_(ADMIN_CHAT_ID))
async def ignore_other_admin_messages(message: types.Message):
    """Игнорирует все остальные сообщения в чате Штаба, которые не являются командами, ответами или сообщениями в состоянии FSM."""
    if message.from_user.id not in ADMIN_IDS: return
    pass 

# -------------------------------------------------------------
# --- ФУНКЦИИ И CALLBACKS ВНЕ ШТАБА ---
# -------------------------------------------------------------

# --- ФУНКЦИЯ АНОНИМНОГО ОТВЕТА ---
async def reply_to_user(message: types.Message):
    """Позволяет админу ответить пользователю анонимно."""
    if not message.text:
          return await message.reply("☝️ Для ответа пользователю нужно отправить ТЕКСТОВОЕ сообщение.")

    original_message_id = message.reply_to_message.message_id
    user_id_to_reply = message_map.get(original_message_id)

    if user_id_to_reply:
        try:
            await bot.send_message(
                chat_id=user_id_to_reply,
                text=f"**📢 Ответ Администрации:**\n\n{message.text}",
                parse_mode=ParseMode.MARKDOWN
            )
            await message.reply(f"✅ Анонимный ответ отправлен пользователю {hcode(user_id_to_reply)}.", parse_mode=ParseMode.HTML)
            
        except Exception as e:
            logging.error(f"Не удалось отправить ответ пользователю {user_id_to_reply}. Причина: {e}")
            await message.reply("❌ Ошибка отправки! Пользователь, возможно, заблокировал бота.")
    else:
        logging.warning(f"Не найден user_id для сообщения-ответа ID: {original_message_id}")
        await message.reply("⚠️ Ошибка: не удалось найти получателя. Связь с этим сообщением потеряна.")

# --- ГЛАВНАЯ ФУНКЦИЯ ПЕРЕСЫЛКИ (КЛИЕНТ -> АДМИНУ) ---
@router.message(F.chat.id.not_in(ADMIN_CHAT_ID))
async def forward_to_admin(message: types.Message):
    """Принимает сообщение от клиента, проверяет блокировку и пересылает админам с кнопками."""
    user = message.from_user
    
    if user.id in ADMIN_IDS: 
        return await message.answer("👋 Привет, Администратор! Ваши сообщения не пересылаются в Штаб.")
    
    is_blocked, unblock_time, reason = await is_user_blocked(user.id)
    if is_blocked:
        # Уведомление заблокированного пользователя с причиной
        return await message.answer(
            f"🛑 **Внимание!** Ваша возможность писать администрации временно ограничена.\n"
            f"Вы сможете снова писать после **{unblock_time}**.\n**Причина:** {reason}",
            parse_mode=ParseMode.MARKDOWN
        )
        
    user_info = (
        f"**🚨 НОВОЕ ОБРАЩЕНИЕ!**\n"
        f"**🆔 ID:** `{user.id}`\n"
        f"**👤 Ник:** @{user.username or 'Нет'}\n"
        f"**🧑‍💻 Имя:** {user.full_name}\n"
        f"**🌎 Язык:** {user.language_code or 'Не указан'}\n"
        f"**✅ Premium:** {'Да' if user.is_premium else 'Нет'}\n"
        f"----------------------------------------\n"
    )
    
    chat_ids_to_try = [int(f'-100{ADMIN_CHAT_ID_RAW}'), -ADMIN_CHAT_ID_RAW]

    last_error = None
    sent_successfully = False
    
    keyboard = get_admin_actions_keyboard(user.id)
    
    for chat_id_to_try in chat_ids_to_try:
        try:
            admin_message_text = await bot.send_message(
                chat_id=chat_id_to_try,
                text=user_info + (message.text or "*--- Сообщение без текста (только медиа) ---*"),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard.as_markup()
            )
            
            if message.content_type != types.ContentType.TEXT:
                await message.copy_to(
                    chat_id=chat_id_to_try,
                    caption=message.caption or "",
                    reply_to_message_id=admin_message_text.message_id
                )

            message_map[admin_message_text.message_id] = user.id
            sent_successfully = True
            break
            
        except Exception as e:
            last_error = e

    if sent_successfully:
        await message.answer("✅ Ваше сообщение успешно доставлено. Мы свяжемся с вами в ответном сообщении.")
    else:
        logging.error(f"Не удалось отправить сообщение в чат {ADMIN_CHAT_ID_RAW} (обе попытки). Последняя ошибка: {last_error}")
        await message.answer("❌ Извините, произошла техническая ошибка. Администратор не получил ваше сообщение.")

# --- CALLBACKS (ОБРАБОТКА НАЖАТИЯ КНОПОК) ---

@router.callback_query(lambda c: c.data.startswith('search_id:'))
async def process_search_id_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("🚫 Нет прав.", show_alert=True)
    _, user_id_str = callback.data.split(':')
    user_id = int(user_id_str)
    search_url = f"https://www.google.com/search?q=telegram+id+{user_id}"
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Искать в Google", url=search_url)
    await callback.message.answer(
        f"**🔍 Поиск информации по ID:**\n\nID: `{user_id}`",
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.MARKDOWN
    )
    await callback.answer(f"ID: {user_id} — готов для поиска.", show_alert=False)

@router.callback_query(lambda c: c.data.startswith('user_detailed_info:'))
async def process_detailed_info_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("🚫 Нет прав.", show_alert=True)
    _, user_id_str = callback.data.split(':')
    user_id = int(user_id_str)
    
    is_blocked, unblock_time, reason = await is_user_blocked(user_id)
    
    block_status = "✅ Чист"
    if is_blocked:
        block_status = f"🚫 **ЗАБЛОКИРОВАН** до {unblock_time}.\n Причина: {reason}"
    
    info_text = (
        f"**📋 ДЕТАЛЬНАЯ ИНФОРМАЦИЯ**\n"
        f"**🆔 ID:** `{user_id}`\n"
        f"----------------------------------\n"
        f"**Статус блокировки:** {block_status}\n"
        f"**История:** (Требуется отдельная база данных)\n"
    )
    
    await callback.answer(f"Загрузка информации о {user_id}...", show_alert=False)
    await callback.message.answer(info_text, parse_mode=ParseMode.MARKDOWN)

@router.callback_query(lambda c: c.data.startswith('forward_to_admin:'))
async def process_forward_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("🚫 Нет прав.", show_alert=True)
    await callback.answer("⚠️ Эта функция требует дополнительной настройки (FSM) для указания получателя.", show_alert=True)


@router.callback_query(lambda c: c.data.startswith('select_mute:'))
async def select_mute_time(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("🚫 Нет прав.", show_alert=True)
    user_id = int(callback.data.split(':')[1])
    keyboard = get_time_selection_keyboard(user_id, MUTE_OPTIONS, "mute")
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())
    await callback.answer("Выберите время мута.")

@router.callback_query(lambda c: c.data.startswith('select_ban:'))
async def select_ban_time(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("🚫 Нет прав.", show_alert=True)
    user_id = int(callback.data.split(':')[1])
    keyboard = get_time_selection_keyboard(user_id, BAN_OPTIONS, "ban")
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())
    await callback.answer("Выберите время бана.")

@router.callback_query(lambda c: c.data.startswith('back_to_main:'))
async def back_to_main_keyboard(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("🚫 Нет прав.", show_alert=True)
    user_id = int(callback.data.split(':')[1])
    keyboard = get_admin_actions_keyboard(user_id)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())
    await callback.answer("Вернулись к основным действиям.")

@router.callback_query(lambda c: c.data.startswith(('mute:', 'ban:')))
async def process_block_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("🚫 Нет прав.", show_alert=True)
    
    prefix, user_id_str, duration_str = callback.data.split(':')
    user_id = int(user_id_str)
    duration = int(duration_str)
    
    action_type = "Мут" if prefix == "mute" else "Бан"
    reason = f"Админ {callback.from_user.full_name} применил {action_type} через кнопки."
    
    block_until_display = await block_user(user_id, duration, reason)
    
    await callback.answer(f"✅ Пользователь заблокирован ({action_type}) до {block_until_display}", show_alert=True)
    
    keyboard = get_admin_actions_keyboard(user_id)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())
    
    await bot.send_message(
        chat_id=callback.message.chat.id,
        text=f"✅ Администратор {callback.from_user.full_name} применил **{action_type}** для {hcode(user_id)} на **{block_until_display}**.",
        parse_mode=ParseMode.HTML
    )
    try:
          await bot.send_message(
              chat_id=user_id,
              text=f"🛑 **Уведомление о блокировке!**\n\nВаша возможность писать администрации временно ограничена.\n"
                   f"**Тип:** {action_type}\n**Причина:** Нарушение правил.\n**До:** {block_until_display}",
              parse_mode=ParseMode.MARKDOWN
          )
    except:
        pass

@router.callback_query(lambda c: c.data.startswith('unblock:'))
async def process_unblock_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("🚫 Нет прав.", show_alert=True)

    _, user_id_str = callback.data.split(':')
    user_id = int(user_id_str)
    
    is_unblocked, reason = await unblock_user(user_id)
    
    if is_unblocked: 
        await callback.answer(f"🥳 Пользователь {user_id} разблокирован.", show_alert=True)
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text=f"✅ Администратор {callback.from_user.full_name} **разблокировал** {hcode(user_id)} (Причина: *{reason}*).",
            parse_mode=ParseMode.HTML
        )
        try:
            await bot.send_message(chat_id=user_id, text="🥳 **Разблокировка!**\n\nТеперь вы снова можете писать администрации.")
        except:
            pass
    else:
        await callback.answer(f"⚠️ Пользователь {user_id} не был найден в списке блокировок.", show_alert=True)
    
    keyboard = get_admin_actions_keyboard(user_id)
    await callback.message.edit_reply_markup(reply_markup=keyboard.as_markup())

@router.callback_query(lambda c: c.data.startswith('reply_info:'))
async def process_reply_info_callback(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return await callback.answer("🚫 Нет прав.", show_alert=True)
    await callback.answer("Нажмите 'Ответить' (Reply) на заголовок сообщения для анонимного ответа.", show_alert=True)


# -------------------------------------------------------------
# --- ЗАПУСК БОТА ---
# -------------------------------------------------------------
async def main() -> None:
    # Разрешаем отправку логов ДО попытки запуска!
    admin_log_handler.is_ready = True 
    
    try:
        await init_db() 
        logging.info("🚀 Бот Связи запущен и готов к работе!")
        await dp.start_polling(bot)
    except Exception as e:
        # Логирование критической ошибки при сбое
        logging.critical(f"🛑 Критическая ошибка при запуске: {e}")
    finally:
        if db_conn:
            await db_conn.close() 
            logging.info("🛑 Соединение с БД закрыто.")

if __name__ == "__main__":
    asyncio.run(main())