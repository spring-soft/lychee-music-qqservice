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
| webapp.py | 3211 入口：页面 + /login /logout + 挂 /rest + 安全头 |
| subsonic_api.py | Subsonic 协议层（认证、端点映射、歌单翻页、专辑/歌手浏览） |
| websession.py | 网页登录 cookie（HMAC 签名，HttpOnly） |
| guard.py | 防护：登录/鉴权失败封禁、每 IP 限速、并发流上限 |
| wwwweb/ | 网页播放器（index.html / app.js / app.css） |
| deploy/qqweb.socket + qqweb.service | systemd socket activation（3211） |
| _rest_selftest.py | 服务器上的协议自检 |
| data/web.json | **不进仓库**，权限 600：user / password / star_write / server_type |

## 部署

```bash
mkdir -p /opt/qqmusic/wwwweb
# 上传 webapp.py subsonic_api.py websession.py guard.py wwwweb/* 到 /opt/qqmusic/
cp deploy/qqweb.socket deploy/qqweb.service /etc/systemd/system/
cat > /opt/qqmusic/data/web.json <<'JSON'
{"user": "665858hjn", "password": "<强密码>", "star_write": true}
JSON
chmod 600 /opt/qqmusic/data/web.json
chown -R qqmusic:qqmusic /opt/qqmusic
systemctl daemon-reload
systemctl enable --now qqweb.socket
systemctl enable --now qqweb.service
```

验证：

```bash
curl -s http://127.0.0.1:3211/healthz          # 含 guard 计数
cd /opt/qqmusic && ./venv/bin/python _rest_selftest.py
```

### 为什么用 socket activation

监听 socket 由 systemd 持有，重启 qqweb 时 socket **不关闭**，
正在建立的连接排队等新进程，而不是被拒绝。
实测：重启期间连续 100 次请求 99 次成功（1 次是正在途中的请求）。

## 公网暴露（Cloudflare Tunnel）

在**另一台内网设备**上跑 cloudflared，入站指向 http://192.168.0.116:3211 ：

```yaml
ingress:
  - hostname: music.<域名>
    service: http://192.168.0.116:3211
  - service: http_status:404
```

⚠️ **绝对不要**给 3210 加隧道规则：那条线没有鉴权。
TLS 由 Cloudflare 终止；音频是 302 到 QQ CDN，**不经过 Cloudflare**。

## 支持的端点

基础：ping / getLicense / getMusicFolders / getUser / getAvatar / getScanStatus
搜索：search3（=search2=search）
歌曲：getSong / getRandomSongs / getSimilarSongs2 / getTopSongs（空）
歌单：getPlaylists / getPlaylist（**6 个 QQ 模式 = 6 个歌单**，自动翻页整份返回）
　　　createPlaylist / updatePlaylist / deletePlaylist（客户端自建歌单，存 data/playlists.json）
收藏：getStarred2 / star（**真写回 QQ「我喜欢」并回读校验**）/ unstar（明确不支持，code=30）
专辑：getAlbumList2（newest/recent/random → QQ 新碟上架）/ getAlbum（专辑详情 + 全部曲目）
歌手：getArtists（QQ 热门歌手，按首字母分组）/ getArtist（歌手专辑）/ getArtistInfo2（空壳）
播放：stream / download（302 → QQ CDN 的 https 直链）
图片/歌词：getCoverArt（服务器代理小图；歌手头像 id 前缀 ar_）/ getLyricsBySongId
其它：scrobble / getNowPlaying / getPlayQueue / savePlayQueue / getOpenSubsonicExtensions
未实现端点返回 status=ok 的空对象（客户端退化，不报错）。

## 防护（guard.py）

| 项 | 值 |
|---|---|
| 登录失败 | 5 次/10 分钟 → 封 IP 15 分钟（返回 429） |
| 协议鉴权失败 | 20 次/5 分钟 → 封 IP 10 分钟（返回 Subsonic code=50） |
| 每 IP 请求 | 120 次/分钟（封面/头像不计） |
| 并发取流 | 全局 8 个，超时拒绝 |
| 安全响应头 | CSP / X-Frame-Options / nosniff / Referrer-Policy |

只对**失败**计数，正常播放/浏览不会被误伤；计数在内存里，重启清零。
healthz 里的 guard 字段可看实时计数。

⚠️ **内网(192.168.x/10.x) / 本机 / Tailscale(100.64.0.0/10 等) 地址不做封禁**——
封禁只为防公网暴破。2026-10-25 踩过：不区分来源时，内网设备输错几次密码就被封，
连已登录的播放/封面也被拦，表现为"突然登录不上、歌也放不了"；修复见 guard._trusted()。

## 凭据自动续期（重要）

QQ 的 musickey 只有 **3 天**有效期（key_expires_in=259200），过期后所有取流接口
报「登录凭证已过期，重新扫码」。但上游库有 refresh_credential()（refresh_token 续期），
所以不需要频繁扫码：

- `credrefresh.timer` 每 30 分钟检查；剩余 < 2 小时就续期并写回 `data/credential.json`
- qqweb 看文件 mtime 自动重载；qqmusic(3210) 有 5 分钟一次的 mtime/到期检查
- 只有 **refresh_token 本身失效**（长期不续、换设备、QQ 侧踢下线）才需要重新扫码：
  打开 http://<服务器>:3210/ 的网页控制台 → QQ 账号面板 → 扫码

手动检查/续期：

```bash
cd /opt/qqmusic && ./venv/bin/python credrefresh.py          # 还够用会跳过
./venv/bin/python credrefresh.py --force                      # 强制续期
systemctl list-timers credrefresh.timer
```

## 网页播放器

登录（HttpOnly cookie）、歌单卡片（含曲目数）、搜索、队列抽屉、歌词同步高亮、
加星（写回 QQ）、随机排序、音量、键盘快捷键（空格播放 / ←→跳 5 秒 / ↑↓音量 / N P 切歌）、
媒体键（锁屏控制）、**刷新后恢复上次队列和进度**（点击播放从上次位置继续）。

## 已知限制

1. **取消收藏做不到**：QQ 上游对 dirId=201 的删除静默忽略（app.py 里 2026-10-11
   穷举过 10 种写法）。star 真写回并回读总数校验；unstar 明确返回 code=30。
2. **拖进度条不可靠**：stream 是 302，客户端用 HTTP Range 直连 CDN 的能拖；
   依赖 timeOffset 的会从头放（QQ 直链没有时间轴 seek）。
3. **收藏/歌单整份返回是翻页拼的**：fav 913 首第一次要约 15 秒，之后缓存 30 分钟，
   启动后后台预热；过期后先返回旧数据再后台刷新（stale-while-revalidate）。
4. **响应格式**：f=json → JSON；f 缺省 / f=xml → XML（Subsonic 规范默认 XML，
   2026-10-25 补，音流 Musiver 就是不带 f 的客户端）；单账号；无播放次数/跨设备续播。
5. **专辑列表只有「新碟上架」**（newest/recent/random），没有按流派/年代筛选。

## 调试

```bash
journalctl -u qqweb -f                     # 每个请求一行 REQ（客户端名 c / UA / IP / 参数）
curl -s http://127.0.0.1:3211/healthz      # 凭据 + guard 计数
```
