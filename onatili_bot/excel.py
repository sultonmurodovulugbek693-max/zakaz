"""
Natijalar Excel'da: o'qituvchi jurnalga ko'chirishi, saralashi yoki ota-onalarga yuborishi uchun.

1-varaq «Natijalar» — har o'quvchi bir qatorda: ball, daraja va har savol bo'yicha + / −.
2-varaq «Savollar» — har savolni necha foiz yechgani (qiyin savollar yuqorida).
"""

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from pdfs import ranked
from scoring import rasch_results, total_points
from structure import essay_to_75, max_points, percent

HEAD = PatternFill("solid", fgColor="1D7A48")
OK = PatternFill("solid", fgColor="D4EDDA")
NO = PatternFill("solid", fgColor="F8D7DA")
MID = PatternFill("solid", fgColor="FFF3CD")
WHITE_BOLD = Font(bold=True, color="FFFFFF")
CENTER = Alignment(horizontal="center", vertical="center")


def results_xlsx(test: dict, attempts: list[dict]) -> bytes:
    milliy = test["fmt"] == "milliy"
    rasch = rasch_results(test, attempts) if milliy and attempts else {}
    labels = [item["label"] for item in test["items"]]
    wb = Workbook()

    ws = wb.active
    ws.title = "Natijalar"
    if milliy:
        head = ["O'rin", "F.I.Sh.", "Variant", "Test (35)", "Yozma (14)", "Esse (75)", "Esse mezon (24)",
                f"Ball ({max_points(test['items'], test['fmt']):g})", "Umumiy (75)", "Daraja"]
    else:
        head = ["O'rin", "F.I.Sh.", "Variant", "To'g'ri", "Foiz"]
    ws.append(head + labels)
    for place, a in enumerate(ranked(attempts, rasch), 1):
        wrong = set(a["wrong"])
        if milliy:
            score, grade = rasch.get(a["id"], (None, None))
            row = [place, a["student_name"], a["variant"], a["test_correct"], a["written_correct"],
                   essay_to_75(a["essay"]), a["essay"], total_points(a), score, grade]
        else:
            right = a["test_correct"] + a["written_correct"]
            row = [place, a["student_name"], a["variant"], right, percent(right, a["auto_total"])]
        ws.append(row + ["−" if label in wrong else "+" for label in labels])
        for col in range(len(head) + 1, len(head) + len(labels) + 1):
            cell = ws.cell(ws.max_row, col)
            cell.fill, cell.alignment = (NO if cell.value == "−" else OK), CENTER

    for cell in ws[1]:
        cell.fill, cell.font, cell.alignment = HEAD, WHITE_BOLD, CENTER
    ws.column_dimensions["B"].width = 28
    for col in range(3, len(head) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 11
    for col in range(len(head) + 1, len(head) + len(labels) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 4.5
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions

    qs = wb.create_sheet("Savollar")
    qs.append(["Savol", "To'g'ri yechdi", "Qatnashchi", "Foiz", "Holati"])
    stats = []
    for label in labels:
        right = sum(1 for a in attempts if label not in a["wrong"])
        share = percent(right, len(attempts))
        stats.append((label, right, len(attempts), share, "oson" if share >= 70 else "o'rtacha" if share >= 40 else "qiyin"))
    for row in sorted(stats, key=lambda s: s[3]):
        qs.append(row)
        qs.cell(qs.max_row, 5).fill = OK if row[3] >= 70 else MID if row[3] >= 40 else NO
    for cell in qs[1]:
        cell.fill, cell.font, cell.alignment = HEAD, WHITE_BOLD, CENTER
    for col, width in zip("ABCDE", (8, 14, 12, 8, 10)):
        qs.column_dimensions[col].width = width

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
