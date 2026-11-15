#!/usr/bin/env python3
"""sync_fav_playlist —— 把 QQ 音乐「收藏」按**原顺序**同步成 Navidrome 歌单。

用户要的：QQ 收藏 → Navidrome 里同名歌单「收藏」，顺序一模一样。

用法（服务器上）：
    /opt/qqmusic/venv/bin/python /opt/qqmusic/sync_fav_playlist.py --dry-run   # 只看会写多少首
    /opt/qqmusic/venv/bin/python /opt/qqmusic/sync_fav_playlist.py             # 真写
    ... --name 收藏            # 歌单名（默认「收藏」，已存在会先删再建）
    ... --star                 # 顺便打星标（默认不打）

⚠️ 关键坑（2026-11-15 实测）：
  分批 createPlaylist / updatePlaylist(songIdToAdd) 写入时，**Navidrome 是"前插"**，
  分批写就会把顺序整体弄反（第一次做出来是反的，用户一眼看出来：QQ 第一首跑到了最后）。
  所以现在**一次性 POST 写入全部 songId**，写完回读校验；万一还是反的，
  自动删掉用 reversed 再写一次。

其它要点：
  · 顺序以 /v1/list?mode=fav 的分页返回顺序为准（与 QQ App 收藏列表一致）
  · 只收录 Navidrome 曲库里**真的有**的歌：读 sqlite 建索引，
    按 歌名(归一化) + 歌手(有交集) + 时长(±3s) 匹配
  · 库里没有的歌（付费单曲等）会列出来，歌单里自然就少这些——先把歌补齐再跑本脚本
  · getPlaylist 在这台 Navidrome 上**会忽略 songCount**、一次返回全部条目，别按分页写循环
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

NAV = "http://127.0.0.1:4533"
QQ = "http://127.0.0.1:3210"
USER, PASS = "665858hjn", "20120220hjn"
DB = "/var/lib/navidrome/navidrome.db"
DEFAULT_NAME = "收藏"


def log(*a):
    print(time.strftime("%H:%M:%S") + " " + " ".join(str(x) for x in a), flush=True)


def _params(**kw):
    salt = secrets.token_hex(8)
    tok = hashlib.md5((PASS + salt).encode()).hexdigest()
    p = {"u": USER, "t": tok, "s": salt, "v": "1.16.1", "c": "favplaylist", "f": "json"}
    p.update(kw)
    return p


def nav(ep: str, **kw):
    body = urllib.parse.urlencode(_params(**kw), doseq=True).encode()
    req = urllib.request.Request(NAV + "/rest/" + ep + ".view", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read().decode())["subsonic-response"]


def qq(path: str, tries: int = 5):
    last = None
    for _ in range(tries):
        try:
            return json.loads(urllib.request.urlopen(QQ + path, timeout=90).read().decode())
        except Exception as e:                              # noqa: BLE001
            last = e
            time.sleep(4)
    raise RuntimeError("QQ 请求失败: %s" % last)


def norm(s: str) -> str:
    return re.sub(r"[\s\-_·'\"“”‘’()（）\[\]【】/_]+", "", (s or "").lower())


def toks(s: str) -> set[str]:
    return {norm(x) for x in re.split(r"[/,&、;；]+", s or "") if norm(x)}


def main() -> int:
    ap = argparse.ArgumentParser(description="QQ 收藏 → Navidrome 歌单（保序）")
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--star", action="store_true",
                    help="顺便把歌打星标（默认不打：用户希望只维护歌单，不要动星标）")
    args = ap.parse_args()

    # 1) QQ 收藏（顺序）
    favs, page = [], 1
    while page <= 40:
        got = qq("/v1/list?mode=fav&limit=100&page=%d" % page).get("items") or []
        favs += got
        log("QQ 收藏第 %d 页: %d 首" % (page, len(got)))
        if len(got) < 100:
            break
        page += 1
    log("QQ 收藏共 %d 首" % len(favs))
    if len(favs) < 10:
        log("✗ 收藏只拉到 %d 首，明显不对，退出" % len(favs))
        return 2

    # 2) 曲库索引
    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    idx: dict[str, list] = {}
    try:
        for sid, t, a, dd in con.execute("select id,title,artist,duration from media_file"):
            k = norm(t)
            if k:
                idx.setdefault(k, []).append((sid, toks(a), int(dd or 0)))
    finally:
        con.close()
    log("曲库索引: %d 个歌名" % len(idx))

    def find(it):
        k = norm(it.get("title"))
        if not k:
            return None
        dur, want = int(it.get("duration") or 0), toks(it.get("artist"))
        for sid, at, dd in idx.get(k, []):
            if (bool(want & at) or not want or not at) and (dur == 0 or dd == 0 or abs(dd - dur) <= 3):
                return sid
        return None

    ids, missing = [], []
    for it in favs:
        sid = find(it)
        if sid:
            ids.append(sid)
        else:
            missing.append({"title": it.get("title"), "artist": it.get("artist")})
    log("可映射 %d 首；曲库里缺 %d 首" % (len(ids), len(missing)))
    if missing:
        log("  缺的前 5 首: " + "、".join((m["title"] or "") for m in missing[:5]))
    if args.dry_run:
        log("dry-run：会写入 %d 首到歌单「%s」" % (len(ids), args.name))
        return 0
    if not ids:
        log("没有可写的歌，退出")
        return 2

    # 3) 删同名旧歌单（保证顺序从零开始）
    existing = (nav("getPlaylists").get("playlists") or {}).get("playlist") or []
    for p in existing:
        if p.get("name") == args.name:
            nav("deletePlaylist", id=p["id"])
            log("删除旧歌单 %s（%s 首）" % (p.get("id"), p.get("songCount")))

    # 4) **一次性**写入（分批会因"前插"把顺序弄反）
    r = nav("createPlaylist", name=args.name, songId=ids)
    pid = (r.get("playlist") or {}).get("id")
    log("创建歌单 %s（一次写入 %d 首）" % (pid, len(ids)))

    # 5) 回读校验（这台 Navidrome 的 getPlaylist 一次返回全部条目）
    def read_back():
        ent = (nav("getPlaylist", id=pid).get("playlist") or {}).get("entry") or []
        return [e.get("id") for e in ent]

    got = read_back()
    if got != ids and got == list(reversed(ids)):
        log("回读发现顺序被反转 → 删掉并用 reversed 重写")
        nav("deletePlaylist", id=pid)
        r = nav("createPlaylist", name=args.name, songId=list(reversed(ids)))
        pid = (r.get("playlist") or {}).get("id")
        got = read_back()

    ok = got == ids
    log("回读 %d 首；顺序与 QQ 一致: %s" % (len(got), ok))
    if not ok:
        for i in range(min(len(got), len(ids))):
            if got[i] != ids[i]:
                log("  首处差异 第 %d 首: 歌单 %s / 期望 %s" % (i + 1, got[i], ids[i]))
                break

    # 6) 打星标（**默认不做**；用户要求只用歌单维护顺序，星标由用户自己管）
    starred = 0
    if args.star:
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            if nav("star", id=chunk).get("status") != "failed":
                starred += len(chunk)
            time.sleep(0.15)
        log("星标 %d 首" % starred)

    rep = {"playlist_id": pid, "name": args.name, "qq_total": len(favs),
           "mapped": len(ids), "actual": len(got), "order_ok": ok,
           "starred": starred, "missing": missing}
    open("/opt/qqmusic/data/fav_playlist_report.json", "w", encoding="utf-8").write(
        json.dumps(rep, ensure_ascii=False, indent=1))
    log("完成：歌单「%s」%d 首，顺序一致=%s" % (args.name, len(got), ok))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
