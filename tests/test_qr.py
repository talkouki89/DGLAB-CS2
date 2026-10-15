"""QR code 顯示的測試。

``qrcode`` / ``Pillow`` 是選用相依，所以這裡只測試不依賴它們的部分，
以及「沒安裝時要優雅退化」的行為。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from coyote import qr


class RenderAsciiTests(unittest.TestCase):
    def test_all_dark(self):
        matrix = [[True, True], [True, True]]
        self.assertEqual(qr.render_ascii(matrix, invert=False), "\u2588\u2588")

    def test_all_light(self):
        matrix = [[False, False], [False, False]]
        self.assertEqual(qr.render_ascii(matrix, invert=False), "  ")

    def test_half_blocks(self):
        matrix = [
            [True, False, True, False],
            [False, True, False, True],
        ]
        self.assertEqual(qr.render_ascii(matrix, invert=False), "\u2580\u2584\u2580\u2584")

    def test_invert_swaps_polarity(self):
        matrix = [
            [True, False, True, False],
            [False, True, False, True],
        ]
        self.assertEqual(qr.render_ascii(matrix, invert=True), "\u2584\u2580\u2584\u2580")

    def test_odd_number_of_rows(self):
        # 多出來的一列用上半格字元表示
        matrix = [[True, True], [True, True], [True, True]]
        lines = qr.render_ascii(matrix, invert=False).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "\u2588\u2588")
        self.assertEqual(lines[1], "\u2580\u2580")

    def test_empty_matrix(self):
        self.assertEqual(qr.render_ascii([]), "")


class AvailabilityTests(unittest.TestCase):
    def test_qr_available_returns_bool(self):
        self.assertIsInstance(qr.qr_available(), bool)

    def test_build_matrix_when_available(self):
        if not qr.qr_available():
            self.skipTest("沒有安裝 qrcode")
        matrix = qr.build_matrix("https://example.com")
        self.assertIsNotNone(matrix)
        self.assertEqual(len(matrix), len(matrix[0]))
        self.assertGreaterEqual(len(matrix), 21)

    def test_build_matrix_without_qrcode(self):
        if qr.qr_available():
            self.skipTest("這個環境有安裝 qrcode")
        self.assertIsNone(qr.build_matrix("https://example.com"))
        self.assertIsNone(qr.render_text("https://example.com"))


class PresentTests(unittest.TestCase):
    URL = (
        "https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#"
        "wss://ws.dungeon-lab.cn/abcd-1234"
    )

    def test_contains_url_and_header(self):
        text = qr.present(self.URL, png_path=None)
        self.assertIn("DG-LAB App", text)
        self.assertIn(self.URL, text)

    def test_falls_back_when_qrcode_missing(self):
        if qr.qr_available():
            self.skipTest("這個環境有安裝 qrcode")
        text = qr.present(self.URL, png_path=None)
        self.assertIn("未安裝 qrcode", text)
        self.assertIn(self.URL, text)

    def test_png_path_is_reported_when_written(self):
        target = Path(__file__).parent / "_scratch_qr.png"
        self.addCleanup(target.unlink, missing_ok=True)
        text = qr.present(self.URL, png_path=target, open_png=False)
        if target.exists():
            self.assertIn("QR code 圖片", text)
        else:
            # 沒有 qrcode / Pillow 時不應該留下痕跡
            self.assertFalse(target.exists())

    def test_save_png_returns_bool(self):
        target = Path(__file__).parent / "_scratch_qr2.png"
        self.addCleanup(target.unlink, missing_ok=True)
        result = qr.save_png(self.URL, target)
        self.assertIsInstance(result, bool)

    def test_open_missing_file_returns_false(self):
        self.assertFalse(qr.open_image(Path(__file__).parent / "_nope_.png"))


if __name__ == "__main__":
    unittest.main()
