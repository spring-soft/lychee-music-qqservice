"""公网面入口：「QQ 版 Navidrome」—— 网页播放 + Subsonic /rest，监听 3211。

和 3210 的 ESP 契约层（app.py）**是两条独立的线**：

  3210  app.py     /v1/*（ESP 专用，内网，无鉴权）+ 服务器 DAC 播放器 + 下载入库
  3211  webapp.py  /rest/*（Subsonic 兼容，带账号密码）+ 网页播放（浏览器本地出声）

为什么要分开进程：3210 那条线会拉起 mpv（管 USB DAC）、带下载/sudo helper、
而且完全没有鉴权。它绝不能出现在公网隧道里。本进程：
  · 不启动 player.py / download.py 的 lifespan（只复用 app.py 的取数函数）
  · 不注册任何 /v1 路由，关闭 /docs、/openapi.json
  · /rest/* 要认证（cookie 或 Subsonic u/t/s），只有一个只读的 /healthz 不带凭据
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

import app as core                     # 复用取数/音质/缓存逻辑（不启动它的 lifespan）
import guard
import subsonic_api
import websession
from qqmusic_api import Client, Credential

log = logging.getLogger("qqweb")

DATA_DIR = Path(os.environ.get("QQ_DATA_DIR", "/data"))
WEB_CONF = Path(os.environ.get("QQ_WEB_CONFIG", str(DATA_DIR / "web.json")))
CRED_PATH = Path(os.environ.get("QQ_CRED_PATH", str(DATA_DIR / "credential.json")))
WWW_WEB = Path(os.environ.get("QQ_WEB_WWW", "/opt/qqmusic/wwwweb"))

_state: dict[str, object] = {"cred_mtime": 0.0}


def load_web_conf() -> dict[str, object]:
    """读网页/Subsonic 的账号密码（data/web.json，权限 600，不进 git）。"""
    user = os.environ.get("QQWEB_USER", "")
    pw = os.environ.get("QQWEB_PASS", "")
    conf: dict[str, object] = {"user": user, "password": pw, "star_write": True}
    if WEB_CONF.exists():
        try:
            raw = json.loads(WEB_CONF.read_text("utf-8"))
            conf.update({k: raw[k] for k in ("user", "password", "star_write") if k in raw})
        except Exception as e:                              # noqa: BLE001
            log.error("读 %s 失败: %s", WEB_CONF, e)
    else:
        log.warning("没有 %s，用环境变量/空账号启动", WEB_CONF)
    return conf


def _load_cred_from(path: Path) -> Credential | None:
    if not path.exists():
        return None
    try:
        return Credential.model_validate(json.loads(path.read_text("utf-8")))
    except Exception as e:                                  # noqa: BLE001
        log.error("读凭据失败: %s", e)
        return None


def _new_client() -> tuple[Client, Credential | None]:
    cred = _load_cred_from(CRED_PATH)
    cli = Client(credential=cred)
    core._state["client"] = cli
    core.set_cred(cred)
    return cli, cred


def _cred_mtime() -> float:
    try:
        return CRED_PATH.stat().st_mtime
    except OSError:
        return 0.0


async def _watch_cred() -> None:
    """ESP 那边重新扫码后凭据文件会变 —— 这里跟着重载，不用重启本服务。"""
    while True:
        await asyncio.sleep(30)
        m = _cred_mtime()
        if m and m != _state["cred_mtime"]:
            try:
                _new_client()
                _state["cred_mtime"] = m
                log.warning("检测到凭据更新，已重新加载")
            except Exception as e:                          # noqa: BLE001
                log.warning("重载凭据失败: %s", e)


async def _warm_loop() -> None:
    """启动后在后台把整份歌单（收藏 900+ 首要翻 10 页）先拉进缓存。"""
    await asyncio.sleep(5)
    while True:
        try:
            await subsonic_api.warm_playlists()
        except Exception as e:                              # noqa: BLE001
            log.warning("歌单预热失败: %s", e)
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    conf = load_web_conf()
    user = str(conf.get("user") or "")
    pw = str(conf.get("password") or "")
    subsonic_api.configure(user, pw, bool(conf.get("star_write", True)),
                           str(conf.get("server_type") or "") or None)
    websession.configure(user, pw)
    if not user or not pw:
        log.error("❌ 没有配置网页账号：请写 %s（{\"user\":..., \"password\":...}）", WEB_CONF)
    cli, cred = _new_client()
    _state["cred_mtime"] = _cred_mtime()
    log.warning("启动：端口 3211，网页用户=%s，QQ 凭据=%s", user, core.cred_state())
    task = asyncio.create_task(_watch_cred())
    warm = asyncio.create_task(_warm_loop())
    try:
        yield
    finally:
        warm.cancel()
        task.cancel()
        try:
            await cli.close()
        except Exception:                                   # noqa: BLE001
            pass


app = FastAPI(title="QQ 音乐（Navidrome 风格）", version="1.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# ★ gzip：收藏歌单 JSON 未压缩约 424KB，压完约 40KB。
#   公网链路是「Cloudflare → 家里上行 → 服务器」，压缩放在源头收益最大（实测 4.1s → 1.x s）。
from starlette.middleware.gzip import GZipMiddleware        # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

app.include_router(subsonic_api.router, prefix="/rest")


# ------------------------------------------------------------------ 登录会话

def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return proto.split(",")[0].strip() == "https"


@app.post("/login")
async def login(request: Request) -> Response:
    """登录表单用 application/x-www-form-urlencoded。

    ⚠️ 故意不用 request.form()：那要额外的 python-multipart 依赖（这台机器 376MB，
       能少一个包就少一个包），而网页只发 urlencoded，自己 parse_qs 就够。
    """
    try:
        raw = await request.body()
        form = parse_qs(raw.decode("utf-8", "replace"))
    except Exception:                                       # noqa: BLE001
        return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
    user = (form.get("user") or [""])[0]
    pw = (form.get("password") or [""])[0]
    ip = guard.client_ip(request)
    if guard.blocked(ip):
        log.warning("已封禁 IP 尝试登录: %s", ip)
        return JSONResponse({"ok": False, "error": "尝试次数过多，请 15 分钟后再试"},
                            status_code=429)
    if not guard.allow(ip):
        return JSONResponse({"ok": False, "error": "请求太频繁，稍后再试"}, status_code=429)
    if not websession.verify_password(user, pw):
        guard.note_login_fail(ip)
        log.warning("网页登录失败（user=%s ip=%s）", user, ip)
        return JSONResponse({"ok": False, "error": "账号或密码不对"}, status_code=401)
    guard.note_login_ok(ip)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(websession.COOKIE, websession.make_token(),
                    max_age=websession.MAX_AGE, httponly=True,
                    samesite="lax", secure=_is_https(request))
    log.warning("网页登录成功（user=%s）", user)
    return resp


@app.post("/logout")
async def logout() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(websession.COOKIE)
    return resp


@app.get("/api/me")
async def me(request: Request) -> Response:
    u = websession.user_from_request(request)
    return JSONResponse({"ok": bool(u), "user": u or ""})


# ------------------------------------------------------------------ 页面

_NO_CACHE = {"Cache-Control": "no-cache"}


def _file(name: str) -> Response:
    f = WWW_WEB / name
    if not f.exists():
        return JSONResponse({"ok": False, "error": f"缺少页面文件 {f}"}, status_code=404)
    return FileResponse(f, headers=_NO_CACHE)


@app.get("/", response_class=HTMLResponse)
async def index() -> Response:
    return _file("index.html")


@app.get("/app.js")
async def app_js() -> Response:
    return _file("app.js")


@app.get("/app.css")
async def app_css() -> Response:
    return _file("app.css")


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """给所有响应加基础安全头（公网面的最低配置）。"""
    resp: Response = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    # 页面只用同源资源；音频会 302 到 QQ CDN，所以 media-src 放开 https。
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; media-src 'self' https: blob:; "
        "connect-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'")
    return resp


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """运维探针（不带凭据）：只回是否起来了 + 账号是否配置 + QQ 凭据状态 + 防护计数。"""
    conf = load_web_conf()
    return JSONResponse({"ok": True,
                         "subsonic_user": conf.get("user") or "",
                         "cred": core.cred_state(),
                         "guard": guard.stats(),
                         "endpoints": ["/rest/ping.view", "/rest/search3.view"]})
