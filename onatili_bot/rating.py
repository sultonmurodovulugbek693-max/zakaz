"""
Reyting va statistika rasmlari (PNG) + ikkalasi bitta PDF'da.

1-varaq — reyting: o'rin, ism, ball chizig'i, ball, daraja; sertifikat chegarasi (46) chizig'i bilan.
O'quvchilar ko'p bo'lsa — har varaqda ROWS_PER_PAGE ta qator, bir necha varaq.
2-varaq — statistika: o'rtacha / eng yuqori / eng past / sertifikat ulushi, ball taqsimoti diagrammasi,
savollar yechilishi (rang — Rasch qiyinligi, logit) va eng ko'p / eng kam yechilgan savollar.

Milliy testda ball — Rasch bo'yicha umumiy ball (75 ballik) va daraja; oddiy testda — foiz.
Chizish Pillow bilan: 420 pt eni, SCALE barobar kattalikda (Telegram'da tiniq ko'rinsin).
"""

from __future__ import annotations

import io
import math
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from config import FONTS_DIR
from rasch import GRADES, grade_for
from scoring import rasch_breakdown
from structure import fmt_num, percent, text_items

SCALE = 2.5
WIDTH = 420
PAD = 22
ROWS_PER_PAGE = 30
CERT_LINE = GRADES[-1][0]  # 46 — C darajasi, sertifikat chegarasi

# Rasmiy hisobot uslubi: oq qog'oz, to'q ko'k-kulrang matn, bosiq ranglar
BG = (255, 255, 255)
PANEL = (246, 247, 249)       # ko'rsatkich kataklari
STRIPE = (248, 249, 251)      # jadvalning juft qatorlari
BOX = (255, 255, 255)
BOX_LINE = (222, 226, 232)
RULE = (31, 52, 84)           # sarlavha ostidagi chiziq
TEXT = (17, 24, 39)
MUTED = (92, 101, 115)
FAINT = (138, 146, 158)
TRACK = (231, 234, 239)
NAVY = (31, 52, 84)
RED = (160, 44, 44)
GREEN = (22, 101, 66)
AVG = (194, 110, 20)          # o'rtacha ball chizig'i
GRADE_COLORS = {
    "A+": (22, 92, 62), "A": (38, 120, 82), "B+": (33, 86, 140), "B": (66, 115, 165),
    "C+": (150, 112, 25), "C": (170, 98, 35), "NC": (160, 44, 44),
}


def _font(size: float, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONTS_DIR / name), max(1, round(size * SCALE)))


def _mix(color: tuple, alpha: float, base: tuple = BG) -> tuple:
    """Rangni fonga aralashtirish (shaffof ko'rinish)."""
    return tuple(round(c * alpha + b * (1 - alpha)) for c, b in zip(color, base))


def _num(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


class _Sheet:
    """Punktlarda chizadi, rasmda SCALE barobar katta."""

    def __init__(self, height: float) -> None:
        self.image = Image.new("RGB", (round(WIDTH * SCALE), round(height * SCALE)), BG)
        self.draw = ImageDraw.Draw(self.image)

    def rect(self, x: float, y: float, w: float, h: float, fill: tuple, radius: float = 0,
             outline: tuple | None = None) -> None:
        if w <= 0 or h <= 0:
            return
        box = [x * SCALE, y * SCALE, (x + w) * SCALE, (y + h) * SCALE]
        self.draw.rounded_rectangle(box, radius=radius * SCALE, fill=fill, outline=outline,
                                    width=max(1, round(SCALE)) if outline else 0)

    def text(self, x: float, y: float, value: str, size: float, fill: tuple = TEXT, bold: bool = False,
             anchor: str = "ls") -> None:
        self.draw.text((x * SCALE, y * SCALE), value, font=_font(size, bold), fill=fill, anchor=anchor)

    def width(self, value: str, size: float, bold: bool = False) -> float:
        return _font(size, bold).getlength(value) / SCALE

    def fit(self, value: str, size: float, room: float, bold: bool = False) -> str:
        """Joyga sig'maydigan matn «…» bilan qisqartiriladi."""
        if self.width(value, size, bold) <= room:
            return value
        while value and self.width(value + "…", size, bold) > room:
            value = value[:-1]
        return value.rstrip() + "…"

    def line(self, x1: float, y1: float, x2: float, y2: float, fill: tuple, width: float = 1,
             dash: float = 0) -> None:
        if not dash:
            self.draw.line([x1 * SCALE, y1 * SCALE, x2 * SCALE, y2 * SCALE], fill=fill, width=max(1, round(width * SCALE)))
            return
        length = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, int(length // (dash * 2)))
        for k in range(steps + 1):
            a, b = k * 2 * dash / length, min(1, (k * 2 + 1) * dash / length)
            if a >= 1:
                break
            self.line(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a, x1 + (x2 - x1) * b, y1 + (y2 - y1) * b, fill, width)

    def png(self) -> bytes:
        buffer = io.BytesIO()
        self.image.save(buffer, "PNG", optimize=True)
        return buffer.getvalue()


# ---------- Ma'lumot ----------

def rows_of(test: dict, attempts: list[dict]) -> list[dict]:
    """Natijasi chiqqan qatnashchilar, balli bo'yicha kamayish tartibida: {name, score, grade}."""
    if test["fmt"] == "milliy":
        breakdown = rasch_breakdown(test, attempts)
        rows = [{"name": a["student_name"], "score": breakdown[a["id"]]["score"], "grade": breakdown[a["id"]]["grade"],
                 "created": a["created_at"] or ""}
                for a in attempts if a["id"] in breakdown and not breakdown[a["id"]].get("pending")]
    else:
        rows = [{"name": a["student_name"], "score": percent(a["test_correct"] + a["written_correct"], a["auto_total"]),
                 "grade": None, "created": a["created_at"] or ""} for a in attempts]
    rows.sort(key=lambda r: (-r["score"], r["created"]))
    return rows


def item_stats(test: dict, attempts: list[dict]) -> list[dict]:
    """Har savol: {label, share (yechganlar ulushi 0..1), difficulty (Rasch logit), written}.
    Yozma javoblari hali tekshirilmagan titullar yozma savollar hisobiga kirmaydi."""
    try:
        from pdfs import _unchecked_written
        unchecked = _unchecked_written(test, attempts)
    except Exception:
        unchecked = set()
    written = {item["label"] for item in text_items(test["items"])}
    stats = []
    for item in test["items"]:
        label = item["label"]
        pool = [a for a in attempts if not (label in written and a["id"] in unchecked)]
        if not pool:
            continue
        share = sum(1 for a in pool if label not in set(a["wrong"])) / len(pool)
        p = min(0.98, max(0.02, share))
        stats.append({"label": label, "share": share, "difficulty": math.log((1 - p) / p), "written": label in written})
    return stats


def _title(test: dict) -> str:
    return test["title"]


def _date() -> str:
    return f"{datetime.now():%d.%m.%Y}"


# ---------- Umumiy bezak ----------

def _header(sheet: _Sheet, kind: str, title: str, sub: str) -> float:
    """Hujjat sarlavhasi: hujjat turi (kichik bosh harflar), nomi, tafsilot va to'q chiziq. Pastki chetini qaytaradi."""
    room = WIDTH - 2 * PAD
    sheet.text(PAD, PAD + 8, kind, 8.5, MUTED, True)
    sheet.text(PAD, PAD + 27, sheet.fit(title, 16, room, True), 16, TEXT, True)
    sheet.text(PAD, PAD + 42, sheet.fit(sub, 10, room), 10, MUTED)
    sheet.rect(PAD, PAD + 52, room, 1.6, RULE)
    return PAD + 52


def _footer(sheet: _Sheet, height: float, lines: list[str]) -> None:
    y = height - PAD - 13 * (len(lines) - 1)
    sheet.rect(PAD, y - 13, WIDTH - 2 * PAD, 0.6, BOX_LINE)
    for k, line in enumerate(lines):
        sheet.text(PAD, y + k * 13, sheet.fit(line, 8.5, WIDTH - 2 * PAD), 8.5, FAINT)


def _kpis(sheet: _Sheet, top: float, items: list[tuple[str, str, tuple]]) -> float:
    """Ko'rsatkichlar qatori (teng kataklar). Pastki chetini qaytaradi."""
    gap = 6
    w = (WIDTH - 2 * PAD - gap * (len(items) - 1)) / len(items)
    for k, (value, label, color) in enumerate(items):
        x = PAD + k * (w + gap)
        sheet.rect(x, top, w, 44, PANEL, 3)
        sheet.text(x + 9, top + 21, value, 15, color, True)
        sheet.text(x + 9, top + 35, sheet.fit(label, 8.5, w - 14), 8.5, MUTED)
    return top + 44


# ---------- 1-varaq: reyting ----------

ROW_H = 19
LINE = (200, 205, 213)


def _rating_page(test: dict, rows: list[dict], start: int, cut_at: int | None, page: int, pages: int,
                 summary: dict) -> bytes:
    milliy = test["fmt"] == "milliy"
    count = len(rows)
    has_cut = cut_at is not None and start < cut_at < start + count
    footer = (["Ball — Rasch modeli bo'yicha umumiy ball (75 dan). Sertifikat — C (46) va yuqori."] if milliy
              else ["Natija — to'g'ri javoblar foizi."])
    height = PAD + 52 + 12 + 44 + 16 + 18 + count * ROW_H + (20 if has_cut else 0) + 22 + 13 * len(footer) + PAD
    sheet = _Sheet(height)

    sub = f"Kod {test['code']} · {_date()} · {summary['n']} qatnashchi" + (f" · {page}/{pages}-varaq" if pages > 1 else "")
    top = _header(sheet, "NATIJALAR REYTINGI", _title(test), sub) + 12
    if milliy:
        kpis = [(str(summary["n"]), "qatnashchi", TEXT), (str(summary["certified"]), "sertifikat oldi", GREEN),
                (str(summary["nc"]), "sertifikatsiz (NC)", RED), (_num(summary["avg"]), "o'rtacha ball", TEXT)]
    else:
        kpis = [(str(summary["n"]), "qatnashchi", TEXT), (str(summary["good"]), "80% va yuqori", GREEN),
                (f"{_num(summary['avg'])}%", "o'rtacha natija", TEXT)]
    top = _kpis(sheet, top, kpis) + 16

    # Ustunlar: № | F.I.Sh. | natija chizig'i | ball | daraja
    right = WIDTH - PAD - 4
    grade_x = right - 26 if milliy else right
    value_right = (grade_x - 10) if milliy else right
    track_w = 84
    track_x = value_right - 38 - track_w
    head = [(PAD + 4, "№", "ls"), (PAD + 28, "F.I.SH.", "ls"), (track_x, "NATIJA", "ls"),
            (value_right, "BALL" if milliy else "FOIZ", "rs")] + ([(grade_x + 13, "DARAJA", "ms")] if milliy else [])
    for x, label, anchor in head:
        sheet.text(x, top + 9, label, 7.5, MUTED, True, anchor)
    top += 14
    sheet.rect(PAD, top, WIDTH - 2 * PAD, 0.8, LINE)
    top += 4

    top_max = 75 if milliy else 100
    for index, row in enumerate(rows):
        place = start + index + 1
        if has_cut and place == cut_at + 1:
            y = top + 10
            label = f"SERTIFIKAT CHEGARASI — {CERT_LINE} BALL"
            half = sheet.width(label, 7.5, True) / 2 + 8
            sheet.line(PAD, y, WIDTH / 2 - half, y, RED, 0.8, 2)
            sheet.line(WIDTH / 2 + half, y, WIDTH - PAD, y, RED, 0.8, 2)
            sheet.text(WIDTH / 2, y + 2.7, label, 7.5, RED, True, "ms")
            top += 20
        if milliy:
            color = GRADE_COLORS[row["grade"]]
        else:
            color = GREEN if row["score"] >= 80 else (GRADE_COLORS["B"] if row["score"] >= 60 else
                                                      (GRADE_COLORS["C+"] if row["score"] >= 40 else RED))
        if index % 2:
            sheet.rect(PAD, top, WIDTH - 2 * PAD, ROW_H, STRIPE)
        mid = top + ROW_H / 2
        best = place <= 3
        sheet.text(PAD + 4, mid, str(place), 10, TEXT if best else MUTED, best, "lm")
        sheet.text(PAD + 28, mid, sheet.fit(row["name"], 10, track_x - PAD - 36, best), 10, TEXT, best, "lm")
        sheet.rect(track_x, mid - 2, track_w, 4, TRACK, 1)
        sheet.rect(track_x, mid - 2, max(2, track_w * min(1, row["score"] / top_max)), 4, color, 1)
        sheet.text(value_right, mid, (_num(row["score"]) if milliy else f"{fmt_num(row['score'])}%"), 10, TEXT, True, "rm")
        if milliy:
            sheet.rect(grade_x, mid - 7, 26, 14, BG, 2, _mix(color, 0.55))
            sheet.text(grade_x + 13, mid, row["grade"], 8.5, color, True, "mm")
        top += ROW_H
    sheet.rect(PAD, top, WIDTH - 2 * PAD, 0.8, LINE)
    _footer(sheet, height, footer)
    return sheet.png()


def rating_pages(test: dict, attempts: list[dict]) -> list[bytes]:
    rows = rows_of(test, attempts)
    if not rows:
        return []
    scores = [r["score"] for r in rows]
    milliy = test["fmt"] == "milliy"
    certified = sum(1 for r in rows if r["grade"] and r["grade"] != "NC")
    summary = {"n": len(rows), "avg": sum(scores) / len(scores), "certified": certified, "nc": len(rows) - certified,
               "good": sum(1 for s in scores if s >= 80)}
    cut_at = certified if milliy and 0 < certified < len(rows) else None
    pages = math.ceil(len(rows) / ROWS_PER_PAGE)
    return [_rating_page(test, rows[k * ROWS_PER_PAGE:(k + 1) * ROWS_PER_PAGE], k * ROWS_PER_PAGE, cut_at, k + 1, pages, summary)
            for k in range(pages)]


# ---------- 2-varaq: statistika ----------

# Savol qiyinligi (Rasch logit) — bitta ko'k rangning to'rt tusi: oson — och, juda qiyin — to'q
DIFFICULTY = [(-1, (176, 196, 222), "oson"), (0, (115, 147, 190), "o'rtacha"),
              (1, (58, 93, 145), "qiyin"), (99, (24, 42, 72), "juda qiyin")]


def _difficulty_color(b: float) -> tuple:
    return next(color for edge, color, _ in DIFFICULTY if b < edge)


def _section(sheet: _Sheet, top: float, height: float, title: str, note: str) -> None:
    sheet.rect(PAD, top, WIDTH - 2 * PAD, height, BOX, 3, BOX_LINE)
    sheet.text(PAD + 12, top + 18, title, 10.5, TEXT, True)
    sheet.text(WIDTH - PAD - 12, top + 18, note, 8.5, MUTED, anchor="rs")


def stats_page(test: dict, attempts: list[dict]) -> bytes | None:
    rows = rows_of(test, attempts)
    if not rows:
        return None
    milliy = test["fmt"] == "milliy"
    scores = sorted((r["score"] for r in rows), reverse=True)
    n = len(scores)
    avg = sum(scores) / n
    median = (scores[(n - 1) // 2] + scores[n // 2]) / 2
    certified = sum(1 for r in rows if r["grade"] and r["grade"] != "NC")
    items = item_stats(test, attempts)
    show_items = len(items) >= 2

    footer = (["Rasch modeli: kam yechilgan savol qiyinroq — uni yechgan o'quvchi ko'proq ball oladi.",
               "Umumiy ball: T = 50 + 10·Z, eng yuqorisi 75. Sertifikat — C (46 ball) va undan yuqori."] if milliy
              else ["Savol qiyinligi — uni yecha olmaganlar ulushidan (Rasch logit)."])
    hist_h, items_h, lists_h = 170, 140, 16 + 5 * 16 + 8
    height = (PAD + 52 + 12 + 44 + 14 + hist_h + 12 + ((items_h + lists_h + 12) if show_items else 0)
              + 10 + 13 * len(footer) + PAD)
    sheet = _Sheet(height)

    method = "Rasch modeli bo'yicha" if milliy else "to'g'ri javoblar foizi"
    top = _header(sheet, "STATISTIK TAHLIL", _title(test), f"Kod {test['code']} · {_date()} · {n} qatnashchi · {method}") + 12
    suffix = "" if milliy else "%"
    last = (f"{round(certified / n * 100)}%", f"sertifikat · {certified}/{n}") if milliy else (_num(median) + "%", "median")
    top = _kpis(sheet, top, [(_num(avg) + suffix, "o'rtacha ball", TEXT), (_num(scores[0]) + suffix, "eng yuqori", GREEN),
                             (_num(scores[-1]) + suffix, "eng past", RED), (last[0], last[1], TEXT)]) + 14

    # Ball taqsimoti
    _section(sheet, top, hist_h, "Umumiy ball taqsimoti",
             "75 ballik shkala, 5 ballik oraliqlar" if milliy else "100 foizlik shkala")
    hi, step = (75, 5) if milliy else (100, 10)
    lo = max(0, min(hi - 4 * step, math.floor(scores[-1] / step) * step))
    bins = []
    edge = lo
    while edge < hi:
        upper = edge + step
        bins.append((edge, sum(1 for s in scores if edge <= s < upper or (upper >= hi and s >= hi))))
        edge = upper
    peak = max(c for _, c in bins) or 1
    y_max = max(2, math.ceil(peak / 2) * 2)
    cx0, cx1, cy0, cy1 = PAD + 34, WIDTH - PAD - 14, top + 34, top + hist_h - 38
    x_of = lambda s: cx0 + (s - lo) / (hi - lo) * (cx1 - cx0)
    y_of = lambda c: cy1 - c / y_max * (cy1 - cy0)
    grid_step = max(1, y_max // 4)
    for c in range(0, y_max + 1, grid_step):
        sheet.line(cx0, y_of(c), cx1, y_of(c), TRACK, 0.6)
        sheet.text(cx0 - 6, y_of(c) + 3, str(c), 8, FAINT, anchor="rs")
    for start, count in bins:
        if not count:
            continue
        x0, x1 = x_of(start) + 1.5, x_of(start + step) - 1.5
        below = milliy and start + step <= CERT_LINE  # butun oraliq sertifikat chegarasidan past
        sheet.rect(x0, y_of(count), x1 - x0, cy1 - y_of(count), (201, 138, 138) if below else NAVY, 1)
        sheet.text((x0 + x1) / 2, y_of(count) - 3.5, str(count), 8.5, TEXT, True, "ms")
    sheet.line(cx0, cy1, cx1, cy1, MUTED, 0.8)
    for start, _ in bins + [(hi, 0)]:
        sheet.text(x_of(start), cy1 + 12, str(start), 8, MUTED, anchor="ms")
    avg_x = x_of(min(max(avg, lo), hi))
    sheet.line(avg_x, cy0 - 6, avg_x, cy1, AVG, 1.1, 2)
    legend_y = top + hist_h - 9
    sheet.line(PAD + 12, legend_y - 3, PAD + 24, legend_y - 3, AVG, 1.1, 2)
    sheet.text(PAD + 29, legend_y, f"o'rtacha {_num(avg)}", 8.5, MUTED)
    x = PAD + 29 + sheet.width(f"o'rtacha {_num(avg)}", 8.5) + 14
    if milliy and lo <= CERT_LINE <= hi:
        sheet.line(x_of(CERT_LINE), cy0 - 6, x_of(CERT_LINE), cy1, RED, 1.1, 3)
        sheet.line(x, legend_y - 3, x + 12, legend_y - 3, RED, 1.1, 3)
        sheet.text(x + 17, legend_y, f"sertifikat chegarasi {CERT_LINE}", 8.5, MUTED)
        x += 17 + sheet.width(f"sertifikat chegarasi {CERT_LINE}", 8.5) + 14
    sheet.text(x, legend_y, f"median {_num(median)}", 8.5, MUTED)
    top += hist_h + 12

    # Savollar yechilishi
    if show_items:
        _section(sheet, top, items_h + lists_h, "Savollar bo'yicha yechilish", "ustun tusi — Rasch qiyinligi")
        qx0, qx1, qy0, qy1 = PAD + 34, WIDTH - PAD - 10, top + 32, top + 100
        bw = (qx1 - qx0) / len(items)
        for value in (0, 50, 100):
            y = qy1 - value / 100 * (qy1 - qy0)
            sheet.line(qx0, y, qx1, y, TRACK, 0.6)
            sheet.text(qx0 - 5, y + 3, f"{value}%", 8, FAINT, anchor="rs")
        for k, item in enumerate(items):
            h = item["share"] * (qy1 - qy0)
            sheet.rect(qx0 + k * bw + bw * 0.14, qy1 - h, bw * 0.72, h, _difficulty_color(item["difficulty"]))
        sheet.line(qx0, qy1, qx1, qy1, MUTED, 0.8)
        ticks = {0, len(items) - 1} | {k for k in range(len(items)) if (k + 1) % 10 == 0}
        first_written = next((k for k, item in enumerate(items) if item["written"]), None)
        if first_written:
            ticks.add(first_written)
            sx = qx0 + first_written * bw
            sheet.line(sx, qy0 - 4, sx, qy1, FAINT, 0.7, 2)
        drawn = []
        for k in sorted(ticks):
            x = qx0 + (k + 0.5) * bw
            if any(abs(x - d) < 14 for d in drawn):
                continue
            drawn.append(x)
            sheet.text(x, qy1 + 11, items[k]["label"], 7.5, MUTED, anchor="ms")
        lx = PAD + 12
        for _, color, label in DIFFICULTY:
            sheet.rect(lx, top + 118, 8, 8, color)
            sheet.text(lx + 12, top + 125, label, 8.5, MUTED)
            lx += 12 + sheet.width(label, 8.5) + 12
        if first_written:
            sheet.text(WIDTH - PAD - 12, top + 125, f"yozma: {items[first_written]['label']}–{items[-1]['label']}",
                       8.5, MUTED, anchor="rs")

        ordered = sorted(items, key=lambda i: -i["share"])
        easy, hard = ordered[:5], list(reversed(ordered[-5:]))
        col_w = (WIDTH - 2 * PAD - 24 - 16) / 2
        sheet.rect(PAD + 12, top + items_h - 4, WIDTH - 2 * PAD - 24, 0.6, BOX_LINE)
        for c, (title, color, group) in enumerate((("ENG KO'P YECHILGAN", NAVY, easy), ("ENG KAM YECHILGAN", RED, hard))):
            x = PAD + 12 + c * (col_w + 16)
            y = top + items_h + 10
            sheet.text(x, y, title, 7.5, color, True)
            for k, item in enumerate(group):
                mid = y + 12 + k * 16
                sheet.text(x, mid, f"{item['label']}-savol", 9, TEXT, anchor="lm")
                bar_x, bar_w = x + 52, col_w - 52 - 34
                sheet.rect(bar_x, mid - 2, bar_w, 4, TRACK, 1)
                sheet.rect(bar_x, mid - 2, max(2, bar_w * item["share"]), 4, color, 1)
                sheet.text(x + col_w, mid, f"{round(item['share'] * 100)}%", 9, TEXT, True, "rm")
        top += items_h + lists_h + 12

    _footer(sheet, height, footer)
    return sheet.png()


# ---------- Hammasi ----------

def rating_images(test: dict, attempts: list[dict]) -> list[bytes]:
    """Reyting varaqlari (1 yoki bir nechta) + statistika varag'i — PNG'lar."""
    pages = rating_pages(test, attempts)
    stats = stats_page(test, attempts)
    return pages + ([stats] if stats else [])


def rating_pdf(images: list[bytes]) -> bytes:
    """Rasmlar bitta PDF'da — har biri alohida sahifa."""
    frames = [Image.open(io.BytesIO(data)).convert("RGB") for data in images]
    buffer = io.BytesIO()
    frames[0].save(buffer, "PDF", save_all=True, append_images=frames[1:], resolution=72 * SCALE)
    return buffer.getvalue()
