"""Bot sozlamalari (.env faylidan o'qiladi)."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH") or str(BASE_DIR / "onatili.db")
FONTS_DIR = BASE_DIR / "fonts"

# Mini App manzili (https). Render o'zi RENDER_EXTERNAL_URL beradi — qo'lda yozish shart emas.
WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Admin(lar)ning Telegram ID'si — majburiy obuna kanallarini boshqaradi. Bir nechta: 123,456
# O'qituvchilar obuna uchun kimga yozadi (Telegram username, @ siz) — ilovada ko'rsatiladi
ADMIN_CONTACT = (os.getenv("ADMIN_CONTACT") or "").strip().lstrip("@")

ADMIN_IDS = {int(x) for x in (os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or "").replace(" ", "").split(",")
             if x.isdigit()}

# Bepul foydalanuvchilar: obuna ham, to'lov ham talab qilinmaydi (hammasi cheksiz).
# Render → Environment → FREE_IDS=123,456  (Telegram ID'lar; o'qituvchi /myid bilan biladi)
FREE_IDS = {int(x) for x in (os.getenv("FREE_IDS") or "").replace(" ", "").split(",") if x.isdigit()}
