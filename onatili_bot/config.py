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
