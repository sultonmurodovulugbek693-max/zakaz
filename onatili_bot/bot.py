"""
Ona tili test boti — Telegram Mini App uchun "eshik".

Hamma ish ilovada (webapp/index.html): test yaratish, javob berish, natijalar,
esse baholash. Bot faqat:
  • /start — salomlashish va ilovani ochish tugmasi («Kirish» menyu tugmasi ham)
  • majburiy obuna (admin bot ichida /admin orqali kanal qo'shadi)
  • xabarnomalar: o'qituvchiga yangi javob, o'quvchiga e'lon qilingan natija, PDF fayllar
  • fonda AI esse tekshiruvi

Ishga tushirish:  python bot.py
"""

import asyncio
import base64
import html
import logging
import os
import secrets
import time

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardRemove,
    WebAppInfo,
)

import db
import excel
import essay_ai
import ops
import places as places_data
import plans
import pdfs
import sheet
import subscription
import webapp
import config
from config import ADMIN_IDS, BOT_TOKEN, WEBAPP_URL
from scoring import rasch_results, total_points
from structure import ESSAY_MAX, ESSAY_SCALE, choice_items, essay_to_75, fmt_num, grade, max_points, percent, text_items

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("onatili_bot")

# 1 — AI bahosi o'qituvchi tasdiqlamasa ham darhol qo'llanadi; 0 — o'qituvchi tasdiqlaydi (tavsiya)
AUTO_CONFIRM_ESSAYS = os.getenv("ESSAY_AUTO_CONFIRM", "0") == "1"

dp = Dispatcher()
dp.message.filter(F.chat.type == "private")
LINE = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"


# ---------- Yordamchilar ----------

def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows]
    )


def app_url(**params) -> str | None:
    """Mini App manzili (https bo'lmasa — None: Telegram faqat https qabul qiladi)."""
    if not WEBAPP_URL.startswith("https://"):
        return None
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{WEBAPP_URL}/app" + (f"?{query}" if query else "")


def app_markup(text: str = "✨ Ilovaga kirish", **params) -> InlineKeyboardMarkup | None:
    url = app_url(**params)
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]])


async def safe_edit(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as error:
        if "not modified" not in str(error):
            await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ---------- Salomlashish ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    await asyncio.to_thread(db.remember_user, user.id, user.full_name, user.username)
    args = (message.text or "").split(maxsplit=1)
    if len(args) == 2 and args[1].startswith("app_"):
        await confirm_app_login(message, args[1][4:])
        return
    if len(args) == 2 and args[1] == "register":
        await send_register_prompt(message)
        return
    await send_welcome(message, user)


async def send_register_prompt(message: Message) -> None:
    """Oflayn «Ona tili» kursiga ro'yxatdan o'tish — ilovaning alohida ekranida."""
    registered = await asyncio.to_thread(db.get_registration, message.from_user.id)
    markup = app_markup("📝 Ro'yxatdan o'tish", v="register")
    if not markup:
        await message.answer("⚠️ Ilova hali sozlanmagan (WEBAPP_URL).")
        return
    await message.answer(
        ("✅ <b>Siz ro'yxatdan o'tgansiz</b>\nMa'lumotlaringizni o'zgartirmoqchi bo'lsangiz — tugmani bosing 👇"
         if registered else
         "📝 <b>Oflayn «Ona tili» kursi</b>\n"
         "Avval kim ekanligingizni tanlaysiz:\n"
         "🧑‍🎓 <b>O'quvchi</b> — yashash joyingizni (viloyat, tuman) tanlaysiz, arizangiz o'sha tumandagi "
         "o'qituvchilarga boradi\n"
         "👩‍🏫 <b>O'qituvchi</b> — dars beradigan tumanlaringizni belgilaysiz, o'quvchilar sizga keladi 👇"),
        reply_markup=markup,
    )


async def confirm_app_login(message: Message, token: str) -> None:
    """APK ilovadagi «Telegram orqali kirish»: shu Telegram akkaunt bilan ilovaga kiriladi."""
    session = secrets.token_hex(24)
    confirmed = await asyncio.to_thread(db.confirm_login, token, message.from_user.id, session)
    if confirmed:
        await message.answer("✅ <b>Ilovaga kirdingiz</b>\nSkaner ilovasiga qayting — testlaringiz o'sha yerda.\n\n"
                             "<i>Agar siz kirmagan bo'lsangiz, bu xabarga e'tibor bermang.</i>")
    else:
        await message.answer("⌛️ Kirish havolasi eskirgan yoki ishlatilgan. Ilovada «Telegram orqali kirish»ni qayta bosing.")


async def send_welcome(message: Message, user) -> None:
    # Salomlashish xabarining o'zi eski versiyadagi pastki tugmalarni (klaviatura) olib tashlaydi.
    # Alohida xabar bilan olib tashlab, uni o'chirib bo'lmaydi — Telegram eski tugmalarni qaytarib ko'rsatadi
    await message.answer(
        "📚 <b>Ona tili · Test</b>\n"
        f"{LINE}\n"
        f"Assalomu alaykum, {esc(user.first_name)}! 👋\n\n"
        "Milliy sertifikat uslubidagi mock testlar ilovasi:\n"
        "🧑‍🎓 <b>O'quvchi</b> — kodni kiritadi, javoblarni belgilaydi, natija kartasini oladi\n"
        "👩‍🏫 <b>O'qituvchi</b> — test yaratadi, natijalar va esselarni kuzatadi, e'lon qiladi",
        reply_markup=ReplyKeyboardRemove(),
    )
    if not app_url():
        await message.answer("⚠️ Ilova hali sozlanmagan (WEBAPP_URL).")
        return
    registered = await asyncio.to_thread(db.get_registration, user.id)
    rows = [[InlineKeyboardButton(text="✨ Ilovaga kirish", web_app=WebAppInfo(url=app_url()))]]
    rows.append([InlineKeyboardButton(
        text="✅ Ma'lumotlarimni o'zgartirish" if registered else "📝 Oflayn kursga ro'yxatdan o'tish",
        web_app=WebAppInfo(url=app_url(v="register")))])
    await message.answer(
        "Boshlash uchun tugmani bosing 👇"
        + ("" if registered else "\n\n📝 Oflayn <b>«Ona tili»</b> kursimizga yozilmoqchi bo'lsangiz — "
                                 "pastdagi ikkinchi tugmani bosing."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    """Admin: baza nusxasini hozir olish (har kuni 03:00 da o'zi ham keladi)."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Bu buyruq faqat admin uchun.")
        return
    note = await message.answer("💾 Nusxa tayyorlanmoqda…")
    sent = await ops.send_backup(message.bot, [message.chat.id])
    await note.edit_text("✅ Nusxa yuborildi" if sent else "❌ Nusxani yuborib bo'lmadi — Render → Logs")


# ---------- Obuna (admin) ----------

def _who(user: dict) -> str:
    name = esc(user.get("full_name") or "") or "—"
    username = f" (@{esc(user['username'])})" if user.get("username") else ""
    return f"{name}{username} · <code>{user['tg_id']}</code>"


@dp.message(Command("obuna"))
async def cmd_grant(message: Message, command: CommandObject) -> None:
    """Admin: /obuna <@username yoki ID> <muddat|paket> [nechta] — obunani yoqish/uzaytirish yoki esse paketi."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Bu buyruq faqat admin uchun.")
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Ishlatish: <code>/obuna @username oy 1</code>  (muddat, nechta)\n"
                             "Muddatlar: <code>hafta</code>, <code>oy</code>, <code>3oy</code>, <code>yil</code>\n"
                             "Esse paketi: <code>/obuna @username paket 1</code>\n"
                             "O'qituvchi oldin botga /start bosgan bo'lsin (yoki ID'sini yozing).\n\n"
                             f"Narxlar:\n{plans.price_list()}")
        return
    count = int(args[2]) if len(args) > 2 and args[2].isdigit() else 1
    user = await asyncio.to_thread(db.find_user, args[0])
    if not user:
        await message.answer("Bunday foydalanuvchi topilmadi — u botga /start bosgan bo'lishi kerak "
                             "(yoki Telegram ID'sini yozing: o'qituvchi /myid bilan biladi).")
        return
    if args[1].lower() in ("paket", "pack"):
        essays = (await asyncio.to_thread(plans.pack))["essays"] * max(1, min(20, count))
        balance = await asyncio.to_thread(plans.add_pack, user["tg_id"], essays)
        await message.answer(f"✅ {_who(user)}\n📦 +{essays} esse · jami qo'shimcha: {balance} ta")
        await notify_pack(message.bot, user["tg_id"], essays, balance)
        return
    try:
        status = await asyncio.to_thread(plans.grant, user["tg_id"], args[1].lower(), count)
    except ValueError as error:
        await message.answer(f"❌ {esc(str(error))}")
        return
    await message.answer(f"✅ {_who(user)}\n💎 <b>{status['name']}</b> · {status['expires']} gacha")
    await notify_granted(message.bot, user["tg_id"], status)


async def notify_granted(bot: Bot, tg_id: int, status: dict) -> None:
    limits = status.get("limits") or {}
    ai = f"\n🤖 AI oyiga {limits['essays']} ta esseni tekshiradi." if limits else ""
    try:
        await bot.send_message(
            tg_id, f"🎉 <b>Obunangiz faollashdi</b>: {status['name']} · {status['expires']} gacha{ai}",
            reply_markup=app_markup())
    except Exception:
        logger.warning("O'qituvchiga obuna xabari yuborilmadi")


async def notify_pack(bot: Bot, tg_id: int, essays: int, balance: int) -> None:
    try:
        await bot.send_message(
            tg_id, f"📦 <b>Esse paketi qo'shildi</b>: +{essays} ta\n"
                   f"Jami qo'shimcha: {balance} ta esse. Oylik limit tugaganda AI shulardan foydalanadi — "
                   "muddati yo'q, keyingi oyga ham o'tadi.", reply_markup=app_markup())
    except Exception:
        logger.warning("O'qituvchiga paket xabari yuborilmadi")


@dp.message(Command("obunalar"))
async def cmd_subscriptions(message: Message) -> None:
    """Admin: barcha o'qituvchilar — tarif, muddat, shu oydagi AI sarfi."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Bu buyruq faqat admin uchun.")
        return
    subs = await asyncio.to_thread(db.list_subscriptions)
    if not subs:
        await message.answer("Hali obunachi yo'q.")
        return
    lines = [f"👩‍🏫 <b>O'qituvchilar</b>: {len(subs)} ta\n"]
    for sub in subs:
        st = await asyncio.to_thread(plans.status, sub["tg_id"])
        mark = "🟢" if st["active"] else "🔴"
        days = f"{st['days_left']} kun" if st["active"] else "tugagan"
        if st["unlimited"]:
            lines.append(f"♾ {_who(sub)}\n    {st['name']} · cheksiz")
            continue
        lines.append(f"{mark} {_who(sub)}\n    {st['name']} · {days} · esse {st['used']['essays']}/{st['limits']['essays']}"
                     + (f" · 📦 {st['credits']}" if st["credits"] else ""))
    text, chunk = "\n".join(lines), ""
    for line in text.split("\n"):
        if len(chunk) + len(line) > 3800:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    await message.answer(chunk)


@dp.message(Command("tariflar"))
async def cmd_prices(message: Message) -> None:
    if plans.is_free(message.from_user.id):
        await message.answer("♾ <b>Sizga hammasi bepul</b> — obuna sotib olish shart emas.")
        return
    trial = await asyncio.to_thread(plans.trial_days)
    contact = f"\n\nSavol bo'lsa: @{esc(config.ADMIN_CONTACT)}" if config.ADMIN_CONTACT else ""
    prices = await asyncio.to_thread(plans.price_list)
    limits, offer = await asyncio.to_thread(plans.limits), await asyncio.to_thread(plans.pack)
    await message.answer(f"💎 <b>Obuna narxlari</b>\n{prices}\n\n"
                         f"🤖 Obunada AI oyiga {limits['essays']} ta esseni tekshiradi.\n"
                         f"📦 Qo'shimcha: +{offer['essays']} esse — {plans.money(offer['price'])} so'm\n\n"
                         + (f"Yangi o'qituvchiga {trial} kunlik bepul sinov.\n" if trial else "")
                         + "Sotib olish: ilovadagi «💎 Obuna» tugmasi — kartaga o'tkazasiz, chekni shu yerga "
                           "yuborasiz, admin tasdiqlaydi." + contact,
                         reply_markup=app_markup())


@dp.message(Command("royxat"))
async def cmd_register(message: Message, state: FSMContext) -> None:
    """Oflayn «Ona tili» kursiga ro'yxatdan o'tish tugmasi."""
    await state.clear()
    await send_register_prompt(message)


@dp.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"🆔 Telegram ID'ingiz: <code>{message.from_user.id}</code>\n"
                         "Admin bo'lish uchun shu raqamni Render'da ADMIN_IDS o'zgaruvchisiga yozing.")


# ---------- Obuna to'lovi: karta → chek → admin tasdiqlaydi ----------

# StateFilter(None): admin biror narsa kiritayotgan bo'lsa (kanal qo'shish, narx) — xabar o'shanga ketsin
@dp.message(StateFilter(None), F.photo | F.document)
async def receipt_received(message: Message) -> None:
    """Chek rasmi: ochiq to'lov so'rovi bo'lsa — adminlarga tasdiqlash uchun yuboriladi."""
    payment = await asyncio.to_thread(db.open_payment, message.from_user.id)
    if not payment:
        await message.answer("🧾 Chek qabul qilinmadi: avval ilovadagi «💎 Obuna» tugmasidan muddatni tanlang.",
                             reply_markup=app_markup("💎 Obuna"))
        return
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    await asyncio.to_thread(db.attach_receipt, payment["id"], file_id)
    await message.answer(f"✅ Chek qabul qilindi · {plans.payment_title(payment)} — "
                         f"{plans.money(payment['amount'])} so'm\nAdmin tekshirib, "
                         + ("esselarni qo'shadi." if payment["period"] == plans.PACK else "obunangizni yoqadi."))
    await send_receipt_to_admins(message.bot, payment["id"], bool(message.photo))


async def send_receipt_to_admins(bot: Bot, payment_id: int, as_photo: bool) -> None:
    payment = await asyncio.to_thread(db.get_payment, payment_id)
    if not payment or not payment["receipt"]:
        return
    caption = (f"🧾 <b>Yangi to'lov</b> · {plans.payment_title(payment)} — "
               f"<b>{plans.money(payment['amount'])} so'm</b>\n{LINE}\n{_who(payment)}")
    markup = inline([[("✅ Tasdiqlash", f"pay:ok:{payment_id}"), ("❌ Rad etish", f"pay:no:{payment_id}")]])
    for admin_id in ADMIN_IDS:
        try:
            if as_photo:
                await bot.send_photo(admin_id, payment["receipt"], caption=caption, reply_markup=markup)
            else:
                await bot.send_document(admin_id, payment["receipt"], caption=caption, reply_markup=markup)
        except Exception:
            logger.warning("Chek adminga yuborilmadi: %s", admin_id, exc_info=True)


@dp.callback_query(F.data.startswith("pay:"))
async def decide_payment(callback: CallbackQuery) -> None:
    """Admin chekni tasdiqlaydi yoki rad etadi. Tasdiqlansa — obuna darhol yoqiladi (yoki esse paketi qo'shiladi)."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Faqat admin uchun", show_alert=True)
        return
    _, action, raw = callback.data.split(":")
    payment = await asyncio.to_thread(db.get_payment, int(raw))
    if not payment:
        await callback.answer("To'lov topilmadi", show_alert=True)
        return
    decided = await asyncio.to_thread(db.decide_payment, payment["id"], "paid" if action == "ok" else "rejected",
                                      callback.from_user.id)
    if not decided:
        await callback.answer("Bu to'lov allaqachon ko'rib chiqilgan", show_alert=True)
        return
    if action == "ok" and payment["period"] == plans.PACK:
        essays = payment["essays"] or (await asyncio.to_thread(plans.pack))["essays"]
        balance = await asyncio.to_thread(plans.add_pack, payment["tg_id"], essays)
        await callback.answer("✅ Esselar qo'shildi")
        await notify_pack(callback.bot, payment["tg_id"], essays, balance)
        note = f"✅ Tasdiqladi: {esc(callback.from_user.full_name)} — +{essays} esse (jami {balance})"
    elif action == "ok":
        status = await asyncio.to_thread(plans.grant, payment["tg_id"], payment["period"])
        await callback.answer("✅ Obuna yoqildi")
        await notify_granted(callback.bot, payment["tg_id"], status)
        note = f"✅ Tasdiqladi: {esc(callback.from_user.full_name)} — {status['name']} · {status['expires']} gacha"
    else:
        await callback.answer("❌ Rad etildi")
        try:
            await callback.bot.send_message(
                payment["tg_id"],
                "❌ <b>To'lovingiz tasdiqlanmadi</b>\nChek noto'g'ri yoki summa mos kelmagan bo'lishi mumkin. "
                "Ilovada qaytadan urinib ko'ring"
                + (f" yoki @{esc(config.ADMIN_CONTACT)} ga yozing." if config.ADMIN_CONTACT else "."))
        except Exception:
            logger.warning("Rad etish xabari yuborilmadi")
        note = f"❌ Rad etdi: {esc(callback.from_user.full_name)}"
    try:
        await callback.message.edit_caption(caption=f"{callback.message.caption or ''}\n{LINE}\n{note}")
    except TelegramBadRequest:
        pass


@dp.message(Command("arizalar"))
async def cmd_registrations(message: Message) -> None:
    """Admin: oflayn kursga yozilganlar ro'yxati."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Bu buyruq faqat admin uchun.")
        return
    regs = await asyncio.to_thread(db.list_registrations, 50)
    if not regs:
        await message.answer("Hali hech kim ro'yxatdan o'tmagan.")
        return
    total = await asyncio.to_thread(db.count_registrations)
    lines = [f"📝 <b>Oflayn kursga yozilganlar</b>: {total} ta\n"]
    for reg in regs:
        who = f"@{esc(reg['username'])}" if reg.get("username") else f"<code>{reg['tg_id']}</code>"
        mark = "👩‍🏫" if reg.get("role") == "teacher" else "🧑‍🎓"
        where = places_data.label(reg.get("region") or "", reg.get("district") or "")
        lines.append(f"{mark} <b>{esc(reg['full_name'])}</b>"
                     + (f" · {reg['age']} yosh" if reg.get("age") else "") + f" · {who}\n"
                     f"    📞 {esc(reg['phone'])}"
                     + (f" · 📍 {esc(where)}" if where else "")
                     + f" · {reg['created_at']}")
    chunk = ""
    for line in "\n".join(lines).split("\n"):
        if len(chunk) + len(line) > 3800:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    await message.answer(chunk)


@dp.message(Command("tolovlar"))
async def cmd_payments(message: Message) -> None:
    """Admin: oxirgi to'lovlar."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Bu buyruq faqat admin uchun.")
        return
    payments = await asyncio.to_thread(db.list_payments, None, 20)
    if not payments:
        await message.answer("Hali to'lov yo'q.")
        return
    marks = {"paid": "✅", "rejected": "❌", "sent": "🧾", "pending": "⏳", "canceled": "🚫"}
    lines = [f"💳 <b>Oxirgi to'lovlar</b>: {len(payments)} ta\n"]
    for pay in payments:
        lines.append(f"{marks.get(pay['status'], '•')} {_who(pay)}\n"
                     f"    {plans.payment_title(pay)} · {plans.money(pay['amount'])} so'm · {pay['created_at']}")
    await message.answer("\n".join(lines)[:3900])


# ---------- Majburiy obuna ----------

class SubscriptionGate(BaseMiddleware):
    """Obuna bo'lmagan foydalanuvchining har qanday xabari/tugmasi o'rniga — obuna so'rovi."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.is_bot or user.id in ADMIN_IDS:
            return await handler(event, data)
        if isinstance(event, Message) and event.chat.type != "private":
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "checksub":
            return await handler(event, data)
        if isinstance(event, Message) and event.text and event.text.startswith("/myid"):
            return await handler(event, data)
        missing = await subscription.missing_channels(data["bot"], user.id)
        if not missing:
            return await handler(event, data)
        if isinstance(event, Message):
            await asyncio.to_thread(db.remember_user, user.id, user.full_name, user.username)
            await show_gate(event, missing)
        else:
            await event.answer("Avval kanallarga obuna bo'ling 👇", show_alert=True)
            await show_gate(event.message, missing)
        return None


def gate_markup(missing: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📢 {c['title'][:40]}", url=c["url"])] for c in missing]
    rows.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="checksub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_gate(message: Message, missing: list[dict]) -> None:
    await message.answer(
        "🔒 <b>Ilovadan foydalanish uchun kanallarga obuna bo'ling</b>\n"
        f"{LINE}\n"
        + "\n".join(f"• {esc(c['title'])}" for c in missing)
        + "\n\nObuna bo'lgach, «✅ Obuna bo'ldim» tugmasini bosing.",
        reply_markup=gate_markup(missing),
    )


@dp.callback_query(F.data == "checksub")
async def check_subscription(callback: CallbackQuery, state: FSMContext) -> None:
    subscription.forget(callback.from_user.id)
    missing = await subscription.missing_channels(callback.bot, callback.from_user.id)
    if missing:
        await callback.answer("Hali obuna bo'lmagansiz: " + ", ".join(c["title"] for c in missing)[:180], show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=gate_markup(missing))
        except TelegramBadRequest:
            pass
        return
    await callback.answer("✅ Rahmat! Obuna tasdiqlandi")
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await state.clear()
    await send_welcome(callback.message, callback.from_user)


# ---------- Admin paneli ----------

class Admin(StatesGroup):
    add_channel = State()
    edit_setting = State()


def is_admin(event) -> bool:
    return event.from_user.id in ADMIN_IDS


async def admin_panel() -> tuple[str, InlineKeyboardMarkup]:
    channels = await asyncio.to_thread(db.list_channels)
    numbers = await asyncio.to_thread(db.stats)
    lines = [
        "🛠 <b>Admin paneli</b>", LINE,
        f"👥 Foydalanuvchilar: <b>{numbers['users']}</b> · 📝 testlar: <b>{numbers['tests']}</b> · "
        f"✅ topshirilgan: <b>{numbers['attempts']}</b>", "",
        "📢 <b>Majburiy obuna kanallari</b>",
    ]
    lines += [f"{i}. <a href=\"{esc(c['url'])}\">{esc(c['title'])}</a>" for i, c in enumerate(channels, 1)] or \
        ["<i>Hozircha kanal yo'q — obuna talab qilinmaydi.</i>"]
    rows = [[(f"❌ {c['title'][:30]}", f"chdel:{c['chat_id']}")] for c in channels]
    rows.append([("➕ Kanal qo'shish", "chadd")])
    rows.append([("💎 Obuna sozlamalari", "settings")])
    rows.append([("🔄 Yangilash", "admin")])
    return "\n".join(lines), inline(rows)


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("⛔️ Bu bo'lim faqat admin uchun.")
        return
    await state.clear()
    text, markup = await admin_panel()
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@dp.callback_query(F.data == "admin")
async def admin_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback):
        await callback.answer("Faqat admin uchun", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    text, markup = await admin_panel()
    await safe_edit(callback.message, text, markup)


@dp.callback_query(F.data == "chadd")
async def admin_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback):
        await callback.answer("Faqat admin uchun", show_alert=True)
        return
    await callback.answer()
    await state.set_state(Admin.add_channel)
    await callback.message.answer(
        "➕ <b>Kanal qo'shish</b>\n"
        f"{LINE}\n"
        "1. Botni kanalga <b>admin</b> qilib qo'shing (obunani tekshirish uchun shart)\n"
        "2. Keyin shulardan birini yuboring:\n"
        "   • kanal username'i: <code>@kanal_nomi</code>\n"
        "   • havola: <code>https://t.me/kanal_nomi</code>\n"
        "   • yoki kanaldan istalgan xabarni shu yerga <b>forward</b> qiling (yopiq kanal uchun)",
        reply_markup=inline([[("↩️ Bekor qilish", "admin")]]),
    )


@dp.message(Admin.add_channel)
async def admin_add_channel(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await state.clear()
        return
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "chat", None) is not None:
        ref = origin.chat.id
    elif message.text:
        ref = message.text.strip()
    else:
        await message.answer("✋ @username, havola yuboring yoki kanaldan xabar forward qiling.")
        return
    try:
        channel = await subscription.resolve_channel(message.bot, ref)
    except ValueError as error:
        await message.answer(f"❗️ {esc(str(error))}", reply_markup=inline([[("↩️ Bekor qilish", "admin")]]))
        return
    await asyncio.to_thread(db.add_channel, channel["chat_id"], channel["title"], channel["url"], message.from_user.id)
    subscription.forget()
    await state.clear()
    await message.answer(f"✅ <b>{esc(channel['title'])}</b> majburiy obunaga qo'shildi.")
    text, markup = await admin_panel()
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("chdel:"))
async def admin_remove_channel(callback: CallbackQuery) -> None:
    if not is_admin(callback):
        await callback.answer("Faqat admin uchun", show_alert=True)
        return
    await asyncio.to_thread(db.remove_channel, int(callback.data.split(":", 1)[1]))
    subscription.forget()
    await callback.answer("❌ Kanal olib tashlandi")
    text, markup = await admin_panel()
    await safe_edit(callback.message, text, markup)


# ---------- Obuna sozlamalari (narx, chegirma, karta) ----------

# kalit: (so'raladigan savol, raqammi)
SETTING_FIELDS = {
    "card_number": ("💳 Karta raqamini yuboring (masalan <code>8600 1234 5678 9012</code>)", False),
    "card_holder": ("👤 Karta egasining ism-familiyasini yuboring", False),
    "trial_days": ("⏳ Bepul sinov necha kun bo'lsin? <code>0</code> — sinov o'chiq", True),
    "ai_essays": ("🤖 Obunada oyiga nechta esseni AI tekshirsin? (har bir o'qituvchiga)", True),
    "pack_essays": ("📦 Bitta esse paketida nechta esse bo'lsin?", True),
}


async def settings_panel() -> tuple[str, InlineKeyboardMarkup]:
    card = await asyncio.to_thread(plans.card)
    offers = await asyncio.to_thread(plans.offers)
    trial = await asyncio.to_thread(plans.trial_days)
    limits = await asyncio.to_thread(plans.limits)
    offer = await asyncio.to_thread(plans.pack)
    lines = [
        "💎 <b>Obuna sozlamalari</b>", LINE,
        f"💳 Karta: <code>{esc(card['number']) or '— kiritilmagan'}</code>"
        + (f"\n👤 Egasi: {esc(card['holder'])}" if card["holder"] else ""),
        f"⏳ Bepul sinov: <b>{trial}</b> kun" + (" <i>(o'chiq — darhol obuna kerak)</i>" if not trial else ""),
        f"🤖 Obunada AI: oyiga <b>{limits['essays']}</b> ta esse",
        f"📦 Esse paketi: +{offer['essays']} ta — <b>{plans.money(offer['price'])} so'm</b>",
        "", "<b>Narxlar</b> — tugma bosib o'zgartiring:",
    ]
    rows = []
    for offer in offers:
        price = f"{plans.money(offer['final'])} so'm"
        if offer["discount"]:
            price = f"<s>{plans.money(offer['price'])}</s> <b>{price}</b> 🔥 −{offer['discount']}%"
        lines.append(f"• {offer['name']} — {price}")
        rows.append([(f"💵 {offer['name']}", f"set:price_{offer['key']}"),
                     (f"🔥 −{offer['discount']}%", f"set:discount_{offer['key']}")])
    rows.append([("💳 Karta", "set:card_number"), ("👤 Karta egasi", "set:card_holder")])
    rows.append([("⏳ Sinov kunlari", "set:trial_days")])
    rows.append([("🤖 Oylik esse limiti", "set:ai_essays")])
    rows.append([("📦 Paket narxi", "set:price_pack"), ("📦 Paket hajmi", "set:pack_essays")])
    rows.append([("🔄 Yangilash", "settings"), ("↩️ Admin paneli", "admin")])
    return "\n".join(lines), inline(rows)


@dp.message(Command("sozlama"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("⛔️ Bu bo'lim faqat admin uchun.")
        return
    await state.clear()
    text, markup = await settings_panel()
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "settings")
async def settings_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback):
        await callback.answer("Faqat admin uchun", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    text, markup = await settings_panel()
    await safe_edit(callback.message, text, markup)


@dp.callback_query(F.data.startswith("set:"))
async def settings_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback):
        await callback.answer("Faqat admin uchun", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key.startswith("price_"):
        period = key[6:]
        question = f"💵 <b>{plans.period_name(period)}</b> narxini so'mda yuboring (masalan <code>99000</code>)"
    elif key.startswith("discount_"):
        period = key[9:]
        question = (f"🔥 <b>{plans.period_name(period)}</b> uchun chegirmani foizda yuboring "
                    f"(<code>0</code> — chegirmasiz, <code>20</code> — 20%)")
    elif key in SETTING_FIELDS:
        question = SETTING_FIELDS[key][0]
    else:
        await callback.answer("Noma'lum sozlama", show_alert=True)
        return
    await callback.answer()
    await state.set_state(Admin.edit_setting)
    await state.update_data(key=key)
    await callback.message.answer(question, reply_markup=inline([[("↩️ Bekor qilish", "settings")]]))


@dp.message(Admin.edit_setting)
async def settings_save(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await state.clear()
        return
    key = (await state.get_data()).get("key", "")
    value = (message.text or "").strip()
    if not key or not value:
        await message.answer("✋ Qiymatni matn ko'rinishida yuboring.")
        return
    if key.startswith(("price_", "discount_", "trial_", "ai_", "pack_")):
        digits = value.replace(" ", "").replace("'", "").replace(",", "")
        if not digits.isdigit():
            await message.answer("Faqat raqam yozing, masalan: <code>99000</code>")
            return
        number = int(digits)
        if key.startswith("discount_") and number > 90:
            await message.answer("Chegirma 0 dan 90 gacha bo'lsin.")
            return
        value = str(number)
    await asyncio.to_thread(db.set_setting, key, value)
    await state.clear()
    text, markup = await settings_panel()
    await message.answer("✅ Saqlandi")
    await message.answer(text, reply_markup=markup)


# ---------- Ilova chaqiradigan amallar (webapp.HOOKS) ----------

async def send_pdf(bot: Bot, chat_id: int, test: dict, kind: str) -> None:
    """Kalit (key), natijalar (results) PDF'i yoki natijalar Excel'ini (excel) chatga yuboradi."""
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    if kind == "key":
        content = await asyncio.to_thread(pdfs.answer_key_pdf, test, keys)
        name, caption = f"kalit_{test['code']}.pdf", "🔑 <b>Javoblar kaliti</b> — faqat o'zingiz uchun (uzatib bo'lmaydi)."
    elif kind == "excel":
        attempts = await asyncio.to_thread(db.list_attempts, test["id"])
        content = await asyncio.to_thread(excel.results_xlsx, test, attempts)
        name, caption = f"natijalar_{test['code']}.xlsx", f"📗 <b>{esc(test['title'])}</b> — Excel, {len(attempts)} ta qatnashchi"
    else:
        attempts = await asyncio.to_thread(db.list_attempts, test["id"])
        content = await asyncio.to_thread(pdfs.results_pdf, test, attempts)
        name, caption = f"natijalar_{test['code']}.pdf", f"📊 <b>{esc(test['title'])}</b> — {len(attempts)} ta qatnashchi"
    await bot.send_document(chat_id, BufferedInputFile(content, filename=name), caption=caption,
                            protect_content=kind == "key")


async def send_sheets(bot: Bot, chat_id: int, test: dict, students: list[dict], blanks: int) -> None:
    """Oflayn test: har o'quvchiga shaxsiy titul (ismi, raqami, varianti) + bo'sh titullar — bitta PDF."""
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    content = await asyncio.to_thread(sheet.sheets_pdf, test, list(keys), students, blanks)
    parts = ([f"👥 {len(students)} ta shaxsiy"] if students else []) + ([f"{blanks} ta bo'sh"] if blanks else [])
    caption = (f"📄 <b>Titullar</b> · {esc(test['title'])}\n" + " + ".join(parts) + "\n"
               + ("🖨 Bo'sh titulni printerda kerakli sonda nusxa qilib chiqaring — o'quvchi ID'sini o'zi bo'yaydi.\n"
                  if blanks else "")
               + (f"📝 Esse qog'ozda emas: o'quvchilar botga kod <b>{test['code']}</b> ni kiritib, tituldagi ID'si "
                  "bilan yozadi — titul skanerlanganda natijaga o'zi qo'shiladi.\n" if test["fmt"] == "milliy" else "")
               + "Bo'yalgan titullarni ilovada «📷 Skanerlash» orqali tekshiring.")
    await bot.send_document(chat_id, BufferedInputFile(content, filename=f"titullar_{test['code']}.pdf"), caption=caption)


async def scan_notice(bot: Bot, chat_id: int, test: dict, name: str, points: float, max_points: float) -> None:
    """APK skanerida saqlangan natija — o'qituvchining bot chatiga."""
    await bot.send_message(chat_id, f"📷 <b>Skaner</b> · {esc(test['title'])} (kod {test['code']})\n"
                                    f"👤 {esc(name)} — <b>{fmt_num(points)}</b> / {fmt_num(max_points)} ball")


async def store_photo(bot: Bot, chat_id: int, data: bytes, caption: str) -> str:
    """Rasmni o'qituvchi chatiga yuboradi va Telegram file_id'ini qaytaradi (rasm Telegram'da saqlanadi)."""
    message = await bot.send_photo(chat_id, BufferedInputFile(data, filename="essay.jpg"), caption=caption[:1000])
    return message.photo[-1].file_id


async def test_channels_missing(bot: Bot, user_id: int, channels: list[dict], recheck: bool) -> list[dict]:
    """Testning majburiy kanallari; «Obuna bo'ldim» bosilganda (recheck) kesh tozalanadi."""
    if recheck:
        subscription.forget_member(user_id)
    return await subscription.missing_in(bot, user_id, channels)


async def record_attempt(bot: Bot, test: dict, tg_id: int, name: str, variant: str, answers: dict,
                         essay_text: str | None, photos: list[str], essay_cost: float, notify: bool = True) -> dict | None:
    """Javoblarni tekshirib saqlaydi, esseni navbatga qo'yadi, o'qituvchiga xabar beradi. Takroriy bo'lsa — None."""
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    key = keys.get(variant) or next(iter(keys.values()))
    result = grade(test["items"], key, answers)
    try:
        attempt = await asyncio.to_thread(db.save_attempt, test["id"], tg_id, name, variant, answers, result)
    except db.IntegrityError:  # shu test uchun javob allaqachon bor (SQLite ham, PostgreSQL ham)
        return None
    if test["fmt"] == "milliy":
        ai = essay_ai.essay_enabled() and await webapp.ai_allowed(test["owner_id"], "essays") if (essay_text or photos) else False
        status = ("pending" if ai else "manual") if (essay_text or photos) else "none"
        await asyncio.to_thread(db.create_essay, attempt["id"], essay_text, photos, status)
        if essay_cost:
            await asyncio.to_thread(db.add_essay_cost, attempt["id"], essay_cost)
    correct = result["test_correct"] + result["written_correct"]
    summary = (
        f"{fmt_num(result['points'])} ball (essesiz)" if test["fmt"] == "milliy"
        else f"{correct}/{result['auto_total']} ({fmt_num(percent(correct, result['auto_total']))}%)"
    )
    if not notify:  # skanerlashda o'qituvchi natijani ekranda ko'rib turadi
        return attempt
    count = len(await asyncio.to_thread(db.list_attempts, test["id"]))
    try:
        await bot.send_message(
            test["owner_id"],
            f"🆕 <b>{esc(test['title'])}</b> · {test['code']}\n"
            f"👤 {esc(name)}{f' · {variant} variant' if len(keys) > 1 else ''} — <b>{summary}</b>\n"
            f"👥 Jami topshirdi: <b>{count}</b> ta",
            reply_markup=app_markup("◈ Testni ochish", t=test["id"]),
        )
    except Exception:
        logger.warning("O'qituvchiga natija haqida xabar yuborilmadi")
    return attempt


def result_text(attempt: dict, test: dict, rasch: tuple[float, str] | None, multi_variant: bool = False) -> str:
    lines = [f"👤 <b>{esc(attempt['student_name'])}</b>" + (f" · {attempt['variant']} variant" if multi_variant else "")]
    if test["fmt"] == "milliy":
        top = max_points(test["items"], test["fmt"])
        lines.append(f"🧩 Test: <b>{attempt['test_correct']}</b>/{len(choice_items(test['items']))} · "
                     f"✍️ Yozma: <b>{attempt['written_correct']}</b>/{len(text_items(test['items']))}"
                     + (f" · 📝 Esse: <b>{fmt_num(essay_to_75(attempt['essay']))}</b>/{ESSAY_SCALE}"
                        if attempt["essay"] is not None else ""))
        lines.append(f"💯 Ball: <b>{fmt_num(total_points(attempt))}</b> / {fmt_num(top)}")
        if rasch:
            lines.append(f"🎓 Umumiy ball: <b>{fmt_num(rasch[0])}</b> / 75 · Daraja: <b>{rasch[1]}</b>")
    else:
        correct = attempt["test_correct"] + attempt["written_correct"]
        lines.append(f"🎯 To'g'ri: <b>{correct}</b> / {attempt['auto_total']} "
                     f"({fmt_num(percent(correct, attempt['auto_total']))}%)")
    return "\n".join(lines)


async def announce_test(bot: Bot, test: dict, owner_chat: int) -> int:
    """Natijalarni e'lon qilish: har bir qatnashchiga qisqa natija + ilovadagi natija kartasi."""
    await asyncio.to_thread(db.set_closed, test["id"], True)
    test["closed"] = 1
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    rasch = await asyncio.to_thread(rasch_results, test, attempts) if test["fmt"] == "milliy" else {}
    multi_variant = len(await asyncio.to_thread(db.get_keys, test["id"])) > 1
    sent = 0
    for attempt in attempts:
        if attempt["tg_id"] < 0:  # oflayn (qog'ozdagi) o'quvchi — Telegram'i yo'q
            continue
        try:
            await bot.send_message(
                attempt["tg_id"],
                f"📢 <b>Natijalar e'lon qilindi</b> · {esc(test['title'])}\n{LINE}\n"
                f"{result_text(attempt, test, rasch.get(attempt['id']), multi_variant)}\n\n"
                "Har bir savol tahlili, to'g'ri javoblar va natija kartasi — ilovada 👇",
                reply_markup=app_markup("◈ Natija kartasi", r=attempt["id"]),
            )
            sent += 1
        except Exception:
            logger.warning("Qatnashchiga xabar yuborilmadi: %s", attempt["tg_id"])
        await asyncio.sleep(0.05)
    await bot.send_message(owner_chat, f"📨 <b>{esc(test['title'])}</b>: natijalar <b>{sent}</b> ta o'quvchiga yuborildi.",
                           reply_markup=app_markup("◈ Statistika", t=test["id"]))
    await send_pdf(bot, owner_chat, test, "results")
    return sent


async def essay_notice(bot: Bot, test: dict, attempt: dict, score: float) -> None:
    """Natijalar e'lon qilingandan keyin esse bahosi o'zgarsa — o'quvchiga xabar."""
    if not test["closed"]:
        return
    attempt = dict(attempt, essay=score)
    top = fmt_num(max_points(test["items"], test["fmt"]))
    try:
        await bot.send_message(
            attempt["tg_id"],
            f"✍️ <b>{esc(test['title'])}</b>\nEsse bahoyingiz yangilandi: <b>{fmt_num(essay_to_75(score))}/{ESSAY_SCALE}</b> "
            f"({fmt_num(score)}/{ESSAY_MAX} mezon bo'yicha)\n"
            f"💯 Jami ball: <b>{fmt_num(total_points(attempt))}/{top}</b>",
            reply_markup=app_markup("◈ Natija kartasi", r=attempt["id"]),
        )
    except Exception:
        logger.warning("Esse bali o'quvchiga yuborilmadi")


def _registration_text(reg: dict, username: str | None, again: bool, places: list[dict], for_teacher: bool) -> str:
    contact = (f"\n💬 @{esc(username)}" if username
               else f"\n🆔 <code>{reg['tg_id']}</code> (username yo'q — telefoniga qo'ng'iroq qiling)")
    teacher = reg.get("role") == "teacher"
    title = ("👩‍🏫 Yangi o'qituvchi" if teacher else
             ("🧑‍🎓 Sizning tumaningizga yangi o'quvchi" if for_teacher else "🧑‍🎓 Yangi o'quvchi"))
    where = "\n".join(f"📍 {esc(places_data.label(p['region'], p['district']))}" for p in places) \
        or f"📍 {esc(places_data.label(reg.get('region') or '', reg.get('district') or ''))}"
    return (f"📝 <b>{title}</b>{' · ariza yangilandi' if again else ''}\n{LINE}\n"
            f"👤 {esc(reg['full_name'])}\n"
            f"📞 {esc(reg['phone'])}"
            + (f"\n🎂 {reg['age']} yosh" if reg.get("age") else "")
            + f"\n{where}" + contact)


async def registration_notice(bot: Bot, reg: dict, username: str | None, again: bool,
                              teachers: list[dict]) -> None:
    """Ariza — shu tumandagi o'qituvchilarga va barcha adminlarga. Keyingi muloqot ular orasida."""
    places = await asyncio.to_thread(db.list_teacher_places, reg["tg_id"]) if reg.get("role") == "teacher" else []
    for teacher in teachers:  # o'quvchining arizasi — tumanidagi o'qituvchilarga
        try:
            await bot.send_message(teacher["tg_id"], _registration_text(reg, username, again, places, True))
        except Exception:
            logger.warning("O'qituvchiga ariza yuborilmadi: %s", teacher["tg_id"])
    text = _registration_text(reg, username, again, places, False)
    if teachers:
        text += f"\n\n👩‍🏫 Shu tumandagi {len(teachers)} ta o'qituvchiga yuborildi."
    for admin_id in ADMIN_IDS:
        if any(t["tg_id"] == admin_id for t in teachers):
            continue  # admin shu tumanning o'qituvchisi — ikki marta kelmasin
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.warning("Adminga ro'yxat xabari yuborilmadi: %s", admin_id)


# ---------- AI esse navbati (fon jarayoni) ----------

ESSAY_MAX_TRIES = 4


async def download_photos(bot: Bot, file_ids: list[str]) -> list[tuple[bytes, str]]:
    images = []
    for file_id in file_ids:
        buffer = await bot.download(file_id)
        images.append((buffer.read(), "image/jpeg"))
    return images


async def essay_worker(bot: Bot) -> None:
    """
    AI navbati: avval skanerlangan titullarning yozma javoblari (tez, kichik rasm), keyin esselar.
    Xato bo'lsa — keyinroq qayta urinadi, hech narsa yo'qolmaydi.
    """
    if not essay_ai.enabled() and not essay_ai.essay_enabled():
        logger.info("AI o'chiq (AI_PROVIDER=%s, ESSAY_PROVIDER=%s)", essay_ai.AI_PROVIDER, essay_ai.ESSAY_PROVIDER)
        return
    logger.info("AI yoqildi: yozma javoblar — %s, esse — %s",
                essay_ai.provider_name() if essay_ai.enabled() else "o'chiq",
                essay_ai.essay_provider_name() if essay_ai.essay_enabled() else "o'chiq")
    delay, purged_at, down_since, alerted = 10, 0.0, None, False
    while True:
        try:
            if time.time() - purged_at > 6 * 3600:  # eski yozma kataklar rasmlari — kuniga bir necha marta tozalanadi
                purged_at = time.time()
                if removed := await asyncio.to_thread(db.purge_written_images):
                    logger.info("Eski yozma kataklar rasmlari o'chirildi: %s ta", removed)
            jobs = await asyncio.to_thread(db.pending_written, 40) if essay_ai.enabled() else []
            groups: dict[int, list[dict]] = {}
            for job in jobs:  # bitta testning titullari — bitta so'rovda (WRITTEN_BATCH tadan)
                groups.setdefault(job["test_id"], []).append(job)
            for group in groups.values():
                for start in range(0, len(group), essay_ai.WRITTEN_BATCH):
                    await read_written_group(bot, group[start:start + essay_ai.WRITTEN_BATCH])
                    await asyncio.sleep(1)
            if jobs:  # navbatda yana titul bo'lishi mumkin — esselardan oldin shularni tugatamiz
                delay = 10
                continue
            for essay in (await asyncio.to_thread(db.pending_essays, 5) if essay_ai.essay_enabled() else []):
                await grade_one(bot, essay)
                await asyncio.sleep(2)
            delay, down_since, alerted = 10, None, False
        except essay_ai.AIError as error:  # limit yoki tarmoq — kutib turamiz
            logger.warning("AI vaqtincha ishlamayapti: %s", error)
            delay = min(delay * 2, 600)
            down_since = down_since or time.time()
            if not alerted and time.time() - down_since > 3600:  # 1 soatdan beri — adminga xabar
                alerted = True
                logger.error("AI 1 soatdan beri ishlamayapti (yozma javoblar va esselar navbatda kutyapti): %s", error)
        except Exception:
            logger.exception("Esse navbatida xato")
            delay = min(delay * 2, 600)
        await asyncio.sleep(delay)


WRITTEN_MAX_TRIES = 5


async def read_written_group(bot: Bot, jobs: list[dict]) -> None:
    """
    Bitta testning bir nechta titulini bitta AI so'rovida o'qiydi (limit kam sarflanadi, tezroq).
    So'rov xato bersa yoki AI biror titulni qaytarmasa — o'sha titullar alohida o'qiladi.
    """
    test = await asyncio.to_thread(db.get_test, jobs[0]["test_id"])
    labels = [item["label"] for item in text_items(test["items"])] if test else []
    valid = []
    for job in jobs:
        if not labels or not job["image"] or not await asyncio.to_thread(db.get_attempt, job["attempt_id"]):
            await asyncio.to_thread(db.written_failed, job["attempt_id"], "Titul yoki rasm topilmadi", True)
        else:
            valid.append(job)
    if len(valid) <= 1:
        for job in valid:
            await read_written_one(bot, job)
        if test and not valid:
            await written_notice(bot, test)
        return
    try:
        results = await essay_ai.read_written_batch([(base64.b64decode(j["image"]), j["mime"]) for j in valid], labels)
    except essay_ai.AIError as error:
        if error.retry:  # limit/tarmoq — hammasi navbatda qoladi, butun navbat biroz kutadi
            raise
        results = [None] * len(valid)  # javobni tushunib bo'lmadi — bittalab
    for job, written in zip(valid, results):
        if written is None:
            await read_written_one(bot, job)
        else:
            await apply_written(test, job, written)
    await written_notice(bot, test)


async def read_written_one(bot: Bot, job: dict) -> None:
    """Skanerlangan titulning yozma kataklarini o'qiydi va natijani qayta hisoblaydi."""
    attempt = await asyncio.to_thread(db.get_attempt, job["attempt_id"])
    test = await asyncio.to_thread(db.get_test, job["test_id"])
    labels = [item["label"] for item in text_items(test["items"])] if test else []
    if not attempt or not labels or not job["image"]:
        await asyncio.to_thread(db.written_failed, job["attempt_id"], "Titul yoki rasm topilmadi", True)
        return
    try:
        written, _cost = await essay_ai.read_written(base64.b64decode(job["image"]), labels, job["mime"])
    except essay_ai.AIError as error:
        give_up = not error.retry or job["tries"] + 1 >= WRITTEN_MAX_TRIES
        await asyncio.to_thread(db.written_failed, job["attempt_id"], str(error), give_up)
        if give_up:
            await written_notice(bot, test)
        if error.retry and not give_up:
            raise  # limit/tarmoq — butun navbat biroz kutadi
        return
    await apply_written(test, job, written)
    await written_notice(bot, test)


async def apply_written(test: dict, job: dict, written: dict[str, str]) -> None:
    """AI o'qigan yozma javoblar → natija qayta hisoblanadi."""
    # O'qituvchi skanerda (yoki AI ishlayotganda) o'zi yozgan javob AI'nikidan ustun — eng yangisini olamiz
    attempt = await asyncio.to_thread(db.get_attempt, job["attempt_id"])
    if not attempt:
        return
    answers = dict(attempt["answers"])
    for label, text in written.items():
        if text and not answers.get(label):
            answers[label] = text
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    key = keys.get(attempt["variant"]) or next(iter(keys.values()))
    await asyncio.to_thread(db.update_attempt_result, attempt["id"], answers, grade(test["items"], key, answers))
    await asyncio.to_thread(db.written_done, attempt["id"], written)
    logger.info("Yozma javoblar o'qildi: attempt=%s (%s ta)", attempt["id"], sum(1 for v in written.values() if v))


async def written_notice(bot: Bot, test: dict) -> None:
    """Testning yozma javob navbati tugadi — o'qituvchiga bitta umumiy xabar."""
    counts = await asyncio.to_thread(db.take_written_notice, test["id"])
    if not counts:
        return
    done, failed = counts.get("done", 0), counts.get("failed", 0)
    text = (f"✍️ <b>{esc(test['title'])}</b> · kod {test['code']}\n"
            f"Yozma javoblar AI'da o'qildi: <b>{done}</b> ta titul.")
    if failed:
        text += f"\n⚠️ {failed} ta titulni AI o'qiy olmadi — ularni qayta skanerlang yoki javobini qo'lda kiriting."
    text += "\nNatijalarni tekshirib, e'lon qilishingiz mumkin."
    try:
        await bot.send_message(test["owner_id"], text, reply_markup=app_markup("◈ Testni ochish", t=test["id"]))
    except Exception:
        logger.warning("Yozma javoblar haqida xabar yuborilmadi")


async def scan_batch_notice(bot: Bot, chat_id: int, test: dict, saved: int, written: int, essays: int,
                            failed: int) -> None:
    """Oflayn skaner telefondagi titullarni yubordi — bot chatiga umumiy xabar."""
    lines = [f"📥 <b>Skaner</b> · {esc(test['title'])} (kod {test['code']})",
             f"✅ Qabul qilindi: <b>{saved}</b> ta titul"]
    if essays:
        lines.append(f"📝 Esse varaqalari: {essays} ta — AI navbatida")
    if written:
        lines.append(f"✍️ Yozma javoblar AI'da o'qilmoqda ({written} ta) — tugagach xabar beraman")
    if failed:
        lines.append(f"⚠️ {failed} tasi yuborilmadi — ilovada qayta «Yuborish»ni bosing")
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    lines.append(f"👥 Jami natijalar: <b>{len(attempts)}</b> ta")
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=app_markup("◈ Testni ochish", t=test["id"]))


async def grade_one(bot: Bot, essay: dict) -> None:
    try:
        images = [] if essay["text"] else await download_photos(bot, essay["photos"])
        result = await essay_ai.grade(essay["topic"] or "", essay["text"], images)
    except essay_ai.AIError as error:
        give_up = not error.retry or essay["tries"] + 1 >= ESSAY_MAX_TRIES
        await asyncio.to_thread(db.essay_failed, essay["attempt_id"], str(error), give_up)
        if error.retry and not give_up:
            raise  # limit/tarmoq — butun navbat biroz kutadi
        return
    await asyncio.to_thread(db.save_essay_result, essay["attempt_id"], result)
    if AUTO_CONFIRM_ESSAYS:
        await asyncio.to_thread(db.set_essay, essay["attempt_id"], result["score"])
    logger.info("Esse baholandi: attempt=%s ball=%s ($%.4f)", essay["attempt_id"], result["score"], result["cost"])


# ---------- Qolgan xabarlar va xatolar ----------

@dp.message()
async def fallback(message: Message, state: FSMContext) -> None:
    await state.clear()
    # Eski pastki tugmalardan biri bosilgan bo'lishi mumkin — tugmalar shu xabar bilan olib tashlanadi
    await message.answer("✨ Hamma ish ilovada — pastdagi «Kirish» tugmasi yoki quyidagi tugma orqali.",
                         reply_markup=ReplyKeyboardRemove())
    if app_url():
        await message.answer("👇", reply_markup=app_markup())


@dp.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    await callback.answer("Bu tugma eskirgan. Ilovani oching 🙂", show_alert=True)


@dp.errors()
async def on_error(event: ErrorEvent) -> bool:
    logger.exception("Kutilmagan xato", exc_info=event.exception)
    update = event.update
    try:
        if update.message:
            await update.message.answer("⚠️ Xatolik yuz berdi. /start ni bosing.")
        elif update.callback_query:
            await update.callback_query.answer("⚠️ Xatolik yuz berdi, qaytadan urinib ko'ring", show_alert=True)
    except Exception:
        pass
    return True


dp.message.outer_middleware(SubscriptionGate())
dp.callback_query.outer_middleware(SubscriptionGate())


# ---------- Ishga tushirish ----------

_BOT_USERNAME: list[str] = []


async def bot_username(bot: Bot) -> str:
    if not _BOT_USERNAME:
        _BOT_USERNAME.append((await bot.get_me()).username)
    return _BOT_USERNAME[0]


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
        raise SystemExit("BOT_TOKEN topilmadi: .env faylida BOT_TOKEN=... ni BotFather tokeni bilan to'ldiring.")
    db.init()
    logger.info("Baza: %s", db.backend_name())
    if not db.POSTGRES and os.getenv("RENDER"):
        logger.warning("DIQQAT: Render'da SQLite ishlatilmoqda — har deployda ma'lumotlar O'CHADI. DATABASE_URL qo'ying!")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    webapp.HOOKS.update(
        record_attempt=lambda *args, **kwargs: record_attempt(bot, *args, **kwargs),
        send_sheets=lambda chat_id, test, students, blanks: send_sheets(bot, chat_id, test, students, blanks),
        store_photo=lambda chat_id, data, caption: store_photo(bot, chat_id, data, caption),
        scan_notice=lambda chat_id, *args: scan_notice(bot, chat_id, *args),
        scan_batch_notice=lambda chat_id, *args: scan_batch_notice(bot, chat_id, *args),
        send_text=lambda chat_id, text: bot.send_message(chat_id, text),
        bot_username=lambda: bot_username(bot),
        announce=lambda test, owner: announce_test(bot, test, owner),
        send_pdf=lambda chat_id, test, kind: send_pdf(bot, chat_id, test, kind),
        send_file=lambda chat_id, content, name, caption: bot.send_document(
            chat_id, BufferedInputFile(content, filename=name), caption=caption),
        essay_notice=lambda test, attempt, score: essay_notice(bot, test, attempt, score),
        registration_notice=lambda reg, username, again, teachers: registration_notice(bot, reg, username, again, teachers),
        missing=lambda user_id: subscription.missing_channels(bot, user_id),
        missing_in=lambda user_id, channels, recheck=False: test_channels_missing(bot, user_id, channels, recheck),
        resolve_channel=lambda ref: subscription.resolve_channel(bot, ref),
    )
    port = os.getenv("PORT")
    if port:  # avval port — Render uni darhol ko'rsin
        await webapp.start(int(port))
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Ilovani ochish"),
            BotCommand(command="royxat", description="Oflayn kursga ro'yxatdan o'tish"),
        ])
    except Exception:
        logger.warning("Buyruqlar ro'yxatini o'rnatib bo'lmadi", exc_info=True)
    if app_url():
        try:  # xabar maydoni yonidagi «Kirish» tugmasi — Mini App
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Kirish", web_app=WebAppInfo(url=app_url())))
        except Exception:
            logger.warning("Menyu tugmasini o'rnatib bo'lmadi", exc_info=True)
    else:
        logger.warning("WEBAPP_URL (https) yo'q — Mini App tugmasi o'rnatilmadi")
    logger.info("Bot ishga tushdi")
    tasks = [
        asyncio.create_task(essay_worker(bot)),   # AI navbati: yozma javoblar va esselar
        asyncio.create_task(ops.alert_sender(bot)),  # xatolar — adminga Telegram'da
        asyncio.create_task(ops.daily_backup(bot)),  # har kuni baza nusxasi — adminga
        asyncio.create_task(ops.plan_reminders(bot)),  # obuna tugashiga 3 va 1 kun qolganda — o'qituvchiga
    ]
    if url := ops.keep_alive_url():  # Render bepul tarifida server uxlab qolmasin
        tasks.append(asyncio.create_task(ops.keep_alive(url)))
    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
    