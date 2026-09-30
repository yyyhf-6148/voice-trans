"""设置界面覆盖率自检：**每个配置键都能在界面里改**。

这个套件存在的意义
------------------
第一版设置窗口是手写控件的，只暴露了 90 个配置键里的 35 个 ——
用户"打开设置找不到 API 接口地址"就是这么来的。

所以这里做三件事，把"全部可改"变成可验证、且不会退化的契约：

  1. 用 `Config` dataclass 递归枚举**全部叶子配置键**
  2. 断言每个叶子键要么被 `Field` 覆盖，要么在 `_EXCLUDED` 里写明理由
  3. 对**每一个**字段做真实扰动 → 收集 → 写入临时 config.yaml →
     重新加载 → 断言改动确实落盘（不是只改了内存里的控件）

跑法:
    .venv\\Scripts\\python.exe tests/test_settings_coverage.py
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_passed = 0
_failed = 0
_fail_detail: list[str] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}  {detail}")
        _fail_detail.append(f"{name} :: {detail}")


# ================================================================
#  枚举 Config 的全部叶子键
# ================================================================
_SECTIONS = (
    "app", "audio", "vad", "asr", "translate", "overlay", "hotkeys", "tray",
)


def leaf_keys(obj, prefix: str = "") -> list[str]:
    """递归收集 dataclass 的叶子键（dataclass 继续下钻，其余算叶子）。

    下划线开头的字段是**运行期附加**信息（如 `_phrases_inline`），
    不是 config.yaml 里的键，跳过。
    """
    out: list[str] = []
    for f in dataclasses.fields(obj):
        if f.name.startswith("_"):
            continue
        val = getattr(obj, f.name)
        path = f"{prefix}{f.name}"
        if dataclasses.is_dataclass(val) and not isinstance(val, type):
            out.extend(leaf_keys(val, path + "."))
        else:
            out.append(path)
    return out


def all_config_keys(cfg) -> list[str]:
    out: list[str] = []
    for name in _SECTIONS:
        out.extend(leaf_keys(getattr(cfg, name), name + "."))
    return out


# ================================================================
#  扰动规则：给每个字段造一个"类型正确且合法"的新值
# ================================================================
def perturb(spec, widget, cur):
    """返回该字段扰动后的值。非法值会让配置校验失败，所以按 validator 走。"""
    kind = spec.kind

    if spec.key == "asr.livetranslate.phrases_file":
        # 必须指向**真实存在**的文件：界面有 file 校验，且 load_config
        # 对不存在的热词表文件直接 fail fast。用项目根的 terms.yaml。
        return str(ROOT / "terms.yaml")
    if spec.validator == "http":
        return "https://example.invalid/v1"
    if spec.validator == "ws":
        return "wss://example.invalid/api-ws/v1/realtime"
    if spec.validator == "env":
        return "MY_TEST_API_KEY"
    if spec.validator == "nonempty":
        return "test-model-name"

    if kind == "bool":
        return not bool(cur)
    if kind == "int":
        v = int(cur or 0) + 1
        if spec.maximum is not None:
            v = min(v, int(spec.maximum))
        if spec.minimum is not None:
            v = max(v, int(spec.minimum))
        return v
    if kind == "float":
        step = spec.step or 0.05
        v = round(float(cur or 0.0) + step, 6)
        if spec.maximum is not None:
            v = min(v, float(spec.maximum))
        if spec.minimum is not None:
            v = max(v, float(spec.minimum))
        return v
    if kind == "color":
        return "#123456"
    if kind == "choice":
        n = widget.count()
        nxt = (widget.currentIndex() + 1) % max(1, n)
        widget.setCurrentIndex(nxt)
        return widget.currentText() if widget.isEditable() else widget.itemData(nxt)
    if kind == "dict":
        d = dict(cur or {})
        d["zz_test_term"] = "测试词"
        return d
    if kind == "list":
        return list(cur or []) + ["#ABCDEF"]
    if kind == "multi":
        # 不能用 `#` 开头：那行会落进 `|` 块标量内部，
        # 把"注释行数"这类按行首字符统计的断言带偏。
        return ("" if cur is None else str(cur)) + "\n-- extra line"

    # text
    if spec.key.startswith("hotkeys."):
        return "ctrl+shift+f9"
    if spec.key == "audio.device_keyword":
        return "USB Audio"
    return ("" if cur is None else str(cur)) + "-x"


def apply_perturbation(dlg, key: str):
    """把扰动值塞进控件，返回期望的新值。"""
    spec = dlg._specs[key]
    widget = dlg._fields[key]
    cur = dlg._read(key)
    want = perturb(spec, widget, cur)

    from PySide6.QtWidgets import (
        QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit,
        QSpinBox,
    )

    if isinstance(widget, QCheckBox):
        widget.setChecked(bool(want))
    elif isinstance(widget, QComboBox):
        if spec.kind != "choice":
            # 设备下拉：没有该项就现加一个
            idx = widget.findData(want)
            if idx < 0:
                widget.addItem(want, want)
                idx = widget.findData(want)
            widget.setCurrentIndex(idx)
        elif widget.isEditable():
            widget.setEditText(str(want))
    elif isinstance(widget, QSpinBox):
        widget.setValue(int(want))
    elif isinstance(widget, QDoubleSpinBox):
        widget.setValue(float(want))
    elif isinstance(widget, QPlainTextEdit):
        if spec.kind == "dict":
            widget.setPlainText("\n".join(f"{k}: {v}" for k, v in want.items()))
        elif spec.kind == "list":
            widget.setPlainText("\n".join(want))
        else:
            widget.setPlainText(str(want))
    elif isinstance(widget, QLineEdit):
        widget.setText(str(want))
    else:
        raise AssertionError(f"未知控件类型 {type(widget)} for {key}")

    return dlg._read(key)


# ================================================================
def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    print("=" * 70)
    print("  设置界面覆盖率自检")
    print("=" * 70)

    from PySide6.QtWidgets import QApplication

    from src.config import load_config
    from src.config_writer import apply_yaml_updates
    from src.ui.settings_dialog import (
        _DEFAULT_LIVETRANSLATE_URL,
        _EXCLUDED,
        SettingsDialog,
    )

    QApplication.instance() or QApplication(sys.argv)

    cfg = load_config(str(ROOT / "config.yaml"))
    keys = all_config_keys(cfg)

    print(f"\n--- 1. 键清单（{len(keys)} 个叶子键） ---")
    chk("叶子键数量符合预期（92）", len(keys) == 92, f"实际 {len(keys)}")
    chk("叶子键无重复", len(keys) == len(set(keys)))

    dlg = SettingsDialog(cfg, None)
    spec_keys = set(dlg._specs)
    field_keys = set(dlg._fields)

    print("\n--- 2. 覆盖率 ---")
    missing = sorted(set(keys) - spec_keys - set(_EXCLUDED))
    chk("每个配置键都被界面覆盖（或已声明排除）", not missing,
        f"未覆盖 {len(missing)} 个: {missing[:12]}")
    chk("排除表里没有已覆盖的键",
        not (set(_EXCLUDED) & spec_keys))
    chk("排除表里没有不存在的键",
        not (set(_EXCLUDED) - set(keys)),
        f"多余: {sorted(set(_EXCLUDED) - set(keys))}")

    phantom = sorted(spec_keys - set(keys))
    chk("界面没有引用不存在的配置键", not phantom,
        f"幽灵键: {phantom[:12]}")

    chk("所有 Field 都注册了控件", spec_keys <= field_keys,
        f"未注册: {sorted(spec_keys - field_keys)[:8]}")
    chk("控件数与字段数一致", len(field_keys) == len(spec_keys),
        f"{len(field_keys)} vs {len(spec_keys)}")

    print("\n--- 3. 初始快照无漂移 ---")
    dlg._collect()
    chk("未改动时 yaml_updates 为空", dlg.yaml_updates == {},
        f"漂移了 {len(dlg.yaml_updates)} 个: "
        f"{list(dlg.yaml_updates.items())[:6]}")
    chk("未改动时 env_updates 为空", dlg.env_updates == {},
        f"实际 {dlg.env_updates}")
    chk("未改动时不需要重启", dlg.restart_required is False)

    # 接口地址：配置为 null 时框里**直接显示实际生效值**，
    # 但显示 ≠ 改动 —— 绝不能因此把它写回 config.yaml（配置漂移）。
    url_widget = dlg._fields["asr.livetranslate.url"]
    want_url = (_DEFAULT_LIVETRANSLATE_URL
                if cfg.asr.livetranslate.url is None
                else cfg.asr.livetranslate.url)
    chk("接口地址框里显示的是实际生效值（不是空框）",
        url_widget.text() == want_url, f"实际 {url_widget.text()!r}")
    chk("显示 fallback 不算改动（零漂移）",
        "asr.livetranslate.url" not in dlg.yaml_updates,
        f"实际 {dlg.yaml_updates.get('asr.livetranslate.url')!r}")
    chk("接口地址读回仍是 None（不是把 fallback 当成用户设置）",
        dlg._read("asr.livetranslate.url") == cfg.asr.livetranslate.url,
        f"实际 {dlg._read('asr.livetranslate.url')!r}")

    print("\n--- 4. 后端已固定为 livetranslate ---")
    chk("界面不再暴露 asr.provider（已声明排除）",
        "asr.provider" not in dlg._fields and "asr.provider" in _EXCLUDED,
        f"在界面里={('asr.provider' in dlg._fields)}")
    chk("界面不再暴露 cloud / local 参数",
        not any(k.startswith(("asr.cloud.", "asr.local."))
                for k in dlg._fields),
        f"残留 {[k for k in dlg._fields if k.startswith(('asr.cloud.', 'asr.local.'))]}")
    chk("界面不再暴露级联翻译 / 本地 VAD",
        not any(k.startswith(("translate.", "vad.")) for k in dlg._fields),
        f"残留 {[k for k in dlg._fields if k.startswith(('translate.', 'vad.'))]}")
    chk("界面不再暴露仅 Whisper 用的通用识别参数",
        not any(k in dlg._fields for k in (
            "asr.language", "asr.initial_prompt", "asr.beam_size",
            "asr.vad_filter")))
    chk("livetranslate 页有接口地址字段",
        "asr.livetranslate.url" in dlg._fields)
    chk("livetranslate 页有业务空间字段",
        "asr.livetranslate.workspace" in dlg._fields)
    chk("livetranslate 页有热词表文件字段",
        "asr.livetranslate.phrases_file" in dlg._fields)
    chk("Key 环境变量名取自 livetranslate",
        dlg._api_key_env_name() == cfg.asr.livetranslate.api_key_env,
        f"实际 {dlg._api_key_env_name()}")
    dlg.deleteLater()

    # ---------------- 5. 每个字段真实扰动 -> 落盘 -> 重载 ----------------
    print("\n--- 5. 逐字段端到端：扰动 -> 写入 -> 重载 ---")
    tmp = Path(tempfile.mkdtemp(prefix="settings_cov_"))
    try:
        work = tmp / "config.yaml"
        shutil.copy2(ROOT / "config.yaml", work)
        # 热词表已外置：phrases_file 的相对路径按 config.yaml 所在目录解析
        shutil.copy2(ROOT / "terms.yaml", tmp / "terms.yaml")
        cfg2 = load_config(work)
        dlg2 = SettingsDialog(cfg2, None)

        readonly = set(dlg2._readonly)
        targets = [k for k in sorted(dlg2._specs) if k not in readonly]
        chk("只读字段被正确识别（app.target_sample_rate）",
            readonly == {"app.target_sample_rate"}, f"实际 {readonly}")

        expected: dict[str, object] = {}
        bad: list[str] = []
        for key in targets:
            try:
                expected[key] = apply_perturbation(dlg2, key)
            except Exception as e:      # noqa: BLE001
                bad.append(f"{key}: {type(e).__name__} {e}")
        chk("所有字段都能被扰动（控件类型可写）", not bad,
            f"{len(bad)} 个失败: {bad[:6]}")

        dlg2._collect()
        updates = dlg2.yaml_updates

        not_collected = [k for k in targets if k not in updates]
        chk("每个被改动的字段都进了 yaml_updates", not not_collected,
            f"漏收集 {len(not_collected)} 个: {not_collected[:10]}")
        chk("只读字段未被收集",
            "app.target_sample_rate" not in updates)

        errs = dlg2._validate()
        chk("扰动值都通过校验（不会误拦合法输入）", not errs,
            f"{errs[:4]}")

        apply_yaml_updates(work, updates)
        reloaded = load_config(work)

        def get(cfgobj, dotted):
            obj = cfgobj
            for part in dotted.split("."):
                obj = getattr(obj, part)
            return obj

        mismatched: list[str] = []
        for key in targets:
            if key not in updates:
                continue
            got = get(reloaded, key)
            want = updates[key]
            spec = dlg2._specs[key]
            if key == "asr.livetranslate.phrases":
                # 运行期 phrases 是「外部文件 + 内联」的**合并结果**，
                # 落盘的只是内联部分 —— 比内联原始值
                ok = reloaded.asr.livetranslate._phrases_inline == want
            elif spec.kind == "multi":
                # `|` 块标量按 YAML 语义会补一个结尾换行；
                # 读进控件时会 rstrip 掉，所以这里也按 rstrip 比较。
                ok = str(got).rstrip("\n") == str(want).rstrip("\n")
            elif isinstance(want, float) and isinstance(got, (int, float)):
                ok = abs(float(got) - want) < 1e-6
            else:
                ok = got == want
            if not ok:
                mismatched.append(f"{key}: 期望 {want!r} 实际 {got!r}")

        chk(f"{len(targets)} 个字段全部落盘且可重载", not mismatched,
            f"{len(mismatched)} 个不一致: {mismatched[:8]}")

        chk("写入后注释行数未变化",
            sum(1 for ln in work.read_text(encoding="utf-8").splitlines()
                if ln.lstrip().startswith("#"))
            == sum(1 for ln in (ROOT / "config.yaml")
                   .read_text(encoding="utf-8").splitlines()
                   if ln.lstrip().startswith("#")))

        chk("写入后内联热词表仍完整",
            reloaded.asr.livetranslate._phrases_inline
            == {"zz_test_term": "测试词"},
            f"实际 {reloaded.asr.livetranslate._phrases_inline}")
        chk("写入后外部热词表仍被引用",
            reloaded.asr.livetranslate._phrases_external_count >= 600,
            f"实际 {reloaded.asr.livetranslate._phrases_external_count}")
        chk("合并后 = 外部 + 内联",
            len(reloaded.asr.livetranslate.phrases)
            == (reloaded.asr.livetranslate._phrases_external_count
                + len(reloaded.asr.livetranslate._phrases_inline)))
        chk("写入后说话人配色仍是列表",
            isinstance(reloaded.overlay.speaker_colors, list)
            and len(reloaded.overlay.speaker_colors) >= 2)
        chk("写入后 system_prompt 仍是多行文本",
            "\n" in reloaded.translate.online.system_prompt)
        dlg2.deleteLater()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- 6. 校验拦截 ----------------
    print("\n--- 6. 非法值被拦截 ---")
    dlg3 = SettingsDialog(cfg, None)

    dlg3._fields["asr.livetranslate.url"].setText("http://wrong-scheme")
    errs = dlg3._validate()
    chk("非 ws(s) 的接口地址被拦", any("接口地址" in e for e in errs),
        f"实际 {errs}")

    dlg3._fields["asr.livetranslate.url"].setText(
        "wss://ok.example/api-ws/v1/realtime")
    dlg3._fields["asr.livetranslate.api_key_env"].setText("9bad-name")
    errs = dlg3._validate()
    chk("非法环境变量名被拦", any("环境变量名" in e for e in errs),
        f"实际 {errs}")

    dlg3._fields["asr.livetranslate.api_key_env"].setText("GOOD_NAME")
    chk("修正后校验通过", dlg3._validate() == [], f"实际 {dlg3._validate()}")

    # 热词表文件：指向不存在的文件必须当场拦住（否则下次启动 fail fast 起不来）
    dlg3._fields["asr.livetranslate.phrases_file"].setText("no_such_terms.yaml")
    errs = dlg3._validate()
    chk("不存在的热词表文件被拦", any("热词表文件" in e for e in errs),
        f"实际 {errs}")
    dlg3._fields["asr.livetranslate.phrases_file"].setText(str(ROOT / "terms.yaml"))
    chk("存在的热词表文件通过校验", dlg3._validate() == [],
        f"实际 {dlg3._validate()}")
    dlg3._fields["asr.livetranslate.phrases_file"].setText("")
    chk("热词表文件留空合法（只用内联表）", dlg3._validate() == [],
        f"实际 {dlg3._validate()}")
    chk("热词表文件留空读作 None",
        dlg3._read("asr.livetranslate.phrases_file") is None,
        f"实际 {dlg3._read('asr.livetranslate.phrases_file')!r}")

    # 可空字段：留空应送 None 而不是空串，且不触发校验
    dlg3._fields["asr.livetranslate.url"].setText("")
    chk("可空接口地址留空不报错", dlg3._validate() == [],
        f"实际 {dlg3._validate()}")
    chk("可空接口地址留空读作 None",
        dlg3._read("asr.livetranslate.url") is None,
        f"实际 {dlg3._read('asr.livetranslate.url')!r}")
    dlg3.deleteLater()

    print()
    print("=" * 70)
    print(f"  结果: {_passed} 通过, {_failed} 失败")
    print("=" * 70)
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
