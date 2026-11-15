# 构建：工具链、链接脚本、两个必须知道的坑

## 依赖

```sh
sudo apt-get install sdcc      # SDCC 4.x（含 sm83 后端）
pip install pyboy              # 只有跑模拟器验证时需要
```

需要的可执行文件：`sdcc`、`sdasgb`（汇编器）、`sdldgb`（链接器）、`makebin`。
**不需要 rgbds**——GB 头与两道校验和由 `tools/fix_header.py` 用 Python 填。
**日常构建也不需要字体**：`src/font_data.h` 是生成产物、跟源码一起提交。只有改了界面
文案要重新生成字库时，才需要 `pillow` 和 Fusion Pixel 8px 的 TTF（见下文第 1 步）。

## 两条构建路径

```sh
./build.sh                # 正式版 -> build/ezgb.dat
./build.sh --diag         # 正式版 + 真机自检屏（多约 1.5 KB）
EZJR_TEST=1 ./build.sh    # 自检版 -> build/ezgb-test.dat（桩掉 SD/RTC 窗口，强制带自检屏）
```

两版用**不同的 obj 目录**（`build/obj` 与 `build/obj-test`）。早期版本共用目录，
后一次构建的 `.map` 覆盖前一次，导致 `emu_test.py` 按符号查地址时取不到
`g_test_rtc_win`。

## 构建步骤

1. **字库（条件执行）**：`font_data.h` 存在时直接沿用；`EZJR_REGEN_FONT=1` 或文件缺失
   才跑 `tools/gen_font.py`。它用 **Fusion Pixel Font 8px** 把界面实际用到的 36 个汉字
   和 ASCII 渲染成 8×8 1bpp（一个汉字正好一格），同时生成 `src/font_data.h` 与
   `src/strings.h`（中文文案 → glyph id 序列宏）。
   字体不随仓库分发：从 <https://github.com/TakWolf/fusion-pixel-font/releases> 下 8px
   monospaced 包，把 `fusion-pixel-8px-monospaced-zh_hans.ttf` 放 `vendor/`
   （或设 `EZJR_FONT`，或用 `--font=PATH`）。
2. `sdasgb` 汇编 `src/crt0.s`。
3. `sdcc -msm83` 编译各 C 模块。
4. `sdldgb` 按 `build/obj/rom.lk` 定位链接：

```
-mjwx
-i ezgb.ihx
-b _HOME = 0x0150
-b _CODE = 0x0400
-b _BANK1 = 0x4000
-b _DATA = 0xC000
-b _INITIALIZED = 0xC000
-k $SDCC/share/sdcc/lib/sm83
-l sm83
crt0.rel ...各模块.rel
-e
```

5. `makebin -s 32768` 补齐 32 KB。
6. `tools/fix_header.py` 填 Nintendo logo、`$0143` 校验和、`$014D` 全局校验和、
   title。`fix_header.py --verify` 可独立复核。
7. 用 `.map` 检查 `_HOME`/`_INITIALIZER`/`_GSINIT`/`_GSFINAL`/`_CODE`/`_BANK1`
   六段 **升序不重叠**，任何重叠直接判构建失败。

---

## 坑 1：`--sdcccall` 不能照抄老教程

SDCC 4.2 起，**sm83 的默认 ABI 是 `--sdcccall 1`**。上游 ezgb 的 `sdcc_build.py`
写的是 `--sdcccall 0`，那是 4.0 时代的默认值。如果按它抄：

- 你自己的 C 代码之间能对上（都用 0），
- 但 `$SDCC/share/sdcc/lib/sm83` 里的预编译库（`memset`、`memcpy`、32 位除法）
  是按 ABI 1 编的，

于是 `memset(0x8000, 0, 0x2000)` 进库后参数落在错误的寄存器/栈位置，长度变成
垃圾值，**死循环**。实测现象：ROM 启动到 `video_init` 就停住，PC 一直停在
`_memset` 里，VRAM 全零、`LCDC` 没置 0x91。

**结论**：本工程**不指定** `--sdcccall`，C 与库都用默认值。若将来必须改 ABI，
要连同库一起重编。

## 坑 2：链接脚本别给 `_GSINIT`/`_GSFINAL` 显式定位

早期把 `_INITIALIZER`、`_GSINIT`、`_GSFINAL` 都 `-b` 到 `$7E00`，结果三段互相重叠，
`gsinit` 初始化全局变量时会写坏别的东西。正确做法是**只定位 `_HOME`/`_CODE`/`_DATA`**，
让其余段由链接器顺序排在 `_HOME` 之后，再用 `.map` 断言它们都在 `_CODE` 之前。

## 坑 3：每个 bank 各自独立溢出（v0.16.6 之前是「32 KB 是硬顶」）

v0.16.6 起 ROM 是 128 KB / 8 bank，代码分到 `_CODE`（bank0）、`_BANK1`（bank1）和
bank2-5 各自的 16 KB 窗口里，布局见 [12-bank-layout.md](12-bank-layout.md)。但
**每一段仍然各自不能越界**，v0.12.0 踩过的两个连环坑照样成立：

1. `makebin` 报的 `error: size of the buffer is too small.` **不是**它自己的
   问题 —— 是前面 `sdldgb` 已经把 `_BANK1` 排到了 `$82D5`（越过 `$7FFF`
   725 字节），`-s 32768` 补不出负数。看 `build/obj/ezgb.map` 里 `_BANK1` 的
   结束地址才是准的。
2. 把大函数从 `BANK1` 挪到 `_CODE` 之后，`--diag` 版反而溢出 433 字节 ——
   因为自检屏（`diag.c`，1.1 KB）本来就在 `_CODE` 里。**两段要一起算总账**。

另外，SDCC 4.2 的 `#pragma codeseg` 在 sm83 上**不生效**（实测：写在函数之间
和文件开头都一样，整个模块还是落在 `--codeseg` 指定的那个段）。想按函数换段，
只能按文件切：`src/fatn.c` 就是这么单独拆出来的。

### 分包之后新增的坑（v0.16.6 起）

1. **RAM 变量必须由常驻段持有**。`g_sav_adopt` / `g_sav_size` 原来定义在
   `save.c`，而 `save.c` 现在编进 bank3 —— 常驻的 `fatn.c` 引用它们时，主链接
   里根本没有定义。可切 bank 的 `.rel` 不参与主链接，RAM 也就分不到。
   规矩：跨 bank 共享的全局变量一律放 bank0/1 的模块里。
2. **可切 bank 的静态变量要单独划 RAM 区**。它们的 `_DATA` 在各自镜像里，如果都用
   `$C000` 会与主链接的变量重叠。现在按 `$D100 + bank×512` 分配（`build.sh` 的
   `BANKRAM`），由 `crt0` 的 gsinit 清零 —— `_DATA` 的清零范围不包含这块。
3. **`makebin -s 32768` 遇到 RAM 段记录会罢工**（`error: size of the buffer is too
   small`）。可切 bank 的 `-b _DATA = $D5xx` 会产生 `$D5xx` 的 ihx 记录，而那不是
   ROM 内容。改用 `tools/ihx2raw.py`：只收 `$0000-$7FFF`，RAM 段的记录直接丢掉。

### 坑 3.6：没 `-b` 的段会落到 RAM，代码进不了 ROM（v0.16.8 → v0.16.9）

独立链接时 `sdld` **只认识 `-b` 过的段**，其余段默认排到已定位段之后。可切 bank
的链接脚本里第一个已定位段是 RAM 的 `_DATA($D5xx)`，于是漏掉的段全被排到 `$D5xx`：

- SDCC 把 `__divulong` / `__mullong` 放在 **`_HOME`** 段（不是 `_CODE`！）；
- `_HOME` 落在 `$D500`，`ihx2raw` 只收 `$0000-$7FFF`，这 379 字节**没进 ROM**；
- 符号地址却是 `$D500`，`fatw` 算簇 LBA 时 `call $D500` → 执行被清零的 RAM →
  NOP 接 NOP 一路跑飞。

真机表现：读卡、列表、菜单全部正常，一进备份就「正在备份」→ **黑屏**。因为
`fat.c` 在 bank1（库函数落 `_HOME=$0150`，正常），读卡路径根本不碰这段库。

现在的防线（`tools/verify_banklayout.py`，`build.sh` 与 `verify_all.sh` 都调）：

1. 段必须在 `$4000-$7FFF`，RAM 段必须在本 bank 的 `$D1xx` 区，不越界不重叠；
2. bank 窗口里不许有 `call/jp` 指向 RAM 里的符号；
3. 窗口字节与最终 `ezgb.dat` **逐字节对照**（这条才抓得住「有地址、没内容」）；
4. `_INITIALIZED` 必须为空 —— 可切 bank 的带初值 static 没人搬初值。

> 教训：静态校验通过 ≠ 代码能跑。v0.16.9 之前 ROM 结构、桩表、段重叠全绿，
> 因为它们检查的都是「符号有没有地址」，而这个 bug 是「有地址、没内容」。

### 坑 3.5：编译期开关漏定义，运行时才炸（v0.16.6 → v0.16.7）

`EZJR_FONT_BANKED` 决定 `video.c` 读字模前要不要把 `$2000` 切到 bank5。重构
`build.sh` 时这个 `-D` 丢了，后果是：

- 编译通过，链接通过，静态校验（`verify_rom.py`）**全部通过** —— ROM 里的字模
  数据一个字节都不差；
- 开机不崩、不报错，界面照常出来；
- 只是 `FONT_BANK_ENTER()` 退化成空操作，读 `$4000` 时窗口还在 bank1，于是把
  `fat.c` 的**代码字节**当点阵画出来 —— 满屏小方块。

这类「宏丢了」的问题没法靠读 ROM 发现，只能靠运行时看画面。现在的防线是
`tools/emu_test.py` 的 `字模来自 bank5` 断言：把 VRAM 里 256 个 tile 与 ROM
bank5 的字模逐字节比对，正常 115 个命中、漏宏时 0 个。

> 教训：**凡是「让编译产物换一种行为」的 `-D`，都要有一条运行时断言盯着**。
> 编译期开关不会报错，它只会让代码安静地走另一条路。

## 坑 4：`add sp, #n` 只能 ±127，大数组别放栈上

sm83 的 `add sp, #imm` 是**有符号 8 位**。函数里放一个 `uint16_t buf[80]`
（160 字节）局部数组，SDCC 生成 `add sp, #225`，`sdasgb` 立刻报
`<q> missing or improper operators`，随后 `build.sh` 又把它笼统报成
`size of the buffer is too small`，非常难认。大缓冲一律用全局 scratch
（`g_lfn_buf`）或复用的 512 字节 `fbuf` / `game_work`。

---

## 产物

| 文件 | 用途 |
|---|---|
| `build/ezgb.dat` | 正式版，拷到 SD 卡根目录 |
| `build/ezgb-test.dat` | 自检版，**只给模拟器用**，不要上卡 |
| `build/obj/ezgb.map` | 段布局与符号地址（验证脚本依赖） |

`build/` 已在 `.gitignore` 里，不随仓库分发（且不含任何官方固件）。

> **这两个产物都不是固件。** `ezgb.dat` 只是 SD 卡根目录上的内核文件，由卡带里已有的
> stage1 引导加载。**请勿使用此项目更新烧录卡的固件，如有损坏，概不负责。**

---

## 发布

打包、版本号同步、Release 产物构成、什么不能入库，见 **[RELEASING.md](../RELEASING.md)**。
一句话版：`sh tools/verify_all.sh` 全绿 → 改 `MZOS_VER` / CHANGELOG / tag 三处一致 →
两版 ROM + `EZJR.FNT` + `restore/` + `MD5SUM.txt`。

> `EZJR.FNT` 是生成物（164 KB），**不入库**：本地用
> `python3 tools/gen_cjk_fnt.py --out EZJR.FNT` 生成，或从 Release 附件下载。
> 它和 `ezgb.dat` 一样放在 SD 卡根目录；缺了只是中文文件名退回 8.3 短名。
