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
import re
import base64
import hashlib
import html
import json
import types
import logging
import os
import secrets
import time
from datetime import timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
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
    InputMediaPhoto,
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
import rating
import results
import sheet
import sheet_styles
import subscription
import webapp
import config
from config import ADMIN_IDS, BOT_TOKEN, WEBAPP_URL
from scoring import essay_on_paper, essay_pending, rasch_results, total_points
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
    if len(args) == 2 and args[1].startswith("t_"):  # ustozning taklif havolasi — o'quvchi ulanadi
        await join_teacher(message, args[1])
        return
    if len(args) == 2 and args[1].startswith("mt_"):  # ustoz o'z o'quvchilariga bergan mock havolasi — bepul
        code = webapp.test_from_own_code(args[1])
        test = await asyncio.to_thread(db.get_test_by_code, code) if code else None
        if not test:
            await message.answer("⌛️ Havola noto'g'ri. Ustozingizdan yangisini so'rang.")
            return
        teacher = await asyncio.to_thread(db.find_user, str(test["owner_id"])) or {}
        if test["owner_id"] != user.id:
            await webapp.link_by_teacher({"id": test["owner_id"], "name": teacher.get("full_name")}, user.id)
        await message.answer(f"✅ <b>{esc(teacher.get('full_name') or 'Ustoz')}</b>ning o'quvchisisiz — mock va esse siz uchun bepul.\n"
                             f"📢 <b>{esc(mock_name(test))}</b> · {esc(test['title'])}\n"
                             f"📅 {test['opens_at'][:10]} · {test['opens_at'][11:16]} ochiladi · {test['entry_until'][11:16]} gacha kirish",
                             reply_markup=app_markup("✨ Mockni ochish", c=test["code"]))
        return
    if len(args) == 2 and args[1].startswith("m_") and args[1][2:].isdigit():  # kanal e'lonidan: «Bugungi mock»
        test = await asyncio.to_thread(db.get_test_by_code, args[1][2:])
        if test and test.get("opens_at"):
            await message.answer(f"📢 <b>{esc(mock_name(test))}</b> · {esc(test['title'])}\n"
                                 f"📅 {test['opens_at'][:10]} · {test['opens_at'][11:16]} ochiladi · {test['entry_until'][11:16]} gacha kirish · "
                                 f"{test['closes_at'][11:16]} da yopiladi\n\nIlovada «Qatnashaman»ni bosing — ochilganda eslatamiz.",
                                 reply_markup=app_markup("✨ Mockni ochish", c=test["code"]))
            return
    if len(args) == 2 and args[1].startswith("p_"):  # ustozning ommaviy havolasi — begona esse tekshirtiradi
        await open_public(message, args[1])
        return
    if len(args) == 2 and args[1] == "register":
        await send_register_prompt(message)
        return
    await send_welcome(message, user)


async def join_teacher(message: Message, code: str) -> None:
    teacher_id = webapp.teacher_from_invite(code)
    if not teacher_id:
        await message.answer("⌛️ Taklif havolasi noto'g'ri. Ustozingizdan yangisini so'rang.")
        return
    await link_to_teacher(message, teacher_id, "taklif havolangiz")


ESSAY_CODE = re.compile(r"^\s*[EeЕе]\s*-?\s*(\d{5})\s*$")  # E48213 (lotin yoki kirill E)


@dp.message(Command("kod"))
async def cmd_essay_code(message: Message, command: CommandObject) -> None:
    """/kod — ustozga o'z esse kodi; /kod E48213 — o'quvchi ustozga ulanadi."""
    if command.args:
        match = ESSAY_CODE.match(command.args)
        teacher_id = match and await asyncio.to_thread(db.teacher_of_essay_code, match.group(1))
        if not teacher_id:
            await message.answer("❌ Bunday esse kodi yo'q. Ustozingizdan kodni qayta so'rang (masalan: E48213).")
            return
        await link_to_teacher(message, teacher_id, "esse kodingiz")
        return
    code = await asyncio.to_thread(db.essay_code_of, message.from_user.id)
    await message.answer(
        f"🔑 <b>Esse kodingiz: <code>{code}</code></b>\n\n"
        "O'quvchilaringizga ayting: botga shu kodni yozsa, sizga ulanadi va «✍️ Esse tekshirish»da esse yozib "
        "AI'da tekshirtira oladi — <b>sizning limitingizdan</b>. Har o'quvchiga oylik chegarani ilovada "
        "«👥 O'quvchilarim»da qo'yasiz.")


async def link_to_teacher(message: Message, teacher_id: int, via: str) -> None:
    """O'quvchini ustozga ulash (taklif havolasi yoki esse kodi orqali): esselari ustoz limitidan tekshiriladi."""
    if teacher_id == message.from_user.id:
        await message.answer("Bu sizning o'zingizniki — uni o'quvchilaringizga bering.")
        return
    teacher = await asyncio.to_thread(db.find_user, str(teacher_id))
    name = (teacher or {}).get("full_name") or "ustoz"
    fresh = await asyncio.to_thread(db.link_student, teacher_id, message.from_user.id)
    await message.answer(
        (f"✅ <b>Siz {esc(name)} ustozga ulandingiz</b>\n" if fresh else f"Siz {esc(name)} ustozga allaqachon ulangansiz.\n")
        + "Ilovada «✍️ Esse tekshirish»: esse yozasiz — AI imtihondagidek 75 ballik shkalada tekshiradi "
          "(ustozingiz hisobidan). Botning o'zida ham yuborsa bo'ladi 👇",
        reply_markup=inline([[("✍️ Botda esse yuborish", f"ecw:new:{teacher_id}")]]))
    if fresh:
        try:
            await message.bot.send_message(teacher_id, f"👤 <b>{esc(message.from_user.full_name)}</b> {via} orqali "
                                                       "ulandi — endi esse tekshirtira oladi (kreditingizdan).")
        except Exception:
            logger.warning("Ustozga ulanish xabari yuborilmadi")


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


LOGIN_EXPIRED = "⌛️ Kirish havolasi eskirgan yoki ishlatilgan. Ilovada «Telegram orqali kirish»ni qayta bosing."


async def confirm_app_login(message: Message, token: str) -> None:
    """
    APK ilovadagi «Telegram orqali kirish». Havolani bosishning o'zi kirish EMAS: boshqa odam o'z ilovasida
    havola ochib, uni sizga yuborishi mumkin — bossangiz, u sizning akkauntingizga kirib qolardi.
    Shuning uchun avval ogohlantirish va «Ha, kirish» tugmasi; sessiya faqat tugma bosilganda yaratiladi.
    """
    if not all(c in "0123456789abcdef" for c in token) or len(token) != 32 \
            or not await asyncio.to_thread(db.login_pending, token):
        await message.answer(LOGIN_EXPIRED)
        return
    await message.answer(
        "🔐 <b>Skaner ilovasiga kirish</b>\n"
        "Bu Telegram akkauntingiz bilan ilovaga kirish so'raldi.\n\n"
        "⚠️ Faqat <b>hozir o'zingiz</b> telefoningizdagi ilovada «Telegram orqali kirish»ni bosgan bo'lsangiz "
        "tasdiqlang. Havolani kimdir sizga yuborgan bo'lsa — <b>bosmang</b>: u akkauntingizga (testlar, kalitlar, "
        "natijalar) kirib oladi.",
        reply_markup=inline([[("✅ Ha, men kiryapman", f"applogin:ok:{token}")],
                             [("❌ Yo'q, men emas", f"applogin:no:{token}")]]),
    )


@dp.callback_query(F.data.startswith("applogin:"))
async def app_login_decision(callback: CallbackQuery) -> None:
    _, action, token = (callback.data.split(":") + ["", ""])[:3]
    if action == "no":
        await asyncio.to_thread(db.cancel_login, token)
        await callback.answer("Bekor qilindi")
        await safe_edit(callback.message, "❌ <b>Kirish bekor qilindi</b>\nHech kim akkauntingizga kirmadi.")
        return
    session = secrets.token_hex(24)
    if not await asyncio.to_thread(db.confirm_login, token, callback.from_user.id, session):
        await callback.answer()
        await safe_edit(callback.message, LOGIN_EXPIRED)
        return
    await callback.answer("✅ Kirdingiz")
    await safe_edit(callback.message,
                    "✅ <b>Ilovaga kirdingiz</b>\nSkaner ilovasiga qayting — testlaringiz o'sha yerda.\n\n"
                    "Begona qurilma kirib qolgan deb o'ylasangiz — /chiqish (barcha qurilmalardan chiqish).")


@dp.message(Command("chiqish"))
async def cmd_logout_all(message: Message) -> None:
    """Skaner ilovasidagi barcha sessiyalarni bekor qilish (telefon yo'qolsa yoki begona kirgan bo'lsa)."""
    removed = await asyncio.to_thread(db.delete_user_sessions, message.from_user.id)
    await message.answer(f"🚪 Skaner ilovasidagi barcha kirishlar bekor qilindi ({removed} ta qurilma).\n"
                         "Ilovaga qayta kirish uchun ilovada «Telegram orqali kirish»ni bosing."
                         if removed else "Skaner ilovasida faol kirish yo'q.")


async def send_welcome(message: Message, user) -> None:
    # Salomlashish xabarining o'zi eski versiyadagi pastki tugmalarni (klaviatura) olib tashlaydi.
    # Alohida xabar bilan olib tashlab, uni o'chirib bo'lmaydi — Telegram eski tugmalarni qaytarib ko'rsatadi
    await message.answer(
        "🧠 <b>Idrok · Test</b>\n"
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


# ---------- Admin: o'qituvchiga test yaratish ruxsati (obuna yopiq paytida) ----------

@dp.message(Command("ruxsat", "ruxsat_ol"))
async def cmd_access(message: Message, command: CommandObject) -> None:
    """/ruxsat <ID yoki @username> — test yaratishga ruxsat; /ruxsat_ol <...> — olib qo'yish."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Bu buyruq faqat admin uchun.")
        return
    allow = command.command == "ruxsat"
    ref = (command.args or "").strip()
    if not ref:
        await message.answer("Ishlatish:\n<code>/ruxsat 123456789</code> — test yaratishga ruxsat berish\n"
                             "<code>/ruxsat_ol 123456789</code> — ruxsatni olish\n<code>/ruxsatlar</code> — ro'yxat\n\n"
                             "ID o'rniga @username ham bo'ladi (botga /start bosgan bo'lsa). O'qituvchi ID'sini "
                             "ilovadagi «🔒 Test yaratish hozircha yopiq» kartasidan yoki /myid dan biladi.")
        return
    user = await asyncio.to_thread(db.find_user, ref.split()[0])
    if not user:
        await message.answer("Bunday foydalanuvchi topilmadi — Telegram ID'sini yozing (o'qituvchi /myid bilan biladi).")
        return
    if user["tg_id"] in ADMIN_IDS or user["tg_id"] in config.FREE_IDS:
        await message.answer(f"ℹ️ {_who(user)} — Render'dagi ro'yxatda (ADMIN_IDS / FREE_IDS), ruxsat u yerda boshqariladi.")
        return
    changed = await asyncio.to_thread(plans.set_access, user["tg_id"], allow)
    if not changed:
        await message.answer(f"ℹ️ {_who(user)} — " + ("allaqachon ruxsat bor." if allow else "ruxsati yo'q edi."))
        return
    await message.answer(("✅ Ruxsat berildi: " if allow else "🚫 Ruxsat olindi: ") + _who(user))
    try:
        await message.bot.send_message(
            user["tg_id"],
            "✅ <b>Sizga ruxsat berildi</b> — testlaringizda 45-esse bo'limi ochiq.\nIlovani oching — «Onlayn test» yoki «Oflayn test»ni bosing."
            if allow else "🔒 Ruxsatingiz olib qo'yildi — test yaratish bepul qoladi, 45-esse bo'limi esa tarif bilan ochiladi.",
            reply_markup=app_markup() if allow else None)
    except Exception:
        logger.warning("Ruxsat haqida xabar yuborilmadi: %s", user["tg_id"])


@dp.message(Command("kredit"))
async def cmd_credit(message: Message, command: CommandObject) -> None:
    """Admin: /kredit <ID yoki @username> <son> — o'qituvchiga AI esse krediti (1 kredit = 1 esse tekshiruvi).
    /kredit <ID> — qoldiqni ko'rish. O'qituvchi to'lagan pulga mos kredit qo'shiladi."""
    if message.from_user.id not in ADMIN_IDS:
        balance = await asyncio.to_thread(db.get_credits, message.from_user.id)
        await message.answer(f"🤖 AI esse kreditingiz: <b>{balance}</b> ta (1 kredit = 1 esse tekshiruvi)."
                             + (f"\nTo'ldirish: @{esc(config.ADMIN_CONTACT)}" if config.ADMIN_CONTACT else ""))
        return
    args = (command.args or "").split()
    if not args:
        await message.answer("Ishlatish:\n<code>/kredit 123456789 100</code> — 100 ta esse krediti qo'shish\n"
                             "<code>/kredit 123456789</code> — qoldiq\n\n1 kredit = o'quvchining 1 ta essesini AI tekshirishi.")
        return
    user = await asyncio.to_thread(db.find_user, args[0])
    if not user:
        await message.answer("Bunday foydalanuvchi topilmadi — Telegram ID'sini yozing.")
        return
    if len(args) < 2:
        balance = await asyncio.to_thread(db.get_credits, user["tg_id"])
        await message.answer(f"🤖 {_who(user)}\nAI esse krediti: <b>{balance}</b> ta")
        return
    if not args[1].isdigit() or not 0 < int(args[1]) <= 100000:
        await message.answer("Kredit soni musbat butun son bo'lsin, masalan 100.")
        return
    amount = int(args[1])
    balance = await asyncio.to_thread(plans.add_pack, user["tg_id"], amount)
    await message.answer(f"✅ {_who(user)}\n🤖 +{amount} esse krediti · jami <b>{balance}</b> ta")
    try:
        await message.bot.send_message(
            user["tg_id"], f"🤖 <b>AI esse krediti qo'shildi: +{amount}</b>\nJami: <b>{balance}</b> ta. "
            "O'quvchilaringiz essesi endi AI bilan tekshiriladi (1 esse = 1 kredit).", reply_markup=app_markup())
    except Exception:
        logger.warning("Kredit haqida xabar yuborilmadi: %s", user["tg_id"])


async def join_notice(bot: Bot, teacher_id: int, request_id: int, name: str, username: str | None,
                      photo: bytes | None) -> None:
    """«O'quvchisiman» arizasi ustozga: ism (va rasm), «Tasdiqlash / Rad etish»."""
    text = (f"🙋 <b>O'quvchingiz bo'lmoqchi</b>\n👤 {esc(name)}" + (f" (@{esc(username)})" if username else "")
            + "\nTasdiqlasangiz — onlayn mocklaringizni bepul topshiradi (kreditingizdan).")
    markup = inline([[("✅ Tasdiqlash", f"jr:ok:{request_id}"), ("❌ Rad etish", f"jr:no:{request_id}")]])
    if photo:
        await bot.send_photo(teacher_id, BufferedInputFile(photo, filename="rasm.jpg"), caption=text, reply_markup=markup)
    else:
        await bot.send_message(teacher_id, text, reply_markup=markup)


@dp.callback_query(F.data.startswith("jr:"))
async def decide_join_button(callback: CallbackQuery) -> None:
    _, action, raw = (callback.data.split(":") + ["", ""])[:3]
    try:
        result = await webapp.decide_join(callback.from_user.id, int(raw), action == "ok")
    except webapp.ApiError as error:
        await callback.answer(json.loads(error.text).get("error", "Xato"), show_alert=True)
        return
    await callback.answer("✅ O'quvchingiz bo'ldi" if action == "ok" else "❌ Rad etildi")
    note = f"✅ Tasdiqlandi — {esc(result['name'])} o'quvchingiz" if action == "ok" else "❌ Rad etildi"
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=f"{callback.message.html_text}\n{LINE}\n{note}")
        else:
            await callback.message.edit_text(f"{callback.message.html_text}\n{LINE}\n{note}")
    except TelegramBadRequest:
        pass


@dp.message(Command("ruxsatlar"))
async def cmd_access_list(message: Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Bu buyruq faqat admin uchun.")
        return
    ids = sorted(await asyncio.to_thread(plans.granted_ids))
    lines = []
    for tg_id in ids:
        lines.append("• " + _who(await asyncio.to_thread(db.find_user, str(tg_id))))
    env = sorted(config.FREE_IDS)
    await message.answer(
        f"👩‍🏫 <b>Test yaratishga ruxsat</b> (botda berilgan): {len(ids)} ta\n" + ("\n".join(lines) or "— yo'q")
        + (f"\n\n⚙️ Render (FREE_IDS): {', '.join(str(x) for x in env)}" if env else "")
        + "\n\nBerish: <code>/ruxsat ID</code> · olish: <code>/ruxsat_ol ID</code>")


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
        text = (f"🤖 <b>AI esse krediti qo'shildi: +{essays}</b>\nJami: <b>{balance}</b> ta — yana {balance} ta o'quvchi "
                "essesini tekshirtira olasiz (1 esse = 1 kredit)." if not plans.sales_on() else
                f"📦 <b>Esse paketi qo'shildi</b>: +{essays} ta\n"
                f"Jami qo'shimcha: {balance} ta esse. Oylik limit tugaganda AI shulardan foydalanadi — "
                "muddati yo'q, keyingi oyga ham o'tadi.")
        await bot.send_message(tg_id, text, reply_markup=app_markup())
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
    if not plans.sales_on():
        await message.answer("🔒 Tarif hozircha sotilmayapti. Test yaratish hamma uchun bepul — 45-esse bo'limi esa "
                             "tarif (yoki admin ruxsati) bilan ochiladi.\n"
                             f"Telegram ID'ingiz: <code>{message.from_user.id}</code>"
                             + (f" — ruxsat uchun @{esc(config.ADMIN_CONTACT)} ga yuboring." if config.ADMIN_CONTACT else ""))
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
    awaiting = await asyncio.to_thread(db.practice_awaiting_pay, message.from_user.id)
    if awaiting:  # botdagi pullik esse tekshiruvi cheki
        await practice_receipt(message, awaiting)
        return
    scan = await asyncio.to_thread(db.collecting_essay_scan, message.from_user.id)
    if scan:  # o'quvchi ilovada «Botga yuboraman» ni bosgan — bu esse sahifasi
        await essay_page_received(message, scan)
        return
    # Bir nechta to'lov ochiq bo'lsa — oxirgi boshlangani (o'quvchi hozir shuni to'layapti)
    uid = message.from_user.id
    candidates = [(pay, handler) for pay, handler in (
        (await asyncio.to_thread(db.open_pass_payment, uid), pass_receipt_received),
        (await asyncio.to_thread(db.open_wallet_payment, uid), wallet_receipt_received),
        (await asyncio.to_thread(db.open_essay_payment, uid), essay_receipt_received)) if pay]
    if candidates:
        pay, handler = max(candidates, key=lambda c: c[0].get("created_at") or "")
        await handler(message, pay)
        return
    payment = await asyncio.to_thread(db.open_payment, message.from_user.id)
    if not payment:
        if not plans.sales_on():  # obuna yopiq — rasm nima uchunligini tushuntiramiz (obunani taklif qilmaymiz)
            await message.answer("📷 Bu rasm hech narsaga biriktirilmadi.\n"
                                 "• Titulni tekshirish — ilovada «📷 Skanerlash»\n"
                                 "• Qog'ozdagi esse — testdagi «📷 Qog'ozga yozdingizmi?» → «🤖 Botga yuboraman»\n"
                                 "• To'lov cheki — avval ilovada to'lovni boshlang",
                                 reply_markup=app_markup())
            return
        await message.answer("🧾 Chek qabul qilinmadi: avval ilovadagi «💎 Obuna» tugmasidan muddatni tanlang.",
                             reply_markup=app_markup("💎 Obuna"))
        return
    if payment["status"] == "sent":  # chek allaqachon adminda — har rasm bilan adminlarni qayta bezovta qilmaymiz
        await message.answer(f"⏳ Chekingiz allaqachon adminga yuborilgan · {plans.payment_title(payment)}.\n"
                             "Tasdiqlanishini kuting. Chek xato bo'lsa — ilovada «💎 Obuna»dan to'lovni qaytadan boshlang.")
        return
    await plan_receipt_received(message, payment)


RECEIPT_WAIT = "🔎 Chek qabul qilindi — AI tekshirmoqda.\n⏳ Natijasi 1–2 daqiqada shu yerga keladi."


def receipt_wait(pay: dict) -> str:
    """Chek kelganda o'quvchiga: AI tekshiradi (1–2 daqiqa) yoki — pul bot kartasiga tushgan bo'lsa — ustoz ko'radi."""
    return "🧾 Chek qabul qilindi — ustozga yuborilmoqda…" if pay.get("payee") == "platform" else RECEIPT_WAIT


async def plan_receipt_received(message: Message, payment: dict) -> None:
    """Obuna (yoki esse paketi) cheki: AI o'qiydi — bugungi sana, summa, qabul qiluvchi karta yoki ismi mos bo'lsa
    obuna darhol yoqiladi. Mos kelmasa (yoki AI o'qiy olmasa) — chek sababi bilan adminlarga (ADMIN_IDS, FREE_IDS)."""
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    mime = "image/jpeg" if message.photo else (message.document.mime_type or "")
    good, reasons, txn, digest = False, [], None, None
    if mime.startswith("image/"):
        wait = await message.answer(RECEIPT_WAIT)
        try:
            data = (await message.bot.download(file_id)).read()
            digest = hashlib.sha256(data).hexdigest()
            card = await asyncio.to_thread(plans.card)
            good, reasons, txn = await webapp.verify_receipt(data, mime, payment["amount"], card)
            if await asyncio.to_thread(db.receipt_used, digest, txn, payment["id"], "payments"):
                good, reasons = False, reasons + ["bu chek oldin ishlatilgan"]
        except Exception:
            logger.exception("Obuna cheki avtomatik tekshirilmadi")
            good, reasons = False, ["chekni avtomatik tekshirib bo'lmadi"]
        finally:
            try:
                await wait.delete()
            except TelegramBadRequest:
                pass
    else:
        reasons = ["chek rasm emas (fayl) — AI o'qimadi"]
    reasons = [r.replace("ustoz", "admin") for r in reasons]
    await asyncio.to_thread(db.attach_receipt, payment["id"], file_id, digest, txn, "; ".join(reasons) or None)
    if good and await asyncio.to_thread(db.decide_payment, payment["id"], "paid", 0):
        note = await apply_payment(message.bot, payment)
        await send_receipt_to_admins(message.bot, payment["id"], bool(message.photo), note=f"🤖 AI tasdiqladi — {note}")
        return
    await message.answer(f"⏳ <b>Chek qabul qilindi</b> · {plans.payment_title(payment)} — {plans.money(payment['amount'])} so'm\n"
                         "Avtomatik tasdiqlanmadi, admin ko'rib chiqadi va bot xabar beradi."
                         + (f"\nSabab: {esc('; '.join(reasons))}" if reasons else ""))
    await send_receipt_to_admins(message.bot, payment["id"], bool(message.photo), reasons=reasons)


READER_NAMES = {"gemini": "Gemini (bepul)", "claude": "Claude (aniqroq)"}


async def essay_scan_start(bot: Bot, student_id: int, test: dict, ai: str) -> None:
    await bot.send_message(student_id, f"📷 <b>Esse rasmi</b> · {esc(test['title'])} (kod {test['code']})\n"
                           f"Qog'ozga yozgan essengiz sahifalarini (1–{db.ESSAY_SCAN_PAGES} ta) rasmga olib, shu yerga yuboring "
                           f"va «✅ Tayyor» ni bosing.\nO'qiydi: <b>{READER_NAMES.get(ai, ai)}</b>\n\n"
                           "Maslahat: yorug' joyda, varaqni tekis qo'yib, butun sahifa ko'rinadigan qilib oling.",
                           reply_markup=inline([[("✅ Tayyor — o'qish", f"es:{test['id']}")]]))


async def essay_page_received(message: Message, scan: dict) -> None:
    if message.document and not (message.document.mime_type or "").startswith("image/"):
        await message.answer("📷 Esse sahifasini rasm ko'rinishida yuboring.")
        return
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    mime = "image/jpeg" if message.photo else message.document.mime_type
    data = (await message.bot.download(file_id)).read()
    count = await asyncio.to_thread(db.add_essay_scan_page, message.from_user.id, scan["test_id"],
                                    base64.b64encode(data).decode(), mime)
    full = count >= db.ESSAY_SCAN_PAGES
    await message.answer(f"📄 {count}-bet qabul qilindi." + (" Ko'pi bilan shuncha." if full else " Yana yuboring yoki")
                         + " «✅ Tayyor» ni bosing.", reply_markup=inline([[("✅ Tayyor — o'qish", f"es:{scan['test_id']}")]]))


@dp.callback_query(F.data.startswith("es:"))
async def essay_scan_done(callback: CallbackQuery) -> None:
    test_id = int(callback.data.split(":")[1])
    scan = await asyncio.to_thread(db.get_essay_scan, callback.from_user.id, test_id)
    if not scan or scan["status"] != "collecting":
        await callback.answer("Bu esse allaqachon o'qilgan yoki boshlanmagan", show_alert=True)
        return
    if not scan["pages"]:
        await callback.answer("Avval esse sahifasi rasmini yuboring", show_alert=True)
        return
    await callback.answer("O'qilmoqda…")
    wait = await callback.message.answer(f"🔎 {len(scan['pages'])} ta sahifa o'qilmoqda — 20–60 soniya…")
    try:
        await webapp.read_essay_scan(callback.from_user.id, test_id, notify=True)
    finally:
        try:
            await wait.delete()
        except TelegramBadRequest:
            pass


async def essay_scan_notice(bot: Bot, student_id: int, test_id: int, text: str, model: str) -> None:
    """AI o'qigan esse matni — chatga (uzun bo'lsa bo'lib) va ilovaga qaytish tugmasi."""
    test = await asyncio.to_thread(db.get_test, test_id)
    chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [""]
    await bot.send_message(student_id, f"📝 <b>Essengiz matni</b> · o'qidi: {esc(model)}\n"
                           "Xato o'qilgan so'zlarni ilovada tuzating.")
    for chunk in chunks:
        await bot.send_message(student_id, esc(chunk))
    await bot.send_message(student_id, "Ilovada «⬇️ Botdan olish» ni bosing — matn esse maydoniga tushadi.",
                           reply_markup=app_markup("📝 Testga qaytish", c=test["code"]) if test else None)


async def essay_receipt_received(message: Message, pay: dict) -> None:
    """Begona o'quvchining esse to'lovi cheki: AI tekshiradi (sana, summa, karta) — to'g'ri bo'lsa mock ochiladi."""
    if pay["status"] == "review":
        await message.answer(f"⏳ Chekingiz adminda · {esc(pay['title'])}. Kutib turing — tasdiqlansa bot xabar beradi.")
        return
    if message.document and not (message.document.mime_type or "").startswith("image/"):
        await message.answer("🧾 Chekni rasm (skrinshot) ko'rinishida yuboring.")
        return
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    mime = "image/jpeg" if message.photo else message.document.mime_type
    wait = await message.answer(receipt_wait(pay))
    try:
        data = (await message.bot.download(file_id)).read()
        await webapp.check_essay_receipt(pay["id"], data, mime)  # natijani o'quvchiga o'zi yozadi
    except Exception:
        logger.exception("Esse cheki tekshirilmadi")
        await message.answer("⚠️ Chekni tekshirib bo'lmadi — birozdan keyin qayta yuboring.")
    finally:
        try:
            await wait.delete()
        except TelegramBadRequest:
            pass


async def wallet_receipt_received(message: Message, pay: dict) -> None:
    """Esse krediti paketi cheki: AI tekshiradi (sana, summa, karta) — to'g'ri bo'lsa kredit darhol qo'shiladi."""
    if pay["status"] == "review":
        await message.answer(f"⏳ Chekingiz ustozda · {pay['essays']} ta kredit. Kutib turing — tasdiqlansa bot xabar beradi.")
        return
    if message.document and not (message.document.mime_type or "").startswith("image/"):
        await message.answer("🧾 Chekni rasm (skrinshot) ko'rinishida yuboring.")
        return
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    mime = "image/jpeg" if message.photo else message.document.mime_type
    wait = await message.answer(receipt_wait(pay))
    try:
        data = (await message.bot.download(file_id)).read()
        await webapp.check_wallet_receipt(pay["id"], data, mime)  # natijani o'quvchiga o'zi yozadi
    except Exception:
        logger.exception("Kredit cheki tekshirilmadi")
        await message.answer("⚠️ Chekni tekshirib bo'lmadi — birozdan keyin qayta yuboring.")
    finally:
        try:
            await wait.delete()
        except TelegramBadRequest:
            pass


async def pass_receipt_received(message: Message, pay: dict) -> None:
    """«💎 Tarif» cheki: AI tekshiradi — to'g'ri bo'lsa tarif darhol yoqiladi."""
    if pay["status"] == "review":
        await message.answer("⏳ Tarif chekingiz ustozda. Kutib turing — tasdiqlansa bot xabar beradi.")
        return
    if message.document and not (message.document.mime_type or "").startswith("image/"):
        await message.answer("🧾 Chekni rasm (skrinshot) ko'rinishida yuboring.")
        return
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    mime = "image/jpeg" if message.photo else message.document.mime_type
    wait = await message.answer(receipt_wait(pay))
    try:
        data = (await message.bot.download(file_id)).read()
        await webapp.check_pass_receipt(pay["id"], data, mime)  # natijani o'quvchiga o'zi yozadi
    except Exception:
        logger.exception("Tarif cheki tekshirilmadi")
        await message.answer("⚠️ Chekni tekshirib bo'lmadi — birozdan keyin qayta yuboring.")
    finally:
        try:
            await wait.delete()
        except TelegramBadRequest:
            pass


async def pass_receipt_notice(bot: Bot, payment_id: int, good: bool, reasons: list[str], photo: bytes) -> None:
    """Ustozga: tarif cheki va AI xulosasi. AI tasdiqlagan — «Bekor qilish», aks holda «Tasdiqlash / Rad etish»."""
    pay = await asyncio.to_thread(db.get_pass_payment, payment_id)
    if not pay:
        return
    who = esc(pay["full_name"] or str(pay["student_id"])) + (f" (@{esc(pay['username'])})" if pay["username"] else "")
    caption = (f"💎 <b>O'quvchi tarifi</b> · {pay['days']} kun\n{LINE}\n👤 {who} · <code>{pay['student_id']}</code>\n"
               f"💳 {plans.money(pay['amount'])} so'm\n"
               + ("🤖 AI: ✅ chek to'g'ri — tarif yoqildi" if good else
                  "🤖 AI: ⚠️ " + esc("; ".join(reasons) or "tekshirilmadi") + "\nTekshirib, qaror qiling:"))
    markup = inline([[("❌ Bekor qilish", f"sp:no:{payment_id}")]] if good else
                    [[("✅ Tasdiqlash", f"sp:ok:{payment_id}"), ("❌ Rad etish", f"sp:no:{payment_id}")]])
    for chat in [pay["teacher_id"], *[a for a in ADMIN_IDS if a != pay["teacher_id"]]]:
        try:
            await bot.send_photo(chat, BufferedInputFile(photo, filename="chek.jpg"), caption=caption, reply_markup=markup)
            return
        except Exception:
            logger.warning("Tarif cheki yuborilmadi: %s", chat)


@dp.callback_query(F.data.startswith("sp:"))
async def decide_pass_payment_button(callback: CallbackQuery) -> None:
    _, action, raw = (callback.data.split(":") + ["", ""])[:3]
    try:
        await webapp.decide_pass_payment(int(raw or 0), action == "ok", callback.from_user.id)
    except webapp.ApiError as error:
        await callback.answer(json.loads(error.text).get("error", "Xato"), show_alert=True)
        return
    await callback.answer("✅ Tasdiqlandi" if action == "ok" else "❌ Rad etildi")
    note = "✅ Tasdiqlandi — tarif yoqildi" if action == "ok" else "❌ Rad etildi / bekor qilindi"
    try:
        await callback.message.edit_caption(caption=f"{callback.message.html_text}\n{LINE}\n{note}", reply_markup=None)
    except TelegramBadRequest:
        pass


def platform_check_text(amount: int) -> str:
    """Begona o'qituvchiga: pul bot kartasiga tushgan (AI chekni o'qimaydi) — nimani tekshirish va qancha tegishi."""
    last4 = "".join(ch for ch in plans.card()["number"] if ch.isdigit())[-4:]
    share = plans.teacher_cut(amount)
    return (f"🏦 Pul <b>bot kartasiga</b>{f' (•••• {last4})' if last4 else ''} tushishi kerak — chekda karta, summa va "
            f"bugungi sanani tekshiring.\nTasdiqlasangiz: <b>{plans.money(share)} so'm</b> ({100 - plans.platform_share()}%) "
            f"balansingizga, {plans.platform_share()}% — bot hisobiga.\nTekshirib, qaror qiling:")


async def platform_approved_notice(bot: Bot, kind: str, payment_id: int) -> None:
    """Ustoz bot kartasiga tushgan chekni o'zi tasdiqladi — nusxasi adminlarga «Bekor qilish» bilan (soxta chek bo'lsa
    bekor qilinadi: o'quvchiga ochilgan esse / kredit va ustozning ulushi qaytib olinadi)."""
    pay = await asyncio.to_thread(db.get_essay_payment if kind == "essay" else db.get_wallet_payment, payment_id)
    if not pay:
        return
    teacher_id = pay["owner_id"] if kind == "essay" else pay["teacher_id"]
    teacher = await asyncio.to_thread(db.find_user, str(teacher_id)) or {}
    who = esc(pay.get("full_name") or str(pay["student_id"]))
    what = f"Esse to'lovi · {esc(pay['title'])}" if kind == "essay" else f"Esse krediti · {pay['essays']} ta"
    caption = (f"👩‍🏫 <b>Ustoz tasdiqladi</b> · {what}\n{LINE}\n👤 {who} · 💳 <b>{plans.money(pay['amount'])} so'm</b>\n"
               f"Ustoz: {esc(teacher.get('full_name') or str(teacher_id))} · ulushi "
               f"{plans.money(plans.teacher_cut(pay['amount']))} so'm\n"
               "Pul kartangizga tushganini tekshiring. Tushmagan bo'lsa — «Bekor qilish».")
    markup = inline([[("❌ Bekor qilish", f"{'ep' if kind == 'essay' else 'wp'}:no:{payment_id}")]])
    photo = base64.b64decode(pay["receipt"]) if pay.get("receipt") else None
    for admin_id in ADMIN_IDS | config.FREE_IDS:
        try:
            if photo:
                await bot.send_photo(admin_id, BufferedInputFile(photo, filename="chek.jpg"), caption=caption,
                                     reply_markup=markup)
            else:
                await bot.send_message(admin_id, caption + "\n<i>Chek yuborilmagan</i>", reply_markup=markup)
        except Exception:
            logger.warning("Ustoz tasdiqlagan chek adminga yuborilmadi: %s", admin_id)


async def wallet_receipt_notice(bot: Bot, payment_id: int, good: bool, reasons: list[str], photo: bytes) -> None:
    """Ustozga (pul uning kartasiga): kredit cheki va AI xulosasi. AI tasdiqlagan bo'lsa — «Bekor qilish»,
    aks holda «Tasdiqlash / Rad etish»."""
    pay = await asyncio.to_thread(db.get_wallet_payment, payment_id)
    if not pay:
        return
    who = esc(pay["full_name"] or str(pay["student_id"])) + (f" (@{esc(pay['username'])})" if pay["username"] else "")
    caption = (f"🧾 <b>Esse krediti</b> · {pay['essays']} ta\n{LINE}\n👤 {who} · <code>{pay['student_id']}</code>\n"
               f"💳 {plans.money(pay['amount'])} so'm\n"
               + (platform_check_text(pay["amount"]) if pay.get("payee") == "platform" and not good else
                  "🤖 AI: ✅ chek to'g'ri — kredit qo'shildi" if good else
                  "🤖 AI: ⚠️ " + esc("; ".join(reasons) or "tekshirilmadi") + "\nTekshirib, qaror qiling:"))
    markup = inline([[("❌ Bekor qilish", f"wp:no:{payment_id}")]] if good else
                    [[("✅ Tasdiqlash", f"wp:ok:{payment_id}"), ("❌ Rad etish", f"wp:no:{payment_id}")]])
    try:
        await bot.send_photo(pay["teacher_id"], BufferedInputFile(photo, filename="chek.jpg"), caption=caption, reply_markup=markup)
        return
    except Exception:  # ustoz botni bloklagan — adminga (ilovada «👥 O'quvchilarim»da ham turadi)
        logger.warning("Kredit cheki ustozga yuborilmadi: %s", pay["teacher_id"])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(admin_id, BufferedInputFile(photo, filename="chek.jpg"),
                                 caption=caption + "\n<i>Ustozga yetkazilmadi — siz qaror qilishingiz mumkin</i>",
                                 reply_markup=markup)
        except Exception:
            logger.warning("Kredit cheki adminga ham yuborilmadi: %s", admin_id)


@dp.callback_query(F.data.startswith("wp:"))
async def decide_wallet_payment_button(callback: CallbackQuery) -> None:
    _, action, raw = (callback.data.split(":") + ["", ""])[:3]
    try:
        await webapp.decide_wallet_payment(int(raw or 0), action == "ok", callback.from_user.id)
    except webapp.ApiError as error:
        await callback.answer(json.loads(error.text).get("error", "Xato"), show_alert=True)
        return
    await callback.answer("✅ Tasdiqlandi" if action == "ok" else "❌ Rad etildi")
    note = "✅ Tasdiqlandi — kredit qo'shildi" if action == "ok" else "❌ Rad etildi / bekor qilindi"
    try:
        await callback.message.edit_caption(caption=f"{callback.message.html_text}\n{LINE}\n{note}", reply_markup=None)
    except TelegramBadRequest:
        pass


async def essay_receipt_notice(bot: Bot, payment_id: int, good: bool, reasons: list[str], photo: bytes) -> None:
    """Mock egasiga (pul uning kartasiga): chek rasmi va AI xulosasi. AI tasdiqlagan bo'lsa — «Bekor qilish»,
    aks holda «Tasdiqlash / Rad etish»."""
    pay = await asyncio.to_thread(db.get_essay_payment, payment_id)
    if not pay:
        return
    who = esc(pay["full_name"] or str(pay["student_id"])) + (f" (@{esc(pay['username'])})" if pay["username"] else "")
    caption = (f"🧾 <b>Esse to'lovi</b> · {esc(pay['title'])} (kod {pay['code']})\n{LINE}\n👤 {who} · "
               f"<code>{pay['student_id']}</code>\n💳 {plans.money(pay['amount'])} so'm\n"
               + (platform_check_text(pay["amount"]) if pay.get("payee") == "platform" and not good else
                  "🤖 AI: ✅ chek to'g'ri — esse ochildi" if good else
                  "🤖 AI: ⚠️ " + esc("; ".join(reasons) or "tekshirilmadi") + "\nTekshirib, qaror qiling:"))
    markup = inline([[("❌ Bekor qilish", f"ep:no:{payment_id}")]] if good else
                    [[("✅ Tasdiqlash", f"ep:ok:{payment_id}"), ("❌ Rad etish", f"ep:no:{payment_id}")]])
    try:
        await bot.send_photo(pay["owner_id"], BufferedInputFile(photo, filename="chek.jpg"), caption=caption, reply_markup=markup)
        return
    except Exception:  # ustoz botni bloklagan / hech yozmagan — adminga (ilovada «🧾 Esse to'lovlari»da ham turadi)
        logger.warning("Chek ustozga yuborilmadi: %s", pay["owner_id"])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(admin_id, BufferedInputFile(photo, filename="chek.jpg"),
                                 caption=caption + "\n<i>Ustozga yetkazilmadi — siz qaror qilishingiz mumkin</i>",
                                 reply_markup=markup)
        except Exception:
            logger.warning("Chek adminga ham yuborilmadi: %s", admin_id)


@dp.callback_query(F.data.startswith("ep:"))
async def decide_essay_payment_button(callback: CallbackQuery) -> None:
    _, action, raw = (callback.data.split(":") + ["", ""])[:3]
    try:
        await webapp.decide_essay_payment(int(raw), action == "ok", callback.from_user.id)
    except webapp.ApiError as error:
        await callback.answer(json.loads(error.text).get("error", "Xato"), show_alert=True)
        return
    await callback.answer("✅ Tasdiqlandi" if action == "ok" else "❌ Rad etildi")
    note = "✅ Tasdiqlandi — esse ochildi" if action == "ok" else "❌ Rad etildi / bekor qilindi"
    try:
        await callback.message.edit_caption(caption=f"{callback.message.html_text}\n{LINE}\n{note}", reply_markup=None)
    except TelegramBadRequest:
        pass


def withdrawal_text(item: dict) -> str:
    who = esc(item.get("full_name") or str(item["teacher_id"])) + (f" (@{esc(item['username'])})" if item.get("username") else "")
    return (f"💸 <b>Yechib olish</b> · <b>{plans.money(item['amount'])} so'm</b>\n{LINE}\n👩‍🏫 {who} · "
            f"<code>{item['teacher_id']}</code>\n💳 <code>{esc(item.get('card') or '—')}</code>"
            + (f"\n👤 {esc(item['holder'])}" if item.get("holder") else "")
            + "\n\n⚠️ Avval shu ustoz tasdiqlagan to'lovlar haqiqatan kartangizga tushganini tekshiring "
              "(soxta chek bo'lsa — o'sha xabardagi «Bekor qilish», ulush balansdan olinadi va bu so'rovni rad eting)."
            + "\nHammasi to'g'ri bo'lsa — pulni shu kartaga o'tkazing va «✅ To'landi» ni bosing.")


def withdrawal_markup(withdrawal_id: int):
    return inline([[("✅ To'landi", f"wd:ok:{withdrawal_id}"), ("❌ Rad etish", f"wd:no:{withdrawal_id}")]])


async def withdraw_notice(bot: Bot, withdrawal_id: int) -> None:
    """O'qituvchi «💸 Yechib olish» ni bosdi — adminlarga karta va summa, «To'landi / Rad etish» tugmalari bilan."""
    item = await asyncio.to_thread(db.get_withdrawal, withdrawal_id)
    if not item:
        return
    for admin_id in ADMIN_IDS | config.FREE_IDS:
        try:
            await bot.send_message(admin_id, withdrawal_text(item), reply_markup=withdrawal_markup(withdrawal_id))
        except Exception:
            logger.warning("Yechish so'rovi adminga yuborilmadi: %s", admin_id)


@dp.callback_query(F.data.startswith("wd:"))
async def decide_withdrawal_button(callback: CallbackQuery) -> None:
    _, action, raw = (callback.data.split(":") + ["", ""])[:3]
    try:
        item = await webapp.decide_withdrawal(int(raw or 0), action == "ok", callback.from_user.id)
    except webapp.ApiError as error:
        await callback.answer(json.loads(error.text).get("error", "Xato"), show_alert=True)
        return
    await callback.answer("✅ To'landi" if action == "ok" else "❌ Rad etildi")
    note = (f"✅ To'landi — {plans.money(item['amount'])} so'm, o'qituvchiga xabar berildi" if action == "ok"
            else "❌ Rad etildi — summa o'qituvchi balansiga qaytdi")
    try:
        await callback.message.edit_text(f"{callback.message.html_text}\n{LINE}\n{note}", reply_markup=None)
    except TelegramBadRequest:
        pass


@dp.message(Command("yechish"))
async def cmd_withdrawals(message: Message) -> None:
    """Admin: kutilayotgan «Yechib olish» so'rovlari va shu oy begonalardan platformaga tushgan pul."""
    if not plans.can_manage(message.from_user.id):
        await message.answer("⛔️ Bu bo'lim faqat admin uchun.")
        return
    income = await asyncio.to_thread(db.platform_income, plans.month_key())
    pending = await asyncio.to_thread(db.pending_withdrawals)
    await message.answer(
        f"💰 <b>Begonalardan tushum · {plans.month_key()}</b>\n{LINE}\n"
        f"🧾 To'lovlar: <b>{income['count']}</b> ta · jami <b>{plans.money(income['amount'])}</b> so'm\n"
        f"👩‍🏫 O'qituvchilar ulushi: {plans.money(income['share'])} so'm\n"
        f"🏦 Platformaga: <b>{plans.money(income['net'])}</b> so'm\n\n"
        + (f"💸 Kutilayotgan so'rovlar: <b>{len(pending)}</b> ta" if pending else "💸 Kutilayotgan so'rov yo'q"))
    for item in pending[:20]:
        await message.answer(withdrawal_text(item), reply_markup=withdrawal_markup(item["id"]))


async def send_receipt_to_admins(bot: Bot, payment_id: int, as_photo: bool, reasons: list[str] | None = None,
                                 note: str = "") -> None:
    """Chek adminlarga (ADMIN_IDS va FREE_IDS). note — AI tasdiqlagan (faqat ma'lumot, tugmasiz);
    aks holda AI sababi va «Tasdiqlash / Rad etish»."""
    payment = await asyncio.to_thread(db.get_payment, payment_id)
    if not payment or not payment["receipt"]:
        return
    caption = (f"🧾 <b>Yangi to'lov</b> · {plans.payment_title(payment)} — "
               f"<b>{plans.money(payment['amount'])} so'm</b>\n{LINE}\n{_who(payment)}")
    if note:
        caption += f"\n{LINE}\n{esc(note)}"
        markup = None
    else:
        if reasons:
            caption += f"\n🤖 AI: {esc('; '.join(reasons))}"
        markup = inline([[("✅ Tasdiqlash", f"pay:ok:{payment_id}"), ("❌ Rad etish", f"pay:no:{payment_id}")]])
    for admin_id in ADMIN_IDS | config.FREE_IDS:
        try:
            if as_photo:
                await bot.send_photo(admin_id, payment["receipt"], caption=caption, reply_markup=markup)
            else:
                await bot.send_document(admin_id, payment["receipt"], caption=caption, reply_markup=markup)
        except Exception:
            logger.warning("Chek adminga yuborilmadi: %s", admin_id, exc_info=True)


async def apply_payment(bot: Bot, payment: dict) -> str:
    """Tasdiqlangan to'lov: obuna yoqiladi/uzaytiriladi yoki esse paketi qo'shiladi, foydalanuvchiga xabar.
    Qaytaradi — adminlar uchun qisqa izoh."""
    if payment["period"] == plans.PACK:
        essays = payment["essays"] or (await asyncio.to_thread(plans.pack))["essays"]
        balance = await asyncio.to_thread(plans.add_pack, payment["tg_id"], essays)
        await notify_pack(bot, payment["tg_id"], essays, balance)
        return f"+{essays} esse (jami {balance})"
    status = await asyncio.to_thread(plans.grant, payment["tg_id"], payment["period"])
    await notify_granted(bot, payment["tg_id"], status)
    return f"{status['name']} · {status['expires']} gacha"


@dp.callback_query(F.data.startswith("pay:"))
async def decide_payment(callback: CallbackQuery) -> None:
    """Admin chekni tasdiqlaydi yoki rad etadi. Tasdiqlansa — obuna darhol yoqiladi (yoki esse paketi qo'shiladi)."""
    if not plans.can_manage(callback.from_user.id):
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
    if action == "ok":
        done = await apply_payment(callback.bot, payment)
        await callback.answer("✅ Tasdiqlandi")
        note = f"✅ Tasdiqladi: {esc(callback.from_user.full_name)} — {esc(done)}"
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
    rows.append([("🤖 AI sozlamalari", "aiset")])
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
    "platform_share": ("🏦 Begona o'qituvchining begonalari to'lagan puldan platformaga necha foiz? "
                       "(<code>80</code> — 80%, <code>0</code> — o'chiq: pul o'qituvchi kartasiga)", True),
    "min_essay_price": ("💵 Begona o'qituvchida bitta esse (1 kredit) narxi kamida necha so'm? (masalan <code>10000</code>)", True),
    "withdraw_min": ("💸 O'qituvchi balansi kamida necha so'm bo'lganda yechib oladi? (masalan <code>100000</code>)", True),
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
        f"🏦 Begonalar puli: platformaga <b>{plans.platform_share()}%</b>, o'qituvchiga {100 - plans.platform_share()}% · "
        f"esse kamida {plans.money(plans.min_essay_price())} so'm · yechish {plans.money(plans.withdraw_min())} so'mdan",
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
    rows.append([("🏦 Platforma %", "set:platform_share"), ("💵 Min. esse narxi", "set:min_essay_price")])
    rows.append([("💸 Yechish chegarasi", "set:withdraw_min")])
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
    if key.startswith(("price_", "discount_", "trial_", "ai_", "pack_", "platform_", "min_", "withdraw_")):
        digits = value.replace(" ", "").replace("'", "").replace(",", "")
        if not digits.isdigit():
            await message.answer("Faqat raqam yozing, masalan: <code>99000</code>")
            return
        number = int(digits)
        if key.startswith("discount_") and number > 90:
            await message.answer("Chegirma 0 dan 90 gacha bo'lsin.")
            return
        if key == "platform_share" and number > 100:
            await message.answer("Foiz 0 dan 100 gacha bo'lsin.")
            return
        value = str(number)
    await asyncio.to_thread(db.set_setting, key, value)
    await state.clear()
    text, markup = await settings_panel()
    await message.answer("✅ Saqlandi")
    await message.answer(text, reply_markup=markup)


# ---------- Ilova chaqiradigan amallar (webapp.HOOKS) ----------

async def send_pdf(bot: Bot, chat_id: int, test: dict, kind: str) -> None:
    """Kalit (key), natijalar (results) PDF'i, natijalar Excel'i (excel) yoki reyting rasmlari (rating)."""
    if kind == "rating":
        return await send_rating(bot, chat_id, test)
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    if kind == "key":
        content = await asyncio.to_thread(pdfs.answer_key_pdf, test, keys)
        name, caption = f"kalit_{test['code']}.pdf", "🔑 <b>Javoblar kaliti</b> — faqat o'zingiz uchun, o'quvchilarga bermang."
    elif kind == "excel":
        attempts = await asyncio.to_thread(db.list_attempts, test["id"])
        content = await asyncio.to_thread(excel.results_xlsx, test, attempts)
        name, caption = f"natijalar_{test['code']}.xlsx", f"📗 <b>{esc(test['title'])}</b> — Excel, {len(attempts)} ta qatnashchi"
    else:
        attempts = await asyncio.to_thread(db.list_attempts, test["id"])
        content = await asyncio.to_thread(pdfs.results_pdf, test, attempts)
        name, caption = f"natijalar_{test['code']}.pdf", f"📊 <b>{esc(test['title'])}</b> — {len(attempts)} ta qatnashchi"
    # protect_content ishlatilmaydi: himoyalangan PDF'ni Telegram saqlash/tashqi ilovada ochishga bermaydi —
    # telefonda kalit «kelgan, lekin ochilmaydi» bo'lib qolardi
    await bot.send_document(chat_id, BufferedInputFile(content, filename=name), caption=caption)


async def send_rating(bot: Bot, chat_id: int, test: dict) -> None:
    """Reyting rasmi (o'quvchilar ko'p bo'lsa — bir necha varaq) + statistika rasmi albom bo'lib, ortidan PDF."""
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    images = await asyncio.to_thread(rating.rating_images, test, attempts)
    if not images:
        await bot.send_message(chat_id, f"🏆 <b>{esc(test['title'])}</b>: reyting uchun hali natija yo'q.")
        return
    caption = f"🏆 <b>{esc(test['title'])}</b> — reyting va statistika"
    for start in range(0, len(images), 10):  # Telegram albomida ko'pi bilan 10 ta rasm
        chunk = images[start:start + 10]
        files = [BufferedInputFile(image, filename=f"reyting_{test['code']}_{start + k + 1}.png") for k, image in enumerate(chunk)]
        first = caption if start == 0 else None
        if len(files) == 1:
            await bot.send_photo(chat_id, files[0], caption=first)
        else:
            await bot.send_media_group(chat_id, [InputMediaPhoto(media=f, caption=first if k == 0 else None)
                                                 for k, f in enumerate(files)])
    pdf = await asyncio.to_thread(rating.rating_pdf, images)
    await bot.send_document(chat_id, BufferedInputFile(pdf, filename=f"reyting_{test['code']}.pdf"),
                            caption="📄 Reyting va statistika — PDF (chop etish yoki guruhga tashlash uchun)")


async def send_sheets(bot: Bot, chat_id: int, test: dict, students: list[dict], blanks: int) -> None:
    """Oflayn test: har o'quvchiga shaxsiy titul (ismi, raqami, varianti) + bo'sh titullar. Imtihon varaqasida
    test titullari va esse varaqlari ikki alohida PDF bo'lib keladi (esse qog'ozda bo'lsa)."""
    keys = list(await asyncio.to_thread(db.get_keys, test["id"]))
    official = sheet_styles.style_of(test) == "official"
    paper_essay = essay_on_paper(test)
    part = "test" if official else "all"
    content = await asyncio.to_thread(sheet.sheets_pdf, test, keys, students, blanks, part)
    parts = ([f"👥 {len(students)} ta shaxsiy"] if students else []) + ([f"{blanks} ta bo'sh"] if blanks else [])
    copy_hint = ("🖨 Bo'sh titulni printerda kerakli sonda nusxa qilib chiqaring — o'quvchi ID'sini o'zi bo'yaydi.\n"
                 if blanks else "")
    if paper_essay:
        essay_hint = "📝 Esse alohida faylda — keyingi xabarda.\n"
    elif test["fmt"] == "milliy":
        essay_hint = (f"📝 Esse qog'ozda emas: o'quvchilar botga kod <b>{test['code']}</b> ni kiritib, tituldagi ID'si "
                      "bilan yozadi — titul skanerlanganda natijaga o'zi qo'shiladi.\n")
    else:
        essay_hint = ""
    caption = (f"📄 <b>Titullar{' (test)' if official else ''}</b> · {esc(test['title'])}\n" + " + ".join(parts) + "\n"
               + copy_hint + essay_hint + "Bo'yalgan titullarni ilovada «📷 Skanerlash» orqali tekshiring.")
    await bot.send_document(chat_id, BufferedInputFile(content, filename=f"titullar_{test['code']}.pdf"), caption=caption)
    if not paper_essay:
        return
    essay = await asyncio.to_thread(sheet.sheets_pdf, test, keys, students, blanks, "essay")
    caption = (f"📝 <b>Esse varaqlari</b> · {esc(test['title'])}\n" + " + ".join(parts) + "\n"
               + ("🖨 Bo'sh esse varag'ini ham kerakli sonda nusxa qiling.\n" if blanks else "")
               + "Esse skanerlanmaydi: o'qib, ilovada 75 ballik shkalada baho qo'ying (yoki rasmini yuklang).")
    await bot.send_document(chat_id, BufferedInputFile(essay, filename=f"esse_{test['code']}.pdf"), caption=caption)


async def scan_notice(bot: Bot, chat_id: int, test: dict, name: str, points: float | None, max_points: float) -> None:
    """APK skanerida saqlangan natija — o'qituvchining bot chatiga."""
    result = (f"<b>{fmt_num(points)}</b> / {fmt_num(max_points)} ball" if points is not None
              else "saqlandi · natija esse bahosi kiritilgach chiqadi")
    await bot.send_message(chat_id, f"📷 <b>Skaner</b> · {esc(test['title'])} (kod {test['code']})\n"
                                    f"👤 {esc(name)} — {result}")


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
        # begona — o'zi to'lagan bo'lsa; charge — yechilgan kredit (AI tekshira olmasa qaytariladi)
        charge = await webapp.essay_ai_charge(test, tg_id) if (essay_text or photos) else None
        status = ("pending" if charge else "manual") if (essay_text or photos) else "none"
        await asyncio.to_thread(db.create_essay, attempt["id"], essay_text, photos, status,
                                charge if charge in ("wallet", "teacher") else None)
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
    # O'quvchi javobi darhol qabul qilinadi — o'qituvchiga xabar orqada ketadi (Telegram sekin bo'lsa ham kutmaydi)
    webapp.in_background(bot.send_message(
            test["owner_id"],
            f"🆕 <b>{esc(test['title'])}</b> · {test['code']}\n"
            f"👤 {esc(name)}{f' · {variant} variant' if len(keys) > 1 else ''} — <b>{summary}</b>\n"
            f"👥 Jami topshirdi: <b>{count}</b> ta",
            reply_markup=app_markup("◈ Testni ochish", t=test["id"]),
    ), "O'qituvchiga yangi natija")
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


async def send_with_retry(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Ko'p xabar ketma-ket yuborilganda Telegram «kuting» desa (flood) — aytilgan vaqt kutib, qayta yuboradi."""
    for _ in range(3):
        try:
            await bot.send_message(chat_id, text, **kwargs)
            return True
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after + 1)
        except Exception:
            logger.warning("Xabar yuborilmadi: %s", chat_id)
            return False
    return False


def announce_targets(attempts: list[dict]) -> list[dict]:
    """E'londa xabar oladiganlar: Telegram'i bor (oflayn titul emas) va hali xabar olmaganlar."""
    return [a for a in attempts if a["tg_id"] > 0 and not a.get("notified")]


async def announce_test(bot: Bot, test: dict, owner_chat: int) -> int:
    """Natijalarni e'lon qilish (fonda): har qatnashchiga qisqa natija + ilovadagi natija kartasi.
    Qayta ochib yana e'lon qilinsa — avval xabar olganlarga takror yuborilmaydi."""
    await asyncio.to_thread(db.set_closed, test["id"], True)
    test["closed"] = 1
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    rasch = await asyncio.to_thread(rasch_results, test, attempts) if test["fmt"] == "milliy" else {}
    multi_variant = len(await asyncio.to_thread(db.get_keys, test["id"])) > 1
    sent = 0
    for attempt in announce_targets(attempts):
        ok = await send_with_retry(
            bot, attempt["tg_id"],
            f"📢 <b>Natijalar e'lon qilindi</b> · {esc(test['title'])}\n{LINE}\n"
            f"{result_text(attempt, test, rasch.get(attempt['id']), multi_variant)}\n\n"
            "Har bir savol tahlili, to'g'ri javoblar va natija kartasi — ilovada 👇",
            reply_markup=app_markup("◈ Natija kartasi", r=attempt["id"]),
        )
        if ok:
            sent += 1
            await asyncio.to_thread(db.mark_notified, attempt["id"])
        await asyncio.sleep(0.05)
    offline = sum(1 for a in attempts if a["tg_id"] < 0)
    await send_with_retry(
        bot, owner_chat,
        f"📨 <b>{esc(test['title'])}</b>: natijalar <b>{sent}</b> ta o'quvchiga yuborildi."
        + (f"\n📄 {offline} ta titul (qog'oz) natijasi — PDF'da." if offline else ""),
        reply_markup=app_markup("◈ Statistika", t=test["id"]))
    await asyncio.to_thread(db.set_announcing, test["id"], False)
    try:
        await send_pdf(bot, owner_chat, test, "results")
    except Exception:
        logger.exception("Natijalar PDF'i yuborilmadi: test %s", test["id"])
    try:
        await send_rating(bot, owner_chat, test)
    except Exception:
        logger.exception("Reyting rasmlari yuborilmadi: test %s", test["id"])
    return sent


# ---------- «Bugungi mock»: eslatmalar va yopilgach avtomatik natija + sertifikat ----------

MOCK_TICK = 30  # soniya
MOCK_FINISH_AFTER = timedelta(minutes=5)  # yopilgach shuncha kutiladi (oxirgi avto-yuborishlar ulgursin)
MOCK_AI_WAIT = timedelta(hours=3)  # AI esselarni shuncha vaqtda tekshirib ulgurmasa — baribir e'lon (qolganlari qo'lda)


async def store_file(bot: Bot, chat_id: int, data: bytes, name: str, caption: str) -> str:
    """Savollar fayli (PDF yoki rasm) ustoz chatiga hujjat bo'lib boradi — sifati saqlanadi; Telegram file_id qaytadi."""
    message = await bot.send_document(chat_id, BufferedInputFile(data, filename=name), caption=caption[:1000])
    return message.document.file_id


async def download_bytes(bot: Bot, file_id: str) -> bytes:
    return (await bot.download(file_id)).read()


async def mock_worker(bot: Bot) -> None:
    while True:
        try:
            await mock_tick(bot)
        except Exception:
            logger.exception("Mock jadvali bajarilmadi")
        await asyncio.sleep(MOCK_TICK)


_PASS_CHECK = {"at": 0.0}


async def pass_reminders(bot: Bot, now) -> None:
    """«💎 Tarif»: 1 kun qolganda va tugaganda o'quvchiga xabar (tarif muddati o'tishi bilan o'zi o'chadi)."""
    fmt = lambda moment: moment.strftime(webapp.TIME_FMT)
    for row in await asyncio.to_thread(db.passes_to_remind, fmt(now + timedelta(days=1)), fmt(now)):
        ended = webapp._at(row["expires_at"]) <= now
        await asyncio.to_thread(db.set_pass_stage, row["teacher_id"], row["student_id"], 2 if ended else 1)
        name = esc(row.get("teacher_name") or "ustoz")
        text = (f"⌛ <b>Tarifingiz tugadi</b> · {name}\nEndi mocklar yana pullik. Yangilash — ilovadagi «💎 Tarif» kartasida."
                if ended else
                f"⏳ <b>Tarifingiz tugashiga 1 kun qoldi</b> · {name}\n{row['expires_at'][:10]} {row['expires_at'][11:16]} gacha "
                "hamma mocklar bepul. Uzaytirish — ilovadagi «💎 Tarif» kartasida.")
        await send_with_retry(bot, row["student_id"], text, reply_markup=app_markup("💎 Tarifni ochish"))
        await asyncio.sleep(0.05)


async def mock_tick(bot: Bot) -> None:
    now = plans.now()
    if time.time() - _PASS_CHECK["at"] > 600:  # tarif eslatmalari — 10 daqiqada bir
        _PASS_CHECK["at"] = time.time()
        await pass_reminders(bot, now)
    fmt = lambda moment: moment.strftime(webapp.TIME_FMT)
    for test in await asyncio.to_thread(db.mocks_to_remind, fmt(now + timedelta(minutes=30)), fmt(now)):
        opened = webapp._at(test["opens_at"]) <= now
        await asyncio.to_thread(db.set_mock_state, test["id"], "opened" if opened else "soon")  # avval — takror yuborilmasin
        signups = await asyncio.to_thread(db.mock_signups, test["id"])
        name = esc(mock_name(test))
        text = (f"<b>{name} boshlandi!</b> 🟢\n\nKirish <b>{test['entry_until'][11:16]}</b> gacha ochiq, test "
                f"<b>{test['closes_at'][11:16]}</b> da hammaga yopiladi.\n{len(signups)} kishi qatnashmoqchi. Omad!" if opened else
                f"<b>{name} 30 daqiqadan keyin</b> ⏰\n\n<b>{test['opens_at'][11:16]}</b> da ochiladi · {esc(test['title'])}\n"
                "Qalam, suv va tinch joy tayyorlang!")
        for signup in signups:
            await send_with_retry(bot, signup["tg_id"], text,
                                  reply_markup=app_markup("Mockni boshlash" if opened else "✨ Mockni ochish", c=test["code"]))
            await asyncio.sleep(0.05)
    for test in await asyncio.to_thread(db.mocks_to_warn_entry, fmt(now), fmt(now + timedelta(minutes=15))):
        await asyncio.to_thread(db.set_mock_state, test["id"], "entry")
        left = webapp._at(test["closes_at"]) - now
        hours, minutes = int(left.total_seconds() // 3600), int(left.total_seconds() % 3600 // 60)
        span = (f"{hours} soat " if hours else "") + f"{minutes} daqiqa"
        for signup in await asyncio.to_thread(db.mock_signups, test["id"]):
            started = await asyncio.to_thread(db.live_started, test["id"], signup["tg_id"])
            if started or await asyncio.to_thread(db.find_attempt, test["id"], signup["tg_id"]):
                continue  # kirgan yoki topshirgan
            await send_with_retry(
                bot, signup["tg_id"], f"<b>Kirishga 15 daqiqa qoldi</b> ⏳\n\n{esc(mock_name(test))} ga hali kirmadingiz: "
                f"<b>{test['entry_until'][11:16]}</b> dan keyin kirib bo'lmaydi.\nHozir boshlasangiz, <b>{span}</b> vaqtingiz bor.",
                reply_markup=app_markup("Hoziroq boshlash", c=test["code"]))
            await asyncio.sleep(0.05)
    await channel_tick(bot, now)
    for test in await asyncio.to_thread(db.mocks_to_finish, fmt(now - MOCK_FINISH_AFTER)):
        await finish_mock(bot, test, now)


def mock_name(test: dict) -> str:
    return f"Mock №{test['mock_no']}" if test.get("mock_no") else test["title"]


UZ_MONTHS = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]
UZ_DAYS = ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"]


def day_label(at) -> str:
    """«26-noyabr, payshanba»."""
    return f"{at.day}-{UZ_MONTHS[at.month - 1]}, {UZ_DAYS[at.weekday()]}"


# Kanal e'lonlari: har biri oldidan katta animatsiyali emoji (Telegram uni stiker kabi chiqaradi), keyin bezatilgan matn
# va «Qatnashaman» tugmasi (botga — u yerdan ilova). Har e'lon bir marta (tests.channel_posts).
async def channel_tick(bot: Bot, now) -> None:
    fmt = lambda moment: moment.strftime(webapp.TIME_FMT)
    for test in await asyncio.to_thread(db.channel_mocks, fmt(now - timedelta(days=2))):
        if not plans.ai_unlimited(test["owner_id"]):
            continue
        # «📣 E'lon kanali» (Sozlamalar); qo'yilmagan bo'lsa — mockning majburiy obuna kanallari (bot u yerda admin)
        channels = (await asyncio.to_thread(webapp.announce_channels, test["owner_id"])
                    or json.loads(test.get("channels") or "[]"))
        if not channels:
            continue
        posted = set(filter(None, (test.get("channel_posts") or "").split(",")))
        post = await channel_post_due(test, now, posted)
        if not post:
            continue
        key, emoji, text = post
        posted.update(key.split(","))
        await asyncio.to_thread(db.set_channel_posts, test["id"], ",".join(sorted(posted)))  # avval — takror ketmasin
        username = await bot_username(bot)
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🏆 Natijalar" if key == "results" else "✍️ Mockda qatnashish",
            url=f"https://t.me/{username}?start=m_{test['code']}")]]) if username else None
        for channel in channels:
            try:
                await bot.send_message(channel["chat_id"], emoji)
                await bot.send_message(channel["chat_id"], text, reply_markup=markup)
            except Exception:
                logger.warning("Kanalga e'lon yuborilmadi: %s", channel.get("chat_id"), exc_info=True)


async def channel_post_due(test: dict, now, posted: set) -> tuple[str, str, str] | None:
    """Hozir yuboriladigan e'lon (bittadan, tartib bilan): yangi → bugun → 1 soat qoldi → boshlandi → kirishga 15 daq → natijalar."""
    opens, entry, closes = (webapp._at(test[k]) for k in ("opens_at", "entry_until", "closes_at"))
    name, line = esc(mock_name(test)), "━━━━━━━━━━━━━━"
    when = f"📅 <b>{day_label(opens)}</b>\n🕗 <b>{opens:%H:%M}</b> ochiladi · <b>{entry:%H:%M}</b> gacha kirish · <b>{closes:%H:%M}</b> da yopiladi"
    signups = len(await asyncio.to_thread(db.mock_signups, test["id"]))
    people = f"\n👥 {signups} kishi qatnashmoqchi" if signups else ""
    if test.get("mock_state") == "done" and test.get("closed"):
        if "results" in posted:
            return None
        data = await asyncio.to_thread(results.test_results, test)
        top = sorted([r for r in data["rows"] if r["score"] is not None], key=lambda r: -r["score"])[:3]
        medals = ["🥇", "🥈", "🥉"]
        short = lambda n: " ".join([p for p in n.split()[:1]] + [p[0] + "." for p in n.split()[1:2]])  # «Ali V.»
        rows = "\n".join(f"{medals[i]} {esc(short(r['name']))} — <b>{fmt_num(r['score'])}</b>" + (f" · {r['grade']}" if r["grade"] else "")
                         for i, r in enumerate(top))
        return "results", "🏆", (f"🏆 <b>{name} natijalari</b>\n{line}\n{rows or 'Natijalar tayyor'}\n{line}\n"
                                 f"👥 Qatnashdi: <b>{len(data['rows'])}</b> · natija Rasch modelida\nSertifikatlar botda 🎓")
    if now >= closes:
        return None
    if "new" not in posted and now < opens:  # birinchi e'lon — har doim to'liq (kech e'lon qilingan bo'lsa ham)
        today = opens.date() == now.date()  # shu kuni e'lon qilindi — «bugun» e'loni ham shu (ikkinchisi ketmaydi)
        soon = now >= opens - timedelta(hours=1)  # 1 soatdan kam qoldi — «1 soat qoldi» e'loni ham shu
        head = f"📢 <b>Bugun {opens:%H:%M} da {name}!</b>" if today else "📢 <b>Yangi mock e'lon qilindi!</b>"
        keys = "new" + (",day" if today else "") + (",hour" if soon else "")
        return keys, "📢", (
            f"{head}\n{line}\n<b>{name}</b> · {esc(test['title'])}\n{when}{people}\n{line}\n"
            "Natija Rasch modelida — haqiqiy imtihondagidek. «Mockda qatnashish»ni bosing, ochilganda eslatamiz 👇")
    if "day" not in posted and opens.date() == now.date() and now.hour >= 9 and now < opens - timedelta(hours=1):
        return "day", "📅", (f"📅 <b>Bugun {opens:%H:%M} da {name}!</b>\n{line}\n{esc(test['title'])}\n{when}{people}\n{line}\n"
                            "Do'stlaringizni ham chaqiring — qancha ko'p qatnashsa, natija shuncha aniq 💪")
    if "hour" not in posted and opens - timedelta(hours=1) <= now < opens:
        return "hour", "⏰", (f"⏰ <b>{name} ochilishiga 1 soat qoldi!</b>\n{line}\n🕗 <b>{opens:%H:%M}</b> da boshlanadi · "
                             f"<b>{entry:%H:%M}</b> gacha kirish{people}\nQalam, suv va tinch joy tayyorlang ✍️")
    if "open" not in posted and opens <= now < entry - timedelta(minutes=15):
        return "open", "🔥", (f"🟢 <b>{name} boshlandi!</b>\n{line}\nKirish <b>{entry:%H:%M}</b> gacha ochiq, test <b>{closes:%H:%M}</b> da "
                             f"hammaga yopiladi.{people}\nOmad! 🍀")
    if "entry" not in posted and entry - timedelta(minutes=15) <= now < entry:
        return "entry", "⏳", (f"⏳ <b>Kirishga 15 daqiqa qoldi!</b>\n{line}\n{name}: <b>{entry:%H:%M}</b> dan keyin kirib bo'lmaydi.\n"
                              "Hali boshlamagan bo'lsangiz — hoziroq kiring 👇")
    return None


async def finish_mock(bot: Bot, test: dict, now) -> None:
    """Yopilgan mock: AI esselarni tekshirib bo'lgach — Rasch, natija e'loni va har qatnashchiga sertifikat."""
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    if test["closed"] or not attempts:  # ustoz o'zi e'lon qilgan yoki hech kim qatnashmagan
        await asyncio.to_thread(db.set_mock_state, test["id"], "done")
        return
    waiting = await asyncio.to_thread(db.pending_essays_of_test, test["id"])
    if waiting and now < webapp._at(test["closes_at"]) + MOCK_AI_WAIT:
        return  # AI hali tekshirmoqda — keyingi tekshiruvda
    unrated = await asyncio.to_thread(db.unrated_teacher_essays, test["id"])
    if unrated:  # «👩‍🏫 Ustoz tekshirsin» tanlanganlar — ustoz baholagach natija o'zi chiqadi
        if test.get("mock_state") != "teacher":
            await asyncio.to_thread(db.set_mock_state, test["id"], "teacher")
            await send_with_retry(
                bot, test["owner_id"], f"✍️ <b>{esc(test['title'])}</b>: mock yopildi. <b>{unrated} ta</b> o'quvchi essesini "
                "siz tekshirishingizni tanlagan — «📝 Esselar»da 75 ballik shkalada baholang.\n"
                "Hammasi baholangach natijalar va sertifikatlar o'zi yuboriladi.",
                reply_markup=app_markup("📝 Esselarni ochish", t=test["id"]))
        return
    await asyncio.to_thread(db.set_mock_state, test["id"], "done")
    await asyncio.to_thread(db.set_announcing, test["id"], True)
    await announce_test(bot, test, test["owner_id"])
    await send_mock_certificates(bot, await asyncio.to_thread(db.get_test, test["id"]))


async def send_mock_certificates(bot: Bot, test: dict) -> int:
    """Har qatnashchiga o'z sertifikati (A4 PDF, daraja bilan) — natija e'lonidan keyin."""
    data = await asyncio.to_thread(results.test_results, test)
    owners = {a["id"]: a["tg_id"] for a in await asyncio.to_thread(db.list_attempts, test["id"])}
    teacher = (await asyncio.to_thread(db.find_user, str(test["owner_id"])) or {}).get("full_name") or ""
    sent = 0
    for row in data["rows"]:
        chat = owners.get(row["attempt_id"])
        if not chat or chat < 0 or row["score"] is None:
            continue
        try:
            pdf = await asyncio.to_thread(pdfs.certificates_pdf, test, [row], teacher)
            await bot.send_document(chat, BufferedInputFile(pdf, filename=f"sertifikat_{test['code']}.pdf"),
                                    caption=f"🎓 <b>Sertifikat</b> · {esc(test['title'])}\n"
                                            f"Ball: <b>{fmt_num(row['score'])}</b>" + (f" · daraja <b>{esc(row['grade'])}</b>" if row["grade"] else ""))
            sent += 1
        except Exception:
            logger.warning("Sertifikat yuborilmadi: %s", chat, exc_info=True)
        await asyncio.sleep(0.05)
    return sent


async def resume_announcements(bot: Bot) -> None:
    """Server e'lon o'rtasida qayta ishga tushgan bo'lsa — qolgan o'quvchilarga davom etadi (xabar olganlarga takror yo'q)."""
    for test in await asyncio.to_thread(db.announcing_tests):
        logger.info("E'lon davom ettirilmoqda: test %s", test["id"])
        try:
            await announce_test(bot, test, test["owner_id"])
        except Exception:
            logger.exception("E'lonni davom ettirib bo'lmadi: test %s", test["id"])


async def essay_notice(bot: Bot, test: dict, attempt: dict, score: float) -> None:
    """Natijalar e'lon qilingandan keyin esse bahosi o'zgarsa — o'quvchiga xabar."""
    if not test["closed"]:
        return
    attempt = dict(attempt, essay=score)
    top = fmt_num(max_points(test["items"], test["fmt"]))
    try:
        await bot.send_message(
            attempt["tg_id"],
            f"✍️ <b>{esc(test['title'])}</b>\nEsse bahoyingiz yangilandi: <b>{fmt_num(essay_to_75(score))}/{ESSAY_SCALE}</b>\n"
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
AI_WAKE = asyncio.Event()  # yangi esse keldi — navbat kutmasdan darhol ishlaydi (o'quvchi natijani kutib turibdi)


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
    if not essay_ai.enabled() and not essay_ai.essay_enabled() and not essay_ai.paid_enabled():
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
            jobs = await asyncio.to_thread(db.pending_written, 40) if essay_ai.enabled() or essay_ai.paid_enabled() else []
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
            for practice in (await asyncio.to_thread(db.pending_practice, 5) if essay_ai.essay_enabled() else []):
                await grade_practice(bot, practice)
                await asyncio.sleep(1)
            delay, down_since, alerted = 10, None, False
        except essay_ai.AIError as error:  # limit yoki tarmoq — kutib turamiz
            logger.warning("AI vaqtincha ishlamayapti: %s", error)
            delay = min(delay * 2, 600)
            down_since = down_since or time.time()
            if not alerted and time.time() - down_since > AI_DOWN_NOTICE:  # 30 daqiqadan beri — ustozlarga va adminga
                alerted = True
                logger.error("AI %s daqiqadan beri ishlamayapti: %s", AI_DOWN_NOTICE // 60, error)
                await ai_down_notice(bot, str(error))
        except Exception:
            logger.exception("Esse navbatida xato")
            delay = min(delay * 2, 600)
        try:
            await asyncio.wait_for(AI_WAKE.wait(), delay)
        except asyncio.TimeoutError:
            pass
        AI_WAKE.clear()


WRITTEN_MAX_TRIES = 5
AI_DOWN_NOTICE = 30 * 60  # AI shuncha vaqt ishlamasa — navbatda ishi bor ustozlarga «qo'lda tekshiring»


async def ai_down_notice(bot: Bot, error: str) -> None:
    """AI uzoq ishlamayapti (limit yoki uzilish): navbatda ishi bor ustozlarga — qo'lda tekshirish yo'li; adminga — sabab."""
    waiting = await asyncio.to_thread(db.ai_waiting_by_owner)
    for row in waiting:
        parts = []
        if row["written"]:
            parts.append(f"✍️ {row['written']} ta titulning yozma javoblari — «✍️ Yozmalarni tekshirish»da rasmga qarab "
                         "o'zingiz belgilashingiz mumkin")
        if row["essays"]:
            parts.append(f"📝 {row['essays']} ta esse — «📝 Esselar»da 75 ballik shkalada o'zingiz baholashingiz mumkin")
        if not parts:
            continue
        try:
            await bot.send_message(row["owner_id"], "⚠️ <b>AI hozir ishlamayapti</b> (limit yoki uzilish). Navbatda:\n"
                                   + "\n".join(f"• {p}" for p in parts)
                                   + "\n\nKutishingiz ham mumkin — AI tiklansa o'zi davom etadi.", reply_markup=app_markup())
        except Exception:
            logger.warning("AI haqida ustozga xabar yuborilmadi: %s", row["owner_id"])
    total = sum(r["written"] + r["essays"] for r in waiting)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"⚠️ <b>AI {AI_DOWN_NOTICE // 60} daqiqadan beri ishlamayapti</b>\n"
                                   f"Navbatda: {total} ta ish · {len(waiting)} ta ustozga xabar berildi.\n"
                                   f"Sabab: <code>{esc(error[:300])}</code>\n\nLimit bo'lsa — Google AI Studio'da "
                                   "billingni yoqing. Hisobot: /ai")
        except Exception:
            logger.warning("AI haqida adminga xabar yuborilmadi: %s", admin_id)


def ai_settings_panel() -> tuple[str, InlineKeyboardMarkup]:
    """Qaysi ishni qaysi AI bajaradi — tugmalar bilan; kaliti yo'q AI — 🔒."""
    keys = {"gemini": essay_ai._ready("gemini"), "claude": essay_ai._ready("claude")}
    lines = ["🤖 <b>AI sozlamalari</b> — qaysi ishni qaysi AI bajaradi", LINE,
             f"Kalitlar (Render): Gemini {'✅' if keys['gemini'] else '❌'} · Claude {'✅' if keys['claude'] else '❌'}",
             f"<i>Claude Sonnet — {esc(essay_ai.CLAUDE_MODEL)} (aniq, qimmatroq) · Claude Haiku — "
             f"{esc(essay_ai.PAID_MODEL)} (arzon) · Gemini — bepul tarif</i>", ""]
    rows = []
    for task, (title, _default) in essay_ai.TASKS.items():
        chosen = essay_ai.choice_of(task)
        working = essay_ai.route_name(task)
        warn = "" if essay_ai.CHOICES[chosen][0] and keys[essay_ai.CHOICES[chosen][0]] else " ⚠️ kalit yo'q"
        lines.append(f"{title}: <b>{essay_ai.CHOICE_NAMES[chosen]}</b>{warn} → ishlayapti: {esc(working)}")
        rows.append([(("✅ " if key == chosen else "🔒 " if not keys[essay_ai.CHOICES[key][0]] else "")
                      + f"{title.split()[0]} {name}", f"aiset:{task}:{key}") for key, name in essay_ai.CHOICE_NAMES.items()])
    lines += ["", "Tugma: birinchi belgi — ish (📝 esse, ✍️ bepul yozma, 💎 pullik yozma, 🧾 chek)."]
    rows.append([("📊 AI hisoboti", "aiusage"), ("↩️ Admin paneli", "admin")])
    return "\n".join(lines), inline(rows)


@dp.message(Command("ai_sozlama"))
async def cmd_ai_settings(message: Message) -> None:
    if not is_admin(message):
        await message.answer("⛔️ Bu bo'lim faqat admin uchun.")
        return
    essay_ai.forget_choices()
    text, markup = await asyncio.to_thread(ai_settings_panel)
    await message.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("aiset"))
async def ai_settings_button(callback: CallbackQuery) -> None:
    if not is_admin(callback):
        await callback.answer("Faqat admin uchun", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) == 3 and parts[1] in essay_ai.TASKS and parts[2] in essay_ai.CHOICES:
        provider = essay_ai.CHOICES[parts[2]][0]
        if not essay_ai._ready(provider):
            await callback.answer("Bu AI'ning kaliti yo'q — Render'da " + ("GEMINI_API_KEY" if provider == "gemini"
                                  else "ANTHROPIC_API_KEY") + " ni qo'shing", show_alert=True)
            return
        await asyncio.to_thread(db.set_setting, f"ai:{parts[1]}", parts[2])
        await callback.answer(f"✅ {essay_ai.TASKS[parts[1]][0]}: {essay_ai.CHOICE_NAMES[parts[2]]}")
    else:
        await callback.answer()
    essay_ai.forget_choices()
    text, markup = await asyncio.to_thread(ai_settings_panel)
    await safe_edit(callback.message, text, markup)


@dp.callback_query(F.data == "aiusage")
async def ai_usage_button(callback: CallbackQuery) -> None:
    if not is_admin(callback):
        await callback.answer("Faqat admin uchun", show_alert=True)
        return
    await callback.answer()
    await cmd_ai_usage(types.SimpleNamespace(from_user=callback.from_user, answer=callback.message.answer))


@dp.message(Command("ai"))
async def cmd_ai_usage(message: Message) -> None:
    """Admin: AI ishlatilishi — bugun va 30 kun: tur bo'yicha so'rovlar, limit xatolari, tokenlar, narx."""
    if not is_admin(message):
        await message.answer("⛔️ Bu buyruq faqat admin uchun.")
        return
    from datetime import datetime as _dt, timedelta as _td
    names = {"written": "✍️ Yozma javoblar", "receipt": "🧾 Cheklar", "essay": "📝 Esselar", "other": "Boshqa"}

    def block(rows: list[dict]) -> str:
        if not rows:
            return "— so'rov yo'q"
        lines, by_kind = [], {}
        for r in rows:
            k = by_kind.setdefault(r["kind"], {"calls": 0, "failures": 0, "limited": 0, "tin": 0, "tout": 0, "cost": 0.0})
            k["calls"] += r["calls"] or 0; k["failures"] += r["failures"] or 0; k["limited"] += r["limited"] or 0
            k["tin"] += r["tokens_in"] or 0; k["tout"] += r["tokens_out"] or 0; k["cost"] += r["cost"] or 0
        for kind, k in by_kind.items():
            lines.append(f"{names.get(kind, kind)}: <b>{k['calls']}</b> ta"
                         + (f" · ❌ {k['failures']} xato" + (f" ({k['limited']} limit)" if k["limited"] else "") if k["failures"] else "")
                         + f" · {(k['tin'] + k['tout']) / 1000:.0f}k token" + (f" · ${k['cost']:.3f}" if k["cost"] else ""))
        models = sorted({r["model"] for r in rows})
        return "\n".join(lines) + f"\n<i>Modellar: {esc(', '.join(models))}</i>"

    today = _dt.now().strftime("%Y-%m-%d")
    month = (_dt.now() - _td(days=30)).strftime("%Y-%m-%d")
    rows_today = await asyncio.to_thread(db.ai_usage_since, today)
    rows_month = await asyncio.to_thread(db.ai_usage_since, month)
    limited = sum(r["limited"] or 0 for r in rows_month)
    await message.answer(
        f"🤖 <b>AI ishlatilishi</b>\nEsse: {esc(essay_ai.route_name('essay'))} · yozma: {esc(essay_ai.route_name('written'))} · "
        f"pullik yozma: {esc(essay_ai.route_name('paid'))} · chek: {esc(essay_ai.route_name('receipt'))}\nO'zgartirish: /ai_sozlama"
        f"\n{LINE}\n<b>Bugun</b>\n{block(rows_today)}\n\n<b>30 kun</b>\n{block(rows_month)}"
        + (f"\n\n⚠️ 30 kunda {limited} marta limitga urildi — Google AI Studio'da billingni yoqish tavsiya etiladi."
           if limited else "")
        + ("" if essay_ai.GEMINI_PRICE_IN else "\n\n<i>Gemini narxi hisoblanmayapti: Render'da GEMINI_PRICE_IN va "
           "GEMINI_PRICE_OUT (USD / 1 mln token) ni kiriting.</i>"))


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
        results = await essay_ai.read_written_batch([(base64.b64decode(j["image"]), j["mime"]) for j in valid], labels,
                                                    paid=webapp.paid_check(test))
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
        written, _cost = await essay_ai.read_written(base64.b64decode(job["image"]), labels, job["mime"],
                                                     paid=webapp.paid_check(test))
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


async def send_scan_photos(bot: Bot, chat_id: int, test: dict, items: list[tuple]) -> None:
    """Skanerlangan titullarning suratlari (telefon tayyorlagan JPEG) — 10 tadan albom, har birining izohi bilan.
    Surat Telegram'da qoladi: file_id natijaga bog'lanadi — ilovada titul belgilar bilan ko'rinadi."""
    for start in range(0, len(items), 10):
        chunk = [(item[0], item[1], item[2] if len(item) > 2 else None) for item in items[start:start + 10]]
        files = [(BufferedInputFile(photo, filename=f"titul_{test['code']}_{start + k + 1}.jpg"), caption)
                 for k, (photo, caption, _a) in enumerate(chunk)]
        head = f"📷 <b>{esc(test['title'])}</b> · titullar\n"
        if len(files) == 1:
            sent = [await bot.send_photo(chat_id, files[0][0], caption=head + esc(files[0][1]))]
        else:
            sent = await bot.send_media_group(chat_id, [InputMediaPhoto(media=f, caption=(head if k == 0 else "") + esc(c))
                                                        for k, (f, c) in enumerate(files)])
        for (_p, _c, attempt_id), message in zip(chunk, sent or []):
            photos = getattr(message, "photo", None)
            if attempt_id and photos:
                await asyncio.to_thread(db.set_sheet_file, attempt_id, photos[-1].file_id)
        await asyncio.sleep(1)  # Telegram cheklovi: bitta chatga ketma-ket albomlar orasida tanaffus


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


async def self_check_notice(bot: Bot, attempt: dict, score: float) -> None:
    """O'quvchi o'zi tekshirgan esse bahosi — o'qituvchiga (e'londan keyin bo'lsa o'quvchiga ham yangi ball)."""
    test = await asyncio.to_thread(db.get_test, attempt["test_id"])
    if not test:
        return
    try:
        await bot.send_message(
            test["owner_id"],
            f"📝 <b>{esc(test['title'])}</b> · {esc(attempt['student_name'])}\n"
            f"Esse AI bilan tekshirildi: <b>{fmt_num(essay_to_75(score))}/{ESSAY_SCALE}</b> — "
            "natijaga va Rasch'ga qo'shildi. O'zgartirish: «📝 Esselar».",
            reply_markup=app_markup("◈ Testni ochish", t=test["id"]))
    except Exception:
        logger.warning("Esse bahosi o'qituvchiga yuborilmadi")
    if test["closed"]:
        await essay_notice(bot, test, attempt, score)


async def grade_practice(bot: Bot, practice: dict) -> None:
    """O'quvchining mustaqil essesi: AI baholaydi va o'quvchiga xabar. Bajarilmasa — ustoz krediti qaytariladi."""
    try:
        images = []
        for file_id, mime in practice.get("photos") or []:
            buffer = await bot.download(file_id)
            images.append((buffer.read(), mime))
        result = await essay_ai.grade(practice["topic"] or "", practice["text"] or None, images)
    except essay_ai.AIError as error:
        give_up = not error.retry or practice["tries"] + 1 >= ESSAY_MAX_TRIES
        await asyncio.to_thread(db.practice_failed, practice["id"], str(error), give_up)
        if give_up:
            paid = bool(practice.get("wallet"))
            if paid:  # o'quvchining krediti bilan — kredit o'quvchiga qaytadi
                await asyncio.to_thread(db.wallet_add, practice["teacher_id"], practice["student_id"], 1)
            try:
                await bot.send_message(practice["student_id"], "❗️ AI esseni tekshira olmadi — "
                                       + ("kreditingiz qaytarildi. " if paid else "ustoz krediti qaytarildi. ")
                                       + "Rasm bo'lsa, yorug' joyda tekis qilib qayta yuboring.")
            except Exception:
                pass
        if give_up and not plans.ai_unlimited(practice["teacher_id"]):
            await asyncio.to_thread(db.add_credits, practice["teacher_id"], 1)  # tekshirilmadi — kredit qaytadi
        if error.retry and not give_up:
            raise
        return
    await asyncio.to_thread(db.save_practice_result, practice["id"], result)
    try:
        words = word_count(practice["text"] or result.get("transcription") or "")
        await bot.send_message(practice["student_id"], practice_result_text(result, words),
                               reply_markup=inline([[("✍️ Yana esse yuborish", f"ecw:new:{practice['teacher_id']}")],
                                                    [("📚 Eng yaxshi esselar", "esp:0")]]))
    except Exception:
        logger.warning("Esse natijasi o'quvchiga yuborilmadi")


async def refund_essay_charge(bot: Bot, attempt_id: int, charge: str) -> None:
    """AI esseni tekshira olmadi: yechilgan kredit qaytadi — o'quvchiniki (wallet) yoki ustozniki (teacher)."""
    attempt = await asyncio.to_thread(db.get_attempt, attempt_id)
    test = attempt and await asyncio.to_thread(db.get_test, attempt["test_id"])
    if not test:
        return
    if charge == "wallet":
        await asyncio.to_thread(db.wallet_add, test["owner_id"], attempt["tg_id"], 1)
        try:
            await bot.send_message(attempt["tg_id"], f"❗️ <b>{esc(test['title'])}</b>: AI essengizni tekshira olmadi — "
                                   "1 kreditingiz qaytarildi. Essengizni ustoz baholaydi.")
        except Exception:
            logger.warning("Kredit qaytgani haqida o'quvchiga yozilmadi: %s", attempt["tg_id"])
    elif charge == "teacher" and not plans.ai_unlimited(test["owner_id"]):
        await asyncio.to_thread(db.add_credits, test["owner_id"], 1)
    logger.info("Esse krediti qaytarildi (%s): attempt=%s", charge, attempt_id)


async def grade_one(bot: Bot, essay: dict) -> None:
    try:
        images = [] if essay["text"] else await download_photos(bot, essay["photos"])
        result = await essay_ai.grade(essay["topic"] or "", essay["text"], images)
    except essay_ai.AIError as error:
        give_up = not error.retry or essay["tries"] + 1 >= ESSAY_MAX_TRIES
        charge = await asyncio.to_thread(db.essay_failed, essay["attempt_id"], str(error), give_up)
        if charge:
            await refund_essay_charge(bot, essay["attempt_id"], charge)
        if error.retry and not give_up:
            raise  # limit/tarmoq — butun navbat biroz kutadi
        return
    await asyncio.to_thread(db.save_essay_result, essay["attempt_id"], result)
    attempt = await asyncio.to_thread(db.get_attempt, essay["attempt_id"])
    # O'quvchi onlayn yozgan esse — o'zi tekshiradi: AI bahosi darhol natijaga qo'shiladi (Rasch ham shundan),
    # o'qituvchi ko'radi va xohlasa o'zgartiradi. Qog'ozdagi esse (rasm) — o'qituvchi tasdiqlaydi
    self_check = bool(attempt and attempt["tg_id"] > 0 and essay["text"])
    if (AUTO_CONFIRM_ESSAYS or self_check) and attempt and attempt["essay"] is None:
        await asyncio.to_thread(db.set_essay, essay["attempt_id"], result["score"])
        if self_check:
            await self_check_notice(bot, attempt, result["score"])
    logger.info("Esse baholandi: attempt=%s ball=%s ($%.4f)", essay["attempt_id"], result["score"], result["cost"])


# ---------- Qolgan xabarlar va xatolar ----------

# ---------- Botda esse tekshiruvi ----------
# Ustozning o'quvchisi (taklif havolasi yoki esse kodi bilan ulangan) — ustoz kreditidan, oylik chegara bilan.
# Begona (ustozning ommaviy havolasi) — PUBLIC_FREE ta bepul, keyin ustozdan kredit sotib oladi (bitta chek —
# bir nechta esse): AI tekshiradi (ustoz kreditidan ham). Esse — matn (bir necha xabar bo'lsa ham),
# rasm(lar) yoki PDF. Matn yuborilgan sari nechta so'z bo'lgani ko'rsatib turiladi. Ilovada ham — «✍️ Esse tekshirish».

PUBLIC_FREE = webapp.PUBLIC_FREE
ESSAY_MIN_WORDS = webapp.PRACTICE_MIN_WORDS
ESSAY_MAX_PHOTOS = 6


class EssayCheck(StatesGroup):
    topic = State()
    essay = State()
    receipt = State()


class EssaySetup(StatesGroup):
    price = State()
    card = State()
    packs = State()


def essay_setup(teacher_id: int) -> dict:
    saved = db.get_settings()
    return {"public": saved.get(f"essay_public:{teacher_id}") == "1",
            "price": int(saved.get(f"essay_price:{teacher_id}") or 0),
            "card": saved.get(f"teacher_card:{teacher_id}", ""), "holder": saved.get(f"teacher_holder:{teacher_id}", "")}


def word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if any(ch.isalpha() for ch in w)]) if text.strip() else 0


async def send_essay_setup(bot: Bot, chat_id: int) -> None:
    cfg = await asyncio.to_thread(essay_setup, chat_id)
    username = await bot_username(bot)
    code = await asyncio.to_thread(db.essay_code_of, chat_id)
    credits = "cheksiz" if plans.ai_unlimited(chat_id) else f"{await asyncio.to_thread(db.get_credits, chat_id)} ta"
    limit = await asyncio.to_thread(webapp.student_limit, chat_id)
    own = f"https://t.me/{username}?start={webapp.invite_code(chat_id)}"
    public = f"https://t.me/{username}?start={webapp.public_code(chat_id)}"
    card = f"<code>{esc(cfg['card'])}</code>" + (f" ({esc(cfg['holder'])})" if cfg["holder"] else "") if cfg["card"] else "qo'yilmagan"
    packs = await asyncio.to_thread(webapp.essay_packs, chat_id)
    text = (f"✍️ <b>Esse tekshiruvi</b>\n{LINE}\n"
            f"👥 <b>O'quvchilaringiz uchun</b> — bepul, kreditingizdan (har biriga oyiga {limit} ta):\n{own}\n"
            f"yoki botga kod: <code>{code}</code>\n\n"
            f"🌐 <b>Begonalar uchun</b> — {'✅ ochiq' if cfg['public'] else '⛔️ yopiq'}:\n{public}\n"
            f"{PUBLIC_FREE} ta bepul, keyin o'quvchi <b>kredit</b> sotib oladi (bitta chek — bir nechta esse):\n"
            f"📦 {esc(webapp.packs_text(packs))}\n"
            "Pul kartangizga tushadi, chekni AI tekshiradi (shubhali bo'lsa — siz tasdiqlaysiz).\n"
            f"💳 Karta: {card}\n\n"
            f"🤖 AI krediti: <b>{credits}</b> — har tekshirilgan esse 1 kredit (begonalarniki ham).\n"
            "Mock narxi alohida — test yaratishda qo'yiladi.")
    markup = inline([[("⛔️ Ommaviyni yopish" if cfg["public"] else "🌐 Ommaviyni ochish", "ecs:pub")],
                     [("💰 1 esse narxi", "ecs:price"), ("📦 Paketlar", "ecs:packs")], [("💳 Karta", "ecs:card")]])
    await bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)


@dp.message(Command("esse"))
async def cmd_essay_setup(message: Message, state: FSMContext) -> None:
    """Ustoz: esse tekshiruvi havolalari, narxi, kartasi, ommaviy rejim."""
    await state.clear()
    await asyncio.to_thread(db.remember_user, message.from_user.id, message.from_user.full_name, message.from_user.username)
    await send_essay_setup(message.bot, message.from_user.id)


@dp.callback_query(F.data.startswith("ecs:"))
async def essay_setup_button(callback: CallbackQuery, state: FSMContext) -> None:
    action, tid = callback.data.split(":")[1], callback.from_user.id
    if action == "pub":
        cfg = await asyncio.to_thread(essay_setup, tid)
        if not cfg["public"] and (not await asyncio.to_thread(webapp.essay_packs, tid) or not cfg["card"]):
            await callback.answer("Avval esse narxi (yoki paket) va kartani kiriting", show_alert=True)
            return
        await asyncio.to_thread(db.set_setting, f"essay_public:{tid}", "0" if cfg["public"] else "1")
        await callback.answer("⛔️ Ommaviy tekshiruv yopildi" if cfg["public"] else "🌐 Ommaviy tekshiruv ochildi")
        await send_essay_setup(callback.bot, tid)
        return
    if action == "price":
        await state.set_state(EssaySetup.price)
        await callback.message.answer("💰 Bitta esse tekshiruvi narxini yozing (so'mda), masalan: <code>15000</code>")
    elif action == "packs":
        await state.set_state(EssaySetup.packs)
        await callback.message.answer(
            "📦 <b>Kredit paketlari</b> — har qatorga: <i>nechta esse</i> va <i>narxi</i> (so'mda), masalan:\n"
            "<code>5 22000\n10 40000</code>\n\n"
            f"Ko'pi bilan {webapp.MAX_PACKS} ta paket, har biri 2–100 ta esse. 1 talik narx — «💰 1 esse narxi».\n"
            "Paketlarni o'chirish uchun: <code>0</code>")
    else:
        await state.set_state(EssaySetup.card)
        await callback.message.answer("💳 Karta raqami va egasining ismini yozing, masalan:\n"
                                      "<code>8600 1234 5678 9012 Aliyev Vali</code>")
    await callback.answer()


@dp.message(EssaySetup.price)
async def essay_setup_price(message: Message, state: FSMContext) -> None:
    price = int(re.sub(r"\D", "", message.text or "") or 0)
    if not 1000 <= price <= 1_000_000:
        await message.answer("Narx 1 000 dan 1 000 000 so'mgacha bo'lsin. Qayta yozing:")
        return
    await asyncio.to_thread(db.set_setting, f"essay_price:{message.from_user.id}", str(price))
    await state.clear()
    await message.answer(f"✅ Esse narxi: {plans.money(price)} so'm")
    await send_essay_setup(message.bot, message.from_user.id)


@dp.message(EssaySetup.packs)
async def essay_setup_packs(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    rows = []
    if text != "0":
        for line in text.splitlines():
            if not line.strip():
                continue
            # «5 22000», «5 ta - 22 000 so'm», «10: 40.000» — birinchi son esselar, qolgan raqamlar narx
            match = re.match(r"^\s*(\d+)\D+(\d[\d\s.,]*)", line)
            price = re.sub(r"\D", "", match.group(2)) if match else ""
            if not match or not price:
                await message.answer(f"«{esc(line.strip())}» — tushunmadim. Har qatorga: nechta esse va narxi, "
                                     "masalan <code>5 22000</code>. Qayta yozing:")
                return
            rows.append((match.group(1), price))
    try:
        packs = await asyncio.to_thread(webapp.save_packs, message.from_user.id, None, rows)
    except ValueError as error:
        await message.answer(f"⚠️ {esc(str(error))}. Qayta yozing:")
        return
    await state.clear()
    await message.answer(f"✅ Paketlar saqlandi: {esc(webapp.packs_text(packs))}")
    await send_essay_setup(message.bot, message.from_user.id)


@dp.message(EssaySetup.card)
async def essay_setup_card(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    digits = re.match(r"^[\d\s-]+", text)
    number = re.sub(r"\D", "", digits.group(0)) if digits else ""
    if len(number) != 16:
        await message.answer("Karta raqami 16 ta raqam bo'lsin. Qayta yozing:")
        return
    holder = text[digits.end():].strip()[:60]
    card = " ".join(number[i:i + 4] for i in range(0, 16, 4))
    await asyncio.to_thread(db.set_setting, f"teacher_card:{message.from_user.id}", card)
    await asyncio.to_thread(db.set_setting, f"teacher_holder:{message.from_user.id}", holder)
    await state.clear()
    await message.answer(f"✅ Karta saqlandi: <code>{card}</code>{' (' + esc(holder) + ')' if holder else ''}")
    await send_essay_setup(message.bot, message.from_user.id)


async def open_public(message: Message, code: str) -> None:
    """Ustozning ommaviy havolasi: begona o'quvchi esse tekshirtiradi (2 ta bepul, keyin to'lov)."""
    teacher_id = webapp.teacher_from_public(code)
    if not teacher_id:
        await message.answer("⌛️ Havola noto'g'ri. Ustozdan yangisini so'rang.")
        return
    if teacher_id == message.from_user.id:
        await message.answer("Bu sizning ommaviy havolangiz — uni kanal yoki guruhga tashlang. Sozlamalar: /esse")
        return
    teacher = await asyncio.to_thread(db.find_user, str(teacher_id))
    name = esc((teacher or {}).get("full_name") or "Ustoz")
    if await asyncio.to_thread(db.is_linked, teacher_id, message.from_user.id):
        await message.answer(f"Siz {name} ustozning o'quvchisisiz — esselaringiz bepul tekshiriladi.",
                             reply_markup=inline([[("✍️ Esse yuborish", f"ecw:new:{teacher_id}")]]))
        return
    cfg = await asyncio.to_thread(essay_setup, teacher_id)
    if not cfg["public"]:
        await message.answer(f"⛔️ {name} hozir ommaviy esse tekshiruvini yopgan.")
        return
    await asyncio.to_thread(db.remember_user, message.from_user.id, message.from_user.full_name, message.from_user.username)
    await asyncio.to_thread(db.remember_public, teacher_id, message.from_user.id)  # ilovada ham «✍️ Esse tekshirish»
    free_left = max(0, PUBLIC_FREE - await asyncio.to_thread(db.public_free_used, teacher_id, message.from_user.id))
    balance = await asyncio.to_thread(db.wallet_balance, teacher_id, message.from_user.id)
    packs = await asyncio.to_thread(webapp.essay_packs, teacher_id)
    await message.answer(
        f"✍️ <b>{name} — esse tekshiruvi</b>\n{LINE}\n"
        "AI imtihondagidek 75 ballik shkalada tekshiradi va har mezon bo'yicha tahlil beradi.\n"
        + (f"🎁 Bepul: <b>{free_left} / {PUBLIC_FREE}</b> ta qoldi\n" if free_left else "")
        + (f"💳 Kreditingiz: <b>{balance} ta</b> esse\n" if balance else "")
        + f"📦 Kredit: {esc(webapp.packs_text(packs))} so'm — bitta chek, har esse 1 kredit",
        reply_markup=public_markup(teacher_id))


def public_markup(teacher_id: int) -> InlineKeyboardMarkup:
    """Ommaviy havola tugmalari; ilova ochiladigan bo'lsa — «✨ Ilovada ochish» (u yerda doim «✍️ Esse tekshirish»)."""
    markup = inline([[("✍️ Esse yuborish", f"ecw:new:{teacher_id}")], [("💳 Kredit olish", f"wb:{teacher_id}")],
                     [("📚 Esse namunalari", "esp:0")]])
    url = app_url(v="practice")
    if url:
        markup.inline_keyboard.append([InlineKeyboardButton(text="✨ Ilovada ochish", web_app=WebAppInfo(url=url))])
    return markup


def packs_markup(teacher_id: int, packs: list[dict]) -> InlineKeyboardMarkup:
    return inline([[(f"{p['essays']} ta — {plans.money(p['price'])} so'm" + (f" (−{p['discount']}%)" if p["discount"] else ""),
                     f"wbp:{teacher_id}:{p['essays']}")] for p in packs])


@dp.callback_query(F.data.startswith("wb:"))
async def wallet_buy_button(callback: CallbackQuery) -> None:
    """«💳 Kredit olish»: ustozning paketlari tugma bo'lib chiqadi."""
    teacher_id = int(callback.data.split(":")[1])
    packs = await asyncio.to_thread(webapp.essay_packs, teacher_id)
    if not packs or not (await asyncio.to_thread(essay_setup, teacher_id))["card"]:
        await callback.answer("Ustoz hali kredit narxini qo'ymagan", show_alert=True)
        return
    balance = await asyncio.to_thread(db.wallet_balance, teacher_id, callback.from_user.id)
    await callback.message.answer(f"💳 <b>Esse krediti</b>{f' · hozir {balance} ta' if balance else ''}\n"
                                  "Paketni tanlang — bot karta va summani yozadi, chekni shu yerga yuborasiz:",
                                  reply_markup=packs_markup(teacher_id, packs))
    await callback.answer()


@dp.callback_query(F.data.startswith("wbp:"))
async def wallet_pack_button(callback: CallbackQuery) -> None:
    _, teacher, essays = (callback.data.split(":") + ["", ""])[:3]
    user = callback.from_user
    try:
        await webapp.wallet_order(int(teacher), {"id": user.id, "name": user.full_name, "username": user.username},
                                  int(essays or 1))  # karta va summa xabarini o'zi yuboradi
    except webapp.ApiError as error:
        await callback.answer(json.loads(error.text).get("error", "Xato"), show_alert=True)
        return
    await callback.answer("💳 Karta va summa yuborildi")


async def essay_kind(teacher_id: int, student_id: int) -> str:
    return "student" if await asyncio.to_thread(db.is_linked, teacher_id, student_id) else "public"


@dp.callback_query(F.data.startswith("ecw:"))
async def essay_flow_button(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action = parts[1]
    if action == "new":
        teacher_id, student_id = int(parts[2]), callback.from_user.id
        if not essay_ai.essay_enabled():
            await callback.answer("AI esse tekshiruvi hozir o'chiq", show_alert=True)
            return
        kind = await essay_kind(teacher_id, student_id)
        if kind == "public" and not (await asyncio.to_thread(essay_setup, teacher_id))["public"]:
            await callback.answer("Ustoz ommaviy tekshiruvni yopgan", show_alert=True)
            return
        if kind == "student":
            limit = await asyncio.to_thread(webapp.student_limit, teacher_id)
            if await asyncio.to_thread(db.practice_used, teacher_id, student_id, plans.month_key()) >= limit:
                await callback.answer(f"Bu oy uchun chegara tugadi ({limit} ta esse)", show_alert=True)
                return
        if not await asyncio.to_thread(plans.can_use_ai, teacher_id):
            await callback.answer("Ustozning AI krediti tugagan — ustozga ayting", show_alert=True)
            return
        await asyncio.to_thread(db.remember_user, student_id, callback.from_user.full_name, callback.from_user.username)
        await state.set_state(EssayCheck.topic)
        await state.set_data({"teacher_id": teacher_id, "kind": kind, "parts": [], "photos": []})
        await callback.message.answer("1️⃣ <b>Mavzu</b> — vaziyat matnini yuboring (AI shu mavzuga mosligini ham tekshiradi).",
                                      reply_markup=inline([[("⏭ Mavzusiz", "ecw:skip")]]))
        await callback.answer()
        return
    if action == "skip" and await state.get_state() == EssayCheck.topic.state:
        await ask_essay(callback.message, state, None)
    elif action == "reset" and await state.get_state() == EssayCheck.essay.state:
        await state.update_data(parts=[], photos=[])
        await callback.message.answer("🗑 Tozalandi — esseni qaytadan yuboring.")
    elif action == "send" and await state.get_state() == EssayCheck.essay.state:
        await submit_essay(callback.message, state, callback.from_user)
    await callback.answer()


async def ask_essay(message: Message, state: FSMContext, topic: str | None) -> None:
    await state.update_data(topic=topic)
    await state.set_state(EssayCheck.essay)
    await message.answer(
        "2️⃣ <b>Esse</b> — quyidagilardan birini yuboring:\n"
        "• ✍️ matn (uzun bo'lsa, bir necha xabarda — so'zlar sanab boriladi);\n"
        f"• 📷 qog'ozdagi essening rasmi (ko'pi bilan {ESSAY_MAX_PHOTOS} ta);\n"
        "• 📄 PDF fayl.\n"
        "Tayyor bo'lgach «✅ Tekshirtirish»ni bosing.")


@dp.message(EssayCheck.topic)
async def essay_topic(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Mavzu matnini yozing yoki «⏭ Mavzusiz»ni bosing.")
        return
    await ask_essay(message, state, text[:1500])


def essay_buttons() -> InlineKeyboardMarkup:
    return inline([[("✅ Tekshirtirish", "ecw:send"), ("🗑 Qaytadan", "ecw:reset")]])


@dp.message(EssayCheck.essay)
async def essay_part(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if message.text and not message.text.startswith("/"):
        if data["photos"]:
            await message.answer("Rasm yuborilgan — matn qo'shib bo'lmaydi. «🗑 Qaytadan» bilan tozalang.")
            return
        parts = data["parts"] + [message.text]
        if sum(len(p) for p in parts) > webapp.PRACTICE_MAX_CHARS:
            await message.answer("Esse juda uzun.")
            return
        await state.update_data(parts=parts)
        words = word_count("\n".join(parts))
        note = (f" · kamida {ESSAY_MIN_WORDS} so'z kerak" if words < ESSAY_MIN_WORDS else
                " · davomi bo'lsa yuboring" if words < 200 else "")
        await message.answer(f"📝 Hozircha: <b>{words} so'z</b>{note}", reply_markup=essay_buttons())
        return
    photo = message.photo[-1].file_id if message.photo else None
    document = message.document
    if document and (document.mime_type or "").lower() == "application/pdf":
        if data["parts"] or data["photos"]:
            await message.answer("PDF alohida yuboriladi — avval «🗑 Qaytadan» bilan tozalang.")
            return
        if (document.file_size or 0) > 10 * 1024 * 1024:
            await message.answer("PDF 10 MB dan kichik bo'lsin.")
            return
        await state.update_data(photos=[[document.file_id, "application/pdf"]])
        await submit_essay(message, state, message.from_user)
        return
    if document and (document.mime_type or "").startswith("image/"):
        photo = document.file_id
    if not photo:
        await message.answer("Esseni matn, rasm yoki PDF qilib yuboring.")
        return
    if data["parts"]:
        await message.answer("Matn yuborilgan — rasm qo'shib bo'lmaydi. «🗑 Qaytadan» bilan tozalang.")
        return
    photos = data["photos"] + [[photo, "image/jpeg"]]
    if len(photos) > ESSAY_MAX_PHOTOS:
        await message.answer(f"Ko'pi bilan {ESSAY_MAX_PHOTOS} ta rasm.", reply_markup=essay_buttons())
        return
    await state.update_data(photos=photos)
    await message.answer(f"📷 {len(photos)}-rasm qabul qilindi. Yana bo'lsa yuboring.", reply_markup=essay_buttons())


async def submit_essay(message: Message, state: FSMContext, user) -> None:
    data = await state.get_data()
    teacher_id, kind = data["teacher_id"], data["kind"]
    text = "\n".join(data["parts"]).strip()
    photos = data["photos"]
    if not text and not photos:
        await message.answer("Avval esseni yuboring.")
        return
    if text and word_count(text) < ESSAY_MIN_WORDS:
        await message.answer(f"Esse juda qisqa — {word_count(text)} so'z, kamida {ESSAY_MIN_WORDS} ta kerak.")
        return
    free = kind == "student" or await asyncio.to_thread(db.public_free_used, teacher_id, user.id) < PUBLIC_FREE
    if free:
        if not await webapp.ai_allowed(teacher_id, "essays"):  # ustozning 1 krediti
            await state.clear()
            await message.answer("⚠️ Ustozning AI krediti tugagan — esse tekshirilmadi. Ustozga ayting.")
            return
        await asyncio.to_thread(db.create_practice, teacher_id, user.id, data.get("topic"), text, kind, photos)
        await state.clear()
        AI_WAKE.set()
        await message.answer(f"⏳ <b>Esse qabul qilindi</b>{f' ({word_count(text)} so' + chr(39) + 'z)' if text else ''} — "
                             "AI tekshirmoqda, odatda 20–60 soniya. Natija shu yerga keladi.")
        return
    cfg = await asyncio.to_thread(essay_setup, teacher_id)
    packs = await asyncio.to_thread(webapp.essay_packs, teacher_id)
    has_credit = await asyncio.to_thread(db.wallet_balance, teacher_id, user.id) > 0
    if not has_credit and (not cfg["public"] or not packs or not cfg["card"]):
        await state.clear()
        await message.answer("⛔️ Ustoz hozir pullik tekshiruvni yopgan.")
        return
    # Bepullar tugagan: esse saqlanadi va kreditdan tekshiriladi; kredit bo'lmasa — kredit olgach o'zi tekshiriladi
    await asyncio.to_thread(db.create_practice, teacher_id, user.id, data.get("topic"), text, kind, photos, "awaiting_credit")
    await state.clear()
    released, teacher_out = await webapp.release_waiting(teacher_id, user.id)
    words = f" ({word_count(text)} so'z)" if text else ""
    if released:
        left = await asyncio.to_thread(db.wallet_balance, teacher_id, user.id)
        await message.answer(f"⏳ <b>Esse qabul qilindi</b>{words} — 1 kredit yechildi, qoldi: <b>{left} ta</b>.\n"
                             "AI tekshirmoqda, odatda 20–60 soniya. Natija shu yerga keladi.")
    elif teacher_out:
        await message.answer("⚠️ Ustozning AI krediti hozir tugagan — kreditingiz yechilmadi, essengiz saqlab qo'yildi. "
                             "Ustozga ayting; keyingi esse yuborganingizda birinchi bo'lib shu esse tekshiriladi.")
    else:
        await message.answer(f"💳 <b>Essengiz saqlandi</b>{words}\n{LINE}\nBepul {PUBLIC_FREE} ta tekshiruv ishlatilgan. "
                             "Kredit oling — to'lov tasdiqlanishi bilan esse o'zi tekshiriladi. Keyingi esselar chek so'ramaydi:",
                             reply_markup=packs_markup(teacher_id, packs))


async def practice_receipt(message: Message, practice: dict) -> None:
    file_id = message.photo[-1].file_id if message.photo else (message.document.file_id if message.document else None)
    if not file_id:
        await message.answer("Chekni rasm qilib yuboring.")
        return
    await asyncio.to_thread(db.set_practice_status, practice["id"], "awaiting_check", file_id)
    user = message.from_user
    who = esc(user.full_name) + (f" (@{esc(user.username)})" if user.username else "")
    caption = (f"🧾 <b>Esse tekshiruvi to'lovi</b>\n{LINE}\n👤 {who} · <code>{user.id}</code>\n"
               f"💳 {plans.money(practice['price'])} so'm\nPul kartangizga tushganini tekshirib, qaror qiling:")
    markup = inline([[("✅ Tasdiqlash", f"ecp:ok:{practice['id']}"), ("❌ Rad etish", f"ecp:no:{practice['id']}")]])
    try:
        if message.photo:
            await message.bot.send_photo(practice["teacher_id"], file_id, caption=caption, reply_markup=markup)
        else:
            await message.bot.send_document(practice["teacher_id"], file_id, caption=caption, reply_markup=markup)
    except Exception:
        logger.warning("Esse cheki ustozga yuborilmadi: %s", practice["teacher_id"])
        await message.answer("⚠️ Chek ustozga yetkazilmadi (ustoz botni to'xtatgan bo'lishi mumkin). Ustozga yozing.")
        return
    await message.answer("🧾 Chek ustozga yuborildi. Tasdiqlashi bilan esse tekshiriladi va natija shu yerga keladi.")


@dp.message(EssayCheck.receipt, F.photo | F.document)
async def essay_receipt(message: Message, state: FSMContext) -> None:
    practice = await asyncio.to_thread(db.get_practice, (await state.get_data()).get("practice_id") or 0)
    await state.clear()
    if not practice or practice["status"] != "awaiting_pay":
        await message.answer("Bu to'lov allaqachon ko'rib chiqilgan.")
        return
    await practice_receipt(message, practice)


@dp.callback_query(F.data.startswith("ecp:"))
async def essay_payment_decision(callback: CallbackQuery) -> None:
    _, action, raw = (callback.data.split(":") + ["", ""])[:3]
    practice = await asyncio.to_thread(db.get_practice, int(raw or 0))
    if not practice or (callback.from_user.id != practice["teacher_id"] and callback.from_user.id not in ADMIN_IDS):
        await callback.answer("Topilmadi", show_alert=True)
        return
    if practice["status"] != "awaiting_check":
        await callback.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
        return
    if action == "ok":
        if not await webapp.ai_allowed(practice["teacher_id"], "essays"):
            await callback.answer("AI kreditingiz tugagan — avval kredit oling, keyin qayta bosing", show_alert=True)
            return
        await asyncio.to_thread(db.set_practice_status, practice["id"], "pending")
        AI_WAKE.set()
        note, student_text = "✅ Tasdiqlandi — esse tekshirilmoqda", "✅ To'lov tasdiqlandi — esse AI'da tekshirilmoqda, natija shu yerga keladi."
    else:
        await asyncio.to_thread(db.set_practice_status, practice["id"], "rejected")
        note, student_text = "❌ Rad etildi", "❌ Ustoz to'lovni tasdiqlamadi. Savol bo'lsa, ustozga yozing."
    await callback.answer(note)
    try:
        await callback.bot.send_message(practice["student_id"], student_text)
    except Exception:
        logger.warning("To'lov qarori o'quvchiga yuborilmadi")
    try:
        await callback.message.edit_caption(caption=f"{callback.message.html_text}\n{LINE}\n{note}", reply_markup=None)
    except TelegramBadRequest:
        pass


def practice_result_text(result: dict, words: int | None) -> str:
    lines = [f"📝 <b>Essengiz tekshirildi: {fmt_num(essay_to_75(result['score']))}/{ESSAY_SCALE}</b>"]
    if words:
        lines.append(f"✍️ {words} so'z")
    lines.append(LINE)
    for item in result.get("criteria", []):
        mark = "✅" if item["score"] == 2 else "🟡" if item["score"] == 1 else "❌"
        comment = item.get("comment", "")
        lines.append(f"{mark} <b>{esc(essay_ai.RUBRIC_TITLES.get(item['key'], item['key']))}</b>: {item['score']}/2"
                     + (f" — {esc(comment[:160])}" if comment else ""))
    if result.get("comment"):
        lines += [LINE, f"💬 {esc(result['comment'][:600])}"]
    return "\n".join(lines)[:4000]



# ---------- Esse namunalari: butun bot bo'yicha 66/75 va undan yuqori esselar ----------

SAMPLE_MIN_75 = 66
SAMPLES_PER_PAGE = 8


def sample_rows() -> list[dict]:
    raw_min = (SAMPLE_MIN_75 - 27) / 2  # essay_to_75 teskarisi: 66 → 19,5
    return db.top_essays(raw_min)


async def send_samples(message: Message, page: int = 0, edit: bool = False) -> None:
    rows = await asyncio.to_thread(sample_rows)
    if not rows:
        text = f"📚 <b>Esse namunalari</b>\nHali {SAMPLE_MIN_75} va undan yuqori ball olgan esse yo'q."
        await (message.edit_text(text) if edit else message.answer(text))
        return
    pages = (len(rows) + SAMPLES_PER_PAGE - 1) // SAMPLES_PER_PAGE
    page = max(0, min(page, pages - 1))
    chunk = rows[page * SAMPLES_PER_PAGE:(page + 1) * SAMPLES_PER_PAGE]
    lines = [f"📚 <b>Esse namunalari</b> — {SAMPLE_MIN_75}/75 va undan yuqori ({len(rows)} ta)", LINE]
    buttons = []
    for row in chunk:
        score = fmt_num(essay_to_75(row["raw"]))
        words = word_count(row["text"])
        topic = (row["topic"] or "Mavzusiz").replace("\n", " ")
        lines.append(f"🏅 <b>{score}</b> · {words} so'z · {esc(topic[:60])}{'…' if len(topic) > 60 else ''}")
        buttons.append([(f"{score} · {words} so'z · {topic[:24]}", f"esn:{row['src']}:{row['id']}")])
    nav = []
    if page > 0:
        nav.append(("⬅️", f"esp:{page - 1}"))
    if page < pages - 1:
        nav.append(("➡️", f"esp:{page + 1}"))
    if nav:
        buttons.append(nav)
    lines.append(f"\nO'qish uchun bosing 👇{f'  ({page + 1}/{pages})' if pages > 1 else ''}")
    text, markup = "\n".join(lines), inline(buttons)
    await (message.edit_text(text, reply_markup=markup) if edit else message.answer(text, reply_markup=markup))


@dp.message(Command("namunalar"))
async def cmd_samples(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_samples(message)


@dp.callback_query(F.data.startswith("esp:"))
async def samples_page(callback: CallbackQuery) -> None:
    try:
        await send_samples(callback.message, int(callback.data.split(":")[1]), edit=True)
    except TelegramBadRequest:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("esn:"))
async def sample_open(callback: CallbackQuery) -> None:
    _, src, raw = (callback.data.split(":") + ["", ""])[:3]
    rows = await asyncio.to_thread(sample_rows)
    row = next((r for r in rows if r["src"] == src and str(r["id"]) == raw), None)
    if not row:
        await callback.answer("Bu esse endi namunalar ro'yxatida yo'q", show_alert=True)
        return
    author = (row["name"] or "").split()[0] if row["name"] else ""
    head = (f"🏅 <b>{fmt_num(essay_to_75(row['raw']))}/{ESSAY_SCALE}</b> · {word_count(row['text'])} so'z"
            f" · {'Mock essesi' if src == 'm' else 'O' + chr(39) + 'quvchi essesi'}{' · ' + esc(author) if author else ''}\n"
            f"📌 <b>Mavzu:</b> {esc(row['topic'] or 'Mavzusiz')}\n{LINE}\n")
    body = esc(row["text"])
    parts, first = [], 4000 - len(head)
    parts.append(head + body[:first])
    rest = body[first:]
    while rest:
        parts.append(rest[:4000])
        rest = rest[4000:]
    for part in parts:
        await callback.message.answer(part)
    await callback.answer()


@dp.message()
async def fallback(message: Message, state: FSMContext) -> None:
    await state.clear()
    match = ESSAY_CODE.match(message.text or "")
    if match:  # o'quvchi ustozning esse kodini yozdi
        teacher_id = await asyncio.to_thread(db.teacher_of_essay_code, match.group(1))
        if teacher_id:
            await asyncio.to_thread(db.remember_user, message.from_user.id, message.from_user.full_name,
                                    message.from_user.username)
            await link_to_teacher(message, teacher_id, "esse kodingiz")
            return
        await message.answer("❌ Bunday esse kodi yo'q. Ustozingizdan kodni qayta so'rang.")
        return
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
        scan_photos=lambda chat_id, *args: send_scan_photos(bot, chat_id, *args),
        send_text=lambda chat_id, text: bot.send_message(chat_id, text),
        bot_username=lambda: bot_username(bot),
        wake_ai=AI_WAKE.set,
        essay_receipt_notice=lambda *a: essay_receipt_notice(bot, *a),
        wallet_receipt_notice=lambda *a: wallet_receipt_notice(bot, *a),
        withdraw_notice=lambda *a: withdraw_notice(bot, *a),
        platform_approved_notice=lambda *a: platform_approved_notice(bot, *a),
        pass_receipt_notice=lambda *a: pass_receipt_notice(bot, *a),
        store_file=lambda chat_id, data, name, caption: store_file(bot, chat_id, data, name, caption),
        download_file=lambda file_id: download_bytes(bot, file_id),
        essay_scan_start=lambda *a: essay_scan_start(bot, *a),
        essay_scan_notice=lambda *a: essay_scan_notice(bot, *a),
        join_notice=lambda *a: join_notice(bot, *a),
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
            BotCommand(command="namunalar", description="Esse namunalari: 66/75 va undan yuqori esselar"),
            BotCommand(command="esse", description="Ustoz: esse tekshiruvi — havolalar, narx, karta"),
            BotCommand(command="kod", description="Esse kodi: ustoz — o'z kodini oladi, o'quvchi — kod bilan ulanadi"),
            BotCommand(command="chiqish", description="Skaner ilovasidagi barcha qurilmalardan chiqish"),
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
        asyncio.create_task(resume_announcements(bot)),  # tugamay qolgan e'lonlar
        asyncio.create_task(mock_worker(bot)),  # «Bugungi mock»: eslatmalar, yopilgach natija va sertifikatlar
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
    