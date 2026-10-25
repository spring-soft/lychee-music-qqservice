# 公网面：Subsonic /rest + 网页播放器（2026-10-25）

这是 QQ 音乐契约层的**第二条线**，和 3210 的 ESP 契约层完全独立。

```
  ESP32-S3 ──内网──> 192.168.0.116:3210   /v1/*      无鉴权，只给 ESP（绝不上公网）
  浏览器/客户端 ──Cloudflare Tunnel──> 192.168.0.116:3211
                                        ├── /            网页播放器（浏览器本地出声）
                                        └── /rest/*      Subsonic / OpenSubsonic 接口
  服务器 USB DAC ── mpv ── player.py    仍是 3210 那条线，和这里无关
```

## 文件

| 文件 | 作用 |
|---|---|
| webapp.py | 3211 的入口：页面 + /login 、/logout + 挂 /rest |
| subsonic_api.py | Subsonic 协议层（响应封装、认证、端点映射） |
| websession.py | 网页登录 cookie（HMAC 签名，HttpOnly） |
| wwwweb/ | 网页播放器（index.html / app.js / app.css） |
| deploy/qqweb.service | systemd 单元（端口 3211，MemoryMax=120M） |
| _rest_selftest.py | 服务器上的协议自检（17 项） |
| data/web.json | **不进仓库**，权限 600：{"user":"...","password":"...","star_write":true} |

## 部署

```bash
mkdir -p /opt/qqmusic/wwwweb
# 上传 webapp.py subsonic_api.py websession.py wwwweb/* 到 /opt/qqmusic/
cp deploy/qqweb.service /etc/systemd/system/
cat > /opt/qqmusic/data/web.json <<'JSON'
{"user": "665858hjn", "password": "<强密码>", "star_write": true}
JSON
chmod 600 /opt/qqmusic/data/web.json
chown -R qqmusic:qqmusic /opt/qqmusic
systemctl daemon-reload && systemctl enable --now qqweb
```

验证：

```bash
curl -s http://127.0.0.1:3211/healthz
cd /opt/qqmusic && ./venv/bin/python _rest_selftest.py   # 应全部 PASS
```

## 公网暴露（Cloudflare Tunnel）

在**另一台内网设备**上跑 cloudflared，入站指向 http://192.168.0.116:3211 ：

```yaml
ingress:
  - hostname: music.<域名>
    service: http://192.168.0.116:3211
  - service: http_status:404
```

⚠️ **绝对不要**给 3210 加任何隧道规则：那条线没有鉴权，/v1/login/import、
/v1/download、/v1/player 全裸。

TLS 由 Cloudflare 终止；本服务只讲 HTTP。因为音频是 302 到 QQ CDN，
**不经过 Cloudflare**，所以不受 CF 免费版 100 秒超时/流量限制影响。

## 支持的端点

ping / getLicense / getMusicFolders / getIndexes / getArtists（空）/ getAlbumList2（空）
search3（=search2=search）/ getSong / getStarred2 / getRandomSongs
getPlaylists / getPlaylist（**6 个 QQ 模式 = 6 个歌单**：fav/daily/million/newrec/guess/radar）
stream / download（302 → QQ CDN，https）/ getCoverArt（服务器代理小图）
getLyricsBySongId / star / scrobble / getNowPlaying / getScanStatus
getOpenSubsonicExtensions / getPlayQueue / savePlayQueue
未实现的端点返回 status=ok 的空对象（不报错，客户端退化）。

## 已知限制（实测得出的，不是猜）

1. **取消收藏做不到**：QQ 上游对 dirId=201 的删除静默忽略（app.py 里 2026-10-11
   穷举过 10 种写法）。star 真写回 + 回读总数校验；unstar 明确返回 Subsonic
   error code=30，不假装成功。要在 QQ 音乐 App 里手动取消。
2. **拖进度条不可靠**：stream 是 302，客户端用 HTTP Range 直连 CDN 的能拖；
   依赖 timeOffset（服务端转码）的会从头放。ESP 那条线本身就禁用 QQ seek。
3. **没有专辑/歌手浏览**：getArtists / getAlbumList2 目前返回空。
4. **只返回 JSON**：f=xml 不实现；ESP 与主流客户端（Symfonium/Feishin）都用 json。
5. **单账号**，无多用户/播放次数/跨设备续播。

## 安全清单

- 只暴露 3211（经隧道）；3210/4533 只在内网。
- /docs、/openapi.json 已关闭；公网 app 不注册 /v1/*。
- 网页登录密码只存在服务端内存 + web.json（600），cookie 是 HttpOnly 签名值；
  **密码在 Subsonic 协议下必须以明文保存在服务端**（t=md5(pass+salt) 决定的），
  所以 web.json 属主必须是 qqmusic、权限 600。
- 建议在 Cloudflare 侧加 Rate limiting（登录失败/暴力破解），或用 fail2ban 读日志。
- QQ 直链（2 小时有效）会随 302 暴露给播放方，这是协议本身决定的，可接受。

## 调试

```bash
journalctl -u qqweb -f
curl -s http://127.0.0.1:3211/healthz
# 手工验证（把 u/t/s 换成真实值）：
curl -s "http://127.0.0.1:3211/rest/ping.view?u=...&t=...&s=...&c=test&v=1.16.1&f=json"
curl -s -o /dev/null -D - "http://127.0.0.1:3211/rest/stream.view?id=<songmid>&u=...&t=...&s=..."
```
