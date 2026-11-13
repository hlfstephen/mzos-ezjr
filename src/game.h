/* game.h — 从列表启动游戏
 *
 * game_launch() 成功时**不返回**（把控制权交给复位后的游戏），
 * 失败时返回错误码，调用方显示出来即可。
 */
#ifndef GAME_H
#define GAME_H

#include <stdint.h>
#include "fat.h"

enum {
    GAME_OK = 0,
    GAME_ERR_BAD_ROM = 1,   /* 文件太小 / 太大，不像 GB ROM */
};

/* 错误码与 fat_build_cmd() 的返回值共用一套数字：
 *   1 BAD ROM（本文件）
 *   5 FAT_ERR_FRAGMENTED（碎片 > 61 段）
 *   6 FAT_ERR_BAD_CHAIN（簇链非法）
 * 界面上统一印 ERR=n，对照表见 docs/06-launch-plan.md。*/
uint8_t game_launch(const GameEntry *g);

/* 512 字节工作缓冲：先装 ROM 首扇区，再原地改写成加载命令。
 * 存档流程（save.c）在菜单态复用它 —— 两边不会同时用到。*/
extern uint8_t game_work[512];

#endif /* GAME_H */
