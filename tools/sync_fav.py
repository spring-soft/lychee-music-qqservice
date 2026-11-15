#!/usr/bin/env python3
"""sync_fav —— 把 QQ 音乐「收藏」里 Navidrome 还没有的歌，下载进曲库（带封面 + UTF-8 歌词）。

一次跑完的完整流程（就是 2026-11-15 那次手工同步固化下来的）：
  1) 读 QQ 收藏（分页 100，带重试）
  2) 读 Navidrome sqlite（只读）建索引，按 歌名+歌手+时长 比对，算出"缺哪些"
  3) 把缺的按批（默认 40 首）提交给服务器下载器（/v1/download/add），等排空再下一批
  4) 重试若干轮（QQ 接口偶发超时，重试基本都能成）
  5) 给"音频已在曲库、但缺 .lrc"的歌补歌词（按文件名反查收藏里的 mid）
  6) 质检：ffprobe 查封面/时长，统计歌词覆盖
  7) 输出：报告 JSON + Markdown 摘要；"付费单曲无权限"的清单单独存一份，等以后补

用法（在服务器上）：
    /opt/qqmusic/venv/bin/python /opt/qqmusic/sync_fav.py --dry-run     # 只看要下多少
    /opt/qqmusic/venv/bin/python /opt/qqmusic/sync_fav.py               # 真下（默认 2 轮重试）
    ... --passes 3 --batch 30 --limit 50                                # 调参
    ... --no-lyrics --no-check                                          # 跳过补词/质检

为什么这些坑要写进代码里（都是实测踩过的）：
  · 曲库盘可能掉线（exfat/USB 供电不稳）→ 写盘前必须确认 /mnt/usb 真的挂载，
    否则会写进根分区把 3.5G 系统撑爆，Navidrome 还会扫到空目录
  · 下载器装完文件后才写歌词 → 装盘失败时歌词会被跳过（曾误判 46 首"无歌词"）
  · /v1/lyric 返回的是**纯文本 LRC**（不是 JSON），别用 json 解析
  · QQ 收藏里有一些"付费单曲/数字专辑"，绿钻也返回 104003 无播放权限 → 单独记录
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

HOST = "http://127.0.0.1:3210"
MUSIC = Path(os.environ.get("QQ_MUSIC_DIR", "/mnt/usb/music"))
DL = MUSIC / "QQ下载"
NAV_DB = "/var/lib/navidrome/navidrome.db"
DATA = Path("/opt/qqmusic/data")
LOCK = Path("/tmp/sync_fav.lock")


# ----------------------------------------------------------------- 小工具

def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def http_get(url: str, timeout: float = 60) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                     # 4xx/5xx 也把 body 读出来（含错误原因）
        try:
            return e.read().decode("utf-8", "replace")
        except Exception:                                   # noqa: BLE001
            return None
    except Exception as e:                                  # noqa: BLE001
        # 把真实原因写到 stderr（默认静默会让"连不上"变成一句没用的 no response）
        sys.stderr.write("[http] %s 失败: %s: %s\n" % (url, type(e).__name__, str(e)[:160]))
        return None


def api(path: str, timeout: float = 60) -> dict:
    """path 可以是相对（/v1/...）也可以是完整 URL（脚本里两种写法都有）。"""
    url = path if path.startswith("http") else HOST + path
    raw = http_get(url, timeout)
    if not raw:
        return {"_err": "no response"}
    try:
        return json.loads(raw)
    except Exception:                                       # noqa: BLE001
        return {"_raw": raw[:200]}


def api_retry(path: str, tries: int = 3, gap: float = 4.0, timeout: float = 60) -> dict:
    last = None
    for i in range(tries):
        r = api(path, timeout)
        if "_err" not in r:
            return r
        last = r["_err"]
        time.sleep(gap)
    return {"_err": last or "unknown"}


def norm(s: str) -> str:
    return re.sub(r"[\s\-_·'\"“”‘’()（）\[\]【】/_]+", "", (s or "").lower())


def artist_tokens(s: str) -> set[str]:
    return {norm(x) for x in re.split(r"[/,&、;；]+", s or "") if norm(x)}


def music_mounted() -> bool:
    try:
        return os.path.ismount(str(MUSIC.parent))
    except OSError:
        return False


def wait_mount(seconds: int = 300) -> bool:
    t0 = time.time()
    while time.time() - t0 < seconds:
        if music_mounted():
            return True
        log("⚠ 曲库盘未挂载，等待中…（%ds）" % int(time.time() - t0))
        time.sleep(10)
    return music_mounted()


# ----------------------------------------------------------------- 数据

def load_favorites(limit: int = 0) -> list[dict]:
    items: list[dict] = []
    page = 1
    while page <= 40:
        r = api_retry(f"{HOST}/v1/list?mode=fav&limit=100&page={page}")
        if "_err" in r:
            raise RuntimeError(f"拉收藏失败（第 {page} 页）: {r['_err']}")
        got = r.get("items") or []
        items += got
        log("  收藏第 %d 页: %d 首" % (page, len(got)))
        if len(got) < 100:
            break
        page += 1
        if limit and len(items) >= limit:
            break
    return items[:limit] if limit else items


def load_library_index() -> dict[str, list[tuple[set[str], int]]]:
    con = sqlite3.connect(f"file:{NAV_DB}?mode=ro", uri=True)
    idx: dict[str, list[tuple[set[str], int]]] = {}
    try:
        for title, artist, dur in con.execute("select title, artist, duration from media_file"):
            k = norm(title)
            if k:
                idx.setdefault(k, []).append((artist_tokens(artist), int(dur or 0)))
    finally:
        con.close()
    return idx


def in_library(lib: dict, it: dict) -> bool:
    """歌名归一化相同 + （歌手有交集或时长差≤3s）→ 视为库里已有。"""
    key = norm(it.get("title"))
    if not key:
        return False
    dur = int(it.get("duration") or 0)
    want = artist_tokens(it.get("artist"))
    for at, d in lib.get(key, []):
        same_artist = bool(want & at) or not want or not at
        if same_artist and (dur == 0 or d == 0 or abs(d - dur) <= 3):
            return True
    return False


# ----------------------------------------------------------------- 下载

def queue_busy() -> tuple[int, int]:
    d = api(f"{HOST}/v1/download/list")
    busy = [j for j in (d.get("jobs") or [])
            if j.get("state") in ("排队", "准备", "下载中", "写入标签", "装入曲库")]
    return len(busy), (d.get("queued") or 0)


def wait_idle(timeout: int = 1800) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        busy, queued = queue_busy()
        if busy == 0 and queued == 0:
            return True
        time.sleep(3)
    return False


def download_pass(missing: list[dict], batch: int, force: bool = False) -> tuple[Counter, list[dict], list[dict]]:
    """把 missing 分批入队并等完成。返回 (状态计数, 付费无权限清单, 其它失败清单)。"""
    stats: Counter = Counter()
    noauth: list[dict] = []
    failed: list[dict] = []
    for ci in range(0, len(missing), batch):
        if not wait_mount(120):
            log("✗ 曲库盘掉了，中止本轮")
            break
        chunk = missing[ci:ci + batch]
        wait_idle()
        ids = {it["id"] for it in chunk}
        for it in chunk:
            q = f"{HOST}/v1/download/add?id={urllib.parse.quote(it['id'])}"
            if force:
                q += "&force=1"      # 跳过下载器三层判重（search3 的模糊匹配会误判"曲库已有"）
            r = api_retry(q, tries=2, gap=2)
            if "_err" in r:
                stats["提交失败"] += 1
                failed.append({"title": it.get("title"), "reason": r["_err"]})
            time.sleep(0.2)
        log("  已提交 %d/%d，等待下载…" % (min(ci + batch, len(missing)), len(missing)))
        wait_idle()
        for j in (api(f"{HOST}/v1/download/list").get("jobs") or []):
            if j.get("id") not in ids:
                continue
            st = j.get("state")
            err = j.get("err") or ""
            stats[st] += 1
            item = next((x for x in chunk if x["id"] == j["id"]), {})
            if st == "失败":
                if "104003" in err or "权限" in err:
                    noauth.append({"title": item.get("title"), "artist": item.get("artist"),
                                   "suffix": item.get("suffix")})
                else:
                    failed.append({"title": item.get("title"), "reason": err[:80]})
        log("  本轮累计: %s" % dict(stats))
    return stats, noauth, failed


# ----------------------------------------------------------------- 歌词 / 质检

def backfill_lyrics(favs: list[dict]) -> tuple[int, list[dict]]:
    log("-- 补歌词（/v1/lyric 是纯文本 LRC）--")
    by_name: dict[str, dict] = {}
    for it in favs:
        by_name[norm("%s - %s" % (it.get("artist") or "", it.get("title") or ""))] = it
        by_name.setdefault(norm(it.get("title") or ""), it)
    if not DL.exists():
        log("  下载目录不存在，跳过")
        return 0, []
    added = 0
    no_lyrics: list[dict] = []
    for fn in sorted(os.listdir(DL)):
        if not fn.lower().endswith((".flac", ".mp3", ".m4a")):
            continue
        base = fn.rsplit(".", 1)[0]
        lrc = DL / (base + ".lrc")
        if lrc.exists():
            continue
        it = by_name.get(norm(base)) or by_name.get(norm(base.split(" - ", 1)[-1]))
        if it is None:
            no_lyrics.append({"file": fn, "why": "文件名对不上收藏"})
            continue
        txt = http_get(f"{HOST}/v1/lyric?id={urllib.parse.quote(it['id'])}", 40)
        if not txt or len(txt.strip()) < 10:
            no_lyrics.append({"file": fn, "why": "QQ 无歌词"})
            continue
        data = txt.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").encode("utf-8")
        lrc.write_bytes(data)
        added += 1
    log("  补歌词: 新增 %d 首，缺 %d 首" % (added, len(no_lyrics)))
    return added, no_lyrics


def quality_check() -> dict:
    log("-- 质检（封面 / 时长 / 歌词）--")
    files = [f for f in os.listdir(DL) if f.lower().endswith((".flac", ".mp3", ".m4a"))] if DL.exists() else []
    no_cover: list[str] = []
    bad: list[str] = []
    flac = mp3 = 0
    for fn in sorted(files):
        if fn.lower().endswith(".flac"):
            flac += 1
        else:
            mp3 += 1
        p = str(DL / fn)
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                            "-show_entries", "format=duration", "-of", "json", p],
                           capture_output=True, text=True)
        try:
            d = json.loads(r.stdout or "{}")
        except Exception:                                   # noqa: BLE001
            d = {}
        types = [s.get("codec_type") for s in (d.get("streams") or [])]
        dur = float((d.get("format") or {}).get("duration") or 0)
        if dur <= 0:
            bad.append(fn)
        if "video" not in types:
            no_cover.append(fn)
    lrc = len([f for f in os.listdir(DL) if f.lower().endswith(".lrc")]) if DL.exists() else 0
    res = {"audio": len(files), "flac": flac, "mp3": mp3, "lrc": lrc,
           "no_cover": no_cover, "bad_duration": bad}
    log("  音频 %d（flac %d / mp3 %d）| 歌词 %d | 无封面 %d | 时长异常 %d"
        % (len(files), flac, mp3, lrc, len(no_cover), len(bad)))
    return res


# ----------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="把 QQ 收藏里缺的歌同步进 Navidrome 曲库")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不下载")
    ap.add_argument("--passes", type=int, default=2, help="下载重试轮数（默认 2）")
    ap.add_argument("--batch", type=int, default=40, help="每批提交多少首（默认 40）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少首（调试用）")
    ap.add_argument("--no-lyrics", action="store_true", help="跳过补歌词")
    ap.add_argument("--no-check", action="store_true", help="跳过质检")
    ap.add_argument("--force", action="store_true",
                    help="强制下载：跳过下载器判重（真缺的歌可能被模糊匹配误判成已有）")
    args = ap.parse_args()

    t_start = time.time()
    log("==== sync_fav 开始 ====  host=%s dry_run=%s passes=%d batch=%d"
        % (HOST, args.dry_run, args.passes, args.batch))

    if not wait_mount():
        log("✗ 曲库盘一直没挂载，退出（先检查 U 盘）")
        return 2
    h = api_retry(f"{HOST}/v1/health", tries=6, gap=5)
    if not h.get("ok"):
        log("✗ QQ 服务不可用: %s" % (h.get("_err") or h))
        return 2
    cred = h.get("credential") or {}
    log("QQ 凭据: present=%s valid=%s expired=%s" % (cred.get("present"), cred.get("valid"), cred.get("expired")))

    favs = load_favorites(args.limit)
    if len(favs) < 10:
        log("✗ 收藏只拉到 %d 首，明显不对，退出（避免误判成\"没有缺歌\"）" % len(favs))
        return 2
    lib = load_library_index()
    missing = [it for it in favs if not in_library(lib, it)]
    log("收藏 %d 首 | Navidrome 索引 %d 个歌名 | 缺 %d 首" % (len(favs), len(lib), len(missing)))
    if args.dry_run:
        for it in missing[:20]:
            log("   - %s | %s | %s" % (it.get("title"), it.get("artist"), it.get("suffix")))
        log("==== dry-run 结束（%ds）====" % int(time.time() - t_start))
        return 0
    if not missing:
        log("没有缺的歌，收工")
        return 0

    all_stats: Counter = Counter()
    noauth: list[dict] = []
    failed: list[dict] = []
    for p in range(1, max(1, args.passes) + 1):
        log("-- 第 %d/%d 轮下载 --" % (p, args.passes))
        stats, na, fl = download_pass(missing, args.batch, force=args.force)
        all_stats.update(stats)
        for x in na:
            if x["title"] not in {y["title"] for y in noauth}:
                noauth.append(x)
        failed = fl
        lib = load_library_index()                          # 重新算还缺哪些
        missing = [it for it in favs if not in_library(lib, it)]
        log("  第 %d 轮后仍缺 %d 首" % (p, len(missing)))
        if not missing:
            break

    lyrics_added = 0
    no_lyrics: list[dict] = []
    if not args.no_lyrics:
        lyrics_added, no_lyrics = backfill_lyrics(favs)
    chk = {} if args.no_check else quality_check()
    lib = load_library_index()
    remaining = [it for it in favs if not in_library(lib, it)]

    DATA.mkdir(parents=True, exist_ok=True)
    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "favorites": len(favs),
        "library_songs": sum(len(v) for v in lib.values()),
        "downloaded_ok": all_stats.get("完成", 0),
        "already_in_library": all_stats.get("已存在", 0),
        "paid_no_permission": noauth,
        "failed": failed,
        "still_missing": [{"title": i.get("title"), "artist": i.get("artist"),
                           "suffix": i.get("suffix")} for i in remaining],
        "lyrics_added": lyrics_added,
        "no_lyrics": no_lyrics,
        "quality": chk,
        "elapsed_s": int(time.time() - t_start),
    }
    (DATA / "sync_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")
    (DATA / "sync_noauth.json").write_text(json.dumps(noauth, ensure_ascii=False, indent=1), "utf-8")

    log("==== 完成（%ds）====" % int(time.time() - t_start))
    log("下载成功 %d | 库里已有 %d | 付费无权限 %d | 仍缺 %d"
        % (report["downloaded_ok"], report["already_in_library"],
           len(noauth), len(remaining)))
    if chk:
        log("音频 %d（flac %d / mp3 %d）| 歌词 %d | 无封面 %d"
            % (chk.get("audio", 0), chk.get("flac", 0), chk.get("mp3", 0),
               chk.get("lrc", 0), len(chk.get("no_cover") or [])))
    log("报告: /opt/qqmusic/data/sync_report.json ；付费清单: sync_noauth.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
