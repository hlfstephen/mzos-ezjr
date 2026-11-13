#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fpga_host_test.py — 在主机上验证 sd_write() / psram_map() 的寄存器序列。

为什么单独测：这两段是「写进去就覆水难收」的硬件路径。真机上写错一个字节
（比如 $7FB4 忘了 bit7、或者先把 LBA 写进去再填窗口数据），现象是"看着成功、
卡上的文件事后损坏"，而且没有任何屏幕提示。主机上把 $7Fxx 映射到一块 RAM，
就能把 fpga.c 的真实代码跑起来，逐步核对。

依据（上游 lukesau/ezgb）：
  stage1/src/fpga.c  sd_write()     写序列
  docs/psram-save-map.md            $4000 页闩锁 + $7FC0=3 的用法
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")

STUB = r'''
#include <stdio.h>
#include <string.h>
#include <stdint.h>

uint8_t g_mem[0x10000];
uint8_t g_win[0x400];

uint8_t *treg(unsigned a) { return g_mem + (a & 0xFFFF); }
void wait_frames(uint8_t n) { (void)n; }
void wait_release(void);

/* 记录 fpga_set 的调用序列。fpga.c 用 -DEZJR_NO_FPGA_SET 编，自己的实现
 * 不进二进制（ld 的 --wrap 只管未定义符号，同一编译单元里的定义照样绑定）。*/
static unsigned g_nset;
static uint16_t g_setreg[16];
static uint8_t  g_setval[16];

void fpga_set(uint16_t reg, uint8_t value) {
    if (g_nset < 16) { g_setreg[g_nset] = reg; g_setval[g_nset] = value; }
    g_nset++;
    g_mem[reg] = value;
}

#include "fpga.h"

int main(void) {
    uint8_t src[512], dst[512];
    unsigned i;

    /* ---- 1. sd_write ---- */
    g_win[0] = 0x00;                     /* 状态窗口不忙 */
    for (i = 0; i < 512; i++) src[i] = (uint8_t)(i * 5 + 1);
    memset(dst, 0, 512);
    g_nset = 0;
    {
        uint8_t r = sd_write(0x12345678UL, src);
        unsigned k;
        printf("RET %u\n", r);
        printf("LBA %02x %02x %02x %02x\n", g_mem[0x7FB0], g_mem[0x7FB1],
               g_mem[0x7FB2], g_mem[0x7FB3]);
        printf("CNT %02x\n", g_mem[0x7FB4]);
        printf("WIN %d\n", memcmp(g_win, src, 512) == 0);
        printf("SETN %u\n", g_nset);
        printf("SET");
        for (k = 0; k < g_nset && k < 16; k++)
            printf(" %03X=%02X", g_setreg[k], g_setval[k]);
        printf("\n");
    }

    /* ---- 2. psram_map / unmap ---- */
    g_nset = 0;
    memset(g_mem, 0, 0x10000);
    psram_map(0x11);
    printf("MAP %02x %02x\n", g_mem[0x4000], g_mem[0x7FC0]);
    psram_unmap();
    printf("UNMAP %02x %02x\n", g_mem[0x4000], g_mem[0x7FC0]);

    /* ---- 3. sd_read（回归）---- */
    for (i = 0; i < 512; i++) g_win[i] = (uint8_t)(i ^ 0x5A);
    g_mem[0x7FB0] = 0;
    sd_read(0x000000A5UL, dst);
    printf("RD %02x %02x %02x %02x OK %d\n", g_mem[0x7FB0], g_mem[0x7FB1],
           g_mem[0x7FB2], g_mem[0x7FB3], dst[7] == (uint8_t)(7 ^ 0x5A));
    return 0;
}
'''


def main():
    tmp = tempfile.mkdtemp(prefix="ezjr-fpga-")
    stub = os.path.join(tmp, "stub.c")
    open(stub, "w").write(STUB)
    exe = os.path.join(tmp, "t")
    cmd = [
        "gcc", "-O1", "-fno-inline", "-I" + SRC,
        "-include", os.path.join(HERE, "host_hw.h"),
        "-DEZJR_NO_FPGA_SET",
        stub, os.path.join(SRC, "fpga.c"), "-o", exe,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("gcc 失败:\n" + r.stderr)
        return 1
    out = subprocess.run([exe], capture_output=True, text=True).stdout
    lines = dict(l.split(" ", 1) for l in out.strip().split("\n") if " " in l)

    ok = True

    def check(cond, good, bad):
        nonlocal ok
        print(("  OK   " if cond else "  FAIL ") + (good if cond else bad))
        ok = ok and cond

    print("fpga_host_test — sd_write / psram_map 寄存器序列")

    check(lines.get("RET") == "0", "sd_write 返回 0（没有超时）",
          f"sd_write 返回 {lines.get('RET')}")
    check(lines.get("LBA") == "78 56 34 12",
          "LBA 小端写入 $7FB0-$7FB3",
          f"LBA 写错：{lines.get('LBA')}")
    check(lines.get("CNT") == "81",
          "$7FB4 = $81（1 个扇区 + 写标志）",
          f"$7FB4 = {lines.get('CNT')}，应为 81")
    check(lines.get("WIN") == "1",
          "512 字节数据已进 $A000 窗口",
          "窗口内容与源数据不一致")

    sets = lines.get("SET", "")
    seq = [s.split("=") for s in sets.split()]
    seq = [(a, b) for a, b in seq]
    check(seq[:1] == [("7FC0", "00")],
          "SD 操作前先把 $7FC0 清 0（否则 $A000 还在 PSRAM 上，512 字节写不进去）",
          f"没有先清 $7FC0：{seq[:2]}")
    check(seq[1:3] == [("7F30", "01"), ("7F30", "03")],
          "$7F30 序列 1 → 3（开数据窗口再切状态窗口）",
          f"$7F30 序列不对：{seq[1:3]}")
    check(seq and seq[-1] == ("7F30", "00"),
          "末尾 $7F30 = 0（关窗口）",
          f"末尾不是关窗口：{seq[-1:]}")
    check("7FB4" not in [a for a, _ in seq],
          "LBA/扇区数走的是直接寄存器写（不被 fpga_set 包）",
          "LBA 写入被包进了 fpga_set，解锁顺序会变")

    check(lines.get("MAP") == "11 03",
          "psram_map：$4000=页号，$7FC0=3",
          f"psram_map 结果 {lines.get('MAP')}，应为 11 03")
    check(lines.get("UNMAP") == "00 00",
          "psram_unmap：$4000=0，$7FC0=0",
          f"psram_unmap 结果 {lines.get('UNMAP')}")

    check(lines.get("RD", "").lower().startswith("a5 00 00 00 ok 1"),
          "sd_read 回归：LBA 写入 + 窗口拷贝正常",
          f"sd_read 异常：{lines.get('RD')}")

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
