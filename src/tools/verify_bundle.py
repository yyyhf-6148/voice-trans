"""打包产物自检：**确认 exe 里没有夹带任何密钥**。

为什么需要它
------------
"记得不要把 key 打进去"这种要求不能靠人记得 —— 一次手滑把 `.env`
写进 `trans.spec` 的 datas，密钥就永久留在 exe 二进制里了
（即使之后从 spec 里删掉，已经发出去的 exe 也收不回来）。

⚠️ 为什么不能直接 `secret in open(exe,'rb').read()`
--------------------------------------------------
PyInstaller 把数据文件（`config.yaml` 等）和 Python 模块
**全部 zlib 压缩**后塞进归档，明文在 exe 里搜不到任何东西 ——
这种写法即使 `.env` 真被打进去也会**假通过**，比不检查更危险。

所以这里改成**读归档 + 解压**再搜：
  1. 用 `CArchiveReader` 列出全部条目 → 按**文件名**拦截 `.env` 之类
     （这一步是决定性的，不依赖内容）
  2. 逐条目 `extract()` 解压 → 搜 `.env` 里每个密钥的**真实值**
  3. 同一遍里再按**通用密钥形态**（`sk-` + 长串）搜一遍 ——
     这一步不依赖 `.env` 是否存在，能兜住"有人把 Key 硬编码进代码"
  4. 把 PYZ 解出来，逐模块 `marshal.dumps()` → 同样两套搜索
     （Python 模块也是压缩的，不 marshal 就搜不到常量）

顺带核对内置 `config.yaml` 是否真的进了包、体积是否合理。

用法::

    .venv\\Scripts\\python.exe -m src.tools.verify_bundle dist/EVE-Voice-Translator.exe
"""
from __future__ import annotations

import marshal
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Iterator

_ROOT = Path(__file__).resolve().parents[2]

# 体积合理区间（MB）。排除了 torch 等重依赖后实测 ~95MB；
# 超过上限说明 excludes 没生效（很可能把 torch 打进去了）。
_SIZE_MIN_MB = 40.0
_SIZE_MAX_MB = 400.0

# 值短于这个长度就不参与搜索（避免 "sk-" 这类短前缀造成假阳性）
_MIN_SECRET_LEN = 12

# 绝不允许出现在包里的文件名（密钥载体）
_FORBIDDEN_NAMES = {
    ".env", ".env.local", "api_keys.json", "secrets.yaml",
}

# 通用密钥形态：DashScope / OpenAI 风格 `sk-` + 长串。
# 前面加否定环视，避免 `task-manager-...` 这类正常字符串被误判。
# 这一条**不依赖 .env 存在** —— 换台机器构建（没有 .env）时
# 第 2 项检查会 SKIP，只有它还能兜住"有人把 Key 硬编码进代码"。
_SECRET_PATTERNS = (
    re.compile(rb"(?<![A-Za-z0-9_])sk-[A-Za-z0-9]{20,}"),
)


def _read_env_values(env_path: Path) -> dict[str, str]:
    """解析 .env，返回 {变量名: 值}。只收有值的那几行。"""
    out: dict[str, str] = {}
    if not env_path.is_file():
        return out
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and val:
            out[key] = val
    return out


def _iter_payloads(exe: Path) -> Iterator[tuple[str, bytes]]:
    """遍历归档里的**全部解压后内容**（数据文件 + PYZ 模块字节码）。

    产出 `(条目名, 字节)`。
    """
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    arch = CArchiveReader(str(exe))
    pyz_entries: list[str] = []

    for name in list(arch.toc):
        typ = arch.toc[name][-1]
        if typ == "z":                     # PYZ 归档，稍后单独处理
            pyz_entries.append(name)
            continue
        data = arch.extract(name)
        if isinstance(data, bytes):
            yield name, data

    for pname in pyz_entries:
        raw = arch.extract(pname)
        if not isinstance(raw, bytes):
            continue
        fd, tmp = tempfile.mkstemp(suffix=".pyz")
        os.close(fd)
        try:
            Path(tmp).write_bytes(raw)
            zarch = ZlibArchiveReader(tmp)
            for mod in list(zarch.toc):
                code = zarch.extract(mod)
                if code is None:
                    continue
                try:
                    yield f"{pname}:{mod}", marshal.dumps(code)
                except (ValueError, TypeError):
                    continue
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2

    exe = Path(args[0]).resolve()
    if not exe.is_file():
        print(f"[FAIL] 找不到产物: {exe}")
        return 1

    from PyInstaller.archive.readers import CArchiveReader

    size_mb = exe.stat().st_size / (1024 * 1024)
    print("=" * 66)
    print("  打包产物自检")
    print("=" * 66)
    print(f"  产物 : {exe}")
    print(f"  体积 : {size_mb:.1f} MB")

    arch = CArchiveReader(str(exe))
    names = list(arch.toc)
    failed = 0

    # ---------------- 1. 文件名黑名单（决定性检查） ----------------
    print("\n--- 1. 归档内文件名黑名单 ---")
    bad_names = sorted(n for n in names if Path(n).name in _FORBIDDEN_NAMES)
    if bad_names:
        failed += 1
        print(f"  [FAIL] 归档里含密钥文件: {bad_names}")
    else:
        print(f"  [PASS] {len(names)} 个条目中没有任何 .env / api_keys.json / secrets.yaml")

    # ---------------- 2. 内容扫描（解压后） ----------------
    print("\n--- 2. 密钥内容扫描（解压后逐条目） ---")
    env_path = _ROOT / ".env"
    secrets = _read_env_values(env_path)
    targets: dict[str, str] = {}
    if not secrets:
        print(f"  [SKIP] {env_path} 不存在或没有值，改用通用密钥形态扫描")
    else:
        targets = {
            name: val for name, val in secrets.items()
            if len(val) >= _MIN_SECRET_LEN
        }
        short = sorted(set(secrets) - set(targets))
        if short:
            print(f"  [SKIP] 值过短（<{_MIN_SECRET_LEN}）不参与搜索: {short}")

    # 一次遍历同时做两件事（解压是这里最贵的操作，不能跑两遍）
    hits: dict[str, list[str]] = {}
    pattern_hits: list[tuple[str, bytes]] = []
    n_payloads = 0
    for entry, blob in _iter_payloads(exe):
        n_payloads += 1
        for name, val in targets.items():
            if val.encode("utf-8") in blob:
                hits.setdefault(name, []).append(entry)
        for pat in _SECRET_PATTERNS:
            for m in pat.finditer(blob):
                pattern_hits.append((entry, m.group(0)))

    if hits:
        failed += 1
        for name, where in hits.items():
            print(f"  [FAIL] {name} 的真实值出现在: {where[:5]}")
    elif targets:
        print(f"  [PASS] 扫描 {n_payloads} 个条目，"
              f"{len(targets)} 个密钥值均未出现")

    if pattern_hits:
        failed += 1
        for entry, tok in pattern_hits[:8]:
            print(f"  [FAIL] 疑似硬编码密钥 {tok[:12]!r}… 出现在: {entry}")
    else:
        print(f"  [PASS] {n_payloads} 个条目里没有任何 `sk-` 形态的密钥串")

    # ---------------- 3. 内置默认配置 ----------------
    print("\n--- 3. 内置资源 ---")
    if "config.yaml" not in names:
        failed += 1
        print("  [FAIL] 归档里没有 config.yaml，首启无法自愈")
    else:
        raw = arch.extract("config.yaml")
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""
        missing = [k for k in ("livetranslate", "overlay", "hotkeys", "audio")
                   if k not in text]
        if missing:
            failed += 1
            print(f"  [FAIL] config.yaml 内容不完整，缺少: {missing}")
        else:
            print(f"  [PASS] config.yaml 已内置且内容完整（{len(raw)} 字节）")
        # 热词表是外部文件（phrases_file 相对引用）——必须在包里，
        # 否则首启 ensure_user_files() 铺不出来 → 加载配置时 fail fast。
        if "phrases_file: terms.yaml" not in text:
            print("  [WARN] config.yaml 未引用 terms.yaml（热词表可能改回内联了）")

    if "terms.yaml" not in names:
        failed += 1
        print("  [FAIL] 归档里没有 terms.yaml —— 首启会因"
              "「热词表文件不存在」直接 fail fast")
    else:
        raw_t = arch.extract("terms.yaml")
        text_t = (raw_t.decode("utf-8", errors="replace")
                  if isinstance(raw_t, bytes) else "")
        n_terms = sum(1 for ln in text_t.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")
                      and ":" in ln)
        if n_terms < 100:
            failed += 1
            print(f"  [FAIL] terms.yaml 条目过少（{n_terms}），疑似没打全")
        else:
            print(f"  [PASS] terms.yaml 已内置（{n_terms} 条 / {len(raw_t)} 字节）")

    # ---------------- 4. 体积合理性 ----------------
    print("\n--- 4. 体积合理性 ---")
    if size_mb < _SIZE_MIN_MB:
        failed += 1
        print(f"  [FAIL] 体积过小（{size_mb:.1f} < {_SIZE_MIN_MB} MB），可能漏打依赖")
    elif size_mb > _SIZE_MAX_MB:
        failed += 1
        print(f"  [FAIL] 体积过大（{size_mb:.1f} > {_SIZE_MAX_MB} MB），"
              f"检查 trans.spec 的 excludes")
    else:
        print(f"  [PASS] {size_mb:.1f} MB 在 {_SIZE_MIN_MB}~{_SIZE_MAX_MB} MB 之间")

    print()
    print("=" * 66)
    if failed:
        print(f"  结果: {failed} 项失败 —— 不要发布这个产物")
    else:
        print("  结果: 全部通过 —— 可以发布")
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
