"""Obuna: bepul sinov, tarif berish/uzaytirish, oylik AI limiti, muddati tugaganda test yaratib bo'lmasligi."""
import asyncio
import json
import unittest
from datetime import timedelta

import _env

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import bot
import db
import ops
import plans
import structure
import webapp

TEACHER, ADMIN = 701, 999


class Plans(unittest.TestCase):
    def setUp(self):
        _env.fresh_db()
        self.saved = set(plans.ADMIN_IDS)
        plans.ADMIN_IDS.clear()
        plans.ADMIN_IDS.add(ADMIN)

    def tearDown(self):
        plans.ADMIN_IDS.clear()
        plans.ADMIN_IDS.update(self.saved)

    def test_trial_starts_on_first_test(self):
        self.assertFalse(plans.status(TEACHER)["active"])
        st = plans.ensure_active(TEACHER)
        self.assertEqual((st["plan"], st["active"], st["days_left"]), ("trial", True, plans.TRIAL_DAYS))

    def test_monthly_ai_limit(self):
        plans.ensure_active(TEACHER)
        limit = plans.PLANS["trial"]["essays"]
        self.assertTrue(all(plans.use_ai(TEACHER, "essays") for _ in range(limit)))
        self.assertFalse(plans.use_ai(TEACHER, "essays"))  # limit tugadi
        self.assertTrue(plans.use_ai(TEACHER, "written"))  # titul limiti alohida
        self.assertEqual(plans.status(TEACHER)["used"]["essays"], limit)

    def test_grant_extends_and_admin_unlimited(self):
        st = plans.grant(TEACHER, "pro", 1)
        self.assertEqual((st["plan"], st["days_left"]), ("pro", 30))
        st = plans.grant(TEACHER, "pro", 2)  # faol obuna — muddat oxiriga qo'shiladi
        self.assertEqual(st["days_left"], 90)
        with self.assertRaises(ValueError):
            plans.grant(TEACHER, "nomalum", 1)
        self.assertTrue(plans.status(ADMIN)["unlimited"])
        self.assertTrue(all(plans.use_ai(ADMIN, "essays") for _ in range(100)))

    def test_expired_plan_blocks_ai(self):
        plans.grant(TEACHER, "start", 1)
        db.set_subscription(TEACHER, "start", (plans.now() - timedelta(days=1)).isoformat(timespec="seconds"))
        self.assertFalse(plans.status(TEACHER)["active"])
        self.assertFalse(plans.use_ai(TEACHER, "essays"))

    def test_find_user_by_username(self):
        db.remember_user(TEACHER, "Ustoz Aliyeva", "ustoz_a")
        self.assertEqual(db.find_user("@Ustoz_A")["tg_id"], TEACHER)
        self.assertEqual(db.find_user(str(TEACHER))["tg_id"], TEACHER)
        self.assertIsNone(db.find_user("@yoq"))


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **_):
        self.messages.append((chat_id, text))


class PlanGates(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        webapp._UPLOADED.clear()
        webapp._STUDENT_OF.clear()
        self.sent = []
        self.bot = FakeBot()

        async def fake_auth(_request):
            return {"id": TEACHER, "name": "Ustoz", "username": None}

        async def send_text(chat_id, text):
            self.sent.append((chat_id, text))

        async def no_pdf(*_args):
            pass

        self._auth, self._current = webapp.auth, webapp.current_user
        webapp.auth = webapp.current_user = fake_auth
        webapp.HOOKS.update(send_text=send_text, send_pdf=no_pdf,
                            record_attempt=lambda *a, **k: bot.record_attempt(self.bot, *a, **k))
        self.client = TestClient(TestServer(webapp.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        webapp.auth, webapp.current_user = self._auth, self._current
        await self.client.close()

    def new_test(self):
        items = structure.build_items("milliy")
        test = db.create_test(TEACHER, "Mock", "milliy", 45, items, {"A": {i["label"]: "A" for i in items}})
        db.set_mode(test["id"], "offline")
        return test

    async def test_expired_teacher_cannot_create_test(self):
        payload = {"title": "Sinov", "fmt": "oddiy", "count": 5, "keys": {"A": {str(n): "A" for n in range(1, 6)}}}
        first = await self.client.post("/api/teacher/tests", json=payload)
        self.assertEqual(first.status, 200)  # birinchi test — sinov o'zi boshlandi
        self.assertEqual(db.get_subscription(TEACHER)["plan"], "trial")
        db.set_subscription(TEACHER, "trial", (plans.now() - timedelta(days=1)).isoformat(timespec="seconds"))
        second = await self.client.post("/api/teacher/tests", json=payload)
        self.assertEqual(second.status, 402)
        self.assertFalse((await second.json())["plan"]["active"])

    async def test_written_ai_stops_at_limit_with_one_notice(self):
        plans.ensure_active(TEACHER)
        test = self.new_test()
        db.consume_usage(TEACHER, plans.month_key(), "written", plans.PLANS["trial"]["written"], None)  # limit tugagan
        students = db.add_students(test["id"], ["Ali Valiyev", "Vali Aliyev"], ["A", "A"])
        answers = {i["label"]: "A" for i in structure.choice_items(test["items"])}
        for n, student in enumerate(students):
            form = FormData()
            form.add_field("data", json.dumps({"uid": f"p{n}", "student_id": student["id"], "variant": "A", "answers": answers}))
            form.add_field("written", b"\xff\xd8 rasm", filename="y.jpg", content_type="image/jpeg")
            r = await (await self.client.post(f"/api/teacher/test/{test['id']}/scan/upload", data=form)).json()
            self.assertFalse(r["written_queued"])  # AI chaqirilmaydi — natija baribir saqlandi
            self.assertTrue(r["attempt_id"])
        self.assertEqual(len([t for _, t in self.sent if "AI tekshiruvi to'xtadi" in t]), 1)  # oyiga bir marta

    async def test_bootstrap_shows_plan(self):
        plans.grant(TEACHER, "start", 1)
        r = await (await self.client.get("/api/bootstrap")).json()
        self.assertEqual(r["plan"]["name"], "Start")
        self.assertEqual([p["key"] for p in r["plans"]], plans.PAID)

    async def test_expiry_reminder_once(self):
        db.set_subscription(TEACHER, "pro", (plans.now() + timedelta(days=2, hours=12)).isoformat(timespec="seconds"))
        self.assertEqual(await ops.remind_plans(self.bot), 1)
        self.assertEqual(await ops.remind_plans(self.bot), 0)  # qayta yuborilmaydi
        self.assertIn("3 kundan keyin", self.bot.messages[0][1])


if __name__ == "__main__":
    unittest.main()
