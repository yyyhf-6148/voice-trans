"""识别流水线：音频采集 -> VAD 切句 -> ASR 转写。

两种运行模式
------------
**模式 A：切句-识别（local / cloud）**

    [采集线程]  AudioCapture
         │  block 16k mono
         ▼
    [处理线程]  push -> VadSegmenter -> ASR
         │  TranscriptionResult
         ▼
    [回调]      on_text(TranscriptionEvent)

**模式 B：流式直推（livetranslate）**

    [采集线程]  AudioCapture
         │  block 16k mono
         ▼
    [转发线程]  push_audio ─── WebSocket ──▶ 云端（自带 VAD）
         │                                        │
         │◀──────── 服务端回调 TranslationEvent ───┘
         ▼
    [回调]      on_translation(TranslationEvent)

模式 B **不经过本地 VAD**：该模型自带服务端 VAD，
两边各切一次会把句子切碎。配置层已强制拦截（见 config._validate）。

设计要点:
  · VAD 和 ASR 在独立线程，避免阻塞采集
  · 半句结果标记 is_partial，上层可先显示粗略译文
  · 统计耗时用于延迟分析与调优
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

import numpy as np

from .asr.base import AsrProvider, TranscriptionResult
from .asr.factory import create_asr_provider, create_live_translate
from .asr.live_translate import TranslationEvent
from .asr.vad import PartialEvent, SpeechSegment, VadSegmenter
from .audio.capture import AudioCapture
from .config import Config
from .errors import AppError
from .logging_setup import get_bound_logger

log = get_bound_logger(__name__)

# 流式模式下采集块 → 云端的队列上限。
# 云端推送是纯网络 IO，正常远快于采集；
# 这里留足余量，一旦积压说明网络出问题，丢旧块保低延迟。
_STREAM_QUEUE_MAX = 64

# ---- 断线检测 / 自动重连 ----
# 推送连续失败多少次判定为「连接已断」。3 次 × 40ms ≈ 0.12s：
# 足够快，又不会被网络抖动误判。
_PUSH_FAIL_LIMIT = 3
# 指数退避：1s → 2s → 4s → 8s → 16s → 30s（封顶）
_RECONNECT_BASE_DELAY_S = 1.0
_RECONNECT_MAX_DELAY_S = 30.0
_RECONNECT_MAX_ATTEMPTS = 6


class TranscriptionEvent:
    """一次识别输出（供上层消费）。"""

    __slots__ = (
        "result", "is_partial", "segment", "latency_s", "sequence",
        "queue_wait_s", "asr_time_s",
    )

    def __init__(
        self,
        result: TranscriptionResult,
        is_partial: bool,
        segment: SpeechSegment | PartialEvent,
        latency_s: float,
        sequence: int,
        queue_wait_s: float = 0.0,
        asr_time_s: float = 0.0,
    ) -> None:
        self.result = result
        self.is_partial = is_partial
        self.segment = segment
        self.latency_s = latency_s
        self.sequence = sequence
        self.queue_wait_s = queue_wait_s
        self.asr_time_s = asr_time_s

    @property
    def text(self) -> str:
        return self.result.text

    def __str__(self) -> str:
        tag = "部分" if self.is_partial else "完整"
        return (
            f"[{self.sequence:03d}][{tag}] {self.text}  "
            f"(asr={self.result.elapsed_s*1000:.0f}ms "
            f"端到端={self.latency_s*1000:.0f}ms)"
        )


class TranslationTextEvent:
    """流式翻译模式下的一条输出（供悬浮窗消费）。

    与 TranscriptionEvent 的区别
    ----------------------------
    · 这里 `translation` 已经是**中文译文**，无需再做翻译步骤
    · `original` 是英文原文（可选），用于双语字幕
    · `is_partial=True` 表示增量，UI 应覆盖上一行的同一条消息
    · 服务端 VAD 的事件（说话开始/结束）也走这里，便于 UI 做提示
    """

    __slots__ = (
        "translation", "original", "kind", "is_partial",
        "timestamp_s", "first_text_delay_ms", "language", "emotion",
        "sequence", "received_monotonic",
    )

    def __init__(
        self,
        ev: TranslationEvent,
        sequence: int,
        received_monotonic: float,
    ) -> None:
        self.translation = ev.translation
        self.original = ev.original
        self.kind = ev.kind
        self.is_partial = ev.is_partial
        self.timestamp_s = ev.timestamp_s
        self.first_text_delay_ms = ev.first_text_delay_ms
        self.language = ev.language
        self.emotion = ev.emotion
        self.sequence = sequence
        self.received_monotonic = received_monotonic

    @property
    def text(self) -> str:
        """优先给中文译文；没有时退回原文。"""
        return self.translation or self.original

    @property
    def is_displayable(self) -> bool:
        """是否是需要上屏的文本事件（排除 VAD 与错误事件）。"""
        return self.kind in ("translation", "original") and bool(self.text)

    def __str__(self) -> str:
        tag = "增量" if self.is_partial else "定稿"
        delay = (
            f" 首字={self.first_text_delay_ms:.0f}ms"
            if self.first_text_delay_ms
            else ""
        )
        extra = f" | {self.original}" if self.original and self.translation else ""
        return f"[{self.sequence:03d}][{tag}] {self.text}{extra}{delay}"


class Pipeline:
    """音频 -> 文本 流水线。"""

    def __init__(
        self,
        cfg: Config,
        on_text: Callable[[TranscriptionEvent], None],
        on_error: Callable[[Exception], None] | None = None,
        on_translation: Callable[[TranslationTextEvent], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.on_text = on_text
        self.on_error = on_error
        # 流式模式（livetranslate）的输出回调。
        # 未提供时，文本事件会退化为打到日志，方便命令行调试。
        self.on_translation = on_translation

        self._asr: AsrProvider | None = None
        self._live = None  # LiveTranslateProvider
        self._vad: VadSegmenter | None = None
        self._capture: AudioCapture | None = None
        self._connect_s = 0.0

        self._work_q: queue.Queue[SpeechSegment | PartialEvent] = queue.Queue(maxsize=64)
        self._stop_evt = threading.Event()
        self._work_thread: threading.Thread | None = None
        self._seq = 0
        self._lock = threading.Lock()

        # ---- 断线自动重连 ----
        # `_conn_lost_evt`：连接断开的信号，守护线程等它
        # `_shutdown_evt` ：程序真的要退出了。
        #   ⚠️ 必须和 `_stop_evt` 分开：重连流程里会 `_stop_evt.set()` 再 clear，
        #      如果守护线程也靠 `_stop_evt` 退出，一次重连就会把它干掉。
        self._conn_lost_evt = threading.Event()
        self._shutdown_evt = threading.Event()
        self._supervise_thread: threading.Thread | None = None
        self._session_started_at: float | None = None
        self._manual_reconnect = False
        self._reconnecting = False

        # 断线/重连生命周期回调（**由守护线程调用**，实现方必须线程安全）
        self.on_connection_lost: Callable[[str], None] | None = None
        self.on_reconnecting: Callable[[int, float], None] | None = None
        self.on_reconnected: Callable[[int], None] | None = None
        self.on_reconnect_failed: Callable[[int], None] | None = None

        # 统计
        self._stats = {
            "blocks_in": 0,
            "segments": 0,
            "partials": 0,
            "errors": 0,
            "total_asr_s": 0.0,
            "total_audio_s": 0.0,
            "total_queue_wait_s": 0.0,
            "max_latency_s": 0.0,
        }

    # ---------------- 模式判定 ----------------
    @property
    def is_stream_mode(self) -> bool:
        """是否为流式直推模式（livetranslate）。"""
        return (self.cfg.asr.provider or "").lower() == "livetranslate"

    # ---------------- 生命周期 ----------------
    def setup(self) -> None:
        """加载模型 / 建立云端连接（耗时较长，建议在 UI 启动前调用）。"""
        log.info("初始化识别流水线...")

        if self.is_stream_mode:
            # 流式模式：只创建 provider，连接放到 start() 里建，
            # 因为连接会超时断开，不宜在 setup 阶段空转。
            self._live = create_live_translate(
                self.cfg.asr, on_event=self._on_live_event
            )
            self._capture = AudioCapture(self.cfg)
            log.info(
                "流水线就绪（流式模式，本地 VAD 已旁路）",
                provider=self._live.info,
            )
            return

        self._asr = create_asr_provider(self.cfg.asr)
        self._asr.load()

        if self.cfg.vad.enabled:
            self._vad = VadSegmenter(self.cfg.vad)
            self._vad.load()
        else:
            log.info("VAD 已关闭，将按固定窗口切分")

        self._capture = AudioCapture(self.cfg)

        log.info(
            "流水线就绪",
            asr=self._asr.info,
            vad="on" if self._vad else "off",
        )

    def start(self) -> None:
        if self._capture is None:
            raise AppError("请先调用 setup()")

        self._stop_evt.clear()
        # 新生命周期：清掉上次 stop() 留下的"永久停止"标志，
        # 否则守护线程不会再起来（`stop()` 会置位它）
        self._shutdown_evt.clear()

        if self.is_stream_mode:
            self._start_stream()
            return

        self._work_thread = threading.Thread(
            target=self._work_loop, name="PipelineWorker", daemon=True
        )
        self._work_thread.start()

        assert self._capture is not None
        self._capture.start()

        # 采集 -> VAD 的桥接线程
        self._bridge_thread = threading.Thread(
            target=self._bridge_loop, name="PipelineBridge", daemon=True
        )
        self._bridge_thread.start()

        log.info("流水线已启动")

    # ---------------- 流式模式 ----------------
    def start_without_capture(self) -> None:
        """只建云端连接，不启动音频采集。

        用于把音频从外部灌进来（如播放 WAV 文件做确定性验证），
        此时无需 loopback 设备，在无声卡/静音环境下也能测。
        """
        assert self._live is not None

        t0 = time.monotonic()
        self._live.start()
        self._connect_s = time.monotonic() - t0

        self._stream_q = queue.Queue(maxsize=_STREAM_QUEUE_MAX)
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="PipelinePump", daemon=True
        )
        self._pump_thread.start()

        log.info(
            "流水线已启动（无采集，等待外部灌音）",
            connect_s=round(self._connect_s, 2),
        )

    def push_audio_raw(self, audio: np.ndarray) -> None:
        """把外部音频直接推给云端（仅流式模式可用）。

        与 _stream_loop 的区别：这里是调用方主动喂数据，
        走同一条 pump 队列，保证推送与网络解耦。
        """
        if self._live is None:
            raise AppError("流水线未启动或非流式模式")
        blk = np.asarray(audio, dtype=np.float32)
        if blk.size == 0:
            return

        with self._lock:
            self._stats["blocks_in"] += 1
            self._stats["total_audio_s"] += (
                blk.size / self.cfg.app.target_sample_rate
            )

        q = getattr(self, "_stream_q", None)
        if q is None:
            # 没走队列（异常情况）→ 直接推
            self._live.push_audio(blk)
            return
        try:
            q.put_nowait(blk)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(blk)
            except queue.Empty:
                pass
            with self._lock:
                self._stats["dropped_tasks"] = (
                    self._stats.get("dropped_tasks", 0) + 1
                )

    def _start_stream(self) -> None:
        """建立 WebSocket 连接并启动采集→转发的搬运线程。"""
        assert self._live is not None and self._capture is not None

        t0 = time.monotonic()
        self._live.start()  # 阻塞直到握手完成或超时
        self._connect_s = time.monotonic() - t0

        self._stream_q: queue.Queue[np.ndarray] = queue.Queue(
            maxsize=_STREAM_QUEUE_MAX
        )
        self._stream_thread = threading.Thread(
            target=self._stream_loop, name="PipelineStream", daemon=True
        )
        self._stream_thread.start()

        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="PipelinePump", daemon=True
        )
        self._pump_thread.start()

        # 采集放最后：这样即使采集启动慢，连接也已就绪
        self._capture.start()

        # 连接刚建立 → 清掉"断开"信号，记录会话起点，确保守护线程在跑
        self._conn_lost_evt.clear()
        self._session_started_at = time.monotonic()
        self._ensure_supervisor()

        log.info(
            "流水线已启动（流式直推云端）",
            connect_s=round(self._connect_s, 2),
        )

    def _stream_loop(self) -> None:
        """把采集到的音频块持续推给云端。

        与 _bridge_loop 的区别：不切句、不做任何缓冲判断，
        收到什么就推什么，切句完全交给云端 VAD。

        ⚠️ 推送本身有网络延迟（通常几毫秒，但网络抖动时会放大）。
        这里**不在本线程直接推**，而是先入队再推，
        这样采集线程永远不会被网络阻塞——采集一旦被阻塞就会丢音频。
        """
        q = self._stream_q

        while not self._stop_evt.is_set():
            blk = self._capture.get_block(timeout=0.05) if self._capture else None
            if blk is None:
                continue

            with self._lock:
                self._stats["blocks_in"] += 1
                self._stats["total_audio_s"] += blk.size / self.cfg.app.target_sample_rate

            # 队列满 → 网络跟不上，丢最旧的块（宁可丢音频也不能积压）
            try:
                q.put_nowait(blk)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(blk)
                except queue.Empty:
                    pass
                with self._lock:
                    self._stats["dropped_tasks"] = (
                        self._stats.get("dropped_tasks", 0) + 1
                    )
                log.warning("推送队列已满，丢弃旧音频块")

    def _pump_loop(self) -> None:
        """把队列里的音频真正推给云端（独立线程，隔离网络延迟）。

        ⚠️ 断线处理是这里的重点（2026-09-29 实机暴露的严重 bug）：
          · 服务端以 close code 1011 关闭连接后，老实现**完全不知情**，
            继续以 ~25 次/秒的速度推 + 每次打一条 ERROR 日志，
            实测刷了 **15,482 条 / 4.67MB**，把有用日志全挤掉。
          · 现在：连接一断就**清空队列并退出循环**，交给守护线程重连；
            推送异常也只在**第一次**打日志，连续 3 次即判定断线。
        """
        q = self._stream_q
        fails = 0
        while not self._stop_evt.is_set():
            # 每轮都查 —— 断线时队列可能已经空了，只靠 push 异常发现太晚
            live = self._live
            if live is not None and not live.is_alive:
                dropped = self._drop_backlog(q)
                log.warning("连接已断开，停止推送", dropped_blocks=dropped)
                self._signal_connection_lost("服务端已关闭连接")
                return

            try:
                blk = q.get(timeout=0.1)
            except queue.Empty:
                continue

            if live is None:
                continue

            try:
                live.push_audio(blk)
                fails = 0
            except Exception as e:                   # noqa: BLE001
                fails += 1
                with self._lock:
                    self._stats["errors"] += 1
                if fails == 1:
                    log.error("音频推送失败（连接可能已断开）", error=str(e))
                if fails >= _PUSH_FAIL_LIMIT:
                    dropped = self._drop_backlog(q)
                    log.error("音频连续推送失败，判定连接已断开",
                              fails=fails, dropped_blocks=dropped)
                    self._signal_connection_lost(f"连续 {fails} 次推送失败")
                    return

    @staticmethod
    def _drop_backlog(q: queue.Queue) -> int:
        """清空推送队列。断线后必须丢 —— 旧音频推给新连接会串内容。"""
        n = 0
        while True:
            try:
                q.get_nowait()
                n += 1
            except queue.Empty:
                return n

    @staticmethod
    def _notify(cb, *args) -> None:
        """安全调用生命周期回调：回调异常绝不能把守护线程带崩。"""
        if cb is None:
            return
        try:
            cb(*args)
        except Exception as e:                       # noqa: BLE001
            log.error("重连回调异常", error=str(e), exc_info=True)

    def _signal_connection_lost(self, reason: str) -> None:
        """标记连接已断并唤醒守护线程。**幂等**，可由任意线程调用。"""
        with self._lock:
            if self._conn_lost_evt.is_set():
                return
            self._conn_lost_evt.set()
        log.warning("检测到连接断开", reason=reason)
        self._notify(self.on_connection_lost, reason)

    # ---------------- 断线守护线程 ----------------
    @property
    def _rotate_interval_s(self) -> float:
        """会话主动轮换间隔（秒）；0 = 关闭。"""
        minutes = getattr(
            self.cfg.asr.livetranslate, "session_rotate_minutes", 0
        ) or 0
        return float(minutes) * 60.0

    def _ensure_supervisor(self) -> None:
        """确保断线守护线程在跑（幂等）。"""
        th = self._supervise_thread
        if th is not None and th.is_alive():
            return
        self._supervise_thread = threading.Thread(
            target=self._supervise_loop, name="PipelineSupervise", daemon=True
        )
        self._supervise_thread.start()

    def _wait_trigger(self) -> str | None:
        """等「连接断开」或「会话到期轮换」。

        返回 ``'lost'`` / ``'rotate'`` / ``None``（程序正在退出）。
        """
        while not self._shutdown_evt.is_set():
            started = self._session_started_at
            rotate_s = self._rotate_interval_s
            if started is None or rotate_s <= 0:
                timeout = 0.5
            else:
                age = time.monotonic() - started
                if age >= rotate_s:
                    return "rotate"
                timeout = max(min(rotate_s - age, 1.0), 0.2)
            if self._conn_lost_evt.wait(timeout=timeout):
                self._conn_lost_evt.clear()
                return "lost"
        return None

    def _supervise_loop(self) -> None:
        """守护线程：连接断了自动重连；会话到期主动轮换。

        ⚠️ 这个线程**不能被 `stop()` join** —— 重连流程本身会调用
        `_stop_stream()`（join 搬运线程），如果它也 join 自己就死锁了。
        它只认 `_shutdown_evt`。
        """
        log.debug("断线守护线程已启动")
        while not self._shutdown_evt.is_set():
            trigger = self._wait_trigger()
            if trigger is None:
                return
            manual = self._manual_reconnect
            self._manual_reconnect = False
            if trigger == "rotate":
                self._rotate_session()
            else:
                self._auto_reconnect(immediate=manual)
        log.debug("断线守护线程已退出")

    def _sleep_interruptible(self, seconds: float) -> bool:
        """退避等待，可被「程序退出」或「手动重连」打断。

        返回 True = 应当中止重连（程序正在退出）。
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._shutdown_evt.is_set():
                return True
            if self._manual_reconnect:
                log.info("手动重连请求打断退避等待")
                return False
            time.sleep(min(0.2, max(deadline - time.monotonic(), 0.0)))
        return False

    def _auto_reconnect(self, *, immediate: bool = False) -> None:
        """指数退避重连。`immediate=True`（手动触发）时首次不等。"""
        if self._reconnecting:
            log.info("重连已在进行中，忽略重复请求")
            return
        self._reconnecting = True
        try:
            for attempt in range(1, _RECONNECT_MAX_ATTEMPTS + 1):
                if self._shutdown_evt.is_set():
                    return
                delay = 0.0 if (immediate and attempt == 1) else min(
                    _RECONNECT_BASE_DELAY_S * (2 ** (attempt - 1)),
                    _RECONNECT_MAX_DELAY_S,
                )
                log.warning(
                    "准备重连云端",
                    attempt=attempt,
                    max_attempts=_RECONNECT_MAX_ATTEMPTS,
                    delay_s=round(delay, 1),
                )
                self._notify(self.on_reconnecting, attempt, delay)
                if delay > 0 and self._sleep_interruptible(delay):
                    return
                self._manual_reconnect = False
                try:
                    self._do_reconnect()
                except Exception as e:               # noqa: BLE001
                    log.error("重连失败", attempt=attempt, error=str(e))
                    continue
                if self._shutdown_evt.is_set():
                    # 重连过程中用户退出了 —— 不能把刚建好的连接留着
                    log.info("重连期间程序已在退出，关闭新连接")
                    self.stop()
                    return
                log.info("已重连", attempt=attempt,
                         connect_s=round(self._connect_s, 2))
                self._notify(self.on_reconnected, attempt)
                return
            log.error("自动重连已达上限，放弃",
                      attempts=_RECONNECT_MAX_ATTEMPTS)
            self._notify(self.on_reconnect_failed, _RECONNECT_MAX_ATTEMPTS)
        finally:
            self._reconnecting = False

    def _rotate_session(self) -> None:
        """会话到期，主动换一条新连接（规避服务端长会话退化）。"""
        age = (time.monotonic() - self._session_started_at
               if self._session_started_at else 0.0)
        log.info("会话到期，主动轮换连接", age_min=round(age / 60.0, 1))
        try:
            self._do_reconnect()
        except Exception as e:                       # noqa: BLE001
            log.error("会话轮换失败，转为自动重连", error=str(e))
            self._auto_reconnect()
            return
        log.info("会话轮换完成", connect_s=round(self._connect_s, 2))
        if self._shutdown_evt.is_set():
            log.info("轮换期间程序已在退出，关闭新连接")
            self.stop()
            return
        self._notify(self.on_reconnected, 0)

    def _do_reconnect(self) -> None:
        """重建云端连接（保留 `_capture` 对象，避免重新枚举音频设备）。

        ⚠️ 与 `stop()` 的唯一区别：**不设 `_shutdown_evt`** ——
        否则守护线程会被自己发起的重连干掉。
        """
        self._stop_evt.set()
        if self._capture is not None:
            self._capture.stop()
        self._stop_stream(timeout=5.0)
        self._stop_evt.clear()

        # 丢掉重连前积压的音频，避免把旧内容推给新连接
        if self._capture is not None:
            dropped = self._capture.drain()
            if dropped:
                log.info("重连时丢弃积压音频", blocks=dropped)

        self._live = create_live_translate(
            self.cfg.asr, on_event=self._on_live_event
        )
        self._start_stream()

    def stop(self, timeout: float = 5.0) -> None:
        # 永久停止：守护线程必须退出。
        # ⚠️ 不能只靠 `_stop_evt` —— 重连流程里会 set 再 clear 它，
        #    守护线程会误以为"又要继续跑了"。
        self._shutdown_evt.set()
        self._stop_evt.set()

        if self._capture is not None:
            self._capture.stop()

        if self.is_stream_mode:
            self._stop_stream(timeout=timeout)
            return

        for th in (getattr(self, "_bridge_thread", None), self._work_thread):
            if th is not None and th.is_alive():
                th.join(timeout=timeout)

        # 定稿残留音频
        if self._vad is not None:
            seg = self._vad.flush()
            if seg is not None:
                self._process_one(seg, is_partial=False)

        if self._asr is not None:
            self._asr.unload()

        log.info("流水线已停止", **{k: round(v, 3) if isinstance(v, float) else v
                                   for k, v in self._stats.items()})

    def _stop_stream(self, timeout: float = 5.0) -> None:
        """停止流式模式：等在途音频推完，再让云端交出最后一句。"""
        for th in (getattr(self, "_stream_thread", None),
                   getattr(self, "_pump_thread", None)):
            if th is not None and th.is_alive():
                th.join(timeout=timeout)

        if self._live is not None:
            # wait_final=True：通知服务端把缓冲的音频翻译完再关，
            # 否则最后半句会被丢弃
            self._live.stop(wait_final=True)
            self._live = None

        log.info(
            "流水线已停止（流式模式）",
            **{k: round(v, 3) if isinstance(v, float) else v
               for k, v in self._stats.items()},
        )

    def request_reconnect(self) -> None:
        """请求重连（托盘「重新连接」用）。**立即返回，不阻塞调用线程。**

        ⚠️ 老实现直接在调用线程（Qt 主线程）里跑完整重连，而重连内部
        `_start_stream()` → `live.start()` 最长阻塞 15s —— 点一下「重新连接」
        界面就卡死十几秒（实测问题）。现在只置信号，真正干活的是守护线程。

        与 `stop()` + `start()` 的区别：**保留 `_capture` 对象**，
        只重建 provider 与搬运线程，避免重新枚举音频设备。
        """
        if not self.is_stream_mode:
            raise AppError(
                "仅流式模式（asr.provider=livetranslate）支持重新连接",
                detail=f"当前 provider = {self.cfg.asr.provider}",
            )
        log.info("收到手动重连请求")
        self._manual_reconnect = True
        self._ensure_supervisor()
        self._signal_connection_lost("手动重连")

    def reconnect(self) -> None:
        """**同步**重建云端连接（阻塞到连上或超时）。

        只给命令行工具 / 测试用；UI 侧一律走 `request_reconnect()`，
        否则会把界面卡死。
        """
        if not self.is_stream_mode:
            raise AppError(
                "仅流式模式（asr.provider=livetranslate）支持重新连接",
                detail=f"当前 provider = {self.cfg.asr.provider}",
            )
        log.info("正在重新连接云端…")
        self._do_reconnect()
        log.info("已重新连接", connect_s=round(self._connect_s, 2))

    # ---------------- 流式事件处理 ----------------
    def _on_live_event(self, ev: TranslationEvent) -> None:
        """接收 LiveTranslate 服务端回调，包装后转给上层。

        ⚠️ 本回调运行在 SDK 的网络线程上，**绝不能做耗时操作**，
        否则会阻塞后续事件接收（表现为"越来越卡"）。
        这里只做轻量包装 + 一次回调调用。
        """
        if ev.kind == "error":
            log.error("实时翻译服务端错误", err=ev.translation)
            with self._lock:
                self._stats["errors"] += 1
            # ⚠️ 这里**只**走 on_error（→ 悬浮窗错误行 + 托盘变红）。
            #    老实现还额外 `_dispatch_translation()` 了一次，导致同一条错误
            #    在悬浮窗里显示两遍（`[APP_ERROR] 实时翻译服务端错误: xxx`
            #    + 裸消息 `xxx`）—— 2026-09-29 用户截图确认。
            #    连接层断开由守护线程自动重连，不再往 UI 重复推第二条。
            if self.on_error:
                try:
                    self.on_error(AppError(f"实时翻译服务端错误: {ev.translation}"))
                except Exception:
                    pass
            return

        with self._lock:
            self._seq += 1
            seq = self._seq
            if ev.kind == "translation" and not ev.is_partial:
                self._stats["segments"] += 1
            elif ev.is_partial:
                self._stats["partials"] += 1

        wrapped = TranslationTextEvent(ev, seq, time.monotonic())
        self._dispatch_translation(wrapped)

    def _dispatch_translation(self, ev: TranslationTextEvent) -> None:
        if self.on_translation is not None:
            try:
                self.on_translation(ev)
            except Exception as e:
                log.error("on_translation 回调异常", error=str(e), exc_info=True)
            return

        # 没给回调时退化为日志，便于命令行调试
        if ev.is_displayable:
            log.info(
                "翻译" if not ev.is_partial else "翻译(增量)",
                seq=ev.sequence,
                zh=ev.translation,
                en=ev.original or None,
            )

    # ---------------- 线程体 ----------------
    def _bridge_loop(self) -> None:
        """把采集的音频块喂给 VAD，产出句子/半句事件。"""
        assert self._capture is not None and self._vad is not None

        while not self._stop_evt.is_set():
            blk = self._capture.get_block(timeout=0.3)
            if blk is None:
                continue

            with self._lock:
                self._stats["blocks_in"] += 1

            try:
                events = self._vad.push(blk)
            except Exception as e:
                log.error("VAD 处理失败", error=str(e))
                with self._lock:
                    self._stats["errors"] += 1
                continue

            for ev in events:
                # 打上产生时间戳，用于计算排队等待
                try:
                    ev._enqueue_ts = time.monotonic()  # type: ignore[attr-defined]
                except AttributeError:
                    pass
                try:
                    self._work_q.put_nowait(ev)
                except queue.Full:
                    # 队列满说明 ASR 跟不上，丢弃最旧的
                    try:
                        self._work_q.get_nowait()
                        self._work_q.put_nowait(ev)
                    except queue.Empty:
                        pass
                    with self._lock:
                        self._stats["dropped_tasks"] = (
                            self._stats.get("dropped_tasks", 0) + 1
                        )
                    log.warning("识别队列已满，丢弃旧任务")

    def _work_loop(self) -> None:
        """消费句子/半句，调用 ASR。"""
        while not self._stop_evt.is_set():
            try:
                item = self._work_q.get(timeout=0.2)
            except queue.Empty:
                continue
            is_partial = isinstance(item, PartialEvent)
            self._process_one(item, is_partial=is_partial)

    def _process_one(
        self, item: SpeechSegment | PartialEvent, *, is_partial: bool
    ) -> None:
        assert self._asr is not None

        # 分阶段计时，便于定位延迟来源
        #   queue_wait: 从产生到开始处理（说明 ASR 是否积压）
        #   asr_time  : 纯识别耗时
        enq_ts = getattr(item, "_enqueue_ts", None)
        queue_wait = (time.monotonic() - enq_ts) if enq_ts else 0.0
        t_asr = time.monotonic()

        try:
            result = self._asr.transcribe(
                item.audio,
                language=self.cfg.asr.language or None,
                is_partial=is_partial,
            )
        except AppError as e:
            log.error("识别失败", error=str(e))
            with self._lock:
                self._stats["errors"] += 1
            if self.on_error:
                self.on_error(e)
            return
        except Exception as e:
            log.error("识别异常", error=str(e), exc_info=True)
            with self._lock:
                self._stats["errors"] += 1
            if self.on_error:
                self.on_error(e)
            return

        asr_time = time.monotonic() - t_asr
        latency = queue_wait + asr_time

        # 当 provider 内部计时与外部计时差异显著时给出诊断
        # （帮助定位 CUDA 同步 / GIL 竞争 / 线程调度问题）
        drift = asr_time - result.elapsed_s
        if drift > 0.15:
            log.debug(
                "ASR 计时偏差较大",
                internal_ms=round(result.elapsed_s * 1000),
                external_ms=round(asr_time * 1000),
                drift_ms=round(drift * 1000),
                audio_s=round(result.audio_duration_s, 2),
            )

        with self._lock:
            self._seq += 1
            seq = self._seq
            if is_partial:
                self._stats["partials"] += 1
            else:
                self._stats["segments"] += 1
            self._stats["total_asr_s"] += result.elapsed_s
            self._stats["total_audio_s"] += result.audio_duration_s
            self._stats["max_latency_s"] = max(
                self._stats["max_latency_s"], latency
            )
            self._stats["total_queue_wait_s"] += queue_wait

        if result.is_empty:
            log.debug("识别结果为空，跳过", audio_s=round(result.audio_duration_s, 2))
            return

        event = TranscriptionEvent(
            result=result,
            is_partial=is_partial,
            segment=item,
            latency_s=latency,
            sequence=seq,
        )
        event.queue_wait_s = queue_wait
        event.asr_time_s = asr_time

        try:
            self.on_text(event)
        except Exception as e:
            log.error("on_text 回调异常", error=str(e), exc_info=True)

    # ---------------- 查询 ----------------
    @property
    def stats(self) -> dict[str, float | int]:
        with self._lock:
            return dict(self._stats)

    @property
    def asr_info(self) -> str:
        if self._live is not None:
            return self._live.info
        return self._asr.info if self._asr else "(未初始化)"

    @property
    def is_running(self) -> bool:
        return self._capture is not None and self._capture.is_running

    @property
    def connect_seconds(self) -> float:
        """流式模式下建连耗时；其它模式为 0。"""
        return getattr(self, "_connect_s", 0.0)

    @property
    def level(self) -> float:
        """当前采集电平（0~1），供 UI 显示音量指示。"""
        return self._capture.level if self._capture else 0.0
