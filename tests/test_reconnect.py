"""断线检测 / 自动重连 单元测试。

背景（2026-09-29 实机暴露的严重 bug）
------------------------------------
真实舰队语音跑了 28 分钟后，服务端返回 `COMMON_ERROR: model repeat output
happened` 并以 WebSocket **close code 1011** 掐断会话。老实现的表现是：

1. 推送循环**完全不知道**连接已死，继续以 ~25 次/秒推 + 每条打一条 ERROR，
   实测刷了 **15,482 条 / 4.67MB**，把 5MB 轮转日志挤满、有用日志全丢；
2. **没有任何自动重连**，会话永久死亡，只能手动点托盘；
3. 同一条错误被两条路径推给 UI，悬浮窗里显示两遍。

这里把这些行为全部锁死。

运行:
    .venv\\Scripts\\python.exe tests\\test_reconnect.py
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.pipeline as P  # noqa: E402
from src.asr.live_translate import TranslationEvent  # noqa: E402
from src.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

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


class _FakeLive:
    """假 provider：可控存活状态 + 可控连续失败次数。"""

    def __init__(self, *, alive: bool = True, fail_times: int = 0) -> None:
        self._alive = alive
        self.fail_times = fail_times
        self.pushed = 0

    @property
    def is_alive(self) -> bool:
        return self._alive

    def push_audio(self, blk) -> None:            # noqa: ANN001
        if not self._alive:
            raise RuntimeError("连接已断开")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("模拟推送失败")
        self.pushed += 1


def _make_pipeline(**kw):                          # noqa: ANN003
    """建一个不连网、不采集的 Pipeline（只测逻辑）。"""
    cfg = load_config(str(ROOT / "config.yaml"))
    return P.Pipeline(cfg, on_text=lambda e: None, **kw)


def _drain(q: queue.Queue, n: int) -> None:
    for _ in range(n):
        q.put_nowait(np.zeros(160, dtype=np.float32))


# =========================================================
#  1. 断线后必须停止推送（不再刷日志）
# =========================================================
def test_pump_stops_when_dead() -> None:
    print("\n--- 断线后停止推送 ---")
    pl = _make_pipeline()
    pl._live = _FakeLive(alive=False)              # 模拟 on_close 已触发
    pl._stream_q = queue.Queue(maxsize=64)
    _drain(pl._stream_q, 20)

    t0 = time.monotonic()
    pl._pump_loop()                                # 直接同步跑，必须立刻返回
    dt = time.monotonic() - t0

    check("连接已死时推送循环立刻退出", dt < 1.0, f"耗时 {dt:.2f}s")
    check("退出后清空了积压队列（旧音频不能推给新连接）",
          pl._stream_q.empty(), f"剩余 {pl._stream_q.qsize()} 块")
    check("已发出「连接断开」信号", pl._conn_lost_evt.is_set())
    check("推送计数为 0（一条都没发出去）", pl._live.pushed == 0)


def test_pump_fail_limit() -> None:
    print("\n--- 连续推送失败 → 判定断线 ---")
    pl = _make_pipeline()
    pl._live = _FakeLive(alive=True, fail_times=99)
    pl._stream_q = queue.Queue(maxsize=64)
    _drain(pl._stream_q, 20)

    pl._pump_loop()

    check(f"连续失败 {P._PUSH_FAIL_LIMIT} 次后判定断线",
          pl._conn_lost_evt.is_set())
    check("失败次数没超过阈值就退出了（不刷屏）",
          pl._live.fail_times > 99 - P._PUSH_FAIL_LIMIT - 1,
          f"剩余 fail_times={pl._live.fail_times}")
    check("积压队列已清空", pl._stream_q.empty())


def test_pump_keeps_running_when_healthy() -> None:
    print("\n--- 连接正常时推送循环继续工作 ---")
    pl = _make_pipeline()
    pl._live = _FakeLive(alive=True)
    pl._stream_q = queue.Queue(maxsize=64)
    _drain(pl._stream_q, 5)

    th = threading.Thread(target=pl._pump_loop, daemon=True)
    th.start()
    deadline = time.monotonic() + 2.0
    while pl._live.pushed < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    pl._stop_evt.set()
    th.join(timeout=1.0)

    check("正常连接下音频被推出去", pl._live.pushed == 5,
          f"实际 {pl._live.pushed}")
    check("正常连接下不触发断线信号", not pl._conn_lost_evt.is_set())


def test_drop_backlog() -> None:
    print("\n--- 清空积压队列 ---")
    q: queue.Queue = queue.Queue()
    _drain(q, 7)
    n = P.Pipeline._drop_backlog(q)
    check("返回丢弃的块数", n == 7, f"实际 {n}")
    check("队列真的空了", q.empty())


# =========================================================
#  2. 自动重连（指数退避）
# =========================================================
def test_auto_reconnect_backoff() -> None:
    print("\n--- 自动重连指数退避 ---")
    saved_base, saved_max = P._RECONNECT_BASE_DELAY_S, P._RECONNECT_MAX_DELAY_S
    P._RECONNECT_BASE_DELAY_S, P._RECONNECT_MAX_DELAY_S = 0.01, 0.02
    try:
        pl = _make_pipeline()
        attempts: list[int] = []
        notices: list[tuple] = []
        pl.on_reconnecting = lambda a, d: notices.append((a, round(d, 3)))
        pl.on_reconnected = lambda a: attempts.append(a)

        calls = [0]

        def _fail_twice() -> None:
            """前两次失败，第三次成功。"""
            if calls[0] < 2:
                calls[0] += 1
                raise RuntimeError("模拟连接失败")

        pl._do_reconnect = _fail_twice        # type: ignore[method-assign]

        pl._auto_reconnect()

        check("重试到第 3 次才成功", attempts == [3], f"实际 {attempts}")
        check("每次都上报了 attempt 序号",
              [n[0] for n in notices] == [1, 2, 3], f"实际 {notices}")
        check("退避时间按 2 倍增长（0.01 → 0.02 → 0.02 封顶）",
              [n[1] for n in notices] == [0.01, 0.02, 0.02],
              f"实际 {notices}")
    finally:
        P._RECONNECT_BASE_DELAY_S, P._RECONNECT_MAX_DELAY_S = saved_base, saved_max


def test_auto_reconnect_gives_up() -> None:
    print("\n--- 重连到上限后放弃并通知 ---")
    saved_base = P._RECONNECT_BASE_DELAY_S
    saved_attempts = P._RECONNECT_MAX_ATTEMPTS
    P._RECONNECT_BASE_DELAY_S = 0.01
    P._RECONNECT_MAX_ATTEMPTS = 3
    try:
        pl = _make_pipeline()
        failed: list[int] = []
        pl.on_reconnect_failed = lambda a: failed.append(a)

        def _always_fail() -> None:
            raise RuntimeError("永远连不上")

        pl._do_reconnect = _always_fail        # type: ignore[method-assign]
        pl._auto_reconnect()

        check("用满全部尝试次数", failed == [3], f"实际 {failed}")
        check("放弃后不再处于「重连中」", pl._reconnecting is False)
    finally:
        P._RECONNECT_BASE_DELAY_S = saved_base
        P._RECONNECT_MAX_ATTEMPTS = saved_attempts


def test_request_reconnect_is_async() -> None:
    print("\n--- 手动重连不阻塞调用线程 ---")
    saved_base = P._RECONNECT_BASE_DELAY_S
    P._RECONNECT_BASE_DELAY_S = 0.01
    try:
        pl = _make_pipeline()
        done = threading.Event()
        seen: list[str] = []

        def _slow() -> None:
            time.sleep(0.3)                    # 模拟最长 15s 的连接阻塞
            seen.append("reconnected")
            done.set()

        pl._do_reconnect = _slow               # type: ignore[method-assign]

        t0 = time.monotonic()
        pl.request_reconnect()                 # 必须立刻返回
        dt = time.monotonic() - t0

        check("request_reconnect 立即返回（不阻塞 UI 线程）",
              dt < 0.2, f"耗时 {dt:.3f}s")
        check("重连在后台真的被执行了", done.wait(timeout=3.0),
              f"实际 {seen}")
    finally:
        P._RECONNECT_BASE_DELAY_S = saved_base
        pl._shutdown_evt.set()


def test_shutdown_stops_supervisor() -> None:
    print("\n--- stop() 必须能终止守护线程 ---")
    pl = _make_pipeline()
    check("初始未置位 shutdown", not pl._shutdown_evt.is_set())
    pl._capture = None
    pl._live = None
    pl.stop()
    check("stop() 置位了 _shutdown_evt（守护线程据此退出）",
          pl._shutdown_evt.is_set())
    check("stop() 也置位了 _stop_evt", pl._stop_evt.is_set())

    # 新生命周期必须能重新起来
    pl._stop_evt.clear()
    pl._shutdown_evt.clear()
    check("手动清位后可以重新开始", not pl._shutdown_evt.is_set())


# =========================================================
#  3. 错误只推一次（不重复显示）
# =========================================================
def test_error_event_not_duplicated() -> None:
    print("\n--- 服务端错误只推一次 ---")
    errors: list[Exception] = []
    translations: list[object] = []
    pl = _make_pipeline(
        on_error=lambda e: errors.append(e),
        on_translation=lambda e: translations.append(e),
    )

    pl._on_live_event(
        TranslationEvent(kind="error", translation="model repeat output happened")
    )

    check("on_error 被调用一次", len(errors) == 1, f"实际 {len(errors)}")
    check("错误消息带上下文前缀",
          "实时翻译服务端错误" in str(errors[0]), str(errors[0]))
    check("**没有**再走 on_translation（否则悬浮窗显示两遍）",
          translations == [], f"实际 {translations}")

    # 正常事件仍然要推
    pl._on_live_event(TranslationEvent(kind="translation", translation="星门"))
    check("正常翻译事件照常推送", len(translations) == 1,
          f"实际 {len(translations)}")


# =========================================================
#  4. 端到端自愈：模拟服务端 close 1011 → 自动重连 → 恢复推送
# =========================================================
class _FakeProvider:
    """假 provider：全局只模拟一次「服务端 1011 掐断」，之后保持健康。

    ⚠️ 用**全局预算**而不是"每个实例死一次" —— 否则重连出来的新连接
    也会立刻再断，测试就变成验证"无限重连循环"而不是"自愈一次"了。
    """

    created: list["_FakeProvider"] = []
    die_budget = 0

    def __init__(self) -> None:
        self.alive = False
        self.pushed = 0
        self.stopped = False
        _FakeProvider.created.append(self)

    @property
    def is_alive(self) -> bool:
        return self.alive

    def start(self) -> None:
        self.alive = True

    def stop(self, wait_final: bool = True) -> None:      # noqa: ARG002
        self.alive = False
        self.stopped = True

    def push_audio(self, blk) -> None:                    # noqa: ANN001
        if not self.alive:
            raise RuntimeError("连接已断开")
        self.pushed += 1
        if _FakeProvider.die_budget > 0:
            _FakeProvider.die_budget -= 1
            # 模拟真实场景：服务端发完 error 事件后 close code 1011
            self.alive = False
            raise RuntimeError(
                "WebSocket connection is not established or has been closed."
            )


class _FakeCapture:
    """假采集：不出声，只满足 Pipeline 的接口。"""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def get_block(self, timeout: float = 0.05):           # noqa: ANN201, ARG002
        return None
    def drain(self) -> int:
        return 0


def test_end_to_end_auto_recovery() -> None:
    print("\n--- 端到端自愈（模拟 1011 断线 → 自动重连 → 恢复推送）---")
    saved_create = P.create_live_translate
    saved_base, saved_max = P._RECONNECT_BASE_DELAY_S, P._RECONNECT_MAX_DELAY_S
    P._RECONNECT_BASE_DELAY_S, P._RECONNECT_MAX_DELAY_S = 0.05, 0.1
    _FakeProvider.created = []
    _FakeProvider.die_budget = 1                  # 只断一次

    pl = _make_pipeline()
    pl._capture = _FakeCapture()
    pl._live = _FakeProvider()

    lost: list[str] = []
    reconnected: list[int] = []
    pl.on_connection_lost = lambda r: lost.append(r)
    pl.on_reconnected = lambda a: reconnected.append(a)

    # `_do_reconnect` 会调工厂造新 provider —— 换成假的
    P.create_live_translate = (                       # type: ignore[assignment]
        lambda asr, on_event=None: _FakeProvider()
    )
    try:
        pl._start_stream()

        # 灌音频：第一次推送就会模拟断线
        for _ in range(8):
            pl.push_audio_raw(np.zeros(160, dtype=np.float32))
            time.sleep(0.02)

        deadline = time.monotonic() + 6.0
        while not reconnected and time.monotonic() < deadline:
            time.sleep(0.05)

        check("检测到断线并上报", len(lost) == 1, f"实际 {lost}")
        check("自动重连成功并上报", reconnected == [1], f"实际 {reconnected}")
        check("真的重建了 provider（工厂被再次调用）",
              len(_FakeProvider.created) == 2,
              f"实际 {len(_FakeProvider.created)} 个")
        check("旧 provider 被关闭", _FakeProvider.created[0].stopped)

        # 重连后新连接必须能继续收音频
        before = _FakeProvider.created[-1].pushed
        for _ in range(6):
            pl.push_audio_raw(np.zeros(160, dtype=np.float32))
            time.sleep(0.02)
        after = _FakeProvider.created[-1].pushed
        check("重连后新连接继续收到音频", after > before,
              f"before={before} after={after}")
        check("断线只发生了一次（没有陷入重连循环）",
              _FakeProvider.die_budget == 0 and len(_FakeProvider.created) == 2,
              f"budget={_FakeProvider.die_budget} "
              f"providers={len(_FakeProvider.created)}")
    finally:
        P.create_live_translate = saved_create             # type: ignore[assignment]
        P._RECONNECT_BASE_DELAY_S = saved_base
        P._RECONNECT_MAX_DELAY_S = saved_max
        pl.stop()
        pl._shutdown_evt.set()


def main() -> int:
    print("=" * 62)
    print("  断线检测 / 自动重连 测试")
    print("=" * 62)

    test_pump_stops_when_dead()
    test_pump_fail_limit()
    test_pump_keeps_running_when_healthy()
    test_drop_backlog()
    test_auto_reconnect_backoff()
    test_auto_reconnect_gives_up()
    test_request_reconnect_is_async()
    test_shutdown_stops_supervisor()
    test_error_event_not_duplicated()
    test_end_to_end_auto_recovery()

    print("\n" + "=" * 62)
    print(f"  结果: {PASS} 通过, {FAIL} 失败")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
