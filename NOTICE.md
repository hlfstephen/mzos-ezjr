# 署名与第三方声明

## 1. 本仓库的版权

- 代码（`src/*.c`、`src/*.h`、`src/*.s`、`tools/*.py`、`build.sh`）：**AGPL-3.0**，
  见 [LICENSE](LICENSE)。
- 汉字点阵与 ASCII 点阵（`src/font_data.h` 中的 `cjk_font`、`ascii_font`，以及由它们
  生成的 `EZJR.FNT`）：派生自 **Fusion Pixel Font 8px**，按 **SIL Open Font License 1.1**
  分发，见第 2 节。

选 AGPL-3.0 的理由与「要不要换」的分析见
[docs/11-license-and-upstream.md](docs/11-license-and-upstream.md)。

## 2. 字体：Fusion Pixel Font 8px（SIL OFL 1.1）

`src/font_data.h` 里的点阵是把 **Fusion Pixel Font 8px monospaced zh_hans** 的字形
直接栅格化成 8×8 单色位图的结果（该字体本身就是为 8px 设计的像素字体，不需要缩放）。
`EZJR.FNT` 是同一套字形的全量版本（U+4E00–U+9FFF，164 KB），由 `tools/gen_cjk_fnt.py`
生成。字形的著作权属于字体作者（TakWolf，OFL-1.1）。按 OFL：

- 可以自由使用、修改、再分发（包括嵌入到 ROM 中）；
- 再分发时须保留本声明与 OFL 许可证文本；
- **不得单独以“字体”名义出售**这些点阵；
- 若修改了字形，不得使用 “Fusion Pixel” 作为保留字体名。

上游：<https://github.com/TakWolf/fusion-pixel-font>（本仓库不附带 TTF 文件；
重新生成字库时自行下载，放 `vendor/` 或设 `EZJR_FONT`）。

## 3. 参考实现：lukesau/ezgb（AGPL-3.0）

本项目用到的**硬件事实**（寄存器地址、解锁/提交序列、页窗口语义、RTC 读写链路、
FAT 布局、启动契约）来自对该仓库文档与反汇编的阅读：

- <https://github.com/lukesau/ezgb>（作者 Luke Esau，AGPL-3.0）

具体参考点：

| 内容 | 来源 |
|---|---|
| `$7F00/$7F10/$7F20/$7FF0` 解锁提交、`$7F30` SD 映射、`$7FB0` LBA | `stage1/src/fpga.c`、`docs/REGISTERS.md` |
| `$A000` 窗口页语义（0/2/4/6） | `docs/fpga-stage1.md`、`stage0/fw5-0918/gb_regs.v` |
| PCF8563 页 6 的 `$A008-$A00E` BCD 顺序、`VL` 位 | `kernel/src/fat_date.c` |
| 设置时间 `$7FD0=1` → PicoBlaze `$10` → I2C 写 | `re/stage0/picoblaze/fw5-0918/bank1.psm`、`re/kernel/1.05e-0918/disassembly/bank_004.asm` |
| GB 头/段布局/`sdldgb` 链接方式 | `stage1/build.sh`、`docs/DEVELOPMENT.md` |
| SD 写扇区的 `$7FB4` bit7 与编程后空转时长 | `stage1/src/fpga.c`（`sd_write`） |
| 元数据页布局（`$A000/$A001/$A00F/$A010/$A200/$A201/$A300`） | `kernel/src/backup.c`、`docs/save-manager.md` |

**本项目不是该仓库的 fork，也未复制其任何源码、Verilog 或反汇编文本。** 只使用了不受
版权保护的事实性信息（寄存器地址、协议时序、磁盘格式），所有实现与错误由本项目自负。
若你重新分发本项目，请同时保留本声明与上游链接。

## 4. 算法参考：FatFs（ChaN）

`src/fatw.c` 的目录项分配（`dir_seek` / `dir_alloc` / `dir_put` / `dir_append`）在
**算法结构**上参照了 ChaN 的 FatFs `ff.c`（`dir_find` / `dir_alloc` / `dir_register` /
`dir_next`），包括「`$E5` 与 `$00` 同等当空槽」「连续槽不够就扩簇」两条关键判断。

**没有把 FatFs 的代码编进 ROM**，也没有逐行照抄：函数签名、缓冲模型（本项目的 `fbuf`
是单扇区窗口，FatFs 是 `move_window` 窗口对象）、错误码与调用约定都不同，且去掉了
与 32 KB 目标无关的全部特性（多卷、f_mkfs、f_findfirst、exFAT、长名缓冲池等）。
FatFs 的许可证为 ChaN 的单条款 BSD 风格（可自由使用、修改、再分发，保留版权声明），
与本项目的 AGPL-3.0 兼容；即使按最保守的口径把它当作衍生，也不影响本项目的授权。
上游：<http://elm-chan.org/fsw/ff/>。

## 5. 工具链

- **SDCC**（编译器与 `sdasgb`/`sdldgb`）：GPL-2.0-or-later；其运行时库为
  **GPLv2 + Linking Exception**，明确允许与任意许可证的程序链接，
  所以本项目的 ROM **不因链接 SDCC 运行时库而受 GPL 约束**。
- **PyBoy**（仅测试用，不进入产物）：测试脚本用它跑 ROM 并截图。
- **Python / gcc**：仅构建与测试脚本使用。

## 6. 不含官方固件

本仓库**不包含**：

- EZ-FLASH 官方内核 `ezgb.dat`（任何版本，含 1.04c 中文版、1.05e）
- 官方固件更新包 `Update_FW4.gb` / `Update_FW5.gb`
- 官方内核里的中文字库点阵（属 EZ-FLASH 专有数据）
- 任何游戏 ROM、存档

`.gitignore` 已拦截 `*.dat *.gb *.gbc *.sav build/`（`EZJR.FNT` 是生成物，同样不入库，
随 Release 附件分发）。

## 7. 免责声明

改内核、刷固件、写 SD 卡都有变砖或丢存档的风险。上卡前请备份原 `ezgb.dat` 与全部存档，
风险自负。本项目按现状提供、不提供任何担保。
