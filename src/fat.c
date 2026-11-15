/* fat.c — 极简 FAT16/FAT32 目录扫描（只读）
 *
 * 只做「列出一个目录里的 .GB/.GBC 和子目录」这一件事，因此比上游
 * stage1/src/fat.c 大幅简化：不写文件、不建加载命令。布局解析
 * （MBR/BPB/FAT 类型判定）参考上游，属 FAT 规范。
 *
 * 目录浏览：fat_cwd 记录当前目录（0 = 根，否则子目录首簇），扫描、取长名、
 * .SAV 读写全都看它。进目录时只改这一个值再重扫，上层一处不用动。
 *
 * 可移植性：本文件只依赖 sd_read()/memcpy()。主机侧验证时把 sd_read 换成
 * 读取一个构造好的 FAT 镜像即可（tools/fat_host_test.py 用等价算法核对）。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"

uint8_t fbuf[512];            /* 单扇区缓存 */

static uint16_t ld16(const uint8_t *p) { return p[0] | (uint16_t)p[1] << 8; }
static uint32_t ld32(const uint8_t *p) { return ld16(p) | (uint32_t)ld16(p + 2) << 16; }

/* 全局布局（挂载后有效）*/
uint32_t fpart_lba;           /* 分区起始 LBA（MBR）或 0（superfloppy）*/
uint32_t ffat_base;           /* 第一张 FAT 的扇区 */
uint32_t froot_lba;           /* FAT16 根目录首扇区；FAT32 用 froot_cluster */
uint16_t froot_sectors;       /* FAT16 根目录扇区数；FAT32 = 0 */
uint32_t froot_cluster;       /* FAT32 根目录簇 */
uint8_t  fis_fat32;
uint8_t  fcsize;              /* 每簇扇区数 */
uint32_t fdata_base;          /* cluster 2 的扇区 */
uint32_t fcluster_count;      /* 数据簇总数，用于判 FAT12/16/32 */
uint8_t  fspc_shift;
uint8_t  fmount_ok;           /* 最近一次 fat_mount() 是否成功 */
/* 写路径要用的两个量，挂载时从 BPB 里存下来 */
uint32_t ffat_size;           /* 每张 FAT 的扇区数 */
uint8_t  fnum_fats;         /* FAT 副本张数（1 或 2），写表时两张都要更新 */

/* ---- 当前目录 ----
 * 0 表示根目录，非 0 是子目录首簇。扫描、取长名、.SAV 读写全都看这一个值，
 * 所以进目录只要改它 + 重扫，别处一行都不用动。*/
uint32_t fat_cwd;
uint8_t  fat_cwd_depth;
static uint32_t fat_cwd_stack[FAT_DIR_DEPTH];

/* 长名 scratch（见 fat.h）。fat_full_lfn 与 fat_open_sav 共用，避免大栈帧。*/
uint16_t g_lfn_buf[FAT_LFN_FULL];

void fat_dir_reset(void)
{
    fat_cwd = 0;
    fat_cwd_depth = 0;
}

uint8_t fat_dir_enter(uint32_t cluster)
{
    if (cluster < 2 || fat_cwd_depth >= FAT_DIR_DEPTH)
        return 0;
    fat_cwd_stack[fat_cwd_depth++] = fat_cwd;
    fat_cwd = cluster;
    return 1;
}

uint8_t fat_dir_up(void)
{
    if (fat_cwd_depth == 0)
        return 0;                    /* 已经在根目录 */
    fat_cwd = fat_cwd_stack[--fat_cwd_depth];
    return 1;
}

uint32_t cluster_sector(uint32_t c)
{
    return fdata_base + ((c - 2) << fspc_shift);
}

/* BPB 的关键字段自洽？有些卡的分区引导扇区 0x36 处没有 "FAT" 标签，
 * 只靠标签会把正常卡判成"不是 FAT"。这里用几个必须成立的字段兜底：
 * 512 字节扇区、每簇扇区数是 2 的幂、保留扇区 >=1、FAT 张数 1-2。*/
static uint8_t bpb_sane(void)
{
    uint8_t spc = fbuf[0x0D], n = fbuf[0x10];

    if (ld16(fbuf + 0x0B) != 512) return 0;
    if (spc == 0 || (spc & (uint8_t)(spc - 1))) return 0;
    if (ld16(fbuf + 0x0E) == 0) return 0;
    if (n < 1 || n > 2) return 0;
    return 1;
}

static uint8_t is_boot_sector(void)
{
    if (fbuf[510] != 0x55 || fbuf[511] != 0xAA)
        return 0;
    if (memcmp(fbuf + 0x36, "FAT", 3) == 0 || memcmp(fbuf + 0x52, "FAT", 3) == 0)
        return 1;
    return bpb_sane();
}

uint8_t fat_mount(void)
{
    uint32_t fat_sz, tot_sec, res_sec, root_ent_sz;
    uint8_t  nfats;
    uint16_t byts, root_entries, fat_sz16;

    fmount_ok = 0;

    sd_read(0, fbuf);
    if (fbuf[510] != 0x55 || fbuf[511] != 0xAA)
        return FAT_ERR_NO_SIG;

    /* LBA0 可能有两种东西：整个卷的引导扇区（superfloppy），
     * 或者 MBR 分区表。判据不能是"0x0B 处是不是 512"——MBR 那块是引导代码，
     * 装的是 x86 指令，真机上这里几乎永远不等于 512，早先按这个判会把
     * 所有带分区表的正常卡判成 NO_SIG（真机实测 ERR=1 就是这么来的）。
     * 改成：先认 FAT 引导扇区，认不出来才当 MBR，取第一个分区项。*/
    if (is_boot_sector()) {
        fpart_lba = 0;
    } else {
        if (fbuf[0x1BE + 4] == 0xEE)        /* GPT 保护分区 */
            return FAT_ERR_GPT;
        fpart_lba = ld32(fbuf + 0x1BE + 8);  /* 第一个分区起始 LBA */
        if (fpart_lba == 0)
            return FAT_ERR_NO_FS;
        sd_read(fpart_lba, fbuf);
        if (!is_boot_sector())
            return FAT_ERR_NO_FS;
    }

    byts = ld16(fbuf + 0x0B);
    fcsize = fbuf[0x0D];
    res_sec = ld16(fbuf + 0x0E);
    nfats = fbuf[0x10];
    root_entries = ld16(fbuf + 0x11);
    fat_sz16 = ld16(fbuf + 0x16);
    tot_sec = ld16(fbuf + 0x13);
    if (tot_sec == 0) tot_sec = ld32(fbuf + 0x20);
    fat_sz = fat_sz16;
    if (fat_sz == 0) fat_sz = ld32(fbuf + 0x24);

    /* 每簇扇区数是 2 的幂，用移位代替乘法 */
    fspc_shift = 0;
    {
        uint8_t c = fcsize, s = 0;
        while (c > 1) { c >>= 1; s++; }
        fspc_shift = s;
    }

    ffat_base = fpart_lba + res_sec;
    ffat_size = fat_sz;
    fnum_fats = nfats;
    root_ent_sz = ((uint32_t)root_entries * 32 + byts - 1) / byts;
    fdata_base = ffat_base + (uint32_t)nfats * fat_sz + root_ent_sz;

    /* 数据簇总数按 FAT 规范扣掉保留扇区、各张 FAT、FAT16 根目录区，
     * 直接用 tot_sec/fcsize 会把它们算成本簇，边界卷会误判类型。*/
    {
        uint32_t meta = res_sec + (uint32_t)nfats * fat_sz + root_ent_sz;
        fcluster_count = (tot_sec > meta) ? (tot_sec - meta) / fcsize : 0;
    }

    if (fcluster_count < 4085) {
        fis_fat32 = 0;
        froot_sectors = (uint16_t)root_ent_sz;
        froot_cluster = 0;
        froot_lba = ffat_base + (uint32_t)nfats * fat_sz;
        return FAT_ERR_FAT12;            /* FAT12 卷不支持 */
    } else if (fcluster_count < 65525) {
        fis_fat32 = 0;
        froot_sectors = (uint16_t)root_ent_sz;
        froot_cluster = 0;
        froot_lba = ffat_base + (uint32_t)nfats * fat_sz;
        fmount_ok = 1;
        return FAT_OK;                   /* FAT16 */
    } else {
        fis_fat32 = 1;
        froot_sectors = 0;
        froot_cluster = ld32(fbuf + 0x2C);
        froot_lba = cluster_sector(froot_cluster);
        fmount_ok = 1;
        return FAT_OK;                   /* FAT32 */
    }
}

/* 供自检屏显示：把挂载时的内部布局变量摊平成几个数字 */
void fat_info(FatInfo *out)
{
    out->mounted = fmount_ok;
    out->is_fat32 = fis_fat32;
    out->sectors_per_cluster = fcsize;
    out->part_lba = fpart_lba;
    out->cluster_count = fcluster_count;
}

/* 把 11 字节 8.3 短名格式化为 "NAME.EXT"（去尾空格），返回长度。
 * 目录项的扩展名三字节全是空格，这时不补那个点 —— 否则文件夹会显示成
 * "GBC." 这种怪名字。*/
static uint8_t format_name(const uint8_t *raw, char *out)
{
    uint8_t i, n = 0;
    for (i = 0; i < 8 && raw[i] != ' '; i++) out[n++] = (char)raw[i];
    if (raw[8] != ' ') {
        out[n++] = '.';
        for (i = 8; i < 11 && raw[i] != ' '; i++) out[n++] = (char)raw[i];
    }
    out[n] = 0;
    return n;
}

static uint8_t is_rom(const uint8_t *raw)
{
    /* 扩展名 3 字节在 raw[8..10] */
    uint8_t e0 = raw[8], e1 = raw[9], e2 = raw[10];
    if (e0 != 'G' || e1 != 'B') return 0;
    return e2 == ' ' || e2 == 'C';       /* .GB 或 .GBC */
}

/* 这个目录项要不要进列表？1 = 游戏，2 = 子目录，0 = 跳过。
 * fat_scan_games 和 fat_page_names 必须用同一份判据，否则长名会和条目错位：
 * 列表靠第 n 个游戏去取第 n 个长名，两边少算一个就整页名字都对不上。*/
uint8_t entry_kind(const uint8_t *e)
{
    uint8_t attr = e[11];

    if (attr & 0x08) return 0;           /* 卷标 */
    if (attr & 0x06) return 0;           /* 隐藏 / 系统 */
    if (attr & 0x10)                     /* 目录 */
        return (e[0] == '.') ? 0 : 2;    /* 跳过目录自带的 "." 和 ".." */
    if (!is_rom(e)) return 0;
    return ld32(e + 28) ? 1 : 0;         /* 0 字节的 .gb 不是 ROM，别当地址用 */
}

/* 簇链的下一个簇。0 或 >0x0FFFFFF7 表示链结束/坏链。
 * fnt.c 展开字库文件的簇表时用。*/

/* 给定簇号，返回它在 FAT 表扇区内的字节偏移，并填该扇区 LBA。
 * 展开长簇链时用：一次读一整个 FAT 扇区，同一扇区里的簇不必反复读。
 * EZJR.FNT 有约 41 个簇，原来要 41 次 SD 读，用这个接口只要 1-2 次。*/
uint16_t fat_fat_offset(uint32_t c, uint32_t *fs)
{
    if (fis_fat32) {
        *fs = ffat_base + (c >> 7);          /* 128 项/扇区 × 4 字节 */
        return (uint16_t)((c & 0x7F) * 4);
    }
    *fs = ffat_base + (c >> 8);              /* 256 项/扇区 × 2 字节 */
    return (uint16_t)((c & 0xFF) * 2);
}


/* 按 8.3 短名（11 字节，空格补齐，全大写）在根目录里找一个文件。
 * 找到返回 1，并填首簇与大小。字库文件 EZJR.FNT 就是这么定位的。
 * 字库固定放根目录 —— 进不进子目录都得能读到，否则中文名全变问号。*/
uint8_t fat_find(const char *name11, uint32_t *cluster, uint32_t *size)
{
    uint8_t  secs, i;
    uint32_t chain[32];

    if (!fmount_ok)
        return 0;
    secs = dir_chain(0, chain, 32);

    for (i = 0; i < secs; i++) {
        uint16_t k;
        sd_read(chain[i], fbuf);
        for (k = 0; k < 512; k += 32) {
            const uint8_t *e = fbuf + k;
            uint8_t j, same = 1;
            if (e[0] == 0) return 0;
            if (e[0] == 0xE5) continue;
            if (e[11] & 0x18) continue;
            for (j = 0; j < 11; j++)
                if (e[j] != (uint8_t)name11[j]) { same = 0; break; }
            if (!same) continue;
            *cluster = (uint32_t)ld16(e + 26) | ((uint32_t)ld16(e + 20) << 16);
            *size = ld32(e + 28);
            return 1;
        }
    }
    return 0;
}

/* ---- VFAT 长文件名（中文名）----
 *
 * 卡上的 "口袋妖怪.gbc" 在目录里存成两样东西：一条 ASCII 8.3 短名（比如
 * "KOUDAI~1.GBC"，给不认 LFN 的系统用）和几条 0x0F 属性的长文件名项
 * （UTF-16LE，13 字符一条，按序号**倒序**排在短名项前面）。
 *
 * 这里最多取 3 段（39 个字符），缓冲截到 FAT_LFN_CHARS-1 = 27 个。
 * 屏幕从第 2 列起画，一行 20 列，所以 18 个字符以外的部分画的时候会被
 * cjk_puts_ucs2() 截掉 —— 多存几个字符不影响显示，只是不浪费判断。
 * 存长一点是为了写 .SAV 的长名目录项时能带上完整主体。*/
#define LFN_CHARS FAT_LFN_CHARS

static void lfn_take(const uint8_t *e, uint16_t *buf)
{
    uint8_t seq = (uint8_t)(e[0] & 0x1F);
    uint8_t base, j;

    if (seq == 0 || seq > 3)            /* 只认前三段（39 字符，缓冲截到 27）*/
        return;
    base = (uint8_t)((seq - 1) * 13);
    for (j = 0; j < 5; j++)             /* 项内 0-4 字符 */
        if (base + j < LFN_CHARS - 1)
            buf[base + j] = ld16(e + 1 + j * 2);
    for (j = 0; j < 6; j++)             /* 5-10 */
        if (base + 5 + j < LFN_CHARS - 1)
            buf[base + 5 + j] = ld16(e + 14 + j * 2);
    for (j = 0; j < 2; j++)             /* 11-12 */
        if (base + 11 + j < LFN_CHARS - 1)
            buf[base + 11 + j] = ld16(e + 28 + j * 2);
}



/* 8.3 名的升序比较：a 排在 b 前面返回 1。
 *
 * 排序键用**短名**而不是长名：长名只有屏幕上那一页的十几项在内存里，别的项
 * 根本没有；而排序键必须对每一项都可得 —— 短名每条目录项都带。
 * 中文 ROM 的短名是 "K?????~1.GBC" 这种：前缀相同、只差末尾序号，排序后仍然
 * 保持卡上的先后次序，不会来回跳。*/
static uint8_t name_less(const char *a, const char *b)
{
    uint8_t i = 0;

    while (a[i] && a[i] == b[i])
        i++;
    return (uint8_t)((uint8_t)a[i] < (uint8_t)b[i]);
}

/* 把 list[from .. from+n) 原地按名字升序排好。
 * 插入排序：n 最多 64，一次扫描只跑一回，比快排省代码也更省栈；
 * 而且是稳定的 —— 同名的两项保持卡上的相对顺序。*/
static void sort_range(GameEntry *list, uint8_t from, uint8_t n)
{
    uint8_t   i, j;
    GameEntry t;

    for (i = 1; i < n; i++) {
        t = list[from + i];
        j = i;
        while (j > 0 && name_less(t.name, list[from + j - 1].name)) {
            list[from + j] = list[from + j - 1];
            j--;
        }
        list[from + j] = t;
    }
}

uint8_t fat_scan_games(GameEntry *list, uint8_t max)
{
    uint8_t  count = 0, n_dir = 0, base = 0, secs, ci, j;
    uint16_t i;
    /* 目录扇区表。FAT16 的根目录是 32 个连续扇区（512 项），必须全放下 ——
     * 之前只写 16 项却按 froot_sectors=32 去遍历 chain[16..31]，越界读到栈上
     * 的其它变量当 LBA 用，轻则漏掉后半截目录，重则把随机扇区当成目录项
     * 收进列表（造出一个簇号是垃圾的"游戏"，启动它装的就是别处的数据）。*/
    uint32_t chain[32];
    GameEntry ent;

    secs = dir_chain(fat_cwd, chain, 32);

    /* 不在根目录时，第 0 项固定是「返回上级」。做成一个真实条目而不是
     * 单独的按键，上下键、翻页、计数就都不用为它特判。*/
    if (fat_cwd && max) {
        list[0].name[0] = '.';
        list[0].name[1] = '.';
        list[0].name[2] = 0;
        list[0].cluster = 0;
        list[0].size = ENTRY_UP_SIZE;
        count = 1;
        base = 1;
    }

    for (ci = 0; ci < secs && count < max; ci++) {
        sd_read(chain[ci], fbuf);
        for (i = 0; i < 512 && count < max; i += 32) {
            const uint8_t *e = fbuf + i;
            uint8_t kind;
            uint32_t cl;

            if (e[0] == 0) goto done;            /* 目录结束 */
            if (e[0] == 0xE5) continue;
            if ((e[11] & 0x3F) == 0x0F) continue;   /* LFN 项：长名另取 */
            kind = entry_kind(e);
            if (!kind) continue;

            cl = (uint32_t)ld16(e + 26) | ((uint32_t)ld16(e + 20) << 16);
            /* 子目录首簇 < 2 是坏项（有的工具会建出首簇为 0 的空壳目录）。
             * 放它进来只会在按 A 时进到一个空列表里，不如直接不列。*/
            if (kind == 2 && cl < 2) continue;

            format_name(e, ent.name);
            ent.cluster = cl;
            /* 目录的 size 记 0（FAT 里本来也是 0），列表靠它判断「这是文件夹」 */
            ent.size = (kind == 2) ? 0UL : ld32(e + 28);

            /* 先分组：目录全部挪到文件前面（磁盘上常常混着放），排序在后。*/
            if (kind == 2) {
                for (j = count; j > (uint8_t)(base + n_dir); j--)
                    list[j] = list[j - 1];
                list[base + n_dir] = ent;
                n_dir++;
            } else {
                list[count] = ent;
            }
            count++;
        }
    }
done:
    /* 两组各自按文件名升序。用户要的是"按名字排"，不是"按卡上的拷贝顺序排"
     * —— 卡是陆续往里拷的，不排序时找一个游戏得从头扫到尾。*/
    sort_range(list, base, n_dir);
    sort_range(list, (uint8_t)(base + n_dir), (uint8_t)(count - base - n_dir));
    return count;
}

/* name83 与目录项原始 11 字节是不是同一个文件。不建临时数组：每项都要比，
 * 而一页 13 个目标名 × 512 个目录项 = 六千多次，省下那点栈比什么都值。*/
static uint8_t same_name(const char *n, const uint8_t *e)
{
    uint8_t i, j = 0;

    for (i = 0; i < 8; i++) {
        uint8_t c = ' ';
        if (n[j] && n[j] != '.') { c = (uint8_t)n[j]; j++; }
        if (c != e[i])
            return 0;
    }
    while (n[j] && n[j] != '.') j++;
    if (n[j] == '.') j++;
    /* ★ 这里的兜底必须是**空格**，不能是 0。
     * 目录项里没有扩展名时那 3 字节是空格（$20），而 name 里没有点：
     *   - 文件夹（名字形如 "GBC193~1"，后面什么都没有）
     *   - 两字符扩展名的 ROM（"TETRIS.GB"）
     * 两者走到这里都拿不到字符。v0.12.1 用 0 兜底 → 与 $20 永远不等 →
     * 于是**所有文件夹和 .GB 游戏都配不上长名**，屏幕上退回 8.3 短名，
     * 只有正好 3 字符扩展名（.GBC）的项能显示中文。
     * tools/fat_host_test.py 当时只给文件造了长名，目录没造，所以没抓住。*/
    for (i = 8; i < 11; i++) {
        uint8_t c = ' ';
        if (n[j]) { c = (uint8_t)n[j]; j++; }
        if (c != e[i])
            return 0;
    }
    return 1;
}

void fat_fill_lfn(const GameEntry *list, uint8_t start, uint8_t rows,
                  uint16_t *out)
{
    uint8_t  secs, i, j, have = 0;
    uint16_t k;
    uint32_t chain[32];
    uint16_t lfn[LFN_CHARS];

    memset(out, 0, (uint16_t)rows * LFN_CHARS * 2);
    if (!fmount_ok || !rows)
        return;

    secs = dir_chain(fat_cwd, chain, 32);
    for (i = 0; i < secs; i++) {
        sd_read(chain[i], fbuf);
        for (k = 0; k < 512; k += 32) {
            const uint8_t *e = fbuf + k;

            if (e[0] == 0)
                return;
            if (e[0] == 0xE5) { have = 0; continue; }
            if ((e[11] & 0x3F) == 0x0F) {        /* LFN 项：倒序排在短名前面 */
                if (!have) {
                    memset(lfn, 0, sizeof lfn);
                    have = 1;
                }
                lfn_take(e, lfn);
                continue;
            }
            if (!entry_kind(e)) { have = 0; continue; }
            if (have) {
                /* 首字节不同的直接跳过 —— 六千多次比较里九成九在这里出局 */
                for (j = 0; j < rows; j++) {
                    if (list[start + j].name[0] != (char)e[0])
                        continue;
                    if (same_name(list[start + j].name, e))
                        memcpy(out + (uint16_t)j * LFN_CHARS,
                               lfn, LFN_CHARS * 2);
                }
            }
            have = 0;
        }
    }
}

