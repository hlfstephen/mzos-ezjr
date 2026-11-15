# 发布流程

这份文档写给维护者：怎么从一次 `git commit` 走到一个别人能直接用的 Release。

---

## 0. 首次推到 GitHub

**新建一个空仓库，不要点 Fork。** 这不是 lukesau/ezgb 的分支，理由见
[docs/11-license-and-upstream.md](docs/11-license-and-upstream.md)。

```sh
# 在 GitHub 上 New repository（空仓库，不勾 README / .gitignore / license）
git remote add origin git@github.com:<你的账号>/ezjr-mini-kernel.git
git push -u origin main
git push origin --tags
```

推完确认三件事：

- 仓库首页语言统计里 Assembly + C 占主体（不是 100% 文档）；
- `LICENSE` 被 GitHub 识别为 **AGPL-3.0**；
- Release 页有最新版的 tag，附件是第 3 节的四样东西。

## 1. 版本号只有一个来源

三处必须一致，改一处就要改三处：

| 位置 | 内容 |
|---|---|
| `src/main.c` 的 `MZOS_VER` | `"V0.16.4"` —— 设置页版本行显示 `MZOS V0.16.4 FW5` |
| `CHANGELOG.md` 顶部小节 | `## v0.16.4 — YYYY-MM-DD` |
| git tag | `v0.16.4`（不带前缀 `MZOS`） |

`build.sh` 会把 `MZOS_VER` 的长度算进版本行位置，所以**不要手改显示宽度**。

版本号规则：`0.x.y` 表示功能仍在演进；`x` 加一表示有真机行为变化，`y` 加一表示修 bug
或只改文档。等 `docs/04-boundaries.md` 里所有 ⚠️ 项都清掉，再考虑 `1.0.0`。

## 2. 发布前检查

```sh
sh tools/verify_all.sh          # 构建 + 静态 + 主机侧 + 模拟器；必须全绿
./build.sh && ./build.sh --diag # 两版都要能编
python3 tools/verify_rom.py build/ezgb.dat
```

看两件事：

1. **段余量**：v0.16.6 起 ROM 是 128 KB / 8 bank，每个功能组一个 bank
   （[docs/12-bank-layout.md](docs/12-bank-layout.md)）。看 `段布局` 那几行：
   `_CODE`（bank0）与 `_BANK1` 各自 < `$4000` / `$8000`，可切 bank 各自 < 16 KB。
   现在正式版 `_CODE` 余 5990、`_BANK1` 余 4327、bank2 余 6853；`--diag` 版只多
   一个 bank4（603 字节）。加功能时先看它要进哪个 bank —— 满了就开 bank6/7，不要
   再往常驻段里塞。
2. **断言数**：`verify_all.sh` 末尾的 OK 计数要与 `docs/03-verification.md` 写的一致
   （v0.16.15 实测 374）。另外 `verify_banklayout.py` 每个可切 bank 都会打印段表 ——
   `_HOME` 必须落在 `$4000-$41FF`、`_CODE` 不越过 `$7000`，任何一个段跑到
   `$Dxxx` 就是「库代码又进 RAM 了」。

## 3. 产物构成

一个 Release 至少要有这四样：

```
ezgb.dat                      正式版（64 KB / 4 bank，装卡用）
EZJR.FNT                      卡上汉字字库（164 KB，与 ezgb.dat 一起放根目录）
diag/ezgb.dat                 自检版（排障用，日常不要装）
restore/ezgb.dat              回退版（上一个 stable 的正式版，v0.14.0）
MD5SUM.txt                    上面四个的校验和
测试步骤.md                    这一版要测什么、怎么判断成功
```

`restore/` 不要放「上一个版本」了事——放**最近一个确认稳定**的版本。v0.12.6/0.12.7
那两版把目录终止符弄丢过，就不能当回退目标。

生成：

```sh
./build.sh            && cp build/ezgb.dat dist/ezgb.dat
./build.sh --diag     && cp build/ezgb.dat dist/diag/ezgb.dat
cp EZJR.FNT dist/
python3 tools/gen_cjk_fnt.py --out dist/EZJR.FNT     # 或从上一版 Release 取
cd dist && md5sum ezgb.dat diag/ezgb.dat restore/ezgb.dat EZJR.FNT > MD5SUM.txt
```

> **Release notes 里必须带上这句**：*请勿使用此项目更新烧录卡的固件，如有损坏，概不负责。*
> 下载页是陌生人唯一会读到的地方，这句话不能只埋在 README 里。`测试步骤.md` 开头也放一份。

## 4. 打 tag 与发布

```sh
git add -A
git commit -m "release: v0.16.4"
git tag -a v0.16.4 -m "v0.16.4: 启动判定改用装载器 busy 信号"
git push origin main --tags

# GitHub Release（gh CLI）
gh release create v0.16.4 dist/*.dat dist/EZJR.FNT dist/MD5SUM.txt dist/测试步骤.md \
  --title "v0.16.4 — 启动判定改用装载器 busy 信号" \
  --notes-file /tmp/release-notes.md
```

Release notes 直接取 `CHANGELOG.md` 里对应小节，**保留「已知问题」**——比只写新功能
有用得多。

## 5. 什么不能进仓库

`.gitignore` 已经拦了：

| 类型 | 原因 |
|---|---|
| `*.dat` / `*.gb` / `*.gbc` / `*.sav` | 官方固件、游戏 ROM、玩家存档，一律不分发 |
| `build/` / `dist/` | 构建产物 |
| `EZJR.FNT` | 生成物（164 KB），从 Release 下载或本地生成 |
| `*.map` / `*.ihx` / `*.lk` / `*.rel` / `*.sym` | 编译器中间产物 |

字体点阵（`src/font_data.h`）**要入库**：它是 OFL 授权的衍生数据，随源码一起分发才符合
「再分发时保留声明」的要求。

## 6. 发布后

- 在 README 顶部更新版本号与截图（截图用 `python3 tools/emu_test.py --shots` 重出）；
- 真机结论回填到 `docs/03-verification.md` 第 4 节，别只写在 CHANGELOG 里；
- 如果这一版有行为变化（例如目录写入策略），在 `docs/07-save.md` / `docs/01-architecture.md`
  同步「为什么」，否则下一版又会有人重新踩。
