"""Subsonic / OpenSubsonic 兼容层（/rest/*.view）—— 「像 Navidrome 一样被调用」的那一面。

设计约束（改动前先读）：

  1. **只读 + 只加收藏**。播放、搜索、歌单、歌词、封面全走这里；
     写操作只有 star（真写回 QQ「我喜欢」）。unstar 明确返回失败 ——
     QQ 上游对 dirId=201 的删除是静默忽略（见 app.py /v1/favorite 上方长注释，
     2026-10-11 实测穷举过 10 种写法），假装成功会骗客户端。
  2. **音频一律 302 到 QQ CDN**，本服务不代理音频。这样 Cloudflare 隧道只转发
     JSON 和 302，不转发音频，不受 CF 100 秒超时/流量限制影响。
  3. **复用 app.py 的取数逻辑**（song_to_item / resolve_play_url / list_by_mode /
     fetch_song / _fav_count），不复制一份 —— 两边音质链、卡片模式、缓存必须同源，
     否则 /v1 和 /rest 的行为会慢慢飘开。本模块**不注册任何 /v1 路由**。
  4. 响应格式固定 JSON（f=json）。ESP 的 subsonic.c 与主流客户端（Symfonium /
     Feishin / substreamer）都用 json；f=xml 不实现。
  5. Subsonic 约定：**业务失败也返回 HTTP 200**，靠 subsonic-response.status=failed +
     error.code 表达。鉴权失败同理（客户端据此弹"账号密码错"）。
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import time
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

import app as core
import websession

log = logging.getLogger("qqweb.subsonic")

API_VERSION = "1.16.1"
SERVER_TYPE = "qqmusic"
SERVER_VERSION = "1.0.0"

router = APIRouter()

# 网页/客户端共用的 Subsonic 账号（webapp.py 启动时从 data/web.json 注入）
_users: dict[str, str] = {}
_star_write = True


def configure(user: str, password: str, star_write: bool = True) -> None:
    global _star_write
    _users.clear()
    if user and password:
        _users[user] = password
    _star_write = bool(star_write)


# ------------------------------------------------------------------ 响应封装

class SubError(Exception):
    """带 Subsonic 错误码的业务异常（code 见 Subsonic API：40=鉴权失败, 70=找不到…）"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _envelope(payload: dict[str, Any] | None = None, *, status: str = "ok") -> dict[str, Any]:
    r: dict[str, Any] = {"status": status, "version": API_VERSION,
                         "type": SERVER_TYPE, "serverVersion": SERVER_VERSION}
    if payload:
        r.update(payload)
    return {"subsonic-response": r}


def _ok(payload: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(_envelope(payload))


def _failed(code: int, message: str) -> JSONResponse:
    r = _envelope(status="failed")
    r["subsonic-response"]["error"] = {"code": code, "message": message}
    return JSONResponse(r)


# ------------------------------------------------------------------ 鉴权

def _auth(request: Request, params: dict[str, str]) -> str:
    """校验身份：网页会话 cookie **或** Subsonic 的 u/t/s、u/p。失败抛 SubError(40)。"""
    cookie_user = websession.user_from_request(request)
    if cookie_user:
        return cookie_user

    u = params.get("u") or ""
    t = (params.get("t") or "").lower()
    s = params.get("s") or ""
    p = params.get("p")

    pw = _users.get(u)
    if pw is None:
        raise SubError(40, "Wrong username or password")
    if t and s:
        if hashlib.md5((pw + s).encode("utf-8")).hexdigest().lower() != t:
            raise SubError(40, "Wrong username or password")
        return u
    if p is not None:
        # 客户端可能用 enc: 前缀；不支持就先按明文比
        if p == pw:
            return u
        raise SubError(40, "Wrong username or password")
    raise SubError(40, "Missing authentication token")


# ------------------------------------------------------------------ 歌曲 JSON

def _bitrate(size: int, duration: int) -> int:
    return int(size * 8 / duration / 1000) if duration > 0 and size > 0 else 0


def _ctype(suffix: str) -> str:
    return "audio/flac" if suffix == "flac" else "audio/mpeg"


def _iso(ts: float | None = None) -> str:
    ts = time.time() if ts is None else ts
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _song_json(it: dict[str, Any], starred: bool = False) -> dict[str, Any]:
    """把我们内部统一的 item（/v1/list 的形状）映射成 Subsonic song。"""
    suffix = (it.get("suffix") or "").lower()
    dur = int(it.get("duration") or 0)
    size = int(it.get("size") or 0)
    artist = it.get("artist") or ""
    album = it.get("album") or ""
    title = it.get("title") or ""
    path = "QQ音乐/" + "/".join(x.replace("/", "_") for x in (artist, album, title) if x)
    song: dict[str, Any] = {
        # ★ id 就是 songmid，和 /v1/* 完全一致 —— 换协议不换 id，收藏/最近播放不会错位
        "id": it.get("id") or "",
        "parent": "",
        "isDir": False,
        "title": title,
        "album": album,
        "artist": artist,
        "track": 0,
        "year": 0,
        "genre": "",
        "coverArt": it.get("cover") or "none",
        "size": size,
        "contentType": _ctype(suffix),
        "suffix": suffix,
        "duration": dur,
        "bitRate": _bitrate(size, dur),
        "path": path + ("." + suffix if suffix else ""),
        "isVideo": False,
        "type": "music",
        "created": _iso(),
        "discNumber": 1,
    }
    if starred:
        song["starred"] = _iso()
    return song


def _songs(items: list[dict[str, Any]], starred: bool = False) -> list[dict[str, Any]]:
    return [_song_json(it, starred) for it in items if it.get("id")]


# ------------------------------------------------------------------ 取数（复用 app.py）

async def _search_songs(q: str, limit: int) -> list[dict[str, Any]]:
    r = await core.search(q=q, limit=limit)
    return _songs(r.get("items") or [])


async def _mode_songs(mode: str, limit: int, page: int = 1) -> list[dict[str, Any]]:
    r = await core.list_by_mode(mode=mode, limit=limit, page=page)
    return _songs(r.get("items") or [], starred=(mode == "fav"))


async def _fav_songs(limit: int) -> list[dict[str, Any]]:
    r = await core.favorites(limit=limit, page=1)
    return _songs(r.get("items") or [], starred=True)


async def _one_song(mid: str) -> dict[str, Any]:
    song = await core.fetch_song(mid)
    it = core.song_to_item(song)
    if it is None:
        raise SubError(70, f"这首歌没有可用音质（可能全部需要付费）: {mid}")
    return _song_json(it)


# ------------------------------------------------------------------ 封面（代理，图片很小）

_cover_cache: dict[str, tuple[bytes, str, float]] = {}
_COVER_TTL = 86400.0
_COVER_MAX = 128
_http: httpx.AsyncClient | None = None


def _http_client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
    return _http


async def _cover_bytes(mid: str, size: int) -> tuple[bytes, str]:
    if size not in core.COVER_SIZES:
        size = 300
    key = f"{mid}:{size}"
    now = time.time()
    hit = _cover_cache.get(key)
    if hit and now - hit[2] < _COVER_TTL:
        return hit[0], hit[1]
    url = core.COVER_TMPL.format(s=size, mid=mid)
    r = await _http_client().get(url)
    if r.status_code != 200 or not r.content:
        raise SubError(70, f"封面取不到（{r.status_code}）: {url}")
    ctype = r.headers.get("content-type") or "image/jpeg"
    if len(_cover_cache) >= _COVER_MAX:
        # 简单淘汰：清掉最旧的一批
        for k in sorted(_cover_cache, key=lambda k: _cover_cache[k][2])[:32]:
            _cover_cache.pop(k, None)
    _cover_cache[key] = (r.content, ctype, now)
    return r.content, ctype


# ------------------------------------------------------------------ 各端点

def h_ping(p: dict[str, str]) -> dict[str, Any]:
    return {}


def h_get_license(p: dict[str, str]) -> dict[str, Any]:
    return {"license": {"valid": True}}


def h_get_music_folders(p: dict[str, str]) -> dict[str, Any]:
    return {"musicFolders": {"musicFolder": [{"id": 1, "name": "QQ 音乐"}]}}


def h_get_indexes(p: dict[str, str]) -> dict[str, Any]:
    return {"indexes": {"ignoredArticles": "", "index": []}}


def h_get_artists(p: dict[str, str]) -> dict[str, Any]:
    # 第一版不做歌手库；返回空表让客户端退化，不要报错
    return {"artists": {"ignoredArticles": "", "index": []}}


def h_get_album_list2(p: dict[str, str]) -> dict[str, Any]:
    # 第一版不做专辑库；客户端的「专辑」页会是空的，歌单/搜索/收藏照常
    return {"albumList2": {"album": []}}


async def h_search(p: dict[str, str]) -> dict[str, Any]:
    q = p.get("query") or ""
    if not q:
        raise SubError(10, "缺少 query 参数")
    try:
        limit = int(p.get("songCount") or 20)
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 50))
    songs = await _search_songs(q, limit)
    return {"searchResult3": {"song": songs}}


async def h_get_song(p: dict[str, str]) -> dict[str, Any]:
    mid = p.get("id") or ""
    if not mid:
        raise SubError(10, "缺少 id 参数")
    return {"song": await _one_song(mid)}


async def h_get_starred2(p: dict[str, str]) -> dict[str, Any]:
    return {"starred2": {"song": await _fav_songs(100)}}


async def h_get_random(p: dict[str, str]) -> dict[str, Any]:
    try:
        size = int(p.get("size") or 30)
    except ValueError:
        size = 30
    size = max(1, min(size, 100))
    return {"randomSongs": {"song": await _mode_songs("guess", size)}}


async def h_get_playlists(p: dict[str, str]) -> dict[str, Any]:
    """6 个 QQ 模式 → 6 个 Subsonic 歌单（id 就是 mode 名，稳定不变）。"""
    items = [{"id": m["mode"], "name": m["name"], "owner": next(iter(_users), ""),
              "public": False, "created": _iso(0), "changed": _iso(0)} for m in core.MODES]
    return {"playlists": {"playlist": items}}


async def h_get_playlist(p: dict[str, str]) -> dict[str, Any]:
    pid = p.get("id") or ""
    known = {m["mode"] for m in core.MODES}
    if pid not in known:
        raise SubError(70, f"没有这个歌单: {pid}")
    entries = await _mode_songs(pid, 100, 1)
    label = next(m["name"] for m in core.MODES if m["mode"] == pid)
    return {"playlist": {"id": pid, "name": label, "owner": next(iter(_users), ""),
                         "public": False, "songCount": len(entries),
                         "created": _iso(0), "changed": _iso(0), "entry": entries}}


def _force_https(url: str) -> str:
    """把 QQ 直链从 http 升级成 https。

    ★ 为什么必须：网页经 Cloudflare 是 https，浏览器对 <audio> 的 http 资源会按
      **混合内容**直接拦掉（播放器停在 0:00 不动）。实测 QQ CDN 的 https 与 http
      完全等价（HTTP/2 206、Range 可用、同一份 MD5），所以统一升级。
    """
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


async def h_stream(p: dict[str, str]) -> Response:
    mid = p.get("id") or ""
    if not mid:
        raise SubError(10, "缺少 id 参数")
    want = _want_quality(p)
    if p.get("timeOffset") and p["timeOffset"] not in ("0", ""):
        # QQ 只有整文件；timeOffset 靠服务端转码实现，这条链路做不到。
        # 明确打日志，便于以后排查"为什么拖了进度条却从头放"。
        log.info("忽略 timeOffset=%s（QQ 直链不支持按时间 seek）", p["timeOffset"])
    r = await core.resolve_play_url(mid, want)
    url = _force_https(r["url"])
    log.info("stream %s -> %s (%s)", mid, r["quality"], url[:64])
    return RedirectResponse(url, status_code=302,
                            headers={"Cache-Control": "no-store"})


def _want_quality(p: dict[str, str]) -> str | None:
    """把客户端的 format/maxBitRate 映射成我们的挡位（做不到真转码，就给对应音质的直链）。"""
    fmt = (p.get("format") or "").lower()
    if fmt in ("mp3", "aac", "ogg", "opus", "m4a"):
        try:
            mbr = int(p.get("maxBitRate") or 0)
        except ValueError:
            mbr = 0
        if mbr and mbr <= 128:
            return "128"
        return "320"
    # raw / flac / 空 → 无损优先（用户在乎无损）
    return None


async def h_cover(p: dict[str, str]) -> Response:
    mid = p.get("id") or ""
    if not mid or mid == "none":
        raise SubError(70, "没有封面")
    try:
        size = int(p.get("size") or 300)
    except ValueError:
        size = 300
    data, ctype = await _cover_bytes(mid, size)
    return Response(data, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})


async def h_lyrics_by_song_id(p: dict[str, str]) -> dict[str, Any]:
    mid = p.get("id") or ""
    if not mid:
        raise SubError(10, "缺少 id 参数")
    lrc = await core.lyric(id=mid)          # format=None → LRC 纯文本
    norm = core._lrc_to_normalized(lrc if isinstance(lrc, str) else "")
    lines = [{"start": int(x["t"]), "value": x["text"]} for x in norm["lines"]]
    song = await core.fetch_song(mid)
    it = core.song_to_item(song) or {}
    structured = [{"displayArtist": it.get("artist", ""),
                   "displayTitle": it.get("title", ""),
                   "lang": "zh",
                   "synced": bool(norm["synced"]),
                   "line": lines}]
    return {"lyricsList": {"structuredLyrics": structured}}


async def h_get_lyrics(p: dict[str, str]) -> dict[str, Any]:
    """老式 getLyrics（按 artist/title 查）—— 我们只能按 id 查，返回空，别报错。"""
    return {}


async def h_star(p: dict[str, str]) -> dict[str, Any]:
    mid = p.get("id") or ""
    if not mid:
        # 专辑/歌手收藏不在这条线里（第一版没做专辑库）
        raise SubError(30, "只支持给歌曲加星")
    if not _star_write:
        raise SubError(30, "服务端关闭了收藏写回")
    cred = core.cred_or_401()
    song = await core.fetch_song(mid)
    raw_id = int(getattr(song, "id", 0) or 0)
    stype = int(getattr(song, "type", 0) or 1)
    if not raw_id:
        raise SubError(70, f"拿不到 {mid} 的数字 song_id")
    before = await core._fav_count()
    try:
        ok = await core._state["client"].songlist.like_song([(raw_id, stype)], credential=cred)
    except Exception as e:                                   # noqa: BLE001
        raise SubError(30, f"写回「我喜欢」失败: {type(e).__name__}: {e}") from e
    after = await core._fav_count()
    added = before is not None and after is not None and after > before
    log.warning("star(mid=%s) 上游=%s 收藏数 %s -> %s 确定新增=%s",
                mid, ok, before, after, added)
    # 回读没变 = 本来就在收藏里（上游对重复收藏也返回成功）；不当作失败
    return {}


async def h_unstar(p: dict[str, str]) -> dict[str, Any]:
    raise SubError(
        30,
        "QQ 音乐不支持从「我喜欢」删除（上游删除接口静默忽略，实测无效）。"
        "请在 QQ 音乐 App 里手动取消收藏。本次未做任何改动。")


async def h_scrobble(p: dict[str, str]) -> dict[str, Any]:
    return {}


async def h_now_playing(p: dict[str, str]) -> dict[str, Any]:
    return {"nowPlaying": {"entry": []}}


async def h_scan_status(p: dict[str, str]) -> dict[str, Any]:
    return {"scanStatus": {"scanning": False, "count": 1621}}


async def h_open_subsonic_extensions(p: dict[str, str]) -> dict[str, Any]:
    return {"openSubsonicExtensions": [{"name": "songLyrics", "versions": [1]}]}


async def h_empty_list(name: str) -> dict[str, Any]:
    return {}


async def h_get_play_queue(p: dict[str, str]) -> dict[str, Any]:
    return {}


async def h_save_play_queue(p: dict[str, str]) -> dict[str, Any]:
    return {}


# 方法名 → handler。没列出来的走 _fallback（返回 status=ok 的空对象，避免客户端整页报错）
_HANDLERS: dict[str, Callable[[dict[str, str]], Any]] = {
    "ping": h_ping,
    "getLicense": h_get_license,
    "getMusicFolders": h_get_music_folders,
    "getIndexes": h_get_indexes,
    "getArtists": h_get_artists,
    "getArtist": h_empty_list,
    "getAlbum": h_empty_list,
    "getAlbumList": h_get_album_list2,
    "getAlbumList2": h_get_album_list2,
    "getSong": h_get_song,
    "search": h_search,
    "search2": h_search,
    "search3": h_search,
    "getStarred": h_get_starred2,
    "getStarred2": h_get_starred2,
    "getRandomSongs": h_get_random,
    "getPlaylists": h_get_playlists,
    "getPlaylist": h_get_playlist,
    "stream": h_stream,
    "download": h_stream,
    "getCoverArt": h_cover,
    "getLyricsBySongId": h_lyrics_by_song_id,
    "getLyrics": h_get_lyrics,
    "star": h_star,
    "unstar": h_unstar,
    "scrobble": h_scrobble,
    "getNowPlaying": h_now_playing,
    "getScanStatus": h_scan_status,
    "getOpenSubsonicExtensions": h_open_subsonic_extensions,
    "getPlayQueue": h_get_play_queue,
    "savePlayQueue": h_save_play_queue,
}


async def _params(request: Request) -> dict[str, str]:
    p = {k: v for k, v in request.query_params.items()}
    if request.method == "POST":
        try:
            form = await request.form()
            for k, v in form.items():
                if k not in p:
                    p[str(k)] = str(v)
        except Exception:                                    # noqa: BLE001
            pass
    return p


def _sanitize(p: dict[str, str]) -> dict[str, str]:
    """日志用：抹掉凭据字段。"""
    return {k: ("***" if k in ("p", "t", "s") else v) for k, v in p.items()}


async def _handle(request: Request, method: str) -> Response:
    p = await _params(request)
    try:
        _auth(request, p)
        fn = _HANDLERS.get(method)
        if fn is None:
            log.info("未实现的端点（返回空结果）: %s %s", method, _sanitize(p))
            return _ok()
        payload = fn(p)
        if inspect.isawaitable(payload):
            payload = await payload
        if isinstance(payload, Response):
            return payload
        if payload is None:
            payload = {}
        return _ok(payload)
    except SubError as e:
        log.warning("%s 失败: code=%s %s", method, e.code, e.message)
        return _failed(e.code, e.message)
    except Exception as e:                                   # noqa: BLE001
        log.exception("%s 异常", method)
        return _failed(0, f"{type(e).__name__}: {e}")



@router.api_route("/{method}.view", methods=["GET", "POST"])
async def rest_view(request: Request, method: str) -> Response:
    return await _handle(request, method)


@router.api_route("/{method}", methods=["GET", "POST"])
async def rest_bare(request: Request, method: str) -> Response:
    return await _handle(request, method)


@router.api_route("", methods=["GET", "POST"])
async def rest_root(request: Request) -> Response:
    return _failed(0, "要调用具体端点，例如 /rest/ping.view")
