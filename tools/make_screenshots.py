"""產生 README 用的截圖。

**這支腳本刻意做了兩件保護隱私的事，改動它之前請先讀完：**

1. **截圖前一定先清空事件紀錄。**
   程式啟動時的紀錄會印出設定檔與記錄檔的完整路徑，也就是
   ``C:\\Users\\<你的名字>\\...`` —— 每一張程式截圖下方都有事件紀錄，
   不清掉的話等於把自己的 Windows 使用者名稱推到公開 repo。
   清掉之後只留遊戲事件（受擊／擊殺／連殺），畫面反而更乾淨。

2. **不要用「往下偏移 N px」來避開瀏覽器工具列。**
   每個人的瀏覽器工具列高度都不一樣（書籤列、擴充功能列…），
   猜偏移量會拍到書籤。改成掃描畫面：頁面底色很暗（``#0f0f17``），
   工具列不是，所以「第一列整列都是底色」就是頁面頂端。
   截完還會把視窗邊框內縮，避免圓角露出桌面。

執行方式（在工作目錄根目錄）：

    python tools/make_screenshots.py

需要 ``pillow`` 與 ``qrcode``；瀏覽器那張需要 Windows（會開啟預設瀏覽器）。
"""

from __future__ import annotations

import ctypes
import dataclasses
import json
import logging
import socket
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".builddeps"))
logging.basicConfig(level=logging.ERROR)

import tkinter as tk  # noqa: E402

from PIL import ImageGrab  # noqa: E402

import cs2cfg  # noqa: E402
import gui as gui_module  # noqa: E402
from config import GsiConfig, load_config  # noqa: E402
from engine import ControlEngine, EngineState  # noqa: E402
from gui import TAB_ORDER, DglabApp  # noqa: E402
from tests.test_gsi import payload_with  # noqa: E402

DOCS = ROOT / "docs"

#: 監看頁面的底色（要和 dashboard.py 的 --bg 一致）
PAGE_BG = (15, 15, 23)
BG_TOLERANCE = 12
#: 抓瀏覽器視窗時先內縮這麼多，切掉圓角與邊框露出的桌面
WINDOW_INSET = 24
#: 瀏覽器工具列不可能比這個高度還矮（深色主題時它也會「整列都是底色」）
MIN_CHROME_HEIGHT = 60
BOTTOM_MARGIN = 28

#: 紀錄裡不該出現的字串（洩漏本機路徑的跡象）
FORBIDDEN_IN_LOG = ("C:\\", "Users", ".toml", ".log")


class _Quiet:
    """截圖時不要被對話框擋住。"""

    def showinfo(self, *args, **kwargs):
        return "ok"

    def showwarning(self, *args, **kwargs):
        return "ok"

    def showerror(self, *args, **kwargs):
        return "ok"

    def askokcancel(self, *args, **kwargs):
        return True

    def askyesno(self, *args, **kwargs):
        return True


def _grab(widget: tk.Misc, name: str, root: tk.Tk) -> None:
    """抓單一視窗（用視窗自己的座標，所以不含瀏覽器或桌面的東西）。"""
    widget.attributes("-topmost", True)
    widget.lift()
    root.update()
    time.sleep(0.5)
    root.update()
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    image = ImageGrab.grab(
        bbox=(x, y, x + widget.winfo_width(), y + widget.winfo_height())
    )
    image.save(DOCS / name)
    print(f"  {name}  {image.size[0]}x{image.size[1]}")


# ------------------------------------------------------------------ 監看頁面


def _is_background(pixel) -> bool:
    return all(abs(pixel[i] - PAGE_BG[i]) <= BG_TOLERANCE for i in range(3))


def _capture_dashboard(url: str) -> None:
    """開啟監看頁面並截圖；只留頁面內容，不會拍到工具列或桌面。"""
    subprocess.Popen(["cmd", "/c", "start", "", url])
    time.sleep(5.0)

    user32 = ctypes.windll.user32
    hits: list[int] = []

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if "DGLAB-CS2" in buf.value:
                    hits.append(hwnd)
        return True

    user32.EnumWindows(
        ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(callback), 0
    )
    if not hits:
        print("  !! 找不到瀏覽器視窗，跳過監看頁面截圖")
        return

    rect = wintypes.RECT()
    user32.GetWindowRect(hits[0], ctypes.byref(rect))
    full = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
    inner = full.crop(
        (
            WINDOW_INSET,
            WINDOW_INSET,
            full.width - WINDOW_INSET,
            full.height - WINDOW_INSET,
        )
    )

    width, height = inner.size
    xs = range(0, width, 6)

    def row_is_background(y: int) -> bool:
        return all(_is_background(inner.getpixel((x, y))) for x in xs)

    def row_has_content(y: int) -> bool:
        return any(not _is_background(inner.getpixel((x, y))) for x in xs)

    # 由上往下找第一列「整列都是底色」= 頁面頂端。
    #
    # 一定要從 MIN_CHROME_HEIGHT 之後才開始找：瀏覽器工具列本身有可能是深色
    # （深色主題），那時它也會「整列都是底色」，從 0 開始找就會切在工具列中間、
    # 把書籤一起留下來。工具列不可能比這個高度還矮。
    page_top = next(
        (
            y
            for y in range(MIN_CHROME_HEIGHT, height)
            if row_is_background(y)
        ),
        None,
    )
    if page_top is None:
        print("  !! 找不到頁面頂端，跳過監看頁面截圖（寧可沒有圖，也不要拍到書籤）")
        return

    content_bottom = next(
        (y for y in range(height - 1, page_top, -1) if row_has_content(y)), height - 1
    )
    bottom = min(height, content_bottom + BOTTOM_MARGIN)

    # 再一道保險：合理的頁面截圖不該佔滿整個視窗
    if bottom - page_top > height - MIN_CHROME_HEIGHT:
        print("  !! 裁切範圍不合理，跳過監看頁面截圖")
        return

    cropped = inner.crop((0, page_top, width, bottom))

    # 自我檢查：最上面幾列必須都是底色，否則就是工具列殘留 ——
    # 這時候**不要存檔**，免得把不該出現的東西寫進 repo。
    if any(row_has_content(y) for y in range(3)):
        print("  !! 裁切後頂端仍有非底色（可能是瀏覽器工具列），不存檔")
        return

    cropped.save(DOCS / "screenshot-dashboard.png")
    print(f"  screenshot-dashboard.png  {cropped.size}（工具列 {page_top}px 已切除）")


# ------------------------------------------------------------------ 主流程


def main() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    config = dataclasses.replace(
        load_config(str(ROOT / "config.toml")),
        gsi=GsiConfig(host="127.0.0.1", port=port, token="DGLAB"),
        dashboard=dataclasses.replace(
            load_config(str(ROOT / "config.toml")).dashboard,
            enabled=True,
            host="127.0.0.1",
            port=0,
        ),
    )

    root = tk.Tk()
    app = DglabApp(root, config, config_path=str(ROOT / "config.toml"), dry_run=True)
    root.update_idletasks()
    root.geometry("")
    root.attributes("-topmost", True)
    root.lift()
    root.update()
    time.sleep(0.9)
    root.update()
    print(f"  視窗: {root.winfo_width()} x {root.winfo_height()}")

    app._on_start()
    for _ in range(80):
        root.update()
        time.sleep(0.1)
        if app.engine.status.state in (EngineState.WAITING_PAIR, EngineState.RUNNING):
            break
    time.sleep(2.2)
    for _ in range(30):
        root.update()
        time.sleep(0.1)

    address = app.engine.status.gsi_address
    for health, burning, kills in ((100, 0, 0), (72, 0, 0), (38, 0, 1), (23, 200, 2)):
        body = json.dumps(payload_with(health, burning=burning, kills=kills)).encode()
        request = urllib.request.Request(
            f"http://{address}/", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
        root.update()
        time.sleep(0.4)

    # --- 保護隱私的關鍵步驟：清掉啟動紀錄 ---
    app._clear_log()
    root.update()
    time.sleep(0.3)
    for health in (20, 17, 39):
        body = json.dumps(payload_with(health, burning=200, kills=0)).encode()
        request = urllib.request.Request(
            f"http://{address}/", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
        root.update()
        time.sleep(0.35)
    time.sleep(0.6)
    for _ in range(25):
        root.update()
        time.sleep(0.05)

    log_text = app.log_text.get("1.0", "end")
    leaks = [token for token in FORBIDDEN_IN_LOG if token in log_text]
    if leaks:
        root.destroy()
        raise SystemExit(f"!! 事件紀錄裡還有 {leaks}，截圖會洩漏本機路徑，中止")
    print("  事件紀錄檢查：乾淨")

    _grab(root, "screenshot-main.png", root)
    for index, name in enumerate(TAB_ORDER):
        app.notebook.select(index)
        root.update()
        time.sleep(0.2)
        target = {
            "疊加": "screenshot-stack.png",
            "輸出": "screenshot-output.png",
            "波形": "screenshot-wave.png",
            "Discord": "screenshot-discord.png",
            "監看": "screenshot-monitor.png",
            "熱鍵": "screenshot-hotkeys.png",
            "更新": "screenshot-update.png",
        }.get(name)
        if target:
            _grab(root, target, root)

    app.bottom_tabs.select(1)
    _grab(root, "screenshot-stats.png", root)
    app.bottom_tabs.select(0)

    # 波形編輯器
    app.notebook.select(TAB_ORDER.index("波形"))
    app._open_wave_editor()
    for _ in range(12):
        root.update()
        time.sleep(0.05)
    tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    if tops:
        _grab(tops[0], "screenshot-wave-editor.png", root)
        tops[0].destroy()

    # 使用教學
    app._show_tutorial()
    for _ in range(10):
        root.update()
        time.sleep(0.05)
    tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    if tops:
        _grab(tops[0], "screenshot-tutorial.png", root)
        tops[0].destroy()

    # GSI 檢查對話框（用空的資料夾模擬「還沒安裝」）
    empty = ROOT / "scratch_empty"
    empty.mkdir(exist_ok=True)
    check = cs2cfg.check_gsi_installation(directories=[empty], port=3199, token="DGLAB")

    def show_check():
        app._ask_about_missing_gsi(check)

    def capture_check():
        tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        if tops:
            _grab(tops[0], "screenshot-gsi-check.png", root)
            tops[0].destroy()

    root.after(400, show_check)
    root.after(1800, capture_check)
    root.after(2800, root.quit)
    root.mainloop()

    # 監看頁面（瀏覽器）
    dashboard_url = app.engine.dashboard_url
    app._hotkeys.stop()
    app.engine.shutdown()
    try:
        root.destroy()
    except tk.TclError:
        pass
    import shutil

    shutil.rmtree(empty, ignore_errors=True)

    if dashboard_url:
        _capture_dashboard(dashboard_url)

    print("完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
