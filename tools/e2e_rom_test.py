#!/usr/bin/env python3
"""e2e_rom_test.py — 用真实 ROM 文件做端到端验证

比 launch_host_test.py 更狠的一层：把**用户卡上那个真实的 2 MB ROM** 放进
构造的 FAT 卷里，跑真正的 src/fat.c + src/game.c，拿到 512 字节加载命令，
再按命令表把文件"重组"一遍，与源文件逐字节比对。

重组规则就是 FPGA 的规则（docs/fpga-stage1.md）：
  extents 是 {u32 LBA, u32 end}，end 是**从文件开头算起的累计扇区数**。
  第 k 段读 end_k - end_{k-1} 个扇区，从 LBA_k 开始；最后一段 end 是
  $FFFFFFFF，读到文件大小为止。

只要重组结果和源文件不等，就说明列表→簇链→LBA→命令表这条链上有一环错，
而且错在第几个字节会直接指出来。

用法：
    python3 tools/e2e_rom_test.py "/path/to/game.gbc"
    python3 tools/e2e_rom_test.py "/path/to/game.gbc" --csize 4
"""
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEC = 512

FAT_STUB = r'''
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "fat.h"
#include "hw.h"

#define IMG_BYTES IMG_SIZE
static uint8_t g_img[IMG_BYTES];

uint8_t g_test_ly = 144;        /* 立刻满足「等到 VBlank」 */
uint8_t g_test_lcdc = 0x91;

void sd_read(uint32_t lba, uint8_t *dst) {
    if ((size_t)lba * 512 + 512 > IMG_BYTES) { memset(dst, 0, 512); return; }
    memcpy(dst, g_img + (size_t)lba * 512, 512);
}
uint8_t sd_write(uint32_t lba, const uint8_t *src){(void)lba;(void)src;return 0;}
void fpga_set(uint16_t r, uint8_t v) { (void)r; (void)v; }
void fpga_page(uint8_t p) { (void)p; }
/* 主机上没有页 6 的时钟窗口；RTC base 的正确性由 tools/rtc_host_test.py 单独验 */
void rtc_base_write(void) { }
uint8_t fw_version(void) { return 5; }
uint8_t buttons(void) { return 0; }
void wait_release(void) {}
void wait_frames(uint8_t n) { (void)n; }
void host_cmd_window(const uint8_t *cmd) {
    uint16_t i;
    printf("CMD ");
    for (i = 0; i < 512; i++) printf("%02x", cmd[i]);
    printf("\n");
}

int main(void) {
    GameEntry list[16];
    uint8_t e, n, k;
    uint16_t i;                      /* 循环到 512，uint8_t 会永远小于 512 */
    FILE *f = fopen("IMG", "rb");
    if (!f || fread(g_img, 1, IMG_BYTES, f) != IMG_BYTES) { printf("IMG_ERR\n"); return 2; }
    fclose(f);
    e = fat_mount();
    printf("MOUNT %u\n", (unsigned)e);
    if (e) return 0;
    n = fat_scan_games(list, 16);
    printf("COUNT %u\n", (unsigned)n);
    for (k = 0; k < n; k++)
        printf("ENTRY %s %lu %lu\n", list[k].name,
               (unsigned long)list[k].cluster, (unsigned long)list[k].size);
    /* 只对第一个 .GBC 构建命令 */
    for (k = 0; k < n; k++)
        if (list[k].name[strlen(list[k].name) - 1] == 'C') break;
    if (k >= n) k = 0;
    if (n) {
        static uint8_t cmdbuf[512];
        e = fat_build_cmd(list[k].cluster, list[k].size, cmdbuf);
        printf("BUILD %u\n", (unsigned)e);
        if (!e) {
            printf("CMD ");
            for (i = 0; i < 512; i++) printf("%02x", cmdbuf[i]);
            printf("\n");
        }
    }
    printf("DONE\n");
    return 0;
}
'''


def build_volume(rom, out_path, csize=8, frag=None, fat32=False):
    """构造一个 superfloppy FAT16 卷，根目录里放一个中文长文件名的 ROM。

    frag 为 None 时文件连续；给一个列表时按列表给出的簇号顺序放置（模拟碎片）。
    """
    nclu_need = (len(rom) + csize * SEC - 1) // (csize * SEC)
    if frag is None:
        chain = list(range(2, 2 + nclu_need))
    else:
        chain = frag[:nclu_need]

    max_clu = max(chain) + 2
    entsz = 4 if fat32 else 2
    # 簇数必须过 4085（FAT16）或 65525（FAT32），否则被当成 FAT12
    min_clu = 65525 if fat32 else 4200
    if max_clu < min_clu:
        max_clu = min_clu
    fat_sz = max(1, (max_clu * entsz + SEC - 1) // SEC)
    root_secs = 0 if fat32 else 32
    res = 32 if fat32 else 1
    root_lba = res + 2 * fat_sz
    data_lba = root_lba + root_secs
    total_sec = data_lba + max_clu * csize + 8

    img = bytearray(total_sec * SEC)

    # BPB
    dbr = bytearray(SEC)
    dbr[0:3] = b"\xEB\x3C\x90"
    dbr[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", dbr, 0x0B, SEC)
    dbr[0x0D] = csize
    struct.pack_into("<H", dbr, 0x0E, res)
    dbr[0x10] = 2
    struct.pack_into("<H", dbr, 0x11, 512)
    dbr[0x15] = 0xF8
    struct.pack_into("<H", dbr, 0x18, 32 if not fat32 else 0)
    struct.pack_into("<H", dbr, 0x1A, 2)
    dbr[0x24] = 0x80
    dbr[0x26] = 0x29
    struct.pack_into("<I", dbr, 0x28, 0x12345678)
    if fat32:
        struct.pack_into("<H", dbr, 0x16, 0)
        struct.pack_into("<I", dbr, 0x20, total_sec)
        struct.pack_into("<I", dbr, 0x24, fat_sz)
        struct.pack_into("<I", dbr, 0x2C, 2)          # root cluster
        struct.pack_into("<H", dbr, 0x42, 0)
        struct.pack_into("<H", dbr, 0x48, 1)
        dbr[0x52:0x5A] = b"FAT32   "
    else:
        struct.pack_into("<H", dbr, 0x13, total_sec)
        struct.pack_into("<H", dbr, 0x16, fat_sz)
        struct.pack_into("<H", dbr, 0x20, total_sec)
        dbr[0x36:0x3E] = b"FAT16   "
    dbr[510] = 0x55
    dbr[511] = 0xAA
    img[0:SEC] = dbr

    # FAT
    fat = bytearray(fat_sz * SEC)
    if fat32:
        fat[0:4] = b"\xF8\xFF\xFF\x0F"
        fat[4:8] = b"\xFF\xFF\xFF\x0F"
    else:
        fat[0] = 0xF8
        fat[1] = 0xFF
    for i, c in enumerate(chain):
        if fat32:
            nxt = chain[i + 1] if i + 1 < len(chain) else 0x0FFFFFFF
            if c * 4 + 4 <= len(fat):
                struct.pack_into("<I", fat, c * 4, nxt)
        else:
            nxt = chain[i + 1] if i + 1 < len(chain) else 0xFFFF
            if c * 2 + 2 <= len(fat):
                struct.pack_into("<H", fat, c * 2, nxt)
    for k in range(2):
        at = (res + k * fat_sz) * SEC
        img[at:at + fat_sz * SEC] = fat

    # 根目录：LFN + 8.3 短名
    root = bytearray(root_secs * SEC)
    longname = "K 口袋妖怪 水晶 金手指v3+全道具251+NPC加强版2.0.gbc"
    entries = lfn_entries(longname, "K__~1   GBC")
    off = 0
    for e in entries:
        root[off:off + 32] = e
        off += 32
    root[off:off + 32] = short_entry("K__~1   GBC", chain[0], len(rom))
    if fat32:
        # 根目录就是 cluster 2，文件数据从 cluster 3 起
        at = data_lba * SEC
    else:
        at = root_lba * SEC
    img[at:at + root_secs * SEC] = root

    # 数据：按簇链顺序把文件铺进去
    pos = 0
    for c in chain:
        lba = data_lba + (c - 2) * csize
        chunk = rom[pos:pos + csize * SEC]
        img[lba * SEC:lba * SEC + len(chunk)] = chunk
        pos += csize * SEC

    open(out_path, "wb").write(img)
    return data_lba, chain


def short_entry(name11, cluster, size, attr=0x20):
    e = bytearray(32)
    e[0:11] = name11.encode("ascii")
    e[11] = attr
    struct.pack_into("<H", e, 26, cluster & 0xFFFF)
    struct.pack_into("<H", e, 20, (cluster >> 16) & 0xFFFF)
    struct.pack_into("<I", e, 28, size)
    return e


def lfn_entries(longname, shortname):
    """生成 LFN 序列（倒序）。名字用 UTF-16LE，按 13 字符一段。"""
    chars = list(longname) + ["\x00"]
    while len(chars) % 13:
        chars.append("\uffff")
    chunks = [chars[i:i + 13] for i in range(0, len(chars), 13)]
    out = []
    for idx in range(len(chunks) - 1, -1, -1):
        c = chunks[idx]
        e = bytearray(32)
        seq = idx + 1
        e[0] = seq | (0x40 if idx == len(chunks) - 1 else 0)
        e[11] = 0x0F
        e[13] = 0
        e[12] = 0
        e[26] = 0
        e[27] = 0
        u = ("".join(c)).encode("utf-16-le")
        u = u + b"\xff" * (26 - len(u)) if len(u) < 26 else u[:26]
        e[1:11] = u[0:10]
        e[14:26] = u[10:22]
        e[28:32] = u[22:26]
        out.append(e)
    return out


def expect_cmd(chain, data_lba, csize, size):
    """按 FPGA 规范独立算一份命令表，用于和 C 的输出比对。"""
    cmd = bytearray(512)
    clusters = 0
    runs = []            # (lba, 累计扇区数)
    start = chain[0]
    prev = chain[0]
    for c in chain[1:]:
        clusters += 1
        if c != prev + 1:
            runs.append((data_lba + (start - 2) * csize, clusters * csize))
            start = c
        prev = c
    clusters += 1
    runs.append((data_lba + (start - 2) * csize, None))   # 最后一段 end = FFFFFFFF

    off = 4
    for i, (lba, end) in enumerate(runs):
        struct.pack_into("<I", cmd, off, lba)
        off += 4
        struct.pack_into("<I", cmd, off, 0xFFFFFFFF if end is None else end)
        off += 4
    struct.pack_into("<I", cmd, 0x1F0, size)
    struct.pack_into("<I", cmd, 0x1F4, 1)
    struct.pack_into("<I", cmd, 0x1F8, csize)
    return bytes(cmd)


def replay(cmd, rom_size):
    """按命令表把文件重新"装载"出来 —— 模拟 FPGA 的读法。"""
    out = []                 # [(lba, 扇区数), ...]
    prev_end = 0
    off = 4
    while off < 0x1F0:
        lba = struct.unpack_from("<I", cmd, off)[0]
        end = struct.unpack_from("<I", cmd, off + 4)[0]
        if lba == 0 and end == 0 and off > 4:
            break
        if end == 0xFFFFFFFF:
            n = (rom_size + SEC - 1) // SEC - prev_end
        else:
            n = end - prev_end
        out += [(lba, n)]
        prev_end = end if end != 0xFFFFFFFF else prev_end + n
        off += 8
        if end == 0xFFFFFFFF:
            break
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    rom_path = sys.argv[1]
    csize = 8
    if "--csize" in sys.argv:
        csize = int(sys.argv[sys.argv.index("--csize") + 1])

    fat32 = "--fat32" in sys.argv
    rom = open(rom_path, "rb").read()
    print(f"ROM  {os.path.basename(rom_path)}  {len(rom)} 字节  ({(len(rom)+16383)//16384} banks)")
    print(f"簇大小 {csize * SEC} 字节 (csize={csize})，卷 = {'FAT32' if fat32 else 'FAT16'}")

    tmp = tempfile.mkdtemp(prefix="ezjr-e2e-")
    img = os.path.join(tmp, "card.img")
    data_lba, chain = build_volume(rom, img, csize=csize, fat32=fat32)
    print(f"镜像 {os.path.getsize(img)} 字节，文件占 {len(chain)} 簇，data_lba={data_lba}")

    stub = os.path.join(tmp, "stub.c")
    src = FAT_STUB.replace("IMG_SIZE", str(os.path.getsize(img))).replace('"IMG"', '"' + img + '"')
    open(stub, "w").write(src)
    exe = os.path.join(tmp, "t")
    r = subprocess.run(["gcc", "-O1", "-DEZJR_HOST_TEST",
                        "-I" + os.path.join(ROOT, "src"),
                        stub,
                        os.path.join(ROOT, "src", "fat.c"),
                        os.path.join(ROOT, "src", "fatw.c"), os.path.join(ROOT, "src", "fatx.c"),
                        os.path.join(ROOT, "src", "game.c"),
                        "-o", exe], capture_output=True, text=True)
    if r.returncode:
        print("gcc 失败:\n" + r.stderr[:2000])
        return 1
    out = subprocess.run([exe], capture_output=True, text=True).stdout
    print("---- C 侧输出 ----")
    print(out.strip())
    print("-------------------")

    if "CMD " not in out:
        print("没有产出命令表，退出")
        return 1
    cmd = bytes.fromhex(out.split("CMD ", 1)[1].splitlines()[0])
    segs = replay(cmd, len(rom))
    print("重组：%d 段  %s" % (len(segs), segs[:4]))

    got = bytearray()
    for lba, n in segs:
        for k in range(n):
            got += open(img, "rb").read()[(lba + k) * SEC:(lba + k) * SEC + SEC]
    got = got[:len(rom)]

    if got == rom:
        print(f"OK   重组结果与源文件逐字节相同（{len(rom)} 字节）")
        return 0
    diff = next((i for i in range(min(len(got), len(rom))) if got[i] != rom[i]), -1)
    print(f"FAIL 第 {diff} 字节起不同（0x{diff:X}）: 得 {got[diff:diff+8].hex()} 期望 {rom[diff:diff+8].hex()}")
    print(f"     长度 got={len(got)} rom={len(rom)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
