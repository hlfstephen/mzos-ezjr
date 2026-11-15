#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_banklayout.py — 校验一个可切 bank 的段布局与跨段调用（docs/12-bank-layout.md）

为什么需要这个脚本（v0.16.9 的真机事故）：
    可切 bank 是**独立链接**的，sdld 只知道命令行 -b 过的段，其余段一律排到已
    定位段之后。而第一个已定位段是 RAM 的 `_DATA`（$D5xx），于是 SDCC 放在
    `_HOME` 里的 __divulong / __mullong 被排在 $D500 的**RAM** 上：
      · ihx2raw 只收 $0000-$7FFF，那部分库代码根本没进 ROM；
      · fatw 算簇 LBA（32 位乘除）时 call $D500，执行的是被 crt0 清零的 RAM，
        NOP 接 NOP 一路跑飞 —— 真机上是「正在备份」刚画出来就黑屏。
    这件事**不会**报错、不会链接失败，只会静默跑飞，所以必须有脚本盯着。

查三件事：
  1. 每个段都落在 bank 窗口 $4000-$7FFF（RAM 段落在本 bank 的 $D1xx 区）；
  2. 段不越界、不互相重叠（_HOME 挤进 _CODE 之类）；
  3. 代码里没有 call/jp 到 RAM 里的**符号地址**（跳过去执行的是 0 字节）。

用法：python3 tools/verify_banklayout.py <bankN.map> <bank> <ram_base>
"""
import os
import re
import sys

WINDOW_LO = 0x4000
WINDOW_HI = 0x8000
RAM_LO = 0xC000
RAM_HI = 0xE000
RAM_STRIDE = 0x200

# ROM 段的期望布局（与 build.sh 的 -b 一致）
ROM_LAYOUT = [
    ("HOME",        0x4000, 0x4200),
    ("CODE",        0x4200, 0x7000),
    ("CONST",       0x7000, 0x7800),
    ("INITIALIZER", 0x7800, 0x7900),
    ("GSINIT",      0x7900, 0x7A00),
    ("GSFINAL",     0x7A00, 0x8000),
]
RAM_LAYOUT = ("DATA", "INITIALIZED", "BSS", "HEAP")


def parse_areas(path):
    """从 sdld map 里取 {段名: (起始, 长度)}（段名去掉前导下划线）。"""
    areas = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r"^(_[A-Za-z][\w.]*)\s+([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})\s*=", line)
        if m:
            name = m.group(1).lstrip("_")
            addr, size = int(m.group(2), 16), int(m.group(3), 16)
            areas.setdefault(name, (addr, size))
        elif re.match(r"^_CODE\s*=\s*0x", line):
            pass
    return areas


def parse_ram_symbols(path):
    """map 里所有落在 RAM 范围的符号地址（段内符号 + -g 注入的常量）。

    call/jp 目标落在这些地址上 = 跳到 RAM 里执行，必跑飞。用符号集合而不是
    「所有 $Cxxx 地址」，是为了不把数据里偶然出现的 CD 58 C0 当成指令。"""
    syms = set()
    for line in open(path, encoding="utf-8", errors="replace"):
        m = re.match(r"^\s+([0-9A-Fa-f]{8})\s+\S", line)
        if m:
            a = int(m.group(1), 16)
            if RAM_LO <= a < RAM_HI:
                syms.add(a)
    return syms


def scan_calls(raw_path, ram_syms):
    """扫 $4000-$7FFF 里的 call/jp 目标，返回落在 RAM 符号上的（地址, 目标）。"""
    d = open(raw_path, "rb").read()
    if len(d) < WINDOW_HI:
        return []
    d = d[WINDOW_LO:WINDOW_HI]
    bad = []
    for p in range(len(d) - 2):
        if d[p] in (0xC3, 0xCD):           # jp nn / call nn
            t = d[p + 1] | (d[p + 2] << 8)
            if t in ram_syms:
                bad.append((WINDOW_LO + p, t))
    return bad


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    map_path, bank, ram_base = sys.argv[1], int(sys.argv[2]), int(sys.argv[3], 0)
    check_rom = "--no-rom" not in sys.argv
    rom_raw = os.path.join(os.path.dirname(map_path), "bank%d.raw" % bank)

    ok = True

    def bad(msg):
        nonlocal ok
        ok = False
        print("  FAIL " + msg)

    areas = parse_areas(map_path)
    if not areas:
        sys.exit("  找不到段表：%s" % map_path)

    print("=== bank%d 段布局 ===" % bank)
    seen = {}
    for name, (addr, size) in sorted(areas.items(), key=lambda kv: kv[1][0]):
        if size == 0:
            continue
        end = addr + size
        seen[name] = (addr, end)
        if name in RAM_LAYOUT:
            if not (ram_base <= addr and end <= ram_base + RAM_STRIDE):
                bad(f"_{name} ${addr:04X}-${end:04X} 不在本 bank 的 RAM 区 "
                    f"${ram_base:04X}-${ram_base + RAM_STRIDE - 1:04X}")
            else:
                print(f"  OK   _{name:12s} ${addr:04X}-${end - 1:04X}  RAM")
        elif WINDOW_LO <= addr and end <= WINDOW_HI:
            print(f"  OK   _{name:12s} ${addr:04X}-${end - 1:04X}  余 "
                  f"{WINDOW_HI - end:5d}")
        else:
            bad(f"_{name} ${addr:04X}-${end:04X} 不在 bank 窗口 "
                f"${WINDOW_LO:04X}-${WINDOW_HI - 1:04X} —— 这段代码/数据进不了 ROM")

    for n in RAM_LAYOUT:
        if n in seen and seen[n][0] != ram_base:
            bad(f"_{n} 起点 ${seen[n][0]:04X}，应为 ${ram_base:04X}")

    # _INITIALIZED 有内容 = 有带初值的 static。crt0 只复制主链接那份，
    # 可切 bank 的初值没人搬 —— 静默变成 0。真出现就在这里拦下。
    if "INITIALIZED" in areas and areas["INITIALIZED"][1] > 0:
        bad(f"bank{bank} 有 {areas['INITIALIZED'][1]} 字节 _INITIALIZED："
            f"带初值的静态变量在可切 bank 里不会被初始化")

    prev = None
    for name, lo, hi in ROM_LAYOUT:
        if name not in seen:
            continue
        a, e = seen[name]
        if a < lo or e > hi:
            bad(f"_{name} ${a:04X}-${e - 1:04X} 超出预留区间 ${lo:04X}-${hi - 1:04X}")
        if prev and a < prev[1]:
            bad(f"_{name} 起点 ${a:04X} 与 _{prev[0]} 结束 ${prev[1]:04X} 重叠")
        prev = (name, e)

    if os.path.exists(rom_raw):
        calls = scan_calls(rom_raw, parse_ram_symbols(map_path))
        if calls:
            for a, t in calls[:5]:
                bad(f"${a:04X} call/jp ${t:04X}（RAM 符号，跳过去执行的是 0 字节）")
        else:
            print("  OK   没有 call/jp 指向 RAM 里的符号")

        # 进了窗口还不够，得确认**真的烘进了 ROM**：ihx2raw 只收 $0000-$7FFF，
        # 而 join_banks 把 bankN.raw 的 $4000-$7FFF 摆到 rom[N*0x4000...]。
        # 两边逐字节比一遍，任何「代码在 map 里有地址、ROM 里却没有」的情况都会露馅。
        rom_path = os.path.join(os.path.dirname(os.path.dirname(map_path)),
                                "ezgb.dat")
        if check_rom and os.path.exists(rom_path):
            rom = open(rom_path, "rb").read()
            off = bank * 0x4000
            if len(rom) < off + 0x4000:
                bad(f"ROM 只有 {len(rom)} 字节，装不下 bank{bank}")
            else:
                seg = open(rom_raw, "rb").read()[WINDOW_LO:WINDOW_HI]
                got = rom[off:off + 0x4000]
                diff = [i for i in range(0x4000) if seg[i] != got[i]]
                if diff:
                    bad(f"bank{bank} 窗口与 ROM 有 {len(diff)} 字节不一致"
                        f"（首个 ${WINDOW_LO + diff[0]:04X}）")
                else:
                    print("  OK   bank 窗口与 ROM 逐字节一致")

    print("=== 结论 ===")
    print("  bank%d 布局正确" % bank if ok else "  bank%d 存在问题，见上" % bank)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
