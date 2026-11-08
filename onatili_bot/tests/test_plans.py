"""Obuna: bepul ro'yxat, sinov, muddat berish/uzaytirish, oylik AI limiti, to'lov (chek → admin tasdiqlaydi)."""
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

TEACHER, ADMIN, FREE = 701, 999, 702
TRIAL = 14


class Plans(unittest.TestCase):
    def setUp(self):
        _env.fresh_db()
        self.saved_admin, self.saved_free = set(plans.ADMIN_IDS), set(plans.FREE_IDS)
        plans.ADMIN_IDS.clear()
        plans.ADMIN_IDS.add(ADMIN)
        plans.FREE_IDS.clear()
        plans.FREE_IDS.add(FREE)
        db.set_setting("trial_days", TRIAL)

    def tearDown(self):
        plans.ADMIN_IDS.clear()
        plans.ADMIN_IDS.update(self.saved_admin)
        plans.FREE_IDS.clear()
        plans.FREE_IDS.update(self.saved_free)

    def test_trial_starts_on_first_test(self):
        self.assertFalse(plans.status(TEACHER)["active"])
        st = plans.ensure_active(TEACHER)
        self.assertEqual((st["plan"], st["active"], st["days_left"]), ("trial", True, TRIAL))

    def test_trial_off_means_no_access(self):
        db.set_setting("trial_days", 0)
        st = plans.ensure_active(TEACHER)
        self.assertFalse(st["active"])
        self.assertIsNone(db.get_subscription(TEACHER))

    def test_free_ids_are_unlimited(self):
        st = plans.status(FREE)
        self.assertEqual((st["unlimited"], st["active"], st["name"]), (True, True, "Bepul"))
        self.assertTrue(all(plans.use_ai(FREE, "essays") for _ in range(50)))
        self.assertTrue(plans.status(ADMIN)["unlimited"])

    def test_monthly_ai_limit(self):
        plans.ensure_active(TEACHER)
        limit = plans.trial_limits()["essays"]
        self.assertTrue(all(plans.use_ai(TEACHER, "essays") for _ in range(limit)))
        self.assertFalse(plans.use_ai(TEACHER, "essays"))  # limit tugadi
        self.assertTrue(plans.use_ai(TEACHER, "written"))  # titul limiti alohida
        self.assertEqual(plans.status(TEACHER)["used"]["essays"], limit)

    def test_grant_extends_and_rejects_unknown_period(self):
        st = plans.grant(TEACHER, "month", 1)
        self.assertEqual((st["plan"], st["days_left"], st["name"]), ("month", 30, "1 oy"))
        st = plans.grant(TEACHER, "oy", 2)  # faol obuna — muddat oxiriga qo'shiladi
        self.assertEqual(st["days_left"], 90)
        self.assertEqual(plans.grant(TEACHER, "hafta")["days_left"], 97)
        with self.assertRaises(ValueError):
            plans.grant(TEACHER, "nomalum", 1)

    def test_expired_plan_blocks_ai(self):
        plans.grant(TEACHER, "month", 1)
        db.set_subscription(TEACHER, "month", (plans.now() - timedelta(days=1)).isoformat(timespec="seconds"))
        self.assertFalse(plans.status(TEACHER)["active"])
        self.assertFalse(plans.use_ai(TEACHER, "essays"))

    def test_prices_and_discount_from_settings(self):
        self.assertEqual(plans.price_of("month"), plans.PERIODS["month"][2])
        db.set_setting("price_month", 100_000)
        db.set_setting("discount_month", 20)
        self.assertEqual(plans.price_of("month"), 80_000)
        offer = next(o for o in plans.offers() if o["key"] == "month")
        self.assertEqual((offer["price"], offer["final"], offer["discount"]), (100_000, 80_000, 20))

    def test_find_user_by_username(self):
        db.remember_user(TEACHER, "Ustoz Aliyeva", "ustoz_a")
        self.assertEqual(db.find_user("@Ustoz_A")["tg_id"], TEACHER)
        self.assertEqual(db.find_user(str(TEACHER))["tg_id"], TEACHER)
        self.assertIsNone(db.find_user("@yoq"))


class Payments(unittest.TestCase):
    """Karta → chek → admin tasdiqlaydi."""

    def setUp(self):
        _env.fresh_db()

    def test_one_open_payment_and_approval(self):
        first = db.create_payment(TEACHER, "month", 99_000)
        second = db.create_payment(TEACHER, "year", 950_000)  # yangisi — eskisi bekor qilinadi
        self.assertEqual(db.open_payment(TEACHER)["id"], second["id"])
        self.assertEqual(db.get_payment(first["id"])["status"], "canceled")
        db.attach_receipt(second["id"], "file-123")
        self.assertEqual(db.open_payment(TEACHER)["status"], "sent")
        self.assertTrue(db.decide_payment(second["id"], "paid", ADMIN))
        self.assertFalse(db.decide_payment(second["id"], "paid", ADMIN))  # ikkinchi admin takrorlamaydi
        self.assertIsNone(db.open_payment(TEACHER))
        self.assertEqual(db.get_payment(second["id"])["status"], "paid")

    def test_settings_roundtrip(self):
        db.set_setting("card_number", "8600 1234 5678 9012")
        db.set_setting("card_number", "8600 0000 1111 2222")  # ustiga yoziladi
        self.assertEqual(plans.card()["number"], "8600 0000 1111 2222")


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
        db.set_setting("trial_days", TRIAL)
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
        db.consume_usage(TEACHER, plans.month_key(), "written", plans.trial_limits()["written"], None)  # limit tugagan
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

    async def test_bootstrap_shows_plan_and_offers(self):
        plans.grant(TEACHER, "month", 1)
        r = await (await self.client.get("/api/bootstrap")).json()
        self.assertEqual(r["plan"]["name"], "1 oy")
        self.assertEqual([p["key"] for p in r["plans"]], list(plans.PERIODS))
        self.assertIsNone(r["payment"])

    async def test_order_requires_card_then_creates_payment(self):
        webapp.HOOKS.pop("bot_username", None)
        no_card = await self.client.post("/api/plan/order", json={"period": "month"})
        self.assertEqual(no_card.status, 400)  # karta kiritilmagan
        db.set_setting("card_number", "8600 1234 5678 9012")
        bad = await self.client.post("/api/plan/order", json={"period": "asr"})
        self.assertEqual(bad.status, 400)
        r = await (await self.client.post("/api/plan/order", json={"period": "month"})).json()
        self.assertEqual((r["payment"]["amount"], r["payment"]["status"]), (plans.price_of("month"), "pending"))
        self.assertEqual(db.open_payment(TEACHER)["period"], "month")
        self.assertIn("8600", [t for _, t in self.sent][-1])  # botga karta va summa yuborildi
        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertEqual(boot["payment"]["period"], "month")

    async def test_expiry_reminder_once(self):
        db.set_subscription(TEACHER, "month", (plans.now() + timedelta(days=2, hours=12)).isoformat(timespec="seconds"))
        self.assertEqual(await ops.remind_plans(self.bot), 1)
        self.assertEqual(await ops.remind_plans(self.bot), 0)  # qayta yuborilmaydi
        self.assertIn("3 kundan keyin", self.bot.messages[0][1])


if __name__ == "__main__":
    unittest.main()
