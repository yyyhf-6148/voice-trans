# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：**单文件、无控制台、不含任何密钥**。

跑法（推荐用 build_exe.bat）::

    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean trans.spec

产物
----
    dist/EVE-Voice-Translator.exe      单文件，双击即用

    首次运行时会在 **exe 同目录**自动铺出（之后可直接编辑）::

        config.yaml     配置（设置界面写回这里）
        terms.yaml      热词表 629 条（可自行增删术语）
        .env            API Key（用户在设置窗口填，不进包）

三条关键约定
------------
**1. `.env` 绝不打包** —— 里面是 API Key。
    用户第一次运行 exe 时在设置窗口里粘贴 Key，由
    `config_writer.apply_env_updates()` 写到 **exe 同目录**的 `.env`。
    下面的 `_assert_no_env_bundled()` 会在打包前直接拦住误加。

**2. `config.yaml` + `terms.yaml` 作为"内置默认文件"打进包**，但**不**从包内直接读 ——
    首次运行时 `src.config.ensure_user_files()` 会把它们复制到 exe 旁边，
    之后程序读写的都是 exe 旁边那份（用户可编辑、设置界面可写回）。
    原因：onefile 的解包目录（`sys._MEIPASS`）进程退出即删，
    写进去的配置会丢。
    ⚠️ `terms.yaml` 必须跟 `config.yaml` 一起铺出去：config.yaml 里
    `asr.livetranslate.phrases_file: terms.yaml` 是**相对路径**（按 config.yaml
    所在目录解析），少了它首启会因"热词表文件不存在"直接 fail fast。

**3. 排除本地 ASR 那一坨重依赖** ——
    本项目固定 `asr.provider = livetranslate`（纯云端，音频直入→中文文本出），
    `torch` / `faster-whisper` / `ctranslate2` / `transformers` 运行期
    一次都不会 import。不排除的话体积会从 ~150MB 涨到 3GB+。
"""
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve()


# =========================================================
#  安全护栏：把密钥挡在包外
# =========================================================
def _assert_no_env_bundled() -> None:
    """禁止把 `.env` / `api_keys.json` 之类塞进 datas。"""
    forbidden = {".env", ".env.local", "api_keys.json", "secrets.yaml"}
    for src, _dst in _DATAS:
        name = Path(src).name
        if name in forbidden:
            raise SystemExit(
                f"[PACKAGING ERROR] 拒绝打包密钥文件: {src}\n"
                f"                  API Key 必须由用户在设置窗口里填，"
                f"不能进 exe。"
            )


# 运行期真正需要的非 Python 资源
_DATAS = [
    # 默认配置 + 默认热词表（首启复制到 exe 旁边）。**不含 .env**
    # terms.yaml 由 config.yaml 的 asr.livetranslate.phrases_file 引用，
    # 必须一起铺出去，否则首启会因为"热词表文件不存在"直接 fail fast。
    (str(ROOT / "config.yaml"), "."),
    (str(ROOT / "terms.yaml"), "."),
]

_assert_no_env_bundled()

datas = _DATAS

# ---- 图标（没有也不影响打包）----
_ICON = ROOT / "assets" / "app.ico"

# ---- 明确要 import、但静态分析容易漏的模块 ----
hiddenimports = [
    "soundcard.mediafoundation",     # Windows 后端，按平台条件导入
    "keyboard._winkeyboard",         # 同上
    "dashscope.audio.qwen_omni",
    "dashscope.audio.qwen_omni.omni_realtime",
    "websocket",
    # 进程级 loopback 是函数内延迟导入（默认采集后端），显式声明防漏打包
    "src.audio.process_loopback",
]

# ---- 运行期绝不会被 import 的重型依赖 ----
excludes = [
    # 本地 ASR / 级联翻译（本项目固定走云端 livetranslate）
    "torch", "torchaudio", "torchvision", "torchgen",
    "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
    "transformers", "sentencepiece", "sacremoses", "safetensors",
    "numba", "llvmlite", "onnxruntime", "openvino", "triton",
    # 数据分析 / 交互式环境
    "matplotlib", "pandas", "IPython", "ipykernel", "jupyter",
    "jupyter_core", "notebook", "nbformat",
    # 测试与文档工具
    "pytest", "_pytest", "unittest", "doctest", "pydoc", "pdb",
    # 本项目用不到的 GUI 后端
    "tkinter", "_tkinter", "PyQt5", "PyQt6", "wx",
    # PySide6 里没用到的模块（体积大头是 WebEngine）
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtQml", "PySide6.QtQmlModels",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtSensors", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
    "PySide6.QtWebSockets", "PySide6.QtNetworkAuth", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtUiTools", "PySide6.QtSvgWidgets",
    "PySide6.QtOpenGLWidgets", "PySide6.QtConcurrent", "PySide6.QtDBus",
    "PySide6.QtPrintSupport",
]


a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="EVE-Voice-Translator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX 压缩常被杀软误报，关掉
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_ICON) if _ICON.exists() else None,
)

if not _ICON.exists():
    print(
        "[WARN] 未找到 assets/app.ico，将使用默认图标。"
        "生成方法: python -m src.tools.make_app_icon",
        file=sys.stderr,
    )
