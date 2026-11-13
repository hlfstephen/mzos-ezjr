/* diag.c — 真机自检屏
 *
 * 为什么要有这一屏：这个内核的 SD 读、RTC 读写全部走 FPGA 寄存器，在模拟器
 * 里都是空的，**只能真机验证**。真机上出错时如果屏幕只显示"存储卡错误"，
 * 一次试错就只能得到"不行"这一个信息，成本极高。所以把链路上每个环节的
 * 关键读数都摊到一屏：
 *
 *   FW      卡上固件版本（页 4 首字节）：4/5，"--" 表示读不到 FPGA
 *   MOUNT   fat_mount() 返回码，0 = 成功（1 无 55AA/非 512 字节，2 FAT12，3 GPT）
 *   SEC0    SD 卡 LBA0 前 5 个字节的原始值（看 SD 通路是否真的读到数据）
 *   SIG     LBA0 的 $55AA 签名（FAT 引导扇区/DBR 标志）
 *   FAT/SPC 卷类型与每簇扇区数
 *   CL      数据簇总数（FAT16 应 < 65525，FAT32 更大；0 表示没解析出 BPB）
 *   PART    MBR 分区起始 LBA（superfloppy 为 0；与卡容量对得上才说明分区表读对）
 *   RTC     页 6 $A008-$A00E 的 7 个原始 BCD 字节（s m h day wday month year）
 *   VL      PCF8563 秒寄存器 bit7：1 = 电池掉过电，时间不可信
 *   T / D   把上面的原始字节按 BCD 解码后的时间日期（与原始值对照可验证换算）
 *   REC/DIR/LFN  「上次启动」那条记录的状态（v0.12.2 加，见下面那段注释）
 *
 * 模拟器（PyBoy）里没有 FPGA：$A000 读回 $FF、没有 SD 卡，所以这屏在模拟器
 * 里看到的都是 0/FF，属正常，不代表真机行为。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"
#include "rtc.h"
#include "video.h"
#include "strings.h"
#include "save.h"
#include "diag.h"

/* 调试屏默认不编译。它是真机排障用的，占了约 1.5 KB —— 平时用不上，而
 * 32 KB 卡带的空间要留给界面和存档。需要时：./build.sh --diag，或者给
 * sdcc 加 -DEZJR_DIAG=1。
 *
 * 关掉时保留一个空函数，调用点就不用满地 #ifdef（空函数只有 3 字节）。*/
#if defined(EZJR_DIAG) && EZJR_DIAG

/* 自检屏读 SD LBA0 用的一扇区缓冲。
 * 正式 / --diag 版复用 game.c 的 game_work：自检屏只在菜单态按 START 进入，
 * 与启动游戏、存档备份互斥，共用这块能省 512 字节 WRAM。以前自己开一块
 * static，--diag 版 _DATA 会涨到 4185 字节、顶穿 $D000 的启动 handoff 区
 * （verify_rom.py 直接判失败），真机上启动游戏可能花屏。
 * 模拟器自检版没编 game.c，只能自己留一块。*/
#if defined(EZJR_TEST_STUB)
static uint8_t sec0[512];
#else
extern uint8_t game_work[512];
#define sec0 game_work
#endif

void diag_screen(void)
{
    uint8_t err, fw;
    uint8_t f[T_FIELDS];
    FatInfo fi;

    /* 自检自己重新挂一次，不依赖主流程的状态：这样「挂载失败」的卡也能进来看 */
    err = fat_mount();
    fat_info(&fi);
    fw = fw_version();
    rtc_read(f);

    cjk_begin();
    video_off();
    clear_screen();
    cjk_center(0, STR_SELF_TEST);

    print_at(3, 0, "FW=");
    if (fw >= 1 && fw <= 9)
        print_dec2(3, 3, fw);
    else
        print_at(3, 3, "--");

    print_at(4, 0, "MOUNT=");
    print_dec2(4, 6, err);

    /* ---- 「上次启动」记录的状态（v0.12.2 加，v0.13.0 起是屏幕主体）----
     *
     * 备份屏显示 8.3 短名只有**一种**直接原因：save_boot() 里那句
     * fat_full_lfn() 返回 0。而它返回 0 有两种完全不同的病：
     *   ① 记录本身就没读回来 → boot_full 是空串，回查根本不会发生（REC=--）
     *   ② 记录在，但回目录里没找到那个 8.3 名（REC=OK 而 LFN=0）
     * 这两种在真机上是同一个现象，只能靠改代码试。所以这三个读数要留着。
     *
     * ★ v0.13.0 把原来的 SEC0 / PT / SIG / RTC 原始字节 / CL / PART 全砍了：
     *   32KB 已经贴边（正式版 BANK1 只剩 150 字节），而那些项在 v0.11 之前
     *   就验证完了 —— 挂载失败现在看 MOUNT，卡类型看 FAT/SPC 已经足够。
     *   真需要时按 CHANGELOG 里的 sha 取回这一版之前的 diag.c。*/
    diag_last_record();

    if (fi.mounted) {
        print_at(10, 0, "FAT=");
        print_at(10, 4, fi.is_fat32 ? "32" : "16");
        print_at(10, 8, "SPC=");
        print_u16(10, 12, fi.sectors_per_cluster);
    } else {
        print_at(10, 0, "FAT=--");
    }

    /* 解码后的 RTC：与卡上文件时间对照，能看出时钟有没有跑 */
    print_at(13, 0, "T=");
    print_dec2(13, 2, f[T_HOUR]);
    print_at(13, 4, ":");
    print_dec2(13, 5, f[T_MIN]);
    print_at(13, 7, ":");
    print_dec2(13, 8, f[T_SEC]);
    print_at(15, 0, "D=20");
    print_dec2(15, 4, f[T_YEAR]);
    print_at(15, 6, "-");
    print_dec2(15, 7, f[T_MON]);
    print_at(15, 9, "-");
    print_dec2(15, 10, f[T_DAY]);

    video_on();

    wait_release();
    while (!buttons())
        wait_frames(1);
    wait_release();
}

#else  /* !EZJR_DIAG */

void diag_screen(void) { }

#endif /* EZJR_DIAG */
