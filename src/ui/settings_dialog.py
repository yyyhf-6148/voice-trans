"""设置对话框：**schema 驱动**，聚焦 livetranslate 一条路。

为什么是 schema 驱动
--------------------
第一版是手写控件的，结果只暴露了 90 个配置键里的 35 个 ——
用户"打开设置找不到 API 接口地址"就是这么来的。手写必然漏。

现在改成声明式字段表（`Field` / `Group`）+ 通用渲染器：

    GROUP_* 常量声明每个字段的 key / 标签 / 控件类型 / 范围 / 校验规则
    SettingsDialog._build_widget() 按声明生成控件

只暴露用得上的键
----------------
本程序固定使用 `asr.provider = livetranslate`（Qwen3.5-LiveTranslate
端到端语音翻译：音频直入 → 中文文本出，**一次调用同时完成识别与翻译**）。

`cloud` / `local` 两个后端、级联翻译（`translate.*`）、本地 VAD（`vad.*`）、
以及只给 Whisper 用的通用识别参数，在 livetranslate 模式下
**运行期完全不被读取**（`pipeline.setup()` 在流式模式直接 return，
不会走到 `cfg.vad` / `cfg.asr.language` 那些分支），
所以从界面上收起来了 —— 它们仍然留在 `config.yaml` 里，需要时手动改即可。

有意不暴露的键连同理由写在 `_EXCLUDED` 里，
`tests/test_settings_coverage.py` 会断言
**config.yaml 里的每个叶子键要么被 Field 覆盖、要么在 _EXCLUDED 里写明理由**，
所以"界面不退化"这件事是可验证的。

设计约定
--------
**对话框只负责"收集改动"，不负责"落盘"**

    `yaml_updates` 返回 `{点分键: 新值}`，`env_updates` 返回
    `{环境变量名: 新值}`。真正的写文件与应用交给 `App`。
    这样配置写入逻辑可以脱离 Qt 单测。

**API Key 单独走 .env**

    `config.yaml` 没有进 .gitignore，密钥写进去有误提交风险。
    所以密钥框是独立控件，值**只写入 .env**（`apply_env_updates()`）。
    密钥框**明文预填**当前已配置的密钥，与「接口地址」同组相邻 ——
    用户打开设置就能一眼看到连到哪里、用的什么 Key。
    只有用户**真的改动过**才写 `.env`（预填不算改动，不产生无谓写入）。
    环境变量**名**（`asr.livetranslate.api_key_env`）是普通配置，照常写 yaml。

**可为空的字段 + fallback（直接显示实际生效值）**

    `url` / `workspace` 声明为 `nullable=True`：
    清空后送 `None`（写 `null`），而不是空串 —— 空串在 YAML 里是 `""`，
    语义上不是"未设置"。

    `url` 还声明了 `fallback`：配置为 `null` 时，框里**直接显示**实际生效的
    官方通用端点（`wss://dashscope.aliyuncs.com/api-ws/v1/realtime`），
    而不是留个空框让用户猜"到底连哪"。
    用户没动过这个框 → `_read()` 识别出"框里还是 fallback 且配置为 None"
    → 仍返回 `None`，**不会把默认值写进 config.yaml**（零配置漂移）。

**热应用 vs 需重启**

    `overlay.*` 与 `hotkeys.*` 可以立即生效；
    其余（`asr.* / audio.* / app.* / tray.*`）需要重启进程。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..logging_setup import get_bound_logger

log = get_bound_logger(__name__)

# 改这些前缀下的键可以热生效；其余需要重启进程
HOT_PREFIXES = ("overlay.", "hotkeys.")

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# =========================================================
#  字段声明
# =========================================================
@dataclass(frozen=True)
class Field:
    """一个设置项的声明。

    kind 决定控件类型与取值方式：
      text   QLineEdit（strip）           int    QSpinBox
      float  QDoubleSpinBox               bool   QCheckBox
      choice QComboBox（取 currentData）  color  QLineEdit + 取色按钮
      multi  QPlainTextEdit（多行文本）   dict   QPlainTextEdit（`k: v` 表）
      list   QPlainTextEdit（一行一项）
    """

    key: str
    label: str
    kind: str = "text"
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    decimals: int = 2
    suffix: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    editable: bool = False
    nullable: bool = False
    readonly: bool = False
    placeholder: str = ""
    width: int = 0
    validator: str = ""          # "" | "http" | "ws" | "env" | "nonempty"
    # 配置为 null 时，界面上**直接显示**这个"实际生效值"，而不是留个空框。
    # 用户没改它 → 视为未设置（不会把默认值写进 config.yaml，不产生漂移）。
    fallback: str = ""


@dataclass(frozen=True)
class Group:
    """一组字段（渲染成一个 QGroupBox）。"""

    title: str
    fields: tuple[Field, ...]
    note: str = ""


def _f(key: str, label: str, kind: str = "text", **kw: Any) -> Field:
    return Field(key=key, label=label, kind=kind, **kw)


def _ms(key: str, label: str, lo: int, hi: int, **kw: Any) -> Field:
    return _f(key, label, "int", minimum=lo, maximum=hi, suffix=" ms", **kw)


def _sec(key: str, label: str, lo: float, hi: float, **kw: Any) -> Field:
    return _f(key, label, "float", minimum=lo, maximum=hi, suffix=" s",
              step=1.0, decimals=1, **kw)


# ---------------- 常量选项 ----------------
_POSITIONS = (
    ("bottom_right", "右下角"),
    ("bottom_left", "左下角"),
    ("top_right", "右上角"),
    ("top_left", "左上角"),
    ("bottom_center", "底部居中"),
)

_AUDIO_BACKENDS = (
    ("process", "指定进程（推荐，只抓 Mumble）"),
    ("wasapi_loopback", "系统音频回环（整路，什么都收）"),
)

_LOG_LEVELS = (
    ("DEBUG", "DEBUG（最啰嗦，排查用）"),
    ("INFO", "INFO（默认）"),
    ("WARNING", "WARNING"),
    ("ERROR", "ERROR"),
)


# =========================================================
#  字段清单 —— 覆盖"本程序实际用到"的全部配置键
# =========================================================
# 第一组：连到哪里、用哪个模型 —— 用户最先要填的东西放在最上面
# =========================================================
# 官方通用端点。config.yaml 里 `url: null` 时用的就是它，
# 界面上要**直接显示出来**（用户反馈"接口地址看不到"），
# 而不是留个空框让人猜。
_DEFAULT_LIVETRANSLATE_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"

GROUP_SERVICE = Group(
    "接口与模型",
    (
        _f("asr.livetranslate.model", "模型名"),
        _f("asr.livetranslate.url", "接口地址（WebSocket 端点）", nullable=True,
           fallback=_DEFAULT_LIVETRANSLATE_URL,
           placeholder="留空 = 用上面的官方通用端点",
           validator="ws",
           help="留空走官方通用端点（即框里显示的这个）。"
                "业务空间专属端点格式："
                "wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"),
        _f("asr.livetranslate.workspace", "业务空间 ID", nullable=True,
           placeholder="留空 = 不使用专属端点",
           help="填了专属 url 才需要。"),
        _f("asr.livetranslate.api_key_env", "Key 环境变量名",
           validator="env", width=220,
           help="密钥本身在下面的「API Key」框里填，只写入 .env。"),
    ),
    note="上面四项 + 下面的 API Key 就是全部「连接信息」。"
         "接口地址是 wss:// 而不是 https:// —— 实时翻译需要全双工长连接，"
         "音频边录边推、译文边生成边回。",
)

# 第二组：连上之后怎么翻 —— 都有合理默认值，不调也能用
GROUP_LIVETRANSLATE = Group(
    "翻译参数",
    (
        _f("asr.livetranslate.enabled", "启用 LiveTranslate", "bool"),
        _f("asr.livetranslate.target_language", "目标语言", width=90),
        _f("asr.livetranslate.source_language", "源语言", width=90,
           placeholder="留空 = 自动"),
        _f("asr.livetranslate.with_original", "同时返回英文原文（双语字幕）",
           "bool"),
        _ms("asr.livetranslate.silence_duration_ms", "判句静音时长", 200, 6000,
            step=50,
            help="越小越快触发翻译，但可能把句子切碎。舰队场景建议 400~600。"),
        _f("asr.livetranslate.vad_threshold", "服务端 VAD 灵敏度", "float",
           minimum=-1.0, maximum=1.0, step=0.05,
           help="越低越容易把弱声音当语音（含背景噪声）。默认 0.2。"),
        _f("asr.livetranslate.voice", "音色", width=140,
           help="仅输出音频时生效；本程序只要文本，此项无影响。"),
        _sec("asr.livetranslate.connect_timeout_s", "连接超时", 1.0, 120.0),
        _f("asr.livetranslate.session_rotate_minutes", "会话轮换间隔", "int",
           minimum=0, maximum=1440, step=1, suffix=" 分钟", width=150,
           help="单位分钟，0 = 关闭（默认）。到点主动换一条新连接，"
                "规避服务端长会话退化（偶发 `model repeat output happened` "
                "后以 close 1011 掐断会话）。断线本身已有自动重连兜底，"
                "这是额外保险；实测故障与会话时长不相关，故默认关闭。"),
        _f("asr.livetranslate.phrases_file", "热词表文件", nullable=True,
           validator="file", width=320,
           placeholder="如 terms.yaml（留空 = 只用下面的内联表）",
           help="外部 YAML 热词表（每行 `术语: 中文译法`）。"
                "相对路径按 config.yaml 所在目录解析；"
                "与下面的内联表合并时**内联优先**。"
                "词表很大（EVE 术语 600+ 条）时用这个，别塞进 config.yaml。"),
        _f("asr.livetranslate.phrases", "内联热词表（覆盖外部）", "dict",
           help="匹配的是服务端识别出的**字面文本**。"
                "ASR 会把 Logi 听成 Logie、Cyno 听成 Sino —— "
                "把常见误听变体也加进去才会命中。"
                "这里只放**临时覆盖/追加**的少量条目；"
                "大表请写进上面的热词表文件。"),
    ),
    note="本地 VAD 已固定关闭 —— 该模型自带服务端 VAD，"
         "两边各切一次会把句子切碎。",
)

GROUP_AUDIO = Group(
    "采集",
    (
        _f("audio.backend", "采集后端", "choice", choices=_AUDIO_BACKENDS,
           help="process = 只抓指定进程（Mumble 专属，不掺游戏音效/音乐）；"
                "wasapi_loopback = 抓整路系统音频（扬声器上的一切声音）。"),
        _f("audio.process_name", "目标进程名", width=160,
           placeholder="不含 .exe",
           help="仅 backend=process 时生效。填写进程名（不含 .exe），"
                "如 mumble。找不到该进程时会自动降级为整路系统音频并告警。"),
        _f("audio.process_buffer_ms", "进程采集缓冲", "int",
           minimum=10, maximum=500, suffix=" ms",
           help="仅 backend=process 时生效。越小延迟越低，"
                "50ms 兼顾低延迟与抗抖动，不建议低于 20。"),
        _f("audio.device_keyword", "采集设备", "text",
           help="仅 backend=wasapi_loopback 时生效。"
                "留空 = 自动选默认扬声器对应的 loopback 设备。"
                "抓的是**扬声器回环**（系统播放的声音）。"),
        _ms("audio.block_ms", "采集分块", 8, 500,
            help="越小延迟越低，但 CPU 占用上升。"),
        _f("audio.max_queue_blocks", "采集队列上限", "int", minimum=16,
           maximum=4096,
           help="超过则丢弃最旧数据，防止内存膨胀。"),
        _f("audio.auto_gain", "自动增益（峰值归一化）", "bool",
           help="提升识别率。注意归一化有 20x 增益上限，"
                "不会把极弱信号拉满（否则会放爆底噪）。"),
        _f("audio.silence_rms_threshold", "静音阈值 (RMS)", "float",
           minimum=0.0, maximum=0.2, step=0.0005, decimals=4,
           help="低于该值的音频块不送入 VAD。"),
    ),
)

GROUP_OVERLAY_LOOK = Group(
    "外观",
    (
        _f("overlay.enabled", "启用悬浮窗", "bool"),
        _f("overlay.opacity", "不透明度", "float", minimum=0.05, maximum=1.0,
           step=0.05),
        _f("overlay.position", "位置", "choice", choices=_POSITIONS),
        _f("overlay.width", "窗口宽度", "int", minimum=200, maximum=2000,
           suffix=" px"),
        _f("overlay.min_height", "最小高度", "int", minimum=60, maximum=800,
           suffix=" px"),
        _f("overlay.max_height", "最大高度", "int", minimum=100,
           maximum=2000, suffix=" px"),
        _f("overlay.font_size_translation", "译文字号", "int", minimum=8,
           maximum=96),
        _f("overlay.font_size_original", "原文字号", "int", minimum=8,
           maximum=96),
        _f("overlay.font_family", "字体族", placeholder="留空 = 自动（优先微软雅黑）",
           help="留空可保证中文不出方块。"),
        _f("overlay.line_spacing", "行间距", "int", minimum=0, maximum=40,
           suffix=" px"),
    ),
)

GROUP_OVERLAY_BEHAVIOR = Group(
    "行为",
    (
        _f("overlay.margin_x", "水平边距", "int", minimum=0, maximum=500,
           suffix=" px"),
        _f("overlay.margin_y", "垂直边距", "int", minimum=0, maximum=500,
           suffix=" px"),
        _f("overlay.max_messages", "最多条数", "int", minimum=1, maximum=200),
        _f("overlay.hold_seconds", "字幕保留时长", "float", minimum=0.0,
           maximum=600.0, step=1.0, decimals=1, suffix=" s",
           help="0 = 不按时间移除，只在条数超限时丢最旧的。"
                "舰队场景建议 20~40。"),
        _f("overlay.auto_fade_after_s", "自动淡出延时", "int", minimum=0,
           maximum=600, suffix=" s",
           help="最后一条消息多久无新内容后开始降透明度，0 = 禁用。"),
        _f("overlay.auto_scroll", "新条目自动滚到底部", "bool",
           help="开：在底部时跟随新消息；鼠标上滚翻历史则暂停跟随，"
                "滚回底部自动恢复。关：视口完全不动，只能手动滚。"
                "（滚轮上滚随时可翻看历史，下方还有内容时右下角提示条数；"
                "但点击穿透开启时滚轮不生效）"),
        _f("overlay.show_original", "显示英文原文", "bool"),
        _f("overlay.click_through", "点击穿透（鼠标穿到游戏）", "bool",
           help="⚠️ 开启后无法用鼠标拖动窗口，需按热键关掉才能拖；"
                "鼠标滚轮也会一并穿透，无法翻看历史字幕。"
                "舰队场景建议：调好位置后改成 true。"),
        _f("overlay.max_chars_per_message", "单条最多字符数", "int",
           minimum=0, maximum=2000,
           help="0 = 不限制。防止超长文本撑爆窗口。"),
    ),
)

GROUP_OVERLAY_COLORS = Group(
    "配色",
    (
        _f("overlay.background", "背景色", "color",
           help="支持 #RRGGBB / #AARRGGBB。带 alpha 可以让底色比文字更透。"),
        _f("overlay.color_translation", "译文颜色", "color"),
        _f("overlay.color_original", "原文颜色", "color"),
        _f("overlay.color_error", "错误颜色", "color"),
        _f("overlay.color_alert", "高亮颜色", "color",
           help="指挥喊话时的情绪标记。"),
    ),
)

GROUP_OVERLAY_ADVANCED = Group(
    "高级",
    (
        _f("overlay.speaker_colors", "说话人配色", "list",
           help="每行一个颜色。P5 启用说话人分离后生效。"),
    ),
)

GROUP_HOTKEYS = Group(
    "全局快捷键",
    (
        _f("hotkeys.toggle_visibility", "显示 / 隐藏字幕"),
        _f("hotkeys.opacity_up", "提高不透明度"),
        _f("hotkeys.opacity_down", "降低不透明度"),
        _f("hotkeys.toggle_click_through", "切换点击穿透"),
        _f("hotkeys.recenter", "回到配置位置"),
        _f("hotkeys.quit", "退出程序"),
    ),
    note="语法示例：ctrl+alt+h、ctrl+shift+f1。留空表示不注册该热键。"
         "不需要管理员权限，也不会吃掉按键（游戏照常收到）。",
)

GROUP_TRAY = Group(
    "系统托盘",
    (
        _f("tray.enabled", "显示托盘图标", "bool",
           help="托盘是唯一的鼠标退出路径 —— 悬浮窗无边框、没有关闭按钮。"
                "关掉后仍可用退出热键。"),
    ),
)

GROUP_APP = Group(
    "日志与运行",
    (
        _f("app.log_level", "日志级别", "choice", choices=_LOG_LEVELS),
        _f("app.log_dir", "日志目录", placeholder="logs"),
        _f("app.target_sample_rate", "内部采样率", "int", readonly=True,
           suffix=" Hz",
           help="整条链路都按 16k 处理，改这个会让识别彻底失效。仅供查看。"),
    ),
)


# 有意不暴露的配置键 -> 理由。覆盖率测试会检查这个表。
#
# 这些键在 `asr.provider = livetranslate` 下**运行期不被读取**：
#   · pipeline.setup() 在流式模式直接 return，不会构造 VAD、
#     也不会走 cfg.asr.language / beam_size / initial_prompt / vad_filter；
#   · translate.* 目前只被 config.py 解析校验，没有任何代码消费它。
# 它们仍然保留在 config.yaml 里，需要时手动编辑即可。
_EXCLUDED: dict[str, str] = {
    # ---- 后端已固定 ----
    "asr.provider": "已固定为 livetranslate；如需切换后端请直接编辑 config.yaml",

    # ---- 仅 local / cloud 后端使用的通用 ASR 参数 ----
    "asr.language": "仅 local / cloud 后端使用；livetranslate 用 source_language",
    "asr.initial_prompt": "仅 local / cloud 后端使用；livetranslate 用热词表",
    "asr.beam_size": "仅 local / cloud 后端使用",
    "asr.vad_filter": "仅 local / cloud 后端使用",

    # ---- 本地 faster-whisper 专用 ----
    "asr.local.model": "本地 faster-whisper 专用（已从界面收起）",
    "asr.local.device": "本地 faster-whisper 专用（已从界面收起）",
    "asr.local.compute_type": "本地 faster-whisper 专用（已从界面收起）",
    "asr.local.cpu_threads": "本地 faster-whisper 专用（已从界面收起）",
    "asr.local.download_root": "本地 faster-whisper 专用（已从界面收起）",
    "asr.local.keep_alive": "本地 faster-whisper 专用（已从界面收起）",
    "asr.local.keep_alive_interval_s": "本地 faster-whisper 专用（已从界面收起）",

    # ---- 云端 ASR 专用 ----
    "asr.cloud.base_url": "云端 ASR 专用（已从界面收起）",
    "asr.cloud.api_key_env": "云端 ASR 专用（已从界面收起）",
    "asr.cloud.model": "云端 ASR 专用（已从界面收起）",
    "asr.cloud.timeout_s": "云端 ASR 专用（已从界面收起）",
    "asr.cloud.max_retries": "云端 ASR 专用（已从界面收起）",
    "asr.cloud.warmup": "云端 ASR 专用（已从界面收起）",

    # ---- 级联翻译：livetranslate 一次调用即出中文，用不到 ----
    "translate.provider": "级联翻译专用；livetranslate 不需要",
    "translate.online.base_url": "级联翻译专用；livetranslate 不需要",
    "translate.online.api_key_env": "级联翻译专用；livetranslate 不需要",
    "translate.online.model": "级联翻译专用；livetranslate 不需要",
    "translate.online.timeout_s": "级联翻译专用；livetranslate 不需要",
    "translate.online.cache_size": "级联翻译专用；livetranslate 不需要",
    "translate.online.system_prompt": "级联翻译专用；livetranslate 不需要",
    "translate.local.model_name": "级联翻译专用；livetranslate 不需要",
    "translate.local.max_length": "级联翻译专用；livetranslate 不需要",

    # ---- 本地 VAD：livetranslate 自带服务端 VAD，必须关闭 ----
    "vad.enabled": "livetranslate 自带服务端 VAD，此项必须为 false",
    "vad.threshold": "仅本地 VAD 使用；livetranslate 用服务端 VAD",
    "vad.min_silence_ms": "仅本地 VAD 使用；livetranslate 用服务端 VAD",
    "vad.min_speech_ms": "仅本地 VAD 使用；livetranslate 用服务端 VAD",
    "vad.max_speech_ms": "仅本地 VAD 使用；livetranslate 用服务端 VAD",
    "vad.padding_before_ms": "仅本地 VAD 使用；livetranslate 用服务端 VAD",
    "vad.partial_trigger_ms": "仅本地 VAD 使用；livetranslate 用服务端 VAD",
}


# =========================================================
#  热词表文本 <-> dict
# =========================================================
def format_phrases(phrases: dict[str, str]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in phrases.items())


def parse_phrases(text: str) -> dict[str, str]:
    """把 `key: 译法` 多行文本解析成 dict。

    容错：支持 `:` / `=` / 空格分隔，忽略空行与 `#` 注释，
    自动剥掉成对引号。无法解析的行直接跳过（不报错）。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        key = val = ""
        for sep in (":", "="):
            if sep in line:
                key, _, val = line.partition(sep)
                break
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            key, val = parts

        key = key.strip().strip("\"'")
        val = val.strip().strip("\"'")
        if key:
            out[key] = val
    return out


def parse_list(text: str) -> list[str]:
    """把多行文本解析成列表（只忽略空行）。

    ⚠️ 这里**不**把 `#` 当注释 —— 列表项本身就是颜色值（`#7dd3fc`），
    按注释过滤会把所有颜色吃掉。
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(line.strip("\"'"))
    return out


class SettingsDialog(QDialog):
    """设置窗口。`exec()` 返回 Accepted 后读取属性取结果。"""

    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg

        self.setWindowTitle("实时音频翻译 · 设置")
        self.resize(780, 720)
        # 悬浮窗是置顶的，对话框也必须置顶，否则会被它盖住
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self._fields: dict[str, QWidget] = {}
        self._specs: dict[str, Field] = {}
        self._orig: dict[str, Any] = {}
        self._readonly: set[str] = set()
        # 需要参与校验的键（本界面所有字段都在此列）
        self._always_keys: list[str] = []

        self._api_key_edit: QLineEdit | None = None
        self._api_key_hint: QLabel | None = None
        # 热词表条数提示（外部文件 + 内联合并后的实际生效条数）
        self._phrases_hint: QLabel | None = None
        # 打开对话框时环境变量里的密钥值（用于判断用户有没有改过）
        self._api_key_orig: str = ""
        self._error_label: QLabel | None = None

        self._yaml_updates: dict[str, Any] = {}
        self._env_updates: dict[str, str] = {}

        self._build()
        self._snapshot()

    # ================================================== 取值工具
    def _cfg_get(self, dotted: str) -> Any:
        obj: Any = self.cfg
        for part in dotted.split("."):
            obj = getattr(obj, part)
        return obj

    # ================================================== 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        tabs = QTabWidget(self)
        tabs.addTab(self._scroll(self._page_service()), "接口与密钥")
        tabs.addTab(
            self._scroll(self._page_groups((GROUP_AUDIO,))), "音频")
        tabs.addTab(
            self._scroll(self._page_groups(
                (GROUP_OVERLAY_LOOK, GROUP_OVERLAY_BEHAVIOR,
                 GROUP_OVERLAY_COLORS, GROUP_OVERLAY_ADVANCED))), "字幕")
        tabs.addTab(
            self._scroll(self._page_groups(
                (GROUP_HOTKEYS, GROUP_TRAY))), "快捷键与托盘")
        tabs.addTab(
            self._scroll(self._page_groups((GROUP_APP,))), "高级")
        self._tabs = tabs
        root.addWidget(tabs, 1)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color:#F87171; font-weight:bold;")
        self._error_label.setVisible(False)
        root.addWidget(self._error_label)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color:#94A3B8;")
        root.addWidget(self._hint)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存并应用")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        bar.addWidget(cancel)
        bar.addWidget(save)
        root.addLayout(bar)

    def _scroll(self, page: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(page)
        return area

    # ---------------- 分页 1：接口与密钥 ----------------
    def _page_service(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)

        intro = QLabel(
            "本程序固定使用 Qwen3.5-LiveTranslate 端到端语音翻译："
            "音频直入 → 中文文本出，一次调用同时完成识别与翻译。\n"
            "连接信息（接口地址 + API Key）就在下面第一组里，"
            "直接显示、直接改。"
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#94A3B8;")
        lay.addWidget(intro)

        # ---- 接口与模型（API Key 也挂在同一组里）----
        gb_service = self._make_group(GROUP_SERVICE)
        self._add_api_key_row(gb_service)
        lay.addWidget(gb_service)

        # ---- 翻译参数（连上之后怎么翻）----
        lay.addWidget(self._make_group(GROUP_LIVETRANSLATE))

        lay.addStretch(1)
        return page

    def _add_api_key_row(self, group_box: QGroupBox) -> None:
        """把 API Key 输入框追加进「接口与模型」组的表单。

        它**不是** `config.yaml` 里的键（密钥只进 `.env`），所以不能写进
        声明式 `Group.fields`，而是在这组渲染完之后手动补一行，
        让它和接口地址挨着 —— 用户要的就是"接口地址和 apikey 直接显示在一起"。
        """
        form = group_box.layout()
        if not isinstance(form, QFormLayout):   # pragma: no cover - 结构断言
            raise AssertionError("GROUP_SERVICE 的布局必须是 QFormLayout")

        self._api_key_edit = QLineEdit()
        # 明文直接显示（不打码、也没有「显示」勾选）
        self._api_key_edit.setEchoMode(QLineEdit.Normal)
        self._api_key_edit.setPlaceholderText(
            "粘贴 API Key（只写入 .env，不会写进 config.yaml）")
        self._api_key_edit.setMinimumWidth(380)
        form.addRow("API Key", self._api_key_edit)

        self._api_key_hint = QLabel("")
        self._api_key_hint.setStyleSheet("color:#94A3B8; font-size:11px;")
        self._api_key_hint.setWordWrap(True)
        form.addRow("", self._api_key_hint)

    # ---------------- 通用分页 ----------------
    def _page_groups(self, groups: tuple[Group, ...]) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(12)
        for g in groups:
            lay.addWidget(self._make_group(g))
        lay.addStretch(1)
        return page

    def _make_group(self, group: Group) -> QGroupBox:
        gb = QGroupBox(group.title)
        form = QFormLayout(gb)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for f in group.fields:
            self._always_keys.append(f.key)
            self._render_field(form, f)
        if group.note:
            form.addRow("", self._note_label(group.note))
        return gb

    def _note_label(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setStyleSheet("color:#94A3B8; font-size:11px;")
        return lb

    def _render_field(self, form: QFormLayout, f: Field) -> None:
        widget = self._build_widget(f)
        form.addRow(f.label, widget)
        if f.key == "asr.livetranslate.phrases":
            # 热词表是「外部文件 + 内联」两张表，光看这个框看不出实际生效多少条
            self._phrases_hint = self._note_label("")
            form.addRow("", self._phrases_hint)
            self._sync_phrases_hint()
        if f.help:
            form.addRow("", self._note_label(f.help))

    # ---------------- 控件工厂 ----------------
    def _build_widget(self, f: Field) -> QWidget:
        cur = self._cfg_get(f.key)
        self._specs[f.key] = f

        if f.kind == "color":
            # 颜色控件是"输入框 + 取色按钮"的组合，
            # 注册进 _fields 的必须是里面那个 QLineEdit（值从它读）
            holder, edit = self._color_row(f, str(cur or "#FFFFFF"))
            self._apply_common(edit, f)
            self._reg(f.key, edit)
            return holder

        if f.key == "audio.device_keyword":
            w: QWidget = self._device_combo(str(cur or ""))
        elif f.kind == "bool":
            cb = QCheckBox()
            cb.setChecked(bool(cur))
            w = cb
        elif f.kind == "choice":
            cbo = QComboBox()
            for value, label in f.choices:
                cbo.addItem(label, value)
            if f.editable:
                cbo.setEditable(True)
                cbo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            idx = cbo.findData(cur)
            if idx >= 0:
                cbo.setCurrentIndex(idx)
            elif f.editable:
                cbo.setEditText("" if cur is None else str(cur))
            w = cbo
        elif f.kind == "int":
            sb = QSpinBox()
            sb.setRange(int(f.minimum if f.minimum is not None else -10**9),
                        int(f.maximum if f.maximum is not None else 10**9))
            if f.step:
                sb.setSingleStep(int(f.step))
            if f.suffix:
                sb.setSuffix(f.suffix)
            sb.setValue(int(cur or 0))
            w = sb
        elif f.kind == "float":
            db = QDoubleSpinBox()
            db.setRange(f.minimum if f.minimum is not None else -1e9,
                        f.maximum if f.maximum is not None else 1e9)
            db.setDecimals(f.decimals)
            db.setSingleStep(f.step if f.step else 0.05)
            if f.suffix:
                db.setSuffix(f.suffix)
            db.setValue(float(cur or 0.0))
            w = db
        elif f.kind == "multi":
            te = QPlainTextEdit("" if cur is None else str(cur).rstrip("\n"))
            te.setMinimumHeight(120)
            te.setStyleSheet("font-family:Consolas,monospace; font-size:12px;")
            w = te
        elif f.kind == "dict":
            shown = cur
            if f.key == "asr.livetranslate.phrases":
                # 框里编辑的是**内联**表（config.yaml 的 `phrases:` 块），
                # 不是运行期合并后的整张表 —— 否则一保存就把外部文件那
                # 600+ 条全部回写进 config.yaml。
                shown = getattr(
                    self.cfg.asr.livetranslate, "_phrases_inline", {}) or {}
            te = QPlainTextEdit(format_phrases(dict(shown or {})))
            te.setPlaceholderText("每行一条，格式  term: 中文译法")
            te.setMinimumHeight(200)
            te.setStyleSheet("font-family:Consolas,monospace; font-size:12px;")
            w = te
        elif f.kind == "list":
            te = QPlainTextEdit("\n".join(str(x) for x in (cur or [])))
            te.setMinimumHeight(90)
            te.setStyleSheet("font-family:Consolas,monospace; font-size:12px;")
            w = te
        else:  # text
            # 配置为 null 且声明了 fallback → 直接把"实际生效值"填进去显示，
            # 而不是留个空框让用户以为没配置（`_read()` 会把它还原成 None，
            # 所以不碰它就不会把默认值写进 config.yaml）
            shown = f.fallback if (cur is None and f.fallback) else (
                "" if cur is None else str(cur))
            le = QLineEdit(shown)
            le.setPlaceholderText(f.placeholder)
            w = le

        self._apply_common(w, f)
        self._reg(f.key, w)
        return w

    def _apply_common(self, w: QWidget, f: Field) -> None:
        """所有控件共用的收尾设置（宽度 / 提示 / 只读 / 联动）。"""
        if f.width and hasattr(w, "setMaximumWidth"):
            w.setMaximumWidth(f.width)
        if f.help:
            w.setToolTip(f.help)
        if f.readonly:
            self._readonly.add(f.key)
            if isinstance(w, QLineEdit):
                w.setReadOnly(True)
                w.setStyleSheet("color:#94A3B8;")
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                # 用 readOnly 而不是 setEnabled(False)：
                # 保持正常外观，用户仍可选中复制，只是改不了
                w.setReadOnly(True)
                w.setButtonSymbols(QSpinBox.NoButtons)
                w.setStyleSheet("color:#94A3B8;")
            elif isinstance(w, QComboBox):
                w.setEnabled(False)
        # 环境变量名一改，密钥提示要跟着更新
        if f.key.endswith(".api_key_env") and isinstance(w, QLineEdit):
            w.textChanged.connect(self._sync_api_key_hint)
        # 热词表：改文件路径或改内联表都要重算"实际生效多少条"
        if f.key == "asr.livetranslate.phrases_file" and isinstance(w, QLineEdit):
            w.textChanged.connect(self._sync_phrases_hint)
        elif (f.key == "asr.livetranslate.phrases"
              and isinstance(w, QPlainTextEdit)):
            w.textChanged.connect(self._sync_phrases_hint)

    def _device_combo(self, cur: str) -> QComboBox:
        combo = QComboBox()
        combo.addItem("自动（默认扬声器）", "")
        try:
            from ..audio.device import list_loopback_devices

            for dev in list_loopback_devices():
                combo.addItem(dev.name, dev.name)
        except Exception as e:      # pragma: no cover - 依赖本机声卡
            log.warning("枚举音频设备失败，仅保留自动选项", error=str(e))

        idx = combo.findData(cur)
        if idx < 0 and cur:
            combo.addItem(cur, cur)
            idx = combo.findData(cur)
        combo.setCurrentIndex(max(0, idx))
        return combo

    def _color_row(self, f: Field, value: str) -> tuple[QWidget, QLineEdit]:
        """返回 (容器, 真正的输入框)。注册进 _fields 的是后者。"""
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        edit = QLineEdit(value)
        edit.setStyleSheet("font-family:Consolas,monospace;")
        lay.addWidget(edit, 1)

        btn = QPushButton("…")
        btn.setFixedWidth(34)

        def pick(_=False, e=edit) -> None:
            cur = QColor(e.text())
            c = QColorDialog.getColor(
                cur if cur.isValid() else QColor("#FFFFFF"),
                self, "选择颜色", QColorDialog.ShowAlphaChannel,
            )
            if not c.isValid():
                return
            # 有透明度时写成 #AARRGGBB，否则 #RRGGBB
            e.setText(c.name(QColor.HexArgb) if c.alpha() < 255 else c.name())

        btn.clicked.connect(pick)
        lay.addWidget(btn)
        return holder, edit

    # ---------------- 注册 ----------------
    def _reg(self, key: str, widget: QWidget) -> None:
        self._fields[key] = widget

    def _read(self, key: str) -> Any:
        w = self._fields[key]
        spec = self._specs.get(key)

        if isinstance(w, QComboBox):
            if w.isEditable():
                return w.currentText().strip()
            data = w.currentData()
            return data if data is not None else w.currentText()
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QSpinBox):
            return w.value()
        if isinstance(w, QDoubleSpinBox):
            return round(w.value(), 6)
        if isinstance(w, QPlainTextEdit):
            if spec is not None and spec.kind == "dict":
                return parse_phrases(w.toPlainText())
            if spec is not None and spec.kind == "list":
                return parse_list(w.toPlainText())
            return w.toPlainText()
        if isinstance(w, QLineEdit):
            text = w.text().strip()
            if spec is not None and spec.nullable:
                if text == "":
                    return None
                # 框里显示的是 fallback（配置为 null 时的实际生效值）→
                # 用户没动过它，仍然算"未设置"，不产生配置漂移
                if (spec.fallback and text == spec.fallback
                        and self._cfg_get(key) is None):
                    return None
            return text
        return None

    def _snapshot(self) -> None:
        self._orig = {k: self._read(k) for k in self._fields}
        self._prefill_api_key()
        self._sync_api_key_hint()

    # ---------------- API Key ----------------
    def _api_key_env_name(self) -> str:
        """当前后端对应的 API Key 环境变量名。

        后端已固定 livetranslate，所以这里直接读它那一项。
        """
        if "asr.livetranslate.api_key_env" not in self._fields:
            return ""
        return str(self._read("asr.livetranslate.api_key_env") or "").strip()

    def _prefill_api_key(self) -> None:
        """把**当前已配置的密钥**直接填进输入框（明文可见）。

        用户要求「apikey 直接显示在设置里面」—— 打开设置就能看到用的是什么
        密钥，不用猜、也不用去翻 `.env`。

        ⚠️ 密钥会明文显示在窗口里，录屏 / 直播时注意遮挡。
        """
        if self._api_key_edit is None:
            return
        env_name = self._api_key_env_name()
        self._api_key_orig = os.environ.get(env_name, "") if env_name else ""
        self._api_key_edit.setText(self._api_key_orig)

    def _sync_api_key_hint(self) -> None:
        if self._api_key_hint is None or self._api_key_edit is None:
            return

        env_name = self._api_key_env_name()
        if not env_name:
            self._api_key_hint.setText(
                "<b style='color:#FBBF24'>请先填写「Key 环境变量名」</b>"
                "（默认 DASHSCOPE_API_KEY）"
            )
            self._api_key_edit.setEnabled(False)
            return

        self._api_key_edit.setEnabled(True)
        if self._api_key_orig:
            self._api_key_hint.setText(
                f"已配置（环境变量 <code>{env_name}</code>）。"
                f"改完点「保存并应用」写回项目根的 .env；"
                f"留空 = 不修改（要删密钥请直接编辑 .env）。"
            )
        else:
            self._api_key_hint.setText(
                f"<b style='color:#FBBF24'>尚未配置</b>"
                f"（环境变量 <code>{env_name}</code>）。"
                f"粘贴密钥后保存即写入项目根的 .env。"
            )

    # ================================================== 热词表提示
    def _resolve_phrases_path(self, raw: str):
        """把热词表文件的相对路径按 config.yaml 所在目录解析成 Path。"""
        from pathlib import Path

        p = Path(raw).expanduser()
        if not p.is_absolute():
            base = (self.cfg.source_path.parent
                    if self.cfg.source_path else Path.cwd())
            p = base / p
        try:
            return p.resolve()
        except OSError:      # pragma: no cover
            return p

    def _sync_phrases_hint(self) -> None:
        """刷新"外部文件 + 内联 → 实际生效多少条"的提示。

        直接读**控件当前值**（而不是已加载的配置），
        这样用户刚敲完路径/刚改完内联表就能看到结果。
        """
        if self._phrases_hint is None:
            return

        from ..config import MAX_PHRASES, read_phrases_file

        te = self._fields.get("asr.livetranslate.phrases")
        inline = parse_phrases(te.toPlainText()) if isinstance(
            te, QPlainTextEdit) else {}
        le = self._fields.get("asr.livetranslate.phrases_file")
        raw = le.text().strip() if isinstance(le, QLineEdit) else ""

        if not raw:
            self._phrases_hint.setText(
                f"当前生效 <b>{len(inline)}</b> 条（全部来自本框内联表）。"
                f"词表很大时建议写进外部文件，用上面的「热词表文件」引用。"
            )
            return

        path = self._resolve_phrases_path(raw)
        try:
            external = read_phrases_file(path)
        except Exception as e:      # noqa: BLE001 - ConfigError 及其他 IO 错
            self._phrases_hint.setText(
                f"<b style='color:#FBBF24'>热词表文件读取失败</b>：{e}"
            )
            return

        merged = dict(external)
        merged.update(inline)
        over = len(merged) > MAX_PHRASES
        warn = ("<b style='color:#F87171'>已超过上限！</b>"
                if over else "")
        self._phrases_hint.setText(
            f"外部文件已加载 <b>{len(external)}</b> 条，"
            f"本框内联 <b>{len(inline)}</b> 条（同名时内联优先）→ "
            f"合并后共 <b>{len(merged)}</b> 条 / 上限 {MAX_PHRASES}。{warn}"
        )

    # ================================================== 校验
    def _active_keys(self) -> list[str]:
        """需要校验的键。本界面所有字段都生效。"""
        return list(self._always_keys)

    def _validate(self) -> list[str]:
        errs: list[str] = []
        for key in self._active_keys():
            spec = self._specs.get(key)
            if spec is None or not spec.validator:
                continue
            value = self._read(key)
            if spec.nullable and value is None:
                continue
            text = "" if value is None else str(value).strip()

            if spec.validator == "nonempty" and not text:
                errs.append(f"「{spec.label}」不能为空")
            elif spec.validator == "http" and not text.startswith(
                    ("http://", "https://")):
                errs.append(f"「{spec.label}」必须以 http:// 或 https:// 开头")
            elif spec.validator == "ws" and text and not text.startswith(
                    ("ws://", "wss://")):
                errs.append(f"「{spec.label}」必须以 ws:// 或 wss:// 开头")
            elif spec.validator == "env" and not _ENV_NAME_RE.match(text):
                errs.append(
                    f"「{spec.label}」不是合法的环境变量名"
                    f"（只能含字母、数字、下划线，且不能以数字开头）"
                )
            elif spec.validator == "file" and text:
                # 文件不存在的话程序下次启动会 fail fast 起不来，
                # 不如在这里就拦住（留空是合法的 = 只用内联表）
                p = self._resolve_phrases_path(text)
                if not p.is_file():
                    errs.append(
                        f"「{spec.label}」指向的文件不存在：{p}"
                        f"（相对路径按 config.yaml 所在目录解析）"
                    )
        return errs

    # ================================================== 结果
    def accept(self) -> None:
        errs = self._validate()
        if errs:
            if self._error_label is not None:
                self._error_label.setText("无法保存：\n  · " + "\n  · ".join(errs))
                self._error_label.setVisible(True)
            log.warning("设置校验未通过", errors=len(errs))
            return
        if self._error_label is not None:
            self._error_label.setVisible(False)
        self._collect()
        super().accept()

    def _collect(self) -> None:
        yaml_updates: dict[str, Any] = {}
        for key, widget in self._fields.items():
            if key in self._readonly:
                continue
            value = self._read(key)
            if value != self._orig.get(key):
                yaml_updates[key] = value

        env_updates: dict[str, str] = {}
        if self._api_key_edit is not None:
            typed = self._api_key_edit.text().strip()
            env_name = self._api_key_env_name()
            # 只在**真的改过**时才写 .env：
            #   · 框里预填的就是当前值 → 不算改动，避免每次保存都重写 .env
            #   · 留空 → 保持"不修改"语义，不会误删密钥
            if typed and env_name and typed != self._api_key_orig:
                env_updates[env_name] = typed

        self._yaml_updates = yaml_updates
        self._env_updates = env_updates

    @property
    def yaml_updates(self) -> dict[str, Any]:
        return dict(self._yaml_updates)

    @property
    def env_updates(self) -> dict[str, str]:
        return dict(self._env_updates)

    @property
    def changed_keys(self) -> set[str]:
        return set(self._yaml_updates)

    @property
    def restart_required(self) -> bool:
        """是否有改动无法热生效。"""
        if self._env_updates:
            return True
        return any(
            not k.startswith(HOT_PREFIXES) for k in self._yaml_updates
        )

    @property
    def hot_keys(self) -> set[str]:
        return {
            k for k in self._yaml_updates if k.startswith(HOT_PREFIXES)
        }
