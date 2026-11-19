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


def buttons(markup):
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


class EssayBot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _env.fresh_db()
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
        self.assertTrue(any("Avval esse narxi va kartani kiriting" in str(r[1]) for r in self.replies))
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

        # 3-esse — to'lov so'raladi
        state = await self.write_essay(STRANGER)
        self.assertTrue(any("To'lov: 15 000 so'm" in str(r[1]) for r in self.replies))
        practice = db.practice_awaiting_pay(STRANGER)
        self.assertEqual((practice["price"], practice["status"]), (15000, "awaiting_pay"))
        await bot.essay_receipt(self.message(STRANGER, photo="chek-1"), state)
        to_teacher = [s for s in self.bot.sent if s[0] == TEACHER and "to'lovi" in str(s[1])]
        self.assertEqual(buttons(to_teacher[-1][2]), [f"ecp:ok:{practice['id']}", f"ecp:no:{practice['id']}"])
        self.assertEqual(db.get_practice(practice["id"])["status"], "awaiting_check")

        await bot.essay_payment_decision(self.callback(STRANGER, f"ecp:ok:{practice['id']}"))  # begona tasdiqlay olmaydi
        self.assertEqual(db.get_practice(practice["id"])["status"], "awaiting_check")
        await bot.essay_payment_decision(self.callback(TEACHER, f"ecp:ok:{practice['id']}"))
        self.assertEqual(db.get_practice(practice["id"])["status"], "pending")  # AI navbatida
        self.assertTrue(any(s[0] == STRANGER and "To'lov tasdiqlandi" in s[1] for s in self.bot.sent))

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

    def test_result_text(self):
        result = {"score": 16, "comment": "Yaxshi", "criteria": [{"key": k, "score": 2, "comment": "ok"} for k in essay_ai.RUBRIC_KEYS]}
        text = bot.practice_result_text(result, 245)
        self.assertIn("245 so'z", text)
        self.assertIn("/75", text)


if __name__ == "__main__":
    unittest.main()
