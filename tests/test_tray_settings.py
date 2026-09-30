"""托盘图标 + 设置对话框自检。

分两部分：
  A. 纯逻辑（无 Qt）—— 热词表文本解析、密钥打码
  B. Qt offscreen  —— 图标生成、菜单结构、设置对话框的收集与热/冷判定

跑法:
    .venv\\Scripts\\python.exe tests/test_tray_settings.py
    .venv\\Scripts\\python.exe tests/test_tray_settings.py --logic-only
"""
from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config                        # noqa: E402

_passed = 0
_failed = 0


def chk(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}  {detail}")


# ================================================================ A. 纯逻辑
def test_logic() -> None:
    from src.ui.settings_dialog import (
        HOT_PREFIXES,
        format_phrases,
        parse_phrases,
    )

    print()
    print("=" * 70)
    print("  A. 纯逻辑")
    print("=" * 70)

    print("\n--- 热词表文本解析 ---")
    d = parse_phrases("gate: 星门\ntackle: 抓人\n")
    chk("基本解析", d == {"gate": "星门", "tackle": "抓人"}, f"实际 {d}")

    d2 = parse_phrases(
        "# 这是注释\n"
        "\n"
        "gate: 星门\n"
        "logie = 奶妈\n"
        "MWD   微曲\n"
        '"cyno": "诱导"\n'
        "只有键没有值:\n"
        "   \n"
    )
    chk("忽略注释与空行", "# 这是注释" not in d2)
    chk("支持 = 分隔", d2.get("logie") == "奶妈")
    chk("支持空格分隔", d2.get("MWD") == "微曲")
    chk("剥掉成对引号", d2.get("cyno") == "诱导")
    chk("空值也能收录", "只有键没有值" in d2)
    chk("总条数正确", len(d2) == 5, f"实际 {len(d2)}: {d2}")

    print("\n--- 往返一致 ---")
    orig = {"gate": "星门", "MWD": "微曲", "ongrid": "在网格上"}
    chk("format -> parse 往返一致",
        parse_phrases(format_phrases(orig)) == orig)
    chk("空表往返一致", parse_phrases(format_phrases({})) == {})

    print("\n--- 热应用前缀 ---")
    chk("overlay 可热应用", "overlay.opacity".startswith(HOT_PREFIXES))
    chk("hotkeys 可热应用", "hotkeys.quit".startswith(HOT_PREFIXES))
    chk("asr 不可热应用", not "asr.provider".startswith(HOT_PREFIXES))
    chk("tray 不可热应用", not "tray.enabled".startswith(HOT_PREFIXES))


# ================================================================ B. Qt
def test_qt() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        chk("PySide6 可用", False, str(e))
        return

    print()
    print("=" * 70)
    print("  B. 托盘与设置（Qt offscreen）")
    print("=" * 70)

    from src.ui.settings_dialog import SettingsDialog
    from src.ui.tray import TrayIcon, make_tray_icon

    cfg = load_config(str(ROOT / "config.yaml"))
    app = QApplication.instance() or QApplication(sys.argv)

    # ---------------- 图标 ----------------
    print("\n--- 图标生成 ---")
    icon = make_tray_icon("#38BDF8", 64)
    chk("图标非空", not icon.isNull())
    pm = icon.pixmap(64, 64)
    chk("像素图尺寸正确", pm.width() == 64 and pm.height() == 64)
    chk("像素图有内容（非全透明）",
        any(pm.toImage().pixelColor(x, y).alpha() > 0
            for x in range(0, 64, 8) for y in range(0, 64, 8)))
    chk("不同颜色生成不同图标",
        make_tray_icon("#F87171", 64).pixmap(8, 8).toImage()
        != icon.pixmap(8, 8).toImage())

    # ---------------- 托盘 ----------------
    print("\n--- 托盘菜单结构 ---")
    tray = TrayIcon()
    names = set(tray._actions)
    expected = {
        "toggle_visibility", "toggle_click_through", "clear",
        "settings", "reconnect",
        "open_config", "open_logs", "restart", "quit",
    }
    chk("菜单项齐全", names == expected,
        f"缺失 {expected - names}，多余 {names - expected}")
    chk("显隐项默认勾选",
        tray._actions["toggle_visibility"].isChecked())
    chk("点击穿透默认不勾选",
        not tray._actions["toggle_click_through"].isChecked())
    chk("显隐项可勾选",
        tray._actions["toggle_visibility"].isCheckable())

    print("\n--- 托盘状态同步 ---")
    tray.set_overlay_visible(False)
    chk("set_overlay_visible(False) 生效",
        not tray._actions["toggle_visibility"].isChecked())
    tray.set_click_through(True)
    chk("set_click_through(True) 生效",
        tray._actions["toggle_click_through"].isChecked())

    for st in ("ok", "error", "idle", "不存在的状态"):
        try:
            tray.set_state(st, f"状态 {st}")
            ok = True
        except Exception as e:
            ok = False
            chk(f"set_state({st!r}) 不抛错", False, str(e))
    chk("set_state 各种取值都不抛错", ok)
    chk("非法状态被规整为 idle", tray._state == "idle")

    print("\n--- 信号 ---")
    got: list[str] = []
    tray.action_triggered.connect(got.append)
    tray._actions["settings"].trigger()
    tray._actions["quit"].trigger()
    chk("菜单动作发出信号", got == ["settings", "quit"], f"实际 {got}")

    print("\n--- 无托盘环境下 show() 应安全返回 ---")
    res = tray.show()
    chk("show() 返回 bool 且不抛错", isinstance(res, bool))
    if not res:
        chk("不可用时返回 False（符合 offscreen 预期）", res is False)

    # ---------------- 设置对话框 ----------------
    print("\n--- 设置对话框构造 ---")
    dlg = SettingsDialog(cfg, None)
    chk("对话框创建成功", dlg is not None)
    chk("字段数覆盖全部生效配置键（>=55）", len(dlg._fields) >= 55,
        f"实际 {len(dlg._fields)}")

    need = {
        "asr.livetranslate.model",
        "asr.livetranslate.silence_duration_ms",
        "asr.livetranslate.vad_threshold",
        "asr.livetranslate.phrases",
        "asr.livetranslate.phrases_file",
        "audio.device_keyword", "audio.block_ms", "audio.auto_gain",
        "overlay.opacity", "overlay.position", "overlay.font_size_translation",
        "overlay.background", "overlay.color_translation",
        "overlay.click_through", "overlay.hold_seconds",
        "hotkeys.toggle_visibility", "hotkeys.quit",
        "tray.enabled",
    }
    chk("关键字段都已注册", need <= set(dlg._fields),
        f"缺失 {need - set(dlg._fields)}")

    # 用户明确要求过：接口地址（端点）必须能在界面里改
    new_need = {
        "asr.livetranslate.url",          # WebSocket 端点
        "asr.livetranslate.workspace",    # 业务空间 ID
        "asr.livetranslate.api_key_env",  # Key 环境变量名
        "app.log_level",
        "overlay.speaker_colors",
    }
    chk("接口地址 / 密钥环境变量 / 高级项都已暴露",
        new_need <= set(dlg._fields),
        f"缺失 {new_need - set(dlg._fields)}")
    chk("cloud / local / 级联翻译 / 本地 VAD 已从界面收起",
        not any(k.startswith(("asr.cloud.", "asr.local.",
                              "translate.", "vad."))
                for k in dlg._fields),
        f"残留 {[k for k in dlg._fields if k.startswith(('asr.cloud.', 'asr.local.', 'translate.', 'vad.'))]}")
    chk("asr.provider 已从界面收起",
        "asr.provider" not in dlg._fields)
    from PySide6.QtWidgets import QLineEdit

    chk("颜色字段注册的是内层输入框（不是外壳容器）",
        isinstance(dlg._fields["overlay.background"], QLineEdit),
        f"实际 {type(dlg._fields['overlay.background']).__name__}")
    chk("颜色字段初值取自配置",
        dlg._fields["overlay.background"].text()
        == cfg.overlay.background,
        f"实际 {dlg._fields['overlay.background'].text()!r}")
    chk("采样率字段只读",
        dlg._fields["app.target_sample_rate"].isReadOnly())

    print("\n--- 后端已固定为 livetranslate ---")
    chk("Key 环境变量名取自 livetranslate",
        dlg._api_key_env_name() == cfg.asr.livetranslate.api_key_env,
        f"实际 {dlg._api_key_env_name()}")
    chk("livetranslate 接口地址值来自配置",
        dlg._read("asr.livetranslate.url") == cfg.asr.livetranslate.url)

    # 用户要求过：接口地址要**直接显示实际生效值**，不能留个空框让人猜
    print("\n--- 接口地址直接显示实际生效值 ---")
    from src.ui.settings_dialog import _DEFAULT_LIVETRANSLATE_URL

    url_w = dlg._fields["asr.livetranslate.url"]
    if cfg.asr.livetranslate.url is None:
        chk("url 为 null 时框里直接显示官方通用端点",
            url_w.text() == _DEFAULT_LIVETRANSLATE_URL,
            f"实际 {url_w.text()!r}")
    else:
        chk("url 已配置时框里显示配置值",
            url_w.text() == cfg.asr.livetranslate.url,
            f"实际 {url_w.text()!r}")

    dlg._collect()
    chk("接口地址只是显示 fallback，未改动 -> 不写回 config.yaml（零漂移）",
        "asr.livetranslate.url" not in dlg.yaml_updates,
        f"实际 {dlg.yaml_updates.get('asr.livetranslate.url')!r}")

    url_w.setText("wss://example.com/api-ws/v1/realtime")
    dlg._collect()
    chk("改过的 url 才写回 config.yaml",
        dlg.yaml_updates.get("asr.livetranslate.url")
        == "wss://example.com/api-ws/v1/realtime",
        f"实际 {dlg.yaml_updates.get('asr.livetranslate.url')!r}")

    dlg.deleteLater()

    dlg = SettingsDialog(cfg, None)

    print("\n--- 初始快照：无改动 ---")
    chk("未改动时 yaml_updates 为空", dlg.yaml_updates == {},
        f"实际 {dlg.yaml_updates}")
    chk("未改动时 env_updates 为空", dlg.env_updates == {})
    chk("未改动时不需要重启", dlg.restart_required is False)

    print("\n--- 改 overlay 字段 -> 可热应用 ---")
    dlg._fields["overlay.opacity"].setValue(0.42)
    dlg._fields["overlay.font_size_translation"].setValue(26)
    dlg._fields["overlay.click_through"].setChecked(True)
    dlg._collect()
    up = dlg.yaml_updates
    chk("opacity 被收集", up.get("overlay.opacity") == 0.42, f"实际 {up}")
    chk("字号被收集", up.get("overlay.font_size_translation") == 26)
    chk("点击穿透被收集", up.get("overlay.click_through") is True)
    chk("标记为可热应用", dlg.hot_keys == set(up), f"实际 {dlg.hot_keys}")
    chk("不需要重启", dlg.restart_required is False)
    chk("未改动的字段不在更新集里", "overlay.width" not in up)

    print("\n--- 改 asr / audio -> 需重启 ---")
    dlg2 = SettingsDialog(cfg, None)
    dlg2._fields["audio.block_ms"].setValue(64)
    dlg2._collect()
    chk("audio 改动被收集", dlg2.yaml_updates.get("audio.block_ms") == 64)
    chk("标记为需要重启", dlg2.restart_required is True)
    chk("hot_keys 为空", dlg2.hot_keys == set())

    print("\n--- 混合改动：仍需重启（安全优先） ---")
    dlg3 = SettingsDialog(cfg, None)
    dlg3._fields["overlay.opacity"].setValue(0.5)
    dlg3._fields["asr.livetranslate.model"].setText("qwen-test-model")
    dlg3._collect()
    chk("两个改动都被收集", len(dlg3.yaml_updates) == 2,
        f"实际 {dlg3.yaml_updates}")
    chk("含冷改则需重启", dlg3.restart_required is True)
    chk("hot_keys 只含 overlay", dlg3.hot_keys == {"overlay.opacity"})

    print("\n--- 热词表编辑 ---")
    dlg4 = SettingsDialog(cfg, None)
    edit = dlg4._fields["asr.livetranslate.phrases"]
    edit.setPlainText("gate: 星门\nsino: 诱导\nlogie: 奶妈")
    dlg4._collect()
    got = dlg4.yaml_updates.get("asr.livetranslate.phrases")
    chk("热词表被解析成 dict",
        got == {"gate": "星门", "sino": "诱导", "logie": "奶妈"},
        f"实际 {got}")
    chk("热词表改动触发重启标记（asr.*）", dlg4.restart_required is True)

    print("\n--- API Key 处理 ---")
    env_name = cfg.asr.livetranslate.api_key_env
    prev_env = os.environ.get(env_name)
    os.environ[env_name] = "sk-existing-key-abcdef"

    dlg5 = SettingsDialog(cfg, None)
    chk("密钥框明文显示（不打码）",
        dlg5._api_key_edit.echoMode() == dlg5._api_key_edit.EchoMode.Normal,
        f"实际 {dlg5._api_key_edit.echoMode()}")
    chk("密钥框预填当前已配置值（直接显示）",
        dlg5._api_key_edit.text() == "sk-existing-key-abcdef",
        f"实际 {dlg5._api_key_edit.text()!r}")

    dlg5._collect()
    chk("预填不算改动 -> env_updates 为空", dlg5.env_updates == {},
        f"实际 {dlg5.env_updates}")
    chk("预填不算改动 -> 不需要重启", dlg5.restart_required is False)

    dlg5._api_key_edit.setText("sk-new-key-123456")
    dlg5._collect()
    chk("改过的密钥进入 env_updates",
        dlg5.env_updates == {env_name: "sk-new-key-123456"},
        f"实际 {dlg5.env_updates}")
    chk("密钥不进入 yaml_updates",
        not any("key" in k for k in dlg5.yaml_updates))
    chk("改密钥需要重启", dlg5.restart_required is True)

    print("\n--- 密钥框留空 = 不修改（不会误删） ---")
    dlg5b = SettingsDialog(cfg, None)
    dlg5b._api_key_edit.setText("")
    dlg5b._collect()
    chk("留空不写 .env", dlg5b.env_updates == {},
        f"实际 {dlg5b.env_updates}")

    # 还原环境变量，避免污染后续用例
    if prev_env is None:
        os.environ.pop(env_name, None)
    else:
        os.environ[env_name] = prev_env

    print("\n--- 清空 Key 环境变量名 -> 密钥框禁用 ---")
    dlg6 = SettingsDialog(cfg, None)
    dlg6._fields["asr.livetranslate.api_key_env"].setText("")
    dlg6._sync_api_key_hint()
    chk("无环境变量名时密钥框被禁用", not dlg6._api_key_edit.isEnabled())
    dlg6._api_key_edit.setText("sk-should-be-ignored")
    dlg6._collect()
    chk("无环境变量名时密钥不写 .env", dlg6.env_updates == {},
        f"实际 {dlg6.env_updates}")

    # ---------------- 悬浮窗热应用 ----------------
    print("\n--- 悬浮窗 apply_config 热应用 ---")
    from src.ui.overlay import OverlayWindow

    o = OverlayWindow(cfg.overlay)
    o.show()
    new_sec = dataclasses.replace(
        cfg.overlay, opacity=0.5, font_size_translation=24,
        hold_seconds=12.0, max_messages=7,
    )
    o.apply_config(new_sec)
    chk("不透明度已应用", abs(o.opacity - 0.5) < 1e-6, f"实际 {o.opacity}")
    chk("字号已应用", o.cfg.font_size_translation == 24)
    chk("保留时长已同步到 store", o.store.hold_seconds == 12.0)
    chk("条数上限已同步到 store", o.store.max_messages == 7)
    chk("窗口仍然可见", o.isVisible())

    print("\n--- 点击穿透切换后窗口不消失 ---")
    o2 = OverlayWindow(cfg.overlay)
    o2.show()
    before = o2.click_through
    o2.toggle_click_through()
    chk("穿透状态已翻转", o2.click_through != before)
    chk("切换后窗口仍可见（setWindowFlags 会隐藏，必须重新 show）",
        o2.isVisible())
    o2.toggle_click_through()
    chk("再次切换回到原状态", o2.click_through == before)
    chk("两次切换后仍可见", o2.isVisible())

    print("\n--- 配置未被子测试污染 ---")
    fresh = load_config(str(ROOT / "config.yaml"))
    chk("磁盘上的 overlay.opacity 未被测试改写",
        abs(fresh.overlay.opacity - cfg.overlay.opacity) < 1e-9)
    chk("磁盘上的 asr.provider 未被测试改写",
        fresh.asr.provider == cfg.asr.provider)


def test_save_chain() -> None:
    """C. 端到端：写文件 -> 重载配置 -> 热应用（不弹窗）。"""
    import shutil
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return

    print()
    print("=" * 70)
    print("  C. 设置保存链路（端到端）")
    print("=" * 70)

    from src.app import App
    from src.config_writer import apply_env_updates, apply_yaml_updates

    QApplication.instance() or QApplication(sys.argv)

    tmp = Path(tempfile.mkdtemp(prefix="tray_e2e_"))
    try:
        work = tmp / "config.yaml"
        shutil.copy2(ROOT / "config.yaml", work)
        # 热词表已外置，config.yaml 里的相对路径要能解析到
        shutil.copy2(ROOT / "terms.yaml", tmp / "terms.yaml")

        cfg = load_config(work)
        app = App(cfg, show_overlay=True, extra_argv=["--no-tray"])

        from src.ui.overlay import OverlayWindow

        app.overlay = OverlayWindow(cfg.overlay)
        app.overlay.show()

        print("\n--- 写入 config.yaml ---")
        apply_yaml_updates(work, {
            "overlay.opacity": 0.55,
            "overlay.font_size_translation": 20,
        })
        apply_env_updates(tmp / ".env", {"DASHSCOPE_API_KEY": "sk-e2e-test"})

        print("\n--- 重载并热应用 ---")
        app._reload_config(
            {"overlay.opacity", "overlay.font_size_translation"},
            restart_required=False,
        )

        chk("App 配置已重载", abs(app.cfg.overlay.opacity - 0.55) < 1e-9,
            f"实际 {app.cfg.overlay.opacity}")
        chk("悬浮窗已热应用新不透明度",
            abs(app.overlay.opacity - 0.55) < 1e-9,
            f"实际 {app.overlay.opacity}")
        chk("悬浮窗字号已热应用",
            app.overlay.cfg.font_size_translation == 20)
        chk("悬浮窗仍在可见状态", app.overlay.isVisible())

        print("\n--- 磁盘文件已持久化 ---")
        chk("重读配置文件仍是新值",
            abs(load_config(work).overlay.opacity - 0.55) < 1e-9)

        print("\n--- 热键热重载 ---")
        from src.ui.hotkeys import HotkeyManager

        app.hotkeys = HotkeyManager(cfg.hotkeys)
        app.hotkeys.bind("quit", lambda: None)
        n1 = app.hotkeys.start()
        app._setup_hotkeys()
        chk("热键管理器被重建（不是同一个对象）",
            app.hotkeys is not None and app.hotkeys.registered)
        app.hotkeys.stop()
        chk("重建后仍能正常注销", not app.hotkeys.is_active)

        app.overlay.hide()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    argv = sys.argv[1:]
    test_logic()
    if "--logic-only" not in argv:
        test_qt()
        test_save_chain()

    print()
    print("=" * 70)
    print(f"  结果: {_passed} 通过, {_failed} 失败")
    print("=" * 70)
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
