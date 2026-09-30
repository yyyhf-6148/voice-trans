"""透明悬浮窗：把字幕条目渲染成聊天流式列表。

窗口行为要点（Windows）
-----------------------
**点击穿透**
    游戏里最关键的一点：悬浮窗不能挡住鼠标操作。
    PySide6 层面用 `Qt.WindowTransparentForInput`（鼠标事件直接穿透到下层窗口）
    配合 `WS_EX_TRANSPARENT | WS_EX_LAYERED` 扩展样式，两者都要设：
      · `WindowTransparentForInput` 让 Qt 不接收鼠标事件
      · `WS_EX_TRANSPARENT` 让 Windows 合成器把命中测试交给下层
    只设 Qt 标记时有概率仍被命中（尤其在独占全屏/无边框窗口游戏中）。

**无边框 + 置顶 + 无任务栏图标**
    `FramelessWindowHint | WindowStaysOnTopHint | Tool`
    · `Tool` 而非 `Window`：不显示在任务栏和 Alt+Tab 里
    · 配合 `WA_TranslucentBackground` 实现圆角与半透明背景

**不要抢焦点**
    `WA_ShowWithoutActivating` + `WindowDoesNotAcceptFocus`
    否则每次刷新字幕都会把游戏切到后台（非常致命）。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

from PySide6.QtCore import (
    QPoint,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import QWidget

from ..config import OverlaySection
from ..logging_setup import get_bound_logger
from .subtitle_model import SubtitleItem, SubtitleStore

log = get_bound_logger(__name__)

# ---- Win32 常量 ----
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

# 内容内边距（像素）。布局与绘制必须共用同一个值。
_PAD = 12

# 鼠标滚轮一格滚动的像素数（Windows 上一格 = angleDelta 120）
_WHEEL_LINE_PX = 54

# 判定"已滚到底部"的容差（像素）。滚到底就恢复自动跟随。
_SCROLL_EPS = 0.5

# 优先使用的字体（第一个存在的生效）。中文必须用带 CJK 字形的字体，
# 否则会显示成方块。
_FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "微软雅黑",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "SimHei",
)


def _pick_font_family(preferred: str = "") -> str:
    """挑一个可用的中文字体族。"""
    families = set(QFontDatabase_families())
    if preferred and preferred in families:
        return preferred
    # 中文字体常见别名粗匹配
    if preferred:
        for f in families:
            if preferred.lower() in f.lower():
                return f
    for c in _FONT_CANDIDATES:
        if c in families:
            return c
    for f in families:
        if "yahei" in f.lower() or "hei" in f.lower():
            return f
    return ""  # 交给 Qt 默认


def QFontDatabase_families() -> list[str]:
    from PySide6.QtGui import QFontDatabase

    return QFontDatabase.families()


class OverlayWindow(QWidget):
    """字幕悬浮窗。

    线程模型
    --------
    Qt 必须在主线程操作。pipeline 的回调运行在采集/网络线程上，
    因此本类暴露的 `push_translation()` 等方法是**线程安全**的：
    它们只往内部队列塞数据并发一个信号，真正的 UI 更新在
    `_drain_queue()` 里由主线程的定时器完成。
    """

    # 线程间通信用：在任意线程 emit，Qt 会自动排队到主线程
    _notify = Signal()

    def __init__(self, section: OverlaySection) -> None:
        super().__init__()
        self.cfg = section

        self._store = SubtitleStore(
            max_messages=section.max_messages,
            max_chars=section.max_chars_per_message,
            hold_seconds=section.hold_seconds,
        )
        # 待处理队列（跨线程写入，主线程消费）
        self._pending: list[tuple] = []
        self._pending_lock = __import__("threading").Lock()

        self._opacity = section.opacity
        self._click_through = section.click_through
        self._visible = True
        self._fade_alpha = 1.0
        self._last_activity = time.monotonic()

        # 拖动支持
        self._drag_offset: QPoint | None = None
        self._user_moved = False

        # ---- 滚动 / 跟随尾部 ----
        # _follow_tail=True：视口钉在最新一条（默认）
        # 用户上滚 → 变成 False，并把视口顶部**锚定到某个具体条目**，
        # 这样新消息到达时阅读位置不会被顶走（锚点法能同时扛住
        # max_messages 从头部裁掉旧条目导致的坐标整体位移）。
        self._follow_tail = True
        self._anchor_item: SubtitleItem | None = None
        self._anchor_dy = 0.0
        self._has_more_below = False
        self._below_count = 0

        self._font_family = _pick_font_family(section.font_family)
        self._setup_window()
        self._notify.connect(self._drain_queue, Qt.QueuedConnection)

        # 定时刷新：处理超时收敛、淡出、队列消费
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        log.info(
            "悬浮窗已创建",
            position=section.position,
            size=f"{section.width}x{section.min_height}~{section.max_height}",
            opacity=round(self._opacity, 2),
            click_through=self._click_through,
            font=self._font_family or "(default)",
        )

    # -------------------------------------------------- 窗口初始化
    def _build_flags(self) -> Qt.WindowType:
        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool                    # 不进任务栏 / Alt+Tab
            | Qt.WindowDoesNotAcceptFocus  # 不抢游戏焦点
        )
        if self._click_through:
            flags |= Qt.WindowTransparentForInput
        return flags

    def _setup_window(self) -> None:
        self.setWindowFlags(self._build_flags())

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

        self.resize(self.cfg.width, self.cfg.min_height)
        self._apply_position()

    def _apply_position(self) -> None:
        """按配置把窗口摆到屏幕角落。"""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo: QRect = screen.availableGeometry()
        w, h = self.width(), self.height()
        mx, my = self.cfg.margin_x, self.cfg.margin_y

        pos = self.cfg.position
        if pos == "bottom_right":
            x, y = geo.right() - w - mx, geo.bottom() - h - my
        elif pos == "bottom_left":
            x, y = geo.left() + mx, geo.bottom() - h - my
        elif pos == "top_right":
            x, y = geo.right() - w - mx, geo.top() + my
        elif pos == "top_left":
            x, y = geo.left() + mx, geo.top() + my
        elif pos == "bottom_center":
            x, y = geo.left() + (geo.width() - w) // 2, geo.bottom() - h - my
        else:
            x, y = geo.right() - w - mx, geo.bottom() - h - my
        self.move(int(x), int(y))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 窗口句柄此时才有效，设置扩展样式
        self._apply_win32_style()

    def _apply_win32_style(self) -> None:
        """设置 Windows 扩展样式（点击穿透 / 不激活 / 无任务栏）。"""
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            # 64 位下必须显式声明签名，否则返回值会被截断成 int32
            get_style = user32.GetWindowLongPtrW
            set_style = user32.SetWindowLongPtrW
            get_style.restype = ctypes.c_longlong
            get_style.argtypes = [wintypes.HWND, ctypes.c_int]
            set_style.restype = ctypes.c_longlong
            set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]

            style = get_style(hwnd, GWL_EXSTYLE)
            style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            if self._click_through:
                style |= WS_EX_TRANSPARENT
            set_style(hwnd, GWL_EXSTYLE, style)
        except Exception as e:
            log.warning("设置窗口扩展样式失败", error=str(e))

    # -------------------------------------------------- 线程安全接口
    def push_translation(
        self,
        translation: str,
        *,
        original: str = "",
        is_partial: bool,
        sequence: int = 0,
        first_text_delay_ms: float | None = None,
        language: str = "",
        emotion: str = "",
    ) -> None:
        """写入一条翻译（可从任意线程调用）。"""
        with self._pending_lock:
            self._pending.append(
                ("translation", dict(
                    translation=translation, original=original,
                    is_partial=is_partial, sequence=sequence,
                    first_text_delay_ms=first_text_delay_ms,
                    language=language, emotion=emotion,
                ))
            )
        self._notify.emit()

    def push_original(
        self, original: str, *, is_partial: bool, sequence: int = 0,
        language: str = "", emotion: str = "",
    ) -> None:
        with self._pending_lock:
            self._pending.append(
                ("original", dict(
                    original=original, is_partial=is_partial,
                    sequence=sequence, language=language, emotion=emotion,
                ))
            )
        self._notify.emit()

    def push_error(self, message: str) -> None:
        with self._pending_lock:
            self._pending.append(("error", dict(message=message)))
        self._notify.emit()

    def push_status(self, text: str) -> None:
        """写入一条状态提示（如"连接中断，正在重连"）。"""
        with self._pending_lock:
            self._pending.append(("status", dict(text=text)))
        self._notify.emit()

    # -------------------------------------------------- 事件循环
    def _drain_queue(self) -> None:
        """主线程消费待处理队列（由信号触发）。"""
        with self._pending_lock:
            batch, self._pending = self._pending, []
        if not batch:
            return

        touched = False
        for kind, kw in batch:
            if kind == "translation":
                original = kw.pop("original", "")
                self._store.push(**kw)
                if original:
                    self._store.push_original(
                        original,
                        is_partial=kw.get("is_partial", False),
                        sequence=kw.get("sequence", 0),
                        language=kw.get("language", ""),
                        emotion=kw.get("emotion", ""),
                    )
            elif kind == "original":
                self._store.push_original(**kw)
            elif kind == "error":
                self._store.push_error(kw["message"])
            elif kind == "status":
                self._store.items.append(
                    SubtitleItem(
                        translation=kw["text"], finalized=True, emotion="status"
                    )
                )
            touched = True

        if touched:
            self._last_activity = time.monotonic()
            self._fade_alpha = 1.0
            self._relayout()
            self.update()

    def _tick(self) -> None:
        """定时清点：收敛超时条目、移除过期条目、处理淡出。"""
        changed = self._store.finalize_stale(timeout_s=3.0)
        changed = self._store.expire_old() or changed

        # 自动淡出
        if self.cfg.auto_fade_after_s > 0:
            idle = time.monotonic() - self._last_activity
            if idle > self.cfg.auto_fade_after_s and self._fade_alpha > 0.15:
                self._fade_alpha = max(0.15, self._fade_alpha - 0.08)
                changed = True
            elif idle <= self.cfg.auto_fade_after_s and self._fade_alpha < 1.0:
                self._fade_alpha = 1.0
                changed = True

        if changed:
            self._relayout()
            self.update()

    def _relayout(self) -> None:
        """根据内容调整窗口高度（限制在 min/max 之间）。"""
        h = self._content_height()
        h = max(self.cfg.min_height, min(self.cfg.max_height, h))
        if h != self.height():
            self.resize(self.width(), h)
            if not self._user_moved:
                self._apply_position()

    def refresh_position(self) -> None:
        """重新贴回配置的位置（热键/配置变更时用）。"""
        self._user_moved = False
        self._relayout()
        self._apply_position()

    # -------------------------------------------------- 布局计算
    #
    # ⚠️ 高度计算必须**只有一处实现**。
    #    之前 _content_height / _paint_items / _paint_item 各算一遍，
    #    结果三处不一致 —— 指标行被算漏了，文字就互相重叠。
    #    现在统一走 _item_metrics()。
    def _item_metrics(self) -> list[tuple[SubtitleItem, int, int, int]]:
        """返回 [(条目, 译文高, 原文高, 指标高), ...]。"""
        pad = _PAD
        avail = self.width() - pad * 2
        fm_zh = QFontMetrics(self._font_zh())
        fm_en = QFontMetrics(self._font_en())
        out: list[tuple[SubtitleItem, int, int, int]] = []

        for it in self._store.items:
            h_zh = h_en = h_mark = 0
            if it.translation:
                h_zh = self._wrapped_height(fm_zh, it.translation, avail)
            if (self.cfg.show_original and it.original
                    and it.original != it.translation):
                h_en = self._wrapped_height(fm_en, it.original, avail)
            if it.translation and self._mark_text(it):
                h_mark = fm_en.lineSpacing()
            out.append((it, h_zh, h_en, h_mark))
        return out

    def _mark_text(self, it: SubtitleItem) -> str:
        """译文下方的辅助标记（情绪 / 首字延迟）。空串表示不显示。"""
        # 错误/状态提示本身就是给别人看的，不需要再挂指标
        if it.emotion in ("error", "status"):
            return ""
        marks: list[str] = []
        if it.is_alert:
            marks.append(f"[{it.emotion}]")
        if it.first_text_delay_ms:
            marks.append(f"{it.first_text_delay_ms:.0f}ms")
        return "  ".join(marks)

    def _item_total_height(self, m: tuple) -> int:
        _, h_zh, h_en, h_mark = m
        return h_zh + h_en + h_mark + self.cfg.line_spacing + 6

    def _content_height(self) -> int:
        """估算渲染所需高度。"""
        pad = _PAD
        total = pad * 2
        for m in self._item_metrics():
            total += self._item_total_height(m)
        return total

    @staticmethod
    def _wrapped_height(fm: QFontMetrics, text: str, width: int) -> int:
        rect = fm.boundingRect(
            QRect(0, 0, width, 10000),
            int(Qt.TextWordWrap) | int(Qt.AlignLeft),
            text,
        )
        return rect.height()

    def _font_zh(self) -> QFont:
        f = QFont(self._font_family) if self._font_family else QFont()
        f.setPixelSize(self.cfg.font_size_translation)
        f.setWeight(QFont.DemiBold)
        return f

    def _font_en(self) -> QFont:
        f = QFont(self._font_family) if self._font_family else QFont()
        f.setPixelSize(self.cfg.font_size_original)
        return f

    # -------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        alpha = self._opacity * self._fade_alpha
        painter.setOpacity(alpha)

        # 背景（圆角矩形，颜色支持 #AARRGGBB 自带透明度）
        bg = QColor(self.cfg.background)
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(self.width()),
                            float(self.height()), 10.0, 10.0)
        painter.fillPath(path, bg)

        # 左侧状态条：有活动语音时亮起（给玩家一个"正在收音"的视觉反馈）
        self._paint_activity_bar(painter)

        self._paint_items(painter)
        self._paint_scroll_hint(painter)
        painter.end()

    def _paint_activity_bar(self, painter: QPainter) -> None:
        active = any(not it.finalized for it in self._store.items)
        color = QColor(self.cfg.color_alert if active else "#334155")
        color.setAlpha(200 if active else 90)
        painter.fillRect(0, 8, 3, self.height() - 16, color)

    # -------------------------------------------------- 滚动 / 跟随尾部
    def _layout_state(self) -> tuple[list[tuple], list[int], int, int, int]:
        """一次性算出绘制与滚动都要用的布局量。

        Returns:
            (metrics, heights, content_h, inner_h, max_scroll)
        """
        metrics = self._item_metrics()
        heights = [self._item_total_height(m) for m in metrics]
        content_h = sum(heights)
        inner_h = max(1, self.height() - _PAD * 2)
        max_scroll = max(0, content_h - inner_h)
        return metrics, heights, content_h, inner_h, max_scroll

    @staticmethod
    def _anchor_at(
        metrics: list[tuple], heights: list[int], scroll_top: float
    ) -> tuple[SubtitleItem | None, float]:
        """内容坐标 scroll_top 落在哪一条上 → (条目, 该条顶部相对偏移)。"""
        off = 0.0
        for m, h in zip(metrics, heights):
            if off + h > scroll_top:
                return m[0], scroll_top - off
            off += h
        return (metrics[-1][0] if metrics else None), 0.0

    def _resolve_scroll_top(
        self, metrics: list[tuple], heights: list[int], max_scroll: int
    ) -> float:
        """求视口顶部在**内容坐标系**里的位置。"""
        if max_scroll <= 0:
            # 内容放得下 —— 没有可滚的，等同于在底部
            self._follow_tail = True
            self._anchor_item = None
            return 0.0
        if self._follow_tail or self._anchor_item is None:
            return float(max_scroll)
        off = 0.0
        for m, h in zip(metrics, heights):
            if m[0] is self._anchor_item:
                return float(
                    max(0.0, min(float(max_scroll), off + self._anchor_dy))
                )
            off += h
        # 锚点已被 max_messages 从头部挤掉 → 退到剩下的最旧一条
        self._anchor_item = None
        return 0.0

    def _freeze_follow(self, scroll_top: float) -> None:
        """停止自动跟随，把视口顶部锚定到当前那条上。

        用**条目对象**而不是像素偏移当锚点：新消息到达时内容只会
        在尾部增长、旧条目被裁时整体上移，锚点法两种情况下阅读位置都不跑。
        """
        metrics, heights, _content, _inner, max_scroll = self._layout_state()
        self._follow_tail = False
        if max_scroll <= 0:
            self._anchor_item = None
            self._anchor_dy = 0.0
            return
        self._anchor_item, self._anchor_dy = self._anchor_at(
            metrics, heights, scroll_top
        )

    def reset_scroll(self) -> None:
        """回到最新（恢复自动跟随）。"""
        self._follow_tail = True
        self._anchor_item = None
        self._anchor_dy = 0.0

    def wheelEvent(self, event) -> None:
        """鼠标滚轮：上滚翻历史，滚回底部自动恢复跟随。"""
        metrics, heights, _content, _inner, max_scroll = self._layout_state()
        if max_scroll <= 0:
            event.ignore()  # 内容放得下，没什么可滚
            return

        dy = float(event.pixelDelta().y())
        if dy == 0.0:
            dy = event.angleDelta().y() / 120.0 * _WHEEL_LINE_PX
        if dy == 0.0:
            event.ignore()
            return

        cur = self._resolve_scroll_top(metrics, heights, max_scroll)
        new = max(0.0, min(float(max_scroll), cur - dy))

        if new >= max_scroll - _SCROLL_EPS:
            # 已经翻到最新 → 恢复自动跟随（auto_scroll=false 时除外）
            self._follow_tail = bool(self.cfg.auto_scroll)
            self._anchor_item = None
            self._anchor_dy = 0.0
        else:
            self._anchor_item, self._anchor_dy = self._anchor_at(
                metrics, heights, new
            )
            self._follow_tail = False

        self.update()
        event.accept()

    def _paint_items(self, painter: QPainter) -> None:
        pad = _PAD
        avail = self.width() - pad * 2
        fm_zh = QFontMetrics(self._font_zh())
        fm_en = QFontMetrics(self._font_en())

        metrics, heights, content_h, inner_h, max_scroll = self._layout_state()
        if not metrics:
            self._has_more_below = False
            self._below_count = 0
            return

        # auto_scroll=false：首次出现溢出就停住，此后新消息不再移动视口
        if not self.cfg.auto_scroll and self._follow_tail and max_scroll > 0:
            self._freeze_follow(float(max_scroll))

        scroll_top = self._resolve_scroll_top(metrics, heights, max_scroll)
        self._has_more_below = (max_scroll - scroll_top) > _SCROLL_EPS
        self._below_count = 0

        if max_scroll <= 0:
            # 内容比视口矮 → 底部对齐（保持原有观感）
            y0 = float(self.height() - pad - content_h)
        else:
            y0 = pad - scroll_top

        # 裁剪到内边距区域：滚动时首尾条目会被切掉半行，
        # 不能让它画到 padding / 圆角外面
        painter.save()
        painter.setClipRect(QRect(0, pad, self.width(), inner_h))
        off = 0.0
        for idx, (m, h) in enumerate(zip(metrics, heights)):
            y = y0 + off
            off += h
            if y + h < pad:  # 整条在视口上方
                continue
            if y > pad + inner_h:  # 整条在视口下方
                self._below_count = len(metrics) - idx
                break
            self._paint_item(
                painter, m, int(round(y)), pad, avail, fm_zh, fm_en
            )
        painter.restore()

    def _paint_scroll_hint(self, painter: QPainter) -> None:
        """视口下方还有内容时给个提示，否则用户不知道来了新消息。"""
        if not self._has_more_below:
            return
        text = f"▼ 下方 {self._below_count} 条" if self._below_count else "▼"
        fm = QFontMetrics(self._font_en())
        w = fm.horizontalAdvance(text) + 18
        h = fm.height() + 4
        x = self.width() - _PAD - w
        y = self.height() - _PAD - h

        painter.save()
        pill = QColor(self.cfg.color_alert)
        pill.setAlpha(215)
        painter.setPen(Qt.NoPen)
        painter.setBrush(pill)
        painter.drawRoundedRect(QRectF(x, y, w, h), h / 2.0, h / 2.0)
        painter.setPen(QColor("#101216"))
        painter.setFont(self._font_en())
        painter.drawText(QRect(x, y, w, h), int(Qt.AlignCenter), text)
        painter.restore()

    def _paint_item(
        self, painter: QPainter, metric: tuple, y: int, pad: int, avail: int,
        fm_zh: QFontMetrics, fm_en: QFontMetrics,
    ) -> int:
        it, h_zh, h_en, h_mark = metric

        # ---- 中文译文 ----
        if it.translation:
            if it.emotion == "error":
                color = QColor(self.cfg.color_error)
            elif it.emotion == "status":
                color = QColor(self.cfg.color_alert)
            elif it.finalized:
                color = QColor(self.cfg.color_translation)
            else:
                # 增量用略暗的色，直观区分"还会变"
                color = QColor(self.cfg.color_translation)
                color.setAlpha(170)
            painter.setFont(self._font_zh())
            painter.setPen(color)
            painter.drawText(
                QRect(pad, y, avail, h_zh),
                int(Qt.TextWordWrap) | int(Qt.AlignLeft) | int(Qt.AlignTop),
                it.translation,
            )
            y += h_zh

            # 情绪 / 首字延迟标记（高度已计入 h_mark，不会再压到原文）
            mark = self._mark_text(it)
            if mark:
                painter.setFont(self._font_en())
                painter.setPen(QColor(self.cfg.color_alert))
                painter.drawText(
                    QRect(pad, y, avail, h_mark),
                    int(Qt.AlignLeft) | int(Qt.AlignVCenter),
                    mark,
                )
                y += h_mark

        # ---- 英文原文 ----
        if h_en > 0:
            painter.setFont(self._font_en())
            painter.setPen(QColor(self.cfg.color_original))
            painter.drawText(
                QRect(pad, y, avail, h_en),
                int(Qt.TextWordWrap) | int(Qt.AlignLeft) | int(Qt.AlignTop),
                it.original,
            )
            y += h_en

        y += self.cfg.line_spacing + 6
        return y

    # -------------------------------------------------- 交互
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self._click_through:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._user_moved = True

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None

    # -------------------------------------------------- 对外控制
    def toggle_visibility(self) -> bool:
        if self._visible:
            self.hide()
            self._visible = False
        else:
            self.show()
            self._visible = True
        return self._visible

    def adjust_opacity(self, delta: float) -> float:
        self._opacity = max(0.05, min(1.0, self._opacity + delta))
        self.update()
        return self._opacity

    def set_opacity(self, value: float) -> float:
        self._opacity = max(0.05, min(1.0, value))
        self.update()
        return self._opacity

    def toggle_click_through(self) -> bool:
        self._click_through = not self._click_through
        self._rebuild_flags()
        return self._click_through

    def _rebuild_flags(self) -> None:
        """重设窗口标志并恢复可见性。

        ⚠️ `setWindowFlags()` 会**隐藏窗口**，必须重新 show()，
        否则调用方看到的是"窗口消失了"。
        """
        was_visible = self._visible
        self.setWindowFlags(self._build_flags())
        if was_visible:
            self.show()
        self._apply_win32_style()
        self.update()

    def apply_config(self, section: OverlaySection) -> None:
        """热应用新的悬浮窗配置（不重启进程）。

        对应托盘「设置」里改动的 `overlay.*` 字段。
        字体、位置、尺寸、条数上限、配色全部即时生效。
        """
        click_changed = section.click_through != self._click_through
        self.cfg = section

        self._opacity = max(0.05, min(1.0, section.opacity))
        self._fade_alpha = 1.0
        self._click_through = section.click_through

        # 字体族可能变了（配置里留空 = 自动挑）
        self._font_family = _pick_font_family(section.font_family)

        # 字幕仓库的上限来自配置，需要同步
        self._store.max_messages = max(1, section.max_messages)
        self._store.max_chars = section.max_chars_per_message
        self._store.hold_seconds = section.hold_seconds
        self._store._trim()

        if self.width() != section.width:
            self.resize(section.width, self.height())

        if click_changed:
            self._rebuild_flags()

        # 配置变更后回到最新，避免旧锚点指向已被裁掉的条目
        self.reset_scroll()
        self.refresh_position()
        self.update()
        log.info(
            "悬浮窗配置已热应用",
            opacity=round(self._opacity, 2),
            position=section.position,
            click_through=self._click_through,
        )

    def clear_items(self) -> None:
        self._store.clear()
        self.reset_scroll()
        self._relayout()
        self.update()

    # -------------------------------------------------- 属性
    @property
    def store(self) -> SubtitleStore:
        return self._store

    @property
    def opacity(self) -> float:
        return self._opacity

    @property
    def click_through(self) -> bool:
        return self._click_through
