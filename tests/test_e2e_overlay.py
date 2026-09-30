"""端到端集成测试：WAV 灌音 → pipeline → 悬浮窗渲染 → 截图。

验证的是**完整链路**，而不是各组件单独工作：
    音频 → 云端实时翻译 → 回调 → 悬浮窗 → 绘制
最后产出一张真实翻译结果的截图。

不需要声卡（用 --wav 灌音），但**需要 DASHSCOPE_API_KEY**。

用法:
    .venv\\Scripts\\python.exe tests/test_e2e_overlay.py
    .venv\\Scripts\\python.exe tests/test_e2e_overlay.py --wav _en_sample.wav --speed 2
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.errors import AppError  # noqa: E402
from src.pipeline import Pipeline  # noqa: E402

_OK = 0
_FAIL = 0


def chk(name: str, cond: bool, extra: str = "") -> None:
    global _OK, _FAIL
    if cond:
        print(f"  [PASS] {name}")
        _OK += 1
    else:
        print(f"  [FAIL] {name} {extra}")
        _FAIL += 1


def load_wav_16k_mono(path: Path):
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SystemExit(f"[X] 只支持 16bit WAV，实际 {width*8}bit")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        n = int(len(data) * 16000 / sr)
        idx = np.linspace(0, len(data) - 1, n)
        data = np.interp(idx, np.arange(len(data)), data).astype(np.float32)
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default="_en_sample.wav")
    ap.add_argument("--speed", type=float, default=2.0)
    ap.add_argument("--out", default="overlay_e2e.png")
    args = ap.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    cfg = load_config(str(ROOT / "config.yaml"))
    if (cfg.asr.provider or "").lower() != "livetranslate":
        print(f"[X] 需要 asr.provider=livetranslate，当前 {cfg.asr.provider}")
        return 2
    if not os.environ.get(cfg.asr.livetranslate.api_key_env):
        print(f"[X] 环境变量 {cfg.asr.livetranslate.api_key_env} 未设置")
        return 2

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from src.ui.overlay import OverlayWindow

    app = QApplication.instance() or QApplication(sys.argv)

    print("=" * 70)
    print("  端到端集成测试：音频 → 云端翻译 → 悬浮窗")
    print("=" * 70)

    overlay = OverlayWindow(cfg.overlay)
    overlay.show()

    # 用 pipeline 的流式模式，把 on_translation 直接接到悬浮窗
    got: list = []

    class Collector:
        """把 pipeline 回调转给悬浮窗，同时记录统计。

        ⚠️ 必须按 `ev.kind` 分流：
           原文事件（kind="original"）只带英文，`translation` 是空串 ——
           如果无脑塞给 push_translation，会先造出一条空译文条目，
           再被 push_original 认领，看起来"配对成功"但其实是自欺欺人。
           app.py 的真实路由就是 original → push_original。
        """

        def __init__(self, ov) -> None:
            self.ov = ov
            self.finals = 0
            self.partials = 0
            self.originals = 0
            self.errors = 0
            self.delays: list[float] = []

        def __call__(self, ev) -> None:
            got.append(ev)
            if ev.kind == "error":
                self.errors += 1
                self.ov.push_error(ev.translation)
                return
            if not ev.is_displayable:
                return

            if ev.kind == "original":
                self.originals += 1
                self.ov.push_original(
                    ev.original,
                    is_partial=ev.is_partial,
                    sequence=ev.sequence,
                    language=ev.language,
                    emotion=ev.emotion,
                )
                return

            if ev.is_partial:
                self.partials += 1
            else:
                self.finals += 1
                if ev.first_text_delay_ms:
                    self.delays.append(ev.first_text_delay_ms)
            self.ov.push_translation(
                ev.translation,
                original=ev.original,
                is_partial=ev.is_partial,
                sequence=ev.sequence,
                first_text_delay_ms=ev.first_text_delay_ms,
                language=ev.language,
                emotion=ev.emotion,
            )

    coll = Collector(overlay)

    pipeline = Pipeline(
        cfg,
        on_text=lambda ev: None,
        on_translation=coll,
        on_error=lambda e: print(f"  [pipeline error] {e}"),
    )

    wav = Path(args.wav)
    if not wav.is_absolute():
        wav = ROOT / wav
    if not wav.exists():
        print(f"[X] 找不到 {wav}")
        return 2

    audio = load_wav_16k_mono(wav)
    dur = len(audio) / 16000.0
    print(f"  音频: {wav.name}  {dur:.1f}s  倍速 {args.speed:g}×")

    print("\n--- 建立连接 ---")
    try:
        pipeline.setup()
        pipeline.start_without_capture()
    except AppError as e:
        print(f"  [FAIL] 启动失败: {e}")
        if e.detail:
            print(f"         {e.detail}")
        return 3
    chk("云端连接建立", pipeline.connect_seconds > 0)
    print(f"         连接耗时 {pipeline.connect_seconds:.2f}s")

    print("\n--- 灌入音频 ---")
    step = 1600
    sleep_s = 0.1 / max(args.speed, 1e-6)
    t0 = time.monotonic()
    for i in range(0, len(audio), step):
        pipeline.push_audio_raw(audio[i: i + step])
        time.sleep(sleep_s)
        # 让 Qt 事件循环有喘息机会（offscreen 下也需手动泵）
        app.processEvents()
    print(f"  灌完，耗时 {time.monotonic()-t0:.1f}s")

    print("\n--- 等待云端输出 ---")
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        app.processEvents()
        overlay._tick()
        time.sleep(0.05)
    app.processEvents()
    overlay._tick()

    # ---- 校验 ----
    print("\n--- 校验 ---")
    chk("收到翻译事件", len(got) > 0, f"实际 {len(got)} 条")
    chk("有定稿译文", coll.finals > 0, f"实际 {coll.finals} 条")
    chk("无服务端错误", coll.errors == 0, f"实际 {coll.errors} 次")

    items = overlay.store.items
    chk("悬浮窗有条目", len(items) > 0, f"实际 {len(items)} 条")

    with_zh = [i for i in items if i.translation]
    chk("条目含中文译文", len(with_zh) > 0)

    # 中文校验：至少有一条译文里含 CJK 字符
    def has_cjk(s: str) -> bool:
        return any("\u4e00" <= c <= "\u9fff" for c in s)

    zh_ok = [i for i in with_zh if has_cjk(i.translation)]
    chk("译文确实是中文（含 CJK 字符）", len(zh_ok) > 0,
        f"实际 {len(zh_ok)}/{len(with_zh)}")

    # 增量合并校验：条目数应远小于事件数
    chk("增量被正确合并（条目 << 事件）",
        len(items) < len(got),
        f"条目 {len(items)} vs 事件 {len(got)}")

    # 双语字幕：先按**事件流**重建"应该"的配对，再和悬浮窗里的实际配对比对。
    #
    # 实测服务端的顺序是  original(seq=N) → translation(seq=N+1) ，
    # 两者序号相差 1，所以只能靠"紧邻"配对，不能靠序号相等。
    # 旧版 SubtitleStore 会把新原文错挂到**上一句**译文上（配对全错），
    # 这里用完整比对把它锁死，而不是只看"有没有配上的"。
    expect_pairs: list[tuple[str, str]] = []
    pend_en: str | None = None
    for e in got:
        if not e.is_displayable:
            continue
        if e.kind == "original" and not e.is_partial and e.original:
            pend_en = e.original
        elif e.kind == "translation" and not e.is_partial and e.translation:
            if pend_en:
                expect_pairs.append((pend_en, e.translation))
                pend_en = None

    actual_pairs = [(i.original, i.translation) for i in items
                    if i.translation and i.original]
    chk("事件流里存在「原文 → 译文」配对", len(expect_pairs) > 0,
        f"实际 {len(expect_pairs)} 对")
    chk("悬浮窗配对与事件流完全一致（无错位）",
        actual_pairs == expect_pairs,
        f"窗口 {actual_pairs[:3]} vs 期望 {expect_pairs[:3]}")

    # 高度适配
    chk("窗口高度在配置范围内",
        cfg.overlay.min_height <= overlay.height() <= cfg.overlay.max_height,
        f"实际 {overlay.height()}")

    # ---- 渲染截图 ----
    print("\n--- 渲染截图 ---")
    img = QImage(overlay.width(), overlay.height(), QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    for y in range(0, overlay.height(), 16):
        for x in range(0, overlay.width(), 16):
            shade = 60 if ((x // 16) + (y // 16)) % 2 == 0 else 90
            p.fillRect(x, y, 16, 16, QColor(shade, shade, shade + 8))
    p.end()
    overlay.render(img)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    chk("截图保存成功", img.save(str(out)), str(out))

    # ---- 结果 ----
    print("\n" + "=" * 70)
    print("  实际收到的翻译")
    print("=" * 70)
    for it in items:
        if not it.translation:
            continue
        mark = ""
        if it.emotion and it.emotion not in ("neutral", ""):
            mark = f"  [{it.emotion}]"
        delay = f"  {it.first_text_delay_ms:.0f}ms" if it.first_text_delay_ms else ""
        print(f"  {it.translation}{mark}{delay}")
        if it.original:
            print(f"    └ {it.original}")

    print("\n" + "=" * 70)
    print(f"  定稿 {coll.finals}  增量 {coll.partials}  "
          f"原文 {coll.originals}  错误 {coll.errors}")
    if coll.delays:
        import statistics
        d = coll.delays
        print(f"  首字延迟: 中位 {statistics.median(d):.0f}ms  "
              f"最大 {max(d):.0f}ms")
    print(f"  悬浮窗条目: {len(items)}")
    print(f"  截图: {out}")
    print("=" * 70)

    pipeline.stop()
    overlay.close()

    print(f"\n  结果: {_OK} 通过, {_FAIL} 失败")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
