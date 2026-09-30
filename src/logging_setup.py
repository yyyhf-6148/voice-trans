"""结构化日志：控制台彩色 + 文件轮转。

用法：
    from src.logging_setup import setup_logging, get_logger
    setup_logging(level="INFO", log_dir=Path("logs"))
    log = get_logger(__name__)
    log.info("capture started", extra={"device": "xxx"})
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False

# LogRecord 的保留字段名。自定义字段若与这些重名，
# logging 会抛 KeyError("Attempt to overwrite 'xxx' in LogRecord")。
# 典型踩坑：log.error("...", message="...")  —— message 是保留名。
RESERVED_LOG_FIELDS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}

# 除 LogRecord 保留字段外，以下名字还会与 logging 方法自身的位置参数冲突：
#   LoggerAdapter.info(self, msg, *args, **kwargs)
# 传 msg=... 会报 got multiple values for argument 'msg'。
_UNSAFE_EXTRA_KEYS: frozenset[str] = RESERVED_LOG_FIELDS | {"msg", "args"}

# ANSI 颜色
_COLORS = {
    "DEBUG": "\033[36m",     # 青
    "INFO": "\033[32m",      # 绿
    "WARNING": "\033[33m",   # 黄
    "ERROR": "\033[31m",     # 红
    "CRITICAL": "\033[35m",  # 品红
}
_RESET = "\033[0m"
_DIM = "\033[2m"


def _enable_windows_ansi() -> bool:
    """在 Windows 上启用 ANSI 转义支持。"""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11, ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class _ContextFilter(logging.Filter):
    """把 extra 里的自定义字段拼到消息尾部。"""

    # ⚠️ `extra_str` 必须排除：它是本 filter **自己写上去的**。
    #    一个 record 会依次过 console / file 两个 handler 的 filter，
    #    若不排除，第二轮会把上一轮写的 extra_str 当成"自定义字段"再收一遍，
    #    结果拼出 `«device=X extra_str=  «device=X»»`（实测踩过）。
    _RESERVED = RESERVED_LOG_FIELDS | {"extra_str"}

    def filter(self, record: logging.LogRecord) -> bool:
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in self._RESERVED and not k.startswith("_")
        }
        record.extra_str = ""
        if extras:
            parts = " ".join(f"{k}={v}" for k, v in extras.items())
            record.extra_str = f"  «{parts}»"
        return True


class _MessageFormatter(logging.Formatter):
    """把 `_ContextFilter` 攒出来的 `extra_str` 追加到消息尾部。"""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = getattr(record, "extra_str", "")
        return base + extra if extra else base


class _ColorFormatter(_MessageFormatter):
    """带颜色的控制台格式化器（仅 Windows Terminal / 支持 ANSI 的环境）。

    必须继承 `_MessageFormatter` 而非裸 `logging.Formatter`：
    否则一旦开了颜色，自定义字段（`«device=…»`）会在控制台上**静默消失**
    （文件 handler 走 `_MessageFormatter` 所以照常有 —— 两边不一致，很难发现）。
    """

    def __init__(self, use_color: bool = True) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)-22s | %(message)s",
            datefmt="%H:%M:%S",
        )
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_color:
            return super().format(record)
        # ⚠️ 绝不能就地改 record.levelname / record.name：
        #    同一个 LogRecord 会被**所有** handler 依次处理，
        #    就地改写会让后跑的文件 handler 也拿到带 ANSI 的文本，
        #    日志文件里于是出现 `[32mINFO[0m` 这类乱码（实测踩过）。
        #    复制一份再着色，原 record 保持干净。
        colored = logging.makeLogRecord(record.__dict__)
        colored.levelname = (
            f"{_COLORS.get(record.levelname, '')}{record.levelname}{_RESET}"
        )
        colored.name = f"{_DIM}{record.name}{_RESET}"
        return super().format(colored)


def _attach_file_handler(root: logging.Logger, log_dir: Path) -> logging.Handler:
    """给 root 挂一个轮转文件 handler。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        _MessageFormatter(
            fmt="%(asctime)s %(levelname)-7s %(name)-30s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    file_handler.addFilter(_ContextFilter())
    root.addHandler(file_handler)
    return file_handler


def _has_file_handler(root: logging.Logger) -> bool:
    return any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
    )


def _make_console_stream_safe(stream):
    """让控制台流对"编不出来"的字符退化成转义，而不是抛异常。

    ⚠️ 为什么必须做
    --------------
    打包成 windowed exe（PyInstaller `console=False`）后，**从终端启动**时
    `sys.stdout` 会继承遗留代码页（中文 Windows 上是 **GBK/cp936**）。
    日志里的自定义字段包装符 `«…»`（U+00AB/U+00BB）**不在 GBK 字符集里** ——
    于是每写一条带 extra 字段的日志，logging 都会吞掉异常并打出一段
    `--- Logging error ---` traceback，把终端刷满，真出问题时反而看不见。

    源码模式下 `sys.stdout.encoding` 是 `utf-8`，所以这个问题**一直没暴露**；
    双击 exe 时 `sys.stdout is None`（不走控制台 handler），同样看不见 ——
    只有"从终端启动 exe"这一条路径会踩到。

    修法：把 `errors` 从 `strict` 改成 `backslashreplace` ——
    编不出的字符退化成 `\\xab` 字面量（**比 `replace` 好**：不静默丢信息，
    日志里还能看出原字符是什么）。中文本身在 GBK 里能编码，不受影响。
    """
    try:
        stream.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        # 不是 TextIOWrapper（如 pytest 的捕获对象）、或流已关闭 —— 忽略即可，
        # 退回原始行为（退化为偶发 traceback，不影响文件日志）。
        pass
    return stream


def setup_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    force: bool = False,
) -> None:
    """初始化根 logger。

    幂等，但**不是**"第二次直接无视"：

    ⚠️ 为什么不能简单地 `if _CONFIGURED: return`
    ------------------------------------------------
    本项目所有模块都写成 `log = get_bound_logger(__name__)`（模块级）。
    它在 **import 时**就会调用一次无参 `setup_logging()`，把 `_CONFIGURED` 置 True。
    于是 `main()` 里那句 `setup_logging(cfg.app.log_level, cfg.log_dir)`
    会被幂等检查直接吃掉 —— **文件 handler 永远装不上**。

    源码模式下控制台有输出，这个 bug 完全看不出来；
    打包成 windowed exe（无 stdout）后表现为"哪儿都没有日志"（实测踩过）。

    所以这里的语义是：已配置过时，如果本次带了 `log_dir` 而根 logger
    还没有文件 handler，就**补装**一个（不动已有的 console handler）。
    `force=True` 则全量重建（测试用）。
    """
    global _CONFIGURED
    root = logging.getLogger()

    if _CONFIGURED and not force:
        if log_dir is not None and not _has_file_handler(root):
            try:
                _attach_file_handler(root, Path(log_dir))
            except OSError as e:
                # 目录不可写也不能让程序起不来，但必须让人看见。
                # ⚠️ windowed exe 里 sys.stderr 是 None，不能直接 print(file=None)
                stream = sys.stderr if sys.stderr is not None else sys.stdout
                if stream is not None:
                    print(f"[logging] 无法创建日志文件于 {log_dir}: {e}", file=stream)
        return

    root.setLevel(logging.DEBUG)  # 由 handler 控制实际输出级别
    root.handlers.clear()

    ctx_filter = _ContextFilter()
    use_color = _enable_windows_ansi()

    # --- 控制台 ---
    #
    # ⚠️ 打包成 windowed exe（PyInstaller `console=False`）后，
    #    `sys.stdout` / `sys.stderr` 都是 **None**。
    #    此时 `logging.StreamHandler(None)` 会把 stream 设成 None，
    #    第一条日志就 AttributeError: 'NoneType' object has no attribute 'write'。
    #    所以没有可用流时**直接跳过**控制台 handler，只保留文件日志。
    console_stream = sys.stdout if sys.stdout is not None else sys.stderr
    if console_stream is not None:
        # ⚠️ 从终端启动 windowed exe 时 stdout 是 GBK —— 日志里的 `«…»`
        #    编不出来会让每条日志吐一段 `--- Logging error ---`（实测踩过）。
        console_stream = _make_console_stream_safe(console_stream)
        console = logging.StreamHandler(console_stream)
        console.setLevel(getattr(logging, level.upper(), logging.INFO))
        console.setFormatter(
            _MessageFormatter(
                fmt="%(asctime)s %(levelname)-7s %(name)-22s | %(message)s "
                if not use_color
                else "{}%(asctime)s %(levelname)-7s %(name)-22s{} | %(message)s".format(
                    _DIM, _RESET
                ),
                datefmt="%H:%M:%S",
            )
        )
        console.addFilter(ctx_filter)
        if use_color:
            # 单独套一层颜色处理（levelname/name 着色）
            color_fmt = _ColorFormatter(use_color=True)
            color_fmt._style._fmt = (
                "%(asctime)s %(levelname)-16s %(name)-31s | %(message)s"
            )
            console.setFormatter(color_fmt)
        root.addHandler(console)

    # --- 文件 ---
    if log_dir is not None:
        _attach_file_handler(root, log_dir)

    # 压低第三方库噪声
    for noisy in ("httpx", "httpcore", "urllib3", "faster_whisper", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # dashscope 在 DEBUG 级会把每个音频块都打一行（`append audio: 1368`），
    # 实测 3 秒会话 125 行日志里 96 行是它 —— 5MB 的轮转文件几小时就被刷满，
    # 真出问题时反而翻不到有用信息。压到 INFO 只留 session created/finished
    # 这类有诊断价值的生命周期事件。
    logging.getLogger("dashscope").setLevel(logging.INFO)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取 logger。未初始化时自动以 INFO 初始化控制台输出。

    返回的 Logger 被包装过 `bind` 风格调用支持：
        log.info("msg", device="x")   # 直接传 kwarg 会自动包进 extra

    注意：这里的自动初始化**没有文件 handler**（此刻还不知道 log_dir）。
    之后带 `log_dir` 再调一次 `setup_logging()` 会把文件 handler 补上，
    不会因为"已配置"被忽略。
    """
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


class BoundLogger(logging.LoggerAdapter):
    """支持 `log.info(msg, key=val)` 写法的 Logger 适配器。

    标准 logging 要求自定义字段必须写成 `extra={"key": val}`，
    这里做了自动包装，避免调用方到处写 extra=。

    ⚠️ 为什么必须重写各级别方法，而不能只改 process()
    --------------------------------------------------
    `LoggerAdapter.info(self, msg, *args, **kwargs)` 的 `msg` 是**位置参数名**。
    调用 `log.info("x", message="y")` 时 Python 先做参数绑定：
      · message  → 与 LogRecord 字段冲突 → KeyError("Attempt to overwrite")
      · msg      → 与位置参数冲突 → TypeError: got multiple values for 'msg'
    这两种冲突在**进入 process() 之前**就已抛出，process() 无从补救。
    因此这里用 `**kwargs` 全量接管，且**首参不命名为 msg**
    （改叫 `message_`），使 `msg=` 能安全落到 kwargs 里被改名。
    """

    @staticmethod
    def _sanitize(msg, kwargs: dict) -> tuple[str, dict]:
        reserved = {"exc_info", "stack_info", "stacklevel", "extra"}
        extra = kwargs.pop("extra", {}) or {}
        passthrough = {k: kwargs.pop(k) for k in list(kwargs) if k in reserved}
        for k, v in kwargs.items():
            # 冲突键加 f_ 前缀：既保留信息，又避开保留名与位置参数名
            safe_k = f"f_{k}" if k in _UNSAFE_EXTRA_KEYS else k
            extra[safe_k] = v
        if extra:
            passthrough["extra"] = extra
        return msg, passthrough

    def process(self, msg, kwargs):  # type: ignore[override]
        return self._sanitize(msg, kwargs)

    # 首参命名为 message_ 而非 msg：若叫 msg，则 msg= 会绑定到它，
    # 导致 "got multiple values for argument 'msg'" 无法规避。
    def debug(self, message_=None, *args, **kwargs):  # type: ignore[override]
        m, kw = self._sanitize(message_, kwargs)
        self.log(logging.DEBUG, m, *args, **kw)

    def info(self, message_=None, *args, **kwargs):  # type: ignore[override]
        m, kw = self._sanitize(message_, kwargs)
        self.log(logging.INFO, m, *args, **kw)

    def warning(self, message_=None, *args, **kwargs):  # type: ignore[override]
        m, kw = self._sanitize(message_, kwargs)
        self.log(logging.WARNING, m, *args, **kw)

    def error(self, message_=None, *args, **kwargs):  # type: ignore[override]
        m, kw = self._sanitize(message_, kwargs)
        self.log(logging.ERROR, m, *args, **kw)

    def critical(self, message_=None, *args, **kwargs):  # type: ignore[override]
        m, kw = self._sanitize(message_, kwargs)
        self.log(logging.CRITICAL, m, *args, **kw)

    def exception(self, message_=None, *args, **kwargs):  # type: ignore[override]
        kwargs.setdefault("exc_info", True)
        m, kw = self._sanitize(message_, kwargs)
        self.log(logging.ERROR, m, *args, **kw)


def get_bound_logger(name: str, **context: object) -> BoundLogger:
    """获取带固定上下文字段的 logger。

    用法：
        log = get_bound_logger(__name__, component="capture")
        log.info("started", device="speaker")

    ⚠️ 模块级调用（`log = get_bound_logger(__name__)`）会在 import 时就完成
    一次**无文件**初始化。别担心 —— 之后 `setup_logging(..., log_dir=...)`
    会补装文件 handler（见 `setup_logging` 文档）。
    """
    if not _CONFIGURED:
        setup_logging()
    return BoundLogger(logging.getLogger(name), dict(context))
