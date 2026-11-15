# QQ 收藏 → Navidrome 曲库 同步指南

> 把 QQ 音乐「收藏」里 **Navidrome 曲库还没有的歌**批量下载进曲库：
> **尽量取 SQ 无损**（绿钻上限 SQ；没有 SQ 时按 320k → 128k 退档）、**补内嵌封面**、
> **写 UTF-8 歌词 `.lrc`**，并自动让 Navidrome 收录。
>
> 本文档对应 2026-11-15 第一次完整同步（933 首收藏）的全过程，脚本已经固化，
> 以后一条命令即可重复。

---

## 一条命令

```bash
# 只看要下多少（不下载）
/opt/qqmusic/venv/bin/python /opt/qqmusic/sync_fav.py --dry-run

# 真正同步（默认 2 轮重试）
/opt/qqmusic/venv/bin/python /opt/qqmusic/sync_fav.py

# 常用变体
sync-fav                              # 包装脚本（等价于上面的真同步）
sync-fav --dry-run                    # 只统计
sync-fav --passes 3 --batch 30        # 超时多就多几轮/每批小一点
sync-fav --no-lyrics --no-check       # 只要歌，不补词、不质检
sync-fav --limit 20                   # 只处理前 20 首（调试用）
```

脚本位置：`/opt/qqmusic/sync_fav.py`（仓库里是
`server/_scratch/ref/qqservice/tools/sync_fav.py`）。

---

## 它做了什么（7 步）

| 步骤 | 说明 |
|---|---|
| 1. 读收藏 | `/v1/list?mode=fav&limit=100&page=N` 分页拉全（932 首要 10 页），每页带重试 |
| 2. 比对曲库 | 直接读 Navidrome sqlite（只读）建索引：**歌名归一化相同 +（歌手有交集 或 时长差≤3s）** 视为已有 |
| 3. 批量下载 | 缺的按批（默认 40 首）提交 `/v1/download/add`，等队列排空再下一批（避免 429、也方便读回执） |
| 4. 多轮重试 | QQ 接口偶发 `TimeoutNetworkError`，第 2/3 轮基本都能补上 |
| 5. 补歌词 | 扫 `QQ下载/` 里缺 `.lrc` 的音频，按文件名反查收藏里的 mid → `/v1/lyric` → 写 UTF-8 无 BOM |
| 6. 质检 | ffprobe 检查每首：是否有**内嵌封面**（video/attached_pic 流）、时长是否正常；统计歌词覆盖 |
| 7. 出报告 | `/opt/qqmusic/data/sync_report.json`（全量）+ `sync_noauth.json`（付费单曲清单） |

下载器自身（`download.py`）负责：取最高可用音质 → ffmpeg remux 补封面+标签 → sudo helper
装进 `/mnt/usb/music/QQ下载/` → 写 `.lrc` → 队列排空后触发 Navidrome 扫描。
Navidrome 约 1 分钟内自动收录（脚本结束前会触发一次 `startScan`）。

---

## 2026-11-15 本次同步记录

| 项目 | 数值 |
|---|---|
| QQ 收藏总数 | **933 首** |
| 同步前 Navidrome | 813 首 |
| 同步后 Navidrome | **956 首**（新增 143） |
| 本次下载的音质 | **SQ 无损 flac 130 首** + HQ/最高可用 mp3 26 首 |
| 封面 | 154/156 有内嵌封面（2 首 QQ 本身无封面） |
| 歌词 | 149/156 有 `.lrc`（7 首 QQ 本身无歌词，已确认） |
| 文件完整性 | 全部 ffprobe 通过，时长异常 0 |
| 无法下载 | **21 首付费单曲**（QQ 返回 `result=104003 没有播放权限`，绿钻等级不够）→ 清单在 `data/sync_noauth.json` |

本次实际耗时约 1 小时（受 QQ 接口超时、U 盘掉线重挂影响；稳定环境下 150 首约 30 分钟）。

### 本次顺手修掉的问题（都已提交）

1. **装盘校验路径写错**（`MUSIC_DIR/name` 应为 `DL_DIR/name`）→ 每首都误报"装进曲库失败"
   且**歌词步骤被跳过**（表现：下了歌却没歌词）。修复后补回了 39 首歌词。
2. **曲库盘掉线保护**：`/mnt/usb` 未挂载时下载器拒绝写入（否则会写进 3.5G 根分区、把系统撑爆；
   Navidrome 还会扫到空目录）。新增等待 60 秒 + 明确失败。
3. **`usb-watchdog.timer`**（每 30 秒）：U 盘掉线自动 `mount` 回来并重启 Navidrome；
   Navidrome 不在跑也拉起。日志 `/var/log/usb-watchdog.log`。

---

## 坑清单（下次遇到别慌）

| 现象 | 原因 / 处理 |
|---|---|
| 下载卡住不动 | 看 `dmesg \| tail`：`Buffer I/O error / device offline` = **U 盘掉线**（供电/接触）。换供电更强的 hub / 直插板子；看门狗会自动挂回，脚本会跳过并继续 |
| 一批里很多"失败: TimeoutNetworkError" | QQ 接口超时，**再跑一轮**（`sync-fav --passes 1`）基本能补上 |
| "失败: 拿不到播放地址 result=104003" | **付费单曲/数字专辑**，绿钻无权限。无解，等会员升级或单曲购买；清单见 `data/sync_noauth.json` |
| 下了歌但没歌词 | 先确认是 QQ 真没歌词（`/v1/lyric?id=` 返回空）还是装盘失败导致歌词被跳过（看 `journalctl -u qqmusic`）。脚本第 5 步会补 |
| 曲库"变空"/取不到歌 | `/mnt/usb` 没挂载。`findmnt /mnt/usb` 检查；`systemctl start mnt-usb.mount`；看 `usb-watchdog.log` |
| 收藏只拉到 0 首 | QQ 服务刚重启或凭据过期。`curl 127.0.0.1:3210/v1/health` 看凭据；脚本会明确报错退出而不是误判"没有缺歌" |
| 重跑会不会重复下载 | 不会。第 2 步按 歌名+歌手+时长 比对，已入库的会直接跳过（日志里的"已存在"） |

## 排错命令

```bash
curl -s 127.0.0.1:3210/v1/health | head -c 200        # QQ 凭据/服务
curl -s 127.0.0.1:3210/v1/download/list | head -c 400 # 下载队列与最近任务
journalctl -u qqmusic -n 50 --no-pager | grep -E '下载|装进曲库|歌词'
findmnt /mnt/usb && df -h /mnt/usb                    # 曲库盘
tail -5 /var/log/usb-watchdog.log                     # U 盘看门狗
python3 -c 'import sqlite3;print(sqlite3.connect("file:/var/lib/navidrome/navidrome.db?mode=ro",uri=True).execute("select count(*) from media_file").fetchone())'
```

## 报告文件

- `/opt/qqmusic/data/sync_report.json`：每次同步的全量结果（收藏数、下载/已有/失败、歌词、质检、耗时）
- `/opt/qqmusic/data/sync_noauth.json`：付费单曲清单（以后买了会员再跑一次即可补）
- `/tmp/sync_fav.log`（如果重定向过）：运行日志

## 以后补下载付费单曲

等会员等级变化后直接再跑一次：

```bash
sync-fav
```

它会重新比对：**已入库的自动跳过**，只尝试仍缺的那些（含上次的付费单曲）。
