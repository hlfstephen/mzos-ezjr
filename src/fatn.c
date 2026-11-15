/* fatn.c — 按 8.3 短名回扫目录、取 ROM 的**完整**长文件名（编在 _CODE）
 *
 * 为什么单独一个文件：这个函数是「存档必须跟 ROM 文件同名」这条硬要求的实现
 * 主体，它有近一千字节，而 BANK1（fat.c / fatw.c / save.c）在 v0.12.0 已经
 * 顶到 $82D5 —— 超出 32KB ROM 的 $8000 上限 725 字节。_CODE 还剩一千多字节，
 * 把它放过来刚好补上。
 *
 * 为什么不干脆省掉这次回扫：列表扫描（fat.c 的 lfn_take）把长名截到
 * FAT_LFN_CHARS-1 = 27 个字符，因为菜单一行只画 18 列。可中文 ROM 名常有
 * 三四十个字符（「口袋妖怪 水晶 金手指v3+全道具251+NPC加强版2.0.gbc」就是
 * 35 个），一截断，写出来的 .sav 长名跟 ROM 名就不再逐字相同，卡上的加载器
 * 按名字配不上对，玩家就会看到「提示备份成功但找不到文件」。所以备份前必须
 * 回目录里把整条长名原样取回来。
 *
 * 依赖：dir_chain() / entry_kind()（src/fat.c，同一套判据）与 fbuf。
 * 本文件按 tools/banks.conf 归 bank1，build.sh 用 --codeseg BANK1 编进常驻段。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"

extern uint8_t fbuf[512];
extern uint32_t fat_cwd;
extern uint8_t fmount_ok;

/* 读档时按**长名**认下来的那条 .SAV 的 8.3 短名（"SLZ2~1.SAV" 之类），以及它的
 * 字节数。★ v0.16.6 起定义在这里（原来的 save.c 已编进 bank3）：这俩是 RAM 变量，
 * 必须由常驻段分配 —— 否则常驻的 fatn.c 引用它们时，地址落在可切 bank 的
 * _DATA 区，主链接拿不到定义。语义上本来也是 fatn 的产出（fat_find_sav_lfn 填），
 * save 只是消费者。*/
char     g_sav_adopt[13];
uint32_t g_sav_size;

static uint16_t n_ld16(const uint8_t *p) { return p[0] | (uint16_t)p[1] << 8; }

/* 一条 LFN 项里的第 j 个字符（j = 0..12）。13 个 UCS-2 不是连着放的：
 *   槽 0-4   -> 偏移 1,3,5,7,9
 *   槽 5-10  -> 偏移 14,16,18,20,22,24
 *   槽 11-12 -> 偏移 28,30
 * 包成一个函数，调用点就不用写三段循环，也就不会漏掉任何一段的边界检查。*/
static uint16_t lfn_slot(const uint8_t *e, uint8_t j)
{
    if (j < 5)  return n_ld16(e + 1 + (uint16_t)j * 2);
    if (j < 11) return n_ld16(e + 14 + (uint16_t)(j - 5) * 2);
    return n_ld16(e + 28 + (uint16_t)(j - 11) * 2);
}

/* 在 dir（0 = 根目录）里按完整 8.3 名 name83 找一个 ROM，把它的**完整**长名
 * 读进 out（最多 max-1 个 UCS-2 + 0），返回字符数；没找到 / 没长名返回 0。
 *
 * 长名项按序号倒序排在短名项前面，每条带自己的序号；这里就地按序号把字符摆到
 * out 的正确位置（不另开大缓冲，out 就是调用方传进来的 g_lfn_buf）。
 * 出口还原 fat_cwd：调用方（save_boot）的当前目录还是根目录，不能被这次查找改掉。*/
#define LFN_LIM (FAT_LFN_FULL - 1)      /* 最多装 47 个字符，第 48 个留给 0 */

uint8_t fat_full_lfn(uint32_t dir, const char *name83, uint16_t *out)
{
    uint8_t  secs, i, have = 0, n = 0, j;
    uint16_t k;                     /* 必须 16 位：k 要走到 512，uint8_t 会绕回 0 */
    uint32_t chain[16], saved = fat_cwd;
    uint8_t  raw[11];

    out[0] = 0;
    if (!fmount_ok || !name83[0])
        return 0;
    /* "POKEMON.GBC" -> 11 字节、空格补齐的目录项原始短名 */
    for (j = 0; j < 11; j++) raw[j] = ' ';
    for (j = 0; j < 8 && name83[j] && name83[j] != '.'; j++)
        raw[j] = (uint8_t)name83[j];
    while (name83[j] && name83[j] != '.') j++;
    if (name83[j] == '.') {
        j++;
        for (n = 8; n < 11 && name83[j]; n++, j++)
            raw[n] = (uint8_t)name83[j];
    }
    n = 0;

    fat_cwd = dir;
    secs = dir_chain(fat_cwd, chain, 16);
    for (i = 0; i < secs; i++) {
        if (n) break;                       /* 已经命中，别再多读扇区 */
        sd_read(chain[i], fbuf);
        for (k = 0; k < 512; k += 32) {
            const uint8_t *e = fbuf + k;
            uint8_t kind;

            if (e[0] == 0 || e[0] == 0xE5) { have = 0; continue; }
            if ((e[11] & 0x3F) == 0x0F) {        /* LFN 项 */
                uint8_t seq = (uint8_t)(e[0] & 0x1F);
                uint8_t b;

                if (seq == 0 || seq > 9) { have = 0; continue; }
                /* 序号 1 是第一段（字符 0-12），序号 2 是 13-25 …… 物理顺序
                 * 是倒的，所以只能按序号摆回去，不能顺序累加。*/
                b = (uint8_t)((seq - 1) * 13);
                /* 缓冲只有 max 个字符，超出的段落整段丢掉 —— 早先这里没判边界，
                 * 序号大的项会写到 out[max] 以后，把栈上的循环变量踩掉，
                 * 表现就是备份时死循环（真机上则是写出一份来路不明的长名）。*/
                if (b >= LFN_LIM) continue;
                if (!have) { memset(out, 0, FAT_LFN_FULL * 2); have = 1; }
                for (j = 0; j < 13; j++) {
                    uint8_t p = (uint8_t)(b + j);
                    if (p >= LFN_LIM) break;
                    out[p] = lfn_slot(e, j);
                }
                continue;
            }
            kind = entry_kind(e);
            if (!kind) { have = 0; continue; }
            if (memcmp(e, raw, 11) == 0) {        /* 命中目标短名项 */
                if (have) {
                    for (n = 0; n < LFN_LIM && out[n]; n++) { }
                    out[n] = 0;
                }
                break;
            }
            have = 0;
        }
    }
    fat_cwd = saved;
    return n;
}

/* 扫 SAVER 目录，找「长名 == g_lfn_buf（已换成 .sav，长 tlen）且扩展名 SAV」。
 *
 * 长名项的 13 个字符分三段摆、序号倒序（物理最靠前那条带 $40，装的是最后一段）。
 * 这里按序号摆回去逐字比。为什么不先拼成一条串：_DATA 已经没地方放第二个
 * 96 字节缓冲了，而 g_lfn_buf 里放的就是要比的目标名。
 *
 * ★ 全用 fbuf[k + o] 直接下标，不写 `uint8_t *e = fbuf + k`：工程里所有指针都是
 *   3 字节 generic 指针，每次 e[o] 都要 call __gptrget。这一个写法省下几百字节，
 *   而 _CODE 与 _BANK1 加起来只剩不到 900。
 */
static const uint8_t sav11[11] = {'S','A','V','E','R',' ',' ',' ',' ',' ',' '};
static const uint8_t lfn_o[13] = {1,3,5,7,9,14,16,18,20,22,24,28,30};
static const uint8_t lfn_b[5]  = {0,0,13,26,39};        /* 下标是序号，最多 4 段 */

/* 在**当前目录**（fat_cwd）里按 8.3 全名找一个子目录，返回首簇；没有返回 0。
 *
 * ★ v0.16.13：这里必须用本文件自己的实现，**不能**调 fatw.c 的 fat_find_dir()。
 *   那个函数在 bank2，调用要走桩；桩把 $4000 窗口换成 bank2，而 sav11 是
 *   **本文件（bank1）_BANK1 段**里的 const 数组 —— 切过去之后 raw11 指向的
 *   11 个字节变成 bank2 的代码，memcmp 永远不相等。
 *   真机表现（v0.16.12）：SAVER 目录明明在卡上，内核说没有 —— 读档读不出来，
 *   备份写完回读校验也找不到自己刚写的档，屏幕报「备份失败/校验失败」。
 *   这类「跨 bank 传本 bank 的 ROM 指针」现在由 tools/verify_bankptr.py 抓。
 *
 * 用 dir_chain（bank0）而不是 dir_nth（bank2）：前者常驻段、直接调用安全。*/
static uint32_t n_find_dir(const uint8_t *raw11)
{
    /* 16 个扇区 = 256 个目录项：根目录塞满 ROM 时 SAVER 也可能排到很后面，
     * 只看头 4 个扇区（64 项）会漏 —— 那正是「卡上有档却读不到」的另一种成因。
     * 栈有 4 KB 余量（SP=$E000，_DATA 到 $CEAF），64 字节数组不心疼。*/
    uint32_t chain[16];
    uint8_t secs, i;
    uint16_t k;

    secs = dir_chain(fat_cwd, chain, 16);
    for (i = 0; i < secs; i++) {
        sd_read(chain[i], fbuf);
        for (k = 0; k < 512; k = (uint16_t)(k + 32)) {
            if (!fbuf[k]) return 0;
            if (fbuf[k] == 0xE5) continue;
            if ((fbuf[k + 11] & 0x3F) == 0x0F) continue;    /* 长名项 */
            if ((fbuf[k + 11] & 0x10) && !memcmp(fbuf + k, raw11, 11)) {
                uint32_t c = (uint32_t)n_ld16(fbuf + k + 26) |
                             ((uint32_t)n_ld16(fbuf + k + 20) << 16);
                return c >= 2 ? c : 0;
            }
        }
    }
    return 0;
}

static uint8_t sav_scan(FatFile *f, uint8_t tlen)
{
    uint8_t secs, i, j, ok = 0, last = 0, seq, base, x, y;
    uint16_t k, o, q;
    /* ★ 16 个扇区 = 256 个目录项 = 64 个存档（每个档 3 条 LFN + 1 条短名）。
     * v0.16.10 及以前只扫前 4 个扇区（16 个档）：SAVER 攒满之后，排在后面的
     * 档永远找不到，而屏幕上一个字都不提示 —— 玩家看到的就是「存档没了」。
     * 真机备份出来的档走第一档（哈希短名），不经过这里；这里专门认从电脑
     * 拷回来的档，而那些档恰恰容易排在目录末尾（最后才拷进去）。*/
    uint32_t chain[16];

    secs = dir_chain(fat_cwd, chain, 16);
    for (i = 0; i < secs; i++) {
        sd_read(chain[i], fbuf);
        for (k = 0; k < 512; k += 32) {
            if (!fbuf[k])
                return 0;                       /* 目录到此结束 */
            if (fbuf[k] == 0xE5) { ok = 0; continue; }
            if ((fbuf[k + 11] & 0x3F) == 0x0F) {        /* 长名项 */
                seq = (uint8_t)(fbuf[k] & 0x1F);
                if (fbuf[k] & 0x40)
                    ok = 1;                     /* 一串长名的头一条 */
                else if (!ok || seq + 1 != last) {
                    ok = 0;
                    continue;                   /* 跳号 = 缺了一段字符，不算数 */
                }
                if (!seq || seq > 4) { ok = 0; continue; }
                last = seq;
                base = lfn_b[seq];
                for (j = 0; j < 13; j++) {
                    /* 期望的两个字节：名字之内取目标字符，正好到头是 $00 收尾，
                     * 再往后是 $FFFF 填充。三个分支收敛到一处比较，省一份代码。*/
                    if ((uint8_t)(base + j) < tlen)
                        q = g_lfn_buf[base + j];
                    else
                        q = (uint16_t)((uint8_t)(base + j) == tlen ? 0x0000 : 0xFFFF);
                    o = (uint16_t)(k + lfn_o[j]);
                    /* ASCII 部分不分大小写：卡上的名字是别的系统写的，
                     * 「.SAV / .sav」「K 口袋 / k 口袋」都可能。汉字是 UCS-2
                     * 双字节，>= $80 的高字节不动，不会被这里改写。*/
                    x = fbuf[o];
                    y = (uint8_t)q;
                    if (x >= 'a' && x <= 'z') x = (uint8_t)(x - 32);
                    if (y >= 'a' && y <= 'z') y = (uint8_t)(y - 32);
                    if (x != y || fbuf[o + 1] != (uint8_t)(q >> 8)) {
                        ok = 0;
                        break;
                    }
                }
                continue;
            }
            /* 短名项：长名串到此为止 */
            if (ok && !(fbuf[k + 11] & 0x18) && fbuf[k + 8] == 'S' &&
                fbuf[k + 9] == 'A' && fbuf[k + 10] == 'V') {
                f->cluster = (uint32_t)fbuf[k + 26] | ((uint32_t)fbuf[k + 27] << 8) |
                             ((uint32_t)fbuf[k + 20] << 16) | ((uint32_t)fbuf[k + 21] << 24);
                /* 大小交给调用方验：它才知道这个游戏该是多少 KB。不验就会把
                 * 半截档（拷了一半、或别的游戏改过名）当成完整的读进去。*/
                g_sav_size = (uint32_t)fbuf[k + 28] | ((uint32_t)fbuf[k + 29] << 8) |
                             ((uint32_t)fbuf[k + 30] << 16) | ((uint32_t)fbuf[k + 31] << 24);
                f->sec_in = 0;
                for (j = 0; j < 8 && fbuf[k + j] != ' '; j++)
                    g_sav_adopt[j] = (char)fbuf[k + j];
                g_sav_adopt[j]     = '.';
                g_sav_adopt[j + 1] = (char)fbuf[k + 8];
                g_sav_adopt[j + 2] = (char)fbuf[k + 9];
                g_sav_adopt[j + 3] = (char)fbuf[k + 10];
                g_sav_adopt[j + 4] = 0;
                return 1;
            }
            ok = 0;
        }
    }
    return 0;
}

/* 按长名在卡上找同名的 .sav —— 只找根目录的 SAVER/，一处。
 *
 * 存档位置只有这一个：本内核备份出去的档在 SAVER/，玩家从电脑拷回来的档也
 * 放进 SAVER/。v0.16.11 曾加过「SAVER 没命中再扫一遍 ROM 所在目录」的兜底，
 * 那是误以为模拟器默认把 .sav 放在 ROM 旁边 —— 真机用法不是这样，v0.16.14
 * 已撤回。多扫一处除了拖慢「找不到」这条路径，还可能在别的目录里认到一条
 * 同名档，让备份写错文件。
 *
 * lfn 会被 fat_sav_lfn 就地换成「….sav」，所以调用方必须先用原名算完 sav_ident。*/
uint8_t fat_find_sav_lfn(FatFile *f)
{
    uint32_t dir0 = fat_cwd, sv;
    uint8_t tlen, found = 0;

    if (!fmount_ok || !g_lfn_buf[0])
        return FAT_ERR_NO_FS;
    tlen = fat_sav_lfn(g_lfn_buf, g_lfn_buf);       /* 就地换成「….sav」*/

    fat_cwd = 0;                                /* SAVER 固定在根目录 */
    sv = n_find_dir(sav11);
    if (sv) {
        fat_dir_heal(sv);
        fat_cwd = sv;
        found = sav_scan(f, tlen);
    }
    fat_cwd = dir0;
    return found ? FAT_OK : FAT_ERR_NO_FS;
}
