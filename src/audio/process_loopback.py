"""Windows 进程级 loopback 采集（WASAPI ApplicationLoopback）。

只抓**指定进程**（含其子进程树）的音频输出，而不是整个扬声器的混音。
这样 Mumble 的舰队语音能进来，游戏音效 / 音乐 / 浏览器不会被翻译。

为什么不用第三方库
------------------
`soundcard` / `pyaudiowpatch` 只支持**设备级** loopback，没有进程级，
也无法表达 `AUDIOCLIENT_ACTIVATION_PARAMS`。进程级 loopback 必须走
`ActivateAudioInterfaceAsync` + 虚拟设备 `VAD\\Process_Loopback`，
所以这里用纯 ctypes 直接调 WASAPI —— **不引入任何新的第三方依赖**
（onefile exe 打包时这点很重要，也省掉 comtypes 的隐藏导入）。

三个必须遵守的坑（都是实测踩出来的，改这个文件前务必读完）
----------------------------------------------------------
**1. 完成回调必须同时实现 `IAgileObject`**
`QueryInterface` 里只放行 `IActivateAudioInterfaceCompletionHandler`
是不够的，`ActivateAudioInterfaceAsync` 会直接返回
`E_ILLEGAL_METHOD_CALL (0x8000000E)`，而且**不给任何解释**。
（已排除：与 STA/MTA 无关 —— MTA 下同样报这个错。）

**2. 三个 COM 对象都必须显式 Release**
`IAudioCaptureClient`、`IAudioClient`、`IActivateAudioInterfaceAsyncOperation`
一个都不能漏。引用计数不归零时音频引擎会认为上一条进程回环流还活着，
**第二次 `IAudioClient::Initialize` 必失败 `E_UNEXPECTED (0x8000FFFF)`**
（实测：只释放 AsyncOperation 仍然失败，补上另外两个才通过）。

⚠️ 纯 ctypes 没有 `comtypes` 那样的 `__del__` 兜底，**必须手动释放**；
反过来，若在 comtypes 下手动 Release 这些接口，会与 `__del__` 二次释放
→ 段错误。两种实现的正确做法正好相反，改实现方式时务必注意。
释放顺序：CaptureClient → AudioClient → AsyncOperation（先子后父）。

**3. 无效 PID 不报错，只是静音**
传一个不存在的 PID，激活与 `Initialize` 都会成功，流持续产出全零。
所以必须自己先确认进程存在（`find_process_id()`）。

其他实测结论
------------
- 可直接请求 16kHz / mono / 16bit PCM，正是 ASR 要的输入，**免重采样**
- `AUDCLNT_BUFFERFLAGS_SILENT` 不会被置位，静音以近零采样送达
  → 静音判定只能靠 RMS 门限（`audio.silence_rms_threshold`）
- `GetBufferSize()` 在该虚拟设备上返回**垃圾值**，不要用
- 目标进程退出后流不会报错，只是变静音 → 需自己轮询 PID 存活
- 缓冲时长 20ms ~ 200ms 都能用（见 `audio.process_buffer_ms`）
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time
from ctypes import POINTER, byref, c_int, c_void_p
from ctypes.wintypes import DWORD, LPCWSTR, WORD

import numpy as np

from ..errors import AudioCaptureError
from ..logging_setup import get_bound_logger
from .ring_buffer import resample

log = get_bound_logger(__name__)

# =========================================================
#  常量
# =========================================================
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1

VT_BLOB = 65
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3

AUDCLNT_BUFFERFLAGS_SILENT = 0x00000002

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

COINIT_MULTITHREADED = 0x00000000
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106
E_NOINTERFACE = -2147467262       # 0x80004002

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x00000002

# 进程级 loopback 从 Windows 10 Build 19041（2004）开始支持
MIN_WINDOWS_BUILD = 19041

# 兜底格式：按顺序尝试，第一个被接受的就用
_FORMAT_CANDIDATES: tuple[tuple[int, int, int, bool], ...] = (
    (16000, 1, 16, False),
    (16000, 1, 32, True),
    (48000, 1, 16, False),
    (48000, 2, 16, False),
    (48000, 2, 32, True),
)


def _hex(hr: int | None) -> str:
    return "n/a" if hr is None else f"0x{(hr or 0) & 0xFFFFFFFF:08X}"


# =========================================================
#  基础类型
# =========================================================
_HRESULT = ctypes.c_long
_ULONG = ctypes.c_ulong
_REFERENCE_TIME = ctypes.c_longlong
_HANDLE = c_void_p


class GUID(ctypes.Structure):
    """COM GUID 的内存布局（前三个字段小端，Data4 原序）。"""

    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    """解析标准 GUID 字符串。

    标准形式 `{8-4-4-4-12}`，split("-") 会得到 **5** 段
    （Data4 的 16 个 hex 被中间那个连字符拆成 4+12）。
    这里把后两段拼回去再逐字段填充。
    """
    parts = text.strip().strip("{}").split("-")
    if len(parts) == 5:
        parts = parts[:3] + [parts[3] + parts[4]]
    if len(parts) != 4:
        raise ValueError(f"非法 GUID 字符串: {text!r}")
    d1, d2, d3, d4 = parts
    if len(d4) != 16:
        raise ValueError(f"非法 GUID Data4（应为 16 个 hex）: {text!r}")
    g = GUID()
    g.Data1 = int(d1, 16)
    g.Data2 = int(d2, 16)
    g.Data3 = int(d3, 16)
    for i in range(8):
        g.Data4[i] = int(d4[i * 2 : i * 2 + 2], 16)
    return g


IID_IUnknown = _guid("00000000-0000-0000-C000-000000000046")
IID_IAudioClient = _guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = _guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317")
IID_IActivateAudioInterfaceCompletionHandler = _guid(
    "41D949AB-9862-444A-80F6-C261334DA5EB"
)
# 关键：不实现这个"敏捷对象"标记接口，激活直接失败（见模块文档坑 1）
IID_IAgileObject = _guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")


def _raw(g: GUID) -> bytes:
    return bytes(memoryview(g))


_ACCEPTED_IIDS = frozenset(
    {
        _raw(IID_IUnknown),
        _raw(IID_IActivateAudioInterfaceCompletionHandler),
        _raw(IID_IAgileObject),
    }
)


class WAVEFORMATEX(ctypes.Structure):
    """18 字节 —— **必须 pack=1**，否则 sizeof 变 20，Initialize 会失败。"""

    _pack_ = 1
    _fields_ = [
        ("wFormatTag", WORD),
        ("nChannels", WORD),
        ("nSamplesPerSec", DWORD),
        ("nAvgBytesPerSec", DWORD),
        ("nBlockAlign", WORD),
        ("wBitsPerSample", WORD),
        ("cbSize", WORD),
    ]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", DWORD),
        ("ProcessLoopbackMode", c_int),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", c_int),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class PROPVARIANT_BLOB(ctypes.Structure):
    """只用到 VT_BLOB 分支；x64 下 sizeof == 24。"""

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("cbSize", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
        ("pBlobData", c_void_p),
    ]


def make_format(rate: int, channels: int, bits: int, float_fmt: bool) -> WAVEFORMATEX:
    f = WAVEFORMATEX()
    f.wFormatTag = WAVE_FORMAT_IEEE_FLOAT if float_fmt else WAVE_FORMAT_PCM
    f.nChannels = channels
    f.nSamplesPerSec = rate
    f.wBitsPerSample = bits
    f.nBlockAlign = channels * bits // 8
    f.nAvgBytesPerSec = rate * f.nBlockAlign
    f.cbSize = 0
    return f


def _struct_bytes(obj: ctypes.Structure) -> bytes:
    return bytes(memoryview(obj))


# =========================================================
#  COM vtable（纯 ctypes，不用 comtypes）
# =========================================================
_QI_FN = ctypes.WINFUNCTYPE(_HRESULT, c_void_p, c_void_p, c_void_p)
_ADDREF_FN = ctypes.WINFUNCTYPE(_ULONG, c_void_p)
_RELEASE_FN = ctypes.WINFUNCTYPE(_ULONG, c_void_p)
_COMPLETED_FN = ctypes.WINFUNCTYPE(_HRESULT, c_void_p, c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI_FN),
        ("AddRef", _ADDREF_FN),
        ("Release", _RELEASE_FN),
        ("ActivateCompleted", _COMPLETED_FN),
    ]


class _AsyncOpVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI_FN),
        ("AddRef", _ADDREF_FN),
        ("Release", _RELEASE_FN),
        (
            "GetActivateResult",
            ctypes.WINFUNCTYPE(
                _HRESULT, c_void_p, POINTER(_HRESULT), POINTER(c_void_p)
            ),
        ),
    ]


class _AudioClientVtbl(ctypes.Structure):
    """IAudioClient，方法顺序必须与 mmdeviceapi.h 一致。"""

    _fields_ = [
        ("QueryInterface", _QI_FN),
        ("AddRef", _ADDREF_FN),
        ("Release", _RELEASE_FN),
        (
            "Initialize",
            ctypes.WINFUNCTYPE(
                _HRESULT,
                c_void_p,        # this
                c_int,           # AUDCLNT_SHAREMODE
                DWORD,           # StreamFlags
                _REFERENCE_TIME,  # hnsBufferDuration
                _REFERENCE_TIME,  # hnsPeriodicity
                c_void_p,        # const WAVEFORMATEX*
                c_void_p,        # LPCGUID AudioSessionGuid
            ),
        ),
        ("GetBufferSize", ctypes.WINFUNCTYPE(_HRESULT, c_void_p, POINTER(ctypes.c_uint32))),
        ("GetStreamLatency", ctypes.WINFUNCTYPE(_HRESULT, c_void_p, POINTER(ctypes.c_longlong))),
        ("GetCurrentPadding", ctypes.WINFUNCTYPE(_HRESULT, c_void_p, POINTER(ctypes.c_uint32))),
        (
            "IsFormatSupported",
            ctypes.WINFUNCTYPE(
                _HRESULT, c_void_p, c_int, c_void_p, POINTER(c_void_p)
            ),
        ),
        ("GetMixFormat", ctypes.WINFUNCTYPE(_HRESULT, c_void_p, POINTER(c_void_p))),
        (
            "GetDevicePeriod",
            ctypes.WINFUNCTYPE(
                _HRESULT, c_void_p, POINTER(ctypes.c_longlong), POINTER(ctypes.c_longlong)
            ),
        ),
        ("Start", ctypes.WINFUNCTYPE(_HRESULT, c_void_p)),
        ("Stop", ctypes.WINFUNCTYPE(_HRESULT, c_void_p)),
        ("Reset", ctypes.WINFUNCTYPE(_HRESULT, c_void_p)),
        ("SetEventHandle", ctypes.WINFUNCTYPE(_HRESULT, c_void_p, _HANDLE)),
        (
            "GetService",
            ctypes.WINFUNCTYPE(_HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p)),
        ),
    ]


class _AudioCaptureClientVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI_FN),
        ("AddRef", _ADDREF_FN),
        ("Release", _RELEASE_FN),
        (
            "GetBuffer",
            ctypes.WINFUNCTYPE(
                _HRESULT,
                c_void_p,
                POINTER(c_void_p),          # BYTE **ppData
                POINTER(ctypes.c_uint32),   # UINT32 *pNumFramesToRead
                POINTER(ctypes.c_uint32),   # DWORD *pdwFlags
                POINTER(ctypes.c_uint64),   # UINT64 *pu64DevicePosition
                POINTER(ctypes.c_uint64),   # UINT64 *pu64QPCPosition
            ),
        ),
        ("ReleaseBuffer", ctypes.WINFUNCTYPE(_HRESULT, c_void_p, ctypes.c_uint32)),
        (
            "GetNextPacketSize",
            ctypes.WINFUNCTYPE(_HRESULT, c_void_p, POINTER(ctypes.c_uint32)),
        ),
    ]


def _vtbl(ptr: int, vtbl_type: type[ctypes.Structure]):
    """取接口指针上的 vtable 结构体实例。

    ⚠️ ctypes 的 WINFUNCTYPE 字段**不会**自动绑定 `this`，
    调用时第一个参数必须显式传接口指针本身。
    """
    return ctypes.cast(ptr, POINTER(POINTER(vtbl_type))).contents.contents


# =========================================================
#  系统 API
# =========================================================
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateEventW.restype = _HANDLE
_k32.CreateEventW.argtypes = [c_void_p, c_int, c_int, LPCWSTR]
_k32.WaitForSingleObject.restype = DWORD
_k32.WaitForSingleObject.argtypes = [_HANDLE, DWORD]
_k32.CloseHandle.argtypes = [_HANDLE]
_k32.OpenProcess.restype = _HANDLE
_k32.OpenProcess.argtypes = [DWORD, c_int, DWORD]
_k32.GetExitCodeProcess.argtypes = [_HANDLE, POINTER(DWORD)]

_ole32 = ctypes.WinDLL("ole32")
_ole32.CoInitializeEx.argtypes = [c_void_p, DWORD]
_ole32.CoInitializeEx.restype = _HRESULT

_mmdevapi: ctypes.WinDLL | None = None


def _load_mmdevapi() -> ctypes.WinDLL:
    global _mmdevapi
    if _mmdevapi is None:
        dll = ctypes.WinDLL("Mmdevapi.dll")
        fn = dll.ActivateAudioInterfaceAsync
        fn.restype = _HRESULT
        fn.argtypes = [
            LPCWSTR,
            POINTER(GUID),
            c_void_p,               # PROPVARIANT*
            c_void_p,               # IActivateAudioInterfaceCompletionHandler*
            POINTER(c_void_p),      # IActivateAudioInterfaceAsyncOperation**
        ]
        _mmdevapi = dll
    return _mmdevapi


def is_supported() -> bool:
    """本机是否支持进程级 loopback（Windows 10 Build 19041+）。"""
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().build >= MIN_WINDOWS_BUILD
    except Exception:  # pragma: no cover - 非 Windows
        return False


# 每个线程只需 CoInitializeEx 一次
_mta_ready = threading.local()


def ensure_mta() -> bool:
    """确保**当前线程**处于 MTA 套间。

    ⚠️ `ActivateAudioInterfaceAsync` 必须在 MTA 线程上调用。
    采集线程是新建的，所以这里调用能生效；主线程若已被别的库
    初始化成 STA，会拿到 `RPC_E_CHANGED_MODE`（记警告但不致命）。
    """
    if getattr(_mta_ready, "done", False):
        return True
    hr = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if hr in (0, 1):  # S_OK / S_FALSE
        _mta_ready.done = True
        return True
    if hr == RPC_E_CHANGED_MODE:
        log.warning(
            "当前线程已是别的 COM 套间，进程级采集可能失败",
            hr=_hex(hr),
        )
        _mta_ready.done = True
        return False
    log.warning("CoInitializeEx 失败", hr=_hex(hr))
    return False


# =========================================================
#  进程查找
# =========================================================
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", POINTER(ctypes.c_ulong)),
        ("th32ModuleID", DWORD),
        ("cntThreads", DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


_k32.CreateToolhelp32Snapshot.restype = _HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [DWORD, DWORD]
_k32.Process32FirstW.argtypes = [_HANDLE, POINTER(_PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [_HANDLE, POINTER(_PROCESSENTRY32W)]


def list_processes() -> list[tuple[int, str]]:
    """列出 (pid, exe 文件名)。失败返回空列表。"""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _HANDLE(-1).value:
        return []
    out: list[tuple[int, str]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            ok = _k32.Process32NextW(snap, byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def find_process_id(name: str, exclude_pid: int | None = None) -> int | None:
    """按可执行文件名找 PID。

    匹配顺序（**精确优先**，这一点很重要）：
      1. 精确匹配 `<name>.exe`（大小写不敏感）
      2. 都不中时退回子串匹配，取第一个

    为什么必须精确优先：Mumble 会同时跑 `mumble.exe` 和
    `mumble-g15-helper.exe`（罗技 G 键辅助进程，不出声）。
    子串匹配会随机命中后者 —— 抓到一条永远静音的流。

    Args:
        name: 进程名，可带或不带 `.exe`
        exclude_pid: 要排除的 PID（默认排除自己，避免自捕获）

    Returns:
        PID；找不到返回 None
    """
    want = (name or "").strip().lower()
    if not want:
        return None
    if not want.endswith(".exe"):
        want += ".exe"
    skip = os.getpid() if exclude_pid is None else exclude_pid

    procs = [(p, n) for p, n in list_processes() if p != skip]
    if not procs:
        return None

    for pid, exe in procs:
        if exe.lower() == want:
            return pid

    stem = want[:-4]
    hits = [(p, n) for p, n in procs if stem in n.lower()]
    if hits:
        log.warning(
            "目标进程名未精确命中，退回子串匹配",
            want=want,
            matched=hits[0][1],
            others=len(hits) - 1,
        )
        return hits[0][0]
    return None


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not h:
        return False
    try:
        code = DWORD()
        if not _k32.GetExitCodeProcess(h, byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


# =========================================================
#  完成回调对象
# =========================================================
class _CompletionHandler(ctypes.Structure):
    """实现 `IActivateAudioInterfaceCompletionHandler` + `IAgileObject`。"""

    _fields_ = [("lpVtbl", POINTER(_HandlerVtbl))]

    def __init__(self) -> None:
        super().__init__()
        self.done = threading.Event()
        self.activate_hr: int | None = None
        self.iface_ptr: int = 0
        self.error: str = ""
        self._refs = 1
        self._vtbl = _HandlerVtbl(
            _QI_FN(self._query_interface),
            _ADDREF_FN(self._add_ref),
            _RELEASE_FN(self._release),
            _COMPLETED_FN(self._activate_completed),
        )
        self.lpVtbl = ctypes.pointer(self._vtbl)

    @property
    def ptr(self) -> int:
        return ctypes.cast(ctypes.pointer(self), c_void_p).value or 0

    # --- IUnknown ---
    def _query_interface(self, this, riid, ppv):
        raw = ctypes.string_at(riid, 16) if riid else b""
        if raw in _ACCEPTED_IIDS:
            ctypes.cast(ppv, POINTER(c_void_p))[0] = this
            self._refs += 1
            return 0
        ctypes.cast(ppv, POINTER(c_void_p))[0] = None
        return E_NOINTERFACE

    def _add_ref(self, this):
        self._refs += 1
        return self._refs

    def _release(self, this):
        self._refs -= 1
        # 故意不归零：对象由 Python 持有，音频引擎调用 Release 不应把它销毁
        return max(self._refs, 1)

    # --- IActivateAudioInterfaceCompletionHandler ---
    def _activate_completed(self, this, op_ptr):
        try:
            hr_result = _HRESULT(0)
            iface = c_void_p()
            _vtbl(op_ptr, _AsyncOpVtbl).GetActivateResult(
                op_ptr, byref(hr_result), byref(iface)
            )
            self.activate_hr = int(hr_result.value)
            self.iface_ptr = int(iface.value or 0)
        except Exception as e:  # pragma: no cover - 回调里不能抛
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.done.set()
        return 0


# =========================================================
#  进程级 loopback 流
# =========================================================
class ProcessLoopbackStream:
    """只采集某个进程（及其子进程）音频的 WASAPI 流。

    典型用法::

        stream = ProcessLoopbackStream(pid, target_rate=16000)
        stream.open()
        while running:
            block = stream.read(timeout=0.2)   # float32 mono @ target_rate
        stream.close()
    """

    def __init__(
        self,
        pid: int,
        target_rate: int = 16000,
        include_tree: bool = True,
        buffer_ms: int = 50,
    ) -> None:
        self.pid = int(pid)
        self.target_rate = int(target_rate)
        self.include_tree = bool(include_tree)
        self.buffer_ms = max(10, int(buffer_ms))

        self._client_ptr = 0
        self._capture_ptr = 0
        self._op_ptr = 0
        self._handler: _CompletionHandler | None = None
        self._event: int = 0
        self._holders: list = []
        self._opened = False

        # 实际协商到的格式
        self.src_rate = self.target_rate
        self.src_channels = 1
        self.src_bits = 16
        self.src_float = False
        self.format_desc = ""

    # ---------------- 生命周期 ----------------
    def open(self) -> None:
        if self._opened:
            raise AudioCaptureError("该流已经打开")
        if not is_supported():
            raise AudioCaptureError(
                "当前系统不支持进程级音频采集",
                detail=f"需要 Windows 10 Build {MIN_WINDOWS_BUILD}（2004）及以上",
            )
        ensure_mta()

        self._activate()
        try:
            self._initialize()
            self._start()
        except Exception:
            self.close()
            raise
        self._opened = True

        log.info(
            "进程级 loopback 已打开",
            pid=self.pid,
            format=self.format_desc,
            buffer_ms=self.buffer_ms,
        )

    def close(self) -> None:
        if self._client_ptr:
            try:
                _vtbl(self._client_ptr, _AudioClientVtbl).Stop(self._client_ptr)
            except Exception:
                pass
        if self._event:
            try:
                _k32.CloseHandle(self._event)
            except Exception:
                pass
            self._event = 0

        # ⚠️ 坑 2：三个 COM 对象都必须显式 Release，否则引用计数不归零，
        # 音频引擎会认为上一条进程回环流仍活着 —— 下一次 Initialize
        # 直接返回 E_UNEXPECTED (0x8000FFFF)。
        # 纯 ctypes 没有 comtypes 那样的 __del__ 兜底，只能手动释放。
        # 释放顺序：先子后父（CaptureClient → AudioClient → AsyncOperation）。
        if self._capture_ptr:
            try:
                _vtbl(self._capture_ptr, _AudioCaptureClientVtbl).Release(
                    self._capture_ptr
                )
            except Exception:
                pass
            self._capture_ptr = 0

        if self._client_ptr:
            try:
                _vtbl(self._client_ptr, _AudioClientVtbl).Release(self._client_ptr)
            except Exception:
                pass
            self._client_ptr = 0

        if self._op_ptr:
            try:
                _vtbl(self._op_ptr, _AsyncOpVtbl).Release(self._op_ptr)
            except Exception:
                pass
            self._op_ptr = 0

        self._holders.clear()
        if self._opened:
            log.info("进程级 loopback 已关闭", pid=self.pid)
        self._opened = False

    @property
    def is_open(self) -> bool:
        return self._opened

    def target_alive(self) -> bool:
        return is_process_alive(self.pid)

    def __enter__(self) -> ProcessLoopbackStream:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------- 内部：激活 ----------------
    def _activate(self) -> None:
        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = self.pid
        params.ProcessLoopbackParams.ProcessLoopbackMode = (
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
            if self.include_tree
            else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
        )
        raw = _struct_bytes(params)
        params_buf = ctypes.create_string_buffer(raw, len(raw))

        pv = PROPVARIANT_BLOB()
        pv.vt = VT_BLOB
        pv.cbSize = ctypes.sizeof(AUDIOCLIENT_ACTIVATION_PARAMS)
        pv.pBlobData = ctypes.cast(params_buf, c_void_p)

        handler = _CompletionHandler()
        op = c_void_p()
        hr = _load_mmdevapi().ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            byref(IID_IAudioClient),
            byref(pv),
            handler.ptr,
            byref(op),
        )
        if hr != 0:
            raise AudioCaptureError(
                f"激活进程级 loopback 失败: {_hex(hr)}",
                detail="请确认系统为 Windows 10 2004 及以上。"
                "0x8000000E 表示完成回调未实现 IAgileObject。",
            )

        if not handler.done.wait(timeout=5.0):
            raise AudioCaptureError("等待激活回调超时（5s）")
        if handler.error:
            raise AudioCaptureError(f"激活回调异常: {handler.error}")
        if handler.activate_hr != 0:
            raise AudioCaptureError(
                f"激活结果失败: {_hex(handler.activate_hr)}",
                detail="0x8889000E 通常是目标进程没有音频会话（该进程没在出声）",
            )
        if not handler.iface_ptr:
            raise AudioCaptureError("激活成功但接口指针为空")

        self._handler = handler
        self._op_ptr = int(op.value or 0)
        self._client_ptr = handler.iface_ptr
        # params_buf / pv 在激活完成前必须存活；激活是异步的，保险起见整段保留
        self._holders.extend([params_buf, pv, handler])

    # ---------------- 内部：初始化 ----------------
    def _initialize(self) -> None:
        flags = (
            AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
        )
        hns = self.buffer_ms * 10000  # 毫秒 -> 100ns 单位

        last_hr: int | None = None
        for rate, ch, bits, isf in _FORMAT_CANDIDATES:
            fmt = make_format(rate, ch, bits, isf)
            buf = ctypes.create_string_buffer(_struct_bytes(fmt), ctypes.sizeof(fmt))
            hr = _vtbl(self._client_ptr, _AudioClientVtbl).Initialize(
                self._client_ptr,
                AUDCLNT_SHAREMODE_SHARED,
                flags,
                hns,
                0,
                ctypes.cast(buf, c_void_p),
                None,
            )
            if hr == 0:
                self._holders.append(buf)
                self.src_rate, self.src_channels, self.src_bits, self.src_float = (
                    rate,
                    ch,
                    bits,
                    isf,
                )
                self.format_desc = (
                    f"{rate}Hz/{ch}ch/{bits}bit/{'float' if isf else 'pcm'}"
                )
                return
            last_hr = hr

        raise AudioCaptureError(
            f"所有候选音频格式都被拒绝，最后一次 {_hex(last_hr)}",
            detail="进程级 loopback 通常支持 16kHz/mono/16bit PCM。",
        )

    def _start(self) -> None:
        self._event = int(_k32.CreateEventW(None, 0, 0, None) or 0)
        if not self._event:
            raise AudioCaptureError("CreateEventW 失败")

        hr = _vtbl(self._client_ptr, _AudioClientVtbl).SetEventHandle(
            self._client_ptr, self._event
        )
        if hr != 0:
            raise AudioCaptureError(f"SetEventHandle 失败: {_hex(hr)}")

        raw_cap = c_void_p()
        hr = _vtbl(self._client_ptr, _AudioClientVtbl).GetService(
            self._client_ptr, byref(IID_IAudioCaptureClient), byref(raw_cap)
        )
        if hr != 0 or not raw_cap.value:
            raise AudioCaptureError(f"获取 IAudioCaptureClient 失败: {_hex(hr)}")
        self._capture_ptr = int(raw_cap.value)

        hr = _vtbl(self._client_ptr, _AudioClientVtbl).Start(self._client_ptr)
        if hr != 0:
            raise AudioCaptureError(f"启动采集流失败: {_hex(hr)}")

    # ---------------- 读取 ----------------
    def read(self, timeout: float = 0.2) -> np.ndarray | None:
        """读一批音频。返回 float32 mono @ target_rate；无数据返回 None。"""
        if not self._opened:
            return None
        rc = _k32.WaitForSingleObject(self._event, max(1, int(timeout * 1000)))
        if rc != WAIT_OBJECT_0:
            return None
        return self._drain()

    def _drain(self) -> np.ndarray | None:
        vtbl = _vtbl(self._capture_ptr, _AudioCaptureClientVtbl)
        chunks: list[np.ndarray] = []
        bytes_per_frame = self.src_channels * self.src_bits // 8

        while True:
            data = c_void_p()
            frames = ctypes.c_uint32()
            flags = ctypes.c_uint32()
            dev_pos = ctypes.c_uint64()
            qpc = ctypes.c_uint64()
            hr = vtbl.GetBuffer(
                self._capture_ptr,
                byref(data),
                byref(frames),
                byref(flags),
                byref(dev_pos),
                byref(qpc),
            )
            if hr != 0 or frames.value == 0:
                break

            n = int(frames.value)
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                chunks.append(np.zeros(n * self.src_channels, dtype=np.float32))
            else:
                raw = ctypes.string_at(data.value, n * bytes_per_frame)
                chunks.append(self._decode(raw))
            vtbl.ReleaseBuffer(self._capture_ptr, n)

            nxt = ctypes.c_uint32()
            if vtbl.GetNextPacketSize(self._capture_ptr, byref(nxt)) != 0:
                break
            if nxt.value == 0:
                break

        if not chunks:
            return None

        audio = np.concatenate(chunks)
        if self.src_channels > 1:
            audio = audio.reshape(-1, self.src_channels).mean(axis=1)
        if self.src_rate != self.target_rate:
            audio = resample(audio, self.src_rate, self.target_rate)
        return np.ascontiguousarray(audio, dtype=np.float32)

    def _decode(self, raw: bytes) -> np.ndarray:
        if self.src_float:
            return np.frombuffer(raw, dtype=np.float32).copy()
        if self.src_bits == 16:
            return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if self.src_bits == 32:
            return (
                np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
            )
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
