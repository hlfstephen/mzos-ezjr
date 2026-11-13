/* rtc.c — PCF8563 读写
 *
 * 硬件依据（上游 lukesau/ezgb，均 AGPL-3.0）：
 *  - 页 6（$7FC0=6）下 $A008-$A00E = s,m,h,day,weekday,month,year（BCD）。
 *    见 kernel/src/fat_date.c 的 RTC_WIN 与 re/stage0 反汇编。
 *  - 秒寄存器 bit7 = VL（Voltage Low，电池掉电后时钟完整性丢失）。
 *  - 写回：gb_regs.v 的 rtc_set_wr（$A008-$A00E 写入锁存到 rtc_set_*），
 *    $7FD0 bit0 -> rtc_set_go -> pb_periph.v 中断 -> PicoBlaze 命令 $10
 *    (cmd_set_rtc) -> I2C 写 PCF8563 寄存器 2-8，数据来自 RTC_SET_DATA。
 *    内核对应实现：InitTimeAutosaveFpga_B4 / DrawTimeAutosaveScreen 的确认分支。
 */
#include "hw.h"
#include "fpga.h"
#include "rtc.h"

/* $A000 窗口在真实硬件上由 FPGA 给出；模拟器（PyBoy）里没有 FPGA，读写
 * 都不会落到 PCF8563。为让 tools/emu_test.py 能在模拟器里验证「设置时间」
 * 的完整路径，测试构建把窗口换成一块普通 RAM 镜像，由测试脚本按符号地址
 * 填入/读回。真实构建（不定义 EZJR_TEST_STUB）完全走 WINDOW。*/
#ifdef EZJR_TEST_STUB
extern uint8_t g_test_rtc_win[32];      /* 定义在 src/test_stub.c */
#define RTCWIN g_test_rtc_win
#else
#define RTCWIN WINDOW
#endif

uint8_t rtc_bcd2bin(uint8_t v)
{
    return (uint8_t)((v >> 4) * 10 + (v & 0x0F));
}

uint8_t rtc_bin2bcd(uint8_t v)
{
    return (uint8_t)(((v / 10) << 4) | (v % 10));
}

uint8_t rtc_read(uint8_t *f)
{
    uint8_t i, vl;
    static const uint8_t offs[T_FIELDS] = {0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E};

    fpga_page(6);
    vl = RTCWIN[0x08] & 0x80;            /* VL 标志 */
    for (i = 0; i < T_FIELDS; i++) {
        uint8_t v = RTCWIN[offs[i]];
        /* 各字段屏蔽掉非 BCD 的高位标志 */
        if (i == T_SEC)  v &= 0x7F;     /* 去 VL */
        if (i == T_MIN)  v &= 0x7F;
        if (i == T_HOUR) v &= 0x3F;
        if (i == T_DAY)  v &= 0x3F;
        if (i == T_WDAY) v &= 0x07;
        if (i == T_MON)  v &= 0x1F;
        f[i] = rtc_bcd2bin(v);
    }
    fpga_page(0);
    return vl ? 1 : 0;
}

void rtc_raw_block(uint8_t *out)
{
    uint8_t i;
    fpga_page(6);
    for (i = 0; i < 7; i++)
        out[i] = RTCWIN[0x08 + i];
    fpga_page(0);
}

void rtc_write(const uint8_t *f)
{
    uint8_t i;
    static const uint8_t offs[T_FIELDS] = {0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E};

    fpga_page(6);
    for (i = 0; i < T_FIELDS; i++)
        RTCWIN[offs[i]] = rtc_bin2bcd(f[i]);
    /* 触发 PicoBlaze 写 PCF8563：独立于页切换，必须在页 6 之后立即做 */
    fpga_set(FPGA_RTC_GO, 1);
    wait_frames(2);                      /* 给 I2C 写一点时间 */
    fpga_set(FPGA_RTC_GO, 0);
    fpga_page(0);
}

uint8_t rtc_field_max(uint8_t idx)
{
    switch (idx) {
        case T_SEC:  return 59;
        case T_MIN:  return 59;
        case T_HOUR: return 23;
        case T_DAY:  return 31;          /* 简化：不按月份天数校验 */
        case T_WDAY: return 6;
        case T_MON:  return 12;          /* 1..12 */
        case T_YEAR: return 99;          /* 2000..2099 */
        default:     return 0;
    }
}

/* 自 2000-01-01 起的天数。星期推算和 MBC3 的时钟基准（DL/DH）都从这张表
 * 出发，所以抽出来共用，省得两处闰年规则跑偏。
 * 2000–2099 区间内闰年规则退化为「能被 4 整除」（2100 不在范围内）。*/
uint16_t rtc_days_from_2000(uint8_t yy, uint8_t mm, uint8_t dd)
{
    /* 必须 uint16_t：月偏移最大 334，uint8_t 会截断成 78（曾经踩过的坑）*/
    static const uint16_t moff[12] = {0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334};
    uint16_t days;

    if (mm < 1 || mm > 12 || dd < 1 || dd > 31)
        return 0;
    days = (uint16_t)((uint16_t)yy * 365u + ((uint16_t)yy + 3u) / 4u);
    days = (uint16_t)(days + moff[mm - 1] + (dd - 1));
    if (mm > 2 && (yy % 4) == 0)
        days++;
    return days;
}

/* 星期：0=周日 … 6=周六（PCF8563 的 weekday 是用户自定义编号，用天数折算）。
 * 2000-01-01 是周六，故 days=0 对应 6。*/
uint8_t rtc_weekday(uint8_t yy, uint8_t mm, uint8_t dd)
{
    if (mm < 1 || mm > 12 || dd < 1 || dd > 31)
        return 0;                       /* 非法日期不进换算（days 会是 0，星期就成了"周六"）*/
    return (uint8_t)((rtc_days_from_2000(yy, mm, dd) + 6u) % 7u);
}

/* ---- FW5 游戏时钟的「基准」（MBC3+TIMER 游戏用）----------------------------
 *
 * 背景（ezjr-104c-fw5-rtc-fix/docs/01-root-cause.md）：FW5 固件给游戏提供的
 * 时钟是 **base + elapsed** —— PicoBlaze 盯着 PCF8563 的秒跳变推进 elapsed，
 * 而 base 只在上电时由内核写入，默认是 0。内核不写 base，游戏读到的就是
 * 「从开机起过了多久」：水晶的时钟会停在 2000-01-01，或者直接从 0 起算。
 *
 * 写哪里：页 6 的 $A018-$A01C = S / M / H / DL / DH（MBC3 的 5 个时钟寄存器）。
 *   `mbc3_clock.v` 的 `k_wr = rtc_set_wr & a4` —— a4 就是地址第 4 位，所以
 *   $A018 与 $A008（PCF8563 读数）在同一个页上，只是解码到不同的目标。
 *   ⚠️ $A010-$A014 是 $A018 的别名（k_sel 不看 a3），但**别的页**上 $A010
 *   是存档路径缓冲区，所以必须先切页 6 再写。
 *
 * 什么时候调用：启动游戏序列里、$7FC0 还是 6 之前/之后都行，用完必须切回 2 ——
 * 加载命令窗口要求 `win_mode[1]=1`（页 2 或 6 都满足），但官方启动序列用的是 2，
 * 切回 0 会让 `tbl_win` 失效（gb_bus.v:55）。
 *
 * VL（秒寄存器 bit7）置位时什么都不写：那说明电池掉过电、时间本身就是假的，
 * 把 2000-01-01 写进去只会让游戏以为"现在是 2000 年"，不如让它从头算。*/
void rtc_base_write(void)
{
    uint8_t  s, m, h, dd, mm, yy, v;
    uint16_t days;

    fpga_page(6);

    v = RTCWIN[0x08];
    if (v & 0x80) {
        fpga_page(2);
        return;
    }

    s  = rtc_bcd2bin((uint8_t)(v & 0x7F));
    m  = rtc_bcd2bin((uint8_t)(RTCWIN[0x09] & 0x7F));
    h  = rtc_bcd2bin((uint8_t)(RTCWIN[0x0A] & 0x3F));
    dd = rtc_bcd2bin((uint8_t)(RTCWIN[0x0B] & 0x3F));
    mm = rtc_bcd2bin((uint8_t)(RTCWIN[0x0D] & 0x1F));
    yy = rtc_bcd2bin(RTCWIN[0x0E]);

    days = rtc_days_from_2000(yy, mm, dd);

    RTCWIN[0x18] = (uint8_t)(s & 0x3F);          /* S  6 位 */
    RTCWIN[0x19] = (uint8_t)(m & 0x3F);          /* M  6 位 */
    RTCWIN[0x1A] = (uint8_t)(h & 0x1F);          /* H  5 位 */
    RTCWIN[0x1B] = (uint8_t)(days);              /* DL day[7:0] */
    RTCWIN[0x1C] = (uint8_t)((days >> 8) & 0x01);/* DH bit0=day[8]，halt/carry 都清 0 */

    fpga_page(2);                                /* ★ 官方启动页，不能是 0 */
}
