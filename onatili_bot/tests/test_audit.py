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


class PracticeGaveUpRouted(unittest.IsolatedAsyncioTestCase):
    """Begona ustozda o'quvchi kredit bilan yuborgan esse: AI tekshira olmasa — kredit o'quvchiga, ustozga bepul kredit emas."""
    async def asyncSetUp(self):
        _env.fresh_db()
        self.bot = FakeBot()

    async def test_wallet_refund_only(self):
        import plans
        self.assertTrue(plans.platform_routed(OWNER))
        practice_id = db.create_practice(OWNER, STUDENT, "Mavzu", "Esse matni. " * 40, "public")
        db.set_practice_wallet(practice_id, "pending")
        before = db.get_credits(OWNER)
        saved = essay_ai.grade

        async def broken(*_a, **_k):
            raise essay_ai.AIError("o'qib bo'lmadi", retry=False)
        essay_ai.grade = broken
        try:
            await bot.grade_practice(self.bot, db.get_practice(practice_id))
        finally:
            essay_ai.grade = saved
        self.assertEqual(db.wallet_balance(OWNER, STUDENT), 1)
        self.assertEqual(db.get_credits(OWNER), before)


class RubricPerTeacher(ServerBase):
    """«⚙️ Sozlamalar» → «🤖 AI» → «📏 Baholash mezoni»: har ustoz o'zi tanlaydi; esse shu mezon bilan baholanadi."""
    ESSAY = "Kitob o'qish foydali. " * 30

    async def asyncSetUp(self):
        await super().asyncSetUp()
        essay_ai.forget_choices()
        self.prompts = []
        self._call = essay_ai._call

        async def fake_call(system, text, images, schema, **_k):
            self.prompts.append(system)
            keys = schema["properties"]["criteria"]["items"]["properties"]["key"]["enum"]
            steps = schema["properties"]["criteria"]["items"]["properties"]["score"]["enum"]
            import json
            out = {"criteria": [{"key": k, "score": 1.5, "comment": "izoh"} for k in keys],
                   "has_outline_or_epigraph": False, "overall_comment": "Yaxshi"}
            self.steps = steps
            return json.dumps(out), {"cost": 0.0}
        essay_ai._call = fake_call

    async def asyncTearDown(self):
        essay_ai._call = self._call
        essay_ai.forget_choices()
        await super().asyncTearDown()

    async def grade(self, teacher_id):
        practice_id = db.create_practice(teacher_id, STUDENT, "Kitob", self.ESSAY, "student")
        await bot.grade_practice(self.bot, db.get_practice(practice_id))
        return db.get_practice(practice_id)

    async def test_teacher_chooses_and_grading_follows(self):
        status, data = await self.get("/api/teacher/settings")
        self.assertEqual(status, 200, data)
        rub = data["rubric"]
        self.assertEqual((rub["chosen"], rub["own"], rub["for_all"]), ("official", False, False))
        self.assertEqual([o["key"] for o in rub["options"]], ["official", "classic"])
        self.assertTrue(all(o["about"] for o in rub["options"]))
        self.assertEqual((await self.post("/api/teacher/settings", {"rubric": "xyz"}))[0], 400)

        status, data = await self.post("/api/teacher/settings", {"rubric": "classic"})
        self.assertEqual((status, data["rubric"]["chosen"], data["rubric"]["own"]), (200, "classic", True))
        self.assertEqual((essay_ai.rubric_of(OWNER), essay_ai.rubric_of(OTHER)), ("classic", "official"))
        self.assertEqual(essay_ai.rubric_choice(), "official")  # boshqa ustozlarga ta'sir qilmaydi

        mine = await self.grade(OWNER)  # eski mezon: 12 × 0/1/2 — 1,5 → 1 (yarim ball yo'q)
        self.assertEqual(self.steps, [0, 1, 2])
        self.assertIn("kochirmaslik", self.prompts[-1])
        self.assertEqual((mine["ai_result"]["rubric"], mine["ai_score"]), ("classic", 12))
        self.assertTrue(any(c == STUDENT and "Publitsistik uslub va so'z boyligi" in t for c, t in self.bot.messages))
        view = webapp._practice_view(mine)
        self.assertEqual(view["titles"]["uslub"], "Publitsistik uslub va so'z boyligi")

        other = await self.grade(OTHER)  # tanlamagan ustoz — standart (UZBMB rasmiy jadvali)
        self.assertEqual(self.steps, [0, 0.5, 1, 1.5, 2])
        self.assertEqual((other["ai_result"]["rubric"], other["ai_score"]), ("official", 18))  # 12 × 1,5
        self.assertEqual(webapp._practice_view(other)["titles"]["uslub"], "Publitsistik uslub")

        status, data = await self.post("/api/teacher/settings", {"rubric": "official"})
        self.assertEqual((status, data["rubric"]["chosen"]), (200, "official"))
        self.assertEqual((await self.grade(OWNER))["ai_score"], 18)

    async def test_admin_sets_default_for_others(self):
        webapp.ADMIN_IDS.add(OTHER)
        try:
            self.user = {"id": OTHER, "name": "Admin", "username": None}
            status, data = await self.post("/api/teacher/settings", {"rubric": "classic"})
            self.assertEqual((status, data["rubric"]["chosen"], data["rubric"]["for_all"]), (200, "classic", True))
            self.assertEqual(essay_ai.rubric_choice(), "classic")
            self.assertEqual(essay_ai.rubric_of(OWNER), "classic")  # o'zi tanlamagan ustoz — admin standarti
            self.user = {"id": OWNER, "name": "Ustoz", "username": None}
            await self.post("/api/teacher/settings", {"rubric": "official"})
            self.assertEqual((essay_ai.rubric_of(OWNER), essay_ai.rubric_choice()), ("official", "classic"))
        finally:
            webapp.ADMIN_IDS.discard(OTHER)
            db.set_setting("ai:rubric", "official")

    async def test_expert_form_uses_own_rubric(self):
        import plans
        saved = plans.FREE_IDS
        plans.FREE_IDS = {OWNER}
        try:
            await self.post("/api/teacher/settings", {"rubric": "classic"})
            practice_id = db.create_practice(OWNER, STUDENT, "Kitob", self.ESSAY, "expert", None, "expert")
            queue = (await self.get("/api/expert/queue"))[1]
            self.assertEqual((queue["rubric_key"], queue["steps"], queue["rubric"][0]["key"]), ("classic", [2, 1, 0], "kirish"))
            scores = {c["key"]: 2 for c in queue["rubric"]}
            scores["imlo"] = 1.5  # eski mezonda yarim ball yo'q
            self.assertEqual((await self.post(f"/api/expert/practice/{practice_id}", {"scores": scores}))[0], 400)
            scores["imlo"] = 1
            status, graded = await self.post(f"/api/expert/practice/{practice_id}", {"scores": scores})
            self.assertEqual((status, graded["score_75"]), (200, 73), graded)
            self.assertEqual(db.get_practice(practice_id)["ai_result"]["rubric"], "classic")
        finally:
            plans.FREE_IDS = saved


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


class PracticePhoto(ServerBase):
    """«✍️ Esse» ilovada: qog'ozdagi esse rasmi (1–6 bet) — Telegram'ga saqlanadi va Claude navbatiga tushadi."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._saved = (essay_ai.essay_enabled, webapp.plans.FREE_IDS)
        essay_ai.essay_enabled = lambda: True
        self.stored = []

        async def store_photo(chat_id, data, caption):
            self.stored.append((chat_id, len(data), caption))
            return f"file-{len(self.stored)}"
        webapp.HOOKS["store_photo"] = store_photo
        webapp.HOOKS["wake_ai"] = lambda: None
        self.user = {"id": STUDENT, "name": "Ali Valiyev", "username": None}

    async def asyncTearDown(self):
        essay_ai.essay_enabled, webapp.plans.FREE_IDS = self._saved
        await super().asyncTearDown()

    async def send(self, files, **fields):
        from aiohttp import FormData
        form = FormData()
        for key, value in {"teacher_id": OWNER, **fields}.items():
            form.add_field(key, str(value))
        for name, data, mime in files:
            form.add_field("photo", data, filename=name, content_type=mime)
        response = await self.client.post("/api/practice/photo", data=form)
        return response.status, await response.json()

    async def test_linked_student_photo_goes_to_claude_queue(self):
        db.link_student(OWNER, STUDENT)
        db.add_credits(OWNER, 3)
        photo = big_photo(1500, 2000)
        status, res = await self.send([("1.jpg", photo, "image/jpeg"), ("2.jpg", photo, "image/jpeg")], topic="Kitob")
        self.assertEqual(status, 200, res)
        item = db.get_practice(res["id"])
        self.assertEqual((item["status"], item["kind"], item["topic"], item["text"]), ("pending", "student", "Kitob", ""))
        self.assertEqual(item["photos"], [["file-1", "image/jpeg"], ["file-2", "image/jpeg"]])
        self.assertEqual([c for c, _, _ in self.stored], [STUDENT, STUDENT])  # o'quvchining o'z bot chatiga
        self.assertTrue(all(size < len(photo) for _, size, _ in self.stored))  # siqilib yuborildi
        self.assertEqual(db.get_credits(OWNER), 2)  # ustozning 1 krediti
        _, page = await self.get("/api/practice")
        self.assertEqual(page["history"][0]["photos"], 2)

    async def test_expert_without_credits_sends_nothing(self):
        webapp.plans.FREE_IDS = {OWNER}
        db.set_setting(f"expert_price:{OWNER}", "20000")
        db.set_setting(f"essay_price:{OWNER}", "10000")
        status, res = await self.send([("1.jpg", big_photo(800, 1000), "image/jpeg")], mode="expert")
        self.assertEqual((status, res.get("need")), (402, 2))
        self.assertEqual(self.stored, [])  # kredit yetmasa — ekspertga rasm bormaydi

    async def test_validation(self):
        db.link_student(OWNER, STUDENT)
        self.assertEqual((await self.send([("1.jpg", big_photo(300, 400), "image/jpeg")]))[0], 402)  # ustozda kredit yo'q
        db.add_credits(OWNER, 3)
        self.assertEqual((await self.send([]))[0], 400)
        self.assertEqual((await self.send([("a.pdf", b"%PDF-1.4", "application/pdf")]))[0], 400)
        many = [(f"{i}.jpg", big_photo(300, 400), "image/jpeg") for i in range(7)]
        self.assertEqual((await self.send(many))[0], 400)
        self.user = {"id": OTHER, "name": "Begona", "username": None}
        self.assertEqual((await self.send([("1.jpg", big_photo(300, 400), "image/jpeg")], teacher_id=999999))[0], 403)
        self.assertEqual(self.stored, [])


if __name__ == "__main__":
    unittest.main()
