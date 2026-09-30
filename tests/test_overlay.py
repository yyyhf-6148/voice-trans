"""P3 悬浮窗 / 字幕模型 / 热键 的自动化自检。

分两部分：
  A. 纯逻辑（SubtitleStore）—— 无需 Qt，任何环境可跑
  B. Qt 部分（OverlayWindow）—— 用 offscreen 平台无头运行

用法:
    .venv\\Scripts\\python.exe tests/test_overlay.py
    .venv\\Scripts\\python.exe tests/test_overlay.py --logic-only
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
from src.ui.subtitle_model import SubtitleStore  # noqa: E402

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


# ================================================================ A. 字幕模型
def test_subtitle_store() -> None:
    print("=" * 70)
    print("  A. 字幕模型（纯逻辑）")
    print("=" * 70)

    print("\n--- 增量合并 ---")
    s = SubtitleStore(max_messages=30)
    s.push("去抓", is_partial=True, sequence=1)
    chk("第一段增量创建条目", len(s.items) == 1 and s.items[0].translation == "去抓")
    s.push("去抓星门", is_partial=True, sequence=1)
    chk("后续增量合并到同一条", len(s.items) == 1)
    chk("文本被更新为最新", s.items[0].translation == "去抓星门")
    chk("合并计数正确", s.merged_partials == 1)

    s.push("去抓星门", is_partial=False, sequence=1)
    chk("定稿收敛同一条", len(s.items) == 1)
    chk("定稿标记正确", s.items[0].finalized is True)
    chk("定稿计数正确", s.total_final == 1)

    s.push("奶妈把电容保持住", is_partial=False, sequence=2)
    chk("新一句创建新条目", len(s.items) == 2)
    chk("新条目也是定稿", s.items[1].finalized)

    print("\n--- 增量乱序 / 前缀差异 ---")
    s2 = SubtitleStore()
    # 云端有时只推新增片段（后缀），应合并而不是另起一条
    s2.push("去抓星门", is_partial=True)
    s2.push("星门", is_partial=True)
    chk("后推是后缀片段时仍合并", len(s2.items) == 1,
        f"实际 {len(s2.items)} 条")
    chk("后缀片段不重复拼接",
        s2.items[0].translation == "去抓星门",
        repr(s2.items[0].translation))

    # 只推新增部分 → 靠 sequence 相同识别为同一句
    s2b = SubtitleStore()
    s2b.push("去抓", is_partial=True, sequence=7)
    s2b.push("星门", is_partial=True, sequence=7)
    chk("同序号的新增片段被合并", len(s2b.items) == 1,
        f"实际 {len(s2b.items)} 条")
    chk("追加结果正确", s2b.items[0].translation == "去抓星门",
        repr(s2b.items[0].translation))

    # 序号不同 → 视为两轮，旧碎片被丢弃
    s2c = SubtitleStore()
    s2c.push("去抓", is_partial=True, sequence=7)
    s2c.push("星门", is_partial=True, sequence=8)
    chk("不同序号另起条目", len(s2c.items) == 1)
    chk("保留的是新一轮内容",
        s2c.items[0].translation == "星门", repr(s2c.items[0].translation))

    s3 = SubtitleStore()
    s3.push("去抓星门", is_partial=True)
    s3.push("完全不同的内容", is_partial=True)
    chk("内容完全不同则另起一条", len(s3.items) == 1)
    chk("旧碎片被丢弃而非保留",
        s3.items[0].translation == "完全不同的内容",
        repr(s3.items[0].translation))

    print("\n--- 累积式增量不重复拼接（真实踩坑）---")
    # 实测 Qwen 推的是累积式前缀："先打" → "先打的是" → "先打的是猎鹰"
    # 无脑追加会拼出 "先打是先打的是先打的是猎" 这种垃圾
    s3c = SubtitleStore()
    for frag in ("先打", "先打的是", "先打的是猎鹰"):
        s3c.push(frag, is_partial=True, sequence=5)
    chk("累积式增量合并为一条", len(s3c.items) == 1,
        f"实际 {len(s3c.items)} 条")
    chk("结果为最新完整版", s3c.items[0].translation == "先打的是猎鹰",
        repr(s3c.items[0].translation))

    print("\n--- 定稿后重复喊同一句 ---")
    s3b = SubtitleStore()
    s3b.push("全体跃迁", is_partial=True)
    s3b.push("全体跃迁", is_partial=False)
    s3b.push("全体跃迁", is_partial=True)   # 指挥又喊了一遍
    chk("定稿后新喊一句另起条目", len(s3b.items) == 2,
        f"实际 {len(s3b.items)} 条")

    print("\n--- 漏发定稿的兜底：碎片被丢弃而非上屏 ---")
    # 实测问题：未定稿的增量碎片若保留，屏幕上会出现
    # "先打是先打的是先打的是猎" + "先打的是猎鹰。" 两条。
    s4 = SubtitleStore()
    it = s4.push("超时未定稿", is_partial=True)
    it.updated_at -= 10.0  # 伪装成 10 秒没更新
    changed = s4.finalize_stale(timeout_s=3.0)
    chk("超时未定稿条目被清理", changed)
    chk("碎片已从列表移除", len(s4.items) == 0, f"实际 {len(s4.items)} 条")
    chk("活动条目引用已清空", s4._active is None)
    # 后续新内容正常另起一条
    s4.push("新的一句", is_partial=True)
    chk("后续内容正常入列表", len(s4.items) == 1)

    print("\n--- 原文归属（双语字幕）---")
    s5 = SubtitleStore()
    s5.push("去抓星门", is_partial=True)
    s5.push_original("Tackle the gate", is_partial=False, language="en")
    chk("原文挂到当前译文条目上", len(s5.items) == 1)
    chk("英文原文已写入", s5.items[0].original == "Tackle the gate")
    chk("语言标记已写入", s5.items[0].language == "en")

    s6 = SubtitleStore()
    s6.push_original("没有对应译文的原文", is_partial=False)
    chk("无译文时原文独立成条", len(s6.items) == 1 and s6.items[0].original)

    print("\n--- 原文先到 / 译文后到（实测服务端顺序）---")
    # 实测（2026-09-28 抓真实事件流）：
    #   original(seq=3) → translation(seq=4)
    # 原文定稿在前、译文定稿在后，两者**序号相差 1**。
    # 旧版靠"找最近一条还没有原文的条目"兜底，会把新原文错挂到
    # **上一句**的译文上（实测配对全错，e2e 断言 0 条配对）。
    s6b = SubtitleStore()
    s6b.push_original("All right. Fleet.", is_partial=False, sequence=3,
                      language="en")
    s6b.push("好的。舰队。", is_partial=False, sequence=4)
    chk("原文先到：译文并入同一条", len(s6b.items) == 1,
        f"实际 {len(s6b.items)} 条")
    chk("原文先到：配对正确",
        s6b.items[0].original == "All right. Fleet."
        and s6b.items[0].translation == "好的。舰队。",
        f"实际 {s6b.items[0].original!r} / {s6b.items[0].translation!r}")
    chk("原文先到：语言标记保留", s6b.items[0].language == "en")
    chk("原文先到：条目已定稿", s6b.items[0].finalized)
    chk("原文先到：挂起标记已清空", s6b._pending_original is None)

    # 连续两句都不能错位（旧版第二句的原文会挂到第一句上）
    s6c = SubtitleStore()
    s6c.push_original("Listen. listen up.", is_partial=False, sequence=7)
    s6c.push("听着。注意听。", is_partial=False, sequence=8)
    s6c.push_original("Primary is the Falcon.", is_partial=False, sequence=11)
    s6c.push("先打的是猎鹰号。", is_partial=False, sequence=12)
    chk("连续两句各自成条", len(s6c.items) == 2,
        f"实际 {len(s6c.items)} 条")
    chk("第一句原文未错挂到第二句",
        s6c.items[0].original == "Listen. listen up."
        and s6c.items[0].translation == "听着。注意听。",
        f"实际 {s6c.items[0].original!r} / {s6c.items[0].translation!r}")
    chk("第二句配对正确",
        s6c.items[1].original == "Primary is the Falcon."
        and s6c.items[1].translation == "先打的是猎鹰号。",
        f"实际 {s6c.items[1].original!r} / {s6c.items[1].translation!r}")

    # 原文增量 → 译文增量 → 译文定稿：全程必须还是同一条
    s6d = SubtitleStore()
    s6d.push_original("Tackle", is_partial=True, sequence=1)
    s6d.push_original("Tackle the gate", is_partial=True, sequence=2)
    s6d.push_original("Tackle the gate.", is_partial=False, sequence=3)
    chk("原文增量补全为一条", len(s6d.items) == 1,
        f"实际 {len(s6d.items)} 条")
    s6d.push("去抓", is_partial=True, sequence=4)
    s6d.push("去抓星门", is_partial=True, sequence=5)
    chk("译文增量并入原文条目", len(s6d.items) == 1,
        f"实际 {len(s6d.items)} 条")
    s6d.push("去抓星门。", is_partial=False, sequence=6)
    chk("定稿后仍是同一条", len(s6d.items) == 1,
        f"实际 {len(s6d.items)} 条")
    chk("原文与译文都在",
        s6d.items[0].original == "Tackle the gate."
        and s6d.items[0].translation == "去抓星门。",
        f"实际 {s6d.items[0].original!r} / {s6d.items[0].translation!r}")

    # 同一次说话（序号相同，调用方一次把译文+原文推来）→ 原文挂到活动条目
    s6e = SubtitleStore()
    s6e.push("奶妈", is_partial=True, sequence=1)
    s6e.push_original("Logi", is_partial=False, sequence=1, language="en")
    chk("同序号：原文挂到活动条目", len(s6e.items) == 1,
        f"实际 {len(s6e.items)} 条")
    chk("同序号：原文已写入", s6e.items[0].original == "Logi")

    print("\n--- 配对的两个边界（防止以后被误改）---")
    # (1) 挂起超时后不再认领 —— 避免把很久以前的孤立原文错配给新译文
    s6f = SubtitleStore()
    it6f = s6f.push_original("孤立原文", is_partial=False, sequence=1)
    it6f.updated_at -= 100.0
    s6f.push("后来的译文", is_partial=False, sequence=9)
    chk("超时的挂起原文不被认领",
        len(s6f.items) == 2 and s6f.items[0].translation == "",
        f"实际 {[(i.original, i.translation) for i in s6f.items]}")

    # (2) 上一句译文还在流式输出时，下一句的原文先到 → 必须另起一条，
    #     不能被上一句的译文（活动条目）吃掉
    s6g = SubtitleStore()
    s6g.push("上一句还在流", is_partial=True, sequence=5)
    s6g.push_original("Next sentence original", is_partial=False,
                      sequence=6, language="en")
    chk("重叠时下一句原文不错挂到上一句",
        len(s6g.items) == 2 and s6g.items[0].original == ""
        and s6g.items[1].original == "Next sentence original",
        f"实际 {[(i.original, i.translation) for i in s6g.items]}")
    # 上一句的译文定稿 → 收敛进它自己那条，不去认领下一句的原文
    s6g.push("上一句还在流完了", is_partial=False, sequence=7)
    chk("迟到的定稿收敛进自己那条",
        len(s6g.items) == 2
        and s6g.items[0].translation == "上一句还在流完了"
        and s6g.items[0].original == "",
        f"实际 {[(i.original, i.translation) for i in s6g.items]}")

    # (3) 已知边界：译文**定稿**先到、原文后到，无法判断归属 ——
    #     实测服务端不是这个顺序，这里明确记录当前行为，避免被误当 bug 乱改
    s6h = SubtitleStore()
    s6h.push("译文先定稿", is_partial=False, sequence=1)
    s6h.push_original("Original later", is_partial=False, sequence=2)
    chk("译文定稿先到 + 原文后到：原文另起一条（已知边界）",
        len(s6h.items) == 2 and s6h.items[1].original == "Original later",
        f"实际 {[(i.original, i.translation) for i in s6h.items]}")

    print("\n--- 情绪高亮 ---")
    s7 = SubtitleStore()
    s7.push("全体跃迁", is_partial=False, emotion="angry")
    chk("非 neutral 情绪标记为 alert", s7.items[0].is_alert)
    s8 = SubtitleStore()
    s8.push("平稳汇报", is_partial=False, emotion="neutral")
    chk("neutral 不算 alert", not s8.items[0].is_alert)

    print("\n--- 条数上限 ---")
    s9 = SubtitleStore(max_messages=5)
    for i in range(12):
        s9.push(f"第{i}句", is_partial=False)
    chk("条数被限制在 5", len(s9.items) == 5, f"实际 {len(s9.items)}")
    chk("保留的是最新的", s9.items[-1].translation == "第11句")

    print("\n--- 字符裁剪 ---")
    s10 = SubtitleStore(max_chars=10)
    s10.push("A" * 50, is_partial=False)
    chk("超长文本被裁剪", len(s10.items[0].translation) == 11,
        f"实际 {len(s10.items[0].translation)}")
    chk("裁剪后带省略号", s10.items[0].translation.endswith("…"))

    print("\n--- hold_seconds 过期 ---")
    s11 = SubtitleStore(hold_seconds=30.0)
    it11 = s11.push("旧句子", is_partial=False)
    it11.updated_at -= 100.0
    s11.push("新句子", is_partial=False)
    changed = s11.expire_old()
    chk("过期条目被移除", changed)
    chk("只留下新句子", len(s11.items) == 1 and s11.items[0].translation == "新句子")

    s12 = SubtitleStore(hold_seconds=0)
    it12 = s12.push("不该过期", is_partial=False)
    it12.updated_at -= 10000.0
    chk("hold_seconds=0 时不过期", not s12.expire_old())

    print("\n--- 未定稿碎片同样会过期 ---")
    # 行为变更（2026-09-25 端到端测试暴露）：未定稿条目只是增量碎片，
    # 若永不过期会一直占屏（"碎片 + 完整句"并存）。现在统一定为过期淘汰。
    s13 = SubtitleStore(hold_seconds=5.0)
    it13 = s13.push("正在说话", is_partial=True)
    it13.updated_at -= 100.0
    s13.push("新的一句", is_partial=False)
    changed13 = s13.expire_old()
    chk("过期检查报告了变化", changed13)
    chk("未定稿碎片被清掉",
        not any(i.translation == "正在说话" for i in s13.items))
    chk("定稿条目保留", any(i.translation == "新的一句" for i in s13.items))
    chk("活动引用被同步置空", s13._active is None)

    print("\n--- 错误提示 ---")
    s14 = SubtitleStore()
    e = s14.push_error("连接断开")
    chk("错误条目以 [错误] 开头", e.translation.startswith("[错误]"))
    chk("错误条目为定稿态", e.finalized)

    print("\n--- 清空 ---")
    s15 = SubtitleStore()
    s15.push("A", is_partial=True)
    s15.clear()
    chk("清空后无条目", not s15.items)
    chk("清空后活动引用为 None", s15._active is None)


# ================================================================ B. Qt 部分
def test_overlay_qt() -> None:
    print()
    print("=" * 70)
    print("  B. 悬浮窗（Qt offscreen）")
    print("=" * 70)

    # 必须用 offscreen，否则 CI / 无桌面环境会崩
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        chk("PySide6 可用", False, str(e))
        return

    from src.ui.overlay import OverlayWindow

    cfg = load_config(str(ROOT / "config.yaml"))
    app = QApplication.instance() or QApplication(sys.argv)

    print("\n--- 构造与基础属性 ---")
    o = OverlayWindow(cfg.overlay)
    chk("窗口创建成功", o is not None)
    chk("初始不透明度与配置一致",
        abs(o.opacity - cfg.overlay.opacity) < 1e-6)
    chk("初始点击穿透与配置一致",
        o.click_through == cfg.overlay.click_through)
    chk("宽度与配置一致", o.width() == cfg.overlay.width)
    chk("高度不低于 min_height", o.height() >= cfg.overlay.min_height)

    # ⚠️ windowFlags() 的字符串表示是数字，不能做子串匹配，必须按位判
    from PySide6.QtCore import Qt

    f = o.windowFlags()
    chk("无边框", bool(f & Qt.FramelessWindowHint))
    chk("置顶", bool(f & Qt.WindowStaysOnTopHint))
    chk("不抢焦点", bool(f & Qt.WindowDoesNotAcceptFocus))
    chk("Tool 窗口（不进任务栏）", bool(f & Qt.Tool))
    chk("未开启时不带穿透", not bool(f & Qt.WindowTransparentForInput))

    print("\n--- 字幕写入与渲染 ---")
    o.push_translation(
        "去抓星门", original="Tackle the gate",
        is_partial=False, sequence=1, language="en",
    )
    o._drain_queue()  # 直接调用，跳过信号排队（offscreen 下事件循环没跑）
    chk("条目已进入 store", len(o.store.items) == 1)
    chk("译文正确", o.store.items[0].translation == "去抓星门")
    chk("原文正确", o.store.items[0].original == "Tackle the gate")

    h_before = o.height()
    for i in range(8):
        o.push_translation(f"第 {i} 句中文译文内容", is_partial=False)
    o._drain_queue()
    chk("批量写入后条目数正确", len(o.store.items) == 9)
    chk("窗口高度随内容增长", o.height() > h_before,
        f"{h_before} -> {o.height()}")
    chk("高度不超过 max_height", o.height() <= cfg.overlay.max_height,
        f"实际 {o.height()}")

    print("\n--- 增量 → 定稿 完整生命周期 ---")
    before = len(o.store.items)
    o.push_translation("奶妈", is_partial=True, sequence=100)
    o._drain_queue()
    o.push_translation("奶妈把电容", is_partial=True, sequence=100)
    o._drain_queue()
    chk("两条增量合并为一条", len(o.store.items) == before + 1)
    o.push_translation("奶妈把电容保持住", is_partial=False, sequence=100)
    o._drain_queue()
    chk("定稿不新增条目", len(o.store.items) == before + 1)
    chk("内容为最终版",
        o.store.items[-1].translation == "奶妈把电容保持住")
    chk("已标记定稿", o.store.items[-1].finalized)

    print("\n--- 双语配对：复刻 app.py 的真实事件路由（离线复现）---")
    # 这里完全按 app.py._on_translation 的分流来推：
    #   kind=="original"    → push_original(ev.original, ...)
    #   kind=="translation" → push_translation(ev.translation, ...)
    # 事件顺序取自 2026-09-28 抓的真实 livetranslate 事件流：
    #   original(seq=N) → translation(seq=N+1)
    # 修复前：原文会错挂到**上一句**的译文上（e2e 断言"配对 0 条"）。
    ob = OverlayWindow(cfg.overlay)
    stream = [
        ("original", 3, "All right. Fleet.", ""),
        ("translation", 4, "", "好的。舰队。"),
        ("original", 7, "Listen. listen up.", ""),
        ("translation", 8, "", "听着。注意听。"),
        ("original", 11, "Primary is the Falcon.", ""),
        ("translation", 12, "", "先打的是猎鹰号。"),
    ]
    for kind, seq, en, zh in stream:
        if kind == "original":
            ob.push_original(en, is_partial=False, sequence=seq, language="en")
        else:
            ob.push_translation(zh, is_partial=False, sequence=seq)
        ob._drain_queue()

    pairs = [(i.original, i.translation) for i in ob.store.items
             if i.translation and i.original]
    chk("三句全部配上原文", len(pairs) == 3, f"实际 {len(pairs)} 条: {pairs}")
    chk("第 1 句配对正确", pairs[:1] == [("All right. Fleet.", "好的。舰队。")],
        f"实际 {pairs[:1]}")
    chk("第 2 句配对正确",
        pairs[1:2] == [("Listen. listen up.", "听着。注意听。")],
        f"实际 {pairs[1:2]}")
    chk("第 3 句配对正确",
        pairs[2:3] == [("Primary is the Falcon.", "先打的是猎鹰号。")],
        f"实际 {pairs[2:3]}")
    chk("没有孤立条目", len(ob.store.items) == 3,
        f"实际 {len(ob.store.items)} 条")
    ob.close()

    print("\n--- 布局一致性（防文字重叠）---")
    # 这是一个真实踩过的坑：
    #   _content_height / _paint_items / _paint_item 三处各算一遍高度，
    #   指标行被某处漏算 → 译文和原文重叠。
    #   现在统一走 _item_metrics()，本测试锁死这个约束。
    o2 = OverlayWindow(cfg.overlay)
    o2.push_translation("奶妈把电容保持住", original="Logi keep the cap up",
                        is_partial=False, first_text_delay_ms=145.0,
                        emotion="angry", language="en")
    o2.push_translation("全体对齐星门", original="Everyone align to the outgate",
                        is_partial=False, first_text_delay_ms=121.0)
    o2._drain_queue()

    metrics = o2._item_metrics()
    chk("_item_metrics 条目数与 store 一致",
        len(metrics) == len(o2.store.items))
    chk("每个 metric 都是 4 元组",
        all(len(m) == 4 for m in metrics))
    chk("带 alert 的条目有指标高度",
        metrics[0][3] > 0, f"实际 h_mark={metrics[0][3]}")
    chk("带延迟的条目有指标高度",
        metrics[1][3] > 0, f"实际 h_mark={metrics[1][3]}")

    total = sum(o2._item_total_height(m) for m in metrics)
    expect = total + 12 * 2   # pad*2
    chk("_content_height 与逐条累加一致",
        abs(o2._content_height() - expect) <= 1,
        f"content={o2._content_height()} 累加={expect}")

    # 没有 show_original 时不应有原文高度
    # ⚠️ 注意：OverlayWindow 持有的是**同一个**配置对象引用，
    #    直接改 cfg.show_original 会污染后续所有窗口。
    #    这里用 dataclasses.replace 造一份独立副本。
    import dataclasses

    zh_cfg = dataclasses.replace(cfg.overlay, show_original=False)
    zh_only = OverlayWindow(zh_cfg)
    zh_only.push_translation("只有译文", is_partial=False)
    zh_only._drain_queue()
    m = zh_only._item_metrics()[0]
    chk("关闭原文显示时 h_en=0", m[2] == 0, f"实际 {m[2]}")
    zh_only.close()

    chk("原配置未被测试污染", cfg.overlay.show_original is True)

    # 只有原文的条目（没有译文）不应崩
    orig_only = OverlayWindow(cfg.overlay)
    orig_only.push_original("Only original text", is_partial=False)
    orig_only._drain_queue()
    m = orig_only._item_metrics()[0]
    chk("纯原文条目 h_zh=0", m[1] == 0)
    chk("纯原文条目 h_en>0", m[2] > 0)
    orig_only.close()

    o2.close()

    print("\n--- 绘制不崩 ---")
    try:
        from PySide6.QtGui import QPixmap

        pm = QPixmap(o.width(), o.height())
        o.render(pm)
        chk("render() 成功（paintEvent 无异常）", True)
    except Exception as e:
        chk("render() 成功（paintEvent 无异常）", False, str(e))

    print("\n--- 错误与状态提示 ---")
    n = len(o.store.items)
    o.push_error("测试错误")
    o._drain_queue()
    chk("错误条目已加入", len(o.store.items) == n + 1)
    chk("错误文本正确",
        "[错误]" in o.store.items[-1].translation)
    o.push_status("正在连接…")
    o._drain_queue()
    chk("状态条目 emotion=status",
        o.store.items[-1].emotion == "status")

    print("\n--- 不透明度调节 ---")
    o.set_opacity(0.5)
    chk("set_opacity 生效", abs(o.opacity - 0.5) < 1e-6)
    o.adjust_opacity(+0.05)
    chk("上调生效", abs(o.opacity - 0.55) < 1e-6)
    o.set_opacity(0.01)
    chk("下限钳制到 0.05", abs(o.opacity - 0.05) < 1e-6)
    o.set_opacity(5.0)
    chk("上限钳制到 1.0", abs(o.opacity - 1.0) < 1e-6)

    print("\n--- 点击穿透切换 ---")
    o.set_opacity(0.85)
    init = o.click_through
    r1 = o.toggle_click_through()
    chk("切换后状态取反", r1 == (not init))
    chk("flags 含 WindowTransparentForInput",
        bool(o.windowFlags() & Qt.WindowTransparentForInput))
    o.toggle_click_through()
    chk("再切换恢复原状态", o.click_through == init)
    chk("flags 已移除穿透",
        not bool(o.windowFlags() & Qt.WindowTransparentForInput))

    print("\n--- 显隐切换 ---")
    o.show()
    v = o.toggle_visibility()
    chk("toggle 后为隐藏", v is False)
    v = o.toggle_visibility()
    chk("再 toggle 后为显示", v is True)

    print("\n--- 位置重算 ---")
    o.refresh_position()
    chk("refresh_position 不崩", True)

    print("\n--- 清空 ---")
    o.clear_items()
    chk("清空后无条目", not o.store.items)

    print("\n--- 定时 tick 不崩 ---")
    o.push_translation("测试 tick", is_partial=True)
    o._drain_queue()
    try:
        o._tick()
        chk("_tick() 正常执行", True)
    except Exception as e:
        chk("_tick() 正常执行", False, str(e))

    o.close()
    o.deleteLater()
    app.processEvents()

    print("\n--- 热键管理器（仅构造，不真注册）---")
    try:
        from src.ui.hotkeys import HotkeyManager

        mgr = HotkeyManager(cfg.hotkeys)
        mgr.bind("quit", lambda: None)
        chk("热键管理器构造成功", True)
        chk("热键字符串可读",
            mgr.combo("quit") == cfg.hotkeys.quit,
            mgr.combo("quit"))
        chk("未 start 时 is_active=False", not mgr.is_active)
        mgr.stop()  # 未启动时 stop 应安全
        chk("未启动时 stop 安全", True)
    except Exception as e:
        chk("热键管理器构造成功", False, str(e))


# ================================================================ B2. 滚动
def test_overlay_scroll() -> None:
    """滚轮翻历史 + 跟随尾部（B 部分的补充）。

    覆盖：
      · 布局量自洽（heights 累加 / max_scroll / inner_h）
      · _anchor_at 条目定位
      · 上滚 → 停止跟随并锚定；滚回底部 → 恢复跟随
      · 上滚期间来新消息，阅读位置不跑（锚点法的核心价值）
      · 无溢出时滚轮被忽略
      · auto_scroll=false → 首次溢出即冻结视口
    """
    print()
    print("=" * 70)
    print("  B2. 悬浮窗滚动 / 跟随尾部")
    print("=" * 70)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QPixmap, QWheelEvent
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        chk("PySide6 可用", False, str(e))
        return

    from src.ui.overlay import _SCROLL_EPS, _WHEEL_LINE_PX, OverlayWindow

    cfg = load_config(str(ROOT / "config.yaml"))
    app = QApplication.instance() or QApplication(sys.argv)

    def _wheel(win: OverlayWindow, notches: int) -> QWheelEvent:
        """造一个滚轮事件并派发（notches>0 上滚翻历史，<0 下滚回最新）。"""
        cx, cy = win.width() / 2.0, win.height() / 2.0
        ev = QWheelEvent(
            QPointF(cx, cy), QPointF(cx, cy),
            QPoint(0, 0), QPoint(0, 120 * notches),
            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
        )
        win.wheelEvent(ev)
        return ev

    # ---- 构造溢出场景：推到 max_height 封顶 ----
    o = OverlayWindow(cfg.overlay)
    for i in range(24):
        o.push_translation(f"第 {i} 句舰队指挥测试文本内容",
                           original=f"line {i} test", is_partial=False)
    o._drain_queue()
    chk("窗口被顶到 max_height", o.height() == cfg.overlay.max_height,
        f"实际 {o.height()}")

    metrics, heights, content_h, inner_h, max_scroll = o._layout_state()
    chk("heights 与 metrics 等长", len(heights) == len(metrics))
    chk("heights 累加 == content_h", sum(heights) == content_h)
    chk("inner_h == height - 2*pad", inner_h == o.height() - 24,
        f"inner_h={inner_h} height={o.height()}")
    chk("max_scroll == content_h - inner_h", max_scroll == content_h - inner_h)
    chk("内容溢出，可滚动", max_scroll > _WHEEL_LINE_PX + _SCROLL_EPS,
        f"max_scroll={max_scroll}")

    # ---- 默认跟随尾部 ----
    chk("默认 _follow_tail=True", o._follow_tail is True)
    chk("跟随尾部时视口钉在底部",
        o._resolve_scroll_top(metrics, heights, max_scroll) == float(max_scroll))

    # ---- _anchor_at 条目定位 ----
    it0, dy0 = OverlayWindow._anchor_at(metrics, heights, 0.0)
    chk("scroll_top=0 → 首条", it0 is metrics[0][0])
    chk("scroll_top=0 → 偏移 0", dy0 == 0.0)

    off2 = float(sum(heights[:2]) + 5)
    it2, dy2 = OverlayWindow._anchor_at(metrics, heights, off2)
    chk("落在第 3 条内部 → 命中第 3 条", it2 is metrics[2][0])
    chk("条目内偏移 == 5", abs(dy2 - 5.0) < 1e-6, f"实际 {dy2}")

    it_last, _ = OverlayWindow._anchor_at(metrics, heights, float(content_h + 999))
    chk("超出全部 → 锚定最后一条", it_last is metrics[-1][0])

    # ---- 上滚一格：停止跟随 + 锚点 ----
    before_top = o._resolve_scroll_top(metrics, heights, max_scroll)
    ev = _wheel(o, +1)
    chk("上滚后 _follow_tail=False", o._follow_tail is False)
    chk("上滚后设了锚点", o._anchor_item is not None)
    chk("上滚事件被 accept", ev.isAccepted())
    after_top = o._resolve_scroll_top(metrics, heights, max_scroll)
    chk("上滚后视口上移", after_top < before_top,
        f"{before_top} -> {after_top}")
    chk("上滚一格位移 == _WHEEL_LINE_PX",
        abs((before_top - after_top) - _WHEEL_LINE_PX) < 1e-6,
        f"实际位移 {before_top - after_top}")

    # ---- 绘制后统计"下方还有内容" ----
    o.render(QPixmap(o.width(), o.height()))
    chk("上滚后 _has_more_below=True", o._has_more_below is True)
    chk("上滚后 _below_count>0", o._below_count > 0, f"实际 {o._below_count}")

    # ---- 上滚期间来新消息：阅读位置不跑 ----
    anchor_before = o._anchor_item
    top_before = o._resolve_scroll_top(metrics, heights, max_scroll)
    o.push_translation("新到达的指挥消息", original="new cmd", is_partial=False)
    o._drain_queue()
    m2, h2, _c2, _i2, ms2 = o._layout_state()
    top_after = o._resolve_scroll_top(m2, h2, ms2)
    chk("上滚期间来新消息，阅读位置不动",
        abs(top_after - top_before) < 1e-6, f"{top_before} -> {top_after}")
    chk("新消息不改变锚点对象", o._anchor_item is anchor_before)

    # ---- 连续下滚回到底部：恢复跟随 ----
    for _ in range(80):
        _wheel(o, -1)
    chk("滚回底部恢复 _follow_tail", o._follow_tail is True)
    chk("滚回底部清空锚点", o._anchor_item is None)
    m3, h3, _c3, _i3, ms3 = o._layout_state()
    chk("滚回底部视口在底部",
        o._resolve_scroll_top(m3, h3, ms3) == float(ms3))
    o.render(QPixmap(o.width(), o.height()))
    chk("在底部时 _has_more_below=False", o._has_more_below is False)

    # ---- reset_scroll ----
    o._follow_tail = False
    o._anchor_item = metrics[0][0]
    o._anchor_dy = 7.0
    o.reset_scroll()
    chk("reset_scroll 恢复跟随", o._follow_tail is True)
    chk("reset_scroll 清空锚点",
        o._anchor_item is None and o._anchor_dy == 0.0)
    o.close()

    # ---- 无溢出：滚轮应被忽略 ----
    o2 = OverlayWindow(cfg.overlay)
    o2.push_translation("只有一条", is_partial=False)
    o2._drain_queue()
    _m, _h, _c, _i, ms = o2._layout_state()
    chk("单条内容不溢出", ms == 0, f"max_scroll={ms}")
    ev2 = _wheel(o2, +1)
    chk("无溢出时滚轮被 ignore", ev2.isAccepted() is False)
    chk("无溢出时仍跟随尾部", o2._follow_tail is True)
    o2.close()

    # ---- auto_scroll=false：首次溢出即冻结视口 ----
    import dataclasses

    cfg_no = dataclasses.replace(cfg.overlay, auto_scroll=False)
    o3 = OverlayWindow(cfg_no)
    for i in range(24):
        o3.push_translation(f"不自动滚动 {i}", is_partial=False)
    o3._drain_queue()
    o3.render(QPixmap(o3.width(), o3.height()))
    chk("auto_scroll=False 首次溢出即冻结", o3._follow_tail is False)
    chk("auto_scroll=False 冻结时设了锚点", o3._anchor_item is not None)

    anchor3 = o3._anchor_item
    m4, h4, _c4, _i4, ms4 = o3._layout_state()
    top4 = o3._resolve_scroll_top(m4, h4, ms4)
    o3.push_translation("又来一条", is_partial=False)
    o3._drain_queue()
    m5, h5, _c5, _i5, ms5 = o3._layout_state()
    top5 = o3._resolve_scroll_top(m5, h5, ms5)
    chk("auto_scroll=False 新消息不移动视口",
        abs(top5 - top4) < 1e-6, f"{top4} -> {top5}")
    chk("auto_scroll=False 锚点对象不变", o3._anchor_item is anchor3)
    o3.close()

    chk("原配置未被滚动测试污染", cfg.overlay.auto_scroll is True)


# ================================================================ C. 配置校验
def test_config_validation() -> None:
    print()
    print("=" * 70)
    print("  C. 配置校验（新增项）")
    print("=" * 70)

    from src.config import Config, _is_valid_color
    from src.errors import ConfigError

    chk("#RRGGBB 合法", _is_valid_color("#FFFFFF"))
    chk("#AARRGGBB 合法", _is_valid_color("#E6101216"))
    chk("#RGB 合法", _is_valid_color("#fff"))
    chk("无 # 非法", not _is_valid_color("FFFFFF"))
    chk("非法字符", not _is_valid_color("#GGGGGG"))
    chk("长度错误", not _is_valid_color("#FFFF"))
    chk("非字符串", not _is_valid_color(123))

    # 逐项构造非法配置，确认会被拦下
    from src.config import _validate

    def expect_err(mutate, desc: str) -> None:
        c = Config()
        c.asr.provider = "cloud"
        c.vad.enabled = True
        mutate(c)
        try:
            _validate(c)
            chk(desc, False, "未抛出 ConfigError")
        except ConfigError:
            chk(desc, True)

    expect_err(lambda c: setattr(c.overlay, "background", "红色"),
               "非法背景色被拦截")
    expect_err(lambda c: setattr(c.overlay, "color_translation", "white"),
               "非法译文颜色被拦截")
    expect_err(lambda c: setattr(c.overlay, "hold_seconds", -1),
               "负 hold_seconds 被拦截")
    expect_err(lambda c: setattr(c.overlay, "max_messages", 0),
               "max_messages=0 被拦截")
    expect_err(lambda c: setattr(c.overlay, "font_size_translation", 200),
               "字号过大被拦截")

    # 最后一条应当通过
    c = Config()
    c.asr.provider = "cloud"
    c.vad.enabled = True
    try:
        _validate(c)
        chk("默认 overlay 配置可通过校验", True)
    except ConfigError as e:
        chk("默认 overlay 配置可通过校验", False, str(e))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logic-only", action="store_true",
                    help="只跑纯逻辑测试，不加载 Qt")
    args = ap.parse_args()

    print()
    test_subtitle_store()
    if not args.logic_only:
        test_overlay_qt()
        test_overlay_scroll()
    test_config_validation()

    print()
    print("=" * 70)
    print(f"  结果: {_OK} 通过, {_FAIL} 失败")
    print("=" * 70)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
