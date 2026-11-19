#!/bin/bash
# 蓝牙音频端口延迟补偿 = 250ms（解决"播放一卡一卡"的关键）
#
# 为什么需要：PulseAudio 对蓝牙 A2DP sink 的默认缓冲只有 ~40ms，
#   这块板子 WiFi 与蓝牙共用 2.4G 射频，共存时几十毫秒的空口抖动就让 PA 欠载
#   → mpv 日志满屏 "Audio device underrun"，听感就是周期性卡顿。
#   把端口延迟偏置调到 250ms 后 PA 会多缓存，实测 55~60 秒 0 欠载。
#
# 什么时候跑：pactl 的这个设置**不随 PA 重启/蓝牙重连保留**，所以三处都调它：
#   1) bt.py 连接音箱成功后
#   2) pulseaudio-system.service 的 ExecStartPost
#   3) bt-latency.timer 每 30 秒兜底（幂等，重复设置也无害）
export PULSE_SERVER=unix:/var/run/pulse/native
for c in $(pactl list short cards 2>/dev/null | awk '$2 ~ /^bluez_card/ {print $2}'); do
  pactl set-port-latency-offset "$c" speaker-output 250000 >/dev/null 2>&1 || true
done
exit 0
