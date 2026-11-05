"""Titul: yozma javob harf kataklari varaqqa sig'adi, PDF chiziladi, skaner uchun joylar to'g'ri."""
import unittest

import _env  # noqa: F401

import sheet
import structure


def milliy_test() -> dict:
    return {"id": 1, "code": "12345", "title": "Sinov", "fmt": "milliy", "count": 45,
            "items": structure.build_items("milliy"), "essay_topic": "Mavzu"}


class Layout(unittest.TestCase):
    def test_written_rows_have_letter_cells(self):
        spec = sheet.layout(milliy_test(), ["A", "B"])
        labels = [box["label"] for box in spec["written"]]
        self.assertEqual(labels, [i["label"] for i in structure.text_items(milliy_test()["items"])])
        for box in spec["written"]:
            self.assertGreaterEqual(box["cells"], 12)
            right = box["x"] + sheet.WRITTEN_LABEL_W + box["cells"] * sheet.WRITTEN_CELL_W
            self.assertLessEqual(right, sheet.RIGHT + 1, box["label"])

    def test_everything_fits_above_bottom_code(self):
        spec = sheet.layout(milliy_test(), ["A"])
        self.assertLess(spec["bottom"] + 30, sheet.H - sheet.CORNER - 20)
        x1, y1, x2, y2 = spec["written_area"]
        self.assertLess(y1, y2)
        self.assertLessEqual(x2, sheet.W)

    def test_pdf_builds_for_students_and_blanks(self):
        pdf = sheet.sheets_pdf(milliy_test(), ["A", "B"], [{"number": 7, "name": "Ali Valiyev", "variant": "B"}], blanks=2)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 5000)


if __name__ == "__main__":
    unittest.main()
