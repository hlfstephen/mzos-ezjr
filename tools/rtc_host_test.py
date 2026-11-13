#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rtc_host_test.py — 主机侧验证 rtc.c 的 BCD 转换与「设置时间」写回序列。

验证点（都是设置时间能否成立的关键）：
  1) bin<->bcd 往返（0..99）
  2) rtc_read：从页 6 的 $A008-$A00E 读 BCD，屏蔽 VL/高位标志，检测 VL
  3) rtc_write：切页 6 -> 写 7 个 BCD -> $7FD0=1 触发 -> $7FD0=0 -> 切回页 0
     （寄存器序列与内核 InitTimeAutosaveFpga_B4 一致）

做法：把 rtc.c 用 gcc 编译进一个测试桩，mock 掉 fpga_* / WINDOW 内存，
在桩里跑断言并打印结果。WINDOW 由 -DWINDOW=g_win 覆盖为普通数组。
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

STUB = r'''
#include <stdio.h>
#include <string.h>
#include <stdint.h>

/* 让 hw.h 的 WINDOW 指向普通 RAM 数组（覆盖绝对地址 0xA000）*/
static uint8_t g_win[0x100];
#define WINDOW_OVERRIDE g_win
#include "hw.h"
#include "fpga.h"
#include "rtc.h"
#include "rtc.c"      /* 直接内联被测实现 */

/* ---- mock 硬件层 ---- */
static int page_seq[16], page_n;
static int set_seq[32], set_n;   /* 编码: (reg<<8)|value */
void fpga_set(uint16_t reg, uint8_t v){ if(set_n<32) set_seq[set_n++]=(reg<<8)|v; }
void fpga_page(uint8_t p){ if(page_n<16) page_seq[page_n++]=p; }
uint8_t buttons(void){return 0;}
void wait_release(void){}
uint8_t fw_version(void){return 5;}
void wait_frames(uint8_t n){(void)n;}
void sd_read(uint32_t l, uint8_t *d){(void)l; memset(d,0,512);}

#define BCD(v) ((uint8_t)((((v)/10)<<4)|((v)%10)))
static int fails;
static void ck(const char *name, int cond){ printf("  %s %s\n", cond?"OK  ":"FAIL", name); if(!cond) fails++; }

int main(void){
    uint8_t f[7], i, vl;

    /* 1) BCD 往返 */
    int ok=1;
    for(i=0;i<100;i++) if(rtc_bin2bcd(i)!=(uint8_t)(((i/10)<<4)|(i%10))) ok=0;
    for(i=0;i<100;i++){ uint8_t b=(uint8_t)(((i/10)<<4)|(i%10)); if(rtc_bcd2bin(b)!=i) ok=0; }
    ck("bin<->bcd 往返 0..99", ok);

    /* 2) rtc_read：2026-11-11 14:30:05, 星期3, VL=0 */
    WINDOW_OVERRIDE[0x08]=BCD(5);   /* sec  */
    WINDOW_OVERRIDE[0x09]=BCD(30);  /* min  */
    WINDOW_OVERRIDE[0x0A]=BCD(14);  /* hour */
    WINDOW_OVERRIDE[0x0B]=BCD(11);  /* day  */
    WINDOW_OVERRIDE[0x0C]=3;        /* wday */
    WINDOW_OVERRIDE[0x0D]=BCD(11);  /* mon  */
    WINDOW_OVERRIDE[0x0E]=BCD(26);  /* year */
    vl=rtc_read(f);
    ck("rtc_read VL=0", vl==0);
    ck("rtc_read 值正确", f[0]==5&&f[1]==30&&f[2]==14&&f[3]==11&&f[5]==11&&f[6]==26);

    /* 高位标志应被屏蔽 */
    WINDOW_OVERRIDE[0x0A]=0x80|BCD(14);  /* hour 带无关高位 */
    rtc_read(f);
    ck("rtc_read 屏蔽高位", f[2]==14);

    /* VL=1 */
    WINDOW_OVERRIDE[0x08]=0x80|BCD(5);
    vl=rtc_read(f);
    ck("rtc_read VL=1 检出", vl==1);
    WINDOW_OVERRIDE[0x08]=BCD(5);

    /* 3) 星期推算：与真实日历全量差分（期望值由宿主 Python 日历生成）*/
    {
        static const struct { uint8_t y, m, d, w; } c[] = {
@@WEEKDAY_CASES@@
        };
        int n = (int)(sizeof(c)/sizeof(c[0])), bad = 0, shown = 0, k;
        for (k = 0; k < n; k++)
            if (rtc_weekday(c[k].y, c[k].m, c[k].d) != c[k].w) {
                if (shown < 6) {
                    printf("       20%02u-%02u-%02u 得到 %u 期望 %u\n", c[k].y, c[k].m, c[k].d,
                           rtc_weekday(c[k].y, c[k].m, c[k].d), c[k].w);
                    shown++;
                }
                bad++;
            }
        printf("       共 %d 个日期，不符 %d 个\n", n, bad);
        ck("rtc_weekday 全量差分 (2000-2099)", bad == 0 && n >= 3600);
        /* 非法日期不能返回越界值 */
        ck("rtc_weekday 非法日期", rtc_weekday(26, 0, 11) == 0 && rtc_weekday(26, 11, 0) == 0);
    }

    /* 4) rtc_write 寄存器序列 */
    f[0]=5; f[1]=30; f[2]=14; f[3]=11; f[4]=3; f[5]=11; f[6]=26;
    page_n=0; set_n=0;
    rtc_write(f);
    /* 页序列: 6(读前 rtc_write 不读) -> 最后 0；rtc_write 内部 fpga_page(6) 与 fpga_page(0) */
    ck("rtc_write 切页 6 再 0", page_n==2 && page_seq[0]==6 && page_seq[1]==0);
    /* BCD 值写回 $A008-$A00E */
    ok=1;
    for(i=0;i<7;i++) if(WINDOW_OVERRIDE[0x08+i]!=BCD(f[i])) ok=0;
    ck("rtc_write 写回 7 个 BCD", ok);
    /* $7FD0 触发 + 清除 */
    ck("rtc_write 触发 $7FD0=1 再 =0",
       set_n>=2 && set_seq[0]==((0x7FD0<<8)|1) && set_seq[set_n-1]==((0x7FD0<<8)|0));

    /* 5) rtc_base_write：PCF8563 的时间 -> FW5 的 MBC3 时钟基准 */
    {
        static const struct {
            uint8_t y, mo, d, h, mi, s, dl, dh;
        } c[] = {
@@BASE_CASES@@
        };
        int n = (int)(sizeof(c)/sizeof(c[0])), bad = 0, shown = 0, k;

        for (k = 0; k < n; k++) {
            int j, e = 0;

            /* 页 6 摆上 PCF8563 的读数 */
            WINDOW_OVERRIDE[0x08] = BCD(c[k].s);
            WINDOW_OVERRIDE[0x09] = BCD(c[k].mi);
            WINDOW_OVERRIDE[0x0A] = BCD(c[k].h);
            WINDOW_OVERRIDE[0x0B] = BCD(c[k].d);
            WINDOW_OVERRIDE[0x0C] = 0;                  /* weekday，base 不用 */
            WINDOW_OVERRIDE[0x0D] = BCD(c[k].mo);
            WINDOW_OVERRIDE[0x0E] = BCD(c[k].y);
            /* 先把 base 区涂成垃圾，确认它真被写了 */
            for (j = 0x18; j <= 0x1C; j++) WINDOW_OVERRIDE[j] = 0x5A;

            set_n = 0;
            rtc_base_write();
            page_n = 0;                                  /* rtc_base_write 已跑完，看它留下的页 */
            if (WINDOW_OVERRIDE[0x18] != c[k].s ||
                WINDOW_OVERRIDE[0x19] != c[k].mi ||
                WINDOW_OVERRIDE[0x1A] != c[k].h ||
                WINDOW_OVERRIDE[0x1B] != c[k].dl ||
                WINDOW_OVERRIDE[0x1C] != c[k].dh)
                e = 1;
            if (set_n != 0) e = 1;                       /* 不该碰任何 fpga_set 寄存器 */
            if (e && shown < 6) {
                printf("       20%02u-%02u-%02u %02u:%02u:%02u -> "
                       "S%02X M%02X H%02X DL%02X DH%02X 期望 %02X %02X %02X %02X %02X\n",
                       c[k].y, c[k].mo, c[k].d, c[k].h, c[k].mi, c[k].s,
                       WINDOW_OVERRIDE[0x18], WINDOW_OVERRIDE[0x19], WINDOW_OVERRIDE[0x1A],
                       WINDOW_OVERRIDE[0x1B], WINDOW_OVERRIDE[0x1C],
                       c[k].s, c[k].mi, c[k].h, c[k].dl, c[k].dh);
                shown++;
            }
            bad += e;
        }
        printf("       共 %d 个时刻，不符 %d 个\n", n, bad);
        ck("rtc_base_write 时/分/秒 + 天数逐字节正确", bad == 0 && n >= 20);
    }

    /* 5b) 结束页必须是 2（加载命令窗口要求 win_mode[1]=1，0 会让 tbl_win 失效）*/
    {
        WINDOW_OVERRIDE[0x08] = BCD(0);
        WINDOW_OVERRIDE[0x0E] = BCD(26);
        page_n = 0;
        rtc_base_write();
        /* rtc_base_write 内部 fpga_page(6) 与 fpga_page(2) */
        ck("rtc_base_write 切页 6 再回 2", page_n==2 && page_seq[0]==6 && page_seq[1]==2);
    }

    /* 5c) VL 置位：时间本身就是假的，一个字节都不能写 */
    {
        int j, touched = 0;
        WINDOW_OVERRIDE[0x08] = 0x80 | BCD(5);
        WINDOW_OVERRIDE[0x09] = BCD(30);
        WINDOW_OVERRIDE[0x0A] = BCD(14);
        WINDOW_OVERRIDE[0x0B] = BCD(11);
        WINDOW_OVERRIDE[0x0D] = BCD(11);
        WINDOW_OVERRIDE[0x0E] = BCD(26);
        for (j = 0x18; j <= 0x1C; j++) WINDOW_OVERRIDE[j] = 0x5A;
        set_n = 0;
        page_n = 0;
        rtc_base_write();
        for (j = 0x18; j <= 0x1C; j++) if (WINDOW_OVERRIDE[j] != 0x5A) touched = 1;
        ck("VL 置位时 base 区一个字节都不写", touched == 0);
        ck("VL 置位时仍切回页 2", page_n==2 && page_seq[0]==6 && page_seq[1]==2);
        WINDOW_OVERRIDE[0x08] = BCD(5);
    }

    printf("  RESULT %s (%d fails)\n", fails?"FAIL":"PASS", fails);
    return fails?1:0;
}
'''


def weekday_cases():
    """用 Python 的真实日历生成期望值：2000-2099 年，每月 1/15/28 日。
    Python weekday() 是 0=周一，PCF8563 我们约定 0=周日，故 (w+1)%7。"""
    from datetime import date
    rows = []
    for y in range(2000, 2100):
        for m in range(1, 13):
            for d in (1, 15, 28):
                w = (date(y, m, d).weekday() + 1) % 7
                rows.append((y - 2000, m, d, w))
    return rows


def base_cases():
    """MBC3 时钟基准的期望值：自 2000-01-01 起的天数由 Python 日历算，
    不在测试里手抄 —— 闰年规则（2000 是闰年，2100 不是）最容易在这儿错。"""
    from datetime import date
    epoch = date(2000, 1, 1)
    picks = [
        (2000, 1, 1, 0, 0, 0),        # 起点：天数必须是 0
        (2000, 1, 2, 23, 59, 59),     # 第 1 天，时分秒边界
        (2000, 2, 29, 12, 0, 0),      # 2000 是闰年 → 第 59 天
        (2000, 3, 1, 0, 0, 0),        # 闰日之后 → 第 60 天
        (2001, 1, 1, 1, 2, 3),        # 第 366 天（2000 有 366 天）
        (2004, 2, 29, 6, 30, 0),      # 闰年
        (2010, 12, 31, 23, 59, 59),
        (2016, 8, 20, 9, 5, 1),
        (2020, 2, 29, 18, 45, 30),
        (2024, 2, 28, 7, 7, 7),
        (2025, 12, 31, 13, 0, 0),
        (2026, 11, 11, 14, 30, 5),    # 文档里的示例时刻
        (2038, 1, 19, 3, 14, 7),      # 32 位时间戳边界（跟本内核无关，取个远点）
        (2050, 6, 15, 22, 10, 0),
        (2077, 7, 7, 7, 7, 7),
        (2099, 12, 31, 23, 59, 59),   # 区间末尾
        (2021, 3, 1, 0, 0, 0),
        (2021, 2, 28, 23, 59, 59),    # 平年 2 月
        (2100 - 1, 12, 31, 12, 0, 0),
        (2008, 2, 29, 11, 11, 11),
    ]
    rows = []
    for y, m, d, h, mi, s in picks:
        days = (date(y, m, d) - epoch).days
        rows.append((y - 2000, m, d, h, mi, s, days & 0xFF, (days >> 8) & 0x01))
    return rows


def main():
    if not any(os.path.exists(p) for p in ["/usr/bin/gcc", "/usr/bin/cc"]):
        print("  跳过：主机无 gcc")
        return 0
    tmp = tempfile.mkdtemp(prefix="ezjr-rtc-test-")
    stub = os.path.join(tmp, "stub.c")
    # WINDOW 覆盖：让 hw.h 用 g_win。通过 -D 传入 WINDOW=g_win 不行（hw.h 里
    # 有 #ifndef 保护），改在桩里 #define WINDOW g_win 之后再 include。
    src = STUB.replace('#include "hw.h"', '#define WINDOW g_win\n#include "hw.h"')
    # 期望表：由宿主 Python 日历生成，避免手写期望值出错
    rows = weekday_cases()
    case_src = "\n".join("            {%2d, %2d, %2d, %d}," % r for r in rows)
    src = src.replace("@@WEEKDAY_CASES@@", case_src)
    brows = base_cases()
    base_src = "\n".join("            {%2d, %2d, %2d, %2d, %2d, %2d, %3d, %d}," % r for r in brows)
    src = src.replace("@@BASE_CASES@@", base_src)
    open(stub, "w").write(src)

    exe = os.path.join(tmp, "rtctest")
    # 只编译桩（内联了 rtc.c），不另链 rtc.c，避免重复定义
    cmd = ["gcc", "-O1", "-I" + os.path.join(ROOT, "src"), stub, "-o", exe]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("gcc 编译失败:\n" + r.stderr)
        return 1
    out = subprocess.run([exe], capture_output=True, text=True)
    print(out.stdout.strip())
    return out.returncode


if __name__ == "__main__":
    sys.exit(main())
