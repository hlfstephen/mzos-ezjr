#!/bin/sh
# verify_all.sh — 一次性跑完全部可在本机做的验证（不含真机）
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
cd "$root"

echo "=========== 1. 构建两版 ROM ==========="
./build.sh
EZJR_TEST=1 ./build.sh

echo
echo "=========== 2. ROM 结构 ==========="
python3 tools/verify_rom.py build/ezgb.dat
python3 tools/verify_rom.py build/ezgb-test.dat
FAR_EXPORTS=build/obj/far_exports_active.txt python3 tools/verify_far.py build/obj
# 段布局（v0.16.9：库代码被排进 RAM 导致备份黑屏，这条专门盯它）
for b in 2 3 4; do
    [ -f "build/obj/bank$b.map" ] && \
        python3 tools/verify_banklayout.py "build/obj/bank$b.map" $b $((0xD100 + b * 0x200))
done
SDL_VIDEODRIVER=dummy python3 tools/farcall_test.py
# 跨 bank 参数（v0.16.13：v0.16.12 就是「SAVER 的 11 字节名从 bank1 传到 bank2」
# 导致读档读不出、备份回读校验失败 —— 段表/桩表全绿，因为它不是结构问题）
python3 tools/verify_bankptr.py build/obj/m

echo
echo "=========== 3. 主机侧算法 ==========="
python3 tools/fat_host_test.py
python3 tools/rtc_host_test.py
python3 tools/launch_host_test.py
python3 tools/fpga_host_test.py
python3 tools/save_host_test.py
python3 tools/window_host_test.py
python3 tools/savident_check.py
python3 tools/launch_sim_test.py

echo
echo "=========== 4. 模拟器 ==========="
python3 tools/emu_test.py

echo
echo "全部本机验证完成。真机项见 docs/03-verification.md 第 4 节。"
