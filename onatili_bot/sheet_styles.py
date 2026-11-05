"""
Titulning qo'shimcha ko'rinishlari — Chittak (matemttest) titullaridan olingan:

  compact — Ixcham (6-versiya): faqat doirachalar (ID, variant, savollar). Savol soniga qarab A4 ning
            1/3, 1/2 qismi yoki to'liq A4 — bitta varaqqa 2–3 ta titul, orasida qirqish chizig'i.
  evalbee — EvalBee uslubi (9–10-versiya): bitta varaqda bitta titul, tepada "Ism-familiya / Imtihon /
            Sana" qutisi, savollar 5 tadan bloklarda, blok burchaklarida qora kvadratlar. O'lchami
            moslashuvchan: savol kam bo'lsa doirachalar kattaroq.
  dtm     — DTM ko'rinishi: EvalBee to'ri, har ustun tepasida fan va savollar oralig'i sarlavhasi.

Yozma javoblar (Milliy sertifikatda 36–44) bu titullarda yo'q — skanerdan keyin qo'lda kiritiladi.

Har bir ko'rinish bitta chizish funksiyasi bilan ham PDF'ni chizadi, ham skaner uchun doirachalar
ro'yxatini yig'adi (layout'da "bo'sh" kanvas bilan) — shuning uchun PDF va skaner hech qachon farq qilmaydi.
Koordinatalar punktlarda, titulning chap-past burchagidan (PDF tizimi).
"""

import math

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics

from structure import LETTERS, choice_items

STYLES = {"standard": "Standart", "compact": "Ixcham", "evalbee": "Yirik doirachalar", "dtm": "Fan ustunli"}

CORNER, CORNER_SIZE = 32, 14
EDGE_SIZE = 8
CODE_BITS, CODE_SIZE = 12, 9
ID_DIGITS = 3
MUTED = colors.HexColor("#6b7280")


def style_of(test: dict) -> str:
    style = test.get("sheet_style") or "standard"
    return style if style in STYLES else "standard"


def _fonts() -> None:
    import pdfs  # noqa: F401 — "Sans" shriftlari ro'yxatdan o'tadi


class _NullCanvas:
    """Hech narsa chizmaydigan kanvas — faqat doirachalar joyini yig'ish uchun (stringWidth haqiqiy)."""

    def stringWidth(self, text, font, size):  # noqa: N802 — reportlab nomi
        return pdfmetrics.stringWidth(text, font, size)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def encode_number(number: int) -> int:
    from sheet import encode_number as encode
    return encode(number)


def _marks(c, width: float, height: float, edge_marks: int, code: int, note) -> None:
    """Burchak kvadratlari, chap/o'ng chet belgilari va pastki 12 bitli kod (o'quvchi raqami)."""
    half = CORNER_SIZE / 2
    for x, y in [(CORNER, height - CORNER), (width - CORNER, height - CORNER), (CORNER, CORNER), (width - CORNER, CORNER)]:
        c.rect(x - half, y - half, CORNER_SIZE, CORNER_SIZE, stroke=0, fill=1)
    step = (height - 2 * CORNER) / (edge_marks + 1)
    for index in range(1, edge_marks + 1):
        y = CORNER + index * step
        for side, x in (("left", CORNER), ("right", width - CORNER)):
            c.rect(x - EDGE_SIZE / 2, y - EDGE_SIZE / 2, EDGE_SIZE, EDGE_SIZE, stroke=0, fill=1)
            note({"kind": "mark", "ref": side, "x": x, "y": y, "r": EDGE_SIZE / 2})
    first_x, last_x = CORNER + 48, width - CORNER - 48
    gap = (last_x - first_x) / (CODE_BITS - 1)
    for bit in range(CODE_BITS):
        x = first_x + bit * gap
        if code >> bit & 1:
            c.rect(x - CODE_SIZE / 2, CORNER - CODE_SIZE / 2, CODE_SIZE, CODE_SIZE, stroke=0, fill=1)
        note({"kind": "code", "ref": "code", "letter": str(bit), "x": x, "y": CORNER, "r": CODE_SIZE / 2})


def _fit(c, text: str, font: str, size: float, room: float, smallest: float) -> tuple[str, float]:
    """Matn joyiga sig'sin: avval shrift kichrayadi, keyin "…" bilan qisqartiriladi."""
    while size > smallest and c.stringWidth(text, font, size) > room:
        size -= 0.25
    while len(text) > 4 and c.stringWidth(text, font, size) > room:
        text = text[:-2].rstrip() + "…"
    return text, size


def _student_digits(student: dict | None) -> str | None:
    return f"{student['number']:03d}" if student else None


# ---------- Ixcham titul ----------

COMPACT_HEIGHTS = (A4[1] / 3, A4[1] / 2, A4[1])
COMPACT_LEFT = 48
COMPACT_TOP = 58
COMPACT_BOTTOM = 46
COMPACT_ROW = 15
COMPACT_RADIUS = 5.0
COMPACT_GAP = 15
COMPACT_DIGIT_STEP = 12.5
COMPACT_ID_GAP = 16
COMPACT_EDGE_STEP = (A4[1] - 2 * CORNER) / 11  # A4 dagi standart tituldagidek oraliq


def compact_plan(test: dict, variants: list[str]) -> dict:
    items = choice_items(test["items"])
    width = A4[0]
    options = max((item["options"] for item in items), default=4)
    column_width = 26 + options * COMPACT_GAP + 6
    id_right = COMPACT_LEFT + 18 + (ID_DIGITS - 1) * COMPACT_ID_GAP + COMPACT_RADIUS
    questions_x = id_right + (30 if len(variants) > 1 else 0) + 14
    right = width - COMPACT_LEFT

    def capacity(height: float) -> tuple[int, int]:
        rows = int((height - COMPACT_TOP - COMPACT_BOTTOM - 12) // COMPACT_ROW)
        return rows, int((right - questions_x) // column_width)

    for height in COMPACT_HEIGHTS:
        rows, columns = capacity(height)
        if rows * columns >= len(items):
            break
    if rows * columns < len(items):
        raise ValueError("Savollar ixcham titulga sig'maydi — «Yirik doirachalar» yoki standart titulni tanlang")
    per_column = max(1, math.ceil(len(items) / columns))
    return {"width": width, "height": height, "column_width": column_width, "questions_x": questions_x,
            "id_right": id_right, "edge_marks": max(2, round((height - 2 * CORNER) / COMPACT_EDGE_STEP) - 1),
            "columns": [items[i: i + per_column] for i in range(0, len(items), per_column)]}


def draw_compact(c, test: dict, plan: dict, variants: list[str], student: dict | None, note) -> None:
    width, height = plan["width"], plan["height"]

    def top(distance: float) -> float:
        return height - distance

    c.setFillColor(colors.black)
    _marks(c, width, height, plan["edge_marks"], encode_number(student["number"]) if student else 0, note)
    c.setFont("Sans-Bold", 9)
    c.drawString(COMPACT_LEFT, top(47), test["title"][:44])
    c.setFont("Sans", 8)
    info = f"{student['name'][:34]} · ID {student['number']:03d} · Kod {test['code']}" if student \
        else f"Kod {test['code']} · Ona tili · doirachani to'liq bo'yang"
    c.drawRightString(width - COMPACT_LEFT, top(47), info)

    rows_top = COMPACT_TOP + 12
    digits = _student_digits(student)
    first_x = COMPACT_LEFT + 18
    c.setFont("Sans-Bold", 8)
    c.drawString(COMPACT_LEFT, top(COMPACT_TOP + 7), "ID")
    c.setLineWidth(0.8)
    for column in range(ID_DIGITS):
        x = first_x + column * COMPACT_ID_GAP
        c.rect(x - 6, top(COMPACT_TOP + 10), 12, 12, stroke=1, fill=0)
        if digits:
            c.setFont("Sans-Bold", 9)
            c.drawCentredString(x, top(COMPACT_TOP + 7), digits[column])
    for digit in range(10):
        center = top(rows_top + 6.5 + digit * COMPACT_DIGIT_STEP)
        c.setFont("Sans-Bold", 7)
        c.drawCentredString(first_x - 13, center - 2.5, str(digit))
        for column in range(ID_DIGITS):
            x = first_x + column * COMPACT_ID_GAP
            c.circle(x, center, COMPACT_RADIUS, stroke=1, fill=1 if digits and int(digits[column]) == digit else 0)
            note({"kind": "id", "ref": f"id{column}", "label": "id", "letter": str(digit),
                  "x": x, "y": center, "r": COMPACT_RADIUS})
    if len(variants) > 1:
        x = plan["id_right"] + 14
        c.setFont("Sans-Bold", 7)
        c.drawCentredString(x + 3, top(COMPACT_TOP + 7), "Var.")
        for index, name in enumerate(variants):
            center = top(rows_top + 6.5 + index * COMPACT_DIGIT_STEP)
            c.circle(x, center, COMPACT_RADIUS, stroke=1, fill=1 if student and student.get("variant") == name else 0)
            c.drawString(x + COMPACT_RADIUS + 2, center - 2.5, name)
            note({"kind": "variant", "ref": "variant", "label": "variant", "letter": name,
                  "x": x, "y": center, "r": COMPACT_RADIUS})

    for index, column in enumerate(plan["columns"]):
        column_x = plan["questions_x"] + index * plan["column_width"]
        bubble_x = column_x + 26 + COMPACT_RADIUS
        c.setFont("Sans-Bold", 7)
        for option in range(max(item["options"] for item in column)):
            c.drawCentredString(bubble_x + option * COMPACT_GAP, top(COMPACT_TOP + 7), LETTERS[option])
        for row, item in enumerate(column):
            center = top(rows_top + (row + 0.5) * COMPACT_ROW)
            c.setFont("Sans", 8)
            c.drawRightString(column_x + 20, center - 3, f"{item['label']}.")
            for option in range(item["options"]):
                x = bubble_x + option * COMPACT_GAP
                c.circle(x, center, COMPACT_RADIUS, stroke=1, fill=0)
                note({"kind": "single", "ref": item["label"], "label": item["label"], "letter": LETTERS[option],
                      "x": x, "y": center, "r": COMPACT_RADIUS})


# ---------- EvalBee va DTM titullari (bloklarga bo'lingan) ----------

GRID = {"step": 78, "row": 13, "header": 9, "radius": 5.5, "gap": 19, "label": 24}
DTM_TITLE_BAND = 20      # DTM: blok sarlavhasi shuncha balandroq (fan nomi va oraliq sig'adi)
MAX_SCALE, FILL_SCALE, MIN_SCALE = 1.3, 2.0, 0.6
BRAND_SPACE = 58
MAX_RADIUS = 6.0


def _blocks(items: list[dict]) -> list[list[dict]]:
    """5 tadan bloklar; variantlar soni o'zgarsa (A–D → A–F) yangi blok boshlanadi."""
    blocks: list[list[dict]] = []
    for item in items:
        if not blocks or len(blocks[-1]) == 5 or blocks[-1][0]["options"] != item["options"]:
            blocks.append([])
        blocks[-1].append(item)
    return blocks


def _grid_style(cells: int, reserved: int, column_base: float, title_band: float) -> dict:
    """cells ta blok sig'adigan eng katta masshtab (Chittak 9-versiya _adaptive_style)."""
    base = {**GRID, "column": column_base, "header": GRID["header"] + title_band, "step": GRID["step"] + title_band}
    best = None
    for columns in range(2, 6):
        for rows in range(max(2, reserved), 16):
            if columns * rows < cells:
                continue
            by_width = (A4[0] - 2 * CORNER) / (columns * base["column"])
            by_height = (A4[1] - 2 * CORNER) / ((rows + 2) * base["step"])
            scale = min(by_width, by_height, MAX_SCALE)
            if scale >= MAX_SCALE:
                by_brand = (A4[1] - BRAND_SPACE - 2 * CORNER) / ((rows + 2) * base["step"])
                scale = max(MAX_SCALE, min(by_width, by_brand, FILL_SCALE))
            width = 2 * CORNER + columns * base["column"] * scale
            height = 2 * CORNER + (rows + 2) * base["step"] * scale
            key = (round(scale, 2), -columns * rows, -abs(height / width - 1.4))
            if best is None or key > best[0]:
                best = (key, columns, rows, scale)
    _, columns, rows, scale = best
    if scale < MIN_SCALE:
        raise ValueError("Savollar bitta titulga sig'maydi — standart titulni tanlang")
    step = base["step"] * scale
    column_width = (A4[0] - 2 * CORNER) / columns  # tor titul varaq kengligigacha cho'ziladi
    height = 2 * CORNER + (rows + 2) * step
    font = min(scale, 1.2)
    box_top, box_bottom = CORNER + 12, CORNER + step - max(8.0, 0.12 * step)
    box_middle = (box_top + box_bottom) / 2
    box_font = 10 * font
    name_left, name_right = CORNER + 16 + 7.2 * box_font, A4[0] - CORNER - 16
    return {
        "columns": columns, "rows": rows, "edge_marks": rows + 1, "step": step, "scale": scale,
        # Doiracha burchak kvadratidan (14 pt) kichik qolsin: aks holda bo'yalgan doirachalar telefondagi
        # skanerga burchakka o'xshab ko'rinadi va titul topilmaydi (titul kattalashsa ham radius ≤ 6 pt)
        "row": base["row"] * scale, "header": base["header"] * scale, "radius": min(base["radius"] * scale, MAX_RADIUS),
        "gap": base["gap"] * scale, "label": base["label"] * scale, "title_band": title_band * scale,
        "number_font": 8.5 * scale, "letter_font": 7 * scale, "digit_font": 8 * scale,
        "box": (box_top, box_middle, box_bottom), "box_font": box_font,
        "sana_width": min(120 * font, (A4[0] - 2 * CORNER - 32) * 0.3),
        "name_area": (name_left, box_top + 2, name_right, box_middle),
        "column_width": column_width, "width": A4[0], "height": height,
    }


def grid_plan(test: dict, variants: list[str], dtm: bool) -> dict:
    items = choice_items(test["items"])
    blocks = _blocks(items)
    show_variants = len(variants) > 1
    reserved = 2 + (1 if show_variants else 0)  # 1-ustunda ID (0–9, ikki blok) va variant
    options = max((item["options"] for item in items), default=4)
    column_base = max(120.0, GRID["label"] + (options - 1) * GRID["gap"] + 2 * GRID["radius"] + 28)
    style = _grid_style(reserved + len(blocks), reserved, column_base, DTM_TITLE_BAND if dtm else 0.0)
    slots = [(column, row) for column in range(style["columns"]) for row in range(style["rows"])
             if column or row >= reserved]
    placed = [(block, *slots[index]) for index, block in enumerate(blocks)]
    titles = []
    if dtm:
        # Har ustunning birinchi bloki ustida: fan (yoki savol turi) va savollar oralig'i
        for column in range(style["columns"]):
            mine = [(block, row) for block, col, row in placed if col == column]
            if not mine:
                continue
            first, last = mine[0][0][0]["label"], mine[-1][0][-1]["label"]
            kind = "Moslashtirish" if mine[0][0][0]["options"] > 4 else "Ona tili va adabiyot"
            titles.append((column, mine[0][1], kind, f"{first}–{last}" if first != last else first))
    return {"style": style, "blocks": placed, "variants": show_variants, "reserved": reserved, "titles": titles,
            "width": style["width"], "height": style["height"], "edge_marks": style["edge_marks"]}


def _fill_examples(c, top, width: float, y_line: float, style: dict) -> None:
    """Titul pastida: qanday bo'yash kerak va qanday bo'yamaslik kerak."""
    radius, band = style["radius"], style["step"]
    font = min(style["scale"], 1.2)
    left, right = CORNER + 16, width - CORNER - 16
    first, second = top(y_line + 0.3 * band), top(y_line + 0.62 * band)
    c.setFont("Sans-Bold", 9.5 * font)
    c.drawString(left, first - 3.3 * font, "TO'G'RI:")
    c.drawString(left, second - 3.3 * font, "NOTO'G'RI:")
    start = left + c.stringWidth("NOTO'G'RI:", "Sans-Bold", 9.5 * font) + 14 + radius
    c.setLineWidth(1)
    c.circle(start, first, radius, stroke=1, fill=1)
    c.setFont("Sans", 8.5 * font)
    hint = "doirachani to'liq, qora ruchka yoki qalam bilan bo'yang. Har savolga — bitta javob."
    for shorter in ("to'liq bo'yang, har savolga bitta javob", "to'liq bo'yang"):
        if c.stringWidth(hint, "Sans", 8.5 * font) > right - start - radius - 8:
            hint = shorter
    c.drawString(start + radius + 8, first - 3 * font, hint)
    examples = ["belgi", "X", "yarim", "nuqta", "ikkita"]
    spacing = min(62.0, (right - start - 2 * radius) / (len(examples) - 0.4))
    c.setFont("Sans", 7.5 * font)
    for index, caption in enumerate(examples):
        x = start + index * spacing
        if caption == "ikkita":
            for dx in (-radius * 1.3, radius * 1.3):
                c.circle(x + dx, second, radius, stroke=1, fill=1)
        else:
            c.circle(x, second, radius, stroke=1, fill=0)
        if caption == "belgi":
            path = c.beginPath()
            if path is not None:
                path.moveTo(x - radius * 0.55, second)
                path.lineTo(x - radius * 0.1, second - radius * 0.5)
                path.lineTo(x + radius * 0.65, second + radius * 0.6)
                c.drawPath(path, stroke=1, fill=0)
        elif caption == "X":
            for sign in (1, -1):
                c.line(x - radius * 0.55, second - sign * radius * 0.55, x + radius * 0.55, second + sign * radius * 0.55)
        elif caption == "yarim":
            c.wedge(x - radius, second - radius, x + radius, second + radius, 90, 180, stroke=0, fill=1)
        elif caption == "nuqta":
            c.circle(x, second, radius * 0.28, stroke=0, fill=1)
        c.drawCentredString(x, second - radius - 8 * font, caption)


def draw_grid(c, test: dict, plan: dict, variants: list[str], student: dict | None, note) -> None:
    style = plan["style"]
    width, height = plan["width"], plan["height"]
    column_width = style["column_width"]
    radius, gap, label_width, header = style["radius"], style["gap"], style["label"], style["header"]
    widest = max((item["options"] for block, _, _ in plan["blocks"] for item in block), default=4)
    content = label_width + (widest - 1) * gap + 2 * radius
    pad = (column_width - content) / 2
    label_right = pad + label_width - 6 * style["scale"]

    def top(distance: float) -> float:
        return height - distance

    def line_y(block_row: int) -> float:
        return top(CORNER + (block_row + 1) * style["step"])

    def line_x(column: int) -> float:
        return CORNER + column * column_width

    def row_center(block_row: int, line: int) -> float:
        return line_y(block_row) - header - (line + 0.5) * style["row"]

    def option_x(column: int, option: int) -> float:
        return line_x(column) + pad + label_width + radius + option * gap

    used = {(column, row) for _, column, row in plan["blocks"]} | {(0, row) for row in range(plan["reserved"])}
    corners_used = {(column + dx, row + dy) for column, row in used for dx in (0, 1) for dy in (0, 1)}

    c.setFillColor(colors.black)
    _marks(c, width, height, style["edge_marks"], encode_number(student["number"]) if student else 0, note)
    anchor_ids: dict[tuple[int, int], str] = {}
    for column in range(style["columns"] + 1):
        for row in range(style["edge_marks"]):
            if (column, row) not in corners_used:
                continue
            x, y = line_x(column), line_y(row)
            if 0 < column < style["columns"]:  # chetdagilari — chet belgilarining o'zi
                c.rect(x - EDGE_SIZE / 2, y - EDGE_SIZE / 2, EDGE_SIZE, EDGE_SIZE, stroke=0, fill=1)
            anchor_ids[(column, row)] = f"a{len(anchor_ids)}"
            note({"kind": "anchor", "ref": "anchor", "letter": anchor_ids[(column, row)], "x": x, "y": y,
                  "r": EDGE_SIZE / 2})

    def block(column: int, row: int) -> list[str]:
        return [anchor_ids[(column, row)], anchor_ids[(column + 1, row)],
                anchor_ids[(column + 1, row + 1)], anchor_ids[(column, row + 1)]]

    # Tepadagi quti: Ism-familiya | Imtihon | Sana
    box_top, box_middle, box_bottom = style["box"]
    font = style["box_font"]
    sana = style["sana_width"]
    box_left, box_right = CORNER + 16, width - CORNER - 16
    c.setStrokeColor(colors.HexColor("#555555"))
    c.setLineWidth(0.8)
    c.rect(box_left, top(box_bottom), box_right - box_left, box_bottom - box_top, stroke=1, fill=0)
    c.line(box_left, top(box_middle), box_right, top(box_middle))
    c.line(box_right - sana, top(box_bottom), box_right - sana, top(box_middle))
    c.setStrokeColor(colors.black)
    name_y = top(box_middle - (box_middle - box_top) / 2 + font * 0.35)
    exam_y = top(box_bottom - (box_bottom - box_middle) / 2 + font * 0.35)
    c.setFont("Sans", font)
    c.drawString(box_left + 6, name_y, "Ism-familiya :")
    c.drawString(box_left + 6, exam_y, "Imtihon :")
    c.drawString(box_right - sana + 6, exam_y, "Sana :")
    if student:
        name_x = box_left + 8 * font + c.stringWidth("Ism-familiya :", "Sans", font) - 7 * font
        name, size = _fit(c, student["name"][:60], "Sans-Bold", font, box_right - 6 - name_x, font * 0.7)
        c.setFont("Sans-Bold", size)
        c.drawString(name_x, name_y, name)
    exam_left = box_left + 5.6 * font + c.stringWidth("Imtihon :", "Sans", font) - 4 * font
    title, size = _fit(c, f"{test['title']}  ·  Kod {test['code']}", "Sans-Bold", font,
                       box_right - sana - exam_left - 6, font * 0.7)
    c.setFont("Sans-Bold", size)
    c.drawString(exam_left, exam_y, title)
    _fill_examples(c, top, width, CORNER + style["edge_marks"] * style["step"], style)

    # O'quvchi ID: 3 ustun, 0–9 ketma-ket (ikki blok bo'yi); tepada raqam yoziladigan kataklar
    digits = _student_digits(student)
    bubble_top = line_y(0) - header - 0.5 * style["row"] + radius
    box_low = bubble_top + 2.5
    box_height = min(GRID["header"] * style["scale"] + style["title_band"] - 3, 13 * min(style["scale"], 1.2))
    text_y = box_low + (box_height - style["letter_font"] * 0.7) / 2
    c.setFont("Sans-Bold", style["letter_font"])
    c.drawRightString(line_x(0) + label_right, text_y, "ID")
    for column in range(ID_DIGITS):
        x = option_x(0, column)
        c.rect(x - radius - 0.5, box_low, 2 * radius + 1, box_height, stroke=1, fill=0)
        if digits:
            c.drawCentredString(x, text_y, digits[column])
    c.setLineWidth(1)
    for digit in range(10):
        center = line_y(0) - header - (digit + 0.5) * style["row"]
        corners = block(0, 0 if line_y(0) - center < style["step"] else 1)
        c.setFont("Sans-Bold", style["digit_font"])
        c.drawRightString(line_x(0) + label_right, center - style["digit_font"] * 0.35, str(digit))
        for column in range(ID_DIGITS):
            x = option_x(0, column)
            c.circle(x, center, radius, stroke=1, fill=1 if digits and int(digits[column]) == digit else 0)
            note({"kind": "id", "ref": f"id{column}", "label": "id", "letter": str(digit),
                  "x": x, "y": center, "r": radius, "block": corners})
    if plan["variants"]:
        corners = block(0, 2)
        center = row_center(2, 0)
        c.setFont("Sans-Bold", style["letter_font"])
        c.drawRightString(line_x(0) + label_right, center - 2.5, "Var.")
        for index, name in enumerate(variants[:6]):
            x = option_x(0, index)
            c.drawCentredString(x, line_y(2) - header + 1.5, name)
            c.circle(x, center, radius, stroke=1, fill=1 if student and student.get("variant") == name else 0)
            note({"kind": "variant", "ref": "variant", "label": "variant", "letter": name,
                  "x": x, "y": center, "r": radius, "block": corners})

    # DTM: ustun sarlavhalari (kvadratlar ostida, A B C D harflari ustida)
    for column, row, caption, text in plan["titles"]:
        center = line_x(column) + column_width / 2
        room = column_width - EDGE_SIZE - 10
        text, size = _fit(c, f"{caption} · {text}", "Sans-Bold", 9 * min(style["scale"], 1.2), room, 6)
        c.setFont("Sans-Bold", size)
        c.drawCentredString(center, line_y(row) - EDGE_SIZE / 2 - 3 - size * 0.8, text)

    for items, column, row in plan["blocks"]:
        corners = block(column, row)
        c.setFont("Sans-Bold", style["letter_font"])
        for option in range(items[0]["options"]):
            c.drawCentredString(option_x(column, option), line_y(row) - header + 1.5, LETTERS[option])
        for line, item in enumerate(items):
            center = row_center(row, line)
            c.setFont("Sans", style["number_font"])
            c.drawRightString(line_x(column) + label_right, center - style["number_font"] * 0.35, item["label"])
            for option in range(item["options"]):
                x = option_x(column, option)
                c.circle(x, center, radius, stroke=1, fill=0)
                note({"kind": "single", "ref": item["label"], "label": item["label"], "letter": LETTERS[option],
                      "x": x, "y": center, "r": radius, "block": corners})


def _brand(c, card_bottom: float) -> None:
    """Titul ostida (skaner o'qimaydigan joyda) bot nomi."""
    y = card_bottom - 28
    if y < 30:
        return
    c.setFillColor(MUTED)
    c.setFont("Sans", 8)
    c.drawCentredString(A4[0] / 2, y, "Ona tili test boti · titulni o'qituvchi ilova orqali skanerlaydi")
    c.setFillColor(colors.black)


# ---------- Umumiy ----------

def plan_for(test: dict, variants: list[str]) -> dict:
    style = style_of(test)
    if style == "compact":
        return {"kind": "compact", **compact_plan(test, variants)}
    return {"kind": style, **grid_plan(test, variants, dtm=style == "dtm")}


def _draw_card(c, test: dict, plan: dict, variants: list[str], student: dict | None, note) -> None:
    (draw_compact if plan["kind"] == "compact" else draw_grid)(c, test, plan, variants, student, note)


def layout(test: dict, variants: list[str]) -> dict:
    """Skaner uchun: doirachalar va titul o'lchami (omr.SheetPage maydonlari)."""
    _fonts()
    plan = plan_for(test, variants)
    bubbles: list[dict] = []
    _draw_card(_NullCanvas(), test, plan, variants or ["A"], None, lambda item: bubbles.append({"page": 1, **item}))
    grid = plan["kind"] != "compact"
    page = {"width": plan["width"], "height": plan["height"], "edge_marks": plan["edge_marks"], "name_box": grid}
    if grid:
        page["name_area"] = list(plan["style"]["name_area"])
    return {"bubbles": bubbles, "written": [], "written_area": None, "page": page,
            "name_area": page.get("name_area")}


def draw_sheets(c, test: dict, variants: list[str], pages: list[dict | None]) -> None:
    """Titullar: ixcham — bitta A4 ga sig'ganicha (orasida qirqish chizig'i); qolganlari — har biri alohida varaqda."""
    _fonts()
    plan = plan_for(test, variants)
    per_sheet = max(1, int(A4[1] // (plan["height"] - 1))) if plan["kind"] == "compact" else 1
    left = (A4[0] - plan["width"]) / 2
    slot = 0
    for student in pages:
        if slot == per_sheet:
            c.showPage()
            slot = 0
        offset = A4[1] - (slot + 1) * plan["height"]
        c.saveState()
        c.translate(left, offset)
        _draw_card(c, test, plan, variants or ["A"], student, lambda item: None)
        c.restoreState()
        if per_sheet == 1:
            _brand(c, offset)
        if slot:
            c.saveState()
            c.setDash(4, 4)
            c.setStrokeColor(colors.grey)
            c.line(12, offset + plan["height"], A4[0] - 12, offset + plan["height"])
            c.restoreState()
        slot += 1
    c.showPage()
