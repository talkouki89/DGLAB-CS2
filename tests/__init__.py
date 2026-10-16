"""離線測試套件。

執行方式（在工作目錄根目錄）：

    python -m unittest discover -s tests -t . -v
"""

import logging

# 測試時不想被應用程式自己的記錄洗版；單元測試的結果仍然正常輸出。
#
# 這裡刻意用「把 root logger 的等級調高」而不是 ``logging.disable()``：
# 後者是**全域開關**，會讓 ``assertLogs`` 完全失效（不管把目標 logger 的
# 等級設成什麼，``Logger.isEnabledFor`` 都會先被那個開關擋掉），
# 於是「有沒有正確記錄錯誤」這類測試就永遠測不出問題。
logging.getLogger().setLevel(logging.CRITICAL)
