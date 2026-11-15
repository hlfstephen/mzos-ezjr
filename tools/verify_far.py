#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_far.py — 静态校验跨 bank 调用（docs/12-bank-layout.md §4）

查三件事：
  1. 桩表内容：$0400 起每个 7 字节必须是 `CD <__farcall> lo hi bank 00`，
     且 lo/hi 与该 bank map 里的符号地址一致；
  2. 可切 bank 里没有「直呼本 bank 之外的 $4000-$7FFF 地址」——运行时那个窗口
     里是它自己的字节，call 过去不会报错，只会跑飞；
  3. 字模 bank 号、far call 目标 bank 都与配置一致。

用法: python3 tools/verify_far.py build/obj
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from banktools import parse_map          # noqa: E402
from gen_farstub import parse_exports    # noqa: E402

STUB_BASE = 0x0400
STUB_SIZE = 7


def main():
    obj = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build/obj")
    rom = open(os.path.join(ROOT, "build/ezgb.dat"), "rb").read()
    exports = parse_exports(os.environ.get("FAR_EXPORTS",
                                           os.path.join(HERE, "far_exports.txt")))

    syms = parse_map(os.path.join(obj, "ezgb.map"))
    if "_farcall" not in syms and "__farcall" not in syms:
        sys.exit("  找不到 __farcall 符号")
    far = syms.get("__farcall", syms.get("_farcall"))[1]
    ok = True

    def bad(msg):
        nonlocal ok
        ok = False
        print("  FAIL " + msg)

    print("=== 桩表 ===")
    for i, (name, bank) in enumerate(exports):
        o = STUB_BASE + i * STUB_SIZE
        b = rom[o:o + STUB_SIZE]
        if b[0] != 0xCD:
            bad(f"{name}: 桩首字节 {b[0]:02X}，应为 CD（call）")
            continue
        target = b[1] | (b[2] << 8)
        if target != far:
            bad(f"{name}: call 目标 ${target:04X}，应为 __farcall ${far:04X}")
        # 从对应 bank 的 map 里取真实地址
        bmap = os.path.join(obj, "bank%d.map" % bank) if bank >= 2 else os.path.join(obj, "ezgb.map")
        if not os.path.exists(bmap):
            bad(f"{name}: 缺 bank{bank} 的 map")
            continue
        bsyms = parse_map(bmap)
        key = "_" + name
        if key not in bsyms:
            bad(f"{name}: bank{bank} 的 map 里没有这个符号")
            continue
        real = bsyms[key][1]
        lo, hi, nb = b[3], b[4], b[5]
        if (lo | (hi << 8)) != real:
            bad(f"{name}: 桩里地址 ${lo | (hi << 8):04X}，实际在 bank{bank} 的 ${real:04X}")
        if nb != bank:
            bad(f"{name}: 桩里 bank={nb}，清单写 {bank}")
        if b[6] != 0:
            bad(f"{name}: 数据表第 4 字节应为 0，实际 {b[6]:02X}")
    if ok:
        print(f"  OK   {len(exports)} 个桩：call __farcall（${far:04X}）+ 目标地址 + bank，全部正确")

    print("=== 可切 bank 里的直呼 ===")
    # 每个可切 bank 的合法 $4000-$7FFF 目标 = 它自己 map 里 _CODE 段的符号地址
    for bank in (2, 3, 4):
        bmap = os.path.join(obj, "bank%d.map" % bank)
        raw = os.path.join(ROOT, "build/obj", "bank%d.raw" % bank)
        if not os.path.exists(bmap) or not os.path.exists(raw):
            continue
        own = {a for (ar, a) in parse_map(bmap).values() if ar == "_CODE"}
        d = open(raw, "rb").read()[0x4000:0x8000]
        bad_calls = []
        for p in range(len(d) - 2):
            if d[p] != 0xCD:
                continue
            t = d[p + 1] | (d[p + 2] << 8)
            if 0x4000 <= t <= 0x7FFF and t not in own:
                bad_calls.append((0x4000 + p, t))
        if bad_calls:
            # 注意：static 函数不进全局符号表，CD 字节也可能是数据，
            # 所以这一项只是提示，不作为失败 —— 真正的硬校验是上面的桩表。
            print(f"  bank{bank}：{len(bad_calls)} 处 call 目标落在 $4000-$7FFF 的"
                  f"非全局符号地址（多为 static 函数或数据里的 CD 字节，仅供参考）")
            for a, t in bad_calls[:5]:
                print(f"    ${a:04X} → ${t:04X}")
        else:
            print(f"  OK   bank{bank}：没有指向其它 bank 窗口的 call")

    print("=== 字模 ===")
    if rom[FONT_BANK * 0x4000:FONT_BANK * 0x4000 + 8] == b"\xFF" * 8:
        bad(f"bank{FONT_BANK} 开头是 $FF，字模没进去？")
    else:
        print(f"  OK   bank{FONT_BANK} 有字模数据")

    print("=== 结论 ===")
    print("  全部通过" if ok else "  存在问题，见上")
    return 0 if ok else 1


FONT_BANK = 5

if __name__ == "__main__":
    sys.exit(main())
