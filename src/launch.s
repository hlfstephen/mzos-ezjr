; launch.s — 游戏启动的 handoff，在 WRAM 里执行
;
; 为什么必须搬进 WRAM：`$7F36 = 3` 写下的那一瞬间，FPGA 开始把游戏流进 ROM 槽，
; 从那一刻起 cart 空间（$0000-$7FFF）里的代码随时会被覆盖。所以这条写指令之后
; 的每一条指令都必须来自 WRAM，不能留在 ROM 里。
;
; ★ 本段会被 memcpy 到 $D000 执行，因此里面**不允许出现任何跨符号引用**：
;   不许 call、不许 jp 绝对地址、不许查表。只有立即数和相对跳转 ——
;   所以下面每个 jr 的距离都是刻意排过的，改代码时注意别把跨段跳距拉过 127。
;
; 序列照 ezgb/stage1/src/game_handoff.s（真机验证过）和 1.04c / FW5 的
; RomLoad_BuildAndRunPoll（04:40e7），差别只在"装载完成了没有"这个判据。
;
; ★ 判据：装载期间**读任何地址都返回 $01**，读回别的就是"装完了"
;
; 这条不是猜的，是 FPGA 的 RTL 写的（ezgb/stage0/rtl/gb_data_out.v:124）：
;
;     wire [7:0] d_o = load_busy_2x ? 8'h01
;                    : buf_en       ? status
;                    : ...
;
; `load_busy_2x` 是装载器的 busy（PicoBlaze 端口 EF），优先级最高：它一身高，
; 数据总线上读什么都变 $01。装完它落下，读回来的才是真正的内存内容。
; 也就是说 **状态码 $02 根本不存在** —— 官方那段 `cp #0x02 / jr c` 判的
; 是"读到的那一个字节 ≥ 2"（装完之后该地址上的数据），所以：
;
;   * 水晶该地址是 $EC → 立刻放行，看起来"一按就进去"；
;   * 皮卡丘是 $00 → $00 < 2，永远等不到，屏已关，就是白屏 + 只能拔卡。
;
; busy 本身来自 PicoBlaze 固件（re/stage0/picoblaze/fw5-0918/bank1.psm）：
; `cmd_load_rom` 在 $222 置位、$227 清位，中间整个 `load_rom_loop` 都在里面，
; **一次中断装完整个文件，中间没有间隙**。所以"见过 $01，再见到非 $01"
; 就是"这次装载真的开始过，并且已经结束"，不需要任何固定等待。
;
; ★ 为什么不能只看 $0104 的 Nintendo logo
;
; logo 在文件第 0 扇区，装载一开始就写好了。更糟的是：换卡之前 $0104 上
; 躺着的还是上一位游戏的头，那几个字节本来就是 $CE $ED $66 ... —— 拿它当
; 完成判据，会在装载还没开始的时候就说"装完了"，然后 $7F36=0 把装载器打断、
; 换进半成品，表现为白屏、连 logo 都见不到。所以必须先确认 busy 升起过。
;
; ★ 双重确认
;
; 阶段一要求 $0104 和 $A000 **同时**读成 $01 才认。busy 是对所有地址生效的，
; 两个地址同时被旧数据读成 $01 的概率是 1/65536，可以忽略。
;
; ★ 计时用 DIV（$FF04）
;
; 调用方已经把 LCD 关了，rLY 不再走，DIV 照走。DIV 每 256 个时钟加 1
; （4.19 MHz 下 61 us），加满 256 次回绕一次（15.6 ms）。
; **必须判"新值 < 旧值"来数回绕**：只判"值变了"的话一次回绕内会减 256 次，
; 同一份代码的时间会缩到 1/256（v0.16.3 的 6 秒实际只有 23 ms，白屏就是这么来的）。
;
; 两个阶段都要超时，但**超时只用来报错，不用来放行**：
;   阶段一 3 秒还没见 busy → 装载器根本没被调度（命令表无效 / 卡带不合法）
;   阶段二 20 秒还没落下     → 装载器卡死（SD 读不动）
; 两种都是闪屏停住，不进游戏。

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

        jr      go

; ---- 装载没成功：不换卡、不复位，闪屏停住 ----
;
; 能画出来，前提是没换 cart —— 换了之后 cart 空间就是游戏了，这段代码和
; 字库一起没了，屏幕必然什么都没有。所以失败路径一定落在换卡之前。
; 这里只切 LCD 的开关位（bit7），整屏黑/亮交替，比一片白屏容易分辨。
fail:
        ld      a, #0xE4
        ld      (0xFF47), a             ; BGP：0 号色白、3 号色黑
        ld      a, #0x80
        ld      (0xFF40), a             ; 先关屏（清 LCDC 的开关位）
1$:
        ld      bc, #0x4000             ; 延时
2$:
        dec     bc
        ld      a, b
        or      c
        jr      nz, 2$
        ld      a, (0xFF40)
        xor     #0x80                   ; 切 LCD 开关位
        ld      (0xFF40), a
        jr      1$

; 失败路径离各跳转点都在 127 字节以外，中间架一跳。
jf:
        jr      fail

; ---- 阶段一：等装载器把 $0104 和 $A000 同时读成 $01 ----
;
; 写 $7F36=3 只是把命令摆进命令表；真正动手的是 PicoBlaze 的中断，
; 它空闲时才进（idle 循环里还有 delay_long 和 RTC 刷新），所以这里有一段
; 一段"读什么都正常"的空窗。空窗里绝不能放行。
go:
        ld      hl, #0x0104
        ld      de, #192                ; 3 秒：192 次 DIV 回绕
        ld      a, (0xFF04)
        ld      c, a
wait_busy:
        ld      a, (hl)
        cp      #0x01
        jr      nz, wb_tick
        ld      a, (0xA000)
        cp      #0x01
        jr      z, got_busy
wb_tick:
        ld      a, (0xFF04)
        cp      c
        jr      z, wait_busy            ; DIV 没动
        ld      c, a
        jr      nc, wait_busy           ; 新值更大：还没回绕
        dec     de                      ; 回绕一次 = 15.6 ms
        ld      a, d
        or      a, e
        jr      nz, wait_busy
        jr      vbad                    ; 3 秒没见 busy：装载器没起来

; ---- 阶段二：等 busy 落下 ----
;
; 落下就是装完了（固件里 busy 覆盖整个 load_rom_loop）。$0104 此时是
; 刚写进去的游戏头，不再是 $01。这里等的是真实装载时间。
got_busy:
        ld      de, #0x0500             ; 20 秒兜底，只用于报错
        ld      a, (0xFF04)
        ld      c, a
wait_done:
        ld      a, (hl)
        cp      #0x01
        jr      nz, done
        ld      a, (0xFF04)
        cp      c
        jr      z, wait_done
        ld      c, a
        jr      nc, wait_done
        dec     de
        ld      a, d
        or      a, e
        jr      nz, wait_done
        jr      vbad                    ; 20 秒 busy 不落：装载器卡死

; ---- 装完了，再确认一次拿到的是 GB 游戏头 ----
;
; a 里已经是 $0104 的值。八个字节是任天堂 logo，GB 卡带必须有。
done:
        cp      #0xCE
        jr      nz, vbad
        inc     hl
        ld      a, (hl)
        cp      #0xED
        jr      nz, vbad
        inc     hl
        ld      a, (hl)
        cp      #0x66
        jr      nz, vbad
        inc     hl
        ld      a, (hl)
        cp      #0x66
        jr      nz, vbad
        inc     hl
        ld      a, (hl)
        cp      #0xCC
        jr      nz, vbad
        inc     hl
        ld      a, (hl)
        cp      #0x0D
        jr      nz, vbad
        inc     hl
        ld      a, (hl)
        or      a, a
        jr      nz, vbad
        inc     hl
        ld      a, (hl)
        cp      #0x0B
        jr      nz, vbad

        jr      ok                      ; 必须跳过去：vbad 就贴在下面

; 三个阶段都可能判失败，汇到这里再跳 jf（往回 112 字节，在 +127 以内）。
vbad:
        jr      jf

        ; ---- $7F36 = 0：关命令窗口 ----
ok:
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
stop:
        jr      stop

_launch_end:
