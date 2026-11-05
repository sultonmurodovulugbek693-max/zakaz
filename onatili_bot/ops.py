"""
Botning "xizmat" ishlari — admin(lar) Telegram'da xabardor bo'lib turadi:

  • keep_alive   — Render'ning bepul tarifida server 15 daqiqa so'rovsiz qolsa uxlaydi (bot javob bermay qo'yadi).
                   Har 10 daqiqada o'z manziliga (/health) murojaat qilinadi. KEEP_ALIVE=0 — o'chirish.
  • daily_backup — har kuni (Toshkent vaqti bilan 03:00) baza nusxasi adminlarga fayl qilib yuboriladi.
                   Tiklash: python restore.py backup.json.gz
  • AlertHandler — ERROR darajasidagi har qanday xato adminlarga qisqa xabar bo'lib boradi (ko'pi bilan
                   5 daqiqada bir marta; orada bo'lganlari soni bilan keyingi xabarga qo'shiladi).
"""

import asyncio
import gzip
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import BufferedInputFile

import db
from config import ADMIN_IDS

logger = logging.getLogger("ops")

TASHKENT = timezone(timedelta(hours=5))
BACKUP_HOUR = 3
KEEP_ALIVE_EVERY = 600
ALERT_EVERY = 300
CONFLICT_GRACE = 180  # bot ikki joyda — Render yangilanishida shuncha soniya normal


# ---------- Uyg'oq tutish ----------

async def keep_alive(url: str) -> None:
    import aiohttp

    target = url.rstrip("/") + "/health"
    logger.info("Uyg'oq tutish yoqildi: har %s daqiqada %s", KEEP_ALIVE_EVERY // 60, target)
    while True:
        await asyncio.sleep(KEEP_ALIVE_EVERY)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(target) as response:
                    if response.status != 200:
                        logger.warning("Uyg'oq tutish: /health %s qaytardi", response.status)
        except Exception as error:  # tarmoq — keyingi safar
            logger.warning("Uyg'oq tutish: %s", error)


# ---------- Zaxira nusxa ----------

def make_backup() -> tuple[bytes, dict[str, int]]:
    data = db.export_tables()
    payload = {"made_at": datetime.now(TASHKENT).isoformat(timespec="seconds"), "tables": data}
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode()
    return gzip.compress(raw, 6), {table: len(rows) for table, rows in data.items()}


async def send_backup(bot: Bot, chat_ids=None) -> int:
    """Nusxani adminlarga yuboradi → nechtasiga yetdi."""
    chat_ids = list(chat_ids or ADMIN_IDS)
    if not chat_ids:
        logger.warning("Zaxira nusxa: ADMIN_IDS bo'sh — yuboriladigan odam yo'q")
        return 0
    content, counts = await asyncio.to_thread(make_backup)
    stamp = datetime.now(TASHKENT).strftime("%Y-%m-%d_%H-%M")
    caption = (f"💾 <b>Baza nusxasi</b> · {stamp.replace('_', ' ')}\n"
               f"📄 Testlar: {counts.get('tests', 0)} · natijalar: {counts.get('attempts', 0)} · "
               f"foydalanuvchilar: {counts.get('users', 0)}\n"
               "Tiklash: <code>python restore.py fayl.json.gz</code>")
    sent = 0
    for chat_id in chat_ids:
        try:
            await bot.send_document(chat_id, BufferedInputFile(content, filename=f"onatili_{stamp}.json.gz"),
                                    caption=caption)
            sent += 1
        except Exception as error:
            logger.warning("Zaxira nusxani %s ga yuborib bo'lmadi: %s", chat_id, error)
    return sent


def _seconds_until(hour: int) -> float:
    now = datetime.now(TASHKENT)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_backup(bot: Bot) -> None:
    while True:
        await asyncio.sleep(_seconds_until(BACKUP_HOUR))
        try:
            await send_backup(bot)
        except Exception:
            logger.warning("Kunlik zaxira nusxa bajarilmadi", exc_info=True)
        await asyncio.sleep(60)


# ---------- Xatolar haqida adminga xabar ----------

class AlertHandler(logging.Handler):
    """ERROR yozuvlarini navbatga qo'yadi; alert_sender ularni adminlarga yuboradi."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.queue: asyncio.Queue | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.conflict_since = self.conflict_last = 0.0

    def _conflict_is_noise(self, text: str) -> bool:
        """
        «Conflict: terminated by other getUpdates» — bot ikki joyda ishlayapti. Render yangilanayotganda eski va
        yangi nusxa 1–2 daqiqa birga ishlaydi — bu normal. Faqat 3 daqiqadan ortiq davom etsa xabar beriladi.
        """
        if "terminated by other getUpdates" not in text:
            return False
        now = time.time()
        if now - self.conflict_last > 300:  # yangi holat
            self.conflict_since = now
        self.conflict_last = now
        return now - self.conflict_since < CONFLICT_GRACE

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "ops" or self.queue is None or self.loop is None:
            return
        text = record.getMessage()
        if self._conflict_is_noise(text):
            return
        if "terminated by other getUpdates" in text:
            text = ("Bot bir vaqtda ikki joyda ishlayapti (3 daqiqadan beri). Kompyuterda yoki boshqa serverda "
                    "shu token bilan bot ishga tushirilganmi? Bittasini to'xtating.")
        if record.exc_info and record.exc_info[1] is not None:
            error = record.exc_info[1]
            text += f"\n{error.__class__.__name__}: {error}"
        line = f"[{record.name}] {text}"[:700]
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, line)
        except RuntimeError:  # sikl yopilgan (bot to'xtayapti)
            pass


ALERTS = AlertHandler()


async def alert_sender(bot: Bot) -> None:
    if not ADMIN_IDS:
        return
    ALERTS.loop, ALERTS.queue = asyncio.get_running_loop(), asyncio.Queue(maxsize=200)
    logging.getLogger().addHandler(ALERTS)
    last_sent = 0.0
    while True:
        first = await ALERTS.queue.get()
        await asyncio.sleep(max(0.0, ALERT_EVERY - (time.time() - last_sent)))  # ko'pi bilan 5 daqiqada bir
        lines = [first]
        while not ALERTS.queue.empty():
            lines.append(ALERTS.queue.get_nowait())
        unique = list(dict.fromkeys(lines))
        text = "⚠️ <b>Botda xato</b>\n" + "\n\n".join(_escape(line) for line in unique[:3])
        if len(lines) > 3:
            text += f"\n\n… yana {len(lines) - 3} ta xato (Render → Logs)"
        last_sent = time.time()
        for chat_id in ADMIN_IDS:
            try:
                await bot.send_message(chat_id, text[:4000])
            except Exception as error:
                logger.warning("Xato haqida xabar yuborilmadi: %s", error)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def keep_alive_url() -> str | None:
    if os.getenv("KEEP_ALIVE", "1") == "0":
        return None
    return os.getenv("RENDER_EXTERNAL_URL") or None
