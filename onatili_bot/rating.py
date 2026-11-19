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

BG = (17, 20, 24)
PANEL = (27, 32, 40)
BOX = (22, 26, 32)
BOX_LINE = (32, 38, 47)
TEXT = (232, 235, 240)
MUTED = (139, 147, 161)
FAINT = (107, 115, 129)
TRACK = (35, 41, 52)
GOLD = (250, 204, 21)
RED = (248, 113, 113)
GREEN = (52, 211, 153)
GRADE_COLORS = {
    "A+": (52, 211, 153), "A": (74, 222, 128), "B+": (56, 189, 248), "B": (96, 165, 250),
    "C+": (250, 204, 21), "C": (251, 146, 60), "NC": (248, 113, 113),
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


# ---------- 1-varaq: reyting ----------

def _kpi(sheet: _Sheet, right: float, top: float, items: list[tuple[str, str, tuple]]) -> float:
    """O'ngdan chapga kichik ko'rsatkich kataklari; chap chetini qaytaradi."""
    x = right
    for value, label, color in reversed(items):
        w = max(sheet.width(value, 16, True), sheet.width(label, 10)) + 20
        x -= w
        sheet.rect(x, top, w, 38, PANEL, 10)
        sheet.text(x + w / 2, top + 20, value, 16, color, True, "ms")
        sheet.text(x + w / 2, top + 32, label, 10, MUTED, anchor="ms")
        x -= 6
    return x


def _rating_page(test: dict, rows: list[dict], start: int, cut_at: int | None, page: int, pages: int,
                 summary: dict) -> bytes:
    milliy = test["fmt"] == "milliy"
    count = len(rows)
    has_cut = cut_at is not None and start < cut_at < start + count
    height = PAD + 44 + 16 + count * 23 + (22 if has_cut else 0) + 30 + PAD
    sheet = _Sheet(height)
    sheet.rect(0, 0, WIDTH, height, BG)

    # Sarlavha va ko'rsatkichlar
    if milliy:
        kpis = [(str(summary["certified"]), "sert.", GREEN), (str(summary["nc"]), "NC", RED), (_num(summary["avg"]), "o'rt.", TEXT)]
    else:
        kpis = [(str(summary["good"]), "80%+", GREEN), (f"{_num(summary['avg'])}%", "o'rt.", TEXT)]
    left_of_kpi = _kpi(sheet, WIDTH - PAD, PAD, kpis)
    room = left_of_kpi - PAD - 8
    sheet.text(PAD, PAD + 17, sheet.fit(_title(test), 17, room, True), 17, TEXT, True)
    sub = f"Reyting · kod {test['code']} · {_date()}" + (f" · {page}/{pages}" if pages > 1 else "")
    sheet.text(PAD, PAD + 34, sheet.fit(sub, 11.5, room), 11.5, MUTED)

    # Ustunlar: o'rin | ism | ball chizig'i | ball | daraja
    right = WIDTH - PAD - 6
    pill_x = right - 30 if milliy else right
    value_right = (pill_x - 8) if milliy else right
    track_x = value_right - 40 - 92
    top = PAD + 60
    top_max = 75 if milliy else 100
    for index, row in enumerate(rows):
        place = start + index + 1
        if has_cut and place == cut_at + 1:
            y = top + 11
            label = f"SERTIFIKAT CHEGARASI · {CERT_LINE} BALL"
            half = sheet.width(label, 9.5) / 2 + 8
            sheet.line(PAD, y, WIDTH / 2 - half, y, (127, 29, 29), 1, 2.5)
            sheet.line(WIDTH / 2 + half, y, WIDTH - PAD, y, (127, 29, 29), 1, 2.5)
            sheet.text(WIDTH / 2, y + 3.5, label, 9.5, RED, anchor="ms")
            top += 22
        if milliy:
            color = GRADE_COLORS[row["grade"]]
        else:
            color = GREEN if row["score"] >= 80 else (GRADE_COLORS["B"] if row["score"] >= 60 else
                                                      (GRADE_COLORS["C+"] if row["score"] >= 40 else RED))
        if place <= 3:  # birinchi uchtalik — oltin tus
            for k in range(60):
                alpha = 0.13 * (1 - k / 60)
                x0 = PAD + (WIDTH - 2 * PAD) * k / 60
                sheet.rect(x0, top, (WIDTH - 2 * PAD) / 60 + 0.5, 20, _mix(GOLD, alpha))
        mid = top + 10
        sheet.text(PAD + 26, mid, str(place), 11.5, FAINT, anchor="rm")
        sheet.text(PAD + 34, mid, sheet.fit(row["name"], 11.5, track_x - PAD - 42), 11.5, TEXT, anchor="lm")
        sheet.rect(track_x, mid - 2.5, 92, 5, TRACK, 2.5)
        sheet.rect(track_x, mid - 2.5, max(3, 92 * min(1, row["score"] / top_max)), 5, color, 2.5)
        sheet.text(value_right, mid, (_num(row["score"]) if milliy else f"{fmt_num(row['score'])}%"), 11.5, TEXT, True, "rm")
        if milliy:
            sheet.rect(pill_x, mid - 8, 30, 16, _mix(color, 0.16), 5)
            sheet.text(pill_x + 15, mid, row["grade"], 10, color, True, "mm")
        top += 23

    note = ("Rasch modeli · umumiy ball 75 dan · sertifikat C (46) va yuqori" if milliy
            else "Natija — to'g'ri javoblar foizi")
    sheet.text(WIDTH / 2, height - PAD + 2, note, 9.5, FAINT, anchor="ms")
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

def _difficulty_color(b: float) -> tuple:
    return GREEN if b < -1 else (163, 230, 53) if b < 0 else GOLD if b < 1 else RED


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

    hist_h, items_h, lists_h = 176, 150, 4 + 8 + 5 * 17 + 10
    height = PAD + 44 + 60 + hist_h + 12 + ((items_h + lists_h + 12) if show_items else 0) + 44 + PAD
    sheet = _Sheet(height)

    sheet.text(PAD, PAD + 17, sheet.fit(_title(test), 17, WIDTH - 2 * PAD, True), 17, TEXT, True)
    method = "Rasch modeli bo'yicha" if milliy else "to'g'ri javoblar foizi"
    sheet.text(PAD, PAD + 34, sheet.fit(f"Statistika · kod {test['code']} · {_date()} · {n} qatnashchi · {method}", 11.5,
                                        WIDTH - 2 * PAD), 11.5, MUTED)

    # Ko'rsatkichlar
    suffix = "" if milliy else "%"
    last = (f"{round(certified / n * 100)}%", f"sertifikat {certified}/{n}") if milliy else (_num(median) + "%", "median")
    kpis = [(_num(avg) + suffix, "o'rtacha ball", TEXT), (_num(scores[0]) + suffix, "eng yuqori", GREEN),
            (_num(scores[-1]) + suffix, "eng past", RED), (last[0], last[1], TEXT)]
    top = PAD + 48
    w = (WIDTH - 2 * PAD - 3 * 6) / 4
    for k, (value, label, color) in enumerate(kpis):
        x = PAD + k * (w + 6)
        sheet.rect(x, top, w, 50, PANEL, 12)
        sheet.text(x + 9, top + 24, value, 18, color, True)
        sheet.text(x + 9, top + 40, sheet.fit(label, 9.5, w - 14), 9.5, MUTED)
    top += 62

    # Ball taqsimoti
    sheet.rect(PAD, top, WIDTH - 2 * PAD, hist_h, BOX, 14, BOX_LINE)
    sheet.text(PAD + 12, top + 20, "Umumiy ball taqsimoti", 12, TEXT, True)
    sheet.text(WIDTH - PAD - 12, top + 20, "75 ballik shkala" if milliy else "100 foizlik shkala", 11, MUTED, anchor="rs")
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
    cx0, cx1, cy0, cy1 = PAD + 32, WIDTH - PAD - 14, top + 38, top + hist_h - 40
    x_of = lambda s: cx0 + (s - lo) / (hi - lo) * (cx1 - cx0)
    y_of = lambda c: cy1 - c / y_max * (cy1 - cy0)
    grid_step = max(1, y_max // 4)
    for c in range(0, y_max + 1, grid_step):
        sheet.line(cx0, y_of(c), cx1, y_of(c), TRACK, 0.8)
        sheet.text(cx0 - 6, y_of(c) + 3.5, str(c), 9.5, FAINT, anchor="rs")
    for start, count in bins:
        if not count:
            continue
        x0, x1 = x_of(start) + 2, x_of(start + step) - 2
        color = GRADE_COLORS[grade_for(start + step / 2)] if milliy else (
            GREEN if start >= 80 else GRADE_COLORS["B"] if start >= 60 else GRADE_COLORS["C+"] if start >= 40 else RED)
        sheet.rect(x0, y_of(count), x1 - x0, cy1 - y_of(count), _mix(color, 0.9), 4)
        sheet.text((x0 + x1) / 2, y_of(count) - 4, str(count), 10.5, TEXT, True, "ms")
    for start, _ in bins + [(hi, 0)]:
        sheet.text(x_of(start), cy1 + 14, str(start), 9.5, MUTED, anchor="ms")
    sheet.line(x_of(min(max(avg, lo), hi)), cy0 - 6, x_of(min(max(avg, lo), hi)), cy1, TEXT, 1.2, 2)
    legend_y = top + hist_h - 10
    sheet.line(PAD + 12, legend_y - 3.5, PAD + 24, legend_y - 3.5, TEXT, 1.2, 2)
    sheet.text(PAD + 29, legend_y, f"o'rtacha {_num(avg)}", 10, MUTED)
    x = PAD + 29 + sheet.width(f"o'rtacha {_num(avg)}", 10) + 14
    if milliy and lo <= CERT_LINE <= hi:
        sheet.line(x_of(CERT_LINE), cy0 - 6, x_of(CERT_LINE), cy1, RED, 1.2, 3)
        sheet.line(x, legend_y - 3.5, x + 12, legend_y - 3.5, RED, 1.2, 3)
        sheet.text(x + 17, legend_y, f"sertifikat chegarasi {CERT_LINE}", 10, MUTED)
        x += 17 + sheet.width(f"sertifikat chegarasi {CERT_LINE}", 10) + 14
    sheet.text(x, legend_y, f"median {_num(median)}", 10, MUTED)
    top += hist_h + 12

    # Savollar yechilishi
    if show_items:
        box_h = items_h + lists_h
        sheet.rect(PAD, top, WIDTH - 2 * PAD, box_h, BOX, 14, BOX_LINE)
        sheet.text(PAD + 12, top + 20, "Savollar yechilishi", 12, TEXT, True)
        sheet.text(WIDTH - PAD - 12, top + 20, "rang — Rasch qiyinligi", 11, MUTED, anchor="rs")
        qx0, qx1, qy0, qy1 = PAD + 34, WIDTH - PAD - 10, top + 34, top + 108
        bw = (qx1 - qx0) / len(items)
        for value in (0, 50, 100):
            y = qy1 - value / 100 * (qy1 - qy0)
            sheet.line(qx0, y, qx1, y, TRACK, 0.8)
            sheet.text(qx0 - 5, y + 3.5, f"{value}%", 9, FAINT, anchor="rs")
        for k, item in enumerate(items):
            h = item["share"] * (qy1 - qy0)
            sheet.rect(qx0 + k * bw + bw * 0.12, qy1 - h, bw * 0.76, h, _difficulty_color(item["difficulty"]), min(1.5, bw / 4))
        ticks = {0, len(items) - 1} | {k for k in range(len(items)) if (k + 1) % 10 == 0}
        first_written = next((k for k, item in enumerate(items) if item["written"]), None)
        if first_written:
            ticks.add(first_written)
            sx = qx0 + first_written * bw
            sheet.line(sx, qy0 - 4, sx, qy1, (75, 85, 99), 0.8, 2)
        drawn = []
        for k in sorted(ticks):
            x = qx0 + (k + 0.5) * bw
            if any(abs(x - d) < 14 for d in drawn):
                continue
            drawn.append(x)
            sheet.text(x, qy1 + 12, items[k]["label"], 8.5, MUTED, anchor="ms")
        lx = PAD + 12
        for label, color in (("oson", GREEN), ("o'rtacha", (163, 230, 53)), ("qiyin", GOLD), ("juda qiyin", RED)):
            sheet.rect(lx, top + 128, 9, 9, color, 2)
            sheet.text(lx + 13, top + 136, label, 10, MUTED)
            lx += 13 + sheet.width(label, 10) + 14
        if first_written:
            sheet.text(WIDTH - PAD - 12, top + 136, f"yozma: {items[first_written]['label']}–{items[-1]['label']}", 10, MUTED, anchor="rs")

        ordered = sorted(items, key=lambda i: -i["share"])
        easy, hard = ordered[:5], list(reversed(ordered[-5:]))
        col_w = (WIDTH - 2 * PAD - 24 - 12) / 2
        for c, (title, color, group) in enumerate((("ENG KO'P YECHILGAN", GREEN, easy), ("ENG KAM YECHILGAN", RED, hard))):
            x = PAD + 12 + c * (col_w + 12)
            y = top + items_h + 4
            sheet.text(x, y, title, 9.5, color, True)
            for k, item in enumerate(group):
                yy = y + 8 + k * 17
                sheet.text(x, yy + 8, f"{item['label']}-s.", 10.5, TEXT, anchor="lm")
                bar_x, bar_w = x + 38, col_w - 38 - 38
                sheet.rect(bar_x, yy + 5.5, bar_w, 5, TRACK, 2.5)
                sheet.rect(bar_x, yy + 5.5, max(2, bar_w * item["share"]), 5, color, 2.5)
                sheet.text(x + col_w, yy + 8, f"{round(item['share'] * 100)}%", 10.5, TEXT, True, "rm")
        top += box_h + 12

    notes = (["Rasch modelida kam yechilgan savol qiyinroq hisoblanadi —",
              "uni yechgan o'quvchi ko'proq ball oladi.",
              "Umumiy ball: T = 50 + 10·Z, eng yuqorisi 75 · sertifikat C (46) va yuqori."] if milliy
             else ["Savol qiyinligi — uni yecha olmaganlar ulushidan (Rasch logit)."])
    for k, note in enumerate(notes):
        sheet.text(PAD, top + 12 + k * 13, sheet.fit(note, 9.5, WIDTH - 2 * PAD), 9.5, FAINT)
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
