"""P2 验收工具：实时语音转写（控制台输出）。

用法:
    .venv\\Scripts\\python.exe -m src.tools.transcribe_live
    .venv\\Scripts\\python.exe -m src.tools.transcribe_live --duration 60
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.config import load_config  # noqa: E402
from src.errors import AppError  # noqa: E402
from src.logging_setup import get_bound_logger, setup_logging  # noqa: E402
from src.pipeline import Pipeline, TranscriptionEvent  # noqa: E402

log = get_bound_logger(__name__)

# 颜色（不依赖第三方库）
C_PARTIAL = "\033[90m"   # 暗灰
C_FINAL = "\033[97m"     # 亮白
C_TIME = "\033[36m"
C_METRIC = "\033[33m"
C_RESET = "\033[0m"
C_DIM = "\033[2m"


class ConsoleRenderer:
    """把识别事件渲染到控制台。"""

    def __init__(self, show_partial: bool = True, show_metrics: bool = True) -> None:
        self.show_partial = show_partial
        self.show_metrics = show_metrics
        self.count = 0
        self.partial_count = 0
        self.latencies: list[float] = []
        self._last_partial_line = False

    def on_event(self, ev: TranscriptionEvent) -> None:
        if ev.is_partial and not self.show_partial:
            return

        self.count += 1
        if ev.is_partial:
            self.partial_count += 1
        else:
            self.latencies.append(ev.latency_s)

        ts = time.strftime("%H:%M:%S")
        seg = ev.segment

        if ev.is_partial:
            # 半句用暗色，前缀 ↻ 表示会被覆盖
            text = f"{C_PARTIAL}  ↻ {ev.text}{C_RESET}"
        else:
            # 完整句子用亮色 + 时间戳
            text = (
                f"{C_TIME}[{ts}]{C_RESET} "
                f"{C_FINAL}{ev.text}{C_RESET}"
            )

        metrics = ""
        if self.show_metrics:
            metrics = (
                f" {C_DIM}({seg.duration_s:.1f}s音频 "
                f"asr={ev.result.elapsed_s*1000:.0f}ms "
                f"延迟={ev.latency_s*1000:.0f}ms){C_RESET}"
            )

        print(f"{text}{metrics}")

    def summary(self) -> None:
        print()
        print("=" * 78)
        print("  识别统计")
        print("=" * 78)
        print(f"  完整句子 : {self.count - self.partial_count}")
        print(f"  半句触发 : {self.partial_count}")
        if self.latencies:
            arr = sorted(self.latencies)
            n = len(arr)
            print(f"  平均延迟 : {sum(arr)/n*1000:.0f}ms")
            print(f"  中位延迟 : {arr[n//2]*1000:.0f}ms")
            print(f"  最大延迟 : {arr[-1]*1000:.0f}ms")
            p90 = arr[min(n - 1, int(n * 0.9))]
            print(f"  P90延迟  : {p90*1000:.0f}ms")
        print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description="实时语音转写（P2 验收）")
    parser.add_argument("--duration", "-d", type=float, default=60.0,
                        help="运行时长（秒），0 = 直到 Ctrl+C")
    parser.add_argument("--config", "-c", type=str, default=None)
    parser.add_argument("--no-partial", action="store_true",
                        help="不显示半句结果")
    parser.add_argument("--no-metrics", action="store_true",
                        help="不显示耗时指标")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except AppError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        if e.detail:
            print(f"  {e.detail}", file=sys.stderr)
        return 2

    setup_logging(cfg.app.log_level, cfg.log_dir)

    print("=" * 78)
    print("  实时语音转写  —  P2 验收")
    print("=" * 78)
    print(f"  ASR 后端 : {cfg.asr.provider}")

    renderer = ConsoleRenderer(
        show_partial=not args.no_partial,
        show_metrics=not args.no_metrics,
    )

    pipeline = Pipeline(cfg, on_text=renderer.on_event)

    # 加载模型（可能耗时较久）
    print("  正在加载模型，请稍候...")
    try:
        t0 = time.monotonic()
        pipeline.setup()
        print(f"  模型就绪 ({time.monotonic()-t0:.1f}s)  {pipeline.asr_info}")
    except AppError as e:
        print(f"\n[初始化失败] {e}", file=sys.stderr)
        if e.detail:
            print(f"  {e.detail}", file=sys.stderr)
        return 3

    print(f"  VAD      : {'开启' if cfg.vad.enabled else '关闭'}"
          f"  (静音阈值 {cfg.vad.min_silence_ms}ms, "
          f"半句触发 {cfg.vad.partial_trigger_ms}ms)")
    print("-" * 78)
    print("  请开始说话（英语）。按 Ctrl+C 结束。")
    print("-" * 78)

    pipeline.start()
    t_start = time.monotonic()

    try:
        while True:
            elapsed = time.monotonic() - t_start
            if args.duration > 0 and elapsed >= args.duration:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n\n[中断] 用户终止")
    finally:
        pipeline.stop()

    renderer.summary()

    stats = pipeline.stats
    print(f"  音频块   : {stats['blocks_in']}")
    print(f"  总音频   : {stats['total_audio_s']:.1f}s")
    print(f"  错误数   : {stats['errors']}")
    if stats["total_audio_s"] > 0:
        rtf = stats["total_asr_s"] / stats["total_audio_s"]
        print(f"  平均 RTF : {rtf:.3f}")
    print("=" * 78)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
