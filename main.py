"""打包入口（PyInstaller 专用）。

为什么需要这个薄壳
------------------
`src/app.py` 内部用的是**包内相对导入**（`from .audio.capture import warmup`）。
如果直接把它当脚本入口交给 PyInstaller，它会被当作顶层 `__main__` 模块，
不属于任何包，相对导入会立刻报
`ImportError: attempted relative import with no known parent package`。

所以打包时用这个文件做入口：它先把 `src` 当普通包导入，再调用 `main()`。

源码运行时也可以直接用（等价于 `python -m src.app`）：

    python main.py
    python main.py --no-overlay
"""
from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

# 让 `import src.xxx` 在"从别处双击 exe / 换工作目录"时也能找到包。
# 打包后 PyInstaller 已经把 src 收进归档，这里只是源码运行的兜底。
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.app import main          # noqa: E402

if __name__ == "__main__":
    # PyInstaller onefile 会 fork 子进程；没有这行，子进程会再跑一遍主逻辑。
    multiprocessing.freeze_support()
    raise SystemExit(main())
