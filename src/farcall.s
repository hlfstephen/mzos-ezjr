; farcall.s — 跨 bank 调用（MBC5，$4000-$7FFF 是唯一可切窗口）
;
; 与上游 FarCallTrampoline 的关键区别：**只占 1 个字的返回槽**。
; 上游压 3 个字，被调函数的第一参数跑到 sp+6，于是每个 far 目标的 C 源码都得
; 加两个 dummy 参数占位。这里保持 sp+2，与普通 call 完全一致 —— C 源码不用改。
;
; 调用点（桩由 tools/gen_farstub.py 生成，7 字节 = call 3 + 数据 4）：
;       call    __farcall
;       .db     <lo>, <hi>, <bank>, 0
;
; 栈的变化（这是全文件最要紧的部分，改之前请先对照着走一遍）：
;   调用者 call 桩    [ret_to_C] [栈上参数...]              ← SP
;   桩 call __farcall [&数据表] [ret_to_C] [栈上参数...]     ← SP
;   __farcall:
;     pop hl          → hl = &数据表（读完 3 字节后 hl 空闲），SP → [ret_to_C]
;     pop hl          → hl = ret_to_C，SP → [栈上参数...]；转存进 _far_tmp
;     存 ret_to_C + old_bank 进 _far_stk
;     push __fcret    → [__fcret] [栈上参数...]              ← SP
;     切 bank，jp 目标
;   ★ 参数与返回值寄存器（SDCC sm83 实测，改代码前务必对照）：
;     参数：A = 第 1 个 8 位，DE = 第 1 个 16 位，BC = 32 位的高半（低半在 DE），
;           第 2 个起的参数压栈
;     返回：A = 8 位，BC = 16 位，BC:DE = 32 位
;     所以 __farcall 进门要存 A/BC/DE（不能碰它们），__fcret 出门要恢复 A/BC/DE。
;   栈上的参数（第 2 个起）回到 sp+2  ✓（和普通 call 一样）
;   目标 ret          → 弹出 __fcret，SP 回到栈上参数区
;   __fcret:
;     取回 ret_to_C，恢复 bank（$4000 窗口又变回调用者的）
;     push ret_to_C → ret → 跳回 C 代码 ✓（返回值寄存器已复原）
;   调用者 add sp,n   → 清栈上参数 ✓
;
; ★ v0.16.10 修的两处（此前 far call 一直在悄悄破坏参数和返回值）：
;   1. **__farcall 算 _far_stk 时用了 `ld bc,#_far_stk`**，而 BC 是 32 位参数的
;      高半 —— `fat_cluster_lba(uint32_t)` 收到的簇号高位就成了 $Cxxx，
;      算出的 LBA 是天文数字，字库读到垃圾扇区，**中文文件名整片空白**。
;   2. **__fcret 用 a / bc / hl 算账**，把返回值全毁了：FAT 的错误码（uint8_t，
;      在 A）变成 bank 号，屏幕报「备份失败 / 未知错误」；32 位返回值的高字
;      （在 BC）变成 _far_stk 的地址。
;   教训：far call 的「参数/返回值寄存器集合」必须实测（`.asm` 里看一眼就知道），
;   而且**测试必须断言返回值** —— v0.16.8 的 farcall_test 只查了 DE 参数不被破坏、
;   目标函数确实写了内存，没查返回值，于是这两处一路带到了卡上。
;
; ★ v0.16.8 修的三处（v0.16.6/0.16.7 的 far call 从来没被真正执行过，
;   一上真机读卡就卡在「读取中」）：
;   1. **de 是第一个参数的寄存器**。SDCC 在 sm83 上把「第一个指针/16 位参数」
;      放在 DE 里（`fat_info` 的机器码第一条就是 `ld (de),a`），根本不在栈上。
;      旧代码用 `pop de` 暂存返回地址，直接把参数覆盖成了垃圾 —— 所有带指针
;      参数的 far 调用都会往随机地址写。现在全程只用 hl / bc，de 一个字节不碰。
;   2. 返回点算错。旧代码把「&数据表 + 4」当返回地址，可桩是 7 字节、末尾没有
;      ret，那个地址正好是**下一个桩的开头** —— __fcret 跳回去又撞进下一个桩的
;      call __farcall，整张桩表连环错乱地跑，且每次多留 2 字节在栈上。
;      现在 ret_to_C 直接从栈上 pop 出来存好，__fcret 自己 push 回去再 ret，
;      不依赖桩的布局。
;   3. 参数位置。旧代码 pop 掉 &数据表 后直接 push __fcret，栈成了
;      [__fcret][ret_to_C][arg...]，栈上的参数整体后移 2 字节。现在中间先把
;      ret_to_C pop 掉，栈上参数回到 sp+2。
;
; 寄存器约定（改代码前务必记住）：
;   DE  第一个 16 位参数 —— **绝不能当成临时寄存器**
;   HL/A/BC  自由使用（SDCC 视为 caller-saved，调用点不指望它们保值）
;
; 嵌套：save(bank3) → fatw(bank2) → fat(bank1) 是真实存在的调用链。
; 每层占 _far_stk 里 4 字节（ret_to_C 2 + old_bank 1 + 1 字节对齐）。
; 临时变量（_far_tmp/_far_tlo/...）用完即弃，不跨层，所以嵌套安全。
;
; 为什么不会跑飞：__farcall、__fcret、桩全部在 bank0（$0000-$3FFF，永不切换）。
; 切换发生在「CPU 不在被换掉的窗口里」的时刻；返回前先恢复 bank，再跳回调用者。

        .module farcall
        .globl  __farcall, __fcret
        .globl  _far_sp, _far_cur

FAR_DEPTH = 4           ; 实测最长调用链 3 层（main→save→fatw），留一层余量

        .area   _CODE

__farcall::
        pop     hl                      ; hl -> 4 字节数据表；SP 现在指向 ret_to_C

        ; ---- 先把「参数寄存器」存起来（v0.16.10）----
        ; SDCC sm83 的传参约定（实测，见 tools/farcall_test.py 的断言）：
        ;   A  = 第 1 个 8 位参数
        ;   DE = 第 1 个 16 位参数
        ;   BC = 32 位参数的高半（低半在 DE）；第 2 个起的参数压栈
        ; 下面算 _far_stk 要用 a/bc，切 bank 前必须原样放回去。DE 一个字节不碰
        ; （它是 16 位参数），so 只要存 A/BC 就够，但 E 也顺手存上，省得漏。
        ld      (_far_pa), a
        ld      a, b
        ld      (_far_pb), a
        ld      a, c
        ld      (_far_pc), a
        ld      a, d
        ld      (_far_pd), a
        ld      a, e
        ld      (_far_pe), a

        ; ---- 再读数据表（读的时候 hl 会被 ++ 破坏，之后就不需要它了）----
        ld      a, (hl+)
        ld      (_far_tlo), a
        ld      a, (hl+)
        ld      (_far_thi), a
        ld      a, (hl)
        ld      (_far_tbk), a

        ; ---- 把调用者的返回地址收进 hl，转存 _far_tmp ----
        pop     hl
        ld      a, l
        ld      (_far_tmp), a
        ld      a, h
        ld      (_far_tmp+1), a

        ; ---- 深度检查 + 取本层指针 ----
        ld      a, (_far_sp)
        cp      #FAR_DEPTH
        jp      nc, far_ovf             ; 距离超过 jr 的 ±127，用 jp
        add     a, a
        add     a, a                    ; depth * 4
        ld      l, a
        ld      h, #0
        ld      bc, #_far_stk
        add     hl, bc

        ; ---- 存 ret_to_C（2 字节）----
        ld      a, (_far_tmp)
        ld      (hl+), a
        ld      a, (_far_tmp+1)
        ld      (hl+), a

        ; ---- 存 old_bank ----
        ld      a, (_far_cur)
        ld      (hl), a

        ; ---- depth++ ----
        ld      a, (_far_sp)
        inc     a
        ld      (_far_sp), a

        ; ---- 占住返回槽：栈上的参数因此回到 sp+2 ----
        ld      hl, #__fcret
        push    hl

        ; ---- 切 bank（此后 CPU 仍在 bank0，安全）----
        ld      a, (_far_tbk)
        ld      (_far_cur), a
        ld      (#0x2000), a

        ; ---- 装目标地址（在 bank N 的 $4000-$7FFF 内）----
        ld      a, (_far_thi)
        ld      h, a
        ld      a, (_far_tlo)
        ld      l, a

        ; ---- 恢复参数寄存器，A 最后恢复（它也是 8 位参数）----
        ld      a, (_far_pe)
        ld      e, a
        ld      a, (_far_pd)
        ld      d, a
        ld      a, (_far_pc)
        ld      c, a
        ld      a, (_far_pb)
        ld      b, a
        ld      a, (_far_pa)
        jp      (hl)

__fcret::
        ; 目标函数已 ret 到这里；SP 指向栈上参数区，不要碰
        ;
        ; ★ v0.16.10：这里必须先把**返回值寄存器**存起来。
        ;   SDCC sm83 的返回约定（实测）：A = 8 位，BC = 16 位，BC:DE = 32 位。
        ;   以前这段直接用 a/bc/hl 算账，把返回值全毁了：
        ;     · FAT 错误码（uint8_t，在 A）变成 bank 号 → 屏幕报「未知错误」；
        ;     · 簇 LBA（uint32_t，高字在 BC）高位变成 _far_stk 的地址 →
        ;       按天文数字扇区去读字库，中文文件名整片空白。
        ld      (_far_pa), a
        ld      a, b
        ld      (_far_pb), a
        ld      a, c
        ld      (_far_pc), a
        ld      a, d
        ld      (_far_pd), a
        ld      a, e
        ld      (_far_pe), a

        ld      a, (_far_sp)
        dec     a
        ld      (_far_sp), a
        add     a, a
        add     a, a
        ld      l, a
        ld      h, #0
        ld      bc, #_far_stk
        add     hl, bc

        ; ---- 取回 ret_to_C（2 字节）----
        ld      a, (hl+)
        ld      c, a
        ld      a, (hl+)
        ld      b, a
        ld      a, (hl)                 ; old_bank

        ; ---- 先恢复 bank：$4000 窗口要变回调用者的，才能跳回它 ----
        ld      (_far_cur), a
        ld      (#0x2000), a

        ; ---- 把 ret_to_C 放回栈顶再 ret，交给调用者的 add sp,n ----
        push    bc

        ; ---- 恢复返回值寄存器（A 最后，它是 8 位返回）----
        ld      a, (_far_pe)
        ld      e, a
        ld      a, (_far_pd)
        ld      d, a
        ld      a, (_far_pc)
        ld      c, a
        ld      a, (_far_pb)
        ld      b, a
        ld      a, (_far_pa)
        ret

far_ovf:
        ; 嵌套超过 FAR_DEPTH：留哨兵 $FF 后停机（不跑飞、不改 bank）
        ld      a, #0xFF
        ld      (_far_sp), a
        di
1$:
        halt
        jr      1$

        ; 放 _DATA 而不是 _BSS：_BSS 是独立段，链接器不会自动定位（会排到 ROM 区
        ; $6Fxx，把 bank1 的代码盖掉），而 _DATA 有 -b 定位、由 crt0 的 gsinit 清零。
        .area   _DATA
_far_sp:  .ds 1
_far_tmp: .ds 2
_far_tlo: .ds 1
_far_thi: .ds 1
_far_tbk: .ds 1
_far_cur: .ds 1
; 参数/返回值寄存器暂存（v0.16.10）。A / BC / DE 三个都是 SDCC 的参数与返回
; 寄存器，__farcall 与 __fcret 都要借它们算账，所以进门先存、出口再放。
; 单份就够：它们只在「进入函数 → 跳到目标」或「目标返回 → 回到调用者」这一小段
; 里有效，而这一小段里不会再发生 far call（全程 di，没有中断）。
_far_pa:  .ds 1
_far_pb:  .ds 1
_far_pc:  .ds 1
_far_pd:  .ds 1
_far_pe:  .ds 1
; C 侧声明 `extern uint8_t _far_cur;` 会被 SDCC 翻成 __far_cur（它给每个外部
; 符号再加一个下划线），而本文件是手写汇编，符号就叫 _far_cur。给个别名对上。
        .globl  __far_cur
__far_cur = _far_cur
_far_stk: .ds 16              ; FAR_DEPTH(4) × 4 字节/层
