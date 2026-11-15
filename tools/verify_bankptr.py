#!/usr/bin/env python3
"""verify_bankptr.py — 抓「跨 bank 传 ROM 指针」这个只在真机上才炸的坑。

背景（v0.16.12 → v0.16.13）：

  `fat_find_sav_lfn()`（bank1）里有 `fat_find_dir(sav11)`，`sav11` 是
  `static const uint8_t sav11[11]`，链接后落在 **bank1 的 $4000-$7FFF**。
  `fat_find_dir()` 在 bank2，调用要走桩 —— 桩把 $4000 窗口换成 bank2，于是
  `raw11` 指向的 11 个字节变成了 **bank2 的代码**，`memcmp(e, raw11, 11)`
  永远不相等 → 找不到 SAVER 目录 → 读档失败、备份后回读校验失败。

  同一类问题还有 `fat_mkdir(saver11)`：saver11 在 bank2，fat_mkdir 在 bank1，
  切过去之后写进目录项的目录名是垃圾（只在「卡上还没有 SAVER」时触发）。

判据：
  · 本 .asm 属于 bank N（N >= 1 时它的 ROM 落在 $4000-$7FFF）
  · `call _<far 导出>` 之前**紧邻**的一段里（走到上一条 call 为止）有
    `ld de,#_sym`（第 1 个参数）或 `ld hl,#_sym; push hl`（第 2 个起的参数）
  · _sym 在本 .asm 里定义于 ROM 段（_CODE/_BANK1/_CONST/_HOME/…）
  · 桩的目标 bank != N（同一个 bank 的桩只是绕个圈，窗口内容不变）
  ⇒ 报错。

RAM 指针（$C000+，段 _DATA/_BSS/_INITIALIZED）和 bank0 的 ROM 都安全 —— 地址
不随窗口切换而变。

用法：python3 tools/verify_bankptr.py [build/obj/m]
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 链接后落在 $4000-$7FFF 的段（bank >= 1）
ROM_AREAS = ("_CODE", "_BANK1", "_CONST", "_HOME", "_GSINIT", "_GSFINAL",
             "_INITIALIZER", "_CABS")


def bank_of(src):
    """按 tools/banks.conf 查源文件属于哪个 bank。"""
    conf = os.path.join(ROOT, "tools", "banks.conf")
    base = os.path.basename(src)
    for line in open(conf, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if base in [os.path.basename(p) for p in parts[1:]]:
            return int(parts[0])
    return None


def far_targets():
    """far_exports.txt：桩标签 → 目标 bank。"""
    out = {}
    path = os.path.join(ROOT, "tools", "far_exports.txt")
    for line in open(path, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        bank = int(parts[1]) if len(parts) > 1 else None
        out["_" + parts[0]] = bank
        out["_fars_" + parts[0]] = bank
    return out


def scan_asm(path, bank, fars):
    """返回 [(行号, 桩名, 符号, 段)]"""
    cur = None
    syms = {}
    lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
    for i, l in enumerate(lines):
        m = re.match(r"\s*\.area\s+(\S+)", l)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"^(_[\w]+):", l)
        if m and cur:
            syms[m.group(1)] = cur

    bad = []
    for i, l in enumerate(lines):
        m = re.match(r"\s*call\s+(_[\w]+)", l)
        if not m or m.group(1) not in fars:
            continue
        tgt = m.group(1)
        tbank = fars[tgt]
        if tbank is not None and tbank == bank:
            continue                    # 同 bank 的桩：窗口切过去还是自己
        arg = None
        for j in range(i - 1, max(-1, i - 12), -1):
            prev = lines[j]
            if re.match(r"\s*(call|ret|reti|jp)\b", prev):
                break                   # 上一条调用的参数，不是这条的
            mh = re.match(r"\s*ld\s+hl,\s*#(_[\w]+)", prev)
            if mh and i > j + 1 and re.match(r"\s*push\s+hl", lines[j + 1]):
                arg = mh.group(1)       # 第 2 个起的参数
                continue
            md = re.match(r"\s*ld\s+de,\s*#(_[\w]+)", prev)
            if md:
                arg = md.group(1)
                break
        if arg is None:
            continue
        area = syms.get(arg)
        if area and area.startswith(ROM_AREAS) and bank != 0:
            bad.append((i + 1, tgt, arg, area))
    return bad


def main():
    objdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "build", "obj", "m")
    fars = far_targets()
    total = 0
    for name in sorted(os.listdir(objdir)):
        if not name.endswith(".asm"):
            continue
        bank = bank_of(name[:-4] + ".c")
        if bank is None:
            print("  ??   %s：banks.conf 里没登记" % name)
            continue
        for ln, tgt, sym, area in scan_asm(os.path.join(objdir, name), bank, fars):
            total += 1
            print("  FAIL %s:%d  call %s 传 %s（%s，本 bank 的 ROM）"
                  % (name, ln, tgt, sym, area))
    if total:
        print("\n%d 处：跨 bank 传了本 bank 的 ROM 指针。切 bank 后它指向别的"
              " bank 的字节，被调函数读到的内容全错。" % total)
        print("改法：把那段逻辑挪进同一个 bank（各自带常量），或先把常量拷进 RAM。")
        return 1
    print("  OK   没有跨 bank 传递本 bank 的 ROM 指针")
    return 0


if __name__ == "__main__":
    sys.exit(main())
