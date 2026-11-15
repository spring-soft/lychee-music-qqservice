#!/usr/bin/env python3
"""蓝牙音箱管理：扫描 / 配对连接 / 断开 / 记住上次设备。

为什么单独一个模块：
  · 3210 的 QQ 契约层（app.py）和播放器（player.py）都要用它
  · 真正的蓝操作需要 root（bluez 的 polkit 授权），所以走一个**只做固定几件事**的
    sudo helper（deploy/qqmusic-bt），服务用户 qqmusic 不需要 root 权限

音频输出路径（和 USB DAC 完全独立）：
  · bluealsa 以 `-p a2dp-source` 运行，ALSA 插件 `libasound2-plugin-bluez` 把
    手机式 A2DP 推流暴露成一个 ALSA PCM
  · mpv 用 `--audio-device=alsa/bluealsa:DEV=<MAC>,PROFILE=a2dp` 直接输出到音箱
    （实测这个写法不用写 /etc/asound.conf；早期测试用 aplay 验证过）
  · 实测协商结果：A2DP / SBC 44.1kHz 双声道 bitpool=47 ≈ 551kbps（音箱只支持 SBC）

约定（用户要求）：
  · **没选蓝牙时不保持连接** —— 切回 DAC 时这里会主动 disconnect
  · 选择蓝牙但没进配对界面时，默认连"上次连过的设备"（记在 data/bt.json）
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

DATA_DIR = Path(os.environ.get("QQ_DATA_DIR", "/opt/qqmusic/data"))
STATE = DATA_DIR / "bt.json"
HELPER = "/usr/local/bin/qqmusic-bt"
SCAN_LOG = Path("/tmp/qqmusic-bt-scan.log")

router = APIRouter(prefix="/v1/bt", tags=["bt"])

_scan_task: asyncio.Task | None = None


# ------------------------------------------------------------------ 状态文件

def _load() -> dict[str, Any]:
    try:
        d = json.loads(STATE.read_text("utf-8"))
        if isinstance(d, dict):
            return {"last_addr": str(d.get("last_addr") or ""),
                    "last_name": str(d.get("last_name") or ""),
                    "scan_at": float(d.get("scan_at") or 0)}
    except Exception:                                       # noqa: BLE001
        pass
    return {"last_addr": "", "last_name": "", "scan_at": 0.0}


def _save(d: dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
        os.chmod(STATE, 0o600)
    except Exception:                                       # noqa: BLE001
        pass


def last() -> dict[str, str]:
    """上次连过的设备（不跑任何蓝牙命令，快速返回，供状态接口用）。"""
    st = _load()
    return {"addr": st["last_addr"], "name": st["last_name"]}


# ------------------------------------------------------------------ 底层调用

def _run(args: list[str], timeout: float = 30.0) -> tuple[int, str]:
    """调 sudo helper（只放行固定几个子命令）。"""
    try:
        p = subprocess.run(["sudo", "-n", HELPER, *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "超时"
    except Exception as e:                                  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def alsa_device(addr: str) -> str:
    """mpv --audio-device 用的字符串。

    ★ 2026-11-15 实测结论：bluealsa 那条路（alsa/bluealsa:DEV=...）在本机推 A2DP
      永远是"假推流"（状态 RUNNING、音箱没声/灯不闪）。改用 PulseAudio：
      PA 的 bluez5 模块建立 bluez_sink.<MAC>.a2dp_sink，实测能真正出声。
      所以这里返回 pulse/<sink>，并且播放器会用 --ao=pulse。
    """
    key = addr.replace(":", "_").upper()
    return f"pulse/bluez_sink.{key}.a2dp_sink"


def _parse_info(txt: str) -> dict[str, Any]:
    out = {"addr": "", "name": "", "connected": False, "paired": False, "trusted": False}
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("Device "):
            out["addr"] = s.split()[1]
        elif s.startswith("Name:") or s.startswith("Alias:"):
            out["name"] = out["name"] or s.split(":", 1)[1].strip()
        elif s.startswith("Connected:"):
            out["connected"] = s.split(":", 1)[1].strip().lower() == "yes"
        elif s.startswith("Paired:"):
            out["paired"] = s.split(":", 1)[1].strip().lower() == "yes"
        elif s.startswith("Trusted:"):
            out["trusted"] = s.split(":", 1)[1].strip().lower() == "yes"
    return out


def device_info(addr: str) -> dict[str, Any]:
    _rc, txt = _run(["info", addr], 10)
    d = _parse_info(txt)
    d["addr"] = d["addr"] or addr
    return d


def _pcm_exists(addr: str) -> bool:
    """bluealsa 里有没有这个设备的 A2DP 输出。出现 = 真的可以出声了。"""
    key = "dev_" + addr.replace(":", "_").upper()
    try:
        out = subprocess.run(["bluealsa-cli", "list-pcms"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:                                       # noqa: BLE001
        return False
    return key in (out or "") and "a2dpsrc" in (out or "")


# ------------------------------------------------------------------ 对外动作

def _sh(args: list[str]) -> str:
    """跑本地命令（pactl 等，走 PULSE_SERVER 环境变量；失败返回空串）。"""
    env = dict(os.environ)
    env.setdefault("PULSE_SERVER", "unix:/var/run/pulse/native")
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, env=env).stdout
    except Exception:                                       # noqa: BLE001
        return ""


def status() -> dict[str, Any]:
    st = _load()
    cur: dict[str, Any] | None = None
    if st["last_addr"]:
        info = device_info(st["last_addr"])
        if info.get("connected"):
            cur = {"addr": info["addr"], "name": info["name"] or st["last_name"],
                   "pcm": _pcm_exists(info["addr"])}
    return {"ok": True,
            "connected": cur,
            "last": {"addr": st["last_addr"], "name": st["last_name"]} if st["last_addr"] else None,
            "scan_at": st.get("scan_at") or 0,
            "note": "没选蓝牙时不会保持连接；选择蓝牙时默认连上次的设备"}


async def connect(addr: str | None = None, wait_s: float = 15.0) -> dict[str, Any]:
    """连接音箱（addr 为空则连上次那台）。成功返回 {addr,name,device}。"""
    st = _load()
    addr = (addr or st["last_addr"] or "").strip().upper()
    if not addr:
        raise HTTPException(400, "还没有配对过蓝牙音箱：请长按 K3 进配对界面选一台")
    loop = asyncio.get_running_loop()
    _rc, txt = await loop.run_in_executor(None, lambda: _run(["connect", addr], 30))
    log_txt = txt.strip().splitlines()[-1] if txt.strip() else ""
    info = device_info(addr)
    if not info.get("connected"):
        raise HTTPException(502, f"连不上蓝牙音箱（{info.get('name') or addr}）：{log_txt[:80]}")
    # 等 PulseAudio 建出 bluez sink（通常 1~5 秒），然后把它设成默认、硬件音量 100%
    # ★ 为什么用 PA 而不是 bluealsa：bluealsa 在本机是"假推流"（状态 RUNNING 但
    #   音箱收不到、灯不闪）；PA 的 bluez5 实测能真正出声。
    # ★ 音量：sink 有 HW_VOLUME_CTRL，设 100% = 直接设音箱硬件音量（等效手机那个
    #   "手机和音箱音量相同"选项）；绝不超 100%，也不要加数字增益（会破音）。
    sink = "bluez_sink." + addr.replace(":", "_").upper() + ".a2dp_sink"
    t0 = time.time()
    while time.time() - t0 < wait_s:
        r = _sh(["pactl", "list", "short", "sinks"])
        if sink in r:
            break
        await asyncio.sleep(0.5)
    _sh(["pactl", "set-default-sink", sink])
    _sh(["pactl", "set-sink-mute", sink, "0"])
    _sh(["pactl", "set-sink-volume", sink, "100%"])
    st["last_addr"] = addr
    st["last_name"] = info.get("name") or st.get("last_name") or ""
    _save(st)
    return {"addr": addr, "name": info.get("name") or "", "device": alsa_device(addr),
            "pcm": _pcm_exists(addr)}


async def disconnect() -> dict[str, Any]:
    """断开并**取消信任** —— 不这样做的话，音箱会自己回连（用户要求：没选蓝牙就不连）。"""
    st = _load()
    addr = st["last_addr"]
    if addr:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: _run(["disconnect", addr], 25))
    return {"ok": True, "addr": addr}


def _parse_scan() -> list[dict[str, Any]]:
    """解析 bluetoothctl scan 的输出：[NEW] Device AA:BB:.. 名字"""
    seen: dict[str, str] = {}
    try:
        txt = SCAN_LOG.read_text("utf-8", "replace")
    except Exception:                                       # noqa: BLE001
        return []
    for line in txt.splitlines():
        m = re.search(r"^(?:\[NEW\]\s+)?Device\s+([0-9A-Fa-f:]{17})\s*(.*)$", line.strip())
        if m:
            seen[m.group(1).upper()] = (m.group(2) or "").strip()
    items = []
    for addr, name in seen.items():
        info = device_info(addr)
        items.append({"addr": addr, "name": name or info.get("name") or "",
                      "paired": bool(info.get("paired")),
                      "connected": bool(info.get("connected"))})
    items.sort(key=lambda x: (not x["connected"], not x["paired"], x["name"].lower()))
    return items


async def scan(seconds: int = 8) -> dict[str, Any]:
    """开始扫描（后台跑，立即返回）。扫描结果用 /v1/bt/devices 取。"""
    global _scan_task
    seconds = max(4, min(15, int(seconds)))   # 别扫太久：这台是 WiFi/BT 组合芯片，扫描会干扰 WiFi
    if _scan_task is not None and not _scan_task.done():
        return {"ok": True, "scanning": True, "note": "已经在扫描了"}
    loop = asyncio.get_running_loop()

    async def _do() -> None:
        await loop.run_in_executor(None, lambda: _run(["scan", str(seconds)], seconds + 15))
        st = _load()
        st["scan_at"] = time.time()
        _save(st)

    _scan_task = asyncio.create_task(_do())
    return {"ok": True, "scanning": True, "seconds": seconds,
            "note": "先把音箱切到配对模式（灯闪），几秒后取下 /v1/bt/devices"}


def devices() -> dict[str, Any]:
    items = _parse_scan()
    st = _load()
    scanning = _scan_task is not None and not _scan_task.done()
    return {"ok": True, "scanning": scanning,
            "last": st.get("last_addr") or "", "items": items}


# ------------------------------------------------------------------ HTTP 路由

@router.get("/status")
async def bt_status() -> dict[str, Any]:
    """蓝牙状态：当前连接 / 上次设备 / 是否已扫描过。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, status)


@router.post("/scan")
async def bt_scan(seconds: int = Query(8, ge=4, le=15)) -> dict[str, Any]:
    """开始扫描附近的蓝牙音箱（后台，4~30 秒）。"""
    return await scan(seconds)


@router.get("/devices")
async def bt_devices() -> dict[str, Any]:
    """扫描到的设备列表（含是否已配对/已连接）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, devices)


@router.post("/connect")
async def bt_connect(addr: str = Query("", description="为空则连上次的设备")) -> dict[str, Any]:
    """连接蓝牙音箱（会顺手 trust，便于下次自动连）。"""
    return await connect(addr or None)


@router.post("/disconnect")
async def bt_disconnect() -> dict[str, Any]:
    """断开蓝牙音箱（切回 DAC 时会自动调用）。"""
    return await disconnect()
