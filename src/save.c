/* save.c — 存档：卡带 RAM ↔ SD 卡上的同名 .SAV
 *
 * 为什么是这个形状（而非"游戏里按组合键退出"）：
 *   EZ-Flash Junior 进游戏后 `$7Fxx` 命令寄存器就被 MBC 顶掉了，游戏里
 *   没有任何办法回到内核，只能断电重启。所以存档只能做成「延迟备份」：
 *
 *     启动前  PSRAM 页 $00-$0F(存档区) ← .SAV，并在元数据页 $11 盖章
 *     游戏里  游戏自己读写 $A000-$BFFF，落到的就是同一片电池供电 PSRAM
 *     下次开机 元数据页 $A000 还是 $AA → 把 PSRAM 写回 .SAV → 清标志
 *
 *   这和官方内核的 BACKUPSAVE 是同一套机制（docs/psram-save-map.md）：
 *   盖章按「每次启动」而不是「存档有没有变」，所以没存档也会提示一次，
 *   代价只是多写一遍同样的数据。
 *
 * ★★ 窗口互斥（v0.3.1 修的就是这里）：
 *   `$7FC0 = 3` 把窗口交给 PSRAM，而 SD 的扇区窗口要求 `$7FC0` 的
 *   bit0/bit2 为 0 —— 两者**不能同时**有效（RTL sd_mem_path.v：
 *   `win_sel = !win_mode[0] & !win_mode[2]`）。所以每一块 512 字节都必须
 *
 *       psram_map(page) → memcpy 出/入 WINDOW → psram_unmap() → SD 操作
 *
 *   少了那个 psram_unmap()，往 WINDOW 写的 512 字节会落进 PSRAM，SD 卡收到的
 *   是控制器缓冲区里的旧内容（v0.3.0 就是这样把根目录扇区写成了 .SAV）。
 *   上游 `stage1/src/backup.c` 的循环也是这个顺序。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"
#include "save.h"
#include "video.h"
#include "strings.h"
#include "game.h"

/* game.c 的 512 字节缓冲。菜单态和启动是互斥的（启动不返回），共用一块
 * 可以把 WRAM 稳住 —— 单独再开 512 会顶到 $D000 的 handoff 区。*/
extern uint8_t game_work[512];

#define SAVE_CHUNK 16                   /* 8KB / 512 */
#define SAVE_FOOTER 48                  /* 官方内核给带时钟的卡在 .SAV 后追加的字节数 */

/* 元数据页 $11 的字段（$000-$01F 与官方内核一致，见 docs/psram-save-map.md）*/
#define META_FLAG  0x000                /* $AA = 有待备份 */
#define META_BANKS 0x001                /* 要 dump 的 8KB 块数 */
#define META_LEN   0x00F                /* 文件名长度 */
#define META_NAME  0x010                /* 文件名（ASCII，无路径）*/
/* ---- 本内核自用：上一次启动的是哪个 ROM ----
 *
 * 一条 18 字节的小记录，写**两份**，每份带一个异或校验：
 *
 *   [0..12]  完整 8.3 名 + 0（"POKEMON.GBC"）
 *   [13..16] 所在目录的首簇（0 = 根目录）
 *   [17]     校验 = $5A ^ 前 17 字节
 *
 * ★ 位置是 v0.12.4 才搞对的。元数据页（page $11）里官方**真正写过**的偏移只有：
 *
 *     $A000/$A001/$A00F/$A010+   待备份章、存档大小、存档路径（每次启动写）
 *     $A200/$A201                自动备份开关、电池哨兵
 *     $A300-$A3FE                上次运行的 ROM 完整路径，255 字节（每次启动写）
 *     $A400+                     7 KB 空闲，但其中 $A410-$A42F 被 stage1 和各种
 *                                mod 占了，官方文档也只说是"代码扫描出来的空闲"，
 *                                没在硬件上验过
 *
 * $A110-$A2FF 这一段，官方内核一个字节都没写过。而 v0.11.0 的散字段
 * （$130/$170/$171/$180/$1F0）和 v0.12.0-v0.12.3 的 $1D0/$1E8 **全在这段里**，
 * 四版四套格式，真机上一律读不回来 —— 现在看，位置本身就是原因。
 *
 * 真机证据（v0.12.3 自检屏）：先从 dqm1 启动，RAW=68,81 = 'D','Q'（短名开头
 * 对得严丝合缝）；再换「K 口袋妖怪」启动，RAW=75,91 = 'K',…，第一个字节跟着
 * 变了。说明写侧真的执行了、名字真的写进去了，只是这段留不住。
 *
 * 所以改用 $A300：官方 last-ROM 记录的位置，每次启动都写、每次按 START 都读
 * 得回来，是这块 PSRAM 上唯一被反复验证过的自留地。两份记录占 $A300-$A323，
 * 都在官方那 255 字节里（我们不用官方的路径格式，覆盖它无所谓）。
 *
 * 另一条老问题也一并解决：这份记录必须能自证。擦除态 $FF 与真值分不开、被
 * 覆盖一个字节就整组作废 —— 所以每条自带异或校验，坏一条还有另一条。
 *
 * 长名**不再存**：开机用 fat_full_lfn() 按 8.3 名回目录现查，卡上那份名字
 * 永远是最新的，也不受截断之害。落盘的只有定位用的完整 8.3 名。*/
#define META_REC   0x300
#define META_REC2  0x320
#define REC_BYTES  18
#define REC_XOR    0x5A

/* 上次启动的 ROM 的名字与所在目录。列表页的 START 菜单要显示
 * 「上次运行的游戏」并能一键启动它，所以这几个值要活到菜单关闭为止 ——
 * save_boot() 里读出来就存这儿，别放在栈上。
 *
 * boot_name 是 8 字符基本名（显示用），boot_full 是完整 8.3 名（定位用）。*/
static char     boot_name[10];
static char     boot_full[GAME_NAME_MAX];
static uint32_t boot_dir_saved;

/* 官方内核的电池哨兵：元数据页 $11 的 $A201，值 $88 = 「这颗纽扣电池在上次
 * 关机期间供住了电」，PSRAM 里的存档是真的。任何别的值（通常是 $FF，或者
 * 电压不够时的随机衰减值）就是官方屏幕上的 BATTERY DRY。
 *
 * 地址和值都与官方内核完全一致（`BatteryCheck`, `00:1835`；`docs/psram-save-map.md`
 * 的「$A201 | $88 = cart initialized, else "Battery dry"」），两边互相认账：
 * 从官方固件升级上来读到 $88 就信任芯片里的存档，刷回官方固件它也认我们盖的。
 *
 * 为什么不看 PCF8563 的 VL 位：官方内核明确不看 RTC 芯片的电压标志（同一份
 * 文档：「It never reads the RTC chip's voltage-low flag」）。而且 RTC 与 PSRAM
 * 虽然共用一颗电池，VL 只在电压跌破阈值那一刻置位并保持 —— 电池耗尽后回光
 * 一次，VL 可能已经被时钟设置流程清成 0，而 PSRAM 里的数据早就是垃圾了。*/
#define META_CANARY 0x201
#define CANARY_OK   0x88

/* ---- 存档指纹：这次开机时芯片里的内容，和上次备份出去的是不是同一份 ----
 *
 * 为什么需要这个：盖章发生在「启动游戏」的时候，备份发生在「下次开机」的
 * 时候。如果这中间玩家根本没在游戏里存过档，备份就等于把卡上那份一模一样
 * 的文件再写一遍 —— 既磨损 SD 卡，更要紧的是让「把电脑上的备份拷到卡上」
 * 这个动作失效：一开机就被芯片里的旧内容盖回去，想恢复都恢复不了。
 *
 * Adler 结构：s1 累加字节，s2 累加 s1，合起来 24 位。比简单求和能抓住字节
 * 换位（存档变动最容易出现的形态之一），又比 CRC16 便宜一半。
 *
 * 只在两处算：备份前算一次用来比对、备份成功后把新值存下来。
 * **启动游戏时不算** —— 那条路径本来就要读 64 个扇区，再叠 0.4 秒会让
 * 「进游戏」明显变慢，而它算了也用不上（指纹只在备份时才更新）。*/
#define META_SUM  0x110                 /* 3 字节指纹 + 1 字节有效标记 */
#define SUM_MARK  0x5A

/* 自动备份开关：$120/$121 两个字节，都等于 AUTO_ON 才算"自动"。
 *
 * 默认是**手动**：PSRAM 掉电（电池没电）后这两个字节会衰减成随机值或 $FF，
 * 而这不是 $5A/$5A，于是自动落回"手动"。这是安全方向 —— 最多多问一句，
 * 绝不会擅自把卡上那份好存档覆盖掉。
 *
 * 为什么用两个字节：单字节时随机值恰好等于 $5A 的概率是 1/256，误判成
 * "自动"就会不问自取地写卡；两个字节降到 1/65536。这一字节的代价很便宜。*/
#define META_AUTO 0x120
#define AUTO_ON   0x5A
#define AUTO_OFF  0x5B

/* 这次上电周期里 PSRAM 的内容可不可信。0 = 可信（电池还在，或本次开机
 * 已经重新盖过章），1 = BATTERY DRY。
 *
 * 由 save_boot() 在开机第一件事里设，save_prepare() 读。所以 save_boot()
 * 必须先跑 —— main() 里就是这么排的（备份在扫目录之前）。*/
static uint8_t batt_dry;

#if defined(EZJR_DIAG) && EZJR_DIAG
/* 上次开机读到的 META_BANKS。0 表示上次启动的那个 ROM 报了"没有存档 RAM"。
 * 自检屏用它区分「记录没写」和「根本不该写」—— 只在 --diag 版存在。*/
static uint8_t boot_banks;

/* save_arm() 写完记录、unmap 再 map 读回来的结果（任一份读得回就算 1）。
 * 和 REC 是两件事：REC 测「跨断电还在」，这个测「当场落地」。两者不一致
 * 就说明写入没落地 —— 那才是 v0.12.0-v0.12.3 一直卡着的那个病。*/
static uint8_t rec_ok;
#endif

/* WINDOW 已经 map 到元数据页时用这个判定（save_boot 里就在 map 内），
 * 免得重复 map/unmap。两个字节都要对上。*/
static uint8_t auto_on_locked(void)
{
    return (uint8_t)(WINDOW[META_AUTO] == AUTO_ON &&
                     WINDOW[META_AUTO + 1] == AUTO_ON);
}

uint8_t save_auto_get(void)
{
    uint8_t on;

    psram_map(PSRAM_META);
    on = auto_on_locked();
    psram_unmap();
    return on;
}

void save_auto_set(uint8_t on)
{
    psram_map(PSRAM_META);
    WINDOW[META_AUTO]     = on ? AUTO_ON : AUTO_OFF;
    WINDOW[META_AUTO + 1] = on ? AUTO_ON : AUTO_OFF;
    psram_unmap();
}

/* 手动模式下问一句：A = 备份，B = 跳过。返回 1 表示玩家按了 A。*/
static uint8_t ask_backup(void)
{
    uint8_t k;

    for (;;) {
        k = buttons();
        if (k & (BTN_A | BTN_B))
            break;
        wait_frames(1);
    }
    wait_release();
    wait_frames(3);
    return (uint8_t)((k & BTN_A) != 0);
}

/* 存档区的指纹（banks × 8KB）*/
static uint32_t save_sum(uint8_t banks)
{
    uint8_t  s1 = 0, p, i;
    uint16_t s2 = 0, k;

    for (p = 0; p < banks; p++) {
        psram_map((uint8_t)(PSRAM_SAVE_LO + p));
        for (i = 0; i < SAVE_CHUNK; i++) {
            const uint8_t *w = (const void *)(WINDOW + ((uint16_t)i << 9));
            for (k = 0; k < 512; k++) {
                s1 = (uint8_t)(s1 + w[k]);
                s2 = (uint16_t)(s2 + s1);
            }
        }
        psram_unmap();
    }
    return ((uint32_t)s2 << 8) | s1;
}

static void sum_store(uint32_t v)
{
    psram_map(PSRAM_META);
    WINDOW[META_SUM]     = (uint8_t)v;
    WINDOW[META_SUM + 1] = (uint8_t)(v >> 8);
    WINDOW[META_SUM + 2] = (uint8_t)(v >> 16);
    WINDOW[META_SUM + 3] = SUM_MARK;
    psram_unmap();
}

uint8_t save_banks_of(uint8_t code)
{
    if (code == 3) return 4;            /* 32KB */
    if (code == 5) return 8;            /* 64KB */
    if (code == 4) return 16;           /* 128KB */
    if (code == 1 || code == 2) return 1;   /* 8KB（2KB 的也按 8KB 处理）*/
    return 0;
}

/* 写一条「上次启动的 ROM」记录（调用方已经 psram_map 过）。
 *
 * ★ 校验值必须**用入参算**，不能"写完再从窗口读回来算"。
 *
 * v0.12.2 就是后者：先写 17 个字节，然后 `x ^= WINDOW[at + i]` 再写第 18 字节。
 * 这在主机测试里天衣无缝（窗口就是一块普通 RAM，写后读必准），但真机上
 * $A000 窗口后面是 PSRAM + FPGA 桥：只要写后立刻读有半拍延迟、读回的是旧值，
 * 落盘的就成了「名字对、校验按全 $FF 算出来」的记录 —— 每次开机校验必然不符，
 * 现象与"从来没写过"一模一样（REC=--、备份屏短名）。
 *
 * 用入参算就没有这个依赖：写进去的是什么，校验就是对什么算的。*/
static void rec_put(uint16_t at, const char *name, uint32_t dir)
{
    uint8_t i, c, x = REC_XOR;

    for (i = 0; i < GAME_NAME_MAX - 1; i++) {
        c = (uint8_t)(name[i] ? name[i] : 0);
        WINDOW[at + i] = c;
        x ^= c;
    }
    WINDOW[at + 12] = 0;                /* 名字最多 12 字符，第 13 字节必是 0 */
    for (i = 0; i < 4; i++) {
        c = (uint8_t)(dir >> (i << 3));
        WINDOW[at + 13 + i] = c;
        x ^= c;
    }
    WINDOW[at + 17] = x;
}

/* 读一条记录并验校验和（调用方已经 psram_map 过）。name 至少要
 * GAME_NAME_MAX 字节。返回 1 = 记录有效。*/
static uint8_t rec_get(uint16_t at, char *name, uint32_t *dir)
{
    uint8_t i, x = REC_XOR;

    for (i = 0; i < 17; i++)
        x ^= WINDOW[at + i];
    if (x != WINDOW[at + 17])
        return 0;                       /* 没写过 / 被覆盖 / 擦除态 $FF */
    for (i = 0; i < GAME_NAME_MAX - 1; i++) {
        uint8_t c = WINDOW[at + i];
        if (!c)
            break;
        /* ★ 不能只放行 0x20-0x7E（v0.12.5 修）。
         *
         * 我们一直假设「8.3 短名 = 人可读 ASCII」，真机上不成立：
         * 这张卡上 "K 口袋妖怪 水晶…" 的短名字段是
         *   4B BF DA B4 FC D1 FD B9   =  'K' 口 袋 妖 怪(前半字节)
         * —— 生成短名的工具把 **GBK 字节直接写进了 8.3 字段**，还按字节
         * 截断、把一个汉字砍成半个。7 个字节 > 0x7E，全被这里拒掉，
         * 于是记录明明写进去了（RAW 跨断电还在）、校验也对，却一律判
         * 无效：REC=-- / V=-- / boot_full 空 / 备份屏短名，四版都是这个。
         *
         * 现在只拒两种：控制字符（不可能出现在合法短名里）和 $FF
         * （擦除态，校验本来就过不了，这里是双保险）。0x80-0xFE 原样收，
         * 字节比较的活交给 same_name()（它用 uint8_t）。*/
        if (c < 0x20 || c == 0xFF)
            return 0;
        name[i] = (char)c;
    }
    if (i == 0)
        return 0;
    name[i] = 0;
    *dir = (uint32_t)WINDOW[at + 13] |
           ((uint32_t)WINDOW[at + 14] << 8) |
           ((uint32_t)WINDOW[at + 15] << 16) |
           ((uint32_t)WINDOW[at + 16] << 24);
    return 1;
}

static void save_arm(const char *name, uint8_t banks)
{
    uint8_t i, n = 0;

    while (n < 8 && name[n] && name[n] != '.') n++;
    psram_map(PSRAM_META);
    /* 「要不要盖待备份的章」和「上次运行的是哪个 ROM」是两件事：
     *
     *   盖章   只对带存档 RAM 的卡带有意义（banks = 0 时备份就是写一堆空数据）
     *   记录   跟卡带有没存档**无关** —— 列表页的 START 菜单靠它跳回上次那个
     *          游戏，备份屏靠它显示中文名。没存档的 ROM 也是 ROM。
     *
     * v0.12.2 把两件事写在同一个 if (banks) 里（在 save_prepare 那边），于是
     * 「无存档卡带」永远记不上 —— 玩家启动它、关机、再开机，START 菜单里还是
     * 「无上次游戏」。这里把记录提到 if 外面。*/
    if (banks) {
        for (i = 0; i < 8; i++)
            WINDOW[META_NAME + i] = (uint8_t)(i < n ? name[i] : 0);
        WINDOW[META_LEN]   = n;
        WINDOW[META_BANKS] = banks;
        WINDOW[META_FLAG]  = 0xAA;
    }

    /* 记下「上次启动的是这个 ROM」：完整 8.3 名 + 所在目录簇。目录必须记，
     * 否则 START 菜单的「运行上次游戏」只在根目录下找得着 —— 子目录里的游戏
     * 第二次按 START 就没反应。写两份，坏一份还有另一份。*/
    rec_put(META_REC,  name, fat_cwd);
    rec_put(META_REC2, name, fat_cwd);
    psram_unmap();

#if defined(EZJR_DIAG) && EZJR_DIAG
    /* 写完**先 unmap 再 map**，然后读回来 —— 这才是"落地了吗"的判据。
     * 在同一个 map 里立刻读是测不出来的：那可能只是总线上的值（v0.12.3 的
     * WR=OK 就是这么来的，而记录照样坏）。*/
    {
        char     tmp[GAME_NAME_MAX];
        uint32_t d;

        psram_map(PSRAM_META);
        rec_ok = (uint8_t)(rec_get(META_REC, tmp, &d) ||
                           rec_get(META_REC2, tmp, &d));
        psram_unmap();
    }
#endif
}

/* 0 = 没记过（新卡 / 刚刷机 / 记录被覆盖），1 = 同一个游戏，2 = 别的游戏 */
static uint8_t last_state(const char *name)
{
    char     want[GAME_NAME_MAX];
    uint32_t d;
    uint8_t  i;

    psram_map(PSRAM_META);
    if (!rec_get(META_REC, want, &d) && !rec_get(META_REC2, want, &d)) {
        psram_unmap();
        return 0;
    }
    psram_unmap();
    for (i = 0; i < GAME_NAME_MAX - 1; i++) {
        if (want[i] != name[i])
            return 2;
        if (!name[i])
            return 1;
    }
    return 1;
}

/* PSRAM -> .SAV */
static uint8_t save_dump(uint8_t banks, const char *name, const uint16_t *lfn)
{
    FatFile f;
    uint32_t size = (uint32_t)banks << 13;
    uint8_t err, p, i;

    err = fat_open_sav(&f, name, size, lfn);
    if (err)
        return err;

    for (p = 0; p < banks; p++) {
        for (i = 0; i < SAVE_CHUNK; i++) {
            psram_map((uint8_t)(PSRAM_SAVE_LO + p));
            memcpy(game_work, (const void *)(WINDOW + ((uint16_t)i << 9)), 512);
            psram_unmap();                   /* ★ 必须在 SD 写之前 */
            err = fat_write_sec(&f, game_work);
            if (err)
                return err;
        }
    }
    return FAT_OK;
}

/* 回读校验：把刚写出去的 .SAV 再读一遍，逐扇区与 PSRAM 比对。
 *
 * 为什么值得多花这一遍（64 次读，不到一秒）：v0.3.0 的窗口 bug 就是把根目录
 * 扇区当成存档写进了卡里，而屏幕照样显示 SAVE OK —— 用户以为存好了，下一局
 * 读档才发现没了。有了校验，"存坏了"能当场看见，而不是等下一次启动。
 *
 * 依赖 sd_read 读的是卡上的真实内容（写完已经等过约 11ms 的编程时间）。*/
static uint8_t save_verify(uint8_t banks, const char *name)
{
    FatFile f;
    uint32_t size = 0, want = (uint32_t)banks << 13;
    uint8_t p, i, k;

    if (fat_open_read(&f, name, &size) != FAT_OK || size != want)
        return FAT_ERR_VERIFY;

    for (p = 0; p < banks; p++) {
        for (i = 0; i < SAVE_CHUNK; i++) {
            fat_read_sec(&f, game_work);             /* 先读 SD：窗口这时是 SD 的 */
            psram_map((uint8_t)(PSRAM_SAVE_LO + p));
            for (k = 0; k < 8; k++) {                /* 每块 64 字节比一次 */
                if (memcmp(game_work + ((uint16_t)k << 6),
                           (const void *)(WINDOW + ((uint16_t)i << 9) + ((uint16_t)k << 6)),
                           64)) {
                    psram_unmap();
                    return FAT_ERR_VERIFY;
                }
            }
            psram_unmap();
        }
    }
    return FAT_OK;
}

/* .SAV -> PSRAM。
 * wipe = 1 表示"卡上没有本游戏的存档，且 PSRAM 里是别的游戏留下的"，
 * 这时清成 $FF；wipe = 0 而卡上也没有存档，就**原样保留** PSRAM ——
 * 芯片里的存档靠纽扣电池活着，误清一次就没了。*/
static void save_load(const char *name, uint8_t banks, uint8_t wipe)
{
    FatFile f;
    uint32_t size = 0, want = (uint32_t)banks << 13;
    uint8_t p, i, have;

    /* 官方内核会给带时钟的卡额外追加 48 字节（时钟 + 启动戳）。我们暂时不生成
     * 也不使用，大小对得上或正好多 48 就取前 want 字节。 */
    have = (fat_open_read(&f, name, &size) == FAT_OK) &&
           (size == want || size == want + SAVE_FOOTER);

    if (!have && !wipe)
        return;                              /* 卡上没存档、也不知道 PSRAM 是谁的 → 一个字节都别动 */
    memset(game_work, 0xFF, 512);            /* 没有 .SAV / 读失败的扇区都要有确定内容 */

    for (p = 0; p < banks; p++) {
        for (i = 0; i < SAVE_CHUNK; i++) {
            if (have)
                fat_read_sec(&f, game_work);         /* 先读 SD：窗口这时是 SD 的 */
            psram_map((uint8_t)(PSRAM_SAVE_LO + p));
            memcpy((void *)(WINDOW + ((uint16_t)i << 9)), game_work, 512);
            psram_unmap();                           /* ★ 下一次 fat_read_sec 之前 */
        }
    }
    psram_unmap();
}

uint8_t save_prepare(const GameEntry *g)
{
    uint8_t banks, last;

    sd_read(fat_cluster_lba(g->cluster), game_work);
    banks = save_banks_of(game_work[0x149]);
    if (banks) {
        last = last_state(g->name);
        /* 电池没电过 → PSRAM 里「上次启动的是哪个游戏」这条记录本身也在衰减，
         * 读到什么都不可信。一律按「别的游戏」处理：卡上有 .SAV 就正常装进去，
         * 卡上没有就清成 $FF（等于新卡带该有的样子）。留着衰减后的垃圾，游戏
         * 会提示存档损坏，个别游戏（水晶）甚至会卡在读档界面。*/
        if (batt_dry) last = 2;
        save_load(g->name, banks, (uint8_t)(last == 2));
    }
    /* ★ 记「上次运行的是谁」与卡带有没有存档 RAM 无关，必须在 if 外面。
     * 见 save_arm() 里的说明。*/
    save_arm(g->name, banks);
    return banks;
}

void save_boot(void)
{
    uint8_t  banks, len, i, err, flag, batt, prev_ok, auto_bk, lfn_n;
    uint8_t  last_ok;
    uint32_t prev = 0, cur = 0;
    uint8_t  cur_ok = 0;

    psram_map(PSRAM_META);
    batt  = (uint8_t)(WINDOW[META_CANARY] == CANARY_OK);
    flag  = WINDOW[META_FLAG];
    banks = WINDOW[META_BANKS];
    len   = WINDOW[META_LEN];
#if defined(EZJR_DIAG) && EZJR_DIAG
    boot_banks = banks;                 /* 只给自检屏看 */
#endif
    prev_ok = (uint8_t)(WINDOW[META_SUM + 3] == SUM_MARK);
    prev  = (uint32_t)WINDOW[META_SUM] |
            ((uint32_t)WINDOW[META_SUM + 1] << 8) |
            ((uint32_t)WINDOW[META_SUM + 2] << 16);
    auto_bk = auto_on_locked();

    /* ---- 「上次启动的游戏」：两条带校验的记录，任一有效就用 ----
     *
     * 元数据页在新卡 / 刚刷完机时是擦除态（$FF 或电池衰减后的随机值）。
     * v0.11.0 靠「$1F0 的 8 字节全是可见 ASCII」当权威，但那组字段在真机上
     * 读回来是空的，于是菜单显示「无上次游戏」、备份屏退回 8.3 短名。
     * 现在每条记录自带异或校验，$FF 填满时校验必然不符，擦除态不会再被
     * 当成真记录；两条中坏一条也还有另一条。*/
    last_ok = (uint8_t)(rec_get(META_REC, boot_full, &boot_dir_saved) ||
                        rec_get(META_REC2, boot_full, &boot_dir_saved));
    if (!last_ok) {
        boot_full[0]   = 0;
        boot_dir_saved = 0;
    }

    /* 先重新盖章再判断：这一上电周期内 PSRAM 有电，内容从此刻起是真的。
     * 官方内核也是在提示之后立刻重盖（re-stamp），顺序一致才互认。
     * 「待备份」标志一起清掉 —— 写失败也不反复弹同一句。*/
    WINDOW[META_CANARY] = CANARY_OK;
    WINDOW[META_FLAG]   = 0x00;

    /* 官方字段里的 8 字符基本名：存档的 8.3 短名就用它（"POKEMON.SAV"），
     * 备份屏在没有长名时也退到它。*/
    if (len > 8)
        len = 8;
    for (i = 0; i < len; i++)
        boot_name[i] = (char)WINDOW[META_NAME + i];
    boot_name[len] = 0;
    psram_unmap();

    /* ---- 电池没电：这次存档不能备份 ----
     *
     * 哨兵不是 $88 说明关机期间 PSRAM 掉过电，里面的存档已经衰减成一堆随机
     * 字节。此时 $AA 章还可能在（$AA 本身就是某些衰减路径的中间值），所以
     * 「有章」不能当证据 —— 真拿它去写卡，只会把卡上那份好的 .SAV 换成垃圾，
     * 那才是真的存档丢了。官方内核在这里同样直接放弃，连 AUTO SAVE 都不例外。
     *
     * 静默条件：连 $AA 章都不在（PSRAM 已经衰减成 $FF 或干脆没启动过游戏），
     * 那就没有「你想备份但没成功」这回事，不必打扰用户。*/
    batt_dry = (uint8_t)!batt;
    if (!batt) {
        if (flag != 0xAA)
            return;
        cjk_begin();
        video_off();
        clear_screen();
        print_center(7, "BATTERY DRY");
        print_center(9, "NOT BACKED UP");
        cjk_center(11, STR_BATTERY_DRY);    /* 电池电压低 */
        video_on();
        wait_frames(150);
        return;
    }

    if (banks == 0 || banks > 16 || len == 0)
        return;

    /* ---- 内容没变就不备份 ----
     *
     * 指纹记的是「上次备份出去的那份内容」。所以：
     *   进游戏存档 → 芯片变了 → 指纹对不上 → 备份（这一步用户要的就是它）
     *   进了游戏但没存档 → 芯片没变 → 跳过（不再白写一遍卡）
     *   把电脑上的备份拷到卡上、没进游戏 → 芯片没变 → 跳过，用户那份不被盖掉
     *   拷了新存档又进了游戏 → 装载后内容变了 → 备份（写回的就是用户那份，无害）
     *
     * 没有指纹记录（第一次升级上来）→ 照旧备份一次，把指纹建立起来。*/
    if (prev_ok) {
        cur = save_sum(banks);
        cur_ok = 1;
        if (cur == prev)
            return;                     /* 一个字都没变，卡上那份不动 */
    }

    /* ---- 手动模式：问一句再写 ----
     * 走到这里说明确实有东西要写（章在、指纹对不上），不是"进游戏看一眼"
     * 那种空跑。手动模式下不擅自碰卡：A 备份，B 跳过。
     * 跳过时不动指纹，所以下次开机还会再问 —— 不会悄悄丢掉一次存档。
     *
     * `!batt_dry` 是冗余的（电池没电时上面那段已经 return 了），写在这里是
     * 给改代码的人看的：**确认屏永远不能出现在 BATTERY DRY 之后**。电池没电
     * 时芯片里那堆字节是随机垃圾，问"要不要备份"等于问"要不要把卡上的好
     * 存档换成一堆乱码"。*/
    /* ---- 中文全名：现查，不再从元数据页读 ----
     * 元数据页里那份 UCS-2 长名（v0.11.0 的 $130）在真机上读不回来，备份屏
     * 只能显示 8.3 短名。现在改成按「目录簇 + 完整 8.3 名」回卡上把**完整**
     * 长名重新读一遍（fat_full_lfn 写进 g_lfn_buf）：卡上那份名字永远是最新的，
     * 也没有 27 字符的截断上限。查不到（纯 ASCII 短名 ROM / 文件被删 / 换卡）
     * 就传空，备份屏退短名、存档只写 8.3 名。*/
    lfn_n = 0;
    if (boot_full[0])
        lfn_n = fat_full_lfn(boot_dir_saved, boot_full, g_lfn_buf);

    if (!auto_bk && !batt_dry) {
        screen_backup_confirm(lfn_n ? (const uint16_t *)g_lfn_buf
                                   : (const uint16_t *)0, boot_name);
        if (!ask_backup())
            return;
    }

    /* 存档一律写进**根目录**的 SAVER（v0.11.0 起跟官方一致，全卡只一个）。
     * v0.10.0 曾按 ROM 所在目录各建一个，用户实测根目录已经有一个、子目录里
     * 又冒出第二个，反而乱。fat_open_sav 内部会自己把工作目录切到根/SAVER。*/
    fat_cwd = 0;

    screen_backing_up();

    /* 存档文件名必须跟 ROM 文件名逐字相同（只换 .sav 扩展名），卡上的加载器
     * 才认得。长名就是上面现查出来那份（lfn_n 个字符），没有就传空 —— 那时
     * fat_open_sav 只写 8.3 短名。*/
    /* 写盘的名字必须和屏幕上那个中文名**同源**：都用 $A300 记录里的完整
     * 8.3 名（boot_full）。此前这里用的是 boot_name —— 那来自官方字段
     * $00F/$010，可能是上一个游戏、甚至官方内核留下的 `/SAVER/...` 路径，
     * 一旦是路径，写出的短名里就带 '/'，Windows 直接当非法项不显示。
     * 而 save_verify 用同一个名字去读，照样能读到，于是屏幕报「完成」、
     * 玩家却找不到文件。boot_full 为空（记录无效）才退回 boot_name。*/
    {
        const char *sav_nm = boot_full[0] ? boot_full : boot_name;

        err = save_dump(banks, sav_nm, lfn_n ? g_lfn_buf : (uint16_t *)0);
        if (!err)
            err = save_verify(banks, sav_nm);
    }

    fat_cwd = 0;                        /* 备份完要回根目录继续扫列表 */

    /* 备份成功才更新指纹：失败（卡满 / 簇链断）时留旧值，这样下次启动游戏
     * 再存档之后，内容一变还是会被判断成"需要备份"。 */
    if (!err) {
        if (!cur_ok)
            cur = save_sum(banks);
        sum_store(cur);
    }

    screen_save_result(err);
    wait_frames(90);
}

/* ---- 给列表页的 START 菜单用 ----
 * 这三个值都是 save_boot() 从元数据页里读出来的，代表「上一次启动的是哪个
 * 游戏」。菜单拿它们显示文件名、并负责一键把它再启动一次。
 *
 * 名字可能是空的（新卡 / 元数据页被擦过），调用方要按「无上次记录」处理。*/
const char *save_last_name(void)
{
    return boot_name;
}

const uint16_t *save_last_lfn(void)
{
    /* 每次现查，不缓存：g_lfn_buf 是全局 scratch，备份流程和 fat_open_sav 都会
     * 往里写，缓存住迟早被覆盖。一次目录扫描只有一两个扇区，而菜单只在按键
     * 时重绘，这点开销看不出来。*/
    g_lfn_buf[0] = 0;
    if (boot_full[0])
        (void)fat_full_lfn(boot_dir_saved, boot_full, g_lfn_buf);
    return g_lfn_buf;
}

const char *save_last_full(void)
{
    return boot_full;
}

uint32_t save_last_dir(void)
{
    return boot_dir_saved;
}

#if defined(EZJR_DIAG) && EZJR_DIAG
/* ---- 自检屏：「上次启动记录」的读数 ----
 *
 * 画在行 1/2（RAW / B / V）、行 8（REC / DIR）、行 12（LFN / K）。
 * 为什么写在 save.c 而不是 diag.c：这些值全是本文件的私有状态，而且 --diag
 * 版的 _CODE 早就顶到 $4000 上限，BANK1 还有几百字节 —— save.c 本来就在
 * BANK1，新代码放这儿不用再折腾段布局。
 *
 *   RAW  $A300 起前 4 字节，**十六进制**（v0.12.4 从十进制改过来）
 *   B    上次开机读到的 META_BANKS，0 = 上次那个 ROM 没有存档 RAM
 *   V    save_arm() 写完、unmap 再 map 读回的结果（v0.12.4 加）
 *   REC  两份带校验的记录里有任一份有效（开机时读的，跨断电）
 *   DIR  记录里的目录首簇，0 = 根目录
 *   LFN  fat_full_lfn() 现在回目录查回来的字符数，0 = 查不到长名
 *   K    记录第 18 字节（校验）的**存放值**，FF = 这个字节没落地
 *
 * 为什么 RAW 必须是十六进制：v0.12.3 用 print_dec2 印两位十进制，而它是
 * `'0' + (v/10)%10` —— 175 印成 "75"、191 印成 "91"、255 印成 "55"。
 * 真机上看到 "75,91"，实际可能是 75/175 和 91/191 四种组合，把"名字到底对
 * 不对"这件最关键的事变成了猜谜。
 *
 * V 和 REC 的分工是这一版的重点：
 *   V=--             → 写下去就没成（unmap 前已经坏）
 *   V=OK 而 REC=--   → 当场读得回来，跨断电没了
 *   V=OK 而 REC=OK   → 正常
 * 以前只有 REC 一个读数，这三种在屏上是一个样子。
 *
 * LFN 这一项会真的去读卡（fat_full_lfn 自己扫目录），这屏比别的行慢一点点，
 * 可以接受 —— 自检屏本来就是停下来给人看的。*/

void diag_last_record(void)
{
    /* v0.13.0 再砍一刀：B= / V= 两行和 LFN 的长度计数都去掉了。
     * 「记录字节数」和「map 读回」在 v0.12.4/$A300 那次就验证完结案；
     * LFN 只关心「查到没查到」，字符数对排障没有额外信息。
     * 现在这屏只剩三个会变的东西：记录读回来了没、目录簇是几、长名查到没。
     * 32KB 的 BANK1 就差这最后一百多字节。*/
    print_at(8, 0, "REC=");
    print_at(8, 4, boot_full[0] ? "OK" : "--");
    print_at(9, 0, "LFN=");
    print_at(9, 4, save_last_lfn()[0] ? "OK" : "--");
}
#endif /* EZJR_DIAG */
