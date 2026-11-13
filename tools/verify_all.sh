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

echo
echo "=========== 3. 主机侧算法 ==========="
python3 tools/fat_host_test.py
python3 tools/rtc_host_test.py
python3 tools/launch_host_test.py
python3 tools/fpga_host_test.py
python3 tools/save_host_test.py
python3 tools/window_host_test.py

echo
echo "=========== 4. 模拟器 ==========="
python3 tools/emu_test.py

echo
echo "全部本机验证完成。真机项见 docs/03-verification.md 第 4 节。"
