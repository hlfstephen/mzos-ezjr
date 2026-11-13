/* fpga.c — FPGA 寄存器、SD 扇区读、joypad、页窗口
 *
 * 全部寄存器序列照上游 lukesau/ezgb 的 stage1/src/fpga.c 与内核反汇编，
 * 这些序列在上游是真机验证过的（docs/fpga-stage1.md「Runs on hardware」）。
 * 读路径 + SD 写 + pSRAM 页映射；加载命令在 fat.c/game.c，存档流程在 save.c。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"

static void unlock(void)
{
    REG(FPGA_KEY1) = KEY_A;
    REG(FPGA_KEY2) = KEY_B;
    REG(FPGA_KEY3) = KEY_C;
}

static void lock(void)
{
    REG(FPGA_LOCK) = KEY_LOCK;
}

#ifndef EZJR_NO_FPGA_SET      /* 主机侧测试用桩替换，见 tools/fpga_host_test.py */
void fpga_set(uint16_t reg, uint8_t value)
{
    unlock();
    REG(reg) = value;
    lock();
}
#endif

void fpga_page(uint8_t page)
{
    fpga_set(FPGA_SRAM_MAP, page);
}

/* ★ $7FC0 的 bit0/bit2 = 0 是"扇区窗口"的**前提**（RTL：sd_mem_path.v
 *
 *     win_sel = win_kernel & !win_mode[0] & !win_mode[2]
 *     buf_en  = win_sel & win_en($7F30 bit0)
 *
 * 而 PSRAM 页模式恰好是 $7FC0 = 3（win_mode = 0b0011，bit0 = 1）。也就是说
 * **PSRAM 映射着的时候，$7F30 的扇区窗口根本不生效**：
 *   - 往 $A000 写 512 字节，写进的是 PSRAM 页，SD 控制器一个字节都没收到；
 *   - SD 控制器照旧把它缓冲区里的旧内容（上次 sd_read 的扇区）写出去。
 * 这正好解释了"存档文件里躺着刚才读过的目录扇区"。
 *
 * 所以每次 SD 操作前必须先把 $7FC0 清 0。代价是调用者的 psram_map() 失效 ——
 * 要用 PSRAM 的代码必须在**每次** SD 操作之后重新 map（save.c 就按这个写，
 * 与上游 backup.c 的 psram_map / memcpy / psram_unmap / fat_write 一致）。*/
static void sd_window(void)
{
    fpga_set(FPGA_SRAM_MAP, 0);
}

/* SD 读：PicoBlaze 做 SD 协议；我们设 LBA/扇区数，等状态窗口非 $E1(忙)，
 * 再从 $A000 数据窗口拷 512 字节。与上游 stage1/src/fpga.c sd_read 一致。*/
void sd_read(uint32_t lba, uint8_t *dst)
{
    const uint8_t *b = (const uint8_t *)&lba;
    uint16_t n = 0;

    sd_window();                        /* $7FC0 必须是 0，见上面 sd_window() */
    fpga_set(FPGA_SD_MAP, 1);
    unlock();
    REG(0x7FB0) = b[0];
    REG(0x7FB1) = b[1];
    REG(0x7FB2) = b[2];
    REG(0x7FB3) = b[3];
    REG(0x7FB4) = 1;                   /* 1 个扇区，bit7=0 表示读 */
    lock();
    fpga_set(FPGA_SD_MAP, 3);          /* 状态窗口 */
    while (WINDOW[0] == 0xE1) {
        if (!++n) break;               /* 超时保护：真机无卡时不要死等 */
    }
    fpga_set(FPGA_SD_MAP, 1);          /* 数据窗口 */
    memcpy(dst, (const void *)WINDOW, 512);
    fpga_set(FPGA_SD_MAP, 0);
}

/* SD 写：内核 DiskWrite 序列（上游 stage1/src/fpga.c sd_write 同款）。
 * 与读的区别全在两个细节上：
 *   1. 数据先 memcpy 进 $A000 窗口，**然后**才给 LBA 和扇区数 —— 顺序反了
 *      发出去的是上一扇区的残留；
 *   2. $7FB4 要带 bit7（0x81 = 写 1 个扇区）。之后必须等 ~11ms 让卡内部
 *      编程完，上游是 350 次空循环；这里照抄，不要改成"读回比对"代替，
 *      读回的可能是卡内缓存。
 * 返回值只反映"状态窗口有没有一直忙"，写成功的真正判据只能断电后重新读。*/
uint8_t sd_write(uint32_t lba, const uint8_t *src)
{
    const uint8_t *b = (const uint8_t *)&lba;
    volatile uint16_t i, n = 0;

    sd_window();                        /* $7FC0 必须是 0，见上面 sd_window() */
    fpga_set(FPGA_SD_MAP, 1);
    fpga_set(FPGA_SD_MAP, 3);
    memcpy((void *)WINDOW, src, 512);
    unlock();
    REG(0x7FB0) = b[0];
    REG(0x7FB1) = b[1];
    REG(0x7FB2) = b[2];
    REG(0x7FB3) = b[3];
    REG(0x7FB4) = 0x81;
    lock();
    while (WINDOW[0] == 0xE1)
        if (!++n) { fpga_set(FPGA_SD_MAP, 0); return 1; }
    for (i = 0; i < 350; i++)          /* 34 周期/次，约 11ms */
        ;
    fpga_set(FPGA_SD_MAP, 0);
    return 0;
}

/* pSRAM 页映射：$4000 写页号，再 $7FC0=3 把窗口切到"页号由 $4000 决定"。
 * 两边的顺序不能换：$7FC0 一旦为 3，窗口立刻按 $4000 的旧值显示。*/
void psram_map(uint8_t page)
{
    REG(PSRAM_LATCH) = page;
    fpga_set(FPGA_SRAM_MAP, 3);
}

void psram_unmap(void)
{
    REG(PSRAM_LATCH) = 0;
    fpga_set(FPGA_SRAM_MAP, 0);
}

/* joypad：选按键行(0x10)/方向行(0x20)，多次读去抖；返回高有效位。*/
uint8_t buttons(void)
{
    uint8_t i, keys = 0xFF, pad = 0xFF;

    rP1 = 0x10;
    for (i = 0; i < 6; i++) keys = rP1;
    rP1 = 0x20;
    for (i = 0; i < 6; i++) pad = rP1;
    rP1 = 0x30;
    return (uint8_t)(~keys & 0x0F) | (uint8_t)(~pad << 4);
}

void wait_release(void)
{
    while (buttons())
        wait_frames(1);
}

uint8_t fw_version(void)
{
    uint8_t v;

    fpga_page(4);
    v = WINDOW[0];
    fpga_page(0);
    /* $FF = 这个窗口没被 FPGA 驱动过（老固件，或者压根不是 EZ-Flash 的硬件，
     * 比如模拟器里 $A000 就是显存）。当成读不到，屏上写 FW-- —— 硬画一个
     * "FW255" 只会让人以为固件版本真是 255。*/
    if (v == 0xFF)
        v = 0;
    return v;
}
