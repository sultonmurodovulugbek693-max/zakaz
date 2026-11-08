"""
Yozma javoblarni qo'lda tekshirish (36–44): skanerdagi rasm → savol bo'yicha ✅/❌ → ball ID bo'yicha
o'quvchi natijasiga; hamda esse DTM jadvali (24 → 75) va umumiy ball = (test + esse) / 2.
"""
import base64
import json
import unittest

import _env

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import bot
import db
import plans
import scoring
import structure
import webapp

OWNER = 901
JPEG = b"\xff\xd8\xff\xe0 yozma kataklar"


class WrittenReview(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        webapp._UPLOADED.clear()
        webapp._STUDENT_OF.clear()
        plans.grant(OWNER, "month")  # obunachi — FREE_IDS da emas, demak yozma qo'lda
        self.items = structure.build_items("milliy")
        key = {i["label"]: "A" for i in structure.choice_items(self.items)}
        key.update({t["label"]: "kitob | kitobi" for t in structure.text_items(self.items)})
        self.test = db.create_test(OWNER, "Mock", "milliy", 45, self.items, {"A": key})
        db.set_mode(self.test["id"], "offline")
        self.students = db.add_students(self.test["id"], ["Ali Valiyev", "Vali Aliyev"], ["A", "A"])
        self.answers = {i["label"]: "A" for i in structure.choice_items(self.items)}

        async def fake_auth(_request):
            return {"id": OWNER, "name": "Ustoz", "username": None}

        class FakeBot:
            async def send_message(self, *_args, **_kwargs):
                pass

        self._auth = webapp.auth
        webapp.auth = fake_auth
        webapp.HOOKS.update(record_attempt=lambda *a, **k: bot.record_attempt(FakeBot(), *a, **k))
        self.client = TestClient(TestServer(webapp.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        webapp.auth = self._auth
        await self.client.close()

    async def scan(self, student, uid):
        form = FormData()
        form.add_field("data", json.dumps({"uid": uid, "student_id": student["id"], "variant": "A", "answers": self.answers}))
        form.add_field("written", JPEG, filename="y.jpg", content_type="image/jpeg")
        r = await self.client.post(f"/api/teacher/test/{self.test['id']}/scan/upload", data=form)
        self.assertEqual(r.status, 200, await r.text())
        return (await r.json())["attempt_id"]

    async def review(self):
        r = await self.client.get(f"/api/teacher/test/{self.test['id']}/written")
        self.assertEqual(r.status, 200, await r.text())
        return await r.json()

    async def mark(self, attempt_id, label, ok):
        r = await self.client.post(f"/api/teacher/attempt/{attempt_id}/mark", json={"label": label, "ok": ok})
        self.assertEqual(r.status, 200, await r.text())
        return await r.json()

    async def test_question_by_question_marking(self):
        first = await self.scan(self.students[0], "u1")
        second = await self.scan(self.students[1], "u2")

        d = await self.review()
        labels = [t["label"] for t in structure.text_items(self.items)]
        self.assertEqual(d["labels"], labels)
        self.assertEqual([s["attempt_id"] for s in d["students"]], [first, second])  # ID tartibida
        self.assertEqual([s["number"] for s in d["students"]], [1, 2])
        self.assertTrue(all(v is None for s in d["students"] for v in s["verdict"].values()))  # hali belgilanmagan
        self.assertEqual(d["keys"]["A"]["36"], ["kitob", "kitobi"])
        self.assertEqual(len(d["boxes"]["36"]), 7)  # qatorni kesish uchun joyi va kataklar
        x, y, w, h = d["boxes"]["36"][:4]
        self.assertTrue(0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1)

        before = db.get_attempt(first)
        await self.mark(first, "36", True)  # ✅ — ball shu o'quvchiga (ID 0001) yoziladi
        after = db.get_attempt(first)
        self.assertEqual(after["written_correct"], before["written_correct"] + 1)
        self.assertAlmostEqual(after["points"], before["points"] + structure.official_points("36"), places=1)
        self.assertEqual(db.get_attempt(second)["written_correct"], 0)  # boshqa o'quvchiga tegmaydi

        r = await self.mark(second, "36", False)  # ❌
        self.assertEqual((r["verdict"], r["answer"]), (False, webapp.WRONG_MARK))
        self.assertIn("36", db.get_attempt(second)["wrong"])

        await self.mark(first, "36", False)  # ortga qaytib almashtirish: ✅ → ❌
        self.assertEqual(db.get_attempt(first)["written_correct"], before["written_correct"])
        await self.mark(first, "36", True)  # va yana ✅
        d = await self.review()
        self.assertEqual([s["verdict"]["36"] for s in d["students"]], [True, False])

        page = await (await self.client.get(f"/api/teacher/test/{self.test['id']}")).json()
        self.assertEqual(page["written_review"], {"students": 2, "total": 2 * len(labels), "done": 2})

    async def test_image_served_and_validation(self):
        attempt = await self.scan(self.students[0], "u1")
        image = await (await self.client.get(f"/api/teacher/attempt/{attempt}/written-image")).json()
        self.assertEqual(image["image"], "data:image/jpeg;base64," + base64.b64encode(JPEG).decode())
        bad = await self.client.post(f"/api/teacher/attempt/{attempt}/mark", json={"label": "5", "ok": True})
        self.assertEqual(bad.status, 400)  # test savoli — yozma emas

    async def test_mini_app_save_keeps_written_image(self):
        image = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
        r = await self.client.post(f"/api/teacher/test/{self.test['id']}/scan/save",
                                   json={"student_id": self.students[0]["id"], "variant": "A", "answers": self.answers,
                                         "written_image": image})
        self.assertEqual(r.status, 200, await r.text())
        job = db.get_written_job((await r.json())["attempt_id"])
        self.assertEqual((job["status"], job["mime"]), ("manual", "image/jpeg"))
        broken = await self.client.post(f"/api/teacher/test/{self.test['id']}/scan/save",
                                        json={"student_id": self.students[1]["id"], "variant": "A",
                                              "answers": self.answers, "written_image": "data:image/jpeg;base64,!!!"})
        self.assertEqual(broken.status, 200)  # buzilgan rasm natijani to'xtatmaydi — shunchaki saqlanmaydi
        self.assertIsNone(db.get_written_job((await broken.json())["attempt_id"]))

    async def test_no_images_yet(self):
        d = await self.review()
        self.assertEqual(d["students"], [])
        page = await (await self.client.get(f"/api/teacher/test/{self.test['id']}")).json()
        self.assertIsNone(page["written_review"])


class EssayScale(unittest.TestCase):
    """DTM «Baholash mezonlari»: esse 24 ballik mezon → 75 ballik shkala; umumiy = (1-bo'lim + 2-bo'lim) / 2."""

    def test_official_table(self):
        table = {24: 75, 23.5: 74, 22: 71, 20: 67, 18.5: 64, 16: 59, 14.5: 56, 12.5: 52, 12: 51,
                 11.5: 50, 9.5: 46, 7: 41, 4.5: 36, 2: 31, 1: 29, 0.5: 28, 0: 0}
        for score, expected in table.items():
            self.assertEqual(structure.essay_to_75(score), expected, score)
        self.assertIsNone(structure.essay_to_75(None))
        self.assertEqual(structure.essay_to_75(17.3), 62)  # 17,5 ga yaxlitlanadi → jadvalda 62

    def test_total_is_mean_of_sections(self):
        items = structure.build_items("milliy")
        test = {"fmt": "milliy", "items": items}
        labels = [i["label"] for i in items]
        attempts = [{"id": n, "wrong": labels[:n * 8], "essay": essay}
                    for n, essay in enumerate([24, 12, None, 0])]
        parts = scoring.rasch_breakdown(test, attempts)
        for a in attempts:
            p = parts[a["id"]]
            if a["essay"] is None:
                self.assertEqual((p["essay"], p["score"]), (None, p["test"]))  # esse hali baholanmagan
            else:
                self.assertEqual(p["essay"], structure.essay_to_75(a["essay"]))
                self.assertEqual(p["score"], round((p["test"] + p["essay"]) / 2, 1))
        self.assertEqual(scoring.rasch_results(test, attempts)[0], (parts[0]["score"], parts[0]["grade"]))


if __name__ == "__main__":
    unittest.main()
