#!/bin/sh
# build.sh — 从 C + 汇编源码构建 ezgb.dat
#
# 需要：SDCC 4.x（sdcc / sdasgb / sdldgb / makebin）、Python3。
# 不需要 rgbds / mgbdis：GB 头与校验和由 tools/fix_header.py 填。
#
#   ./build.sh                    -> build/ezgb.dat（128KB，8 bank）
#   ./build.sh --diag             -> 同上，另把自检屏编进 bank4
#   EZJR_TEST=1 ./build.sh        -> build/ezgb-test.dat（32KB 单 bank，模拟器自检用）
#   EZJR_CHECK_ONLY=1 ./build.sh  （仅回归校验，见 tools/verify_rom.py）
#
# ★ v0.16.6 起按 bank 分包（docs/12-bank-layout.md）：
#   bank0  $0000-$3FFF 常驻：crt0 / far 桩表 / main / video / fnt / fpga
#   bank1  $4000-$7FFF 常驻（默认映射）：fat / fatn / fatx / game / savkey / rtc / launch
#   bank2  fatw（FAT 写）      bank3  save（存档链）
#   bank4  debug（--diag）     bank5  字模
#   bank6/7 空（$FF）
# 可切 bank 的代码**单独链接**，跨 bank 调用走 src/farcall.s 的桩 ——
# 这样 16KB 的屋顶只算一个模块的量，不用再靠 --max-allocs 挤字节。
set -e
here=$(cd "$(dirname "$0")" && pwd)

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
mkdir -p "$b/m"

LIB=$(dirname "$(command -v sdcc)")/../share/sdcc/lib/sm83
TOTAL_BANKS=8           # 128 KB
FONT_BANK=5             # 字模所在的物理 bank
BANKRAM=0xD100          # 可切 bank 的静态变量区（$D100-$DFFF；$D000-$D0FF 是 handoff）
BANKRAM_STRIDE=0x200

# 从 tools/banks.conf 取某个 bank 的 .rel 列表（单一事实来源是那个文件）
bank_rels() {
    EZJR_DIAG_ON="$DIAG" python3 - "$1" "$b" "$here" <<'PY'
import os, sys
bank, obj, root = sys.argv[1], sys.argv[2], sys.argv[3]
# DIAG=0 时 bank4（自检屏）不参与：它只在 --diag 版编进去，
# 否则正式版会链上上一次构建留下的旧 diag.rel。
if bank == "4" and not os.environ.get("EZJR_DIAG_ON") in ("1",):
    print()
    sys.exit(0)
out = []
for line in open(os.path.join(root, "tools/banks.conf")):
    p = line.split("#")[0].split()
    if len(p) > 1 and p[0] == bank:
        for s in p[1:]:
            n = os.path.basename(s).rsplit(".", 1)[0]
            out.append(os.path.join(obj, "m", n + ".rel"))
print("\n".join(out))
PY
}

echo "[1/6] 检查工具链"
for t in sdcc sdasgb sdldgb makebin python3; do
    command -v "$t" >/dev/null 2>&1 || { echo "缺少 $t，请先安装 SDCC 4.x"; exit 1; }
done
sdcc --version 2>&1 | head -1

echo "[2/6] 字库"
# font_data.h 是生成产物、跟源码一起提交，所以默认不重新生成（生成要 10 秒）。
# 换了文案或字体时：EZJR_REGEN_FONT=1 ./build.sh
if [ -n "${EZJR_REGEN_FONT:-}" ] || [ ! -f src/font_data.h ]; then
    python3 tools/gen_font.py ${EZJR_FONT:+--font "$EZJR_FONT"} > "$b/font.log" 2>&1 \
        || { cat "$b/font.log"; exit 1; }
    grep -E "汉字|ROM 占用" "$b/font.log" | sed 's/^/      /'
else
    echo "      沿用 src/font_data.h（重新生成：EZJR_REGEN_FONT=1 ./build.sh）"
fi

echo "[3/6] 汇编 crt0.s / launch.s / farcall.s"
sdasgb -plosgff -o "$b/crt0.rel"   src/crt0.s
# farcall.rel 两版都要：crt0 的 gsinit 里会初始化 _far_cur / _far_sp
# （单 bank 版不切 bank，但符号得在，否则链不上）。
sdasgb -plosgff -o "$b/farcall.rel" src/farcall.s
if [ -z "${EZJR_TEST:-}" ]; then
    sdasgb -plosgff -o "$b/launch.rel"  src/launch.s
fi

echo "[4/6] 编译各 C 模块"
# 注意：SDCC 4.2 起 sm83 的默认 ABI 是 --sdcccall 1，预编译库
# （$SDCC/share/sdcc/lib/sm83，提供 memset/memcpy/32 位除法）也是按它编译的。
# 若这里显式写 --sdcccall 0，C 侧与库侧参数传递方式不一致，memset 会拿到
# 错误的长度而死循环——所以这里**不指定** ABI，全部走默认值。
#
# --max-allocs-per-node 20000：v0.16.6 起回到这个值。v0.16.5 曾为了在 32KB 里
# 挤下 900 字节提到 150000（省 400 字节，代价是编译 40 秒 → 4 分）。现在每个
# 模块有自己的 16KB 屋顶，不需要再榨优化器了。
# EZJR_FONT_BANKED：正式版必须定义 —— 字模（font_data.c）编在 bank5，
# video.c 读它之前要先把 $2000 切到 5。**漏掉这个宏不会报错**，只是
# FONT_BANK_ENTER() 变成空操作，于是读字模时窗口停在 bank1，把 fat.c 的
# 代码字节当成点阵画出来：满屏乱码小方块，程序本身却跑得好好的。
# v0.16.6 重构 build.sh 时漏过一次，靠模拟器截图比对才抓到，务必保留。
CFLAGS="-msm83 --opt-code-size --max-allocs-per-node 20000 --no-std-crt0 -Isrc -DEZJR_DIAG=$DIAG -DEZJR_FONT_BANKED"
if [ -n "${EZJR_TEST:-}" ]; then
    # 模拟器自检版：单 bank 32KB，用 src/test_stub.c 顶掉 fat.c 的 SD 读路径，
    # 并把 RTC 窗口换成 RAM 镜像，供 tools/emu_test.py 驱动验证。
    # 产物是 ezgb-test.dat，不能拷到卡上当正式内核用。
    DIAG=1
    CFLAGS="-msm83 --opt-code-size --max-allocs-per-node 20000 --no-std-crt0 -Isrc -DEZJR_DIAG=$DIAG -DEZJR_TEST_STUB"
    SRC_LIST="src/fpga.c src/video.c src/rtc.c src/diag.c src/main.c src/fnt.c src/test_stub.c src/font_data.c"
    ROM_NAME="ezgb-test.dat"
    echo "      模式：模拟器自检（EZJR_TEST=1）；调试屏 DIAG=$DIAG"
else
    SRC_LIST=$(DIAG_OFF=$([ "$DIAG" = "1" ] || echo 1) python3 - <<'PY'
import os
mods = []
for line in open("tools/banks.conf"):
    p = line.split("#")[0].split()
    if len(p) > 1:
        mods += p[1:]
import os
mods = [m for m in mods if not (os.path.basename(m) == "diag.c" and os.environ.get("DIAG_OFF"))]
print(" ".join("src/%s" % os.path.basename(m) for m in mods))
PY
)
    ROM_NAME="ezgb.dat"
    echo "      调试屏 DIAG=$DIAG（--diag 会把它编进 bank4）"
fi

for c in $SRC_LIST; do
    mod=$(basename "${c%.c}")
    extra=""
    if [ -z "${EZJR_TEST:-}" ]; then
        case "$mod" in
            # bank1 的模块编进 _BANK1 段（主链接把它定位 $4000）
            fat|fatn|fatx|game|savkey|rtc) extra="--codeseg BANK1" ;;
            # 字模进独立段，物理上放 bank5（--constseg 的段名只是逻辑名）
            font_data) extra="--constseg BANKFONT" ;;
        esac
    fi
    sdcc $CFLAGS $extra -c "$c" -o "$b/m/$mod.rel"
done

if [ -z "${EZJR_TEST:-}" ]; then
    # ---------------------------------------------------------------- 多 bank 路径
    echo "[5/6] 链接（bank0/1 常驻 + bank2-5 独立）"

    # 链接 A：只含 bank0/1，可切 bank 的函数先用 $4000 占位。
    # 目的：拿到常驻段的代码地址与**全部 RAM 变量地址** —— 可切 bank 单独链接时
    # 要知道它们，而 _DATA 段的分配只由 bank0/1 的 rel 决定，与桩无关，所以
    # 这一步算出的地址在链接 B 里仍然成立（下面有 cmp 断言）。
    FONT_G=$(python3 - "$b/font.map" <<'PY' 2>/dev/null || true
PY
)

    # 字模先单独链接 -> 拿符号地址（_BANKFONT 定位 $4000，物理上是 bank5）
    cat > "$b/font.lk" <<EOF
-mjwx
-i $b/font.ihx
-b _BANKFONT = 0x4000
$b/m/font_data.rel
-e
EOF
    sdldgb -nf "$b/font.lk"
    python3 tools/ihx2raw.py "$b/font.ihx" "$b/font.raw"
    FONT_G=$(python3 - "$b/font.map" <<'PY'
import sys
want = ("cjk_symbols", "ascii_font", "cjk_font", "cjk_cp")
got = {}
for line in open(sys.argv[1]):
    p = line.split()
    if len(p) >= 2:
        n = p[1].lstrip("_")
        if n in want and n not in got:
            got[n] = int(p[0], 16)
miss = [n for n in want if n not in got]
if miss:
    sys.exit("font.map 缺符号: " + ", ".join(miss))
for n in want:
    print("-g _%s = 0x%04X" % (n, got[n]))
PY
)
    echo "      字模符号：$(echo $FONT_G | tr '\n' ' ')"

    # 桩表固定紧跟在 crt0 之后 = $0400 起（crt0 的 _CODE 段是空的）。
    # 为什么要固定：可切 bank 单独链接时要往桩里 call，而那会儿主链接还没做，
    # 桩的真实地址算不出来。固定成 $0400 后两边天然一致，也不用二次链接。
    # 链接 A 里放同样大小、同样标签的**哑桩**（全 0），布局与链接 B 完全相同。
    # 本版实际生效的导出清单：diag_* 只在 --diag 版存在，正式版剔掉，
    # 否则桩表编号会错位（dummy 与真桩必须逐个对齐）。
    EXPORTS="$b/far_exports_active.txt"
    if [ "$DIAG" = "1" ]; then
        cp tools/far_exports.txt "$EXPORTS"
    else
        grep -v -E "^diag_(screen|last_record)" tools/far_exports.txt > "$EXPORTS"
    fi
    export FAR_EXPORTS="$EXPORTS"

    python3 tools/gen_farstub.py --dummy --exports "$EXPORTS" --out "$b/farstub_dummy.s"
    sdasgb -plosgff -o "$b/farstub_dummy.rel" "$b/farstub_dummy.s"
    STUB_BASE=0x0400
    export STUB_BASE

    cat > "$b/romA.lk" <<EOF
-mjwx
-i $b/ezgbA.ihx
-b _HOME = 0x0150
-b _CODE = 0x0400
-b _BANK1 = 0x4000
-b _DATA = 0xC000
-b _INITIALIZED = 0xC000
-k $LIB
-l sm83
$FONT_G
$b/crt0.rel
$b/farstub_dummy.rel
$b/farcall.rel
$(bank_rels 0)
$(bank_rels 1)
$b/launch.rel
-e
EOF
    sdldgb -nf "$b/romA.lk"

    echo "      桩表起点：$STUB_BASE（固定，紧跟 crt0）"

    # 各可切 bank 单独链接：代码/常量/库都定位在 $4000-$7FFF，静态变量放各自的 RAM 区
    #
    # ★★ 为什么每个段都必须显式 -b（v0.16.9 的真机事故）：
    #   独立链接时，sdld **只**知道命令行里 -b 过的段，其余段一律排到已定位段
    #   之后 —— 而这里第一个已定位段是 RAM 的 _DATA($D5xx)，于是
    #   `_HOME`（SDCC 把 __divulong/__mullong 这些乘除法库放在这个段）被排到
    #   $D5xx 的 RAM 上。后果是：
    #     · ihx2raw 只收 $0000-$7FFF，那 379 字节的库代码**根本没进 ROM**；
    #     · 桩/符号表里 __divulong 的地址是 $D500，fatw 算簇 LBA（32 位乘除）
    #       时 call 过去，执行的是被 crt0 清零的 RAM —— 一条 NOP 接一条 NOP，
    #       一路跑飞。真机表现：备份进度屏「正在备份」刚画出来就黑屏。
    #   _CONST/_INITIALIZER/_GSINIT/_GSFINAL 同理，少了任何一个都可能静默落进
    #   RAM 或低地址。tools/verify_banklayout.py 现在会盯住这一点。
    #
    # 布局（每 bank 16KB，$4000-$7FFF）：
    #   $4000  _HOME        库（乘除法，实测 379 字节）
    #   $4200  _CODE        本 bank 的代码（最大 11.5KB，fatw 现在 8.3KB）
    #   $7000  _CONST       常量
    #   $7800  _INITIALIZER 初值镜像（若哪天出现带初值的 static，说明要补 gsinit）
    #   $7900  _GSINIT / _GSFINAL
    #
    # 空段占位：sdld 对「-b 指定了、却没有任何模块定义」的段会打 warning 并返回
    # 退出码 2（set -e 直接打断构建）。某些 bank 确实一个常量都没有，所以先用
    # 一个只声明 .area、不放内容的汇编把它们「定义」出来，让布局无条件固定。
    cat > "$b/segstub.s" <<'EOF'
        .module segstub
        .area   _HOME
        .area   _CONST
        .area   _INITIALIZER
        .area   _GSINIT
        .area   _GSFINAL
EOF
    sdasgb -plosgff -o "$b/segstub.rel" "$b/segstub.s"

    : > "$b/banksym.txt"
    # bank1 的函数对可切 bank 来说也要经过桩（bank2 运行时 $4000 窗口里是 bank2，
    # 直接 call 会跳到同址的字节上），所以它们的地址也进 banksym。
    python3 tools/banktools.py stub "$b/ezgbA.map" 1 "$b/banksym.txt" _BANK1
    for bank in 2 3 4; do
        rels=$(bank_rels $bank)
        if [ -z "$rels" ] || [ ! -f "$(echo $rels | awk '{print $1}')" ]; then
            echo "      bank$bank：空"
            rm -f "$b/bank$bank.raw"      # 别让上一次构建的残留混进来
            rm -f "$b/bank$bank.map" "$b/bank$bank.ihx"
            continue
        fi
        python3 tools/banktools.py inject "$b/ezgbA.map" "$b/bank$bank.g" $rels
        dr=$((BANKRAM + bank * BANKRAM_STRIDE))
        cat > "$b/bank$bank.lk" <<EOF
-mjwx
-i $b/bank$bank.ihx
-b _HOME = 0x4000
-b _CODE = 0x4200
-b _CONST = 0x7000
-b _INITIALIZER = 0x7800
-b _GSINIT = 0x7900
-b _GSFINAL = 0x7A00
-b _DATA = $dr
-b _INITIALIZED = $dr
-k $LIB
-l sm83
$rels
$b/segstub.rel
$(cat $b/bank$bank.g)
-e
EOF
        sdldgb -nf "$b/bank$bank.lk"
        python3 tools/ihx2raw.py "$b/bank$bank.ihx" "$b/bank$bank.raw"
        python3 tools/verify_banklayout.py "$b/bank$bank.map" $bank $dr --no-rom
        python3 tools/banktools.py stub "$b/bank$bank.map" $bank "$b/banksym.txt"
    done

    # 桩表（生成 -> 汇编）
    python3 tools/gen_farstub.py --sym "$b/banksym.txt" \
        --exports "$EXPORTS" --out "$b/farstub.s"
    sdasgb -plosgff -o "$b/farstub.rel" "$b/farstub.s"

    # 链接 B：桩表**排在最后**，这样常驻段里已有符号的地址不会因为桩变长而后移
    # （cmp 会盯着这条假设，破了就报错）。
    cat > "$b/rom.lk" <<EOF
-mjwx
-i $b/ezgb.ihx
-b _HOME = 0x0150
-b _CODE = 0x0400
-b _BANK1 = 0x4000
-b _DATA = 0xC000
-b _INITIALIZED = 0xC000
-k $LIB
-l sm83
$FONT_G
$b/crt0.rel
$b/farstub.rel
$b/farcall.rel
$(bank_rels 0)
$(bank_rels 1)
$b/launch.rel
-e
EOF
    sdldgb -nf "$b/rom.lk"
    python3 tools/banktools.py cmp "$b/ezgbA.map" "$b/ezgb.map"
    python3 tools/ihx2raw.py "$b/ezgb.ihx" "$b/ezgb.raw"

    # 拼接：bank0/1 是主镜像，bank2-5 各取自己的后 16KB，其余填 $FF
    JOIN=""
    for bank in 2 3 4; do
        [ -f "$b/bank$bank.raw" ] && JOIN="$JOIN $bank:$b/bank$bank.raw"
    done
    JOIN="$JOIN $FONT_BANK:$b/font.raw"
    python3 tools/join_banks.py "$b/ezgb128.raw" $TOTAL_BANKS "$b/ezgb.raw" $JOIN
    RAW="$b/ezgb128.raw"

    echo "[6/6] 填 GB 头 + 校验和，产出 $ROM_NAME"
    python3 tools/fix_header.py "$RAW" "$out/$ROM_NAME"

    # 段布局检查（$0000-$7FFF 内不得越界/重叠）
    python3 - "$b/ezgb.map" <<'PY'
import re, sys
m = open(sys.argv[1]).read()
s = {k: int(v, 16) for v, k in re.findall(r"([0-9A-Fa-f]{8})\s+s__(\w+)", m)}
l = {k: int(v, 16) for v, k in re.findall(r"([0-9A-Fa-f]{8})\s+l__(\w+)", m)}
rom = sorted((s[a], s[a] + l.get(a, 0), a)
             for a in ("HOME", "CODE", "BANK1", "INITIALIZER", "GSINIT", "GSFINAL") if a in s)
for x, y, a in rom:
    if y > 0x8000:
        sys.exit(f"链接错误: _{a} 到 ${y:04X}，超出 $0000-$7FFF")
for (s1, e1, a1), (s2, e2, a2) in zip(rom, rom[1:]):
    if e1 > s2 and e1 > s1 and e2 > s2:
        sys.exit(f"链接错误: _{a1} (${s1:04X}-${e1:04X}) 与 _{a2} (${s2:04X}-${e2:04X}) 重叠")
print("  段布局:", ", ".join(f"_{a}=${x:04X}-${y:04X}" for x, y, a in rom))
LIMIT = {"HOME": 0x0400, "CODE": 0x4000, "BANK1": 0x8000}
for x, y, a in rom:
    if a in LIMIT:
        print(f"    _{a:6s} ${x:04X}-${y - 1:04X}  余 {LIMIT[a] - y:5d} 字节")
PY

    # 可切 bank 的最终校验：段在窗口内 + 没有 call 到 RAM + **窗口字节与 ROM 一致**。
    # 最后一条要等 join/fix_header 之后才有意义，所以放在这里（上面循环里那次
    # 传 --no-rom 跳过它，那时 build/ezgb.dat 还是上一次的产物）。
    for bank in 2 3 4; do
        if [ -f "$b/bank$bank.map" ] && [ -f "$b/bank$bank.raw" ]; then
            python3 tools/verify_banklayout.py "$b/bank$bank.map" $bank \
                $((BANKRAM + bank * BANKRAM_STRIDE))
        fi
    done

else
    # ---------------------------------------------------------------- 单 bank 路径（模拟器自检）
    echo "[5/6] 链接（单 bank 32KB）"
    cat > "$b/rom.lk" <<EOF
-mjwx
-i $b/ezgb.ihx
-b _HOME = 0x0150
-b _CODE = 0x0400
-b _DATA = 0xC000
-b _INITIALIZED = 0xC000
-k $LIB
-l sm83
$b/crt0.rel
$b/farcall.rel
$(for m in fpga video rtc diag main fnt test_stub font_data; do echo $b/m/$m.rel; done)
-e
EOF
    sdldgb -nf "$b/rom.lk"
    python3 tools/ihx2raw.py "$b/ezgb.ihx" "$b/ezgb.raw"
    RAW="$b/ezgb.raw"
    echo "[6/6] 填 GB 头 + 校验和，产出 $ROM_NAME"
    python3 tools/fix_header.py "$RAW" "$out/$ROM_NAME"
fi

echo ""
echo "完成：build/$ROM_NAME"
python3 tools/fix_header.py --verify "$out/$ROM_NAME"
echo ""
echo "跨 bank 参数检查："
if [ -z "${EZJR_TEST:-}" ]; then
    # 指向本 bank ROM 的指针（常量、字符串字面量）不能跨 bank 传 —— 切过去
    # 就指向别的 bank 的字节。v0.16.12 的「读档读不出、备份校验失败」就是它。
    python3 tools/verify_bankptr.py "$b/m" || exit 1
fi
echo ""
echo "回归校验："
python3 tools/verify_rom.py "$out/$ROM_NAME" || true
