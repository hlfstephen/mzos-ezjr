; crt0.s — 基本内核的启动代码（本工程唯一的纯汇编文件之一）
;
; 这个内核是一个普通 32 KB GB ROM，命名为 ezgb.dat 放在 SD 卡根目录。
; 上电时 FPGA 的工厂引导 / stage1 把它加载进卡带执行槽，然后从 $0100 进入
; （见上游 lukesau/ezgb 的 docs/ezgb-dat-boot.md 与 docs/boot-map.md）。
; 因此这里用标准 GB cart 头 + 一个极简 SDCC crt0：关中断、设栈、清 WRAM、
; 复制 .data，然后调用 C 的 main()。C 主体在 main.c。
;
; 为什么这部分必须是汇编：中断向量位置、SP/IE 初始化、gsinit 的数据段复制
; 在 C 里无法表达（与上游 stage1/src/crt0.s 同理）。其余全部用 C。
;
; 汇编器: sdasgb (SDCC 自带)。段布局在 tools/build.sh 里由 sdldgb 定位。

        .module crt0
        .globl  _main

        ; ---- 固定地址的向量与入口（rgbfix 缺失，头字段由 tools/fix_header.py 填）----
        .area   _HEADER (ABS)
        .org    0x0000
        ret                             ; rst $00
        .org    0x0008
        ret
        .org    0x0010
        ret
        .org    0x0018
        ret
        .org    0x0020
        ret
        .org    0x0028
        ret
        .org    0x0030
        ret
        .org    0x0038
        ret
        .org    0x0040                  ; VBlank：本内核不用中断，全部 reti
        reti
        .org    0x0048                  ; LCD STAT
        reti
        .org    0x0050                  ; Timer
        reti
        .org    0x0058                  ; Serial
        reti
        .org    0x0060                  ; Joypad
        reti
        .org    0x0100
        nop
        jp      init

        ; ---- 段声明顺序：代码/常量在 ROM，变量在 WRAM ----
        .area   _HOME
        .area   _CODE
        .area   _INITIALIZER
        .area   _GSINIT
        .area   _GSFINAL
        .area   _DATA
        .area   _INITIALIZED
        .area   _BSEG
        .area   _BSS
        .area   _HEAP

        .area   _HOME
init:
        di
        ld      sp, #0xE000
        xor     a, a
        ldh     (0xFF), a               ; IE = 0，全部中断关闭
        ld      (#0x2000), a            ; ROM bank 1（与官方内核启动时一致）
        inc     a
        ld      (#0x2000), a
        call    gsinit
        call    _main
1$:                                     ; main 正常不返回
        halt
        nop
        jr      1$

        ; ---- SDCC 运行时的 data/bss 初始化 ----
        .area   _GSINIT
gsinit::
        ld      hl, #s__DATA
        ld      bc, #l__DATA + 0x0101   ; +0x0101: 让 8 位循环能处理 0 长度
        xor     a, a
        jr      3$
2$:
        ld      (hl+), a
3$:
        dec     c
        jr      nz, 2$
        dec     b
        jr      nz, 2$
        ld      de, #s__INITIALIZED
        ld      hl, #s__INITIALIZER
        ld      bc, #l__INITIALIZER + 0x0101
        jr      5$
4$:
        ld      a, (hl+)
        ld      (de), a
        inc     de
5$:
        dec     c
        jr      nz, 4$
        dec     b
        jr      nz, 4$
        .area   _GSFINAL
        ; ---- 可切 bank 的静态变量区（$D100-$D8FF）----
        ; gsinit 上面只清/拷 _DATA（$C000 起，由主链接分配），而 bank2/3/4 的
        ; static 变量落在各自的 $D1xx/$D3xx/$D5xx（见 build.sh 的 BANKRAM），
        ; 不在 _DATA 范围内 —— 不清的话首次进入可切 bank 读到的是上电随机值。
        ld      hl, #0xD100
        ld      bc, #0x0800
6$:     xor     a, a
        ld      (hl+), a
        dec     bc
        ld      a, b
        or      c
        jr      nz, 6$
        ; ---- far call 的当前 bank ----
        ; 上面 init 已把 ROM_BANK 设成 1，这里让软件影子跟上；_far_sp 归零。
        ld      a, #1
        ld      (_far_cur), a
        xor     a, a
        ld      (_far_sp), a
        ret
