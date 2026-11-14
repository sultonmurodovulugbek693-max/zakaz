"""Testlar uchun muhit: vaqtinchalik SQLite baza, AI "yoqilgan" (lekin tarmoqqa chiqmaydi — testlar uni almashtiradi).
Haqiqiy .env dagi kalitlar va baza ishlatilmaydi."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="onatili_test_")
os.environ.update({
    "DB_PATH": os.path.join(TMP, "test.db"),
    "DATABASE_URL": "",
    "AI_PROVIDER": "gemini",
    "GEMINI_API_KEY": "test-key",
    "ADMIN_IDS": "",
    "KEEP_ALIVE": "0",
    "PLANS_ENABLED": "1",  # obuna mantig'i testlanadi; yopiq holat — test_fixes.SalesClosed
})
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402


def fresh_db() -> None:
    """Har test uchun toza baza."""
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init()
