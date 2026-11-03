"""
Telegram Mini App: veb-server (aiohttp) — sahifa (webapp/ papkasi) va JSON API.

Har bir API so'rovi Telegram imzosi (initData) bilan keladi: X-Init-Data sarlavhasi.
Imzo bot tokeni bilan tekshiriladi — foydalanuvchi o'zini boshqa odam qilib ko'rsata olmaydi.

Bot bilan bog'liq amallar (natijalarni e'lon qilish, PDF yuborish, o'qituvchiga xabar)
bot.py dan HOOKS orqali beriladi — bu modul botni import qilmaydi.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data

import db
import essay_ai
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

LIVE_IDLE = 60  # shuncha soniya signal kelmasa — «faol emas»


async def test_info(request: web.Request) -> web.Response:
    user = await auth(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test:
        raise ApiError(404, "Bunday kodli test topilmadi")
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


async def ping(request: web.Request) -> web.Response:
    """O'quvchi test yechayotganda har bir necha soniyada: nechta savol belgilangan — o'qituvchining jonli ekrani uchun."""
    user = await auth(request)
    payload = await body(request)
    test = await run(db.get_test_by_code, request.match_info["code"])
    if not test or test["closed"]:
        return ok({"ok": False})
    try:
        total = max(0, min(int(payload.get("total") or 0), 500))
        answered = max(0, min(int(payload.get("answered") or 0), total))
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
    await HOOKS["send_pdf"](user["id"], test, "key")
    return ok({"ok": True, "id": test["id"], "code": test["code"]})


async def copy_test(request: web.Request) -> web.Response:
    """Shu testning nusxasi — yangi kod bilan, kalitlar o'sha: boshqa guruhga qayta kiritmasdan berish uchun."""
    user = await auth(request)
    test = await own_test(request, user)
    title = " ".join(str((await body(request)).get("title") or "").split())[:80] or f"{test['title']} (nusxa)"[:80]
    keys = await run(db.get_keys, test["id"])
    copy = await run(db.create_test, user["id"], title, test["fmt"], test["count"], test["items"], keys, test.get("essay_topic"))
    await run(db.set_theme, copy["id"], test.get("theme") or "classic")
    return ok({"ok": True, "id": copy["id"], "code": copy["code"]})


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
                 "essay_topic": test.get("essay_topic"), "max_points": max_points(test["items"], test["fmt"])},
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
    if kind not in ("key", "results", "excel"):
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
    app.router.add_post("/api/test/{code:\\d{5}}/ping", ping)
    app.router.add_get("/api/result/{attempt_id:\\d+}", result)
    app.router.add_post("/api/teacher/tests", create_test)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}", teacher_test)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/announce", announce)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/reopen", reopen)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/delete", delete_test)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/copy", copy_test)
    app.router.add_get("/api/teacher/test/{test_id:\\d+}/live", live)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/send", send_pdf)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/theme", set_theme)
    app.router.add_post("/api/teacher/test/{test_id:\\d+}/essays/confirm-all", essays_confirm_all)
    app.router.add_get("/api/teacher/essay/{attempt_id:\\d+}", essay_detail)
    app.router.add_post("/api/teacher/essay/{attempt_id:\\d+}", essay_score)
    return app


async def start(port: int) -> web.AppRunner:
    runner = web.AppRunner(create_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Mini App serveri %s-portda ochildi", port)
    return runner
