"""线程安全的环形缓冲 + 重采样工具。

职责：
  - 把不等长的采集块拼成连续的 float32 mono 波形
  - 按需取出指定时长 / 到某个时间点的音频
  - 提供 RMS 计算与峰值归一化
"""
from __future__ import annotations

import threading
from collections import deque

import numpy as np

from ..logging_setup import get_bound_logger

log = get_bound_logger(__name__)


class RingBuffer:
    """float32 mono 环形缓冲。

    内部用 deque[np.ndarray] 存块，避免频繁大数组拼接。
    所有公开方法均为线程安全。
    """

    def __init__(self, sample_rate: int, max_seconds: float = 60.0) -> None:
        self.sample_rate = sample_rate
        self.max_samples = int(sample_rate * max_seconds)
        self._blocks: deque[np.ndarray] = deque()
        self._total = 0
        self._lock = threading.RLock()

    # ---------------- 写入 ----------------
    def write(self, samples: np.ndarray) -> None:
        """写入一块音频。samples 必须为 1-D float32。"""
        if samples.ndim != 1:
            samples = samples.reshape(-1)
        if samples.size == 0:
            return
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32, copy=False)

        with self._lock:
            self._blocks.append(samples)
            self._total += samples.size
            # 超出容量则从头部丢弃
            while self._total > self.max_samples and self._blocks:
                head = self._blocks[0]
                overflow = self._total - self.max_samples
                if head.size <= overflow:
                    self._total -= head.size
                    self._blocks.popleft()
                else:
                    self._blocks[0] = head[overflow:]
                    self._total -= overflow

    # ---------------- 读取 ----------------
    def snapshot(self, seconds: float | None = None) -> np.ndarray:
        """返回最近 N 秒的连续波形（拷贝）。seconds=None 返回全部。"""
        with self._lock:
            if not self._blocks:
                return np.zeros(0, dtype=np.float32)
            if seconds is None:
                return np.concatenate(list(self._blocks))
            n = int(self.sample_rate * seconds)
            return self._tail_locked(n)

    def read_last(self, n_samples: int) -> np.ndarray:
        """返回最近 n_samples 个采样点（不足则返回全部）。"""
        with self._lock:
            return self._tail_locked(n_samples)

    def _tail_locked(self, n: int) -> np.ndarray:
        if n <= 0 or not self._blocks:
            return np.zeros(0, dtype=np.float32)
        if n >= self._total:
            return np.concatenate(list(self._blocks))

        parts: list[np.ndarray] = []
        need = n
        for blk in reversed(self._blocks):
            if need <= 0:
                break
            if blk.size <= need:
                parts.append(blk)
                need -= blk.size
            else:
                parts.append(blk[-need:])
                need = 0
        parts.reverse()
        return np.concatenate(parts)

    def drain(self, seconds: float | None = None) -> np.ndarray:
        """取出并移除音频（消费语义）。seconds=None 取出全部。"""
        with self._lock:
            if not self._blocks:
                return np.zeros(0, dtype=np.float32)
            if seconds is None:
                data = np.concatenate(list(self._blocks))
                self._blocks.clear()
                self._total = 0
                return data

            n = int(self.sample_rate * seconds)
            if n >= self._total:
                data = np.concatenate(list(self._blocks))
                self._blocks.clear()
                self._total = 0
                return data

            # 取尾部 n 个，保留前面的
            tail = self._tail_locked(n)
            # 重建剩余部分
            remaining_total = self._total - tail.size
            new_blocks: deque[np.ndarray] = deque()
            acc = 0
            for blk in self._blocks:
                if acc + blk.size <= remaining_total:
                    new_blocks.append(blk)
                    acc += blk.size
                else:
                    keep = remaining_total - acc
                    if keep > 0:
                        new_blocks.append(blk[:keep])
                    break
            self._blocks = new_blocks
            self._total = remaining_total
            return tail

    def clear(self) -> None:
        with self._lock:
            self._blocks.clear()
            self._total = 0

    # ---------------- 属性 ----------------
    @property
    def total_samples(self) -> int:
        with self._lock:
            return self._total

    @property
    def duration_s(self) -> float:
        return self.total_samples / self.sample_rate

    def __len__(self) -> int:
        return self.total_samples


# =========================================================
#  信号处理工具
# =========================================================
def rms(samples: np.ndarray) -> float:
    """均方根（音量）值，范围通常 0~1。"""
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def peak(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.max(np.abs(samples)))


def normalize_peak(samples: np.ndarray, target: float = 0.95) -> np.ndarray:
    """把音频峰值归一化到 target。接近静音时原样返回。"""
    p = peak(samples)
    if p < 1e-4:
        return samples
    gain = target / p
    # 限制增益倍数，避免把底噪放大
    gain = min(gain, 20.0)
    return (samples * gain).astype(np.float32, copy=False)


def resample(
    samples: np.ndarray, src_rate: int, dst_rate: int
) -> np.ndarray:
    """重采样到目标采样率。

    优先使用 scipy.signal.resample_poly（质量高），失败则退回线性插值。
    """
    if src_rate == dst_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)

    try:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(src_rate, dst_rate)
        up = dst_rate // g
        down = src_rate // g
        out = resample_poly(samples, up, down)
        return out.astype(np.float32, copy=False)
    except ImportError:
        # 线性插值兜底
        n_out = int(round(samples.size * dst_rate / src_rate))
        if n_out <= 0:
            return np.zeros(0, dtype=np.float32)
        x_old = np.linspace(0.0, 1.0, samples.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
        out = np.interp(x_new, x_old, samples)
        return out.astype(np.float32)


def to_mono(samples: np.ndarray) -> np.ndarray:
    """多声道 -> 单声道（按通道平均）。"""
    if samples.ndim == 1:
        return samples
    if samples.ndim == 2:
        # 约定 shape = (frames, channels)
        if samples.shape[1] > 1:
            return samples.mean(axis=1).astype(np.float32, copy=False)
        return samples[:, 0]
    return samples.reshape(-1)
