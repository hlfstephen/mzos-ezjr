/* 自动生成，请勿手改；由 tools/gen_font.py 生成。 */
/* 中文字符串 = glyph id 序列，0 结尾。文案源见 tools/gen_font.py 的 UI_STRINGS。 */
#ifndef STRINGS_H
#define STRINGS_H

#include <stdint.h>

/* 符号的 tile 编号（= 字符码，可以直接在 C 串里写 "\x01"）*/
#define SYM_ARROW  0x01
#define SYM_HLINE  0x02
#define SYM_HEAVY  0x03
#define SYM_BLOCK  0x04
#define SYM_LOWER8  0x05
#define SYM_UPPER8  0x06
#define SYM_LEFT8  0x07
#define SYM_RIGHT8  0x08
#define SYM_TRI_UP  0x09
#define SYM_TRI_DOWN  0x0A
#define SYM_DOT  0x0B

/* 单字 id（供拼装，顺序即 cjk_font 下标+1）*/
#define CJK_01  1   /* 游 */
#define CJK_02  2   /* 戏 */
#define CJK_03  3   /* 设 */
#define CJK_04  4   /* 置 */
#define CJK_05  5   /* 时 */
#define CJK_06  6   /* 间 */
#define CJK_07  7   /* 自 */
#define CJK_08  8   /* 动 */
#define CJK_09  9   /* 备 */
#define CJK_10  10   /* 份 */
#define CJK_11  11   /* 手 */
#define CJK_12  12   /* 保 */
#define CJK_13  13   /* 存 */
#define CJK_14  14   /* 档 */
#define CJK_15  15   /* 年 */
#define CJK_16  16   /* 月 */
#define CJK_17  17   /* 日 */
#define CJK_18  18   /* 分 */
#define CJK_19  19   /* 秒 */
#define CJK_20  20   /* 星 */
#define CJK_21  21   /* 期 */
#define CJK_22  22   /* 无 */
#define CJK_23  23   /* 已 */
#define CJK_24  24   /* 请 */
#define CJK_25  25   /* 电 */
#define CJK_26  26   /* 池 */
#define CJK_27  27   /* 压 */
#define CJK_28  28   /* 低 */
#define CJK_29  29   /* 储 */
#define CJK_30  30   /* 卡 */
#define CJK_31  31   /* 错 */
#define CJK_32  32   /* 误 */
#define CJK_33  33   /* 读 */
#define CJK_34  34   /* 取 */
#define CJK_35  35   /* 中 */
#define CJK_36  36   /* 个 */
#define CJK_37  37   /* 检 */
#define CJK_38  38   /* 启 */
#define CJK_39  39   /* 失 */
#define CJK_40  40   /* 败 */
#define CJK_41  41   /* 跳 */
#define CJK_42  42   /* 过 */
#define CJK_43  43   /* 正 */
#define CJK_44  44   /* 在 */
#define CJK_45  45   /* 完 */
#define CJK_46  46   /* 成 */
#define CJK_47  47   /* 校 */
#define CJK_48  48   /* 验 */
#define CJK_49  49   /* 空 */
#define CJK_50  50   /* 不 */
#define CJK_51  51   /* 足 */
#define CJK_52  52   /* 目 */
#define CJK_53  53   /* 录 */
#define CJK_54  54   /* 满 */
#define CJK_55  55   /* 未 */
#define CJK_56  56   /* 知 */
#define CJK_57  57   /* 是 */
#define CJK_58  58   /* 文 */
#define CJK_59  59   /* 件 */
#define CJK_60  60   /* 太 */
#define CJK_61  61   /* 碎 */
#define CJK_62  62   /* 损 */
#define CJK_63  63   /* 坏 */
#define CJK_64  64   /* 法 */
#define CJK_65  65   /* 识 */
#define CJK_66  66   /* 别 */
#define CJK_67  67   /* 此 */
#define CJK_68  68   /* 格 */
#define CJK_69  69   /* 式 */
#define CJK_70  70   /* 支 */
#define CJK_71  71   /* 持 */
#define CJK_72  72   /* 区 */
#define CJK_73  73   /* 表 */
#define CJK_74  74   /* 上 */
#define CJK_75  75   /* 没 */
#define CJK_76  76   /* 有 */
#define CJK_77  77   /* 运 */
#define CJK_78  78   /* 行 */
#define CJK_79  79   /* 次 */
#define CJK_80  80   /* 删 */
#define CJK_81  81   /* 除 */
#define CJK_82  82   /* 当 */
#define CJK_83  83   /* 前 */
#define CJK_84  84   /* 能 */
#define CJK_85  85   /* 返 */
#define CJK_86  86   /* 回 */

/* 完整文案。数组放 ROM(const)，元素 1..CJK_COUNT。0 = 串结束。 */
static const uint8_t STR_GAME[] = {1,2, 0};  /* 游戏 — 标签 1 */
static const uint8_t STR_SETTINGS[] = {3,4, 0};  /* 设置 — 标签 2 / 时间条目的按钮 */
static const uint8_t STR_TIME[] = {5,6, 0};  /* 时间 — 设置条目名（提示语里也用到） */
static const uint8_t STR_AUTO_BACKUP[] = {7,8,9,10, 0};  /* 自动备份 — 设置条目名 */
static const uint8_t STR_AUTO[] = {7,8, 0};  /* 自动 — 备份状态：自动 */
static const uint8_t STR_MANUAL[] = {11,8, 0};  /* 手动 — 备份状态：手动 */
static const uint8_t STR_SAVE_BTN[] = {12,13, 0};  /* 保存 — 编辑时的按钮 */
static const uint8_t STR_BACKUP_Q[] = {9,10,13,14, 0};  /* 备份存档 — 手动模式下开机确认 */
static const uint8_t STR_YEAR[] = {15, 0};  /* 年 — 时间字段 */
static const uint8_t STR_MONTH[] = {16, 0};  /* 月 — 时间字段 */
static const uint8_t STR_DAY[] = {17, 0};  /* 日 — 时间字段 */
static const uint8_t STR_HOUR[] = {5, 0};  /* 时 — 时间字段 */
static const uint8_t STR_MINUTE[] = {18, 0};  /* 分 — 时间字段 */
static const uint8_t STR_SECOND[] = {19, 0};  /* 秒 — 时间字段 */
static const uint8_t STR_WEEKDAY[] = {20,21, 0};  /* 星期 — 时间字段 */
/* v0.12.2：「无游戏」这条文案删掉了。列表页不再用一句话宣布"这层没有游戏"
 * —— 纯文件夹层也符合那个判据，真机上就是"明明列着一列目录名，屏幕中央却说
 * 无游戏"（见 main.c draw_list）。字库里那三个字仍然保留，别的文案在用
 * （「游戏」标签、「无上次游戏」），所以 gen_font.py 的字表不用改。*/
static const uint8_t STR_SAVED[] = {23,12,13, 0};  /* 已保存 — 写入 RTC 后 */
static const uint8_t STR_PLEASE_SET[] = {24,3,4,5,6, 0};  /* 请设置时间 — RTC VL 标志 */
static const uint8_t STR_BATTERY_DRY[] = {25,26,25,27,28, 0};  /* 电池电压低 — RTC VL / 存档掉电 */
static const uint8_t STR_SD_ERROR[] = {13,29,30,31,32, 0};  /* 存储卡错误 — 挂载失败 */
static const uint8_t STR_READING[] = {33,34,35, 0};  /* 读取中 — 扫目录 */
static const uint8_t STR_GAMES_UNIT[] = {36,1,2, 0};  /* 个游戏 — 列表计数后缀 */
static const uint8_t STR_SELF_TEST[] = {7,37, 0};  /* 自检 — 调试屏标题（条件编译） */
static const uint8_t STR_LAUNCH_FAIL[] = {38,8,39,40, 0};  /* 启动失败 — 启动出错 */
static const uint8_t STR_BACKUP_BTN[] = {9,10, 0};  /* 备份 — 确认屏 A= 后面的字 */
static const uint8_t STR_SKIP_BTN[] = {41,42, 0};  /* 跳过 — 确认屏 B= 后面的字 */
static const uint8_t STR_BACKING_UP[] = {43,44,9,10, 0};  /* 正在备份 — 备份进度屏 行 8 */
static const uint8_t STR_FINISHED[] = {45,46, 0};  /* 完成 — 备份成功，补在进度屏下一行 */
static const uint8_t STR_BACKUP_FAIL[] = {9,10,39,40, 0};  /* 备份失败 — 备份失败，补在进度屏下一行 */
static const uint8_t STR_ERR_VERIFY[] = {47,48,39,40, 0};  /* 校验失败 — 写完回读比对不一致 */
static const uint8_t STR_ERR_NOSPACE[] = {49,6,50,51, 0};  /* 空间不足 — FAT 上没有空簇 */
static const uint8_t STR_ERR_DIRFULL[] = {52,53,23,54, 0};  /* 目录已满 — 目录项写不进去 */
static const uint8_t STR_ERR_UNKNOWN[] = {55,56,31,32, 0};  /* 未知错误 — 兜底原因 */
static const uint8_t STR_NOT_GB[] = {50,57,1,2,30, 0};  /* 不是游戏卡 — 启动失败：ROM 头没有 Nintendo logo */
static const uint8_t STR_TOO_FRAG[] = {58,59,60,61, 0};  /* 文件太碎 — 启动失败：碎片超过加载命令表容量 */
static const uint8_t STR_BAD_CHAIN[] = {58,59,23,62,63, 0};  /* 文件已损坏 — 启动失败：簇链成环 / 越界 */
static const uint8_t STR_NO_SIG[] = {22,64,65,66,67,30, 0};  /* 无法识别此卡 — 挂载失败：引导扇区没有 55AA 签名 */
static const uint8_t STR_FAT12_MSG[] = {68,69,50,70,71, 0};  /* 格式不支持 — 挂载失败：FAT12 */
static const uint8_t STR_GPT_MSG[] = {18,72,73,50,70,71, 0};  /* 分区表不支持 — 挂载失败：GPT 保护分区 */
static const uint8_t STR_NO_FS[] = {30,74,75,76,18,72, 0};  /* 卡上没有分区 — 挂载失败：结构对但找不到 FAT 引导扇区 */
static const uint8_t STR_LAST_RUN[] = {77,78,74,79,1,2, 0};  /* 运行上次游戏 — 菜单项 1 */
static const uint8_t STR_NO_LAST[] = {22,74,79,1,2, 0};  /* 无上次游戏 — 菜单项 1：还没有启动过任何游戏 */
static const uint8_t STR_DELETE_ROM[] = {80,81,82,83,58,59, 0};  /* 删除当前文件 — 菜单项 2：光标在 ROM 上 */
static const uint8_t STR_CANT_DEL[] = {52,53,50,84,80, 0};  /* 目录不能删 — 菜单项 2：光标在文件夹上（文件夹不删） */
static const uint8_t STR_DELETE_DONE[] = {23,80,81, 0};  /* 已删除 — 删完在底部提示一下 */
static const uint8_t STR_BACK[] = {85,86, 0};  /* 返回 — 菜单底部 B 键提示 */

#endif /* STRINGS_H */
