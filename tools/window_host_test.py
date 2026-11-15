#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
window_host_test.py — 存档全流程 + **$A000 窗口择路**的主机侧验证。

为什么需要它：v0.3.0 在真机上把 .SAV 写成了「根目录扇区」，主机测试却全绿 ——
因为那些测试把 sd_read/sd_write 换成了桩，`$A000` 窗口根本不存在，谁来写都对。
真机上的规则（RTL: stage0/rtl/sd_mem_path.v）是：

    win_sel = $7FC0 的 bit0 = 0 且 bit2 = 0      ← SD 扇区窗口的前提
    PSRAM 页模式 = $7FC0 = 3                     ← bit0 = 1，两者互斥

也就是说 `psram_map()` 之后没有 `psram_unmap()` 就去做 SD 操作，那 512 字节
会写进 PSRAM，卡上收到的则是控制器缓冲区的旧内容。本文件把这个择路逻辑建成
一个可在主机上跑的模型，跑**真的** fpga.c / fat.c / fatw.c / save.c：

    WINDOW 指向哪块内存，取决于此刻 $7FC0 与 $7F30 的值

于是"漏了 unmap"这类错误在主机上就会失败，而不是等真机把卡写坏。

覆盖：
  1. 首次启动（卡上没有 .SAV、元数据页是空的）→ PSRAM 里芯片存档**不被清掉**；
  2. 存档落盘：PSRAM 页 0-3 → .SAV，独立解析器核对 32KB 逐字节一致；
  3. 再启动：.SAV → PSRAM 页 0-3，逐字节回到落盘前的内容；
  4. 换游戏（元数据页记的是别的 ROM）→ PSRAM 被清成 $FF，不会串档；
  5. 官方内核留下的元数据页（$010 有 /SAVER 路径）不被误判成"别的游戏"；
  6. **电池没电**（哨兵 $11:$A201 不是 $88，但 $AA 章还在）→ 屏上 BATTERY DRY、
     **一个扇区都不写**、卡上原有的 .SAV 逐字节不动、哨兵重新盖成 $88，
     恢复供电后的下一次开机照常备份。

用法：python3 tools/window_host_test.py
"""
import hashlib
import atexit
import os
import subprocess
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, HERE)

from save_host_test import build_image, parse_file, SEC, TOTAL_SEC, RES_SEC  # noqa: E402

PSRAM_PAGES = 0x20          # 256 KB / 8 KB
PSRAM_META = 0x11
ROM_CLUSTER = 2             # 镜像里假 ROM 的起始簇
SUBDIR_CLUSTER = 5          # 镜像里假子目录的簇（验证存档不会写到这里）
def sav_ident_py(name83, lfn=None):
    """复刻 src/savkey.c 的 sav_ident()，返回目录项里的 11 字节原始名。

    存档短名现在是「完整长名的 28 位哈希」（S1A2B3C4.SAV），中文 ROM 之间
    不再撞名。测试要按这个名字去卡上找文件，否则永远找不到。"""
    h = 0x811C9DC5
    if lfn:
        for c in lfn:
            h = ((h << 5) + h + (c & 0xFF)) & 0xFFFFFFFF
            h ^= (c >> 8) & 0xFF
    else:
        for ch in name83.split(".")[0][:8]:
            h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    out = "S"
    for _ in range(7):
        v = h & 0xF
        out += chr(ord("0") + v) if v < 10 else chr(ord("A") + v - 10)
        h >>= 4
    return (out + "SAV").encode()


def sav_ident_str(name83, lfn=None):
    """同 sav_ident_py，但返回 "S1A2B3C4.SAV" 这种点分形式（给 C 侧用）。"""
    raw = sav_ident_py(name83, lfn)
    return raw[:8].decode() + ".SAV"


NAME11 = sav_ident_py("POKEMON.GBC")


def pat(k, tag):
    """第 k 字节的期望值：两个 tag 用不同种子，混了就立刻看出来。"""
    return ((k * 31 + (k >> 9) * 7 + tag) ^ (k >> 5)) & 0xFF


def pattern(tag, n=32768):
    return bytes(pat(k, tag) for k in range(n))


def digest(b):
    return hashlib.md5(b).hexdigest()[:12]


HOST_HW = r'''
/* 主机侧"假硬件"：把 $7Fxx/$FFxx 和一页窗口映射到进程内存。
 * WINDOW 不是固定地址，而是"此刻该是哪块内存"——这就是真机的窗口择路。*/
#ifndef EZJR_HOST_HW_H
#define EZJR_HOST_HW_H
#include <stdint.h>

extern uint8_t g_mem[0x10000];
extern uint8_t g_psram[32 * 0x2000];
extern uint8_t g_sdbuf[512];
extern uint8_t g_alt[512];
extern uint8_t g_dummy[512];

uint8_t *treg(unsigned a);
uint8_t *win_ptr(void);

#define REG(a)   (*(volatile uint8_t *)treg(a))
#define WINDOW   win_ptr()

#endif
'''

PROG = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

uint8_t g_mem[0x10000];
uint8_t g_psram[32 * 0x2000];
uint8_t g_sdbuf[512];
uint8_t g_alt[512];
uint8_t g_dummy[512];
uint8_t game_work[512];

static uint8_t *g_img;
static uint8_t  g_cmd;              /* 有一条 SD 命令待落地 */
static unsigned g_wr_sectors;       /* 落盘的扇区数 */
static uint32_t g_wr_lba[128];
static unsigned g_wr_n;

/* ---- "SD 控制器"：命令落地时才真正动数据 ----
 * 真机上 $7FB4 一写，PicoBlaze 就干活；这里等到下一次窗口访问再结算，
 * 效果等价（数据在这之前已经由调用方放进缓冲区了）。*/
static void sd_service(void)
{
    uint32_t lba;
    uint8_t  b4;

    if (!g_cmd) return;
    g_cmd = 0;
    b4 = g_mem[0x7FB4];
    lba = (uint32_t)g_mem[0x7FB0] | ((uint32_t)g_mem[0x7FB1] << 8) |
          ((uint32_t)g_mem[0x7FB2] << 16) | ((uint32_t)g_mem[0x7FB3] << 24);
    if (b4 & 0x80) {                        /* 写：缓冲区 -> 卡 */
        const uint8_t *src = (g_mem[0x7F30] & 2) ? g_alt : g_sdbuf;
        memcpy(g_img + (size_t)lba * 512, src, 512);
        if (g_wr_n < 128) g_wr_lba[g_wr_n++] = lba;
        g_wr_sectors++;
    } else {                                /* 读：卡 -> 缓冲区 */
        memcpy(g_sdbuf, g_img + (size_t)lba * 512, 512);
        memcpy(g_alt, g_sdbuf, 512);
        memset(g_alt, 0, 16);               /* $7F30 bit1：状态窗口读到 $00（不忙）*/
    }
}

uint8_t *treg(unsigned a)
{
    if (a == 0x7FB4) g_cmd = 1;             /* 命令寄存器被访问 = 一条新命令 */
    return g_mem + (a & 0xFFFF);
}

/* 窗口择路：这是整个测试的核心，照 RTL 抄。
 *   $7FC0 = 3            → PSRAM 页（页号来自 $4000）
 *   $7FC0 bit0/bit2 = 0  → SD 扇区缓冲区（$7F30 bit0 使能；bit1 读状态）
 *   其它                 → 谁都不指向（真机上也不是这两块）
 */
uint8_t *win_ptr(void)
{
    uint8_t wm = g_mem[0x7FC0];

    if (wm == 3)
        return g_psram + ((uint32_t)g_mem[0x4000] << 13);
    if (!(wm & 0x05) && (g_mem[0x7F30] & 1)) {
        sd_service();
        return (g_mem[0x7F30] & 2) ? g_alt : g_sdbuf;
    }
    return g_dummy;
}

/* ---- 显示层桩 ----
 * save.c 现在把备份三屏的绘制放在 video.c（真机 / 模拟器共用），而本测试
 * 只编 fpga/fat/fatw/save，不编 video.c，所以这三个 screen_* 要自己给桩。
 * 真机文案是汉字（行 8「正在备份」、行 9「完成」/「备份失败」），这里用
 * 全大写的记号代替 —— 本测试验的是「哪一行被写了什么」，不是字模；
 * 汉字排版归 emu_test.py 管（那边能读 tilemap）。*/
char g_scr[18][24];
void cjk_begin(void) {}
void cjk_puts(uint8_t r, uint8_t c, const uint8_t *s) { (void)r; (void)c; (void)s; }
void cjk_center(uint8_t r, const uint8_t *s) { (void)r; (void)s; }
void cjk_center_wide(uint8_t r, const uint8_t *s) { (void)r; (void)s; }
void video_off(void) {}
void video_on(void) {}
void clear_screen(void) { memset(g_scr, 0, sizeof g_scr); }
void print_center(uint8_t r, const char *s)
{
    size_t i;
    for (i = 0; i < 23 && s[i]; i++) g_scr[r][i] = s[i];
    g_scr[r][i] = 0;
}
void screen_backup_confirm(const uint16_t *lfn, const char *shortname)
{
    (void)lfn; (void)shortname;
    memset(g_scr, 0, sizeof g_scr);
    print_center(5, "BACKUP?");
}
void screen_backing_up(void)
{
    memset(g_scr, 0, sizeof g_scr);
    print_center(8, "ZH_BACKING");      /* 真机：正在备份 */
}
void screen_save_result(uint8_t err)
{
    print_center(9, err ? "ZH_FAIL" : "ZH_DONE");   /* 真机：备份失败 / 完成 */
}
static void pr_scr_tag(const char *tag)
{
    int r;
    char *p;
    for (r = 7; r <= 9; r++)
        for (p = g_scr[r]; *p; p++) if (*p == ' ') *p = '_';
    printf("SCR7%s %s\n", tag, g_scr[7]);
    printf("SCR8%s %s\n", tag, g_scr[8]);
    printf("SCR9%s %s\n", tag, g_scr[9]);
}
static void pr_scr(void) { pr_scr_tag(""); }
void print_dec2(uint8_t r, uint8_t c, uint8_t v) { (void)r; (void)c; (void)v; }
void wait_frames(uint8_t n) { (void)n; }

#include "fat.h"
#include "save.h"
#include "game.h"

static void rd_img(const char *p)
{
    FILE *f = fopen(p, "rb");
    if (!f) { printf("IMG_OPEN\n"); exit(2); }
    if (fread(g_img, 1, (size_t)TOTAL_SEC * 512, f) != (size_t)TOTAL_SEC * 512) {
        printf("IMG_SHORT\n"); exit(2);
    }
    fclose(f);
}
static void wr_img(const char *p)
{
    FILE *f = fopen(p, "wb");
    fwrite(g_img, 1, (size_t)TOTAL_SEC * 512, f);
    fclose(f);
}

/* PSRAM 存档区（页 0..3）填成 tag 图案 */
static void fill_save(uint8_t tag)
{
    unsigned k;
    for (k = 0; k < 32768; k++)
        g_psram[k] = (uint8_t)(((k * 31 + (k >> 9) * 7 + tag) ^ (k >> 5)) & 0xFF);
}
static unsigned long hash_save(void)
{
    unsigned long h = 5381;
    unsigned k;
    for (k = 0; k < 32768; k++) h = h * 33 + g_psram[k];
    return h & 0xFFFFFFFFUL;
}
/* 元数据页清成 $FF（= 没有待备份记录），并把电池哨兵盖成 $88。
 * 真机上 $FF 的哨兵等于 BATTERY DRY，那是另一个用例（op 'd'）要测的东西；
 * 其它用例想表达的是"电池没问题、只是没有存档记录"，所以这里补上。*/
static void meta_clear(void)
{
    uint8_t *m = g_psram + PSRAM_META_ * 0x2000;
    memset(m, 0xFF, 0x2000);
    m[0x201] = 0x88;
    /* 主机模型没有真实按键：buttons() 恒返回全按下，手动模式的
     * ask_backup() -> wait_release() 会无限空转。这里显式把备份开关设成
     * 「自动」（双字节都 = AUTO_ON 0x5A），等价于玩家在设置页切到自动，
     * 让存档全流程照常跑完。手动确认屏的交互由 emu_test.py 覆盖。*/
    m[0x120] = 0x5A;
    m[0x121] = 0x5A;
}
/* 电池没电：哨兵被清成 $FF，但**待备份章 $AA 还在** —— 这是最要命的情形，
 * 也正是官方 BATTERY DRY 提示要挡的那一种：章还在，数据已经废了。存档区同时
 * 衰减成 $FF/$00 交替（真机上就是这种随机字节）。*/
static void canary_dry(void)
{
    uint8_t *m = g_psram + PSRAM_META_ * 0x2000;
    unsigned k;

    m[0x000] = 0xAA;                    /* 章还在：说要备份 */
    m[0x001] = 4;                       /* 32KB */
    m[0x00F] = 7;
    memcpy(m + 0x010, "POKEMON", 7);
    m[0x201] = 0xFF;                    /* 但电池没电 */
    for (k = 0; k < 32768; k++)
        g_psram[k] = (uint8_t)((k & 1) ? 0xFF : 0x00);
}
/* 往元数据页写一条合法的"上次启动的是哪个游戏"记录（$A300 那 18 字节，
 * 13 字节完整 8.3 名 + 4 字节目录 + 1 字节校验，见 src/save.c 的 META_REC）。*/
static void meta_name(const char *s)
{
    uint8_t *p = g_psram + PSRAM_META_ * 0x2000 + 0x300;
    size_t n = strlen(s);
    unsigned i;
    uint8_t x = 0x5A;

    memset(p, 0, 18);
    if (n > 12) n = 12;
    memcpy(p, s, n);
    for (i = 0; i < 17; i++) x ^= p[i];
    p[17] = x;
}

/* ---- 诊断用：把 SAVER 目录的前 32 个槽原样 dump 出来 ----
 * 真机反馈「备份显示完成、卡上找不到中文名 .sav」，靠文件名猜了三轮没结论，
 * 这里直接看目录项字节：第几槽、短名 11 字节、属性、大小。op 'q' 用。*/
extern uint32_t fdata_base;
extern uint8_t  fspc_shift;
extern uint8_t  fcsize;
uint32_t fat_find_dir(const uint8_t *raw11);
uint8_t  fatw_dir_seek(const uint8_t *raw11, uint32_t *lba, uint16_t *off);

static void dump_dir(uint32_t c, const char *tag)
{
    uint32_t lba;
    uint8_t  s;
    uint16_t i;

    if (c < 2) { printf("%s NOCLUSTER\n", tag); return; }
    lba = fdata_base + ((c - 2) << fspc_shift);
    for (s = 0; s < fcsize && s < 8; s++) {
        sd_read(lba + s, fbuf);
        for (i = 0; i < 512; i += 32) {
            const uint8_t *e = fbuf + i;
            uint16_t slot = (uint16_t)(s * 16 + (i >> 5));

            if (e[0] == 0x00) { printf("%s %02u END\n", tag, slot); continue; }
            printf("%s %02u %02X %02X %02X %02X %02X %02X %02X %02X %02X "
                   "%02X %02X %02X A%02X S%lu\n",
                   tag, slot, e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[7],
                   e[8], e[9], e[10], e[11], e[11],
                   (unsigned long)((uint32_t)e[28] | ((uint32_t)e[29] << 8) |
                                   ((uint32_t)e[30] << 16) |
                                   ((uint32_t)e[31] << 24)));
        }
    }
}

int main(int argc, char **argv)
{
    GameEntry g;
    uint8_t e, banks, op;

    if (argc < 3) { printf("USAGE\n"); return 2; }
    op = (uint8_t)(argv[1][0]);
    g_img = malloc((size_t)TOTAL_SEC * 512);
    rd_img(argv[2]);

    e = fat_mount();
    if (e) { printf("MOUNT_ERR %u\n", (unsigned)e); return 0; }

    memset(g.name, 0, sizeof g.name);
    strcpy(g.name, "POKEMON.GBC");
    g.cluster = 2;
    if (argc > 3) {                     /* 换个游戏名 / 换 tag */
        memset(g.name, 0, sizeof g.name);
        strncpy(g.name, argv[3], 12);
    }

    memset(g_psram, 0, sizeof g_psram);
    fill_save(TAG_);
    if (op == 'o') {                    /* 元数据页记的是别的游戏（POKEMON），这次启动 OTHER */
        meta_clear();
        meta_name("POKEMON");
    } else if (op == 'r') {             /* 模拟"上次启动过这个游戏" */
        meta_clear();
        meta_name("POKEMON.GBC");
    } else if (op == 'u') {             /* 官方内核留下的元数据：$010 是路径，$1F0 全 $FF */
        meta_clear();
        memcpy(g_psram + PSRAM_META_ * 0x2000 + 0x010, "/SAVER/POKEMON.SAV", 18);
        g_psram[PSRAM_META_ * 0x2000 + 0x000] = 0x00;
    } else {
        meta_clear();                   /* 首次：元数据页全 $FF */
    }

    if (op == 'd') {                    /* BATTERY DRY：开机先查电池，再启动游戏 */
        canary_dry();
        save_boot();                    /* 应当提示、清章、**一个字节都不写** */
        printf("WRITES_BOOT %u\n", g_wr_sectors);
        pr_scr_tag("_DRY");
        printf("CANARY_AFTER %02X\n", g_psram[PSRAM_META_ * 0x2000 + 0x201]);
        g_wr_sectors = 0;
        banks = save_prepare(&g);
        printf("BANKS %u\n", banks);
        printf("HASH_AFTER_PREPARE %08lX\n", hash_save());
        fill_save(TAG2_);
        save_boot();                    /* 这一次哨兵已经是 $88 → 正常备份 */
        printf("WRITES %u\n", g_wr_sectors);
        pr_scr();
        wr_img(argv[2]);
        printf("HASH_END %08lX\n", hash_save());
        return 0;
    }

    if (op == 'L') {                    /* 「运行上次游戏」按目录簇 + 全名定位 ROM */
        GameEntry g2;
        uint32_t before = fat_cwd;
        unsigned ok;

        ok = fat_locate(0, "POKEMON.GBC", &g2);
        printf("LOC_ROOT %u\n", ok);
        printf("LOC_ROOT_NAME %s\n", ok ? g2.name : "-");
        printf("LOC_ROOT_SIZE %u\n", ok ? (unsigned)g2.size : 0);
        ok = fat_locate(SUBDIR_CLUSTER, "SUBROM.GBC", &g2);
        printf("LOC_SUB %u\n", ok);
        printf("LOC_SUB_NAME %s\n", ok ? g2.name : "-");
        printf("CWD_UNCHANGED %u\n", (unsigned)(fat_cwd == before));
        printf("LOC_MISS %u\n", (unsigned)fat_locate(SUBDIR_CLUSTER, "NOPE.GBC", &g2));
        return 0;
    }

    if (op == 'M') {                    /* 元数据校验：首次按 START 不能出乱码 */
        uint8_t *m = g_psram + PSRAM_META_ * 0x2000;
        const char *want = "POKEMON.GBC";
        unsigned i;
        uint8_t x;

        meta_clear();                   /* 全 $FF：新卡 / 刚刷完机 */
        save_boot();
        printf("DIRTY_FULL_EMPTY %u\n", (unsigned)(save_last_full()[0] == 0));
        printf("DIRTY_LFN_EMPTY %u\n", (unsigned)(save_last_lfn()[0] == 0));

        /* v0.12.1：一条 18 字节、带异或校验的记录，$A300 / $A320 各一份。
         * 这里按格式手写一份合法的，模拟"上次启动过 POKEMON.GBC"。*/
        memset(m + 0x300, 0, 18);
        for (i = 0; want[i]; i++) m[0x300 + i] = (uint8_t)want[i];
        x = 0x5A;
        for (i = 0; i < 17; i++) x ^= m[0x300 + i];
        m[0x300 + 17] = x;
        m[0x000] = 0;                  /* 没有待备份章，别进备份流程 */
        save_boot();
        printf("GOOD_FULL %s\n", save_last_full());

        /* 第一份的某个字节被改了（电池衰减 / 别的东西覆盖）：校验不符 */
        m[0x300 + 3] = (uint8_t)(m[0x300 + 3] ^ 0xFF);
        save_boot();
        printf("BADXOR_FULL_EMPTY %u\n", (unsigned)(save_last_full()[0] == 0));

        /* 第一份坏、第二份好 —— 冗余的意义就在这里：记录照样读得回来。
         * 注意先恢复第一份的字节，只留第二份坏，才验得到"坏一份还有另一份"。*/
        m[0x300 + 3] = (uint8_t)(m[0x300 + 3] ^ 0xFF);
        memset(m + 0x320, 0, 18);
        for (i = 0; want[i]; i++) m[0x320 + i] = (uint8_t)want[i];
        m[0x320 + 13] = 3;              /* 记成「子目录 3」 */
        x = 0x5A;
        for (i = 0; i < 17; i++) x ^= m[0x320 + i];
        m[0x320 + 17] = x;
        m[0x300 + 3] = 0xEE;            /* 第一份故意坏掉 */
        save_boot();
        printf("SECOND_FULL %s\n", save_last_full());
        printf("SECOND_DIR %lu\n", (unsigned long)save_last_dir());

        /* v0.12.5：8.3 短名里可能有**高位字节**。
         *
         * 真机那张卡上 "K 口袋妖怪 水晶…" 的短名字段是
         *   4B BF DA B4 FC D1 FD B9  =  'K' 口 袋 妖 怪(前半字节)
         * —— 生成短名的工具把 GBK 字节直接写进 8.3 字段，还按字节截断把
         * 一个汉字砍成半个。旧判据 `c < 0x20 || c > 0x7E` 把它整条作废，
         * 于是记录写对了也永远 REC=--。
         *
         * 这里照那张卡的真实字节造一条记录，必须原样读回来。
         * 期望串：K + 口 + 袋 + 妖 + 怪(半个) + ".GBC" = 12 字节。*/
        {
            static const char gbk[13] = {
                (char)0x4B, (char)0xBF, (char)0xDA, (char)0xB4,
                (char)0xFC, (char)0xD1, (char)0xFD, (char)0xB9,
                '.', 'G', 'B', 'C', 0
            };

            memset(m + 0x300, 0, 18);
            for (i = 0; gbk[i]; i++)
                m[0x300 + i] = (uint8_t)gbk[i];
            x = 0x5A;
            for (i = 0; i < 17; i++)
                x ^= m[0x300 + i];
            m[0x300 + 17] = x;
            m[0x000] = 0;
            save_boot();
            printf("GBK_OK %u\n", (unsigned)(
                !memcmp(save_last_full(), gbk, 13) &&
                save_last_dir() == 0));
            printf("GBK_LEN %u\n", (unsigned)strlen(save_last_full()));
        }
        return 0;
    }

    if (op == 'q') {                    /* SAVER 里已有 12 个存档，长名还写得进去吗 */
        static const uint8_t saver11[11] =
            {'S','A','V','E','R',' ',' ',' ',' ',' ',' '};
        FatFile f;
        char nm[13];
        uint16_t ln[40];
        uint8_t k, r;
        uint32_t sc;

        /* 照真机那张卡造：12 个存档，**每个都带长名项**（中文名在卡上必然
         * 有 LFN），加 "." ".."，空槽起点落在**扇区中间**而不是扇区开头 ——
         * 这正是 v0.12.6「本扇区尾部腾 4 槽」补丁覆盖不到的形状。
         * 15 字符主体 → 2 条 LFN + 1 条短名项 = 3 槽/个，12 个 = 36 槽。*/
        for (k = 0; k < 12; k++) {
            uint16_t nn[24];
            uint8_t  j;

            sprintf(nm, "GAME%02u.GB", (unsigned)k);
            for (j = 0; j < 15; j++) nn[j] = (uint16_t)('a' + (k % 26));
            nn[15] = '.'; nn[16] = 'g'; nn[17] = 'b'; nn[18] = 'c'; nn[19] = 0;
            printf("Q_ADD %u %u\n", (unsigned)k,
                   (unsigned)fat_open_sav(&f, nm, 8192, nn));
        }
        /* 30 字符长名 → 3 条 LFN + 1 条短名项 = 4 个连续槽。
         * 必须有 '.'（sav_lfn 按最后一个点切扩展名），
         * 否则整段主体都会被丢掉、只剩 ".sav"。*/
        for (k = 0; k < 26; k++) ln[k] = (uint16_t)('A' + (k % 26));
        ln[26] = '.';
        ln[27] = 'g';
        ln[28] = 'b';
        ln[29] = 'c';
        ln[30] = 0;

        r = fat_open_sav(&f, "POKEMON.GBC", 32768, ln);
        printf("Q_R %u\n", (unsigned)r);
        sc = fat_find_dir(saver11);
        printf("Q_SAVER %lu\n", (unsigned long)sc);
        printf("Q_SLBA %lu\n", (unsigned long)(fdata_base + ((sc - 2) << fspc_shift)));
        printf("Q_CSPC %u\n", (unsigned)fcsize);
        /* 目录项到底落在哪个扇区？真机上"屏幕报完成、卡上找不到文件"的唯一
         * 自洽解释就是：项写到了 SAVER 簇链**之外**的扇区，而 save_verify
         * 用同一套寻址读回来，于是校验通过。这里把落点 LBA 打出来，断言它
         * 落在 [SAVER 首扇区, +每簇扇区数) 之内。*/
        {
            static const uint8_t sav11[11] =
                {'P','O','K','E','M','O','N',' ','S','A','V'};
            uint32_t el = 0, rl = 0;
            uint16_t eo = 0, ro = 0;
            uint8_t  rn = 0;
            uint8_t  rr;

            fat_dir_enter(sc);          /* dir_seek 用的是 fat_cwd，必须先切到 SAVER */
            rr = fatw_dir_seek(sav11, &el, &eo);
            printf("Q_ENT %u %lu %u\n", (unsigned)rr,
                   (unsigned long)el, (unsigned)eo);
            fat_dir_reset();
        }
        dump_dir(sc, "QD");
        wr_img(argv[2]);            /* 断言要按卡上的字节来核 */
        return 0;
    }

    if (op == 'R') {                    /* 12 个**短名**存档：$00 落在槽 14，长名跨扇区 */
        static const uint8_t saver11[11] =
            {'S','A','V','E','R',' ',' ',' ',' ',' ',' '};
        FatFile f;
        char nm[13];
        uint16_t ln[40];
        uint8_t k, r;
        uint32_t sc;

        /* 12 个短名 = 12 槽，加 "." ".." 共 14 槽 = 448 字节，$00 正好落在
         * 槽 14（扇区 0 内，距扇区尾 2 槽）。要写 3 条长名 + 1 条短名 = 4 槽，
         * 本扇区放不下 → 必须把尾部 2 个 $00 标成 $E5，让追加位落到下一扇区。*/
        for (k = 0; k < 12; k++) {
            sprintf(nm, "GAME%02u.GB", (unsigned)k);
            printf("Q_ADD %u %u\n", (unsigned)k,
                   (unsigned)fat_open_sav(&f, nm, 8192, (const uint16_t *)0));
        }
        for (k = 0; k < 26; k++) ln[k] = (uint16_t)('A' + (k % 26));
        ln[26] = '.'; ln[27] = 'g'; ln[28] = 'b'; ln[29] = 'c'; ln[30] = 0;
        r = fat_open_sav(&f, "POKEMON.GBC", 32768, ln);
        printf("Q_R %u\n", (unsigned)r);
        sc = fat_find_dir(saver11);
        printf("Q_SAVER %lu\n", (unsigned long)sc);
        printf("Q_SLBA %lu\n", (unsigned long)(fdata_base + ((sc - 2) << fspc_shift)));
        printf("Q_CSPC %u\n", (unsigned)fcsize);
        {
            static const uint8_t sav11[11] =
                {'P','O','K','E','M','O','N',' ','S','A','V'};
            uint32_t el = 0, rl = 0;
            uint16_t eo = 0, ro = 0;
            uint8_t  rn = 0, rr;

            fat_dir_enter(sc);
            rr = fatw_dir_seek(sav11, &el, &eo);
            printf("Q_ENT %u %lu %u\n", (unsigned)rr, (unsigned long)el, (unsigned)eo);
            fat_dir_reset();
        }
        wr_img(argv[2]);
        return 0;
    }

    if (op == 'p') {                    /* SAVER 被填满（连 $00 都不剩）→ 必须扩簇 */
        static const uint8_t saver11[11] =
            {'S','A','V','E','R',' ',' ',' ',' ',' ',' '};
        FatFile f;
        char nm[13];
        uint16_t ln[40];
        uint8_t k, r;
        uint32_t sc;

        /* 每簇 1 扇区 = 16 槽，"." ".." 占 2 槽，剩下 14 槽用短名存档填满：
         * 目录里连 $00 结尾都不剩。这是 dir_nth 最容易走到链外去的情形 ——
         * 早先它按 FAT32 的 $0FFFFFF8 比 FAT16 的链尾 $FFFF，于是"还有下一簇"，
         * cluster_sector() 算出一个数据区之外的野扇区，目录项就写到那儿去了：
         * 卡上干净，屏幕报「完成」。*/
        for (k = 0; k < 14; k++) {
            sprintf(nm, "F%02u.GB", (unsigned)k);
            printf("Q_ADD %u %u\n", (unsigned)k,
                   (unsigned)fat_open_sav(&f, nm, 8192, (const uint16_t *)0));
        }
        for (k = 0; k < 26; k++) ln[k] = (uint16_t)('A' + (k % 26));
        ln[26] = '.'; ln[27] = 'g'; ln[28] = 'b'; ln[29] = 'c'; ln[30] = 0;
        r = fat_open_sav(&f, "POKEMON.GBC", 32768, ln);
        printf("Q_R %u\n", (unsigned)r);
        sc = fat_find_dir(saver11);
        printf("Q_SAVER %lu\n", (unsigned long)sc);
        printf("Q_SLBA %lu\n", (unsigned long)(fdata_base + ((sc - 2) << fspc_shift)));
        printf("Q_CSPC %u\n", (unsigned)fcsize);
        {
            static const uint8_t sav11[11] =
                {'P','O','K','E','M','O','N',' ','S','A','V'};
            uint32_t el = 0, rl = 0;
            uint16_t eo = 0, ro = 0;
            uint8_t  rn = 0, rr;

            fat_dir_enter(sc);
            rr = fatw_dir_seek(sav11, &el, &eo);
            printf("Q_ENT %u %lu %u\n", (unsigned)rr, (unsigned long)el, (unsigned)eo);
            fat_dir_reset();
        }
        wr_img(argv[2]);
        return 0;
    }

    if (op == 'z') {                    /* 无存档 RAM 的卡带也要记「上次运行」 */
        banks = save_prepare(&g);
        printf("Z_BANKS %u\n", banks);
        save_boot();
        printf("Z_FULL [%s]\n", save_last_full());
        return 0;
    }

    if (op == 'i') {                    /* 子目录里的游戏：存档仍进根目录的 SAVER */
        FatFile ff;
        uint32_t sz = 0;

        if (!fat_dir_enter(SUBDIR_CLUSTER)) { printf("ENTER_FAIL\n"); return 0; }
        banks = save_prepare(&g);    /* 盖章：META_DIR 记成子目录簇（菜单定位用）*/
        printf("BANKS %u\n", banks);
        fill_save(TAG2_);               /* 游戏里存了一次 */
        fat_dir_reset();                /* 模拟关机重启：fat_cwd 回到根目录 */
        save_boot();                    /* v0.11.0：一律写进根目录唯一的 SAVER */
        printf("WRITES %u\n", g_wr_sectors);
        pr_scr();
        wr_img(argv[2]);

        if (fat_open_read(&ff, "SAVNAME83", &sz) == FAT_OK)
            printf("ROOT_READ %u\n", (unsigned)sz);
        else
            printf("ROOT_READ 0\n");
        printf("HASH_END %08lX\n", hash_save());
        return 0;
    }

    if (op == 'K') {                    /* ★ sav_ident 必须是不改写输入的纯函数 */
        uint16_t buf[24], copy[24];
        char a[13], b[13];
        unsigned i;

        for (i = 0; i < 24; i++) buf[i] = (uint16_t)(0x4E00 + i * 37);
        buf[20] = 0;
        memcpy(copy, buf, sizeof buf);
        sav_ident("POKEMON.GBC", buf, a);
        sav_ident("POKEMON.GBC", buf, b);
        /* 如果哪天有人为了省 96 字节，改成在 sav_ident 里就地写 lfn，这两条就红：
         * 纯函数被破坏之后，save_boot 里「先算名、再交给 fat_open_sav」的顺序
         * 就不再安全 —— 那正是 v0.16.0 真机翻车的机制。*/
        printf("K_PURE %u\n", (unsigned)(memcmp(buf, copy, sizeof buf) == 0));
        printf("K_IDEM %u\n", (unsigned)(strcmp(a, b) == 0));
        printf("K_ID %s\n", a);
        return 0;
    }

    if (op == 'Z') {                    /* ★ 中文长名端到端：启动 → 游戏内存档 → 备份 */
        banks = save_prepare(&g);
        printf("Z_BANKS %u\n", banks);
        fill_save(TAG2_);               /* 游戏里存了一次 */
        save_boot();                    /* fat_full_lfn → sav_ident → dump → verify */
        printf("Z_WRITES %u\n", g_wr_sectors);
        pr_scr();
        wr_img(argv[2]);
        return 0;
    }

    banks = save_prepare(&g);
    printf("BANKS %u\n", banks);
    printf("HASH_AFTER_PREPARE %08lX\n", hash_save());

    if (op == 'N') {                    /* 端到端：启动写元数据 → 重启读回 */
        banks = save_prepare(&g);
        printf("N_BANKS %u\n", banks);
        save_boot();
        printf("N_FULL [%s]\n", save_last_full());
        printf("N_NAME [%s]\n", save_last_name());
        printf("N_LFN [%04X %04X %04X]\n", (unsigned)save_last_lfn()[0],
               (unsigned)save_last_lfn()[1], (unsigned)save_last_lfn()[2]);
        printf("N_DIR %lu\n", (unsigned long)save_last_dir());
        return 0;
    }

    if (op == 'e') {                    /* 游戏里存了一次 → 换图案 → 落盘 */
        fill_save(TAG2_);
        printf("HASH_BEFORE_DUMP %08lX\n", hash_save());
        save_boot();
        printf("WRITES %u\n", g_wr_sectors);
        pr_scr();
        wr_img(argv[2]);
    } else if (op == 'w') {             /* 用户把电脑上的备份拷到卡上 → 别覆盖它 */
        save_boot();                    /* 第一轮：建指纹并备份 */
        g_wr_sectors = 0;
        {                               /* 模拟换卡上的文件：把 .SAV 第一个字节翻一下 */
            FatFile f;
            uint32_t sz = 0;
            if (fat_open_read(&f, "SAVNAME83", &sz) == FAT_OK) {
                size_t off = (size_t)fat_cluster_lba(f.cluster) * 512;
                g_img[off] ^= 0xFF;
                printf("CARD_BYTE %02X\n", g_img[off]);
            } else {
                printf("CARD_BYTE OPEN_FAIL\n");
            }
        }
        memset(g_scr, 0, sizeof g_scr);
        save_boot();                    /* 第二天开机：芯片没变 → 一个字节都不写 */
        printf("WRITES %u\n", g_wr_sectors);
        pr_scr();
        wr_img(argv[2]);
    } else if (op == 's') {             /* 存档没变：第二轮开机不该再写卡 */
        save_boot();                    /* 第一轮：建指纹（这一次会备份）*/
        printf("WRITES_FIRST %u\n", g_wr_sectors);
        g_wr_sectors = 0;
        banks = save_prepare(&g);       /* 再进一次游戏，chip 内容 = 卡上那份，没变 */
        printf("HASH_PREPARE2 %08lX\n", hash_save());
        memset(g_scr, 0, sizeof g_scr); /* 清掉上一轮的屏幕残留 */
        save_boot();                    /* 第二轮：指纹应当对上 → 不写 */
        printf("WRITES %u\n", g_wr_sectors);
        pr_scr();
        wr_img(argv[2]);
    }
    printf("HASH_END %08lX\n", hash_save());
    return 0;
}
'''


def build(tmp, tag, tag2):
    stub = os.path.join(tmp, "win_stub.c")
    code = (PROG.replace("TAG_", str(tag)).replace("TAG2_", str(tag2))
                .replace("PSRAM_META_", hex(PSRAM_META))
                .replace("SUBDIR_CLUSTER", str(SUBDIR_CLUSTER))
                .replace("TOTAL_SEC", str(TOTAL_SEC))
                .replace("SAVNAME83", sav_ident_str("POKEMON.GBC")))
    open(stub, "w").write(code)
    hh = os.path.join(tmp, "host_hw.h")
    open(hh, "w").write(HOST_HW)
    exe = os.path.join(tmp, "t")
    cmd = ["gcc", "-O1", "-fno-inline", "-I" + SRC, "-include", hh, stub,
           os.path.join(SRC, "fpga.c"), os.path.join(SRC, "fat.c"),
           os.path.join(SRC, "fatw.c"), os.path.join(SRC, "fatn.c"),
           os.path.join(SRC, "fatx.c"), os.path.join(SRC, "save.c"),
           os.path.join(SRC, "savkey.c"),
           "-o", exe]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("  gcc 失败:\n" + r.stderr)
        sys.exit(1)
    return exe


def run(exe, op, img, name=""):
    cmd = [exe, op, img] + ([name] if name else [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = {}
    for line in r.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    if r.returncode != 0:
        print("  运行失败:", r.returncode, r.stdout, r.stderr)
        sys.exit(1)
    return out


def hash_py(tag):
    h = 5381
    for k in range(32768):
        h = (h * 33 + pat(k, tag)) & 0xFFFFFFFF
    return h


def seed_rom(img, geo, ram=3):
    """写一个假 ROM：占簇 2，头 $149 = ram（默认 3 = 32KB 存档 RAM）。

    ram=0 用来测「没有存档 RAM 的卡带」——它在 save_prepare 里 banks = 0，
    但「上次运行的是哪个 ROM」照样得记下来（v0.12.3 修的就是这个）。

    FAT 里必须标成已用，否则 .SAV 会被分到同一簇上，把 ROM 头覆盖掉。"""
    rom = bytearray(SEC)
    rom[0:8] = b"POKEMON\x00"
    rom[0x149] = ram
    with open(img, "r+b") as f:
        f.seek(geo["data_lba"] * SEC)
        f.write(rom)
        for k in range(2):                      # 两张 FAT 副本
            f.seek((RES_SEC + k * geo["fat_sz"]) * SEC + ROM_CLUSTER * 2)
            f.write(b"\xff\xff")
        f.seek(geo["root_lba"] * SEC + 32)      # 根目录第 1 槽（第 0 槽是卷标）
        ent = bytearray(32)
        ent[0:11] = b"POKEMON GBC"
        ent[11] = 0x20
        ent[26] = ROM_CLUSTER & 0xFF
        ent[27] = ROM_CLUSTER >> 8
        ent[28:32] = (2048).to_bytes(4, "little")
        f.write(ent)


def lfn_checksum(name11):
    """VFAT 短名校验和：11 字节循环右移累加。"""
    s = 0
    for b in name11:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def lfn_entries(name11, text):
    """把长名切成 VFAT LFN 目录项（UCS-2LE）。

    物理顺序是**倒序**的：最后一段（序号 n，带 0x40）排在最前，序号 1 紧邻
    短名项。fat_full_lfn() 正是按序号摆回去的（fatn.c:80-95），所以这里必须
    照 FAT 规范生成，不能顺序写。
    """
    chk = lfn_checksum(name11)
    u = [ord(c) for c in text] + [0]                 # 结尾 0x0000
    while len(u) % 13:
        u.append(0xFFFF)                             # 尾部填充
    n = len(u) // 13
    ents = []
    for seq in range(n, 0, -1):
        chunk = u[(seq - 1) * 13: seq * 13]
        e = bytearray(32)

        def put(off, chars):
            for k, c in enumerate(chars):
                e[off + 2 * k] = c & 0xFF
                e[off + 2 * k + 1] = (c >> 8) & 0xFF

        e[0] = 0x40 | seq if seq == n else seq
        put(1, chunk[0:5])                           # 字符 0-4
        e[11] = 0x0F                                 # 属性：长名
        e[13] = chk
        put(14, chunk[5:11])                         # 字符 5-10
        put(28, chunk[11:13])                        # 字符 11-12
        ents.append(bytes(e))
    return ents


def seed_rom_lfn(img, geo, name83, text, ram=3):
    """假 ROM + 卡上的中文长名（LFN）。

    ★ 这是 v0.16.0「长名 ROM 备份失败」那个 bug 的唯一可测形状：
      seed_rom() 只写 8.3 项 → fat_full_lfn() 返回 0 → sav_ident() 走
      「8.3 名派生」分支 → 两个名字天然一致，bug 永远测不出来。
      真机上的中文 ROM 一定带 LFN，走的是另一条分支：sav_ident() 读
      g_lfn_buf，而 fat_open_sav() 会把这个缓冲**就地**改成 "….sav"。
    """
    rom = bytearray(SEC)
    rom[0:8] = b"KCHN\x00\x00\x00\x00"
    rom[0x149] = ram
    ents = lfn_entries(name83, text)
    with open(img, "r+b") as f:
        f.seek(geo["data_lba"] * SEC)
        f.write(rom)
        for k in range(2):
            f.seek((RES_SEC + k * geo["fat_sz"]) * SEC + ROM_CLUSTER * 2)
            f.write(b"\xff\xff")
        # 槽 0 是卷标（build_image 已写），LFN 从槽 1 起，短名项紧随其后。
        f.seek(geo["root_lba"] * SEC + 32)
        for e in ents:
            f.write(e)
        ent = bytearray(32)
        ent[0:11] = name83
        ent[11] = 0x20
        ent[26] = ROM_CLUSTER & 0xFF
        ent[27] = ROM_CLUSTER >> 8
        ent[28:32] = (2048).to_bytes(4, "little")
        f.write(ent)


def add_subdir(img, geo):
    """在根目录加一个 SUBDIR（簇 5），目录里有 "."、".." 和一个假 ROM。

    用来验证两件事（v0.11.0）：① 子目录里的游戏，存档仍只进根目录的 SAVER，
    子目录里既不建 SAVER 也不落 .SAV；② START 菜单能按目录簇 + 完整 8.3 名
    在子目录里精确定位到 SUBROM.GBC。"""
    with open(img, "r+b") as f:
        for k in range(2):
            f.seek((RES_SEC + k * geo["fat_sz"]) * SEC + SUBDIR_CLUSTER * 2)
            f.write(b"\xff\xff")                # 目录簇标记为 EOF
        f.seek(geo["root_lba"] * SEC + 64)      # 根目录第 2 槽
        d = bytearray(32)
        d[0:11] = b"SUBDIR     "
        d[11] = 0x10
        d[26] = SUBDIR_CLUSTER & 0xFF
        d[27] = SUBDIR_CLUSTER >> 8
        f.write(d)
        f.seek((geo["data_lba"] + (SUBDIR_CLUSTER - 2) * geo["spc"]) * SEC)
        de = bytearray(SEC)
        de[0:11] = b".          "
        de[11] = 0x10
        de[26] = SUBDIR_CLUSTER & 0xFF
        de[27] = SUBDIR_CLUSTER >> 8
        de[32:43] = b"..         "
        de[43] = 0x10
        de[64:75] = b"SUBROM  GBC"       # 子目录里的假 ROM，给 fat_locate 定位
        de[75] = 0x20
        de[90] = ROM_CLUSTER & 0xFF
        de[91] = ROM_CLUSTER >> 8
        de[92:96] = (2048).to_bytes(4, "little")
        f.write(de)


def q_out(stdout):
    """把 op 'q' 的 Q_ 行收成字典（同名出现多次的收成列表）。"""
    out = {}
    for l in stdout.splitlines():
        if l.startswith("Q_") and not l.startswith("QD"):
            kk, *vv = l.split()
            out.setdefault(kk, []).append(vv)
            print("   " + l)
    return out


def fat_chain(data, geo, start):
    """从 start 簇开始走 FAT 链，返回簇号列表（够核对目录项落点就行）。"""
    base = RES_SEC * SEC
    out = []
    c = start
    eof = 0x0FFFFFF8 if geo["fat32"] else 0xFFF8
    while 2 <= c < eof and len(out) < 512:
        out.append(c)
        if geo["fat32"]:
            nxt = int.from_bytes(data[base + c * 4: base + c * 4 + 4], "little")
            c = nxt & 0x0FFFFFFF
        else:
            c = int.from_bytes(data[base + c * 2: base + c * 2 + 2], "little")
    return out


def check_sav_entry(img_path, out, tag, geo):
    """核对**卡上的字节**：目录项落点是否在 SAVER 簇链里、名字、3 条长名项。

    ★ 这条断言是真机「屏幕报完成、卡上找不到文件」的照妖镜：项要是写到了
    SAVER 簇链之外（野扇区），save_verify 用同一套寻址读回来照样能过，
    只有按字节核卡、并顺着 FAT 链核落点，才看得见。"""
    if out.get("Q_R", [["?"]])[0][0] != "0":
        print(f"  FAIL {tag}：fat_open_sav 返回 {out['Q_R'][0][0]}（应当 0）")
        return False
    rr, elba, eoff = (int(x) for x in out["Q_ENT"][0][:3])
    if rr != 1:
        print(f"  FAIL {tag}：写完之后 SAVER 里找不到 POKEMON.SAV（dir_seek={rr}）")
        return False

    data = open(img_path, "rb").read()
    sv = int(out["Q_SAVER"][0][0])
    chain = fat_chain(data, geo, sv) if sv >= 2 else []
    cl = (elba - geo["data_lba"]) // geo["spc"] + 2 if elba >= geo["data_lba"] else -1
    if cl not in chain:
        print(f"  FAIL {tag}：目录项 LBA {elba}（簇 {cl}）不在 SAVER 簇链里 "
              f"{chain[:8]}{'…' if len(chain) > 8 else ''} —— 写到了链外")
        return False
    print(f"  OK   {tag}：目录项在 SAVER 簇链里（LBA {elba} = 簇 {cl}，"
          f"链长 {len(chain)} 簇，槽内偏移 {eoff}）")

    base = elba * SEC + eoff
    ent = data[base:base + 32]
    if ent[:11] != b"POKEMON SAV" or ent[11] != 0x20:
        print(f"  FAIL {tag}：该槽不是 POKEMON.SAV，而是 {ent[:12].hex()}")
        return False
    s = 0
    for i in range(11):
        s = (((s & 1) << 7) + (s >> 1) + ent[i]) & 0xFF
    bad = [k for k in range(3)
           if data[base - (k + 1) * 32 + 11] != 0x0F
           or data[base - (k + 1) * 32 + 13] != s]
    if bad:
        print(f"  FAIL {tag}：长名项缺失或校验和不符（第 {bad} 条）——"
              "Windows 会退回 8.3 名")
        return False
    print(f"  OK   {tag}：该槽是 POKEMON.SAV（属性 $20），紧邻 3 条长名项校验和配对")
    return True


def q_case(exe, img, tag, geo, op, nfill):
    """跑一次 op（先塞 nfill 个存档，再写一条 30 字符长名），核对结果。"""
    r = subprocess.run([exe, op, img], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        print(f"  FAIL 运行失败（退出码 {r.returncode}）")
        print(r.stdout[-1500:], r.stderr[-500:])
        return False
    out = q_out(r.stdout)
    adds = out.get("Q_ADD", [])
    bad = [f"#{v[0]}={v[1]}" for v in adds if v[1] != "0"]
    if len(adds) == nfill and not bad:
        print(f"  OK   {nfill} 个存档全部写成功")
        good = True
    else:
        print(f"  FAIL 存档写入失败：成功 {nfill - len(bad)}/{nfill}，失败项 {bad}")
        good = False
    return check_sav_entry(img, out, tag, geo) and good


def full_case(exe, img, geo):
    """SAVER 填满后：必须**自动扩一簇**把文件写进去，而不是报「目录已满」。

    v0.12.7 及以前这里是「老实报满」—— 因为 32KB 塞不下 dir_append。
    v0.13.0 把 fpga.c 挪回 _CODE 腾出空间后，扩簇做进去了：目录本来就是普通
    文件，链尾接一簇、清零，新簇就是一整片空槽。玩家的 SAVER 再也不会因为
    「槽用完了」而只能手动删存档。"""
    r = subprocess.run([exe, "p", img], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        print(f"  FAIL 运行失败（退出码 {r.returncode}）")
        print(r.stdout[-1500:], r.stderr[-500:])
        return False
    out = q_out(r.stdout)
    adds = out.get("Q_ADD", [])
    good = True
    if len(adds) == 14 and all(v[1] == "0" for v in adds):
        print("  OK   14 个存档把 16 槽（含 . 和 ..）刚好填满")
    else:
        print(f"  FAIL 填满失败：{adds}")

    rr = out["Q_R"][0][0]
    if rr == "0":
        print("  OK   目录满了不报错，扩簇后照样写成功")
    else:
        print(f"  FAIL 填满后返回 {rr}（应当 0 = 扩簇成功）")
        good = False

    data = open(img, "rb").read()
    sv = int(out["Q_SAVER"][0][0])
    chain = fat_chain(data, geo, sv) if sv >= 2 else []
    if len(chain) >= 2:
        print(f"  OK   SAVER 簇链从 1 簇长到 {len(chain)} 簇（真的扩了，不是写到链外）")
    else:
        print(f"  FAIL 簇链还是 {len(chain)} 簇 —— 文件项多半写到链外去了")
        good = False

    # 目录项必须落在这条（已经变长的）链里
    try:
        if not check_sav_entry(img, out, "填满后扩簇", geo):
            good = False
    except KeyError as e:
        print(f"  FAIL 缺少输出字段 {e}")
        good = False
    return good


def main():
    print("window_host_test — 存档全流程（真 fpga.c/fat.c/fatw.c/save.c + 窗口择路模型）")
    ok = True

    tmp = tempfile.mkdtemp(prefix="ezjr-win-")
    # 每个用例都要建几十 MB 的 FAT 镜像，不清理会把 /tmp 撑爆（曾漏出 124 GB）
    atexit.register(shutil.rmtree, tmp, True)
    img = os.path.join(tmp, "card.img")
    geo = build_image(img, False)
    seed_rom(img, geo)

    # 另一张卡：同样的 ROM，额外带一个子目录。
    # 用例 7 单独用它，免得被前面几轮已经写出的根目录 .SAV 干扰。
    img2 = os.path.join(tmp, "card_sub.img")
    geo2 = build_image(img2, False)
    seed_rom(img2, geo2)
    add_subdir(img2, geo2)

    exe = build(tmp, 0xA5, 0x5A)

    # --- 1. 首次启动：卡上没有 .SAV，芯片里的存档不能被清掉 ---
    print("\n== 1. 首次启动（无 .SAV，元数据页空）==")
    o = run(exe, "a", img)
    banks = int(o.get("BANKS", "0"))
    if banks == 4 and int(o.get("HASH_END", "0"), 16) == hash_py(0xA5):
        print(f"  OK   识别 32KB 存档（banks=4），PSRAM 原样保留（hash {o['HASH_END']}）")
    else:
        print(f"  FAIL banks={banks} hash={o.get('HASH_END')} 期望 {hash_py(0xA5):08X}")
        ok = False

    # --- 2. 游戏内存档 → 落盘 ---
    print("\n== 2. 游戏里存一次 → 下次开机落盘 ==")
    o = run(exe, "e", img)
    print(f"  BANKS={o.get('BANKS')} 落盘扇区={o.get('WRITES')} "
          f"dump前hash={o.get('HASH_BEFORE_DUMP')}")
    f = parse_file(open(img, "rb").read(), geo, NAME11)
    want = pattern(0x5A)
    scr8 = o.get("SCR8", "")
    scr9 = o.get("SCR9", "")
    if scr8 == "ZH_BACKING" and scr9 == "ZH_DONE":
        print(f"  OK   屏幕显示 {scr8} + 下一行 {scr9}（同屏不换画面；回读校验通过）")
    else:
        print(f"  FAIL 屏幕显示 行8={scr8} / 行9={scr9}")
        ok = False
    if f and f["size"] == 32768 and f["data"] == want:
        print(f"  OK   .SAV 32KB 逐字节等于 PSRAM（首簇 {f['first']}，"
              f"{len(f['chain'])} 簇）")
    else:
        got = f["data"][:32].hex() if f else "None"
        print(f"  FAIL .SAV 内容不对：size={f['size'] if f else None} 头32字节={got}")
        ok = False
    nwr = int(o.get("WRITES", "0"))
    if nwr >= 64:
        print(f"  OK   落盘写扇区 {nwr} 个（64 个存档数据 + FAT/目录项）")
    else:
        print(f"  FAIL 写扇区数 {nwr}，至少要 64")
        ok = False

    # --- 2b. 进了游戏但没存档（内容没变）→ 不该再写一次卡 ---
    print("\n== 2b. 又进一次游戏但没存档（芯片内容与上次备份的一致）==")
    snap = parse_file(open(img, "rb").read(), geo, NAME11)
    o = run(exe, "s", img)
    if int(o.get("WRITES", "1")) == 0:
        print("  OK   一个扇区都没写（指纹对上了）")
    else:
        print(f"  FAIL 写了 {o.get('WRITES')} 个扇区，内容没变时不该动卡")
        ok = False
    if not any(k in o for k in ("SCR7", "SCR8", "SCR9")):
        print("  OK   屏幕上什么都没显示（没有多余的备份闪屏）")
    else:
        print(f"  FAIL 屏幕显示了 行8={o.get('SCR8')} / 行9={o.get('SCR9')}")
        ok = False
    now = parse_file(open(img, "rb").read(), geo, NAME11)
    if snap and now and now["data"] == snap["data"]:
        print("  OK   卡上那份 .SAV 逐字节没变")
    else:
        print("  FAIL 卡上的 .SAV 被改了")
        ok = False

    # --- 3. 再启动：.SAV 装回 PSRAM ---
    print("\n== 3. 再启动（元数据页记着同一个游戏）→ .SAV 装回 PSRAM ==")
    o = run(exe, "r", img)
    if int(o.get("HASH_AFTER_PREPARE", "0"), 16) == hash_py(0x5A):
        print(f"  OK   PSRAM 与 .SAV 一致（hash {o['HASH_AFTER_PREPARE']}）")
    else:
        print(f"  FAIL PSRAM hash={o.get('HASH_AFTER_PREPARE')} 期望 {hash_py(0x5A):08X}")
        ok = False

    # --- 4. 换游戏：PSRAM 被清成 $FF，不串档 ---
    print("\n== 4. 换游戏（元数据页记着 POKEMON，这次启动 OTHER）→ 清成 $FF ==")
    o = run(exe, "o", img, "OTHER.GBC")
    h = int(o.get("HASH_AFTER_PREPARE", "0"), 16)
    expect = 5381
    for _ in range(32768):
        expect = (expect * 33 + 0xFF) & 0xFFFFFFFF
    if h == expect:
        print(f"  OK   PSRAM 全部 $FF（hash {o['HASH_AFTER_PREPARE']}）")
    else:
        print(f"  FAIL hash={o.get('HASH_AFTER_PREPARE')} 期望 {expect:08X}")
        ok = False

    # --- 5. 官方内核留下的元数据页：不能误判成"别的游戏" ---
    print("\n== 5. 元数据页是官方内核的（$010 有 /SAVER 路径，$1F0 全 $FF）==")
    o = run(exe, "u", img, "POKEMON.GBC")
    if int(o.get("HASH_AFTER_PREPARE", "0"), 16) == hash_py(0x5A):
        print(f"  OK   照旧从 .SAV 装载，PSRAM 未被误清（hash {o['HASH_AFTER_PREPARE']}）")
    else:
        print(f"  FAIL PSRAM hash={o.get('HASH_AFTER_PREPARE')} 期望 {hash_py(0x5A):08X}")
        ok = False

    # --- 6. 电池没电：章还在也不能备份，卡上那份 .SAV 必须原样不动 ---
    print("\n== 6. BATTERY DRY（哨兵 $A201 不是 $88，但 $AA 章还在）==")
    before = parse_file(open(img, "rb").read(), geo, NAME11)
    o = run(exe, "d", img, "OTHER.GBC")
    if o.get("SCR7_DRY") == "BATTERY_DRY" and o.get("SCR9_DRY") == "NOT_BACKED_UP":
        print(f"  OK   屏上显示 {o['SCR7_DRY']} / {o['SCR9_DRY']}")
    else:
        print(f"  FAIL 屏上是 {o.get('SCR7_DRY')} / {o.get('SCR9_DRY')}，"
              f"期望 BATTERY_DRY / NOT_BACKED_UP")
        ok = False
    if int(o.get("WRITES_BOOT", "1")) == 0:
        print("  OK   一个扇区都没写（卡没被动过）")
    else:
        print(f"  FAIL 写了 {o.get('WRITES_BOOT')} 个扇区，电池没电时不能写卡")
        ok = False
    after = parse_file(open(img, "rb").read(), geo, NAME11)
    if before and after and after["data"] == before["data"] and after["size"] == 32768:
        print("  OK   卡上原有的 .SAV 逐字节不变（没被衰减数据覆盖）")
    else:
        print("  FAIL 卡上原有的 .SAV 被改了")
        ok = False
    if o.get("CANARY_AFTER") == "88":
        print("  OK   哨兵已重新盖成 $88（下次开机才有得比）")
    else:
        print(f"  FAIL 哨兵是 {o.get('CANARY_AFTER')}，期望 88")
        ok = False
    if int(o.get("HASH_AFTER_PREPARE", "0"), 16) == expect:
        print("  OK   启动另一个游戏时 PSRAM 清成 $FF（不把衰减数据喂给游戏）")
    else:
        print(f"  FAIL PSRAM hash={o.get('HASH_AFTER_PREPARE')} 期望 {expect:08X}")
        ok = False
    if o.get("SCR8") == "ZH_BACKING" and o.get("SCR9") == "ZH_DONE":
        print(f"  OK   恢复供电后的下一次开机照常备份"
              f"（{o.get('SCR8')} / {o.get('SCR9')}）")
    else:
        print(f"  FAIL 第二次备份显示 行8={o.get('SCR8')} / 行9={o.get('SCR9')}")
        ok = False
    f = parse_file(open(img, "rb").read(), geo, sav_ident_py("OTHER.GBC"))
    if f and f["size"] == 32768 and f["data"] == pattern(0x5A):
        print("  OK   OTHER.SAV 32KB，内容等于这一局的 PSRAM")
    else:
        print(f"  FAIL OTHER.SAV：size={f['size'] if f else None}")
        ok = False

    # --- 7. 用户把电脑上的备份拷到卡上 → 开机不能把它盖回去 ---
    print("\n== 7. 用户在电脑上换了卡上的 .SAV，然后开机 ==")
    o = run(exe, "w", img)
    if int(o.get("WRITES", "1")) == 0:
        print("  OK   一个扇区都没写（芯片没变，就不该动卡）")
    else:
        print(f"  FAIL 写了 {o.get('WRITES')} 个扇区，把用户换的文件盖掉了")
        ok = False
    if not any(k in o for k in ("SCR7", "SCR8", "SCR9")):
        print("  OK   屏幕上什么都没显示")
    else:
        print(f"  FAIL 屏幕显示了 行8={o.get('SCR8')} / 行9={o.get('SCR9')}")
        ok = False
    f = parse_file(open(img, "rb").read(), geo, NAME11)
    want_mark = int(o.get("CARD_BYTE", "-1"), 16) if o.get("CARD_BYTE") else -1
    if f and f["size"] == 32768 and want_mark >= 0 and f["data"][0] == want_mark:
        print(f"  OK   用户换进去的字节还在（首字节 {f['data'][0]:02X}，没被盖回去）")
    else:
        got = f["data"][0] if f else None
        print(f"  FAIL 首字节成了 {got}，期望 {want_mark:02X}")
        ok = False

    # --- 7c. sav_ident 纯函数不变量 ---
    print("\n== 7c. sav_ident 是纯函数（不改写输入、同输入同输出）==")
    o = run(exe, "K", img)
    if o.get("K_PURE") == "1":
        print("  OK   调用后 lfn 缓冲逐字节未变（没有就地改写）")
    else:
        print("  FAIL sav_ident 改写了传入的长名缓冲")
        ok = False
    if o.get("K_IDEM") == "1":
        print(f"  OK   连调两次结果相同：{o.get('K_ID')}")
    else:
        print("  FAIL 连调两次结果不同")
        ok = False

    # --- 7b. 中文长名 ROM：启动 → 存档 → 备份（v0.16.0 真机翻车的形状） ---
    # 真机现象：「K 口袋怪兽 水晶版」备份失败 + 校验失败，而 1.gbc 正常。
    # 差别只有一个：1.gbc 是纯 8.3 名，目录里没有 LFN 项，fat_full_lfn() 返回 0，
    # sav_ident() 走「8.3 名派生」分支，同一份输入算几次都一样；中文 ROM 有 LFN，
    # sav_ident() 读的是 g_lfn_buf，而 fat_open_sav() 会把这块**共用 scratch**
    # 就地改成 "….sav" —— 再算一次就是另一个名字了。
    # 旧测试的假 ROM 全叫 POKEMON.GBC（无 LFN），所以这个形状一次都没被覆盖。
    print("\n== 7b. 中文长名 ROM（卡上有 LFN）→ 备份必须按长名哈希落盘 ==")
    for case, (short83, cn) in enumerate((
            (b"KCHN    GBC", "K 口袋怪兽 水晶版.gbc"),
            (b"PKM2    GBC", "口袋妖怪 加强版2.0 金手指.gbc"),
            (b"PKMC    GBC", "口袋妖怪 皮卡丘版.gbc"),
            (b"K       GBC", "K 口袋妖怪 水晶 金手指 中文版.gbc"))):
        tag = cn[:10]
        img3 = os.path.join(tmp, "card_lfn_%d.img" % case)
        geo3 = build_image(img3, False)
        seed_rom_lfn(img3, geo3, short83, cn)
        gname = short83[:8].decode().rstrip() + ".GBC"
        o = run(exe, "Z", img3, gname)

        if o.get("SCR9") == "ZH_DONE":
            print(f"  OK   [{tag}] 备份结果屏是「完成」")
        else:
            print(f"  FAIL [{tag}] 备份结果屏是 {o.get('SCR9')}（长名 ROM 备份失败）")
            ok = False

        data3 = open(img3, "rb").read()
        want_id = sav_ident_py(gname, [ord(c) for c in cn])
        f = parse_file(data3, geo3, want_id)
        if f and f["size"] == 32768 and f["data"] == pattern(0x5A):
            print(f"  OK   [{tag}] 按长名哈希 {want_id[:8].decode()} 找到 32KB .SAV，"
                  f"内容逐字节一致")
        else:
            print(f"  FAIL [{tag}] 按长名哈希 {want_id[:8].decode()} 找不到正确的 .SAV"
                  f"（size={f['size'] if f else None}）")
            ok = False

        # 反证：把长名当成已被改成 ".sav" 的版本再哈希一次，卡上不该存在这个文件。
        # 这正是旧代码 save_verify() 实际做的事 —— 两个名字必须是不同的。
        wrong_id = sav_ident_py(gname, [ord(c) for c in cn.rsplit(".", 1)[0] + ".sav"])
        if wrong_id != want_id and parse_file(data3, geo3, wrong_id) is None:
            print(f"  OK   [{tag}] 卡上没有「长名被改成 .sav 后再哈希」的第二个名字"
                  f"（{want_id[:8].decode()} ≠ {wrong_id[:8].decode()}）")
        else:
            print(f"  FAIL [{tag}] 出现第二个名字或两个哈希相同："
                  f"{want_id[:8].decode()} / {wrong_id[:8].decode()}")
            ok = False

    # --- 8. 子目录里的游戏：SAVER 全卡只有一个，在根目录 ---
    # v0.11.0 按真机反馈改的：v0.10.0 曾在 ROM 所在目录各建一个 SAVER，
    # 用户看到根目录已经有一个、子目录里又冒出第二个，反而乱。
    print("\n== 8. 子目录里的游戏（SAVER 只在根目录，存档不落子目录）==")
    o = run(exe, "i", img2)
    if int(o.get("ROOT_READ", "0")) == 32768:
        print("  OK   从根目录读到了 32KB 的 POKEMON.sav")
    else:
        print(f"  FAIL 根目录读不到存档：ROOT_READ={o.get('ROOT_READ')}")
        ok = False

    data = open(img2, "rb").read()

    def dir_names(lba):
        """读一个目录扇区里的短名（跳过已删除项与 VFAT 长名项）。"""
        out = []
        sec = data[lba * SEC:(lba + 1) * SEC]
        for i in range(0, SEC, 32):
            e = sec[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5 or (e[11] & 0x3F) == 0x0F:
                continue
            out.append((bytes(e[:11]), e[11]))
        return out

    root = dir_names(geo2["root_lba"])
    sub = dir_names(geo2["data_lba"] + (SUBDIR_CLUSTER - 2) * geo2["spc"])
    if any(n == b"SAVER      " and (a & 0x10) for n, a in root):
        print("  OK   根目录建出了 SAVER 子目录")
    else:
        print("  FAIL 根目录没有 SAVER 子目录")
        ok = False
    if not any(n in (b"SAVER      ", NAME11) for n, a in sub):
        print("  OK   子目录里既没有第二个 SAVER，也没有散落的 .SAV")
    else:
        print(f"  FAIL 子目录里多出了东西：{sub}")
        ok = False

    if o.get("SCR8") == "ZH_BACKING" and o.get("SCR9") == "ZH_DONE":
        print(f"  OK   备份过程正常（{o.get('SCR8')} / {o.get('SCR9')}）")
    else:
        print(f"  FAIL 备份屏显示 行8={o.get('SCR8')} / 行9={o.get('SCR9')}")
        ok = False

    # --- 9. 运行上次游戏：按目录簇 + 完整 8.3 名直接定位 ---
    # v0.10.0 拿「没有扩展名的 8 字符基本名」去跟列表里的完整名 strcmp，
    # 永远匹配不上，于是只把玩家送到上次那个目录、什么都没启动。
    print("\n== 9. 运行上次游戏（fat_locate 按目录簇 + 全名定位）==")
    o = run(exe, "L", img2)
    if (o.get("LOC_ROOT") == "1" and o.get("LOC_ROOT_NAME") == "POKEMON.GBC"
            and int(o.get("LOC_ROOT_SIZE", "0")) == 2048):
        print("  OK   根目录里按完整 8.3 名定位到 ROM（名字、大小都对）")
    else:
        print(f"  FAIL 根目录定位：{o}")
        ok = False
    if o.get("LOC_SUB") == "1" and o.get("LOC_SUB_NAME") == "SUBROM.GBC":
        print("  OK   子目录里按目录簇 + 完整名也能定位到 ROM")
    else:
        print(f"  FAIL 子目录定位：LOC_SUB={o.get('LOC_SUB')} / {o.get('LOC_SUB_NAME')}")
        ok = False
    if o.get("CWD_UNCHANGED") == "1":
        print("  OK   定位完 fat_cwd 原样还原（列表不会被切到别的目录）")
    else:
        print(f"  FAIL 工作目录被改了：CWD_UNCHANGED={o.get('CWD_UNCHANGED')}")
        ok = False
    if o.get("LOC_MISS") == "0":
        print("  OK   找不到时安静返回 0（文件被删 / 换了卡不会误启动）")
    else:
        print(f"  FAIL 不存在的 ROM 也返回了成功：{o.get('LOC_MISS')}")
        ok = False

    # --- 10. 首次按 START 的元数据校验 ---
    # v0.10.0 无条件把擦除态元数据页抄进菜单，第一次按 START 就是一行乱码。
    print("\n== 10. 首次按 START（元数据页擦除态不能画乱码）==")
    o = run(exe, "M", img)
    if o.get("DIRTY_FULL_EMPTY") == "1" and o.get("DIRTY_LFN_EMPTY") == "1":
        print("  OK   擦除态：完整名与中文长名都被清空（菜单显示「无上次游戏」）")
    else:
        print(f"  FAIL 擦除态没清干净：{o}")
        ok = False
    if o.get("GOOD_FULL") == "POKEMON.GBC":
        print("  OK   合法记录：完整 8.3 名能读回来，菜单可直接定位启动")
    else:
        print(f"  FAIL 合法记录读不回来：GOOD_FULL={o.get('GOOD_FULL')}")
        ok = False
    if o.get("BADXOR_FULL_EMPTY") == "1":
        print("  OK   记录里改一个字节（校验不符）→ 整条作废，不会拿垃圾名去启动")
    else:
        print(f"  FAIL 坏记录没被挡住：{o}")
        ok = False
    if o.get("SECOND_FULL") == "POKEMON.GBC" and o.get("SECOND_DIR") == "3":
        print("  OK   第一份坏、第二份好 → 仍然读得出来（冗余生效，目录簇也不丢）")
    else:
        print(f"  FAIL 冗余没兜住：{o}")
        ok = False
    # 真机那张卡的短名是 GBK 字节，还带半个汉字 —— 记录必须原样读回来
    if o.get("GBK_OK") == "1" and o.get("GBK_LEN") == "12":
        print("  OK   8.3 名含 GBK 高位字节（'K'+口+袋+妖+半个怪）→ 原样读回，不再判无效")
    else:
        print(f"  FAIL 高位字节的短名被拒：GBK_OK={o.get('GBK_OK')} GBK_LEN={o.get('GBK_LEN')}")
        ok = False

    # --- 11. 没有存档 RAM 的卡带也要记「上次运行」---
    # v0.12.2 把 rec_put 写在 save_prepare 的 if (banks) 里，于是 $149 = 0 的
    # ROM 永远记不上：启动、关机、再开机，START 菜单还是「无上次游戏」。
    print("\n== 11. 无存档 RAM 的卡带（$149 = 0）也要记「上次运行」==")
    img3 = os.path.join(tmp, "card_noram.img")
    geo3 = build_image(img3, False)
    seed_rom(img3, geo3, ram=0)
    o = run(exe, "z", img3)
    if o.get("Z_BANKS") == "0":
        print("  OK   识别成「没有存档 RAM」（banks=0），不会去备份")
    else:
        print(f"  FAIL 应该识别成 banks=0：{o.get('Z_BANKS')}")
        ok = False
    if o.get("Z_FULL") == "[POKEMON.GBC]":
        print("  OK   记录照样写进去，重启后「上次运行」还找得回来")
    else:
        print(f"  FAIL 无 RAM 的 ROM 记不上：Z_FULL={o.get('Z_FULL')}")
        ok = False

    # --- 12. SAVER 里已有 12 个**带长名**的存档时，30 字符长名还写得进去吗 ---
    print("\n== 12. SAVER 已有 12 个带长名的存档（占 36 槽）+ 30 字符长名 ==")
    img3 = os.path.join(tmp, "card_big.img")
    geo3 = build_image(img3, False)
    seed_rom(img3, geo3)
    ok = q_case(exe, img3, "单簇 SAVER", geo3, "q", 12) and ok

    # --- 13. $00 落在扇区尾部：3 条长名项必须跨到下一扇区 ---
    # 12 个短名存档占 14 槽，$00 正好在槽 14（扇区尾还剩 2 槽），而一个 30 字符
    # 名字要 4 槽。v0.12.6 的处理是把尾部 2 个 $00 改写成 $E5 再重扫，但重扫时
    # 又把这段 $E5 当成"最长空槽起点"、只认 $00 开头 → 报「目录已满」，从那个
    # 存档起 SAVER 永久写不进东西。这里就压这条路径。
    print("\n== 13. $00 落在扇区尾部（3 条长名项要跨扇区）==")
    img4 = os.path.join(tmp, "card_tail.img")
    geo4 = build_image(img4, False)
    seed_rom(img4, geo4)
    ok = q_case(exe, img4, "跨扇区", geo4, "R", 12) and ok

    # --- 14. SAVER 被填满（连 $00 都不剩）→ 必须扩一簇写进去 ---
    # 这条用例真正压的是 dir_nth 的链尾判据：FAT16 的链尾是 $FFF8 起，早先一律
    # 按 FAT32 的 $0FFFFFF8 比，$FFFF 这种正常链尾被当成"还有下一簇"，于是
    # cluster_sector() 算出数据区之外的野扇区（实测 LBA 65792，簇 65535），
    # 目录项写到那儿去了 —— 卡上干净的，屏幕报「完成」。
    print("\n== 14. SAVER 填满（16 槽全占，连 $00 都不剩）→ 自动扩簇 ==")
    img5 = os.path.join(tmp, "card_full.img")
    geo5 = build_image(img5, False, spc=1, total_sec=TOTAL_SEC // 4)
    seed_rom(img5, geo5)
    ok = full_case(exe, img5, geo5) and ok

    print("\n" + ("全部通过" if ok else "有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
