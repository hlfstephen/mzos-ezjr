#!/usr/bin/env python3
"""savident_check.py — 静态检查：存档短名只能在 fat_open_sav 之前算一次。

## 为什么需要这个检查

`fat_open_sav()` 会把 g_lfn_buf 里那份 ROM 长名**就地**改成「….sav」
（fatw.c: sav_lfn(lfn, sav)，sav 就是 g_lfn_buf）。这是 2024 年为省 96 字节
缓冲做的设计，本身没错。

错的是「先调 save_dump，再调 save_verify，两次都拿 lfn 算哈希」：

    sav_ident(name, lfn, id)   →  hash("口袋妖怪-水晶.gbc")
    fat_open_sav(...)          →  g_lfn_buf 变成 "口袋妖怪-水晶.sav"
    sav_ident(name, lfn, id)   →  hash("口袋妖怪-水晶.sav")   ← 另一个名字

结果写进去的文件按前一个名字建、校验却按后一个名字找，永远读不到。
真机现象（v0.16.0 第一次测试）：

    长中文名「K 口袋怪兽 水晶版」→ 备份失败 + 校验失败（屏幕两行）
    1.gbc（纯 8.3，没有长名项）  → 备份正常

短名 ROM 正常，是因为它 lfn 为空、根本不进就地改写那条路 —— 这也正是这个
bug 极难在主机测试里发现的原因：测试用的 ROM 全是 POKEMON.GBC。

## 检查什么

1. `save_dump()` / `save_verify()` 体内不能出现 sav_ident()。
2. `save_boot()` 体内必须恰好有一次 sav_ident()。
3. 那次调用必须出现在 save_dump 之前（按文本位置比）。
4. 传给 save_dump / save_verify 的第一个名字参数必须是同一个变量（sav_id）。
"""
import os
import re
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "save.c")


def body(text, sig):
    """取函数体：从签名到下一个顶格 '}'。"""
    i = text.find(sig)
    if i < 0:
        return None
    j = text.find("\n}", i)
    return text[i:j if j > 0 else len(text)]


def main():
    text = open(SRC, encoding="utf-8").read()
    fails = []

    dump = body(text, "static uint8_t save_dump(")
    verify = body(text, "static uint8_t save_verify(")
    boot = body(text, "void save_boot(")

    for name, b in (("save_dump", dump), ("save_verify", verify), ("save_boot", boot)):
        if b is None:
            print(f"  FAIL 找不到 {name}() —— 函数被改名了？检查脚本要跟着改")
            return 1

    # 1. dump / verify 里不能再算名字
    for name, b in (("save_dump", dump), ("save_verify", verify)):
        if "sav_ident(" in b:
            fails.append(f"{name}() 里又调了 sav_ident()：fat_open_sav 已经把 "
                         f"g_lfn_buf 就地改成 «….sav»，第二次算出来是另一个名字")

    # 2. save_boot 里恰好一次
    n = boot.count("sav_ident(")
    if n != 1:
        fails.append(f"save_boot() 里 sav_ident() 出现 {n} 次，必须恰好 1 次")

    # 3. 必须排在 save_dump 之前
    i_id = boot.find("sav_ident(")
    i_dump = boot.find("save_dump(")
    if i_id >= 0 and i_dump >= 0 and i_id > i_dump:
        fails.append("save_boot() 里 sav_ident() 排在 save_dump() 之后 —— 此时 "
                     "g_lfn_buf 已被就地改写，算出来的名字和写进去的不一致")

    # 4. 校验必须走 sav_open()，与读档同一个判据
    #    v0.16.12 起 verify 不再按短名读（早先那样会和按长名认的读档分家：
    #    LFN 没写进去时校验照样通过，下次开机却读不到）。
    if "sav_open(" not in verify:
        fails.append("save_verify() 没走 sav_open() —— 校验与读档必须是同一个判据")
    if not re.search(r"save_verify\(banks,", boot):
        fails.append("save_boot() 里没找到 save_verify(banks, …) 的调用形式，"
                     "检查脚本要跟着改")

    # 4b. 写入侧仍然从 sav_ident 的名字出发
    m_d = re.search(r"save_dump\(banks,\s*([A-Za-z_][A-Za-z0-9_]*)\s*,", boot)
    if not m_d:
        fails.append("save_boot() 里没找到 save_dump(banks, X, …) 的调用形式，"
                     "检查脚本要跟着改")
    elif m_d.group(1) not in ("sav_id", "use"):
        fails.append(f"save_dump 的名字参数叫 {m_d.group(1)}，期望 sav_id"
                     f"（由 sav_ident 算出的缓冲）")
    elif m_d.group(1) == "use" and "use = sav_id" not in boot:
        # v0.16.5 起 save_boot 用 use 中转：默认就是 sav_id，只有当卡上已有一条
        # 「长名相同、短名不同」的档时才换成 g_sav_adopt（写回同一个文件）。
        fails.append("save_dump 用的是 use，但 save_boot() 里没有 `use = sav_id` —— "
                     "名字不是从 sav_ident 来的")

    if fails:
        for f in fails:
            print(f"  FAIL {f}")
        return 1

    print("  OK   存档短名只在 save_boot 里算一次，且在 fat_open_sav 改写之前"
          "（长名 ROM 不会「写一个名、校验另一个名」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
