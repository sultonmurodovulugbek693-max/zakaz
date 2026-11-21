"""Botdagi esse tekshiruvi: ustoz sozlamalari, ommaviy havola (2 ta bepul → to'lov → ustoz tasdiqlaydi), so'z sanash."""

import asyncio
import types
import unittest

import _env

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot
import db
import essay_ai
import plans
import webapp

TEACHER, STRANGER, PUPIL = 5001, 5002, 5003


class FakeBot:
    def __init__(self):
        self.sent = []  # (chat_id, text, markup)

    async def get_me(self):
        return types.SimpleNamespace(username="testbot")

    async def send_message(self, chat_id, text, reply_markup=None, **_):
        self.sent.append((chat_id, text, reply_markup))

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **_):
        self.sent.append((chat_id, caption, reply_markup))

    async def send_document(self, chat_id, document, caption=None, reply_markup=None, **_):
        self.sent.append((chat_id, caption, reply_markup))

    async def download(self, file_id):
        import io
        return io.BytesIO(f"rasm-{file_id}".encode())


def buttons(markup):
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


class EssayBot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
        db.set_setting("platform_share", "0")  # eski tartib: begonalar puli ustoz kartasiga (yangisi — test_fixes.RevenueShare)
        self.bot = FakeBot()
        self.storage = MemoryStorage()
        self.replies = []
        self._enabled = essay_ai.essay_enabled
        essay_ai.essay_enabled = lambda: True
        plans.grant(TEACHER, "month")
        db.add_credits(TEACHER, 10)
        db.remember_user(TEACHER, "Ustoz Aliyev", None)

    async def asyncTearDown(self):
        essay_ai.essay_enabled = self._enabled

    def state(self, user_id):
        return FSMContext(self.storage, StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))

    def message(self, user_id, text=None, photo=None, document=None):
        async def answer(value, reply_markup=None, **_):
            self.replies.append((user_id, value, reply_markup))

            async def delete():
                return True
            return types.SimpleNamespace(delete=delete)  # Telegram yuborilgan xabarni qaytaradi

        user = types.SimpleNamespace(id=user_id, full_name=f"User {user_id}", username=None)
        return types.SimpleNamespace(text=text, from_user=user, answer=answer, bot=self.bot,
                                     photo=[types.SimpleNamespace(file_id=photo)] if photo else None, document=document,
                                     html_text=text or "")

    def callback(self, user_id, data):
        async def answer(value=None, show_alert=False, **_):
            self.replies.append((user_id, value, None))

        async def edit_caption(**_):
            return None

        msg = self.message(user_id)
        msg.edit_caption = edit_caption
        user = types.SimpleNamespace(id=user_id, full_name=f"User {user_id}", username=None)
        return types.SimpleNamespace(data=data, from_user=user, answer=answer, message=msg, bot=self.bot)

    async def write_essay(self, user_id, words=80):
        state = self.state(user_id)
        await bot.essay_flow_button(self.callback(user_id, f"ecw:new:{TEACHER}"), state)
        await bot.essay_topic(self.message(user_id, "Kitob o'qish foydalimi?"), state)
        await bot.essay_part(self.message(user_id, " ".join(["so'z"] * (words // 2))), state)
        await bot.essay_part(self.message(user_id, " ".join(["davomi"] * (words - words // 2))), state)
        await bot.essay_flow_button(self.callback(user_id, "ecw:send"), state)
        return state

    def test_word_count(self):
        self.assertEqual(bot.word_count("  Kitob — bilim manbai, 2024-yil.  "), 4)
        self.assertEqual(bot.word_count(""), 0)

    async def test_public_link_two_free_then_payment(self):
        # Ustoz: narx va karta, keyin ommaviy
        state = self.state(TEACHER)
        await bot.essay_setup_button(self.callback(TEACHER, "ecs:pub"), state)
        self.assertTrue(any("Avval esse narxi (yoki paket) va kartani kiriting" in str(r[1]) for r in self.replies))
        await bot.essay_setup_button(self.callback(TEACHER, "ecs:price"), state)
        await bot.essay_setup_price(self.message(TEACHER, "15 000"), state)
        await bot.essay_setup_button(self.callback(TEACHER, "ecs:card"), state)
        await bot.essay_setup_card(self.message(TEACHER, "8600 1234 5678 9012 Aliyev Vali"), state)
        await bot.essay_setup_button(self.callback(TEACHER, "ecs:pub"), state)
        cfg = bot.essay_setup(TEACHER)
        self.assertEqual((cfg["public"], cfg["price"], cfg["card"], cfg["holder"]),
                         (True, 15000, "8600 1234 5678 9012", "Aliyev Vali"))

        code = webapp.public_code(TEACHER)
        self.assertEqual(webapp.teacher_from_public(code), TEACHER)
        await bot.open_public(self.message(STRANGER, f"/start {code}"), code)
        self.assertTrue(any("Bepul: <b>2 / 2</b>" in str(r[1]) for r in self.replies))

        # So'zlar soni ko'rsatib turiladi
        await self.write_essay(STRANGER)
        counts = [r[1] for r in self.replies if r[0] == STRANGER and "Hozircha" in str(r[1])]
        self.assertIn("40 so'z", counts[0])
        self.assertIn("80 so'z", counts[1])
        await self.write_essay(STRANGER)
        self.assertEqual(db.public_free_used(TEACHER, STRANGER), 2)
        self.assertEqual(db.get_credits(TEACHER) if not plans.SALES_ENABLED else 10, 8 if not plans.SALES_ENABLED else 10)

        # 3-esse — saqlanadi va kredit taklif qilinadi (har esse uchun alohida chek yo'q)
        await self.write_essay(STRANGER)
        offer = [r for r in self.replies if r[0] == STRANGER and "Essengiz saqlandi" in str(r[1])]
        self.assertEqual(buttons(offer[-1][2]), [f"wbp:{TEACHER}:1"])
        waiting = db.practice_awaiting_credit(TEACHER, STRANGER)
        self.assertEqual(len(waiting), 1)
        self.assertEqual(db.public_free_used(TEACHER, STRANGER), 2)  # kredit kutayotgani bepullarga kirmaydi

    async def setup_public(self, price="5000", packs="5 22 000\n10 - 40000"):
        state = self.state(TEACHER)
        await bot.essay_setup_button(self.callback(TEACHER, "ecs:price"), state)
        await bot.essay_setup_price(self.message(TEACHER, price), state)
        await bot.essay_setup_button(self.callback(TEACHER, "ecs:packs"), state)
        await bot.essay_setup_packs(self.message(TEACHER, packs), state)
        await bot.essay_setup_button(self.callback(TEACHER, "ecs:card"), state)
        await bot.essay_setup_card(self.message(TEACHER, "8600 1234 5678 9012 Aliyev Vali"), state)
        await bot.essay_setup_button(self.callback(TEACHER, "ecs:pub"), state)

    def hooks(self):
        """Bot ↔ webapp: matnlar FakeBot'ga, AI chekni «o'qiydi» (tarmoqsiz)."""
        saved = dict(webapp.HOOKS), essay_ai.enabled, essay_ai.read_receipt
        webapp.HOOKS.update(send_text=lambda chat_id, text: self.bot.send_message(chat_id, text),
                            wallet_receipt_notice=lambda *a: bot.wallet_receipt_notice(self.bot, *a),
                            wake_ai=lambda: None)
        essay_ai.enabled = lambda: True
        self.receipt = {}

        async def read(image, mime="image/jpeg"):
            return dict(self.receipt)
        essay_ai.read_receipt = read

        def restore():
            webapp.HOOKS.clear()
            webapp.HOOKS.update(saved[0])
            essay_ai.enabled, essay_ai.read_receipt = saved[1], saved[2]
        self.addCleanup(restore)

    def good_receipt(self, amount):
        self.receipt = {"is_receipt": True, "success": True, "amount": amount, "date": plans.now().strftime("%Y-%m-%d"),
                        "recipient_card": "8600 **** **** 9012", "recipient_name": "ALIYEV VALI", "transaction_id": f"TX{amount}",
                        "looks_edited": False}

    async def test_packs_parsed_with_spaces_and_discount(self):
        await self.setup_public()
        packs = webapp.essay_packs(TEACHER)
        self.assertEqual([(p["essays"], p["price"], p["discount"]) for p in packs],
                         [(1, 5000, 0), (5, 22000, 12), (10, 40000, 20)])
        state = self.state(TEACHER)
        await bot.essay_setup_button(self.callback(TEACHER, "ecs:packs"), state)
        await bot.essay_setup_packs(self.message(TEACHER, "1 3000"), state)  # 1 talik — paket emas
        self.assertTrue(any("2–100" in str(r[1]) for r in self.replies))
        await bot.essay_setup_packs(self.message(TEACHER, "0"), state)  # paketlarni o'chirish
        self.assertEqual([p["essays"] for p in webapp.essay_packs(TEACHER)], [1])

    async def test_credit_bought_once_then_essays_without_receipt(self):
        self.hooks()
        await self.setup_public()
        code = webapp.public_code(TEACHER)
        await bot.open_public(self.message(STRANGER), code)
        self.assertIn(f"wb:{TEACHER}", buttons(self.replies[-1][2]))
        await self.write_essay(STRANGER)
        await self.write_essay(STRANGER)
        await self.write_essay(STRANGER)  # bepullar tugagan — saqlanadi, kredit kutadi
        waiting = db.practice_awaiting_credit(TEACHER, STRANGER)[0]
        offer = [r for r in self.replies if r[0] == STRANGER and "Essengiz saqlandi" in str(r[1])][-1]
        self.assertEqual(buttons(offer[2]), [f"wbp:{TEACHER}:1", f"wbp:{TEACHER}:5", f"wbp:{TEACHER}:10"])

        await bot.wallet_pack_button(self.callback(STRANGER, f"wbp:{TEACHER}:5"))
        self.assertTrue(any(s[0] == STRANGER and "22 000 so'm" in s[1] and "8600 1234 5678 9012" in s[1] for s in self.bot.sent))
        pay = db.open_wallet_payment(STRANGER)
        self.assertEqual((pay["essays"], pay["amount"], pay["status"]), (5, 22000, "open"))

        self.good_receipt(22000)
        await bot.receipt_received(self.message(STRANGER, photo="chek-1"))
        self.assertEqual(db.get_wallet_payment(pay["id"])["status"], "paid")
        self.assertEqual(db.wallet_balance(TEACHER, STRANGER), 4)  # 5 − saqlangan esse
        released = db.get_practice(waiting["id"])
        self.assertEqual((released["status"], released["wallet"]), ("pending", 1))
        self.assertTrue(any(s[0] == STRANGER and "Kredit qo'shildi: +5" in s[1] for s in self.bot.sent))
        to_teacher = [s for s in self.bot.sent if s[0] == TEACHER and "Esse krediti" in str(s[1])]
        self.assertEqual(buttons(to_teacher[-1][2]), [f"wp:no:{pay['id']}"])  # AI tasdiqladi — faqat bekor qilish

        await self.write_essay(STRANGER)  # 4-esse — chek so'ralmaydi
        self.assertTrue(any("1 kredit yechildi, qoldi: <b>3 ta</b>" in str(r[1]) for r in self.replies if r[0] == STRANGER))
        self.assertEqual(db.public_free_used(TEACHER, STRANGER), 2)

        # Shu chek ikkinchi marta — AI tasdiqlamaydi (ustozga)
        await bot.wallet_pack_button(self.callback(STRANGER, f"wbp:{TEACHER}:1"))
        self.good_receipt(22000)
        await bot.receipt_received(self.message(STRANGER, photo="chek-1"))
        second = db.open_wallet_payment(STRANGER)
        self.assertEqual(second["status"], "review")
        self.assertIn("oldin ishlatilgan", db.get_wallet_payment(second["id"])["note"])
        await bot.decide_wallet_payment_button(self.callback(STRANGER, f"wp:ok:{second['id']}"))  # begona — yo'q
        self.assertEqual(db.get_wallet_payment(second["id"])["status"], "review")
        await bot.decide_wallet_payment_button(self.callback(TEACHER, f"wp:no:{second['id']}"))
        self.assertEqual(db.get_wallet_payment(second["id"])["status"], "rejected")
        self.assertEqual(db.wallet_balance(TEACHER, STRANGER), 3)

        # Ustoz AI tasdiqlagan birinchi to'lovni bekor qiladi — berilgan kredit qaytib olinadi (0 dan pastga emas)
        await bot.decide_wallet_payment_button(self.callback(TEACHER, f"wp:no:{pay['id']}"))
        self.assertEqual(db.wallet_balance(TEACHER, STRANGER), 0)

    async def test_failed_check_returns_credit(self):
        db.wallet_add(TEACHER, STRANGER, 1)
        pid = db.create_practice(TEACHER, STRANGER, None, "matn " * 80, "public", None, "awaiting_credit")
        self.assertEqual(await webapp.release_waiting(TEACHER, STRANGER), (1, False))
        self.assertEqual(db.wallet_balance(TEACHER, STRANGER), 0)
        grade, essay_ai.grade = essay_ai.grade, None

        async def broken(*_):
            raise essay_ai.AIError("o'qib bo'lmadi", retry=False)
        essay_ai.grade = broken
        try:
            await bot.grade_practice(self.bot, db.get_practice(pid))
        finally:
            essay_ai.grade = grade
        self.assertEqual(db.wallet_balance(TEACHER, STRANGER), 1)
        self.assertTrue(any("kreditingiz qaytarildi" in s[1] for s in self.bot.sent if s[0] == STRANGER))

    async def test_closed_public_and_linked_student(self):
        code = webapp.public_code(TEACHER)
        await bot.open_public(self.message(STRANGER), code)
        self.assertTrue(any("yopgan" in str(r[1]) for r in self.replies))
        db.link_student(TEACHER, PUPIL)  # ustozning o'quvchisi — har doim kreditdan, to'lovsiz
        await self.write_essay(PUPIL)
        await self.write_essay(PUPIL)
        await self.write_essay(PUPIL)
        rows = db.practice_of_student(PUPIL)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["status"] == "pending" and r["kind"] == "student" for r in rows))

    async def test_short_essay_rejected(self):
        db.link_student(TEACHER, PUPIL)
        await self.write_essay(PUPIL, words=20)
        self.assertTrue(any("juda qisqa" in str(r[1]) for r in self.replies))
        self.assertEqual(db.practice_of_student(PUPIL), [])

    async def test_samples_list_and_open(self):
        good = {"score": 22, "criteria": [], "comment": "", "transcription": "Rasmdan o'qilgan matn " * 30}
        for raw, text in ((22, "A'lo esse " * 140), (19.5, "Yaxshi esse " * 120), (15, "O'rtacha " * 100)):
            pid = db.create_practice(TEACHER, PUPIL, "Kitob va internet", text, "student")
            db.save_practice_result(pid, {**good, "score": raw})
        photo = db.create_practice(TEACHER, STRANGER, None, "", "public", [["f1", "image/jpeg"]])
        db.save_practice_result(photo, good)
        db.remember_user(PUPIL, "Lola Karimova", None)
        rows = bot.sample_rows()
        self.assertEqual([r["raw"] for r in rows], [22, 22, 19.5])  # 15 (57/75) kirmaydi
        await bot.send_samples(self.message(STRANGER))
        text, markup = self.replies[-1][1], self.replies[-1][2]
        self.assertIn("71</b> · 280 so'z", text)
        self.assertIn("66</b>", text)
        await bot.sample_open(self.callback(STRANGER, buttons(markup)[0]))
        opened = [r[1] for r in self.replies if r[0] == STRANGER and "Mavzu:" in str(r[1])][0]
        self.assertIn("71/75", opened)
        self.assertIn("Kitob va internet", opened)
        self.assertIn("Lola", opened)
        self.assertIn("A'lo esse", opened)

    def test_result_text(self):
        result = {"score": 16, "comment": "Yaxshi", "criteria": [{"key": k, "score": 2, "comment": "ok"} for k in essay_ai.RUBRIC_KEYS]}
        text = bot.practice_result_text(result, 245)
        self.assertIn("245 so'z", text)
        self.assertIn("/75", text)


if __name__ == "__main__":
    unittest.main()
