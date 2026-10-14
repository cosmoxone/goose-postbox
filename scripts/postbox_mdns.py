#!/usr/bin/env python3
"""postbox mDNS 自动发现（M5c，v0.6）

零配置组网：在局域网广播/发现 _postbox._tcp 服务；发现的 peers 写入
BASE/mdns/peers.json，store 侧与静态 config.json peers 合并（静态优先）。
传输仍为 ssh（TXT 里带 user@ip），SRV port=22 仅为语义占位。

用法：
  postbox_mdns.py serve [--name 别名]   # 前台运行（ctl mdns start 可托管为后台）
  postbox_mdns.py list                  # 打印当前已发现 peers
依赖：zeroconf（纯 pip 可装；无则本脚本报错退出，不影响 postbox 其他功能）
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time

BASE = os.path.expanduser("~/.local/share/goose/postbox")
MDNS_DIR = os.path.join(BASE, "mdns")
PEERS_FILE = os.path.join(MDNS_DIR, "peers.json")
STYPE = "_postbox._tcp.local."


def _atomic_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # 不实际发包，仅选路
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def default_name() -> str:
    host = socket.gethostname().split(".")[0][:24] or "postbox"
    ip = lan_ip().rsplit(".", 2)[-2:]  # 撞名机器用 IP 后缀消歧（OEM 主机名高发）
    return f"{host}-{'-'.join(ip)}"


def cmd_list() -> int:
    try:
        peers = json.load(open(PEERS_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        peers = {}
    if not peers:
        print("(尚无发现；对端需运行 postbox_mdns.py serve)")
        return 0
    print(f"{'别名':28} ssh 目标              首见")
    for k, v in sorted(peers.items()):
        print(f"{k:28} {v.get('ssh', '?'):22} {v.get('seen', '')[:19]}")
    return 0


def cmd_serve(name: str | None) -> int:
    try:
        from zeroconf import ServiceInfo, Zeroconf, ServiceStateChange
        from zeroconf import ServiceBrowser
    except ImportError:
        print("缺依赖 zeroconf：pip3 install --user --break-system-packages zeroconf", file=sys.stderr)
        return 2

    alias = name or default_name()
    ip = lan_ip()
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "me"
    me = {"alias": alias, "ssh": f"{user}@{ip}", "origin": socket.gethostname()}

    def load() -> dict:
        try:
            return json.load(open(PEERS_FILE, encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    class Listener:
        def remove_service(self, zc, t, name):  # noqa: N802
            short = name.split(".")[0]
            peers = load()
            if short in peers and peers[short].get("origin") != me["origin"]:
                peers.pop(short, None)
                _atomic_json(PEERS_FILE, peers)

        def add_service(self, zc, t, name):  # noqa: N802
            info = zc.get_service_info(t, name, timeout=3000)
            if not info:
                return
            props = {k.decode(): v.decode() for k, v in (info.properties or {}).items()}
            # 自过滤：以 服务名/ssh目标 为准。hostname 不可靠（OEM 撞名实证 2026-10-13）
            if name.split(".")[0] == alias or props.get("ssh") == me["ssh"]:
                return
            peers = load()
            peers[name.split(".")[0]] = {
                "ssh": props.get("ssh", ""),
                "origin": props.get("origin", "?"),
                "ip": socket.inet_ntoa(info.addresses[0]) if info.addresses else "",
                "seen": time.strftime("%F %T")}
            _atomic_json(PEERS_FILE, peers)

        def update_service(self, zc, t, name):  # noqa: N802
            self.add_service(zc, t, name)

    iface = os.environ.get("POSTBOX_MDNS_IFACE") or ip  # 多网卡同网段时锁定广播口（.223/.23 双口实证）
    try:
        zc = Zeroconf(interfaces=[iface])
    except TypeError:
        zc = Zeroconf()
    info = ServiceInfo(
        STYPE, f"{alias}.{STYPE}",
        addresses=[socket.inet_aton(ip)], port=22,
        properties={"v": "1", "ssh": me["ssh"], "origin": me["origin"]})
    zc.register_service(info)
    ServiceBrowser(zc, STYPE, Listener())
    _atomic_json(os.path.join(MDNS_DIR, "self.json"), me)
    print(f"[mdns] 广播 alias={alias} ssh={me['ssh']} iface={iface}（Ctrl-C 退出）", flush=True)

    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(v=True))
    signal.signal(signal.SIGINT, lambda *a: stop.update(v=True))
    try:
        while not stop["v"]:
            time.sleep(1)
    finally:
        zc.unregister_service(info)
        zc.close()
        print("[mdns] 已下线", flush=True)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "list":
        return cmd_list()
    if args and args[0] == "serve":
        name = args[args.index("--name") + 1] if "--name" in args else None
        return cmd_serve(name)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
