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
/* fat.c 的簇号->扇区换算。fat.c 里已去掉 static；fatw.c 另有一份同名 static，
 * 互不影响，所以这里不要放进 fat.h（会与 fatw.c 的 static 定义冲突）。*/
extern uint32_t cluster_sector(uint32_t c);
extern uint32_t froot_lba;
extern uint16_t froot_sectors;

/* fat.c 里这两个是 static；dir_chain 搬过来后需要自己的一份（纯字节拼接）。*/
static uint16_t ld16(const uint8_t *p) { return p[0] | (uint16_t)p[1] << 8; }
static uint32_t ld32(const uint8_t *p) { return ld16(p) | (uint32_t)ld16(p + 2) << 16; }

/* v0.16.13：fat_mkdir() 搬去 fatw.c（bank2）了 —— 从 bank2 调 bank0 的
 * 目录创建要过桩，而调用方传的 "SAVER\\0\\0..." 是 bank2 的 _CODE 常量，
 * 切 bank 之后指针指向 bank0 的字节，写进目录项的名字是垃圾。两个函数现在
 * 都只在 fatw.c 内部用，不再需要 far 桩。put_entry() 也跟着搬走了。*/

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

/* ---- 目录自愈：修历史版本写进中间的错误 $00 ----
 *
 * v0.12.x 的 dir_put 在**每一次新建**后都无条件补一个 $00 终止符。若新项落在
 * 目录中间的 $E5 空洞里，这个 $00 会把它后面的有效项全部截断：Windows 从
 * $00 处停住，dir_seek 也在那里 return 2。表现是「卡上有档，却查不到，再建
 * 一条」，同名 .sav 越攒越多。
 *
 * 修法：找出整个目录里**最后一个有效项**的位置，把它之前出现的所有 $00 改成
 * $E5（已删除标记，Windows 会跳过继续读）。最后一个有效项之后的 $00 是真正的
 * 链尾，保持不动。
 *
 * 只改 $00 → $E5，不删不改任何有效项，也不碰簇链 —— 中途断电最坏只是这次
 * 没修完，下次开机再修，不会丢档。*/
uint8_t fat_dir_heal(uint32_t dir)
{
    uint32_t chain[16], saved = fat_cwd;
    uint8_t  secs, i, nfix = 0, last = 0xFF;
    uint16_t k, last_off = 0, lim;

    if (!fmount_ok) return 0;
    fat_cwd = dir;
    secs = dir_chain(dir, chain, 16);

    /* 第一遍：记住整个目录最后一个有效项在哪个扇区、哪个偏移 */
    for (i = 0; i < secs; i++) {
        sd_read(chain[i], fbuf);
        for (k = 0; k < 512; k += 32) {
            if (fbuf[k] != 0x00 && fbuf[k] != 0xE5) {
                last = i;
                last_off = k;
            }
        }
    }
    if (last == 0xFF) { fat_cwd = saved; return 0; }

    /* 第二遍：它之前的 $00 都是错的。同一扇区只扫到 last_off，
       后面的真终止符不碰；更靠前的扇区整扇都算「之前」。*/
    for (i = 0; i <= last && i < secs; i++) {
        uint8_t dirty = 0;
        lim = (i == last) ? last_off : 512;
        sd_read(chain[i], fbuf);
        for (k = 0; k < lim; k += 32) {
            if (fbuf[k] == 0x00) {
                fbuf[k] = 0xE5;
                dirty = 1;
                nfix++;
            }
        }
        if (dirty) sd_write(chain[i], fbuf);
    }
    fat_cwd = saved;
    return nfix;
}

/* ---- 簇链遍历（原在 fat.c 的 _BANK1 里，v0.15.0 移到 _CODE 腾出空间）----
 * 它被 fat.c(BANK1) 和 fatn.c/fatx.c(_CODE) 多处调用，放 bank0 后 bank1 省约
 * 150 字节。bank0 调 bank1 的 cluster_sector 是安全的：bank1 常驻 $4000。*/
/* 展开簇链时的 FAT 扇区缓存。借用 fbuf 而不是自己再开 512 字节：
 * _DATA 只剩不到 200 字节，再来一个静态 512 缓冲就顶到 $D000 handoff 区了。
 * dir_chain() 运行期间 fbuf 只装 FAT 扇区，返回后调用者才开始读目录扇区，
 * 两者不重叠，所以可以共用。*/
static uint8_t  fchain_ok;
static uint32_t fchain_lba;

static uint32_t fat_next_fbuf(uint32_t c)
{
    uint32_t fs;
    uint16_t off = fat_fat_offset(c, &fs);

    if (!fchain_ok || fchain_lba != fs) {
        sd_read(fs, fbuf);
        fchain_lba = fs;
        fchain_ok  = 1;
    }
    if (fis_fat32)
        return ld32(fbuf + off) & 0x0FFFFFFFUL;
    return ld16(fbuf + off);
}

/* 填目录扇区表。dir = 0 是根目录，否则是子目录首簇。
 * 返回扇区个数（最多 cap）。FAT16 的根目录是紧跟 FAT 的一片连续扇区；
 * 子目录和 FAT32 根目录都走簇链。
 *
 * cap 传 32：FAT16 根目录正好 32 扇区（512 个条目）；子目录在 4KB 簇时
 * 32 扇区 = 4 簇 = 16KB 目录，也够 512 项。列表上限 64 条早就截断了。*/
uint8_t dir_chain(uint32_t dir, uint32_t *chain, uint8_t cap)
{
    uint8_t  n = 0;
    uint16_t i;
    uint32_t c;

    fchain_ok = 0;

    if (dir == 0 && !fis_fat32) {
        for (i = 0; i < froot_sectors && n < cap; i++)
            chain[n++] = (uint32_t)(froot_lba + i);
        return n;
    }

    c = (dir == 0) ? froot_cluster : dir;
    /* ★ 链尾判据要按 FAT 类型分开写：FAT16 的 EOF 标记是 $FFF8-$FFFF，
     * 而 $FFF8-$FFFF 全都小于 $0FFFFFF8 —— 只用 FAT32 那个门限，FAT16 上
     * 链尾会被当成合法簇号继续走，把 cluster_sector(65535) 这种天文数字
     * 当成 LBA 去读（真机上读回垃圾、主机测试里直接越界）。
     * 以前只有根目录走 dir_chain（FAT16 走上面那个连续扇区分支），所以没露馅；
     * v0.12.0 的 fat_full_lfn 第一次让子目录也走簇链，才踩到。*/
    while (c >= 2 && c < (fis_fat32 ? 0x0FFFFFF8UL : 0xFFF8UL) && n < cap) {
        uint32_t nxt = fat_next_fbuf(c);
        for (i = 0; i < fcsize && n < cap; i++)
            chain[n++] = cluster_sector(c) + i;
        c = nxt;
    }
    fchain_ok = 0;                       /* fbuf 马上要被目录扇区覆盖 */
    return n;
}
