#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
emu_test.py — 在 PyBoy 里真跑 ezgb.dat，验证界面与设置时间的完整路径。

为什么需要它：静态校验只能证明 ROM 结构合法，不能证明程序跑得起来。本脚本
用 PyBoy（无头 Game Boy 模拟器）加载 ROM，逐帧运行，然后：

  1) 正式版（build/ezgb.dat）：验证启动、LCD 初始化、ASCII 字库与汉字点阵
     真的被画到 tilemap 上。模拟器没有 SD 卡，所以应停在「存储卡错误」界面。
  2) 自检版（build/ezgb-test.dat，EZJR_TEST=1 构建）：把 SD 读路径换成固定
     清单、RTC 窗口换成 RAM 镜像，从而驱动「游戏列表」与「设置时间」两个
     界面：翻页、移动光标、改小时、按 A 保存，并读回写出的 BCD 值。

注意：PyBoy 不模拟 EZ-Flash 的 FPGA（SD 读、PCF8563、$7Fxx 解锁序列都无效），
所以「真机/整机」验证仍需上手实测，见 docs/04-boundaries.md。

用法：
  python3 tools/emu_test.py            # 两套都跑
  python3 tools/emu_test.py --shots    # 顺便把截图存到 docs/shots/
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(ROOT, "build")

TILE_BASE = 0x9800
SCR_W = 20


SYM_CH = {1: "\u25b6", 2: "\u2500", 3: "\u2501", 4: "\u2588", 5: "\u2584",
          6: "\u2580", 7: "\u258c", 8: "\u2590", 9: "\u25b2", 10: "\u25bc",
          11: "\u25cf"}


def tile_char(t):
    """tilemap 编号 -> 可读字符。

    $00 空白 / $01-$0B 符号 / $0C-$1F 汉字池 A / $20-$7E ASCII（编号 == 字符码）
    / $7F-$FF 汉字池 B。"""
    if t == 0:
        return " "
    if t in SYM_CH:
        return SYM_CH[t]
    if 0x20 <= t <= 0x7E:
        return chr(t)
    return "#"                              # 汉字 tile


def is_cjk(t):
    """是否是汉字动态池里的 tile（$0C-$1F 或 $7F-$FF 两段）。"""
    return 0x0C <= t <= 0x1F or 0x7F <= t <= 0xFF


def row_text(py, row):
    return "".join(tile_char(py.memory[TILE_BASE + row * 32 + c]) for c in range(SCR_W)).rstrip()


def row_raw(py, row):
    return [py.memory[TILE_BASE + row * 32 + c] for c in range(SCR_W)]


def save_shot(py, shots, name):
    """按 BGP 把 BG 层画成 PNG。

    为什么不用 py.screen.image：PyBoy 在 window="null" 下这个接口拿到的是
    一张全白图（LCDC 状态是对的，但帧缓冲没同步），没法用来核对排版。这里
    直接按 tilemap + tile 数据 + BGP 自己算，得到的就是真机上的画面。
    """
    from PIL import Image
    img = Image.new("L", (160, 144), 255)
    px = img.load()
    scx, scy = py.memory[0xFF43], py.memory[0xFF42]
    bgp = py.memory[0xFF47]
    for y in range(144):
        row = ((y + scy) & 0xFF) >> 3
        for x in range(160):
            col = ((x + scx) & 0xFF) >> 3
            t = py.memory[0x9800 + row * 32 + col]
            fy, fx = (y + scy) & 7, (x + scx) & 7
            lo = py.memory[0x8000 + t * 16 + fy * 2]
            hi = py.memory[0x8000 + t * 16 + fy * 2 + 1]
            bit = 7 - fx
            v = (((hi >> bit) & 1) << 1) | ((lo >> bit) & 1)
            px[x, y] = 255 - ((bgp >> (v * 2)) & 3) * 85
    img.resize((640, 576), Image.NEAREST).save(os.path.join(shots, name))


def load_map_symbols(test=True):
    """从链接 map 里取 C 全局符号地址（测试桩用）。"""
    path = os.path.join(BUILD, "obj-test" if test else "obj", "ezgb.map")
    syms = {}
    if not os.path.exists(path):
        return syms
    for v, k in re.findall(r"([0-9A-Fa-f]{8})\s+[sSlL]__(\w+)", open(path).read()):
        syms[k] = int(v, 16)
    # 全局/静态变量与函数：map 里形如 "     0000C7E4  _g_test_rtc_win   test_stub"
    for v, k in re.findall(r"^\s+([0-9A-Fa-f]{8})\s+_(\w+)\s", open(path).read(), re.M):
        syms.setdefault(k, int(v, 16))
    return syms


class Result:
    def __init__(self):
        self.fails = 0
        self.checks = 0

    def ck(self, name, cond, extra=""):
        self.checks += 1
        if not cond:
            self.fails += 1
        print(("  OK   " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))

    def done(self, title):
        print(f"  -- {title}: {self.checks - self.fails}/{self.checks} 通过")
        return self.fails


def bcd(v):
    return ((v // 10) << 4) | (v % 10)


def bcd2(v):
    return (v >> 4) * 10 + (v & 0x0F)


def run_stock(r, shots=None):
    """正式版：应当跑到「存储卡错误」。"""
    from pyboy import PyBoy
    rom = os.path.join(BUILD, "ezgb.dat")
    if not os.path.exists(rom):
        print("  跳过：build/ezgb.dat 不存在，先跑 ./build.sh")
        return 0
    py = PyBoy(rom, window="null", sound_emulated=False)
    for _ in range(200):
        py.tick()

    print("\n[1] 正式版 build/ezgb.dat（模拟器无 SD 卡，应停在错误界面）")
    r.ck("LCD 已开启 (LCDC bit7)", py.memory[0xFF40] & 0x80)
    r.ck("BGP 已设置为 0xE4", py.memory[0xFF47] == 0xE4)
    r.ck("SCX/SCY 归零", py.memory[0xFF43] == 0 and py.memory[0xFF42] == 0)

    vr = sum(1 for a in range(0x8000, 0x9800) if py.memory[a])
    r.ck("VRAM 已有 tile 数据（符号+ASCII 已上传）", vr > 300, f"{vr} 非零字节")

    ji = row_raw(py, 6)
    cjk = sum(1 for t in ji if is_cjk(t))
    r.ck("第 6 行出现汉字 tile（「存储卡错误」5 字）", cjk >= 5, f"{cjk} 个")

    # 标题已经是中文，下面不再重复一行英文；原因也用汉字写。
    r.ck("不再有英文副标题 MOUNT FAILED", "MOUNT" not in row_text(py, 8),
         repr(row_text(py, 8)))
    why9 = row_text(py, 9)
    cjk9 = sum(1 for t in row_raw(py, 9) if is_cjk(t))
    r.ck("第 9 行用中文说明原因（「无法识别此卡」6 字）", cjk9 == 6, f"{cjk9} 个 tile / {why9!r}")
    r.ck("正式版不显示错误码 ERR=",
         "ERR=" not in row_text(py, 10) and "ERR=" not in row_text(py, 12),
         repr(row_text(py, 10) + "|" + row_text(py, 12)))

    # 8x8 汉字：一个字一个 tile（$80 起递增），去重后就是字数
    hi = [t for t in ji if is_cjk(t)]
    r.ck("汉字 tile 一字一格（去重后 >= 5 组）", len(set(hi)) >= 5, f"{len(set(hi))} 组")
    r.ck("汉字 tile 都在动态池里", all(is_cjk(t) for t in hi), f"{sorted(set(hi))}")

    if shots:
        save_shot(py, shots, "01-sd-error.png")
    py.stop(save=False)
    return r.fails


def run_test_rom(r, shots=None):
    """自检版：驱动游戏列表与设置时间。"""
    from pyboy import PyBoy
    rom = os.path.join(BUILD, "ezgb-test.dat")
    if not os.path.exists(rom):
        print("  跳过：build/ezgb-test.dat 不存在，先跑 EZJR_TEST=1 ./build.sh")
        return 0
    syms = load_map_symbols()
    win = syms.get("g_test_rtc_win")
    if win is None:
        print("  跳过：map 里找不到 g_test_rtc_win")
        return 0

    py = PyBoy(rom, window="null", sound_emulated=False)
    for _ in range(150):
        py.tick()

    print(f"\n[2] 自检版 build/ezgb-test.dat（RTC 窗口镜像在 WRAM ${win:04X}）")

    def press(btn, hold=4, settle=26):
        """按键：按住 hold 帧再松开，然后等整屏重绘完成。
        重绘（clear_screen + 字库上传 + draw）实测约 4-10 帧，settle 给足。"""
        py.button_press(btn)
        for _ in range(hold):
            py.tick()
        py.button_release(btn)
        for _ in range(settle):
            py.tick()

    # ---- 游戏列表 ----
    print("\n  游戏列表（标签 0 / 分隔 1 / 列表 3-15 / 计数 17）")
    r0 = row_raw(py, 0)
    r.ck("行 0 有两个中文标签", sum(1 for t in r0 if is_cjk(t)) >= 4, repr(row_text(py, 0)))
    r.ck("行 0 用 [ ] 标出当前标签", row_text(py, 0).startswith("[")
         and row_text(py, 0).count("]") == 1, repr(row_text(py, 0)))
    r.ck("行 1 是整行粗分隔线", all(t == 3 for t in row_raw(py, 1)), repr(row_raw(py, 1)))
    r3 = row_raw(py, 3)
    # v0.9.0：列表顺序改为「目录在前、文件在后」，根目录第一页开头变成两个文件夹。
    # 这样分类在视觉上自然分层，翻页找游戏也不用翻到最后才看到文件夹。
    r.ck("首项是文件夹 GBC/（目录排在文件前面）",
         r3[0] == 1 and row_text(py, 3).startswith("\u25b6 GBC/"),
         repr(row_text(py, 3)))
    r.ck("第 2 项是中文名文件夹「游戏时间/」",
         row_raw(py, 4)[0] != 1
         and sum(1 for t in row_raw(py, 4) if is_cjk(t)) == 4
         and row_text(py, 4).rstrip().endswith("/"), repr(row_text(py, 4)))
    r5 = row_raw(py, 5)
    r.ck("第 3 项中文名里的 ASCII 没被吞掉（'K 游戏时间.GBC'）",
         r5[0] != 1 and r5[2] == ord("K") and r5[3] == 0x20
         and sum(1 for t in r5 if is_cjk(t)) == 4
         and r5[8] == ord(".") and r5[9] == ord("G")
         and r5[10] == ord("B") and r5[11] == ord("C"),
         repr(row_text(py, 5)))
    r.ck("第 4 项也是中文名（「游戏.GB」，2 个字）",
         sum(1 for t in row_raw(py, 6) if is_cjk(t)) == 2, repr(row_text(py, 6)))
    r.ck("没有长名的项退回 8.3 短名", row_text(py, 8).strip() == "ZELDA.GB",
         repr(row_text(py, 8)))
    r.ck("第 12 项占满最后一列表行", row_text(py, 14).strip() == "ALLEY.GB",
         repr(row_text(py, 14)))
    c17 = row_text(py, 17)
    r.ck("行 17 显示「20 个游戏」（文件夹不算进去）",
         "20" in c17 and any(is_cjk(t) for t in row_raw(py, 17)),
         repr(c17))
    all_txt = "".join(row_text(py, i2) for i2 in range(18))
    r.ck("界面上没有按键提示字样",
         not any(w in all_txt for w in ("UP/DN", "L/R", "A:RUN", "B:TIME", "SEL")),
         repr(all_txt[:60]))
    if shots:
        save_shot(py, shots, "02-list.png")

    press("down")
    r.ck("按↓ 光标移到第 2 项", row_raw(py, 4)[0] == 1 and row_raw(py, 3)[0] != 1,
         f"行3={row_text(py,3)!r} 行4={row_text(py,4)!r}")
    press("down"); press("down")
    r.ck("再按↓↓ 到第 4 项「游戏.GB」", row_raw(py, 6)[0] == 1
         and row_text(py, 6).strip() == "\u25b6 ##.GB", repr(row_text(py, 6)))

    # ---- 翻页：光标走出本页时自动翻页 ----
    print("\n  翻页（光标移出本页自动翻）")
    for _ in range(10):                 # 索引 3 -> 13，正是第 2 页第 2 项
        press("down")
    r.ck("移到第 14 项时自动翻页，首项 BOMBER.GB",
         row_text(py, 3).startswith("\u25b6 BOMBER.GB"), repr(row_text(py, 3)))
    if shots:
        save_shot(py, shots, "03-page2.png")
    for _ in range(13):                 # 索引 13 -> 0
        press("up")
    r.ck("按↑ 转回第 1 页首项（文件夹 GBC/）", row_raw(py, 3)[0] == 1
         and row_text(py, 3).startswith("\u25b6 GBC/"), repr(row_text(py, 3)))

    # ---- 左右键整页翻（v0.14.0）----
    # 20 个游戏 + 2 个文件夹 = 22 条，每页 13 行 → 第 1 页 13 条、第 2 页 9 条。
    print("\n  左右键翻页（← 上一页 / → 下一页）")
    press("left")                       # 已在第 1 页
    r.ck("第 1 页按 ← 不动（不绕回到末页）",
         row_text(py, 3).startswith("\u25b6 GBC/"), repr(row_text(py, 3)))
    press("right")
    r.ck("按 → 整页翻到第 2 页，光标跟到首行",
         row_text(py, 3).startswith("\u25b6 BOMBER.GB"), repr(row_text(py, 3)))
    if shots:
        save_shot(py, shots, "03b-page-right.png")
    press("right")                      # 只有两页
    r.ck("末页再按 → 停住（仍是 BOMBER.GB 在首行）",
         row_text(py, 3).startswith("\u25b6 BOMBER.GB"), repr(row_text(py, 3)))
    press("left")
    r.ck("按 ← 回到第 1 页，光标仍在首行",
         row_text(py, 3).startswith("\u25b6 GBC/"), repr(row_text(py, 3)))

    # 翻页要保住光标的行号，否则连按几下 → 就得每页重新找位置
    # 用 ZELDA.GB（无长名，行 8）当参照：有长名的项画成汉字，比对起来不方便
    for _ in range(5):                  # 索引 0 -> 5（行 8，ZELDA.GB）
        press("down")
    r.ck("翻页前光标在第 6 行（ZELDA.GB）",
         row_raw(py, 8)[0] == 1 and row_text(py, 8).strip() == "\u25b6 ZELDA.GB",
         repr(row_text(py, 8)))
    press("right")
    r.ck("翻页后光标还在第 6 行，名字换成第 2 页第 6 项（DUCK.GB）",
         row_raw(py, 8)[0] == 1 and row_text(py, 8).strip() == "\u25b6 DUCK.GB"
         and row_raw(py, 3)[0] != 1, repr(row_text(py, 8)))

    # 末页只有 9 条：第 1 页最后一行（相对行 12）翻过去会越过末尾，要贴到最后一条
    press("left")                       # 回第 1 页，相对行号仍 5
    for _ in range(7):                  # 索引 5 -> 12（行 15，CONTRA.GB）
        press("down")
    r.ck("光标到第 1 页最后一行（CONTRA.GB）",
         row_raw(py, 15)[0] == 1 and row_text(py, 15).strip() == "\u25b6 CONTRA.GB",
         repr(row_text(py, 15)))
    press("right")
    r.ck("末页不满时贴到最后一条，不落在空行上（LAST.GB / 行 11）",
         row_raw(py, 11)[0] == 1 and row_text(py, 11).strip() == "\u25b6 LAST.GB"
         and not row_text(py, 12).strip(), repr(row_text(py, 11)))
    if shots:
        save_shot(py, shots, "03c-page-clamp.png")

    # 复原到首项，后面子目录用例按「光标在 GBC/ 上」写
    press("left")                       # 索引 21 -> 8（相对行号 8 保住）
    for _ in range(8):                  # 索引 8 -> 0
        press("up")
    r.ck("左右翻页后能回到首项 GBC/",
         row_text(py, 3).startswith("\u25b6 GBC/"), repr(row_text(py, 3)))

    # ---- 子目录浏览：文件夹带 /，A 进去、B 回上级 ----
    print("\n  子目录浏览（文件夹带 / 标记）")
    r.ck("根目录开头就是文件夹，名字后面带 /",
         row_text(py, 3).startswith("\u25b6 GBC/")
         and row_text(py, 4).rstrip().endswith("/"), repr(row_text(py, 4)))
    if shots:
        save_shot(py, shots, "11-subdir-folders.png")

    press("a")                          # 光标在 GBC/ 上，A 进入
    r.ck("按 A 进入文件夹：首项是「..」",
         row_text(py, 3).startswith("\u25b6 .."), repr(row_text(py, 3)))
    r.ck("文件夹里只列本目录的内容，计数只算真正的游戏",
         sum(1 for t in row_raw(py, 4) if is_cjk(t)) == 4
         and row_text(py, 17).strip().startswith("3"),
         f"行4={row_text(py, 4)!r} 行17={row_text(py, 17)!r}")
    if shots:
        save_shot(py, shots, "12-subdir-inside.png")

    press("b")                          # B 回上级
    r.ck("按 B 回到上级目录（首项又是 GBC/）",
         row_raw(py, 3)[0] == 1 and row_text(py, 3).startswith("\u25b6 GBC/")
         and row_text(py, 17).strip().startswith("20"), repr(row_text(py, 3)))

    # ---- 设置标签 ----
    print("\n  设置标签（SELECT 切过去）")
    seed = {0x08: bcd(5), 0x09: bcd(30), 0x0A: bcd(14), 0x0B: bcd(11),
            0x0C: 3, 0x0D: bcd(11), 0x0E: bcd(26)}
    for off, v in seed.items():
        py.memory[win + off] = v

    press("select", settle=34)
    r.ck("按 SELECT 切到设置标签（[ ] 移过去了）",
         row_text(py, 0).count("]") == 1 and row_raw(py, 0)[0] != ord("["),
         repr(row_text(py, 0)))
    d3 = "".join(tile_char(t) for t in row_raw(py, 3))
    print(f"       时间行: {d3!r}")
    r.ck("时间行 = 26-11-11 14:30:05（一行 17 列）",
         d3[0:2] == "26" and d3[2] == "-" and d3[3:5] == "11" and d3[5] == "-"
         and d3[6:8] == "11" and d3[8] == " " and d3[9:11] == "14" and d3[11] == ":"
         and d3[12:14] == "30" and d3[14] == ":" and d3[15:17] == "05",
         repr(d3[:17]))
    r.ck("时间行右侧是「设置」按钮（2 个汉字，列 18-19）",
         is_cjk(row_raw(py, 3)[18]) and is_cjk(row_raw(py, 3)[19]), repr(d3[18:20]))
    r.ck("行 4 有焦点线（时间按钮下方）",
         all(t == 3 for t in row_raw(py, 4)[18:20]), repr(row_raw(py, 4)[18:20]))
    r.ck("行 6 = 自动备份 / 自动 / 设置",
         all(is_cjk(row_raw(py, 6)[c]) for c in (0, 2, 4, 6))
         and all(is_cjk(row_raw(py, 6)[c]) for c in (9, 11))
         and is_cjk(row_raw(py, 6)[18]), repr(row_text(py, 6)))
    r.ck("行 7 没有焦点线（焦点在时间条目上）",
         all(t in (0, 0x20) for t in row_raw(py, 7)[18:20]), repr(row_raw(py, 7)[18:20]))
    # v0.12.0：设置页显示固件版本号；v0.12.2 起内核版本跟发布号同步（带补丁号），
    # 列位置由 main.c 的 VER_COL 按 sizeof(MZOS_VER) 算出来，所以这里不写死列号，
    # 直接按「MZOS V<主>.<次>[.<补丁>] FW<固件号>」整行匹配。
    d9 = "".join(tile_char(t) for t in row_raw(py, 9))
    print(f"       版本行: {d9!r}")
    mv = re.match(r"^MZOS V(\d+)\.(\d+)(?:\.(\d+))? FW(--|\d+)$", d9.strip())
    r.ck("行 9 = 「MZOS V<主>.<次>[.<补丁>] FW<固件号>」，固件号不补零",
         bool(mv) and (mv.group(4) == "--" or not mv.group(4).startswith("0")),
         repr(d9))
    # 版本号必须跟着 main.c 的 MZOS_VER 走，不能在这里写死一个字面量 ——
    # 否则改了版本号这里还会绿，屏上却还是老号（v0.12.1 就吃过这个亏）。
    with open(os.path.join(ROOT, "src", "main.c"), encoding="utf-8") as f:
        cur = re.search(r'#define\s+MZOS_VER\s+"(V[\d.]+)"', f.read()).group(1)
    r.ck("版本号与 main.c 的 MZOS_VER 一致",
         f"MZOS {cur} " in d9, f"{cur} vs {d9!r}")
    set_btn = tuple(row_raw(py, 3)[18:20])
    if shots:
        save_shot(py, shots, "04-settings.png")

    # ---- 进编辑：按钮变「保存」、光标出现在年上 ----
    print("\n  时间编辑（A 进入 / 左右选字段 / 上下改值 / 保存）")
    press("a")
    save_btn = tuple(row_raw(py, 3)[18:20])
    r.ck("按 A 进入编辑，按钮从「设置」变「保存」",
         save_btn != set_btn and all(is_cjk(t) for t in save_btn),
         f"{set_btn} -> {save_btn}")
    r.ck("▼ 默认指在「年」上方（列 0）", row_raw(py, 2)[0] == 10,
         f"行2 列0 = {row_raw(py, 2)[0]}")
    if shots:
        save_shot(py, shots, "05-editing.png")

    py.memory[win + 0x08] = bcd(40)     # RTC 走了一秒，编辑时不该跟
    for _ in range(30):
        py.tick()
    r.ck("编辑中时间冻结（RTC 变了屏上不动）",
         "".join(tile_char(t) for t in row_raw(py, 3))[15:17] == "05",
         "".join(tile_char(t) for t in row_raw(py, 3))[15:17])

    press("right"); press("right")
    r.ck("按→→ 光标移到「日」（列 6）",
         row_raw(py, 2)[6] == 10 and row_raw(py, 2)[0] in (0, 0x20),
         f"{row_raw(py,2)[0]:02X} / {row_raw(py,2)[6]:02X}")
    press("up")
    d3b = "".join(tile_char(t) for t in row_raw(py, 3))
    r.ck("按↑ 把日 11 改成 12", d3b[6:8] == "12", repr(d3b[6:8]))
    press("down")
    d3c = "".join(tile_char(t) for t in row_raw(py, 3))
    r.ck("按↓ 退回 11", d3c[6:8] == "11", repr(d3c[6:8]))
    press("up")                          # 再改成 12，等下验证保存

    for _ in range(3):                   # 日(2) -> 时(3) 分(4) 秒(5)
        press("right")
    r.ck("再按→→→ 光标到「秒」（列 15）", row_raw(py, 2)[15] == 10,
         f"行2 列15 = {row_raw(py, 2)[15]}")
    press("right")
    r.ck("再按→ 到「保存」按钮（列 18）", row_raw(py, 2)[18] == 10,
         f"行2 列18 = {row_raw(py, 2)[18]}")

    press("a")
    r.ck("在「保存」上按 A，按钮变回「设置」",
         tuple(row_raw(py, 3)[18:20]) == set_btn, repr(row_raw(py, 3)[18:20]))
    r.ck("光标消失", all(t in (0, 0x20) for t in row_raw(py, 2)))
    r.ck("行 17 提示「已保存」",
         sum(1 for t in row_raw(py, 17) if is_cjk(t)) >= 3, repr(row_text(py, 17)))

    # ---- 保存后时间重新开始走 ----
    print("\n  保存后时间继续流动")
    py.memory[win + 0x08] = bcd(41)
    for _ in range(20):
        py.tick()
    sec_now = "".join(tile_char(t) for t in row_raw(py, 3))[15:17]
    r.ck("RTC 秒变了，屏上自动跟到 41", sec_now == "41", repr(sec_now))
    got = {off: py.memory[win + off] for off in range(0x08, 0x0F)}
    r.ck("写回的日 = 12（刚改的值）", got[0x0B] == bcd(12), hex(got[0x0B]))
    r.ck("写回的时/分 未被误改", got[0x0A] == bcd(14) and got[0x09] == bcd(30),
         f"{got[0x0A]:02X} {got[0x09]:02X}")
    r.ck("写回的星期是推算值（12 号是周四 -> 4）", got[0x0C] == 4, hex(got[0x0C]))

    # ---- B 键放弃修改 ----
    print("\n  编辑中按 B 放弃修改")
    press("a")
    press("right"); press("right"); press("right")   # 到「时」
    press("up"); press("up")                          # 时 14 -> 16
    hour_before = "".join(tile_char(t) for t in row_raw(py, 3))[9:11]
    press("b")
    hour_after = "".join(tile_char(t) for t in row_raw(py, 3))[9:11]
    r.ck("按 B 还原成编辑前的值",
         hour_before == "16" and hour_after in ("14", "41"),
         f"{hour_before!r} -> {hour_after!r}")
    r.ck("按 B 后按钮也是「设置」", tuple(row_raw(py, 3)[18:20]) == set_btn,
         repr(row_raw(py, 3)[18:20]))

    # ---- 自动备份开关 ----
    print("\n  自动备份开关")
    press("down")
    r.ck("按↓ 焦点移到备份条目（行 7 出焦点线）",
         all(t == 3 for t in row_raw(py, 7)[18:20])
         and all(t in (0, 0x20) for t in row_raw(py, 4)[18:20]),
         f"行4={row_raw(py,4)[18:20]} 行7={row_raw(py,7)[18:20]}")
    auto_tiles = tuple(row_raw(py, 6)[9:12])
    ab = syms.get("g_test_auto_backup")
    r.ck("默认是「手动」备份（PSRAM 掉电后也落回手动）",
         ab is not None and py.memory[ab] == 0,
         f"auto={py.memory[ab] if ab else '?'}")
    press("a")
    manual_tiles = tuple(row_raw(py, 6)[9:12])
    r.ck("按 A 从「手动」切到「自动」", manual_tiles != auto_tiles,
         f"{auto_tiles} -> {manual_tiles}")
    r.ck("切到自动后开关也变成 1", ab is not None and py.memory[ab] == 1,
         f"auto={py.memory[ab] if ab else '?'}")
    press("a")
    r.ck("再按 A 切回「手动」", tuple(row_raw(py, 6)[9:12]) == auto_tiles,
         repr(row_raw(py, 6)[9:12]))
    r.ck("切回后开关也变回 0", ab is not None and py.memory[ab] == 0,
         f"auto={py.memory[ab] if ab else '?'}")
    if shots:
        save_shot(py, shots, "06-backup-switch.png")

    # ---- 切回游戏页 ----
    print("\n  回到游戏页")
    press("select", settle=34)
    r.ck("再按 SELECT 回到游戏页", row_raw(py, 3)[0] == 1, repr(row_text(py, 3)))
    if shots:
        save_shot(py, shots, "07-back-to-list.png")

    # ---- START 菜单 ----
    # 这会儿光标停在第 1 项（文件夹 GBC/），所以第二项应该显示「目录不能删」。
    print("\n  START 菜单（光标在文件夹上）")
    press("start", settle=34)
    r.ck("菜单项 1「运行上次游戏」", sum(1 for t in row_raw(py, 6) if is_cjk(t)) == 6,
         repr(row_text(py, 6)))
    r.ck("菜单项 1 下面挂着上次运行的游戏名（中文名，不是 8.3 短名）",
         sum(1 for t in row_raw(py, 8) if is_cjk(t)) >= 4, repr(row_text(py, 8)))
    # diag 版菜单多一行「自检」，所以底线/提示行比正式版低两行
    foot = 16
    r.ck("光标在文件夹上时第二项是「目录不能删」（5 字）",
         sum(1 for t in row_raw(py, 10) if is_cjk(t)) == 5, repr(row_text(py, 10)))
    r.ck("底部提示「B 返回」", "B" in row_text(py, foot)
         and sum(1 for t in row_raw(py, foot) if is_cjk(t)) == 2, repr(row_text(py, foot)))
    # v0.12.0：不反色、去掉左右竖边框、上下两条细线变短，名字行不再加框
    r.ck("菜单不反色：仍是白底黑字（BGP=$E4），跟列表同一套配色",
         py.memory[0xFF47] == 0xE4, hex(py.memory[0xFF47]))
    r.ck("左右竖边框已去掉（各行末列全空，首列只有 ▶）",
         all(row_raw(py, rr)[19] == 0 for rr in (2, 3, 5, 6, 8, 10, 12, 13))
         and all(row_raw(py, rr)[0] in (0, 0x01)
                 for rr in (2, 3, 5, 6, 8, 10, 12, 13)),
         repr([[row_raw(py, rr)[0], row_raw(py, rr)[19]]
               for rr in (2, 3, 5, 6, 8, 10, 12, 13)]))
    r.ck("上下两条细线只画中间 16 列（col 2-17），不顶到屏幕边缘",
         all(t == 0x02 for t in row_raw(py, 4)[2:18])
         and row_raw(py, 4)[:2] == [0, 0] and row_raw(py, 4)[18:] == [0, 0]
         and all(t == 0x02 for t in row_raw(py, 14)[2:18])
         and row_raw(py, 14)[:2] == [0, 0] and row_raw(py, 14)[18:] == [0, 0],
         repr(row_raw(py, 4)) + " / " + repr(row_raw(py, 14)))
    r.ck("文件名行不再加框（行 7 / 行 9 都没有框线）",
         row_raw(py, 7)[2] != 0x02 and row_raw(py, 9)[2] != 0x02,
         repr(row_text(py, 7)) + " / " + repr(row_text(py, 9)))
    r.ck("文件名行是「空格─」引导（col2 空、col3 横线）+ 缩进到第 6 列",
         row_raw(py, 8)[2] == 0 and row_raw(py, 8)[3] == 0x02
         and row_raw(py, 8)[4] == 0 and row_raw(py, 8)[5] == 0
         and (is_cjk(row_raw(py, 8)[6]) or 0x20 <= row_raw(py, 8)[6] <= 0x7E),
         repr(row_text(py, 8)) + " " + repr(row_raw(py, 8)[:8]))
    if shots:
        save_shot(py, shots, "13-menu-dir.png")
    press("b", settle=30)
    r.ck("B 关掉菜单回到列表", row_raw(py, 3)[0] == 1, repr(row_text(py, 3)))
    r.ck("关菜单后调色板回到白底黑字", py.memory[0xFF47] == 0xE4,
         hex(py.memory[0xFF47]))

    # 首次开机（元数据页是擦除态）：菜单不能有乱码、也不能画名字
    no_last = syms.get("g_test_no_last")
    if no_last:
        py.memory[no_last] = 1
        press("start", settle=34)
        r.ck("首次按 START：第一项是「无上次游戏」（5 字）",
             sum(1 for t in row_raw(py, 6) if is_cjk(t)) == 5, repr(row_text(py, 6)))
        r.ck("首次按 START：名字行一个字都没有（不显示乱码）",
             all(t == 0 for t in row_raw(py, 8)[2:19]), repr(row_text(py, 8)))
        r.ck("首次按 START：连子信息细线框都不画",
             row_raw(py, 7)[2] == 0 and row_raw(py, 9)[2] == 0,
             repr(row_text(py, 7)) + " / " + repr(row_text(py, 9)))
        press("b", settle=30)
        py.memory[no_last] = 0

    # 移到第一个 ROM 上再开一次：第二项应当变成「删除当前文件」（6 字）
    print("\n  START 菜单（光标在 ROM 上）")
    press("down"); press("down")
    press("start", settle=34)
    r.ck("光标在 ROM 上时第二项是「删除当前文件」（6 字）",
         sum(1 for t in row_raw(py, 10) if is_cjk(t)) == 6, repr(row_text(py, 10)))
    if shots:
        save_shot(py, shots, "14-menu-rom.png")
    press("down")
    r.ck("下键把 ▶ 移到第 2 项",
         row_raw(py, 10)[0] == 1 and row_raw(py, 6)[0] != 1,
         repr(row_text(py, 6)) + " / " + repr(row_text(py, 10)))
    press("down")
    r.ck("diag 版菜单多一项「自检」", sum(1 for t in row_raw(py, 12) if is_cjk(t)) == 2,
         repr(row_text(py, 12)))

    # ---- 自检屏（仅有 DIAG=1 时；测试版强制打开）----
    # 先灌一份干净值：自检屏直接印寄存器，不该受前面编辑的影响
    for off, v in seed.items():
        py.memory[win + off] = v
    py.memory[win + 0x08] = bcd(15)

    print("\n  真机自检屏（菜单第三项进入，A 返回）")
    press("a", settle=30)
    r.ck("自检屏标题有汉字", any(is_cjk(t) for t in row_raw(py, 0)), repr(row_text(py, 0)))
    # v0.13.0 版式：只留 FW / MOUNT / REC / LFN / FAT+SPC / 解码后的 RTC。
    # SEC0、PT、SIG、CL、PART、RTC 原始 BCD、VL/WDAY 全砍了（32KB 贴边，
    # 那些项在 v0.11 之前就验证完），所以这里也只钉剩下的几行。
    r.ck("行 3 = FW", row_text(py, 3).startswith("FW="), repr(row_text(py, 3)))
    r.ck("行 4 = MOUNT", row_text(py, 4).startswith("MOUNT="), repr(row_text(py, 4)))
    r.ck("行 8 = REC（记录读回来没）", row_text(py, 8).startswith("REC="), repr(row_text(py, 8)))
    r.ck("行 9 = LFN（长名查到没）", row_text(py, 9).startswith("LFN="), repr(row_text(py, 9)))
    r.ck("行 10 = FAT/SPC", row_text(py, 10).startswith("FAT=16") and "SPC=8" in row_text(py, 10),
         repr(row_text(py, 10)))
    r.ck("行 13 = 解码时间 T=14:30:15", row_text(py, 13).strip() == "T=14:30:15",
         repr(row_text(py, 13)))
    r.ck("行 15 = 解码日期 D=2026-11-11", row_text(py, 15).strip() == "D=2026-11-11",
         repr(row_text(py, 15)))
    if shots:
        save_shot(py, shots, "07-diag.png")
    press("a", settle=30)
    # 进菜单前光标在第 3 项（第一个 ROM），所以回来时 ▶ 还在行 5
    r.ck("A 返回游戏列表，光标没被菜单打乱",
         row_text(py, 5).startswith("\u25b6"), repr(row_text(py, 5)))
    press("up"); press("up")             # 归位到第 1 项（文件夹），下面的用例按这个假设写
    r.ck("上键走回列表顶部（第 1 项是文件夹）",
         row_text(py, 3).startswith("\u25b6 GBC/"), repr(row_text(py, 3)))

    # ---- 启动游戏：模拟器里必然走失败路径 ----
    # PyBoy 没有 EZ-Flash 的 FPGA：$7F36=3 不会真的装载，$7FE0=$80 也不会
    # 复位进游戏。测试桩让 game_launch() 直接返回"不是 GB ROM"，所以这里
    # 验证的是「A 键接进去了 + 错误屏画得对 + 能自动回列表」这一段。
    print("\n  启动游戏（模拟器无 FPGA，走失败提示）")
    # 光标这会儿停在第 1 项（文件夹 GBC/）。v0.9.0 起目录排在文件前面，
    # 直接按 A 会「进文件夹」而不是「启动游戏」—— 先移到第一个 ROM 上。
    press("down"); press("down")
    r.ck("光标移到第一个游戏（不是文件夹）",
         row_text(py, 5).startswith("\u25b6 K"), repr(row_text(py, 5)))
    press("a", settle=30)
    r.ck("标题是「启动失败」", any(is_cjk(t) for t in row_raw(py, 6)), repr(row_text(py, 6)))
    r.ck("不再有英文副标题 LAUNCH FAILED", "LAUNCH" not in row_text(py, 8),
         repr(row_text(py, 8)))
    cjk9 = sum(1 for t in row_raw(py, 9) if is_cjk(t))
    r.ck("第 9 行用中文说明原因（「不是游戏卡」5 字）", cjk9 == 5,
         f"{cjk9} 个 tile / {row_text(py, 9)!r}")
    # v0.10.0：连 diag 版也不显示 ERR= —— 上面一行已经写明原因了
    r.ck("不再显示 ERR= 错误码",
         all("ERR" not in row_text(py, i) for i in range(11, 15)),
         repr(row_text(py, 12)))
    if shots:
        save_shot(py, shots, "08-launch-fail.png")
    for _ in range(200):                 # 错误屏停 180 帧后自动回列表
        py.tick()
    r.ck("错误屏后自动回到列表（光标还在刚才那个游戏上）",
         row_text(py, 5).startswith("\u25b6 K"), repr(row_text(py, 5)))

    # ---- 删除当前文件 ----
    print("\n  删除当前文件（菜单第二项）")
    before = row_text(py, 5)
    press("start", settle=34)
    press("down")
    press("a", settle=20)                # 删完先在行 17 提示 1 秒
    r.ck("删完在行 17 提示「已删除」", sum(1 for t in row_raw(py, 17) if is_cjk(t)) == 3,
         repr(row_text(py, 17)))
    for _ in range(80):                  # 提示 60 帧后恢复成计数
        py.tick()
    r.ck("删掉的那一项从列表里消失（列表重扫过、光标回到第 1 项）",
         row_text(py, 5) != before and row_text(py, 3).startswith("\u25b6 GBC/")
         and "K" not in row_text(py, 5),
         repr(before) + " -> " + repr(row_text(py, 3)) + " / " + repr(row_text(py, 5)))
    r.ck("计数变成 19 个游戏", row_text(py, 17).strip().startswith("19"),
         repr(row_text(py, 17)))

    # 删完之后光标回到第 1 项（文件夹），第二项又该是「目录不能删」
    press("start", settle=34)
    r.ck("删除后光标回到文件夹，第二项回到「目录不能删」",
         sum(1 for t in row_raw(py, 10) if is_cjk(t)) == 5, repr(row_text(py, 10)))
    press("b", settle=30)
    r.ck("B 返回列表", row_raw(py, 3)[0] == 1, repr(row_text(py, 3)))

    # ---- 运行上次游戏 ----
    print("\n  运行上次游戏（桩里 game_launch 必然失败 → 错误屏）")
    press("start", settle=34)
    press("a", settle=12)
    r.ck("第一项真的去启动上次那个游戏（走到启动失败屏）",
         any(is_cjk(t) for t in row_raw(py, 6)), repr(row_text(py, 6)))
    # v0.11.0：不再「切到上次游戏所在目录再找」；启动失败后必须回到按 START
    # 之前的那个列表（这会儿光标在第 1 项文件夹 GBC/）。
    for _ in range(200):
        py.tick()
    r.ck("启动失败后回到原来的列表，没停在别的目录里",
         row_text(py, 3).startswith("\u25b6 GBC/"), repr(row_text(py, 3)))

    py.stop(save=False)
    return r.fails


def run_boot_screens(r, shots=None):
    """备份流程三屏。

    save.c 在测试版不编，但备份屏的绘制已经抽到 video.c（真机同一份代码）。
    g_test_boot_screen 是测试桩的演示开关，在 ROM 起来之前写进去，
    save_boot_demo() 就画出对应那一屏并停住；截图之后写 0 放行。
    """
    from pyboy import PyBoy
    rom = os.path.join(BUILD, "ezgb-test.dat")
    if not os.path.exists(rom):
        return
    addr = load_map_symbols().get("g_test_boot_screen")
    if addr is None:
        print("  跳过备份屏：map 里没有 g_test_boot_screen")
        return

    print("\n[3] 备份流程三屏（真机同一份绘制代码）")
    cases = [
        (1, "07-backup-confirm.png", "确认屏"),
        (2, "08-backing-up.png", "进度屏"),
        (3, "09-save-finished.png", "完成屏"),
        (4, "10-save-failed.png", "失败屏"),
    ]
    for mode, shot, label in cases:
        py = PyBoy(rom, window="null", sound_emulated=False)
        for _ in range(220):            # 先等 BSS 清零、列表画完
            py.tick()
        py.memory[addr] = mode          # 再唤出对应那一屏
        for _ in range(20):
            py.tick()
        rows = [row_text(py, i) for i in range(18)]

        if mode == 1:
            r.ck("确认屏标题是「备份存档」",
                 sum(1 for t in row_raw(py, 5) if is_cjk(t)) == 4,
                 repr(rows[5]))
            r.ck("确认屏显示中文全名（含开头的 K）",
                 "K" in rows[8] and sum(1 for t in row_raw(py, 8) if is_cjk(t)) >= 4,
                 repr(rows[8]))
            r.ck("确认屏 A= 后面是「备份」",
                 rows[11].strip().startswith("A=")
                 and sum(1 for t in row_raw(py, 11) if is_cjk(t)) == 2,
                 repr(rows[11]))
            r.ck("确认屏 B= 后面是「跳过」",
                 rows[13].strip().startswith("B=")
                 and sum(1 for t in row_raw(py, 13) if is_cjk(t)) == 2,
                 repr(rows[13]))
            r.ck("确认屏没有 BACKUP/SKIP 英文提示",
                 "BACKUP" not in "".join(rows) and "SKIP" not in "".join(rows),
                 "")
        elif mode == 2:
            r.ck("进度屏是「正在备份」，且没有游戏名",
                 sum(1 for t in row_raw(py, 8) if is_cjk(t)) == 4
                 and "POKEMON" not in "".join(rows) and "~1" not in "".join(rows),
                 repr(rows[8]))
            r.ck("进度屏不再有英文 BACKING UP",
                 "BACKING" not in "".join(rows), "")
        elif mode == 3:
            # 不换画面：「正在备份」仍在行 8，完成只在下一行补「完成」
            r.ck("完成屏不换画面：「正在备份」保留 + 下方「完成」",
                 sum(1 for t in row_raw(py, 8) if is_cjk(t)) == 4
                 and sum(1 for t in row_raw(py, 10) if is_cjk(t)) == 2
                 and "SAVE OK" not in "".join(rows) and "FINISHED" not in "".join(rows)
                 and "POKEMON" not in "".join(rows) and "~1" not in "".join(rows),
                 f"{rows[8]!r} / {rows[10]!r}")
        else:
            r.ck("失败屏「正在备份」保留 + 「备份失败」/「校验失败」",
                 sum(1 for t in row_raw(py, 8) if is_cjk(t)) == 4
                 and sum(1 for t in row_raw(py, 10) if is_cjk(t)) == 4
                 and sum(1 for t in row_raw(py, 12) if is_cjk(t)) == 4
                 and "FAILED" not in "".join(rows) and "ERR=" not in "".join(rows),
                 f"{rows[8]!r} / {rows[10]!r} / {rows[12]!r}")

        if shots:
            save_shot(py, shots, shot)
        py.memory[addr] = 0               # 放行，确认它不会卡死
        for _ in range(25):
            py.tick()
        py.stop(save=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", action="store_true", help="保存截图到 docs/shots/")
    ap.add_argument("--only", choices=["stock", "test"], help="只跑其中一套")
    args = ap.parse_args()

    try:
        import pyboy  # noqa: F401
    except ImportError:
        print("  跳过：未安装 pyboy（pip install pyboy）")
        return 0

    shots = None
    if args.shots:
        shots = os.path.join(ROOT, "docs", "shots")
        os.makedirs(shots, exist_ok=True)

    r = Result()
    if args.only != "test":
        run_stock(r, shots)
    if args.only != "stock":
        run_test_rom(r, shots)
        run_boot_screens(r, shots)

    print(f"\n=== 模拟器验证结果：{'全部通过' if r.fails == 0 else str(r.fails) + ' 项失败'} ===")
    return 1 if r.fails else 0


if __name__ == "__main__":
    sys.exit(main())
