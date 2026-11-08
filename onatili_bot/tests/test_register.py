"""Ro'yxatdan o'tish: rol (o'quvchi/o'qituvchi), viloyat-tuman tanlash va tuman bo'yicha yo'naltirish."""
import unittest

import _env

from aiohttp.test_utils import TestClient, TestServer

import db
import places
import webapp

STUDENT, TEACHER, OTHER = 801, 802, 803
REGION, DISTRICT = "Toshkent shahri", "Chilonzor"


class Register(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        self.notices = []
        self.who = {"id": STUDENT, "name": "Ali Valiyev", "username": "ali_v"}

        async def fake_auth(_request):
            return self.who

        async def notice(reg, username, again, teachers):
            self.notices.append({"name": reg["full_name"], "role": reg["role"], "again": again,
                                 "teachers": [t["tg_id"] for t in teachers]})

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
        return {"role": "student", "first_name": "Ali", "last_name": "Valiyev", "phone": "+998 90 123 45 67",
                "age": 16, "places": [{"region": REGION, "district": DISTRICT}], **changes}

    async def teacher_in(self, tg_id, districts, region=REGION):
        """O'qituvchi o'ziga joy ochadi."""
        self.who = {"id": tg_id, "name": "Ustoz", "username": "ustoz"}
        r = await self.client.post("/api/register", json=self.form(
            role="teacher", first_name="Ustoz", last_name="Aliyev", age=None,
            places=[{"region": region, "district": d} for d in districts]))
        self.assertEqual(r.status, 200, await r.text())
        self.who = {"id": STUDENT, "name": "Ali Valiyev", "username": "ali_v"}
        return await r.json()

    async def test_places_list_and_courses_marked(self):
        first = await (await self.client.get("/api/register")).json()
        self.assertIsNone(first["registration"])
        self.assertEqual(first["regions"], places.REGIONS)  # ilova ro'yxatni serverdan oladi
        self.assertEqual(first["with_courses"], [])

        await self.teacher_in(TEACHER, [DISTRICT, "Yunusobod"])
        marked = await (await self.client.get("/api/register")).json()
        self.assertEqual({(p["region"], p["district"]) for p in marked["with_courses"]},
                         {(REGION, DISTRICT), (REGION, "Yunusobod")})

    async def test_student_goes_to_teachers_in_same_district(self):
        await self.teacher_in(TEACHER, [DISTRICT])
        await self.teacher_in(OTHER, ["Sergeli"])  # boshqa tuman — unga bormaydi

        r = await (await self.client.post("/api/register", json=self.form())).json()
        self.assertEqual(r["teachers"], 1)
        self.assertEqual(self.notices[-1]["teachers"], [TEACHER])
        saved = r["registration"]
        self.assertEqual((saved["role"], saved["region"], saved["district"]), ("student", REGION, DISTRICT))

    async def test_student_without_teacher_still_saved(self):
        r = await (await self.client.post("/api/register", json=self.form())).json()
        self.assertEqual(r["teachers"], 0)  # tumanda o'qituvchi yo'q — faqat adminga boradi
        self.assertEqual(db.count_registrations(), 1)

    async def test_teacher_places_replaced_on_resend(self):
        await self.teacher_in(TEACHER, [DISTRICT, "Yunusobod"])
        self.assertEqual(len(db.list_teacher_places(TEACHER)), 2)
        again = await self.teacher_in(TEACHER, ["Sergeli"])  # qayta yuborsa — eskisi qolmaydi
        self.assertTrue(again["again"])
        self.assertEqual([p["district"] for p in db.list_teacher_places(TEACHER)], ["Sergeli"])
        self.assertEqual(db.teachers_in(REGION, DISTRICT), [])
        self.assertIsNone(db.get_registration(TEACHER)["age"])  # o'qituvchidan yosh so'ralmaydi

    async def test_student_keeps_one_place(self):
        r = await (await self.client.post("/api/register", json=self.form(
            places=[{"region": REGION, "district": DISTRICT}, {"region": REGION, "district": "Sergeli"}]))).json()
        self.assertEqual(r["registration"]["district"], DISTRICT)
        self.assertEqual(db.list_teacher_places(STUDENT), [])  # o'quvchi joy ochmaydi

    async def test_validation(self):
        bads = [({"first_name": "A"}, "Ism"), ({"last_name": ""}, "Familiya"), ({"phone": "12"}, "Telefon"),
                ({"age": 3}, "Yosh"), ({"places": []}, "Yashash joyingizni"),
                ({"places": [{"region": "Yo'q viloyat", "district": "Yo'q tuman"}]}, "Yashash joyingizni"),
                ({"role": "teacher", "age": None, "places": []}, "Dars beradigan")]
        for bad, message in bads:
            r = await self.client.post("/api/register", json=self.form(**bad))
            self.assertEqual(r.status, 400, bad)
            self.assertIn(message, (await r.json())["error"])
        self.assertEqual(db.count_registrations(), 0)
        self.assertEqual(self.notices, [])

    async def test_bootstrap_flag(self):
        self.assertFalse((await (await self.client.get("/api/bootstrap")).json())["registered"])
        await self.client.post("/api/register", json=self.form())
        self.assertTrue((await (await self.client.get("/api/bootstrap")).json())["registered"])


if __name__ == "__main__":
    unittest.main()
