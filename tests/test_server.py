#!/usr/bin/env python3
"""postbox MCP 服务器协议测试：真实子进程 + stdin/stdout JSON-RPC 往返。

覆盖：initialize 握手 / tools/list / tools/call（send/recv/list）/
      业务错误 isError / 缺 AGENT_SESSION_ID 身份错误。
运行：python3 test_server.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))  # 发布树布局: tests/../scripts
import postbox_store as st  # noqa: E402

_SERVER_CANDIDATES = [
    os.environ.get("POSTBOX_SERVER", ""),
    os.path.join(_HERE, "postbox_server.py"),            # 开发布局
    os.path.join(_HERE, "..", "ext", "postbox_server.py")  # 发布树布局
]
SERVER = next(p for p in _SERVER_CANDIDATES if p and os.path.exists(p))
SELF = "test-srv-x"
PEER = "test-srv-peer"
FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def talk(lines: list[str], env_extra: dict | None = None) -> dict[int, dict]:
    env = dict(os.environ)
    env["AGENT_SESSION_ID"] = SELF
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, SERVER], input="\n".join(lines) + "\n",
                       capture_output=True, text=True, timeout=30, env=env)
    out = {}
    for line in p.stdout.splitlines():
        if line.strip():
            r = json.loads(line)
            out[r.get("id")] = r
    return out


def call(i: int, name: str, args: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                       "params": {"name": name, "arguments": args}})


def main() -> int:
    _cfg = os.path.join(st.BASE, "config.json")
    _saved = open(_cfg).read() if os.path.exists(_cfg) else None
    if _saved is not None:  # 隔离生产 relay 配置：未命中用例不得走回退
        json.dump({}, open(_cfg, "w"))
    # 清场 + 伪造 peer 注册（供按 id 寻址）
    for sid in (SELF, PEER):
        shutil.rmtree(os.path.join(st.INBOX_DIR, sid), ignore_errors=True)
        os.path.exists(st._limit_path(sid)) and os.remove(st._limit_path(sid))
        os.path.exists(os.path.join(st.SESSIONS_DIR, f"{sid}.json")) and \
            os.remove(os.path.join(st.SESSIONS_DIR, f"{sid}.json"))
    st._atomic_write(os.path.join(st.SESSIONS_DIR, f"{PEER}.json"), {
        "id": PEER, "name": "", "tmux_pane": "", "goose_pid": os.getpid(),
        "started": st.now_iso(), "last_seen": st.now_iso(), "busy": False, "events": 1})

    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        call(3, "postbox_list", {}),
        call(4, "postbox_send", {"to": PEER, "body": "协议测试消息"}),
        call(5, "postbox_send", {"to": "no-such-session", "body": "x"}),
        call(6, "postbox_recv", {}),
    ]
    res = talk(lines)

    check("握手: serverInfo 0.8.0",
          res[1]["result"]["serverInfo"] == {"name": "postbox", "version": "0.8.0"})
    names = [t["name"] for t in res[2]["result"]["tools"]]
    check("工具清单: 四个工具", names == ["postbox_send", "postbox_recv", "postbox_list",
                                          "postbox_dispatch"])
    lst = json.loads(res[3]["result"]["content"][0]["text"])
    check("list: 含 peer 且标注非己", any(s["id"] == PEER and not s["is_self"]
                                          for s in lst["sessions"]))
    snd = json.loads(res[4]["result"]["content"][0]["text"])
    check("send: 成功且 wake=skipped(no-tmux)", snd.get("ok") is True
          and snd.get("wake", "").startswith("skipped"), json.dumps(snd)[:80])
    err = res[5]["result"]
    check("send: 未命中目标返回 isError 且文案可行动",
          err.get("isError") is True and "未找到目标会话" in err["content"][0]["text"])
    check("send: 消息真实落盘", st.unread_count(PEER) == 1)

    # 缺身份：无 AGENT_SESSION_ID 的进程 send 应得到明确错误
    res2 = talk([call(1, "postbox_send", {"to": PEER, "body": "y"})],
                env_extra={"AGENT_SESSION_ID": ""})
    r = res2.get(1, {}).get("result", {})
    check("身份: 缺 AGENT_SESSION_ID 报可读错误",
          r.get("isError") is True and "AGENT_SESSION_ID" in r.get("content", [{}])[0].get("text", ""))

    # N3 dispatch 工具面接线：用不存在节点快失败（验证接线与可读错误，不依赖真机链路）
    res3 = talk([call(1, "postbox_dispatch", {"to": "ghost-node-e2e", "prompt": "server接线快失败用例v2",
                                              "timeout_s": 5})])
    r3 = res3.get(1, {}).get("result", {})
    txt3 = r3.get("content", [{}])[0].get("text", "")
    check("dispatch: 工具接线（快失败可行动错误）",
          r3.get("isError") is True and ("fleetd" in txt3 or "ghost-node-e2e" in txt3),
          str(r3)[:100])

    # 清场
    for sid in (SELF, PEER):
        shutil.rmtree(os.path.join(st.INBOX_DIR, sid), ignore_errors=True)
        os.path.exists(st._limit_path(sid)) and os.remove(st._limit_path(sid))
        os.path.exists(os.path.join(st.SESSIONS_DIR, f"{sid}.json")) and \
            os.remove(os.path.join(st.SESSIONS_DIR, f"{sid}.json"))
    pf = os.path.join(st.LIMITS_DIR, f"pair.{st._pair_key(SELF, PEER)}.json")
    os.path.exists(pf) and os.remove(pf)

    if _saved is not None:
        open(_cfg, "w").write(_saved)
    print("\n" + ("🎉 ALL PASS" if not FAILS else f"💥 FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
