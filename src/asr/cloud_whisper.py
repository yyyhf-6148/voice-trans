"""云端 ASR 实现（OpenAI 兼容接口）。

适用场景：
  · AMD 显卡机器（无法用 CUDA）
  · 本机 GPU 不可用时的兜底
  · 需要更高识别质量的场合（可换成 large 模型）

支持任何 OpenAI `/v1/audio/transcriptions` 兼容的服务：
  · OpenAI 官方      base_url=https://api.openai.com/v1
  · 硅基流动          base_url=https://api.siliconflow.cn/v1
  · DeepSeek 生态     （若提供 ASR 端点）
  · 自建 faster-whisper-server / whisper.cpp server

实时场景要点（舰队语音，端到端 <2s）：
  · **连接预热**：首次请求要建 TCP+TLS（约 300ms），必须在 load() 阶段
    提前握手并保活（keep-alive），否则第一句延迟会爆
  · **重试**：网络抖动一次就丢一句话，必须带指数退避重试
  · **超时分级**：connect 超时短，read 超时长（模型推理本身耗时）
"""
from __future__ import annotations

import io
import os
import time
import wave

import numpy as np

from ..errors import MissingApiKeyError, UpstreamApiError
from ..logging_setup import get_bound_logger
from .base import AsrProvider, TranscriptionResult, TranscriptionSegment

log = get_bound_logger(__name__)

# 上游瞬时错误，值得重试
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def pcm_to_wav_bytes(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    """把 float32 mono 音频编码成 16-bit PCM WAV 字节流。"""
    pcm16 = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


class CloudWhisperProvider(AsrProvider):
    """通过 HTTP API 做语音识别。"""

    name = "cloud-whisper"

    def __init__(
        self,
        *,
        base_url: str,
        api_key_env: str,
        model: str = "whisper-1",
        language: str | None = "en",
        timeout_s: float = 30.0,
        initial_prompt: str = "",
        extra_headers: dict[str, str] | None = None,
        max_retries: int = 2,
        warmup: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.language = language or None
        self.timeout_s = timeout_s
        self.initial_prompt = initial_prompt or None
        self.extra_headers = extra_headers or {}
        self.max_retries = max(0, max_retries)
        self.warmup_enabled = warmup

        self._client = None

    # ---------------- 生命周期 ----------------
    def load(self) -> None:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise MissingApiKeyError(
                f"环境变量 {self.api_key_env} 未设置",
                detail=f"设置方法: set {self.api_key_env}=你的APIKey\n"
                "（设置后需重开终端生效）",
            )

        try:
            import httpx
        except ImportError as e:
            raise UpstreamApiError(
                "缺少 httpx 库",
                detail="pip install httpx",
            ) from e

        # HTTP/2 需要 h2 包；缺失时优雅回落 HTTP/1.1（不崩溃）
        try:
            import h2  # noqa: F401

            use_http2 = True
        except ImportError:
            use_http2 = False
            log.debug("未安装 h2，回落 HTTP/1.1（不影响功能，仅少些多路复用收益）")

        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                **self.extra_headers,
            },
            # connect 短超时（握手快失败），read 给足模型推理时间
            timeout=httpx.Timeout(self.timeout_s, connect=5.0),
            # 复用连接，避免每句都重新握手
            limits=httpx.Limits(
                max_keepalive_connections=4,
                max_connections=8,
                keepalive_expiry=90.0,
            ),
            http2=use_http2,
        )

        if self.warmup_enabled:
            self._warmup()

        log.info(
            "云端 ASR 已就绪",
            base_url=self.base_url,
            model=self.model,
            key_env=self.api_key_env,
            retries=self.max_retries,
        )

    def _warmup(self) -> None:
        """提前建立 TCP+TLS 连接，把握手开销移出首句路径。

        失败不致命（网络问题会在真正请求时报错），只记 debug。
        """
        t0 = time.monotonic()
        try:
            # 用模型列表端点做握手，不消耗识别配额
            r = self._client.get("/models", timeout=5.0)
            log.debug(
                "云端连接预热完成",
                status=r.status_code,
                elapsed_ms=round((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            log.debug("云端连接预热失败（不致命）", error=str(e))

    def unload(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ---------------- 识别 ----------------
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str | None = None,
        is_partial: bool = False,
    ) -> TranscriptionResult:
        if self._client is None:
            self.load()

        audio = self.validate_audio(audio)
        audio_dur = len(audio) / 16000.0
        lang = language or self.language

        if audio.size == 0:
            return TranscriptionResult(language=lang or "")

        wav_bytes = pcm_to_wav_bytes(audio)

        data: dict[str, str] = {
            "model": self.model,
            "response_format": "verbose_json",
        }
        if lang:
            data["language"] = lang
        if self.initial_prompt:
            data["prompt"] = self.initial_prompt

        t0 = time.monotonic()
        resp = self._post_with_retry(wav_bytes, data)
        elapsed = time.monotonic() - t0

        if resp is None:
            # 重试耗尽：返回空结果（调用方可据 elapsed 与空文本判断）
            return TranscriptionResult(
                language=lang or "",
                elapsed_s=elapsed,
                audio_duration_s=audio_dur,
            )

        if resp.status_code != 200:
            detail = resp.text[:300]
            log.error(
                "云端识别返回错误",
                status=resp.status_code,
                detail=detail,
            )
            raise UpstreamApiError(
                f"ASR 接口返回 {resp.status_code}",
                status=resp.status_code,
                detail=detail,
            )

        try:
            payload = resp.json()
        except Exception as e:
            raise UpstreamApiError(
                "ASR 响应不是合法 JSON",
                detail=resp.text[:300],
            ) from e

        segments = self._parse_segments(payload, is_partial)

        result = TranscriptionResult(
            segments=segments,
            language=payload.get("language", lang or ""),
            language_probability=0.0,
            elapsed_s=elapsed,
            audio_duration_s=audio_dur,
        )

        log.debug(
            "云端识别完成",
            text=result.text[:60] or "(空)",
            audio_s=round(audio_dur, 2),
            elapsed_s=round(elapsed, 3),
            partial=is_partial,
        )
        return result

    def _post_with_retry(self, wav_bytes: bytes, data: dict[str, str]):
        """POST 识别请求，对瞬时错误做指数退避重试。

        返回 httpx.Response；全部失败返回 None。
        4xx（除可重试状态码）直接返回响应，由调用方抛错——不浪费时间重试。
        """
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(
                    "/audio/transcriptions",
                    files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                    data=data,
                )
            except Exception as e:
                last_err = e
                log.warning(
                    "云端识别请求异常，准备重试",
                    attempt=attempt + 1,
                    max_attempts=self.max_retries + 1,
                    error=str(e),
                )
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code not in _RETRYABLE_STATUS:
                    # 参数/鉴权类错误，重试无意义
                    return resp
                last_err = None
                log.warning(
                    "云端识别返回可重试状态码",
                    attempt=attempt + 1,
                    status=resp.status_code,
                )

            if attempt < self.max_retries:
                # 指数退避：0.2s, 0.4s（实时场景不宜等太久）
                time.sleep(0.2 * (2**attempt))

        log.error(
            "云端识别重试耗尽",
            attempts=self.max_retries + 1,
            error=str(last_err) if last_err else "上游持续返回可重试状态码",
        )
        return None

    @staticmethod
    def _parse_segments(
        payload: dict, is_partial: bool
    ) -> list[TranscriptionSegment]:
        """解析不同服务商的响应格式。"""
        # 优先解析 verbose_json 的 segments
        raw_segs = payload.get("segments")
        if isinstance(raw_segs, list) and raw_segs:
            out: list[TranscriptionSegment] = []
            for s in raw_segs:
                text = str(s.get("text", "")).strip()
                if not text:
                    continue
                out.append(
                    TranscriptionSegment(
                        text=text,
                        start_s=float(s.get("start", 0.0) or 0.0),
                        end_s=float(s.get("end", 0.0) or 0.0),
                        avg_logprob=(
                            float(s["avg_logprob"])
                            if s.get("avg_logprob") is not None
                            else None
                        ),
                        is_partial=is_partial,
                    )
                )
            return out

        # 退化为纯文本响应
        text = str(payload.get("text", "")).strip()
        if not text:
            return []
        return [
            TranscriptionSegment(
                text=text, start_s=0.0, end_s=0.0, is_partial=is_partial
            )
        ]

    @property
    def info(self) -> str:
        return f"{self.name}({self.model})"
