#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
banktools.py — 多 bank 构建的辅助工具（由 build.sh 调用，不手工执行）

子命令：
    syms   <map>                     列出 map 里所有「段 + 符号 + 地址」
    inject <mapA> <out.g> <rel...>   为一批 rel 生成 -g 参数（只注入镜像外的符号）
    stub   <map> <bank> <out.txt>    输出该 bank 的 _CODE 符号（name addr bank）
    cmp    <mapA> <mapB>             断言两次链接的公共符号地址一致

为什么需要这些：可切 bank 要**单独链接**（否则多份代码挤进同一个 $4000），
而它引用的常驻段函数、RAM 变量地址得由主链接先算出来。顺序是：
    链接 A（bank0/1 + 占位）→ 拿地址 → 各 bank 单独链接 → 出桩 → 链接 B（最终）
链接 A 与 B 的 RAM 地址必须一致（_DATA 段只由 bank0/1 的 rel 决定，与桩无关），
用 cmp 兜住这个假设。
"""
import os
import re
import sys

AREA_RE = re.compile(r"^(\S+)\s+([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})\s+=")
# 符号行末尾可能还跟着「定义所在模块」那一列，要容忍
SYM_RE = re.compile(r"^\s+([0-9A-Fa-f]{8})\s+(_\S+)(?:\s+\S+)?\s*$")
SEG_RE = re.compile(r"^[sl]__")


def parse_map(path):
    """-> {symbol: (area, addr)}；同名符号取第一个（全局符号表在 .ABS. 页重复出现）"""
    out = {}
    cur = None
    with open(path, encoding="latin-1", errors="ignore") as f:
        for line in f:
            m = AREA_RE.match(line)
            if m:
                cur = m.group(1)
                continue
            if line.startswith(("Files Linked", "User Base")):
                cur = None
                continue
            m = SYM_RE.match(line)
            if m and cur and cur.startswith("_"):
                sym = m.group(2)
                if SEG_RE.match(sym):
                    continue          # s__CODE / l__BANK1 之类，链接器内部符号
                out[sym] = (cur, int(m.group(1), 16))
    return out


def cmd_syms(map_path):
    for sym, (area, addr) in sorted(parse_map(map_path).items(), key=lambda kv: kv[1][1]):
        print(f"{area:12s} {addr:04X} {sym}")
    return 0


LIB_SYMS = {"_memset", "_memcpy", "__memcpy", "_memcmp", "_strlen"}


def is_lib(sym):
    """C 库符号：每个 bank 链接时都 -l sm83 自带一份，不能再从常驻段注入
    （否则 Redefinition）。"""
    return sym in LIB_SYMS or sym.startswith(("__div", "__mod", "__mul", "__"))


def cmd_inject(map_path, out_path, rel_paths, stub_base=None, exports_path=None):
    syms = parse_map(map_path)
    # 桩表的符号顺序（与 gen_farstub 生成的一致），用来算「别的可切 bank 的函数」
    # 在主链接里那个桩的地址：桩表紧跟在 _CODE 末尾，每个 7 字节。
    order = []
    if exports_path:
        with open(exports_path) as f:
            for line in f:
                p = line.split("#")[0].split()
                if p:
                    order.append(p[0])
    refs, defs = set(), set()
    for p in rel_paths:
        with open(p, encoding="latin-1", errors="ignore") as f:
            for line in f:
                f2 = line.split()
                if len(f2) >= 3 and f2[0] == "S":
                    (defs if f2[2].startswith("Def") else refs).add(f2[1])
    given, missing, via_stub = [], [], 0
    for s in sorted(refs - defs):
        if is_lib(s):
            continue
        name = s.lstrip("_")
        if stub_base is not None and name in order:
            # 清单里的符号一律走桩 —— 包括定义在 bank1 的（真地址在 $4xxx，
            # 可切 bank 运行时那里是它自己，直接 call 会跳到同址的字节上）。
            # bank0 永远可见，所以桩本身不用先切 bank 就能 call。
            idx = order.index(name)
            given.append(f"-g {s} = 0x{int(stub_base, 16) + idx * 7:04X}")
            via_stub += 1
        elif s in syms:
            given.append(f"-g {s} = 0x{syms[s][1]:04X}")
        else:
            missing.append(s)
    with open(out_path, "w") as f:
        f.write("\n".join(given) + ("\n" if given else ""))
    lib = [s for s in missing if s in ("_memset", "_memcpy", "__memcpy", "_memcmp", "_strlen")
           or s.startswith(("__div", "__mod", "__mul"))]
    other = [s for s in missing if s not in lib]
    print(f"    注入 {len(given)} 个符号地址（其中 {via_stub} 个走桩），"
          f"C 库 {len(lib)} 个（自带）")
    if other:
        sys.exit("    错误：这些符号既不在常驻段也不在 C 库里：" + ", ".join(other))
    return 0


def cmd_stub_base(map_path):
    """桩表起点 = _CODE 区起始 + 长度（farstub.rel 排在最后，紧接 _CODE 末尾）"""
    with open(map_path, encoding="latin-1", errors="ignore") as f:
        for line in f:
            m = re.match(r"^_CODE\s+([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})", line)
            if m:
                print(f"0x{int(m.group(1), 16) + int(m.group(2), 16):04X}")
                return 0
    sys.exit("map 里找不到 _CODE 区")


def cmd_stub(map_path, bank, out_path, area="_CODE"):
    syms = parse_map(map_path)
    n = 0
    with open(out_path, "a") as f:
        for sym, (a, addr) in sorted(syms.items(), key=lambda kv: kv[1][1]):
            if a != area:
                continue
            if not (0x4000 <= addr <= 0x7FFF):
                sys.exit(f"    错误：{sym} 在 {area} 段但地址 ${addr:04X} 不在 $4000-$7FFF")
            f.write(f"{sym} {addr:04X} {bank}\n")
            n += 1
    print(f"    bank{bank} 的 {area} 符号 {n} 个")
    return 0


def cmd_cmp(map_a, map_b):
    a, b = parse_map(map_a), parse_map(map_b)
    # 桩表插在库代码之前，库符号（memset/除法 helper）会整体后移，这不影响
    # 可切 bank（它们各自 -l sm83 自带一份），所以一致性只比自家符号。
    common = {s for s in set(a) & set(b) if not is_lib(s)}
    bad = [s for s in common if a[s][1] != b[s][1]]
    if bad:
        print(f"  错误：链接 A 与链接 B 的 {len(bad)} 个符号地址不一致（桩表改变了已有布局？）")
        for s in sorted(bad)[:8]:
            print(f"    {s}: A=${a[s][1]:04X} B=${b[s][1]:04X}")
        return 1
    print(f"  链接 A/B 一致性：{len(common)} 个公共符号地址全部相同")
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "syms":
        return cmd_syms(argv[2])
    if cmd == "inject":
        # 环境变量传桩表信息，免得参数解析写两遍：
        #   STUB_BASE  桩表起始地址（bank0 的 _CODE 末尾）
        #   FAR_EXPORTS  桩的符号顺序（与 gen_farstub 一致）
        return cmd_inject(argv[2], argv[3], argv[4:],
                          stub_base=os.environ.get("STUB_BASE"),
                          exports_path=os.environ.get("FAR_EXPORTS"))
    if cmd == "stub":
        return cmd_stub(argv[2], argv[3], argv[4],
                        argv[5] if len(argv) > 5 else "_CODE")
    if cmd == "stub-base":
        return cmd_stub_base(argv[2])
    if cmd == "cmp":
        return cmd_cmp(argv[2], argv[3])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
