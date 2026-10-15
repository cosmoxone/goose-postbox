#!/usr/bin/env python3
"""postbox hook 脚本测试：以子进程管道真实 payload，验证注册/心跳/busy/Stop注入/注销。

默认测试已安装插件（~/.agents/plugins/postbox/scripts/postbox_hook.py）——
即验证真实部署物；可用 POSTBOX_HOOK 环境变量指向其他副本。
前置：hook 脚本需能 import postbox_store（install.sh 已将 store 复制进插件目录）。
运行：python3 test_hook.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))  # 发布树布局: tests/../scripts
import postbox_store as st  # noqa: E402

HOOK = os.environ.get(
    "POSTBOX_HOOK",
    os.path.expanduser("~/.agents/plugins/postbox/scripts/postbox_hook.py"))
SID = "test-hook-x"
FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def fire(event: str, sid: str = SID) -> tuple[int, str]:
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(
        {"event": event, "session_id": sid}), capture_output=True, text=True, timeout=10)
    return p.returncode, p.stdout.strip()


def reg_path(sid: str = SID) -> str:
    return os.path.join(st.SESSIONS_DIR, f"{sid}.json")


def main() -> int:
    # 清场
    for f in (reg_path(), st._allow_path(SID)):
        os.path.exists(f) and os.remove(f)
    shutil_like = os.path.join(st.INBOX_DIR, SID)
    import shutil
    shutil.rmtree(shutil_like, ignore_errors=True)

    # 1) SessionStart -> 注册
    rc, out = fire("SessionStart")
    rec = json.load(open(reg_path()))
    check("注册: 文件生成且字段齐全",
          rc == 0 and out == "" and rec["id"] == SID and rec["busy"] is False
          and rec.get("goose_pid", 0) > 0 and "started" in rec)

    # 2) UserPromptSubmit -> busy=true 心跳
    fire("UserPromptSubmit")
    rec = json.load(open(reg_path()))
    check("心跳: busy=true", rec["busy"] is True and rec["events"] == 2)

    # 3) 预置未读 -> Stop 应输出 block decision 且标记已读
    st.send("test-hook-y", "y", SID, "hook注入测试正文")
    rc, out = fire("Stop")
    try:
        dec = json.loads(out)
    except ValueError:
        dec = {}
    check("注入: Stop 输出唯一 decision JSON",
          dec.get("decision") == "block" and "hook注入测试正文" in dec.get("reason", ""))
    rec = json.load(open(reg_path()))
    check("注入后: busy=false 且未读清零", rec["busy"] is False and st.unread_count(SID) == 0)

    # 4) 无未读 -> Stop 静默
    rc, out = fire("Stop")
    check("无未读: Stop 静默放行", rc == 0 and out == "")

    # 5) 非法 session_id -> 不写文件不崩溃
    rc, out = fire("SessionStart", "../../evil")
    check("防护: 非法 id 拒绝且 fail-open", rc == 0 and not os.path.exists(
        os.path.join(st.SESSIONS_DIR, "..", "evil.json")))

    # 6) SessionEnd -> 注销
    fire("SessionEnd")
    check("注销: 注册文件删除", not os.path.exists(reg_path()))

    # 清场
    shutil.rmtree(shutil_like, ignore_errors=True)
    for f in (st._limit_path("test-hook-y"),
              os.path.join(st.LIMITS_DIR, f"pair.{st._pair_key('test-hook-y', SID)}.json")):
        os.path.exists(f) and os.remove(f)

    print("\n" + ("🎉 ALL PASS" if not FAILS else f"💥 FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
