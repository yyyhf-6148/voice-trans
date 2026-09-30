"""GPU 保活（GpuKeepAlive）效果对照测试。

问题背景：
    P106 等显卡在空闲后会降频，再次唤醒需额外 ~700ms。
    舰队语音间隔通常 2-5s，若不保活则每次都吃唤醒开销。

测试方法：
    同一进程内跑两轮，每轮 4 次识别 + 每次间隔 sleep(5s)：
      A. keep_alive=False  -> 期望后几次明显变慢
      B. keep_alive=True   -> 期望 4 次都维持在同一水平

用法：
    .venv/Scripts/python.exe tests/test_gpu_keepalive.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.asr.local_whisper import LocalWhisperProvider  # noqa: E402

WAV = ROOT / "test_audio.wav"
IDLE_S = 5.0
ROUNDS = 4


def load_audio() -> np.ndarray:
    import wave

    with wave.open(str(WAV), "rb") as w:
        assert w.getframerate() == 16000, f"采样率 {w.getframerate()} != 16000"
        assert w.getnchannels() == 1, f"声道数 {w.getnchannels()} != 1"
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def run_round(label: str, audio: np.ndarray, *, keep_alive: bool) -> list[float]:
    print(f"\n{'=' * 62}")
    print(f"  {label}  (keep_alive={keep_alive}, 间隔 {IDLE_S:.0f}s)")
    print(f"{'=' * 62}")

    p = LocalWhisperProvider(
        model="small",
        device="auto",
        compute_type="auto",
        language="en",
        keep_alive=keep_alive,
        keep_alive_interval_s=1.5,
    )
    p.load()
    print(f"  设备: {p.info}")

    lat: list[float] = []
    for i in range(ROUNDS):
        t = time.monotonic()
        r = p.transcribe(audio, language="en")
        el = time.monotonic() - t
        lat.append(el * 1000)
        mark = "  <-- 冷启动" if i == 0 else (f"  (+{el * 1000 - lat[0]:+.0f}ms)" if el * 1000 > lat[0] * 1.3 else "")
        print(f"  第{i + 1}次: {el * 1000:7.0f}ms   「{r.text[:48]}」{mark}")
        if i < ROUNDS - 1:
            time.sleep(IDLE_S)

    p.unload()
    warm = lat[0]
    steady = lat[1:]
    avg_steady = sum(steady) / len(steady)
    print(f"  -- 首次 {warm:.0f}ms / 后续均值 {avg_steady:.0f}ms / 劣化 {avg_steady / warm:.2f}x")
    return lat


def main() -> int:
    if not WAV.exists():
        print(f"[X] 找不到 {WAV}")
        return 1

    audio = load_audio()
    print(f"音频: {WAV.name}  时长 {len(audio) / 16000:.2f}s")

    off = run_round("A. 关闭保活", audio, keep_alive=False)
    time.sleep(2.0)
    on = run_round("B. 开启保活", audio, keep_alive=True)

    print(f"\n{'=' * 62}")
    print("  汇总（单位 ms）")
    print(f"{'=' * 62}")
    print(f"  {'次数':<6}{'关闭保活':>12}{'开启保活':>12}{'改善':>12}")
    for i in range(ROUNDS):
        delta = off[i] - on[i]
        print(f"  #{i + 1:<5}{off[i]:>12.0f}{on[i]:>12.0f}{delta:>+11.0f}")
    off_avg = sum(off[1:]) / len(off[1:])
    on_avg = sum(on[1:]) / len(on[1:])
    print(f"  {'后续均值':<6}{off_avg:>12.0f}{on_avg:>12.0f}{off_avg - on_avg:>+11.0f}")
    print(
        f"\n  结论: 保活{'有效' if on_avg < off_avg * 0.85 else '未见明显收益'}"
        f"（后续均值 {off_avg:.0f}ms -> {on_avg:.0f}ms）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
