"""確認程式版本和要發布的 tag 一致。

**發布前一定要跑這個。** 忘記改 ``coyote/__init__.py`` 的 ``__version__``
就會出事：exe 裡寫的還是舊版號，使用者一開啟就看到「有新版本」的提示，
點下去卻發現自己已經是最新版了（實際發生過一次：v3.2.1 的 exe 回報 3.2.0）。

用法（在工作目錄根目錄）：

    python tools/check_version.py v3.2.1

版本一致時回傳 0，不一致時回傳 1 並印出兩邊的版號。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coyote import __version__  # noqa: E402


def normalize(text: str) -> str:
    """把 ``v3.2.1`` 或 ``3.2.1`` 都正規化成 ``3.2.1``。"""
    return text.strip().lstrip("vV")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：python tools/check_version.py <tag>   例如 v3.2.1")
        return 2

    tag = normalize(argv[1])
    current = normalize(__version__)
    if tag == current:
        print(f"  版本一致：{current}")
        return 0

    print(f"  !! 版本不一致：程式是 {current}，但你要發布 {tag}")
    print(f"     請把 coyote/__init__.py 的 __version__ 改成 \"{tag}\" 再重新打包。")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
