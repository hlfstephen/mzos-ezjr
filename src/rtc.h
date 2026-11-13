/* rtc.h — PCF8563 实时时钟（经 FPGA 页 6 窗口）*/
#ifndef RTC_H
#define RTC_H

#include <stdint.h>

/* 时间字段索引（bin 值，非 BCD）*/
enum { T_SEC = 0, T_MIN, T_HOUR, T_DAY, T_WDAY, T_MON, T_YEAR, T_FIELDS };

/* 读当前时间到 f[T_FIELDS]。返回 0 正常；非 0 = VL 置位（电池掉过电，
 * 时钟不可信，界面应提示"电池电压低/请设置时间"）。*/
uint8_t rtc_read(uint8_t *f);

/* 读 7 个原始 BCD 寄存器字节（$A008-$A00E，不做换算/掩码）。
 * 自检屏用：真机上要能看到硬件给的原始值，才能判断是 I2C 没通还是换算错。*/
void rtc_raw_block(uint8_t *out);

/* 把 f[] 写回 PCF8563。序列照内核 InitTimeAutosaveFpga_B4：
 * 页 6，写 $A008-$A00E 的 BCD，置 $7FD0=1 触发 PicoBlaze 命令 $10
 * （I2C 写寄存器 2-8），再切回页 0。*/
void rtc_write(const uint8_t *f);

/* BCD <-> bin（0-99）*/
uint8_t rtc_bcd2bin(uint8_t v);
uint8_t rtc_bin2bcd(uint8_t v);

/* 每个字段的合法上限（含），用于时间编辑界面加减回绕 */
uint8_t rtc_field_max(uint8_t idx);

/* 由年月日推算星期，0=周日 … 6=周六（写 PCF8563 weekday 寄存器用）*/
uint8_t rtc_weekday(uint8_t yy, uint8_t mm, uint8_t dd);

/* 自 2000-01-01 起的天数（星期推算与 MBC3 时钟基准共用）*/
uint16_t rtc_days_from_2000(uint8_t yy, uint8_t mm, uint8_t dd);

/* 把 PCF8563 的实时时间写成 FW5 的 MBC3 游戏时钟基准（页 6 的 $A018-$A01C）。
 * 只在启动 MBC3+TIMER 游戏（cart type $0F/$10）之前调用一次。
 * 结束状态是 $7FC0 = 2（官方启动页），VL 置位时内部直接返回、什么都不写。*/
void rtc_base_write(void);

#endif /* RTC_H */
