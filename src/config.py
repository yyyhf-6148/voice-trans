"""集中式配置管理：加载 YAML -> 类型化对象 -> 启动时校验（fail fast）。

用法：
    from src.config import load_config
    cfg = load_config()          # 默认读取项目根 config.yaml
    print(cfg.asr.model)
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_type_hints

import yaml

from .errors import ConfigError


# =========================================================
#  路径解析（源码运行 vs PyInstaller 打包运行）
# =========================================================
def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出来的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def _app_root() -> Path:
    """程序"根目录" —— `config.yaml` / `.env` / `logs` 都放在这里。

    - **源码运行**：项目根（`src/` 的上一级）
    - **打包运行**：**exe 所在目录**

    ⚠️ 打包后 `__file__` 指向 PyInstaller 的临时解包目录（`sys._MEIPASS`），
    该目录**进程一退出就被删除**。如果把 `.env` / `config.yaml` / `logs`
    写在它下面，用户填好的 API Key 关一次程序就没了。
    所以打包环境下必须改用 exe 自身所在目录。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundle_root() -> Path | None:
    """打包时**内置资源**所在目录（`sys._MEIPASS`）；源码运行返回 None。"""
    if not is_frozen():
        return None
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def user_data_dir() -> Path:
    """exe 目录不可写时的兜底数据目录（只读安装位置用）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path(tempfile.gettempdir())
    return root / "EVEVoiceTranslator"


# PyInstaller 引导器（bootloader）注入的环境变量。
# onefile 模式下引导器把归档解到 `%TEMP%\_MEIxxxxxx`，然后**用它自己重新拉起
# 一个子进程**（真正的 Python 解释器）来跑你的代码，并给子进程设置这些变量，
# 让子进程直接复用已解包的目录、不必再解一遍。
_BOOTLOADER_ENV_PREFIXES = ("_MEI", "_PYI_")


def child_process_env() -> dict[str, str]:
    """构造"重启自身"要传给子进程的环境变量。

    ⚠️ 为什么必须剥掉引导器变量（否则重启必崩）：
    onefile exe 里 `sys.executable` 就是 exe 自己。应用若直接
    `Popen(sys.executable)`，新进程会**继承** `_MEIPASS` / `_PYI_PARENT_PROCESS_LEVEL`
    等变量 → 引导器认为"父进程已经解过包了" → **复用父进程的 `_MEIxxxxxx` 目录**，
    不再解压自己的。
    可父进程（也就是当前进程）马上就要退出，最外层引导器退出时会把 `_MEIxxxxxx`
    **整个删掉**；新进程边被删边 import（`PySide6.QtWidgets` 有上百个文件）→
    `ModuleNotFoundError: No module named 'PySide6.QtWidgets'`。

    剥掉之后新进程就是"干净的一次启动"，会重新解压自己的 `_MEI` 目录，与父进程无关。

    源码运行（非冻结）时这些变量本就不存在，函数原样返回。
    """
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(_BOOTLOADER_ENV_PREFIXES):
            env.pop(name, None)
    return env


PROJECT_ROOT = _app_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def ensure_user_files() -> list[Path]:
    """首次运行时把**内置的默认文件**铺到 exe 旁边。

    只复制目标**不存在**的文件 —— 绝不覆盖用户已经改过的配置。
    返回本次实际创建的文件列表（源码运行时恒为空）。
    """
    created: list[Path] = []
    src_root = _bundle_root()
    if src_root is None:
        return created

    for name in ("config.yaml", "terms.yaml"):
        src = src_root / name
        dst = PROJECT_ROOT / name
        if not src.is_file() or dst.exists():
            continue
        try:
            shutil.copyfile(src, dst)
        except OSError:
            # 目录只读时留给 load_config 抛更明确的错误
            continue
        created.append(dst)
    return created


# =========================================================
#  配置数据类
# =========================================================
@dataclass
class AppSection:
    log_level: str = "INFO"
    log_dir: str = "logs"
    target_sample_rate: int = 16000


@dataclass
class AudioSection:
    # 采集后端: process（只抓指定进程，默认）| wasapi_loopback（整路系统音频）
    backend: str = "process"
    process_name: str = "mumble"
    device_keyword: str = ""
    block_ms: int = 32
    max_queue_blocks: int = 256
    auto_gain: bool = True
    silence_rms_threshold: float = 0.0015
    # 进程级采集的 WASAPI 缓冲（毫秒），仅 backend=process 时生效
    process_buffer_ms: int = 50


@dataclass
class VadSection:
    enabled: bool = True
    threshold: float = 0.5
    min_silence_ms: int = 420
    min_speech_ms: int = 250
    max_speech_ms: int = 12000
    padding_before_ms: int = 200
    partial_trigger_ms: int = 1800


@dataclass
class LocalAsrSection:
    """本地 faster-whisper 配置。"""

    # 模型: tiny | base | small | medium | large-v3
    model: str = "small"
    # 设备: auto | cuda | cpu
    #   auto = 有 CUDA 用 CUDA，否则 CPU
    device: str = "auto"
    # 计算精度: auto | float16 | float32 | int8_float16 | int8
    #   auto 会按显卡架构自动选择：
    #   Pascal(CC<7，如 P106/P100) 不支持 FP16，自动用 float32
    compute_type: str = "auto"
    # 线程数（CPU 模式生效）
    cpu_threads: int = 8
    # 模型下载目录，None = 默认缓存位置
    download_root: str | None = None
    # GPU 保活：周期性提交极小计算，试图阻止显卡空闲降频。
    #   ⚠️ P106-100 实测无效（2026-09-25）：显卡仍会在空闲 3-5s 后
    #   从 P0/1506MHz 掉到 P8/607MHz，识别延迟 320ms → 900ms。
    #   默认关闭，避免白占 CUDA context。仅在其他显卡上有据可查时可开。
    keep_alive: bool = False
    # 保活周期（秒）
    keep_alive_interval_s: float = 1.5


@dataclass
class CloudAsrSection:
    """云端 ASR 配置（OpenAI 兼容接口）。"""

    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "ASR_API_KEY"
    model: str = "whisper-1"
    timeout_s: int = 30
    # 瞬时错误重试次数（网络抖动一次就丢一句话，舰队场景建议 >=1）
    max_retries: int = 2
    # 启动时提前建连（把 TCP+TLS 握手开销移出首句路径）
    warmup: bool = True


@dataclass
class LiveTranslateSection:
    """Qwen3.5-LiveTranslate 实时翻译（流式，音频直入→中文文本）。

    与 local/cloud 的本质区别：
      这是 **端到端语音翻译**，一次调用同时完成识别+翻译，
      并自带服务端 VAD。走 WebSocket 长连接。

    ⚠️ 启用后上层**不得**再用本地 VadSegmenter 切句，
    否则两边各切一次会把句子切碎。
    """

    enabled: bool = False
    # API Key 环境变量名（百炼）
    api_key_env: str = "DASHSCOPE_API_KEY"
    model: str = "qwen3.5-livetranslate-flash-realtime"
    # 服务端点。留空 = SDK 默认（wss://dashscope.aliyuncs.com/api-ws/v1/realtime）
    # 业务空间专属端点性能更好，格式：
    #   wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime
    url: str | None = None
    # 业务空间 ID（用专属域名时需填）
    workspace: str | None = None
    # 目标语言（翻译成什么）
    target_language: str = "zh"
    # 源语言。留空 = 自动检测
    source_language: str | None = "en"
    # 是否同时返回源语言识别原文（双语字幕用）
    with_original: bool = True
    # 服务端 VAD 静音判句时长（ms），越小越快但可能切碎
    silence_duration_ms: int = 500
    # 服务端 VAD 灵敏度，越低越容易触发
    vad_threshold: float = 0.2
    # 音色（仅输出音频时生效；纯文本模式无影响）
    voice: str = "Tina"
    connect_timeout_s: float = 15.0
    # EVE 术语热词表：源语言术语 → 中文译法
    # 这是本方案相对通用翻译模型的核心优势——可强制术语准确
    #
    # ⚠️ 这是**运行期生效值**：加载配置时会把它替换成
    #    「外部文件（phrases_file）+ 本处内联」的**合并结果**（内联优先）。
    #    内联原始值（config.yaml 里 `phrases:` 块的内容）另存于
    #    `_phrases_inline` —— 设置界面编辑的是后者，避免把整张外部表
    #    回写进 config.yaml。
    phrases: dict[str, str] = field(default_factory=dict)
    # 外部热词表文件（YAML 顶层映射 `term: 译法`）。
    # 相对路径按 config.yaml 所在目录解析；留空 = 只用内联 phrases。
    # 词表很大（EVE 术语 600+ 条）时用这个，别塞进 config.yaml。
    phrases_file: str | None = None
    # 会话主动轮换间隔（分钟）。**0 = 关闭（默认）**。
    #
    # 背景：服务端模型偶发 `model repeat output happened`（COMMON_ERROR），
    # 随后以 WebSocket close code 1011 掐断会话。断线本身已由自动重连兜住
    # （1~30s 退避，见 pipeline 的守护线程），这个开关只是额外的"保险"：
    # 到点主动换一条新连接，避免长时间会话累积到退化状态。
    #
    # ⚠️ 默认关闭是有依据的：实测两次故障间隔只有 22 秒（16:21:33 / 16:21:55），
    #    中间还重连过一次 —— 说明**与"会话跑了多久"不相关**，轮换并不能避免。
    #    如果你后续观察到长会话故障率明显更高，再设成 20~25 即可。
    session_rotate_minutes: int = 0

    # ---- 以下为运行期附加字段，**不是** config.yaml 键 ----
    # 内联原始值（`phrases:` 块的内容，未经合并）
    _phrases_inline: dict[str, str] = field(default_factory=dict, repr=False)
    # 外部文件解析后的绝对路径（未配置 / 加载失败前为 None）
    _phrases_file_path: Path | None = field(default=None, repr=False)
    # 外部文件读到的条目数（仅用于界面提示；未配置为 0）
    _phrases_external_count: int = field(default=0, repr=False)


@dataclass
class AsrSection:
    # 后端: local | cloud | livetranslate
    provider: str = "local"
    # 源语言。空字符串 = 自动检测
    language: str = "en"
    # 识别上下文提示词（把专有名词写这里可提升准确率）
    initial_prompt: str = ""
    # beam search 宽度，1 = 贪心解码（最快）
    beam_size: int = 1
    # 启用 whisper 内置 VAD 过滤（与外部 VAD 不冲突）
    vad_filter: bool = False

    local: LocalAsrSection = field(default_factory=LocalAsrSection)
    cloud: CloudAsrSection = field(default_factory=CloudAsrSection)
    livetranslate: LiveTranslateSection = field(
        default_factory=LiveTranslateSection
    )


@dataclass
class OnlineTranslateSection:
    base_url: str = "https://api.deepseek.com/v1"
    api_key_env: str = "DEEPSEEK_API_KEY"
    model: str = "deepseek-chat"
    timeout_s: int = 8
    system_prompt: str = ""
    cache_size: int = 512


@dataclass
class LocalTranslateSection:
    model_name: str = "Helsinki-NLP/opus-mt-en-zh"
    max_length: int = 256


@dataclass
class TranslateSection:
    provider: str = "online"
    online: OnlineTranslateSection = field(default_factory=OnlineTranslateSection)
    local: LocalTranslateSection = field(default_factory=LocalTranslateSection)


@dataclass
class OverlaySection:
    enabled: bool = True
    opacity: float = 0.85
    position: str = "bottom_right"
    margin_x: int = 24
    margin_y: int = 80
    width: int = 560
    min_height: int = 120
    max_height: int = 460
    max_messages: int = 30
    font_size_translation: int = 18
    font_size_original: int = 12
    show_original: bool = True
    auto_fade_after_s: int = 0
    speaker_colors: list[str] = field(
        default_factory=lambda: ["#7dd3fc", "#fca5a5", "#86efac"]
    )

    # ---- P3 悬浮窗行为 ----
    # 启动时是否点击穿透（不挡游戏鼠标操作）。
    # 注意：点击穿透后无法用鼠标拖动窗口，只能靠热键切换回来。
    click_through: bool = False
    # 字幕保留时长（秒）：超过该时长且无新内容的条目会淡出。
    #   0 = 不淡出（一直保留到条数超限）
    # 舰队场景建议 20~40s：能回看刚才的指令，又不会堆满屏幕。
    hold_seconds: float = 30.0
    # 新条目到来时是否自动滚到底部。
    #   True  = 在底部时跟随新消息；鼠标上滚翻历史则暂停跟随，
    #           再滚回底部自动恢复（推荐）
    #   False = 视口完全不动，只能手动滚
    # 注：滚轮上滚可翻历史，下方还有内容时右下角提示条数；
    #     但 click_through=True 时鼠标事件整体穿透，滚轮不生效。
    auto_scroll: bool = True
    # 窗口背景色（#RRGGBB 或 #AARRGGBB）。
    # 用带 alpha 的写法可让背景比文字更透明，观感更清爽。
    background: str = "#E6101216"
    # 译文文字颜色
    color_translation: str = "#FFFFFF"
    # 原文文字颜色
    color_original: str = "#9AA4B2"
    # 错误提示颜色
    color_error: str = "#F87171"
    # 高亮情绪（指挥喊话）时译文旁显示的标记颜色
    color_alert: str = "#FBBF24"
    # 字体族。留空 = 自动挑选（优先微软雅黑，保证中文不出方块）
    font_family: str = ""
    # 行间距（像素）
    line_spacing: int = 4
    # 单条消息最多显示多少字符（防超长文本撑爆窗口），0 = 不限制
    max_chars_per_message: int = 300


@dataclass
class HotkeysSection:
    toggle_visibility: str = "ctrl+alt+h"
    opacity_up: str = "ctrl+alt+up"
    opacity_down: str = "ctrl+alt+down"
    toggle_click_through: str = "ctrl+alt+m"
    quit: str = "ctrl+alt+q"
    # 拖动窗口后按此键让它回到配置的角落（留空 = 不注册）
    recenter: str = "ctrl+alt+r"


@dataclass
class TraySection:
    """系统托盘图标。

    托盘是**唯一的鼠标退出路径** —— 悬浮窗是无边框的，
    没有关闭按钮，全靠托盘菜单或 Ctrl+Alt+Q。
    """

    enabled: bool = True


@dataclass
class Config:
    app: AppSection = field(default_factory=AppSection)
    audio: AudioSection = field(default_factory=AudioSection)
    vad: VadSection = field(default_factory=VadSection)
    asr: AsrSection = field(default_factory=AsrSection)
    translate: TranslateSection = field(default_factory=TranslateSection)
    overlay: OverlaySection = field(default_factory=OverlaySection)
    hotkeys: HotkeysSection = field(default_factory=HotkeysSection)
    tray: TraySection = field(default_factory=TraySection)

    # 运行期附加信息
    source_path: Path | None = None

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def log_dir(self) -> Path:
        d = PROJECT_ROOT / self.app.log_dir
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            # 打包后 exe 可能被放在只读位置（如 Program Files）
            fallback = user_data_dir() / self.app.log_dir
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback


# =========================================================
#  加载逻辑
# =========================================================
_VALID_BACKENDS = {"wasapi_loopback", "process"}
_VALID_DEVICES = {"auto", "cuda", "cpu"}
_VALID_COMPUTE_TYPES = {
    "auto", "float16", "float32", "int8_float16", "int8", "bfloat16"
}
_VALID_ASR_PROVIDERS = {"local", "cloud", "livetranslate"}
_VALID_TRANSLATE_PROVIDERS = {"online", "local"}
_VALID_POSITIONS = {"bottom_right", "bottom_left", "top_right", "top_left", "bottom_center"}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# 热词表条数硬上限（官方建议）：**合并后**（内联 + 外部文件）的总数
MAX_PHRASES = 1000

# 颜色：支持 #RGB / #RRGGBB / #AARRGGBB（Qt 的 QColor 也接受三种长度）
_COLOR_LEN = {4, 7, 9}


def _is_valid_color(s: object) -> bool:
    if not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) not in _COLOR_LEN or not s.startswith("#"):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in s[1:])


# 缓存 get_type_hints 结果，避免重复解析（且它比 f.type 慢）
_TYPE_HINTS_CACHE: dict[type, dict[str, Any]] = {}


def _resolved_hints(cls: type) -> dict[str, Any]:
    """取已求值的类型注解。

    注意：模块启用了 `from __future__ import annotations`，
    此时 `dataclasses.fields(cls)[i].type` 是**字符串**而非类型对象，
    必须用 get_type_hints 还原，否则嵌套 dataclass 无法被识别。
    """
    if cls not in _TYPE_HINTS_CACHE:
        try:
            _TYPE_HINTS_CACHE[cls] = get_type_hints(cls)
        except Exception:
            # 极端情况下退化为空字典，后续按未知类型处理
            _TYPE_HINTS_CACHE[cls] = {}
    return _TYPE_HINTS_CACHE[cls]


def _build_dataclass(cls: type, data: Any, path: str) -> Any:
    """把 dict 递归转换成 dataclass 实例。忽略未知字段但给出警告。"""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ConfigError(f"配置节 `{path}` 应为映射类型，实际为 {type(data).__name__}")

    hints = _resolved_hints(cls)
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(
            f"配置节 `{path}` 含未知字段: {', '.join(sorted(unknown))}",
            detail="请检查 config.yaml 拼写",
        )

    kwargs: dict[str, Any] = {}
    for name in known:
        if name not in data:
            continue
        raw = data[name]
        ftype = hints.get(name)
        # 处理 Optional[...] / Union[...]，取非 None 的那个类型
        origin = typing.get_origin(ftype)
        if origin is typing.Union:
            args = [a for a in typing.get_args(ftype) if a is not type(None)]
            ftype = args[0] if len(args) == 1 else ftype
        if isinstance(ftype, type) and is_dataclass(ftype):
            kwargs[name] = _build_dataclass(ftype, raw, f"{path}.{name}")
        else:
            kwargs[name] = raw
    return cls(**kwargs)


def _apply_env_overrides(cfg: Config) -> None:
    """允许用环境变量覆盖部分关键配置，便于临时调试。

    支持：
      TRANSLATE_API_KEY_ENV   覆盖 translate.online.api_key_env
      TRANSLATE_BASE_URL      覆盖 translate.online.base_url
      TRANSLATE_MODEL         覆盖 translate.online.model
      ASR_MODEL               覆盖 asr.model
      ASR_DEVICE              覆盖 asr.device
      AUDIO_BACKEND           覆盖 audio.backend
      AUDIO_PROCESS_NAME      覆盖 audio.process_name
    """
    mapping = {
        "TRANSLATE_API_KEY_ENV": ("translate", "online", "api_key_env"),
        "TRANSLATE_BASE_URL": ("translate", "online", "base_url"),
        "TRANSLATE_MODEL": ("translate", "online", "model"),
        "ASR_MODEL": ("asr", None, "model"),
        "ASR_DEVICE": ("asr", None, "device"),
        "AUDIO_BACKEND": ("audio", None, "backend"),
        "AUDIO_PROCESS_NAME": ("audio", None, "process_name"),
    }
    for env_name, (sec, sub, attr) in mapping.items():
        val = os.environ.get(env_name)
        if not val:
            continue
        target = getattr(cfg, sec)
        if sub:
            target = getattr(target, sub)
        setattr(target, attr, val)


def _validate(cfg: Config) -> None:
    """启动时严格校验，尽早暴露配置错误。"""
    errs: list[str] = []

    if cfg.app.log_level.upper() not in _VALID_LOG_LEVELS:
        errs.append(f"app.log_level 非法: {cfg.app.log_level}")

    if cfg.audio.backend not in _VALID_BACKENDS:
        errs.append(
            f"audio.backend 非法: {cfg.audio.backend}（可选: {', '.join(sorted(_VALID_BACKENDS))}）"
        )

    if not (8 <= cfg.audio.block_ms <= 500):
        errs.append(f"audio.block_ms 应在 8~500 之间，当前 {cfg.audio.block_ms}")

    if not (10 <= cfg.audio.process_buffer_ms <= 500):
        errs.append(
            f"audio.process_buffer_ms 应在 10~500 之间，"
            f"当前 {cfg.audio.process_buffer_ms}"
        )

    if cfg.audio.backend == "process" and not cfg.audio.process_name.strip():
        errs.append("audio.process_name 不能为空（backend=process 时需要目标进程名）")

    if cfg.asr.provider not in _VALID_ASR_PROVIDERS:
        errs.append(
            f"asr.provider 非法: {cfg.asr.provider}"
            f"（可选: {', '.join(sorted(_VALID_ASR_PROVIDERS))}）"
        )

    if cfg.asr.local.device not in _VALID_DEVICES:
        errs.append(
            f"asr.local.device 非法: {cfg.asr.local.device}"
            f"（可选: {', '.join(sorted(_VALID_DEVICES))}）"
        )

    if cfg.asr.local.compute_type not in _VALID_COMPUTE_TYPES:
        errs.append(
            f"asr.local.compute_type 非法: {cfg.asr.local.compute_type}"
            f"（可选: {', '.join(sorted(_VALID_COMPUTE_TYPES))}）"
        )

    # ---- livetranslate 专项校验 ----
    lt = cfg.asr.livetranslate
    if cfg.asr.provider == "livetranslate":
        if not lt.target_language:
            errs.append("asr.livetranslate.target_language 不能为空")
        if not (200 <= lt.silence_duration_ms <= 6000):
            errs.append(
                f"asr.livetranslate.silence_duration_ms 应在 200~6000 之间，"
                f"当前 {lt.silence_duration_ms}（服务端限制）"
            )
        if not (-1.0 <= lt.vad_threshold <= 1.0):
            errs.append(
                f"asr.livetranslate.vad_threshold 应在 -1.0~1.0 之间，"
                f"当前 {lt.vad_threshold}"
            )
        if lt.connect_timeout_s <= 0:
            errs.append(
                f"asr.livetranslate.connect_timeout_s 应 > 0，"
                f"当前 {lt.connect_timeout_s}"
            )
        if not (0 <= lt.session_rotate_minutes <= 1440):
            errs.append(
                "asr.livetranslate.session_rotate_minutes 应在 0~1440 之间"
                f"（0 = 关闭），当前 {lt.session_rotate_minutes}"
            )
        if len(lt.phrases) > MAX_PHRASES:
            errs.append(
                f"asr.livetranslate 热词过多（{len(lt.phrases)} 条），"
                f"官方建议不超过 {MAX_PHRASES} 条"
                "（含 phrases_file 外部表；内联优先）"
            )
        # 云端 VAD 与本地 VAD 冲突：两边各切一次会把句子切碎
        if cfg.vad.enabled:
            errs.append(
                "asr.provider=livetranslate 与 vad.enabled=true 冲突：\n"
                "      该模型自带服务端 VAD，本地 VAD 会重复切句导致句子碎片化。\n"
                "      请设置 vad.enabled = false（推荐），"
                "或改用 asr.provider = cloud"
            )
        if lt.url and "{WorkspaceId}" in lt.url:
            errs.append(
                "asr.livetranslate.url 含未替换的 {WorkspaceId} 占位符，"
                "请填入真实业务空间 ID，或留空使用默认端点"
            )

    if not (0.0 <= cfg.vad.threshold <= 1.0):
        errs.append(f"vad.threshold 应在 0~1 之间，当前 {cfg.vad.threshold}")

    if cfg.vad.min_silence_ms < 100:
        errs.append(f"vad.min_silence_ms 过小（{cfg.vad.min_silence_ms}），建议 >= 100")

    if cfg.vad.partial_trigger_ms > 0 and cfg.vad.partial_trigger_ms < 500:
        errs.append(
            f"vad.partial_trigger_ms 过小（{cfg.vad.partial_trigger_ms}），"
            "建议 >= 500，否则会把词切碎"
        )

    if cfg.translate.provider not in _VALID_TRANSLATE_PROVIDERS:
        errs.append(
            f"translate.provider 非法: {cfg.translate.provider}"
            f"（可选: {', '.join(sorted(_VALID_TRANSLATE_PROVIDERS))}）"
        )

    if cfg.overlay.position not in _VALID_POSITIONS:
        errs.append(
            f"overlay.position 非法: {cfg.overlay.position}（可选: {', '.join(sorted(_VALID_POSITIONS))}）"
        )

    if not (0.05 <= cfg.overlay.opacity <= 1.0):
        errs.append(f"overlay.opacity 应在 0.05~1.0 之间，当前 {cfg.overlay.opacity}")

    if cfg.overlay.min_height > cfg.overlay.max_height:
        errs.append("overlay.min_height 不能大于 max_height")

    if not (0.02 <= cfg.overlay.opacity <= 1.0):
        errs.append(
            f"overlay.opacity 应在 0.02~1.0 之间，当前 {cfg.overlay.opacity}"
        )

    if cfg.overlay.max_messages < 1:
        errs.append(f"overlay.max_messages 至少为 1，当前 {cfg.overlay.max_messages}")

    if cfg.overlay.hold_seconds < 0:
        errs.append(
            f"overlay.hold_seconds 不能为负，当前 {cfg.overlay.hold_seconds}"
        )

    for name in ("font_size_translation", "font_size_original"):
        v = getattr(cfg.overlay, name)
        if not (8 <= v <= 96):
            errs.append(f"overlay.{name} 应在 8~96 之间，当前 {v}")

    # 颜色字段校验（避免用户填错导致 Qt 静默变黑）
    for name in ("background", "color_translation", "color_original",
                 "color_error", "color_alert"):
        v = getattr(cfg.overlay, name)
        if not _is_valid_color(v):
            errs.append(
                f"overlay.{name} 不是合法颜色（应为 #RGB / #RRGGBB / #AARRGGBB），"
                f"当前 {v!r}"
            )

    # 只在 `translate.online` **真的会被用到**时才提示缺 Key。
    # `asr.provider = livetranslate` 时翻译由实时模型自己完成，整个
    # `translate.*` 段是死配置 —— 这时再喊"翻译功能将不可用"纯属误导
    # （打包版 stderr 是 None 看不见，源码运行则每次启动都刷一条）。
    if cfg.translate.provider == "online" and cfg.asr.provider != "livetranslate":
        key_env = cfg.translate.online.api_key_env
        if not os.environ.get(key_env):
            # 不阻断启动：P1/P2 阶段无需翻译
            import warnings

            warnings.warn(
                f"环境变量 {key_env} 未设置，翻译功能将不可用。"
                f"设置方法: set {key_env}=你的APIKey",
                stacklevel=2,
            )

    if errs:
        raise ConfigError(
            "配置校验失败:\n  - " + "\n  - ".join(errs),
            detail=f"配置文件: {cfg.source_path}",
        )


def _load_dotenv(path: Path) -> int:
    """把 .env 文件里的变量注入 os.environ（已存在的环境变量不覆盖）。

    轻量内置实现，避免引入 python-dotenv 依赖。
    支持：
      KEY=value
      KEY="value" / KEY='value'
      # 注释行
      export KEY=value   （兼容部分 shell 风格写法）

    Returns:
        成功注入的变量个数
    """
    if not path.exists():
        return 0

    count = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # 去掉成对引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        # 真实环境变量优先，不覆盖
        if key not in os.environ:
            os.environ[key] = val
            count += 1
    return count


def read_phrases_file(path: str | Path) -> dict[str, str]:
    """读取外部热词表文件（YAML 顶层映射 `term: 译法`）。

    - 支持 `.yaml` / `.yml`（走 PyYAML）
    - 值统一转成字符串；`null` 值的条目跳过（等于"未收录"）
    - 文件不存在 / YAML 语法错 / 顶层不是映射 → `ConfigError`（fail fast）

    设置界面用它做即时校验与条数提示，`load_config()` 用它做真实加载。
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(
            f"热词表文件不存在: {p}",
            detail="asr.livetranslate.phrases_file 指向的文件必须存在"
                   "（相对路径按 config.yaml 所在目录解析）",
        )
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"热词表 YAML 解析失败: {e}", detail=str(p)) from e
    except OSError as e:
        raise ConfigError(f"热词表文件读取失败: {e}", detail=str(p)) from e

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"热词表顶层应为映射（每行 `术语: 中文译法`），"
            f"实际是 {type(raw).__name__}",
            detail=str(p),
        )

    out: dict[str, str] = {}
    for k, v in raw.items():
        if v is None:
            continue
        key = str(k).strip()
        if not key:
            continue
        out[key] = str(v)
    return out


def _resolve_phrases_path(raw: str, cfg_path: Path) -> Path:
    """把 phrases_file 的相对路径按 config.yaml 所在目录解析。"""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = cfg_path.parent / p
    try:
        return p.resolve()
    except OSError:      # pragma: no cover - 极端路径
        return p


def _load_external_phrases(cfg: Config, cfg_path: Path) -> None:
    """把外部热词表合并进 `asr.livetranslate.phrases`。

    合并规则：**内联优先**（同名时 config.yaml 里的 `phrases:` 覆盖外部文件）。
    这样用户可以在设置界面里临时改一两个术语，不必去动大文件。

    副作用（均为运行期字段，不写回 config.yaml）：
      · `_phrases_inline`         内联原始值
      · `_phrases_file_path`      外部文件绝对路径
      · `_phrases_external_count` 外部文件条目数
    """
    lt = cfg.asr.livetranslate
    # `phrases:` 底下没有子行时 YAML 会解析成 None —— 归一化成空表
    if not isinstance(lt.phrases, dict):
        lt.phrases = {}
    lt._phrases_inline = dict(lt.phrases)
    lt._phrases_file_path = None
    lt._phrases_external_count = 0

    name = (lt.phrases_file or "").strip()
    if not name:
        return

    p = _resolve_phrases_path(name, cfg_path)
    lt._phrases_file_path = p
    external = read_phrases_file(p)          # 失败直接抛 ConfigError
    lt._phrases_external_count = len(external)

    merged = dict(external)
    merged.update(lt._phrases_inline)        # 内联优先
    lt.phrases = merged


def load_config(path: str | Path | None = None) -> Config:
    """加载并校验配置。

    会先从项目根目录的 .env 注入环境变量（API Key 等），
    使 _validate() 能正确判断密钥是否就绪。

    打包运行时，若 exe 旁边还没有 config.yaml，会先从内置资源复制一份
    （见 `ensure_user_files()`），用户改坏/删掉也能自愈。
    """
    if path is None:
        ensure_user_files()

    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH

    # .env 优先于 yaml，但让位于已存在的真实环境变量
    _load_dotenv(cfg_path.parent / ".env")

    if not cfg_path.exists():
        raise ConfigError(
            f"配置文件不存在: {cfg_path}",
            detail="请确认 config.yaml 位于项目根目录",
        )

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 解析失败: {e}", detail=str(cfg_path)) from e

    if not isinstance(raw, dict):
        raise ConfigError("配置根节点应为映射类型", detail=str(cfg_path))

    known_sections = {f.name for f in fields(Config) if f.name != "source_path"}
    unknown = set(raw) - known_sections
    if unknown:
        raise ConfigError(
            f"未知的配置节: {', '.join(sorted(unknown))}",
            detail=f"可用节: {', '.join(sorted(known_sections))}",
        )

    cfg = _build_dataclass(Config, raw, "root")
    cfg.source_path = cfg_path
    _apply_env_overrides(cfg)
    _load_external_phrases(cfg, cfg_path)
    _validate(cfg)
    return cfg
