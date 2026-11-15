#!/bin/bash
# U 盘掉线自动挂回 + Navidrome 守护（服务器 usb-watchdog.timer 每 30 秒调用）
#
# ⚠️⚠️ 踩坑记录（2026-11-15，都是实测事故，别删这段说明）
#
# 1) 第一版用 `findmnt -q /mnt/usb` 判断挂载 —— 这台机器的 findmnt **不支持 -q**，
#    每次报 "invalid option -- 'q'" 并返回 1，于是看门狗每 30 秒都误判"盘没挂载"
#    → 反复 `mount /mnt/usb`（叠加挂载）→ systemd 认为 /mnt/usb 的挂载变了
#    → **每次把 Navidrome 停掉重启**。24 小时里 Navidrome 重启了 102 次，
#    曲库扫描被反复打断，日志满屏 "Scan failed: context canceled"。
#
# 2) 第二版改用 `mountpoint -q`，同样不稳（该环境下也会误判）。
#
# 现在：**只看 /proc/mounts 里有没有 " /mnt/usb "** —— 不依赖任何额外命令，
# 并且**只在真的没挂载时才 mount**，从根上避免叠加挂载。
#
# 另：这块 OpenStick 的 U 盘会真的掉线（供电/接触，内核有 Buffer I/O error +
#    重新枚举 sda→sdb）。真掉线时这个脚本负责挂回并重启 Navidrome 重新扫描；
#    排查看 /var/log/usb-watchdog.log 和 dmesg。

MOUNT=/mnt/usb
MSG=""

if ! grep -q " $MOUNT " /proc/mounts; then
    logger -t usb-watchdog "U 盘未挂载，尝试重新挂载"
    mount "$MOUNT" 2>/dev/null || systemctl restart mnt-usb.mount 2>/dev/null
    sleep 3
    if grep -q " $MOUNT " /proc/mounts; then
        MSG="已重新挂载 U 盘"
        systemctl restart navidrome 2>/dev/null && MSG="$MSG；已重启 Navidrome 重新扫描"
    else
        MSG="重挂失败（U 盘没插好？）"
    fi
fi

if ! systemctl is-active --quiet navidrome; then
    logger -t usb-watchdog "Navidrome 未运行，拉起"
    systemctl start navidrome 2>/dev/null
    MSG="$MSG；已拉起 Navidrome"
fi

[ -n "$MSG" ] && echo "$(date '+%F %T') $MSG" >> /var/log/usb-watchdog.log
exit 0
