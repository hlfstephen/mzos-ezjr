#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
save_host_test.py — 在主机上验证存档写路径（fat.c + fatw.c）真的改对了 FAT。

为什么必须单独测：`sd_write()` 在真机上写过一次就覆水难收，而 FAT 写错的表现
是「当时看着成功，事后卡上的文件系统坏了」。这类 bug 上卡才发现，代价是一张
卡。所以在主机上把镜像当 SD 卡，让真代码改，再用**独立的 Python 解析器**
（不复用 C 的逻辑）把文件取出来比对。

覆盖：
  1. 新建 .SAV：目录项、FAT 链、数据三处都对得上；
  2. 同大小重写：复用原簇链，不泄漏簇；
  3. 改大小重写：旧链被标记为空，新链完整；
  4. 读回：fat_open_read + fat_read_sec 拿到的字节与写入一致；
  5. FAT16 与 FAT32 各跑一遍；
  6. 带中文长名新建：VFAT LFN 目录项的序号/0x40 标志/校验和/UCS-2
     字符都对，且短名项仍能被 fat_open_read 找到；
  7. 已有 .SAV 不搬迁目录项：普通重写不破坏已写好的 LFN。

用法：python3 tools/save_host_test.py
"""
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")

SEC = 512
RES_SEC = 1
N_FATS = 2          # 故意用 2 张 FAT：只写第一张的代码在这里会立刻露馅
ROOT_ENTRIES = 64
ROOT_SECS = (ROOT_ENTRIES * 32 + SEC - 1) // SEC
TOTAL_SEC = 64 * 1024 * 1024 // SEC        # 64 MB


def build_image(path, fat32, spc=None, total_sec=None):
    """空卷：只有一个卷标项，根目录其余全空。

    spc = 每簇扇区数。默认 FAT32 用 1、FAT16 用 4；老卡上还有 1 扇区/簇的
    几何，目录一簇只有 16 个槽，能逼出「目录要不要扩簇」这条路。"""
    if spc is None:
        spc = 1 if fat32 else 4
    # total_sec 只影响 BPB 与几何计算（镜像仍是整块缓冲）：
    # FAT16 规定簇数 < 65525，1 扇区/簇的卡必须是 32MB 以内的老卡才有这种几何。
    if total_sec is None:
        total_sec = TOTAL_SEC
    root_secs = 0 if fat32 else ROOT_SECS      # FAT32 没有固定根目录区
    ent = 4 if fat32 else 2
    n_clusters = (total_sec - RES_SEC - 32) // spc
    fat_sz = (n_clusters * ent + SEC - 1) // SEC
    cluster_count = (total_sec - RES_SEC - N_FATS * fat_sz - root_secs) // spc
    fat_sz = (cluster_count * ent + SEC - 1) // SEC
    root_lba = RES_SEC + N_FATS * fat_sz
    data_lba = root_lba + root_secs

    img = bytearray(TOTAL_SEC * SEC)

    dbr = bytearray(SEC)
    dbr[0:3] = b"\xEB\x3C\x90"
    dbr[3:11] = b"EZJRSAVE"
    struct.pack_into("<H", dbr, 0x0B, SEC)
    dbr[0x0D] = spc
    struct.pack_into("<H", dbr, 0x0E, RES_SEC)
    dbr[0x10] = N_FATS
    struct.pack_into("<H", dbr, 0x11, 0 if fat32 else ROOT_ENTRIES)
    if total_sec <= 0xFFFF:
        struct.pack_into("<H", dbr, 0x13, total_sec)
    else:
        struct.pack_into("<I", dbr, 0x20, total_sec)
    dbr[0x15] = 0xF8
    struct.pack_into("<H", dbr, 0x16, 0 if fat32 else fat_sz)
    if fat32:
        struct.pack_into("<I", dbr, 0x24, fat_sz)
        struct.pack_into("<I", dbr, 0x2C, 2)          # root cluster = 2
        dbr[0x36:0x3E] = b"FAT32   "
    else:
        dbr[0x36:0x3E] = b"FAT16   "
    dbr[0x1FE] = 0x55
    dbr[0x1FF] = 0xAA
    img[0:SEC] = dbr

    # 每张 FAT 的头两项：介质字节 + EOF
    for k in range(N_FATS):
        base = (RES_SEC + k * fat_sz) * SEC
        if fat32:
            struct.pack_into("<I", img, base + 0, 0x0FFFFFF8)
            struct.pack_into("<I", img, base + 4, 0x0FFFFFFF)
            struct.pack_into("<I", img, base + 8, 0x0FFFFFFF)   # 根目录簇 2
        else:
            struct.pack_into("<H", img, base + 0, 0xFFF8)
            struct.pack_into("<H", img, base + 2, 0xFFFF)

    if fat32:
        # 根目录簇 2 清空（全 0 = 目录结束）
        pass
    else:
        root = bytearray(ROOT_SECS * SEC)
        root[0:11] = b"EZJRVOL    "
        root[11] = 0x08
        img[root_lba * SEC:root_lba * SEC + 32] = root[0:32]

    open(path, "wb").write(img)
    return dict(spc=spc, fat_sz=fat_sz, root_lba=root_lba, data_lba=data_lba,
                fat32=fat32, cluster_count=cluster_count, root_secs=root_secs)


# ---------------------------------------------------------------------------
# 独立解析器：完全不看 C 代码，按 FAT 规范从镜像里把文件取出来
# ---------------------------------------------------------------------------
SAVER11 = b"SAVER      "          # 8.3 原始 11 字节：名字补 3 个空格 + 扩展 3 个空格

# 真机反馈 #3 的那个 ROM：36 个字符（含 ".gbc"），换成 .sav 之后仍是 36 个字符，
# 要 3 条 LFN 项才装得下。它同时踩过老代码的两个坑：
#   ① fat_open_sav 里 `if (nent > 3) nent = 0` —— 名字一长就整条长名都不写；
#   ② sav_lfn 按**第一个**点当扩展名分隔，于是 "2.0" 被当成扩展名，"0.gbc" 被吞掉，
#      写出来是 "…加强版2.sav"，跟 ROM 名对不上，卡上的加载器就找不到存档。
LONG_ROM = "K 口袋妖怪 水晶 金手指v3+全道具251+NPC加强版2.0.gbc"
LONG_SAV = LONG_ROM[:LONG_ROM.rfind(".")] + ".sav"


def fat_entry_of(img, geo, c):
    p = RES_SEC * SEC + c * (4 if geo["fat32"] else 2)
    if geo["fat32"]:
        return struct.unpack_from("<I", img, p)[0] & 0x0FFFFFFF
    return struct.unpack_from("<H", img, p)[0]


def dir_sectors(img, geo, first):
    """目录占用的扇区号。first=None 表示根目录，否则从那个簇沿链走。"""
    if first is None and not geo["fat32"]:
        return [geo["root_lba"] + i for i in range(geo["root_secs"])]

    eof = 0x0FFFFFF8 if geo["fat32"] else 0xFFF8
    c = 2 if first is None else first
    secs = []
    for _ in range(64):
        if c < 2 or c >= eof:
            break
        base = geo["data_lba"] + (c - 2) * geo["spc"]
        secs += [base + i for i in range(geo["spc"])]
        n = fat_entry_of(img, geo, c)
        if n >= eof:
            break
        c = n
    return secs


def find_dir(img, geo, name11):
    """在根目录里找一个子目录，返回首簇；没有返回 None。"""
    for s in dir_sectors(img, geo, None):
        for i in range(0, SEC, 32):
            e = img[s * SEC + i:s * SEC + i + 32]
            if e[0] == 0:
                return None
            # 跳过已删除 / 长名项 / 隐藏 / 系统 / 卷标。注意别把 0x10（目录）
            # 一起跳过 —— 要找的 SAVER 本身就是目录。
            if e[0] == 0xE5 or (e[11] & 0x3F) == 0x0F or (e[11] & 0x0E):
                continue
            if e[0:11] == name11 and (e[11] & 0x10):
                return (struct.unpack_from("<H", e, 26)[0] |
                        (struct.unpack_from("<H", e, 20)[0] << 16))
    return None


def move_sav_to_root(img, geo, name11=b"POKEMON SAV"):
    """把 SAVER 里的 .SAV 目录项搬到根目录 —— 模拟 v0.9.0 及以前写在 ROM
    同目录的存档，用来验证升级后还能读到它（fat_open_read 的回落路径）。
    返回 True 表示搬成功。"""
    assert isinstance(img, bytearray)
    sv = find_dir(img, geo, SAVER11)
    if not sv:
        return False
    src = None
    for s in dir_sectors(img, geo, sv):
        for i in range(0, SEC, 32):
            e = img[s * SEC + i:s * SEC + i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5 or (e[11] & 0x3F) == 0x0F:
                continue
            if e[0:11] == name11:
                src = (s, i, bytes(e))
                break
        if src:
            break
    if not src:
        return False
    for s in dir_sectors(img, geo, None):
        for i in range(0, SEC, 32):
            if img[s * SEC + i] in (0x00, 0xE5):
                img[s * SEC + i:s * SEC + i + 32] = src[2]
                img[src[0] * SEC + src[1]] = 0xE5
                return True
    return False


def has_entry(img, geo, first, name11):
    """目录 first（None = 根）里有没有这个 8.3 名。"""
    for s in dir_sectors(img, geo, first):
        for i in range(0, SEC, 32):
            e = img[s * SEC + i:s * SEC + i + 32]
            if e[0] == 0:
                return False
            if e[0] == 0xE5 or (e[11] & 0x3F) == 0x0F:
                continue
            if e[0:11] == name11:
                return True
    return False


def parse_file(img, geo, name11):
    ent, spc = (4 if geo["fat32"] else 2), geo["spc"]
    fat_off = RES_SEC * SEC
    data_base = geo["data_lba"]

    def fat_entry(c):
        p = fat_off + c * ent
        if geo["fat32"]:
            return struct.unpack_from("<I", img, p)[0] & 0x0FFFFFFF
        return struct.unpack_from("<H", img, p)[0]

    # v0.10.0 起存档在 SAVER 子目录里；没有这个目录时退回根目录
    # （那是读旧版本留下的 .SAV 的路径，也要能解析）。
    sv = find_dir(img, geo, SAVER11)
    secs = dir_sectors(img, geo, sv)

    found = None
    for s in secs:
        for i in range(0, SEC, 32):
            e = img[s * SEC + i:s * SEC + i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5 or (e[11] & 0x3F) == 0x0F or (e[11] & 0x18):
                continue
            if e[0:11] == name11:
                first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
                size = struct.unpack_from("<I", e, 28)[0]
                found = (first, size, e[11], e[12])
                break
        if found:
            break
    if not found:
        return None

    first, size, attr, nt = found
    chain, c, guard = [], first, 4096
    while c >= 2 and c < (0x0FFFFFF8 if geo["fat32"] else 0xFFF8) and guard:
        chain.append(c)
        c = fat_entry(c)
        guard -= 1

    body = bytearray()
    for c in chain:
        base = data_base + (c - 2) * spc
        for i in range(spc):
            body += img[(base + i) * SEC:(base + i + 1) * SEC]
        if len(body) >= size:
            break
    return dict(first=first, size=size, attr=attr, nt=nt, chain=chain,
                saver=sv, data=bytes(body[:size]))


def fat_free_count(img, geo):
    ent = 4 if geo["fat32"] else 2
    used = 0
    for c in range(2, geo["cluster_count"] + 2):
        p = RES_SEC * SEC + c * ent
        v = struct.unpack_from("<I" if geo["fat32"] else "<H", img, p)[0]
        if geo["fat32"]:
            v &= 0x0FFFFFFF
        if v:
            used += 1
    return used


# ---------------------------------------------------------------------------
# VFAT 长文件名目录项的独立解析器（不复用 C 代码，按 FAT 规范自己算）
# ---------------------------------------------------------------------------
# 一条 LFN 项装 13 个 UCS-2 字符，分散在这 13 个 2 字节槽里：
#   槽 0-4  -> 偏移 1,3,5,7,9
#   槽 5-10 -> 偏移 14,16,18,20,22,24
#   槽 11-12-> 偏移 28,30
LFN_SLOTS = (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30)


def lfn_checksum(name11):
    """VFAT 的短名校验和：sum 循环右移一位再累加短名字节。"""
    s = 0
    for b in name11:
        s = (((0x80 if (s & 1) else 0) + (s >> 1) + b) & 0xFF)
    return s


def root_dir_sectors(img, geo):
    """存档所在目录的扇区号：有 SAVER 就是 SAVER，没有就是根目录。"""
    return dir_sectors(img, geo, find_dir(img, geo, SAVER11))


def parse_lfn(img, geo, name11):
    """找 name11 短名项前紧邻的一组 LFN 项，返回解析结果；没有则 None。
    返回 dict：ords(序号字节)、chk(校验和列表)、name(UCS-2 码点表)、nent。"""
    entries = []
    for s in root_dir_sectors(img, geo):
        for i in range(0, SEC, 32):
            e = img[s * SEC + i:s * SEC + i + 32]
            if e[0] == 0x00:
                break                       # 目录结束
            entries.append(e)
        else:
            continue
        break

    short_idx = None
    for k, e in enumerate(entries):
        if e[0] == 0xE5 or (e[11] & 0x3F) == 0x0F or (e[11] & 0x18):
            continue
        if e[0:11] == name11:
            short_idx = k
            break
    if short_idx is None:
        return None

    lfn, j = [], short_idx - 1
    while j >= 0 and (entries[j][11] & 0x3F) == 0x0F:
        lfn.append(entries[j])
        j -= 1
    lfn.reverse()                           # 物理顺序：ord 最大(带 0x40) -> 1
    if not lfn:
        return None

    chars = {}
    for e in lfn:
        seq = e[0] & 0x1F
        base = (seq - 1) * 13
        for k, off in enumerate(LFN_SLOTS):
            chars[base + k] = e[off] | (e[off + 1] << 8)
    name = []
    for k in sorted(chars):
        cp = chars[k]
        if cp in (0x0000, 0xFFFF):         # 名字结束 / 尾部填充
            break
        name.append(cp)
    return dict(ords=[e[0] for e in lfn], chk=[e[13] for e in lfn],
                name=name, nent=len(lfn))


# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------
STUB = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "fat.h"

/* g_sav_adopt / g_sav_size 现在定义在 fatn.c（v0.16.6 起 save.c 编进 bank3，
 * 常驻段要引用它俩，只能由常驻模块持有）——这里只声明，不要再定义一遍。*/
extern char     g_sav_adopt[13];
extern uint32_t g_sav_size;

#define IMG_BYTES ((size_t)TOTAL_SEC_ * 512)
static uint8_t *g_img;
static unsigned long g_writes;

/* 模拟卡上 ROM 的中文长名 "K 口袋妖怪 水晶 金手指.GBC"。
 * fat_open_sav() 会把它换成 "…金手指.SAV" 写进 VFAT LFN 目录项。*/
static const uint16_t g_lfn[] = {
    'K', ' ', 0x53E3, 0x888B, 0x5996, 0x602A,
    ' ', 0x6C34, 0x6676, ' ', 0x91D1, 0x624B, 0x6307,
    '.', 'G', 'B', 'C', 0
};

/* 真机反馈 #3 的 ROM 全名（36 字符，".gbc" 在最后）。核实存档写出来的长名
 * 是不是这个名字只换扩展名 —— 中间那个 "2.0" 的点不能被当成分隔符。*/
static const uint16_t g_lfn_long[] = {
__LFNLONG__
    0
};

void sd_read(uint32_t lba, uint8_t *dst) {
    memcpy(dst, g_img + (size_t)lba * 512, 512);
}
uint8_t sd_write(uint32_t lba, const uint8_t *src) {
    memcpy(g_img + (size_t)lba * 512, src, 512);
    g_writes++;
    return 0;
}
void fpga_set(uint16_t r, uint8_t v) { (void)r; (void)v; }
void fpga_page(uint8_t p) { (void)p; }
uint8_t buttons(void) { return 0; }
void wait_release(void) {}
uint8_t fw_version(void) { return 5; }
void wait_frames(uint8_t n) { (void)n; }

static void rd_img(const char *p) {
    FILE *f = fopen(p, "rb");
    if (!f) { printf("IMG_OPEN\n"); exit(2); }
    if (fread(g_img, 1, IMG_BYTES, f) != IMG_BYTES) { printf("IMG_SHORT\n"); exit(2); }
    fclose(f);
}
static void wr_img(const char *p) {
    FILE *f = fopen(p, "wb");
    fwrite(g_img, 1, IMG_BYTES, f);
    fclose(f);
}

int main(int argc, char **argv) {
    FatFile ff;
    uint32_t size = 0;
    uint8_t e;
    unsigned i, n;
    uint32_t bytes;

    if (argc < 3) { printf("USAGE\n"); return 2; }
    g_img = malloc(IMG_BYTES);
    rd_img(argv[2]);

    e = fat_mount();
    if (e) { printf("MOUNT_ERR %u\n", (unsigned)e); return 0; }

    if (!strcmp(argv[1], "write") || !strcmp(argv[1], "writelfn") ||
        !strcmp(argv[1], "writelfnlong")) {
        const uint16_t *lfn = 0;
        bytes = (uint32_t)strtoul(argc > 3 ? argv[3] : "8192", 0, 10);
        if (!strcmp(argv[1], "writelfn")) lfn = g_lfn;
        if (!strcmp(argv[1], "writelfnlong")) lfn = g_lfn_long;
        e = fat_open_sav(&ff, "POKEMON", bytes, lfn);
        if (e) { printf("OPEN_ERR %u\n", (unsigned)e); return 0; }
        n = bytes >> 9;
        for (i = 0; i < n; i++) {
            uint8_t buf[512];
            unsigned j;
            for (j = 0; j < 512; j++)
                buf[j] = (uint8_t)((i * 7 + j) ^ (i >> 3));
            e = fat_write_sec(&ff, buf);
            if (e) { printf("WRITE_ERR %u\n", (unsigned)e); return 0; }
        }
        wr_img(argv[2]);
        printf("WROTE %lu\n", g_writes);
        return 0;
    }
    if (!strcmp(argv[1], "writeforeign")) {
        /* 模拟「从电脑拷回 SAVER/ 的档」：文件名（长名）被改过，系统按新名字
         * 现生成短名 SLZ2~1.SAV，跟本内核按 sav_ident() 算的哈希短名
         * （S1xxxxxx.SAV）对不上 —— 读档只能靠长名。档始终在 SAVER/ 里。*/
        bytes = (uint32_t)strtoul(argc > 3 ? argv[3] : "8192", 0, 10);
        e = fat_open_sav(&ff, "SLZ2~1", bytes, g_lfn);
        if (e) { printf("OPEN_ERR %u\n", (unsigned)e); return 0; }
        n = bytes >> 9;
        for (i = 0; i < n; i++) {
            uint8_t buf[512];
            unsigned j;
            for (j = 0; j < 512; j++)
                buf[j] = (uint8_t)((i * 7 + j) ^ (i >> 3));
            e = fat_write_sec(&ff, buf);
            if (e) { printf("WRITE_ERR %u\n", (unsigned)e); return 0; }
        }
        wr_img(argv[2]);
            printf("WROTE %lu\n", g_writes);
        return 0;
    }
    if (!strcmp(argv[1], "fill")) {
        /* 先往 SAVER 里塞一堆别的存档，把后面那个「外来档」挤到目录后半段。
         * 每个填充档 = 1 条 LFN + 1 条短名 = 2 个目录项，32 个就是 64 项：
         *   FAT16(SPC=4) 一簇 128 项 —— 目标档落在第 5 个扇区；
         *   FAT32(SPC=1) 一簇 16 项 —— 目标档落在第 5 簇。
         * 两者都越过「只扫前 4 个扇区」的老实现。*/
        unsigned cnt = (unsigned)strtoul(argc > 3 ? argv[3] : "32", 0, 10);
        char nm[13];
        uint16_t lf[8];
        for (i = 0; i < cnt; i++) {
            uint8_t buf[512];
            unsigned j;
            sprintf(nm, "FILL%02u", i);
            lf[0] = 0x586B; lf[1] = 0x5145; lf[2] = 0x6863;   /* 填 充 */
            lf[3] = (uint16_t)('0' + (i / 10));
            lf[4] = (uint16_t)('0' + (i % 10));
            lf[5] = 0;
            e = fat_open_sav(&ff, nm, 512, lf);
            if (e) { printf("FILL_ERR %u at %u\n", (unsigned)e, i); return 0; }
            for (j = 0; j < 512; j++) buf[j] = (uint8_t)(i + j);
            e = fat_write_sec(&ff, buf);
            if (e) { printf("FILL_WERR %u at %u\n", (unsigned)e, i); return 0; }
        }
        wr_img(argv[2]);
        printf("FILLED %u\n", cnt);
        return 0;
    }
    if (!strcmp(argv[1], "findlfn") || !strcmp(argv[1], "findother")) {
        unsigned j;
        for (j = 0; j < 18; j++) g_lfn_buf[j] = g_lfn[j];    /* 含结尾的 0 */
        if (!strcmp(argv[1], "findother"))
            g_lfn_buf[7] = 0x6C35;          /* 换掉一个字：不该认出来 */
        e = fat_find_sav_lfn(&ff);
        printf("FIND %u %s %lu\n", (unsigned)e, g_sav_adopt,
               (unsigned long)g_sav_size);
        return 0;
    }
    if (!strcmp(argv[1], "del")) {
        e = fat_delete(argc > 3 ? argv[3] : "POKEMON.SAV");
        printf("DEL %u\n", (unsigned)e);
        wr_img(argv[2]);
        return 0;
    }
    if (!strcmp(argv[1], "read")) {
        e = fat_open_read(&ff, "POKEMON", &size);
        if (e) { printf("READOPEN_ERR %u\n", (unsigned)e); return 0; }
        n = size >> 9;
        printf("SIZE %lu\n", (unsigned long)size);
        for (i = 0; i < n; i++) {
            uint8_t buf[512];
            unsigned j;
            fat_read_sec(&ff, buf);
            for (j = 0; j < 512; j++) printf("%02x", buf[j]);
            printf("\n");
        }
        return 0;
    }
    printf("BADOP\n");
    return 0;
}
'''


def pattern(nsec):
    out = bytearray()
    for i in range(nsec):
        out += bytes((((i * 7 + j) ^ (i >> 3)) & 0xFF) for j in range(512))
    return bytes(out)


def run(op, img_path, arg=""):
    tmp = tempfile.mkdtemp(prefix="ezjr-save-")
    stub = os.path.join(tmp, "stub.c")
    cps = [ord(c) for c in LONG_ROM]
    lines, cur = [], []
    for cp in cps:
        cur.append(cp)
        if len(cur) == 6:
            lines.append("    " + ", ".join(f"0x{c:04X}" for c in cur) + ",")
            cur = []
    if cur:
        lines.append("    " + ", ".join(f"0x{c:04X}" for c in cur) + ",")
    open(stub, "w").write(STUB.replace("TOTAL_SEC_", str(TOTAL_SEC))
                               .replace("__LFNLONG__", "\n".join(lines)))
    exe = os.path.join(tmp, "t")
    r = subprocess.run(["gcc", "-O1", "-I" + SRC, stub,
                        os.path.join(SRC, "fat.c"), os.path.join(SRC, "fatw.c"),
                        os.path.join(SRC, "fatx.c"), os.path.join(SRC, "fatn.c"),
                        "-o", exe], capture_output=True, text=True)
    if r.returncode != 0:
        print("  gcc 失败:\n" + r.stderr)
        sys.exit(1)
    cmd = [exe, op, img_path] + ([arg] if arg else [])
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def case(title, fat32):
    ok = True
    print(f"\n== {title} ==")
    tmp = tempfile.mkdtemp(prefix="ezjr-img-")
    img_path = os.path.join(tmp, "card.img")
    geo = build_image(img_path, fat32)
    print(f"  卷: {'FAT32' if fat32 else 'FAT16'}  SPC={geo['spc']}  "
          f"FAT={geo['fat_sz']}扇区  数据区首扇区={geo['data_lba']}")

    name11 = b"POKEMON SAV"

    # --- 1. 新建 8KB ---
    out = run("write", img_path, "8192")
    img = open(img_path, "rb").read()
    f = parse_file(img, geo, name11)
    if out.startswith("WROTE") and f and f["size"] == 8192 and f["data"] == pattern(16):
        need = (8192 + geo["spc"] * SEC - 1) // (geo["spc"] * SEC)
        print(f"  OK   新建 8KB：目录项+链({len(f['chain'])} 簇，应 {need}) + 数据一致")
        ok &= len(f["chain"]) == need
    else:
        print(f"  FAIL 新建 8KB：{out} size={f['size'] if f else None}")
        return False

    # --- 1b. 落在 SAVER 子目录里，扩展名小写 ---
    if f["saver"] is None:
        print("  FAIL 没有建出 SAVER 目录")
        ok = False
    else:
        print(f"  OK   SAVER 子目录已建立（首簇 {f['saver']}），.SAV 落在里面")
        base = geo["data_lba"] + (f["saver"] - 2) * geo["spc"]
        d0 = img[base * SEC:(base + 1) * SEC]
        if d0[0:11] == b".          " and d0[11] == 0x10 and \
           d0[32:43] == b"..         " and d0[43] == 0x10:
            print('  OK   SAVER 里的 "." 与 ".." 目录项齐全（电脑上打开不报损坏）')
        else:
            print('  FAIL SAVER 目录缺 "." / ".."')
            ok = False
    if f["nt"] & 0x10:
        print("  OK   目录项 +$0C 置了「扩展名小写」位 → 卡上显示 .sav")
    else:
        print(f"  FAIL 扩展名没标小写：+$0C = {f['nt']:#04x}")
        ok = False
    if has_entry(img, geo, None, name11):
        print("  FAIL 根目录里也有一份 .SAV（应在 SAVER 里）")
        ok = False
    else:
        print("  OK   根目录干净：.SAV 只在 SAVER 里")

    # --- 2. 同大小重写（应复用簇链）---
    used_before = fat_free_count(img, geo)
    first_before = f["first"]
    out = run("write", img_path, "8192")
    img = open(img_path, "rb").read()
    f2 = parse_file(img, geo, name11)
    used_after = fat_free_count(img, geo)
    if f2 and f2["first"] == first_before and used_after == used_before and f2["data"] == pattern(16):
        print(f"  OK   同大小重写：复用首簇 {first_before}，占用簇数不变（{used_after}）")
    else:
        print(f"  FAIL 同大小重写：首簇 {first_before}->{f2['first'] if f2 else None} "
              f"占用 {used_before}->{used_after}")
        ok = False

    # --- 3. 改大小（32KB）---
    out = run("write", img_path, "32768")
    img = open(img_path, "rb").read()
    f3 = parse_file(img, geo, name11)
    need = (32768 + geo["spc"] * SEC - 1) // (geo["spc"] * SEC)
    if f3 and f3["size"] == 32768 and f3["data"] == pattern(64) and len(f3["chain"]) == need:
        print(f"  OK   改大小 32KB：链 {len(f3['chain'])} 簇（应 {need}），数据一致")
    else:
        print(f"  FAIL 改大小 32KB：{out} size={f3['size'] if f3 else None} "
              f"chain={len(f3['chain']) if f3 else 0}")
        ok = False

    # --- 4. 两张 FAT 必须一致 ---
    ent = 4 if fat32 else 2
    f0 = img[RES_SEC * SEC:RES_SEC * SEC + geo["fat_sz"] * SEC]
    f1 = img[(RES_SEC + geo["fat_sz"]) * SEC:(RES_SEC + 2 * geo["fat_sz"]) * SEC]
    if f0 == f1:
        print("  OK   FAT 两张副本完全一致")
    else:
        diff = next(i for i in range(len(f0)) if f0[i] != f1[i])
        print(f"  FAIL FAT 副本不一致，首处 +{diff:#x}")
        ok = False

    # --- 5. 读回 ---
    out = run("read", img_path)
    lines = out.split("\n")
    if lines[0] == "SIZE 32768":
        got = b"".join(bytes.fromhex(l) for l in lines[1:] if l)
        if got == pattern(64):
            print("  OK   读回：32KB 逐字节一致")
        else:
            print("  FAIL 读回：内容不一致")
            ok = False
    else:
        print(f"  FAIL 读回：{lines[0]}")
        ok = False

    # --- 6. 老存档（v0.9.0 写在 ROM 同目录）还能读到 ---
    buf = bytearray(open(img_path, "rb").read())
    if move_sav_to_root(buf, geo):
        open(img_path, "wb").write(buf)
        out = run("read", img_path)
        first = out.split("\n")[0]
        if first == "SIZE 32768":
            print("  OK   升级兼容：SAVER 里没有时会去 ROM 同目录找老 .SAV")
        else:
            print(f"  FAIL 老位置的 .SAV 读不到：{first}")
            ok = False
    else:
        print("  FAIL 搬不动 .SAV 目录项（测试自身的问题）")
        ok = False

    # --- 7. 删除：目录项标 $E5、簇回收、VFAT 长名项一并清掉 ---
    buf = bytearray(open(img_path, "rb").read())
    used_before = fat_free_count(buf, geo)
    out = run("del", img_path)
    img = open(img_path, "rb").read()
    f = parse_file(img, geo, name11)
    used_after = fat_free_count(img, geo)
    if out == "DEL 0" and f is None:
        print("  OK   删除：SAVER 里找不到这个文件了")
    else:
        print(f"  FAIL 删除：{out} 仍能解析出 {f['size'] if f else None}")
        ok = False
    # 文件那条链（len(f3["chain"]) 簇）必须整条退回空闲
    if used_after == used_before - len(f3["chain"]):
        print(f"  OK   簇链回收：占用簇 {used_before} → {used_after}"
              f"（{len(f3['chain'])} 簇已退回空闲）")
    else:
        print(f"  FAIL 簇回收：{used_before} → {used_after}，"
              f"期望 {used_before - len(f3['chain'])}")
        ok = False
    if parse_lfn(img, geo, name11) is None:
        print("  OK   配对的 VFAT 长名项也清掉了（不会粘到下一个文件上）")
    else:
        print("  FAIL 还留着孤儿长名项")
        ok = False
    if run("del", img_path) == "DEL 4":
        print("  OK   重复删除返回「找不到」（FAT_ERR_NO_FS），不会误删别的")
    else:
        print("  FAIL 重复删除的返回值不对")
        ok = False
    return ok


# "K 口袋妖怪 水晶 金手指.GBC" 经 sav_lfn() 换成 .SAV 的全名（UCS-2 码点）
LFN_EXPECT = [ord('K'), ord(' '), 0x53E3, 0x888B, 0x5996, 0x602A,
              ord(' '), 0x6C34, 0x6676, ord(' '), 0x91D1, 0x624B, 0x6307,
              ord('.'), ord('s'), ord('a'), ord('v')]


def lfn_case(title, fat32):
    ok = True
    print(f"\n== {title} · 中文全名 .SAV（VFAT LFN 目录项） ==")
    tmp = tempfile.mkdtemp(prefix="ezjr-lfn-")
    img_path = os.path.join(tmp, "card.img")
    geo = build_image(img_path, fat32)
    name11 = b"POKEMON SAV"

    # --- 1. 带长名新建 ---
    out = run("writelfn", img_path, "8192")
    img = open(img_path, "rb").read()
    f = parse_file(img, geo, name11)
    if out.startswith("WROTE") and f and f["size"] == 8192 and f["data"] == pattern(16):
        print("  OK   新建 8KB：短名项 + FAT 链 + 数据都正确（LFN 未干扰定位）")
    else:
        print(f"  FAIL 新建 8KB：{out} size={f['size'] if f else None}")
        return False

    # --- 1b. 闭环：用**固件的查找路径**把刚写进去的长名认回来 ---
    #     这一条对应真机上的「备份写完立刻回读校验」（save_verify → sav_open
    #     → fat_find_sav_lfn）。v0.16.12 只按长名读档，这条路一旦和写入侧
    #     不一致，真机表现就是「备份失败/校验失败」+「存档读不出来」。
    out = run("findlfn", img_path)
    parts = out.split()
    if len(parts) >= 4 and parts[0] == "FIND" and parts[1] == "0" \
            and parts[2] == "POKEMON.SAV" and parts[3] == "8192":
        print("  OK   写完立刻按长名找回来（短名/大小都对）")
    else:
        print(f"  FAIL 按长名找不回来：{out}")
        ok = False

    # --- 2. LFN 项结构（独立解析器核对）---
    lf = parse_lfn(img, geo, name11)
    want_sum = lfn_checksum(name11)
    if not lf:
        print("  FAIL 根目录里没有找到 LFN 目录项")
        return False
    if lf["nent"] == 2 and lf["ords"] == [0x42, 0x01]:
        print(f"  OK   LFN {lf['nent']} 条，序号 {[hex(o) for o in lf['ords']]}"
              f"（首条带 0x40 结束标志，末条 ord=1 紧邻短名项）")
    else:
        print(f"  FAIL LFN 序号/条数：nent={lf['nent']} ords={[hex(o) for o in lf['ords']]}")
        ok = False
    if all(c == want_sum for c in lf["chk"]):
        print(f"  OK   全部 LFN 项校验和 = {want_sum:#04x}（与短名 POKEMON SAV 配对）")
    else:
        print(f"  FAIL 校验和 {[hex(c) for c in lf['chk']]}，期望 {want_sum:#04x}")
        ok = False
    if lf["name"] == LFN_EXPECT:
        txt = "".join(chr(c) if c < 128 else f"U+{c:04X}" for c in lf["name"])
        print(f"  OK   UCS-2 拼回中文全名（{len(lf['name'])} 字符）：{txt}")
    else:
        print(f"  FAIL 长名不符：{[hex(c) for c in lf['name']]}")
        ok = False

    # --- 3. 短名项仍可读（fat_open_read 只按 8.3 找，不受 LFN 影响）---
    out = run("read", img_path)
    lines = out.split("\n")
    if lines[0] == "SIZE 8192" and b"".join(bytes.fromhex(l) for l in lines[1:] if l) == pattern(16):
        print("  OK   按短名读回 8KB 逐字节一致")
    else:
        print(f"  FAIL 读回：{lines[0]}")
        ok = False

    # --- 4. 普通重写（不带长名）不搬迁/不破坏已有 LFN ---
    out = run("write", img_path, "8192")
    img = open(img_path, "rb").read()
    lf2 = parse_lfn(img, geo, name11)
    if lf2 and lf2["name"] == LFN_EXPECT and lf2["ords"] == lf["ords"]:
        print("  OK   同大小重写：已有 .SAV 不搬目录项，中文全名原样保留")
    else:
        print(f"  FAIL 重写后长名丢失：{lf2}")
        ok = False
    return ok


def long_case(title, fat32):
    """真机反馈 #3：36 字符的中文 ROM 名，.sav 必须逐字同名（只换扩展名）。"""
    ok = True
    print(f"\n== {title} · 长中文名（{len(LONG_SAV)} 字符）存档同名 ==")
    tmp = tempfile.mkdtemp(prefix="ezjr-long-")
    img_path = os.path.join(tmp, "card.img")
    geo = build_image(img_path, fat32)
    name11 = b"POKEMON SAV"

    out = run("writelfnlong", img_path, "8192")
    img = open(img_path, "rb").read()
    f = parse_file(img, geo, name11)
    if out.startswith("WROTE") and f and f["size"] == 8192 and f["data"] == pattern(16):
        print(f"  OK   新建 8KB：{out}，短名项 + FAT 链 + 数据都对（长名不影响定位）")
    else:
        print(f"  FAIL 新建 8KB：{out} size={f['size'] if f else None}")
        return False

    lf = parse_lfn(img, geo, name11)
    if not lf:
        print("  FAIL 根目录里没有找到 LFN 目录项（长名一条都没写）")
        return False

    want = [ord(c) for c in LONG_SAV]
    got = "".join(chr(c) if c < 128 else f"U+{c:04X}" for c in lf["name"])
    if lf["name"] == want:
        print(f"  OK   长名与 ROM 名逐字相同（只换扩展名）：{got}")
    else:
        print(f"  FAIL 长名不符：{got}")
        print(f"       期望：{LONG_SAV}")
        ok = False

    if LONG_SAV.endswith(".sav") and ".2.0.sav" in LONG_SAV.replace(" ", ""):
        if lf["name"] and lf["name"][-1] == ord("v") and len(lf["name"]) == len(LONG_SAV):
            print("  OK   中间那个 \"2.0\" 的点没被当扩展名分隔（没截成 …加强版2.sav）")
        else:
            print(f"  FAIL 名字被截断：{len(lf['name'])} 字符，期望 {len(LONG_SAV)}")
            ok = False

    nent = (len(LONG_SAV) + 12) // 13
    if lf["nent"] == nent and lf["ords"] == [0x40 | nent] + list(range(nent - 1, 0, -1)):
        print(f"  OK   LFN {lf['nent']} 条（{len(LONG_SAV)} 字符装 {nent} × 13 槽），"
              f"序号 {[hex(o) for o in lf['ords']]}，首条带 0x40")
    else:
        print(f"  FAIL LFN 条数/序号：nent={lf['nent']} ords={[hex(o) for o in lf['ords']]}，"
              f"期望 {nent} 条")
        ok = False

    want_sum = lfn_checksum(name11)
    if all(c == want_sum for c in lf["chk"]):
        print(f"  OK   {lf['nent']} 条 LFN 的校验和都是 {want_sum:#04x}（配对短名项）")
    else:
        print(f"  FAIL 校验和 {[hex(c) for c in lf['chk']]}，期望 {want_sum:#04x}")
        ok = False

    out = run("read", img_path)
    rl = out.split("\n")
    if rl[0] == "SIZE 8192" and b"".join(bytes.fromhex(l) for l in rl[1:] if l) == pattern(16):
        print("  OK   按 8.3 短名读回 8KB 逐字节一致（加载器照旧能读到）")
    else:
        print(f"  FAIL 读回：{rl[0]}")
        ok = False
    return ok



def find_case(title, fat32):
    """按长名认存档：从电脑拷回 SAVER/ 的 .sav（短名是系统现生成的）也该读得到。"""
    ok = True
    print(f"\n== {title} ==")
    tmp = tempfile.mkdtemp(prefix="ezjr-img-")
    img_path = os.path.join(tmp, "card.img")
    geo = build_image(img_path, fat32)

    # --- 1. 造一份「外来」存档：短名 SLZ2~1.SAV，长名 = ROM 长名换 .sav ---
    out = run("writeforeign", img_path, "8192")
    img = open(img_path, "rb").read()
    if not out.startswith("WROTE"):
        print(f"  FAIL 造外来存档：{out}")
        return False
    lfn = parse_lfn(img, geo, b"SLZ2~1  SAV")
    if lfn and "".join(chr(c) for c in lfn["name"]).endswith(".sav"):
        print(f"  OK   卡上有 SLZ2~1.SAV，长名 = 「{''.join(chr(c) for c in lfn['name'])}」")
    else:
        print(f"  FAIL 长名不对：{lfn}")
        return False

    # --- 2. sav_ident 的哈希短名找不到它（这就是真机上的现象）---
    #      S1 开头的名字不存在，只在解释器里确认一下，避免误判成「本来就能找到」
    if has_entry(img, geo, None, b"SLZ2~1  SAV"):
        print("  OK   短名就是 SLZ2~1.SAV（不是 sav_ident 的哈希名）")

    # --- 3. 按长名找：必须命中，并把短名记下来 ---
    out = run("findlfn", img_path)
    if out == "FIND 0 SLZ2~1.SAV 8192":
        print("  OK   按长名认出来了：adopt = SLZ2~1.SAV（备份写回同一文件），"
              "大小 8192（调用方按它验档）")
    else:
        print(f"  FAIL 按长名没认出来：{out}")
        ok = False

    # --- 4. 名字差一个字：不能认 ---
    out = run("findother", img_path)
    if out.startswith("FIND 4"):
        print("  OK   名字差一个字就不认（返回 FAT_ERR_NO_FS）")
    else:
        print(f"  FAIL 认错了：{out}")
        ok = False
    return ok


def upcase_sav_ext(img, geo):
    """把 SAVER 里所有 .sav 长名的扩展名改成大写 .SAV（Windows 上很常见）。
    返回是否改过。用来验证「按长名认档」不区分 ASCII 大小写。"""
    sv = find_dir(img, geo, SAVER11)
    if not sv:
        return False
    hit = False
    for s in dir_sectors(img, geo, sv):
        for i in range(0, SEC, 32):
            p = s * SEC + i
            if img[p] == 0:
                return hit
            if (img[p + 11] & 0x3F) != 0x0F:
                continue
            ch = [(p + o, struct.unpack_from("<H", img, p + o)[0])
                  for o in LFN_SLOTS]
            for k in range(len(ch) - 3):
                if [c for _, c in ch[k:k + 4]] == [ord('.'), ord('s'),
                                                   ord('a'), ord('v')]:
                    for d, c in zip(range(1, 4), "SAV"):
                        struct.pack_into("<H", img, ch[k + d][0], ord(c))
                    hit = True
    return hit


def crowd_case(title, fat32):
    """SAVER 里已有几十个档时，后面那个外来档还能不能认出来。

    真机上 SAVER 会慢慢攒到十几个、几十个文件；只扫目录前 4 个扇区的话，
    排在后面的档永远找不到，而屏幕上没有任何提示 —— 玩家只看到「存档没了」。"""
    ok = True
    print(f"\n== {title} ==")
    tmp = tempfile.mkdtemp(prefix="ezjr-img-")
    img_path = os.path.join(tmp, "card.img")
    geo = build_image(img_path, fat32)

    out = run("fill", img_path, "32")
    if not out.startswith("FILLED"):
        print(f"  FAIL 先塞 32 个填充档：{out}")
        return False
    print("  OK   SAVER 里先放了 32 个别的档（64 个目录项）")

    out = run("writeforeign", img_path, "8192")
    if not out.startswith("WROTE"):
        print(f"  FAIL 再造外来档：{out}")
        return False
    img = open(img_path, "rb").read()
    dsecs = dir_sectors(img, geo, find_dir(img, geo, SAVER11))
    # 目标档的短名项落在第几个扇区 —— 就是老实现扫不到的位置
    where = None
    for n, s in enumerate(dsecs):
        for i in range(0, SEC, 32):
            p = s * SEC + i
            if img[p:p + 11] == b"SLZ2~1  SAV":
                where = n
    if where is not None and where >= 4:
        print(f"  OK   外来档排在第 {where + 1} 个扇区（老实现只扫前 4 个）")
    else:
        print(f"  FAIL 没把外来档放到目录后半段：扇区 {where}")
        return False

    out = run("findlfn", img_path)
    if out == "FIND 0 SLZ2~1.SAV 8192":
        print("  OK   排在第 %d 个扇区也认出来了" % (where + 1))
    else:
        print(f"  FAIL 目录一长就找不到：{out}")
        ok = False
    return ok


def upper_case(title, fat32):
    """卡上长名的扩展名是大写 .SAV（Windows / 部分模拟器的写法）也得认。"""
    ok = True
    print(f"\n== {title} ==")
    tmp = tempfile.mkdtemp(prefix="ezjr-img-")
    img_path = os.path.join(tmp, "card.img")
    geo = build_image(img_path, fat32)

    out = run("writeforeign", img_path, "8192")
    if not out.startswith("WROTE"):
        print(f"  FAIL 造外来档：{out}")
        return False

    img = bytearray(open(img_path, "rb").read())
    if not upcase_sav_ext(img, geo):
        print("  FAIL 没找到 .sav 的长名项，改不了大小写")
        return False
    open(img_path, "wb").write(img)
    print("  OK   把卡上长名的扩展名改成大写「.SAV」")

    out = run("findlfn", img_path)
    if out == "FIND 0 SLZ2~1.SAV 8192":
        print("  OK   扩展名大写也认出来了")
    else:
        print(f"  FAIL 大写扩展名认不出：{out}")
        ok = False
    return ok


def move_entry_with_lfn(img, geo, name11):
    """把 SAVER 里某个文件连同它前面的长名项一起搬到根目录。
    用于负向用例：存档不在 SAVER/ 时，内核应该认不出（只找这一处）。"""
    assert isinstance(img, bytearray)
    sv = find_dir(img, geo, SAVER11)
    if not sv:
        return False
    src = []                       # 从长名项第一条到短名项
    for s in dir_sectors(img, geo, sv):
        for i in range(0, SEC, 32):
            p = s * SEC + i
            e = img[p:p + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                src = []
                continue
            if (e[11] & 0x3F) == 0x0F:
                src.append((p, bytes(e)))
                continue
            if e[0:11] == name11:
                src.append((p, bytes(e)))
                for s2 in dir_sectors(img, geo, None):
                    for i2 in range(0, SEC, 32):
                        if img[s2 * SEC + i2] in (0x00, 0xE5):
                            if i2 + 32 * len(src) > SEC:
                                return False    # 别跨扇区，测试用不着
                            for n, (p0, raw) in enumerate(src):
                                img[s2 * SEC + i2 + n * 32:
                                    s2 * SEC + i2 + n * 32 + 32] = raw
                                img[p0] = 0xE5
                            return True
                return False
            src = []
    return False


def only_saver_case(title, fat32):
    """存档只在 SAVER/ 认：不在 SAVER/ 的那份必须认不出（负向守门）。

    v0.16.11 曾按「模拟器默认把 .sav 放在 ROM 旁边」的假设加了 ROM 所在目录
    兜底，v0.16.14 已撤回 —— 真机上存档永远拷回 SAVER/，去别的目录认档只会
    认错文件。这一组把档搬到根目录，断言**读不到**，防止那条兜底被加回来。"""
    print(f"\n== {title} ==")
    tmp = tempfile.mkdtemp(prefix="ezjr-img-")
    img_path = os.path.join(tmp, "card.img")
    geo = build_image(img_path, fat32)

    out = run("writeforeign", img_path, "8192")
    if not out.startswith("WROTE"):
        print(f"  FAIL 造外来档：{out}")
        return False

    img = bytearray(open(img_path, "rb").read())
    if not move_entry_with_lfn(img, geo, b"SLZ2~1  SAV"):
        print("  FAIL 没能把存档连同长名项搬到根目录")
        return False
    open(img_path, "wb").write(img)
    print("  OK   存档连同长名项搬到根目录（不在 SAVER/ 里）")

    out = run("findlfn", img_path)
    if out.startswith("FIND 4"):
        print("  OK   不在 SAVER/ 就不认（返回 FAT_ERR_NO_FS）")
        return True
    print(f"  FAIL 又去 SAVER/ 以外的地方认档了：{out}")
    return False


def main():
    print("save_host_test — 存档写路径（真 fat.c + 真 fatw.c，独立 Python 解析器核对）")
    a = case("FAT16 · 4KB 簇", False)
    b = case("FAT32 · 512B 簇", True)
    c = lfn_case("FAT16 · 4KB 簇", False)
    d = lfn_case("FAT32 · 512B 簇", True)
    e = long_case("FAT16 · 4KB 簇", False)
    f_ = long_case("FAT32 · 512B 簇", True)
    g = find_case("按长名认存档 · FAT16", False)
    h = find_case("按长名认存档 · FAT32", True)
    i = crowd_case("目录里几十个档，外来档排在后面 · FAT16", False)
    j = crowd_case("目录里几十个档，外来档排在后面 · FAT32", True)
    k = upper_case("长名扩展名大写 .SAV · FAT16", False)
    l = only_saver_case("存档只在 SAVER/ 认（不在 SAVER/ 的档不认） · FAT16", False)
    allok = (a and b and c and d and e and f_ and g and h and i and j and k
             and l)
    print("\n" + ("全部通过" if allok else "有失败项"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
