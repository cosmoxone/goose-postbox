#!/usr/bin/env python3
"""postbox MCP 扩展（M2）：goose 会话间消息传递

工具：
  postbox_send(to, body)          发消息给指定会话（按 -n 名/前缀/id 寻址）
  postbox_recv(mark_read?, all?)  收取自己的未读消息（默认标记已读）
  postbox_list()                  列出可寻址的活跃会话（busy/alive/未读数）
  postbox_dispatch(to, prompt, backend?, node?)  执行面：派发任务到目标机（N3）

身份：goose 自动为 STDIO 扩展注入 AGENT_SESSION_ID 环境变量。
传输：MCP stdio（newline-delimited JSON-RPC 2.0），零外部依赖。
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))  # 发布树布局: store 在 ../scripts
import postbox_store as store  # noqa: E402

SELF_ID = os.environ.get("AGENT_SESSION_ID", "")
SELF_NAME = os.environ.get("GOOSE_POSTBOX_NAME", "")


def _self_or_error():
    if not SELF_ID:
        raise store.PostboxError(
            "未获得 AGENT_SESSION_ID（goose 会自动注入 STDIO 扩展）；请从 goose 会话内调用本工具")
    return SELF_ID


def tool_send(args: dict) -> str:
    to = str(args.get("to", "")).strip()
    body = str(args.get("body", ""))
    if not to:
        raise store.PostboxError("缺少参数 to（目标会话名或 id）")
    if not body.strip():
        raise store.PostboxError("缺少参数 body（消息内容）")
    res = store.send_to(to, _self_or_error(), SELF_NAME, body)  # v0.5: 支持 名@peer
    return json.dumps({"ok": True, **res,
                       "hint": "对方将在下一回合收到；若其在 tmux 中，M3 将支持即时唤醒"},
                      ensure_ascii=False)


def tool_recv(args: dict) -> str:
    msgs = store.recv(_self_or_error(),
                      mark_read=bool(args.get("mark_read", True)),
                      include_read=bool(args.get("all", False)))
    return json.dumps({"count": len(msgs), "messages": msgs}, ensure_ascii=False)


def tool_list(_args: dict) -> str:
    rows = store.list_sessions(self_id=SELF_ID or None, include_peers=True)
    return json.dumps({"count": len(rows), "sessions": rows}, ensure_ascii=False)


def tool_dispatch(args: dict) -> str:
    to = str(args.get("to", "")).strip()
    prompt = str(args.get("prompt", ""))
    backend = str(args.get("backend", "fleetd"))
    node = str(args.get("node", "")).strip() or None
    timeout_s = int(args.get("timeout_s", 120) or 120)
    trigger = bool(args.get("trigger", True))
    if not to:
        raise store.PostboxError("缺少参数 to（目标 peer 名，如 l150）")
    if not prompt.strip():
        raise store.PostboxError("缺少参数 prompt（要执行的任务描述）")
    res = store.dispatch(_self_or_error(), SELF_NAME, to, prompt,
                         backend=backend, node=node, timeout_s=timeout_s, trigger=trigger)
    return json.dumps({"ok": True, **res}, ensure_ascii=False)


TOOLS = [
    {
        "name": "postbox_send",
        "description": ("向另一个正在运行的 goose 会话发送消息（跨会话通信）。to 支持会话名"
                        "（goose session -n 设置的名字精确优先）或 session_id；body 为纯文本。"
                        "发送成功后对方在下一回合经 postbox_recv 或注入获得。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "目标会话名或 session_id"},
                "body": {"type": "string", "description": "消息正文（<=64KB）"},
            },
            "required": ["to", "body"],
        },
    },
    {
        "name": "postbox_recv",
        "description": ("收取本会话的跨会话消息。默认只取未读并标记已读；all=true 含已读。"
                        "建议每个回合开始时调用一次。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mark_read": {"type": "boolean", "default": True},
                "all": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "postbox_list",
        "description": "列出当前可寻址的活跃 goose 会话（名称/忙闲/存活/未读数/是否自己）。跨机会话带 peer 字段，用 名@peer 寻址。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "postbox_dispatch",
        "description": ("把一段任务描述派发到目标机执行（执行面原子操作）。backend=fleetd："
                        "经 fleet 守护同步执行并返回结果（goose serve 节点）；backend=nf-intake："
                        "在目标机夜厂票池建票（class:batch）并触发一轮执行，返回票 id，"
                        "四态经目标机 runs 表/vk 看板可查。受急停/白名单/预算全套治理。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "目标 peer 名（如 l150）"},
                "prompt": {"type": "string", "description": "任务描述（<=8KB）"},
                "backend": {"type": "string", "enum": ["fleetd", "nf-intake"],
                            "default": "fleetd"},
                "node": {"type": "string", "description": "fleet 内节点 slug（缺省=to 同名）"},
                "timeout_s": {"type": "integer", "default": 120,
                              "description": "fleetd 同步等待秒数（30-600）"},
                "trigger": {"type": "boolean", "default": True,
                            "description": "nf-intake 是否顺带触发一轮 window 执行"},
            },
            "required": ["to", "prompt"],
        },
    },
]
HANDLERS = {"postbox_send": tool_send, "postbox_recv": tool_recv, "postbox_list": tool_list,
            "postbox_dispatch": tool_dispatch}


def reply(req_id, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply_err(req_id, code, message) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id,
                                 "error": {"code": code, "message": message}},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        method = req.get("method", "")
        req_id = req.get("id")

        if method == "initialize":
            reply(req_id, {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "postbox", "version": "0.8.0"},
            })
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue  # 通知无需应答
        elif method == "ping":
            reply(req_id, {})
        elif method == "tools/list":
            reply(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = req.get("params", {}).get("name", "")
            args = req.get("params", {}).get("arguments", {}) or {}
            if name not in HANDLERS:
                reply_err(req_id, -32601, f"unknown tool {name}")
                continue
            try:
                text = HANDLERS[name](args)
                reply(req_id, {"content": [{"type": "text", "text": text}]})
            except store.PostboxError as e:  # 业务错误：正常返回，让模型可行动
                reply(req_id, {"content": [{"type": "text", "text": f"[postbox] {e}"}],
                               "isError": True})
            except Exception as e:  # noqa: BLE001
                reply(req_id, {"content": [{"type": "text", "text": f"[postbox] 内部错误: {e}"}],
                               "isError": True})
        elif req_id is not None:
            reply_err(req_id, -32601, f"method not found: {method}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
