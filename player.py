"""服务器端播放器 —— 让这台 OpenStick 自己出声（接 USB DAC）。

定位：ESP-Music 的「第二个播放设备」。固件菜单里选了「服务器」之后，歌由这里放，
ESP 只当遥控器 + 显示屏（固件那侧还没做，本文件先把服务端能力做齐）。

============================================================================
为什么是 mpv 当引擎（备选都试过了，别改回去之前先看这里）
============================================================================
· 这台机器**没有 PulseAudio/PipeWire**，纯 ALSA。mpv 能
  `--audio-device=alsa/hw:CARD=ECHOA,DEV=0` 直接占硬件设备，
  实测 24bit 直出 OK（`audio-params` 报 samplerate=44100 / format=s32）。
· ffmpeg 反而做不到 24bit：它的 alsa 输出端会把格式压回 s16
  （实测 `-sample_fmt s32` 出的还是 pcm_s16le，`hw:` 直接开不起来）。
· mpd 在 Debian 13 上要「升级 4 个包 + 卸载 4 个包」，而 mpv 是
  「15 个新装 / 0 升级 / 0 卸载」—— 在一台只有 376MB 内存的生产机上，
  前者风险不值得冒。mpv 自带的 JSON IPC 已经把暂停/跳转/位置/时长/播完事件全给了。
· mpv 是本服务的**子进程**，跟着 systemd 一起死活，不额外多一个 daemon。

============================================================================
为什么队列在我们这边、而不是整个塞进 mpv 的 playlist
============================================================================
那样能真无缝，但 QQ 的播放地址是**播放前现取**的（vkey 只有 2 小时有效期），
一次把 60 首的 URL 全解出来既慢又容易触发上游限流。所以队列留在这里，
一首一首 loadfile；代价是换歌有 ~0.1s 的缝 —— 用「提前解下一首的 URL」压到最小。

============================================================================
接口约定
============================================================================
全部 **GET + query 参数**（跟本服务既有风格一致：固件拼 URL 比构造 POST body 省事）。

    GET /v1/player/status
    GET /v1/player/play?source=qq|nav&mode=fav&index=0&limit=60   # 整列表从第 index 首起播
    GET /v1/player/play?source=nav&id=abc123                      # 单曲
    GET /v1/player/pause | resume | toggle | stop | next | prev
    GET /v1/player/seek?pos_ms=12345
    GET /v1/player/volume?level=0..100
    GET /v1/player/queue
    GET /v1/player/modes          # 服务器这侧两个来源各自有哪些「模式」
    GET /v1/player/devices        # 机器上有哪些声卡（调试/以后设置页用）

配置：/opt/qqmusic/data/player.json（600 权限，不在仓库里）
    {"device": "alsa/hw:CARD=ECHOA,DEV=0", "volume": 80,
     "nav": {"host": "http://127.0.0.1:4533", "user": "...", "password": "..."}}
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse, Response

log = logging.getLogger("player")

DATA_DIR = Path(os.environ.get("QQ_DATA_DIR", "/data"))
CFG_PATH = DATA_DIR / "player.json"
SOCK_PATH = DATA_DIR / "mpv.sock"
MPV_LOG = DATA_DIR / "mpv.log"

DEFAULT_CFG: dict[str, Any] = {
    # USB DAC（TTGK ECHO-A）实测支持 S16_LE / S24_3LE / S32_LE，44.1k~384k
    "device": "alsa/hw:CARD=ECHOA,DEV=0",
    "volume": 80,
    "nav": {"host": "http://127.0.0.1:4533", "user": "", "password": ""},
}

# 状态里给固件看的「服务器端可用模式」。
#
# ⚠️ 顺序和名字**必须跟固件里的 Navidrome 模式一一对应**（固件用的是数字下标
#    0=最近播放 / 1=我的收藏 / 2=随机30首）。对不上的后果是：用户在设备上选「我的收藏」，
#    服务器却按「随机30首」建队列 —— 而且因为数据都对，症状只是"歌不对"，极难查。
#    QQ 的模式表由 app.py 传进来（那份是权威，见 app.py 的 MODES）。
NAV_MODES = [
    {"index": 0, "mode": "recent",  "name": "最近播放",   "desc": "getAlbumList2?type=recent 摊平成歌"},
    {"index": 1, "mode": "starred", "name": "我的收藏",   "desc": "getStarred2 星标歌曲"},
    {"index": 2, "mode": "random",  "name": "随机 30 首", "desc": "getRandomSongs"},
]


# ============================================================ 配置

def load_cfg() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CFG))          # 深拷贝
    if not CFG_PATH.exists():
        return cfg
    try:
        user = json.loads(CFG_PATH.read_text("utf-8"))
    except Exception as e:                              # noqa: BLE001
        log.warning("player.json 读不了，用默认值: %s", e)
        return cfg
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def save_cfg(cfg: dict[str, Any]) -> None:
    try:
        tmp = CFG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(CFG_PATH)
    except Exception as e:                              # noqa: BLE001
        log.warning("player.json 写不进去: %s", e)


# ============================================================ mpv 子进程

# ============================================================ USB DAC 在位检测
#
# ★ 为什么要自己查这个（真踩过）：
#   DAC 被拔掉时，mpv 的 ao 直接废掉；**插回来它也不会自己恢复** ——
#   mpv 不会重新枚举 ALSA 设备，新开的文件照样往一个已经不存在的 pcm 写，直接报错。
#   以前的现象：拔一下 DAC，音乐停了；插回去，还是停着（除非手动重启服务）。
#   所以：我们来当看门狗 —— 发现设备没了就记住位置停下，回来了就重起 mpv 接着放。

_DAC_CARD_RE = re.compile(r"CARD=([^,]+)")


def dac_present(device: str) -> bool:
    """mpv --audio-device 里那个声卡现在还在不在。

    device 形如 `alsa/hw:CARD=ECHOA,DEV=0`（按**名字**匹配，所以重新拔插后
    卡号变了也不影响）。读不到 /proc/asound/cards 就返回 True —— 宁可不动，
    也别因为读不到而把播放器锁死。
    """
    try:
        txt = Path("/proc/asound/cards").read_text("utf-8", "replace")
    except OSError:
        return True
    # 调试开关：data/dac_absent 存在 → 当作"没有 DAC"。
    # 为什么要它：验证热插拔逻辑（拔掉→记住位置→插回续播）总不能让用户真的去拔线；
    # 有这个文件就能一条命令模拟"拔掉/插回"，测完删掉即可。
    if (DATA_DIR / "dac_absent").exists():
        return False
    m = _DAC_CARD_RE.search(device or "")
    if m:
        return m.group(1).strip().upper() in txt.upper()
    m2 = re.search(r"hw:(\d+)", device or "")
    if m2:
        return re.search(r"^\s*%s\s+\[" % re.escape(m2.group(1)), txt, re.M) is not None
    return True


class Mpv:
    """mpv 的一层薄封装：起进程、连 IPC、发命令、收事件。

    IPC 协议（mpv 官方）：每行一个 JSON，命令带 request_id，响应带回同一个 id；
    没有 request_id 的是事件（file-loaded / end-file / property-change ...）。
    """

    def __init__(self, sock: Path, device: str, volume: int):
        self.sock = sock
        self.device = device
        self.volume = volume
        self.proc: asyncio.subprocess.Process | None = None
        self.alive = False
        self.on_end: Callable[[str], Awaitable[None]] | None = None
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self._rid = 0
        self._wait: dict[int, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        # 每次 load() 建一个 future：file-loaded 或 end-file(reason=error) 时兑现
        self._load_fut: asyncio.Future | None = None
        self._end_reason = ""
        self._last_try = 0.0

    # ---- 起进程 ----
    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.sock.unlink()                          # 上次留下的死 socket 会让 bind 失败
        args = [
            "mpv",
            "--no-video", "--idle=yes", "--no-input-terminal",
            # ⚠️ --no-config 必须加：不然 mpv 会去读 ~/.config/mpv/ 下的配置，
            #   哪天有人在那个目录里留个 mpv.conf（比如改 vo/ao/volume）就会
            #   悄悄改掉本服务的行为，而且**完全不报错**，极难查。
            "--no-config",
            # 日志写文件（终端输出反正进了 /dev/null，出问题看 data/mpv.log）
            # ⚠️ 级别只用 warn：all=info 实测 4 分钟就写了 48KB（一分钟十几 KB），
            #   这台机器一天下来能写到十几 MB —— eMMC 写放大不值当。
            #   警告/错误才是排查播放问题要的，正常流程由我们自己的日志覆盖。
            f"--log-file={MPV_LOG}", "--msg-level=all=warn",
            "--ao=alsa", f"--audio-device={self.device}",
            f"--volume={self.volume}",
            "--audio-exclusive=no",                     # 留给 amixer/别人的余地
            # 网络流的缓冲。
            # ⚠️ 这几个数字是为内存调的：这台机器总共 376MB，而本服务在
            #   systemd 里被 MemoryHigh=120M / MemoryMax=150M 卡着。
            #   第一版照抄了 mpv 常见配置（32MiB demuxer + 15s cache），
            #   实测 cgroup 直接顶到 119.4M/120M（available 只剩 560K），
            #   再放歌就会开始疯狂换页。音频流码率最多 ~1MB/s，
            #   8MiB 预读 ≈ 8 秒，完全够；实测调小后 cgroup 降了 20 多 MB。
            "--cache=yes", "--cache-secs=10",
            "--demuxer-readahead-secs=12",
            "--demuxer-max-bytes=8MiB", "--demuxer-max-back-bytes=2MiB",
            "--network-timeout=20",
            f"--input-ipc-server={self.sock}",
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        # 等 socket 就位（不写死 sleep —— mpv 起得快慢跟机器负载有关）
        for _ in range(200):
            if self.sock.exists():
                try:
                    self._r, self._w = await asyncio.open_unix_connection(str(self.sock))
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    pass
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("mpv 起来了但 IPC socket 连不上")
        self._task = asyncio.create_task(self._loop())
        self.alive = True
        log.info("mpv 就绪 pid=%s device=%s volume=%d", self.proc.pid, self.device, self.volume)

    async def ensure(self) -> None:
        """按需拉起（mpv 被 OOM 杀掉后不用重启整个服务）。10 秒冷却，别疯狂重试。"""
        if self.alive:
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_try < 10:
            raise RuntimeError("mpv 没在跑（刚试过，等一会儿再试）")
        self._last_try = now
        await self.start()

    # ---- IPC ----
    async def _loop(self) -> None:
        assert self._r is not None
        while True:
            line = await self._r.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:                           # noqa: BLE001
                continue
            rid = msg.get("request_id")
            if rid is not None:
                fut = self._wait.pop(rid, None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
                continue
            ev = msg.get("event")
            if ev == "file-loaded":
                if self._load_fut is not None and not self._load_fut.done():
                    self._load_fut.set_result("loaded")
            elif ev == "end-file":
                self._end_reason = msg.get("reason") or ""
                # ⚠️⚠️ 这里有个很容易踩的坑（实测栽过）：loadfile ... replace 时，
                #   **旧文件**也会发一条 end-file，reason 是 **stop**。
                #   第一版在 load() 里等"任意 end-file"就判失败，于是每次切歌都报
                #   `mpv 播放失败: stop` —— 可实际上新歌已经在放了（状态里 pos 在涨）。
                #   所以只有 reason=error 才算这次加载失败；stop/eof 一律忽略。
                if (self._load_fut is not None and not self._load_fut.done()
                        and self._end_reason == "error"):
                    self._load_fut.set_result("error")
                elif self.on_end is not None and self._end_reason in ("eof", "error"):
                    asyncio.create_task(self.on_end(self._end_reason))
        self.alive = False

    async def command(self, *args: Any, timeout: float = 10.0) -> Any:
        if not self.alive or self._w is None:
            raise RuntimeError("mpv 不在跑")
        self._rid += 1
        rid = self._rid
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._wait[rid] = fut
        self._w.write((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
        await self._w.drain()
        try:
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"mpv 命令超时: {args[0]}") from e
        finally:
            self._wait.pop(rid, None)
        if msg.get("error") not in (None, "success"):
            raise RuntimeError(f"mpv 命令失败 {args[0]}: {msg.get('error')}")
        return msg.get("data")

    async def get(self, prop: str) -> Any:
        return await self.command("get_property", prop, timeout=5.0)

    async def set(self, prop: str, value: Any) -> Any:
        return await self.command("set_property", prop, value, timeout=5.0)

    # ---- 播放 ----
    async def load(self, url: str, start_ms: int = 0) -> None:
        """让 mpv 放这个 URL，等到真正开始（file-loaded）才算成功。

        ⚠️ 失败判定只看 `end-file` 的 reason == **error**。别改成"任意 end-file 就失败"：
           `loadfile ... replace` 时旧文件会发一条 reason=stop 的 end-file，
           那样每次切歌都会误报"播放失败: stop"（歌其实已经在放）。
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._load_fut = fut
        self._end_reason = ""
        try:
            await self.command("loadfile", url, "replace")
            try:
                res = await asyncio.wait_for(fut, 30)
            except asyncio.TimeoutError as e:
                raise RuntimeError("mpv 30 秒内没开始播放（网络或解码问题？看 data/mpv.log）") from e
        finally:
            self._load_fut = None
        if res == "error":
            raise RuntimeError(f"mpv 播放失败: {self._end_reason or '未知'}")
        if start_ms > 0:
            await self.command("seek", round(start_ms / 1000.0, 3), "absolute+exact")

    async def pause(self, want: bool) -> None:
        await self.set("pause", bool(want))

    async def seek_ms(self, ms: int) -> None:
        await self.command("seek", round(max(0, ms) / 1000.0, 3), "absolute+exact")

    async def stop(self) -> None:
        await self.command("stop")

    async def shutdown(self) -> None:
        self.alive = False
        with contextlib.suppress(Exception):
            await self.command("quit", timeout=2.0)
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.kill()
                await asyncio.wait_for(self.proc.wait(), 3)
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def restart(self) -> None:
        """整个重起一个 mpv 进程。

        什么时候需要：USB DAC 拔掉后 mpv 的 ao 已经废了，在坏进程上重新 load
        大概率继续报错 —— 直接换一个干净的进程最省事（DAC 插回来时用）。
        """
        log.warning("重启 mpv 进程（%s）", self.device)
        with contextlib.suppress(Exception):
            await self.shutdown()
        self._wait.clear()
        self._load_fut = None
        self.alive = False
        await self.start()


# ============================================================ Navidrome 客户端

def _nav_item(s: dict[str, Any]) -> dict[str, Any]:
    """Subsonic song JSON -> 我们统一的队列项。"""
    return {
        "back": 0,
        "id": s.get("id", ""),
        "title": s.get("title", ""),
        "artist": s.get("artist", ""),
        "album": s.get("album", ""),
        "dur_ms": int(s.get("duration") or 0) * 1000,   # Subsonic 给的是秒
        "cover": s.get("coverArt", ""),
        "suffix": s.get("suffix", ""),
        "quality": s.get("suffix", ""),
        "size": int(s.get("size") or 0),
    }


def _qq_item(it: dict[str, Any]) -> dict[str, Any]:
    """QQ /v1/list、/v1/search 的项 -> 我们统一的队列项（duration 是秒）。"""
    return {
        "back": 1,
        "id": it["id"],
        "title": it.get("title", ""),
        "artist": it.get("artist", ""),
        "album": it.get("album", ""),
        "dur_ms": int(it.get("duration") or 0) * 1000,
        "cover": it.get("cover", ""),
        "suffix": it.get("suffix", ""),
        "quality": it.get("quality", ""),
        "size": int(it.get("size") or 0),
    }


class Nav:
    """Navidrome 的 Subsonic 接口（服务端自己用，跟固件那份凭据是同一个账号）。

    为什么在服务器上直连 127.0.0.1：Navidrome 就跑在这台机器上，
    绕公网/IP 没意义，而且能避开热点那边的所有不确定性。
    """

    def __init__(self, host: str, user: str, password: str):
        self.base = (host or "http://127.0.0.1:4533").rstrip("/")
        self.user = user
        self.password = password
        self.http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)

    def _params(self, **kw: Any) -> dict[str, str]:
        # Subsonic 认证：t = md5(password + salt)，每次换随机 salt
        salt = secrets.token_hex(8)
        tok = hashlib.md5((self.password + salt).encode()).hexdigest()
        p = {"u": self.user, "t": tok, "s": salt,
             "v": "1.16.1", "c": "esp-music-server", "f": "json"}
        p.update({k: str(v) for k, v in kw.items()})
        return p

    async def get(self, endpoint: str, **kw: Any) -> dict[str, Any]:
        url = f"{self.base}/rest/{endpoint}.view"
        r = await self.http.get(url, params=self._params(**kw))
        r.raise_for_status()
        d = r.json().get("subsonic-response") or {}
        if d.get("status") != "ok":
            raise RuntimeError(f"Navidrome {endpoint} 失败: {d.get('error')}")
        return d

    async def check(self) -> dict[str, Any]:
        return await self.get("ping")

    def stream_url(self, sid: str) -> str:
        # format=raw：不许服务器转码（跟固件 subsonic_stream_url() 完全一致）
        return f"{self.base}/rest/stream.view?" + urlencode(self._params(id=sid, format="raw"))

    async def get_song(self, sid: str) -> dict[str, Any]:
        d = await self.get("getSong", id=sid)
        s = (d.get("song") or {})
        if not s:
            raise RuntimeError(f"Navidrome 查不到 {sid}")
        return _nav_item(s)

    async def search(self, q: str, limit: int) -> list[dict[str, Any]]:
        d = await self.get("search3", query=q, songCount=limit,
                           artistCount=0, albumCount=0)
        songs = ((d.get("searchResult3") or {}).get("song") or [])
        return [_nav_item(s) for s in songs]

    async def cover(self, cid: str, size: int) -> tuple[bytes, str]:
        """取封面字节（网页控制台用）。

        ⚠️ 为什么由服务端代理、而不是让浏览器直连 Navidrome：
           Subsonic 的 getCoverArt 要带账号令牌。把令牌写进网页 = 所有能打开这个页面的人
           都拿到了你的 Navidrome 账号（内网也一样）。所以令牌只留在服务端。
        """
        r = await self.http.get(f"{self.base}/rest/getCoverArt.view",
                                params=self._params(id=cid, size=size))
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg")

    async def list_mode(self, mode: str, limit: int) -> tuple[str, list[dict[str, Any]]]:
        if mode in ("starred", "fav"):
            d = await self.get("getStarred2")
            songs = ((d.get("starred2") or {}).get("song") or [])
            return "我的收藏", [_nav_item(s) for s in songs][:limit]

        if mode == "random":
            d = await self.get("getRandomSongs", size=limit)
            songs = ((d.get("randomSongs") or {}).get("song") or [])
            return f"随机 {len(songs)} 首", [_nav_item(s) for s in songs][:limit]

        if mode == "recent":
            # Navidrome 的 getAlbumList2?type=recent 返回的是**专辑**，
            # 得逐个 getAlbum 摊平成歌。只取前 8 张（N+1 打太狠会拖慢首播）。
            d = await self.get("getAlbumList2", type="recent", size=8)
            albums = ((d.get("albumList2") or {}).get("album") or [])
            items: list[dict[str, Any]] = []
            for a in albums:
                try:
                    ad = await self.get("getAlbum", id=a["id"])
                except Exception as e:                  # noqa: BLE001
                    log.warning("getAlbum %s 失败: %s", a.get("id"), e)
                    continue
                items.extend(_nav_item(s) for s in ((ad.get("album") or {}).get("song") or []))
                if len(items) >= limit:
                    break
            return "最近播放", items[:limit]

        raise HTTPException(404, f"Navidrome 没有模式 {mode}；可用：starred/random/recent")

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.http.aclose()


# ============================================================ 播放器

class Player:
    """队列 + 当前曲目 + mpv 的胶水层。"""

    def __init__(self, cfg: dict[str, Any], mpv: Mpv, nav: Nav | None):
        self.cfg = cfg
        self.mpv = mpv
        self.nav = nav
        self.lock = asyncio.Lock()
        self.queue: list[dict[str, Any]] = []
        self.index = -1
        self.page = 1                    # 已加载到第几页（「加载更多」用）
        self.source: str | None = None
        self.mode_key = ""
        self.mode_label = ""
        self.gen = 0
        self.error: str | None = None
        self.volume = int(cfg.get("volume", 80))
        self._err_streak = 0
        # ---- USB DAC 热插拔状态（见文件上部 dac_present() 的说明）----
        self.dac_ok = True               # DAC 现在在不在
        self.dac_lost = False            # 是不是"因为拔 DAC 才停下的"（插回来要接着放）
        self.dac_pos_ms = 0              # 拔掉那一刻放到哪儿了
        self.dac_lost_idx = -1           # 拔掉时放的是队列第几首
        self._pending_play: dict[str, Any] | None = None  # DAC 不在时记下的点播请求
        self._dac_task: asyncio.Task | None = None
        # 下一首 URL 预取（vkey 只有 2 小时，所以只在"马上要用"时才解）
        self._next_url: str | None = None
        self._next_idx = -1
        self._vol_task: asyncio.Task | None = None
        # 由 app.py 注入（QQ 那侧的取数逻辑属于 app.py，不在这里重写一遍）
        self.qq_item: Callable[[str], Awaitable[dict[str, Any]]] | None = None
        self.qq_url: Callable[[str], Awaitable[str]] | None = None
        self.qq_list: Callable[[str, int], Awaitable[tuple[str, list[dict[str, Any]]]]] | None = None
        self.qq_search: Callable[[str, int], Awaitable[list[dict[str, Any]]]] | None = None
        self.mpv.on_end = self._on_end

    # ---- 取数/解地址 ----
    async def _list(self, source: str, mode: str, limit: int,
                    page: int = 1) -> tuple[str, list[dict[str, Any]]]:
        if source == "qq":
            assert self.qq_list is not None
            label, items = await self.qq_list(mode, limit, page)
            return label, [_qq_item(it) for it in items]

        if self.nav is None:
            raise HTTPException(503, "这台服务器没配置 Navidrome 账号（data/player.json 的 nav 段）")
        return await self.nav.list_mode(mode, limit)

    async def more(self, n: int = 60) -> dict[str, Any]:
        """列表「加载更多」：把当前模式**下一页**追加到队列尾部。

        为什么要这个：一开始只建 60 首的队列，用户滚到底就没得看了
        （收藏有 64 首就会少 4 首，歌单多的时候更惨）。
        QQ 的收藏/卡片歌单都支持 page（get_fav_song(page=) / get_detail(page=)），
        所以下一页是真·下一页；刷歌/雷达是每次随机，只能"再抽一批、去重后追加"。
        Navidrome 那三个模式没有分页概念 → 直接返回 exhausted。
        """
        async with self.lock:
            if not self.queue or not self.source:
                raise HTTPException(409, "还没有列表，先挑个模式或搜索")
            if self.source != "qq":
                return {"ok": True, "added": 0, "exhausted": True,
                        "count": len(self.queue)}
            page = self.page + 1
            try:
                _label, items = await self._list("qq", self.mode_key, n, page)
            except HTTPException:
                raise
            except Exception as e:                          # noqa: BLE001
                raise HTTPException(502, f"取下一页失败: {type(e).__name__}: {e}") from e
            have = {(it.get("back"), it.get("id")) for it in self.queue}
            added = 0
            for it in items:
                k = (it.get("back"), it.get("id"))
                if k in have:
                    continue
                have.add(k)
                self.queue.append(it)
                added += 1
            self.page = page
            if added:
                self.gen += 1            # 让客户端知道队列变了（ESP 会重新镜像前 64 首）
            log.info("加载更多：page=%d 取到 %d 首，新增 %d 首，队列共 %d 首",
                     page, len(items), added, len(self.queue))
            return {"ok": True, "added": added,
                    "exhausted": added == 0, "count": len(self.queue),
                    "page": self.page}

    async def search(self, source: str, q: str, limit: int) -> list[dict[str, Any]]:
        q = (q or "").strip()
        if not q:
            raise HTTPException(400, "搜索词不能为空")
        if source == "qq":
            assert self.qq_search is not None
            return [_qq_item(it) for it in await self.qq_search(q, limit)]
        if self.nav is None:
            raise HTTPException(503, "这台服务器没配置 Navidrome 账号")
        return await self.nav.search(q, limit)

    async def _url_of(self, item: dict[str, Any]) -> str:
        if int(item.get("back") or 0) == 1:
            assert self.qq_url is not None
            return await self.qq_url(item["id"])
        assert self.nav is not None
        return self.nav.stream_url(item["id"])

    # ---- 播放控制 ----
    async def _start_index(self, i: int) -> None:
        """从队列第 i 首开始放。调用方必须持有 self.lock。"""
        item = self.queue[i]
        # ★ 先把 index 记成"要放的这首"，再去让 mpv 加载：加载失败时界面也会显示
        #   "这首放不了"，而不是停在上一次成功的曲目上（状态会跟实际脱节）。
        self.index = i
        url = self._next_url if (self._next_idx == i and self._next_url) else await self._url_of(item)
        self._next_url, self._next_idx = None, -1
        self.error = None
        try:
            await self.mpv.ensure()
            await self.mpv.load(url)
        except Exception as e:                          # noqa: BLE001
            self.error = f"播放失败：{e}"
            log.warning("[%d/%d] %s —— %s", i + 1, len(self.queue), item.get("title"), self.error)
            raise HTTPException(502, self.error) from e
        self._err_streak = 0
        log.info("开始播放 [%d/%d] %s - %s（%s，%s）", i + 1, len(self.queue),
                 item.get("title"), item.get("artist"), item.get("suffix"),
                 "QQ" if item.get("back") else "Navidrome")
        asyncio.create_task(self._prefetch_next())

    async def _prefetch_next(self) -> None:
        """提前解下一首的 URL —— 换歌时那 ~0.1s 的缝主要就耗在解地址上。"""
        if len(self.queue) < 2 or self.index < 0:
            return
        nxt = (self.index + 1) % len(self.queue)
        try:
            url = await self._url_of(self.queue[nxt])
        except Exception as e:                          # noqa: BLE001
            log.info("预取下一首 URL 失败（不影响当前播放）: %s", e)
            return
        if self.index + 1 == nxt or nxt == 0:           # 期间没换过歌才采用
            self._next_url, self._next_idx = url, nxt

    async def _on_end(self, reason: str) -> None:
        """mpv 播完一首（或出错）。reason 只有 eof/error 会进来（stop 被过滤掉了）。"""
        # ★ DAC 拔掉引起的 error 不是"播放失败"：
        #   不计入连续失败、也**不要自动切下一首**（切了也一样出不了声，还会把队列刷乱）。
        if reason == "error" and not dac_present(self.mpv.device):
            pos = 0
            with contextlib.suppress(Exception):
                tp = await self.mpv.get("time-pos")
                if tp is not None:
                    pos = int(float(tp) * 1000)
            self._mark_dac_lost(pos)
            return
        async with self.lock:
            if reason == "error":
                self._err_streak += 1
                if self._err_streak >= 3:
                    self.error = "连续 3 首播放失败，已停止（看 data/mpv.log）"
                    log.warning("%s", self.error)
                    return
            else:
                self._err_streak = 0
            if not self.queue:
                return
            if self.index < 0:
                return
            # ⚠️ 这里在后台任务里（mpv 事件触发的），抛出去没人接 —— 必须自己兜住
            try:
                await self._start_index((self.index + 1) % len(self.queue))
            except Exception as e:                      # noqa: BLE001
                log.warning("自动切下一首失败: %s", e)

    # ---- USB DAC 热插拔（看门狗）----
    def start_dac_watch(self) -> None:
        """启动看门狗。开机时 DAC 就不在也照样起 —— 点播时给明确的提示，插上自动开始。"""
        self.dac_ok = dac_present(self.mpv.device)
        if not self.dac_ok:
            log.warning("启动时没找到 USB DAC（%s）—— 插上后就能播", self.mpv.device)
        self._dac_task = asyncio.create_task(self._dac_watch())

    async def _dac_watch(self) -> None:
        while True:
            await asyncio.sleep(1.5)
            try:
                ok = dac_present(self.mpv.device)
            except Exception:                       # noqa: BLE001
                continue
            if ok == self.dac_ok:
                continue
            if ok:
                await self._dac_back()
            else:
                await self._dac_lost()

    def _mark_dac_lost(self, pos_ms: int = 0) -> None:
        if not self.dac_ok and self.dac_lost:
            return                                   # 已经处理过，别刷日志
        self.dac_ok = False
        self.dac_lost = True
        self.dac_lost_idx = self.index
        self.dac_pos_ms = max(0, int(pos_ms))
        self.error = "USB DAC 已拔出（插上会自动继续）"
        log.warning("USB DAC 拔掉了（%s）：停在 %d.%03d 秒，插回来会接着放",
                    self.mpv.device, self.dac_pos_ms // 1000, self.dac_pos_ms % 1000)

    async def _dac_lost(self) -> None:
        pos = 0
        if self.mpv.alive:
            with contextlib.suppress(Exception):
                tp = await self.mpv.get("time-pos")
                if tp is not None:
                    pos = int(float(tp) * 1000)
        self._mark_dac_lost(pos)
        # 让 mpv 空闲下来：别对着一个已经不存在的 pcm 一直写、一直报错刷日志
        if self.mpv.alive:
            with contextlib.suppress(Exception):
                await self.mpv.stop()

    async def _dac_back(self) -> None:
        self.dac_ok = True
        log.warning("USB DAC 插回来了（%s）", self.mpv.device)

        # ① DAC 不在的时候用户点过播 → 现在补上
        pending, self._pending_play = self._pending_play, None
        if pending:
            try:
                if pending.get("kind") == "id":
                    await self.play_id(pending["source"], pending["id"])
                else:
                    await self.play_mode(pending["source"], pending["mode"],
                                         pending.get("index", 0),
                                         pending.get("limit", 60),
                                         pending.get("skip", 0))
                self.dac_lost = False
                self.error = None
                log.warning("DAC 插回后按「等待中的点播请求」开播：%s",
                            pending.get("mode") or pending.get("id"))
                return
            except Exception as e:                  # noqa: BLE001
                log.warning("DAC 插回后补播失败: %s", e)

        # ② 拔掉时正在放的那首 → 接着放
        idx = self.dac_lost_idx
        if not self.dac_lost or not (0 <= idx < len(self.queue)):
            self.dac_lost = False
            return
        async with self.lock:
            item = self.queue[idx]
            self.index = idx
            # ⚠️ Navidrome 带 timeOffset 会让服务器转码 → 破坏"无损"，
            #    所以 Nav 来源**从头重放**（跟"切回本机保证无损"一个逻辑）；
            #    QQ 是静态文件（mpv 走 HTTP Range），可以真的续播。
            pos = self.dac_pos_ms if int(item.get("back") or 0) == 1 else 0
            self.dac_lost = False
            self.dac_pos_ms = 0
            note = ("从 %.1f 秒继续" % (pos / 1000.0)) if pos else "从头重放（保证无损）"
            try:
                url = await self._url_of(item)
                await self.mpv.ensure()
                await self.mpv.load(url, start_ms=pos)
                self.error = None
                log.warning("DAC 恢复播放：%s - %s（%s）",
                            item.get("title"), item.get("artist"), note)
            except Exception as e:                  # noqa: BLE001
                # ao 已经废了：整个重起 mpv 再试一次
                log.warning("DAC 恢复首次失败，重起 mpv 再试: %s", e)
                try:
                    await self.mpv.restart()
                    url = await self._url_of(item)
                    await self.mpv.load(url, start_ms=pos)
                    self.error = None
                    log.warning("DAC 恢复播放（重起 mpv 后成功）：%s", item.get("title"))
                except Exception as e2:             # noqa: BLE001
                    self.error = f"DAC 插回来了但恢复播放失败：{e2}"
                    log.error("%s", self.error)

    def _need_dac(self, pending: dict[str, Any] | None = None) -> None:
        """DAC 不在就别去开播（顺手记下请求，插上自动播）。"""
        if self.dac_ok:
            return
        if pending is not None:
            self._pending_play = pending
        raise HTTPException(503, "USB DAC 没插（插上会自动开始播放）")

    async def play_mode(self, source: str, mode: str, index: int, limit: int,
                        skip: int = 0) -> dict[str, Any]:
        """按模式建队列并开播。

        skip：QQ「收藏」里**置顶的 N 首**要跳过去（固件那边叫 fav_skip，用户置顶了 5 首，
              希望进收藏时直接从第 6 首开始）。队列仍然保留全部歌 —— 只是起播位置往后挪，
              这样"上一首"还能退回去看置顶那几首。
        """
        async with self.lock:
            label, items = await self._list(source, mode, limit)
            if not items:
                raise HTTPException(404, f"{source}/{mode} 没取到歌")
            self._need_dac({"kind": "mode", "source": source, "mode": mode,
                            "index": index, "limit": limit, "skip": skip})
            self.source = source
            self.mode_key = mode
            self.mode_label = label
            self.queue = items
            self.page = 1
            self.gen += 1
            self._next_url, self._next_idx = None, -1
            start = max(0, min(index + max(0, skip), len(items) - 1))
            await self._start_index(start)
        return await self.status()

    async def play_id(self, source: str, sid: str) -> dict[str, Any]:
        self._need_dac({"kind": "id", "source": source, "id": sid})
        if source == "qq":
            assert self.qq_item is not None
            item = _qq_item(await self.qq_item(sid))
        else:
            if self.nav is None:
                raise HTTPException(503, "这台服务器没配置 Navidrome 账号")
            item = await self.nav.get_song(sid)

        async with self.lock:
            # 插到当前播放位置之后（跟固件「收藏里点一首就播」的观感一致）
            at = self.index + 1 if 0 <= self.index < len(self.queue) else len(self.queue)
            if not any(x["id"] == item["id"] and x["back"] == item["back"] for x in self.queue):
                self.queue.insert(at, item)
            else:
                at = next(i for i, x in enumerate(self.queue)
                          if x["id"] == item["id"] and x["back"] == item["back"])
            self.source = source
            self.mode_key = "single"
            self.mode_label = "单曲"
            self.gen += 1
            self._next_url, self._next_idx = None, -1
            await self._start_index(at)
        return await self.status()

    async def step(self, delta: int) -> dict[str, Any]:
        self._need_dac()
        async with self.lock:
            if not self.queue:
                raise HTTPException(409, "队列是空的，先挑个列表或单曲")
            self._next_url, self._next_idx = None, -1
            n = len(self.queue)
            await self._start_index(((self.index if self.index >= 0 else 0) + delta) % n)
        return await self.status()

    async def stop(self) -> dict[str, Any]:
        async with self.lock:
            self.index = -1
            self._next_url, self._next_idx = None, -1
            if self.mpv.alive:
                await self.mpv.stop()
        return await self.status()

    async def set_pause(self, want: bool) -> dict[str, Any]:
        if not self.mpv.alive:
            raise HTTPException(503, "mpv 没在跑")
        idle = await self.mpv.get("idle-active")
        if idle:
            raise HTTPException(409, "现在没有在放歌")
        await self.mpv.pause(want)
        return await self.status()

    async def toggle(self) -> dict[str, Any]:
        if not self.mpv.alive:
            raise HTTPException(503, "mpv 没在跑")
        if await self.mpv.get("idle-active"):
            raise HTTPException(409, "现在没有在放歌")
        await self.mpv.pause(not bool(await self.mpv.get("pause")))
        return await self.status()

    async def seek(self, pos_ms: int) -> dict[str, Any]:
        if not self.mpv.alive:
            raise HTTPException(503, "mpv 没在跑")
        if await self.mpv.get("idle-active"):
            raise HTTPException(409, "现在没有在放歌")
        dur = 0.0
        with contextlib.suppress(Exception):
            dur = float(await self.mpv.get("duration") or 0)
        ms = max(0, pos_ms)
        if dur > 0:
            ms = min(ms, int(dur * 1000) - 500)
        await self.mpv.seek_ms(ms)
        return await self.status()

    async def set_volume(self, level: int) -> dict[str, Any]:
        level = max(0, min(100, int(level)))
        self.volume = level
        if self.mpv.alive:
            with contextlib.suppress(Exception):
                await self.mpv.set("volume", level)
        # 落盘去抖：音量可能是连续拖动，别每次都写 eMMC
        if self._vol_task is not None and not self._vol_task.done():
            self._vol_task.cancel()
        self._vol_task = asyncio.create_task(self._save_volume_soon())
        return await self.status()

    async def _save_volume_soon(self) -> None:
        await asyncio.sleep(3)
        self.cfg["volume"] = self.volume
        save_cfg(self.cfg)

    # ---- 状态 ----
    async def status(self) -> dict[str, Any]:
        st: dict[str, Any] = {
            "ok": True, "device": "server",
            "state": "stopped", "pos_ms": 0, "dur_ms": 0,
            "volume": self.volume,
            "source": self.source,
            "song": {},
            "queue": {"index": self.index, "count": len(self.queue),
                      "mode": self.mode_label, "mode_key": self.mode_key,
                      "back": 1 if self.source == "qq" else 0 if self.source else -1},
            "gen": self.gen, "mpv_ok": self.mpv.alive,
            "device_id": self.mpv.device, "error": self.error,
            "audio": {},
            # ★ USB DAC 在位状态：拔掉时网页/设备能直接显示"插上会自动继续"，
            #   而不是让人以为播放器坏了。
            "dac": {"present": self.dac_ok, "device": self.mpv.device},
        }
        if 0 <= self.index < len(self.queue):
            st["song"] = self.queue[self.index]
            st["dur_ms"] = int(self.queue[self.index].get("dur_ms") or 0)
        if not self.dac_ok:
            st["state"] = "paused"
            st["pos_ms"] = self.dac_pos_ms
            st["error"] = self.error or "USB DAC 已拔出（插上会自动继续）"
            return st
        if not self.mpv.alive:
            return st
        try:
            if await self.mpv.get("idle-active"):
                return st
            st["state"] = "paused" if await self.mpv.get("pause") else "playing"
            tp = await self.mpv.get("time-pos")
            if tp is not None:
                st["pos_ms"] = int(float(tp) * 1000)
            d = await self.mpv.get("duration")
            if d:
                st["dur_ms"] = int(float(d) * 1000)
            ap = await self.mpv.get("audio-params") or {}
            st["audio"] = {"samplerate": ap.get("samplerate"),
                           "format": ap.get("format"),
                           "channels": ap.get("channel-count")}
        except Exception as e:                          # noqa: BLE001
            st["error"] = f"读 mpv 状态失败：{e}"
        return st


# ============================================================ 路由

router = APIRouter(prefix="/v1/player", tags=["player"])
_player: Player | None = None
_qq_modes: list[dict[str, Any]] = []


def get_player() -> Player:
    if _player is None:
        raise HTTPException(503, "播放器没初始化")
    return _player


@router.get("/status")
async def player_status() -> dict[str, Any]:
    """一眼看全：在放什么、放没放、位置/时长/音量、实际输出格式（能验 24bit）。"""
    return await get_player().status()


@router.get("/play")
async def player_play(source: str = Query(...), mode: str | None = None,
                      index: int = 0, id: str | None = None,
                      limit: int = 60, skip: int = 0) -> dict[str, Any]:
    """建队列并开播。

    source = qq | nav
    mode   = 模式键（见 /v1/player/modes），给了就整列表建队列从 index 起播
    id     = 单曲（给了它就不用 mode）
    skip   = 起播位置再往后挪几首（QQ 收藏跳过置顶，固件传 fav_skip）
    """
    if source not in ("qq", "nav"):
        raise HTTPException(400, "source 只能是 qq 或 nav")
    p = get_player()
    if id:
        return await p.play_id(source, id)
    if not mode:
        raise HTTPException(400, "要带 mode（整列表播）或 id（单曲）")
    return await p.play_mode(source, mode, index, max(1, min(limit, 100)), max(0, skip))


@router.get("/pause")
async def player_pause() -> dict[str, Any]:
    return await get_player().set_pause(True)


@router.get("/resume")
async def player_resume() -> dict[str, Any]:
    return await get_player().set_pause(False)


@router.get("/toggle")
async def player_toggle() -> dict[str, Any]:
    return await get_player().toggle()


@router.get("/next")
async def player_next() -> dict[str, Any]:
    return await get_player().step(1)


@router.get("/prev")
async def player_prev() -> dict[str, Any]:
    return await get_player().step(-1)


@router.get("/stop")
async def player_stop() -> dict[str, Any]:
    return await get_player().stop()


@router.get("/seek")
async def player_seek(pos_ms: int = Query(...)) -> dict[str, Any]:
    return await get_player().seek(pos_ms)


@router.get("/volume")
async def player_volume(level: int = Query(...)) -> dict[str, Any]:
    return await get_player().set_volume(level)


@router.get("/queue")
async def player_queue() -> dict[str, Any]:
    p = get_player()
    return {"source": p.source, "mode": p.mode_label, "mode_key": p.mode_key,
            "index": p.index, "count": len(p.queue), "page": p.page,
            "gen": p.gen, "items": p.queue}


@router.get("/more")
async def player_more(n: int = 60) -> dict[str, Any]:
    """列表加载更多：把当前模式下一页追加进队列（网页往下滚到底时调）。"""
    return await get_player().more(max(10, min(n, 100)))


@router.get("/modes")
async def player_modes() -> dict[str, Any]:
    """服务器这侧两个来源分别能放哪些「模式」（固件的模式菜单用这份）。

    每项都带 `index`：固件那边是数字下标（0/1/2...），直接按 index 对，
    不用去认字符串，省得两边名字改一个就错位。
    """
    nav_ok = _player is not None and _player.nav is not None
    qq_modes = [{"index": i, **m} for i, m in enumerate(_qq_modes)]
    return {"sources": [
        {"back": 1, "name": "QQ音乐", "available": True, "modes": qq_modes},
        {"back": 0, "name": "Navidrome", "available": nav_ok, "modes": NAV_MODES},
    ]}


@router.get("/search")
async def player_search(source: str = Query(...), q: str = Query(...),
                        limit: int = 20) -> dict[str, Any]:
    """搜歌（网页控制台用）。QQ 和 Navidrome 各走各的上游，返回同一种项。"""
    if source not in ("qq", "nav"):
        raise HTTPException(400, "source 只能是 qq 或 nav")
    items = await get_player().search(source, q, max(1, min(limit, 50)))
    return {"source": source, "items": items, "count": len(items)}


@router.get("/cover")
async def player_cover(id: str = Query(...), back: int = 0, size: int = 300) -> Any:
    """封面（网页控制台用）。

    QQ  ：直接 302 到 y.qq.com 的固定尺寸模板 —— 浏览器自己缓存，服务端零开销。
    Nav ：由服务端带账号令牌去 Navidrome 取回来再转发。
          ⚠️ 不能让浏览器直连 Navidrome：Subsonic 的封面接口要账号令牌，
             塞进网页 = 把账号给所有能打开这个页面的人。
    """
    if back == 1:
        return RedirectResponse(
            f"http://y.qq.com/music/photo_new/T002R300x300M000{id}.jpg",
            status_code=302)
    p = get_player()
    if p.nav is None:
        raise HTTPException(503, "这台服务器没配置 Navidrome 账号")
    try:
        data, ctype = await p.nav.cover(id, max(60, min(size, 800)))
    except Exception as e:                              # noqa: BLE001
        raise HTTPException(502, f"取封面失败: {e}") from e
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "max-age=86400"})


@router.get("/devices")
async def player_devices() -> dict[str, Any]:
    """机器上有哪些声卡。只用于调试/以后的设置页 —— 当前固定用 player.json 里的 device。"""
    cards: list[dict[str, Any]] = []
    try:
        txt = Path("/proc/asound/cards").read_text("utf-8", "replace")
        cur: dict[str, Any] | None = None
        for line in txt.splitlines():
            if not line.strip():
                continue
            if line[0].isdigit() or line.startswith(" "):
                if "[" in line and "]" in line and ":" in line:
                    idx = line.split("[")[0].strip()
                    cid = line.split("[")[1].split("]")[0].strip()
                    rest = line.split(":", 1)[1].strip() if ":" in line else ""
                    cur = {"card": int(idx), "id": cid, "name": rest,
                           "device": f"alsa/hw:CARD={cid},DEV=0"}
                    cards.append(cur)
    except Exception as e:                              # noqa: BLE001
        return {"devices": [], "error": str(e), "current": _player.mpv.device if _player else None}
    return {"devices": cards, "current": _player.mpv.device if _player else None}


# ============================================================ 生命周期（app.py 调）

async def startup(qq_item, qq_url, qq_list, qq_search,
                  qq_modes: list[dict[str, Any]]) -> None:
    global _player, _qq_modes
    _qq_modes = qq_modes
    cfg = load_cfg()
    navcfg = cfg.get("nav") or {}
    nav = None
    if navcfg.get("user") and navcfg.get("password"):
        nav = Nav(navcfg.get("host", "http://127.0.0.1:4533"),
                  navcfg["user"], navcfg["password"])
    mpv = Mpv(SOCK_PATH, cfg["device"], int(cfg.get("volume", 80)))
    p = Player(cfg, mpv, nav)
    p.qq_item, p.qq_url, p.qq_list, p.qq_search = qq_item, qq_url, qq_list, qq_search
    _player = p
    # mpv 起不来不要让整个服务挂掉 —— /v1/* 的 QQ 契约接口还得能用
    try:
        await mpv.start()
    except Exception as e:                              # noqa: BLE001
        log.error("mpv 起不来，服务器播放暂不可用: %s", e)
    if nav is not None:
        try:
            await nav.check()
            log.info("Navidrome 就绪: %s (user=%s)", nav.base, nav.user)
        except Exception as e:                          # noqa: BLE001
            log.warning("Navidrome 连不上（只影响 nav 来源）: %s", e)
    else:
        log.warning("没配 Navidrome 账号（data/player.json 的 nav 段），nav 来源不可用")

    # USB DAC 看门狗：拔掉→记住位置停下；插回→重起 mpv 接着放。
    # 开机时 DAC 就不在也没关系（mpv 照常起来待命，点播时给明确提示）。
    p.start_dac_watch()


async def shutdown() -> None:
    if _player is None:
        return
    if _player._dac_task is not None:
        _player._dac_task.cancel()
    with contextlib.suppress(Exception):
        _player.cfg["volume"] = _player.volume
        save_cfg(_player.cfg)
    await _player.mpv.shutdown()
    if _player.nav is not None:
        await _player.nav.close()
