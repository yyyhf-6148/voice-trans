"""ASR 基准测试：测出各配置在真实语音上的耗时。

目的：用数据决定跑本地还是云端 ASR。

用法:
    .venv\\Scripts\\python.exe tests\\bench_asr.py
    .venv\\Scripts\\python.exe tests\\bench_asr.py --model small --types float16,float32,int8
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# =========================================================
#  测试音频生成（用 TTS 或合成语音样例）
# =========================================================
SAMPLE_SENTENCES = [
    "Enemy fleet jumping in through the gate, get ready to engage.",
    "I need capacitor, I am running out of juice here.",
    "Tackle the primary target and hold it down.",
    "Warp to the out gate and align to the next system.",
]


def make_silence(duration_s: float, sr: int = 16000) -> np.ndarray:
    return np.zeros(int(sr * duration_s), dtype=np.float32)


def make_speech_like(duration_s: float, sr: int = 16000) -> np.ndarray:
    """生成类语音信号（用于测量推理耗时，不是测准确率）。

    用多个共振峰 + 包络调制模拟语音的频谱特征，
    让 Whisper 有实质计算量（否则静音会被 VAD 跳过，测不出真实耗时）。
    """
    n = int(sr * duration_s)
    t = np.linspace(0, duration_s, n, endpoint=False)
    # 基频 + 共振峰
    sig = np.zeros(n, dtype=np.float64)
    for f0, amp in [(120, 0.5), (240, 0.3), (360, 0.2), (800, 0.15),
                    (1200, 0.12), (2400, 0.08)]:
        sig += amp * np.sin(2 * np.pi * f0 * t + np.random.rand() * 2 * np.pi)
    # 音节包络（4Hz 调制）
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)
    sig *= env
    sig = sig / (np.abs(sig).max() + 1e-9) * 0.6
    return sig.astype(np.float32)


# =========================================================
#  基准测试核心
# =========================================================
def bench_config(
    model_name: str,
    device: str,
    compute_type: str,
    audio: np.ndarray,
    runs: int = 3,
    warmup: int = 1,
) -> dict | None:
    """测一组配置的耗时。返回 None 表示该配置不可用。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("  [错误] faster-whisper 未安装")
        return None

    label = f"{model_name}/{device}/{compute_type}"
    print(f"\n  ── {label} ──")

    # 加载模型
    t0 = time.monotonic()
    try:
        cpu_threads = 8 if device == "cpu" else 4
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            num_workers=1,
        )
    except Exception as e:
        print(f"     [不可用] {type(e).__name__}: {str(e)[:120]}")
        return {"label": label, "available": False, "error": str(e)[:200]}

    load_s = time.monotonic() - t0
    print(f"     模型加载: {load_s:.2f}s")

    # 预热（第一次推理含 cuDNN 初始化等开销）
    try:
        for _ in range(warmup):
            segs, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=False)
            list(segs)  # 强制消费生成器
    except Exception as e:
        print(f"     [预热失败] {type(e).__name__}: {str(e)[:120]}")
        return {"label": label, "available": False, "error": str(e)[:200]}

    # 正式计时
    times: list[float] = []
    for i in range(runs):
        t = time.monotonic()
        segs, info = model.transcribe(
            audio, language="en", beam_size=1, vad_filter=False
        )
        seg_list = list(segs)
        dt = time.monotonic() - t
        times.append(dt)

    audio_dur = len(audio) / 16000
    avg = float(np.mean(times))
    mn = float(np.min(times))
    rtf = avg / audio_dur  # 实时因子：<1 表示比实时快

    print(f"     音频时长: {audio_dur:.2f}s")
    print(f"     耗时: {', '.join(f'{x:.3f}s' for x in times)}")
    print(f"     平均: {avg:.3f}s  最快: {mn:.3f}s")
    print(f"     RTF : {rtf:.3f}  ({'实时可跑' if rtf < 1 else '慢于实时!'})")

    # 释放显存
    del model
    try:
        import torch

        if device == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    return {
        "label": label,
        "available": True,
        "load_s": round(load_s, 2),
        "avg_s": round(avg, 3),
        "min_s": round(mn, 3),
        "rtf": round(rtf, 3),
        "audio_dur": round(audio_dur, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ASR 基准测试")
    parser.add_argument("--model", default="small", help="模型名（tiny/base/small/medium）")
    parser.add_argument("--types", default="float16,float32,int8_float16,int8",
                        help="逗号分隔的 compute_type 列表")
    parser.add_argument("--duration", type=float, default=5.0,
                        help="测试音频时长（秒）")
    parser.add_argument("--runs", type=int, default=3, help="每配置重复次数")
    parser.add_argument("--json", type=str, default=None, help="结果写入 JSON 文件")
    args = parser.parse_args()

    print("=" * 70)
    print("  ASR 基准测试")
    print("=" * 70)

    # 环境信息
    import platform

    print(f"  Python : {platform.python_version()}")
    print(f"  平台   : {platform.system()} {platform.release()}")
    try:
        import torch

        print(f"  torch  : {torch.__version__}  CUDA={torch.cuda.is_available()}")
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            print(f"  GPU    : {p.name}  CC={p.major}.{p.minor}  "
                  f"{p.total_memory/1024**3:.1f}GB")
    except ImportError:
        print("  torch  : 未安装")

    # 生成测试音频
    audio = make_speech_like(args.duration)
    print(f"  测试音频: {args.duration}s 类语音信号（仅测速度，不测准确率）")

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    results: list[dict] = []

    # ---- GPU ----
    try:
        import torch

        has_cuda = torch.cuda.is_available()
    except ImportError:
        has_cuda = False

    if has_cuda:
        print("\n[GPU 测试]")
        for ct in types:
            r = bench_config(args.model, "cuda", ct, audio, runs=args.runs)
            if r:
                results.append(r)
    else:
        print("\n[GPU 测试] 跳过 — CUDA 不可用")

    # ---- CPU（作为对照）----
    print("\n[CPU 测试] 对照参考")
    for ct in ["int8", "float32"]:
        if ct in types or ct == "int8":
            r = bench_config(args.model, "cpu", ct, audio, runs=max(1, args.runs // 2))
            if r:
                results.append(r)

    # ---- 汇总 ----
    print("\n" + "=" * 70)
    print("  汇总")
    print("=" * 70)
    ok = [r for r in results if r.get("available")]
    ok.sort(key=lambda r: r["avg_s"])

    if not ok:
        print("  没有可用的配置。")
        return 1

    print(f"\n  {'配置':<40} {'平均':>8} {'RTF':>7} {'判定':>10}")
    print("  " + "-" * 68)
    for r in ok:
        verdict = "优秀" if r["rtf"] < 0.3 else ("可用" if r["rtf"] < 0.7 else "勉强")
        print(f"  {r['label']:<40} {r['avg_s']:>7.3f}s {r['rtf']:>7.3f} {verdict:>10}")

    failed = [r for r in results if not r.get("available")]
    if failed:
        print("\n  不可用的配置:")
        for r in failed:
            err = r.get("error", "")[:80].replace("\n", " ")
            print(f"    ✗ {r['label']}: {err}")

    # ---- 建议 ----
    best = ok[0]
    print("\n" + "=" * 70)
    print("  建议")
    print("=" * 70)
    if best["rtf"] < 0.3:
        print(f"  ✅ 用本地 ASR：{best['label']}")
        print(f"     单句 {best['avg_s']:.2f}s，链路预估 "
              f"{best['avg_s'] + 0.15 + 0.2 + 0.4:.2f}s（含VAD+翻译）")
    elif best["rtf"] < 0.7:
        print(f"  ⚠️  本地勉强可用：{best['label']}（{best['avg_s']:.2f}s/句）")
        print("     延迟余量紧张，若网络差建议改用云端 ASR")
    else:
        print(f"  ❌ 本地太慢（{best['avg_s']:.2f}s/句），建议改用云端 ASR")

    if args.json:
        Path(args.json).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n  结果已写入: {args.json}")

    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
