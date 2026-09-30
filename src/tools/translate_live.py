"""流式实时翻译（控制台验收）。

跑通「系统音频 -> 云端翻译 -> 中文字幕」整条链路的命令行版本，
也是 P3 悬浮窗之前的验证工具。

用法:
    # 抓系统音频，实时翻译（默认 60 秒）
    .venv\\Scripts\\python.exe -m src.tools.translate_live

    # 指定时长
    .venv\\Scripts\\python.exe -m src.tools.translate_live -d 120

    # 只显示中文，不显示英文原文
    .venv\\Scripts\\python.exe -m src.tools.translate_live --no-original

    # 显示服务端 VAD 的说话起止（排查切句问题用）
    .venv\\Scripts\\python.exe -m src.tools.translate_live --show-vad
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.errors import AppError  # noqa: E402
from src.logging_setup import get_bound_logger, setup_logging  # noqa: E402
from src.pipeline import Pipeline, TranslationTextEvent  # noqa: E402

log = get_bound_logger(__name__)


def load_wav_16k_mono(path: Path) -> np.ndarray:
    """读取 WAV 并转成 16k float32 √mono。

    用简单的线性插值重采样 —— 仅用于测试灌音，
    不需要高质量重采样（scipy 在此处是多余依赖）。
    """
    import wave

    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    if width != 2:
        raise SystemExit(f"[X] 只支持 16bit WAV，实际 {width*8}bit")

    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)

    if sr != 16000:
        # 线性插值重采样
        n_out = int(len(data) * 16000 / sr)
        idx = np.linspace(0, len(data) - 1, n_out)
        data = np.interp(idx, np.arange(len(data)), data).astype(np.float32)

    return data

# 控制台配色（不依赖第三方库）
C_ZH = "\033[97m"        # 亮白 —— 中文译文
C_EN = "\033[90m"        # 暗灰 —— 英文原文
C_PARTIAL = "\033[2m"    # 更暗 —— 增量
C_TIME = "\033[36m"
C_METRIC = "\033[33m"
C_VAD = "\033[35m"
C_ERR = "\033[31m"
C_RESET = "\033[0m"


class ConsoleRenderer:
    """把翻译事件渲染到控制台。

    增量事件用 ``\\r`` 原地刷新（TTY 下体验接近字幕），
    定稿事件换行输出并保留。
    """

    def __init__(
        self,
        show_original: bool = True,
        show_metrics: bool = True,
        show_vad: bool = False,
    ) -> None:
        self.show_original = show_original
        self.show_metrics = show_metrics
        self.show_vad = show_vad

        self.final_count = 0
        self.partial_count = 0
        self.errors = 0
        self.delays: list[float] = []
        self.latencies: list[float] = []  # 从说话结束到收到译文的秒数
        self._last_speech_stop: float | None = None
        self._tty = sys.stdout.isatty()
        self._dirty = False

    def on_event(self, ev: TranslationTextEvent) -> None:
        ts = time.strftime("%H:%M:%S")

        # ---- VAD 事件 ----
        if ev.kind == "speech_start":
            self._last_speech_stop = None
            if self.show_vad:
                self._clear_partial()
                print(f"{C_VAD}  >> 说话开始{C_RESET}")
            return

        if ev.kind == "speech_stop":
            self._last_speech_stop = ev.received_monotonic
            if self.show_vad:
                self._clear_partial()
                print(f"{C_VAD}  << 说话结束{C_RESET}")
            return

        # ---- 错误 ----
        if ev.kind == "error":
            self.errors += 1
            self._clear_partial()
            print(f"{C_ERR}  [!] {ev.translation}{C_RESET}")
            return

        if not ev.is_displayable:
            return

        # ---- 翻译增量 ----
        if ev.is_partial:
            self.partial_count += 1
            if not self._tty:
                return  # 重定向时增量会刷屏，只留定稿
            line = f"{C_PARTIAL}  {ts} {ev.text}{C_RESET}"
            print(line[:200].ljust(78), end="\r", flush=True)
            self._dirty = True
            return

        # ---- 定稿 ----
        self._clear_partial()

        if ev.kind == "original":
            # 原文定稿：只在需要时显示，且给译文留出视觉主位
            if self.show_original and not ev.translation:
                print(f"{C_EN}  [{ts}] 原文: {ev.original}{C_RESET}")
            return

        self.final_count += 1

        if ev.first_text_delay_ms:
            self.delays.append(ev.first_text_delay_ms)
        if self._last_speech_stop is not None:
            self.latencies.append(ev.received_monotonic - self._last_speech_stop)
            self._last_speech_stop = None

        # 主行：中文译文
        print(f"{C_TIME}[{ts}]{C_RESET} {C_ZH}{ev.translation}{C_RESET}")

        # 副行：英文原文 + 指标
        extras: list[str] = []
        if self.show_original and ev.original and ev.original != ev.translation:
            extras.append(f"{C_EN}{ev.original}{C_RESET}")
        if self.show_metrics:
            bits = []
            if ev.first_text_delay_ms:
                bits.append(f"首字={ev.first_text_delay_ms:.0f}ms")
            if ev.language:
                bits.append(ev.language)
            if ev.emotion and ev.emotion not in ("neutral", ""):
                bits.append(ev.emotion)
            if bits:
                extras.append(f"{C_METRIC}({' '.join(bits)}){C_RESET}")
        for e in extras:
            print(f"           {e}")

    def _clear_partial(self) -> None:
        if self._dirty and self._tty:
            print(" " * 78, end="\r")
            self._dirty = False

    def summary(self) -> None:
        print()
        print("=" * 70)
        print("  实时翻译统计")
        print("=" * 70)
        print(f"  定稿条数 : {self.final_count}")
        print(f"  增量条数 : {self.partial_count}")
        print(f"  错误次数 : {self.errors}")

        if self.delays:
            d = sorted(self.delays)
            print(f"  首字延迟 : 中位 {statistics.median(d):.0f}ms  "
                  f"平均 {statistics.mean(d):.0f}ms  "
                  f"最大 {max(d):.0f}ms")
            if statistics.median(d) < 2000:
                print("             [OK] 满足 <2s 目标")
            else:
                print("             [!] 超过 2s 目标")

        if self.latencies:
            lat = sorted(self.latencies)
            print(f"  句尾→译文: 中位 {statistics.median(lat)*1000:.0f}ms  "
                  f"平均 {statistics.mean(lat)*1000:.0f}ms  "
                  f"最大 {max(lat)*1000:.0f}ms")
            print("             （此值含服务端 VAD 判静音等待 + 翻译 + 网络）")

        print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="流式实时翻译（Qwen3.5-LiveTranslate）"
    )
    parser.add_argument("--duration", "-d", type=float, default=60.0,
                        help="运行时长（秒），0 = 直到 Ctrl+C")
    parser.add_argument("--config", "-c", type=str, default=None)
    parser.add_argument("--no-original", action="store_true",
                        help="不显示英文原文")
    parser.add_argument("--no-metrics", action="store_true",
                        help="不显示延迟指标")
    parser.add_argument("--show-vad", action="store_true",
                        help="显示服务端 VAD 的说话起止事件")
    parser.add_argument("--device", type=str, default=None,
                        help="指定采集设备名关键字（覆盖配置）")
    parser.add_argument("--wav", type=str, default=None,
                        help="不抓系统音频，改为把 WAV 按实时速度灌进流水线"
                             "（用于无声卡环境的确定性验证）")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="--wav 时的播放倍速，1.0=实时")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except AppError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        if e.detail:
            print(f"  {e.detail}", file=sys.stderr)
        return 2

    setup_logging(cfg.app.log_level, cfg.log_dir)

    if (cfg.asr.provider or "").lower() != "livetranslate":
        print(f"[!] 当前 asr.provider = {cfg.asr.provider}，"
              f"本工具只支持 livetranslate", file=sys.stderr)
        print("    请把 config.yaml 中 asr.provider 改为 livetranslate，"
              "或改用 src.tools.transcribe_live", file=sys.stderr)
        return 2

    if args.device:
        cfg.audio.device_keyword = args.device

    lt = cfg.asr.livetranslate
    print("=" * 70)
    print("  流式实时翻译  —  Qwen3.5-LiveTranslate")
    print("=" * 70)
    print(f"  模型     : {lt.model}")
    print(f"  目标语言 : {lt.target_language}")
    print(f"  热词表   : {len(lt.phrases)} 条"
          + (f"（外部 {lt.phrases_file} {lt._phrases_external_count} 条"
             f" + 内联 {len(lt._phrases_inline)} 条）"
             if lt.phrases_file else ""))
    print(f"  服务端VAD: 静音判句 {lt.silence_duration_ms}ms"
          f"  阈值 {lt.vad_threshold}")
    print(f"  原文识别 : {'开' if lt.with_original else '关'}")

    renderer = ConsoleRenderer(
        show_original=not args.no_original,
        show_metrics=not args.no_metrics,
        show_vad=args.show_vad,
    )

    pipeline = Pipeline(
        cfg,
        on_text=lambda ev: None,          # 流式模式下不用这个
        on_translation=renderer.on_event,
        on_error=lambda e: log.error("流水线错误", error=str(e)),
    )

    print("-" * 70)
    print("  正在建立云端连接...")
    try:
        pipeline.setup()
        if args.wav:
            pipeline.start_without_capture()
        else:
            pipeline.start()
    except AppError as e:
        print(f"\n[启动失败] {e}", file=sys.stderr)
        if e.detail:
            print(f"  {e.detail}", file=sys.stderr)
        return 3

    print(f"  连接就绪 ({pipeline.connect_seconds:.2f}s)")
    print("-" * 70)

    t_start = time.monotonic()
    try:
        if args.wav:
            wav_path = Path(args.wav)
            if not wav_path.is_absolute():
                wav_path = ROOT / wav_path
            if not wav_path.exists():
                print(f"[X] 找不到 {wav_path}", file=sys.stderr)
                return 2
            audio = load_wav_16k_mono(wav_path)
            dur = len(audio) / 16000.0
            print(f"  灌入 {wav_path.name}"
                  f"（{dur:.1f}s，倍速 {args.speed:g}×）")
            step = 1600                       # 100ms
            sleep_s = 0.1 / max(args.speed, 1e-6)
            for i in range(0, len(audio), step):
                pipeline.push_audio_raw(audio[i: i + step])
                time.sleep(sleep_s)
            # 留出时间让云端把最后一句吐完
            print("  音频灌完，等待云端输出…")
            time.sleep(3.0)
        else:
            print("  请播放英语语音（Mumble / 视频 / TTS）。按 Ctrl+C 结束。")
            print("-" * 70)
            while True:
                elapsed = time.monotonic() - t_start
                if args.duration > 0 and elapsed >= args.duration:
                    break
                time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n\n[中断] 用户终止")
    finally:
        print("\n  结束会话，等待最后一句翻译...")
        pipeline.stop()

    renderer.summary()

    stats = pipeline.stats
    print(f"  音频块   : {stats['blocks_in']}")
    print(f"  总音频   : {stats['total_audio_s']:.1f}s")
    print(f"  丢弃块   : {stats.get('dropped_tasks', 0)}")
    print(f"  错误数   : {stats['errors']}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
