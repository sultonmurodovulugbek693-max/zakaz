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

import json
from datetime import datetime, timedelta, timezone

import db
from config import ADMIN_IDS, FREE_IDS, SALES_ENABLED

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


def granted_ids() -> set[int]:
    """Admin botda /ruxsat bilan ruxsat bergan o'qituvchilar (bazada — Render'ni o'zgartirish shart emas)."""
    raw = db.get_settings().get("allowed_ids", "")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def set_access(tg_id: int, allowed: bool) -> bool:
    """Ruxsat berish/olish. Qaytaradi: holat o'zgardimi."""
    ids = granted_ids()
    if (tg_id in ids) == allowed:
        return False
    ids = ids | {tg_id} if allowed else ids - {tg_id}
    db.set_setting("allowed_ids", ",".join(str(x) for x in sorted(ids)))
    return True


def can_manage(tg_id: int) -> bool:
    """Obuna tariflari va to'lov kartasini ilovada kiritadi: admin va Render'dagi FREE_IDS."""
    return tg_id in ADMIN_IDS or tg_id in FREE_IDS


def sales_on() -> bool:
    """Obuna sotilyaptimi: Render'da PLANS_ENABLED=1 yoki ilovadagi «💎 Obuna tariflari»da yoqilgan."""
    return SALES_ENABLED or db.get_settings().get("sales_on") == "1"


def credit_mode(tg_id: int) -> bool:
    """AI esse — o'qituvchining kreditidan: obuna sotilmayapti yoki /ruxsat bilan bepul qo'shilgan
    (obuna olmaydigan) o'qituvchi. Admin va FREE_IDS — cheksiz, ularga bu tekshiruv yetib bormaydi.
    Sotuv to'xtatilgan bo'lsa ham, to'langan obunasi tugamagan o'qituvchi oylik limitidan foydalanaveradi."""
    if tg_id in granted_ids() and not ai_unlimited(tg_id):
        return True
    if sales_on():
        return False
    sub = db.get_subscription(tg_id)
    return not (sub and sub["plan"] != "trial" and _expires(sub) > now())


# ---------- Ilovada kiritiladigan tariflar: «1 oy — 200 000», «3 oy — 600 000» ----------
# Kalit: m1, m3 … (oy soni). Tarif kiritilmagan bo'lsa — eski PERIODS (admin bot /sozlama) ishlaydi.
TARIFF_MONTHS_MAX = 36


def tariffs() -> list[dict]:
    try:
        rows = json.loads(db.get_settings().get("tariffs") or "[]")
    except (TypeError, ValueError):
        return []
    out = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            months, price = int(row["months"]), int(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= months <= TARIFF_MONTHS_MAX and price > 0:
            out[months] = price
    return [{"months": m, "price": out[m]} for m in sorted(out)]


def set_tariffs(rows: list[tuple[int, int]]) -> None:
    db.set_setting("tariffs", json.dumps([{"months": m, "price": p} for m, p in rows]))


def tariff_months(key: str) -> int | None:
    """«m3» → 3 (ilovadagi tarif kaliti), aks holda None."""
    key = str(key or "")
    return int(key[1:]) if key[:1] == "m" and key[1:].isdigit() and 1 <= int(key[1:]) <= TARIFF_MONTHS_MAX else None


# ---------- Begona o'qituvchi (admin / FREE_IDS emas): begonalar puli platformaga, ulushi o'qituvchi balansiga ----------
# Begona o'quvchi mock essesi yoki kredit paketi uchun to'laydi → pul PLATFORMA kartasiga (card()) → tasdiqlangach
# o'qituvchi balansiga ulushi (20%) yoziladi → balans withdraw_min ga yetsa «💸 Yechib olish» → admin o'tkazadi.
# AI xarajati platformaniki (shu 80% dan). platform_share = 0 — o'chiq: pul avvalgidek o'qituvchi kartasiga.
REVSHARE = {"platform_share": 80, "min_essay_price": 10_000, "withdraw_min": 100_000}


def platform_share() -> int:
    return max(0, min(100, setting("platform_share", REVSHARE["platform_share"])))


def platform_routed(teacher_id: int) -> bool:
    """Shu o'qituvchining begonalari to'lagan pul platformaga tushadimi (admin / FREE_IDS — o'z kartasiga)."""
    return not ai_unlimited(teacher_id) and platform_share() > 0


def teacher_cut(amount: int) -> int:
    """To'lovdan o'qituvchiga tegishli qism (so'm): 10 000 → 2 000 (platforma 80%)."""
    return max(0, amount - round(amount * platform_share() / 100))


def min_essay_price() -> int:
    """Begona o'qituvchida bitta esse (1 kredit) narxi kamida shuncha — AI xarajati va platforma ulushi qoplansin."""
    return max(0, setting("min_essay_price", REVSHARE["min_essay_price"]))


def withdraw_min() -> int:
    return max(1000, setting("withdraw_min", REVSHARE["withdraw_min"]))


def ai_unlimited(tg_id: int) -> bool:
    """AI cheksiz: admin va Render'dagi FREE_IDS. Botda /ruxsat bilan qo'shilgan o'qituvchi test yaratadi,
    lekin AI esse tekshiruvi uning kreditidan yechiladi (o'qituvchi to'laydi — o'quvchilari foydalanadi)."""
    return tg_id in ADMIN_IDS or tg_id in FREE_IDS


def is_free(tg_id: int) -> bool:
    """Admin, Render'dagi FREE_IDS yoki admin botda ruxsat bergan o'qituvchi — hammasi bepul va cheksiz."""
    return tg_id in ADMIN_IDS or tg_id in FREE_IDS or tg_id in granted_ids()


def essay_open(teacher_id: int) -> bool:
    """Bot hamma uchun bepul (test yaratish va yechish), faqat ESSE pullik: ustozning testlarida 45-esse bo'limi
    ochiqmi — admin / FREE_IDS / ruxsat berilgan yoki faol tarifi (obunasi) bor ustoz. Aks holda o'quvchilar
    testni essesiz yechadi. Ochiq bo'lsa begona o'quvchi esse uchun to'laydi yoki kredit ishlatadi."""
    if is_free(teacher_id):
        return True
    sub = db.get_subscription(teacher_id)
    return bool(sub) and _expires(sub) > now()


# ---------- Sozlamalar (admin botda o'zgartiradi) ----------

def setting(key: str, default=0) -> int:
    value = db.get_settings().get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def price_of(period: str) -> int:
    """Chegirma qo'llangandan keyingi narx (so'mda). Ilovadagi tarif (m3) — kiritilgan narx, chegirmasiz."""
    months = tariff_months(period)
    if months is not None:
        return next((t["price"] for t in tariffs() if t["months"] == months), 0)
    base = setting(f"price_{period}", PERIODS[period][2])
    discount = max(0, min(90, setting(f"discount_{period}", 0)))
    return max(0, round(base * (100 - discount) / 100 / 1000) * 1000)


def offers() -> list[dict]:
    """Ilova va botda ko'rsatiladigan tariflar ro'yxati: ilovada kiritilganlar, bo'lmasa — eski muddatlar."""
    custom = tariffs()
    if custom:
        return [{"key": f"m{t['months']}", "name": f"{t['months']} oy", "days": 30 * t["months"],
                 "price": t["price"], "final": t["price"], "discount": 0} for t in custom]
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
    if tariff_months(key) is not None:
        return f"{tariff_months(key)} oy"
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
    if not sales_on():  # obuna yopiq — FREE_IDS / admin va oldin to'langan (muddati tugamagan) obunalar
        current = status(tg_id)
        if current["active"] and current["plan"] != "trial":
            return current
        return {**current, "active": False, "closed": True}
    if not db.get_subscription(tg_id) and trial_days() > 0:
        db.set_subscription(tg_id, "trial", (now() + timedelta(days=trial_days())).isoformat(timespec="seconds"))
    return status(tg_id)


def grant(tg_id: int, period: str, count: int = 1) -> dict:
    """Obunani yoqish/uzaytirish. Faol obuna bo'lsa — muddat oxiriga qo'shiladi."""
    months = tariff_months(period)
    key = f"m{months}" if months is not None else ALIASES.get(str(period).lower().strip())
    if key is None:
        raise ValueError("Muddat noto'g'ri. Bor muddatlar: hafta, oy, 3oy, yil")
    count = max(1, min(24, count))
    sub = db.get_subscription(tg_id)
    start = _expires(sub) if sub and sub["plan"] != "trial" and _expires(sub) > now() else now()
    days = (30 * months if months is not None else PERIODS[key][1]) * count
    db.set_subscription(tg_id, key, (start + timedelta(days=days)).isoformat(timespec="seconds"))
    return status(tg_id)


def revoke(tg_id: int, period: str) -> dict:
    """AI tasdiqlagan obuna bekor qilindi (soxta chek): shu to'lov qo'shgan kunlar olib tashlanadi (hozirdan oldinga emas)."""
    months = tariff_months(period)
    key = f"m{months}" if months is not None else ALIASES.get(str(period).lower().strip())
    sub = db.get_subscription(tg_id)
    if key is None or not sub:
        return status(tg_id)
    days = 30 * months if months is not None else PERIODS[key][1]
    ends = max(now(), _expires(sub) - timedelta(days=days))
    db.set_subscription(tg_id, sub["plan"], ends.isoformat(timespec="seconds"))
    return status(tg_id)


def use_ai(tg_id: int, kind: str, amount: int = 1) -> bool:
    """AI ishlatishga ruxsat (va sarfni yozish). kind: "essays" | "written".
    Avval oylik limitdan, u tugasa — esse paketidan (faqat faol obuna bilan)."""
    if ai_unlimited(tg_id):
        db.consume_usage(tg_id, month_key(), kind, amount, None)
        return True
    if credit_mode(tg_id):
        # Obuna yopiq yoki /ruxsat bilan bepul o'qituvchi: esse tekshiruvi kreditidan (admin /kredit bilan to'ldiradi)
        if kind == "essays" and db.use_credits(tg_id, amount):
            db.consume_usage(tg_id, month_key(), kind, amount, None)
            return True
        return False
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


def can_use_ai(tg_id: int, kind: str = "essays") -> bool:
    """AI ishlatish mumkinmi — sarflamasdan (to'lov so'rashdan oldin: ustozning krediti/limiti bormi)."""
    if ai_unlimited(tg_id):
        return True
    has_credits = kind == "essays" and db.get_credits(tg_id) > 0
    if credit_mode(tg_id):
        return has_credits
    # use_ai bilan bir xil tartib: paket faqat faol obuna bilan ishlaydi (aks holda «bor» deb, sarflashda rad etardi)
    sub = db.get_subscription(tg_id)
    if not sub or _expires(sub) <= now():
        return False
    limit = (trial_limits() if sub["plan"] == "trial" else limits())[kind]
    return (db.get_usage(tg_id, month_key()).get(kind) or 0) < limit or has_credits


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
