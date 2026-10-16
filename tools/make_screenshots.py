"""重新產生 README 用的所有截圖（深色主題）。"""

import dataclasses
import json
import logging
import shutil
import socket
import sys
import time
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, ".")
logging.basicConfig(level=logging.WARNING)

import tkinter as tk  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import cs2cfg  # noqa: E402
import gui as gui_module  # noqa: E402
from config import GsiConfig, load_config  # noqa: E402
from engine import EngineState  # noqa: E402
from gui import DglabApp  # noqa: E402
from tests.test_gsi import payload_with  # noqa: E402

DOCS = Path("docs")
DOCS.mkdir(exist_ok=True)
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = int(s.getsockname()[1])

config = dataclasses.replace(
    load_config("config.toml"),
    gsi=GsiConfig(host="127.0.0.1", port=PORT, token="DGLAB"),
)


class Quiet:
    def showinfo(self, *a, **k):
        return "ok"

    def showwarning(self, *a, **k):
        return "ok"

    def showerror(self, *a, **k):
        return "ok"

    def askokcancel(self, *a, **k):
        return True

    def askyesno(self, *a, **k):
        return True


root = tk.Tk()
app = DglabApp(root, config, config_path="config.toml", dry_run=True)
root.update_idletasks()
root.geometry("")
root.attributes("-topmost", True)
root.lift()
root.update()
time.sleep(0.9)
root.update()
print(f"視窗大小: {root.winfo_width()} x {root.winfo_height()}")


def grab(widget, name):
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
    print(f"  {name}  {image.size[0]}x{image.size[1]}  {(DOCS / name).stat().st_size // 1024} KB")


# --- 主畫面（餵假的遊戲資料讓它看起來是活的）---
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
    time.sleep(0.35)
time.sleep(0.6)
for _ in range(20):
    root.update()
    time.sleep(0.05)

grab(root, "screenshot-main.png")
app.notebook.select(2)
grab(root, "screenshot-output.png")

# --- Discord 分頁（真的去讀圖片清單）---
app.vars["discord_client_id"].set("1560163564839510086")
app.vars["discord_enabled"].set(True)
app._maybe_fetch_discord_assets(explicit=False)
deadline = time.time() + 20
while time.time() < deadline and app._assets_fetching:
    root.update()
    time.sleep(0.1)
for _ in range(20):
    root.update()
    time.sleep(0.05)
app.notebook.select(5)
grab(root, "screenshot-discord.png")

# --- 使用教學 ---
app._show_tutorial()
for _ in range(10):
    root.update()
    time.sleep(0.05)
tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
if tops:
    grab(tops[0], "screenshot-tutorial.png")
    tops[0].destroy()

# --- GSI 把關對話框 ---
empty = Path("scratch_empty")
empty.mkdir(exist_ok=True)
check = cs2cfg.check_gsi_installation(directories=[empty], port=3199, token="DGLAB")


def show_check():
    app._ask_about_missing_gsi(check)


def capture_check():
    tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    if tops:
        grab(tops[0], "screenshot-gsi-check.png")
        tops[0].destroy()


root.after(400, show_check)
root.after(1800, capture_check)
root.after(2800, root.quit)
root.mainloop()

app.engine.shutdown()
try:
    root.destroy()
except tk.TclError:
    pass
shutil.rmtree(empty, ignore_errors=True)
print("完成")
