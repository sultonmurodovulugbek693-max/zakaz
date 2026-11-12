"""
«📊 Natijalar» bo'limi: sinf natijalari doimiy ID orqali yig'iladi, Rasch butun test bo'yicha (sinf ichida
emas), mock sertifikat va sinf jadvali PDF'i bot chatiga yuboriladi.
"""
import re
import unittest

import _env

from aiohttp.test_utils import TestClient, TestServer

import db
import results
import scoring
import structure
import webapp

OWNER, OTHER = 901, 902


def pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf))


class Results(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        self.items = structure.build_items("milliy")
        labels = [i["label"] for i in structure.choice_items(self.items)]
        self.key = {label: "A" for label in labels}
        self.cls = db.create_class(OWNER, "11-A")
        self.pupils = db.add_class_students(self.cls["id"], OWNER, ["Ali Valiyev", "Madina Yusupova", "Bobur Karimov"])
        self.other_cls = db.create_class(OWNER, "10-B")
        self.outsider = db.add_class_students(self.other_cls["id"], OWNER, ["Jasur Aliyev"])[0]

        self.test = db.create_test(OWNER, "Mock №1", "milliy", 45, self.items, {"A": self.key})
        db.set_mode(self.test["id"], "offline")
        # 11-A: uch o'quvchi turli natija; 10-B dan bittasi; sinfsiz bo'sh titul (ID 777) va onlayn javob
        errors = {"Ali Valiyev": 0, "Madina Yusupova": 6, "Bobur Karimov": 12}
        for pupil in self.pupils:
            self.save(self.test, db.offline_tg_id(self.test["id"], pupil["number"]), pupil["name"],
                      labels[:errors[pupil["name"]]])
        self.save(self.test, db.offline_tg_id(self.test["id"], self.outsider["number"]), self.outsider["name"], labels[:3])
        self.save(self.test, db.offline_tg_id(self.test["id"], 777), "Begona", labels[:20])
        self.save(self.test, 555001, "Onlayn o'quvchi", labels[:9])
        self.empty = db.create_test(OWNER, "Hali hech kim yo'q", "milliy", 45, self.items, {"A": self.key})

        self.sent = []

        async def send_file(chat_id, content, name, caption):
            self.sent.append((chat_id, content, name, caption))

        async def fake_auth(_request):
            return {"id": OWNER, "name": "Ustoz Aliyeva", "username": None}

        self._auth = webapp.auth
        webapp.auth = fake_auth
        webapp.HOOKS["send_file"] = send_file
        self.client = TestClient(TestServer(webapp.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        webapp.auth = self._auth
        webapp.HOOKS.pop("send_file", None)
        await self.client.close()

    def save(self, test, tg_id, name, wrong_labels):
        answers = {label: ("B" if label in wrong_labels else "A") for label in self.key}
        return db.save_attempt(test["id"], tg_id, name, "A", answers, structure.grade(self.items, self.key, answers))

    def test_offline_number_roundtrip(self):
        self.assertEqual(results.offline_number(7, db.offline_tg_id(7, 42)), 42)
        self.assertIsNone(results.offline_number(7, 555001))  # onlayn — sinfga bog'lanmaydi
        self.assertIsNone(results.offline_number(7, None))

    async def test_overview(self):
        r = await (await self.client.get("/api/teacher/results")).json()
        self.assertEqual({c["name"]: c["tests"] for c in r["classes"]}, {"11-A": 1, "10-B": 1})
        self.assertEqual([(t["title"], t["n"]) for t in r["tests"]], [("Mock №1", 6)])  # bo'sh test ko'rinmaydi

    async def test_class_report_uses_whole_test_rasch(self):
        r = await (await self.client.get(f"/api/teacher/class/{self.cls['id']}/results")).json()
        self.assertEqual([t["n"] for t in r["tests"]], [3])
        full = scoring.rasch_breakdown(self.test, db.list_attempts(self.test["id"]))
        by_name = {row["name"]: row for row in r["students"]}
        for attempt in db.list_attempts(self.test["id"]):
            if attempt["student_name"] in by_name:
                cell = by_name[attempt["student_name"]]["results"][str(self.test["id"])]
                self.assertEqual((cell["score"], cell["grade"]), (full[attempt["id"]]["score"], full[attempt["id"]]["grade"]))
        self.assertEqual([row["name"] for row in r["students"]], ["Ali Valiyev", "Madina Yusupova", "Bobur Karimov"])
        self.assertEqual(r["summary"]["taken"], 3)

    async def test_test_results_filtered_by_class(self):
        r = await (await self.client.get(f"/api/teacher/test/{self.test['id']}/results")).json()
        self.assertEqual(r["n"], 6)
        names = {row["name"]: row for row in r["rows"]}
        self.assertEqual((names["Ali Valiyev"]["class_name"], names["Begona"]["class_name"]), ("11-A", None))
        self.assertEqual(names["Begona"]["number"], 777)
        self.assertIsNone(names["Onlayn o'quvchi"]["number"])
        mine = await (await self.client.get(f"/api/teacher/test/{self.test['id']}/results?class={self.cls['id']}")).json()
        self.assertEqual([row["name"] for row in mine["rows"]], ["Ali Valiyev", "Madina Yusupova", "Bobur Karimov"])
        self.assertEqual(mine["class"]["name"], "11-A")
        foreign = db.create_class(OTHER, "Begona sinf")
        bad = await self.client.get(f"/api/teacher/test/{self.test['id']}/results?class={foreign['id']}")
        self.assertEqual(bad.status, 404)

    async def test_certificates_pdf(self):
        r = await self.client.post(f"/api/teacher/test/{self.test['id']}/certificates", json={"class": self.cls["id"]})
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual((await r.json())["count"], 3)
        chat, content, name, caption = self.sent[-1]
        self.assertEqual(chat, OWNER)
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(pages(content), 3)  # har o'quvchiga bitta sahifa
        self.assertIn("11-A", caption)

        one = db.list_attempts(self.test["id"])[0]
        r = await (await self.client.post(f"/api/teacher/test/{self.test['id']}/certificates",
                                          json={"attempt_ids": [one["id"]]})).json()
        self.assertEqual(r["count"], 1)
        self.assertEqual(self.sent[-1][2], "sertifikat_Ali_Valiyev.pdf")

        simple = db.create_test(OWNER, "Oddiy", "oddiy", 5, structure.build_items("oddiy", 5), {"A": {str(n): "A" for n in range(1, 6)}})
        bad = await self.client.post(f"/api/teacher/test/{simple['id']}/certificates", json={})
        self.assertEqual(bad.status, 400)  # sertifikat — faqat Milliy format
        none = await self.client.post(f"/api/teacher/test/{self.empty['id']}/certificates", json={})
        self.assertEqual(none.status, 400)

    async def test_class_report_pdf(self):
        r = await self.client.post(f"/api/teacher/class/{self.cls['id']}/report", json={})
        self.assertEqual(r.status, 200, await r.text())
        _, content, name, caption = self.sent[-1]
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(name, "sinf_11-A.pdf")
        self.assertIn("3 ta o'quvchi", caption)
        lonely = db.create_class(OWNER, "Bo'sh sinf")
        empty = await self.client.post(f"/api/teacher/class/{lonely['id']}/report", json={})
        self.assertEqual(empty.status, 400)  # hali titulli testda qatnashmagan


if __name__ == "__main__":
    unittest.main()
