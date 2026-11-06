"""
O'qituvchilar obunasi: tariflar, muddat va oylik AI limitlari.

  • Yangi o'qituvchi birinchi testini yaratganda — bepul sinov (TRIAL_DAYS kun, kichik limit).
  • Pulli tarifni admin beradi: botda /obuna @username start 1  (tarif, necha oy).
  • Obuna tugasa — yangi test yaratib bo'lmaydi; eski testlar, natijalar va o'quvchilar ishlayveradi.
  • AI limiti (oyiga) tugasa — esse va yozma javoblar o'qituvchining o'ziga (qo'lda) qoladi, AI chaqirilmaydi.
  • Admin(lar) (ADMIN_IDS) — cheklovsiz.

Narx va limitlarni shu yerda o'zgartirasiz (narx — so'mda, oyiga; botda va ilovada shu ko'rsatiladi).
"""

from datetime import datetime, timedelta, timezone

import db
from config import ADMIN_IDS

TASHKENT = timezone(timedelta(hours=5))
TRIAL_DAYS = 14

# kalit: (nomi, oylik narx so'mda, AI esse limiti, AI titul (yozma javoblar) limiti)
PLANS = {
    "trial":  {"name": "Sinov",  "price": 0,      "essays": 30,   "written": 60},
    "start":  {"name": "Start",  "price": 99_000,  "essays": 300,  "written": 600},
    "pro":    {"name": "Pro",    "price": 249_000, "essays": 1500, "written": 3000},
    "markaz": {"name": "Markaz", "price": 690_000, "essays": 6000, "written": 10000},
}
PAID = [key for key in PLANS if key != "trial"]


def now() -> datetime:
    return datetime.now(TASHKENT)


def month_key() -> str:
    return now().strftime("%Y-%m")


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def _expires(sub: dict) -> datetime:
    return datetime.fromisoformat(sub["expires_at"])


def status(tg_id: int) -> dict:
    """Ilova uchun: tarif, faolmi, necha kun qoldi, oylik limit va sarf."""
    if is_admin(tg_id):
        return {"plan": "admin", "name": "Admin", "active": True, "unlimited": True, "days_left": None}
    sub = db.get_subscription(tg_id)
    used = db.get_usage(tg_id, month_key())
    if not sub:
        return {"plan": None, "name": "Obuna yo'q", "active": False, "unlimited": False, "days_left": None,
                "trial_available": True, "trial_days": TRIAL_DAYS,
                "used": {"essays": used["essays"], "written": used["written"]}}
    plan = PLANS.get(sub["plan"], PLANS["trial"])
    left = _expires(sub) - now()
    return {
        "plan": sub["plan"], "name": plan["name"], "active": left.total_seconds() > 0, "unlimited": False,
        "days_left": max(0, left.days + (1 if left.seconds else 0)), "expires": _expires(sub).strftime("%d.%m.%Y"),
        "limits": {"essays": plan["essays"], "written": plan["written"]},
        "used": {"essays": used["essays"], "written": used["written"]},
    }


def ensure_active(tg_id: int) -> dict:
    """Test yaratishdan oldin: obuna yo'q bo'lsa — bepul sinov boshlanadi. Tugagan bo'lsa — status (active=False)."""
    if is_admin(tg_id):
        return status(tg_id)
    if not db.get_subscription(tg_id):
        db.set_subscription(tg_id, "trial", (now() + timedelta(days=TRIAL_DAYS)).isoformat(timespec="seconds"))
    return status(tg_id)


def grant(tg_id: int, plan: str, months: int) -> dict:
    """Admin: tarif berish/uzaytirish. Faol obuna bo'lsa — muddat oxiriga qo'shiladi."""
    if plan not in PLANS:
        raise ValueError(f"Tarif noto'g'ri. Bor tariflar: {', '.join(PAID)}")
    months = max(1, min(24, months))
    sub = db.get_subscription(tg_id)
    start = _expires(sub) if sub and sub["plan"] != "trial" and _expires(sub) > now() else now()
    db.set_subscription(tg_id, plan, (start + timedelta(days=30 * months)).isoformat(timespec="seconds"))
    return status(tg_id)


def use_ai(tg_id: int, kind: str, amount: int = 1) -> bool:
    """AI ishlatishga ruxsat (va sarfni yozish). kind: "essays" | "written". Obuna tugagan bo'lsa ham — False."""
    if is_admin(tg_id):
        db.consume_usage(tg_id, month_key(), kind, amount, None)
        return True
    sub = db.get_subscription(tg_id)
    if not sub or _expires(sub) <= now():
        return False
    limit = PLANS.get(sub["plan"], PLANS["trial"])[kind]
    return db.consume_usage(tg_id, month_key(), kind, amount, limit)


def price_list() -> str:
    lines = []
    for key in PAID:
        plan = PLANS[key]
        lines.append(f"• <b>{plan['name']}</b> — {plan['price']:,} so'm/oy · AI: {plan['essays']} esse, "
                     f"{plan['written']} titul".replace(",", " "))
    return "\n".join(lines)
