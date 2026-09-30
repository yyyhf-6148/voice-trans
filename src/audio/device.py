"""音频设备枚举与自动选择。

提供与具体采集后端解耦的设备描述对象。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import DeviceNotFoundError
from ..logging_setup import get_bound_logger

log = get_bound_logger(__name__)


@dataclass(frozen=True)
class AudioDevice:
    """一个可采集的音频设备。"""

    id: str
    name: str
    is_loopback: bool = False
    channels: int = 2
    default_samplerate: int = 48000

    def __str__(self) -> str:
        tag = "[LOOPBACK]" if self.is_loopback else "[INPUT]   "
        return f"{tag} {self.name}  ({self.channels}ch @ {self.default_samplerate}Hz)"


def list_loopback_devices() -> list[AudioDevice]:
    """列出所有 WASAPI loopback 设备（即"扬声器"的回环）。

    依赖 soundcard 库。若未安装则抛出 AudioError。
    """
    from ..errors import AudioError

    try:
        import soundcard as sc
    except ImportError as e:
        raise AudioError(
            "缺少 soundcard 库",
            detail="pip install soundcard",
        ) from e

    devices: list[AudioDevice] = []
    try:
        speakers = sc.all_speakers()
    except Exception as e:  # pragma: no cover - 依赖系统状态
        raise AudioError(f"枚举扬声器失败: {e}") from e

    for spk in speakers:
        # soundcard 的 loopback 通过 speaker 对象的 player/microphone 语义区分。
        # 在 Windows 上，对 speaker 调用 `.recorder()` 即得到该设备的 loopback 源。
        devices.append(
            AudioDevice(
                id=str(spk.id),
                name=spk.name,
                is_loopback=True,
                channels=2,
                default_samplerate=48000,
            )
        )
    return devices


def list_input_devices() -> list[AudioDevice]:
    """列出所有麦克风设备（备用，非主路径）。"""
    from ..errors import AudioError

    try:
        import soundcard as sc
    except ImportError as e:
        raise AudioError("缺少 soundcard 库", detail="pip install soundcard") from e

    out: list[AudioDevice] = []
    for mic in sc.all_microphones(include_loopback=False):
        out.append(
            AudioDevice(
                id=str(mic.id),
                name=mic.name,
                is_loopback=False,
                channels=getattr(mic, "channels", 2) or 2,
                default_samplerate=48000,
            )
        )
    return out


def get_default_loopback() -> AudioDevice:
    """获取默认扬声器对应的 loopback 设备。"""
    from ..errors import AudioError

    try:
        import soundcard as sc
    except ImportError as e:
        raise AudioError("缺少 soundcard 库", detail="pip install soundcard") from e

    try:
        spk = sc.default_speaker()
    except Exception as e:
        raise DeviceNotFoundError(f"无法获取默认扬声器: {e}") from e

    if spk is None:
        raise DeviceNotFoundError(
            "系统没有可用的默认扬声器",
            detail="请检查声音输出设备是否正常",
        )

    return AudioDevice(
        id=str(spk.id),
        name=spk.name,
        is_loopback=True,
        channels=2,
        default_samplerate=48000,
    )


def resolve_device(device_keyword: str = "") -> AudioDevice:
    """按关键字查找 loopback 设备；关键字为空时返回默认设备。

    匹配规则：不区分大小写，子串匹配设备名。
    """
    if not device_keyword or not device_keyword.strip():
        dev = get_default_loopback()
        log.info("使用默认 loopback 设备", device=dev.name)
        return dev

    kw = device_keyword.strip().lower()
    candidates = list_loopback_devices()
    matched = [d for d in candidates if kw in d.name.lower()]

    if not matched:
        names = "\n    ".join(str(d) for d in candidates) or "(无)"
        raise DeviceNotFoundError(
            f"未找到名称包含 '{device_keyword}' 的音频设备",
            detail=f"当前可用设备:\n    {names}\n可通过 `python -m src.tools.list_devices` 查看完整列表",
        )

    dev = matched[0]
    if len(matched) > 1:
        log.warning(
            "关键字匹配到多个设备，使用第一个",
            keyword=device_keyword,
            chosen=dev.name,
            others=len(matched) - 1,
        )
    log.info("已选定 loopback 设备", device=dev.name)
    return dev
