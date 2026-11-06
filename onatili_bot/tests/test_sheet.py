"""Titul: yozma javob harf kataklari varaqqa sig'adi, PDF chiziladi, skaner uchun joylar to'g'ri."""
import unittest

import _env

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

    def test_pdf_has_one_page_per_sheet_without_essay(self):
        """Esse botda yoziladi — titul PDF'ida esse varaqasi yo'q: 2 o'quvchi + 1 bo'sh = 3 bet."""
        import re
        students = [{"number": 7, "name": "Ali", "variant": "A"}, {"number": 8, "name": "Vali", "variant": "B"}]
        pdf = sheet.sheets_pdf(milliy_test(), ["A", "B"], students, blanks=1)
        self.assertEqual(len(re.findall(rb"/Type /Page[^s]", pdf)), 3)

    def test_pdf_builds_for_students_and_blanks(self):
        pdf = sheet.sheets_pdf(milliy_test(), ["A", "B"], [{"number": 7, "name": "Ali Valiyev", "variant": "B"}], blanks=2)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 5000)


class Styles(unittest.TestCase):
    def oddiy(self, style: str) -> dict:
        return {"id": 2, "code": "54321", "title": "Oddiy", "fmt": "oddiy", "count": 30,
                "items": structure.build_items("oddiy", 30), "sheet_style": style}

    def test_every_style_draws_and_scans(self):
        import sheet_styles
        for style in sheet_styles.STYLES:
            test = self.oddiy(style)
            spec = sheet.layout(test, ["A", "B"])
            self.assertTrue(any(b["kind"] == "single" for b in spec["bubbles"]), style)
            self.assertTrue(sheet.sheets_pdf(test, ["A", "B"], [], blanks=1).startswith(b"%PDF"), style)

    def test_old_style_names_still_work(self):
        import sheet_styles
        self.assertEqual(sheet_styles.style_of({"sheet_style": "evalbee"}), "large")
        self.assertEqual(sheet_styles.style_of({"sheet_style": "dtm"}), "columns")
        self.assertEqual(sheet_styles.style_of({"sheet_style": "nomalum"}), "standard")

    def test_database_migrates_old_style_names(self):
        import db
        _env.fresh_db()
        test = db.create_test(1, "Eski", "oddiy", 30, structure.build_items("oddiy", 30), {"A": {}})
        db.set_sheet_style(test["id"], "dtm")
        db.init()  # bot qayta ishga tushdi
        self.assertEqual(db.get_test(test["id"])["sheet_style"], "columns")


if __name__ == "__main__":
    unittest.main()
