"""
Baza: testlar, variantlar (kalitlar), natijalar, esselar, oflayn o'quvchilar, jonli holat, kanallar.

Ikki rejim:
  • DATABASE_URL (postgresql://...) berilgan bo'lsa — PostgreSQL. Productionda SHU kerak:
    Render'da fayl tizimi vaqtinchalik, SQLite fayli har deployda o'chib ketadi.
  • aks holda — SQLite fayli (DB_PATH), kompyuterda sinash uchun.

Barcha so'rovlar "?" belgisi bilan yoziladi; PostgreSQL uchun avtomatik "%s" ga aylantiriladi.
"""

import json
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from config import DB_PATH

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    IntegrityError = (psycopg.errors.IntegrityError, sqlite3.IntegrityError)
else:
    IntegrityError = (sqlite3.IntegrityError,)

# Ustun turlari: Telegram ID'lar 32 bitga sig'maydi (BIGINT); kasr ball — aniq (DOUBLE PRECISION)
ID = "BIGSERIAL PRIMARY KEY" if POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
BIG = "BIGINT" if POSTGRES else "INTEGER"
FLOAT = "DOUBLE PRECISION" if POSTGRES else "REAL"

SCHEMA = [
    f"""CREATE TABLE IF NOT EXISTS users (
        tg_id          {BIG} PRIMARY KEY,
        full_name      TEXT,
        username       TEXT,
        last_name_used TEXT,
        created_at     TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS tests (
        id          {ID},
        code        TEXT UNIQUE NOT NULL,
        owner_id    {BIG} NOT NULL,
        title       TEXT NOT NULL,
        fmt         TEXT NOT NULL,
        count       INTEGER NOT NULL DEFAULT 0,
        items       TEXT NOT NULL,
        closed      INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT,
        essay_topic TEXT,
        theme       TEXT,
        mode        TEXT NOT NULL DEFAULT 'online',
        channels    TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS variants (
        test_id {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        name    TEXT NOT NULL,
        answers TEXT NOT NULL,
        PRIMARY KEY (test_id, name)
    )""",
    f"""CREATE TABLE IF NOT EXISTS attempts (
        id              {ID},
        test_id         {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        tg_id           {BIG} NOT NULL,
        student_name    TEXT NOT NULL,
        variant         TEXT NOT NULL,
        answers         TEXT NOT NULL,
        test_correct    INTEGER NOT NULL,
        written_correct INTEGER NOT NULL,
        auto_total      INTEGER NOT NULL,
        wrong           TEXT NOT NULL,
        points          {FLOAT} NOT NULL DEFAULT 0,
        essay           {FLOAT},
        created_at      TEXT,
        UNIQUE (test_id, tg_id)
    )""",
    # Esse (45-savol): matn yoki qo'lyozma rasmlari (Telegram file_id), AI bahosi.
    # status: none — esse yo'q; manual — AI o'chiq; pending — AI navbatida; graded — baholandi; failed — xato
    f"""CREATE TABLE IF NOT EXISTS essays (
        id         {ID},
        attempt_id {BIG} UNIQUE NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        text       TEXT,
        photos     TEXT NOT NULL DEFAULT '[]',
        status     TEXT NOT NULL,
        ai_score   {FLOAT},
        ai_result  TEXT,
        provider   TEXT,
        cost       {FLOAT} NOT NULL DEFAULT 0,
        tries      INTEGER NOT NULL DEFAULT 0,
        error      TEXT,
        created_at TEXT,
        graded_at  TEXT
    )""",
    # Oflayn test o'quvchilari: raqam titul kodida bosiladi, skaner varaq kimniki ekanini shundan taniydi
    f"""CREATE TABLE IF NOT EXISTS students (
        id         {ID},
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        number     INTEGER NOT NULL,
        name       TEXT NOT NULL,
        variant    TEXT NOT NULL,
        attempt_id {BIG} REFERENCES attempts(id) ON DELETE SET NULL,
        UNIQUE (test_id, number)
    )""",
    # Onlayn test jonli holati: kim ochgan, nechta belgilagan, oxirgi faolligi (unix soniya)
    f"""CREATE TABLE IF NOT EXISTS live (
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        tg_id      {BIG} NOT NULL,
        name       TEXT NOT NULL,
        answered   INTEGER NOT NULL DEFAULT 0,
        total      INTEGER NOT NULL DEFAULT 0,
        started_at {BIG} NOT NULL,
        seen_at    {BIG} NOT NULL,
        PRIMARY KEY (test_id, tg_id)
    )""",
    # Majburiy obuna kanallari (admin bot ichidan qo'shadi)
    f"""CREATE TABLE IF NOT EXISTS channels (
        chat_id  {BIG} PRIMARY KEY,
        title    TEXT NOT NULL,
        url      TEXT NOT NULL,
        added_by {BIG},
        added_at TEXT
    )""",
    # Oflayn skaner: titulning yozma javob kataklari rasmi (base64) — AI fonda o'qiydi va natijani qayta hisoblaydi.
    # status: pending — navbatda; done — o'qildi; failed — o'qib bo'lmadi. notified — o'qituvchiga xabar ketganmi
    f"""CREATE TABLE IF NOT EXISTS written_jobs (
        attempt_id {BIG} PRIMARY KEY REFERENCES attempts(id) ON DELETE CASCADE,
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        image      TEXT,
        mime       TEXT NOT NULL DEFAULT 'image/jpeg',
        status     TEXT NOT NULL DEFAULT 'pending',
        tries      INTEGER NOT NULL DEFAULT 0,
        error      TEXT,
        result     TEXT,
        notified   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT
    )""",
    # Titulli (qog'oz) testning essesi: o'quvchi botda ID'si bilan yozadi. Titul skanerlanganda
    # shu ID'li o'quvchining natijasiga biriktiriladi (oldin ham, keyin ham yuborilishi mumkin)
    f"""CREATE TABLE IF NOT EXISTS offline_essays (
        test_id    {BIG} NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
        number     INTEGER NOT NULL,
        tg_id      {BIG} NOT NULL,
        name       TEXT,
        text       TEXT NOT NULL,
        created_at TEXT,
        PRIMARY KEY (test_id, number)
    )""",
    # O'qituvchining sinflari: o'quvchiga doimiy ID beriladi (o'qituvchining barcha sinflari bo'yicha takrorlanmaydi).
    # Titulli testga sinf qo'shilganda o'quvchilar shu ID'lar bilan test ro'yxatiga ko'chiriladi
    f"""CREATE TABLE IF NOT EXISTS classes (
        id         {ID},
        owner_id   {BIG} NOT NULL,
        name       TEXT NOT NULL,
        created_at TEXT
    )""",
    f"""CREATE TABLE IF NOT EXISTS class_students (
        id       {ID},
        class_id {BIG} NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
        owner_id {BIG} NOT NULL,
        number   INTEGER NOT NULL,
        name     TEXT NOT NULL,
        UNIQUE (owner_id, number)
    )""",
    # Har o'qituvchiga berilgan oxirgi ID — o'chirilgan o'quvchining ID'si boshqasiga qayta berilmasin
    f"""CREATE TABLE IF NOT EXISTS id_counters (
        owner_id {BIG} PRIMARY KEY,
        last     INTEGER NOT NULL
    )""",
    # O'qituvchi obunasi: tarif va tugash sanasi (admin beradi; birinchi testda — bepul sinov)
    f"""CREATE TABLE IF NOT EXISTS subscriptions (
        tg_id      {BIG} PRIMARY KEY,
        plan       TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        updated_at TEXT,
        reminded   TEXT
    )""",
    # Oylik AI sarfi (o'qituvchi bo'yicha): esse va yozma javoblar (titul). Limit shu yerda tekshiriladi
    f"""CREATE TABLE IF NOT EXISTS usage (
        tg_id    {BIG} NOT NULL,
        month    TEXT NOT NULL,
        essays   INTEGER NOT NULL DEFAULT 0,
        written  INTEGER NOT NULL DEFAULT 0,
        notified INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (tg_id, month)
    )""",
    # Ilovaga (APK) Telegram orqali kirish: kind = login (bot tasdiqlashini kutadi) | session (kirilgan)
    f"""CREATE TABLE IF NOT EXISTS app_tokens (
        token   TEXT PRIMARY KEY,
        kind    TEXT NOT NULL,
        tg_id   {BIG},
        session TEXT,
        created {FLOAT} NOT NULL
    )""",
    # Admin bot orqali o'zgartiradigan sozlamalar: narxlar, chegirmalar, karta raqami, sinov kunlari
    """CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    # Obuna to'lovi: o'quvchi/o'qituvchi kartaga o'tkazadi, chekni botga yuboradi, admin tasdiqlaydi.
    # status: pending — chek kutilmoqda; sent — chek yuborilgan, admin ko'rmoqda; paid — tasdiqlangan; rejected — rad etilgan
    f"""CREATE TABLE IF NOT EXISTS payments (
        id         {ID},
        tg_id      {BIG} NOT NULL,
        period     TEXT NOT NULL,
        amount     {BIG} NOT NULL,
        status     TEXT NOT NULL DEFAULT 'pending',
        receipt    TEXT,
        created_at TEXT,
        decided_at TEXT,
        admin_id   {BIG}
    )""",
    # Oflayn «Ona tili» kursiga ro'yxatdan o'tish: o'quvchi ilovada ism, telefon, yosh va manzilini
    # qoldiradi — ariza adminga boradi, keyin ular bevosita (botdan tashqari) muloqot qiladi.
    # Bitta foydalanuvchi — bitta ariza (qaytadan yuborsa, eskisi yangilanadi)
    f"""CREATE TABLE IF NOT EXISTS registrations (
        id         {ID},
        tg_id      {BIG} UNIQUE NOT NULL,
        role       TEXT NOT NULL DEFAULT 'student',
        first_name TEXT,
        last_name  TEXT,
        full_name  TEXT NOT NULL,
        phone      TEXT NOT NULL,
        age        INTEGER,
        region     TEXT,
        district   TEXT,
        address    TEXT,
        created_at TEXT,
        updated_at TEXT
    )""",
    # O'qituvchi dars beradigan joylar («joy ochadi»): bir nechta tuman bo'lishi mumkin.
    # O'quvchi shu tumanni tanlasa — arizasi o'sha tumandagi o'qituvchilarga boradi
    f"""CREATE TABLE IF NOT EXISTS teacher_places (
        id         {ID},
        tg_id      {BIG} NOT NULL,
        region     TEXT NOT NULL,
        district   TEXT NOT NULL,
        created_at TEXT,
        UNIQUE (tg_id, region, district)
    )""",
]

# Eski bazalar uchun keyin qo'shilgan ustunlar
MIGRATIONS = [
    ("attempts", "points", f"{FLOAT} NOT NULL DEFAULT 0"),
    ("tests", "essay_topic", "TEXT"),
    ("tests", "theme", "TEXT"),
    ("tests", "mode", "TEXT NOT NULL DEFAULT 'online'"),
    ("tests", "channels", "TEXT"),
    ("tests", "sheet_style", "TEXT"),  # titul ko'rinishi: standard | compact | large | columns
    ("users", "role", "TEXT"),  # oxirgi tanlangan ekran: student | teacher
    ("registrations", "first_name", "TEXT"),  # ism va familiya alohida so'raladi
    ("registrations", "last_name", "TEXT"),
    ("registrations", "role", "TEXT NOT NULL DEFAULT 'student'"),  # o'quvchi | o'qituvchi
    ("registrations", "region", "TEXT"),  # manzil endi ro'yxatdan tanlanadi (places.py)
    ("registrations", "district", "TEXT"),
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------- Ulanish ----------

class _Connection:
    """sqlite3 va psycopg uchun bitta ko'rinish: execute("... ? ...", params) -> qatorlar dict ko'rinishida."""

    def __init__(self, raw, postgres: bool):
        self.raw, self.postgres = raw, postgres

    def execute(self, sql: str, params: tuple | list = ()):
        if self.postgres:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                DATABASE_URL, min_size=1, max_size=8, open=True, timeout=30,
                kwargs={"row_factory": dict_row, "autocommit": False},
                check=ConnectionPool.check_connection,  # uzilib qolgan ulanish qayta ochiladi
            )
        return _pool


@contextmanager
def connect():
    """Tranzaksiya: muvaffaqiyatli bo'lsa saqlanadi, xato bo'lsa bekor qilinadi."""
    if POSTGRES:
        with _get_pool().connection() as raw:  # chiqishda commit / xatoda rollback
            yield _Connection(raw, True)
        return
    raw = sqlite3.connect(DB_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        yield _Connection(raw, False)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _row(row) -> dict | None:
    return dict(row) if row is not None else None


def init() -> None:
    with connect() as conn:
        for statement in SCHEMA:
            conn.execute(statement)
        for table, column, ddl in MIGRATIONS:
            if POSTGRES:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
            else:
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # Titul ko'rinishlarining oldingi nomlari → hozirgilari
        conn.execute("UPDATE tests SET sheet_style = 'large' WHERE sheet_style = 'evalbee'")
        conn.execute("UPDATE tests SET sheet_style = 'columns' WHERE sheet_style = 'dtm'")


def backend_name() -> str:
    return "PostgreSQL" if POSTGRES else f"SQLite ({DB_PATH})"


# ---------- Foydalanuvchilar ----------

def remember_user(tg_id: int, full_name: str, username: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (tg_id, full_name, username, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET full_name = excluded.full_name, username = excluded.username",
            (tg_id, full_name, username, _now()),
        )


def get_role(tg_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    return row["role"] if row else None


def set_role(tg_id: int, role: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET role = ? WHERE tg_id = ?", (role, tg_id))


def create_handoff(token: str, tg_id: int, session: str) -> None:
    """Mini App'dan APK'ga o'tish: allaqachon tasdiqlangan kirish (Telegram imzosi bilan kelgan foydalanuvchi)."""
    create_login(token)
    confirm_login(token, tg_id, session)


def last_name_used(tg_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT last_name_used FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    return row["last_name_used"] if row else None


def set_last_name_used(tg_id: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET last_name_used = ? WHERE tg_id = ?", (name, tg_id))


# ---------- Ilovaga kirish (APK) ----------

LOGIN_TTL = 10 * 60          # kirish havolasi 10 daqiqa amal qiladi
SESSION_TTL = 180 * 24 * 3600  # ilovadagi sessiya ~6 oy


def create_login(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM app_tokens WHERE kind = 'login' AND created < ?", (time.time() - LOGIN_TTL,))
        conn.execute("INSERT INTO app_tokens (token, kind, created) VALUES (?, 'login', ?)", (token, time.time()))


def confirm_login(token: str, tg_id: int, session: str) -> bool:
    """Bot /start app_<token> ni oldi: kirish tasdiqlanadi va sessiya yaratiladi."""
    with connect() as conn:
        row = conn.execute("SELECT created, tg_id FROM app_tokens WHERE token = ? AND kind = 'login'",
                           (token,)).fetchone()
        if not row or row["tg_id"] is not None or time.time() - row["created"] > LOGIN_TTL:
            return False
        conn.execute("UPDATE app_tokens SET tg_id = ?, session = ? WHERE token = ?", (tg_id, session, token))
        conn.execute("INSERT INTO app_tokens (token, kind, tg_id, created) VALUES (?, 'session', ?, ?)",
                     (session, tg_id, time.time()))
    return True


def take_login(token: str) -> str | None:
    """Ilova kutayotgan kirish: tasdiqlangan bo'lsa sessiya tokeni (bir marta beriladi)."""
    with connect() as conn:
        row = conn.execute("SELECT session FROM app_tokens WHERE token = ? AND kind = 'login'", (token,)).fetchone()
        if row and row["session"]:
            conn.execute("DELETE FROM app_tokens WHERE token = ?", (token,))
            return row["session"]
    return None


def session_user(session: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT s.tg_id, s.created, u.full_name, u.username FROM app_tokens s "
            "LEFT JOIN users u ON u.tg_id = s.tg_id WHERE s.token = ? AND s.kind = 'session'", (session,)).fetchone()
    if not row or time.time() - row["created"] > SESSION_TTL:
        return None
    return {"id": row["tg_id"], "name": row["full_name"] or "Foydalanuvchi", "username": row["username"]}


def delete_session(session: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM app_tokens WHERE token = ? AND kind = 'session'", (session,))


# ---------- Testlar ----------

def _test_dict(row) -> dict | None:
    if row is None:
        return None
    test = dict(row)
    test["items"] = json.loads(test["items"])
    test["channels"] = json.loads(test.get("channels") or "[]")
    return test


def create_test(owner_id: int, title: str, fmt: str, count: int, items: list[dict],
                keys: dict[str, dict[str, str]], essay_topic: str | None = None) -> dict:
    with connect() as conn:
        while True:
            code = str(random.randint(10000, 99999))
            if not conn.execute("SELECT 1 FROM tests WHERE code = ?", (code,)).fetchone():
                break
        test_id = conn.execute(
            "INSERT INTO tests (code, owner_id, title, fmt, count, items, created_at, essay_topic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (code, owner_id, title, fmt, count, json.dumps(items, ensure_ascii=False), _now(), essay_topic),
        ).fetchone()["id"]
        for name, answers in keys.items():
            conn.execute(
                "INSERT INTO variants (test_id, name, answers) VALUES (?, ?, ?)",
                (test_id, name, json.dumps(answers, ensure_ascii=False)),
            )
    return get_test(test_id)


def get_test(test_id: int) -> dict | None:
    with connect() as conn:
        return _test_dict(conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone())


def get_test_by_code(code: str) -> dict | None:
    with connect() as conn:
        return _test_dict(conn.execute("SELECT * FROM tests WHERE code = ?", (code,)).fetchone())


def list_tests(owner_id: int, limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM attempts a WHERE a.test_id = t.id) AS students "
            "FROM tests t WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
            (owner_id, limit),
        ).fetchall()
    return [_test_dict(row) for row in rows]


def set_closed(test_id: int, closed: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET closed = ? WHERE id = ?", (int(closed), test_id))


def delete_test(test_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM tests WHERE id = ?", (test_id,))


def get_keys(test_id: int) -> dict[str, dict[str, str]]:
    """Variantlar kalitlari: {"A": {"1": "B", ...}, "B": {...}}."""
    with connect() as conn:
        rows = conn.execute("SELECT name, answers FROM variants WHERE test_id = ? ORDER BY name", (test_id,)).fetchall()
    return {row["name"]: json.loads(row["answers"]) for row in rows}


# ---------- Natijalar ----------

def _attempt_dict(row) -> dict | None:
    if row is None:
        return None
    attempt = dict(row)
    attempt["answers"] = json.loads(attempt["answers"])
    attempt["wrong"] = json.loads(attempt["wrong"])
    return attempt


def save_attempt(test_id: int, tg_id: int, student_name: str, variant: str, answers: dict, result: dict) -> dict:
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO attempts (test_id, tg_id, student_name, variant, answers, test_correct, written_correct, "
            "auto_total, wrong, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (test_id, tg_id, student_name, variant, json.dumps(answers, ensure_ascii=False),
             result["test_correct"], result["written_correct"], result["auto_total"],
             json.dumps(result["wrong"]), result["points"], _now()),
        ).fetchone()
    return _attempt_dict(row)


def update_attempt_result(attempt_id: int, answers: dict, result: dict, variant: str | None = None) -> None:
    """Javoblar o'zgardi (AI yozma javoblarni o'qidi yoki o'qituvchi tuzatdi) — natija qayta hisoblanib saqlanadi."""
    with connect() as conn:
        conn.execute(
            "UPDATE attempts SET answers = ?, test_correct = ?, written_correct = ?, auto_total = ?, wrong = ?, "
            "points = ?, variant = COALESCE(?, variant) WHERE id = ?",
            (json.dumps(answers, ensure_ascii=False), result["test_correct"], result["written_correct"],
             result["auto_total"], json.dumps(result["wrong"]), result["points"], variant, attempt_id),
        )


def get_attempt(attempt_id: int) -> dict | None:
    with connect() as conn:
        return _attempt_dict(conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone())


def find_attempt(test_id: int, tg_id: int) -> dict | None:
    with connect() as conn:
        return _attempt_dict(
            conn.execute("SELECT * FROM attempts WHERE test_id = ? AND tg_id = ?", (test_id, tg_id)).fetchone()
        )


def list_attempts(test_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM attempts WHERE test_id = ? ORDER BY id", (test_id,)).fetchall()
    return [_attempt_dict(row) for row in rows]


# ---------- Jonli kuzatuv ----------

def live_ping(test_id: int, tg_id: int, name: str, answered: int, total: int) -> None:
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            "INSERT INTO live (test_id, tg_id, name, answered, total, started_at, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(test_id, tg_id) DO UPDATE SET name = excluded.name, answered = excluded.answered, "
            "total = excluded.total, seen_at = excluded.seen_at",
            (test_id, tg_id, name, answered, total, now, now),
        )


def live_list(test_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM live WHERE test_id = ? ORDER BY started_at", (test_id,)).fetchall()
    return [dict(row) for row in rows]


def set_essay(attempt_id: int, score: float) -> None:
    with connect() as conn:
        conn.execute("UPDATE attempts SET essay = ? WHERE id = ?", (score, attempt_id))


# ---------- Esselar ----------

def _essay_dict(row) -> dict | None:
    if row is None:
        return None
    essay = dict(row)
    essay["photos"] = json.loads(essay["photos"] or "[]")
    essay["ai_result"] = json.loads(essay["ai_result"]) if essay["ai_result"] else None
    return essay


def create_essay(attempt_id: int, text: str | None, photos: list[str], status: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO essays (attempt_id, text, photos, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (attempt_id, text, json.dumps(photos), status, _now()),
        )


def get_essay(attempt_id: int) -> dict | None:
    with connect() as conn:
        return _essay_dict(conn.execute("SELECT * FROM essays WHERE attempt_id = ?", (attempt_id,)).fetchone())


def essays_for_test(test_id: int) -> dict[int, dict]:
    """{attempt_id: esse} — testning barcha esselari."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.* FROM essays e JOIN attempts a ON a.id = e.attempt_id WHERE a.test_id = ?", (test_id,)
        ).fetchall()
    return {row["attempt_id"]: _essay_dict(row) for row in rows}


def pending_essays(limit: int = 5) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.*, t.essay_topic AS topic, t.id AS test_id FROM essays e "
            "JOIN attempts a ON a.id = e.attempt_id JOIN tests t ON t.id = a.test_id "
            "WHERE e.status = 'pending' ORDER BY e.id LIMIT ?",
            (limit,),
        ).fetchall()
    return [_essay_dict(row) | {"topic": row["topic"], "test_id": row["test_id"]} for row in rows]


def save_essay_result(attempt_id: int, result: dict) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE essays SET status = 'graded', ai_score = ?, ai_result = ?, provider = ?, cost = cost + ?, "
            "graded_at = ?, error = NULL WHERE attempt_id = ?",
            (result["score"], json.dumps(result, ensure_ascii=False), result.get("provider"),
             result.get("cost", 0), _now(), attempt_id),
        )


def essay_failed(attempt_id: int, error: str, give_up: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE essays SET tries = tries + 1, error = ?, "
            "status = CASE WHEN ? = 1 THEN 'failed' ELSE status END WHERE attempt_id = ?",
            (error[:500], int(give_up), attempt_id),
        )


def add_essay_cost(attempt_id: int, cost: float) -> None:
    with connect() as conn:
        conn.execute("UPDATE essays SET cost = cost + ? WHERE attempt_id = ?", (cost, attempt_id))


def create_written_job(attempt_id: int, test_id: int, image_b64: str, mime: str, status: str = "pending") -> None:
    """Titulning yozma kataklari rasmi. status: pending — AI o'qiydi; manual — o'qituvchi rasmga qarab belgilaydi
    («✍️ Yozmalarni tekshirish»). Qo'lda tekshiriladigan rasm «AI o'qidi» xabarini chiqarmaydi (notified = 1)."""
    with connect() as conn:
        conn.execute("DELETE FROM written_jobs WHERE attempt_id = ?", (attempt_id,))
        conn.execute(
            "INSERT INTO written_jobs (attempt_id, test_id, image, mime, status, notified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, test_id, image_b64, mime, status, 1 if status == "manual" else 0, _now()),
        )


def written_images_for_test(test_id: int) -> set[int]:
    """Yozma kataklar rasmi saqlangan natijalar — qo'lda tekshirish ekrani shularni ko'rsatadi."""
    with connect() as conn:
        rows = conn.execute("SELECT attempt_id FROM written_jobs WHERE test_id = ? AND image IS NOT NULL",
                            (test_id,)).fetchall()
    return {row["attempt_id"] for row in rows}


def pending_written(limit: int = 5) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM written_jobs WHERE status = 'pending' ORDER BY created_at, attempt_id LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def written_done(attempt_id: int, result: dict) -> None:
    """O'qildi. Rasm qoladi — o'qituvchi AI o'qiganini rasm bilan solishtirib tuzata oladi (~60 KB)."""
    with connect() as conn:
        conn.execute(
            "UPDATE written_jobs SET status = 'done', error = NULL, result = ? WHERE attempt_id = ?",
            (json.dumps(result, ensure_ascii=False), attempt_id),
        )


def written_failed(attempt_id: int, error: str, give_up: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE written_jobs SET tries = tries + 1, error = ?, "
            "status = CASE WHEN ? = 1 THEN 'failed' ELSE status END WHERE attempt_id = ?",
            (error[:500], int(give_up), attempt_id),
        )


def get_written_job(attempt_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM written_jobs WHERE attempt_id = ?", (attempt_id,)).fetchone())


def written_jobs_for_test(test_id: int) -> dict[int, str]:
    """{attempt_id: holat} — natijalar ro'yxatida «AI o'qimoqda / o'qiy olmadi» belgisi uchun."""
    with connect() as conn:
        rows = conn.execute("SELECT attempt_id, status FROM written_jobs WHERE test_id = ?", (test_id,)).fetchall()
    return {row["attempt_id"]: row["status"] for row in rows}


def purge_written_images(days: int = 45) -> int:
    """Eski titullarning yozma kataklar rasmlari o'chiriladi (natija qoladi) — baza to'lib ketmasin."""
    before = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d %H:%M")
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE written_jobs SET image = NULL WHERE image IS NOT NULL AND status <> 'pending' AND created_at < ?",
            (before,),
        )
        return cursor.rowcount or 0


# ---------- Zaxira nusxa (backup) ----------

# Tiklash tartibi muhim: avval bog'lanadigan jadvallar. app_tokens (kirish sessiyalari — maxfiy) va
# live (vaqtinchalik) nusxaga kirmaydi; yozma kataklar rasmlari ham (hajmi katta, natija o'zi saqlangan)
BACKUP_TABLES = {
    "users": "*",
    "channels": "*",
    "tests": "*",
    "variants": "*",
    "attempts": "*",
    "essays": "*",
    "students": "*",
    "written_jobs": "attempt_id, test_id, mime, status, tries, error, result, notified, created_at",
    "offline_essays": "*",
    "classes": "*",
    "class_students": "*",
    "id_counters": "*",
    "subscriptions": "*",
    "usage": "*",
}
SERIAL_TABLES = ("tests", "attempts", "essays", "students", "classes", "class_students")


def export_tables() -> dict[str, list[dict]]:
    with connect() as conn:
        return {table: [dict(row) for row in conn.execute(f"SELECT {columns} FROM {table}").fetchall()]
                for table, columns in BACKUP_TABLES.items()}


def import_tables(data: dict[str, list[dict]], force: bool = False) -> dict[str, int]:
    """Zaxira nusxadan tiklash — bo'sh bazaga (force=True bo'lsa, mavjud yozuvlar ustiga qo'shmaydi, o'tkazib yuboradi)."""
    counts = {}
    with connect() as conn:
        if not force and conn.execute("SELECT COUNT(*) AS n FROM tests").fetchone()["n"]:
            raise ValueError("Baza bo'sh emas — tiklash faqat yangi (bo'sh) bazaga. Majburan: --force")
        for table in BACKUP_TABLES:
            rows = data.get(table) or []
            for row in rows:
                columns = list(row)
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
                    "ON CONFLICT DO NOTHING",
                    [row[c] for c in columns],
                )
            counts[table] = len(rows)
        if POSTGRES:  # avtomatik raqamlar tiklangan eng katta id'dan davom etsin
            for table in SERIAL_TABLES:
                conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                             f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
    return counts


# ---------- Sinflar (o'qituvchining doimiy o'quvchilar ro'yxati) ----------

MAX_ID = 9999  # titulda 4 xonali ID


def list_classes(owner_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM class_students s WHERE s.class_id = c.id) AS students "
            "FROM classes c WHERE c.owner_id = ? ORDER BY c.id", (owner_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def create_class(owner_id: int, name: str) -> dict:
    with connect() as conn:
        row = conn.execute("INSERT INTO classes (owner_id, name, created_at) VALUES (?, ?, ?) RETURNING *",
                           (owner_id, name, _now())).fetchone()
    return dict(row)


def get_class(class_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone())


def rename_class(class_id: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE classes SET name = ? WHERE id = ?", (name, class_id))


def delete_class(class_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM class_students WHERE class_id = ?", (class_id,))
        conn.execute("DELETE FROM classes WHERE id = ?", (class_id,))


def list_class_students(class_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM class_students WHERE class_id = ? ORDER BY name, number", (class_id,)).fetchall()]


def add_class_students(class_id: int, owner_id: int, names: list[str]) -> list[dict]:
    """Yangi o'quvchilar — o'qituvchining keyingi ID'lari bilan (o'chirilgan ID hech qachon qayta berilmaydi)."""
    with connect() as conn:
        counter = conn.execute("SELECT last FROM id_counters WHERE owner_id = ?", (owner_id,)).fetchone()
        present = conn.execute("SELECT COALESCE(MAX(number), 0) AS n FROM class_students WHERE owner_id = ?",
                               (owner_id,)).fetchone()["n"]
        last = max(counter["last"] if counter else 0, present)
        if last + len(names) > MAX_ID:
            raise ValueError(f"ID'lar tugadi (ko'pi bilan {MAX_ID})")
        for index, name in enumerate(names, start=1):
            conn.execute("INSERT INTO class_students (class_id, owner_id, number, name) VALUES (?, ?, ?, ?)",
                         (class_id, owner_id, last + index, name))
        conn.execute("INSERT INTO id_counters (owner_id, last) VALUES (?, ?) "
                     "ON CONFLICT(owner_id) DO UPDATE SET last = excluded.last", (owner_id, last + len(names)))
    return list_class_students(class_id)


def update_class_student(class_id: int, student_id: int, name: str | None = None, delete: bool = False) -> None:
    with connect() as conn:
        if delete:
            conn.execute("DELETE FROM class_students WHERE id = ? AND class_id = ?", (student_id, class_id))
        elif name:
            conn.execute("UPDATE class_students SET name = ? WHERE id = ? AND class_id = ?", (name, student_id, class_id))


# ---------- Ro'yxatdan o'tish (oflayn «Ona tili» kursi) ----------

def save_registration(tg_id: int, role: str, first_name: str, last_name: str, phone: str,
                      age: int | None, region: str, district: str) -> dict:
    """Yangi ariza yoki borini yangilash (bitta foydalanuvchi — bitta ariza).

    full_name — ism va familiyadan yig'iladi: ro'yxatlar va adminga xabarlarda shu ishlatiladi.
    O'qituvchida region/district — birinchi joyi (qolganlari teacher_places jadvalida).
    """
    full_name = f"{first_name} {last_name}".strip()
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO registrations (tg_id, role, first_name, last_name, full_name, phone, age, "
            "region, district, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET role = excluded.role, first_name = excluded.first_name, "
            "last_name = excluded.last_name, full_name = excluded.full_name, phone = excluded.phone, "
            "age = excluded.age, region = excluded.region, district = excluded.district, "
            "updated_at = excluded.updated_at RETURNING *",
            (tg_id, role, first_name, last_name, full_name, phone, age, region, district, _now(), _now()),
        ).fetchone()
    return dict(row)


# ---------- O'qituvchining dars beradigan joylari ----------

def set_teacher_places(tg_id: int, places: list[tuple[str, str]]) -> list[dict]:
    """O'qituvchining joylari to'liq almashtiriladi (ro'yxatni qayta yuborganda eskisi qolmaydi)."""
    with connect() as conn:
        conn.execute("DELETE FROM teacher_places WHERE tg_id = ?", (tg_id,))
        for region, district in places:
            conn.execute("INSERT INTO teacher_places (tg_id, region, district, created_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(tg_id, region, district) DO NOTHING", (tg_id, region, district, _now()))
    return list_teacher_places(tg_id)


def list_teacher_places(tg_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT region, district FROM teacher_places WHERE tg_id = ? ORDER BY region, district",
                            (tg_id,)).fetchall()
    return [dict(row) for row in rows]


def teachers_in(region: str, district: str) -> list[dict]:
    """Shu tumanda dars beradigan o'qituvchilar — o'quvchining arizasi shularga boradi."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.tg_id, r.full_name, r.phone, u.username FROM teacher_places p "
            "LEFT JOIN registrations r ON r.tg_id = p.tg_id LEFT JOIN users u ON u.tg_id = p.tg_id "
            "WHERE p.region = ? AND p.district = ?", (region, district)).fetchall()
    return [dict(row) for row in rows]


def districts_with_teachers() -> list[dict]:
    """Kurs bor joylar (o'quvchi tanlashda belgilab ko'rsatiladi)."""
    with connect() as conn:
        rows = conn.execute("SELECT region, district, COUNT(*) AS teachers FROM teacher_places "
                            "GROUP BY region, district").fetchall()
    return [dict(row) for row in rows]


def get_registration(tg_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM registrations WHERE tg_id = ?", (tg_id,)).fetchone())


def list_registrations(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT r.*, u.username FROM registrations r LEFT JOIN users u ON u.tg_id = r.tg_id "
            "ORDER BY r.id DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_registrations() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM registrations").fetchone()["n"]


# ---------- Obuna va AI sarfi ----------

def get_subscription(tg_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM subscriptions WHERE tg_id = ?", (tg_id,)).fetchone())


def set_subscription(tg_id: int, plan: str, expires_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO subscriptions (tg_id, plan, expires_at, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET plan = excluded.plan, expires_at = excluded.expires_at, "
            "updated_at = excluded.updated_at, reminded = NULL",
            (tg_id, plan, expires_at, _now()),
        )


def mark_reminded(tg_id: int, key: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE subscriptions SET reminded = ? WHERE tg_id = ?", (key, tg_id))


def list_subscriptions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, u.full_name, u.username FROM subscriptions s LEFT JOIN users u ON u.tg_id = s.tg_id "
            "ORDER BY s.expires_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def find_user(ref: str) -> dict | None:
    """Telegram ID yoki @username bo'yicha (bot bilan gaplashgan foydalanuvchi)."""
    ref = ref.strip()
    with connect() as conn:
        if ref.lstrip("-").isdigit():
            row = conn.execute("SELECT * FROM users WHERE tg_id = ?", (int(ref),)).fetchone()
            return dict(row) if row else {"tg_id": int(ref), "full_name": None, "username": None}
        row = conn.execute("SELECT * FROM users WHERE LOWER(username) = ?", (ref.lstrip("@").lower(),)).fetchone()
    return _row(row)


def get_usage(tg_id: int, month: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM usage WHERE tg_id = ? AND month = ?", (tg_id, month)).fetchone()
    return dict(row) if row else {"tg_id": tg_id, "month": month, "essays": 0, "written": 0, "notified": 0}


def consume_usage(tg_id: int, month: str, kind: str, amount: int, limit: int | None) -> bool:
    """AI sarfini yozadi — limitdan oshsa yozmaydi va False (bitta tranzaksiyada: ikki so'rov limitni yorib o'tmaydi)."""
    assert kind in ("essays", "written")
    with connect() as conn:
        conn.execute("INSERT INTO usage (tg_id, month) VALUES (?, ?) ON CONFLICT(tg_id, month) DO NOTHING", (tg_id, month))
        if limit is None:
            conn.execute(f"UPDATE usage SET {kind} = {kind} + ? WHERE tg_id = ? AND month = ?", (amount, tg_id, month))
            return True
        updated = conn.execute(
            f"UPDATE usage SET {kind} = {kind} + ? WHERE tg_id = ? AND month = ? AND {kind} + ? <= ?",
            (amount, tg_id, month, amount, limit),
        ).rowcount
    return bool(updated)


def take_limit_notice(tg_id: int, month: str) -> bool:
    """Limit tugagani haqida oyiga bir marta xabar — birinchi chaqiriqda True."""
    with connect() as conn:
        return bool(conn.execute("UPDATE usage SET notified = 1 WHERE tg_id = ? AND month = ? AND notified = 0",
                                 (tg_id, month)).rowcount)


# ---------- Sozlamalar (admin bot orqali o'zgartiradi) ----------

def get_settings() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


# ---------- Obuna to'lovlari (karta → chek → admin tasdiqlaydi) ----------

def create_payment(tg_id: int, period: str, amount: int) -> dict:
    """Yangi to'lov so'rovi. Shu foydalanuvchining eski tugallanmagan so'rovlari bekor qilinadi."""
    with connect() as conn:
        conn.execute("UPDATE payments SET status = 'canceled' WHERE tg_id = ? AND status IN ('pending', 'sent')",
                     (tg_id,))
        row = conn.execute(
            "INSERT INTO payments (tg_id, period, amount, created_at) VALUES (?, ?, ?, ?) RETURNING *",
            (tg_id, period, amount, _now()),
        ).fetchone()
    return dict(row)


def get_payment(payment_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT p.*, u.full_name, u.username FROM payments p LEFT JOIN users u ON u.tg_id = p.tg_id "
            "WHERE p.id = ?", (payment_id,)).fetchone())


def open_payment(tg_id: int) -> dict | None:
    """Foydalanuvchining tugallanmagan to'lovi (chek kutilmoqda yoki admin ko'rib chiqmoqda)."""
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM payments WHERE tg_id = ? AND status IN ('pending', 'sent') ORDER BY id DESC LIMIT 1",
            (tg_id,)).fetchone())


def attach_receipt(payment_id: int, file_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE payments SET receipt = ?, status = 'sent' WHERE id = ? AND status IN ('pending', 'sent')",
                     (file_id, payment_id))


def decide_payment(payment_id: int, status: str, admin_id: int) -> bool:
    """Admin tasdiqladi (paid) yoki rad etdi (rejected) — birinchi bosganda True (ikkinchi admin takrorlamaydi)."""
    with connect() as conn:
        return bool(conn.execute(
            "UPDATE payments SET status = ?, decided_at = ?, admin_id = ? WHERE id = ? AND status IN ('pending', 'sent')",
            (status, _now(), admin_id, payment_id)).rowcount)


def list_payments(status: str | None = None, limit: int = 30) -> list[dict]:
    with connect() as conn:
        sql = ("SELECT p.*, u.full_name, u.username FROM payments p LEFT JOIN users u ON u.tg_id = p.tg_id "
               + ("WHERE p.status = ? " if status else "") + "ORDER BY p.id DESC LIMIT ?")
        rows = conn.execute(sql, ((status, limit) if status else (limit,))).fetchall()
    return [dict(row) for row in rows]


# ---------- Titulli testning ilovada yozilgan essesi ----------

def get_offline_essay(test_id: int, number: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM offline_essays WHERE test_id = ? AND number = ?",
                                 (test_id, number)).fetchone())


def offline_essay_of_user(test_id: int, tg_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM offline_essays WHERE test_id = ? AND tg_id = ?",
                                 (test_id, tg_id)).fetchone())


def save_offline_essay(test_id: int, number: int, tg_id: int, name: str | None, text: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO offline_essays (test_id, number, tg_id, name, text, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(test_id, number) DO UPDATE SET name = excluded.name, text = excluded.text, "
            "created_at = excluded.created_at",
            (test_id, number, tg_id, name, text, _now()),
        )


def waiting_offline_essays(test_id: int) -> int:
    """Yuborilgan, lekin titul hali skanerlanmagan (natijaga biriktirilmagan) esselar soni."""
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM offline_essays e WHERE e.test_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM students s WHERE s.test_id = e.test_id AND s.number = e.number AND s.attempt_id IS NOT NULL)",
            (test_id,),
        ).fetchone()["n"]


def set_essay_text(attempt_id: int, text: str, status: str) -> None:
    """Natijaga esse matni (yangi yoki almashtirilgan) — AI navbatiga qayta qo'yiladi."""
    with connect() as conn:
        updated = conn.execute(
            "UPDATE essays SET text = ?, photos = '[]', status = ?, tries = 0, error = NULL, ai_score = NULL, "
            "ai_result = NULL, graded_at = NULL WHERE attempt_id = ?",
            (text, status, attempt_id),
        ).rowcount
        if not updated:
            conn.execute("INSERT INTO essays (attempt_id, text, photos, status, created_at) VALUES (?, ?, '[]', ?, ?)",
                         (attempt_id, text, status, _now()))


def retry_written(attempt_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE written_jobs SET status = 'pending', tries = 0, error = NULL, notified = 0 "
                     "WHERE attempt_id = ? AND image IS NOT NULL", (attempt_id,))


def written_status(test_id: int) -> dict:
    """Testning yozma javob navbati: {"pending": n, "done": n, "failed": n}."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM written_jobs WHERE test_id = ? GROUP BY status", (test_id,)
        ).fetchall()
    counts = {"pending": 0, "done": 0, "failed": 0}
    counts.update({row["status"]: row["n"] for row in rows})
    return counts


def take_written_notice(test_id: int) -> dict | None:
    """Navbat bo'shagan bo'lsa — hali xabar qilinmagan natijalar soni (va ular "xabar qilingan" bo'ladi)."""
    with connect() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM written_jobs WHERE test_id = ? AND status = 'pending'", (test_id,)
        ).fetchone()["n"]
        if pending:
            return None
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM written_jobs WHERE test_id = ? AND notified = 0 GROUP BY status",
            (test_id,),
        ).fetchall()
        conn.execute("UPDATE written_jobs SET notified = 1 WHERE test_id = ? AND notified = 0", (test_id,))
    counts = {row["status"]: row["n"] for row in rows}
    return counts if counts else None


def retry_essay(attempt_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE essays SET status = 'pending', tries = 0, error = NULL WHERE attempt_id = ?", (attempt_id,))


# ---------- Mini App ----------

def set_test_channels(test_id: int, channels: list[dict]) -> None:
    """Testning majburiy kanallari: [{"chat_id", "title", "url"}] — o'quvchi obuna bo'lmaguncha testni ocholmaydi."""
    with connect() as conn:
        conn.execute("UPDATE tests SET channels = ? WHERE id = ?", (json.dumps(channels, ensure_ascii=False), test_id))


def set_sheet_style(test_id: int, style: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET sheet_style = ? WHERE id = ?", (style, test_id))


def set_mode(test_id: int, mode: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET mode = ? WHERE id = ?", (mode, test_id))


def set_essay_photos(attempt_id: int, photos: list[str], status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE essays SET photos = ?, status = ?, tries = 0, error = NULL WHERE attempt_id = ?",
                     (json.dumps(photos), status, attempt_id))


def set_theme(test_id: int, theme: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE tests SET theme = ? WHERE id = ?", (theme, test_id))


def attempts_of_user(tg_id: int, limit: int = 50) -> list[dict]:
    """O'quvchining topshirgan testlari (yangi birinchi)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.id, a.variant, a.created_at, t.code, t.title, t.fmt, t.closed, t.theme "
            "FROM attempts a JOIN tests t ON t.id = a.test_id WHERE a.tg_id = ? ORDER BY a.id DESC LIMIT ?",
            (tg_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- Majburiy obuna ----------

def list_channels() -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM channels ORDER BY added_at").fetchall()]


def add_channel(chat_id: int, title: str, url: str, added_by: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO channels (chat_id, title, url, added_by, added_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, url = excluded.url",
            (chat_id, title, url, added_by, _now()),
        )


def remove_channel(chat_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))


def stats() -> dict:
    with connect() as conn:
        return {
            name: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for name, table in (("users", "users"), ("tests", "tests"), ("attempts", "attempts"))
        }


# ---------- Oflayn test: o'quvchilar ----------

def offline_tg_id(test_id: int, number: int) -> int:
    """Telegram'siz o'quvchi uchun ichki ID (manfiy — xabar yuborilmaydi, attempts jadvalida takrorlanmaydi)."""
    return -(test_id * 100000 + number)


def list_students(test_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM students WHERE test_id = ? ORDER BY number",
                                              (test_id,)).fetchall()]


def get_student(test_id: int, student_id: int | None = None, number: int | None = None) -> dict | None:
    with connect() as conn:
        if student_id is not None:
            row = conn.execute("SELECT * FROM students WHERE id = ? AND test_id = ?", (student_id, test_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM students WHERE number = ? AND test_id = ?", (number, test_id)).fetchone()
    return _row(row)


def add_students(test_id: int, names: list[str], variants: list[str], numbers: list[int] | None = None) -> list[dict]:
    """Ro'yxatga qo'shish. numbers — titulda bo'yalgan ID (bo'sh bo'lsa yoki band bo'lsa — keyingi bo'sh raqam)."""
    with connect() as conn:
        taken = {row["number"] for row in conn.execute("SELECT number FROM students WHERE test_id = ?", (test_id,))}
        for index, (name, variant) in enumerate(zip(names, variants)):
            wanted = numbers[index] if numbers and index < len(numbers) else None
            if not wanted or wanted in taken:
                wanted = max(taken, default=0) + 1
            taken.add(wanted)
            conn.execute("INSERT INTO students (test_id, number, name, variant) VALUES (?, ?, ?, ?)",
                         (test_id, wanted, name, variant))
    return list_students(test_id)


def update_student(test_id: int, student_id: int, name: str | None = None, variant: str | None = None) -> None:
    with connect() as conn:
        if name is not None:
            conn.execute("UPDATE students SET name = ? WHERE id = ? AND test_id = ?", (name, student_id, test_id))
        if variant is not None:
            conn.execute("UPDATE students SET variant = ? WHERE id = ? AND test_id = ?", (variant, student_id, test_id))


def delete_student(test_id: int, student_id: int) -> None:
    with connect() as conn:
        row = conn.execute("SELECT attempt_id FROM students WHERE id = ? AND test_id = ?", (student_id, test_id)).fetchone()
        if row and row["attempt_id"]:
            conn.execute("DELETE FROM attempts WHERE id = ?", (row["attempt_id"],))
        conn.execute("DELETE FROM students WHERE id = ? AND test_id = ?", (student_id, test_id))


def link_student_attempt(student_id: int, attempt_id: int | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE students SET attempt_id = ? WHERE id = ?", (attempt_id, student_id))


def delete_attempt(attempt_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE students SET attempt_id = NULL WHERE attempt_id = ?", (attempt_id,))
        conn.execute("DELETE FROM attempts WHERE id = ?", (attempt_id,))
