/* diag.h — 真机自检屏 */
#ifndef DIAG_H
#define DIAG_H

/* 调试屏是否参与编译。默认关（省 ~1.5 KB）；./build.sh --diag 打开。
 * 这里给个默认值，是为了 main.c 里可以直接写 #if EZJR_DIAG。*/
#ifndef EZJR_DIAG
#define EZJR_DIAG 0
#endif

/* 显示自检屏并等按键返回。可在「游戏列表」或「存储卡错误」界面按
 * SELECT / START 进入，用于真机排障时直接读出关键寄存器值。*/
void diag_screen(void);

#endif
