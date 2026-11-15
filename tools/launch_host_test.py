#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
launch_host_test.py — 在主机上验证 fat_build_cmd() 生成的 512 字节加载命令。

为什么单独测：这张表的格式有两处反直觉的地方，真机上一错就是「能进游戏但花屏」
或「第三段起内容错位」，而且模拟器完全测不出来（PyBoy 没有 FPGA 的装载引擎）。

  1) 段表第二项是**累计已加载扇区数**，不是本段长度。前两段两者恰好相等，
     从第三段起才分道扬镳 —— 只测 1~2 段的用例永远发现不了。
  2) 段数上限 61。溢出会踩掉 +$1F0 的文件大小字段。

做法：Python 按规范独立算一份期望值（不复用 C 的实现），gcc 编译 src/fat.c 拿
实际值，逐字节比对 512 字节。覆盖单簇 / 2 段 / 3 段 / 61 段 / 62 段 / 成环。
"""
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SEC = 512
CSIZE = 1
FAT_SZ = 1
RES_SEC = 1
N_FATS = 1
ROOT_ENTRIES = 64
ROOT_SECS = (ROOT_ENTRIES * 32 + SEC - 1) // SEC
TOTAL_SEC = 8192

EOF16 = 0xFFF8


# ---------------------------------------------------------------------------
# 造镜像
# ---------------------------------------------------------------------------
def make_dirent(name8, ext3, attr, cluster, size):
    e = bytearray(32)
    e[0:8] = name8.encode("ascii").ljust(8)[:8]
    e[8:11] = ext3.encode("ascii").ljust(3)[:3]
    struct.pack_into("<B", e, 11, attr)
    struct.pack_into("<H", e, 26, cluster & 0xFFFF)
    struct.pack_into("<I", e, 28, size)
    return e


def build_image(path, chain, size_bytes, label="GAME    GBC", fat_map=None,
                first_sector=None):
    """一个 superfloppy FAT16 卷，根目录只有游戏这一个条目，文件占据 chain。

    fat_map 给出时直接按它写 FAT 项（用于构造成环这种 chain 表达不了的链）。"""
    img = bytearray(TOTAL_SEC * SEC)
    # FAT 大小按链长算：1 MB 的文件有 2048 簇，1 个扇区的 FAT 装不下
    fat_sz = max(FAT_SZ, ((max(chain) + 2) * 2 + SEC - 1) // SEC)
    fat_lba = RES_SEC
    root_lba = RES_SEC + N_FATS * fat_sz
    data_lba = root_lba + ROOT_SECS

    dbr = bytearray(SEC)
    dbr[0:3] = b"\xEB\x3C\x90"
    dbr[3:11] = b"EZJRTEST"
    struct.pack_into("<H", dbr, 0x0B, SEC)
    dbr[0x0D] = CSIZE
    struct.pack_into("<H", dbr, 0x0E, RES_SEC)
    dbr[0x10] = N_FATS
    struct.pack_into("<H", dbr, 0x11, ROOT_ENTRIES)
    struct.pack_into("<H", dbr, 0x13, TOTAL_SEC)
    dbr[0x15] = 0xF8
    struct.pack_into("<H", dbr, 0x16, fat_sz)
    dbr[0x36:0x3E] = b"FAT16   "
    dbr[0x1FE] = 0x55
    dbr[0x1FF] = 0xAA
    img[0:SEC] = dbr

    # FAT：给链上每一簇写下一簇，最后一簇写 EOF
    fat = bytearray(fat_sz * SEC)
    fat[0] = 0xF8
    fat[1] = 0xFF
    if fat_map is not None:
        for c, nxt in fat_map.items():
            struct.pack_into("<H", fat, c * 2, nxt)
    else:
        for i, c in enumerate(chain):
            nxt = chain[i + 1] if i + 1 < len(chain) else EOF16
            struct.pack_into("<H", fat, c * 2, nxt)
    img[fat_lba * SEC:(fat_lba + fat_sz) * SEC] = fat

    # 根目录
    root = bytearray(ROOT_SECS * SEC)
    root[0:32] = make_dirent(label[:8], label[8:11], 0x20, chain[0], size_bytes)
    img[root_lba * SEC:(root_lba + 1) * SEC] = root

    if first_sector is not None:
        at = lba_of(chain[0], data_lba) * SEC
        img[at:at + len(first_sector)] = first_sector

    open(path, "wb").write(img)
    return data_lba


# ---------------------------------------------------------------------------
# 期望值：按规范独立实现（不参考 C 代码）
# ---------------------------------------------------------------------------
def lba_of(cluster, data_base):
    return data_base + (cluster - 2) * CSIZE


def expect_cmd(chain, data_base, size_bytes):
    cmd = bytearray(512)

    def w(off, v):
        struct.pack_into("<I", cmd, off, v)

    off = 4
    w(off, lba_of(chain[0], data_base))
    off += 4

    done = 0
    c = chain[0]
    for n in list(chain[1:]) + [EOF16]:
        done += 1
        if n != c + 1:
            if n >= EOF16:
                break
            w(off, done * CSIZE)          # 累计扇区数，不是本段长度
            off += 4
            w(off, lba_of(n, data_base))
            off += 4
        c = n

    w(off, 0xFFFFFFFF)
    w(0x1F0, size_bytes)
    w(0x1F4, 1)
    w(0x1F8, CSIZE)
    return bytes(cmd)


# ---------------------------------------------------------------------------
# 桩 + 编译
# ---------------------------------------------------------------------------
STUB = r'''
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "fat.h"

#define IMG_BYTES IMG_SIZE
static uint8_t g_img[IMG_BYTES];

void sd_read(uint32_t lba, uint8_t *dst) {
    if ((size_t)lba * 512 + 512 > IMG_BYTES) { memset(dst, 0, 512); return; }
    memcpy(dst, g_img + (size_t)lba * 512, 512);
}
uint8_t sd_write(uint32_t lba, const uint8_t *src){(void)lba;(void)src;return 0;}
void fpga_set(uint16_t r, uint8_t v){(void)r;(void)v;}
void fpga_page(uint8_t p){(void)p;}
uint8_t buttons(void){return 0;}
void wait_release(void){}
uint8_t fw_version(void){return 5;}
void wait_frames(uint8_t n){(void)n;}

int main(void) {
    GameEntry list[4];
    uint8_t e, n, cmd[512];
    FILE *f = fopen("IMG", "rb");
    uint16_t i;
    if (!f || fread(g_img, 1, IMG_BYTES, f) != IMG_BYTES) { printf("IMG_ERR\n"); return 2; }
    fclose(f);
    e = fat_mount();
    if (e) { printf("MOUNT_ERR %u\n", (unsigned)e); return 0; }
    n = fat_scan_games(list, 4);
    if (n < 1) { printf("NO_GAME\n"); return 0; }
    e = fat_build_cmd(list[0].cluster, list[0].size, cmd);
    if (e) { printf("CMD_ERR %u\n", (unsigned)e); return 0; }
    printf("CMD ");
    for (i = 0; i < 512; i++) printf("%02x", cmd[i]);
    printf("\n");
    return 0;
}
'''


def run_case(name, chain, size_bytes, expect_err=None, fat_map=None):
    tmp = tempfile.mkdtemp(prefix="ezjr-launch-")
    img = os.path.join(tmp, "card.img")
    data_base = build_image(img, chain, size_bytes, fat_map=fat_map)

    stub = os.path.join(tmp, "stub.c")
    src = (STUB.replace("IMG_SIZE", str(TOTAL_SEC * SEC))
               .replace('"IMG"', '"' + img + '"'))
    open(stub, "w").write(src)
    exe = os.path.join(tmp, "t")
    r = subprocess.run(["gcc", "-O1", "-I" + os.path.join(ROOT, "src"),
                        stub, os.path.join(ROOT, "src", "fat.c"),
                        os.path.join(ROOT, "src", "fatw.c"), os.path.join(ROOT, "src", "fatx.c"), "-o", exe],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("  gcc 失败:\n" + r.stderr)
        return False
    out = subprocess.run([exe], capture_output=True, text=True).stdout.strip()

    if expect_err is not None:
        ok = out == "CMD_ERR %d" % expect_err
        print(("  OK   " if ok else "  FAIL ") + f"{name:<14} 期望 CMD_ERR {expect_err}，实得 {out}")
        return ok

    if not out.startswith("CMD "):
        print(f"  FAIL {name:<14} 未产出命令：{out}")
        return False
    got = bytes.fromhex(out[4:])
    want = expect_cmd(chain, data_base, size_bytes)
    if got == want:
        print(f"  OK   {name:<14} 512 字节逐字节一致（{len(chain)} 簇，{segments(chain)} 段）")
        return True
    diff = next((i for i in range(512) if got[i] != want[i]), -1)
    print(f"  FAIL {name:<14} 首处差异 +${diff:03X}: 实得 {got[diff]:02x} 期望 {want[diff]:02x}")
    return False


def segments(chain):
    seg = 1
    for i in range(len(chain) - 1):
        if chain[i + 1] != chain[i] + 1:
            seg += 1
    return seg


def main():
    if not any(os.path.exists(p) for p in ["/usr/bin/gcc", "/usr/bin/cc"]):
        print("  跳过：主机无 gcc")
        return 0

    print("  加载命令表（fat_build_cmd → 512 字节）")
    fails = 0

    # 1 簇：最小文件
    if not run_case("single", [2], 32 * 1024):
        fails += 1
    # 2 段：前两段累计值 == 段长，这个用例本身区分不出两者的差别
    if not run_case("two_seg", [2, 3, 10, 11], 64 * 1024):
        fails += 1
    # 3 段：从第三段起，累计值 != 段长（4 簇后 done=4，而本段只有 1 簇）
    if not run_case("three_seg", [2, 3, 4, 8, 9, 15], 128 * 1024):
        fails += 1
    # 连着 5 个单簇段，累计值 1,2,3,4,5 vs 段长全 1 —— 最能暴露「写成段长」
    if not run_case("five_single", [2, 5, 9, 14, 20, 30], 256 * 1024):
        fails += 1
    # 恰好 61 段（上限）
    chain61 = []
    c = 2
    for _ in range(61):
        chain61.append(c)
        c += 2
    if not run_case("max_61seg", chain61, 1024 * 1024):
        fails += 1
    # 62 段：必须干脆拒绝，而不是溢出踩掉 +$1F0
    chain62 = []
    c = 2
    for _ in range(62):
        chain62.append(c)
        c += 2
    if not run_case("over_62seg", chain62, 1024 * 1024, expect_err=5):
        fails += 1
    # 成环：5 指回 3。链本身表达不出环（同一簇会写两次 FAT 覆盖掉），
    # 所以直接给 FAT 项：2→3→4→5→3。
    # 报 FRAGMENTED 而非 BAD_CHAIN 是对的：绕环会不断产生新段（每 2 簇一段），
    # 61 段的表先装不下。真机上两者都显示「无法启动」，区别只在文档里。
    if not run_case("loop_as_frag", [2], 128 * 1024, expect_err=5,
                    fat_map={2: 3, 3: 4, 4: 5, 5: 3}):
        fails += 1
    # 真正触发 BAD_CHAIN：下一簇是 0（空闲簇，非法）
    if not run_case("bad_next", [2], 32 * 1024, expect_err=6,
                    fat_map={2: 0}):
        fails += 1

    print("\n  启动寄存器序列（game_launch → fpga_set 顺序与取值）")
    # 《宝可梦 水晶》的头：MBC3+TIMER+RAM+BATTERY，1MB，128KB 存档
    if not run_game_case("crystal_mbc3", make_rom_header(0x10, 0x05, 0x04, b"CRYSTAL"),
                         1024 * 1024):
        fails += 1
    # 纯 ROM 32KB（俄罗斯方块那类）：MBC 码 0，无 timer
    if not run_game_case("tetris_romonly", make_rom_header(0x00, 0x00, 0x00, b"TETRIS"),
                         32 * 1024):
        fails += 1
    # MBC5（大容量 GBC 游戏）：MBC 码 4
    if not run_game_case("mbc5", make_rom_header(0x1B, 0x05, 0x03, b"GBCGAME"),
                         1024 * 1024):
        fails += 1

    print("\n  handoff 段静态检查（能否安全搬到 WRAM 执行）")
    if not stat_launch_section():
        fails += 1

    return 1 if fails else 0




# ===========================================================================
# 第二部分：启动寄存器序列（game.c）
#
# 10 个寄存器的写顺序和取值是「真机上一错就崩、模拟器完全看不出」的地方，
# 所以专门做一层 golden 断言：桩记录每一次 fpga_set/fpga_page，与按规范
# 独立算出的序列逐项比对。
# ===========================================================================
GAME_STUB = r'''
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "fat.h"
#include "game.h"
#include "hw.h"

#define IMG_BYTES IMG_SIZE
static uint8_t g_img[IMG_BYTES];

uint8_t g_test_ly = 144;        /* 立刻满足「等到 VBlank」 */
uint8_t g_test_lcdc = 0x91;     /* 初始 LCD 开着 */

void sd_read(uint32_t lba, uint8_t *dst) {
    if ((size_t)lba * 512 + 512 > IMG_BYTES) { memset(dst, 0, 512); return; }
    memcpy(dst, g_img + (size_t)lba * 512, 512);
}
uint8_t sd_write(uint32_t lba, const uint8_t *src){(void)lba;(void)src;return 0;}
void fpga_set(uint16_t r, uint8_t v) { printf("SET %04X %02X\n", r, v); }
void fpga_page(uint8_t p) { fpga_set(0x7FC0, p); }
/* 页 6 的时钟窗口主机上没有；RTC base 本身由 tools/rtc_host_test.py 单测 */
void rtc_base_write(void) { }
uint8_t fw_version(void) { uint8_t v = 5; fpga_page(4); fpga_page(0); return v; }
uint8_t buttons(void) { return 0; }
void wait_release(void) {}
void wait_frames(uint8_t n) { (void)n; }

void host_cmd_window(const uint8_t *cmd) {
    uint16_t i;
    printf("LCDC %02X\n", g_test_lcdc);
    printf("CMD ");
    for (i = 0; i < 512; i++) printf("%02x", cmd[i]);
    printf("\n");
}

int main(void) {
    GameEntry list[4];
    uint8_t e, n;
    FILE *f = fopen("IMG", "rb");
    if (!f || fread(g_img, 1, IMG_BYTES, f) != IMG_BYTES) { printf("IMG_ERR\n"); return 2; }
    fclose(f);
    e = fat_mount();
    if (e) { printf("MOUNT_ERR %u\n", (unsigned)e); return 0; }
    n = fat_scan_games(list, 4);
    if (n < 1) { printf("NO_GAME\n"); return 0; }
    e = game_launch(&list[0]);
    printf("RET %u\n", (unsigned)e);
    return 0;
}
'''


def mbc_of(t):
    if t in (0x00, 0x08, 0x09):
        return 0
    if 0x01 <= t <= 0x03 or t in (0xEA, 0xFF):
        return 1
    if t in (0x05, 0x06):
        return 2
    if 0x0F <= t <= 0x13 or t == 0xFC:
        return 3
    if 0x19 <= t <= 0x1E:
        return 4
    return 6


def rom_mask_of(code, size):
    mask = 0
    if 1 <= code <= 8:
        mask = (2 << code) - 1
    elif code == 0x52:
        mask = 0x47
    elif code == 0x53:
        mask = 0x4F
    elif code == 0x54:
        mask = 0x5F
    banks = max(1, size >> 14)
    if mask < banks - 1:
        m = 1
        while m <= banks and m < 0x200:
            m <<= 1
        mask = min(m - 1, 0x1FF)
    return mask


def ram_mask_of(code, mbc):
    if mbc == 2 or code <= 2:
        return 0x00
    if code == 4:
        return 0x0F
    if code == 5:
        return 0x07
    return 0x03


def header_sum(hdr):
    x = 0
    for b in hdr[0x134:0x14D]:
        x = (x - b - 1) & 0xFF
    return x


def make_rom_header(cart_type, rom_code, ram_code, title=b"TESTGAME"):
    h = bytearray(512)
    h[0x100:0x104] = b"\x00\xC3\x50\x01"
    h[0x134:0x134 + len(title)] = title
    h[0x147] = cart_type
    h[0x148] = rom_code
    h[0x149] = ram_code
    h[0x14B] = 0x33                       # new licensee
    h[0x14C] = 0x00                       # version
    # 头校验和：先填 0，算出真实值再回填
    h[0x14D] = header_sum(h)
    return h


def run_game_case(name, hdr, size_bytes):
    tmp = tempfile.mkdtemp(prefix="ezjr-game-")
    img = os.path.join(tmp, "card.img")
    nclu = (size_bytes + SEC - 1) // SEC
    chain = list(range(2, 2 + nclu))
    data_base = build_image(img, chain, size_bytes, first_sector=hdr)

    stub = os.path.join(tmp, "stub.c")
    src = (GAME_STUB.replace("IMG_SIZE", str(TOTAL_SEC * SEC))
                     .replace('"IMG"', '"' + img + '"'))
    open(stub, "w").write(src)
    exe = os.path.join(tmp, "g")
    r = subprocess.run(["gcc", "-O1", "-DEZJR_HOST_TEST",
                        "-I" + os.path.join(ROOT, "src"),
                        stub, os.path.join(ROOT, "src", "game.c"),
                        os.path.join(ROOT, "src", "fat.c"),
                        os.path.join(ROOT, "src", "fatw.c"), os.path.join(ROOT, "src", "fatx.c"), "-o", exe],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("  gcc 失败:\n" + r.stderr)
        return False
    out = subprocess.run([exe], capture_output=True, text=True).stdout
    if "CMD " not in out:
        print(f"  FAIL {name:<14} 未产出命令：{out.strip()[:120]}")
        return False

    got_seq = []
    for line in out.splitlines():
        if line.startswith("SET "):
            _, reg, val = line.split()
            got_seq.append((int(reg, 16), int(val, 16)))
    lcdc = next(int(l.split()[1], 16) for l in out.splitlines() if l.startswith("LCDC "))
    cmd = bytes.fromhex(out.split("CMD ", 1)[1].splitlines()[0])

    t = hdr[0x147]
    mbc = mbc_of(t)
    want_seq = [
        (0x7FC0, 0x04), (0x7FC0, 0x00),                 # fw_version 读页 4
        (0x7FC0, 0x02),                                  # 命令窗口要求页 2
        (0x7F37, mbc | (0x80 if t in (0x0F, 0x10) else 0)),
        (0x7FD3, 0x00), (0x7FD4, 0x11),
        (0x7FC4, ram_mask_of(hdr[0x149], mbc)),
        (0x7FC1, rom_mask_of(hdr[0x148], size_bytes) & 0xFF),
        (0x7FC2, (rom_mask_of(hdr[0x148], size_bytes) >> 8) & 0xFF),
        (0x7FC3, header_sum(hdr)),
        (0x7F36, 0x01),
    ]

    ok = True
    if got_seq != want_seq:
        i = next((k for k in range(min(len(got_seq), len(want_seq)))
                  if got_seq[k] != want_seq[k]), min(len(got_seq), len(want_seq)))
        g = f"${got_seq[i][0]:04X}=${got_seq[i][1]:02X}" if i < len(got_seq) else "(缺)"
        w = f"${want_seq[i][0]:04X}=${want_seq[i][1]:02X}" if i < len(want_seq) else "(多)"
        print(f"  FAIL {name:<14} 序列第 {i} 步：实得 {g} 期望 {w}")
        ok = False
    if lcdc != 0x00:
        print(f"  FAIL {name:<14} 关屏失败，LCDC=${lcdc:02X}")
        ok = False
    want_cmd = expect_cmd(chain, data_base, size_bytes)
    if cmd != want_cmd:
        i = next((k for k in range(512) if cmd[k] != want_cmd[k]), -1)
        print(f"  FAIL {name:<14} 命令表 +${i:03X}: 实得 {cmd[i]:02x} 期望 {want_cmd[i]:02x}")
        ok = False
    if ok:
        print(f"  OK   {name:<14} 寄存器 {len(got_seq)} 步、LCDC=0、命令表尾字段一致")
    return ok


def stat_launch_section():
    """静态检查 handoff 段：不许有 call/jp 绝对地址，LD HL 只能指向 $7Fxx、
    $A000 或游戏头部 $0104。这是「搬到 WRAM 执行」的前提，错了真机会随机死机。

    检查改读汇编列表（build/obj/launch.lst），不再对机器码字节流做模式匹配 ——
    字节流扫描会把 jr 的偏移量和立即数当成指令，段一变长就误报（曾经把
    `jr nz` 的偏移 0x23 和后面的 `ld a,(hl)` 拼成 `LD HL,$7E23`）。

    $0104 是特意放行的：装载完成后这段要读游戏的 Nintendo logo 判成功与否，
    那时 cart 空间里已经是装载结果，$0104 是 cart 的绝对地址，不需要重定位。
    只放行这一个地址，是怕有人顺手把 LD HL 指回内核自己的 ROM。
    失败时切 LCD 开关用到的 $FF40/$FF47 走的是 `ld (nn),a`，不经过 LD HL，无需放行。
    """
    import re
    lst_path = os.path.join(ROOT, "build", "obj", "launch.lst")
    map_path = os.path.join(ROOT, "build", "obj", "ezgb.map")
    if not os.path.exists(lst_path):
        print("  跳过 launch 段静态检查（未构建）")
        return True
    n_ins = 0
    bad = []
    for line in open(lst_path, encoding="utf-8", errors="replace"):
        if not re.match(r"^\s+[0-9A-F]{6}\s", line):
            continue                                   # 注释 / 空行
        n_ins += 1
        low = line.lower()
        if re.search(r"\bcall\b", low):
            bad.append("含 call：" + low.split(")", 1)[-1].strip())
        if re.search(r"\bjp\b", low):
            bad.append("含 jp：" + low.split(")", 1)[-1].strip())
        m = re.search(r"ld\s+hl\s*,\s*#?\$?0x([0-9a-f]+)", low)
        if m:
            tgt = int(m.group(1), 16)
            if not (0x7F00 <= tgt <= 0x7FFF or tgt in (0xA000, 0x0104)):
                bad.append("LD HL,$%04X" % tgt)

    size = "?"
    if os.path.exists(map_path):
        m = open(map_path).read()
        a = re.search(r"([0-9A-F]{8})\s+_launch_start", m)
        b = re.search(r"([0-9A-F]{8})\s+_launch_end", m)
        if a and b:
            size = "%d 字节" % (int(b.group(1), 16) - int(a.group(1), 16))

    if bad:
        print("  FAIL launch 段（%s，%d 条指令）：" % (size, n_ins) + "；".join(bad))
        return False
    print("  OK   launch 段（%s，%d 条指令）无 call/jp、LD HL 全在允许范围" % (size, n_ins))
    return True


if __name__ == "__main__":
    sys.exit(main())
