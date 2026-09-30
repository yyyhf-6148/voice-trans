#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 EVE SDE 的 types.jsonl 生成「舰队语音实时翻译」术语热词表。

===========================================================================
产出
===========================================================================
  terms.yaml   —— 热词表（key: "中文"），默认输出到**项目根**
  该文件是「服务端 ASR 字面文本 → 强制中文译法」的映射，
  由 config.yaml 的 `asr.livetranslate.phrases_file: terms.yaml` 引用，
  程序启动时加载；与 config.yaml 里的内联 `phrases:` 合并时**内联优先**。

===========================================================================
背景：热词表到底在干什么
===========================================================================
链路是   Mumble 舰队语音 → 云端端到端语音翻译
        (dashscope qwen3.5-livetranslate-flash-realtime)
        → 中文文本 → 悬浮窗。

模型的 translation_params.corpus.phrases 是一个 dict[str, str]，
语义是「强制术语译法」：不加表时 gate 可能被译成"大门"，
加了 gate: "星门" 就会被译成"星门"。舰队指挥场景里这是致命差异。

热词表在 WebSocket 建连时用 update_session() 一次性下发，整个会话只发一次
（见 src/asr/live_translate.py 的 start()，约 200-230 行）。

===========================================================================
筛选策略（本文档即策略说明）
===========================================================================
关键前提，全部来自实测/源码结论：

P1. 热词表匹配的是**服务端 ASR 输出的字面文本**，不是"语义"。
    拼写差一个字母就失效。实测 ASR 会把 Logi 听成 Logie、Cyno 听成 Sino。

P2. 舰队语音里说的是**短名**。人们说 "Loki" / "Sabre" / "Keepstar"，
    **不会**说 "Large Shield Extender II"。
    => 键必须尽量等于"人真的会念出来的那个字符串"。

P3. 因此 SDE 的物品**完整型号全名**（Large Shield Extender II、
    Domination EM Ward Field 之类）灌进去几乎永不命中，属于纯占位。
    本脚本据此**主动丢弃**大部分型号全名，只保留：
      - 本身就是短名的（舰船 / 建筑 / 无人机基名）
      - 核心战斗模块的**基名**：把 T1/T2 型号全名剥掉"尺寸前缀 + 等级后缀"
        后得到的基名，例如
          Small/Medium/Large/Capital Armor Repairer I/II  ->  armor repairer
          Small/Medium/Large/X-Large Shield Booster I/II  ->  shield booster
          Stasis Webifier I/II                            ->  stasis webifier
        因为舰队语音念的正是这个基名（"armor repairer"、"shield booster"、
        "stasis webifier"），而不是带尺寸/等级的全名。
        ⚠️ 早期版本保留了 160 条"尺寸+等级全名"，那是永不命中的死重，
           本版已全部替换为基名（详见报告）。

P3b. 有意**不**收录弹药（ammo/charge）。
     Void/Null/Rage/Fury/Precision/Spike/Iron/Lead/Fusion/Nova/Inferno/
     Standard/Radio/Microwave… 几乎全是常用英语词，在非术语语境被强制
     替换的副作用远大于收益（例如 "standard"、"iron"）。这是**有意的取舍**，
     不是遗漏，理由已写入报告「有意取舍」小节。

P3c. 有意**不**收录舰队指挥用语/黑话（reps / anchor / blops / primary target
     之外的临场口令等）。这类不在 SDE 里，属下一阶段的独立工作，
     留待后续单独评估。

P4. **副作用风险**：SDE 含大量普通英语词（Point/Gate/Jam/Scan/Fit/Cap/
    Hull/Structure/Bubble，以及舰船名里的 Eagle/Hawk/Raven/Guardian/
    Scorpion/Widow/Sin/Hel/Avatar/Drake/Vulture/Wasp/Ogre/Berserker）。
    这些词在非术语语境下会被**强制替换**，反而破坏译文。
    本脚本对这类条目"保留但在报告里逐条列出"，交人工复核；
    仅丢弃「EVE 语义明显不占主导」的极少数（见 DROP_AS_COMMON_WORD）。

P5. phrases 是 dict[str,str]，同名多义会**后写覆盖先写** = 随机结果。
    本脚本用"类别优先级 + 稳定排序"做确定性消解，并把冲突全量记录。

P6. 硬上限 1000 条（src/config.py 第 559 行校验，超了直接 ConfigError）。
    目标区间 620~660。

P7. 键的大小写：服务端行为未知。dashscope SDK 1.27.7 的
    dashscope/audio/qwen_omni/omni_realtime.py 在构造 session.update 时
    **对 corpus.phrases 不做任何大小写归一化**，直接把 dict 原样
    json.dumps 后发出（见该文件 504-515 行与 525-533 行）。
    也就是说大小写是否敏感**完全由服务端决定，客户端无从得知**。
    现有手写表几乎全是小写键（只有 MWD/AB 大写），故本脚本默认
    **统一转小写**（--case lower），与现有表保持一致；
    另提供 --case preserve 用于实测对照。

===========================================================================
类目白名单（全部经数据核对，非照抄）
===========================================================================
SHIP_GROUP_IDS       —— 舰船。原任务给的候选表里有 1404/1406，
                        经核对这两个是**昇威建筑**（工程复合体 / 精炼厂），
                        不是舰船，已移入结构类。
                        另：任务书说 shipTreeGroupID 是"识别舰船的强信号"，
                        实测**不成立**——该字段在 934 条记录上出现，
                        但 groupID=314 那 90 条全是补给箱/EGDLI 道具，
                        groupID=306 那 71 条全是任务设施。故本脚本
                        **不使用** shipTreeGroupID，改用 groupID 白名单。
STRUCTURE_GROUP_IDS  —— 昇威堡垒/工程复合体/精炼厂、诱导信标/干扰塔、
                        移动式仓库/牵引装置/诱导信标、卫星钻探、雇佣兵巢穴。
DRONE_GROUP_IDS      —— 战斗/后勤/采矿/电子战无人机 + 铁骑舰载机。
MODULE_GROUP_IDS     —— 核心战斗模块的 T1/T2 基型（见 P3）。

===========================================================================
丢弃规则（每条都写清理由）
===========================================================================
D1 published != true           —— 未发布/测试/内部物品，玩家永远不会念。
D2 名称含 Blueprint          —— 蓝图副本，任务明确要求丢弃。
D3 名称含 Test/GFX/Polaris/
   DO NOT TRANSLATE          —— 开发测试残留。
D4 名称含 " Edition"         —— 周年/联名/涂装版舰船（YC117 特别版、
                                Quafe 版、InterBus 版…），玩家不念全名。
D5 名称非 ASCII              —— 少数条目 en 字段直接是中文（如
                                "毒蜥级YC117年特别版"），无对照价值。
D6 词数 > 上限               —— 舰船 >3 词、结构 >4 词。超过就是型号全名，
                                按 P3 属于纯占位。'Draccous' Fortizar 这类
                                玩家自命名建筑也在此列。
D7 名称以引号/单引号开头      —— 玩家自命名/品牌前缀（'Wetu' Mobile Depot）。
D8 模块名非 T1/T2 基型        —— 只留 "... I"/"... II" 且不含品牌修饰词；
                                Enduring/Compact/Scoped/Restrained/Abyssal/
                                C-Type/舰队海军/官员 等全丢（口语里不念全名）。
D9 DROP_AS_COMMON_WORD      —— 极少数"作为普通英语词被说出来的概率
                                远高于 EVE 语义"的条目，直接丢弃。
D10 同名多义                 —— 同 key 不同中文，按优先级取第一个，
                                其余记入报告。

===========================================================================
可复现性
===========================================================================
- 只用标准库；逐行流式读 146MB 文件，不整体 load。
- 排序稳定（key 排序 + typeID 兜底），重复运行结果完全一致。
- 手写 50 条最高优先级，原样输出，绝不被 SDE 覆盖。

用法：
    python build_phrases.py                 # 默认输出 terms.yaml（项目根），小写键
    python build_phrases.py --out X.yaml    # 指定输出路径
    python build_phrases.py --case preserve # 保留 SDE 原始大小写
    python build_phrases.py --stats-json X  # 额外导出统计 JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SDE_PATH = PROJECT_ROOT / "types.jsonl"
OUT_DIR = PROJECT_ROOT / ".workbuddy" / "_tmp_spike"
# 默认输出就是项目根的 terms.yaml —— 由 config.yaml 的
# `asr.livetranslate.phrases_file: terms.yaml` 引用，程序启动时加载。
OUT_YAML = PROJECT_ROOT / "terms.yaml"

# ---------------------------------------------------------------------------
# A0 · 手写表（50 条）—— 最高优先级，原样保留
# ---------------------------------------------------------------------------
# 约束：不得覆盖、丢弃或改写译法。SDE 里若出现同名键，一律让位给这里。
HANDWRITTEN: list[tuple[str, str]] = [
    ("gate", "星门"),
    ("gates", "星门"),
    ("tackle", "抓人"),
    ("tackled", "被抓住了"),
    ("tackleing", "正在抓人"),
    ("tackling", "正在抓人"),      # 修正拼写：tackleing 是既有手写表的笔误，原条目保留不动，这里补正确拼写
    ("tackleing_enemy", "正在抓人"),
    ("warp", "跃迁"),
    ("warping", "跃迁中"),
    ("warped", "已跃迁"),
    ("aligned", "已对齐"),
    ("align", "对齐"),
    ("cap", "电容"),
    ("capstable", "电容稳定"),
    ("neut", "毁电"),
    ("neuts", "毁电"),
    ("neuting", "正在毁电"),
    ("neuted", "被毁电"),
    ("logi", "后勤"),
    ("logie", "后勤"),
    ("logis", "后勤"),
    ("logistics", "后勤"),
    ("scout", "侦察"),
    ("scouts", "侦察"),
    ("cyno", "诱导"),
    ("sino", "诱导"),
    ("cynos", "诱导"),
    ("sinos", "诱导"),
    ("MWD", "微曲"),
    ("AB", "加力"),
    ("bubbles", "泡泡"),
    ("bubble", "泡泡"),
    ("grid", "网格"),
    ("ongrid", "在网格上"),
    ("offgrid", "不在网格"),
    ("broadcast", "广播点"),
    ("broadcasted", "已广播"),
    ("hold", "别动"),
    ("holding", "保持不动"),
    ("jump", "过门"),
    ("jumped", "已过门"),
    ("jumping", "正在过门"),
    ("point", "点住"),
    ("pointed", "被点住"),
    ("web", "网住"),
    ("webbed", "被网住"),
    ("primaried", "先打"),
    ("primary", "先打"),
    ("reload", "装弹"),
    ("reloading", "正在装弹"),
]

# ---------------------------------------------------------------------------
# A1 · 舰船 groupID 白名单
# ---------------------------------------------------------------------------
# 逐组核对过样本，确认是舰船组。1404/1406 已剔除（是建筑）。
SHIP_GROUP_IDS: set[int] = {
    25,    # 护卫舰 T1
    26,    # 巡洋舰 T1
    27,    # 战列舰 T1
    28,    # 工业舰 T1
    29,    # 太空舱
    30,    # 泰坦
    31,    # 穿梭机
    237,   # 新手舰
    324,   # 突击护卫舰 T2
    358,   # 重型突击巡洋舰 T2
    380,   # 深空运输舰 T2
    419,   # 战列巡洋舰 T1
    420,   # 驱逐舰 T1
    463,   # 采掘者 T1（采矿驳船）
    485,   # 无畏舰
    513,   # 货舰
    540,   # 指挥舰 T2
    541,   # 拦截舰 T2
    543,   # 采掘者 T2（exhumer）
    547,   # 航母
    659,   # 超级航母
    830,   # 隐密行动护卫舰 T2
    831,   # 截击舰 T2
    832,   # 后勤巡洋舰 T2
    833,   # 侦察舰（力场）T2
    834,   # 隐身轰炸机 T2
    893,   # 电子攻击舰 T2
    894,   # 重型拦截舰 T2
    898,   # 黑隐特勤舰 T2
    900,   # 掠夺舰 T2
    902,   # 跳跃货舰 T2
    906,   # 侦察舰（战斗）T2
    941,   # 工业指挥舰
    963,   # 战略巡洋舰 T3
    1022,  # 特殊游艇（Zephyr）
    1201,  # 攻击战列巡洋舰
    1202,  # 封锁舰（blockade runner）T2
    1283,  # 远征护卫舰 T2
    1305,  # 战术驱逐舰 T3
    1527,  # 后勤护卫舰 T2
    1534,  # 指挥驱逐舰 T2
    1538,  # 力场辅助舰（force auxiliary）
    4594,  # 枪骑兵级无畏舰（Bane/Hubris/Karura/Valravn）
    5120,  # 新型航母（Gaia/Salvation/Simurgh/Ymir）
}

# ---------------------------------------------------------------------------
# A2 · 建筑与结构 groupID 白名单
# ---------------------------------------------------------------------------
STRUCTURE_GROUP_IDS: dict[int, str] = {
    1657: "昇威堡垒（星城/铁壁/空堡）",
    1404: "昇威工程复合体（莱塔卢/阿兹贝尔/索迪约）",
    1406: "昇威精炼厂（阿塔诺/塔塔拉）",
    2017: "诱导信标（明灯）",
    2016: "诱导干扰塔（暗影）",
    1408: "跳跃桥接（安塞波）",
    1246: "移动式仓库",
    1249: "移动式诱导抑制装置",
    1250: "移动式牵引装置",
    4093: "移动式诱导信标",
    4744: "卫星钻探（梅特诺克斯）",
    4810: "雇佣兵巢穴",
}

# ---------------------------------------------------------------------------
# B1 · 无人机 groupID 白名单
# ---------------------------------------------------------------------------
DRONE_GROUP_IDS: dict[int, str] = {
    100: "战斗无人机",
    1652: "铁骑舰载机",
    101: "采矿无人机",
    639: "电子战无人机",
    641: "结构/护盾干扰无人机",
    544: "能量中和无人机",
    1159: "打捞无人机",
}

# ---------------------------------------------------------------------------
# B2 · 模块 groupID 白名单（只取 T1/T2 基型）
# ---------------------------------------------------------------------------
MODULE_GROUP_IDS: dict[int, str] = {
    40: "护盾回充器",
    41: "远程护盾回充器",
    43: "电容回充器",
    46: "加力燃烧器 / 微型跃迁推进器",
    52: "跃迁扰频器 / 跃迁干扰器",
    57: "护盾能源继电器",
    59: "回转稳定器",
    60: "损伤控制",
    61: "电容电池",
    62: "装甲维修器",
    63: "结构维修器",
    65: "停滞缠绕光束",
    67: "远程电容传输装置",
    68: "掠能器",
    71: "能量中和器",
    76: "电容注电器",
    77: "护盾增强器",
    78: "强化舱壁",
    205: "散热槽",
    209: "远程追踪计算机",
    210: "信号放大器",
    211: "追踪增强器",
    212: "感应增强器",
    213: "追踪计算机",
    285: "协处理器",
    290: "远程感应增强器",
    302: "磁场稳定器",
    315: "曲跃核心稳定器",
    367: "弹道控制系统",
    379: "目标标记装置",
    645: "无人机伤害增效装置",
    762: "惯性稳定器",
    763: "纳米纤维内部结构",
    764: "超载喷射系统",
    766: "能量诊断系统",
    769: "反应堆控制单元",
    325: "远程装甲维修器（Remote Armor Repairer）",
    585: "远程结构维修器（Remote Hull Repairer）",
}
# 故意**不含** groupID=38（Shield Extender）：
#   - SDE 官方 zh 在本组内自相矛盾：多数条目是"护盾扩展装置"，
#     唯独 'Large Shield Extender I' 是"护盾扩充器"；
#   - 口语形态（LSE / extender / shield extender）已在 B3 覆盖，
#     且沿用与既有 B3 条目一致的"护盾扩充器"，避免组内冲突。
# 也不含 1156/1199/1697/1698（Ancillary 系列）：其 en 名没有 " I"/" II"
#   等级后缀，不符合 D8 的 T1/T2 基型规则；"ancil" 类口语留待后续评估。

# ---------------------------------------------------------------------------
# B3 · 口语短名（人工补充，非 SDE）
# ---------------------------------------------------------------------------
# 理由：SDE 里这些概念只有型号全名（"Warp Scrambler I"），
# 而舰队语音念的是 "scram" / "lse" / "sebo" / "mjd"。
# 中文一律取自 SDE 官方译名本身（不是自创），对应关系见行内注释；
# 只有 dictor / hic / hictor 三个是社区通用叫法（SDE 本文件里没有
# "Interdictor" 这个组名可引用），已在报告里标注需复核。
ORAL_SHORT: list[tuple[str, str]] = [
    ("scram", "跃迁扰频器"),        # SDE: Warp Scrambler -> 跃迁扰频器
    ("scrams", "跃迁扰频器"),
    ("scrammed", "被扰频"),
    ("scrambler", "跃迁扰频器"),
    ("cap booster", "电容注电器"),  # SDE: Capacitor Booster -> 电容注电器
    ("nos", "掠能器"),              # SDE: Energy Nosferatu -> 掠能器
    ("nosferatu", "掠能器"),
    ("mjd", "微型跳跃引擎"),        # SDE: Micro Jump Drive Operation -> 微型跳跃引擎操作
    ("dictor", "拦截舰"),           # 社区叫法（Interdictor）
    ("dictors", "拦截舰"),
    ("hic", "重型拦截舰"),          # 社区叫法（Heavy Interdictor）
    ("hictor", "重型拦截舰"),
    ("interdictor", "拦截舰"),
    ("lse", "大型护盾扩充器"),      # SDE: Large Shield Extender I -> 大型护盾扩充器I
    ("extender", "护盾扩充器"),
    ("sebo", "感应增强器"),         # SDE: Sensor Booster I -> 感应增强器 I
    ("paste", "纳米体修复粘合剂"),  # SDE: Nanite Repair Paste -> 纳米体修复粘合剂
    ("nanite", "纳米体修复粘合剂"),
    ("stabs", "跃迁核心稳定器"),    # SDE: Warp Core Stabilizer I -> 跃迁核心稳定器 I
    ("pod", "太空舱"),              # SDE: Capsule -> 太空舱
    ("plate", "钢附甲板"),          # SDE: Steel Plates -> 钢附甲板
    ("webifier", "停滞缠绕光束"),   # SDE: Stasis Webifier I -> 停滞缠绕光束 I
    # --- 以下 3 条为本次补充的"明显缺失基名"，均有 SDE 依据 ---
    ("shield extender", "护盾扩充器"),   # SDE: Large Shield Extender I -> 大型护盾扩充器I
                                        #   ⚠️ SDE 组内不一致：多数 Shield Extender 条目
                                        #      官方 zh 是"护盾扩展装置"，仅 Large I 是
                                        #      "护盾扩充器"；此处沿用既有 lse/extender 的
                                        #      "护盾扩充器"，需与游戏内实际显示核对。
    # ⚠️ 有意不收 "armor plate"：SDE 里的 "Armor Plates"(groupID 754, typeID 25605)
    #    是**贸易材料**（译名"装甲附甲板"），不是附甲板**模块**
    #    （模块是 "1600mm Steel Plates I"，groupID 329 -> "1600mm钢附甲板 I"）。
    #    舰队说的 "armor plate" 指模块，已被上面的 "plate" 覆盖，
    #    收录会把材料名强加到模块语境上 —— 属误分类，故丢弃。
    ("shield hardener", "护盾增强器"),   # SDE: groupID 77 组名 Shield Hardener -> 护盾增强器
                                        #   （组内条目都是 EM/Kinetic/Thermal/Explosive/
                                        #     Multispectrum 前缀的，无裸 "Shield Hardener" 物品；
                                        #     此为家族通称，对应 B2 的 4 条具体抗性增强器）
]

# ---------------------------------------------------------------------------
# 报告用：与常用英语词撞车的高风险键
# ---------------------------------------------------------------------------
# 这些键在**非术语语境**下会被强制替换，可能破坏译文。
# 本脚本不丢弃它们（丢错会永久失去一个术语），但会在报告里逐条列出，
# 交人工按实测结果决定去留。
COMMON_EN_WORDS: set[str] = {
    # 手写表里已有的
    "cap", "grid", "gate", "gates", "hold", "holding", "jump", "jumped",
    "jumping", "point", "pointed", "web", "webbed", "primary", "primaried",
    "align", "aligned", "warp", "warped", "warping", "logistics", "reload",
    "reloading", "tackle", "tackled", "scout", "scouts", "neut", "neuts",
    # 舰船名里撞车的（SDE 引入）
    "apocalypse", "armageddon", "avatar", "badger", "bane", "blackbird",
    "breacher", "burst", "capsule", "caracal", "chimera", "claw", "cobra",
    "condor", "crane", "crow", "crucifier", "crusader", "curse", "damnation",
    "daredevil", "deacon", "deluge", "drake", "eagle", "echelon", "echo",
    "enforcer", "executioner", "falcon", "fiend", "flycatcher", "gecko",
    "gram", "griffin", "guardian", "hammerhead", "harbinger", "hawk", "hel",
    "heron", "heretic", "hobgoblin", "hornet", "hound", "hubris", "hulk",
    "hurricane", "hydra", "hyena", "imp", "impel", "inquisitor", "jaguar",
    "kestrel", "legion", "leopard", "leviathan", "locust", "mamba", "mammoth",
    "marshal", "mastodon", "merlin", "moa", "nemesis",
    "nightmare", "nomad", "obelisk", "omen", "onyx", "oracle", "orca",
    "outrider", "pacifier", "paladin", "panther", "perseverance", "phoenix",
    "pilgrim", "pioneer", "plate", "pod", "porpoise", "praxis", "probe",
    "prophecy", "prospect", "prowler", "punisher", "purifier", "python",
    "raven", "reaper", "retribution", "retriever", "revelation", "rook",
    "rupture", "sabre", "salvation", "satyr", "scalpel", "scimitar",
    "scorpion", "scythe", "sentinel", "sigil", "sin", "skiff", "slasher",
    "squall", "stiletto", "succubus", "taipan", "tempest", "templar",
    "thorax", "thrasher", "tormentor", "tornado", "torrent", "typhoon",
    "vagabond", "valkyrie", "vanguard", "vanquisher", "vehement", "vendetta",
    "vengeance", "venture", "vespa", "victor", "vigil", "vigilant",
    "vindicator", "violator", "virtuoso", "vulture", "warden", "warrior",
    "wasp", "widow", "wolf", "worm", "wreathe", "zealot", "zephyr", "ogre",
    "berserker", "scrambler", "scrams", "extender", "paste", "nanite",
    "stabs", "nos", "pod",
    # B2 基名里带普通英语含义的（本次 B2 重构后新增关注）
    "damage control", "heat sink", "plate",
}

# ---------------------------------------------------------------------------
# D9 · 直接丢弃的"普通英语词"（EVE 语义明显不占主导）
# ---------------------------------------------------------------------------
# 判断标准：该词在日常英语口语中的出现频率 >> 舰队语境，
# 强制替换会明显破坏译文。其余"高风险"词一律保留并记入报告。
DROP_AS_COMMON_WORD: set[str] = {
    # 目前为空：SDE 里命中的高风险词都至少是"特定语境才出现"，
    # 保留 + 报告比直接丢弃更安全（丢错了会永久失去一个术语）。
    # 若实测发现某个词频繁误伤，把它的**小写键**加进来即可。
}

# ---------------------------------------------------------------------------
# D11 · 人工点杀名单（SDE 官方中文本身有问题 / 键里有引号会污染 YAML）
# ---------------------------------------------------------------------------
# 逐条理由见注释。这类条目数量极少，手工维护比写规则更可靠。
MANUAL_DROP: dict[str, str] = {
    # SDE 官方中文译名语序错位（"蜻蜓级加达里海军" 应为 "加达里海军蜻蜓级"），
    # 保留反而会强制出一个错误译法。
    "caldari navy dragonfly": "SDE 中文译名语序错位",
    "imperial navy templar": "SDE 中文译名语序错位",
}

# 键里出现引号/撇号的一律丢弃：
#  - 玩家自命名建筑的绰号（'Draccous' Fortizar）
#  - 带绰号的无人机（Mutated 'Excavator' Mining Drone）
#  - 含撇号的所有格（Goru's Shuttle）
# 这些键写进 YAML 需要转义，且舰队语音不会念出带引号的全名。
QUOTE_CHARS = "'\"“”‘’"

# ---------------------------------------------------------------------------
# 通用丢弃 token
# ---------------------------------------------------------------------------
DROP_SUBSTRINGS = (
    "blueprint",
    "do not translate",
    "wreck",
    "lights on",
    "under construction",
    "non-interactable",
    "non interactable",
    "fragment",
    "bpc token",
    "quantum core",
    "construction",
)
DROP_TOKENS = ("test", "gfx", "polaris", "soct")
DROP_EDITION = " edition"

# 模块：只接受 "… I" / "… II"，且不含品牌/势力/深渊修饰
MODULE_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]* I{1,2}$")
MODULE_DROP_SUBSTR = (
    "abyssal",
    "concord",
    "civilian",
    "enduring",
    "compact",
    "scoped",
    "restrained",
    "ample",
    "upgraded",
    "limited",
    "experimental",
    "prototype",
    "type-",
    "c-type",
    "a-type",
    "b-type",
    "x-type",
    "navy",
    "fleet",
    "guristas",
    "sansha",
    "blood",
    "angel",
    "serpentis",
    "domination",
    "republic",
    "federation",
    "imperial",
    "khanid",
    "ammatar",
    "modified",
)

# 无人机 / 铁骑舰载机的罗马数字后缀（en 与 zh 都要剥）
DRONE_ROMAN_RE = re.compile(r"\s+(I{1,3})$")

# 名称里的"非 ASCII"判定
ASCII_RE = re.compile(r"^[\x20-\x7e]+$")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class Entry:
    __slots__ = ("key", "value", "category", "type_id", "en", "priority")

    def __init__(self, key, value, category, type_id, en, priority):
        self.key = key
        self.value = value
        self.category = category
        self.type_id = type_id
        self.en = en
        self.priority = priority


# 类别优先级：数字越小越优先（同名冲突时胜出）
PRIORITY = {
    "A0 手写表": 0,
    "A1 舰船": 1,
    "A2 建筑与结构": 2,
    "B1 无人机": 3,
    "B2 模块": 4,
    "B3 口语短名": 5,
}

# 每类的词数上限（超过即视为型号全名，见 D6）
WORD_LIMIT = {
    "A1 舰船": 3,
    "A2 建筑与结构": 4,
    "B1 无人机": 4,
    "B2 模块": 6,
}


def is_ascii(text: str) -> bool:
    return bool(ASCII_RE.match(text))


def word_count(text: str) -> int:
    return len(text.split())


def strip_drone_suffix(name: str) -> str:
    """'Wasp I' -> 'Wasp'；'胡蜂 I' -> '胡蜂'；'Wasp EC-900' 原样返回。"""
    return DRONE_ROMAN_RE.sub("", name).strip()


# ---------------------------------------------------------------------------
# B2 模块：型号全名 -> 基名
# ---------------------------------------------------------------------------
# 舰队语音念的是基名（"armor repairer"），不是带尺寸/等级的全名
# （"Small Armor Repairer I"）。这里把两侧的尺寸前缀与等级后缀都剥掉。
#
# en 侧：尺寸前缀是**独立的第一个词**（如 "capital armor repairer i"）。
MODULE_SIZE_EN = (
    "10000mn", "50000mn", "1000mn", "500mn", "100mn", "50mn", "10mn", "5mn", "1mn",
    "x-large", "large", "medium", "small", "capital", "heavy",
)
# zh 侧：尺寸前缀**紧贴基名、无空格**（如 "旗舰级装甲维修器 I"）；
# 按长度降序匹配，避免 "大型" 抢先匹配 "超大型"。
MODULE_SIZE_ZH = (
    "10000MN", "50000MN", "1000MN", "500MN", "100MN", "50MN", "10MN", "5MN", "1MN",
    "超大型", "大型", "中型", "小型", "旗舰级", "重型",
)
# zh 等级后缀形如 " I" / " II"（可带前导空格）
MODULE_LEVEL_ZH_RE = re.compile(r"\s*I{1,2}$")


def module_base(en: str, zh: str) -> tuple[str, str]:
    """把模块型号全名剥成基名。

    'Small Armor Repairer I'  / '小型装甲维修器 I'   -> ('armor repairer', '装甲维修器')
    'Capital Shield Booster II' / '旗舰级护盾回充增量器 II' -> ('shield booster', '护盾回充增量器')
    'Stasis Webifier I' / '停滞缠绕光束 I'          -> ('stasis webifier', '停滞缠绕光束')
    """
    parts = en.split()
    if parts and parts[0].lower() in MODULE_SIZE_EN:
        parts = parts[1:]
    if parts and parts[-1].upper() in ("I", "II"):
        parts = parts[:-1]
    base_en = " ".join(parts).strip()

    base_zh = zh.strip()
    for p in MODULE_SIZE_ZH:
        if base_zh.startswith(p):
            base_zh = base_zh[len(p):]
            break
    base_zh = MODULE_LEVEL_ZH_RE.sub("", base_zh).strip()
    return base_en, base_zh


class Stats:
    def __init__(self) -> None:
        self.dropped = Counter()          # 理由 -> 条数
        self.dropped_samples = {}         # 理由 -> [样例]
        self.candidates = Counter()       # 类别 -> 候选条数
        self.kept = Counter()             # 类别 -> 最终条数
        self.collisions = []              # (key, 胜者, 败者)
        self.handwritten_shadowed = []    # SDE 想写但被手写表挡下的
        self.same_name_diff_value = []    # 同名多义（同类别内）
        self.b2_merged = 0                # B2 基名归并掉（同一基名的多个尺寸/等级变体）

    def drop(self, reason: str, sample: str) -> None:
        self.dropped[reason] += 1
        if len(self.dropped_samples.setdefault(reason, [])) < 12:
            self.dropped_samples[reason].append(sample)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 EVE 舰队语音热词表")
    ap.add_argument("--case", choices=("lower", "preserve"), default="lower",
                    help="键的大小写策略（默认 lower，与现有手写表一致）")
    ap.add_argument("--sde", default=str(SDE_PATH), help="types.jsonl 路径")
    ap.add_argument("--out", default=str(OUT_YAML), help="输出 yaml 路径")
    ap.add_argument("--stats-json", default="", help="额外导出统计 JSON 路径")
    args = ap.parse_args()

    sde_path = Path(args.sde)
    if not sde_path.exists():
        print(f"错误：找不到 {sde_path}", file=sys.stderr)
        return 2

    stats = Stats()
    entries: "OrderedDict[str, Entry]" = OrderedDict()
    # 记录"同类别内同 key 但中文不同"的情况（真正的多义）
    seen_same_cat: dict[str, str] = {}

    def normalize_key(raw: str) -> str:
        return raw if args.case == "preserve" else raw.lower()

    handwritten_keys = {k for k, _ in HANDWRITTEN}

    def add(entry: Entry) -> None:
        """按优先级插入；同名冲突时高优先级胜出，并记录冲突。"""
        existing = entries.get(entry.key)
        if existing is None:
            entries[entry.key] = entry
            return
        if entry.priority < existing.priority:
            stats.collisions.append((entry.key, entry.category, existing.category))
            entries[entry.key] = entry
        else:
            stats.collisions.append((entry.key, existing.category, entry.category))

    # ---- A0 手写表 --------------------------------------------------------
    for key, value in HANDWRITTEN:
        add(Entry(key, value, "A0 手写表", -1, key, PRIORITY["A0 手写表"]))

    # ---- B3 口语短名（人工补充）--------------------------------------------
    for key, value in ORAL_SHORT:
        add(Entry(key, value, "B3 口语短名", -1, key, PRIORITY["B3 口语短名"]))

    # ---- 流式扫描 SDE ------------------------------------------------------
    with open(sde_path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            group_id = obj.get("groupID")
            type_id = obj.get("_key")
            name = obj.get("name") or {}
            en_raw = (name.get("en") or "").strip()
            zh_raw = (name.get("zh") or "").strip()
            published = bool(obj.get("published"))

            # 决定这条属于哪个类目
            if group_id in SHIP_GROUP_IDS:
                category = "A1 舰船"
            elif group_id in STRUCTURE_GROUP_IDS:
                category = "A2 建筑与结构"
            elif group_id in DRONE_GROUP_IDS:
                category = "B1 无人机"
            elif group_id in MODULE_GROUP_IDS:
                category = "B2 模块"
            else:
                continue

            stats.candidates[category] += 1

            if not en_raw or not zh_raw:
                stats.drop("D0 缺 en/zh 对照", f"{en_raw!r}/{zh_raw!r}")
                continue

            low = en_raw.lower()

            # D1 未发布
            if not published:
                stats.drop("D1 未发布(published!=true)", en_raw)
                continue
            # D2 蓝图
            if "blueprint" in low:
                stats.drop("D2 蓝图 Blueprint", en_raw)
                continue
            # D3 开发/测试残留（按"整词"匹配，避免 protest 被 test 误伤）
            words = {w.strip("'\"“”") for w in low.split()}
            if words & set(DROP_TOKENS):
                stats.drop("D3 测试/占位物(Test/GFX/Polaris/SOCT)", en_raw)
                continue
            if "do not translate" in low:
                stats.drop("D3 测试/占位物(DO NOT TRANSLATE)", en_raw)
                continue
            # D5 非 ASCII
            if not is_ascii(en_raw):
                stats.drop("D5 en 非 ASCII", en_raw)
                continue
            # 结构类：名称含 wreck/量子芯/凭证等
            if category == "A2 建筑与结构":
                if any(sub in low for sub in DROP_SUBSTRINGS):
                    stats.drop("D2/D4 残骸·蓝图·量子芯等派生物", en_raw)
                    continue
            # D4 版本/涂装版舰船
            if DROP_EDITION in low:
                stats.drop("D4 联名/纪念版(… Edition)", en_raw)
                continue
            # D7 玩家自命名 / 品牌前缀 / 含引号撇号（会污染 YAML 键）
            if any(ch in en_raw for ch in QUOTE_CHARS):
                stats.drop("D7 含引号/撇号（自命名·绰号·所有格）", en_raw)
                continue

            # D11 人工点杀（SDE 官方中文本身有误）
            if low in MANUAL_DROP:
                stats.drop(f"D11 人工点杀：{MANUAL_DROP[low]}", en_raw)
                continue

            # 无人机：剥罗马数字后缀（en 与 zh 同步剥）
            if category == "B1 无人机":
                en = strip_drone_suffix(en_raw)
                zh = strip_drone_suffix(zh_raw)
            else:
                en = en_raw
                zh = zh_raw

            # D6 词数上限
            limit = WORD_LIMIT.get(category, 99)
            if word_count(en) > limit:
                stats.drop(f"D6 词数>{limit}（型号全名）", en_raw)
                continue

            # B2 模块：只留 T1/T2 基型，并进一步剥成"基名"（见 P3）
            if category == "B2 模块":
                if not MODULE_BASE_RE.match(en):
                    stats.drop("D8 模块非 T1/T2 基型", en_raw)
                    continue
                if any(sub in low for sub in MODULE_DROP_SUBSTR):
                    stats.drop("D8 模块品牌/势力/深渊修饰", en_raw)
                    continue
                base_en, base_zh = module_base(en, zh)
                if not base_en or not base_zh:
                    stats.drop("D8 模块基名剥离后为空", en_raw)
                    continue
                en, zh = base_en, base_zh

            # D9 普通英语词直接丢弃
            key = normalize_key(en)
            if key in DROP_AS_COMMON_WORD:
                stats.drop("D9 普通英语词占主导", en_raw)
                continue

            # D10 同名多义（同类别内同 key 不同中文）
            prev = seen_same_cat.get(key)
            if prev is not None and prev != zh:
                stats.same_name_diff_value.append((key, prev, zh, type_id))
            else:
                seen_same_cat.setdefault(key, zh)

            # B2 基名归并：同一基名的多个尺寸/等级变体合并成一条。
            # 这不是"同名冲突"（是同一术语的型号变体），故不计入 collisions。
            if category == "B2 模块":
                prev_b2 = entries.get(key)
                if prev_b2 is not None and prev_b2.category == "B2 模块":
                    if prev_b2.value != zh:
                        stats.same_name_diff_value.append(
                            (key, prev_b2.value, zh, type_id))
                    stats.b2_merged += 1
                    continue

            # 若 SDE 撞上手写表：记录，但不覆盖（手写表优先级最高）
            if key in handwritten_keys:
                stats.handwritten_shadowed.append((key, category, zh))

            add(Entry(key, zh, category, type_id, en_raw, PRIORITY[category]))

    # ---- 归类整理 ----------------------------------------------------------
    # A0/B3 是人工表：保持**原始书写顺序**（"原样保留"）；
    # A1/A2/B1/B2 来自 SDE：按 key 字典序排序，typeID 兜底，保证可复现。
    manual_order = {k: i for i, (k, _) in enumerate(HANDWRITTEN)}
    manual_order.update({k: i for i, (k, _) in enumerate(ORAL_SHORT)})

    by_cat: dict[str, list[Entry]] = {c: [] for c in PRIORITY}
    for entry in entries.values():
        by_cat[entry.category].append(entry)
    for cat in by_cat:
        if cat in ("A0 手写表", "B3 口语短名"):
            by_cat[cat].sort(key=lambda e: manual_order.get(e.key, 10**9))
        else:
            by_cat[cat].sort(key=lambda e: (e.key, e.type_id if e.type_id >= 0 else -1))
        stats.kept[cat] = len(by_cat[cat])

    # 高风险条目（与常用英语词撞车但保留）
    risky = sorted(
        (e.key, e.category, e.value)
        for e in entries.values() if e.key in COMMON_EN_WORDS
    )

    total = sum(stats.kept.values())
    if total > 1000:
        print(f"错误：总条数 {total} 超过硬上限 1000", file=sys.stderr)
        return 3

    # ---- 写 yaml -----------------------------------------------------------
    section_titles = {
        "A0 手写表": "A0 · 手写表（最高优先级，原样保留，不可被覆盖）",
        "A1 舰船": "A1 · 舰船名（SDE groupID 白名单）",
        "A2 建筑与结构": "A2 · 建筑与结构（昇威建筑 / 诱导信标 / 移动式装置）",
        "B1 无人机": "B1 · 无人机与铁骑舰载机（已剥罗马数字后缀）",
        "B2 模块": "B2 · 核心战斗模块基名（已剥尺寸前缀与等级后缀）",
        "B3 口语短名": "B3 · 口语短名（人工补充，非 SDE，需复核）",
    }

    lines: list[str] = []
    lines.append("# ===========================================================================")
    lines.append("#  EVE Online 舰队语音实时翻译 —— 术语热词表")
    lines.append("#  由 src/tools/build_phrases.py 从 EVE SDE types.jsonl 自动生成")
    lines.append("#  用途：dashscope qwen3.5-livetranslate-flash-realtime 的")
    lines.append("#        translation_params.corpus.phrases（强制术语译法）")
    lines.append("#  大小写策略：" + ("全部小写（与现有手写表一致）" if args.case == "lower"
                                   else "保留 SDE 原始大小写"))
    lines.append(f"#  总条数：{total} / 硬上限 1000")
    lines.append("#  用法：config.yaml 里 `asr.livetranslate.phrases_file: terms.yaml`")
    lines.append("#        引用本文件；与内联 phrases 合并时**内联优先**。")
    lines.append("#  ⚠️ 改完必须重启程序 / 托盘「重新连接」——热词表在建连时一次性下发。")
    lines.append("#  ⚠️ 大小写不敏感（2026-09-28 联机 A/B 实测），键统一小写即可。")
    lines.append("# ===========================================================================")
    lines.append("")
    for cat in PRIORITY:
        items = by_cat[cat]
        lines.append("")
        lines.append(f"# ---------- {section_titles[cat]}  （{len(items)} 条） ----------")
        for entry in items:
            lines.append(f'{entry.key}: "{entry.value}"')
    lines.append("")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- 统计输出 ----------------------------------------------------------
    print("=" * 72)
    print("候选 / 最终 条数")
    for cat in PRIORITY:
        print(f"  {cat:<16} 候选={stats.candidates.get(cat, 0):>5}  "
              f"最终={stats.kept[cat]:>5}")
    print(f"  {'合计':<16} 最终={total:>5}   (硬上限 1000)")
    print()
    print(f"B2 基名归并：{stats.b2_merged} 条尺寸/等级变体被并入基名")
    print()
    print("丢弃分布")
    for reason, cnt in stats.dropped.most_common():
        print(f"  {cnt:>6}  {reason}")
        for s in stats.dropped_samples[reason][:3]:
            print(f"          e.g. {s}")
    print()
    print(f"同名冲突（共 {len(stats.collisions)} 条）")
    for key, win, lose in stats.collisions[:40]:
        print(f"  {key!r}: 保留 {win}  丢弃 {lose}")
    print()
    print(f"被手写表挡下的 SDE 条目（共 {len(stats.handwritten_shadowed)} 条）")
    for key, cat, zh in stats.handwritten_shadowed[:40]:
        print(f"  {key!r}: 手写表胜出  <->  SDE({cat}) {zh!r}")
    print()
    print(f"同名多义（同类别内同 key 不同中文，共 {len(stats.same_name_diff_value)} 条）")
    for key, a, b, tid in stats.same_name_diff_value[:60]:
        print(f"  {key!r}: {a!r} / {b!r}  (typeID={tid})")
    print()
    print(f"⚠️ 与常用英语词撞车但保留（共 {len(risky)} 条，需实测复核）")
    for key, cat, val in risky:
        print(f"  {key:<28} {val:<14} [{cat}]")
    print()
    print(f"已写出: {out_path}")

    if args.stats_json:
        payload = {
            "total": total,
            "case": args.case,
            "candidates": dict(stats.candidates),
            "kept": dict(stats.kept),
            "dropped": dict(stats.dropped),
            "dropped_samples": stats.dropped_samples,
            "collisions": stats.collisions,
            "handwritten_shadowed": stats.handwritten_shadowed,
            "same_name_diff_value": stats.same_name_diff_value,
            "b2_merged": stats.b2_merged,
            "risky_common_english": risky,
            "keys": {cat: [e.key for e in by_cat[cat]] for cat in by_cat},
        }
        Path(args.stats_json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"统计 JSON: {args.stats_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
