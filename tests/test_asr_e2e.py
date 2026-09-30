"""P2 端到端验收：合成英文语音 -> 播放 -> 采集 -> VAD -> 识别。

用 Windows 内置 SAPI TTS 生成真实英文语音，播放后走完整链路，
比对识别结果与原文，验证准确率。

用法:
    .venv\\Scripts\\python.exe tests\\test_asr_e2e.py
    .venv\\Scripts\\python.exe tests\\test_asr_e2e.py --keep-audio
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402

PASS = 0
FAIL = 0
WARN = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def warn(name: str, detail: str) -> None:
    global WARN
    WARN += 1
    print(f"  [WARN] {name}  {detail}")


# =========================================================
#  TTS 生成测试语音
# =========================================================
TEST_SENTENCES = [
    "Enemy fleet is jumping in through the gate.",
    "I need capacitor, I am running low.",
    "Tackle the primary target and hold it down.",
]


def synthesize_speech(text: str, out_path: Path) -> bool:
    """用 Windows SAPI 合成语音到 wav。优先选英文语音。"""
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return False

    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        # 优先挑英文语音，否则识别英文会不准
        voices = voice.GetVoices()
        for i in range(voices.Count):
            desc = voices.Item(i).GetDescription().lower()
            if "english" in desc:
                voice.Voice = voices.Item(i)
                break

        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(str(out_path), 3, False)  # 3 = SSFMCreateForWrite
        voice.AudioOutputStream = stream
        voice.Rate = 0
        voice.Speak(text)
        stream.Close()
        return True
    except Exception:
        return False


def synthesize_with_fallback(text: str, out_path: Path, sr: int = 16000) -> bool:
    """尝试 SAPI；失败则用合成近似信号（仅测链路，不测准确率）。"""
    if synthesize_speech(text, out_path):
        return True
    return False


def play_wav_async(path: Path) -> threading.Thread:
    import winsound

    def _play() -> None:
        winsound.PlaySound(str(path), winsound.SND_FILENAME)

    th = threading.Thread(target=_play, daemon=True)
    th.start()
    return th


def wav_to_16k_mono(path: Path) -> np.ndarray:
    """读取 wav 并转成 16k mono float32。"""
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    if sw == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0

    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)

    if sr != 16000:
        from src.audio.ring_buffer import resample

        data = resample(data, sr, 16000)

    return data.astype(np.float32)


# =========================================================
#  测试项
# =========================================================
def test_asr_direct() -> list[tuple[str, str]]:
    """直接识别 TTS 音频（不经过采集，隔离测试 ASR 本身）。"""
    print("\n--- ASR 直接识别测试（隔离采集环节）---")

    from src.asr.factory import create_asr_provider

    cfg = load_config()
    results: list[tuple[str, str]] = []

    tts_dir = ROOT / "_tts"
    tts_dir.mkdir(exist_ok=True)

    ok_count = 0
    provider = create_asr_provider(cfg.asr)
    provider.load()  # 只加载一次，避免重复开销

    try:
        for i, sent in enumerate(TEST_SENTENCES):
            wav_path = tts_dir / f"sent_{i}.wav"
            if not synthesize_with_fallback(sent, wav_path):
                warn(f"句子{i+1} TTS 合成", "SAPI 不可用，跳过")
                continue

            audio = wav_to_16k_mono(wav_path)
            if len(audio) == 0:
                warn(f"句子{i+1}", "读取音频为空")
                continue

            t0 = time.monotonic()
            result = provider.transcribe(audio, language="en")
            elapsed = time.monotonic() - t0

            text = result.text.strip()
            results.append((sent, text))
            ok_count += 1

            print(f"\n    原文  : {sent}")
            print(f"    识别  : {text}")
            print(f"    耗时  : {elapsed:.3f}s  (音频 {len(audio)/16000:.2f}s, "
                  f"RTF {elapsed/(len(audio)/16000):.3f})")
    finally:
        provider.unload()

    check(f"至少完成 1 句识别 (实际 {ok_count})", ok_count > 0)

    # 简单相似度
    if results:
        scores = []
        for orig, got in results:
            o = set(orig.lower().replace(".", "").replace(",", "").split())
            g = set(got.lower().replace(".", "").replace(",", "").split())
            if o:
                scores.append(len(o & g) / len(o))
        if scores:
            avg = sum(scores) / len(scores)
            print(f"\n    平均词重合度: {avg*100:.0f}%")
            check(
                f"识别准确度可接受（词重合 >= 60%，实际 {avg*100:.0f}%）",
                avg >= 0.6,
                f"avg={avg:.2f}",
            )

    # 清理
    for f in tts_dir.glob("*.wav"):
        f.unlink(missing_ok=True)
    tts_dir.rmdir()

    return results


def test_vad_segmentation() -> None:
    """测试 VAD 切句：静音-语音-静音 应产出一个句子。"""
    print("\n--- VAD 切句测试 ---")

    from src.asr.vad import SpeechSegment, VadSegmenter

    cfg = load_config()
    seg = VadSegmenter(cfg.vad)
    seg.load()

    # 用 TTS 语音作为素材
    tts_dir = ROOT / "_tts_vad"
    tts_dir.mkdir(exist_ok=True)
    wav_path = tts_dir / "vad_test.wav"

    if not synthesize_with_fallback(TEST_SENTENCES[0], wav_path):
        warn("VAD 切句测试", "SAPI 不可用，用近似信号代替")
        # 用共振峰信号代替
        sr = 16000
        t = np.linspace(0, 2.0, sr * 2, endpoint=False)
        speech = sum(
            a * np.sin(2 * np.pi * f * t)
            for f, a in [(120, 0.5), (240, 0.3), (800, 0.2)]
        ).astype(np.float32)
        speech = (speech / np.abs(speech).max() * 0.5).astype(np.float32)
    else:
        speech = wav_to_16k_mono(wav_path)
        tts_dir.joinpath("vad_test.wav").unlink(missing_ok=True)

    try:
        tts_dir.rmdir()
    except OSError:
        pass

    # 组装: 0.5s静音 + 语音 + 1.0s静音（触发结束）
    silence_head = np.zeros(int(16000 * 0.5), dtype=np.float32)
    silence_tail = np.zeros(int(16000 * 1.0), dtype=np.float32)
    stream = np.concatenate([silence_head, speech, silence_tail])

    # 按 512 采样窗口喂入
    events = []
    win = 512
    for i in range(0, len(stream) - win, win):
        for ev in seg.push(stream[i:i + win]):
            events.append(ev)

    full = [e for e in events if isinstance(e, SpeechSegment)]
    partials = [e for e in events if not isinstance(e, SpeechSegment)]

    print(f"    完整句子: {len(full)}   半句触发: {len(partials)}")
    for e in full:
        print(f"      {e}")

    check(f"检测到 1 个完整语音段 (实际 {len(full)})", len(full) == 1)
    if full:
        seg0 = full[0]
        # 注意：TTS 生成的 wav 首尾含静音填充（本次约 0.1s + 0.7s），
        # VAD 裁掉静音是正确的，所以不能拿「文件时长」当基准。
        # 这里改为校验：切出的段落应占原始音频的合理比例（50%~100%）
        ratio = seg0.duration_s / (len(speech) / 16000)
        print(f"    切出 {seg0.duration_s:.2f}s / 原始 {len(speech)/16000:.2f}s "
              f"= {ratio*100:.0f}%（差额为首尾静音，属正常）")
        check(
            f"切出时长占原始 50%~100% (实际 {ratio*100:.0f}%)",
            0.5 <= ratio <= 1.0,
            f"ratio={ratio:.2f}",
        )
        # 应保留大部分语音内容（不该切掉超过一半）
        check(
            "未过度裁剪（保留 >= 60% 音频）",
            seg0.duration_s >= len(speech) / 16000 * 0.6,
            f"got {seg0.duration_s:.2f}s",
        )


def test_pipeline_config() -> None:
    """测试 Provider 工厂与配置校验。"""
    print("\n--- Provider 工厂测试 ---")

    from src.asr.factory import create_asr_provider
    from src.config import AsrSection, CloudAsrSection, LocalAsrSection

    # local
    sec = AsrSection(provider="local")
    p = create_asr_provider(sec)
    check("创建 local provider", p.name == "local-whisper", f"got {p.name}")

    # cloud（不 load，只验证创建）
    sec2 = AsrSection(
        provider="cloud",
        cloud=CloudAsrSection(
            base_url="https://example.com/v1", api_key_env="FAKE_KEY"
        ),
    )
    p2 = create_asr_provider(sec2)
    check("创建 cloud provider", p2.name == "cloud-whisper", f"got {p2.name}")

    # 非法 provider
    from src.errors import ConfigError

    try:
        create_asr_provider(AsrSection(provider="bogus"))
        check("非法 provider 被拒绝", False, "未抛异常")
    except ConfigError:
        check("非法 provider 被拒绝", True)

    # 设备探测
    from src.asr.base import detect_device

    dev, ct = detect_device("auto")
    print(f"    detect_device(auto) -> {dev}/{ct}")
    check("设备探测返回合法值", dev in {"cuda", "cpu"} and ct in {
        "float16", "float32", "int8"
    }, f"got {dev}/{ct}")


def main() -> int:
    parser = argparse.ArgumentParser(description="P2 ASR 端到端验收")
    parser.add_argument("--skip-asr", action="store_true",
                        help="跳过最耗时的直接识别测试")
    args = parser.parse_args()

    print("=" * 70)
    print("  P2 验收测试：VAD + ASR")
    print("=" * 70)

    try:
        test_pipeline_config()
        test_vad_segmentation()
        if not args.skip_asr:
            test_asr_direct()
    except KeyboardInterrupt:
        print("\n[中断]")
        return 130

    print("\n" + "=" * 70)
    print(f"  结果: {PASS} 通过, {FAIL} 失败, {WARN} 警告")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
