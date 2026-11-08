"""Oflayn «Ona tili» kursiga ro'yxatdan o'tish: ilovadagi alohida ekran → ariza adminga boradi."""
import unittest

import _env

from aiohttp.test_utils import TestClient, TestServer

import db
import webapp

STUDENT = 801


class Register(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        self.notices = []

        async def fake_auth(_request):
            return {"id": STUDENT, "name": "Ali Valiyev", "username": "ali_v"}

        async def notice(reg, username, again):
            self.notices.append((reg["full_name"], username, again))

        self._auth, self._current = webapp.auth, webapp.current_user
        webapp.auth = webapp.current_user = fake_auth
        webapp.HOOKS["registration_notice"] = notice
        self.client = TestClient(TestServer(webapp.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        webapp.auth, webapp.current_user = self._auth, self._current
        webapp.HOOKS.pop("registration_notice", None)
        await self.client.close()

    def form(self, **changes):
        return {"first_name": "Ali", "last_name": "Valiyev", "phone": "+998 90 123 45 67", "age": 16,
                "address": "Toshkent, Chilonzor", **changes}

    async def test_register_then_update(self):
        empty = await (await self.client.get("/api/register")).json()
        self.assertIsNone(empty["registration"])
        self.assertNotIn("name", empty)  # Telegram ismi formaga to'ldirilmaydi

        first = await self.client.post("/api/register", json=self.form())
        self.assertEqual(first.status, 200)
        saved = (await first.json())["registration"]
        self.assertEqual((saved["first_name"], saved["last_name"], saved["age"]), ("Ali", "Valiyev", 16))
        self.assertEqual(saved["full_name"], "Ali Valiyev")  # ro'yxat va adminga xabar uchun
        self.assertEqual(self.notices[-1], ("Ali Valiyev", "ali_v", False))  # adminga yangi ariza

        again = await (await self.client.post("/api/register", json=self.form(phone="+998 91 000 00 00"))).json()
        self.assertTrue(again["again"])
        self.assertEqual(again["registration"]["phone"], "+998 91 000 00 00")
        self.assertEqual(self.notices[-1][2], True)  # adminga «yangilandi»
        self.assertEqual(db.count_registrations(), 1)  # bitta foydalanuvchi — bitta ariza

        boot = await (await self.client.get("/api/bootstrap")).json()
        self.assertTrue(boot["registered"])

    async def test_validation(self):
        for bad, field in [({"first_name": "A"}, "Ism"), ({"last_name": ""}, "Familiya"),
                           ({"phone": "12"}, "Telefon"), ({"age": 3}, "Yosh"), ({"age": "x"}, "Yosh")]:
            r = await self.client.post("/api/register", json=self.form(**bad))
            self.assertEqual(r.status, 400, bad)
            self.assertIn(field, (await r.json())["error"])
        self.assertEqual(db.count_registrations(), 0)
        self.assertEqual(self.notices, [])

    async def test_address_optional(self):
        r = await self.client.post("/api/register", json=self.form(address=""))
        self.assertEqual(r.status, 200)
        self.assertEqual(db.get_registration(STUDENT)["address"], "")


if __name__ == "__main__":
    unittest.main()
