"""
Majburiy obuna: foydalanuvchi admin qo'shgan barcha kanallarga obuna bo'lmaguncha
botdan ham, Mini App'dan ham foydalana olmaydi.

Telegram obunani faqat bot o'sha kanalda ADMIN bo'lsa tekshirishga ruxsat beradi.
Bot kanaldan chiqarib yuborilsa yoki adminlikdan olinsa, o'sha kanal tekshiruvda
o'tkazib yuboriladi (foydalanuvchilar bloklanib qolmasligi uchun) va logga yoziladi.
"""

import asyncio
import logging
import time

from aiogram import Bot

import db

logger = logging.getLogger("subscription")

MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}
OK_TTL = 300    # obuna tasdiqlansa — 5 daqiqa qayta tekshirilmaydi
FAIL_TTL = 20   # obuna yo'q bo'lsa — 20 soniyadan keyin qayta tekshiriladi
_cache: dict[int, tuple[float, list[dict]]] = {}


def forget(user_id: int | None = None) -> None:
    """Keshni tozalash: «Obuna bo'ldim» bosilganda yoki kanallar ro'yxati o'zgarganda."""
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)


async def missing_channels(bot: Bot, user_id: int) -> list[dict]:
    """Foydalanuvchi obuna bo'lmagan kanallar: [{"chat_id", "title", "url"}]."""
    cached = _cache.get(user_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    channels = await asyncio.to_thread(db.list_channels)
    missing = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel["chat_id"], user_id)
            status = member.status.value if hasattr(member.status, "value") else str(member.status)
            if status not in MEMBER_STATUSES or (status == "restricted" and not getattr(member, "is_member", True)):
                missing.append(channel)
        except Exception as error:  # bot kanalda admin emas yoki kanal o'chirilgan — bloklamaymiz
            logger.warning("Kanal %s tekshirilmadi: %s", channel["chat_id"], error)
    ttl = FAIL_TTL if missing else OK_TTL
    _cache[user_id] = (time.monotonic() + ttl, missing)
    return missing


_member_cache: dict[tuple[int, int], tuple[float, bool]] = {}


async def missing_in(bot: Bot, user_id: int, channels: list[dict]) -> list[dict]:
    """Testning o'z kanallari (o'qituvchi qo'shgan) — foydalanuvchi obuna bo'lmaganlari."""
    missing = []
    for channel in channels:
        key = (channel["chat_id"], user_id)
        cached = _member_cache.get(key)
        if cached and cached[0] > time.monotonic():
            is_member = cached[1]
        else:
            try:
                member = await bot.get_chat_member(channel["chat_id"], user_id)
                status = member.status.value if hasattr(member.status, "value") else str(member.status)
                is_member = status in MEMBER_STATUSES and not (status == "restricted" and not getattr(member, "is_member", True))
            except Exception as error:  # bot adminlikdan olingan — o'quvchini bloklamaymiz
                logger.warning("Test kanali %s tekshirilmadi: %s", channel["chat_id"], error)
                is_member = True
            _member_cache[key] = (time.monotonic() + (OK_TTL if is_member else FAIL_TTL), is_member)
        if not is_member:
            missing.append(channel)
    return missing


def forget_member(user_id: int) -> None:
    """«Obuna bo'ldim» bosilganda — shu foydalanuvchi uchun test kanallari qayta tekshiriladi."""
    for key in [k for k in _member_cache if k[1] == user_id]:
        _member_cache.pop(key, None)


async def resolve_channel(bot: Bot, ref: str | int) -> dict:
    """
    Admin yuborgan kanalni (@username, t.me havola, -100... ID) tekshiradi va saqlashga tayyorlaydi.
    Xato bo'lsa — tushunarli matn bilan ValueError.
    """
    if isinstance(ref, str):
        ref = ref.strip()
        for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
            if ref.startswith(prefix):
                ref = "@" + ref[len(prefix):].split("/")[0].split("?")[0]
        if ref.lstrip("-").isdigit():
            ref = int(ref)
        elif not ref.startswith("@"):
            ref = "@" + ref
    try:
        chat = await bot.get_chat(ref)
    except Exception:
        raise ValueError("Kanal topilmadi. @username'ni tekshiring yoki kanaldan xabar forward qiling.")
    if chat.type not in ("channel", "supergroup", "group"):
        raise ValueError("Bu kanal yoki guruh emas.")
    me = await bot.get_me()
    try:
        bot_member = await bot.get_chat_member(chat.id, me.id)
        status = bot_member.status.value if hasattr(bot_member.status, "value") else str(bot_member.status)
    except Exception:
        status = "left"
    if status not in ("administrator", "creator"):
        raise ValueError(f"Bot «{chat.title}» kanalida admin emas. Avval botni kanalga admin qiling, keyin qayta urining.")
    url = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    if not url:
        try:
            url = (await bot.create_chat_invite_link(chat.id, name="Majburiy obuna")).invite_link
        except Exception:
            raise ValueError("Yopiq kanal uchun taklif havolasini yaratib bo'lmadi — botga «Foydalanuvchilarni "
                             "taklif qilish» huquqini bering.")
    return {"chat_id": chat.id, "title": chat.title or str(chat.id), "url": url}
