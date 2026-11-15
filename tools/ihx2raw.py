#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ihx2raw.py — Intel HEX -> 32KB 原始镜像，只保留 $0000-$7FFF。

为什么不直接用 makebin：可切 bank 单独链接时，RAM 段（-b _DATA = $D5xx）会
在 ihx 里留下记录，makebin -s 32768 遇到超出范围的地址会报
"size of the buffer is too small" 而拒绝输出。而 RAM 段的内容**本来就不该进
ROM 镜像**（运行时由 crt0 的 gsinit 从 _INITIALIZER 复制），直接丢掉即可。

用法: python3 tools/ihx2raw.py <in.ihx> <out.raw>
"""
import sys

SIZE = 0x8000      # 32KB：bank0 + bank1（或单个可切 bank 的窗口）
FILL = 0xFF


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]

    rom = bytearray([FILL]) * SIZE
    dropped = 0
    with open(src) as f:
        for line in f:
            line = line.strip()
            if not line.startswith(":"):
                continue
            b = bytes.fromhex(line[1:])
            n, addr, typ = b[0], (b[1] << 8) | b[2], b[3]
            if typ != 0x00:
                continue                      # EOF / 扩展地址记录：忽略
            data = b[4:4 + n]
            if addr >= SIZE:
                dropped += len(data)          # RAM 段的记录，不进 ROM
                continue
            if addr + n > SIZE:
                n = SIZE - addr
                data = data[:n]
            rom[addr:addr + n] = data

    open(dst, "wb").write(rom)
    used = sum(1 for x in rom if x != FILL)
    print(f"  {dst.rsplit('/', 1)[-1]}：有效 {used} 字节"
          + (f"，丢弃 RAM 段 {dropped} 字节" if dropped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
