#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fat_host_test.py — 在主机上验证 fat.c 的挂载与根目录扫描逻辑。

做法：
  1) 用 Python 构造几种真实世界的卷布局镜像，用 gcc 把 src/fat.c 编成主机程序，
     mock 掉 sd_read（从镜像读扇区），跑 fat_mount()/fat_scan_games()；
  2) 断言"该列出的列出、该报的错报对"。

覆盖的布局（每一条都对应真卡上会遇到的情况）：

  superfloppy   LBA0 就是 FAT 引导扇区（整卡一个卷，没有分区表）
  mbr           LBA0 是 MBR，引导代码为 0，第一个分区 0x0C 从 LBA 2048 开始
                —— **这正是真机 ERR=1 的那种卡**：MBR 的 0x0B 处不是 512
  mbr_gpt       MBR 里第一个分区项是 0xEE（GPT 保护分区）→ 应报 GPT
  mbr_no_fs     有分区表，但分区里的扇区不是 FAT → 应报 NO_FS
  fat12         簇数 < 4085 → 明确拒绝，而不是按 FAT16 硬解
  no_label      引导扇区 0x36 处没有 "FAT" 标签（部分工具不写）→ 靠 BPB 兜底

早期版本只测了 superfloppy，于是漏掉了「MBR 的 0x0B 处不等于 512 就被判成
NO_SIG」这个 bug —— 真机上所有带分区表的卡都会挂载失败。这条用例是补上的。
"""
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SEC = 512
CSIZE = 1
FAT_SZ = 1
RES_SEC = 1
N_FATS = 1
ROOT_ENTRIES = 64
ROOT_SECS = (ROOT_ENTRIES * 32 + SEC - 1) // SEC

# v0.9.0：列表顺序是「目录在前、文件在后」，SUBDIR 从末尾挪到第一项。
# 目录项在磁盘上排在后面（第 6 项），这里刻意保持磁盘顺序不变，
# 用来验证固件会自己把目录提到前面。
# 目录在前、文件在后；**两组各自按文件名升序**（v0.12.1 起）。
# 卡上的拷贝顺序是 SUBDIR / POKEMON / TETRIS / CRYSTAL，排序后文件组变成
# CRYSTAL / POKEMON / TETRIS —— 这条断言就是在钉"确实按名字排了"。
GAMES = ["SUBDIR", "CRYSTAL.GBC", "POKEMON.GBC", "TETRIS.GB"]


# ---------------------------------------------------------------------------
# 构造镜像
# ---------------------------------------------------------------------------
LFN_SLOTS = (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30)


def lfn_sum(name11):
    """VFAT 长名项的校验和：由 11 字节短名算出，和它要配对的那条短名项一致。"""
    c = 0
    for b in name11:
        c = (((c & 1) << 7) + (c >> 1) + b) & 0xFF
    return c


def make_lfn_entries(text, name11):
    """生成 text 对应的 VFAT 长名项（物理顺序：序号大的在前，首条带 0x40）。

    用来验证「排序 + 长名」：列表按名字重排之后，fat_fill_lfn() 必须按**短名**
    把长名配回去 —— 要是还按磁盘下标套，排序一改就整个错位（屏幕显示 A 的名字、
    按 A 启动的却是 B）。"""
    cps = [ord(c) for c in text] + [0]
    n = (len(cps) + 12) // 13
    cps += [0xFFFF] * (n * 13 - len(cps))
    chk = lfn_sum(name11)
    ents = []
    for seq in range(n, 0, -1):
        chunk = cps[(seq - 1) * 13: seq * 13]
        e = bytearray(32)
        e[0] = seq | (0x40 if seq == n else 0)
        e[11] = 0x0F
        e[13] = chk
        for k, off in enumerate(LFN_SLOTS):
            e[off] = chunk[k] & 0xFF
            e[off + 1] = chunk[k] >> 8
        ents.append(bytes(e))
    return ents


def make_dirent(name8, ext3, attr, cluster, size):
    """标准 FAT 8.3：8 字节主名 + 3 字节扩展，均空格补齐，共 11 字节（无点）。"""
    e = bytearray(32)
    e[0:8] = name8.encode("ascii").ljust(8)[:8]
    e[8:11] = ext3.encode("ascii").ljust(3)[:3]
    struct.pack_into("<B", e, 11, attr)
    struct.pack_into("<H", e, 20, (cluster >> 16) & 0xFFFF)   # FAT32 簇高位
    struct.pack_into("<H", e, 26, cluster & 0xFFFF)
    struct.pack_into("<I", e, 28, size)
    return e


def write_fat16_volume(img, base, total_sec, label=True):
    """在 base 扇区处写入一个 FAT16 卷（引导扇区 + FAT + 根目录条目）。"""
    root_lba = base + RES_SEC + N_FATS * FAT_SZ

    dbr = bytearray(SEC)
    dbr[0:3] = b"\xEB\x3C\x90"                  # jmp + nop
    dbr[3:11] = b"EZJRTEST"                     # OEM
    struct.pack_into("<H", dbr, 0x0B, SEC)      # bytes/sector
    dbr[0x0D] = CSIZE                           # sectors/cluster
    struct.pack_into("<H", dbr, 0x0E, RES_SEC)  # reserved
    dbr[0x10] = N_FATS
    struct.pack_into("<H", dbr, 0x11, ROOT_ENTRIES)
    struct.pack_into("<H", dbr, 0x13, total_sec)
    dbr[0x15] = 0xF8                            # media
    struct.pack_into("<H", dbr, 0x16, FAT_SZ)
    if label:
        dbr[0x36:0x3E] = b"FAT16   "            # fs type 标签
    dbr[0x1FE] = 0x55
    dbr[0x1FF] = 0xAA
    img[base * SEC:(base + 1) * SEC] = dbr

    fat = bytearray(SEC)
    fat[0] = 0xF8
    fat[1] = 0xFF
    fat[2] = 0xFF
    fat[3] = 0xFF                               # cluster 2 = EOF
    fat[7 * 2] = 0xFF                           # cluster 7 = EOF（SUBDIR 目录）
    fat[7 * 2 + 1] = 0xFF
    img[(base + RES_SEC) * SEC:(base + RES_SEC + 1) * SEC] = fat

    # v0.12.2：长名不再只挂在 .GBC 上。这三种都要能被 fat_fill_lfn 配回去：
    #   POKEMON.GBC  → 三字符扩展名（一直是对的）
    #   TETRIS.GB    → **两字符扩展名**（v0.12.1 的 same_name 用 0 兜底，配不上）
    #   SUBDIR       → **无扩展名的目录**（同上，所有文件夹都中招）
    entries = (make_lfn_entries("口袋妖怪水晶.gbc", b"POKEMON GBC")
               + [make_dirent("POKEMON", "GBC", 0x20, 2, 2 * 1024 * 1024)]
               + make_lfn_entries("俄罗斯方块", b"TETRIS  GB ")
               + [make_dirent("TETRIS", "GB", 0x20, 3, 32 * 1024),
                  make_dirent("CRYSTAL", "GBC", 0x20, 4, 2 * 1024 * 1024),  # ROM
                  make_dirent("README", "TXT", 0x20, 5, 100),               # 非 ROM
                  make_dirent("DELETED", "GB", 0x20, 6, 32 * 1024)]         # 稍后标 0xE5
               + make_lfn_entries("游戏合集", b"SUBDIR     ")
               + [make_dirent("SUBDIR", "", 0x10, 7, 0),                    # 目录
                  make_dirent("HIDDEN", "GB", 0x22, 8, 32 * 1024),          # 隐藏
                  make_dirent("LONGFI~1", "GB", 0x0F, 9, 32 * 1024)])       # LFN
    root = bytearray(ROOT_SECS * SEC)
    for i, e in enumerate(entries):
        root[i * 32:(i + 1) * 32] = e
    # 标记 DELETED 已删除：插入长名项之后下标会变，按名字找
    for i in range(0, len(root), 32):
        if root[i:i + 8] == b"DELETED ":
            root[i] = 0xE5
            break
    img[root_lba * SEC:(root_lba + ROOT_SECS) * SEC] = root

    # 子目录 SUBDIR 的内容（cluster 7）：. / .. / 一个游戏。
    # 用来验证「进目录后 . 和 .. 被过滤掉、里面的 ROM 照样列出来」。
    sub = bytearray(SEC)
    sub[0:32] = make_dirent(".", "", 0x10, 7, 0)
    sub[32:64] = make_dirent("..", "", 0x10, 0, 0)
    sub[64:96] = make_dirent("SUBGAME", "GBC", 0x20, 3, 2 * 1024 * 1024)
    data_lba = root_lba + ROOT_SECS + (7 - 2) * CSIZE
    img[data_lba * SEC:(data_lba + 1) * SEC] = sub
    return root_lba


def write_mbr(img, part_type, start, zero_boot_code=True):
    """LBA0 写成 MBR。引导代码默认为 0（真卡常见），0x0B 处自然不是 512。"""
    m = bytearray(SEC)
    if not zero_boot_code:
        m[0:3] = b"\xFA\x33\xC0"
    struct.pack_into("<I", m, 440, 0x1234ABCD)   # disk signature
    e = 0x1BE
    m[e + 0] = 0x00                              # 非活动分区
    m[e + 4] = part_type
    struct.pack_into("<I", m, e + 8, start)
    struct.pack_into("<I", m, e + 12, 0x10000)
    m[0x1FE] = 0x55
    m[0x1FF] = 0xAA
    img[0:SEC] = m


def build_image(path, layout):
    """按布局造镜像，返回 (期望结果)。期望结果是游戏名列表或错误码。"""
    if layout == "superfloppy":
        img = bytearray(4096 * SEC)
        root_lba = write_fat16_volume(img, 0, 4096)
        expect = GAMES

    elif layout == "no_label":
        img = bytearray(4096 * SEC)
        root_lba = write_fat16_volume(img, 0, 4096, label=False)
        expect = GAMES

    elif layout == "mbr":
        # 真机实测那种卡：MBR 引导代码全 0，分区 0x0C 从 2048 开始
        img = bytearray(8192 * SEC)
        write_mbr(img, 0x0C, 2048)
        root_lba = write_fat16_volume(img, 2048, 4096)
        expect = GAMES

    elif layout == "mbr_gpt":
        img = bytearray(4096 * SEC)
        write_mbr(img, 0xEE, 1)
        root_lba = -1
        expect = 3                                # FAT_ERR_GPT

    elif layout == "mbr_no_fs":
        img = bytearray(8192 * SEC)
        write_mbr(img, 0x0B, 2048)                # 分区里全是 0，没有引导扇区
        root_lba = -1
        expect = 4                                # FAT_ERR_NO_FS

    elif layout == "fat12":
        img = bytearray(3000 * SEC)
        root_lba = write_fat16_volume(img, 0, 2048)   # 簇数 2042 < 4085
        expect = 2                                # FAT_ERR_FAT12

    else:
        raise ValueError(layout)

    open(path, "wb").write(img)
    return {"expect": expect, "root_lba": root_lba, "size": len(img)}


# ---------------------------------------------------------------------------
# 生成 C 测试桩 + 编译
# ---------------------------------------------------------------------------
STUB = r'''
/* 主机侧 fat.c 测试桩：mock 掉 sd_read 等硬件依赖，从镜像文件读扇区 */
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "fat.h"

#define IMG_BYTES  IMG_SIZE
#define IMG_PATH   "IMGFILE"
static uint8_t g_img[IMG_BYTES];

void sd_read(uint32_t lba, uint8_t *dst) {
    if ((size_t)lba * 512 + 512 > IMG_BYTES) { memset(dst, 0, 512); return; }
    memcpy(dst, g_img + (size_t)lba * 512, 512);
}
/* fpga.h / fpga.c 里的函数 fat.c 不引用，这里给空实现以防链接器需要 */
uint8_t sd_write(uint32_t lba, const uint8_t *src){(void)lba;(void)src;return 0;}
void fpga_set(uint16_t r, uint8_t v){(void)r;(void)v;}
void fpga_page(uint8_t p){(void)p;}
uint8_t buttons(void){return 0;}
void wait_release(void){}
uint8_t fw_version(void){return 5;}
void wait_frames(uint8_t n){(void)n;}

int main(void) {
    GameEntry list[32];
    uint8_t e, n, i;
    FILE *f = fopen(IMG_PATH, "rb");
    if (!f || fread(g_img, 1, IMG_BYTES, f) != IMG_BYTES) {
        printf("IMG_READ_ERR\n"); return 2;
    }
    fclose(f);
    e = fat_mount();
    if (e) { printf("MOUNT_ERR %u\n", (unsigned)e); return 0; }
    n = fat_scan_games(list, 32);
    printf("COUNT %u\n", (unsigned)n);
    {
        uint16_t names[8][FAT_LFN_CHARS];
        uint8_t j;

        fat_fill_lfn(list, 0, 8, &names[0][0]);
        for (i = 0; i < n; i++) {
            printf("GAME %s\n", list[i].name);
            if (i < 8)
                printf("LFN %s %04X\n", list[i].name, (unsigned)names[i][0]);
            (void)j;
        }
    }

    /* 进第一个目录（size == 0），列出内容，再退回来 —— 验证子目录浏览 */
    for (i = 0; i < n; i++)
        if (list[i].size == 0) break;
    if (i < n) {
        if (!fat_dir_enter(list[i].cluster)) { printf("ENTER_FAIL\n"); return 0; }
        n = fat_scan_games(list, 32);
        printf("SUBCOUNT %u\n", (unsigned)n);
        for (i = 0; i < n; i++)
            printf("SUB %s %u\n", list[i].name, (unsigned)list[i].size);
        if (!fat_dir_up()) printf("UP_FAIL\n");
        n = fat_scan_games(list, 32);
        printf("BACKCOUNT %u\n", (unsigned)n);
        for (i = 0; i < n; i++)
            printf("BACK %s\n", list[i].name);
    }
    return 0;
}
'''


def build_and_run(img_path):
    tmp = tempfile.mkdtemp(prefix="ezjr-fat-test-")
    stub = os.path.join(tmp, "stub.c")
    img = open(img_path, "rb").read()
    src = (STUB.replace("IMG_SIZE", str(len(img)))
               .replace("IMGFILE", img_path))
    open(stub, "w").write(src)

    exe = os.path.join(tmp, "fattest")
    cmd = ["gcc", "-O1", "-I" + os.path.join(ROOT, "src"),
           stub, os.path.join(ROOT, "src", "fat.c"),
                        os.path.join(ROOT, "src", "fatw.c"), os.path.join(ROOT, "src", "fatx.c"), "-o", exe]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("gcc 编译失败:\n" + r.stderr)
        return None
    return subprocess.run([exe], capture_output=True, text=True).stdout


def main():
    if not any(os.path.exists(p) for p in ["/usr/bin/gcc", "/usr/bin/cc"]):
        print("  跳过：主机无 gcc（不影响 ROM 构建，仅跳过算法验证）")
        return 0

    layouts = ["superfloppy", "mbr", "no_label", "mbr_gpt", "mbr_no_fs", "fat12"]
    fails = 0
    tmp = tempfile.mkdtemp(prefix="ezjr-fat-img-")

    for layout in layouts:
        img = os.path.join(tmp, layout + ".img")
        info = build_image(img, layout)
        out = build_and_run(img)
        if out is None:
            return 1
        lines = out.strip().splitlines()
        got_games = [l.split(" ", 1)[1] for l in lines if l.startswith("GAME ")]
        got_err = next((int(l.split()[1]) for l in lines if l.startswith("MOUNT_ERR")), 0)
        expect = info["expect"]

        if isinstance(expect, int):
            ok = got_err == expect
            got = f"ERR={got_err}" if got_err else f"{len(got_games)} 个游戏"
            want = f"ERR={expect}"
        else:
            ok = got_games == expect
            got = f"ERR={got_err}" if got_err else ", ".join(got_games)
            want = ", ".join(expect)

        print(("  OK   " if ok else "  FAIL ") + f"{layout:<11} 期望 {want:<26} 实得 {got}")
        if not ok:
            fails += 1

        if isinstance(expect, list):
            # 排序之后长名还得跟对人：列表按名字重排过，这里就是钉「别按磁盘下标套」。
            # 三条覆盖三种短名形状（v0.12.2 起都必须能配上）：
            #   POKEMON.GBC → 口 U+53E3（三字符扩展名）
            #   TETRIS.GB   → 俄 U+4FC4（**两字符**扩展名，v0.12.1 在这里是坏的）
            #   SUBDIR      → 游 U+6E38（**无**扩展名的文件夹，v0.12.1 全军覆没）
            lfn = {}
            for l in lines:
                if l.startswith("LFN "):
                    nm, v = l[4:].rsplit(" ", 1)
                    lfn[nm] = v
            want_lfn = {"POKEMON.GBC": "53E3", "TETRIS.GB": "4FC4", "SUBDIR": "6E38"}
            if (all(lfn.get(k) == v for k, v in want_lfn.items())
                    and all(v == "0000" for k, v in lfn.items() if k not in want_lfn)):
                print(f"  OK   {layout:<11} 排序后长名仍跟对人（.GBC / .GB / 文件夹三种短名都配上）")
            else:
                print(f"  FAIL {layout:<11} 长名错位或漏配：{lfn}")
                fails += 1

            sub = [l.split(" ", 1)[1] for l in lines if l.startswith("SUB ")]
            back = [l.split(" ", 1)[1] for l in lines if l.startswith("BACK ")]
            want_sub = [".. 4294967295", "SUBGAME.GBC 2097152"]
            if sub == want_sub and back == expect:
                print(f"  OK   {layout:<11} 子目录 = 「..」+ 目录内 ROM，返回上级一致")
            else:
                print(f"  FAIL {layout:<11} 子目录 期望 {want_sub} 实得 {sub} / 返回 {back}")
                fails += 1

    if fails:
        print(f"  -- {fails} 项不符")
    else:
        print(f"  -- {len(layouts)} 种卷布局 + 子目录浏览全部符合预期")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
