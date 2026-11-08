"""
Milliy sertifikat uslubidagi yakuniy natija — DTM «Baholash mezonlari» tartibida:

  1-bo'lim — test topshiriqlari (1–44): Rasch → T-shkala (50 + 10·Z), ko'pi bilan 75 ball;
  2-bo'lim — esse: 24 ballik mezon bilan baholanadi va jadval bo'yicha 75 ballik shkalaga
             o'tkaziladi (structure.essay_to_75);
  umumiy ball — ikki bo'limning o'rtacha arifmetigi; daraja (A+ … C) shu umumiy balldan.

Rasch bali testni ishlagan HAMMA qatnashchilarning javoblaridan hisoblanadi (savol qiyinligi
shulardan aniqlanadi), shuning uchun u har yangi natija bilan biroz o'zgaradi — bu rasmiy natija
emas, mock uchun taxminiy baho. Essesi hali baholanmagan qatnashchida faqat 1-bo'lim ko'rsatiladi.
"""

from rasch import grade_for, rasch_scores
from structure import essay_to_75


def rasch_breakdown(test: dict, attempts: list[dict]) -> dict[int, dict]:
    """{attempt_id: {"test": 1-bo'lim, "essay": esse (75 ballik) yoki None, "score": umumiy, "grade": daraja}}."""
    if not attempts:
        return {}
    labels = [item["label"] for item in test["items"]]
    matrix = []
    for attempt in attempts:
        wrong = set(attempt["wrong"])
        matrix.append([0 if label in wrong else 1 for label in labels])
    section1 = rasch_scores(matrix)["scores"]
    milliy = test["fmt"] == "milliy"
    out = {}
    for attempt, part1 in zip(attempts, section1):
        part1 = round(part1, 1)  # ko'rsatilgan sonlardan hisoblanadi — o'quvchi (test + esse) / 2 ni o'zi tekshira oladi
        essay = essay_to_75(attempt.get("essay")) if milliy else None
        score = round((part1 + essay) / 2, 1) if essay is not None else part1
        out[attempt["id"]] = {"test": part1, "essay": essay, "score": score, "grade": grade_for(score)}
    return out


def rasch_results(test: dict, attempts: list[dict]) -> dict[int, tuple[float, str]]:
    """{attempt_id: (umumiy ball, daraja)} — e'lon, PDF va Excel shundan foydalanadi."""
    return {key: (value["score"], value["grade"]) for key, value in rasch_breakdown(test, attempts).items()}


def total_points(attempt: dict) -> float:
    """To'plangan ball (100 dan, namunaviy test ballari): avtomatik tekshirilgan savollar + esse (24 ballik)."""
    return round((attempt.get("points") or 0) + (attempt.get("essay") or 0), 1)
