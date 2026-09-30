"""字幕消息模型：把翻译事件整理成可上屏的条目。

职责边界
--------
本模块**只管数据**，不碰 Qt。这样：
  · 逻辑可以脱离 GUI 单元测试
  · 未来换 UI 框架（如 tkinter / Web）也能复用

核心问题是**增量合并**：云端流式翻译会先推
`response.text.text`（增量，内容会变），最后推
`response.text.done`（定稿）。UI 上它们应该是**同一条**
消息在其生命周期内不断被改写，而不是刷屏出一堆半句。

合并策略
--------
维护一个"活动条目"（active item）：
  · 收到增量 → 若已有活动条目则更新文本，否则新建
  · 收到定稿 → 用定稿文本收敛活动条目，然后**关闭**它
    下一条翻译会创建新条目

边界情况处理：
  · 只有增量、迟迟没有定稿 → 靠 `finalize_stale()` 按超时收敛
  · 定稿来了但活动条目已被超时收敛 → 视为新条目（不丢内容）
  · 原文（英文）先到、译文后到 → 原文条目先挂起（`_pending_original`），
    等紧随其后的译文条目创建时**认领**它，合成一条双语字幕

⚠️ 原文/译文的到达顺序（实测，2026-09-28）
--------------------------------------------
Qwen3.5-LiveTranslate 的真实事件序是：

    original(seq=N) → translation(seq=N+1)      ← 原文定稿在前，译文定稿在后

两者序号**相差 1**（pipeline 对每个事件都自增序号），所以序号无法用来
跨事件配对。旧版靠"找最近一条还没有原文的条目"兜底，会把新原文错挂到
**上一句**的译文上 —— 实测配对全错。现在改为"挂起 + 认领"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# 原文挂起等待译文认领的最长时间（秒）。
# 超过这个时间还没等到译文，就不再认领 —— 避免把很久以前孤立留下的
# 原文错当成当前这句的原文。正常流式翻译在 1s 内就会认领。
_PENDING_ORIGINAL_TTL_S = 6.0


@dataclass
class SubtitleItem:
    """一条字幕（一次"说话"的完整生命周期）。"""

    # 中文译文（会随增量更新）
    translation: str = ""
    # 英文原文
    original: str = ""
    # 创建时刻（monotonic）
    created_at: float = field(default_factory=time.monotonic)
    # 最后更新时刻
    updated_at: float = field(default_factory=time.monotonic)
    # 是否已定稿（定稿后不再被增量改写）
    finalized: bool = False
    # 序号（用于 UI 排序与去重）
    sequence: int = 0
    # 服务端首字延迟（ms）
    first_text_delay_ms: float | None = None
    # 识别的语言
    language: str = ""
    # 情绪（非 neutral 时 UI 会加提示标记）
    emotion: str = ""

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def idle_s(self) -> float:
        """距最后一次更新的秒数。"""
        return time.monotonic() - self.updated_at

    @property
    def is_alert(self) -> bool:
        """是否应高亮提示（指挥在喊话）。

        服务端情绪识别返回非 neutral 时认为需要提醒玩家注意。
        """
        return bool(self.emotion) and self.emotion.lower() not in (
            "neutral", "calm", "",
        )

    @property
    def text(self) -> str:
        return self.translation or self.original

    def matches(self, other_translation: str) -> bool:
        """判断新译文是否可能是本条的延续。

        云端增量有三种推送风格，都要能识别为同一条：
          · 纯累积：  "去抓" → "去抓星门"（前缀包含）
          · 尾部追加： "去抓" → "星门"（只推新增部分，是后缀）
          · 被修正：   "去抓星门" → "去抓星门！"（服务端微调标点/措辞）

        判定规则（任一成立即视为同一句）：
          1. 任意一方是另一方的前缀
          2. 任意一方是另一方的后缀
          3. 公共前缀长度 >= max(2, 较短方一半)
        """
        a, b = self.translation, other_translation
        if not a or not b:
            return True
        if a.startswith(b) or b.startswith(a):
            return True
        if a.endswith(b) or b.endswith(a):
            return True

        # 公共前缀足够长 → 视为同一句的修正
        n = 0
        for ca, cb in zip(a, b):
            if ca != cb:
                break
            n += 1
        shorter = min(len(a), len(b))
        return n >= max(2, shorter // 2)

    def should_merge_into_self(self, other: str, sequence: int = 0) -> bool:
        """是否应把新译文并入本条（含"已在说下一句"的判断）。

        与 matches 的区别：这里做**两重**判断。

        1. 序号判定（优先，最可靠）
           若两次增量的 `sequence` 相同，说明云端认定它们属于
           同一次说话 —— 即使文本看起来毫无关系（例如
           "去抓" → "星门" 这种只推新增片段的风格）也应合并。
           序号为 0 表示调用方没有提供序号，此时退化为文本判定。

        2. 文本判定（兜底）
           序号不同或缺失时，按文本相似度判断，避免把
           连续两个独立短句误合。

        3. 定稿后一律不合并
           定稿之后的任何新内容都应另起一条，即使文本完全相同
           —— 否则指挥重复喊同一句指令会被吞掉一次播报。
        """
        if self.finalized:
            return False
        if sequence and self.sequence and sequence == self.sequence:
            return True
        return self.matches(other)


class SubtitleStore:
    """字幕条目集合，负责合并增量、收敛超时、限制条数。"""

    def __init__(
        self,
        max_messages: int = 30,
        max_chars: int = 300,
        hold_seconds: float = 30.0,
    ) -> None:
        self.max_messages = max(1, max_messages)
        self.max_chars = max_chars
        self.hold_seconds = hold_seconds
        self.items: list[SubtitleItem] = []
        self._active: SubtitleItem | None = None   # 正在被增量改写的译文条目
        self._active_original: SubtitleItem | None = None
        # 原文先到、还在等译文认领的条目（见模块文档"到达顺序"一节）
        self._pending_original: SubtitleItem | None = None
        # 统计
        self.total_final = 0
        self.total_partial = 0
        self.merged_partials = 0

    # ---------------- 对外接口 ----------------
    def push(
        self,
        translation: str,
        *,
        original: str = "",
        is_partial: bool,
        sequence: int = 0,
        first_text_delay_ms: float | None = None,
        language: str = "",
        emotion: str = "",
    ) -> SubtitleItem:
        """写入一条翻译。返回受影响的条目。

        增量会尽量合并到已有条目，避免刷屏。

        `original` 非空时直接挂到刚写入的条目上（一次给全译文+原文）；
        overlay 走的是"先 push 译文、再 push_original"两条路径，效果相同。
        """
        translation = self._clip(translation)
        original = self._clip(original)

        if is_partial:
            item = self._push_partial(translation, sequence)
        else:
            item = self._push_final(
                translation,
                sequence=sequence,
                first_text_delay_ms=first_text_delay_ms,
                language=language,
                emotion=emotion,
            )

        if original:
            self._attach_original(
                item, original, is_partial=is_partial,
                language=language, emotion=emotion,
            )
        return item

    def push_original(
        self, original: str, *, is_partial: bool, sequence: int = 0,
        language: str = "", emotion: str = "",
    ) -> SubtitleItem:
        """写入一条原文（英文）。

        归属规则（按优先级）：
          1. 有条目的序号与 sequence 相同 → 挂到它
             （调用方一次把译文+原文推来的情况）
          2. 正挂起等待译文的原文条目 → 继续补全它（原文增量会分多次推）
          3. 有活动译文条目（译文正在流式输出）**且序号相同/未给序号**
             → 挂到它。序号不同就说明这条原文属于**下一句**
             （上一句的译文还在流式输出中），必须走第 4 条。
          4. 都不满足 → 原文独立成条，并记为**挂起**，
             等紧随其后的译文条目来认领（实测服务端就是这个顺序）

        ⚠️ 这里刻意**没有**"找最近一条还没有原文的条目"的兜底：
           它会把当前这句的原文错挂到上一句的译文上（实测配对全错）。
        """
        original = self._clip(original)

        target = self._find_original_target(sequence)

        if target is not None:
            self._attach_original(
                target, original, is_partial=is_partial,
                language=language, emotion=emotion,
            )
            return target

        # 没有可归属的译文条目 → 原文独立成条，并挂起等待译文认领
        item = SubtitleItem(
            original=original,
            sequence=sequence,
            language=language,
            emotion=emotion,
            finalized=not is_partial,
        )
        self.items.append(item)
        self._pending_original = item
        self._trim()
        return item

    @staticmethod
    def _attach_original(
        target: SubtitleItem, original: str, *, is_partial: bool,
        language: str = "", emotion: str = "",
    ) -> None:
        """把原文写进目标条目（增量直接覆盖，定稿只在更完整时覆盖）。"""
        if is_partial:
            # 增量原文直接覆盖（云端会逐步补全）
            target.original = original
        elif not target.original:
            # 定稿原文：只在目标还没有原文时写入，避免覆盖更完整的版本
            target.original = original
        elif len(original) > len(target.original):
            # 新版本更完整 → 采用
            target.original = original
        target.updated_at = time.monotonic()
        if language:
            target.language = language
        if emotion:
            target.emotion = emotion

    def _find_original_target(self, sequence: int) -> SubtitleItem | None:
        """为原文找归属条目。"""
        # 1) 序号精确匹配（同一次说话，调用方一次推来）
        if sequence:
            for it in reversed(self.items):
                if it.sequence == sequence:
                    return it

        # 2) 挂起中的原文条目（原文先到、正在等译文）
        p = self._pending_original
        if p is not None and p in self.items and not p.translation:
            return p

        # 3) 活动译文条目 —— 只认"同一次说话"（序号相同），或调用方没给序号。
        #    真实事件流里原文/译文各自占一个序号，序号**必然不同**，
        #    所以序号不同就说明这条原文属于**下一句**（上一句的译文还在
        #    流式输出中），必须挂起等它自己的译文，不能错挂到上一句。
        if self._active is not None:
            if not sequence or not self._active.sequence \
                    or sequence == self._active.sequence:
                return self._active

        return None

    def _take_pending_original(self, sequence: int = 0) -> SubtitleItem | None:
        """认领"原文先到、正在等译文"的条目。

        只在满足全部条件时认领：
          · 条目还在列表里、还没有译文
          · 没有过期（`_PENDING_ORIGINAL_TTL_S`）
          · **本条译文的序号大于原文的序号** —— 即"这条译文确实是在
            原文之后到达的"。上一句的译文迟到时序号反而更小，
            绝不能让它认领下一句的原文。
        """
        p = self._pending_original
        if p is None:
            return None
        self._pending_original = None
        if p not in self.items or p.translation:
            return None
        if p.idle_s > _PENDING_ORIGINAL_TTL_S:
            return None
        if sequence and p.sequence and sequence < p.sequence:
            return None
        return p

    def push_error(self, message: str) -> SubtitleItem:
        """写入一条错误提示（UI 用红色显示）。"""
        item = SubtitleItem(
            translation=f"[错误] {self._clip(message)}",
            finalized=True,
            emotion="error",
        )
        self.items.append(item)
        self._trim()
        return item

    def finalize_stale(self, timeout_s: float = 3.0) -> bool:
        """清理长时间未定稿的活动条目。

        云端偶尔会漏发定稿事件（网络抖动 / 短句被 VAD 吞掉）。
        这些未定稿条目只是**增量碎片**，不该当作正式字幕留在屏幕上
        —— 否则会出现"碎片 + 完整句"并存（实测问题）。

        因此这里的处理是**丢弃**而不是提升为定稿。

        Returns:
            是否发生了变化（UI 据此决定要不要重绘）
        """
        changed = False
        for attr in ("_active", "_active_original"):
            item = getattr(self, attr)
            if item is not None and not item.finalized and item.idle_s > timeout_s:
                self._drop_item(item)
                setattr(self, attr, None)
                changed = True
        return changed

    def expire_old(self) -> bool:
        """移除超过 hold_seconds 未更新的条目。

        未定稿条目同样会过期 —— 否则一个被遗弃的增量碎片
        会永远占着屏幕（它不会被 finalize_stale 之外的路径清掉）。
        """
        if self.hold_seconds <= 0:
            return False
        now = time.monotonic()
        before = len(self.items)
        self.items = [
            it for it in self.items
            if (now - it.updated_at) < self.hold_seconds
        ]
        # 活动引用若已被移除，需要一并清空
        if self._active is not None and self._active not in self.items:
            self._active = None
        if (self._active_original is not None
                and self._active_original not in self.items):
            self._active_original = None
        if (self._pending_original is not None
                and self._pending_original not in self.items):
            self._pending_original = None
        return len(self.items) != before

    def clear(self) -> None:
        self.items.clear()
        self._active = None
        self._active_original = None
        self._pending_original = None

    # ---------------- 内部 ----------------
    def _push_partial(self, translation: str, sequence: int) -> SubtitleItem:
        self.total_partial += 1

        # 1) 优先合并到活动条目 —— 同一句的后续增量。
        #    ⚠️ 必须排在"认领挂起原文"之前：上一句的译文如果还在流式输出，
        #    此时到达的原文其实属于**下一句**，绝不能被这条译文认领。
        if self._active is not None and self._active.should_merge_into_self(
            translation, sequence
        ):
            self._active.translation = self._merge_text(
                self._active.translation, translation
            )
            self._active.updated_at = time.monotonic()
            self._active.sequence = sequence or self._active.sequence
            self.merged_partials += 1
            return self._active

        # 2) 无法合并 → 说明上一轮结束了（定稿事件可能漏发）。
        #
        # ⚠️ 关键：这里的旧活动条目**不能当作正式内容留在屏幕上**。
        #    它只是一个未完成的增量碎片（如 "先打是先打的是先打的是猎"），
        #    如果保留，屏幕上会出现"碎片 + 完整句"两条，非常难看。
        #    实测正是这个问题（2026-09-25 端到端测试暴露）。
        #    处理：把它丢弃（从 items 里摘掉），让位给新条目。
        if self._active is not None:
            self._drop_item(self._active)
            self._active = None

        # 3) 有挂起的原文（原文先到、译文后到）→ 这条译文正是它的译文
        pend = self._take_pending_original(sequence)
        if pend is not None:
            pend.translation = self._merge_text(pend.translation, translation)
            pend.finalized = False
            pend.updated_at = time.monotonic()
            if sequence:
                pend.sequence = sequence
            self._active = pend
            return pend

        # 4) 全新的一条
        item = SubtitleItem(
            translation=translation,
            sequence=sequence,
            finalized=False,
        )
        self.items.append(item)
        self._active = item
        self._trim()
        return item

    def _drop_item(self, item: SubtitleItem) -> None:
        """把条目从列表中摘掉（用于丢弃未完成的增量碎片）。"""
        try:
            self.items.remove(item)
        except ValueError:
            pass

    @staticmethod
    def _merge_text(old: str, new: str) -> str:
        """把新增量合并进已有文本。

        云端增量主要有两种风格：
          · **累积式**（实测 Qwen 是这种）：每次都推完整前缀
            `"先打"` → `"先打的是"` → `"先打的是猎鹰"`
            → 后推的更长，**直接用新的覆盖**即可
          · **纯新增片段**：`"去抓"` → `"星门"`
            → 后推的是新片段，需要追加

        区分方法看长度与包含关系，而不是无脑追加 ——
        无脑追加会拼出 `"先打是先打的是先打的是猎"` 这种垃圾
        （实测确实发生了）。
        """
        if not old:
            return new
        if not new:
            return old

        # 累积式：新文本包含了旧文本 → 直接换新（最干净）
        if old in new:
            return new
        # 回退：旧文本包含新文本 → 保留更长的旧文本
        if new in old:
            return old

        # 两者是前缀关系但都不是对方子串（几乎不会发生）
        if old.startswith(new) or new.startswith(old):
            return new if len(new) > len(old) else old

        # 尾部重复（如 "星门" 又推了一遍）→ 不重复拼
        if old.endswith(new):
            return old

        # 真正的纯新增片段 → 追加
        return old + new

    def _push_final(
        self,
        translation: str,
        *,
        sequence: int,
        first_text_delay_ms: float | None,
        language: str,
        emotion: str,
    ) -> SubtitleItem:
        self.total_final += 1

        # 1) 先试着收敛到活动条目（同一句的"增量 → 定稿"）。
        #    ⚠️ 必须排在"认领挂起原文"之前，理由同 _push_partial：
        #    上一句的译文定稿迟到时，不能去认领下一句的原文。
        target = self._active
        if target is not None and not target.finalized:
            same_turn = (
                sequence and target.sequence and sequence == target.sequence
            )
            if same_turn or target.matches(translation):
                target.translation = translation
                target.finalized = True
                target.updated_at = time.monotonic()
                target.first_text_delay_ms = first_text_delay_ms
                if sequence:
                    target.sequence = sequence
                if language:
                    target.language = language
                if emotion:
                    target.emotion = emotion
                self._active = None
                return target
            # 内容差异过大 → 关闭旧的，新建
            target.finalized = True

        # 2) 原文先到并已挂起 → 定稿收敛到同一条
        #    （实测服务端的顺序就是"原文定稿 → 译文定稿"）
        pend = self._take_pending_original(sequence)
        if pend is not None:
            pend.translation = translation
            pend.finalized = True
            pend.updated_at = time.monotonic()
            if sequence:
                pend.sequence = sequence
            pend.first_text_delay_ms = first_text_delay_ms
            if language:
                pend.language = language
            if emotion:
                pend.emotion = emotion
            self._active = None
            return pend

        item = SubtitleItem(
            translation=translation,
            sequence=sequence,
            finalized=True,
            first_text_delay_ms=first_text_delay_ms,
            language=language,
            emotion=emotion,
        )
        self.items.append(item)
        self._active = None
        self._trim()
        return item

    def _clip(self, s: str) -> str:
        if self.max_chars > 0 and len(s) > self.max_chars:
            return s[: self.max_chars] + "…"
        return s

    def _trim(self) -> None:
        """限制条目总数（从头丢最旧的）。"""
        overflow = len(self.items) - self.max_messages
        if overflow <= 0:
            return
        # 保留活动条目的引用有效性
        dropped = self.items[:overflow]
        self.items = self.items[overflow:]
        if self._active in dropped:
            self._active = None
        if self._active_original in dropped:
            self._active_original = None
        if self._pending_original in dropped:
            self._pending_original = None
