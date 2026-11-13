/* video.c — LCD 初始化、8x8 字形渲染、汉字动态 tile 池
 *
 * 屏幕 20x18 个 8x8 tile，tilemap 在 $9800，tile 数据在 $8000（LCDC.4=1 的
 * unsigned 寻址，编号 0-255）。
 *
 * tile 分配：
 *   $00        空白（清屏、擦除）
 *   $01-$0B    界面符号（▶ ─ █ ▄ ▀ ▌ ▐ ▲ ▼ ●），启动时一次性装入
 *   $0C-$1F    汉字动态池 A 段（20 格）
 *   $20-$7E    ASCII，编号 = 字符码，启动时一次性装入
 *   $7F-$FF    汉字动态池 B 段（129 格）
 *
 * 汉字池一共 149 格。中文文件名一屏可能上百个不同汉字（13 行 × 8 字），
 * 池小一点就会在翻页时反复作废重传。
 *
 * 为什么汉字能一个格子放一个：字形来自 Fusion Pixel Font 8px（OFL-1.1），
 * 是专为 8x8 设计的像素字体。原来用 Noto CJK 缩到 16x16，一个字要 4 个 tile、
 * 64 字节 ROM、2x2 格位置 —— 一屏只放得下 32 个汉字。现在 1 格 / 8 字节，
 * 一屏 360 格全可用，ROM 里 36 个字只占 288 字节。
 *
 * 写 VRAM 的两种方式：
 *   video_off() 关屏后写 —— 无竞态，用于整屏换内容（360 格 + 字库上传）；
 *   开着屏写 —— 每格等 mode 3 结束（put），一行 20 格不到 0.2 帧，够快也不会丢。
 *   不要贪心「等一次 VBlank 然后连写」：实测写三十几个格子就会在 VBlank 末尾
 *   丢掉中间几个字节，屏幕上是「TETRI .G」这种断字。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "video.h"
#include "fnt.h"
#include "font_data.h"
#include "strings.h"
#include "fat.h"

#define TILE_BASE   ((uint8_t *)0x8000)
#define MAP_BASE    ((uint8_t *)0x9800)
#define SCR_W       20
#define SCR_H       18

/* 池里每个槽对应的 Unicode 码点，0 = 空槽。
 *
 * 为什么用码点而不是 glyph id 做键：界面汉字和卡上中文文件名里的同一个字
 * 应该共用 VRAM 里一个 tile。界面汉字的码点在 cjk_cp[]（ROM），中文名直接
 * 从目录项里读出码点，两者一视同仁。
 *
 * 149 项 × 2 字节 = 298 字节 WRAM。查找是线性的（最多 149 次比较）——
 * 一屏十来个界面汉字时几乎瞬完；中文名一屏七八十个字，翻页时也就几千次
 * 比较，换翻页那一两帧完全看不出。*/
static uint16_t pool_cp[CJK_POOL_N];
static uint8_t  pool_next;              /* 池内序号 0..CJK_POOL_N-1 */

/* 字库和字体里都没有的字，画个空心小方框 —— 比留白更容易看出"这里有个字"。*/
static const uint8_t glyph_unknown[8] = {
    0x7E, 0x42, 0x42, 0x42, 0x42, 0x42, 0x7E, 0x00
};

/* 汉字 tile 池复位（video_init 和 cjk_begin 都用）*/
static void gid_tile_init(void)
{
    memset(pool_cp, 0, sizeof pool_cp);
    pool_next = 0;
}

/* 池内序号 -> tile 号。池被 ASCII 区劈成两段，所以要映射一下：
 * 0..19 -> $0C-$1F，20..52 -> $7F-$9F。*/
static uint8_t pool_slot(uint8_t n)
{
    if (n < CJK_POOL_A_N)
        return (uint8_t)(CJK_POOL_A + n);
    return (uint8_t)(CJK_POOL_B + (n - CJK_POOL_A_N));
}

/* ---------------- VRAM 写入的原语 ---------------- */

/* 单格写 VRAM：LCD 开着时等 mode 3 结束再写。
 *
 * 为什么不用「等 VBlank 再连写」：试过，会在 VBlank 末尾丢掉中间几个字节
 * （写 30 多个格子就超时了，屏幕上是「TETRI .G」这种断字）。逐格等 STAT
 * 每次最多等 114 个周期，一行 20 格不到 0.2 帧，比等整帧还快。
 *
 * 整屏 360 格 + 字库上传仍然走 video_off()：那个量级逐格等会跨好几帧。*/
/* 单格写 VRAM：LCD 开着时等 VBlank（mode 1）再写。
 *
 * 为什么要等 mode 1，而不是「等 mode 3（VRAM 占用）结束」：
 * mode 3 之后是 HBlank，只有 204 个周期；从读到 STAT 到字节真正落进
 * VRAM 要 20 多个周期。如果读 STAT 的那一刻已经走到 HBlank 的末尾，
 * 写入就掉进下一个 mode 3，被硬件整格吞掉 —— 实测 18 格丢 2 格，屏幕上
 * 是「PO EMON.GB」这种断字，而且丢的位置每次一样（时序是确定的）。
 *
 * VBlank 有 1140 个周期，后面紧跟的 mode 2（OAM 扫描）也允许访问 VRAM，
 * 所以从 VBlank 里起手连写绝不会踩到 mode 3；一次 VBlank 大约写得下
 * 45 格，清一行（20 格）绰绰有余，清两行加写字最多跨两帧，肉眼看不出。
 *
 * LCD 关着时没有 mode 1，会死等，所以先看一眼 LCDC。
 * 整屏 360 格仍然走 video_off()：那个量级逐格等会跨七八帧。*/
static void put(uint8_t *p, uint8_t c)
{
    if (rLCDC & 0x80)
        while ((rSTAT & 3) != 1)
            ;
    *p = c;
}

/* 上传一个 8x8 1bpp 字形到 tile t（展开成 2bpp：low = high = 位图，
 * 像素值就是颜色 3 即最深色）。调用者保证在 VBlank 或关屏状态。*/
static void vram_tile_ex(uint8_t t, const uint8_t *src, uint8_t dim)
{
    uint8_t *d = TILE_BASE + ((uint16_t)t << 4);
    uint8_t i, b;

    for (i = 0; i < 8; i++) {
        b = src[i];
        if (dim) {
            put(d + (i << 1), 0);       /* low = 0, high = 位图 -> 像素值 2 */
            put(d + (i << 1) + 1, b);
        } else {
            put(d + (i << 1), b);       /* low = high = 位图 -> 像素值 3 */
            put(d + (i << 1) + 1, b);
        }
    }
}

static void vram_tile(uint8_t t, const uint8_t *src)
{
    vram_tile_ex(t, src, 0);
}

/* ---------------- 初始化 ---------------- */

void video_init(void)
{
    uint8_t i;

    if (rLCDC & 0x80) {
        while (rLY != 144)
            ;
    }
    rLCDC = 0;
    rSCY = 0;
    rSCX = 0;
    rSTAT = 0;
    rWY = 0;
    rWX = 7;
    rBGP = 0xE4;                    /* 00 -> 白, 11 -> 黑 */

    memset((void *)0x8000, 0, 0x2000);  /* 整个 VRAM：tile 数据 + 两片 tilemap */

    for (i = 0; i < SYM_COUNT; i++)
        vram_tile((uint8_t)(SYM_FIRST + i), cjk_symbols[i]);
    for (i = 0; i < ASCII_COUNT; i++)
        vram_tile((uint8_t)(ASCII_FIRST + i), ascii_font[i]);

    memset((void *)0x9800, 0, 0x800);
    memset((void *)0x9C00, 0, 0x800);

    gid_tile_init();
    rLCDC = 0x91;                   /* LCD on, BG tile $8000, BG 显示 */
}

void video_off(void)
{
    rLCDC = 0;
}

void video_on(void)
{
    rLCDC = 0x91;
}

/* ---------------- 整行/整屏 ---------------- */

void fill_row(uint8_t row, uint8_t tile)
{
    uint8_t *p = MAP_BASE + (uint16_t)row * 32;
    uint8_t x;

    for (x = 0; x < SCR_W; x++)
        put(p + x, tile);
}

void clear_row(uint8_t row)
{
    fill_row(row, 0);
}

/* 单个格子的写入/擦除。光标移动这类「只变一格」的操作走这里 ——
 * 一次只写 1 个字节，必定落在同一个 VBlank 里，屏幕上一点动静都没有。*/
void put_tile(uint8_t row, uint8_t col, uint8_t tile)
{
    if (row >= SCR_H || col >= SCR_W)
        return;
    put(MAP_BASE + (uint16_t)row * 32 + col, tile);
}

void clear_screen(void)
{
    uint8_t r;
    /* 菜单是反色的（黑底白字）；任何整屏重绘都把调色板收回正常态，
     * 只有 draw_menu() 在清屏之后才切过去。放在这里一处管，省得每个
     * 画屏函数各写一遍，也不会出现「菜单关掉了屏还是黑的」。*/
    rBGP = 0xE4;
    for (r = 0; r < SCR_H; r++)
        fill_row(r, 0);
}

void print_repeat(uint8_t row, uint8_t col, uint8_t tile, uint8_t n)
{
    uint8_t *p = MAP_BASE + (uint16_t)row * 32 + col;
    uint8_t x;

    for (x = 0; x < n && col + x < SCR_W; x++)
        put(p + x, tile);
}

/* ---------------- ASCII / 符号 ---------------- */

/* 字符 -> tile 编号。符号（$01-$0B）和 ASCII（$20-$7E）都直接用字符码。*/
static uint8_t tile_of(char c)
{
    uint8_t v = (uint8_t)c;

    if (v >= ASCII_FIRST && v < ASCII_FIRST + ASCII_COUNT)
        return v;
    if (v >= SYM_FIRST && v < SYM_FIRST + SYM_COUNT)
        return v;
    return (uint8_t)'?';
}

void print_at(uint8_t row, uint8_t col, const char *s)
{
    uint8_t *p = MAP_BASE + (uint16_t)row * 32 + col;
    char c;

    if (col >= SCR_W)
        return;
    while ((c = *s++) && col < SCR_W) {
        put(p++, tile_of(c));
        col++;
    }
}

void print_center(uint8_t row, const char *s)
{
    uint8_t len = 0;
    const char *q = s;

    while (*q++) len++;
    clear_row(row);
    if (len <= SCR_W)
        print_at(row, (uint8_t)((SCR_W - len) / 2), s);
    else
        print_at(row, 0, s);
}

void print_dec2(uint8_t row, uint8_t col, uint8_t v)
{
    uint8_t *p;

    if (col >= SCR_W)
        return;
    p = MAP_BASE + (uint16_t)row * 32 + col;
    put(p, (uint8_t)('0' + (v / 10) % 10));
    if (col + 1 < SCR_W)
        put(p + 1, (uint8_t)('0' + v % 10));
}

void print_u16(uint8_t row, uint8_t col, uint16_t v)
{
    uint8_t buf[5], n = 0, i;
    uint8_t *p;

    if (v == 0) buf[n++] = '0';
    while (v) { buf[n++] = (uint8_t)('0' + v % 10); v /= 10; }
    if (col >= SCR_W)
        return;
    p = MAP_BASE + (uint16_t)row * 32 + col;
    for (i = 0; i < n && col + i < SCR_W; i++)
        put(p + i, buf[n - 1 - i]);
}

void print_u32(uint8_t row, uint8_t col, uint32_t v)
{
    uint8_t buf[10], n = 0, i;
    uint8_t *p;

    if (v == 0) buf[n++] = '0';
    while (v) { buf[n++] = (uint8_t)('0' + (uint8_t)(v % 10UL)); v /= 10UL; }
    if (col >= SCR_W)
        return;
    p = MAP_BASE + (uint16_t)row * 32 + col;
    for (i = 0; i < n && col + i < SCR_W; i++)
        put(p + i, buf[n - 1 - i]);
}

/* ---------------- 汉字 ---------------- */

void cjk_begin(void)
{
    gid_tile_init();
    fnt_flush_cache();      /* game_work 可能被上一步的存档/目录操作改过 */
}

/* 码点 -> tile 号。没上传过就上传：rom 非空时用 ROM 里的点阵（界面汉字，
 * 最快），否则去卡上的字库里读（中文文件名）。返回 0 表示没有这个字形。*/
static uint8_t pool_get(uint16_t cp, const uint8_t *rom)
{
    uint8_t i, t;
    uint8_t g[8];

    if (cp == 0)
        return 0;
    for (i = 0; i < pool_next; i++)         /* 本屏已经传过，直接复用 */
        if (pool_cp[i] == cp)
            return pool_slot(i);

    if (pool_next >= CJK_POOL_N) {          /* 池满：整体作废重来，宁慢不乱 */
        memset(pool_cp, 0, sizeof pool_cp);
        pool_next = 0;
    }

    if (!rom) {
        if (!fnt_glyph(cp, g))
            return 0;
        rom = g;
    }
    pool_cp[pool_next] = cp;
    t = pool_slot(pool_next);
    pool_next++;
    vram_tile(t, rom);
    return t;
}

/* 界面汉字：码点从 cjk_cp[] 查到，点阵就在 ROM 里 */
static uint8_t cjk_tile(uint8_t id)
{
    if (id < 1 || id > CJK_COUNT)
        return 0;
    return pool_get(cjk_cp[id - 1], cjk_font[id - 1]);
}

uint8_t cjk_width(const uint8_t *str)
{
    uint8_t n = 0;
    while (*str++) n++;
    return n;
}

uint8_t cjk_puts(uint8_t row, uint8_t col, const uint8_t *str)
{
    uint8_t *p = MAP_BASE + (uint16_t)row * 32 + col;
    uint8_t id, n = 0;

    if (col >= SCR_W)
        return 0;
    while ((id = *str++) && col < SCR_W) {
        uint8_t t = cjk_tile(id);
        if (!t)
            break;
        put(p++, t);                /* 一个汉字占一格 */
        col++;
        n++;
    }
    return n;
}

uint8_t cjk_puts_wide(uint8_t row, uint8_t col, const uint8_t *str)
{
    uint8_t *p = MAP_BASE + (uint16_t)row * 32 + col;
    uint8_t id, n = 0;

    if (col >= SCR_W)
        return 0;
    while ((id = *str++) && col < SCR_W) {
        uint8_t t = cjk_tile(id);
        if (!t)
            break;
        put(p, t);
        p += 2;                     /* 字 + 一列留白 */
        col = (uint8_t)(col + 2);
        n++;
    }
    return n;
}

void cjk_center(uint8_t row, const uint8_t *str)
{
    uint8_t n = cjk_width(str);

    clear_row(row);
    if (n <= SCR_W)
        cjk_puts(row, (uint8_t)((SCR_W - n) / 2), str);
    else
        cjk_puts(row, 0, str);
}

void cjk_center_wide(uint8_t row, const uint8_t *str)
{
    uint8_t n = cjk_width(str);
    uint8_t w = (uint8_t)(n * 2 - 1);        /* 最后一个字后面不留白 */

    clear_row(row);
    if (w <= SCR_W)
        cjk_puts_wide(row, (uint8_t)((SCR_W - w) / 2), str);
    else
        cjk_puts_wide(row, 0, str);
}

/* ---- 卡上中文文件名（UCS-2 码点串）----
 *
 * 字模不在 ROM 里，得靠 fnt_glyph() 从 SD 卡的字库文件读。字库不可用时返回
 * 0，调用者退回显示 8.3 短名。*/
uint8_t cjk_puts_ucs2(uint8_t row, uint8_t col, const uint16_t *s)
{
    uint8_t *p = MAP_BASE + (uint16_t)row * 32 + col;
    uint8_t n = 0;
    uint16_t cp;

    if (col >= SCR_W)
        return 0;
    while ((cp = *s++) != 0 && col < SCR_W) {
        uint8_t t;

        /* ASCII 直接拿字符码当 tile 号。中文名里常混着 "K 口袋妖怪" 这种：
         * 以前把 'K' 和空格也送进汉字字库（EZJR.FNT 只收 U+4E00-U+9FFF），
         * 找不到就画成空白，屏幕上 "K 口袋妖怪" 于是变成 "  口袋妖怪"。*/
        if (cp >= 0x20 && cp <= 0x7E) {
            t = (uint8_t)cp;
        } else {
            t = pool_get(cp, 0);
            if (!t)
                t = (uint8_t)'?';       /* 字库里没这个字：问号，别留空白 */
        }
        put(p++, t);
        col++;
        n++;
    }
    return n;
}

/* 居中显示一串 UCS-2（中文文件名）。超过一行就从左边界开始，自然截断。*/
void cjk_center_ucs2(uint8_t row, const uint16_t *s)
{
    uint8_t len = 0;
    const uint16_t *q = s;

    while (*q++ && len < SCR_W) len++;
    clear_row(row);
    if (len >= SCR_W)
        cjk_puts_ucs2(row, 0, s);
    else
        cjk_puts_ucs2(row, (uint8_t)((SCR_W - len) / 2), s);
}

/* ---- 备份流程的画面 ----
 * 放在 video.c 而不是 save.c，是因为模拟器（EZJR_TEST_STUB）不编 save.c，
 * 但备份屏的排版同样需要在 PyBoy 里核对、出截图。真机走的是同一份代码。
 *
 * 确认屏是一整屏；「进度 → 完成」是**同一屏**：screen_backing_up() 画好
 * BACKING UP... 后不清屏，screen_save_result() 只在它下一行补结果，避免
 * 备份结束时闪一下换成另一屏。*/
void screen_backup_confirm(const uint16_t *lfn, const char *shortname)
{
    cjk_begin();
    video_off();
    clear_screen();
    cjk_center_wide(5, STR_BACKUP_Q);       /* 备份存档 */

    if (lfn && lfn[0]) {
        cjk_center_ucs2(8, lfn);            /* 中文全名 */
    } else {
        /* 没有长名时把 8.3 短名里的非 ASCII 字节换成空格。中文名生成的
         * 短名常含 0x81 之类，原样画出来就是 "K?????~1" 那串乱码。*/
        char    clean[13];
        uint8_t i, n = 0;
        for (i = 0; i < 12 && shortname[i]; i++)
            clean[n++] = (shortname[i] >= 0x20 && shortname[i] <= 0x7E)
                         ? shortname[i] : ' ';
        clean[n] = 0;
        print_center(8, clean);
    }
    print_at(11, 8, "A=");
    cjk_puts(11, 10, STR_BACKUP_BTN);       /* 备份 */
    print_at(13, 8, "B=");
    cjk_puts(13, 10, STR_SKIP_BTN);         /* 跳过 */
    video_on();
}

void screen_backing_up(void)
{
    cjk_begin();                    /* 汉字按需上传，换一屏文案要重新 begin */
    video_off();
    clear_screen();
    cjk_center(8, STR_BACKING_UP);   /* 正在备份 */
    video_on();
}

/* 备份结果**不换画面**：screen_backing_up() 画的那行「正在备份」留在行 8，
 * 这里只在下面补结果。用 cjk_center 逐行局部改写（内部逐格等 VBlank），
 * 不清屏、不重绘整屏 —— 玩家看到的是同一屏上多出一行。
 * 结果放行 10 而不是紧贴的 9：8px 汉字上下几乎顶满格子，行 8/9 挨着会
 * 糊成一坨；隔一行才看得清是两句话。
 * 成功只加「完成」；失败多给一行原因。
 *
 * 原因用汉字而不是错误码：正式版没有自检屏，ERR=05 对玩家等于没说。
 * 需要精确码时用 --diag 版，那里有完整的ERR 与字段读数。*/
void screen_save_result(uint8_t err)
{
    if (err) {
        cjk_center(10, STR_BACKUP_FAIL);         /* 备份失败 */
        if (err == FAT_ERR_VERIFY)
            cjk_center(12, STR_ERR_VERIFY);      /* 校验失败 */
        else if (err == FAT_ERR_NO_SPACE)
            cjk_center(12, STR_ERR_NOSPACE);     /* 空间不足 */
        else if (err == FAT_ERR_ADD_DIR)
            cjk_center(12, STR_ERR_DIRFULL);     /* 目录已满 */
        else
            cjk_center(12, STR_ERR_UNKNOWN);     /* 未知错误 */
    } else {
        cjk_center(10, STR_FINISHED);            /* 完成 */
    }
}

/* ---------------- 列表页的 START 菜单 ----------------
 *
 * 整屏替换而不是盖在列表上：20x18 的屏幕里塞一个六行的菜单，下面露出半截
 * 列表反而更乱。调用方负责清屏、画标签栏，这里只画菜单本体。
 *
 * v0.12.0 按真机反馈重做了一遍，三条改动：
 *   ① 不再整屏反色。黑底白字跟列表页是两个世界，看着像进了另一个程序；
 *      现在跟列表同一套白底黑字，只靠上下两条细线和缩进区分层级。
 *   ② 去掉左右两根竖线。它们是为了「框住」反色块才画的，既然不反色了，
 *      留着就是两条没有来由的竖杠，还把 20 列挤成 18 列。
 *   ③ 上下两条线只画中间 16 列（col 2-17），不顶到屏幕边缘；线本身用
 *      SYM_HLINE（1px 细线）而不是 SYM_HEAVY。
 *
 * 行 8 是「上次运行的游戏」的文件名 —— 光看菜单项不知道会启动哪个游戏，
 * 而按错一下就要重走一遍开机流程，所以把它挂在项下面。它跟同级菜单项的区别
 * 靠三点：缩进到第 6 列（菜单项从第 2 列起）、行首「空格─」引导、上下都不画
 * 框线（框线是用户点名要去掉的 —— 那两根线夹出来的更像一个输入框）。*/
/* 行距是 2 而不是 1：8px 汉字的字形在 8x8 格里几乎顶满，隔一行还能看出
 * 是两句话，紧挨着就糊成一坨（备份屏的行 8/9 也是因为这个才改成 10/12）。*/
#define MENU_TOP    4               /* 顶上的细线 */
#define MENU_ITEM1  6               /* 运行上次游戏 */
#define MENU_NAME   8               /* 上一项的文件名（空格─ 引导 + 缩进 6 列）*/
#define MENU_ITEM2 10               /* 删除当前文件 */
#define MENU_ITEM3 12               /* 自检（仅 diag 版多这一项）*/
#define MENU_RULE_C 2               /* 上下细线的起点列 */
#define MENU_RULE_N 16              /* 上下细线的长度（20 列里留 2 列边）*/
void screen_menu(uint8_t has_last, const uint16_t *lfn, const char *shortname,
                 uint8_t is_dir, uint8_t sel, uint8_t extra)
{
    uint8_t bottom = extra ? (uint8_t)(MENU_ITEM3 + 2) : (uint8_t)(MENU_ITEM2 + 2);
    uint8_t foot   = (uint8_t)(bottom + 2);

    print_repeat(MENU_TOP, MENU_RULE_C, SYM_HLINE, MENU_RULE_N);
    print_repeat(bottom,   MENU_RULE_C, SYM_HLINE, MENU_RULE_N);

    clear_row(MENU_ITEM1);
    clear_row(MENU_NAME);
    clear_row(MENU_ITEM2);
    clear_row(foot);
    if (extra)
        clear_row(MENU_ITEM3);

    /* 项 1：运行上次游戏（没记录时改文案，按了也没事）*/
    cjk_puts_wide(MENU_ITEM1, 2, has_last ? STR_LAST_RUN : STR_NO_LAST);
    if (has_last) {
        uint8_t w = 0;

        /* 文件名是**上一项的附属信息**，不是同级菜单项。三个手段一起用：
         *   ① 行首「空格─」引导符号（col2 留空、col3 一条横线）；
         *   ② 缩进到第 6 列（菜单项从第 2 列起）；
         *   ③ 上下不画任何框线 —— v0.11.0 那两根线夹出来的更像一个输入框，
         *      按真机反馈去掉了。*/
        put_tile(MENU_NAME, 3, SYM_HLINE);          /* 「空格─」：col2 留空、col3 是横线 */

        if (lfn && lfn[0])
            w = cjk_puts_ucs2(MENU_NAME, 6, lfn);   /* 中文全名 */
        if (!w && shortname) {
            /* 没有长名（或字库不在，一个都画不出来）：退回 8.3 短名，
             * 但把非 ASCII 字节换成空格 —— 中文名生成的短名常含 0x81 之类，
             * 原样画出来是一串乱码。*/
            uint8_t i = 0;
            char    clean[19];

            while (shortname[i] && i < 18) {
                clean[i] = (shortname[i] >= 0x20 && shortname[i] <= 0x7E)
                           ? shortname[i] : ' ';
                i++;
            }
            clean[i] = 0;
            print_at(MENU_NAME, 6, clean);
        }
    }

    /* 项 2：删除当前文件。光标在文件夹上时改成「目录不能删」——
     * 直接说结果，比「删除」按下去没反应强。*/
    cjk_puts_wide(MENU_ITEM2, 2, is_dir ? STR_CANT_DEL : STR_DELETE_ROM);

    if (extra)
        cjk_puts_wide(MENU_ITEM3, 2, STR_SELF_TEST);   /* 自检（仅 diag 版）*/

    print_at(foot, 7, "B");
    cjk_puts_wide(foot, 9, STR_BACK);                  /* 返回 */

    menu_cursor(sel, extra);
}

/* 只移动 ▶：菜单上下切换时调它，三格 put_tile 就够，不重绘整屏。
 * extra=1 时多一项，最后一行（自检）也有自己的箭头位置。*/
void menu_cursor(uint8_t sel, uint8_t extra)
{
    /* 选中项的列 0 画 ▶，其余清空（不再画左边框竖线）。*/
    put_tile(MENU_ITEM1, 0, 0);
    put_tile(MENU_ITEM2, 0, 0);
    if (extra)
        put_tile(MENU_ITEM3, 0, 0);
    if (sel == 0)      put_tile(MENU_ITEM1, 0, SYM_ARROW);
    else if (sel == 1) put_tile(MENU_ITEM2, 0, SYM_ARROW);
    else if (extra)    put_tile(MENU_ITEM3, 0, SYM_ARROW);
}

/* ---------------- 帧同步 ---------------- */

void wait_frames(uint8_t n)
{
    while (n--) {
        while (rLY == 144)
            ;
        while (rLY != 144)
            ;
    }
}
