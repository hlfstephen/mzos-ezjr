#!/usr/bin/env python3
"""farcall_test.py — 在模拟器里**真正执行** far call，验证桩的寄存器与栈约定。

为什么需要这个测试：v0.16.6/0.16.7 的 far call 从来没被真正跑过 —— 模拟器里
正式版没有 SD 卡，开机直接停在错误屏；`fat_info`、`fat_mount` 都在 bank1，主链接
里是直达调用，一个桩都不会命中。于是三个 bug 一路带到真机，表现是读卡时卡在
「读取中」：

  1. `pop de` 把**第一个参数**覆盖了（SDCC sm83 的第一个 16 位参数放在 DE 里，
     不在栈上）；
  2. 返回点指向「下一个桩」，`__fcret` 跳回去又撞进桩表；
  3. 栈上参数整体后移 2 字节。

做法：内核跑到错误屏后，手工构造一次 C→桩 的调用（DE 放参数指针、栈上放返回
地址、PC 指向桩），跑一帧，然后检查寄存器、栈、以及目标函数是否真的往参数指向
的缓冲区写了东西。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")

RET_STUB = 0xC100       # 返回地址：WRAM 里放一条 `jr $`（GB 可以执行 WRAM）
ARG_BUF = 0xC200        # FatInfo 结构写到这里
STACK = 0xDF80          # 测试专用栈；内核自己的 SP 在 $DFE2 附近，避开它
SENTINEL = 0xAA


def stub_addrs():
    """farstub.s 里标签的顺序 = 链接顺序，桩从 $0400 起每 7 字节一个。"""
    path = os.path.join(BUILD, "obj", "farstub.s")
    if not os.path.exists(path):
        return {}
    out, idx = {}, 0
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^(_[\w]+)::", line)
        if m:
            out[m.group(1)] = 0x0400 + idx * 7
            idx += 1
    return out


def map_syms():
    path = os.path.join(BUILD, "obj", "ezgb.map")
    syms = {}
    for v, k in re.findall(r"^\s+([0-9A-Fa-f]{8})\s+(_\w+)\s", open(path).read(), re.M):
        syms.setdefault(k, int(v, 16))
    return syms


def far_call(py, stub, de_val, ret_addr=RET_STUB):
    """模拟一次 C→桩 调用：DE = 第一个参数，栈上压返回地址，PC 指向桩。"""
    py.memory[STACK] = ret_addr & 0xFF
    py.memory[STACK + 1] = ret_addr >> 8
    py.register_file.SP = STACK
    py.register_file.D = de_val >> 8        # pyboy 没有 DE 聚合属性，只能分开设
    py.register_file.E = de_val & 0xFF
    py.register_file.PC = stub
    py.tick()


def main():
    try:
        from pyboy import PyBoy
    except ImportError:
        print("  跳过：未安装 pyboy")
        return 0
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

    rom = os.path.join(BUILD, "ezgb.dat")
    if not os.path.exists(rom):
        print("  跳过：build/ezgb.dat 不存在")
        return 0

    stubs = stub_addrs()
    syms = map_syms()
    stub = stubs.get("_fars_fat_info")
    far_sp = syms.get("_far_sp")
    far_cur = syms.get("_far_cur")
    fmount = syms.get("_fmount_ok")
    if stub is None or far_sp is None:
        print("  跳过：找不到 fat_info 的桩或 _far_sp")
        return 0

    py = PyBoy(rom, window="null", sound_emulated=False)
    for _ in range(400):
        py.tick()

    fails = checks = 0

    def ck(name, cond, extra=""):
        nonlocal fails, checks
        checks += 1
        if not cond:
            fails += 1
        print(("  OK   " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))

    print(f"\n[far] 桩 _fars_fat_info=${stub:04X}（数据表 -> bank1 的 $45CA）")
    print(f"      内核自身 SP=${py.register_file.SP:04X}，测试栈=${STACK:04X}")

    # 返回地址处放 `jr $`（0x18 0xFE），far call 回来后就在原地打转
    py.memory[RET_STUB] = 0x18
    py.memory[RET_STUB + 1] = 0xFE

    for i in range(12):
        py.memory[ARG_BUF + i] = SENTINEL

    far_call(py, stub, ARG_BUF)

    sp = py.register_file.SP
    depth = py.memory[far_sp]
    cur = py.memory[far_cur]
    de = (py.register_file.D << 8) | py.register_file.E
    got = list(py.memory[ARG_BUF:ARG_BUF + 8])

    ck("_far_sp 回到 0（__farcall/__fcret 配对）", depth == 0, f"_far_sp={depth}")
    ck("SP 恢复（比进入时多 2 = 弹掉了返回地址）", sp == STACK + 2,
       f"SP=${sp:04X}，期望 ${STACK + 2:04X}")
    ck("_far_cur 回到调用者的 bank 1", cur == 1, f"_far_cur={cur}")
    ck("DE（第一个参数）没被破坏", de == ARG_BUF, f"DE=${de:04X}")
    ck("目标函数真的写了参数指向的缓冲区（哨兵被覆盖）",
       got[:6] != [SENTINEL] * 6, f"$C200={got}")
    # fat_info 把 fmount_ok 写进 out->mounted；模拟器没有 SD 卡，它必为 0。
    # 这一条同时证明「参数确实传到了目标函数手里」。
    ck("写进去的就是 fmount_ok（参数真的传对了）",
       fmount is not None and py.memory[ARG_BUF] == py.memory[fmount],
       f"$C200={py.memory[ARG_BUF]} fmount_ok={py.memory[fmount] if fmount else '?'}")

    # 连续调用：栈若有一处不平衡，这里会累积暴露
    ok = True
    for n in range(5):
        for i in range(12):
            py.memory[ARG_BUF + i] = SENTINEL
        far_call(py, stub, ARG_BUF)
        if py.memory[far_sp] != 0 or py.register_file.SP != STACK + 2:
            ok = False
            print(f"       第 {n + 1} 次：_far_sp={py.memory[far_sp]} "
                  f"SP=${py.register_file.SP:04X}")
    ck("连调 5 次没有栈泄漏", ok)

    # ---- v0.16.10：返回值寄存器 ----
    #
    # SDCC sm83 的返回约定（实测）：A = 8 位，BC = 16 位，BC:DE = 32 位。
    # __fcret 以前直接用 a/bc/hl 算栈，把这三个寄存器全践踏了：
    #   · FAT 的错误码（uint8_t，在 A）变成 bank 号 → 屏幕「备份失败 / 未知错误」；
    #   · 32 位返回值的高字（在 BC）变成 _far_stk 的地址 → fat_cluster_lba 算出
    #     天文数字的 LBA → 字库读到垃圾扇区 → **中文文件名整片空白**。
    # 这条测试就是防止再犯：三种宽度各挑一个返回**确定值**的 far 函数。
    print(f"\n[far] 返回值寄存器（A / BC / BC:DE）")

    def call_and_read(name, expect_bc=None, expect_a=None, label=""):
        st = stubs.get(name)
        if st is None:
            ck(f"{label}：桩 {name} 存在", False)
            return
        far_call(py, st, 0xC300)            # 这些函数都不吃参数
        a = py.register_file.A
        bc = (py.register_file.B << 8) | py.register_file.C
        de = (py.register_file.D << 8) | py.register_file.E
        if expect_a is not None:
            ck(label + "（8 位返回值在 A）", a == expect_a, f"A={a}，期望 {expect_a}")
        if expect_bc is not None:
            ck(label + "（16/32 位返回值的高字在 BC）", bc == expect_bc,
               f"BC=${bc:04X}，期望 ${expect_bc:04X}")
        return a, bc, de

    # 无 SD 卡 → mount 失败 → 一律返回 FAT_ERR_NO_FS（=4）。修复前这里会是 bank 号 1。
    call_and_read("_fars_fat_find_sav_lfn", expect_a=4,
                  label="fat_find_sav_lfn 返回 FAT_ERR_NO_FS")

    # save_last_lfn() 返回 g_lfn_buf 的地址（全局，map 里有）；修复前 BC 被
    # `ld bc,#_far_stk` 改成 $Cxxx 附近的地址。
    glfn = syms.get("_g_lfn_buf")
    if glfn is not None:
        call_and_read("_save_last_lfn", expect_bc=glfn,
                      label="save_last_lfn 返回 g_lfn_buf 的地址")
    else:
        ck("map 里有 _g_lfn_buf", False)

    # save_last_dir() 返回 uint32_t boot_dir_saved（未启动过 = 0），占 BC:DE。
    call_and_read("_save_last_dir", expect_bc=0,
                  label="save_last_dir 返回 0（32 位，高字在 BC）")

    py.stop()
    print(f"  -- far call 运行时验证：{checks - fails}/{checks} 通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
