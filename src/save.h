/* save.h — 存档（卡带 RAM ↔ SD 卡上的 .SAV） */
#ifndef SAVE_H
#define SAVE_H

#include <stdint.h>
#include "fat.h"

/* ROM 头 $0149 -> 存档块数（1 块 = 8KB）。0 表示这个游戏没有存档 RAM。*/
uint8_t save_banks_of(uint8_t code);

/* 启动游戏前调用：把 `.SAV` 装进 PSRAM，并在元数据页盖章（待备份）。
 * 返回块数，0 = 这个游戏没有存档，什么都没做。*/
/* lfn 是当前游戏的完整中文名（UCS-2，可为 NULL）。盖章时一并写进元数据页，
 * 下次开机备份屏和 .SAV 文件名都用它。*/
uint8_t save_prepare(const GameEntry *g);

/* 开机第一件事（**每次开机都要调用**，无条件）：先查电池哨兵
 * （元数据页 $11 的 $A201 是不是 $88），再决定要不要把 PSRAM 里的存档写回
 * SD 卡。自带提示屏：
 *
 *   电池还在 + 有待备份章  → BACKING UP...（同一屏下一行补 FINISHED / FAILED）
 *   电池没电 + 有待备份章  → BATTERY DRY / NOT BACKED UP（**不写卡**）
 *   没有待备份章           → 什么都不显示
 *
 * 备份结束不换画面：进度屏画好后保留，只在下一行局部补结果，不整屏重绘。
 *
 * 电池没电时不写卡是刻意的：PSRAM 已经衰减成一堆随机字节，写下去会把卡上
 * 那份好的 .SAV 换成垃圾。调用完之后 save_prepare() 也会知道这次的 PSRAM
 * 不可信，卡上没有 .SAV 时按新卡带处理（清成 $FF）。*/
void save_boot(void);

/* 自动备份开关（存在元数据页 $120）。手动模式下 save_boot() 会先问一句
 * 「备份存档？A=备份 B=跳过」，玩家按 B 就不写卡、也不更新指纹，
 * 下次开机还会再问。设置页里切换这个开关。*/
uint8_t save_auto_get(void);
void    save_auto_set(uint8_t on);

/* ---- 上一次启动的游戏（给列表页的 START 菜单）----
 * 数据来自元数据页，save_boot() 每次开机都会读出来并校验：元数据页是擦除态
 * （新卡 / 刚刷机）时这组值全部清空，菜单显示「无上次游戏」、不画文件名 ——
 * 否则第一次按 START 会看到一行随机字节拼出来的乱码。
 *
 * save_last_name() 是 8 字符基本名（显示回退用，也是备份流程的存档名）；
 * save_last_full() 是完整 8.3 名（"POKEMON.GBC"）—— 菜单靠它在目录里精确
 * 定位 ROM，光有基本名拼不出 FAT 的 11 字节目录项。
 * save_last_lfn() **每次调用都现查**（按 8.3 名回目录读 VFAT 长名，写进
 * 全局 scratch g_lfn_buf）：元数据页里那份 UCS-2 长名在真机上读不回来，
 * v0.12.1 起不再存长名，改成用时从卡上取。返回的指针会被后续 SD 操作覆盖，
 * 调用方拿到就立刻用掉，不要留住。
 * save_last_dir() 是那个 ROM 所在目录的簇号（0 = 根目录）。菜单用
 * 「目录簇 + 完整名」直接定位并启动，不切目录、不重扫列表，所以按 START
 * 前后看到的是同一个列表。*/
const char     *save_last_name(void);
const uint16_t *save_last_lfn(void);
const char     *save_last_full(void);
uint32_t        save_last_dir(void);

/* 自检屏专用（--diag 版）：把上面那几个读数画到行 8 / 行 12，一行调用搞定。
 * 作用是把「备份屏显示短名」的两种病因分开：
 *   REC=--          记录没读回来 → 连 START 菜单都不显示上次游戏
 *   REC=OK LFN=0    记录在，但回目录没找到长名（大目录超出回查窗口）
 * 正式版（EZJR_DIAG=0）没有这个函数，diag.c 也不编，所以不需要空实现；
 * 模拟器自检版由 test_stub.c 给一个空的。*/
void diag_last_record(void);

#endif /* SAVE_H */
