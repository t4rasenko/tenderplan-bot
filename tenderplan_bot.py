from __future__ import annotations

import logging
import os
from telegram import MenuButtonCommands
from telegram.request import HTTPXRequest
from telegram import BotCommand
import asyncio
import requests
from telegram.error import RetryAfter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes,
    ConversationHandler, filters,)
import time
from messages_exporter import export_messages
from Parser import generate_report 
from messages_exporter import format_tender_message, fetch_tender_detail
from config import BOT_TOKEN
from config import TOKEN
from database import get_connection
from datetime import datetime

# состояния разговора
ASK_EXISTING, ENTER_KEY, ASK_MORE, ADDING_KEY, DELETING_KEY = range(5)

API_URL = "https://tenderplan.ru/api"


# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Возвращает last_ts — метку времени последнего обновления для подписки пользователя по ключу
def get_last_ts(user_id: int, key: str) -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_ts FROM subscription_state WHERE tg_user_id = ? AND tender_key = ?",
            (user_id, key)
        )
        row = cursor.fetchone()
        return row[0] if row else 0


# Получение всех ключей пользователя из таблицы user_keys
def get_user_keys(user_id: int) -> list[tuple[str,str]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
       "SELECT tender_key, tender_name FROM user_keys WHERE tg_user_id=?", (user_id,)
        )
        return cursor.fetchall()


# Добавляет ключ пользователя в таблицу user_keys (если ещё не добавлен)
def add_user_key(user_id: int, tender_key: str, tender_name: str = ""):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
        "INSERT OR IGNORE INTO user_keys (tg_user_id, tender_key, tender_name) VALUES (?, ?, ?)",
        (user_id, tender_key, tender_name)
        )
        conn.commit()
   

# Устанавливает или обновляет активный ключ пользователя в таблице active_keys.
def set_active_key(user_id: int, key: str):
    logger.info(f"Сохраняю active_key={key} для user_id={user_id}")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """      
            INSERT INTO active_keys (tg_user_id, tender_key) VALUES (?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET tender_key=excluded.tender_key
            """, 
            (user_id, key))
        conn.commit()


# Возвращает активный tender_key для заданного пользователя по его user_id.
def get_active_key(user_id: int) -> str | None:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
        "SELECT tender_key FROM active_keys WHERE tg_user_id = ?",
        (user_id,)
        )
        row = cursor.fetchone()
    # присвоим в переменную, чтобы её можно было залогировать
    key = row[0] if row else None
    logger.info(f"Извлек active_key={key!r} для user_id={user_id}")
    return key


def was_tender_sent(user_id: int, tender_id: str) -> bool:
    """
    Проверяет, был ли уже отправлен данный тендер пользователю.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM sent_tenders WHERE tg_user_id = ? AND tender_id = ?",
            (user_id, tender_id)
        )
        return cur.fetchone() is not None


def mark_tender_as_sent(user_id: int, tender_id: str):
    """
    Отмечает тендер как отправленный пользователю,
    чтобы избежать повторной рассылки.
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_tenders (tg_user_id, tender_id) VALUES (?, ?)",
            (user_id, tender_id)
        )
        conn.commit()


# Обрабатывает завершение выбора ключа через callback_query.
async def finish_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("👌 Ключ установлен и готов к использованию! "
        "Теперь вы можете выгрузить тендеры командой /export"
    )
    return ConversationHandler.END


def update_subscription_state(user_id: int, key: str, last_ts: int):
    """
    Сохраняет или обновляет в базе данных метку времени last_ts для пары (user_id, key).
    Используется для фиксации последней обработанной даты публикации тендера
    в состоянии подписки пользователя.
    Если запись для пользователя и ключа существует — обновляет last_ts,
    иначе вставляет новую запись.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO subscription_state (tg_user_id, tender_key, last_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_user_id, tender_key) DO UPDATE
                SET last_ts=excluded.last_ts
            """, 
            (user_id, key, last_ts)
            )
        conn.commit()


def subscribe_user(user_id: int, key: str):
    """
    Регистрирует подписку пользователя на тендерный ключ.
    
    - Обновляет или добавляет запись в таблице subscription_state с текущей меткой времени (last_ts),
      чтобы отслеживать время последней обработки уведомлений по этому ключу.
    - Добавляет запись в таблицу subscriptions, если такой подписки ещё нет.
    """
    now_ms = int(time.time()*1000)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO subscription_state (tg_user_id, tender_key, last_ts)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_user_id, tender_key) DO UPDATE
                SET last_ts=excluded.last_ts
            """, 
            (user_id, key, now_ms)
            )
        cursor.execute(
            "INSERT OR IGNORE INTO subscriptions (tg_user_id, tender_key) VALUES (?, ?)",
            (user_id, key)
        )
        conn.commit()


def unsubscribe_user(user_id: int, key: str):
    """
    Удаляет подписку пользователя на заданный tender_key.
    Удаляет запись из таблицы subscriptions по user_id и ключу.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM subscriptions WHERE tg_user_id = ? AND tender_key = ?",
            (user_id, key))
        cursor.execute(
            "DELETE FROM subscription_state WHERE tg_user_id=? AND tender_key=?",
            (user_id, key)
        )
        conn.commit()


def get_subscriptions() -> list[tuple[int,str]]:
    """
    Возвращает список всех подписок из таблицы subscriptions.
    Каждая подписка представлена кортежем (tg_user_id, tender_key).
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tg_user_id, tender_key FROM subscriptions")
        rows = cursor.fetchall()
    return rows


def is_subscribed(user_id: int, key: str) -> bool:
    """
    Проверяет, подписан ли пользователь с user_id на заданный tender_key.
    Возвращает True, если подписка существует, иначе False.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM subscriptions WHERE tg_user_id = ? AND tender_key = ?",
            (user_id, key)
        )
        exists = cursor.fetchone() is not None
    return exists


# Возвращает словарь HTTP-заголовков с авторизацией для API-запросов.
def get_headers():
    return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}


# --- Команда /ключи — вывод сохранённых в context.user_data['my_keys'] ключей ---
async def keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_keys = get_user_keys(update.effective_user.id)
    if not user_keys:
        return await update.message.reply_text(
            "У вас ещё нет ни одного ключа. Запустите /start и добавьте."
        )
    # Основные кнопки для выбора ключей
    key_buttons = [
        [InlineKeyboardButton(f"🔑 {name or key[:12]}…", callback_data=f"select_key_{key}")]
        for key, name in user_keys
    ]
    # Кнопки управления
    manage_buttons = [
        [InlineKeyboardButton("➕ Добавить ключ", callback_data="change_key")],
        [InlineKeyboardButton("🗑 Удалить ключ", callback_data="delete_key")],
        [InlineKeyboardButton("↩️ Главное меню", callback_data="go_start")],
    ]
    # Выводим список ключей
    await update.message.reply_text(
        "Ваши ключи:\nВыберите активный ключ:",
        reply_markup=InlineKeyboardMarkup(key_buttons + manage_buttons)
    )
    return ASK_EXISTING
    
# Обрабатывает callback или команду выбора формата экспорта тендеров.
# Проверяет наличие ключей пользователя и активного ключа.
# Если активный ключ отсутствует, предлагает выбрать его.
# После выбора ключа предлагает выбрать формат выгрузки (Excel, сообщениями или отмена).
async def export_choice_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
    else:
        user_id = update.effective_user.id            # Обработка для случая, когда команда вызвана не через callback (например, через /export)
    
    user_keys = get_user_keys(user_id)
    if not user_keys:
        text = "У вас нет ключей для работы. Добавьте ключ через /start"
        if query:
            return await query.edit_message_text(text)
        else:
            return await update.message.reply_text(text)
    # Если несколько ключей — всегда просим выбрать
    if len(user_keys) > 1:
        buttons = []
        for key, name in user_keys:
            label = name or key
            buttons.append([InlineKeyboardButton(f"{label}", callback_data=f"select_key_{key}")])
        text = ("У вас несколько ключей. Выберите, по какому сделать поиск тендеров:")
        if query:
            return await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        else:
            return await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        
     # Если один ключ — используем его
    only_key, _ = user_keys[0]
    set_active_key(user_id, only_key)    
    # показываем кнопки выбора формата
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 В Excel", callback_data="export_excel")],
        [InlineKeyboardButton("💬 Сообщениями", callback_data="export_msgs")],
        [InlineKeyboardButton("❌ Отмена",        callback_data="cancel_export")]
    ])
    if query:
        await query.edit_message_text("Выберите формат выгрузки тендеров:", reply_markup=kb)
    else:
        await update.message.reply_text("Выберите формат выгрузки тендеров:", reply_markup=kb)


# --- Экспорт тендеров в сообщения ---
async def export_to_messages_cb(update, context):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    key_id  = get_active_key(user_id)
    msgs = export_messages(key_id)
    sent_count = 0
    for tid, text, atts in msgs:
        # собираем кнопку, если есть вложения
        kb = None
        if atts:
            # сохраняем в user_data для callback
            context.user_data[f"atts_{tid}"] = atts
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("📎 Документы", callback_data=f"show_atts:{tid}")
            ]])
         # Пытаемся отправить, ловим flood‑контроль
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb
            )
        except RetryAfter as e:
            # Telegram говорит подождать e.retry_after секунд
            await asyncio.sleep(e.retry_after)
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb
            )
        sent_count += 1
        # И небольшая пауза между сообщениями
        await asyncio.sleep(0.1)  # 100 мс
    # после всех — финальная клавиатура
    kb = [
        [InlineKeyboardButton("📊 В Excel",       callback_data="export_excel")],
        [InlineKeyboardButton("↩️ В начало",     callback_data="go_start")],
        [InlineKeyboardButton("🔑 Сменить ключ", callback_data="change_key")],
    ]
    # Если нет подписки — добавим кнопку подписки
    subscribed = is_subscribed(user_id, key_id)
    if not subscribed:
        kb.insert(0, [InlineKeyboardButton("🔔 Подписаться на новые", callback_data="subscribe")])
    await context.bot.send_message(
    chat_id=user_id,
    text=f"✅ Все тендеры отправлены в чат.\nОтправлено тендеров: {sent_count}\nЧто дальше?",
    reply_markup=InlineKeyboardMarkup(kb)
    )
    return ConversationHandler.END

# --- Подкрепление к сообщениям ссылок на документы ---
async def show_attachments_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, tid = q.data.split(":", 1)
    docs   = context.user_data.get(f"atts_{tid}", [])

    if not docs:
        return await context.bot.send_message(
            chat_id=q.message.chat_id,
            text="📎 Документов нет.",
            reply_to_message_id=q.message.message_id
        )

    lines = [
        f'— <a href="{a["href"]}">{a.get("displayName","Файл")}</a>'
        for a in docs
    ]
    text = "<b>📎 Документы:</b>\n" + "\n".join(lines)
    # Отправляем документы именно в ответ на сообщение с тендером
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_to_message_id=q.message.message_id
    )


# --- Отмена экспорта ---
async def cancel_export_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.delete_message()
    except:
        await query.edit_message_text("❌ Экспорт отменён.")
    return ConversationHandler.END


# --- Callback для кнопок select_0, select_1… сохраняет в context.user_data['active_key'] ---
async def select_key_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    # Достаём ключ из callback_data
    key = query.data.split("_", 2)[-1]

    # Проверка, что ключ есть у пользователя
    user_keys = [k for k, _ in get_user_keys(user_id)]
    if key not in user_keys:
        return await query.edit_message_text("❗ Ошибка: ключ не найден.")
    set_active_key(user_id, key)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 В Excel",    callback_data="export_excel")],
        [InlineKeyboardButton("💬 Сообщениями", callback_data="export_msgs")],
        [InlineKeyboardButton("❌ Отмена",       callback_data="cancel_export")],
    ])
    await query.edit_message_text(
        text=(
            f"🔑 Активный ключ установлен:\n`{key}`\n\n"
            "Выберите формат выгрузки тендеров:"
        ),
        parse_mode="Markdown",
        reply_markup=kb
    )
 


# --- Управление ключами ---
async def manage_keys_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
    "🛠 Управление ключами:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Добавить ключ", callback_data="has_existing")],
            [InlineKeyboardButton("🗑 Удалить ключ",        callback_data="delete_key")],
            [InlineKeyboardButton("↩️ Главное меню",       callback_data="go_start")],
        ])
    )
    return ASK_EXISTING


# --- СТАРТ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Собираем все ключи из БД
    user_keys = get_user_keys(user_id)
    if not user_keys:
        now_ms = int(time.time() * 1000)
        for rec in user_keys:
            key_id = rec[0]
            if not is_subscribed(user_id, key_id):
                update_subscription_state(user_id, key_id, now_ms)
        # 👉 Получаем имя пользователя
        user = update.effective_user
        full_name = user.full_name or "пользователь"        
        await update.message.reply_text(f"👋 Добро пожаловать, {full_name}!")
        await update.message.reply_text(
            "У вас ещё нет ключа — добавьте его из системы:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Добавить", callback_data="has_existing")],
            ])
        )
        return ASK_EXISTING
    # Получаем активный ключ
    active_key = get_active_key(user_id)
    # Если активного ключа нет, делаем активным первый из списка (например, если ключ один или просто не выбран)
    if not active_key and user_keys:
        first_key = user_keys[0][0]
        set_active_key(user_id, first_key)
        active_key = first_key
    # 🟡 Унифицированная логика и для одного, и для многих ключей
    buttons = []
    for key, name in user_keys:
        label = name or key
        is_active = (key == active_key)
        sub = is_subscribed(user_id, key)
        sub_label = "❌ Отписаться" if sub else "✅ Подписаться"
        sub_action = f"unsubscribe_{key}" if sub else f"subscribe_{key}"

        display_label = f"🔹 {label}" if is_active else label

        buttons.append([InlineKeyboardButton(display_label, callback_data=f"select_key_{key}")])
        buttons.append([
            InlineKeyboardButton(sub_label, callback_data=sub_action),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_key_{key}")
        ])

    buttons.append([InlineKeyboardButton("➕ Добавить ещё ключ", callback_data="has_existing")])
     # Показываем кнопку выгрузки, только если есть активный ключ
    if len(user_keys) == 1:
        buttons.append([InlineKeyboardButton("📤 Выгрузить тендеры", callback_data="choose_export_format")])

    await update.message.reply_text(
        "📌 Ваши ключи поиска тендеров:\n\n"
        "Нажмите на ключ, чтобы сделать его активным и получать по нему данные.\n"
        "✅ Подписаться — получать уведомления. ❌ — Отписаться.\n"
        "🗑 Удалить — убрать ключ.\n\n",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    return ASK_EXISTING


#Выбираем формат выгрузки тендеров
async def choose_export_format_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "Выберите способ получения тендеров:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Excel", callback_data="export_excel")],
            [InlineKeyboardButton("📩 Сообщениями", callback_data="export_msgs")],
            [InlineKeyboardButton("🔙 Назад", callback_data="go_start")]
        ])
    )


# Открываем список ключей для удаления
async def delete_key_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    user_keys = get_user_keys(q.from_user.id)
    buttons = []
    for key,name in user_keys:
        label = name or key
        buttons.append([InlineKeyboardButton(f"{key}. {label}", callback_data=f"del_{key}")])
    await q.edit_message_text(
        "🗑 Выберите ключ для удаления:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return DELETING_KEY


# Подтверждаем удаление ключей
async def delete_key_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает подтверждение удаления ключа пользователя.
    Получает индекс ключа из callback_data, находит соответствующий ключ,
    удаляет его из базы данных и уведомляет пользователя.
    После удаления возвращает в меню управления ключами.
    """
    q = update.callback_query; 
    await q.answer()
    key_id = q.data.split("_", 1)[1]  # получить ключ из callback_data
    user_id = q.from_user.id
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM user_keys WHERE tg_user_id=? AND tender_key=?",
            (user_id, key_id)
        )
        # ✅ Дополнительно чистим все связанные данные
        cursor.execute(
            "DELETE FROM subscriptions WHERE tg_user_id=? AND tender_key=?",
            (user_id, key_id)
        )
        cursor.execute(
            "DELETE FROM subscription_state WHERE tg_user_id=? AND tender_key=?",
            (user_id, key_id)
        )
        conn.commit()
    await q.edit_message_text(f"✅ Ключ *{key_id}* удалён.", parse_mode="Markdown")
    return await manage_keys_cb(update, context)


# --- Ввод ключа пользователем, поиск будет по ИД или имени ---
async def ask_existing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "has_existing":
        await q.edit_message_text("Введите полное название ключа *или* его ID:")
        return ENTER_KEY


# --- Поиск введенного ключа пользователем в системе Tenderplan ---
async def enter_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    # пытаемся найти ID по имени среди ключей в TenderPlan
    try:
        resp = requests.get(f"{API_URL}/keys/getall", headers=get_headers())
        resp.raise_for_status()
        data = resp.json()
        remote_keys = data if isinstance(data, list) else data.get("keys", []) or data.get("data", [])
        match = [k for k in remote_keys if k["name"].lower() == text.lower()]
        if len(match) == 1:
            key_id = match[0]["_id"]
            tender_name= match[0]["name"]
        else:
            key_id = text
            tender_name= ""
    except Exception as e:
        logger.exception("Не удалось получить список ключей с сервера")
        key_id = text
        tender_name = ""
    try:     
        add_user_key(user_id, key_id, tender_name)
    except Exception as e:
        logger.exception("Ошибка при добавлении ключа в базу")
        await update.message.reply_text("❗️Не удалось добавить ключ. Возможно, он уже добавлен.")
        return ASK_MORE
    context.user_data.setdefault("added_keys", []).append(key_id)
    # подтверждаем
    kb = [
        [
            InlineKeyboardButton("Да, ещё есть", callback_data="more_yes"),
            InlineKeyboardButton("Нет, достаточно", callback_data="more_no"),
        ]
    ]
    await update.message.reply_text(
        f"✅ Ключ `{key_id}` добавлен.\nХотите добавить ещё?",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ASK_MORE


# --- Спрашиваем ещё ключи, добавляем ещё при необходимости---
async def ask_more(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # Пользователь выбрал «Да, ещё есть» — снова входим в ENTER_KEY
    if q.data == "more_yes":
        await q.edit_message_text("Введите следующий ключ (имя или ID):")
        return ENTER_KEY
    # Иначе — «Нет, достаточно» → выбираем активный
    user_id = q.from_user.id
    user_keys = get_user_keys(user_id)
    # Если нет ключей (на всякий случай)
    if not user_keys:
        await q.edit_message_text(
            "У вас пока нет ни одного ключа — вернёмся в начало."
        )
        return await start(update, context)
    # Если ровно один — сразу назначаем и даём подсказку /export
    if len(user_keys) == 1:
        only_key, _ = user_keys[0]
        set_active_key(user_id, only_key)
        await q.edit_message_text(
            f"🔑 Активный ключ: `{only_key}`\n\n"
            "Теперь вы можете выгрузить тендеры командой /export",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    # Если ключей больше одного — показываем кнопки выбора
    buttons = [
    [InlineKeyboardButton(f"{idx+1}. {name or key}", callback_data=f"select_key_{key}")]
    for idx, (key, name) in enumerate(user_keys)
    ]
    await q.edit_message_text(
    "У вас несколько ключей — выберите активный:",
    reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ASK_EXISTING
    

# --- Команда помощь ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — Перезапустить бота, начать с чистого листа\n"
        "/keys — Просмотреть список ваших ключей, добавить новый или выбрать активный\n"
        "/export — Выгрузить список тендеров по активному ключу в удобном формате\n"
        "/subscriptions — Показать на какие ключи вы подписаны для уведомлений о новых тендерах\n"
        "/help — Показать это сообщение с описанием команд\n")


# --- Команда экспорта тендеров=экспорт в excel ---
async def export_tenders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        message = update.callback_query.message
    else:
        message = update.message
    user_id = message.chat.id
    key_id = get_active_key(user_id)
    if not key_id:
        return await message.reply_text(
            "У вас не выбран активный ключ. Выберите его командой /keys или добавьте новый."
        )

    # ————— Скачиваем превью и берём max(publicationDate) —————
    resp = requests.get(
        f"{API_URL}/tenders/getlist",
        params={'key': key_id, 'page': 0, 'size': 1000, 'status': 1},
        headers=get_headers(),
        verify=False
    )
    resp.raise_for_status()
    previews = resp.json().get('tenders', [])
    #timestamps = [t.get('publicationDate', 0) for t in previews]
    #max_pub = max(timestamps) if timestamps else int(time.time() * 1000)
    #update_subscription_state(user_id, key_id, max_pub)

    # ————— Генерируем отчёт —————
    notice = await message.reply_text("Генерирую отчёт…⏳")
    try:
        result = generate_report(key_id)
        report_path = result[0] if isinstance(result, tuple) else result
    except Exception as e:
        await message.reply_text(f"❌ Не удалось создать отчёт: {e}")
        return ConversationHandler.END 
    await notice.delete()
    try:
        with open(report_path, "rb") as doc:
            await context.bot.send_document(
                chat_id=user_id,
                document=doc,
                filename=os.path.basename(report_path)
            )
    except Exception as e:
        logger.exception("Ошибка при отправке отчёта")
    finally:
        if os.path.exists(report_path):
            os.remove(report_path)

    # ————— Предлагаем следующие действия —————
    subscribed = is_subscribed(user_id, key_id)
    buttons = []
    if subscribed:
        buttons.append([InlineKeyboardButton("❌ Отписаться от уведомлений", callback_data=f"unsubscribe_{key_id}")])
    else:
        buttons.append([InlineKeyboardButton("🔔 Подписаться на новые тендеры", callback_data=f"subscribe_{key_id}")])

    buttons.append([InlineKeyboardButton("🔑 Выбрать другой ключ", callback_data="change_key")])
    buttons.append([InlineKeyboardButton("↩️ В начало", callback_data="go_start")])

    await message.reply_text(
        "✅ Отчёт готов и отправлен!\n\nЧто будем делать дальше?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ConversationHandler.END

# --- Команда подписки ---
async def subscribe_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id    
    key_id = q.data.split("_", 1)[1]

    subscribe_user(user_id, key_id)

    # Обновляем меню с актуальным состоянием подписок
    await update_keys_menu(q, user_id)
    await q.message.reply_text(
        f"✅ Вы подписались на новые тендеры по ключу `{key_id}`.\n\n"
        "🔄 Бот будет проверять новые тендеры каждые 30 минут и присылать уведомления.",
        parse_mode="Markdown"
    )
  


   # --- Команда отписки ---
async def unsubscribe_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    key_id = q.data.split("_", 1)[1]

    unsubscribe_user(user_id, key_id)

    # Обновляем меню с актуальным состоянием подписок
    await update_keys_menu(q, user_id)

    await q.message.reply_text(
        f"❌ Вы отписались от уведомлений по ключу `{key_id}`.\n\n"
        "🔕 Новые тендеры по этому ключу приходить не будут.",
        parse_mode="Markdown"
    )


#Универсальная функция для обновления меню
async def update_keys_menu(q, user_id: int):
    user_keys = get_user_keys(user_id)
    buttons = []
    for i, (key, name) in enumerate(user_keys):
        label = name or key
        sub = is_subscribed(user_id, key)
        sub_label = "❌ Отписаться" if sub else "✅ Подписаться"
        sub_action = f"unsubscribe_{key}" if sub else f"subscribe_{key}"
        buttons.append([InlineKeyboardButton(f"{i+1}. {label}", callback_data=f"select_key_{key}")])
        buttons.append([
            InlineKeyboardButton(sub_label, callback_data=sub_action),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_key_{key}")
        ])

    buttons.append([InlineKeyboardButton("Добавить ещё ключ", callback_data="has_existing")])

    await q.edit_message_text(
        "📌 Ваши ключи поиска тендеров:\n\n"
        "Выберите ключ, чтобы посмотреть актуальные тендеры.\n"
        "✅ Подписка — получать новые тендеры. ❌ — Отписаться.\n"
        "Удалить — полностью убрать ключ.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# Обрабатывает callback-запрос на смену активного ключа.
async def change_key_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_keys = get_user_keys(q.from_user.id)
    buttons = [[InlineKeyboardButton(f"{i+1}. {name or key}", callback_data=f"select_key_{key}")]
            for i, (key, name) in enumerate(user_keys)]
    await q.edit_message_text(
        "🔑 Выберите активный ключ:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# --- Callback "В начало" — полный аналог /start, но по кнопке
async def go_start_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id

    user_keys = get_user_keys(user_id)
    if not user_keys:
        full_name = q.from_user.full_name or "пользователь"
        await q.edit_message_text(f"👋 Добро пожаловать, {full_name}!")
        await context.bot.send_message(
            chat_id=user_id,
            text="У вас ещё нет ключа — добавьте его из системы:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Добавить", callback_data="has_existing")]
            ])
        )
        return ASK_EXISTING
    # Получаем активный ключ
    active_key = get_active_key(user_id)

    # Если активный ключ не установлен, назначим первый
    if not active_key and user_keys:
        first_key = user_keys[0][0]
        set_active_key(user_id, first_key)
        active_key = first_key

    buttons = []
    for key, name in user_keys:
        label = name or key
        is_active = (key == active_key)
        sub = is_subscribed(user_id, key)
        sub_label = "❌ Отписаться" if sub else "✅ Подписаться"
        sub_action = f"unsubscribe_{key}" if sub else f"subscribe_{key}"

        display_label = f"🔹 {label}" if is_active else label

        buttons.append([InlineKeyboardButton(display_label, callback_data=f"select_key_{key}")])
        buttons.append([
            InlineKeyboardButton(sub_label, callback_data=sub_action),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_key_{key}")
        ])

    buttons.append([InlineKeyboardButton("➕ Добавить ещё ключ", callback_data="has_existing")])

    # Кнопка выгрузки — если есть активный ключ
    if len(user_keys) == 1:
        buttons.append([InlineKeyboardButton("📤 Выгрузить тендеры", callback_data="choose_export_format")])

    await q.edit_message_text(
        "📌 Ваши ключи поиска тендеров:\n\n"
        "Нажмите на ключ, чтобы сделать его активным и получать по нему данные.\n"
        "✅ Подписаться — получать уведомления. ❌ — Отписаться.\n"
        "🗑 Удалить — убрать ключ.\n\n",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

    return ASK_EXISTING
    

# --- Команда проверки и отправки новых тендеров по подписке ---
async def check_new_tenders(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    now_ts = int(datetime.now().timestamp() * 1000)  # текущее время в мс
    for user_id, key in get_subscriptions():
        last_ts = get_last_ts(user_id, key)
        print(f"Проверяем ключ {key} для пользователя {user_id}, last_ts={last_ts}")
        all_new_tenders = []
        page = 0
        size = 50
        while True:
            try:
                resp = requests.get(
                    f"{API_URL}/tenders/v2/getlist",
                    params={
                        'type': 0,
                        'id': key,
                        'statuses': [1],
                        'page': page,
                        'size': size,
                        'fromPublicationDateTime': last_ts,
                        'publicationDateTime': -1
                    },
                    headers=get_headers(),
                    verify=False
                )
                resp.raise_for_status()
                batch = resp.json().get('tenders', [])
                print(f"[INFO] Ключ {key}, страница {page}, всего тендеров в batch: {len(batch)}")
                #Оставляем только актуальные (ещё не закончены)
                new_items = [
                    t for t in batch
                    if (t.get("submissionCloseDateTime") or t.get("submissionCloseDate") or 0) > now_ts
                ]
                if not new_items and len(batch) < size:
                    print(f"[INFO] Нет новых тендеров и страницы закончились, выходим.")
                    # Если новых нет и дальше страницы закончились — выходим
                    break

                all_new_tenders.extend(new_items)
                # Если пришло меньше, чем size — значит последняя страница
                if len(batch) < size:
                    print(f"[INFO] Последняя страница получена.")
                    break
                page += 1
            except Exception as e:
                print(f"[!] Ошибка при загрузке тендеров по ключу {key}, страница {page}: {e}")
                break
        if not all_new_tenders:
            print(f"[INFO] Для ключа {key} новых тендеров нет.")
            continue
        print(f"Новых тендеров всего: {len(all_new_tenders)}")
        for preview in all_new_tenders:
            try:
                tid = preview.get('_id')
                if was_tender_sent(user_id, tid):
                    print(f"[SKIP] Тендер {tid} уже был отправлен пользователю {user_id}, пропускаем.")
                    continue
                detail = fetch_tender_detail(preview)
                key_name = get_key_name(user_id, key)
                text = f"🔑 Подписка по ключу: <b>{key_name}</b>\n\n" + format_tender_message(detail)
                atts = detail.get('attachments', [])
                tid = detail.get('_id', '')
                if atts:
                    save_attachments(tid, atts)
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📎 Документы", callback_data=f"show_sub_atts:{tid}")]])
                else:
                    kb = None
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=kb
                    )
                except RetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                    await bot.send_message(chat_id=user_id,text=text,
                                            parse_mode="HTML",
                                            disable_web_page_preview=True,
                                            reply_markup=kb)
                # ✅ Отмечаем тендер как отправленный
                mark_tender_as_sent(user_id, tid)
                await asyncio.sleep(0.1)
            except Exception as e:
                print(f"[!] Ошибка при обработке тендера: {e}")
                continue

                # Обновляем границу только если есть новые тендеры
        filtered = [t for t in all_new_tenders if isinstance(t, dict)]
        if filtered:
            new_max = max(t.get('publicationDateTime', 0) for t in filtered)
            if new_max > last_ts:
                print(f"Обновляем last_ts с {last_ts} на {new_max} для ключа {key}")
                update_subscription_state(user_id, key, new_max)
            else:
                print(f"[DEBUG] new_max ({new_max}) <= last_ts ({last_ts}) — не обновляем.")
        else:
            print(f"Нет новых тендеров для ключа {key}, last_ts не обновляем.")


def save_attachments(tender_id: str, attachments: list[dict]):
    """
    Сохраняет список вложений (документов) для конкретного тендера в таблицу attachments.

    - tender_id: ID тендера.
    - attachments: [{"displayName": "Документ.pdf", "href": "https://..."}]
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        for att in attachments:
            file_name = att.get("displayName") or att.get("fileName") or "Файл"
            url = att.get("href") or att.get("url")
            if url:  # сохраняем только если есть ссылка
                cursor.execute("""
                    INSERT OR IGNORE INTO attachments (tender_id, file_name, url)
                    VALUES (?, ?, ?)
                """, (tender_id, file_name, url))
        conn.commit()



def get_attachments(tender_id: str) -> list[tuple[str, str]]:
    """
    Возвращает список вложений (имя файла и URL) для указанного тендера.

    - tender_id: ID тендера.
    - Возвращает список кортежей (file_name, url).
    - Если документов нет, возвращает пустой список.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT file_name, url FROM attachments WHERE tender_id=?", (tender_id,))
        return cursor.fetchall()

#Обработчик кнопки "Документы"
async def show_attachments_sub_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, tid = q.data.split(":", 1)

    docs = get_attachments(tid)  # [(file_name, url), ...]

    if not docs:
        return await context.bot.send_message(
            chat_id=q.message.chat_id,
            text="📎 Документов нет.",
            reply_to_message_id=q.message.message_id
        )

    lines = [f'— <a href="{url}">{name}</a>' for name, url in docs]
    text = "<b>📎 Документы:</b>\n" + "\n".join(lines)

    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_to_message_id=q.message.message_id
    )



def get_key_name(user_id: int, key: str) -> str:
    """
    Возвращает имя ключа (tender_name) для заданного пользователя и ключа.
    Если имя отсутствует или не найдено — возвращает сам ключ как fallback.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT tender_name FROM user_keys WHERE tg_user_id = ? AND tender_key = ?",
            (user_id, key)
        )
        row = cursor.fetchone()
    return row[0] if row and row[0] else key


# Обработчик ошибок: логирует исключения, возникшие при обработке обновлений Telegram.
async def error_handler(update, context):
    logger.error("Exception while handling an update:", exc_info=context.error)


async def show_user_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Асинхронно выводит список активных подписок пользователя.
    Делает JOIN между таблицами subscriptions и user_keys,
    чтобы дополнительно отобразить имя ключа (если оно задано).
    Если подписок нет — отправляет соответствующее уведомление.
    """
    user_id = update.effective_user.id
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.tender_key, k.tender_name 
            FROM subscriptions s
            LEFT JOIN user_keys k ON s.tg_user_id = k.tg_user_id AND s.tender_key = k.tender_key
            WHERE s.tg_user_id = ?
            """, 
            (user_id,)
            )
        rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("🔕 У вас нет активных подписок.")
        return
    # Формируем сообщение со списком подписок
    lines = []
    for key, name in rows:
        name_part = f" — {name}" if name else ""
        lines.append(f"🔔 `{key}`{name_part}")
    text = "📬 *Ваши подписки на ключи:*\n\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="Markdown")


async def refresh_keys_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Загружает список ключей пользователя из системы TenderPlan через API.
    Обновляет имена существующих ключей в локальной БД и выводит актуальный список
    для выбора активного ключа.
    Пользователь может использовать только те ключи, которые уже были созданы на сайте.
    """
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id

    # 1) Получаем сводный список ключей из TenderPlan
    try:
        resp = requests.get(f"{API_URL}/keys/getall", headers=get_headers())
        resp.raise_for_status()
        payload = resp.json()
        remote = payload if isinstance(payload, list) else payload.get("keys", []) or payload.get("data", [])
    except Exception as e:
        logger.exception("Не удалось скачать ключи из TenderPlan")
        return await q.edit_message_text(f"❌ Ошибка при получении ключей: {e}")

    # 2) Перезаписываем список ключей в локальной БД
    existing = {key: name for key, name in get_user_keys(user_id)}
    for k in remote:
        key_id   = k.get("_id") or k.get("id")
        key_name = k.get("name", "")
        # если такой ключ уже добавлен руками — обновляем имя
        if key_id in existing and key_name and key_name != existing[key_id]:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE user_keys SET tender_name = ? WHERE tg_user_id = ? AND tender_key = ?",
                    (key_name, user_id, key_id)
                )
                conn.commit()
    # 3) Собираем уже обновлённый список из БД
    user_keys = get_user_keys(user_id)
    buttons = []
    for i, rec in enumerate(user_keys):
        key_id, name = rec if isinstance(rec, tuple) else (rec, "")
        buttons.append([InlineKeyboardButton(f"{i+1}. {name or key_id[:8]}…", callback_data=f"select_key_{key_id}")])
    buttons.append([InlineKeyboardButton("🔄 Обновить список ключей", callback_data="refresh_keys")])

    await q.edit_message_text(
        "🔑 Ваши сохранённые ключи (обновлённый список):",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ASK_EXISTING


if __name__ == '__main__':
    request = HTTPXRequest(
    connection_pool_size=50,
    pool_timeout=10.0            
    )
    app = (ApplicationBuilder().token(BOT_TOKEN).request(request).build())

    # ОЧИЩАЕМ ВСЕ КОМАНДЫ ОДИН РАЗ
    asyncio.get_event_loop().run_until_complete(app.bot.set_my_commands([]))

    # <-- регистрируем команды
    asyncio.get_event_loop().run_until_complete(
        app.bot.set_my_commands([
            BotCommand("start",  "Запустить/Перезапустить бота"),
            BotCommand("export", "Выгрузить тендеры по активному ключу"),
            BotCommand("keys",   "Управление ключами"),
            BotCommand("subscriptions", "Мои подписки"),
            BotCommand("help",   "Показать справку по командам"),
        ])
    )
    # Показываем команды в выпадающем меню
    asyncio.get_event_loop().run_until_complete(
        app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start),
                      CommandHandler("keys",  keys_command),
                      CommandHandler("export", export_choice_cb),
                      CallbackQueryHandler(ask_existing, pattern="^has_existing$"),
                      ],
        states={
            ASK_EXISTING: [
            CommandHandler("start", start),  # <— теперь /start в любой момент зацепится
            CommandHandler("keys",  keys_command),
            CommandHandler("export", export_choice_cb),
            CallbackQueryHandler(ask_existing,    pattern="^(has_existing|no_existing)$"),
            CallbackQueryHandler(refresh_keys_cb, pattern="^refresh_keys$"),
            CallbackQueryHandler(export_to_messages_cb, pattern="^export_msgs$"),
            CallbackQueryHandler(export_tenders,pattern="^export_excel$"),
            CallbackQueryHandler(cancel_export_cb,      pattern="^cancel_export$"),
            CallbackQueryHandler(select_key_cb,   pattern=r"^select(_key)?_\d+$"),
            # ✅ Удаление
            CallbackQueryHandler(delete_key_cb, pattern=r"^delete_key_.+$"),

            CallbackQueryHandler(change_key_cb,     pattern="^change_key$"),
            CallbackQueryHandler(go_start_cb,   pattern="^go_start$"),
            CallbackQueryHandler(finish_cb,       pattern="^finish$")
        ],
        ENTER_KEY: [ MessageHandler(filters.TEXT & ~filters.COMMAND, enter_key) ],
        ASK_MORE:  [ CallbackQueryHandler(ask_more, pattern="^more_") ],
        DELETING_KEY: [ CallbackQueryHandler(delete_key_confirm_cb, pattern=r"^del_.+$") ],
        
    },
    fallbacks=[CommandHandler("help", help_command)],
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(manage_keys_cb,    pattern="^manage_keys$"))
    app.add_handler(CommandHandler("keys",  keys_command))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("subscriptions", show_user_subscriptions))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("export", export_choice_cb))
    app.add_handler(CallbackQueryHandler(subscribe_cb, pattern=r"^subscribe_.+$"))
    app.add_handler(CallbackQueryHandler(unsubscribe_cb, pattern=r"^unsubscribe_.+$"))
    app.add_handler(CallbackQueryHandler(change_key_cb, pattern="^change_key$"))
    app.add_handler(CallbackQueryHandler(refresh_keys_cb, pattern="^refresh_keys$"))
   # ✅ Универсальный паттерн для выбора ключа (и для /start, и для /export):
    app.add_handler(CallbackQueryHandler(select_key_cb, pattern=r"^select(_key)?_.+$"))
    app.add_handler(CallbackQueryHandler(ask_existing, pattern="^has_existing$"))
    app.add_handler(CallbackQueryHandler(export_to_messages_cb, pattern="^export_msgs$"))
    app.add_handler(CallbackQueryHandler(show_attachments_cb, pattern=r"^show_atts:"))
    app.add_handler(CallbackQueryHandler(go_start_cb, pattern="^go_start$"))
    app.add_handler(CallbackQueryHandler(export_tenders,pattern="^export_excel$"))
    app.add_handler(CallbackQueryHandler(cancel_export_cb,      pattern="^cancel_export$"))
    app.add_handler(CallbackQueryHandler(cancel_export_cb, pattern="^cancel_export$", block=False))
    app.add_handler(CallbackQueryHandler(delete_key_cb, pattern=r"^delete_key_.+$"))    # первый шаг
    app.add_handler(CallbackQueryHandler(delete_key_confirm_cb, pattern=r"^del_.+$")) # подтверждение
    app.add_handler(CallbackQueryHandler(show_attachments_sub_cb, pattern=r"^show_sub_atts:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, enter_key))

    app.add_handler(CallbackQueryHandler(choose_export_format_cb, pattern="^choose_export_format$"))

    app.add_error_handler(error_handler)
    # запустим job каждые 30 минут
    app.job_queue.run_repeating(check_new_tenders, interval=1800, first=10)
    app.run_polling()