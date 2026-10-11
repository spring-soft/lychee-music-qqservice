# QQ 音乐 → ESP-Music 契约层

给 `E:\program\esp-music` 那个 ESP32 播放器提供"第二个音源"：在设备上搜 QQ 音乐、放 QQ 音乐的歌。

部署位置：`F:\wsl\qqmusic`（和 Navidrome / Immich 那套并列）
运行端口：**3210**
设备访问地址：`http://192.168.0.108:3210`（本机局域网 IP，和设备同网段）

---

## 一、为什么要有"契约层"这一层

上游用的是 [`qqmusic-api-python`](https://pypi.org/project/qqmusic-api-python/)（仓库 `l-1124/QQMusicApi`）——
它是逆向 QQ 接口的，**QQ 一改签名/参数它就可能失效**，作者得跟着修。

关键在于：**固件不能跟着变。** 每次上游一变就要重刷 ESP32 是灾难（要插串口、要盯日志）。
所以这里定义一套**我们自己的、冻结的** `/v1/*`，固件只依赖它：

```
QQ 音乐上游 ──(非官方接口 + 凭据)
      ↑
[本容器]  =  上游适配（qqmusic-api-python，锁版本）  +  ★契约层（我方 schema，固件只认这个）
      ↑
[ESP32]   search / list(6 模式) / url(直链) / lyric  ＋ 自己拼封面直链
```

上游怎么变，只改这个容器里的 `app.py`，固件一行不用动。

### 为什么没用现成的 Docker 镜像

Docker Hub 上搜 `qqmusic` 全是 **0 star 的社区镜像**（最相关的 `zhouchijian/qqmusicapi` 自称打包自
`jsososo/QQMusicApi`）。**不用它们的原因**：容器里要放你的 **QQ 账号凭据**（等于账号密码），
跑一个无法审计的第三方镜像，供应链风险不能接受。所以自己写 Dockerfile、自己锁版本。

### 为什么不用 `jsososo/QQMusicApi`

它是这类项目里最出名的，但**最后一次提交是 2022-07-09（四年前）**，QQ 的接口早变了。
选型看的是**维护活跃度**：

| 候选 | 最后更新 | 结论 |
|---|---|---|
| `jsososo/QQMusicApi` | 2022-07-09 | ❌ 四年前，基本失效 |
| **`qqmusic-api-python`（l-1124）** | **2026-10-05** | ✅ **选它**（Python、pip 可锁版本） |
| `sansenjian/qq-music-api` | 2026-08-27 | ✅ 活跃，但是 Node 服务，不好嵌进契约层 |

---

## 二、契约（固件只依赖这一节）

### `GET /v1/health`
一眼判断能不能用。**这是最重要的接口** —— 这类服务头号故障是"凭据过期"，
表现却是"突然所有歌都放不了"，最难查。

```json
{"ok": true,
 "credential": {"present": true, "valid": true, "expired": false, "musicid": 115...},
 "quality_chain": ["flac", "320", "128"],
 "vip": {"truthy_keys": ["can_renew", "identity", "svip", "userinfo", ...]},
 "cdn": "http://aqqmusic.tc.qq.com/"}
```
`ok=false` 或 `credential.valid=false` → 重新登录。

### `GET /v1/search?q=<utf8>&limit=20&quality=flac`
```json
{"items": [{"id": "0027oMO61wWi55",     // 稳定 id（QQ 的 mid），★ 不含音质挡位
            "title": "发如雪", "artist": "周杰伦", "album": "十一月的萧邦",
            "duration": 299,             // 秒
            "suffix": "flac",            // ★ 只可能是 flac / mp3（白名单内）
            "size": 36226753,            // 字节 —— 固件靠它算码率
            "quality": "flac",           // flac / 320 / 128
            "cover": "0024bjiL2aocxT"}], // 专辑 mid，短且稳定
 "count": 1}
```

### `GET /v1/url?id=<mid>&q=flac|320|128`  ← **播放的主路径**
返回 **QQ 直链**，设备直接拿它拉流（**本服务不做音频代理**）。

```json
{"url": "http://ws6.stream.qqmusic.qq.com/F000002A6j2q1tdUUP.flac?guid=...&vkey=428FA257...",
 "quality": "flac", "suffix": "flac", "size": 36226753, "expiration": 7200}
```

⚠️ **必须"播放前现取"**：`expiration` 只有 2 小时，不能提前把 URL 存进队列。
⚠️ URL 长度约 **250~260 字符** —— 固件 `player.c` 里是 `char url[768]`，装得下。

**为什么不做代理**（2026-10-11 把代理接口删了，三条"必须代理"的理由实测全部不成立）：
1. `vkey` + `guid` 都是 URL 里的查询参数 → **完全不带任何请求头，直连就返回 206**；
2. 2 小时过期 → 设备本来就该播放前现取，不是问题；
3. 直链不返回 `Accept-Ranges` → 固件里这个字段**只打日志、不参与任何判断**
   （`audio_source_http.c:87` 是唯一读取处）。
   而直链**实测支持 Range**（`bytes=5000000-5001023` → 206），拖进度条没问题。

### 其它
- `GET /v1/lyric?id=<mid>` → LRC 纯文本（上游是加密的，库已解密）
- `GET /v1/favorites?limit=50` → 读 QQ 账号的"我喜欢"（dirid=201），**格式与 /v1/search 完全一致**
- `POST /v1/login/qr?type=mobile` / `GET /v1/login/status` / `POST /v1/login/import`

### 封面：固件自己拼（本服务也不代理封面）
```
http://y.qq.com/music/photo_new/T002R300x300M000{albumMid}.jpg
```
- `albumMid` = 列表里返回的 `cover` 字段（16 字符、稳定，适合当封面缓存 key）
- ⚠️ **尺寸只能取 90 / 120 / 150 / 300 / 500 / 800**，其它（40/60/100/160/200…）一律 **404**。
  设备屏幕 160×128，**固定用 300** 最省事。
- 实测 http/https 都通，不带任何请求头也能取到。

---

## 三、⚠️ 固件侧的硬性要求（改契约前先读）

这些不是"设计偏好"，是**固件代码决定**的，违反就会静默出错：

| # | 要求 | 固件依据 |
|---|---|---|
| 1 | **音频和封面都走直连**（本服务不做任何代理） | vkey+guid 都在 URL 里，实测无请求头直连可通；`ae_source_http_new(url)` 也只吃一个 URL |
| 2 | 播放地址**必须播放前现取**（`/v1/url`） | vkey `expiration` 只有 2 小时，不能提前存队列 |
| 3 | `suffix` 只能是 `mp3/flac/aac/m4a` | `subsonic.c` 的 `suffix_direct_playable()` 白名单 |
| 4 | `size`/`duration` 必须填准 | `bitrate_fits_direct()` 卡 **300KB/s（2400kbps）**，固件靠它决定直出还是转码 |
| 5 | `cover` 是专辑 mid，固件自己拼封面直链 | 固件 `sub_song_t.cover` 只有 48 字节，装不下整条 URL |
| 6 | `id` 必须**稳定**，且**不含音质挡位** | id 会被写进固件的收藏/最近播放；含挡位则换挡=换歌、收藏全失 |

**音质策略**（`QUALITY_CHAIN`）：`flac → 320 → 128`，第一个有文件的胜出。
**刻意不用的挡位**：
- **OGG 系**（QQ 的 HQ 是 `.ogg`）→ 扩展名不在固件白名单内
- **臻品母带 / 全景声 / DTS**（`size_new[0]` 是 24bit/192kHz）→ 码率远超固件阈值，
  且扩展名是 `.mp4`/`.nac`，固件解不了

---

## 四、部署与日常

```bash
cd F:\wsl\qqmusic
docker compose build           # 改完 app.py 要重跑（只重编最后一层，很快）
docker compose up -d
docker compose logs -f --tail 100
curl http://localhost:3210/v1/health
```

凭据落在 `./data/credential.json`（宿主机），**重建容器不用重新扫码**。

### 登录（⚠️ 有个坑）
```bash
curl -X POST "http://localhost:3210/v1/login/qr?type=mobile" -o qr.json
# 取 qr.json 里的 image_base64 存成 png，用【手机 QQ 音乐 App】扫
curl "http://localhost:3210/v1/login/status"     # 看 event：WAITING/SCAN/CONF/DONE/TIMEOUT
```

**坑：库里有三种二维码，别用错。**

| 库的类型 | 实际是 | 扫出来 |
|---|---|---|
| `QQ` | **QQ 网页登录**（`ssl.ptlogin2.qq.com/ptqrlogin`） | ❌ **QQ 下载页**（我第一次就用错了） |
| `WX` | 微信扫码 | 微信授权 |
| **`MOBILE`** | **手机 QQ 音乐 App 扫码**（`music.login.LoginServer.CreateQRCode`，`tmeAppID="qqmusic"`） | ✅ **真正要的那个** |

而且校验方式不同：`QQ`/`WX` 用 `check_qrcode()` 一次性轮询；
**`MOBILE` 必须用 `checking_mobile_qrcode()`** —— 它是个**异步生成器**，开一条 MQTT 连接
（`mu.y.qq.com:443/ws/handshake`，topic `management.qrcode_login/<id>`）持续收推送。
注意 `check_qrcode()` 内部**根本不处理 MOBILE**（只判 WX，其余全当 QQ）。
本服务已把它包成"后台任务 + `GET /v1/login/status` 读最新状态"。

---

## 五、凭据会过期（头号运维事项）

- 凭据里 `keyExpiresIn` 决定有效期，过期后**所有拿播放地址的请求都会失败**。
- 判断方法：`GET /v1/health` 的 `credential.expired` / `valid`。
- 过期了就重新扫一次码（`POST /v1/login/qr`），几秒钟的事。
- ⚠️ 别把 `credential.json` 提交进任何仓库、别贴给别人 —— 它等于你的 QQ 登录态。

---

## 六、⚠️ 头号运维坑：Docker Desktop 的代理设置会把容器搞成全断网

**这个坑真实发生过（2026-10-11），现象极具误导性，务必记住。**

Docker Desktop 里配着 manual 代理：
```json
// %APPDATA%\Docker\settings-store.json
"ProxyHTTPMode": "manual",
"OverrideProxyHTTP":  "http://127.0.0.1:7890",
"OverrideProxyHTTPS": "http://127.0.0.1:7890"
```
当 `7890` 那个代理**关掉**时，**容器内所有出网请求全部失败**：

| 现象 | 实际报错 |
|---|---|
| 容器内所有 HTTPS | `SSL: UNEXPECTED_EOF_WHILE_READING`（TLS 握手被截断） |
| 容器内 HTTP | 500，并明说 `via app settings HTTP proxy http://127.0.0.1:7890: **actively refused it**` |
| **主机**上同样的请求 | ✅ 完全正常 |

**误判风险极高**：因为主机一切正常、HTTP 有回应、只有 HTTPS 断，
很容易误判成"QQ 封了账号 / MTU 问题 / 固件代码问题"，从而往完全错误的方向查。

诊断口诀（记住这一条就够）：
```
容器内连 pypi/github/百度 也全断 → 是 Docker 的网络/代理问题，跟目标站点无关
只有某个站点断                    → 才轮到怀疑那个站点
```
排查命令：
```powershell
Test-NetConnection 127.0.0.1 -Port 7890         # 代理在不在
Get-Content "$env:APPDATA\Docker\settings-store.json" | Select-String proxy
docker run --rm python:3.13-slim python -c "import urllib.request;print(urllib.request.urlopen('https://pypi.org/simple/',timeout=10).status)"
```

两种修法：
- **要国内直连** → 把 Docker Desktop 的代理关掉（Settings → Resources → Proxies → No proxy）。
  **需要重启 Docker 引擎**，会短暂停掉 Immich 等其它容器。
- **图快** → 把 7890 的代理重新打开，立刻恢复。但容器流量会绕到国外出口。

> 注意：**音频那一路上这两种选择都不影响** —— 设备是从局域网自己出去取 QQ 直链的，
> 走的是你家宽带的国内出口，跟容器的代理无关。受影响的只有服务端的元数据请求。

---

## 七、已知限制

1. **拉不到 Docker Hub**：本机 `auth.docker.io` 解析到 IPv6 后连不上
   （`failed to fetch oauth token: dial tcp [2a03:2880:...]`），
   所以 Dockerfile 的基础镜像用的是**本机已有的** `python:3.13-slim`。
2. **上游可能瞬时抖动**：实测「歌手漫游」会偶发 `CgiApiException code=80121`，
   隔几秒再请求就好。`_retry()` 会对所有模式重试一次（1.5s 后）。
3. **收藏：能加，不能删**（2026-10-11 查清，穷举过 4 类 10 种组合）
   - **加** ✅ `POST /v1/favorite?id=<mid>&on=1` → `PlaylistDetailWrite / AddSonglist`（dirid=201）
     实测有效，歌真的进了「我喜欢」。返回里带 `fav_count_before/after` 做**回读校验**。
   - **删** ❌ QQ 服务端对 `dirId=201` 的删除是**静默忽略**：
     `PlaylistDetailWrite / DelSonglist` 返回 `retCode=0` 但什么都不做。
     试过且**全部无效**：库的 `unlike_song`、真实 tid `5490354462`、
     `preserve_bool=True`、`dirId=tid`（→80092）、
     `music.musicasset.SongFavWrite` 的 6 种方法/参数（→500003，模块不存在）。
     参考实现 `sansenjian/qq-music-api` 也**根本没有写操作**（文件名就叫 `readonly-extras.test.ts`）。
   - **所以 `on=0` 返回 409 并明确说明**，不做任何改动；设备侧提示用户去 App 手动取消。

   > ⚠️ **别被上游库的自测骗了**：`qqmusic-api-python` 的
   > `tests/test_songlist.py::test_like_song_roundtrip` 看起来验证了"取消收藏可逆往返"，
   > 但它只有 `assert await unlike_song(...)` —— 而 `del_songs` 的实现是
   > `return data.get("retCode") == 0`，`DelSonglist` **永远**返回 `retCode=0`，
   > 所以**删除失败也断言通过**。它从不回读列表确认歌真的没了，是个**假阳性**。
   > （该库 `main` 分支与 0.8.2 的 `del_songs` 一模一样，没有未发布的修复。）
   >
   > 教训：**写操作必须回读校验**。这里用 `info.songnum`（收藏**总数**）做前后对比 ——
   > 它不受 `limit` 分页影响（这个号 898 首，读前 100 首根本判断不出来）。

4. **单 worker**：uvicorn 故意只起 1 个 worker —— 多 worker 会各自维护登录态、也更容易触发风控。
5. **`/v1/favorites` 会多打一次上游**：上游的收藏列表**不带文件大小和时长**，
   必须再调一次 `query_song` 补齐（一次最多 100 个 mid），否则固件没法算码率。
6. **`songlist.get_detail` 没有 `credential` 参数** —— 只能靠 **Client 的默认凭据**。
   所以启动时就必须 `Client(credential=...)` + `set_cred()`；漏了的话：
   公共歌单（百万收藏/新歌推荐）照样能拿，**只有个性化歌单「每日30首」报 `code=10004`**，
   现象是"别的模式都好，就它坏"，很难查。

---

## 七、升级上游的流程

`requirements.txt` 里 `qqmusic-api-python` 是**锁死版本**的。升级时：

1. 改版本号 → `docker compose build && up -d`
2. 跑一遍本机验证（`/v1/search` / `/v1/url` / 6 个 `/v1/list?mode=` / `/v1/lyric`）
3. 重点看：`/v1/url` 是否还 200（即 `result` 还是 0）、`suffix` 是否还在白名单内、
   `/v1/favorites` 的返回结构有没有变（`app.py` 里是"尝试多个 key"的容错写法）

---

## 八、已验证（2026-10-11，真机真账号）

| 项 | 结果 |
|---|---|
| 镜像构建 | ✅ `esp-music/qqmusic:1.0` |
| 容器 | ✅ 跑在 3210，`restart: unless-stopped` |
| 扫码登录（mobile） | ✅ `event=DONE`，凭据落盘，`musicid=1152921504874043511` |
| VIP 识别 | ✅ `vip.truthy_keys` 里含 `svip` |
| 搜索 | ✅ 周杰伦 → flac/833~1677kbps，中文按**码点**验证（`发如雪`=0x53d1 0x5982 0x96ea） |
| **拿播放地址** | ✅ `HTTP 200`，`filename=F000...flac`，带 `vkey`，`expiration=7200` |
| **直连拉流 + Range** | ✅ 无任何请求头直连 `206`；中途 `bytes=5000000-5001023` 也 `206`（任意偏移 → seek 可用） |
| 文件真实性 | ✅ magic `fLaC`；mp3 档 magic `494433`(ID3) |
| 封面 | ✅ 映射后 200，JPEG(`ffd8ff`) |
| 歌词 | ✅ 2595 字节 LRC，**已解密**（`[ti:发如雪]`） |
| 「我喜欢」 | ✅ 读到真实收藏，归一化后含 quality/size/duration |
| 自动降级 | ✅ 没 FLAC 的歌退到 320k mp3 |
