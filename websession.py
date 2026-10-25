"""网页登录会话（HttpOnly cookie）—— 公网页和 /rest 共用一套身份。

为什么不把密码存浏览器 localStorage：网页是**公网**的，密码留在前端等于多一个
泄露面（浏览器同步、插件、共享设备）。这里改成：

  登录一次 → 服务端下发 HMAC 签名的 HttpOnly cookie → 之后 /rest/* 直接认 cookie
  （所以网页里调 /rest 不需要带 u/t/s，也不需要在 JS 里碰密码）

签名密钥从「账号+密码」派生（改了密码，旧会话自动失效），不额外落密钥文件；
密码本身只存在服务端内存里，用来校验登录表单。
第三方 Subsonic 客户端仍走标准 u/t/s 或 u/p —— 那部分在 subsonic_api._auth。
"""
from __future__ import annotations

import hashlib
import hmac

COOKIE = "qqsess"
MAX_AGE = 30 * 86400

_user = ""
_pw = b""
_secret = b""


def configure(user: str, password: str) -> None:
    global _user, _pw, _secret
    _user = user or ""
    _pw = (password or "").encode()
    _secret = hashlib.sha256(("qqweb-session|" + _user + "|" + (password or "")).encode()).digest()


def configured() -> bool:
    return bool(_user and _pw and _secret)


def verify_password(user: str, password: str) -> bool:
    if not configured():
        return False
    ok_user = hmac.compare_digest(user.encode(), _user.encode())
    ok_pass = hmac.compare_digest((password or "").encode(), _pw)
    return ok_user and ok_pass


def make_token() -> str:
    mac = hmac.new(_secret, _user.encode(), hashlib.sha256).hexdigest()
    return _user + "." + mac


def verify_token(token: str | None) -> str | None:
    if not token or not _secret:
        return None
    user, _, mac = token.partition(".")
    if user != _user:
        return None
    want = hmac.new(_secret, _user.encode(), hashlib.sha256).hexdigest()
    return user if hmac.compare_digest(mac, want) else None


def user_from_request(request) -> str | None:
    return verify_token(request.cookies.get(COOKIE))
