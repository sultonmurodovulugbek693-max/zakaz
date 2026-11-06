"""
Skanerlanadigan titul (javoblar varaqasi) — omr.py shu varaqni o'qiydi.

Bitta funksiya — layout() — har bir doiracha, belgi va katakning aniq joyini beradi.
PDF ham, skaner ham AYNAN shu joylashuvdan foydalanadi, shuning uchun ular hech qachon
bir-biridan farq qilmaydi (varaq qayta chizilsa ham — u testdan qayta hisoblanadi).

Varaq tuzilishi:
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
QUESTIONS_TOP = 320     # birinchi savol qatori markazi (tepadan)
VARIANT_TOP = 132
# O'quvchi ID'si (imtihon blankalaridagidek): tepada 4 ta katak — raqam yoziladi, ostida 4 ta tik ustun,
# har ustunda 0–9 doirachalar — har ustunda bittasi bo'yaladi. Shaxsiy titulda oldindan bo'yalgan,
# bo'sh titulda o'quvchi o'zi to'ldiradi — skaner varaq kimniki ekanini shundan ham taniydi
ID_DIGITS = 4
ID_BOX_TOP, ID_BOX = 146, 16          # raqam yoziladigan kataklar (tepasi, o'lchami)
ID_TOP, ID_ROW = 174, 11.5            # 0-qator doirachalari markazi, qatorlar orasi
ID_X0, ID_STEP, ID_R = 88, 20, 4.7    # 1-ustun markazi, ustunlar orasi, doiracha radiusi
MAX_STUDENTS = 2047                   # titul pastidagi kod 11 bit
BLOCK_ROWS = 5          # savollar 5 tadan blok; blok burchaklarida qora kvadratcha
ANCHOR_SIZE = 6
NAME_AREA = (100, 86, 392, 110)  # ism katagi (tepadan) — skanerda rasm qilib ko'rsatiladi
WRITTEN_CELL_W, WRITTEN_CELL_H, WRITTEN_LABEL_W = 14.5, 21, 30  # yozma javobning harf kataklari

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
    """
    Varaqdagi barcha elementlarning joyi: {"bubbles", "written", "written_area", "name_area", "page"}.
    page — titul o'lchami (omr.SheetPage maydonlari); standart A4 titulda None.
    Ixcham / yirik doirachali / fan ustunli ko'rinishlar — sheet_styles.py da.
    """
    import sheet_styles
    if sheet_styles.style_of(test) != "standard":
        return sheet_styles.layout(test, variant_names)
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

    for column in range(ID_DIGITS):  # ref id0 — eng chap raqam (skaner ref tartibida o'qiydi)
        for digit in range(10):
            bubbles.append({"page": 1, "kind": "id", "ref": f"id{column}", "label": "id", "letter": str(digit),
                            "x": ID_X0 + column * ID_STEP, "y": H - (ID_TOP + digit * ID_ROW), "r": ID_R})

    choices = choice_items(test["items"])
    columns, rows = _choice_grid(len(choices))
    column_width = (RIGHT - LEFT) / columns

    # Blok belgilari: har ustundagi har 5 savollik blok burchaklarida qora kvadratcha.
    # Skaner har doirachani o'z blokining 4 belgisi orasida joylashtiradi — bukilgan qog'ozda ham aniq.
    def row_y(row: int) -> float:
        return H - (QUESTIONS_TOP + row * ROW_STEP)

    for column in range(columns):
        column_items = choices[column * rows:(column + 1) * rows]
        if not column_items:
            continue
        x0 = LEFT + column * column_width
        widest = max(item["options"] for item in column_items)
        left_x, right_x = x0 + 32 - BUBBLE_R - 8, x0 + 32 + (widest - 1) * LETTER_STEP + BUBBLE_R + 8
        count = len(column_items)
        boundaries = [row_y(0) + ROW_STEP / 2]
        boundaries += [(row_y(r - 1) + row_y(r)) / 2 for r in range(BLOCK_ROWS, count, BLOCK_ROWS)]
        boundaries.append(row_y(count - 1) - ROW_STEP / 2)
        for k, y in enumerate(boundaries):
            for side, x in (("L", left_x), ("R", right_x)):
                bubbles.append({"page": 1, "kind": "anchor", "ref": "anchor", "letter": f"c{column}b{k}{side}",
                                "x": x, "y": y, "r": ANCHOR_SIZE / 2})
        for row, item in enumerate(column_items):
            block = row // BLOCK_ROWS
            corners = [f"c{column}b{block}L", f"c{column}b{block}R", f"c{column}b{block + 1}R", f"c{column}b{block + 1}L"]
            for option in range(item["options"]):
                bubbles.append({"page": 1, "kind": "single", "ref": item["label"], "label": item["label"],
                                "letter": LETTERS[option], "x": x0 + 32 + option * LETTER_STEP, "y": row_y(row),
                                "r": BUBBLE_R, "block": corners})
    choices_bottom = QUESTIONS_TOP + (rows - 1) * ROW_STEP + 14

    # Yozma javoblar: har javob — bir qator harf kataklari (har katakka bitta bosh harf).
    # Alohida kataklardagi bosma harflarni AI yaxlit qo'lyozmadan ancha aniq o'qiydi
    written = []
    texts = text_items(test["items"])
    written_top = choices_bottom + 34
    if texts:
        text_rows = math.ceil(len(texts) / 2)
        box_h, box_gap = WRITTEN_CELL_H, 5
        half = (RIGHT - LEFT - 12) / 2
        cells = int((half - WRITTEN_LABEL_W) // WRITTEN_CELL_W)
        for index, item in enumerate(texts):
            column, row = divmod(index, text_rows)
            x = LEFT + column * (half + 12)
            top = written_top + row * (box_h + box_gap)
            written.append({"label": item["label"], "x": x, "top": top, "w": half, "h": box_h, "cells": cells})
        bottom = written_top + text_rows * (box_h + box_gap)
        written_area = (LEFT - 4, written_top - 4, RIGHT + 4, bottom + 2)
    else:
        bottom, written_area = choices_bottom, None
    return {"bubbles": bubbles, "written": written, "written_area": written_area, "name_area": NAME_AREA,
            "page": None, "choices_columns": (columns, rows, column_width), "bottom": bottom}


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
        elif bubble["kind"] == "anchor":
            _square(c, bubble["x"], bubble["y"], ANCHOR_SIZE)

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
    if student:
        c.setFont("Sans-Bold", 11)
        c.drawString(410, H - 104, f"ID: {student['number']:0{ID_DIGITS}d}")

    # ID: tepada raqam kataklari, ostida har raqam uchun 0–9 ustuni
    digits = f"{student['number']:0{ID_DIGITS}d}" if student else None
    c.setFillColor(INK)
    c.setFont("Sans-Bold", 9.5)
    c.drawString(LEFT, H - ID_BOX_TOP - ID_BOX / 2 - 3.5, "ID")
    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    for column in range(ID_DIGITS):
        x = ID_X0 + column * ID_STEP
        c.rect(x - ID_BOX / 2, H - ID_BOX_TOP - ID_BOX, ID_BOX, ID_BOX, stroke=1, fill=0)
        if digits:
            c.setFont("Sans-Bold", 11)
            c.drawCentredString(x, H - ID_BOX_TOP - ID_BOX + 4, digits[column])
    c.setFillColor(GRID)
    c.setFont("Sans-Bold", 6.5)
    for digit in range(10):  # chap tomonda raqamlar — qaysi qator qaysi raqam
        c.drawRightString(ID_X0 - ID_R - 4, H - (ID_TOP + digit * ID_ROW) - 2.3, str(digit))
    for bubble in spec["bubbles"]:
        if bubble["kind"] != "id":
            continue
        column = int(bubble["ref"][2:])
        filled = bool(digits) and digits[column] == bubble["letter"]
        _bubble(c, bubble["x"], bubble["y"], bubble["r"], "", filled)
    c.setFillColor(INK)
    help_x = ID_X0 + ID_DIGITS * ID_STEP + 8
    lines = (["ID'ingiz oldindan yozilgan va bo'yalgan —", "bu kataklarga tegmang."] if student else
             ["O'qituvchi bergan ID'ni to'ldiring:",
              "1. Tepadagi 4 ta katakka raqamni yozing.",
              "2. Har ustunda shu raqamning doirachasini bo'yang.",
              "Masalan, ID 0007: birinchi uchta ustunda 0 ni,",
              "to'rtinchi ustunda 7 ni bo'yaysiz."])
    for index, line in enumerate(lines):
        c.setFont("Sans-Bold" if index == 0 else "Sans", 8)
        c.drawString(help_x, H - ID_BOX_TOP - 10 - index * 13, line)

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
    # harflar har ustun tepasida sarlavha qilib yoziladi
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
        c.drawString(LEFT, H - spec["written"][0]["top"] + 18, f"{first.rstrip('ab')}–{last.rstrip('ab')} · yozma javoblar")
        c.setFillColor(INK)
        rule = ("Har katakka bitta BOSH harf: K I T O B. O' va G' — bitta katakka, so'zlar orasida "
                "1 katak bo'sh, tinish belgisi ham alohida katakka.")
        size = min(7.5, 7.5 * (RIGHT - LEFT) / pdfmetrics.stringWidth(rule, "Sans", 7.5))  # varaqdan chiqmasin
        c.setFont("Sans", size)
        c.drawString(LEFT, H - spec["written"][0]["top"] + 7, rule)
        c.setStrokeColor(GRID)
        c.setLineWidth(0.7)
        for box in spec["written"]:
            c.setFillColor(INK)
            c.setFont("Sans-Bold", 9)
            c.drawString(box["x"] + 2, H - box["top"] - box["h"] / 2 - 3, box["label"])
            bottom = H - box["top"] - box["h"]
            for cell in range(box["cells"]):
                c.rect(box["x"] + WRITTEN_LABEL_W + cell * WRITTEN_CELL_W, bottom, WRITTEN_CELL_W, box["h"],
                       stroke=1, fill=0)

    c.setFillColor(INK)
    c.setFont("Sans", 8)
    y = H - spec["bottom"] - 20
    if test["fmt"] == "milliy":
        c.drawString(LEFT, y, f"45 — esse botda yoziladi ({ESSAY_MAX} ball): botga test kodi {test['code']} ni "
                              "kiriting va shu ID bilan yozing.")
        c.drawString(LEFT, y - 12, "Varaqni bukmang, burchakdagi qora kvadratlar ustiga yozmang.")
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
    """
    Har o'quvchiga shaxsiy titul va kerak bo'lsa bo'sh titullar. Esse qog'ozda emas — o'quvchi uni botda
    test kodi va ID'si bilan yozadi (titul skanerlanganda natijaga ID bo'yicha biriktiriladi).
    """
    import pdfs  # noqa: F401 — shriftlar ro'yxatdan o'tgan bo'lsin

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Titullar — {test['title']}")
    pages = [*students, *([None] * blanks)] or [None]
    import sheet_styles
    if sheet_styles.style_of(test) != "standard":
        sheet_styles.draw_sheets(c, test, variant_names, pages)
        c.save()
        return buffer.getvalue()
    for student in pages:
        draw_answer_page(c, test, variant_names, student)
        c.showPage()
    c.save()
    return buffer.getvalue()
