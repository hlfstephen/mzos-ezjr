/* fpga.h — 硬件层对外接口 */
#ifndef FPGA_H
#define FPGA_H

#include <stdint.h>

/* 解锁/提交包裹一次寄存器写；所有 $7Fxx 写都必须走它 */
void fpga_set(uint16_t reg, uint8_t value);

/* 读一个 512 字节扇区到 dst（PicoBlaze 执行 SD 协议，我们给 LBA 并等待）*/
void sd_read(uint32_t lba, uint8_t *dst);

/* 写一个 512 字节扇区。顺序照上游内核 DiskWrite：先开窗口、把数据放进
 * $A000、再给 LBA 和 $7FB4=0x81，等状态非 $E1 后延时约 11ms 再关窗口。
 * 返回 1 = 状态一直忙（超时），0 = 命令已发出。*/
uint8_t sd_write(uint32_t lba, const uint8_t *src);

/* 把 pSRAM 的某一页（8KB）映射到 $A000-$BFFF。page 用完后调 psram_unmap()。*/
void psram_map(uint8_t page);
void psram_unmap(void);

/* joypad：返回 hw.h 的 BTN_* 位，高有效；wait_release 等到全松开 */
uint8_t buttons(void);
void wait_release(void);

/* 读卡上固件版本：页 4 的 $A000 读到 4(FW4)/5(FW5)，用于界面显示 */
uint8_t fw_version(void);

/* 切 $A000 窗口的页（封装 fpga_set(FPGA_SRAM_MAP, page)）*/
void fpga_page(uint8_t page);

/* 等待 n 个 VBlank 帧（去抖/延时用，实现在 video.c）*/
void wait_frames(uint8_t n);

#endif /* FPGA_H */
