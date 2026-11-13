/* main.c — 内核主程序
 *
 * 两个标签，**SELECT 键切换**（v0.5.0 起不再用左右键）。
 *
 *   ┌────────────────────┐        ┌────────────────────┐
 *   │[游戏]      设置     │  行 0   │ 游戏      [设置]    │
 *   │━━━━━━━━━━━━━━━━━━━━│  行 1   │━━━━━━━━━━━━━━━━━━━━│
 *   │                    │        │26-11-11 15:30:05 设置│ 行 3  时间（走秒）
 *   │▶POKEMON.GBC        │  行 3   │                  ━━│ 行 4  焦点线
 *   │ POKEMON2.GBC       │        │                    │
 *   │        …           │        │自动备份 自动     设置│ 行 6
 *   │                    │        │                  ━━│ 行 7  焦点线
 *   │      20 个游戏      │  行 17  │                    │
 *   └────────────────────┘        └────────────────────┘
 *
 * 游戏页：上下选游戏，A 启动；光标走出本页自动翻页。
 * 设置页：上下在两个条目的按钮之间移动焦点。
 *   - 时间条目按 A 进编辑：时间冻结，左右选字段（走到最右是「保存」按钮），
 *     上下改值，在「保存」上按 A 写回 PCF8563；按 B 放弃修改。不编辑时每秒
 *     跟着 RTC 走，只重画那一行。
 *   - 自动备份条目按 A 在「自动 / 手动」之间切换。手动模式下开机有存档要备份
 *     时先问一句，A 备份、B 跳过。
 *
 * 为什么时间能"流动"：主循环改成非阻塞了（poll_key 取按下沿 + 每帧查一次），
 * 不再像以前那样卡在 get_key() 里等按键。sd_error_screen 那种"停在这儿等按"
 * 的屏仍然用阻塞版 get_key()。
 *
 * 重绘策略见 docs/09-ui.md。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"
#include "video.h"
#include "rtc.h"
#include "strings.h"
#include "diag.h"
#include "game.h"
#include "save.h"
#include "fnt.h"

/* 仅模拟器自检版：让测试脚本能在运行时唤出备份流程的三屏。
 * 正式版这两行不参与编译。*/
#if defined(EZJR_TEST_STUB)
extern volatile uint8_t g_test_boot_screen;
extern void save_boot_demo(void);
#endif

#define SCR_W        20
#define LIST_TOP     3                 /* 行 2 留空：分隔线和列表之间透一口气 */
#define LIST_ROWS    13                /* 行 3-15；行 16 再留空，行 17 放计数 */

/* 内核版本，显示在设置页（"MZOS V0.12.2 FW5"）。
 *
 * **跟发布号同步**：以前只写主次版本（"V0.12"），真机上刷了两版补丁却看不出
 * 差别，只能靠回忆。屏幕一共 20 列，"MZOS " + "V0.12.2" + " " + "FW" + 1 位
 * 固件 = 16 列，补丁号放得下；再长就到两位数补丁号，那时才需要缩。
 *
 * 后面的列位置由 VER_COL 自己算，改版本号不用再动绘制代码。*/
#define MZOS_VER     "V0.14.0"
#define VER_COL      (5 + ((uint8_t)(sizeof(MZOS_VER) - 1)) + 1)  /* "MZOS "+版本+空格 */

#define TAB_GAME     0
#define TAB_SET      1

/* 设置页版式 */
#define ROW_TIME      3                /* 时间那一行：26-11-11 15:30:05 设置 */
#define ROW_TIME_UL   4                /* 时间按钮下面的焦点线 */
#define ROW_BACKUP    6                /* 自动备份 自动 设置 */
#define ROW_BACKUP_UL 7
#define ROW_CURSOR    2                /* 编辑时的 ▼ 停在内容上方 */
#define COL_BTN      18                /* 两个按钮的右对齐列 */

/* 焦点：设置页里哪一条的按钮被选中 */
#define F_TIME       0
#define F_BACKUP     1

/* 编辑光标比字段多一个位置：最右是「保存」按钮 */
#define EF_BTN       6

/* 最多 64 个游戏。这个数直接决定 WRAM 占用（64×13 = 832 字节），而 WRAM
 * 只有 $C000-$CFFF 可给 _DATA 用（$D000 是启动游戏的 handoff 区）。*/
#define GAME_MAX 64
static GameEntry games[GAME_MAX];
/* 当前这一页的长文件名（中文名）。13 行 × 16 个 UCS-2 = 416 字节。
 * 只缓存一页而不是全部：全部存下来要 96×32 字节，WRAM 放不下；
 * 翻页时重扫一遍目录（32 个扇区）比常驻几百字节划算。*/
static uint16_t  page_names[LIST_ROWS][FAT_LFN_CHARS];
static uint8_t   game_count;
static uint8_t   cursor;
static uint8_t   fw;
static uint8_t   tab;

/* 设置页状态 */
static uint8_t   tf[T_FIELDS];          /* 当前显示/编辑的值 */
static uint8_t   tf_orig[T_FIELDS];     /* 进编辑前的那份，B 键还原用 */
static uint8_t   tfield;                /* 编辑光标 0..5 字段，6 = 保存按钮 */
static uint8_t   editing;               /* 1 = 时间冻结、正在编辑 */
static uint8_t   tvl;                   /* RTC 的 VL：时间不可信 */
static uint8_t   set_focus;             /* F_TIME / F_BACKUP */
static uint8_t   auto_backup;           /* 1 = 自动备份（默认），0 = 手动 */
static uint8_t   tick;                  /* 帧计数，用来每 1/4 秒查一次 RTC */
static uint8_t   held;                  /* 上一帧按键，取按下沿用 */

/* 「年 月 日 时 分 秒」在 T_FIELDS 里的顺序 */
static const uint8_t field_idx[6] = {T_YEAR, T_MON, T_DAY, T_HOUR, T_MIN, T_SEC};
/* 每个字段在行 3 的起始列（26-11-11 15:30:05 里数字的位置）*/
static const uint8_t field_col[6] = {0, 3, 6, 9, 12, 15};

/* ---------------- 按键 ---------------- */

/* 阻塞取一个键：等到有键、再等松开、稳 3 帧。只给"停在这儿等按"的屏用。*/
static uint8_t get_key(void)
{
    uint8_t k;
    do {
        k = buttons();
        wait_frames(1);
    } while (!k);
    wait_release();
    wait_frames(3);
    return k;
}

/* 非阻塞取按下沿：按住不放只算一次，松开后再按才算下一次。
 * 主循环每帧调一次，所以时间才能一边走一边响应按键。*/
static uint8_t poll_key(void)
{
    uint8_t k = buttons();
    uint8_t e = (uint8_t)(k & ~held);
    held = k;
    return e;
}

/* ---------------- 标签栏 ---------------- */

static void draw_tabs(void)
{
    clear_row(0);
    /* 两个标签位置固定，选中的用方括号包住。用宽间距版本（字后留一列），
     * 否则两个汉字会贴成一团 —— 8px 汉字的字形几乎顶满 8x8。
     *   游@1 空@2 戏@3   …   设@9 空@10 置@11 */
    cjk_puts_wide(0, 1, STR_GAME);
    cjk_puts_wide(0, 9, STR_SETTINGS);
    if (tab == TAB_GAME) {
        print_at(0, 0, "[");
        print_at(0, 4, "]");
    } else {
        print_at(0, 8, "[");
        print_at(0, 12, "]");
    }
    print_repeat(1, 0, SYM_HEAVY, SCR_W);
}

/* ---------------- 界面 1：游戏列表 ---------------- */

static void draw_list_row(uint8_t i)
{
    uint8_t base = (uint8_t)((cursor / LIST_ROWS) * LIST_ROWS);
    uint8_t idx = (uint8_t)(base + i);
    uint8_t row = (uint8_t)(LIST_TOP + i);
    uint8_t w;

    clear_row(row);
    if (idx >= game_count)
        return;
    if (idx == cursor)
        put_tile(row, 0, SYM_ARROW);

    /* 有中文名就画中文名，没有（或字库文件不在）就退回 8.3 短名 */
    if (page_names[i][0]) {
        w = cjk_puts_ucs2(row, 2, page_names[i]);
    } else {
        uint8_t n = (uint8_t)strlen(games[idx].name);
        w = (n > SCR_W - 2) ? (uint8_t)(SCR_W - 2) : n;
        print_at(row, 2, games[idx].name);
    }

    /* 文件夹在名字后面加一撇 "/"：按 A 是"进去"还是"启动"，一眼能分清。
     * 「..」不加，它自己已经说明用途了。*/
    if (ENTRY_IS_DIR(&games[idx]) && (uint8_t)(2 + w) < SCR_W)
        put_tile(row, (uint8_t)(2 + w), '/');
}

/* 光标在同一页内上下移动：只改两格（旧的箭头擦掉、新的画上）。*/
static void move_cursor(uint8_t old_i, uint8_t new_i)
{
    put_tile((uint8_t)(LIST_TOP + old_i), 0, 0);
    put_tile((uint8_t)(LIST_TOP + new_i), 0, SYM_ARROW);
}

/* 真正能启动的条目有几个。文件夹和「..」不算游戏 ——
 * 混进来会让「20 个游戏」这种数字看着对不上。*/
static uint8_t count_roms(void)
{
    uint8_t i, n = 0;

    for (i = 0; i < game_count; i++)
        if (!ENTRY_IS_DIR(&games[i]) && !ENTRY_IS_UP(&games[i]))
            n++;
    return n;
}

/* 行 17：一共几个游戏 */
static void draw_count(void)
{
    uint8_t n = 1, w, x, roms;
    uint16_t v;

    clear_row(17);
    roms = count_roms();
    if (roms == 0)
        return;
    v = roms;
    while (v >= 10) { v /= 10; n++; }
    w = (uint8_t)(n + 1 + 3);
    x = (uint8_t)((SCR_W - w) / 2);
    print_u16(17, x, roms);
    cjk_puts(17, (uint8_t)(x + n + 1), STR_GAMES_UNIT);
}

/* 开机骨架：fat_mount / fnt_open / save_boot / 扫目录 这一段全是 SD 读，
 * 原来屏幕上什么都没有，就是用户看到的那段长白屏。先画标签栏和「读取中」
 * （只用 ROM 里的字模，一次 SD 都不读），后面几十次扇区读期间屏幕有内容。*/
static void boot_screen(void)
{
    cjk_begin();
    video_off();
    clear_screen();
    draw_tabs();
    cjk_center_wide(9, STR_READING);
    video_on();
}

/* refetch=1 表示换页了，要重新扫一遍目录拿这一页的长文件名；
 * 开机那次的长名由 rescan() 顺带填好，不用再扫。*/
static void draw_list(uint8_t refetch)
{
    uint8_t i, base;

    cjk_begin();
    /* 重新取这一页的长文件名：翻页、切标签、启动失败返回都走这里。
     * fat_fill_lfn 按「短名」回目录里配对，所以列表怎么排序都不会错位。*/
    base = (uint8_t)((cursor / LIST_ROWS) * LIST_ROWS);
    if (refetch)
        fat_fill_lfn(games, base, LIST_ROWS, &page_names[0][0]);

    video_off();
    clear_screen();
    draw_tabs();
    for (i = 0; i < LIST_ROWS; i++)
        draw_list_row(i);
    /* 这里**不再**打「无游戏」。
     *
     * v0.12.1 及以前是 `if (count_roms() == 0) cjk_center(8, "无游戏")`，判据只看
     * 「能启动的 ROM 有几个」，于是**整层都是文件夹时也会打这三个字**：玩家明明
     * 看着一列目录名，屏幕正中央却说"无游戏"，像目录读坏了（真机反馈就是
     * "GBC官方游戏全集(1934个)\超级机器人大战 里显示无游戏"）。
     * 真的一无所有时也不用说：只有一行「..」本身已经说明这层是空的。
     * 空目录/纯目录层的区分交给列表自己表达。*/
    draw_count();
    video_on();
}

/* 进 / 出目录之后：重扫当前目录、光标回到第一行、整屏重绘。
 * 长名单独取一遍（fat_fill_lfn 按短名配对），所以 draw_list(0) 不用再扫。*/
static void rescan(void)
{
    cursor = 0;
    game_count = fat_scan_games(games, GAME_MAX);
    fat_fill_lfn(games, 0, LIST_ROWS, &page_names[0][0]);
    draw_list(0);
}

/* 整页翻：← 上一页、→ 下一页。
 *
 * 光标落在新页的**同一相对位置**（不是跳回该页第一行）：这样连按几下 → 就是
 * 一页一页往后扫，眼睛不用每页重新找位置。最后一页通常不满，相对位置会越过
 * game_count，这时贴到本页最后一条 —— 否则光标会指到空行上，看着像列表坏了。
 *
 * 到第一页 / 最后一页**停住不绕回**：翻页是有明确页序的操作，绕回会让人丢掉
 * 「我在第几页」的位置感。上下键绕回是因为它要能从最后一项直接回到开头，
 * 那是"移动光标"的语义，跟"翻页"不是一回事。
 *
 * 换页必须 draw_list(1)：page_names 只缓存当前页那 13 条长名，不重取就会把
 * 上一页的名字画到这一页上。*/
static void page_move(uint8_t next)
{
    uint8_t page = (uint8_t)(cursor / LIST_ROWS);
    uint8_t npage;

    if (!game_count)
        return;
    if (next) {
        npage = (uint8_t)(page + 1);
        /* npage 最大 5（64/13+1），5*13 = 65，uint8_t 装得下，不会绕回 */
        if ((uint8_t)(npage * LIST_ROWS) >= game_count)
            return;                     /* 已经是最后一页 */
    } else {
        if (!page)
            return;                     /* 已经是第一页 */
        npage = (uint8_t)(page - 1);
    }
    cursor = (uint8_t)(npage * LIST_ROWS + (cursor % LIST_ROWS));
    if (cursor >= game_count)
        cursor = (uint8_t)(game_count - 1);
    draw_list(1);
}

/* ---------------- 界面 2：设置 ---------------- */

/* 行 3 的 17 列：26-11-11 15:30:05
 *
 * 时间是 17 列、按钮 2 列，正好 20，中间不留空。年份只放两位：PCF8563
 * 本来也只存两位年，而四位年要多占 2 列，按钮就挤出去了。*/
static void draw_time_line(void)
{
    print_dec2(ROW_TIME, 0, tf[T_YEAR]);
    put_tile(ROW_TIME, 2, '-');
    print_dec2(ROW_TIME, 3, tf[T_MON]);
    put_tile(ROW_TIME, 5, '-');
    print_dec2(ROW_TIME, 6, tf[T_DAY]);
    put_tile(ROW_TIME, 8, ' ');
    print_dec2(ROW_TIME, 9, tf[T_HOUR]);
    put_tile(ROW_TIME, 11, ':');
    print_dec2(ROW_TIME, 12, tf[T_MIN]);
    put_tile(ROW_TIME, 14, ':');
    print_dec2(ROW_TIME, 15, tf[T_SEC]);
}

/* 时间条目的按钮：平时「设置」，编辑时「保存」（都是两个字，覆盖写不用清）*/
static void draw_time_button(void)
{
    cjk_puts(ROW_TIME, COL_BTN, editing ? STR_SAVE_BTN : STR_SETTINGS);
}

/* 自动备份条目：左边写条目名，紧跟状态，按钮单独右对齐。
 * 状态别贴着按钮 —— 两个两字词紧挨会读成一个四字词（「手动设置」）。*/
static void draw_backup_state(void)
{
    cjk_puts_wide(ROW_BACKUP, 9, auto_backup ? STR_AUTO : STR_MANUAL);
}

/* 两条的焦点线：选中的那条画粗线，另一条擦掉 */
static void draw_focus(void)
{
    print_repeat(ROW_TIME_UL, COL_BTN, 0, 2);
    print_repeat(ROW_BACKUP_UL, COL_BTN, 0, 2);
    if (set_focus == F_TIME)
        print_repeat(ROW_TIME_UL, COL_BTN, SYM_HEAVY, 2);
    else
        print_repeat(ROW_BACKUP_UL, COL_BTN, SYM_HEAVY, 2);
}

/* 编辑光标：▼ 停在行 2，指向行 3 里当前字段（或最右的保存按钮）*/
static void edit_cursor_col(uint8_t pos)
{
    put_tile(ROW_CURSOR, pos == EF_BTN ? COL_BTN : field_col[pos], SYM_TRI_DOWN);
}

static void move_edit_cursor(uint8_t old_pos, uint8_t new_pos)
{
    put_tile(ROW_CURSOR, old_pos == EF_BTN ? COL_BTN : field_col[old_pos], 0);
    edit_cursor_col(new_pos);
}

/* 行 17：状态提示 */
static void draw_status(uint8_t saved)
{
    uint8_t want = saved ? 1 : (tvl ? 2 : 0);

    clear_row(17);
    if (want == 1)
        cjk_center(17, STR_SAVED);
    else if (want == 2)
        cjk_center(17, STR_PLEASE_SET);
}

static void draw_settings(uint8_t saved)
{
    cjk_begin();
    video_off();
    clear_screen();
    draw_tabs();
    draw_time_line();
    draw_time_button();
    cjk_puts_wide(ROW_BACKUP, 0, STR_AUTO_BACKUP);      /* 自动备份 @0-6 */
    draw_backup_state();
    cjk_puts(ROW_BACKUP, COL_BTN, STR_SETTINGS);        /* 备份条目的按钮 */
    draw_focus();
    if (editing)
        edit_cursor_col(tfield);
    /* 版本行：MZOS <内核版本> FW<固件版本>，例如 "MZOS V0.12.2 FW5"。
     *
     * FPGA 页 4 的首字节就是固件版本（真机上是 5）。**十进制不补零** —— 补成
     * "05" 会被当成另一台机器（v0.12.0 用的 print_dec2 就是这个问题）。读不到
     * 时写 "FW--"。整行只读，不参与上下选条目。*/
    {
        uint8_t fv = fw_version();

        print_at(9, 0, "MZOS " MZOS_VER " ");
        if (fv) {
            print_at(9, VER_COL, "FW");
            print_u16(9, (uint8_t)(VER_COL + 2), fv);
        } else {
            print_at(9, VER_COL, "FW--");
        }
    }
    draw_status(saved);
    video_on();
}

/* 字段加减 */
static void bump_field(int8_t delta)
{
    uint8_t idx = field_idx[tfield];
    uint8_t mx = rtc_field_max(idx);
    uint8_t v = tf[idx];

    if (idx == T_MON || idx == T_DAY) {         /* 1 起算的字段 */
        if (delta > 0) v = (uint8_t)(v >= mx ? 1 : v + 1);
        else           v = (uint8_t)(v <= 1 ? mx : v - 1);
    } else {
        if (delta > 0) v = (uint8_t)(v >= mx ? 0 : v + 1);
        else           v = (uint8_t)(v == 0 ? mx : v - 1);
    }
    tf[idx] = v;
    draw_time_line();
}

/* 读一次 RTC 刷新显示（只在非编辑状态下跑）*/
static void poll_clock(void)
{
    uint8_t f[T_FIELDS];
    uint8_t i, diff = 0;

    if (rtc_read(f))
        return;                     /* 读不到就保持原样，别把屏刷成 00 */
    for (i = 0; i < T_FIELDS; i++)
        if (f[i] != tf[i]) diff = 1;
    if (!diff)
        return;
    memcpy(tf, f, T_FIELDS);
    draw_time_line();
}

/* launch_error 在下面「错误界面」那一节定义，这里先用一下（启动失败要回到列表）*/
static void launch_error(uint8_t err);

/* ---------------- 界面 3：START 菜单 ----------------
 *
 * 列表页按 START 弹出（diag 版多一项「自检」）。两项：
 *   运行上次游戏 —— 直接启动元数据页里记着的那个 ROM，省得每次翻目录
 *   删除当前文件 —— 把光标处的 ROM 从卡上删掉（文件夹不删）
 * B 返回。
 *
 * 做成整屏而不是盖在列表上：20x18 的屏幕里，六行菜单压住半个列表，底下的
 * 名字缺一半反而看不清。整屏重绘也就一两帧。菜单反色（黑底白字）+ 左右边框
 * 拉满全屏，名字行用细线框和引导符号明确降级为子信息。上下切换只挪箭头。*/
#if EZJR_DIAG
#define MENU_N      3
#define MENU_EXTRA  1
#else
#define MENU_N      2
#define MENU_EXTRA  0
#endif

static uint8_t menu_open;
static uint8_t menu_sel;

/* 光标处能不能删。「..」也算目录：按 A 是回上级，当然不能删。*/
static uint8_t cursor_is_dir(void)
{
    if (!game_count)
        return 1;
    return (uint8_t)(ENTRY_IS_DIR(&games[cursor]) || ENTRY_IS_UP(&games[cursor]));
}

static void draw_menu(void)
{
    cjk_begin();
    video_off();
    clear_screen();                 /* 顺手把调色板收回正常态（白底黑字）*/
    draw_tabs();
    screen_menu((uint8_t)(save_last_full()[0] != 0), save_last_lfn(),
                save_last_full(), cursor_is_dir(), menu_sel, MENU_EXTRA);
    video_on();
}

/* 启动「上次运行的游戏」——**直接启动**，不切目录、不重扫列表。
 *
 * v0.10.0 的做法是「按元数据里的目录簇切过去 → 重扫 → 按名字找」，但存下来
 * 的只有 8 字符基本名，跟列表里的 "POKEMON.GBC" 永远 strcmp 不上；玩家看到
 * 的就是「跳到上次游戏所在目录」，什么都没启动。
 *
 * 现在元数据里多存了完整 8.3 名，fat_locate() 拿「目录簇 + 完整名」直接在
 * 那个目录里定位 ROM 并填一条临时 GameEntry，全程不碰 fat_cwd / games[] /
 * cursor。启动成功就不返回；启动失败画完错误屏，主循环把**原来那个列表**
 * 重画一遍，光标还在原处。文件被删或换了卡就安静回列表。*/
static void run_last(void)
{
    const char *full = save_last_full();
    uint32_t    d = save_last_dir(), saved;
    GameEntry   g;
    uint8_t     err;

    if (!full[0])
        return;                         /* 没有有效记录：菜单里写的是「无上次游戏」*/
    if (!fat_locate(d, full, &g))
        return;                         /* 文件被删 / 换了卡：安静回列表 */

    /* save_arm() 把当时的 fat_cwd 记进「上次启动」的记录，作为下一次
     * 「运行上次游戏」的目录。这里临时切到目标目录再盖章，否则会把子目录里
     * 的游戏记成「根目录的」，第二次按 START 就找不着了。*/
    saved = fat_cwd;
    fat_cwd = d;
    save_prepare(&g);
    fat_cwd = saved;

    err = game_launch(&g);
    launch_error(err);
}

/* 删掉光标处的 ROM，然后把行 17 的计数临时换成「已删除」。
 * 不弹确认屏：菜单里已经写明删的是哪个文件，再套一层反而多按一次键；
 * 真正危险的操作（覆盖存档）已经在存档流程里确认过了。*/
static void delete_current(void)
{
    if (cursor_is_dir())
        return;
    if (fat_delete(games[cursor].name) != FAT_OK)
        return;
    rescan();
    cjk_center(17, STR_DELETE_DONE);
    wait_frames(60);
    draw_count();
}

/* ---------------- 错误界面 ---------------- */

/* 错误屏：标题已经是中文，就不再下面再挂一行英文 "LAUNCH FAILED" ——
 * 同一句话写两遍对玩家没有任何信息量。原因也全用汉字（STR_* 里不能混 ASCII，
 * 数组元素是 glyph id，0 是结束符），所以「不是游戏卡」而不是 "NOT A GB ROM"。
 *
 * 中间那行 ERR=01 也去掉了（v0.10.0）：下面一行已经用人话写清楚是什么问题，
 * 再挂一个错误码只是让玩家多一个看不懂的数字。需要错误码定位时看自检屏。*/
static void launch_error(uint8_t err)
{
    const uint8_t *why;

    if (err == GAME_ERR_BAD_ROM)
        why = STR_NOT_GB;
    else if (err == FAT_ERR_FRAGMENTED)
        why = STR_TOO_FRAG;
    else if (err == FAT_ERR_BAD_CHAIN)
        why = STR_BAD_CHAIN;
    else
        why = STR_ERR_UNKNOWN;

    cjk_begin();
    video_off();
    clear_screen();
    cjk_center_wide(6, STR_LAUNCH_FAIL);
    cjk_center(9, why);
    video_on();
    wait_frames(180);
}

/* 挂载失败屏。和启动失败屏同样的道理：标题「存储卡错误」已经是中文，
 * 再挂一行 "MOUNT FAILED" 只是把同一句话说第二遍，删掉；原因换成汉字。
 * 每种 err 对应一句人话 —— 「无法识别此卡」和「格式不支持」是两回事，
 * 玩家能据此判断是擦卡重来（前者）还是换台电脑重新格式化（后者）。*/
static void sd_error_screen(uint8_t err)
{
    const uint8_t *why;
    uint8_t k;

    switch (err) {
    case FAT_ERR_NO_SIG: why = STR_NO_SIG;      break;  /* 无 55AA 签名 */
    case FAT_ERR_FAT12:  why = STR_FAT12_MSG;   break;
    case FAT_ERR_GPT:    why = STR_GPT_MSG;     break;
    case FAT_ERR_NO_FS:  why = STR_NO_FS;       break;
    default:             why = STR_ERR_UNKNOWN; break;
    }

    for (;;) {
        cjk_begin();
        video_off();
        clear_screen();
        cjk_center_wide(6, STR_SD_ERROR);
        cjk_center(9, why);
#if EZJR_DIAG
        print_center(14, "ST:DIAG");     /* 一眼看出刷的是自检版 */
#endif
        video_on();

        k = get_key();
#if EZJR_DIAG
        if (k & BTN_START)
            diag_screen();
        else
#endif
            wait_frames(60);
    }
}

/* ---------------- 主流程 ---------------- */

void main(void)
{
    uint8_t err, k;

    video_init();
    tab = TAB_GAME;
    boot_screen();                      /* 先把界面画出来，别让用户盯白屏 */
    fw = fw_version();
    (void)fw;
    fpga_page(0);

    err = fat_mount();
    if (err)
        sd_error_screen(err);           /* 不返回 */

    /* 卡上有没有汉字字库文件（EZJR.FNT）。没有也不影响别的功能，
     * 只是中文文件名退回显示 8.3 短名。*/
    (void)fnt_open();

    /* 开机第一件事：查电池哨兵，顺手把 PSRAM 里的存档写回 SD 卡。
     * 顺序重要：save_boot 设的 batt_dry 给后面 save_prepare 用；它也可能往
     * 根目录加 .SAV，先做能让扫描看到最新目录。*/
    auto_backup = save_auto_get();
    save_boot();

    game_count = fat_scan_games(games, GAME_MAX);
    fat_fill_lfn(games, 0, LIST_ROWS, &page_names[0][0]);
    cursor = 0;
    tab = TAB_GAME;
    editing = 0;
    set_focus = F_TIME;
    tfield = 0;
    tvl = rtc_read(tf);

    draw_list(0);                       /* 长名已由 fat_fill_lfn 取好 */

    for (;;) {
#if defined(EZJR_TEST_STUB)
        if (g_test_boot_screen) {
            save_boot_demo();               /* 画完停住，等脚本写 0 放行 */
            draw_list(0);
        }
#endif
        k = poll_key();

        /* SELECT：切标签。正在编辑的话算放弃（不保存）。*/
        if (k & BTN_SELECT) {
            if (editing) {
                memcpy(tf, tf_orig, T_FIELDS);
                editing = 0;
            }
            menu_open = 0;              /* 菜单开着时切标签 = 关掉菜单 */
            if (tab == TAB_GAME) {
                tab = TAB_SET;
                tvl = rtc_read(tf);
                draw_settings(0);
            } else {
                tab = TAB_GAME;
                draw_list(0);
            }
            wait_frames(1);
            continue;
        }

        /* 菜单开着：按键全部给菜单，列表一个都不响应。
         * 菜单是「模态」的，否则上下键会同时移动菜单和背后的列表光标。*/
        if (menu_open) {
            if (k & BTN_UP) {
                if (menu_sel) {
                    menu_sel--;
                    menu_cursor(menu_sel, MENU_EXTRA);
                }
            } else if (k & BTN_DOWN) {
                if ((uint8_t)(menu_sel + 1) < MENU_N) {
                    menu_sel++;
                    menu_cursor(menu_sel, MENU_EXTRA);
                }
            } else if (k & BTN_A) {
                menu_open = 0;
                if (menu_sel == 0) {
                    run_last();
                    draw_list(0);       /* 启动失败返回，或者没找到那个游戏 */
                } else if (menu_sel == 1) {
                    delete_current();   /* 内部已重扫 + 重绘 */
                }
#if EZJR_DIAG
                else {
                    diag_screen();
                    draw_list(0);
                }
#endif
            } else if (k & (BTN_B | BTN_START)) {
                menu_open = 0;          /* B 返回；START 再按一次也当取消 */
                draw_list(0);
            }
            wait_frames(1);
            continue;
        }

        if (tab == TAB_GAME) {
            if (k & BTN_UP) {
                if (game_count) {
                    uint8_t old = cursor;
                    uint8_t opage = (uint8_t)(old / LIST_ROWS);
                    cursor = (uint8_t)(old == 0 ? game_count - 1 : old - 1);
                    if ((uint8_t)(cursor / LIST_ROWS) != opage)
                        draw_list(1);
                    else
                        move_cursor((uint8_t)(old % LIST_ROWS),
                                    (uint8_t)(cursor % LIST_ROWS));
                }
            } else if (k & BTN_DOWN) {
                if (game_count) {
                    uint8_t old = cursor;
                    uint8_t opage = (uint8_t)(old / LIST_ROWS);
                    cursor = (uint8_t)(old + 1 >= game_count ? 0 : old + 1);
                    if ((uint8_t)(cursor / LIST_ROWS) != opage)
                        draw_list(1);
                    else
                        move_cursor((uint8_t)(old % LIST_ROWS),
                                    (uint8_t)(cursor % LIST_ROWS));
                }
            } else if (k & BTN_LEFT) {
                page_move(0);               /* ← 上一页 */
            } else if (k & BTN_RIGHT) {
                page_move(1);               /* → 下一页 */
            } else if ((k & BTN_A) && game_count) {
                if (ENTRY_IS_UP(&games[cursor])) {
                    fat_dir_up();               /* 列表里的「..」 */
                    rescan();
                } else if (ENTRY_IS_DIR(&games[cursor])) {
                    /* 超出深度限制时 enter 返回 0，停在原地不动 —— 总比
                     * 进去了出不来好（路径栈只有 4 层）。*/
                    if (fat_dir_enter(games[cursor].cluster))
                        rescan();
                } else {
                    /* SAVER 固定在根目录，跟当前在几层子目录里无关 */
                    save_prepare(&games[cursor]);
                    err = game_launch(&games[cursor]);
                    launch_error(err);
                    draw_list(0);
                }
            } else if ((k & BTN_B) && fat_cwd_depth) {
                fat_dir_up();                   /* B 也能回上级，习惯哪个用哪个 */
                rescan();
            } else if (k & BTN_START) {
                /* 弹出菜单。diag 版的自检屏也挪到菜单里了 —— START 只有一个，
                 * 让它先出菜单比在同一个键上绑两个功能清楚。*/
                menu_open = 1;
                menu_sel = 0;
                draw_menu();
            }
        } else {
            /* ---------------- 设置页 ---------------- */

            if (editing) {
                /* 时间冻结：左右选字段/按钮，上下改值 */
                if (k & BTN_LEFT) {
                    uint8_t old = tfield;
                    tfield = (uint8_t)(tfield == 0 ? EF_BTN : tfield - 1);
                    move_edit_cursor(old, tfield);
                } else if (k & BTN_RIGHT) {
                    uint8_t old = tfield;
                    tfield = (uint8_t)(tfield >= EF_BTN ? 0 : tfield + 1);
                    move_edit_cursor(old, tfield);
                } else if (k & BTN_UP) {
                    if (tfield != EF_BTN)
                        bump_field(1);
                } else if (k & BTN_DOWN) {
                    if (tfield != EF_BTN)
                        bump_field(-1);
                } else if (k & BTN_A) {
                    if (tfield == EF_BTN) {
                        /* 保存：写回 PCF8563，退出编辑，时间重新开始走 */
                        tf[T_WDAY] = rtc_weekday(tf[T_YEAR], tf[T_MON], tf[T_DAY]);
                        rtc_write(tf);
                        tvl = 0;
                        put_tile(ROW_CURSOR, COL_BTN, 0);
                        editing = 0;
                        draw_time_button();
                        draw_status(1);
                    }
                } else if (k & BTN_B) {
                    /* 放弃修改 */
                    put_tile(ROW_CURSOR,
                             tfield == EF_BTN ? COL_BTN : field_col[tfield], 0);
                    memcpy(tf, tf_orig, T_FIELDS);
                    editing = 0;
                    draw_time_line();
                    draw_time_button();
                    draw_status(0);
                }
            } else {
                if (k & BTN_UP) {
                    if (set_focus == F_BACKUP) {
                        set_focus = F_TIME;
                        draw_focus();
                    }
                } else if (k & BTN_DOWN) {
                    if (set_focus == F_TIME) {
                        set_focus = F_BACKUP;
                        draw_focus();
                    }
                } else if (k & BTN_A) {
                    if (set_focus == F_TIME) {
                        /* 进编辑：冻结时间、备份原值、光标停在「年」 */
                        memcpy(tf_orig, tf, T_FIELDS);
                        editing = 1;
                        tfield = 0;
                        draw_time_button();
                        edit_cursor_col(tfield);
                        draw_status(0);
                    } else {
                        auto_backup ^= 1;
                        save_auto_set(auto_backup);
                        draw_backup_state();
                    }
                }
            }
        }

        /* 每 1/4 秒查一次 RTC：秒一变就只重画行 3 的 17 格，不闪 */
        if (tab == TAB_SET && !editing) {
            if (++tick >= 15) {
                tick = 0;
                poll_clock();
            }
        }

        wait_frames(1);
    }
}
