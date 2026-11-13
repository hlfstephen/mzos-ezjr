#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_cjk_fnt.py — 生成卡上用的汉字字库文件 EZJR.FNT

为什么要有这么一个文件：中文文件名存在 VFAT 长文件名项里（UTF-16LE），
要画出来就得有对应汉字的点阵。ROM 只剩十几 KB，装不下 GB2312 那 3755 个
常用字（8x8 也要 30 KB），所以把字模放到 SD 卡上，内核按需读。

文件格式（刻意做到最简，内核只要一次减法和一次乘法就能定位）：

    裸字模数组，没有文件头。第 i 个 8 字节 = U+4E00+i 的字形，
    每字节一行，bit7 = 最左像素。

    offset(cp) = (cp - 0x4E00) * 8

大小固定 20992 * 8 = 167936 字节（U+4E00–U+9FFF 共 20992 个码位）。
内核会核对文件大小，对不上就不用它、退回显示 8.3 短名。

字形来自 Fusion Pixel Font 8px（OFL-1.1），和界面字库同一套字体，
所以中文文件名和界面汉字的笔画粗细完全一致。

用法：
  python3 tools/gen_cjk_fnt.py                    # -> EZJR.FNT
  python3 tools/gen_cjk_fnt.py --font=PATH -o OUT
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FONT_NAME = "fusion-pixel-8px-monospaced-zh_hans.ttf"
DEFAULT_FONT = os.environ.get("EZJR_FONT") or os.path.join(HERE, "..", "vendor", FONT_NAME)
DEFAULT_OUT = os.path.join(ROOT, "EZJR.FNT")

FIRST = 0x4E00              # 一
LAST = 0x9FFF              # 龿（CJK 统一表意文字基本区末尾）
COUNT = LAST - FIRST + 1
TILE = 8
THRESH = 128


def render_tile(font, ch):
    """渲成 8x8 1bpp。像素字体直接画在 (0,0)，不要裁剪缩放。"""
    img = Image.new("L", (TILE, TILE), 0)
    ImageDraw.Draw(img).text((0, 0), ch, font=font, fill=255)
    px = img.load()
    out = bytearray(TILE)
    any_px = False
    for y in range(TILE):
        b = 0
        for x in range(TILE):
            if px[x, y] >= THRESH:
                b |= 0x80 >> x
                any_px = True
        out[y] = b
    return bytes(out), any_px


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", default=DEFAULT_FONT)
    ap.add_argument("-o", "--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    if not os.path.exists(args.font):
        print("找不到字体: %s" % args.font, file=sys.stderr)
        print("见 tools/gen_font.py 顶部的说明（字体放 vendor/ 或设 EZJR_FONT）。",
              file=sys.stderr)
        return 2

    font = ImageFont.truetype(args.font, TILE)
    buf = bytearray(COUNT * 8)
    blank = 0

    for i in range(COUNT):
        bits, any_px = render_tile(font, chr(FIRST + i))
        if not any_px:
            blank += 1
        buf[i * 8:(i + 1) * 8] = bits

    with open(args.out, "wb") as f:
        f.write(buf)

    size = os.path.getsize(args.out)
    print("字体: %s" % args.font)
    print("  码位 U+%04X-U+%04X，%d 个" % (FIRST, LAST, COUNT))
    print("  空白（字体里没有这个字）: %d 个（%.1f%%）"
          % (blank, blank * 100.0 / COUNT))
    print("  写出: %s（%d 字节，应为 %d）" % (args.out, size, COUNT * 8))
    if size != COUNT * 8:
        print("大小不对！", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
