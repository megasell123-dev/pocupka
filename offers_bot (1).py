"""
Offers Telegram Bot — single-file edition
Requirements: Python 3.10+, aiogram 3, aiosqlite, python-dotenv.

Setup:
  pip install aiogram aiosqlite python-dotenv
  Set BOT_TOKEN and ADMIN_IDS environment variables, then run:
  python offers_bot.py

You can also put the variables in a .env file beside this script.
"""

import asyncio
import logging
import os
import sys
import subprocess
import importlib.util
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

# Автоматически устанавливаем необходимые пакеты при первом запуске.
# Можно отключить автоматическую установку переменной AUTO_INSTALL_DEPS=0.
REQUIRED_PACKAGES = {
    "aiogram": "aiogram>=3.4,<4.0",
    "aiosqlite": "aiosqlite>=0.19",
    "dotenv": "python-dotenv>=1.0",
}

def ensure_dependencies():
    if os.getenv("AUTO_INSTALL_DEPS", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    missing = [
        package for module, package in REQUIRED_PACKAGES.items()
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return
    print("Устанавливаю необходимые зависимости:", ", ".join(missing), flush=True)
    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing
        ])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "Не удалось установить зависимости автоматически. "
            "Проверьте интернет и права установки, затем выполните: "
            f"{sys.executable} -m pip install {' '.join(missing)}"
        ) from exc

ensure_dependencies()

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in .env or as an environment variable.")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS is missing. Set your numeric Telegram ID in .env.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("offers-bot")

# ---------------------------------------------------------------------------
# SQLite database (embedded from database.py so this is a true single file)
# ---------------------------------------------------------------------------

DEFAULTS = {
    "welcome_text": "<b>Offers</b>\n\nУправление доступом и рабочими инструментами.\n\nВыберите раздел.",
    "welcome_video_file_id": "",
    "price": "6",
    "currency": "TON",
    "wallet_address": "",
    "delivery_text": "Ваш доступ активирован. Используйте инструкцию или ссылку, указанную ниже.",
}

class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn = None

    async def init(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            created_at TEXT NOT NULL, subscription_until TEXT, referrer_id INTEGER,
            state TEXT NOT NULL DEFAULT '', promo_pending TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount TEXT NOT NULL, currency TEXT NOT NULL, wallet TEXT NOT NULL,
            comment TEXT NOT NULL, promo_code TEXT, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, submitted_at TEXT, resolved_at TEXT,
            resolved_by INTEGER, subscription_until TEXT
        );
        CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY, discount_type TEXT NOT NULL, discount TEXT NOT NULL,
            usage_limit INTEGER NOT NULL, used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promo_redemptions (
            user_id INTEGER PRIMARY KEY,
            promo_code TEXT NOT NULL,
            payment_id INTEGER NOT NULL UNIQUE,
            redeemed_at TEXT NOT NULL
        );
        """)
        for key, value in DEFAULTS.items():
            await self.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def _one(self, sql, params=()):
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None

    async def _all(self, sql, params=()):
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [dict(r) for r in rows]

    async def ensure_user(self, user_id, username, full_name):
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute("""INSERT INTO users(user_id,username,full_name,created_at)
            VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
            (user_id, username, full_name, now))
        await self.conn.commit()

    async def set_referrer(self, user_id, referrer_id):
        await self.conn.execute("UPDATE users SET referrer_id=? WHERE user_id=? AND referrer_id IS NULL", (referrer_id, user_id))
        await self.conn.commit()

    async def get_settings(self):
        rows = await self._all("SELECT key,value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    async def set_setting(self, key, value):
        await self.conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self.conn.commit()

    async def create_payment(self, user_id, amount, currency, wallet, comment, promo_code):
        now = datetime.now(timezone.utc).isoformat()
        cur = await self.conn.execute("""INSERT INTO payments(user_id,amount,currency,wallet,comment,promo_code,status,created_at)
            VALUES(?,?,?,?,?,?,'pending',?)""", (user_id, amount, currency, wallet, comment, promo_code, now))
        await self.conn.commit()
        return cur.lastrowid

    async def get_payment(self, payment_id):
        return await self._one("SELECT * FROM payments WHERE id=?", (payment_id,))

    async def mark_payment_submitted(self, payment_id):
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute("UPDATE payments SET status='submitted',submitted_at=? WHERE id=? AND status='pending'", (now, payment_id))
        await self.conn.commit()

    async def resolve_payment(self, payment_id, status, admin_id, expiry):
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self.conn.execute("BEGIN IMMEDIATE")
            cur = await self.conn.execute("""UPDATE payments SET status=?,resolved_at=?,resolved_by=?,subscription_until=?
                WHERE id=? AND status IN ('pending','submitted')""", (status, now, admin_id, expiry, payment_id))
            if cur.rowcount != 1:
                await self.conn.rollback()
                return False
            if status == "approved":
                payment = await self._one("SELECT user_id, promo_code FROM payments WHERE id=?", (payment_id,))
                user_id = payment["user_id"]
                promo_code = payment["promo_code"]
                if promo_code:
                    already_used = await self._one("SELECT 1 AS yes FROM promo_redemptions WHERE user_id=?", (user_id,))
                    if already_used:
                        await self.conn.rollback()
                        return False
                    promo = await self._one("SELECT used_count, usage_limit FROM promos WHERE code=?", (promo_code,))
                    if not promo or promo["used_count"] >= promo["usage_limit"]:
                        await self.conn.rollback()
                        return False
                    await self.conn.execute(
                        "INSERT INTO promo_redemptions(user_id,promo_code,payment_id,redeemed_at) VALUES(?,?,?,?)",
                        (user_id, promo_code, payment_id, now),
                    )
                    await self.conn.execute("UPDATE promos SET used_count=used_count+1 WHERE code=?", (promo_code,))
                await self.conn.execute("UPDATE users SET subscription_until=? WHERE user_id=?", (expiry, user_id))
            await self.conn.commit()
            return True
        except Exception:
            await self.conn.rollback()
            raise

    async def get_user(self, user_id):
        return await self._one("SELECT * FROM users WHERE user_id=?", (user_id,))

    async def list_users(self, limit=15):
        return await self._all("SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,))

    async def list_payments(self, limit=10):
        return await self._all("SELECT * FROM payments ORDER BY id DESC LIMIT ?", (limit,))

    async def set_user_state(self, user_id, state):
        await self.conn.execute("UPDATE users SET state=? WHERE user_id=?", (state, user_id))
        await self.conn.commit()

    async def get_user_state(self, user_id):
        row = await self._one("SELECT state FROM users WHERE user_id=?", (user_id,))
        return row["state"] if row else ""

    async def has_redeemed_promo(self, user_id):
        row = await self._one("SELECT 1 AS yes FROM promo_redemptions WHERE user_id=?", (user_id,))
        return bool(row)

    async def set_user_promo(self, user_id, code):
        # Новый промокод заменяет предыдущий, а не суммируется с ним.
        await self.conn.execute("UPDATE users SET promo_pending=? WHERE user_id=?", (code, user_id))
        await self.conn.commit()

    async def get_user_promo(self, user_id):
        if await self.has_redeemed_promo(user_id):
            return None
        row = await self._one("""SELECT p.* FROM users u JOIN promos p ON p.code=u.promo_pending WHERE u.user_id=?""", (user_id,))
        if not row or row["used_count"] >= row["usage_limit"]:
            return None
        try:
            expiry = datetime.fromisoformat(row["expires_at"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                return None
        except ValueError:
            return None
        return row

    async def clear_user_promo(self, user_id):
        await self.conn.execute("UPDATE users SET promo_pending=NULL WHERE user_id=?", (user_id,))
        await self.conn.commit()

    async def get_promo(self, code):
        row = await self._one("SELECT * FROM promos WHERE code=?", (code,))
        if not row:
            return None
        try:
            expiry = datetime.fromisoformat(row["expires_at"])
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                return None
        except ValueError:
            return None
        return row

    async def create_promo(self, code, discount_type, discount, usage_limit, expires_at):
        now = datetime.now(timezone.utc).isoformat()
        await self.conn.execute("INSERT INTO promos(code,discount_type,discount,usage_limit,expires_at,created_at) VALUES(?,?,?,?,?,?)",
            (code, discount_type, discount, usage_limit, expires_at, now))
        await self.conn.commit()

    async def list_promos(self):
        return await self._all("SELECT * FROM promos ORDER BY created_at DESC")

    async def increment_promo_usage(self, code):
        if code:
            await self.conn.execute("UPDATE promos SET used_count=used_count+1 WHERE code=? AND used_count<usage_limit", (code,))
            await self.conn.commit()

    async def create_ticket(self, user_id, message):
        now = datetime.now(timezone.utc).isoformat()
        cur = await self.conn.execute("INSERT INTO tickets(user_id,message,created_at) VALUES(?,?,?)", (user_id, message, now))
        await self.conn.commit()
        return cur.lastrowid

    async def get_ticket(self, ticket_id):
        return await self._one("SELECT * FROM tickets WHERE id=?", (ticket_id,))

    async def list_tickets(self, limit=10):
        return await self._all("SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,))

    async def referral_count(self, user_id):
        row = await self._one("SELECT COUNT(*) AS n FROM users WHERE referrer_id=?", (user_id,))
        return row["n"]

    async def stats(self):
        now = datetime.now(timezone.utc).isoformat()
        users = await self._one("SELECT COUNT(*) AS n FROM users")
        active = await self._one("SELECT COUNT(*) AS n FROM users WHERE subscription_until>?", (now,))
        payments = await self._one("SELECT COUNT(*) AS n FROM payments")
        approved = await self._one("SELECT COUNT(*) AS n FROM payments WHERE status='approved'")
        pending = await self._one("SELECT COUNT(*) AS n FROM payments WHERE status IN ('pending','submitted')")
        return {"users": users["n"], "active": active["n"], "payments": payments["n"], "approved": approved["n"], "pending": pending["n"]}

db = Database(DB_PATH)
dp = Dispatcher()

# Разделы показываются inline-кнопками прямо под приветственным сообщением.
# Это позволяет меню быть привязанным к сообщению, а не располагаться под полем ввода.
def user_menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 ПОДПИСКА", callback_data="user:subscription")],
        [InlineKeyboardButton(text="🔑 МОЙ ДОСТУП", callback_data="user:access")],
        [InlineKeyboardButton(text="👥 Реферальная система", callback_data="user:referrals")],
        [InlineKeyboardButton(text="🛠 Поддержка", callback_data="user:support")],
    ])

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def money(value) -> str:
    return f"{Decimal(str(value)).normalize():f}"

def main_menu_text(settings: dict) -> str:
    return settings.get("welcome_text") or DEFAULTS["welcome_text"]

def admin_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    rows = [
        ("💳 Платежи", "adm:payments"), ("👥 Пользователи", "adm:users"),
        ("🎟 Промокоды", "adm:promos"), ("⚙️ Настройки", "adm:settings"),
        ("🎬 Приветствие и видео", "adm:welcome"), ("🔗 Выдача доступа", "adm:delivery"),
        ("📨 Поддержка", "adm:support"), ("📊 Статистика", "adm:stats"),
    ]
    for label, data in rows:
        b.button(text=label, callback_data=data)
    b.adjust(2)
    return b.as_markup()

def back_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="adm:home")]])

def payment_actions(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"pay:approve:{payment_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pay:reject:{payment_id}")]
    ])

async def notify_admins(bot: Bot, text: str, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except (TelegramForbiddenError, TelegramBadRequest):
            log.warning("Could not notify admin %s", admin_id)

async def show_home(message: Message, bot: Bot):
    # Убираем reply-клавиатуру, оставшуюся от старой версии бота.
    # Основное меню всегда показывается inline-кнопками под приветствием.
    removal_message = await message.answer("‎", reply_markup=ReplyKeyboardRemove())
    try:
        await bot.delete_message(message.chat.id, removal_message.message_id)
    except TelegramBadRequest:
        pass
    settings = await db.get_settings()
    video_id = settings.get("welcome_video_file_id", "")
    text = main_menu_text(settings)
    markup = user_menu_inline()
    if video_id:
        try:
            await bot.send_video(message.chat.id, video_id, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest:
            log.exception("Stored welcome video could not be sent")
    await message.answer(text, reply_markup=markup)

@dp.message(CommandStart())
async def start(message: Message, bot: Bot):
    await db.ensure_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    args = (message.text or "").split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            referrer_id = int(args[1][4:])
            if referrer_id != message.from_user.id:
                await db.set_referrer(message.from_user.id, referrer_id)
        except ValueError:
            pass
    await show_home(message, bot)

@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("<b>Панель администратора</b>\nВыберите действие.", reply_markup=admin_menu())

@dp.callback_query(F.data == "user:subscription")
async def user_subscription(callback: CallbackQuery):
    settings = await db.get_settings()
    price = settings.get("price", "6")
    currency = settings.get("currency", "TON")
    b = InlineKeyboardBuilder()
    b.button(text=f"Оплатить {price} {currency}", callback_data="sub:pay")
    b.button(text="🎟 Ввести промокод", callback_data="promo:enter")
    b.button(text="⬅️ Главное меню", callback_data="user:home")
    b.adjust(1)
    await callback.message.answer(
        f"<b>ОФОРМЛЕНИЕ ДОСТУПА</b>\\n━━━━━━━━━━━━━━\\nСрок: 30 дней\\nСтоимость: {price} {currency}\\n\\nВыберите действие.",
        reply_markup=b.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "user:access")
async def user_access(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    expiry = parse_dt(user.get("subscription_until") if user else None)
    if expiry and expiry > utcnow():
        text = f"✅ Подписка активна до <code>{expiry.strftime('%Y-%m-%d %H:%M UTC')}</code>."
    else:
        text = "Активной подписки нет. Оформите доступ в разделе «ПОДПИСКА»."
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 ПОДПИСКА", callback_data="user:subscription")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="user:home")],
    ]))
    await callback.answer()

@dp.callback_query(F.data == "user:referrals")
async def user_referrals(callback: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    count = await db.referral_count(callback.from_user.id)
    await callback.message.answer(
        f"Ваша ссылка:\\n<code>https://t.me/{me.username}?start=ref_{callback.from_user.id}</code>\\nПриглашено пользователей: {count}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="user:home")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "user:support")
async def user_support(callback: CallbackQuery):
    await db.set_user_state(callback.from_user.id, "support")
    await callback.message.answer("Напишите одним сообщением, чем мы можем помочь. Оно будет передано администратору.\\n/cancel — отмена.")
    await callback.answer()

@dp.callback_query(F.data == "user:home")
async def user_home(callback: CallbackQuery, bot: Bot):
    settings = await db.get_settings()
    video_id = settings.get("welcome_video_file_id", "")
    text = main_menu_text(settings)
    markup = user_menu_inline()
    if video_id:
        try:
            await bot.send_video(callback.message.chat.id, video_id, caption=text, reply_markup=markup)
            await callback.answer()
            return
        except TelegramBadRequest:
            log.exception("Stored welcome video could not be sent")
    await callback.message.answer(text, reply_markup=markup)
    await callback.answer()

@dp.message(F.text == "⬅️ Главное меню")
async def text_home(message: Message, bot: Bot):
    await show_home(message, bot)

@dp.message(F.text == "💳 ПОДПИСКА")
async def subscription(message: Message):
    settings = await db.get_settings()
    price = settings.get("price", "6")
    currency = settings.get("currency", "TON")
    b = InlineKeyboardBuilder()
    b.button(text=f"Оплатить {price} {currency}", callback_data="sub:pay")
    b.button(text="🎟 Ввести промокод", callback_data="promo:enter")
    b.adjust(1)
    await message.answer(
        f"<b>ОФОРМЛЕНИЕ ДОСТУПА</b>\n━━━━━━━━━━━━━━\nСрок: 30 дней\nСтоимость: {price} {currency}\n\nВыберите действие.",
        reply_markup=b.as_markup()
    )

@dp.callback_query(F.data == "sub:pay")
async def create_payment(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    settings = await db.get_settings()
    wallet = settings.get("wallet_address", "").strip()
    price = settings.get("price", "6")
    if not wallet:
        await callback.answer("Администратор ещё не настроил кошелёк.", show_alert=True)
        return
    promo = await db.get_user_promo(user.id)
    final_price = Decimal(str(price))
    promo_note = ""
    if promo:
        discount = Decimal(str(promo["discount"]))
        # Все скидки — фиксированная сумма в TON, включая ранее созданные коды.
        final_price = max(Decimal("0"), final_price - discount)
        promo_note = f"\nПромокод: {promo['code']} (-{money(discount)} TON)"
    final_price = final_price.quantize(Decimal("0.000001"))
    comment = str(user.id)
    payment_id = await db.create_payment(user.id, str(final_price), settings.get("currency", "TON"), wallet, comment, promo["code"] if promo else None)
    await db.clear_user_promo(user.id)
    text = (
        "<b>ОПЛАТА TON</b>\n━━━━━━━━━━━━━━\n"
        f"Сумма: <code>{money(final_price)} {settings.get('currency', 'TON')}</code>\n"
        "Сеть: TON\n\n"
        f"Кошелёк:\n<code>{wallet}</code>\n\n"
        f"Комментарий к платежу (обязательно): <code>{comment}</code>\n\n"
        "Переведите точную сумму и укажите ваш Telegram ID в комментарии/memo, если кошелёк поддерживает комментарии. "
        "Затем нажмите «Я оплатил». Заявка будет отправлена администратору на проверку."
        f"{promo_note}\n\nНомер заявки: <code>#{payment_id}</code>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил — проверить", callback_data=f"pay:submit:{payment_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="sub:back")]
    ])
    await callback.message.answer(text, reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "sub:back")
async def subscription_back(callback: CallbackQuery):
    await user_home(callback, callback.bot)

@dp.callback_query(F.data.startswith("pay:submit:"))
async def submit_payment(callback: CallbackQuery, bot: Bot):
    payment_id = int(callback.data.rsplit(":", 1)[1])
    payment = await db.get_payment(payment_id)
    if not payment or payment["user_id"] != callback.from_user.id:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if payment["status"] != "pending":
        await callback.answer("Эта заявка уже обработана или отправлена.", show_alert=True)
        return
    await db.mark_payment_submitted(payment_id)
    username = f"@{callback.from_user.username}" if callback.from_user.username else "не указан"
    text = (
        "🔔 <b>Заявка на проверку оплаты</b>\n"
        f"Заявка: <code>#{payment_id}</code>\n"
        f"Пользователь: <code>{payment['user_id']}</code> ({username})\n"
        f"Сумма: <code>{payment['amount']} {payment['currency']}</code>\n"
        f"Кошелёк: <code>{payment['wallet']}</code>\n"
        f"Комментарий ожидался: <code>{payment['comment']}</code>\n"
        f"Промокод: {payment['promo_code'] or 'нет'}\n\n"
        "Проверьте транзакцию в блокчейне/кошельке перед подтверждением."
    )
    await notify_admins(bot, text, payment_actions(payment_id))
    await callback.message.answer("Заявка отправлена администратору. Доступ будет выдан после подтверждения.")
    await callback.answer("Заявка отправлена.")

@dp.callback_query(F.data.startswith("pay:approve:"))
async def approve_payment(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    payment_id = int(callback.data.rsplit(":", 1)[1])
    payment = await db.get_payment(payment_id)
    if not payment or payment["status"] not in ("pending", "submitted"):
        await callback.answer("Заявка уже обработана или не найдена.", show_alert=True); return
    settings = await db.get_settings()
    now = utcnow()
    old_expiry = parse_dt((await db.get_user(payment["user_id"]) or {}).get("subscription_until"))
    start_from = old_expiry if old_expiry and old_expiry > now else now
    expiry = start_from + timedelta(days=30)
    changed = await db.resolve_payment(payment_id, "approved", callback.from_user.id, expiry.isoformat())
    if not changed:
        await callback.answer("Заявка уже обработана.", show_alert=True); return
    delivery = settings.get("delivery_text", "Оплата подтверждена! Ваш доступ активирован на 30 дней.")
    try:
        await bot.send_message(payment["user_id"], f"✅ <b>Оплата подтверждена!</b>\nДоступ до: <code>{expiry.strftime('%Y-%m-%d %H:%M UTC')}</code>\n\n{delivery}", disable_web_page_preview=True)
    except TelegramForbiddenError:
        log.info("User %s blocked the bot", payment["user_id"])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Подтверждено. Пользователь {payment['user_id']}, доступ до {expiry.strftime('%Y-%m-%d %H:%M UTC')}.")
    await callback.answer("Платёж подтверждён.")

@dp.callback_query(F.data.startswith("pay:reject:"))
async def reject_payment(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    payment_id = int(callback.data.rsplit(":", 1)[1])
    payment = await db.get_payment(payment_id)
    if not payment or payment["status"] not in ("pending", "submitted"):
        await callback.answer("Заявка уже обработана или не найдена.", show_alert=True); return
    changed = await db.resolve_payment(payment_id, "rejected", callback.from_user.id, None)
    if not changed:
        await callback.answer("Заявка уже обработана.", show_alert=True); return
    try:
        await bot.send_message(payment["user_id"], f"❌ Заявка на оплату #{payment_id} отклонена. Если вы считаете это ошибкой, обратитесь в поддержку.")
    except TelegramForbiddenError:
        pass
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Заявка #{payment_id} отклонена.")
    await callback.answer("Отклонено.")

@dp.message(F.text == "🔑 МОЙ ДОСТУП")
async def my_access(message: Message):
    user = await db.get_user(message.from_user.id)
    expiry = parse_dt(user.get("subscription_until") if user else None)
    if expiry and expiry > utcnow():
        await message.answer(f"✅ Подписка активна до <code>{expiry.strftime('%Y-%m-%d %H:%M UTC')}</code>.")
    else:
        await message.answer("Активной подписки нет. Оформите доступ в разделе «ПОДПИСКА».")

@dp.message(F.text == "👥 Реферальная система")
async def referrals(message: Message, bot: Bot):
    me = await bot.get_me()
    count = await db.referral_count(message.from_user.id)
    await message.answer(f"Ваша ссылка:\n<code>https://t.me/{me.username}?start=ref_{message.from_user.id}</code>\nПриглашено пользователей: {count}")

@dp.message(F.text == "🛠 Поддержка")
async def support_start(message: Message):
    await db.set_user_state(message.from_user.id, "support")
    await message.answer("Напишите одним сообщением, чем мы можем помочь. Оно будет передано администратору.")

@dp.message(Command("cancel"))
async def cancel(message: Message):
    await db.set_user_state(message.from_user.id, "")
    await message.answer("Действие отменено.")
    await show_home(message, message.bot)

@dp.message(F.text, ~F.text.startswith("/"))
async def handle_text(message: Message, bot: Bot):
    state = await db.get_user_state(message.from_user.id)
    if state == "support":
        await db.set_user_state(message.from_user.id, "")
        ticket_id = await db.create_ticket(message.from_user.id, message.text)
        await notify_admins(bot, f"📨 <b>Обращение в поддержку #{ticket_id}</b>\nОт: <code>{message.from_user.id}</code>\n\n{message.text}",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✉️ Ответить", callback_data=f"support:reply:{ticket_id}")]]))
        await message.answer("Сообщение отправлено в поддержку. Ожидайте ответа.")
        return
    if state == "user_promo":
        promo = await db.get_promo(message.text.strip().upper())
        await db.set_user_state(message.from_user.id, "")
        if not promo:
            await message.answer("Промокод не найден или срок его действия истёк.")
            return
        if promo["used_count"] >= promo["usage_limit"]:
            await message.answer("Лимит использований этого промокода исчерпан.")
            return
        if await db.has_redeemed_promo(message.from_user.id):
            await db.clear_user_promo(message.from_user.id)
            await message.answer("Вы уже использовали промокод на этом аккаунте. Повторное применение промокодов запрещено.")
            return
        # Один активный промокод: новый код заменяет прежний.
        await db.set_user_promo(message.from_user.id, promo["code"])
        await message.answer(f"Промокод <code>{promo['code']}</code> принят. Скидка заменяет предыдущую, не складывается с ней. Откройте «ПОДПИСКА» и выберите оплату.")
        return
    if state and state.startswith("admin_set:") and is_admin(message.from_user.id):
        key = state.split(":", 1)[1]
        if key == "welcome_video_file_id":
            await message.answer("Для видео отправьте видеофайл, а не текст.")
            return
        await db.set_setting(key, message.text.strip())
        await db.set_user_state(message.from_user.id, "")
        await message.answer(f"Настройка <code>{key}</code> обновлена.", reply_markup=admin_menu())
        return
    if state == "promo_create" and is_admin(message.from_user.id):
        await db.set_user_state(message.from_user.id, "")
        parts = message.text.split()
        try:
            code = parts[0].upper()
            opts = {parts[i].lower(): parts[i+1] for i in range(1, len(parts)-1, 2)}
            discount_type = opts.get("type", "fixed").lower()
            discount = Decimal(opts.get("discount", "10"))
            limit = int(opts.get("limit", "10"))
            days = int(opts.get("days", "30"))
            if discount_type != "fixed" or discount <= 0 or limit <= 0 or days <= 0:
                raise ValueError()
            await db.create_promo(code, discount_type, str(discount), limit, (utcnow()+timedelta(days=days)).isoformat())
            await message.answer(f"Промокод <code>{code}</code> создан.", reply_markup=admin_menu())
        except (ValueError, InvalidOperation, IndexError):
            await message.answer("Неверный формат.\nУкажите фиксированную скидку в TON. Пример: SAVE type fixed discount 1 limit 10 days 7")
        return
    if state and state.startswith("admin_reply:") and is_admin(message.from_user.id):
        try:
            user_id = int(state.split(":", 1)[1])
            await bot.send_message(user_id, f"💬 <b>Ответ поддержки</b>\n\n{message.text}")
            await message.answer("Ответ отправлен.", reply_markup=admin_menu())
        except (ValueError, TelegramForbiddenError):
            await message.answer("Не удалось отправить ответ.")
        await db.set_user_state(message.from_user.id, "")
        return

@dp.message(F.video)
async def admin_video(message: Message):
    if not is_admin(message.from_user.id):
        return
    state = await db.get_user_state(message.from_user.id)
    if state == "admin_set:welcome_video_file_id":
        await db.set_setting("welcome_video_file_id", message.video.file_id)
        await db.set_user_state(message.from_user.id, "")
        await message.answer("Приветственное видео сохранено.", reply_markup=admin_menu())

@dp.callback_query(F.data == "promo:enter")
async def promo_enter(callback: CallbackQuery):
    await db.set_user_state(callback.from_user.id, "user_promo")
    await callback.message.answer("Введите промокод. Для отмены отправьте /cancel.")
    await callback.answer()

@dp.callback_query(F.data.startswith("support:reply:"))
async def support_reply(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    ticket = await db.get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Обращение не найдено.", show_alert=True); return
    await db.set_user_state(callback.from_user.id, f"admin_reply:{ticket['user_id']}")
    await callback.message.answer(f"Введите ответ пользователю {ticket['user_id']}. /cancel — отмена.")
    await callback.answer()

@dp.callback_query(F.data.startswith("adm:"))
async def admin_callbacks(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    action = callback.data.split(":", 1)[1]
    if action == "home":
        await callback.message.answer("Панель администратора", reply_markup=admin_menu())
    elif action == "payments":
        rows = await db.list_payments(10)
        if not rows:
            await callback.message.answer("Платежей пока нет.", reply_markup=back_admin())
        else:
            await callback.message.answer("\n\n".join(
                f"#{p['id']} — {p['status']}\nID: {p['user_id']} | {p['amount']} {p['currency']}\nКошелёк: {p['wallet']}"
                for p in rows), reply_markup=back_admin())
    elif action == "users":
        rows = await db.list_users(15)
        await callback.message.answer("Последние пользователи:\n\n" + ("\n".join(
            f"{u['user_id']} | @{u['username'] or '—'} | до {u['subscription_until'] or 'нет подписки'}"
            for u in rows) or "Пользователей нет."), reply_markup=back_admin())
    elif action == "promos":
        b = InlineKeyboardBuilder()
        b.button(text="➕ Создать промокод", callback_data="adm:promo_create")
        b.button(text="📋 Список промокодов", callback_data="adm:promo_list")
        b.button(text="⬅️ Назад", callback_data="adm:home")
        b.adjust(1)
        await callback.message.answer("Промокоды", reply_markup=b.as_markup())
    elif action == "promo_create":
        await db.set_user_state(callback.from_user.id, "promo_create")
        await callback.message.answer("Отправьте строку с фиксированной скидкой в TON, например:\n<code>SAVE type fixed discount 1 limit 10 days 7</code>")
    elif action == "promo_list":
        promos = await db.list_promos()
        await callback.message.answer("\n".join(f"{p['code']} — {p['discount']} {p['discount_type']}; использовано {p['used_count']}/{p['usage_limit']}; до {p['expires_at']}" for p in promos) or "Промокодов нет.", reply_markup=back_admin())
    elif action == "settings":
        s = await db.get_settings()
        b = InlineKeyboardBuilder()
        for label, key in [("Цена подписки","price"),("Валюта","currency"),("Адрес кошелька","wallet_address")]:
            b.button(text=f"Изменить: {label}", callback_data=f"set:{key}")
        b.button(text="⬅️ Назад", callback_data="adm:home")
        b.adjust(1)
        await callback.message.answer(f"Текущие настройки:\nЦена: {s.get('price')}\nВалюта: {s.get('currency')}\nКошелёк: {s.get('wallet_address') or 'не задан'}", reply_markup=b.as_markup())
    elif action == "welcome":
        b = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Изменить приветствие", callback_data="set:welcome_text")],
            [InlineKeyboardButton(text="Загрузить/заменить видео", callback_data="set:welcome_video_file_id")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")]
        ])
        await callback.message.answer("Настройки приветствия. Для видео нажмите кнопку и отправьте видео в чат.", reply_markup=b)
    elif action == "delivery":
        await callback.message.answer("Настройте сообщение после подтверждения оплаты.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Изменить сообщение выдачи", callback_data="set:delivery_text")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")]
        ]))
    elif action == "support":
        rows = await db.list_tickets(10)
        await callback.message.answer("\n\n".join(f"#{t['id']} | user {t['user_id']} | {t['created_at']}\n{t['message']}" for t in rows) or "Обращений нет.", reply_markup=back_admin())
    elif action == "stats":
        s = await db.stats()
        await callback.message.answer(f"Пользователей: {s['users']}\nАктивных подписок: {s['active']}\nПлатежей всего: {s['payments']}\nПодтверждено: {s['approved']}\nОжидают: {s['pending']}", reply_markup=back_admin())
    await callback.answer()

@dp.callback_query(F.data.startswith("set:"))
async def setting_prompt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа.", show_alert=True); return
    key = callback.data.split(":", 1)[1]
    allowed = {"price", "currency", "wallet_address", "welcome_text", "welcome_video_file_id", "delivery_text"}
    if key not in allowed:
        await callback.answer("Неизвестная настройка.", show_alert=True); return
    await db.set_user_state(callback.from_user.id, f"admin_set:{key}")
    prompt = "Теперь отправьте видеофайл в этот чат." if key == "welcome_video_file_id" else f"Отправьте новое значение для настройки <code>{key}</code>. /cancel — отмена."
    await callback.message.answer(prompt)
    await callback.answer()

async def main():
    await db.init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())
