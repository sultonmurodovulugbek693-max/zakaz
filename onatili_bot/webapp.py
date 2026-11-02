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
import io
import json
import random
import logging
import time
from pathlib import Path

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data

import db
import essay_ai
import sheet
from config import ADMIN_IDS, BOT_TOKEN
from scoring import rasch_results, total_points
from structure import (
    ESSAY_MAX,
    LETTERS,
    MAX_TEXT_OPTIONS,
    VARIANT_NAMES,
    build_items,
    choice_items,
    format_title,
    grade,
    max_points,
    percent,
    split_options,
    text_items,
)

logger = logging.getLogger("webapp")
STATIC_DIR = Path(__file__).resolve().parent / "webapp"
THEMES = ["classic", "diploma", "ikat", "tiles", "neon", "terminal", "brutal", "glass", "chalk", "newspaper",
          "bauhaus", "pastel", "ocean", "autumn", "minimal", "luxe", "sakura", "sport", "paper", "space"]
INIT_DATA_MAX_AGE = 24 * 3600
ESSAY_MIN_CHARS = 200

HOOKS: dict = {}  # bot.py to'ldiradi: record_attempt, announce, send_pdf


class ApiError(web.HTTPException):
    def __init__(self, status: int, message: str, **extra):
        self.status_code = status
        super().__init__(text=json.dumps({"error": message, **extra}, ensure_ascii=False), content_type="application/json")


def ok(data: dict | list) -> web.Response:
    return web.json_response(data, dumps=lambda value: json.dumps(value, ensure_ascii=False))


def current_user(request: web.Request) -> dict:
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


async def missing_for(user: dict) -> list[dict]:
    """Obuna bo'lmagan kanallar (admin va hook yo'q bo'lsa — bo'sh)."""
    if user["id"] in ADMIN_IDS or "missing" not in HOOKS:
        return []
    return [{"title": c["title"], "url": c["url"]} for c in await HOOKS["missing"](user["id"])]


async def auth(request: web.Request) -> dict:
    """Imzo + majburiy obuna tekshiruvi."""
    user = current_user(request)
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
    user = current_user(request)
    await run(db.remember_user, user["id"], user["name"], user["username"])
    missing = await missing_for(user)
    if missing:
        return ok({"user": user, "subscribe": missing})
    attempts = await run(db.attempts_of_user, user["id"])
    tests = await run(db.list_tests, user["id"], 50)
    teacher_tests = []
    for test in tests:
        test_attempts = await run(db.list_attempts, test["id"])
        totals = [total_points(a) for a in test_attempts]
        teacher_tests.append({
            "id": test["id"], "code": test["code"], "title": test["title"], "fmt": test["fmt"],
            "format": format_title(test["fmt"], test["count"]), "closed": bool(test["closed"]),
            "students": len(test_attempts), "created_at": test["created_at"], "theme": test.get("theme") or "classic",
            "mode": test.get("mode") or "online",
            "avg": round(sum(totals) / len(totals), 1) if totals else None,
            "max_points": max_points(test["items"], test["fmt"]),
        })
    return ok({
        "user": user,
        "last_name": await run(db.last_name_used, user["id"]),
        "attempts": [{
            "id": a["id"], "code": a["code"], "title": a["title"], "fmt": a["fmt"], "variant": a["variant"],
            "announced": bool(a["closed"]), "created_at": a["created_at"], "theme": a["theme"] or "classic",
        } for a in attempts],
        "tests": teacher_tests,
        "ai": essay_ai.enabled(),
        "themes": THEMES,
    })


# ---------- O'quvchi ----------

async def test_info(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test:
        raise ApiError(404, "Bunday kodli test topilmadi")
    if (test.get("mode") or "online") == "offline":
        raise ApiError(409, "Bu test qog'ozda (titulda) o'tkaziladi — javobni o'qituvchingizga topshiring")
    previous = await run(db.find_attempt, test["id"], user["id"])
    keys = await run(db.get_keys, test["id"])
    return ok({
        "code": test["code"], "title": test["title"], "fmt": test["fmt"],
        "format": format_title(test["fmt"], test["count"]), "items": public_items(test),
        "variants": list(keys), "closed": bool(test["closed"]), "theme": test.get("theme") or "classic",
        "essay_topic": test.get("essay_topic"), "submitted": previous["id"] if previous else None,
        "last_name": await run(db.last_name_used, user["id"]), "ai": essay_ai.enabled(),
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
    return ok({"ok": True, "attempt_id": attempt["id"]})


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
        scores = await run(rasch_results, test, all_attempts)
        if attempt["id"] in scores:
            rasch = {"score": scores[attempt["id"]][0], "grade": scores[attempt["id"]][1]}
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
                 "items": public_items(test), "format": format_title(test["fmt"], test["count"])},
        "attempt": {
            "id": attempt["id"], "name": attempt["student_name"], "variant": attempt["variant"],
            "test_correct": attempt["test_correct"], "written_correct": attempt["written_correct"],
            "choices_total": len(choice_items(test["items"])), "texts_total": len(text_items(test["items"])),
            "auto_total": attempt["auto_total"], "points": attempt.get("points") or 0, "essay": attempt["essay"],
            "total": total_points(attempt), "max_points": max_points(test["items"], test["fmt"]),
            "wrong": attempt["wrong"], "answers": attempt["answers"], "created_at": attempt["created_at"],
            "percent": percent(attempt["test_correct"] + attempt["written_correct"], attempt["auto_total"]),
        },
        "rasch": rasch,
        "key": key_display(test, keys.get(attempt["variant"], {})),
        "essay": essay_view,
        "essay_max": ESSAY_MAX,
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


async def create_test(request: web.Request) -> web.Response:
    user = await auth(request)
    payload = await body(request)
    title = " ".join(str(payload.get("title", "")).split())[:80]
    if len(title) < 2:
        raise ApiError(400, "Test nomini yozing")
    fmt = payload.get("fmt")
    if fmt not in ("milliy", "oddiy"):
        raise ApiError(400, "Test turini tanlang")
    count = 45 if fmt == "milliy" else int(payload.get("count") or 0)
    if fmt == "oddiy" and not 5 <= count <= 100:
        raise ApiError(400, "Savollar soni 5–100 bo'lishi kerak")
    items = build_items(fmt, count)
    raw_keys = payload.get("keys") or {}
    if not isinstance(raw_keys, dict) or not raw_keys:
        raise ApiError(400, "Kalitni kiriting")
    keys = {}
    for name in VARIANT_NAMES:
        if name in raw_keys:
            keys[name] = validate_key(items, raw_keys[name] or {})
    if "A" not in keys:
        raise ApiError(400, "A variant kaliti kerak")
    theme = payload.get("theme") if payload.get("theme") in THEMES else "classic"
    topic = str(payload.get("essay_topic") or "").strip()[:2000] or None
    test = await run(db.create_test, user["id"], title, fmt, count, items, keys, topic if fmt == "milliy" else None)
    await run(db.set_theme, test["id"], theme)
    mode = "offline" if payload.get("mode") == "offline" else "online"
    await run(db.set_mode, test["id"], mode)
    if mode == "online":
        await HOOKS["send_pdf"](user["id"], test, "sheet")
    await HOOKS["send_pdf"](user["id"], test, "key")
    return ok({"ok": True, "id": test["id"], "code": test["code"]})


async def teacher_test(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    attempts = await run(db.list_attempts, test["id"])
    keys = await run(db.get_keys, test["id"])
    essays = await run(db.essays_for_test, test["id"]) if test["fmt"] == "milliy" else {}
    rasch = await run(rasch_results, test, attempts) if test["fmt"] == "milliy" and attempts else {}
    rows = []
    for a in attempts:
        essay = essays.get(a["id"])
        score, grade_name = rasch.get(a["id"], (None, None))
        rows.append({
            "id": a["id"], "name": a["student_name"], "variant": a["variant"],
            "test_correct": a["test_correct"], "written_correct": a["written_correct"], "auto_total": a["auto_total"],
            "points": a.get("points") or 0, "essay": a["essay"], "total": total_points(a),
            "percent": percent(a["test_correct"] + a["written_correct"], a["auto_total"]),
            "rasch": score, "grade": grade_name, "wrong": a["wrong"], "created_at": a["created_at"],
            "essay_status": essay["status"] if essay else None,
            "ai_score": essay["ai_score"] if essay else None,
        })
    rows.sort(key=lambda r: (-(r["rasch"] or 0), -r["total"]))
    analysis = [{"label": item["label"],
                 "share": round(sum(1 for a in attempts if item["label"] not in a["wrong"]) * 100 / len(attempts))}
                for item in test["items"]] if attempts else []
    grades = {}
    for _, grade_name in rasch.values():
        grades[grade_name] = grades.get(grade_name, 0) + 1
    return ok({
        "test": {"id": test["id"], "code": test["code"], "title": test["title"], "fmt": test["fmt"],
                 "format": format_title(test["fmt"], test["count"]), "closed": bool(test["closed"]),
                 "theme": test.get("theme") or "classic", "variants": list(keys), "created_at": test["created_at"],
                 "essay_topic": test.get("essay_topic"), "max_points": max_points(test["items"], test["fmt"]),
                 "mode": test.get("mode") or "online"},
        "students": [student_view(s) for s in await run(db.list_students, test["id"])],
        "attempts": rows, "analysis": analysis, "grades": grades,
        "essay_cost": round(sum(e["cost"] for e in essays.values()), 4),
        "ai": essay_ai.enabled(), "ai_provider": essay_ai.provider_name(),
    })


async def announce(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    if test["closed"]:
        raise ApiError(409, "Natijalar allaqachon e'lon qilingan")
    if not await run(db.list_attempts, test["id"]):
        raise ApiError(400, "Hali hech kim javob yubormagan")
    sent = await HOOKS["announce"](test, user["id"])
    return ok({"ok": True, "sent": sent})


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
    if kind not in ("sheet", "key", "results"):
        raise ApiError(400, "Noma'lum hujjat")
    await HOOKS["send_pdf"](user["id"], test, kind)
    return ok({"ok": True})


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
    return ok({"ok": True, "score": score})


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
    return ok({"ok": True, "count": count})


# ---------- Oflayn test: o'quvchilar, titullar, skaner ----------

def student_view(student: dict) -> dict:
    return {"id": student["id"], "number": student["number"], "name": student["name"],
            "variant": student["variant"], "scanned": bool(student["attempt_id"]), "attempt_id": student["attempt_id"]}


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
    return ok({"students": [student_view(s) for s in await run(db.list_students, test["id"])],
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


async def sheets(request: web.Request) -> web.Response:
    """Titullar PDF'i chatga: har o'quvchiga shaxsiy titul + bo'sh titullar."""
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    blanks = max(0, min(200, int(payload.get("blanks") or 0)))
    students = await run(db.list_students, test["id"])
    if payload.get("only"):
        students = [s for s in students if s["id"] in set(payload["only"])]
    if not students and not blanks:
        raise ApiError(400, "Avval o'quvchilarni qo'shing yoki bo'sh titullar sonini kiriting")
    await HOOKS["send_sheets"](user["id"], test, students, blanks)
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


async def scan(request: web.Request) -> web.Response:
    """Titul surati → o'qilgan javoblar (TAKLIF: o'qituvchi tekshirib saqlaydi)."""
    user = await auth(request)
    test = await own_test(request, user)
    data = await read_photo(request)
    variants = list(await run(db.get_keys, test["id"]))
    spec = sheet.layout(test, variants)

    def work():
        import cv2
        import omr
        page = omr.SheetPage(name_area=spec["name_area"])
        prepared = omr.PreparedSheet(data, spec["bubbles"], 1, page)
        result = omr.scan_answer_sheet(data, spec["bubbles"], 1, prepared, page)
        written_png = None
        if spec["written_area"]:
            x1, y1, x2, y2 = (round(v * omr.SCALE) for v in spec["written_area"])
            crop = prepared.warped[max(0, y1): y2, max(0, x1): x2]
            ok_, png = cv2.imencode(".png", crop)
            written_png = png.tobytes() if ok_ else None
        return result, written_png

    try:
        result, written_png = await run(work)
    except Exception as error:
        message = str(error) if error.__class__.__name__ == "ScanError" else "Rasmni o'qib bo'lmadi — qaytadan suratga oling"
        if error.__class__.__name__ != "ScanError":
            logger.exception("Skaner xatosi")
        raise ApiError(422, message)

    # O'quvchi: avval titul pastidagi kod (shaxsiy titul), bo'lmasa o'quvchi bo'yagan ID doirachalari
    number = sheet.decode_number(result["sheet_code"])
    id_source = "code" if number else None
    if not number and result.get("sheet_number_status") == "ok":
        number, id_source = result["sheet_number"], "bubbles"
    student = await run(db.get_student, test["id"], None, number) if number else None
    if number and not student:
        id_source = "unknown"  # ID o'qildi, lekin ro'yxatda bunday o'quvchi yo'q
    variant = result["variant"] or (student["variant"] if student else None) or (variants[0] if len(variants) == 1 else None)
    written, ai_note = {}, None
    labels = [i["label"] for i in text_items(test["items"])]
    if labels and written_png:
        if essay_ai.enabled():
            try:
                written, _cost = await essay_ai.read_written(written_png, labels)
            except essay_ai.AIError as error:
                ai_note = f"AI yozma javoblarni o'qiy olmadi: {error}"
        else:
            ai_note = "AI o'chiq — yozma javoblarni rasmga qarab o'zingiz yozing"
    return ok({
        "student": student_view(student) if student else None,
        "code_status": "ok" if student else ("blank" if result["sheet_code"] == 0 else "unclear"),
        "id_source": id_source, "id_read": number,
        "id_status": result.get("sheet_number_status"),
        "variant": variant, "variant_read": result["variant"],
        "questions": [{"label": q["label"], "chosen": q["chosen"], "status": q["status"]} for q in result["questions"]],
        "written": written, "written_image": _png_data_url(written_png), "name_image": result["name_image"],
        "ai_note": ai_note,
    })


async def scan_save(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await own_test(request, user)
    payload = await body(request)
    keys = await run(db.get_keys, test["id"])
    variant = payload.get("variant")
    if variant not in keys:
        raise ApiError(400, "Variantni tanlang")
    student = None
    if payload.get("student_id"):
        student = await run(db.get_student, test["id"], int(payload["student_id"]))
        if not student:
            raise ApiError(404, "O'quvchi topilmadi")
        if student["attempt_id"]:
            await run(db.delete_attempt, student["attempt_id"])  # qayta skanerlash — eski natija almashtiriladi
        if student["variant"] != variant:
            await run(db.update_student, test["id"], student["id"], None, variant)
    else:
        name = " ".join(str(payload.get("name") or "").split())[:60]
        if len(name) < 3:
            raise ApiError(400, "O'quvchi ismini yozing yoki ro'yxatdan tanlang")
        student = (await run(db.add_students, test["id"], [name], [variant]))[-1]
    answers = clean_answers(test, payload.get("answers") or {})
    attempt = await HOOKS["record_attempt"](test, db.offline_tg_id(test["id"], student["number"]), student["name"],
                                            variant, answers, None, [], 0.0, notify=False)
    if attempt is None:
        raise ApiError(409, "Bu o'quvchining natijasi allaqachon bor")
    await run(db.link_student_attempt, student["id"], attempt["id"])
    return ok({"ok": True, "attempt_id": attempt["id"], "name": student["name"],
               "points": total_points(attempt), "max_points": max_points(test["items"], test["fmt"]),
               "correct": attempt["test_correct"] + attempt["written_correct"], "auto_total": attempt["auto_total"]})


async def essay_photo(request: web.Request) -> web.Response:
    """Oflayn esse varaqasi surati: bot orqali Telegram'ga saqlanadi va AI navbatiga qo'yiladi."""
    user = await auth(request)
    attempt, test = await _own_attempt(request, user)
    data = await read_photo(request)
    file_id = await HOOKS["store_photo"](user["id"], data, f"📝 Esse · {attempt['student_name']} · {test['title']}")
    status = "pending" if essay_ai.enabled() else "manual"
    if not await run(db.get_essay, attempt["id"]):
        await run(db.create_essay, attempt["id"], None, [file_id], status)
    else:
        await run(db.set_essay_photos, attempt["id"], [file_id], status)
    return ok({"ok": True, "status": status})


# ---------- Server ----------

async def index(request: web.Request) -> web.Response:
    response = web.FileResponse(STATIC_DIR / "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="Bot ishlayapti")


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
    app = web.Application(middlewares=[errors], client_max_size=14 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/app", index)
    app.router.add_get("/health", health)
    app.router.add_static("/static/", STATIC_DIR)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_get("/api/test/{code:\\d{5}}", test_info)
    app.router.add_post("/api/test/{code:\\d{5}}/submit", submit)
    app.router.add_get("/api/result/{attempt_id:\\d+}", result)
    app.router.add_post("/api/teacher/tests", create_test)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}", teacher_test)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/announce", announce)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/reopen", reopen)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/delete", delete_test)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/send", send_pdf)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/theme", set_theme)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/essays/confirm-all", essays_confirm_all)
    app.router.add_get("/api/teacher/essay/{attempt_id:\\d+}", essay_detail)
    app.router.add_post("/api/teacher/essay/{attempt_id:\\d+}", essay_score)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}/students", students_list)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/students", students_add)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/students/{student_id:\\d+}", student_update)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/students/import", students_import)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/sheets", sheets)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/scan", scan)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/scan/save", scan_save)
    app.router.add_post("/api/teacher/attempt/{attempt_id:\\d+}/essay-photo", essay_photo)
    return app


async def start(port: int) -> web.AppRunner:
    runner = web.AppRunner(create_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Mini App serveri %s-portda ochildi", port)
    return runner
