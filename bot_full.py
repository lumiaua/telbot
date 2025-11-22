
# -*- coding: utf-8 -*-
"""Anonymous Chat Bot — single file production-ready skeleton
Features included:
- Anonymous random pairing (find / stop)
- User profiles (create/edit/view)
- Reputation system
- Balance & VIP status
- Simple mini-games: Rock-Paper-Scissors and Guess-the-number (1v1)
- Reporting users (complaint) -> notifies admins
- Admin commands: /admin_panel (list users, ban/unban, mute, add_balance, give_vip)
- Donate flow: creates a local invoice ID and gives user a deep-link to @CryptoBot to pay
  (you must create a Crypto Pay token and use it locally — instructions in README)
- Uses SQLite for storage
- Single-file: insert your BOT_TOKEN and optional ADMIN_IDS and CRYPTO_PAY settings
Notes:
- DO NOT share your tokens. Insert them locally before running.
- This file is a starting point. For production, secure the server, use HTTPS if enabling webhooks,
  and consider migrating SQLite -> PostgreSQL for scale.
"""

import asyncio
import logging
import sqlite3
import secrets
import time
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, Text
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# ============================
# === CONFIGURATION SECTION ==
# ============================
# Insert your tokens here (do NOT commit them to public repos)
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
# Example of admin ids: [123456789]
ADMIN_IDS = []  # put your Telegram numeric user id(s) here

# Crypto donation settings:
# We will generate a local invoice id and create a CryptoBot deep link:
# https://t.me/CryptoBot?start=<invoice_id>
# When user pays via CryptoBot, you must manually (or by advanced webhook/polling) verify payment.
CRYPTO_DEEP_LINK_BASE = "https://t.me/CryptoBot?start="

# Database file
DB_FILE = "anon_chat_bot.db"

# ============================
# === Logging configuration ==
# ============================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================
# === Database helpers =======
# ============================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    # users table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            about TEXT,
            created_at TEXT,
            reputation INTEGER DEFAULT 0,
            balance INTEGER DEFAULT 0,
            vip_until TEXT DEFAULT NULL,
            banned INTEGER DEFAULT 0,
            muted_until TEXT DEFAULT NULL
        )
    ''')
    # pairing queue
    cur.execute('''
        CREATE TABLE IF NOT EXISTS pairing (
            user_id INTEGER PRIMARY KEY,
            looking_since TEXT
        )
    ''')
    # active chats
    cur.execute('''
        CREATE TABLE IF NOT EXISTS chats (
            user_id INTEGER PRIMARY KEY,
            peer_id INTEGER
        )
    ''')
    # complaints
    cur.execute('''
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            complainer INTEGER,
            target INTEGER,
            reason TEXT,
            created_at TEXT,
            handled INTEGER DEFAULT 0
        )
    ''')
    # invoices
    cur.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount INTEGER,
            created_at TEXT,
            paid INTEGER DEFAULT 0
        )
    ''')
    # games table (simple storage)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS games (
            user_id INTEGER PRIMARY KEY,
            game_type TEXT,
            state TEXT,
            peer_id INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch=False, many=False):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    if many:
        cur.executemany(query, params)
        conn.commit()
        conn.close()
        return None
    cur.execute(query, params)
    result = None
    if fetch:
        result = cur.fetchall()
    conn.commit()
    conn.close()
    return result

# ============================
# === Utility functions ======
# ============================
def ensure_user(user: types.User):
    now = datetime.utcnow().isoformat()
    existing = db_execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,), fetch=True)
    if not existing:
        db_execute(
            "INSERT INTO users (user_id, username, display_name, about, created_at) VALUES (?, ?, ?, ?, ?)",
            (user.id, user.username or "", user.full_name, "", now)
        )

def is_admin(user_id: int):
    return user_id in ADMIN_IDS

def is_banned(user_id: int):
    r = db_execute("SELECT banned FROM users WHERE user_id = ?", (user_id,), fetch=True)
    if not r:
        return False
    return r[0][0] == 1

def is_muted(user_id: int):
    r = db_execute("SELECT muted_until FROM users WHERE user_id = ?", (user_id,), fetch=True)
    if not r or r[0][0] is None:
        return False
    try:
        muted_until = datetime.fromisoformat(r[0][0])
        return datetime.utcnow() < muted_until
    except Exception:
        return False

def give_vip(user_id: int, days: int):
    r = db_execute("SELECT vip_until FROM users WHERE user_id = ?", (user_id,), fetch=True)
    now = datetime.utcnow()
    if r and r[0][0]:
        try:
            current = datetime.fromisoformat(r[0][0])
        except Exception:
            current = now
        if current > now:
            new_until = current + timedelta(days=days)
        else:
            new_until = now + timedelta(days=days)
    else:
        new_until = now + timedelta(days=days)
    db_execute("UPDATE users SET vip_until = ? WHERE user_id = ?", (new_until.isoformat(), user_id))

def add_balance(user_id: int, amount: int):
    db_execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))

def get_profile_text(user_id: int):
    r = db_execute("SELECT username, display_name, about, reputation, balance, vip_until FROM users WHERE user_id = ?", (user_id,), fetch=True)
    if not r:
        return "Профиль не найден."
    username, display_name, about, reputation, balance, vip_until = r[0]
    vip_text = vip_until if vip_until else "Нет"
    return f"🔹 {display_name} (@{username})\n\n{about if about else '📝 Описание отсутствует'}\n\n⭐ Репутация: {reputation}\n💰 Баланс: {balance}\n👑 VIP до: {vip_text}"

# ============================
# === Bot & Dispatcher =======
# ============================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Inline keyboards
def main_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Найти собеседника", callback_data="find")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile"),
         InlineKeyboardButton(text="💰 Баланс / Донат", callback_data="balance")],
        [InlineKeyboardButton(text="🎮 Игры", callback_data="games")],
    ])
    return kb

def inchat_kb(can_reveal=True):
    buttons = [
        [InlineKeyboardButton(text="✋ Отключиться", callback_data="stop")],
        [InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data="complain")]
    ]
    if can_reveal:
        buttons[0:0] = [[InlineKeyboardButton(text="🔓 Раскрыть личность", callback_data="reveal")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ============================
# === Command handlers =======
# ============================
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    ensure_user(msg.from_user)
    if is_banned(msg.from_user.id):
        await msg.answer("Вы заблокированы.")
        return
    await msg.answer(
        "Привет! Это анонимный чат. Нажми кнопку чтобы найти собеседника.",
        reply_markup=main_kb()
    )

@dp.callback_query(Text("profile"))
async def cb_profile(query: types.CallbackQuery):
    ensure_user(query.from_user)
    text = get_profile_text(query.from_user.id)
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✏️ Редактировать анкету", callback_data="edit_profile")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
    ]))

@dp.callback_query(Text("back_main"))
async def cb_back(query: types.CallbackQuery):
    await query.message.edit_text("Главное меню", reply_markup=main_kb())

@dp.callback_query(Text("balance"))
async def cb_balance(query: types.CallbackQuery):
    ensure_user(query.from_user)
    r = db_execute("SELECT balance FROM users WHERE user_id = ?", (query.from_user.id,), fetch=True)
    bal = r[0][0] if r else 0
    text = f"💰 Ваш баланс: {bal}\nВы можете пополнить баланс кнопкой ниже."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Пополнить 50", callback_data="donate_50"),
         InlineKeyboardButton("Пополнить 100", callback_data="donate_100")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
    ])
    await query.message.edit_text(text, reply_markup=kb)

# Donation flow: create a local invoice and provide a deep link to @CryptoBot
@dp.callback_query(lambda c: c.data and c.data.startswith("donate_"))
async def cb_donate(query: types.CallbackQuery):
    ensure_user(query.from_user)
    amount = int(query.data.split("_")[1])
    invoice_id = secrets.token_hex(12)
    created = datetime.utcnow().isoformat()
    db_execute("INSERT INTO invoices (invoice_id, user_id, amount, created_at, paid) VALUES (?, ?, ?, ?, 0)",
               (invoice_id, query.from_user.id, amount, created))
    link = CRYPTO_DEEP_LINK_BASE + invoice_id
    text = f"Оплатите {amount} условных единиц через CryptoBot по ссылке ниже.\nПосле оплаты нажмите 'Проверить оплату'."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Оплатить через CryptoBot", url=link)],
        [InlineKeyboardButton("Проверить оплату", callback_data=f"checkpay_{invoice_id}")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
    ])
    await query.message.edit_text(text, reply_markup=kb)

# Check payment: For a real integration you must verify with CryptoBot API/webhook.
# Here we provide a simple manual "check" that the admin can mark as paid (or you can implement polling).
@dp.callback_query(lambda c: c.data and c.data.startswith("checkpay_"))
async def cb_checkpay(query: types.CallbackQuery):
    invoice_id = query.data.split("_", 1)[1]
    r = db_execute("SELECT paid, amount, user_id FROM invoices WHERE invoice_id = ?", (invoice_id,), fetch=True)
    if not r:
        await query.answer("Счёт не найден.", show_alert=True)
        return
    paid, amount, user_id = r[0]
    if paid:
        await query.message.edit_text(f"Счёт {invoice_id} уже оплачен. Пополнено {amount}.")
        return
    # Not paid — instruct user/admin how to mark as paid.
    if is_admin(query.from_user.id):
        # Admin can mark invoice paid
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("Отметить как оплачен", callback_data=f"markpaid_{invoice_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
        ])
        await query.message.edit_text(f"Счёт {invoice_id} не оплачен. Сумма: {amount}", reply_markup=kb)
    else:
        await query.answer("Счёт не оплачен. Попросите администратора подтвердить платеж.", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("markpaid_"))
async def cb_markpaid(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("Только администратор.", show_alert=True)
        return
    invoice_id = query.data.split("_", 1)[1]
    r = db_execute("SELECT paid, amount, user_id FROM invoices WHERE invoice_id = ?", (invoice_id,), fetch=True)
    if not r:
        await query.answer("Счёт не найден.", show_alert=True)
        return
    paid, amount, user_id = r[0]
    if paid:
        await query.answer("Уже оплачен.", show_alert=True)
        return
    db_execute("UPDATE invoices SET paid = 1 WHERE invoice_id = ?", (invoice_id,))
    add_balance(user_id, amount)
    await query.message.edit_text(f"Отмечено как оплаченное. Пользователю {user_id} начислено {amount}.")
    try:
        await bot.send_message(user_id, f"Ваш платёж на {amount} зачислен на баланс.")
    except Exception:
        pass

# ============================
# === Pairing & chat logic ===
# ============================
def queue_add(user_id: int):
    now = datetime.utcnow().isoformat()
    try:
        db_execute("INSERT INTO pairing (user_id, looking_since) VALUES (?, ?)", (user_id, now))
    except Exception:
        pass

def queue_remove(user_id: int):
    db_execute("DELETE FROM pairing WHERE user_id = ?", (user_id,))

def queue_find_pair(user_id: int):
    # naive: pick first other user in queue
    rows = db_execute("SELECT user_id FROM pairing WHERE user_id != ? ORDER BY looking_since LIMIT 1", (user_id,), fetch=True)
    if not rows:
        return None
    return rows[0][0]

def create_chat(user1: int, user2: int):
    db_execute("INSERT OR REPLACE INTO chats (user_id, peer_id) VALUES (?, ?)", (user1, user2))
    db_execute("INSERT OR REPLACE INTO chats (user_id, peer_id) VALUES (?, ?)", (user2, user1))

def end_chat(user_id: int):
    r = db_execute("SELECT peer_id FROM chats WHERE user_id = ?", (user_id,), fetch=True)
    if not r:
        return None
    peer = r[0][0]
    db_execute("DELETE FROM chats WHERE user_id = ?", (user_id,))
    db_execute("DELETE FROM chats WHERE user_id = ?", (peer,))
    return peer

def get_peer(user_id: int):
    r = db_execute("SELECT peer_id FROM chats WHERE user_id = ?", (user_id,), fetch=True)
    if not r:
        return None
    return r[0][0]

@dp.callback_query(Text("find"))
async def cb_find(query: types.CallbackQuery):
    uid = query.from_user.id
    ensure_user(query.from_user)
    if is_banned(uid):
        await query.answer("Вы заблокированы.", show_alert=True)
        return
    if is_muted(uid):
        await query.answer("Вам временно запрещено искать собеседников.", show_alert=True)
        return
    # if already in chat:
    if get_peer(uid):
        await query.answer("Вы уже в чате. Нажмите Отключиться.", show_alert=True)
        return
    queue_add(uid)
    pair = queue_find_pair(uid)
    if pair:
        # form chat
        queue_remove(uid)
        queue_remove(pair)
        create_chat(uid, pair)
        try:
            await bot.send_message(uid, "Собеседник найден! Можно общаться. Чтобы раскрыть личность или пожаловаться нажми кнопку.", reply_markup=inchat_kb())
        except Exception:
            pass
        try:
            await bot.send_message(pair, "Собеседник найден! Можно общаться. Чтобы раскрыть личность или пожаловаться нажми кнопку.", reply_markup=inchat_kb())
        except Exception:
            pass
    else:
        await query.message.edit_text("🔎 Ищем собеседника... Нажмите снова, если захотите отменить.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("Отменить поиск", callback_data="cancel_search")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
        ]))

@dp.callback_query(Text("cancel_search"))
async def cb_cancel_search(query: types.CallbackQuery):
    queue_remove(query.from_user.id)
    await query.message.edit_text("Поиск отменён.", reply_markup=main_kb())

@dp.callback_query(Text("stop"))
async def cb_stop(query: types.CallbackQuery):
    peer = end_chat(query.from_user.id)
    if peer:
        try:
            await bot.send_message(peer, "Собеседник отключился.", reply_markup=main_kb())
        except Exception:
            pass
    await query.message.edit_text("Вы отключены.", reply_markup=main_kb())

@dp.callback_query(Text("reveal"))
async def cb_reveal(query: types.CallbackQuery):
    uid = query.from_user.id
    peer = get_peer(uid)
    if not peer:
        await query.answer("Вы не в чате.", show_alert=True)
        return
    # fetch profile of uid and send to peer
    text = get_profile_text(uid)
    try:
        await bot.send_message(peer, f"Пользователь раскрыл личность:\n\n{text}")
        await query.answer("Анкета отправлена собеседнику.", show_alert=True)
    except Exception:
        await query.answer("Не удалось отправить.", show_alert=True)

@dp.callback_query(Text("complain"))
async def cb_complain(query: types.CallbackQuery):
    uid = query.from_user.id
    peer = get_peer(uid)
    if not peer:
        await query.answer("Вы не в чате.", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Оскорбления", callback_data=f"compl_{peer}_insult")],
        [InlineKeyboardButton("Спам / реклама", callback_data=f"compl_{peer}_spam")],
        [InlineKeyboardButton("Другое", callback_data=f"compl_{peer}_other")],
        [InlineKeyboardButton("◀️ Назад", callback_data="inchat_back")]
    ])
    await query.message.edit_text("Выберите причину жалобы:", reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("compl_"))
async def cb_compl_reason(query: types.CallbackQuery):
    parts = query.data.split("_", 2)
    target = int(parts[1])
    reason = parts[2] if len(parts) > 2 else "Не указано"
    db_execute("INSERT INTO complaints (complainer, target, reason, created_at) VALUES (?, ?, ?, ?)",
               (query.from_user.id, target, reason, datetime.utcnow().isoformat()))
    # auto-increase complaint count impacts reputation
    db_execute("UPDATE users SET reputation = reputation - 1 WHERE user_id = ?", (target,))
    # notify admins
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, f"Новая жалоба на {target} от {query.from_user.id}. Причина: {reason}")
        except Exception:
            pass
    await query.message.edit_text("Жалоба отправлена админам.", reply_markup=main_kb())

# ============================
# === Message routing ========
# ============================
@dp.message()
async def handle_messages(msg: types.Message):
    uid = msg.from_user.id
    ensure_user(msg.from_user)
    # if user is admin and sends commands in private chat, allow admin panel
    if msg.text and msg.text.startswith("/admin"):
        if not is_admin(uid):
            await msg.reply("Недостаточно прав.")
            return
    # if user is in a chat, forward message to peer (text and simple media)
    peer = get_peer(uid)
    if peer:
        if is_muted(uid):
            await msg.reply("Вы временно заблокированы и не можете отправлять сообщения.")
            return
        # forward text
        if msg.text:
            await bot.send_message(peer, msg.text)
        # forward stickers, photos, voice, etc. (basic)
        elif msg.sticker:
            await bot.send_sticker(peer, msg.sticker.file_id)
        elif msg.photo:
            await bot.send_photo(peer, msg.photo[-1].file_id, caption=msg.caption)
        elif msg.voice:
            await bot.send_voice(peer, msg.voice.file_id)
        elif msg.video:
            await bot.send_video(peer, msg.video.file_id, caption=msg.caption)
        else:
            await msg.reply("Этот тип сообщений пока не поддерживается.")
        return

    # if not in chat — interpret commands
    text = msg.text or ""
    if text.startswith("/profile_edit"):
        # quick inline edit: "/profile_edit меня зовут Вася|25|про меня"
        try:
            _, payload = text.split(" ", 1)
            parts = payload.split("|", 2)
            display = parts[0]
            about = parts[1] if len(parts) > 1 else ""
            db_execute("UPDATE users SET display_name = ?, about = ? WHERE user_id = ?", (display, about, uid))
            await msg.reply("Профиль обновлён.")
        except Exception:
            await msg.reply("Неправильный формат. Пример: /profile_edit Вася|Про меня")
        return

    if text.startswith("/start") and not peer:
        await msg.reply("Используйте кнопки меню.", reply_markup=main_kb())
        return

    # If user typed "игры" etc — show games menu
    if text.lower().startswith("игры") or text.lower().startswith("/games"):
        await msg.reply("Игры меню:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("Камень-Ножницы-Бумага (1v1)", callback_data="game_rps")],
            [InlineKeyboardButton("Угадай число (1v1)", callback_data="game_guess")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
        ]))
        return

    # Fallback
    await msg.reply("Чтобы начать — нажми 'Найти собеседника'.", reply_markup=main_kb())

# ============================
# === Mini-games (1v1) ======
# ============================
@dp.callback_query(Text("games"))
async def cb_games_main(query: types.CallbackQuery):
    await query.message.edit_text("Игры меню:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Камень-Ножницы-Бумага (1v1)", callback_data="game_rps")],
        [InlineKeyboardButton("Угадай число (1v1)", callback_data="game_guess")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_main")]
    ]))

@dp.callback_query(Text("game_rps"))
async def cb_game_rps(query: types.CallbackQuery):
    # join queue for RPS by reusing pairing table but with special marker
    await query.message.edit_text("Нажми 'Найти соперника' чтобы играть в RPS (ставка может быть добавлена).",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                      [InlineKeyboardButton("Найти соперника RPS", callback_data="find_rps")],
                                      [InlineKeyboardButton("◀️ Назад", callback_data="cb_games_main")]
                                  ]))

@dp.callback_query(Text("find_rps"))
async def cb_find_rps(query: types.CallbackQuery):
    uid = query.from_user.id
    # add to games queue by using games table with game_type='rps'
    try:
        db_execute("INSERT OR REPLACE INTO games (user_id, game_type, state, peer_id) VALUES (?, ?, ?, ?)",
                   (uid, "rps", "", None))
    except Exception:
        pass
    # find other rps player
    r = db_execute("SELECT user_id FROM games WHERE game_type = 'rps' AND user_id != ? LIMIT 1", (uid,), fetch=True)
    if r:
        peer = r[0][0]
        # pair them
        db_execute("UPDATE games SET peer_id = ? WHERE user_id = ?", (peer, uid))
        db_execute("UPDATE games SET peer_id = ? WHERE user_id = ?", (uid, peer))
        # initial state - waiting for moves
        db_execute("UPDATE games SET state = ? WHERE user_id IN (?, ?)", ("waiting", uid, peer))
        try:
            await bot.send_message(uid, "Соперник найден! Отправь: камень / ножницы / бумага")
            await bot.send_message(peer, "Соперник найден! Отправь: камень / ножницы / бумага")
        except Exception:
            pass
    else:
        await query.answer("Добавлено в очередь RPS. Подождите соперника.", show_alert=True)

@dp.callback_query(Text("game_guess"))
async def cb_game_guess(query: types.CallbackQuery):
    await query.message.edit_text("Найди соперника для 'Угадай число' (1-10).", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("Найти соперника Guess", callback_data="find_guess")],
        [InlineKeyboardButton("◀️ Назад", callback_data="cb_games_main")]
    ]))

@dp.callback_query(Text("find_guess"))
async def cb_find_guess(query: types.CallbackQuery):
    uid = query.from_user.id
    try:
        db_execute("INSERT OR REPLACE INTO games (user_id, game_type, state, peer_id) VALUES (?, ?, ?, ?)",
                   (uid, "guess", "", None))
    except Exception:
        pass
    r = db_execute("SELECT user_id FROM games WHERE game_type = 'guess' AND user_id != ? LIMIT 1", (uid,), fetch=True)
    if r:
        peer = r[0][0]
        db_execute("UPDATE games SET peer_id = ? WHERE user_id = ?", (peer, uid))
        db_execute("UPDATE games SET peer_id = ? WHERE user_id = ?", (uid, peer))
        secret = secrets.randbelow(10) + 1
        # store secret in state of one player (the setter)
        db_execute("UPDATE games SET state = ? WHERE user_id = ?", (str(secret), uid))
        db_execute("UPDATE games SET state = ? WHERE user_id = ?", ("guessing", peer))
        try:
            await bot.send_message(uid, f"Вы загадали число (секрет установлен). Соперник должен угадать.")
            await bot.send_message(peer, "Соперник загадал число от 1 до 10. Отправьте вашу догадку (число).")
        except Exception:
            pass
    else:
        await query.answer("Добавлено в очередь 'Guess'. Подождите соперника.", show_alert=True)

# handle messages for games moves
@dp.message()
async def handle_game_moves(msg: types.Message):
    uid = msg.from_user.id
    # check if user is in games table with peer
    r = db_execute("SELECT game_type, state, peer_id FROM games WHERE user_id = ?", (uid,), fetch=True)
    if not r:
        return  # not in any game here
    game_type, state, peer = r[0]
    text = (msg.text or "").lower().strip()
    if game_type == "rps":
        if state != "waiting":
            await msg.reply("Игра не готова.")
            return
        if text not in ("камень", "ножницы", "бумага"):
            await msg.reply("Отправь: камень / ножницы / бумага")
            return
        # store move in state column as JSON-like: {"move":"камень"}
        db_execute("UPDATE games SET state = ? WHERE user_id = ?", (text, uid))
        # check peer's move
        pr = db_execute("SELECT state FROM games WHERE user_id = ?", (peer,), fetch=True)
        if pr and pr[0][0] in ("камень", "ножницы", "бумага"):
            m1 = text
            m2 = pr[0][0]
            # determine winner
            if m1 == m2:
                res_text = "Ничья."
            elif (m1 == "камень" and m2 == "ножницы") or (m1 == "ножницы" and m2 == "бумага") or (m1 == "бумага" and m2 == "камень"):
                res_text = f"Победил {uid}"
                db_execute("UPDATE users SET reputation = reputation + 1 WHERE user_id = ?", (uid,))
            else:
                res_text = f"Победил {peer}"
                db_execute("UPDATE users SET reputation = reputation + 1 WHERE user_id = ?", (peer,))
            # cleanup
            db_execute("DELETE FROM games WHERE user_id IN (?, ?)", (uid, peer))
            await bot.send_message(uid, f"Результат: {res_text}")
            await bot.send_message(peer, f"Результат: {res_text}")
        else:
            await msg.reply("Ваш ход принят. Ожидаем ход соперника.")
        return

    if game_type == "guess":
        if state == "guessing":
            # this player should guess; peer has secret in their state
            try:
                guess = int(text)
            except Exception:
                await msg.reply("Отправьте число от 1 до 10.")
                return
            # find secret
            pr = db_execute("SELECT state FROM games WHERE user_id = ?", (peer,), fetch=True)
            if not pr:
                await msg.reply("Ошибка игры.")
                return
            secret = int(pr[0][0])
            if guess == secret:
                await bot.send_message(uid, "Вы угадали! Победа!")
                await bot.send_message(peer, "Вас угадали. Вы проиграли.")
                db_execute("UPDATE users SET reputation = reputation + 1 WHERE user_id = ?", (uid,))
            else:
                await bot.send_message(uid, "Не угадали. Попробуйте снова или завершите.")
                await bot.send_message(peer, f"Соперник попытался угадать: {guess}")
            # For simplicity, end game after guess (could be extended)
            db_execute("DELETE FROM games WHERE user_id IN (?, ?)", (uid, peer))
        else:
            await msg.reply("Ожидайте инструкций.")
        return

# ============================
# === Admin commands =========
# ============================
@dp.message(Command("admin_panel"))
async def cmd_admin_panel(msg: types.Message):
    if not is_admin(msg.from_user.id):
        await msg.reply("Нет доступа.")
        return
    users = db_execute("SELECT user_id, username, display_name, reputation, balance, vip_until, banned FROM users", fetch=True)
    text = "Пользователи:\n"
    for u in users:
        text += f"{u[0]} | @{u[1]} | {u[2]} | rep:{u[3]} | bal:{u[4]} | vip:{u[5]} | banned:{u[6]}\n"
    await msg.reply(text)

@dp.message(Command("ban"))
async def cmd_ban(msg: types.Message):
    if not is_admin(msg.from_user.id):
        await msg.reply("Нет доступа.")
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Использование: /ban <user_id>")
        return
    try:
        target = int(parts[1])
        db_execute("UPDATE users SET banned = 1 WHERE user_id = ?", (target,))
        await msg.reply("Пользователь заблокирован.")
    except Exception:
        await msg.reply("Ошибка.")

@dp.message(Command("unban"))
async def cmd_unban(msg: types.Message):
    if not is_admin(msg.from_user.id):
        await msg.reply("Нет доступа.")
        return
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.reply("Использование: /unban <user_id>")
        return
    try:
        target = int(parts[1])
        db_execute("UPDATE users SET banned = 0 WHERE user_id = ?", (target,))
        await msg.reply("Пользователь разбанен.")
    except Exception:
        await msg.reply("Ошибка.")

@dp.message(Command("mute"))
async def cmd_mute(msg: types.Message):
    if not is_admin(msg.from_user.id):
        await msg.reply("Нет доступа.")
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply("Использование: /mute <user_id> <minutes>")
        return
    try:
        target = int(parts[1])
        minutes = int(parts[2])
        until = (datetime.utcnow() + timedelta(minutes=minutes)).isoformat()
        db_execute("UPDATE users SET muted_until = ? WHERE user_id = ?", (until, target))
        await msg.reply("Пользователь замучен.")
    except Exception:
        await msg.reply("Ошибка.")

@dp.message(Command("add_balance"))
async def cmd_add_balance(msg: types.Message):
    if not is_admin(msg.from_user.id):
        await msg.reply("Нет доступа.")
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply("Использование: /add_balance <user_id> <amount>")
        return
    try:
        target = int(parts[1]); amount = int(parts[2])
        add_balance(target, amount)
        await msg.reply("Баланс обновлён.")
    except Exception:
        await msg.reply("Ошибка.")

@dp.message(Command("give_vip"))
async def cmd_give_vip(msg: types.Message):
    if not is_admin(msg.from_user.id):
        await msg.reply("Нет доступа.")
        return
    parts = (msg.text or "").split()
    if len(parts) < 3:
        await msg.reply("Использование: /give_vip <user_id> <days>")
        return
    try:
        target = int(parts[1]); days = int(parts[2])
        give_vip(target, days)
        await msg.reply("VIP выдан.")
    except Exception:
        await msg.reply("Ошибка.")

# ============================
# === Startup & main ========
# ============================
async def on_startup():
    init_db()
    logger.info("Bot starting... DB initialized.")

async def main():
    await on_startup()
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
