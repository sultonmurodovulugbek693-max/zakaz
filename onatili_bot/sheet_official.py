"""
Milliy sertifikat (Ona tili) oflayn titul — imtihondagi javoblar varaqasi ko'rinishida, 4 varaq:

  1-varaq — «JAVOBLAR VARAQASI [1 - 35]»: ikki ustunda doirachalar (33–35 — A–F), «Yozilmasin» ustuni,
            pastda ism / otasining ismi / familiya bloki
  2-varaq — «JAVOBLAR VARAQASI [36 - 44]»: namuna, har javob — bosh harflar uchun kataklar qatori,
            «DIQQAT! YOZILMASIN» ustuni
  3–4-varaq — ESSE (chiziqli varaq, skanerlanmaydi: esseni o'qituvchi o'qib, 75 ballik shkalada kiritadi)

Skaner uchun 1- va 2-varaqda standart tituldagi belgilar saqlanadi (burchak kvadratlari, chap/o'ng chet
belgilari, pastdagi 12 bitli o'quvchi kodi, ID doirachalari, 5 savollik blok belgilari) — skaner varaqni
xuddi standart tituldek tekislaydi. Qaysi varaq ekanini tepadagi «sahifa belgisi» aytadi: 1-varaqda
chapdagi, 2-varaqda o'ngdagi kvadrat bo'yalgan (layout'da ikkala varaqda ham ikkalasi tekshiriladi).

Koordinatalar punktlarda: doirachalar PDF tizimida (y pastdan), kataklar va hududlar — tepadan.
"""

from reportlab.lib import colors
from reportlab.pdfgen import canvas

from sheet import (ANCHOR_SIZE, BLOCK_ROWS, CODE_BITS, CODE_SIZE, CORNER, CORNER_SIZE, EDGE_MARKS, EDGE_SIZE, H,
                   ID_DIGITS, LEFT, RIGHT, W, _bubble, _square, encode_number)
from structure import LETTERS, choice_items, text_items

PAGES = 2  # skanerlanadigan varaqlar (esse varaqlari skanerlanmaydi)

# Ranglar — namunadagidek; qora-oq printerda ham doirachalar aniq (to'q to'q-sariq ≈ 50% kulrang)
PEACH = colors.HexColor("#fde3d3")      # sarlavha va harf ustunlari foni
RING = colors.HexColor("#d4621c")       # doiracha halqasi va yozma kataklar
CREAM = colors.HexColor("#fff3cd")      # «Yozilmasin» kataklari
BLUE = colors.HexColor("#1f3fbf")
INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#52606d")
RULE = colors.HexColor("#e8a0a0")       # esse varag'i chiziqlari

PAGE_MARK_TOP, PAGE_MARK_SIZE = 50, 10
PAGE_MARK_X = {1: W / 2 - 120, 2: W / 2 + 120}

R = 6.2                 # doiracha radiusi
STEP = 20               # harflar orasi
ROW = 23.5              # savol qatorlari orasi
Q_TOP = 266             # birinchi savol qatori markazi (tepadan)
LEFT_COL = {"label": 80, "x0": 104, "box": (192, 236)}      # 1–18
RIGHT_COL = {"label": 300, "x0": 324, "box": (452, 496)}    # 19–35
NAME_BLOCK = (LEFT, 688, RIGHT, 788)                          # ism bloki (tepadan)

# ID: tepada 4 katak, ostida 0–9 ustunlari (standart titul bilan bir xil o'qiladi)
ID_X0, ID_STEP, ID_R, ID_ROW = 88, 20, 4.7, 11
ID_BOX = 16
ID_TOP = {1: (106, 132), 2: (182, 208)}  # varaq: (kataklar tepasi, 0-qator doirachalari markazi)

# 2-varaq: yozma javoblar
W_LABEL, W_CELL, W_H, W_CELLS = 40, 21.5, 22, 15
W_TOPS = {"36": 324, "37": 356, "38": 388, "39": 420, "40a": 454, "40b": 478, "41a": 510, "41b": 534,
          "42a": 566, "42b": 590, "43a": 622, "43b": 646, "44a": 678, "44b": 702}
W_COLUMN = (440, RIGHT)  # «DIQQAT! YOZILMASIN»


def _y(top: float) -> float:
    return H - top


def _page_frame(page: int) -> list[dict]:
    """Ikkala varaqda bir xil: chet belgilari, pastki kod, sahifa belgilari (ikkalasi ham tekshiriladi)."""
    bubbles = []
    step = (H - 2 * CORNER) / (EDGE_MARKS + 1)
    for index in range(1, EDGE_MARKS + 1):
        for side, x in (("left", CORNER), ("right", W - CORNER)):
            bubbles.append({"page": page, "kind": "mark", "ref": side, "x": x, "y": CORNER + index * step,
                            "r": EDGE_SIZE / 2})
    first_x, last_x = CORNER + 48, W - CORNER - 48
    gap = (last_x - first_x) / (CODE_BITS - 1)
    for bit in range(CODE_BITS):
        bubbles.append({"page": page, "kind": "code", "ref": "code", "letter": str(bit),
                        "x": first_x + bit * gap, "y": CORNER, "r": CODE_SIZE / 2})
    for mark, x in PAGE_MARK_X.items():
        bubbles.append({"page": page, "kind": "pagemark", "ref": "pagemark", "letter": str(mark),
                        "x": x, "y": _y(PAGE_MARK_TOP), "r": PAGE_MARK_SIZE / 2})
    boxes_top, first_row = ID_TOP[page]
    for column in range(ID_DIGITS):
        for digit in range(10):
            bubbles.append({"page": page, "kind": "id", "ref": f"id{column}", "label": "id", "letter": str(digit),
                            "x": ID_X0 + column * ID_STEP, "y": _y(first_row + digit * ID_ROW), "r": ID_R})
    return bubbles


def _question_rows(choices: list[dict]) -> list[tuple[dict, dict, float]]:
    """(savol, ustun, qator markazi tepadan): 1–18 chapda, 19–35 o'ngda; A–F savollari oldidan sarlavha uchun joy."""
    rows = []
    left, right = choices[:18], choices[18:]
    for index, item in enumerate(left):
        rows.append((item, LEFT_COL, Q_TOP + index * ROW))
    top = Q_TOP
    for index, item in enumerate(right):
        if index and item["options"] != right[index - 1]["options"]:
            top += ROW  # «A B C D E F» sarlavhasi
        rows.append((item, RIGHT_COL, top))
        top += ROW
    return rows


def layout(test: dict) -> dict:
    bubbles = _page_frame(1) + _page_frame(2)
    rows = _question_rows(choice_items(test["items"]))
    for column in (LEFT_COL, RIGHT_COL):
        column_rows = [(item, top) for item, col, top in rows if col is column]
        if not column_rows:
            continue
        widest = max(item["options"] for item, _ in column_rows)
        left_x = column["x0"] - R - 8
        right_x = column["x0"] + (widest - 1) * STEP + R + 8
        tops = [top for _, top in column_rows]
        # Blok belgilari: har 5 savol oralig'ida (standart tituldagidek) — bukilgan qog'ozda ham aniq
        boundaries = [tops[0] - ROW / 2]
        boundaries += [(tops[k - 1] + tops[k]) / 2 for k in range(BLOCK_ROWS, len(tops), BLOCK_ROWS)]
        boundaries.append(tops[-1] + ROW / 2)
        key = "L" if column is LEFT_COL else "R"
        for k, top in enumerate(boundaries):
            for side, x in (("L", left_x), ("R", right_x)):
                bubbles.append({"page": 1, "kind": "anchor", "ref": "anchor", "letter": f"{key}b{k}{side}",
                                "x": x, "y": _y(top), "r": ANCHOR_SIZE / 2})
        for index, (item, top) in enumerate(column_rows):
            block = index // BLOCK_ROWS
            corners = [f"{key}b{block}L", f"{key}b{block}R", f"{key}b{block + 1}R", f"{key}b{block + 1}L"]
            for option in range(item["options"]):
                bubbles.append({"page": 1, "kind": "single", "ref": item["label"], "label": item["label"],
                                "letter": LETTERS[option], "x": column["x0"] + option * STEP, "y": _y(top),
                                "r": R, "block": corners})
    written = []
    for item in text_items(test["items"]):
        top = W_TOPS.get(item["label"])
        if top is not None:
            written.append({"label": item["label"], "x": LEFT, "top": top, "w": W_LABEL + W_CELLS * W_CELL,
                            "h": W_H, "cells": W_CELLS, "label_w": W_LABEL, "cell_w": W_CELL, "page": 2})
    bottom = max((box["top"] + box["h"] for box in written), default=0)
    written_area = (LEFT - 4, min(W_TOPS.values()) - 4, LEFT + W_LABEL + W_CELLS * W_CELL + 4, bottom + 4) if written else None
    x1, y1, x2, y2 = NAME_BLOCK
    return {"bubbles": bubbles, "written": written, "written_area": written_area, "name_area": (x1, y1, x2, y2),
            "page": None, "pages": PAGES, "bottom": y2,
            "rows": [(item["label"], col is LEFT_COL, top) for item, col, top in rows]}


# ---------- Chizish ----------

def _frame(c: canvas.Canvas, spec: dict, page: int, student: dict | None) -> None:
    c.setFillColor(colors.black)
    for x, y in [(CORNER, H - CORNER), (W - CORNER, H - CORNER), (CORNER, CORNER), (W - CORNER, CORNER)]:
        _square(c, x, y, CORNER_SIZE)
    code = encode_number(student["number"]) if student and student["number"] <= 0x7FF else 0
    for bubble in spec["bubbles"]:
        if bubble["page"] != page:
            continue
        if bubble["kind"] == "mark":
            _square(c, bubble["x"], bubble["y"], EDGE_SIZE)
        elif bubble["kind"] == "code" and code >> int(bubble["letter"]) & 1:
            _square(c, bubble["x"], bubble["y"], CODE_SIZE)
        elif bubble["kind"] == "anchor":
            _square(c, bubble["x"], bubble["y"], ANCHOR_SIZE)
        elif bubble["kind"] == "pagemark" and bubble["letter"] == str(page):
            _square(c, bubble["x"], bubble["y"], PAGE_MARK_SIZE)


def _header(c: canvas.Canvas, text: str, top: float = 62) -> None:
    c.setFillColor(PEACH)
    c.rect(LEFT, _y(top + 18), RIGHT - LEFT, 18, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Sans-Bold", 11)
    c.drawCentredString(W / 2, _y(top + 13), text)


def _subtitle(c: canvas.Canvas, test: dict, top: float = 94) -> None:
    c.setFont("Sans", 8)
    c.setFillColor(MUTED)
    c.drawString(LEFT, _y(top), f"{test['title'][:60]} · Ona tili va adabiyot · Milliy sertifikat")
    c.setFont("Sans-Bold", 9)
    c.setFillColor(INK)
    c.drawRightString(RIGHT, _y(top), f"Kod {test['code']}")


def _id_block(c: canvas.Canvas, spec: dict, page: int, student: dict | None, lines: list[str]) -> None:
    boxes_top, first_row = ID_TOP[page]
    digits = f"{student['number']:0{ID_DIGITS}d}" if student else None
    c.setFillColor(INK)
    c.setFont("Sans-Bold", 9.5)
    c.drawString(LEFT, _y(boxes_top + ID_BOX / 2 + 3.5), "ID")
    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    for column in range(ID_DIGITS):
        x = ID_X0 + column * ID_STEP
        c.rect(x - ID_BOX / 2, _y(boxes_top + ID_BOX), ID_BOX, ID_BOX, stroke=1, fill=0)
        if digits:
            c.setFont("Sans-Bold", 11)
            c.drawCentredString(x, _y(boxes_top + ID_BOX - 4), digits[column])
    c.setFillColor(MUTED)
    c.setFont("Sans-Bold", 6.5)
    for digit in range(10):
        c.drawRightString(ID_X0 - ID_R - 4, _y(first_row + digit * ID_ROW) - 2.3, str(digit))
    for bubble in spec["bubbles"]:
        if bubble["page"] == page and bubble["kind"] == "id":
            filled = bool(digits) and digits[int(bubble["ref"][2:])] == bubble["letter"]
            _bubble(c, bubble["x"], bubble["y"], bubble["r"], "", filled)
    c.setFillColor(INK)
    x = ID_X0 + ID_DIGITS * ID_STEP + 10
    for index, line in enumerate(lines):
        c.setFont("Sans-Bold" if index == 0 else "Sans", 8.5 if index == 0 else 8)
        c.drawString(x, _y(boxes_top + 10 + index * 13), line)


def _ring(c: canvas.Canvas, x: float, y: float, filled: bool = False) -> None:
    """Imtihon varaqasidagidek to'q-sariq halqa (ichi oq — skaner ichini o'lchaydi)."""
    c.setStrokeColor(RING)
    c.setLineWidth(1.3)
    c.setFillColor(colors.black if filled else colors.white)
    c.circle(x, y, R, stroke=1, fill=1)


def draw_page1(c: canvas.Canvas, test: dict, spec: dict, student: dict | None) -> None:
    _frame(c, spec, 1, student)
    _header(c, "JAVOBLAR VARAQASI [1 - 35]")
    _subtitle(c, test)
    personal = [f"ID: {student['number']:0{ID_DIGITS}d} — oldindan yozilgan va bo'yalgan,", "bu kataklarga tegmang."] \
        if student else ["O'qituvchi bergan ID'ni to'ldiring:", "1. Tepadagi 4 ta katakka raqamni yozing.",
                         "2. Har ustunda shu raqamning doirachasini bo'yang.", "Masalan, ID 0007: uchta ustunda 0, to'rtinchisida 7.",
                         "ID'ni 2-varaqqa ham xuddi shunday yozing."]
    _id_block(c, spec, 1, student, personal)
    # Bo'yash namunasi
    c.setFont("Sans", 8)
    c.setFillColor(INK)
    text = "Javobni doirachani to'liq bo'yab belgilang:"
    c.drawRightString(RIGHT - 22, _y(222), text)
    _ring(c, RIGHT - 8, _y(219), filled=True)

    rows = _question_rows(choice_items(test["items"]))
    for column in (LEFT_COL, RIGHT_COL):
        column_rows = [(item, top) for item, col, top in rows if col is column]
        if not column_rows:
            continue
        widest = max(item["options"] for item, _ in column_rows)
        first, last = column_rows[0][1], column_rows[-1][1]
        # Harf ustunlari foni — faqat shu harf bor savollar oralig'ida (E, F — faqat 33–35 da)
        c.setFillColor(PEACH)
        for option in range(widest):
            tops = [top for item, top in column_rows if item["options"] > option]
            x = column["x0"] + option * STEP
            c.rect(x - 8.5, _y(tops[-1] + ROW / 2), 17, tops[-1] - tops[0] + ROW + 12, stroke=0, fill=1)
        # Harflar sarlavhasi (A–F savollari oldidan alohida sarlavha)
        c.setFillColor(INK)
        c.setFont("Sans-Bold", 9)
        previous = None
        for item, top in column_rows:
            if item["options"] != previous:
                for option in range(item["options"]):
                    c.drawCentredString(column["x0"] + option * STEP, _y(top - ROW / 2 - 3), LETTERS[option])
                previous = item["options"]
        # «Yozilmasin» ustuni
        bx1, bx2 = column["box"]
        head_top = first - ROW / 2 - 13
        c.setFillColor(BLUE)
        c.rect(bx1, _y(head_top + 12), bx2 - bx1, 12, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Sans", 6.5)
        c.drawCentredString((bx1 + bx2) / 2, _y(head_top + 8.5), "Yozilmasin")
        c.setStrokeColor(INK)
        c.setLineWidth(0.6)
        for item, top in column_rows:
            c.setFillColor(CREAM)
            c.rect(bx1, _y(top + ROW / 2 - 1), bx2 - bx1, ROW - 2, stroke=1, fill=1)
        # Savollar
        for item, top in column_rows:
            c.setFillColor(INK)
            c.setFont("Sans-Bold", 9.5)
            c.drawRightString(column["label"], _y(top) - 3.3, f"{item['label']}.")
            for option in range(item["options"]):
                _ring(c, column["x0"] + option * STEP, _y(top))

    # Ism bloki
    x1, y1, x2, y2 = NAME_BLOCK
    c.setStrokeColor(INK)
    c.setLineWidth(1)
    c.rect(x1, _y(y2), x2 - x1, y2 - y1, stroke=1, fill=0)
    c.setDash(4, 2)
    c.setLineWidth(0.6)
    for index, caption in enumerate(("ismi", "otasining ismi", "familiyasi")):
        line_top = y1 + 26 + index * 30
        c.line(x1 + 12, _y(line_top), x2 - 12, _y(line_top))
        c.setFont("Sans", 7)
        c.setFillColor(BLUE)
        c.drawCentredString((x1 + x2) / 2, _y(line_top + 9), f"({caption})")
    c.setDash()
    if student and student.get("name"):
        c.setFillColor(INK)
        c.setFont("Sans-Bold", 12)
        c.drawString(x1 + 16, _y(y1 + 22.5), student["name"][:48])
    c.setFillColor(MUTED)
    c.setFont("Sans", 7)
    c.drawCentredString(W / 2, CORNER + 9, "Varaqni bukmang, qora kvadratlar ustiga yozmang · 1-varaq")


def draw_page2(c: canvas.Canvas, test: dict, spec: dict, student: dict | None) -> None:
    _frame(c, spec, 2, student)
    _header(c, "JAVOBLAR VARAQASI [36 - 44]")
    _subtitle(c, test)
    c.setFillColor(INK)
    c.setFont("Sans", 8.5)
    c.drawString(LEFT, _y(112), "36–44-savollar javobini quyidagi namuna asosida bosh harflar bilan yozing. Sh, Ch, Ng")
    c.drawString(LEFT, _y(124), "tovushlaridagi har bir belgi alohida katakchada, O', G' tovushlari esa bitta katakchada yoziladi.")
    # Namuna kataklari
    samples = [["A", "B", "", "C", "h", "A", "N", "g", "", "E", "'", "L", "O", "N"],
               ["–", "-", "«", "»", ":", ";", "?", ".", ",", "", "S", "h", "O'", "G'"]]
    c.setStrokeColor(RING)
    c.setLineWidth(1)
    for row, letters in enumerate(samples):
        top = 134 + row * 20
        for index, letter in enumerate(letters):
            x = LEFT + 20 + index * 19
            c.setFillColor(colors.white)
            c.rect(x, _y(top + 17), 17, 17, stroke=1, fill=1)
            if letter:
                c.setFillColor(INK)
                c.setFont("Sans", 8.5)
                c.drawCentredString(x + 8.5, _y(top + 12), letter)
    c.setFillColor(BLUE)
    c.rect(W_COLUMN[0], _y(170), W_COLUMN[1] - W_COLUMN[0], 34, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Sans-Bold", 7.5)
    c.drawCentredString((W_COLUMN[0] + W_COLUMN[1]) / 2, _y(150), "DIQQAT!")
    c.drawCentredString((W_COLUMN[0] + W_COLUMN[1]) / 2, _y(161), "YOZILMASIN")

    lines = ([f"ID: {student['number']:0{ID_DIGITS}d} · {student.get('name', '')[:30]}", "oldindan yozilgan — tegmang."] if student
             else ["1-varaqdagi ID'ingizni shu yerga ham", "yozing va bo'yang — ikki varaq", "bitta o'quvchiga qo'shiladi."])
    _id_block(c, spec, 2, student, lines)

    # Yozma javoblar
    c.setStrokeColor(RING)
    for box in spec["written"]:
        top, label = box["top"], box["label"]
        number, part = (label[:-1], label[-1]) if label[-1] in "ab" else (label, "")
        c.setFillColor(INK)
        c.setFont("Sans-Bold", 10)
        if part in ("", "a"):
            c.drawString(box["x"], _y(top + W_H / 2 + 3.5), f"{number}." if not part else f"{number}.")
        if part:
            c.setFont("Sans-Bold", 9)
            c.drawRightString(box["x"] + W_LABEL - 4, _y(top + W_H / 2 + 3.2), f"{part})")
        c.setLineWidth(1.1)
        for cell in range(box["cells"]):
            c.setFillColor(colors.white)
            c.rect(box["x"] + W_LABEL + cell * W_CELL + 1, _y(top + W_H), W_CELL - 2, W_H, stroke=1, fill=1)
    # «DIQQAT! YOZILMASIN» ustuni
    if spec["written"]:
        first = spec["written"][0]["top"] - 6
        last = spec["written"][-1]["top"] + W_H + 6
        c.setFillColor(CREAM)
        c.setStrokeColor(INK)
        c.setLineWidth(1)
        c.rect(W_COLUMN[0] + 6, _y(last), W_COLUMN[1] - W_COLUMN[0] - 6, last - first, stroke=1, fill=1)
        c.setDash(3, 2)
        c.setStrokeColor(MUTED)
        c.setLineWidth(0.5)
        for box in spec["written"]:
            y = _y(box["top"] + W_H - 3)
            c.line(W_COLUMN[0] + 40, y, W_COLUMN[1] - 8, y)
        c.setDash()
    c.setFillColor(MUTED)
    c.setFont("Sans", 7)
    c.drawCentredString(W / 2, CORNER + 9, "Varaqni bukmang, qora kvadratlar ustiga yozmang · 2-varaq")


def _essay_frame(c: canvas.Canvas) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = 40, 40, W - 40, H - 40
    c.setStrokeColor(RING)
    c.setLineWidth(1)
    c.rect(x1, y1, x2 - x1, y2 - y1, stroke=1, fill=0)
    return x1, y1, x2, y2


def _rules(c: canvas.Canvas, x1: float, x2: float, top: float, bottom: float) -> None:
    c.setStrokeColor(RULE)
    c.setLineWidth(0.7)
    y = top
    while y > bottom:
        c.line(x1 + 12, y, x2 - 12, y)
        y -= 21


def draw_essay_pages(c: canvas.Canvas, test: dict, student: dict | None) -> None:
    """3–4-varaq: esse (qo'lda yoziladi, skanerlanmaydi — o'qituvchi 75 ballik shkalada baholaydi)."""
    x1, y1, x2, y2 = _essay_frame(c)
    c.setFillColor(PEACH)
    c.rect(x1 + 8, y2 - 26, x2 - x1 - 16, 16, stroke=0, fill=1)
    c.setFillColor(INK)
    c.setFont("Sans-Bold", 10.5)
    c.drawCentredString(W / 2, y2 - 21.5, "ESSE")
    c.setFont("Sans", 8.5)
    who = f"F.I.Sh.: {student['name'][:40]}   ·   ID: {student['number']:0{ID_DIGITS}d}" if student else \
        "F.I.Sh.: ________________________________   ·   ID: ________"
    c.drawString(x1 + 14, y2 - 42, who)
    c.drawRightString(x2 - 14, y2 - 42, f"Kod {test['code']}")
    top = y2 - 56
    topic = (test.get("essay_topic") or "").strip()
    if topic:
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Paragraph
        from pdfs import _esc
        para = Paragraph("<b>Vaziyat:</b> " + _esc(topic).replace("\n", "<br/>"),
                         ParagraphStyle("t", fontName="Sans", fontSize=9, leading=12, textColor=INK))
        _, height = para.wrap(x2 - x1 - 36, 200)
        c.setFillColor(colors.HexColor("#fdf3ec"))
        c.rect(x1 + 12, top - height - 8, x2 - x1 - 24, height + 10, stroke=0, fill=1)
        para.drawOn(c, x1 + 18, top - height - 3)
        top -= height + 22
    _rules(c, x1, x2, top, y1 + 14)
    c.showPage()
    x1, y1, x2, y2 = _essay_frame(c)
    c.setFillColor(colors.HexColor("#3b63c4"))
    path = c.beginPath()
    path.moveTo(x1 + 14, y2 - 12)
    path.lineTo(x1 + 26, y2 - 18)
    path.lineTo(x1 + 14, y2 - 24)
    path.close()
    c.drawPath(path, stroke=0, fill=1)
    if student:
        c.setFillColor(MUTED)
        c.setFont("Sans", 7.5)
        c.drawRightString(x2 - 14, y2 - 20, f"{student['name'][:40]} · ID {student['number']:0{ID_DIGITS}d} · esse davomi")
    _rules(c, x1, x2, y2 - 32, y1 + 14)


def draw_sheets(c: canvas.Canvas, test: dict, pages: list[dict | None]) -> None:
    spec = layout(test)
    for student in pages:
        draw_page1(c, test, spec, student)
        c.showPage()
        draw_page2(c, test, spec, student)
        c.showPage()
        draw_essay_pages(c, test, student)
        c.showPage()
