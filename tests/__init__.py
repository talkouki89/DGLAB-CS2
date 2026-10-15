"""離線測試套件。

執行方式（在工作目錄根目錄）：

    python -m unittest discover -s tests -t . -v
"""

import logging

# 測試時不想被應用程式自己的記錄洗版；單元測試的結果仍然正常輸出。
logging.disable(logging.CRITICAL)
