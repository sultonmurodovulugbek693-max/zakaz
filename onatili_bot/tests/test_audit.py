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


def big_photo(width=3000, height=4000) -> bytes:
    """Telefon rasmiga o'xshash katta JPEG (shovqinli — yaxshi siqilmaydi)."""
    import random
    from PIL import Image
    random.seed(1)
    small = Image.frombytes("RGB", (300, 400), bytes(random.getrandbits(8) for _ in range(300 * 400 * 3)))
    out = io.BytesIO()
    small.resize((width, height)).save(out, "JPEG", quality=95)
    return out.getvalue()


class StorageGuard(unittest.IsolatedAsyncioTestCase):
    """Baza to'lib qolmasin: rasmlar siqiladi, eskilari tozalanadi, hajm nazorat qilinadi."""

    async def asyncSetUp(self):
        _env.fresh_db()

    def test_shrink_image(self):
        from PIL import Image
        photo = big_photo()
        small, mime = webapp.shrink_image(photo, "image/jpeg")
        self.assertEqual(mime, "image/jpeg")
        self.assertLess(len(small), len(photo) / 5)
        self.assertEqual(max(Image.open(io.BytesIO(small)).size), 1600)
        pdf = b"%PDF-1.4 chek"
        self.assertEqual(webapp.shrink_image(pdf, "application/pdf"), (pdf, "application/pdf"))
        self.assertEqual(webapp.shrink_image(b"\xff\xd8 buzuq", "image/jpeg"), (b"\xff\xd8 buzuq", "image/jpeg"))
        png = io.BytesIO()
        Image.new("RGBA", (2400, 1200), (10, 20, 30, 0)).save(png, "PNG")
        small, mime = webapp.shrink_image(png.getvalue(), "image/png")
        self.assertEqual((mime, Image.open(io.BytesIO(small)).mode), ("image/jpeg", "RGB"))

    async def test_receipt_stored_small_hash_from_original(self):
        import base64
        import hashlib
        photo = big_photo()
        pay = db.start_wallet_payment(OWNER, STUDENT, 5, 40000)
        saved = webapp.read_receipt_for

        async def no_ai(*_a, **_k):
            return False, ["sinov"], None
        webapp.read_receipt_for = no_ai
        try:
            await webapp.check_wallet_receipt(pay["id"], photo, "image/jpeg")
        finally:
            webapp.read_receipt_for = saved
        with db.connect() as conn:
            row = conn.execute("SELECT receipt, mime, receipt_hash FROM wallet_payments WHERE id = ?", (pay["id"],)).fetchone()
        self.assertLess(len(base64.b64decode(row["receipt"])), len(photo) / 5)
        self.assertEqual((row["mime"], row["receipt_hash"]), ("image/jpeg", hashlib.sha256(photo).hexdigest()))

    def test_purge_old_files_keeps_records(self):
        old = "2026-01-01 10:00"
        paid = db.start_wallet_payment(OWNER, STUDENT, 5, 40000)
        review = db.start_wallet_payment(OWNER, OTHER, 5, 40000)
        with db.connect() as conn:
            conn.execute("UPDATE wallet_payments SET receipt = 'eski', status = 'paid', created_at = ? WHERE id = ?", (old, paid["id"]))
            conn.execute("UPDATE wallet_payments SET receipt = 'kutyapti', status = 'review', created_at = ? WHERE id = ?",
                         (old, review["id"]))
        db.save_join_request(OWNER, STUDENT, "Ali", "Valiyev", "rasm", "image/jpeg")
        with db.connect() as conn:
            conn.execute("UPDATE join_requests SET status = 'approved', created_at = ?", (old,))
        test = esse_mock([])
        db.start_essay_scan(STUDENT, test["id"], "gemini")
        db.add_essay_scan_page(STUDENT, test["id"], "sahifa", "image/jpeg")
        with db.connect() as conn:
            conn.execute("UPDATE essay_scans SET updated_at = ?", (old,))
        self.assertIsNone(db.collecting_essay_scan(STUDENT))  # eski yig'ish chekni «esse sahifasi» deb tutmaydi
        counts = db.purge_old_files()
        self.assertEqual((counts["wallet_payments"], counts["join_requests"], counts["essay_scans"]), (1, 1, 1))
        with db.connect() as conn:
            rows = {r["id"]: r for r in conn.execute("SELECT id, receipt, status, amount FROM wallet_payments").fetchall()}
            scan = conn.execute("SELECT status, pages FROM essay_scans").fetchone()
        self.assertEqual((rows[paid["id"]]["receipt"], rows[paid["id"]]["status"], rows[paid["id"]]["amount"]),
                         (None, "paid", 40000))  # rasm o'chdi, to'lov yozuvi qoldi
        self.assertEqual(rows[review["id"]]["receipt"], "kutyapti")  # ko'rib chiqilmagani tegilmaydi
        self.assertEqual((scan["status"], scan["pages"]), ("expired", "[]"))
        self.assertEqual(db.purge_old_files()["wallet_payments"], 0)

    def test_recent_scan_still_collects(self):
        test = esse_mock([])
        db.start_essay_scan(STUDENT, test["id"], "gemini")
        self.assertEqual(db.collecting_essay_scan(STUDENT)["test_id"], test["id"])

    async def test_size_alert_and_admin_text(self):
        import ops
        fake = FakeBot()
        saved = (ops.ADMIN_IDS, db.database_size)
        ops.ADMIN_IDS = {OWNER}
        try:
            db.database_size = lambda: 10 * 1024 * 1024
            await ops.run_maintenance(fake)
            self.assertEqual(fake.messages, [])  # 10 MB — xabar yo'q
            db.database_size = lambda: 420 * 1024 * 1024
            result = await ops.run_maintenance(fake)
        finally:
            ops.ADMIN_IDS, db.database_size = saved
        self.assertIn("deyarli to'ldi", fake.messages[0][1])
        self.assertIn("420,0 MB / 500 MB (84%)", ops.size_text(result["size"]))
        self.assertIsInstance(db.database_size(), int)  # SQLite — fayl hajmi

    def test_pool_check_skipped_for_fresh_connection(self):
        from psycopg_pool import ConnectionPool
        checked = []
        saved = ConnectionPool.check_connection
        ConnectionPool.check_connection = staticmethod(lambda conn: checked.append(conn))
        try:
            class Conn:  # weakref bo'ladigan oddiy obyekt
                pass
            fresh, idle = Conn(), Conn()
            db._mark_returned(fresh)
            db._check_if_idle(fresh)
            db._check_if_idle(idle)  # hech qachon qaytmagan (yangi yoki uzoq turgan) — tekshiriladi
        finally:
            ConnectionPool.check_connection = saved
        self.assertEqual(checked, [idle])


async def _done():
    return None


if __name__ == "__main__":
    unittest.main()
