"""P4 单元测试：进程级 loopback（纯 ctypes WASAPI 实现）。

覆盖范围：
  1. 结构体内存布局（pack=1 / PROPVARIANT 24B / PROCESSENTRY32W 568B）
  2. GUID 解析与 IAgileObject 放行（坑 1 回归防护）
  3. 系统支持性判断与 MTA 套间初始化
  4. 进程查找（精确优先 / 子串兜底 / 排除自身）
  5. 进程存活判断
  6. 真机采集：打开-读取-关闭（含连续开关，坑 2 回归防护）

运行:
    .venv\\Scripts\\python.exe tests\\test_process_loopback.py
    .venv\\Scripts\\python.exe tests\\test_process_loopback.py --capture  # 含真机采集
    或 pytest tests/test_process_loopback.py
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audio import process_loopback as pl  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


# =========================================================
#  1. 结构体布局
# =========================================================
def test_struct_layout() -> None:
    print("\n--- 结构体布局 ---")

    # WAVEFORMATEX 必须 _pack_ = 1，否则 sizeof = 20，Initialize 会失败
    check("WAVEFORMATEX sizeof == 18 (pack=1)",
          ctypes.sizeof(pl.WAVEFORMATEX) == 18,
          f"got {ctypes.sizeof(pl.WAVEFORMATEX)}")

    # 两个字段都是 DWORD（4 字节）→ sizeof = 8
    check("PROCESS_LOOPBACK_PARAMS sizeof == 8",
          ctypes.sizeof(pl.AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS) == 8,
          f"got {ctypes.sizeof(pl.AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS)}")

    # ActivationType(4) + ProcessLoopbackParams(8) = 12
    # —— 这个 12 就是 PROPVARIANT blob 的 cbSize，务必一致
    check("ACTIVATION_PARAMS sizeof == 12",
          ctypes.sizeof(pl.AUDIOCLIENT_ACTIVATION_PARAMS) == 12,
          f"got {ctypes.sizeof(pl.AUDIOCLIENT_ACTIVATION_PARAMS)}")

    if ctypes.sizeof(ctypes.c_void_p) == 8:
        check("PROPVARIANT_BLOB sizeof == 24 (x64)",
              ctypes.sizeof(pl.PROPVARIANT_BLOB) == 24,
              f"got {ctypes.sizeof(pl.PROPVARIANT_BLOB)}")

    check("PROCESSENTRY32W sizeof == 568",
          ctypes.sizeof(pl._PROCESSENTRY32W) == 568,
          f"got {ctypes.sizeof(pl._PROCESSENTRY32W)}")

    # make_format：PCM
    f = pl.make_format(16000, 1, 16, False)
    check("PCM 格式 tag == 1", f.wFormatTag == pl.WAVE_FORMAT_PCM)
    check("PCM 采样率", f.nSamplesPerSec == 16000)
    check("PCM 声道", f.nChannels == 1)
    check("PCM 位深", f.wBitsPerSample == 16)
    check("PCM blockAlign == 2", f.nBlockAlign == 2)
    check("PCM avgBytes == 32000", f.nAvgBytesPerSec == 32000)
    check("PCM cbSize == 0", f.cbSize == 0)

    # make_format：float
    ff = pl.make_format(48000, 2, 32, True)
    check("float 格式 tag == 3", ff.wFormatTag == pl.WAVE_FORMAT_IEEE_FLOAT)
    check("float blockAlign == 8", ff.nBlockAlign == 8)
    check("float avgBytes == 384000", ff.nAvgBytesPerSec == 384000)


# =========================================================
#  2. GUID / IAgileObject
# =========================================================
def test_guid() -> None:
    print("\n--- GUID 与 IAgileObject ---")

    g = pl._guid("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    check("GUID Data1 解析", g.Data1 == 0x94EA2B94, f"got {g.Data1:#x}")
    check("GUID Data2 解析", g.Data2 == 0xE9CC, f"got {g.Data2:#x}")
    check("GUID Data3 解析", g.Data3 == 0x49E0, f"got {g.Data3:#x}")
    check("GUID Data4 首字节", g.Data4[0] == 0xC0, f"got {g.Data4[0]:#x}")
    check("GUID Data4 末字节", g.Data4[7] == 0x90, f"got {g.Data4[7]:#x}")
    check("GUID raw 长度 16", len(pl._raw(g)) == 16)

    # 无花括号 / 小写同样可解析
    g2 = pl._guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
    check("GUID 无花括号小写等价", pl._raw(g2) == pl._raw(g))

    # 坑 1 回归：IAgileObject 必须在放行列表里
    check("IAgileObject 在 _ACCEPTED_IIDS 中",
          pl._raw(pl.IID_IAgileObject) in pl._ACCEPTED_IIDS)
    check("IUnknown 在 _ACCEPTED_IIDS 中",
          pl._raw(pl.IID_IUnknown) in pl._ACCEPTED_IIDS)
    check("CompletionHandler 在 _ACCEPTED_IIDS 中",
          pl._raw(pl.IID_IActivateAudioInterfaceCompletionHandler)
          in pl._ACCEPTED_IIDS)
    check("_ACCEPTED_IIDS 共 3 项", len(pl._ACCEPTED_IIDS) == 3,
          f"got {len(pl._ACCEPTED_IIDS)}")


# =========================================================
#  3. 支持性 / MTA
# =========================================================
def test_support_and_mta() -> None:
    print("\n--- 支持性与套间 ---")

    supported = pl.is_supported()
    check("is_supported() 返回 bool", isinstance(supported, bool))
    if sys.platform == "win32":
        build = sys.getwindowsversion().build
        expect = build >= 19041
        check("is_supported() 与本机 build 一致", supported == expect,
              f"build={build} supported={supported}")

    # ensure_mta 在本机应成功（脚本主线程未被初始化成 STA）
    if sys.platform == "win32":
        check("ensure_mta() 成功", pl.ensure_mta() is True)
        # 幂等
        check("ensure_mta() 幂等", pl.ensure_mta() is True)


# =========================================================
#  4. 进程查找
# =========================================================
def test_find_process() -> None:
    print("\n--- 进程查找 ---")

    fake = [
        (100, "mumble.exe"),
        (200, "mumble-g15-helper.exe"),
        (300, "explorer.exe"),
    ]
    orig = pl.list_processes
    pl.list_processes = lambda: fake  # type: ignore[assignment]
    try:
        # 精确优先：必须命中 mumble.exe（100），不是子串命中的 helper
        check("精确匹配优先于子串",
              pl.find_process_id("mumble", exclude_pid=0) == 100,
              f"got {pl.find_process_id('mumble', exclude_pid=0)}")
        check("带 .exe 也精确命中",
              pl.find_process_id("mumble.exe", exclude_pid=0) == 100)
        check("大小写不敏感",
              pl.find_process_id("MUMBLE", exclude_pid=0) == 100)

        # 无精确命中时退回子串
        check("子串兜底命中 helper",
              pl.find_process_id("mumble-g15", exclude_pid=0) == 200)

        # 完全不存在
        check("不存在的进程返回 None",
              pl.find_process_id("no-such-proc-xyz", exclude_pid=0) is None)

        # 空名字
        check("空名字返回 None", pl.find_process_id("", exclude_pid=0) is None)
        check("纯空白名字返回 None",
              pl.find_process_id("   ", exclude_pid=0) is None)

        # 排除指定 PID：精确命中被排除后，退回子串兜底
        check("exclude 精确项后退回子串兜底",
              pl.find_process_id("mumble", exclude_pid=100) == 200,
              f"got {pl.find_process_id('mumble', exclude_pid=100)}")
        # 只有唯一匹配且被排除 → None
        check("唯一匹配被排除后返回 None",
              pl.find_process_id("explorer", exclude_pid=300) is None,
              f"got {pl.find_process_id('explorer', exclude_pid=300)}")
        check("不排除时可命中",
              pl.find_process_id("explorer", exclude_pid=0) == 300)
    finally:
        pl.list_processes = orig  # type: ignore[assignment]


# =========================================================
#  5. 进程存活
# =========================================================
def test_process_alive() -> None:
    print("\n--- 进程存活判断 ---")

    if sys.platform != "win32":
        check("非 Windows 跳过", True)
        return

    check("自身进程存活", pl.is_process_alive(os.getpid()) is True)
    check("PID=0 视为不存在", pl.is_process_alive(0) is False)
    check("负 PID 视为不存在", pl.is_process_alive(-1) is False)
    # Windows 上 PID 均为 4 的倍数，奇数必不存在
    check("奇数 PID 不存在", pl.is_process_alive(0x7FFFFFFF) is False)

    procs = pl.list_processes()
    check("list_processes 非空", len(procs) > 0, f"got {len(procs)}")
    check("自身 PID 在进程列表中",
          any(p == os.getpid() for p, _ in procs))
    check("列表元素形如 (int, str)",
          all(isinstance(p, int) and isinstance(n, str) for p, n in procs))


# =========================================================
#  6. 真机采集（可选）
# =========================================================
def test_capture_open_close() -> None:
    print("\n--- 真机采集（自捕获） ---")

    if not pl.is_supported():
        print("  [SKIP] 系统不支持进程级 loopback")
        return

    # 连续开关 3 轮 —— 专门防「AsyncOperation 未释放导致第二次
    # Initialize E_UNEXPECTED」的回归（见模块文档坑 2）
    for i in range(3):
        try:
            st = pl.ProcessLoopbackStream(
                os.getpid(), target_rate=16000, buffer_ms=50
            )
            st.open()
            opened = st.is_open
            alive = st.target_alive()
            # 读几次（自身无音频输出 → 静音或 None，都算正常）
            for _ in range(3):
                st.read(timeout=0.1)
            st.close()
            check(f"第 {i + 1} 轮打开-读取-关闭", opened and not st.is_open,
                  f"opened={opened} closed={not st.is_open}")
            check(f"第 {i + 1} 轮目标存活判断", alive is True)
        except Exception as e:  # noqa: BLE001
            check(f"第 {i + 1} 轮打开-读取-关闭", False, f"{type(e).__name__}: {e}")


def main() -> int:
    print("=" * 60)
    print("  P4 进程级 loopback 测试")
    print("=" * 60)

    test_struct_layout()
    test_guid()
    test_support_and_mta()
    test_find_process()
    test_process_alive()

    if "--capture" in sys.argv:
        test_capture_open_close()
    else:
        print("\n--- 真机采集 ---\n  [SKIP] 加 --capture 参数启用")

    print("\n" + "=" * 60)
    print(f"  结果: {PASS} 通过, {FAIL} 失败")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
