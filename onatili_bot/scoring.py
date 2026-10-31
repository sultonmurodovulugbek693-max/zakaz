"""
Milliy sertifikat uslubidagi yakuniy natija: Rasch bali (T-shkala) va daraja.

Rasch bali testni ishlagan HAMMA qatnashchilarning javoblaridan hisoblanadi
(savol qiyinligi shulardan aniqlanadi), shuning uchun u har yangi natija bilan
biroz o'zgaradi va qatnashchilar qancha ko'p bo'lsa, shuncha aniq bo'ladi.
Bu — rasmiy natija emas, mock uchun taxminiy baho.

Esse (0–24) Rasch'ga 4 ta bosqich sifatida qo'shiladi (6, 12, 18, 24 ball) —
faqat hamma qatnashchining essesi baholangan bo'lsa.
"""

from rasch import grade_for, rasch_scores
from structure import ESSAY_MAX

ESSAY_STEPS = [ESSAY_MAX * step / 4 for step in range(1, 5)]


def rasch_results(test: dict, attempts: list[dict]) -> dict[int, tuple[float, str]]:
    """{attempt_id: (Rasch bali, daraja)}."""
    if not attempts:
        return {}
    labels = [item["label"] for item in test["items"]]
    with_essay = test["fmt"] == "milliy" and all(a["essay"] is not None for a in attempts)
    matrix = []
    for attempt in attempts:
        wrong = set(attempt["wrong"])
        row = [0 if label in wrong else 1 for label in labels]
        if with_essay:
            row += [1 if attempt["essay"] >= step else 0 for step in ESSAY_STEPS]
        matrix.append(row)
    scores = rasch_scores(matrix)["scores"]
    return {attempt["id"]: (score, grade_for(score)) for attempt, score in zip(attempts, scores)}


def total_points(attempt: dict) -> float:
    """To'plangan ball (100 dan): avtomatik tekshirilgan savollar + esse."""
    return round((attempt.get("points") or 0) + (attempt.get("essay") or 0), 1)
