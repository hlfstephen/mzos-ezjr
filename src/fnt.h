/* fnt.h — 卡上的汉字字库（EZJR.FNT） */
#ifndef FNT_H
#define FNT_H

#include <stdint.h>

/* 开机扫完目录后调一次：找 EZJR.FNT、核对大小、展开簇链。
 * 返回 1 = 可用；0 = 没有这个文件 / 大小不对 / 簇太碎。
 * 不可用时中文文件名退回显示 8.3 短名，其它功能不受影响。*/
uint8_t fnt_open(void);

uint8_t fnt_available(void);

/* 让字模扇区缓存失效。game_work 是共用缓冲，整屏重绘前必须调一次，
 * 否则存档/目录操作覆盖了缓冲，缓存还以为里面是字模。*/
void fnt_flush_cache(void);

/* 读一个字模（码点 -> 8 字节 1bpp）。返回 1 = 读到。
 * 每次调用可能读一个 512 字节扇区，同一扇区内的字不重复读由上层缓存负责。*/
uint8_t fnt_glyph(uint16_t cp, uint8_t *out);

#endif /* FNT_H */
