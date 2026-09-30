"""列出系统音频设备，用于填写 config.yaml 的 audio.device_keyword。

用法:
    python -m src.tools.list_devices
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.audio.device import (  # noqa: E402
    list_input_devices,
    list_loopback_devices,
)
from src.errors import AppError  # noqa: E402


def main() -> int:
    print("=" * 72)
    print("  Windows 音频设备列表")
    print("=" * 72)

    try:
        default_dev = None
        try:
            from src.audio.device import get_default_loopback

            default_dev = get_default_loopback()
        except AppError:
            pass

        print("\n[扬声器 / Loopback]  ← 抓取系统播放音频用这些")
        print("-" * 72)
        loops = list_loopback_devices()
        if not loops:
            print("  (无)")
        for i, d in enumerate(loops, 1):
            mark = "  ← 默认" if default_dev and d.id == default_dev.id else ""
            print(f"  {i}. {d}{mark}")
            print(f"      device_keyword 可填: \"{d.name}\"")

        print("\n[麦克风 / Input]  ← 非主路径，仅备用")
        print("-" * 72)
        ins = list_input_devices()
        if not ins:
            print("  (无)")
        for i, d in enumerate(ins, 1):
            print(f"  {i}. {d}")

        print("\n" + "=" * 72)
        print(f"  共 {len(loops)} 个 loopback 设备, {len(ins)} 个输入设备")
        print("=" * 72)
        print(
            "\n使用提示：\n"
            "  · 抓取系统音频（推荐）: config.yaml 中 audio.backend = wasapi_loopback\n"
            "    audio.device_keyword 留空即使用默认扬声器\n"
            "  · 想指定设备，把上面 device_keyword 的值填进去即可\n"
        )
        return 0

    except AppError as e:
        print(f"\n[错误] {e}\n", file=sys.stderr)
        if e.detail:
            print(f"  {e.detail}\n", file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"\n[错误] 缺少依赖: {e}", file=sys.stderr)
        print("  请先执行: pip install -r requirements.txt\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
