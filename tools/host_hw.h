/* host_hw.h — 给主机侧测试用的"假硬件"：把 $7Fxx/$FFxx 和 $A000 窗口
 * 映射到进程内的普通内存，这样 fpga.c 的真实代码可以直接在 gcc 下跑。
 * 用法：gcc -include tools/host_hw.h ...
 */
#ifndef EZJR_HOST_HW_H
#define EZJR_HOST_HW_H

#include <stdint.h>

uint8_t *treg(unsigned a);
extern uint8_t g_win[0x400];

#define REG(a)  (*(volatile uint8_t *)treg(a))
#define WINDOW  ((volatile uint8_t *)g_win)

#endif
