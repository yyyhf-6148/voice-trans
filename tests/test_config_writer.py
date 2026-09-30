"""config_writer 自检：行级手术是否可靠（不破坏注释、能往返读回）。

跑法:
    .venv\\Scripts\\python.exe tests/test_config_writer.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config, read_phrases_file       # noqa: E402
from src.config_writer import (                          # noqa: E402
    KeyNotFoundError,
    apply_env_updates,
    apply_yaml_updates,
    dump_scalar,
    read_env,
)
from src.errors import ConfigError                       # noqa: E402

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


def _count_comments(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.lstrip().startswith("#"))


def main() -> int:
    print("=" * 70)
    print("  config_writer 自检")
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="cfgwriter_"))
    src_yaml = ROOT / "config.yaml"
    work = tmp / "config.yaml"
    shutil.copy2(src_yaml, work)
    # 热词表已外置：config.yaml 里 phrases_file: terms.yaml，
    # 相对路径按 config.yaml 所在目录解析 —— 临时目录里也得有一份，
    # 否则 load_config 会因为"热词表文件不存在"直接 fail fast。
    shutil.copy2(ROOT / "terms.yaml", tmp / "terms.yaml")

    orig_text = work.read_text(encoding="utf-8")
    orig_comments = _count_comments(orig_text)

    # ---------------- 标量 ----------------
    print("\n--- 标量替换 ---")
    apply_yaml_updates(work, {
        "overlay.opacity": 0.66,
        "overlay.font_size_translation": 22,
        "overlay.show_original": False,
        "asr.livetranslate.silence_duration_ms": 650,
        "asr.livetranslate.voice": "Cherry",
        "audio.device_keyword": "S/PDIF",
    })
    after = work.read_text(encoding="utf-8")
    cfg = load_config(work)

    chk("float 写入正确", abs(cfg.overlay.opacity - 0.66) < 1e-9,
        f"实际 {cfg.overlay.opacity}")
    chk("int 写入正确", cfg.overlay.font_size_translation == 22)
    chk("bool 写入正确", cfg.overlay.show_original is False)
    chk("嵌套三层键写入正确",
        cfg.asr.livetranslate.silence_duration_ms == 650)
    chk("含特殊字符的字符串被正确加引号",
        cfg.asr.livetranslate.voice == "Cherry")
    chk("含斜杠的值被正确写入",
        cfg.audio.device_keyword == "S/PDIF")

    print("\n--- 注释与结构未被破坏 ---")
    chk("注释行数不变",
        _count_comments(after) == orig_comments,
        f"{orig_comments} -> {_count_comments(after)}")
    chk("总行数变化在合理范围（±3）",
        abs(len(after.splitlines()) - len(orig_text.splitlines())) <= 3,
        f"{len(orig_text.splitlines())} -> {len(after.splitlines())}")
    chk("文件仍以换行结尾", after.endswith("\n"))

    print("\n--- 未改动的键保持原值 ---")
    orig_cfg = load_config(src_yaml)
    chk("未涉及的其他 overlay 字段不变",
        cfg.overlay.color_translation == orig_cfg.overlay.color_translation)
    chk("未涉及的其他节不变",
        cfg.translate.online.model == orig_cfg.translate.online.model)
    chk("热词表未被标量写入破坏",
        len(cfg.asr.livetranslate.phrases)
        == len(orig_cfg.asr.livetranslate.phrases))
    chk("外部热词表已加载（terms.yaml）",
        cfg.asr.livetranslate._phrases_external_count
        == orig_cfg.asr.livetranslate._phrases_external_count > 0,
        f"实际 {cfg.asr.livetranslate._phrases_external_count}")

    # ---------------- 块映射 ----------------
    print("\n--- 块映射（内联热词表）整体替换 ---")
    new_phrases = {"gate": "星门", "logie": "奶妈", "MWD": "微曲"}
    apply_yaml_updates(work, {"asr.livetranslate.phrases": new_phrases})
    cfg2 = load_config(work)
    # ⚠️ 运行期 phrases 是「外部文件 + 内联」的合并结果，
    #    所以要比的是**内联原始值**（config.yaml 里 `phrases:` 块的内容）
    chk("内联热词表被替换为新内容",
        cfg2.asr.livetranslate._phrases_inline == new_phrases,
        f"实际 {cfg2.asr.livetranslate._phrases_inline}")
    chk("内联热词表覆盖外部表（内联优先）",
        all(cfg2.asr.livetranslate.phrases[k] == v
            for k, v in new_phrases.items()),
        f"实际 {cfg2.asr.livetranslate.phrases.get('MWD')!r}")
    # 合并后条数 = 外部 + 内联 − 重名（重名的以内联为准，不会重复计数）
    external = read_phrases_file(cfg2.asr.livetranslate._phrases_file_path)
    overlap = len(set(external) & set(new_phrases))
    chk("合并后条数 = 外部 + 内联 − 重名",
        len(cfg2.asr.livetranslate.phrases)
        == len(new_phrases)
        + cfg2.asr.livetranslate._phrases_external_count
        - overlap,
        f"实际 {len(cfg2.asr.livetranslate.phrases)}"
        f"（外部 {cfg2.asr.livetranslate._phrases_external_count}"
        f" + 内联 {len(new_phrases)} − 重名 {overlap}）")
    after2 = work.read_text(encoding="utf-8")
    chk("块替换后注释仍在",
        _count_comments(after2) == orig_comments)
    chk("块替换后文件仍可解析",
        cfg2.asr.livetranslate.target_language == "zh")

    # 再写回大表，确认可扩展
    apply_yaml_updates(work, {
        "asr.livetranslate.phrases": {f"term{i}": f"词{i}" for i in range(60)}
    })
    cfg3 = load_config(work)
    chk("内联热词表可扩到 60 条",
        len(cfg3.asr.livetranslate._phrases_inline) == 60)
    chk("块替换不影响后续节（hotkeys）",
        cfg3.hotkeys.quit == orig_cfg.hotkeys.quit)

    # ---------------- 块标量（多行文本） ----------------
    print("\n--- 块标量 system_prompt ---")
    orig_prompt = orig_cfg.translate.online.system_prompt
    chk("原始 system_prompt 是多行块",
        "\n" in orig_prompt.strip(), f"{orig_prompt!r:.40}")

    new_prompt = "Line one.\nLine two.\nLine three."
    apply_yaml_updates(work, {"translate.online.system_prompt": new_prompt})
    cfg4 = load_config(work)
    chk("块标量写入后可解析",
        cfg4.translate.online.system_prompt.rstrip("\n") == new_prompt,
        f"实际 {cfg4.translate.online.system_prompt!r}")

    after_bs = work.read_text(encoding="utf-8")
    chk("块标量写入后旧内容被清干净",
        "You are a real-time subtitle translator" not in after_bs)
    chk("块标量写入后注释行数不变",
        _count_comments(after_bs) == orig_comments)
    chk("块标量写入不影响后续节（overlay）",
        cfg4.overlay.opacity == cfg3.overlay.opacity)

    # 反复写入应稳定（行数不膨胀）
    n_before = len(after_bs.splitlines())
    apply_yaml_updates(work, {"translate.online.system_prompt": new_prompt})
    apply_yaml_updates(work, {"translate.online.system_prompt": new_prompt})
    n_after = len(work.read_text(encoding="utf-8").splitlines())
    chk("块标量反复写入行数稳定", n_before == n_after,
        f"{n_before} -> {n_after}")

    # 块标量变短：旧内容必须整段消失
    apply_yaml_updates(work, {"translate.online.system_prompt": "short"})
    cfg5 = load_config(work)
    chk("块标量可缩短",
        cfg5.translate.online.system_prompt.strip() == "short",
        f"实际 {cfg5.translate.online.system_prompt!r}")
    chk("缩短后旧长行已删除",
        "Line three." not in work.read_text(encoding="utf-8"))

    # ---------------- 块序列（列表） ----------------
    print("\n--- 块序列 speaker_colors ---")
    new_colors = ["#111111", "#222222", "#333333", "#444444"]
    apply_yaml_updates(work, {"overlay.speaker_colors": new_colors})
    cfg6 = load_config(work)
    chk("列表被整体替换",
        cfg6.overlay.speaker_colors == new_colors,
        f"实际 {cfg6.overlay.speaker_colors}")
    chk("列表替换后后续节不受影响",
        cfg6.tray.enabled == orig_cfg.tray.enabled)

    apply_yaml_updates(work, {"overlay.speaker_colors": ["#ABCDEF"]})
    cfg7 = load_config(work)
    chk("列表可缩短到 1 项",
        cfg7.overlay.speaker_colors == ["#ABCDEF"],
        f"实际 {cfg7.overlay.speaker_colors}")
    chk("缩短后旧颜色已删除",
        "#444444" not in work.read_text(encoding="utf-8"))

    # ---------------- 空串 / None 归一化 ----------------
    print("\n--- 可选字段清空回 null ---")
    apply_yaml_updates(work, {"asr.livetranslate.url": "wss://x.y/z"})
    chk("先写入一个 url",
        load_config(work).asr.livetranslate.url == "wss://x.y/z")

    # 契约：设置界面遇到"可为空的字段被清空"时显式送 None，
    # 而不是空串 —— 空串会被 YAML 解析成 ""，语义上不是"未设置"。
    apply_yaml_updates(work, {"asr.livetranslate.url": None})
    chk("送 None 清空后回到 None",
        load_config(work).asr.livetranslate.url is None,
        f"实际 {load_config(work).asr.livetranslate.url!r}")
    chk("yaml 里写的是 null",
        "url: null" in work.read_text(encoding="utf-8"))

    # 兜底：本来就 null 的字段被送空串时，保持 null 而不是变成 ""
    apply_yaml_updates(work, {"asr.livetranslate.workspace": ""})
    chk("原本 null 的字段送空串仍是 None",
        load_config(work).asr.livetranslate.workspace is None,
        f"实际 {load_config(work).asr.livetranslate.workspace!r}")

    apply_yaml_updates(work, {"audio.device_keyword": "USB"})
    apply_yaml_updates(work, {"audio.device_keyword": ""})
    chk("原本是空串的字段清空后仍是空串",
        load_config(work).audio.device_keyword == "",
        f"实际 {load_config(work).audio.device_keyword!r}")

    # 所有改动写完后，整份配置仍然合法
    chk("多轮写入后配置整体仍可加载", load_config(work) is not None)

    # ---------------- 错误路径 ----------------
    print("\n--- 错误处理 ---")
    try:
        apply_yaml_updates(work, {"overlay.not_a_field": 1})
        chk("未知键被拦截", False, "没有抛错")
    except KeyNotFoundError:
        chk("未知键被拦截", True)
    except Exception as e:
        chk("未知键被拦截", False, f"抛了 {type(e).__name__}: {e}")

    after_fail = work.read_text(encoding="utf-8")
    chk("失败时不产生部分写入（原子性）",
        after_fail == after2 or "not_a_field" not in after_fail)

    try:
        apply_yaml_updates(work, {"asr.livetranslate.api_key": "sk-xxx"})
        chk("密钥类键被拒绝写入 yaml", False, "没有抛错")
    except ConfigError:
        chk("密钥类键被拒绝写入 yaml", True)
    except Exception as e:
        chk("密钥类键被拒绝写入 yaml", False, f"抛了 {type(e).__name__}")

    # ---------------- 标量序列化 ----------------
    print("\n--- 标量序列化 ---")
    chk("bool -> true", dump_scalar(True) == "true")
    chk("None -> null", dump_scalar(None) == "null")
    chk("int 原样", dump_scalar(42) == "42")
    chk("float 去浮点噪声", dump_scalar(0.1 + 0.2) == "0.3")
    chk("普通字符串不加引号", dump_scalar("qwen3.5-flash") == "qwen3.5-flash")
    chk("含 # 的字符串加引号", dump_scalar("#FFFFFF") == '"#FFFFFF"')
    chk("含冒号的字符串加引号",
        dump_scalar("wss://a.b/c") == '"wss://a.b/c"')
    chk("裸 true 形状的字符串加引号", dump_scalar("true") == '"true"')
    chk("空串加引号", dump_scalar("") == '""')
    # 中文走加引号分支：与 config.yaml 现有风格一致（`gate: "星门"`），
    # 且能防御首尾空格等边界，比裸写更稳。
    chk("中文加引号（与现有风格一致）", dump_scalar("星门") == '"星门"')
    chk("含引号的字符串被转义",
        dump_scalar('say "hi"') == '"say \\"hi\\""')

    # ---------------- .env ----------------
    print("\n--- .env 写入 ---")
    env = tmp / ".env"
    env.write_text(
        "# 注释\nASR_API_KEY=old\n\n# 尾部注释\nDEEPSEEK_API_KEY=dsk-old\n",
        encoding="utf-8",
    )
    apply_env_updates(env, {"ASR_API_KEY": "sk-new"})
    got = read_env(env)
    chk("已存在的键被更新", got["ASR_API_KEY"] == "sk-new")
    chk("其他键不受影响", got["DEEPSEEK_API_KEY"] == "dsk-old")
    env_text = env.read_text(encoding="utf-8")
    chk(".env 注释保留", "# 注释" in env_text and "# 尾部注释" in env_text)

    apply_env_updates(env, {"DASHSCOPE_API_KEY": "sk-dash"})
    got2 = read_env(env)
    chk("新键被追加", got2.get("DASHSCOPE_API_KEY") == "sk-dash")
    chk("追加后旧键仍在", got2["ASR_API_KEY"] == "sk-new")

    chk("os.environ 被同步刷新",
        __import__("os").environ.get("DASHSCOPE_API_KEY") == "sk-dash")

    # 不存在的 .env 应能创建
    env2 = tmp / "sub" / "new.env"
    env2.parent.mkdir(parents=True, exist_ok=True)
    apply_env_updates(env2, {"FOO": "bar"})
    chk("可创建新的 .env", read_env(env2) == {"FOO": "bar"})

    print()
    print("=" * 70)
    print(f"  结果: {_passed} 通过, {_failed} 失败")
    print("=" * 70)

    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
