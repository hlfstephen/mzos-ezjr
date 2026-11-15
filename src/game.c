/* game.c — 从游戏列表启动一个 ROM
 *
 * 做两件事：把 SD 卡上的文件描述成 FPGA 的 512 字节加载命令，然后按官方
 * 内核的顺序配置寄存器、关屏、把控制权交给 WRAM 里的 handoff 汇编。
 *
 * 顺序依据（均经真机验证的上游实现）：
 *   ezgb/stage1/src/game.c: game_launch()   读头、算 5 个值、写寄存器
 *   ezgb/stage1/src/game_handoff.s          $7F36=3 轮询 -> $7FE0=$80 复位
 *   ezgb/docs/fpga-stage1.md「Fast launch」  序列说明与反例
 *
 * ★ 本文件执行到最后一行时，cart 空间里已经不是这个内核了。所以：
 *   - 第 9 步之后不能再调用任何 ROM 里的函数（handoff 是纯汇编 + 立即数）；
 *   - game_launch() 成功路径不返回，也不恢复栈。
 *
 * ★ RTC base（$A018-$A01C）留到 P1。P0 的目标是"先能进游戏"，把两个变量
 *   分开，真机上失败时才分得清是加载命令错还是 base 写错。
 */

#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"
#include "game.h"
#include "rtc.h"
#include "video.h"
#include "strings.h"

/* 512 字节工作区：先装游戏首扇区（取 $0100-$014F 的头），算完 5 个值后
 * 原地改写成加载命令。WRAM 只剩不到 1 KB 余量（见 docs/06 §5.3），
 * 开两块 512 会顶到 $D000 的 handoff 区。*/
uint8_t game_work[512];   /* 与 save.c 共用，见 save.c 顶部注释 */

#ifndef EZJR_HOST_TEST
extern const uint8_t launch_start[], launch_end[];
#endif

/* ---- $0147 -> $7F37 的低 4 位 ---- */
static uint8_t mbc_of(uint8_t t)
{
    if (t == 0x00 || t == 0x08 || t == 0x09) return 0;
    if ((t >= 0x01 && t <= 0x03) || t == 0xEA || t == 0xFF) return 1;
    if (t == 0x05 || t == 0x06) return 2;
    if ((t >= 0x0F && t <= 0x13) || t == 0xFC) return 3;
    if (t >= 0x19 && t <= 0x1E) return 4;
    return 6;
}

/* ---- $0148 -> ROM bank 掩码；头里写小了就按文件实际大小抬上去 ---- */
static uint16_t rom_mask_of(uint8_t code, uint32_t size)
{
    uint16_t mask = 0, m, banks = (uint16_t)(size >> 14);   /* 16 KB / bank */

    if (code >= 1 && code <= 8)
        mask = (uint16_t)((2u << code) - 1u);
    else if (code == 0x52)
        mask = 0x47;
    else if (code == 0x53)
        mask = 0x4F;
    else if (code == 0x54)
        mask = 0x5F;

    if (banks == 0)
        banks = 1;
    if (mask < (uint16_t)(banks - 1)) {
        for (m = 1; m <= banks && m < 0x200; m <<= 1)
            ;
        mask = (uint16_t)(m - 1);
        if (mask > 0x1FF)
            mask = 0x1FF;
    }
    return mask;
}

/* ---- $0149 -> $7FC4 存档 RAM 掩码 ---- */
static uint8_t ram_mask_of(uint8_t code, uint8_t mbc)
{
    if (mbc == 2 || code <= 2) return 0x00;
    if (code == 4) return 0x0F;
    if (code == 5) return 0x07;
    return 0x03;
}

/* ---- $0134-$014C 的卡带头校验和 ----
 * h 是整扇区（512 字节），header 在这块缓冲里从 $100 开始。循环变量用
 * 相对偏移 $00-$18（25 字节）而不是绝对 $134-$14C：后者塞进 uint8_t 会
 * 截成 $34，条件恒真、死循环 —— 和 rtc_weekday 月偏移表那次是同一类坑。*/
static uint8_t header_sum(const uint8_t *h)
{
    uint8_t x = 0, i;

    for (i = 0; i <= 0x18; i++)
        x = (uint8_t)(x - h[0x134 + i] - 1);
    return x;
}

#ifdef EZJR_HOST_TEST
void host_cmd_window(const uint8_t *cmd);   /* 桩：见 tools/launch_host_test.py */
#endif

uint8_t game_launch(const GameEntry *g)
{
    uint8_t  t, mbc, timer, ram, chk, fw, err, rc, ac;
    uint16_t rmask;
#ifndef EZJR_HOST_TEST
    void   (*run)(void);
#endif

    if (g->size < 0x8000UL || g->size > 0x800000UL)
        return GAME_ERR_BAD_ROM;

    sd_read(fat_cluster_lba(g->cluster), game_work);

    t     = game_work[0x147];                       /* cart type */
    mbc   = mbc_of(t);
    timer = (uint8_t)(t == 0x0F || t == 0x10); /* 只有这两个带时钟 */
    rc    = game_work[0x148];
    ac    = game_work[0x149];
    rmask = rom_mask_of(rc, g->size);
    ram   = ram_mask_of(ac, mbc);
    chk   = header_sum(game_work);

    /* 头已经取完，这块缓冲原地改写成加载命令 */
    err = fat_build_cmd(g->cluster, g->size, game_work);
    if (err)
        return err;                            /* 碎片/坏链，原样透传 */

    fw = fw_version();                         /* 页 4 -> 0 */

    fpga_page(2);                              /* 命令窗口要求 win_mode[1]=1 */

    /* MBC3+TIMER：FW5 的游戏时钟是 base + elapsed，base 上电是 0。不写它，
     * 水晶读到的就是「从开机起过了多久」（显示 2000-01-01 或从 0 起算）。
     * 这一步内部切页 6 写完再切回页 2，见 rtc.c / docs/08-rtc-base.md。
     *
     * EZJR_NO_RTC_BASE：真机上万一这条路有问题（花屏/进不去游戏），
     * 用 build.sh 加 -DEZJR_NO_RTC_BASE 重编，就退回 v0.3.2 的行为。*/
#ifndef EZJR_NO_RTC_BASE
    if (timer)
        rtc_base_write();
#endif

    fpga_set(FPGA_MBC, (uint8_t)(mbc | (timer ? 0x80 : 0)));
    if (fw >= 5) {
        /* 快加载键 = $7FD3 + $7FD4。FW4 不解码 $7FD4，写了也无害，但省掉更干净。
         * 只写 $7FD4=$11 而让 $7FD3 保持 CGB 引导值 $11，和会变成 $22，反而变慢。*/
        fpga_set(FPGA_FAST_A, 0x00);
        fpga_set(FPGA_FAST_B, 0x11);
    }
    fpga_set(FPGA_RAM_MASK, ram);
    fpga_set(FPGA_ROM_LO, (uint8_t)rmask);
    fpga_set(FPGA_ROM_HI, (uint8_t)(rmask >> 8));
    fpga_set(FPGA_HDR_SUM, chk);

    if (rLCDC & 0x80) {                        /* 已经关屏时 rLY 不再走 */
        while (rLY != 144)                     /* 关屏必须在 VBlank，否则花屏 */
            ;
    }
    rLCDC = 0x00;

    fpga_set(FPGA_LOAD_MAP, 1);

#ifdef EZJR_HOST_TEST
    /* 主机上没有 $A000 窗口，也没有 $D000 可跳。桩拿到命令表自己做断言。*/
    host_cmd_window(game_work);
    return GAME_OK;
#else

    memcpy((void *)0xA000, game_work, 512);

    /* 以下两行之后，ROM 里就没有这个内核了 —— 顺序不能调换 */
    memcpy((void *)0xD000, launch_start,
           (uint16_t)(launch_end - launch_start));
    run = (void (*)(void))0xD000;
    run();

    return GAME_OK;                            /* 不会到这儿 */
#endif
}
