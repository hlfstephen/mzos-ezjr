#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_font.py — 生成内核的点阵字库（8x8 像素字体）。

字体来源: Fusion Pixel Font 8px 等宽简体中文版
          https://github.com/TakWolf/fusion-pixel-font
          fusion-pixel-8px-monospaced-zh_hans.ttf
          OFL-1.1，允许嵌入、修改、再分发（含点阵衍生）。

为什么是 8px: 这是给 8x8 tile 设计的像素字体，一个汉字正好一个 tile。
原来的做法是把 Noto CJK 缩到 16x16（4 tile、64 字节 2bpp），一个字占 2x2 格、
一屏最多 32 个字；换成 8px 像素字体后 1 格、8 字节，一屏 20x18 格全能放汉字，
ROM 也省下 3.8 KB。

输出:
  src/font_data.h    —— 符号 + ASCII + 汉字，全部 8x8 1bpp
  src/strings.h      —— 中文串（glyph id 序列），文案源就是下面的 UI_STRINGS
  docs/cjk-glyphs.md —— 收录汉字清单（人可读）

GB 的 tile 是 8x8 2bpp（16 字节：8 行 × (low, high)）。ROM 里只存 1bpp（8 字节），
video.c 上传时把 low/high 写成同一个字节（像素值 3 = 前景），ROM 占用减半。

用法:
  python3 tools/gen_font.py --font /path/fusion-pixel-8px-monospaced-zh_hans.ttf
  python3 tools/gen_font.py --check
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HERE = os.path.dirname(os.path.abspath(__file__))
# 字体不随仓库分发（3.6 MB，且只在重新生成字库时才用到）；生成的 font_data.h
# 已经在仓库里，日常构建根本不需要字体。要改文案重新生成时：
#   1. 从 https://github.com/TakWolf/fusion-pixel-font/releases 下 8px monospaced ttf
#   2. 解压后把 fusion-pixel-8px-monospaced-zh_hans.ttf 放到 vendor/ 下
#      （或设环境变量 EZJR_FONT 指到别处，或命令行 --font）
FONT_NAME = "fusion-pixel-8px-monospaced-zh_hans.ttf"
DEFAULT_FONT = os.environ.get("EZJR_FONT") or os.path.join(HERE, "..", "vendor", FONT_NAME)
OUT_H = os.path.join(ROOT, "src", "font_data.h")
OUT_STR = os.path.join(ROOT, "src", "strings.h")
OUT_DOC = os.path.join(ROOT, "docs", "cjk-glyphs.md")

# ---------------------------------------------------------------------------
# 界面符号。tile $01-$1F 没有字符占用，拿来做图标（C 里用 "\x01" 引用）。
# ---------------------------------------------------------------------------
UI_SYMBOLS = [
    ("ARROW",    "\u25b6", "选中箭头 ▶"),
    ("HLINE",    "\u2500", "细横线 ─"),
    ("HEAVY",    "\u2501", "粗横线 ━"),
    ("BLOCK",    "\u2588", "实心块 █"),
    ("LOWER8",   "\u2584", "下半块 ▄"),
    ("UPPER8",   "\u2580", "上半块 ▀"),
    ("LEFT8",    "\u258c", "左半块 ▌"),
    ("RIGHT8",   "\u2590", "右半块 ▐"),
    ("TRI_UP",   "\u25b2", "上三角 ▲"),
    ("TRI_DOWN", "\u25bc", "下三角 ▼"),
    ("DOT",      "\u25cf", "实心圆 ●"),
]

# ---------------------------------------------------------------------------
# 界面用到的全部中文文案 —— 整个工程唯一的中文文案源。
#   汉字去重集合（glyph id 即出现顺序，0 保留作「串结束」）
#   src/strings.h：每条文案转成 {glyph id, ..., 0} 的 STR_* 数组
#
# v0.5.0：两个标签（游戏 / 设置），SELECT 切换。设置页里两个条目各有自己的
# 按钮（设置 / 保存、自动 / 手动），所以这里多了按钮上的字。
# 界面上仍然不出现「上下调整」「左右选择」这类按键提示 —— 按钮本身就是入口。
# ---------------------------------------------------------------------------
UI_STRINGS = [
    ("GAME",        "游戏",       "标签 1"),
    ("SETTINGS",    "设置",       "标签 2 / 时间条目的按钮"),
    ("TIME",        "时间",       "设置条目名（提示语里也用到）"),
    ("AUTO_BACKUP", "自动备份",   "设置条目名"),
    ("AUTO",        "自动",       "备份状态：自动"),
    ("MANUAL",      "手动",       "备份状态：手动"),
    ("SAVE_BTN",    "保存",       "编辑时的按钮"),
    ("BACKUP_Q",    "备份存档",   "手动模式下开机确认"),
    ("YEAR",        "年",         "时间字段"),
    ("MONTH",       "月",         "时间字段"),
    ("DAY",         "日",         "时间字段"),
    ("HOUR",        "时",         "时间字段"),
    ("MINUTE",      "分",         "时间字段"),
    ("SECOND",      "秒",         "时间字段"),
    ("WEEKDAY",     "星期",       "时间字段"),
    ("NO_GAMES",    "无游戏",     "空列表"),
    ("SAVED",       "已保存",     "写入 RTC 后"),
    ("PLEASE_SET",  "请设置时间", "RTC VL 标志"),
    ("BATTERY_DRY", "电池电压低", "RTC VL / 存档掉电"),
    ("SD_ERROR",    "存储卡错误", "挂载失败"),
    ("READING",     "读取中",     "扫目录"),
    ("GAMES_UNIT",  "个游戏",     "列表计数后缀"),
    ("SELF_TEST",   "自检",       "调试屏标题（条件编译）"),
    ("LAUNCH_FAIL", "启动失败",   "启动出错"),
    # v0.7.0：手动备份确认屏的按键标签改成中文。追加在末尾，前面的 glyph id 不变。
    ("BACKUP_BTN",  "备份",       "确认屏 A= 后面的字"),
    ("SKIP_BTN",    "跳过",       "确认屏 B= 后面的字"),
    # v0.9.0：错误屏与备份屏全部中文化。
    # 注意：STR_* 里**不能混 ASCII** —— 数组元素是 glyph id，0 是串结束符，
    # 而 ASCII 没有 glyph id（会生成 0，把串截断）。所以这里的文案必须是纯汉字，
    # 需要写「FAT」「MBR」的地方一律换成汉字说法（格式 / 分区表）。
    ("BACKING_UP",  "正在备份",   "备份进度屏 行 8"),
    ("FINISHED",    "完成",       "备份成功，补在进度屏下一行"),
    ("BACKUP_FAIL", "备份失败",   "备份失败，补在进度屏下一行"),
    ("ERR_VERIFY",  "校验失败",   "写完回读比对不一致"),
    ("ERR_NOSPACE", "空间不足",   "FAT 上没有空簇"),
    ("ERR_DIRFULL", "目录已满",   "目录项写不进去"),
    ("ERR_UNKNOWN", "未知错误",   "兜底原因"),
    ("NOT_GB",      "不是游戏卡", "启动失败：ROM 头没有 Nintendo logo"),
    ("TOO_FRAG",    "文件太碎",   "启动失败：碎片超过加载命令表容量"),
    ("BAD_CHAIN",   "文件已损坏", "启动失败：簇链成环 / 越界"),
    ("NO_SIG",      "无法识别此卡", "挂载失败：引导扇区没有 55AA 签名"),
    ("FAT12_MSG",   "格式不支持", "挂载失败：FAT12"),
    ("GPT_MSG",     "分区表不支持", "挂载失败：GPT 保护分区"),
    ("NO_FS",       "卡上没有分区", "挂载失败：结构对但找不到 FAT 引导扇区"),
    # v0.10.0：列表页 START 菜单。菜单项 1 下面还要跟一行「上次运行的游戏名」，
    # 那一行是 UCS-2 长名，直接画，不占 STR_*。
    ("LAST_RUN",    "运行上次游戏", "菜单项 1"),
    ("NO_LAST",     "无上次游戏",  "菜单项 1：还没有启动过任何游戏"),
    ("DELETE_ROM",  "删除当前文件", "菜单项 2：光标在 ROM 上"),
    ("CANT_DEL",    "目录不能删",  "菜单项 2：光标在文件夹上（文件夹不删）"),
    ("DELETE_DONE", "已删除",      "删完在底部提示一下"),
    ("BACK",        "返回",        "菜单底部 B 键提示"),
]

TILE = 8               # 8x8 一个 tile
THRESH = 128           # 像素字体只有 0/255，阈值取中间


def collect_glyphs():
    """CJK 字符去重（保持出现顺序）。ASCII 由 ascii_font 单独渲染。"""
    seen = []
    for _ident, text, _note in UI_STRINGS:
        for ch in text:
            cp = ord(ch)
            if ((0x3400 <= cp <= 0x9FFF) or (0x3000 <= cp <= 0x303F)) and ch not in seen:
                seen.append(ch)
    return seen


def render_tile(font, ch):
    """渲染成 8x8 1bpp，返回 8 字节。

    像素字体**不要**做「裁剪 + 缩放 + 居中」那套（那是给非像素字体缩到小尺寸
    用的，会把像素网格打散）。直接默认 anchor 画在 (0,0)：fusion-pixel 8px 的
    ascent 是 7，字形正好落在第 0-7 行。
    """
    img = Image.new("L", (TILE, TILE), 0)
    ImageDraw.Draw(img).text((0, 0), ch, font=font, fill=255)
    out = bytearray()
    for y in range(TILE):
        b = 0
        for x in range(TILE):
            if img.getpixel((x, y)) >= THRESH:
                b |= 0x80 >> x
        out.append(b)
    return bytes(out)


def check_overflow(font, chars, label):
    """确认字形没有画出 8x8 之外（换字体版本时要能立刻发现）。"""
    bad = []
    for ch in chars:
        big = Image.new("L", (32, 32), 0)
        ImageDraw.Draw(big).text((0, 0), ch, font=font, fill=255)
        bb = big.getbbox()
        if bb and (bb[2] > TILE or bb[3] > TILE or bb[0] < 0 or bb[1] < 0):
            bad.append((ch, bb))
    if bad:
        print("  [警告] %s 有 %d 个字形超出 8x8：%s" % (label, len(bad), bad[:6]))
    return bad


def check_empty(font, chars, label):
    """全 0 的字形 = 字体缺这个字（豆腐块或空白）。"""
    empty = [c for c in chars if not any(render_tile(font, c))]
    if empty:
        print("  [警告] %s 有 %d 个字全空（可能缺字）：%s"
              % (label, len(empty), "".join(empty)))
    return empty


def hexrow(d):
    return ",".join("0x%02X" % b for b in d)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", default=DEFAULT_FONT)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.font):
        print("找不到字体: %s" % args.font, file=sys.stderr)
        print("", file=sys.stderr)
        print("字库只在改动界面文案后需要重新生成，正常构建用的是仓库里的", file=sys.stderr)
        print("src/font_data.h。要重新生成，去", file=sys.stderr)
        print("  https://github.com/TakWolf/fusion-pixel-font/releases", file=sys.stderr)
        print("下 8px monospaced 的 ttf 包，把 %s 放到" % FONT_NAME, file=sys.stderr)
        print("  %s" % os.path.normpath(os.path.join(HERE, "..", "vendor")), file=sys.stderr)
        print("（或设 EZJR_FONT / 用 --font 指定路径）。", file=sys.stderr)
        sys.exit(2)

    font = ImageFont.truetype(args.font, TILE)
    glyphs = collect_glyphs()
    print("字体: %s" % args.font)
    print("  face: %s" % " ".join(font.getname()))
    print("  汉字 %d 个：%s" % (len(glyphs), "".join(glyphs)))

    bad = []
    bad += check_overflow(font, [s[1] for s in UI_SYMBOLS], "符号")
    bad += check_overflow(font, [chr(c) for c in range(0x20, 0x7F)], "ASCII")
    bad += check_overflow(font, glyphs, "汉字")
    bad += check_empty(font, [s[1] for s in UI_SYMBOLS], "符号")
    bad += check_empty(font, glyphs, "汉字")

    if len(glyphs) > 149:
        print("  汉字太多：%d 个，tile 池只有 149 格（$0C-$1F + $7F-$FF）"
              % len(glyphs), file=sys.stderr)
        sys.exit(3)

    # ---- font_data.h ----
    sym_bytes = b"".join(render_tile(font, s[1]) for s in UI_SYMBOLS)
    total = len(glyphs) * 8 + len(UI_SYMBOLS) * 8 + 95 * 8
    print("  ROM 占用: 汉字 %d B + 符号 %d B + ASCII %d B = %d B"
          % (len(glyphs) * 8, len(UI_SYMBOLS) * 8, 95 * 8, total))

    L = []
    L.append("/* 自动生成，请勿手改；由 tools/gen_font.py 生成。 */")
    L.append("/* 字形来自 Fusion Pixel Font 8px（OFL-1.1），见 NOTICE.md。 */")
    L.append("#ifndef FONT_DATA_H")
    L.append("#define FONT_DATA_H")
    L.append("")
    L.append("/* tilemap 里的 tile 编号约定：")
    L.append(" *   $00       空白（清屏/擦除用）")
    L.append(" *   $01-$0B  界面符号，见 cjk_symbols[]，C 里用 \"\\x01\" 引用")
    L.append(" *   $0C-$1F  汉字动态池 A 段（20 格）")
    L.append(" *   $20-$7E  ASCII，编号 = 字符码（大小写都有，不强制转大写）")
    L.append(" *   $7F-$FF  汉字动态池 B 段（129 格）")
    L.append(" * ASCII 能和字符码一一对应，是因为 $20-$7E 恰好 95 个、且紧接在符号区")
    L.append(" * 之后 —— print_at() 不用查表，直接把字符码当 tile 号。")
    L.append(" * 汉字池一共 20+129 = 149 格：中文文件名一屏可能有上百个不同汉字（13 行")
    L.append(" * × 8 字），池小一点就会在翻页时反复作废重传。*/")
    L.append("#define SYM_FIRST      0x01")
    L.append("#define SYM_COUNT      %d" % len(UI_SYMBOLS))
    L.append("#define ASCII_FIRST    0x20")
    L.append("#define ASCII_COUNT    (0x7F - 0x20)")
    L.append("#define CJK_POOL_A     0x0C")
    L.append("#define CJK_POOL_A_N   20")
    L.append("#define CJK_POOL_B     0x7F")
    L.append("#define CJK_POOL_N     149")
    L.append("#define CJK_COUNT      %d" % len(glyphs))
    L.append("")
    L.append("/* 每个字形 8 字节 1bpp（bit7 = 最左像素）。GB 的 tile 是 2bpp 16 字节，")
    L.append(" * video.c 上传时把 low/high 两半写成同一个字节，像素值就是颜色 3 —— ")
    L.append(" * 这样 ROM 只占一半，上传也就一个循环。*/")
    L.append("")
    L.append("static const unsigned char cjk_symbols[SYM_COUNT][8] = {")
    for i, (ident, ch, note) in enumerate(UI_SYMBOLS):
        L.append("    {%s},  /* 0x%02X %s %s */"
                 % (hexrow(render_tile(font, ch)), i + 1, ident, note))
    L.append("};")
    L.append("")
    L.append("static const unsigned char ascii_font[ASCII_COUNT][8] = {")
    for c in range(0x20, 0x7F):
        ch = chr(c)
        show = {"\\": "backslash", "'": "quote", '"': "dquote"}.get(ch, ch)
        L.append("    {%s},  /* 0x%02X '%s' */" % (hexrow(render_tile(font, ch)), c, show))
    L.append("};")
    L.append("")
    L.append("/* 汉字：索引 = glyph id - 1，每个 8 字节 */")
    L.append("static const unsigned char cjk_font[CJK_COUNT][8] = {")
    for i, ch in enumerate(glyphs):
        L.append("    {%s},  /* %2d %s */" % (hexrow(render_tile(font, ch)), i + 1, ch))
    L.append("};")
    L.append("")
    L.append("/* 每个界面汉字的 Unicode 码点。渲染时按码点做缓存键 —— 这样界面汉字")
    L.append(" * 和卡上中文文件名里的同一个字共用 VRAM 里一个 tile，不会各占一份。*/")
    L.append("static const uint16_t cjk_cp[CJK_COUNT] = {")
    for i, ch in enumerate(glyphs):
        L.append("    0x%04X,  /* %2d %s */" % (ord(ch), i + 1, ch))
    L.append("};")
    L.append("")
    L.append("#endif /* FONT_DATA_H */")
    header = "\n".join(L) + "\n"

    # ---- strings.h ----
    S = []
    S.append("/* 自动生成，请勿手改；由 tools/gen_font.py 生成。 */")
    S.append("/* 中文字符串 = glyph id 序列，0 结尾。文案源见 tools/gen_font.py 的 UI_STRINGS。 */")
    S.append("#ifndef STRINGS_H")
    S.append("#define STRINGS_H")
    S.append("")
    S.append("#include <stdint.h>")
    S.append("")
    S.append("/* 符号的 tile 编号（= 字符码，可以直接在 C 串里写 \"\\x01\"）*/")
    for i, (ident, ch, note) in enumerate(UI_SYMBOLS):
        S.append("#define SYM_%s  0x%02X" % (ident, i + 1))
    S.append("")
    S.append("/* 单字 id（供拼装，顺序即 cjk_font 下标+1）*/")
    for i, ch in enumerate(glyphs):
        S.append("#define CJK_%02d  %d   /* %s */" % (i + 1, i + 1, ch))
    S.append("")
    S.append("/* 完整文案。数组放 ROM(const)，元素 1..CJK_COUNT。0 = 串结束。 */")
    id_of = {ch: i + 1 for i, ch in enumerate(glyphs)}
    for ident, text, note in UI_STRINGS:
        seq = [id_of.get(ch, 0) for ch in text]
        S.append("static const uint8_t STR_%s[] = {%s, 0};  /* %s — %s */"
                 % (ident, ",".join(str(v) for v in seq), text, note))
    S.append("")
    S.append("#endif /* STRINGS_H */")
    ids_h = "\n".join(S) + "\n"

    # ---- docs/cjk-glyphs.md ----
    D = ["# 收录汉字清单", "",
         "由 `tools/gen_font.py` 生成。字体：[Fusion Pixel Font]"
         "(https://github.com/TakWolf/fusion-pixel-font) 8px 等宽简体中文版"
         "（OFL-1.1）。", "",
         "界面文案固定在 `tools/gen_font.py` 的 `UI_STRINGS`，改完重跑脚本即自动生成",
         "`src/strings.h` 的 `STR_*` 与 `src/font_data.h` 的点阵。", "",
         "| id | 字 | id | 字 | id | 字 | id | 字 |",
         "|---|---|---|---|---|---|---|---|"]
    rows = (len(glyphs) + 3) // 4
    for r in range(rows):
        cells = []
        for k in range(4):
            i = r + k * rows
            if i < len(glyphs):
                cells += [str(i + 1), glyphs[i]]
            else:
                cells += ["", ""]
        D.append("| " + " | ".join(cells) + " |")
    D += ["",
          "合计 **%d** 个汉字，%d 字节 1bpp 点阵数据（8 字节/字）。"
          % (len(glyphs), len(glyphs) * 8),
          "",
          "> 这是**界面提示用**的精简字库，只覆盖本内核界面里出现的汉字，",
          "> **不含**游戏文件名里的任意汉字，也不是 GB2312 全字库。",
          "> 文件名按 FAT 的 8.3 短名显示（ASCII），与官方内核处理 LFN 的方式不同。",
          "> 全字库：8x8 1bpp 的 GB2312 一级字也要 ~30 KB，放不进 32 KB 卡带的",
          "> 剩余空间，见 docs/04-boundaries.md。", ""]
    doc_txt = "\n".join(D) + "\n"

    print("\n  font_data.h 预计 %d 字节" % len(header))
    print("  strings.h   预计 %d 字节" % len(ids_h))

    if args.check:
        print("\n[--check] 仅校验，未写文件。")
        return 0 if not bad else 1

    open(OUT_H, "w", encoding="utf-8").write(header)
    open(OUT_STR, "w", encoding="utf-8").write(ids_h)
    open(OUT_DOC, "w", encoding="utf-8").write(doc_txt)
    old = os.path.join(ROOT, "src", "cjk_ids.h")
    if os.path.exists(old):
        os.remove(old)
    print("\n已写出:\n  %s\n  %s\n  %s" % (OUT_H, OUT_STR, OUT_DOC))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
