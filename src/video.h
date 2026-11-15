/* video.h — 显示层对外接口（行列均以 8x8 tile 为单位，屏幕 20x18）
 *
 * 字形全部是 8x8：一个 ASCII 字符一格，一个汉字也一格。所以"列"就是"字符数"，
 * 排版可以直接按 20 列算，不需要再折算 2 列/字。
 *
 * tile 编号约定（见 font_data.h）：
 *   $00      空白
 *   $01-$0B  符号
 *   $0C-$1F  汉字动态池 A 段（20 格）
 *   $20-$7E  ASCII（= 字符码）
 *   $7F-$FF  汉字动态池 B 段（129 格）
 */
#ifndef VIDEO_H
#define VIDEO_H

#include <stdint.h>

void video_init(void);                       /* 关 LCD、装字体、清屏、开 LCD */

/* 整屏重绘时关/开 LCD。
 *
 * 为什么需要：LCD 开着时 VRAM 只在非 mode 3 可写。整屏 360 格 + 字库上传
 * 若逐格等 STAT 会跨好几帧、看得见闪烁；关掉 LCD 就没有竞态，也不会闪
 * （黑一两帧而已）。局部改写（一行、几个格子）不关屏，逐格等 STAT 就够快。*/
void video_off(void);
void video_on(void);

void clear_screen(void);
void clear_row(uint8_t row);

/* 单个格子写/擦（tile=0 即擦）。只改一两个格子的场合用它，比
 * 「整行清掉再重画」快得多，也不会让那一行闪一下。*/
void put_tile(uint8_t row, uint8_t col, uint8_t tile);

/* 整行填同一个 tile（画分隔线、底纹）。tile 用 SYM_HLINE / SYM_HEAVY 等。*/
void fill_row(uint8_t row, uint8_t tile);

/* 从 (row,col) 起重复画 n 个同一 tile */
void print_repeat(uint8_t row, uint8_t col, uint8_t tile, uint8_t n);

/* ASCII + 符号：tile 编号等于字符码，符号用 "\x01" 这类控制字符引用。
 * 大小写都保留（fusion-pixel 8px 的小写比强制大写好看）。*/
void print_at(uint8_t row, uint8_t col, const char *s);
void print_center(uint8_t row, const char *s);

/* 中文：str 是 strings.h 的 STR_*（glyph id 序列，0 结尾）。
 * 每个汉字占 1 个 tile，字模按需从 ROM 上传到 VRAM 的 0x80-0xFF 池并缓存 ——
 * 同一屏里重复的字只上传一次。整屏换内容前调 cjk_begin() 复位缓存。*/
void cjk_begin(void);

/* 卡上中文文件名：s 是 UCS-2 码点串（0 结尾），字模由 fnt.c 从 EZJR.FNT 读。
 * 字库不可用 / 字库里缺这个字时画一个方框，不返回失败 —— 名字断一截比整行
 * 消失好。返回占了几列。*/
uint8_t cjk_puts_ucs2(uint8_t row, uint8_t col, const uint16_t *s);
void    cjk_center_ucs2(uint8_t row, const uint16_t *s);
void    cjk_center_ucs2_base(uint8_t row, const uint16_t *s);

/* 备份流程的三屏（共用，模拟器也编）。lfn 可为 NULL / 空串，
 * 那时退回显示过滤过的 8.3 短名。*/
void screen_backup_confirm(const uint16_t *lfn, const char *shortname);
void screen_backing_up(void);
void screen_save_result(uint8_t err);

/* 列表页按 START 弹出的菜单（整屏，调用方先清屏 + 画标签栏）。
 *   has_last = 0    第一项显示「无上次游戏」，下面不画文件名
 *   lfn             上次那个 ROM 的中文长名（可为 NULL）
 *   shortname       8.3 短名，长名画不出来时退回它
 *   is_dir = 1      光标停在文件夹上 → 第二项显示「目录不能删」
 *   extra = 1       多一项「自检」（仅 diag 版用）
 * sel 是当前选中项 0/1/2。上下切换用 menu_cursor() 只挪箭头，别重绘整屏。*/
void screen_menu(uint8_t has_last, const uint16_t *lfn, const char *shortname,
                 uint8_t is_dir, uint8_t sel, uint8_t extra);
void menu_cursor(uint8_t sel, uint8_t extra);
uint8_t cjk_puts(uint8_t row, uint8_t col, const uint8_t *str);   /* 返回占了几列 */

/* 每个字后面空一列。8px 汉字在 8x8 里几乎没有左右留白，两个字紧贴会糊成
 * 一团（尤其「游戏」这类笔画密的），所以标题、标签一律用这个版本；
 * 「2026 年 11 月」这种前后本来就有空格的，用紧凑版省宽度。*/
uint8_t cjk_puts_wide(uint8_t row, uint8_t col, const uint8_t *str);

uint8_t cjk_width(const uint8_t *str);                            /* 字数 */
void cjk_center(uint8_t row, const uint8_t *str);                 /* 紧凑居中 */
void cjk_center_wide(uint8_t row, const uint8_t *str);            /* 带字间距居中 */

/* 两位十进制数（时间/计数用），固定 2 位宽 */
void print_dec2(uint8_t row, uint8_t col, uint8_t v);
void print_u16(uint8_t row, uint8_t col, uint16_t v);
void print_u32(uint8_t row, uint8_t col, uint32_t v);

void wait_frames(uint8_t n);

#endif /* VIDEO_H */
