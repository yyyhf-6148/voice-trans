"""应用入口：采集 -> 云端翻译 -> 悬浮窗 + 系统托盘。

启动顺序（每一步都影响首句延迟，顺序有讲究）
-------------------------------------------
1. `load_config()`             —— 解析 .env / 校验配置，失败立即退出
2. `setup_logging()`           —— 之后所有日志可落盘
3. `audio.warmup()`            —— 提前导入 soundcard（约 0.4s），
                                  把开销从"首块音频"挪到启动阶段
4. 创建悬浮窗并 show()          —— 先让窗口出现，用户有反馈
5. `pipeline.setup()`          —— 流式模式下只是实例化，不建连
6. `pipeline.start()`          —— 建 WebSocket（1~2s）+ 启动采集
7. `hotkeys.start()`           —— 注册全局热键
8. 托盘图标                     —— 提供鼠标可达的设置/退出入口

⚠️ 为什么建连要在 UI 之后：建连是阻塞的（约 1.5s），
   放前面会让悬浮窗迟迟不出现，用户以为程序没启动。

用法:
    .venv\\Scripts\\python.exe -m src.app
    .venv\\Scripts\\python.exe -m src.app --no-overlay   # 只跑日志（无窗口）
    .venv\\Scripts\\python.exe -m src.app --no-tray      # 不显示托盘
    .venv\\Scripts\\python.exe -m src.app --no-overlay --duration 5   # 跑 5 秒自检
    .venv\\Scripts\\python.exe -m src.app --duration 5   # 带窗口跑 5 秒（验 Qt 能不能起来）
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .audio.capture import warmup
from .config import (
    PROJECT_ROOT,
    child_process_env,
    is_frozen,
    load_config,
)
from .config_writer import apply_env_updates, apply_yaml_updates
from .errors import AppError
from .logging_setup import get_bound_logger, setup_logging
from .pipeline import Pipeline, TranslationTextEvent

log = get_bound_logger(__name__)

# 内部自检（`--selftest restart`）用来在父子进程之间传递"我是重启出来的子进程"。
_SELFTEST_RESTART_ENV = "_EVEV_SELFTEST_RESTART"


class App:
    """把流水线、悬浮窗、托盘、热键粘在一起的顶层对象。"""

    def __init__(
        self,
        cfg,
        show_overlay: bool = True,
        extra_argv: list[str] | None = None,
        duration: float = 0.0,
        selftest: str = "",
    ) -> None:
        self.cfg = cfg
        self.show_overlay = show_overlay and cfg.overlay.enabled
        # 重启时原样带回的参数
        self.extra_argv = list(extra_argv or [])
        # 无窗口模式的运行时长（秒），0 = 直到 Ctrl+C
        self.duration = float(duration or 0.0)
        # 内部自检（`--selftest <name>`）：验证打包后最容易崩、又最难手工复现的路径
        self.selftest = str(selftest or "").strip().lower()

        self.overlay = None
        self.hotkeys = None
        self.tray = None
        self.pipeline: Pipeline | None = None
        self._qt_app = None
        self._shutting_down = False

    # ---------------- 启动 ----------------
    def run(self) -> int:
        # ---- Qt 应用（必须先创建，再建窗口）----
        if self.show_overlay:
            from PySide6.QtWidgets import QApplication

            from .ui.overlay import OverlayWindow

            self._qt_app = QApplication.instance() or QApplication(sys.argv)
            self._qt_app.setQuitOnLastWindowClosed(False)  # 关窗不等于退出

            self.overlay = OverlayWindow(self.cfg.overlay)
            self.overlay.show()
            self.overlay.push_status("正在连接翻译服务…")
            self._setup_tray()

        # ---- 内部自检（`--selftest <name>`）----
        # 这两条路都不在阶段 A~D 的覆盖范围里，却都是用户真会走到的：
        #   restart  → "保存并应用 → 重启"，实机崩过（见 _run_selftest_restart）
        #   settings → 首启引导"打开后直接填 Key 就能用"的必经之路
        if self.selftest == "restart":
            return self._run_selftest_restart()
        if self.selftest == "settings":
            return self._run_selftest_settings()
        if self.selftest:
            log.error("未知的自检项", name=self.selftest)
            return 1

        # ---- 首次使用：没配 API Key 就别白建连，直接把设置窗口推到面前 ----
        if self._api_key_missing():
            return self._run_first_time_setup()

        # ---- 音频子系统预热 ----
        w = warmup()
        log.debug("音频预热", seconds=round(w, 3))

        # ---- 流水线 ----
        self.pipeline = Pipeline(
            self.cfg,
            on_text=self._on_text,
            on_translation=self._on_translation,
            on_error=self._on_error,
        )
        # 断线自动重连的生命周期回调。
        # ⚠️ 它们由 Pipeline 的**守护线程**调用（不是 Qt 主线程）——
        #    实现里只能用线程安全的通道：overlay.push_*（内部加锁 + 信号）
        #    与 tray.set_state（与既有 _on_error 同一条路径）。
        self.pipeline.on_connection_lost = self._on_connection_lost
        self.pipeline.on_reconnecting = self._on_reconnecting
        self.pipeline.on_reconnected = self._on_reconnected
        self.pipeline.on_reconnect_failed = self._on_reconnect_failed

        try:
            self.pipeline.setup()
        except AppError as e:
            log.error("流水线初始化失败", error=str(e), detail=e.detail)
            if self.overlay is not None:
                self.overlay.push_error(f"初始化失败: {e.message}")
                self._fatal_banner(str(e))
            return 3

        try:
            self.pipeline.start()
        except AppError as e:
            log.error("启动失败", error=str(e), detail=e.detail)
            if self.overlay is not None:
                self.overlay.push_error(f"连接失败: {e.message}")
                if e.detail:
                    for line in str(e.detail).splitlines():
                        self.overlay.push_error(line)
                self._fatal_banner(str(e))
            else:
                return 4
        else:
            info = self.pipeline.asr_info
            log.info("服务已就绪", provider=info)
            if self.overlay is not None:
                self.overlay.push_status(
                    f"已连接 · {self.cfg.asr.livetranslate.model}"
                    f" · 连接耗时 {self.pipeline.connect_seconds:.1f}s"
                )
            if self.tray is not None:
                self.tray.set_state("ok", f"实时音频翻译 · 已连接（{info}）")
                self.tray.notify(
                    "实时音频翻译",
                    f"已就绪 · 连接耗时 {self.pipeline.connect_seconds:.1f}s",
                )

        # ---- 全局热键 ----
        self._setup_hotkeys()

        if self._qt_app is None:
            # 无窗口模式：命令行阻塞运行
            return self._run_headless(self.duration)

        # 定时保存退出标志，配合 aboutToQuit
        self._qt_app.aboutToQuit.connect(self.shutdown)
        log.info(
            "进入事件循环",
            hotkeys=self.hotkeys.registered if self.hotkeys else [],
            tray="on" if self.tray else "off",
        )
        if self.duration > 0:
            # ⚠️ `--duration` 在**窗口模式**下也必须生效。
            #    打包产物冒烟测试全程用 `--no-overlay`，而那条路**根本不 import
            #    PySide6** —— "冻结环境里 Qt 平台插件能不能加载"（PyInstaller +
            #    PySide6 的经典翻车点）就完全没被验证过，偏偏用户双击 exe 走的
            #    正是窗口模式。有了这个定时退出，才能无人值守地把这条路也跑一遍。
            from PySide6.QtCore import QTimer

            log.info("窗口模式限时运行", seconds=self.duration)
            QTimer.singleShot(int(self.duration * 1000), self._qt_app.quit)
        return self._qt_app.exec()

    # ---------------- 内部自检：重启路径 ----------------
    def _run_selftest_restart(self) -> int:
        """验证"重启自身"在打包环境里不崩（父进程真的重启一次）。

        - **父进程**：完整走一遍 `_restart()`，把子进程拉起来后**立刻退出**
          （用 `os._exit` 跳过 Qt 清理）——父进程一退出，最外层 PyInstaller
          引导器就会删掉 `_MEIxxxxxx`，这正是崩溃的触发条件。
        - **子进程**：能看到这条日志，就说明 `PySide6.QtWidgets` / 悬浮窗 /
          托盘在"重启出来的进程"里全部导入成功，即修复生效。

        必须走**窗口模式**（不要加 `--no-overlay`）：只有这条路才会 import
        PySide6，而无窗口模式根本不碰它。
        """
        if self._qt_app is None:
            log.error("重启自检必须运行在窗口模式下（不要加 --no-overlay）")
            return 1
        meipass = str(getattr(sys, "_MEIPASS", ""))
        stage = os.environ.get(_SELFTEST_RESTART_ENV, "")

        if stage == "child":
            log.info(
                "SELFTEST-RESTART-CHILD-OK",
                meipass=meipass,
                parent_level=os.environ.get("_PYI_PARENT_PROCESS_LEVEL", ""),
            )
            if self.tray is not None:
                self.tray.set_state("ok", "重启自检通过")
            from PySide6.QtCore import QTimer

            QTimer.singleShot(600, self._qt_app.quit)
            return self._qt_app.exec()

        log.info("SELFTEST-RESTART-PARENT", meipass=meipass)
        self._restart(extra_env={_SELFTEST_RESTART_ENV: "child"})
        os._exit(0)          # 父进程立刻消失，触发引导器删除 _MEI 目录

    def _run_selftest_settings(self) -> int:
        """验证**设置窗口**在冻结环境里能构造出来。

        首启引导（"打开后直接填 API Key 就能用"）走的正是这条路：
        `_run_first_time_setup()` → `_open_settings()` → `SettingsDialog`。
        它比悬浮窗/托盘多 import 一大票 QtWidgets 控件，一旦被 `trans.spec` 的
        `excludes` 误伤（或漏了 hiddenimport），用户会在**第一次运行**就崩 ——
        而那条路任何其它阶段都不走。
        """
        from PySide6.QtCore import QTimer

        from .ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(self.cfg, None)
        tabs = getattr(dlg, "_tabs", None)
        names = [tabs.tabText(i) for i in range(tabs.count())] if tabs else []
        log.info(
            "SELFTEST-SETTINGS-OK",
            pages=len(names),
            tab_names=" / ".join(names),
            fields=len(getattr(dlg, "_fields", None) or {}),
        )
        dlg.deleteLater()
        QTimer.singleShot(300, self._qt_app.quit)
        return self._qt_app.exec()

    # ---------------- 托盘 ----------------
    def _setup_tray(self) -> None:
        if not self.cfg.tray.enabled:
            log.info("托盘已按配置关闭")
            return

        from .ui.tray import TrayIcon

        tray = TrayIcon()
        tray.action_triggered.connect(self._on_tray_action)
        tray.set_overlay_visible(True)
        tray.set_click_through(self.cfg.overlay.click_through)

        if tray.show():
            tray.set_state("idle", "实时音频翻译 · 正在连接…")
            self.tray = tray
        else:
            # 系统不支持托盘时不留引用，避免误以为可用
            tray.deleteLater()

    def _on_tray_action(self, name: str) -> None:
        """托盘菜单动作分发（全部在 Qt 主线程）。"""
        log.debug("托盘动作", action=name)

        if name == "quit":
            self._request_quit()
        elif name == "settings":
            self._open_settings()
        elif name == "reconnect":
            self._reconnect()
        elif name == "restart":
            self._restart()
        elif name == "clear":
            if self.overlay is not None:
                self.overlay.clear_items()
        elif name == "toggle_visibility":
            self._toggle_overlay()
        elif name == "toggle_click_through":
            self._toggle_click_through()
        elif name == "open_config":
            self._open_path(self.cfg.source_path)
        elif name == "open_logs":
            self._open_path(self.cfg.log_dir)
        else:
            log.warning("未知的托盘动作", action=name)

    # ---------------- 悬浮窗动作（热键与托盘共用） ----------------
    def _toggle_overlay(self) -> None:
        if self.overlay is None:
            return
        visible = self.overlay.toggle_visibility()
        if self.tray is not None:
            self.tray.set_overlay_visible(visible)
        log.info("悬浮窗显示切换", visible=visible)

    def _toggle_click_through(self) -> None:
        if self.overlay is None:
            return
        enabled = self.overlay.toggle_click_through()
        if self.tray is not None:
            self.tray.set_click_through(enabled)
        log.info("点击穿透", enabled=enabled)

    # ---------------- 热键 ----------------
    def _setup_hotkeys(self) -> None:
        from .ui.hotkeys import HotkeyManager

        # 支持热重载：先注销旧的，否则会重复注册
        if self.hotkeys is not None:
            try:
                self.hotkeys.stop()
            except Exception as e:
                log.warning("注销旧热键失败", error=str(e))
            self.hotkeys = None

        mgr = HotkeyManager(self.cfg.hotkeys)

        # 悬浮窗可能不存在（--no-overlay），此时热键只保留退出
        #
        # ⚠️ 热键回调运行在 keyboard 的监听线程上。
        #    overlay 内部对此已做线程安全处理（队列 + Qt 信号），
        #    所以这里可以直接调用其方法，但**不要**再往里加 Qt 控件操作。
        if self.overlay is not None:
            o = self.overlay

            def op_up() -> None:
                v = o.adjust_opacity(+0.05)
                log.info("不透明度", value=round(v, 2))

            def op_down() -> None:
                v = o.adjust_opacity(-0.05)
                log.info("不透明度", value=round(v, 2))

            def recenter() -> None:
                o.refresh_position()
                log.info("悬浮窗已回到配置位置")

            mgr.bind("toggle_visibility", self._toggle_overlay)
            mgr.bind("opacity_up", op_up)
            mgr.bind("opacity_down", op_down)
            mgr.bind("toggle_click_through", self._toggle_click_through)
            if self.cfg.hotkeys.recenter:
                mgr.bind("recenter", recenter)

        mgr.bind("quit", self._request_quit)

        n = mgr.start()
        if n == 0:
            log.warning(
                "未注册任何全局热键",
                detail="检查 keyboard 是否安装，以及 hotkeys 配置是否合法",
            )
        self.hotkeys = mgr

    # ---------------- 首次使用引导 ----------------
    def _api_key_missing(self) -> bool:
        """当前后端所需的 API Key 是否还没配。"""
        env_name = self.cfg.asr.livetranslate.api_key_env
        return not (env_name and os.environ.get(env_name))

    def _run_first_time_setup(self) -> int:
        """没配 API Key 时的启动路径：不建连，直接把设置窗口推给用户。

        直接建连只会抛 MissingApiKeyError，用户看到的是一堆红字，
        却不知道该去哪儿填。不如把设置窗口直接弹出来 ——
        打包成 exe 后尤其重要：没有控制台，stderr 是 None，报错根本看不见。
        """
        env_name = self.cfg.asr.livetranslate.api_key_env
        log.warning(
            "未配置 API Key，改为引导填写",
            env=env_name,
            hint="设置窗口 → 接口与密钥 → API Key",
        )

        if self.overlay is not None:
            self.overlay.push_status(
                f"尚未配置 API Key（{env_name}）—— 请在设置窗口填入后重启"
            )
        if self.tray is not None:
            self.tray.set_state("error", "实时音频翻译 · 未配置 API Key")
            self.tray.notify(
                "需要配置 API Key",
                "设置窗口已打开，粘贴 Key 后点「保存并应用」即可",
            )

        self._setup_hotkeys()

        if self._qt_app is None:
            # 无窗口模式没有设置界面可弹，只能失败退出
            log.error("无窗口模式下无法引导填写 API Key")
            return 2

        from PySide6.QtCore import QTimer

        # 延后一点，等悬浮窗先画出来
        QTimer.singleShot(300, self._open_settings)

        self._qt_app.aboutToQuit.connect(self.shutdown)
        return self._qt_app.exec()

    # ---------------- 设置 ----------------
    def _open_settings(self) -> None:
        if self._qt_app is None:
            return

        from PySide6.QtWidgets import QDialog

        from .ui.settings_dialog import SettingsDialog

        dlg = SettingsDialog(self.cfg, None)
        if dlg.exec() != QDialog.Accepted:
            return

        yaml_up = dlg.yaml_updates
        env_up = dlg.env_updates
        if not yaml_up and not env_up:
            self._toast("设置", "没有任何改动")
            return

        try:
            if env_up:
                # 密钥只能进 .env（config.yaml 未进 .gitignore）
                apply_env_updates(self.cfg.source_path.parent / ".env", env_up)
            if yaml_up:
                apply_yaml_updates(self.cfg.source_path, yaml_up)
        except Exception as e:
            log.error("保存配置失败", error=str(e))
            self._toast("保存失败", str(e))
            return

        log.info(
            "设置已保存",
            yaml_keys=len(yaml_up),
            env_keys=len(env_up),
            need_restart=dlg.restart_required,
        )
        self._reload_config(dlg.hot_keys, dlg.restart_required)

    def _reload_config(self, hot_keys: set[str], restart_required: bool) -> None:
        """重新读取配置并热应用能热改的部分。"""
        try:
            new_cfg = load_config(self.cfg.source_path)
        except AppError as e:
            log.error("重载配置失败", error=str(e))
            self._toast("配置校验失败", f"{e.message}（改动已写入文件但未生效）")
            return

        self.cfg = new_cfg
        if self.pipeline is not None:
            self.pipeline.cfg = new_cfg

        if any(k.startswith("overlay.") for k in hot_keys):
            if self.overlay is not None:
                self.overlay.apply_config(new_cfg.overlay)
            if self.tray is not None:
                self.tray.set_click_through(new_cfg.overlay.click_through)

        if any(k.startswith("hotkeys.") for k in hot_keys):
            self._setup_hotkeys()

        if restart_required:
            self._ask_restart()
        else:
            self._toast("设置已应用", "改动已立即生效")

    def _ask_restart(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox()
        box.setWindowTitle("需要重启")
        box.setIcon(QMessageBox.Information)
        box.setText("部分设置需要重启程序才能生效。")
        box.setInformativeText(
            "识别后端 / 音频设备 / API Key 属于启动时初始化项。\n现在重启吗？"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.Yes)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        if box.exec() == QMessageBox.Yes:
            self._restart()
        else:
            self._toast("设置已保存", "重启程序后生效")

    # ---------------- 重连 / 重启 / 打开路径 ----------------
    def _reconnect(self) -> None:
        """托盘「重新连接」：只发请求，真正的重连在守护线程里做。

        ⚠️ 老实现在这里**同步**跑 `pipeline.reconnect()`，而重连内部
        `_start_stream()` → `live.start()` 最长阻塞 15s（连接超时）——
        点一下「重新连接」界面就卡死十几秒（实测问题）。
        """
        if self.pipeline is None:
            return
        if self.tray is not None:
            self.tray.set_state("idle", "实时音频翻译 · 正在重连…")
        try:
            self.pipeline.request_reconnect()
        except Exception as e:
            log.error("重新连接请求失败", error=str(e))
            if self.overlay is not None:
                self.overlay.push_error(f"重连失败: {e}")
            if self.tray is not None:
                self.tray.set_state("error", "实时音频翻译 · 重连失败")
            self._toast("重新连接失败", str(e))

    def _restart(self, extra_env: dict[str, str] | None = None) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True

        log.info("正在重启程序…")
        self.shutdown()

        if is_frozen():
            # 打包后 sys.executable 就是本 exe，不能再加 "-m src.app"
            cmd = [sys.executable, *self.extra_argv]
        else:
            cmd = [sys.executable, "-m", "src.app", *self.extra_argv]
        try:
            # ⚠️ 必须传 env=child_process_env()：
            #    onefile exe 里 sys.executable 是 exe 自己，直接 Popen 会让新进程
            #    继承 `_MEIPASS` / `_PYI_PARENT_PROCESS_LEVEL` 等引导器变量 →
            #    复用**即将被父进程退出时删掉**的 `_MEIxxxxxx` 目录 →
            #    新进程 import 到一半文件消失，报
            #    `ModuleNotFoundError: No module named 'PySide6.QtWidgets'`。
            #    剥掉后新进程会重新解压自己的目录，与父进程彻底解耦。
            env = child_process_env()
            if extra_env:
                env.update(extra_env)
            subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env)
        except Exception as e:
            log.error("重启失败", error=str(e))
            self._shutting_down = False
            self._toast("重启失败", str(e))
            return

        if self._qt_app is not None:
            self._qt_app.quit()
        else:
            raise SystemExit(0)

    def _open_path(self, target) -> None:
        if target is None:
            return
        p = Path(target)
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        if not p.exists():
            self._toast("路径不存在", str(p))
            return

        try:
            if sys.platform == "win32":
                os.startfile(str(p))          # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:
            log.error("打开路径失败", path=str(p), error=str(e))
            self._toast("打开失败", str(e))

    def _toast(self, title: str, body: str) -> None:
        if self.tray is not None:
            self.tray.notify(title, body)
        log.info("提示", title=title, body=body)

    # ---------------- 回调 ----------------
    def _on_translation(self, ev: TranslationTextEvent) -> None:
        """流式翻译结果（运行在网络线程）→ 转给悬浮窗。"""
        if self.overlay is None:
            if ev.is_displayable and not ev.is_partial:
                log.info("翻译", zh=ev.translation, en=ev.original or None)
            return

        if ev.kind == "error":
            self.overlay.push_error(ev.translation)
            return

        if not ev.is_displayable:
            return

        if ev.kind == "original":
            self.overlay.push_original(
                ev.original, is_partial=ev.is_partial, sequence=ev.sequence,
                language=ev.language, emotion=ev.emotion,
            )
            return

        self.overlay.push_translation(
            ev.translation,
            original=ev.original,
            is_partial=ev.is_partial,
            sequence=ev.sequence,
            first_text_delay_ms=ev.first_text_delay_ms,
            language=ev.language,
            emotion=ev.emotion,
        )

    def _on_text(self, ev) -> None:
        """切句-识别模式的输出（local/cloud provider 用）。"""
        if ev.is_partial and self.overlay is None:
            return
        if self.overlay is None:
            print(f"[{ev.sequence:03d}] {ev.text}")
            return
        self.overlay.push_translation(
            ev.text, is_partial=ev.is_partial, sequence=ev.sequence
        )

    def _on_error(self, exc: Exception) -> None:
        log.error("流水线错误", error=str(exc))
        if self.overlay is not None:
            self.overlay.push_error(str(exc))
        if self.tray is not None:
            self.tray.set_state("error", f"实时音频翻译 · 出错：{exc}")

    # ---------------- 断线 / 自动重连（守护线程回调） ----------------
    #
    # 背景：服务端偶发 `model repeat output happened`（COMMON_ERROR）后以
    # WebSocket close code 1011 掐断会话。老实现断线后既不自愈、又疯狂刷
    # 「音频推送失败」日志（实测 15,482 条 / 4.67MB），只能手动点托盘重连。
    # 现在 Pipeline 的守护线程负责退避重连，这里只更新 UI。
    def _on_connection_lost(self, reason: str) -> None:
        log.warning("连接断开", reason=reason)
        if self.overlay is not None:
            self.overlay.push_status("连接已断开，正在自动重连…")
        if self.tray is not None:
            self.tray.set_state("idle", "实时音频翻译 · 连接断开，重连中…")

    def _on_reconnecting(self, attempt: int, delay: float) -> None:
        if self.tray is not None:
            wait = f"，{delay:.0f}s 后" if delay > 0 else ""
            self.tray.set_state(
                "idle",
                f"实时音频翻译 · 重连中（第 {attempt} 次{wait}）",
            )

    def _on_reconnected(self, attempt: int) -> None:
        if self.tray is not None:
            self.tray.set_state("ok", "实时音频翻译 · 已重连")
        if self.overlay is not None:
            self.overlay.push_status(
                "已重新连接" if attempt <= 1
                else f"已重新连接（第 {attempt} 次尝试）"
            )
        # attempt == 0 表示"会话到期主动轮换"，属于计划内行为，不弹提示
        if attempt > 0:
            self._toast("已重新连接", f"第 {attempt} 次尝试成功")

    def _on_reconnect_failed(self, attempts: int) -> None:
        log.error("自动重连失败", attempts=attempts)
        if self.overlay is not None:
            self.overlay.push_error(
                f"自动重连失败（已尝试 {attempts} 次），"
                "请检查网络后点托盘「重新连接」"
            )
        if self.tray is not None:
            self.tray.set_state("error", "实时音频翻译 · 自动重连失败")
        self._toast("自动重连失败", "请检查网络后点托盘「重新连接」")

    # ---------------- 关闭 ----------------
    def _request_quit(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        log.info("收到退出请求")
        if self.tray is not None:
            self.tray.hide()
        if self._qt_app is not None:
            self._qt_app.quit()
        else:
            self.shutdown()

    def shutdown(self) -> None:
        if self.pipeline is None:
            return
        log.info("正在关闭…")
        try:
            if self.hotkeys is not None:
                self.hotkeys.stop()
            self.pipeline.stop()
        except Exception as e:
            log.error("关闭时异常", error=str(e))
        finally:
            self.pipeline = None

    def _fatal_banner(self, msg: str) -> None:
        log.error("启动失败，窗口保留以便查看错误", error=msg)
        if self.tray is not None:
            self.tray.set_state("error", f"实时音频翻译 · 启动失败：{msg}")

    def _run_headless(self, duration: float = 0.0) -> int:
        """无窗口模式：阻塞到 Ctrl+C（或跑满 `duration` 秒）。

        `duration > 0` 时到点自动退出 —— 打包后的 exe 靠它做无人值守冒烟测试。
        """
        if duration > 0:
            log.info("无窗口模式运行中", seconds=duration)
        else:
            log.info("无窗口模式运行中，Ctrl+C 结束")
        stop = {"v": False}

        def _sig(_s, _f) -> None:
            stop["v"] = True

        try:
            signal.signal(signal.SIGINT, _sig)
        except Exception:
            pass

        deadline = time.time() + duration if duration > 0 else None
        try:
            while not stop["v"]:
                if deadline is not None and time.time() >= deadline:
                    break
                time.sleep(0.3)
        except (KeyboardInterrupt, EOFError):
            pass
        self.shutdown()
        return 0


def _show_fatal_dialog(title: str, text: str) -> None:
    """弹一个致命错误对话框。

    打包成 windowed exe 后没有控制台，`print(..., file=sys.stderr)` 是**静默无效**的
    （`sys.stderr` 为 None）。启动失败必须弹窗，否则用户看到的是"双击没反应"。
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QMessageBox

        QApplication.instance() or QApplication(sys.argv)
        box = QMessageBox()
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Critical)
        box.setText(text)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.exec()
    except Exception:      # pragma: no cover - 连弹窗都失败就无能为力了
        pass


def _install_excepthook() -> None:
    """兜住未捕获异常：写日志 + 弹窗。

    默认的 excepthook 往 stderr 打印，而打包后 stderr 是 None ——
    程序会"无声无息地消失"。这里改成落日志 + 弹窗。
    """

    def _hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log.critical("未捕获异常", error=str(exc), exc_info=(exc_type, exc, tb))
        _show_fatal_dialog(
            "程序出错",
            f"{exc_type.__name__}: {exc}\n\n详细日志见程序目录下的 logs\\app.log",
        )

    sys.excepthook = _hook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实时音频翻译悬浮窗")
    parser.add_argument("--config", "-c", default=None, help="配置文件路径")
    parser.add_argument("--no-overlay", action="store_true",
                        help="不显示悬浮窗，只输出日志")
    parser.add_argument("--duration", "-d", type=float, default=0.0,
                        help="运行时长（秒），到点自动退出；0=一直运行"
                             "（无窗口模式下 Ctrl+C 结束）")
    parser.add_argument("--no-tray", action="store_true",
                        help="不显示系统托盘图标")
    # 内部自检开关：不在 --help 里露出来，供打包产物冒烟测试无人值守地跑
    # 用户真会走到、但其它阶段覆盖不到的路径（见 App.run 里的 selftest 分支）。
    parser.add_argument("--selftest", choices=["restart", "settings"],
                        default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if is_frozen():
        _install_excepthook()

    try:
        cfg = load_config(args.config)
    except AppError as e:
        lines = [f"[配置错误] {e}"]
        if e.detail:
            lines.append(f"  {e.detail}")
        msg = "\n".join(lines)
        print(msg, file=sys.stderr)
        if is_frozen():
            _show_fatal_dialog("启动失败", msg)
        return 2

    if args.no_tray:
        cfg.tray.enabled = False

    setup_logging(cfg.app.log_level, cfg.log_dir)
    # 托盘依附于 Qt 应用，无窗口模式下不会创建
    tray_on = cfg.tray.enabled and not args.no_overlay
    log.info(
        "启动",
        provider=cfg.asr.provider,
        overlay="on" if (cfg.overlay.enabled and not args.no_overlay) else "off",
        tray="on" if tray_on else "off",
        cwd=str(Path.cwd()),
    )

    # 重启时原样带回的参数
    extra: list[str] = []
    if args.no_overlay:
        extra.append("--no-overlay")
    if args.no_tray:
        extra.append("--no-tray")
    if args.selftest:
        # 子进程也要带这个开关，靠环境变量区分父子身份
        extra += ["--selftest", args.selftest]

    app = App(
        cfg,
        show_overlay=not args.no_overlay,
        extra_argv=extra,
        duration=args.duration,
        selftest=args.selftest,
    )
    try:
        return app.run()
    except AppError as e:
        log.error("运行失败", error=str(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
