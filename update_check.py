"""檢查 GitHub Releases 上有沒有新版本。

只在程式啟動時做一次「有沒有新版」的查詢，不負責下載或安裝：

1. 用標準函式庫的 ``urllib`` 問 GitHub 的 ``releases/latest`` API。
2. 把回應整理成 :class:`ReleaseInfo`（版號、標題、說明、發佈時間、下載頁網址）。
3. 和目前版本比大小，回傳 :class:`UpdateCheckResult`。

設計上刻意**不拋例外**：這支程式會在背景執行緒被呼叫，沒有網路、逾時、DNS
失敗、GitHub 回 4xx/5xx、JSON 壞掉等等都只會變成 ``status="error"``，
呼叫端（GUI）只需要看結果，不必自己包 try/except。

典型用法::

    from update_check import check_in_background

    def on_result(result):
        if result.available:
            ...  # 提示使用者有 result.latest.version

    check_in_background("3.1.0", on_result)
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: GitHub 的「最新發佈」API。
RELEASES_URL = "https://api.github.com/repos/talkouki89/DGLAB-CS2/releases/latest"

#: 給使用者看的發佈頁（查不到時也可以直接開這個）。
RELEASES_PAGE = "https://github.com/talkouki89/DGLAB-CS2/releases/latest"

#: 一定要帶 User-Agent，否則 GitHub API 會直接回 403。
USER_AGENT_TEMPLATE = "DGLAB-CS2/{version}"

#: 只用來抓出「開頭的數字與點」，後面的 -beta.1、+build 之類都先不管。
_VERSION_PREFIX = re.compile(r"\d+(?:\.\d+)*")


class Status(str, Enum):
    """檢查結果的三種狀態。

    繼承 ``str``，所以 ``result.status == "up_to_date"`` 這種寫法也成立。
    """

    UPDATE_AVAILABLE = "update_available"
    UP_TO_DATE = "up_to_date"
    ERROR = "error"


@dataclass(frozen=True)
class ReleaseInfo:
    """GitHub 上最新一筆發佈的資訊。"""

    #: 原始標籤，例如 ``"v3.2.0"``
    tag: str
    #: 去掉開頭 ``v`` 的版號，例如 ``"3.2.0"``
    version: str
    #: 發佈頁網址（``html_url``）
    url: str
    #: 發佈標題
    name: str
    #: 發佈說明（markdown），可能很長或整個是空的
    notes: str
    #: 發佈時間（``published_at``）
    published_at: str


@dataclass(frozen=True)
class UpdateCheckResult:
    """一次版本檢查的結果。"""

    #: :class:`Status` 的其中一個值
    status: Status
    #: 目前版本（呼叫端傳進來的）
    current: str
    #: 查不到最新發佈時是 ``None``
    latest: ReleaseInfo | None
    #: ``status == "error"`` 時的原因說明
    error: str = ""

    @property
    def available(self) -> bool:
        """是否有比目前版本更新的版本。"""
        return self.status == Status.UPDATE_AVAILABLE


def parse_version(text: str) -> tuple[int, ...]:
    """把版號字串轉成可以比大小的數字 tuple。

    可以處理 ``"v3.1.0"``、``"3.1.0"``、``"3.1"``、``"v1.2.3.4"`` 與
    ``"3.1.0-beta.1"``。無法解析時回傳空 tuple（**不拋例外**）。
    """
    if not isinstance(text, str):
        return ()
    match = _VERSION_PREFIX.search(text.strip())
    if match is None:
        return ()
    try:
        return tuple(int(part) for part in match.group(0).split("."))
    except ValueError:  # pragma: no cover - 規則上不可能發生
        return ()


def is_newer(candidate: str, current: str) -> bool:
    """``candidate`` 是否比 ``current`` 新。

    兩邊都會補 0 到一樣長，所以 ``(3, 1)`` 和 ``(3, 1, 0)`` 視為相同版本；
    任何一邊解析不出來就回傳 ``False``。
    """
    new = parse_version(candidate)
    old = parse_version(current)
    if not new or not old:
        return False
    length = max(len(new), len(old))
    padded_new = new + (0,) * (length - len(new))
    padded_old = old + (0,) * (length - len(old))
    return padded_new > padded_old


def _as_text(value: Any) -> str:
    """把 JSON 欄位轉成字串；``None`` 或非字串都變成空字串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _parse_release(raw: Any) -> ReleaseInfo | None:
    """把 API 回應的 JSON 物件轉成 :class:`ReleaseInfo`。

    不是 dict、沒有 ``tag_name``、或標籤解析不出版號時回傳 ``None``。
    """
    if not isinstance(raw, dict):
        return None
    tag = _as_text(raw.get("tag_name"))
    if not tag:
        return None
    version = parse_version(tag)
    if not version:
        return None
    return ReleaseInfo(
        tag=tag,
        version=".".join(str(part) for part in version),
        url=_as_text(raw.get("html_url")),
        name=_as_text(raw.get("name")),
        # 說明可能幾千字，這裡刻意不截斷，交給呼叫端決定怎麼顯示
        notes=_as_text(raw.get("body")),
        published_at=_as_text(raw.get("published_at")),
    )


def _error(current: str, message: str, log: logging.Logger) -> UpdateCheckResult:
    """組出錯誤結果，並留下一行警告。"""
    log.warning("檢查更新失敗：%s", message)
    return UpdateCheckResult(
        status=Status.ERROR,
        current=current,
        latest=None,
        error=message,
    )


def check_for_update(
    current_version: str,
    *,
    url: str = RELEASES_URL,
    timeout: float = 10.0,
    opener: Callable[..., Any] | None = None,
    logger: logging.Logger | None = None,
) -> UpdateCheckResult:
    """問 GitHub 有沒有比 ``current_version`` 更新的版本。

    Args:
        current_version: 目前版本，例如 ``"3.1.0"``。
        url: 要查的 API 網址（測試或改用別的 repo 時可以換掉）。
        timeout: 連線與讀取的逾時秒數。
        opener: 測試用；預設是 ``urllib.request.urlopen``，可以用假的替換掉。
        logger: 測試用；預設用本模組的 logger。

    這個函式**絕對不拋例外**，任何失敗都回傳 ``status="error"`` 的結果。
    """
    # 參數名稱和模組層的 logger 同名，所以要用 globals() 才拿得到後者
    log = logger if logger is not None else globals()["logger"]
    current = _as_text(current_version)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT_TEMPLATE.format(version=current or "unknown"),
            "Accept": "application/vnd.github+json",
        },
    )
    fetch = opener or urllib.request.urlopen

    try:
        response = fetch(request, timeout=timeout)
        try:
            payload = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except urllib.error.HTTPError as exc:
        return _error(current, f"GitHub 回覆錯誤（HTTP {exc.code}）", log)
    except urllib.error.URLError as exc:
        return _error(current, f"連不上 GitHub（{exc.reason}）", log)
    except (OSError, ValueError) as exc:
        return _error(current, f"讀取發佈資訊失敗：{exc}", log)
    except Exception as exc:  # noqa: BLE001 - 背景執行緒，絕不能把例外丟出去
        return _error(current, f"讀取發佈資訊失敗：{exc!r}", log)

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return _error(current, f"GitHub 回覆的不是合法 JSON：{exc}", log)
    except Exception as exc:  # noqa: BLE001 - 同上，保險用
        return _error(current, f"解析 GitHub 回覆失敗：{exc!r}", log)

    if not isinstance(data, dict):
        return _error(current, "GitHub 回覆的格式不是預期的物件", log)

    release = _parse_release(data)
    if release is None:
        return _error(current, "無法解析最新版本的版號", log)

    status = (
        Status.UPDATE_AVAILABLE
        if is_newer(release.version, current)
        else Status.UP_TO_DATE
    )
    log.debug("檢查更新完成：目前 %s、最新 %s（%s）", current, release.version, status)
    return UpdateCheckResult(status=status, current=current, latest=release)


def check_in_background(
    current_version: str,
    callback: Callable[[UpdateCheckResult], None],
    *,
    url: str = RELEASES_URL,
    timeout: float = 10.0,
    opener: Callable[..., Any] | None = None,
) -> threading.Thread:
    """在背景執行緒檢查，完成後呼叫 callback（在背景執行緒）。

    回傳那個 Thread 物件，方便測試 join。
    """

    def worker() -> None:
        try:
            result = check_for_update(
                current_version,
                url=url,
                timeout=timeout,
                opener=opener,
            )
        except Exception:  # noqa: BLE001 - 連 callback 都不該被例外打斷
            logger.exception("背景檢查更新時發生非預期的錯誤")
            result = UpdateCheckResult(
                status=Status.ERROR,
                current=current_version,
                latest=None,
                error="背景檢查更新時發生非預期的錯誤",
            )
        try:
            callback(result)
        except Exception:  # noqa: BLE001 - callback 的錯不該弄掛執行緒
            logger.exception("處理更新檢查結果時發生錯誤")

    thread = threading.Thread(target=worker, name="update-check", daemon=True)
    thread.start()
    return thread
