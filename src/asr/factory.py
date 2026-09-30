"""ASR Provider 工厂：按配置创建对应的识别后端。"""
from __future__ import annotations

from ..config import AsrSection
from ..errors import ConfigError
from ..logging_setup import get_bound_logger
from .base import AsrProvider

log = get_bound_logger(__name__)

_PROVIDERS = {"local", "cloud", "livetranslate"}


def create_asr_provider(section: AsrSection) -> AsrProvider:
    """根据配置创建 ASR Provider（单次请求-响应语义）。

    ⚠️ `livetranslate` 是**流式** provider，不实现 AsrProvider 接口，
    不能从这里创建。请改用 `create_live_translate(section)`。
    这里遇到它会抛出带指引的 ConfigError。

    Args:
        section: 配置中的 asr 节

    Returns:
        AsrProvider 实例（尚未 load()）
    """
    provider = (section.provider or "local").lower()

    if provider not in _PROVIDERS:
        raise ConfigError(
            f"未知的 asr.provider: {provider}",
            detail=f"可选值: {', '.join(sorted(_PROVIDERS))}",
        )

    if provider == "livetranslate":
        raise ConfigError(
            "livetranslate 是流式 provider，不能用 create_asr_provider 创建",
            detail="请改用 src.asr.factory.create_live_translate(section)，"
            "它返回 LiveTranslateProvider（start/push_audio/stop 接口）",
        )

    if provider == "local":
        from .local_whisper import LocalWhisperProvider

        cfg = section.local
        log.info(
            "创建本地 ASR Provider",
            model=cfg.model,
            device=cfg.device,
            compute_type=cfg.compute_type,
        )
        return LocalWhisperProvider(
            model=cfg.model,
            device=cfg.device,
            compute_type=cfg.compute_type,
            language=section.language or None,
            initial_prompt=section.initial_prompt,
            beam_size=section.beam_size,
            vad_filter=section.vad_filter,
            cpu_threads=cfg.cpu_threads,
            download_root=cfg.download_root,
            keep_alive=cfg.keep_alive,
            keep_alive_interval_s=cfg.keep_alive_interval_s,
        )

    # cloud
    from .cloud_whisper import CloudWhisperProvider

    cfg = section.cloud
    log.info(
        "创建云端 ASR Provider",
        base_url=cfg.base_url,
        model=cfg.model,
    )
    return CloudWhisperProvider(
        base_url=cfg.base_url,
        api_key_env=cfg.api_key_env,
        model=cfg.model,
        language=section.language or None,
        timeout_s=cfg.timeout_s,
        initial_prompt=section.initial_prompt,
        max_retries=cfg.max_retries,
        warmup=cfg.warmup,
    )


def create_live_translate(section: AsrSection, on_event=None):
    """创建 Qwen3.5-LiveTranslate 实时翻译 provider（流式）。

    与 create_asr_provider 分离的原因：
        LiveTranslate 走 WebSocket 长连接 + 服务端 VAD，
        接口是 start() / push_audio() / stop()，
        与 AsrProvider 的 transcribe(audio)->result 语义不兼容。

    Args:
        section: 配置中的 asr 节
        on_event: 收到翻译事件时的回调（TranslationEvent -> None）

    Returns:
        LiveTranslateProvider 实例（尚未 start()）
    """
    from .live_translate import LiveTranslateProvider

    cfg = section.livetranslate
    log.info(
        "创建实时翻译 Provider",
        model=cfg.model,
        target=cfg.target_language,
        phrases=len(cfg.phrases),
    )
    return LiveTranslateProvider(
        api_key_env=cfg.api_key_env,
        model=cfg.model,
        url=cfg.url,
        workspace=cfg.workspace,
        target_language=cfg.target_language,
        source_language=cfg.source_language or None,
        phrases=cfg.phrases,
        with_original=cfg.with_original,
        silence_duration_ms=cfg.silence_duration_ms,
        vad_threshold=cfg.vad_threshold,
        voice=cfg.voice,
        connect_timeout_s=cfg.connect_timeout_s,
        on_event=on_event,
    )
