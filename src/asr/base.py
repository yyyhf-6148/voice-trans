"""ASR Provider 抽象层。

目标：让「本地 GPU」「本地 CPU」「云端 API」三种后端可以互换，
通过 config.yaml 的 asr.provider 切换，不改动上层流水线代码。

两台机器场景：
  · P106 那台  -> provider: local, device: auto（自动用 CUDA）
  · AMD 那台   -> provider: cloud（或 local + device: cpu）
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

from ..logging_setup import get_bound_logger

log = get_bound_logger(__name__)


# =========================================================
#  统一数据结构
# =========================================================
@dataclass
class TranscriptionSegment:
    """一段识别结果。"""

    text: str
    start_s: float
    end_s: float
    # 该段的平均对数概率（可作为置信度参考），云端可能不提供
    avg_logprob: float | None = None
    # 是否为「半句」结果（VAD 提前触发产生），后续会被完整结果覆盖
    is_partial: bool = False

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def __str__(self) -> str:
        tag = "~" if self.is_partial else " "
        return f"{tag}[{self.start_s:.1f}-{self.end_s:.1f}s] {self.text}"


@dataclass
class TranscriptionResult:
    """一次识别的完整结果。"""

    segments: list[TranscriptionSegment] = field(default_factory=list)
    language: str = ""
    language_probability: float = 0.0
    # 端到端耗时（含排队），由 Provider 填充
    elapsed_s: float = 0.0
    # 音频时长
    audio_duration_s: float = 0.0

    @property
    def text(self) -> str:
        """把所有片段拼成一段文本。"""
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())

    @property
    def is_empty(self) -> bool:
        return not self.text

    @property
    def realtime_factor(self) -> float:
        """实时因子，<1 表示比实时快。"""
        if self.audio_duration_s <= 0:
            return 0.0
        return self.elapsed_s / self.audio_duration_s


# =========================================================
#  抽象接口
# =========================================================
class AsrProvider(ABC):
    """语音识别后端抽象。"""

    name: str = "base"

    @abstractmethod
    def load(self) -> None:
        """加载模型 / 建立连接。应在 start 前调用一次。"""

    @abstractmethod
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        is_partial: bool = False,
    ) -> TranscriptionResult:
        """识别一段 16kHz mono float32 音频。

        Args:
            audio: 16kHz mono float32 PCM，范围 [-1, 1]
            language: 源语言，None 表示自动检测
            is_partial: 是否为半句（VAD 提前触发），
                        实现可据此选择更快的解码参数

        Returns:
            TranscriptionResult
        """

    @abstractmethod
    def unload(self) -> None:
        """释放资源。"""

    # ---------------- 通用辅助 ----------------
    @staticmethod
    def validate_audio(audio: np.ndarray) -> np.ndarray:
        """校验并规范化输入音频。"""
        if audio is None or audio.size == 0:
            return np.zeros(0, dtype=np.float32)
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32, copy=False)
        if audio.ndim > 1:
            audio = audio.reshape(-1)
        return audio

    @property
    def info(self) -> str:
        return self.name


# =========================================================
#  设备探测
# =========================================================
def detect_device(preference: str = "auto") -> tuple[str, str]:
    """探测可用的推理设备，返回 (device, compute_type)。

    Args:
        preference: "auto" | "cuda" | "cpu" | "dml"(未支持)

    Returns:
        (device, compute_type)

    设计说明：
      · preference="auto" 时优先 CUDA，不可用则退 CPU
      · Pascal 架构（CC 6.1，如 P106/P100）的 FP16 吞吐极差，
        自动降级为 float32 避免「更慢或直接报错」
    """
    if preference == "cpu":
        return "cpu", "int8"

    try:
        import torch

        cuda_ok = torch.cuda.is_available()
    except ImportError:
        cuda_ok = False
        if preference == "cuda":
            log.warning("请求 CUDA 但 torch 未安装，降级到 CPU")

    if preference == "cuda" and not cuda_ok:
        log.warning("请求 CUDA 但不可用，降级到 CPU")
        return "cpu", "int8"

    if not cuda_ok:
        return "cpu", "int8"

    # 探测计算能力：Pascal(6.x) / Volta(7.x) 的 FP16 有坑
    try:
        import torch

        cc_major = torch.cuda.get_device_properties(0).major
        gpu_name = torch.cuda.get_device_properties(0).name
    except Exception:
        cc_major = 7
        gpu_name = "unknown"

    if cc_major < 7:
        # Pascal 及更早：FP16 吞吐被阉割，用 float32 更稳
        log.info(
            "检测到 Pascal 架构显卡，FP16 性能受限，使用 float32",
            gpu=gpu_name,
            compute_capability=f"{cc_major}.x",
        )
        return "cuda", "float32"

    return "cuda", "float16"


def describe_environment() -> dict[str, object]:
    """收集运行环境信息，用于日志与诊断。"""
    import platform

    info: dict[str, object] = {
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
    }
    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda_available"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            info["gpu_name"] = p.name
            info["gpu_cc"] = f"{p.major}.{p.minor}"
            info["gpu_mem_gb"] = round(p.total_memory / 1024**3, 1)
    except ImportError:
        info["torch"] = None
    return info
