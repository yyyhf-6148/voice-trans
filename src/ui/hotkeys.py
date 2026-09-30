"""全局热键（Windows）。

用 `keyboard` 库注册系统级热键——**无需管理员权限**，
且不会把按键事件"吃掉"（游戏仍能收到该按键），
这点比 RegisterHotKey API 更适合叠加在游戏上的场景。

⚠️ 热键回调运行在 keyboard 自己的监听线程上。
**绝不能**在回调里直接操作 Qt 控件。本模块的做法是
把动作投递到一个队列，由 Qt 主线程的定时器取出执行。
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from ..config import HotkeysSection
from ..logging_setup import get_bound_logger

log = get_bound_logger(__name__)


class HotkeyManager:
    """注册与管理全局热键。

    用法::

        mgr = HotkeyManager(cfg.hotkeys)
        mgr.bind("toggle_visibility", lambda: overlay.toggle_visibility())
        mgr.start()
        ...
        mgr.stop()
    """

    def __init__(self, section: HotkeysSection) -> None:
        self.section = section
        self._actions: dict[str, Callable[[], None]] = {}
        self._handles: list = []
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop_evt = threading.Event()
        self._pump_thread: threading.Thread | None = None
        self._started = False

    # ---------------- 注册 ----------------
    def bind(self, name: str, fn: Callable[[], None]) -> None:
        """把动作名绑定到回调函数。名字对应 HotkeysSection 的字段名。"""
        self._actions[name] = fn

    def combo(self, name: str) -> str:
        """取动作名对应的热键字符串。"""
        return getattr(self.section, name, "") or ""

    # ---------------- 生命周期 ----------------
    def start(self) -> int:
        """注册热键并启动派发线程。返回成功注册的数量。"""
        if self._started:
            return len(self._handles)

        try:
            import keyboard
        except ImportError as e:
            log.warning(
                "未安装 keyboard 库，全局热键不可用",
                detail="pip install keyboard",
                error=str(e),
            )
            return 0

        # 先启动派发线程，再注册，避免回调早于消费端就绪
        self._stop_evt.clear()
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="HotkeyPump", daemon=True
        )
        self._pump_thread.start()

        ok = 0
        for name in self._actions:
            combo = self.combo(name)
            if not combo:
                continue
            try:
                h = keyboard.add_hotkey(
                    combo,
                    lambda n=name: self._queue.put(n),
                    suppress=False,     # 关键：不吃掉按键，游戏照常收到
                    trigger_on_release=False,
                )
                self._handles.append(h)
                ok += 1
                log.info("热键已注册", action=name, combo=combo)
            except Exception as e:
                log.warning(
                    "热键注册失败", action=name, combo=combo, error=str(e)
                )
        self._started = True
        return ok

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_evt.set()
        try:
            import keyboard

            for h in self._handles:
                try:
                    keyboard.remove_hotkey(h)
                except Exception:
                    pass
        except Exception:
            pass
        self._handles.clear()
        if self._pump_thread is not None and self._pump_thread.is_alive():
            self._pump_thread.join(timeout=1.0)
        self._started = False
        log.info("全局热键已注销")

    # ---------------- 派发 ----------------
    def _pump_loop(self) -> None:
        """把热键事件转交给注册的回调（仍在非 Qt 线程）。

        调用方（UI 层）需保证回调本身是线程安全的——
        约定：回调只往 Qt 的线程安全入口投递。
        """
        while not self._stop_evt.is_set():
            try:
                name = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            fn = self._actions.get(name)
            if fn is None:
                continue
            try:
                fn()
            except Exception as e:
                log.error("热键回调异常", action=name, error=str(e))

    def drain(self) -> None:
        """兼容接口：派发由内部线程完成，这里无需动作。"""
        return

    @property
    def is_active(self) -> bool:
        return self._started and bool(self._handles)

    @property
    def registered(self) -> list[str]:
        return [self.combo(n) for n in self._actions if self.combo(n)]
