"""阿里云百炼 Qwen3.5-LiveTranslate 实时翻译 Provider。

与其它 ASR Provider 的根本差异
--------------------------------
本项目其它 provider（local/cloud）都是 **HTTP 请求-响应** 模型：
    调用方给一段完整音频 → 返回识别结果

本 provider 是 **WebSocket 长连接 + 服务端 VAD** 模型：
    持续把音频流 append 上去 → 服务端自动切句 → 回调推送翻译结果

因此它**不实现 `AsrProvider.transcribe()` 的单次语义**，而是提供
`start()` / `push_audio()` / `stop()` 的流式接口，并用回调把结果
推给上层（悬浮窗）。

⚠️ 关键设计决策：**云端 VAD 与本地 VAD 二选一**
    本 provider 启用服务端 VAD（`enable_turn_detection=True`），
    上层**绝不能**再用 `VadSegmenter` 切句——两边各切一次会把句子切碎。
    接入 pipeline 时需走 `_stream_loop` 分支，绕过本地 VAD。

实测要点
--------
· 输入: PCM 16kHz / 16bit / mono（与现有采集管线完全对齐，无需重采样）
· 输出: 文本（output_modalities=[TEXT]，不产生 TTS 费用）
· 原文: 设 input_audio_transcription_model 可同时拿到英文原文（双语字幕）
· 热词: translation_params.corpus.phrases 强制术语映射（EVE 术语关键）
· 延迟: SDK 自带 get_last_first_text_delay() 可直接读取首字延迟
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ..errors import ASRError, MissingApiKeyError
from ..logging_setup import get_bound_logger

log = get_bound_logger(__name__)

# 默认端点。百炼支持两套：
#   · 通用（无需 WorkspaceId）: wss://dashscope.aliyuncs.com/api-ws/v1/realtime
#   · 业务空间专属（性能更好）: wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime
DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_MODEL = "qwen3.5-livetranslate-flash-realtime"
ASR_MODEL = "qwen3-asr-flash-realtime"

# 每次 append 的音频块大小（采样点）。1600 @16k = 100ms，
# 与官方示例一致；块太小会增加 WebSocket 帧开销。
CHUNK_SAMPLES = 1600


@dataclass
class TranslationEvent:
    """一条实时翻译事件。"""

    # 中文译文
    translation: str = ""
    # 英文原文（需启用 input_audio_transcription）
    original: str = ""
    # 事件类型: translation | original | speech_start | speech_stop | error
    kind: str = "translation"
    # 是否为增量（delta）而非定稿
    is_partial: bool = False
    # 距会话开始的秒数
    timestamp_s: float = 0.0
    # 服务端首字延迟（ms），仅翻译定稿事件带
    first_text_delay_ms: float | None = None
    # 识别的源语言（如 "en"），服务端原文事件带
    language: str = ""
    # 语音情绪（如 "angry" / "happy"），服务端原文事件带
    # 舰队场景可用于判断指挥是否在紧急喊话
    emotion: str = ""

    def __str__(self) -> str:
        if self.kind == "error":
            return f"[错误] {self.translation}"
        return self.translation or self.original


class LiveTranslateProvider:
    """Qwen3.5-LiveTranslate 实时翻译（WebSocket 流式）。"""

    name = "qwen-livetranslate"

    def __init__(
        self,
        *,
        api_key_env: str = "DASHSCOPE_API_KEY",
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        url: str | None = None,
        workspace: str | None = None,
        target_language: str = "zh",
        source_language: str | None = None,
        phrases: dict[str, str] | None = None,
        with_original: bool = True,
        enable_turn_detection: bool = True,
        silence_duration_ms: int = 500,
        vad_threshold: float = 0.2,
        voice: str = "Tina",
        on_event: Callable[[TranslationEvent], None] | None = None,
        connect_timeout_s: float = 15.0,
    ) -> None:
        self.api_key_env = api_key_env
        self._api_key = api_key
        self.model = model
        self.url = url or DEFAULT_URL
        self.workspace = workspace
        self.target_language = target_language
        self.source_language = source_language
        self.phrases = phrases or {}
        self.with_original = with_original
        self.enable_turn_detection = enable_turn_detection
        self.silence_duration_ms = silence_duration_ms
        self.vad_threshold = vad_threshold
        self.voice = voice
        self.on_event = on_event
        self.connect_timeout_s = connect_timeout_s

        self._conv = None
        self._connected = threading.Event()
        self._connect_error: Exception | None = None
        self._t0 = 0.0
        self._lock = threading.Lock()
        # 连接是否仍然可用。`on_open` 置 True、`on_close` 置 False。
        # ⚠️ 必须由**服务端主动关闭**来驱动（close code 1011 等），
        #    不能靠"推送失败"推断——否则网络抖动会被误判成断线。
        self._alive = False
        # 统计
        self._sent_chunks = 0
        self._sent_samples = 0

    @property
    def is_alive(self) -> bool:
        """连接是否仍然可用（`on_close` 之后恒为 False）。"""
        return self._alive and self._conv is not None

    # ---------------- 生命周期 ----------------
    def _resolve_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        import os

        key = os.environ.get(self.api_key_env)
        if not key:
            raise MissingApiKeyError(
                f"环境变量 {self.api_key_env} 未设置",
                detail=f"设置方法: set {self.api_key_env}=sk-你的百炼Key\n"
                "获取地址: https://bailian.console.aliyun.com/",
            )
        return key

    def start(self) -> None:
        """建立 WebSocket 连接并配置会话。

        阻塞直到连接就绪或超时（默认 15s）。
        """
        try:
            from dashscope.audio.qwen_omni import (
                MultiModality,
                OmniRealtimeCallback,
                OmniRealtimeConversation,
            )
            from dashscope.audio.qwen_omni.omni_realtime import TranslationParams
        except ImportError as e:
            raise ASRError(
                "缺少 dashscope SDK",
                detail="pip install 'dashscope>=1.25.6'",
            ) from e

        api_key = self._resolve_api_key()
        outer = self

        class _Callback(OmniRealtimeCallback):
            def on_open(self) -> None:
                outer._alive = True
                log.info("LiveTranslate 连接已建立", model=outer.model)
                outer._connected.set()

            def on_close(self, code, msg) -> None:
                # 先置位再打日志：pipeline 的推送循环靠这个标志**立刻停止推送**，
                # 否则断线后会以 ~25 次/秒的速度无限刷「音频推送失败」日志
                # （实测刷了 15000+ 条 / 4.6MB，把有用日志全挤掉）。
                outer._alive = False
                log.info("LiveTranslate 连接关闭", close_code=code,
                         close_reason=str(msg)[:120])

            def on_event(self, response: dict) -> None:
                outer._handle_event(response)

        kwargs: dict = {
            "model": self.model,
            "callback": _Callback(),
            "api_key": api_key,
        }
        if self.url:
            kwargs["url"] = self.url
        if self.workspace:
            kwargs["workspace"] = self.workspace

        t0 = time.monotonic()
        self._conv = OmniRealtimeConversation(**kwargs)

        try:
            self._conv.connect()
        except Exception as e:
            raise ASRError(
                f"连接实时翻译服务失败: {e}",
                detail=f"url={self.url} workspace={self.workspace or '(未设置)'}",
            ) from e

        if not self._connected.wait(self.connect_timeout_s):
            raise ASRError(
                f"连接超时（{self.connect_timeout_s}s）",
                detail=f"错误: {self._connect_error}",
            )

        # 配置会话：只要文本，开启原文识别，启用服务端 VAD
        #
        # ⚠️ voice 必须传合法值，即使 output_modalities 只有 TEXT！
        #    SDK 在 voice=None 时会序列化成字符串 "null"，服务端直接拒绝：
        #      <400> InternalError.Algo.InvalidParameter: Voice 'null' is not supported.
        #    这是实测踩到的坑，文档未提及。
        tparams = TranslationParams(
            language=self.target_language,
            corpus=(TranslationParams.Corpus(phrases=self.phrases)
                    if self.phrases else None),
        )
        self._conv.update_session(
            output_modalities=[MultiModality.TEXT],
            voice=self.voice or "Tina",
            input_audio_transcription_model=(
                ASR_MODEL if self.with_original else None
            ),
            enable_turn_detection=self.enable_turn_detection,
            turn_detection_silence_duration_ms=self.silence_duration_ms,
            turn_detection_threshold=self.vad_threshold,
            translation_params=tparams,
        )

        self._t0 = time.monotonic()

        # 给服务端一点时间回应 session.updated / error。
        # 若配置被拒（如 voice 非法），错误事件会先于此处到达，
        # 立即抛出而不是等到推送音频时才崩。
        time.sleep(0.4)
        if self._connect_error is not None:
            err = self._connect_error
            self._connect_error = None
            try:
                self._conv.close()
            except Exception:
                pass
            self._conv = None
            raise ASRError(
                f"会话配置被服务端拒绝: {err}",
                detail=(err.detail or "")
                + "\n提示: 检查 target_language / voice / silence_duration_ms 是否合法",
            )

        log.info(
            "实时翻译会话已配置",
            target=self.target_language,
            source=self.source_language or "auto",
            phrases=len(self.phrases),
            with_original=self.with_original,
            setup_s=round(time.monotonic() - t0, 2),
        )

    def push_audio(self, audio: np.ndarray) -> None:
        """把 float32 mono 16k 音频追加到云端缓冲区。

        云端 VAD 会自动检测语音起止并触发翻译。

        ⚠️ 连接已断开时**抛异常**，不静默返回：调用方（`Pipeline._pump_loop`）
        靠它停止推送并触发重连。**这里绝不能打日志** —— 推送循环每秒调 20+ 次，
        断线后逐条打会把 5MB 的轮转日志几秒刷满（实测刷了 15000+ 条），
        日志由调用方「只在第一次失败时」统一打。
        """
        if self._conv is None:
            raise ASRError("会话未启动，请先调用 start()")

        if audio.size == 0:
            return

        if not self._alive:
            raise ASRError("连接已断开（服务端已关闭 WebSocket）")

        # float32 [-1,1] → int16 PCM
        pcm16 = np.clip(audio, -1.0, 1.0)
        pcm16 = (pcm16 * 32767.0).astype(np.int16)

        # 按 CHUNK_SAMPLES 切片发送，避免单帧过大
        for i in range(0, len(pcm16), CHUNK_SAMPLES):
            chunk = pcm16[i : i + CHUNK_SAMPLES]
            if chunk.size == 0:
                continue
            b64 = base64.b64encode(chunk.tobytes()).decode("ascii")
            with self._lock:
                try:
                    self._conv.append_audio(b64)
                except Exception as e:
                    # 连接已不可用 → 置位，让推送循环下一轮立刻退出（不再刷日志）
                    self._alive = False
                    raise ASRError(f"音频推送失败: {e}") from e
                self._sent_chunks += 1
                self._sent_samples += len(chunk)

    def stop(self, *, wait_final: bool = True) -> None:
        """结束会话，等待服务端完成最后一句翻译。"""
        if self._conv is None:
            return
        try:
            if wait_final:
                # end_session 会通知服务端把缓冲的音频翻译完
                self._conv.end_session()
            self._conv.close()
        except Exception as e:
            log.debug("关闭会话时异常（可忽略）", error=str(e))
        finally:
            self._conv = None
            self._connected.clear()
            log.info(
                "实时翻译会话已结束",
                chunks=self._sent_chunks,
                audio_s=round(self._sent_samples / 16000.0, 1),
            )

    # ---------------- 事件处理 ----------------
    #
    # ⚠️ 事件名随 output_modalities 变化（实测确认，文档只写了 audio 模式）
    #
    #   纯文本模式（output_modalities=[TEXT]，本项目使用）：
    #     翻译增量: response.text.text
    #     翻译定稿: response.text.done
    #   含音频模式（[TEXT, AUDIO]）：
    #     翻译增量: response.audio_transcript.delta
    #     翻译定稿: response.audio_transcript.done
    #
    #   原文识别（两种模式相同）：
    #     增量: conversation.item.input_audio_transcription.text
    #     定稿: conversation.item.input_audio_transcription.completed
    #
    #   本项目两种事件名都监听，以免切换 modalities 时失效。
    def _handle_event(self, response: dict) -> None:
        """把服务端事件转换为 TranslationEvent 并回调。"""
        etype = response.get("type", "")

        if etype == "error":
            err = response.get("error", {}) or {}
            msg = err.get("message", "未知错误")
            # 注意：不能用 message= 作为日志字段，它是 LogRecord 保留名
            log.error("服务端返回错误", err_code=err.get("code"), err_msg=msg)
            # 记录以便 start()/push_audio() 能感知，而不是静默继续推送
            self._connect_error = ASRError(
                f"服务端错误: {msg}",
                detail=f"code={err.get('code')}",
            )
            self._emit(TranslationEvent(kind="error", translation=msg))
            return

        # ---- 原文识别（源语言，即英文）----
        if etype == "conversation.item.input_audio_transcription.completed":
            text = (response.get("transcript") or "").strip()
            if text:
                self._emit(
                    TranslationEvent(
                        kind="original",
                        original=text,
                        language=response.get("language") or "",
                        emotion=response.get("emotion") or "",
                    )
                )
            return

        if etype == "conversation.item.input_audio_transcription.text":
            # 注意：此事件的 text 与 stash 需拼接才是完整内容
            text = self._join_stash(response)
            if text:
                self._emit(
                    TranslationEvent(kind="original", original=text,
                                     is_partial=True)
                )
            return

        # ---- 翻译定稿（纯文本模式）----
        if etype == "response.text.done":
            text = (response.get("text") or "").strip()
            if text:
                self._emit(
                    TranslationEvent(
                        kind="translation",
                        translation=text,
                        first_text_delay_ms=self._read_first_text_delay(),
                    )
                )
            return

        # ---- 翻译增量（纯文本模式）----
        if etype == "response.text.text":
            text = self._join_stash(response)
            if text:
                self._emit(
                    TranslationEvent(kind="translation", translation=text,
                                     is_partial=True)
                )
            return

        # ---- 翻译定稿（含音频模式，兼容保留）----
        if etype == "response.audio_transcript.done":
            text = (response.get("transcript") or "").strip()
            if text:
                self._emit(
                    TranslationEvent(
                        kind="translation",
                        translation=text,
                        first_text_delay_ms=self._read_first_text_delay(),
                    )
                )
            return

        if etype == "response.audio_transcript.delta":
            text = (response.get("delta") or "").strip()
            if text:
                self._emit(
                    TranslationEvent(kind="translation", translation=text,
                                     is_partial=True)
                )
            return

        # ---- VAD 事件 ----
        if etype == "input_audio_buffer.speech_started":
            self._emit(TranslationEvent(kind="speech_start"))
            return

        if etype == "input_audio_buffer.speech_stopped":
            self._emit(TranslationEvent(kind="speech_stop"))
            return

        log.debug("未处理的服务端事件", type=etype)

    @staticmethod
    def _join_stash(response: dict) -> str:
        """拼接流式事件的 text + stash 字段。

        Qwen 的流式增量事件里，已确定部分在 `text`，
        尚不稳定的尾部在 `stash`，两者拼接才是当前完整内容。
        """
        text = (response.get("text") or "").strip()
        stash = (response.get("stash") or "").strip()
        return (text + stash).strip()

    def _read_first_text_delay(self) -> float | None:
        """读取 SDK 内置的首字延迟（ms）。"""
        try:
            d = self._conv.get_last_first_text_delay()
            return float(d) * 1000.0 if d is not None else None
        except Exception:
            return None

    def _emit(self, ev: TranslationEvent) -> None:
        ev.timestamp_s = time.monotonic() - self._t0 if self._t0 else 0.0
        if ev.kind in ("translation", "original") and not ev.is_partial:
            log.info(
                "翻译结果",
                kind=ev.kind,
                text=ev.translation or ev.original,
                first_text_ms=(round(ev.first_text_delay_ms)
                               if ev.first_text_delay_ms else None),
            )
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception as e:
                log.error("事件回调异常", error=str(e))

    @property
    def info(self) -> str:
        return (
            f"{self.name}({self.model}/{self.target_language}"
            f"/phrases={len(self.phrases)})"
        )
