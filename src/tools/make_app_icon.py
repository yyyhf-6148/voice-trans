"""生成 exe 用的图标 `assets/app.ico`。

复用托盘图标那一套画法（圆角底 + 三条电平柱），保证
**任务栏 / 托盘 / exe 文件图标**三处视觉一致，且不引入任何外部图片资源。

用法::

    .venv\\Scripts\\python.exe -m src.tools.make_app_icon
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 没有桌面时也能跑（CI / 服务器）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]

# Windows 的 exe 图标建议 256×256（系统会自动缩放其它尺寸）
_SIZES = (256, 128, 64, 48, 32, 16)


def main() -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from ..ui.tray import make_tray_icon

    QApplication.instance() or QApplication(sys.argv)

    out_dir = _ROOT / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "app.ico"

    icon = make_tray_icon("#38BDF8", 256)
    # 把多个尺寸塞进同一个 .ico —— Qt 的 ICO writer 只写当前 pixmap，
    # 所以先写 256，再把小尺寸追加进去（Qt 不直接支持，这里退而求其次：
    # 只写 256×256，Windows 缩放效果已经足够）。
    pm = icon.pixmap(256, 256)
    ok = pm.save(str(out), "ICO")
    if not ok:
        print("[ERROR] 写出 ICO 失败", file=sys.stderr)
        return 1

    print(f"saved {out}  ({out.stat().st_size} bytes, 256x256)")
    print("可用尺寸参考:", ", ".join(str(s) for s in _SIZES))
    print("QIcon 尺寸数:", len(icon.availableSizes()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
