"""云端 ASR 延迟与稳定性测试。

用途：
    · 验证 API Key 配置是否正确
    · 测量真实网络往返 + 模型推理延迟
    · 模拟舰队节奏（短音频、间隔 2-5s）看延迟抖动

用法：
    先设置 API Key（PowerShell）：
        $env:ASR_API_KEY = "sk-xxxxx"
    或写进 .env（推荐，见 .env.example）

    然后：
        .venv/Scripts/python.exe tests/test_cloud_asr.py
        .venv/Scripts/python.exe tests/test_cloud_asr.py --wav test_audio.wav
        .venv/Scripts/python.exe tests/test_cloud_asr.py --rounds 6 --idle 3
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.asr.cloud_whisper import CloudWhisperProvider  # noqa: E402
from src.config import load_config  # noqa: E402
from src.errors import AppError  # noqa: E402


def load_wav_mono16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    if sr != 16000 or ch != 1:
        raise SystemExit(f"[X] 需要 16k mono WAV，实际 {sr}Hz / {ch}ch")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default="test_audio.wav", help="测试音频路径")
    ap.add_argument("--rounds", type=int, default=5, help="识别轮次")
    ap.add_argument("--idle", type=float, default=3.0, help="轮次间隔秒数")
    ap.add_argument("--clip-s", type=float, default=4.0,
                    help="每轮截取音频长度（模拟单句），0=用完整音频")
    args = ap.parse_args()

    cfg = load_config(str(ROOT / "config.yaml"))
    cloud = cfg.asr.cloud

    print("=" * 64)
    print("  云端 ASR 测试")
    print("=" * 64)
    print(f"  base_url : {cloud.base_url}")
    print(f"  model    : {cloud.model}")
    print(f"  key_env  : {cloud.api_key_env}")

    key = os.environ.get(cloud.api_key_env)
    if not key:
        print(f"\n  [X] 环境变量 {cloud.api_key_env} 未设置\n")
        print("  设置方法（PowerShell）：")
        print(f'      $env:{cloud.api_key_env} = "sk-你的Key"')
        print("  或写进项目根目录的 .env 文件。")
        return 2
    print(f"  api_key  : {key[:6]}...{key[-4:]}（长度 {len(key)}）")

    wav_path = ROOT / args.wav
    if not wav_path.exists():
        print(f"\n  [X] 找不到音频 {wav_path}")
        return 2
    audio = load_wav_mono16k(wav_path)

    clip = audio if args.clip_s <= 0 else audio[: int(16000 * args.clip_s)]
    print(f"  音频     : {wav_path.name}  轮次 {args.rounds}  间隔 {args.idle}s")
    print(f"  每轮输入 : {len(clip) / 16000:.2f}s\n")

    provider = CloudWhisperProvider(
        base_url=cloud.base_url,
        api_key_env=cloud.api_key_env,
        model=cloud.model,
        language=cfg.asr.language or None,
        timeout_s=cloud.timeout_s,
        initial_prompt=cfg.asr.initial_prompt,
        max_retries=cloud.max_retries,
        warmup=cloud.warmup,
    )

    try:
        t0 = time.monotonic()
        provider.load()  # 含连接预热
        print(f"  初始化（含建连预热）: {(time.monotonic() - t0) * 1000:.0f}ms\n")
    except AppError as e:
        print(f"  [X] 初始化失败: {e}")
        if e.detail:
            print(f"      {e.detail}")
        return 1

    lat: list[float] = []
    print(f"  {'轮次':<6}{'耗时':>10}{'RTF':>8}   文本")
    print("  " + "-" * 58)
    for i in range(args.rounds):
        t = time.monotonic()
        try:
            r = provider.transcribe(clip, language=cfg.asr.language or None)
        except AppError as e:
            print(f"  #{i + 1:<5}{'ERR':>10}   {e}")
            lat.append(float("nan"))
            continue
        el = time.monotonic() - t
        lat.append(el)
        rtf = r.realtime_factor
        txt = r.text[:38] if r.text else "(空)"
        print(f"  #{i + 1:<5}{el * 1000:>9.0f}ms{rtf:>8.2f}   {txt}")
        if i < args.rounds - 1 and args.idle > 0:
            time.sleep(args.idle)

    ok = [x for x in lat if x == x]
    print("  " + "-" * 58)
    if ok:
        p90 = sorted(ok)[min(len(ok) - 1, int(len(ok) * 0.9))]
        print(f"  平均 {statistics.mean(ok) * 1000:.0f}ms   "
              f"中位 {statistics.median(ok) * 1000:.0f}ms   "
              f"最大 {max(ok) * 1000:.0f}ms   "
              f"P90 {p90 * 1000:.0f}ms")
        if len(ok) > 1:
            print(f"  抖动（标准差） {statistics.stdev(ok) * 1000:.0f}ms")
        print()
        if max(ok) < 2.0:
            print("  [OK] 最大延迟 <2s，满足舰队实时目标")
        else:
            print("  [!] 存在 >2s 的延迟，需要优化（换更小模型/更近的服务区）")
    provider.unload()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
