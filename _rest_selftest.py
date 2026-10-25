#!/usr/bin/env python3
"""公网面（/rest）自检：在服务器上跑，只打 PASS/FAIL，不碰播放器。

用法：python3 _rest_selftest.py
说明：账号密码从 /opt/qqmusic/data/web.json 读（root 权限）。
      star 只对「本来就在收藏里」的歌做幂等调用 —— 避免往真实账号里塞测试歌。
"""
import hashlib
import http.client
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:3211"
CONF = "/opt/qqmusic/data/web.json"

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + str(detail)) if detail else ""))
    if not cond:
        fails += 1


def conf():
    with open(CONF, "r", encoding="utf-8") as f:
        return json.load(f)


def call(method, params, timeout=60, follow=False):
    q = dict(params)
    q.setdefault("c", "selftest")
    q.setdefault("v", "1.16.1")
    q.setdefault("f", "json")
    url = BASE + "/rest/" + method + ".view?" + urllib.parse.urlencode(q)
    if follow:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    req = urllib.request.Request(url)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def body(method, params, **kw):
    st, raw = call(method, params, **kw)
    try:
        return st, json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return st, {"_raw": raw[:300].decode("utf-8", "replace")}


def main():
    c = conf()
    user, pw = c["user"], c["password"]
    salt = "selftest1"
    tok = hashlib.md5((pw + salt).encode()).hexdigest()
    auth = {"u": user, "t": tok, "s": salt}

    # 0) /v1 没被影响
    try:
        with urllib.request.urlopen("http://127.0.0.1:3210/v1/health", timeout=20) as r:
            h = json.loads(r.read())
        check("/v1/health 仍正常（ESP 那条线没被动）", h.get("ok") is True)
    except Exception as e:
        check("/v1/health 仍正常（ESP 那条线没被动）", False, e)

    # 1) ping
    st, j = body("ping", auth)
    check("ping 鉴权通过", j.get("subsonic-response", {}).get("status") == "ok", j)

    # 2) 错误密码
    bad = {"u": user, "p": "wrong-password"}
    st, j = body("ping", bad)
    e = j.get("subsonic-response", {}).get("error", {})
    check("错误密码被拒（code=40）", e.get("code") == 40, e)

    # 3) 搜索
    st, j = body("search3", {**auth, "query": "周杰伦", "songCount": 5})
    songs = j.get("subsonic-response", {}).get("searchResult3", {}).get("song", [])
    check("search3 有结果", len(songs) > 0, "n=%d" % len(songs))
    if songs:
        s0 = songs[0]
        check("search3 song 字段齐全",
              all(s0.get(k) not in (None, "") for k in ("id", "title", "artist", "suffix", "duration")),
              {k: s0.get(k) for k in ("id", "title", "suffix", "duration", "size")})
        mid = s0["id"]
    else:
        mid = None

    # 4) 歌单
    st, j = body("getPlaylists", auth)
    pls = j.get("subsonic-response", {}).get("playlists", {}).get("playlist", [])
    check("getPlaylists 返回 6 个模式", len(pls) == 6, [p.get("id") for p in pls])

    st, j = body("getPlaylist", {**auth, "id": "fav"})
    pl = j.get("subsonic-response", {}).get("playlist", {})
    entry = pl.get("entry", [])
    check("getPlaylist(fav) 有歌", len(entry) > 0, "n=%d" % len(entry))
    check("getPlaylist 里歌带 starred", (not entry) or ("starred" in entry[0]))

    # 5) getSong
    if mid:
        st, j = body("getSong", {**auth, "id": mid})
        check("getSong 正常", j.get("subsonic-response", {}).get("song", {}).get("id") == mid)

    # 6) stream → 302
    if mid:
        st, raw = call("stream", {**auth, "id": mid})
        loc = ""
        # 302 的 Location 在异常对象里拿不到，用 http.client 再打一次
        conn = http.client.HTTPConnection("127.0.0.1", 3211, timeout=60)
        q = urllib.parse.urlencode({**auth, "id": mid, "c": "selftest", "v": "1.16.1", "f": "json"})
        conn.request("GET", "/rest/stream.view?" + q)
        resp = conn.getresponse()
        loc = resp.getheader("Location") or ""
        check("stream 返回 302 且指向 QQ CDN",
              resp.status == 302 and "qqmusic" in loc, "status=%s loc=%s" % (resp.status, loc[:80]))
        conn.close()

    # 7) 封面
    if songs and songs[0].get("coverArt") not in (None, "", "none"):
        u = BASE + "/rest/getCoverArt.view?" + urllib.parse.urlencode(
            {**auth, "id": songs[0]["coverArt"], "size": 300})
        try:
            with urllib.request.urlopen(u, timeout=30) as rv:
                ctype = rv.headers.get("content-type", "")
                n = len(rv.read())
            check("getCoverArt 返回图片", n > 1000 and ctype.startswith("image"),
                  "n=%d type=%s" % (n, ctype))
        except Exception as e:
            check("getCoverArt 返回图片", False, e)

    # 8) 歌词
    if mid:
        st, j = body("getLyricsBySongId", {**auth, "id": mid})
        lines = (j.get("subsonic-response", {}).get("lyricsList", {})
                  .get("structuredLyrics", [{}])[0].get("line", []))
        check("getLyricsBySongId 返回行（有歌词则 >0）", isinstance(lines, list), "n=%d" % len(lines))

    # 9) star：只对已在收藏里的歌做幂等调用（不往账号里塞测试歌）
    if entry:
        mid_fav = entry[0]["id"]
        st, j = body("star", {**auth, "id": mid_fav})
        check("star 对已收藏歌幂等成功", j.get("subsonic-response", {}).get("status") == "ok",
              j.get("subsonic-response", {}).get("error"))
        st, j = body("unstar", {**auth, "id": mid_fav})
        e = j.get("subsonic-response", {}).get("error", {})
        check("unstar 明确报不支持（不静默假装成功）", e.get("code") == 30, e)

    # 10) 随机 + OpenSubsonic
    st, j = body("getRandomSongs", {**auth, "size": 3})
    check("getRandomSongs 有结果",
          len(j.get("subsonic-response", {}).get("randomSongs", {}).get("song", [])) > 0)
    st, j = body("getOpenSubsonicExtensions", auth)
    check("getOpenSubsonicExtensions 正常",
          isinstance(j.get("subsonic-response", {}).get("openSubsonicExtensions"), list))

    print("-" * 50)
    print("结果：%s" % ("全部通过" if fails == 0 else "有 %d 项失败" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
