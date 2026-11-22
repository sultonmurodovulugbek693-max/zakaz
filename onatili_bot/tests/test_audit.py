"""
Katta tekshiruvda topilgan xatolar qaytmasin:
  • esse mocki (fmt="esse"): natijalar taxtasi, kanal e'loni, sertifikat, reyting, Excel/PDF va o'quvchiga xabar —
    ball 0 emas, essening 75 ballik bahosi; o'qituvchi sahifasida ham ball va esse holati;
  • AI esseni tekshira olmasa — o'quvchiga xabar, mock natijasi ustoz baholaguncha kutadi;
  • esse kodini taxmin qilish cheklangan; ustozning mt_ havolasi jadvalsiz mockda yiqilmaydi;
  • rasmiy mezondagi 1,5 ball botda to'g'ri ko'rinadi; obuna xabarlarida «0 ta esse» yo'q.
"""
import io
import types
import unittest

import _env

from test_fixes import OTHER, OWNER, STUDENT, FakeBot, ServerBase

import bot
import db
import essay_ai
import excel
import pdfs
import rating
import results
import structure
import webapp


def esse_mock(scores=((201, 18), (202, 12.5), (203, 22))) -> dict:
    """Esse mocki va qatnashchilar: (tg_id, 24 ballik baho). Kutilgan 75 ballik: 18 → 63, 12,5 → 52, 22 → 71."""
    items = structure.build_items("esse")
    test = db.create_test(OWNER, "Esse mock", "esse", 0, items, {"A": {}}, "Mavzu")
    for index, (uid, score) in enumerate(scores):
        attempt = db.save_attempt(test["id"], uid, f"O'quvchi{index} Ali", "A", {}, structure.grade(items, {}, {}))
        db.create_essay(attempt["id"], "Esse matni. " * 40, [], "graded")
        if score is not None:
            db.set_essay(attempt["id"], score)
    return db.get_test(test["id"])


class EsseMockScores(unittest.TestCase):
    def setUp(self):
        _env.fresh_db()
        self.test = esse_mock()
        self.attempts = db.list_attempts(self.test["id"])

    def test_results_board_and_channel_rows(self):
        data = results.test_results(self.test)  # «🏆 Mocklar» taxtasi, kanal e'loni va sertifikatlar shundan
        self.assertEqual([(r["score"], r["suffix"]) for r in data["rows"]], [(71, ""), (63, ""), (52, "")])
        self.assertEqual([r["grade"] for r in data["rows"]], ["A+", "B+", "C+"])
        self.assertEqual(results.overview(OWNER)["tests"][0]["suffix"], "")

    def test_student_announce_text(self):
        text = bot.result_text(self.attempts[0], self.test, (63.0, "B+"))
        self.assertIn("Esse: <b>63</b> / 75", text)
        self.assertIn("B+", text)
        self.assertNotIn("0 / 0", text)

    def test_rating_excel_pdf_certificate(self):
        self.assertEqual([r["score"] for r in rating.rows_of(self.test, self.attempts)], [71, 63, 52])
        self.assertTrue(rating.rating_images(self.test, self.attempts))
        import openpyxl
        sheet = openpyxl.load_workbook(io.BytesIO(excel.results_xlsx(self.test, self.attempts))).active
        rows = list(sheet.iter_rows(min_row=2, values_only=True))
        self.assertEqual([(row[2], row[3], row[4]) for row in rows], [(22, 71, "A+"), (18, 63, "B+"), (12.5, 52, "C+")])
        self.assertTrue(pdfs.results_pdf(self.test, self.attempts).startswith(b"%PDF"))
        row = results.test_results(self.test)["rows"][0]
        self.assertTrue(pdfs.certificates_pdf(self.test, [row], "Ustoz").startswith(b"%PDF"))

    def test_unrated_essay_waits_and_is_not_zero(self):
        test = esse_mock([(301, None)])
        self.assertEqual(results.test_results(test)["rows"][0]["score"], None)  # baholanmagan — 0 emas


class EsseMockTeacherPage(ServerBase):
    async def test_teacher_sees_score_and_essay_status(self):
        test = esse_mock([(201, 18), (202, None)])
        status, page = await self.get(f"/api/teacher/test/{test['id']}")
        self.assertEqual(status, 200)
        rows = {a["name"]: a for a in page["attempts"]}
        self.assertEqual((rows["O'quvchi0 Ali"]["rasch"], rows["O'quvchi0 Ali"]["grade"]), (63, "B+"))
        self.assertEqual(rows["O'quvchi1 Ali"]["essay_status"], "graded")  # «AI taklifi — tasdiqlang»

    async def test_certificates_for_esse_mock(self):
        test = esse_mock()
        webapp.HOOKS["send_file"] = lambda chat, content, name, caption: self.bot.send_document(
            chat, types.SimpleNamespace(filename=name))
        status, res = await self.post(f"/api/teacher/test/{test['id']}/certificates", {})
        self.assertEqual((status, res["count"]), (200, 3))


class AiGaveUp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        self.bot = FakeBot()
        self.test = esse_mock([])
        items = structure.build_items("esse")
        attempt = db.save_attempt(self.test["id"], STUDENT, "Ali Valiyev", "A", {}, structure.grade(items, {}, {}))
        db.create_essay(attempt["id"], "Esse matni. " * 40, [], "pending")  # pul to'lagan — kredit yechilmagan
        self.attempt_id = attempt["id"]

    async def test_student_told_and_mock_waits_for_teacher(self):
        saved = essay_ai.grade

        async def broken(*_a, **_k):
            raise essay_ai.AIError("o'qib bo'lmadi", retry=False)
        essay_ai.grade = broken
        try:
            await bot.grade_one(self.bot, db.pending_essays(5)[0])
        finally:
            essay_ai.grade = saved
        self.assertEqual(db.get_essay(self.attempt_id)["status"], "failed")
        self.assertTrue(any(c == STUDENT and "ustoz baholaydi" in t for c, t in self.bot.messages))
        self.assertEqual(db.unrated_teacher_essays(self.test["id"]), 1)  # mock natijasi ustoz baholaguncha kutadi


class PinAttempts(ServerBase):
    async def test_wrong_pin_locks_after_five(self):
        import sheet
        webapp._PIN_FAILS.clear()
        _, r = await self.create("offline", fmt="milliy")
        db.set_sheet_style(r["id"], "standard")  # eski titul: esse botda
        test = db.get_test(r["id"])
        pin = sheet.essay_pin(test["id"], 7)
        wrong = "0000" if pin != "0000" else "1111"
        essay = {"number": "0007", "essay": "so'z " * 40, "name": "Ali Valiyev"}
        self.user = {"id": STUDENT, "name": "Ali", "username": None}
        for _ in range(webapp.PIN_MAX_FAILS):
            self.assertEqual((await self.post(f"/api/test/{test['code']}/essay", {**essay, "pin": wrong}))[0], 400)
        self.assertEqual((await self.post(f"/api/test/{test['code']}/essay", {**essay, "pin": pin}))[0], 429)
        self.user = {"id": OTHER, "name": "Boshqa", "username": None}  # boshqa akkaunt — o'z hisobi
        self.assertEqual((await self.post(f"/api/test/{test['code']}/essay", {**essay, "pin": pin}))[0], 200)
        webapp._PIN_FAILS.clear()


class SmallFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()

    async def test_own_mock_link_without_schedule(self):
        items = structure.build_items("esse")
        test = db.create_test(OWNER, "Jadvalsiz mock", "esse", 0, items, {"A": {}}, "Mavzu")
        replies = []

        async def answer(text, **_):
            replies.append(text)
        user = types.SimpleNamespace(id=STUDENT, full_name="Ali Valiyev", username=None, first_name="Ali")
        message = types.SimpleNamespace(from_user=user, text=f"/start {webapp.own_mock_code(test)}", answer=answer)
        state = types.SimpleNamespace(clear=lambda: _done())
        await bot.cmd_start(message, state)
        self.assertTrue(any("Jadvalsiz mock" in r for r in replies))
        self.assertTrue(db.is_linked(OWNER, STUDENT))

    def test_half_points_in_bot_result(self):
        result = {"score": 19.5, "comment": "", "criteria": [{"key": "imlo", "score": 1.5, "comment": ""}]}
        text = bot.practice_result_text(result, 250)
        self.assertIn("🟡", text)
        self.assertIn("1,5/2", text)

    async def test_no_zero_essays_in_plan_messages(self):
        fake = FakeBot()
        await bot.notify_granted(fake, OWNER, {"name": "1 oy", "expires": "22.12.2026", "limits": {"essays": 0}})
        self.assertNotIn("0 ta esse", fake.messages[0][1])


async def _done():
    return None


if __name__ == "__main__":
    unittest.main()
