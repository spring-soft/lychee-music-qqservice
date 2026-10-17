# 部署说明（这台 OpenStick 的实际情况，不是 Docker）

> 仓库里的 `Dockerfile` / `docker-compose.yml` 是**上游模板**，这台机器上没用
> （机器上根本没装 docker/podman）。实际是 **裸机 + systemd + venv**。

## 目录

```
/opt/qqmusic/
├── app.py            # QQ 契约层（/v1/*）
├── player.py         # ★ 服务器端播放器（mpv 子进程 + /v1/player/*）
├── requirements.txt
├── venv/             # python3 -m venv，装在本地
├── www/index.html    # 网页控制台（http://<机器>:3210/）
└── data/             # 600 权限，**不进仓库**
    ├── credential.json   # QQ 扫码凭据（会过期，用 /v1/login/qr 重新扫）
    ├── player.json       # 声卡 + 音量 + Navidrome 账号
    └── mpv.sock / mpv.log
```

`data/player.json` 长这样：

```json
{"device": "alsa/hw:CARD=ECHOA,DEV=0",
 "volume": 80,
 "nav": {"host": "http://127.0.0.1:4533", "user": "...", "password": "..."}}
```

## 一次性准备（换机器要重做）

```bash
apt-get install -y --no-install-recommends mpv     # 实测 15 个新包、0 升级 0 卸载
usermod -aG audio qqmusic                          # 放音需要 /dev/snd
mkdir -p /opt/qqmusic/www && chown -R qqmusic:qqmusic /opt/qqmusic
cp deploy/qqmusic.service /etc/systemd/system/
mkdir -p /etc/systemd/system/qqmusic.service.d
cp deploy/qqmusic.service.d-memory.conf /etc/systemd/system/qqmusic.service.d/memory.conf
systemctl daemon-reload && systemctl enable --now qqmusic
```

## 为什么内存上限是 160M/200M（`deploy/qqmusic.service.d-memory.conf`）

原单元文件里是 `MemoryHigh=120M / MemoryMax=150M`，那是 2026-10-12 定的
**在还没有播放能力时**的实测值（峰值 68M）。加了 mpv 之后：

- 常态 cgroup：anon 72M + 页缓存 40M ≈ 119M，`memory.events` 里 `high` 被顶了 76 次（一直限流）
- 放歌时 mpv 还要吃解码缓冲，120M 肯定不够

所以用 drop-in 放宽到 `High=160M / Max=200M`：正常播放摸不到 High，
而 200M 硬上限保证最坏情况只杀本服务，仍给系统（sshd/热点）留 ~176MB。

## 部署（在开发机上）

```bash
scp app.py player.py  root@<机器>:/opt/qqmusic/
scp www/index.html    root@<机器>:/opt/qqmusic/www/
ssh root@<机器> 'chown -R qqmusic:qqmusic /opt/qqmusic && systemctl restart qqmusic'
```

⚠️ 启动约 **10 秒**（要拉起 mpv 再 ping Navidrome），别用 `sleep 3` 判活。

## 验收

```bash
curl -s localhost:3210/v1/health | head -c 200       # QQ 凭据 + 档位链
curl -s localhost:3210/v1/player/status              # 播放器状态（mpv_ok 必须 true）
curl -s 'localhost:3210/v1/player/play?source=qq&mode=fav&index=0&limit=5'
curl -s 'localhost:3210/v1/player/play?source=nav&mode=starred&index=0&limit=3'
cat /proc/asound/card0/pcm0p/sub0/hw_params          # 声卡实际格式/采样率
```

浏览器打开 `http://<机器>:3210/` 就是网页控制台。

## 已知限制

- **没有鉴权**（跟 ESP 的控制台一样）。同网段任何人都能控制播放，别把这个端口暴露到公网。
- 换歌不是真无缝：队列在 Python 这边，一首一首 `loadfile`，代价是 ~0.1s 的缝
  （因为 QQ 的 vkey 只有 2 小时，不能提前把 60 首的地址全解出来）。
- `data/mpv.log` 每次启动会重写；日志级别是 `warn`，只留警告/错误。
