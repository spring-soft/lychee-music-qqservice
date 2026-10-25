#!/usr/bin/env python3
"""QQ 音乐凭据自动续期（systemd timer 每 30 分钟跑一次）。

背景：musickey 有效期 3 天（key_expires_in=259200），过期后所有取流接口报
"登录凭证已过期，重新扫码"。上游库提供 refresh_credential()（用 refresh_token 续期），
所以只要定期续，就**不需要频繁扫码**。

策略：剩余有效期 > 2 小时就什么都不做；否则调 refresh 并写回 credential.json。
写回后两个服务都会自己重载（qqweb 看 mtime，qqmusic 有 reload 任务）——
所以这里**只负责刷新，不重启任何服务**。

用法：
    /opt/qqmusic/venv/bin/python /opt/qqmusic/credrefresh.py          # 需要时续期
    /opt/qqmusic/venv/bin/python /opt/qqmusic/credrefresh.py --force  # 强制续期
退出码：0=正常（含无需续期）；1=续期失败（refresh_token 也过期了，需要重新扫码）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from qqmusic_api import Client, Credential

CRED = Path(os.environ.get("QQ_CRED_PATH", "/opt/qqmusic/data/credential.json"))
MIN_REMAIN = 2 * 3600          # 剩余不足 2 小时才续期


def load() -> Credential | None:
    try:
        return Credential.model_validate(json.loads(CRED.read_text("utf-8")))
    except Exception as e:                                   # noqa: BLE001
        print("读凭据失败:", e)
        return None


def remain_seconds(c: Credential) -> int:
    try:
        return int(c.musickey_create_time + c.key_expires_in - time.time())
    except Exception:                                        # noqa: BLE001
        return 0


def save(c: Credential) -> None:
    CRED.write_text(c.model_dump_json(indent=2), "utf-8")
    try:
        os.chmod(CRED, 0o600)
    except OSError:
        pass


async def main() -> int:
    force = "--force" in sys.argv
    cred = load()
    if cred is None:
        return 1
    if not cred.is_valid():
        print("凭据不完整（musicid/musickey 缺失），需要重新扫码")
        return 1
    remain = remain_seconds(cred)
    print("剩余有效期: %d 秒（%.1f 小时），expired=%s" % (remain, remain / 3600, cred.is_expired()))
    if not force and remain > MIN_REMAIN:
        print("还够用，跳过")
        return 0
    cli = Client(credential=cred)
    try:
        new = await cli.login.refresh_credential()
    except Exception as e:                                   # noqa: BLE001
        print("续期失败:", type(e).__name__, e)
        return 1
    finally:
        try:
            await cli.close()
        except Exception:                                    # noqa: BLE001
            pass
    if not new.is_valid():
        print("续期返回的凭据不完整")
        return 1
    save(new)
    print("续期成功，新剩余 %d 秒（%.1f 小时）"
          % (remain_seconds(new), remain_seconds(new) / 3600))
    return 0


sys.exit(asyncio.run(main()))
