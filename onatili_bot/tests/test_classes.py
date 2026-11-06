"""Sinflar: doimiy ID, Excel'dan yuklash, ID ro'yxati chatga, sinfni titulli testga qo'shish."""
import unittest

import _env

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import db
import sheet
import structure
import webapp

OWNER, OTHER = 111, 222


class Classes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        self.user = {"id": OWNER}
        self.sent = []

        async def fake_auth(_request):
            return {"id": self.user["id"], "name": "Ustoz"}

        async def send_text(chat_id, text):
            self.sent.append((chat_id, text))

        self._auth = webapp.auth
        webapp.auth = fake_auth
        webapp.HOOKS["send_text"] = send_text
        self.client = TestClient(TestServer(webapp.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        webapp.auth = self._auth
        await self.client.close()

    async def post(self, path, payload=None):
        response = await self.client.post(path, json=payload or {})
        return response.status, await response.json()

    async def make_class(self, name, names):
        _, r = await self.post("/api/teacher/classes", {"name": name})
        cid = r["class"]["id"]
        _, r = await self.post(f"/api/teacher/class/{cid}/students", {"names": names})
        return cid, r["class"]["list"]

    async def test_ids_are_unique_across_classes(self):
        _, a = await self.make_class("11-A", ["Ali Valiyev", "Madina Yusupova"])
        _, b = await self.make_class("11-B", ["Bobur Karimov"])
        self.assertEqual(sorted(s["number"] for s in a), [1, 2])
        self.assertEqual([s["number"] for s in b], [3])
        listing = await (await self.client.get("/api/teacher/classes")).json()
        self.assertEqual([(c["name"], c["students"]) for c in listing["classes"]], [("11-A", 2), ("11-B", 1)])

    async def test_deleted_id_is_not_reused(self):
        cid, pupils = await self.make_class("10-A", ["Ali Valiyev", "Vali Aliyev"])
        newest = max(pupils, key=lambda p: p["number"])  # eng oxirgi berilgan ID o'chiriladi
        await self.post(f"/api/teacher/class/{cid}/students/{newest['id']}", {"delete": True})
        _, r = await self.post(f"/api/teacher/class/{cid}/students", {"names": ["Yangi O'quvchi"]})
        self.assertEqual(max(s["number"] for s in r["class"]["list"]), 3)

    async def test_excel_import_and_send_ids(self):
        _, r = await self.post("/api/teacher/classes", {"name": "9-A"})
        cid = r["class"]["id"]
        form = FormData()
        form.add_field("file", "F.I.Sh.\nAli Valiyev\nMadina Yusupova\n".encode(), filename="royxat.csv")
        r = await (await self.client.post(f"/api/teacher/class/{cid}/students/import", data=form)).json()
        self.assertEqual(r["added"], 2)
        status, _ = await self.post(f"/api/teacher/class/{cid}/send")
        self.assertEqual(status, 200)
        text = self.sent[0][1]
        self.assertIn("<code>0001</code> — Ali Valiyev", text)
        self.assertIn("9-A", text)

    async def test_class_into_titul_test_keeps_ids(self):
        cid, pupils = await self.make_class("11-A", ["Ali Valiyev", "Madina Yusupova"])
        items = structure.build_items("milliy")
        test = db.create_test(OWNER, "Mock", "milliy", 45, items, {"A": {}, "B": {}})
        db.set_mode(test["id"], "offline")
        status, r = await self.post(f"/api/teacher/test/{test['id']}/students/from-class", {"class_id": cid, "mode": "cycle"})
        self.assertEqual((status, r["added"]), (200, 2))
        roster = {s["name"]: (s["number"], s["variant"]) for s in db.list_students(test["id"])}
        by_name = {p["name"]: p["number"] for p in pupils}
        self.assertEqual({n: v[0] for n, v in roster.items()}, by_name)
        self.assertEqual(sorted(v[1] for v in roster.values()), ["A", "B"])
        status, r = await self.post(f"/api/teacher/test/{test['id']}/students/from-class", {"class_id": cid})
        self.assertEqual(status, 400)  # qayta qo'shilmaydi

    async def test_other_teacher_cannot_touch_class(self):
        cid, _ = await self.make_class("11-A", ["Ali Valiyev"])
        self.user["id"] = OTHER
        self.assertEqual((await self.client.get(f"/api/teacher/class/{cid}")).status, 404)
        self.assertEqual((await self.post(f"/api/teacher/class/{cid}", {"delete": True}))[0], 404)
        self.user["id"] = OWNER
        self.assertIsNotNone(db.get_class(cid))

    async def test_rename_and_delete(self):
        cid, _ = await self.make_class("11-A", ["Ali Valiyev"])
        _, r = await self.post(f"/api/teacher/class/{cid}", {"name": "11-B"})
        self.assertEqual(r["class"]["name"], "11-B")
        await self.post(f"/api/teacher/class/{cid}", {"delete": True})
        self.assertIsNone(db.get_class(cid))
        self.assertEqual(db.list_class_students(cid), [])


    async def test_backup_restores_classes_and_counter(self):
        import os
        import ops
        cid, pupils = await self.make_class("11-A", ["Ali Valiyev", "Madina Yusupova"])
        await self.post(f"/api/teacher/class/{cid}/students/{pupils[0]['id']}", {"delete": True})
        payload = __import__("json").loads(__import__("gzip").decompress(ops.make_backup()[0]))
        original = db.DB_PATH
        db.DB_PATH = os.path.join(_env.TMP, "restored_classes.db")
        try:
            if os.path.exists(db.DB_PATH):
                os.remove(db.DB_PATH)
            db.init()
            db.import_tables(payload["tables"])
            self.assertEqual([c["name"] for c in db.list_classes(OWNER)], ["11-A"])
            added = db.add_class_students(cid, OWNER, ["Yangi O'quvchi"])
            self.assertEqual(max(s["number"] for s in added), 3)  # hisoblagich ham tiklandi — ID qaytmaydi
        finally:
            db.DB_PATH = original


class BigIdOnSheet(unittest.TestCase):
    def test_id_above_code_range_is_not_encoded(self):
        """2047 dan katta ID titul pastidagi kodga yozilmaydi (boshqa o'quvchiga tushmasin) — skaner doirachalardan o'qiydi."""
        captured = []
        original = sheet.encode_number
        sheet.encode_number = lambda n: captured.append(n) or original(n)
        try:
            test = {"id": 1, "code": "12345", "title": "t", "fmt": "milliy", "count": 45, "items": structure.build_items("milliy")}
            sheet.sheets_pdf(test, ["A"], [{"number": 3000, "name": "Katta ID", "variant": "A"},
                                           {"number": 42, "name": "Kichik ID", "variant": "A"}])
        finally:
            sheet.encode_number = original
        self.assertEqual(captured, [42])


if __name__ == "__main__":
    unittest.main()
