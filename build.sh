#!/bin/sh
# build.sh — 从 C + 汇编源码构建基本内核 ezgb.dat（32KB GB ROM）
#
# 需要：SDCC 4.x（sdcc / sdasgb / sdldgb / makebin）、Python3。
# 不需要 rgbds / mgbdis：GB 头与校验和由 tools/fix_header.py 填。
#
#   ./build.sh                 -> build/ezgb.dat（可直接拷到 SD 卡根目录）
#   EZJR_CHECK_ONLY=1 ./build.sh  （仅回归校验，见 tools/verify_rom.py）
set -e
here=$(cd "$(dirname "$0")" && pwd)

# 选项：
#   --diag        把真机自检屏编进去（约 1.5 KB，默认不编，见 docs/05）
#   --font=PATH   重新生成字库时用这个字体（需同时给 EZJR_REGEN_FONT=1）
DIAG=0
FONT_OPT=""
for a in "$@"; do
    case "$a" in
        --diag)    DIAG=1 ;;
        --font=*)  FONT_OPT="${a#--font=}" ;;
    esac
done
if [ -n "$FONT_OPT" ]; then export EZJR_FONT="$FONT_OPT"; fi
cd "$here"
out="$here/build"
# 正式版与自检版用各自的 obj 目录：否则后一次构建的 map/ihx 会覆盖前一次的，
# tools/emu_test.py 需要按符号查地址（如 g_test_rtc_win）就取不到了。
if [ -n "${EZJR_TEST:-}" ]; then b="$out/obj-test"; else b="$out/obj"; fi
mkdir -p "$b"

echo "[1/6] 检查工具链"
for t in sdcc sdasgb sdldgb makebin python3; do
    command -v "$t" >/dev/null 2>&1 || { echo "缺少 $t，请先安装 SDCC 4.x"; exit 1; }
done
sdcc --version 2>&1 | head -1

echo "[2/6] 字库"
# font_data.h 是生成产物、跟源码一起提交，所以默认不重新生成（生成要 10 秒）。
# 换了文案或字体时：EZJR_REGEN_FONT=1 ./build.sh
# 字体默认用 fusion-pixel 8px 等宽简中（OFL-1.1），见 tools/gen_font.py 顶部说明。
if [ -n "${EZJR_REGEN_FONT:-}" ] || [ ! -f src/font_data.h ]; then
    python3 tools/gen_font.py ${EZJR_FONT:+--font "$EZJR_FONT"} > "$b/font.log" 2>&1 \
        || { cat "$b/font.log"; exit 1; }
    grep -E "汉字|ROM 占用" "$b/font.log" | sed 's/^/      /'
else
    echo "      沿用 src/font_data.h（重新生成：EZJR_REGEN_FONT=1 ./build.sh）"
fi

echo "[3/6] 汇编 crt0.s / launch.s"

echo "[4/6] 编译各 C 模块"
# 注意：SDCC 4.2 起 sm83 的默认 ABI 是 --sdcccall 1，预编译库
# （$SDCC/share/sdcc/lib/sm83，提供 memset/memcpy/32 位除法）也是按它编译的。
# 若这里显式写 --sdcccall 0，C 侧与库侧参数传递方式不一致，memset 会拿到
# 错误的长度而死循环——所以这里**不指定** ABI，全部走默认值。
CFLAGS="-msm83 --opt-code-size --max-allocs-per-node 20000 --no-std-crt0 -Isrc -DEZJR_DIAG=$DIAG"
# EZJR_TEST=1 时构建「模拟器自检版」：用 src/test_stub.c 顶掉 fat.c 的
# SD 读路径，并把 RTC 窗口换成 RAM 镜像，供 tools/emu_test.py 驱动验证。
# 产物是 ezgb-test.dat，不能拷到卡上当正式内核用。
if [ -n "${EZJR_TEST:-}" ]; then
    # 模拟器自检版必须带调试屏：tools/emu_test.py 就是靠它读寄存器的
    DIAG=1
    CFLAGS="-msm83 --opt-code-size --max-allocs-per-node 20000 --no-std-crt0 -Isrc -DEZJR_DIAG=$DIAG -DEZJR_TEST_STUB"
    SRC_LIST="src/fpga.c src/video.c src/rtc.c src/diag.c src/main.c src/fnt.c src/test_stub.c"
    ASM_LIST="src/crt0.s"
    ROM_NAME="ezgb-test.dat"
    echo "      模式：模拟器自检（EZJR_TEST=1）；调试屏 DIAG=$DIAG"
else
    SRC_LIST="src/fpga.c src/fat.c src/fatn.c src/fatw.c src/fatx.c src/game.c src/save.c src/video.c src/rtc.c src/fnt.c src/main.c"
    # 自检屏（diag.c，约 780 字节）只在 --diag 版编进去：正式版里没人调它，
    # 白白占着 BANK1 —— 那一段 v0.10.0 之后只剩几百字节。
    if [ "$DIAG" = "1" ]; then SRC_LIST="$SRC_LIST src/diag.c"; fi
    ASM_LIST="src/crt0.s src/launch.s"
    ROM_NAME="ezgb.dat"
    echo "      调试屏 DIAG=$DIAG（--diag 打开）"
fi
for a in $ASM_LIST; do
    sdasgb -plosgff -o "$b/$(basename "${a%.s}").rel" "$a"
done
# 存档(save.c)、自检(diag.c)、FAT 读写(fat.c/fatw.c)编到 BANK1 段，
# 由链接器放到 $4000-$7FFF：32KB 的 ROM 在 $4000 处天然就是文件后半段，
# 跨段 call 只是地址不同，不需要切 bank（header 仍写 ROM ONLY）。
# 不这么拆，_CODE 会冲出 $4000。
# v0.9.0：错误屏中文化后 _CODE 涨到 $41FD，把 fat.c 也挪了过来；
# 换来 fnt.c 挪回 _CODE —— BANK1 只有 16KB，diag 版要塞下自检屏，
# 谁大谁留 _CODE 都得算，「全塞 BANK1」在 diag 版会溢出。
# v0.10.0：BANK1 只剩几百字节，SAVER 目录/删除（fatx.c）改放 _CODE；
# diag.c 也回 _CODE（它只占不到 800 字节，_CODE 还有三四个 KB 余量）。
# v0.12.0：fat_full_lfn（存档同名要用的完整长名，近 900 字节）放在 _CODE 的
# fatn.c 里，于是 _CODE 反过来先满了（--diag 版溢出 433 字节）。腾地方的办法：
#   ① 删掉从来没人调用的 fat_next_cluster（BANK1 省 307 字节，还顺带去掉一个
#      512 字节的栈上缓冲）；
#   ② 把 fpga.c（417 字节，纯端口读写，放哪一段都一样）挪到 BANK1。
# 两处加起来让 --diag 版重新回到 32KB 以内。
rels=""
for c in $SRC_LIST; do
    o="$b/$(basename "${c%.c}").rel"
    extra=""
    # 模拟器自检版没有 BANK1（链接脚本里那一行被删掉了），所以不能给任何文件
    # 指定 --codeseg BANK1 —— 否则那部分代码没有落点，链接直接溢出。
    if [ -z "${EZJR_TEST:-}" ]; then
        case "$c" in
            src/save.c|src/fat.c|src/fatw.c) extra="--codeseg BANK1" ;;
        esac
    fi
    sdcc $CFLAGS $extra -c "$c" -o "$o"
    rels="$rels $o"
done

echo "[5/6] 链接到真实 ROM 地址"
# _HOME=$0150, _CODE=$0400, _DATA=$C000(WRAM)；sm83 库提供 32 位运算 helper。
# 段顺序与 stage1/build.sh 一致，确保 _HOME/_INITIALIZER/_GSINIT 都落在 _CODE 之前。
# 只定位 _HOME / _CODE / _DATA；_INITIALIZER/_GSINIT/_GSFINAL 由链接器
# 顺序排布（显式都定位到 0x7E00 会互相重叠）。这些段必须全部落在 _CODE(0x0400)
# 之前，否则会覆盖代码——下面用 map 检查实际边界。
cat > "$b/rom.lk" <<EOF
-mjwx
-i $b/ezgb.ihx
-b _HOME = 0x0150
-b _CODE = 0x0400
-b _BANK1 = 0x4000
-b _DATA = 0xC000
-b _INITIALIZED = 0xC000
-k $(dirname "$(command -v sdcc)")/../share/sdcc/lib/sm83
-l sm83
$(for a in $ASM_LIST; do echo "$b/$(basename "${a%.s}").rel"; done)
$(for r in $rels; do echo "$r"; done)
-e
EOF
# 模拟器自检版没有任何源文件编到 BANK1（fat/save/diag 都不在 SRC_LIST 里），
# 这时那一行 -b 会让链接器报 "No definition of area _BANK1" 并整体失败。
# 删掉那一行比在上面拼字符串干净：剩下的行还要参与后续的 -k/-l/-e。
if [ -n "${EZJR_TEST:-}" ]; then
    grep -v "_BANK1" "$b/rom.lk" > "$b/rom2.lk" && mv "$b/rom2.lk" "$b/rom.lk"
fi
sdldgb -nf "$b/rom.lk"
makebin -s 32768 "$b/ezgb.ihx" "$b/ezgb.raw"

echo "[6/6] 填 GB 头 + 校验和，产出 $ROM_NAME"
python3 tools/fix_header.py "$b/ezgb.raw" "$out/$ROM_NAME"

# 段重叠检查（防止 _HOME 溢出撞上中断向量或 _CODE）
python3 - "$b/ezgb.map" <<'PY'
import re, sys
m = open(sys.argv[1]).read()
s = {k: int(v, 16) for v, k in re.findall(r"([0-9A-Fa-f]{8})\s+s__(\w+)", m)}
l = {k: int(v, 16) for v, k in re.findall(r"([0-9A-Fa-f]{8})\s+l__(\w+)", m)}
rom = sorted((s[a], s[a] + l.get(a, 0), a)
             for a in ("HOME", "CODE", "BANK1", "INITIALIZER", "GSINIT", "GSFINAL") if a in s)
for x, y, a in rom:
    if y > 0x8000:
        sys.exit(f"链接错误: _{a} 到 ${y:04X}，超出 32KB ROM")
for (s1, e1, a1), (s2, e2, a2) in zip(rom, rom[1:]):
    if e1 > s2 and e1 > s1 and e2 > s2:
        sys.exit(f"链接错误: _{a1} (${s1:04X}-${e1:04X}) 与 _{a2} (${s2:04X}-${e2:04X}) 重叠")
print("  段布局:", ", ".join(f"_{a}=${x:04X}-${y:04X}" for x, y, a in rom))
PY

echo ""
echo "完成：build/$ROM_NAME"
python3 tools/fix_header.py --verify "$out/$ROM_NAME"
echo ""
echo "回归校验："
python3 tools/verify_rom.py "$out/$ROM_NAME" || true
