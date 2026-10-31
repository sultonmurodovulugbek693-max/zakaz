"""SQLite baza: testlar, variantlar (kalitlar) va o'quvchilar natijalari."""

import json
import random
import sqlite3
from datetime import datetime

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id      INTEGER PRIMARY KEY,
    full_name  TEXT,
    username   TEXT,
    last_name_used TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS tests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT UNIQUE NOT NULL,
    owner_id   INTEGER NOT NULL,
    title      TEXT NOT NULL,
    fmt        TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    items      TEXT NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS variants (
    test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    answers TEXT NOT NULL,
    PRIMARY KEY (test_id, name)
);
CREATE TABLE IF NOT EXISTS attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id         INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
    tg_id           INTEGER NOT NULL,
    student_name    TEXT NOT NULL,
    variant         TEXT NOT NULL,
    answers         TEXT NOT NULL,
    test_correct    INTEGER NOT NULL,
    written_correct INTEGER NOT NULL,
    auto_total      INTEGER NOT NULL,
    wrong           TEXT NOT NULL,
    points          REAL NOT NULL DEFAULT 0,
    essay           REAL,
    created_at      TEXT,
    UNIQUE (test_id, tg_id)
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(attempts)")}
        if "points" not in columns:  # eski baza — ball ustuni keyin qo'shilgan
            conn.execute("ALTER TABLE attempts ADD COLUMN points REAL NOT NULL DEFAULT 0")


# ---------- Foydalanuvchilar ----------

def remember_user(tg_id: int, full_name: str, username: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (tg_id, full_name, username, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET full_name = excluded.full_name, username = excluded.username",
            (tg_id, full_name, username, _now()),
        )


def last_name_used(tg_id: int) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT last_name_used FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    return row["last_name_used"] if row else None


def set_last_name_used(tg_id: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET last_name_used = ? WHERE tg_id = ?", (name, tg_id))


# ---------- Testlar ----------

def _test_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    test = dict(row)
    test["items"] = json.loads(test["items"])
    return test


def create_test(owner_id: int, title: str, fmt: str, count: int, items: list[dict],
                keys: dict[str, dict[str, str]]) -> dict:
    with connect() as conn:
        while True:
            code = str(random.randint(10000, 99999))
            if not conn.execute("SELECT 1 FROM tests WHERE code = ?", (code,)).fetchone():
                break
        cursor = conn.execute(
            "INSERT INTO tests (code, owner_id, title, fmt, count, items, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, owner_id, title, fmt, count, json.dumps(items, ensure_ascii=False), _now()),
        )
        test_id = cursor.lastrowid
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

def _attempt_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    attempt = dict(row)
    attempt["answers"] = json.loads(attempt["answers"])
    attempt["wrong"] = json.loads(attempt["wrong"])
    return attempt


def save_attempt(test_id: int, tg_id: int, student_name: str, variant: str, answers: dict, result: dict) -> dict:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO attempts (test_id, tg_id, student_name, variant, answers, test_correct, written_correct, "
            "auto_total, wrong, points, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (test_id, tg_id, student_name, variant, json.dumps(answers, ensure_ascii=False),
             result["test_correct"], result["written_correct"], result["auto_total"],
             json.dumps(result["wrong"]), result["points"], _now()),
        )
        row = conn.execute("SELECT * FROM attempts WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _attempt_dict(row)


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


def set_essay(attempt_id: int, score: float) -> None:
    with connect() as conn:
        conn.execute("UPDATE attempts SET essay = ? WHERE id = ?", (score, attempt_id))
