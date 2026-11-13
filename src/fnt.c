/* fnt.c — 卡上的汉字字库（EZJR.FNT），按需读字模
 *
 * 为什么字库在卡上而不在 ROM 里：中文文件名存在 VFAT 长文件名项里，要把它们
 * 画出来就得有对应汉字的点阵。GB2312 那 3755 个常用字，8x8 也要 30 KB，
 * 而 ROM 只剩十几 KB（整个才 32 KB）。所以把字模放到 SD 卡上，用到哪个读哪个。
 *
 * 文件格式（见 tools/gen_cjk_fnt.py）：
 *   裸数组，没有文件头。第 i 个 8 字节 = U+4E00+i 的字形，8 行 1bpp，
 *   bit7 = 最左像素。offset(cp) = (cp - 0x4E00) * 8。
 *   大小必须正好 20992*8 = 167936，对不上就不用它（防止拷错文件）。
 *
 * 读一个字的代价：一次 512 字节的扇区读。字模 8 字节，同一扇区里有 64 个字，
 * 所以连续取一串汉字（一行名字）通常只读一两次。
 *
 * ★ 扇区号怎么算：文件在 FAT 上是簇链，不能直接按偏移算 LBA。开机时把簇链
 *   展开成一张表（最多 64 簇），之后的每次读就是两次除法 + 一次表查。
 */
/* 模拟器（EZJR_TEST_STUB）里没有 SD 卡，也没有 EZJR.FNT 文件，真实实现
 * 整个不编；test_stub.c 提供一个内置小字库，只为把"码点 -> 字模 -> tile"
 * 这条渲染路径跑通。*/
#ifndef EZJR_TEST_STUB

#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"
#include "fnt.h"
#include "video.h"

#define FNT_FIRST   0x4E00
#define FNT_LAST    0x9FFF
#define FNT_SIZE    ((uint32_t)(FNT_LAST - FNT_FIRST + 1) * 8)   /* 167936 */
#define FNT_MAX_CL 64                    /* 64 簇 × 4KB = 256KB，够放 164KB */

/* "EZJR    FNT" —— 8.3 短名，空格补齐、全大写 */
static const char fnt_name11[11] = {'E','Z','J','R',' ',' ',' ',' ','F','N','T'};

static uint16_t fnt_chain[FNT_MAX_CL];   /* 每项一个簇号 */
static uint8_t  fnt_ncl;                 /* 有效簇数 */
static uint8_t  fnt_spc;                 /* 每簇扇区数（挂载时就定了） */
static uint8_t  fnt_is32;
static uint8_t  fnt_ok;

/* 字模扇区缓存：game_work 里留着上一次读的 512 字节。一屏中文名十几个字，
 * 码点相邻的字常常落在同一个扇区，命中就不必再读一次 SD。
 * game_work 是全局共用缓冲（存档路径也用它），所以每次整屏重绘前由
 * cjk_begin() 调 fnt_flush_cache() 让它失效。*/
static uint16_t fnt_sec;
static uint8_t  fnt_sec_ok;

void fnt_flush_cache(void)
{
    fnt_sec_ok = 0;
}

/* 512 字节缓冲和 game.c 共用：菜单态和启动互斥（启动不返回）。*/
extern uint8_t game_work[512];

uint8_t fnt_open(void)
{
    uint32_t c, size, cur_fs = 0xFFFFFFFFUL;
    uint8_t  n = 0;
    FatInfo  fi;

    fnt_ok = 0;
    fnt_ncl = 0;
    fnt_flush_cache();
    if (!fat_find(fnt_name11, &c, &size))
        return 0;
    if (size != FNT_SIZE)                /* 拷错了 / 拷了个旧的，宁可不显示 */
        return 0;
    if (c < 2)
        return 0;

    fat_info(&fi);
    fnt_spc = fi.sectors_per_cluster;
    fnt_is32 = fi.is_fat32;
    if (!fnt_spc)
        return 0;

    /* 展开簇链时，一次读一整个 FAT 扇区到 game_work（开机时它闲置）。
     * 原来每簇调一次 fat_next_cluster，41 个簇就是 41 次 SD 读；FAT 表是
     * 连续的，实际只需 1-2 次。这一项就砍掉了开机白屏的一大半。*/
    /* 链尾门限按 FAT 类型分开：FAT16 是 $FFF8-$FFFF（见 fat.c dir_chain 的说明）*/
    while (c >= 2 && c < (fnt_is32 ? 0x0FFFFFF8UL : 0xFFF8UL) && n < FNT_MAX_CL) {
        uint32_t fs;
        uint16_t off;

        fnt_chain[n++] = (uint16_t)c;
        off = fat_fat_offset(c, &fs);
        if (fs != cur_fs) {
            sd_read(fs, game_work);
            cur_fs = fs;
        }
        if (fnt_is32)
            c = (uint32_t)game_work[off] |
                ((uint32_t)game_work[off + 1] << 8) |
                ((uint32_t)game_work[off + 2] << 16) |
                ((uint32_t)(game_work[off + 3] & 0x0F) << 24);
        else
            c = (uint32_t)game_work[off] |
                ((uint32_t)game_work[off + 1] << 8);
    }
    /* 簇表放不下整个文件就别用：读到后面会算出错误扇区，屏幕上出现乱码比
     * 干脆不显示更糟。64 簇 × 4KB = 256KB，正常卡的簇不会更小。*/
    if ((uint32_t)n * fnt_spc * 512 < FNT_SIZE)
        return 0;

    fnt_ncl = n;
    fnt_ok = 1;
    return 1;
}

uint8_t fnt_available(void)
{
    return fnt_ok;
}

/* 读一个字模到 out（8 字节）。返回 1 = 读到了。*/
uint8_t fnt_glyph(uint16_t cp, uint8_t *out)
{
    uint32_t off, sec, lba;
    uint16_t idx, in;

    if (!fnt_ok)
        return 0;
    if (cp < FNT_FIRST || cp > FNT_LAST)
        return 0;

    off = (uint32_t)(cp - FNT_FIRST) * 8;
    sec = off >> 9;                      /* 文件内第几个扇区 */
    idx = (uint16_t)(sec / fnt_spc);     /* 第几个簇 */
    in  = (uint16_t)(sec % fnt_spc);     /* 簇内第几个扇区 */
    if (idx >= fnt_ncl)
        return 0;

    if (!fnt_sec_ok || fnt_sec != (uint16_t)sec) {
        lba = fat_cluster_lba(fnt_chain[idx]) + in;
        sd_read(lba, game_work);
        fnt_sec = (uint16_t)sec;
        fnt_sec_ok = 1;
    }
    memcpy(out, game_work + (off & 511), 8);
    return 1;
}

#endif /* EZJR_TEST_STUB */
