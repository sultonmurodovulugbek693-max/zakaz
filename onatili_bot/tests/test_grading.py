"""Javoblarni tekshirish: yozma javob solishtiruvi, ball, titul kodi."""
import unittest

import _env  # noqa: F401

import sheet
import structure
from structure import grade, normalize_text, text_is_correct


class WrittenAnswers(unittest.TestCase):
    def test_technical_differences_are_not_mistakes(self):
        for student in ["Go'zal", "GO'ZAL", "goʻzal", "go‘zal", "go`zal", "  go'zal. ", "гўзал"]:
            self.assertTrue(text_is_correct(student, "go'zal"), student)

    def test_spelling_mistake_is_wrong(self):
        self.assertFalse(text_is_correct("gozal", "go'zal"))
        self.assertFalse(text_is_correct("maktb", "maktab"))
        self.assertFalse(text_is_correct("", "maktab"))

    def test_synonyms(self):
        self.assertTrue(text_is_correct("ko'rkam", "chiroyli | go'zal | ko'rkam"))
        self.assertTrue(text_is_correct("chiroyli", "chiroyli / go'zal"))

    def test_punctuation_answer(self):
        self.assertTrue(text_is_correct(': " , " .', ': " , " .'))
        self.assertTrue(text_is_correct(":«,».", ': " , " .'))
        self.assertFalse(text_is_correct(': ", ", .', ': " , " .'))

    def test_normalize_hyphen_and_spaces(self):
        self.assertEqual(normalize_text("Ota - ona"), "ota-ona")
        self.assertEqual(normalize_text("  katta   uy "), "katta uy")


class Grade(unittest.TestCase):
    def test_milliy_full_score_is_75_without_essay(self):
        items = structure.build_items("milliy")
        key = {i["label"]: ("A" if i["kind"] == "choice" else "so'z") for i in items}
        result = grade(items, key, dict(key))
        self.assertEqual(result["wrong"], [])
        self.assertEqual(result["test_correct"] + result["written_correct"], len(items))
        self.assertAlmostEqual(result["points"] + structure.ESSAY_MAX, structure.max_points(items, "milliy"), places=1)

    def test_wrong_answers_listed(self):
        items = structure.build_items("milliy")
        key = {i["label"]: ("A" if i["kind"] == "choice" else "so'z") for i in items}
        answers = dict(key, **{"1": "B", "36": "soz"})
        result = grade(items, key, answers)
        self.assertEqual(sorted(result["wrong"]), ["1", "36"])


class SheetCode(unittest.TestCase):
    def test_student_number_roundtrip(self):
        for number in [1, 7, 42, 999, 2047]:
            self.assertEqual(sheet.decode_number(sheet.encode_number(number)), number)

    def test_corrupted_code_is_rejected(self):
        code = sheet.encode_number(42)
        self.assertIsNone(sheet.decode_number(code ^ 1))  # bitta bit xato o'qildi
        self.assertEqual(sheet.decode_number(0), 0)  # bo'sh titul


if __name__ == "__main__":
    unittest.main()
