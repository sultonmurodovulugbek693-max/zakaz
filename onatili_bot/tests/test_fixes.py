"""
Tuzatishlar: ilovaga kirish faqat tugma bilan, oflayn esseni o'qituvchi bekor qilishi, e'lon takrorlanmasligi
(fonda, flood'da qayta urinadi), ommaviy tasdiqlashda o'quvchiga xabar, noto'g'ri son — 400, yangi testda PDF'lar
va oflayn testda majburiy obuna yo'qligi. Telegram almashtirilgan — internet kerak emas.
"""
import asyncio
import json
import time
import types
import unittest

import _env

from aiohttp.test_utils import TestClient, TestServer
from aiogram.exceptions import TelegramRetryAfter

import bot
import db
import plans
import structure
import webapp

OWNER, STUDENT, OTHER = 111, 222, 333


class FakeBot:
    def __init__(self, flood_once=False):
        self.messages, self.documents, self.flood_once = [], [], flood_once

    async def send_message(self, chat_id, text, **_):
        if self.flood_once:
            self.flood_once = False
            raise TelegramRetryAfter(method=types.SimpleNamespace(), message="flood", retry_after=0)
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, **_):
        self.documents.append((chat_id, getattr(document, "filename", "")))


class AppLogin(unittest.TestCase):
    def setUp(self):
        _env.fresh_db()

    def test_login_needs_confirmation_once(self):
        db.create_login("a" * 32)
        self.assertTrue(db.login_pending("a" * 32))
        self.assertIsNone(db.take_login("a" * 32))  # tugma bosilmaguncha ilova sessiya olmaydi
        self.assertTrue(db.confirm_login("a" * 32, OWNER, "s1"))
        self.assertFalse(db.confirm_login("a" * 32, OTHER, "s2"))  # ikkinchi bosish — boshqa sessiya yo'q
        self.assertEqual(db.take_login("a" * 32), "s1")

    def test_cancel_and_logout_all(self):
        db.create_login("b" * 32)
        db.cancel_login("b" * 32)
        self.assertFalse(db.login_pending("b" * 32))
        db.remember_user(OWNER, "Ustoz", None)
        db.create_login("c" * 32)
        db.confirm_login("c" * 32, OWNER, "s3")
        self.assertEqual(db.delete_user_sessions(OWNER), 1)
        self.assertIsNone(db.session_user("s3"))


class ServerBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        plans.grant(OWNER, "month")
        self.bot = FakeBot()
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}

        async def fake_auth(_request):
            return self.user

        self._auth = webapp.auth
        webapp.auth = fake_auth
        self._current = webapp.current_user
        webapp.current_user = fake_auth
        webapp.HOOKS.clear()
        webapp.HOOKS.update(
            record_attempt=lambda *a, **k: bot.record_attempt(self.bot, *a, **k),
            announce=lambda t, o: bot.announce_test(self.bot, t, o),
            send_pdf=lambda c, t, k: bot.send_pdf(self.bot, c, t, k),
            send_sheets=lambda c, t, s, b: bot.send_sheets(self.bot, c, t, s, b),
            send_text=lambda c, t: self.bot.send_message(c, t),
            essay_notice=lambda t, a, s: bot.essay_notice(self.bot, t, a, s),
            resolve_channel=lambda ref: asyncio.sleep(0, {"chat_id": -100, "title": "K", "url": "https://t.me/k"}),
        )
        self.client = TestClient(TestServer(webapp.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        webapp.auth = self._auth
        webapp.current_user = self._current
        webapp.HOOKS.clear()
        await self.client.close()

    async def post(self, path, payload):
        response = await self.client.post(path, json=payload)
        return response.status, await response.json()

    async def create(self, mode="online", fmt="oddiy", channels=()):
        items = structure.build_items(fmt, 5 if fmt == "oddiy" else 45)
        key = {i["label"]: ("A" if i["kind"] == "choice" else "kitob") for i in items}
        return await self.post("/api/teacher/tests", {"title": "Sinov", "fmt": fmt, "count": 5, "mode": mode,
                                                      "keys": {"A": key}, "channels": list(channels)})


class ServerFixes(ServerBase):
    async def test_offline_test_sends_key_and_sheet_without_channels(self):
        status, r = await self.create("offline", channels=["@kanal"])
        self.assertEqual(status, 200)
        self.assertEqual(sorted(r["sent"]), ["key", "sheets"])
        self.assertIsNone(r["pdf_error"])
        self.assertEqual(db.get_test(r["id"])["channels"], [])  # oflaynda majburiy obuna yo'q
        names = sorted(name.split("_")[0] for _, name in self.bot.documents)
        self.assertEqual(names, ["kalit", "titullar"])
        status, _ = await self.post(f"/api/teacher/test/{r['id']}/channels", {"channels": ["@kanal"]})
        self.assertEqual(status, 400)

    async def test_pdf_failure_keeps_test_and_explains(self):
        async def blocked(*_):
            raise RuntimeError("Telegram server says - Forbidden: bot can't initiate conversation with a user")
        webapp.HOOKS["send_pdf"] = blocked
        status, r = await self.create()
        self.assertEqual(status, 200)
        self.assertIn("/start", r["pdf_error"])
        self.assertIsNotNone(db.get_test(r["id"]))

    async def test_announce_runs_in_background_and_never_repeats(self):
        _, r = await self.create()
        test = db.get_test(r["id"])
        await bot.record_attempt(self.bot, test, STUDENT, "Ali Valiyev", "A", {"1": "A"}, None, [], 0.0)
        self.bot.messages.clear()
        self.bot.flood_once = True  # birinchi xabarda Telegram «kuting» deydi — qayta yuboriladi
        status, res = await self.post(f"/api/teacher/test/{r['id']}/announce", {})
        self.assertEqual((status, res["queued"]), (200, 1))
        self.assertTrue(db.get_test(r["id"])["closed"])  # darhol yopiladi
        await asyncio.gather(*webapp._BACKGROUND)
        self.assertEqual([c for c, _ in self.bot.messages].count(STUDENT), 1)
        # Qayta ochib, yana e'lon — o'quvchiga ikkinchi xabar bormaydi
        await self.post(f"/api/teacher/test/{r['id']}/reopen", {})
        self.bot.messages.clear()
        _, res = await self.post(f"/api/teacher/test/{r['id']}/announce", {})
        await asyncio.gather(*webapp._BACKGROUND)
        self.assertEqual(res["queued"], 0)
        self.assertNotIn(STUDENT, [c for c, _ in self.bot.messages])

    async def test_confirm_all_notifies_after_announce(self):
        _, r = await self.create(fmt="milliy")
        test = db.get_test(r["id"])
        attempt = await bot.record_attempt(self.bot, test, STUDENT, "Ali Valiyev", "A", {}, "Esse " * 60, [], 0.0)
        db.set_essay_text(attempt["id"], "Esse " * 60, "pending")
        db.save_essay_result(attempt["id"], {"score": 18, "provider": "test"})
        db.set_closed(test["id"], True)
        self.bot.messages.clear()
        status, res = await self.post(f"/api/teacher/test/{r['id']}/essays/confirm-all", {})
        self.assertEqual((status, res["count"]), (200, 1))
        self.assertTrue(any(c == STUDENT and "Esse" in t for c, t in self.bot.messages))

    async def test_bad_numbers_are_400_not_500(self):
        items = structure.build_items("oddiy", 5)
        status, _ = await self.post("/api/teacher/tests", {"title": "X", "fmt": "oddiy", "count": "abc",
                                                           "keys": {"A": {i["label"]: "A" for i in items}}})
        self.assertEqual(status, 400)
        _, r = await self.create("offline")
        status, _ = await self.post(f"/api/teacher/test/{r['id']}/sheets", {"blanks": "ko'p"})
        self.assertEqual(status, 400)
        webapp.HOOKS["scan_batch_notice"] = lambda *a: asyncio.sleep(0)
        status, _ = await self.post(f"/api/teacher/test/{r['id']}/scan/upload/done", {"saved": "x"})
        self.assertEqual(status, 400)

    async def test_offline_essay_cancel_frees_id(self):
        _, r = await self.create("offline", fmt="milliy")
        test = db.get_test(r["id"])
        db.add_students(test["id"], ["Ali Valiyev"], ["A"], [7])
        self.user = {"id": OTHER, "name": "Begona", "username": "beg"}
        status, _ = await self.post(f"/api/test/{test['code']}/essay", {"number": "0007", "essay": "so'z " * 40})
        self.assertEqual(status, 400)  # ism majburiy
        status, _ = await self.post(f"/api/test/{test['code']}/essay",
                                    {"number": "0007", "name": "Hacker Boy", "essay": "so'z " * 40})
        self.assertEqual(status, 200)
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        listed = await (await self.client.get(f"/api/teacher/test/{test['id']}")).json()
        row = listed["offline_essays"][0]
        self.assertEqual((row["number"], row["name_ok"], row["account"]), (7, False, "Begona"))
        self.bot.messages.clear()
        status, res = await self.post(f"/api/teacher/test/{test['id']}/offline-essays/7/delete", {})
        self.assertEqual((status, res["offline_essays"]), (200, []))
        self.assertTrue(any(c == OTHER for c, _ in self.bot.messages))  # yuborganga xabar
        self.user = {"id": STUDENT, "name": "Ali", "username": None}
        status, _ = await self.post(f"/api/test/{test['code']}/essay",
                                    {"number": "0007", "name": "Ali Valiyev", "essay": "so'z " * 40})
        self.assertEqual(status, 200)  # ID bo'shadi — haqiqiy egasi yubora oladi


class ClassFromId(ServerBase):
    """Titul qaysi testda skanerlansa ham o'quvchi va uning sinfi doimiy ID'dan aniqlanadi."""

    async def test_scanned_id_resolves_pupil_from_any_class(self):
        eleven = db.create_class(OWNER, "11-A")
        seven = db.create_class(OWNER, "7-B")
        ali = db.add_class_students(eleven["id"], OWNER, ["Ali Valiyev"])[0]
        db.add_class_students(seven["id"], OWNER, ["Madina Yusupova"])
        _, r = await self.create("offline")
        test = db.get_test(r["id"])
        # Test ro'yxati bo'sh (yoki boshqa sinfniki) — titulda faqat 11-A o'quvchisining ID'si
        answers = {str(i): "A" for i in range(1, 6)}
        status, saved = await self.post(f"/api/teacher/test/{test['id']}/scan/save",
                                        {"number": str(ali["number"]), "variant": "A", "answers": answers})
        self.assertEqual(status, 200)
        self.assertEqual(saved["name"], "Ali Valiyev")  # ism sinf ro'yxatidan
        page = await (await self.client.get(f"/api/teacher/test/{test['id']}")).json()
        self.assertEqual(page["students"][0]["class_name"], "11-A")
        self.assertIn(ali["number"], [p["number"] for p in page["pupils"]])  # oflayn skaner uchun
        # Natijalar bo'limida — 11-A sinfida (7-B da emas)
        report = await (await self.client.get(f"/api/teacher/class/{eleven['id']}/results")).json()
        self.assertIn("Ali Valiyev", str(report))
        self.assertIn(test["title"], str(report))
        other = await (await self.client.get(f"/api/teacher/class/{seven['id']}/results")).json()
        self.assertNotIn("Ali Valiyev", str(other))
        # Shu ID qayta skanerlansa — ikkinchi o'quvchi ochilmaydi, natija almashtiriladi
        status, _ = await self.post(f"/api/teacher/test/{test['id']}/scan/save",
                                    {"number": str(ali["number"]), "variant": "A", "answers": {"1": "B"}})
        self.assertEqual(status, 200)
        self.assertEqual(len(db.list_students(test["id"])), 1)
        self.assertEqual(len(db.list_attempts(test["id"])), 1)

    async def test_ninth_grader_scanned_inside_eleventh_grade_test(self):
        """11-sinf testi ichida 9-sinf o'quvchisining titulini skanerlasa ham — natija 9-sinfga tushadi."""
        eleven, nine = db.create_class(OWNER, "11-sinf"), db.create_class(OWNER, "9-sinf")
        ali = db.add_class_students(eleven["id"], OWNER, ["Ali Valiyev"])[0]
        vali = db.add_class_students(nine["id"], OWNER, ["Vali Karimov"])[0]
        _, r = await self.create("offline")
        await self.post(f"/api/teacher/test/{r['id']}/students/from-class", {"class_id": eleven["id"]})  # 11-sinf ro'yxati
        status, saved = await self.post(f"/api/teacher/test/{r['id']}/scan/save",
                                        {"number": str(vali["number"]), "variant": "A", "answers": {"1": "A"}})
        self.assertEqual((status, saved["name"]), (200, "Vali Karimov"))
        page = await (await self.client.get(f"/api/teacher/test/{r['id']}")).json()
        classes = {s["name"]: s["class_name"] for s in page["students"]}
        self.assertEqual(classes, {"Ali Valiyev": "11-sinf", "Vali Karimov": "9-sinf"})
        nine_report = await (await self.client.get(f"/api/teacher/class/{nine['id']}/results")).json()
        self.assertIn("Vali Karimov", str(nine_report))
        eleven_report = await (await self.client.get(f"/api/teacher/class/{eleven['id']}/results")).json()
        self.assertNotIn("Vali Karimov", str(eleven_report))
        self.assertIsNotNone(ali)


class SixProblems(ServerBase):
    async def test_sheets_sent_marks_first_step(self):
        _, r = await self.create("offline")
        page = await (await self.client.get(f"/api/teacher/test/{r['id']}")).json()
        self.assertTrue(page["test"]["sheets_sent"])  # yaratishda titul PDF'i ketdi — 1-qadam bajarilgan
        # Onlayn testga ham titul yuboriladi — o'qituvchi chop etib tarqatadi
        self.bot.documents.clear()
        _, r2 = await self.create("online")
        self.assertEqual(sorted(r2["sent"]), ["key", "sheets"])
        self.assertEqual(sorted(n.split("_")[0] for _, n in self.bot.documents), ["kalit", "titullar"])

    async def test_announce_resumes_after_restart(self):
        _, r = await self.create()
        test = db.get_test(r["id"])
        for uid in (STUDENT, OTHER):
            await bot.record_attempt(self.bot, test, uid, f"O'quvchi {uid}", "A", {"1": "A"}, None, [], 0.0)
        # E'lon boshlangan, birinchi o'quvchiga yuborilgan, keyin server o'chib qolgan
        db.set_closed(test["id"], True)
        db.set_announcing(test["id"], True)
        first = db.list_attempts(test["id"])[0]
        db.mark_notified(first["id"])
        self.bot.messages.clear()
        await bot.resume_announcements(self.bot)
        targets = [c for c, _ in self.bot.messages if c in (STUDENT, OTHER)]
        self.assertEqual(targets, [OTHER])  # faqat qolgan o'quvchiga
        self.assertEqual(db.announcing_tests(), [])
        self.bot.messages.clear()
        await bot.resume_announcements(self.bot)  # ikkinchi marta — hech narsa
        self.assertEqual(self.bot.messages, [])

    async def test_rate_limit(self):
        webapp._HITS.clear()
        codes = [(await self.client.post("/api/app/login")).status for _ in range(32)]
        self.assertEqual(codes.count(429), 2)  # daqiqasiga 30 ta kirish havolasi
        webapp._HITS.clear()

    async def test_upload_dedupe_survives_restart(self):
        from aiohttp import FormData
        _, r = await self.create("offline")
        answers = {str(i): "A" for i in range(1, 6)}

        async def upload():
            form = FormData()
            form.add_field("data", json.dumps({"uid": "phone-1", "name": "Ali Valiyev", "variant": "A", "answers": answers}))
            response = await self.client.post(f"/api/teacher/test/{r['id']}/scan/upload", data=form)
            return response.status, await response.json()

        status, first = await upload()
        self.assertEqual(status, 200)
        webapp._UPLOADED.clear()
        webapp._STUDENT_OF.clear()  # server qayta ishga tushdi — xotira bo'sh
        status, again = await upload()
        self.assertEqual((status, again), (200, first))
        self.assertEqual(len(db.list_attempts(r["id"])), 1)

    async def test_essay_pin(self):
        import sheet
        _, r = await self.create("offline", fmt="milliy")
        test = db.get_test(r["id"])
        db.add_students(test["id"], ["Ali Valiyev"], ["A"], [7])
        pin = sheet.essay_pin(test["id"], 7)
        self.assertRegex(pin, r"^\d{4}$")
        self.assertEqual(pin, sheet.essay_pin(test["id"], 7))
        essay = {"number": "0007", "essay": "so'z " * 40}
        # Begona akkaunt kodsiz egallab oladi
        self.user = {"id": OTHER, "name": "Begona", "username": None}
        self.assertEqual((await self.post(f"/api/test/{test['code']}/essay", {**essay, "name": "Hacker Boy"}))[0], 200)
        # Haqiqiy egasi: noto'g'ri kod — 400; kodsiz — 409; to'g'ri kod — o'zi oladi
        self.user = {"id": STUDENT, "name": "Ali", "username": None}
        wrong = "0000" if pin != "0000" else "1111"
        self.assertEqual((await self.post(f"/api/test/{test['code']}/essay", {**essay, "name": "Ali Valiyev", "pin": wrong}))[0], 400)
        self.assertEqual((await self.post(f"/api/test/{test['code']}/essay", {**essay, "name": "Ali Valiyev"}))[0], 409)
        self.bot.messages.clear()
        self.assertEqual((await self.post(f"/api/test/{test['code']}/essay", {**essay, "name": "Ali Valiyev", "pin": pin}))[0], 200)
        row = db.get_offline_essay(test["id"], 7)
        self.assertEqual((row["tg_id"], row["verified"]), (STUDENT, 1))
        self.assertTrue(any(c == OTHER for c, _ in self.bot.messages))  # begonaga xabar
        # Endi begona kod bilan ham olib qo'ya olmaydi (kodni bilmaydi), kodsiz — 409
        self.user = {"id": OTHER, "name": "Begona", "username": None}
        self.assertEqual((await self.post(f"/api/test/{test['code']}/essay", {**essay, "name": "Hacker Boy"}))[0], 409)
        page = await self._teacher_page(test["id"])
        self.assertTrue(page["offline_essays"][0]["verified"])

    async def _teacher_page(self, test_id):
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        return await (await self.client.get(f"/api/teacher/test/{test_id}")).json()

    def test_sheet_prints_pin(self):
        import sheet
        test = db.create_test(OWNER, "Mock", "milliy", 45, structure.build_items("milliy"), {"A": {}})
        drawn = []

        class Canvas:
            def __getattr__(self, name):
                return lambda *a, **k: drawn.append(a)
        sheet.draw_answer_page(Canvas(), test, ["A"], {"number": 7, "name": "Ali"})
        texts = " ".join(str(x) for args in drawn for x in args)
        self.assertIn(f"Esse kodi: {sheet.essay_pin(test['id'], 7)}", texts)


class DirectDownload(ServerBase):
    async def test_download_link_gives_files(self):
        _, r = await self.create("offline", fmt="milliy")
        for kind, magic in (("key", b"%PDF"), ("sheets", b"%PDF"), ("results", b"%PDF"), ("excel", b"PK")):
            status, res = await self.post(f"/api/teacher/test/{r['id']}/download", {"kind": kind})
            self.assertEqual(status, 200)
            path = res["url"].split("/dl/")[1]
            response = await self.client.get(f"/dl/{path}")
            self.assertEqual(response.status, 200, kind)
            self.assertTrue((await response.read()).startswith(magic), kind)
            self.assertIn("attachment", response.headers["Content-Disposition"])

    async def test_download_link_is_signed_and_expires(self):
        _, r = await self.create()
        _, res = await self.post(f"/api/teacher/test/{r['id']}/download", {"kind": "key"})
        test_id, kind, expires, sig = res["url"].split("/dl/")[1].split(".")
        forged = f"{int(test_id) + 1}.{kind}.{expires}.{sig}"  # boshqa test — imzo mos emas
        self.assertEqual((await self.client.get(f"/dl/{forged}")).status, 404)
        old = f"{test_id}.{kind}.{int(time.time()) - 5}"
        self.assertEqual((await self.client.get(f"/dl/{old}.{webapp._sign(old)}")).status, 410)

    async def test_key_pdf_is_not_protected(self):
        sent = {}

        class Bot:
            async def send_document(self, chat_id, document, **kwargs):
                sent.update(kwargs)
        _, r = await self.create()
        await bot.send_pdf(Bot(), OWNER, db.get_test(r["id"]), "key")
        self.assertNotIn("protect_content", sent)  # himoyalangan PDF telefonda ochilmasdi


class SalesClosed(ServerBase):
    """Obuna sotish o'chiq: test yaratishni faqat FREE_IDS / admin / admin botda ruxsat bergan o'qituvchi qiladi."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._sales = (plans.SALES_ENABLED, webapp.SALES_ENABLED)
        plans.SALES_ENABLED = webapp.SALES_ENABLED = False

    async def asyncTearDown(self):
        plans.SALES_ENABLED, webapp.SALES_ENABLED = self._sales
        await super().asyncTearDown()

    async def test_only_allowed_teachers_create(self):
        # OWNER'da obuna bor (asyncSetUp), lekin sotish yopiq — baribir mumkin emas
        status, r = await self.create()
        self.assertEqual(status, 403)
        self.assertTrue(r["closed"])
        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertEqual((boot["sales"], boot["can_create"]), (False, False))
        status, _ = await self.post("/api/plan/order", {"period": "month"})
        self.assertEqual(status, 403)
        # Admin botda ruxsat beradi — endi yarata oladi
        self.assertTrue(plans.set_access(OWNER, True))
        self.assertFalse(plans.set_access(OWNER, True))  # ikkinchi marta — o'zgarmaydi
        status, _ = await self.create()
        self.assertEqual(status, 200)
        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertTrue(boot["can_create"])
        # Ruxsat olinadi — yana yopiq
        plans.set_access(OWNER, False)
        self.assertEqual((await self.create())[0], 403)

    async def test_admin_command(self):
        replies, sent = [], []

        class Msg:
            def __init__(self, uid):
                self.from_user = types.SimpleNamespace(id=uid)
                self.bot = types.SimpleNamespace(send_message=lambda chat, text, **k: asyncio.sleep(0, sent.append(chat)))

            async def answer(self, text, **_):
                replies.append(text)

        admin = 999
        bot.ADMIN_IDS.add(admin)
        try:
            await bot.cmd_access(Msg(admin), types.SimpleNamespace(command="ruxsat", args=str(OTHER)))
            self.assertIn(OTHER, plans.granted_ids())
            self.assertEqual(sent, [OTHER])  # o'qituvchiga xabar
            await bot.cmd_access(Msg(admin), types.SimpleNamespace(command="ruxsat_ol", args=str(OTHER)))
            self.assertNotIn(OTHER, plans.granted_ids())
            await bot.cmd_access(Msg(OTHER), types.SimpleNamespace(command="ruxsat", args=str(OTHER)))
            self.assertNotIn(OTHER, plans.granted_ids())  # admin emas — o'zi ruxsat ololmaydi
        finally:
            bot.ADMIN_IDS.discard(admin)


class ClassAverage(ServerBase):
    async def test_percent_tests_have_class_average(self):
        """Faqat oddiy (foizli) testlar bo'lgan sinfda o'rtacha 0 emas, foizda chiqadi."""
        cls = db.create_class(OWNER, "7-B")
        pupils = db.add_class_students(cls["id"], OWNER, ["Ali Valiyev", "Vali Karimov"])
        _, r = await self.create("offline")
        for pupil, right in zip(pupils, (4, 2)):
            answers = {str(q): "A" if q <= right else "B" for q in range(1, 6)}
            await self.post(f"/api/teacher/test/{r['id']}/scan/save",
                            {"number": str(pupil["number"]), "variant": "A", "answers": answers})
        report = await (await self.client.get(f"/api/teacher/class/{cls['id']}/results")).json()
        self.assertEqual((report["summary"]["avg"], report["summary"]["max"], report["summary"]["suffix"]), (60.0, 80.0, "%"))


class EssaySelfCheck(ServerBase):
    """O'quvchi onlayn esseni o'zi tekshiradi: AI bahosi darhol natijaga va Rasch'ga, o'qituvchiga xabar."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        import essay_ai
        self.essay_ai = essay_ai
        self._grade, self._enabled = essay_ai.grade, essay_ai.essay_enabled

        async def fake_grade(topic, text, images=()):
            criteria = [{"key": k, "score": 2 if i < 9 else 0, "comment": "Yaxshi"} for i, k in enumerate(essay_ai.RUBRIC_KEYS)]
            return {"score": sum(c["score"] for c in criteria), "criteria": criteria, "comment": "Umumiy izoh",
                    "outline": False, "transcription": "", "cost": 0.01, "provider": "test"}
        essay_ai.grade = fake_grade
        essay_ai.essay_enabled = lambda: True
        self.woke = []
        webapp.HOOKS["wake_ai"] = lambda: self.woke.append(1)

    async def asyncTearDown(self):
        self.essay_ai.grade, self.essay_ai.essay_enabled = self._grade, self._enabled
        await super().asyncTearDown()

    async def test_student_sees_score_and_it_goes_to_teacher_and_rasch(self):
        _, r = await self.create(fmt="milliy")
        test = db.get_test(r["id"])
        self.user = {"id": STUDENT, "name": "Ali", "username": None}
        status, sub = await self.post(f"/api/test/{test['code']}/submit",
                                      {"variant": "A", "name": "Ali Valiyev", "answers": {"1": "A"}, "essay": "So'z " * 80})
        self.assertEqual((status, sub["essay"]), (200, True))
        self.assertEqual(self.woke, [1])  # navbat darhol uyg'ondi
        mine = await (await self.client.get(f"/api/my-essay/{sub['attempt_id']}")).json()
        self.assertEqual(mine["status"], "pending")
        self.bot.messages.clear()
        for essay in db.pending_essays(5):
            await bot.grade_one(self.bot, essay)
        mine = await (await self.client.get(f"/api/my-essay/{sub['attempt_id']}")).json()
        expected = len(self.essay_ai.RUBRIC_KEYS[:9]) * 2
        self.assertEqual((mine["status"], mine["score"], len(mine["criteria"])), ("graded", expected, len(self.essay_ai.RUBRIC_KEYS)))
        self.assertEqual(db.get_attempt(sub["attempt_id"])["essay"], expected)  # natijaga qo'shildi
        self.assertTrue(any(c == OWNER and "Esse AI bilan tekshirildi" in t for c, t in self.bot.messages))
        # Rasch umumiy bali — test + esse o'rtachasi
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        page = await (await self.client.get(f"/api/teacher/test/{test['id']}")).json()
        row = page["attempts"][0]
        self.assertEqual(row["essay"], expected)
        # Boshqa o'quvchi birovning essesini ko'ra olmaydi
        self.user = {"id": OTHER, "name": "Begona", "username": None}
        self.assertEqual((await self.client.get(f"/api/my-essay/{sub['attempt_id']}")).status, 404)

    async def test_paper_essay_still_needs_teacher(self):
        _, r = await self.create(fmt="milliy")
        test = db.get_test(r["id"])
        attempt = await bot.record_attempt(self.bot, test, db.offline_tg_id(test["id"], 7), "Qog'oz", "A", {}, None, ["file-1"], 0.0)
        db.set_essay_text(attempt["id"], "Qo'lda yozilgan " * 20, "pending")
        for essay in db.pending_essays(5):
            await bot.grade_one(self.bot, essay)
        self.assertIsNone(db.get_attempt(attempt["id"])["essay"])  # o'qituvchi tasdiqlaydi


class Names(unittest.TestCase):
    def test_names_match(self):
        self.assertTrue(webapp.names_match("valiyev ali", "Ali Valiyev"))
        self.assertTrue(webapp.names_match("Gʻulomov Aziz", "G'ulomov Aziz"))
        self.assertFalse(webapp.names_match("Hacker Boy", "Ali Valiyev"))
        self.assertIsNone(webapp.names_match("Ali", None))


if __name__ == "__main__":
    unittest.main()
