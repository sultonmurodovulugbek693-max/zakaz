"""PDF fayllar: titul (javoblar varaqasi), javoblar kaliti va natijalar jadvali."""

import io
import math
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from config import FONTS_DIR
from scoring import rasch_results, total_points
from structure import (ESSAY_MAX, LETTERS, choice_items, fmt_num, format_title, max_points, percent, split_options,
                       text_items)

pdfmetrics.registerFont(TTFont("Sans", str(FONTS_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("Sans-Bold", str(FONTS_DIR / "DejaVuSans-Bold.ttf")))

INK = colors.HexColor("#1f2933")
ACCENT = colors.HexColor("#1d6f42")
LIGHT = colors.HexColor("#e8f3ec")
GRID = colors.HexColor("#9aa5b1")

STYLES = {
    "title": ParagraphStyle("title", fontName="Sans-Bold", fontSize=15, leading=19, alignment=TA_CENTER, textColor=INK),
    "sub": ParagraphStyle("sub", fontName="Sans", fontSize=10, leading=13, alignment=TA_CENTER, textColor=INK),
    "h2": ParagraphStyle("h2", fontName="Sans-Bold", fontSize=12, leading=16, textColor=ACCENT, spaceBefore=8, spaceAfter=4),
    "cell": ParagraphStyle("cell", fontName="Sans", fontSize=9, leading=11, textColor=INK),
}


# ---------- Titul (javoblar varaqasi) ----------

def _bubble(c: canvas.Canvas, x: float, y: float, letter: str, radius: float = 2.6 * mm, filled: bool = False) -> None:
    c.setLineWidth(0.8)
    c.setStrokeColor(INK)
    if filled:
        c.setFillColor(INK)
        c.circle(x, y, radius, stroke=1, fill=1)
        return
    c.setFillColor(colors.white)
    c.circle(x, y, radius, stroke=1, fill=1)
    c.setFillColor(INK)
    c.setFont("Sans", 7)
    c.drawCentredString(x, y - 2.4, letter)


def answer_sheet_pdf(test: dict, variant_names: list[str]) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin = 14 * mm
    y = height - margin

    # Sarlavha
    c.setFillColor(ACCENT)
    c.rect(margin, y - 12 * mm, width - 2 * margin, 12 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Sans-Bold", 14)
    c.drawCentredString(width / 2, y - 8 * mm, "ONA TILI — JAVOBLAR VARAQASI")
    y -= 19 * mm

    c.setFillColor(INK)
    c.setFont("Sans", 10)
    c.drawString(margin, y, f"Test: {test['title'][:60]}")
    c.setFont("Sans-Bold", 12)
    c.drawRightString(width - margin, y, f"Test kodi: {test['code']}")
    y -= 6 * mm
    c.setFont("Sans", 9)
    c.drawString(margin, y, format_title(test["fmt"], test["count"]))
    y -= 10 * mm

    # F.I.Sh. va variant
    c.setFont("Sans-Bold", 10)
    c.drawString(margin, y, "F.I.Sh.:")
    c.setLineWidth(0.6)
    c.line(margin + 17 * mm, y - 1, width - margin - 62 * mm, y - 1)
    c.drawString(width - margin - 55 * mm, y, "Variant:")
    names = variant_names or ["A"]
    for index, name in enumerate(names):
        _bubble(c, width - margin - 33 * mm + index * 9 * mm, y + 1.2 * mm, name, radius=3.2 * mm)
    y -= 10 * mm

    # Ko'rsatma
    c.setFont("Sans", 8.5)
    c.drawString(margin, y, "Har bir savolga bitta javobni to'liq bo'yang:")
    _bubble(c, margin + 70 * mm, y + 1.2 * mm, "", radius=2.2 * mm, filled=True)
    c.drawString(margin + 75 * mm, y, "to'g'ri.   Yozma javoblarni katakka aniq yozing.")
    y -= 8 * mm

    # Variantli savollar
    choices = choice_items(test["items"])
    first, last = choices[0]["label"], choices[-1]["label"]
    c.setFont("Sans-Bold", 10.5)
    c.setFillColor(ACCENT)
    c.drawString(margin, y, f"{first}–{last} savollar (test)")
    c.setFillColor(INK)
    y -= 6 * mm

    if test["fmt"] == "milliy":
        columns = 3
    else:
        columns = max(2 if len(choices) > 10 else 1, math.ceil(len(choices) / 25))
    rows = math.ceil(len(choices) / columns)
    row_h = 7.2 * mm
    col_w = (width - 2 * margin) / columns
    box_top = y + 5 * mm
    box_h = rows * row_h + 2 * mm
    c.setStrokeColor(GRID)
    c.setLineWidth(0.5)
    c.roundRect(margin, box_top - box_h, width - 2 * margin, box_h, 2 * mm, stroke=1, fill=0)
    for index, item in enumerate(choices):
        col, row = divmod(index, rows)
        x = margin + col * col_w + 4 * mm
        row_y = y - row * row_h
        if row % 2 == 0:
            c.setFillColor(LIGHT)
            c.rect(margin + col * col_w + 1 * mm, row_y - 2.6 * mm, col_w - 2 * mm, row_h - 0.6 * mm, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont("Sans-Bold", 9)
        c.drawRightString(x + 5 * mm, row_y - 1, item["label"])
        for option in range(item["options"]):
            _bubble(c, x + 10 * mm + option * 6.8 * mm, row_y + 0.6 * mm, LETTERS[option])
    y = box_top - box_h - 8 * mm

    # Yozma savollar
    texts = text_items(test["items"])
    if texts:
        c.setFont("Sans-Bold", 10.5)
        c.setFillColor(ACCENT)
        c.drawString(margin, y, f"{texts[0]['label'].rstrip('ab')}–{texts[-1]['label'].rstrip('ab')} savollar (yozma)")
        c.setFillColor(INK)
        y -= 4 * mm
        text_rows = math.ceil(len(texts) / 2)
        text_col_w = (width - 2 * margin) / 2
        text_row_h = 9.5 * mm
        for index, item in enumerate(texts):
            col, row = divmod(index, text_rows)
            x = margin + col * text_col_w
            box_y = y - (row + 1) * text_row_h
            c.setFont("Sans-Bold", 9.5)
            c.drawRightString(x + 9 * mm, box_y + 2.8 * mm, item["label"])
            c.setStrokeColor(GRID)
            c.rect(x + 11 * mm, box_y + 0.5 * mm, text_col_w - 14 * mm, text_row_h - 1.5 * mm, stroke=1, fill=0)
        y -= text_rows * text_row_h + 8 * mm

    if test["fmt"] == "milliy":
        c.setFont("Sans-Bold", 10)
        c.drawString(margin, y, "45 — Esse")
        c.setFont("Sans", 9)
        c.drawString(margin + 22 * mm, y, f"esse varaqasiga yoziladi (keyingi sahifa, {ESSAY_MAX} ball).")
        y -= 6 * mm
        c.drawString(margin, y, "Diqqat! Yozma javoblarni savol daftarchasidan javoblar varaqasiga ko'chirib yozing.")

    _footer(c, width, test)
    c.showPage()
    if test["fmt"] == "milliy":
        _draw_essay_page(c, width, height, margin, test)
        _footer(c, width, test)
        c.showPage()
    c.save()
    return buffer.getvalue()


def _footer(c: canvas.Canvas, width: float, test: dict) -> None:
    c.setFont("Sans", 7)
    c.setFillColor(GRID)
    c.drawCentredString(width / 2, 8 * mm, f"Natijani bilish: botga test kodi ({test['code']}) va javoblaringizni yuboring")
    c.setFillColor(INK)


ESSAY_RULES = [
    "I. Kirish — 2–3 jumla; berilgan vaziyat matnini aynan ko'chirmang.",
    "II. Asosiy qism — kamida 3 ta xatboshi: tomonlarning qarashlari va shaxsiy fikringiz, hayotiy misollar bilan.",
    "III. Xulosa — 2–3 jumla, asosiy qismdagi fikrlarni umumlashtiring.",
    "Publitsistik uslubda, adabiy til me'yorlariga amal qilib yozing. Reja tuzilmaydi, epigraf qo'yilmaydi.",
]


def _draw_essay_page(c: canvas.Canvas, width: float, height: float, margin: float, test: dict) -> None:
    """2-sahifa: esse varaqasi (rasmiy namunadagi talablar bilan)."""
    y = height - margin
    c.setFillColor(ACCENT)
    c.rect(margin, y - 10 * mm, width - 2 * margin, 10 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Sans-Bold", 12.5)
    c.drawCentredString(width / 2, y - 7 * mm, f"45. ESSE VARAQASI ({ESSAY_MAX} ball)")
    y -= 16 * mm
    c.setFillColor(INK)
    c.setFont("Sans-Bold", 10)
    c.drawString(margin, y, "F.I.Sh.:")
    c.setLineWidth(0.6)
    c.line(margin + 17 * mm, y - 1, width - margin - 40 * mm, y - 1)
    c.drawRightString(width - margin, y, f"Test kodi: {test['code']}")
    y -= 8 * mm
    c.setFont("Sans", 8.5)
    for rule in ESSAY_RULES:
        c.drawString(margin, y, "• " + rule)
        y -= 4.6 * mm
    y -= 3 * mm
    c.setStrokeColor(GRID)
    c.setLineWidth(0.5)
    while y > 16 * mm:
        c.line(margin, y, width - margin, y)
        y -= 8 * mm


# ---------- Javoblar kaliti ----------

def answer_key_pdf(test: dict, keys: dict[str, dict[str, str]]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm, title=f"Kalit — {test['title']}")
    story = [
        Paragraph("JAVOBLAR KALITI", STYLES["title"]),
        Paragraph(f"{_esc(test['title'])} · kod {test['code']} · {format_title(test['fmt'], test['count'])}", STYLES["sub"]),
        Paragraph("Bu varaqni o'quvchilarga bermang.", STYLES["sub"]),
    ]
    choices = choice_items(test["items"])
    texts = text_items(test["items"])
    per_row = 10
    for name, key in keys.items():
        story.append(Paragraph(f"{name} variant", STYLES["h2"]))
        cells = [f"{item['label']}. {key.get(item['label'], '')}" for item in choices]
        cells += [""] * (-len(cells) % per_row)
        grid = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
        table = Table(grid, colWidths=[(A4[0] - 30 * mm) / per_row] * per_row)
        table.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, -1), "Sans", 9.5),
            ("GRID", (0, 0), (-1, -1), 0.4, GRID),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(table)
        if texts:
            story.append(Spacer(1, 4 * mm))
            rows = [[item["label"], Paragraph(" <b>/</b> ".join(_esc(o) for o in split_options(key.get(item["label"], ""))), STYLES["cell"])]
                    for item in texts]
            table = Table(rows, colWidths=[14 * mm, A4[0] - 44 * mm])
            table.setStyle(TableStyle([
                ("FONT", (0, 0), (0, -1), "Sans-Bold", 9.5),
                ("GRID", (0, 0), (-1, -1), 0.4, GRID),
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            story.append(table)
    doc.build(story)
    return buffer.getvalue()


# ---------- Natijalar ----------

def ranked(attempts: list[dict], rasch: dict | None = None) -> list[dict]:
    """O'rinlar bo'yicha: Rasch bali (bo'lsa), keyin to'plangan ball."""
    rasch = rasch or {}
    return sorted(
        attempts,
        key=lambda a: (-rasch.get(a["id"], (0, ""))[0], -total_points(a), a["created_at"] or ""),
    )


def results_pdf(test: dict, attempts: list[dict]) -> bytes:
    if test["fmt"] == "milliy":
        return _milliy_results_pdf(test, attempts)
    return _simple_results_pdf(test, attempts)


def _milliy_results_pdf(test: dict, attempts: list[dict]) -> bytes:
    buffer = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=f"Natijalar — {test['title']}")
    choices, texts = choice_items(test["items"]), text_items(test["items"])
    top = max_points(test["items"], test["fmt"])
    rasch = rasch_results(test, attempts)
    story = [
        Paragraph("ONA TILI VA ADABIYOT — MOCK NATIJALARI", STYLES["title"]),
        Paragraph(
            f"{_esc(test['title'])} · kod {test['code']} · {len(attempts)} ta qatnashchi · {datetime.now():%d.%m.%Y %H:%M}",
            STYLES["sub"],
        ),
        Spacer(1, 5 * mm),
    ]
    header = ["№", "F.I.Sh.", "Var.", f"Test\n(1–35)", f"Yozma\n(36–44)", f"Esse\n({ESSAY_MAX})",
              f"Ball\n({fmt_num(top)})", "Rasch\nbali", "Daraja", "Xato savollar"]
    rows = [header]
    for place, attempt in enumerate(ranked(attempts, rasch), start=1):
        score, grade = rasch.get(attempt["id"], (0.0, "—"))
        rows.append([
            str(place), Paragraph(_esc(attempt["student_name"]), STYLES["cell"]), attempt["variant"],
            f"{attempt['test_correct']}/{len(choices)}", f"{attempt['written_correct']}/{len(texts)}",
            "—" if attempt["essay"] is None else fmt_num(attempt["essay"]),
            fmt_num(total_points(attempt)), fmt_num(score), grade,
            Paragraph(", ".join(attempt["wrong"]) or "—", STYLES["cell"]),
        ])
    if len(rows) == 1:
        rows.append(["", "Hali hech kim javob yubormagan"] + [""] * (len(header) - 2))

    fixed = [10 * mm, 60 * mm, 12 * mm, 17 * mm, 17 * mm, 15 * mm, 16 * mm, 16 * mm, 15 * mm]
    table = Table(rows, colWidths=fixed + [page[0] - 24 * mm - sum(fixed)], repeatRows=1)
    table.setStyle(_results_style())
    story.append(table)
    story.append(Spacer(1, 4 * mm))
    essay_note = (
        "Esse hamma qatnashchida baholangani uchun Rasch'ga qo'shildi."
        if attempts and all(a["essay"] is not None for a in attempts)
        else "Esse hamma qatnashchida baholanmagan — Rasch bali hozircha essesiz hisoblandi."
    )
    story.append(Paragraph(
        "<b>Ball</b> — rasmiy namunadagi savol ballari bo'yicha (1,1 / 1,7 / 2,5; esse 24; jami 100). "
        "<b>Rasch bali</b> — Milliy sertifikat uslubida (T-shkala, o'rtacha 50), shu testni ishlagan "
        "qatnashchilar javoblaridan hisoblanadi: qatnashchilar ko'p bo'lsa aniqroq. Bu taxminiy natija, rasmiy emas. "
        f"{essay_note}<br/><b>Darajalar:</b> A+ — 70 va yuqori · A — 65–69,9 · B+ — 60–64,9 · B — 55–59,9 · "
        "C+ — 50–54,9 · C — 46–49,9 · NC — 46 dan past (sertifikat berilmaydi).",
        STYLES["cell"],
    ))
    doc.build(story)
    return buffer.getvalue()


def _results_style() -> TableStyle:
    return TableStyle([
        ("FONT", (0, 0), (-1, 0), "Sans-Bold", 8.5),
        ("FONT", (0, 1), (-1, -1), "Sans", 9),
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 0), (-2, -1), "CENTER"),
    ])


def _simple_results_pdf(test: dict, attempts: list[dict]) -> bytes:
    buffer = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=f"Natijalar — {test['title']}")
    milliy = False
    choices, texts = choice_items(test["items"]), text_items(test["items"])
    story = [
        Paragraph("ONA TILI — NATIJALAR", STYLES["title"]),
        Paragraph(
            f"{_esc(test['title'])} · kod {test['code']} · {format_title(test['fmt'], test['count'])} · "
            f"{len(attempts)} ta qatnashchi · {datetime.now():%d.%m.%Y %H:%M}",
            STYLES["sub"],
        ),
        Spacer(1, 5 * mm),
    ]
    header = ["№", "F.I.Sh.", "Variant", f"Test ({len(choices)})"]
    if milliy:
        header += [f"Yozma ({len(texts)})", f"Esse ({ESSAY_MAX})"]
    header += ["Jami", "Foiz", "Xato savollar"]
    rows = [header]
    for place, attempt in enumerate(ranked(attempts), start=1):
        correct = attempt["test_correct"] + attempt["written_correct"]
        row = [str(place), Paragraph(_esc(attempt["student_name"]), STYLES["cell"]), attempt["variant"],
               str(attempt["test_correct"])]
        if milliy:
            row += [str(attempt["written_correct"]), "—" if attempt["essay"] is None else str(attempt["essay"])]
        row += [f"{correct}/{attempt['auto_total']}", f"{percent(correct, attempt['auto_total'])}%",
                Paragraph(", ".join(attempt["wrong"]) or "—", STYLES["cell"])]
        rows.append(row)
    if len(rows) == 1:
        rows.append(["", "Hali hech kim javob yubormagan"] + [""] * (len(header) - 2))

    fixed = [10 * mm, 62 * mm, 16 * mm, 18 * mm] + ([22 * mm, 19 * mm] if milliy else []) + [18 * mm, 16 * mm]
    widths = fixed + [page[0] - 24 * mm - sum(fixed)]
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(_results_style())
    story.append(table)
    doc.build(story)
    return buffer.getvalue()


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
