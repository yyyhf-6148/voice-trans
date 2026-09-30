"""系统托盘图标：右键菜单 + 状态提示 + 程序化生成的图标。

为什么图标是画出来的而不是读 PNG
--------------------------------
    打包 / 换目录 / 换工作目录都会让相对资源路径失效。
    这里用 QPainter 画一个纯几何图标（圆角底 + 三条电平柱），
    **不依赖任何字体**（offscreen 平台没有字体后端，画字会变方块），
    也不依赖外部文件。

线程模型
--------
    QSystemTrayIcon 必须在 Qt 主线程创建与操作。
    本类发出的信号都在主线程，App 直接连接即可。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..logging_setup import get_bound_logger

log = get_bound_logger(__name__)

# 图标配色：正常 / 断开 / 待连接
_ICON_COLORS = {
    "ok": "#38BDF8",
    "error": "#F87171",
    "idle": "#94A3B8",
}


def make_tray_icon(color: str = "#38BDF8", size: int = 64) -> QIcon:
    """程序化生成托盘图标（圆角底 + 三条电平柱）。

    纯几何绘制，不依赖字体 —— offscreen 环境下也不会出方块。
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)

    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)

    # 圆角底
    radius = size * 0.22
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)

    # 三条电平柱（深浅对比用底色反相）
    bar_w = size * 0.13
    gap = size * 0.10
    total = bar_w * 3 + gap * 2
    x0 = (size - total) / 2.0
    heights = (0.30, 0.58, 0.42)

    p.setBrush(QColor("#0B1220"))
    for i, hf in enumerate(heights):
        h = size * hf
        x = x0 + i * (bar_w + gap)
        y = (size - h) / 2.0
        p.drawRoundedRect(QRectF(x, y, bar_w, h), bar_w / 2.0, bar_w / 2.0)

    p.end()
    return QIcon(pm)


class TrayIcon(QObject):
    """托盘图标封装。

    交互约定
    --------
      · 左键单击 / 双击 → 显示或隐藏悬浮窗
      · 右键 → 弹出菜单
      · 所有菜单动作以信号形式抛出，App 负责真正执行
    """

    action_triggered = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        self._icons = {k: make_tray_icon(v) for k, v in _ICON_COLORS.items()}
        self._state = "idle"

        self._tray = QSystemTrayIcon(self._icons["idle"], self)
        self._tray.setToolTip("实时音频翻译 · 启动中…")

        self._menu = QMenu()
        self._actions: dict[str, QAction] = {}

        # ---- 悬浮窗相关 ----
        self._add_checkable("toggle_visibility", "显示字幕", True)
        self._add_checkable("toggle_click_through", "点击穿透（不挡游戏）", False)
        self._add_action("clear", "清空字幕")
        self._menu.addSeparator()

        # ---- 程序相关 ----
        self._add_action("settings", "设置…")
        self._add_action("reconnect", "重新连接")
        self._menu.addSeparator()
        self._add_action("open_config", "打开配置文件")
        self._add_action("open_logs", "打开日志目录")
        self._add_action("restart", "重启程序")
        self._menu.addSeparator()
        self._add_action("quit", "退出")

        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)

    # ---------------- 构建 ----------------
    def _add_action(self, name: str, text: str) -> QAction:
        act = QAction(text, self._menu)
        act.triggered.connect(lambda _=False, n=name: self.action_triggered.emit(n))
        self._menu.addAction(act)
        self._actions[name] = act
        return act

    def _add_checkable(self, name: str, text: str, checked: bool) -> QAction:
        act = self._add_action(name, text)
        act.setCheckable(True)
        act.setChecked(checked)
        return act

    # ---------------- 生命周期 ----------------
    def show(self) -> bool:
        """显示托盘图标。系统不支持托盘时返回 False（不抛错）。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.warning(
                "系统托盘不可用，跳过托盘图标",
                detail="悬浮窗与全局热键不受影响，仍可用 Ctrl+Alt+Q 退出",
            )
            return False
        self._tray.show()
        log.info("托盘图标已显示")
        return True

    def hide(self) -> None:
        self._tray.hide()

    # ---------------- 状态同步 ----------------
    def set_state(self, state: str, tooltip: str = "") -> None:
        """更新图标颜色与提示文字。state: ok | error | idle"""
        if state not in self._icons:
            state = "idle"
        if state != self._state:
            self._state = state
            self._tray.setIcon(self._icons[state])
        if tooltip:
            self._tray.setToolTip(tooltip)

    def set_overlay_visible(self, visible: bool) -> None:
        act = self._actions.get("toggle_visibility")
        if act is not None:
            act.setChecked(visible)

    def set_click_through(self, enabled: bool) -> None:
        act = self._actions.get("toggle_click_through")
        if act is not None:
            act.setChecked(enabled)

    def notify(self, title: str, message: str, msecs: int = 3000) -> None:
        """气泡提示（系统不支持时静默忽略）。"""
        try:
            self._tray.showMessage(
                title, message, QSystemTrayIcon.Information, msecs
            )
        except Exception as e:      # pragma: no cover - 依赖系统实现
            log.debug("气泡提示失败", error=str(e))

    # ---------------- 内部 ----------------
    def _on_activated(self, reason) -> None:
        # 左键单击 / 双击 → 切换悬浮窗显隐
        if reason in (
            QSystemTrayIcon.Trigger,
            QSystemTrayIcon.DoubleClick,
        ):
            self.action_triggered.emit("toggle_visibility")
