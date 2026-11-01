#!/bin/bash
# 用 HTTP Date 头校准系统时间（这台 OpenStick 没有 RTC 电池、NTP 又起不来）。
# 由 timesync.timer 在开机和每 10 分钟调用；偏差 > 5 秒才改时间。
set -u
LOG_TAG="timesync"
real=""
for u in https://www.baidu.com https://www.qq.com https://www.aliyun.com https://www.cloudflare.com; do
    d=$(curl -sI --max-time 6 "$u" 2>/dev/null | awk -F': ' 'tolower($1)=="date"{print $2}' | tr -d '\r')
    if [ -n "$d" ]; then real="$d"; break; fi
done
if [ -z "$real" ]; then
    logger -t "$LOG_TAG" "拿不到外部时间，跳过"
    exit 0
fi
epoch=$(date -u -d "$real" +%s 2>/dev/null)
if [ -z "$epoch" ]; then
    logger -t "$LOG_TAG" "解析时间失败: $real"
    exit 0
fi
now=$(date -u +%s)
skew=$(( epoch - now ))
if [ "$skew" -lt 0 ]; then abs=$(( -skew )); else abs=$skew; fi
if [ "$abs" -gt 5 ]; then
    if date -u -s "@$epoch" >/dev/null 2>&1; then
        logger -t "$LOG_TAG" "系统时间已校准，偏差 $skew 秒，现在 $(date -u)"
        # 时间跳变后立刻让凭据巡检跑一次（否则可能要等 30 分钟才续期）
        systemctl start credrefresh.service >/dev/null 2>&1 || true
    else
        logger -t "$LOG_TAG" "设置时间失败（需要 root）"
    fi
fi
exit 0
