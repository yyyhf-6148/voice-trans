"""类型化异常层级。

所有可预期的错误都应继承 AppError，便于上层统一处理与友好提示。
"""
from __future__ import annotations


class AppError(Exception):
    """应用级异常基类。"""

    code = "APP_ERROR"
    is_operational = True  # True = 可预期的运行时错误，非程序 bug

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"[{self.code}] {self.message} ({self.detail})"
        return f"[{self.code}] {self.message}"


# ---------------- 配置 ----------------
class ConfigError(AppError):
    code = "CONFIG_ERROR"


# ---------------- 音频 ----------------
class AudioError(AppError):
    code = "AUDIO_ERROR"


class DeviceNotFoundError(AudioError):
    code = "DEVICE_NOT_FOUND"


class AudioCaptureError(AudioError):
    code = "AUDIO_CAPTURE_ERROR"


# ---------------- 语音识别 ----------------
class ASRError(AppError):
    code = "ASR_ERROR"


class ModelLoadError(ASRError):
    code = "MODEL_LOAD_ERROR"


class DeviceUnavailableError(ASRError):
    code = "DEVICE_UNAVAILABLE"


# ---------------- 翻译 ----------------
class TranslationError(AppError):
    code = "TRANSLATION_ERROR"


class MissingApiKeyError(TranslationError):
    code = "MISSING_API_KEY"


class UpstreamApiError(TranslationError):
    code = "UPSTREAM_API_ERROR"

    def __init__(self, message: str, *, status: int | None = None, detail: str | None = None) -> None:
        super().__init__(message, detail=detail)
        self.status = status


# ---------------- UI ----------------
class UIError(AppError):
    code = "UI_ERROR"
