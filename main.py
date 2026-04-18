import asyncio
import random
import os
import string
import json
import psutil
import shutil
import tempfile
import base64
import re
import threading
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, \
    CallbackQuery, BotCommand, BotCommandScopeChat, FSInputFile, ChatJoinRequest, ChatMember
from aiogram.filters import Command, StateFilter
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from cachetools import TTLCache
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession
import aiohttp
from functools import wraps

# ========== ЗАЩИТА ОТ СПАМА (ГЛОБАЛЬНАЯ) ==========
user_last_action = {}
callback_last_action = {}

class AntiSpamMiddleware:
    def __init__(self, limit_seconds: float = 0.5):
        self.limit_seconds = limit_seconds
    async def __call__(self, handler, event: Message, data: dict):
        if isinstance(event, Message):
            user_id = str(event.from_user.id)
            now = time.time()
            last_time = user_last_action.get(user_id, 0)
            if now - last_time < self.limit_seconds:
                return
            user_last_action[user_id] = now
        return await handler(event, data)

class CallbackAntiSpamMiddleware:
    def __init__(self, limit_seconds: float = 0.5):
        self.limit_seconds = limit_seconds
    async def __call__(self, handler, event: CallbackQuery, data: dict):
        if isinstance(event, CallbackQuery):
            user_id = str(event.from_user.id)
            now = time.time()
            last_time = callback_last_action.get(user_id, 0)
            if now - last_time < self.limit_seconds:
                await event.answer("⏳ Не так быстро!", show_alert=False)
                return
            callback_last_action[user_id] = now
        return await handler(event, data)

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = "8410870219:AAFT55BS8hGfpV2ZcEc7sixXx7QBOkvUpyM"
ADMIN_IDS = [7636031451, 5366500428, 7892214606]

# ========== БОТ (БЕЗ ПРОКСИ, ЧТОБЫ НЕ БЫЛО ПЕРЕБОЕВ) ==========
# Если нужен прокси - раскомментируй строки ниже и закомментируй bot = Bot(token=TOKEN)
# PROXY_URL = "socks5://127.0.0.1:10801"
# session = AiohttpSession(proxy=PROXY_URL)
# bot = Bot(token=TOKEN, session=session)

# БЕЗ ПРОКСИ (СТАБИЛЬНЕЕ):
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== ПОДКЛЮЧАЕМ ЗАЩИТУ ОТ СПАМА ==========
dp.message.middleware(AntiSpamMiddleware(limit_seconds=0.5))
dp.callback_query.middleware(CallbackAntiSpamMiddleware(limit_seconds=0.5))

# ========== КАНАЛЫ ==========
CHANNELS = [
    {"id": "-1003876875157", "url": "https://t.me/+6I0icR_TrYQ4ZTkx"},
]

# Файлы для сохранения данных
DATA_FILE = "bot_data.json"
BACKUP_FILE = "bot_data_backup.json"
TASKS_FILE = "tasks_data.json"

# ========== ХРАНИЛИЩА ==========
captcha_passed = {}
captcha_data = {}
referral_codes = {}
pending_referrals = {}
users_db = {}
user_bonus = {}
withdraw_requests = {}
request_counter = 0
request_sent = {}
user_tasks_completed = {}
tasks_list = {}
task_requests = {}
active_task = {}
skipped_tasks = {}

# Защита от спама (дополнительная)
spam_cache = TTLCache(maxsize=10000, ttl=0.5)

# Список эмодзи животных
ANIMAL_EMOJIS = ["🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼", "🐨", "🐸",
                 "🐒", "🐔", "🐧", "🐦", "🐤", "🐴", "🐝", "🐛", "🐌", "🐞",
                 "🐟", "🐠", "🐡", "🐙", "🦋", "🐳", "🐬", "🦄", "🐪", "🐘"]

# ========== АВТОСОХРАНЕНИЕ ==========
cache_dirty = False
cache_lock = threading.Lock()
last_save_time = time.time()

def force_save_data():
    global last_save_time, cache_dirty
    with cache_lock:
        try:
            data = {
                "captcha_passed": captcha_passed,
                "captcha_data": captcha_data,
                "referral_codes": referral_codes,
                "pending_referrals": pending_referrals,
                "users_db": users_db,
                "user_bonus": user_bonus,
                "withdraw_requests": withdraw_requests,
                "request_counter": request_counter,
                "request_sent": request_sent,
                "user_tasks_completed": user_tasks_completed,
                "task_requests": task_requests,
                "active_task": active_task,
                "skipped_tasks": skipped_tasks
            }
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            last_save_time = time.time()
            cache_dirty = False
        except Exception as e:
            print(f"Ошибка при сохранении: {e}")

def mark_data_dirty():
    global cache_dirty
    cache_dirty = True

def save_data():
    mark_data_dirty()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
class AdminStates(StatesGroup):
    waiting_for_user_id_reset = State()
    waiting_for_user_id_balance = State()
    waiting_for_clear_confirmation = State()
    waiting_for_request_id = State()
    waiting_for_task_name = State()
    waiting_for_task_url = State()
    waiting_for_task_reward = State()
    waiting_for_task_delete = State()

def extract_channel_id_from_url(url: str) -> str:
    match = re.search(r't\.me/([^/?]+)', url)
    if match:
        username = match.group(1)
        return f"@{username}"
    if '/+' in url:
        return None
    return None

def is_private_channel(url: str) -> bool:
    return '/+' in url or 'joinchat' in url

async def check_subscription(user_id: int, channel_url: str) -> bool:
    user_id_str = str(user_id)
    if is_private_channel(channel_url):
        return user_id_str in task_requests
    channel_id = extract_channel_id_from_url(channel_url)
    if not channel_id:
        channel_id = channel_url
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print(f"Ошибка проверки подписки: {e}")
        return user_id_str in task_requests

def generate_animal_captcha():
    correct_animal = random.choice(ANIMAL_EMOJIS)
    other_animals = random.sample([a for a in ANIMAL_EMOJIS if a != correct_animal], 3)
    options = other_animals + [correct_animal]
    random.shuffle(options)
    question = f"Найди {correct_animal} среди животных"
    return correct_animal, options, question

def encode_user_id(user_id: str) -> str:
    encoded_bytes = base64.urlsafe_b64encode(user_id.encode('utf-8'))
    encoded = encoded_bytes.decode('utf-8')
    return encoded.rstrip('=')

def decode_user_id(encoded: str) -> str:
    try:
        padding = 4 - (len(encoded) % 4)
        if padding != 4:
            encoded += '=' * padding
        decoded_bytes = base64.urlsafe_b64decode(encoded.encode('utf-8'))
        return decoded_bytes.decode('utf-8')
    except Exception as e:
        print(f"Ошибка декодирования: {e}")
        return None

# ========== ЗАГРУЗКА ДАННЫХ ==========
def load_data():
    global captcha_passed, captcha_data, referral_codes, pending_referrals, users_db, user_bonus, withdraw_requests, request_counter, request_sent, user_tasks_completed, task_requests, active_task, skipped_tasks
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                captcha_passed = data.get("captcha_passed", {})
                captcha_data = data.get("captcha_data", {})
                referral_codes = data.get("referral_codes", {})
                pending_referrals = data.get("pending_referrals", {})
                users_db = data.get("users_db", {})
                user_bonus = data.get("user_bonus", {})
                withdraw_requests = data.get("withdraw_requests", {})
                request_counter = data.get("request_counter", 0)
                request_sent = data.get("request_sent", {})
                user_tasks_completed = data.get("user_tasks_completed", {})
                task_requests = data.get("task_requests", {})
                active_task = data.get("active_task", {})
                skipped_tasks = data.get("skipped_tasks", {})
    except Exception as e:
        print(f"Ошибка при загрузке данных: {e}")

def save_tasks():
    try:
        data = {
            "tasks_list": tasks_list,
            "user_tasks_completed": user_tasks_completed
        }
        with open(TASKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка при сохранении заданий: {e}")

def load_tasks():
    global tasks_list, user_tasks_completed
    try:
        if os.path.exists(TASKS_FILE):
            with open(TASKS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                tasks_list = data.get("tasks_list", {})
                user_tasks_completed = data.get("user_tasks_completed", {})
    except Exception as e:
        print(f"Ошибка при загрузке заданий: {e}")

def save_user(user_id, username, full_name):
    if user_id not in users_db:
        users_db[user_id] = {
            "username": username,
            "full_name": full_name,
            "joined_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_active": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        save_data()
    else:
        users_db[user_id]["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        users_db[user_id]["username"] = username
        users_db[user_id]["full_name"] = full_name
        save_data()

def has_sent_request(user_id: str) -> bool:
    return user_id in request_sent and request_sent[user_id].get("sent", False)

def has_active_withdraw(user_id: str) -> tuple:
    for req_id, req_data in withdraw_requests.items():
        if req_data["user_id"] == user_id and req_data["status"] == "pending":
            return True, req_id
    return False, None

def has_completed_task(user_id: str, task_id: str) -> bool:
    return user_id in user_tasks_completed and task_id in user_tasks_completed[user_id]

def has_skipped_task(user_id: str, task_id: str) -> bool:
    if user_id not in skipped_tasks or task_id not in skipped_tasks[user_id]:
        return False
    skip_time = skipped_tasks[user_id][task_id]
    if datetime.now().timestamp() - skip_time > 24 * 3600:
        del skipped_tasks[user_id][task_id]
        save_data()
        return False
    return True

def mark_task_completed(user_id: str, task_id: str):
    if user_id not in user_tasks_completed:
        user_tasks_completed[user_id] = {}
    user_tasks_completed[user_id][task_id] = True
    save_tasks()

def mark_task_skipped(user_id: str, task_id: str):
    if user_id not in skipped_tasks:
        skipped_tasks[user_id] = {}
    skipped_tasks[user_id][task_id] = datetime.now().timestamp()
    save_data()

def get_first_available_task(user_id: str):
    available_tasks = []
    skipped_available_tasks = []
    for task_id, task in tasks_list.items():
        if not has_completed_task(user_id, task_id):
            if not has_skipped_task(user_id, task_id):
                try:
                    num_id = int(task_id)
                except:
                    num_id = float('inf')
                available_tasks.append((num_id, task_id, task))
            else:
                try:
                    num_id = int(task_id)
                except:
                    num_id = float('inf')
                skipped_available_tasks.append((num_id, task_id, task))
    available_tasks.sort(key=lambda x: x[0])
    skipped_available_tasks.sort(key=lambda x: x[0])
    if available_tasks:
        return available_tasks[0][1], available_tasks[0][2]
    if skipped_available_tasks:
        task_id = skipped_available_tasks[0][1]
        if user_id in skipped_tasks and task_id in skipped_tasks[user_id]:
            del skipped_tasks[user_id][task_id]
            save_data()
        return skipped_available_tasks[0][1], skipped_available_tasks[0][2]
    return None, None

def subscribe_keyboard():
    markup = InlineKeyboardMarkup(inline_keyboard=[])
    for i, ch in enumerate(CHANNELS, start=1):
        markup.inline_keyboard.append([
            InlineKeyboardButton(text=f"✨ {i}) Подписаться", url=ch["url"])
        ])
    markup.inline_keyboard.append([
        InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="check_subs")
    ])
    return markup

def kill_other_bot_processes():
    current_pid = os.getpid()
    current_script = os.path.basename(__file__)
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                cmdline = proc.info['cmdline']
                if cmdline and len(cmdline) > 1:
                    if current_script in ' '.join(cmdline):
                        if proc.info['pid'] != current_pid:
                            proc.terminate()
                            proc.wait(timeout=3)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass

def get_user_mention(user):
    if user.username:
        return f'<a href="https://t.me/{user.username}">Пользователь</a>'
    else:
        return "Пользователь"

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ========== КЛАВИАТУРЫ ==========
tasks_admin_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕ Добавить задание", callback_data="task_add")],
    [InlineKeyboardButton(text="🗑 Удалить задание", callback_data="task_delete")],
    [InlineKeyboardButton(text="📋 Список заданий", callback_data="task_list")],
    [InlineKeyboardButton(text="🔙 Назад", callback_data="task_back")]
])

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⭐ Заработать звёзды")],
        [KeyboardButton(text="🎁 Вывести звёзды")],
        [KeyboardButton(text="🎯 Задания"), KeyboardButton(text="💎 Бонус")]
    ],
    resize_keyboard=True
)

admin_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👥 Просмотр пользователей")],
        [KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="📨 Рассылка")],
        [KeyboardButton(text="🔄 Сброс рефералов")],
        [KeyboardButton(text="💰 Редактировать баланс")],
        [KeyboardButton(text="✅ Обработать заявку")],
        [KeyboardButton(text="📝 Управление заданиями")],
        [KeyboardButton(text="🧹 Очистить всех")],
        [KeyboardButton(text="📥 Выгрузить пользователей TXT")],
        [KeyboardButton(text="📋 Заявки на вывод")],
        [KeyboardButton(text="🔙 Выйти из админ-панели")]
    ],
    resize_keyboard=True
)

stars_inline_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="15⭐", callback_data="stars_15"),
         InlineKeyboardButton(text="25⭐", callback_data="stars_25")],
        [InlineKeyboardButton(text="50⭐", callback_data="stars_50"),
         InlineKeyboardButton(text="100⭐", callback_data="stars_100")],
        [InlineKeyboardButton(text="150⭐", callback_data="stars_150"),
         InlineKeyboardButton(text="350⭐", callback_data="stars_350")],
        [InlineKeyboardButton(text="500⭐", callback_data="stars_500")]
    ]
)

# ========== ДАЛЬШЕ ВСЕ ХЕНДЛЕРЫ (start, admin, и т.д.) ==========
# ... (весь остальной твой код с хендлерами)


def kill_other_bot_processes():
    current_pid = os.getpid()
    current_script = os.path.basename(__file__)
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                cmdline = proc.info['cmdline']
                if cmdline and len(cmdline) > 1:
                    if current_script in ' '.join(cmdline):
                        if proc.info['pid'] != current_pid:
                            proc.terminate()
                            proc.wait(timeout=3)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            pass


def get_user_mention(user):
    if user.username:
        return f'<a href="https://t.me/{user.username}">Пользователь</a>'
    else:
        return "Пользователь"


def is_admin(user_id):
    return user_id in ADMIN_IDS


@dp.chat_join_request()
async def handle_join_request(update: ChatJoinRequest):
    user_id = str(update.from_user.id)
    request_sent[user_id] = {
        "sent": True,
        "time": datetime.now().timestamp(),
        "chat_id": update.chat.id
    }
    if user_id in active_task:
        task_id = active_task[user_id]
        task_requests[user_id] = {
            "task_id": task_id,
            "time": datetime.now().timestamp()
        }
    save_data()


@dp.message(Command("start"))
async def start(message: Message):
    user_id = str(message.from_user.id)
    user_id_int = int(message.from_user.id)
    save_user(user_id, message.from_user.username, message.from_user.full_name)
    args = message.text.split()
    referral_param = args[1] if len(args) > 1 else None
    referral_id = None
    if referral_param:
        referral_id = decode_user_id(referral_param)

    # ОБРАБОТКА РЕФЕРАЛКИ ТОЛЬКО 1 РАЗ
    if user_id not in captcha_passed and referral_id and referral_id.isdigit() and referral_id != user_id:
        referrer_id = referral_id

        # Проверяем, не начисляли ли уже бонус от этого пользователя
        already_rewarded = False
        for ref_id, pending_list in pending_referrals.items():
            for pending in pending_list:
                if pending.get("user_id") == user_id:
                    already_rewarded = True
                    break
            if already_rewarded:
                break

        # Если ещё не начисляли - начисляем 0.25
        if not already_rewarded and referrer_id in referral_codes:
            referral_codes[referrer_id]["earned"] += 0.35
            save_data()

            # Добавляем в pending_referrals
            if referrer_id not in pending_referrals:
                pending_referrals[referrer_id] = []
            pending_referrals[referrer_id].append({
                "user_id": user_id,
                "username": message.from_user.username,
                "full_name": message.from_user.full_name
            })
            save_data()

            # Отправляем уведомление рефереру
            user_mention = get_user_mention(message.from_user)
            try:
                await bot.send_message(
                    int(referrer_id),
                    f"🤖 {user_mention} перешел по вашей реферальной ссылке, начислили на ваш баланс в боте +0.35⭐\n\n"
                    f"Как только он пройдет капчу бота, начислим еще +2.65⭐",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
            except:
                pass

    # ДАЛЬШЕ КАПЧА...
    if user_id not in captcha_passed or not captcha_passed[user_id].get("passed", False):
        correct_animal, options, question = generate_animal_captcha()
        captcha_data[user_id] = {
            "correct": correct_animal,
            "created_at": asyncio.get_event_loop().time(),
            "attempts": 0
        }
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=emoji, callback_data=f"captcha_{emoji}") for emoji in options[i:i + 2]]
            for i in range(0, len(options), 2)
        ])
        await message.answer(
            f"🤖 <b>Добро пожаловать! Подтвердите, что вы человек</b>\n\n"
            f"{question}\n\n"
            f"⏳ У вас есть 60 секунд",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
        return

    if user_id in captcha_passed and captcha_passed[user_id].get("passed", False):
        if not has_sent_request(user_id):
            await message.answer(
                "🔥 Добро пожаловать!\n\n"
                "Чтобы получить доступ, подпишитесь на спонсоров ниже 👇",
                reply_markup=subscribe_keyboard()
            )
            return
        else:
            text = (
                "👥 Приглашать пользователей — самый простой способ получения звёзд\n\n"
                "⭐ Нажми «Заработать звёзды»\n\n"
                "Выведено уже более 100.000 звёзд"
            )
            await message.answer(text, reply_markup=main_kb)


@dp.callback_query(F.data.startswith("captcha_"))
async def handle_captcha(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    selected = callback.data.replace("captcha_", "")
    if user_id not in captcha_data:
        await callback.answer("⏰ Время вышло! Напишите /start заново", show_alert=True)
        return
    elapsed = asyncio.get_event_loop().time() - captcha_data[user_id]["created_at"]
    if elapsed > 60:
        del captcha_data[user_id]
        await callback.answer("⏰ Время вышло! Напишите /start заново", show_alert=True)
        return
    correct = captcha_data[user_id]["correct"]
    if selected == correct:
        del captcha_data[user_id]
        captcha_passed[user_id] = {"passed": True, "answer": None}
        save_data()
        await callback.message.delete()
        await callback.message.answer(
            "✅ <b>Капча пройдена!</b>\n\n"
            "🔥 Добро пожаловать!\n\n"
            "⚡️ Чтобы получить доступ, подпишитесь на все каналы ниже 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=subscribe_keyboard()
        )
        await callback.answer("✅ Капча пройдена!", show_alert=True)
    else:
        captcha_data[user_id]["attempts"] += 1
        attempts = captcha_data[user_id]["attempts"]
        if attempts >= 3:
            del captcha_data[user_id]
            await callback.message.delete()
            await callback.message.answer(
                "❌ <b>Вы использовали все попытки!</b>\n\n"
                "Напишите /start чтобы попробовать снова",
                parse_mode=ParseMode.HTML
            )
            await callback.answer("❌ Попытки закончились!", show_alert=True)
        else:
            correct_animal, options, question = generate_animal_captcha()
            captcha_data[user_id]["correct"] = correct_animal
            captcha_data[user_id]["created_at"] = asyncio.get_event_loop().time()
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=emoji, callback_data=f"captcha_{emoji}") for emoji in options[i:i + 2]]
                for i in range(0, len(options), 2)
            ])
            await callback.message.edit_text(
                f"❌ <b>Неправильно! Осталось попыток: {3 - attempts}</b>\n\n"
                f"{question}\n\n"
                f"⏳ У вас есть 60 секунд",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )
            await callback.answer("❌ Неправильно! Попробуйте ещё раз", show_alert=True)


@dp.callback_query(F.data == "check_subs")
async def check_subscriptions(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    if has_sent_request(user_id):
        for referrer_id, pending_list in list(pending_referrals.items()):
            for i, pending in enumerate(pending_list):
                if pending["user_id"] == user_id:
                    if referrer_id in referral_codes:
                        referral_codes[referrer_id]["earned"] += 2.65
                        referral_codes[referrer_id]["referrals"] = referral_codes[referrer_id].get("referrals", 0) + 1
                        save_data()
                        user_mention = get_user_mention(callback.from_user)
                        try:
                            await bot.send_message(
                                int(referrer_id),
                                f"🤖 {user_mention} прошел подписку на каналы, начислено +1.75⭐",
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                        except:
                            pass
                    pending_list.pop(i)
                    save_data()
                    break
        await callback.message.delete()
        text = (
            "👥 Приглашать пользователей — самый простой способ получения звёзд\n\n"
            "⭐ Нажми «Заработать звёзды»\n\n"
            "Выведено уже более 100.000 звёзд"
        )
        await callback.message.answer(text, reply_markup=main_kb)
        await callback.answer("✅ Подписка подтверждена! Доступ открыт!", show_alert=True)
    else:
        await callback.answer("❌ Вы не подписались на все каналы!", show_alert=True)


@dp.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа к админ-панели!")
        return
    await state.clear()
    await message.answer("🔐 Добро пожаловать в админ-панель!", reply_markup=admin_kb)


@dp.message(F.text == "📝 Управление заданиями")
async def manage_tasks(message: Message):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    text = "📝 <b>Управление заданиями</b>\n\n"
    if tasks_list:
        text += f"📋 Активных заданий: {len(tasks_list)}\n\n"
        for task_id, task in tasks_list.items():
            channel_type = "🔒 Приватный" if is_private_channel(task['url']) else "🌐 Публичный"
            text += f"🆔 <b>ID: {task_id}</b>\n"
            text += f"📌 Название: {task['name']}\n"
            text += f"🔗 Ссылка: {task['url']}\n"
            text += f"📡 Тип: {channel_type}\n"
            text += f"💰 Награда: {task['reward']}⭐\n"
            text += f"{'─' * 40}\n\n"
    else:
        text += "❌ Нет заданий\n"
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=tasks_admin_kb)


@dp.callback_query(F.data.startswith("task_"))
async def handle_tasks_admin(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.from_user.id)
    if not is_admin(user_id):
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return
    action = callback.data.replace("task_", "")
    if action == "add":
        await callback.message.delete()
        await callback.message.answer(
            "➕ <b>Добавление задания</b>\n\n"
            "Введите название задания:\n"
            "Пример: <code>Подпишись на канал</code>\n\n"
            "❌ Для отмены /cancel",
            parse_mode=ParseMode.HTML
        )
        await state.set_state(AdminStates.waiting_for_task_name)
        await callback.answer()
    elif action == "delete":
        if not tasks_list:
            await callback.answer("❌ Нет заданий для удаления!", show_alert=True)
            return
        text = "🗑 <b>Удаление задания</b>\n\n"
        for task_id, task in tasks_list.items():
            text += f"🆔 ID: {task_id} - {task['name']} | +{task['reward']}⭐\n"
            text += f"🔗 Ссылка: {task['url']}\n\n"
        text += "\n✏️ Введите ID задания для удаления:"
        await callback.message.delete()
        await callback.message.answer(text, parse_mode=ParseMode.HTML)
        await state.set_state(AdminStates.waiting_for_task_delete)
        await callback.answer()
    elif action == "list":
        if not tasks_list:
            await callback.answer("❌ Нет заданий!", show_alert=True)
            return
        text = "📋 <b>СПИСОК ВСЕХ ЗАДАНИЙ:</b>\n\n"
        for task_id, task in tasks_list.items():
            channel_type = "🔒 Приватный (проверка заявки)" if is_private_channel(task['url']) else "🌐 Публичный (проверка подписки)"
            text += f"🆔 <b>ID: {task_id}</b>\n"
            text += f"📌 Название: {task['name']}\n"
            text += f"🔗 Ссылка: {task['url']}\n"
            text += f"📡 {channel_type}\n"
            text += f"💰 Награда: {task['reward']}⭐\n"
            text += f"{'═' * 40}\n\n"
        await callback.message.answer(text, parse_mode=ParseMode.HTML)
        await callback.answer()
    elif action == "back":
        await callback.message.delete()
        await callback.message.answer("🔐 Админ-панель", reply_markup=admin_kb)
        await callback.answer()


@dp.message(AdminStates.waiting_for_task_name)
async def add_task_name(message: Message, state: FSMContext):
    admin_id = int(message.from_user.id)
    if not is_admin(admin_id):
        await state.clear()
        return
    task_name = message.text.strip()
    await state.update_data(task_name=task_name)
    await message.answer(
        "🔗 <b>Введите ссылку</b>\n\n"
        "Введите ссылку на канал/группу:\n"
        "Для публичного канала: <code>https://t.me/example</code>\n"
        "Для приватного канала: <code>https://t.me/+invite_link</code>\n\n"
        "❌ Для отмены /cancel",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(AdminStates.waiting_for_task_url)


@dp.message(AdminStates.waiting_for_task_url)
async def add_task_url(message: Message, state: FSMContext):
    admin_id = int(message.from_user.id)
    if not is_admin(admin_id):
        await state.clear()
        return
    task_url = message.text.strip()
    await state.update_data(task_url=task_url)
    if is_private_channel(task_url):
        await message.answer(
            "🔒 <b>Приватный канал</b>\n\n"
            "Проверка будет по отправке заявки на вступление.\n\n"
            "💰 <b>Введите награду</b>\n\n"
            "Пример: <code>0.25</code> или <code>0.5</code>\n\n"
            "❌ Для отмены /cancel",
            parse_mode=ParseMode.HTML
        )
    else:
        await message.answer(
            "🌐 <b>Публичный канал</b>\n\n"
            "Проверка будет по реальной подписке через API.\n\n"
            "💰 <b>Введите награду</b>\n\n"
            "Пример: <code>0.25</code> или <code>0.5</code>\n\n"
            "❌ Для отмены /cancel",
            parse_mode=ParseMode.HTML
        )
    await state.set_state(AdminStates.waiting_for_task_reward)


@dp.message(AdminStates.waiting_for_task_reward)
async def add_task_reward(message: Message, state: FSMContext):
    admin_id = int(message.from_user.id)
    if not is_admin(admin_id):
        await state.clear()
        return
    try:
        reward = float(message.text.strip().replace(',', '.'))
        if reward <= 0:
            raise ValueError
    except:
        await message.answer("❌ Неверный формат! Введите число больше 0. Пример: 0.25")
        return
    data = await state.get_data()
    task_name = data.get("task_name")
    task_url = data.get("task_url")
    max_id = 0
    for tid in tasks_list.keys():
        try:
            max_id = max(max_id, int(tid))
        except:
            pass
    new_id = str(max_id + 1)
    tasks_list[new_id] = {
        "name": task_name,
        "url": task_url,
        "reward": reward
    }
    save_tasks()
    await message.answer(
        f"✅ <b>Задание добавлено!</b>\n\n"
        f"🆔 ID: {new_id}\n"
        f"📌 Название: {task_name}\n"
        f"🔗 Ссылка: {task_url}\n"
        f"💰 Награда: {reward}⭐\n\n"
        f"💡 Пользователи смогут увидеть новое задание, нажав на кнопку «🎯 Задания» или обновив список.",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_kb
    )
    await state.clear()


@dp.message(AdminStates.waiting_for_task_delete)
async def delete_task(message: Message, state: FSMContext):
    admin_id = int(message.from_user.id)
    if not is_admin(admin_id):
        await state.clear()
        return
    task_id = message.text.strip()
    if task_id not in tasks_list:
        await message.answer(f"❌ Задание с ID {task_id} не найдено!", reply_markup=admin_kb)
        await state.clear()
        return
    task_name = tasks_list[task_id]["name"]
    del tasks_list[task_id]
    save_tasks()
    await message.answer(
        f"✅ <b>Задание удалено!</b>\n\n"
        f"🆔 ID: {task_id}\n"
        f"📌 Название: {task_name}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_kb
    )
    await state.clear()


async def show_current_task(message: Message, user_id: str):
    task_id, task = get_first_available_task(user_id)
    if not task_id:
        has_skipped = False
        for tid in tasks_list.keys():
            if not has_completed_task(user_id, tid) and has_skipped_task(user_id, tid):
                has_skipped = True
                break
        refresh_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить новые задания", callback_data="refresh_tasks")],
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")]
        ])
        if has_skipped:
            await message.answer(
                "📭 Вы выполнили все доступные задания!\n\n"
                "💡 У вас есть пропущенные задания. Нажмите «Проверить новые задания», чтобы вернуться к ним.",
                reply_markup=refresh_kb
            )
        else:
            await message.answer(
                "📭 Вы выполнили все задания!\n\n"
                "✨ Новые задания появятся позже.",
                reply_markup=refresh_kb
            )
        return
    active_task[user_id] = task_id
    if user_id in task_requests:
        del task_requests[user_id]
    save_data()
    skip_note = ""
    if has_skipped_task(user_id, task_id):
        skip_note = "\n\n⚠️ Это задание вы пропустили ранее. Теперь оно доступно для выполнения!"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Перейти", url=task["url"]), InlineKeyboardButton(text="✅ Проверить", callback_data=f"check_{task_id}")],
        [InlineKeyboardButton(text="⏩ Пропустить", callback_data=f"skip_{task_id}")],
    ])
    await message.answer(
        f"💡 Получай <b>Звёзды</b> за <b>простые</b> <b>задания</b>! 👇\n\n"
        f"Подпишись на <a href='{task['url']}'>канал</a> и нажми «Проверить»\n\n"
        f"Вознаграждение: +{task['reward']}⭐{skip_note}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,  # ← ЭТО ОТКЛЮЧАЕТ ПРЕВЬЮ
        reply_markup=markup
    )


@dp.callback_query(F.data == "refresh_tasks")
async def refresh_tasks(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    has_any = False
    for task_id in tasks_list.keys():
        if not has_completed_task(user_id, task_id):
            has_any = True
            break
    if has_any:
        await callback.message.delete()
        await show_current_task(callback.message, user_id)
        await callback.answer("🔄 Список заданий обновлен!", show_alert=True)
    else:
        await callback.answer("❌ Новых заданий пока нет! Зайдите позже.", show_alert=True)


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    if user_id in active_task:
        del active_task[user_id]
    if user_id in task_requests:
        del task_requests[user_id]
    save_data()
    text = (
        "👥 <b>Приглашать пользователей — самый простой способ получения звёзд</b>\n\n"
        "⭐ <b>Нажми «Заработать звёзды»</b>\n\n"
        "<b>Выведено уже более 100.000 звёзд</b>"
    )
    await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_kb)
    await callback.answer()


@dp.message(F.text == "🎯 Задания")
async def tasks_start(message: Message):
    user_id = str(message.from_user.id)
    if user_id not in captcha_passed or not captcha_passed[user_id].get("passed", False):
        await message.answer("⚠️ Сначала пройдите капчу! Напишите /start")
        return
    if not has_sent_request(user_id):
        await message.answer(
            "⚠️ Сначала отправьте заявку на подписку!",
            reply_markup=subscribe_keyboard()
        )
        return
    if not tasks_list:
        await message.answer(
            "📭 Заданий пока нет!\n\n"
            "Зайдите позже ✨",
            reply_markup=main_kb
        )
        return
    await show_current_task(message, user_id)


@dp.callback_query(F.data.startswith("check_"))
async def check_task(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    task_id = callback.data.replace("check_", "")
    user_id_int = int(user_id)
    if task_id not in tasks_list:
        await callback.answer("❌ Задание не найдено!", show_alert=True)
        return
    if has_completed_task(user_id, task_id):
        await callback.answer("❌ Вы уже выполнили это задание!", show_alert=True)
        return
    if has_skipped_task(user_id, task_id):
        await callback.answer("❌ Вы пропустили это задание!", show_alert=True)
        return
    task = tasks_list[task_id]
    is_subscribed = await check_subscription(user_id_int, task["url"])
    if is_subscribed:
        mark_task_completed(user_id, task_id)
        if user_id not in referral_codes:
            referral_codes[user_id] = {"referrals": 0, "earned": 0}
        referral_codes[user_id]["earned"] += task["reward"]
        save_data()
        if user_id in active_task:
            del active_task[user_id]
        if user_id in task_requests:
            del task_requests[user_id]
        save_data()
        await callback.message.delete()
        await callback.message.answer(
            f"✅ <b>Задание выполнено!</b>\n\n"
            f"📌 {task['name']}\n"
            f"💰 +{task['reward']}⭐️ начислено!\n\n"
            f"💎 Ваш баланс: {referral_codes[user_id]['earned']:.2f}⭐\n\n"
            f"<blockquote><i>❗️ Не отписывайся от канала в течение как минимум 3 дней.\nВ противном случае, ты получишь штраф или блокировку аккаунта.</i></blockquote>",
            parse_mode=ParseMode.HTML
        )
        await callback.answer("✅ Награда получена!", show_alert=True)
        await asyncio.sleep(2)
        await show_current_task(callback.message, user_id)
    else:
        if is_private_channel(task["url"]):
            await callback.answer(
                "❌ Задание не выполнено!\n\n"
                "Пожалуйста:\n"
                "1. Нажми «Перейти»\n"
                "2. Нажми кнопку «Вступить» в канале\n"
                "3. Затем нажми «Проверить»",
                show_alert=True
            )
        else:
            await callback.answer(
                "❌ Задание не выполнено!\n\n"
                "Пожалуйста:\n"
                "1. Нажми «Перейти»\n"
                "2. Подпишись на канал\n"
                "3. Затем нажми «Проверить»",
                show_alert=True
            )


@dp.callback_query(F.data.startswith("skip_"))
async def skip_task(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    task_id = callback.data.replace("skip_", "")
    if task_id not in tasks_list:
        await callback.answer("❌ Задание не найдено!", show_alert=True)
        return
    if has_completed_task(user_id, task_id):
        await callback.answer("❌ Вы уже выполнили это задание!", show_alert=True)
        return
    if has_skipped_task(user_id, task_id):
        await callback.answer("❌ Вы уже пропустили это задание!", show_alert=True)
        return
    mark_task_skipped(user_id, task_id)
    if user_id in active_task:
        del active_task[user_id]
    if user_id in task_requests:
        del task_requests[user_id]
    save_data()
    await callback.message.delete()
    await callback.answer("⏩ Задание пропущено!", show_alert=True)
    await show_current_task(callback.message, user_id)


@dp.message(F.text == "📥 Выгрузить пользователей TXT")
async def export_users_txt(message: Message):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    if not users_db:
        await message.answer("📭 Нет пользователей для выгрузки!")
        return
    try:
        temp_file = f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(temp_file, 'w', encoding='utf-8') as f:
            f.write(f"Экспорт пользователей от {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Всего пользователей: {len(users_db)}\n")
            f.write("=" * 50 + "\n\n")
            for uid, udata in users_db.items():
                username = udata.get("username", "Нет username")
                full_name = udata.get("full_name", "Неизвестно")
                joined_at = udata.get("joined_at", "Неизвестно")
                last_active = udata.get("last_active", "Неизвестно")
                earned = referral_codes.get(uid, {}).get("earned", 0)
                referrals_count = referral_codes.get(uid, {}).get("referrals", 0)
                sent_request = "Да" if has_sent_request(uid) else "Нет"
                f.write(f"ID: {uid}\n")
                f.write(f"Имя: {full_name}\n")
                f.write(f"Username: @{username}\n")
                f.write(f"Баланс: {earned}⭐\n")
                f.write(f"Пригласил: {referrals_count}\n")
                f.write(f"Отправил заявку: {sent_request}\n")
                f.write(f"Присоединился: {joined_at}\n")
                f.write(f"Последний визит: {last_active}\n")
                f.write("-" * 50 + "\n\n")
        document = FSInputFile(temp_file)
        await message.answer_document(
            document,
            caption=f"📊 Выгрузка пользователей\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n👥 Всего: {len(users_db)} пользователей"
        )
        os.remove(temp_file)
        await message.answer("✅ Файл успешно создан и отправлен!")
    except Exception as e:
        await message.answer(f"❌ Ошибка при выгрузке: {str(e)}")


@dp.message(F.text == "📋 Заявки на вывод")
async def view_withdraw_requests(message: Message):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    if not withdraw_requests:
        await message.answer("📭 Нет активных заявок на вывод!")
        return
    text = "📋 <b>Заявки на вывод:</b>\n\n"
    for req_id, req_data in withdraw_requests.items():
        status_emoji = "⏳" if req_data['status'] == 'pending' else "✅"
        text += (
            f"{status_emoji} <b>Заявка #{req_id}</b>\n"
            f"👤 Пользователь: @{req_data['username']}\n"
            f"🆔 ID: <code>{req_data['user_id']}</code>\n"
            f"💰 Сумма: {req_data['amount']}⭐\n"
            f"📅 Создана: {req_data['created_at']}\n"
            f"📊 Статус: {req_data['status']}\n"
            f"{'─' * 30}\n\n"
        )
    await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(F.text == "✅ Обработать заявку")
async def process_withdraw_request(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    pending_requests = []
    for req_id, req_data in withdraw_requests.items():
        if req_data["status"] == "pending":
            pending_requests.append((req_id, req_data))
    if not pending_requests:
        await message.answer("📭 Нет активных заявок на вывод!")
        return
    text = "📋 <b>Активные заявки на вывод:</b>\n\n"
    for req_id, req_data in pending_requests:
        text += f"🆔 #{req_id} | @{req_data['username']} | {req_data['amount']}⭐\n"
    text += "\n✏️ Введите номер заявки, которую хотите отметить как выполненную:"
    await message.answer(text, parse_mode=ParseMode.HTML)
    await state.set_state(AdminStates.waiting_for_request_id)


@dp.message(AdminStates.waiting_for_request_id)
async def complete_withdraw_request(message: Message, state: FSMContext):
    admin_id = int(message.from_user.id)
    if not is_admin(admin_id):
        await state.clear()
        await message.answer("⛔ У вас нет доступа!")
        return
    req_id = message.text.strip()
    if req_id not in withdraw_requests:
        await message.answer(f"❌ Заявка #{req_id} не найдена!", reply_markup=admin_kb)
        await state.clear()
        return
    if withdraw_requests[req_id]["status"] != "pending":
        await message.answer(f"❌ Заявка #{req_id} уже обработана!", reply_markup=admin_kb)
        await state.clear()
        return
    withdraw_requests[req_id]["status"] = "completed"
    save_data()
    user_id = int(withdraw_requests[req_id]["user_id"])
    amount = withdraw_requests[req_id]["amount"]
    username = withdraw_requests[req_id]["username"]
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Ваша заявка #{req_id} на вывод {amount}⭐ выполнена!</b>\n\n"
            f"🎁 Подарок отправлен!\n"
            f"Спасибо что пользуетесь нашим ботом! 💫",
            parse_mode=ParseMode.HTML
        )
    except:
        pass
    await message.answer(
        f"✅ Заявка #{req_id} отмечена как выполненная!\n"
        f"👤 Пользователь @{username} получил уведомление.",
        reply_markup=admin_kb
    )
    await state.clear()


@dp.message(F.text == "🧹 Очистить всех")
async def clear_all_users(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    total_users = len(users_db)
    total_earned = sum(c.get("earned", 0) for c in referral_codes.values())
    total_referrals = sum(c.get("referrals", 0) for c in referral_codes.values())
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ ДА, ОЧИСТИТЬ ВСЁ", callback_data="clear_confirm"),
            InlineKeyboardButton(text="❌ НЕТ, ОТМЕНА", callback_data="clear_cancel")
        ]
    ])
    await state.set_state(AdminStates.waiting_for_clear_confirmation)
    await message.answer(
        f"⚠️ <b>ВНИМАНИЕ! ОПАСНОЕ ДЕЙСТВИЕ!</b> ⚠️\n\n"
        f"Вы собираетесь ПОЛНОСТЬЮ ОЧИСТИТЬ все данные бота!\n\n"
        f"📊 <b>Будет удалено:</b>\n"
        f"👥 Пользователей: {total_users}\n"
        f"💰 Всего звёзд: {total_earned:.2f}⭐\n"
        f"👥 Всего рефералов: {total_referrals}\n\n"
        f"❓ <b>Вы уверены, что хотите продолжить?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=confirm_kb
    )


@dp.callback_query(F.data == "clear_confirm")
async def confirm_clear_all(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.from_user.id)
    if not is_admin(user_id):
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return
    backup_data()
    clear_all_data()
    await callback.message.delete()
    await callback.message.answer(
        "✅ <b>ВСЕ ДАННЫЕ УСПЕШНО ОЧИЩЕНЫ!</b>\n\n"
        "🔄 Бот теперь как новый!",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_kb
    )
    await callback.answer("✅ Очистка выполнена!")
    await state.clear()


@dp.callback_query(F.data == "clear_cancel")
async def cancel_clear_all(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.from_user.id)
    if not is_admin(user_id):
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return
    await callback.message.delete()
    await callback.message.answer("❌ Очистка данных отменена.", reply_markup=admin_kb)
    await callback.answer("Отменено")
    await state.clear()


@dp.message(F.text == "🔄 Сброс рефералов")
async def reset_referrals(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    await state.set_state(AdminStates.waiting_for_user_id_reset)
    await message.answer(
        "🔄 <b>Сброс рефералов</b>\n\n"
        "Введите ID пользователя:\n"
        "Пример: <code>7636031451</code>\n\n"
        "Или отправьте <b>все</b> чтобы обнулить всех\n\n"
        "❌ Для отмены /cancel",
        parse_mode=ParseMode.HTML
    )


@dp.message(AdminStates.waiting_for_user_id_reset)
async def process_reset_referrals(message: Message, state: FSMContext):
    admin_id = int(message.from_user.id)
    target = message.text.strip()
    if not is_admin(admin_id):
        await state.clear()
        await message.answer("⛔ У вас нет доступа!")
        return
    if target.lower() == "все":
        count = 0
        for uid in referral_codes:
            referral_codes[uid]["referrals"] = 0
            count += 1
        save_data()
        await message.answer(f"✅ Обнулены рефералы у {count} пользователей", reply_markup=admin_kb)
    elif target.isdigit():
        target_id = target
        if target_id in referral_codes:
            old_referrals = referral_codes[target_id]["referrals"]
            referral_codes[target_id]["referrals"] = 0
            save_data()
            user_info = users_db.get(target_id, {})
            username = user_info.get("username", "нет username")
            full_name = user_info.get("full_name", "неизвестно")
            await message.answer(
                f"✅ Рефералы обнулены!\n\n"
                f"🆔 ID: <code>{target_id}</code>\n"
                f"👤 Имя: {full_name}\n"
                f"📱 Username: @{username}\n"
                f"📊 Было: {old_referrals}\n"
                f"📊 Стало: 0",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_kb
            )
        else:
            await message.answer(
                f"❌ Пользователь с ID <code>{target_id}</code> не найден!",
                parse_mode=ParseMode.HTML
            )
            return
    else:
        await message.answer(
            "❌ Неверный формат! Введите ID или 'все'.",
            parse_mode=ParseMode.HTML
        )
        return
    await state.clear()


@dp.message(F.text == "💰 Редактировать баланс")
async def edit_balance(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    await state.set_state(AdminStates.waiting_for_user_id_balance)
    await message.answer(
        "💰 <b>Редактирование баланса</b>\n\n"
        "Введите ID и сумму через пробел:\n"
        "Пример: <code>7636031451 10.5</code>\n\n"
        "Можно с + или -:\n"
        "<code>7636031451 +5</code> (прибавить)\n"
        "<code>7636031451 -3</code> (отнять)\n\n"
        "❌ Для отмены /cancel",
        parse_mode=ParseMode.HTML
    )


@dp.message(AdminStates.waiting_for_user_id_balance)
async def process_edit_balance(message: Message, state: FSMContext):
    admin_id = int(message.from_user.id)
    text = message.text.strip()
    if not is_admin(admin_id):
        await state.clear()
        await message.answer("⛔ У вас нет доступа!")
        return
    try:
        parts = text.split()
        if len(parts) != 2:
            raise ValueError("Неверный формат")
        target_id = parts[0]
        value = parts[1]
        if target_id not in referral_codes:
            await message.answer(
                f"❌ Пользователь с ID <code>{target_id}</code> не найден!",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_kb
            )
            await state.clear()
            return
        old_balance = referral_codes[target_id]["earned"]
        if value.startswith('+'):
            delta = float(value[1:])
            new_balance = old_balance + delta
            operation = f"+{delta}"
        elif value.startswith('-'):
            delta = float(value[1:])
            new_balance = old_balance - delta
            operation = f"-{delta}"
        else:
            new_balance = float(value)
            operation = f"= {new_balance}"
        if new_balance < 0:
            await message.answer(
                "❌ Баланс не может быть отрицательным!",
                reply_markup=admin_kb
            )
            await state.clear()
            return
        referral_codes[target_id]["earned"] = new_balance
        save_data()
        user_info = users_db.get(target_id, {})
        username = user_info.get("username", "нет username")
        full_name = user_info.get("full_name", "неизвестно")
        await message.answer(
            f"✅ Баланс обновлён!\n\n"
            f"🆔 ID: <code>{target_id}</code>\n"
            f"👤 Имя: {full_name}\n"
            f"📱 Username: @{username}\n"
            f"📊 Операция: {operation}⭐\n"
            f"💰 Было: {old_balance}⭐\n"
            f"💰 Стало: {new_balance}⭐",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_kb
        )
    except ValueError:
        await message.answer(
            "❌ Неверный формат!\n\n"
            "Пример: <code>7636031451 10.5</code>",
            parse_mode=ParseMode.HTML
        )
        return
    await state.clear()


@dp.message(F.text == "👥 Просмотр пользователей")
async def view_users(message: Message):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    if not users_db:
        await message.answer("📭 Пользователей пока нет")
        return
    users_list = list(users_db.items())
    total_pages = (len(users_list) + 9) // 10
    pagination_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="users_page_prev"),
            InlineKeyboardButton(text=f"1/{total_pages}", callback_data="users_page_info"),
            InlineKeyboardButton(text="Вперед ▶️", callback_data="users_page_next")
        ],
        [InlineKeyboardButton(text="📥 Экспорт в JSON", callback_data="export_users")]
    ])
    await show_users_page(message, 0, users_list, pagination_kb, total_pages)


async def show_users_page(message: Message, page: int, users_list: list, kb: InlineKeyboardMarkup, total_pages: int):
    start_idx = page * 10
    end_idx = min(start_idx + 10, len(users_list))
    text = "👥 <b>Список пользователей:</b>\n\n"
    for i in range(start_idx, end_idx):
        user_id, user_data = users_list[i]
        username = user_data.get("username", "Нет username")
        full_name = user_data.get("full_name", "Неизвестно")
        joined_at = user_data.get("joined_at", "Неизвестно")
        last_active = user_data.get("last_active", "Неизвестно")
        earned = referral_codes.get(user_id, {}).get("earned", 0)
        referrals_count = referral_codes.get(user_id, {}).get("referrals", 0)
        sent_request = "✅" if has_sent_request(user_id) else "❌"
        text += (
            f"🆔 ID: <code>{user_id}</code>\n"
            f"👤 Имя: {full_name}\n"
            f"📱 Username: @{username}\n"
            f"💰 Баланс: {earned}⭐\n"
            f"👥 Пригласил: {referrals_count}\n"
            f"📝 Заявка: {sent_request}\n"
            f"📅 Присоединился: {joined_at}\n"
            f"🕐 Последний визит: {last_active}\n"
            f"{'─' * 30}\n"
        )
    text += f"\n📊 Страница {page + 1} из {total_pages}\n"
    text += f"📈 Всего пользователей: {len(users_list)}"
    kb.inline_keyboard[0][1] = InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="users_page_info")
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@dp.callback_query(F.data.startswith("users_page_"))
async def handle_users_pagination(callback: CallbackQuery):
    user_id = int(callback.from_user.id)
    if not is_admin(user_id):
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return
    if not hasattr(handle_users_pagination, "current_page"):
        handle_users_pagination.current_page = 0
    action = callback.data.replace("users_page_", "")
    if action == "next":
        handle_users_pagination.current_page += 1
    elif action == "prev":
        handle_users_pagination.current_page = max(0, handle_users_pagination.current_page - 1)
    elif action == "info":
        await callback.answer(f"Страница {handle_users_pagination.current_page + 1}", show_alert=True)
        return
    users_list = list(users_db.items())
    total_pages = (len(users_list) + 9) // 10
    if handle_users_pagination.current_page >= total_pages and total_pages > 0:
        handle_users_pagination.current_page = total_pages - 1
    elif total_pages == 0:
        await callback.message.delete()
        await callback.message.answer("📭 Пользователей пока нет", reply_markup=admin_kb)
        await callback.answer()
        return
    pagination_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="users_page_prev"),
            InlineKeyboardButton(text=f"{handle_users_pagination.current_page + 1}/{total_pages}",
                                 callback_data="users_page_info"),
            InlineKeyboardButton(text="Вперед ▶️", callback_data="users_page_next")
        ],
        [InlineKeyboardButton(text="📥 Экспорт в JSON", callback_data="export_users")]
    ])
    await callback.message.delete()
    await show_users_page(callback.message, handle_users_pagination.current_page, users_list, pagination_kb,
                          total_pages)
    await callback.answer()


@dp.callback_query(F.data == "export_users")
async def export_users(callback: CallbackQuery):
    user_id = int(callback.from_user.id)
    if not is_admin(user_id):
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return
    export_data = []
    for uid, udata in users_db.items():
        export_data.append({
            "user_id": uid,
            "username": udata.get("username"),
            "full_name": udata.get("full_name"),
            "joined_at": udata.get("joined_at"),
            "last_active": udata.get("last_active"),
            "balance": referral_codes.get(uid, {}).get("earned", 0),
            "referrals": referral_codes.get(uid, {}).get("referrals", 0),
            "request_sent": has_sent_request(uid)
        })
    temp_file = f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(temp_file, 'w', encoding='utf-8') as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    document = FSInputFile(temp_file)
    await callback.message.answer_document(
        document,
        caption=f"📊 Экспорт пользователей от {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nВсего: {len(export_data)} пользователей"
    )
    os.remove(temp_file)
    await callback.answer("✅ Экспорт выполнен!")


@dp.message(F.text == "📊 Статистика")
async def view_stats(message: Message):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    total_users = len(users_db)
    total_passed_captcha = sum(1 for u in captcha_passed.values() if u.get("passed", False))
    total_earned = sum(c.get("earned", 0) for c in referral_codes.values())
    total_referrals = sum(c.get("referrals", 0) for c in referral_codes.values())
    total_request_sent = sum(1 for uid in users_db.keys() if has_sent_request(uid))
    pending_requests = sum(1 for r in withdraw_requests.values() if r["status"] == "pending")
    total_completed_tasks = sum(len(tasks) for tasks in user_tasks_completed.values())
    text = (
        "📊 <b>Статистика бота:</b>\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"✅ Прошли капчу: {total_passed_captcha}\n"
        f"📝 Отправили заявку: {total_request_sent}\n"
        f"💰 Всего начислено звёзд: {total_earned:.2f}⭐\n"
        f"👥 Всего рефералов: {total_referrals}\n"
        f"📋 Заявок на вывод: {pending_requests} активных\n"
        f"🎯 Выполнено заданий: {total_completed_tasks}"
    )
    if total_users > 0:
        text += f"\n📈 Конверсия капчи: {total_passed_captcha / total_users * 100:.1f}%"
    await message.answer(text, parse_mode=ParseMode.HTML)


class MailingStates(StatesGroup):
    waiting_for_content = State()
    waiting_for_confirmation = State()


@dp.message(F.text == "📨 Рассылка")
async def start_mailing(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    await state.set_state(MailingStates.waiting_for_content)
    await message.answer(
        "📨 <b>Создание рассылки</b>\n\n"
        "Отправьте сообщение для рассылки.\n\n"
        "❌ Для отмены /cancel",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("cancel"))
async def cancel_action(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        user_id = int(message.from_user.id)
        if is_admin(user_id):
            await message.answer("❌ Действие отменено", reply_markup=admin_kb)
        else:
            await message.answer("❌ Действие отменено", reply_markup=main_kb)
    else:
        await message.answer("Нет активного действия")


@dp.message(MailingStates.waiting_for_content)
async def get_mailing_content(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await state.clear()
        await message.answer("⛔ У вас нет доступа!")
        return
    content_data = {
        "type": None,
        "data": None,
        "caption": None,
        "text": None
    }
    if message.text:
        content_data["type"] = "text"
        content_data["text"] = message.text
    elif message.photo:
        content_data["type"] = "photo"
        content_data["data"] = message.photo[-1].file_id
        content_data["caption"] = message.caption
    elif message.sticker:
        content_data["type"] = "sticker"
        content_data["data"] = message.sticker.file_id
    else:
        await message.answer("❌ Этот тип сообщения не поддерживается.")
        return
    await state.update_data(mailing_content=content_data)
    total_users = len(users_db)
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="mailing_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="mailing_cancel")
        ]
    ])
    await message.answer(f"📨 <b>Предпросмотр</b>\n\n👥 Получателей: {total_users}", parse_mode=ParseMode.HTML)
    if content_data["type"] == "text":
        await message.answer(content_data["text"], reply_markup=confirm_kb, parse_mode=ParseMode.HTML)
    elif content_data["type"] == "photo":
        await message.answer_photo(content_data["data"], caption=content_data["caption"],
                                   reply_markup=confirm_kb, parse_mode=ParseMode.HTML)
    elif content_data["type"] == "sticker":
        await message.answer_sticker(content_data["data"])
        await message.answer("✅ Стикер будет отправлен", reply_markup=confirm_kb)
    await state.set_state(MailingStates.waiting_for_confirmation)


@dp.callback_query(F.data.startswith("mailing_"))
async def handle_mailing_confirmation(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.from_user.id)
    if not is_admin(user_id):
        await callback.answer("⛔ Нет доступа!", show_alert=True)
        return
    action = callback.data.replace("mailing_", "")
    if action == "cancel":
        await state.clear()
        await callback.message.delete()
        await callback.message.answer("❌ Рассылка отменена", reply_markup=admin_kb)
        await callback.answer("Отменено")
        return
    elif action == "confirm":
        await callback.message.delete()
        await callback.answer("⏳ Начинаю рассылку...")
        data = await state.get_data()
        content = data.get("mailing_content")
        if not content:
            await callback.message.answer("❌ Ошибка: контент не найден")
            await state.clear()
            return
        total_users = len(users_db)
        success_count = 0
        fail_count = 0
        status_msg = await callback.message.answer(
            f"📨 <b>Рассылка начата!</b>\n\n👥 Всего: {total_users}",
            parse_mode=ParseMode.HTML
        )
        for i, user_id_str in enumerate(users_db.keys(), 1):
            try:
                user_id_int = int(user_id_str)
                if content["type"] == "text":
                    await bot.send_message(user_id_int, content["text"], parse_mode=ParseMode.HTML)
                elif content["type"] == "photo":
                    await bot.send_photo(user_id_int, content["data"], caption=content["caption"],
                                         parse_mode=ParseMode.HTML)
                elif content["type"] == "sticker":
                    await bot.send_sticker(user_id_int, content["data"])
                success_count += 1
                if i % 10 == 0:
                    await status_msg.edit_text(
                        f"📨 <b>Рассылка в процессе...</b>\n\n✅ Успешно: {success_count}\n❌ Ошибок: {fail_count}\n📊 {i}/{total_users}",
                        parse_mode=ParseMode.HTML
                    )
                await asyncio.sleep(0.05)
            except Exception as e:
                fail_count += 1
        await status_msg.edit_text(
            f"✅ <b>Рассылка завершена!</b>\n\n✅ Успешно: {success_count}\n❌ Ошибок: {fail_count}",
            parse_mode=ParseMode.HTML
        )
        await callback.message.answer("✅ Рассылка завершена!", reply_markup=admin_kb)
        await state.clear()


@dp.message(F.text == "🔙 Выйти из админ-панели")
async def exit_admin(message: Message, state: FSMContext):
    user_id = int(message.from_user.id)
    if not is_admin(user_id):
        await message.answer("⛔ У вас нет доступа!")
        return
    await state.clear()
    await message.answer("🔐 Вы вышли из админ-панели", reply_markup=main_kb)


@dp.message(F.text == "⭐ Заработать звёзды")
async def earn_stars(message: Message):
    user_id = str(message.from_user.id)
    if user_id not in captcha_passed or not captcha_passed[user_id].get("passed", False):
        await message.answer("⚠️ Сначала пройдите капчу! Напишите /start")
        return
    if not has_sent_request(user_id):
        await message.answer(
            "🔥 Добро пожаловать!\n\n"
            "⚡️ Чтобы получить доступ, подпишитесь на все каналы ниже 👇",
            reply_markup=subscribe_keyboard()
        )
        return
    if user_id not in referral_codes:
        referral_codes[user_id] = {"referrals": 0, "earned": 0}
        save_data()
    referrals = referral_codes[user_id]["referrals"]
    encoded_id = encode_user_id(user_id)
    share_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="📤 Пригласить друга",
                url=f"https://t.me/share/url?url=https://t.me/FlyStarsingBot?start={encoded_id}"
            )]
        ]
    )
    text = (
        "👥 <b>Приглашай пользователей в бота и получай звёзды!</b>\n\n"
        "💎 За каждого приглашённого друга ты получаешь <b>3.00⭐</b>\n\n"
        "<b>Как это работает:</b>\n"
        "• <b>0.35⭐</b> — начисляется сразу после перехода по твоей ссылке\n"
        "• <b>2.65⭐</b> — начисляется после того, как друг подпишется на канал\n\n"
        "💰 Чем больше друзей пригласишь — тем больше звёзд получишь!\n\n"
        "📎 <b>Твоя ссылка:</b>\n"
        f"<code>https://t.me/FlyStarsingBot?start={encoded_id}</code>\n\n"
        "<blockquote>"
        "<b>❓ Как использовать свою реферальную ссылку?</b>\n"
        "• Отправь её друзьям в личные сообщения 👥\n"
        "• Поделись ссылкой в своём Telegram-канале 📣\n"
        "• Оставь её в комментариях или чатах 💬\n"
        "• Распространяй ссылку в соцсетях:\n"
        "TikTok, Instagram, WhatsApp и других 🕸️"
        "</blockquote>\n\n"
        f"🗣 <b>Вы пригласили: {referrals}</b>\n\n"
        "👇 <b>Жми на кнопку ниже и делись ссылкой с друзьями</b>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=share_kb)


@dp.message(F.text == "🎁 Вывести звёзды")
async def withdraw(message: Message):
    user_id = str(message.from_user.id)
    if user_id not in captcha_passed or not captcha_passed[user_id].get("passed", False):
        await message.answer("⚠️ Сначала пройдите капчу! Напишите /start")
        return
    if not has_sent_request(user_id):
        await message.answer(
            "🔥 Добро пожаловать!\n\n"
            "⚡️ Чтобы получить доступ, подпишитесь на все каналы ниже 👇",
            reply_markup=subscribe_keyboard()
        )
        return
    has_active, active_id = has_active_withdraw(user_id)
    earned = referral_codes.get(user_id, {}).get("earned", 0)
    if has_active:
        text = (
            f"💰 <b>Заработано: {earned:.2f}⭐</b>\n\n"
            f"⚠️ <b>У вас уже есть активная заявка #{active_id}!</b>\n\n"
            f"Дождитесь обработки текущей заявки перед созданием новой.\n\n"
            f"🔻 <b>Выбери подарок:</b>"
        )
    else:
        text = (
            f"💰 <b>Заработано: {earned:.2f}⭐</b>\n\n"
            f"🔻 <b>Выбери подарок за сколько звёзд хочешь получить:</b>"
        )
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=stars_inline_kb)


@dp.message(F.text == "💎 Бонус")
async def bonus(message: Message):
    user_id = str(message.from_user.id)
    if user_id not in captcha_passed or not captcha_passed[user_id].get("passed", False):
        await message.answer("⚠️ Сначала пройдите капчу! Напишите /start")
        return
    if not has_sent_request(user_id):
        await message.answer(
            "🔥 Добро пожаловать!\n\n"
            "⚡️ Чтобы получить доступ, подпишитесь на все каналы ниже 👇",
            reply_markup=subscribe_keyboard()
        )
        return
    current_time = datetime.now()
    if user_id in user_bonus:
        last_bonus_time = datetime.fromtimestamp(user_bonus[user_id]["last_bonus"])
        time_since_last = current_time - last_bonus_time
        if time_since_last < timedelta(hours=24):
            next_bonus = last_bonus_time + timedelta(hours=24)
            time_left = next_bonus - current_time
            hours_left = time_left.seconds // 3600
            minutes_left = (time_left.seconds % 3600) // 60
            await message.answer(
                f"⏰ <b>Следующий бонус будет доступен через:</b>\n"
                f"{hours_left} ч {minutes_left} мин\n\n"
                f"🌟 Заходите завтра снова!",
                parse_mode=ParseMode.HTML
            )
            return
    bonus_amount = random.choice([0.25, 0.75, 1.25, 2.00])
    if user_id in referral_codes:
        referral_codes[user_id]["earned"] += bonus_amount
    else:
        referral_codes[user_id] = {"referrals": 0, "earned": bonus_amount}
    user_bonus[user_id] = {
        "last_bonus": current_time.timestamp(),
        "last_amount": bonus_amount
    }
    save_data()
    await message.answer(
        f"🎉 <b>Поздравляем! Вы получили бонус!</b>\n\n"
        f"💰 <b>+{bonus_amount:.2f}⭐</b> начислено на ваш баланс!\n\n"
        f"💎 Ваш баланс: {referral_codes[user_id]['earned']:.2f}⭐",
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(F.data.startswith("stars_"))
async def handle_stars_selection(callback: CallbackQuery):
    global request_counter
    user_id = str(callback.from_user.id)
    stars_count = int(callback.data.replace("stars_", ""))
    if user_id not in captcha_passed or not captcha_passed[user_id].get("passed", False):
        await callback.answer("⚠️ Сначала пройдите капчу! Напишите /start", show_alert=True)
        return
    if not has_sent_request(user_id):
        await callback.message.answer(
            "🔥 Добро пожаловать!\n\n"
            "⚡️ Чтобы получить доступ, подпишитесь на все каналы ниже 👇",
            reply_markup=subscribe_keyboard()
        )
        await callback.answer()
        return
    has_active, active_id = has_active_withdraw(user_id)
    if has_active:
        await callback.answer(
            f"❌ У вас уже есть активная заявка #{active_id}!\n\n"
            f"Дождитесь обработки текущей заявки перед созданием новой.",
            show_alert=True
        )
        return
    current_balance = referral_codes.get(user_id, {}).get("earned", 0)
    if current_balance < stars_count:
        await callback.answer(
            f"❌ Недостаточно звёзд!\n\n"
            f"Ваш баланс: {current_balance:.2f}⭐\n"
            f"Для вывода {stars_count}⭐ не хватает {stars_count - current_balance:.2f}⭐",
            show_alert=True
        )
        return
    user_data = users_db.get(user_id, {})
    username = user_data.get("username")
    if not username:
        await callback.answer(
            "❌ У вас нет username в Telegram!\n\n"
            "Пожалуйста, установите username в настройках Telegram.\n"
            "Это нужно для отправки подарка!",
            show_alert=True
        )
        return
    request_counter += 1
    request_id = request_counter
    withdraw_requests[str(request_id)] = {
        "user_id": user_id,
        "username": username,
        "amount": stars_count,
        "status": "pending",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    referral_codes[user_id]["earned"] -= stars_count
    save_data()
    await callback.message.answer(
        f"✅ <b>Заявка #{request_id} на вывод {stars_count}⭐ создана!</b>\n\n"
        f"⏳ <b>Ваша заявка рассматривается администратором!</b>\n\n"
        f"<b>Отправим тебе подарок в течение 72-х часов, ожидай!</b>\n"
        f"<b>Все заявки на вывод просматриваются в ручную</b>\n\n"
        f"<i>Не меняйте username, иначе мы не сможем отправить подарок, а заявка будет отклонена!</i>",
        parse_mode=ParseMode.HTML
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📋 <b>Новая заявка на вывод!</b>\n\n"
                f"🆔 Заявка #{request_id}\n"
                f"👤 Пользователь: @{username}\n"
                f"🆔 ID: <code>{user_id}</code>\n"
                f"💰 Сумма: {stars_count}⭐\n"
                f"📅 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"➡️ Для обработки заявки нажмите «✅ Обработать заявку» в админ-меню",
                parse_mode=ParseMode.HTML
            )
        except:
            pass
    await callback.answer("✅ Заявка создана! Ожидайте обработки администратором!", show_alert=True)


async def auto_save_loop():
    """Фоновая задача для автосохранения каждые 5 минут"""
    while True:
        await asyncio.sleep(300)
        if cache_dirty:
            force_save_data()


async def set_commands():
    base_commands = [
        BotCommand(command="start", description="🚀 Запустить бота"),
    ]
    await bot.set_my_commands(base_commands)
    for admin_id in ADMIN_IDS:
        try:
            admin_commands = [
                BotCommand(command="start", description="🚀 Запустить бота"),
                BotCommand(command="admin", description="🔐 Админ-панель"),
                BotCommand(command="cancel", description="❌ Отменить действие"),
            ]
            await bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception as e:
            print(f"Не удалось установить команды для админа {admin_id}: {e}")


async def delete_webhook():
    await bot.delete_webhook(drop_pending_updates=True)
    print("Webhook очищен")


async def main():
    print("Проверяю другие процессы бота...")
    kill_other_bot_processes()
    load_data()
    load_tasks()
    await delete_webhook()
    await asyncio.sleep(2)
    await set_commands()
    # Запускаем фоновое автосохранение
    asyncio.create_task(auto_save_loop())
    print("Бот запущен и готов к работе!")
    try:
        await dp.start_polling(bot)
    finally:
        force_save_data()
        print("Данные сохранены при остановке")


if __name__ == "__main__":
    asyncio.run(main())
