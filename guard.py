"""轻量防护：登录/鉴权失败限流、每 IP 请求限速、并发流上限、安全响应头。

为什么自己写而不是靠 Cloudflare：域名是用户自己映射的，配置随时可能被改；
服务端这一层防护不依赖任何外部条件。全部只在内存里（重启清零），够用且零依赖。

设计要点：
  · **只对失败计数**，正常请求不消耗额度（避免把正常播放/滚动歌单的朋友误伤）
  · 客户端 IP：Cloudflare 会带 CF-Connecting-IP；局域网直连则用 socket 地址
  · 失败窗口：登录 5 次/10 分钟 → 封 15 分钟；协议鉴权 20 次/5 分钟 → 封 10 分钟
  · 每 IP 令牌桶：普通请求 120/分钟（几乎不会碰到），登录 20/分钟
  · 并发流：全局最多 8 个「取直链」在途（QQ 上游对并发敏感，也防刷）
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

log = logging.getLogger("qqweb.guard")

LOGIN_FAILS = 5          # 登录失败次数
LOGIN_WINDOW = 600.0     # 统计窗口（秒）
LOGIN_BAN = 900.0        # 封禁时长

AUTH_FAILS = 20
AUTH_WINDOW = 300.0
AUTH_BAN = 600.0

RATE_PER_MIN = 120       # 每 IP 每分钟请求数
STREAM_CONCURRENCY = 8


class _Buckets:
    def __init__(self) -> None:
        self.fail: dict[str, deque[float]] = {}
        self.ban_until: dict[str, float] = {}
        self.req: dict[str, deque[float]] = {}

    def _prune(self, dq: deque[float], window: float, now: float) -> None:
        while dq and now - dq[0] > window:
            dq.popleft()


_b = _Buckets()
_stream_sem = asyncio.Semaphore(STREAM_CONCURRENCY)
_counters = {"blocked": 0, "login_fail": 0, "auth_fail": 0, "too_many": 0}


def client_ip(request) -> str:
    """真实客户端 IP：Cloudflare → XFF → socket。"""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "-"


def blocked(ip: str) -> bool:
    now = time.time()
    until = _b.ban_until.get(ip, 0.0)
    if until and now < until:
        return True
    if until and now >= until:
        _b.ban_until.pop(ip, None)
    return False


def allow(ip: str) -> bool:
    """每 IP 每分钟请求数（滑动窗口）。"""
    now = time.time()
    dq = _b.req.setdefault(ip, deque())
    _b._prune(dq, 60.0, now)
    if len(dq) >= RATE_PER_MIN:
        _counters["too_many"] += 1
        return False
    dq.append(now)
    return True


def note_login_fail(ip: str) -> None:
    now = time.time()
    dq = _b.fail.setdefault("login:" + ip, deque())
    _b._prune(dq, LOGIN_WINDOW, now)
    dq.append(now)
    _counters["login_fail"] += 1
    if len(dq) >= LOGIN_FAILS:
        _b.ban_until[ip] = now + LOGIN_BAN
        log.warning("登录失败 %d 次，封禁 IP %s %d 分钟", len(dq), ip, int(LOGIN_BAN / 60))


def note_login_ok(ip: str) -> None:
    _b.fail.pop("login:" + ip, None)


def note_auth_fail(ip: str) -> None:
    now = time.time()
    dq = _b.fail.setdefault("auth:" + ip, deque())
    _b._prune(dq, AUTH_WINDOW, now)
    dq.append(now)
    _counters["auth_fail"] += 1
    if len(dq) >= AUTH_FAILS:
        _b.ban_until[ip] = now + AUTH_BAN
        log.warning("鉴权失败 %d 次，封禁 IP %s %d 分钟", len(dq), ip, int(AUTH_BAN / 60))


class StreamSlot:
    """并发流闸门。拿不到（10 秒内）就拒绝，别让 QQ 上游被刷爆。"""

    def __init__(self) -> None:
        self.ok = False

    async def __aenter__(self) -> "StreamSlot":
        try:
            await asyncio.wait_for(_stream_sem.acquire(), timeout=10.0)
            self.ok = True
        except asyncio.TimeoutError:
            self.ok = False
        return self

    async def __aexit__(self, *exc) -> None:
        if self.ok:
            _stream_sem.release()


def stream_slot() -> StreamSlot:
    return StreamSlot()


def stats() -> dict[str, int]:
    return {**_counters, "banned": sum(1 for t in _b.ban_until.values() if t > time.time())}
