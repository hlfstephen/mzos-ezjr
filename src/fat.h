/* fat.h — 极简 FAT16/FAT32 根目录只读层 */
#ifndef FAT_H
#define FAT_H

#include <stdint.h>

#define GAME_NAME_MAX 13            /* 8.3 + '.' + NUL = 13 */
#define PAGE_SIZE     12            /* 列表每页显示 12 行 */

typedef struct {
    char    name[GAME_NAME_MAX];    /* "POKEMON.GBC"，大写 8.3，0 结尾 */
    uint32_t cluster;               /* 起始簇，供后续「启动游戏」构建加载命令 */
    uint32_t size;                  /* 文件字节数；目录/上级项见下面两个宏 */
} GameEntry;

/* 目录也占一个条目（这样上下键、翻页、计数全都复用同一套逻辑），
 * 但不给 GameEntry 加字段 —— 加一个 uint8_t 会被对齐成 4 字节，
 * 64 条就多 256 字节 WRAM，而 _DATA 只剩不到 200 字节。
 *
 * 借用 size 编码：FAT 里目录项的 size 恒为 0，而 ROM 不可能是 0 字节，
 * 两边不会撞车。0xFFFFFFFF 这个大小对 8MB 以内的 ROM 也不可能出现。*/
#define ENTRY_IS_DIR(g)   ((g)->size == 0UL)
#define ENTRY_IS_UP(g)    ((g)->size == 0xFFFFFFFFUL)
#define ENTRY_UP_SIZE     0xFFFFFFFFUL

/* ---- 目录浏览 ----
 * 根目录用 0 表示，子目录用首簇表示。fat_cwd 是所有扫描与读写路径共用的
 * "当前目录"，这样 fat_scan_games / fat_page_names / fat_open_sav 都不用加参数
 * （SDCC 传参很贵，而这几处都在开机关键路径上）。*/
extern uint32_t fat_cwd;
extern uint8_t  fat_cwd_depth;

#define FAT_DIR_DEPTH 4             /* 最多进 4 层，够 GBC/中文名/子目录 这种 */

void fat_dir_reset(void);           /* 回到根目录 */
uint8_t fat_dir_enter(uint32_t cluster);  /* 进入子目录，超出深度返回 0 */
uint8_t fat_dir_up(void);           /* 返回上级，已在根目录返回 0 */

/* 挂载结果摘要，供真机自检屏显示（src/diag.c）。真机上 SD 读不通时
 * 只能靠这些数字判断卡在哪一步，所以单独暴露出来。*/
typedef struct {
    uint8_t  mounted;               /* 1 = fat_mount() 返回 0 */
    uint8_t  is_fat32;              /* 0 = FAT16，1 = FAT32 */
    uint8_t  sectors_per_cluster;
    uint32_t part_lba;              /* MBR 分区起始 LBA，superfloppy 为 0 */
    uint32_t cluster_count;
} FatInfo;

void fat_info(FatInfo *out);

/* 挂载：解析 MBR/BPB，定位 FAT、根目录。返回 0 成功，否则 FAT_ERR_* */
uint8_t fat_mount(void);

/* 扫描【当前目录】填 list，最多 max 条，返回条数（含下面说的「..」项）。
 *
 * 列入：`.GB/.GBC` 文件、子目录。子目录的 size 记 0，用 ENTRY_IS_DIR() 判断；
 * 不在根目录时第 0 项固定是一条「..」（size = ENTRY_UP_SIZE，用 ENTRY_IS_UP()
 * 判断），按它就能回上级。只扫一层，不递归。
 *
 * 顺序固定是「.. → 所有目录 → 所有文件」，**两组各自按文件名升序**。*/
uint8_t fat_scan_games(GameEntry *list, uint8_t max);

/* 取长名：按 list[start .. start+rows) 这 rows 项的**短名**回目录里配对，
 * 命中就把那一条的 VFAT 长名写进 out（每项 FAT_LFN_CHARS 个 UCS-2），
 * 没有长名的项保持全 0（显示时退回短名）。
 *
 * 为什么列表和长名分两步：列表要排序，排完序磁盘顺序和列表顺序就对不上了，
 * 扫描时顺手填的长名会整体错位 —— 屏幕上显示 A 的名字、按 A 启动的却是 B。
 * 这里按「短名」配对，顺序怎么变都不会错位。*/
void fat_fill_lfn(const GameEntry *list, uint8_t start, uint8_t rows,
                  uint16_t *out);

enum {
    FAT_OK = 0,
    FAT_ERR_NO_SIG,     /* 无 55AA 签名 / 非 512 字节扇区 */
    FAT_ERR_FAT12,      /* FAT12 不支持 */
    FAT_ERR_GPT,        /* GPT 保护分区，请用 MBR */
    FAT_ERR_NO_FS,      /* 结构对但找不到 FAT 引导扇区（分区为空/不是 FAT）*/
    FAT_ERR_FRAGMENTED, /* 文件碎片超过 61 段，加载命令表装不下 */
    FAT_ERR_BAD_CHAIN,  /* 簇链成环 / 簇号越界 */
    FAT_ERR_ADD_DIR,    /* 根目录项写不进去（目录满 / 重扫失败）*/
    FAT_ERR_NO_SPACE,   /* FAT 上没有空簇可分配 */
    FAT_ERR_VERIFY,     /* 写完回读，卡上的数据与内存里不一致 */
};

/* 簇号 -> 首扇区 LBA */
uint32_t fat_cluster_lba(uint32_t cluster);

/* 簇号 c 在 FAT 扇区内的字节偏移，并填该扇区 LBA。展开长簇链时用来
 * 一次读一整个 FAT 扇区，避免每簇读一次。*/
uint16_t fat_fat_offset(uint32_t c, uint32_t *fs);

/* 按 8.3 短名（11 字节，空格补齐、全大写）在根目录找文件。
 * 找到返回 1 并填首簇与大小 —— 字库文件 EZJR.FNT 就是这么定位的。*/
uint8_t fat_find(const char *name11, uint32_t *cluster, uint32_t *size);

/* ---- 中文文件名（VFAT 长文件名）----
 * 8.3 短名只能是 ASCII，卡上的中文名存在 0x0F 属性的 LFN 项里（UTF-16LE）。
 * 取第 start 个游戏起 count 个的长名，写进 out（每项 16 个 UCS-2，0 结尾）。
 * 没长名的项保持全 0，调用者退回显示短名。*/
#define FAT_LFN_CHARS 28               /* 列表/菜单显示用的长名缓冲宽度 */

/* 存档长名用到的最大长度（UCS-2 字符数）。存档文件名必须跟 ROM 文件名逐字
 * 相同（只换扩展名），中文 ROM 名常有三四十个字符，所以给到 48（覆盖 40 字符的
 * 游戏名 + 一点余量；4 条 LFN 项够装）。
 * 注意：sm83 的 `add sp, #n` 只能 ±127，所以这么大的缓冲不能放栈上 —— 用
 * g_lfn_buf 这一个全局 scratch 复用，不要在任何函数里开局部大数组。*/
#define FAT_LFN_FULL  48

/* 长名 scratch：fat_full_lfn（回扫目录取 ROM 完整长名）和 fat_open_sav（生成
 * .sav 长名）共用这一份，避免任何一个函数背着几十上百字节的栈帧（sm83 装不下）。*/
extern uint16_t g_lfn_buf[FAT_LFN_FULL];

/* 存档身份：ROM 名 → 8.3 短名 "S1A2B3C4.SAV"（见 src/savkey.c）。
 *
 * 为什么需要：中文 ROM 在卡上的 8.3 名是 GBK 硬截断，不同游戏会算出同一个
 * 名字，存档互相覆盖。这里用完整长名算 28 位哈希，纯函数、零 I/O、编在
 * 常驻区。lfn 为空时退回按 8.3 名派生，读写两侧仍然一致。
 *
 * 输出是点分字符串，直接交给 fat_open_sav / fat_open_read 即可 —— 它们内部
 * 的 sav_name() 会截到点并补 "SAV" 扩展名，正好还原成 11 字节原始项。*/
void sav_ident(const char *name83, const uint16_t *lfn, char *out);

/* 构建 512 字节加载命令（格式见 docs/06-launch-plan.md §4.1）。
 * cmd 由调用者提供，需 512 字节可写空间。返回 FAT_OK 或错误码。*/
uint8_t fat_build_cmd(uint32_t cluster, uint32_t size, uint8_t *cmd);

/* ---- 写路径（存档 .SAV）----
 * 顺序写句柄：open 之后每次 write_sec 写 512 字节，簇内写满自动沿簇链走。*/
typedef struct {
    uint32_t cluster;               /* 当前簇 */
    uint16_t sec_in;                /* 当前簇内已写到第几扇区 */
} FatFile;

/* 打开（必要时创建）根目录 SAVER 里的 `<ROM名>.sav`，准备整文件覆写 size
 * 字节。大小与已有文件相同就复用原簇链，否则回收旧链重新分配。
 *
 * 存档位置不看 fat_cwd：内部自己切到根目录唯一的 SAVER，出口把 fat_cwd 还原。
 * 读存档（fat_open_read）找不到 SAVER 时会回落到调用时的当前目录，兼容老 .SAV。*/
/* 新建 .SAV 时顺便写 VFAT 长文件名项，让卡上显示中文全名。
 * lfn 是 ROM 的完整长名（UCS-2，0 结尾），可传 NULL / 空串表示只写短名。
 * 读取仍按 8.3 短名定位，所以不影响已有文件。*/
uint8_t fat_open_sav(FatFile *f, const char *rom_name, uint32_t size,
                     const uint16_t *lfn);

/* 顺序写一个 512 字节扇区。返回 0 成功，非 0 表示簇链在中途断了。*/
uint8_t fat_write_sec(FatFile *f, const uint8_t *src);

/* 打开已有的 `<ROM名>.SAV` 准备顺序读；不存在返回 FAT_ERR_NO_FS。*/
uint8_t fat_open_read(FatFile *f, const char *rom_name, uint32_t *size);

/* 把 ROM 长名换成存档长名：「口袋妖怪 水晶.gbc」→「口袋妖怪 水晶.sav」，
 * 返回字符数。rom 与 out 可以**指向同一个缓冲**（逐字节原地改写，调用点靠这个
 * 省掉一份 96 字节的暂存）。末位扩展名换成 .sav，主体一个字都不截断。*/
uint8_t fat_sav_lfn(const uint16_t *rom, uint16_t *out);

/* 按**长名**找存档（src/fatn.c，编在 _CODE）。卡上从电脑拷回 SAVER/ 的 .sav，
 * 长名改过之后短名是系统现生成的（S1A2~1.SAV 之类），跟 sav_ident() 的哈希名
 * 对不上 —— 这时只能比长名。只扫根目录的 SAVER/，一处。比较范围是名字**开头 13 个字符**
 * （LFN 项里序号为 1 的那条），名字更长时靠调用方的「大小必须是 banks×8KB」兜底。
 *
 * ★ 名字**必须已经在 g_lfn_buf 里**（fat_full_lfn 刚刚填过），函数会就地把它改成
 *   「….sav」——省一份 96 字节缓冲，_DATA 已经没地方了。所以调用方必须先用原名
 *   算完 sav_ident 再进来。命中后填好 f（已定位到首簇），并把它的 8.3 短名写进
 *   g_sav_adopt（备份写回用）。返回 FAT_OK / FAT_ERR_NO_FS。*/
uint8_t fat_find_sav_lfn(FatFile *f);

/* 在 dir（0 = 根目录）里按完整 8.3 名 name83 找一个 ROM，把它的**完整**长名
 * 读进 out（最多 max-1 个 UCS-2 + 0），返回字符数；没找到 / 没长名返回 0。
 * 备份存档时用：存档长名必须跟 ROM 长名逐字相同（只换 .sav 扩展名），卡上
 * 的加载器才按文件名配对。出口还原 fat_cwd，不影响调用方。*/
uint8_t fat_full_lfn(uint32_t dir, const char *name83, uint16_t *out);

/* 把目录里「中间 $00 + 后面还有有效项」改成 $E5 —— 修 v0.12.x 把终止符
 * 写进目录中间造成的历史截断。只改 $00→$E5，不动任何有效项，无断电丢档
 * 风险；正常目录零写入。返回改动的槽数。实现在 src/fatx.c（_CODE）。*/
uint8_t fat_dir_heal(uint32_t dir);

/* ---- 内部：目录遍历原语（src/fat.c，编在 BANK1）----
 * 只有 fat_full_lfn（src/fatn.c，编在 _CODE）要用。导出而不复制，是为了让
 * 「什么算一条目录项」「目录扇区怎么排」两块逻辑全工程只有一份 —— 列表、翻页、
 * 取长名三处一旦判据不一致，长名就会整页串位。*/
uint8_t dir_chain(uint32_t dir, uint32_t *chain, uint8_t cap);
uint8_t entry_kind(const uint8_t *e);
extern uint8_t fbuf[512];
extern uint8_t fmount_ok;

/* 顺序读一个扇区。返回 1 = 后面还有数据，0 = 已到文件末尾。*/
uint8_t fat_read_sec(FatFile *f, uint8_t *dst);

/* ---- 目录与文件管理（src/fatx.c，编在 _CODE）----
 * 建子目录：名字是 11 字节 8.3 原始格式（空格补齐）。已存在返回原簇号，
 * 不存在就分配一个簇、写 "."/".."、在父目录里落下目录项。返回 0 表示失败
 * （目录满 / 没有空簇）。存档用的 SAVER 目录就是这么建出来的。*/
uint32_t fat_mkdir(const uint8_t *raw11);

/* 在当前目录里找一个子目录（11 字节 8.3 名），返回首簇；没有返回 0。
 * 与 fat_find() 的区别：那个只找文件，这个只找目录。*/
uint32_t fat_find_dir(const uint8_t *raw11);

/* 删掉**当前目录**里的一个 ROM（名字是 "POKEMON.GBC" 这种 8.3 形式）。
 * 目录项标成已删除、配对的 VFAT 长名项一并清掉、簇链回收。
 * 不删目录 —— 目录项属性是 0x10，本函数根本不看它。*/
uint8_t fat_delete(const char *rom_name);

/* 在**指定目录簇**（0 = 根目录）里按完整 8.3 名（"POKEMON.GBC"）定位一个
 * ROM，填一条 GameEntry；找不到返回 0。
 * START 菜单「运行上次游戏」专用：只临时借用 fat_cwd 查一下，出口还原，
 * 不重扫列表、不改光标 —— 按 START 前后看到的是同一个列表。*/
uint8_t fat_locate(uint32_t dir, const char *name83, GameEntry *out);

#endif /* FAT_H */
