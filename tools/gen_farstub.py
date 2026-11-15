#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_farstub.py — 生成跨 bank 调用的桩表（farstub.s），并做布局体检。

一个功能组一个 bank 之后，常驻段（bank0/1）要调用可切 bank（bank2+）里的函数，
中间必须经过「切 bank → 调用 → 恢复 bank」的桩。桩由本脚本生成，不手写。

用法：
    # 分析：哪些符号需要桩（读各模块 .rel 的定义/引用记录）
    python3 tools/gen_farstub.py --analyze

    # 生成：读 build/obj/banksym.txt（各 bank map 里的 _CODE 符号地址）
    python3 tools/gen_farstub.py --sym build/obj/banksym.txt \
        --exports tools/far_exports.txt --out build/obj/farstub.s

    # 体检：可切 bank 直接引用了别的可切 bank 的符号 → 报错
    python3 tools/gen_farstub.py --check build/obj/*.rel

设计说明见 docs/12-bank-layout.md §4、§5.1。
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------- banks.conf
def load_banks(path=None):
    """-> {module_stem: bank}；bank 值 0 = 常驻"""
    path = path or os.path.join(HERE, "banks.conf")
    mods = {}
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = line.split()
            bank = int(parts[0])
            for src in parts[1:]:
                stem = os.path.basename(src)
                if stem.endswith(".c"):
                    stem = stem[:-2]
                mods[stem] = bank
    return mods


# ---------------------------------------------------------------- .rel 解析
def parse_rel(path):
    """-> (defs, refs)；defs: {symbol: (segment, offset)}，refs: set(symbol)"""
    defs, refs = {}, set()
    with open(path, encoding="latin-1", errors="ignore") as f:
        for line in f:
            p = line.split()
            if len(p) < 3 or p[0] != "S":
                continue
            name, tag = p[1], p[2]
            if tag.startswith("Def"):
                defs[name] = int(tag[3:], 16)
            elif tag.startswith("Ref"):
                refs.add(name)
    return defs, refs


def strip_us(name):
    return name[1:] if name.startswith("_") else name


# ---------------------------------------------------------------- 函数/变量区分
# .rel 的 S 记录只有段内偏移，分不出函数和变量；头文件的原型是唯一可靠来源。
# 只给函数建桩：RAM 变量在 $C000 与 bank 无关，ROM 常量（字模）走 -g 固定地址。
PROTO_RE = re.compile(r"^\s*(?:extern\s+)?[A-Za-z_][\w\s\*]*?\b([A-Za-z_]\w*)\s*\([^;{]*\)\s*;")


def scan_header_functions(src_dir=None):
    src_dir = src_dir or os.path.join(ROOT, "src")
    funcs = set()
    for path in glob.glob(os.path.join(src_dir, "*.h")):
        text = open(path, encoding="utf-8", errors="ignore").read()
        # 去掉块注释与预处理行，避免把 #define 里的括号当原型
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            m = PROTO_RE.match(line)
            if m:
                funcs.add(m.group(1))
    # fatw.c 用 `#define dir_seek fatw_dir_seek` 把实现改名成 fatw_ 前缀，
    # 头文件里声明的是宏名，真实符号是右边那个 —— 别名要跟着算函数。
    for path in glob.glob(os.path.join(src_dir, "*.c")):
        for line in open(path, encoding="utf-8", errors="ignore"):
            m = re.match(r"\s*#define\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*$", line)
            if m and m.group(1) in funcs:
                funcs.add(m.group(2))
    return funcs


# ---------------------------------------------------------------- map 解析
AREA_RE = re.compile(r"^(_[A-Z0-9]+)\s+([0-9A-F]{8})\s+([0-9A-F]{8})")
SYM_RE = re.compile(r"^\s+([0-9A-F]{8})\s+(\S+)")


def parse_map_code_syms(path, area="_CODE"):
    """从 map 里取指定段的全局符号 -> {symbol: addr}

    只在目标段的区块内取符号，所以 _DATA/_BSS 里的变量不会被误当成函数。
    """
    out = {}
    cur = None
    with open(path, encoding="latin-1", errors="ignore") as f:
        for line in f:
            m = AREA_RE.match(line)
            if m:
                cur = m.group(1)
                continue
            if line.startswith("Files Linked") or line.startswith("User Base"):
                cur = None
                continue
            if cur != area:
                continue
            m = SYM_RE.match(line)
            if m and m.group(2).startswith("_"):
                out.setdefault(m.group(2), int(m.group(1), 16))
    return out


# ---------------------------------------------------------------- analyze
def compute_need(rel_dir, only_resident=False):
    """-> {symbol: (def_bank, [user modules])}；只算真正被引用的跨 bank 函数

    only_resident=True 时只统计 bank0/1 的引用者 —— 链接 A 只含常驻段，
    给「只有可切 bank 才引用」的符号 -g 会被 ASlink 判成 No definition。
    """
    banks = load_banks()
    funcs = scan_header_functions()
    # far_exports.txt 是人工维护的函数清单，里面的符号无条件算函数 ——
    # fatw_* 那批在头文件里是宏名（#define fat_alloc fatw_alloc），
    # 靠原型反查不到真实符号，只能靠清单声明。
    try:
        funcs |= {n for n, _ in parse_exports(os.path.join(HERE, "far_exports.txt"))}
    except FileNotFoundError:
        pass
    owner = {}          # symbol -> module
    defs_by_mod = {}
    refs_by_mod = {}
    for path in sorted(glob.glob(os.path.join(rel_dir, "*.rel"))):
        mod = os.path.basename(path)[:-4]
        defs, refs = parse_rel(path)
        defs_by_mod[mod] = defs
        refs_by_mod[mod] = refs
        for s in defs:
            owner[s] = mod

    need = {}           # symbol -> (def_bank, [user modules])
    unknown = set()
    for mod, refs in refs_by_mod.items():
        if mod not in banks:
            continue
        if only_resident and banks[mod] > 1:
            continue
        for s in refs:
            d = owner.get(s)
            if d is None:
                unknown.add(s)
                continue
            if banks.get(d) is None or banks.get(mod) is None:
                continue
            # bank0 常驻（$0000-$3FFF 永远可见）：谁调它都不用桩
            if banks[d] == 0:
                continue
            # bank0<->bank1 在同一个 32KB 镜像里，互相引用不需要桩
            if banks[d] <= 1 and banks[mod] <= 1:
                continue
            # 只要函数；RAM 变量（$C000）与字模常量不走桩
            if strip_us(s) not in funcs:
                continue
            need.setdefault(s, (banks[d], []))[1].append(mod)

    return need


def cmd_analyze(rel_dir):
    need = compute_need(rel_dir)
    print(f"{'symbol':28s} {'bank':>4s}  使用者")
    for s in sorted(need, key=lambda x: (need[x][0], x)):
        b, users = need[s]
        print(f"{strip_us(s):28s} {b:4d}  {', '.join(sorted(set(users)))}")
    print(f"\n共 {len(need)} 个符号需要桩（每个 7 字节：call 3 + 数据 4）")
    lib = sorted(strip_us(s) for s in unknown
                 if strip_us(s) in ("memset", "memcpy", "memcmp", "strlen")
                 or strip_us(s).startswith(("_div", "_mod", "_mul")))
    other = sorted(strip_us(s) for s in unknown if strip_us(s) not in lib)
    if lib:
        print("C 库（每个 bank 各链一份，不需要桩）：" + ", ".join(lib))
    if other:
        print("未找到定义的符号（检查是否 static 或漏编）：" + ", ".join(other))
    return need


# ---------------------------------------------------------------- 生成
def parse_banksym(path):
    """banksym.txt: <symbol> <addr> <bank>"""
    out = {}
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            name, addr, bank = line.split()
            out[name] = (int(addr, 16), int(bank))
    return out


def parse_exports(path):
    """far_exports.txt: <symbol> [bank]；地址一定从 banksym 查"""
    out = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            p = line.split()
            out.append((p[0], int(p[1]) if len(p) > 1 else None))
    return out


def cmd_gen(sym_path, exports_path, out_path, dummy=False):
    """dummy=True 时生成同样大小、同样标签的哑桩（全 0），给「链接 A」占位用。

    位置固定：桩表紧跟在 crt0.rel 之后，也就是 $0400 起 —— 这样可切 bank 的
    链接不必等主链接算地址，桩表起点恒为 $0400，两边布局必然一致。
    """
    syms = parse_banksym(sym_path) if os.path.exists(sym_path) else {}
    exports = parse_exports(exports_path)
    lines = [
        "; farstub.s — 由 tools/gen_farstub.py 自动生成，请勿手改",
        "; 每个桩 7 字节：call __farcall（3）+ 目标地址与 bank（4）",
        "; 桩必须排在归档文件的最后：链接时放在 rel 列表末尾，",
        "; 这样 bank0 里已有符号的地址不会因为桩表变长而后移。",
        "",
        "        .module farstub",
        "        .area   _CODE",
        "",
    ]
    missing = []
    for name, want_bank in exports:
        if dummy:
            # 标签规则必须和真桩一致：bank1 的函数在主链接里有真定义，
            # 桩只能叫 _fars_xxx（否则 Multiple definition）。
            key = name if name.startswith("_") else "_" + name
            label = key if (want_bank is not None and want_bank >= 2) else "_fars_" + key[1:]
            lines += [f"{label}::", "        .db 0,0,0,0,0,0,0", ""]
            continue
        key = name if name.startswith("_") else "_" + name
        if key not in syms:
            missing.append(name)
            continue
        addr, bank = syms[key]
        if want_bank is not None and bank != want_bank:
            sys.exit(f"错误：{name} 在 banksym 里属于 bank{bank}，清单写的是 bank{want_bank}")
        if not (0x4000 <= addr <= 0x7FFF):
            sys.exit(f"错误：{name} 地址 ${addr:04X} 不在 $4000-$7FFF，不能 far call")
        # bank1 的函数在主链接里已有真实定义（常驻段自己 call 它走直达），
        # 桩只能换个名字，由可切换 bank 的 -g 指过来；bank2+ 的函数没有真定义，
        # 桩就用原名 —— 常驻段的 call 直接落到桩上。
        label = key if bank >= 2 else "_fars_" + key[1:]
        if dummy:
            lines += [f"{label}::", "        .db 0,0,0,0,0,0,0", ""]
        else:
            lines += [
                f"{label}::",
                "        call    __farcall",
                f"        .db     0x{addr & 0xFF:02X}, 0x{addr >> 8:02X}, 0x{bank:02X}, 0x00",
                "",
            ]
    if missing and not dummy:
        sys.exit("错误：清单里的符号在 banksym 里找不到（拼错？或没编进可切 bank）："
                 + ", ".join(missing))
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  桩表：{len(exports)} 个桩 → {os.path.relpath(out_path, ROOT)}"
          f"（{len(exports) * 7} 字节）")


# ---------------------------------------------------------------- 体检
def cmd_check(rel_paths):
    banks = load_banks()
    owner = {}
    for path in rel_paths:
        defs, _ = parse_rel(path)
        mod = os.path.basename(path)[:-4]
        for s in defs:
            owner[s] = mod
    bad = []
    for path in rel_paths:
        mod = os.path.basename(path)[:-4]
        if banks.get(mod, 0) <= 1:
            continue
        _, refs = parse_rel(path)
        for s in refs:
            d = owner.get(s)
            if d is None or banks.get(d) is None:
                continue
            if banks[d] > 1:
                bad.append((mod, strip_us(s), banks[d]))
    if bad:
        print("  体检失败：可切 bank 直接引用了别的可切 bank 的符号（应改走桩）")
        for mod, s, b in bad:
            print(f"    {mod} → {s}（bank{b}）")
        return 1
    print("  体检通过：可切 bank 之间没有直接引用")
    return 0


def main(argv):
    if "--placeholder" in argv:
        # 链接 A 用：把可切 bank 的函数先钉在 $4000 占位，好让常驻段的链接跑通、
        # 把 RAM 与 bank0/1 的地址算出来。这些值不会进最终产物。
        # ASlink 的 -g 不能给「没人引用」的符号：那样会报 No definition。
        # 所以只占位真正被常驻段引用的那些（compute_need 算出来的集合）。
        rel_dir = os.path.join(ROOT, "build/obj/m")
        for a in argv:
            if os.path.isdir(a):
                rel_dir = a
        need = compute_need(rel_dir, only_resident=True)
        for sym in sorted(need):
            if need[sym][0] >= 2:
                print(f"-g {sym} = 0x4000")
        return 0
    if "--analyze" in argv:
        i = argv.index("--analyze")
        rel_dir = argv[i + 1] if len(argv) > i + 1 else os.path.join(ROOT, "build/obj")
        cmd_analyze(rel_dir)
        return 0
    if "--check" in argv:
        i = argv.index("--check")
        return cmd_check(argv[i + 1:])
    if "--sym" in argv or "--dummy" in argv:
        def opt(name, default=None):
            if name in argv:
                i = argv.index(name)
                if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    return argv[i + 1]
            return default
        cmd_gen(opt("--sym", ""),
                opt("--exports", os.path.join(HERE, "far_exports.txt")),
                opt("--out", os.path.join(ROOT, "build/obj/farstub.s")),
                dummy="--dummy" in argv)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
