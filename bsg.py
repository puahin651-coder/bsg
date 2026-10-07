import os
import re
import json
import asyncio
import logging
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from sqlalchemy import String, Integer, Numeric, DateTime, Text, Boolean, ForeignKey, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy import create_engine

# ============================================================
# 1. НАСТРОЙКИ — СЮДА МОЖНО ВСТАВИТЬ КЛЮЧИ ПРЯМО В КОД.
#    Для Render лучше использовать Environment Variables.
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8924692482:AAFb_nK6_gcph2kslZh5Ge1K_y3gdgSKJJQ")
CRYPTO_PAY_TOKEN = os.getenv("643224:AA0wshl4254RctN7BpPE7Et7ABuMeF23kqd", "PASTE_CRYPTO_PAY_API_TOKEN_HERE")
XROCKET_API_TOKEN = os.getenv("XROCKET_API_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBJZCI6IjMwMzk0MiIsImp0aSI6ImFwcDozMDM5NDI6YzdkNjhlYjQtY2U0Yy00OWZlLWIyZDgtZWVmYTBlYjU3MjE3IiwiaWF0IjoxNzkxMzYzNDY0fQ.nMhLODuFpDyBZGXOhMW-KQKPbdJFG776U57L2i9p3fE")

# ID администраторов через запятую: ADMIN_IDS=123456789,987654321
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip().isdigit()}

# Комиссия сервиса по умолчанию — 3% как на скриншотах.
DEFAULT_COMMISSION = Decimal(os.getenv("COMMISSION_PERCENT", "3"))
MIN_AMOUNT = Decimal("1")
MAX_AMOUNT = Decimal("500000")

# База. Для Render + внешний PostgreSQL можно задать DATABASE_URL.
# Пример: postgresql+psycopg://user:password@host:5432/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bot.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
el_kwargs = {"connect_args": {"check_same_thread": False}} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, **el_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# Публичные ссылки из ТЗ/скриншотов.
RULES_URL = "https://telegra.ph/Pravila-chata-BSG-03-30"
TECH_SUPPORT_URL = "https://t.me/devjeb"
IMPORTANT_SUPPORT_URL = "https://t.me/aliseglassss"
PROJECT_URL = "https://t.me/portalbsg_bot"
ARBITRAGE_URL = "https://telegra.ph/Arbitrazh-kak-vyzvat-i-kak-prohodit-01-04"
FORBIDDEN_DEALS_URL = "https://telegra.ph/Zapreschchyonnyye-sdelki-01-04"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("escrow_bot")


# ============================================================
# DATABASE
# ============================================================
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    balance: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=0)
    deposited: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=0)
    withdrawn: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Deal(Base):
    __tablename__ = "deals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    buyer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    seller_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="buyer")
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), default=0)
    commission_percent: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=3)
    creator_fee_share: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=50)
    terms: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="waiting_partner", index=True)
    funded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    target: Mapped[str] = mapped_column(String(256))
    external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Ledger(Base):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    reference: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


Base.metadata.create_all(engine)


def db_get_user(tg_id: int, username: Optional[str] = None, first_name: str = "") -> User:
    with SessionLocal() as s:
        u = s.scalar(select(User).where(User.tg_id == tg_id))
        if not u:
            u = User(tg_id=tg_id, username=username, first_name=first_name)
            s.add(u)
        else:
            u.username = username
            u.first_name = first_name
        s.commit()
        s.refresh(u)
        return u


def db_get_setting(key: str, default: str) -> str:
    with SessionLocal() as s:
        row = s.get(Setting, key)
        return row.value if row else default


def db_set_setting(key: str, value: str) -> None:
    with SessionLocal() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        s.commit()


def commission() -> Decimal:
    try:
        return Decimal(db_get_setting("commission", str(DEFAULT_COMMISSION)))
    except Exception:
        return DEFAULT_COMMISSION


def maintenance() -> bool:
    return db_get_setting("maintenance", "0") == "1"


def money(v: Decimal | float | int | str) -> str:
    return f"{Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}".replace(",", " ")


def pct(v: Decimal | float | int | str) -> str:
    x = Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(x, "f").rstrip("0").rstrip(".")


def parse_amount(text: str) -> Optional[Decimal]:
    try:
        x = Decimal(text.replace(",", ".").strip())
        if x < MIN_AMOUNT or x > MAX_AMOUNT:
            return None
        return x.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def fee_for(deal: Deal) -> Decimal:
    return (Decimal(deal.amount) * Decimal(deal.commission_percent) / Decimal("100")).quantize(Decimal("0.01"))


def add_balance(user_id: int, amount: Decimal, kind: str, ref: str = "") -> None:
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return
        u.balance = Decimal(u.balance) + amount
        if kind == "deposit":
            u.deposited = Decimal(u.deposited) + amount
        if kind == "withdraw":
            u.withdrawn = Decimal(u.withdrawn) + amount
        s.add(Ledger(user_id=user_id, kind=kind, amount=amount, reference=ref))
        s.commit()


def take_balance(user_id: int, amount: Decimal, kind: str, ref: str = "") -> bool:
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u or Decimal(u.balance) < amount:
            return False
        u.balance = Decimal(u.balance) - amount
        if kind == "withdraw":
            u.withdrawn = Decimal(u.withdrawn) + amount
        if kind == "withdraw_refund":
            u.withdrawn = max(Decimal("0"), Decimal(u.withdrawn) - amount)
        ledger_amount = amount if kind == "withdraw_refund" else -amount
        s.add(Ledger(user_id=user_id, kind=kind, amount=ledger_amount, reference=ref))
        s.commit()
        return True


# ============================================================
# PAYMENT API CLIENTS
# ============================================================
class CryptoPay:
    base = "https://pay.crypt.bot/api"

    def __init__(self, token: str):
        self.token = token

    async def call(self, method: str, data: dict | None = None):
        if not self.token or self.token.startswith("PASTE_"):
            raise RuntimeError("CRYPTO_PAY_TOKEN не задан")
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{self.base}/{method}", headers={"Crypto-Pay-API-Token": self.token}, json=data or {})
            r.raise_for_status()
            obj = r.json()
            if not obj.get("ok"):
                raise RuntimeError(str(obj.get("error")))
            return obj["result"]

    async def create_invoice(self, amount: Decimal, payload: str):
        return await self.call("createInvoice", {
            "asset": "USDT", "amount": str(amount), "description": "Пополнение кошелька Автогаранта",
            "payload": payload, "allow_comments": False, "allow_anonymous": False,
            "expires_in": 900,
        })

    async def get_invoice(self, invoice_id: int):
        return (await self.call("getInvoices", {"asset": "USDT", "invoice_ids": str(invoice_id), "count": 1}))[0]

    async def transfer(self, tg_id: int, amount: Decimal, spend_id: str):
        return await self.call("transfer", {
            "user_id": tg_id, "asset": "USDT", "amount": str(amount), "spend_id": spend_id,
            "comment": "Вывод из кошелька Автогаранта",
        })


class XRocket:
    base = "https://pay.api.xrocket.exchange/api/v1"

    def __init__(self, token: str):
        self.token = token

    async def call(self, method: str, path: str, data: dict | None = None, params: dict | None = None):
        if not self.token or self.token.startswith("PASTE_"):
            raise RuntimeError("XROCKET_API_TOKEN не задан")
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.request(method, f"{self.base}{path}", headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"}, json=data, params=params)
            if r.status_code >= 400:
                try:
                    body = r.json()
                except Exception:
                    body = r.text
                raise RuntimeError(f"xRocket HTTP {r.status_code}: {body}")
            return r.json()

    async def create_invoice(self, amount: Decimal, client_id: str, tg_id: int, username: Optional[str]):
        return await self.call("POST", "/invoices", {
            "priceAmount": str(amount), "priceCurrency": "USDT", "payoutCurrency": "USDT", "payCurrencies": ["USDT"],
            "clientInvoiceId": client_id, "description": "Пополнение кошелька Автогаранта", "expiresIn": 900000,
            "customer": {"telegramId": str(tg_id), "telegramUsername": (username or "").lstrip("@")},
            "data": {"telegram_id": tg_id},
        })

    async def get_invoice(self, invoice_id: str):
        return await self.call("GET", "/invoice", params={"invoiceId": invoice_id})

    async def payout(self, tg_id: int, amount: Decimal, client_id: str):
        return await self.call("POST", "/payouts", {
            "clientPayoutId": client_id, "target": str(tg_id), "targetType": "telegram_user_id",
            "asset": "USDT", "amount": str(amount), "description": "Вывод из кошелька Автогаранта",
        })


crypto = CryptoPay(CRYPTO_PAY_TOKEN)
xrocket = XRocket(XROCKET_API_TOKEN)


# ============================================================
# FSM
# ============================================================
class DealFSM(StatesGroup):
    role = State()
    amount = State()
    fee_share = State()
    terms = State()
    confirm = State()


class DepositFSM(StatesGroup):
    amount = State()
    custom_amount = State()


class WithdrawFSM(StatesGroup):
    provider = State()
    amount = State()


class AdminFSM(StatesGroup):
    commission = State()
    broadcast = State()
    user_search = State()


# ============================================================
# KEYBOARDS / UI
# ============================================================
def main_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text=f"➕ Создать сделку ({money(commission())[:-3]}%)")
    kb.button(text="👤 Мой профиль")
    kb.button(text="🆘 Поддержка")
    kb.button(text="📎 Дополнительно")
    kb.button(text="👑 Наши проекты")
    kb.adjust(1, 1, 1, 2)
    return kb.as_markup(resize_keyboard=True, is_persistent=True)


def back_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="⬅️ Назад")
    kb.button(text="🏠 Меню")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def inline_url(text: str, url: str):
    kb = InlineKeyboardBuilder()
    kb.button(text=text, url=url)
    return kb.as_markup()


def profile_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💼 Кошелёк", callback_data="wallet")
    kb.button(text="🤝 Мои сделки", callback_data="deals")
    kb.button(text="📄 Правила", url=RULES_URL)
    kb.button(text="↩ В меню", callback_data="menu")
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def wallet_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Пополнить", callback_data="deposit")
    kb.button(text="➖ Вывести", callback_data="withdraw")
    kb.button(text="↩ Назад", callback_data="profile")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(2, 2)
    return kb.as_markup()


def deposit_methods_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="CryptoPay", callback_data="dep:cryptopay")
    kb.button(text="xRocket", callback_data="dep:xrocket")
    kb.button(text="↩ Назад", callback_data="wallet")
    kb.adjust(2, 1)
    return kb.as_markup()


def amount_kb(provider: str):
    kb = InlineKeyboardBuilder()
    for a in [5, 10, 25, 50, 100]:
        kb.button(text=f"➕ {a} USDT", callback_data=f"depamt:{provider}:{a}")
    kb.button(text="✍️ Ввести сумму", callback_data=f"depcustom:{provider}")
    kb.button(text="↩ Назад", callback_data="deposit")
    kb.adjust(3, 2, 1, 1)
    return kb.as_markup()


def support_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🛠️ Технические вопросы", url=TECH_SUPPORT_URL)
    kb.button(text="⚠️ Важные вопросы", url=IMPORTANT_SUPPORT_URL)
    kb.button(text="↩ В меню", callback_data="menu")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def additional_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Пользовательское соглашение", url=db_get_setting("agreement_url", RULES_URL))
    kb.button(text="📄 Правила", url=RULES_URL)
    kb.button(text="❓ FAQ", url=db_get_setting("faq_url", RULES_URL))
    kb.button(text="⚖️ Арбитраж (как вызвать / как проходит)", url=ARBITRAGE_URL)
    kb.button(text="🛡️ Запрещённые сделки", url=FORBIDDEN_DEALS_URL)
    kb.button(text="⬅️ Главное меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def projects_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 BSG LOBBY", url=PROJECT_URL)
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def admin_kb():
    kb = InlineKeyboardBuilder()
    for t, d in [
        ("📊 Статистика", "adm:stats"), ("👥 Пользователи", "adm:users"),
        ("🤝 Сделки", "adm:deals"), ("💸 Выводы", "adm:withdrawals"),
        ("⚙️ Комиссия", "adm:commission"), ("📢 Рассылка", "adm:broadcast"),
        ("🔧 Тех. режим", "adm:maintenance"), ("🔗 Ссылки", "adm:links"),
    ]:
        kb.button(text=t, callback_data=d)
    kb.adjust(2, 2, 2, 2)
    return kb.as_markup()


# ============================================================
# TEXT
# ============================================================
def welcome_text(user: User) -> str:
    return (
        "<b>Добро пожаловать!</b>\n\n"
        "ℹ️ • Я помогу безопасно провести сделку между сторонами.\n"
        f"Текущая комиссия сервиса: {pct(commission())}%\n\n"
        f"Ваш ID: <code>{user.tg_id}</code>\n\n"
        "Выберите действие ниже ⬇️"
    )


def profile_text(u: User) -> str:
    username = f"@{u.username}" if u.username else "не указан"
    return (
        "<b>👤 Мой профиль</b>\n"
        f"🆔 ID: <code>{u.tg_id}</code>\n"
        f"👤 Юзернейм: {username}\n"
        f"💰 баланс: {money(Decimal(u.balance))} USDT\n"
        "⭐ Подписка: не активна\n\n"
        "ℹ️ Здесь вы можете управлять своим аккаунтом, пополнять баланс и следить за сделками."
    )


def wallet_text(u: User) -> str:
    return (
        "<b>💼 Кошелёк</b>\n"
        f"💰 Баланс: {money(Decimal(u.balance))} USDT\n"
        f"📥 Пополнено: {money(Decimal(u.deposited))} USDT\n"
        f"📤 Выведено: {money(Decimal(u.withdrawn))} USDT\n\n"
        "Пополняйте баланс через CryptoPay или xRocket в USDT и используйте средства для сделок."
    )


# ============================================================
# BOT
# ============================================================
bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())


async def ensure_user(message: Message) -> User:
    return db_get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)


async def check_allowed(message: Message) -> bool:
    u = await ensure_user(message)
    if u.is_banned:
        await message.answer("🚫 Ваш аккаунт заблокирован.")
        return False
    if maintenance() and message.from_user.id not in ADMIN_IDS:
        await message.answer("🔧 Бот временно находится на техническом обслуживании.")
        return False
    return True


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    u = await ensure_user(message)
    if u.is_banned:
        return await message.answer("🚫 Ваш аккаунт заблокирован.")
    args = (message.text or "").split(maxsplit=1)
    if len(args) == 2 and args[1].startswith("deal_"):
        token = args[1][5:]
        await join_deal(message, token)
        return
    await message.answer(welcome_text(u), reply_markup=main_kb())


@dp.message(Command("admin"))
async def admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("⛔ Нет доступа.")
    await message.answer("<b>👑 Админ-панель</b>\nВыберите раздел:", reply_markup=admin_kb())


@dp.message(F.text == "🏠 Меню")
async def menu(message: Message, state: FSMContext):
    await state.clear()
    if not await check_allowed(message): return
    u = await ensure_user(message)
    await message.answer(welcome_text(u), reply_markup=main_kb())


@dp.message(F.text.startswith("➕ Создать сделку"))
async def create_deal(message: Message, state: FSMContext):
    if not await check_allowed(message): return
    await state.clear()
    await state.set_state(DealFSM.role)
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Покупатель", callback_data="role:buyer")
    kb.button(text="💼 Продавец", callback_data="role:seller")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    await message.answer(
        "<b>🤝 Создание новой сделки | Шаг 1/6</b>\n\n"
        "Кем вы выступаете в данной сделке?\n\n"
        "🛒 <b>Покупатель</b> — если вы оплачиваете товар/услугу и ожидаете получение\n"
        "💼 <b>Продавец</b> — если вы передаёте товар/услугу и ожидаете выплату\n\n"
        "⬇️ Выберите свою роль кнопкой ниже:", reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data.startswith("role:"))
async def deal_role(c: CallbackQuery, state: FSMContext):
    role = c.data.split(":", 1)[1]
    await state.update_data(role=role)
    await state.set_state(DealFSM.amount)
    await c.message.edit_text(
        "<b>💰 Создание новой сделки | Шаг 2/6</b>\n\n"
        "Введите сумму сделки в USDT ($)\n"
        "Комиссия сервиса будет автоматически рассчитана и показана на следующем этапе.\n\n"
        "💡 Примеры ввода:\n"
        "• 100 — если сумма сделки сто долларов\n"
        "• 25.5 — если сумма сделки с центами (используйте точку)\n\n"
        "⌨️ Жду сумму сделки:", reply_markup=back_kb()
    )
    await c.answer()


@dp.message(DealFSM.amount)
async def deal_amount(message: Message, state: FSMContext):
    x = parse_amount(message.text or "")
    if x is None:
        return await message.answer(f"❌ Сумма должна быть от {money(MIN_AMOUNT)} до {money(MAX_AMOUNT)} USDT.\nПример: <code>25.50</code>")
    await state.update_data(amount=str(x))
    await state.set_state(DealFSM.fee_share)
    await message.answer(
        "<b>📜 Создание новой сделки | Шаг 3/6</b>\n\n"
        "<b>Комиссия сервиса</b>\n"
        f"Комиссия сервиса: {pct(commission())}%\n"
        "Выберите, какой процент от комиссии платите вы:\n\n"
        "Примеры:\n"
        "• Вы 0% — партнёр 100%\n"
        "• Вы 50% — партнёр 50%\n"
        "• Вы 100% — партнёр 0%"
    , reply_markup=fee_kb())


def fee_kb():
    kb = InlineKeyboardBuilder()
    for p in [0, 25, 50, 75, 100]:
        kb.button(text=f"{p}%", callback_data=f"feesplit:{p}")
    kb.button(text="⬅️ Назад", callback_data="dealback:amount")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(5, 2)
    return kb.as_markup()


@dp.callback_query(F.data.startswith("feesplit:"))
async def fee_split(c: CallbackQuery, state: FSMContext):
    p = Decimal(c.data.split(":", 1)[1])
    await state.update_data(creator_fee_share=str(p))
    await state.set_state(DealFSM.terms)
    await c.message.edit_text(
        "<b>📝 Создание новой сделки | Шаг 4/6</b>\n\n"
        "Введите условия и правила вашей сделки\n\n"
        "⚠️ Эти условия увидят обе стороны. В случае спора арбитраж опирается только на них.\n\n"
        "<b>Кликабельный шаблон (скопируйте и заполните одним сообщением):</b>\n"
        "1) Продавец обязуется:\n"
        "2) Покупатель обязуется:\n"
        "3) Предмет сделки:\n"
        "4) Сроки: продавец до __ / покупатель проверяет до __\n"
        "5) Сделка считается выполненной, если:\n"
        "6) Что считается нарушением/неисполнением:\n\n"
        "⬇️ Жду условия сделки", reply_markup=back_kb()
    )
    await c.answer()


@dp.message(DealFSM.terms)
async def deal_terms(message: Message, state: FSMContext):
    terms = (message.text or "").strip()
    if len(terms) < 1:
        return await message.answer("❌ Условия не могут быть пустыми.")
    await state.update_data(terms=terms)
    await state.set_state(DealFSM.confirm)
    d = await state.get_data()
    role = "Покупатель" if d["role"] == "buyer" else "Продавец"
    fee = Decimal(d["amount"]) * commission() / Decimal("100")
    await message.answer(
        "<b>🏁 Финальный этап | Шаг 5/6</b>\n\n"
        "Сверка данных и создание сделки\n"
        "Проверьте информацию. После подтверждения изменить условия будет нельзя.\n\n"
        "📋 <b>Карточка сделки:</b>\n"
        f"👤 Ваша роль: {role}\n"
        f"💰 Сумма сделки: {money(Decimal(d['amount']))} USDT\n"
        f"⚖️ Комиссия: {money(fee)} USDT ({pct(commission())}%)\n"
        f"⚖️ Распределение комиссии: вы {d['creator_fee_share']}% / партнёр {100 - Decimal(d['creator_fee_share'])}%\n"
        f"📝 Условия и правила:\n{terms}\n\n"
        "⚠️ Нажимая «✅ Подтвердить и отправить», вы подтверждаете корректность данных и согласие с правилами сервиса.\n\n"
        "✅ Всё верно?", reply_markup=confirm_deal_kb()
    )


def confirm_deal_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить и отправить", callback_data="deal:confirm")
    kb.button(text="⬅️ Назад", callback_data="dealback:terms")
    kb.adjust(1)
    return kb.as_markup()


@dp.callback_query(F.data == "deal:confirm")
async def confirm_deal(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d:
        return await c.answer("Сессия истекла. Создайте сделку заново.", show_alert=True)
    u = db_get_user(c.from_user.id, c.from_user.username, c.from_user.first_name)
    role = d["role"]
    amount = Decimal(d["amount"])
    token = secrets.token_urlsafe(12)
    with SessionLocal() as s:
        deal = Deal(token=token, creator_id=u.id, role=role, amount=amount,
                    commission_percent=commission(), creator_fee_share=Decimal(d["creator_fee_share"]),
                    terms=d["terms"], status="waiting_partner")
        if role == "buyer": deal.buyer_id = u.id
        else: deal.seller_id = u.id
        s.add(deal); s.commit(); s.refresh(deal)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=deal_{token}"
    await state.clear()
    await c.message.edit_text(
        "<b>🏁 Создание новой сделки | Шаг 6/6</b>\n\n"
        f"✅ Сделка <code>#{deal.id}</code> создана.\n\n"
        "Теперь пригласите вторую сторону. Она должна открыть ссылку и подтвердить участие.\n\n"
        f"🔗 Ссылка на сделку:\n{link}\n\n"
        "После подключения второй стороны бот покажет дальнейшие действия по оплате, подтверждению и завершению сделки.",
        reply_markup=InlineKeyboardBuilder().button(text="📤 Поделиться сделкой", url=f"https://t.me/share/url?url={link}").as_markup()
    )
    await c.answer()


async def join_deal(message: Message, token: str):
    u = await ensure_user(message)
    with SessionLocal() as s:
        deal = s.scalar(select(Deal).where(Deal.token == token))
        if not deal:
            return await message.answer("❌ Сделка не найдена.", reply_markup=main_kb())
        if deal.status not in {"waiting_partner"}:
            return await message.answer("❌ Эта сделка уже недоступна для присоединения.", reply_markup=main_kb())
        if deal.creator_id == u.id:
            return await message.answer("❌ Нельзя присоединиться к собственной сделке.")
        if deal.role == "buyer": deal.seller_id = u.id
        else: deal.buyer_id = u.id
        deal.status = "awaiting_funding"
        s.commit()
        role = "Покупатель" if deal.role == "seller" else "Продавец"
        await message.answer(
            "<b>🤝 Вы присоединились к сделке</b>\n\n"
            f"ID сделки: <code>#{deal.id}</code>\n"
            f"💰 Сумма: {money(Decimal(deal.amount))} USDT\n"
            f"👤 Ваша роль: {role}\n\n"
            f"📜 Условия:\n{deal.terms}\n\n"
            "⏱️ Ожидаем внесение средств покупателем. После зачисления обеим сторонам будет показан статус сделки.",
            reply_markup=main_kb()
        )
        # notify creator
        try:
            creator = s.get(User, deal.creator_id)
            if creator:
                await bot.send_message(creator.tg_id, f"🤝 Вторая сторона присоединилась к сделке <code>#{deal.id}</code>.\nОжидаем оплату покупателем.")
        except Exception:
            pass


@dp.callback_query(F.data == "cancel")
async def cancel(c: CallbackQuery, state: FSMContext):
    await state.clear(); await c.answer("Отменено")
    u = db_get_user(c.from_user.id, c.from_user.username, c.from_user.first_name)
    await c.message.edit_text(welcome_text(u))
    await c.message.answer("Главное меню:", reply_markup=main_kb())


# ============================================================
# PROFILE / WALLET
# ============================================================
@dp.message(F.text == "👤 Мой профиль")
async def profile(message: Message):
    if not await check_allowed(message): return
    u = await ensure_user(message)
    await message.answer(profile_text(u), reply_markup=profile_kb())


@dp.callback_query(F.data == "profile")
async def profile_cb(c: CallbackQuery):
    u = db_get_user(c.from_user.id, c.from_user.username, c.from_user.first_name)
    await c.message.edit_text(profile_text(u), reply_markup=profile_kb()); await c.answer()


@dp.callback_query(F.data == "wallet")
async def wallet(c: CallbackQuery):
    u = db_get_user(c.from_user.id, c.from_user.username, c.from_user.first_name)
    await c.message.edit_text(wallet_text(u), reply_markup=wallet_kb()); await c.answer()


@dp.callback_query(F.data == "deposit")
async def deposit(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("<b>➕ Пополнение</b>\n────────────\n\nВыберите способ пополнения.", reply_markup=deposit_methods_kb()); await c.answer()


@dp.callback_query(F.data.startswith("dep:"))
async def dep_provider(c: CallbackQuery, state: FSMContext):
    provider = c.data.split(":", 1)[1]
    await state.update_data(provider=provider)
    await state.set_state(DepositFSM.amount)
    await c.message.edit_text(
        "<b>➕ Пополнение</b>\n────────────\n\n"
        f"Способ: <b>{'CryptoPay' if provider == 'cryptopay' else 'xRocket'}</b>\n"
        "Выберите сумму в USDT или введите свою.\n"
        "Диапазон: 1 — 500000 USDT", reply_markup=amount_kb(provider)
    )
    await c.answer()


@dp.callback_query(F.data.startswith("depamt:"))
async def dep_amount_button(c: CallbackQuery, state: FSMContext):
    _, provider, raw = c.data.split(":")
    amount = Decimal(raw)
    await create_deposit(c.message, c.from_user, provider, amount, state)
    await c.answer()


@dp.callback_query(F.data.startswith("depcustom:"))
async def dep_custom(c: CallbackQuery, state: FSMContext):
    provider = c.data.split(":", 1)[1]
    await state.update_data(provider=provider)
    await state.set_state(DepositFSM.custom_amount)
    await c.message.edit_text(
        "<b>➕ Пополнение</b>\n────────────\n\n"
        f"Способ: <b>{'CryptoPay' if provider == 'cryptopay' else 'xRocket'}</b>\n"
        "✍️ Введите сумму пополнения в USDT\n\n"
        "Допустимо: 1 — 500000 USDT\nПример: 12 или 12.50\n\nОтправьте сообщение с суммой.",
        reply_markup=back_kb()
    )
    await c.answer()


@dp.message(DepositFSM.custom_amount)
async def dep_custom_amount(message: Message, state: FSMContext):
    amount = parse_amount(message.text or "")
    if amount is None:
        return await message.answer("❌ Некорректная сумма. Допустимо от 1 до 500000 USDT.")
    d = await state.get_data()
    await create_deposit(message, message.from_user, d["provider"], amount, state)


async def create_deposit(message: Message, tg_user, provider: str, amount: Decimal, state: FSMContext):
    await state.clear()
    u = db_get_user(tg_user.id, tg_user.username, tg_user.first_name)
    try:
        if provider == "cryptopay":
            inv = await crypto.create_invoice(amount, f"deposit:{u.tg_id}:{secrets.token_hex(8)}")
            external = str(inv["invoice_id"])
            pay_url = inv.get("pay_url") or inv.get("bot_invoice_url") or inv.get("mini_app_invoice_url")
            shown_id = external
        else:
            client_id = f"deposit_{u.tg_id}_{secrets.token_hex(8)}"
            # На скриншоте xRocket сервис начисляет 10 USDT, а к оплате выставляет 10.64 USDT (6%).
            gross = (amount * Decimal("1.06")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            inv = await xrocket.create_invoice(gross, client_id, u.tg_id, u.username)
            external = str(inv["id"])
            pay_url = inv.get("links", {}).get("telegramBotLink")
            shown_id = external
        with SessionLocal() as s:
            s.add(Payment(user_id=u.id, provider=provider, external_id=external, amount=amount, status="active")); s.commit()
        kb = InlineKeyboardBuilder()
        if pay_url:
            kb.button(text="💳 Оплатить", url=pay_url)
        kb.button(text="💼 В кошелёк", callback_data="wallet")
        kb.adjust(1)
        gross = (amount * Decimal("1.06")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if provider == "xrocket" else amount
        text = (
            "<b>➕ Пополнение</b>\n────────────\n\n"
            "🧾 Счёт создан\n\n"
            f"Способ: <b>{'CryptoPay' if provider == 'cryptopay' else 'xRocket'}</b>\n"
            f"🎯 Зачислим на баланс: <b>{money(amount)} USDT</b>\n"
            f"💳 К оплате{(' (с комиссией 6%)' if provider == 'xrocket' else '')}: <b>{money(gross)} USDT</b>\n"
            f"🧾 ID: <code>{shown_id}</code>\n\n"
            "Нажмите «Оплатить». После оплаты баланс обновится автоматически."
        )
        await message.answer(text, reply_markup=kb.as_markup())
    except Exception as e:
        log.exception("deposit create error")
        await message.answer(f"❌ Не удалось создать счёт. Проверьте API-ключ выбранного способа.\n\n<code>{str(e)[:500]}</code>")


@dp.callback_query(F.data == "withdraw")
async def withdraw(c: CallbackQuery, state: FSMContext):
    u = db_get_user(c.from_user.id, c.from_user.username, c.from_user.first_name)
    if Decimal(u.balance) <= 0:
        await c.answer("Баланс пуст.", show_alert=True); return
    kb = InlineKeyboardBuilder()
    kb.button(text="CryptoPay", callback_data="wd:cryptopay")
    kb.button(text="xRocket", callback_data="wd:xrocket")
    kb.button(text="↩ Назад", callback_data="wallet")
    kb.adjust(2, 1)
    await state.clear(); await state.set_state(WithdrawFSM.provider)
    await c.message.edit_text("<b>➖ Вывод</b>\n\nВыберите способ вывода USDT:", reply_markup=kb.as_markup()); await c.answer()


@dp.callback_query(F.data.startswith("wd:"))
async def withdraw_provider(c: CallbackQuery, state: FSMContext):
    provider = c.data.split(":", 1)[1]
    await state.update_data(provider=provider); await state.set_state(WithdrawFSM.amount)
    await c.message.edit_text(
        f"<b>➖ Вывод</b>\n\nСпособ: <b>{'CryptoPay' if provider == 'cryptopay' else 'xRocket'}</b>\n"
        "Введите сумму в USDT.\n\n"
        "⚠️ Для автоматического вывода пользователь должен быть доступен выбранному платёжному сервису."
    , reply_markup=back_kb()); await c.answer()


@dp.message(WithdrawFSM.amount)
async def withdraw_amount(message: Message, state: FSMContext):
    amount = parse_amount(message.text or "")
    if amount is None:
        return await message.answer("❌ Некорректная сумма.")
    u = db_get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if amount > Decimal(u.balance):
        return await message.answer(f"❌ Недостаточно средств. Баланс: {money(Decimal(u.balance))} USDT")
    d = await state.get_data(); provider = d["provider"]
    wd_id = secrets.token_hex(8)
    if not take_balance(u.id, amount, "withdraw", wd_id):
        return await message.answer("❌ Не удалось зарезервировать средства. Попробуйте ещё раз.")
    try:
        if provider == "cryptopay":
            result = await crypto.transfer(u.tg_id, amount, f"wd_{u.tg_id}_{wd_id}")
            external = str(result.get("transfer_id", result.get("id", "")))
            status = "paid"
        else:
            result = await xrocket.payout(u.tg_id, amount, f"wd_{u.tg_id}_{wd_id}")
            external = str(result.get("payoutId", ""))
            status = result.get("status", "pending")
        with SessionLocal() as s:
            s.add(Withdrawal(user_id=u.id, provider=provider, amount=amount, target=str(u.tg_id), external_id=external, status=status)); s.commit()
        await state.clear()
        await message.answer(f"✅ Вывод создан.\n\nСумма: <b>{money(amount)} USDT</b>\nСпособ: <b>{'CryptoPay' if provider == 'cryptopay' else 'xRocket'}</b>\nID операции: <code>{external}</code>", reply_markup=main_kb())
    except Exception as e:
        add_balance(u.id, amount, "withdraw_refund", wd_id)
        log.exception("withdraw error")
        await message.answer(f"❌ Вывод не выполнен, средства возвращены на баланс.\n\n<code>{str(e)[:500]}</code>")


# ============================================================
# SUPPORT / ADDITIONAL / PROJECTS / DEALS
# ============================================================
@dp.message(F.text == "🆘 Поддержка")
async def support(message: Message):
    if not await check_allowed(message): return
    await message.answer("<b>🆘 Поддержка</b>\n\nВыберите, с каким вопросом вы обращаетесь:\n\n🛠️ Технические вопросы — проблемы с ботом, ошибки, баги\n⚠️ Важные вопросы — сделки, арбитраж, спорные ситуации", reply_markup=support_kb())


@dp.message(F.text == "📎 Дополнительно")
async def additional(message: Message):
    if not await check_allowed(message): return
    await message.answer("<b>⭐ Дополнительно</b>", reply_markup=additional_kb())


@dp.message(F.text == "👑 Наши проекты")
async def projects(message: Message):
    if not await check_allowed(message): return
    await message.answer("<b>🧩 BSG PORTAL</b>\nНебольшое пространство для наших проектов.\nЖми кнопку ниже 👇", reply_markup=projects_kb())


@dp.callback_query(F.data == "menu")
async def menu_cb(c: CallbackQuery):
    u = db_get_user(c.from_user.id, c.from_user.username, c.from_user.first_name)
    await c.message.edit_text(welcome_text(u)); await c.message.answer("Выберите действие:", reply_markup=main_kb()); await c.answer()


@dp.callback_query(F.data == "deals")
async def deals(c: CallbackQuery):
    u = db_get_user(c.from_user.id, c.from_user.username, c.from_user.first_name)
    with SessionLocal() as s:
        rows = s.scalars(select(Deal).where((Deal.creator_id == u.id) | (Deal.buyer_id == u.id) | (Deal.seller_id == u.id)).order_by(Deal.id.desc()).limit(20)).all()
    if not rows:
        text = "<b>📂 Мои сделки</b>\n\nПока что у вас нет сделок.\nСоздайте первую сделку в меню — и она появится здесь."
    else:
        lines = ["<b>📂 Мои сделки</b>"]
        for d in rows:
            lines.append(f"\n🤝 <code>#{d.id}</code> — {money(Decimal(d.amount))} USDT — <b>{d.status}</b>")
        text = "".join(lines)
    kb = InlineKeyboardBuilder(); kb.button(text="👤 В профиль", callback_data="profile")
    await c.message.edit_text(text, reply_markup=kb.as_markup()); await c.answer()


# ============================================================
# ADMIN PANEL
# ============================================================
def admin_only(c: CallbackQuery) -> bool:
    return c.from_user.id in ADMIN_IDS


@dp.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    if not admin_only(c): return await c.answer("Нет доступа", show_alert=True)
    with SessionLocal() as s:
        users = s.scalar(select(func.count(User.id))) or 0
        deals = s.scalar(select(func.count(Deal.id))) or 0
        active = s.scalar(select(func.count(Deal.id)).where(Deal.status.not_in(["completed", "cancelled"]))) or 0
        balance = s.scalar(select(func.coalesce(func.sum(User.balance), 0))) or 0
    await c.message.edit_text(f"<b>📊 Статистика</b>\n\n👥 Пользователей: {users}\n🤝 Сделок: {deals}\n🟢 Активных сделок: {active}\n💰 Балансы пользователей: {money(balance)} USDT", reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data == "adm:users")
async def adm_users(c: CallbackQuery):
    if not admin_only(c): return await c.answer("Нет доступа", show_alert=True)
    with SessionLocal() as s:
        rows = s.scalars(select(User).order_by(User.id.desc()).limit(15)).all()
    text = "<b>👥 Последние пользователи</b>\n\n" + "\n".join([f"<code>{u.tg_id}</code> @{u.username or '-'} — {money(Decimal(u.balance))} USDT" for u in rows])
    await c.message.edit_text(text, reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data == "adm:deals")
async def adm_deals(c: CallbackQuery):
    if not admin_only(c): return await c.answer("Нет доступа", show_alert=True)
    with SessionLocal() as s:
        rows = s.scalars(select(Deal).order_by(Deal.id.desc()).limit(15)).all()
    text = "<b>🤝 Последние сделки</b>\n\n" + "\n".join([f"<code>#{d.id}</code> {money(Decimal(d.amount))} USDT — {d.status}" for d in rows])
    await c.message.edit_text(text or "Сделок пока нет.", reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data == "adm:withdrawals")
async def adm_withdrawals(c: CallbackQuery):
    if not admin_only(c): return await c.answer("Нет доступа", show_alert=True)
    with SessionLocal() as s:
        rows = s.scalars(select(Withdrawal).order_by(Withdrawal.id.desc()).limit(15)).all()
    text = "<b>💸 Выводы</b>\n\n" + "\n".join([f"<code>#{w.id}</code> {money(Decimal(w.amount))} USDT — {w.provider} — {w.status}" for w in rows])
    await c.message.edit_text(text or "Выводов пока нет.", reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data == "adm:commission")
async def adm_commission(c: CallbackQuery, state: FSMContext):
    if not admin_only(c): return await c.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminFSM.commission)
    await c.message.edit_text(f"<b>⚙️ Комиссия</b>\n\nТекущая: {money(commission())[:-3]}%\n\nВведите новое значение, например <code>3</code>:")
    await c.answer()


@dp.message(AdminFSM.commission)
async def adm_set_commission(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    try:
        x = Decimal((message.text or "").replace(",", "."))
        if x < 0 or x > 100: raise ValueError
    except Exception:
        return await message.answer("❌ Введите число от 0 до 100.")
    db_set_setting("commission", str(x)); await state.clear()
    await message.answer(f"✅ Комиссия изменена: {pct(x)}%", reply_markup=admin_kb())


@dp.callback_query(F.data == "adm:maintenance")
async def adm_maintenance(c: CallbackQuery):
    if not admin_only(c): return await c.answer("Нет доступа", show_alert=True)
    new = not maintenance(); db_set_setting("maintenance", "1" if new else "0")
    await c.message.edit_text(f"🔧 Технический режим: <b>{'ВКЛ' if new else 'ВЫКЛ'}</b>", reply_markup=admin_kb()); await c.answer()


@dp.callback_query(F.data == "adm:broadcast")
async def adm_broadcast(c: CallbackQuery, state: FSMContext):
    if not admin_only(c): return await c.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminFSM.broadcast)
    await c.message.edit_text("<b>📢 Рассылка</b>\n\nОтправьте текст рассылки одним сообщением:")
    await c.answer()


@dp.message(AdminFSM.broadcast)
async def adm_do_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    with SessionLocal() as s:
        ids = [x for x in s.scalars(select(User.tg_id)).all()]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, message.text or "")
            ok += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await state.clear(); await message.answer(f"✅ Рассылка завершена. Доставлено: {ok}/{len(ids)}", reply_markup=admin_kb())


@dp.callback_query(F.data == "adm:links")
async def adm_links(c: CallbackQuery):
    if not admin_only(c): return await c.answer("Нет доступа", show_alert=True)
    await c.message.edit_text(
        "<b>🔗 Ссылки</b>\n\n"
        f"Правила: {RULES_URL}\nТех. поддержка: {TECH_SUPPORT_URL}\nВажные вопросы: {IMPORTANT_SUPPORT_URL}\nПроекты: {PROJECT_URL}",
        reply_markup=admin_kb()
    ); await c.answer()


# ============================================================
# PAYMENT RECONCILIATION — НЕ ДОВЕРЯЕМ КНОПКЕ «ОПЛАЧЕНО»,
# подтверждаем только фактический статус API.
# ============================================================
async def payment_worker():
    while True:
        try:
            with SessionLocal() as s:
                pending = s.scalars(select(Payment).where(Payment.status == "active").order_by(Payment.id).limit(30)).all()
            for p in pending:
                try:
                    if p.provider == "cryptopay":
                        inv = await crypto.get_invoice(int(p.external_id))
                        paid = inv.get("status") == "paid"
                    else:
                        inv = await xrocket.get_invoice(p.external_id)
                        paid = inv.get("status") == "paid"
                    if paid:
                        with SessionLocal() as s:
                            row = s.get(Payment, p.id)
                            if row and row.status == "active":
                                row.status = "paid"; row.paid_at = datetime.now(timezone.utc); s.commit()
                                u = s.get(User, row.user_id)
                                if u:
                                    u.balance = Decimal(u.balance) + Decimal(row.amount)
                                    u.deposited = Decimal(u.deposited) + Decimal(row.amount)
                                    s.add(Ledger(user_id=u.id, kind="deposit", amount=Decimal(row.amount), reference=f"payment:{row.id}")); s.commit()
                                    try:
                                        await bot.send_message(u.tg_id, f"✅ Пополнение подтверждено.\nНа баланс зачислено <b>{money(Decimal(row.amount))} USDT</b>.\n\n💰 Текущий баланс: <b>{money(Decimal(u.balance))} USDT</b>")
                                    except Exception:
                                        pass
                except Exception as e:
                    log.warning("payment check %s/%s: %s", p.provider, p.external_id, e)
                await asyncio.sleep(0.2)
        except Exception:
            log.exception("payment worker")
        await asyncio.sleep(15)


# ============================================================
# SIMPLE HEALTH ENDPOINT FOR RENDER
# ============================================================
app = FastAPI()

@app.get("/")
async def root():
    return {"ok": True, "service": "telegram-escrow-bot"}

@app.get("/health")
async def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


async def run_bot():
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_"):
        raise RuntimeError("BOT_TOKEN не задан. Укажите токен в Environment Variables Render или в BOT_TOKEN в коде.")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(payment_worker())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    # Render Web Service: запускайте uvicorn, а бот — в отдельной задаче.
    # Для локального запуска достаточно: python bot.py
    import threading
    import uvicorn

    def serve():
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

    threading.Thread(target=serve, daemon=True).start()
    asyncio.run(run_bot())
