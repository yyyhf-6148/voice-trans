"""设置窗口实机截图（必须用真实平台，offscreen 没有字体后端会全变方块）。

跑法:
    .venv\\Scripts\\python.exe tests/preview_settings_live.py

产出:
    settings_tab0.png ~ settings_tab4.png   对话框实际观感（滚动区内可见部分）
    settings_page0.png ~ settings_page4.png 每个分页的完整内容（不受滚动裁剪）
    settings_validation.png                 校验失败时的红字提示
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QFontMetrics               # noqa: E402
from PySide6.QtWidgets import QApplication          # noqa: E402

from src.config import load_config                  # noqa: E402
from src.ui.settings_dialog import SettingsDialog   # noqa: E402
from src.ui.tray import make_tray_icon              # noqa: E402

_TABS = ["接口与密钥", "音频", "字幕", "快捷键与托盘", "高级"]


def _pump(app, n: int = 8) -> None:
    for _ in range(n):
        app.processEvents()


def _unique_colors(pixmap) -> int:
    """统计像素图里不同颜色的数量 —— 用来证明"确实画出了内容"。

    全空白页只有 1 种颜色；有文字/边框的页面会有几十种。
    """
    img = pixmap.toImage()
    seen: set[int] = set()
    for y in range(0, img.height(), 3):
        for x in range(0, img.width(), 3):
            seen.add(img.pixel(x, y))
    return len(seen)


def _grab_full_page(app, area, out: Path) -> int:
    """把滚动区里的整页内容按完整高度抓下来（不受可视区域裁剪）。

    返回该页的独特颜色数（用于断言不是空白页）。
    """
    page = area.widget()
    page.adjustSize()
    _pump(app)
    w = max(page.sizeHint().width(), area.viewport().width())
    page.resize(w, page.sizeHint().height())
    _pump(app)
    pm = page.grab()
    ok = pm.save(str(out))
    colors = _unique_colors(pm)
    print(f"{'saved' if ok else 'FAILED'} {out.name}  "
          f"({page.width()}x{page.height()}, {colors} colors)")
    return colors


def main() -> int:
    cfg = load_config(str(ROOT / "config.yaml"))
    app = QApplication.instance() or QApplication(sys.argv)

    # 托盘图标单独存一张（放大 4 倍便于查看细节）
    icon = make_tray_icon("#38BDF8", 64)
    icon.pixmap(256, 256).save(str(ROOT / "tray_icon.png"))
    print("saved tray_icon.png")

    dlg = SettingsDialog(cfg, None)
    dlg.resize(820, 900)
    dlg.show()
    _pump(app)

    # 真实平台必须有 CJK 字形，否则中文会画成方块（offscreen 平台的典型症状）
    fm = QFontMetrics(dlg.font())
    missing = [c for c in "接口密钥音频字幕快捷键托盘高级" if not fm.inFont(c)]
    print(f"CJK 字形检查: {'OK' if not missing else 'MISSING ' + ''.join(missing)}")

    blank: list[str] = []
    for i, name in enumerate(_TABS):
        dlg._tabs.setCurrentIndex(i)
        _pump(app)

        out = ROOT / f"settings_tab{i}.png"
        ok = dlg.grab().save(str(out))
        print(f"{'saved' if ok else 'FAILED'} {out.name}  ({name})")

        colors = _grab_full_page(
            app, dlg._tabs.widget(i), ROOT / f"settings_page{i}.png")
        if colors < 5:
            blank.append(name)

    print(f"空白页检查: {'OK' if not blank else 'BLANK ' + ', '.join(blank)}")

    # 校验失败提示（后端已固定 livetranslate，用它的接口地址字段）
    dlg._tabs.setCurrentIndex(0)
    dlg._fields["asr.livetranslate.url"].setText("http://wrong-scheme")
    dlg.accept()
    _pump(app)
    out = ROOT / "settings_validation.png"
    ok = dlg.grab().save(str(out))
    print(f"{'saved' if ok else 'FAILED'} {out.name}  "
          f"(错误提示可见={dlg._error_label.isVisible()})")

    dlg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
