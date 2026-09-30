"""WASAPI 音频采集。

两条路径，由 ``audio.backend`` 决定：

  - ``process``（默认）：进程级 loopback，只抓指定进程（Mumble）的音频，
    见 :mod:`src.audio.process_loopback`。需要 Windows 10 2004+。
    启动时找不到目标进程会**自动降级**为整路 loopback 并告警。
  - ``wasapi_loopback``：soundcard 库的整路系统回环采集（扬声器上的一切声音）。

设计要点：
  - 采集在独立线程中运行，产出 float32 mono 16k 音频块
  - 通过 callback 或 queue 向上游投递
  - 支持运行时查询采集电平，用于 UI 指示与静音判定
  - soundcard 路径遇到 data discontinuity（丢帧）时自动重新打开录音流
  - 进程级路径目标进程退出后自动重连；始终找不到则降级整路采集
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import warnings
from collections.abc import Callable

import numpy as np

from ..config import AudioSection, Config
from ..errors import AudioCaptureError, AudioError
from ..logging_setup import get_bound_logger
from .device import AudioDevice, resolve_device
from .ring_buffer import normalize_peak, resample, rms, to_mono

log = get_bound_logger(__name__)

# soundcard 在流被抢占 / 时钟漂移时会发这个警告，说明丢了音频
_DISCONTINUITY_TEXT = "data discontinuity"

# 预热状态：首次导入 soundcard 约需 0.4s，
# 提前到主线程预热可避免首个音频块延迟。
_WARMED = False


def warmup() -> float:
    """在主线程预热音频子系统，返回耗时（秒）。

    建议在启动 UI 前调用，把 ~0.4s 的模块导入开销
    从"首块音频延迟"里挪到启动阶段。
    """
    global _WARMED
    if _WARMED:
        return 0.0
    t0 = time.monotonic()
    import soundcard as sc  # noqa: F401

    # 触发一次设备枚举，让 COM/WASAPI 初始化完成
    try:
        sc.all_speakers()
    except Exception:
        pass
    elapsed = time.monotonic() - t0
    _WARMED = True
    log.debug("音频子系统预热完成", elapsed_s=round(elapsed, 3))
    return elapsed


class AudioCapture:
    """WASAPI loopback 采集器。

    典型用法::

        cap = AudioCapture(cfg)
        cap.start()
        while running:
            block = cap.get_block(timeout=0.5)   # float32 mono 16k
        cap.stop()
    """

    # 连续多少次丢帧警告后重建流
    _RECONNECT_AFTER_DISCONTINUITY = 3

    # 进程级采集连续失败多少次后降级为整路 loopback
    _DEGRADE_AFTER_PROCESS_FAILURES = 3

    def __init__(
        self,
        cfg: Config,
        device: AudioDevice | None = None,
        on_block: Callable[[np.ndarray], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.section: AudioSection = cfg.audio
        self.target_rate = cfg.app.target_sample_rate
        self.on_block = on_block

        self._device = device
        self._queue: queue.Queue[np.ndarray] = queue.Queue(
            maxsize=max(4, self.section.max_queue_blocks)
        )
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._reopen_evt = threading.Event()

        # 统计信息（线程安全读取）
        self._stat_lock = threading.Lock()
        self._level = 0.0           # 最近一块的 RMS
        self._peak = 0.0
        self._dropped = 0           # 因队列满丢弃的块数
        self._captured_s = 0.0      # 累计采集时长
        self._last_block_ts = 0.0
        self._discontinuities = 0   # 累计丢帧次数

        self._src_rate = 48000
        self._device_resolve_s = 0.0
        self._recording = False  # 是否已产出过音频

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            log.warning("采集已在运行，忽略重复 start()")
            return

        # 进程级后端不碰 soundcard —— 不预热、不解析设备，
        # 省掉 ~0.4s 的无谓导入。真需要降级时再按需加载（见 _capture_loop_process）。
        if self.section.backend != "process":
            warmup()  # 预热音频子系统（幂等），避免首块延迟
            t0 = time.monotonic()
            if self._device is None:
                self._device = resolve_device(self.section.device_keyword)
            self._device_resolve_s = time.monotonic() - t0

        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="AudioCapture", daemon=True
        )
        self._thread.start()
        log.info(
            "音频采集已启动",
            backend=self.section.backend,
            device=self._device.name if self._device else "(按需解析)",
            target_rate=self.target_rate,
            block_ms=self.section.block_ms,
            setup_s=round(self._device_resolve_s, 2),
        )

    def wait_until_recording(self, timeout: float = 5.0) -> bool:
        """阻塞直到开始产出音频块。用于需要精确对齐的测量场景。

        Returns:
            True = 已开始录音；False = 超时
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.qsize() > 0:
                return True
            if self._thread is not None and not self._thread.is_alive():
                return False
            time.sleep(0.01)
        return False

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_evt.set()
        self._reopen_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                log.warning("采集线程未在超时内退出")
        self._thread = None
        log.info("音频采集已停止", captured_s=round(self._captured_s, 1))

    def reopen_stream(self) -> None:
        """请求重建录音流（用于切换设备或恢复丢帧）。

        线程安全，非阻塞。
        """
        self._reopen_evt.set()

    def __enter__(self) -> AudioCapture:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ---------------- 数据获取 ----------------
    def get_block(self, timeout: float | None = 0.5) -> np.ndarray | None:
        """从队列取一块 16k mono float32 音频。超时返回 None。"""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_nowait(self) -> np.ndarray | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def drain(self) -> int:
        """清空内部队列，返回丢弃的块数。

        重连云端 / 换采集设备后调用 —— 否则积压的旧音频
        会被推给新连接，翻译出一堆与当前时刻无关的内容。
        """
        n = 0
        while True:
            try:
                self._queue.get_nowait()
                n += 1
            except queue.Empty:
                return n

    # ---------------- 状态查询 ----------------
    @property
    def level(self) -> float:
        with self._stat_lock:
            return self._level

    @property
    def peak(self) -> float:
        with self._stat_lock:
            return self._peak

    @property
    def dropped_blocks(self) -> int:
        with self._stat_lock:
            return self._dropped

    @property
    def discontinuities(self) -> int:
        """soundcard 报告的丢帧次数（流被抢占/时钟漂移的迹象）。"""
        with self._stat_lock:
            return self._discontinuities

    @property
    def captured_seconds(self) -> float:
        with self._stat_lock:
            return self._captured_s

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def device(self) -> AudioDevice | None:
        return self._device

    def seconds_since_last_block(self) -> float:
        with self._stat_lock:
            if self._last_block_ts == 0.0:
                return float("inf")
            return time.monotonic() - self._last_block_ts

    # ---------------- 采集线程 ----------------
    def _run(self) -> None:
        try:
            if self.section.backend == "process":
                self._capture_loop_process()
            else:
                assert self._device is not None
                self._capture_loop_soundcard()
        except Exception as e:
            log.error("采集线程异常退出", error=str(e), exc_info=True)

    def _capture_loopback_source(self):  # pragma: no cover - 依赖驱动
        """返回 soundcard 的可采集对象（整路 loopback）。

        仅在确实要走 soundcard 路径时调用 —— 进程级后端已由
        ``_capture_loop_process`` 处理，不会走到这里。
        """
        import soundcard as sc

        # 按 device id 精确定位 speaker，取其 loopback recorder
        want_id = str(self._device.id) if self._device else ""
        for spk in sc.all_speakers():
            if str(spk.id) == want_id:
                return spk
        if self._device:
            # id 匹配失败，退回按名字匹配
            for spk in sc.all_speakers():
                if spk.name == self._device.name:
                    return spk
        return sc.default_speaker()

    def _capture_loop_soundcard(self) -> None:
        """soundcard 采集主循环（含丢帧自愈）。

        soundcard 的 recorder 采样率受驱动限制，
        这里以设备原生采样率采集，再重采样到 target_rate。

        Windows 上 soundcard 在流被抢占时会抛 SoundcardRuntimeWarning
        ("data discontinuity")，随后录音数据会断裂。这里捕获该警告，
        连续多次出现则重建录音流。
        """
        try:
            import soundcard as sc
            from soundcard import SoundcardRuntimeWarning
        except ImportError as e:
            raise AudioError(
                "缺少 soundcard 库，无法进行系统音频采集",
                detail="请执行: pip install soundcard",
            ) from e

        spk = self._capture_loopback_source()
        if spk is None:
            raise AudioCaptureError("没有可用的扬声器设备用于回环采集")

        src_rate = int(getattr(spk, "default_samplerate", 0) or 48000)
        self._src_rate = src_rate
        block_frames = max(64, int(src_rate * self.section.block_ms / 1000))

        log.debug(
            "准备打开 loopback 采集",
            speaker=spk.name,
            src_rate=src_rate,
            block_frames=block_frames,
        )

        # soundcard: speaker 本身可作为 loopback 麦克风使用
        try:
            mic = sc.get_microphone(id=str(spk.id), include_loopback=True)
        except Exception:
            mic = None
        if mic is None:
            mic = spk

        first_open = True
        disc_streak = 0

        while not self._stop_evt.is_set():
            self._reopen_evt.clear()
            try:
                recorder_cm = mic.recorder(
                    samplerate=src_rate, channels=2, blocksize=block_frames
                )
            except Exception as e:
                raise AudioCaptureError(
                    f"打开 loopback 录音流失败: {e}",
                    detail="可能是设备被独占、或被其他程序占用。"
                    "可尝试在 config.yaml 中指定 audio.device_keyword",
                ) from e

            try:
                with recorder_cm as rec:
                    if first_open:
                        log.info(
                            "loopback 录音流已打开",
                            device=self._device.name if self._device else spk.name,
                        )
                        first_open = False
                    else:
                        log.info("loopback 录音流已重建")

                    while not self._stop_evt.is_set() and not self._reopen_evt.is_set():
                        try:
                            # 捕获 soundcard 的丢帧警告（不打印到 stderr，避免刷屏）
                            with warnings.catch_warnings(record=True) as caught:
                                warnings.simplefilter("always")
                                data = rec.record(numframes=block_frames)

                            disc = any(
                                issubclass(w.category, SoundcardRuntimeWarning)
                                and _DISCONTINUITY_TEXT in str(w.message)
                                for w in caught
                            )
                            if disc:
                                disc_streak += 1
                                with self._stat_lock:
                                    self._discontinuities += 1
                                if disc_streak >= self._RECONNECT_AFTER_DISCONTINUITY:
                                    log.warning(
                                        "检测到连续丢帧，重建录音流",
                                        streak=disc_streak,
                                    )
                                    disc_streak = 0
                                    break  # 跳出内层 -> 重建
                            else:
                                disc_streak = 0

                        except Exception as e:
                            log.error("录音读取失败，重建流", error=str(e))
                            time.sleep(0.05)
                            break

                        self._handle_raw_block(data, src_rate)

            except AudioCaptureError:
                raise
            except Exception as e:
                log.error("录音流异常，1 秒后重建", error=str(e))
                time.sleep(1.0)
                continue

            # 重建前稍作等待，避免紧密循环
            if not self._stop_evt.is_set():
                time.sleep(0.15)

    def _capture_loop_process(self) -> None:
        """进程级 loopback 采集主循环（只抓目标进程的音频）。

        行为：
          - 启动时找不到目标进程 / 系统版本过低 → 降级为整路 loopback 并告警
          - 目标进程中途退出 → 尝试重新查找；找到就重开，找不到则降级
          - 打开失败 → 2 秒后重试（避免紧密循环）
        """
        from .process_loopback import (
            ProcessLoopbackStream,
            find_process_id,
            is_supported,
        )

        if not is_supported():
            build = sys.getwindowsversion().build
            log.warning(
                "系统版本过低，降级为整路系统音频采集",
                build=build,
                required=19041,
            )
            self._degrade_to_soundcard()
            return

        name = self.section.process_name.strip()
        degrade = False
        fail_streak = 0
        # 进程级流按 WASAPI 周期（~10ms）产出小块，这里聚合到 block_ms，
        # 与 soundcard 路径保持一致，避免上游收到过多碎片
        block_frames = max(1, int(self.target_rate * self.section.block_ms / 1000))

        while not self._stop_evt.is_set():
            pid = find_process_id(name, exclude_pid=os.getpid())
            if pid is None:
                log.warning(
                    "未找到目标进程，降级为整路系统音频采集",
                    process_name=name,
                    hint="请确认 Mumble 已启动；或在 config.yaml 中调整 audio.process_name",
                )
                degrade = True
                break

            try:
                with ProcessLoopbackStream(
                    pid,
                    target_rate=self.target_rate,
                    buffer_ms=self.section.process_buffer_ms,
                ) as stream:
                    self._src_rate = stream.src_rate
                    fail_streak = 0
                    log.info(
                        "进程级采集流已打开",
                        process_name=name,
                        pid=pid,
                        src_rate=stream.src_rate,
                        src_channels=stream.src_channels,
                        format=stream.format_desc,
                    )
                    last_check = 0.0
                    pending: list[np.ndarray] = []
                    pending_n = 0

                    while not self._stop_evt.is_set():
                        data = stream.read(timeout=0.2)
                        if data is not None and data.size:
                            pending.append(data)
                            pending_n += data.size
                            if pending_n >= block_frames:
                                block = np.concatenate(pending)
                                pending.clear()
                                pending_n = 0
                                self._handle_raw_block(block, self.target_rate)

                        now = time.monotonic()
                        if now - last_check >= 1.0:
                            last_check = now
                            if not stream.target_alive():
                                log.warning(
                                    "目标进程已退出，尝试重新查找", pid=pid
                                )
                                break
            except Exception as e:
                fail_streak += 1
                log.error(
                    "进程级采集失败，2 秒后重试",
                    error=str(e),
                    fail_streak=fail_streak,
                )
                if fail_streak >= self._DEGRADE_AFTER_PROCESS_FAILURES:
                    log.warning(
                        "进程级采集连续失败，降级为整路系统音频采集",
                        fail_streak=fail_streak,
                    )
                    degrade = True
                    break
                for _ in range(20):
                    if self._stop_evt.is_set():
                        return
                    time.sleep(0.1)
                continue

            # 内层因目标进程退出而跳出 → 稍等后重新查找
            if not self._stop_evt.is_set():
                time.sleep(0.3)

        # 降级为整路采集
        if not self._stop_evt.is_set() and degrade:
            self._degrade_to_soundcard()

    def _degrade_to_soundcard(self) -> None:
        """切到整路 loopback 采集（soundcard 按需加载）。"""
        warmup()
        if self._device is None:
            try:
                self._device = resolve_device(self.section.device_keyword)
            except Exception as e:
                log.warning("解析采集设备失败，改用默认扬声器", error=str(e))
        self._capture_loop_soundcard()

    def _handle_raw_block(self, data: np.ndarray, src_rate: int) -> None:
        """把原始多声道块处理成 16k mono 并投递。"""
        if data is None or len(data) == 0:
            return

        mono = to_mono(np.asarray(data, dtype=np.float32))
        if mono.size == 0:
            return

        # 重采样到目标采样率
        if src_rate != self.target_rate:
            mono = resample(mono, src_rate, self.target_rate)

        # 统计与增益
        level = rms(mono)
        pk = float(np.max(np.abs(mono))) if mono.size else 0.0
        with self._stat_lock:
            self._level = level
            self._peak = pk
            self._captured_s += mono.size / self.target_rate
            self._last_block_ts = time.monotonic()
            self._recording = True

        # 自动增益：仅在明显有声时归一化，避免放大静音底噪
        if self.section.auto_gain and level > self.section.silence_rms_threshold:
            mono = normalize_peak(mono, target=0.9)

        # 投递
        if self.on_block is not None:
            try:
                self.on_block(mono)
            except Exception as e:
                log.error("on_block 回调异常", error=str(e))

        try:
            self._queue.put_nowait(mono)
        except queue.Full:
            # 丢弃最旧的一块，保持低延迟
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(mono)
            except queue.Empty:
                pass
            with self._stat_lock:
                self._dropped += 1


# =========================================================
#  便捷测试：录制到 wav
# =========================================================
def record_to_wav(
    cfg: Config,
    out_path: str,
    duration_s: float = 10.0,
    progress: Callable[[float, float], None] | None = None,
) -> None:
    """录制系统音频到 wav 文件（调试用）。

    Args:
        cfg: 配置对象
        out_path: 输出 wav 路径
        duration_s: 录制时长（秒）
        progress: 回调 (已录秒数, 当前RMS)
    """
    import wave

    cap = AudioCapture(cfg)
    cap.start()
    frames: list[np.ndarray] = []
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < duration_s:
            blk = cap.get_block(timeout=0.5)
            if blk is None:
                continue
            frames.append(blk)
            if progress:
                progress(time.monotonic() - t0, cap.level)
    finally:
        cap.stop()

    if not frames:
        raise AudioCaptureError("未采集到任何音频数据")

    audio = np.concatenate(frames)
    pcm16 = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype(np.int16)

    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(cfg.app.target_sample_rate)
        wf.writeframes(pcm16.tobytes())

    log.info(
        "录制完成",
        path=out_path,
        duration_s=round(len(audio) / cfg.app.target_sample_rate, 2),
    )
