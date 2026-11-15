#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_header.py — 填 GB ROM 头并计算校验和（替代缺失的 rgbfix）。

输入：makebin 产出的 32KB raw ROM（$0100 之前是 crt0.s 的向量和入口）。
把 $0104-$014F 的 cart header 填好：
  - $0104-$0133 Nintendo logo（标准 48 字节，GB 兼容所必需）
  - $0134-$0143 title "EZJRMINIKERNEL"
  - $0147/$0148 cart type / ROM size（32KB 写 ROM ONLY，>32KB 写 MBC5 + 容量）
  - $0149 RAM size 0x00
  - $014D header checksum
  - $014E-$014F global checksum

用法: python3 tools/fix_header.py build/ezgb.raw build/ezgb.dat
      python3 tools/fix_header.py --verify build/ezgb.dat
"""
import sys

# 标准 GB boot logo（公有领域：任天堂 boot ROM 校验用的固定图形，各开源
# GB 工程普遍内嵌这一常量；它不是可执行代码）。
NINTENDO_LOGO = bytes.fromhex(
    "CE ED 66 66 CC 0D 00 0B 03 73 00 83 00 0C 00 0D"
    "00 08 11 1F 88 89 00 0E DC CC 6E E6 DD DD D9 99"
    "BB BB 67 63 6E 0E EC CC DD DC 99 9F BB B9 33 3E"
)

TITLE = b"EZJRMINIKERNEL"   # 14 bytes, $0134-$0141
CGB_FLAG = 0x00             # DMG 兼容（真机 FPGA 只给 DMG 模式）


def fix(data: bytearray) -> bytearray:
    n = len(data)
    assert n in (0x8000, 0x10000, 0x20000, 0x40000), f"ROM 应为 32/64/128/256KB，实际 {n}"
    banks = n // 0x4000
    # logo
    data[0x0104:0x0134] = NINTENDO_LOGO
    # title 区（$0134-$0143）先清零再填
    data[0x0134:0x0144] = b"\x00" * 16
    data[0x0134:0x0134 + len(TITLE)] = TITLE
    data[0x0143] = CGB_FLAG
    data[0x0144:0x0146] = b"\x00\x00"   # new licensee
    data[0x0146] = 0x00                 # SGB flag
    # cart type / ROM size：>32KB 写 MBC5 + 对应容量，这是 64KB ROM 的规范编码，
    # 也是 PyBoy 之类模拟器选对 mapper 的依据。真机侧 stage1 按**文件大小**加载、
    # 不读这两个字段（上游 docs/kernel-size.md 明说，官方 160KB 内核 $0148 仍是
    # 0x00），所以写规范值只为模拟器，不影响真机加载。
    if banks > 2:
        data[0x0147] = 0x19             # MBC5
        data[0x0148] = {4: 0x01, 8: 0x02, 16: 0x03}.get(banks, 0x00)
        assert data[0x0148], f"{banks} bank 的 ROM size 编码没定义"
    else:
        data[0x0147] = 0x00             # ROM ONLY
        data[0x0148] = 0x00             # 32KB
    data[0x0149] = 0x00                 # RAM size: none
    data[0x014A] = 0x00                 # destination: Japan
    data[0x014B] = 0x33                 # old licensee
    data[0x014C] = 0x00                 # mask ROM version
    # header checksum: $0134..$014C
    chk = 0
    for a in range(0x0134, 0x014D):
        chk = (chk - data[a] - 1) & 0xFF
    data[0x014D] = chk
    # global checksum: $0000..$014D 与 $0150..$7FFF 之和（跳过两字节自身位置）
    g = 0
    for a in range(0x0000, len(data)):
        if a in (0x014E, 0x014F):
            continue
        g = (g + data[a]) & 0xFFFF
    data[0x014E] = (g >> 8) & 0xFF
    data[0x014F] = g & 0xFF
    return data


def verify(data: bytes) -> bool:
    if len(data) not in (0x8000, 0x10000, 0x20000, 0x40000):
        print(f"  大小错误: {len(data)}"); return False
    if data[0x0104:0x0134] != NINTENDO_LOGO:
        print("  Nintendo logo 不符"); return False
    chk = 0
    for a in range(0x0134, 0x014D):
        chk = (chk - data[a] - 1) & 0xFF
    if chk != data[0x014D]:
        print(f"  header checksum 不符: 算得 {chk:02X}, 文件 {data[0x014D]:02X}"); return False
    g = 0
    for a in range(len(data)):
        if a in (0x014E, 0x014F): continue
        g = (g + data[a]) & 0xFFFF
    if g != ((data[0x014E] << 8) | data[0x014F]):
        print(f"  global checksum 不符: 算得 {g:04X}"); return False
    print(f"  OK: {len(data)//1024}KB, logo 正确, header chk={chk:02X}, global chk={g:04X}")
    print(f"  title={data[0x0134:0x0142].decode('ascii','replace').rstrip(chr(0))} "
          f"type={data[0x0147]:02X} rom={data[0x0148]:02X}")
    return True


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--verify":
        return 0 if verify(open(sys.argv[2], "rb").read()) else 1
    if len(sys.argv) != 3:
        print(__doc__); return 2
    data = bytearray(open(sys.argv[1], "rb").read())
    fix(data)
    open(sys.argv[2], "wb").write(data)
    print(f"已写 {sys.argv[2]} ({len(data)} 字节)")
    return 0 if verify(bytes(data)) else 1


if __name__ == "__main__":
    sys.exit(main())
