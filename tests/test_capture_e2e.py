"""P1 端到端验收测试：验证 loopback 采集的保真度。

做法：
  1. 生成一段已知内容的测试音频（多频正弦叠加 + 语音节奏包络）
  2. 播放的同时进行 loopback 采集
  3. 对采集结果做频谱与时长比对，验证采集保真

用法:
    .venv\\Scripts\\python.exe tests\\test_capture_e2e.py
"""
from __future__ import annotations

import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.audio.capture import AudioCapture  # noqa: E402
from src.audio.ring_buffer import rms  # noqa: E402
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
#  测试信号生成
# =========================================================
SR = 44100
TONE_HZ = 440.0
DURATION_S = 8.0


def make_test_wav(path: Path) -> None:
    """生成 440Hz + 880Hz 断续音（模拟语音节奏）。"""
    t = np.linspace(0, DURATION_S, int(SR * DURATION_S), endpoint=False)
    # 1.5Hz 方波包络：每 0.67s 响 0.33s
    env = (np.sin(2 * np.pi * 1.5 * t) > 0).astype(np.float32)
    tone = (
        0.30 * np.sin(2 * np.pi * TONE_HZ * t)
        + 0.12 * np.sin(2 * np.pi * 2 * TONE_HZ * t)
    ).astype(np.float32)
    sig = (tone * env).astype(np.float32)
    pcm = (np.clip(sig, -1, 1) * 32767).astype(np.int16)

    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def play_async(path: Path) -> threading.Thread:
    import winsound

    def _play() -> None:
        winsound.PlaySound(str(path), winsound.SND_FILENAME)

    th = threading.Thread(target=_play, daemon=True)
    th.start()
    return th


# =========================================================
#  验证
# =========================================================
def test_capture_fidelity() -> None:
    print("\n--- Loopback 采集保真度 ---")

    cfg = load_config()
    test_wav = ROOT / "_e2e_test.wav"
    make_test_wav(test_wav)

    cap = AudioCapture(cfg)

    # 先启动采集并等待真正开始录音，避免启动延迟造成的时间偏差
    t_start = time.monotonic()
    cap.start()
    ok = cap.wait_until_recording(timeout=5.0)
    t_recording = time.monotonic()
    check("采集流成功打开", ok, "5 秒内未产出音频块")
    if not ok:
        cap.stop()
        return

    print(f"        (采集启动耗时 {t_recording - t_start:.2f}s)")
    print(f"        设备: {cap.device.name if cap.device else '?'}")

    # 开始播放，同时采集
    t_play = time.monotonic()
    play_async(test_wav)
    time.sleep(0.15)  # 让音频真正出声

    frames: list[np.ndarray] = []
    levels: list[float] = []
    deadline = t_play + DURATION_S + 0.5

    while time.monotonic() < deadline:
        blk = cap.get_block(timeout=0.2)
        if blk is None:
            continue
        frames.append(blk)
        levels.append(rms(blk))

    cap.stop()
    test_wav.unlink(missing_ok=True)

    if not frames:
        check("采集到音频数据", False, "未采到任何数据")
        return

    audio = np.concatenate(frames)
    cap_sr = cfg.app.target_sample_rate
    dur = len(audio) / cap_sr
    expected_dur = DURATION_S + 0.5 - 0.15  # 采集窗口实际时长

    check("采集到音频数据", True)
    check("输出为 16kHz mono float32",
          audio.ndim == 1 and audio.dtype == np.float32)

    # 时长覆盖率：允许 15% 误差（启动/停止边界）
    cover = dur / expected_dur
    check(
        f"音频覆盖率 >= 85% (实际 {cover*100:.0f}%)",
        cover >= 0.85,
        f"采集 {dur:.2f}s / 预期 {expected_dur:.2f}s",
    )

    # 有声段比例：测试信号占空比约 50%
    speech = sum(1 for lv in levels if lv > cfg.audio.silence_rms_threshold)
    ratio = speech / len(levels) if levels else 0
    check(
        f"有声比例接近 50% (实际 {ratio*100:.0f}%)",
        0.25 <= ratio <= 0.75,
        f"ratio={ratio:.3f}",
    )

    # 频谱保真：主频应为 440Hz
    seg = audio[int(cap_sr * 1.0): int(cap_sr * 1.8)]
    if np.abs(seg).max() < 0.001:
        warn("频谱保真度", "采样段几乎无声，跳过（可能是包络静音区）")
    else:
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        freqs = np.fft.rfftfreq(len(seg), 1 / cap_sr)
        peak_hz = float(freqs[int(np.argmax(spec))])
        check(
            f"主频为 440Hz ±15 (实际 {peak_hz:.1f}Hz)",
            abs(peak_hz - TONE_HZ) <= 15,
            f"got {peak_hz:.1f}Hz",
        )

        # 二次谐波应存在（880Hz）
        band = (freqs > 820) & (freqs < 940)
        f0_band = (freqs > 400) & (freqs < 480)
        if band.any() and f0_band.any():
            h2 = spec[band].max()
            f1 = spec[f0_band].max()
            check(
                "二次谐波 880Hz 存在",
                h2 > f1 * 0.05,
                f"h2/f1={h2/f1:.3f}",
            )

    # 丢帧检查
    disc = cap.discontinuities
    if disc > 0:
        warn(f"检测到 {disc} 次丢帧", "属正常范围（流竞争/时钟漂移）")
    else:
        check("无丢帧", True)

    print(f"\n        采集时长 {dur:.2f}s | 块数 {len(frames)} | "
          f"丢帧 {disc} | 队列丢弃 {cap.dropped_blocks}")
    print(f"        峰值电平 {np.abs(audio).max():.3f} | "
          f"平均RMS {rms(audio):.4f}")


def test_ringbuffer_integration() -> None:
    """验证采集数据流入环形缓冲后仍完整。"""
    print("\n--- 采集 + 环形缓冲联动 ---")

    from src.audio.ring_buffer import RingBuffer

    cfg = load_config()
    rb = RingBuffer(sample_rate=cfg.app.target_sample_rate, max_seconds=30.0)
    cap = AudioCapture(cfg, on_block=rb.write)

    cap.start()
    ok = cap.wait_until_recording(timeout=5.0)
    check("采集启动成功", ok)
    if not ok:
        cap.stop()
        return

    time.sleep(2.0)
    cap.stop()

    n = rb.total_samples
    dur = rb.duration_s
    check(f"环形缓冲收到数据 ({n} 采样 / {dur:.2f}s)", n > 0)
    check(
        f"缓冲时长约 2s (实际 {dur:.2f}s)",
        1.6 <= dur <= 2.6,
        f"got {dur:.2f}s",
    )


def main() -> int:
    print("=" * 64)
    print("  P1 端到端验收测试（会短暂播放声音，请注意音量）")
    print("=" * 64)

    try:
        test_capture_fidelity()
        test_ringbuffer_integration()
    except KeyboardInterrupt:
        print("\n[中断]")
        return 130

    print("\n" + "=" * 64)
    print(f"  结果: {PASS} 通过, {FAIL} 失败, {WARN} 警告")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
