"""
Ona tili test boti (Milliy sertifikat mock).

O'qituvchi: test yaratadi (A–D variant kalitlari) → bot test kodi, titul PDF
va kalit PDF beradi → natijalarni PDF jadvalda ko'radi, esseni baholaydi.
O'quvchi: test kodi va javoblarini yuboradi → natija yashirin turadi.
O'qituvchi «Natijalarni e'lon qilish»ni bosganda hammaga natijasi yuboriladi.

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
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import db
import essay_ai
import pdfs
from config import BOT_TOKEN
from scoring import rasch_results, total_points
from structure import (
    ESSAY_MAX,
    VARIANT_NAMES,
    AnswerError,
    build_items,
    choice_items,
    fmt_num,
    format_title,
    grade,
    max_points,
    parse_choices,
    parse_texts,
    percent,
    split_options,
    text_items,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("onatili_bot")

# 1 — AI bahosi o'qituvchi tasdiqlamasa ham darhol qo'llanadi; 0 — o'qituvchi tasdiqlaydi (tavsiya)
AUTO_CONFIRM_ESSAYS = os.getenv("ESSAY_AUTO_CONFIRM", "0") == "1"

dp = Dispatcher()
dp.message.filter(F.chat.type == "private")

# ---------- Tugmalar va yordamchilar ----------

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
    input_field_placeholder="Test kodi yoki 12345*abcd...",
)
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)

LINE = "━━━━━━━━━━━━━━━━"


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows]
    )


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def bar(value: float, total: float, width: int = 10) -> str:
    """Progress chizig'i: ▰▰▰▰▰▰▱▱▱▱ 62%"""
    share = value / total if total else 0
    filled = round(share * width)
    return "▰" * filled + "▱" * (width - filled) + f" {round(share * 100)}%"


def step(number: int, total: int | None, title: str) -> str:
    label = f"{number}/{total}-qadam" if total else f"{number}-qadam"
    return f"<b>{label}</b> · {title}\n{LINE}\n"


async def safe_edit(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Xabarni tahrirlaydi; o'zgarmagan bo'lsa yoki tahrirlab bo'lmasa — xato bermaydi."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "not modified" not in str(error):
            await message.answer(text, reply_markup=markup)


async def remember(message: Message) -> None:
    user = message.from_user
    await asyncio.to_thread(db.remember_user, user.id, user.full_name, user.username)


class Create(StatesGroup):
    title = State()
    fmt = State()
    topic = State()
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
    essay = State()


class Essay(StatesGroup):
    score = State()


HELP_TEXT = (
    "📘 <b>Yo'riqnoma</b>\n"
    f"{LINE}\n"
    "👩‍🏫 <b>O'qituvchi uchun</b>\n"
    f"1. «{BTN_CREATE}» → test nomi → turi\n"
    "2. Kalitni yuboring: <code>abcdabcd...</code> yoki <code>1a2b3c...</code>\n"
    "3. Yozma javoblar — har biri yangi qatorda. 3 tagacha sinonim: <code>chiroyli | go'zal | ko'rkam</code>\n"
    "4. Xohlasangiz B, C, D variantlarni qo'shing\n"
    "5. Bot test kodi, titul va kalit PDF'ini beradi — titulni chop eting\n"
    f"6. «{BTN_MY_TESTS}» → kim topshirganini kuzating, esseni baholang (0–24)\n"
    "7. Hamma topshirgach — «📢 Natijalarni e'lon qilish». Shunda har bir o'quvchiga natijasi boradi\n\n"
    "🧑‍🎓 <b>O'quvchi uchun</b>\n"
    f"«{BTN_CHECK}» → kod → variant → ism → javoblar\n"
    "⚡️ Tez usul: <code>12345*abcdabcd...</code>\n"
    "Javob bermagan savolga <code>-</code> qo'ying. Har testga bir marta javob yuboriladi.\n"
    "⏳ Natija o'qituvchi e'lon qilganda keladi.\n\n"
    "🏅 <b>Milliy sertifikat mock</b>\n"
    "• 45 topshiriq, 180 daqiqa, jami 100 ball (esse — 24)\n"
    "• Ballar UZBMB rasmiy namunasidagidek: 1,1 / 1,7 / 2,5\n"
    "• Tinish belgili javoblar ham tekshiriladi: <code>: \" , \" .</code>\n"
    "• Daraja: A+ 70 · A 65 · B+ 60 · B 55 · C+ 50 · C 46\n"
    "• Rasch bali qatnashchilar natijasidan hisoblanadi — taxminiy"
)


# ---------- Umumiy buyruqlar (istalgan holatda ishlaydi) ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await remember(message)
    await message.answer(
        "📚 <b>Ona tili · Test boti</b>\n"
        f"{LINE}\n"
        f"Assalomu alaykum, {esc(message.from_user.first_name)}! 👋\n\n"
        "Milliy sertifikat uslubidagi mock testlar uchun yordamchi.\n\n"
        "👩‍🏫 <b>O'qituvchi</b> — test yaratadi, titul va kalitni PDF'da oladi, natijalarni kuzatadi\n"
        "🧑‍🎓 <b>O'quvchi</b> — kod va javoblarni yuboradi, natijani o'qituvchi e'lon qilganda oladi\n\n"
        "Boshlash uchun pastdagi tugmani tanlang 👇",
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
    await message.answer("↩️ Bekor qilindi. Bosh menyu 👇", reply_markup=MAIN_KB)


# ---------- Test yaratish ----------

@dp.message(F.text == BTN_CREATE)
async def create_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await remember(message)
    await state.set_state(Create.title)
    await message.answer(
        step(1, None, "Test nomi")
        + "Test nomini yozing.\nMasalan: <i>Mock №3 · 11-sinf</i> yoki <i>9-sinf, 1-chorak</i>",
        reply_markup=CANCEL_KB,
    )


@dp.message(Create.title, F.text)
async def create_title(message: Message, state: FSMContext) -> None:
    title = " ".join(message.text.split())[:80]
    if len(title) < 2:
        await message.answer("✋ Nom juda qisqa. Qaytadan yozing.")
        return
    await state.update_data(title=title)
    await state.set_state(Create.fmt)
    await message.answer(
        step(2, None, "Test turi")
        + f"📝 <b>{esc(title)}</b>\n\nTest turini tanlang:\n\n"
        "🏅 <b>Milliy sertifikat</b> — 45 savol: test, moslashtirish, yozma, esse. Rasch bali va daraja\n"
        "📝 <b>Oddiy test</b> — 5–100 ta savol, faqat A–D",
        reply_markup=inline([
            [("🏅 Milliy sertifikat (45)", "fmt:milliy")],
            [("📝 Oddiy test (A–D)", "fmt:oddiy")],
        ]),
    )


@dp.callback_query(Create.fmt, F.data.startswith("fmt:"))
async def create_fmt(callback: CallbackQuery, state: FSMContext) -> None:
    fmt = callback.data.split(":", 1)[1]
    await callback.answer()
    if fmt == "milliy":
        await state.update_data(fmt="milliy", count=45, keys={})
        await safe_edit(
            callback.message,
            "🏅 <b>Milliy sertifikat (mock)</b> — UZBMB namunasi bo'yicha\n"
            f"{LINE}\n"
            "• 1–32 — test (A–D)\n"
            "• 33–35 — moslashtirish (A–F)\n"
            "• 36–39 — yozma (bitta javob)\n"
            "• 40–44 — yozma (a va b)\n"
            "• 45 — esse (24 ball" + (", AI tekshiradi, siz tasdiqlaysiz)" if essay_ai.enabled() else ", o'zingiz baholaysiz)")
            + "\n\n⏱ 180 daqiqa · 💯 jami 100 ball",
        )
        await state.set_state(Create.topic)
        await callback.message.answer(
            step(3, 5, "Esse mavzusi")
            + "45-savoldagi <b>vaziyat matnini</b> yuboring — esse shu mavzu bo'yicha tekshiriladi.\n\n"
            "<i>Masalan: Bugungi kunda televideniye orqali namoyish qilinadigan reklamalarga ko'pchilik salbiy "
            "munosabat bildiradi. Ayrimlar esa ularni kerakli deb hisoblashadi.</i>",
            reply_markup=inline([[("⏭ Mavzusiz davom etish", "topic:skip")]]),
        )
    else:
        await state.update_data(fmt="oddiy")
        await state.set_state(Create.count)
        await safe_edit(callback.message, "📝 <b>Oddiy test</b> tanlandi.")
        await callback.message.answer(step(3, 4, "Savollar soni") + "Nechta savol? 5 dan 100 gacha son yozing.")


@dp.message(Create.topic, F.text)
async def create_topic(message: Message, state: FSMContext) -> None:
    topic = message.text.strip()
    if len(topic) < 15:
        await message.answer("✋ Vaziyat matnini to'liq yozing (yoki «⏭ Mavzusiz davom etish»).")
        return
    await state.update_data(essay_topic=topic[:2000])
    await message.answer("✅ Esse mavzusi saqlandi.")
    await _ask_key_choices(message, state)


@dp.callback_query(Create.topic, F.data == "topic:skip")
async def create_topic_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(essay_topic=None)
    await _ask_key_choices(callback.message, state)


@dp.message(Create.count, F.text)
async def create_count(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text.isdigit() or not 5 <= int(text) <= 100:
        await message.answer("✋ 5 dan 100 gacha son yozing.")
        return
    await state.update_data(count=int(text), keys={})
    await _ask_key_choices(message, state)


def _items_from_data(data: dict) -> list[dict]:
    return build_items(data["fmt"], data["count"])


async def _ask_key_choices(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    variant = VARIANT_NAMES[len(data["keys"])]
    count = len(choice_items(_items_from_data(data)))
    extra = "\n🔸 33–35 javoblari A–F bo'lishi mumkin." if data["fmt"] == "milliy" else ""
    number, total = (4, 5) if data["fmt"] == "milliy" else (4, 4)
    await state.set_state(Create.choices)
    await message.answer(
        step(number, total, f"{variant} variant kaliti")
        + f"1–{count} savollarning to'g'ri javoblarini yuboring:\n\n"
        f"<code>abcdabcd...</code> — {count} ta harf ketma-ket\n"
        f"<code>1a2b3c...</code> — raqam va harf{extra}",
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
            step(5, 5, "Yozma javoblar")
            + f"36–44 javoblarini yuboring — <b>har biri yangi qatorda</b>, {len(labels)} qator:\n"
            f"<code>{', '.join(labels)}</code>\n\n"
            "🔸 Har biriga <b>3 tagacha</b> sinonim: <code>|</code> yoki <code>/</code> bilan\n"
            "🔸 O'quvchi bittasini yozsa ham to'g'ri\n\n"
            "Masalan:\n<code>36) yaqin | aziz | qadrdon\n37) : \" , \" .\n38) -adigan\n...</code>"
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
        f"✅ <b>{variant} variant</b> kaliti qabul qilindi.\n\nYana variant qo'shasizmi yoki saqlaymizmi?",
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
    test = await asyncio.to_thread(db.create_test, owner_id, data["title"], data["fmt"], data["count"], items,
                                   data["keys"], data.get("essay_topic"))
    names = list(data["keys"])
    await message.answer(
        "🎉 <b>Test tayyor!</b>\n"
        f"{LINE}\n"
        f"📝 {esc(test['title'])}\n"
        f"📋 {format_title(test['fmt'], test['count'])}\n"
        f"🅰️ Variantlar: {', '.join(names)}\n\n"
        f"🔢 Test kodi:  <code>{test['code']}</code>\n\n"
        "O'quvchilarga kodni bering. Ular javobni shunday yuboradi:\n"
        f"<code>{test['code']}*abcdabcd...</code>\n\n"
        "📄 Titul varaq va 🔑 kalit 👇",
        reply_markup=MAIN_KB,
    )
    await _send_sheet(message, test, names)
    await _send_key(message, test)


async def _send_sheet(message: Message, test: dict, names: list[str]) -> None:
    content = await asyncio.to_thread(pdfs.answer_sheet_pdf, test, names)
    await message.answer_document(
        BufferedInputFile(content, filename=f"titul_{test['code']}.pdf"),
        caption="📄 <b>Titul varaq</b> — chop etib o'quvchilarga tarqating.",
    )


async def _send_key(message: Message, test: dict) -> None:
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    content = await asyncio.to_thread(pdfs.answer_key_pdf, test, keys)
    await message.answer_document(
        BufferedInputFile(content, filename=f"kalit_{test['code']}.pdf"),
        caption="🔑 <b>Javoblar kaliti</b> — faqat o'zingiz uchun (uzatib bo'lmaydi).",
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
        text = f"📋 <b>Testlarim</b>\n{LINE}\nSizda hali test yo'q.\n«{BTN_CREATE}» tugmasini bosing."
        markup = None
    else:
        open_count = sum(1 for t in tests if not t["closed"])
        text = (
            f"📋 <b>Testlarim</b>\n{LINE}\n"
            f"Jami: {len(tests)} ta · ⏳ javob yig'ilmoqda: {open_count} · 📢 e'lon qilingan: {len(tests) - open_count}\n\n"
            "Testni tanlang:"
        )
        markup = inline([
            [(f"{'📢' if t['closed'] else '⏳'} {t['title'][:26]} · {t['code']} · 👥 {t['students']}", f"t:{t['id']}")]
            for t in tests
        ])
    if edit:
        await safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _own_test(callback: CallbackQuery) -> dict | None:
    try:
        test_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        test_id = 0
    test = await asyncio.to_thread(db.get_test, test_id)
    if not test or test["owner_id"] != callback.from_user.id:
        await callback.answer("Test topilmadi yoki o'chirilgan", show_alert=True)
        return None
    return test


def _test_card(test: dict, keys: dict, attempts: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    status = "📢 Natijalar e'lon qilingan" if test["closed"] else "🟢 Javob qabul qilinmoqda · natijalar yashirin"
    lines = [
        f"📝 <b>{esc(test['title'])}</b>",
        LINE,
        f"📋 {format_title(test['fmt'], test['count'])}",
        f"🔢 Kod: <code>{test['code']}</code>",
        f"🅰️ Variantlar: {', '.join(keys)}",
        f"🗓 {test['created_at']}",
        f"{status}",
        "",
        f"👥 Qatnashchilar: <b>{len(attempts)}</b>",
    ]
    if attempts:
        if test["fmt"] == "milliy":
            totals = [total_points(a) for a in attempts]
            top = max_points(test["items"], test["fmt"])
            lines.append(f"📈 O'rtacha ball: <b>{fmt_num(sum(totals) / len(totals))}</b> / {fmt_num(top)}")
            lines.append(f"🏆 Eng yuqori: <b>{fmt_num(max(totals))}</b>")
            waiting = sum(1 for a in attempts if a["essay"] is None)
            if waiting:
                lines.append(f"✍️ Esse kutmoqda: <b>{waiting}</b> ta")
        else:
            shares = [percent(a["test_correct"] + a["written_correct"], a["auto_total"]) for a in attempts]
            lines.append(f"📈 O'rtacha: <b>{fmt_num(sum(shares) / len(shares))}%</b> · 🏆 eng yuqori: <b>{fmt_num(max(shares))}%</b>")
    tid = test["id"]
    rows = []
    if not test["closed"]:
        rows.append([("📢 Natijalarni e'lon qilish", f"close:{tid}")])
    rows += [
        [("📊 Natijalar PDF", f"res:{tid}")],
        [("📄 Titul", f"sheet:{tid}"), ("🔑 Kalit", f"key:{tid}")],
    ]
    if test["fmt"] == "milliy":
        rows.append([("✍️ Esseni baholash", f"essay:{tid}")])
    if test["closed"]:
        rows.append([("🔓 Qayta ochish", f"open:{tid}")])
    rows.append([("🗑 O'chirish", f"del:{tid}")])
    rows.append([("⬅️ Testlarim", "back")])
    return "\n".join(lines), inline(rows)


async def _render_card(callback: CallbackQuery, test: dict) -> None:
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    text, markup = _test_card(test, keys, attempts)
    await safe_edit(callback.message, text, markup)


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
        await callback.answer("📄 Tayyorlanmoqda…")
        keys = await asyncio.to_thread(db.get_keys, test["id"])
        await _send_sheet(callback.message, test, list(keys))


@dp.callback_query(F.data.startswith("key:"))
async def send_key(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await callback.answer("🔑 Tayyorlanmoqda…")
        await _send_key(callback.message, test)


async def _send_results(message: Message, test: dict) -> None:
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    content = await asyncio.to_thread(pdfs.results_pdf, test, attempts)
    await message.answer_document(
        BufferedInputFile(content, filename=f"natijalar_{test['code']}.pdf"),
        caption=f"📊 <b>{esc(test['title'])}</b> — {len(attempts)} ta qatnashchi",
    )


@dp.callback_query(F.data.startswith("res:"))
async def send_results(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await callback.answer("📊 Tayyorlanmoqda…")
        await _send_results(callback.message, test)


@dp.callback_query(F.data.startswith("close:"))
async def close_ask(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        attempts = await asyncio.to_thread(db.list_attempts, test["id"])
        if not attempts:
            await callback.answer("Hali hech kim javob yubormagan", show_alert=True)
            return
        await callback.answer()
        essays = await asyncio.to_thread(db.essays_for_test, test["id"]) if test["fmt"] == "milliy" else {}
        # esse yubormaganlar (➖) kutilmaydi — ular esse uchun 0 ball oladi
        waiting = sum(1 for a in attempts
                      if a["essay"] is None and (essays.get(a["id"]) or {}).get("status", "manual") != "none")
        warning = (
            f"\n⚠️ <b>{waiting} ta</b> o'quvchining essesi hali baholanmagan — ular essesiz ball oladi.\n"
            if waiting else ""
        )
        await safe_edit(
            callback.message,
            f"📢 <b>{esc(test['title'])}</b> natijalarini e'lon qilasizmi?\n{LINE}\n"
            f"👥 Topshirganlar: <b>{len(attempts)}</b> ta\n{warning}\n"
            "• Javob qabul qilish to'xtaydi\n"
            "• Har bir o'quvchiga natijasi va to'g'ri javoblar yuboriladi\n"
            + ("• Rasch bali va daraja barcha natijalar asosida hisoblanadi\n" if test["fmt"] == "milliy" else ""),
            inline(
                ([[("✍️ Avval esselarni baholash", f"essay:{test['id']}")]] if waiting else [])
                + [[("📢 Ha, e'lon qilish", f"closeok:{test['id']}"), ("↩️ Yo'q", f"t:{test['id']}")]]
            ),
        )


@dp.callback_query(F.data.startswith("closeok:"))
async def close_test(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if not test:
        return
    if test["closed"]:
        await callback.answer("Natijalar allaqachon e'lon qilingan")
        await _render_card(callback, test)
        return
    await asyncio.to_thread(db.set_closed, test["id"], True)
    test["closed"] = 1
    await callback.answer("📢 E'lon qilinmoqda…")
    await _render_card(callback, test)
    sent = await _notify_participants(callback.bot, test)
    await callback.message.answer(f"📨 Natijalar <b>{sent}</b> ta o'quvchiga yuborildi.")
    await _send_results(callback.message, test)


async def _notify_participants(bot: Bot, test: dict) -> int:
    """Test yakunlanganda har bir qatnashchiga natijasi va o'z varianti bo'yicha to'g'ri javoblar yuboriladi."""
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    rasch = await asyncio.to_thread(rasch_results, test, attempts) if test["fmt"] == "milliy" else {}
    essays = await asyncio.to_thread(db.essays_for_test, test["id"]) if test["fmt"] == "milliy" else {}
    choices, texts = choice_items(test["items"]), text_items(test["items"])
    sent = 0
    for attempt in attempts:
        key = keys.get(attempt["variant"], {})
        line = "".join(key.get(item["label"], "?") for item in choices).lower()
        text = (
            f"📢 <b>Natijalar e'lon qilindi</b> · {esc(test['title'])}\n{LINE}\n"
            f"{_result_text(attempt, test, rasch.get(attempt['id']))}\n\n"
            f"🔑 To'g'ri javoblar ({attempt['variant']} variant):\n<code>{line}</code>"
        )
        if texts:
            written = "\n".join(
                f"{item['label']}) {esc(' / '.join(split_options(key.get(item['label'], ''))))}" for item in texts
            )
            text += f"\n\n✍️ Yozma:\n{written}"
        try:
            await bot.send_message(attempt["tg_id"], text[:4000], reply_markup=_review_button(attempt["id"]))
            sent += 1
            essay = essays.get(attempt["id"])
            if essay and essay.get("ai_result") and attempt["essay"] is not None:
                await bot.send_message(
                    attempt["tg_id"],
                    f"📝 <b>Esse tahlili</b> · {fmt_num(attempt['essay'])}/{ESSAY_MAX}\n{LINE}\n"
                    f"{_criteria_text(essay['ai_result'])}"[:4000],
                )
        except Exception:
            logger.warning("Qatnashchiga xabar yuborilmadi: %s", attempt["tg_id"])
        await asyncio.sleep(0.05)
    return sent


@dp.callback_query(F.data.startswith("open:"))
async def reopen_test(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await asyncio.to_thread(db.set_closed, test["id"], False)
        test["closed"] = 0
        await callback.answer("🔓 Test qayta ochildi")
        await _render_card(callback, test)


@dp.callback_query(F.data.startswith("del:"))
async def delete_ask(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await callback.answer()
        await safe_edit(
            callback.message,
            f"🗑 <b>{esc(test['title'])}</b> ({test['code']}) testini va barcha natijalarini o'chirasizmi?\n\n"
            "⚠️ Buni qaytarib bo'lmaydi.",
            inline([[("🗑 Ha, o'chirish", f"delok:{test['id']}"), ("↩️ Yo'q", f"t:{test['id']}")]]),
        )


@dp.callback_query(F.data.startswith("delok:"))
async def delete_confirm(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if test:
        await asyncio.to_thread(db.delete_test, test["id"])
        await callback.answer("🗑 O'chirildi")
        await _show_tests(callback.message, callback.from_user.id, edit=True)


# ---------- Esse baholash (o'qituvchi) ----------

ESSAY_ICONS = {"pending": "⏳", "graded": "🤖", "failed": "❗️", "manual": "✍️", "none": "➖"}


def _essay_status(attempt: dict, essay: dict | None) -> str:
    """Ro'yxatdagi belgi: ✅ tasdiqlangan, 🤖 AI taklifi, ⏳ navbatda, ✍️ qo'lda, ❗️ xato, ➖ esse yo'q."""
    if attempt["essay"] is not None:
        return f"✅ {fmt_num(attempt['essay'])}"
    if not essay:
        return "✍️"
    if essay["status"] == "graded":
        return f"🤖 {fmt_num(essay['ai_score'])}"
    return ESSAY_ICONS.get(essay["status"], "✍️")


def _criteria_text(result: dict) -> str:
    lines = [f"{'🟢' if c['score'] == 2 else '🟡' if c['score'] == 1 else '🔴'} "
             f"<b>{essay_ai.RUBRIC_TITLES.get(c['key'], c['key'])}</b> — {c['score']}/2\n    <i>{esc(c['comment'])}</i>"
             for c in result.get("criteria", [])]
    if result.get("outline"):
        lines.append("⚠️ Reja yoki epigraf qo'yilgan")
    if result.get("comment"):
        lines.append(f"\n💬 {esc(result['comment'])}")
    return "\n".join(lines)


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
    await _render_essay_list(callback.message, test, attempts)


async def _render_essay_list(message: Message, test: dict, attempts: list[dict]) -> None:
    essays = await asyncio.to_thread(db.essays_for_test, test["id"])
    confirmed = sum(1 for a in attempts if a["essay"] is not None)
    suggested = [a for a in attempts if a["essay"] is None and (essays.get(a["id"]) or {}).get("status") == "graded"]
    pending = sum(1 for e in essays.values() if e["status"] == "pending")
    cost = sum(e["cost"] for e in essays.values())
    rows = []
    if suggested:
        rows.append([(f"🤖 AI baholarini tasdiqlash ({len(suggested)} ta)", f"esall:{test['id']}")])
    rows += [[(f"{_essay_status(a, essays.get(a['id']))} · {a['student_name'][:30]}", f"es:{a['id']}")]
             for a in attempts[:90]]
    rows.append([("🔄 Yangilash", f"essay:{test['id']}"), ("⬅️ Orqaga", f"t:{test['id']}")])
    ai_line = (f"🤖 AI: {essay_ai.provider_name()} · navbatda {pending} ta · sarf ${cost:.3f}\n"
               if essay_ai.enabled() else "🤖 AI o'chiq — esselarni o'zingiz baholaysiz\n")
    await safe_edit(
        message,
        f"✍️ <b>Esse baholash</b> · {esc(test['title'])}\n{LINE}\n"
        f"✅ Tasdiqlangan: <b>{confirmed}</b> / {len(attempts)}\n{ai_line}\n"
        "✅ tasdiqlangan · 🤖 AI taklifi · ⏳ navbatda · ✍️ qo'lda baholash · ❗️ AI xatosi · ➖ esse yo'q",
        inline(rows),
    )


@dp.callback_query(F.data.startswith("esall:"))
async def essay_confirm_all(callback: CallbackQuery) -> None:
    test = await _own_test(callback)
    if not test:
        return
    attempts = await asyncio.to_thread(db.list_attempts, test["id"])
    essays = await asyncio.to_thread(db.essays_for_test, test["id"])
    count = 0
    for attempt in attempts:
        essay = essays.get(attempt["id"])
        if attempt["essay"] is None and essay and essay["status"] == "graded":
            await asyncio.to_thread(db.set_essay, attempt["id"], essay["ai_score"])
            count += 1
    await callback.answer(f"✅ {count} ta AI bahosi tasdiqlandi", show_alert=True)
    await _render_essay_list(callback.message, test, await asyncio.to_thread(db.list_attempts, test["id"]))


async def _owned_attempt(callback: CallbackQuery) -> tuple[dict, dict] | None:
    attempt = await asyncio.to_thread(db.get_attempt, int(callback.data.split(":")[1]))
    test = attempt and await asyncio.to_thread(db.get_test, attempt["test_id"])
    if not test or test["owner_id"] != callback.from_user.id:
        await callback.answer("Topilmadi", show_alert=True)
        return None
    return attempt, test


@dp.callback_query(F.data.startswith("es:"))
async def essay_pick(callback: CallbackQuery) -> None:
    found = await _owned_attempt(callback)
    if not found:
        return
    attempt, test = found
    await callback.answer()
    essay = await asyncio.to_thread(db.get_essay, attempt["id"])
    aid = attempt["id"]
    lines = [f"👤 <b>{esc(attempt['student_name'])}</b> · {attempt['variant']} variant", LINE]
    if attempt["essay"] is not None:
        lines.append(f"✅ Tasdiqlangan baho: <b>{fmt_num(attempt['essay'])}</b> / {ESSAY_MAX}")
    rows = []
    if not essay or essay["status"] == "none":
        lines.append("➖ O'quvchi esse yubormagan (yoki qog'ozda topshirgan).")
    elif essay["status"] == "graded":
        result = essay["ai_result"] or {}
        lines.append(f"🤖 AI bahosi ({esc(essay['provider'] or '')}): <b>{fmt_num(essay['ai_score'])}</b> / {ESSAY_MAX}\n")
        lines.append(_criteria_text(result))
        rows.append([(f"✅ AI bahosini tasdiqlash ({fmt_num(essay['ai_score'])})", f"esok:{aid}")])
    elif essay["status"] == "pending":
        lines.append("⏳ AI hali baholamadi — bir necha daqiqadan keyin qayta oching.")
    elif essay["status"] == "failed":
        lines.append(f"❗️ AI baholay olmadi: <i>{esc(essay['error'] or '')}</i>")
        if essay_ai.enabled():
            rows.append([("🔁 AI'da qayta baholash", f"esretry:{aid}")])
    else:
        lines.append("✍️ Esseni o'zingiz baholang.")
    if essay and (essay["text"] or essay["photos"]):
        rows.append([("📄 Esse matni / rasmi", f"estext:{aid}")])
    rows.append([("✏️ O'zim ball qo'yaman", f"esmanual:{aid}")])
    rows.append([("⬅️ Ro'yxat", f"essay:{test['id']}")])
    await safe_edit(callback.message, "\n".join(lines)[:4000], inline(rows))


@dp.callback_query(F.data.startswith("esok:"))
async def essay_confirm(callback: CallbackQuery) -> None:
    found = await _owned_attempt(callback)
    if not found:
        return
    attempt, test = found
    essay = await asyncio.to_thread(db.get_essay, attempt["id"])
    if not essay or essay["status"] != "graded":
        await callback.answer("AI bahosi yo'q", show_alert=True)
        return
    await asyncio.to_thread(db.set_essay, attempt["id"], essay["ai_score"])
    await callback.answer(f"✅ {fmt_num(essay['ai_score'])} ball tasdiqlandi")
    await _essay_saved_notice(callback.bot, test, attempt, essay["ai_score"])
    await _render_essay_list(callback.message, test, await asyncio.to_thread(db.list_attempts, test["id"]))


@dp.callback_query(F.data.startswith("esretry:"))
async def essay_retry(callback: CallbackQuery) -> None:
    found = await _owned_attempt(callback)
    if found:
        await asyncio.to_thread(db.retry_essay, found[0]["id"])
        await callback.answer("🔁 Esse AI navbatiga qo'yildi", show_alert=True)


@dp.callback_query(F.data.startswith("estext:"))
async def essay_show_text(callback: CallbackQuery) -> None:
    found = await _owned_attempt(callback)
    if not found:
        return
    attempt, _ = found
    await callback.answer()
    essay = await asyncio.to_thread(db.get_essay, attempt["id"])
    for file_id in essay["photos"]:
        await callback.message.answer_photo(file_id)
    if essay["text"]:
        text = essay["text"]
        for start in range(0, len(text), 3800):
            await callback.message.answer(f"📄 <b>{esc(attempt['student_name'])}</b>\n\n{esc(text[start:start + 3800])}")


@dp.callback_query(F.data.startswith("esmanual:"))
async def essay_manual(callback: CallbackQuery, state: FSMContext) -> None:
    found = await _owned_attempt(callback)
    if not found:
        return
    attempt, _ = found
    await callback.answer()
    await state.set_state(Essay.score)
    await state.update_data(attempt_id=attempt["id"])
    current = "" if attempt["essay"] is None else f"\nHozirgi baho: {fmt_num(attempt['essay'])}"
    await callback.message.answer(
        f"👤 <b>{esc(attempt['student_name'])}</b>{current}\n\nEsse balini yozing (0–{ESSAY_MAX}), masalan <code>18,5</code>:",
        reply_markup=CANCEL_KB,
    )


async def _essay_saved_notice(bot: Bot, test: dict, attempt: dict, score: float) -> None:
    """Natijalar e'lon qilingan bo'lsa — o'quvchiga yangilangan esse bahosi yuboriladi."""
    if not test["closed"]:
        return
    attempt = dict(attempt, essay=score)
    top = fmt_num(max_points(test["items"], test["fmt"]))
    try:
        await bot.send_message(
            attempt["tg_id"],
            f"✍️ <b>{esc(test['title'])}</b>\nEsse bahoyingiz yangilandi: <b>{fmt_num(score)}/{ESSAY_MAX}</b>\n"
            f"💯 Jami ball: <b>{fmt_num(total_points(attempt))}/{top}</b>",
        )
    except Exception:
        logger.warning("Esse bali o'quvchiga yuborilmadi")


@dp.message(Essay.score, F.text)
async def essay_score(message: Message, state: FSMContext) -> None:
    # Ikki ekspert bahosining o'rtachasi kasr bo'lishi mumkin: 18,5
    raw = message.text.strip().replace(",", ".")
    try:
        score = float(raw)
    except ValueError:
        score = -1
    if not 0 <= score <= ESSAY_MAX or round(score * 2) != score * 2:
        await message.answer(f"✋ 0 dan {ESSAY_MAX} gacha son yozing (0,5 qadam bilan: 18 yoki 18,5).")
        return
    data = await state.get_data()
    await state.clear()
    attempt = await asyncio.to_thread(db.get_attempt, data["attempt_id"])
    test = attempt and await asyncio.to_thread(db.get_test, attempt["test_id"])
    if not test or test["owner_id"] != message.from_user.id:
        await message.answer("Topilmadi.", reply_markup=MAIN_KB)
        return
    await asyncio.to_thread(db.set_essay, attempt["id"], score)
    top = fmt_num(max_points(test["items"], test["fmt"]))
    total = fmt_num(total_points(dict(attempt, essay=score)))
    await message.answer(
        f"✅ <b>{esc(attempt['student_name'])}</b>\n📝 Esse: <b>{fmt_num(score)}</b>/{ESSAY_MAX} · 💯 Jami: <b>{total}</b>/{top}",
        reply_markup=MAIN_KB,
    )
    await message.answer("Keyingisi?", reply_markup=inline([[("✍️ Esselar ro'yxati", f"essay:{test['id']}")]]))
    await _essay_saved_notice(message.bot, test, attempt, score)


# ---------- AI esse navbati (fon jarayoni) ----------

ESSAY_MAX_TRIES = 4


async def essay_worker(bot: Bot) -> None:
    """Navbatdagi esselarni birma-bir AI'da baholaydi. Xato bo'lsa — keyinroq qayta urinadi, esse yo'qolmaydi."""
    if not essay_ai.enabled():
        logger.info("AI esse tekshiruvi o'chiq (AI_PROVIDER=%s)", essay_ai.AI_PROVIDER)
        return
    logger.info("AI esse tekshiruvi yoqildi: %s", essay_ai.provider_name())
    delay = 10
    while True:
        try:
            batch = await asyncio.to_thread(db.pending_essays, 5)
            for essay in batch:
                await _grade_one(bot, essay)
                await asyncio.sleep(2)
            delay = 10
        except essay_ai.AIError as error:  # limit yoki tarmoq — kutib turamiz
            logger.warning("AI vaqtincha ishlamayapti: %s", error)
            delay = min(delay * 2, 600)
        except Exception:
            logger.exception("Esse navbatida xato")
            delay = min(delay * 2, 600)
        await asyncio.sleep(delay)


async def _grade_one(bot: Bot, essay: dict) -> None:
    try:
        images = [] if essay["text"] else await _download_photos(bot, essay["photos"])
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


# ---------- O'quvchi: javobni tekshirish ----------

CODE_PATTERN = re.compile(r"^\s*(\d{5})\s*(?:\*\s*(.+))?$", re.DOTALL)


@dp.message(F.text == BTN_CHECK)
async def check_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await remember(message)
    await state.set_state(Check.code)
    await message.answer(
        "✅ <b>Javobni tekshirish</b>\n"
        f"{LINE}\n"
        "🔢 Test kodini yuboring (5 xonali son).\n\n"
        "⚡️ Tez usul — kod va javoblar birga:\n<code>12345*abcdabcd...</code>",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(None), F.text.regexp(r"^\s*\d{5}\s*(\*|$)"))
async def quick_check(message: Message, state: FSMContext) -> None:
    """Bosh menyuda "12345" yoki "12345*abcd..." yuborilsa — darhol tekshirishga o'tamiz."""
    await remember(message)
    await check_code(message, state)


@dp.message(Check.code, F.text)
async def check_code(message: Message, state: FSMContext) -> None:
    match = CODE_PATTERN.match(message.text)
    if not match:
        await message.answer("✋ Kod 5 xonali son bo'lishi kerak. Masalan: <code>12345</code>", reply_markup=CANCEL_KB)
        await state.set_state(Check.code)
        return
    test = await asyncio.to_thread(db.get_test_by_code, match.group(1))
    if not test:
        await message.answer("🔍 Bunday kodli test topilmadi. Kodni tekshirib qayta yuboring.", reply_markup=CANCEL_KB)
        await state.set_state(Check.code)
        return
    previous = await asyncio.to_thread(db.find_attempt, test["id"], message.from_user.id)
    if previous:
        await state.clear()
        if test["closed"]:  # natijalar e'lon qilingan
            await message.answer(
                f"📊 <b>Natijangiz</b> · {esc(test['title'])}\n{LINE}\n{_result_text(previous, test)}",
                reply_markup=_review_button(previous["id"]),
            )
        else:
            await message.answer(
                f"ℹ️ Siz bu testga javob yuborgansiz.\n\n⏳ Natijalar o'qituvchi e'lon qilgandan keyin keladi.",
                reply_markup=MAIN_KB,
            )
        return
    if test["closed"]:
        await state.clear()
        await message.answer("🔒 Bu testning natijalari e'lon qilingan, javob qabul qilinmaydi.", reply_markup=MAIN_KB)
        return

    await state.update_data(test_id=test["id"], pending=match.group(2))
    keys = await asyncio.to_thread(db.get_keys, test["id"])
    await message.answer(
        f"📝 <b>{esc(test['title'])}</b>\n📋 {format_title(test['fmt'], test['count'])}", reply_markup=CANCEL_KB
    )
    if len(keys) > 1:
        await state.set_state(Check.variant)
        await message.answer(
            "🅰️ Qaysi variantni yechdingiz?",
            reply_markup=inline([[(f"{name} variant", f"var:{name}") for name in keys]]),
        )
        return
    await state.update_data(variant=next(iter(keys)))
    await _ask_name(message, state, message.from_user.id)


@dp.callback_query(Check.variant, F.data.startswith("var:"))
async def check_variant(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    variant = callback.data.split(":", 1)[1]
    await safe_edit(callback.message, f"🅰️ <b>{variant} variant</b> tanlandi")
    await state.update_data(variant=variant)
    await _ask_name(callback.message, state, callback.from_user.id)


async def _ask_name(message: Message, state: FSMContext, tg_id: int) -> None:
    await state.set_state(Check.name)
    last = await asyncio.to_thread(db.last_name_used, tg_id)
    markup = inline([[(f"👤 {last[:40]}", "name:last")]]) if last else None
    await message.answer(
        "👤 Ism va familiyangizni yozing:" + ("\n<i>yoki avvalgisini tanlang</i>" if last else ""), reply_markup=markup
    )


@dp.callback_query(Check.name, F.data == "name:last")
async def check_name_last(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    name = await asyncio.to_thread(db.last_name_used, callback.from_user.id)
    await safe_edit(callback.message, f"👤 <b>{esc(name or '')}</b>")
    await _name_done(callback.message, state, name or callback.from_user.full_name)


@dp.message(Check.name, F.text)
async def check_name(message: Message, state: FSMContext) -> None:
    name = " ".join(message.text.split())[:60]
    if len(name) < 3 or name.startswith("/"):
        await message.answer("✋ Ism va familiyani to'liq yozing.")
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
        await state.update_data(pending=None)
        try:
            answers = parse_choices(data["pending"], test["items"], allow_blank=True)
        except AnswerError as error:
            await message.answer(f"❗️ Kod bilan yuborilgan javoblarda xato: {esc(str(error))}")
        else:
            await _choices_done(message, state, test, answers)
            return
    count = len(choice_items(test["items"]))
    extra = "\n🔸 33–35 javoblari A–F" if test["fmt"] == "milliy" else ""
    await state.set_state(Check.choices)
    await message.answer(
        f"✏️ <b>1–{count} test javoblari</b>\n\n"
        f"<code>abcdabcd...</code> — {count} ta harf ketma-ket\n"
        f"<code>1a2b3c...</code> — raqam va harf\n"
        f"🔸 Javob bermagan savolga <code>-</code>{extra}",
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
            f"✍️ <b>Yozma javoblar</b> — har biri yangi qatorda, {len(labels)} qator:\n"
            f"<code>{', '.join(labels)}</code>\n\n"
            "🔸 Javob bermagan bo'lsangiz — <code>-</code>\n"
            "Masalan:\n<code>36) aziz\n37) : \" , \" .\n38) -\n...</code>",
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
    answers = {**data["answers"], **written}
    if test["fmt"] == "milliy":
        await state.update_data(answers=answers)
        await _ask_essay(message, state)
        return
    await _finish_check(message, state, test, answers)


# ---------- O'quvchi: esse (45-savol) ----------

ESSAY_MIN_CHARS = 200
ESSAY_MAX_PHOTOS = 3


async def _ask_essay(message: Message, state: FSMContext) -> None:
    await state.set_state(Check.essay)
    await state.update_data(essay_photos=[], essay_text=None, essay_cost=0.0)
    who = "AI tekshiradi, o'qituvchi tasdiqlaydi" if essay_ai.enabled() else "o'qituvchi baholaydi"
    await message.answer(
        f"📝 <b>45. Esse</b> · {who}\n{LINE}\n"
        "Esseni yuboring:\n"
        "✍️ <b>matn qilib</b> — bitta xabarda, xatboshilarni yangi qatordan\n"
        f"📷 yoki <b>qo'lyozma rasmi</b> — 1–{ESSAY_MAX_PHOTOS} sahifa, aniq va yorug' joyda\n\n"
        "Esse yozmagan bo'lsangiz — pastdagi tugmani bosing.",
        reply_markup=inline([[("⏭ Essesiz topshirish", "sx:skip")]]),
    )


@dp.message(Check.essay, F.text)
async def essay_text_in(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if len(text) < ESSAY_MIN_CHARS:
        await message.answer(
            f"✋ Esse juda qisqa ({len(text)} belgi). To'liq esseni bitta xabarda yuboring "
            "yoki «⏭ Essesiz topshirish»ni bosing.",
            reply_markup=inline([[("⏭ Essesiz topshirish", "sx:skip")]]),
        )
        return
    await state.update_data(essay_text=text[:12000], essay_photos=[])
    await _finish_from_state(message, state)


@dp.message(Check.essay, F.photo)
async def essay_photo_in(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photos = list(data.get("essay_photos") or [])
    if len(photos) >= ESSAY_MAX_PHOTOS:
        await message.answer(f"✋ Ko'pi bilan {ESSAY_MAX_PHOTOS} ta sahifa. «✅ Tayyor»ni bosing.")
        return
    photos.append(message.photo[-1].file_id)  # eng katta o'lcham
    await state.update_data(essay_photos=photos)
    await message.answer(
        f"📷 {len(photos)}-sahifa qabul qilindi. Yana sahifa bo'lsa yuboring yoki «✅ Tayyor»ni bosing.",
        reply_markup=inline([
            [(f"✅ Tayyor ({len(photos)} sahifa)", "sx:done")],
            [("🗑 Rasmlarni qaytadan yuborish", "sx:reset")],
        ]),
    )


async def _download_photos(bot: Bot, file_ids: list[str]) -> list[tuple[bytes, str]]:
    images = []
    for file_id in file_ids:
        buffer = await bot.download(file_id)
        images.append((buffer.read(), "image/jpeg"))
    return images


@dp.callback_query(Check.essay, F.data.startswith("sx:"))
async def essay_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    photos = data.get("essay_photos") or []  # sx:skip / sx:done / sx:reset / sx:ok / sx:type
    if action == "skip":
        await callback.answer()
        await state.update_data(essay_text=None, essay_photos=[])
        await safe_edit(callback.message, "⏭ Essesiz topshirilmoqda…")
        await _finish_from_state(callback.message, state)
    elif action == "reset":
        await callback.answer("Rasmlar o'chirildi")
        await state.update_data(essay_photos=[], essay_text=None)
        await safe_edit(callback.message, "📷 Rasmlarni qaytadan yuboring (yoki esseni matn qilib yozing).")
    elif action == "type":
        await callback.answer()
        await state.update_data(essay_photos=[], essay_text=None)
        await safe_edit(callback.message, "✍️ Esseni matn qilib bitta xabarda yuboring.")
    elif action == "ok":
        await callback.answer()
        await safe_edit(callback.message, "✅ Esse matni tasdiqlandi.")
        await _finish_from_state(callback.message, state)
    elif action == "done":
        if not photos:
            await callback.answer("Avval rasm yuboring", show_alert=True)
            return
        await callback.answer()
        if not essay_ai.enabled():  # AI o'chiq — rasmlar o'qituvchiga boradi
            await safe_edit(callback.message, f"✅ {len(photos)} sahifa qabul qilindi.")
            await _finish_from_state(callback.message, state)
            return
        await safe_edit(callback.message, "⏳ Qo'lyozma o'qilmoqda… (bir necha soniya)")
        try:
            images = await _download_photos(callback.bot, photos)
            text, cost = await essay_ai.transcribe(images)
        except Exception as error:
            logger.warning("Qo'lyozmani o'qib bo'lmadi: %s", error)
            await callback.message.answer("⚠️ Rasmni hozir o'qib bo'lmadi — esse rasm holida yuboriladi.")
            await _finish_from_state(callback.message, state)
            return
        await state.update_data(essay_text=text[:12000], essay_cost=data.get("essay_cost", 0) + cost)
        await callback.message.answer(
            f"📄 <b>Qo'lyozmangiz shunday o'qildi</b>\n{LINE}\n{esc(text[:3300])}\n{LINE}\n"
            "To'g'ri o'qilganini tekshiring: noto'g'ri o'qilgan so'zlar xato deb hisoblanishi mumkin.",
            reply_markup=inline([
                [("✅ To'g'ri, yuborish", "sx:ok")],
                [("📷 Qaytadan rasmga olish", "sx:reset"), ("✍️ Matn qilib yozaman", "sx:type")],
            ]),
        )


async def _finish_from_state(message: Message, state: FSMContext) -> None:
    test = await _test_from_state(state)
    if not test:
        await state.clear()
        await message.answer("Test topilmadi.", reply_markup=MAIN_KB)
        return
    data = await state.get_data()
    await _finish_check(message, state, test, data["answers"])


def _result_text(attempt: dict, test: dict, rasch: tuple[float, str] | None = None) -> str:
    correct = attempt["test_correct"] + attempt["written_correct"]
    total = attempt["auto_total"]
    choices, texts = choice_items(test["items"]), text_items(test["items"])
    lines = [f"👤 <b>{esc(attempt['student_name'])}</b> · {attempt['variant']} variant", ""]
    if test["fmt"] != "milliy":
        lines.append(f"🎯 To'g'ri: <b>{correct}</b> / {total}")
        lines.append(bar(correct, total))
    else:
        top = max_points(test["items"], test["fmt"])
        points = total_points(attempt)
        essay = attempt.get("essay")
        lines.append(f"🧩 Test (1–35):  <b>{attempt['test_correct']}</b> / {len(choices)}")
        lines.append(f"✍️ Yozma (36–44):  <b>{attempt['written_correct']}</b> / {len(texts)}")
        lines.append(f"📝 Esse:  <b>{fmt_num(essay)}</b> / {ESSAY_MAX}" if essay is not None else "📝 Esse:  ⏳ baholanmoqda")
        lines.append("")
        lines.append(f"💯 Ball: <b>{fmt_num(points)}</b> / {fmt_num(top)}")
        lines.append(bar(points, top))
        if rasch:
            lines.append(f"🎓 Rasch bali: <b>{fmt_num(rasch[0])}</b>  ·  Daraja: <b>{rasch[1]}</b>")
    lines.append("")
    lines.append(f"❌ Xato: {', '.join(attempt['wrong'])}" if attempt["wrong"] else "🏆 Hammasi to'g'ri!")
    return "\n".join(lines)


def _review_button(attempt_id: int) -> InlineKeyboardMarkup:
    return inline([[("🔍 Batafsil: har bir savol", f"rev:{attempt_id}")]])


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
        await message.answer(
            "ℹ️ Siz bu testga javob yuborgansiz.\n\n⏳ Natijalar o'qituvchi e'lon qilgandan keyin keladi.",
            reply_markup=MAIN_KB,
        )
        return

    essay_line = ""
    if test["fmt"] == "milliy":
        essay_text, photos = data.get("essay_text"), data.get("essay_photos") or []
        if essay_text or photos:
            status = "pending" if essay_ai.enabled() else "manual"
            essay_line = "📝 Esse: qabul qilindi\n"
        else:
            status = "none"
            essay_line = "📝 Esse: yuborilmadi\n"
        await asyncio.to_thread(db.create_essay, attempt["id"], essay_text, photos, status)
        if data.get("essay_cost"):
            await asyncio.to_thread(db.add_essay_cost, attempt["id"], data["essay_cost"])

    # Natija darhol ko'rsatilmaydi: o'qituvchi hamma topshirgach e'lon qiladi
    await message.answer(
        f"✅ <b>Javoblaringiz qabul qilindi!</b>\n{LINE}\n"
        f"📝 {esc(test['title'])}\n"
        f"👤 {esc(data['name'])} · {data['variant']} variant\n"
        f"{essay_line}\n"
        "⏳ Natijalar o'qituvchi e'lon qilgandan keyin shu yerga keladi.",
        reply_markup=MAIN_KB,
    )

    correct = result["test_correct"] + result["written_correct"]
    summary = (
        f"{fmt_num(result['points'])} ball (essesiz)" if test["fmt"] == "milliy"
        else f"{correct}/{result['auto_total']} ({fmt_num(percent(correct, result['auto_total']))}%)"
    )
    count = len(await asyncio.to_thread(db.list_attempts, test["id"]))
    try:
        await message.bot.send_message(
            test["owner_id"],
            f"🆕 <b>{esc(test['title'])}</b> · {test['code']}\n"
            f"👤 {esc(data['name'])} · {data['variant']} variant — <b>{summary}</b>\n"
            f"👥 Jami topshirdi: <b>{count}</b> ta",
            reply_markup=inline([[("📋 Testni ochish", f"t:{test['id']}")]]),
        )
    except Exception:
        logger.warning("O'qituvchiga natija haqida xabar yuborilmadi")


@dp.callback_query(F.data.startswith("rev:"))
async def review(callback: CallbackQuery) -> None:
    """Har bir savol bo'yicha ✅/❌. To'g'ri javoblar faqat test yakunlangach ko'rsatiladi."""
    attempt = await asyncio.to_thread(db.get_attempt, int(callback.data.split(":")[1]))
    test = attempt and await asyncio.to_thread(db.get_test, attempt["test_id"])
    if not test or callback.from_user.id not in (attempt["tg_id"], test["owner_id"]):
        await callback.answer("Topilmadi", show_alert=True)
        return
    if not test["closed"] and callback.from_user.id != test["owner_id"]:
        await callback.answer("⏳ Natijalar hali e'lon qilinmagan", show_alert=True)
        return
    await callback.answer()
    show_key = True
    key = (await asyncio.to_thread(db.get_keys, test["id"])).get(attempt["variant"], {}) if show_key else {}
    wrong = set(attempt["wrong"])
    answers = attempt["answers"]

    cells = []
    for item in choice_items(test["items"]):
        label = item["label"]
        given = answers.get(label, "-")
        mark = "❌" if label in wrong else "✅"
        fix = f"→{key[label]}" if show_key and label in wrong else ""
        cells.append(f"{label:>2} {given}{mark}{fix}")
    width = 3 if show_key else 4
    grid = "\n".join("  ".join(cells[i:i + width]) for i in range(0, len(cells), width))
    text = f"🔍 <b>Batafsil</b> · {esc(test['title'])}\n{LINE}\n<pre>{esc(grid)}</pre>"

    texts = text_items(test["items"])
    if texts:
        rows = []
        for item in texts:
            label = item["label"]
            mark = "❌" if label in wrong else "✅"
            given = esc(answers.get(label, "—"))
            right = ""
            if show_key and label in wrong:
                right = f"  → <i>{esc(' / '.join(split_options(key.get(label, ''))))}</i>"
            rows.append(f"{mark} <b>{label}</b>: {given}{right}")
        text += "\n✍️ <b>Yozma</b>\n" + "\n".join(rows)
    await callback.message.answer(text[:4000])


# ---------- Qolgan xabarlar va xatolar ----------

@dp.message()
async def fallback(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await message.answer("✋ Iltimos, matn ko'rinishida yuboring yoki «❌ Bekor qilish» ni bosing.")
        return
    await message.answer(
        "🤔 Tushunmadim. Test kodini yuboring yoki pastdagi tugmalardan birini tanlang 👇", reply_markup=MAIN_KB
    )


@dp.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    """Eski xabardagi tugma (bosqich o'tib ketgan) — aylanib qolmasin."""
    await callback.answer("Bu tugma eskirgan. Qaytadan boshlang 🙂", show_alert=True)


@dp.errors()
async def on_error(event: ErrorEvent) -> bool:
    logger.exception("Kutilmagan xato", exc_info=event.exception)
    update = event.update
    try:
        if update.message:
            await update.message.answer("⚠️ Xatolik yuz berdi. Qaytadan urinib ko'ring yoki /start bosing.")
        elif update.callback_query:
            await update.callback_query.answer("⚠️ Xatolik yuz berdi, qaytadan urinib ko'ring", show_alert=True)
    except Exception:
        pass
    return True


# ---------- Ishga tushirish ----------

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


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
        raise SystemExit("BOT_TOKEN topilmadi: .env faylida BOT_TOKEN=... ni BotFather tokeni bilan to'ldiring.")
    await _start_health_server()  # avval port — Render uni darhol ko'rsin
    db.init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Bosh menyu"),
            BotCommand(command="yordam", description="Yo'riqnoma"),
            BotCommand(command="cancel", description="Bekor qilish"),
        ])
    except Exception:
        logger.warning("Buyruqlar ro'yxatini o'rnatib bo'lmadi", exc_info=True)
    logger.info("Bot ishga tushdi")
    worker = asyncio.create_task(essay_worker(bot))  # AI esse navbati
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()


if __name__ == "__main__":
    asyncio.run(main())
