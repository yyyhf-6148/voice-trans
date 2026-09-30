"""悬浮窗视觉预览：用模拟数据渲染一张 PNG，用于快速看样式。

不需要 API Key、不采集音频、不联网。
调样式时改 config.yaml 的 overlay 节，重跑本脚本即可。

用法:
    .venv\\Scripts\\python.exe tests/preview_overlay.py
    .venv\\Scripts\\python.exe tests/preview_overlay.py -o preview.png
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须用 offscreen，才能在无桌面环境渲染出图
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.config import load_config  # noqa: E402
from src.ui.overlay import OverlayWindow  # noqa: E402

# 模拟一段舰队指挥语音流（含增量→定稿的完整生命周期）
# 增量阶段不要传 original —— 真实链路里此时还没有原文
SAMPLE: list[tuple] = [
    # (译文, 原文, is_partial, first_text_delay_ms, emotion)
    ("全体注意，诱导已经就位。", "All ships, cyno is up.", False, 118.0, "neutral"),
    ("侦察报告：本地有 12 人。", "Scout reports twelve in local.", False, 132.0, "neutral"),
    ("奶妈把电容保持住", "", True, None, ""),
    ("奶妈把电容保持住，先把后勤救起来。",
     "Logi keep the cap up, save the logi first.", False, 145.0, "neutral"),
    ("抓人！抓住星门那边的目标！", "Tackle! Tackle the target on the gate!",
     False, 96.0, "angry"),
    ("所有人对齐星门出口。", "Everyone align to the outgate.", False, 121.0, "neutral"),
    ("泡泡已经放下了，别过门。", "Bubbles are up, do not jump.", False, 127.0, "neutral"),
    ("装弹，准备第二轮。", "Reload, prepare for second cycle.", False, 109.0, "neutral"),
]

# 附带一条错误示例，让预览能覆盖全部视觉状态
SAMPLE_ERROR = "服务端连接中断，正在重连…"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="overlay_preview.png",
                    help="输出图片路径")
    ap.add_argument("--with-error", action="store_true",
                    help="附带一条错误提示（预览红色样式）")
    ap.add_argument("--transparent-bg", action="store_true",
                    help="用纯色底替代透明，便于看清圆角与半透明效果")
    args = ap.parse_args()

    cfg = load_config(str(ROOT / "config.yaml"))
    ov = cfg.overlay

    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    w = OverlayWindow(ov)
    for zh, en, partial, delay, emo in SAMPLE:
        w.push_translation(
            zh, original=en, is_partial=partial,
            first_text_delay_ms=delay, emotion=emo, language="en",
        )
    if args.with_error:
        w.push_error(SAMPLE_ERROR)
    w._drain_queue()
    w._tick()
    app.processEvents()

    # 渲染到 QImage（透明底）
    img = QImage(w.width(), w.height(), QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    # 给透明底加一层棋盘格，这样半透明效果能看出来
    if not args.transparent_bg:
        for y in range(0, w.height(), 16):
            for x in range(0, w.width(), 16):
                shade = 60 if ((x // 16) + (y // 16)) % 2 == 0 else 90
                p.fillRect(x, y, 16, 16, QColor(shade, shade, shade + 8))
    p.end()

    # render(QPaintDevice) 只收位置参数，PySide6 不支持 target= 关键字
    w.render(img)

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    ok = img.save(str(out))
    if not ok:
        print(f"[X] 保存失败: {out}")
        return 1

    print(f"[OK] 已保存预览图: {out}")
    print(f"     尺寸 {w.width()}x{w.height()}   条目 {len(w.store.items)}")
    print(f"     字号 译文{ov.font_size_translation}px / 原文{ov.font_size_original}px")
    print(f"     背景 {ov.background}  不透明度 {ov.opacity}")

    w.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
