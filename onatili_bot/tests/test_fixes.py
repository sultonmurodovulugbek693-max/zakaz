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
from datetime import timedelta

import _env

from aiohttp.test_utils import TestClient, TestServer
from aiogram.exceptions import TelegramRetryAfter

import bot
import db
import essay_ai
import plans
import structure
import rating
import webapp

OWNER, STUDENT, OTHER = 111, 222, 333


class FakeBot:
    def __init__(self, flood_once=False):
        self.messages, self.documents, self.photos, self.flood_once = [], [], [], flood_once

    async def send_message(self, chat_id, text, **_):
        if self.flood_once:
            self.flood_once = False
            raise TelegramRetryAfter(method=types.SimpleNamespace(), message="flood", retry_after=0)
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, **_):
        self.documents.append((chat_id, getattr(document, "filename", "")))

    async def send_photo(self, chat_id, photo, **_):
        self.photos.append((chat_id, getattr(photo, "filename", "")))

    async def send_media_group(self, chat_id, media, **_):
        self.photos.extend((chat_id, getattr(m.media, "filename", "")) for m in media)


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

    async def get(self, path):
        response = await self.client.get(path)
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
        db.set_sheet_style(r["id"], "standard")  # eski titul: esse botda (yangi oflayn test — qog'ozda)
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


class RatingImages(ServerBase):
    """Reyting va statistika rasmlari (albom) + PDF; ilovadagi «🏆 Reyting rasmi» tugmasi."""

    async def test_rating_album_and_pdf(self):
        _, r = await self.create("online", fmt="milliy")
        test = db.get_test(r["id"])
        labels = [item["label"] for item in test["items"]]
        for k in range(6):
            wrong = labels[k * 4:k * 4 + 3 + k * 2]
            db.save_attempt(test["id"], 1000 + k, f"O'quvchi {k}", "A", {},
                            {"test_correct": 30 - k, "written_correct": 5, "auto_total": 44, "wrong": wrong, "points": 40 - k})
        self.bot.documents.clear()
        status, res = await self.post(f"/api/teacher/test/{test['id']}/send", {"kind": "rating"})
        self.assertEqual(status, 200)
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.bot.photos), 2)  # reyting + statistika
        self.assertTrue(all(name.endswith(".png") for _, name in self.bot.photos))
        self.assertEqual([n for _, n in self.bot.documents], [f"reyting_{test['code']}.pdf"])
        images = rating.rating_images(test, db.list_attempts(test["id"]))
        self.assertEqual(images[0][1:4], b"PNG")
        self.assertTrue(rating.rating_pdf(images).startswith(b"%PDF"))

    async def test_rating_without_results(self):
        _, r = await self.create("online", fmt="milliy")
        self.assertEqual(rating.rating_images(db.get_test(r["id"]), []), [])


class KeyFix(ServerBase):
    """Kalit tuzatilsa — hamma natija qayta tekshiriladi, test egasiga xabar boradi."""

    async def test_key_change_regrades_attempts(self):
        _, r = await self.create("online")
        test = db.get_test(r["id"])
        key = db.get_keys(test["id"])["A"]  # hammasi "A"
        answers = {str(i): "A" for i in range(1, 6)}
        answers["3"] = "B"
        res = structure.grade(test["items"], key, answers)
        attempt = db.save_attempt(test["id"], 777, "Ali", "A", answers, res)
        self.assertEqual(attempt["test_correct"], 4)
        sent = []
        webapp.HOOKS["send_text"] = lambda chat_id, text: asyncio.sleep(0, result=sent.append((chat_id, text)))

        status, page = await self.get(f"/api/teacher/test/{test['id']}/keys")
        self.assertEqual(status, 200)
        self.assertEqual(page["attempts"], 1)
        new = {**key, "3": "B"}  # 3-savol kaliti noto'g'ri edi
        status, out = await self.post(f"/api/teacher/test/{test['id']}/keys", {"keys": {"A": new}})
        self.assertEqual(status, 200)
        self.assertEqual(out["edited"], ["3"])
        self.assertEqual((out["total"], out["changed"]), (1, 1))
        fresh = db.get_attempt(attempt["id"])
        self.assertEqual(fresh["test_correct"], 5)
        self.assertEqual(fresh["wrong"], [])
        self.assertEqual(db.get_keys(test["id"])["A"]["3"], "B")
        await asyncio.sleep(0.05)
        self.assertEqual(sent[0][0], OWNER)  # xabar test egasiga
        self.assertIn("1 ta natija qayta tekshirildi", sent[0][1])

        status, out = await self.post(f"/api/teacher/test/{test['id']}/keys", {"keys": {"A": new}})
        self.assertEqual(out["edited"], [])  # o'zgarmagan — qayta tekshirilmaydi
        status, _ = await self.post(f"/api/teacher/test/{test['id']}/keys", {"keys": {"A": {**new, "2": "Z"}}})
        self.assertEqual(status, 400)


class EssayCode(unittest.IsolatedAsyncioTestCase):
    """Ustozning qisqa esse kodi: o'quvchi botga yozsa — ustozga ulanadi (esselari ustoz limitidan)."""

    async def asyncSetUp(self):
        _env.fresh_db()

    def message(self, text, user_id=OTHER):
        replies, sent = [], []

        async def answer(value, **_):
            replies.append(value)

        async def send_message(chat_id, value, **_):
            sent.append((chat_id, value))

        user = types.SimpleNamespace(id=user_id, full_name="Ali Valiyev", username="ali")
        msg = types.SimpleNamespace(text=text, from_user=user, answer=answer,
                                    bot=types.SimpleNamespace(send_message=send_message))
        return msg, replies, sent

    async def test_code_is_stable_and_links_student(self):
        code = db.essay_code_of(OWNER)
        self.assertRegex(code, r"^E\d{5}$")
        self.assertEqual(db.essay_code_of(OWNER), code)  # doimiy
        self.assertNotEqual(db.essay_code_of(OTHER), code)
        self.assertEqual(db.teacher_of_essay_code(code), OWNER)
        self.assertEqual(db.teacher_of_essay_code("e " + code[1:]), OWNER)

        state = types.SimpleNamespace(clear=lambda: asyncio.sleep(0))
        msg, replies, sent = self.message(code.lower())
        await bot.fallback(msg, state)
        self.assertTrue(db.is_linked(OWNER, OTHER))
        self.assertIn("ulandingiz", replies[0])
        self.assertEqual(sent[0][0], OWNER)  # ustozga xabar

        msg, replies, _ = self.message("E00000")
        await bot.fallback(msg, state)
        self.assertIn("Bunday esse kodi yo'q", replies[0])

        msg, replies, _ = self.message(code, user_id=OWNER)  # ustoz o'z kodini yozdi
        await bot.fallback(msg, state)
        self.assertIn("o'zingizniki", replies[0])


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
        db.set_sheet_style(r["id"], "standard")  # eski titul: esse botda (yangi oflayn test — qog'ozda)
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
        # Sotish yopiq: to'langan obuna muddati tugaguncha ishlaydi, obunasiz o'qituvchi esa yarata olmaydi
        status, _ = await self.create()
        self.assertEqual(status, 200)  # OWNER'da to'langan oylik obuna bor (asyncSetUp)
        db.set_subscription(OWNER, "month", (plans.now() - timedelta(days=1)).isoformat(timespec="seconds"))
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
        db.link_student(OWNER, STUDENT)  # ustozning o'quvchisi — bepul
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


class EssayScore75(ServerBase):
    async def test_teacher_enters_75_scale(self):
        _, r = await self.create("offline", fmt="milliy")
        test = db.get_test(r["id"])
        attempt = await bot.record_attempt(self.bot, test, db.offline_tg_id(test["id"], 3), "Qog'oz", "A", {}, None, [], 0.0)
        path = f"/api/teacher/essay/{attempt['id']}"
        status, res = await self.post(path, {"score75": "51"})
        self.assertEqual((status, res["score"], res["score_75"]), (200, 12.0, 51.0))
        self.assertEqual(db.get_attempt(attempt["id"])["essay"], 12.0)  # Rasch shundan
        self.assertEqual((await self.post(path, {"score75": 0}))[1]["score"], 0.0)
        for bad in ("27", "76", "50,5", "abc"):
            self.assertEqual((await self.post(path, {"score75": bad}))[0], 400, bad)
        self.assertEqual((await self.post(path, {"score": "18,5"}))[1]["score_75"], 64.0)  # 24 ballik ham ishlaydi


class OfficialSheet(ServerBase):
    """Imtihon varaqasi: 1-varaq 1–35, 2-varaq 36–44 alohida skanerlanadi; esse qog'ozda — bahosi kiritilmaguncha natija yo'q."""

    async def make(self):
        _, r = await self.create("offline", fmt="milliy")
        test = db.get_test(r["id"])
        self.assertEqual(test["sheet_style"], "official")
        db.add_students(test["id"], ["Ali Valiyev", "Vali Karimov"], ["A", "A"], [7, 8])
        return test

    async def upload(self, test, payload, written=None):
        from aiohttp import FormData
        form = FormData()
        form.add_field("data", json.dumps(payload))
        if written:
            form.add_field("written", written, filename="y.jpg", content_type="image/jpeg")
        response = await self.client.post(f"/api/teacher/test/{test['id']}/scan/upload", data=form)
        return response.status, await response.json()

    async def test_pdf_has_four_pages_per_student(self):
        import re
        import sheet
        test = await self.make()
        pdf = sheet.sheets_pdf(test, ["A"], db.list_students(test["id"]), 0)
        self.assertEqual(len(re.findall(rb"/Type /Page[^s]", pdf)), 8)  # 2 o'quvchi × 4 varaq
        self.assertEqual(sheet.layout(test, ["A"])["pages"], 2)

    async def test_test_and_essay_sheets_sent_as_separate_files(self):
        test = await self.make()
        fake = FakeBot()
        await bot.send_sheets(fake, OWNER, test, db.list_students(test["id"]), 0)
        self.assertEqual([name for _, name in fake.documents],
                         [f"titullar_{test['code']}.pdf", f"esse_{test['code']}.pdf"])
        status, res = await self.post(f"/api/teacher/test/{test['id']}/download", {"kind": "essay"})
        self.assertEqual(status, 200)
        response = await self.client.get("/dl/" + res["url"].split("/dl/")[1])
        self.assertIn(f"esse_{test['code']}.pdf", response.headers["Content-Disposition"])

    async def test_page2_before_and_after_page1(self):
        test = await self.make()
        answers = {str(i): "A" for i in range(1, 36)}
        # 7: avval 2-varaq, keyin 1-varaq
        status, r = await self.upload(test, {"uid": "p2-7", "page": 2, "number": 7}, b"\xff\xd8 yozma7")
        self.assertEqual((status, r["attached"]), (200, False))
        status, r = await self.upload(test, {"uid": "p1-7", "number": "7", "variant": "A", "answers": answers})
        self.assertEqual(status, 200)
        self.assertIsNotNone(db.get_written_job(r["attempt_id"]))  # kutib turgan 2-varaq biriktirildi
        self.assertIsNone(r["points"])  # esse bahosi yo'q — ball ko'rsatilmaydi
        # 8: avval 1-varaq, keyin 2-varaq
        _, r8 = await self.upload(test, {"uid": "p1-8", "number": "8", "variant": "A", "answers": answers})
        self.assertIsNone(db.get_written_job(r8["attempt_id"]))
        _, r = await self.upload(test, {"uid": "p2-8", "page": 2, "number": 8}, b"\xff\xd8 yozma8")
        self.assertTrue(r["attached"])
        self.assertIsNotNone(db.get_written_job(r8["attempt_id"]))
        # 1-varaq qayta skanerlansa — 2-varaq rasmi yo'qolmaydi
        _, again = await self.upload(test, {"uid": "p1-8b", "number": "8", "variant": "A", "answers": answers})
        self.assertNotEqual(again["attempt_id"], r8["attempt_id"])
        self.assertIsNotNone(db.get_written_job(again["attempt_id"]))
        # ID'siz 2-varaq — xato
        status, _ = await self.upload(test, {"uid": "p2-x", "page": 2}, b"\xff\xd8 x")
        self.assertEqual(status, 400)

    async def test_result_hidden_until_essay_score(self):
        test = await self.make()
        answers = {str(i): "A" for i in range(1, 36)}
        _, r = await self.upload(test, {"uid": "a", "number": "7", "variant": "A", "answers": answers})
        page = await (await self.client.get(f"/api/teacher/test/{test['id']}")).json()
        row = page["attempts"][0]
        self.assertEqual((row["essay_pending"], row["rasch"], row["grade"], page["essays_missing"]), (True, None, None, 1))
        status, res = await self.post(f"/api/teacher/test/{test['id']}/announce", {})
        self.assertEqual((status, res["essays_missing"]), (409, 1))
        self.assertEqual((await self.post(f"/api/teacher/test/{test['id']}/certificates", {}))[0], 400)
        import excel
        import pdfs
        self.assertTrue(pdfs.results_pdf(test, db.list_attempts(test["id"])).startswith(b"%PDF"))  # «kutilmoqda» bilan
        excel.results_xlsx(test, db.list_attempts(test["id"]))
        # O'qituvchi esse balini kiritadi — natija chiqadi
        await self.post(f"/api/teacher/essay/{r['attempt_id']}", {"score75": 63})
        page = await (await self.client.get(f"/api/teacher/test/{test['id']}")).json()
        row = page["attempts"][0]
        self.assertFalse(row["essay_pending"])
        self.assertIsNotNone(row["rasch"])
        self.assertEqual(page["essays_missing"], 0)
        status, _ = await self.post(f"/api/teacher/test/{test['id']}/announce", {})
        self.assertEqual(status, 200)

    async def test_results_page_with_pending_essay(self):
        """Esse bahosi kutilayotgan natija bor — «📊 Natijalar» ochiladi (o'rtacha faqat bor ballardan)."""
        test = await self.make()
        answers = {str(i): "A" for i in range(1, 36)}
        _, r7 = await self.upload(test, {"uid": "m7", "number": "7", "variant": "A", "answers": answers})
        await self.upload(test, {"uid": "m8", "number": "8", "variant": "A", "answers": answers})
        status, res = await self.get("/api/teacher/results")
        self.assertEqual(status, 200, res)
        self.assertIsNone(res["tests"][0]["avg"])  # ikkalasida ham esse bahosi yo'q
        await self.post(f"/api/teacher/essay/{r7['attempt_id']}", {"score75": 63})
        status, res = await self.get("/api/teacher/results")
        self.assertEqual(status, 200, res)
        self.assertIsNotNone(res["tests"][0]["avg"])

    async def test_rescan_keeps_essay_score(self):
        """1-varaq qayta skanerlansa — o'qituvchi kiritgan esse bahosi yo'qolmaydi (natija yopilib qolmaydi)."""
        test = await self.make()
        answers = {str(i): "A" for i in range(1, 36)}
        _, first = await self.upload(test, {"uid": "s1", "number": "7", "variant": "A", "answers": answers})
        await self.post(f"/api/teacher/essay/{first['attempt_id']}", {"score75": 63})
        _, again = await self.upload(test, {"uid": "s2", "number": "7", "variant": "A", "answers": {**answers, "1": "B"}})
        self.assertNotEqual(again["attempt_id"], first["attempt_id"])
        self.assertFalse(again["essay_pending"])
        self.assertIsNotNone(again["points"])
        self.assertEqual(webapp.essay_to_75(db.get_attempt(again["attempt_id"])["essay"]), 63)

    async def test_paid_check_queues_written_from_phone_scanner(self):
        """«💎 Pullik tekshiruv»: telefon skaneridan kelgan yozma kataklar ham AI navbatiga (kreditdan), «qo'lda» emas."""
        _, r = await self.create("offline", fmt="milliy")
        db.set_sheet_style(r["id"], "standard")  # bitta varaqli titul: yozma kataklar 1-varaqda
        db.set_check_mode(r["id"], "paid")
        test = db.get_test(r["id"])
        db.add_students(test["id"], ["Ali Valiyev"], ["A"], [7])
        db.add_credits(OWNER, 2)
        paid, essay_ai.paid_enabled = essay_ai.paid_enabled, lambda: True
        try:
            _, res = await self.upload(test, {"uid": "w1", "number": "7", "variant": "A",
                                              "answers": {str(i): "A" for i in range(1, 36)}}, b"\xff\xd8 yozma")
        finally:
            essay_ai.paid_enabled = paid
        self.assertTrue(res["written_queued"])
        self.assertEqual(db.get_written_job(res["attempt_id"])["status"], "pending")

    async def test_no_bot_essay_for_paper_test(self):
        test = await self.make()
        self.user = {"id": STUDENT, "name": "Ali", "username": None}
        self.assertEqual((await self.client.get(f"/api/test/{test['code']}")).status, 409)
        status, _ = await self.post(f"/api/test/{test['code']}/essay", {"number": "0007", "name": "Ali Valiyev", "essay": "so'z " * 40})
        self.assertEqual(status, 409)


class TeacherCredits(EssaySelfCheck):
    """Obuna yopiq: o'quvchi essesini AI tekshirishi o'qituvchining kreditidan (admin /kredit bilan to'ldiradi)."""

    # EssaySelfCheck sozlamasi (soxta AI) kerak, uning testlari emas — ular cheksiz AI'ni tekshiradi
    test_student_sees_score_and_it_goes_to_teacher_and_rasch = None
    test_paper_essay_still_needs_teacher = None

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._sales = (plans.SALES_ENABLED, webapp.SALES_ENABLED)
        plans.SALES_ENABLED = webapp.SALES_ENABLED = False
        plans.set_access(OWNER, True)  # botda ruxsat berilgan o'qituvchi (FREE_IDS emas)

    async def asyncTearDown(self):
        plans.SALES_ENABLED, webapp.SALES_ENABLED = self._sales
        await super().asyncTearDown()

    async def submit(self, test, uid):
        db.link_student(OWNER, uid)  # ustozning o'quvchisi — bepul
        self.user = {"id": uid, "name": "O'quvchi", "username": None}
        status, sub = await self.post(f"/api/test/{test['code']}/submit",
                                      {"variant": "A", "name": f"O'quvchi {uid}", "answers": {"1": "A"}, "essay": "So'z " * 80})
        self.assertEqual(status, 200)
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        return db.get_essay(sub["attempt_id"])["status"]

    async def test_essays_spend_teacher_credits(self):
        _, r = await self.create(fmt="milliy")
        test = db.get_test(r["id"])
        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertEqual(boot["ai_credits"], 0)
        # Kredit yo'q — AI tekshirmaydi, o'qituvchiga xabar
        self.bot.messages.clear()
        self.assertEqual(await self.submit(test, 501), "manual")
        self.assertTrue(any(c == OWNER and "krediti tugadi" in t for c, t in self.bot.messages))
        # Admin 2 kredit qo'shadi
        replies, sent = [], []

        class Msg:
            def __init__(self, uid):
                self.from_user = types.SimpleNamespace(id=uid)
                self.bot = types.SimpleNamespace(send_message=lambda chat, text, **k: asyncio.sleep(0, sent.append((chat, text))))

            async def answer(self, text, **_):
                replies.append(text)
        bot.ADMIN_IDS.add(999)
        try:
            await bot.cmd_credit(Msg(999), types.SimpleNamespace(command="kredit", args=f"{OWNER} 2"))
        finally:
            bot.ADMIN_IDS.discard(999)
        self.assertEqual(db.get_credits(OWNER), 2)
        self.assertTrue(any(c == OWNER and "+2" in t for c, t in sent))
        # Ikkita esse — AI tekshiradi, kredit yechiladi; uchinchisi — yana qo'lda
        self.assertEqual(await self.submit(test, 502), "pending")
        self.assertEqual(await self.submit(test, 503), "pending")
        self.assertEqual(db.get_credits(OWNER), 0)
        self.assertEqual(await self.submit(test, 504), "manual")
        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertEqual(boot["ai_credits"], 0)

    async def test_failed_essay_refunds_teacher_credit(self):
        """AI esseni tekshira olmadi — ustozning yechilgan krediti qaytadi; «qayta urinish»dan keyin yana bo'lmasa —
        ikkinchi marta qaytmaydi."""
        _, r = await self.create(fmt="milliy")
        test = db.get_test(r["id"])
        db.add_credits(OWNER, 1)
        self.assertEqual(await self.submit(test, 601), "pending")
        self.assertEqual(db.get_credits(OWNER), 0)

        async def broken(*_a, **_k):
            raise self.essay_ai.AIError("Buzilgan javob", retry=False)
        self.essay_ai.grade = broken
        essay = db.pending_essays(5)[0]
        await bot.grade_one(self.bot, essay)
        self.assertEqual(db.get_essay(essay["attempt_id"])["status"], "failed")
        self.assertEqual(db.get_credits(OWNER), 1)  # kredit qaytdi
        db.retry_essay(essay["attempt_id"])  # ustoz «qayta urinish» — kredit yechilmaydi
        await bot.grade_one(self.bot, db.pending_essays(5)[0])
        self.assertEqual(db.get_credits(OWNER), 1)  # ikkinchi marta qaytmadi

    async def test_admin_and_free_ids_unlimited(self):
        self.assertTrue(plans.ai_unlimited(next(iter(plans.ADMIN_IDS), 0)) if plans.ADMIN_IDS else True)
        self.assertFalse(plans.ai_unlimited(OWNER))  # /ruxsat — test yaratadi, lekin AI kreditdan
        self.assertTrue(plans.is_free(OWNER))


class LinkedPractice(TeacherCredits):
    """Ustozga ulangan o'quvchi test bo'lmasa ham esse tekshirtiradi — ustoz kreditidan, oylik chegara bilan."""

    test_essays_spend_teacher_credits = None
    test_admin_and_free_ids_unlimited = None
    test_failed_essay_refunds_teacher_credit = None

    async def practice(self, uid, words=80):
        self.user = {"id": uid, "name": "O'quvchi", "username": None}
        status, res = await self.post("/api/practice", {"teacher_id": OWNER, "topic": "Kitob", "essay": "So'z " * words})
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        return status, res

    async def test_invite_and_add_link_students(self):
        import bot as botmod
        code = webapp.invite_code(OWNER)
        self.assertEqual(webapp.teacher_from_invite(code), OWNER)
        self.assertIsNone(webapp.teacher_from_invite(code[:-1] + ("0" if code[-1] != "0" else "1")))  # soxta imzo
        replies = []

        class Msg:
            from_user = types.SimpleNamespace(id=STUDENT, full_name="Ali Valiyev")
            bot = types.SimpleNamespace(send_message=lambda chat, text, **k: asyncio.sleep(0))

            async def answer(self, text, **_):
                replies.append(text)
        await botmod.join_teacher(Msg(), code)
        self.assertTrue(db.is_linked(OWNER, STUDENT))
        # Ustoz @username bilan qo'shadi (o'quvchi botga /start bosgan)
        db.remember_user(OTHER, "Vali Karimov", "vali_k")
        status, view = await self.post("/api/teacher/link", {"ref": "@vali_k"})
        self.assertEqual(status, 200)
        self.assertEqual({s["id"] for s in view["students"]}, {STUDENT, OTHER})
        self.assertEqual((await self.post("/api/teacher/link", {"ref": "@yoq_odam"}))[0], 404)
        _, view = await self.post("/api/teacher/link/remove", {"student_id": OTHER})
        self.assertEqual([s["id"] for s in view["students"]], [STUDENT])

    async def test_practice_spends_credit_with_monthly_limit(self):
        db.link_student(OWNER, STUDENT)
        self.assertEqual((await self.practice(STUDENT))[0], 402)  # kredit yo'q
        plans.add_pack(OWNER, 5)
        await self.post("/api/teacher/link/limit", {"limit": 2})
        self.assertEqual((await self.practice(STUDENT, words=10))[0], 400)  # juda qisqa
        status, res = await self.practice(STUDENT)
        self.assertEqual(status, 200)
        self.assertEqual(db.get_credits(OWNER), 4)
        for item in db.pending_practice(5):
            await bot.grade_practice(self.bot, item)
        self.user = {"id": STUDENT, "name": "Ali", "username": None}
        result = await (await self.client.get(f"/api/practice/{res['id']}")).json()
        self.assertEqual(result["status"], "graded")
        self.assertIsNotNone(result["score_75"])
        self.assertTrue(any(c == STUDENT and "tekshirildi" in t for c, t in self.bot.messages))
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        self.assertEqual((await self.practice(STUDENT))[0], 200)
        self.assertEqual((await self.practice(STUDENT))[0], 429)  # oylik chegara 2
        self.assertEqual(db.get_credits(OWNER), 3)
        self.assertEqual((await self.practice(OTHER))[0], 403)  # ulanmagan

    async def test_failed_practice_refunds_credit(self):
        db.link_student(OWNER, STUDENT)
        plans.add_pack(OWNER, 1)
        await self.practice(STUDENT)
        self.assertEqual(db.get_credits(OWNER), 0)

        async def broken(*_a, **_k):
            raise self.essay_ai.AIError("Buzilgan javob", retry=False)
        self.essay_ai.grade = broken
        for item in db.pending_practice(5):
            await bot.grade_practice(self.bot, item)
        self.assertEqual(db.get_credits(OWNER), 1)  # tekshirilmadi — kredit qaytdi

    async def test_teacher_buys_credits_himself(self):
        db.set_setting("card_number", "8600 1234 5678 9012")
        db.set_setting("price_pack", 50000)
        db.set_setting("pack_essays", 100)
        status, order = await self.post("/api/plan/order", {"period": "pack", "count": 2})
        self.assertEqual(status, 200)
        self.assertEqual((order["payment"]["amount"], order["payment"]["essays"]), (100000, 200))
        payment = db.get_payment(order["payment"]["id"])
        db.attach_receipt(payment["id"], "file-1")
        self.assertTrue(db.decide_payment(payment["id"], "paid", 999))
        plans.add_pack(OWNER, payment["essays"])
        self.assertEqual(db.get_credits(OWNER), 200)
        self.assertEqual((await self.post("/api/plan/order", {"period": "month"}))[0], 403)  # obuna yopiq


class EssayPaid(ServerBase):
    """Test hammaga bepul. Ustoz mock yaratishda begonalar uchun esse narxini qo'yadi; begona esseni yuklashdan oldin
    ustoz kartasiga to'lab, chekni botga yuboradi — AI tekshiradi. Ustozning o'quvchisi — kreditdan, tugasa ogohlantirish."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        db.set_setting("platform_share", "0")  # eski tartib: begonalar puli ustoz kartasiga (yangisi — RevenueShare)
        self._enabled, self._essay_enabled, self._read = essay_ai.enabled, essay_ai.essay_enabled, essay_ai.read_receipt
        essay_ai.enabled = essay_ai.essay_enabled = lambda: True
        self.receipt = {}

        async def fake_read(image, mime="image/jpeg"):
            return dict(self.receipt)
        essay_ai.read_receipt = fake_read
        self.notices = []

        async def notice(*args):
            self.notices.append(args)
        webapp.HOOKS["essay_receipt_notice"] = notice
        webapp.HOOKS["bot_username"] = lambda: asyncio.sleep(0, "test_bot")

    async def asyncTearDown(self):
        essay_ai.enabled, essay_ai.essay_enabled, essay_ai.read_receipt = self._enabled, self._essay_enabled, self._read
        await super().asyncTearDown()

    async def as_student(self, uid):
        self.user = {"id": uid, "name": f"O'quvchi {uid}", "username": f"u{uid}"}

    async def as_owner(self):
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}

    def good_receipt(self, **kw):
        self.receipt = {"is_receipt": True, "success": True, "amount": 15000, "date": plans.now().strftime("%Y-%m-%d"),
                        "time": "10:00", "recipient_card": "8600 **** **** 9012", "recipient_name": "NODIRA KARIMOVA",
                        "transaction_id": "TX1", "looks_edited": False, "note": "", **kw}

    async def paid_mock(self, price=15000):
        """Ustoz milliy onlayn mock yaratadi: begonalar uchun esse narxi va kartasi bilan."""
        await self.as_owner()
        items = structure.build_items("milliy", 45)
        key = {i["label"]: ("A" if i["kind"] == "choice" else "kitob") for i in items}
        status, r = await self.post("/api/teacher/tests", {"title": "Mock", "fmt": "milliy", "mode": "online", "keys": {"A": key},
                                                           "channels": [], "essay_price": price, "card": "8600 1234 5678 9012",
                                                           "holder": "Nodira Karimova"})
        self.assertEqual(status, 200, r)
        return db.get_test(r["id"])

    async def info(self, test):
        response = await self.client.get(f"/api/test/{test['code']}")
        return response.status, await response.json()

    async def submit(self, test, essay=""):
        return await self.post(f"/api/test/{test['code']}/submit", {"variant": "A", "name": "Begona Bola", "answers": {}, "essay": essay})

    ESSAY = "Vatan — bu ona. " * 30

    async def test_price_validation_and_card_saved(self):
        items = structure.build_items("milliy", 45)
        key = {i["label"]: ("A" if i["kind"] == "choice" else "kitob") for i in items}
        base = {"title": "Mock", "fmt": "milliy", "mode": "online", "keys": {"A": key}, "channels": []}
        self.assertEqual((await self.post("/api/teacher/tests", {**base, "essay_price": 15000}))[0], 400)  # kartasiz
        self.assertEqual((await self.post("/api/teacher/tests", {**base, "essay_price": 50, "card": "8600123412341234"}))[0], 400)
        test = await self.paid_mock()
        self.assertEqual(test["price"], 15000)
        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertEqual(boot["my_card"], {"number": "8600 1234 5678 9012", "holder": "Nodira Karimova"})

    async def test_free_without_price(self):
        test = await self.paid_mock(price=0)
        await self.as_student(OTHER)
        status, data = await self.info(test)
        self.assertEqual((status, data["essay_pay"]), (200, None))
        self.assertFalse(await webapp.essay_ai_ok(test, OTHER))  # begona, to'lamagan — ustoz krediti ishlatilmaydi

    async def test_outsider_pays_before_essay(self):
        test = await self.paid_mock()
        await self.as_student(OTHER)
        status, data = await self.info(test)  # mock ochiladi, test bepul
        self.assertEqual((status, data["essay_pay"]["price"], data["essay_pay"]["card"]), (200, 15000, "8600 1234 5678 9012"))
        status, res = await self.submit(test, self.ESSAY)
        self.assertEqual((status, res["essay_pay"]["price"]), (402, 15000))  # esse — to'lovdan keyin
        status, res = await self.post(f"/api/test/{test['code']}/essay-pay", {})
        self.assertEqual((status, res["status"], res["bot"]), (200, "open", "test_bot"))
        self.assertIn("8600 1234 5678 9012", self.bot.messages[-1][1])
        self.good_receipt()
        credits = db.get_credits(OWNER)
        result = await webapp.check_essay_receipt(db.open_essay_payment(OTHER)["id"], b"chek-1", "image/jpeg")
        self.assertEqual(result["status"], "paid")
        self.assertIn("To'landi", self.bot.messages[-1][1])
        self.assertTrue(self.notices[-1][1])
        self.assertIsNone((await self.info(test))[1]["essay_pay"])  # esse maydoni ochildi
        self.assertTrue(await webapp.essay_ai_ok(test, OTHER))
        self.assertEqual((await self.submit(test, self.ESSAY))[0], 200)
        self.assertEqual(db.get_credits(OWNER), credits)  # ustoz krediti ishlatilmadi

    async def test_failed_essay_refunds_student_credit(self):
        """Begona o'quvchi essesi 1 kreditidan: AI tekshira olmasa — kredit o'quvchiga qaytadi va xabar boradi."""
        test = await self.paid_mock()
        db.wallet_add(OWNER, OTHER, 1)
        await self.as_student(OTHER)
        status, res = await self.submit(test, self.ESSAY)
        self.assertEqual(status, 200, res)
        self.assertEqual((db.get_essay(res["attempt_id"])["status"], db.wallet_balance(OWNER, OTHER)), ("pending", 0))
        saved = essay_ai.grade

        async def broken(*_a, **_k):
            raise essay_ai.AIError("o'qib bo'lmadi", retry=False)
        essay_ai.grade = broken
        try:
            await bot.grade_one(self.bot, db.pending_essays(5)[0])
        finally:
            essay_ai.grade = saved
        self.assertEqual(db.wallet_balance(OWNER, OTHER), 1)
        self.assertTrue(any(c == OTHER and "kreditingiz qaytarildi" in t for c, t in self.bot.messages))

    async def test_outsider_can_submit_without_essay(self):
        test = await self.paid_mock()
        await self.as_student(OTHER)
        self.assertEqual((await self.submit(test))[0], 200)

    async def test_bad_receipt_goes_to_teacher(self):
        test = await self.paid_mock()
        test2 = await self.paid_mock()
        await self.as_student(OTHER)
        await self.post(f"/api/test/{test['code']}/essay-pay", {})
        pay = db.open_essay_payment(OTHER)
        old = (plans.now() - __import__("datetime").timedelta(days=2)).strftime("%Y-%m-%d")
        self.good_receipt(amount=5000, date=old, recipient_card="9860 **** 1111", recipient_name="Boshqa Odam")
        result = await webapp.check_essay_receipt(pay["id"], b"chek-2", "image/jpeg")
        self.assertEqual((result["status"], len(result["reasons"])), ("review", 3))  # summa, sana, karta
        self.assertIn("kutib turing", self.bot.messages[-1][1])
        self.assertEqual((await self.info(test))[1]["essay_pay"]["status"], "review")
        with self.assertRaises(webapp.ApiError):
            await webapp.decide_essay_payment(pay["id"], True, STUDENT)  # begona ustoz qaror qila olmaydi
        await webapp.decide_essay_payment(pay["id"], True, OWNER)  # mock egasi tasdiqladi
        self.assertIsNone((await self.info(test))[1]["essay_pay"])
        with self.assertRaises(webapp.ApiError):
            await webapp.decide_essay_payment(pay["id"], True, OWNER)
        await self.as_student(OTHER)
        await self.post(f"/api/test/{test2['code']}/essay-pay", {})
        pay2 = db.open_essay_payment(OTHER)
        self.good_receipt()
        result = await webapp.check_essay_receipt(pay2["id"], b"chek-2", "image/jpeg")  # o'sha rasm
        self.assertEqual((result["status"], result["reasons"]), ("review", ["bu chek oldin ishlatilgan"]))
        await webapp.decide_essay_payment(pay2["id"], False, OWNER)
        self.assertEqual(db.open_essay_payment(OTHER)["status"], "rejected")
        self.good_receipt(recipient_card="", transaction_id="TX9")  # karta ko'rinmasa ham ism mos
        self.assertEqual((await webapp.check_essay_receipt(pay2["id"], b"chek-4", "image/jpeg"))["status"], "paid")

    async def test_linked_student_credit_warning(self):
        test = await self.paid_mock()
        db.link_student(OWNER, STUDENT)
        await self.as_student(STUDENT)
        self.assertEqual((await self.post(f"/api/test/{test['code']}/essay-pay", {}))[0], 409)  # to'lov kerak emas
        status, data = await self.info(test)
        self.assertIsNone(data["essay_pay"])
        self.assertIsNone(data["essay_warning"])  # obunaning oylik limiti bor — AI tekshiradi (kredit bo'lmasa ham)
        db.set_setting("ai_essays", 0)  # oylik limit tugadi, kredit yo'q
        self.assertIn("limiti tugagan", (await self.info(test))[1]["essay_warning"])
        self.assertFalse(await webapp.essay_ai_ok(test, STUDENT))
        db.add_credits(OWNER, 3)
        self.assertIsNone((await self.info(test))[1]["essay_warning"])
        self.assertTrue(await webapp.essay_ai_ok(test, STUDENT))

    async def test_wallet_credit_opens_mock_essay(self):
        """Begona o'quvchi ustozdan kredit paketi oladi: mock essesi chek so'ramaydi, 1 kredit yechiladi."""
        test = await self.paid_mock()
        status, r = await self.post("/api/teacher/packs", {"price": 5000, "packs": [{"essays": 5, "price": 20000}]})
        self.assertEqual(status, 200, r)
        self.assertEqual([(p["essays"], p["discount"]) for p in r["packs"]], [(1, 0), (5, 20)])
        # Ilova: «Kredit — Narxi» qatorlari; 1 kreditlik qator — 1 esse narxi, narxsiz qator saqlanmaydi
        status, r = await self.post("/api/teacher/packs", {"packs": [
            {"essays": 1, "price": 12000}, {"essays": 2, "price": ""}, {"essays": 5, "price": 50000}, {"essays": 50, "price": 400000}]})
        self.assertEqual(status, 200, r)
        self.assertEqual([(p["essays"], p["price"], p["discount"]) for p in r["packs"]],
                         [(1, 12000, 0), (5, 50000, 17), (50, 400000, 33)])
        self.assertEqual((await self.post("/api/teacher/packs", {"packs": [{"essays": "", "price": 9000}]}))[0], 400)
        self.assertEqual((await self.post("/api/teacher/packs", {"packs": [{"essays": 1, "price": 1}, {"essays": 1, "price": 2}]}))[0], 400)
        await self.post("/api/teacher/packs", {"price": 5000, "packs": [{"essays": 5, "price": 20000}]})

        await self.as_student(OTHER)
        _, info = await self.info(test)
        self.assertEqual([p["essays"] for p in info["essay_pay"]["packs"]], [1, 5])
        status, order = await self.post(f"/api/test/{test['code']}/wallet-order", {"essays": 5})
        self.assertEqual((status, order["payment"]["amount"]), (200, 20000), order)
        self.assertEqual((await self.post(f"/api/test/{test['code']}/wallet-order", {"essays": 3}))[0], 409)  # bunday paket yo'q

        await self.post(f"/api/teacher/wallet-payment/{order['payment']['id']}", {"action": "approve"})  # begona — 404
        self.assertEqual(db.wallet_balance(OWNER, OTHER), 0)
        await self.as_owner()
        status, _ = await self.post(f"/api/teacher/wallet-payment/{order['payment']['id']}", {"action": "approve"})
        self.assertEqual((status, db.wallet_balance(OWNER, OTHER)), (200, 5))
        self.assertEqual((await self.post(f"/api/teacher/wallet-payment/{order['payment']['id']}", {"action": "approve"}))[0], 409)

        await self.as_student(OTHER)
        _, info = await self.info(test)
        self.assertIsNone(info["essay_pay"])
        self.assertEqual(info["wallet"], {"balance": 5})
        status, res = await self.submit(test, self.ESSAY)
        self.assertEqual(status, 200, res)
        self.assertEqual(db.wallet_balance(OWNER, OTHER), 4)
        self.assertEqual(db.get_essay(res["attempt_id"])["status"], "pending")  # AI tekshiradi

    async def test_pack_needs_active_subscription(self):
        """Obuna tugagan: paketdagi kredit ishlatilmaydi — can_use_ai ham «yo'q» deydi (use_ai bilan bir xil)."""
        db.add_credits(OWNER, 3)
        self.assertTrue(plans.can_use_ai(OWNER))
        db.set_subscription(OWNER, "month", (plans.now() - timedelta(days=1)).isoformat(timespec="seconds"))
        self.assertFalse(plans.can_use_ai(OWNER))
        self.assertFalse(plans.use_ai(OWNER, "essays"))

    async def test_bot_photo_handler(self):
        """Botga rasm: ochiq esse to'lovi bo'lsa — yuklab oladi va AI tekshiradi; to'g'ri bo'lsa — paid."""
        import io
        test = await self.paid_mock()
        await self.as_student(OTHER)
        await self.post(f"/api/test/{test['code']}/essay-pay", {})
        self.good_receipt()
        answers = []

        class Sent:
            async def delete(self):
                pass

        async def answer(text, **_):
            answers.append(text)
            return Sent()

        async def download(file_id):
            return io.BytesIO(b"bot-chek")
        message = types.SimpleNamespace(
            from_user=types.SimpleNamespace(id=OTHER), photo=[types.SimpleNamespace(file_id="F1")], document=None,
            answer=answer, bot=types.SimpleNamespace(download=download))
        await bot.receipt_received(message)
        self.assertEqual(db.essay_payment_of(test["id"], OTHER)["status"], "paid")
        self.assertIn("To'landi", self.bot.messages[-1][1])
        await bot.receipt_received(message)  # ochiq esse to'lovi yo'q — oddiy obuna chekiga o'tadi
        self.assertIn("Chek qabul qilinmadi", answers[-1])

    async def test_backup_keeps_links_and_payments(self):
        """Zaxira nusxa: ustoz-o'quvchi bog'lanishi, «O'quvchisiman» arizasi, esse to'lovi tiklanadi (rasmlarsiz)."""
        import os
        test = await self.paid_mock()
        db.link_student(OWNER, STUDENT)
        db.save_join_request(OWNER, OTHER, "Vali", "Aliyev", "cmFzbQ==", "image/jpeg")
        await self.as_student(OTHER)
        await self.post(f"/api/test/{test['code']}/essay-pay", {})
        self.good_receipt()
        pay_id = db.open_essay_payment(OTHER)["id"]
        await webapp.check_essay_receipt(pay_id, b"chek-b", "image/jpeg")
        data = db.export_tables()
        self.assertNotIn("receipt", data["essay_payments"][0])  # chek rasmi nusxaga kirmaydi
        self.assertNotIn("photo", data["join_requests"][0])
        original = db.DB_PATH
        db.DB_PATH = os.path.join(_env.TMP, "restored_pay.db")
        try:
            if os.path.exists(db.DB_PATH):
                os.remove(db.DB_PATH)
            db.init()
            db.import_tables(data)
            self.assertTrue(db.is_linked(OWNER, STUDENT))
            self.assertEqual(db.join_status(OWNER, OTHER), "pending")
            self.assertEqual(db.essay_payment_of(test["id"], OTHER)["status"], "paid")
            self.assertTrue(db.receipt_used(__import__("hashlib").sha256(b"chek-b").hexdigest(), None, 0))
        finally:
            db.DB_PATH = original

    # ---------- AI ishlamasa — qo'lda zaxira; AI hisobi ----------

    async def test_receipt_ai_timeout_goes_to_teacher(self):
        test = await self.paid_mock()
        await self.as_student(OTHER)
        await self.post(f"/api/test/{test['code']}/essay-pay", {})

        async def slow(image, mime="image/jpeg"):
            await asyncio.sleep(5)
        essay_ai.read_receipt, timeout = slow, webapp.RECEIPT_AI_TIMEOUT
        webapp.RECEIPT_AI_TIMEOUT = 0.05
        try:
            result = await webapp.check_essay_receipt(db.open_essay_payment(OTHER)["id"], b"chek", "image/jpeg")
        finally:
            webapp.RECEIPT_AI_TIMEOUT = timeout
        self.assertEqual((result["status"], result["reasons"]), ("review", ["AI javob bermadi — ustoz tekshiradi"]))
        self.assertIn("kutib turing", self.bot.messages[-1][1])

    async def test_teacher_sees_payments_in_app(self):
        test = await self.paid_mock()
        await self.as_student(OTHER)
        await self.post(f"/api/test/{test['code']}/essay-pay", {})
        self.good_receipt(amount=100)
        pay_id = db.open_essay_payment(OTHER)["id"]
        await webapp.check_essay_receipt(pay_id, b"chek", "image/jpeg")
        await self.as_student(STUDENT)  # begona ustoz ko'rmaydi
        self.assertEqual((await (await self.client.get("/api/teacher/essay-payments")).json())["payments"], [])
        self.assertEqual((await self.client.get(f"/api/teacher/essay-payment/{pay_id}/receipt")).status, 404)
        self.assertEqual((await self.post(f"/api/teacher/essay-payment/{pay_id}", {"action": "approve"}))[0], 404)
        await self.as_owner()
        listed = (await (await self.client.get("/api/teacher/essay-payments")).json())["payments"]
        self.assertEqual((listed[0]["status"], listed[0]["amount"]), ("review", 15000))
        self.assertIn("summa", listed[0]["note"])
        image = await (await self.client.get(f"/api/teacher/essay-payment/{pay_id}/receipt")).json()
        self.assertTrue(image["image"].startswith("data:image/"))
        self.assertEqual((await self.post(f"/api/teacher/essay-payment/{pay_id}", {"action": "approve"}))[1]["status"], "paid")
        await self.as_student(OTHER)
        self.assertIsNone((await self.info(test))[1]["essay_pay"])

    async def test_receipt_notice_falls_back_to_admin(self):
        test = await self.paid_mock()
        await self.as_student(OTHER)
        await self.post(f"/api/test/{test['code']}/essay-pay", {})
        pay_id = db.open_essay_payment(OTHER)["id"]
        sent = []

        class Tg:
            async def send_photo(self, chat_id, photo, caption=None, **_):
                if chat_id == OWNER:
                    raise RuntimeError("Forbidden: bot was blocked by the user")
                sent.append((chat_id, caption))
        admins, bot.ADMIN_IDS = bot.ADMIN_IDS, {999}
        try:
            await bot.essay_receipt_notice(Tg(), pay_id, False, ["summa kam"], b"chek")
        finally:
            bot.ADMIN_IDS = admins
        self.assertEqual(sent[0][0], 999)
        self.assertIn("Ustozga yetkazilmadi", sent[0][1])

    async def test_ai_down_notice_tells_teachers(self):
        test = await self.paid_mock()
        db.link_student(OWNER, STUDENT)
        await self.as_student(STUDENT)
        status, res = await self.submit(test, self.ESSAY)
        self.assertEqual(status, 200)
        db.create_written_job(res["attempt_id"], test["id"], "aGk=", "image/png", "pending")
        admins, bot.ADMIN_IDS = bot.ADMIN_IDS, {999}
        try:
            await bot.ai_down_notice(self.bot, "Gemini band yoki limit: 429")
        finally:
            bot.ADMIN_IDS = admins
        to_owner = [t for c, t in self.bot.messages if c == OWNER]
        self.assertTrue(to_owner and "Yozmalarni tekshirish" in to_owner[-1] and "Esselar" in to_owner[-1])
        self.assertTrue(any(c == 999 and "429" in t for c, t in self.bot.messages))

    async def test_receipt_hung_model_falls_back_in_time(self):
        """Asosiy Gemini modeli osilib qolsa — chek uchun 20 soniyadan keyin zaxira modelga o'tiladi (90 s ichida)."""
        import aiohttp
        models, timeouts = (essay_ai.GEMINI_MODEL, essay_ai.GEMINI_FALLBACK, essay_ai.GEMINI_API_KEY), []
        good = ('{"is_receipt": true, "success": true, "amount": 15000, "date": "2026-11-21", "time": "", '
                '"recipient_card": "", "recipient_name": "", "transaction_id": "", "looks_edited": false, "note": ""}')

        class Resp:
            status = 200
            async def json(self, content_type=None):
                return {"candidates": [{"content": {"parts": [{"text": good}]}}]}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False

        class Session:
            def __init__(self, timeout):
                timeouts.append(timeout.total)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            def post(self, url, **kw):
                if "hung" in url:
                    raise asyncio.TimeoutError()
                return Resp()
        essay_ai.GEMINI_MODEL, essay_ai.GEMINI_FALLBACK, essay_ai.GEMINI_API_KEY = "hung", ["lite"], "k"
        original = aiohttp.ClientSession
        aiohttp.ClientSession = Session
        try:
            r = await self._read(b"x", "image/jpeg")  # asl read_receipt (setUp soxtasini emas)
        finally:
            aiohttp.ClientSession = original
            essay_ai.GEMINI_MODEL, essay_ai.GEMINI_FALLBACK, essay_ai.GEMINI_API_KEY = models
        self.assertEqual(r["amount"], 15000)
        self.assertEqual(timeouts, [essay_ai.RECEIPT_MODEL_TIMEOUT] * 2)
        self.assertLess(essay_ai.RECEIPT_MODEL_TIMEOUT * 3, webapp.RECEIPT_AI_TIMEOUT)  # 2 ta osilgan model + 1 ta ishlagani

    async def test_ai_usage_recorded(self):
        ready, gemini = essay_ai._ready, essay_ai._gemini
        calls = []

        async def fake(system, text, images, schema):
            if calls:
                raise essay_ai.AIError("Gemini band yoki limit: 429 (m)", retry=True)
            calls.append(1)
            return "{}", {"in": 1000, "out": 200, "cost": 0.0, "model": "gemini-x"}
        essay_ai._ready, essay_ai._gemini = (lambda provider: True), fake
        try:
            await essay_ai._call("s", "t", [], None, provider="gemini", kind="written")
            with self.assertRaises(essay_ai.AIError):
                await essay_ai._call("s", "t", [], None, provider="gemini", kind="written")
        finally:
            essay_ai._ready, essay_ai._gemini = ready, gemini
        rows = db.ai_usage_since("2000-01-01")
        total = {k: sum(r[k] for r in rows if r["kind"] == "written") for k in ("calls", "failures", "limited", "tokens_in")}
        self.assertEqual(total, {"calls": 1, "failures": 1, "limited": 1, "tokens_in": 1000})
        answers = []

        async def answer(text, **_):
            answers.append(text)
        admins, bot.ADMIN_IDS = bot.ADMIN_IDS, {999}
        try:
            await bot.cmd_ai_usage(types.SimpleNamespace(from_user=types.SimpleNamespace(id=999), answer=answer))
        finally:
            bot.ADMIN_IDS = admins
        self.assertIn("Yozma javoblar", answers[0])
        self.assertIn("limitga urildi", answers[0])


class Names(unittest.TestCase):
    def test_names_match(self):
        self.assertTrue(webapp.names_match("valiyev ali", "Ali Valiyev"))
        self.assertTrue(webapp.names_match("Gʻulomov Aziz", "G'ulomov Aziz"))
        self.assertFalse(webapp.names_match("Hacker Boy", "Ali Valiyev"))
        self.assertIsNone(webapp.names_match("Ali", None))



class CourseJoin(ServerBase):
    """Kursni topish: tuman → ustozlar → «O'quvchisiman» (ism-familiya, rasm ixtiyoriy) → ustoz tasdiqlaydi → ulanadi."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        import places
        self.region = next(iter(places.REGIONS)); self.district = places.REGIONS[self.region][0]
        db.remember_user(OWNER, "Nodira Karimova", "nodira")
        db.save_registration(OWNER, "teacher", "Nodira", "Karimova", "+998901112233", None, self.region, self.district)
        db.set_teacher_places(OWNER, [(self.region, self.district)])
        self.notices = []

        async def join_notice(*args):
            self.notices.append(args)
        webapp.HOOKS["join_notice"] = join_notice

    async def as_student(self, uid):
        self.user = {"id": uid, "name": f"O'quvchi {uid}", "username": f"u{uid}"}

    async def join(self, first="Ali", last="Valiyev", photo=None):
        from aiohttp import FormData
        form = FormData()
        form.add_field("teacher_id", str(OWNER)); form.add_field("first_name", first); form.add_field("last_name", last)
        if photo:
            form.add_field("photo", photo, filename="rasm.jpg", content_type="image/jpeg")
        response = await self.client.post("/api/courses/join", data=form)
        return response.status, await response.json()

    async def courses(self):
        from urllib.parse import quote
        return await (await self.client.get(f"/api/courses?region={quote(self.region)}&district={quote(self.district)}")).json()

    async def test_join_approve_links_student(self):
        _, r = await self.create()
        code = db.get_test(r["id"])["code"]
        await self.as_student(STUDENT)
        teachers = (await self.courses())["teachers"]
        self.assertEqual([(t["id"], t["status"]) for t in teachers], [(OWNER, None)])
        self.assertEqual((await self.join(last=""))[0], 400)  # familiya majburiy
        self.assertEqual((await self.join(photo=bytes([255, 216]) + b" rasm"))[1]["status"], "pending")
        self.assertEqual(self.notices[0][2], "Ali Valiyev")
        self.assertEqual((await self.courses())["teachers"][0]["status"], "pending")
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        joins = (await (await self.client.get("/api/teacher/joins")).json())["joins"]
        self.assertEqual((joins[0]["first_name"], joins[0]["has_photo"]), ("Ali", True))
        self.assertEqual((await self.client.get(f"/api/teacher/join/{joins[0]['id']}/photo")).status, 200)
        status, data = await self.post(f"/api/teacher/join/{joins[0]['id']}", {"action": "approve"})
        self.assertEqual((status, data["name"]), (200, "Ali Valiyev"))
        self.assertTrue(db.is_linked(OWNER, STUDENT))
        self.assertIn("qabul qildi", str(self.bot.messages[-1]))
        self.assertEqual((await self.post(f"/api/teacher/join/{joins[0]['id']}", {"action": "approve"}))[0], 409)
        await self.as_student(STUDENT)
        self.assertEqual((await self.client.get(f"/api/test/{code}")).status, 200)  # endi mock bepul
        self.assertEqual((await self.courses())["teachers"][0]["status"], "approved")
        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertEqual(boot["my_teachers"], ["Nodira Karimova"])

    async def test_reject_and_others_cannot_decide(self):
        await self.as_student(OTHER)
        await self.join("Vali", "Aliyev")
        request_id = db.join_requests_for(OWNER)[0]["id"]
        await self.as_student(STUDENT)  # begona ustoz emas — boshqaning arizasini ko'ra olmaydi
        self.assertEqual((await self.post(f"/api/teacher/join/{request_id}", {"action": "approve"}))[0], 404)
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        self.assertEqual((await self.post(f"/api/teacher/join/{request_id}", {"action": "reject"}))[1]["status"], "rejected")
        self.assertFalse(db.is_linked(OWNER, OTHER))
        await self.as_student(OTHER)
        self.assertEqual((await self.courses())["teachers"][0]["status"], "rejected")
        self.assertEqual((await self.join("Vali", "Aliyev"))[1]["status"], "pending")  # qayta yuborsa bo'ladi
        await webapp.decide_join(OWNER, request_id, True)
        db.unlink_student(OWNER, OTHER)  # ustoz keyin o'chirdi — qayta ariza yuborsa bo'ladi
        self.assertIsNone((await self.courses())["teachers"][0]["status"])

    async def test_bad_place(self):
        self.assertEqual((await self.client.get("/api/courses?region=Yo'q&district=Yo'q")).status, 400)


class PaidCheck(ServerBase):
    """Oflayn test: yozma javoblar — bepul (Gemini: FREE_IDS; boshqalar qo'lda) yoki pullik Claude (ustoz kreditidan)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._saved = (plans.SALES_ENABLED, webapp.SALES_ENABLED, essay_ai.ANTHROPIC_API_KEY)
        plans.SALES_ENABLED = webapp.SALES_ENABLED = False
        plans.set_access(OWNER, True)  # ruxsat berilgan ustoz (FREE_IDS emas)
        essay_ai.ANTHROPIC_API_KEY = "test-key"

    async def asyncTearDown(self):
        plans.SALES_ENABLED, webapp.SALES_ENABLED, essay_ai.ANTHROPIC_API_KEY = self._saved
        await super().asyncTearDown()

    async def offline(self, check_mode):
        items = structure.build_items("milliy", 45)
        key = {i["label"]: ("A" if i["kind"] == "choice" else "kitob") for i in items}
        status, r = await self.post("/api/teacher/tests", {"title": "Oflayn", "fmt": "milliy", "mode": "offline",
                                                           "keys": {"A": key}, "channels": [], "check_mode": check_mode})
        self.assertEqual(status, 200, r)
        return db.get_test(r["id"])

    async def attempt(self, test, n):
        attempt = await bot.record_attempt(self.bot, test, 9000 + n, f"O'quvchi {n}", "A", {}, None, [], 0.0, notify=False)
        return db.get_attempt(attempt["id"])

    async def test_paid_mode_spends_credit_per_ten_sheets(self):
        test = await self.offline("paid")
        self.assertEqual(test["check_mode"], "paid")
        db.add_credits(OWNER, 2)
        queued = [await webapp.attach_written(test, await self.attempt(test, n), "aGk=", "image/png") for n in range(12)]
        self.assertEqual(queued, [True] * 12)
        self.assertEqual(db.get_credits(OWNER), 0)  # 1–10 → 1 kredit, 11–12 → 2-kredit
        for n in range(12, 20):  # 2-kredit 20-titulgacha yetadi
            self.assertTrue(await webapp.attach_written(test, await self.attempt(test, n), "aGk=", "image/png"))
        self.assertFalse(await webapp.attach_written(test, await self.attempt(test, 20), "aGk=", "image/png"))  # 21-titul

    async def test_paid_mode_without_credit_is_manual(self):
        test = await self.offline("paid")
        attempt = await self.attempt(test, 1)
        self.assertFalse(await webapp.attach_written(test, attempt, "aGk=", "image/png"))
        self.assertEqual(db.get_written_job(attempt["id"])["status"], "manual")  # ustoz rasmga qarab belgilaydi

    async def test_free_mode_for_regular_teacher_is_manual(self):
        test = await self.offline("free")
        db.add_credits(OWNER, 5)
        attempt = await self.attempt(test, 1)
        self.assertFalse(await webapp.attach_written(test, attempt, "aGk=", "image/png"))
        self.assertEqual(db.get_credits(OWNER), 5)  # bepul rejimda kredit ketmaydi

    async def test_paid_needs_key_and_online_ignored(self):
        essay_ai.ANTHROPIC_API_KEY = ""
        items = structure.build_items("milliy", 45)
        key = {i["label"]: ("A" if i["kind"] == "choice" else "kitob") for i in items}
        base = {"title": "Sinov", "fmt": "milliy", "keys": {"A": key}, "channels": [], "check_mode": "paid"}
        self.assertEqual((await self.post("/api/teacher/tests", {**base, "mode": "offline"}))[0], 400)
        status, r = await self.post("/api/teacher/tests", {**base, "mode": "online"})
        self.assertEqual((status, db.get_test(r["id"])["check_mode"]), (200, "free"))
        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertFalse(boot["paid_ai"])

    async def test_claude_haiku_request_shape(self):
        """Haiku: server zaxirasi (betas/fallbacks) yuborilmaydi; Sonnet'da yuboriladi."""
        import anthropic
        sent = []

        class Fake:
            def __init__(self, api_key):
                self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self.create))

            async def create(self, **params):
                sent.append(params)
                return types.SimpleNamespace(
                    stop_reason="end_turn", content=[types.SimpleNamespace(type="thinking"),
                                                     types.SimpleNamespace(type="text", text='{"ok": 1}')],
                    usage=types.SimpleNamespace(input_tokens=1000, output_tokens=100))

            async def close(self):
                pass
        original = anthropic.AsyncAnthropic
        anthropic.AsyncAnthropic = Fake
        try:
            out, usage = await essay_ai._call("s", "t", [], None, provider="claude", model="claude-haiku-5-5",
                                              effort="low", kind="written")
            await essay_ai._call("s", "t", [], None, provider="claude", model="claude-sonnet-5-5", kind="essay")
        finally:
            anthropic.AsyncAnthropic = original
        self.assertEqual(out, '{"ok": 1}')  # thinking bloki o'tkazib yuboriladi
        self.assertEqual((sent[0]["model"], sent[0]["output_config"]["effort"]), ("claude-haiku-5-5", "low"))
        self.assertNotIn("fallbacks", sent[0])
        self.assertNotIn("betas", sent[0])
        self.assertEqual(sent[1]["fallbacks"], "default")
        self.assertAlmostEqual(usage["cost"], (1000 * 0.10 + 100 * 0.50) / 1e6, places=6)
        self.assertEqual({r["model"] for r in db.ai_usage_since("2000-01-01")}, {"claude-haiku-5-5", "claude-sonnet-5-5"})



class OnlineOfficialTitul(ServerBase):
    """Har milliy mockka rasmiy titul (onlayn ham); onlayn mockda esse ilovada — natija yashirilmaydi."""

    async def test_online_milliy_gets_official_but_result_open(self):
        from scoring import essay_on_paper, essay_pending
        _, r = await self.create(mode="online", fmt="milliy")
        test = db.get_test(r["id"])
        self.assertEqual(test["sheet_style"], "official")
        self.assertFalse(essay_on_paper(test))
        self.assertFalse(essay_pending(test, {"essay": None}))
        _, off = await self.create(mode="offline", fmt="milliy")
        self.assertTrue(essay_on_paper(db.get_test(off["id"])))
        status, copy = await self.post(f"/api/teacher/test/{r['id']}/copy", {})
        self.assertEqual(status, 200, copy)
        self.assertEqual(db.get_test(copy["id"])["sheet_style"], "official")


class EssayScan(ServerBase):
    """Qog'ozdagi esse rasmi → matn: ilovada yuklash yoki botga yuborish; AI'ni o'quvchi tanlaydi; zaxira AI."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        db.set_setting("platform_share", "0")  # eski tartib: begonalar puli ustoz kartasiga (yangisi — RevenueShare)
        self._saved = (essay_ai.ANTHROPIC_API_KEY, essay_ai._call)
        essay_ai.ANTHROPIC_API_KEY = "test-key"
        self.calls = []

        async def fake_call(system, text, images, schema, provider=None, effort="medium", kind="other", model=None):
            self.calls.append(provider)
            if provider in self.broken:
                raise essay_ai.AIError("limit 429", retry=True)
            return "Vatan — bu ona.\nUni sevamiz.", {"model": model or "gemini-flash-latest", "cost": 0}
        self.broken = set()
        essay_ai._call = fake_call
        notices = self.notices = []

        async def notice(*a):
            notices.append(a)
        webapp.HOOKS.update(essay_scan_notice=notice, essay_scan_start=notice,
                            bot_username=lambda: asyncio.sleep(0, "test_bot"))
        _, r = await self.create(mode="online", fmt="milliy")
        self.test = db.get_test(r["id"])
        db.link_student(OWNER, STUDENT)
        self.user = {"id": STUDENT, "name": "O'quvchi", "username": "s"}

    async def asyncTearDown(self):
        essay_ai.ANTHROPIC_API_KEY, essay_ai._call = self._saved
        await super().asyncTearDown()

    async def upload(self, ai, n=2):
        from aiohttp import FormData
        form = FormData()
        form.add_field("ai", ai)
        for i in range(n):
            form.add_field("photo", f"bet{i}".encode(), filename=f"{i}.jpg", content_type="image/jpeg")
        response = await self.client.post(f"/api/test/{self.test['code']}/essay-scan/photo", data=form)
        return response.status, await response.json()

    async def test_readers_listed_and_upload_reads(self):
        info = await (await self.client.get(f"/api/test/{self.test['code']}")).json()
        self.assertEqual([r["key"] for r in info["readers"]], ["gemini", "claude"])
        status, r = await self.upload("claude")
        self.assertEqual((status, r["status"], r["model"]), (200, "done", essay_ai.PAID_MODEL))
        self.assertIn("Vatan", r["text"])
        self.assertEqual(self.calls, ["claude"])
        got = await (await self.client.get(f"/api/test/{self.test['code']}/essay-scan")).json()
        self.assertEqual(got["text"], r["text"])
        self.assertEqual(db.get_essay_scan(STUDENT, self.test["id"])["pages"], [])  # rasmlar saqlanib qolmaydi

    async def test_fallback_to_other_ai(self):
        self.broken = {"gemini"}
        status, r = await self.upload("gemini")
        self.assertEqual((r["status"], r["model"]), ("done", essay_ai.PAID_MODEL))
        self.assertEqual(self.calls, ["gemini", "claude"])
        self.broken = {"gemini", "claude"}
        status, r = await self.upload("gemini")
        self.assertEqual((status, r["status"]), (200, "failed"))

    async def test_outsider_must_pay_first_and_submitted_blocked(self):
        db.set_test_price(self.test["id"], 15000)
        db.set_setting(f"teacher_card:{OWNER}", "8600123412341234")
        self.user = {"id": OTHER, "name": "Begona", "username": None}
        self.assertEqual((await self.upload("gemini"))[0], 402)
        self.user = {"id": STUDENT, "name": "O'quvchi", "username": "s"}
        await self.post(f"/api/test/{self.test['code']}/submit", {"variant": "A", "name": "Ali Valiyev", "answers": {}})
        self.assertEqual((await self.upload("gemini"))[0], 409)

    async def test_bot_route(self):
        import io
        status, r = await self.post(f"/api/test/{self.test['code']}/essay-scan/start", {"ai": "gemini"})
        self.assertEqual((status, r["bot"]), (200, "test_bot"))
        self.assertEqual(self.notices[-1][2], "gemini")
        answers = []

        class Sent:
            async def delete(self):
                pass

        async def answer(text, **_):
            answers.append(text)
            return Sent()

        async def download(file_id):
            return io.BytesIO(b"bet")
        message = types.SimpleNamespace(from_user=types.SimpleNamespace(id=STUDENT), photo=[types.SimpleNamespace(file_id="F")],
                                        document=None, answer=answer, bot=types.SimpleNamespace(download=download))
        await bot.receipt_received(message)  # chek emas — esse sahifasi
        await bot.receipt_received(message)
        self.assertIn("2-bet", answers[-1])
        replies = []

        async def cb_answer(text=None, **_):
            replies.append(text)
        callback = types.SimpleNamespace(data=f"es:{self.test['id']}", from_user=types.SimpleNamespace(id=STUDENT),
                                         answer=cb_answer, message=types.SimpleNamespace(answer=answer))
        await bot.essay_scan_done(callback)
        scan = db.get_essay_scan(STUDENT, self.test["id"])
        self.assertEqual(scan["status"], "done")
        self.assertIn("Vatan", self.notices[-1][2])  # matn chatga
        await bot.essay_scan_done(callback)  # qayta bosish
        self.assertIn("allaqachon", replies[-1])
        await bot.receipt_received(message)  # endi esse yig'ilmayapti — oddiy chek oqimi
        self.assertIn("Chek qabul qilinmadi", answers[-1])



class AiRoutes(ServerBase):
    """Admin botda tugmalar bilan tanlaydi: qaysi ishni qaysi AI (Gemini / Claude Sonnet / Claude Haiku) bajaradi."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._key = essay_ai.ANTHROPIC_API_KEY
        essay_ai.forget_choices()

    async def asyncTearDown(self):
        essay_ai.ANTHROPIC_API_KEY = self._key
        essay_ai.forget_choices()
        await super().asyncTearDown()

    async def press(self, data, admin=999):
        replies, edits = [], []

        async def answer(text=None, **_):
            replies.append(text)

        async def edit_text(text, **_):
            edits.append(text)
        cb = types.SimpleNamespace(data=data, from_user=types.SimpleNamespace(id=admin), answer=answer,
                                   message=types.SimpleNamespace(edit_text=edit_text, html_text="", text=""))
        admins, bot.ADMIN_IDS = bot.ADMIN_IDS, {999}
        try:
            await bot.ai_settings_button(cb)
        finally:
            bot.ADMIN_IDS = admins
        return replies, edits

    async def test_buttons_choose_ai_per_task(self):
        essay_ai.ANTHROPIC_API_KEY = ""
        self.assertEqual(essay_ai.route("essay"), ("gemini", None))
        replies, _ = await self.press("aiset:essay:sonnet")  # Claude kaliti yo'q — tanlanmaydi
        self.assertIn("ANTHROPIC_API_KEY", replies[0])
        self.assertEqual(essay_ai.choice_of("essay"), "gemini")
        essay_ai.ANTHROPIC_API_KEY = "test-key"
        await self.press("aiset:essay:sonnet")
        await self.press("aiset:receipt:haiku")
        await self.press("aiset:paid:sonnet")
        self.assertEqual(essay_ai.route("essay"), ("claude", essay_ai.CLAUDE_MODEL))
        self.assertEqual(essay_ai.route("receipt"), ("claude", essay_ai.PAID_MODEL))
        self.assertEqual(essay_ai._paid(True)["model"], essay_ai.CLAUDE_MODEL)
        self.assertEqual(essay_ai.route("written"), ("gemini", None))  # tegilmagan — standart
        essay_ai.ANTHROPIC_API_KEY = ""  # kalit olib tashlandi — bor AI'ga o'tadi
        self.assertEqual(essay_ai.route("essay"), ("gemini", None))
        text, _ = bot.ai_settings_panel()
        self.assertIn("kalit yo'q", text)
        replies, _ = await self.press("aiset:essay:gemini", admin=123)  # admin emas
        self.assertIn("admin", replies[0].lower())


class FastSend(ServerBase):
    """Botga yuborish ilovani kutdirmaydi: fayl sekin ketsa — «yuborilmoqda» va orqada davom etadi."""

    async def test_slow_pdf_returns_pending_then_finishes(self):
        _, r = await self.create()
        finished = []

        async def slow_pdf(chat_id, test, kind):
            await asyncio.sleep(0.3)
            finished.append(kind)
        webapp.HOOKS["send_pdf"] = slow_pdf
        wait, webapp.DELIVER_WAIT = webapp.DELIVER_WAIT, 0.05
        try:
            status, res = await self.post(f"/api/teacher/test/{r['id']}/send", {"kind": "results"})
            self.assertEqual((status, res["pending"], finished), (200, True, []))  # ilova darhol javob oldi
            await asyncio.sleep(0.4)
            self.assertEqual(finished, ["results"])  # orqada yuborildi
            webapp.DELIVER_WAIT = 2
            status, res = await self.post(f"/api/teacher/test/{r['id']}/send", {"kind": "key"})
            self.assertEqual((status, res["pending"]), (200, False))  # ulgurdi — «yuborildi»
        finally:
            webapp.DELIVER_WAIT = wait

    async def test_fast_failure_is_reported(self):
        _, r = await self.create()

        async def broken(chat_id, test, kind):
            raise RuntimeError("Forbidden: bot was blocked by the user")
        webapp.HOOKS["send_pdf"] = broken
        status, res = await self.post(f"/api/teacher/test/{r['id']}/send", {"kind": "results"})
        self.assertEqual(status, 502)
        self.assertIn("/start", res["error"])

    async def test_stray_photo_without_sales_does_not_offer_subscription(self):
        answers = []

        async def answer(text, **_):
            answers.append(text)
        message = types.SimpleNamespace(from_user=types.SimpleNamespace(id=STUDENT), photo=[types.SimpleNamespace(file_id="F")],
                                        document=None, answer=answer)
        sales, plans.SALES_ENABLED = plans.SALES_ENABLED, False  # sotuv — plans.sales_on() orqali
        try:
            await bot.receipt_received(message)
        finally:
            plans.SALES_ENABLED = sales
        self.assertNotIn("Obuna", answers[0])
        self.assertIn("Skanerlash", answers[0])



class ResultsPdfClarity(ServerBase):
    """Natijalar PDF: tekshirilmagan yozma javoblar xato emas, xatolar qisqa oraliqda, esse kutilganda «—»."""

    def test_compact_ranges(self):
        import pdfs
        order = [str(i) for i in range(1, 36)] + ["36", "37", "38", "39", "40a", "40b"]
        self.assertEqual(pdfs._compact(["1", "2", "4", "6", "7", "8", "9", "35", "36", "37"], order), "1, 2, 4, 6–9, 35–37")
        self.assertEqual(pdfs._compact([], order), "—")

    async def test_unchecked_written_not_counted_wrong(self):
        import pdfs
        _, r = await self.create(mode="offline", fmt="milliy")
        test = db.get_test(r["id"])
        empty = await bot.record_attempt(self.bot, test, 9001, "Ali", "A", {"1": "A"}, None, [], 0.0, notify=False)
        read = await bot.record_attempt(self.bot, test, 9002, "Vali", "A", {"1": "A", "36": "kitob"}, None, [], 0.0, notify=False)
        db.create_written_job(read["id"], test["id"], "aGk=", "image/png", "done")
        self.assertEqual(pdfs._unchecked_written(test, db.list_attempts(test["id"])), {empty["id"]})
        pdf = pdfs.results_pdf(test, db.list_attempts(test["id"]))
        self.assertTrue(pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()


class PracticePublic(ServerBase):
    """Begona o'quvchi ilovada: ommaviy havolani bir marta ochsa — «✍️ Esse tekshirish» (bepullari, krediti, paketlar)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        db.set_setting("platform_share", "0")  # eski tartib: begonalar puli ustoz kartasiga (yangisi — RevenueShare)
        self._essay_enabled = essay_ai.essay_enabled
        essay_ai.essay_enabled = lambda: True
        db.remember_user(OWNER, "Ustoz Aliyev", None)
        db.set_setting(f"essay_public:{OWNER}", "1")
        db.set_setting(f"teacher_card:{OWNER}", "8600 1234 5678 9012")
        webapp.save_packs(OWNER, 5000, [(5, 20000)])

    async def asyncTearDown(self):
        essay_ai.essay_enabled = self._essay_enabled
        await super().asyncTearDown()

    ESSAY = "Kitob o'qish insonni boyitadi. " * 20

    async def send(self):
        return await self.post("/api/practice", {"teacher_id": OWNER, "topic": "Kitob", "essay": self.ESSAY})

    async def test_stranger_free_then_credit_in_app(self):
        self.user = {"id": OTHER, "name": "Begona", "username": None}
        self.assertFalse((await self.get("/api/bootstrap"))[1]["practice"])  # hech bir ustozni tanimaydi
        db.remember_public(OWNER, OTHER)  # ommaviy havolani botda ochdi
        self.assertTrue((await self.get("/api/bootstrap"))[1]["practice"])
        _, home = await self.get("/api/practice")
        t = home["teachers"][0]
        self.assertEqual((t["kind"], t["free_left"], t["balance"], [p["essays"] for p in t["packs"]]), ("public", 2, 0, [1, 5]))

        for _ in range(2):
            status, res = await self.send()
            self.assertEqual((status, res["status"], res["free"]), (200, "pending", True), res)
        status, res = await self.send()  # bepullar tugadi, kredit yo'q — saqlanadi
        self.assertEqual((status, res["status"], res["balance"]), (200, "awaiting_credit", 0))
        waiting_id = res["id"]
        t = (await self.get("/api/practice"))[1]["teachers"][0]
        self.assertEqual((t["free_left"], t["waiting"]), (0, 1))

        status, order = await self.post("/api/practice/wallet-order", {"teacher_id": OWNER, "essays": 5})
        self.assertEqual((status, order["payment"]["amount"]), (200, 20000), order)
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        await self.post(f"/api/teacher/wallet-payment/{order['payment']['id']}", {"action": "approve"})
        self.assertEqual(db.get_practice(waiting_id)["status"], "pending")  # kredit tushdi — saqlangan esse tekshiruvda
        self.assertEqual(db.wallet_balance(OWNER, OTHER), 4)

        self.user = {"id": OTHER, "name": "Begona", "username": None}
        status, res = await self.send()
        self.assertEqual((res["status"], res["balance"]), ("pending", 3))
        db.set_setting(f"essay_public:{OWNER}", "0")  # ustoz yopdi
        self.assertEqual((await self.send())[0], 403)
        self.assertEqual((await self.get("/api/practice"))[1]["teachers"][0]["open"], False)  # krediti bor — ko'rinadi

    async def test_linked_student_unchanged(self):
        db.link_student(OWNER, STUDENT)
        self.user = {"id": STUDENT, "name": "O'quvchi", "username": None}
        _, home = await self.get("/api/practice")
        self.assertEqual(home["teachers"][0]["kind"], "student")
        status, res = await self.send()
        self.assertEqual(status, 200, res)
        self.assertNotIn("status", res)
        self.assertEqual(db.get_practice(res["id"])["kind"], "student")


class TeacherSettings(ServerBase):
    """⚙️ Sozlamalar: karta, begonalar uchun ochish (karta va narxsiz — yo'q), kredit narxlari."""

    async def test_card_public_and_packs(self):
        webapp.HOOKS["bot_username"] = lambda: asyncio.sleep(0, "test_bot")
        status, s = await self.get("/api/teacher/settings")
        self.assertEqual((status, s["card"], s["public"], s["packs"]), (200, "", False, []))
        self.assertEqual((await self.post("/api/teacher/settings", {"public": True}))[0], 400)  # karta va narx yo'q
        self.assertEqual((await self.post("/api/teacher/settings", {"card": "8600 1234", "holder": "Ali Valiyev"}))[0], 400)
        status, s = await self.post("/api/teacher/settings", {"card": "8600123456789012", "holder": " Ali  Valiyev "})
        self.assertEqual((status, s["card"], s["holder"]), (200, "8600 1234 5678 9012", "Ali Valiyev"))
        self.assertEqual((await self.post("/api/teacher/settings", {"public": True}))[0], 400)  # narx hali yo'q
        await self.post("/api/teacher/packs", {"packs": [{"essays": 1, "price": 12000}, {"essays": 5, "price": 55000}]})
        status, s = await self.post("/api/teacher/settings", {"public": True})
        self.assertEqual((status, s["public"]), (200, True))
        self.assertIn("start=p_", s["public_url"])
        self.assertEqual([p["essays"] for p in s["packs"]], [1, 5])
        self.assertEqual(webapp.teacher_card(OWNER)["number"], "8600 1234 5678 9012")  # bot ham shu kartani ko'radi


class ScheduledMock(ServerBase):
    """«Bugungi mock»: jadval → «Qatnashaman» → tanlov (AI / ustoz) va to'lov → kirish vaqti → yopilgach natija va sertifikat."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from datetime import datetime
        self._saved = (plans.now, plans.FREE_IDS, essay_ai.essay_enabled)
        self.clock = datetime(2026, 11, 20, 19, 0, tzinfo=plans.TASHKENT)
        plans.now = lambda: self.clock
        plans.FREE_IDS = {OWNER}
        essay_ai.essay_enabled = lambda: True
        webapp.HOOKS["bot_username"] = lambda: asyncio.sleep(0, "test_bot")
        db.remember_user(OWNER, "Ustoz Aliyev", None)

    async def asyncTearDown(self):
        plans.now, plans.FREE_IDS, essay_ai.essay_enabled = self._saved
        await super().asyncTearDown()

    def at(self, hour, minute=0):
        self.clock = self.clock.replace(hour=hour, minute=minute)

    def give_history(self, uid):
        """O'quvchi shu ustozning boshqa vaqtli mockida bepul joy olgan («birinchi mock bepul» ishlatilgan)."""
        items = structure.build_items("milliy", 45)
        old = db.create_test(OWNER, "Oldingi mock", "milliy", 45, items, {"A": {i["label"]: "A" for i in items}})
        db.set_schedule(old["id"], "2026-11-13 20:00", "2026-11-13 22:00", "2026-11-13 23:00", True)
        db.start_essay_payment(old["id"], uid, 0, "ai")

    def as_user(self, uid):
        self.user = {"id": uid, "name": f"O'quvchi {uid}", "username": None}

    ESSAY = "Vatan — bu ona. " * 30

    async def test_full_day(self):
        _, r = await self.create("online", fmt="milliy")
        test = db.get_test(r["id"])
        await self.post("/api/teacher/settings", {"card": "8600123456789012", "holder": "Ustoz Aliyev"})
        status, res = await self.post(f"/api/teacher/test/{test['id']}/schedule", {
            "date": "2026-11-20", "opens": "20:00", "entry": "22:00", "closes": "23:00", "public": True,
            "price_ai": 15000, "price_teacher": 10000})
        self.assertEqual(status, 200, res)
        self.assertEqual((res["schedule"]["mock_no"], res["schedule"]["phase"], res["schedule"]["price_ai"]), (1, "soon", 15000))
        bad = {"date": "2026-11-20", "opens": "22:00", "entry": "21:00", "closes": "23:00"}
        self.assertEqual((await self.post(f"/api/teacher/test/{test['id']}/schedule", bad))[0], 400)

        # Begona: bosh sahifada karta, «Qatnashaman»
        self.as_user(OTHER)
        mocks = (await self.get("/api/bootstrap"))[1]["mocks"]
        self.assertEqual((len(mocks), mocks[0]["signups"], mocks[0]["signed"]), (1, 0, False))
        status, card = await self.post(f"/api/mock/{test['code']}/signup", {"on": True})
        self.assertEqual((status, card["signups"], card["signed"]), (200, 1, True))
        status, err = await self.get(f"/api/test/{test['code']}")
        self.assertEqual((status, err["mock"]["schedule"]["phase"]), (409, "soon"))  # hali ochilmagan

        # 30 daqiqa qoldi va ochildi — bot eslatadi (har biri bir marta)
        self.at(19, 40)
        await bot.mock_tick(self.bot)
        self.at(20, 5)
        await bot.mock_tick(self.bot)
        await bot.mock_tick(self.bot)
        reminders = [m for c, m in self.bot.messages if c == OTHER]
        self.assertEqual(len(reminders), 2)
        self.assertIn("boshlandi", reminders[1])
        self.assertIn("1 kishi qatnashmoqchi", reminders[1])

        # Birinchi mock — bepul (kartada va tanlovda «🎁 Birinchi mockingiz bepul»)
        status, err = await self.get(f"/api/test/{test['code']}")
        self.assertEqual((status, err["mock_choice"]["first_free"]), (402, True))
        self.assertEqual([o["price"] for o in err["mock_choice"]["options"]], [0, 0])
        self.assertTrue((await self.get("/api/bootstrap"))[1]["mocks"][0]["first_free"])
        self.give_history(OTHER)  # avval boshqa mockda qatnashgan — endi pullik

        # Ochiq: avval tanlov (AI — 15 000, ustoz — 10 000), to'lanmaguncha savollar yo'q
        status, err = await self.get(f"/api/test/{test['code']}")
        self.assertEqual((status, err["mock_choice"]["first_free"]), (402, False))
        self.assertEqual([(o["key"], o["price"]) for o in err["mock_choice"]["options"]], [("ai", 15000), ("teacher", 10000)])
        status, res = await self.post(f"/api/test/{test['code']}/mock-choice", {"choice": "ai"})
        self.assertEqual((status, res["status"], res["amount"]), (200, "open", 15000))
        self.assertTrue(any(c == OTHER and "15 000 so'm" in m for c, m in self.bot.messages))
        self.assertEqual((await self.post(f"/api/test/{test['code']}/submit", {"name": "Begona Bola", "answers": {}}))[0], 402)
        pay = db.essay_payment_of(test["id"], OTHER)
        db.decide_essay_payment(pay["id"], "paid", ("open",))  # chekni AI tasdiqladi
        status, info = await self.get(f"/api/test/{test['code']}")
        self.assertEqual((status, info["schedule"]["phase"]), (200, "open"))
        status, res = await self.post(f"/api/test/{test['code']}/submit", {"name": "Begona Bola", "answers": {}, "essay": self.ESSAY})
        self.assertEqual(status, 200, res)
        ai_attempt = res["attempt_id"]
        self.assertEqual(db.get_essay(ai_attempt)["status"], "pending")  # AI tekshiradi

        # Ustozning o'quvchisi: bepul, «ustoz tekshirsin»
        db.link_student(OWNER, STUDENT)
        self.as_user(STUDENT)
        err = (await self.get(f"/api/test/{test['code']}"))[1]
        self.assertEqual([o["price"] for o in err["mock_choice"]["options"]], [0, 0])
        status, res = await self.post(f"/api/test/{test['code']}/mock-choice", {"choice": "teacher"})
        self.assertEqual((status, res["status"]), (200, "paid"))
        status, res = await self.post(f"/api/test/{test['code']}/submit", {"name": "O'z O'quvchi", "answers": {}, "essay": self.ESSAY})
        self.assertEqual(status, 200, res)
        teacher_attempt = res["attempt_id"]
        self.assertEqual(db.get_essay(teacher_attempt)["status"], "manual")

        # 22:30 — kirish yopilgan: yangi odam kira olmaydi; 23:10 — yopilgan
        self.at(22, 30)
        self.as_user(444)
        status, err = await self.get(f"/api/test/{test['code']}")
        self.assertEqual(status, 409)
        self.assertIn("Kirish vaqti tugagan", err["error"])

        # Yopilgach: AI hali tekshirmagan — kutadi; AI tugadi — ustozni kutadi; ustoz baholadi — natija va sertifikat
        self.at(23, 10)
        await bot.mock_tick(self.bot)
        self.assertEqual(db.get_test(test["id"])["mock_state"], "opened")
        db.save_essay_result(ai_attempt, {"score": 18, "criteria": [], "comment": ""})
        db.set_essay(ai_attempt, 18)
        await bot.mock_tick(self.bot)
        self.assertEqual(db.get_test(test["id"])["mock_state"], "teacher")
        self.assertTrue(any(c == OWNER and "1 ta" in m for c, m in self.bot.messages))
        self.as_user(OWNER)
        await self.post(f"/api/teacher/essay/{teacher_attempt}", {"score75": 63})
        await bot.mock_tick(self.bot)
        done = db.get_test(test["id"])
        self.assertEqual((done["mock_state"], bool(done["closed"])), ("done", True))
        certificates = [c for c, name in self.bot.documents if name.startswith("sertifikat_")]
        self.assertEqual(sorted(certificates), sorted([OTHER, STUDENT]))

    async def test_student_pass_makes_mocks_free(self):
        """«💎 Tarif»: FREE_IDS ustoz narx qo'yadi, begona o'quvchi oladi (chekni AI tasdiqlaydi) — mock bepul bo'ladi."""
        await self.post("/api/teacher/settings", {"card": "8600123456789012", "holder": "Ustoz Aliyev"})
        status, s = await self.post("/api/teacher/settings", {"student_tariffs": [{"days": 30, "price": "50000"}, {"days": 90, "price": ""}]})
        self.assertEqual((status, s["student_tariffs"]), (200, [{"days": 30, "price": 50000}]))
        _, r = await self.create("online", fmt="milliy")
        test = db.get_test(r["id"])
        await self.post(f"/api/teacher/test/{test['id']}/schedule", {"date": "2026-11-20", "opens": "20:00", "entry": "22:00",
                                                                     "closes": "23:00", "public": True, "price_ai": 15000})
        self.as_user(OTHER)
        passes = (await self.get("/api/bootstrap"))[1]["passes"]
        self.assertEqual([(p["teacher_id"], p["until"]) for p in passes], [(OWNER, None)])
        status, order = await self.post("/api/pass/order", {"teacher_id": OWNER, "days": 30})
        self.assertEqual((status, order["payment"]["amount"]), (200, 50000), order)
        self.assertEqual((await self.post("/api/pass/order", {"teacher_id": OWNER, "days": 7}))[0], 409)  # bunday tarif yo'q

        enabled, read = essay_ai.enabled, essay_ai.read_receipt
        essay_ai.enabled = lambda: True

        async def good(image, mime="image/jpeg"):
            return {"is_receipt": True, "success": True, "amount": 50000, "date": "2026-11-20", "recipient_card": "8600 **** **** 9012",
                    "recipient_name": "USTOZ ALIYEV", "transaction_id": "TXP1", "looks_edited": False}
        essay_ai.read_receipt = good
        notices = []
        webapp.HOOKS["pass_receipt_notice"] = lambda *a: asyncio.sleep(0, notices.append(a))
        try:
            res = await webapp.check_pass_receipt(order["payment"]["id"], b"chek-tarif", "image/jpeg")
        finally:
            essay_ai.enabled, essay_ai.read_receipt = enabled, read
        self.assertEqual(res["status"], "paid")
        self.assertEqual(db.pass_until(OWNER, OTHER), "2026-12-20 19:00")
        self.assertTrue(notices and notices[0][1])  # ustozga: AI tasdiqladi
        self.assertTrue(any(c == OTHER and "Tarif yoqildi" in m for c, m in self.bot.messages))

        # Mock ochildi — tarif egasiga narx yo'q (tanlov bepul)
        self.at(20, 30)
        err = (await self.get(f"/api/test/{test['code']}"))[1]
        self.assertEqual([o["price"] for o in err["mock_choice"]["options"]], [0, 0])
        self.assertEqual((await self.get("/api/bootstrap"))[1]["passes"][0]["until"], "2026-12-20 19:00")

        # Ustoz AI tasdiqlaganini bekor qiladi — kunlar qaytib olinadi, narx yana chiqadi
        await webapp.decide_pass_payment(order["payment"]["id"], False, OWNER)
        self.assertEqual(db.pass_until(OWNER, OTHER), "2026-11-20 19:00")
        self.give_history(OTHER)  # birinchi mocki allaqachon bo'lgan — narx chiqadi
        err = (await self.get(f"/api/test/{test['code']}"))[1]
        self.assertEqual(err["mock_choice"]["options"][0]["price"], 15000)

    async def test_pass_countdown_and_reminders(self):
        """Tarif: «30 kun qoldi» → ertasi 29 …; 1 kun qolganda va tugaganda bot xabar beradi, keyin o'zi o'chadi."""
        await self.post("/api/teacher/settings", {"card": "8600123456789012", "holder": "Ustoz Aliyev"})
        await self.post("/api/teacher/settings", {"student_tariffs": [{"days": 30, "price": "50000"}]})
        pay = db.start_pass_payment(OWNER, OTHER, 30, 50000)
        self.assertEqual(db.pay_pass_payment(pay["id"], ("open",), "2026-11-20 19:00"), "2026-12-20 19:00")
        self.as_user(OTHER)
        offer = (await self.get("/api/bootstrap"))[1]["passes"][0]
        self.assertEqual((offer["days_left"], offer["total_days"]), (30, 30))
        self.clock = self.clock.replace(day=21, hour=8)  # ertasi kuni
        offer = (await self.get("/api/bootstrap"))[1]["passes"][0]
        self.assertEqual(offer["days_left"], 30)  # 29 kun 11 soat — 30-kun hali tugamagan
        self.clock = self.clock.replace(day=22, hour=20)
        self.assertEqual((await self.get("/api/bootstrap"))[1]["passes"][0]["days_left"], 28)

        self.clock = self.clock.replace(month=12, day=19, hour=21)  # 22 soat qoldi
        await bot.pass_reminders(self.bot, self.clock)
        await bot.pass_reminders(self.bot, self.clock)  # takror yuborilmaydi
        self.assertEqual(sum(1 for c, m in self.bot.messages if c == OTHER and "1 kun qoldi" in m), 1)
        self.assertEqual((await self.get("/api/bootstrap"))[1]["passes"][0]["days_left"], 1)

        self.clock = self.clock.replace(day=20, hour=19, minute=30)  # tugadi
        await bot.pass_reminders(self.bot, self.clock)
        self.assertEqual(sum(1 for c, m in self.bot.messages if c == OTHER and "Tarifingiz tugadi" in m), 1)
        offer = (await self.get("/api/bootstrap"))[1]["passes"][0]
        self.assertEqual((offer["until"], offer["expired"]), (None, "2026-12-20 19:00"))
        self.assertFalse(await webapp.pass_active(OWNER, OTHER))  # o'zi o'chdi — mocklar yana pullik


    async def test_entry_warning_channel_posts_and_free_ids_only(self):
        """Kirishga 15 daqiqa — hali kirmaganlarga; kanalga e'lonlar tartib bilan (bir martadan); jadval — faqat FREE_IDS."""
        _, r = await self.create("online", fmt="milliy")
        test = db.get_test(r["id"])
        await self.post(f"/api/teacher/test/{test['id']}/schedule", {"date": "2026-11-20", "opens": "20:00", "entry": "22:00",
                                                                     "closes": "23:00", "public": True})
        webapp.HOOKS["resolve_channel"] = lambda ref: asyncio.sleep(0, {"chat_id": -1009, "title": "Ona tili kanali", "url": "https://t.me/k"})
        status, s = await self.post("/api/teacher/settings", {"announce_channels": ["@k"]})
        self.assertEqual((status, [c["chat_id"] for c in s["announce_channels"]]), (200, [-1009]))
        db.mock_signup(test["id"], OTHER, "Begona", True)
        db.mock_signup(test["id"], STUDENT, "Kirgan", True)

        async def bot_username(_bot):
            return "test_bot"
        saved_username, bot.bot_username = bot.bot_username, bot_username
        try:
            for hour, minute in ((9, 30), (19, 10), (20, 1), (20, 1), (21, 50), (21, 50)):
                self.at(hour, minute)
                if (hour, minute) == (21, 50):
                    db.live_ping(test["id"], STUDENT, "Kirgan", 3, 45)  # bu o'quvchi allaqachon kirgan
                await bot.mock_tick(self.bot)
        finally:
            bot.bot_username = saved_username
        posts = [m for c, m in self.bot.messages if c == -1009 and len(m) > 4]
        stickers = [m for c, m in self.bot.messages if c == -1009 and len(m) <= 4]
        self.assertEqual(len(posts), 4, posts)  # 19:00 dan oldin «bugun», 19:10 «1 soat», 20:01 «boshlandi», 21:50 «15 daqiqa»
        self.assertIn("Bugun 20:00 da", posts[0])
        self.assertIn("1 soat qoldi", posts[1])
        self.assertIn("boshlandi", posts[2])
        self.assertIn("Kirishga 15 daqiqa", posts[3])
        self.assertEqual(len(stickers), 4)  # har e'londan oldin katta emoji
        warned = [c for c, m in self.bot.messages if "Kirishga 15 daqiqa qoldi" in m and c > 0]
        self.assertEqual(warned, [OTHER])  # kirgan o'quvchiga yuborilmaydi
        self.assertTrue(any("1 soat 10 daqiqa" in m for c, m in self.bot.messages if c == OTHER))

        # FREE_IDS bo'lmagan ustoz jadval qo'ya olmaydi
        plans.FREE_IDS = set()
        status, _ = await self.post(f"/api/teacher/test/{test['id']}/schedule", {"date": "2026-11-20", "opens": "20:00",
                                                                                 "entry": "22:00", "closes": "23:00"})
        self.assertEqual(status, 403)


    async def test_credit_pay_own_link_and_claude_capacity(self):
        """Mock kredit bilan (narxga teng kredit), o'z o'quvchilari havolasi (bepul), Claude — nechta esse qoldi."""
        await self.post("/api/teacher/settings", {"card": "8600123456789012", "holder": "Ustoz Aliyev"})
        await self.post("/api/teacher/packs", {"packs": [{"essays": 1, "price": 5000}]})  # 1 kredit = 5 000
        _, r = await self.create("online", fmt="milliy")
        test = db.get_test(r["id"])
        await self.post(f"/api/teacher/test/{test['id']}/schedule", {"date": "2026-11-20", "opens": "20:00", "entry": "22:00",
                                                                     "closes": "23:00", "public": True, "price_ai": 15000, "price_teacher": 12000})
        self.at(20, 30)
        self.give_history(OTHER)
        db.wallet_add(OWNER, OTHER, 2)
        self.as_user(OTHER)
        err = (await self.get(f"/api/test/{test['code']}"))[1]["mock_choice"]
        self.assertEqual(([(o["key"], o["credits"]) for o in err["options"]], err["balance"]), ([("ai", 3), ("teacher", 3)], 2))
        status, res = await self.post(f"/api/test/{test['code']}/mock-choice", {"choice": "ai", "pay": "credit"})
        self.assertEqual(status, 409)  # 3 kredit kerak, 2 ta bor — hech narsa yechilmaydi
        self.assertEqual(db.wallet_balance(OWNER, OTHER), 2)
        db.wallet_add(OWNER, OTHER, 3)
        status, res = await self.post(f"/api/test/{test['code']}/mock-choice", {"choice": "ai", "pay": "credit"})
        self.assertEqual((status, res["status"], res["credits"], res["balance"]), (200, "paid", 3, 2))
        self.assertEqual((await self.get(f"/api/test/{test['code']}"))[0], 200)  # mock ochildi
        self.assertTrue(await webapp.essay_ai_ok(test, OTHER))  # AI tekshiradi

        # O'z o'quvchilari havolasi: kirgan o'quvchi ustozga ulanadi — mock bepul
        self.as_user(OWNER)
        webapp.HOOKS["bot_username"] = lambda: asyncio.sleep(0, "test_bot")
        link = (await self.get(f"/api/teacher/test/{test['id']}"))[1]["own_link"]
        self.assertIn("start=mt_" + test["code"], link)
        code = link.split("start=")[1]
        self.assertEqual(webapp.test_from_own_code(code), test["code"])
        self.assertIsNone(webapp.test_from_own_code(code[:-1] + ("0" if code[-1] != "0" else "1")))  # soxta imzo
        answers = []

        async def answer(text, **_):
            answers.append(text)
        message = types.SimpleNamespace(text=f"/start {code}", answer=answer,
                                        from_user=types.SimpleNamespace(id=555, full_name="Yangi O'quvchi", username=None))
        await bot.cmd_start(message, types.SimpleNamespace(clear=lambda: asyncio.sleep(0)))
        self.assertTrue(db.is_linked(OWNER, 555))
        self.assertIn("bepul", answers[-1])
        self.as_user(555)
        err = (await self.get(f"/api/test/{test['code']}"))[1]["mock_choice"]
        self.assertEqual([o["price"] for o in err["options"]], [0, 0])

        # Claude: balans $10, 2 ta esse $0,05 dan — $9,90 qoldi ≈ 198 ta esse
        self.as_user(OWNER)
        await self.post("/api/teacher/settings", {"claude_balance": "10"})
        for _ in range(2):
            db.record_ai_usage("essay", "claude-sonnet-5-5", {"in": 1000, "out": 500, "cost": 0.05})
        c = (await self.get("/api/bootstrap"))[1]["claude"]
        self.assertEqual((c["usd"], c["per_essay"], c["essays_left"], c["checked"]), (9.9, 0.05, 198, 2))
        self.assertEqual((await self.get("/api/bootstrap"))[1]["default_channel"], "@Til_va_adabiyot_Xalilov")

    async def test_paying_student_not_locked_out_after_entry(self):
        """Kirish vaqtida to'lovni boshlagan o'quvchi — chek tekshirilguncha kirish vaqti o'tsa ham mockka kiradi.
        Ochmagan o'quvchi kirish vaqtidan keyin to'lay olmaydi (kredit ham yechilmaydi); ochilmasdan oldin ham."""
        await self.post("/api/teacher/settings", {"card": "8600123456789012", "holder": "Ustoz Aliyev"})
        await self.post("/api/teacher/packs", {"packs": [{"essays": 1, "price": 5000}]})
        _, r = await self.create("online", fmt="milliy")
        test = db.get_test(r["id"])
        await self.post(f"/api/teacher/test/{test['id']}/schedule", {"date": "2026-11-20", "opens": "20:00", "entry": "22:00",
                                                                     "closes": "23:00", "public": True, "price_ai": 15000})
        for uid in (OTHER, STUDENT):
            self.give_history(uid)  # birinchi mock bepul emas
        self.as_user(OTHER)
        self.assertEqual((await self.post(f"/api/test/{test['code']}/mock-choice", {"choice": "ai"}))[0], 409)  # hali ochilmagan

        self.at(21, 55)  # kirishga 5 daqiqa qolganda ochdi va to'lovni boshladi
        self.assertEqual((await self.get(f"/api/test/{test['code']}"))[0], 402)
        self.assertEqual((await self.post(f"/api/test/{test['code']}/mock-choice", {"choice": "ai"}))[1]["status"], "open")
        self.at(22, 5)  # chek tekshirilguncha kirish vaqti o'tdi
        self.assertEqual((await self.get(f"/api/test/{test['code']}"))[0], 402)  # «Kirish vaqti tugagan» emas
        db.decide_essay_payment(db.essay_payment_of(test["id"], OTHER)["id"], "paid", ("open",))
        self.assertEqual((await self.get(f"/api/test/{test['code']}"))[0], 200)
        status, res = await self.post(f"/api/test/{test['code']}/submit", {"name": "Begona Bola", "answers": {}, "essay": self.ESSAY})
        self.assertEqual(status, 200, res)

        # Kirish vaqtida ochmagan o'quvchi: kirolmaydi, to'lay olmaydi, krediti yechilmaydi
        self.as_user(STUDENT)
        db.wallet_add(OWNER, STUDENT, 5)
        self.assertEqual((await self.get(f"/api/test/{test['code']}"))[0], 409)
        self.assertEqual((await self.post(f"/api/test/{test['code']}/mock-choice", {"choice": "ai", "pay": "credit"}))[0], 409)
        self.assertEqual(db.wallet_balance(OWNER, STUDENT), 5)
        self.assertEqual((await self.post(f"/api/test/{test['code']}/essay-pay", {}))[0], 409)  # jadvalli mockda — faqat tanlov

    async def test_upcoming_mock_shown_before_closed_ones(self):
        """Bosh sahifa: bugun yopilgan 3 ta mock bo'lsa ham kechqurungi (bo'lajak) mock kartasi chiqadi — birinchi bo'lib."""
        items = structure.build_items("milliy", 45)
        key = {"A": {i["label"]: "A" for i in items}}
        for hour in (8, 10, 12):
            done = db.create_test(OWNER, f"Ertalabki {hour}", "milliy", 45, items, key)
            db.set_schedule(done["id"], f"2026-11-20 {hour:02d}:00", f"2026-11-20 {hour:02d}:30", f"2026-11-20 {hour + 1:02d}:00", True)
        evening = db.create_test(OWNER, "Kechki mock", "milliy", 45, items, key)
        db.set_schedule(evening["id"], "2026-11-20 20:00", "2026-11-20 22:00", "2026-11-20 23:00", True)
        self.as_user(OTHER)
        mocks = (await self.get("/api/bootstrap"))[1]["mocks"]
        self.assertEqual([m["title"] for m in mocks], ["Kechki mock", "Ertalabki 12", "Ertalabki 10"])

    async def test_channel_post_falls_back_to_required_channel(self):
        """E'lon kanali sozlanmagan — e'lon mockning majburiy obuna kanaliga; tugma «Mockda qatnashish»."""
        _, r = await self.create("online", fmt="milliy", channels=["@Til_va_adabiyot_Xalilov"])
        test = db.get_test(r["id"])
        self.assertEqual([c["chat_id"] for c in test["channels"]], [-100])  # ServerBase: soxta kanal -100
        await self.post(f"/api/teacher/test/{test['id']}/schedule", {"date": "2026-11-20", "opens": "20:00", "entry": "22:00",
                                                                     "closes": "23:00", "public": True})
        buttons = []

        class ChannelBot(FakeBot):
            async def send_message(self, chat_id, text, reply_markup=None, **_):
                self.messages.append((chat_id, text))
                if reply_markup:
                    buttons.extend(b.text for row in reply_markup.inline_keyboard for b in row)

        channel_bot = ChannelBot()

        async def bot_username(_bot):
            return "test_bot"
        saved, bot.bot_username = bot.bot_username, bot_username
        try:
            await bot.mock_tick(channel_bot)
        finally:
            bot.bot_username = saved
        posts = [m for c, m in channel_bot.messages if c == -100 and len(m) > 4]
        self.assertEqual(len(posts), 1)
        self.assertIn("Bugun 20:00 da", posts[0])
        self.assertEqual(buttons, ["✍️ Mockda qatnashish"])

class ScanResults(ServerBase):
    """Titul natijalari: surat belgilar bilan, ism, boshqa o'quvchiga ko'chirish, o'chirish, qo'lda kiritish, A/B/C/D tahlili."""

    async def upload(self, test, payload, photo=None, marks=None):
        from aiohttp import FormData
        form = FormData()
        form.add_field("data", json.dumps(payload))
        if photo:
            form.add_field("photo", photo, filename="titul.jpg", content_type="image/jpeg")
        if marks:
            form.add_field("marks", json.dumps(marks))
        response = await self.client.post(f"/api/teacher/test/{test['id']}/scan/upload", data=form)
        return response.status, await response.json()

    async def test_sheet_edit_move_delete_manual(self):
        _, r = await self.create("offline")
        test = db.get_test(r["id"])
        db.add_students(test["id"], ["Ali Valiyev", "Vali Karimov"], ["A", "A"], [7, 8])

        class AlbumBot(FakeBot):
            async def send_photo(self, chat_id, photo, **_):
                return types.SimpleNamespace(photo=[types.SimpleNamespace(file_id="TG-PHOTO-1")])
        album = AlbumBot()
        webapp.HOOKS["scan_photos"] = lambda c, t, items: bot.send_scan_photos(album, c, t, items)
        webapp.HOOKS["download_file"] = lambda file_id: asyncio.sleep(0, b"\xff\xd8 rasm " + file_id.encode())

        marks = {"positions": {"1": {"A": [10, 20, 4], "B": [20, 20, 4]}}, "page_w": 595}
        _, res = await self.upload(test, {"uid": "u1", "number": "7", "variant": "A", "answers": {"1": "A", "2": "B"}},
                                   b"\xff\xd8 titul", marks)
        attempt_id = res["attempt_id"]
        self.assertEqual((await self.get(f"/api/teacher/attempt/{attempt_id}/sheet"))[1], {"pending": True})  # albom to'lmagan
        await self.post(f"/api/teacher/test/{test['id']}/scan/upload/done", {"saved": 1})
        await asyncio.sleep(0.05)
        status, sheet = await self.get(f"/api/teacher/attempt/{attempt_id}/sheet")
        self.assertEqual((status, sheet["marks"]), (200, marks))
        photo = await self.client.get(sheet["url"])
        self.assertEqual((photo.status, await photo.read()), (200, b"\xff\xd8 rasm TG-PHOTO-1"))

        # Ism tuzatish — natijada ham, ro'yxatda ham
        await self.post(f"/api/teacher/attempt/{attempt_id}/rename", {"name": "Ali  Valiyevich"})
        self.assertEqual(db.get_attempt(attempt_id)["student_name"], "Ali Valiyevich")
        self.assertEqual(db.get_student(test["id"], None, 7)["name"], "Ali Valiyevich")

        # ID xato o'qilgan: natija 8-ga; 7 bo'shaydi
        status, moved = await self.post(f"/api/teacher/attempt/{attempt_id}/move", {"number": "8"})
        self.assertEqual((status, moved["name"]), (200, "Vali Karimov"))
        self.assertEqual(db.get_student(test["id"], None, 8)["attempt_id"], attempt_id)
        self.assertIsNone(db.get_student(test["id"], None, 7)["attempt_id"])
        self.assertEqual(db.get_attempt(attempt_id)["tg_id"], db.offline_tg_id(test["id"], 8))

        # 7-ga qo'lda kiritish, keyin 8-ning natijasini 7-ga ko'chirmoqchi — band: avval so'raydi, keyin almashtiradi
        seven = db.get_student(test["id"], None, 7)
        status, manual = await self.post(f"/api/teacher/test/{test['id']}/scan/save",
                                         {"student_id": seven["id"], "variant": "A", "answers": {"1": "B", "2": "A"}})
        self.assertEqual(status, 200, manual)
        status, busy = await self.post(f"/api/teacher/attempt/{attempt_id}/move", {"number": "7"})
        self.assertEqual((status, busy.get("busy")), (409, True))
        status, _ = await self.post(f"/api/teacher/attempt/{attempt_id}/move", {"number": "7", "replace": True})
        self.assertEqual(status, 200)
        self.assertIsNone(db.get_attempt(manual["attempt_id"]))
        self.assertEqual(db.get_student(test["id"], None, 7)["attempt_id"], attempt_id)

        # A/B/C/D tahlili: 2-savolga hammasi B (kalit — A) — «kalitni tekshiring»
        eight = db.get_student(test["id"], None, 8)
        await self.post(f"/api/teacher/test/{test['id']}/scan/save", {"student_id": eight["id"], "variant": "A", "answers": {"1": "A", "2": "B"}})
        page = (await self.get(f"/api/teacher/test/{test['id']}"))[1]
        q1, q2 = page["analysis"][0], page["analysis"][1]
        self.assertEqual((q1["dist"]["A"], q1["key"], q1["suspicious"]), (2, "A", False))
        self.assertEqual((q2["dist"]["B"], q2["top_wrong"], q2["suspicious"]), (2, "B", True))
        self.assertEqual(q2["blank"], 0)

        # O'chirish — natija yo'q, o'quvchi ro'yxatda qoladi
        status, _ = await self.post(f"/api/teacher/attempt/{attempt_id}/delete", {})
        self.assertEqual(status, 200)
        self.assertIsNone(db.get_attempt(attempt_id))
        self.assertIsNone(db.get_student(test["id"], None, 7)["attempt_id"])


class RevenueShare(ServerBase):
    """Begona o'qituvchi (admin / FREE_IDS emas): begona o'quvchi puli PLATFORMA kartasiga, o'qituvchiga 20% balans,
    esse narxi kamida 10 000; chekni faqat admin tasdiqlaydi; balans 100 000 ga yetsa — «💸 Yechib olish» → admin."""

    ADMIN = 999

    async def asyncSetUp(self):
        await super().asyncSetUp()
        webapp.ADMIN_IDS.add(self.ADMIN)
        db.set_setting("card_number", "9860 1111 2222 3333")
        db.set_setting("card_holder", "Platforma Egasi")
        self._enabled, self._essay_enabled, self._read = essay_ai.enabled, essay_ai.essay_enabled, essay_ai.read_receipt
        essay_ai.enabled = essay_ai.essay_enabled = lambda: True
        self.receipt = {}

        async def fake_read(image, mime="image/jpeg"):
            return dict(self.receipt)
        essay_ai.read_receipt = fake_read
        self.notices = []

        async def notice(*args):
            self.notices.append(args)
        webapp.HOOKS["essay_receipt_notice"] = webapp.HOOKS["wallet_receipt_notice"] = notice
        webapp.HOOKS["withdraw_notice"] = lambda wid: bot.withdraw_notice(self.bot, wid)
        webapp.HOOKS["bot_username"] = lambda: asyncio.sleep(0, "test_bot")

    async def asyncTearDown(self):
        webapp.ADMIN_IDS.discard(self.ADMIN)
        essay_ai.enabled, essay_ai.essay_enabled, essay_ai.read_receipt = self._enabled, self._essay_enabled, self._read
        await super().asyncTearDown()

    def receipt_for(self, amount, card="9860 **** **** 3333", txn="TX1"):
        self.receipt = {"is_receipt": True, "success": True, "amount": amount, "date": plans.now().strftime("%Y-%m-%d"),
                        "time": "10:00", "recipient_card": card, "recipient_name": "", "transaction_id": txn,
                        "looks_edited": False, "note": ""}

    async def mock(self, price):
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        items = structure.build_items("milliy", 45)
        key = {i["label"]: ("A" if i["kind"] == "choice" else "kitob") for i in items}
        return await self.post("/api/teacher/tests", {"title": "Mock", "fmt": "milliy", "mode": "online", "keys": {"A": key},
                                                      "channels": [], "essay_price": price, "card": "8600 1234 5678 9012",
                                                      "holder": "Nodira Karimova"})

    def test_who_is_routed(self):
        self.assertTrue(plans.platform_routed(OWNER))  # obuna olgan oddiy o'qituvchi
        self.assertFalse(plans.platform_routed(self.ADMIN))  # admin / FREE_IDS — pul o'z kartasiga
        self.assertEqual(plans.teacher_cut(10_000), 2_000)
        db.set_setting("platform_share", "0")  # o'chiq — avvalgidek
        self.assertFalse(plans.platform_routed(OWNER))

    async def test_mock_essay_paid_to_platform_teacher_gets_share(self):
        self.assertEqual((await self.mock(5000))[0], 400)  # kamida 10 000
        status, r = await self.mock(15000)
        self.assertEqual(status, 200, r)
        test = db.get_test(r["id"])
        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertEqual(boot["revshare"], {"teacher_percent": 20, "min_price": 10_000})

        self.user = {"id": OTHER, "name": "Begona", "username": "b"}
        info = await (await self.client.get(f"/api/test/{test['code']}")).json()
        self.assertEqual((info["essay_pay"]["price"], info["essay_pay"]["card"]), (15000, "9860 1111 2222 3333"))
        status, res = await self.post(f"/api/test/{test['code']}/essay-pay", {})
        self.assertEqual(status, 200, res)
        self.assertIn("9860 1111 2222 3333", self.bot.messages[-1][1])  # ustoz kartasi emas — platformaniki
        pay = db.open_essay_payment(OTHER)
        self.assertEqual(pay["payee"], "platform")

        reads = []
        essay_ai.read_receipt = lambda *a, **k: reads.append(a) or asyncio.sleep(0, dict(self.receipt))
        self.receipt_for(15000)  # to'g'ri chek ham — AI o'qimaydi (faqat admin / FREE_IDS ustozlarida)
        self.assertEqual((await webapp.check_essay_receipt(pay["id"], b"chek-0", "image/jpeg"))["status"], "review")
        self.assertEqual(reads, [])
        self.assertIn("Ustoz ko'rib chiqadi", self.bot.messages[-1][1])
        self.assertEqual(db.earnings_balance(OWNER), 0)

        # Ustoz o'zi tasdiqlaydi (ilovada): 20% balansiga, nusxasi adminlarga «Bekor qilish» bilan
        copies = []

        async def copy(kind, payment_id):
            copies.append((kind, payment_id))
        webapp.HOOKS["platform_approved_notice"] = copy
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        listed = (await self.get("/api/teacher/essay-payments"))[1]["payments"]
        self.assertEqual((listed[0]["payee"], listed[0]["status"]), ("platform", "review"))
        self.assertEqual((await self.post(f"/api/teacher/essay-payment/{pay['id']}", {"action": "approve"}))[0], 200)
        self.assertEqual((db.earnings_balance(OWNER), copies), (3000, [("essay", pay["id"])]))  # 15 000 × 20%
        await webapp.decide_essay_payment(pay["id"], False, self.ADMIN)  # admin bekor qildi — ulush ham olinadi
        self.assertEqual(db.earnings_balance(OWNER), 0)
        await webapp.decide_essay_payment(pay["id"], True, self.ADMIN)  # admin tasdiqladi — nusxa yuborilmaydi
        self.assertEqual(len(copies), 1)
        await webapp.credit_share("essay", db.get_essay_payment(pay["id"]), OWNER)  # takror chaqiriq
        self.assertEqual(db.earnings_balance(OWNER), 3000)  # bitta to'lov — bir marta

        # Bot: ustozga chek «bot kartasiga» izohi bilan; adminga tasdiqlangan nusxa «Bekor qilish» bilan
        self.assertIn("80% — bot hisobiga", bot.platform_check_text(15000))
        self.assertIn("•••• 3333", bot.platform_check_text(15000))
        await bot.platform_approved_notice(self.bot, "essay", pay["id"])
        self.assertIn(self.ADMIN, [chat for chat, _ in self.bot.photos])

    async def test_ai_approved_receipt_shares_and_old_cheap_price_raised(self):
        status, r = await self.mock(15000)
        test = db.get_test(r["id"])
        db.set_test_price(test["id"], 5000)  # oldin qo'yilgan arzon narx — minimalga ko'tariladi
        self.user = {"id": OTHER, "name": "Begona", "username": "b"}
        info = await (await self.client.get(f"/api/test/{test['code']}")).json()
        self.assertEqual(info["essay_pay"]["price"], 10_000)
        await self.post(f"/api/test/{test['code']}/essay-pay", {})
        pay = db.open_essay_payment(OTHER)
        self.assertEqual(pay["amount"], 10_000)
        self.receipt_for(10_000)
        self.assertEqual((await webapp.check_essay_receipt(pay["id"], b"chek-1", "image/jpeg"))["status"], "review")
        await webapp.decide_essay_payment(pay["id"], True, OWNER)  # ustoz tasdiqladi
        self.assertEqual(db.earnings_balance(OWNER), 2000)
        credits = db.get_credits(OWNER)
        self.assertTrue(await webapp.essay_ai_ok(test, OTHER))
        self.assertEqual(db.get_credits(OWNER), credits)  # AI xarajati platformadan — ustoz krediti yechilmadi

    async def test_credit_packs_min_price_and_share(self):
        status, r = await self.post("/api/teacher/packs", {"price": 5000, "packs": []})
        self.assertEqual(status, 400, r)
        status, r = await self.post("/api/teacher/packs", {"packs": [{"essays": 1, "price": 12000}, {"essays": 5, "price": 40000}]})
        self.assertEqual(status, 400, r)  # 5 ta — kamida 50 000
        self.assertIn("50 000", r["error"])
        status, r = await self.post("/api/teacher/packs", {"packs": [{"essays": 1, "price": 12000}, {"essays": 5, "price": 50000}]})
        self.assertEqual(status, 200, r)
        db.set_setting(f"essay_price:{OWNER}", "3000")  # oldingi arzon narx — sotuvda minimalga ko'tariladi
        self.assertEqual(webapp.essay_packs(OWNER)[0]["price"], 10_000)

        self.user = {"id": OTHER, "name": "Begona", "username": "b"}
        order = await webapp.wallet_order(OWNER, self.user, 5)
        self.assertEqual((order["card"]["number"], order["payment"]["payee"]), ("9860 1111 2222 3333", "platform"))
        self.receipt_for(50000)
        self.assertEqual((await webapp.check_wallet_receipt(order["payment"]["id"], b"chek-2", "image/jpeg"))["status"], "review")
        self.assertEqual((db.wallet_balance(OWNER, OTHER), db.earnings_balance(OWNER)), (0, 0))
        await webapp.decide_wallet_payment(order["payment"]["id"], True, OWNER)  # ustoz tasdiqladi
        self.assertEqual((db.wallet_balance(OWNER, OTHER), db.earnings_balance(OWNER)), (5, 10_000))
        await webapp.decide_wallet_payment(order["payment"]["id"], False, self.ADMIN)  # admin: pul tushmagan
        self.assertEqual((db.wallet_balance(OWNER, OTHER), db.earnings_balance(OWNER)), (0, 0))

        # Kredit evaziga ommaviy havola essesi: ustozning AI limiti yechilmaydi (puli platformada)
        db.wallet_add(OWNER, OTHER, 1)
        practice_id = db.create_practice(OWNER, OTHER, None, "matn " * 80, "public", None, "awaiting_credit")
        used = db.get_usage(OWNER, plans.month_key())["essays"]
        self.assertEqual(await webapp.release_waiting(OWNER, OTHER), (1, False))
        self.assertEqual(db.get_practice(practice_id)["status"], "pending")
        self.assertEqual(db.get_usage(OWNER, plans.month_key())["essays"], used)

    async def test_withdraw_flow(self):
        self.assertEqual((await self.post("/api/teacher/withdraw", {}))[0], 400)  # karta yo'q
        await self.post("/api/teacher/settings", {"card": "8600 1234 5678 9012", "holder": "Nodira Karimova"})
        db.add_earning(OWNER, "essay", 1, OTHER, 250_000, 50_000)
        status, r = await self.post("/api/teacher/withdraw", {})
        self.assertEqual(status, 409, r)  # 100 000 dan kam
        self.assertIn("100 000", r["error"])
        settings = (await self.get("/api/teacher/settings"))[1]
        self.assertEqual((settings["earnings"]["balance"], settings["earnings"]["can_withdraw"]), (50_000, False))

        db.add_earning(OWNER, "wallet", 7, OTHER, 350_000, 70_000)
        self.assertFalse(db.add_earning(OWNER, "wallet", 7, OTHER, 350_000, 70_000))  # takror yozilmaydi
        status, r = await self.post("/api/teacher/withdraw", {})
        self.assertEqual((status, r["amount"]), (200, 120_000), r)
        self.assertEqual(r["earnings"]["pending"]["amount"], 120_000)
        self.assertEqual((r["earnings"]["balance"], r["earnings"]["can_withdraw"]), (0, False))
        self.assertEqual((await self.post("/api/teacher/withdraw", {}))[0], 409)  # oldingisi kutilmoqda
        admin_msg = [m for m in self.bot.messages if m[0] == self.ADMIN]
        self.assertTrue(admin_msg and "120 000" in admin_msg[-1][1] and "8600 1234 5678 9012" in admin_msg[-1][1])

        wid = db.pending_withdrawals()[0]["id"]
        with self.assertRaises(webapp.ApiError):  # o'qituvchi o'zi «to'landi» deya olmaydi
            await webapp.decide_withdrawal(wid, True, OWNER)
        await webapp.decide_withdrawal(wid, False, self.ADMIN)  # rad — summa balansga qaytadi
        self.assertEqual(db.earnings_balance(OWNER), 120_000)
        self.assertIn("rad etildi", self.bot.messages[-1][1])
        status, r = await self.post("/api/teacher/withdraw", {})
        self.assertEqual(status, 200, r)
        wid = db.pending_withdrawals()[0]["id"]
        await webapp.decide_withdrawal(wid, True, self.ADMIN)
        self.assertEqual(self.bot.messages[-1][0], OWNER)
        self.assertIn("120 000 so'm kartangizga o'tkazildi", self.bot.messages[-1][1])
        with self.assertRaises(webapp.ApiError):  # ikkinchi marta — allaqachon ko'rilgan
            await webapp.decide_withdrawal(wid, True, self.ADMIN)
        summary = (await self.get("/api/teacher/settings"))[1]["earnings"]
        self.assertEqual((summary["balance"], summary["total"], [w["status"] for w in summary["history"]]),
                         (0, 120_000, ["paid", "rejected"]))
        self.assertEqual(db.platform_income(plans.month_key()), {"count": 2, "amount": 600_000, "share": 120_000, "net": 480_000})

    async def test_admin_panel_in_app(self):
        """Mini app «💎 Obuna tariflari»: «💰 Begonalar puli» — sozlamalar, shu oy tushum, yechish so'rovlari tugmalari."""
        self.assertEqual((await self.get("/api/admin/sales"))[0], 403)  # oddiy o'qituvchi
        db.set_setting(f"teacher_card:{OWNER}", "8600 1234 5678 9012")
        db.add_earning(OWNER, "essay", 1, OTHER, 600_000, 120_000)
        self.assertEqual((await self.post("/api/teacher/withdraw", {}))[0], 200)

        self.user = {"id": self.ADMIN, "name": "Admin", "username": None}
        rs = (await self.get("/api/admin/sales"))[1]["revshare"]
        self.assertEqual((rs["platform_share"], rs["min_essay_price"], rs["withdraw_min"]), (80, 10_000, 100_000))
        self.assertEqual((rs["income"]["net"], rs["withdrawals"][0]["amount"]), (480_000, 120_000))
        status, r = await self.post("/api/admin/sales", {"revshare": {"platform_share": 101, "min_essay_price": 0, "withdraw_min": 5000}})
        self.assertEqual(status, 400, r)
        status, r = await self.post("/api/admin/sales", {"revshare": {"platform_share": 70, "min_essay_price": 12000, "withdraw_min": 50000}})
        self.assertEqual((status, r["revshare"]["platform_share"]), (200, 70), r)
        self.assertEqual((plans.teacher_cut(10_000), plans.min_essay_price(), plans.withdraw_min()), (3000, 12000, 50000))

        wid = rs["withdrawals"][0]["id"]
        status, r = await self.post(f"/api/admin/withdrawal/{wid}", {"action": "paid"})
        self.assertEqual((status, r["revshare"]["withdrawals"]), (200, []), r)
        self.assertIn("kartangizga o'tkazildi", self.bot.messages[-1][1])
        self.assertEqual((await self.post(f"/api/admin/withdrawal/{wid}", {"action": "paid"}))[0], 409)
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}
        self.assertEqual((await self.post(f"/api/admin/withdrawal/{wid}", {"action": "reject"}))[0], 403)
