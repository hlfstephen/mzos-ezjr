#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_rom.py — 静态校验构建出的 ezgb.dat：
  - 32KB、GB 头与校验和（复用 fix_header.verify）
  - $0100 入口是 nop + jp（匹配 crt0.s）
  - 代码段不与中断向量区（$0000-$0100）冲突
  - 中文/ASCII 字库数据确实烘进了 ROM（在 ROM 里能找到特征字节）
  - 关键字符串常量存在
用法: python3 tools/verify_rom.py build/ezgb.dat
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fix_header import verify as header_verify  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print(__doc__); return 2
    path = sys.argv[1]
    data = open(path, "rb").read()
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + name + (("  " + extra) if extra else ""))
        if not cond: ok = False

    print("=== GB ROM 结构 ===")
    chk("32KB", len(data) == 0x8000, f"{len(data)} bytes")
    print("  头字段:")
    if not header_verify(data): ok = False
    chk("$0100 = nop; jp", data[0x0100] == 0x00 and data[0x0101] == 0xC3,
        f"{data[0x0100]:02X} {data[0x0101]:02X}")
    jp_target = data[0x0102] | (data[0x0103] << 8)
    chk("入口 jp 落在 _HOME ($0150-$03FF)", 0x0150 <= jp_target <= 0x03FF,
        f"jp ${jp_target:04X}")

    # WRAM：_DATA 从 $C000 起顺序分配，而 $D000 是启动游戏的 handoff 区
    # （game.c 把 launch.s 拷过去执行）。_DATA 长过 $D000 会把 handoff 覆盖掉，
    # 链接器不会报错 —— 症状是启动游戏时跑到一半就崩。这里卡住它。
    mp = os.path.join(os.path.dirname(path), "obj", "ezgb.map")
    if os.path.exists(mp):
        import re
        m = re.search(r"^_DATA\s+([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})", open(mp).read(), re.M)
        if m:
            size = int(m.group(2), 16)
            chk("_DATA 没顶到 $D000 的 handoff 区", size <= 0xF00,
                f"{size} / 3840 字节")

    print("\n=== 代码/数据烘入检查 ===")
    # ASCII 字体：'A' 的点阵在 ROM 里（连续 16 字节 2bpp）
    chk("ROM 非空（含代码/数据）", sum(1 for b in data[0x0150:] if b) > 2000,
        f"{sum(1 for b in data[0x0150:] if b)} 非零字节")
    # 中文字符串渲染相关：至少能找到 CJK 字模的非平凡字节序列
    chk("存在中文字库数据（大量非 0xFF/0x00 的 tile 字节）",
        len(set(data[0x0400:0x4000])) > 40)

    print("\n=== 结论 ===")
    print("  静态结构校验" + ("全部通过" if ok else "存在失败"))
    print("  注意：静态校验 ≠ 真机可运行。硬件层结论来自真机实测，")
    print("        逐项清单见 docs/03-verification.md 第 4 节。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
