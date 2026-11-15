/* hw.h — Game Boy 与 EZ-FLASH Junior 的硬件寄存器定义
 *
 * 寄存器地址是硬件事实，参考上游 lukesau/ezgb 的 stage1/src/hw.h、
 * docs/REGISTERS.md 与 stage0/fw5-0918/gb_regs.v（均为 AGPL-3.0）。
 * 本文件的宏与注释按本工程需要重新组织。
 */
#ifndef HW_H
#define HW_H

#include <stdint.h>

/* 主机侧测试可以用 -D'REG(a)=...' 覆盖，把 $FFxx/$7Fxx 映射到一块 RAM */
#ifndef REG
#define REG(a) (*(volatile uint8_t *)(a))
#endif

/* $2000：kernel mode 下是 ROM bank 的 8 位锁存（见 docs/01-architecture.md）。
 * 写 N 就把 bank N 映射到 $4000-$7FFF；bank0（$0000-$3FFF）永远固定。
 * 与上游一致：官方内核也用这同一处写来切 bank（FarCallTrampoline）。*/
#define ROM_BANK REG(0x2000)

/* ---- GB 侧 ----
 * 主机侧测试（-DEZJR_HOST_TEST）把 LY/LCDC 换成普通变量：直接访问 $FF44
 * 在 Linux 上会段错误，而「等 VBlank 再关屏」这段逻辑值得单独验证。*/
#ifdef EZJR_HOST_TEST
extern uint8_t g_test_ly;
extern uint8_t g_test_lcdc;
#define rLY    g_test_ly
#define rLCDC  g_test_lcdc
#else
#define rLCDC  REG(0xFF40)
#define rLY    REG(0xFF44)
#endif
#define rSTAT  REG(0xFF41)
#define rSCY   REG(0xFF42)
#define rSCX   REG(0xFF43)
#define rBGP   REG(0xFF47)
#define rWY    REG(0xFF4A)
#define rWX    REG(0xFF4B)
#define rP1    REG(0xFF00)
#define rIE    REG(0xFFFF)
#define rIF    REG(0xFF0F)

/* ---- EZ-FLASH Junior FPGA 寄存器（$7Fxx，写前需 unlock，写后需 lock）---- */
#define FPGA_SD_MAP   0x7F30   /* 1: 扇区数据映射到 $A000; 3: 读状态; 0: 关闭 */
#define FPGA_LOAD_MAP 0x7F36   /* 1: 加载命令窗口; 3: 启动加载; 0: 关闭 */
#define FPGA_SD_LBA   0x7FB0   /* $7FB0-$7FB3 LBA(小端), $7FB4 扇区数(bit7=写) */
#define FPGA_SRAM_MAP 0x7FC0   /* $A000 窗口显示的页: 0/2/4/6 ... */
#define FPGA_MBC      0x7F37   /* bits0-3 = MBC 码，bit7 = RTC(MBC3+TIMER) */
#define FPGA_ROM_LO   0x7FC1   /* ROM bank 掩码低 8 位 */
#define FPGA_ROM_HI   0x7FC2   /* ROM bank 掩码高 8 位 */
#define FPGA_HDR_SUM  0x7FC3   /* 卡带头校验和（$0134-$014C 的补码）*/
#define FPGA_RAM_MASK 0x7FC4   /* 存档 RAM 掩码 */
#define FPGA_FAST_A   0x7FD3   /* FW5 快加载键的一部分（与 $7FD4 相加）*/
#define FPGA_FAST_B   0x7FD4   /* FW5 快加载键：和为 $11 时跳过每次 SD 读的停顿 */
/* pSRAM 页闩锁：`$4000 = page` 之后 `$7FC0 = 3`，$A000-$BFFF 就是那一页 8KB。
 * 与 `$7FC0 = 0/2/4/6` 那套是两种性格：小值直接选前 8 个 8KB 块，值 3 是
 * "改用 $4000 的页号"。存档在页 $00-$0F（16 页 = 128KB），元数据在页 $11。*/
#define PSRAM_LATCH   0x4000
#define PSRAM_SAVE_LO 0x00     /* 存档页起点 */
#define PSRAM_META    0x11     /* 元数据页：$A000 待备份标志等 */

#define FPGA_RTC_GO   0x7FD0   /* bit0=1 触发 PicoBlaze 写 PCF8563（设置时间）*/
#define FPGA_RST_CMD  0x7FE0   /* $80 = 复位进已加载的游戏 */
#define FPGA_SWAP_LO  0x7F31   /* 换卡带内容参数（低字节）*/
#define FPGA_SWAP_HI  0x7F32   /* $00 = 把已加载的文件换进来 */

/* 解锁/提交序列（docs/REGISTERS.md）：
 *   $7F00=$E1, $7F10=$E2, $7F20=$E3  ... 命令写 ...  $7FF0=$E4
 */
#define FPGA_KEY1 0x7F00
#define FPGA_KEY2 0x7F10
#define FPGA_KEY3 0x7F20
#define FPGA_LOCK 0x7FF0
#define KEY_A     0xE1
#define KEY_B     0xE2
#define KEY_C     0xE3
#define KEY_LOCK  0xE4

/* WINDOW 可被主机侧测试用 -DWINDOW=... 覆盖（不改真实构建行为）*/
#ifndef WINDOW
#define WINDOW ((volatile uint8_t *)0xA000)
#endif

/* 页 6 的 RTC 寄存器窗口（$A008-$A00E，BCD：s,m,h,day,wday,month,year）*/
#define RTC_SEC   WINDOW[0x08]
#define RTC_MIN   WINDOW[0x09]
#define RTC_HOUR  WINDOW[0x0A]
#define RTC_DAY   WINDOW[0x0B]
#define RTC_WDAY  WINDOW[0x0C]
#define RTC_MON   WINDOW[0x0D]
#define RTC_YEAR  WINDOW[0x0E]

/* 页 2 的加载命令窗口（512 字节表，见 docs/fpga-stage1.md）*/
#define LOAD_WINDOW WINDOW

/* ---- 按键（buttons() 返回，高有效）---- */
#define BTN_A      0x01
#define BTN_B      0x02
#define BTN_SELECT 0x04
#define BTN_START  0x08
#define BTN_RIGHT  0x10
#define BTN_LEFT   0x20
#define BTN_UP     0x40
#define BTN_DOWN   0x80

#endif /* HW_H */
