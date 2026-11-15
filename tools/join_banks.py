#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
join_banks.py — 把主镜像与各扩展 bank 拼成 N×16KB 的 ezgb.dat。

为什么要两步链接：SDCC 的 sdldgb 允许多个 bank 段都定位到 $4000（不报错），
但 makebin 只是按地址填字节，同址的两组记录后写覆盖前写——它不会像 rgbds
那样按 BANK[n] 自动拆成物理 bank。所以每个扩展 bank 分开链接、分开 makebin，
再由本脚本按物理偏移拼回去。

布局（与上游一致，见 docs/fpga-stage1.md）：
    bank N 在文件偏移 N × $4000
    文件 $0000-$7FFF  bank0（_HOME/_CODE）+ bank1（_BANK1），由主链接产出
    文件 $8000 起      bank2、bank3…（各自独立链接），空 bank 填 $FF

用法:
    python3 tools/join_banks.py <out> <总bank数> <主镜像32KB> [bank:文件 ...]

    例：python3 tools/join_banks.py build/ezgb128.raw 8 build/ezgb.raw \
            2:build/bank2.raw 3:build/bank3.raw 5:build/font.raw
"""
import sys


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    out, total = sys.argv[1], int(sys.argv[2])
    main_bin = sys.argv[3]
    ext = []
    for a in sys.argv[4:]:
        n, p = a.split(":", 1)
        ext.append((int(n), p))

    m = open(main_bin, "rb").read()
    if len(m) != 0x8000:
        sys.exit(f"主镜像应为 32KB，实际 {len(m)}")
    for n, p in ext:
        if not (1 <= n < total):
            sys.exit(f"bank 号 {n} 超出 0..{total - 1}")

    rom = bytearray(b"\xFF" * (total * 0x4000))
    rom[0x0000:0x8000] = m

    info = []
    for n, p in ext:
        d = open(p, "rb").read()
        if len(d) != 0x8000:
            sys.exit(f"bank{n} 镜像应为 32KB（含空的前半），实际 {len(d)}")
        body = d[0x4000:0x8000]
        if not any(body):
            sys.exit(f"bank{n} 的 $4000-$7FFF 全空——该段没有内容，检查 --codeseg")
        rom[n * 0x4000:(n + 1) * 0x4000] = body
        info.append(f"bank{n}={sum(1 for x in body if x != 0xFF)} 字节")

    open(out, "wb").write(rom)
    empty = [str(n) for n in range(2, total) if n not in {b for b, _ in ext}]
    print(f"  拼接 {total} bank / {total * 16}KB：bank0+bank1=主镜像，"
          + "，".join(info) + (f"，空 bank={'/'.join(empty)}" if empty else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
