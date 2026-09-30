"""本地 faster-whisper 实现。

关键适配点：
  · Pascal 架构（CC 6.x，如 P106/P100）不支持 FP16，
    CTranslate2 会直接抛 ValueError，必须用 float32
    → 实测 P106-100: float32 RTF 0.08（比实时快 12 倍）
  · 支持半句识别（is_partial）以降低舰队场景首字延迟
  · GPU 保活（keep-alive）：矿卡/独显在空闲后会降频，
    再次唤醒需 ~700ms。舰队语音间隔通常 2-5s，
    若不保活则每次都吃唤醒开销（实测 391ms → 1110ms）
"""
from __future__ import annotations

import threading
import time

import numpy as np

from ..errors import DeviceUnavailableError, ModelLoadError
from ..logging_setup import get_bound_logger
from .base import (
    AsrProvider,
    TranscriptionResult,
    TranscriptionSegment,
    detect_device,
)

log = get_bound_logger(__name__)


class GpuKeepAlive:
    """GPU 保活：周期性执行极小计算，阻止显卡进入深度降频状态。

    原理：
      显卡在无负载数百毫秒后会降低时钟频率。
      下次有负载时需要额外时间恢复高频（P106 实测约 700ms）。
      定期提交一个极小的 kernel 可以让驱动维持较高性能状态。

    开销：每周期约 0.1-0.3ms，可忽略。
    """

    def __init__(self, interval_s: float = 1.5) -> None:
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tensor = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            import torch

            if not torch.cuda.is_available():
                return
            # 预分配一个极小的 CUDA 张量，反复做 matmul 保持 GPU 活跃
            self._tensor = torch.randn(8, 8, device="cuda")
        except Exception as e:
            log.debug("GPU 保活初始化失败，跳过", error=str(e))
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="GpuKeepAlive", daemon=True
        )
        self._thread.start()
        log.info("GPU 保活已启用", interval_s=self.interval_s)

    def _loop(self) -> None:
        import torch

        assert self._tensor is not None
        while not self._stop.is_set():
            try:
                with torch.no_grad():
                    # 极小计算，约 0.1ms，足以维持时钟
                    _ = self._tensor @ self._tensor
            except Exception:
                pass
            self._stop.wait(self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self._tensor = None


class LocalWhisperProvider(AsrProvider):
    """基于 faster-whisper 的本地识别。"""

    name = "local-whisper"

    def __init__(
        self,
        *,
        model: str = "small",
        device: str = "auto",
        compute_type: str = "auto",
        language: str | None = "en",
        initial_prompt: str = "",
        beam_size: int = 1,
        vad_filter: bool = False,
        cpu_threads: int = 8,
        download_root: str | None = None,
        keep_alive: bool = True,
        keep_alive_interval_s: float = 1.5,
    ) -> None:
        self.model_name = model
        self.device_pref = device
        self.compute_type_pref = compute_type
        self.language = language or None
        self.initial_prompt = initial_prompt or None
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.cpu_threads = cpu_threads
        self.download_root = download_root
        self.keep_alive_enabled = keep_alive
        self.keep_alive_interval_s = keep_alive_interval_s

        self._model = None
        self._device = ""
        self._compute_type = ""
        self._keepalive: GpuKeepAlive | None = None
        # 推理锁：WhisperModel 非线程安全，且避免保活线程干扰
        self._infer_lock = threading.Lock()

    # ---------------- 生命周期 ----------------
    def load(self) -> None:
        if self._model is not None:
            return

        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise ModelLoadError(
                "缺少 faster-whisper",
                detail="pip install faster-whisper",
            ) from e

        device, compute_type = detect_device(self.device_pref)

        # 用户显式指定 compute_type 时以用户为准（但仍做合法性检查）
        if self.compute_type_pref != "auto":
            compute_type = self.compute_type_pref

        t0 = time.monotonic()
        try:
            self._model = WhisperModel(
                self.model_name,
                device=device,
                compute_type=compute_type,
                cpu_threads=self.cpu_threads if device == "cpu" else 0,
                num_workers=1,
                download_root=self.download_root,
            )
        except ValueError as e:
            # Pascal 上 float16 会走到这里，自动降级重试一次
            msg = str(e).lower()
            if "float16" in msg and compute_type != "float32":
                log.warning(
                    "float16 不受支持，自动降级为 float32",
                    device=device,
                    original_compute_type=compute_type,
                )
                compute_type = "float32"
                try:
                    self._model = WhisperModel(
                        self.model_name,
                        device=device,
                        compute_type=compute_type,
                        num_workers=1,
                        download_root=self.download_root,
                    )
                except Exception as e2:
                    raise ModelLoadError(
                        f"加载 Whisper 模型失败（已降级 float32）: {e2}",
                        detail=f"model={self.model_name} device={device}",
                    ) from e2
            else:
                raise ModelLoadError(
                    f"加载 Whisper 模型失败: {e}",
                    detail=f"model={self.model_name} device={device} "
                    f"compute_type={compute_type}",
                ) from e
        except Exception as e:
            if device == "cuda":
                raise DeviceUnavailableError(
                    f"CUDA 上加载模型失败: {e}",
                    detail="可尝试在 config.yaml 设置 asr.local.device = cpu，"
                    "或改用云端 ASR（asr.provider = cloud）",
                ) from e
            raise ModelLoadError(
                f"加载 Whisper 模型失败: {e}",
                detail=f"model={self.model_name} device={device}",
            ) from e

        self._device = device
        self._compute_type = compute_type
        elapsed = time.monotonic() - t0

        log.info(
            "Whisper 模型已加载",
            model=self.model_name,
            device=device,
            compute_type=compute_type,
            load_s=round(elapsed, 2),
        )

        # 启动 GPU 保活，避免空闲降频导致的唤醒开销
        if self.keep_alive_enabled and device == "cuda":
            self._keepalive = GpuKeepAlive(self.keep_alive_interval_s)
            self._keepalive.start()

    def unload(self) -> None:
        if self._keepalive is not None:
            self._keepalive.stop()
            self._keepalive = None

        self._model = None
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    # ---------------- 识别 ----------------
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        is_partial: bool = False,
    ) -> TranscriptionResult:
        if self._model is None:
            self.load()

        audio = self.validate_audio(audio)
        audio_dur = len(audio) / 16000.0

        if audio.size == 0:
            return TranscriptionResult(language=language or self.language or "")

        lang = language or self.language

        # 半句识别用更激进的参数换低延迟
        beam = 1 if is_partial else self.beam_size

        t0 = time.monotonic()
        try:
            # WhisperModel 非线程安全；锁同时避免保活线程与推理竞争 CUDA
            with self._infer_lock:
                segments, info = self._model.transcribe(
                    audio,
                    language=lang,
                    beam_size=beam,
                    initial_prompt=self.initial_prompt,
                    vad_filter=self.vad_filter,
                    condition_on_previous_text=False,  # 避免上下文污染导致的幻觉
                    word_timestamps=False,
                )
                # 注意：faster-whisper 是惰性生成器，必须在此处消费完
                seg_list = list(segments)
        except Exception as e:
            log.error("识别失败", error=str(e), audio_dur=round(audio_dur, 2))
            return TranscriptionResult(
                language=lang or "", elapsed_s=time.monotonic() - t0,
                audio_duration_s=audio_dur,
            )

        elapsed = time.monotonic() - t0

        out_segments = [
            TranscriptionSegment(
                text=s.text.strip(),
                start_s=float(s.start),
                end_s=float(s.end),
                avg_logprob=float(s.avg_logprob) if s.avg_logprob is not None else None,
                is_partial=is_partial,
            )
            for s in seg_list
            if s.text.strip()
        ]

        result = TranscriptionResult(
            segments=out_segments,
            language=getattr(info, "language", lang or "") or "",
            language_probability=float(
                getattr(info, "language_probability", 0.0) or 0.0
            ),
            elapsed_s=elapsed,
            audio_duration_s=audio_dur,
        )

        log.debug(
            "识别完成",
            text=result.text[:60] or "(空)",
            audio_s=round(audio_dur, 2),
            elapsed_s=round(elapsed, 3),
            rtf=round(result.realtime_factor, 3),
            partial=is_partial,
        )
        return result

    @property
    def info(self) -> str:
        return (
            f"{self.name}({self.model_name}/{self._device or '?'}"
            f"/{self._compute_type or '?'})"
        )
