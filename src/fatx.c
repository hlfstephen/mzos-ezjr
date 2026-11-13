/* fatx.c — 目录创建（SAVER）、文件删除、按目录簇定位 ROM
 *
 * 为什么单独一个文件，不并进 fatw.c：fatw.c 编在 BANK1（$4000-$7FFF），
 * v0.9.0 之后那一段只剩几百字节；这里两个操作加起来要一千上下，塞不进去。
 * 本文件编在 _CODE（默认段，$0400-$3FFF），那边还有三四个 KB 余量。
 *
 * 依赖 fatw.c 导出的四个底层件（dir_seek / fat_alloc / fat_put /
 * fat_free_chain）—— 两个段之间直接 call 就行，32KB 的 ROM 不需要切 bank。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"

/* fat.c 的布局量 */
extern uint8_t  fbuf[512];
extern uint8_t  fcsize;
extern uint8_t  fis_fat32;
extern uint32_t froot_cluster;
extern uint8_t  fmount_ok;

/* fatw.c 的写路径底层件（那边用宏换成了带 fatw_ 前缀的名字）*/
extern uint8_t  fatw_dir_seek(const uint8_t *raw11, uint32_t *lba, uint16_t *off);
extern uint8_t  fatw_slot(uint8_t nent, uint32_t *lba, uint16_t *off);
extern uint32_t fatw_alloc(void);
extern void     fatw_put(uint32_t c, uint32_t v);
extern void     fatw_free_chain(uint32_t c);
extern uint8_t  fatw_lfn_sum(const uint8_t *raw11);

/* 写一条目录项：名字、属性、首簇。大小恒 0（目录项就该是 0），
 * 时间日期也留 0 —— 卡上有 1980 年建的文件不奇怪，写一串假日期反而更怪。*/
static void put_entry(uint8_t *e, const uint8_t *raw11, uint8_t attr,
                      uint32_t cluster)
{
    uint8_t i;

    memset(e, 0, 32);
    for (i = 0; i < 11; i++) e[i] = raw11[i];
    e[11] = attr;
    e[20] = (uint8_t)(cluster >> 16);
    e[21] = (uint8_t)(cluster >> 24);
    e[26] = (uint8_t)cluster;
    e[27] = (uint8_t)(cluster >> 8);
}

/* 在**当前目录**（fat_cwd）下建一个子目录。已存在就返回它原来的簇号。
 * 返回 0 = 失败（目录满 / 没有空簇），调用方自己决定要不要退而求其次。*/
uint32_t fat_mkdir(const uint8_t *raw11)
{
    static const uint8_t dot[11]    = {'.',' ',' ',' ',' ',' ',' ',' ',' ',' ',' '};
    static const uint8_t dotdot[11] = {'.','.',' ',' ',' ',' ',' ',' ',' ',' ',' '};
    uint32_t lba = 0, c, parent;
    uint16_t off = 0;
    uint8_t  s;

    if (!fmount_ok)
        return 0;

    c = fat_find_dir(raw11);            /* 已经有了就直接用，别重复建 */
    if (c)
        return c;

    if (!fatw_slot(1, &lba, &off))      /* 目录满，且扩不出新簇 */
        return 0;

    c = fatw_alloc();
    if (c == 0)
        return 0;
    fatw_put(c, fis_fat32 ? 0x0FFFFFFFUL : 0xFFFFUL);

    /* 新簇必须先清零：里面残留的旧数据会被当成目录项读出来，
     * 玩家就会在 SAVER 里看见一堆乱七八糟的文件名。*/
    memset(fbuf, 0, 512);
    for (s = 0; s < fcsize; s++)
        sd_write(fat_cluster_lba(c) + s, fbuf);

    /* "." 和 ".."。本内核自己不读它们（列表里的「..」是扫描时插的虚拟项，
     * 不依赖这两个目录项），但电脑上的文件管理器会看，缺了会报目录损坏。*/
    parent = fat_cwd ? fat_cwd : (fis_fat32 ? froot_cluster : 0);
    sd_read(fat_cluster_lba(c), fbuf);
    put_entry(fbuf, dot, 0x10, c);
    put_entry(fbuf + 32, dotdot, 0x10, parent);
    sd_write(fat_cluster_lba(c), fbuf);

    /* 父目录里落下这一项 */
    sd_read(lba, fbuf);
    put_entry(fbuf + off, raw11, 0x10, c);
    sd_write(lba, fbuf);
    return c;
}

/* "POKEMON.GBC" -> 8.3 原始 11 字节 "POKEMON GBC"。
 * 名字里的非 ASCII 字节（中文名生成的 ~1 短名）原样抄过去，与目录项一致。*/
static void rom11(const char *rom, uint8_t *raw)
{
    uint8_t i, n;

    for (i = 0; i < 11; i++) raw[i] = ' ';
    for (i = 0; i < 8 && rom[i] && rom[i] != '.'; i++)
        raw[i] = (uint8_t)rom[i];
    while (rom[i] && rom[i] != '.') i++;     /* 名字后面剩下的部分跳过 */
    if (rom[i] == '.') {
        i++;
        for (n = 8; n < 11 && rom[i]; n++, i++)
            raw[n] = (uint8_t)rom[i];
    }
}

/* 在指定目录里按 8.3 全名找一个 ROM，填一条 GameEntry。
 *
 * START 菜单的「运行上次游戏」用：直接拿元数据页里的目录簇 + 完整名定位，
 * 不改 fat_cwd、不重扫列表 —— v0.10.0 的做法是「切到上次目录 → 重扫 → 按
 * 名字找」，而存下来的名字是没有扩展名的 8 字符基本名，跟列表里的完整名
 * 永远 strcmp 不上，结果就是玩家被送到那个目录、什么都没启动。
 * 现在连列表都不碰，启动失败回到列表时还停在原来那一项上。*/
uint8_t fat_locate(uint32_t dir, const char *name83, GameEntry *out)
{
    uint8_t  raw[11], r, i;
    uint32_t lba = 0, saved, first, size;
    uint16_t off = 0;

    if (!fmount_ok || !name83[0])
        return 0;
    rom11(name83, raw);
    saved = fat_cwd;
    fat_cwd = dir;
    r = fatw_dir_seek(raw, &lba, &off);
    if (r == 1)
        sd_read(lba, fbuf);
    fat_cwd = saved;                    /* 出口必须还原：列表还在原来的目录 */
    if (r != 1)
        return 0;

    /* fat.c 的 ld16/ld32 是 static，这里直接拼字节 */
    first = (uint32_t)fbuf[off + 26] | ((uint32_t)fbuf[off + 27] << 8) |
            ((uint32_t)fbuf[off + 20] << 16) | ((uint32_t)fbuf[off + 21] << 24);
    size  = (uint32_t)fbuf[off + 28] | ((uint32_t)fbuf[off + 29] << 8) |
            ((uint32_t)fbuf[off + 30] << 16) | ((uint32_t)fbuf[off + 31] << 24);
    if (first < 2 || !size)
        return 0;

    for (i = 0; i < GAME_NAME_MAX - 1 && name83[i]; i++)
        out->name[i] = name83[i];
    out->name[i] = 0;
    out->cluster = first;
    out->size    = size;
    return 1;
}

/* 删掉当前目录里的一个 ROM。目录项标 $E5 + 释放簇链。
 *
 * 为什么连 LFN 项一起删：只标短名项会把它前面那几条 0x0F 项留成「孤儿」，
 * 而 VFAT 的规矩是「LFN 项粘在紧跟着的那条短名项上」—— 孤儿会去粘下一个
 * 文件，把那个文件的名字改成刚删掉的名字。删了 A 却看见 B 换了名，
 * 比删不掉更让人不敢用。*/
uint8_t fat_delete(const char *rom)
{
    uint8_t  raw[11], r, sum, k;
    uint32_t lba = 0, first;
    uint16_t off = 0, p;

    if (!fmount_ok)
        return FAT_ERR_NO_FS;
    rom11(rom, raw);

    r = fatw_dir_seek(raw, &lba, &off);
    if (r != 1)
        return FAT_ERR_NO_FS;           /* 已经不在了 */

    sd_read(lba, fbuf);
    first = (uint32_t)fbuf[off + 26] | ((uint32_t)fbuf[off + 27] << 8) |
            ((uint32_t)fbuf[off + 20] << 16) | ((uint32_t)fbuf[off + 21] << 24);
    fbuf[off] = 0xE5;                   /* FAT 的「已删除」标记，其余字节保留 */

    sum = fatw_lfn_sum(raw);            /* 长名项靠这个校验和认领短名项 */
    p = off;
    for (k = 0; k < 20 && p >= 32; k++) {
        p = (uint16_t)(p - 32);
        if ((fbuf[p + 11] & 0x3F) != 0x0F) break;   /* 不是长名项 → 到头了 */
        if (fbuf[p + 13] != sum) break;             /* 是别人的长名项 */
        fbuf[p] = 0xE5;
    }
    sd_write(lba, fbuf);

    if (first >= 2)
        fatw_free_chain(first);         /* 簇不回收，卡用几次就满了 */
    return FAT_OK;
}
