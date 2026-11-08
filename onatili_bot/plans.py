"""
Obuna: muddatlar (1 hafta / 1 oy / 3 oy / 1 yil), narxlar, to'lov va oylik AI limitlari.

  • FREE_IDS (Render → Environment) ro'yxatidagi foydalanuvchilarga hammasi bepul va cheksiz.
  • Qolganlar obuna sotib oladi: kartaga o'tkazadi → chekni botga yuboradi → admin tasdiqlaydi.
  • Obunasi yo'q yoki tugagan o'qituvchi yangi test yarata olmaydi; eski testlar, natijalar va
    o'quvchilar ishlayveradi.
  • Obunada AI oyiga ma'lum miqdorda esse tekshiradi (boshlang'ich 150 ta). Tugasa — o'qituvchi
    «📦 Esse paketi» sotib oladi (+100 esse, xuddi obunadek: karta → chek → admin). Paketdagi esselar
    muddatsiz — keyingi oyga o'tadi, faqat obuna faol bo'lganda ishlatiladi. Paket ham tugasa — esse
    o'qituvchining o'ziga (qo'lda baholash) qoladi.
  • Yozma javoblarni (36–44) AI faqat FREE_IDS ga o'qiydi — qolganlar rasmga qarab o'zi belgilaydi.
  • Admin(lar) (ADMIN_IDS) — cheklovsiz.

Narx, chegirma, karta raqami va sinov kunlarini ADMIN BOTDA o'zgartiradi (/sozlama). Shu yerdagi
qiymatlar — faqat boshlang'ich (sozlanmagan bo'lsa ishlatiladi).
"""

from datetime import datetime, timedelta, timezone

import db
from config import ADMIN_IDS, FREE_IDS

TASHKENT = timezone(timedelta(hours=5))

# kalit: (nomi, necha kun, boshlang'ich narx so'mda)
PERIODS = {
    "week":    ("1 hafta", 7,   29_000),
    "month":   ("1 oy",    30,  99_000),
    "quarter": ("3 oy",    90,  267_000),
    "year":    ("1 yil",   365, 950_000),
}
# Admin qo'lda yozadigan qisqa nomlar: /obuna @user oy
ALIASES = {"hafta": "week", "week": "week", "1hafta": "week",
           "oy": "month", "month": "month", "1oy": "month",
           "3oy": "quarter", "chorak": "quarter", "quarter": "quarter",
           "yil": "year", "year": "year", "1yil": "year"}

# Oylik AI limiti (har qanday faol obuna uchun bir xil), esse paketi va bepul sinov.
# Esse narxi (Claude Sonnet) ≈ 250–500 so'm: 150 ta esse 1 oylik obunadan arzonga tushadi, paket ham zarar qilmaydi
DEFAULTS = {"ai_essays": 150, "ai_written": 2000, "trial_days": 0, "trial_essays": 30, "trial_written": 60,
            "pack_essays": 100, "price_pack": 59_000}

# To'lovdagi «muddat» o'rnida — qo'shimcha esse paketi
PACK = "pack"


def now() -> datetime:
    return datetime.now(TASHKENT)


def month_key() -> str:
    return now().strftime("%Y-%m")


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def is_free(tg_id: int) -> bool:
    """Admin yoki Render'dagi FREE_IDS ro'yxatida — hammasi bepul va cheksiz."""
    return tg_id in ADMIN_IDS or tg_id in FREE_IDS


# ---------- Sozlamalar (admin botda o'zgartiradi) ----------

def setting(key: str, default=0) -> int:
    value = db.get_settings().get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def price_of(period: str) -> int:
    """Chegirma qo'llangandan keyingi narx (so'mda)."""
    base = setting(f"price_{period}", PERIODS[period][2])
    discount = max(0, min(90, setting(f"discount_{period}", 0)))
    return max(0, round(base * (100 - discount) / 100 / 1000) * 1000)


def offers() -> list[dict]:
    """Ilova va botda ko'rsatiladigan tariflar ro'yxati."""
    out = []
    for key, (name, days, default_price) in PERIODS.items():
        base = setting(f"price_{key}", default_price)
        discount = max(0, min(90, setting(f"discount_{key}", 0)))
        out.append({"key": key, "name": name, "days": days, "price": base,
                    "final": price_of(key), "discount": discount})
    return out


def card() -> dict:
    """To'lov kartasi — admin botda kiritadi."""
    saved = db.get_settings()
    return {"number": saved.get("card_number", ""), "holder": saved.get("card_holder", "")}


def limits() -> dict:
    return {"essays": setting("ai_essays", DEFAULTS["ai_essays"]),
            "written": setting("ai_written", DEFAULTS["ai_written"])}


def trial_limits() -> dict:
    return {"essays": setting("trial_essays", DEFAULTS["trial_essays"]),
            "written": setting("trial_written", DEFAULTS["trial_written"])}


def trial_days() -> int:
    return max(0, setting("trial_days", DEFAULTS["trial_days"]))


def pack() -> dict:
    """Qo'shimcha esse paketi: nechta esse va narxi (admin /sozlama da o'zgartiradi)."""
    return {"essays": max(1, setting("pack_essays", DEFAULTS["pack_essays"])),
            "price": max(0, setting("price_pack", DEFAULTS["price_pack"]))}


def add_pack(tg_id: int, essays: int) -> int:
    """Paket tasdiqlandi: esselar qo'shiladi (muddatsiz). Qaytaradi — endi jami nechta qo'shimcha esse bor."""
    balance = db.add_credits(tg_id, essays)
    db.reset_limit_notice(tg_id, month_key())  # paket ham tugasa — yana bir marta xabar beriladi
    return balance


# ---------- Obuna holati ----------

def _expires(sub: dict) -> datetime:
    return datetime.fromisoformat(sub["expires_at"])


def period_name(key: str) -> str:
    if key == PACK:
        return "Esse paketi"
    return PERIODS[key][0] if key in PERIODS else ("Sinov" if key == "trial" else key)


def payment_title(payment: dict) -> str:
    """Chek va to'lovlar ro'yxatida: «1 oy» yoki «📦 Esse paketi · +100 ta»."""
    if payment["period"] == PACK:
        return f"📦 Esse paketi · +{payment.get('essays') or pack()['essays']} ta"
    return period_name(payment["period"])


def status(tg_id: int) -> dict:
    """Ilova uchun: obuna, faolmi, necha kun qoldi, oylik AI limiti va sarf."""
    if is_free(tg_id):
        name = "Admin" if is_admin(tg_id) else "Bepul"
        return {"plan": "free", "name": name, "active": True, "unlimited": True, "days_left": None}
    sub = db.get_subscription(tg_id)
    used = db.get_usage(tg_id, month_key())
    spent = {"essays": used["essays"], "written": used["written"]}
    extra = {"credits": db.get_credits(tg_id), "pack": pack()}
    if not sub:
        return {"plan": None, "name": "Obuna yo'q", "active": False, "unlimited": False, "days_left": None,
                "trial_available": trial_days() > 0, "trial_days": trial_days(),
                "limits": limits(), "used": spent, **extra}
    left = _expires(sub) - now()
    return {
        "plan": sub["plan"], "name": period_name(sub["plan"]), "active": left.total_seconds() > 0, "unlimited": False,
        "days_left": max(0, left.days + (1 if left.seconds else 0)), "expires": _expires(sub).strftime("%d.%m.%Y"),
        "limits": trial_limits() if sub["plan"] == "trial" else limits(), "used": spent, **extra,
    }


def ensure_active(tg_id: int) -> dict:
    """Test yaratishdan oldin: obunasi yo'q bo'lsa — sinov yoqilgan bo'lsa boshlanadi, aks holda active=False."""
    if is_free(tg_id):
        return status(tg_id)
    if not db.get_subscription(tg_id) and trial_days() > 0:
        db.set_subscription(tg_id, "trial", (now() + timedelta(days=trial_days())).isoformat(timespec="seconds"))
    return status(tg_id)


def grant(tg_id: int, period: str, count: int = 1) -> dict:
    """Obunani yoqish/uzaytirish. Faol obuna bo'lsa — muddat oxiriga qo'shiladi."""
    key = ALIASES.get(str(period).lower().strip())
    if key is None:
        raise ValueError("Muddat noto'g'ri. Bor muddatlar: hafta, oy, 3oy, yil")
    count = max(1, min(24, count))
    sub = db.get_subscription(tg_id)
    start = _expires(sub) if sub and sub["plan"] != "trial" and _expires(sub) > now() else now()
    days = PERIODS[key][1] * count
    db.set_subscription(tg_id, key, (start + timedelta(days=days)).isoformat(timespec="seconds"))
    return status(tg_id)


def use_ai(tg_id: int, kind: str, amount: int = 1) -> bool:
    """AI ishlatishga ruxsat (va sarfni yozish). kind: "essays" | "written".
    Avval oylik limitdan, u tugasa — esse paketidan (faqat faol obuna bilan)."""
    if is_free(tg_id):
        db.consume_usage(tg_id, month_key(), kind, amount, None)
        return True
    sub = db.get_subscription(tg_id)
    if not sub or _expires(sub) <= now():
        return False
    limit = (trial_limits() if sub["plan"] == "trial" else limits())[kind]
    if db.consume_usage(tg_id, month_key(), kind, amount, limit):
        return True
    if kind == "essays" and db.use_credits(tg_id, amount):
        db.consume_usage(tg_id, month_key(), kind, amount, None)  # oylik hisobotda ham ko'rinsin
        return True
    return False


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ")


def price_list() -> str:
    lines = []
    for offer in offers():
        if offer["discount"]:
            lines.append(f"• <b>{offer['name']}</b> — <s>{money(offer['price'])}</s> "
                         f"<b>{money(offer['final'])}</b> so'm  🔥 −{offer['discount']}%")
        else:
            lines.append(f"• <b>{offer['name']}</b> — {money(offer['final'])} so'm")
    return "\n".join(lines)
