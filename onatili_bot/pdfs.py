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
        "Esse hamma qatnashchida baholangani uchun Rasch'ga qo'shildi."
        if attempts and all(a["essay"] is not None for a in attempts)
        else "Esse hamma qatnashchida baholanmagan — Rasch bali hozircha essesiz hisoblandi."
    )
    return (
        "<b>Ball</b> — UZBMB rasmiy namunasidagi savol ballari bo'yicha (1,1 / 1,7 / 2,5; esse 24; jami 100). "
        "<b>Rasch bali</b> — Milliy sertifikat uslubida (T-shkala, o'rtacha 50, eng yuqori 75), shu testni ishlagan "
        "qatnashchilar javoblaridan hisoblanadi: qatnashchilar ko'p bo'lsa aniqroq. Taxminiy natija, rasmiy emas. "
        f"{essay_note}<br/><b>Darajalar:</b> A+ — 70 va yuqori · A — 65–69,9 · B+ — 60–64,9 · B — 55–59,9 · "
        "C+ — 50–54,9 · C — 46–49,9 · NC — 46 dan past (sertifikat berilmaydi)."
    )


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
