"""
Bazani zaxira nusxadan tiklash (bot har kuni adminga yuboradigan onatili_*.json.gz fayli).

  python restore.py onatili_2026-11-06_03-00.json.gz           # yangi (bo'sh) bazaga
  python restore.py onatili_2026-11-06_03-00.json.gz --force   # bazada yozuv bo'lsa ham (borlari o'tkazib yuboriladi)

Qaysi bazaga: .env dagi DATABASE_URL (PostgreSQL) yoki u bo'sh bo'lsa — SQLite (DB_PATH).
Yozma kataklar rasmlari nusxaga kirmaydi (natijalar to'liq tiklanadi).
"""

import gzip
import json
import sys

import config  # noqa: F401 — .env yuklansin
import db


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        raise SystemExit(__doc__)
    with gzip.open(args[0], "rt", encoding="utf-8") as file:
        payload = json.load(file)
    db.init()
    print(f"Baza: {db.backend_name()} · nusxa: {payload.get('made_at', '?')}")
    try:
        counts = db.import_tables(payload["tables"], force="--force" in sys.argv)
    except ValueError as error:
        raise SystemExit(f"❌ {error}")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    print("✅ Tiklandi")


if __name__ == "__main__":
    main()
