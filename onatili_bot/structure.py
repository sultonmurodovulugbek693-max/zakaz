"""
Ona tili test tuzilishi, javoblarni o'qish va tekshirish.

Ikki xil test bor:
  milliy — Milliy sertifikat (UZBMB, Ona tili va adabiyot), 180 daqiqa:
           1–32 test (A–D), 33–35 moslashtirish (A–F),
           36–39 yozma (bitta javob), 40–44 yozma (a va b),
           45 — esse (o'qituvchi 0–24 ball qo'yadi, avtomatik tekshirilmaydi)
  oddiy  — N ta oddiy test (A–D), yozmasiz, har savol 1 ball

Savollar bali — UZBMB rasmiy namunaviy test topshiriqlaridagidek
(Ona tili va adabiyot, namunaviy test: 1,1 / 1,7 / 2,5 ball; esse — 24 ball).
Hammasi qo'shilsa 100 ball: yopiq savollar 59,1 + yozma 16,9 + esse 24.
"""

import re

ESSAY_MAX = 24
MAX_TEXT_OPTIONS = 3  # yozma savolga nechta to'g'ri javob (sinonim) kiritish mumkin
VARIANT_NAMES = ["A", "B", "C", "D"]
LETTERS = "ABCDEF"

# Rasmiy namunadagi savollar bali
_POINTS_BY_NUMBER = {
    **{n: 1.1 for n in (1, 2, 3, 5, 6, 23, 24, 25, 26, 27)},
    **{n: 1.7 for n in (4, 7, 9, 10, 11, *range(13, 23), 33, 34, 35, 36, 38, 39)},
    **{n: 2.5 for n in (8, 12, 28, 29, 30, 31, 32, 37)},
}
_POINTS_BY_PART = {"40a": 1.2, "40b": 1.3, **{f"{n}a": 0.8 for n in range(41, 45)}, **{f"{n}b": 0.9 for n in range(41, 45)}}


def official_points(label: str) -> float:
    return _POINTS_BY_PART.get(label) or _POINTS_BY_NUMBER.get(int(label), 1.0)


def build_items(fmt: str, count: int = 0) -> list[dict]:
    """Test savollari ro'yxati: {"label": "33", "kind": "choice"|"text", "options": 6}."""
    if fmt == "milliy":
        items = [{"label": str(n), "kind": "choice", "options": 4} for n in range(1, 33)]
        items += [{"label": str(n), "kind": "choice", "options": 6} for n in range(33, 36)]
        items += [{"label": str(n), "kind": "text"} for n in range(36, 40)]
        items += [{"label": f"{n}{part}", "kind": "text"} for n in range(40, 45) for part in "ab"]
        for item in items:
            item["points"] = official_points(item["label"])
        return items
    return [{"label": str(n), "kind": "choice", "options": 4, "points": 1.0} for n in range(1, count + 1)]


def item_points(item: dict) -> float:
    return item.get("points", 1.0)


def max_points(items: list[dict], fmt: str) -> float:
    """Testning eng yuqori bali (Milliy sertifikatda esse bilan — 100)."""
    return round(sum(item_points(item) for item in items) + (ESSAY_MAX if fmt == "milliy" else 0), 1)


def choice_items(items: list[dict]) -> list[dict]:
    return [item for item in items if item["kind"] == "choice"]


def text_items(items: list[dict]) -> list[dict]:
    return [item for item in items if item["kind"] == "text"]


def format_title(fmt: str, count: int) -> str:
    if fmt == "milliy":
        return "Milliy sertifikat (45 savol)"
    return f"Oddiy test ({count} ta savol)"


class AnswerError(ValueError):
    """Foydalanuvchiga ko'rsatiladigan tushunarli xato."""


# ---------- Variantli javoblar: "abcd..." yoki "1a2b3c..." ----------

# Kirill klaviaturada yozilgan javob harflari: "а б с д е ф" / "а в с д" -> a b c d e f
_CYRILLIC_LETTERS = str.maketrans({"а": "a", "б": "b", "в": "b", "с": "c", "д": "d", "е": "e", "ф": "f"})

def parse_choices(text: str, items: list[dict], allow_blank: bool = False) -> dict[str, str]:
    """
    Variantli savollar javobini o'qiydi. Ikki xil yozish mumkin:
      abcdabcd...      — ketma-ket harflar (har savolga bitta)
      1a2b3c...        — raqam va harf (tartib muhim emas)
    allow_blank=True (o'quvchi): ketma-ket yozishda "-" yoki "x" — javob berilmagan.
    """
    items = choice_items(items)
    raw = "".join(text.split()).lower().translate(_CYRILLIC_LETTERS)
    if not raw:
        raise AnswerError("Javoblar bo'sh.")

    if any(char.isdigit() for char in raw):
        cleaned = re.sub(r"[.,;:)\-]", "", raw)
        pairs = re.findall(r"(\d+)([a-f])", cleaned)
        if not pairs or "".join(n + l for n, l in pairs) != cleaned:
            raise AnswerError("Format noto'g'ri. Masalan: 1a2b3c4d... yoki abcd...")
        answers: dict[str, str] = {}
        labels = {item["label"] for item in items}
        for number, letter in pairs:
            if number not in labels:
                raise AnswerError(f"{number}-savol bu testda yo'q.")
            if number in answers:
                raise AnswerError(f"{number}-savol ikki marta yozilgan.")
            answers[number] = letter.upper()
    else:
        if len(raw) != len(items):
            raise AnswerError(f"{len(items)} ta javob kerak, siz {len(raw)} ta yubordingiz.")
        answers = {}
        for item, char in zip(items, raw):
            if char in "-x_?" and allow_blank:
                continue
            answers[item["label"]] = char.upper()

    for item in items:
        value = answers.get(item["label"])
        allowed = LETTERS[: item["options"]]
        if value is None:
            if not allow_blank:
                raise AnswerError(f"{item['label']}-savolga javob yozilmagan.")
            continue
        if value not in allowed:
            raise AnswerError(f"{item['label']}-savol javobi {allowed[0]}–{allowed[-1]} bo'lishi kerak (siz: {value}).")
    return answers


# ---------- Yozma javoblar: har biri yangi qatorda ----------

_LABEL_PREFIX = re.compile(r"^\s*(\d{1,2})\s*([ab])?\s*[).:\-]\s*", re.IGNORECASE)


def parse_texts(text: str, items: list[dict], allow_blank: bool = False) -> dict[str, str]:
    """
    Yozma javoblar: har biri yangi qatorda, tartib bilan (36, 37, ..., 40a, 40b, ...).
    Qator boshida raqam yozish ham mumkin: "40a) kitob". "-" — javob yo'q.
    O'qituvchi 3 tagacha to'g'ri javobni (sinonimlar) "|" yoki "/" bilan yozadi:
    "chiroyli | go'zal | ko'rkam" — o'quvchi shulardan bittasini yozsa ham to'g'ri.
    """
    items = text_items(items)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    labels = [item["label"] for item in items]
    answers: dict[str, str] = {}

    labelled = [_LABEL_PREFIX.match(line) for line in lines]
    if lines and all(labelled):
        for line, match in zip(lines, labelled):
            label = match.group(1) + (match.group(2) or "").lower()
            if label not in labels:
                raise AnswerError(f"«{label}» — bunday yozma savol yo'q.")
            answers[label] = line[match.end():].strip()
    else:
        if len(lines) != len(labels):
            raise AnswerError(
                f"{len(labels)} ta qator kerak ({labels[0]} dan {labels[-1]} gacha), siz {len(lines)} ta yubordingiz."
            )
        answers = dict(zip(labels, lines))

    for label in labels:
        value = answers.get(label, "").strip()
        if value in ("", "-"):
            if not allow_blank:
                raise AnswerError(f"{label}: to'g'ri javob yozilmagan.")
            answers.pop(label, None)
        elif allow_blank:
            answers[label] = value[:200]  # o'quvchi javobi — bo'linmaydi
        else:
            options = split_options(value)
            if len(options) > MAX_TEXT_OPTIONS:
                raise AnswerError(f"{label}: ko'pi bilan {MAX_TEXT_OPTIONS} ta to'g'ri javob kiritish mumkin.")
            answers[label] = "|".join(option[:100] for option in options)
    return answers


def split_options(value: str) -> list[str]:
    """
    O'qituvchi kalitidagi to'g'ri javoblar: "a | b / c" -> ["a", "b", "c"].
    Tinish belgilaridan iborat javobda (37-savol) "/" ajratuvchi emas — faqat "|".
    """
    separator = r"\|" if _is_punctuation_only((value or "").replace("|", "")) else r"[|/]"
    return [option.strip() for option in re.split(separator, value or "") if option.strip()]


# ---------- Tekshirish ----------

_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʻ": "'", "ʼ": "'", "`": "'", "´": "'"})


_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "'": '"', "–": "-", "—": "-", "−": "-"})


def _is_punctuation_only(value: str) -> bool:
    return bool(value.strip()) and not any(char.isalnum() for char in value)


def normalize_punctuation(value: str) -> str:
    """37-savol kabi javoblar — faqat tinish belgilari: ': " , " .' — qo'shtirnoq/tire turlari va bo'shliq farqsiz."""
    return "".join((value or "").translate(_APOSTROPHES).translate(_QUOTES).split())


def normalize_text(value: str) -> str:
    """Katta-kichik harf, o' / oʻ / o` farqi, tinish belgilari va ortiqcha bo'shliq hisobga olinmaydi."""
    text = (value or "").lower().translate(_APOSTROPHES)
    text = re.sub(r"[.,;:!?\"«»“”„()]", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    return " ".join(text.split())


def text_is_correct(student: str, correct: str) -> bool:
    if not (student or "").strip():
        return False
    for option in split_options(correct):
        if _is_punctuation_only(option):
            if normalize_punctuation(option) == normalize_punctuation(student):
                return True
        elif normalize_text(student) and normalize_text(option) == normalize_text(student):
            return True
    return False


def grade(items: list[dict], key: dict[str, str], answers: dict[str, str]) -> dict:
    """Natija: to'g'ri javoblar soni, to'plangan ball (esse siz) va xato savollar ro'yxati."""
    test_correct = written_correct = 0
    points = 0.0
    wrong: list[str] = []
    for item in items:
        label = item["label"]
        given = answers.get(label, "")
        if item["kind"] == "choice":
            ok = given.upper() == key.get(label, "")
            test_correct += ok
        else:
            ok = text_is_correct(given, key.get(label, ""))
            written_correct += ok
        if ok:
            points += item_points(item)
        else:
            wrong.append(label)
    return {
        "points": round(points, 1),
        "test_correct": test_correct,
        "written_correct": written_correct,
        "auto_total": len(items),
        "wrong": wrong,
    }


def fmt_num(value: float) -> str:
    """63.4 -> "63,4", 24.0 -> "24" (o'zbekcha yozuv)."""
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def percent(correct: float, total: float) -> float:
    return round(correct * 100 / total, 1) if total else 0.0
