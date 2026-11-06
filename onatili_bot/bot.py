"""
Ona tili test boti — Telegram Mini App uchun "eshik".

Hamma ish ilovada (webapp/index.html): test yaratish, javob berish, natijalar,
esse baholash. Bot faqat:
  • /start — salomlashish va ilovani ochish tugmasi («Ochish» menyu tugmasi ham)
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
from aiogram.filters import Command, CommandStart
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
import pdfs
import sheet
import subscription
import webapp
from config import ADMIN_IDS, BOT_TOKEN, WEBAPP_URL
from scoring import rasch_results, total_points
from structure import ESSAY_MAX, choice_items, fmt_num, grade, max_points, percent, text_items

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("onatili_bot")

# 1 — AI bahosi o'qituvchi tasdiqlamasa ham darhol qo'llanadi; 0 — o'qituvchi tasdiqlaydi (tavsiya)
AUTO_CONFIRM_ESSAYS = os.getenv("ESSAY_AUTO_CONFIRM", "0") == "1"

dp = Dispatcher()
dp.message.filter(F.chat.type == "private")
LINE = "━━━━━━━━━━━━━━━━"


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


def app_markup(text: str = "📱 Ilovani ochish", **params) -> InlineKeyboardMarkup | None:
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
    await send_welcome(message, user)


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
    await message.answer("Boshlash uchun tugmani bosing 👇" if app_url() else "⚠️ Ilova hali sozlanmagan (WEBAPP_URL).",
                         reply_markup=app_markup())


@dp.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    """Admin: baza nusxasini hozir olish (har kuni 03:00 da o'zi ham keladi)."""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Bu buyruq faqat admin uchun.")
        return
    note = await message.answer("💾 Nusxa tayyorlanmoqda…")
    sent = await ops.send_backup(message.bot, [message.chat.id])
    await note.edit_text("✅ Nusxa yuborildi" if sent else "❌ Nusxani yuborib bo'lmadi — Render → Logs")


@dp.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"🆔 Telegram ID'ingiz: <code>{message.from_user.id}</code>\n"
                         "Admin bo'lish uchun shu raqamni Render'da ADMIN_IDS o'zgaruvchisiga yozing.")


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
        status = ("pending" if essay_ai.essay_enabled() else "manual") if (essay_text or photos) else "none"
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
            reply_markup=app_markup("📱 Testni ochish", t=test["id"]),
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
                     + (f" · 📝 Esse: <b>{fmt_num(attempt['essay'])}</b>/{ESSAY_MAX}" if attempt["essay"] is not None else ""))
        lines.append(f"💯 Ball: <b>{fmt_num(total_points(attempt))}</b> / {fmt_num(top)}")
        if rasch:
            lines.append(f"🎓 Rasch bali: <b>{fmt_num(rasch[0])}</b> · Daraja: <b>{rasch[1]}</b>")
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
                reply_markup=app_markup("📱 Natija kartasini ochish", r=attempt["id"]),
            )
            sent += 1
        except Exception:
            logger.warning("Qatnashchiga xabar yuborilmadi: %s", attempt["tg_id"])
        await asyncio.sleep(0.05)
    await bot.send_message(owner_chat, f"📨 <b>{esc(test['title'])}</b>: natijalar <b>{sent}</b> ta o'quvchiga yuborildi.",
                           reply_markup=app_markup("📱 Statistikani ochish", t=test["id"]))
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
            f"✍️ <b>{esc(test['title'])}</b>\nEsse bahoyingiz yangilandi: <b>{fmt_num(score)}/{ESSAY_MAX}</b>\n"
            f"💯 Jami ball: <b>{fmt_num(total_points(attempt))}/{top}</b>",
            reply_markup=app_markup("📱 Natija kartasi", r=attempt["id"]),
        )
    except Exception:
        logger.warning("Esse bali o'quvchiga yuborilmadi")


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
        await bot.send_message(test["owner_id"], text, reply_markup=app_markup("📱 Testni ochish", t=test["id"]))
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
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=app_markup("📱 Testni ochish", t=test["id"]))


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
    await message.answer("📱 Hamma ish ilovada — pastdagi «Ochish» tugmasi yoki quyidagi tugma orqali.",
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
        bot_username=lambda: bot_username(bot),
        announce=lambda test, owner: announce_test(bot, test, owner),
        send_pdf=lambda chat_id, test, kind: send_pdf(bot, chat_id, test, kind),
        essay_notice=lambda test, attempt, score: essay_notice(bot, test, attempt, score),
        missing=lambda user_id: subscription.missing_channels(bot, user_id),
        missing_in=lambda user_id, channels, recheck=False: test_channels_missing(bot, user_id, channels, recheck),
        resolve_channel=lambda ref: subscription.resolve_channel(bot, ref),
    )
    port = os.getenv("PORT")
    if port:  # avval port — Render uni darhol ko'rsin
        await webapp.start(int(port))
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Ilovani ochish")])
    except Exception:
        logger.warning("Buyruqlar ro'yxatini o'rnatib bo'lmadi", exc_info=True)
    if app_url():
        try:  # xabar maydoni yonidagi «Ochish» tugmasi — Mini App
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Ochish", web_app=WebAppInfo(url=app_url())))
        except Exception:
            logger.warning("Menyu tugmasini o'rnatib bo'lmadi", exc_info=True)
    else:
        logger.warning("WEBAPP_URL (https) yo'q — Mini App tugmasi o'rnatilmadi")
    logger.info("Bot ishga tushdi")
    tasks = [
        asyncio.create_task(essay_worker(bot)),   # AI navbati: yozma javoblar va esselar
        asyncio.create_task(ops.alert_sender(bot)),  # xatolar — adminga Telegram'da
        asyncio.create_task(ops.daily_backup(bot)),  # har kuni baza nusxasi — adminga
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
    