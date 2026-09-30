"""实时音频电平监测 + 录制工具（P1 阶段验收用）。

用法:
    # 实时监测 30 秒并录制到 test_audio.wav
    python -m src.tools.audio_monitor

    # 自定义时长
    python -m src.tools.audio_monitor --duration 15 --out my.wav

验收标准：播放任意音频（音乐/视频/Mumble 通话），能看到电平条跳动，
且生成的 wav 能正常播放并听到对应内容。
"""
from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.config import load_config  # noqa: E402
from src.errors import AppError  # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402

BAR_WIDTH = 50

# \r 回退刷新的前提是标准输出为终端；重定向到文件/管道时必须换行输出
_IS_TTY = sys.stdout.isatty()


def _emit(line: str) -> None:
    """输出一行状态（自适应 TTY）。"""
    if _IS_TTY:
        sys.stdout.write("\r" + line + " " * 6)
    else:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _render_bar(level: float, peak: float) -> str:
    """把 RMS 电平画成进度条（对数刻度更直观）。"""
    # 对数映射: -60dB ~ 0dB -> 0 ~ 1
    db = 20 * np.log10(max(level, 1e-6))
    norm = float(np.clip((db + 60) / 60, 0, 1))
    filled = int(norm * BAR_WIDTH)
    color = _IS_TTY

    bar_chars = []
    for i in range(BAR_WIDTH):
        if i < filled:
            ratio = i / BAR_WIDTH
            if not color:
                bar_chars.append("#")
            elif ratio < 0.65:
                bar_chars.append("\033[92m█\033[0m")
            elif ratio < 0.85:
                bar_chars.append("\033[93m█\033[0m")
            else:
                bar_chars.append("\033[91m█\033[0m")
        else:
            bar_chars.append("\033[90m░\033[0m" if color else ".")

    db_str = f"{db:6.1f}dB" if level > 1e-6 else "  -inf "
    pk_str = f"{peak:5.3f}"
    return f"|{''.join(bar_chars)}| {db_str} pk={pk_str}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="实时音频电平监测 + 录制（P1 验收工具）"
    )
    parser.add_argument("--duration", "-d", type=float, default=30.0,
                        help="监测/录制时长（秒），默认 30")
    parser.add_argument("--out", "-o", type=str, default="test_audio.wav",
                        help="输出 wav 路径，默认 test_audio.wav")
    parser.add_argument("--config", "-c", type=str, default=None,
                        help="配置文件路径")
    parser.add_argument("--no-save", action="store_true",
                        help="只监测不保存")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except AppError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        if e.detail:
            print(f"  {e.detail}", file=sys.stderr)
        return 2

    setup_logging(cfg.app.log_level, cfg.log_dir)

    from src.audio.capture import AudioCapture

    print("=" * 78)
    print("  实时音频监测  —  现在请播放音频（音乐 / 视频 / Mumble 通话）")
    print("=" * 78)

    cap = AudioCapture(cfg)
    try:
        cap.start()
    except AppError as e:
        print(f"\n[采集启动失败] {e}", file=sys.stderr)
        if e.detail:
            print(f"  {e.detail}", file=sys.stderr)
        return 3

    dev = cap.device
    print(f"  设备: {dev.name if dev else '(未知)'}")
    print(f"  采样率: {cfg.app.target_sample_rate} Hz  |  块大小: {cfg.audio.block_ms} ms")
    print(f"  时长: {args.duration:.0f} 秒" + ("" if args.no_save else f"  →  保存至 {args.out}"))
    print("-" * 78)

    frames: list[np.ndarray] = []
    silent_blocks = 0
    loud_blocks = 0
    max_level = 0.0
    t0 = time.monotonic()
    last_render = 0.0

    try:
        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= args.duration:
                break

            blk = cap.get_block(timeout=0.3)
            if blk is None:
                continue

            frames.append(blk)
            lv = cap.level
            pk = cap.peak
            max_level = max(max_level, lv)

            if lv < cfg.audio.silence_rms_threshold:
                silent_blocks += 1
            else:
                loud_blocks += 1

            # 限制刷新频率，避免刷屏
            now = time.monotonic()
            if now - last_render >= (0.05 if _IS_TTY else 0.5):
                last_render = now
                remaining = args.duration - elapsed
                _emit(
                    f"[{remaining:5.1f}s] {_render_bar(lv, pk)} "
                    f"块={len(frames):5d} 静音={silent_blocks:5d} 有声={loud_blocks:5d}"
                )
    except KeyboardInterrupt:
        print("\n\n[中断] 用户终止")
    finally:
        cap.stop()

    if _IS_TTY:
        sys.stdout.write("\n")
    print("-" * 78)
    total_blocks = len(frames)
    if total_blocks == 0:
        print("[失败] 未采集到任何音频数据。")
        print("  排查建议:")
        print("    1. 确认系统正在播放声音（音量不为 0）")
        print("    2. 执行 `python -m src.tools.list_devices` 检查设备")
        print("    3. 在 config.yaml 中指定 audio.device_keyword")
        return 4

    speech_ratio = loud_blocks / total_blocks * 100
    audio_dur = len(np.concatenate(frames)) / cfg.app.target_sample_rate
    print(f"  采集块数 : {total_blocks}")
    print(f"  音频时长 : {audio_dur:.2f}s  (目标 {args.duration:.2f}s, "
          f"覆盖率 {audio_dur / args.duration * 100:.0f}%)")
    print(f"  有声比例 : {speech_ratio:.1f}%")
    print(f"  最大电平 : {max_level:.6f}  ({20*np.log10(max(max_level,1e-6)):.1f} dB)")
    print(f"  丢帧次数 : {cap.discontinuities}   队列丢弃: {cap.dropped_blocks}")

    if audio_dur < args.duration * 0.85:
        print(f"\n  [警告] 音频覆盖率仅 {audio_dur/args.duration*100:.0f}%，"
              "可能存在采集丢帧。")
        print("    · 检查是否有其他程序独占音频设备")
        print("    · 尝试增大 audio.block_ms")

    if speech_ratio < 5.0:
        print("\n  [警告] 有声比例过低，可能原因：")
        if cfg.audio.backend == "process":
            print(f"    · Mumble（{cfg.audio.process_name}）里当前没人说话")
            print("    · 目标进程名填错 → 实际抓的是另一个进程")
            print("    · 目标进程不在出声（如只开着界面、未加入频道）")
        else:
            print("    · 系统未播放音频")
            print("    · 采集到了错误的设备")
        print("    · silence_rms_threshold 设置过高")
    else:
        print("\n  [OK] 采集正常，检测到声音信号。")

    if not args.no_save:
        audio = np.concatenate(frames)
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = cfg.project_root / out_path
        pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(cfg.app.target_sample_rate)
            wf.writeframes(pcm16.tobytes())

        dur = len(audio) / cfg.app.target_sample_rate
        size_kb = out_path.stat().st_size / 1024
        print(f"\n  已保存: {out_path}")
        print(f"  时长: {dur:.2f}s   大小: {size_kb:.1f} KB")

    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
