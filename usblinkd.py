#!/usr/bin/env python3
"""usblinkd —— 服务器侧：通过 USB 串口（板子的 CH343）和板子通信，并执行板子发来的指令。

============================================================================
为什么是"用户态串口"
============================================================================
板子插上后是一个标准 CDC-ACM 设备（1a86:55d3），但**这台服务器的内核没有
cdc_acm 模块**（也没有 gcc/内核头，编不了模块）。既然它只是标准 CDC-ACM：
SET_LINE_CODING + SET_CONTROL_LINE_STATE + bulk 读写就够了 —— 用 libusb 在用户态
实现最小 ACM，不需要内核驱动、不需要编内核。

============================================================================
协议（一行一条，UTF-8，\n 结尾）
============================================================================
    板子 → 服务器      ESP>指令 [参数...]
    服务器 → 板子      ESP<OK ...  / ESP<ERR ...  / ESP<EVT ...

指令：
    PING                      → ESP<OK PONG
    STATUS                    → ESP<OK 摘要（IP/热点/链路）
    HOTSPOT [ssid] [pass]     → 让服务器开热点（无网时给手机配网用）
    HOTSPOT-OFF               → 关热点
    WIFI <ssid> <pass>        → 让服务器连指定 WiFi

板子的串口日志（`I (123) ...`）也会被读到，只做计数/丢弃，不当指令。

============================================================================
本地控制口（测试/调试用，只监听 127.0.0.1:8888）
============================================================================
    GET  /status          → JSON：链路是否在、收发字节数、热点状态
    POST /cmd  body=指令  → 和板子发来的指令等价（比如 body 写 PING）
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import usb.core

VID, PID = 0x1A86, 0x55D3
EP_OUT, EP_IN = 0x02, 0x82
IF_COMM, IF_DATA = 0, 1
HTTP_PORT = int(os.environ.get("USBLINK_HTTP_PORT", "8888"))
DRY = os.environ.get("USBLINK_DRY", "") == "1"     # 干跑：不真的动网络，只打印
LOG = Path(os.environ.get("USBLINK_LOG", "/var/log/usblink.log"))


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:                                   # noqa: BLE001
        pass


# ----------------------------------------------------------------- 链路

class Link:
    def __init__(self) -> None:
        self.dev = None
        self.txq: queue.Queue[bytes] = queue.Queue()
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.connected = False
        self.started_at = 0.0
        self.handlers_line: list[str] = []           # 最近收到的板子指令（调试用）

    def open(self) -> None:
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise RuntimeError("没找到板子（1a86:55d3）")
        dev.set_configuration()
        # DTR/RTS 拉起来；115200 8N1
        dev.ctrl_transfer(0x21, 0x22, 0x03, 0, b"")
        dev.ctrl_transfer(0x21, 0x20, 0, 0, bytes([0x00, 0xC2, 0x01, 0x00, 0, 0, 8]))
        for itf in (IF_COMM, IF_DATA):
            try:
                if dev.is_kernel_driver_active(itf):
                    dev.detach_kernel_driver(itf)
            except Exception:                           # noqa: BLE001
                pass
            try:
                dev.claim_interface(itf)
            except Exception:                           # noqa: BLE001
                pass
        self.dev = dev
        self.connected = True
        self.started_at = time.time()
        log("USB 链路已建立（1a86:55d3）")

    def close(self) -> None:
        self.connected = False
        if self.dev is not None:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:                           # noqa: BLE001
                pass
            self.dev = None

    def send_line(self, text: str) -> None:
        """发给板子（自动加 ESP< 前缀如果还没有）。"""
        if not text.startswith(("ESP<", "ESP>")):
            text = "ESP<" + text
        self.txq.put((text.rstrip("\r\n") + "\n").encode("utf-8"))

    def pump_tx(self) -> bool:
        try:
            data = self.txq.get_nowait()
        except queue.Empty:
            return False
        if self.dev is None:
            return False
        try:
            self.dev.write(EP_OUT, data, timeout=1000)
            self.tx_bytes += len(data)
            return True
        except Exception as e:                          # noqa: BLE001
            log("写串口失败: %s" % e)
            self.connected = False
            return False


# ----------------------------------------------------------------- 指令执行

def _run(cmd: list[str], timeout: int = 90) -> tuple[int, str]:
    if DRY:
        log("[DRY] 将执行: %s" % " ".join(cmd))
        return 0, "(dry-run)"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()
        log("执行 %s → rc=%d %s" % (" ".join(cmd), p.returncode, out[:300]))
        return p.returncode, out
    except Exception as e:                              # noqa: BLE001
        log("执行失败 %s: %s" % (" ".join(cmd), e))
        return -1, str(e)


def wifi_ip() -> dict:
    out = _run(["ip", "-br", "addr", "show", "wlan0"], 10)[1]
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)/", out)
    return {"wlan0": m.group(1) if m else "", "raw": out}


def hotspot_state() -> dict:
    out = _run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE,STATE", "connection", "show"], 15)[1]
    for line in out.splitlines():
        f = line.split(":")
        if len(f) >= 4 and ("hotspot" in f[0].lower() or f[1] == "wifi"):
            if "hotspot" in f[0].lower():
                return {"name": f[0], "state": f[3] if len(f) > 3 else line}
    return {"name": "", "state": "off"}


def do_4g() -> str:
    """让服务器切到 4G 数据（板子点"使用4G网"时用）。
    没有信号时直接回明确原因，别让用户在那干等。"""
    out = _run(["mmcli", "-m", "any"], 25)[1]
    if "Modem/" not in out and "modem" not in out.lower():
        return "ERR 没检测到 4G modem（SIM 插好了吗）"
    m = re.search(r"signal quality: (\d+)%", out)
    sig = int(m.group(1)) if m else -1
    if sig == 0:
        return "ERR 4G 没信号（0%），检查天线/位置"
    rc, o = _run(["nmcli", "connection", "up", "lte"], 90)
    if rc != 0:
        return "ERR 4G 连不上: %s" % o[:120]
    ip = _run(["bash", "-c", "ip -4 -br addr show wwan0 | awk '{print $3}'"], 10)[1]
    return "OK 4G 已连上（信号 %d%%）ip=%s" % (sig, ip)


def do_hotspot(args: list[str]) -> str:
    ssid = args[0] if args else "ESP-Music-Server"
    password = args[1] if len(args) > 1 else "esp12345678"
    if len(password) < 8:
        return "ERR 热点密码至少 8 位"
    # ⚠️ 这一步会把服务器的 wlan0 从"连路由器"切成 AP —— 无网时正是我们要的，
    #    但会有线/无线暂时断一下（用户要的就是这个场景，所以只在板子明确要求时才做）。
    rc, out = _run(["nmcli", "device", "wifi", "hotspot",
                    "ifname", "wlan0", "con-name", "esp-music-hotspot",
                    "ssid", ssid, "password", password], 120)
    if rc != 0:
        return "ERR 开热点失败: %s" % out[:120]
    return "OK 热点已开 SSID=%s PASS=%s，手机连它后访问 http://192.168.4.1:3210/" % (ssid, password)


def do_hotspot_off() -> str:
    rc, out = _run(["nmcli", "connection", "down", "esp-music-hotspot"], 60)
    _run(["nmcli", "connection", "delete", "esp-music-hotspot"], 60)
    return "OK 热点已关" if rc == 0 else "ERR 关热点失败: %s" % out[:120]


def do_wifi(args: list[str]) -> str:
    if len(args) < 2:
        return "ERR 用法: WIFI <ssid> <password>"
    ssid, password = args[0], args[1]
    rc, out = _run(["nmcli", "device", "wifi", "connect", ssid,
                    "password", password, "ifname", "wlan0"], 120)
    if rc != 0:
        return "ERR 连不上 %s: %s" % (ssid, out[:120])
    ip = wifi_ip()["wlan0"]
    return "OK ip=%s 已连上 %s" % (ip, ssid)


def handle_command(text: str) -> str:
    """执行一条板子发来的指令，返回要回给板子的那一行（不含 ESP< 前缀）。"""
    parts = text.strip().split()
    if not parts:
        return "ERR 空指令"
    cmd = parts[0].upper()
    args = parts[1:]
    log("收到指令: %s" % text.strip())
    if cmd == "PING":
        return "OK PONG"
    if cmd == "STATUS":
        ip = wifi_ip()
        hs = hotspot_state()
        return "OK ip=%s dac=? hotspot=%s rx=%d tx=%d" % (
            ip["wlan0"] or "none", hs.get("state", "?"), LINK.rx_bytes, LINK.tx_bytes)
    if cmd == "4G":
        return do_4g()
    if cmd == "HOTSPOT":
        return do_hotspot(args)
    if cmd == "HOTSPOT-OFF":
        return do_hotspot_off()
    if cmd == "WIFI":
        return do_wifi(args)
    return "ERR 不认识的指令: %s" % cmd


# ----------------------------------------------------------------- 读循环

LINK = Link()
_last_open_try = 0.0


def link_loop() -> None:
    """连上板子 → 一直读；断了自己重连（板子重插/重启都能恢复）。"""
    global _last_open_try
    buf = b""
    while True:
        if not LINK.connected:
            now = time.time()
            if now - _last_open_try < 2:
                time.sleep(0.3)
                continue
            _last_open_try = now
            try:
                LINK.open()
                buf = b""
            except Exception as e:                      # noqa: BLE001
                log("等板子接入…（%s）" % e)
                time.sleep(1.5)
                continue
        # 发队列里的东西
        while LINK.pump_tx():
            pass
        try:
            data = LINK.dev.read(EP_IN, 128, timeout=300)
        except usb.core.USBTimeoutError:
            continue
        except Exception as e:                          # noqa: BLE001
            log("读串口失败: %s" % e)
            LINK.close()
            continue
        if not data:
            continue
        LINK.rx_bytes += len(data)
        buf += bytes(data)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip("\r")
            if not text:
                continue
            if text.startswith("ESP>"):
                resp = handle_command(text[4:])
                LINK.send_line(resp)
                LINK.handlers_line.append(text)
                del LINK.handlers_line[:-20]
            # 其它都是板子的串口日志，忽略（要排查就 tail /var/log/usblink.log）


# ----------------------------------------------------------------- 本地控制口

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                          # noqa: A003
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/status"):
            self._json({
                "link": LINK.connected, "rx_bytes": LINK.rx_bytes,
                "tx_bytes": LINK.tx_bytes, "uptime_s": int(time.time() - LINK.started_at)
                if LINK.started_at else 0,
                "wifi": wifi_ip(), "hotspot": hotspot_state(),
                "dry_run": DRY,
                "last_cmds": LINK.handlers_line[-5:],
            })
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.path.startswith("/cmd"):
            self._json({"error": "not found"}, 404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        text = self.rfile.read(n).decode("utf-8", "replace")
        # 同时发给板子 + 本地执行（本地执行便于在服务器上测试）
        LINK.send_line(text)
        result = handle_command(text)
        self._json({"ok": True, "sent_to_board": True, "result": result})
        LINK.send_line(result)


def main() -> None:
    log("usblinkd 启动（dry_run=%s http=127.0.0.1:%d）" % (DRY, HTTP_PORT))
    threading.Thread(target=link_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    log("本地控制口 http://127.0.0.1:%d/status" % HTTP_PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
