; launch.s — 游戏启动的 handoff，在 WRAM 里执行
;
; 为什么必须搬进 WRAM：`$7F36 = 3` 写下的那一瞬间，FPGA 开始把游戏流进 ROM 槽，
; 从那一刻起 cart 空间（$0000-$7FFF）里的代码随时会被覆盖。所以这条写指令之后
; 的每一条指令都必须来自 WRAM，不能留在 ROM 里。
;
; ★ 本段会被 memcpy 到 $D000 执行，因此里面**不允许出现任何跨符号引用**：
;   不许 call、不许 jp 绝对地址、不许查表。只有立即数和相对跳转。
;   混进一个 ROM 地址的后果是真机上随机死机，且现象不可预测 ——
;   这是整个启动流程里唯一"模拟器完全测不出、出错也难定位"的地方。
;
; 序列照 ezgb/stage1/src/game_handoff.s（真机验证过），差别只有两点：
;   - 上游用 call 复用 unlock/lock 子程序（依赖 $D000 基址重定位），这里全展开；
;   - 上游末尾有 SKIP_BOOT 快速启动分支，这里只走 $7FE0=$80 复位这一条路。

        .module launch
        .globl  _launch_start, _launch_end

        .area   _CODE

_launch_start:
        ; ---- $7F36 = 3：开始装载命令里描述的文件 ----
        ld      hl, #0x7F00
        ld      (hl), #0xE1
        ld      hl, #0x7F10
        ld      (hl), #0xE2
        ld      hl, #0x7F20
        ld      (hl), #0xE3
        ld      hl, #0x7F36
        ld      (hl), #0x03
        ld      hl, #0x7FF0
        ld      (hl), #0xE4

        ; ---- 轮询 $A000：0 未开始 / 1 装载中 / 2 完成 ----
        ld      hl, #0xA000
1$:
        ld      a, (hl)
        cp      #0x02
        jr      c, 1$

        ; ---- $7F36 = 0：关命令窗口 ----
        ld      hl, #0x7F00
        ld      (hl), #0xE1
        ld      hl, #0x7F10
        ld      (hl), #0xE2
        ld      hl, #0x7F20
        ld      (hl), #0xE3
        ld      hl, #0x7F36
        ld      (hl), #0x00
        ld      hl, #0x7FF0
        ld      (hl), #0xE4

        ; ---- $7F31 = 0, $7F32 = 0：把刚装好的文件换进卡带 ----
        ld      hl, #0x7F00
        ld      (hl), #0xE1
        ld      hl, #0x7F10
        ld      (hl), #0xE2
        ld      hl, #0x7F20
        ld      (hl), #0xE3
        ld      hl, #0x7F31
        ld      (hl), #0x00
        inc     hl
        ld      (hl), #0x00
        ld      hl, #0x7FF0
        ld      (hl), #0xE4

        ; ---- MBC 初始状态：ROM bank 1，RAM bank 0 ----
        ; 这两条写给的是刚换进来的游戏的 MBC，不是 FPGA 寄存器，所以不解锁。
        ld      a, #0x01
        ld      (#0x2000), a
        xor     a, a
        ld      (#0x3000), a

        ; ---- $7FE0 = $80：复位进游戏 ----
        ld      hl, #0x7F00
        ld      (hl), #0xE1
        ld      hl, #0x7F10
        ld      (hl), #0xE2
        ld      hl, #0x7F20
        ld      (hl), #0xE3
        ld      hl, #0x7FE0
        ld      (hl), #0x80
        ld      hl, #0x7FF0
        ld      (hl), #0xE4

        ; 正常不会走到这儿（$7FE0 已触发复位）；停下来比跑飞安全。
2$:
        halt
        jr      2$

_launch_end:
