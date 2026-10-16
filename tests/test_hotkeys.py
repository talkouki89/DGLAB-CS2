"""全域熱鍵模組的測試。

這裡**絕對不會真的去註冊系統熱鍵**：所有會碰到 Win32 的路徑都用假的
``user32`` / ``kernel32`` 物件取代（見 :class:`FakeUser32`），背景執行緒
也不會跑真正的 ``GetMessageW`` 迴圈（見 :class:`FakeMessagePump`）。
所以整套測試可以離線、無互動地跑完。

非 Windows 的降級行為則用 ``mock.patch`` 把 ``hotkeys.sys.platform``
改成 ``"linux"`` 來驗證。

.. note::
    在 Windows 上跑測試時，``hotkeys._load_win32()`` 已經抓好真的
    ``ctypes.windll.user32``，所以每一個會用到它的測試都必須自己 patch 掉；
    :func:`make_manager` 會處理這件事。
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
import unittest
from ctypes import wintypes
from unittest import mock

import hotkeys
from hotkeys import (
    DEFAULT_TIMEOUT,
    ERROR_HOTKEY_ALREADY_REGISTERED,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    WM_APP,
    WM_HOTKEY,
    WM_QUIT,
    Hotkey,
    HotkeyError,
    HotkeyManager,
    describe_error,
    format_hotkey,
    name_for_vk,
    parse_hotkey,
    vk_for_name,
)

#: 模組內部常數的預期值，讓斷言不依賴實作細節
ID_BASE = 0xB000
#: 假執行緒代碼
THREAD_ID = 0x1234


class FakeUser32:
    """假的 ``user32``：記錄註冊／取消註冊，可設定要讓哪一個鍵失敗。

    ``GetMessageW`` 模擬「阻塞到有人送 ``WM_QUIT`` 為止」，這樣
    :meth:`HotkeyManager.stop` 的 ``join`` 會立刻成功，測試不必等逾時。
    """

    def __init__(
        self,
        *,
        fail_on: int | None = None,
        error_code: int = ERROR_HOTKEY_ALREADY_REGISTERED,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.registered: dict[int, tuple[int, int]] = {}
        self.unregistered: list[int] = []
        self.fail_on = fail_on
        self.error_code = error_code
        self.unregister_error: int | None = None
        self.last_error = 0
        self.quit_event = stop_event or threading.Event()
        self.get_message_calls = 0
        self.peek_message_calls = 0
        self.posts: list[tuple[int, int]] = []
        #: 還沒被 ``GetMessageW`` 取走的訊息（模擬執行緒的訊息佇列）
        self.pending: list[tuple[int, int]] = []
        self.wake = threading.Event()

    # --------------------------------------------------------- Win32 介面

    def RegisterHotKey(self, hwnd, hotkey_id: int, flags: int, vk: int) -> int:  # noqa: N802
        if self.fail_on == vk:
            self.last_error = self.error_code
            return 0
        self.registered[hotkey_id] = (flags, vk)
        return 1

    def UnregisterHotKey(self, hwnd, hotkey_id: int) -> int:  # noqa: N802
        self.unregistered.append(hotkey_id)
        if self.unregister_error is not None:
            self.last_error = self.unregister_error
            return 0
        self.registered.pop(hotkey_id, None)
        return 1

    def GetMessageW(self, msg_ref, hwnd, min_filter, max_filter) -> int:  # noqa: N802
        """模擬阻塞式的訊息迴圈。

        真的 ``GetMessageW`` 會卡住直到有訊息進來；這裡照樣卡住，只有兩種
        情況會返回：

        - ``stop()`` 送來 ``WM_QUIT`` → 回傳 0 讓 worker 跳出迴圈
        - 有 ``PostThreadMessageW`` 送來的工作 → 回傳 1，worker 就會去處理

        注意**不能**每次呼叫都回傳 0：那等於叫 worker 立刻結束，
        後續的 ``apply()`` 就會找不到活著的執行緒而逾時。
        """
        self.get_message_calls += 1
        while True:
            if self.pending:
                message, _wparam = self.pending.pop(0)
                # WM_QUIT 由「收到它的那一個 worker」消費掉，之後重新啟動的
                # worker 不會再看到它 —— 真的訊息佇列就是這個語意。
                # （用一個會一直保持設定的 Event 當開關的話，stop() 之後再
                # apply() 開的新 worker 會立刻收到「結束」而馬上死掉。）
                if message == WM_QUIT:
                    return 0
                return 1
            # 沒有訊息時睡一下，避免假迴圈變成吃滿 CPU 的忙迴圈
            self.wake.wait(timeout=0.02)
            self.wake.clear()

    def PeekMessageW(self, msg_ref, hwnd, min_filter, max_filter, remove) -> int:  # noqa: N802
        # hotkeys 用它逼系統建立訊息佇列；假的版本只要不炸就好
        self.peek_message_calls += 1
        return 0

    def PostThreadMessageW(self, thread_id, message, wparam, lparam) -> int:  # noqa: N802
        """把訊息放進假的訊息佇列（``WM_QUIT`` 會讓 ``GetMessageW`` 回傳 0）。"""
        self.posts.append((thread_id, message))
        self.pending.append((message, wparam))
        self.wake.set()
        if message == WM_QUIT:
            self.quit_event.set()
        return 1

    # ------------------------------------------------------------ 測試輔助

    @property
    def calls(self) -> list[tuple[int, int, int]]:
        """``(識別碼, flags, 虛擬鍵碼)``，依識別碼排序。"""
        return [
            (hotkey_id, flags, vk)
            for hotkey_id, (flags, vk) in sorted(self.registered.items())
        ]


class FakeMessage(ctypes.Structure):
    """假的 ``wintypes.MSG``，只要是真實的 ctypes 結構就好。

    ``hotkeys`` 會對它呼叫 ``ctypes.byref``，所以不能用普通的物件假裝。
    這裡故意只宣告欄位、大小不重要，因為假的 ``GetMessageW`` 不會去填它。
    """

    _fields_ = [
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
    ]


class FakeKernel32:
    """假的 ``kernel32``：只提供執行緒代碼與錯誤碼。"""

    def __init__(self, error: int = 0) -> None:
        self.error = error

    def GetCurrentThreadId(self) -> int:  # noqa: N802
        return THREAD_ID

    def GetLastError(self) -> int:  # noqa: N802
        return self.error


class FakeMessagePump:
    """``PostThreadMessageW`` 的觀察窗。

    真正的工作是 :class:`FakeUser32` 做的（``hotkeys`` 是透過
    ``_user32`` 呼叫它），這裡只提供好讀的斷言，並且在建立管理員時先把
    ``_thread_id`` 塞好，讓 ``_dispatch`` 不必等背景執行緒就緒。
    """

    def __init__(self, user32: FakeUser32) -> None:
        self.user32 = user32

    def install(self, manager: HotkeyManager) -> None:
        manager._thread_id = THREAD_ID
        manager._ready.set()

    @property
    def posts(self) -> list[tuple[int, int]]:
        return self.user32.posts

    @property
    def wake_ups(self) -> int:
        return sum(1 for _, message in self.posts if message == WM_APP)

    @property
    def quits(self) -> int:
        return sum(1 for _, message in self.posts if message == WM_QUIT)


class Recorder:
    """假的 ``on_trigger``：記錄被觸發的名稱，可選擇丟出例外。"""

    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.names: list[str] = []

    def __call__(self, name: str) -> None:
        self.names.append(name)
        if self.raises:
            raise RuntimeError("回呼爆炸了")


def make_manager(
    case: unittest.TestCase,
    *,
    user32: FakeUser32 | None = None,
    kernel32: FakeKernel32 | None = None,
    on_trigger=None,
    timeout: float = DEFAULT_TIMEOUT,
    platform: str = "win32",
    start_thread: bool = True,
) -> tuple[HotkeyManager, FakeUser32, FakeKernel32, FakeMessagePump]:
    """建立一個只使用假 Win32 的 :class:`HotkeyManager`。

    ``HotkeyManager`` 會在 ``__init__`` 就把 ``user32`` / ``kernel32``
    繫結抓成實例屬性（背景執行緒之後都用那一組），所以這裡直接換掉實例
    屬性，測試之間不會互相污染。所有 mock 都掛在 ``addCleanup`` 上，
    清理時會先停掉管理員再還原。
    """
    fake_user32 = user32 or FakeUser32()
    fake_kernel32 = kernel32 or FakeKernel32()
    pump = FakeMessagePump(fake_user32)

    case.addCleanup(mock.patch.object(hotkeys.sys, "platform", platform).stop)
    mock.patch.object(hotkeys.sys, "platform", platform).start()

    manager = HotkeyManager(on_trigger or Recorder(), timeout=timeout)
    case.addCleanup(manager.stop)
    manager._user32 = fake_user32
    manager._kernel32 = fake_kernel32
    manager._msg_type = FakeMessage
    pump.install(manager)

    if start_thread:
        # 走真正的 _start_worker：背景執行緒會跑 GetMessageW 迴圈，
        # 但那是假的版本（阻塞到 stop() 為止），所以不會碰到作業系統
        case.assertTrue(manager._ensure_worker(), "背景執行緒沒有就緒")
    return manager, fake_user32, fake_kernel32, pump


# ------------------------------------------------------------------ 按鍵名稱


class VkForNameTests(unittest.TestCase):
    def test_letters_and_digits(self):
        self.assertEqual(vk_for_name("s"), 0x53)
        self.assertEqual(vk_for_name("S"), 0x53)
        self.assertEqual(vk_for_name(" a "), 0x41)
        self.assertEqual(vk_for_name("z"), 0x5A)
        self.assertEqual(vk_for_name("0"), 0x30)
        self.assertEqual(vk_for_name("9"), 0x39)

    def test_function_keys(self):
        self.assertEqual(vk_for_name("f1"), 0x70)
        self.assertEqual(vk_for_name("F1"), 0x70)
        self.assertEqual(vk_for_name("f12"), 0x7B)
        self.assertEqual(vk_for_name("F24"), 0x87)

    def test_named_keys(self):
        cases = {
            "space": 0x20,
            "SPACE": 0x20,
            "pagedown": 0x22,
            "pgdn": 0x22,
            "pageup": 0x21,
            "pgup": 0x21,
            "escape": 0x1B,
            "esc": 0x1B,
            "enter": 0x0D,
            "return": 0x0D,
            "tab": 0x09,
            "backspace": 0x08,
            "insert": 0x2D,
            "delete": 0x2E,
            "home": 0x24,
            "end": 0x23,
            "capslock": 0x14,
            "numpad5": 0x65,
        }
        for name, vk in cases.items():
            with self.subTest(name=name):
                self.assertEqual(vk_for_name(name), vk)

    def test_arrow_keys(self):
        self.assertEqual(vk_for_name("left"), 0x25)
        self.assertEqual(vk_for_name("up"), 0x26)
        self.assertEqual(vk_for_name("right"), 0x27)
        self.assertEqual(vk_for_name("down"), 0x28)
        self.assertEqual(vk_for_name("arrowleft"), 0x25)
        self.assertEqual(vk_for_name("arrowup"), 0x26)

    def test_punctuation(self):
        self.assertEqual(vk_for_name(";"), 0xBA)
        self.assertEqual(vk_for_name("/"), 0xBF)
        self.assertEqual(vk_for_name("semicolon"), 0xBA)
        self.assertEqual(vk_for_name("backtick"), 0xC0)
        self.assertEqual(vk_for_name("quote"), 0xDE)

    def test_unknown_names(self):
        for name in ("nope", "", " ", "ctrl", "f25", "f0", "滑鼠左鍵"):
            with self.subTest(name=name):
                self.assertIsNone(vk_for_name(name))

    def test_non_string_is_ignored(self):
        for value in (None, 0x53, b"s", ["s"]):
            with self.subTest(value=value):
                self.assertIsNone(vk_for_name(value))


class NameForVkTests(unittest.TestCase):
    def test_known_codes(self):
        self.assertEqual(name_for_vk(0x53), "s")
        self.assertEqual(name_for_vk(0x70), "f1")
        self.assertEqual(name_for_vk(0x87), "f24")
        self.assertEqual(name_for_vk(0x22), "pagedown")
        self.assertEqual(name_for_vk(0x20), "space")
        self.assertEqual(name_for_vk(0x25), "left")
        self.assertEqual(name_for_vk(0x30), "0")

    def test_names_are_canonical_and_round_trip(self):
        # 同一組鍵碼只會有一個名字，而且要被 vk_for_name 認得
        for vk in (0x08, 0x0D, 0x1B, 0x20, 0x21, 0x22, 0x25, 0x30, 0x5A,
                   0x70, 0x87, 0xBA, 0xDE):
            with self.subTest(vk=hex(vk)):
                name = name_for_vk(vk)
                self.assertIsNotNone(name)
                self.assertEqual(vk_for_name(name), vk)

    def test_unknown_codes(self):
        for vk in (0x00, 0x01, 0xFF, -1, 999):
            with self.subTest(vk=vk):
                self.assertIsNone(name_for_vk(vk))

    def test_rejects_non_int(self):
        for value in (None, "s", [], True):
            with self.subTest(value=value):
                self.assertIsNone(name_for_vk(value))


# -------------------------------------------------------------------- 解析


class ParseHotkeyTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse_hotkey("ctrl+s"), Hotkey(MOD_CONTROL, 0x53))

    def test_case_insensitive(self):
        self.assertEqual(parse_hotkey("CTRL+S"), Hotkey(MOD_CONTROL, 0x53))
        self.assertEqual(
            parse_hotkey("Ctrl+Alt+S"), Hotkey(MOD_CONTROL | MOD_ALT, 0x53)
        )

    def test_whitespace_is_ignored(self):
        for text in (" ctrl + alt + s ", "ctrl+alt+s", "\tctrl+alt+s\n"):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_hotkey(text), Hotkey(MOD_CONTROL | MOD_ALT, 0x53)
                )

    def test_any_order(self):
        expected = Hotkey(MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x53)
        for text in ("ctrl+alt+shift+s", "shift+alt+ctrl+s", "alt+ctrl+shift+s"):
            with self.subTest(text=text):
                self.assertEqual(parse_hotkey(text), expected)

    def test_modifier_aliases(self):
        self.assertEqual(parse_hotkey("control+s"), Hotkey(MOD_CONTROL, 0x53))
        self.assertEqual(parse_hotkey("windows+s"), Hotkey(MOD_WIN, 0x53))
        self.assertEqual(parse_hotkey("meta+s"), Hotkey(MOD_WIN, 0x53))
        self.assertEqual(parse_hotkey("super+s"), Hotkey(MOD_WIN, 0x53))
        self.assertEqual(parse_hotkey("cmd+s"), Hotkey(MOD_WIN, 0x53))

    def test_repeated_modifier_is_merged(self):
        self.assertEqual(parse_hotkey("ctrl+ctrl+s"), Hotkey(MOD_CONTROL, 0x53))

    def test_special_keys(self):
        cases = {
            "ctrl+f1": Hotkey(MOD_CONTROL, 0x70),
            "ctrl+space": Hotkey(MOD_CONTROL, 0x20),
            "ctrl+pagedown": Hotkey(MOD_CONTROL, 0x22),
            "ctrl+pgdn": Hotkey(MOD_CONTROL, 0x22),
            "ctrl+left": Hotkey(MOD_CONTROL, 0x25),
            "ctrl+arrowright": Hotkey(MOD_CONTROL, 0x27),
            "alt+esc": Hotkey(MOD_ALT, 0x1B),
            "alt+enter": Hotkey(MOD_ALT, 0x0D),
            "win+shift+s": Hotkey(MOD_WIN | MOD_SHIFT, 0x53),
            "ctrl+numpad5": Hotkey(MOD_CONTROL, 0x65),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_hotkey(text), expected)

    def test_full_width_plus(self):
        # 從文件或聊天軟體複製過來時常會是全形加號
        self.assertEqual(
            parse_hotkey("ctrl＋alt＋S"), Hotkey(MOD_CONTROL | MOD_ALT, 0x53)
        )

    def test_requires_at_least_one_modifier(self):
        for text in ("s", "s ", "f1", "space", "pagedown", "enter", "escape"):
            with self.subTest(text=text):
                self.assertIsNone(parse_hotkey(text))

    def test_modifier_without_key_is_invalid(self):
        for text in ("ctrl", "ctrl+alt", "ctrl+alt+shift"):
            with self.subTest(text=text):
                self.assertIsNone(parse_hotkey(text))

    def test_two_keys_are_invalid(self):
        for text in ("ctrl+a+b", "ctrl+alt+s+d"):
            with self.subTest(text=text):
                self.assertIsNone(parse_hotkey(text))

    def test_unknown_key_is_invalid(self):
        for text in ("ctrl+nope", "ctrl+f25", "ctrl+滑鼠"):
            with self.subTest(text=text):
                self.assertIsNone(parse_hotkey(text))

    def test_malformed_is_invalid(self):
        for text in ("", " ", "+", "ctrl+", "+s", "ctrl++s", "-", "alt+shift+"):
            with self.subTest(text=text):
                self.assertIsNone(parse_hotkey(text))

    def test_never_raises(self):
        for value in (None, 5, b"ctrl+s", ["ctrl+a"]):
            with self.subTest(value=value):
                self.assertIsNone(parse_hotkey(value))

    def test_norepeat_is_not_inferred(self):
        self.assertFalse(parse_hotkey("ctrl+s").modifiers & MOD_NOREPEAT)


# ------------------------------------------------------------------ 格式化


class FormatHotkeyTests(unittest.TestCase):
    def test_modifier_order_is_fixed(self):
        all_modifiers = MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN
        self.assertEqual(
            format_hotkey(all_modifiers, 0x53), "ctrl+alt+shift+win+s"
        )

    def test_order_does_not_depend_on_bit_order(self):
        expected = "ctrl+alt+shift+win+s"
        for modifiers in (
            MOD_WIN | MOD_SHIFT | MOD_CONTROL | MOD_ALT,
            MOD_ALT | MOD_WIN | MOD_SHIFT | MOD_CONTROL,
            MOD_SHIFT | MOD_ALT | MOD_WIN | MOD_CONTROL,
        ):
            with self.subTest(modifiers=modifiers):
                self.assertEqual(format_hotkey(modifiers, 0x53), expected)

    def test_single_modifiers(self):
        self.assertEqual(format_hotkey(MOD_CONTROL, 0x53), "ctrl+s")
        self.assertEqual(format_hotkey(MOD_ALT, 0x53), "alt+s")
        self.assertEqual(format_hotkey(MOD_SHIFT, 0x53), "shift+s")
        self.assertEqual(format_hotkey(MOD_WIN, 0x53), "win+s")

    def test_norepeat_flag_is_not_printed(self):
        self.assertEqual(format_hotkey(MOD_CONTROL | MOD_NOREPEAT, 0x53), "ctrl+s")

    def test_named_keys_are_pretty_printed(self):
        self.assertEqual(format_hotkey(MOD_CONTROL, 0x70), "ctrl+f1")
        self.assertEqual(format_hotkey(MOD_CONTROL, 0x22), "ctrl+pagedown")
        self.assertEqual(format_hotkey(MOD_ALT, 0x20), "alt+space")

    def test_unknown_vk_falls_back_to_hex(self):
        self.assertEqual(format_hotkey(MOD_CONTROL, 0x00), "ctrl+0x00")
        self.assertEqual(format_hotkey(MOD_CONTROL, 0xFF), "ctrl+0xFF")

    def test_hex_fallback_is_parseable_by_nobody(self):
        # 認不出來的鍵碼再解析回來會失敗，這是刻意的（寧可回報也不要亂猜）
        self.assertIsNone(parse_hotkey(format_hotkey(MOD_CONTROL, 0xFF)))

    def test_text_matches_format_hotkey(self):
        modifiers, vk = MOD_CONTROL | MOD_WIN, 0x41
        hotkey = Hotkey(modifiers, vk)
        self.assertEqual(hotkey.text, format_hotkey(modifiers, vk))
        self.assertEqual(hotkey.text, "ctrl+win+a")

    def test_str_is_text(self):
        hotkey = Hotkey(MOD_ALT, 0x70)
        self.assertEqual(str(hotkey), hotkey.text)
        self.assertEqual(f"{hotkey}", "alt+f1")

    def test_hotkey_is_frozen_and_hashable(self):
        hotkey = Hotkey(MOD_CONTROL, 0x53)
        with self.assertRaises(Exception):
            hotkey.vk = 0x41  # type: ignore[misc]
        self.assertEqual({hotkey}, {Hotkey(MOD_CONTROL, 0x53)})
        self.assertEqual(
            {Hotkey(MOD_CONTROL, 0x53): "x"}[Hotkey(MOD_CONTROL, 0x53)], "x"
        )


# ---------------------------------------------------------------- 來回轉換


class RoundTripTests(unittest.TestCase):
    #: 正規寫法（round-trip 之後必須一字不差）
    CANONICAL = [
        "ctrl+s",
        "ctrl+alt+s",
        "ctrl+alt+shift+win+s",
        "shift+f1",
        "ctrl+space",
        "ctrl+pagedown",
        "alt+left",
        "ctrl+numpad5",
        "win+enter",
        "ctrl+a",
        "alt+0",
        "ctrl+semicolon",
        "ctrl+slash",
        "win+backspace",
    ]

    #: 別名寫法（解析得出來，輸出會被正規化）
    ALIASES = {
        "CTRL + ALT + S": "ctrl+alt+s",
        "alt+ctrl+s": "ctrl+alt+s",
        "win+shift+S": "shift+win+s",
        "control+escape": "ctrl+esc",
        "windows+pgdn": "win+pagedown",
        "ctrl＋alt＋S": "ctrl+alt+s",
        "ctrl+arrowright": "ctrl+right",
        "meta+backspace": "win+backspace",
        " super + space ": "win+space",
    }

    def test_parse_then_format_is_stable(self):
        for text in self.CANONICAL:
            with self.subTest(text=text):
                hotkey = parse_hotkey(text)
                self.assertIsNotNone(hotkey)
                self.assertEqual(hotkey.text, text)
                self.assertEqual(format_hotkey(hotkey.modifiers, hotkey.vk), text)

    def test_format_then_parse_is_stable(self):
        for text in self.CANONICAL:
            with self.subTest(text=text):
                hotkey = parse_hotkey(text)
                again = parse_hotkey(format_hotkey(hotkey.modifiers, hotkey.vk))
                self.assertEqual(again, hotkey)

    def test_aliases_are_normalized(self):
        for text, expected in self.ALIASES.items():
            with self.subTest(text=text):
                hotkey = parse_hotkey(text)
                self.assertIsNotNone(hotkey)
                self.assertEqual(hotkey.text, expected)

    def test_vk_survives_round_trip(self):
        for vk in (0x08, 0x0D, 0x1B, 0x20, 0x21, 0x22, 0x25, 0x30, 0x41,
                   0x5A, 0x70, 0x87, 0xBA, 0xDE):
            with self.subTest(vk=hex(vk)):
                parsed = parse_hotkey(format_hotkey(MOD_CONTROL, vk))
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed, Hotkey(MOD_CONTROL, vk))

    def test_normalized_output_is_always_parseable_again(self):
        for modifiers in (MOD_CONTROL, MOD_ALT, MOD_WIN, MOD_CONTROL | MOD_ALT):
            with self.subTest(modifiers=modifiers):
                text = format_hotkey(modifiers, 0x53)
                self.assertEqual(parse_hotkey(text).text, text)


# ---------------------------------------------------------------- 錯誤訊息


class DescribeErrorTests(unittest.TestCase):
    def test_already_registered(self):
        self.assertIn("已經被其他程式占用", describe_error(1409))
        self.assertIn("1409", describe_error(1409))

    def test_access_denied(self):
        self.assertIn("權限不足", describe_error(5))

    def test_hotkey_not_registered(self):
        self.assertIn("沒有註冊過", describe_error(1419))

    def test_unknown_code_is_still_readable(self):
        message = describe_error(87)
        self.assertIn("失敗", message)
        self.assertIn("87", message)

    def test_hotkey_error_messages(self):
        self.assertEqual(
            HotkeyError.ALREADY_REGISTERED.message(),
            "這個組合鍵已經被其他程式占用",
        )
        self.assertIn("182", HotkeyError.FAILED.message("錯誤碼 182"))
        self.assertEqual(
            HotkeyError.UNSUPPORTED_PLATFORM.message(), hotkeys.UNSUPPORTED_PLATFORM
        )
        self.assertIn("無法解析", HotkeyError.PARSE_ERROR.message())

    def test_error_enum_values_are_unique(self):
        values = [error.value for error in HotkeyError]
        self.assertEqual(len(values), len(set(values)))


# -------------------------------------------------------------- 非 Windows


class NonWindowsTests(unittest.TestCase):
    """非 Windows 必須可以匯入、可以建構，而且 ``apply()`` 不拋例外。"""

    @staticmethod
    def make(on_trigger=None) -> HotkeyManager:
        with mock.patch.object(hotkeys.sys, "platform", "linux"):
            return HotkeyManager(on_trigger or Recorder())

    def patched(self):
        return mock.patch.object(hotkeys.sys, "platform", "linux")

    def test_apply_returns_error_instead_of_raising(self):
        manager = self.make()
        with self.patched():
            result = manager.apply({"stop": "ctrl+alt+s"})
        self.assertIsInstance(result, dict)
        self.assertIn(hotkeys._ALL_KEY, result)
        self.assertIn("Windows", result[hotkeys._ALL_KEY])
        self.assertEqual(manager.active, {})

    def test_error_key_is_a_single_reasonable_name(self):
        manager = self.make()
        with self.patched():
            result = manager.apply({"stop": "ctrl+alt+s", "bad": "nope"})
        # 非 Windows 時直接回報平台不支援，不會去逐項解析
        self.assertEqual(list(result), [hotkeys._ALL_KEY])

    def test_no_user32_calls_at_all(self):
        fake_user32 = FakeUser32()
        manager = self.make()
        manager._user32 = fake_user32
        manager._kernel32 = FakeKernel32()
        manager._msg_type = FakeMessage
        with self.patched():
            manager.apply({"stop": "ctrl+alt+s"})
            manager.clear()
            manager.stop()
        self.assertEqual(fake_user32.registered, {})
        self.assertEqual(fake_user32.unregistered, [])
        self.assertEqual(fake_user32.get_message_calls, 0)
        self.assertEqual(fake_user32.peek_message_calls, 0)
        self.assertEqual(fake_user32.posts, [])

    def test_clear_and_stop_are_safe(self):
        manager = self.make()
        with self.patched():
            manager.clear()
            manager.stop()
            manager.stop()
        self.assertEqual(manager.active, {})
        self.assertEqual(manager.thread_id, 0)

    def test_various_platforms(self):
        manager = self.make()
        for platform in ("linux", "darwin", "freebsd13", "cygwin"):
            with self.subTest(platform=platform):
                with mock.patch.object(hotkeys.sys, "platform", platform):
                    result = manager.apply({"stop": "ctrl+alt+s"})
                self.assertIn(hotkeys._ALL_KEY, result)

    def test_win32_is_accepted(self):
        manager = self.make()
        with mock.patch.object(hotkeys.sys, "platform", "win32"):
            self.assertTrue(manager._windows_available())

    def test_importing_the_module_works_on_any_platform(self):
        # 匯入本身不會碰 windll；這裡確認模組層級的物件都在
        self.assertIsNotNone(hotkeys.HotkeyManager)
        self.assertIsNotNone(hotkeys.parse_hotkey)


# ----------------------------------------------------------- 管理員（Windows）


class HotkeyManagerWindowsTests(unittest.TestCase):
    """用假的 Win32 物件驗證註冊流程，不會碰到真的作業系統。"""

    def test_apply_registers_everything(self):
        manager, user32, _, pump = make_manager(self)

        errors = manager.apply({"stop": "ctrl+alt+s", "reset": "ctrl+f1"})

        self.assertEqual(errors, {})
        self.assertEqual(
            user32.calls,
            [
                (ID_BASE, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x53),
                (ID_BASE + 1, MOD_CONTROL | MOD_NOREPEAT, 0x70),
            ],
        )
        self.assertEqual(
            manager.active,
            {
                "stop": Hotkey(MOD_CONTROL | MOD_ALT, 0x53),
                "reset": Hotkey(MOD_CONTROL, 0x70),
            },
        )
        self.assertEqual(pump.wake_ups, 1)

    def test_hotkey_ids_start_at_base_and_increase(self):
        manager, user32, _, _ = make_manager(self)
        manager.apply({f"key{i}": f"ctrl+f{i + 1}" for i in range(5)})
        ids = [hotkey_id for hotkey_id, _, _ in user32.calls]
        self.assertEqual(ids, [ID_BASE + index for index in range(5)])

    def test_active_uses_normalized_hotkeys(self):
        manager, _, _, _ = make_manager(self)
        manager.apply({"stop": "CTRL + ALT + S"})
        self.assertEqual(manager.active["stop"].text, "ctrl+alt+s")
        self.assertEqual(manager.active["stop"], Hotkey(MOD_CONTROL | MOD_ALT, 0x53))

    def test_apply_reports_unparseable_strings(self):
        manager, user32, _, _ = make_manager(self)

        errors = manager.apply({"good": "ctrl+a", "bad": "s", "worse": "not-a-key"})

        self.assertEqual(set(errors), {"bad", "worse"})
        self.assertIn("無法解析", errors["bad"])
        self.assertIn("無法解析", errors["worse"])
        self.assertIn("'s'", errors["bad"])
        # 解析成功的那一項還是要註冊
        self.assertEqual(user32.calls, [(ID_BASE, MOD_CONTROL | MOD_NOREPEAT, 0x41)])
        self.assertEqual(manager.active, {"good": Hotkey(MOD_CONTROL, 0x41)})

    def test_apply_all_invalid_skips_the_os(self):
        manager, user32, _, _ = make_manager(self)
        errors = manager.apply({"bad": "s", "worse": ""})
        self.assertEqual(set(errors), {"bad", "worse"})
        self.assertEqual(user32.calls, [])
        self.assertEqual(manager.active, {})

    def test_empty_bindings_clears_everything(self):
        manager, user32, _, _ = make_manager(self)
        manager.apply({"stop": "ctrl+alt+s"})
        errors = manager.apply({})
        self.assertEqual(errors, {})
        self.assertEqual(manager.active, {})
        self.assertIn(ID_BASE, user32.unregistered)

    def test_duplicate_hotkey_is_reported(self):
        manager, _, _, _ = make_manager(self, user32=FakeUser32(fail_on=0x53))

        errors = manager.apply({"stop": "ctrl+alt+s", "reset": "ctrl+f1"})

        self.assertEqual(set(errors), {"stop"})
        self.assertIn("已經被其他程式占用", errors["stop"])
        self.assertEqual(set(manager.active), {"reset"})

    def test_access_denied_is_reported(self):
        manager, _, _, _ = make_manager(
            self, user32=FakeUser32(fail_on=0x53, error_code=5)
        )
        errors = manager.apply({"stop": "ctrl+alt+s"})
        self.assertIn("權限不足", errors["stop"])

    def test_unknown_error_code_is_reported(self):
        manager, _, _, _ = make_manager(
            self, user32=FakeUser32(fail_on=0x53, error_code=87)
        )
        errors = manager.apply({"stop": "ctrl+alt+s"})
        self.assertIn("87", errors["stop"])
        self.assertIn("失敗", errors["stop"])

    def test_zero_return_without_error_code(self):
        user32 = FakeUser32(fail_on=0x53, error_code=0)
        manager, _, _, _ = make_manager(self, user32=user32)
        errors = manager.apply({"stop": "ctrl+alt+s"})
        self.assertIn("0", errors["stop"])

    def test_error_code_comes_from_kernel32(self):
        user32 = FakeUser32(fail_on=0x53)
        kernel32 = FakeKernel32(error=ERROR_HOTKEY_ALREADY_REGISTERED)
        manager, _, _, _ = make_manager(self, user32=user32, kernel32=kernel32)
        errors = manager.apply({"stop": "ctrl+alt+s"})
        self.assertIn("已經被其他程式占用", errors["stop"])

    def test_reapply_unregisters_first(self):
        manager, user32, _, _ = make_manager(self)

        manager.apply({"first": "ctrl+a"})
        manager.apply({"second": "ctrl+b"})

        self.assertIn(ID_BASE, user32.unregistered)
        self.assertEqual(user32.calls, [(ID_BASE, MOD_CONTROL | MOD_NOREPEAT, 0x42)])
        self.assertEqual(set(manager.active), {"second"})

    def test_clear_unregisters_and_empties_active(self):
        manager, user32, _, _ = make_manager(self)
        manager.apply({"stop": "ctrl+alt+s"})

        manager.clear()

        self.assertIn(ID_BASE, user32.unregistered)
        self.assertEqual(user32.registered, {})
        self.assertEqual(manager.active, {})

    def test_clear_when_nothing_registered(self):
        manager, user32, _, _ = make_manager(self)
        manager.clear()
        self.assertEqual(user32.unregistered, [])
        self.assertEqual(manager.active, {})

    def test_clear_ignores_unregister_failure(self):
        user32 = FakeUser32()
        user32.unregister_error = 1419
        manager, _, _, _ = make_manager(self, user32=user32)
        manager.apply({"stop": "ctrl+alt+s"})

        with self.assertLogs("hotkeys", level="DEBUG"):
            manager.clear()  # 不該拋例外

        self.assertEqual(manager.active, {})

    def test_stop_posts_wm_quit_and_joins(self):
        manager, _, _, pump = make_manager(self)
        manager.apply({"stop": "ctrl+alt+s"})

        manager.stop()

        self.assertEqual(pump.quits, 1)
        self.assertEqual(pump.posts, [(THREAD_ID, WM_APP), (THREAD_ID, WM_QUIT)])
        self.assertEqual(manager.active, {})
        self.assertEqual(manager.thread_id, 0)
        self.assertFalse(manager._thread.is_alive())

    def test_stop_is_idempotent(self):
        manager, _, _, pump = make_manager(self)
        manager.stop()
        manager.stop()
        self.assertEqual(pump.quits, 1)

    def test_stop_on_a_manager_that_never_started(self):
        manager, _, _, pump = make_manager(self, start_thread=False)
        manager.stop()
        self.assertEqual(pump.posts, [])
        self.assertEqual(manager.active, {})

    def test_apply_after_stop_starts_a_new_thread(self):
        manager, _, _, _ = make_manager(self)
        manager.apply({"stop": "ctrl+alt+s"})
        manager.stop()

        errors = manager.apply({"stop": "ctrl+a"})

        self.assertEqual(errors, {})
        self.assertEqual(manager.active["stop"], Hotkey(MOD_CONTROL, 0x41))

    def test_timeout_returns_error_instead_of_hanging(self):
        manager, _, _, _ = make_manager(self)
        manager._await_result = mock.Mock(side_effect=queue.Empty)

        errors = manager.apply({"stop": "ctrl+alt+s"})

        self.assertIn(hotkeys._ALL_KEY, errors)
        self.assertEqual(errors[hotkeys._ALL_KEY], hotkeys.WORKER_TIMEOUT_MESSAGE)

    def test_timeout_keeps_parse_errors(self):
        manager, _, _, _ = make_manager(self)
        manager._await_result = mock.Mock(side_effect=queue.Empty)

        errors = manager.apply({"bad": "s", "stop": "ctrl+alt+s"})

        self.assertIn("bad", errors)
        self.assertIn(hotkeys._ALL_KEY, errors)

    def test_stale_results_are_drained_before_waiting(self):
        manager, _, _, _ = make_manager(self)
        manager._results.put(({0: "舊的"}, {}))  # 上一輪逾時留下的殘骸
        manager._await_result = mock.Mock(side_effect=queue.Empty)

        errors = manager.apply({"stop": "ctrl+alt+s"})

        self.assertIn(hotkeys._ALL_KEY, errors)

    def test_wake_up_failure_is_reported(self):
        user32 = FakeUser32()
        kernel32 = FakeKernel32()
        manager, _, _, _ = make_manager(self, user32=user32, kernel32=kernel32)
        user32.PostThreadMessageW = mock.Mock(return_value=0)  # type: ignore[attr-defined]
        kernel32.error = 6

        errors = manager.apply({"stop": "ctrl+alt+s"})

        self.assertIn(hotkeys._ALL_KEY, errors)
        self.assertIn("6", errors[hotkeys._ALL_KEY])

    def test_error_code_falls_back_to_ctypes(self):
        kernel32 = FakeKernel32(error=8)  # ERROR_NOT_ENOUGH_MEMORY
        manager, _, _, _ = make_manager(self, kernel32=kernel32)
        self.assertEqual(manager._last_error(), 8)

        manager._kernel32 = None
        with mock.patch.object(hotkeys.ctypes, "get_last_error", return_value=14):
            self.assertEqual(manager._last_error(), 14)

    def test_missing_thread_id_is_reported(self):
        manager, _, _, _ = make_manager(self)
        manager._thread_id = 0

        errors = manager.apply({"stop": "ctrl+alt+s"})

        self.assertIn(hotkeys._ALL_KEY, errors)

    def test_missing_win32_libraries_are_reported(self):
        manager, _, _, _ = make_manager(self)
        with mock.patch.multiple(
            manager, _user32=None, _kernel32=None, _msg_type=None
        ):
            errors = manager.apply({"stop": "ctrl+alt+s"})
            # 也不能因此拋例外
            manager.clear()
        self.assertIn(hotkeys._ALL_KEY, errors)
        self.assertIn("Windows", errors[hotkeys._ALL_KEY])

    def test_start_failure_is_reported(self):
        manager, user32, _, pump = make_manager(self, start_thread=False)
        with mock.patch.object(manager, "_ensure_worker", return_value=False):
            errors = manager.apply({"stop": "ctrl+alt+s"})
        self.assertEqual(errors, {hotkeys._ALL_KEY: hotkeys.WORKER_UNAVAILABLE_MESSAGE})
        self.assertEqual(user32.calls, [])
        self.assertEqual(pump.posts, [])

    def test_context_manager_stops(self):
        manager, _, _, pump = make_manager(self)
        with manager:
            manager.apply({"stop": "ctrl+alt+s"})
        self.assertEqual(pump.quits, 1)

    def test_active_returns_a_copy(self):
        manager, _, _, _ = make_manager(self)
        manager.apply({"stop": "ctrl+alt+s"})
        snapshot = manager.active
        snapshot["stop"] = Hotkey(MOD_CONTROL, 0x41)
        self.assertEqual(manager.active["stop"], Hotkey(MOD_CONTROL | MOD_ALT, 0x53))


class TriggerDispatchTests(unittest.TestCase):
    """驗證 ``WM_HOTKEY`` 怎麼被換成回呼（不涉及真實訊息迴圈）。"""

    def test_wm_hotkey_calls_trigger_with_name(self):
        recorder = Recorder()
        manager, _, _, _ = make_manager(self, on_trigger=recorder)
        manager.apply({"stop": "ctrl+alt+s", "reset": "ctrl+f1"})

        manager._fire(ID_BASE)
        manager._fire(ID_BASE + 1)

        self.assertEqual(recorder.names, ["stop", "reset"])

    def test_only_active_ids_fire(self):
        recorder = Recorder()
        manager, _, _, _ = make_manager(
            self, on_trigger=recorder, user32=FakeUser32(fail_on=0x53)
        )
        manager.apply({"stop": "ctrl+alt+s", "reset": "ctrl+f1"})

        manager._fire(ID_BASE)  # 註冊失敗的那一個
        manager._fire(ID_BASE + 1)

        self.assertEqual(recorder.names, ["reset"])

    def test_unknown_id_is_ignored(self):
        recorder = Recorder()
        manager, _, _, _ = make_manager(self, on_trigger=recorder)
        manager.apply({"stop": "ctrl+alt+s"})

        manager._fire(0x7FFF)

        self.assertEqual(recorder.names, [])

    def test_trigger_exception_does_not_propagate(self):
        recorder = Recorder(raises=True)
        manager, _, _, _ = make_manager(self, on_trigger=recorder)
        manager.apply({"stop": "ctrl+alt+s"})

        with self.assertLogs("hotkeys", level="ERROR"):
            manager._fire(ID_BASE)  # 不該往外丟

        self.assertEqual(recorder.names, ["stop"])

    def test_trigger_can_call_stop_without_deadlock(self):
        # 使用者在回呼裡按了「緊急停止」而呼叫 stop()，不能把自己 join 死
        manager, _, _, _ = make_manager(self)
        manager.apply({"stop": "ctrl+alt+s"})
        manager._on_trigger = lambda name: manager.stop()

        done = threading.Event()

        def run() -> None:
            manager._fire(ID_BASE)
            done.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(done.wait(timeout=5.0), "在回呼裡呼叫 stop() 死鎖了")

    def test_worker_loop_processes_tasks(self):
        # 真的讓背景執行緒跑一圈假的訊息迴圈再收工
        manager, user32, _, _ = make_manager(self)
        manager.apply({"stop": "ctrl+alt+s"})

        deadline = time.monotonic() + 2.0
        while user32.get_message_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.005)

        manager.stop()
        self.assertGreaterEqual(user32.get_message_calls, 1)

    def test_worker_clears_state_when_it_ends(self):
        manager, _, _, _ = make_manager(self)
        manager.apply({"stop": "ctrl+alt+s"})
        manager.stop()
        self.assertEqual(manager.active, {})


class ConstantsTests(unittest.TestCase):
    def test_win32_constants(self):
        self.assertEqual(WM_HOTKEY, 0x0312)
        self.assertEqual(WM_APP, 0x8000)
        self.assertEqual(WM_QUIT, 0x0012)
        self.assertEqual(MOD_ALT, 0x0001)
        self.assertEqual(MOD_CONTROL, 0x0002)
        self.assertEqual(MOD_SHIFT, 0x0004)
        self.assertEqual(MOD_WIN, 0x0008)
        self.assertEqual(MOD_NOREPEAT, 0x4000)
        self.assertEqual(ERROR_HOTKEY_ALREADY_REGISTERED, 1409)

    def test_id_base_avoids_low_range(self):
        self.assertEqual(hotkeys._HOTKEY_ID_BASE, ID_BASE)
        self.assertGreaterEqual(ID_BASE, 0xB000)
        self.assertLess(ID_BASE, 0x10000)

    def test_public_api_is_exported(self):
        for name in (
            "MOD_ALT",
            "MOD_CONTROL",
            "MOD_SHIFT",
            "MOD_WIN",
            "MOD_NOREPEAT",
            "Hotkey",
            "HotkeyManager",
            "HotkeyError",
            "vk_for_name",
            "name_for_vk",
            "parse_hotkey",
            "format_hotkey",
            "describe_error",
            "ERROR_HOTKEY_ALREADY_REGISTERED",
            "WM_HOTKEY",
            "WM_APP",
            "WM_QUIT",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(hotkeys, name))
                self.assertIn(name, hotkeys.__all__)

    def test_default_timeout_is_reasonable(self):
        self.assertGreater(DEFAULT_TIMEOUT, 0)
        self.assertLessEqual(DEFAULT_TIMEOUT, 10)

    def test_logger_name_comes_from_module(self):
        self.assertEqual(hotkeys.logger.name, "hotkeys")


if __name__ == "__main__":
    unittest.main()
