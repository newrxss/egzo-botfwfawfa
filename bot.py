import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice,
    PreCheckoutQuery, ContentType,
    InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = 8941907250
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "egzo_bot.sqlite3")

TARIFFS = {
    "7":   {"days": 7,   "price": 69,   "title": "7 дней"},
    "14":  {"days": 14,  "price": 138,  "title": "14 дней"},
    "30":  {"days": 30,  "price": 250,  "title": "30 дней"},
    "90":  {"days": 90,  "price": 650,  "title": "90 дней"},
    "365": {"days": 365, "price": 1990, "title": "365 дней"},
}
# ===============================================

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class BlockedUserMiddleware:
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and is_blocked(user.id) and not is_admin(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Доступ к боту заблокирован.", show_alert=True)
            elif isinstance(event, Message):
                language = get_language(user.id)
                text = ("🚫 <b>Bot access is blocked.</b>\n\nIf this is a mistake, contact support." if language == "en" else
                        "🚫 <b>Доступ к боту заблокирован.</b>\n\nЕсли это ошибка — обратись в поддержку.")
                await event.answer(text, parse_mode="HTML")
            return
        return await handler(event, data)


dp.message.outer_middleware(BlockedUserMiddleware())
dp.callback_query.outer_middleware(BlockedUserMiddleware())


def db_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def get_language(user_id: int) -> str:
    with db_connection() as db:
        row = db.execute("SELECT language FROM user_languages WHERE user_id = ?", (user_id,)).fetchone()
    return row["language"] if row else "ru"


def set_language(user_id: int, language: str):
    with db_connection() as db:
        db.execute(
            "INSERT INTO user_languages (user_id, language) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET language = excluded.language",
            (user_id, language),
        )


def language_menu():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
        InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
    )
    return builder.as_markup()


def lang_text(language: str, ru: str, en: str) -> str:
    return en if language == "en" else ru


def init_database():
    with db_connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS license_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tariff TEXT NOT NULL,
                license_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'available',
                created_at TEXT NOT NULL,
                sold_at TEXT,
                buyer_id INTEGER,
                purchase_charge_id TEXT
            );

            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_charge_id TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                username TEXT,
                tariff TEXT NOT NULL,
                license_key TEXT,
                stars INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_license_keys_available
            ON license_keys (tariff, status, id);
            CREATE INDEX IF NOT EXISTS idx_purchases_user
            ON purchases (user_id, id DESC);

            CREATE TABLE IF NOT EXISTS funpay_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                tariff TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_funpay_orders_status
            ON funpay_orders (status, id);

            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                blocked_at TEXT NOT NULL,
                blocked_by INTEGER
            );

            CREATE TABLE IF NOT EXISTS user_languages (
                user_id INTEGER PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'ru'
            );
        """)


def available_key_counts():
    counts = {tariff: 0 for tariff in TARIFFS}
    with db_connection() as db:
        rows = db.execute(
            "SELECT tariff, COUNT(*) AS amount FROM license_keys "
            "WHERE status = 'available' GROUP BY tariff"
        ).fetchall()
    for row in rows:
        counts[row["tariff"]] = row["amount"]
    return counts


def add_keys_to_database(tariff: str, keys: list[str]) -> tuple[int, int]:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    added = duplicates = 0
    with db_connection() as db:
        for key in keys:
            key = key.strip()
            if not key:
                continue
            try:
                db.execute(
                    "INSERT INTO license_keys (tariff, license_key, created_at) VALUES (?, ?, ?)",
                    (tariff, key, now),
                )
                added += 1
            except sqlite3.IntegrityError:
                duplicates += 1
    return added, duplicates


def issue_key_for_payment(tariff: str, user_id: int, username: str | None,
                          charge_id: str, stars: int) -> tuple[str, bool]:
    """Returns a key/message and whether this payment was already processed."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT license_key FROM purchases WHERE telegram_charge_id = ?", (charge_id,)
        ).fetchone()
        if existing:
            return existing["license_key"] or "Ключи закончились. Напиши в поддержку.", True

        row = db.execute(
            "SELECT id, license_key FROM license_keys "
            "WHERE tariff = ? AND status = 'available' ORDER BY id LIMIT 1", (tariff,)
        ).fetchone()
        key = row["license_key"] if row else None
        if row:
            db.execute(
                "UPDATE license_keys SET status = 'sold', sold_at = ?, buyer_id = ?, "
                "purchase_charge_id = ? WHERE id = ?",
                (now, user_id, charge_id, row["id"]),
            )
        db.execute(
            "INSERT INTO purchases (telegram_charge_id, user_id, username, tariff, license_key, stars, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (charge_id, user_id, username, tariff, key, stars, now),
        )
    return key or "Ключи закончились. Напиши в поддержку — выдадим вручную.", False


def purchases_for_user(user_id: int):
    with db_connection() as db:
        return db.execute(
            "SELECT tariff, license_key, stars, created_at FROM purchases "
            "WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()


def remove_available_keys(keys: list[str]) -> tuple[int, int]:
    removed = 0
    with db_connection() as db:
        for key in keys:
            result = db.execute(
                "DELETE FROM license_keys WHERE license_key = ? AND status = 'available'",
                (key.strip(),),
            )
            removed += result.rowcount
    return removed, len(keys) - removed


def issue_manual_key(tariff: str, user_id: int, username: str | None,
                     source: str) -> str | None:
    """Takes one available key and stores the issuance in the database."""
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    charge_id = f"{source}:{datetime.now().timestamp()}:{user_id}"
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT id, license_key FROM license_keys WHERE tariff = ? "
            "AND status = 'available' ORDER BY id LIMIT 1", (tariff,)
        ).fetchone()
        if not row:
            return None
        db.execute(
            "UPDATE license_keys SET status = 'sold', sold_at = ?, buyer_id = ?, "
            "purchase_charge_id = ? WHERE id = ?",
            (now, user_id, charge_id, row["id"]),
        )
        db.execute(
            "INSERT INTO purchases (telegram_charge_id, user_id, username, tariff, license_key, stars, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (charge_id, user_id, username, tariff, row["license_key"], 0, now),
        )
    return row["license_key"]


def create_funpay_order(user_id: int, username: str | None, tariff: str) -> int:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with db_connection() as db:
        existing = db.execute(
            "SELECT id FROM funpay_orders WHERE user_id = ? AND tariff = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1", (user_id, tariff),
        ).fetchone()
        if existing:
            return existing["id"]
        cursor = db.execute(
            "INSERT INTO funpay_orders (user_id, username, tariff, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username, tariff, now),
        )
        return cursor.lastrowid


def funpay_order(order_id: int):
    with db_connection() as db:
        return db.execute("SELECT * FROM funpay_orders WHERE id = ?", (order_id,)).fetchone()


def review_funpay_order(order_id: int, admin_id: int, approved: bool):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("SELECT * FROM funpay_orders WHERE id = ?", (order_id,)).fetchone()
        if not order or order["status"] != "pending":
            return None
        status = "approved" if approved else "declined"
        db.execute(
            "UPDATE funpay_orders SET status = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?",
            (status, now, admin_id, order_id),
        )
    return order


def ensure_owner_admin():
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with db_connection() as db:
        db.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (OWNER_ID, now))

def get_admin_ids() -> list[int]:
    with db_connection() as db:
        rows = db.execute("SELECT user_id FROM admins ORDER BY user_id").fetchall()
    return [int(row["user_id"]) for row in rows]

def is_admin(user_id: int) -> bool:
    return user_id in get_admin_ids()

def add_admin(user_id: int) -> bool:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with db_connection() as db:
        result = db.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (user_id, now))
    return result.rowcount > 0

def remove_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return False
    with db_connection() as db:
        result = db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    return result.rowcount > 0


def is_blocked(user_id: int) -> bool:
    with db_connection() as db:
        return db.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)).fetchone() is not None


def block_user(user_id: int, admin_id: int) -> bool:
    if user_id == OWNER_ID or is_admin(user_id):
        return False
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    with db_connection() as db:
        result = db.execute("INSERT OR IGNORE INTO blocked_users (user_id, blocked_at, blocked_by) VALUES (?, ?, ?)", (user_id, now, admin_id))
    return result.rowcount > 0


def unblock_user(user_id: int) -> bool:
    with db_connection() as db:
        result = db.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
    return result.rowcount > 0


def get_blocked_user_ids() -> list[int]:
    with db_connection() as db:
        return [int(r["user_id"]) for r in db.execute("SELECT user_id FROM blocked_users ORDER BY user_id")]


def main_menu(language: str = "ru"):
    builder = InlineKeyboardBuilder()
    if language == "en":
        builder.row(InlineKeyboardButton(text="🛒 Buy EGZO Lua", callback_data="buy"))
        builder.row(InlineKeyboardButton(text="📦 My purchases", callback_data="my_purchases"))
        builder.row(InlineKeyboardButton(text="🆘 Support", callback_data="support"))
        builder.row(InlineKeyboardButton(text="ℹ️ About me", callback_data="about"))
        builder.row(InlineKeyboardButton(text="📢 Official channel", url="https://t.me/+eOQvGXnjFNc3Mzkx"))
        builder.row(InlineKeyboardButton(text="🌐 Official website", url="https://egzolua.xyz"))
    else:
        builder.row(InlineKeyboardButton(text="🛒 Купить EGZO Lua", callback_data="buy"))
        builder.row(InlineKeyboardButton(text="📦 Мои покупки", callback_data="my_purchases"))
        builder.row(InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"))
        builder.row(InlineKeyboardButton(text="ℹ️ Обо мне", callback_data="about"))
        builder.row(InlineKeyboardButton(text="📢 Наш официальный канал", url="https://t.me/+eOQvGXnjFNc3Mzkx"))
        builder.row(InlineKeyboardButton(text="🌐 Официальный сайт", url="https://egzolua.xyz"))
    return builder.as_markup()


def tariffs_menu(language: str = "ru"):
    builder = InlineKeyboardBuilder()
    for key, data in TARIFFS.items():
        title = data['title'] if language == "ru" else {"7":"7 days","14":"14 days","30":"30 days","90":"90 days","365":"365 days"}[key]
        builder.row(InlineKeyboardButton(text=f"⭐ {title} — {data['price']} Stars", callback_data=f"tariff_{key}"))
    builder.row(InlineKeyboardButton(text="◀️ Назад" if language == "ru" else "◀️ Back", callback_data="back_main"))
    return builder.as_markup()


def payment_method_menu(language: str = "ru"):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="buy_stars"))
    builder.row(InlineKeyboardButton(text="💳 FunPay (для СНГ)" if language == "ru" else "💳 FunPay (CIS)", callback_data="buy_funpay"))
    builder.row(InlineKeyboardButton(text="◀️ Назад" if language == "ru" else "◀️ Back", callback_data="back_main"))
    return builder.as_markup()


@dp.message(CommandStart())
async def start(message: Message):
    with db_connection() as db:
        exists = db.execute("SELECT 1 FROM user_languages WHERE user_id = ?", (message.from_user.id,)).fetchone()
    if not exists:
        await message.answer("🌐 Choose your language / Выберите язык:", reply_markup=language_menu())
        return
    language = get_language(message.from_user.id)
    if language == "en":
        text = (
            "<b>🔥 EGZO LUA — premium Lua for Nixware CS2</b>\n\n"
            "Official store: <a href='https://egzolua.xyz'>egzolua.xyz</a>\n\n"
            "• Stable operation\n• Regular updates\n• Fast support\n\n"
            "Choose an action below 👇"
        )
    else:
        text = (
            "<b>🔥 EGZO LUA — премиум Lua для Nixware CS2</b>\n\n"
            "Официальный магазин: <a href='https://egzolua.xyz'>egzolua.xyz</a>\n\n"
            "• Стабильная работа\n• Регулярные обновления\n• Быстрая поддержка\n\n"
            "Выбери действие ниже 👇"
        )
    await message.answer(text, reply_markup=main_menu(language), parse_mode="HTML", disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("lang_"))
async def choose_language(callback: CallbackQuery):
    language = callback.data.split("_")[1]
    set_language(callback.from_user.id, language)
    if language == "en":
        text = "<b>🔥 EGZO LUA — premium Lua for Nixware CS2</b>\n\nOfficial store: <a href='https://egzolua.xyz'>egzolua.xyz</a>\n\nChoose an action below 👇"
    else:
        text = "<b>🔥 EGZO LUA — премиум Lua для Nixware CS2</b>\n\nОфициальный магазин: <a href='https://egzolua.xyz'>egzolua.xyz</a>\n\nВыбери действие ниже 👇"
    await callback.message.edit_text(text, reply_markup=main_menu(language), parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer("Language changed" if language == "en" else "Язык изменён")


@dp.message(Command("lang"))
async def change_language(message: Message):
    await message.answer("🌐 Choose your language / Выберите язык:", reply_markup=language_menu())


@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    language = get_language(callback.from_user.id)
    text = (
        "<b>🔥 EGZO LUA — premium Lua for Nixware CS2</b>\n\nChoose an action:"
        if language == "en" else
        "<b>🔥 EGZO LUA — премиум Lua для Nixware CS2</b>\n\nВыбери действие:"
    )
    await callback.message.edit_text(text, reply_markup=main_menu(language), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "buy")
async def buy_menu(callback: CallbackQuery):
    language = get_language(callback.from_user.id)
    text = "<b>🛒 Buy EGZO Lua</b>\n\nChoose a payment method 👇" if language == "en" else "<b>🛒 Купить EGZO Lua</b>\n\nВыбери способ оплаты 👇"
    await callback.message.edit_text(text, reply_markup=payment_method_menu(language), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "buy_stars")
async def buy_stars(callback: CallbackQuery):
    language = get_language(callback.from_user.id)
    text = ("<b>⭐ Purchase with Telegram Stars</b>\n\nChoose a tariff. After successful payment, the key will be sent automatically." if language == "en" else
            "<b>⭐ Покупка через Telegram Stars</b>\n\nВыбери нужный тариф. После успешной оплаты ключ придёт автоматически.")
    await callback.message.edit_text(text, reply_markup=tariffs_menu(language), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "buy_funpay")
async def buy_funpay(callback: CallbackQuery):
    await funpay_info(callback)


@dp.callback_query(F.data == "buy_stars")
async def buy_stars(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>⭐ Покупка через Telegram Stars</b>\n\n"
        "Выбери нужный тариф. После успешной оплаты ключ придёт автоматически.",
        reply_markup=tariffs_menu(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "buy_funpay")
async def buy_funpay(callback: CallbackQuery):
    await funpay_info(callback)


@dp.callback_query(F.data.startswith("tariff_"))
async def process_tariff(callback: CallbackQuery):
    tariff_id = callback.data.split("_")[1]
    tariff = TARIFFS[tariff_id]

    prices = [LabeledPrice(label=f"EGZO Lua — {tariff['title']}", amount=tariff["price"])]

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"EGZO Lua — {tariff['title']}",
        description=f"Доступ к EGZO Lua на {tariff['days']} дней\nСайт: egzolua.xyz",
        payload=f"egzo_{tariff_id}_{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="egzo-lua"
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    parts = payload.split("_")
    tariff_id = parts[1]
    user_id = message.from_user.id
    tariff = TARIFFS[tariff_id]
    key, already_processed = issue_key_for_payment(
        tariff_id,
        user_id,
        message.from_user.username,
        message.successful_payment.telegram_payment_charge_id,
        tariff["price"],
    )

    language = get_language(user_id)
    title = tariff['title'] if language == "ru" else {"7":"7 days","14":"14 days","30":"30 days","90":"90 days","365":"365 days"}[tariff_id]
    if already_processed:
        text = (f"<b>This payment has already been processed.</b>\n\nYour key:\n<code>{key}</code>" if language == "en" else
                f"<b>Эта оплата уже обработана.</b>\n\nТвой ключ:\n<code>{key}</code>")
        await message.answer(text, parse_mode="HTML")
        return

    if language == "en":
        text = (f"<b>✅ Payment successful!</b>\n\nTariff: <b>{title}</b>\nAmount: <b>{tariff['price']} ⭐</b>\n\n"
                f"<b>Your key:</b>\n<code>{key}</code>\n\nInstallation instructions: egzolua.xyz\n\nIf you have questions — contact support.")
    else:
        text = (f"<b>✅ Оплата прошла успешно!</b>\n\nТариф: <b>{title}</b>\nСумма: <b>{tariff['price']} ⭐</b>\n\n"
                f"<b>Твой ключ:</b>\n<code>{key}</code>\n\nИнструкция по установке: egzolua.xyz\n\nЕсли возникнут вопросы — пиши в поддержку.")
    await message.answer(text, parse_mode="HTML")

    admin_text = (
        f"🛒 <b>Новая покупка!</b>\n\n"
        f"Пользователь: @{message.from_user.username or 'нет'} (ID: {user_id})\n"
        f"Тариф: {tariff['title']}\n"
        f"Сумма: {tariff['price']} ⭐\n"
        f"Ключ: <code>{key}</code>"
    )
    for admin_id in get_admin_ids():
        try:
            await bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except:
            pass


@dp.callback_query(F.data == "funpay")
async def funpay_info(callback: CallbackQuery):
    language = get_language(callback.from_user.id)
    if language == "en":
        text = "<b>💳 Payment via FunPay (CIS)</b>\n\nChoose a tariff. After payment, press «I paid» — an administrator will verify the order and issue the key."
    else:
        text = "<b>💳 Оплата через FunPay (для СНГ)</b>\n\nВыбери нужный тариф. После оплаты нажми «Я оплатил» — администратор проверит заказ и выдаст ключ."
    builder = InlineKeyboardBuilder()
    for tariff_id, tariff in TARIFFS.items():
        title = tariff['title'] if language == "ru" else {"7":"7 days","14":"14 days","30":"30 days","90":"90 days","365":"365 days"}[tariff_id]
        builder.row(InlineKeyboardButton(text=f"{title} — {tariff['price']} ₽", callback_data=f"funpay_tariff_{tariff_id}"))
    builder.row(InlineKeyboardButton(text="◀️ Назад" if language == "ru" else "◀️ Back", callback_data="buy"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("funpay_tariff_"))
async def funpay_tariff(callback: CallbackQuery):
    tariff_id = callback.data.removeprefix("funpay_tariff_")
    tariff = TARIFFS.get(tariff_id)
    language = get_language(callback.from_user.id)
    if not tariff:
        await callback.answer("Tariff not found" if language == "en" else "Тариф не найден", show_alert=True)
        return
    title = tariff['title'] if language == "ru" else {"7":"7 days","14":"14 days","30":"30 days","90":"90 days","365":"365 days"}[tariff_id]
    if language == "en":
        text = f"<b>💳 FunPay — {title}</b>\n\nPrice: <b>{tariff['price']} ₽</b>\n\n1. Open the seller profile on FunPay.\n2. Place an order for the selected tariff.\n3. Return here and press «I paid».\n\nThe key is issued only after manual payment verification by an administrator."
    else:
        text = f"<b>💳 FunPay — {title}</b>\n\nСтоимость: <b>{tariff['price']} ₽</b>\n\n1. Открой профиль продавца на FunPay.\n2. Оформи заказ на выбранный тариф.\n3. Вернись сюда и нажми «Я оплатил».\n\nКлюч выдаётся только после ручной проверки оплаты администратором."
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Open FunPay" if language == "en" else "Открыть FunPay", url="https://funpay.com/users/19327116/"))
    builder.row(InlineKeyboardButton(text="✅ I paid" if language == "en" else "✅ Я оплатил", callback_data=f"funpay_paid_{tariff_id}"))
    builder.row(InlineKeyboardButton(text="◀️ Back to tariffs" if language == "en" else "◀️ К тарифам", callback_data="funpay"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("funpay_paid_"))
async def funpay_paid(callback: CallbackQuery):
    tariff_id = callback.data.removeprefix("funpay_paid_")
    tariff = TARIFFS.get(tariff_id)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    order_id = create_funpay_order(callback.from_user.id, callback.from_user.username, tariff_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"fp_yes_{order_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"fp_no_{order_id}"),
    )
    admin_text = (
        f"<b>💳 Заявка FunPay №{order_id}</b>\n\n"
        f"Покупатель: @{callback.from_user.username or 'нет'} (ID: <code>{callback.from_user.id}</code>)\n"
        f"Тариф: <b>{tariff['title']}</b>\n"
        f"Сумма: <b>{tariff['price']} ₽</b>\n\n"
        "Проверь оплату на FunPay и выбери действие."
    )
    for admin_id in get_admin_ids():
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception:
            logging.exception("Не удалось отправить заявку FunPay администратору")
    language = get_language(callback.from_user.id)
    text = ("<b>✅ Request sent.</b>\n\nAn administrator will verify the FunPay payment. "
            "After confirmation, the key will be sent here automatically." if language == "en" else
            "<b>✅ Заявка отправлена.</b>\n\nАдминистратор проверит оплату на FunPay. После подтверждения ключ придёт сюда автоматически.")
    await callback.message.edit_text(text, reply_markup=main_menu(language), parse_mode="HTML")
    await callback.answer("Request sent" if language == "en" else "Заявка отправлена")


@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    language = get_language(callback.from_user.id)
    if language == "en":
        text = "<b>🆘 EGZO Lua Support</b>\n\nTelegram: @egzolua1\nDiscord: aed201_92585._17994\n\nWrite for any questions: activation, bugs, renewal."
    else:
        text = "<b>🆘 Поддержка EGZO Lua</b>\n\nTelegram: @egzolua1\nDiscord: aed201_92585._17994\n\nПиши по любым вопросам: активация, баги, продление."
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад" if language == "ru" else "◀️ Back", callback_data="back_main"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    language = get_language(callback.from_user.id)
    if language == "en":
        text = (
            "<b>ℹ️ About me</b>\n\n"
            "Hello! I am the official bot for purchasing EGZO Lua keys, made by the creators and programmers.\n\n"
            "I have many keys for many tariffs.\n\n"
            "My creator is @egzolua, and he also has Discord: <code>aed201_92585._17994</code>. "
            "You can write to him anytime, he probably won't mind.\n\n"
            "📢 Our channel: <a href='https://t.me/+eOQvGXnjFNc3Mzkx'>https://t.me/+eOQvGXnjFNc3Mzkx</a>\n\n"
            "I will be happy if you buy a key from us :)"
        )
    else:
        text = (
            "<b>ℹ️ Обо мне</b>\n\n"
            "Привет! Я бот покупки EGZO Lua ключей, официальный бот от создателей и программистов.\n\n"
            "У меня есть много ключей на много тарифов.\n\n"
            "Также мой создатель — @egzolua, у него есть Discord: <code>aed201_92585._17994</code>. "
            "Можете написать ему в любое время, он будет не против (наверно).\n\n"
            "📢 Наш канал: <a href='https://t.me/+eOQvGXnjFNc3Mzkx'>https://t.me/+eOQvGXnjFNc3Mzkx</a>\n\n"
            "Буду счастлив, если купишь ключик у нас :)"
        )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📢 Official channel" if language == "en" else "📢 Наш официальный канал", url="https://t.me/+eOQvGXnjFNc3Mzkx"))
    builder.row(InlineKeyboardButton(text="◀️ Back" if language == "en" else "◀️ Назад", callback_data="back_main"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()


@dp.callback_query(F.data == "my_purchases")
async def my_purchases(callback: CallbackQuery):
    language = get_language(callback.from_user.id)
    user_id = callback.from_user.id
    purchases = purchases_for_user(user_id)
    if not purchases:
        text = "You don't have any purchases yet." if language == "en" else "У тебя пока нет покупок."
    else:
        text = "<b>📦 Your purchases:</b>\n\n" if language == "en" else "<b>📦 Твои покупки:</b>\n\n"
        for purchase in purchases:
            title = TARIFFS[purchase["tariff"]]["title"]
            if language == "en":
                title = {"7":"7 days","14":"14 days","30":"30 days","90":"90 days","365":"365 days"}[purchase["tariff"]]
            key = purchase["license_key"] or ("Issued by support" if language == "en" else "Выдаётся поддержкой")
            text += f"• {title} — {purchase['created_at']}\n" + (f"Key: <code>{key}</code>\n\n" if language == "en" else f"Ключ: <code>{key}</code>\n\n")
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Back" if language == "en" else "◀️ Назад", callback_data="back_main"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return

    counts = available_key_counts()
    total_keys = sum(counts.values())
    text = (
        f"<b>🛠 Админ-панель EGZO</b>\n\n"
        f"Ключей в наличии: <b>{total_keys}</b>\n\n"
        f"7 дней: {counts['7']}\n"
        f"14 дней: {counts['14']}\n"
        f"30 дней: {counts['30']}\n"
        f"90 дней: {counts['90']}\n"
        f"365 дней: {counts['365']}\n\n"
        f"Выбери действие кнопкой ниже."
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🎁 Выдать ключ бесплатно", callback_data="admin_gift"))
    builder.row(InlineKeyboardButton(text="🗑 Убрать ключи", callback_data="admin_remove"))
    builder.row(InlineKeyboardButton(text="👑 Администраторы", callback_data="admin_manage"))
    builder.row(InlineKeyboardButton(text="🔄 Обновить остаток", callback_data="admin_refresh"))
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "admin_manage")
async def admin_manage(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    admins = get_admin_ids()
    admin_list = "\n".join(f"• <code>{x}</code>{' — 👑 владелец' if x == OWNER_ID else ''}" for x in admins)
    builder = InlineKeyboardBuilder()
    if callback.from_user.id == OWNER_ID:
        builder.row(InlineKeyboardButton(text="➕ Добавить администратора", callback_data="admin_add"))
        builder.row(InlineKeyboardButton(text="➖ Удалить администратора", callback_data="admin_del"))
    builder.row(InlineKeyboardButton(text="🚫 Заблокировать пользователя", callback_data="admin_block"))
    builder.row(InlineKeyboardButton(text="✅ Разблокировать пользователя", callback_data="admin_unblock"))
    builder.row(InlineKeyboardButton(text="📋 Заблокированные", callback_data="admin_blocked"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back"))
    await callback.message.edit_text("<b>👑 Управление администраторами</b>\n\n" + admin_list, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add_help(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец.", show_alert=True); return
    await callback.message.answer("➕ <code>/addadmin ID</code>\nПример: <code>/addadmin 123456789</code>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_del")
async def admin_del_help(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("Только владелец.", show_alert=True); return
    await callback.message.answer("➖ <code>/deladmin ID</code>\nПример: <code>/deladmin 123456789</code>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_block")
async def admin_block_help(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    await callback.message.answer("🚫 <b>Блокировка</b>\n\n<code>/block ID</code>\nПример: <code>/block 123456789</code>", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_unblock")
async def admin_unblock_help(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    await callback.message.answer("✅ <b>Разблокировка</b>\n\n<code>/unblock ID</code>\nПример: <code>/unblock 123456789</code>", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_blocked")
async def admin_blocked(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    blocked = get_blocked_user_ids()
    text = "📋 <b>Заблокированных нет.</b>" if not blocked else "📋 <b>Заблокированные:</b>\n\n" + "\n".join(f"• <code>{uid}</code>" for uid in blocked)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_manage"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True); return
    counts = available_key_counts()
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🎁 Выдать ключ бесплатно", callback_data="admin_gift"))
    builder.row(InlineKeyboardButton(text="🗑 Убрать ключи", callback_data="admin_remove"))
    builder.row(InlineKeyboardButton(text="👑 Администраторы", callback_data="admin_manage"))
    builder.row(InlineKeyboardButton(text="🔄 Обновить остаток", callback_data="admin_refresh"))
    await callback.message.edit_text(f"<b>🛠 Админ-панель EGZO</b>\n\nКлючей в наличии: <b>{sum(counts.values())}</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.message(Command("addadmin"))
async def add_admin_command(message: Message):
    if message.from_user.id != OWNER_ID: return
    args = message.text.split()[1:]
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: <code>/addadmin ID</code>", parse_mode="HTML"); return
    uid=int(args[0])
    if uid == OWNER_ID:
        await message.answer("👑 Это ID владельца."); return
    await message.answer(f"✅ Пользователь <code>{uid}</code> добавлен." if add_admin(uid) else "⚠️ Он уже администратор.", parse_mode="HTML")

@dp.message(Command("deladmin"))
async def del_admin_command(message: Message):
    if message.from_user.id != OWNER_ID: return
    args=message.text.split()[1:]
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: <code>/deladmin ID</code>", parse_mode="HTML"); return
    uid=int(args[0])
    if uid == OWNER_ID:
        await message.answer("❌ Владельца удалить нельзя."); return
    await message.answer(f"✅ Пользователь <code>{uid}</code> удалён." if remove_admin(uid) else "⚠️ Администратор не найден.", parse_mode="HTML")


@dp.callback_query(F.data.startswith("fp_yes_") | F.data.startswith("fp_no_"))
async def review_funpay(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    action, raw_id = callback.data.rsplit("_", 1)
    order_id = int(raw_id)
    approved = action == "fp_yes"
    order = funpay_order(order_id)
    if not order or order["status"] != "pending":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return

    if approved:
        key = issue_manual_key(order["tariff"], order["user_id"], order["username"], f"funpay-{order_id}")
        if not key:
            await callback.answer("Нет ключей этого тарифа. Добавь ключи и повтори.", show_alert=True)
            return
        review_funpay_order(order_id, callback.from_user.id, True)
        user_language = get_language(order["user_id"])
        title = TARIFFS[order['tariff']]['title'] if user_language == "ru" else {"7":"7 days","14":"14 days","30":"30 days","90":"90 days","365":"365 days"}[order['tariff']]
        user_text = (f"<b>✅ Payment confirmed!</b>\n\nTariff: <b>{title}</b>\nYour key:\n<code>{key}</code>\n\nInstructions: egzolua.xyz" if user_language == "en" else
                     f"<b>✅ Оплата подтверждена!</b>\n\nТариф: <b>{title}</b>\nТвой ключ:\n<code>{key}</code>\n\nИнструкция: egzolua.xyz")
        await bot.send_message(order["user_id"], user_text, parse_mode="HTML")
        await callback.message.edit_text(
            f"✅ Заявка FunPay №{order_id} подтверждена.\nКлюч выдан пользователю.",
        )
    else:
        review_funpay_order(order_id, callback.from_user.id, False)
        user_language = get_language(order["user_id"])
        user_text = ("❌ The payment request was not confirmed. If this is a mistake, contact support." if user_language == "en" else
                     "❌ Заявка на оплату не подтверждена. Если это ошибка — напиши в поддержку.")
        await bot.send_message(order["user_id"], user_text)
        await callback.message.edit_text(f"❌ Заявка FunPay №{order_id} отклонена.")
    await callback.answer()


@dp.callback_query(F.data == "admin_gift")
async def admin_gift_help(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.answer(
        "Отправь команду:\n<code>/gift ID_пользователя тариф</code>\n\n"
        "Пример: <code>/gift 123456789 14</code>", parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_remove")
async def admin_remove_help(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.answer(
        "Отправь ключи, которые нужно убрать:\n"
        "<code>/removekeys ключ1 ключ2 ключ3</code>\n\n"
        "Удаляются только ещё не выданные ключи.", parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_refresh")
async def admin_refresh(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    counts = available_key_counts()
    await callback.message.edit_text(
        "<b>📦 Ключи в наличии</b>\n\n"
        f"7 дней: {counts['7']}\n14 дней: {counts['14']}\n"
        f"30 дней: {counts['30']}\n90 дней: {counts['90']}\n365 дней: {counts['365']}",
        reply_markup=callback.message.reply_markup, parse_mode="HTML",
    )
    await callback.answer()


@dp.message(Command("block"))
async def block_command(message: Message):
    if not is_admin(message.from_user.id): return
    args = message.text.split()[1:]
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: <code>/block ID</code>", parse_mode="HTML"); return
    uid = int(args[0])
    if uid == OWNER_ID:
        await message.answer("❌ Владельца заблокировать нельзя."); return
    if is_admin(uid):
        await message.answer("❌ Нельзя заблокировать администратора."); return
    if block_user(uid, message.from_user.id):
        await message.answer(f"🚫 Пользователь <code>{uid}</code> заблокирован.", parse_mode="HTML")
        try: await bot.send_message(uid, "🚫 <b>Bot access is blocked.</b>" if get_language(uid) == "en" else "🚫 <b>Доступ к боту заблокирован.</b>", parse_mode="HTML")
        except Exception: pass
    else: await message.answer("⚠️ Пользователь уже заблокирован.")


@dp.message(Command("unblock"))
async def unblock_command(message: Message):
    if not is_admin(message.from_user.id): return
    args = message.text.split()[1:]
    if len(args) != 1 or not args[0].isdigit():
        await message.answer("Использование: <code>/unblock ID</code>", parse_mode="HTML"); return
    uid = int(args[0])
    if unblock_user(uid):
        await message.answer(f"✅ Пользователь <code>{uid}</code> разблокирован.", parse_mode="HTML")
        try: await bot.send_message(uid, "✅ <b>Bot access has been restored.</b>" if get_language(uid) == "en" else "✅ <b>Доступ к боту восстановлен.</b>", parse_mode="HTML")
        except Exception: pass
    else: await message.answer("⚠️ Пользователь не найден среди заблокированных.")


@dp.message(Command("addkeys"))
async def add_keys(message: Message):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()[1:]
    if len(args) < 2:
        await message.answer("Использование:\n/addkeys 7 ключ1 ключ2 ключ3")
        return

    tariff = args[0]
    if tariff not in TARIFFS:
        await message.answer("Неверный тариф. Доступны: 7, 14, 30, 90, 365")
        return

    added, duplicates = add_keys_to_database(tariff, args[1:])
    available = available_key_counts()[tariff]
    suffix = f"\n♻️ Уже были в базе: {duplicates}" if duplicates else ""
    await message.answer(
        f"✅ Добавлено {added} ключей на {tariff} дней.\n"
        f"Сейчас в наличии: {available}{suffix}"
    )


@dp.message(Command("gift"))
async def gift_key(message: Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if len(args) != 2 or not args[0].isdigit() or args[1] not in TARIFFS:
        await message.answer("Использование: <code>/gift ID_пользователя тариф</code>", parse_mode="HTML")
        return
    user_id, tariff = int(args[0]), args[1]
    key = issue_manual_key(tariff, user_id, None, "gift")
    if not key:
        await message.answer("❌ Нет доступных ключей для этого тарифа.")
        return
    try:
        await bot.send_message(
            user_id,
            (f"<b>🎁 You received free EGZO Lua access</b>\n\nTariff: <b>{{title}}</b>\nKey:\n<code>{{key}}</code>" if get_language(user_id) == "en" else
             f"<b>🎁 Тебе выдан бесплатный доступ EGZO Lua</b>\n\nТариф: <b>{{TARIFFS[tariff]['title']}}</b>\nКлюч:\n<code>{{key}}</code>").format(title=(TARIFFS[tariff]['title'] if get_language(user_id) == "ru" else {"7":"7 days","14":"14 days","30":"30 days","90":"90 days","365":"365 days"}[tariff]), key=key), parse_mode="HTML",
        )
        await message.answer(f"✅ Ключ на {TARIFFS[tariff]['title']} выдан пользователю <code>{user_id}</code>.", parse_mode="HTML")
    except Exception:
        await message.answer(
            f"⚠️ Ключ зарезервирован, но бот не смог написать пользователю. "
            f"Пусть он сначала запустит бота: <code>{key}</code>", parse_mode="HTML",
        )


@dp.message(Command("removekeys"))
async def remove_keys(message: Message):
    if not is_admin(message.from_user.id):
        return
    keys = message.text.split()[1:]
    if not keys:
        await message.answer("Использование: <code>/removekeys ключ1 ключ2</code>", parse_mode="HTML")
        return
    removed, not_removed = remove_available_keys(keys)
    await message.answer(
        f"✅ Удалено ключей: {removed}\n"
        f"⚠️ Не удалено: {not_removed} (не найдено или уже выдано)",
    )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Задай BOT_TOKEN в переменной окружения перед запуском.")

    # Используем существующий egzo_bot.sqlite3 рядом с этим скриптом.
    # init_database() только создаёт отсутствующие таблицы и не удаляет
    # существующие ключи, покупки и администраторов.
    init_database()
    ensure_owner_admin()

    counts = available_key_counts()
    print(
        "Бот EGZO Lua запущен. "
        f"База: {DB_PATH}; доступных ключей: {sum(counts.values())}"
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())