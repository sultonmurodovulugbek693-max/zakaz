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
    # Ilovaga (APK) Telegram orqali kirish: kind = login (bot tasdiqlashini kutadi) | session (kirilgan)
    f"""CREATE TABLE IF NOT EXISTS app_tokens (
        token   TEXT PRIMARY KEY,
        kind    TEXT NOT NULL,
        tg_id   {BIG},
        session TEXT,
        created {FLOAT} NOT NULL
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


def create_written_job(attempt_id: int, test_id: int, image_b64: str, mime: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM written_jobs WHERE attempt_id = ?", (attempt_id,))
        conn.execute(
            "INSERT INTO written_jobs (attempt_id, test_id, image, mime, created_at) VALUES (?, ?, ?, ?, ?)",
            (attempt_id, test_id, image_b64, mime, _now()),
        )


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
}
SERIAL_TABLES = ("tests", "attempts", "essays", "students")


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


def add_students(test_id: int, names: list[str], variants: list[str]) -> list[dict]:
    with connect() as conn:
        start = conn.execute("SELECT COALESCE(MAX(number), 0) AS n FROM students WHERE test_id = ?",
                             (test_id,)).fetchone()["n"]
        for index, (name, variant) in enumerate(zip(names, variants), start=1):
            conn.execute("INSERT INTO students (test_id, number, name, variant) VALUES (?, ?, ?, ?)",
                         (test_id, start + index, name, variant))
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
