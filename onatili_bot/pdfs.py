"""PDF fayllar: esse varaqasi, javoblar kaliti va natijalar jadvali."""

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from config import FONTS_DIR
from scoring import rasch_results, total_points
from structure import (ESSAY_MAX, ESSAY_SCALE, choice_items, essay_to_75, fmt_num, format_title, max_points, percent, split_options,
                       text_items)

pdfmetrics.registerFont(TTFont("Sans", str(FONTS_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("Sans-Bold", str(FONTS_DIR / "DejaVuSans-Bold.ttf")))
# <b> teglari matnda qalin bo'lishi uchun shrift oilasi
pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold", italic="Sans", boldItalic="Sans-Bold")

INK = colors.HexColor("#1f2933")
ACCENT = colors.HexColor("#4b4ac0")  # ilova bilan bir xil siyohrang-ko'k
LIGHT = colors.HexColor("#eeedfb")
GRID = colors.HexColor("#9aa5b1")

STYLES = {
    "title": ParagraphStyle("title", fontName="Sans-Bold", fontSize=15, leading=19, alignment=TA_CENTER, textColor=INK),
    "sub": ParagraphStyle("sub", fontName="Sans", fontSize=10, leading=13, alignment=TA_CENTER, textColor=INK),
    "h2": ParagraphStyle("h2", fontName="Sans-Bold", fontSize=12, leading=16, textColor=ACCENT, spaceBefore=8, spaceAfter=4),
    "cell": ParagraphStyle("cell", fontName="Sans", fontSize=9, leading=11, textColor=INK),
    "cell_small": ParagraphStyle("cell_small", fontName="Sans", fontSize=8, leading=10, textColor=INK),
    "small": ParagraphStyle("small", fontName="Sans", fontSize=8, leading=10.5, textColor=colors.HexColor("#52606d")),
    "h1": ParagraphStyle("h1", fontName="Sans-Bold", fontSize=14, leading=18, textColor=INK, spaceAfter=2),
    "banner": ParagraphStyle("banner", fontName="Sans-Bold", fontSize=15, leading=19, textColor=colors.white),
    "banner_sub": ParagraphStyle("banner_sub", fontName="Sans", fontSize=9.5, leading=12,
                                 textColor=colors.HexColor("#dcdbf7")),
    "card_value": ParagraphStyle("card_value", fontName="Sans-Bold", fontSize=15, leading=18, alignment=TA_CENTER,
                                 textColor=ACCENT),
    "card_label": ParagraphStyle("card_label", fontName="Sans", fontSize=8, leading=10, alignment=TA_CENTER,
                                 textColor=colors.HexColor("#52606d")),
    "chip": ParagraphStyle("chip", fontName="Sans", fontSize=9, leading=11, alignment=TA_CENTER, textColor=colors.white),
    "analysis": ParagraphStyle("analysis", fontName="Sans", fontSize=8.5, leading=11, alignment=TA_CENTER,
                               textColor=INK),
}


# ---------- Esse varaqasi (titul bilan birga chop etiladi) ----------

def _footer(c: canvas.Canvas, width: float, test: dict) -> None:
    c.setFont("Sans", 7)
    c.setFillColor(GRID)
    c.drawCentredString(width / 2, 8 * mm, f"Ona tili test boti · test kodi {test['code']} · varaqni o'qituvchiga topshiring")
    c.setFillColor(INK)


ESSAY_RULES = [
    "I. Kirish — 2–3 jumla; berilgan vaziyat matnini aynan ko'chirmang.",
    "II. Asosiy qism — kamida 3 ta xatboshi: tomonlarning qarashlari va shaxsiy fikringiz, hayotiy misollar bilan.",
    "III. Xulosa — 2–3 jumla, asosiy qismdagi fikrlarni umumlashtiring.",
    "Publitsistik uslubda, adabiy til me'yorlariga amal qilib yozing. Reja tuzilmaydi, epigraf qo'yilmaydi.",
]


def draw_essay_page(c: canvas.Canvas, test: dict, student: dict | None = None) -> None:
    """Esse varaqasi (oflayn titul to'plamida har o'quvchiga) — ismi va raqami bosilgan bo'lsa ko'rsatiladi."""
    width, height = A4
    _draw_essay_page(c, width, height, 14 * mm, test, student)
    _footer(c, width, test)


def _draw_essay_page(c: canvas.Canvas, width: float, height: float, margin: float, test: dict,
                     student: dict | None = None) -> None:
    """2-sahifa: esse varaqasi (rasmiy namunadagi talablar bilan)."""
    y = height - margin
    c.setFillColor(ACCENT)
    c.rect(margin, y - 10 * mm, width - 2 * margin, 10 * mm, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Sans-Bold", 12.5)
    c.drawCentredString(width / 2, y - 7 * mm, f"45. ESSE VARAQASI ({ESSAY_MAX} ballik mezon → {ESSAY_SCALE} ballik shkala)")
    y -= 16 * mm
    c.setFillColor(INK)
    c.setFont("Sans-Bold", 10)
    c.drawString(margin, y, "F.I.Sh.:")
    c.setLineWidth(0.6)
    c.line(margin + 17 * mm, y - 1, width - margin - 40 * mm, y - 1)
    c.drawRightString(width - margin, y, f"Test kodi: {test['code']}")
    if student:
        c.setFont("Sans-Bold", 11)
        c.drawString(margin + 19 * mm, y + 1, f"{student.get('name', '')[:40]}  ·  ID {student['number']:04d}")
        c.setFont("Sans-Bold", 10)
    y -= 7 * mm
    topic = (test.get("essay_topic") or "").strip()
    if topic:
        # O'qituvchi kiritgan esse mavzusi (45-savol vaziyati) — ramka ichida
        style = ParagraphStyle("topic", fontName="Sans", fontSize=10.5, leading=14.5, textColor=INK)
        para = Paragraph("<b>Mavzu:</b> " + _esc(topic).replace("\n", "<br/>"), style)
        box_width = width - 2 * margin
        _, text_height = para.wrap(box_width - 8 * mm, 200 * mm)
        box_height = text_height + 6 * mm
        c.setFillColor(LIGHT)
        c.setStrokeColor(ACCENT)
        c.setLineWidth(0.8)
        c.roundRect(margin, y - box_height, box_width, box_height, 2 * mm, stroke=1, fill=1)
        para.drawOn(c, margin + 4 * mm, y - box_height + 3 * mm)
        y -= box_height + 5 * mm
    else:
        c.setFont("Sans-Bold", 10)
        c.drawString(margin, y - 2, "Mavzu:")
        c.setStrokeColor(GRID)
        c.line(margin + 16 * mm, y - 3, width - margin, y - 3)
        y -= 10 * mm
    y -= 1 * mm
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

GRADE_COLORS = {
    "A+": "#0f5132", "A": "#1d6f42", "B+": "#2e8b57", "B": "#5a9e3a",
    "C+": "#b08900", "C": "#c77c02", "NC": "#9b2c2c",
}
GOOD, MID, BAD = colors.HexColor("#d4edda"), colors.HexColor("#fff3cd"), colors.HexColor("#f8d7da")


def ranked(attempts: list[dict], rasch: dict | None = None) -> list[dict]:
    """O'rinlar bo'yicha: Rasch bali (bo'lsa), keyin to'plangan ball."""
    rasch = rasch or {}
    return sorted(
        attempts,
        key=lambda a: (-rasch.get(a["id"], (0, ""))[0], -total_points(a), a["created_at"] or ""),
    )


def results_pdf(test: dict, attempts: list[dict]) -> bytes:
    buffer = io.BytesIO()
    page = landscape(A4)
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=14 * mm, title=f"Natijalar — {test['title']}")
    width = page[0] - 24 * mm
    milliy = test["fmt"] == "milliy"
    rasch = rasch_results(test, attempts) if milliy and attempts else {}

    story = [_banner(test, width), Spacer(1, 4 * mm), _summary_cards(test, attempts, rasch, width)]
    if milliy and rasch:
        story += [Spacer(1, 3 * mm), _grade_strip(rasch, width)]
    story.append(Spacer(1, 5 * mm))
    story.append(_milliy_table(test, attempts, rasch, width) if milliy else _simple_table(test, attempts, width))
    if milliy:
        story += [Spacer(1, 4 * mm), Paragraph(_milliy_note(attempts), STYLES["small"])]
    if attempts:
        story += [PageBreak(), Paragraph("Savollar tahlili", STYLES["h1"]),
                  Paragraph("Har bir savolni necha foiz qatnashchi to'g'ri yechgani. "
                            "Qizil — qiyin (40% dan kam), sariq — o'rtacha, yashil — oson (70% va ko'p).", STYLES["small"]),
                  Spacer(1, 3 * mm), _analysis_table(test, attempts, width)]
    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return buffer.getvalue()


def _page_footer(c: canvas.Canvas, doc) -> None:
    c.saveState()
    c.setFont("Sans", 7.5)
    c.setFillColor(GRID)
    c.drawString(12 * mm, 7 * mm, "Ona tili test boti · Milliy sertifikat mock")
    c.drawRightString(doc.pagesize[0] - 12 * mm, 7 * mm, f"{doc.page}-sahifa")
    c.restoreState()


def _banner(test: dict, width: float) -> Table:
    title = "ONA TILI VA ADABIYOT — MOCK NATIJALARI" if test["fmt"] == "milliy" else "ONA TILI — TEST NATIJALARI"
    info = (f"{_esc(test['title'])}  ·  kod {test['code']}  ·  {format_title(test['fmt'], test['count'])}  ·  "
            f"{datetime.now():%d.%m.%Y %H:%M}")
    table = Table([[Paragraph(title, STYLES["banner"])], [Paragraph(info, STYLES["banner_sub"])]], colWidths=[width])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("TOPPADDING", (0, 0), (-1, 0), 9), ("BOTTOMPADDING", (0, -1), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
    ]))
    return table


def _summary_cards(test: dict, attempts: list[dict], rasch: dict, width: float) -> Table:
    count = len(attempts)
    if test["fmt"] == "milliy":
        top = max_points(test["items"], test["fmt"])
        totals = [total_points(a) for a in attempts] or [0]
        certified = sum(1 for _, grade in rasch.values() if grade != "NC")
        cards = [
            ("Qatnashchilar", str(count)),
            ("O'rtacha ball", f"{fmt_num(sum(totals) / max(count, 1))} / {fmt_num(top)}"),
            ("Eng yuqori ball", fmt_num(max(totals))),
            ("O'rtacha Rasch", fmt_num(sum(s for s, _ in rasch.values()) / len(rasch)) if rasch else "—"),
            ("Sertifikat (C va yuqori)", f"{certified} ta" if rasch else "—"),
        ]
    else:
        shares = [percent(a["test_correct"] + a["written_correct"], a["auto_total"]) for a in attempts] or [0]
        cards = [
            ("Qatnashchilar", str(count)),
            ("O'rtacha natija", f"{fmt_num(sum(shares) / max(count, 1))}%"),
            ("Eng yuqori", f"{fmt_num(max(shares))}%"),
            ("80% va yuqori", f"{sum(1 for s in shares if s >= 80) if attempts else 0} ta"),
        ]
    cells = [[Paragraph(value, STYLES["card_value"]) for _, value in cards],
             [Paragraph(label, STYLES["card_label"]) for label, _ in cards]]
    gap = 3 * mm
    table = Table(cells, colWidths=[width / len(cards)] * len(cards))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("LINEABOVE", (0, 0), (-1, 0), 2.5, ACCENT),
        ("LINEAFTER", (0, 0), (-2, -1), gap, colors.white),
        ("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, -1), (-1, -1), 7),
    ]))
    return table


def _grade_strip(rasch: dict, width: float) -> Table:
    """Darajalar taqsimoti: A+ 2 · A 3 · ... rangli kataklarda."""
    order = ["A+", "A", "B+", "B", "C+", "C", "NC"]
    counts = {grade: 0 for grade in order}
    for _, grade in rasch.values():
        counts[grade] += 1
    cells = [[Paragraph(f"<b>{grade}</b>   {counts[grade]} ta", STYLES["chip"]) for grade in order]]
    table = Table(cells, colWidths=[width / len(order)] * len(order))
    style = [("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
             ("LINEAFTER", (0, 0), (-2, -1), 2, colors.white)]
    for index, grade in enumerate(order):
        style.append(("BACKGROUND", (index, 0), (index, 0), colors.HexColor(GRADE_COLORS[grade])))
    table.setStyle(TableStyle(style))
    return table


def _milliy_table(test: dict, attempts: list[dict], rasch: dict, width: float) -> Table:
    choices, texts = choice_items(test["items"]), text_items(test["items"])
    top = max_points(test["items"], test["fmt"])
    header = ["№", "F.I.Sh.", "Var.", "Test\n(1–35)", "Yozma\n(36–44)", f"Esse\n({ESSAY_SCALE})",
              f"Ball\n({fmt_num(top)})", "Umumiy\n(75)", "Daraja", "Xato savollar"]
    rows = [header]
    style = _table_style()
    for place, attempt in enumerate(ranked(attempts, rasch), start=1):
        score, grade = rasch.get(attempt["id"], (0.0, "—"))
        rows.append([
            str(place), Paragraph(_esc(attempt["student_name"]), STYLES["cell"]), attempt["variant"],
            f"{attempt['test_correct']}/{len(choices)}", f"{attempt['written_correct']}/{len(texts)}",
            "—" if attempt["essay"] is None else fmt_num(essay_to_75(attempt["essay"])),
            fmt_num(total_points(attempt)), fmt_num(score), grade,
            Paragraph(", ".join(attempt["wrong"]) or "—", STYLES["cell_small"]),
        ])
        if grade in GRADE_COLORS:
            style.add("BACKGROUND", (8, place), (8, place), colors.HexColor(GRADE_COLORS[grade]))
            style.add("TEXTCOLOR", (8, place), (8, place), colors.white)
            style.add("FONT", (8, place), (8, place), "Sans-Bold", 9)
    if len(rows) == 1:
        rows.append(["", "Hali hech kim javob yubormagan"] + [""] * (len(header) - 2))
    fixed = [10 * mm, 58 * mm, 12 * mm, 17 * mm, 17 * mm, 15 * mm, 16 * mm, 16 * mm, 16 * mm]
    table = Table(rows, colWidths=fixed + [width - sum(fixed)], repeatRows=1)
    table.setStyle(style)
    return table


def _simple_table(test: dict, attempts: list[dict], width: float) -> Table:
    header = ["№", "F.I.Sh.", "Variant", "To'g'ri", "Foiz", "Xato savollar"]
    rows = [header]
    style = _table_style()
    for place, attempt in enumerate(ranked(attempts), start=1):
        correct = attempt["test_correct"] + attempt["written_correct"]
        share = percent(correct, attempt["auto_total"])
        rows.append([str(place), Paragraph(_esc(attempt["student_name"]), STYLES["cell"]), attempt["variant"],
                     f"{correct}/{attempt['auto_total']}", f"{fmt_num(share)}%",
                     Paragraph(", ".join(attempt["wrong"]) or "—", STYLES["cell_small"])])
        style.add("BACKGROUND", (4, place), (4, place), GOOD if share >= 70 else MID if share >= 40 else BAD)
    if len(rows) == 1:
        rows.append(["", "Hali hech kim javob yubormagan"] + [""] * (len(header) - 2))
    fixed = [10 * mm, 70 * mm, 18 * mm, 20 * mm, 18 * mm]
    table = Table(rows, colWidths=fixed + [width - sum(fixed)], repeatRows=1)
    table.setStyle(style)
    return table


def _table_style() -> TableStyle:
    return TableStyle([
        ("FONT", (0, 0), (-1, 0), "Sans-Bold", 8.5),
        ("FONT", (0, 1), (-1, -1), "Sans", 9),
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f5fc")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#dedcf2")),
        ("BOX", (0, 0), (-1, -1), 0.6, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 0), (-2, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])


def _analysis_table(test: dict, attempts: list[dict], width: float) -> Table:
    """Har savol: necha foiz to'g'ri yechgan — rangli katak (10 tadan qatorda)."""
    per_row = 10
    cells, shades = [], []
    for item in test["items"]:
        solved = sum(1 for a in attempts if item["label"] not in a["wrong"])
        share = solved * 100 / len(attempts)
        cells.append(Paragraph(f"<b>{item['label']}</b><br/>{round(share)}%", STYLES["analysis"]))
        shades.append(GOOD if share >= 70 else MID if share >= 40 else BAD)
    while len(cells) % per_row:
        cells.append("")
        shades.append(colors.white)
    rows = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
    style = [("GRID", (0, 0), (-1, -1), 1.5, colors.white), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    for index, shade in enumerate(shades):
        cell = (index % per_row, index // per_row)
        style.append(("BACKGROUND", cell, cell, shade))
    table = Table(rows, colWidths=[width / per_row] * per_row)
    table.setStyle(TableStyle(style))
    return table


def _milliy_note(attempts: list[dict]) -> str:
    essay_note = (
        "" if attempts and all(a["essay"] is not None for a in attempts)
        else "Essesi hali baholanmagan qatnashchida umumiy ball o'rnida faqat test qismi turibdi."
    )
    return (
        "<b>Ball</b> — UZBMB rasmiy namunasidagi savol ballari bo'yicha (1,1 / 1,7 / 2,5; esse 24; jami 100). "
        "<b>Umumiy (75)</b> — DTM tartibida: test qismi (Rasch, T-shkala, eng yuqori 75) va esse (24 ballik mezon → "
        "75 ballik shkala) o'rtachasi. Rasch shu testni ishlagan qatnashchilar javoblaridan hisoblanadi: qatnashchilar "
        f"ko'p bo'lsa aniqroq. Taxminiy natija, rasmiy emas. {essay_note}<br/><b>Darajalar:</b> A+ — 70 va yuqori · "
        "A — 65–69,9 · B+ — 60–64,9 · B — 55–59,9 · C+ — 50–54,9 · C — 46–49,9 · NC — 46 dan past (sertifikat berilmaydi)."
    )


# ---------- Mock sertifikat (Natijalar bo'limi) ----------
# O'zimizning dizayn: rasmiy (DTM) sertifikatga o'xshatilmaydi va pastida «rasmiy emas» deb yoziladi.

GOLD = colors.HexColor("#b8923a")
MUTED = colors.HexColor("#52606d")
PAPER = colors.HexColor("#fbfaf6")
GRADE_SCALE = "A+ 70–75 · A 65–69,9 · B+ 60–64,9 · B 55–59,9 · C+ 50–54,9 · C 46–49,9 · NC — 46 dan past"


def _date(value: str | None) -> str:
    """2026-11-08 → 08.11.2026"""
    parts = (value or "")[:10].split("-")
    return ".".join(reversed(parts)) if len(parts) == 3 else (value or "")


def _spaced(c: canvas.Canvas, x: float, y: float, text: str, font: str, size: float, space: float, color) -> None:
    """Harflar orasi kengaytirilgan sarlavha — markazda."""
    width = pdfmetrics.stringWidth(text, font, size) + space * (len(text) - 1)
    obj = c.beginText(x - width / 2, y)
    obj.setFont(font, size)
    obj.setCharSpace(space)
    obj.setFillColor(color)
    obj.textOut(text)
    obj.setCharSpace(0)  # Tc sahifa holatida qoladi — qaytarilmasa keyingi hamma matn ham kengayib ketadi
    c.drawText(obj)


def _fit(text: str, font: str, size: float, max_width: float, smallest: float = 10) -> float:
    while size > smallest and pdfmetrics.stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def certificates_pdf(test: dict, rows: list[dict], teacher: str) -> bytes:
    """Har qatnashchiga bitta sahifa (A4, yotiq). rows — results.test_results() qatorlari."""
    buffer = io.BytesIO()
    page = landscape(A4)
    c = canvas.Canvas(buffer, pagesize=page)
    c.setTitle(f"Sertifikatlar — {test['title']}")
    for row in rows:
        _certificate(c, page, test, row, teacher)
        c.showPage()
    c.save()
    return buffer.getvalue()


def _certificate(c: canvas.Canvas, page: tuple, test: dict, row: dict, teacher: str) -> None:
    w, h = page
    cx = w / 2
    c.setFillColor(PAPER)
    c.rect(0, 0, w, h, stroke=0, fill=1)
    c.setStrokeColor(ACCENT)
    c.setLineWidth(3)
    c.rect(12 * mm, 12 * mm, w - 24 * mm, h - 24 * mm)
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.8)
    c.rect(15.5 * mm, 15.5 * mm, w - 31 * mm, h - 31 * mm)
    c.setFillColor(GOLD)
    for x, y in ((15.5 * mm, 15.5 * mm), (w - 15.5 * mm, 15.5 * mm), (15.5 * mm, h - 15.5 * mm), (w - 15.5 * mm, h - 15.5 * mm)):
        path = c.beginPath()
        path.moveTo(x, y + 2.6 * mm)
        path.lineTo(x + 2.6 * mm, y)
        path.lineTo(x, y - 2.6 * mm)
        path.lineTo(x - 2.6 * mm, y)
        path.close()
        c.drawPath(path, stroke=0, fill=1)

    _spaced(c, cx, h - 34 * mm, "ONA TILI VA ADABIYOT · MOCK IMTIHON", "Sans-Bold", 10, 2.2, GOLD)
    _spaced(c, cx, h - 54 * mm, "SERTIFIKAT", "Sans-Bold", 40, 7, ACCENT)
    c.setFont("Sans", 11)
    c.setFillColor(MUTED)
    c.drawCentredString(cx, h - 65 * mm, "Ushbu sertifikat egasi")

    name = row["name"] or "—"
    size = _fit(name, "Sans-Bold", 30, w - 90 * mm, 16)
    c.setFont("Sans-Bold", size)
    c.setFillColor(INK)
    c.drawCentredString(cx, h - 81 * mm, name)
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.8)
    c.line(cx - 85 * mm, h - 85 * mm, cx + 85 * mm, h - 85 * mm)
    meta = " · ".join(part for part in (
        f"ID {row['number']:04d}" if row.get("number") is not None else "",
        f"{row['class_name']} sinfi" if row.get("class_name") else "") if part)
    if meta:
        c.setFont("Sans", 10.5)
        c.setFillColor(MUTED)
        c.drawCentredString(cx, h - 91 * mm, meta)
    line = f"«{test['title']}» mock imtihonida ({_date(row.get('date'))}) quyidagi natijani ko'rsatdi:"
    c.setFont("Sans", _fit(line, "Sans", 12, w - 70 * mm))
    c.setFillColor(INK)
    c.drawCentredString(cx, h - 101 * mm, line)

    # Natija: chapda umumiy ball, o'rtada daraja, o'ngda bo'limlar
    band = 72 * mm
    card_w, card_h = 66 * mm, 34 * mm
    for x in (cx - 82 * mm, cx + 82 * mm):
        c.setFillColor(LIGHT)
        c.roundRect(x - card_w / 2, band - card_h / 2, card_w, card_h, 4 * mm, stroke=0, fill=1)
    left = cx - 82 * mm
    c.setFillColor(MUTED)
    c.setFont("Sans-Bold", 8.5)
    c.drawCentredString(left, band + 9 * mm, "UMUMIY BALL")
    score = fmt_num(row["score"]) if row.get("score") is not None else "—"
    c.setFillColor(ACCENT)
    c.setFont("Sans-Bold", 30)
    value_w = pdfmetrics.stringWidth(score, "Sans-Bold", 30)
    c.drawString(left - (value_w + pdfmetrics.stringWidth(" / 75", "Sans", 12)) / 2, band - 6 * mm, score)
    c.setFont("Sans", 12)
    c.setFillColor(MUTED)
    c.drawString(left - (value_w + pdfmetrics.stringWidth(" / 75", "Sans", 12)) / 2 + value_w, band - 6 * mm, " / 75")

    grade = row.get("grade") or "—"
    c.setFillColor(colors.HexColor(GRADE_COLORS.get(grade, "#9aa5b1")))
    c.circle(cx, band + 2 * mm, 17 * mm, stroke=0, fill=1)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.2)
    c.circle(cx, band + 2 * mm, 19 * mm, stroke=1, fill=0)
    c.setFillColor(colors.white)
    c.setFont("Sans-Bold", _fit(grade, "Sans-Bold", 34, 26 * mm, 18))
    c.drawCentredString(cx, band - 2 * mm, grade)
    c.setFillColor(MUTED)
    c.setFont("Sans-Bold", 8.5)
    c.drawCentredString(cx, band - 22 * mm, "DARAJA")

    right = cx + 82 * mm
    essay = row.get("essay")
    parts = [("Test qismi", f"{fmt_num(row['test'])} / 75" if row.get("test") is not None else None),
             ("Esse", f"{fmt_num(essay)} / 75" if essay is not None else None)]
    for index, (label, value) in enumerate(parts):
        y = band + 5 * mm - index * 13 * mm
        c.setFont("Sans", 10)
        c.setFillColor(MUTED)
        c.drawString(right - card_w / 2 + 7 * mm, y, label)
        c.setFont("Sans-Bold" if value else "Sans", 13 if value else 10)
        c.setFillColor(INK if value else MUTED)
        c.drawRightString(right + card_w / 2 - 7 * mm, y, value or "baholanmagan")

    c.setFont("Sans", 8)
    c.setFillColor(MUTED)
    c.drawCentredString(cx, 41 * mm, f"Darajalar: {GRADE_SCALE}")

    c.setStrokeColor(INK)
    c.setLineWidth(0.6)
    c.line(32 * mm, 31 * mm, 100 * mm, 31 * mm)
    c.setFont("Sans", 9)
    c.setFillColor(MUTED)
    c.drawString(32 * mm, 26.5 * mm, f"O'qituvchi: {teacher}"[:60])
    c.drawRightString(w - 32 * mm, 31.5 * mm, f"Sana: {_date(row.get('date'))}")
    c.drawRightString(w - 32 * mm, 26.5 * mm, f"№ M-{test['id']}-{row['attempt_id']}")
    c.setFont("Sans", 7.5)
    c.setFillColor(GRID)
    c.drawCentredString(cx, 19.5 * mm, "Rasmiy sertifikat emas — o'quv mashg'uloti (mock imtihon) natijasi. "
                                        "Ball DTM tartibida, Rasch modeli bo'yicha taxminiy hisoblangan.")


# ---------- Sinf jadvali (Natijalar bo'limi) ----------

def class_report_pdf(report: dict) -> bytes:
    """Sinf o'quvchilari × testlar: har katakda umumiy ball va daraja; oxirida o'rtacha. 8 testdan sahifalarga bo'linadi."""
    buffer = io.BytesIO()
    page = landscape(A4)
    cls, tests, rows = report["class"], report["tests"], report["students"]
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=14 * mm, title=f"Sinf natijalari — {cls['name']}")
    width = page[0] - 24 * mm
    banner = Table([[Paragraph(f"{_esc(cls['name'])} — NATIJALAR JADVALI", STYLES["banner"])],
                    [Paragraph(f"{cls['students']} ta o'quvchi  ·  {len(tests)} ta test  ·  {datetime.now():%d.%m.%Y}",
                               STYLES["banner_sub"])]], colWidths=[width])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT),
                                ("TOPPADDING", (0, 0), (-1, 0), 9), ("BOTTOMPADDING", (0, -1), (-1, -1), 9),
                                ("LEFTPADDING", (0, 0), (-1, -1), 12)]))
    head = ParagraphStyle("head", fontName="Sans", fontSize=7.5, leading=9, alignment=TA_CENTER, textColor=colors.white)
    story = [banner, Spacer(1, 5 * mm)]
    chunks = [tests[i:i + 8] for i in range(0, len(tests), 8)] or [[]]
    for index, chunk in enumerate(chunks):
        if index:
            story.append(PageBreak())
        header = ["№", "ID", "F.I.Sh."] + [
            Paragraph(f"<b>{_esc(t['title'][:28])}</b><br/>{_date(t['date'])}", head) for t in chunk] + ["O'rtacha"]
        body, style = [header], _table_style()
        for place, row in enumerate(rows, start=1):
            line = [str(place), f"{row['number']:04d}", Paragraph(_esc(row["name"]), STYLES["cell"])]
            for col, test in enumerate(chunk, start=3):
                cell = row["results"].get(str(test["id"]))
                if not cell:
                    line.append("—")
                    continue
                line.append(f"{fmt_num(cell['score'])}{cell['suffix']}" + (f"\n{cell['grade']}" if cell["grade"] else ""))
                _shade(style, (col, place), cell)
            avg = row["avg"]
            line.append("—" if avg is None else f"{fmt_num(avg)}{row['suffix']}" + (f"\n{row['grade']}" if row["grade"] else ""))
            if avg is not None:
                _shade(style, (len(line) - 1, place), {"score": avg, "grade": row["grade"], "suffix": row["suffix"]})
            body.append(line)
        if len(body) == 1:
            body.append(["", "", "Sinfda hali o'quvchi yo'q"] + [""] * (len(header) - 3))
        fixed = [10 * mm, 14 * mm, 62 * mm]
        rest = width - sum(fixed) - 22 * mm
        cols = fixed + [rest / max(1, len(chunk))] * max(1, len(chunk)) + [22 * mm] if chunk else fixed + [width - sum(fixed)]
        table = Table(body, colWidths=cols, repeatRows=1)
        style.add("ALIGN", (3, 0), (-1, -1), "CENTER")
        table.setStyle(style)
        story.append(table)
    story += [Spacer(1, 4 * mm), Paragraph(
        "Har katakda — o'quvchining shu testdagi <b>umumiy bali (75)</b> va darajasi (DTM tartibida: test qismi Rasch + "
        "esse o'rtachasi; Rasch testni ishlagan barcha qatnashchilar bo'yicha). Oddiy testlarda — foiz. «—» — o'quvchi "
        "bu testda qatnashmagan. <b>O'rtacha</b> — Milliy sertifikat formatidagi testlar bo'yicha. "
        f"Taxminiy natija, rasmiy emas.<br/><b>Darajalar:</b> {GRADE_SCALE}.", STYLES["small"])]
    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return buffer.getvalue()


def _shade(style: TableStyle, cell: tuple, value: dict) -> None:
    if value.get("grade") in GRADE_COLORS:
        style.add("BACKGROUND", cell, cell, colors.HexColor(GRADE_COLORS[value["grade"]]))
        style.add("TEXTCOLOR", cell, cell, colors.white)
        style.add("FONT", cell, cell, "Sans-Bold", 8.5)
    elif value.get("suffix") == "%":
        share = value["score"] or 0
        style.add("BACKGROUND", cell, cell, GOOD if share >= 70 else MID if share >= 40 else BAD)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
