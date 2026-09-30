"""打包产物冒烟测试：在**干净目录**里真跑一遍 exe。

为什么需要它
------------
源码模式下的 518 项测试全部通过，**不能**证明打包产物能跑 ——
打包特有的坑源码测试一个都覆盖不到：

  1. exe 能不能在没有 `config.yaml` 的目录里自愈（铺出内置默认配置）
  2. `.env` 放在 **exe 旁边**能不能被读到（而不是写进会被删掉的解包目录）
  3. 冻结环境里整条依赖链能不能 import
     （soundcard / dashscope / websocket / numpy / scipy / PySide6）
  4. `sys.stdout is None`（windowed exe）时日志会不会崩
     —— 更要紧的是：日志文件**到底有没有被创建**。
     `logging_setup._CONFIGURED` 的幂等提前返回曾让文件 handler 永远装不上，
     源码模式有控制台输出所以看不出来，只有真跑 exe 才暴露（实测踩过）。

六个阶段
--------
  A. **无 Key**   → 应退出码 2，且 exe 旁边出现 config.yaml / logs/app.log
  B. **假 Key**   → 应退出码 4（连不上），但日志必须走到"启动失败"，
                    说明 soundcard / dashscope 都 import 成功了
  B2.**归档核对** → 直接读 exe 的 PyInstaller 归档，确认关键模块（尤其是
                    函数内延迟导入的 `src.audio.process_loopback`）真的进了 PYZ。
                    不依赖运行时环境，源码测试和 B 阶段都覆盖不到这一类漏打包。
  C. **真 Key**   → 需加 `--online`；应退出码 0，日志出现"服务已就绪"
                    （会真的连一次百炼，消耗极少量额度）
  D. **窗口模式** → 需加 `--online`；**不加 `--no-overlay`**，应退出码 0，
                    日志出现"进入事件循环" + "托盘图标已显示"。
                    ⚠️ A/B/C 全走 `--no-overlay`，那条路**根本不 import PySide6**，
                    所以"冻结环境里 Qt 平台插件能不能加载"完全没被覆盖 ——
                    而用户双击 exe 走的正是这条路。靠 `--duration` 让它能自动退出。
  E. **重启自身** → 不需要 Key；跑 `--selftest-restart`（窗口模式）。
                    父进程真的 `Popen` 一次自己然后**立刻退出**，子进程必须活下来。
                    ⚠️ 这条路径 A~D 全都不走，而用户实机报的崩溃恰恰出在这里：
                    onefile exe 里 `sys.executable` 是 exe 自己，重启出的进程若
                    **继承** `_MEIPASS` / `_PYI_PARENT_PROCESS_LEVEL`，就会复用父进程
                    即将被引导器删掉的 `_MEIxxxxxx` 目录 → 边删边 import →
                    `ModuleNotFoundError: No module named 'PySide6.QtWidgets'`。
  F. **设置窗口** → 不需要 Key；跑 `--selftest settings`（窗口模式）。
                    首启引导（"打开后直接填 Key 就能用"）必经 `SettingsDialog`，
                    它比悬浮窗/托盘多 import 一大票 QtWidgets 控件 ——
                    被 `trans.spec` 的 excludes 误伤就会在**第一次运行**崩。

用法::

    .venv\\Scripts\\python.exe -m src.tools.smoke_test_exe dist/EVE-Voice-Translator.exe
    .venv\\Scripts\\python.exe -m src.tools.smoke_test_exe dist/EVE-Voice-Translator.exe --online
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# 单次运行的墙钟上限（秒）。含 15s 连接超时 + 解包启动开销，留足余量。
_RUN_TIMEOUT_S = 150

# 会被清掉、避免"环境里本来就有 Key"污染阶段 A 的变量
_KEY_VARS = ("DASHSCOPE_API_KEY", "ASR_API_KEY", "DEEPSEEK_API_KEY")

_FAKE_KEY = "sk-fake-key-for-smoke-test-000000000000"

_passed = 0
_failed = 0


def chk(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}  {detail}")


def _read_env_values(env_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_path.is_file():
        return out
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and val:
            out[key] = val
    return out


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _KEY_VARS}
    if extra:
        env.update(extra)
    return env


def _decode_console(raw: bytes) -> str:
    """解 exe 控制台输出的字节。

    ⚠️ windowed exe 的 stdout 落到系统 ANSI 代码页（中文 Windows 是 **GBK**），
    不是 UTF-8 —— 直接按 utf-8 解会把中文变成乱码，让断言假失败（踩过）。
    先试 utf-8（纯 ASCII 或真 UTF-8 控制台），失败再退回 GBK。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def _run(exe: Path, cwd: Path, env: dict[str, str], seconds: int = 3,
         overlay: bool = False):
    """跑一次 exe，返回 `(returncode, 说明, 合并后的 stdout+stderr 文本)`。

    `overlay=False` 时加 `--no-overlay`（不创建 Qt 应用）。
    输出文本用来核对"控制台有没有被 `--- Logging error ---` 刷屏"——
    windowed exe 从终端/管道启动时 stdout 是 GBK，日志里的 `«…»` 编不出来，
    曾导致每条日志吐一段 traceback（已修，这里锁死回归）。
    """
    cmd = [str(exe)]
    if not overlay:
        cmd.append("--no-overlay")
    cmd += ["--duration", str(seconds)]
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), env=env, timeout=_RUN_TIMEOUT_S,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired:
        return None, f"超时（>{_RUN_TIMEOUT_S}s）", ""
    return p.returncode, "", _decode_console(p.stdout)


def _log_text(exe_dir: Path) -> str:
    log = exe_dir / "logs" / "app.log"
    if not log.is_file():
        return ""
    return log.read_text(encoding="utf-8", errors="replace")


# 必须确认在包内的模块。
#
# 为什么单独列出来：**函数内延迟导入**的模块静态分析最容易漏，
# 而漏了在阶段 B 也未必暴露 —— ImportError 只打在采集线程的日志里，
# 主流程照样按"连接失败"退出码 4 走完，看起来一切正常。
# 典型：`src.audio.process_loopback` 是默认采集后端的延迟导入。
_REQUIRED_MODULES = [
    "src.audio.process_loopback",   # 默认采集后端，延迟导入
    "src.audio.capture",
    "src.audio.device",
    "src.ui.overlay",
    "src.ui.settings_dialog",
    "src.ui.tray",
    "src.ui.hotkeys",
    "src.ui.subtitle_model",
    "src.pipeline",
    "src.config",
]


def _check_archive(exe: Path) -> None:
    """直接读 PyInstaller 归档，确认关键模块真的进了 PYZ。

    比"跑一遍看日志"确定得多：不依赖运行时环境（比如本机恰好没装 Mumble
    就会走降级路径，反而不触发目标模块），也不受日志级别影响。
    """
    try:
        import PyInstaller.archive.readers as _readers
    except ImportError:
        print("  [SKIP] 未安装 PyInstaller，跳过归档核对")
        return

    try:
        car = _readers.CArchiveReader(str(exe))
        pyz = car.open_embedded_archive("PYZ.pyz")
        raw = pyz.toc.keys() if isinstance(pyz.toc, dict) else pyz.toc
        names = {n for n in raw if isinstance(n, str)}
    except Exception as e:  # noqa: BLE001
        chk("能读取归档目录", False, f"{type(e).__name__}: {e}")
        return

    chk("能读取归档目录", True)
    missing = [m for m in _REQUIRED_MODULES if m not in names]
    chk(f"关键模块全部进包（{len(_REQUIRED_MODULES)} 个）",
        not missing, f"缺失: {missing}")
    chk("PYZ 模块数在合理范围（>1500）", len(names) > 1500,
        f"实际 {len(names)}")


def _wait_for_log(exe_dir: Path, needle: str, timeout: float = 45.0) -> str:
    """轮询日志，直到出现 `needle`（重启自检的子进程是异步起来的）。"""
    deadline = time.time() + timeout
    txt = ""
    while time.time() < deadline:
        txt = _log_text(exe_dir)
        if needle in txt:
            return txt
        time.sleep(0.4)
    return txt


def _meipass_of(log_text: str, marker: str) -> str:
    """从日志里抽出某个 SELFTEST 标记行的 `meipass=` 值。"""
    for line in log_text.splitlines():
        if marker in line:
            m = re.search(r"meipass=(.*?)(?:\s+parent_level=|\s*»|$)", line)
            return (m.group(1) if m else "").strip()
    return ""


def _run_restart_selftest(exe: Path, cwd: Path, env: dict[str, str]):
    """跑一次"重启自身"自检。父进程 `os._exit(0)` 立刻返回，子进程异步起来。"""
    cmd = [str(exe), "--selftest", "restart", "--duration", "30"]
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), env=env, timeout=90,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired:
        return None, "超时（>90s）"
    return p.returncode, ""


def _run_settings_selftest(exe: Path, cwd: Path, env: dict[str, str]):
    """跑一次"设置窗口能不能构造"自检（窗口模式，自动退出）。"""
    cmd = [str(exe), "--selftest", "settings", "--duration", "30"]
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), env=env, timeout=90,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired:
        return None, "超时（>90s）"
    return p.returncode, ""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    online = "--online" in args
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print(__doc__)
        return 2

    src_exe = Path(positional[0]).resolve()
    if not src_exe.is_file():
        print(f"[FAIL] 找不到产物: {src_exe}")
        return 1

    work = Path(tempfile.mkdtemp(prefix="eve_exe_smoke_"))
    exe = work / src_exe.name
    shutil.copy2(src_exe, exe)

    print("=" * 70)
    print("  打包产物冒烟测试")
    print("=" * 70)
    print(f"  产物 : {src_exe}")
    print(f"  沙箱 : {work}")

    try:
        # ================= 阶段 A：无 Key =================
        print("\n--- A. 无 API Key（应退出码 2 并自愈配置） ---")
        rc, note, out_a = _run(exe, work, _clean_env())
        chk("exe 能正常启动并退出", rc is not None, note)
        chk("无 Key 时退出码为 2", rc == 2, f"实际 {rc}")
        chk("首启自动铺出 config.yaml", (work / "config.yaml").is_file())
        chk("config.yaml 内容非空",
            (work / "config.yaml").stat().st_size > 1000
            if (work / "config.yaml").is_file() else False)
        # 热词表是**外部文件**（config.yaml 的 phrases_file 相对引用），
        # 必须跟 config.yaml 一起铺出来，否则首启 fail fast 起不来。
        chk("首启自动铺出 terms.yaml（外部热词表）",
            (work / "terms.yaml").is_file())
        chk("terms.yaml 内容非空（629 条词表）",
            (work / "terms.yaml").stat().st_size > 5000
            if (work / "terms.yaml").is_file() else False)
        chk("config.yaml 里引用了 terms.yaml",
            "phrases_file: terms.yaml"
            in (work / "config.yaml").read_text(encoding="utf-8", errors="replace")
            if (work / "config.yaml").is_file() else False)
        chk("terms.yaml 里能读到已知术语（gate: 星门）",
            "gate:" in (work / "terms.yaml").read_text(
                encoding="utf-8", errors="replace")
            if (work / "terms.yaml").is_file() else False)
        chk("写好了日志文件 logs/app.log", (work / "logs" / "app.log").is_file())
        log_a = _log_text(work)
        chk("日志里记录了「未配置 API Key」",
            "未配置 API Key" in log_a, f"日志片段: {log_a[-200:]!r}")
        # 打包产物的日志必须干净：windowed exe 没有控制台，
        # 一旦着色/extra 逻辑有 bug，日志文件就是唯一证据来源，不能脏。
        chk("日志文件里没有 ANSI 转义（着色没串进文件）",
            "\x1b" not in log_a, repr(log_a[:200]))
        # ⚠️ windowed exe 从终端/管道启动时 stdout 落到 GBK 代码页，
        #    日志里的 `«…»`（自定义字段包装）编不出来 → 每条日志吐一段
        #    `--- Logging error ---` traceback 把终端刷满（实测踩过，已修）。
        #    双击启动时 stdout 是 None，这条路径根本不走 —— 只有真跑 exe 能覆盖。
        chk("控制台没有被 `--- Logging error ---` 刷屏",
            "--- Logging error ---" not in out_a,
            f"片段: {out_a[:300]!r}")
        chk("控制台日志里中文正常（GBK 能编码）",
            "未配置 API Key" in out_a or "启动" in out_a,
            f"片段: {out_a[:300]!r}")

        # ================= 阶段 B：假 Key（验依赖链） =================
        print("\n--- B. 假 API Key（验冻结环境的依赖链） ---")
        (work / "logs" / "app.log").unlink(missing_ok=True)
        rc, note, out_b = _run(exe, work, _clean_env({"DASHSCOPE_API_KEY": _FAKE_KEY}))
        chk("假 Key 下 exe 不崩溃（能走完 import 链）", rc is not None, note)
        chk("假 Key 下退出码为 4（连接失败）", rc == 4, f"实际 {rc}")
        log_b = _log_text(work)
        chk("日志显示已进入流水线启动阶段",
            "启动失败" in log_b or "服务已就绪" in log_b,
            f"日志尾部: {log_b[-400:]!r}")
        # `warmup()` 内部会 `import soundcard` 并枚举一次扬声器，
        # 这句日志出现 = soundcard 在冻结环境里真的 import 成功了。
        chk("音频采集库可用（soundcard 在冻结环境 import 成功）",
            "音频子系统预热完成" in log_b,
            f"日志尾部: {log_b[-400:]!r}")
        chk("日志里没有重复收编的 extra_str",
            "extra_str=" not in log_b, repr(log_b[-300:]))
        # 阶段 B 会打出一堆带自定义字段的日志（`«…»`）—— 正是触发 GBK 编码
        # 崩溃的路径，这里再确认一次控制台是干净的。
        chk("控制台没有被 `--- Logging error ---` 刷屏（带 extra 字段的日志）",
            "--- Logging error ---" not in out_b, f"片段: {out_b[:300]!r}")

        # ================= 阶段 B2：归档内容核对 =================
        #
        # 阶段 B 只证明 soundcard / dashscope 能 import；
        # "某个模块有没有被打进包"是**另一类**失败，B 阶段未必暴露。
        # 这里直接读归档，不依赖运行时环境。
        print("\n--- B2. 归档内容核对（关键模块真的进包了吗） ---")
        _check_archive(exe)

        # ================= 阶段 C/D：真 Key（需 --online） =================
        if not online:
            print("\n--- C. 真 API Key ---")
            print("  [SKIP] 未加 --online，跳过在线连接（避免消耗额度）")
            print("\n--- D. 窗口模式（冻结环境的 Qt 平台插件） ---")
            print("  [SKIP] 需真 Key 才能走到窗口模式，加 --online 再跑")
        else:
            print("\n--- C. 真 API Key（真的连一次百炼） ---")
            secrets = _read_env_values(_ROOT / ".env")
            key = secrets.get("DASHSCOPE_API_KEY", "")
            if not key:
                chk("项目根 .env 里有 DASHSCOPE_API_KEY", False,
                    "跳过在线阶段")
            else:
                # 模拟"用户在设置窗口里填好 Key"的效果：写到 exe 旁边
                (work / ".env").write_text(
                    f"DASHSCOPE_API_KEY={key}\n", encoding="utf-8")
                (work / "logs" / "app.log").unlink(missing_ok=True)
                rc, note, _out_c = _run(exe, work, _clean_env(), seconds=6)
                chk("真 Key 下 exe 正常退出", rc is not None, note)
                log_c = _log_text(work)
                chk("exe 旁边的 .env 被读到（未提示缺少 Key）",
                    "未配置 API Key" not in log_c)
                chk("成功连上翻译服务",
                    "服务已就绪" in log_c, f"日志尾部: {log_c[-500:]!r}")
                chk("退出码为 0", rc == 0, f"实际 {rc}")

                # ---------- D. 窗口模式 ----------
                #
                # ⚠️ A/B/C 全走 `--no-overlay`，而那条路**根本不 import PySide6**
                #    —— "冻结环境里 Qt 平台插件能不能加载"（PyInstaller + PySide6
                #    的经典翻车点）完全没被验证过，偏偏用户双击 exe 走的就是这条路。
                #    这里用 `--duration` 让窗口模式也能无人值守地跑完。
                print("\n--- D. 窗口模式（验冻结环境的 Qt 平台插件） ---")
                (work / "logs" / "app.log").unlink(missing_ok=True)
                rc, note, _out_d = _run(exe, work, _clean_env(), seconds=5,
                                        overlay=True)
                chk("窗口模式能启动并自动退出", rc is not None, note)
                log_d = _log_text(work)
                chk("Qt 应用起来了（日志出现「进入事件循环」）",
                    "进入事件循环" in log_d, f"日志尾部: {log_d[-500:]!r}")
                chk("托盘创建成功",
                    "托盘图标已显示" in log_d, f"日志尾部: {log_d[-500:]!r}")
                chk("窗口模式限时运行生效", "窗口模式限时运行" in log_d,
                    f"日志尾部: {log_d[-300:]!r}")
                chk("能干净关闭（日志出现「正在关闭」）",
                    "正在关闭" in log_d, f"日志尾部: {log_d[-300:]!r}")
                chk("窗口模式退出码为 0", rc == 0, f"实际 {rc}")
                chk("窗口模式日志同样没有 ANSI 转义",
                    "\x1b" not in log_d, repr(log_d[-200:]))

        # ================= 阶段 E：重启自身 =================
        #
        # 这是**用户实机踩到的崩溃**所在的路径，而 A~D 一个都不走它。
        # 自检机制：父进程（引导器的子进程，环境里有 _MEIPASS）调 `_restart()`
        # 拉起一个新 exe，然后立刻 `os._exit(0)` —— 父进程一退出，最外层引导器
        # 就删掉 `_MEIxxxxxx`。若新进程继承并复用了同一个目录，它会边被删边
        # import，最终 `ModuleNotFoundError: No module named 'PySide6.QtWidgets'`。
        #
        # 不需要 API Key：自检分支在 `_api_key_missing()` 之前就返回了。
        print("\n--- E. 重启自身（onefile 引导器环境变量不能被子进程继承） ---")
        (work / "logs" / "app.log").unlink(missing_ok=True)
        rc, note = _run_restart_selftest(exe, work, _clean_env())
        chk("重启自检的父进程能正常退出", rc is not None, note)
        chk("父进程退出码为 0", rc == 0, f"实际 {rc}")

        log_e = _wait_for_log(work, "SELFTEST-RESTART-CHILD-OK", timeout=45.0)
        chk("父进程走的是真重启路径",
            "SELFTEST-RESTART-PARENT" in log_e,
            f"日志尾部: {log_e[-500:]!r}")
        chk("重启出来的子进程活着（PySide6.QtWidgets 可导入）",
            "SELFTEST-RESTART-CHILD-OK" in log_e,
            f"日志尾部: {log_e[-800:]!r}")
        chk("子进程里悬浮窗/托盘都建起来了",
            "SELFTEST-RESTART-CHILD-OK" in log_e
            and log_e.count("托盘图标已显示") >= 2,
            f"托盘日志出现 {log_e.count('托盘图标已显示')} 次")

        mei_parent = _meipass_of(log_e, "SELFTEST-RESTART-PARENT")
        mei_child = _meipass_of(log_e, "SELFTEST-RESTART-CHILD-OK")
        chk("父子进程用的是各自的 _MEI 目录（没有复用父进程的）",
            mei_parent != mei_child,
            f"父={mei_parent!r} 子={mei_child!r}")
        chk("子进程重新解压出了自己的 _MEI 目录",
            mei_child != "" and "MEI" in mei_child.upper(),
            f"子进程 _MEIPASS={mei_child!r}")
        chk("重启路径的日志也没有 ANSI 转义",
            "\x1b" not in log_e, repr(log_e[-200:]))

        # ================= 阶段 F：设置窗口 =================
        #
        # 首启引导（"打开后直接填 API Key 就能用"）必经 `SettingsDialog`。
        # 它比悬浮窗/托盘多 import 一大票 QtWidgets 控件，一旦被 trans.spec 的
        # excludes 误伤（或漏了 hiddenimport），用户会在**第一次运行**就崩 ——
        # 而这条路 A~E 一个都不走。
        print("\n--- F. 设置窗口（首启引导的必经之路） ---")
        (work / "logs" / "app.log").unlink(missing_ok=True)
        rc, note = _run_settings_selftest(exe, work, _clean_env())
        chk("设置窗口自检能正常退出", rc is not None, note)
        chk("设置窗口退出码为 0", rc == 0, f"实际 {rc}")
        log_f = _log_text(work)
        chk("SettingsDialog 在冻结环境里构造成功",
            "SELFTEST-SETTINGS-OK" in log_f, f"日志尾部: {log_f[-500:]!r}")
        chk("5 个分页都在（接口与密钥/音频/字幕/快捷键与托盘/高级）",
            all(p in log_f for p in ("接口与密钥", "音频", "字幕",
                                     "快捷键与托盘", "高级")),
            f"日志尾部: {log_f[-500:]!r}")
        chk("设置窗口自检没有抛异常",
            "Traceback" not in log_f and "未捕获异常" not in log_f,
            f"日志尾部: {log_f[-500:]!r}")

        print()
        print("=" * 70)
        if _failed:
            print(f"  结果: {_passed} 通过, {_failed} 失败")
        else:
            print(f"  结果: {_passed} 通过, 0 失败 —— 产物可用")
        print("=" * 70)
        if _failed:
            print(f"\n  沙箱目录保留以便排查: {work}")
            return 1
    finally:
        if not _failed:
            shutil.rmtree(work, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
