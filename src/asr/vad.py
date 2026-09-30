"""语音活动检测（VAD）+ 句子切分。

用 Silero VAD 判定「是否在说话」，产出完整句子。

低延迟关键设计 —— 半句触发（partial trigger）:
  舰队指挥场景要求「听到就译」，不能等一句话说完。
  做法：语音持续超过 partial_trigger_ms 时，先送一次识别，
  句子真正结束后再送一次完整音频覆盖结果。用户先看到粗略译文，
  1 秒后看到精确译文，主观延迟大幅下降。

状态机:
    SILENCE ──speech──> SPEAKING ──silence>min──> 产出句子
       ↑                    │
       └────句子过短丢弃─────┘
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto

import numpy as np

from ..config import VadSection
from ..errors import ASRError
from ..logging_setup import get_bound_logger

log = get_bound_logger(__name__)


class VadState(Enum):
    SILENCE = auto()
    SPEAKING = auto()


@dataclass
class SpeechSegment:
    """一个完整的语音段。"""

    audio: np.ndarray          # 16kHz mono float32
    start_s: float             # 相对会话开始的秒数
    end_s: float
    trigger: str               # "silence" | "max_duration"

    @property
    def duration_s(self) -> float:
        return len(self.audio) / 16000.0

    def __str__(self) -> str:
        return (
            f"[{self.start_s:.2f}-{self.end_s:.2f}s] "
            f"{self.duration_s:.2f}s ({self.trigger})"
        )


@dataclass
class PartialEvent:
    """半句触发事件：携带当前累积的音频，供提前识别。"""

    audio: np.ndarray
    start_s: float
    end_s: float
    index: int  # 同一句话内的第几次半句触发（从 1 开始）

    @property
    def duration_s(self) -> float:
        return len(self.audio) / 16000.0


class VadSegmenter:
    """基于 Silero VAD 的句子切分器。

    用法::

        seg = VadSegmenter(cfg.vad)
        seg.load()
        for block in audio_blocks:          # 每块 16kHz mono float32
            for ev in seg.push(block):      # 返回事件列表
                if isinstance(ev, SpeechSegment):
                    ...   # 完整句子
                elif isinstance(ev, PartialEvent):
                    ...   # 半句，可提前识别
    """

    SAMPLE_RATE = 16000

    def __init__(self, section: VadSection) -> None:
        self.cfg = section
        self._model = None
        self._state = VadState.SILENCE
        self._buffer: list[np.ndarray] = []
        self._buf_samples = 0
        self._elapsed_samples = 0          # 会话累计采样（用于时间戳）
        self._speech_start_sample = 0
        self._silence_samples = 0
        self._last_partial_at = 0          # 上次半句触发时的缓冲区采样数
        self._partial_index = 0
        self._pending_partial: np.ndarray | None = None  # 已发出但未定稿的音频
        # Silero 要求固定窗口，这里用 512 采样（32ms @16k）
        self._window = 512

    # ---------------- 生命周期 ----------------
    def load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
        except ImportError as e:
            raise ASRError(
                "缺少 torch，无法使用 Silero VAD",
                detail="pip install torch。或设置 vad.enabled = false 关闭 VAD",
            ) from e

        t0 = time.monotonic()
        try:
            # torch.hub 会缓存到 ~/.cache/torch/hub
            self._model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                force_reload=False,
                onnx=False,
                trust_repo=True,
            )
            self._get_speech_timestamps = utils[0]
        except Exception as e:
            raise ASRError(
                f"加载 Silero VAD 失败: {e}",
                detail="首次运行需联网下载模型（约 2MB）。"
                "也可设置 vad.enabled = false 关闭 VAD",
            ) from e

        self._torch = torch
        self._model.eval()
        log.info("Silero VAD 已加载", load_s=round(time.monotonic() - t0, 2))

    def reset(self) -> None:
        self._state = VadState.SILENCE
        self._buffer.clear()
        self._buf_samples = 0
        self._speech_start_sample = 0
        self._silence_samples = 0
        self._last_partial_at = 0
        self._partial_index = 0
        self._pending_partial = None
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:
                pass

    # ---------------- 主入口 ----------------
    def push(self, block: np.ndarray) -> list[SpeechSegment | PartialEvent]:
        """送入一块音频，返回本次产生的事件（可能为空/多个）。"""
        events: list[SpeechSegment | PartialEvent] = []

        if self._model is None:
            self.load()

        if block.dtype != np.float32:
            block = block.astype(np.float32, copy=False)
        if block.ndim > 1:
            block = block.reshape(-1)
        if block.size == 0:
            return events

        # VAD 需要固定窗口，把 block 切片送入
        probs = self._speech_probabilities(block)

        # 逐窗口推进状态机
        n_windows = len(probs)
        for i, prob in enumerate(probs):
            is_speech = prob >= self.cfg.threshold
            w_start = i * self._window
            w_end = w_start + self._window
            chunk = block[w_start:w_end]
            self._elapsed_samples += len(chunk)

            if self._state is VadState.SILENCE:
                if is_speech:
                    self._on_speech_start()
                    self._buffer.append(chunk)
                    self._buf_samples += len(chunk)
                    self._state = VadState.SPEAKING
                # 静音期不缓存（省内存）
            else:  # SPEAKING
                self._buffer.append(chunk)
                self._buf_samples += len(chunk)

                if is_speech:
                    self._silence_samples = 0
                else:
                    self._silence_samples += len(chunk)

                # 1) 静音够久 -> 句子结束
                if self._silence_samples >= self._ms_to_samples(self.cfg.min_silence_ms):
                    seg = self._finalize("silence")
                    if seg is not None:
                        events.append(seg)
                    continue

                # 2) 语音过长 -> 强制切分（防止长句无限累积）
                speech_samples = self._buf_samples - self._silence_samples
                if self._ms_to_samples(self.cfg.max_speech_ms) > 0 and \
                        speech_samples >= self._ms_to_samples(self.cfg.max_speech_ms):
                    seg = self._finalize("max_duration")
                    if seg is not None:
                        events.append(seg)
                    continue

                # 3) 半句触发 -> 提前识别（低延迟关键）
                if self.cfg.partial_trigger_ms > 0:
                    since_last = self._buf_samples - self._last_partial_at
                    if since_last >= self._ms_to_samples(self.cfg.partial_trigger_ms):
                        # 只在已经安静下来一点时才触发，避免把词切碎
                        if self._silence_samples >= self._ms_to_samples(120):
                            partial = self._make_partial()
                            if partial is not None:
                                events.append(partial)

        return events

    def flush(self) -> SpeechSegment | None:
        """结束会话时把残留音频定稿。"""
        if self._state is VadState.SPEAKING and self._buf_samples > 0:
            return self._finalize("flush")
        return None

    # ---------------- 内部 ----------------
    def _ms_to_samples(self, ms: int) -> int:
        return int(self.SAMPLE_RATE * ms / 1000)

    def _on_speech_start(self) -> None:
        """进入说话状态，回填 padding_before 的音频。"""
        self._speech_start_sample = self._elapsed_samples
        self._silence_samples = 0
        self._last_partial_at = 0
        self._partial_index = 0
        self._pending_partial = None

    def _speech_probabilities(self, block: np.ndarray) -> np.ndarray:
        """对整块音频逐窗口计算语音概率。"""
        torch = self._torch
        win = self._window
        n = len(block) // win
        if n == 0:
            return np.zeros(0, dtype=np.float32)

        probs = np.zeros(n, dtype=np.float32)
        with torch.no_grad():
            for i in range(n):
                chunk = block[i * win:(i + 1) * win]
                tensor = torch.from_numpy(chunk.copy())
                try:
                    p = self._model(tensor, self.SAMPLE_RATE)
                    probs[i] = float(p.item())
                except Exception:
                    # 单窗口失败不影响整体（模型状态可能异常）
                    probs[i] = 0.0
        return probs

    def _make_partial(self) -> PartialEvent | None:
        """生成半句事件。"""
        if self._buf_samples == 0:
            return None
        audio = self._collect_buffer()
        # 去掉尾部静音，让识别更聚焦
        audio = self._trim_trailing_silence(audio)
        if len(audio) < self._ms_to_samples(self.cfg.min_speech_ms):
            return None

        self._partial_index += 1
        self._last_partial_at = self._buf_samples
        self._pending_partial = audio

        return PartialEvent(
            audio=audio,
            start_s=self._speech_start_sample / self.SAMPLE_RATE,
            end_s=self._elapsed_samples / self.SAMPLE_RATE,
            index=self._partial_index,
        )

    def _collect_buffer(self) -> np.ndarray:
        if not self._buffer:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._buffer)

    def _trim_trailing_silence(self, audio: np.ndarray) -> np.ndarray:
        """裁掉尾部静音（保留一点余量）。"""
        if self._silence_samples <= 0:
            return audio
        keep_tail = self._ms_to_samples(60)
        cut = max(0, self._silence_samples - keep_tail)
        if cut <= 0 or cut >= len(audio):
            return audio
        return audio[: len(audio) - cut]

    def _finalize(self, trigger: str) -> SpeechSegment | None:
        """把当前缓冲定稿为一个语音段，并重置状态。"""
        audio = self._collect_buffer()
        start_s = self._speech_start_sample / self.SAMPLE_RATE
        end_s = self._elapsed_samples / self.SAMPLE_RATE

        # 裁掉尾部多余静音（避免识别出空内容）
        audio = self._trim_trailing_silence(audio)

        seg: SpeechSegment | None = None
        if len(audio) >= self._ms_to_samples(self.cfg.min_speech_ms):
            seg = SpeechSegment(
                audio=audio, start_s=start_s, end_s=end_s, trigger=trigger
            )
        else:
            log.debug(
                "语音段过短，丢弃",
                duration_ms=int(len(audio) / self.SAMPLE_RATE * 1000),
                min_ms=self.cfg.min_speech_ms,
            )

        # 重置
        self._buffer.clear()
        self._buf_samples = 0
        self._silence_samples = 0
        self._last_partial_at = 0
        self._partial_index = 0
        self._pending_partial = None
        self._state = VadState.SILENCE
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:
                pass

        return seg

    @property
    def state(self) -> VadState:
        return self._state
