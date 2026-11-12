"""
Tuzatishlar: ilovaga kirish faqat tugma bilan, oflayn esseni o'qituvchi bekor qilishi, e'lon takrorlanmasligi
(fonda, flood'da qayta urinadi), ommaviy tasdiqlashda o'quvchiga xabar, noto'g'ri son — 400, yangi testda PDF'lar
va oflayn testda majburiy obuna yo'qligi. Telegram almashtirilgan — internet kerak emas.
"""
import asyncio
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


class ServerFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        plans.grant(OWNER, "month")
        self.bot = FakeBot()
        self.user = {"id": OWNER, "name": "Ustoz", "username": None}

        async def fake_auth(_request):
            return self.user

        self._auth = webapp.auth
        webapp.auth = fake_auth
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


class Names(unittest.TestCase):
    def test_names_match(self):
        self.assertTrue(webapp.names_match("valiyev ali", "Ali Valiyev"))
        self.assertTrue(webapp.names_match("Gʻulomov Aziz", "G'ulomov Aziz"))
        self.assertFalse(webapp.names_match("Hacker Boy", "Ali Valiyev"))
        self.assertIsNone(webapp.names_match("Ali", None))


if __name__ == "__main__":
    unittest.main()
