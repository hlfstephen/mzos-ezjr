/* fatw.c — FAT 的写路径与簇链：加载命令装配、存档 .SAV 的读写
 *
 * 为什么单独一个文件：`--codeseg BANK1` 让 build.sh 把本文件编到
 * $4000-$7FFF，也就是 32KB ROM 的后半段。加上存档功能后 `_CODE` 会冲出
 * $4000 压到这一段上，所以把「不常执行但要占地方」的写路径挪过来。
 * 跨段调用只是地址不同：header 仍写 ROM ONLY，不需要切 bank。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"

/* fat.c 的布局量（挂载后填好）。这里用 f 前缀的全局名避免和局部变量撞。*/
extern uint32_t ffat_base;
extern uint32_t froot_lba;
extern uint16_t froot_sectors;
extern uint32_t froot_cluster;
extern uint32_t fdata_base;
extern uint32_t fcluster_count;
extern uint8_t  fis_fat32;
extern uint8_t  fcsize;
extern uint8_t  fspc_shift;
extern uint32_t ffat_size;
extern uint8_t  fnum_fats;
extern uint8_t  fmount_ok;
extern uint8_t  fbuf[512];

/* 搬过来的代码用短名字，读起来和 fat.c 里一致 */
#define fat_base      ffat_base
#define root_lba      froot_lba
#define root_sectors  froot_sectors
#define root_cluster  froot_cluster
#define data_base     fdata_base
#define cluster_count fcluster_count
#define is_fat32      fis_fat32
#define csize         fcsize
#define sectors_per_cluster_shift fspc_shift
#define fat_size      ffat_size
#define num_fats      fnum_fats
#define buf           fbuf
#define mount_ok      fmount_ok

static uint16_t ld16(const uint8_t *p) { return p[0] | (uint16_t)p[1] << 8; }
static uint32_t ld32(const uint8_t *p) { return ld16(p) | (uint32_t)ld16(p + 2) << 16; }
static uint32_t cluster_sector(uint32_t c)
{
    return data_base + ((c - 2) << sectors_per_cluster_shift);
}

/* ---- 给 fatx.c（SAVER 目录、删除文件）复用的底层件 ----
 * fatx.c 编在 _CODE，本文件编在 BANK1，两个段都能直接 call，所以去掉 static
 * 就够，不需要额外的跳板。宏只是让本文件里的调用点保持原来的短名字。*/
#define dir_seek       fatw_dir_seek
#define fat_alloc      fatw_alloc
#define fat_put        fatw_put

/* far 段的分配/写表在文件后半部分（fat_put/fat_alloc），dir_append 在它们之前
 * 就要用，先给原型。宏在上面已经改好名了。*/
void fat_put(uint32_t c, uint32_t v);
uint32_t fat_alloc(void);
#define fat_free_chain fatw_free_chain
#define lfn_sum        fatw_lfn_sum

/* ==================== 启动游戏：簇链与加载命令 ====================
 * FPGA 的加载窗口收一张 512 字节表（见 docs/06-launch-plan.md §4.1），
 * 之后 PicoBlaze 自己按表把文件流进 pSRAM，CPU 不用参与搬运。
 *
 * 表里最容易写错的一处：段表是 {起始 LBA, 累计已加载扇区数}，第二个 u32
 * 是「从文件开头算起到本段结束一共多少扇区」，不是本段长度。前两段两者
 * 恰好相等，从第三段起才分道扬镳。
 */

/* 把 32 位小端写进命令表的 off 处 */
static void w32(uint8_t *p, uint16_t off, uint32_t v)
{
    p[off]     = (uint8_t)v;
    p[off + 1] = (uint8_t)(v >> 8);
    p[off + 2] = (uint8_t)(v >> 16);
    p[off + 3] = (uint8_t)(v >> 24);
}

/* 簇号 -> 首扇区 LBA。给 game.c 读 ROM 头用。*/
uint32_t fat_cluster_lba(uint32_t cluster)
{
    return cluster_sector(cluster);
}

/* FAT 表里 cluster c 的下一簇。链尾返回 >= EOF 标记的值。
 * 注意：会覆盖本文件的单扇区缓存 fbuf，调用方别指望 fbuf 内容还在。*/
static uint32_t next_cluster(uint32_t c)
{
    uint32_t fs;
    uint16_t off;

    if (is_fat32) {
        fs  = fat_base + (c >> 7);          /* 128 项/扇区 */
        off = (uint16_t)((c & 0x7F) * 4);
        sd_read(fs, fbuf);
        return ld32(fbuf + off) & 0x0FFFFFFFUL;
    }
    fs  = fat_base + (c >> 8);              /* 256 项/扇区 */
    off = (uint16_t)((c & 0xFF) * 2);
    sd_read(fs, fbuf);
    return ld16(fbuf + off);
}

/* 构建加载命令到 cmd（512 字节）。返回 FAT_OK 或错误码。
 * 段表上限 61 段：+$1F0 起是文件大小等尾字段，溢出会踩掉它们。*/
uint8_t fat_build_cmd(uint32_t cluster, uint32_t size, uint8_t *cmd)
{
    uint32_t c = cluster, n, done = 0;
    uint32_t eof = is_fat32 ? 0x0FFFFFF8UL : 0xFFF8UL;
    uint32_t limit = cluster_count + 2;
    uint16_t off = 4;                       /* +$000 是一个 32 位 0 */

    memset(cmd, 0, 512);
    if (cluster < 2 || cluster > limit)
        return FAT_ERR_BAD_CHAIN;

    w32(cmd, off, cluster_sector(c));
    off += 4;

    for (;;) {
        n = next_cluster(c);
        if (++done > limit)                 /* 绕回来的环 */
            return FAT_ERR_BAD_CHAIN;
        if (n != c + 1) {
            if (n >= eof)
                break;
            if (n < 2 || n > limit)
                return FAT_ERR_BAD_CHAIN;
            if (off > 0x1E4)                /* 再写一段就压到 +$1F0 */
                return FAT_ERR_FRAGMENTED;
            w32(cmd, off, done << sectors_per_cluster_shift);  /* 累计扇区数 */
            off += 4;
            w32(cmd, off, cluster_sector(n));
            off += 4;
        }
        c = n;
    }
    w32(cmd, off, 0xFFFFFFFFUL);            /* 剩余全部，到此为止 */

    w32(cmd, 0x1F0, size);
    w32(cmd, 0x1F4, 1);
    w32(cmd, 0x1F8, 1UL << sectors_per_cluster_shift);
    return FAT_OK;
}

/* ==================== 写路径：存档 .SAV ====================
 * 只服务一件事：把「游戏名」写成同名 `.sav` 并覆写其内容，落进**根目录唯一
 * 一个 SAVER 子目录**（v0.11.0 起跟官方一致）。名字来自 8.3 的 ROM 名，天然合规。
 *
 * 早先（v0.9.0 及以前）是写在 ROM 同目录，v0.10.0 改成按 ROM 所在目录各建一个
 * SAVER，理由都是避免 `GBC/口袋妖怪.gbc` 与 `GB/口袋妖怪.gbc` 互相覆盖。
 * 真机反馈根目录已经有一个、子目录里又冒出第二个反而乱，于是统一回根目录。
 * 同名不同目录的游戏共用一个存档是官方行为，接受。
 *
 * 三条容易踩的规矩：
 *   1. FAT 有 2 张副本时**两张都要写**，只写第一张的卡在电脑上会显示损坏；
 *   2. FAT32 的 28 位簇号：写回时高 4 位必须保留（那是标志位，不是簇号）；
 *   3. 目录项里首簇的高 16 位在 +$14，低 16 位在 +$1A（FAT32 才有高位）。
 */

/* 目录 dir（0 = 根目录）的第 n 个扇区；越界返回 $FFFFFFFF。
 * 每次从目录头走簇链 —— 目录通常只有一两个簇，比维护游标省一截代码。*/
static uint32_t dir_nth(uint32_t dir, uint16_t n)
{
    uint32_t c;
    uint16_t skip;
    /* ★ 链尾判据必须按 FAT 类型分开（v0.12.7 修）。
     * FAT16 的 EOF 是 $FFF8 起，早先这里一律用 FAT32 的 $0FFFFFF8 比 ——
     * 于是 FAT16 卡上 $FFFF 这种正常链尾被当成"还有下一簇"，cluster_sector()
     * 拿它算出一个**数据区之外的扇区**，dir_nth 返回一个野 LBA。调用方从那
     * 个扇区读到的随机内容会被当成目录项/空槽，写目录项时就写到那儿去了：
     * 卡上 SAVER 里干干净净，屏幕照样报「完成」（读回走同一套野地址）。*/
    uint32_t eof = is_fat32 ? 0x0FFFFFF8UL : 0xFFF8UL;

    if (dir == 0 && !is_fat32)
        return (n < root_sectors) ? root_lba + n : 0xFFFFFFFFUL;

    c = (dir == 0) ? root_cluster : dir;
    skip = (uint16_t)(n / csize);
    while (skip--) {
        if (c < 2 || c >= eof || c > cluster_count + 1) return 0xFFFFFFFFUL;
        c = next_cluster(c);
    }
    if (c < 2 || c >= eof || c > cluster_count + 1) return 0xFFFFFFFFUL;
    return cluster_sector(c) + (n % csize);
}

/* 在**当前目录**里找 11 字节名（8.3 原始格式）：
 * 返回 1 = 已有同名项（lba/off 指向它），2 = 没找到。
 *
 * ★ v0.13.0 起这里只负责「查找」，「找空槽」交给 dir_alloc。
 * 早先它同时统计连续空槽，还要求整串长名项落在同一个扇区里 —— 为了把追加位
 * 挪到下一扇区，外层就去改写尾部 $00，那条路把目录写死过两次（v0.12.6 抹掉
 * 终止符、v0.12.7 把「没有 $00」判成「目录满」，删光存档也救不回来）。
 * 两件事拆开之后，谁也不用猜对方的意思。*/
uint8_t dir_seek(const uint8_t *raw11, uint32_t *lba, uint16_t *off)
{
    uint16_t n, i;

    for (n = 0; n < 256; n++) {
        uint32_t sl = dir_nth(fat_cwd, n);

        if (sl == 0xFFFFFFFFUL) break;
        sd_read(sl, fbuf);
        for (i = 0; i < 512; i += 32) {
            uint8_t *e = fbuf + i;

            if (e[0] == 0x00) return 2;              /* 目录到此结束 */
            if (e[0] == 0xE5) continue;              /* 已删除，跳过 */
            if ((e[11] & 0x3F) == 0x0F) continue;    /* LFN 项 */
            if (e[11] & 0x18) continue;              /* 目录 / 卷标 */
            if (memcmp(e, raw11, 11) == 0) {
                *lba = sl;
                *off = i;
                return 1;
            }
        }
    }
    return 2;
}

/* 找 nent 个**连续**空槽，$E5 与 $00 同等看待。
 *
 * 这是照 ChaN 的 FatFs R0.16 抄的（ff.c 的 dir_alloc）：
 *     if (dp->dir[DIR_Name] == DDEM || dp->dir[DIR_Name] == 0) {
 *         if (++n == n_ent) break;
 *     } else n = 0;
 * 它从来不去改写 $00。$00 只是个「可以用的空槽」，写满了自然又被后面的 $00
 * 接上 —— 目录终止符自己就跟着往后走了，根本不需要腾位。
 *
 * 也允许跨扇区：写的时候按扇区拆开写（dir_put），不再要求整串落在同一个扇区。
 * 「必须同扇区」这条要求是所有麻烦的起点。
 *
 * 返回 1 = 找到（sidx = 相对目录首扇区的扇区号，soff = 扇区内字节偏移）；
 * 0 = 这条簇链里凑不出来。*/
static uint8_t dir_alloc(uint8_t nent, uint16_t *sidx, uint16_t *soff)
{
    uint16_t n, i, run = 0;

    for (n = 0; n < 256; n++) {
        uint32_t sl = dir_nth(fat_cwd, n);

        if (sl == 0xFFFFFFFFUL) return 0;      /* 链到尾了，没凑够 */
        sd_read(sl, fbuf);
        for (i = 0; i < 512; i += 32) {
            uint8_t c = fbuf[i];

            if (c == 0xE5 || c == 0x00) {
                if (run == 0) { *sidx = n; *soff = i; }
                if (++run == nent) return 1;
            } else {
                run = 0;                        /* 撞上有效项，重新计数 */
            }
        }
    }
    return 0;
}

/* 给**当前目录**的簇链尾接一簇并清零 —— 对应 FatFs 的 create_chain + dir_clear。
 *
 * 目录本来就是普通文件，没有「目录不能长」这回事。新簇全是 $00，等于一整簇
 * 空槽，dir_alloc 重新扫一次必然够。
 * 返回新簇号；0 = 失败（没有空簇，或这是 FAT16 的根目录固定区）。*/
static uint32_t dir_append(void)
{
    uint32_t c, w, eof = is_fat32 ? 0x0FFFFFF8UL : 0xFFF8UL;
    uint8_t  i, guard;

    if (fat_cwd < 2) return 0;             /* FAT16 根目录在固定区，扩不了 */

    c = fat_cwd;
    for (guard = 0; guard < 64; guard++) { /* 走到链尾 */
        w = next_cluster(c);
        if (w < 2 || w >= eof || w > cluster_count + 1) break;
        c = w;
    }
    if (guard >= 64) return 0;             /* 链成环，别陪着转 */

    w = fat_alloc();
    if (w < 2) return 0;
    fat_put(w, is_fat32 ? 0x0FFFFFFFUL : 0xFFFFUL);
    fat_put(c, w);                         /* 链尾 -> 新簇 */
    memset(fbuf, 0, 512);
    for (i = 0; i < csize; i++)
        sd_write(cluster_sector(w) + i, fbuf);
    return w;
}

/* ---- VFAT 长文件名项 ----
 * 一条 LFN 项装 13 个 UCS-2 字符，物理上按序号**倒序**排在短名项前面：
 *   ord=n|0x40, ord=n-1, ..., ord=1, 短名项
 * ord=1 那条存名字的第 0-12 个字符。校验和由 11 字节短名算出，两边靠它配对。*/
uint8_t lfn_sum(const uint8_t *raw11)
{
    uint8_t sum = 0, i;

    for (i = 0; i < 11; i++)
        sum = (uint8_t)(((sum & 1) ? 0x80 : 0) + (sum >> 1) + raw11[i]);
    return sum;
}

static void lfn_entry(uint8_t *e, uint8_t ord, uint8_t first,
                      const uint16_t *name, uint8_t len, uint8_t sum)
{
    uint8_t base = (uint8_t)((ord - 1) * 13);
    uint8_t j;

    e[0]  = (uint8_t)(ord | (first ? 0x40 : 0));
    e[11] = 0x0F;
    e[12] = 0;
    e[13] = sum;
    e[26] = 0;                          /* 首簇低 16 位必须为 0 */
    e[27] = 0;
    for (j = 0; j < 13; j++) {
        uint8_t  ix = (uint8_t)(base + j);
        uint16_t cp;
        uint8_t  o;

        if (ix < len)       cp = name[ix];
        else if (ix == len) cp = 0x0000;   /* 名字结束 */
        else                cp = 0xFFFF;   /* 后面填满 */
        if (j < 5)       o = (uint8_t)(1 + j * 2);
        else if (j < 11) o = (uint8_t)(14 + (j - 5) * 2);
        else             o = (uint8_t)(28 + (j - 11) * 2);
        e[o]     = (uint8_t)cp;
        e[o + 1] = (uint8_t)(cp >> 8);
    }
}

/* 从 (sidx, soff) 起写 nent 条 LFN + 1 条短名项 + 1 条 $00 终止符。
 *
 * 按扇区拆开写：起点离扇区尾只差一两槽时，长名项会跨到下一个扇区 —— 这在
 * FatFs 里是常态（它每次 move_window 只改 32 字节，窗口自己滚动），我们按
 * 扇区读写，就把同一扇区的那几项攒起来写一次。跨扇区不是异常，不用绕。
 *
 * 最后一项固定写 $00：目录终止符必须紧跟最后一个有效项，否则 Windows 从
 * $00 处截断，新写的项就成了「隐形项」。dir_alloc 要了 nent+2 个槽，其中
 * 最后那个槽就是留给它的。*/
/* dir_put 的两个 32 位入参走文件内全局。
 *
 * 8 个参数在 sm83 上要压 18 字节栈，而主机测试实测到 size 拿到的是**上一次**
 * 的值（改成 32KB 之后目录项里还写着 8192）。这里只有一个调用者，两者同步
 * 赋值，用全局既避开长参数表，也省掉一层栈。*/
static uint32_t dp_first, dp_size;

static void dir_put(uint8_t nent, uint16_t sidx, uint16_t soff,
                    const uint8_t *raw, const uint16_t *sav, uint8_t slen)
{
    uint32_t first = dp_first, size = dp_size;
    uint8_t sum = lfn_sum(raw), k = 0;
    uint8_t last = (uint8_t)(nent + 1);        /* 下标 nent 是短名，nent+1 是终止符 */

    while (k <= last) {
        uint16_t o = (uint16_t)(soff + k * 32);   /* 一屏之内，16 位够 */
        uint16_t cur = (uint16_t)(o >> 9);
        uint32_t lba = dir_nth(fat_cwd, (uint16_t)(sidx + cur));

        if (lba == 0xFFFFFFFFUL) return;
        sd_read(lba, fbuf);
        for (; k <= last; k++) {
            uint8_t *e;

            o = (uint16_t)(soff + k * 32);
            if ((uint16_t)(o >> 9) != cur) break;
            e = fbuf + (o & 511);

            if (k < nent) {
                lfn_entry(e, (uint8_t)(nent - k), (uint8_t)(k == 0),
                          sav, slen, sum);
            } else if (k == nent) {
                memcpy(e, raw, 11);
                memset(e + 11, 0, 15);                 /* 属性 + 时间日期 */
                e[11] = 0x20;                          /* 普通文件 */
                /* +$0C 的 bit4 = 「扩展名是小写」。短名本身仍存大写 "SAV"
                 * （FAT 规定），这一位只影响 Windows / Linux 的显示。*/
                e[12] = 0x10;
                e[20] = (uint8_t)(first >> 16);        /* 首簇高 16 位 */
                e[21] = (uint8_t)(first >> 24);
                e[26] = (uint8_t)first;                /* 首簇低 16 位 */
                e[27] = (uint8_t)(first >> 8);
                e[28] = (uint8_t)size;
                e[29] = (uint8_t)(size >> 8);
                e[30] = (uint8_t)(size >> 16);
                e[31] = (uint8_t)(size >> 24);
            } else {
                e[0] = 0x00;                           /* 目录终止符 */
            }
        }
        sd_write(lba, fbuf);
    }
}

/* 给调用方（fatx.c 的 mkdir）找 nent 个连续空槽，返回落点。
 *
 * mkdir 要的是「往哪儿写」，不是「有没有同名项」—— 这两件事 v0.13.0 起分开
 * 了。早先它复用 dir_seek 拿到的落点，而 dir_seek 现在只负责查找、不再设置
 * lba/off，直接沿用会把目录项写到扇区 0（引导扇区）。宁可多一个函数。*/
uint8_t fatw_slot(uint8_t nent, uint32_t *lba, uint16_t *off)
{
    uint16_t sidx = 0, soff = 0;
    uint32_t sl;

    if (!dir_alloc(nent, &sidx, &soff)) {
        if (!dir_append() || !dir_alloc(nent, &sidx, &soff))
            return 0;
    }
    sl = dir_nth(fat_cwd, sidx);
    if (sl == 0xFFFFFFFFUL) return 0;
    *lba = sl;
    *off = soff;
    return 1;
}

/* 把 ROM 的长名换成 .SAV 的长名："K 口袋妖怪.gbc" -> "K 口袋妖怪.sav"。
 * 返回字符数。
 *
 * 关键点（v0.12.0 修）：
 *   1. 取**最后一个**点当扩展名分隔，而不是第一个 —— 名字里常有 "2.0" 这种点，
 *      "2.0.gbc" 把第一个点当分隔会变成 "...加强版2.sav"，丢掉 "0.gbc"，和 ROM
 *      文件名对不上，加载器就配不上对。
 *   2. 主体**不截断**，整段搬过来（缓冲 FAT_LFN_FULL 够宽）；只换末位扩展名。
 *      截断会让 .sav 比 ROM 短，同样导致「存档读不出来」。*/
static uint8_t sav_lfn(const uint16_t *rom, uint16_t *out)
{
    uint8_t i, n = 0, dot = 0, o = 0;

    while (n < FAT_LFN_FULL - 1 && rom[n]) n++;
    /* 最后一个 '.' 才是扩展名分隔 —— 名字里可能还有别的点（"加强版2.0.gbc"
     * 的 "2.0"），按第一个点切会把 "0.gbc" 当成扩展名吞掉。
     * dot 记的是**点本身的下标**，拷贝时不含它，否则会拼出 "…2.0..sav"。*/
    for (i = 0; i < n; i++)
        if (rom[i] == '.') dot = i;
    for (i = 0; i < dot && o < FAT_LFN_FULL - 5; i++)
        out[o++] = rom[i];
    out[o++] = '.';
    out[o++] = 's';
    out[o++] = 'a';
    out[o++] = 'v';
    out[o] = 0;
    return o;
}

/* 写一个 FAT 表项（两张副本都写）。FAT32 保留高 4 位。*/
void fat_put(uint32_t c, uint32_t v)
{
    uint32_t fs;
    uint16_t off;
    uint8_t  k;

    for (k = 0; k < num_fats; k++) {
        fs = fat_base + (uint32_t)k * fat_size;
        if (is_fat32) {
            fs  += (c >> 7);
            off  = (uint16_t)((c & 0x7F) * 4);
            sd_read(fs, fbuf);
            fbuf[off]     = (uint8_t)v;
            fbuf[off + 1] = (uint8_t)(v >> 8);
            fbuf[off + 2] = (uint8_t)(v >> 16);
            fbuf[off + 3] = (uint8_t)((fbuf[off + 3] & 0xF0) | (uint8_t)(v >> 24));
        } else {
            fs  += (c >> 8);
            off  = (uint16_t)((c & 0xFF) * 2);
            sd_read(fs, fbuf);
            fbuf[off]     = (uint8_t)v;
            fbuf[off + 1] = (uint8_t)(v >> 8);
        }
        sd_write(fs, fbuf);
    }
}

/* 找一个空闲簇：按扇区扫 FAT，一次读能看 256(FAT16)/128(FAT32) 项。*/
uint32_t fat_alloc(void)
{
    uint32_t s, c, limit = cluster_count + 2;
    uint16_t i;

    for (s = 0; s < fat_size; s++) {
        sd_read(fat_base + s, fbuf);
        if (is_fat32) {
            for (i = 0; i < 512; i += 4) {
                c = (s << 7) + (i >> 2);
                if (c >= 2 && c < limit && (ld32(fbuf + i) & 0x0FFFFFFFUL) == 0)
                    return c;
            }
        } else {
            for (i = 0; i < 512; i += 2) {
                c = (s << 8) + (i >> 1);
                if (c >= 2 && c < limit && ld16(fbuf + i) == 0)
                    return c;
            }
        }
    }
    return 0;
}

/* 把一条簇链标记为空（回收旧存档）。*/
void fat_free_chain(uint32_t c)
{
    uint32_t n, eof = is_fat32 ? 0x0FFFFFF8UL : 0xFFF8UL;
    uint32_t guard = cluster_count + 2;

    while (c >= 2 && c < eof && guard--) {
        n = next_cluster(c);
        fat_put(c, 0);
        c = n;
    }
}

/* "POKEMON.GBC" -> 8.3 原始 11 字节 "POKEMON SAV"（右侧空格填充）。
 * ROM 名本来就是 8.3，所以改扩展名就够了，不需要 LFN。*/
static void sav_name(const char *rom, uint8_t *raw)
{
    uint8_t i;

    for (i = 0; i < 11; i++) raw[i] = ' ';
    for (i = 0; i < 8 && rom[i] && rom[i] != '.'; i++)
        raw[i] = (uint8_t)rom[i];
    raw[8]  = 'S';
    raw[9]  = 'A';
    raw[10] = 'V';
}

/* ---- SAVER 目录 ----
 * 官方 EZ-Flash Junior 把存档收在名为 SAVER 的文件夹里，本内核跟随这个约定，
 * 而且**全卡只此一个，固定在根目录**（v0.11.0 起）。不管 ROM 在几层子目录里，
 * 得到的都是 `根/SAVER/xxx.sav`。
 *
 * v0.10.0 曾按 ROM 所在目录各建一个，用户实测根目录已有一个、子目录里又冒出
 * 第二个，反而乱。下面的 saver_root() 会临时把 fat_cwd 切到根目录再查/建。
 * 建不出来（卡满 / 目录项写不进）也退回根目录写，不会留下散落的 .sav。*/
static const uint8_t saver11[11] = {'S','A','V','E','R',' ',' ',' ',' ',' ',' '};

/* 在当前目录里找一个**子目录**，返回首簇；没有返回 0。
 *
 * 不能复用 dir_seek()：那个函数是给「找同名文件」用的，它把属性位 0x10
 * （目录）当噪声跳过 —— 拿它找文件夹会永远返回「不存在」，然后就每备份一次
 * 建一个新 SAVER，卡上很快堆出一串空目录。*/
uint32_t fat_find_dir(const uint8_t *raw11)
{
    uint16_t n, i;

    for (n = 0; n < 256; n++) {
        uint32_t sl = dir_nth(fat_cwd, n);
        if (sl == 0xFFFFFFFFUL)
            break;
        sd_read(sl, fbuf);
        for (i = 0; i < 512; i += 32) {
            uint8_t *e = fbuf + i;

            if (e[0] == 0) return 0;
            if (e[0] == 0xE5) continue;
            if ((e[11] & 0x3F) == 0x0F) continue;      /* 长名项 */
            if ((e[11] & 0x10) && memcmp(e, raw11, 11) == 0) {
                uint32_t c = (uint32_t)ld16(e + 26) |
                             ((uint32_t)ld16(e + 20) << 16);
                return (c >= 2) ? c : 0;
            }
        }
    }
    return 0;
}

/* SAVER 固定在**根目录**，全卡只此一个（v0.11.0 起跟官方目录约定一致）。
 * v0.10.0 按 ROM 所在目录各建一个，用户实测根目录已经有一个、子目录里又冒出
 * 第二个，反而乱。两个函数都临时把工作目录切到根，出口还原，这样不管 ROM
 * 在几层子目录里，存档都落在同一个 SAVER。*/
static uint32_t saver_root(uint8_t create)
{
    uint32_t saved = fat_cwd, c;

    fat_cwd = 0;                        /* 0 = 根目录（FAT32 内部会映射 root cluster）*/
    c = create ? fat_mkdir(saver11) : fat_find_dir(saver11);
    fat_cwd = saved;
    return c;
}

/* 只查不建：读存档时不该为了找文件而在卡上写东西。*/
static uint32_t saver_find(void)
{
    return saver_root(0);
}

/* 没有就建一个（fatx.c）。返回簇号，0 = 失败。*/
static uint32_t saver_dir(void)
{
    return saver_root(1);
}

/* 打开「同名的 .SAV」准备整文件覆写。大小相同就复用原簇链（不抄走旧数据，
 * 反正马上被整块覆盖），大小不同则回收旧链重新分配。
 * 返回 FAT_OK / FAT_ERR_ADD_DIR / FAT_ERR_NO_SPACE。*/
uint8_t fat_open_sav(FatFile *f, const char *rom_name, uint32_t size,
                     const uint16_t *lfn)
{
    uint8_t  raw[11], r, slen = 0, nent = 0, rc = FAT_OK, reuse = 0;
    uint32_t lba = 0, first = 0, need, c, prev = 0, guard, dir0, sv;
    uint16_t off = 0, sidx = 0, soff = 0;
    uint16_t *sav = g_lfn_buf;          /* 共用 scratch：名字是 ROM 名就地改 .sav */

    if (!mount_ok || size == 0) return FAT_ERR_NO_FS;

    sav_name(rom_name, raw);

    /* 只在新建目录项时写长名。已有 .SAV（尤其是旧版留下的短名文件）保持
     * 原样不动 —— 「搬目录项」要删旧项写新项，中途断电会丢档，不值得。 */
    if (lfn && lfn[0]) {
        slen = sav_lfn(lfn, sav);
        nent = (uint8_t)((slen + 12) / 13);
        /* 一条 LFN 项装 13 字符，9 条够 117 字符（远超现实最长名）。上限只用来
         * 兜底，真正能不能写这么多条，还要看下面 dir_seek 找不找得到连续空槽。*/
        if (nent > 9) nent = 9;
    }

    /* 切进 SAVER 再操作。dir_seek 用的是 fat_cwd，所以只改这一个值。
     * dir0 留着，出口处必须还原 —— 否则备份完之后列表会停在 SAVER 里
     * （那里面一个游戏都没有，玩家会以为卡出问题了）。*/
    dir0 = fat_cwd;
    sv = saver_dir();
    /* 建不出 SAVER 就报错，**不要**退回根目录写。
     * v0.12.5 及以前是 `fat_cwd = sv ? sv : 0` —— 文件落进根目录，而
     * save_verify → fat_open_read() 里有"SAVER 找不到就回根目录找"的兜底，
     * 于是照样校验通过、屏幕照样报「完成」，玩家在 SAVER 里永远找不到它。
     * 宁可让屏幕说「目录已满」，也不要把文件丢到没人会去看的地方。*/
    if (!sv) { rc = FAT_ERR_ADD_DIR; goto out; }
    fat_cwd = sv;

    r = dir_seek(raw, &lba, &off);
    if (r == 1) {
        sd_read(lba, fbuf);
        first = (uint32_t)ld16(fbuf + off + 26) |
                ((uint32_t)ld16(fbuf + off + 20) << 16);
        if (ld32(fbuf + off + 28) == size && first >= 2) {
            /* 顺手把「扩展名小写」标志补上：v0.9.0 写的 .SAV 没有这一位，
             * 重写一次之后卡上就统一显示成 .sav 了。*/
            if (!(fbuf[off + 12] & 0x10)) {
                fbuf[off + 12] |= 0x10;
                sd_write(lba, fbuf);
            }
            f->cluster = first;
            f->sec_in  = 0;
            goto out;
        }
        if (first >= 2) fat_free_chain(first);
        first = 0;
        /* ★ 大小变了也要**改这一项**，不能另开一条。
         * 重开的话旧项还指着刚被释放的簇，卡上就多出一个同名的坏文件，
         * 而 parse/Windows 先读到的是旧项（size 还是老值）。v0.13.0 第一版
         * 就踩了这个坑，主机测试里表现为「改成 32KB，目录项写着 8192」。*/
        reuse = 1;
    }

    need = (size + (((uint32_t)csize << 9) - 1)) / ((uint32_t)csize << 9);
    guard = need;
    while (guard--) {
        c = fat_alloc();
        if (c == 0) { rc = FAT_ERR_NO_SPACE; goto out; }
        fat_put(c, is_fat32 ? 0x0FFFFFFFUL : 0xFFFFUL);
        if (prev) fat_put(prev, c);
        else      first = c;
        prev = c;
    }

    /* ---- 找落点并把目录项写进去 ----
     * 要 nent+2 个连续空槽：nent 条 LFN + 1 条短名 + 1 条终止符 $00。
     * 结尾那个槽是必须的：没它，新写的项后面直接是链尾，Windows 读不到
     * 「目录到此结束」，轻则显示多余项，重则整条目录被判成损坏。
     *
     * 凑不齐就扩一簇再找 —— 新簇全 $00，必然够。这条路径以前是「报目录满」，
     * 玩家只能手动删存档；而 SAVER 才十几个文件，根本不该满。*/
    if (reuse) {
        /* 原项还在原地：只改首簇和大小，长名一个字都不动
         * （旧版写的短名项保持短名，别为了好看去搬它 —— 搬就要删旧写新，
         * 中途断电就丢档）。*/
        sd_read(lba, fbuf);
        fbuf[off + 20] = (uint8_t)(first >> 16);
        fbuf[off + 21] = (uint8_t)(first >> 24);
        fbuf[off + 26] = (uint8_t)first;
        fbuf[off + 27] = (uint8_t)(first >> 8);
        w32(fbuf, (uint16_t)(off + 28), size);
        sd_write(lba, fbuf);
    } else {
        if (!dir_alloc((uint8_t)(nent + 2), &sidx, &soff)) {
            if (!dir_append() ||
                !dir_alloc((uint8_t)(nent + 2), &sidx, &soff)) {
                rc = FAT_ERR_ADD_DIR;
                goto out;
            }
        }
        dp_first = first;
        dp_size  = size;
        dir_put(nent, sidx, soff, raw, sav, slen);
    }

    f->cluster = first;
    f->sec_in  = 0;

out:
    fat_cwd = dir0;
    return rc;
}

/* 打开已有的 .SAV 准备顺序读；不存在返回 FAT_ERR_NO_FS。
 *
 * 先找 SAVER，找不到再退回 ROM 同目录 —— v0.9.0 及以前存档是写在 ROM 旁边的，
 * 升级上来第一次读档必须还能读到它，否则玩家看到的就是「存档没了」。*/
uint8_t fat_open_read(FatFile *f, const char *rom_name, uint32_t *size)
{
    uint8_t  raw[11], r;
    uint32_t lba = 0, first, dir0, sv;
    uint16_t off = 0;

    if (!mount_ok) return FAT_ERR_NO_FS;
    sav_name(rom_name, raw);

    dir0 = fat_cwd;
    sv = saver_find();
    if (sv) fat_cwd = sv;

    r = dir_seek(raw, &lba, &off);
    if (r != 1 && sv) {                 /* 老位置回落 */
        fat_cwd = dir0;
        r = dir_seek(raw, &lba, &off);
    }
    fat_cwd = dir0;
    if (r != 1) return FAT_ERR_NO_FS;

    sd_read(lba, fbuf);
    first = (uint32_t)ld16(fbuf + off + 26) |
            ((uint32_t)ld16(fbuf + off + 20) << 16);
    if (first < 2) return FAT_ERR_NO_FS;
    *size = ld32(fbuf + off + 28);
    f->cluster = first;
    f->sec_in  = 0;
    return FAT_OK;
}

/* 顺序读一个扇区到 dst（512 字节），簇内读满自动沿链走。*/
uint8_t fat_read_sec(FatFile *f, uint8_t *dst)
{
    sd_read(cluster_sector(f->cluster) + f->sec_in, dst);
    if (++f->sec_in >= csize) {
        f->sec_in = 0;
        f->cluster = next_cluster(f->cluster);
        if (f->cluster < 2 || f->cluster >= (is_fat32 ? 0x0FFFFFF8UL : 0xFFF8UL))
            return 0;                    /* 最后一簇读完，正常结束 */
    }
    return 1;
}

/* 往已打开的文件顺序追加一个扇区（512 字节）。簇内写满就沿链推进。
 *
 * 注意：写完**最后一簇的最后一扇区**时簇号会变成 EOF，这不是错误 ——
 * 判断"链断了"要放到下一次调用（那时 f->cluster 已经是 EOF/0）。早先在
 * 这里直接报错，8KB 的存档恰好整簇写完，第一次写就返回 6。
 */
uint8_t fat_write_sec(FatFile *f, const uint8_t *src)
{
    uint32_t eof = is_fat32 ? 0x0FFFFFF8UL : 0xFFF8UL;

    if (f->cluster < 2 || f->cluster >= eof)
        return FAT_ERR_BAD_CHAIN;
    sd_write(cluster_sector(f->cluster) + f->sec_in, src);
    if (++f->sec_in >= csize) {
        f->sec_in = 0;
        f->cluster = next_cluster(f->cluster);
    }
    return 0;
}
