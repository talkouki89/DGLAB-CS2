"""全域熱鍵（global hotkey），讓 CS2 全螢幕時也能呼叫本程式。

為什麼要自己刻而不用第三方套件？因為這個專案只依賴標準函式庫，而 Win32 的
``RegisterHotKey`` 本來就夠用：它是由作業系統層級攔截按鍵，所以即使 CS2
用獨占全螢幕把畫面搶走，熱鍵照樣會觸發。

設計上的幾個重點：

- **訊息佇列是「執行緒」的，不是「視窗」的。** ``RegisterHotKey`` 之後，
  ``WM_HOTKEY`` 只會送進「呼叫註冊的那個執行緒」的訊息佇列。所以註冊與
  ``GetMessageW`` 迴圈都必須待在專屬的背景執行緒裡；主執行緒只負責派工作。
- **跨執行緒派工用 ``queue.Queue`` ＋ ``PostThreadMessageW``。** 背景執行緒
  平常阻塞在 ``GetMessageW``，主執行緒把工作丟進佇列後送一個 ``WM_APP``
  把它叫醒。停止時送 ``WM_QUIT``，阻塞中的 ``GetMessageW`` 就會返回 0。
- **``apply()`` 是同步的。** 呼叫者要拿到「哪幾個鍵註冊失敗」的結果，所以
  背景執行緒做完事會把結果放進另一個佇列，主執行緒等它（有逾時，不會卡死）。
- **非 Windows 必須能安全匯入。** 這個檔案要能被人拿到 Linux／macOS 上讀，
  因此 ``ctypes.windll`` 只在 ``sys.platform`` 是 Windows 時才碰，
  :meth:`HotkeyManager.apply` 在其他平台會回報「只支援 Windows」而不是拋例外。
- **回呼在背景執行緒被呼叫。** 想碰 tkinter 或 asyncio 的話，呼叫者要自己
  丟回主執行緒（``root.after`` / ``loop.call_soon_threadsafe``）。
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- 修飾鍵常數

#: 對應 Win32 的 ``MOD_ALT``
MOD_ALT = 0x0001
#: 對應 Win32 的 ``MOD_CONTROL``
MOD_CONTROL = 0x0002
#: 對應 Win32 的 ``MOD_SHIFT``
MOD_SHIFT = 0x0004
#: 對應 Win32 的 ``MOD_WIN``
MOD_WIN = 0x0008
#: 按住不放時不要連續觸發（Windows 7 以上支援）
MOD_NOREPEAT = 0x4000

#: 所有修飾鍵位元的聯集，用來檢查「至少有一個修飾鍵」
_ALL_MODIFIERS = MOD_ALT | MOD_CONTROL | MOD_SHIFT | MOD_WIN

#: 錯誤代碼：這個組合鍵已經被其他程式註冊走了
ERROR_HOTKEY_ALREADY_REGISTERED = 1409


# ------------------------------------------------------------- Win32 常數

#: 熱鍵觸發時送到訊息佇列的視窗訊息
WM_HOTKEY = 0x0312
#: ``WM_QUIT`` 會讓 ``GetMessageW`` 回傳 0，是我們用來結束迴圈的訊號
WM_QUIT = 0x0012
#: 我們自己拿來當「有事要忙，起床」的訊號
WM_APP = 0x8000

#: 使用者自訂訊息的起點，拿來當「有工作要做」的喚醒訊號
WM_USER = 0x0400

#: ``PeekMessage`` 的選項：只看訊息、不從佇列移除
PM_NOREMOVE = 0x0000

#: ``RegisterHotKey`` 的識別碼起始值。選 ``0xB000`` 是刻意避開微軟文件
#: 建議的 ``0x0000``～``0xBFFF`` 之中的低段，減少和別的程式撞號的機會。
_HOTKEY_ID_BASE = 0xB000

#: 等背景執行緒回報結果的預設逾時（秒）
DEFAULT_TIMEOUT = 3.0

#: 停止背景執行緒時，最多等它多久（秒）
_STOP_JOIN_TIMEOUT = 1.0

#: 非 Windows 平台時，:meth:`HotkeyManager.apply` 回報錯誤用的鍵名
_ALL_KEY = "__all__"

#: 模組層級的錯誤訊息
UNSUPPORTED_PLATFORM = "全域熱鍵只支援 Windows"
WORKER_TIMEOUT_MESSAGE = "等不到背景執行緒回報，熱鍵可能沒有生效"
WORKER_UNAVAILABLE_MESSAGE = "熱鍵背景執行緒沒有啟動"


class HotkeyError(Enum):
    """熱鍵註冊失敗的原因。

    :meth:`HotkeyManager.apply` 對外回傳的是中文訊息，這個列舉方便程式
    自己判斷原因（例如「被占用」時可以提示使用者換一個組合鍵）。
    """

    UNSUPPORTED_PLATFORM = "unsupported_platform"
    PARSE_ERROR = "parse_error"
    ALREADY_REGISTERED = "already_registered"
    DENIED = "denied"
    INVALID_HOTKEY = "invalid_hotkey"
    FAILED = "failed"

    def message(self, detail: str = "") -> str:
        """把錯誤原因轉成一句看得懂的中文訊息。"""
        base = {
            HotkeyError.UNSUPPORTED_PLATFORM: UNSUPPORTED_PLATFORM,
            HotkeyError.PARSE_ERROR: "熱鍵字串無法解析",
            HotkeyError.ALREADY_REGISTERED: "這個組合鍵已經被其他程式占用",
            HotkeyError.DENIED: "權限不足，無法註冊熱鍵"
            "（可能被其他程式獨占或需要較高權限）",
            HotkeyError.INVALID_HOTKEY: "這個組合鍵不合法（鍵碼或修飾鍵有問題）",
            HotkeyError.FAILED: "註冊熱鍵失敗",
        }[self]
        return f"{base}：{detail}" if detail else base


# ------------------------------------------------------------ 按鍵名稱對照

#: 修飾鍵名稱 → ``MOD_*`` 位元。順序不重要，輸出時另有固定順序。
_MODIFIER_NAMES: dict[str, int] = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "meta": MOD_WIN,
    "super": MOD_WIN,
    "cmd": MOD_WIN,
    "command": MOD_WIN,
}

#: 按鍵名稱 → Win32 虛擬鍵碼。名稱一律小寫，比對前會先正規化。
_KEY_NAMES: dict[str, int] = {}

#: 虛擬鍵碼 → 正規名稱（每個鍵碼只留第一個登記的名稱）
_CANONICAL_BY_VK: dict[int, str] = {}


def _add_key(canonical: str, vk: int, *aliases: str) -> None:
    """登記一個按鍵名稱及其別名。

    別名和各種常見寫法都收進來（例如 ``pagedown`` 與 ``pgdn``），但只有
    ``canonical`` 會被 :func:`name_for_vk` 輸出，這樣格式才會穩定。
    """
    _KEY_NAMES[canonical] = vk
    for alias in aliases:
        _KEY_NAMES[alias] = vk
    _CANONICAL_BY_VK.setdefault(vk, canonical)


# 控制鍵
_add_key("backspace", 0x08, "back", "bksp")
_add_key("tab", 0x09)
_add_key("enter", 0x0D, "return")
_add_key("esc", 0x1B, "escape")
_add_key("space", 0x20, "spacebar", "空白鍵")
_add_key("capslock", 0x14, "caps")
_add_key("pause", 0x13)

# 編輯鍵
_add_key("insert", 0x2D, "ins")
_add_key("delete", 0x2E, "del")
_add_key("home", 0x24)
_add_key("end", 0x23)
_add_key("pageup", 0x21, "pgup")
_add_key("pagedown", 0x22, "pgdn")

# 方向鍵
_add_key("left", 0x25, "arrowleft")
_add_key("up", 0x26, "arrowup")
_add_key("right", 0x27, "arrowright")
_add_key("down", 0x28, "arrowdown")

# 功能鍵
for _index in range(1, 25):
    _add_key(f"f{_index}", 0x70 + _index - 1)

# 標點與 OEM 鍵
_add_key("semicolon", 0xBA, ";", "分號")
_add_key("equal", 0xBB, "=", "equals", "plus")
_add_key("comma", 0xBC, ",", "逗號")
_add_key("minus", 0xBD, "-", "dash")
_add_key("period", 0xBE, ".", "dot")
_add_key("slash", 0xBF, "/")
_add_key("backtick", 0xC0, "`", "grave")
_add_key("bracketleft", 0xDB, "[")
_add_key("backslash", 0xDC, "\\")
_add_key("bracketright", 0xDD, "]")
_add_key("quote", 0xDE, "'", "apostrophe")

# 右側修飾鍵（左右要區分時才用得到）
_add_key("lshift", 0xA0, "leftshift")
_add_key("rshift", 0xA1, "rightshift")
_add_key("lctrl", 0xA2, "leftctrl", "leftcontrol")
_add_key("rctrl", 0xA3, "rightctrl", "rightcontrol")
_add_key("lalt", 0xA4, "leftalt")
_add_key("ralt", 0xA5, "rightalt")
_add_key("lwin", 0x5B, "leftwin")
_add_key("rwin", 0x5C, "rightwin")

# 數字鍵盤（NumLock 開啟時）
_add_key("numpad0", 0x60)
_add_key("numpad1", 0x61)
_add_key("numpad2", 0x62)
_add_key("numpad3", 0x63)
_add_key("numpad4", 0x64)
_add_key("numpad5", 0x65)
_add_key("numpad6", 0x66)
_add_key("numpad7", 0x67)
_add_key("numpad8", 0x68)
_add_key("numpad9", 0x69)
_add_key("multiply", 0x6A, "numpadmultiply", "*")
_add_key("add", 0x6B, "numpadadd", "+")
_add_key("subtract", 0x6D, "numpadsubtract")
_add_key("decimal", 0x6E, "numpaddecimal")
_add_key("divide", 0x6F, "numpaddivide")
_add_key("numlock", 0x90)


def _normalize(name: str) -> str:
    """把使用者輸入的名稱正規化：去空白、轉小寫。"""
    return name.strip().lower()


#: 寫得出來的字元 → 虛擬鍵碼（讓 ``^``、``!`` 這種寫法也能用）
_CHAR_KEYS: dict[str, int] = {
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
    "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
    "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
    "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
    "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59,
    "z": 0x5A,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD,
    ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB,
    "\\": 0xDC, "]": 0xDD, "'": 0xDE, "*": 0x6A, "+": 0x6B,
}
_CHAR_KEYS.update(_KEY_NAMES)

# 英文字母與數字的「正規」寫法就是它自己（小寫）
for _char, _vk in list(_CHAR_KEYS.items()):
    _CANONICAL_BY_VK.setdefault(_vk, _char)


def vk_for_name(name: str) -> int | None:
    """把按鍵名稱換成 Win32 虛擬鍵碼，認不出來時回傳 ``None``。

    大小寫與前後空白都不拘，例如 ``"F1"``、``" f1 "`` 都等於 ``0x70``。
    """
    if not isinstance(name, str):
        return None
    return _CHAR_KEYS.get(_normalize(name))


def name_for_vk(vk: int) -> str | None:
    """把 Win32 虛擬鍵碼換成正規的按鍵名稱，認不出來時回傳 ``None``。

    同一個鍵碼可能有很多別名（``esc`` / ``escape``），這裡只輸出正規名稱。
    """
    if isinstance(vk, bool) or not isinstance(vk, int):
        return None
    return _CANONICAL_BY_VK.get(vk)


def _modifier_text(modifiers: int) -> list[str]:
    """把修飾鍵位元轉成固定順序的名稱清單：ctrl、alt、shift、win。"""
    parts: list[str] = []
    if modifiers & MOD_CONTROL:
        parts.append("ctrl")
    if modifiers & MOD_ALT:
        parts.append("alt")
    if modifiers & MOD_SHIFT:
        parts.append("shift")
    if modifiers & MOD_WIN:
        parts.append("win")
    return parts


def format_hotkey(modifiers: int, vk: int) -> str:
    """組成熱鍵字串，修飾鍵順序固定為 ``ctrl+alt+shift+win+<鍵>``。

    範例：``format_hotkey(MOD_CONTROL | MOD_ALT, 0x53)`` 得到 ``"ctrl+alt+s"``。
    """
    name = name_for_vk(vk)
    if name is None:
        name = f"0x{vk:02X}"
    parts = _modifier_text(modifiers)
    parts.append(name)
    return "+".join(parts)


def parse_hotkey(text: str) -> Hotkey | None:
    """把 ``"Ctrl + Alt + S"`` 這樣的字串解析成 :class:`Hotkey`。

    規則：

    - 大小寫不拘、空白不拘、修飾鍵順序不拘。
    - **至少要有一個修飾鍵**（純 ``"s"`` 不接受，否則會把全系統的按鍵搶走）。
    - 只有一個「主鍵」，多一個或少一個都算失敗。
    - 任何解析不出來的狀況都回傳 ``None``，不拋例外，方便直接餵使用者輸入。
    """
    # 全形加號也一起收，複製貼上時很常見
    return _parse_tokens(text, ("+", "＋"))


def _parse_tokens(text: str, separators: tuple[str, ...]) -> Hotkey | None:
    """實際的解析工作，``separators`` 讓我們可以接受全形加號。"""
    if not isinstance(text, str) or not text.strip():
        return None
    normalized = text
    for separator in separators[1:]:
        normalized = normalized.replace(separator, separators[0])
    tokens = [token.strip() for token in normalized.split(separators[0])]
    if any(not token for token in tokens):
        # "ctrl+" 或 "ctrl++s" 這種殘缺寫法
        return None

    modifiers = 0
    vk: int | None = None
    for token in tokens:
        key = _normalize(token)
        modifier = _MODIFIER_NAMES.get(key)
        if modifier is not None:
            modifiers |= modifier
            continue
        if vk is not None:
            # 兩個主鍵，例如 "ctrl+a+b"
            return None
        vk = _CHAR_KEYS.get(key)
        if vk is None:
            return None

    if vk is None or not modifiers & _ALL_MODIFIERS:
        return None
    return Hotkey(modifiers=modifiers, vk=vk)


@dataclass(frozen=True)
class Hotkey:
    """一組修飾鍵加上一個主鍵。

    ``modifiers`` 是 ``MOD_*`` 的位元組合，``vk`` 是 Win32 虛擬鍵碼。
    """

    modifiers: int
    vk: int

    @property
    def text(self) -> str:
        """正規化後的字串，例如 ``"ctrl+alt+s"``。"""
        return format_hotkey(self.modifiers, self.vk)

    def __str__(self) -> str:
        return self.text


# --------------------------------------------------------------- 錯誤訊息


def describe_error(code: int) -> str:
    """把 Win32 錯誤碼轉成看得懂的中文訊息。"""
    if code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return HotkeyError.ALREADY_REGISTERED.message(f"錯誤碼 {code}")
    if code == 5:  # ERROR_ACCESS_DENIED
        return HotkeyError.DENIED.message(f"錯誤碼 {code}")
    if code == 1400:  # ERROR_INVALID_WINDOW_HANDLE
        return "視窗代碼不合法（錯誤碼 1400）"
    if code == 1419:  # ERROR_HOTKEY_NOT_REGISTERED
        return "這個熱鍵沒有註冊過（錯誤碼 1419）"
    return HotkeyError.FAILED.message(f"錯誤碼 {code}")


# ------------------------------------------------------------ Win32 綁定層

#: 背景執行緒用的 ``user32`` / ``kernel32`` 函式庫；非 Windows 時是 ``None``
_user32 = None
_kernel32 = None
_MSG = None


def _load_win32() -> None:
    """載入 ``user32`` / ``kernel32`` 並補上 ``ctypes`` 沒附的常數。

    非 Windows 或載入失敗時安靜地把函式庫留成 ``None``，
    :meth:`HotkeyManager.apply` 會據此回報「只支援 Windows」。
    """
    global _user32, _kernel32, _MSG

    if not sys.platform.startswith("win"):
        return
    try:
        _user32 = ctypes.windll.user32
        _kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):  # pragma: no cover - 只有在奇怪環境才會踩到
        logger.warning("載入 user32/kernel32 失敗，全域熱鍵將無法使用")
        _user32 = None
        _kernel32 = None
        return

    if not hasattr(wintypes, "ULONG_PTR"):
        # 32 位元是 4 bytes、64 位元是 8 bytes
        wintypes.ULONG_PTR = (  # type: ignore[attr-defined]
            ctypes.c_ulong if ctypes.sizeof(ctypes.c_void_p) == 4 else ctypes.c_ulonglong
        )
    if not hasattr(wintypes, "MSG"):
        class _WindowsMessage(ctypes.Structure):
            """``ctypes.wintypes`` 沒有提供 ``MSG`` 時自己補一個。"""

            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt_x", wintypes.LONG),
                ("pt_y", wintypes.LONG),
            ]

        wintypes.MSG = _WindowsMessage  # type: ignore[attr-defined]

    _MSG = wintypes.MSG


#: 讀取 Win32 用的 ``user32``；模組載入時就先抓好，非 Windows 會是 ``None``
_load_win32()


# ------------------------------------------------------------- 熱鍵管理員


def _result_queue() -> "queue.SimpleQueue[str]":
    """建立背景執行緒回報結果用的佇列（抽出來方便測試替換）。"""
    return queue.SimpleQueue()


class HotkeyManager:
    """用 Win32 ``RegisterHotKey`` 註冊全域熱鍵。

    ``RegisterHotKey`` 的訊息只會送到「註冊它的那個執行緒」的訊息佇列，
    所以一定要在專屬執行緒裡跑 ``GetMessage`` 迴圈。這個類別負責建立那個
    執行緒、把註冊／取消註冊的動作丟進去，並把結果同步帶回來。

    .. warning::
        ``on_trigger(名稱)`` 是在**背景執行緒**被呼叫的。如果要更新 tkinter
        介面，請自己丟回主執行緒，例如 ``root.after(0, callback, name)``；
        要碰 asyncio 就用 ``loop.call_soon_threadsafe``。直接在回呼裡碰 UI
        會踩到 tkinter 的執行緒限制。

    Args:
        on_trigger: 熱鍵觸發時要呼叫的函式，參數是綁定時用的名稱。
            這個函式丟出的例外會被記在 log，不會讓背景執行緒死掉。
        timeout: 等背景執行緒回報註冊結果的秒數，逾時就回報錯誤而不是卡死。
    """

    def __init__(
        self,
        on_trigger: Callable[[str], None],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._on_trigger = on_trigger
        self._timeout = float(timeout)
        # 建構時就把 Win32 繫結抓下來，之後背景執行緒都用這一組。
        # 這樣測試可以替換模組層級的繫結，也不會互相污染。
        self._user32 = _user32
        self._kernel32 = _kernel32
        self._msg_type = _MSG

        self._tasks: queue.SimpleQueue = queue.SimpleQueue()
        self._results = _result_queue()
        # 保護 _active 與 _pending；worker 只做很短的操作，不會死鎖
        self._lock = threading.Lock()
        self._pending: dict[int, str] = {}
        self._active: dict[str, Hotkey] = {}

        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._stop_requested = False
        self._ready = threading.Event()

    # ------------------------------------------------------------ 生命週期

    @property
    def active(self) -> dict[str, Hotkey]:
        """目前成功註冊的熱鍵，``{名稱: Hotkey}``。"""
        with self._lock:
            return dict(self._active)

    @property
    def thread_id(self) -> int:
        """背景執行緒的執行緒代碼；還沒啟動或已停止時是 ``0``。"""
        return self._thread_id

    def __enter__(self) -> "HotkeyManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def apply(self, bindings: Mapping[str, str]) -> dict[str, str]:
        """設定要註冊的熱鍵。``bindings`` 是 ``{名稱: 熱鍵字串}``。

        回傳 ``{名稱: 錯誤訊息}``，只包含失敗的項目（組合鍵被其他程式占用、
        權限不足、或字串解析不出來）。空字典代表全部成功。
        已經註冊的會先全部取消，再重新註冊。

        非 Windows 平台不會拋例外，而是回傳
        ``{"__all__": "全域熱鍵只支援 Windows"}``。
        """
        if not self._windows_available():
            logger.info("非 Windows 平台，略過熱鍵註冊")
            return {_ALL_KEY: UNSUPPORTED_PLATFORM}

        parsed, errors = self._parse_bindings(bindings)
        if not self._ensure_worker():
            errors[_ALL_KEY] = WORKER_UNAVAILABLE_MESSAGE
            return errors

        plan = {
            _HOTKEY_ID_BASE + index: name for index, (name, _) in enumerate(parsed)
        }
        with self._lock:
            self._pending = dict(plan)

        assigned, failures = self._dispatch(("apply", (parsed, plan)))
        if assigned is None:
            errors[_ALL_KEY] = failures.pop(_ALL_KEY, WORKER_TIMEOUT_MESSAGE)
        errors.update(failures)
        return errors

    def clear(self) -> None:
        """取消全部註冊（Windows 以外或沒啟動時什麼都不做）。"""
        if not self._windows_available() or self._thread is None:
            return
        if not self._thread.is_alive():
            self._reset_state()
            return
        self._dispatch(("clear", None))

    def stop(self) -> None:
        """停止背景執行緒；可以重複呼叫，已經停了就什麼都不做。

        結束之後 ``self._thread`` 會留著（只是已經死掉），這樣
        :meth:`_ensure_worker` 下次就知道要重新開一條。
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            # 沒啟動過、或已經停了：只要把狀態清乾淨
            self._reset_state()
            self._thread_id = 0
            return

        thread_id = self._thread_id
        if thread_id and self._user32 is not None:
            # 叫醒阻塞在 GetMessageW 的迴圈，讓它看到 WM_QUIT 後結束
            if not self._user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0):
                logger.debug("PostThreadMessageW(WM_QUIT) 失敗，改用旗標通知")

        self._stop_requested = True
        if thread.is_alive() and thread is not threading.current_thread():
            # 如果在回呼裡呼叫 stop()，這裡就是同一個執行緒，join 自己會死鎖
            thread.join(timeout=_STOP_JOIN_TIMEOUT)
            if thread.is_alive():
                logger.warning("熱鍵背景執行緒沒有在時限內結束")

        self._thread_id = 0
        self._reset_state()

    # -------------------------------------------------------- 內部：派工

    def _windows_available(self) -> bool:
        """這個模組現在能不能真的註冊熱鍵。"""
        if not sys.platform.startswith("win"):
            return False
        if (
            self._user32 is None
            or self._kernel32 is None
            or self._msg_type is None
        ):
            logger.warning("Win32 函式庫不可用，無法註冊熱鍵")
            return False
        return True

    @staticmethod
    def _parse_bindings(
        bindings: Mapping[str, str],
    ) -> tuple[list[tuple[str, Hotkey]], dict[str, str]]:
        """先把字串解析成 :class:`Hotkey`，解析失敗的挑出來。

        Returns:
            ``(成功解析的 [(名稱, Hotkey)], {名稱: 錯誤訊息})``
        """
        parsed: list[tuple[str, Hotkey]] = []
        errors: dict[str, str] = {}
        for name, text in bindings.items():
            hotkey = parse_hotkey(text)
            if hotkey is None:
                errors[str(name)] = HotkeyError.PARSE_ERROR.message(repr(text))
                logger.warning("熱鍵 %s 的字串無法解析：%r", name, text)
                continue
            parsed.append((str(name), hotkey))
        return parsed, errors

    def _post(self, thread_id: int) -> bool:
        """喚醒背景執行緒，失敗時重試幾次。

        正常情況下第一次就會成功（背景執行緒已經先用 ``PeekMessage`` 建好
        佇列）。留著重試是為了保險：如果執行緒閒置很久之後被系統回收了
        佇列，這裡可以自己救回來，而不是讓熱鍵默默失效。
        """
        assert self._user32 is not None
        for attempt in range(3):
            if self._user32.PostThreadMessageW(thread_id, WM_APP, 0, 0):
                return True
            if attempt < 2:
                time.sleep(0.02)
        return False

    def _ensure_worker(self) -> bool:
        """確認背景執行緒活著，需要時把它啟動（非 Windows 直接回報失敗）。"""
        if not self._windows_available():
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        return self._start_worker()

    def _start_worker(self) -> bool:
        """建立並啟動背景執行緒，等到它真的開好訊息佇列才回傳。"""
        self._ready.clear()
        self._stop_requested = False
        self._thread_id = 0
        self._thread = threading.Thread(
            target=self._worker,
            name="dglab-hotkeys",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=self._timeout):
            logger.error("熱鍵背景執行緒沒有在時限內就緒")
            return False
        return True

    def _dispatch(
        self, task: tuple[str, object | None]
    ) -> tuple[dict[int, str] | None, dict[str, str]]:
        """把工作丟進佇列並等背景執行緒回報。

        Returns:
            ``(背景執行緒回報的結果, 失敗訊息)``；逾時時第一個值是 ``None``，
            代表「不確定有沒有註冊成功」。
        """
        # 清掉上一輪逾時留下的殘骸，避免把舊結果當成這一次的結果
        self._drain_results()
        self._tasks.put(task)

        thread_id = self._thread_id
        if not thread_id or self._user32 is None:
            return None, {_ALL_KEY: WORKER_UNAVAILABLE_MESSAGE}
        if not self._post(thread_id):
            code = self._last_error()
            logger.error("無法喚醒熱鍵執行緒（錯誤碼 %s）", code)
            return None, {_ALL_KEY: f"無法喚醒熱鍵執行緒（錯誤碼 {code}）"}

        try:
            result = self._await_result(self._timeout)
        except queue.Empty:
            logger.error("等不到熱鍵執行緒回報（逾時 %.1f 秒）", self._timeout)
            # 把我們留下的「佔位」拿走；背景執行緒之後如果真的做完，
            # 下一次 apply() 開頭也會再清一次
            self._drain_results()
            return None, {_ALL_KEY: WORKER_TIMEOUT_MESSAGE}
        return result

    def _await_result(self, timeout: float) -> tuple[dict[int, str], dict[str, str]]:
        """等背景執行緒回報結果，逾時就丟 :class:`queue.Empty`。"""
        return self._results.get(timeout=timeout)

    def _drain_results(self) -> None:
        """把佇列裡剩下的結果丟掉。"""
        while True:
            try:
                self._results.get_nowait()
            except queue.Empty:
                return

    def _reset_state(self) -> None:
        """清掉註冊狀態。"""
        with self._lock:
            self._active = {}
            self._pending = {}

    # ------------------------------------------------------ 內部：背景執行緒

    def _worker(self) -> None:
        """背景執行緒主體：``GetMessageW`` 迴圈 ＋ 處理主執行緒丟來的工作。"""
        user32 = self._user32
        kernel32 = self._kernel32
        msg_type = self._msg_type
        if user32 is None or kernel32 is None or msg_type is None:  # pragma: no cover
            return

        # 先把訊息佇列建立起來。
        #
        # Windows 是「執行緒第一次呼叫訊息相關函式時」才替它建立訊息佇列，
        # 光拿到 thread id 還不夠。這裡用 PeekMessage 逼系統建立佇列，
        # 之後主執行緒的 PostThreadMessageW 才不會隨機拿到
        # 1444（ERROR_INVALID_THREAD_ID）——那個錯誤會讓熱鍵偶發性註冊失敗。
        self._thread_id = kernel32.GetCurrentThreadId()
        msg = msg_type()
        user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)
        self._ready.set()

        while not self._stop_requested:
            received = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if received == 0:  # WM_QUIT
                break
            if received == -1:  # 取得訊息失敗
                logger.error("GetMessageW 失敗（錯誤碼 %s）", self._last_error())
                break

            self._process_task()
            if msg.message == WM_HOTKEY:
                self._fire(int(msg.wParam))

        self._ready.clear()
        with self._lock:
            self._pending = {}
            self._active = {}
        logger.debug("熱鍵背景執行緒結束")

    def _process_task(self) -> None:
        """處理一件主執行緒派來的工作（沒有的話直接返回）。"""
        try:
            task = self._tasks.get_nowait()
        except queue.Empty:
            return

        action, payload = task
        try:
            if action == "apply":
                parsed, plan = payload  # type: ignore[misc]
                result = self._do_apply(parsed, plan)
            else:
                result = self._do_clear()
        except OSError as exc:  # pragma: no cover - 防禦性
            logger.exception("處理熱鍵工作時發生錯誤")
            result = None, {_ALL_KEY: str(exc)}
        self._results.put(result)

    def _do_apply(
        self,
        parsed: list[tuple[str, Hotkey]],
        plan: dict[int, str],
    ) -> tuple[dict[int, str] | None, dict[str, str]]:
        """在背景執行緒裡真的呼叫 ``RegisterHotKey``。

        Returns:
            ``(成功的 {識別碼: 名稱}, {名稱: 錯誤訊息})``
        """
        self._do_clear()

        lookup = dict(parsed)
        registered: dict[int, str] = {}
        failures: dict[str, str] = {}

        for hotkey_id, name in sorted(plan.items()):
            hotkey = lookup.get(name)
            if hotkey is None:  # pragma: no cover - 防禦性
                continue
            flags = hotkey.modifiers | MOD_NOREPEAT
            if self._user32.RegisterHotKey(None, hotkey_id, flags, hotkey.vk):
                registered[hotkey_id] = name
                continue

            code = self._last_error()
            reason = describe_error(code)
            failures[name] = reason
            logger.warning("註冊熱鍵 %s（%s）失敗：%s", name, hotkey.text, reason)

        with self._lock:
            self._pending = dict(registered)
            self._active = {name: lookup[name] for name in registered.values()}
        return registered, failures

    def _do_clear(self) -> tuple[dict[int, str] | None, dict[str, str]]:
        """在背景執行緒裡取消目前所有註冊。"""
        with self._lock:
            pending = dict(self._pending)
        for hotkey_id in sorted(pending):
            if not self._user32.UnregisterHotKey(None, hotkey_id):
                logger.debug(
                    "取消熱鍵（識別碼 0x%X）失敗（錯誤碼 %s）",
                    hotkey_id,
                    self._last_error(),
                )
        with self._lock:
            self._pending = {}
            self._active = {}
        return {}, {}

    def _fire(self, hotkey_id: int) -> None:
        """把 ``WM_HOTKEY`` 的識別碼換回名稱並呼叫回呼。"""
        with self._lock:
            name = self._active.get(hotkey_id)
        if name is None:
            name = self._pending.get(hotkey_id)
        if name is None:
            logger.debug("收到未知的熱鍵識別碼 0x%X，忽略", hotkey_id)
            return
        try:
            self._on_trigger(name)
        except Exception:  # noqa: BLE001 - 回呼的錯不該讓背景執行緒死掉
            logger.exception("熱鍵 %s 的回呼發生例外", name)


    def _last_error(self) -> int:
        """取得最近一次 Win32 呼叫的錯誤碼。

        依序問三個來源，第一個給出非 0 答案的就採用：

        1. ``kernel32.GetLastError()`` —— 正式環境的主要來源
        2. ``user32.last_error`` —— ctypes 開了 ``use_last_error`` 之後的快取
           （測試用的假物件也放在這個欄位）
        3. ``ctypes.get_last_error()`` —— 前兩者都拿不到時的保底

        三個都是 0（或都不存在）就回傳 0。
        """
        for source in (self._kernel32, self._user32):
            if source is None:
                continue
            getter = getattr(source, "GetLastError", None)
            if callable(getter):
                code = int(getter())
            else:
                code = int(getattr(source, "last_error", 0) or 0)
            if code:
                return code

        if self._kernel32 is None:
            return int(ctypes.get_last_error())
        return 0


__all__ = [
    "DEFAULT_TIMEOUT",
    "ERROR_HOTKEY_ALREADY_REGISTERED",
    "Hotkey",
    "HotkeyError",
    "HotkeyManager",
    "MOD_ALT",
    "MOD_CONTROL",
    "MOD_NOREPEAT",
    "MOD_SHIFT",
    "MOD_WIN",
    "UNSUPPORTED_PLATFORM",
    "WM_APP",
    "WM_HOTKEY",
    "WM_QUIT",
    "describe_error",
    "format_hotkey",
    "name_for_vk",
    "parse_hotkey",
    "vk_for_name",
]
