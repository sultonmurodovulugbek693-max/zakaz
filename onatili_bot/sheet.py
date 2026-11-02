"""
Skanerlanadigan titul (javoblar varaqasi) — omr.py shu varaqni o'qiydi.

Bitta funksiya — layout() — har bir doiracha, belgi va katakning aniq joyini beradi.
PDF ham, skaner ham AYNAN shu joylashuvdan foydalanadi, shuning uchun ular hech qachon
bir-biridan farq qilmaydi (varaq qayta chizilsa ham — u testdan qayta hisoblanadi).

Varaq tuzilishi (Chittak 3-versiya titul o'lchamlarida):
  • burchaklarda 4 ta qora kvadrat, chap/o'ng chetda 10 tadan tekislash belgisi
  • pastki chetda 12 bitli kod: o'quvchi raqami (11 bit) + juftlik biti → skaner varaq
    kimniki ekanini o'zi taniydi; 0 — bo'sh (umumiy) titul
  • variant doirachalari (shaxsiy titulda belgilangan varianti oldindan bo'yalgan)
  • test savollari doirachalari, yozma javob kataklari (AI o'qiydi), ism katagi

Koordinatalar punktlarda; doirachalar PDF tizimida (y pastdan), kataklar — tepadan.
"""

import io
import math

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from structure import ESSAY_MAX, LETTERS, choice_items, format_title, text_items

W, H = A4
CORNER, CORNER_SIZE = 32, 14
EDGE_MARKS, EDGE_SIZE = 10, 8
CODE_BITS, CODE_SIZE = 12, 9
LEFT, RIGHT = 58, W - 58
BUBBLE_R = 6.2
LETTER_STEP = 17.5
ROW_STEP = 18.5
QUESTIONS_TOP = 176     # birinchi savol qatori markazi (tepadan)
VARIANT_TOP = 132
NAME_AREA = (100, 86, 392, 110)  # ism katagi (tepadan) — skanerda rasm qilib ko'rsatiladi

INK = colors.HexColor("#1f2933")
ACCENT = colors.HexColor("#1d6f42")
GRID = colors.HexColor("#9aa5b1")


def encode_number(number: int) -> int:
    """O'quvchi raqami (0..2047) → 12 bitli kod: 11 bit raqam + juftlik biti (xato o'qishni sezish uchun)."""
    number &= 0x7FF
    return number | ((bin(number).count("1") & 1) << 11)


def decode_number(code: int | None) -> int | None:
    if code is None:
        return None
    number, parity = code & 0x7FF, (code >> 11) & 1
    return number if parity == (bin(number).count("1") & 1) else None


def _choice_grid(count: int) -> tuple[int, int]:
    """(ustunlar, qatorlar) — savollar soniga qarab."""
    if count <= 36:
        columns = 3
    else:
        columns = min(4, math.ceil(count / 27))
    return columns, math.ceil(count / columns)


def layout(test: dict, variant_names: list[str]) -> dict:
    """Varaqdagi barcha elementlarning joyi: {"bubbles", "written", "written_area", "name_area", "bottom"}."""
    bubbles: list[dict] = []

    step = (H - 2 * CORNER) / (EDGE_MARKS + 1)
    for index in range(1, EDGE_MARKS + 1):
        y = CORNER + index * step
        for side, x in (("left", CORNER), ("right", W - CORNER)):
            bubbles.append({"page": 1, "kind": "mark", "ref": side, "x": x, "y": y, "r": EDGE_SIZE / 2})

    first_x, last_x = CORNER + 48, W - CORNER - 48
    gap = (last_x - first_x) / (CODE_BITS - 1)
    for bit in range(CODE_BITS):
        bubbles.append({"page": 1, "kind": "code", "ref": "code", "letter": str(bit),
                        "x": first_x + bit * gap, "y": CORNER, "r": CODE_SIZE / 2})

    for index, name in enumerate(variant_names or ["A"]):
        bubbles.append({"page": 1, "kind": "variant", "ref": "variant", "letter": name, "label": "variant",
                        "x": 142 + index * 32, "y": H - VARIANT_TOP, "r": 7})

    choices = choice_items(test["items"])
    columns, rows = _choice_grid(len(choices))
    column_width = (RIGHT - LEFT) / columns
    for index, item in enumerate(choices):
        column, row = divmod(index, rows)
        x0 = LEFT + column * column_width
        y = H - (QUESTIONS_TOP + row * ROW_STEP)
        for option in range(item["options"]):
            bubbles.append({"page": 1, "kind": "single", "ref": item["label"], "label": item["label"],
                            "letter": LETTERS[option], "x": x0 + 32 + option * LETTER_STEP, "y": y, "r": BUBBLE_R})
    choices_bottom = QUESTIONS_TOP + (rows - 1) * ROW_STEP + 14

    written = []
    texts = text_items(test["items"])
    written_top = choices_bottom + 34
    if texts:
        text_rows = math.ceil(len(texts) / 2)
        box_h, box_gap = 27, 5
        half = (RIGHT - LEFT - 12) / 2
        for index, item in enumerate(texts):
            column, row = divmod(index, text_rows)
            x = LEFT + column * (half + 12)
            top = written_top + row * (box_h + box_gap)
            written.append({"label": item["label"], "x": x, "top": top, "w": half, "h": box_h})
        bottom = written_top + text_rows * (box_h + box_gap)
        written_area = (LEFT - 4, written_top - 4, RIGHT + 4, bottom + 2)
    else:
        bottom, written_area = choices_bottom, None
    return {"bubbles": bubbles, "written": written, "written_area": written_area, "name_area": NAME_AREA,
            "choices_columns": (columns, rows, column_width), "bottom": bottom}


def _square(c: canvas.Canvas, x: float, y: float, size: float) -> None:
    c.rect(x - size / 2, y - size / 2, size, size, stroke=0, fill=1)


def draw_answer_page(c: canvas.Canvas, test: dict, variant_names: list[str], student: dict | None) -> None:
    """Bitta skanerlanadigan titul sahifasi. student = {"number", "name", "variant"} yoki None (bo'sh titul)."""
    spec = layout(test, variant_names)
    c.setFillColor(colors.black)
    for x, y in [(CORNER, H - CORNER), (W - CORNER, H - CORNER), (CORNER, CORNER), (W - CORNER, CORNER)]:
        _square(c, x, y, CORNER_SIZE)
    code = encode_number(student["number"]) if student else 0
    for bubble in spec["bubbles"]:
        if bubble["kind"] == "mark":
            _square(c, bubble["x"], bubble["y"], EDGE_SIZE)
        elif bubble["kind"] == "code" and code >> int(bubble["letter"]) & 1:
            _square(c, bubble["x"], bubble["y"], CODE_SIZE)

    # Sarlavha (tepadagi chiziq — burchaklar orasi — bo'sh qolishi shart)
    c.setFillColor(ACCENT)
    c.setFont("Sans-Bold", 13)
    c.drawString(LEFT, H - 62, "ONA TILI — JAVOBLAR VARAQASI")
    c.setFillColor(INK)
    c.drawRightString(RIGHT, H - 62, f"Kod {test['code']}")
    c.setFont("Sans", 8.5)
    c.drawString(LEFT, H - 76, f"{test['title'][:70]} · {format_title(test['fmt'], test['count'])}")

    # Ism katagi va o'quvchi raqami
    c.setFont("Sans-Bold", 9.5)
    c.drawString(LEFT, H - 104, "F.I.Sh.:")
    x1, top, x2, bottom = NAME_AREA
    c.setStrokeColor(GRID)
    c.setLineWidth(0.7)
    c.line(x1 + 2, H - bottom + 4, x2, H - bottom + 4)
    if student and student.get("name"):
        c.setFont("Sans-Bold", 12)
        c.drawString(x1 + 6, H - bottom + 8, student["name"][:40])
    c.setFont("Sans-Bold", 9.5)
    c.drawString(410, H - 104, f"№ {student['number']:03d}" if student else "№ ____")

    # Variant
    c.drawString(LEFT, H - VARIANT_TOP - 3, "Variant:")
    for bubble in spec["bubbles"]:
        if bubble["kind"] != "variant":
            continue
        filled = bool(student) and student.get("variant") == bubble["letter"]
        c.setFillColor(INK)
        c.setFont("Sans-Bold", 9)
        c.drawRightString(bubble["x"] - 9.5, bubble["y"] - 3, bubble["letter"])
        _bubble(c, bubble["x"], bubble["y"], bubble["r"], "", filled)
    c.setFont("Sans", 8)
    c.setFillColor(INK)
    hint = "Variantingiz belgilangan" if student else "Variantingizni bo'yang"
    text = f"{hint} · javobni to'liq bo'yang:"
    c.drawString(290, H - VARIANT_TOP - 3, text)
    end = 290 + pdfmetrics.stringWidth(text, "Sans", 8)
    _bubble(c, end + 10, H - VARIANT_TOP, 5, "", True)
    c.drawString(end + 19, H - VARIANT_TOP - 3, "to'g'ri")

    # Test savollari
    choices = choice_items(test["items"])
    columns, rows, column_width = spec["choices_columns"]
    c.setFont("Sans-Bold", 9.5)
    c.setFillColor(ACCENT)
    c.drawString(LEFT, H - QUESTIONS_TOP + 22, f"{choices[0]['label']}–{choices[-1]['label']} · test savollari")
    # Doirachalar BO'SH chiziladi (ichidagi harf skanerga "qisman bo'yalgan" bo'lib ko'rinadi) —
    # harflar har ustun tepasida sarlavha qilib yoziladi (Chittak titulidagidek)
    labels_x, headers = {}, {}
    for bubble in spec["bubbles"]:
        if bubble["kind"] != "single":
            continue
        if bubble["label"] not in labels_x:
            labels_x[bubble["label"]] = bubble
            c.setFillColor(INK)
            c.setFont("Sans-Bold", 9)
            c.drawRightString(bubble["x"] - 12, bubble["y"] - 3, bubble["label"])
        column_key = (round(bubble["x"]), bubble["letter"])
        headers.setdefault(column_key, bubble)
        _bubble(c, bubble["x"], bubble["y"], bubble["r"], "", False)
    c.setFillColor(GRID)
    c.setFont("Sans-Bold", 7)
    top_row_y = H - QUESTIONS_TOP + 9.5
    for (x, letter), bubble in headers.items():
        c.drawCentredString(bubble["x"], top_row_y, letter)

    # Yozma javob kataklari
    if spec["written"]:
        first, last = spec["written"][0]["label"], spec["written"][-1]["label"]
        c.setFont("Sans-Bold", 9.5)
        c.setFillColor(ACCENT)
        c.drawString(LEFT, H - spec["written"][0]["top"] + 8, f"{first.rstrip('ab')}–{last.rstrip('ab')} · yozma javoblar (aniq, bosma harflarda yozing)")
        c.setStrokeColor(GRID)
        for box in spec["written"]:
            c.setFillColor(INK)
            c.setFont("Sans-Bold", 9)
            c.drawString(box["x"] + 4, H - box["top"] - box["h"] / 2 - 3, box["label"])
            c.rect(box["x"] + 28, H - box["top"] - box["h"], box["w"] - 28, box["h"], stroke=1, fill=0)

    c.setFillColor(INK)
    c.setFont("Sans", 8)
    y = H - spec["bottom"] - 20
    if test["fmt"] == "milliy":
        c.drawString(LEFT, y, f"45 — Esse esse varaqasiga yoziladi ({ESSAY_MAX} ball). Varaqni bukmang, burchakdagi "
                              "qora kvadratlar ustiga yozmang.")
    else:
        c.drawString(LEFT, y, "Varaqni bukmang, burchakdagi qora kvadratlar ustiga yozmang.")
    c.setFillColor(GRID)
    c.setFont("Sans", 7)
    c.drawCentredString(W / 2, CORNER + 14, "Ona tili test boti · titulni o'qituvchi ilova orqali skanerlaydi")


def _bubble(c: canvas.Canvas, x: float, y: float, r: float, letter: str, filled: bool) -> None:
    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    if filled:
        c.setFillColor(colors.black)
        c.circle(x, y, r, stroke=1, fill=1)
        return
    c.setFillColor(colors.white)
    c.circle(x, y, r, stroke=1, fill=1)
    if letter:
        c.setFillColor(INK)
        c.setFont("Sans", 7)
        c.drawCentredString(x, y - 2.4, letter)


def sheets_pdf(test: dict, variant_names: list[str], students: list[dict], blanks: int = 0) -> bytes:
    """Har o'quvchiga shaxsiy titul (+ milliyda esse varaqasi) va kerak bo'lsa bo'sh titullar."""
    import pdfs  # shriftlar ro'yxatdan o'tgan bo'lsin; esse sahifasi shu yerda chiziladi

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Titullar — {test['title']}")
    pages = [*students, *([None] * blanks)] or [None]
    for student in pages:
        draw_answer_page(c, test, variant_names, student)
        c.showPage()
        if test["fmt"] == "milliy":
            pdfs.draw_essay_page(c, test, student)
            c.showPage()
    c.save()
    return buffer.getvalue()
