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
 * fat.c / fatw.c 编在 BANK1，本文件不传 --codeseg，落在 _CODE。
 */
#include <string.h>
#include "hw.h"
#include "fpga.h"
#include "fat.h"

extern uint8_t fbuf[512];
extern uint8_t fmount_ok;

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
