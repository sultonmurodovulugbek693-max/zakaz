"""
Telegram Mini App: veb-server (aiohttp) — sahifa (webapp/ papkasi) va JSON API.

Har bir API so'rovi Telegram imzosi (initData) bilan keladi: X-Init-Data sarlavhasi.
Imzo bot tokeni bilan tekshiriladi — foydalanuvchi o'zini boshqa odam qilib ko'rsata olmaydi.

Bot bilan bog'liq amallar (natijalarni e'lon qilish, PDF yuborish, o'qituvchiga xabar)
bot.py dan HOOKS orqali beriladi — bu modul botni import qilmaydi.
"""

import asyncio
import base64
import csv
import hashlib
import hmac
import html
import io
import json
import math
import random
import secrets
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data

import db
import essay_ai
import places as places_data
import pdfs
import plans
import results
import sheet
import sheet_styles
from config import ADMIN_CONTACT, ADMIN_IDS, BOT_TOKEN, SALES_ENABLED  # noqa: F401 (env bayrog'i — plans.sales_on ichida)
from scoring import essay_on_paper, essay_pending, rasch_breakdown, rasch_results, total_points
from structure import (
    ESSAY_MAX,
    ESSAY_SCALE,
    LETTERS,
    MAX_TEXT_OPTIONS,
    build_items,
    choice_items,
    essay_to_75,
    format_title,
    grade,
    max_points,
    percent,
    split_options,
    essay_from_75,
    text_is_correct,
    text_items,
)

logger = logging.getLogger("webapp")
STATIC_DIR = Path(__file__).resolve().parent / "webapp"
THEMES = ["classic", "diploma", "ikat", "tiles", "neon", "terminal", "brutal", "glass", "chalk", "newspaper",
          "bauhaus", "pastel", "ocean", "autumn", "minimal", "luxe", "sakura", "sport", "paper", "space"]
INIT_DATA_MAX_AGE = 24 * 3600
ESSAY_MIN_CHARS = 200

HOOKS: dict = {}  # bot.py to'ldiradi: record_attempt, announce, send_pdf
_BACKGROUND: set = set()  # fondagi vazifalar (e'lon) — tugaguncha havola saqlanadi


class ApiError(web.HTTPException):
    def __init__(self, status: int, message: str, **extra):
        self.status_code = status
        super().__init__(text=json.dumps({"error": message, **extra}, ensure_ascii=False), content_type="application/json")


def ok(data: dict | list) -> web.Response:
    return web.json_response(data, dumps=lambda value: json.dumps(value, ensure_ascii=False))


async def current_user(request: web.Request) -> dict:
    """Telegram Mini App'da — initData imzosi; APK ilovada — bot orqali olingan sessiya (X-App-Session)."""
    session = request.headers.get("X-App-Session", "")
    if session and not request.headers.get("X-Init-Data"):
        user = await run(db.session_user, session)
        if not user:
            raise ApiError(401, "Qaytadan Telegram orqali kiring", relogin=True)
        return user
    raw = request.headers.get("X-Init-Data", "")
    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, raw)
    except ValueError:
        raise ApiError(401, "Ilovani Telegram ichidan oching")
    if time.time() - data.auth_date.timestamp() > INIT_DATA_MAX_AGE:
        raise ApiError(401, "Sessiya eskirgan — ilovani qayta oching")
    user = data.user
    if user is None:
        raise ApiError(401, "Ilovani Telegram ichidan oching")
    name = " ".join(part for part in (user.first_name, user.last_name) if part) or "Foydalanuvchi"
    return {"id": user.id, "name": name, "username": user.username}


def from_app(request: web.Request) -> bool:
    return bool(request.headers.get("X-App-Session")) and not request.headers.get("X-Init-Data")


async def missing_for(user: dict) -> list[dict]:
    """Obuna bo'lmagan kanallar (admin va hook yo'q bo'lsa — bo'sh)."""
    if user["id"] in ADMIN_IDS or "missing" not in HOOKS:
        return []
    return [{"title": c["title"], "url": c["url"]} for c in await HOOKS["missing"](user["id"])]


async def auth(request: web.Request) -> dict:
    """Imzo + majburiy obuna tekshiruvi."""
    user = await current_user(request)
    missing = await missing_for(user)
    if missing:
        raise ApiError(403, "Avval kanallarga obuna bo'ling", subscribe=missing)
    return user


async def body(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(400, "Noto'g'ri so'rov")
    if not isinstance(payload, dict):
        raise ApiError(400, "Noto'g'ri so'rov")
    return payload


def to_int(value, default: int = 0, what: str = "Son") -> int:
    """So'rovdagi son: noto'g'ri bo'lsa — 400 (500 emas)."""
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{what} noto'g'ri")


def run(fn, *args):
    return asyncio.to_thread(fn, *args)


async def own_test(request: web.Request, user: dict) -> dict:
    test = await run(db.get_test, int(request.match_info["test_id"]))
    if not test or test["owner_id"] != user["id"]:
        raise ApiError(404, "Test topilmadi")
    return test


def public_items(test: dict) -> list[dict]:
    return [{"label": i["label"], "kind": i["kind"], "options": i.get("options", 0), "points": i.get("points", 1)}
            for i in test["items"]]


# ---------- Umumiy ----------

async def bootstrap(request: web.Request) -> web.Response:
    user = await current_user(request)
    await run(db.remember_user, user["id"], user["name"], user["username"])
    missing = await missing_for(user)
    if missing:
        return ok({"user": user, "subscribe": missing})
    uid = user["id"]
    unlimited = plans.ai_unlimited(uid)
    # Bir-biriga bog'liq bo'lmagan so'rovlar — parallel (masofadagi bazada ketma-ket kutish ilovani sekinlashtirardi)
    (attempts, tests, all_totals, role, last_name, classes, registration, plan, credits, essay_ai_ok, teachers,
     wallets, my_card, joins, offers, card, payment) = await asyncio.gather(
        run(db.attempts_of_user, uid), run(db.list_tests, uid, 50), run(db.owner_totals, uid),
        run(db.get_role, uid), run(db.last_name_used, uid), run(db.list_classes, uid), run(db.get_registration, uid),
        run(plans.status, uid), run(db.get_credits, uid), run(plans.can_use_ai, uid, "essays"), run(db.teachers_of, uid),
        run(db.wallets_of_student, uid), run(teacher_card, uid), run(db.join_requests_for, uid), run(plans.offers),
        run(plans.card), run(db.open_payment, uid))
    sales, credit_mode = await asyncio.gather(run(plans.sales_on), run(plans.credit_mode, uid))
    teacher_tests = []
    for test in tests:
        totals = all_totals.get(test["id"], [])
        teacher_tests.append({
            "id": test["id"], "code": test["code"], "title": test["title"], "fmt": test["fmt"],
            "format": format_title(test["fmt"], test["count"]), "closed": bool(test["closed"]),
            "students": len(totals), "created_at": test["created_at"], "theme": test.get("theme") or "classic",
            "mode": test.get("mode") or "online",
            "avg": round(sum(totals) / len(totals), 1) if totals else None,
            "max_points": max_points(test["items"], test["fmt"]),
        })
    return ok({
        "user": user,
        "role": role,
        "last_name": last_name,
        "attempts": [{
            "id": a["id"], "code": a["code"], "title": a["title"], "fmt": a["fmt"], "variant": a["variant"],
            "announced": bool(a["closed"]), "created_at": a["created_at"], "theme": a["theme"] or "classic",
        } for a in attempts],
        "tests": teacher_tests,
        "classes": len(classes),
        "registered": bool(registration),
        "plan": plan,
        "sales": sales,  # obuna sotilyaptimi (Render PLANS_ENABLED yoki ilovadagi «💎 Obuna tariflari»)
        # test yarata oladimi: sotuv yoqiq (obunasizga — sotib olish taklif qilinadi), bepul yoki to'langan obuna bor
        "can_create": sales or plans.is_free(uid) or bool(plan.get("active")),
        "credit_mode": credit_mode,  # AI esse kreditdan (obuna emas): «🤖 AI» bo'limi ko'rinadi
        "can_manage": plans.can_manage(uid),  # tariflar va to'lov kartasini kiritadi (admin, FREE_IDS)
        # AI esse krediti: o'quvchilar essesi shundan tekshiriladi (cheksiz — admin / FREE_IDS)
        "ai_credits": None if unlimited else credits,
        # o'quvchilar essesini hozir AI tekshira oladimi (kredit yoki obunaning oylik limiti) — «tugagan» ogohlantirishi uchun
        "essay_ai_ok": essay_ai_ok,
        "mocks": await mock_cards(user),  # «Bugungi mock» kartalari (FREE_IDS / admin ustozlarniki)
        "passes": await pass_offers(user),  # «💎 Tarif»: muddat davomida ustozning hamma mocklari bepul
        # O'quvchi: ulangan ustozlari — «✍️ Esse tekshirish» (ustoz kreditidan)
        # Begona ham: ustozning ommaviy havolasini ochgan bo'lsa (bepul esselari va krediti shu yerda)
        "practice": essay_ai.essay_enabled() and (bool(teachers) or bool(wallets)),
        "my_card": my_card,
        # Begona o'qituvchi: begonalar puli platformaga, ulushi balansiga; esse narxi kamida min_price
        "revshare": {"teacher_percent": 100 - await run(plans.platform_share), "min_price": await run(plans.min_essay_price)}
        if await run(plans.platform_routed, uid) else None,
        "paid_ai": essay_ai.paid_enabled(),  # oflayn testda «💎 AI tekshiruvi (Claude)» tanlovi
        "free_ai": unlimited and essay_ai.enabled(),  # yozma javoblarni bepul Gemini o'qiydi
        "can_public": unlimited,  # «Bugungi mock»ni hamma o'quvchiga ko'rsatish — faqat FREE_IDS / admin
        "claude": await run(claude_capacity) if unlimited else None,  # Claude yana nechta esse tekshira oladi
        "default_channel": DEFAULT_MOCK_CHANNEL if unlimited else None,  # yangi mockda majburiy obuna — oldindan
        "joins_pending": sum(1 for r in joins if r["status"] == "pending"),
        "my_teachers": [t["full_name"] or "Ustoz" for t in teachers],
        "plans": offers,
        "card": card,
        "payment": payment,
        "admin_contact": ADMIN_CONTACT,
        "ai": essay_ai.essay_enabled(),
        "themes": THEMES,
    })


# ---------- O'quvchi ----------

LIVE_IDLE = 60  # shuncha soniya signal kelmasa — «faol emas»
MAX_TEST_CHANNELS = 3


async def test_gate(test: dict, user: dict, recheck: bool = False) -> None:
    """O'qituvchi testga qo'shgan kanallar: o'quvchi obuna bo'lmaguncha testni ocholmaydi va javob yuborolmaydi."""
    if not test.get("channels") or test["owner_id"] == user["id"] or user["id"] in ADMIN_IDS or "missing_in" not in HOOKS:
        return
    if (test.get("mode") or "online") == "offline":  # oflayn testda majburiy obuna yo'q (eski testlarda qolgan bo'lsa ham)
        return
    missing = await HOOKS["missing_in"](user["id"], test["channels"], recheck)
    if missing:
        raise ApiError(403, "Testni yechish uchun kanallarga obuna bo'ling", channels=public_channels(missing))


def public_channels(channels: list[dict]) -> list[dict]:
    return [{"chat_id": c["chat_id"], "title": c["title"], "url": c["url"]} for c in channels]


async def resolve_channels(refs) -> list[dict]:
    """O'qituvchi yozgan kanallar (@username, t.me havola yoki ID) — bot har birida admin ekanini tekshiradi."""
    if not isinstance(refs, list):
        raise ApiError(400, "Kanallar ro'yxati noto'g'ri")
    refs = [r for r in refs if str(r).strip()][:MAX_TEST_CHANNELS]
    out = []
    for ref in refs:
        try:
            channel = await HOOKS["resolve_channel"](ref)
        except ValueError as error:
            raise ApiError(400, str(error))
        if all(c["chat_id"] != channel["chat_id"] for c in out):
            out.append(channel)
    return out


async def check_channel(request: web.Request) -> web.Response:
    """Test yaratishda «Qo'shish» bosilganda: kanal topildimi, bot unda adminmi."""
    await auth(request)
    ref = str((await body(request)).get("ref") or "").strip()
    if not ref:
        raise ApiError(400, "Kanal @username'ini yoki havolasini yozing")
    return ok(public_channels(await resolve_channels([ref]))[0])


async def set_channels(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    if (test.get("mode") or "online") == "offline":
        raise ApiError(400, "Oflayn testda majburiy obuna yo'q")
    channels = await resolve_channels((await body(request)).get("channels") or [])
    await run(db.set_test_channels, test["id"], channels)
    return ok({"channels": public_channels(channels)})


async def test_info(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test:
        raise ApiError(404, "Bunday kodli test topilmadi")
    if (test.get("mode") or "online") == "offline":
        if test["fmt"] != "milliy" or essay_on_paper(test):
            raise ApiError(409, "Bu test qog'ozda (titulda) o'tkaziladi — javoblarni ham, esseni ham varaqqa yozib "
                                "o'qituvchingizga topshiring")
        # Titulli milliy test: test qog'ozda, 45-esse esa shu yerda yoziladi (ID bilan)
        await test_gate(test, user, recheck=request.query.get("recheck") == "1")
        mine = await run(db.offline_essay_of_user, test["id"], user["id"])
        return ok({
            "code": test["code"], "title": test["title"], "fmt": test["fmt"], "mode": "offline",
            "closed": bool(test["closed"]), "essay_topic": test.get("essay_topic"), "ai": essay_ai.essay_enabled(),
            "last_name": await run(db.last_name_used, user["id"]),
            "essay_sent": {"number": mine["number"], "words": len(mine["text"].split()), "name": mine["name"]} if mine else None,
        })
    previous = await run(db.find_attempt, test["id"], user["id"])
    if not previous:
        await test_gate(test, user, recheck=request.query.get("recheck") == "1")
        await mock_gate(test, user)  # «Bugungi mock»: vaqt oralig'i va (pullik bo'lsa) to'lov
    keys = await run(db.get_keys, test["id"])
    return ok({
        "schedule": schedule_view(test), "files": await files_view(test),
        "code": test["code"], "title": test["title"], "fmt": test["fmt"],
        "format": format_title(test["fmt"], test["count"]), "items": public_items(test),
        "variants": list(keys), "closed": bool(test["closed"]), "theme": test.get("theme") or "classic",
        "essay_topic": test.get("essay_topic"), "submitted": previous["id"] if previous else None,
        "last_name": await run(db.last_name_used, user["id"]), "ai": essay_ai.essay_enabled(),
        "essay_pay": None if previous else await essay_pay_info(test, user),
        "essay_warning": None if previous else await essay_warning(test, user),
        "wallet": None if previous else await wallet_view(test, user),
        "readers": essay_ai.readers() if test["fmt"] == "milliy" else [],  # esse rasmini o'qiydigan AI'lar
    })


async def essay_warning(test: dict, user: dict) -> str | None:
    """Ustozning o'quvchisi: esse ustoz limitidan — limit tugagan bo'lsa ogohlantirish (esseni ustoz o'zi baholaydi)."""
    if test["fmt"] != "milliy" or not essay_ai.essay_enabled() or test["owner_id"] == user["id"]:
        return None
    if await is_outsider(test, user["id"]):
        return None
    if await run(plans.can_use_ai, test["owner_id"], "essays"):  # kredit yoki obunaning oylik limiti
        return None
    return "Ustozingizning esse tekshirish limiti tugagan — essengizni AI tekshirmaydi, ustoz o'zi baholaydi."



def teacher_card(teacher_id: int) -> dict:
    """Ustozning to'lov kartasi (begonalar esse tekshiruvini shunga to'laydi) — mock yaratishda bir marta kiritiladi.
    Begona o'qituvchida (plans.platform_routed) begonalar platformaga to'laydi — bu karta daromadini olish uchun."""
    saved = db.get_settings()
    return {"number": saved.get(f"teacher_card:{teacher_id}", ""), "holder": saved.get(f"teacher_holder:{teacher_id}", "")}


def payee_of(teacher_id: int) -> str | None:
    """Begona o'quvchi puli kimga: "platform" — platforma kartasiga (begona o'qituvchi), None — ustozning o'ziga."""
    return "platform" if plans.platform_routed(teacher_id) else None


def pay_card(teacher_id: int, payee: str | None) -> dict:
    """To'lov kartasi: platformaga bo'lsa — platforma kartasi (admin /sozlama), aks holda ustoz kartasi."""
    return plans.card() if payee == "platform" else teacher_card(teacher_id)


def essay_price_of(test: dict) -> int:
    """Begonaning mock esse narxi: begona o'qituvchida kamida min_essay_price (oldin qo'yilgan arzon narx ham)."""
    price = int(test.get("price") or 0)
    if price and plans.platform_routed(test["owner_id"]):
        price = max(price, plans.min_essay_price())
    return price


async def credit_share(source: str, pay: dict, teacher_id: int) -> None:
    """Platformaga tushgan to'lov tasdiqlandi — o'qituvchi ulushi balansiga (bitta to'lov — bir marta)."""
    amount = int(pay.get("amount") or 0)
    if pay.get("payee") != "platform" or amount <= 0:
        return
    share = await run(plans.teacher_cut, amount)
    await run(db.add_earning, teacher_id, source, pay["id"], pay.get("student_id"), amount, share)


def platform_decider(by_id: int) -> bool:
    """Platformaga tushgan to'lovni ustozdan tashqari kim bekor qila / tasdiqlay oladi: admin va FREE_IDS (karta ularniki)."""
    return plans.can_manage(by_id)


def _voided_key(kind: str, payment_id: int) -> str:
    return f"voided:{kind}:{payment_id}"


async def guard_platform_decision(kind: str, pay: dict, approve: bool, by_id: int) -> None:
    """Platforma to'lovi: admin bekor qilgan bo'lsa — ustoz uni qayta tasdiqlay olmaydi (soxta chek bilan 20% ni
    qayta-qayta yozdirmasin); faqat admin. Admin bekor qilsa — belgi qo'yiladi."""
    if pay.get("payee") != "platform":
        return
    key = _voided_key(kind, pay["id"])
    if platform_decider(by_id):
        if not approve:
            await run(db.set_setting, key, "1")
        return
    if approve and (await run(db.get_settings)).get(key):
        raise ApiError(403, "Bu to'lovni admin bekor qilgan — endi faqat admin tasdiqlay oladi")


async def read_receipt_for(pay: dict, teacher_id: int, data: bytes, mime: str) -> tuple[bool, list[str], str | None]:
    """Chekni AI o'qiydi faqat admin / FREE_IDS ustozlarida. Begona o'qituvchida (pul platformaga) — AI o'qimaydi,
    chek ustozga boradi va u o'zi tasdiqlaydi (nusxasi adminga — teacher_approved_notice)."""
    if pay.get("payee") == "platform":
        return False, ["chekni ustoz tekshiradi"], None
    return await verify_receipt(data, mime, pay["amount"], await run(pay_card, teacher_id, pay.get("payee")))


async def teacher_approved_notice(kind: str, pay: dict, by_id: int) -> None:
    """Ustoz platforma kartasiga tushgan chekni o'zi tasdiqladi (20% balansiga): nusxasi adminlarga «Bekor qilish» bilan —
    soxta chek bo'lsa admin bekor qiladi, ulush ham qaytib olinadi."""
    if pay.get("payee") != "platform" or platform_decider(by_id) or "platform_approved_notice" not in HOOKS:
        return
    try:
        await HOOKS["platform_approved_notice"](kind, pay["id"])
    except Exception:
        logger.warning("Ustoz tasdiqlagan chek adminga yuborilmadi", exc_info=True)


async def is_outsider(test: dict, student_id: int) -> bool:
    """Ustozning o'quvchisi emas (ustozning o'zi, adminlar va ustozdan tarif olganlar ham emas)."""
    if test["owner_id"] == student_id or student_id in ADMIN_IDS:
        return False
    if await run(db.is_linked, test["owner_id"], student_id):
        return False
    return not await pass_active(test["owner_id"], student_id)


async def pass_active(teacher_id: int, student_id: int) -> bool:
    until = await run(db.pass_until, teacher_id, student_id)
    return bool(until) and until > plans.now().strftime(TIME_FMT)


async def essay_pay_info(test: dict, user: dict) -> dict | None:
    """Onlayn mock: test hammaga bepul. Esseli mock (milliy)da begona o'quvchi (ustozning o'quvchisi emas) esseni
    yuklashdan oldin esse tekshiruvini to'laydi — narxni ustoz mock yaratishda qo'ygan, pul ustoz kartasiga.
    Chekni botga yuboradi, AI tekshiradi. None — to'lov kerak emas (o'quvchisi, to'langan yoki narx qo'yilmagan)."""
    if test["fmt"] != "milliy" or not await is_outsider(test, user["id"]):
        return None
    price = await run(essay_price_of, test)
    card = await run(pay_card, test["owner_id"], await run(payee_of, test["owner_id"]))
    if not price or not card["number"]:
        return None
    if test.get("opens_at"):  # «Bugungi mock» — o'quvchi tanlovi va to'lovi (mock_choice_info)
        return None
    pay = await run(db.essay_payment_of, test["id"], user["id"])
    if pay and pay["status"] == "paid":
        return None
    if await run(db.wallet_balance, test["owner_id"], user["id"]) > 0:  # ustozdan olingan kredit bor — 1 kredit yechiladi
        return None
    wallet = await run(db.open_wallet_payment, user["id"])
    return {"price": price, "card": card["number"], "holder": card["holder"], "status": pay["status"] if pay else None,
            "note": pay["note"] if pay and pay["status"] == "rejected" else None,
            "packs": await run(essay_packs, test["owner_id"]),  # kredit paketlari — keyingi esselar chek so'ramaydi
            "wallet_status": wallet["status"] if wallet and wallet["teacher_id"] == test["owner_id"] else None,
            "bot": await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""}


# ---------- «Bugungi mock»: jadval (ochiladi → kirish to'xtaydi → yopiladi), savollar (PDF / rasm), qatnashuvchilar ----------
# Faqat FREE_IDS / admin mocki hamma o'quvchining bosh sahifasida karta bo'lib chiqadi. Yopilgach bot o'zi: AI esselarni
# tekshirib bo'lishi bilan Rasch → natija e'lon → har qatnashchiga sertifikat (bot.mock_worker).

MOCK_GRACE = timedelta(minutes=3)  # yopilishdan keyin ham shuncha qabul qilinadi (avto-yuborish internetda kechiksa)
TIME_FMT = "%Y-%m-%d %H:%M"


def _at(value: str | None):
    if not value:
        return None
    return datetime.strptime(value[:16], TIME_FMT).replace(tzinfo=plans.TASHKENT)


def mock_phase(test: dict) -> str | None:
    """None — jadvalsiz; soon — hali ochilmagan; open — kirsa bo'ladi; late — faqat kirganlar davom etadi; closed."""
    opens, entry, closes = _at(test.get("opens_at")), _at(test.get("entry_until")), _at(test.get("closes_at"))
    if not opens:
        return None
    now = plans.now()
    if now < opens:
        return "soon"
    if now < entry:
        return "open"
    return "late" if now < closes + MOCK_GRACE else "closed"


def schedule_view(test: dict) -> dict | None:
    """Ilova uchun: vaqtlar (ms, qayta sanoq shu bilan) va server soati (telefon soati noto'g'ri bo'lsa ham to'g'ri sanaydi)."""
    if not test.get("opens_at"):
        return None
    ms = lambda value: int(_at(value).timestamp() * 1000)
    return {"opens": ms(test["opens_at"]), "entry": ms(test["entry_until"]), "closes": ms(test["closes_at"]),
            "opens_at": test["opens_at"], "entry_until": test["entry_until"], "closes_at": test["closes_at"],
            "mock_no": test.get("mock_no"), "public": bool(test.get("public_mock")), "phase": mock_phase(test),
            "price_ai": int(test.get("price") or 0), "price_teacher": int(test.get("price_teacher") or 0),
            "now": int(time.time() * 1000)}


async def mock_gate(test: dict, user: dict) -> None:
    """Jadvalli mockni ochish: vaqti kelmagan / kirish tugagan / yopilgan — 409; pullik va to'lanmagan — 402.
    Birinchi ochilishda «kirdi» deb yoziladi (kirish vaqti o'tgach ham shu o'quvchi davom etadi)."""
    phase = mock_phase(test)
    if not phase or test["owner_id"] == user["id"]:
        return
    card = await mock_card(test, user)
    if phase == "soon":
        raise ApiError(409, "Mock hali ochilmagan", mock=card)
    if phase == "closed":
        raise ApiError(409, "Mock yopilgan — natijalar tekshirilgach e'lon qilinadi", mock=card)
    started = await run(db.live_started, test["id"], user["id"])
    if phase == "late" and not started and not await mock_entered(test, user["id"]):
        raise ApiError(409, f"Kirish vaqti tugagan ({test['entry_until'][11:16]} gacha edi)", mock=card)
    choice = await mock_choice_info(test, user)
    if choice:  # avval: esseni kim tekshiradi (AI / ustoz) va pullik bo'lsa — to'lov (karta → chek → AI)
        raise ApiError(402, "Mockni boshlashdan oldin esseni kim tekshirishini tanlang", mock_choice=choice, mock=card)
    if not started:
        await run(db.live_ping, test["id"], user["id"], user["name"] or "Ismsiz", 0, len(test["items"]))


CHOICES = {"ai": "🤖 AI tekshiradi", "teacher": "👩‍🏫 Ustoz tekshiradi"}
DEFAULT_MOCK_CHANNEL = "@Til_va_adabiyot_Xalilov"  # yangi mockning majburiy obunasida oldindan turadi (o'chirsa bo'ladi)


async def mock_entered(test: dict, student_id: int) -> bool:
    """Kirish vaqtida kirgan: mockni boshlagan yoki tanlov / to'lovni boshlagan (to'lov yozuvi faqat kirish vaqtida
    ochiladi). To'lov va chek tekshiruvi cho'zilib, shu orada kirish vaqti o'tsa ham o'quvchi mockdan qolmaydi."""
    if await run(db.live_started, test["id"], student_id):
        return True
    return bool(await run(db.essay_payment_of, test["id"], student_id))


async def mock_choice_info(test: dict, user: dict) -> dict | None:
    """Mockni boshlashdan oldin: esseni kim tekshiradi va narxi (ustoz qo'ygan; o'z o'quvchilari — bepul).
    None — tanlangan va (pullik bo'lsa) to'langan. Pul — ustoz kartasiga, chekni botda AI tekshiradi."""
    if not test.get("opens_at") or test["owner_id"] == user["id"]:
        return None
    pay = await run(db.essay_payment_of, test["id"], user["id"])
    if pay and pay["status"] == "paid":
        return None
    outsider = await is_outsider(test, user["id"])
    # Begonaga ustozning birinchi vaqtli mocki — bepul («Birinchi mockingiz bepul»), keyingilari — narx yoki tarif
    first_free = outsider and not await run(db.had_mock, test["owner_id"], user["id"], test["id"])
    prices = {"ai": int(test.get("price") or 0), "teacher": int(test.get("price_teacher") or 0)} \
        if outsider and not first_free else {"ai": 0, "teacher": 0}
    keys = ["ai", "teacher"] if essay_ai.essay_enabled() else ["teacher"]
    card = await run(teacher_card, test["owner_id"])
    unit = await run(credit_unit_price, test["owner_id"])
    balance = await run(db.wallet_balance, test["owner_id"], user["id"]) if outsider else 0
    return {"options": [{"key": k, "title": CHOICES[k], "price": prices[k],
                         "credits": math.ceil(prices[k] / unit) if unit and prices[k] else 0} for k in keys],
            "balance": balance,  # o'quvchining shu ustozdagi krediti — «kreditdan yechilsinmi?»
            "chosen": pay["choice"] if pay else None, "status": pay["status"] if pay else None,
            "amount": pay["amount"] if pay else 0, "note": pay["note"] if pay and pay["status"] == "rejected" else None,
            "card": card["number"], "holder": card["holder"],
            "first_free": bool(first_free and (test.get("price") or test.get("price_teacher"))),  # pullik mock — birinchisi bepul
            "bot": await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""}


async def mock_choice(request: web.Request) -> web.Response:
    """O'quvchi tanladi: bepul — darhol ochiladi; pullik — bot karta va summani yozadi, chekni AI tekshiradi."""
    user = await auth(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test or not test.get("opens_at"):
        raise ApiError(404, "Mock topilmadi")
    phase = mock_phase(test)
    if phase in ("soon", "closed"):  # tanlov / to'lov — faqat kirish vaqtida (shu bilan «kirgan» hisoblanadi)
        raise ApiError(409, "Mock hali ochilmagan" if phase == "soon" else "Mock yopilgan")
    if phase == "late" and not await mock_entered(test, user["id"]):  # kirish vaqtidan keyin yangi to'lov olinmaydi
        raise ApiError(409, f"Kirish vaqti tugagan ({test['entry_until'][11:16]} gacha edi)")
    info = await mock_choice_info(test, user)
    if not info:
        return ok({"ok": True, "status": "paid"})
    if info["status"] == "review":
        raise ApiError(409, "Chekingiz tekshirilmoqda — kutib turing")
    key = str((await body(request)).get("choice") or "")
    option = next((o for o in info["options"] if o["key"] == key), None)
    if not option:
        raise ApiError(400, "Tanlang: AI tekshiradi yoki ustoz tekshiradi")
    await run(db.remember_user, user["id"], user["name"], user.get("username"))
    if option["price"] and (await body(request)).get("pay") == "credit":  # o'quvchi o'z kreditidan to'laydi
        need = option["credits"]
        if not need or not await run(db.wallet_use_n, test["owner_id"], user["id"], need):
            raise ApiError(409, f"Kredit yetarli emas — {need} kredit kerak, sizda {info['balance']} ta")
        await run(db.mark_mock_paid, test["id"], user["id"], option["price"], key, f"kredit: {need}")
        return ok({"ok": True, "status": "paid", "choice": key, "credits": need,
                   "balance": await run(db.wallet_balance, test["owner_id"], user["id"])})
    if option["price"] and not info["card"]:
        raise ApiError(409, "Ustoz hali kartasini kiritmagan — keyinroq urinib ko'ring")
    pay = await run(db.start_essay_payment, test["id"], user["id"], option["price"], key)
    if option["price"] and pay["status"] == "open" and "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](
                user["id"], f"💳 <b>Mock</b> · {html.escape(test['title'])} — {CHOICES[key]}\n"
                f"Summa: <b>{plans.money(option['price'])} so'm</b>\nKarta: <code>{html.escape(info['card'])}</code>"
                + (f"\nQabul qiluvchi: {html.escape(info['holder'])}" if info["holder"] else "")
                + "\n\nShu summani o'tkazing va <b>chek skrinshotini shu yerga yuboring</b> — AI tekshiradi, to'g'ri bo'lsa "
                  "mock darhol ochiladi.")
        except Exception:
            logger.warning("Mock to'lovi ko'rsatmasi yuborilmadi", exc_info=True)
    return ok({"ok": True, "status": pay["status"], "amount": pay["amount"], "choice": key, "bot": info["bot"]})


def _initials(name: str | None) -> str:
    parts = [p for p in str(name or "").split() if p[:1].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "•"


async def mock_card(test: dict, user: dict, signups: list[dict] | None = None) -> dict:
    """Bosh sahifadagi karta: jadval, qatnashmoqchilar (soni va bosh harflar), men yozilganmanmi, topshirganmanmi."""
    signups = signups if signups is not None else await run(db.mock_signups, test["id"])
    attempt = await run(db.find_attempt, test["id"], user["id"])
    return {"code": test["code"], "title": test["title"], "owner": test.get("owner_name"),
            "schedule": schedule_view(test), "signups": len(signups),
            "faces": [_initials(s["name"]) for s in signups[:4]], "signed": any(s["tg_id"] == user["id"] for s in signups),
            "attempt": attempt["id"] if attempt else None, "announced": bool(test.get("closed")),
            "price": int(test.get("price") or 0) if await is_outsider(test, user["id"]) else 0,
            # Kartadagi izoh: tarifi bilan bepulmi; ustoz tarif sotadimi (sotsa — «tarif olsangiz, hamma mock bepul»)
            "owner_id": test["owner_id"], "has_pass": await pass_active(test["owner_id"], user["id"]),
            "first_free": await is_outsider(test, user["id"]) and not await run(db.had_mock, test["owner_id"], user["id"], test["id"]),
            "sells_pass": bool(await run(student_tariffs, test["owner_id"]))}


async def mock_cards(user: dict) -> list[dict]:
    """Hamma o'quvchiga ko'rinadigan mocklar (FREE_IDS / admin ustozlarniki): bo'lajak, ochiq va 12 soat ichida yopilgan."""
    now = plans.now()
    since = (now - timedelta(hours=12)).strftime(TIME_FMT)
    rows = [t for t in await run(db.public_mocks, since, now.strftime(TIME_FMT)) if plans.ai_unlimited(t["owner_id"])]
    if not rows:
        return []
    return [await mock_card(row, user, await run(db.mock_signups, row["id"])) for row in rows]


async def mock_signup(request: web.Request) -> web.Response:
    """«Qatnashaman» / bekor qilish — mock ochilganda bot eslatadi."""
    user = await auth(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test or not test.get("opens_at"):
        raise ApiError(404, "Mock topilmadi")
    if mock_phase(test) == "closed":
        raise ApiError(409, "Mock yopilgan")
    await run(db.remember_user, user["id"], user["name"], user.get("username"))
    await run(db.mock_signup, test["id"], user["id"], user["name"], bool((await body(request)).get("on", True)))
    return ok(await mock_card(test, user))


def file_token(test_id: int, file_row: int, expires: int) -> str:
    payload = f"{test_id}.{file_row}.{expires}"
    return f"{payload}.{_sign('mf:' + payload)}"


async def files_view(test: dict) -> list[dict]:
    """Savollar fayllari — imzolangan havolalar bilan (rasm <img> da, PDF brauzerda ochiladi; sarlavha kerak emas)."""
    rows = await run(db.test_files, test["id"])
    if not rows:
        return []
    closes = _at(test.get("closes_at"))
    expires = int((closes.timestamp() if closes else time.time()) + 6 * 3600)
    return [{"id": f["id"], "kind": f["kind"], "name": f["name"], "size": f["size"],
             "url": f"/mf/{file_token(test['id'], f['id'], expires)}"} for f in rows]


_FILE_CACHE: dict[str, tuple[bytes, float]] = {}  # file_id → (fayl, oxirgi so'ralgan) — mock paytida yuzlab o'quvchi


async def mock_file(request: web.Request) -> web.StreamResponse:
    parts = request.match_info["token"].split(".")
    if len(parts) != 4 or not all(p.isalnum() for p in parts):
        raise web.HTTPNotFound(text="Havola noto'g'ri")
    test_id, file_row, expires, sig = parts
    if not hmac.compare_digest(sig, _sign(f"mf:{test_id}.{file_row}.{expires}")):
        raise web.HTTPNotFound(text="Havola noto'g'ri")
    if not expires.isdigit() or int(expires) < time.time():
        raise web.HTTPGone(text="Havola eskirgan — ilovada testni qayta oching")
    row = next((f for f in await run(db.test_files, int(test_id)) if f["id"] == int(file_row)), None)
    if not row or "download_file" not in HOOKS:
        raise web.HTTPNotFound(text="Fayl topilmadi")
    cached = _FILE_CACHE.get(row["file_id"])
    if cached:
        data = cached[0]
    else:
        data = await HOOKS["download_file"](row["file_id"])
        if sum(len(v[0]) for v in _FILE_CACHE.values()) > 120 * 1024 * 1024:
            _FILE_CACHE.clear()
        _FILE_CACHE[row["file_id"]] = (data, time.time())
    disposition = "inline" if row["kind"] == "image" else f'inline; filename="savollar_{test_id}.pdf"'
    return web.Response(body=data, headers={"Content-Type": row["mime"] or "application/octet-stream",
                                            "Content-Disposition": disposition, "Cache-Control": "private, max-age=3600"})


def parse_schedule(payload: dict) -> tuple[str, str, str]:
    """{date: 2026-11-20, opens: 20:00, entry: 22:00, closes: 23:00} → uchta «YYYY-MM-DD HH:MM» (tartibi tekshiriladi)."""
    date = str(payload.get("date") or "").strip()
    values = []
    for key, what in (("opens", "Ochilish"), ("entry", "Kirish tugashi"), ("closes", "Yopilish")):
        raw = str(payload.get(key) or "").strip()
        try:
            values.append(datetime.strptime(f"{date} {raw}", TIME_FMT).strftime(TIME_FMT))
        except ValueError:
            raise ApiError(400, f"{what} vaqtini kiriting (sana va soat)")
    opens, entry, closes = values
    if not opens < entry <= closes:
        raise ApiError(400, "Tartib: ochiladi → kirish to'xtaydi → yopiladi (kirish yopilishdan kech bo'lmasin)")
    if _at(closes) - _at(opens) > timedelta(hours=12):
        raise ApiError(400, "Mock 12 soatdan uzun bo'lmasin")
    if _at(closes) < plans.now():
        raise ApiError(400, "Yopilish vaqti o'tib ketgan")
    return opens, entry, closes


async def schedule_save(request: web.Request) -> web.Response:
    """Ustoz: mock jadvali (yoki olib tashlash). «Hammaga ko'rsatish» — faqat FREE_IDS / admin, onlayn milliy mock."""
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    if payload.get("clear"):
        await run(db.set_schedule, test["id"], None, None, None, False)
        return ok({"schedule": None})
    if (test.get("mode") or "online") != "online":
        raise ApiError(400, "Jadval faqat onlayn testga qo'yiladi")
    if not plans.ai_unlimited(user["id"]):
        raise ApiError(403, "«📅 E'lon qilish» — faqat ruxsat berilgan ustozlar uchun")
    opens, entry, closes = parse_schedule(payload)
    public = bool(payload.get("public"))
    if public and (not plans.ai_unlimited(user["id"]) or test["fmt"] != "milliy"):
        raise ApiError(403, "Hammaga ko'rsatish — faqat ruxsat berilgan ustozlarning milliy mocki uchun")
    # Narxlar (begonalar uchun; o'z o'quvchilaringiz — bepul): esseni AI tekshirsa / ustoz tekshirsa. 0 — bepul
    prices = []
    for key, what in (("price_ai", "AI tekshiruvi"), ("price_teacher", "Ustoz tekshiruvi")):
        price = to_int(payload.get(key), 0, f"{what} narxi")
        if price and not 1000 <= price <= 1_000_000:
            raise ApiError(400, f"{what} narxi: 0 (bepul) yoki 1 000 – 1 000 000 so'm")
        prices.append(price)
    if any(prices) and not (await run(teacher_card, user["id"]))["number"]:
        raise ApiError(400, "Pullik mock uchun avval «⚙️ Sozlamalar»da kartangizni kiriting")
    await run(db.set_schedule, test["id"], opens, entry, closes, public)
    await run(db.set_mock_prices, test["id"], *prices)
    return ok({"schedule": schedule_view(await run(db.get_test, test["id"]))})


FILE_MAX = 20 * 1024 * 1024  # Telegram bot API yuklab olish chegarasi
MAX_TEST_FILES = 30


async def files_upload(request: web.Request) -> web.Response:
    """Savollar: PDF va/yoki rasmlar (bir nechta). Telegram'da saqlanadi (ustoz chatiga hujjat bo'lib boradi)."""
    user = await auth(request)
    test = await own_test(request, user)
    try:
        form = await request.post()
    except Exception:
        raise ApiError(400, "Fayl yuborilmadi")
    fields = [f for f in form.getall("file", []) if hasattr(f, "file")]
    if not fields:
        raise ApiError(400, "PDF yoki rasm tanlang")
    if len(await run(db.test_files, test["id"])) + len(fields) > MAX_TEST_FILES:
        raise ApiError(400, f"Bitta testga ko'pi bilan {MAX_TEST_FILES} ta fayl")
    if "store_file" not in HOOKS:
        raise ApiError(503, "Fayl saqlab bo'lmadi")
    added = []
    for field in fields:
        data = field.file.read()
        mime = (getattr(field, "content_type", None) or "").lower()
        name = str(field.filename or "fayl")[:80]
        kind = "pdf" if mime == "application/pdf" or name.lower().endswith(".pdf") else "image" if mime.startswith("image/") else None
        if not kind:
            raise ApiError(400, f"«{name}» — faqat PDF yoki rasm (JPG, PNG)")
        if not data or len(data) > FILE_MAX:
            raise ApiError(400, f"«{name}» — 20 MB dan kichik bo'lsin")
        file_id = await HOOKS["store_file"](user["id"], data, name, f"📎 Savollar · {test['title']} (kod {test['code']})")
        added.append(await run(db.add_test_file, test["id"], kind, file_id, mime or "application/pdf", name, len(data)))
    return ok({"files": await files_view(test), "added": len(added)})


async def file_delete(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    if not await run(db.delete_test_file, test["id"], int(request.match_info["file_id"])):
        raise ApiError(404, "Fayl topilmadi")
    return ok({"files": await files_view(test)})


async def wallet_view(test: dict, user: dict) -> dict | None:
    """Begona o'quvchining shu ustozdagi krediti (bo'lsa): esse maydonida «1 kredit yechiladi» deb ko'rsatiladi."""
    if test["fmt"] != "milliy" or not await is_outsider(test, user["id"]):
        return None
    balance = await run(db.wallet_balance, test["owner_id"], user["id"])
    return {"balance": balance} if balance > 0 else None


# ---------- Qog'ozdagi esse rasmi → matn (onlayn mock): ilovada yoki botda; qaysi AI o'qishini o'quvchi tanlaydi ----------

async def _scan_test(request: web.Request, user: dict) -> dict:
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test or test["fmt"] != "milliy" or (test.get("mode") or "online") != "online":
        raise ApiError(404, "Test topilmadi")
    if test["closed"]:
        raise ApiError(409, "Natijalar e'lon qilingan — javob qabul qilinmaydi")
    if await run(db.find_attempt, test["id"], user["id"]):
        raise ApiError(409, "Siz bu testga javob yuborgansiz")
    pay = await essay_pay_info(test, user)
    if pay:
        raise ApiError(402, "Esseni yuklashdan oldin esse tekshiruvini to'lang", essay_pay=pay)
    return test


def _reader(raw) -> str:
    keys = [r["key"] for r in essay_ai.readers()]
    if not keys:
        raise ApiError(409, "Esse rasmini o'qiydigan AI hozir ulanmagan — esseni yozib kiriting")
    return raw if raw in keys else keys[0]


def _scan_view(scan: dict | None) -> dict:
    if not scan:
        return {"status": None}
    return {"status": scan["status"], "ai": scan["ai"], "text": scan["text"] if scan["status"] == "done" else None,
            "model": scan["model"], "pages": len(scan["pages"] or []), "error": scan["error"]}


async def read_essay_scan(student_id: int, test_id: int, notify: bool) -> dict:
    """Yig'ilgan sahifalarni AI o'qiydi → matn saqlanadi; notify — botga ham (matn chatga)."""
    scan = await run(db.get_essay_scan, student_id, test_id)
    if not scan or not scan["pages"]:
        raise ApiError(400, "Avval esse rasmini yuboring")
    await run(db.set_essay_scan, student_id, test_id, "reading", None, None, None, True)
    images = [(base64.b64decode(b64), mime) for b64, mime in scan["pages"]]
    try:
        text, model = await asyncio.wait_for(essay_ai.transcribe(images, scan["ai"]), ESSAY_SCAN_TIMEOUT)
    except Exception as error:
        logger.warning("Esse rasmi o'qilmadi: %s", error)
        await run(db.set_essay_scan, student_id, test_id, "failed", None, None, "AI rasmni o'qiy olmadi", True)
        if notify and "send_text" in HOOKS:
            await HOOKS["send_text"](student_id, "⚠️ AI rasmni o'qiy olmadi. Rasmni yorug' joyda, tekis qilib qayta oling "
                                                 "yoki esseni ilovada yozib kiriting.")
        return _scan_view(await run(db.get_essay_scan, student_id, test_id))
    await run(db.set_essay_scan, student_id, test_id, "done", text, model)
    if notify and "essay_scan_notice" in HOOKS:
        try:
            await HOOKS["essay_scan_notice"](student_id, test_id, text, model)
        except Exception:
            logger.warning("Esse matni botga yuborilmadi", exc_info=True)
    return _scan_view(await run(db.get_essay_scan, student_id, test_id))


ESSAY_SCAN_TIMEOUT = 150


async def essay_scan_get(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test:
        raise ApiError(404, "Test topilmadi")
    return ok(_scan_view(await run(db.get_essay_scan, user["id"], test["id"])))


async def essay_scan_start(request: web.Request) -> web.Response:
    """«Botga yuboraman»: bot esse sahifalarini kutadi, o'quvchi yuborib «✅ Tayyor» ni bosadi."""
    user = await auth(request)
    test = await _scan_test(request, user)
    ai = _reader((await body(request)).get("ai"))
    await run(db.remember_user, user["id"], user["name"], user.get("username"))
    await run(db.start_essay_scan, user["id"], test["id"], ai)
    if "essay_scan_start" in HOOKS:
        try:
            await HOOKS["essay_scan_start"](user["id"], test, ai)
        except Exception:
            logger.warning("Esse rasmi ko'rsatmasi yuborilmadi", exc_info=True)
    return ok({"ok": True, "status": "collecting", "bot": await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""})


async def essay_scan_upload(request: web.Request) -> web.Response:
    """Ilovaning o'zida: esse sahifalari rasmi (1–4) → AI darhol o'qiydi → matn."""
    user = await auth(request)
    test = await _scan_test(request, user)
    try:
        form = await request.post()
    except Exception:
        raise ApiError(400, "Rasm yuborilmadi")
    ai = _reader(form.get("ai"))
    files = [f for f in form.getall("photo", []) if hasattr(f, "file")][:db.ESSAY_SCAN_PAGES]
    if not files:
        raise ApiError(400, "Esse rasmini tanlang")
    await run(db.start_essay_scan, user["id"], test["id"], ai)
    for field in files:
        data = field.file.read()
        if not data or len(data) > 8 * 1024 * 1024:
            raise ApiError(400, "Har rasm 8 MB dan kichik bo'lsin")
        await run(db.add_essay_scan_page, user["id"], test["id"], base64.b64encode(data).decode(),
                  (getattr(field, "content_type", None) or "image/jpeg")[:30])
    return ok(await read_essay_scan(user["id"], test["id"], notify=False))


async def _teacher_charge(owner_id: int) -> str | None:
    """Ustozning 1 AI krediti: yechildi — "teacher" (AI cheksiz bo'lsa — "free", qaytarish shart emas), yetmadi — None."""
    if not await ai_allowed(owner_id, "essays"):
        return None
    return "free" if plans.ai_unlimited(owner_id) else "teacher"


async def essay_ai_charge(test: dict, student_id: int) -> str | None:
    """Onlayn mock essesini AI tekshiradimi va kimning hisobidan: ustozning o'quvchisi — ustoz kreditidan; begona —
    o'zi to'lagan bo'lsa. None — AI tekshirmaydi; "free" — kredit yechilmadi (o'quvchi pul to'lagan / AI cheksiz);
    "wallet" — o'quvchining shu ustozdagi 1 krediti; "teacher" — ustozning 1 krediti. AI esseni tekshira olmasa,
    yechilgan kredit qaytariladi (bot.grade_one)."""
    if not essay_ai.essay_enabled():
        return None
    if test.get("opens_at"):  # «Bugungi mock»: o'quvchi tanlovi — «🤖 AI tekshirsin» (to'langan) yoki «👩‍🏫 Ustoz»
        pay = await run(db.essay_payment_of, test["id"], student_id)
        if not pay or pay["status"] != "paid" or pay.get("choice") != "ai":
            return None
        return "free" if pay["amount"] > 0 else await _teacher_charge(test["owner_id"])  # bepul — ustoz kreditidan
    if await is_outsider(test, student_id):
        pay = await run(db.essay_payment_of, test["id"], student_id)
        if pay and pay["status"] == "paid":
            return "free"  # ustoz krediti ishlatilmaydi
        # yoki o'quvchining shu ustozdagi 1 krediti
        return "wallet" if await run(db.wallet_use, test["owner_id"], student_id) else None
    return await _teacher_charge(test["owner_id"])


async def essay_ai_ok(test: dict, student_id: int) -> bool:
    return bool(await essay_ai_charge(test, student_id))


async def _paid_test(request: web.Request, user: dict) -> dict:
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test or (test.get("mode") or "online") != "online" or test["fmt"] != "milliy":
        raise ApiError(404, "Test topilmadi")
    if test.get("opens_at"):  # jadvalli mockda to'lov — faqat «mock-choice» orqali (kirish vaqti tekshiriladi)
        raise ApiError(409, "To'lov kerak emas")
    if test["closed"]:
        raise ApiError(409, "Mock yopilgan — natijalar e'lon qilingan")
    if not await is_outsider(test, user["id"]):
        raise ApiError(409, "Siz ustozning o'quvchisisiz — to'lov kerak emas")
    payee = await run(payee_of, test["owner_id"])
    if not test.get("price") or not (await run(pay_card, test["owner_id"], payee))["number"]:
        raise ApiError(409, "To'lov kerak emas")
    return test


async def essay_pay_start(request: web.Request) -> web.Response:
    """«Chekni botga yuborish»: to'lov ochiladi va bot karta + summani yozib, chek skrinshotini so'raydi."""
    user = await auth(request)
    test = await _paid_test(request, user)
    await run(db.remember_user, user["id"], user["name"], user.get("username"))
    payee = await run(payee_of, test["owner_id"])
    price, card = await run(essay_price_of, test), await run(pay_card, test["owner_id"], payee)
    pay = await run(db.start_essay_payment, test["id"], user["id"], price, None, payee)
    if pay["status"] == "open" and "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](
                user["id"], f"💳 <b>Esse tekshiruvi</b> · {html.escape(test['title'])} (kod {test['code']})\n"
                f"Summa: <b>{plans.money(price)} so'm</b>\nKarta: <code>{html.escape(card['number'])}</code>"
                + (f"\nQabul qiluvchi: {html.escape(card['holder'])}" if card["holder"] else "")
                + "\n\nShu summani o'tkazing va <b>chek skrinshotini shu yerga yuboring</b> — AI tekshiradi, "
                  "to'g'ri bo'lsa mock darhol ochiladi.")
        except Exception:
            logger.warning("To'lov ko'rsatmasi yuborilmadi", exc_info=True)
    return ok({"ok": True, "status": pay["status"],
               "bot": await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""})


def _digits(text) -> str:
    return "".join(ch for ch in str(text or "") if ch.isdigit())


RECEIPT_AI_TIMEOUT = 90


async def verify_receipt(data: bytes, mime: str, amount: int, card: dict) -> tuple[bool, list[str], str | None]:
    """AI chekni o'qiydi, qolganini kod solishtiradi: bugungi sana, summa, qabul qiluvchi karta (oxirgi 4 raqam) yoki ismi."""
    if not essay_ai.enabled():
        return False, ["AI o'chiq — admin tekshiradi"], None
    try:  # AI 90 soniyada javob bermasa — chek ustozga (qo'lda)
        r = await asyncio.wait_for(essay_ai.read_receipt(data, mime), RECEIPT_AI_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Chekni AI vaqtida o'qimadi")
        return False, ["AI javob bermadi — ustoz tekshiradi"], None
    except Exception as error:
        logger.warning("Chekni AI o'qiy olmadi: %s", error)
        return False, ["AI chekni o'qiy olmadi — ustoz tekshiradi"], None
    reasons = []
    if not r.get("is_receipt"):
        reasons.append("rasm to'lov chekiga o'xshamaydi")
    elif not r.get("success"):
        reasons.append("to'lov muvaffaqiyatli deb ko'rsatilmagan")
    try:
        paid = int(float(r.get("amount") or 0))
    except (TypeError, ValueError):
        paid = 0
    if paid < amount:
        reasons.append(f"summa {plans.money(paid)} so'm — {plans.money(amount)} so'm kerak")
    now = plans.now().astimezone(plans.TASHKENT)
    days = {now.strftime("%Y-%m-%d")} | ({(now - timedelta(hours=1)).strftime("%Y-%m-%d")} if now.hour < 1 else set())
    if str(r.get("date") or "").strip()[:10] not in days:
        reasons.append(f"sana {r.get('date') or '?'} — bugungi chek emas")
    last4, shown = _digits(card["number"])[-4:], _digits(r.get("recipient_card"))
    card_ok = bool(last4) and len(shown) >= 4 and shown.endswith(last4)
    if not card_ok and not names_match(r.get("recipient_name"), card["holder"]):
        reasons.append("qabul qiluvchi karta yoki ismi mos emas")
    if r.get("looks_edited"):
        reasons.append("rasm tahrirlanganga o'xshaydi")
    txn = str(r.get("transaction_id") or "").strip()[:60] or None
    return not reasons, reasons, txn


async def check_essay_receipt(payment_id: int, data: bytes, mime: str) -> dict:
    """Botga yuborilgan chek: AI to'g'ri desa — «paid», esse ochiladi; aks holda ustozga, o'quvchiga «kutib turing»."""
    pay = await run(db.get_essay_payment, payment_id)
    if not pay:
        raise ApiError(404, "To'lov topilmadi")
    if pay["status"] in ("paid", "review"):
        return {"ok": True, "status": pay["status"], "reasons": []}
    digest = hashlib.sha256(data).hexdigest()
    good, reasons, txn = await read_receipt_for(pay, pay["owner_id"], data, mime)
    if await run(db.receipt_used, digest, txn, payment_id):
        good, reasons = False, reasons + ["bu chek oldin ishlatilgan"]
    status = "paid" if good else "review"
    await run(db.save_essay_receipt, payment_id, base64.b64encode(data).decode(), mime, digest, txn, "; ".join(reasons), status)
    if good:
        await credit_share("essay", pay, pay["owner_id"])
    if "send_text" in HOOKS:
        text = (f"✅ <b>To'landi!</b> · {html.escape(pay['title'])}\nEsse maydoni ochildi — ilovada kod "
                f"<b>{pay['code']}</b> ni kiriting. Essengizni AI imtihondagidek tekshiradi." if good else
                f"⏳ <b>Chek tekshirilmoqda — kutib turing</b> · {html.escape(pay['title'])}\n"
                "Ustoz ko'rib chiqadi va bot xabar beradi.")
        try:
            await HOOKS["send_text"](pay["student_id"], text)
        except Exception:
            logger.warning("To'lov natijasi o'quvchiga yuborilmadi", exc_info=True)
    if "essay_receipt_notice" in HOOKS:
        try:
            await HOOKS["essay_receipt_notice"](payment_id, good, reasons, data)
        except Exception:
            logger.warning("Chek adminlarga yuborilmadi", exc_info=True)
    return {"ok": True, "status": status, "reasons": reasons}


async def essay_payments_list(request: web.Request) -> web.Response:
    """Ustoz ilovada: begonalarning esse to'lovlari (bot xabari yetmasa ham shu yerda ko'rinadi)."""
    user = await auth(request)
    return ok({"payments": await run(db.essay_payments_for_owner, user["id"])})


async def essay_payment_receipt(request: web.Request) -> web.Response:
    user = await auth(request)
    pay = await run(db.get_essay_payment, int(request.match_info["payment_id"]))
    if not pay or pay["owner_id"] != user["id"] or not pay["receipt"]:
        raise ApiError(404, "Chek topilmadi")
    return ok({"image": _image_data_url(pay["receipt"], pay["mime"])})


async def essay_payment_decide(request: web.Request) -> web.Response:
    user = await auth(request)
    action = (await body(request)).get("action")
    if action not in ("approve", "reject"):
        raise ApiError(400, "Amal noto'g'ri")
    return ok(await decide_essay_payment(int(request.match_info["payment_id"]), action == "approve", user["id"]))


async def decide_essay_payment(payment_id: int, approve: bool, by_id: int) -> dict:
    """Mock egasi (yoki admin): chekni tasdiqlaydi (esse ochiladi) yoki rad etadi / AI tasdiqlaganini bekor qiladi."""
    pay = await run(db.get_essay_payment, payment_id)
    platform = bool(pay) and pay.get("payee") == "platform"
    if not pay or (pay["owner_id"] != by_id and by_id not in ADMIN_IDS and not (platform and platform_decider(by_id))):
        raise ApiError(404, "To'lov topilmadi")
    await guard_platform_decision("essay", pay, approve, by_id)
    allowed = ("open", "review", "rejected") if approve else ("open", "review", "paid")
    if not await run(db.decide_essay_payment, payment_id, "paid" if approve else "rejected", allowed):
        raise ApiError(409, "Bu to'lov allaqachon ko'rib chiqilgan")
    if approve:
        await credit_share("essay", pay, pay["owner_id"])
        await teacher_approved_notice("essay", pay, by_id)
    else:
        await run(db.remove_earning, "essay", payment_id)
    if "send_text" in HOOKS:
        text = (f"✅ <b>To'lov tasdiqlandi</b> · {html.escape(pay['title'])}\nEsse maydoni ochildi — kod "
                f"<b>{pay['code']}</b>." if approve else
                f"❌ <b>To'lov tasdiqlanmadi</b> · {html.escape(pay['title'])}\nChek mos kelmadi (summa, sana yoki karta). "
                "To'g'ri chekni qayta yuboring yoki ustoz bilan bog'laning.")
        try:
            await HOOKS["send_text"](pay["student_id"], text)
        except Exception:
            logger.warning("To'lov qarori o'quvchiga yuborilmadi", exc_info=True)
    return {"ok": True, "status": "paid" if approve else "rejected", "title": pay["title"]}


# ---------- Begonaning esse krediti: paket bir marta to'lanadi, keyin har esse — 1 kredit (chek har safar emas) ----------
# Kredit har ustozda alohida (pul o'sha ustoz kartasiga tushadi). Narxlarni ustoz o'zi qo'yadi: 1 ta esse narxi
# (essay_price) va 2–100 talik paketlar (essay_packs). Botdagi ommaviy havola va onlayn mock — ikkalasi shu kreditdan.

MAX_PACKS = 9  # 1 talikdan tashqari nechta paket (ilovada jami 10 qator)
DEFAULT_PACKS = (1, 2, 5, 10, 30, 50)  # ilovada oldindan yozilgan kredit sonlari — ustoz narxini qo'yadi
PACK_PRICE_MIN, PACK_PRICE_MAX = 1000, 5_000_000


def essay_packs(teacher_id: int) -> list[dict]:
    """Ustozning paketlari: [{essays, price, discount}], 1 talik (esse narxi) birinchi. Chegirma — 1 talikka nisbatan."""
    saved = db.get_settings()
    floor = plans.min_essay_price() if plans.platform_routed(teacher_id) else 0  # oldin qo'yilgan arzon narx ham
    single = int(saved.get(f"essay_price:{teacher_id}") or 0)
    single = max(single, floor) if single else 0
    out = [{"essays": 1, "price": single}] if single else []
    try:
        raw = json.loads(saved.get(f"essay_packs:{teacher_id}") or "[]")
    except ValueError:
        raw = []
    for item in raw if isinstance(raw, list) else []:
        try:
            essays, price = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 2 <= essays <= 100 and PACK_PRICE_MIN <= price <= PACK_PRICE_MAX and all(p["essays"] != essays for p in out):
            out.append({"essays": essays, "price": max(price, floor * essays)})
    out.sort(key=lambda p: p["essays"])
    for p in out:
        full = single * p["essays"]
        p["discount"] = round(100 - p["price"] * 100 / full) if single and p["essays"] > 1 and p["price"] < full else 0
    return out


def clean_packs(rows) -> list[list[int]]:
    """Ustoz kiritgan paketlar [(nechta esse, narx), ...] → saqlanadigan ro'yxat. Xato bo'lsa — ValueError (sababi bilan)."""
    out: list[list[int]] = []
    for essays, price in rows:
        try:
            essays, price = int(essays), int(price)
        except (TypeError, ValueError):
            raise ValueError("Paket: esse soni va narxi son bo'lsin")
        if not 2 <= essays <= 100:
            raise ValueError("Paketdagi kredit soni 2–100 bo'lsin (1 talik — «1 esse narxi»)" if essays == 1
                             else "Har qatorda kredit soni 1–100 bo'lsin")
        if not PACK_PRICE_MIN <= price <= PACK_PRICE_MAX:
            raise ValueError(f"Paket narxi {plans.money(PACK_PRICE_MIN)} – {plans.money(PACK_PRICE_MAX)} so'm bo'lsin")
        if any(p[0] == essays for p in out):
            raise ValueError(f"{essays} talik paket ikki marta yozilgan")
        out.append([essays, price])
    if len(out) > MAX_PACKS:
        raise ValueError(f"Ko'pi bilan {MAX_PACKS} ta paket")
    return sorted(out)


def save_packs(teacher_id: int, single: int | None, rows) -> list[dict]:
    """1 ta esse narxi (None — o'zgarmaydi) va paketlar saqlanadi."""
    packs = clean_packs(rows)
    floor = plans.min_essay_price() if plans.platform_routed(teacher_id) else 0
    for essays, price in packs:
        if price < floor * essays:
            raise ValueError(f"Har bir kredit kamida {plans.money(floor)} so'm: {essays} ta — kamida "
                             f"{plans.money(floor * essays)} so'm")
    if single is not None:
        if single and not PACK_PRICE_MIN <= single <= 1_000_000:
            raise ValueError("1 ta esse narxi 1 000 – 1 000 000 so'm bo'lsin")
        if single and single < floor:
            raise ValueError(f"1 ta esse narxi kamida {plans.money(floor)} so'm bo'lsin")
        db.set_setting(f"essay_price:{teacher_id}", str(single))
    db.set_setting(f"essay_packs:{teacher_id}", json.dumps(packs))
    return essay_packs(teacher_id)


def packs_text(packs: list[dict]) -> str:
    """Bot xabari uchun: «1 ta — 5 000 · 5 ta — 22 000 (−12%)»."""
    return " · ".join(f"{p['essays']} ta — {plans.money(p['price'])}" + (f" (−{p['discount']}%)" if p["discount"] else "")
                      for p in packs) or "narx qo'yilmagan"


async def wallet_order(teacher_id: int, student: dict, essays: int) -> dict:
    """O'quvchi paket tanladi: to'lov ochiladi, bot karta va summani yozadi — chek skrinshotini shu yerga yuboradi."""
    if teacher_id == student["id"]:
        raise ApiError(400, "O'zingizdan kredit sotib ololmaysiz")
    pack = next((p for p in await run(essay_packs, teacher_id) if p["essays"] == essays), None)
    payee = await run(payee_of, teacher_id)
    card = await run(pay_card, teacher_id, payee)
    if not pack or not card["number"]:
        raise ApiError(409, "Ustoz bu paketni sotmayapti — narx yoki karta qo'yilmagan")
    await run(db.remember_user, student["id"], student.get("name"), student.get("username"))
    pay = await run(db.start_wallet_payment, teacher_id, student["id"], pack["essays"], pack["price"], payee)
    if pay["status"] == "review":
        raise ApiError(409, "Oldingi chekingiz hali tekshirilmoqda — tasdiqlangach yangi paket olasiz")
    if "send_text" in HOOKS:
        teacher = await run(db.find_user, str(teacher_id)) or {}
        try:
            await HOOKS["send_text"](
                student["id"], f"💳 <b>Esse krediti · {pack['essays']} ta</b> — {html.escape(teacher.get('full_name') or 'ustoz')}\n"
                f"Summa: <b>{plans.money(pack['price'])} so'm</b>\nKarta: <code>{html.escape(card['number'])}</code>"
                + (f"\nQabul qiluvchi: {html.escape(card['holder'])}" if card["holder"] else "")
                + "\n\nShu summani o'tkazing va <b>chek skrinshotini shu yerga yuboring</b> — AI tekshiradi, to'g'ri bo'lsa "
                  "kredit darhol qo'shiladi. Har esse — 1 kredit, muddati yo'q.")
        except Exception:
            logger.warning("Kredit to'lovi ko'rsatmasi yuborilmadi", exc_info=True)
    return {"ok": True, "payment": pay, "card": card,
            "bot": await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""}


async def release_waiting(teacher_id: int, student_id: int) -> tuple[int, bool]:
    """Kredit kutayotgan esselar (eng eskisidan): har biriga 1 kredit va ustozning 1 AI krediti → AI navbatiga.
    Qaytaradi: (nechtasi navbatga qo'yildi, ustozning AI krediti tugaganmi)."""
    released, teacher_out = 0, False
    routed = await run(plans.platform_routed, teacher_id)  # kredit puli platformada — AI'ni platforma to'laydi
    for practice in await run(db.practice_awaiting_credit, teacher_id, student_id):
        if not await run(db.wallet_use, teacher_id, student_id):
            break
        if not routed and not await ai_allowed(teacher_id, "essays"):
            await run(db.wallet_add, teacher_id, student_id, 1)  # AI tekshirmaydi — kredit yechilmaydi
            teacher_out = True
            break
        await run(db.set_practice_wallet, practice["id"], "pending")
        released += 1
    if released and "wake_ai" in HOOKS:
        HOOKS["wake_ai"]()
    return released, teacher_out


async def credited(pay: dict, balance: int, by_ai: bool) -> None:
    """Kredit qo'shildi: o'quvchiga xabar va kutib turgan esselari tekshiruvga."""
    released, teacher_out = await release_waiting(pay["teacher_id"], pay["student_id"])
    balance -= released
    text = (f"✅ <b>Kredit qo'shildi: +{pay['essays']} ta</b>" + (" (chek AI'da tasdiqlandi)" if by_ai else "")
            + f"\n💳 Balans: <b>{max(0, balance)} ta</b> esse")
    if released:
        text += f"\n⏳ Saqlab qo'yilgan {released} ta essengiz tekshirilmoqda — natija shu yerga keladi."
    elif teacher_out:
        text += ("\n⚠️ Ustozning AI krediti hozir tugagan — saqlangan essengiz kutib turibdi (kredit yechilmadi). "
                 "Ustozga ayting; keyingi esse yuborganingizda birinchi bo'lib tekshiriladi.")
    else:
        text += "\nEndi «✍️ Esse yuborish» — har esse 1 kredit, chek kerak emas."
    if "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](pay["student_id"], text)
        except Exception:
            logger.warning("Kredit xabari o'quvchiga yuborilmadi", exc_info=True)


async def check_wallet_receipt(payment_id: int, data: bytes, mime: str) -> dict:
    """Botga yuborilgan kredit cheki: AI to'g'ri desa — kredit darhol; aks holda ustozga, o'quvchiga «kutib turing»."""
    pay = await run(db.get_wallet_payment, payment_id)
    if not pay:
        raise ApiError(404, "To'lov topilmadi")
    if pay["status"] in ("paid", "review"):
        return {"ok": True, "status": pay["status"], "reasons": []}
    digest = hashlib.sha256(data).hexdigest()
    good, reasons, txn = await read_receipt_for(pay, pay["teacher_id"], data, mime)
    if await run(db.receipt_used, digest, txn, payment_id, "wallet_payments"):
        good, reasons = False, reasons + ["bu chek oldin ishlatilgan"]
    await run(db.save_wallet_receipt, payment_id, base64.b64encode(data).decode(), mime, digest, txn, "; ".join(reasons))
    balance = await run(db.pay_wallet_payment, payment_id, ("review",)) if good else None
    if balance is not None:
        await credit_share("wallet", pay, pay["teacher_id"])
        await credited(pay, balance, by_ai=True)
    elif "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](pay["student_id"], f"⏳ <b>Chek tekshirilmoqda — kutib turing</b> · {pay['essays']} ta kredit\n"
                                                        "Ustoz ko'rib chiqadi va bot xabar beradi.")
        except Exception:
            logger.warning("Chek holati o'quvchiga yuborilmadi", exc_info=True)
    if "wallet_receipt_notice" in HOOKS:
        try:
            await HOOKS["wallet_receipt_notice"](payment_id, balance is not None, reasons, data)
        except Exception:
            logger.warning("Kredit cheki ustozga yuborilmadi", exc_info=True)
    return {"ok": True, "status": "paid" if balance is not None else "review", "reasons": reasons}


async def decide_wallet_payment(payment_id: int, approve: bool, by_id: int) -> dict:
    """Ustoz (yoki admin): kredit chekini tasdiqlaydi yoki rad etadi / AI tasdiqlaganini bekor qiladi (kredit qaytib olinadi)."""
    pay = await run(db.get_wallet_payment, payment_id)
    platform = bool(pay) and pay.get("payee") == "platform"
    if not pay or (pay["teacher_id"] != by_id and by_id not in ADMIN_IDS and not (platform and platform_decider(by_id))):
        raise ApiError(404, "To'lov topilmadi")
    await guard_platform_decision("wallet", pay, approve, by_id)
    if approve:
        balance = await run(db.pay_wallet_payment, payment_id, ("open", "review", "rejected"))
        if balance is None:
            raise ApiError(409, "Bu to'lov allaqachon ko'rib chiqilgan")
        await credit_share("wallet", pay, pay["teacher_id"])
        await teacher_approved_notice("wallet", pay, by_id)
        await credited(pay, balance, by_ai=False)
    else:
        if not await run(db.reject_wallet_payment, payment_id, ("open", "review", "paid")):
            raise ApiError(409, "Bu to'lov allaqachon ko'rib chiqilgan")
        await run(db.remove_earning, "wallet", payment_id)
        if "send_text" in HOOKS:
            try:
                await HOOKS["send_text"](pay["student_id"], f"❌ <b>Kredit to'lovi tasdiqlanmadi</b> · {pay['essays']} ta\n"
                                         "Chek mos kelmadi (summa, sana yoki karta). To'g'ri chekni qayta yuboring yoki ustozga yozing.")
            except Exception:
                logger.warning("Kredit qarori o'quvchiga yuborilmadi", exc_info=True)
    return {"ok": True, "status": "paid" if approve else "rejected", "essays": pay["essays"],
            "name": pay["full_name"] or "O'quvchi"}


async def wallet_order_for_test(request: web.Request) -> web.Response:
    """Onlayn mock: begona o'quvchi ustozning kredit paketini oladi (shu ustozning hamma mocklari va ommaviy havolasi uchun)."""
    user = await auth(request)
    test = await _paid_test(request, user)
    return ok(await wallet_order(test["owner_id"], user, to_int((await body(request)).get("essays"), 1, "Paket")))


async def packs_save(request: web.Request) -> web.Response:
    user = await auth(request)
    payload = await body(request)
    rows = payload.get("packs") or []
    if not isinstance(rows, list):
        raise ApiError(400, "Paketlar noto'g'ri")
    # Narxi yozilmagan qator saqlanmaydi (kredit soni oldindan yozilgan bo'ladi). 1 kreditlik qator — «1 esse narxi»
    rows = [(to_int(p.get("essays"), 0, "Kredit soni"), to_int(p.get("price"), 0, "Narx"))
            for p in rows if isinstance(p, dict) and str(p.get("price") or "").strip() not in ("", "0")]
    singles = [price for essays, price in rows if essays == 1]
    if len(singles) > 1:
        raise ApiError(400, "1 kreditlik narx ikki marta yozilgan")
    rows = [row for row in rows if row[0] != 1]
    if payload.get("price") is not None:  # eski ko'rinish: alohida «1 esse narxi»
        singles = [to_int(payload.get("price"), 0, "Esse narxi")]
    single = singles[0] if singles else 0
    try:
        packs = await run(save_packs, user["id"], single, rows)
    except ValueError as error:
        raise ApiError(400, str(error))
    return ok({"ok": True, "packs": packs})


# ---------- 💎 Obuna tariflari (admin va FREE_IDS): tariflar, to'lov kartasi, sotuvni yoqish ----------

async def sales_admin_view() -> dict:
    card, tariffs, on = await asyncio.gather(run(plans.card), run(plans.tariffs), run(plans.sales_on))
    pending = await run(db.list_payments, "sent", 20)
    return {"tariffs": tariffs, "card": card["number"], "holder": card["holder"], "sales_on": on,
            "forced": bool(SALES_ENABLED),  # Render'da PLANS_ENABLED=1 — ilovadan o'chirib bo'lmaydi
            "pending": len(pending), "revshare": await revshare_admin_view()}


async def revshare_admin_view() -> dict:
    """«💰 Begonalar puli» (admin): ulush, minimal narx, yechish chegarasi, shu oy tushum va kutilayotgan yechishlar."""
    share, floor, minimum = await asyncio.gather(run(plans.platform_share), run(plans.min_essay_price),
                                                 run(plans.withdraw_min))
    income, waiting = await asyncio.gather(run(db.platform_income, plans.month_key()), run(db.pending_withdrawals))
    return {"platform_share": share, "min_essay_price": floor, "withdraw_min": minimum, "income": income,
            "withdrawals": [{"id": w["id"], "amount": w["amount"], "card": w["card"], "holder": w["holder"],
                             "name": w.get("full_name") or str(w["teacher_id"]), "username": w.get("username"),
                             "created_at": w["created_at"]} for w in waiting]}


async def withdrawal_admin_decide(request: web.Request) -> web.Response:
    """Admin ilovada: yechish so'rovi — «✅ To'landi» (pul o'tkazildi) yoki «❌ Rad etish» (summa balansga qaytadi)."""
    user = await auth(request)
    action = (await body(request)).get("action")
    if action not in ("paid", "reject"):
        raise ApiError(400, "Amal noto'g'ri")
    await decide_withdrawal(int(request.match_info["withdrawal_id"]), action == "paid", user["id"])
    return ok(await sales_admin_view())


async def sales_admin(request: web.Request) -> web.Response:
    user = await auth(request)
    if not plans.can_manage(user["id"]):
        raise ApiError(403, "Bu bo'lim faqat admin uchun")
    return ok(await sales_admin_view())


async def sales_admin_save(request: web.Request) -> web.Response:
    """Tariflar ([oy, narx] qatorlari), to'lov kartasi (16 raqam + egasi — AI chekda shu ismni tekshiradi)
    va «obuna sotilsin» tugmasi. Yoqish uchun kamida bitta tarif va karta kerak."""
    user = await auth(request)
    if not plans.can_manage(user["id"]):
        raise ApiError(403, "Bu bo'lim faqat admin uchun")
    payload = await body(request)
    if "tariffs" in payload:
        raw = payload.get("tariffs")
        if not isinstance(raw, list):
            raise ApiError(400, "Tariflar noto'g'ri")
        rows = {}
        for item in raw:
            if not isinstance(item, dict) or str(item.get("price") or "").strip() in ("", "0"):
                continue  # narxi yozilmagan qator — saqlanmaydi
            months, price = to_int(item.get("months"), 0, "Oy soni"), to_int(item.get("price"), 0, "Narx")
            if not 1 <= months <= plans.TARIFF_MONTHS_MAX:
                raise ApiError(400, f"Oy soni 1 dan {plans.TARIFF_MONTHS_MAX} gacha bo'lsin")
            if price < 1000:
                raise ApiError(400, f"{months} oylik narx juda kichik — so'mda yozing (masalan 200000)")
            if months in rows:
                raise ApiError(400, f"{months} oylik tarif ikki marta yozilgan")
            rows[months] = price
        await run(plans.set_tariffs, sorted(rows.items()))
    if "card" in payload:
        number = _digits(payload.get("card"))
        if len(number) != 16:
            raise ApiError(400, "Karta raqami 16 ta raqam bo'lsin")
        holder = clean_text(payload.get("holder"), 60)
        if len(holder) < 3:
            raise ApiError(400, "Karta egasining ism-familiyasini yozing — AI chekda shuni tekshiradi")
        await run(db.set_setting, "card_number", " ".join(number[i:i + 4] for i in range(0, 16, 4)))
        await run(db.set_setting, "card_holder", holder)
    if "revshare" in payload:  # «💰 Begonalar puli»: platforma ulushi (%), minimal esse narxi, yechish chegarasi
        raw = payload.get("revshare") if isinstance(payload.get("revshare"), dict) else {}
        share = to_int(raw.get("platform_share"), 0, "Platforma ulushi")
        floor = to_int(raw.get("min_essay_price"), 0, "Minimal esse narxi")
        minimum = to_int(raw.get("withdraw_min"), 0, "Yechish chegarasi")
        if not 0 <= share <= 100:
            raise ApiError(400, "Platforma ulushi 0 dan 100 foizgacha bo'lsin")
        if not 0 <= floor <= 1_000_000:
            raise ApiError(400, "Minimal esse narxi 0 – 1 000 000 so'm bo'lsin")
        if not 1000 <= minimum <= 100_000_000:
            raise ApiError(400, "Yechish chegarasi kamida 1 000 so'm bo'lsin")
        for key, value in (("platform_share", share), ("min_essay_price", floor), ("withdraw_min", minimum)):
            await run(db.set_setting, key, str(value))
    if "sales_on" in payload:
        turn_on = bool(payload.get("sales_on"))
        if turn_on:
            card = await run(plans.card)
            if not await run(plans.tariffs):
                raise ApiError(400, "Avval kamida bitta tarif narxini yozing")
            if not card["number"] or not card["holder"]:
                raise ApiError(400, "Avval to'lov kartasi va egasining ismini kiriting")
        await run(db.set_setting, "sales_on", "1" if turn_on else "0")
    return ok(await sales_admin_view())


async def settings_view(user: dict) -> dict:
    """Ustozning «⚙️ Sozlamalar»i: karta, ommaviy esse tekshiruvi (havolasi bilan) va kredit narxlari."""
    username = await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""
    card, packs, is_open = await asyncio.gather(run(teacher_card, user["id"]), run(essay_packs, user["id"]),
                                                run(public_open, user["id"]))
    return {"card": card["number"], "holder": card["holder"], "public": is_open, "packs": packs, "free": PUBLIC_FREE,
            "earnings": await earnings_view(user["id"]),
            "can_public": plans.ai_unlimited(user["id"]), "student_tariffs": await run(student_tariffs, user["id"]),
            "announce_channels": await run(announce_channels, user["id"]),
            "claude": await run(claude_capacity) if plans.ai_unlimited(user["id"]) else None,
            "public_url": f"https://t.me/{username}?start={public_code(user['id'])}" if username else None}


async def teacher_settings(request: web.Request) -> web.Response:
    return ok(await settings_view(await auth(request)))


async def earnings_view(teacher_id: int) -> dict | None:
    """«💰 Daromad»: begonalar puli platformaga tushadigan o'qituvchi (yoki balansi qolgan) — balans, shu oy, yechishlar.
    None — bo'lim ko'rsatilmaydi (pul o'qituvchining o'z kartasiga tushadi)."""
    routed = await run(plans.platform_routed, teacher_id)
    summary = await run(db.earnings_summary, teacher_id, plans.month_key())
    if not routed and not summary["balance"] and not summary["pending"] and not summary["total"]:
        return None
    share, minimum, floor = await asyncio.gather(run(plans.platform_share), run(plans.withdraw_min),
                                                 run(plans.min_essay_price))
    return {**summary, "routed": routed, "teacher_percent": 100 - share, "withdraw_min": minimum, "min_price": floor,
            "can_withdraw": summary["balance"] >= minimum and not summary["pending"]}


async def withdraw(request: web.Request) -> web.Response:
    """«💸 Yechib olish»: butun balans so'raladi — adminga xabar boradi, u kartaga o'tkazib «✅ To'landi» bosadi."""
    user = await auth(request)
    card = await run(teacher_card, user["id"])
    if not card["number"]:
        raise ApiError(400, "Avval «💳 Karta»ni kiriting — pul shu kartaga o'tkaziladi")
    summary = await run(db.earnings_summary, user["id"], plans.month_key())
    if summary["pending"]:
        raise ApiError(409, "Oldingi so'rovingiz hali ko'rib chiqilmoqda — o'tkazilgach bot xabar beradi")
    minimum = await run(plans.withdraw_min)
    if summary["balance"] < minimum:
        raise ApiError(409, f"Yechib olish uchun balans kamida {plans.money(minimum)} so'm bo'lsin — "
                            f"hozir {plans.money(max(0, summary['balance']))} so'm")
    item = await run(db.request_withdrawal, user["id"], summary["balance"], card["number"], card["holder"])
    if not item:
        raise ApiError(409, "So'rov yuborilmadi — sahifani yangilab, qayta urinib ko'ring")
    if "withdraw_notice" in HOOKS:
        try:
            await HOOKS["withdraw_notice"](item["id"])
        except Exception:
            logger.warning("Yechish so'rovi adminga yuborilmadi", exc_info=True)
    return ok({"ok": True, "amount": item["amount"], "earnings": await earnings_view(user["id"])})


async def decide_withdrawal(withdrawal_id: int, paid: bool, by_id: int) -> dict:
    """Admin: pul o'tkazildi («paid») yoki rad etildi (summa balansga qaytadi) — o'qituvchiga xabar."""
    if not platform_decider(by_id):
        raise ApiError(403, "Faqat admin uchun")
    item = await run(db.get_withdrawal, withdrawal_id)
    if not item:
        raise ApiError(404, "So'rov topilmadi")
    if not await run(db.decide_withdrawal, withdrawal_id, "paid" if paid else "rejected", by_id):
        raise ApiError(409, "Bu so'rov allaqachon ko'rib chiqilgan")
    if "send_text" in HOOKS:
        last4 = _digits(item["card"])[-4:]
        text = (f"✅ <b>{plans.money(item['amount'])} so'm kartangizga o'tkazildi</b> (•••• {last4})\n"
                "Begona o'quvchilardan tushgan daromadingiz. Rahmat!" if paid else
                f"❌ <b>Yechish so'rovi rad etildi</b> · {plans.money(item['amount'])} so'm\n"
                "Summa balansingizga qaytdi. Kartangizni tekshirib, qayta so'rang yoki admin bilan bog'laning.")
        try:
            await HOOKS["send_text"](item["teacher_id"], text)
        except Exception:
            logger.warning("Yechish qarori o'qituvchiga yuborilmadi", exc_info=True)
    return {**item, "status": "paid" if paid else "rejected"}


async def teacher_settings_save(request: web.Request) -> web.Response:
    """Karta (16 raqam + egasi) va/yoki ommaviy tekshiruvni yoqish — botdagi /esse bilan bir xil qoidalar."""
    user = await auth(request)
    payload = await body(request)
    if "card" in payload:
        number = _digits(payload.get("card"))
        if len(number) != 16:
            raise ApiError(400, "Karta raqami 16 ta raqam bo'lsin")
        holder = clean_text(payload.get("holder"), 60)
        if len(holder) < 3:
            raise ApiError(400, "Karta egasining ism-familiyasini yozing — AI chekda shuni tekshiradi")
        await run(db.set_setting, f"teacher_card:{user['id']}", " ".join(number[i:i + 4] for i in range(0, 16, 4)))
        await run(db.set_setting, f"teacher_holder:{user['id']}", holder)
    if "claude_balance" in payload:  # Anthropic hisobidagi balans ($) — «nechta esse tekshira oladi» shundan
        if not plans.ai_unlimited(user["id"]):
            raise ApiError(403, "Faqat ruxsat berilgan ustozlar uchun")
        try:
            usd = round(float(str(payload.get("claude_balance")).replace(",", ".")), 2)
        except ValueError:
            raise ApiError(400, "Balansni dollarda yozing, masalan 20 yoki 12,50")
        if not 0 <= usd <= 100_000:
            raise ApiError(400, "Balans 0 – 100 000 $ oralig'ida bo'lsin")
        await run(db.set_setting, "claude_balance", json.dumps({"usd": usd, "day": plans.now().strftime("%Y-%m-%d")}))
    if "announce_channels" in payload:  # «📣 E'lon kanali»: bot mocklar haqida o'zi yozib turadi — FREE_IDS / admin
        if not plans.ai_unlimited(user["id"]):
            raise ApiError(403, "E'lon kanali — faqat ruxsat berilgan ustozlar uchun")
        channels = await resolve_channels(payload.get("announce_channels") or [])  # bot kanalda admin ekanini tekshiradi
        await run(db.set_setting, f"announce_channels:{user['id']}", json.dumps(public_channels(channels), ensure_ascii=False))
    if "student_tariffs" in payload:  # «💎 O'quvchi tariflari» — faqat FREE_IDS / admin
        if not plans.ai_unlimited(user["id"]):
            raise ApiError(403, "O'quvchi tariflari — faqat ruxsat berilgan ustozlar uchun")
        rows = payload.get("student_tariffs") or []
        if not isinstance(rows, list):
            raise ApiError(400, "Tariflar noto'g'ri")
        clean = []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("price") or "").strip():
                continue  # narxi yozilmagan qator saqlanmaydi
            days, price = to_int(row.get("days"), 0, "Muddat"), to_int(row.get("price"), 0, "Narx")
            if not 1 <= days <= 366:
                raise ApiError(400, "Tarif muddati 1–366 kun bo'lsin")
            if not 1000 <= price <= 10_000_000:
                raise ApiError(400, "Tarif narxi 1 000 – 10 000 000 so'm bo'lsin")
            if any(c[0] == days for c in clean):
                raise ApiError(400, f"{days} kunlik tarif ikki marta yozilgan")
            clean.append([days, price])
        if len(clean) > 6:
            raise ApiError(400, "Ko'pi bilan 6 ta tarif")
        if clean and not (await run(teacher_card, user["id"]))["number"]:
            raise ApiError(400, "Avval kartangizni kiriting — pul shunga tushadi")
        await run(db.set_setting, f"student_tariffs:{user['id']}", json.dumps(sorted(clean)))
    if "public" in payload:
        turn_on = bool(payload.get("public"))
        if turn_on and (not (await run(teacher_card, user["id"]))["number"] or not await run(essay_packs, user["id"])):
            raise ApiError(400, "Avval karta va kamida bitta kredit narxini kiriting")
        await run(db.set_setting, f"essay_public:{user['id']}", "1" if turn_on else "0")
    return ok(await settings_view(user))


# ---------- «💎 Tarif» (begona o'quvchi): FREE_IDS ustoz narxini qo'yadi, pul uning kartasiga; muddatida hamma mock bepul ----------

def credit_unit_price(teacher_id: int) -> int:
    """1 kredit narxi (so'm): 1 kreditlik narx, bo'lmasa paketlardagi eng arzon bitta kredit."""
    packs = essay_packs(teacher_id)
    single = next((p["price"] for p in packs if p["essays"] == 1), 0)
    if single:
        return single
    return min((math.ceil(p["price"] / p["essays"]) for p in packs), default=0)


def own_mock_code(test: dict) -> str:
    """O'z o'quvchilari uchun mock havolasi: t.me/<bot>?start=mt_<kod>_<imzo> — kirgan o'quvchi ustozga ulanadi (bepul)."""
    sig = hmac.new(f"mock:{BOT_TOKEN}".encode(), test["code"].encode(), hashlib.sha256).hexdigest()[:10]
    return f"mt_{test['code']}_{sig}"


def test_from_own_code(code: str) -> str | None:
    parts = code.split("_")
    if len(parts) != 3 or parts[0] != "mt" or not parts[1].isdigit():
        return None
    return parts[1] if hmac.compare_digest(own_mock_code({"code": parts[1]}), code) else None


CLAUDE_ESSAY_COST = 0.02  # $ — tarix bo'lmasa, bitta esse tekshiruvining taxminiy narxi


def claude_capacity() -> dict | None:
    """Claude yana nechta esse tekshira oladi: admin kiritgan balans − o'shandan beri sarf, ÷ bitta esse narxi."""
    try:
        saved = json.loads(db.get_settings().get("claude_balance") or "null")
    except ValueError:
        saved = None
    if not saved:
        return None
    spent = db.claude_spent_since(saved["day"])
    left = max(0.0, float(saved["usd"]) - spent["cost"])
    per = spent["essay_cost"] / spent["essay_calls"] if spent["essay_calls"] else CLAUDE_ESSAY_COST
    return {"usd": round(left, 2), "set_usd": float(saved["usd"]), "since": saved["day"], "per_essay": round(per, 4),
            "essays_left": int(left / per) if per > 0 else 0, "checked": spent["essay_calls"]}


def announce_channels(teacher_id: int) -> list[dict]:
    """Ustozning e'lon kanallari: [{chat_id, title, url}] — bot ularga mock e'lonlarini yuboradi."""
    try:
        raw = json.loads(db.get_settings().get(f"announce_channels:{teacher_id}") or "[]")
    except ValueError:
        raw = []
    return [c for c in raw if isinstance(c, dict) and c.get("chat_id")]


def student_tariffs(teacher_id: int) -> list[dict]:
    try:
        raw = json.loads(db.get_settings().get(f"student_tariffs:{teacher_id}") or "[]")
    except ValueError:
        raw = []
    return [{"days": int(d), "price": int(p)} for d, p in raw if int(d) > 0 and int(p) > 0]


async def pass_offers(user: dict) -> list[dict]:
    """Bosh sahifadagi «💎 Tarif» kartalari: tarif qo'ygan FREE_IDS / admin ustozlar (o'zi va o'quvchisiga emas)."""
    out = []
    for teacher_id in sorted(set(plans.FREE_IDS) | set(ADMIN_IDS)):
        if teacher_id == user["id"]:
            continue
        tariffs = await run(student_tariffs, teacher_id)
        if not tariffs or not (await run(teacher_card, teacher_id))["number"]:
            continue
        if await run(db.is_linked, teacher_id, user["id"]):
            continue
        teacher = await run(db.find_user, str(teacher_id)) or {}
        until = await run(db.pass_until, teacher_id, user["id"])
        pay = await run(db.open_pass_payment, user["id"])
        offer = {"teacher_id": teacher_id, "name": teacher.get("full_name") or "Ustoz", "tariffs": tariffs,
                 "until": None, "days_left": 0, "total_days": 0, "expired": None,
                 "status": pay["status"] if pay and pay["teacher_id"] == teacher_id else None}
        end, now = _at(until), plans.now()
        if end and end > now:  # faol: «29 kun qoldi» (oxirgi kunda — 1), halqa — qolgan / jami
            left = math.ceil((end - now).total_seconds() / 86400)
            offer.update(until=until, days_left=left,
                         total_days=max(left, await run(db.last_pass_days, teacher_id, user["id"])))
        elif end and now - end < timedelta(days=7):  # yaqinda tugagan — «yangilang»
            offer["expired"] = until
        out.append(offer)
    return out


async def pass_order(request: web.Request) -> web.Response:
    """O'quvchi tarif tanladi: bot karta va summani yozadi — chek skrinshotini botga yuboradi (AI tekshiradi)."""
    user = await auth(request)
    payload = await body(request)
    teacher_id, days = to_int(payload.get("teacher_id"), 0, "Ustoz"), to_int(payload.get("days"), 0, "Muddat")
    tariff = next((t for t in await run(student_tariffs, teacher_id) if t["days"] == days), None)
    card = await run(teacher_card, teacher_id)
    if not tariff or not card["number"] or not plans.ai_unlimited(teacher_id) or teacher_id == user["id"]:
        raise ApiError(409, "Bu tarif sotilmayapti")
    await run(db.remember_user, user["id"], user["name"], user.get("username"))
    pay = await run(db.start_pass_payment, teacher_id, user["id"], days, tariff["price"])
    if pay["status"] == "review":
        raise ApiError(409, "Oldingi chekingiz hali tekshirilmoqda — tasdiqlangach yana olasiz")
    teacher = await run(db.find_user, str(teacher_id)) or {}
    if "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](
                user["id"], f"💎 <b>Tarif · {days} kun</b> — {html.escape(teacher.get('full_name') or 'ustoz')}\n"
                f"Muddat davomida ustozning hamma mocklari bepul.\nSumma: <b>{plans.money(tariff['price'])} so'm</b>\n"
                f"Karta: <code>{html.escape(card['number'])}</code>"
                + (f"\nQabul qiluvchi: {html.escape(card['holder'])}" if card["holder"] else "")
                + "\n\nShu summani o'tkazing va <b>chek skrinshotini shu yerga yuboring</b> — AI tekshiradi, to'g'ri bo'lsa "
                  "tarif darhol yoqiladi.")
        except Exception:
            logger.warning("Tarif to'lovi ko'rsatmasi yuborilmadi", exc_info=True)
    return ok({"ok": True, "payment": pay, "bot": await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""})


async def pass_granted(pay: dict, until: str, by_ai: bool) -> None:
    if "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](pay["student_id"], f"✅ <b>Tarif yoqildi</b>" + (" (chek AI'da tasdiqlandi)" if by_ai else "")
                                     + f"\n💎 {until[:10]} gacha ustozning hamma mocklari siz uchun bepul.")
        except Exception:
            logger.warning("Tarif xabari o'quvchiga yuborilmadi", exc_info=True)


async def check_pass_receipt(payment_id: int, data: bytes, mime: str) -> dict:
    """Botga yuborilgan tarif cheki: AI to'g'ri desa — tarif darhol; aks holda ustozga, o'quvchiga «kutib turing»."""
    pay = await run(db.get_pass_payment, payment_id)
    if not pay:
        raise ApiError(404, "To'lov topilmadi")
    if pay["status"] in ("paid", "review"):
        return {"ok": True, "status": pay["status"], "reasons": []}
    digest = hashlib.sha256(data).hexdigest()
    good, reasons, txn = await verify_receipt(data, mime, pay["amount"], await run(teacher_card, pay["teacher_id"]))
    if await run(db.receipt_used, digest, txn, payment_id, "pass_payments"):
        good, reasons = False, reasons + ["bu chek oldin ishlatilgan"]
    await run(db.save_pass_receipt, payment_id, base64.b64encode(data).decode(), mime, digest, txn, "; ".join(reasons))
    until = await run(db.pay_pass_payment, payment_id, ("review",), plans.now().strftime(TIME_FMT)) if good else None
    if until:
        await pass_granted(pay, until, by_ai=True)
    elif "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](pay["student_id"], "⏳ <b>Chek tekshirilmoqda — kutib turing</b> · tarif\n"
                                                        "Avtomatik tasdiqlanmadi, ustoz ko'rib chiqadi va bot xabar beradi.")
        except Exception:
            logger.warning("Chek holati o'quvchiga yuborilmadi", exc_info=True)
    if "pass_receipt_notice" in HOOKS:
        try:
            await HOOKS["pass_receipt_notice"](payment_id, bool(until), reasons, data)
        except Exception:
            logger.warning("Tarif cheki ustozga yuborilmadi", exc_info=True)
    return {"ok": True, "status": "paid" if until else "review", "reasons": reasons}


async def decide_pass_payment(payment_id: int, approve: bool, by_id: int) -> dict:
    pay = await run(db.get_pass_payment, payment_id)
    if not pay or (pay["teacher_id"] != by_id and by_id not in ADMIN_IDS):
        raise ApiError(404, "To'lov topilmadi")
    if approve:
        until = await run(db.pay_pass_payment, payment_id, ("open", "review", "rejected"), plans.now().strftime(TIME_FMT))
        if not until:
            raise ApiError(409, "Bu to'lov allaqachon ko'rib chiqilgan")
        await pass_granted(pay, until, by_ai=False)
    else:
        if not await run(db.reject_pass_payment, payment_id, ("open", "review", "paid")):
            raise ApiError(409, "Bu to'lov allaqachon ko'rib chiqilgan")
        if "send_text" in HOOKS:
            try:
                await HOOKS["send_text"](pay["student_id"], "❌ <b>Tarif to'lovi tasdiqlanmadi</b>\nChek mos kelmadi (summa, sana yoki "
                                         "karta). To'g'ri chekni qayta yuboring yoki ustozga yozing.")
            except Exception:
                logger.warning("Tarif qarori o'quvchiga yuborilmadi", exc_info=True)
    return {"ok": True, "status": "paid" if approve else "rejected"}


async def wallet_payments_list(request: web.Request) -> web.Response:
    user = await auth(request)
    return ok({"payments": await run(db.wallet_payments_for_teacher, user["id"])})


async def wallet_payment_receipt(request: web.Request) -> web.Response:
    user = await auth(request)
    pay = await run(db.get_wallet_payment, int(request.match_info["payment_id"]))
    if not pay or pay["teacher_id"] != user["id"] or not pay["receipt"]:
        raise ApiError(404, "Chek topilmadi")
    return ok({"image": _image_data_url(pay["receipt"], pay["mime"])})


async def wallet_payment_decide(request: web.Request) -> web.Response:
    user = await auth(request)
    action = (await body(request)).get("action")
    if action not in ("approve", "reject"):
        raise ApiError(400, "Amal noto'g'ri")
    return ok(await decide_wallet_payment(int(request.match_info["payment_id"]), action == "approve", user["id"]))


def clean_answers(test: dict, raw: dict) -> dict:
    answers = {}
    for item in test["items"]:
        value = raw.get(item["label"])
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if item["kind"] == "choice":
            value = value.upper()
            if value not in LETTERS[: item["options"]]:
                raise ApiError(400, f"{item['label']}-savol javobi noto'g'ri")
        answers[item["label"]] = value[:200]
    return answers


async def submit(request: web.Request) -> web.Response:
    user = await auth(request)
    payload = await body(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test:
        raise ApiError(404, "Test topilmadi")
    if test["closed"]:
        raise ApiError(409, "Natijalar e'lon qilingan — javob qabul qilinmaydi")
    if (test.get("mode") or "online") == "offline":
        raise ApiError(409, "Bu test qog'ozda o'tkaziladi")
    await test_gate(test, user)
    phase = mock_phase(test)
    if phase and test["owner_id"] != user["id"]:  # «Bugungi mock»: faqat vaqt oralig'ida va (pullik bo'lsa) to'laganlar
        if phase in ("soon", "closed"):
            raise ApiError(409, "Mock hali ochilmagan" if phase == "soon" else "Mock yopilgan — javob qabul qilinmaydi")
        if phase == "late" and not await mock_entered(test, user["id"]):
            raise ApiError(409, "Kirish vaqti tugagan")
        if await mock_choice_info(test, user):
            raise ApiError(402, "Mockni boshlashdan oldin esseni kim tekshirishini tanlang (pullik bo'lsa — to'lang)")
    keys = await run(db.get_keys, test["id"])
    variant = payload.get("variant") or next(iter(keys))
    if variant not in keys:
        raise ApiError(400, "Variant noto'g'ri")
    name = " ".join(str(payload.get("name", "")).split())[:60]
    if len(name) < 3:
        raise ApiError(400, "Ism va familiyani to'liq yozing")
    answers = clean_answers(test, payload.get("answers") or {})
    essay = str(payload.get("essay") or "").strip()
    if test["fmt"] != "milliy":
        essay = ""
    if essay and len(essay) < ESSAY_MIN_CHARS:
        raise ApiError(400, f"Esse juda qisqa ({len(essay)} belgi) — to'liq yozing yoki bo'sh qoldiring")
    if essay:
        pay = await essay_pay_info(test, user)
        if pay:  # begona o'quvchi: esse — to'lovdan keyin (test o'zi bepul)
            raise ApiError(402, "Esseni yuborishdan oldin esse tekshiruvini to'lang — yoki essesiz yuboring", essay_pay=pay)
    await run(db.remember_user, user["id"], user["name"], user["username"])
    await run(db.set_last_name_used, user["id"], name)
    attempt = await HOOKS["record_attempt"](test, user["id"], name, variant, answers, essay[:12000] or None, [], 0.0)
    if attempt is None:
        raise ApiError(409, "Siz bu testga javob yuborgansiz")
    if essay and "wake_ai" in HOOKS:
        HOOKS["wake_ai"]()  # o'quvchi essesi — navbat kutmasdan tekshiriladi
    return ok({"ok": True, "attempt_id": attempt["id"], "essay": bool(essay)})


async def my_essay(request: web.Request) -> web.Response:
    """O'quvchi o'z essesini tekshiradi: holat (tekshirilmoqda / tayyor), ball, mezonlar va izoh — e'londan oldin ham."""
    user = await auth(request)
    attempt = await run(db.get_attempt, int(request.match_info["attempt_id"]))
    if not attempt or attempt["tg_id"] != user["id"]:
        raise ApiError(404, "Topilmadi")
    essay = await run(db.get_essay, attempt["id"])
    if not essay or essay["status"] == "none":
        return ok({"status": "none"})
    ai = essay["ai_result"] or {}
    score = attempt["essay"] if attempt["essay"] is not None else essay["ai_score"]
    return ok({
        "status": essay["status"],  # pending — AI tekshirmoqda, graded — tayyor, manual — o'qituvchi, failed — AI o'qiy olmadi
        "score": score, "score_75": essay_to_75(score), "max": ESSAY_MAX, "scale": ESSAY_SCALE,
        "teacher_changed": attempt["essay"] is not None and essay["ai_score"] is not None and attempt["essay"] != essay["ai_score"],
        "criteria": ai.get("criteria") if essay["status"] == "graded" else None,
        "comment": ai.get("comment") if essay["status"] == "graded" else None,
        "titles": essay_ai.RUBRIC_TITLES,
    })


async def ping(request: web.Request) -> web.Response:
    """O'quvchi test yechayotganda har bir necha soniyada: nechta savol belgilangan — o'qituvchining jonli ekrani uchun."""
    user = await auth(request)
    payload = await body(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test or test["closed"] or (test.get("mode") or "online") == "offline":
        return ok({"ok": False})
    try:
        total = max(0, min(to_int(payload.get("total")), 500))
        answered = max(0, min(to_int(payload.get("answered")), total))
    except (TypeError, ValueError):
        raise ApiError(400, "Noto'g'ri ma'lumot")
    name = " ".join(str(payload.get("name") or "").split())[:60]
    await run(db.live_ping, test["id"], user["id"], name if len(name) >= 3 else user["name"] or "Ismsiz", answered, total)
    return ok({"ok": True})


async def live(request: web.Request) -> web.Response:
    """Jonli kuzatuv: kim yechmoqda, kim chiqib ketdi, kim topshirdi."""
    user = await auth(request)
    test = await own_test(request, user)
    rows, attempts = await asyncio.gather(run(db.live_list, test["id"]), run(db.list_attempts, test["id"]))
    submitted = {a["tg_id"]: a for a in attempts}
    now = int(time.time())
    people = []
    for row in rows:
        attempt = submitted.pop(row["tg_id"], None)
        people.append({"name": attempt["student_name"] if attempt else row["name"], "answered": row["answered"],
                       "total": row["total"], "started": now - row["started_at"], "seen": now - row["seen_at"],
                       "status": "done" if attempt else "active" if now - row["seen_at"] < LIVE_IDLE else "idle"})
    for attempt in submitted.values():  # jonli holat yozilmasdan topshirganlar (bot orqali yoki eski ilova)
        people.append({"name": attempt["student_name"], "answered": None, "total": None, "started": None,
                       "seen": None, "status": "done"})
    return ok({"people": people, "closed": bool(test["closed"])})


_RASCH_CACHE: dict = {}


async def cached_rasch(test: dict, attempts: list[dict]) -> dict:
    """Rasch — natijalar o'zgarmaguncha bir marta hisoblanadi: e'londan keyin yuzlab o'quvchi kartasini
    bir vaqtda ochganda har biri uchun qayta hisoblanmaydi."""
    state = json.dumps([(a["id"], a["wrong"], a["essay"], a.get("points")) for a in attempts], sort_keys=True)
    key = (test["id"], hash(state))
    if key not in _RASCH_CACHE:
        if len(_RASCH_CACHE) > 200:
            _RASCH_CACHE.clear()
        _RASCH_CACHE[key] = await run(rasch_breakdown, test, attempts)
    return _RASCH_CACHE[key]


def key_display(test: dict, key: dict) -> dict:
    out = {}
    for item in test["items"]:
        value = key.get(item["label"], "")
        out[item["label"]] = " / ".join(split_options(value)) if item["kind"] == "text" else value
    return out


async def result(request: web.Request) -> web.Response:
    user = await auth(request)
    attempt = await run(db.get_attempt, int(request.match_info["attempt_id"]))
    test = attempt and await run(db.get_test, attempt["test_id"])
    if not test:
        raise ApiError(404, "Natija topilmadi")
    is_teacher = test["owner_id"] == user["id"]
    if not is_teacher and attempt["tg_id"] != user["id"]:
        raise ApiError(404, "Natija topilmadi")
    if not is_teacher and not test["closed"]:
        raise ApiError(403, "Natijalar hali e'lon qilinmagan")
    keys = await run(db.get_keys, test["id"])
    rasch = None
    if test["fmt"] == "milliy":
        all_attempts = await run(db.list_attempts, test["id"])
        parts = (await cached_rasch(test, all_attempts)).get(attempt["id"])
        if parts:  # umumiy = (test qismi + esse 75 ballik) / 2 — DTM tartibi
            rasch = {"score": parts["score"], "grade": parts["grade"], "test": parts["test"], "essay": parts["essay"]}
    essay = await run(db.get_essay, attempt["id"])
    essay_view = None
    if essay and essay["status"] != "none":
        confirmed = attempt["essay"] is not None
        essay_view = {
            "status": essay["status"], "confirmed": confirmed,
            "criteria": (essay["ai_result"] or {}).get("criteria") if (confirmed or is_teacher) else None,
            "comment": (essay["ai_result"] or {}).get("comment") if (confirmed or is_teacher) else None,
            "titles": essay_ai.RUBRIC_TITLES,
        }
    return ok({
        "test": {"title": test["title"], "code": test["code"], "fmt": test["fmt"], "theme": test.get("theme") or "classic",
                 "items": public_items(test), "format": format_title(test["fmt"], test["count"]),
                 "variants": len(keys)},
        "attempt": {
            "id": attempt["id"], "name": attempt["student_name"], "variant": attempt["variant"],
            "test_correct": attempt["test_correct"], "written_correct": attempt["written_correct"],
            "choices_total": len(choice_items(test["items"])), "texts_total": len(text_items(test["items"])),
            "auto_total": attempt["auto_total"], "points": attempt.get("points") or 0, "essay": attempt["essay"],
            "essay_75": essay_to_75(attempt["essay"]),
            "total": total_points(attempt), "max_points": max_points(test["items"], test["fmt"]),
            "wrong": attempt["wrong"], "answers": attempt["answers"], "created_at": attempt["created_at"],
            "percent": percent(attempt["test_correct"] + attempt["written_correct"], attempt["auto_total"]),
        },
        "rasch": rasch,
        "key": key_display(test, keys.get(attempt["variant"], {})),
        "essay": essay_view,
        "essay_max": ESSAY_MAX, "essay_scale": ESSAY_SCALE,
        "announced": bool(test["closed"]),
    })


# ---------- O'qituvchi ----------

def validate_key(items: list[dict], raw: dict) -> dict:
    key = {}
    for item in items:
        value = raw.get(item["label"])
        if not isinstance(value, str) or not value.strip():
            raise ApiError(400, f"{item['label']}-savol kaliti yozilmagan")
        value = value.strip()
        if item["kind"] == "choice":
            value = value.upper()
            if value not in LETTERS[: item["options"]]:
                raise ApiError(400, f"{item['label']}-savol kaliti noto'g'ri")
        else:
            options = split_options(value)
            if not options or len(options) > MAX_TEXT_OPTIONS:
                raise ApiError(400, f"{item['label']}: 1–{MAX_TEXT_OPTIONS} ta to'g'ri javob yozing")
            value = "|".join(option[:100] for option in options)
        key[item["label"]] = value
    return key


async def keys_get(request: web.Request) -> web.Response:
    """Kalitni tahrirlash sahifasi: savollar va hozirgi kalit (variantlar bo'yicha)."""
    user = await auth(request)
    test = await own_test(request, user)
    keys = await run(db.get_keys, test["id"])
    attempts = await run(db.list_attempts, test["id"])
    items = [{"label": i["label"], "kind": i["kind"], "options": i.get("options", 4)} for i in test["items"]]
    return ok({"test": {"id": test["id"], "title": test["title"], "code": test["code"]}, "items": items,
               "keys": keys, "attempts": len(attempts)})


def regrade_attempts(test: dict, keys: dict) -> tuple[int, int]:
    """Hamma natija yangi kalit bilan qayta tekshiriladi. (jami, bali o'zgargan) qaytaradi.
    Rasch bali saqlanmaydi — har safar to'g'ri/xato javoblardan qayta hisoblanadi, demak o'zi yangilanadi."""
    attempts = db.list_attempts(test["id"])
    changed = 0
    for attempt in attempts:
        key = keys.get(attempt["variant"]) or next(iter(keys.values()))
        fresh = grade(test["items"], key, attempt["answers"])
        if fresh["wrong"] != attempt["wrong"] or fresh["points"] != (attempt.get("points") or 0):
            changed += 1
        db.update_attempt_result(attempt["id"], attempt["answers"], fresh)
    return len(attempts), changed


async def keys_save(request: web.Request) -> web.Response:
    """Kalit tuzatildi: saqlanadi, hamma natija qayta tekshiriladi, test egasiga botda xabar boradi."""
    user = await auth(request)
    test = await own_test(request, user)
    raw = (await body(request)).get("keys")
    old = await run(db.get_keys, test["id"])
    if not isinstance(raw, dict):
        raise ApiError(400, "Kalit yuborilmadi")
    keys = {}
    for name in old:
        if not isinstance(raw.get(name), dict):
            raise ApiError(400, f"{name} variant kaliti yuborilmadi")
        keys[name] = validate_key(test["items"], raw[name])
    edited = [item["label"] for item in test["items"] if any(keys[v][item["label"]] != old[v].get(item["label"]) for v in keys)]
    if not edited:
        return ok({"ok": True, "edited": [], "total": 0, "changed": 0})
    await run(db.set_keys, test["id"], keys)
    total, changed = await run(regrade_attempts, test, keys)
    if "send_text" in HOOKS:
        shown = ", ".join(edited[:10]) + ("…" if len(edited) > 10 else "")
        text = (f"🔑 <b>{html.escape(test['title'])}</b> (kod {test['code']}): kalit tuzatildi — {shown}-savol.\n"
                f"♻️ {total} ta natija qayta tekshirildi" + (f", {changed} tasining bali o'zgardi." if total else "."))
        if total and any(item["kind"] == "text" and item["label"] in edited for item in test["items"]):
            text += "\n✍️ Yozma javob kaliti o'zgardi — «✍️ Yozmalarni tekshirish»da belgilaganlaringizni ko'zdan kechiring."
        in_background(HOOKS["send_text"](user["id"], text), "Kalit o'zgargani haqida xabar")
    return ok({"ok": True, "edited": edited, "total": total, "changed": changed})


async def require_plan(user: dict) -> None:
    """Yangi test faqat faol obuna (yoki bepul sinov) bilan. Birinchi marta — sinov o'zi boshlanadi."""
    status = await run(plans.ensure_active, user["id"])
    if status.get("closed"):
        raise ApiError(403, "Test yaratish hozircha faqat ruxsat berilgan o'qituvchilarga"
                       + (f" — ruxsat uchun @{ADMIN_CONTACT} ga ID'ingizni yuboring" if ADMIN_CONTACT else ""),
                       closed=True, user_id=user["id"])
    if not status["active"]:
        raise ApiError(402, "Test yaratish uchun obuna kerak — «💎 Obuna» tugmasidan sotib oling", plan=status)


async def plan_order(request: web.Request) -> web.Response:
    """«Sotib olish» bosildi (obuna muddati yoki «📦 Esse paketi»): to'lov so'rovi ochiladi,
    foydalanuvchiga karta va summa ko'rsatiladi."""
    user = await auth(request)
    payload = await body(request)
    period = str(payload.get("period") or "")
    count = max(1, min(20, to_int(payload.get("count"), 1, "Paketlar soni")))
    essays = None
    if await run(plans.credit_mode, user["id"]):
        # Obuna yopiq (yoki /ruxsat bilan bepul o'qituvchi): AI esse kreditini o'zi sotib oladi (karta → chek)
        if period != plans.PACK:
            raise ApiError(403, "Sizga test yaratish bepul — obuna kerak emas" if await run(plans.sales_on)
                           else "Obuna hozircha yopiq")
        if not await run(plans.is_free, user["id"]):
            raise ApiError(403, "Kredit test yaratishga ruxsati bor o'qituvchilar uchun")
        if plans.ai_unlimited(user["id"]):
            raise ApiError(400, "Sizda AI cheksiz — kredit kerak emas")
        offer = await run(plans.pack)
        if not offer["price"]:
            raise ApiError(400, "Kredit narxi hali belgilanmagan — admin bilan bog'laning")
        amount, essays = offer["price"] * count, offer["essays"] * count
    elif await run(plans.is_free, user["id"]):
        raise ApiError(400, "Sizda hamma narsa bepul — obuna kerak emas")
    elif period != plans.PACK and period not in {o["key"] for o in await run(plans.offers)}:
        raise ApiError(400, "Bunday tarif yo'q — ilovani yangilab, qaytadan tanlang")
    elif period == plans.PACK:
        if not (await run(plans.status, user["id"]))["active"]:
            raise ApiError(400, "Esse paketi faol obunaga qo'shiladi — avval obunani yangilang")
        offer = await run(plans.pack)
        amount, essays = offer["price"] * count, offer["essays"] * count
    else:
        amount = await run(plans.price_of, period)
    card = await run(plans.card)
    if not card["number"]:
        raise ApiError(400, "To'lov kartasi hali kiritilmagan — admin bilan bog'laning")
    await run(db.remember_user, user["id"], user["name"], user["username"])
    payment = await run(db.create_payment, user["id"], period, amount, essays)
    username = await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""
    if "send_text" in HOOKS:
        done = f"{essays} ta esse qo'shiladi" if essays else "obunangiz darhol yoqiladi"
        try:
            await HOOKS["send_text"](
                user["id"],
                f"💳 <b>To'lov</b> · {plans.payment_title(payment)} — <b>{plans.money(amount)} so'm</b>\n"
                f"Karta: <code>{html.escape(card['number'])}</code>"
                + (f"\nQabul qiluvchi: {html.escape(card['holder'])}" if card["holder"] else "")
                + "\n\nShu summani kartaga o'tkazing va <b>chek rasmini shu yerga yuboring</b> — "
                  f"admin tasdiqlagach {done}.")
        except Exception:
            logger.warning("To'lov ko'rsatmasi yuborilmadi", exc_info=True)
    return ok({"payment": {"id": payment["id"], "period": period, "amount": amount, "essays": essays,
                           "status": payment["status"]},
               "card": card, "bot": username})


async def written_ai(owner_id: int) -> bool:
    """Yozma javoblarni (36–44) AI o'qiydimi: faqat FREE_IDS va adminlarga. Qolganlar rasmga qarab
    o'zi belgilaydi — «✍️ Yozmalarni tekshirish» (har savol bo'yicha, kalit bilan yonma-yon)."""
    return plans.ai_unlimited(owner_id) and essay_ai.enabled() and await ai_allowed(owner_id, "written")


PAID_SHEETS_PER_CREDIT = 10  # pullik tekshiruv: shuncha titulning yozma javoblari = 1 kredit


def paid_check(test: dict) -> bool:
    """Oflayn testning yozma javoblari pullik AI (Claude) bilan tekshiriladimi."""
    return test.get("check_mode") == "paid" and essay_ai.paid_enabled()


async def paid_written_allowed(owner_id: int) -> bool:
    """Pullik tekshiruv: har PAID_SHEETS_PER_CREDIT ta titulga ustozning 1 krediti (birinchi titulda yechiladi).
    Kredit tugagan bo'lsa — False (titul «qo'lda» bo'ladi) va ustozga bir marta xabar."""
    if plans.ai_unlimited(owner_id):
        return True
    key = f"paid_sheets:{owner_id}"
    used = int((await run(db.get_settings)).get(key) or 0)
    if used % PAID_SHEETS_PER_CREDIT == 0 and not await ai_allowed(owner_id, "essays"):
        return False
    await run(db.set_setting, key, used + 1)
    return True


CREDIT_WARN = 5  # shuncha kredit qolganda o'qituvchiga ogohlantirish


async def ai_allowed(owner_id: int, kind: str, amount: int = 1) -> bool:
    """AI ishlatish (oylik limit bo'yicha). Limit/obuna tugagan bo'lsa — o'qituvchiga oyiga bir marta xabar."""
    if await run(plans.use_ai, owner_id, kind, amount):
        if kind == "essays" and await run(plans.credit_mode, owner_id) and not plans.ai_unlimited(owner_id) and "send_text" in HOOKS:
            left = await run(db.get_credits, owner_id)
            if left == CREDIT_WARN:  # kredit oz qoldi — bir marta ogohlantirish
                try:
                    await HOOKS["send_text"](owner_id, f"🤖 <b>AI esse krediti oz qoldi: {left} ta</b>\nYana {left} ta o'quvchi "
                                             "essesini tekshirtira olasiz. To'ldirish: ilovada «🤖 AI esse» → «Kredit sotib olish».")
                except Exception:
                    logger.warning("Kredit ogohlantirishi yuborilmadi", exc_info=True)
        return True
    if kind == "essays" and await run(plans.credit_mode, owner_id):
        # Obuna yopiq — kredit tugadi: o'qituvchiga bir marta (kredit to'ldirilgach yana) xabar
        await run(db.consume_usage, owner_id, plans.month_key(), kind, 0, None)  # oy qatori bo'lsin (xabar belgisi)
        if await run(db.take_limit_notice, owner_id, plans.month_key()) and "send_text" in HOOKS:
            contact = f" — @{ADMIN_CONTACT}" if ADMIN_CONTACT else ""
            try:
                await HOOKS["send_text"](
                    owner_id, "⚠️ <b>AI esse krediti tugadi</b>\nO'quvchilaringizning yangi esselarini AI tekshirmaydi — "
                    "«📝 Esselar»da o'zingiz baholaysiz (75 ballik).\n\nKreditni to'ldirish uchun admin bilan bog'laning"
                    f"{contact}. Telegram ID'ingiz: <code>{owner_id}</code>")
            except Exception:
                logger.warning("Kredit haqida xabar yuborilmadi", exc_info=True)
        return False
    if await run(db.take_limit_notice, owner_id, plans.month_key()) and "send_text" in HOOKS:
        status = await run(plans.status, owner_id)
        if not status["active"]:
            text = ("⚠️ <b>AI esse tekshiruvi to'xtadi</b>: obunangiz muddati tugagan.\n"
                    "Yangi esselarni o'zingiz baholaysiz («📝 Esselar»). Uzaytirish — ilovadagi «💎 Obuna».")
        else:
            offer = status["pack"]
            text = (f"⚠️ <b>Bu oygi AI esse limiti tugadi</b> ({status['limits']['essays']} ta).\n"
                    "Yangi esselarni o'zingiz baholaysiz («📝 Esselar»).\n\n"
                    f"AI davom etsin desangiz: ilovada «💎 Obuna» → <b>📦 +{offer['essays']} esse</b> "
                    f"({plans.money(offer['price'])} so'm).")
        try:
            await HOOKS["send_text"](owner_id, text)
        except Exception:
            logger.warning("Limit haqida xabar yuborilmadi", exc_info=True)
    return False


async def create_test(request: web.Request) -> web.Response:
    user = await auth(request)
    payload = await body(request)
    await require_plan(user)
    title = " ".join(str(payload.get("title", "")).split())[:80]
    if len(title) < 2:
        raise ApiError(400, "Test nomini yozing")
    fmt = payload.get("fmt")
    if fmt not in ("milliy", "oddiy"):
        raise ApiError(400, "Test turini tanlang")
    count = 45 if fmt == "milliy" else to_int(payload.get("count"), 0, "Savollar soni")
    if fmt == "oddiy" and not 5 <= count <= 100:
        raise ApiError(400, "Savollar soni 5–100 bo'lishi kerak")
    items = build_items(fmt, count)
    raw_keys = payload.get("keys") or {}
    if not isinstance(raw_keys, dict) or not raw_keys:
        raise ApiError(400, "Kalitni kiriting")
    if not raw_keys.get("A"):
        raise ApiError(400, "Kalitni kiriting")
    keys = {"A": validate_key(items, raw_keys["A"])}  # bitta kalit; eski testlardagi B–D variantlar ishlashda davom etadi
    theme = payload.get("theme") if payload.get("theme") in THEMES else "classic"
    topic = str(payload.get("essay_topic") or "").strip()[:2000] or None
    mode = "offline" if payload.get("mode") == "offline" else "online"
    # Oflayn (qog'ozdagi) testda majburiy obuna yo'q — o'quvchi testni ilovada ochmaydi
    channels = await resolve_channels(payload.get("channels") or []) if mode == "online" else []
    price = to_int(payload.get("essay_price") or 0, 0, "Esse narxi") if fmt == "milliy" and mode == "online" else 0
    card = " ".join(str(payload.get("card") or "").split())[:30]
    if price:
        if not 1000 <= price <= 1_000_000:
            raise ApiError(400, "Esse tekshiruvi narxini so'mda yozing (1 000 – 1 000 000)")
        floor = await run(plans.min_essay_price) if await run(plans.platform_routed, user["id"]) else 0
        if price < floor:
            raise ApiError(400, f"Begonalar uchun esse narxi kamida {plans.money(floor)} so'm bo'lsin")
        if len(_digits(card)) < 12:
            raise ApiError(400, "Begonalar to'laydigan kartangiz raqamini yozing")
    check_mode = "paid" if payload.get("check_mode") == "paid" and mode == "offline" else "free"
    if check_mode == "paid" and not essay_ai.paid_enabled():
        raise ApiError(400, "Pullik AI tekshiruvi hali ulanmagan — admin bilan bog'laning")
    test = await run(db.create_test, user["id"], title, fmt, count, items, keys, topic if fmt == "milliy" else None)
    await run(db.set_theme, test["id"], theme)
    if check_mode == "paid":
        await run(db.set_check_mode, test["id"], "paid")
    if price:
        await run(db.set_test_price, test["id"], price)
        await run(db.set_setting, f"teacher_card:{user['id']}", card)
        await run(db.set_setting, f"teacher_holder:{user['id']}", " ".join(str(payload.get("holder") or "").split())[:60])
    if channels:
        await run(db.set_test_channels, test["id"], channels)
    await run(db.set_mode, test["id"], mode)
    if fmt == "milliy":  # imtihon varaqasi (namunadagidek): 1–35, 36–44 va esse varaqlari; onlayn mockning tituli ham
        await run(db.set_sheet_style, test["id"], "official")
    test = await run(db.get_test, test["id"])
    sent, pdf_error = await send_created_pdfs(user["id"], test)
    return ok({"ok": True, "id": test["id"], "code": test["code"], "mode": mode, "sent": sent, "pdf_error": pdf_error})


async def send_created_pdfs(chat_id: int, test: dict) -> tuple[list[str], str | None]:
    """Yangi test: kalit PDF'i, oflayn testga yana bo'sh titul PDF'i — bot chatiga. Yuborib bo'lmasa ham test
    saqlanib qoladi (xato qaytarilsa, o'qituvchi qayta bosib, ikkinchi testni yaratib qo'yardi)."""
    # Titul (javoblar varaqasi) — har qanday testga: o'qituvchi uni chop etib o'quvchilarga tarqatadi
    jobs = [("key", lambda: HOOKS["send_pdf"](chat_id, test, "key")),
            ("sheets", lambda: HOOKS["send_sheets"](chat_id, test, [], 1))]
    sent, error = [], None
    for kind, job in jobs:
        try:
            await job()
            sent.append(kind)
            if kind == "sheets":
                await run(db.mark_sheets_sent, test["id"])
        except Exception as exc:
            logger.exception("Yangi test PDF'i (%s) yuborilmadi: test %s", kind, test["id"])
            error = send_error_text(exc)
    return sent, error


# ---------- Botga yuborish ilovani kutdirmasin ----------
# Xabarlar — orqada (ilova darhol javob oladi). Fayllar — DELIVER_WAIT soniyagacha kutiladi: ulgursa «yuborildi»,
# ulgurmasa «yuborilmoqda» va orqada davom etadi (Render → Telegram yuklash sekin bo'lsa ham ilova qotmaydi).
DELIVER_WAIT = 4.0
_BACKGROUND: set = set()


def in_background(coro, what: str) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)

    def done(t: asyncio.Task) -> None:
        _BACKGROUND.discard(t)
        if not t.cancelled() and t.exception():
            logger.warning("%s: botga yuborilmadi — %s", what, t.exception())
    task.add_done_callback(done)
    return task


async def deliver(coro, what: str) -> bool:
    """True — yuborildi; False — hali yuborilmoqda (orqada). Xato bo'lsa (vaqtida) — istisno ko'tariladi."""
    task = in_background(coro, what)
    done, _ = await asyncio.wait({task}, timeout=DELIVER_WAIT)
    if task in done:
        task.result()
        return True
    return False


def send_error_text(exc: Exception) -> str:
    """Bot chatga fayl yubora olmadi — o'qituvchiga tushunarli sabab."""
    text = str(exc).lower()
    if "forbidden" in text or "chat not found" in text or "blocked" in text:
        return "Bot sizga xabar yubora olmadi — botni oching va /start bosing, so'ng «Fayllar»dan qayta oling"
    return "PDF yuborilmadi — test sahifasidagi «Fayllar»dan qayta oling"


async def copy_test(request: web.Request) -> web.Response:
    """Shu testning nusxasi — yangi kod bilan, kalitlar o'sha: boshqa guruhga qayta kiritmasdan berish uchun."""
    user = await auth(request)
    test = await own_test(request, user)
    await require_plan(user)
    title = " ".join(str((await body(request)).get("title") or "").split())[:80] or f"{test['title']} (nusxa)"[:80]
    keys = await run(db.get_keys, test["id"])
    copy = await run(db.create_test, user["id"], title, test["fmt"], test["count"], test["items"], keys, test.get("essay_topic"))
    await run(db.set_theme, copy["id"], test.get("theme") or "classic")
    if test.get("channels") and (test.get("mode") or "online") == "online":
        await run(db.set_test_channels, copy["id"], test["channels"])
    await run(db.set_mode, copy["id"], test.get("mode") or "online")
    if test.get("sheet_style"):  # titul ko'rinishi, tekshiruv turi va begonalar uchun esse narxi ham nusxaga
        await run(db.set_sheet_style, copy["id"], test["sheet_style"])
    if test.get("check_mode") == "paid":
        await run(db.set_check_mode, copy["id"], "paid")
    if test.get("price"):
        await run(db.set_test_price, copy["id"], int(test["price"]))
    return ok({"ok": True, "id": copy["id"], "code": copy["code"]})


async def teacher_test(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    milliy = test["fmt"] == "milliy"
    attempts, keys, essays, written_jobs, students, pupils = await asyncio.gather(  # parallel — sahifa tezroq
        run(db.list_attempts, test["id"]), run(db.get_keys, test["id"]),
        run(db.essays_for_test, test["id"]) if milliy else asyncio.sleep(0, {}),
        run(db.written_jobs_for_test, test["id"]), run(db.list_students, test["id"]), pupils_of(user["id"]))
    rasch = await run(rasch_results, test, attempts) if milliy and attempts else {}
    rows = []
    for a in attempts:
        essay = essays.get(a["id"])
        score, grade_name = rasch.get(a["id"], (None, None))
        rows.append({
            "id": a["id"], "name": a["student_name"], "variant": a["variant"],
            "test_correct": a["test_correct"], "written_correct": a["written_correct"], "auto_total": a["auto_total"],
            "points": a.get("points") or 0, "essay": a["essay"], "essay_75": essay_to_75(a["essay"]),
            "total": total_points(a),
            "percent": percent(a["test_correct"] + a["written_correct"], a["auto_total"]),
            "rasch": score, "grade": grade_name, "wrong": a["wrong"], "created_at": a["created_at"],
            "essay_pending": essay_pending(test, a),  # qog'ozdagi esse bahosi kiritilmagan — natija yopiq
            "essay_status": essay["status"] if essay else None,
            "ai_score": essay["ai_score"] if essay else None,
            "written_status": written_jobs.get(a["id"]),  # skanerlangan titul: pending | done | failed
        })
    rows.sort(key=lambda r: (-(r["rasch"] or 0), -r["total"]))
    # Savol tahlili: to'g'ri ulushi va (test savollarida) har variantni nechta o'quvchi tanlagani — kalitdagi xatoni topish uchun
    main_key = keys.get("A") or (next(iter(keys.values())) if keys else {})
    analysis = []
    for item in test["items"] if attempts else []:
        row = {"label": item["label"], "kind": item["kind"],
               "share": round(sum(1 for a in attempts if item["label"] not in a["wrong"]) * 100 / len(attempts))}
        if item["kind"] == "choice":
            dist = {letter: 0 for letter in LETTERS[: item.get("options", 4)]}
            blank = 0
            for a in attempts:
                chosen = str(a["answers"].get(item["label"]) or "").upper()
                if chosen in dist:
                    dist[chosen] += 1
                else:
                    blank += 1
            key = str(main_key.get(item["label"]) or "").upper()
            wrong = {k: v for k, v in dist.items() if k not in key}
            top = max(wrong, key=wrong.get) if wrong and max(wrong.values()) else None
            row.update(dist=dist, blank=blank, key=key, top_wrong=top,
                       suspicious=bool(top and dist[top] > max([dist.get(k, 0) for k in key] or [0])))
        analysis.append(row)
    grades = {}
    for _, grade_name in rasch.values():
        grades[grade_name] = grades.get(grade_name, 0) + 1
    written_review_info = await written_review_summary(test, attempts, keys)
    offline_milliy = test["fmt"] == "milliy" and test.get("mode") == "offline"
    return ok({
        "written_review": written_review_info,
        "test": {"id": test["id"], "code": test["code"], "title": test["title"], "fmt": test["fmt"],
                 "format": format_title(test["fmt"], test["count"]), "closed": bool(test["closed"]),
                 "theme": test.get("theme") or "classic", "variants": list(keys), "created_at": test["created_at"],
                 "essay_topic": test.get("essay_topic"), "max_points": max_points(test["items"], test["fmt"]),
                 "channels": public_channels(test["channels"]), "mode": test.get("mode") or "online",
                 "sheets_sent": bool(test.get("sheets_sent")),
                 "sheet_style": sheet_styles.style_of(test)},
        "sheet_styles": sheet_styles.STYLES,
        "students": [student_view(s, pupils) for s in students],
        "pupils": list(pupils.values()) if test.get("mode") == "offline" else [],
        "attempts": rows, "analysis": analysis, "grades": grades,
        "essay_cost": round(sum(e["cost"] for e in essays.values()), 4),
        "ai": essay_ai.essay_enabled(), "ai_provider": essay_ai.essay_provider_name(),
        "waiting_essays": await run(db.waiting_offline_essays, test["id"]) if offline_milliy and not essay_on_paper(test) else 0,
        "essay_on_paper": essay_on_paper(test),
        "essays_missing": sum(1 for a in attempts if essay_pending(test, a)),
        "ai_credits": None if plans.ai_unlimited(user["id"]) else await run(db.get_credits, user["id"]),
        # «Bugungi mock»: jadval, savollar fayllari, qatnashmoqchilar; «Hammaga ko'rsatish» tanlovi kimga mumkin
        "schedule": schedule_view(test), "files": await files_view(test),
        "signups": len(await run(db.mock_signups, test["id"])), "can_public": plans.ai_unlimited(user["id"]),
        "own_link": (f"https://t.me/{await HOOKS['bot_username']()}?start={own_mock_code(test)}"
                     if test.get("opens_at") and "bot_username" in HOOKS else None),
        "offline_essays": [offline_essay_view(e) for e in await run(db.list_offline_essays, test["id"])]
        if offline_milliy else [],
    })


async def announce(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    if test["closed"]:
        raise ApiError(409, "Natijalar allaqachon e'lon qilingan")
    attempts = await run(db.list_attempts, test["id"])
    if not attempts:
        raise ApiError(400, "Hali hech kim javob yubormagan")
    missing = sum(1 for a in attempts if essay_pending(test, a))
    if missing:
        raise ApiError(409, f"{missing} ta o'quvchining esse bahosi kiritilmagan — natijalar esse bahosi bilan chiqadi. "
                            "«📝 Esselar»da 75 ballik shkalada kiriting.", essays_missing=missing)
    # Test darhol yopiladi, xabarlar fonda ketadi: 300 o'quvchida ham so'rov kutib qolmaydi (tugagach bot xabar beradi)
    await run(db.set_closed, test["id"], True)
    await run(db.set_announcing, test["id"], True)
    queued = sum(1 for a in attempts if a["tg_id"] > 0 and not a.get("notified"))
    task = asyncio.create_task(HOOKS["announce"]({**test, "closed": 1}, user["id"]))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return ok({"ok": True, "queued": queued, "sent": queued})


async def delete_test(request: web.Request) -> web.Response:
    """Testni butunlay o'chirish: variantlar, natijalar, esselar va o'quvchilar ro'yxati ham (CASCADE)."""
    user = await auth(request)
    test = await own_test(request, user)
    await run(db.delete_test, test["id"])
    return ok({"ok": True})


async def reopen(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    await run(db.set_closed, test["id"], False)
    return ok({"ok": True})


async def send_pdf(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    kind = (await body(request)).get("kind")
    if kind not in ("key", "results", "excel", "rating"):
        raise ApiError(400, "Noma'lum hujjat")
    try:
        sent = await deliver(HOOKS["send_pdf"](user["id"], test, kind), f"Fayl ({kind}), test {test['id']}")
    except Exception as exc:
        logger.exception("Fayl (%s) yuborilmadi: test %s", kind, test["id"])
        raise ApiError(502, send_error_text(exc))
    return ok({"ok": True, "pending": not sent})


# ---------- Ustoz — o'quvchi: ulash va «✍️ Esse tekshirish» (ustoz kreditidan) ----------
# O'quvchi ustozga ikki yo'l bilan ulanadi: ustoz uning @username / Telegram ID'sini qo'shadi yoki o'quvchi ustozning
# taklif havolasini bosadi (t.me/<bot>?start=t_<ustoz>_<imzo>). Ulangan o'quvchi test bo'lmasa ham esse yozib,
# AI'da (75 ballik, imtihondagidek) tekshirtiradi — har esse ustozning 1 krediti. Ustoz har o'quvchiga oylik chegara qo'yadi.

STUDENT_LIMIT_DEFAULT = 10
PRACTICE_MIN_WORDS, PRACTICE_MAX_CHARS = 60, 20000
PUBLIC_FREE = 2  # ommaviy havola: begona o'quvchiga shuncha bepul esse, keyin kredit


def public_open(teacher_id: int) -> bool:
    return db.get_settings().get(f"essay_public:{teacher_id}") == "1"


def invite_code(teacher_id: int) -> str:
    sig = hmac.new(f"invite:{BOT_TOKEN}".encode(), str(teacher_id).encode(), hashlib.sha256).hexdigest()[:10]
    return f"t_{teacher_id}_{sig}"


def public_code(teacher_id: int) -> str:
    """Begonalar uchun havola (esse tekshiruvi pullik): t.me/<bot>?start=p_<id>_<imzo>."""
    sig = hmac.new(f"public:{BOT_TOKEN}".encode(), str(teacher_id).encode(), hashlib.sha256).hexdigest()[:10]
    return f"p_{teacher_id}_{sig}"


def teacher_from_public(code: str) -> int | None:
    parts = code.split("_")
    if len(parts) != 3 or parts[0] != "p" or not parts[1].isdigit():
        return None
    return int(parts[1]) if hmac.compare_digest(public_code(int(parts[1])), code) else None


def teacher_from_invite(code: str) -> int | None:
    parts = code.split("_")
    if len(parts) != 3 or parts[0] != "t" or not parts[1].isdigit():
        return None
    return int(parts[1]) if hmac.compare_digest(invite_code(int(parts[1])), code) else None


def student_limit(teacher_id: int) -> int:
    try:
        return max(1, min(200, int(db.get_settings().get(f"student_limit:{teacher_id}", STUDENT_LIMIT_DEFAULT))))
    except (TypeError, ValueError):
        return STUDENT_LIMIT_DEFAULT


async def link_by_teacher(teacher: dict, student_id: int) -> bool:
    """Ulash va ikkala tomonga xabar. Qaytaradi: yangi ulandimi."""
    fresh = await run(db.link_student, teacher["id"], student_id)
    if fresh and "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](
                student_id, f"✅ <b>Siz {html.escape(teacher.get('name') or 'ustoz')} ustozga ulandingiz</b>\n"
                "Endi ilovada «✍️ Esse tekshirish»: esse yozasiz — AI imtihondagidek 75 ballik shkalada tekshiradi "
                "(ustozingiz hisobidan).")
        except Exception:
            logger.warning("O'quvchiga ulanish xabari yuborilmadi", exc_info=True)
    return fresh


async def link_view(request: web.Request) -> web.Response:
    user = await auth(request)
    if not await run(plans.is_free, user["id"]) and not await run(plans.sales_on):
        raise ApiError(403, "Bu bo'lim test yaratishga ruxsati bor o'qituvchilar uchun")
    username = await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""
    students = await run(db.linked_students, user["id"], plans.month_key())
    offer = await run(plans.pack)
    card = await run(plans.card)
    unlimited = plans.ai_unlimited(user["id"])
    return ok({
        "invite_url": f"https://t.me/{username}?start={invite_code(user['id'])}" if username else None,
        "essay_code": await run(db.essay_code_of, user["id"]),
        "students": [{"id": s["student_id"], "name": s["full_name"] or "O'quvchi", "username": s["username"],
                      "used": s["used"]} for s in students],
        "limit": await run(student_limit, user["id"]),
        "credits": None if unlimited else await run(db.get_credits, user["id"]),
        "unlimited": unlimited, "ai": essay_ai.essay_enabled(),
        "pack": offer, "card_ready": bool(card["number"]) and bool(offer["price"]),
        "payment": await run(db.open_payment, user["id"]),
        # Begonalar uchun: esse narxi, kredit paketlari va ustoz kartasi (pul shunga tushadi)
        "essay_packs": await run(essay_packs, user["id"]),
        "my_card": await run(teacher_card, user["id"]),
    })


async def link_add(request: web.Request) -> web.Response:
    user = await auth(request)
    if not await run(plans.is_free, user["id"]) and not await run(plans.sales_on):
        raise ApiError(403, "Ruxsat yo'q")
    ref = str((await body(request)).get("ref") or "").strip()
    if not ref:
        raise ApiError(400, "O'quvchining @username'ini yoki Telegram ID'sini yozing")
    found = await run(db.find_user, ref.split()[0])
    if not found or (not ref.lstrip("@").isdigit() and not found.get("full_name")):
        raise ApiError(404, "Topilmadi — o'quvchi avval botga /start bosgan bo'lishi kerak (yoki taklif havolasini yuboring)")
    if found["tg_id"] == user["id"]:
        raise ApiError(400, "O'zingizni qo'sha olmaysiz")
    await link_by_teacher(user, found["tg_id"])
    return await link_view(request)


async def link_remove(request: web.Request) -> web.Response:
    user = await auth(request)
    await run(db.unlink_student, user["id"], to_int((await body(request)).get("student_id"), 0, "O'quvchi"))
    return await link_view(request)


async def link_limit(request: web.Request) -> web.Response:
    user = await auth(request)
    limit = to_int((await body(request)).get("limit"), STUDENT_LIMIT_DEFAULT, "Chegara")
    if not 1 <= limit <= 200:
        raise ApiError(400, "Oylik chegara 1–200 ta esse")
    await run(db.set_setting, f"student_limit:{user['id']}", limit)
    return await link_view(request)


def _practice_view(item: dict) -> dict:
    ai = item["ai_result"] or {}
    return {"id": item["id"], "status": item["status"], "topic": item["topic"], "created_at": item["created_at"],
            "score": item["ai_score"], "score_75": essay_to_75(item["ai_score"]), "max": ESSAY_MAX, "scale": ESSAY_SCALE,
            "criteria": ai.get("criteria") if item["status"] == "graded" else None,
            "comment": ai.get("comment") if item["status"] == "graded" else None,
            "titles": essay_ai.RUBRIC_TITLES, "words": len(item["text"].split())}


async def practice_home(request: web.Request) -> web.Response:
    """O'quvchi: ustozlari (oylik qoldiq bilan) va oldingi esselari."""
    user = await auth(request)
    month = plans.month_key()
    teachers = []
    for t in await run(db.teachers_of, user["id"]):
        limit = await run(student_limit, t["teacher_id"])
        used = await run(db.practice_used, t["teacher_id"], user["id"], month)
        teachers.append({"id": t["teacher_id"], "name": t["full_name"] or "Ustoz", "kind": "student",
                         "left": max(0, limit - used), "limit": limit})
    linked = {t["id"] for t in teachers}
    for w in await run(db.wallets_of_student, user["id"]):  # begona: ommaviy havola orqali tanigan ustozlari
        tid = w["teacher_id"]
        if tid in linked or tid == user["id"]:
            continue
        is_open = await run(public_open, tid)
        if not is_open and not w["balance"]:
            continue
        free_left = max(0, PUBLIC_FREE - await run(db.public_free_used, tid, user["id"]))
        card = await run(teacher_card, tid)
        teachers.append({"id": tid, "name": w["full_name"] or "Ustoz", "kind": "public", "open": is_open,
                         "free_left": free_left, "free": PUBLIC_FREE, "balance": w["balance"],
                         "waiting": len(await run(db.practice_awaiting_credit, tid, user["id"])),
                         "packs": await run(essay_packs, tid) if card["number"] else []})
    history = [_practice_view(p) for p in await run(db.practice_of_student, user["id"], 20)]
    return ok({"teachers": teachers, "history": history, "ai": essay_ai.essay_enabled()})


async def practice_public_submit(user: dict, teacher_id: int, topic: str | None, text: str) -> dict:
    """Begona o'quvchi ilovada: bepullari bo'lsa — ustoz kreditidan; tugagan bo'lsa — o'z kreditidan (yo'q bo'lsa
    esse saqlanadi va kredit olgach o'zi tekshiriladi). Botdagi ommaviy havola bilan bir xil qoidalar."""
    if not await run(public_open, teacher_id):
        raise ApiError(403, "Ustoz ommaviy esse tekshiruvini yopgan")
    await run(db.remember_user, user["id"], user["name"], user.get("username"))
    await run(db.remember_public, teacher_id, user["id"])
    if await run(db.public_free_used, teacher_id, user["id"]) < PUBLIC_FREE:
        if not await ai_allowed(teacher_id, "essays"):
            raise ApiError(402, "Ustozning AI krediti tugagan — keyinroq urinib ko'ring yoki ustozga ayting")
        practice_id = await run(db.create_practice, teacher_id, user["id"], topic, text, "public")
        if "wake_ai" in HOOKS:
            HOOKS["wake_ai"]()
        return {"ok": True, "id": practice_id, "status": "pending", "free": True}
    if not await run(db.wallet_balance, teacher_id, user["id"]) and not await run(essay_packs, teacher_id):
        raise ApiError(409, "Ustoz hali kredit narxini qo'ymagan")
    practice_id = await run(db.create_practice, teacher_id, user["id"], topic, text, "public", None, "awaiting_credit")
    _released, teacher_out = await release_waiting(teacher_id, user["id"])
    status = (await run(db.get_practice, practice_id))["status"]
    return {"ok": True, "id": practice_id, "status": status, "teacher_out": teacher_out,
            "balance": await run(db.wallet_balance, teacher_id, user["id"])}


async def practice_wallet_order(request: web.Request) -> web.Response:
    """Ilovadagi «✍️ Esse tekshirish»: begona o'quvchi ustozning kredit paketini oladi."""
    user = await auth(request)
    payload = await body(request)
    teacher_id = to_int(payload.get("teacher_id"), 0, "Ustoz")
    if await run(db.is_linked, teacher_id, user["id"]):
        raise ApiError(409, "Siz bu ustozning o'quvchisisiz — esselaringiz bepul")
    return ok(await wallet_order(teacher_id, user, to_int(payload.get("essays"), 1, "Paket")))


async def practice_submit(request: web.Request) -> web.Response:
    user = await auth(request)
    payload = await body(request)
    teacher_id = to_int(payload.get("teacher_id"), 0, "Ustoz")
    linked = await run(db.is_linked, teacher_id, user["id"])
    if not linked and (teacher_id == user["id"] or not await run(db.find_user, str(teacher_id))):
        raise ApiError(403, "Siz bu ustozga ulanmagansiz")
    if not essay_ai.essay_enabled():
        raise ApiError(503, "AI esse tekshiruvi hozir o'chiq")
    text = str(payload.get("essay") or "").strip()
    if len(text.split()) < PRACTICE_MIN_WORDS:
        raise ApiError(400, f"Esse juda qisqa — kamida {PRACTICE_MIN_WORDS} so'z yozing")
    if len(text) > PRACTICE_MAX_CHARS:
        raise ApiError(400, "Esse juda uzun")
    if not linked:  # begona — ommaviy havola qoidalari (bepul / kredit)
        return ok(await practice_public_submit(user, teacher_id, str(payload.get("topic") or "").strip()[:1500] or None, text))
    limit = await run(student_limit, teacher_id)
    if await run(db.practice_used, teacher_id, user["id"], plans.month_key()) >= limit:
        raise ApiError(429, f"Bu oy uchun chegara tugadi ({limit} ta esse) — keyingi oy yoki ustozingizdan so'rang")
    if not await ai_allowed(teacher_id, "essays"):  # ustozning 1 krediti
        raise ApiError(402, "Ustozingizning AI krediti tugagan — ustozingizga ayting")
    topic = str(payload.get("topic") or "").strip()[:1500] or None
    await run(db.remember_user, user["id"], user["name"], user.get("username"))
    practice_id = await run(db.create_practice, teacher_id, user["id"], topic, text)
    if "wake_ai" in HOOKS:
        HOOKS["wake_ai"]()
    return ok({"ok": True, "id": practice_id})


async def practice_get(request: web.Request) -> web.Response:
    user = await auth(request)
    item = await run(db.get_practice, int(request.match_info["practice_id"]))
    if not item or item["student_id"] != user["id"]:
        raise ApiError(404, "Topilmadi")
    return ok(_practice_view(item))


# ---------- Kursni topish: viloyat → tuman → ustozlar; «O'quvchisiman» arizasi ----------

def _not_approved(status: str | None) -> str | None:
    return None if status == "approved" else status  # ustoz keyin o'chirgan bo'lsa — qayta ariza yuborsa bo'ladi


async def courses(request: web.Request) -> web.Response:
    """O'quvchi tumanini tanladi: shu tumanda kurs ochgan ustozlar (ismi, telefoni) va ularga holati."""
    user = await auth(request)
    region, district = str(request.query.get("region") or ""), str(request.query.get("district") or "")
    if region not in places_data.REGIONS or district not in places_data.REGIONS[region]:
        raise ApiError(400, "Viloyat va tumanni tanlang")
    out = []
    for t in await run(db.teachers_in, region, district):
        if t["tg_id"] == user["id"]:
            continue
        linked = await run(db.is_linked, t["tg_id"], user["id"])
        out.append({"id": t["tg_id"], "name": t["full_name"] or "Ustoz", "username": t["username"], "phone": t["phone"],
                    "status": "approved" if linked else _not_approved(await run(db.join_status, t["tg_id"], user["id"]))})
    return ok({"teachers": out, "region": region, "district": district})


async def course_join(request: web.Request) -> web.Response:
    """«O'quvchisiman»: ism-familiya (majburiy) va rasm (ixtiyoriy) — ustozga ariza, u tasdiqlasa o'quvchisi bo'ladi."""
    user = await auth(request)
    try:
        form = await request.post()
    except Exception:
        raise ApiError(400, "Ariza yuborilmadi")
    teacher_id = to_int(form.get("teacher_id"), 0, "Ustoz")
    first, last = clean_text(form.get("first_name"), 40), clean_text(form.get("last_name"), 40)
    if len(first) < 2 or len(last) < 2:
        raise ApiError(400, "Ism va familiyangizni yozing")
    if not teacher_id or teacher_id == user["id"] or not await run(db.find_user, str(teacher_id)):
        raise ApiError(404, "Ustoz topilmadi")
    if await run(db.is_linked, teacher_id, user["id"]):
        return ok({"ok": True, "status": "approved"})
    photo, mime, raw = None, None, None
    field = form.get("photo")
    if field is not None and hasattr(field, "file"):
        raw = field.file.read()
        if len(raw) > 6 * 1024 * 1024:
            raise ApiError(400, "Rasm 6 MB dan kichik bo'lsin")
        if raw:
            photo, mime = base64.b64encode(raw).decode(), (getattr(field, "content_type", None) or "image/jpeg")[:20]
    await run(db.remember_user, user["id"], user["name"], user.get("username"))
    request_id = await run(db.save_join_request, teacher_id, user["id"], first, last, photo, mime)
    if request_id and "join_notice" in HOOKS:
        try:
            await HOOKS["join_notice"](teacher_id, request_id, f"{first} {last}", user.get("username"), raw or None)
        except Exception:
            logger.warning("Ariza haqida ustozga xabar yuborilmadi", exc_info=True)
    return ok({"ok": True, "status": "pending"})


async def joins_list(request: web.Request) -> web.Response:
    user = await auth(request)
    return ok({"joins": await run(db.join_requests_for, user["id"])})


async def join_photo(request: web.Request) -> web.Response:
    user = await auth(request)
    item = await run(db.get_join_request, int(request.match_info["request_id"]))
    if not item or item["teacher_id"] != user["id"] or not item["photo"]:
        raise ApiError(404, "Rasm yo'q")
    return ok({"image": _image_data_url(item["photo"], item["mime"])})


async def decide_join(teacher_id: int, request_id: int, approve: bool) -> dict:
    """Ustoz «O'quvchisiman» arizasini tasdiqlaydi — o'quvchisi bo'ladi (mocklar ustoz hisobidan). Bot tugmasi ham shu."""
    item = await run(db.get_join_request, request_id)
    if not item or item["teacher_id"] != teacher_id:
        raise ApiError(404, "Ariza topilmadi")
    if not await run(db.decide_join_request, request_id, "approved" if approve else "rejected"):
        raise ApiError(409, "Bu ariza allaqachon ko'rib chiqilgan")
    name = f"{item['first_name']} {item['last_name']}"
    if approve:
        await run(db.link_student, teacher_id, item["student_id"])
    if "send_text" in HOOKS:
        teacher = await run(db.find_user, str(teacher_id)) or {}
        text = (f"✅ <b>{html.escape(teacher.get('full_name') or 'Ustoz')} sizni o'quvchi sifatida qabul qildi</b>\n"
                "Endi ustozingizning onlayn mocklarini bepul topshirasiz va «✍️ Esse tekshirish»dan foydalanasiz." if approve else
                "❌ Ustoz «o'quvchisiman» arizangizni tasdiqlamadi. Ism-familiyani tekshirib, qayta yuborishingiz mumkin.")
        try:
            await HOOKS["send_text"](item["student_id"], text)
        except Exception:
            logger.warning("Ariza natijasi o'quvchiga yuborilmadi", exc_info=True)
    return {"ok": True, "status": "approved" if approve else "rejected", "name": name}


async def join_decide(request: web.Request) -> web.Response:
    user = await auth(request)
    action = (await body(request)).get("action")
    if action not in ("approve", "reject"):
        raise ApiError(400, "Amal noto'g'ri")
    return ok(await decide_join(user["id"], int(request.match_info["request_id"]), action == "approve"))


# ---------- Fayllarni to'g'ridan-to'g'ri yuklab olish (bot chatiga yuborilmasa ham) ----------
# Havola 10 daqiqa amal qiladi va imzolangan: kim, qaysi test, qaysi fayl. Telegram'da tashqi brauzer/yuklovchi
# sarlavha (initData) yubora olmaydi — shuning uchun ruxsat havolaning o'zida

DOWNLOAD_TTL = 600
FILE_KINDS = ("key", "results", "excel", "sheets", "essay", "rating")


def _sign(payload: str) -> str:
    return hmac.new(f"dl:{BOT_TOKEN}".encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def download_token(test_id: int, kind: str) -> str:
    payload = f"{test_id}.{kind}.{int(time.time()) + DOWNLOAD_TTL}"
    return f"{payload}.{_sign(payload)}"


def build_file(test: dict, kind: str) -> tuple[bytes, str, str]:
    """(fayl, nomi, turi) — bot yuboradigan fayllarning o'zi."""
    keys = db.get_keys(test["id"])
    if kind == "key":
        return pdfs.answer_key_pdf(test, keys), f"kalit_{test['code']}.pdf", "application/pdf"
    if kind in ("sheets", "essay"):  # imtihon varaqasida test titullari va esse varaqlari — alohida fayllar
        students = db.list_students(test["id"])
        official = sheet_styles.style_of(test) == "official"
        part = kind if kind == "essay" else ("test" if official else "all")
        name = f"{'esse' if kind == 'essay' else 'titullar'}_{test['code']}.pdf"
        return sheet.sheets_pdf(test, list(keys), students, 0 if students else 1, part), name, "application/pdf"
    attempts = db.list_attempts(test["id"])
    if kind == "rating":
        import rating
        images = rating.rating_images(test, attempts)
        if not images:
            raise ApiError(400, "Reyting uchun hali natija yo'q")
        return rating.rating_pdf(images), f"reyting_{test['code']}.pdf", "application/pdf"
    if kind == "excel":
        import excel
        return (excel.results_xlsx(test, attempts), f"natijalar_{test['code']}.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    return pdfs.results_pdf(test, attempts), f"natijalar_{test['code']}.pdf", "application/pdf"


async def download_link(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    kind = (await body(request)).get("kind")
    if kind not in FILE_KINDS:
        raise ApiError(400, "Noma'lum fayl")
    if kind == "essay" and sheet_styles.style_of(test) != "official":
        raise ApiError(400, "Esse varaqlari faqat imtihon varaqasida")
    return ok({"url": f"{site_url(request)}/dl/{download_token(test['id'], kind)}"})


async def download_file(request: web.Request) -> web.StreamResponse:
    parts = request.match_info["token"].split(".")
    if len(parts) != 4 or not all(p.isalnum() for p in parts):
        raise web.HTTPNotFound(text="Havola noto'g'ri")
    test_id, kind, expires, sig = parts
    if not hmac.compare_digest(sig, _sign(f"{test_id}.{kind}.{expires}")) or kind not in FILE_KINDS:
        raise web.HTTPNotFound(text="Havola noto'g'ri")
    if not expires.isdigit() or int(expires) < time.time():
        raise web.HTTPGone(text="Havola eskirgan — ilovada «Yuklab olish»ni qayta bosing")
    test = await run(db.get_test, int(test_id))
    if not test:
        raise web.HTTPNotFound(text="Test topilmadi")
    content, name, mime = await run(build_file, test, kind)
    return web.Response(body=content, headers={
        "Content-Type": mime, "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


async def set_theme(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    theme = (await body(request)).get("theme")
    if theme not in THEMES:
        raise ApiError(400, "Dizayn topilmadi")
    await run(db.set_theme, test["id"], theme)
    return ok({"ok": True})


async def essay_detail(request: web.Request) -> web.Response:
    user = await auth(request)
    attempt, test = await _own_attempt(request, user)
    essay = await run(db.get_essay, attempt["id"])
    return ok({
        "name": attempt["student_name"], "essay": attempt["essay"],
        "status": essay["status"] if essay else "none", "text": essay["text"] if essay else None,
        "photos": len(essay["photos"]) if essay else 0, "ai_score": essay["ai_score"] if essay else None,
        "ai": essay["ai_result"] if essay else None, "error": essay["error"] if essay else None,
        "titles": essay_ai.RUBRIC_TITLES, "topic": test.get("essay_topic"),
    })


async def _own_attempt(request: web.Request, user: dict) -> tuple[dict, dict]:
    attempt = await run(db.get_attempt, int(request.match_info["attempt_id"]))
    test = attempt and await run(db.get_test, attempt["test_id"])
    if not test or test["owner_id"] != user["id"]:
        raise ApiError(404, "Topilmadi")
    return attempt, test


def _image_data_url(image_b64: str | None, mime: str | None) -> str | None:
    return f"data:{mime or 'image/jpeg'};base64,{image_b64}" if image_b64 else None


async def attempt_answers(request: web.Request) -> web.Response:
    """O'qituvchi bitta natijani ochadi: javoblar, kalit bilan solishtiruv, AI o'qigan yozma kataklar rasmi."""
    user = await auth(request)
    attempt, test = await _own_attempt(request, user)
    keys = await run(db.get_keys, test["id"])
    key = keys.get(attempt["variant"]) or next(iter(keys.values()))
    job = await run(db.get_written_job, attempt["id"])
    return ok({
        "id": attempt["id"], "name": attempt["student_name"], "variant": attempt["variant"], "variants": list(keys),
        "items": [{"label": i["label"], "kind": i["kind"], "options": i.get("options", 4)} for i in test["items"]],
        "answers": attempt["answers"], "wrong": attempt["wrong"],
        "key": key,
        "points": total_points(attempt), "max_points": max_points(test["items"], test["fmt"]),
        "written": {"status": job["status"], "error": job["error"], "ai": json.loads(job["result"]) if job["result"] else None,
                    "image": _image_data_url(job["image"], job["mime"])} if job else None,
        "closed": bool(test["closed"]),
        "offline": attempt["tg_id"] < 0,  # titul natijasi — boshqa o'quvchiga ko'chirsa bo'ladi
        "test_id": test["id"],
    })


def sheet_token(attempt_id: int, expires: int) -> str:
    payload = f"{attempt_id}.{expires}"
    return f"{payload}.{_sign('sp:' + payload)}"


async def attempt_sheet(request: web.Request) -> web.Response:
    """Skanerlangan titul surati (imzolangan havola) va doirachalar joyi — ilova belgilarni o'zi chizadi."""
    user = await auth(request)
    attempt, _test = await _own_attempt(request, user)
    sheet = await run(db.get_sheet, attempt["id"])
    if not sheet:
        raise ApiError(404, "Bu natijaning titul surati yo'q (qo'lda kiritilgan yoki eski skaner)")
    if not sheet["file_id"]:
        return ok({"pending": True})  # surat hali botga yuborilmagan (albom to'lmagan) — «Yuborish tugadi»dan keyin
    marks = {}
    try:
        marks = json.loads(sheet["marks"] or "{}")
    except ValueError:
        pass
    return ok({"url": f"/sp/{sheet_token(attempt['id'], int(time.time()) + 3600)}", "marks": marks})


async def sheet_photo(request: web.Request) -> web.StreamResponse:
    parts = request.match_info["token"].split(".")
    if len(parts) != 3 or not all(p.isalnum() for p in parts):
        raise web.HTTPNotFound(text="Havola noto'g'ri")
    attempt_id, expires, sig = parts
    if not hmac.compare_digest(sig, _sign(f"sp:{attempt_id}.{expires}")) or int(expires) < time.time():
        raise web.HTTPNotFound(text="Havola eskirgan")
    sheet = await run(db.get_sheet, int(attempt_id))
    if not sheet or not sheet["file_id"] or "download_file" not in HOOKS:
        raise web.HTTPNotFound(text="Surat topilmadi")
    data = await HOOKS["download_file"](sheet["file_id"])
    return web.Response(body=data, headers={"Content-Type": "image/jpeg", "Cache-Control": "private, max-age=3600"})


async def attempt_delete(request: web.Request) -> web.Response:
    """Noto'g'ri skanerlangan natijani o'chirish (ro'yxatdagi o'quvchi qoladi — qayta skanerlash yoki qo'lda kiritish)."""
    user = await auth(request)
    attempt, _test = await _own_attempt(request, user)
    await run(db.delete_attempt, attempt["id"])
    return ok({"ok": True})


async def attempt_rename(request: web.Request) -> web.Response:
    user = await auth(request)
    attempt, _test = await _own_attempt(request, user)
    name = clean_name((await body(request)).get("name"))
    if len(name) < 3:
        raise ApiError(400, "Ism va familiyani to'liq yozing")
    await run(db.rename_attempt, attempt["id"], name)
    return ok({"ok": True, "name": name})


async def attempt_move(request: web.Request) -> web.Response:
    """ID xato o'qilgan: natija boshqa o'quvchiga (ro'yxatdan yoki sinfdagi ID bo'yicha). U o'quvchining natijasi bor
    bo'lsa — replace=True bilan almashtiriladi."""
    user = await auth(request)
    attempt, test = await _own_attempt(request, user)
    if attempt["tg_id"] >= 0:
        raise ApiError(400, "Onlayn natijani boshqa o'quvchiga ko'chirib bo'lmaydi")
    payload = await body(request)
    target = None
    if payload.get("student_id"):
        target = await run(db.get_student, test["id"], to_int(payload["student_id"], 0, "O'quvchi"))
    elif str(payload.get("number") or "").isdigit():
        number = int(payload["number"])
        target = await run(db.get_student, test["id"], None, number)
        if not target:  # test ro'yxatida yo'q — sinfdan (doimiy ID)
            pupil = (await pupils_of(test["owner_id"])).get(number)
            if not pupil:
                raise ApiError(404, f"ID {number:04d} ro'yxatda ham, sinflarda ham yo'q")
            target = (await run(db.add_students, test["id"], [pupil["name"]], [attempt["variant"]], [number]))[-1]
    if not target:
        raise ApiError(404, "O'quvchi topilmadi")
    if target["attempt_id"] == attempt["id"]:
        return ok({"ok": True, "name": target["name"]})
    if target["attempt_id"]:
        if not payload.get("replace"):
            raise ApiError(409, f"{target['name']}ning natijasi bor — almashtirasizmi?", busy=True)
        await run(db.delete_attempt, target["attempt_id"])
    await run(db.move_attempt, attempt["id"], db.offline_tg_id(test["id"], target["number"]), target["name"])
    await run(db.link_student_attempt, target["id"], attempt["id"])
    return ok({"ok": True, "name": target["name"], "number": target["number"]})


async def attempt_answers_save(request: web.Request) -> web.Response:
    """Tuzatilgan javoblar → natija qayta hisoblanadi (Rasch ham shundan). retry_ai — yozma kataklarni AI qayta o'qisin."""
    user = await auth(request)
    attempt, test = await _own_attempt(request, user)
    payload = await body(request)
    if payload.get("retry_ai"):
        await run(db.retry_written, attempt["id"])
        return ok({"ok": True})
    keys = await run(db.get_keys, test["id"])
    variant = payload.get("variant") or attempt["variant"]
    if variant not in keys:
        raise ApiError(400, "Variant noto'g'ri")
    answers = clean_answers(test, payload.get("answers") or {})
    result = grade(test["items"], keys[variant], answers)
    await run(db.update_attempt_result, attempt["id"], answers, result, variant)
    fresh = await run(db.get_attempt, attempt["id"])
    return ok({"ok": True, "points": total_points(fresh), "max_points": max_points(test["items"], test["fmt"]),
               "wrong": fresh["wrong"], "correct": fresh["test_correct"] + fresh["written_correct"],
               "auto_total": fresh["auto_total"]})


# ---------- Yozma javoblarni qo'lda tekshirish (36–44) ----------
# Skanerda har titulning yozma kataklari bitta rasm bo'lib saqlanadi. O'qituvchi savol bo'yicha yuradi:
# 36-savol → har o'quvchining 36-katagi (rasmdan kesilgan) kalit bilan yonma-yon → ✅ / ❌ → keyingi o'quvchi,
# keyin 37-savol va h.k. Belgi o'quvchining javobiga yoziladi: ✅ — kalitdagi javob, ❌ — WRONG_MARK.
# Natija (ball, Rasch) odatdagidek qayta hisoblanadi; ortga qaytib belgini almashtirsa bo'ladi.

WRONG_MARK = "✗"


def written_boxes(test: dict, variants: list[str]) -> dict[str, list[float]]:
    """Har yozma savol qatorining yozma maydon rasmi ichidagi joyi (ulushda):
    [x, y, kenglik, balandlik, raqam ustuni kengligi, bitta katak kengligi, kataklar soni].
    Ilova shundan qatorni kesadi va faqat yozilgan kataklarni kattalashtirib ko'rsatadi."""
    spec = sheet.layout(test, variants)
    area = spec.get("written_area")
    if not area:
        return {}
    x1, y1, x2, y2 = area
    width, height = x2 - x1, y2 - y1
    return {box["label"]: [round((box["x"] - x1) / width, 4), round((box["top"] - y1) / height, 4),
                           round(box["w"] / width, 4), round(box["h"] / height, 4),
                           round(box.get("label_w", sheet.WRITTEN_LABEL_W) / width, 4),  # imtihon varaqasida kataklar boshqa
                           round(box.get("cell_w", sheet.WRITTEN_CELL_W) / width, 4), box["cells"]]
            for box in spec["written"]}


def written_verdicts(attempt: dict, key: dict, labels: list[str]) -> dict[str, bool | None]:
    """None — hali belgilanmagan (katak bo'sh, AI o'qimagan); True/False — to'g'ri/noto'g'ri."""
    out = {}
    for label in labels:
        given = str(attempt["answers"].get(label) or "").strip()
        out[label] = None if not given else text_is_correct(given, key.get(label, ""))
    return out


async def written_review_summary(test: dict, attempts: list[dict], keys: dict) -> dict | None:
    """O'qituvchi sahifasidagi «✍️ Yozmalarni tekshirish» tugmasi uchun: nechta katak belgilangan."""
    labels = [i["label"] for i in text_items(test["items"])]
    if not labels or not keys:
        return None
    with_image = await run(db.written_images_for_test, test["id"])
    rows = [a for a in attempts if a["id"] in with_image]
    if not rows:
        return None
    done = sum(1 for a in rows for v in written_verdicts(a, keys.get(a["variant"]) or next(iter(keys.values())), labels).values()
               if v is not None)
    return {"students": len(rows), "total": len(rows) * len(labels), "done": done}


async def written_review(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    labels = [i["label"] for i in text_items(test["items"])]
    if not labels:
        raise ApiError(400, "Bu testda yozma savollar yo'q")
    keys = await run(db.get_keys, test["id"])
    with_image = await run(db.written_images_for_test, test["id"])
    numbers = {s["attempt_id"]: s["number"] for s in await run(db.list_students, test["id"]) if s["attempt_id"]}
    attempts = [a for a in await run(db.list_attempts, test["id"]) if a["id"] in with_image]
    attempts.sort(key=lambda a: (numbers.get(a["id"]) or 10 ** 6, a["student_name"]))
    students = []
    for a in attempts:
        key = keys.get(a["variant"]) or next(iter(keys.values()))
        students.append({"attempt_id": a["id"], "name": a["student_name"], "number": numbers.get(a["id"]),
                         "variant": a["variant"], "answers": {l: a["answers"].get(l, "") for l in labels},
                         "verdict": written_verdicts(a, key, labels)})
    return ok({
        "test": {"id": test["id"], "title": test["title"], "code": test["code"], "closed": bool(test["closed"])},
        "labels": labels, "students": students, "wrong_mark": WRONG_MARK,
        "keys": {v: {l: split_options(k.get(l, "")) for l in labels} for v, k in keys.items()},
        "boxes": written_boxes(test, list(keys)),
    })


async def written_image(request: web.Request) -> web.Response:
    user = await auth(request)
    attempt, _test = await _own_attempt(request, user)
    job = await run(db.get_written_job, attempt["id"])
    if not job or not job["image"]:
        raise ApiError(404, "Bu titulning yozma kataklari rasmi saqlanmagan")
    return ok({"image": _image_data_url(job["image"], job["mime"])})


async def written_mark(request: web.Request) -> web.Response:
    """✅ / ❌ — o'quvchining shu yozma savoliga belgi; ball va Rasch qayta hisoblanadi (ID bo'yicha o'z natijasiga)."""
    user = await auth(request)
    attempt, test = await _own_attempt(request, user)
    payload = await body(request)
    label = str(payload.get("label") or "")
    if label not in {i["label"] for i in text_items(test["items"])}:
        raise ApiError(400, "Savol noto'g'ri")
    keys = await run(db.get_keys, test["id"])
    key = keys.get(attempt["variant"]) or next(iter(keys.values()))
    correct = bool(payload.get("ok"))
    options = split_options(key.get(label, ""))
    if correct and not options:
        raise ApiError(400, f"{label}-savolning kaliti kiritilmagan")
    answers = dict(attempt["answers"])
    answers[label] = options[0] if correct else WRONG_MARK
    result = grade(test["items"], key, answers)
    await run(db.update_attempt_result, attempt["id"], answers, result)
    return ok({"ok": True, "label": label, "verdict": correct, "answer": answers[label],
               "written_correct": result["written_correct"], "points": result["points"]})


async def essay_score(request: web.Request) -> web.Response:
    user = await auth(request)
    attempt, test = await _own_attempt(request, user)
    payload = await body(request)
    if payload.get("action") == "confirm":
        essay = await run(db.get_essay, attempt["id"])
        if not essay or essay["status"] != "graded":
            raise ApiError(400, "AI bahosi yo'q")
        score = essay["ai_score"]
    elif payload.get("action") == "retry":
        await run(db.retry_essay, attempt["id"])
        return ok({"ok": True})
    elif payload.get("score75") not in (None, ""):  # o'qituvchi 75 ballik shkalada kiritdi (oflayn — qog'ozdagi esse)
        try:
            score = essay_from_75(float(str(payload.get("score75")).replace(",", ".")))
        except ValueError:
            raise ApiError(400, "75 ballik baho: 0 yoki 28–75 oralig'idagi butun son")
    else:
        try:
            score = float(str(payload.get("score")).replace(",", "."))
        except ValueError:
            raise ApiError(400, "Ball noto'g'ri")
        if not 0 <= score <= ESSAY_MAX or round(score * 2) != score * 2:
            raise ApiError(400, f"0–{ESSAY_MAX} ball, 0,5 qadam bilan")
    await run(db.set_essay, attempt["id"], score)
    if HOOKS.get("essay_notice"):
        await HOOKS["essay_notice"](test, attempt, score)
    return ok({"ok": True, "score": score, "score_75": essay_to_75(score), "attempt_id": attempt["id"]})


async def essays_confirm_all(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    attempts = await run(db.list_attempts, test["id"])
    essays = await run(db.essays_for_test, test["id"])
    count = 0
    for a in attempts:
        essay = essays.get(a["id"])
        if a["essay"] is None and essay and essay["status"] == "graded":
            await run(db.set_essay, a["id"], essay["ai_score"])
            count += 1
            if HOOKS.get("essay_notice"):  # e'londan keyin tasdiqlansa — o'quvchiga yangi ball
                try:
                    await HOOKS["essay_notice"](test, a, essay["ai_score"])
                except Exception:
                    logger.warning("Esse bahosi o'quvchiga yuborilmadi", exc_info=True)
    return ok({"ok": True, "count": count})


# ---------- Oflayn test: o'quvchilar, titullar, skaner ----------

def student_view(student: dict, pupils: dict | None = None) -> dict:
    pupil = (pupils or {}).get(student["number"])
    return {"id": student["id"], "number": student["number"], "name": student["name"],
            "variant": student["variant"], "scanned": bool(student["attempt_id"]), "attempt_id": student["attempt_id"],
            "class_name": pupil["class_name"] if pupil else None}


async def pupils_of(owner_id: int) -> dict[int, dict]:
    """{doimiy ID: {number, name, class_name}} — o'qituvchining BARCHA sinflari. ID o'qituvchi bo'yicha takrorlanmaydi,
    shuning uchun titul qaysi testda skanerlansa ham o'quvchi va uning sinfi (masalan 11-A) ID'dan aniqlanadi."""
    return {s["number"]: {"number": s["number"], "name": s["name"], "class_name": s["class_name"]}
            for s in await run(db.owner_class_students, owner_id)}


def assign_variants(count: int, variants: list[str], mode: str, start: int = 0) -> list[str]:
    """Variant taqsimlash: cycle — navbat bilan (A, B, C, A...), random — aralash, aks holda bitta variant."""
    if mode in variants:
        return [mode] * count
    if mode == "random":
        pool = [variants[(start + i) % len(variants)] for i in range(count)]
        random.shuffle(pool)
        return pool
    return [variants[(start + i) % len(variants)] for i in range(count)]


async def students_list(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    pupils = await pupils_of(user["id"])
    return ok({"students": [student_view(s, pupils) for s in await run(db.list_students, test["id"])],
               "variants": list(await run(db.get_keys, test["id"]))})


async def students_add(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    names = [" ".join(str(n).split())[:60] for n in (payload.get("names") or []) if str(n).strip()]
    if not names:
        raise ApiError(400, "Ism-familiyalarni yozing (har biri yangi qatorda)")
    existing = await run(db.list_students, test["id"])
    if len(existing) + len(names) > sheet.MAX_STUDENTS:
        raise ApiError(400, f"Bitta testda ko'pi bilan {sheet.MAX_STUDENTS} o'quvchi (ID 001–999)")
    variants = list(await run(db.get_keys, test["id"]))
    chosen = assign_variants(len(names), variants, str(payload.get("mode") or "cycle"), start=len(existing))
    students = await run(db.add_students, test["id"], names, chosen)
    return ok({"students": [student_view(s) for s in students]})


HEADER_WORDS = ("ism", "familiya", "f.i.sh", "fish", "f.i.o", "name", "o'quvchi", "oquvchi", "talaba", "№", "variant")


def parse_roster(data: bytes, filename: str, variant_names: list[str]) -> list[tuple[str, str | None]]:
    """
    Excel (.xlsx) yoki CSV ro'yxat → [(ism-familiya, variant yoki None)].
    Sarlavha qatori, tartib raqami ustuni va bo'sh qatorlar o'tkazib yuboriladi; "Familiya" va "Ism"
    alohida ustunlarda bo'lsa — birlashtiriladi; A/B/C/D katakchasi — variant.
    """
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        import openpyxl
        try:
            book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:
            raise ApiError(400, "Excel faylini ochib bo'lmadi — .xlsx formatida saqlang")
        rows = [list(row) for row in book.active.iter_rows(values_only=True)]
    elif name.endswith((".csv", ".txt")):
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ApiError(400, "Faylni o'qib bo'lmadi")
        delimiter = ";" if text.count(";") > text.count(",") else ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    elif name.endswith(".xls"):
        raise ApiError(400, "Eski .xls formati — Excel'da «Save as → .xlsx» qilib qayta yuklang")
    else:
        raise ApiError(400, "Faqat Excel (.xlsx) yoki CSV fayl")

    result = []
    for row in rows:
        cells = [" ".join(str(cell).split()) for cell in row if cell is not None and str(cell).strip()]
        if not cells:
            continue
        lowered = " ".join(cells).lower()
        if any(word in lowered for word in HEADER_WORDS) and not result and len(lowered) < 80:
            continue  # sarlavha qatori
        variant = next((cell.upper() for cell in cells if cell.upper() in variant_names and len(cell) == 1), None)
        words = [cell for cell in cells
                 if not cell.replace(".", "").isdigit() and not (len(cell) == 1 and cell.upper() in "ABCDEF")]
        full = " ".join(words[:3])[:60]
        if len(full) >= 3:
            result.append((full, variant))
    return result


async def students_import(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    try:
        form = await request.post()
    except Exception:
        raise ApiError(400, "Fayl yuborilmadi")
    field = form.get("file")
    if field is None or not hasattr(field, "file"):
        raise ApiError(400, "Fayl yuborilmadi")
    data = field.file.read()
    if len(data) > 5 * 1024 * 1024:
        raise ApiError(400, "Fayl juda katta (5 MB gacha)")
    variants = list(await run(db.get_keys, test["id"]))
    parsed = await run(parse_roster, data, field.filename, variants)
    if not parsed:
        raise ApiError(400, "Faylda ism-familiya topilmadi — birinchi ustunda F.I.Sh. bo'lsin")
    existing = await run(db.list_students, test["id"])
    if len(existing) + len(parsed) > sheet.MAX_STUDENTS:
        raise ApiError(400, f"Bitta testda ko'pi bilan {sheet.MAX_STUDENTS} o'quvchi")
    auto = assign_variants(len(parsed), variants, str(form.get("mode") or "cycle"), start=len(existing))
    names = [n for n, _ in parsed]
    chosen = [v or auto[i] for i, (_, v) in enumerate(parsed)]
    students = await run(db.add_students, test["id"], names, chosen)
    return ok({"students": [student_view(s) for s in students], "added": len(names),
               "with_variant": sum(1 for _, v in parsed if v)})


async def student_update(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    student_id = int(request.match_info["student_id"])
    if not await run(db.get_student, test["id"], student_id):
        raise ApiError(404, "O'quvchi topilmadi")
    if payload.get("delete"):
        await run(db.delete_student, test["id"], student_id)
    else:
        variant = payload.get("variant")
        if variant is not None and variant not in await run(db.get_keys, test["id"]):
            raise ApiError(400, "Variant noto'g'ri")
        name = " ".join(str(payload["name"]).split())[:60] if payload.get("name") else None
        await run(db.update_student, test["id"], student_id, name, variant)
    return ok({"students": [student_view(s) for s in await run(db.list_students, test["id"])]})


# ---------- Sinflar: o'quvchilarga doimiy ID ----------

def class_view(cls: dict, students: list[dict] | None = None) -> dict:
    view = {"id": cls["id"], "name": cls["name"], "students": cls.get("students", len(students or []))}
    if students is not None:
        view["list"] = [{"id": s["id"], "number": s["number"], "name": s["name"]} for s in students]
    return view


async def own_class(request: web.Request, user: dict) -> dict:
    cls = await run(db.get_class, int(request.match_info["class_id"]))
    if not cls or cls["owner_id"] != user["id"]:
        raise ApiError(404, "Sinf topilmadi")
    return cls


def clean_name(value) -> str:
    return " ".join(str(value or "").split())[:60]


def clean_text(value, limit: int) -> str:
    """Bo'sh joylarni tartibga solib, berilgan uzunlikkacha qisqartiradi (clean_name 60 belgi bilan cheklangan)."""
    return " ".join(str(value or "").split())[:limit]


async def classes_list(request: web.Request) -> web.Response:
    user = await auth(request)
    return ok({"classes": [class_view(c) for c in await run(db.list_classes, user["id"])]})


async def class_create(request: web.Request) -> web.Response:
    user = await auth(request)
    payload = await body(request)
    name = clean_name(payload.get("name"))[:40]
    if len(name) < 1:
        raise ApiError(400, "Sinf nomini yozing (masalan: 11-A)")
    if len(await run(db.list_classes, user["id"])) >= 100:
        raise ApiError(400, "Sinflar soni 100 tadan oshmasin")
    return ok({"class": class_view(await run(db.create_class, user["id"], name), [])})


async def class_detail(request: web.Request) -> web.Response:
    user = await auth(request)
    cls = await own_class(request, user)
    return ok({"class": class_view(cls, await run(db.list_class_students, cls["id"]))})


async def class_update(request: web.Request) -> web.Response:
    user = await auth(request)
    cls = await own_class(request, user)
    payload = await body(request)
    if payload.get("delete"):
        await run(db.delete_class, cls["id"])
        return ok({"ok": True})
    name = clean_name(payload.get("name"))[:40]
    if name:
        await run(db.rename_class, cls["id"], name)
    return ok({"class": class_view({**cls, "name": name or cls["name"]}, await run(db.list_class_students, cls["id"]))})


async def _add_to_class(user: dict, cls: dict, names: list[str]) -> list[dict]:
    names = [n for n in (clean_name(n) for n in names) if len(n) >= 3]
    if not names:
        raise ApiError(400, "Ism-familiyalarni yozing (har biri yangi qatorda)")
    if len(await run(db.list_class_students, cls["id"])) + len(names) > 300:
        raise ApiError(400, "Bitta sinfda ko'pi bilan 300 o'quvchi")
    try:
        return await run(db.add_class_students, cls["id"], user["id"], names)
    except ValueError as error:
        raise ApiError(400, str(error))


async def class_students_add(request: web.Request) -> web.Response:
    user = await auth(request)
    cls = await own_class(request, user)
    payload = await body(request)
    students = await _add_to_class(user, cls, list(payload.get("names") or []))
    return ok({"class": class_view(cls, students)})


async def class_students_import(request: web.Request) -> web.Response:
    user = await auth(request)
    cls = await own_class(request, user)
    try:
        form = await request.post()
    except Exception:
        raise ApiError(400, "Fayl yuborilmadi")
    field = form.get("file")
    if field is None or not hasattr(field, "file"):
        raise ApiError(400, "Fayl yuborilmadi")
    data = field.file.read()
    if len(data) > 5 * 1024 * 1024:
        raise ApiError(400, "Fayl juda katta (5 MB gacha)")
    parsed = await run(parse_roster, data, field.filename, [])
    if not parsed:
        raise ApiError(400, "Faylda ism-familiya topilmadi — birinchi ustunda F.I.Sh. bo'lsin")
    students = await _add_to_class(user, cls, [n for n, _ in parsed])
    return ok({"class": class_view(cls, students), "added": len(parsed)})


async def class_student_update(request: web.Request) -> web.Response:
    user = await auth(request)
    cls = await own_class(request, user)
    payload = await body(request)
    student_id = int(request.match_info["student_id"])
    await run(db.update_class_student, cls["id"], student_id, clean_name(payload.get("name")) or None,
              bool(payload.get("delete")))
    return ok({"class": class_view(cls, await run(db.list_class_students, cls["id"]))})


async def class_send_ids(request: web.Request) -> web.Response:
    """Sinfning «ID — ism» ro'yxati bot chatiga (o'quvchilarga tarqatish uchun)."""
    user = await auth(request)
    cls = await own_class(request, user)
    students = sorted(await run(db.list_class_students, cls["id"]), key=lambda s: s["number"])
    if not students:
        raise ApiError(400, "Sinfda hali o'quvchi yo'q")
    lines = [f"<code>{s['number']:04d}</code> — {html.escape(s['name'])}" for s in students]
    header = (f"👥 <b>{html.escape(cls['name'])}</b> · {len(students)} ta o'quvchi\n"
              "Titulda shu ID'ni 4 ta katakka yozib, doirachalarini bo'yaysiz.\n\n")
    chunk, chunks = header, []
    for line in lines:
        if len(chunk) + len(line) > 3800:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    chunks.append(chunk)
    for text in chunks:
        await HOOKS["send_text"](user["id"], text)
    return ok({"ok": True, "count": len(students)})


# ---------- «📊 Natijalar» bo'limi: sinf va test bo'yicha, PDF jadval va mock sertifikatlar ----------

def _file_slug(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text).strip("_")[:40] or "fayl"


async def _class_of(user: dict, raw) -> dict | None:
    """So'rovdagi sinf (ixtiyoriy) — faqat o'qituvchining o'ziniki."""
    if raw in (None, ""):
        return None
    if not str(raw).isdigit():
        raise ApiError(400, "Sinf noto'g'ri")
    cls = await run(db.get_class, int(raw))
    if not cls or cls["owner_id"] != user["id"]:
        raise ApiError(404, "Sinf topilmadi")
    return cls


async def results_overview(request: web.Request) -> web.Response:
    user = await auth(request)
    return ok(await run(results.overview, user["id"]))


async def class_results(request: web.Request) -> web.Response:
    """Sinf ichi: o'quvchilar × testlar (doimiy ID orqali), o'rtacha ball va daraja."""
    user = await auth(request)
    cls = await own_class(request, user)
    return ok(await run(results.class_report, user["id"], cls))


async def test_results_view(request: web.Request) -> web.Response:
    """Bitta test natijalari (?class= berilsa — faqat shu sinf o'quvchilari)."""
    user = await auth(request)
    test = await own_test(request, user)
    cls = await _class_of(user, request.query.get("class"))
    data = await run(results.test_results, test, cls["id"] if cls else None)
    return ok({"test": {"id": test["id"], "title": test["title"], "code": test["code"], "fmt": test["fmt"],
                        "format": format_title(test["fmt"], test["count"]), "mode": test.get("mode") or "online",
                        "date": (test.get("created_at") or "")[:10], "closed": bool(test["closed"])},
               "class": {"id": cls["id"], "name": cls["name"]} if cls else None, **data})


async def send_certificates(request: web.Request) -> web.Response:
    """🎓 Mock sertifikatlar (PDF) bot chatiga: butun test, bitta sinf yoki tanlangan o'quvchi(lar)."""
    user = await auth(request)
    test = await own_test(request, user)
    if test["fmt"] != "milliy":
        raise ApiError(400, "Sertifikat Milliy sertifikat formatidagi test uchun chiqariladi")
    payload = await body(request)
    cls = await _class_of(user, payload.get("class"))
    chosen = {int(x) for x in payload.get("attempt_ids") or [] if str(x).isdigit()}
    data = await run(results.test_results, test, cls["id"] if cls else None)
    rows = [r for r in data["rows"] if (not chosen or r["attempt_id"] in chosen) and r["score"] is not None]
    if not rows:
        raise ApiError(400, "Sertifikat uchun natija topilmadi (esse bahosi kiritilmagan bo'lishi mumkin)")
    content = await run(pdfs.certificates_pdf, test, rows, user.get("name") or "")
    single = len(rows) == 1
    name = f"sertifikat_{_file_slug(rows[0]['name']) if single else test['code']}.pdf"
    who = html.escape(rows[0]["name"]) if single else f"{len(rows)} ta o'quvchi" + (f" · {html.escape(cls['name'])}" if cls else "")
    sent = await deliver(HOOKS["send_file"](user["id"], content, name,
                             f"🎓 <b>Mock sertifikat</b> · {html.escape(test['title'])}\n{who}\n"
                             "Chop etish uchun — A4, yotiq. Rasmiy sertifikat emas."), "Sertifikat")
    return ok({"ok": True, "count": len(rows), "pending": not sent})


async def send_class_report(request: web.Request) -> web.Response:
    """📄 Sinf jadvali (PDF): o'quvchilar × barcha testlar, o'rtacha ball va daraja."""
    user = await auth(request)
    cls = await own_class(request, user)
    report = await run(results.class_report, user["id"], cls)
    if not report["tests"]:
        raise ApiError(400, "Bu sinf hali titulli testda qatnashmagan")
    content = await run(pdfs.class_report_pdf, report)
    sent = await deliver(HOOKS["send_file"](user["id"], content, f"sinf_{_file_slug(cls['name'])}.pdf",
                             f"📄 <b>{html.escape(cls['name'])}</b> — natijalar jadvali\n"
                             f"{report['class']['students']} ta o'quvchi · {len(report['tests'])} ta test"),
                         "Sinf jadvali")
    return ok({"ok": True, "pending": not sent})


async def students_from_class(request: web.Request) -> web.Response:
    """Titulli testga sinf o'quvchilari — doimiy ID'lari bilan (testda bor ID'lar o'tkazib yuboriladi)."""
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    cls = await run(db.get_class, to_int(payload.get("class_id"), 0, "Sinf"))
    if not cls or cls["owner_id"] != user["id"]:
        raise ApiError(404, "Sinf topilmadi")
    pupils = await run(db.list_class_students, cls["id"])
    taken = {s["number"] for s in await run(db.list_students, test["id"])}
    fresh = [p for p in pupils if p["number"] not in taken]
    if not fresh:
        raise ApiError(400, "Bu sinf o'quvchilari testda allaqachon bor" if pupils else "Sinfda hali o'quvchi yo'q")
    variants = list(await run(db.get_keys, test["id"]))
    chosen = assign_variants(len(fresh), variants, str(payload.get("mode") or "cycle"), start=len(taken))
    students = await run(db.add_students, test["id"], [p["name"] for p in fresh], chosen, [p["number"] for p in fresh])
    return ok({"students": [student_view(s) for s in students], "added": len(fresh)})


# ---------- Ro'yxatdan o'tish (oflayn «Ona tili» kursi) ----------
# Botda /start → «Ro'yxatdan o'tish» → ilovaning alohida ekrani.
#   • O'qituvchi: ism, familiya, telefon va dars beradigan tuman(lar)ini belgilaydi («joy ochadi»).
#   • O'quvchi: ism, familiya, telefon, yosh va o'z tumanini tanlaydi — tumanda kurs bo'lsa ko'rinadi.
# Ariza shu tumandagi o'qituvchilarga va adminlarga boradi; keyingi muloqot ular orasida davom etadi.

MIN_AGE, MAX_AGE = 5, 99
MAX_PLACES = 20  # bitta o'qituvchi nechta tumanda dars bera oladi


def registration_view(reg: dict | None, places: list[dict] | None = None) -> dict | None:
    if not reg:
        return None
    view = {"role": reg.get("role") or "student", "first_name": reg.get("first_name") or "",
            "last_name": reg.get("last_name") or "", "full_name": reg["full_name"], "phone": reg["phone"],
            "age": reg["age"], "region": reg.get("region") or "", "district": reg.get("district") or "",
            "created_at": reg["created_at"]}
    if places is not None:
        view["places"] = places
    return view


async def registration_get(request: web.Request) -> web.Response:
    """Ism Telegram profilidan olinmaydi — foydalanuvchi o'zi kiritadi.

    Ilovaga manzillar ro'yxati va qaysi tumanlarda kurs borligi ham beriladi.
    """
    user = await auth(request)
    reg = await run(db.get_registration, user["id"])
    places = await run(db.list_teacher_places, user["id"]) if reg and reg.get("role") == "teacher" else None
    return ok({
        "registration": registration_view(reg, places),
        "regions": places_data.REGIONS,
        "with_courses": await run(db.districts_with_teachers),
    })


def clean_places(raw, single: bool = False) -> list[tuple[str, str]]:
    """Ilovadan kelgan joylar: faqat ma'lumotnomadagi viloyat/tumanlar qoladi (takrorsiz)."""
    out: list[tuple[str, str]] = []
    for item in (raw or [])[:MAX_PLACES]:
        if not isinstance(item, dict):
            continue
        region, district = clean_text(item.get("region"), 60), clean_text(item.get("district"), 60)
        if places_data.valid(region, district) and (region, district) not in out:
            out.append((region, district))
    return out[:1] if single else out


async def registration_save(request: web.Request) -> web.Response:
    user = await auth(request)
    payload = await body(request)
    role = "teacher" if payload.get("role") == "teacher" else "student"
    first_name = clean_text(payload.get("first_name"), 40)
    last_name = clean_text(payload.get("last_name"), 40)
    phone = clean_text(payload.get("phone"), 30)
    places = clean_places(payload.get("places"), single=role == "student")
    try:
        age = int(str(payload.get("age") or "").strip() or 0)
    except ValueError:
        age = 0
    if len(first_name) < 2:
        raise ApiError(400, "Ismingizni yozing")
    if len(last_name) < 2:
        raise ApiError(400, "Familiyangizni yozing")
    if len(phone) < 7:
        raise ApiError(400, "Telefon raqamingizni yozing")
    if role == "student" and not MIN_AGE <= age <= MAX_AGE:
        raise ApiError(400, f"Yoshingizni yozing ({MIN_AGE}–{MAX_AGE})")
    if not places:
        raise ApiError(400, "Dars beradigan tumanni tanlang" if role == "teacher"
                            else "Yashash joyingizni (viloyat va tuman) tanlang")
    region, district = places[0]
    await run(db.remember_user, user["id"], user["name"], user["username"])
    again = bool(await run(db.get_registration, user["id"]))
    reg = await run(db.save_registration, user["id"], role, first_name, last_name, phone,
                    age if role == "student" else None, region, district)
    saved = await run(db.set_teacher_places, user["id"], places) if role == "teacher" else []
    teachers = [] if role == "teacher" else await run(db.teachers_in, region, district)
    if "registration_notice" in HOOKS:
        try:
            await HOOKS["registration_notice"](reg, user.get("username"), again, teachers)
        except Exception:
            logger.warning("Ro'yxat haqida xabar yuborilmadi", exc_info=True)
    return ok({"registration": registration_view(reg, saved if role == "teacher" else None),
               "again": again, "teachers": len(teachers)})


async def sheets(request: web.Request) -> web.Response:
    """Titullar PDF'i chatga: har o'quvchiga shaxsiy titul + bo'sh titullar."""
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    blanks = max(0, min(200, to_int(payload.get("blanks", 1), 0, "Bo'sh titullar soni")))  # bo'sh titul — printerda ko'paytiriladi
    if payload.get("only") is None and await run(db.list_students, test["id"]):
        blanks = 0  # ro'yxat bor — faqat shaxsiy titullar, qo'shimcha bo'sh titulsiz
    style = payload.get("style")
    if style is not None:
        if style not in sheet_styles.STYLES:
            raise ApiError(400, "Titul ko'rinishi noto'g'ri")
        if test["fmt"] == "milliy" and style not in sheet_styles.MILLIY_STYLES:  # yozma kataklar shu ikkisida
            raise ApiError(400, "Milliy sertifikat testi uchun imtihon varaqasi yoki standart titul")
        if test["fmt"] != "milliy" and style == "official":
            raise ApiError(400, "Imtihon varaqasi faqat Milliy sertifikat testi uchun")
        try:  # savollar shu ko'rinishga sig'adimi
            sheet.layout({**test, "sheet_style": style}, list(await run(db.get_keys, test["id"])))
        except ValueError as error:
            raise ApiError(400, str(error))
        await run(db.set_sheet_style, test["id"], style)
        test = {**test, "sheet_style": style}
    students = await run(db.list_students, test["id"])
    if payload.get("only"):
        only = payload["only"] if isinstance(payload["only"], list) else []
        wanted = {to_int(x, 0, "O'quvchi") for x in only}
        students = [s for s in students if s["id"] in wanted]
    if not students and not blanks:
        raise ApiError(400, "Titul tanlanmadi")
    try:
        sent = await deliver(HOOKS["send_sheets"](user["id"], test, students, blanks), f"Titullar, test {test['id']}")
    except Exception as exc:
        logger.exception("Titullar yuborilmadi: test %s", test["id"])
        raise ApiError(502, send_error_text(exc))
    await run(db.mark_sheets_sent, test["id"])
    return ok({"ok": True, "pages": len(students) + blanks, "pending": not sent})


async def read_photo(request: web.Request) -> bytes:
    try:
        form = await request.post()
    except Exception:
        raise ApiError(400, "Rasm yuborilmadi")
    field = form.get("photo")
    if field is None or not hasattr(field, "file"):
        raise ApiError(400, "Rasm yuborilmadi")
    data = field.file.read()
    if len(data) > 12 * 1024 * 1024:
        raise ApiError(400, "Rasm juda katta (12 MB dan kichik bo'lsin)")
    return data


def _png_data_url(png: bytes | None) -> str | None:
    return "data:image/png;base64," + base64.b64encode(png).decode() if png else None


async def scan_layout(request: web.Request) -> web.Response:
    """Titul tuzilishi (doirachalar, chet belgilari joyi) — jonli skaner kadrlarni telefonning o'zida o'qiydi."""
    user = await auth(request)
    test = await own_test(request, user)
    all_keys = await run(db.get_keys, test["id"])
    variants = list(all_keys)
    spec = sheet.layout(test, variants)
    choice = {item["label"] for item in test["items"] if item["kind"] == "choice"}
    # written_area / name_area — telefon tekislangan varaqdan shu qismlarni kesib oladi (oflayn skaner).
    # keys — test savollari kaliti: ushlangan titul ustida to'g'ri (yashil) / xato (qizil) belgilash uchun
    return ok({"bubbles": spec["bubbles"], "variants": variants, "sheet_page": spec["page"],
               "keys": {v: {k: a for k, a in key.items() if k in choice} for v, key in all_keys.items()},
               "style": sheet_styles.style_of(test), "written_area": spec.get("written_area"),
               "name_area": spec.get("name_area"), "written_labels": [i["label"] for i in text_items(test["items"])],
               "pages": spec.get("pages", 1), "ai": essay_ai.enabled()})


async def scan(request: web.Request) -> web.Response:
    """
    Titul surati → o'qilgan javoblar (TAKLIF: o'qituvchi tekshirib saqlaydi).

    ?live=1 — jonli kamera kadri (har soniyada): titul topilmasa xato emas, maslahat qaytadi;
    yozma javoblar AI'da o'qilmaydi (pullik va sekin) — titul topilgach ilova shu kadrni oddiy rejimda yuboradi.
    """
    user = await auth(request)
    test = await own_test(request, user)
    live = request.query.get("live") == "1"
    data = await read_photo(request)
    variants = list(await run(db.get_keys, test["id"]))
    spec = sheet.layout(test, variants)

    def work():
        import cv2
        import omr
        page = omr.SheetPage(**spec["page"]) if spec["page"] else omr.SheetPage(name_area=spec["name_area"])
        prepared = omr.PreparedSheet(data, spec["bubbles"], 1, page)
        result = omr.scan_answer_sheet(data, spec["bubbles"], 1, prepared, page)
        result["page"] = 1
        if spec.get("pages", 1) > 1:  # ikki varaqli titul: qaysi varaq ekani sahifa belgisidan
            result["page"] = omr.page_of(result["page_marks"]) or 1
            if result["page"] != 1:
                result = {**omr.scan_answer_sheet(data, spec["bubbles"], result["page"], prepared, page), "page": result["page"]}
        written_png = None
        # Yozma kataklar — faqat ular turgan varaqda (ikki varaqli titulda 2-varaq)
        if spec["written_area"] and (spec.get("pages", 1) == 1 or result["page"] == 2):
            x1, y1, x2, y2 = (round(v * omr.SCALE) for v in spec["written_area"])
            crop = prepared.warped[max(0, y1): y2, max(0, x1): x2]
            ok_, png = cv2.imencode(".png", crop)
            written_png = png.tobytes() if ok_ else None
        width, height = prepared.size
        corners = [[round(float(x) / width, 4), round(float(y) / height, 4)] for x, y in prepared.corners]
        return result, written_png, corners

    try:
        result, written_png, corners = await run(work)
    except Exception as error:
        message = str(error) if error.__class__.__name__ == "ScanError" else "Rasmni o'qib bo'lmadi — qaytadan suratga oling"
        if error.__class__.__name__ != "ScanError":
            logger.exception("Skaner xatosi")
        if live:
            return ok({"found": False, "hint": message})
        raise ApiError(422, message)

    # O'quvchi: avval titul pastidagi kod (shaxsiy titul), bo'lmasa o'quvchi bo'yagan ID doirachalari
    number = sheet.decode_number(result["sheet_code"])
    id_source = "code" if number else None
    if not number and result.get("sheet_number_status") == "ok":
        number, id_source = result["sheet_number"], "bubbles"
    student = await run(db.get_student, test["id"], None, number) if number else None
    pupil = (await pupils_of(test["owner_id"])).get(number) if number and not student else None
    if number and not student and not pupil:
        id_source = "unknown"  # ID o'qildi, lekin na test ro'yxatida, na sinflarda bunday o'quvchi yo'q
    variant = result["variant"] or (student["variant"] if student else None) or (variants[0] if len(variants) == 1 else None)
    written, ai_note = {}, None
    labels = [i["label"] for i in text_items(test["items"])]
    if labels and written_png and not live:
        if await written_ai(test["owner_id"]):
            try:
                written, _cost = await essay_ai.read_written(written_png, labels)
            except essay_ai.AIError as error:
                ai_note = f"AI yozma javoblarni o'qiy olmadi: {error}"
        else:
            ai_note = "Yozma javoblarni keyin «✍️ Yozmalarni tekshirish» bo'limida rasmga qarab belgilaysiz"
    return ok({
        "student": student_view(student) if student else None,
        "pupil": pupil,  # sinf ro'yxatidan topilgan o'quvchi (saqlanganda test ro'yxatiga o'zi qo'shiladi)
        "code_status": "ok" if student else ("blank" if result["sheet_code"] == 0 else "unclear"),
        "id_source": id_source, "id_read": number,
        "id_status": result.get("sheet_number_status"),
        "variant": variant, "variant_read": result["variant"],
        "questions": [{"label": q["label"], "chosen": q["chosen"], "status": q["status"]} for q in result["questions"]],
        "written": written, "written_image": _png_data_url(written_png), "name_image": result["name_image"],
        "ai_note": ai_note, "found": True, "corners": corners, "page": result.get("page", 1),
        "pages": spec.get("pages", 1),
    })


async def save_scanned(test: dict, payload: dict) -> tuple[dict, dict]:
    """Skanerlangan titul natijasini saqlaydi → (attempt, student). Qayta skanerlansa — eski natija almashtiriladi."""
    keys = await run(db.get_keys, test["id"])
    variant = payload.get("variant")
    if variant not in keys:
        raise ApiError(400, "Variantni tanlang")
    student, carried = None, None
    if payload.get("student_id"):
        student = await run(db.get_student, test["id"], to_int(payload["student_id"], 0, "O'quvchi"))
        if not student:
            raise ApiError(404, "O'quvchi topilmadi")
        if student["attempt_id"]:  # qayta skanerlash — eski natija almashtiriladi
            carried = await drop_old_attempt(test, student)
        if student["variant"] != variant:
            await run(db.update_student, test["id"], student["id"], None, variant)
    else:
        raw = str(payload.get("number") or "")
        number = int(raw) if raw.isdigit() and 0 < int(raw) <= 9999 else None
        typed = " ".join(str(payload.get("name") or "").split())[:60]
        student = await run(db.get_student, test["id"], None, number) if number else None
        if student and typed and not same_name(typed, student["name"]):
            student = None  # boshqa bola shu ID'ni xato bo'yagan — natijasi ustiga yozilmasin, alohida qo'shiladi
        if student:  # bu ID test ro'yxatida bor — o'sha o'quvchi (ikkinchi nusxa ochilmaydi)
            if student["attempt_id"]:
                carried = await drop_old_attempt(test, student)
            if student["variant"] != variant:
                await run(db.update_student, test["id"], student["id"], None, variant)
        else:
            # ID o'qituvchining sinfida bo'lsa — ism o'sha yerdan (natija o'quvchining o'z sinfiga tushadi)
            pupil = (await pupils_of(test["owner_id"])).get(number) if number else None
            name = " ".join(str(payload.get("name") or "").split())[:60] or (pupil["name"] if pupil else "")
            if len(name) < 3:
                raise ApiError(400, "O'quvchi ismini yozing yoki ro'yxatdan tanlang")
            # Titulda bo'yalgan ID ro'yxatda yo'q — o'quvchi shu ID bilan qo'shiladi (esse ID bo'yicha topiladi)
            before = {s["id"] for s in await run(db.list_students, test["id"])}
            students = await run(db.add_students, test["id"], [name], [variant], [number] if number else None)
            student = next(s for s in students if s["id"] not in before)
    answers = clean_answers(test, payload.get("answers") or {})
    attempt = await HOOKS["record_attempt"](test, db.offline_tg_id(test["id"], student["number"]), student["name"],
                                            variant, answers, None, [], 0.0, notify=False)
    if attempt is None:
        raise ApiError(409, "Bu o'quvchining natijasi allaqachon bor")
    await run(db.link_student_attempt, student["id"], attempt["id"])
    if carried:  # eski natijaning essesi (va o'qituvchi bahosi) — qayta tekshirmasdan
        await run(db.restore_essay, attempt["id"], *carried)
        attempt = await run(db.get_attempt, attempt["id"])
    elif test["fmt"] == "milliy":
        await link_offline_essay(test, student["number"], attempt["id"])
    if two_pages(test):  # 2-varaq avval skanerlangan bo'lsa — yozma kataklar rasmi shu natijaga
        waiting = await run(db.take_written_pending, test["id"], student["number"])
        if waiting:
            await attach_written(test, attempt, waiting["image"], waiting["mime"])
    return attempt, student


async def drop_old_attempt(test: dict, student: dict) -> tuple[dict | None, float | None] | None:
    """Qayta skanerlashdan oldin eski natija o'chiriladi. Qaytaradi: (esse, o'qituvchi bahosi) — yangi natijaga
    o'tkazish uchun (aks holda kiritilgan esse bahosi yo'qolar, botdagi esse kreditdan qayta tekshirilar edi)."""
    old_id = student["attempt_id"]
    old = await run(db.get_attempt, old_id)
    essay = await run(db.get_essay, old_id)
    if essay and essay["status"] == "none":
        essay = None
    score = old["essay"] if old else None
    await keep_written_page(test, student)
    await run(db.delete_attempt, old_id)
    return (essay, score) if essay or score is not None else None


def two_pages(test: dict) -> bool:
    """Imtihon varaqasi (rasmiy titul): 1-varaq — 1–35, 2-varaq — 36–44 alohida skanerlanadi."""
    return sheet_styles.style_of(test) == "official"



async def attach_written(test: dict, attempt: dict, image_b64: str, mime: str) -> bool:
    """Yozma kataklar rasmi natijaga: AI o'qiydi (ruxsat bo'lsa) yoki o'qituvchi «✍️ Yozmalarni tekshirish»da belgilaydi."""
    labels = [i["label"] for i in text_items(test["items"])]
    need_ai = [label for label in labels if not attempt["answers"].get(label)]
    queued = bool(need_ai) and (await paid_written_allowed(test["owner_id"]) if paid_check(test)
                                else await written_ai(test["owner_id"]))
    await run(db.create_written_job, attempt["id"], test["id"], image_b64, mime[:20], "pending" if queued else "manual")
    if queued and "wake_ai" in HOOKS:
        HOOKS["wake_ai"]()
    return queued


async def keep_written_page(test: dict, student: dict) -> None:
    """1-varaq qayta skanerlanadi (eski natija o'chadi): 2-varaq rasmi yo'qolmasin — kutishga qaytadi."""
    if not two_pages(test) or not student.get("attempt_id"):
        return
    job = await run(db.get_written_job, student["attempt_id"])
    if job and job["image"]:
        await run(db.save_written_pending, test["id"], student["number"], job["image"], job["mime"])


async def save_written_page(test: dict, payload: dict, image_b64: str, mime: str) -> dict:
    """2-varaq (yozma javoblar): o'quvchi ID'si (yoki ro'yxatdagi o'quvchi) bo'yicha. 1-varaq saqlangan bo'lsa —
    darhol natijaga, aks holda 1-varaq kelguncha kutadi."""
    student = None
    if payload.get("student_id"):
        student = await run(db.get_student, test["id"], to_int(payload["student_id"], 0, "O'quvchi"))
    raw = str(payload.get("number") or "")
    number = student["number"] if student else (int(raw) if raw.isdigit() and 0 < int(raw) <= 9999 else None)
    if number is None:
        raise ApiError(400, "2-varaqda o'quvchi ID'si o'qilmadi — ID'ni bo'yab, qayta skanerlang")
    student = student or await run(db.get_student, test["id"], None, number)
    pupil = (await pupils_of(test["owner_id"])).get(number)
    name = (student or {}).get("name") or (pupil or {}).get("name") or f"ID {number:04d}"
    attempt = await run(db.get_attempt, student["attempt_id"]) if student and student["attempt_id"] else None
    if attempt:
        queued = await attach_written(test, attempt, image_b64, mime)
        return {"ok": True, "page": 2, "attached": True, "written_queued": queued, "name": name, "number": number,
                "attempt_id": attempt["id"]}
    await run(db.save_written_pending, test["id"], number, image_b64, mime[:20])
    return {"ok": True, "page": 2, "attached": False, "written_queued": False, "name": name, "number": number}


async def link_offline_essay(test: dict, number: int, attempt_id: int) -> bool:
    """O'quvchi botda yozgan esse (ID bo'yicha) — titul natijasiga."""
    essay = await run(db.get_offline_essay, test["id"], number)
    if not essay:
        return False
    charge = await _teacher_charge(test["owner_id"]) if essay_ai.essay_enabled() else None
    await run(db.set_essay_text, attempt_id, essay["text"], "pending" if charge else "manual")
    await run(db.set_essay_charge, attempt_id, charge if charge == "teacher" else None)
    return True


ESSAY_MIN_WORDS, ESSAY_MAX_CHARS = 30, 20000


async def offline_essay_submit(request: web.Request) -> web.Response:
    """
    Titulli testning 45-essesi: o'quvchi titulda bo'yagan ID'si bilan botda yozadi. Titul oldin yoki keyin
    skanerlanishidan qat'i nazar, natijaga ID bo'yicha biriktiriladi. Bitta akkaunt — bitta ID.
    """
    user = await auth(request)
    payload = await body(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test or (test.get("mode") or "online") != "offline" or test["fmt"] != "milliy":
        raise ApiError(404, "Bunday kodli titulli test topilmadi")
    if essay_on_paper(test):
        raise ApiError(409, "Bu testda esse qog'ozga (javoblar varaqasining 3–4-varag'iga) yoziladi")
    if test["closed"]:
        raise ApiError(409, "Natijalar e'lon qilingan — esse qabul qilinmaydi")
    await test_gate(test, user)
    raw = str(payload.get("number") or "").strip()
    if not raw.isdigit() or not 0 < int(raw) <= 9999:
        raise ApiError(400, "ID'ni 4 ta raqam bilan yozing (titulda bo'yaganingizdek, masalan 0007)")
    number = int(raw)
    text = str(payload.get("essay") or "").strip()
    if len(text.split()) < ESSAY_MIN_WORDS:
        raise ApiError(400, f"Esse juda qisqa — kamida {ESSAY_MIN_WORDS} so'z yozing")
    if len(text) > ESSAY_MAX_CHARS:
        raise ApiError(400, "Esse juda uzun")
    # Ism majburiy: o'qituvchi uni ro'yxatdagi ism bilan solishtiradi (begona ID bilan yuborilsa — ko'rinadi)
    name = " ".join(str(payload.get("name") or "").split())[:60]
    if len(name) < 3:
        raise ApiError(400, "Ism va familiyangizni to'liq yozing")
    # Esse kodi (shaxsiy titulda ID yonida): to'g'ri bo'lsa — esse shu ID egasiniki deb tasdiqlanadi
    pin = "".join(ch for ch in str(payload.get("pin") or "") if ch.isdigit())
    if pin and pin != sheet.essay_pin(test["id"], number):
        raise ApiError(400, "Esse kodi noto'g'ri — titulingizda ID yonida yozilgan 4 ta raqam")
    verified = bool(pin)
    await run(db.remember_user, user["id"], user["name"], user.get("username"))  # o'qituvchi kim yuborganini ko'radi
    mine = await run(db.offline_essay_of_user, test["id"], user["id"])
    if mine and mine["number"] != number:
        raise ApiError(409, f"Siz bu testga ID {mine['number']:04d} bilan esse yuborgansiz — ID'ni o'zgartirib bo'lmaydi")
    existing = await run(db.get_offline_essay, test["id"], number)
    takeover = False
    if existing and existing["tg_id"] != user["id"]:
        if verified and not existing.get("verified"):
            takeover = True  # kodsiz yuborgan begona akkaunt — egasi kod bilan o'z ID'sini qaytarib oladi
        else:
            raise ApiError(409, f"ID {number:04d} bilan esse boshqa akkauntdan yuborilgan — "
                                + ("titulingizdagi esse kodini ham yozing" if not verified else "o'qituvchingizga ayting"))
    student = await run(db.get_student, test["id"], None, number)
    attempt = await run(db.get_attempt, student["attempt_id"]) if student and student["attempt_id"] else None
    if attempt and attempt["essay"] is not None:
        raise ApiError(409, "Essengiz allaqachon baholangan — o'zgartirib bo'lmaydi")
    await run(db.save_offline_essay, test["id"], number, user["id"], name, text, verified)
    if takeover and "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](existing["tg_id"], f"⚠️ <b>{html.escape(test['title'])}</b>: ID {number:04d} bilan yuborgan "
                                     "essengiz bekor qilindi — bu ID egasi esse kodi bilan yubordi. ID'ingizni tekshiring.")
        except Exception:
            logger.warning("Esse egasi o'zgargani haqida xabar yuborilmadi", exc_info=True)
    linked = bool(attempt) and await link_offline_essay(test, number, attempt["id"])
    return ok({"ok": True, "number": number, "words": len(text.split()), "linked": linked,
               "name": student["name"] if student else name})


_APOSTROPHES = str.maketrans("", "", "'`ʻʼ‘’")


def names_match(given: str | None, roster: str | None) -> bool | None:
    """Botda yozilgan ism ro'yxatdagiga mosmi: kamida bitta so'z (3+ harf) bir xil. None — solishtirib bo'lmaydi.
    Faqat ogohlantirish uchun (o'quvchi kirillda yozgan bo'lishi mumkin) — esse rad etilmaydi."""
    words = lambda text: {w for w in str(text or "").lower().translate(_APOSTROPHES).split() if len(w) >= 3}
    a, b = words(given), words(roster)
    if not a or not b:
        return None
    return bool(a & b)


def same_name(a: str | None, b: str | None) -> bool:
    """Bir xil odammi: so'zlar to'plami bir xil (tartib, katta-kichik harf, tutuq belgisi farq qilmaydi)."""
    words = lambda text: sorted(str(text or "").lower().translate(_APOSTROPHES).split())
    return words(a) == words(b)


def offline_essay_view(row: dict) -> dict:
    return {"number": row["number"], "name": row["name"], "roster_name": row["roster_name"],
            "name_ok": names_match(row["name"], row["roster_name"]),
            "account": row["tg_name"] or "Foydalanuvchi", "username": row["username"],
            "words": len((row["text"] or "").split()), "created_at": row["created_at"],
            "linked": bool(row["attempt_id"]), "score": row["score"], "verified": bool(row.get("verified"))}


async def offline_essay_delete(request: web.Request) -> web.Response:
    """O'qituvchi botda yozilgan esseni bekor qiladi (begona akkaunt boshqa o'quvchi ID'sini egallagan yoki
    o'quvchi ID'ni xato yozgan): ID bo'shaydi, haqiqiy egasi qayta yuboradi. Yuborgan akkauntga xabar boradi."""
    user = await auth(request)
    test = await own_test(request, user)
    number = int(request.match_info["number"])
    sender = await run(db.delete_offline_essay, test["id"], number)
    if sender is None:
        raise ApiError(404, "Bu ID bilan esse topilmadi")
    if "send_text" in HOOKS:
        try:
            await HOOKS["send_text"](
                sender, f"⚠️ <b>{html.escape(test['title'])}</b>: ID {number:04d} bilan yuborgan essengizni o'qituvchi "
                        "bekor qildi.\nTituldagi ID'ingizni tekshirib, esseni to'g'ri ID bilan qayta yuboring."
                        if not test["closed"] else
                        f"⚠️ <b>{html.escape(test['title'])}</b>: ID {number:04d} bilan yuborgan essengizni o'qituvchi "
                        "bekor qildi.")
        except Exception:
            logger.warning("Esse bekor qilingani haqida xabar yuborilmadi", exc_info=True)
    essays = await run(db.list_offline_essays, test["id"])
    return ok({"ok": True, "offline_essays": [offline_essay_view(e) for e in essays]})


async def store_written_image(test: dict, attempt: dict, image) -> None:
    """Skanerda kesilgan yozma kataklar rasmi (data URL) — «✍️ Yozmalarni tekshirish» uchun saqlanadi."""
    image = str(image or "")
    if not text_items(test["items"]) or not image.startswith("data:image/") or ";base64," not in image:
        return
    head, data = image.split(",", 1)
    if len(data) > 8_000_000:
        return
    try:
        base64.b64decode(data, validate=True)
    except ValueError:
        return
    # Skaner yozma javoblarni o'qimagan bo'lsa (pullik tekshiruv yoki AI xato bergan) — AI navbatiga, aks holda qo'lda
    await attach_written(test, attempt, data, head[5:head.index(";")])


async def scan_save(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    if str(payload.get("page") or "1") == "2":  # ikki varaqli titulning 2-varag'i
        image = str(payload.get("written_image") or "")
        if not image.startswith("data:image/") or ";base64," not in image:
            raise ApiError(400, "2-varaq rasmi yuborilmadi")
        head, data = image.split(",", 1)
        return ok(await save_written_page(test, payload, data, head[5:head.index(";")][:20]))
    attempt, student = await save_scanned(test, payload)
    await store_written_image(test, attempt, payload.get("written_image"))
    if from_app(request) and "scan_notice" in HOOKS:  # APK'dan skanerlandi — natija bot chatiga ham (orqada)
        in_background(HOOKS["scan_notice"](user["id"], test, student["name"],
                                           None if essay_pending(test, attempt) else total_points(attempt),
                                           max_points(test["items"], test["fmt"])), "Skaner xabari")
    hidden = essay_pending(test, attempt)  # qog'ozdagi esse bahosi kiritilmaguncha natija chiqmaydi
    return ok({"ok": True, "attempt_id": attempt["id"], "name": student["name"], "essay_pending": hidden,
               "points": None if hidden else total_points(attempt), "max_points": max_points(test["items"], test["fmt"]),
               "correct": attempt["test_correct"] + attempt["written_correct"], "auto_total": attempt["auto_total"]})


async def attach_essay(user: dict, test: dict, attempt: dict, pages: list[bytes]) -> str:
    """Esse varaqasi suratlari: bot orqali Telegram'ga saqlanadi va AI navbatiga qo'yiladi → esse holati."""
    file_ids = []
    for index, data in enumerate(pages, 1):
        page = f" · {index}-bet" if len(pages) > 1 else ""
        file_ids.append(await HOOKS["store_photo"](user["id"], data,
                                                   f"📝 Esse · {attempt['student_name']}{page} · {test['title']}"))
    charge = await _teacher_charge(test["owner_id"]) if essay_ai.essay_enabled() else None
    status = "pending" if charge else "manual"
    if not await run(db.get_essay, attempt["id"]):
        await run(db.create_essay, attempt["id"], None, file_ids, status)
    else:
        await run(db.set_essay_photos, attempt["id"], file_ids, status)
    await run(db.set_essay_charge, attempt["id"], charge if charge == "teacher" else None)
    return status


async def essay_photo(request: web.Request) -> web.Response:
    """Oflayn esse varaqasi surati: bot orqali Telegram'ga saqlanadi va AI navbatiga qo'yiladi."""
    user = await auth(request)
    attempt, test = await _own_attempt(request, user)
    data = await read_photo(request)
    return ok({"ok": True, "status": await attach_essay(user, test, attempt, [data])})


# ---------- Oflayn skaner: telefonda saqlangan titullar bittada yuboriladi ----------

# Telefon bir titulni ikki marta yubormasin (tarmoq uzilib, javob kelmay qolsa): uid → javob
_UPLOADED: dict[str, dict] = {}
_STUDENT_OF: dict[str, int] = {}
UPLOAD_PART_MAX = 6 * 1024 * 1024

# Titul suratlari: telefon tayyorlagan kichik JPEG'lar xotirada yig'iladi va 10 tadan albom bo'lib botga
# uzatiladi (server rasmni ochmaydi ham — protsessorga yuk yo'q). Qolgani «yuborish tugadi»da jo'natiladi
SHEET_PHOTO_MAX = 1_500_000
_SHEET_PHOTOS: dict[tuple[int, int], list[tuple[bytes, str]]] = {}


def queue_sheet_photos(user_id: int, test: dict, photos: list[bytes], caption: str, attempt_id: int | None = None) -> None:
    """attempt_id — birinchi surat (1-varaq) shu natijaga bog'lanadi: ilovada titul rasmi belgilar bilan ko'rinadi."""
    items = _SHEET_PHOTOS.setdefault((user_id, test["id"]), [])
    for index, photo in enumerate(photos):
        if photo[:2] == b"\xff\xd8" and len(photo) <= SHEET_PHOTO_MAX:  # faqat JPEG
            items.append((photo, caption[:900], attempt_id if index == 0 else None))
    if len(items) >= 10:
        flush_sheet_photos(user_id, test)


def flush_sheet_photos(user_id: int, test: dict) -> None:
    items = _SHEET_PHOTOS.pop((user_id, test["id"]), None)
    if items and "scan_photos" in HOOKS:
        in_background(HOOKS["scan_photos"](user_id, test, items), "Titul suratlari")


async def scan_upload(request: web.Request) -> web.Response:
    """
    Telefonda skanerlangan bitta titul: data — JSON (uid, student_id yoki name, variant, answers),
    written — yozma kataklar rasmi (AI fonda o'qiydi), essay — esse varaqasi suratlari (bir nechta bo'lishi mumkin).
    """
    user = await auth(request)
    test = await own_test(request, user)
    try:
        form = await request.post()
        payload = json.loads(form.get("data") or "{}")
    except Exception:
        raise ApiError(400, "Ma'lumot noto'g'ri yuborildi")
    if not isinstance(payload, dict):
        raise ApiError(400, "Ma'lumot noto'g'ri yuborildi")
    uid = str(payload.get("uid") or "")[:64]
    if uid and uid in _UPLOADED:
        return ok(_UPLOADED[uid])
    previous = await run(db.get_upload, uid, test["id"]) if uid else None  # server qayta ishga tushgan bo'lsa ham
    if previous and previous["response"]:
        return ok(previous["response"])
    if uid in _STUDENT_OF or (previous and previous["student_id"]):
        # oldingi urinishda o'quvchi yaratilgan, keyin xato bo'lgan — ikkinchi nusxa ochilmasin
        payload = {**payload, "student_id": _STUDENT_OF.get(uid) or previous["student_id"], "name": None}

    def part(field) -> bytes | None:
        if field is None or not hasattr(field, "file"):
            return None
        data = field.file.read()
        if len(data) > UPLOAD_PART_MAX:
            raise ApiError(400, "Rasm juda katta")
        return data or None

    written = part(form.get("written"))
    essays = [data for data in (part(f) for f in form.getall("essay", [])) if data]
    photos = [data for data in (part(f) for f in form.getall("photo", [])) if data]
    if str(payload.get("page") or "1") == "2":  # ikki varaqli titulning 2-varag'i — faqat yozma kataklar rasmi
        if not written:
            raise ApiError(400, "2-varaq rasmi yuborilmadi")
        response = await save_written_page(test, payload, base64.b64encode(written).decode(),
                                           str(payload.get("written_mime") or "image/jpeg"))
        if photos:
            queue_sheet_photos(user["id"], test, photos, f"2-varaq · ID {to_int(payload.get('number'))}")
        if uid:
            _UPLOADED[uid] = response
        return ok(response)
    attempt, student = await save_scanned(test, payload)
    if uid:
        _STUDENT_OF[uid] = student["id"]
        await run(db.save_upload, uid, test["id"], student["id"])
    queued = False
    if written and text_items(test["items"]):
        # AI o'qiydi (bepul ro'yxat yoki «💎 pullik tekshiruv» — kreditdan) yoki o'qituvchi keyin rasmga qarab belgilaydi.
        # Hamma yozma javob kiritilgan bo'lsa — AI kerak emas
        queued = await attach_written(test, attempt, base64.b64encode(written).decode(),
                                      str(payload.get("written_mime") or "image/jpeg"))
    essay_status = await attach_essay(user, test, attempt, essays) if essays else None
    if photos:
        number = student.get("number")
        caption = (f"№ {number:03d} " if isinstance(number, int) else "") + f"{student['name']} · test " \
                  f"{attempt['test_correct']}/{len(choice_items(test['items']))}"
        queue_sheet_photos(user["id"], test, photos, caption, attempt["id"])
        marks = str(form.get("marks") or "")
        if marks.startswith("{") and len(marks) < 200_000:  # doirachalar joyi — belgilarni qayta chizish uchun
            await run(db.save_sheet_marks, attempt["id"], marks)
    response = {"ok": True, "attempt_id": attempt["id"], "student_id": student["id"], "name": student["name"],
                "written_queued": queued, "essay_status": essay_status, "essay_pending": essay_pending(test, attempt),
                "points": None if essay_pending(test, attempt) else total_points(attempt),
                "max_points": max_points(test["items"], test["fmt"])}
    if uid:
        if len(_UPLOADED) > 5000:
            _UPLOADED.clear()
            _STUDENT_OF.clear()
        _UPLOADED[uid] = response
        await run(db.save_upload, uid, test["id"], student["id"], response)
    return ok(response)


async def scan_upload_done(request: web.Request) -> web.Response:
    """Telefondagi titullar yuborib bo'lindi — bot chatiga bitta umumiy xabar."""
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    count = lambda key: max(0, min(10000, to_int(payload.get(key))))
    status = await run(db.written_status, test["id"])
    flush_sheet_photos(user["id"], test)
    if "scan_batch_notice" in HOOKS and count("saved"):
        in_background(HOOKS["scan_batch_notice"](user["id"], test, count("saved"), count("written"), count("essays"),
                                                 count("failed")), "Skaner xabari (to'plam)")
    return ok({"ok": True, "written": status, "ai": essay_ai.enabled()})


# ---------- APK ilova: Telegram orqali kirish ----------

APK_PATH = Path(__file__).resolve().parent / "downloads" / "onatili-skaner.apk"
APP_SCHEME, APP_PACKAGE = "onatiliskaner", "uz.onatili.skaner"


async def set_role(request: web.Request) -> web.Response:
    """O'quvchi / O'qituvchi tanlovi — keyingi kirishda shu ekran ochiladi (har qurilmada)."""
    user = await current_user(request)
    role = (await body(request)).get("role")
    if role not in ("student", "teacher"):
        raise ApiError(400, "Noto'g'ri rol")
    await run(db.remember_user, user["id"], user["name"], user["username"])
    await run(db.set_role, user["id"], role)
    return ok({"ok": True})


def site_url(request: web.Request) -> str:
    from config import WEBAPP_URL
    return WEBAPP_URL or f"{request.scheme}://{request.host}"


async def app_link(request: web.Request) -> web.Response:
    """Mini App'dagi «Skanerlash» → APK: bir martalik kirish kaliti bilan havola (ilovada qayta kirish shart emas)."""
    user = await auth(request)
    test = await own_test(request, user)
    token, session = secrets.token_hex(16), secrets.token_hex(24)
    await run(db.create_handoff, token, user["id"], session)
    return ok({"url": f"{site_url(request)}/open?t={test['id']}&login={token}", "download": f"{site_url(request)}/download",
               "has_apk": APK_PATH.exists()})


async def open_app(request: web.Request) -> web.Response:
    """Brauzerda ochiladi: APK o'rnatilgan bo'lsa — uni shu testning skanerida ochadi, bo'lmasa — yuklab olish."""
    test_id = request.query.get("t", "")
    token = request.query.get("login", "")
    if not test_id.isdigit() or not all(c in "0123456789abcdef" for c in token):
        raise web.HTTPBadRequest(text="Havola noto'g'ri")
    base = site_url(request)
    query = f"t={test_id}&login={token}"
    fallback = f"{base}/download"
    intent = (f"intent://open?{query}#Intent;scheme={APP_SCHEME};package={APP_PACKAGE};"
              f"S.browser_fallback_url={fallback};end")
    html = f"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Skaner ilovasi</title>
<style>body{{font-family:system-ui,sans-serif;background:#f4faf6;color:#1f2933;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{max-width:360px;padding:28px;text-align:center}}a{{display:block;padding:15px;border-radius:14px;margin:12px 0;text-decoration:none;font-weight:700}}
.p{{background:#1d6f42;color:#fff}}.g{{border:2px solid #1d6f42;color:#1d6f42}}p{{color:#5d6b66}}</style></head>
<body><main><div style="font-size:56px">📷</div><h2>Ona tili · Skaner</h2>
<p>Skaner ilovasi ochilmoqda… Ochilmasa, avval ilovani o'rnating.</p>
<a class="p" href="{intent}">📲 Ilovada ochish</a><a class="g" href="{fallback}">⬇️ Ilovani yuklab olish (APK)</a>
<p style="font-size:13px">O'rnatgach, Telegram'dagi botga qayting va «Skanerlash»ni yana bosing.</p></main>
<script>location.href = {json.dumps(intent)};</script></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def download_apk(_request: web.Request) -> web.StreamResponse:
    if not APK_PATH.exists():
        raise web.HTTPNotFound(text="APK hali yuklanmagan")
    return web.FileResponse(APK_PATH, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Disposition": 'attachment; filename="onatili-skaner.apk"',
    })

async def app_login(_request: web.Request) -> web.Response:
    """Ilova kirish havolasini so'raydi: t.me/<bot>?start=app_<token> — bot bosilganda tasdiqlaydi."""
    token = secrets.token_hex(16)
    await run(db.create_login, token)
    username = await HOOKS["bot_username"]() if "bot_username" in HOOKS else ""
    return ok({"token": token, "url": f"https://t.me/{username}?start=app_{token}", "bot": username})


async def app_login_status(request: web.Request) -> web.Response:
    session = await run(db.take_login, request.match_info["token"])
    if not session:
        return ok({"ready": False})
    user = await run(db.session_user, session)
    return ok({"ready": True, "session": session, "user": user})


async def app_logout(request: web.Request) -> web.Response:
    session = request.headers.get("X-App-Session", "")
    if session:
        await run(db.delete_session, session)
    return ok({"ok": True})


# ---------- Server ----------

async def index(request: web.Request) -> web.Response:
    response = web.FileResponse(STATIC_DIR / "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


async def service_worker(_request: web.Request) -> web.Response:
    """Sahifani telefonda saqlovchi skript (oflayn skaner) — butun sayt uchun, kesh'siz."""
    response = web.FileResponse(STATIC_DIR / "sw.js")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    response.content_type = "application/javascript"
    return response


async def health(request: web.Request) -> web.Response:
    """?db=1 — bazagacha (masalan Neon) bitta so'rov necha ms ketishini ham ko'rsatadi (sekinlikni tekshirish uchun)."""
    if request.query.get("db") != "1":
        return web.Response(text="Bot ishlayapti")
    times = []
    for _ in range(3):
        started = time.perf_counter()
        await run(db.ping)
        times.append(round((time.perf_counter() - started) * 1000))
    return web.Response(text=f"Bot ishlayapti · baza ({db.backend_name()}): {', '.join(map(str, times))} ms")


@web.middleware
async def timing(request: web.Request, handler):
    """Har API javobiga Server-Timing (serverda necha ms ketgani); 1,5 s dan sekinlari logga yoziladi."""
    started = time.perf_counter()
    response = await handler(request)
    ms = round((time.perf_counter() - started) * 1000)
    try:
        response.headers["Server-Timing"] = f"app;dur={ms}"
    except Exception:
        pass
    if ms > 1500 and request.path.startswith("/api/"):
        logger.warning("Sekin so'rov: %s %s — %s ms", request.method, request.path, ms)
    return response


# So'rovlarni cheklash (IP bo'yicha, daqiqasiga). Maktab WiFi'sida 30+ o'quvchi bitta IP'dan kiradi —
# shuning uchun umumiy chegara keng; kirish havolasi va og'ir (rasm) so'rovlar alohida, torroq
RATE_LIMITS = [  # (yo'l boshlanishi, daqiqasiga)
    ("/api/app/login", 30),
    ("/api/teacher/test/", 600),
    ("/api/", 1500),
]
_HITS: dict = {}


def client_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.remote or "?"


@web.middleware
async def rate_limit(request: web.Request, handler):
    path = request.path
    if path.startswith("/api/"):
        minute = int(time.time() // 60)
        ip = client_ip(request)
        for prefix, limit in RATE_LIMITS:
            if path.startswith(prefix) and not (prefix == "/api/app/login" and request.method != "POST"):
                key = (ip, prefix, minute)
                _HITS[key] = _HITS.get(key, 0) + 1
                if _HITS[key] > limit:
                    return web.json_response({"error": "Juda ko'p so'rov — bir daqiqa kutib, qayta urinib ko'ring"}, status=429)
                break
        if len(_HITS) > 20000:  # eski daqiqalar
            for key in [k for k in _HITS if k[2] < minute]:
                _HITS.pop(key, None)
    return await handler(request)


@web.middleware
async def errors(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("API xatosi: %s", request.path)
        return web.json_response({"error": "Serverda xatolik, qaytadan urinib ko'ring"}, status=500)


def create_app() -> web.Application:
    # 21 MB: mock savollari PDF'i 20 MB gacha (Telegram chegarasi); qolgan so'rovlar baribir kichik
    app = web.Application(middlewares=[timing, rate_limit, errors], client_max_size=21 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/app", index)
    app.router.add_get("/health", health)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_static("/static/", STATIC_DIR)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_post("/api/app/login", app_login)
    app.router.add_get("/api/app/login/{token:[0-9a-f]{32}}", app_login_status)
    app.router.add_post("/api/app/logout", app_logout)
    app.router.add_post("/api/role", set_role)
    app.router.add_post("/api/plan/order", plan_order)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/app-link", app_link)
    app.router.add_get("/open", open_app)
    app.router.add_get("/download", download_apk)
    app.router.add_get("/api/test/{code:\\d{5}}", test_info)
    app.router.add_post("/api/test/{code:\\d{5}}/submit", submit)
    app.router.add_post("/api/test/{code:\\d{5}}/ping", ping)
    app.router.add_post("/api/test/{code:\\d{5}}/essay", offline_essay_submit)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/offline-essays/{number:\\d{1,4}}/delete", offline_essay_delete)
    app.router.add_get("/api/result/{attempt_id:\\d+}", result)
    app.router.add_get("/api/my-essay/{attempt_id:\\d+}", my_essay)
    app.router.add_post("/api/teacher/tests", create_test)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}", teacher_test)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/announce", announce)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/reopen", reopen)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}/keys", keys_get)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/keys", keys_save)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/delete", delete_test)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/copy", copy_test)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/channels", set_channels)
    app.router.add_post("/api/teacher/channel", check_channel)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}/live", live)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/send", send_pdf)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/download", download_link)
    app.router.add_get("/dl/{token}", download_file)
    app.router.add_get("/api/teacher/link", link_view)
    app.router.add_get("/api/courses", courses)
    app.router.add_post("/api/courses/join", course_join)
    app.router.add_get("/api/teacher/joins", joins_list)
    app.router.add_get("/api/teacher/join/{request_id:\\d+}/photo", join_photo)
    app.router.add_post("/api/teacher/join/{request_id:\\d+}", join_decide)
    app.router.add_post("/api/test/{code:\\d{5}}/essay-pay", essay_pay_start)
    app.router.add_get("/api/test/{code:\\d{5}}/essay-scan", essay_scan_get)
    app.router.add_post("/api/test/{code:\\d{5}}/essay-scan/start", essay_scan_start)
    app.router.add_post("/api/test/{code:\\d{5}}/essay-scan/photo", essay_scan_upload)
    app.router.add_get("/api/teacher/essay-payments", essay_payments_list)
    app.router.add_get("/api/teacher/essay-payment/{payment_id:\\d+}/receipt", essay_payment_receipt)
    app.router.add_post("/api/teacher/essay-payment/{payment_id:\\d+}", essay_payment_decide)
    app.router.add_post("/api/test/{code:\\d{5}}/wallet-order", wallet_order_for_test)
    app.router.add_post("/api/teacher/packs", packs_save)
    app.router.add_post("/api/teacher/withdraw", withdraw)
    app.router.add_post("/api/admin/withdrawal/{withdrawal_id:\\d+}", withdrawal_admin_decide)
    app.router.add_get("/api/admin/sales", sales_admin)
    app.router.add_post("/api/admin/sales", sales_admin_save)
    app.router.add_get("/api/teacher/settings", teacher_settings)
    app.router.add_post("/api/teacher/settings", teacher_settings_save)
    app.router.add_post("/api/mock/{code:\\d{5}}/signup", mock_signup)
    app.router.add_post("/api/pass/order", pass_order)
    app.router.add_get("/api/teacher/attempt/{attempt_id:\\d+}/sheet", attempt_sheet)
    app.router.add_get("/sp/{token}", sheet_photo)
    app.router.add_post("/api/teacher/attempt/{attempt_id:\\d+}/delete", attempt_delete)
    app.router.add_post("/api/teacher/attempt/{attempt_id:\\d+}/rename", attempt_rename)
    app.router.add_post("/api/teacher/attempt/{attempt_id:\\d+}/move", attempt_move)
    app.router.add_post("/api/test/{code:\\d{5}}/mock-choice", mock_choice)
    app.router.add_get("/mf/{token}", mock_file)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/schedule", schedule_save)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/files", files_upload)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/files/{file_id:\\d+}/delete", file_delete)
    app.router.add_get("/api/teacher/wallet-payments", wallet_payments_list)
    app.router.add_get("/api/teacher/wallet-payment/{payment_id:\\d+}/receipt", wallet_payment_receipt)
    app.router.add_post("/api/teacher/wallet-payment/{payment_id:\\d+}", wallet_payment_decide)
    app.router.add_post("/api/teacher/link", link_add)
    app.router.add_post("/api/teacher/link/remove", link_remove)
    app.router.add_post("/api/teacher/link/limit", link_limit)
    app.router.add_get("/api/practice", practice_home)
    app.router.add_post("/api/practice", practice_submit)
    app.router.add_get("/api/practice/{practice_id:\\d+}", practice_get)
    app.router.add_post("/api/practice/wallet-order", practice_wallet_order)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/theme", set_theme)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/essays/confirm-all", essays_confirm_all)
    app.router.add_get("/api/teacher/essay/{attempt_id:\\d+}", essay_detail)
    app.router.add_post("/api/teacher/essay/{attempt_id:\\d+}", essay_score)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}/students", students_list)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/students", students_add)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/students/{student_id:\\d+}", student_update)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/students/import", students_import)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/students/from-class", students_from_class)
    app.router.add_get("/api/teacher/classes", classes_list)
    app.router.add_post("/api/teacher/classes", class_create)
    app.router.add_get("/api/teacher/class/{class_id:\\d+}", class_detail)
    app.router.add_post("/api/teacher/class/{class_id:\\d+}", class_update)
    app.router.add_post("/api/teacher/class/{class_id:\\d+}/students", class_students_add)
    app.router.add_post("/api/teacher/class/{class_id:\\d+}/students/import", class_students_import)
    app.router.add_post("/api/teacher/class/{class_id:\\d+}/students/{student_id:\\d+}", class_student_update)
    app.router.add_post("/api/teacher/class/{class_id:\\d+}/send", class_send_ids)
    app.router.add_get("/api/register", registration_get)
    app.router.add_post("/api/register", registration_save)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/sheets", sheets)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/scan", scan)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}/scan/layout", scan_layout)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/scan/save", scan_save)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/scan/upload", scan_upload)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/scan/upload/done", scan_upload_done)
    app.router.add_post("/api/teacher/attempt/{attempt_id:\\d+}/essay-photo", essay_photo)
    app.router.add_get("/api/teacher/attempt/{attempt_id:\\d+}/answers", attempt_answers)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}/written", written_review)
    app.router.add_get("/api/teacher/attempt/{attempt_id:\\d+}/written-image", written_image)
    app.router.add_post("/api/teacher/attempt/{attempt_id:\\d+}/mark", written_mark)
    app.router.add_post("/api/teacher/attempt/{attempt_id:\\d+}/answers", attempt_answers_save)
    # «📊 Natijalar» bo'limi
    app.router.add_get("/api/teacher/results", results_overview)
    app.router.add_get("/api/teacher/class/{class_id:\\d+}/results", class_results)
    app.router.add_post("/api/teacher/class/{class_id:\\d+}/report", send_class_report)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}/results", test_results_view)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/certificates", send_certificates)
    return app


async def start(port: int) -> web.AppRunner:
    runner = web.AppRunner(create_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Mini App serveri %s-portda ochildi", port)
    return runner
