#!/bin/bash
# U 盘掉线自动挂回 + Navidrome 守护（服务器 usb-watchdog.timer 每 30 秒调用）
#
# 判定方式：直接**访问** /mnt/usb/music。
#   · fstab 里已加 x-systemd.automount：目录一被访问，systemd 就按需挂上真实 U 盘
#   · 访问成功且有文件 → 健康，什么都不做
#   · 访问失败 → 拉起 automount / 重启挂载单元，恢复后重启 Navidrome 重新扫描
#   · /run/usb-watchdog.broken 记住"刚才坏过"，恢复时只重启一次 Navidrome
#
# ⚠️⚠️ 踩坑记录（2026-11-15，务必保留，别再走回头路）
#   v1 `findmnt -q`：这台机器的 findmnt **不支持 -q**，每次报 invalid option 并返回 1
#      → 每 30 秒误判"盘没挂载" → 反复 mount 叠加挂载 → systemd 认为挂载点变了
#      → **反复把 Navidrome 停掉重启**：24 小时 102 次，曲库扫描被打断，
#        日志满屏 "Scan failed: context canceled"
#   v2 `mountpoint -q`：本环境同样会误判
#   v3 查 /proc/mounts：加了 automount 后会看到 autofs 那行 → 误判"已挂载"，漏检真问题
#   v4 查 `findmnt -T` 的 FSTYPE：这个 findmnt 对同一路径会输出两行（autofs + fuseblk），
#      判断不干净
#   v5（现在）：只信"目录能不能读" —— 最贴近真实可用性，且不依赖任何版本的参数差异。
MOUNT=/mnt/usb
FLAG=/run/usb-watchdog.broken
MSG=""

ok() {
    ls "$MOUNT/music" >/dev/null 2>&1 || return 1
    [ "$(ls "$MOUNT/music" 2>/dev/null | wc -l)" -gt 0 ] || return 1
    return 0
}

if ok; then
    if [ -f "$FLAG" ]; then
        rm -f "$FLAG"
        MSG="U 盘已恢复访问"
        systemctl restart navidrome 2>/dev/null && MSG="$MSG；已重启 Navidrome 重新扫描"
    fi
else
    touch "$FLAG"
    logger -t usb-watchdog "曲库盘不可访问，尝试恢复"
    systemctl start mnt-usb.automount 2>/dev/null
    sleep 1
    ok || { systemctl restart mnt-usb.mount 2>/dev/null; sleep 2; }
    if ok; then
        rm -f "$FLAG"
        MSG="已恢复挂载，重启 Navidrome 重新扫描"
        systemctl restart navidrome 2>/dev/null || true
    else
        MSG="恢复失败（U 盘没插好？）"
    fi
fi

if ! systemctl is-active --quiet navidrome; then
    logger -t usb-watchdog "Navidrome 未运行，拉起"
    systemctl start navidrome 2>/dev/null || true
    MSG="${MSG:+$MSG；}已拉起 Navidrome"
fi

[ -n "$MSG" ] && echo "$(date '+%F %T') $MSG" >> /var/log/usb-watchdog.log
exit 0
