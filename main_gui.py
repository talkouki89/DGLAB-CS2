"""圖形介面的進入點。

直接雙擊執行，或從終端機：

    python main_gui.py
    python main_gui.py --dry-run
    python main_gui.py --config 我的設定.toml
"""

from __future__ import annotations

import sys

from gui import main

if __name__ == "__main__":
    sys.exit(main())
