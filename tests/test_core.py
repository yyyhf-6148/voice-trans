"""P1 单元测试：环形缓冲、信号处理、配置加载、打包路径。

运行:
    .venv\\Scripts\\python.exe -m pytest tests/ -v
    或
    .venv\\Scripts\\python.exe tests\\test_core.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audio.ring_buffer import (  # noqa: E402
    RingBuffer,
    normalize_peak,
    peak,
    resample,
    rms,
    to_mono,
)
from src.config import load_config  # noqa: E402
from src.errors import ConfigError  # noqa: E402

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


def test_ring_buffer() -> None:
    print("\n--- RingBuffer ---")
    rb = RingBuffer(sample_rate=16000, max_seconds=1.0)

    # 写入与读取
    rb.write(np.ones(8000, dtype=np.float32))
    check("写入 8000 采样后长度正确", rb.total_samples == 8000, f"got {rb.total_samples}")
    check("时长计算正确", abs(rb.duration_s - 0.5) < 1e-6)

    rb.write(np.ones(8000, dtype=np.float32) * 2)
    check("追加后总长 16000", rb.total_samples == 16000, f"got {rb.total_samples}")

    # 容量限制
    rb.write(np.ones(8000, dtype=np.float32) * 3)
    check("超容量后仍为 16000", rb.total_samples == 16000, f"got {rb.total_samples}")
    tail = rb.read_last(8000)
    check("环形覆盖：尾部为最新数据", np.allclose(tail, 3.0), f"mean={tail.mean()}")

    # read_last 边界
    check("read_last(0) 返回空", rb.read_last(0).size == 0)
    check("read_last 超量返回全部", rb.read_last(99999).size == 16000)

    # snapshot
    snap = rb.snapshot(0.5)
    check("snapshot(0.5) 返回 8000", snap.size == 8000, f"got {snap.size}")

    # drain
    drained = rb.drain()
    check("drain 后缓冲清空", rb.total_samples == 0, f"got {rb.total_samples}")
    check("drain 返回全部数据", drained.size == 16000, f"got {drained.size}")

    # 多块拼接正确性
    rb2 = RingBuffer(sample_rate=1000, max_seconds=10)
    for i in range(5):
        rb2.write(np.full(100, float(i), dtype=np.float32))
    arr = rb2.snapshot()
    check("多块拼接长度正确", arr.size == 500, f"got {arr.size}")
    check("多块拼接顺序正确", arr[0] == 0.0 and arr[-1] == 4.0, f"{arr[0]}, {arr[-1]}")

    # 部分 drain
    rb3 = RingBuffer(sample_rate=1000, max_seconds=10)
    rb3.write(np.arange(500, dtype=np.float32))
    got = rb3.drain(0.2)
    check("部分 drain 取尾部", got.size == 200 and got[-1] == 499.0, f"size={got.size}, last={got[-1]}")
    check("部分 drain 后剩余正确", rb3.total_samples == 300, f"got {rb3.total_samples}")


def test_signal_utils() -> None:
    print("\n--- 信号处理 ---")

    # rms
    check("rms(空) == 0", rms(np.zeros(0, dtype=np.float32)) == 0.0)
    check("rms(全1) == 1", abs(rms(np.ones(100, dtype=np.float32)) - 1.0) < 1e-6)
    sine = np.sin(np.linspace(0, 2 * np.pi * 10, 16000)).astype(np.float32)
    check("rms(正弦) ≈ 0.707", abs(rms(sine) - 0.7071) < 0.01, f"got {rms(sine):.4f}")

    # peak
    check("peak 正确", abs(peak(np.array([0.1, -0.8, 0.5], dtype=np.float32)) - 0.8) < 1e-6)

    # normalize_peak
    # 注意：函数带 20x 增益上限，目的是"正常音量范围微调"，
    # 不是把极弱信号强行拉到满幅（否则会放爆底噪）。
    quiet = np.array([0.2, -0.4, 0.3], dtype=np.float32)
    norm = normalize_peak(quiet, target=0.9)
    check("normalize_peak 提升到目标", abs(peak(norm) - 0.9) < 1e-5, f"got {peak(norm)}")

    # 增益上限：0.001 峰值需要 900x，应被截断到 20x
    tiny = np.array([0.001, -0.001], dtype=np.float32)
    t_norm = normalize_peak(tiny, target=0.9)
    check("normalize_peak 遵守 20x 增益上限",
          abs(peak(t_norm) - 0.02) < 1e-6, f"got {peak(t_norm)}")

    # 静音不应被放大
    silence = np.zeros(100, dtype=np.float32)
    check("normalize_peak 对静音不放大", peak(normalize_peak(silence)) == 0.0)

    # to_mono
    stereo = np.array([[1.0, 3.0], [2.0, 4.0]], dtype=np.float32)
    mono = to_mono(stereo)
    check("to_mono 双声道平均", mono.size == 2 and abs(mono[0] - 2.0) < 1e-6, f"got {mono}")

    single = np.array([[1.0], [2.0]], dtype=np.float32)
    check("to_mono 单通道直通", to_mono(single).size == 2)

    already = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    check("to_mono 对 1D 直通", to_mono(already).size == 3)

    # resample
    src = np.sin(np.linspace(0, 2 * np.pi * 5, 48000)).astype(np.float32)
    out = resample(src, 48000, 16000)
    check("resample 48k->16k 长度正确", abs(out.size - 16000) <= 2, f"got {out.size}")
    check("resample 输出 float32", out.dtype == np.float32, f"got {out.dtype}")

    same = resample(src, 48000, 48000)
    check("resample 同采样率直通", same.size == src.size)

    empty = resample(np.zeros(0, dtype=np.float32), 48000, 16000)
    check("resample 空输入返回空", empty.size == 0)

    # 保真度检查：5Hz 正弦重采样后频谱仍应是 5Hz
    spec = np.abs(np.fft.rfft(out * np.hanning(out.size)))
    dominant = int(np.argmax(spec)) * 16000 / out.size
    check("resample 主频保持 ~5Hz", abs(dominant - 5) <= 1, f"got {dominant}Hz")


def test_config() -> None:
    print("\n--- 配置加载 ---")
    try:
        cfg = load_config()
        check("加载 config.yaml 成功", True)
        check("app 节存在", cfg.app is not None)
        check("target_sample_rate == 16000", cfg.app.target_sample_rate == 16000)
        check("audio.backend 合法", cfg.audio.backend in {"wasapi_loopback", "process"})
        check("asr.provider 合法",
              cfg.asr.provider in {"local", "cloud", "livetranslate"})
        check("asr.local.device 合法", cfg.asr.local.device in {"auto", "cuda", "cpu"})
        check("asr.cloud.max_retries >= 0", cfg.asr.cloud.max_retries >= 0)
        check("overlay.opacity 在范围内", 0.05 <= cfg.overlay.opacity <= 1.0)
        check("speaker_colors 非空", len(cfg.overlay.speaker_colors) > 0)
        check("initial_prompt 已配置", len(cfg.asr.initial_prompt) > 0)
        check("log_dir 可创建", cfg.log_dir.exists())
    except ConfigError as e:
        check("加载 config.yaml 成功", False, str(e))


def test_config_validation() -> None:
    print("\n--- 配置校验 ---")
    import tempfile

    import yaml

    # 非法 backend
    bad = {"audio": {"backend": "nonexistent"}}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        yaml.safe_dump(bad, f, allow_unicode=True)
        tmp = f.name
    try:
        load_config(tmp)
        check("非法 backend 被拦截", False, "未抛异常")
    except ConfigError:
        check("非法 backend 被拦截", True)
    finally:
        Path(tmp).unlink(missing_ok=True)

    # 未知字段
    bad2 = {"audio": {"backend": "wasapi_loopback", "typo_field": 1}}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        yaml.safe_dump(bad2, f, allow_unicode=True)
        tmp = f.name
    try:
        load_config(tmp)
        check("未知字段被拦截", False, "未抛异常")
    except ConfigError:
        check("未知字段被拦截", True)
    finally:
        Path(tmp).unlink(missing_ok=True)

    # 文件不存在
    try:
        load_config("__not_exist__.yaml")
        check("缺失文件被拦截", False, "未抛异常")
    except ConfigError:
        check("缺失文件被拦截", True)

    # ---- 进程级采集相关 ----
    from src.config import Config

    d = Config()
    check("默认 backend 为 process", d.audio.backend == "process",
          f"got {d.audio.backend}")
    check("默认 process_buffer_ms 为 50", d.audio.process_buffer_ms == 50,
          f"got {d.audio.process_buffer_ms}")

    # 非法 process_buffer_ms（过小）
    bad3 = {"audio": {"process_buffer_ms": 5}}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        yaml.safe_dump(bad3, f, allow_unicode=True)
        tmp = f.name
    try:
        load_config(tmp)
        check("process_buffer_ms 过小被拦截", False, "未抛异常")
    except ConfigError:
        check("process_buffer_ms 过小被拦截", True)
    finally:
        Path(tmp).unlink(missing_ok=True)

    # 空 process_name（backend=process 时非法）
    bad4 = {"audio": {"process_name": "   "}}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        yaml.safe_dump(bad4, f, allow_unicode=True)
        tmp = f.name
    try:
        load_config(tmp)
        check("空 process_name 被拦截", False, "未抛异常")
    except ConfigError:
        check("空 process_name 被拦截", True)
    finally:
        Path(tmp).unlink(missing_ok=True)

    # 但 backend=wasapi_loopback 时不该被它拦住（那个后端不看进程名）
    ok = {"audio": {"backend": "wasapi_loopback", "process_name": "   "}}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        yaml.safe_dump(ok, f, allow_unicode=True)
        tmp = f.name
    try:
        c = load_config(tmp)
        check("整路后端下空 process_name 不报错", c.audio.backend == "wasapi_loopback")
    except ConfigError as e:
        check("整路后端下空 process_name 不报错", False, str(e))
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_frozen_paths() -> None:
    """打包路径解析：根目录必须是 **exe 旁边**，绝不能是临时解包目录。

    背景：PyInstaller onefile 会把资源解到 `sys._MEIPASS`（%TEMP%\\_MEIxxxx），
    **进程退出即删**。若 `PROJECT_ROOT` 仍按 `__file__` 推导，
    用户填好的 `.env`（API Key）关一次程序就没了 —— 这是打包后最容易踩的坑。
    """
    import tempfile

    import src.config as C

    print("\n--- 打包路径解析 ---")
    proj = Path(__file__).resolve().parent.parent

    # ---------------- 源码模式 ----------------
    check("源码模式 is_frozen() 为 False", C.is_frozen() is False)
    check("源码模式 PROJECT_ROOT 指向项目根", C.PROJECT_ROOT == proj,
          f"实际 {C.PROJECT_ROOT}")
    check("源码模式没有内置资源目录", C._bundle_root() is None)
    check("源码模式 ensure_user_files() 什么都不做",
          C.ensure_user_files() == [])

    # ---------------- 模拟打包模式 ----------------
    exe_dir = Path(tempfile.mkdtemp(prefix="frozen_exe_"))
    mei_dir = Path(tempfile.mkdtemp(prefix="frozen_mei_"))
    fallback_root: Path | None = None
    (mei_dir / "config.yaml").write_text(
        "app:\n  log_level: INFO\noverlay:\n  opacity: 0.85\n", encoding="utf-8")
    # terms.yaml 由 config.yaml 的 `asr.livetranslate.phrases_file` 引用，
    # 打包时必须一起铺出去，否则首启会因"热词表文件不存在"直接 fail fast。
    (mei_dir / "terms.yaml").write_text(
        "gate: 星门\nloki: 洛基级\n", encoding="utf-8")

    saved = {
        "frozen": getattr(sys, "frozen", None),
        "meipass": getattr(sys, "_MEIPASS", None),
        "executable": sys.executable,
        "project_root": C.PROJECT_ROOT,
        "config_path": C.DEFAULT_CONFIG_PATH,
        "user_data_dir": C.user_data_dir,
    }
    try:
        sys.frozen = True
        sys._MEIPASS = str(mei_dir)
        sys.executable = str(exe_dir / "App.exe")
        C.PROJECT_ROOT = C._app_root()
        C.DEFAULT_CONFIG_PATH = C.PROJECT_ROOT / "config.yaml"

        check("打包模式 is_frozen() 为 True", C.is_frozen() is True)
        check("打包模式 PROJECT_ROOT 指向 exe 所在目录",
              C.PROJECT_ROOT == exe_dir, f"实际 {C.PROJECT_ROOT}")
        check("根目录**不是**临时解包目录（否则 .env 会丢）",
              C.PROJECT_ROOT != mei_dir)
        check("内置资源目录才是 _MEIPASS",
              C._bundle_root() == mei_dir, f"实际 {C._bundle_root()}")

        created = C.ensure_user_files()
        dst = exe_dir / "config.yaml"
        dst_terms = exe_dir / "terms.yaml"
        check("首启把内置 config.yaml 铺到 exe 旁边", dst.is_file(),
              f"实际 {created}")
        check("首启把内置 terms.yaml 铺到 exe 旁边（热词表外部引用）",
              dst_terms.is_file(), f"实际 {created}")
        check("返回值列出新建的两个文件",
              set(created) == {dst, dst_terms}, f"实际 {created}")

        dst.write_text("app:\n  log_level: DEBUG\n", encoding="utf-8")
        dst_terms.write_text("gate: 用户改过的星门\n", encoding="utf-8")
        again = C.ensure_user_files()
        check("已存在时绝不覆盖", again == [], f"实际 {again}")
        check("用户的改动没被默认值覆盖",
              "DEBUG" in dst.read_text(encoding="utf-8"))
        check("用户改过的热词表没被默认值覆盖",
              "用户改过的星门" in dst_terms.read_text(encoding="utf-8"))

        cfg = C.Config()
        check("打包模式 log_dir 落在 exe 旁边",
              cfg.log_dir == exe_dir / "logs", f"实际 {cfg.log_dir}")
        check("log_dir 目录真的被创建", (exe_dir / "logs").is_dir())

        # ---------------- exe 放在只读位置 -> 兜底 ----------------
        blocker = Path(tempfile.mkdtemp(prefix="frozen_block_")) / "file.txt"
        blocker.write_text("x", encoding="utf-8")
        fallback_root = Path(tempfile.mkdtemp(prefix="frozen_fb_"))
        C.PROJECT_ROOT = blocker / "sub"        # mkdir 必然失败
        C.user_data_dir = lambda: fallback_root
        cfg2 = C.Config()
        check("exe 目录不可写时 log_dir 兜底到用户数据目录",
              cfg2.log_dir == fallback_root / "logs", f"实际 {cfg2.log_dir}")
        check("兜底目录被创建", (fallback_root / "logs").is_dir())
    finally:
        if saved["frozen"] is None:
            sys.__dict__.pop("frozen", None)
        else:
            sys.frozen = saved["frozen"]
        if saved["meipass"] is None:
            sys.__dict__.pop("_MEIPASS", None)
        else:
            sys._MEIPASS = saved["meipass"]
        sys.executable = saved["executable"]
        C.PROJECT_ROOT = saved["project_root"]
        C.DEFAULT_CONFIG_PATH = saved["config_path"]
        C.user_data_dir = saved["user_data_dir"]
        for d in (exe_dir, mei_dir, fallback_root):
            if d is not None:
                shutil.rmtree(d, ignore_errors=True)


def test_child_process_env() -> None:
    """重启自身时传给子进程的环境变量：必须剥掉 PyInstaller 引导器变量。

    背景（用户实机踩到的崩溃）：
    onefile exe 里 `sys.executable` 就是 exe 自己。应用直接 `Popen(sys.executable)`
    时新进程会继承 `_MEIPASS` / `_PYI_PARENT_PROCESS_LEVEL` → 引导器判定
    "已解过包"，**复用父进程的 `_MEIxxxxxx` 目录**；父进程一退出，最外层引导器
    就把该目录删掉 → 新进程 import 到一半文件消失 →
    `ModuleNotFoundError: No module named 'PySide6.QtWidgets'`。
    """
    import os as _os

    import src.config as C

    print("\n--- 重启子进程环境变量 ---")

    saved_env = dict(_os.environ)
    try:
        # ---------------- 无引导器变量：原样返回 ----------------
        for k in list(_os.environ):
            if k.startswith(("_MEI", "_PYI_")):
                _os.environ.pop(k, None)
        _os.environ["DASHSCOPE_API_KEY"] = "sk-test-should-survive"
        _os.environ["MY_MEIPASS_NOTE"] = "不是引导器变量，必须保留"

        env0 = C.child_process_env()
        check("无引导器变量时 key 数量不变",
              set(env0) == set(_os.environ), f"{len(env0)} vs {len(_os.environ)}")
        check("普通业务变量被保留",
              env0.get("DASHSCOPE_API_KEY") == "sk-test-should-survive")
        check("只含 MEI 子串、不以 _MEI 开头的变量不误删",
              env0.get("MY_MEIPASS_NOTE") == "不是引导器变量，必须保留")

        # ---------------- 有引导器变量：全部剥掉 ----------------
        boot = {
            "_MEIPASS": r"C:\Users\x\AppData\Local\Temp\_MEI00004d282",
            "_MEIPASS2": "old-5.x-name",
            "_PYI_ARCHIVE_FILE": r"D:\App.exe",
            "_PYI_PARENT_PROCESS_LEVEL": "1",
            "_PYI_SPLASH_IPC": "0",
            "_PYI_APPLICATION_HOME_DIR": r"C:\App",
        }
        _os.environ.update(boot)

        env1 = C.child_process_env()
        leaked = sorted(k for k in env1 if k.startswith(("_MEI", "_PYI_")))
        check("所有 _MEI* / _PYI_* 引导器变量都被剥掉", leaked == [], f"残留 {leaked}")
        check("_PYI_PARENT_PROCESS_LEVEL 不在子进程环境里",
              "_PYI_PARENT_PROCESS_LEVEL" not in env1)
        check("_MEIPASS 不在子进程环境里（关键：否则复用将被删的目录）",
              "_MEIPASS" not in env1)
        check("剥变量后业务变量仍在",
              env1.get("DASHSCOPE_API_KEY") == "sk-test-should-survive")

        # ---------------- 绝不改动当前进程的 os.environ ----------------
        check("函数不修改 os.environ（只复制）",
              "_MEIPASS" in _os.environ and "DASHSCOPE_API_KEY" in _os.environ)
        check("返回值是独立副本（改动它不影响 os.environ）",
              env1 is not _os.environ)
    except Exception as e:                       # noqa: BLE001
        check("重启子进程环境测试未抛异常", False, f"{type(e).__name__}: {e}")
    finally:
        _os.environ.clear()
        _os.environ.update(saved_env)


def test_logging_without_console() -> None:
    """打包成 windowed exe 后 `sys.stdout` / `sys.stderr` 都是 None。

    此时 `logging.StreamHandler(None)` 会把 stream 设成 None，
    第一条日志就 `AttributeError: 'NoneType' object has no attribute 'write'`。
    这是"打包后一启动就闪退"的典型原因，必须锁死。
    """
    import logging as _logging

    import src.logging_setup as L

    print("\n--- 无控制台环境下的日志（windowed exe） ---")
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    root = _logging.getLogger()
    # ⚠️ 这段测试里 stdout 被置为 None，`print()` 会变成静默 no-op ——
    #    所以先把结果攒起来，**恢复 stdout 之后**再报告，
    #    否则连失败信息都看不见（等于没测）。
    results: list[tuple[str, bool, str]] = []
    try:
        sys.stdout = None
        sys.stderr = None
        L._CONFIGURED = False
        root.handlers.clear()
        try:
            L.setup_logging("INFO", None)
            results.append(
                ("sys.stdout/stderr 为 None 时 setup_logging 不抛异常", True, ""))
            no_none_stream = all(
                getattr(h, "stream", True) is not None for h in root.handlers)
            results.append(
                ("没有 stream 为 None 的 handler", no_none_stream,
                 f"实际 {root.handlers}"))

            lg = L.get_bound_logger("test.noconsole")
            lg.info("无控制台时写日志", key="value")
            lg.error("错误也不能崩", err_msg="boom")
            results.append(("无控制台时写日志不抛异常", True, ""))
        except Exception as e:                   # noqa: BLE001
            results.append(
                ("sys.stdout/stderr 为 None 时 setup_logging 不抛异常", False,
                 f"{type(e).__name__}: {e}"))
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        root.handlers.clear()
        L._CONFIGURED = False
        L.setup_logging("INFO", None)            # 恢复一个可用的日志配置

    for name, ok, detail in results:
        check(name, ok, detail)


def test_logging_upgrade_and_purity() -> None:
    """锁住三个实测踩过的日志 bug。

    1. **升级安装**：模块级 `log = get_bound_logger(__name__)` 会在 import 时
       先做一次无参初始化（`_CONFIGURED=True`）。此后 `main()` 里带 `log_dir`
       的 `setup_logging()` 必须**补装**文件 handler，不能被幂等检查吃掉 ——
       否则打包成 windowed exe 后一条日志都不落盘（实测踩过，4 天没写日志）。
    2. **着色隔离**：控制台的 ANSI 转义不能串进日志文件
       （`_ColorFormatter` 曾经就地改写共享的 `record.levelname`）。
    3. **extra 去重**：`extra_str` 是 filter 自己写的属性，第二轮 filter
       不能再把它当成自定义字段收一遍（会拼出嵌套的 `«… extra_str= «…»»`）。
    """
    import logging as _logging
    import tempfile

    import src.logging_setup as L

    print("\n--- 日志：升级安装 / 着色隔离 / extra 去重 ---")
    root = _logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_configured = L._CONFIGURED
    saved_ansi = L._enable_windows_ansi
    saved_stdout, saved_stderr = sys.stdout, sys.stderr

    tmp = Path(tempfile.mkdtemp(prefix="eve_logtest_"))
    results: list[tuple[str, bool, str]] = []
    try:
        # 强制走彩色分支，否则这条断言在无 ANSI 的环境里形同虚设
        L._enable_windows_ansi = lambda: True  # type: ignore[assignment]
        root.handlers.clear()
        L._CONFIGURED = False

        # ---- 1) 模拟模块级 import 时的无参初始化 ----
        L.get_bound_logger("test.module_level")
        results.append(("无参初始化后 _CONFIGURED 为 True", L._CONFIGURED is True, ""))

        # ---- 2) 之后带 log_dir 调用必须补装文件 handler ----
        L.setup_logging("INFO", tmp)
        results.append(("已配置后再带 log_dir 会补装文件 handler",
                        L._has_file_handler(root), f"handlers={root.handlers}"))

        lg = L.get_bound_logger("test.upgrade")
        lg.info("升级后的日志", device="扬声器 (MG-10)")

        log_file = tmp / "app.log"
        text = log_file.read_text(encoding="utf-8") if log_file.is_file() else ""
        results.append(("日志文件真的被写出来", bool(text.strip()), repr(text[:140])))
        results.append(("文件里没有 ANSI 转义（着色不串台）",
                        "\033[" not in text, repr(text[:200])))
        results.append(("自定义字段只出现一次（extra_str 不被二次收编）",
                        text.count("device=") == 1, repr(text[-200:])))
        results.append(("自定义字段内容可见", "device=扬声器" in text,
                        repr(text[-200:])))

        # ---- 3) 彩色格式化不能吞掉自定义字段 / 不能改写原 record ----
        rec = _logging.LogRecord("t", _logging.INFO, __file__, 1, "msg", (), None)
        rec.device = "扬声器"
        L._ContextFilter().filter(rec)
        colored = L._ColorFormatter(use_color=True).format(rec)
        plain = L._MessageFormatter(fmt="%(message)s").format(rec)
        results.append(("彩色格式化保留自定义字段", "device=扬声器" in colored,
                        repr(colored)))
        results.append(("非彩色格式化保留自定义字段", "device=扬声器" in plain,
                        repr(plain)))
        results.append(("彩色格式化不改写原 record（否则会污染文件日志）",
                        rec.levelname == "INFO" and rec.name == "t",
                        f"levelname={rec.levelname!r} name={rec.name!r}"))

        # ---- 4) force=True 全量重建 / 重复调用不堆叠 ----
        L.setup_logging("INFO", tmp, force=True)
        n_force = sum(1 for h in root.handlers
                      if isinstance(h, _logging.handlers.RotatingFileHandler))
        results.append(("force=True 重建后只剩一个文件 handler", n_force == 1,
                        f"{n_force} 个"))

        L.setup_logging("INFO", tmp)
        L.setup_logging("INFO", tmp)
        n_repeat = sum(1 for h in root.handlers
                       if isinstance(h, _logging.handlers.RotatingFileHandler))
        results.append(("重复调用不会堆叠文件 handler", n_repeat == 1,
                        f"{n_repeat} 个"))
    except Exception as e:                       # noqa: BLE001
        results.append(("日志升级测试未抛异常", False, f"{type(e).__name__}: {e}"))
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        L._enable_windows_ansi = saved_ansi  # type: ignore[assignment]
        root.handlers.clear()
        for h in saved_handlers:
            root.addHandler(h)
        L._CONFIGURED = saved_configured
        shutil.rmtree(tmp, ignore_errors=True)

    for name, ok, detail in results:
        check(name, ok, detail)


def test_logging_gbk_console() -> None:
    """从终端启动 windowed exe 时 stdout 是 GBK —— 日志里的 `«…»` 编不出来。

    现象：logging 吞掉 `UnicodeEncodeError`，每条带 extra 字段的日志都打一段
    `--- Logging error ---` traceback，把终端刷满，真出问题时反而看不见。

    为什么一直没暴露：源码模式 `sys.stdout.encoding` 是 `utf-8`；
    双击 exe 时 `sys.stdout is None`（根本不挂控制台 handler）——
    只有"从终端启动 exe"这一条路径会踩到。
    修法：把控制台流的 `errors` 策略从 `strict` 改成 `backslashreplace`。
    """
    import io
    import logging as _logging

    import src.logging_setup as L

    print("\n--- GBK 控制台（从终端启动 windowed exe） ---")

    # 造一个"中文 Windows 管道"：GBK 编码 + strict 错误策略
    buf = io.BytesIO()
    gbk = io.TextIOWrapper(buf, encoding="gbk", errors="strict", newline="")
    results: list[tuple[str, bool, str]] = []
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    root = _logging.getLogger()
    try:
        sys.stdout = gbk
        L._CONFIGURED = False
        root.handlers.clear()
        L.setup_logging("INFO", None)

        results.append(
            ("控制台流的 errors 策略已改成 backslashreplace",
             gbk.errors == "backslashreplace", f"实际 {gbk.errors!r}"))

        # logging 的 `--- Logging error ---` 是打到 sys.stderr 的 —— 抓起来
        captured = io.StringIO()
        sys.stderr = captured
        try:
            lg = L.get_bound_logger("test.gbk")
            # `«»`（U+00AB/U+00BB）不在 GBK 字符集里，正是它触发的崩溃
            lg.info("启动", provider="livetranslate", cwd=r"D:\trans")
            lg.warning("降级", process_name="mumble")
        finally:
            sys.stderr = saved_stderr
        gbk.flush()
        text = buf.getvalue().decode("gbk", errors="replace")
        err_text = captured.getvalue()

        results.append(("GBK 控制台下写日志不抛异常", True, ""))
        results.append(
            ("没有 `--- Logging error ---` traceback",
             "--- Logging error ---" not in err_text, err_text[:200]))
        results.append(("中文正常落到流里（GBK 能编码）", "启动" in text, text[:120]))
        results.append(
            ("编不出的 `«` 退化成转义而不是静默丢失",
             "\\xab" in text, text[:160]))
    except Exception as e:                       # noqa: BLE001
        results.append(("GBK 控制台下写日志不抛异常", False,
                        f"{type(e).__name__}: {e}"))
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        root.handlers.clear()
        L._CONFIGURED = False
        L.setup_logging("INFO", None)            # 恢复一个可用的日志配置

    for name, ok, detail in results:
        check(name, ok, detail)


def main() -> int:
    print("=" * 60)
    print("  P1 核心测试")
    print("=" * 60)

    test_ring_buffer()
    test_signal_utils()
    test_config()
    test_config_validation()
    test_frozen_paths()
    test_child_process_env()
    test_logging_without_console()
    test_logging_gbk_console()
    test_logging_upgrade_and_purity()

    print("\n" + "=" * 60)
    print(f"  结果: {PASS} 通过, {FAIL} 失败")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
