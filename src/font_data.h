/* 自动生成，请勿手改；由 tools/gen_font.py 生成。 */
/* 字形来自 Fusion Pixel Font 8px（OFL-1.1），见 NOTICE.md。 */
#ifndef FONT_DATA_H
#define FONT_DATA_H

#include <stdint.h>

/* tilemap 里的 tile 编号约定：
 *   $00       空白（清屏/擦除用）
 *   $01-$0B  界面符号，见 cjk_symbols[]，C 里用 "\x01" 引用
 *   $0C-$1F  汉字动态池 A 段（20 格）
 *   $20-$7E  ASCII，编号 = 字符码（大小写都有，不强制转大写）
 *   $7F-$FF  汉字动态池 B 段（129 格）
 * ASCII 能和字符码一一对应，是因为 $20-$7E 恰好 95 个、且紧接在符号区
 * 之后 —— print_at() 不用查表，直接把字符码当 tile 号。
 * 汉字池一共 20+129 = 149 格：中文文件名一屏可能有上百个不同汉字（13 行
 * × 8 字），池小一点就会在翻页时反复作废重传。*/
#define SYM_FIRST      0x01
#define SYM_COUNT      11
#define ASCII_FIRST    0x20
#define ASCII_COUNT    (0x7F - 0x20)
#define CJK_POOL_A     0x0C
#define CJK_POOL_A_N   20
#define CJK_POOL_B     0x7F
#define CJK_POOL_N     149
#define CJK_COUNT      86

/* 每个字形 8 字节 1bpp（bit7 = 最左像素）。GB 的 tile 是 2bpp 16 字节，
 * video.c 上传时把 low/high 两半写成同一个字节，像素值就是颜色 3 —— 
 * 这样 ROM 只占一半，上传也就一个循环。*/

/* 汉字：索引 = glyph id - 1，每个 8 字节 */

/* 每个界面汉字的 Unicode 码点。渲染时按码点做缓存键 —— 这样界面汉字
 * 和卡上中文文件名里的同一个字共用 VRAM 里一个 tile，不会各占一份。*/


/* 字模数据的定义在 src/font_data.c。正式版用 --constseg BANK2 编进扩展 bank
 * （见 build.sh 与 docs/01-architecture.md）；模拟器版和主机测试用默认 const 段。
 * 这里只声明，避免每个 include 它的编译单元各存一份。*/
extern const unsigned char cjk_symbols[SYM_COUNT][8];
extern const unsigned char ascii_font[ASCII_COUNT][8];
extern const unsigned char cjk_font[CJK_COUNT][8];
extern const uint16_t cjk_cp[CJK_COUNT];

#endif /* FONT_DATA_H */
