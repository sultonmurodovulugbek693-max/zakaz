"""
«📊 Natijalar» bo'limi: sinf bo'yicha barcha testlar va test bo'yicha natijalar (PDF va mock sertifikat uchun ham).

Titulli (oflayn) testda o'quvchi natijasi tg_id = db.offline_tg_id(test_id, ID) bilan saqlanadi. Sinf
o'quvchisining doimiy ID'si ham shu — shuning uchun sinf qaysi testlarda qatnashgani shundan topiladi.
Onlayn (kod bilan) testlar sinfga bog'lanmaydi — ular «Testlar» ro'yxatida ko'rinadi.

Ball: Milliy sertifikat formatida — umumiy ball (75, Rasch: test + esse o'rtachasi) va daraja; oddiy testda — foiz.
Rasch bali har doim testni ishlagan HAMMA qatnashchilar bo'yicha hisoblanadi (faqat sinf ichida emas),
shuning uchun sinf ro'yxatidagi ball test sahifasidagi bilan bir xil.
"""

from rasch import grade_for
from scoring import rasch_breakdown
from structure import percent

import db

MAX_TESTS = 200  # Natijalar bo'limida ko'rinadigan oxirgi testlar


def offline_number(test_id: int, tg_id: int | None) -> int | None:
    """Titulli testdagi natija → o'quvchining doimiy ID'si (db.offline_tg_id ning teskarisi). Onlayn bo'lsa — None."""
    if tg_id is None or tg_id >= 0:
        return None
    number = -tg_id - test_id * 100000
    return number if 0 <= number < 100000 else None


def scores(test: dict, attempts: list[dict]) -> dict[int, dict]:
    """{attempt_id: {"score", "grade", "test", "essay", "suffix"}} — Milliy: umumiy ball (75); oddiy: foiz."""
    if not attempts:
        return {}
    if test["fmt"] == "milliy":
        return {key: {**value, "suffix": ""} for key, value in rasch_breakdown(test, attempts).items()}
    return {a["id"]: {"score": percent(a["test_correct"] + a["written_correct"], a["auto_total"]),
                      "grade": None, "test": None, "essay": None, "suffix": "%"} for a in attempts}


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def summary(rows: list[dict]) -> dict:
    """Qatnashchilar, o'rtacha, eng yuqori, C va yuqori daraja, darajalar taqsimoti."""
    values = [r["score"] for r in rows if r["score"] is not None]
    grades: dict[str, int] = {}
    for row in rows:
        if row.get("grade"):
            grades[row["grade"]] = grades.get(row["grade"], 0) + 1
    return {"n": len(rows), "avg": _mean(values), "max": max(values) if values else None,
            "certified": sum(n for g, n in grades.items() if g != "NC"), "grades": grades}


def class_map(owner_id: int) -> dict[int, dict]:
    """{doimiy ID: o'quvchi (sinf nomi bilan)} — o'qituvchining barcha sinflari bo'yicha."""
    return {s["number"]: s for s in db.owner_class_students(owner_id)}


def _row(test: dict, attempt: dict, score: dict, number: int | None, pupil: dict | None) -> dict:
    return {
        "attempt_id": attempt["id"], "name": attempt["student_name"], "number": number,
        "class_id": pupil["class_id"] if pupil else None, "class_name": pupil["class_name"] if pupil else None,
        "variant": attempt["variant"], "score": score["score"], "grade": score["grade"], "suffix": score["suffix"],
        "test": score["test"], "essay": score["essay"],
        "test_correct": attempt["test_correct"], "written_correct": attempt["written_correct"],
        "auto_total": attempt["auto_total"], "date": (attempt.get("created_at") or "")[:10],
    }


def test_results(test: dict, class_id: int | None = None) -> dict:
    """Bitta test natijalari (o'rin bo'yicha). class_id berilsa — faqat shu sinf o'quvchilari."""
    attempts = db.list_attempts(test["id"])
    marks = scores(test, attempts)
    pupils = class_map(test["owner_id"])
    rows = []
    for attempt in attempts:
        number = offline_number(test["id"], attempt.get("tg_id"))
        pupil = pupils.get(number) if number is not None else None
        if class_id is not None and (not pupil or pupil["class_id"] != class_id):
            continue
        rows.append(_row(test, attempt, marks[attempt["id"]], number, pupil))
    rows.sort(key=lambda r: (-(r["score"] or 0), r["name"]))
    return {"rows": rows, **summary(rows)}


def overview(owner_id: int) -> dict:
    """Natijalar bo'limining bosh sahifasi: sinflar (nechta testda qatnashgan) va natijasi bor testlar."""
    pupils = class_map(owner_id)
    tests_of_class: dict[int, set] = {}
    tests = []
    for test in db.list_tests(owner_id, MAX_TESTS):
        attempts = db.list_attempts(test["id"])
        if not attempts:
            continue
        marks = scores(test, attempts)
        for attempt in attempts:
            pupil = pupils.get(offline_number(test["id"], attempt.get("tg_id")))
            if pupil:
                tests_of_class.setdefault(pupil["class_id"], set()).add(test["id"])
        tests.append({"id": test["id"], "title": test["title"], "code": test["code"], "fmt": test["fmt"],
                      "mode": test.get("mode") or "online", "date": (test.get("created_at") or "")[:10],
                      "n": len(attempts), "avg": _mean([m["score"] for m in marks.values()]),
                      "suffix": "" if test["fmt"] == "milliy" else "%"})
    classes = [{"id": c["id"], "name": c["name"], "students": c["students"],
                "tests": len(tests_of_class.get(c["id"], ()))} for c in db.list_classes(owner_id)]
    return {"classes": classes, "tests": tests}


def class_report(owner_id: int, cls: dict) -> dict:
    """Sinf jadvali: o'quvchilar × testlar (eskidan yangiga). Har o'quvchiga o'rtacha ball va daraja."""
    students = sorted(db.list_class_students(cls["id"]), key=lambda s: s["number"])
    by_number = {s["number"]: s for s in students}
    cells: dict[int, dict] = {s["id"]: {} for s in students}
    tests = []
    for test in reversed(db.list_tests(owner_id, MAX_TESTS)):
        attempts = db.list_attempts(test["id"])
        mine = []
        for attempt in attempts:
            number = offline_number(test["id"], attempt.get("tg_id"))
            if number in by_number:
                mine.append((attempt, by_number[number]))
        if not mine:
            continue
        marks = scores(test, attempts)  # Rasch — butun test qatnashchilari bo'yicha
        for attempt, pupil in mine:
            cells[pupil["id"]][test["id"]] = {"attempt_id": attempt["id"], **marks[attempt["id"]]}
        tests.append({"id": test["id"], "title": test["title"], "code": test["code"], "fmt": test["fmt"],
                      "date": (test.get("created_at") or "")[:10], "n": len(mine),
                      "avg": _mean([marks[a["id"]]["score"] for a, _ in mine]),
                      "suffix": "" if test["fmt"] == "milliy" else "%"})
    order = [t["id"] for t in tests]
    rows = []
    for pupil in students:
        own = cells[pupil["id"]]
        milliy = [own[t]["score"] for t in order if t in own and own[t]["suffix"] == ""]
        other = [own[t]["score"] for t in order if t in own and own[t]["suffix"] == "%"]
        avg = _mean(milliy) if milliy else _mean(other)
        rows.append({"student_id": pupil["id"], "number": pupil["number"], "name": pupil["name"],
                     "results": {str(t): own[t] for t in order if t in own}, "count": len(own),
                     "avg": avg, "suffix": "" if milliy or not other else "%",
                     "grade": grade_for(avg) if milliy else None})
    rows.sort(key=lambda r: (r["avg"] is None, -(r["avg"] or 0), r["number"]))
    # Sinf o'rtachasi: Milliy testlar bo'lsa — umumiy ball (75), faqat oddiy testlar bo'lsa — foiz
    milliy_avgs = [r["avg"] for r in rows if r["avg"] is not None and r["suffix"] == ""]
    other_avgs = [r["avg"] for r in rows if r["avg"] is not None and r["suffix"] == "%"]
    averages, suffix = (milliy_avgs, "") if milliy_avgs else (other_avgs, "%")
    return {"class": {"id": cls["id"], "name": cls["name"], "students": len(students)},
            "tests": tests, "students": rows,
            "summary": {"tests": len(tests), "taken": sum(r["count"] for r in rows), "suffix": suffix,
                        "avg": _mean(averages), "max": max(averages) if averages else None}}
