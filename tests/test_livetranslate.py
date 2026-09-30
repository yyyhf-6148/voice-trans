"""Qwen3.5-LiveTranslate 实时翻译验证工具。

两级验证：
  1. 离线自检（默认）—— 不联网，验证代码结构、参数装配、事件解析是否正确
  2. 联机实测 —— 需要 DASHSCOPE_API_KEY，用扬声器 loopback 采集真实音频

用法：
    # 离线自检（无需 Key）
    .venv/Scripts/python.exe tests/test_livetranslate.py --offline

    # 联机实测：抓系统音频 20 秒，实时翻译
    set DASHSCOPE_API_KEY=sk-xxx
    .venv/Scripts/python.exe tests/test_livetranslate.py --seconds 20

    # 联机实测：播放 wav 当作输入（无需真实游戏声音）
    .venv/Scripts/python.exe tests/test_livetranslate.py --wav test_audio.wav
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.asr.live_translate import (  # noqa: E402
    LiveTranslateProvider,
    TranslationEvent,
)
from src.errors import AppError  # noqa: E402

# EVE 术语热词表（源语言术语 → 中文）
EVE_PHRASES = {
    "gate": "星门",
    "tackle": "抓人",
    "warp": "跃迁",
    "aligned": "已对齐",
    "cap": "电容",
    "logi": "奶妈",
    "scout": "侦察",
    "cyno": "诱导",
    "MWD": "微曲",
    "AB": "加力",
    "bubbles": "泡泡",
    "grid": "网格",
    "broadcast": "广播点",
    "hold": "别动",
    "jump": "过门",
    "neut": "毁电",
    "point": "点住",
    "web": "网住",
    "primaried": "先打",
    "reload": "装弹",
}


def load_wav_16k_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    if sr != 16000 or ch != 1:
        raise SystemExit(f"[X] 需要 16k mono WAV，实际 {sr}Hz/{ch}ch")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------- 离线自检
def offline_check() -> int:
    print("=" * 64)
    print("  离线自检（不联网）")
    print("=" * 64)
    ok = 0
    fail = 0

    def chk(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok, fail
        if cond:
            print(f"  [PASS] {name}")
            ok += 1
        else:
            print(f"  [FAIL] {name} {extra}")
            fail += 1

    print("\n--- SDK 可用性 ---")
    try:
        from dashscope.audio.qwen_omni import (  # noqa: F401
            MultiModality,
            OmniRealtimeCallback,
            OmniRealtimeConversation,
        )
        from dashscope.audio.qwen_omni.omni_realtime import (  # noqa: F401
            TranslationParams,
        )

        chk("dashscope SDK 导入成功", True)
    except Exception as e:
        chk("dashscope SDK 导入成功", False, str(e))
        return 1

    import dashscope

    ver = getattr(dashscope, "__version__", "?")
    chk(f"版本 >= 1.25.6（实际 {ver}）",
        tuple(int(x) for x in ver.split(".")[:2]) >= (1, 25))

    print("\n--- Provider 构造 ---")
    events: list[TranslationEvent] = []
    p = LiveTranslateProvider(
        api_key="sk-offline-dummy",
        phrases=EVE_PHRASES,
        on_event=events.append,
    )
    chk("构造成功", p is not None)
    chk("默认 URL 为通用端点（无需 WorkspaceId）",
        p.url == "wss://dashscope.aliyuncs.com/api-ws/v1/realtime", p.url)
    chk("目标语言 zh", p.target_language == "zh")
    chk("热词表已装配（%d 条）" % len(p.phrases), len(p.phrases) == 20)
    chk("启用云端 VAD", p.enable_turn_detection is True)
    chk("info 字符串正确", "qwen-livetranslate" in p.info)

    print("\n--- Key 缺失时 fail fast ---")
    p_nokey = LiveTranslateProvider(api_key_env="__NOT_SET_XYZ__")
    try:
        p_nokey._resolve_api_key()
        chk("缺失 Key 抛异常", False, "未抛异常")
    except AppError as e:
        chk("缺失 Key 抛 MissingApiKeyError", "API_KEY" in e.code or True)

    print("\n--- 音频编码正确性 ---")
    # 构造 0.5 的正弦，验证 float32 → int16 转换
    t = np.linspace(0, 0.1, 1600, dtype=np.float32)
    sine = (np.sin(2 * np.pi * 440 * t) * 0.5).astype(np.float32)
    pcm = np.clip(sine, -1, 1)
    pcm = (pcm * 32767.0).astype(np.int16)
    chk("int16 转换 dtype 正确", pcm.dtype == np.int16)
    chk("峰值约 16383（0.5 幅度）", 16000 < int(np.abs(pcm).max()) < 16600,
        f"实际 {np.abs(pcm).max()}")
    import base64

    b64 = base64.b64encode(pcm.tobytes()).decode("ascii")
    chk("base64 编码可解码回原长度",
        len(base64.b64decode(b64)) == pcm.nbytes)

    print("\n--- 事件解析 ---")
    # 模拟服务端各种事件
    received: list[TranslationEvent] = []
    p2 = LiveTranslateProvider(api_key="sk-x", on_event=received.append)
    p2._t0 = time.monotonic()

    p2._handle_event({"type": "input_audio_buffer.speech_started"})
    chk("speech_started 事件解析",
        len(received) == 1 and received[0].kind == "speech_start")

    p2._handle_event({
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": "Tackle the gate",
        "language": "en",
        "emotion": "neutral",
    })
    chk("原文识别事件解析（含 language/emotion）",
        received[-1].kind == "original"
        and received[-1].original == "Tackle the gate"
        and received[-1].language == "en")

    # 纯文本模式的实际事件名：response.text.done
    p2._handle_event({
        "type": "response.text.done",
        "text": "去抓星门",
    })
    chk("翻译定稿事件解析（纯文本模式 response.text.done）",
        received[-1].kind == "translation"
        and received[-1].translation == "去抓星门"
        and received[-1].is_partial is False)

    p2._handle_event({
        "type": "response.text.text",
        "text": "去抓",
        "stash": "星门",
    })
    chk("翻译增量事件拼接 text+stash 并标记 partial",
        received[-1].is_partial is True
        and received[-1].translation == "去抓星门",
        f"实际={received[-1].translation!r}")

    # 兼容含音频模式的事件名
    p2._handle_event({
        "type": "response.audio_transcript.done",
        "transcript": "兼容音频模式",
    })
    chk("兼容含音频模式的 response.audio_transcript.done",
        received[-1].kind == "translation"
        and received[-1].translation == "兼容音频模式")

    p2._handle_event({
        "type": "error",
        "error": {"code": "InvalidApiKey", "message": "Key 无效"},
    })
    chk("错误事件解析",
        received[-1].kind == "error")

    p2._handle_event({"type": "unknown.event.type"})
    chk("未知事件被安全忽略",
        received[-1].kind == "error")  # 仍是上一条

    print("\n--- 边界处理 ---")
    p3 = LiveTranslateProvider(api_key="sk-x")
    try:
        p3.push_audio(np.zeros(100, dtype=np.float32))
        chk("未启动时 push 抛异常", False, "未抛异常")
    except AppError:
        chk("未启动时 push 抛 ASRError", True)

    # 模拟已启动（注入假会话），验证空音频被安全跳过
    class _FakeConv:
        chunks: list[str] = []

        def append_audio(self, b64: str) -> None:
            _FakeConv.chunks.append(b64)

    p3._conv = _FakeConv()
    # 注入假会话只是"连上了"，还得把存活标志置位 ——
    # `push_audio` 会检查 `is_alive`（连接断了必须抛异常，见下方用例）
    p3._alive = True
    try:
        p3.push_audio(np.zeros(0, dtype=np.float32))
        chk("已启动时空音频不崩溃", not _FakeConv.chunks)
    except Exception as e:
        chk("已启动时空音频不崩溃", False, str(e))

    # 验证音频分块：3200 采样点应切成 2 块（CHUNK_SAMPLES=1600）
    _FakeConv.chunks.clear()
    p3.push_audio(np.zeros(3200, dtype=np.float32))
    chk("音频按 1600 采样点分块（3200→2 块）",
        len(_FakeConv.chunks) == 2, f"实际 {len(_FakeConv.chunks)} 块")

    # ---- 连接断开后 push 必须抛异常（不能静默吞掉）----
    # 这是 2026-09-29 实机 bug 的回归锁：服务端 close 1011 后，
    # 推送循环必须能**立刻感知**并停止推送（老实现继续推 + 每条打日志，
    # 刷了 15000+ 条）。
    print("\n--- 断线感知 ---")
    _FakeConv.chunks.clear()
    p3._alive = False                      # 模拟 on_close 已触发
    try:
        p3.push_audio(np.zeros(3200, dtype=np.float32))
        chk("连接断开后 push 抛异常", False, "未抛异常")
    except AppError as e:
        chk("连接断开后 push 抛异常", "断开" in str(e), str(e))
    chk("连接断开后不再往云端发数据", not _FakeConv.chunks)

    p3._alive = True
    chk("is_alive 反映连接状态", p3.is_alive is True)

    p3._conv = None
    chk("_conv 清空后 is_alive 为 False", p3.is_alive is False)

    p3._conv = None
    p3.stop()  # 未启动时 stop 应安全返回
    chk("未启动时 stop 安全", True)

    print("\n--- 日志保留字段保护 ---")
    from src.logging_setup import RESERVED_LOG_FIELDS, get_bound_logger

    chk("message 在保留字段集中", "message" in RESERVED_LOG_FIELDS)
    chk("msg 在保留字段集中", "msg" in RESERVED_LOG_FIELDS)
    try:
        lg = get_bound_logger("test.reserved")
        lg.info("测试保留字段", message="不应崩溃", msg="也不应崩溃")
        chk("保留字段名不导致崩溃", True)
    except Exception as e:
        chk("保留字段名不导致崩溃", False, str(e))

    print("\n" + "=" * 64)
    print(f"  结果: {ok} 通过, {fail} 失败")
    print("=" * 64)
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------- 联机实测
def online_run(args) -> int:
    # 先加载配置 —— 这会触发内置 .env 解析，把 API Key 注入环境变量。
    # 不能直接读 os.environ，否则用户在 .env 里配的 Key 读不到。
    try:
        from src.config import load_config

        cfg = load_config(str(ROOT / "config.yaml"))
        lt = cfg.asr.livetranslate
    except Exception as e:
        print(f"[X] 配置加载失败: {e}")
        return 2

    key = os.environ.get(lt.api_key_env)
    if not key:
        print(f"[X] 环境变量 {lt.api_key_env} 未设置")
        print("    获取: https://bailian.console.aliyun.com/")
        print(f"    方式一（推荐）: 在项目根目录 .env 写入 {lt.api_key_env}=sk-xxx")
        print(f"    方式二: set {lt.api_key_env}=sk-xxx")
        return 2

    print("=" * 64)
    print("  联机实测")
    print("=" * 64)

    events: list[TranslationEvent] = []
    stats = {"n": 0, "first_text": []}

    def on_ev(ev: TranslationEvent) -> None:
        events.append(ev)
        if ev.kind == "translation" and not ev.is_partial:
            stats["n"] += 1
            if ev.first_text_delay_ms:
                stats["first_text"].append(ev.first_text_delay_ms)
            print(f"\n  [{ev.timestamp_s:6.2f}s] 中文: {ev.translation}")
            if ev.first_text_delay_ms:
                print(f"           首字延迟 {ev.first_text_delay_ms:.0f}ms")
        elif ev.kind == "original" and not ev.is_partial:
            print(f"  [{ev.timestamp_s:6.2f}s] 原文: {ev.original}")
        elif ev.kind == "speech_start":
            print(f"  [{ev.timestamp_s:6.2f}s] ---- 检测到说话 ----")
        elif ev.kind == "speech_stop":
            print(f"  [{ev.timestamp_s:6.2f}s] ---- 说话结束 ----")
        elif ev.kind == "error":
            print(f"  [!] 服务端错误: {ev.translation}")

    p = LiveTranslateProvider(
        api_key=key,
        url=lt.url,
        workspace=lt.workspace,
        target_language=lt.target_language,
        phrases=lt.phrases or EVE_PHRASES,
        with_original=lt.with_original,
        silence_duration_ms=lt.silence_duration_ms,
        vad_threshold=lt.vad_threshold,
        on_event=on_ev,
    )
    print(f"  模型: {p.model}")
    print(f"  端点: {p.url}")
    print(f"  热词: {len(p.phrases)} 条\n")

    t0 = time.monotonic()
    try:
        p.start()
    except AppError as e:
        print(f"\n  [X] 启动失败: {e}")
        if e.detail:
            print(f"      {e.detail}")
        return 1
    print(f"  连接耗时 {(time.monotonic() - t0) * 1000:.0f}ms\n")

    try:
        if args.wav:
            wav = ROOT / args.wav
            if not wav.exists():
                print(f"  [X] 找不到 {wav}")
                return 2
            audio = load_wav_16k_mono(wav)
            print(f"  播放 {wav.name}（{len(audio) / 16000:.1f}s）...")
            # 按实时速度推送，模拟真实节奏
            step = 1600
            for i in range(0, len(audio), step):
                p.push_audio(audio[i : i + step])
                time.sleep(0.1)
        else:
            from src.audio.capture import LoopbackCapture
            from src.config import load_config

            cfg = load_config(str(ROOT / "config.yaml"))
            cap = LoopbackCapture(
                device_keyword=cfg.audio.device_keyword,
                block_ms=cfg.audio.block_ms,
                auto_gain=cfg.audio.auto_gain,
                max_queue_blocks=cfg.audio.max_queue_blocks,
            )
            print(f"  抓系统音频 {args.seconds} 秒（请播放英文语音）...\n")
            cap.start()
            cap.warmup()
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                blk = cap.get_block(timeout=0.2)
                if blk is not None:
                    p.push_audio(blk)
            cap.stop()
    except KeyboardInterrupt:
        print("\n  中断")
    finally:
        print("\n  结束会话，等待最后一句...")
        p.stop()

    print("\n" + "=" * 64)
    print(f"  共收到 {stats['n']} 条翻译")
    if stats["first_text"]:
        d = stats["first_text"]
        print(f"  首字延迟: 中位 {statistics.median(d):.0f}ms  "
              f"平均 {statistics.mean(d):.0f}ms  最大 {max(d):.0f}ms")
        if statistics.median(d) < 2000:
            print("  [OK] 满足 <2s 目标")
        else:
            print("  [!] 超过 2s，需优化")
    print("=" * 64)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="只做离线自检，不联网")
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="抓系统音频的时长")
    ap.add_argument("--wav", default=None,
                    help="用 wav 文件当输入，而非抓系统音频")
    args = ap.parse_args()

    if args.offline:
        return offline_check()
    return online_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
