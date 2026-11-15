#!/usr/bin/env python3
"""launch.s 分支逻辑验证（最小 GB CPU 模拟）。

为什么需要它：launch.s 会被搬到 WRAM 执行，里面不允许任何跨符号引用，
所以"等装载器""超时""校验头"这些分支在真机上跑错了就是白屏，在模拟器里
又完全测不出来（PyBoy 没有 FPGA）。这里只模拟这条路径，把装载器的几种
时间行为各跑一遍，断言最后走的是哪条路、以及**有没有在装载开始前放行**。

判据来自 RTL（stage0/rtl/gb_data_out.v:124）与 PicoBlaze 固件
（re/stage0/picoblaze/fw5-0918/bank1.psm，$222 置 busy、$227 清）：
装载器忙的整段时间里读什么都返回 $01，所以 $0104 和 $A000 同时为 $01
才说明"这次装载真的开始了"。

超时用 DIV 计数，模拟里把 DIV 的增量放大（每次读 +128，两次读回绕一次），
否则真要跑几百万步；只验证分支与先后顺序，不验证真实秒数。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_code():
    rom = open(os.path.join(ROOT, "build", "ezgb.dat"), "rb").read()
    m = open(os.path.join(ROOT, "build", "obj", "ezgb.map")).read()
    a = re.search(r"([0-9A-F]{8})\s+_launch_start", m)
    b = re.search(r"([0-9A-F]{8})\s+_launch_end", m)
    if not (a and b):
        raise SystemExit("map 里找不到 _launch_start/_launch_end")
    return rom, int(a.group(1), 16), int(b.group(1), 16)


# Nintendo logo，$0104 起 8 字节
LOGO = bytes([0xCE, 0xED, 0x66, 0x66, 0xCC, 0x0D, 0x00, 0x0B])


def loader(busy_from, busy_to, head):
    """装载器模型：步数在 [busy_from, busy_to) 里时 busy，读什么都返回 $01。

    head：装载完成后 $0104 上的 8 字节（正常是 LOGO，失败时是别的）。
    """
    def f(s, addr):
        if busy_from <= s.steps < busy_to:
            return 0x01
        if 0x0104 <= addr < 0x0104 + 8:
            return head[addr - 0x0104]
        if addr == 0xA000:
            return 0xF3                     # 命令表第 0 字节，随便一个非 $01
        return None
    return f


class Sim:
    def __init__(self, rom, load, head=LOGO):
        self.mem = bytearray(0x10000)
        self.mem[0:0x8000] = rom[0:0x8000]
        self.load = load                  # (self, addr) -> 值 或 None
        self.head = head
        self.fail_seen = False
        self.game_seen = False
        self.game_at = None
        self.busy_start = None
        self.writes = []                  # (addr, val) 记录 IO / FPGA 写
        self.dv = 0
        self.regs = {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0, "h": 0, "l": 0}
        self.f = {"z": 0, "c": 0}
        self.steps = 0
        self.vram = {}

    # --- 内存 ---
    def rd(self, addr):
        addr &= 0xFFFF
        if addr == 0xFF04:
            self.dv = (self.dv + 128) & 0xFF     # 放大 128 倍：两次读回绕一次
            return self.dv
        v = self.load(self, addr)
        if v is not None:
            return v
        if 0x8000 <= addr <= 0x9FFF:
            return self.vram.get(addr, self.mem[addr])
        return self.mem[addr]

    def wr(self, addr, val):
        addr &= 0xFFFF
        val &= 0xFF
        if 0x8000 <= addr <= 0x9FFF:
            self.vram[addr] = val
            return
        self.mem[addr] = val
        self.writes.append((addr, val))
        if addr == 0xFF40 and val == 0x80 and not self.game_seen:
            self.fail_seen = True           # fail 段的第一条写：闪屏开始
        if addr == 0x7FE0 and val == 0x80:
            self.game_seen = True           # $7FE0=$80：复位进游戏
            self.game_at = self.steps

    def got(self, addr, val):
        return any(a == addr and v == val for a, v in self.writes)

    # --- 寄存器 ---
    def r(self, i):
        if i == 6:                    # (HL)：必须走 rd()，$A000/$FF04/$0104 是回调
            return self.rd(self.hl())
        return [self.regs["b"], self.regs["c"], self.regs["d"],
                self.regs["e"], self.regs["h"], self.regs["l"],
                0, self.regs["a"]][i]

    def setr(self, i, v):
        v &= 0xFF
        for n, k in enumerate(["b", "c", "d", "e", "h", "l"]):
            if i == n:
                self.regs[k] = v
                return
        if i == 6:
            self.wr(self.hl(), v)
        else:
            self.regs["a"] = v

    def hl(self):
        return (self.regs["h"] << 8) | self.regs["l"]

    def set_hl(self, v):
        self.regs["h"] = (v >> 8) & 0xFF
        self.regs["l"] = v & 0xFF

    def rr(self, i):
        if i == 0:
            return (self.regs["b"] << 8) | self.regs["c"]
        if i == 1:
            return (self.regs["d"] << 8) | self.regs["e"]
        return self.hl()

    def set_rr(self, i, v):
        v &= 0xFFFF
        if i == 0:
            self.regs["b"], self.regs["c"] = (v >> 8) & 0xFF, v & 0xFF
        elif i == 1:
            self.regs["d"], self.regs["e"] = (v >> 8) & 0xFF, v & 0xFF
        else:
            self.set_hl(v)

    def run(self, pc, end, limit=400000):
        while self.steps < limit:
            if self.fail_seen:
                return ("失败屏", pc)
            if pc < 0 or pc >= len(self.mem):
                return ("跑飞", pc)
            if pc < end and self.steps > 32 and self.mem[pc] == 0x18 and self.mem[pc + 1] == 0xFE:
                return ("自循环", pc)          # jr $
            self.steps += 1
            op = self.mem[pc]
            pc = self.step(op, pc)
            if pc is None:
                return ("halt", 0)
        return ("步数超限", pc)

    def step(self, op, pc):
        n = self.mem
        # LD r,n
        if 0x06 <= op <= 0x3E and op & 7 == 6 and op < 0x40:
            self.setr((op >> 3) & 7, n[pc + 1])
            return pc + 2
        # LD r,r'
        if 0x40 <= op <= 0x7F and op != 0x76:
            self.setr((op >> 3) & 7, self.r(op & 7))
            return pc + 1
        # ALU r
        if 0x80 <= op <= 0xBF:
            self.alu((op >> 3) & 7, self.r(op & 7))
            return pc + 1
        # ALU n
        if 0xC6 <= op <= 0xFE and op & 7 == 6:
            self.alu((op >> 3) & 7, n[pc + 1])
            return pc + 2
        if op == 0x18:                                    # JR e
            return pc + 2 + self.se(n[pc + 1])
        if op in (0x20, 0x28, 0x30, 0x38):                # JR cc,e
            take = {0x20: not self.f["z"], 0x28: self.f["z"],
                    0x30: not self.f["c"], 0x38: self.f["c"]}[op]
            return (pc + 2 + self.se(n[pc + 1])) if take else pc + 2
        if op == 0x21:                                    # LD HL,nn
            self.set_hl(n[pc + 1] | (n[pc + 2] << 8))
            return pc + 3
        if op == 0x11:                                    # LD DE,nn
            self.set_rr(1, n[pc + 1] | (n[pc + 2] << 8))
            return pc + 3
        if op == 0x01:                                    # LD BC,nn
            self.set_rr(0, n[pc + 1] | (n[pc + 2] << 8))
            return pc + 3
        if op == 0x36:                                    # LD (HL),n
            self.wr(self.hl(), n[pc + 1])
            return pc + 2
        if op == 0xEA:                                    # LD (nn),A
            self.wr(n[pc + 1] | (n[pc + 2] << 8), self.regs["a"])
            return pc + 3
        if op == 0xFA:                                    # LD A,(nn)
            self.regs["a"] = self.rd(n[pc + 1] | (n[pc + 2] << 8))
            return pc + 3
        if op == 0x23:                                    # INC HL
            self.set_hl(self.hl() + 1)
            return pc + 1
        if op in (0x03, 0x13, 0x33):                      # INC rr
            self.set_rr((op >> 4) & 3, self.rr((op >> 4) & 3) + 1)
            return pc + 1
        if op in (0x0B, 0x1B, 0x2B, 0x3B):                # DEC rr
            self.set_rr((op >> 4) & 3, self.rr((op >> 4) & 3) - 1)
            return pc + 1
        if op in (0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x3D):
            i = (op >> 3) & 7
            v = (self.r(i) - 1) & 0xFF
            self.setr(i, v)
            self.f["z"] = int(v == 0)
            return pc + 1
        if op == 0xAF:
            self.regs["a"] = 0
            self.f["z"] = 1
            return pc + 1
        if op == 0x76:
            return None
        if op == 0x00:
            return pc + 1
        raise SystemExit("未实现指令 %02X @ %04X" % (op, pc))

    @staticmethod
    def se(b):
        return b - 256 if b > 127 else b

    def alu(self, kind, v):
        a = self.regs["a"]
        if kind == 4:                 # AND
            a &= v
            self.f["z"] = int(a == 0)
            self.f["c"] = 0
        elif kind == 5:               # XOR
            a ^= v
            self.f["z"] = int(a == 0)
            self.f["c"] = 0
        elif kind == 6:               # OR
            a |= v
            self.f["z"] = int(a == 0)
            self.f["c"] = 0
        elif kind == 7:               # CP
            self.f["z"] = int(a == v)
            self.f["c"] = int(a < v)
        else:
            raise SystemExit("未实现 ALU %d" % kind)
        self.regs["a"] = a & 0xFF


def case(name, start, end, rom, load, expect, fast=None, head=LOGO,
         must_be_after=None):
    s = Sim(rom, load, head)
    how, pc = s.run(start, end)
    game = s.game_seen
    fail_screen = s.fail_seen
    cart = s.got(0x7F31, 0x00)
    ok = (game and not fail_screen) if expect == "game" else (fail_screen and not game)
    why = ""
    if ok and fast is not None and s.steps >= fast:
        ok, why = False, "没有早退（步数 %d ≥ %d）" % (s.steps, fast)
    if ok and must_be_after is not None:
        # 必须在装载器忙过之后才放行：$7F36=0 不能早于 busy 结束
        if s.game_at is None or s.game_at <= must_be_after:
            ok, why = False, "装载没开始就放行了（game@%s ≤ %d）" % (s.game_at, must_be_after)
    print("  %-4s %-22s 结束=%s@%04X 进游戏=%s@%s 失败屏=%s 换卡=%s 步数=%d%s"
          % ("OK" if ok else "FAIL", name, how, pc, game, s.game_at,
             fail_screen, cart, s.steps, ("  " + why) if why else ""))
    return ok


def main():
    rom, start, end = load_code()
    print("launch.s 分支验证（段 $%04X-$%04X，%d 字节）" % (start, end, end - start))

    ok = True
    # 正常装载：$0104 上本来就躺着上一位游戏的合法 logo（换卡前的残留），
    # 装载器忙 30-260 步。必须在忙完之后才放行，绝不能拿旧 logo 当完成信号。
    ok &= case("皮卡丘 正常装载", start, end, rom,
               loader(30, 260, LOGO), "game", fast=1200, must_be_after=260)
    ok &= case("水晶 正常装载", start, end, rom,
               loader(30, 260, LOGO), "game", fast=1200, must_be_after=260)
    # 装载器根本没被调度（license 不过 / 命令表无效）：$0104 是旧 logo，
    # 旧逻辑会在这里直接放行 —— 现在必须报失败。
    ok &= case("装载器没起来", start, end, rom, loader(1 << 30, 1 << 30, LOGO), "fail")
    # 装载器卡死：busy 一直挂着
    ok &= case("busy 不落", start, end, rom, loader(0, 1 << 30, LOGO), "fail")
    # 装完了，但拿到的东西不是 GB 游戏
    ok &= case("装完头部错", start, end, rom,
               loader(30, 260, bytes([0xFF] * 8)), "fail", head=bytes([0xFF] * 8))
    print("RESULT %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
