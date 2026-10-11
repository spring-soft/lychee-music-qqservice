"""ESP-Music ← QQ 音乐 契约层.

这个服务存在的理由（别把它当成"又一个 QQ 音乐 API 代理"）：

  上游 `qqmusic-api-python` 随时会因 QQ 改接口而变，而**固件不能跟着变** ——
  每次上游一变就要重刷 ESP32 是灾难。所以这里定义一套**我们自己的、冻结的**
  契约（/v1/*），固件只依赖它。上游怎么变，只改这个容器。

固件侧的硬性要求（都有代码依据，改动前先读一遍）：

  1. **音频和封面都走【直连】** —— 本服务**不做任何代理**（2026-10-11 改的）。
     QQ 音乐在国内全部可直连：vkey + guid 都在 URL 里，实测**不带任何请求头**就能取到
     （206 + Range 可用）；而且固件 `ae_source_http_new(url)` 只吃一个 URL，
     直链正合适。详见文件里 `这里的音频代理被删掉` 那段注释。
  2. 列表里 `suffix` 只能是 `mp3/flac/aac/m4a`（固件 `suffix_direct_playable()` 白名单），
     且 `size`/`duration` 必须填准 —— 固件用它们算码率决定"直出还是转码"（阈值 300KB/s）。
  3. `cover` 是**专辑 mid**（16 字符，短且稳定）。固件自己拼封面直链：
     `http://y.qq.com/music/photo_new/T002R300x300M000{cover}.jpg`
     ⚠️ 尺寸只能取 90/120/150/300/500/800，其它一律 404（固定用 300 最省事）。
  4. `id` 必须**稳定**（会被写进固件的收藏/最近播放），**不能把音质挡位编进 id**。
  5. 播放地址**必须播放前现取**（`GET /v1/url`）—— vkey 的 `expiration` 只有 2 小时，
     不能把它提前存进队列。

音质策略（刻意保守）：只走 FLAC → 320k mp3 → 128k mp3。
  **不用 OGG 挡位**（QQ 的 HQ 高品质是 OGG，扩展名 `.ogg` 不在固件白名单里），
  **不用臻品母带/全景声/DTS**（`size_new[0]` 是 24bit/192kHz，码率远超固件阈值，
  而且扩展名是 .mp4/.nac，固件解不了）。

登录：扫码（POST /v1/login/qr + GET /v1/login/status），或直接导入凭据 JSON。
  凭据落在 /data/credential.json，**会过期** —— 所以 /v1/health 是第一个要看的接口。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from qqmusic_api import Client, Credential
from qqmusic_api.core.exceptions import (
    CredentialExpiredError,
    CredentialInvalidError,
    RatelimitedError,
)
from qqmusic_api.modules.login import QRLoginType
from qqmusic_api.modules.search import SearchType
from qqmusic_api.modules.song import SongFileInfo, SongFileType, SongQueryInfo

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("qqmusic")

DATA_DIR = Path(os.environ.get("QQ_DATA_DIR", "/data"))
CRED_PATH = DATA_DIR / "credential.json"

# 封面 URL 模板（**给固件参考**：本服务不代理封面，固件自己拼这个 URL 直连）。
#   格式：http://y.qq.com/music/photo_new/T002R{size}x{size}M000{albumMid}.jpg
#   `albumMid` = 列表接口返回的 `cover` 字段（16 字符、稳定，适合当封面缓存 key）。
#
# ⚠️ 它只认【特定尺寸】—— 不是给多少都行。实测：
#     90 / 120 / 150 / 300 / 500 / 800 -> 200
#     40 / 50 / 60 / 100 / 160 / 200    -> 404
#   设备屏幕 160×128，**固定用 300** 最省事，不需要做尺寸映射。
#   （http 和 https 都通；实测不带任何请求头也能取到。）
COVER_SIZES = [90, 120, 150, 300, 500, 800]
COVER_TMPL = "http://y.qq.com/music/photo_new/T002R{s}x{s}M000{mid}.jpg"

# 播放地址兜底域名。库里定义了 `_SONG_URL_FALLBACK_DOMAIN` 却**从未使用** ——
# purl 是相对路径，拼 URL 是留给调用方的。优先用 cdn_dispatch 返回的 sip。
CDN_FALLBACK = "https://isure.stream.qqmusic.qq.com/"

# 音质挡位链：从高到低，第一个"有文件"的胜出。
#   第 2 列是库的文件类型，第 3 列是对应 `File` 模型里的大小字段，第 4 列是给固件的后缀。
QUALITY_CHAIN: list[tuple[str, SongFileType, str, str]] = [
    ("flac", SongFileType.FLAC,    "size_flac",   "flac"),   # SQ 无损
    ("320",  SongFileType.MP3_320, "size_320mp3", "mp3"),    # HQ 高品质
    ("128",  SongFileType.MP3_128, "size_128mp3", "mp3"),    # 标准音质
]
QUALITY_NAMES = [q[0] for q in QUALITY_CHAIN]

# 上游 `UrlinfoItem.result` 的结果码 -> 人话
RESULT_MSG = {
    0: "成功",
    104003: "没有播放权限（可能是付费单曲 / 会员等级不够）",
    104004: "VKey 获取失败（凭据可能已失效）",
    104013: "播放设备受限（账号被风控）",
}

_state: dict[str, Any] = {
    "client": None,
    "cred": None,
    "cdn_sip": [],
    "cdn_at": 0.0,
    "detail_cache": {},      # mid -> (Song, 时间戳)
    # 扫码登录状态（mobile 走 MQTT 推送，由后台任务写这里）
    "login": {"gen": 0},
    "login_task": None,
}
DETAIL_TTL = 300.0


# ----------------------------------------------------------------- 凭据

def load_cred() -> Credential | None:
    if not CRED_PATH.exists():
        return None
    try:
        return Credential.model_validate(json.loads(CRED_PATH.read_text("utf-8")))
    except Exception as e:                                   # noqa: BLE001
        log.error("读凭据失败: %s", e)
        return None


def save_cred(cred: Credential) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CRED_PATH.write_text(cred.model_dump_json(indent=2), "utf-8")
    try:
        os.chmod(CRED_PATH, 0o600)          # 里面是账号登录态
    except OSError:
        pass
    log.warning("凭据已保存到 %s", CRED_PATH)


def set_cred(cred: Credential | None) -> None:
    """把凭据同时挂到【客户端默认凭据】上 —— 这一步是必须的。

    ⚠️ 踩过的坑：一开始凭据只存在 _state["cred"] 里，靠每个接口显式 `credential=cred` 传。
    但**有些方法根本没有 credential 参数**（例如 songlist.get_detail 的签名是
    `(songlist_id, dirid, num, page, *, onlysong, tag, userinfo)`），这种就只能靠
    客户端的默认凭据。不挂的话它会拿空凭据去请求：
      · 公共歌单（百万收藏 / 新歌推荐）照样能拿 → 看不出问题
      · **个性化歌单（每日30首）直接 code=10004（无权限）**
    现象就是"别的模式都好，只有每日30首 502"，查起来很费劲。
    """
    _state["cred"] = cred
    if _state["client"] is not None:
        _state["client"].credential = cred


def cred_state() -> dict[str, Any]:
    c = _state["cred"]
    if c is None:
        return {"present": False, "valid": False, "expired": None}
    return {"present": True, "valid": c.is_valid(), "expired": c.is_expired(),
            "musicid": c.musicid}


def cred_or_401() -> Credential:
    c = _state["cred"]
    if c is None:
        raise HTTPException(401, "还没有登录：先 POST /v1/login/qr 扫码")
    return c


# ----------------------------------------------------------------- CDN / 工具

async def cdn_domain() -> str:
    """音频 CDN 根地址；失败退回兜底域名。缓存 10 分钟。"""
    now = time.time()
    if _state["cdn_sip"] and now - _state["cdn_at"] < 600:
        return _state["cdn_sip"][0]
    try:
        resp = await _state["client"].song.get_cdn_dispatch()
        sip = [s for s in (getattr(resp, "sip", None) or []) if s]
        if sip:
            _state["cdn_sip"], _state["cdn_at"] = sip, now
            log.info("CDN 根地址: %s", sip[0])
            return sip[0]
    except Exception as e:                                   # noqa: BLE001
        log.warning("cdn_dispatch 失败（用兜底域名）: %s", e)
    return CDN_FALLBACK


def join_url(domain: str, purl: str) -> str:
    if purl.startswith(("http://", "https://")):
        return purl
    return domain.rstrip("/") + "/" + purl.lstrip("/")


def pick_quality(song: Any, want: str | None = None) -> dict[str, Any] | None:
    """按挡位链选一个"有文件、且扩展名在固件白名单里"的音质。"""
    f = getattr(song, "file", None)
    if f is None:
        return None
    chain = QUALITY_CHAIN
    if want in QUALITY_NAMES:           # 指定挡位：从它开始往下退
        chain = QUALITY_CHAIN[QUALITY_NAMES.index(want):]
    for name, ftype, size_attr, suffix in chain:
        size = int(getattr(f, size_attr, 0) or 0)
        if size > 0:
            return {"quality": name, "file_type": ftype, "size": size, "suffix": suffix}
    return None


def song_to_item(song: Any, want: str | None = None) -> dict[str, Any] | None:
    q = pick_quality(song, want)
    if q is None:
        return None
    singers = " / ".join(getattr(s, "name", "") for s in (getattr(song, "singer", None) or []))
    album = getattr(song, "album", None)
    return {
        # ★ id 只用 mid，绝不含音质挡位 —— 换挡位不能让收藏失效
        "id": song.mid,
        "title": song.name or song.title or "",
        "artist": singers,
        "album": getattr(album, "name", "") if album else "",
        "duration": int(getattr(song, "interval", 0) or 0),
        "suffix": q["suffix"],
        "size": q["size"],
        "quality": q["quality"],
        "cover": (getattr(album, "mid", "") if album else "") or "",
    }


async def fetch_song(mid: str) -> Any:
    """取歌曲详情（带 5 分钟缓存 —— 设备会先搜索再播放，别重复打上游）。"""
    now = time.time()
    hit = _state["detail_cache"].get(mid)
    if hit and now - hit[1] < DETAIL_TTL:
        return hit[0]
    resp = await _state["client"].song.query_song([SongQueryInfo(mid=mid)])
    tracks = getattr(resp, "tracks", None) or []
    if not tracks:
        raise HTTPException(404, f"上游查不到这首 mid={mid}")
    song = tracks[0]
    _state["detail_cache"][mid] = (song, now)
    if len(_state["detail_cache"]) > 500:            # 简单防膨胀
        for k in sorted(_state["detail_cache"], key=lambda k: _state["detail_cache"][k][1])[:200]:
            _state["detail_cache"].pop(k, None)
    return song


async def resolve_play_url(mid: str, want: str | None) -> dict[str, Any]:
    """mid -> {url, quality, suffix, size, filename}"""
    cred = cred_or_401()
    song = await fetch_song(mid)
    q = pick_quality(song, want)
    if q is None:
        raise HTTPException(404, "这首歌没有任何可用音质（可能全都需要付费）")
    media_mid = getattr(getattr(song, "file", None), "media_mid", "") or None
    try:
        resp = await _state["client"].song.get_song_urls(
            [SongFileInfo(mid=mid, file_type=q["file_type"], media_mid=media_mid)],
            q["file_type"], credential=cred,
        )
    except (CredentialExpiredError, CredentialInvalidError) as e:
        raise HTTPException(401, f"凭据失效，重新扫码: {e}") from e
    except RatelimitedError as e:
        raise HTTPException(429, f"被限流: {e}") from e
    items = getattr(resp, "data", None) or []
    if not items:
        raise HTTPException(502, "上游没返回播放地址")
    u = items[0]
    code = int(getattr(u, "result", 0) or 0)
    if code != 0 or not getattr(u, "purl", ""):
        raise HTTPException(403, f"拿不到播放地址（result={code}）：{RESULT_MSG.get(code, '未知')}")
    return {
        "url": join_url(await cdn_domain(), u.purl),
        "quality": q["quality"], "suffix": q["suffix"], "size": q["size"],
        "filename": getattr(u, "filename", ""), "expiration": int(getattr(resp, "expiration", 0) or 0),
    }


# ----------------------------------------------------------------- 应用

@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cred = load_cred()
    # ★ 凭据在构造时就交给 Client —— 这样那些【没有 credential 参数】的方法
    #   （如 songlist.get_detail）也会自动带上登录态，见 set_cred 的注释。
    _state["client"] = Client(credential=cred)
    set_cred(cred)
    log.info("凭据: %s", cred_state())
    try:
        yield
    finally:
        await _state["client"].close()


app = FastAPI(title="ESP-Music QQ 契约层", version="1.0", lifespan=lifespan)


# ---------------- /v1/health ----------------

@app.get("/v1/health")
async def health() -> dict[str, Any]:
    """一眼看出能不能用 —— 这个服务最重要的接口。

    这类逆向服务的头号故障是"凭据过期"，表现却是"突然所有歌都放不了"，最难查。
    所以把凭据状态、VIP、CDN、挡位链全摊开。
    """
    st: dict[str, Any] = {"ok": True, "credential": cred_state(),
                          "quality_chain": QUALITY_NAMES}
    c = _state["cred"]
    if c is not None:
        try:
            vip = await _state["client"].user.get_vip_info(credential=c)
            dump = vip.model_dump() if hasattr(vip, "model_dump") else {}
            st["vip"] = {"truthy_keys": sorted(k for k, v in dump.items() if v)[:12]}
        except (CredentialExpiredError, CredentialInvalidError) as e:
            st["ok"] = False
            st["credential"]["reason"] = f"{type(e).__name__}: {e}"
        except RatelimitedError as e:
            st["ok"] = False
            st["credential"]["reason"] = f"被限流（风控）: {e}"
        except Exception as e:                               # noqa: BLE001
            st["vip_error"] = f"{type(e).__name__}: {e}"
        st["cdn"] = await cdn_domain()
    return st


# ---------------- 登录 ----------------
#
# ⚠️⚠️ 这里踩过一个坑，记下来别再犯：
#
#   库有 3 种二维码登录（QRLoginType）：QQ / WX / MOBILE
#     · QQ     = **QQ 网页登录**（走 ssl.ptlogin2.qq.com/ptqrlogin）
#                —— 拿手机扫码会打开 **QQ 下载页**，跟 QQ 音乐授权毫无关系
#     · WX     = 微信扫码
#     · MOBILE = **手机 QQ 音乐 App 扫码**（走 music.login.LoginServer.CreateQRCode，
#                param 里 tmeAppID="qqmusic"）—— 这才是我们要的
#
#   而且校验方式也不同：
#     · QQ / WX 用 check_qrcode() 一次性轮询
#     · **MOBILE 必须用 checking_mobile_qrcode()** —— 它是**异步生成器**，
#       开一条 MQTT 连接（mu.y.qq.com:443/ws/handshake，
#       topic management.qrcode_login/<id>）持续接收服务端推送。
#       注意 check_qrcode() 内部【根本不处理 MOBILE】（只判 WX，其余都当 QQ）。
#
#   所以：MOBILE 的登录要在**后台任务**里消费那个生成器，HTTP 只读最新状态。

def _login_state() -> dict[str, Any]:
    st = _state["login"]
    return {"event": st.get("event"), "identifier": st.get("identifier"),
            "saved": st.get("saved", False), "error": st.get("error")}


async def _watch_mobile_login(qr: Any, gen: int, deadline_s: float = 300.0) -> None:
    """后台消费 MQTT 推送的扫码状态；DONE 时落盘凭据。"""
    log.info("开始监听扫码状态（MQTT）...")
    try:
        import anyio
        deadline = anyio.current_time() + deadline_s
        async for ev in _state["client"].login.checking_mobile_qrcode(qr, deadline):
            if gen != _state["login"].get("gen"):
                log.info("二维码已换代，停止旧监听")
                return
            name = ev.event.name
            _state["login"]["event"] = name
            log.info("扫码事件: %s", name)
            if ev.done and ev.credential is not None:
                set_cred(ev.credential)          # 同时挂到 Client 的默认凭据上
                save_cred(ev.credential)
                _state["login"].update({"event": "DONE", "saved": True})
                log.warning("登录成功，凭据已保存")
                return
    except Exception as e:                                   # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        _state["login"].update({"event": "ERROR", "error": msg})
        log.error("扫码监听失败: %s", msg)


@app.post("/v1/login/qr")
async def login_qr(type: str = "mobile") -> dict[str, Any]:
    """拿登录二维码。**默认 mobile**（手机 QQ 音乐 App 扫）。"""
    kind = {"mobile": QRLoginType.MOBILE, "wx": QRLoginType.WX, "qq": QRLoginType.QQ}.get(
        type.lower(), QRLoginType.MOBILE)
    # 换一张码就换一代，让旧的监听任务自己退出
    gen = int(_state["login"].get("gen", 0)) + 1
    qr = await _state["client"].login.get_qrcode(kind)
    # 保留 gen，其它字段重建
    _state["login"].clear()
    _state["login"].update({"gen": gen, "identifier": qr.identifier,
                            "event": "WAITING", "saved": False})
    prev = _state.get("login_task")
    if prev is not None and not prev.done():
        prev.cancel()
    if kind == QRLoginType.MOBILE:
        _state["login_task"] = asyncio.create_task(_watch_mobile_login(qr, gen))
    else:
        _state["login_task"] = None
    return {"type": type, "identifier": qr.identifier, "mimetype": qr.mimetype,
            "image_base64": base64.b64encode(qr.data).decode("ascii"),
            "_note": "mobile=手机QQ音乐App扫码；qq=QQ网页登录（扫出来是下载页，别用）"}


@app.get("/v1/login/status")
async def login_status(identifier: str | None = None) -> dict[str, Any]:
    """读扫码状态。

    mobile 流程是 MQTT 推送，状态由后台任务写进 _state，这里只读最新值 ——
    所以**不需要**（也不能）像 QQ 网页登录那样调 check_qrcode。
    """
    if identifier and identifier != _state["login"].get("identifier"):
        # 兼容老调用方式：给的是旧码，直接说清楚
        return {"event": "STALE", "identifier": identifier,
                "_note": "这不是当前二维码，重新 POST /v1/login/qr"}
    return _login_state()


@app.post("/v1/login/import")
async def login_import(payload: dict[str, Any]) -> dict[str, Any]:
    """备用：直接导入凭据 JSON（从别处导出来的）。"""
    cred = Credential.model_validate(payload)
    if not cred.is_valid():
        raise HTTPException(400, "凭据不完整（musicid / musickey 至少要有）")
    set_cred(cred)                            # 同时挂到 Client 的默认凭据上
    save_cred(cred)
    return {"ok": True, "musicid": cred.musicid}


# ---------------- 搜索 ----------------

@app.get("/v1/search")
async def search(q: str = Query(..., min_length=1), limit: int = 20,
                 quality: str | None = None) -> dict[str, Any]:
    limit = max(1, min(limit, 50))
    try:
        resp = await _state["client"].search.search_by_type(q, SearchType.SONG, num=limit)
        songs = getattr(resp, "song", None) or []
        items = [it for it in (song_to_item(s, quality) for s in songs) if it]
    except RatelimitedError as e:
        raise HTTPException(429, f"被限流，稍后再试: {e}") from e
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(502, f"上游搜索失败: {type(e).__name__}: {e}") from e
    return {"items": items, "count": len(items)}


# ---------------- 播放地址（调试用：给的是 QQ 直链，设备别用这个） ----------------

@app.get("/v1/url")
async def url_of(id: str = Query(...), q: str | None = None) -> dict[str, Any]:
    r = await resolve_play_url(id, q)
    return {**r, "_note": "QQ 直链，设备直接用它拉流（本服务【不做】音频代理，理由见下面那段注释）"}


# ⚠️ 这里【故意没有】音频代理接口（2026-10-11 删掉）。
#
# 原本有个 `GET /v1/stream` 把音频流代理给设备，理由是"固件不支持自定义请求头，
# 而 QQ 直链有时效 / 可能绑 IP"。—— 实测**三条全不成立**：
#   1) vkey + guid 都是 URL 里的查询参数 → **完全不带任何请求头，直连就返回 206**；
#   2) 链接 2 小时过期（expiration=7200）→ 设备本来就在播放前现取，不是问题；
#   3) 直链不返回 `Accept-Ranges` 头 → 固件里这个字段**只打日志、不参与任何判断**
#      （audio_source_http.c:87 是它唯一的读取处）。
# 再加上 vkey 直链只有 259 字符，固件 `char url[768]` 装得下。
# 结论：**直连**。谁想恢复代理，先回答"上面三条哪条变了"。


# ---------------- 模式列表（收藏 / 每日30首 / 刷歌 / 雷达 / 新歌） ----------------
#
# 设计成【一个接口 + mode 参数】而不是五个接口：固件那边只要一套解析、一套 UI。
#
# 这几个模式都是实测出来的（2026-10-11，真账号）：
#   fav   = QQ 账号的「我喜欢」(dirid 201)
#   daily = 「每日30首」——它不在库里封装，要靠首页 Feed 里那张卡片拿 id，
#           再当歌单取详情。★ 所以这里**动态查卡片 id**，不硬编码：
#           id 可能是跟人走的，硬编码哪天就失效了。
#   guess = 「猜你喜欢-沉浸刷歌」——**这就是"刷歌"**。
#           ★ 证据：首页 Feed 里那张卡片的标题就叫「猜你喜欢-沉浸刷歌」，id=99，
#             而这正是 get_guess_recommend 内部写死的 id。
#           实测连调三次结果完全不同（每次 5 首新歌）→ 天然适合"刷"。
#   radar = 「雷达推荐」（也有 has_more 可续推）
#   new   = 「新歌速递」（上游是 CGI 直调：newsong.NewSongServer）
#
# ⚠️ 试过但**不通**的（别再浪费时间）：
#   music.web_srf_svr / get_recommend（参考实现里的"每日推荐"）
#   music.web_fm_svr  / GetPrivateFmPlaylist（私人FM）
#   → 六种 uin/login/comm 组合全部返回 `code=500003`。
#     原因：那两个是 **web 模块**，认的是浏览器 cookie，不认我们这种 App 凭据。
# 卡片类模式：模式名 -> 首页 Feed 里那张卡片的标题。
# ★ 不硬编码 id，而是每次从首页 Feed 动态查（带 1 小时缓存）——
#   「每日30首」的 id 是**跟人走的**（实测 5725792700 是这个号的），硬编码早晚失效。
#   卡片 id 从 QQ 自己的元数据里来，最可靠。
CARD_MODES: dict[str, str] = {
    "daily":   "每日30首",
    "million": "百万收藏",
    "newrec":  "新歌推荐",
}
# 说明：同源的「歌手漫游」也能出歌，但偶发 code=80121（上游限流），
# 用的人决定不要（2026-10-11）。真要加：往上面这张表里加一行 `"roam": "歌手漫游"` 即可。

_card_cache: dict[str, Any] = {"map": {}, "at": 0.0}


def _g(o: Any, k: str, default: Any = None) -> Any:
    """既能取 dict 的键、也能取对象的属性 —— 上游这两种形态都出现过。

    ⚠️ 这里踩过坑：卡片列表在 model_dump() 里是 **dict**，但同一个响应里
     shelves/niches 是模型对象。一开始只用 getattr() 取 title，结果 dict 取不到，
     「每日30首」就"找不到"了（返回 502）。一个 _g() 两种都兼容。"""
    if isinstance(o, dict):
        return o.get(k, default)
    return getattr(o, k, default)


async def _card_index() -> dict[str, int]:
    """首页 Feed 的「卡片标题 -> id」索引（缓存 1 小时）。

    ⚠️ 缓存字典叫 _card_cache，**不能**跟这个函数同名 ——
       同名的后果是函数把自己遮蔽了，函数体里 `_card_index["map"]` 变成
       "对一个函数取下标" → `TypeError: 'function' object is not subscriptable`，
       三个卡片模式全 502。（这个坑我踩过一次，写在这里。）"""
    if _card_cache["map"] and time.time() - _card_cache["at"] < 3600:
        return _card_cache["map"]
    r = await _state["client"].recommend.get_home_feed(page=1)
    d = r.model_dump() if hasattr(r, "model_dump") else r
    m: dict[str, int] = {}
    for sh in (_g(d, "shelves") or []):
        for ni in (_g(sh, "niches") or []):
            for cd in (_g(ni, "cards") or []):
                t = _g(cd, "title") or ""
                i = _g(cd, "id") or ""
                if t and str(i).isdigit():
                    m[str(t)] = int(i)
    _card_cache.update({"map": m, "at": time.time()})
    log.info("首页卡片索引 %d 张: %s", len(m), list(m)[:10])
    return m


async def _retry(coro_factory, times: int = 2, delay: float = 1.5):
    """上游偶发失败重试一次。

    ⚠️ 实测「歌手漫游」会偶发 `CgiApiException code=80121`，隔几秒再请求就好了 ——
    那是上游限流/抖动，不是配置错。不重试的话设备上会看到"偶尔点进去是空的"。"""
    for i in range(times):
        try:
            return await coro_factory()
        except (CredentialExpiredError, CredentialInvalidError):
            raise
        except Exception:                                    # noqa: BLE001
            if i == times - 1:
                raise
            log.warning("上游失败，%.1fs 后重试（第 %d 次）", delay, i + 1)
            await asyncio.sleep(delay)


async def _songs_to_items(songs: Any, quality: str | None) -> list[dict[str, Any]]:
    return [it for it in (song_to_item(s, quality) for s in (songs or [])) if it]


@app.get("/v1/modes")
async def modes() -> dict[str, Any]:
    """列出可用的模式（给固件/前端看的说明书）。

    固件的「模式」菜单就用这份清单 —— 加/删模式只改服务端，固件不用重编。
    """
    return {"modes": [
        {"mode": "fav",     "name": "收藏",     "desc": "QQ 账号的「我喜欢」",        "kind": "user"},
        {"mode": "daily",   "name": "每日30首", "desc": "每天 30 首为你推荐",         "kind": "card"},
        {"mode": "million", "name": "百万收藏", "desc": "百万收藏歌单",               "kind": "card"},
        {"mode": "newrec",  "name": "新歌推荐", "desc": "首页卡片里的新歌推荐",        "kind": "card"},
        {"mode": "guess",   "name": "刷歌",     "desc": "猜你喜欢·沉浸刷歌，每次都不一样", "kind": "radio"},
        {"mode": "radar",   "name": "雷达",     "desc": "红心雷达：按你的收藏推荐（每条带推荐理由）", "kind": "radar"},
    ]}


@app.get("/v1/list")
async def list_by_mode(mode: str = Query(...), limit: int = 30,
                       quality: str | None = None) -> dict[str, Any]:
    """按模式取歌单。**所有模式返回同一个结构**，固件只写一套解析。"""
    limit = max(1, min(limit, 100))
    try:
        if mode == "fav":
            # 复用 favorites() 的取数逻辑，但**统一成同一结构**（固件只写一套解析）
            fav = await favorites(limit=limit, quality=quality)
            return {"mode": "fav", "label": "收藏", "items": fav["items"],
                    "count": fav["count"]}

        if mode in CARD_MODES:
            # 卡片类：先从首页 Feed 查标题对应的 id，再当歌单取详情
            title = CARD_MODES[mode]
            idx = await _card_index()
            sid = idx.get(title)
            if sid is None:
                raise HTTPException(502, f"首页 Feed 里没有「{title}」卡片（上游改版了？）")
            r = await _retry(lambda: _state["client"].songlist.get_detail(
                songlist_id=sid, num=limit, page=1))
            songs = getattr(r, "songs", None) or []
            label = title

        elif mode == "guess":
            r = await _retry(lambda: _state["client"].recommend.get_guess_recommend(
                credential=cred_or_401()))
            songs = getattr(r, "songs", None) or []
            label = "刷歌"

        elif mode == "radar":
            # 「红心雷达」：music.recommend.TrackRelationServer / GetRadarSong
            # 它的入参是 FavSongs(收藏的歌) + EntranceSongs(入口种子)，
            # 而且返回的每条都带 RadioRecTag.RecReasonTemplate（推荐理由），
            # 文案就是"根据你的钟爱为你推荐" —— 即按你的收藏个性化推荐。
            r = await _retry(lambda: _state["client"].recommend.get_radar_recommend(page=1))
            songs = getattr(r, "songs", None) or []
            label = "雷达"

        else:
            known = "/".join(["fav", *CARD_MODES, "guess", "radar"])
            raise HTTPException(400, f"未知模式 {mode}；可用：{known}")
    except RatelimitedError as e:
        raise HTTPException(429, f"被限流: {e}") from e
    except (CredentialExpiredError, CredentialInvalidError) as e:
        raise HTTPException(401, f"凭据失效，重新扫码: {e}") from e
    except HTTPException:
        raise
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(502, f"{mode} 取歌失败: {type(e).__name__}: {e}") from e

    items = await _songs_to_items(songs, quality)
    log.info("list mode=%s (%s) -> %d 首（上游 %d）", mode, label, len(items), len(songs or []))
    return {"mode": mode, "label": label, "items": items, "count": len(items)}


# ---------------- 封面 ----------------
#
# ⚠️ 同样【故意没有】封面代理接口（2026-10-11 删掉）。
# 封面 CDN 直连也通（实测 `http://y.qq.com/...` → 200），所以固件自己拼 URL 就行：
#
#     http://y.qq.com/music/photo_new/T002R{size}x{size}M000{albumMid}.jpg
#
# 两条必须注意的：
#   1) **尺寸只能取 90 / 120 / 150 / 300 / 500 / 800** —— 其它（40/60/100/160/200…）
#      一律 404。设备屏幕是 160×128，直接固定用 **300** 最省事，不用做映射。
#   2) `albumMid` 就是列表里返回的 `cover` 字段（16 字符，稳定，适合做封面缓存 key）。
#
# 完整模板常量见 COVER_TMPL / COVER_SIZES（仍保留在文件上部，供你拼 URL 时参考）。


# ---------------- 歌词 ----------------

@app.get("/v1/lyric", response_class=PlainTextResponse)
async def lyric(id: str = Query(...)) -> str:
    """返回 LRC 文本。拿不到就返回空串（固件那边歌词页空白，不影响播放）。"""
    try:
        resp = await _state["client"].lyric.get_lyric(id)
        for attr in ("lyric", "lrc", "lyric_text", "content"):
            v = getattr(resp, attr, None)
            if isinstance(v, str) and v.strip():
                return v
        log.info("歌词接口没找到已知字段，dump: %s",
                 list(resp.model_dump().keys()) if hasattr(resp, "model_dump") else "?")
    except Exception as e:                                   # noqa: BLE001
        log.warning("歌词失败 %s: %s", id, e)
    return ""


# ---------------- "我喜欢"（读 + 加；**不能删**，见 /v1/favorite 那段的说明） ----------------

@app.get("/v1/favorites")
async def favorites(limit: int = 50, quality: str | None = None) -> dict[str, Any]:
    """读 QQ 账号的"我喜欢"（dirid=201）。

    写回见 `POST /v1/favorite`：**能加，不能删**（上游对 dirId=201 的删除静默忽略）。

    ⚠️ 这里必须**再补一次 query_song**：上游的收藏列表只给 mid/歌名之类的摘要，
    **不带 file（各挡位字节数）和 interval（时长）**，而固件正是靠这两个算码率
    决定"直出还是转码"。不补的话这一路返回的数据固件直接吃不了。
    query_song 一次最多 100 个 mid，正好覆盖 limit 上限。
    """
    cred = cred_or_401()
    limit = max(1, min(limit, 100))
    try:
        resp = await _state["client"].user.get_fav_song(
            euin=cred.encrypt_uin, num=limit, credential=cred,
        )
    except (CredentialExpiredError, CredentialInvalidError) as e:
        raise HTTPException(401, f"凭据失效，重新扫码: {e}") from e
    except RatelimitedError as e:
        raise HTTPException(429, f"被限流: {e}") from e

    dump = resp.model_dump() if hasattr(resp, "model_dump") else {}
    raw = None
    for key in ("songs", "song", "list", "items", "data"):
        v = dump.get(key)
        if isinstance(v, list):
            raw = v
            break
    if raw is None:
        log.warning("favorites 认不出结构，keys=%s", list(dump.keys())[:20])
        return {"items": [], "count": 0, "_keys": list(dump.keys())[:20]}

    mids: list[str] = []
    for r in raw:
        if isinstance(r, dict):
            m = r.get("mid") or r.get("songmid") or r.get("songMid")
            if m:
                mids.append(str(m))
    if not mids:
        return {"items": [], "count": 0, "_note": "收藏里没解析出 mid"}

    try:
        detail = await _state["client"].song.query_song([SongQueryInfo(mid=m) for m in mids])
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(502, f"补详情失败: {type(e).__name__}: {e}") from e

    tracks = getattr(detail, "tracks", None) or []
    items = [it for it in (song_to_item(t, quality) for t in tracks) if it]
    log.info("favorites: 上游 %d 条 -> 可播 %d 条", len(mids), len(items))
    return {"items": items, "count": len(items)}


# ---------------- 写回「我喜欢」（红心） ----------------
#
# ★★ 结论先说：**只能加，不能删。**（2026-10-11 查清，别再走弯路）
#
# 【加】`music.musicasset.PlaylistDetailWrite / AddSonglist`（dirid=201）
#       —— 实测有效：歌真的进了「我喜欢」，列表和 songnum 都能看到。
#
# 【删】QQ 服务端对 dirId=201 的删除是**静默忽略**：
#       `PlaylistDetailWrite / DelSonglist` 返回 retCode=0，但**什么都不做**
#       （原始响应里 result.songlist 是空的、dirId 是 0）。
#       穷举过 4 类 10 种组合，全部无效：
#         · DelSonglist（就是库里 unlike_song 干的事）      -> 空操作
#         · + 真实 tid 5490354462                          -> 空操作
#         · + preserve_bool=True（对齐 Add 的写法）         -> 空操作
#         · dirId=tid                                      -> code=80092 参数无效
#         · music.musicasset.SongFavWrite × 6 种方法/参数   -> 全 500003（模块不存在）
#         · 参考实现 sansenjian/qq-music-api                -> 根本没有写操作
#       ⚠️ **库自己的测试是假阳性**（tests/test_songlist.py 的 test_like_song_roundtrip
#          只 `assert await unlike_song(...)`，而 del_songs 返回 `retCode == 0` 恒为 True，
#          从不回读列表确认歌真的没了）—— 所以"上游库验证过取消可用"是不可信的。
#
# 因此本接口：
#   on=1 -> 真写回，并**回读 songnum 做校验**（不重复那个假阳性错误）
#   on=0 -> **明确报错**，不做任何改动。设备侧据此提示用户"QQ 音乐里需手动取消"。
#
# 另外两条设计约束：
#   · 上游要**数字 `song_id` + `song_type`**，而设备只有 mid → 先 mid → (id,type)。
#     好在 fetch_song() 有 5 分钟缓存，设备刚播过/刚列过的歌基本都命中。
#   · 写操作比读更容易触发风控：一次只写一首、不做整表重写、失败明确返回不静默重试。


async def _fav_count() -> int | None:
    """读「我喜欢」的总数（`info.songnum`）。

    ★ 这是校验写操作最可靠的信号：它是**总数**，不受 limit 分页影响
      （这个号有 898 首，只读前 100 首根本判断不出加没加进去）。"""
    cred = _state["cred"]
    if cred is None:
        return None
    try:
        resp = await _state["client"].user.get_fav_song(
            euin=cred.encrypt_uin, num=1, credential=cred)
        dump = resp.model_dump() if hasattr(resp, "model_dump") else {}
        n = (dump.get("info") or {}).get("songnum")
        return int(n) if n is not None else None
    except Exception as e:                                   # noqa: BLE001
        log.warning("读收藏总数失败: %s", e)
        return None


@app.post("/v1/favorite")
async def favorite(id: str = Query(...), on: int = Query(1)) -> dict[str, Any]:
    """把一首歌**加入** QQ 账号的「我喜欢」。

    参数用 query 而不是 JSON body —— 固件拼 URL 比构造 POST body 省事得多。
      id  = 歌曲 mid（就是 /v1/list、/v1/search 返回的 `id`）
      on  = 1 加入（可用）；**0 移除会返回 409 —— 上游不支持删除**，见文件上方长注释
    """
    cred = cred_or_401()

    # ---- 取消：明确拒绝，且**不做任何本地/远端改动** ----
    if not on:
        raise HTTPException(
            status_code=409,
            detail=("QQ 上游不支持从「我喜欢」删除歌曲（DelSonglist 返回成功但静默忽略）。"
                    "请在 QQ 音乐 App 里手动取消。本次未做任何改动。"))

    song = await fetch_song(id)              # 顺带拿到数字 id / type（有缓存）
    raw_id = int(getattr(song, "id", 0) or 0)
    stype = int(getattr(song, "type", 0) or 1)
    if not raw_id:
        raise HTTPException(502, f"拿不到 {id} 的数字 song_id，没法写")

    before = await _fav_count()              # 写之前的总数
    try:
        ok = await _state["client"].songlist.like_song([(raw_id, stype)], credential=cred)
    except (CredentialExpiredError, CredentialInvalidError) as e:
        raise HTTPException(401, f"凭据失效，重新扫码: {e}") from e
    except RatelimitedError as e:
        raise HTTPException(429, f"被风控限流了，先别连点: {e}") from e
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(502, f"写回失败: {type(e).__name__}: {e}") from e

    after = await _fav_count()               # ★ 回读校验（别只信上游的返回值）
    # after > before  -> 确定加进去了
    # after == before -> 本来就在收藏里（上游对"已收藏"也返回成功），或者没生效
    added = (before is not None and after is not None and after > before)

    log.warning("写回「我喜欢」: mid=%s song_id=%s type=%s 上游回=%s 收藏数 %s -> %s (确定新增=%s)",
                id, raw_id, stype, ok, before, after, added)
    return {
        "ok": bool(ok),
        "on": True,
        "id": id, "song_id": raw_id, "name": getattr(song, "name", ""),
        "fav_count_before": before, "fav_count_after": after,
        "added": added,
        "note": ("已加入" if added else
                 "上游返回成功，但收藏总数没变 —— 多半是这首歌本来就在你「我喜欢」里"),
    }


