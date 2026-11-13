/* test_stub.c — 仅供模拟器自检的桩（真实构建不编译本文件）
 *
 * 用途：让 tools/emu_test.py 能在 PyBoy 里跑通「游戏列表」和「设置时间」
 * 两个界面。模拟器没有 EZ-Flash 的 FPGA，也没有 SD 卡，所以替换的只是
 * **数据来源**，界面 / 绘制 / 状态机 / BCD 换算用的仍是正式代码：
 *   - fat_mount / fat_scan_games：不读 SD，返回固定清单
 *   - g_test_rtc_win：代替页 6 的 $A008-$A00E 窗口，供测试脚本填入/读回
 *
 * 由 build.sh 的 EZJR_TEST=1 分支以 -DEZJR_TEST_STUB 编译，产物是
 * build/ezgb-test.dat，**不是**要拷到卡上的 ezgb.dat。
 */
#include <string.h>
#include "hw.h"
#include "fat.h"
#include "game.h"
#include "save.h"
#include "fnt.h"
#include "video.h"
#include "font_data.h"

#ifdef EZJR_TEST_STUB


/* 页 6 RTC 窗口的 RAM 镜像：0x08..0x0E = s,m,h,day,wday,month,year (BCD) */
uint8_t g_test_rtc_win[32];

/* 20 个条目，正好跨两页（每页 12 行），用来验证翻页 */
static const char *const test_names[] = {
    "POKEMON.GBC",
    "TETRIS.GB",
    "CRYSTAL.GBC",
    "ZELDA.GB",
    "MARIO.GB",
    "METROID.GB",
    "KIRBY.GB",
    "WARIO.GB",
    "PICROSS.GB",
    "ALLEY.GB",
    "CONTRA.GB",
    "BOMBER.GB",
    "TENNIS.GB",
    "GOLF.GB",
    "FZERO.GB",
    "STAR.GB",
    "DUCK.GB",
    "BOX.GB",
    "PONG.GB",
    "LAST.GB",
};

#define TEST_COUNT ((uint8_t)(sizeof(test_names) / sizeof(test_names[0])))

/* ---- 假的目录树：验证子目录浏览 ----
 *
 * 根目录 = 20 个游戏（正好跨两页）+ 末尾两个文件夹；
 * 进文件夹后 = 「..」+ 3 个游戏。这样一条测试路径就覆盖了：
 * 列目录、名字后面的 "/"、按 A 进去、按 B / 按「..」回上级。*/
#define STUB_DIR_GBC  100UL             /* ASCII 名文件夹 */
#define STUB_DIR_CN   101UL             /* 中文长名文件夹 */
#define STUB_ROMS_IN_DIR 3

uint32_t fat_cwd;
uint8_t  fat_cwd_depth;
static uint32_t stub_stack[FAT_DIR_DEPTH];

void fat_dir_reset(void) { fat_cwd = 0; fat_cwd_depth = 0; }

uint8_t fat_dir_enter(uint32_t cluster)
{
    if (cluster < 2 || fat_cwd_depth >= FAT_DIR_DEPTH)
        return 0;
    stub_stack[fat_cwd_depth++] = fat_cwd;
    fat_cwd = cluster;
    return 1;
}

uint8_t fat_dir_up(void)
{
    if (!fat_cwd_depth)
        return 0;
    fat_cwd = stub_stack[--fat_cwd_depth];
    return 1;
}

uint8_t fat_mount(void)
{
    return FAT_OK;
}

/* 自检屏（src/diag.c）要显示布局数字：模拟器里没有真实 BPB，
 * 这里给一组固定的合理值，只为把渲染路径跑一遍。*/
void fat_info(FatInfo *out)
{
    out->mounted = 1;
    out->is_fat32 = 0;
    out->sectors_per_cluster = 8;
    out->part_lba = 8192;
    out->cluster_count = 4096;
}

/* 真机才有的启动流程：模拟器里没有 FPGA 的装载引擎，也没有 $7FE0 复位，
 * 所以这里直接返回「不是 GB ROM」，让 main 走失败提示那条路径 ——
 * 正好把错误屏的渲染也验一遍。*/
uint8_t game_launch(const GameEntry *g)
{
    (void)g;
    return GAME_ERR_BAD_ROM;
}

static void stub_put(GameEntry *list, uint8_t n, const char *name,
                     uint32_t cluster, uint32_t size)
{
    uint8_t k = 0;

    while (name[k] && k < GAME_NAME_MAX - 1) {
        list[n].name[k] = name[k];
        k++;
    }
    list[n].name[k] = 0;
    list[n].cluster = cluster;
    list[n].size = size;
}

/* ---- 删除文件（START 菜单的第二项）----
 * 位图记「哪些 test_names 已经被删」。列表和长名都用同一个「第 k 个还活着的
 * 游戏」映射，删掉一项之后它们才不会错位 —— 错位的表现是名字整体往上挪一行，
 * 看起来像删了 A 结果 B 换了名。*/
static uint8_t stub_del[3];

static uint8_t nth_alive(uint8_t k)
{
    uint8_t i, n = 0;

    for (i = 0; i < TEST_COUNT; i++) {
        if (stub_del[i >> 3] & (uint8_t)(1 << (i & 7)))
            continue;
        if (n == k)
            return i;
        n++;
    }
    return 0xFF;
}

uint8_t fat_delete(const char *rom)
{
    uint8_t i;

    for (i = 0; i < TEST_COUNT; i++) {
        const char *p = test_names[i];
        uint8_t k = 0;

        while (p[k] && rom[k] && p[k] == rom[k]) k++;
        if (!p[k] && !rom[k]) {
            stub_del[i >> 3] |= (uint8_t)(1 << (i & 7));
            return FAT_OK;
        }
    }
    return FAT_ERR_NO_FS;
}

uint8_t fat_scan_games(GameEntry *list, uint8_t max)
{
    uint8_t i, n = 0, k;

    if (fat_cwd == 0) {
        /* 顺序与 fat.c 一致：目录在前、文件在后（v0.9.0 起） */
        if (n < max) { stub_put(list, n, "GBC", STUB_DIR_GBC, 0); n++; }
        if (n < max) { stub_put(list, n, "GAMES", STUB_DIR_CN, 0); n++; }
        for (k = 0; n < max; k++) {
            i = nth_alive(k);
            if (i == 0xFF) break;
            stub_put(list, n, test_names[i], (uint32_t)(i + 2), 0x20000UL);
            n++;
        }
    } else {
        /* 子目录第 0 项固定是「返回上级」，跟 fat.c 的行为一致 */
        stub_put(list, 0, "..", 0, ENTRY_UP_SIZE);
        n = 1;
        for (k = 0; k < STUB_ROMS_IN_DIR && n < max; k++) {
            i = nth_alive(k);
            if (i == 0xFF) break;
            stub_put(list, n, test_names[i], (uint32_t)(i + 2), 0x20000UL);
            n++;
        }
    }

    return n;
}


/* 存档流程：模拟器里没有 PSRAM、没有 SD 写，返回 0 即可（等于"这个
 * 游戏没有存档"），主流程照常走。真机路径见 src/save.c。*/
uint8_t save_prepare(const GameEntry *g) { (void)g; return 0; }
void save_boot(void) { }
/* 设置页要读写这个开关。桩版存在 WRAM 里，跑测试时看不到真机的 PSRAM。
 * 初值 0 = 手动，跟真机「PSRAM 掉电后落回手动」一致。
 *
 * g_test_auto_backup 是给 emu_test.py 读的镜像（非 static 才进链接 map），
 * 用来断言"默认就是手动"。*/
/* 只用一个变量：显式写初值的 static 会被 SDCC 放进 _INITIALIZED 段，而本工程
 * 的链接脚本把 _INITIALIZED 和 _DATA 都定位在 0xC000 —— 两段重叠，读回来的
 * 其实是汉字池 pool_cp[0] 的低字节（0x38），于是"默认手动"会莫名变成"自动"。
 * 无初值的全局变量落在 _DATA，开局由 BSS 清零，没有这个问题。*/
uint8_t g_test_auto_backup;
uint8_t save_auto_get(void) { return g_test_auto_backup; }
void save_auto_set(uint8_t on) { g_test_auto_backup = on; }

/* ---- 中文文件名：模拟器里没有 SD 卡，也就没有 EZJR.FNT ----
 *
 * 内置一张"字库"：直接复用 font_data.h 里全部界面汉字（按 cjk_cp[] 的码点
 * 反查）。这样 fat_fill_lfn 返回的长名能一路走到
 * cjk_puts_ucs2 -> pool_get -> fnt_glyph -> vram_tile，整条渲染路径都能验。
 *
 * 第一项故意做成 "K 游戏时间.GBC" —— ASCII 字母、空格和汉字混在一起，
 * 专门回归「中文名开头的 K 被吞成空白」那个 bug。*/
static const uint16_t stub_names[3][FAT_LFN_CHARS] = {
    {'K', ' ', 0x6E38, 0x620F, 0x65F6, 0x95F4, '.', 'G', 'B', 'C', 0},
    {0x6E38, 0x620F, '.', 'G', 'B', 0},
    {0x6E38, 0x620F, 0x65F6, 0x95F4, '.', 'G', 'B', 'C', 0},
};

/* 中文名文件夹 "游戏时间"：四个字都在界面字库里（STR_* 用过），
 * 模拟器里能真的画出来，验证「中文目录名 + / 标记」。*/
static const uint16_t dir_cn_name[] = {0x6E38, 0x620F, 0x65F6, 0x95F4, 0};

/* 按**短名**查长名：排序之后下标和名字的对应关系变了，不能再拿 idx 硬套。*/
static const uint16_t *stub_lfn_of(const char *name)
{
    uint8_t i;

    if (!name[0])
        return 0;
    if (!strcmp(name, "GAMES"))
        return dir_cn_name;
    /* 只有前 3 个游戏准备了假长名，后 3 个故意没有 —— 这样「有长名画中文、
     * 没长名退 8.3」两条路都能在模拟器里看到。*/
    for (i = 0; i < 3; i++) {
        if (!strcmp(name, test_names[i]))
            return stub_names[i];
    }
    return 0;
}

void fat_fill_lfn(const GameEntry *list, uint8_t start, uint8_t rows,
                  uint16_t *out)
{
    uint8_t i;

    memset(out, 0, (uint16_t)rows * FAT_LFN_CHARS * 2);
    for (i = 0; i < rows; i++) {
        const uint16_t *src = stub_lfn_of(list[start + i].name);
        if (src)
            memcpy(out + (uint16_t)i * FAT_LFN_CHARS, src, FAT_LFN_CHARS * 2);
    }
}

void fnt_flush_cache(void) { }
uint8_t fnt_open(void) { return 1; }
uint8_t fnt_available(void) { return 1; }

uint8_t fnt_glyph(uint16_t cp, uint8_t *out)
{
    uint8_t i;
    for (i = 0; i < CJK_COUNT; i++) {
        if (cjk_cp[i] == cp) {
            memcpy(out, cjk_font[i], 8);
            return 1;
        }
    }
    return 0;
}

/* ---- 备份流程三屏的演示开关 ----
 * save.c 在测试版不编，备份屏的绘制代码已经抽到 video.c（真机也用它）。
 * emu_test.py 在列表出现之后把这个字节设成 1/2/3/4，main.c 里的测试钩子
 * 就会画出对应那一屏并停住；截图后写 0 放行。
 * 不能放在 save_boot() 里：那会在 ROM 启动几十帧内执行，而 PyBoy 的
 * BSS 清零在后面，写进去的标志会被抹掉。*/
volatile uint8_t g_test_boot_screen;

static const uint16_t demo_name[] = {
    'K', ' ', 0x6E38, 0x620F, 0x65F6, 0x95F4, '.', 'G', 'B', 'C', 0
};

void save_boot_demo(void)
{
    if (g_test_boot_screen == 1)
        screen_backup_confirm(demo_name, "K_____~1.GBC");
    else if (g_test_boot_screen == 2)
        screen_backing_up();
    else if (g_test_boot_screen == 3) {
        /* 完成屏 = 进度屏 + 下一行结果，演示时先画进度屏再叠结果 */
        screen_backing_up();
        screen_save_result(0);
    } else if (g_test_boot_screen == 4) {
        screen_backing_up();
        screen_save_result(FAT_ERR_VERIFY);
    }
    while (g_test_boot_screen)
        wait_frames(1);
}

/* ---- START 菜单的「上次运行游戏」----
 * 模拟器里没有 PSRAM，save.c 也不编，这里给一组固定值：截图上能看见
 * 「运行上次游戏」下面挂着 POKEMON.GBC 那一行。
 * g_test_no_last 是非 static 的，emu_test.py 可以在运行时改成 1，验证
 * 「还没有启动过任何游戏」时第一项变成「无上次游戏」。*/
static const char stub_last[] = "POKEMON.GBC";
uint8_t g_test_no_last;

const char *save_last_name(void)
{
    return g_test_no_last ? "" : stub_last;
}

const char *save_last_full(void)
{
    return g_test_no_last ? "" : stub_last;
}

const uint16_t *save_last_lfn(void)
{
    return g_test_no_last ? 0 : stub_names[0];
}

uint32_t save_last_dir(void)
{
    return 0;
}

/* 自检屏的「上次启动记录」读数：模拟器里没有 PSRAM（$A000 就是显存），
 * save.c 也不编（EZJR_TEST_STUB 版没有 BANK1），所以这里照真机的版式画
 * 一份**空读数** —— 目的只是让布局在模拟器截图上可见、可断言。真机上这一段
 * 在 save.c（BANK1）里，见 diag_last_record() 的注释。
 *
 * 空读数长这样：REC=-- = 没有有效记录；LFN=-- = 也没查到长名。
 * v0.13.0 版式：B= / V= / DIR= 都撤了（真机版同步，BANK1 只剩几字节）。*/
void diag_last_record(void)
{
    print_at(8, 0, "REC=--");
    print_at(9, 0, "LFN=--");
}

/* 「运行上次游戏」的定位桩：正式版由 fatx.c 按目录簇找目录项，模拟器里
 * 直接把名字填进一条假 GameEntry 交给 game_launch（它必然返回 BAD_ROM）。
 * 关键是**不改 fat_cwd / games[] / cursor** —— 启动失败后列表还在原处。*/
uint8_t fat_locate(uint32_t dir, const char *name83, GameEntry *out)
{
    (void)dir;
    if (!name83[0])
        return 0;
    stub_put(out, 0, name83, 100, 0x20000);
    return 1;
}

#endif /* EZJR_TEST_STUB */
