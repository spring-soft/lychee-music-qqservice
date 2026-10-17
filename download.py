"""下载 QQ 音乐的歌到 Navidrome 曲库。

需求（2026-10-17）：把歌下载到**服务器的 Navidrome 音乐库**里
（`/mnt/usb/music`），让 Navidrome 扫到、变成曲库的一部分 ——
这样服务器和 ESP 都能像本地歌一样播，断网也能听。

============================================================================
几个已经实测过的事实（别再猜）
============================================================================
· QQ 的播放直链（/v1/url 给的）下下来的**就是明文**：
  头 16 字节是 `fLaC` / ID3，ffprobe 能正常解出 codec/采样率/位深。
  不需要任何解密（QQ App 自己那套 .mflac/.qmc 加密不会出现在这条路上）。
· QQ 的 flac 自带 TITLE/ALBUM/ARTIST 标签，但没有内嵌封面
  （ffprobe 只有 1 个 audio stream）—— 所以这里**补一次封面**再落盘，
  否则 ESP 播下载来的歌会没有封面（它从 Navidrome 的 getCoverArt 取）。
· 歌词：`/v1/lyric?id=` 直接给 LRC 原文（带时间轴）。库里既有的约定就是
  `Artist - Title.flac` + 同名 `Artist - Title.lrc` 旁路文件，Navidrome 认这个。

============================================================================
落点与权限
============================================================================
写到 `/mnt/usb/music/QQ下载/`（子目录），不直接撒进库根：
  · 库根是 root:root 755，本服务用户 qqmusic 写不进去（也不用去改它的权限）
  · 子目录能明确"这些是设备下载的"，也避免和原有文件撞名
Navidrome 默认每 1 分钟自己扫一次（这台机器的 navidrome.toml 没改调度），
所以**不用**主动触发 startScan（那会把整库 47GB 重扫一遍，这机器只有 4 核/376MB）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Query

import logging

log = logging.getLogger("download")

DATA_DIR = Path(os.environ.get("QQ_DATA_DIR", "/data"))
MANIFEST = DATA_DIR / "downloads.json"
MUSIC_DIR = Path(os.environ.get("QQ_MUSIC_DIR", "/mnt/usb/music"))
DL_DIR = MUSIC_DIR / "QQ下载"
# ⚠️ 曲库在 exfat 上（挂载 uid=0,gid=0,umask=022），本服务用户**写不进去**。
#    所以下载/转码都在这个 ext4 暂存目录里做，最后用 sudo helper（sudoers 只放行
#    这一条命令）把成品"装"进曲库。见 deploy/qqmusic-install-dl。
STAGE_DIR = DATA_DIR / "dl_stage"
INSTALL_HELPER = "/usr/local/bin/qqmusic-install-dl"
# Navidrome 账号就在 player.json 的 nav 段里（跟播放器共用一份，不重复配置）
CFG_PATH = DATA_DIR / "player.json"

COVER_TMPL = "http://y.qq.com/music/photo_new/T002R300x300M000{mid}.jpg"
QUEUE_MAX = 200          # 排队上限（每首 10~40MB，别一下排几千首）
JOB_KEEP = 60            # 结果列表保留多少条

router = APIRouter(prefix="/v1/download", tags=["download"])

# 由 app.py 注入（QQ 那侧的取数逻辑属于 app.py）
_resolve_url: Callable[[str], Awaitable[dict[str, Any]]] | None = None
_meta: Callable[[str], Awaitable[dict[str, Any]]] | None = None
_lyric: Callable[[str], Awaitable[str]] | None = None

_jobs: list[dict[str, Any]] = []      # 最新的在前
_queued: list[dict[str, Any]] = []
_worker: asyncio.Task | None = None
_manifest: dict[str, Any] = {}
_http: httpx.AsyncClient | None = None


# ----------------------------------------------------------------- 工具

def _safe(s: str) -> str:
    """文件名里不能有的字符全换成下划线。

    跟库里既有约定一致：歌手串里的 " / " 会变成 " _ "
    （库里已有 `3LAU _ Carly Paige _ … - Touch.flac` 这种）。
    """
    s = (s or "").strip()
    for ch in '/\\:*?"<>|':
        s = s.replace(ch, "_")
    s = re.sub(r"\s+", " ", s)
    return s.strip().rstrip(".")[:150]


def _base_name(title: str, artist: str) -> str:
    t, a = _safe(title), _safe(artist)
    if a and t:
        return f"{a} - {t}"
    return t or a or "unnamed"


def _existing(base: str) -> str | None:
    """曲库里是不是已经有同名文件（下载目录或库根都算）。返回命中路径。"""
    for d in (DL_DIR, MUSIC_DIR):
        try:
            for fn in os.listdir(d):
                if fn.rsplit(".", 1)[0] == base and not fn.endswith(".part"):
                    return str(d / fn)
        except FileNotFoundError:
            continue
    return None


def _load_manifest() -> None:
    global _manifest
    if MANIFEST.exists():
        try:
            _manifest = json.loads(MANIFEST.read_text("utf-8"))
        except Exception:                                   # noqa: BLE001
            _manifest = {}


def _save_manifest() -> None:
    try:
        MANIFEST.write_text(json.dumps(_manifest, ensure_ascii=False, indent=1), "utf-8")
    except Exception as e:                                  # noqa: BLE001
        log.warning("下载清单写不进去: %s", e)


def _job(mid: str) -> dict[str, Any] | None:
    for j in _jobs:
        if j["id"] == mid:
            return j
    return None


def _install(src: Path, name: str) -> bool:
    """把暂存目录里的成品装进曲库（特权 helper；失败就报错，不假装成功）。"""
    try:
        p = subprocess.run(["sudo", "-n", INSTALL_HELPER, str(src), name],
                           capture_output=True, text=True, timeout=180)
    except Exception as e:                                  # noqa: BLE001
        log.error("调用 install helper 失败: %s", e)
        return False
    if p.returncode != 0:
        log.error("装入曲库失败（rc=%d）: %s %s", p.returncode,
                  (p.stdout or "").strip(), (p.stderr or "").strip())
        return False
    return True


def _publish(job: dict[str, Any]) -> None:
    """把 job 放到列表最前面（新建）或原位更新。"""
    cur = _job(job["id"])
    if cur is None:
        _jobs.insert(0, job)
        del _jobs[JOB_KEEP:]
    else:
        cur.update(job)


# ----------------------------------------------------------------- 下载核心

async def _download_one(mid: str) -> None:
    job = _job(mid)
    if job is None:
        return

    # 1) 元数据 + 直链（直链必须现取：vkey 只有 2 小时）
    try:
        job.update(state="准备", err=None)
        meta = await _meta(mid)                             # type: ignore[misc]
        resolved = await _resolve_url(mid)                  # type: ignore[misc]
        url = resolved["url"]
    except HTTPException as e:
        job.update(state="失败", err=str(e.detail)[:120])
        return
    except Exception as e:                                  # noqa: BLE001
        job.update(state="失败", err=f"{type(e).__name__}: {e}"[:120])
        return

    title = meta.get("title") or ""
    artist = meta.get("artist") or ""
    album = meta.get("album") or ""
    cover_mid = meta.get("cover") or ""
    # 音质以**直链接口**为准（它按 flac → 320 → 128 的链条挑"有文件"的最高档）
    suffix = (meta.get("suffix") or "flac").lower()
    quality = resolved.get("quality") or suffix
    quality_name = {"flac": "SQ 无损", "320": "HQ 320k", "128": "标准 128k"}.get(quality, quality)
    job.update(title=title, artist=artist, album=album, suffix=suffix,
               quality=quality, quality_name=quality_name)

    # ★ 用户要求：**尽量往高取**，但不要强制只下无损 ——
    #   QQ 的歌参差不齐（大部分有 SQ，有些只有 HQ）。绿钻最高能取到 SQ(flac)，
    #   所以这里就按链条取到的最高档下，档位记在任务里给界面显示。
    if quality != "flac":
        log.warning("这首没有 SQ 无损，按最高可用档位下: %s - %s（%s）",
                    artist, title, quality_name)

    base = _base_name(title, artist)
    hit = _existing(base)
    if hit:
        job.update(state="已存在", file=hit, pct=100)
        log.info("已存在，跳过: %s", hit)
        return

    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    raw = STAGE_DIR / f"{mid}.{suffix}"          # 下下来的原始文件
    fname = f"{base}.{suffix}"
    staged = STAGE_DIR / fname                    # 转码/打标签后的成品（还在暂存区）

    # 2) 流式下载到暂存文件（边下边报进度）
    try:
        job.update(state="下载中", pct=0, bytes=0, total=0)
        total = 0
        got = 0
        async with _http.stream("GET", url, timeout=120) as r:   # type: ignore[union-attr]
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            job["total"] = total
            with open(raw, "wb") as f:
                async for chunk in r.aiter_bytes(256 * 1024):
                    f.write(chunk)
                    got += len(chunk)
                    job["bytes"] = got
                    job["pct"] = int(got * 100 / total) if total else 0
        log.info("下载完成 %s: %d 字节 -> %s", mid, got, raw.name)
    except Exception as e:                                  # noqa: BLE001
        job.update(state="失败", err=f"下载失败: {type(e).__name__}: {e}"[:120])
        raw.unlink(missing_ok=True)
        return

    # 3) 补封面 + 写标签（remux，不解码，几秒）
    job.update(state="写入标签", pct=100)
    cover_tmp = None
    try:
        if cover_mid:
            cover_tmp = STAGE_DIR / f"{mid}.jpg"
            try:
                r = await _http.get(COVER_TMPL.format(mid=cover_mid), timeout=30)  # type: ignore[union-attr]
                r.raise_for_status()
                cover_tmp.write_bytes(r.content)
            except Exception as e:                          # noqa: BLE001
                log.warning("封面取不到（不影响下载）: %s", e)
                cover_tmp = None

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw)]
        if cover_tmp is not None:
            cmd += ["-i", str(cover_tmp), "-map", "0:a", "-map", "1:v",
                    "-c", "copy", "-disposition:v", "attached_pic"]
        else:
            cmd += ["-map", "0:a", "-c", "copy"]
        for k, v in (("title", title), ("artist", artist), ("album", album)):
            if v:
                cmd += ["-metadata", f"{k}={v}"]
        cmd += [str(staged)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if p.returncode != 0 or not staged.exists():
            # remux 失败就退回"原文件直接用"（标签靠原文件自带的，封面没有）
            log.warning("remux 失败，直接用原文件: %s", (p.stderr or "")[:200])
            shutil.move(str(raw), str(staged))
    except Exception as e:                                  # noqa: BLE001
        log.warning("remux 异常，直接用原文件: %s", e)
        try:
            if staged.exists():
                staged.unlink()
            shutil.move(str(raw), str(staged))
        except Exception:                                   # noqa: BLE001
            job.update(state="失败", err="转码失败")
            return
    finally:
        raw.unlink(missing_ok=True)
        if cover_tmp is not None:
            cover_tmp.unlink(missing_ok=True)

    # 4) 装进 Navidrome 曲库（特权 helper；exfat 上本服务用户写不进去）
    job.update(state="装入曲库", pct=100)
    if not _install(staged, fname):
        job.update(state="失败", err="装进曲库失败（看服务日志 journalctl -u qqmusic）")
        staged.unlink(missing_ok=True)
        return
    final = DL_DIR / fname

    # 5) 歌词（同名 .lrc 旁路文件 —— 库里既有的约定，Navidrome 认）
    lrc_note = ""
    try:
        lrc = await _lyric(mid)                             # type: ignore[misc]
        if lrc and len(lrc.strip()) > 10:
            # ★ 统一写成 **UTF-8（无 BOM）、LF 换行**：
            #   QQ 接口给的是 JSON 里的字符串（本来就是 UTF-8），但 \r\n 和 BOM
            #   在不同来源上出现过；统一掉，播放器/编辑器都不会乱码。
            txt = lrc.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
            data = txt.encode("utf-8")
            lrc_stage = STAGE_DIR / f"{base}.lrc"
            lrc_stage.write_bytes(data)
            if _install(lrc_stage, f"{base}.lrc"):
                lrc_note = f" + 歌词（UTF-8，{len(data)} 字节）"
            else:
                lrc_note = "（歌词装进曲库失败）"
        else:
            lrc_note = "（这首没歌词）"
    except Exception as e:                                  # noqa: BLE001
        lrc_note = f"（歌词失败: {type(e).__name__}）"
        log.info("歌词没取到: %s", e)

    size = final.stat().st_size if final.exists() else 0
    job.update(state="完成", pct=100, file=str(final), size=size,
               note=f"已存到曲库（{quality_name}）{lrc_note}，Navidrome 约 1 分钟内扫到")
    _manifest[mid] = {"file": str(final), "title": title, "artist": artist,
                      "at": int(time.time()), "size": size}
    _save_manifest()
    log.warning("下载入库: %s（%.1f MB）%s", final.name, size / 1048576, lrc_note)


async def _worker_loop() -> None:
    while True:
        if not _queued:
            await asyncio.sleep(1)
            continue
        job = _queued.pop(0)
        try:
            await _download_one(job["id"])
        except Exception as e:                              # noqa: BLE001
            log.error("下载任务异常: %s", e)
            job.update(state="失败", err=str(e)[:120])
        # 队列排空了 → 触发一次 Navidrome 扫描，让新歌**立刻**进库。
        # 为什么不在每首后面都扫：批量下 60 首会白扫 60 次（每次都要遍历/stat 曲库）。
        # 为什么还是要显式触发：Navidrome 自己有一套调度，但这台机器 4 核/376MB，
        # 等它排到可能要一分钟，用户会以为"下载没成功"。
        if not _queued:
            ok = await _nav_scan()
            last = _jobs[0] if _jobs else None
            if last is not None and last.get("state") == "完成":
                last["note"] = (last.get("note") or "") + (
                    "，已触发曲库扫描" if ok else "，扫描触发失败（文件已就位）")
        await asyncio.sleep(0.5)


async def _nav_scan() -> bool:
    """调 Navidrome 的 startScan（用 player.json 里那套 nav 账号，本机 127.0.0.1）。"""
    try:
        cfg = json.loads(CFG_PATH.read_text("utf-8")) if CFG_PATH.exists() else {}
        nav = cfg.get("nav") or {}
        host = (nav.get("host") or "http://127.0.0.1:4533").rstrip("/")
        user, pw = nav.get("user"), nav.get("password")
        if not user or not pw or _http is None:
            log.warning("没配 Navidrome 账号，跳过扫描触发")
            return False
        salt = secrets.token_hex(8)
        tok = hashlib.md5((pw + salt).encode()).hexdigest()
        params = {"u": user, "t": tok, "s": salt, "v": "1.16.1",
                  "c": "esp-music-dl", "f": "json"}
        r = await _http.get(host + "/rest/startScan.view", params=params, timeout=25)
        j = r.json().get("subsonic-response") or {}
        ok = (j.get("status") == "ok")
        log.info("触发曲库扫描: %s %s", j.get("status"), "" if ok else j)
        return ok
    except Exception as e:                                  # noqa: BLE001
        log.warning("触发曲库扫描失败（文件已经下好了，不影响）: %s", e)
        return False


# ----------------------------------------------------------------- 生命周期

async def startup(resolve_url, meta, lyric) -> None:
    global _resolve_url, _meta, _lyric, _worker, _http
    _resolve_url, _meta, _lyric = resolve_url, meta, lyric
    _http = httpx.AsyncClient(follow_redirects=True)
    _load_manifest()
    try:
        STAGE_DIR.mkdir(parents=True, exist_ok=True)
        log.info("下载就绪：曲库落点 %s（服务用户可写=%s），暂存 %s（可写=%s）",
                 DL_DIR, os.access(DL_DIR, os.W_OK), STAGE_DIR, os.access(STAGE_DIR, os.W_OK))
        if not os.path.exists(INSTALL_HELPER):
            log.error("找不到特权 helper %s —— 下载能下但装不进曲库（见 deploy/qqmusic-install-dl）",
                      INSTALL_HELPER)
    except Exception as e:                                  # noqa: BLE001
        log.error("暂存目录建不了（%s），下载功能不可用: %s", STAGE_DIR, e)
    _worker = asyncio.create_task(_worker_loop())


async def shutdown() -> None:
    if _worker is not None:
        _worker.cancel()
    if _http is not None:
        await _http.aclose()


# ----------------------------------------------------------------- 接口

@router.get("/add")
async def dl_add(id: str = Query(...)) -> dict[str, Any]:
    """下载一首（给的是 QQ 的歌曲 mid）。入队后立刻返回，进度看 /v1/download/list。"""
    cur = _job(id)
    if cur is not None and cur.get("state") in ("准备", "下载中", "写入标签"):
        return {"ok": True, "job": cur, "note": "已经在下了"}
    if len(_queued) >= QUEUE_MAX:
        raise HTTPException(429, f"排队太多了（>{QUEUE_MAX} 首），等一会儿")
    job = {"id": id, "state": "排队", "pct": 0, "bytes": 0, "total": 0,
           "title": "", "artist": "", "album": "", "suffix": "", "file": "",
           "size": 0, "err": None, "note": "", "at": int(time.time())}
    _publish(job)
    _queued.append(job)
    return {"ok": True, "job": job, "queued": len(_queued)}


@router.get("/list")
async def dl_list() -> dict[str, Any]:
    """下载任务/结果列表（最新的在前）。"""
    busy = sum(1 for j in _jobs if j.get("state") in ("准备", "下载中", "写入标签"))
    return {"jobs": _jobs, "queued": len(_queued), "busy": busy,
            "dir": str(DL_DIR), "downloaded": len(_manifest)}


@router.get("/summary")
async def dl_summary() -> dict[str, Any]:
    """一句话下载概况（给 ESP 那种小内存客户端用，避免它拉整份 /list）。

    返回 text 就是一行能直接显示的中文，例如：
      "下载中：歌名 45%（还有 2 首）" / "曲库已收录 12 首" / ""
    """
    busy = [j for j in _jobs if j.get("state") in ("准备", "下载中", "写入标签", "装入曲库", "排队")]
    done = sum(1 for j in _jobs if j.get("state") in ("完成", "已存在"))
    text = ""
    if busy:
        j = busy[0]
        text = "下载中：%s %d%%" % ((j.get("title") or j.get("id") or "")[:20], j.get("pct") or 0)
        if len(busy) > 1:
            text += "（还有 %d 首）" % (len(busy) - 1)
    elif done:
        text = "本次已下载 %d 首" % done
    return {"busy": len(busy), "done": done, "text": text,
            "downloaded": len(_manifest), "dir": str(DL_DIR)}


@router.get("/check")
async def dl_check(id: str = Query(...)) -> dict[str, Any]:
    """这首是不是已经下过了（在清单里，或文件已经在目录里）。"""
    if id in _manifest:
        return {"downloaded": True, "file": _manifest[id].get("file")}
    for j in _jobs:
        if j["id"] == id:
            return {"downloaded": j.get("state") == "完成", "file": j.get("file") or "",
                    "state": j.get("state")}
    return {"downloaded": False}
