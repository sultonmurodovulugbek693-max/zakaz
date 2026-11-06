"""
Oflayn skaner yo'li (server): telefondan titul yuborish → AI navbati → natija qayta hisoblanadi → tuzatish,
hamda zaxira nusxa. AI va Telegram almashtirilgan — internet kerak emas.
"""
import asyncio
import gzip
import json
import os
import unittest

import _env

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import bot
import db
import essay_ai
import ops
import plans
import structure
import webapp

OWNER, OTHER = 111, 222


class FakeBot:
    def __init__(self):
        self.messages, self.documents = [], []

    async def send_message(self, chat_id, text, **_):
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, **_):
        self.documents.append((chat_id, document))


class ScanFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        webapp._UPLOADED.clear()
        webapp._STUDENT_OF.clear()
        self.items = structure.build_items("milliy")
        key = {i["label"]: "A" for i in structure.choice_items(self.items)}
        key.update({t["label"]: "kitob" for t in structure.text_items(self.items)})
        self.test = db.create_test(OWNER, "Sinov", "milliy", 45, self.items, {"A": key, "B": {**key, "1": "B"}})
        self.student = db.add_students(self.test["id"], ["Ali Valiyev"], ["A"])[-1]
        plans.grant(OWNER, "markaz", 1)  # AI limiti yetarli tarif
        self.user = {"id": OWNER}
        self.bot = FakeBot()

        async def fake_auth(_request):
            return {"id": self.user["id"], "name": "Ustoz"}

        self._auth = webapp.auth
        webapp.auth = fake_auth
        webapp.HOOKS.update(
            record_attempt=lambda *a, **k: bot.record_attempt(self.bot, *a, **k),
            store_photo=lambda chat_id, data, caption: asyncio.sleep(0, result=f"file-{len(data)}"),
            scan_batch_notice=lambda chat_id, *args: bot.scan_batch_notice(self.bot, chat_id, *args),
        )
        self.client = TestClient(TestServer(webapp.create_app()))
        await self.client.start_server()
        self.answers = {i["label"]: "A" for i in structure.choice_items(self.items)}

    async def asyncTearDown(self):
        webapp.auth = self._auth
        await self.client.close()

    async def upload(self, payload, written=b"\xff\xd8 yozma", essays=()):
        form = FormData()
        form.add_field("data", json.dumps(payload))
        if written:
            form.add_field("written", written, filename="y.jpg", content_type="image/jpeg")
        for data in essays:
            form.add_field("essay", data, filename="e.jpg", content_type="image/jpeg")
        response = await self.client.post(f"/api/teacher/test/{self.test['id']}/scan/upload", data=form)
        return response.status, await response.json()

    async def test_upload_is_idempotent_and_queues_ai(self):
        payload = {"uid": "u1", "student_id": self.student["id"], "variant": "A", "answers": self.answers}
        status, first = await self.upload(payload, essays=[b"\xff\xd8 bet1", b"\xff\xd8 bet2"])
        self.assertEqual(status, 200)
        self.assertTrue(first["written_queued"])
        self.assertEqual(first["essay_status"], "pending")
        _, again = await self.upload(payload)
        self.assertEqual(again, first)  # tarmoq qayta yuborsa — ikkinchi natija yo'q
        self.assertEqual(len(db.list_attempts(self.test["id"])), 1)
        self.assertEqual(len(db.get_essay(first["attempt_id"])["photos"]), 2)

    async def test_new_student_by_name_and_validation(self):
        status, r = await self.upload({"uid": "u2", "name": "Yangi O'quvchi", "variant": "A", "answers": self.answers})
        self.assertEqual((status, r["name"]), (200, "Yangi O'quvchi"))
        status, r = await self.upload({"uid": "u3", "variant": "Z", "answers": self.answers})
        self.assertEqual(status, 400)
        status, r = await self.upload({"uid": "u4", "name": "Al", "variant": "A", "answers": self.answers})
        self.assertEqual(status, 400)

    async def test_teacher_typed_written_answers_skip_ai(self):
        typed = dict(self.answers, **{t["label"]: "kitob" for t in structure.text_items(self.items)})
        _, r = await self.upload({"uid": "u5", "student_id": self.student["id"], "variant": "A", "answers": typed})
        self.assertFalse(r["written_queued"])

    async def test_ai_reads_written_and_regrades(self):
        _, r = await self.upload({"uid": "u6", "student_id": self.student["id"], "variant": "A",
                                  "answers": dict(self.answers, **{"37": "kitob"})})
        before = db.get_attempt(r["attempt_id"])

        async def fake_read(image, labels, mime="image/png"):
            self.assertEqual(mime, "image/jpeg")
            return {"36": "KITOB", "37": "noto'g'ri", "38": "kitop"}, 0.0

        original = essay_ai.read_written
        essay_ai.read_written = fake_read
        try:
            for job in db.pending_written(10):
                await bot.read_written_one(self.bot, job)
        finally:
            essay_ai.read_written = original
        after = db.get_attempt(r["attempt_id"])
        self.assertEqual(after["answers"]["36"], "KITOB")
        self.assertEqual(after["answers"]["37"], "kitob")  # o'qituvchi yozgani — AI'niki ustidan yozilmaydi
        self.assertEqual(after["written_correct"], before["written_correct"] + 1)
        self.assertIn("38", after["wrong"])
        self.assertEqual(db.written_status(self.test["id"]), {"pending": 0, "done": 1, "failed": 0})
        self.assertTrue(any("Yozma javoblar AI'da o'qildi" in text for _, text in self.bot.messages))

    async def test_batch_reads_many_sheets_in_one_request(self):
        students = db.add_students(self.test["id"], ["Bobur", "Dilnoza", "Ezoza"], ["A", "A", "A"])[-3:]
        ids = []
        for n, st in enumerate(students):
            _, r = await self.upload({"uid": f"b{n}", "student_id": st["id"], "variant": "A", "answers": self.answers},
                                     written=f"\xff\xd8 rasm {n}".encode())
            ids.append(r["attempt_id"])
        calls = {"batch": 0, "single": 0}

        async def fake_batch(images, labels):
            calls["batch"] += 1
            self.assertEqual(len(images), 3)
            return [{"36": "kitob"}, None, {"36": "xato"}]  # 2-titulni AI qaytarmadi

        async def fake_single(image, labels, mime="image/png"):
            calls["single"] += 1
            self.assertEqual(image, "\xff\xd8 rasm 1".encode())  # aynan qaytmagan titul alohida o'qiladi
            return {"36": "kitob"}, 0.0

        originals = essay_ai.read_written_batch, essay_ai.read_written
        essay_ai.read_written_batch, essay_ai.read_written = fake_batch, fake_single
        try:
            jobs = db.pending_written(40)
            await bot.read_written_group(self.bot, jobs)
        finally:
            essay_ai.read_written_batch, essay_ai.read_written = originals
        self.assertEqual(calls, {"batch": 1, "single": 1})
        got = [db.get_attempt(i)["answers"].get("36") for i in ids]
        self.assertEqual(got, ["kitob", "kitob", "xato"])
        self.assertEqual(db.written_status(self.test["id"])["done"], 3)
        notices = [text for _, text in self.bot.messages if "Yozma javoblar AI'da o'qildi" in text]
        self.assertEqual(len(notices), 1)  # 3 titul — bitta umumiy xabar

    # ---------- Titulli testning essesi — botda, ID bilan ----------

    ESSAY = " ".join(["Ijtimoiy tarmoqlar yoshlar hayotida muhim o'rin tutadi."] * 8)

    async def essay(self, number, text=None, user=None):
        if user:
            self.user["id"] = user
        response = await self.client.post(f"/api/test/{self.test['code']}/essay",
                                          json={"number": number, "name": "Ali", "essay": text or self.ESSAY})
        self.user["id"] = OWNER
        return response.status, await response.json()

    async def test_offline_essay_before_and_after_scan(self):
        db.set_mode(self.test["id"], "offline")
        info = await (await self.client.get(f"/api/test/{self.test['code']}")).json()
        self.assertEqual(info["mode"], "offline")
        number = self.student["number"]
        # 1) Avval esse, keyin titul
        status, r = await self.essay(f"{number:04d}", user=501)
        self.assertEqual((status, r["linked"]), (200, False))
        page = await (await self.client.get(f"/api/teacher/test/{self.test['id']}")).json()
        self.assertEqual(page["waiting_essays"], 1)
        _, up = await self.upload({"uid": "e1", "student_id": self.student["id"], "variant": "A", "answers": self.answers})
        essay = db.get_essay(up["attempt_id"])
        self.assertEqual((essay["text"], essay["status"]), (self.ESSAY, "pending"))
        page = await (await self.client.get(f"/api/teacher/test/{self.test['id']}")).json()
        self.assertEqual(page["waiting_essays"], 0)
        # 2) Titul allaqachon bor — esse darhol biriktiriladi; o'sha akkaunt tuzatib qayta yuborsa ham
        status, r = await self.essay(f"{number:04d}", text=self.ESSAY + " Xulosa.", user=501)
        self.assertEqual((status, r["linked"]), (200, True))
        self.assertTrue(db.get_essay(up["attempt_id"])["text"].endswith("Xulosa."))

    async def test_offline_essay_protection(self):
        db.set_mode(self.test["id"], "offline")
        self.assertEqual((await self.essay("0001", user=501))[0], 200)
        self.assertEqual((await self.essay("0001", user=502))[0], 409)  # boshqa akkaunt shu ID bilan
        self.assertEqual((await self.essay("0002", user=501))[0], 409)  # bitta akkaunt — bitta ID
        self.assertEqual((await self.essay("0003", text="Juda qisqa esse", user=503))[0], 400)
        self.assertEqual((await self.essay("12a", user=504))[0], 400)
        # Baholangan esse o'zgarmaydi
        _, up = await self.upload({"uid": "e2", "student_id": self.student["id"], "variant": "A", "answers": self.answers})
        db.set_essay(up["attempt_id"], 18)
        self.assertEqual((await self.essay("0001", user=501))[0], 409)

    async def test_blank_sheet_student_gets_bubbled_id(self):
        """Ro'yxatda yo'q o'quvchi: titulda bo'yagan ID'si bilan qo'shiladi va essesi topiladi."""
        db.set_mode(self.test["id"], "offline")
        await self.essay("0123", user=601)
        _, up = await self.upload({"uid": "e3", "name": "Yangi O'quvchi", "number": 123, "variant": "A", "answers": self.answers})
        student = db.get_student(self.test["id"], None, 123)
        self.assertEqual((student["name"], student["attempt_id"]), ("Yangi O'quvchi", up["attempt_id"]))
        self.assertEqual(db.get_essay(up["attempt_id"])["text"], self.ESSAY)
        # ID band bo'lsa — keyingi bo'sh raqam (ikkinchi nusxa ochilmaydi)
        _, up2 = await self.upload({"uid": "e4", "name": "Boshqa O'quvchi", "number": 123, "variant": "A", "answers": self.answers})
        self.assertNotEqual(db.get_attempt(up2["attempt_id"])["student_name"], "Yangi O'quvchi")

    async def test_ai_failure_gives_up_after_retries(self):
        _, r = await self.upload({"uid": "u7", "student_id": self.student["id"], "variant": "A", "answers": self.answers})

        async def broken(*_a, **_k):
            raise essay_ai.AIError("limit", retry=True)

        original = essay_ai.read_written
        essay_ai.read_written = broken
        try:
            for _ in range(bot.WRITTEN_MAX_TRIES):
                for job in db.pending_written(10):
                    try:
                        await bot.read_written_one(self.bot, job)
                    except essay_ai.AIError:
                        pass
        finally:
            essay_ai.read_written = original
        self.assertEqual(db.get_written_job(r["attempt_id"])["status"], "failed")
        self.assertTrue(any("AI o'qiy olmadi" in text for _, text in self.bot.messages))

    async def test_editor_regrades_and_protects_other_teachers(self):
        _, r = await self.upload({"uid": "u8", "student_id": self.student["id"], "variant": "A", "answers": self.answers})
        aid = r["attempt_id"]
        url = f"/api/teacher/attempt/{aid}/answers"
        opened = await (await self.client.get(url)).json()
        self.assertTrue(opened["written"]["image"].startswith("data:image/jpeg;base64,"))
        fixed = dict(opened["answers"], **{"36": "kitob", "1": "B"})
        saved = await (await self.client.post(url, json={"answers": fixed})).json()
        self.assertIn("1", saved["wrong"])
        self.assertNotIn("36", saved["wrong"])
        saved = await (await self.client.post(url, json={"answers": fixed, "variant": "B"})).json()
        self.assertNotIn("1", saved["wrong"])
        self.assertEqual(db.get_attempt(aid)["variant"], "B")
        self.assertEqual((await self.client.post(url, json={"answers": {"1": "Z"}})).status, 400)
        self.user["id"] = OTHER
        self.assertEqual((await self.client.get(url)).status, 404)

    async def test_batch_notice_and_test_page_status(self):
        await self.upload({"uid": "u9", "student_id": self.student["id"], "variant": "A", "answers": self.answers})
        done = await (await self.client.post(f"/api/teacher/test/{self.test['id']}/scan/upload/done",
                                             json={"saved": 1, "written": 1, "essays": 0, "failed": 0})).json()
        self.assertEqual(done["written"]["pending"], 1)
        self.assertTrue(any("Qabul qilindi" in text for _, text in self.bot.messages))
        page = await (await self.client.get(f"/api/teacher/test/{self.test['id']}")).json()
        self.assertEqual(page["attempts"][0]["written_status"], "pending")

    async def test_layout_gives_crop_areas(self):
        layout = await (await self.client.get(f"/api/teacher/test/{self.test['id']}/scan/layout")).json()
        self.assertEqual(len(layout["written_labels"]), 14)
        self.assertEqual(len(layout["written_area"]), 4)
        self.assertTrue(layout["ai"])

    async def test_backup_roundtrip(self):
        _, r = await self.upload({"uid": "u10", "student_id": self.student["id"], "variant": "A", "answers": self.answers})
        content, counts = ops.make_backup()
        self.assertEqual(counts["attempts"], 1)
        payload = json.loads(gzip.decompress(content))
        self.assertNotIn("image", payload["tables"]["written_jobs"][0])  # rasmlar nusxaga kirmaydi
        with self.assertRaises(ValueError):  # bo'sh bo'lmagan bazaga — yo'q
            db.import_tables(payload["tables"])
        original = db.DB_PATH
        db.DB_PATH = os.path.join(_env.TMP, "restored.db")
        try:
            if os.path.exists(db.DB_PATH):
                os.remove(db.DB_PATH)
            db.init()
            db.import_tables(payload["tables"])
            restored = db.get_attempt(r["attempt_id"])
            self.assertEqual(restored["student_name"], "Ali Valiyev")
            self.assertEqual(db.list_students(self.test["id"])[0]["attempt_id"], r["attempt_id"])
            new = db.create_test(OWNER, "Keyingi", "milliy", 45, self.items, {"A": {}})
            self.assertGreater(new["id"], self.test["id"])  # raqamlar tiklangandan keyin davom etadi
        finally:
            db.DB_PATH = original

    async def test_send_backup_to_admin(self):
        sent = await ops.send_backup(self.bot, [OWNER])
        self.assertEqual(sent, 1)
        self.assertEqual(self.bot.documents[0][0], OWNER)

    async def test_purge_old_images(self):
        _, r = await self.upload({"uid": "u11", "student_id": self.student["id"], "variant": "A", "answers": self.answers})
        db.written_done(r["attempt_id"], {})
        with db.connect() as conn:
            conn.execute("UPDATE written_jobs SET created_at = '2020-01-01 00:00'")
        self.assertEqual(db.purge_written_images(), 1)
        self.assertIsNone(db.get_written_job(r["attempt_id"])["image"])


if __name__ == "__main__":
    unittest.main()
