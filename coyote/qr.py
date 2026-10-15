"""把 DG-LAB 的配對連結畫成 QR code。

- 終端機：用半格字元（``▀``/``▄``/``█``）畫出可以直接用手機掃的 QR code。
- 檔案：同時存一張 PNG，方便用圖片檢視器打開、或貼給手機看。

``qrcode`` 與 ``Pillow`` 都是選用相依：沒安裝時只會退回「把連結印出來」，
程式其他部分完全不受影響。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

#: 深色終端機用的預設值：亮色模組用方塊畫，暗色模組留空（背景是黑的）
DEFAULT_INVERT = True


def qr_available() -> bool:
    """是否安裝了 ``qrcode``。"""
    try:
        import qrcode  # noqa: F401
    except ImportError:
        return False
    return True


def build_matrix(data: str, *, border: int = 4) -> list[list[bool]] | None:
    """產生 QR code 的模組矩陣（``True`` = 深色模組）。

    Returns:
        矩陣；沒有安裝 ``qrcode`` 時回傳 ``None``。
    """
    try:
        import qrcode
    except ImportError:
        logger.debug("沒有安裝 qrcode，無法產生 QR code 圖形")
        return None

    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=border,
    )
    code.add_data(data)
    code.make(fit=True)
    return [[bool(module) for module in row] for row in code.get_matrix()]


def render_ascii(matrix: Sequence[Sequence[bool]], *, invert: bool = DEFAULT_INVERT) -> str:
    """把模組矩陣畫成終端機文字。

    每個字元代表上下兩個模組，所以輸出的高度是矩陣的一半。

    Args:
        matrix: :func:`build_matrix` 的結果。
        invert: ``True`` 時「亮色模組」才會畫成方塊，適合深色背景的終端機；
            ``False`` 時「深色模組」畫成方塊，適合白底終端機。
    """
    if not matrix:
        return ""

    def lit(module: bool) -> bool:
        # module 為 True 代表深色模組
        return (not module) if invert else module

    lines: list[str] = []
    for top_index in range(0, len(matrix), 2):
        top_row = matrix[top_index]
        bottom_row = matrix[top_index + 1] if top_index + 1 < len(matrix) else None
        chars: list[str] = []
        for column in range(len(top_row)):
            top = lit(top_row[column])
            bottom = lit(bottom_row[column]) if bottom_row is not None else False
            if top and bottom:
                chars.append("\u2588")  # █
            elif top:
                chars.append("\u2580")  # ▀
            elif bottom:
                chars.append("\u2584")  # ▄
            else:
                chars.append(" ")
        lines.append("".join(chars))
    return "\n".join(lines)


def render_text(data: str, *, invert: bool = DEFAULT_INVERT) -> str | None:
    """直接由連結產生終端機 QR code 文字。"""
    matrix = build_matrix(data)
    if matrix is None:
        return None
    return render_ascii(matrix, invert=invert)


def save_png(data: str, path: str | Path, *, scale: int = 8) -> bool:
    """把 QR code 存成 PNG。

    Returns:
        成功回傳 ``True``；缺少 ``qrcode`` 或 ``Pillow`` 時回傳 ``False``。
    """
    matrix = build_matrix(data)
    if matrix is None:
        return False
    try:
        from PIL import Image
    except ImportError:
        logger.debug("沒有安裝 Pillow，無法輸出 QR code 圖片")
        return False

    size = len(matrix)
    try:
        image = Image.new("1", (size, size), 1)
        pixels = image.load()
        for y, row in enumerate(matrix):
            for x, module in enumerate(row):
                if module:
                    pixels[x, y] = 0
        image = image.resize((size * scale, size * scale), Image.NEAREST)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target)
    except Exception:
        logger.warning("輸出 QR code 圖片失敗", exc_info=True)
        return False
    logger.info("QR code 圖片已存到 %s", target)
    return True


def open_image(path: str | Path) -> bool:
    """用系統預設的圖片檢視器打開檔案。"""
    import os
    import subprocess
    import sys

    target = Path(path)
    if not target.exists():
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception:
        logger.debug("無法自動開啟圖片", exc_info=True)
        return False
    return True


def present(
    data: str,
    *,
    invert: bool = DEFAULT_INVERT,
    png_path: str | Path | None = None,
    open_png: bool = False,
) -> str:
    """產生要顯示給使用者的文字區塊。

    內含 QR code（若可用）、PNG 路徑與原始連結。呼叫端負責把字串印出來。
    """
    lines = [
        "",
        "=" * 62,
        "  請用 DG-LAB App 掃描下面的 QR code 完成配對",
        "  （App 首頁右上角 → 掃碼連接）",
        "=" * 62,
        "",
    ]

    drawn = render_text(data, invert=invert)
    if drawn:
        lines.append(drawn)
    else:
        lines.append("  （未安裝 qrcode 套件，無法顯示圖形；請直接使用下面的連結）")
    lines.append("")

    if png_path is not None and save_png(data, png_path):
        lines.append(f"  QR code 圖片：{Path(png_path).resolve()}")
        if open_png:
            open_image(png_path)

    lines.append(f"  連結：{data}")
    lines.append("")
    lines.append("=" * 62)
    lines.append("")
    return "\n".join(lines)
