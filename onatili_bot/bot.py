"""
Ona tili test boti.

O'qituvchi: test yaratadi (A–D variant kalitlari) → bot test kodi, titul PDF
va kalit PDF beradi → natijalarni PDF jadvalda ko'radi, esseni baholaydi.
O'quvchi: test kodi va javoblarini yuboradi → bot darhol tekshiradi.

Ishga tushirish:  python bot.py
"""

import asyncio
import html
import logging
import os
import re
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import db
import pdfs
from config import BOT_TOKEN
from scoring import rasch_results, total_points
from structure import (
    fmt_num,
    max_points,
    ESSAY_MAX,
    VARIANT_NAMES,
    AnswerError,
    build_items,
    choice_items,
    format_title,
    grade,
    parse_choices,
    parse_texts,
    percent,
    split_options,
    text_items,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("onatili_bot")

dp = Dispatcher()
dp.message.filter(F.chat.type == "private")

# ---------- Tugmalar ----------

BTN_CREATE = "➕ Test yaratish"
BTN_MY_TESTS = "📋 Testlarim"
BTN_CHECK = "✅ Javobni tekshirish"
BTN_HELP = "📘 Yo'riqnoma"
BTN_CANCEL = "❌ Bekor qilish"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CHECK)],
        [KeyboardButton(text=BTN_CREATE), KeyboardButton(text=BTN_MY_TESTS)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows]
    )


def esc(text: str) -> str:
    return html.escape(text or "")


class Create(StatesGroup):
    title = State()
    fmt = State()
    count = State()
    choices = State()
    texts = State()
    more = State()


class Check(StatesGroup):
    code = State()
    variant = State()
    name = State()
    choices = State()
    texts = State()


class Essay(StatesGroup):
    score = State()


HELP_TEXT = (
    "📘 <b>Yo'riqnoma</b>\n\n"
    "<b>O'qituvchi uchun</b>\n"
    f"1. «{BTN_CREATE}» → test nomi → turi (Milliy sertifikat yoki oddiy test).\n"
    "2. Variant kalitini yuboring: <code>abcdabcd...</code> yoki <code>1a2b3c...</code>\n"
    "   Milliy sertifikatda 33–35 javoblari A–F, so'ng 36–44 yozma javoblar har biri yangi qatorda.\n"
    "   Har bir yozma savolga 3 tagacha to'g'ri javob (sinonim) — <code>|</code> yoki <code>/</code> bilan: "
    "<code>chiroyli | go'zal | ko'rkam</code> — o'quvchi bittasini yozsa ham to'g'ri.\n"
    "3. Xohlasangiz B, C, D variantlarni ham qo'shing.\n"
    "4. Bot test kodi, titul varaq (PDF) va kalit (PDF) beradi. Titulni chop etib tarqating.\n"
    f"5. «{BTN_MY_TESTS}» → natijalar PDF, esse baholash (0–24, masalan 18,5), testni yakunlash.\n"
    "   Yakunlanganda har bir o'quvchiga Rasch bali, daraja va to'g'ri javoblar yuboriladi.\n\n"
    "<b>Milliy sertifikat mock haqida</b>\n"
    "• 45 ta topshiriq, 180 daqiqa; ballar rasmiy namunadagidek, jami 100 (esse — 24)\n"
    "• 37-savol kabi tinish belgili javoblar ham tekshiriladi: <code>: \" , \" .</code>\n"
    "• Daraja (Rasch bali): A+ 70+ · A 65 · B+ 60 · B 55 · C+ 50 · C 46 · undan past — NC\n"
    "• Rasch bali qatnashchilar natijasidan hisoblanadi — taxminiy, rasmiy natija emas\n\n"
    "<b>O'quvchi uchun</b>\n"
    f"«{BTN_CHECK}» → test kodi → variant → ism → javoblar.\n"
    "Tez usul: <code>12345*abcdabcd...</code> (kod * javoblar)\n"
    "Javob bermagan savolga <code>-</code> qo'ying. Har testga bir marta javob yuboriladi."
)


# ---------- Umumiy buyruqlar (istalgan holatda ishlaydi) ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    await asyncio.to_thread(db.remember_user, user.id, user.full_name, user.username)
    await message.answer(
        f"Assalomu alaykum, {esc(user.first_name)}! 👋\n\n"
        "📚 <b>Ona tili test boti</b>ga xush kelibsiz.\n\n"
        "• O'qituvchilar — test yaratadi, titul va kalitni PDF ko'rinishida oladi\n"
        "• O'quvchilar — test kodi va javoblarini yuborib, natijani darhol bilib oladi\n\n"
        "Pastdagi tugmalardan birini tanlang.",
        reply_markup=MAIN_KB,
    )


@dp.message(Command("help", "yordam"))
@dp.message(F.text == BTN_HELP)
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=MAIN_KB)


@dp.message(Command("cancel"))
@dp.message(F.text == BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=MAIN_KB)


# ---------- Test yaratish ----------

@dp.message(F.text == BTN_CREATE)
async def create_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await asyncio.to_thread(db.remember_user, message.from_user.id, message.from_user.full_name, message.from_user.username)
    await state.set_state(Create.title)
    await message.answer("📝 Test nomini yozing.\nMasalan: <i>9-sinf, 1-chorak nazorat ishi</i>", reply_markup=CANCEL_KB)


@dp.message(Create.title, F.text)
async def create_title(message: Message, state: FSMContext) -> None:
    title = " ".join(message.text.split())[:80]
    if len(title) < 2:
        await message.answer("Nom juda qisqa. Qaytadan yozing.")
        return
    await state.update_data(title=title)
    await state.set_state(Create.fmt)
    await message.answer(
        "Test turini tanlang:",
        reply_markup=inline([
            [("🏅 Milliy sertifikat (45 savol)", "fmt:milliy")],
            [("📝 Oddiy test (faqat A–D)", "fmt:oddiy")],
        ]),
    )


@dp.callback_query(Create.fmt, F.data.startswith("fmt:"))
async def create_fmt(callback: CallbackQuery, state: FSMContext) -> None:
    fmt = callback.data.split(":", 1)[1]
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if fmt == "milliy":
        await state.update_data(fmt="milliy", count=45, keys={})
        await callback.message.answer(
            "🏅 <b>Milliy sertifikat (mock)</b> — UZBMB rasmiy namunasi bo'yicha, 180 daqiqa:\n"
            "• 1–32 — test (A–D), 33–35 — moslashtirish (A–F)\n"
            "• 36–39 — yozma (bitta javob), 40–44 — yozma (a va b)\n"
            "• 45 — esse (24 ball, o'zingiz baholaysiz)\n"
            "Ballar rasmiy namunadagidek (1,1 / 1,7 / 2,5), jami 100. "
            "Test yakunlanganda Rasch bali va daraja (A+ … C) hisoblanadi."
        )
        await _ask_key_choices(callback.message, state)
    else:
        await state.update_data(fmt="oddiy")
        await state.set_state(Create.count)
        await callback.message.answer("Nechta savol? (5 dan 100 gacha son yozing)")


@dp.message(Create.count, F.text)
async def create_count(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text.isdigit() or not 5 <= int(text) <= 100:
        await message.answer("5 dan 100 gacha son yozing.")
        return
    await state.update_data(count=int(text), keys={})
    await _ask_key_choices(message, state)


def _items_from_data(data: dict) -> list[dict]:
    return build_items(data["fmt"], data["count"])


async def _ask_key_choices(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    variant = VARIANT_NAMES[len(data["keys"])]
    count = len(choice_items(_items_from_data(data)))
    extra = "\n33–35 javoblari A–F bo'lishi mumkin." if data["fmt"] == "milliy" else ""
    await state.set_state(Create.choices)
    await message.answer(
        f"🔑 <b>{variant} variant</b> — 1–{count} test javoblarini yuboring.\n"
        f"Masalan: <code>abcdabcd...</code> ({count} ta harf) yoki <code>1a2b3c...</code>{extra}",
        reply_markup=CANCEL_KB,
    )


@dp.message(Create.choices, F.text)
async def create_choices(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    items = _items_from_data(data)
    try:
        answers = parse_choices(message.text, items)
    except AnswerError as error:
        await message.answer(f"❗️ {esc(str(error))}\nQaytadan yuboring.")
        return
    await state.update_data(current=answers)
    if text_items(items):
        labels = [item["label"] for item in text_items(items)]
        await state.set_state(Create.texts)
        await message.answer(
            f"✍️ Endi yozma savollar javoblarini yuboring — <b>har biri yangi qatorda</b>, {len(labels)} qator:\n"
            f"<code>{', '.join(labels)}</code>\n\n"
            "Har biriga <b>3 tagacha</b> to'g'ri javob (sinonim) yozish mumkin — <code>|</code> yoki <code>/</code> bilan. "
            "O'quvchi ulardan bittasini yozsa ham to'g'ri hisoblanadi.\n\n"
            "Masalan:\n<code>36) kelishik\n37) chiroyli | go'zal | ko'rkam\n38) do'st / o'rtoq\n...</code>"
        )
        return
    await _key_done(message, state)


@dp.message(Create.texts, F.text)
async def create_texts(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        answers = parse_texts(message.text, _items_from_data(data))
    except AnswerError as error:
        await message.answer(f"❗️ {esc(str(error))}\nQaytadan yuboring.")
        return
    await state.update_data(current={**data["current"], **answers})
    await _key_done(message, state)


async def _key_done(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    keys = dict(data["keys"])
    variant = VARIANT_NAMES[len(keys)]
    keys[variant] = data["current"]
    await state.update_data(keys=keys, current=None)
    if len(keys) >= len(VARIANT_NAMES):
        await _save_test(message, state, message.chat.id)
        return
    next_variant = VARIANT_NAMES[len(keys)]
    await state.set_state(Create.more)
    await message.answer(
        f"✅ {variant} variant kaliti qabul qilindi.",
        reply_markup=inline([
            [(f"➕ {next_variant} variant qo'shish", "more:add")],
            [("💾 Saqlash va PDF olish", "more:save")],
        ]),
    )


@dp.callback_query(Create.more, F.data.startswith("more:"))
async def create_more(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if callback.data == "more:add":
        await _ask_key_choices(callback.message, state)
    else:
        await _save_test(callback.message, state, callback.from_user.id)


async def _save_test(message: Message, state: FSMContext, owner_id: int) -> None:
    data = await state.get_data()
    await state.clear()
    items = _items_from_data(data)
    test = await asyncio.to_thread(db.create_test, owner_id, data["title"], data["fmt"], data["count"], items, data["keys"])
    names = list(data["keys"])
    await message.answer(
        f"🎉 <b>Test yaratildi!</b>\n\n"
        f"📝 {esc(test['title'])}\n"
        f"📋 {format_title(test['fmt'], test['count'])}\n"
        f"🔢 Test kodi: <code>{test['code']}</code>\n"
        f"🅰️ Variantlar: {', '.join(names)}\n\n"
        f"O'quvchilar botga «{BTN_CHECK}» orqali yoki <code>{test['code']}*javoblar</code> ko'rinishida yuboradi.\n"
        "Titul varaq va kalitni yuboryapman 👇",
        reply_markup=MAIN_KB,
    )
    await _send_sheet(message, test, names)
    await _send_key(message, test)


async def _send_sheet(message: Message, test: dict, names: list[str]) -> None:
    content = await asyncio.to_thread(pdfs.answer_sheet_pdf, test, names)
    await message.answer_document(
        BufferedInputFile(content, filename=f"titul_{test['code']}.pdf"),
        caption="📄 Titul varaq — chop etib o'quvchilarga tarqating.",
    )


async def _send_key(message: Message, test: dict) -> None:
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    content = await asyncio.to_thread(pdfs.answer_key_pdf, test, keys)
    await message.answer_document(
        BufferedInputFile(content, filename=f"kalit_{test['code']}.pdf"),
        caption="🔑 Javoblar kaliti — faqat o'zingiz uchun.",
        protect_content=True,
    )


# ---------- Testlarim ----------

@dp.message(F.text == BTN_MY_TESTS)
async def my_tests(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_tests(message, message.from_user.id)


async def _show_tests(message: Message, owner_id: int, edit: bool = False) -> None:
    tests = await asyncio.to_thread(db.list_tests, owner_id)
    if not tests:
        text, markup = f"Sizda hali test yo'q. «{BTN_CREATE}» tugmasini bosing.", None
    else:
        text = "📋 <b>Testlaringiz</b> (oxirgi 20 ta):"
        markup = inline([
            [(f"{'🔒' if t['closed'] else '🟢'} {t['title'][:28]} · {t['code']} · 👥{t['students']}", f"t:{t['id']}")]
            for t in tests
        ])
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _own_test(callback: CallbackQuery) -> dict | None:
    test_id = int(callback.data.split(":")[1])
    test = await asyncio.to_thread(db.get_test, test_id)
    if not test or test["owner_id"] != callback.from_user.id:
        await callback.answer("Test topilmadi", show_alert=True)
        return None
    return test


def _test_card(test: dict, keys: dict, students: int) -> tuple[str, InlineKeyboardMarkup]:
    status = "🔒 Yakunlangan (javob qabul qilinmaydi)" if test["closed"] else "🟢 Ochiq (javob qabul qilinadi)"
    text = (
        f"📝 <b>{esc(test['title'])}</b>\n"
        f"📋 {format_title(test['fmt'], test['count'])}\n"
        f"🔢 Kod: <code>{test['code']}</code>\n"
        f"🅰️ Variantlar: {', '.join(keys)}\n"
        f"👥 Qatnashchilar: {students}\n"
        f"{status}\n🗓 {test['created_at']}"
    )
    tid = test["id"]
    rows = [
        [("📄 Titul varaq", f"sheet:{tid}"), ("🔑 Kalit", f"key:{tid}")],
        [("📊 Natijalar (PDF)", f"res:{tid}")],
    ]
    if test["fmt"] == "milliy":
        rows.append([("✍️ Esseni baholash", f"essay:{tid}")])
    rows.append([("🔓 Qayta ochish", f"open:{tid}") if test["closed"] else ("🔒 Yakunlash", f"close:{tid}"),
                 ("🗑 O'chirish", f"del:{tid}")])
    rows.append([("⬅️ Orqaga", "back")])
    return text, inline(rows)


async def _render_card(callback: CallbackQuery, test: dict) -> None:
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    text, markup = _test_card(test, keys, len(attempts))
    await callback.message.edit_text(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("t:"))
async def test_card(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await callback.answer()
        await _render_card(callback, test)


@dp.callback_query(F.data == "back")
async def back_to_list(callback: CallbackQuery) -> None:
    await callback.answer()
    await _show_tests(callback.message, callback.from_user.id, edit=True)


@dp.callback_query(F.data.startswith("sheet:"))
async def send_sheet(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await callback.answer("Tayyorlanmoqda…")
        keys = await asyncio.to_thread(db.get_keys, test["id"])
        await _send_sheet(callback.message, test, list(keys))


@dp.callback_query(F.data.startswith("key:"))
async def send_key(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await callback.answer("Tayyorlanmoqda…")
        await _send_key(callback.message, test)


async def _send_results(message: Message, test: dict) -> None:
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    content = await asyncio.to_thread(pdfs.results_pdf, test, attempts)
    await message.answer_document(
        BufferedInputFile(content, filename=f"natijalar_{test['code']}.pdf"),
        caption=f"📊 {esc(test['title'])} — {len(attempts)} ta qatnashchi",
    )


@dp.callback_query(F.data.startswith("res:"))
async def send_results(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await callback.answer("Tayyorlanmoqda…")
        await _send_results(callback.message, test)


@dp.callback_query(F.data.startswith("close:"))
async def close_test(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if not test:
        return
    await asyncio.to_thread(db.set_closed, test["id"], True)
    test["closed"] = 1
    await callback.answer("Test yakunlandi")
    await _render_card(callback, test)
    await _send_results(callback.message, test)
    await _notify_participants(callback.bot, test)


async def _notify_participants(bot: Bot, test: dict) -> None:
    """Test yakunlanganda har bir qatnashchiga o'z varianti bo'yicha to'g'ri javoblar yuboriladi."""
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    rasch = rasch_results(test, attempts) if test["fmt"] == "milliy" else {}
    choices, texts = choice_items(test["items"]), text_items(test["items"])
    for attempt in attempts:
        key = keys.get(attempt["variant"], {})
        line = "".join(key.get(item["label"], "?") for item in choices).lower()
        written = "\n".join(f"{item['label']}) {esc(' / '.join(split_options(key.get(item['label'], ''))))}" for item in texts)
        text = (
            f"🏁 <b>{esc(test['title'])}</b> testi yakunlandi.\n\n"
            f"{_result_text(attempt, test, rasch.get(attempt['id']))}\n\n"
            f"🔑 To'g'ri javoblar ({attempt['variant']} variant):\n<code>{line}</code>"
        )
        if written:
            text += f"\n\n✍️ Yozma:\n{written}"
        try:
            await bot.send_message(attempt["tg_id"], text[:4000])
        except Exception:
            logger.warning("Qatnashchiga xabar yuborilmadi: %s", attempt["tg_id"])
        await asyncio.sleep(0.05)


@dp.callback_query(F.data.startswith("open:"))
async def reopen_test(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await asyncio.to_thread(db.set_closed, test["id"], False)
        test["closed"] = 0
        await callback.answer("Test qayta ochildi")
        await _render_card(callback, test)


@dp.callback_query(F.data.startswith("del:"))
async def delete_ask(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await callback.answer()
        await callback.message.edit_text(
            f"🗑 <b>{esc(test['title'])}</b> ({test['code']}) testini va barcha natijalarini o'chirasizmi?",
            reply_markup=inline([[("✅ Ha, o'chirish", f"delok:{test['id']}"), ("❌ Yo'q", f"t:{test['id']}")]]),
        )


@dp.callback_query(F.data.startswith("delok:"))
async def delete_confirm(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await asyncio.to_thread(db.delete_test, test["id"])
        await callback.answer("O'chirildi")
        await _show_tests(callback.message, callback.from_user.id, edit=True)


# ---------- Esse baholash ----------

@dp.callback_query(F.data.startswith("essay:"))
async def essay_list(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if not test:
        return
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    if not attempts:
        await callback.answer("Hali hech kim javob yubormagan", show_alert=True)
        return
    await callback.answer()
    rows = [
        [(f"{'✍️' if a['essay'] is None else '✅ ' + fmt_num(a['essay'])} · {a['student_name'][:30]}", f"es:{a['id']}")]
        for a in attempts[:90]
    ]
    rows.append([("⬅️ Orqaga", f"t:{test['id']}")])
    await callback.message.edit_text(
        f"✍️ Esse (45-savol) uchun 0–{ESSAY_MAX} ball qo'ying. O'quvchini tanlang:", reply_markup=inline(rows)
    )


@dp.callback_query(F.data.startswith("es:"))
async def essay_pick(callback: CallbackQuery, state: FSMContext) -> None:
    attempt = await asyncio.to_thread(db.get_attempt, int(callback.data.split(":")[1]))
    test = attempt and await asyncio.to_thread(db.get_test, attempt["test_id"])
    if not test or test["owner_id"] != callback.from_user.id:
        await callback.answer("Topilmadi", show_alert=True)
        return
    await callback.answer()
    await state.set_state(Essay.score)
    await state.update_data(attempt_id=attempt["id"])
    await callback.message.answer(
        f"👤 <b>{esc(attempt['student_name'])}</b>\nEsse balini yozing (0–{ESSAY_MAX}):", reply_markup=CANCEL_KB
    )


@dp.message(Essay.score, F.text)
async def essay_score(message: Message, state: FSMContext) -> None:
    # Ikki ekspert bahosining o'rtachasi kasr bo'lishi mumkin: 18,5
    text = message.text.strip().replace(",", ".")
    try:
        score = float(text)
    except ValueError:
        score = -1
    if not 0 <= score <= ESSAY_MAX or round(score * 2) != score * 2:
        await message.answer(f"0 dan {ESSAY_MAX} gacha son yozing (0,5 qadam bilan: 18 yoki 18,5).")
        return
    text = fmt_num(score)
    data = await state.get_data()
    await state.clear()
    attempt = await asyncio.to_thread(db.get_attempt, data["attempt_id"])
    test = attempt and await asyncio.to_thread(db.get_test, attempt["test_id"])
    if not test or test["owner_id"] != message.from_user.id:
        await message.answer("Topilmadi.", reply_markup=MAIN_KB)
        return
    await asyncio.to_thread(db.set_essay, attempt["id"], score)
    attempt["essay"] = score
    top = fmt_num(max_points(test["items"], test["fmt"]))
    await message.answer(
        f"✅ {esc(attempt['student_name'])} — esse: {text}/{ESSAY_MAX}, jami ball: {fmt_num(total_points(attempt))}/{top}",
        reply_markup=inline([[("✍️ Keyingi o'quvchi", f"essay:{test['id']}")]]),
    )
    await message.answer("Bosh menyu", reply_markup=MAIN_KB)
    try:
        await message.bot.send_message(
            attempt["tg_id"],
            f"✍️ <b>{esc(test['title'])}</b>: esse uchun bahoyingiz — <b>{text}/{ESSAY_MAX}</b>\n"
            f"💯 Jami ball: <b>{fmt_num(total_points(attempt))}/{top}</b>",
        )
    except Exception:
        logger.warning("Esse bali o'quvchiga yuborilmadi")


# ---------- O'quvchi: javobni tekshirish ----------

CODE_PATTERN = re.compile(r"^\s*(\d{5})\s*(?:\*\s*(.+))?$", re.DOTALL)


@dp.message(F.text == BTN_CHECK)
async def check_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await asyncio.to_thread(db.remember_user, message.from_user.id, message.from_user.full_name, message.from_user.username)
    await state.set_state(Check.code)
    await message.answer(
        "🔢 Test kodini yuboring (5 xonali son).\n\n"
        "Tez usul: <code>12345*abcdabcd...</code> — kod va test javoblari birga.",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(None), F.text.regexp(r"^\s*\d{5}\s*\*"))
async def quick_check(message: Message, state: FSMContext) -> None:
    """Hech qanday bosqichda bo'lmaganda "12345*abcd..." yuborilsa — darhol tekshirishga o'tamiz."""
    await asyncio.to_thread(db.remember_user, message.from_user.id, message.from_user.full_name, message.from_user.username)
    await check_code(message, state)


@dp.message(Check.code, F.text)
async def check_code(message: Message, state: FSMContext) -> None:
    match = CODE_PATTERN.match(message.text)
    if not match:
        await message.answer("Kod 5 xonali son bo'lishi kerak. Masalan: <code>12345</code>", reply_markup=CANCEL_KB)
        return
    test = await asyncio.to_thread(db.get_test_by_code, match.group(1))
    if not test:
        await message.answer("❗️ Bunday kodli test topilmadi. Kodni tekshirib qayta yuboring.", reply_markup=CANCEL_KB)
        await state.set_state(Check.code)
        return
    if test["closed"]:
        await state.clear()
        await message.answer("🔒 Bu test yakunlangan, javob qabul qilinmaydi.", reply_markup=MAIN_KB)
        return
    previous = await asyncio.to_thread(db.find_attempt, test["id"], message.from_user.id)
    if previous:
        await state.clear()
        await message.answer(
            f"Siz bu testga javob yuborgansiz.\n\n{_result_text(previous, test)}", reply_markup=MAIN_KB
        )
        return

    await state.update_data(test_id=test["id"], pending=match.group(2))
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    await message.answer(f"📝 <b>{esc(test['title'])}</b>\n📋 {format_title(test['fmt'], test['count'])}")
    if len(keys) > 1:
        await state.set_state(Check.variant)
        await message.answer(
            "Qaysi variantni yechdingiz?", reply_markup=inline([[(f"{name} variant", f"var:{name}") for name in keys]])
        )
        return
    await state.update_data(variant=next(iter(keys)))
    await _ask_name(message, state, message.from_user.id)


@dp.callback_query(Check.variant, F.data.startswith("var:"))
async def check_variant(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    variant = callback.data.split(":", 1)[1]
    await callback.message.edit_text(f"✅ {variant} variant")
    await state.update_data(variant=variant)
    await _ask_name(callback.message, state, callback.from_user.id)


async def _ask_name(message: Message, state: FSMContext, tg_id: int) -> None:
    await state.set_state(Check.name)
    last = await asyncio.to_thread(db.last_name_used, tg_id)
    markup = inline([[(f"✅ {last[:40]}", "name:last")]]) if last else None
    await message.answer(
        "👤 Ism va familiyangizni yozing:" + ("\n(yoki avvalgisini tanlang)" if last else ""), reply_markup=markup
    )


@dp.callback_query(Check.name, F.data == "name:last")
async def check_name_last(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    name = await asyncio.to_thread(db.last_name_used, callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=None)
    await _name_done(callback.message, state, name or callback.from_user.full_name)


@dp.message(Check.name, F.text)
async def check_name(message: Message, state: FSMContext) -> None:
    name = " ".join(message.text.split())[:60]
    if len(name) < 3:
        await message.answer("Ism va familiyani to'liq yozing.")
        return
    await asyncio.to_thread(db.set_last_name_used, message.from_user.id, name)
    await _name_done(message, state, name)


async def _test_from_state(state: FSMContext) -> dict | None:
    data = await state.get_data()
    return await asyncio.to_thread(db.get_test, data.get("test_id", 0))


async def _name_done(message: Message, state: FSMContext, name: str) -> None:
    await state.update_data(name=name)
    data = await state.get_data()
    test = await _test_from_state(state)
    if not test:
        await state.clear()
        await message.answer("Test topilmadi.", reply_markup=MAIN_KB)
        return
    if data.get("pending"):
        try:
            answers = parse_choices(data["pending"], test["items"], allow_blank=True)
        except AnswerError as error:
            await state.update_data(pending=None)
            await message.answer(f"❗️ Kod bilan yuborilgan javoblarda xato: {esc(str(error))}")
        else:
            await state.update_data(pending=None)
            await _choices_done(message, state, test, answers)
            return
    count = len(choice_items(test["items"]))
    await state.set_state(Check.choices)
    await message.answer(
        f"✏️ 1–{count} test javoblaringizni yuboring.\n"
        f"Masalan: <code>abcdabcd...</code> ({count} ta harf) yoki <code>1a2b3c...</code>\n"
        "Javob bermagan savolga <code>-</code> qo'ying.",
        reply_markup=CANCEL_KB,
    )


@dp.message(Check.choices, F.text)
async def check_choices(message: Message, state: FSMContext) -> None:
    test = await _test_from_state(state)
    if not test:
        await state.clear()
        await message.answer("Test topilmadi.", reply_markup=MAIN_KB)
        return
    try:
        answers = parse_choices(message.text, test["items"], allow_blank=True)
    except AnswerError as error:
        await message.answer(f"❗️ {esc(str(error))}\nQaytadan yuboring.")
        return
    await _choices_done(message, state, test, answers)


async def _choices_done(message: Message, state: FSMContext, test: dict, answers: dict) -> None:
    await state.update_data(answers=answers)
    texts = text_items(test["items"])
    if texts:
        labels = [item["label"] for item in texts]
        await state.set_state(Check.texts)
        await message.answer(
            f"✍️ Yozma javoblaringizni yuboring — <b>har biri yangi qatorda</b>, {len(labels)} qator:\n"
            f"<code>{', '.join(labels)}</code>\n"
            "Javob bermagan bo'lsangiz — <code>-</code> yozing.",
            reply_markup=CANCEL_KB,
        )
        return
    await _finish_check(message, state, test, answers)


@dp.message(Check.texts, F.text)
async def check_texts(message: Message, state: FSMContext) -> None:
    test = await _test_from_state(state)
    if not test:
        await state.clear()
        await message.answer("Test topilmadi.", reply_markup=MAIN_KB)
        return
    try:
        written = parse_texts(message.text, test["items"], allow_blank=True)
    except AnswerError as error:
        await message.answer(f"❗️ {esc(str(error))}\nQaytadan yuboring.")
        return
    data = await state.get_data()
    await _finish_check(message, state, test, {**data["answers"], **written})


def _result_text(attempt: dict, test: dict, rasch: tuple[float, str] | None = None) -> str:
    correct = attempt["test_correct"] + attempt["written_correct"]
    total = attempt["auto_total"]
    choices, texts = choice_items(test["items"]), text_items(test["items"])
    lines = [f"👤 <b>{esc(attempt['student_name'])}</b> · {attempt['variant']} variant"]
    if test["fmt"] != "milliy":
        lines.append(f"📊 To'g'ri: <b>{correct}/{total}</b> ({percent(correct, total)}%)")
    else:
        lines.append(f"🧩 Test (1–35): <b>{attempt['test_correct']}/{len(choices)}</b>")
        lines.append(f"✍️ Yozma (36–44): <b>{attempt['written_correct']}/{len(texts)}</b>")
        essay = attempt.get("essay")
        lines.append(f"📝 Esse: <b>{fmt_num(essay)}/{ESSAY_MAX}</b>" if essay is not None else "📝 Esse: baholanmoqda")
        top = max_points(test["items"], test["fmt"])
        lines.append(f"💯 Ball: <b>{fmt_num(total_points(attempt))}/{fmt_num(top)}</b>")
        if rasch:
            lines.append(f"🎓 Rasch bali: <b>{fmt_num(rasch[0])}</b> · daraja: <b>{rasch[1]}</b>")
    lines.append(f"❌ Xato: {', '.join(attempt['wrong'])}" if attempt["wrong"] else "🏆 Hammasi to'g'ri!")
    return "\n".join(lines)


async def _finish_check(message: Message, state: FSMContext, test: dict, answers: dict) -> None:
    data = await state.get_data()
    await state.clear()
    tg_id = message.chat.id
    fresh = await asyncio.to_thread(db.get_test, test["id"])
    if not fresh or fresh["closed"]:
        await message.answer("🔒 Test yakunlangan yoki o'chirilgan, javob qabul qilinmadi.", reply_markup=MAIN_KB)
        return
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    key = keys.get(data["variant"]) or next(iter(keys.values()))
    result = grade(test["items"], key, answers)
    try:
        attempt = await asyncio.to_thread(db.save_attempt, test["id"], tg_id, data["name"], data["variant"], answers, result)
    except sqlite3.IntegrityError:
        previous = await asyncio.to_thread(db.find_attempt, test["id"], tg_id)
        await message.answer(f"Siz bu testga javob yuborgansiz.\n\n{_result_text(previous, test)}", reply_markup=MAIN_KB)
        return

    note = (
        "\n\nℹ️ Esse o'qituvchi tomonidan baholanadi. Rasch bali va daraja (A+ … C) test yakunlanganda, "
        "barcha qatnashchilar natijasi asosida yuboriladi."
        if test["fmt"] == "milliy" else ""
    )
    await message.answer(f"✅ <b>Javoblaringiz tekshirildi</b>\n\n{_result_text(attempt, test)}{note}", reply_markup=MAIN_KB)

    correct = result["test_correct"] + result["written_correct"]
    summary = (
        f"{fmt_num(result['points'])} ball (essesiz)" if test["fmt"] == "milliy"
        else f"{correct}/{result['auto_total']} ({percent(correct, result['auto_total'])}%)"
    )
    try:
        await message.bot.send_message(
            test["owner_id"],
            f"🆕 <b>{esc(test['title'])}</b> ({test['code']})\n"
            f"👤 {esc(data['name'])} · {data['variant']} variant — {summary}",
        )
    except Exception:
        logger.warning("O'qituvchiga natija haqida xabar yuborilmadi")


# ---------- Qolgan xabarlar ----------

@dp.message()
async def fallback(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer("Iltimos, matn ko'rinishida yuboring yoki «❌ Bekor qilish» ni bosing.")
        return
    await message.answer("Pastdagi tugmalardan birini tanlang 👇", reply_markup=MAIN_KB)


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
        raise SystemExit("BOT_TOKEN topilmadi: .env faylida BOT_TOKEN=... ni BotFather tokeni bilan to'ldiring.")
    db.init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await bot.set_my_commands([
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="yordam", description="Yo'riqnoma"),
        BotCommand(command="cancel", description="Bekor qilish"),
    ])
    logger.info("Bot ishga tushdi")
    await _start_health_server()
    await dp.start_polling(bot)


async def _start_health_server() -> None:
    """
    Render Web Service port ochilishini talab qiladi — PORT berilgan bo'lsa,
    kichik veb-server "Bot ishlayapti" deb javob beradi. Shu manzilni
    UptimeRobot/cron-job.org har 10 daqiqada ochib tursa, bepul tarifda bot uxlamaydi.
    """
    port = os.getenv("PORT")
    if not port:
        return
    from aiohttp import web

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="Bot ishlayapti")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    logger.info("Veb-server %s-portda ochildi", port)


if __name__ == "__main__":
    asyncio.run(main())
