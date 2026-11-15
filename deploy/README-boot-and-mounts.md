# 服务器开机自启 / 挂载配置（OpenStick「410 棒子」）

本文档记录服务器上**与自启、挂载有关的全部配置**，以及 2026-11-15 的实测结果。
仓库里的 `usb-watchdog.sh` 是配套脚本。

---

## 1. 自启链（重启后全自动）

```
开机
 ├─ NetworkManager → 连 TP-LINK_4225（**静态 IP 192.168.0.116**，autoconnect=yes）
 ├─ /etc/fstab  → mnt-usb.automount（按需挂载 U 盘曲库，nofail）
 ├─ qqmusic.service（FastAPI :3210，enabled）
 ├─ navidrome.service（:4533，enabled，Restart=always，RequiresMountsFor=/mnt/usb）
 ├─ usblinkd.service（USB 串口链路，enabled）
 ├─ wifi-watchdog.timer（OnBootSec=25，每 30s：WiFi 掉了自动重连）
 └─ usb-watchdog.timer（OnBootSec=40，每 30s：曲库盘/ Navidrome 守护）
```

**故意不开机自启**：`ModemManager`（4G）保持 `masked` —— modem 固件会崩溃循环，开着拖累系统。

## 2. fstab 关键改动（2026-11-15）

```fstab
# 现存的两行
PARTUUID=80780b1d-0fe1-27d3-23e4-9244e62f8c46  /boot  ext2  defaults  0 2
UUID=2835-98E8 /mnt/usb exfat-fuse defaults,nofail,x-systemd.automount,x-systemd.device-timeout=10,uid=0,gid=0,umask=022  0 0

# 已注释掉（损坏的 U 盘，0 字节）
# [2026-11-15 移除] 这块 U 盘已损坏（0 字节），它会让开机白等 90 秒
# UUID=F087-CA02 /mnt/tf vfat defaults,nofail,uid=0,gid=0,umask=022 0 0
```

要点：

| 配置 | 为什么 |
|---|---|
| `nofail` | U 盘没插也能正常开机，不会卡住 |
| `x-systemd.automount` | 按需挂载：第一次访问 `/mnt/usb` 时 systemd 自动挂上，比轮询更及时 |
| `x-systemd.device-timeout=10` | 设备没出现时最多等 10 秒，别拖开机 |
| 删掉 `F087-CA02 /mnt/tf` | 这块 U 盘已损坏（0 字节），留着会让开机等 90 秒 |

## 3. 实测结果（2026-11-15 两次重启验证）

| 项目 | 结果 |
|---|---|
| 开机耗时 | **31 秒**（原来 1 分 33 秒，因为白等了坏 U 盘 90 秒） |
| qqmusic / navidrome / usblinkd | ✅ 全部 active + enabled |
| 两个 watchdog.timer | ✅ active + enabled |
| 按需挂载 | ✅ `mnt-usb.automount` active；访问后 `fuseblk` 挂上，1621 个文件 |
| 网络 | ✅ 静态 IP `192.168.0.116` 自动恢复，外网通 |
| Navidrome | ✅ 就绪 3.2 秒，初始扫描 0.75 秒，**启动 1 次、无反复重启** |
| 曲库 | ✅ 956 首，QQ 与 Navidrome 都能正常取歌 |
| 系统错误日志 | ✅ 无（仅有 image 自带的 cloudflared 开机重试，与本项目无关） |

## 4. U 盘掉线的处理（这块板子的老毛病）

- 现象：内核 `Buffer I/O error ... lost async page write` / `device offline error`，
  设备重新枚举（`sda` → `sdb`），`/mnt/usb` 掉挂载
- 影响：Navidrome 取不到歌；下载器写盘会写到根分区（已加拒绝写入保护）
- 现在的兜底：
  1. `x-systemd.automount`：访问时自动重新挂载
  2. `usb-watchdog.timer`：每 30 秒 `ls /mnt/usb/music`，失败则触发挂载并把 Navidrome 重启一次
  3. Navidrome `Restart=always`：即使被 systemd 停掉也会自己起来
- 日志：`/var/log/usb-watchdog.log`、`dmesg | tail`

**注意**：看门狗的"是否挂载"判断曾踩过两次坑（`findmnt -q` 不支持、`mountpoint -q` 误判、
`/proc/mounts` 在 automount 下误判、`findmnt -T` 输出两行），最终只信"目录能不能读"。
细节见 `usb-watchdog.sh` 头部注释，别再改成那些判断方式。

## 5. 其它 drop-in

| 文件 | 作用 |
|---|---|
| `/etc/systemd/system/navidrome.service.d/override.conf` | `RequiresMountsFor=/mnt/usb`（等曲库挂载点） |
| `/etc/systemd/system/navidrome.service.d/restart.conf` | `Restart=always` / `RestartSec=5` |

## 6. 排错命令

```bash
systemd-analyze                    # 开机耗时
systemd-analyze blame | head       # 谁拖慢开机
systemctl is-active qqmusic navidrome usblinkd mnt-usb.automount
findmnt -n -o SOURCE,FSTYPE /mnt/usb
journalctl -u navidrome -b | grep -E 'ready|Finished scanning'
tail -5 /var/log/usb-watchdog.log
```
