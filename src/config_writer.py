"""配置写回：把设置界面的改动写进 config.yaml / .env。

设计取舍
--------
**为什么不用 PyYAML 往返**

    config.yaml 里的注释是项目资产 —— 每个字段为什么这么配、
    哪个值踩过什么坑，都写在注释里。`yaml.safe_dump()` 往返一次
    这些注释**全部丢失**。所以这里做**行级手术**：
    只替换目标键所在行的值，其余字节原样保留。

**为什么 API Key 只写 .env**

    `.env` 在 .gitignore 里，`config.yaml` 不在。
    把密钥写进 config.yaml 有误提交到仓库的风险。
    因此 `apply_yaml_updates()` 会**拒绝**任何看起来像密钥的键。

用法::

    from src.config_writer import apply_yaml_updates, apply_env_updates

    apply_yaml_updates("config.yaml", {
        "overlay.opacity": 0.7,
        "asr.provider": "cloud",
    })
    apply_env_updates(".env", {"DASHSCOPE_API_KEY": "sk-xxx"})
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigError

__all__ = [
    "apply_yaml_updates",
    "apply_env_updates",
    "read_env",
    "dump_scalar",
    "KeyNotFoundError",
]

# `key:` 或 `key: value`。键字符集排除 `:`，保证值里的冒号（如 URL）不被吃掉。
_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_.\-]*)\s*:\s?(.*)$")

# YAML 里裸写会被解析成非字符串的词
_YAML_BOOLISH = {
    "true", "false", "yes", "no", "on", "off", "null", "~",
    "y", "n",
}

# 值不需要加引号的安全字符集
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")

# 绝不写进 config.yaml 的键：按**下划线末段**精确匹配，
# 避免误伤 `device_keyword` / `api_key_env`（它们存的是关键词和环境变量名，不是密钥）。
_SECRET_LEAVES = {"key", "apikey", "token", "secret", "password", "passwd"}


class KeyNotFoundError(ConfigError):
    """目标键在 YAML 中不存在（说明键名拼错了）。"""


# =========================================================
#  标量序列化
# =========================================================
def dump_scalar(value: Any) -> str:
    """把 Python 值序列化成 YAML 标量（必要时加双引号）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # 避免 0.30000000000000004 这种浮点噪声
        s = f"{value:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"

    s = str(value)
    if s == "":
        return '""'
    if s.lower() in _YAML_BOOLISH:
        return f'"{s}"'
    if _PLAIN_RE.match(s):
        return s
    # 需要引号：含特殊字符 / 前后空格 / 数字形状
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _split_comment(s: str) -> tuple[str, str]:
    """把 `值  # 注释` 拆成 (值部分, 注释部分)。引号内的 # 不算注释。"""
    in_s = in_d = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d:
            if i == 0 or s[i - 1] in " \t":
                return s[:i], s[i:]
    return s, ""


# =========================================================
#  YAML 扫描
# =========================================================
def _iter_keys(lines: list[str]):
    """按出现顺序产出 (行号, 缩进, 键名, 值部分, 完整点分路径)。

    自动跳过块标量（`|` / `>`）内部的行 —— 否则提示词里的
    `Rules:` 之类会被误当成配置键，污染路径栈。
    """
    stack: list[tuple[int, str]] = []
    i = 0
    n = len(lines)

    while i < n:
        body = lines[i].rstrip("\r\n")
        stripped = body.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        m = _KEY_RE.match(body)
        if not m:
            i += 1
            continue

        indent = len(m.group(1))
        key = m.group(2)
        rest = m.group(3)

        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        yield i, indent, key, rest, path

        # 块标量：跳过所有比它更深缩进的行
        if rest and rest[0] in "|>":
            i += 1
            while i < n:
                nb = lines[i].rstrip("\r\n")
                if not nb.strip():
                    i += 1
                    continue
                if len(nb) - len(nb.lstrip()) <= indent:
                    break
                i += 1
            continue

        # 值为空 = 容器，后续更深的键归属于它
        if not rest:
            stack.append((indent, key))
        i += 1


def _dominant_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _assert_not_secret(dotted: str) -> None:
    leaf = dotted.rsplit(".", 1)[-1].lower()
    if leaf.split("_")[-1] in _SECRET_LEAVES:
        raise ConfigError(
            f"拒绝把疑似密钥的键 `{dotted}` 写入 config.yaml",
            detail="密钥只能写进 .env（该文件已被 .gitignore 忽略）",
        )


# =========================================================
#  YAML 写入
# =========================================================
def _set_scalar(lines: list[str], dotted: str, value: Any) -> bool:
    """替换标量值。返回是否命中。

    特例：把**原本是 `null` 的可选字段**清空（写入空串）时保持 `null`。
    这样 `url` / `workspace` / `download_root` 这类 `str | None` 字段
    在界面上清空后能干净地回到"未设置"，而不是变成空字符串。
    """
    for idx, _indent, _key, rest, path in _iter_keys(lines):
        if path != dotted:
            continue

        raw = lines[idx]
        body = raw.rstrip("\r\n")
        ending = raw[len(body):]
        val_part, comment = _split_comment(rest)
        # 保留原值后面到注释之间的空白，缩进风格不变
        trail = val_part[len(val_part.rstrip()):]
        prefix = body[: len(body) - len(rest)]

        if value == "" and val_part.strip() in ("null", "~"):
            value = None

        # 原值是块标量（`|` / `>`）而新值是单行 —— 必须把旧块内容整段删掉，
        # 否则会残留在新值后面（`short` + 旧的 8 行提示词 = YAML 结构损坏）。
        was_block = val_part.strip()[:1] in ("|", ">")

        lines[idx] = prefix + dump_scalar(value) + trail + comment + ending

        if was_block:
            last_content = _block_extent(lines, idx, _indent)
            del lines[idx + 1: last_content + 1]
        return True
    return False


def _block_extent(lines: list[str], target_idx: int, target_indent: int) -> int:
    """返回块内容的最后一行行号（无内容时返回 target_idx 本身）。

    块的判定：缩进严格大于 target_indent 的行，直到遇到第一个
    缩进回退的非空行为止。空行不终止块（YAML 块内允许空行）。
    """
    n = len(lines)
    scan = target_idx + 1
    last = target_idx
    while scan < n:
        body = lines[scan].rstrip("\r\n")
        if not body.strip():
            scan += 1
            continue
        if len(body) - len(body.lstrip()) <= target_indent:
            break
        last = scan
        scan += 1
    return last


def _find_container(lines: list[str], dotted: str) -> tuple[int, int] | None:
    """定位一个"容器键"（值为空、靠子行承载内容）的行号与缩进。

    也接受**内联空容器** `key: {}` / `key: []` —— 它们语义上等价于
    "没有子行"，整体替换时由调用方把内联的 `{}` / `[]` 抹掉。
    """
    for idx, indent, _key, rest, path in _iter_keys(lines):
        if path != dotted:
            continue
        if not rest or _split_comment(rest)[0].strip() in ("{}", "[]"):
            return idx, indent
    return None


def _clear_inline_empty_container(lines: list[str], idx: int) -> None:
    """把 `key: {}` / `key: []` 的键行还原成裸 `key:`（准备写子行）。"""
    raw = lines[idx]
    body = raw.rstrip("\r\n")
    ending = raw[len(body):]
    m = _KEY_RE.match(body)
    if m is None:       # pragma: no cover - _find_container 已保证匹配
        return
    _val, comment = _split_comment(m.group(3))
    prefix = body[: len(body) - len(m.group(3))].rstrip()
    lines[idx] = prefix + (" " + comment if comment else "") + ending


def _set_mapping(lines: list[str], dotted: str, mapping: Mapping) -> bool:
    """整体替换一个块映射（如热词表 phrases）。"""
    hit = _find_container(lines, dotted)
    if hit is None:
        return False
    target_idx, target_indent = hit

    ending = _dominant_ending("".join(lines))
    child_indent = " " * (target_indent + 2)
    new_lines = [
        f"{child_indent}{dump_scalar(str(k))}: {dump_scalar(v)}{ending}"
        for k, v in mapping.items()
    ]
    _clear_inline_empty_container(lines, target_idx)
    last_content = _block_extent(lines, target_idx, target_indent)
    lines[target_idx + 1: last_content + 1] = new_lines
    return True


def _set_sequence(lines: list[str], dotted: str, seq) -> bool:
    """整体替换一个块序列（如 overlay.speaker_colors）。"""
    hit = _find_container(lines, dotted)
    if hit is None:
        return False
    target_idx, target_indent = hit

    ending = _dominant_ending("".join(lines))
    child_indent = " " * (target_indent + 2)
    new_lines = [f"{child_indent}- {dump_scalar(v)}{ending}" for v in seq]
    _clear_inline_empty_container(lines, target_idx)
    last_content = _block_extent(lines, target_idx, target_indent)
    lines[target_idx + 1: last_content + 1] = new_lines
    return True


def _set_block_scalar(lines: list[str], dotted: str, text: str) -> bool:
    """把多行文本写成 YAML 块标量（`|`）。

    ⚠️ 必须**先把旧的块内容整段删掉**再写新内容 ——
    否则旧内容会残留在新块后面，直接把 YAML 结构写坏。
    （`system_prompt` 这种 `|` 字段被标量写入时会踩这个坑。）
    """
    for idx, indent, _key, rest, path in _iter_keys(lines):
        if path != dotted:
            continue

        raw = lines[idx]
        body = raw.rstrip("\r\n")
        ending = raw[len(body):] or _dominant_ending("".join(lines))
        val_part, comment = _split_comment(rest)
        prefix = body[: len(body) - len(rest)]

        # 保留原有的块指示符（`|` / `|-` / `>`）；不是块就补一个 `|`
        indicator = val_part.strip()
        if not indicator or indicator[0] not in "|>":
            indicator = "|"
        lines[idx] = f"{prefix}{indicator}{comment}{ending}"

        body_text = text.replace("\r\n", "\n").replace("\r", "\n")
        if body_text and not body_text.endswith("\n"):
            body_text += "\n"

        child = " " * (indent + 2)
        if body_text:
            new_block = [
                (child + ln + ending) if ln.strip() else ending
                for ln in body_text.split("\n")[:-1]
            ]
        else:
            new_block = []

        last_content = _block_extent(lines, idx, indent)
        lines[idx + 1: last_content + 1] = new_block
        return True
    return False


def apply_yaml_updates(path: str | Path, updates: Mapping[str, Any]) -> list[str]:
    """批量更新 config.yaml（一次读写，避免中间态落盘）。

    Args:
        path:    config.yaml 路径
        updates: {点分键: 新值}，如 `{"overlay.opacity": 0.7}`。
                 值类型决定写入方式：
                   · 标量（str/int/float/bool/None）→ 原地替换该行
                   · dict        → 整体替换块映射（如热词表）
                   · list/tuple  → 整体替换块序列（如 speaker_colors）
                   · 含换行的 str → 写为 `|` 块标量（如 system_prompt）

    Returns:
        实际改动的键列表

    Raises:
        KeyNotFoundError: 某个键在文件里找不到（多半是拼错了）
        ConfigError:      试图写入疑似密钥的键
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"配置文件不存在: {p}")

    text = p.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    missing: list[str] = []
    for dotted, value in updates.items():
        _assert_not_secret(dotted)
        if isinstance(value, Mapping):
            ok = _set_mapping(lines, dotted, value)
        elif isinstance(value, (list, tuple)):
            ok = _set_sequence(lines, dotted, value)
        elif isinstance(value, str) and "\n" in value:
            ok = _set_block_scalar(lines, dotted, value)
        else:
            ok = _set_scalar(lines, dotted, value)
        if not ok:
            missing.append(dotted)

    if missing:
        raise KeyNotFoundError(
            "以下配置键在文件中不存在，未写入任何改动:\n  - "
            + "\n  - ".join(missing),
            detail=f"配置文件: {p}",
        )

    p.write_text("".join(lines), encoding="utf-8")
    return list(updates)


# =========================================================
#  .env 读写
# =========================================================
_ENV_RE = re.compile(r"^(\s*)(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def read_env(path: str | Path) -> dict[str, str]:
    """读取 .env 里的键值（不做引号剥离以外的解析）。"""
    p = Path(path)
    if not p.exists():
        return {}

    out: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_RE.match(raw)
        if not m:
            continue
        val = m.group(3).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[m.group(2)] = val
    return out


def apply_env_updates(path: str | Path, updates: Mapping[str, str]) -> list[str]:
    """写入 / 更新 .env 中的键。

    - 已存在的键：原地替换该行的值（保留注释与顺序）
    - 不存在的键：追加到文件末尾

    ⚠️ 写完后同步刷新 `os.environ` —— 否则运行中的进程读到的还是旧值。

    Returns:
        实际写入的键列表
    """
    if not updates:
        return []

    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = text.splitlines(keepends=True)
    ending = _dominant_ending(text) if text else "\n"

    written: list[str] = []
    for key, value in updates.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ConfigError(f"非法环境变量名: {key!r}")
        # 值里换行会破坏 .env 格式
        value = str(value).replace("\r", "").replace("\n", " ")

        hit = False
        for idx, raw in enumerate(lines):
            m = _ENV_RE.match(raw.rstrip("\r\n"))
            if m and m.group(2) == key:
                lines[idx] = f"{m.group(1)}{key}={value}{ending}"
                hit = True
                break

        if not hit:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += ending
            lines.append(f"{key}={value}{ending}")

        os.environ[key] = value      # 立即生效，无需重启进程读 .env
        written.append(key)

    p.write_text("".join(lines), encoding="utf-8")
    return written
