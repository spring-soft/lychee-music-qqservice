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

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import os
import random
import time
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

import app as core
import guard
import websession

log = logging.getLogger("qqweb.subsonic")

API_VERSION = "1.16.1"
SERVER_TYPE = "qqmusic"
SERVER_VERSION = "1.0.0"

router = APIRouter()

# 网页/客户端共用的 Subsonic 账号（webapp.py 启动时从 data/web.json 注入）
_users: dict[str, str] = {}
_star_write = True
# ping 里报告的服务器类型。有的客户端会按 type 做特判（认不出就报奇怪的错），
# 所以做成可配置：data/web.json 里加 "server_type": "navidrome" 即可切换，默认 qqmusic。
_server_type = "qqmusic"


def configure(user: str, password: str, star_write: bool = True,
              server_type: str | None = None) -> None:
    global _star_write, _server_type
    _users.clear()
    if user and password:
        _users[user] = password
    _star_write = bool(star_write)
    if server_type:
        _server_type = server_type


# ------------------------------------------------------------------ 响应封装

class SubError(Exception):
    """带 Subsonic 错误码的业务异常（code 见 Subsonic API：40=鉴权失败, 70=找不到…）"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _envelope(payload: dict[str, Any] | None = None, *, status: str = "ok") -> dict[str, Any]:
    # openSubsonic=True：我们实现了 OpenSubsonic 的 songLyrics 扩展，
    # 部分客户端（含 CloudPureMusic）会看这个标志决定后续走哪套流程。
    r: dict[str, Any] = {"status": status, "version": API_VERSION,
                         "type": _server_type, "serverVersion": SERVER_VERSION,
                         "openSubsonic": True}
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
    """校验身份。按客户端实际会用的四套方式依次尝试（都是 Subsonic 生态里真实存在的）：

      1) 网页会话 cookie（自己的页面用）
      2) HTTP Basic: Authorization: Basic base64(user:pass)  ← 有的客户端走这条
      3) u + t + s：t = md5(password + salt)              ← 标准 token 认证
      4) u + p：明文，或 p=enc:<六进制>                     ← 老客户端 / HTTP 下的写法

    失败统一抛 SubError(40)（Subsonic 约定：鉴权失败也返回 HTTP 200）。
    """
    cookie_user = websession.user_from_request(request)
    if cookie_user:
        return cookie_user

    # 2) HTTP Basic
    authz = request.headers.get("authorization") or ""
    if authz.lower().startswith("basic "):
        try:
            raw = base64.b64decode(authz.split(None, 1)[1].strip()).decode("utf-8", "replace")
            bu, _, bpw = raw.partition(":")
            if websession.verify_password(bu, bpw):
                return bu
        except Exception:                                    # noqa: BLE001
            pass

    u = params.get("u") or ""
    pw = _users.get(u)
    if pw is None:
        raise SubError(40, "Wrong username or password")

    # 3) token
    t = (params.get("t") or "").lower()
    s = params.get("s") or ""
    if t and s:
        if hashlib.md5((pw + s).encode("utf-8")).hexdigest().lower() == t:
            return u
        raise SubError(40, "Wrong username or password")

    # 4) 明文 / enc:<hex>
    p = params.get("p")
    if p is not None:
        cand = p
        if cand.startswith("enc:"):
            try:
                cand = bytes.fromhex(cand[4:]).decode("utf-8", "replace")
            except ValueError:
                cand = ""
        if hmac.compare_digest(cand.encode("utf-8"), pw.encode("utf-8")):
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


# ------------------------------------------------------------------ 完整歌单

# 可翻页的模式（启动后后台预热这几个，避免用户点开时等十几秒）
PAGED_MODES = ("fav", "daily", "million", "newrec")

# ★ Subsonic 的 getPlaylist 没有分页参数：客户端期望一次拿到整份歌单。
#   而 /v1/list 单页上限 100 首（上游 query_song 一次最多 100 个 mid），
#   所以这里要自己翻页。实测「收藏」有 900+ 首，只取第一页客户端就永远停在 100。
_PLAYLIST_TTL = 1800.0       # 整份歌单缓存 30 分钟（翻页要 10+ 次上游请求，别每次都打）
_PLAYLIST_MAX = 2000         # 上限，防止某个模式无限翻页
_PLAYLIST_PAGE = 100         # /v1/list 单页上限
_PLAYLIST_KINDS_PAGED = {"fav", "daily", "million", "newrec"}   # 能真翻页的模式

_pc: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_pc_lock = asyncio.Lock()


async def warm_playlists() -> None:
    """后台预热整份歌单。实测「收藏」913 首要翻 10 页 ≈ 15 秒，
    用户点开时如果现翻会以为卡死；所以启动后和每 5 分钟在后台先拉好（缓存 30 分钟）。"""
    for mode in PAGED_MODES:
        try:
            n = len(await _full_mode_songs(mode))
            log.info("预热歌单 %s 完成（%d 首）", mode, n)
        except Exception as e:                               # noqa: BLE001
            log.warning("预热歌单 %s 失败: %s", mode, e)
        await asyncio.sleep(1.0)     # 别把上游一次打太狠


async def _full_mode_songs(mode: str) -> list[dict[str, Any]]:
    """按模式取【整份】歌单（自动翻页 + 去重 + 10 分钟缓存）。

    · fav / daily / million / newrec：真能翻页，一直翻到不足一页
    · guess / radar：上游每次随机给一小批，翻页没有意义 → 只取一批
    """
    now = time.time()
    hit = _pc.get(mode)
    if hit and now - hit[0] < _PLAYLIST_TTL:
        return hit[1]

    async with _pc_lock:                      # 避免客户端并发请求时把上游打爆
        hit = _pc.get(mode)
        if hit and time.time() - hit[0] < _PLAYLIST_TTL:
            return hit[1]

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages = 20 if mode in _PLAYLIST_KINDS_PAGED else 1
        for page in range(1, pages + 1):
            r = await core.list_by_mode(mode=mode, limit=_PLAYLIST_PAGE, page=page)
            batch = r.get("items") or []
            added = 0
            for it in batch:
                sid = it.get("id") or ""
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                items.append(it)
                added += 1
                if len(items) >= _PLAYLIST_MAX:
                    break
            log.info("整份歌单 %s: page=%d 取到 %d 首，新增 %d，累计 %d",
                     mode, page, len(batch), added, len(items))
            if len(items) >= _PLAYLIST_MAX:
                break
            if len(batch) < _PLAYLIST_PAGE:   # 不足一页 = 到头了
                break
            if added == 0:                    # 翻页没带来新歌（随机模式）→ 停
                break
        out = _songs(items, starred=(mode == "fav"))
        _pc[mode] = (time.time(), out)
        return out


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
    # 歌手头像：getArtists 里用 "ar_<mid>" 当 coverArt id，这里换成 T001 模板
    if mid.startswith("ar_"):
        url = "http://y.qq.com/music/photo_new/T001R{s}x{s}M000{mid}.jpg".format(
            s=size, mid=mid[3:])
    else:
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


# ------------------------------------------------------------------ 客户端自建歌单
#
# 实测（2026-10-25，CloudPureMusic）：客户端会自己在服务端建歌单，id 形如
# cloudpureplay_heartlist（存它的"红心列表"）。以前 createPlaylist 走"未实现→空结果"，
# 但 getPlaylist 报 70，客户端就反复重试/报错。这里做一个**极小**的服务端歌单存储：
#   data/playlists.json  {id: {id, name, created, changed, items:[item…]}}
# 只存歌的元数据（与 /v1/list 同形状），播放时仍按 songmid 现取直链。
_pl_lock = asyncio.Lock()
_pl_cache: dict[str, Any] | None = None
_PL_MAX_SONGS = 1000


def _pl_path():
    return core.DATA_DIR / "playlists.json"


def _pl_load() -> dict[str, Any]:
    global _pl_cache
    if _pl_cache is None:
        try:
            raw = json.loads(_pl_path().read_text("utf-8"))
            _pl_cache = raw if isinstance(raw, dict) else {}
        except Exception:                                    # noqa: BLE001
            _pl_cache = {}
    return _pl_cache


def _pl_save() -> None:
    path = _pl_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_pl_cache or {}, ensure_ascii=False), "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _ids_of(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)]


async def _resolve_items(ids: list[str]) -> list[dict[str, Any]]:
    """songId → 内部 item（并发 8，上限 1000 首，别让客户端超大列表把上游打爆）"""
    ids = ids[:_PL_MAX_SONGS]
    sem = asyncio.Semaphore(8)

    async def one(sid: str):
        async with sem:
            try:
                song = await core.fetch_song(sid)
                return core.song_to_item(song)
            except Exception:                                # noqa: BLE001
                return None
    got = await asyncio.gather(*[one(i) for i in ids])
    return [x for x in got if x]


def _pl_json(rec: dict[str, Any]) -> dict[str, Any]:
    entries = _songs(rec.get("items") or [])
    return {"id": rec.get("id"), "name": rec.get("name") or "",
            "owner": next(iter(_users), ""), "public": False,
            "songCount": len(entries), "created": _iso(rec.get("created") or 0),
            "changed": _iso(rec.get("changed") or 0), "entry": entries}


async def h_create_playlist(p: dict[str, Any]) -> dict[str, Any]:
    pid = str(p.get("playlistId") or "").strip()
    name = str(p.get("name") or "").strip()
    items = await _resolve_items(_ids_of(p.get("songId")))
    async with _pl_lock:
        pls = _pl_load()
        now = time.time()
        if not pid:
            pid = "qwp%d" % int(now * 1000)
            name = name or "我的歌单"
        rec = pls.get(pid) or {"id": pid, "name": name or "我的歌单", "created": now}
        if name:
            rec["name"] = name
        rec["items"] = items
        rec["changed"] = now
        pls[pid] = rec
        _pl_save()
    log.warning("createPlaylist id=%s name=%s songs=%d", pid, rec["name"], len(items))
    return {"playlist": _pl_json(rec)}


async def h_update_playlist(p: dict[str, Any]) -> dict[str, Any]:
    pid = str(p.get("playlistId") or p.get("id") or "")
    add_items = await _resolve_items(_ids_of(p.get("songIdToAdd")))
    async with _pl_lock:
        pls = _pl_load()
        rec = pls.get(pid)
        if rec is None:
            raise SubError(70, f"没有这个歌单: {pid}")
        if p.get("name"):
            rec["name"] = str(p["name"])
        if add_items:
            have = {it.get("id") for it in rec.get("items") or []}
            rec.setdefault("items", []).extend(
                [it for it in add_items if it.get("id") and it.get("id") not in have])
        for x in _ids_of(p.get("songIndexToRemove")):
            try:
                i = int(x)
            except ValueError:
                continue
            if 0 <= i < len(rec.get("items") or []):
                rec["items"].pop(i)
        rec["changed"] = time.time()
        _pl_save()
    log.warning("updatePlaylist id=%s -> %d 首", pid, len(rec.get("items") or []))
    return {}


async def h_delete_playlist(p: dict[str, Any]) -> dict[str, Any]:
    pid = str(p.get("id") or "")
    async with _pl_lock:
        pls = _pl_load()
        pls.pop(pid, None)
        _pl_save()
    log.warning("deletePlaylist id=%s", pid)
    return {}


# ------------------------------------------------------------------ 各端点

def h_ping(p: dict[str, str]) -> dict[str, Any]:
    return {}


def h_get_license(p: dict[str, str]) -> dict[str, Any]:
    return {"license": {"valid": True}}


def h_get_music_folders(p: dict[str, str]) -> dict[str, Any]:
    return {"musicFolders": {"musicFolder": [{"id": 1, "name": "QQ 音乐"}]}}


async def h_get_user(p: dict[str, str]) -> dict[str, Any]:
    """getUser —— 登录后很多客户端第一件事就是它。

    ★ 实测踩过（2026-10-25）：CloudPureMusic 在 token 认证通过之后调 getUser，
      当时这里走"未实现 → status=ok 的空对象"，客户端就判定**用户名或密码错误**。
      所以它必须返回真实的 user 对象，不能空。
    """
    wanted = p.get("username") or p.get("u") or ""
    if wanted not in _users:
        raise SubError(70, f"没有这个用户: {wanted}")
    return {"user": {
        "username": wanted,
        "email": "",
        "scrobblingEnabled": False,
        "adminRole": True,
        "settingsRole": False,
        "downloadRole": True,
        "uploadRole": False,
        "playlistRole": True,
        "coverArtRole": True,
        "commentRole": False,
        "podcastRole": False,
        "shareRole": False,
        "videoConversionRole": False,
        "musicFolderId": [1],
    }}


def h_get_avatar(p: dict[str, str]) -> Response:
    # 头像：没有就 204（Subsonic 规范允许无头像）
    return Response(status_code=204)


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
        # 有些客户端会先发不带 query 的 search（拿空结果/历史），不该报错
        return {"searchResult3": {"song": []}}
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
    # 整份「我喜欢」都返回（以前只给 100 首，客户端收藏列表会永远停在 100）
    return {"starred2": {"song": await _full_mode_songs("fav"),
                         "album": [], "artist": []}}


# ------------------------------------------------------------------ 专辑 / 歌手浏览
#
# 为什么现在才做：第一版只保证"歌单 + 搜索 + 播放"，客户端的「库」页是空的。
# 上游库支持 专辑列表/专辑详情/专辑歌曲/歌手列表/歌手专辑/相似歌曲，全部现成，
# 只是要翻译成 Subsonic 的 album/artist 结构。

_album_cache: dict[str, list[dict[str, Any]]] = {}
_album_cache_at = 0.0
_ALBUM_TTL = 600.0
_artist_names: dict[str, str] = {}


def _year(s: Any) -> int:
    try:
        return int(str(s)[:4])
    except Exception:                                        # noqa: BLE001
        return 0


def _index_letter(s: str) -> str:
    ch = (s or "").strip()[:1].upper()
    return ch if ch.isascii() and ch.isalpha() else "#"


def _song_json_model(song: Any, track: int = 0) -> dict[str, Any] | None:
    it = core.song_to_item(song)
    if it is None:
        return None
    j = _song_json(it)
    if track:
        j["track"] = track
    return j


def _album_json(a: Any, song_count: int = 0) -> dict[str, Any]:
    singers = getattr(a, "singers", None) or []
    artist = " / ".join(getattr(s, "name", "") for s in singers)         or getattr(a, "singer_name", "") or ""
    mid = getattr(a, "mid", "") or ""
    count = song_count or int(getattr(a, "total_num", 0) or 0)         or int(getattr(a, "songnum", 0) or 0)
    return {
        "id": mid,
        "name": getattr(a, "name", "") or getattr(a, "title", ""),
        "artist": artist,
        "coverArt": mid or "none",
        "songCount": count,
        "duration": 0, "playCount": 0, "created": _iso(0),
        "year": _year(getattr(a, "time_public", "") or getattr(a, "release_time", "")),
    }


async def h_get_album_list2(p: dict[str, Any]) -> dict[str, Any]:
    """专辑列表：newest/recent/random 用 QQ「新碟上架」；其余类型返回空（不报错）。"""
    typ = str(p.get("type") or "newest").lower()
    try:
        size = int(p.get("size") or 20)
        offset = int(p.get("offset") or 0)
    except ValueError:
        size, offset = 20, 0
    size = max(1, min(size, 100))
    if typ not in ("newest", "recent", "random"):
        return {"albumList2": {"album": []}}
    global _album_cache, _album_cache_at
    now = time.time()
    try:
        if not _album_cache or now - _album_cache_at > _ALBUM_TTL:
            resp = await core._state["client"].album.get_new_album(area=1, num=100, page=1)
            albums = getattr(resp, "albums", None) or []
            _album_cache = [_album_json(a) for a in albums if getattr(a, "mid", "")]
            _album_cache_at = now
            log.info("新碟上架缓存 %d 张", len(_album_cache))
    except Exception as e:                                   # noqa: BLE001
        log.warning("取新碟上架失败: %s", e)
        return {"albumList2": {"album": []}}
    albums = list(_album_cache)
    if typ == "random":
        random.shuffle(albums)
    page = albums[offset:offset + size]
    return {"albumList2": {"album": page}}


async def h_get_album(p: dict[str, Any]) -> dict[str, Any]:
    mid = p.get("id") or ""
    if not mid:
        raise SubError(10, "缺少 id 参数")
    name, artists, year = "", "", 0
    try:
        detail = await core._state["client"].album.get_detail(mid)
        album = getattr(detail, "album", None)
        if album is not None:
            name = getattr(album, "name", "") or getattr(album, "title", "")
            year = _year(getattr(album, "time_public", ""))
        artists = " / ".join(getattr(s, "name", "")
                             for s in (getattr(detail, "singers", None) or []))
    except Exception as e:                                   # noqa: BLE001
        log.warning("专辑详情失败 %s: %s", mid, e)

    items: list[dict[str, Any]] = []
    total = 0
    try:
        for page in range(1, 11):
            r = await core._state["client"].album.get_song(mid, num=100, page=page)
            batch = getattr(r, "song_list", None) or []
            total = int(getattr(r, "total_num", 0) or 0)
            for s in batch:
                j = _song_json_model(s, track=len(items) + 1)
                if j:
                    items.append(j)
            if len(batch) < 100 or (total and len(items) >= total):
                break
    except Exception as e:                                   # noqa: BLE001
        raise SubError(70, f"取专辑歌曲失败: {e}") from e
    if not items:
        raise SubError(70, f"专辑里没有可播的歌: {mid}")
    return {"album": {"id": mid, "name": name or mid, "artist": artists,
                      "coverArt": mid, "songCount": len(items), "duration": 0,
                      "playCount": 0, "created": _iso(0), "year": year, "song": items}}


async def h_get_artists(p: dict[str, Any]) -> dict[str, Any]:
    """歌手列表（QQ「热门歌手」，按首字母分组）。"""
    try:
        resp = await core._state["client"].singer.get_singer_list_index(page=1, num=100)
    except Exception as e:                                   # noqa: BLE001
        log.warning("取歌手列表失败: %s", e)
        return {"artists": {"ignoredArticles": "", "index": []}}
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in getattr(resp, "singerlist", None) or []:
        mid = getattr(s, "mid", "") or ""
        if not mid:
            continue
        name = getattr(s, "name", "") or getattr(s, "title", "")
        _artist_names[mid] = name
        letter = _index_letter(getattr(s, "spell", "") or name)
        groups.setdefault(letter, []).append(
            {"id": mid, "name": name, "albumCount": 0, "coverArt": "ar_" + mid})
    return {"artists": {"ignoredArticles": "",
                        "index": [{"name": k, "artist": v} for k, v in sorted(groups.items())]}}


async def h_get_artist(p: dict[str, Any]) -> dict[str, Any]:
    mid = p.get("id") or ""
    if not mid:
        raise SubError(10, "缺少 id 参数")
    albums: list[dict[str, Any]] = []
    try:
        r = await core._state["client"].singer.get_album_list(mid, num=30, page=1)
        for a in getattr(r, "album_list", None) or []:
            if getattr(a, "mid", ""):
                albums.append(_album_json(a, int(getattr(a, "total_num", 0) or 0)))
    except Exception as e:                                   # noqa: BLE001
        log.warning("取歌手专辑失败 %s: %s", mid, e)
    return {"artist": {"id": mid, "name": _artist_names.get(mid, mid),
                       "albumCount": len(albums), "coverArt": "ar_" + mid,
                       "album": albums}}


async def h_get_artist_info2(p: dict[str, Any]) -> dict[str, Any]:
    # 第一版不给简介/相似歌手（客户端会显示空，不会报错）
    return {"artistInfo2": {"biography": "", "musicBrainzId": "",
                            "lastFmUrl": "", "smallImageUrl": "",
                            "mediumImageUrl": "", "largeImageUrl": "",
                            "similarArtist": []}}


async def h_get_similar_songs2(p: dict[str, Any]) -> dict[str, Any]:
    mid = p.get("id") or ""
    try:
        count = int(p.get("count") or 20)
    except ValueError:
        count = 20
    count = max(1, min(count, 50))
    song = await core.fetch_song(mid)
    sid = int(getattr(song, "id", 0) or 0)
    if not sid:
        raise SubError(70, f"拿不到 {mid} 的数字 id")
    try:
        resp = await core._state["client"].song.get_similar_song(sid)
    except Exception as e:                                   # noqa: BLE001
        raise SubError(70, f"取相似歌曲失败: {e}") from e
    out: list[dict[str, Any]] = []
    for g in getattr(resp, "song", None) or []:
        for s in getattr(g, "song", None) or []:
            j = _song_json_model(s)
            if j and j["id"] != mid:
                out.append(j)
            if len(out) >= count:
                break
        if len(out) >= count:
            break
    return {"similarSongs2": {"song": out}}


async def h_get_random(p: dict[str, str]) -> dict[str, Any]:
    try:
        size = int(p.get("size") or 30)
    except ValueError:
        size = 30
    size = max(1, min(size, 100))
    return {"randomSongs": {"song": await _mode_songs("guess", size)}}


async def h_get_playlists(p: dict[str, Any]) -> dict[str, Any]:
    """6 个 QQ 模式 → 6 个 Subsonic 歌单（id 就是 mode 名，稳定不变）。

    再加上客户端自己建的服务端歌单（data/playlists.json）。
    """
    owner = next(iter(_users), "")
    items: list[dict[str, Any]] = []
    for m in core.MODES:
        hit = _pc.get(m["mode"])
        items.append({"id": m["mode"], "name": m["name"], "owner": owner,
                      "public": False, "songCount": len(hit[1]) if hit else 0,
                      "created": _iso(0), "changed": _iso(0)})
    for pid, rec in _pl_load().items():
        items.append({"id": pid, "name": rec.get("name") or pid, "owner": owner,
                      "public": False, "songCount": len(rec.get("items") or []),
                      "created": _iso(rec.get("created") or 0),
                      "changed": _iso(rec.get("changed") or 0)})
    return {"playlists": {"playlist": items}}


async def h_get_playlist(p: dict[str, Any]) -> dict[str, Any]:
    pid = p.get("id") or ""
    owner = next(iter(_users), "")
    known = {m["mode"] for m in core.MODES}
    if pid in known:
        entries = await _full_mode_songs(pid)
        label = next(m["name"] for m in core.MODES if m["mode"] == pid)
        return {"playlist": {"id": pid, "name": label, "owner": owner,
                             "public": False, "songCount": len(entries),
                             "created": _iso(0), "changed": _iso(0), "entry": entries}}
    rec = _pl_load().get(pid)
    if rec is not None:
        return {"playlist": _pl_json(rec)}
    # 客户端引用了不存在的歌单（典型：它自己还没同步成功的 cloudpureplay_xxx）：
    # 返回空歌单而不是错误码 —— 报 70 会让某些客户端一直重试并弹错误。
    log.info("getPlaylist: 未知歌单 %s，返回空歌单", pid)
    return {"playlist": {"id": pid, "name": pid, "owner": owner, "public": False,
                         "songCount": 0, "created": _iso(0), "changed": _iso(0),
                         "entry": []}}


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
    async with guard.stream_slot() as slot:
        if not slot.ok:
            raise SubError(50, "同时取流过多，稍后再试")
        return await _stream_inner(p)


async def _stream_inner(p: dict[str, str]) -> Response:
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
        return Response(status_code=204)
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
    "getUser": h_get_user,
    "getAvatar": h_get_avatar,
    "getIndexes": h_get_indexes,
    "getArtists": h_get_artists,
    "getArtist": h_get_artist,
    "getAlbum": h_get_album,
    "getArtistInfo2": h_get_artist_info2,
    "getSimilarSongs2": h_get_similar_songs2,
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
    "createPlaylist": h_create_playlist,
    "updatePlaylist": h_update_playlist,
    "deletePlaylist": h_delete_playlist,
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


async def _params(request: Request) -> dict[str, Any]:
    """取参数。同名参数（客户端会重复传 songId=…&songId=…）收成 list。"""
    p: dict[str, Any] = {}
    for k, v in request.query_params.multi_items():
        if k in p:
            if isinstance(p[k], list):
                p[k].append(v)
            else:
                p[k] = [p[k], v]
        else:
            p[k] = v
    if request.method == "POST":
        try:
            form = await request.form()
            for k, v in form.multi_items():
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
    ip = guard.client_ip(request)
    # ★ 防护：被封 / 超速 → Subsonic error 50（客户端会显示"太频繁"而不是密码错）
    if guard.blocked(ip):
        log.warning("已封禁 IP 仍在请求: %s %s", ip, method)
        return _failed(50, "请求过于频繁，已被暂时封禁，请稍后再试")
    if method not in ("getCoverArt", "getAvatar") and not guard.allow(ip):
        return _failed(50, "请求过于频繁，请稍后再试")
    try:
        _auth(request, p)

        # ★ 每个请求一行（不含封面/头像这种高频请求）：排查"客户端到底连到哪、发了什么"
        #   时这是第一手证据（客户端名 c=…、UA、参数）。
        if method not in ("getCoverArt", "getAvatar"):
            ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() \
                or (request.client.host if request.client else "-")
            log.info("REQ %-20s c=%-16s u=%-10s v=%-7s ip=%-15s args=%s ua=%r",
                     method, str(p.get("c") or "-")[:16], str(p.get("u") or "-")[:10],
                     str(p.get("v") or "-")[:7], ip,
                     json.dumps({k: v for k, v in p.items()
                                 if k in ("id", "query", "songCount", "size", "username",
                                          "playlistId", "format", "maxBitRate")},
                                ensure_ascii=False)[:180],
                     request.headers.get("user-agent"))

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
        if e.code in (40, 41, 42, 43):
            guard.note_auth_fail(ip)
            # ★ 鉴权失败的诊断日志：**不记密码/令牌**，只记"客户端用了哪种方式、账号是什么"。
            #   真实客户端五花八门（token / enc: / Basic / 空密码），没这条日志只能瞎猜。
            log.warning("鉴权失败 %s: u=%r t=%s s=%s p=%r basic=%s cookie=%s ua=%r query=%s",
                        method, p.get("u"), bool(p.get("t")), bool(p.get("s")),
                        (p.get("p") or "")[:4], bool(request.headers.get("authorization")),
                        websession.COOKIE in request.cookies,
                        request.headers.get("user-agent"), _sanitize(p))
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
