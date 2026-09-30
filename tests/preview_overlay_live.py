"""悬浮窗实机预览：在真实桌面上弹出窗口，渲染后截图保存。

与 preview_overlay.py 的区别
---------------------------
`preview_overlay.py` 用 Qt 的 offscreen 平台渲染。
**本机的 offscreen 插件没有可用的字体后端，所有字形都画成方块**，
所以那张图只能看布局，看不清文字。

本脚本用真实平台（windows）显示窗口后再抓图，文字是真实的。
代价是会短暂弹出窗口（约 1.5 秒）。

用法:
    .venv\\Scripts\\python.exe tests/preview_overlay_live.py
    .venv\\Scripts\\python.exe tests/preview_overlay_live.py --keep
        （--keep = 保留窗口不自动关闭，按 Ctrl+C 结束）
    .venv\\Scripts\\python.exe tests/preview_overlay_live.py --scroll
        （--scroll = 连拍三张：跟随尾部 / 上滚翻历史 / 回到底部）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.ui.overlay import OverlayWindow  # noqa: E402

# 模拟舰队指挥语音流（覆盖：定稿 / 增量 / 情绪高亮 / 错误）
#
# ⚠️ 顺序要贴近真实：
#    服务端先推增量（只有译文片段），最后推定稿（译文 + 原文都可能到）。
#    增量阶段不要传 original —— 真实链路里此时还没有原文，
#    传了会渲染出"译文/原文并排"的假象。
SAMPLE: list[tuple[str, str, bool, float | None, str]] = [
    ("全体注意，诱导已经就位。", "All ships, cyno is up.", False, 118.0, "neutral"),
    ("侦察报告：本地有 12 人。", "Scout reports twelve in local.", False, 132.0, "neutral"),
    # 增量：只有译文片段
    ("奶妈把电容保持住", "", True, None, ""),
    ("奶妈把电容保持住，先把后勤救起来。",
     "Logi keep the cap up, save the logi first.", False, 145.0, "neutral"),
    ("抓人！抓住星门那边的目标！", "Tackle! Tackle the target on the gate!",
     False, 96.0, "angry"),
    ("所有人对齐星门出口。", "Everyone align to the outgate.", False, 121.0, "neutral"),
    ("泡泡已经放下了，别过门。", "Bubbles are up, do not jump.", False, 127.0, "neutral"),
    ("装弹，准备第二轮。", "Reload, prepare for second cycle.", False, 109.0, "neutral"),
]


def _fill(w: OverlayWindow, target: int) -> None:
    """循环推入 target 条**定稿**字幕，把窗口顶到 max_height 制造溢出。

    只用定稿条目 —— 增量条目会被后续定稿改写，不适合当滚动演示的素材。
    """
    pushed = 0
    k = 0
    while pushed < target:
        zh, en, partial, delay, emo = SAMPLE[k % len(SAMPLE)]
        k += 1
        if partial:
            continue
        w.push_translation(
            zh, original=en, is_partial=False,
            first_text_delay_ms=delay, emotion=emo, language="en",
        )
        pushed += 1
    w._drain_queue()
    w._tick()


def _wheel(w: OverlayWindow, notches: int) -> None:
    """派发一个滚轮事件（notches>0 上滚翻历史，<0 下滚回最新）。"""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    cx, cy = w.width() / 2.0, w.height() / 2.0
    w.wheelEvent(QWheelEvent(
        QPointF(cx, cy), QPointF(cx, cy),
        QPoint(0, 0), QPoint(0, 120 * notches),
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
    ))


def _grab(app, w: OverlayWindow, out: Path, tag: str = "") -> None:
    """抓窗口所在的屏幕矩形（半透明窗口不能走 grabWindow(winId())）。"""
    screen = app.primaryScreen()
    g = w.frameGeometry()
    pm = screen.grabWindow(0, g.x(), g.y(), g.width(), g.height())
    ok = pm.save(str(out))
    below = w._below_count if w._has_more_below else 0
    print(f"[{'OK' if ok else 'X '}] {out.name}  {tag}   "
          f"条目={len(w.store.items)}  跟随尾部={w._follow_tail}  "
          f"下方还有={below} 条")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="overlay_live.png")
    ap.add_argument("--keep", action="store_true", help="截图后保留窗口")
    ap.add_argument("--with-error", action="store_true",
                    help="附带一条红色错误提示")
    ap.add_argument("--scroll", action="store_true",
                    help="滚动演示：连拍 跟随尾部 / 上滚翻历史 / 回到底部 三张")
    args = ap.parse_args()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    cfg = load_config(str(ROOT / "config.yaml"))

    app = QApplication.instance() or QApplication(sys.argv)

    # 关键：不设 QT_QPA_PLATFORM，用真实平台
    w = OverlayWindow(cfg.overlay)
    # 预览时把窗口挪到屏幕中上，避免和任务栏/托盘重叠
    w.move(120, 120)
    w._user_moved = True
    w.show()

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out

    if args.scroll:
        return _run_scroll_demo(app, w, out, QTimer, args.keep)

    for zh, en, partial, delay, emo in SAMPLE:
        w.push_translation(
            zh, original=en, is_partial=partial,
            first_text_delay_ms=delay, emotion=emo, language="en",
        )
    if args.with_error:
        w.push_error("服务端连接中断，正在重连…")
    w._drain_queue()
    w._tick()

    def grab() -> None:
        _grab(app, w, out)
        if not args.keep:
            app.quit()

    # 等窗口真正显示完再抓，否则可能是空白
    QTimer.singleShot(900, grab)

    if args.keep:
        print("窗口保持显示中，Ctrl+C 结束")
    return app.exec()


def _run_scroll_demo(app, w: OverlayWindow, out: Path, QTimer, keep: bool) -> int:
    """滚动演示：三帧连拍，验证「上滚停跟随、回底恢复跟随」。"""
    stem = out.with_suffix("")
    suffix = out.suffix or ".png"
    p_follow = stem.with_name(stem.name + "_1_跟随尾部").with_suffix(suffix)
    p_scroll = stem.with_name(stem.name + "_2_上滚翻历史").with_suffix(suffix)
    p_bottom = stem.with_name(stem.name + "_3_回到底部").with_suffix(suffix)

    _fill(w, 26)  # 26 条 → 内容超过 max_height，必然溢出
    print(f"窗口高度 {w.height()}px（max_height={w.cfg.max_height}）")

    def step1() -> None:
        _grab(app, w, p_follow, "① 跟随尾部")
        _wheel(w, +4)  # 上滚 4 格
        QTimer.singleShot(300, step2)

    def step2() -> None:
        _grab(app, w, p_scroll, "② 上滚翻历史")
        _wheel(w, -60)  # 一路下滚回底部
        QTimer.singleShot(300, step3)

    def step3() -> None:
        _grab(app, w, p_bottom, "③ 回到底部")
        if not keep:
            app.quit()

    QTimer.singleShot(900, step1)
    if keep:
        print("窗口保持显示中，Ctrl+C 结束")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
