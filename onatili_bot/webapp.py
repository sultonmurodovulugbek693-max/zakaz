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
import random
import secrets
import logging
import time
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
from config import ADMIN_CONTACT, ADMIN_IDS, BOT_TOKEN, SALES_ENABLED
from scoring import rasch_breakdown, rasch_results, total_points
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
    attempts = await run(db.attempts_of_user, user["id"])
    tests = await run(db.list_tests, user["id"], 50)
    all_totals = await run(db.owner_totals, user["id"]) if tests else {}
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
        "role": await run(db.get_role, user["id"]),
        "last_name": await run(db.last_name_used, user["id"]),
        "attempts": [{
            "id": a["id"], "code": a["code"], "title": a["title"], "fmt": a["fmt"], "variant": a["variant"],
            "announced": bool(a["closed"]), "created_at": a["created_at"], "theme": a["theme"] or "classic",
        } for a in attempts],
        "tests": teacher_tests,
        "classes": len(await run(db.list_classes, user["id"])),
        "registered": bool(await run(db.get_registration, user["id"])),
        "plan": await run(plans.status, user["id"]),
        "sales": SALES_ENABLED,  # obuna sotish yoqilganmi (hozircha yo'q)
        "can_create": SALES_ENABLED or plans.is_free(user["id"]),  # test yarata oladimi
        "plans": await run(plans.offers),
        "card": await run(plans.card),
        "payment": await run(db.open_payment, user["id"]),
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
        if test["fmt"] != "milliy":
            raise ApiError(409, "Bu test qog'ozda (titulda) o'tkaziladi — javobni o'qituvchingizga topshiring")
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
    keys = await run(db.get_keys, test["id"])
    return ok({
        "code": test["code"], "title": test["title"], "fmt": test["fmt"],
        "format": format_title(test["fmt"], test["count"]), "items": public_items(test),
        "variants": list(keys), "closed": bool(test["closed"]), "theme": test.get("theme") or "classic",
        "essay_topic": test.get("essay_topic"), "submitted": previous["id"] if previous else None,
        "last_name": await run(db.last_name_used, user["id"]), "ai": essay_ai.essay_enabled(),
    })


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
    if not SALES_ENABLED:
        raise ApiError(403, "Obuna hozircha yopiq")
    if await run(plans.is_free, user["id"]):
        raise ApiError(400, "Sizda hamma narsa bepul — obuna kerak emas")
    period = str((await body(request)).get("period") or "")
    if period != plans.PACK and period not in plans.PERIODS:
        raise ApiError(400, "Muddat noto'g'ri")
    essays = None
    if period == plans.PACK:
        if not (await run(plans.status, user["id"]))["active"]:
            raise ApiError(400, "Esse paketi faol obunaga qo'shiladi — avval obunani yangilang")
        offer = await run(plans.pack)
        amount, essays = offer["price"], offer["essays"]
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
    return plans.is_free(owner_id) and essay_ai.enabled() and await ai_allowed(owner_id, "written")


async def ai_allowed(owner_id: int, kind: str, amount: int = 1) -> bool:
    """AI ishlatish (oylik limit bo'yicha). Limit/obuna tugagan bo'lsa — o'qituvchiga oyiga bir marta xabar."""
    if await run(plans.use_ai, owner_id, kind, amount):
        return True
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
    test = await run(db.create_test, user["id"], title, fmt, count, items, keys, topic if fmt == "milliy" else None)
    await run(db.set_theme, test["id"], theme)
    if channels:
        await run(db.set_test_channels, test["id"], channels)
    await run(db.set_mode, test["id"], mode)
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
    return ok({"ok": True, "id": copy["id"], "code": copy["code"]})


async def teacher_test(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    attempts = await run(db.list_attempts, test["id"])
    keys = await run(db.get_keys, test["id"])
    essays = await run(db.essays_for_test, test["id"]) if test["fmt"] == "milliy" else {}
    rasch = await run(rasch_results, test, attempts) if test["fmt"] == "milliy" and attempts else {}
    written_jobs = await run(db.written_jobs_for_test, test["id"])
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
            "essay_status": essay["status"] if essay else None,
            "ai_score": essay["ai_score"] if essay else None,
            "written_status": written_jobs.get(a["id"]),  # skanerlangan titul: pending | done | failed
        })
    rows.sort(key=lambda r: (-(r["rasch"] or 0), -r["total"]))
    analysis = [{"label": item["label"],
                 "share": round(sum(1 for a in attempts if item["label"] not in a["wrong"]) * 100 / len(attempts))}
                for item in test["items"]] if attempts else []
    grades = {}
    for _, grade_name in rasch.values():
        grades[grade_name] = grades.get(grade_name, 0) + 1
    written_review_info = await written_review_summary(test, attempts, keys)
    pupils = await pupils_of(user["id"])
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
        "students": [student_view(s, pupils) for s in await run(db.list_students, test["id"])],
        "pupils": list(pupils.values()) if test.get("mode") == "offline" else [],
        "attempts": rows, "analysis": analysis, "grades": grades,
        "essay_cost": round(sum(e["cost"] for e in essays.values()), 4),
        "ai": essay_ai.essay_enabled(), "ai_provider": essay_ai.essay_provider_name(),
        "waiting_essays": await run(db.waiting_offline_essays, test["id"]) if offline_milliy else 0,
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
    if kind not in ("key", "results", "excel"):
        raise ApiError(400, "Noma'lum hujjat")
    try:
        await HOOKS["send_pdf"](user["id"], test, kind)
    except Exception as exc:
        logger.exception("Fayl (%s) yuborilmadi: test %s", kind, test["id"])
        raise ApiError(502, send_error_text(exc))
    return ok({"ok": True})


# ---------- Fayllarni to'g'ridan-to'g'ri yuklab olish (bot chatiga yuborilmasa ham) ----------
# Havola 10 daqiqa amal qiladi va imzolangan: kim, qaysi test, qaysi fayl. Telegram'da tashqi brauzer/yuklovchi
# sarlavha (initData) yubora olmaydi — shuning uchun ruxsat havolaning o'zida

DOWNLOAD_TTL = 600
FILE_KINDS = ("key", "results", "excel", "sheets")


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
    if kind == "sheets":
        students = db.list_students(test["id"])
        return (sheet.sheets_pdf(test, list(keys), students, 0 if students else 1), f"titullar_{test['code']}.pdf",
                "application/pdf")
    attempts = db.list_attempts(test["id"])
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
    })


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
                           round(sheet.WRITTEN_LABEL_W / width, 4), round(sheet.WRITTEN_CELL_W / width, 4), box["cells"]]
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
    rows = [r for r in data["rows"] if not chosen or r["attempt_id"] in chosen]
    if not rows:
        raise ApiError(400, "Sertifikat uchun natija topilmadi")
    content = await run(pdfs.certificates_pdf, test, rows, user.get("name") or "")
    single = len(rows) == 1
    name = f"sertifikat_{_file_slug(rows[0]['name']) if single else test['code']}.pdf"
    who = html.escape(rows[0]["name"]) if single else f"{len(rows)} ta o'quvchi" + (f" · {html.escape(cls['name'])}" if cls else "")
    await HOOKS["send_file"](user["id"], content, name,
                             f"🎓 <b>Mock sertifikat</b> · {html.escape(test['title'])}\n{who}\n"
                             "Chop etish uchun — A4, yotiq. Rasmiy sertifikat emas.")
    return ok({"ok": True, "count": len(rows)})


async def send_class_report(request: web.Request) -> web.Response:
    """📄 Sinf jadvali (PDF): o'quvchilar × barcha testlar, o'rtacha ball va daraja."""
    user = await auth(request)
    cls = await own_class(request, user)
    report = await run(results.class_report, user["id"], cls)
    if not report["tests"]:
        raise ApiError(400, "Bu sinf hali titulli testda qatnashmagan")
    content = await run(pdfs.class_report_pdf, report)
    await HOOKS["send_file"](user["id"], content, f"sinf_{_file_slug(cls['name'])}.pdf",
                             f"📄 <b>{html.escape(cls['name'])}</b> — natijalar jadvali\n"
                             f"{report['class']['students']} ta o'quvchi · {len(report['tests'])} ta test")
    return ok({"ok": True})


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
        if test["fmt"] == "milliy" and style != "standard":  # yozma javob kataklari faqat standart titulda
            raise ApiError(400, "Milliy sertifikat testi uchun faqat standart titul")
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
        await HOOKS["send_sheets"](user["id"], test, students, blanks)
    except Exception as exc:
        logger.exception("Titullar yuborilmadi: test %s", test["id"])
        raise ApiError(502, send_error_text(exc))
    await run(db.mark_sheets_sent, test["id"])
    return ok({"ok": True, "pages": len(students) + blanks})


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
    variants = list(await run(db.get_keys, test["id"]))
    spec = sheet.layout(test, variants)
    # written_area / name_area — telefon tekislangan varaqdan shu qismlarni kesib oladi (oflayn skaner)
    return ok({"bubbles": spec["bubbles"], "variants": variants, "sheet_page": spec["page"],
               "style": sheet_styles.style_of(test), "written_area": spec.get("written_area"),
               "name_area": spec.get("name_area"), "written_labels": [i["label"] for i in text_items(test["items"])],
               "ai": essay_ai.enabled()})


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
        written_png = None
        if spec["written_area"]:
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
        "ai_note": ai_note, "found": True, "corners": corners,
    })


async def save_scanned(test: dict, payload: dict) -> tuple[dict, dict]:
    """Skanerlangan titul natijasini saqlaydi → (attempt, student). Qayta skanerlansa — eski natija almashtiriladi."""
    keys = await run(db.get_keys, test["id"])
    variant = payload.get("variant")
    if variant not in keys:
        raise ApiError(400, "Variantni tanlang")
    student = None
    if payload.get("student_id"):
        student = await run(db.get_student, test["id"], to_int(payload["student_id"], 0, "O'quvchi"))
        if not student:
            raise ApiError(404, "O'quvchi topilmadi")
        if student["attempt_id"]:
            await run(db.delete_attempt, student["attempt_id"])  # qayta skanerlash — eski natija almashtiriladi
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
                await run(db.delete_attempt, student["attempt_id"])
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
    if test["fmt"] == "milliy":
        await link_offline_essay(test, student["number"], attempt["id"])
    return attempt, student


async def link_offline_essay(test: dict, number: int, attempt_id: int) -> bool:
    """O'quvchi botda yozgan esse (ID bo'yicha) — titul natijasiga."""
    essay = await run(db.get_offline_essay, test["id"], number)
    if not essay:
        return False
    ai = essay_ai.essay_enabled() and await ai_allowed(test["owner_id"], "essays")
    await run(db.set_essay_text, attempt_id, essay["text"], "pending" if ai else "manual")
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
    await run(db.create_written_job, attempt["id"], test["id"], data, head[5:head.index(";")][:20], "manual")


async def scan_save(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    attempt, student = await save_scanned(test, payload)
    await store_written_image(test, attempt, payload.get("written_image"))
    if from_app(request) and "scan_notice" in HOOKS:
        try:  # APK'dan skanerlandi — natija bot chatiga ham boradi
            await HOOKS["scan_notice"](user["id"], test, student["name"], total_points(attempt),
                                       max_points(test["items"], test["fmt"]))
        except Exception:
            logger.warning("Skaner xabarini botga yuborib bo'lmadi", exc_info=True)
    return ok({"ok": True, "attempt_id": attempt["id"], "name": student["name"],
               "points": total_points(attempt), "max_points": max_points(test["items"], test["fmt"]),
               "correct": attempt["test_correct"] + attempt["written_correct"], "auto_total": attempt["auto_total"]})


async def attach_essay(user: dict, test: dict, attempt: dict, pages: list[bytes]) -> str:
    """Esse varaqasi suratlari: bot orqali Telegram'ga saqlanadi va AI navbatiga qo'yiladi → esse holati."""
    file_ids = []
    for index, data in enumerate(pages, 1):
        page = f" · {index}-bet" if len(pages) > 1 else ""
        file_ids.append(await HOOKS["store_photo"](user["id"], data,
                                                   f"📝 Esse · {attempt['student_name']}{page} · {test['title']}"))
    status = "pending" if essay_ai.essay_enabled() and await ai_allowed(test["owner_id"], "essays") else "manual"
    if not await run(db.get_essay, attempt["id"]):
        await run(db.create_essay, attempt["id"], None, file_ids, status)
    else:
        await run(db.set_essay_photos, attempt["id"], file_ids, status)
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
    attempt, student = await save_scanned(test, payload)
    if uid:
        _STUDENT_OF[uid] = student["id"]
        await run(db.save_upload, uid, test["id"], student["id"])
    labels = [i["label"] for i in text_items(test["items"])]
    # O'qituvchi hamma yozma javobni o'zi kiritgan bo'lsa — AI kerak emas
    need_ai = [label for label in labels if not (payload.get("answers") or {}).get(label)]
    queued = False
    if written:  # AI o'qiydi (bepul ro'yxat) yoki o'qituvchi keyin rasmga qarab belgilaydi
        queued = bool(need_ai) and await written_ai(test["owner_id"])
        await run(db.create_written_job, attempt["id"], test["id"], base64.b64encode(written).decode(),
                  str(payload.get("written_mime") or "image/jpeg")[:20], "pending" if queued else "manual")
    essay_status = await attach_essay(user, test, attempt, essays) if essays else None
    response = {"ok": True, "attempt_id": attempt["id"], "student_id": student["id"], "name": student["name"],
                "written_queued": queued, "essay_status": essay_status,
                "points": total_points(attempt), "max_points": max_points(test["items"], test["fmt"])}
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
    if "scan_batch_notice" in HOOKS and count("saved"):
        try:
            await HOOKS["scan_batch_notice"](user["id"], test, count("saved"), count("written"), count("essays"),
                                             count("failed"))
        except Exception:
            logger.warning("Skaner xabarini botga yuborib bo'lmadi", exc_info=True)
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


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="Bot ishlayapti")


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
    app = web.Application(middlewares=[rate_limit, errors], client_max_size=14 * 1024 * 1024)
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
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/delete", delete_test)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/copy", copy_test)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/channels", set_channels)
    app.router.add_post("/api/teacher/channel", check_channel)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}/live", live)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/send", send_pdf)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/download", download_link)
    app.router.add_get("/dl/{token}", download_file)
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
