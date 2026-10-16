"""update_check 模組的測試。

**完全離線**：所有會碰到網路的路徑都注入假的 ``opener``，測試本身不會連外網。
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
from typing import Any

from update_check import (
    RELEASES_PAGE,
    RELEASES_URL,
    ReleaseInfo,
    Status,
    UpdateCheckResult,
    check_for_update,
    check_in_background,
    is_newer,
    parse_version,
)

#: 一般測試用的「最新發佈」JSON。
SAMPLE_LATEST = {
    "tag_name": "v3.2.0",
    "html_url": "https://github.com/talkouki89/DGLAB-CS2/releases/tag/v3.2.0",
    "name": "DGLAB-CS2 3.2.0",
    "body": "## 更新內容\n\n- 新增自動檢查更新",
    "published_at": "2026-02-01T12:34:56Z",
}


class FakeResponse:
    """假的 HTTP 回應，只要支援 ``read`` 與 ``close`` 就夠了。"""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """假的 ``urlopen``：記錄請求、回傳準備好的內容或拋出準備好的例外。"""

    def __init__(
        self,
        *,
        payload: Any = None,
        body: bytes | None = None,
        error: Exception | None = None,
    ) -> None:
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        self.body = body if body is not None else b""
        self.error = error
        self.requests: list[Any] = []
        self.timeouts: list[float] = []
        self.responses: list[FakeResponse] = []

    def __call__(self, request: Any, *, timeout: float = 10.0) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        response = FakeResponse(self.body)
        self.responses.append(response)
        return response

    @property
    def request(self) -> Any:
        return self.requests[-1]

    def header(self, name: str, index: int = -1) -> str:
        """取出送出的標頭（不分大小寫，``Request`` 會改寫標頭名稱的格式）。"""
        headers = self.requests[index].headers
        wanted = name.lower()
        for key, value in headers.items():
            if key.lower() == wanted:
                return str(value)
        return ""


def http_error(code: int, reason: str = "假的錯誤") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        RELEASES_URL, code, reason, hdrs=None, fp=None  # type: ignore[arg-type]
    )


class ParseVersionTests(unittest.TestCase):
    def test_common_forms(self):
        for text, expected in (
            ("v3.1.0", (3, 1, 0)),
            ("3.1.0", (3, 1, 0)),
            ("3.1", (3, 1)),
            ("v1.2.3.4", (1, 2, 3, 4)),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_version(text), expected)

    def test_prerelease_suffix_is_ignored(self):
        # -beta.1 這種字尾先不管，只要抓得到前面的版號就好
        self.assertEqual(parse_version("3.1.0-beta.1"), (3, 1, 0))
        self.assertEqual(parse_version("v3.1.0-beta.1"), (3, 1, 0))

    def test_unparseable_returns_empty_tuple(self):
        for text in ("", "   ", "abc", "v", "版本三"):
            with self.subTest(text=text):
                self.assertEqual(parse_version(text), ())

    def test_non_string_is_tolerated(self):
        self.assertEqual(parse_version(None), ())  # type: ignore[arg-type]
        self.assertEqual(parse_version(3.1), ())  # type: ignore[arg-type]


class IsNewerTests(unittest.TestCase):
    def test_minor_compares_numerically_not_as_text(self):
        # 這是重點：字串比的話 "3.10.0" < "3.9.0" 會錯
        self.assertTrue(is_newer("3.10.0", "3.9.0"))
        self.assertTrue(is_newer("v3.10.0", "3.9.0"))
        self.assertFalse(is_newer("3.9.0", "3.10.0"))

    def test_missing_parts_count_as_zero(self):
        self.assertFalse(is_newer("3.1", "3.1.0"))
        self.assertFalse(is_newer("3.1.0", "3.1"))
        self.assertTrue(is_newer("3.1.1", "3.1"))

    def test_patch_and_major(self):
        self.assertTrue(is_newer("3.1.1", "3.1.0"))
        self.assertTrue(is_newer("4.0.0", "3.99.99"))
        self.assertFalse(is_newer("3.1.0", "3.1.0"))

    def test_unparseable_is_never_newer(self):
        self.assertFalse(is_newer("abc", "3.1.0"))
        self.assertFalse(is_newer("3.2.0", ""))
        self.assertFalse(is_newer("", ""))


class CheckForUpdateTests(unittest.TestCase):
    def test_update_available(self):
        opener = FakeOpener(payload=SAMPLE_LATEST)
        result = check_for_update("3.1.0", opener=opener)

        self.assertEqual(result.status, Status.UPDATE_AVAILABLE)
        self.assertTrue(result.available)
        self.assertEqual(result.current, "3.1.0")
        self.assertEqual(result.error, "")
        self.assertIsInstance(result.latest, ReleaseInfo)

        latest = result.latest
        assert latest is not None
        self.assertEqual(latest.tag, "v3.2.0")
        self.assertEqual(latest.version, "3.2.0")
        self.assertEqual(latest.url, SAMPLE_LATEST["html_url"])
        self.assertEqual(latest.name, SAMPLE_LATEST["name"])
        self.assertEqual(latest.notes, SAMPLE_LATEST["body"])
        self.assertEqual(latest.published_at, SAMPLE_LATEST["published_at"])

    def test_same_version_is_up_to_date(self):
        opener = FakeOpener(payload={**SAMPLE_LATEST, "tag_name": "v3.1.0"})
        result = check_for_update("3.1.0", opener=opener)
        self.assertEqual(result.status, Status.UP_TO_DATE)
        self.assertFalse(result.available)
        self.assertIsNotNone(result.latest)

    def test_older_release_is_up_to_date(self):
        opener = FakeOpener(payload={**SAMPLE_LATEST, "tag_name": "v3.0.9"})
        result = check_for_update("3.1.0", opener=opener)
        self.assertEqual(result.status, Status.UP_TO_DATE)
        self.assertFalse(result.available)

    def test_status_compares_as_plain_string(self):
        opener = FakeOpener(payload=SAMPLE_LATEST)
        result = check_for_update("3.1.0", opener=opener)
        self.assertEqual(result.status, "update_available")

    def test_request_carries_user_agent_and_url(self):
        opener = FakeOpener(payload=SAMPLE_LATEST)
        check_for_update("3.1.0", opener=opener)

        self.assertEqual(opener.request.full_url, RELEASES_URL)
        self.assertEqual(opener.header("User-Agent"), "DGLAB-CS2/3.1.0")
        self.assertTrue(opener.header("Accept"))
        self.assertEqual(opener.timeouts, [10.0])

    def test_timeout_is_forwarded(self):
        opener = FakeOpener(payload=SAMPLE_LATEST)
        check_for_update("3.1.0", timeout=2.5, opener=opener)
        self.assertEqual(opener.timeouts, [2.5])

    def test_custom_url_is_used(self):
        opener = FakeOpener(payload=SAMPLE_LATEST)
        url = "https://example.invalid/releases/latest"
        check_for_update("3.1.0", url=url, opener=opener)
        self.assertEqual(opener.request.full_url, url)

    def test_response_is_closed(self):
        opener = FakeOpener(payload=SAMPLE_LATEST)
        check_for_update("3.1.0", opener=opener)
        self.assertTrue(opener.responses[0].closed)

    def test_long_notes_are_not_truncated(self):
        notes = "很長的說明\n" * 2000
        opener = FakeOpener(payload={**SAMPLE_LATEST, "body": notes})
        result = check_for_update("3.1.0", opener=opener)
        assert result.latest is not None
        self.assertEqual(result.latest.notes, notes.strip())
        self.assertGreater(len(result.latest.notes), 10000)

    def test_missing_or_null_fields_become_empty_strings(self):
        opener = FakeOpener(
            payload={
                "tag_name": "v3.2.0",
                "html_url": None,
                "name": None,
                "body": None,
                "published_at": None,
            }
        )
        result = check_for_update("3.1.0", opener=opener)
        self.assertEqual(result.status, Status.UPDATE_AVAILABLE)
        assert result.latest is not None
        self.assertEqual(result.latest.url, "")
        self.assertEqual(result.latest.name, "")
        self.assertEqual(result.latest.notes, "")
        self.assertEqual(result.latest.published_at, "")

    def test_version_keeps_four_parts(self):
        opener = FakeOpener(payload={**SAMPLE_LATEST, "tag_name": "v3.1.0.1"})
        result = check_for_update("3.1.0", opener=opener)
        self.assertEqual(result.status, Status.UPDATE_AVAILABLE)
        assert result.latest is not None
        self.assertEqual(result.latest.version, "3.1.0.1")


class CheckForUpdateErrorTests(unittest.TestCase):
    """各種失敗都只能回傳 error 結果，不能拋例外。"""

    def assert_error(self, result: UpdateCheckResult, *, contains: str = "") -> None:
        self.assertEqual(result.status, Status.ERROR)
        self.assertFalse(result.available)
        self.assertIsNone(result.latest)
        self.assertTrue(result.error)
        if contains:
            self.assertIn(contains, result.error)

    def test_http_404(self):
        opener = FakeOpener(error=http_error(404, "Not Found"))
        self.assert_error(check_for_update("3.1.0", opener=opener), contains="404")

    def test_http_403(self):
        opener = FakeOpener(error=http_error(403, "Forbidden"))
        self.assert_error(check_for_update("3.1.0", opener=opener), contains="403")

    def test_url_error(self):
        opener = FakeOpener(error=urllib.error.URLError("名稱解析失敗"))
        self.assert_error(check_for_update("3.1.0", opener=opener), contains="連不上")

    def test_timeout(self):
        opener = FakeOpener(error=TimeoutError("timed out"))
        self.assert_error(check_for_update("3.1.0", opener=opener))

    def test_broken_json(self):
        opener = FakeOpener(body="<html>不是 JSON</html>".encode("utf-8"))
        self.assert_error(
            check_for_update("3.1.0", opener=opener), contains="不是合法 JSON"
        )

    def test_response_is_not_a_dict(self):
        opener = FakeOpener(payload=["這不是物件"])
        self.assert_error(check_for_update("3.1.0", opener=opener), contains="格式")

    def test_response_is_null(self):
        opener = FakeOpener(body=b"null")
        self.assert_error(check_for_update("3.1.0", opener=opener))

    def test_unparseable_tag(self):
        opener = FakeOpener(payload={**SAMPLE_LATEST, "tag_name": "latest"})
        self.assert_error(
            check_for_update("3.1.0", opener=opener), contains="無法解析最新版本的版號"
        )

    def test_missing_tag(self):
        payload = {
            key: value for key, value in SAMPLE_LATEST.items() if key != "tag_name"
        }
        opener = FakeOpener(payload=payload)
        self.assert_error(check_for_update("3.1.0", opener=opener))

    def test_current_version_garbage_is_error_not_crash(self):
        # 目前版本解析不出來時，is_newer 會回 False，所以是「已最新」而不是爆炸
        opener = FakeOpener(payload=SAMPLE_LATEST)
        result = check_for_update("", opener=opener)
        self.assertEqual(result.status, Status.UP_TO_DATE)

    def test_unexpected_opener_exception_is_still_an_error_result(self):
        def rude_opener(request: Any, *, timeout: float = 10.0) -> Any:
            raise KeyError("莫名其妙的錯誤")

        self.assert_error(check_for_update("3.1.0", opener=rude_opener))

    def test_custom_logger_receives_the_warning(self):
        records: list[str] = []

        class Collector:
            def warning(self, message: str, *args: Any) -> None:
                records.append(message % args)

            def debug(self, message: str, *args: Any) -> None:
                pass

        opener = FakeOpener(error=http_error(500))
        collector = Collector()
        result = check_for_update(
            "3.1.0", opener=opener, logger=collector  # type: ignore[arg-type]
        )
        self.assertEqual(result.status, Status.ERROR)
        self.assertEqual(len(records), 1)
        self.assertIn("檢查更新失敗", records[0])


class CheckInBackgroundTests(unittest.TestCase):
    def test_callback_receives_the_result_and_thread_can_join(self):
        opener = FakeOpener(payload=SAMPLE_LATEST)
        done = threading.Event()
        seen: list[UpdateCheckResult] = []

        def callback(result: UpdateCheckResult) -> None:
            seen.append(result)
            done.set()

        thread = check_in_background("3.1.0", callback, opener=opener)
        self.assertIsInstance(thread, threading.Thread)

        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(done.wait(timeout=1.0))

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].status, Status.UPDATE_AVAILABLE)
        self.assertEqual(seen[0].current, "3.1.0")

    def test_callback_still_runs_on_error(self):
        opener = FakeOpener(error=http_error(404))
        done = threading.Event()
        seen: list[UpdateCheckResult] = []

        def callback(result: UpdateCheckResult) -> None:
            seen.append(result)
            done.set()

        thread = check_in_background("3.1.0", callback, opener=opener)
        thread.join(timeout=10.0)
        self.assertTrue(done.wait(timeout=1.0))
        self.assertEqual(seen[0].status, Status.ERROR)

    def test_callback_exception_does_not_kill_the_process(self):
        opener = FakeOpener(payload=SAMPLE_LATEST)

        def boom(result: UpdateCheckResult) -> None:
            raise RuntimeError("使用者端壞掉了")

        thread = check_in_background("3.1.0", boom, opener=opener)
        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive())

    def test_opener_failure_is_reported_as_error(self):
        def broken_opener(request: Any, *, timeout: float = 10.0) -> Any:
            raise RuntimeError("opener 自己壞了")

        done = threading.Event()
        seen: list[UpdateCheckResult] = []

        def callback(result: UpdateCheckResult) -> None:
            seen.append(result)
            done.set()

        thread = check_in_background("3.1.0", callback, opener=broken_opener)
        thread.join(timeout=10.0)
        self.assertTrue(done.wait(timeout=1.0))
        self.assertEqual(seen[0].status, Status.ERROR)


class ModuleTests(unittest.TestCase):
    def test_logger_name_comes_from_the_module(self):
        import update_check

        self.assertEqual(update_check.logger.name, "update_check")

    def test_defaults_point_at_the_project_repository(self):
        self.assertIn("talkouki89/DGLAB-CS2", RELEASES_URL)
        self.assertIn("talkouki89/DGLAB-CS2", RELEASES_PAGE)


if __name__ == "__main__":
    unittest.main()
