#!/usr/bin/env python3
"""postbox hook (M1+review): 会话注册 / 注销 / 心跳 / busy 标记

stdin  : goose hook payload JSON，至少含 {"event": ..., "session_id": ...}
注册表 : ~/.local/share/goose/postbox/sessions/<session_id>.json
审计   : ~/.local/share/goose/postbox/hooks.log （>1MB 时保留尾部 100KB）

事件语义：
  SessionStart    -> 写注册表（id/name/tmux_pane/goose_pid/started/busy=false）+ 清扫 stale tmp
  UserPromptSubmit-> busy=true  （turn 开始，空闲检测用）
  Stop            -> busy=false （turn 结束；同时是 last_seen 心跳）
  SessionEnd      -> 删除注册文件（注销）

Review 修复（2026-10-12）：
  R1 路径注入：session_id 白名单 [A-Za-z0-9_.-]，非法即拒（stderr 记录）
  R2 僵尸注册：记录 goose_pid（hook 父进程），供 M2 kill(pid,0) 判活
  R3 原子写残留：SessionStart 清扫 sessions/*.tmp.*
  R4 日志轮转：hooks.log 超 1MB 保留尾部 100KB

设计要点（M0 教训）：
  - tmux_pane 取 $TMUX_PANE（稳定 pane_id，如 %3），绝不记 window.index
  - 原子写（tmp + rename），避免扩展/hook 并发读到半截 JSON
  - 命名不在 hook 侧解析：M2 扩展读侧 join `goose session list`
    （-n 名字 user_set_name=true 优先，$GOOSE_POSTBOX_NAME 仅作显式覆盖备选）
"""
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # store 库随插件分发（install.sh 复制）；缺失则退化为纯注册 hook
    import postbox_store
except ImportError:
    postbox_store = None

BASE = os.path.expanduser("~/.local/share/goose/postbox")
SESSIONS_DIR = os.path.join(BASE, "sessions")
LOG = os.path.join(BASE, "hooks.log")
CURRENT_SID = ""  # 当前 hook 所属会话（sweep_dead_sessions 保护用）
SID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
LOG_MAX = 1024 * 1024
LOG_KEEP = 100 * 1024


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def log_rotate() -> None:
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            with open(LOG, "rb") as f:
                f.seek(max(0, os.path.getsize(LOG) - LOG_KEEP))
                tail = f.read()
            with open(LOG + ".new", "wb") as f:
                f.write(tail)
            os.replace(LOG + ".new", LOG)
    except OSError:
        pass  # 轮转失败不影响主流程


def atomic_write_json(path: str, obj: dict) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sweep_stale_tmp() -> None:
    for name in os.listdir(SESSIONS_DIR):
        if ".tmp." in name:
            try:
                os.remove(os.path.join(SESSIONS_DIR, name))
            except OSError:
                pass


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, OSError, TypeError):
        return False


def find_goose_pid() -> int:
    """向上遍历进程树，返回第一个 comm 为 goose 的祖先 pid；找不到返回 0。

    不能直接用 os.getppid()：hook 命令可能经瞬时 shell 包装执行，该 pid 在
    hook 结束后即消失，会被 sweep_dead_sessions 误判为僵尸（2026-10-12 实证）。
    """
    p = os.getppid()
    for _ in range(10):
        if p <= 1:
            return 0
        try:
            with open(f"/proc/{p}/comm", encoding="utf-8") as f:
                comm = f.read().strip()
        except OSError:
            return 0
        if "goose" in comm:
            return p
        try:
            with open(f"/proc/{p}/stat", encoding="utf-8") as f:
                p = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return 0
    return 0


def sweep_dead_sessions() -> None:
    """清扫僵尸注册：goose_pid 已死且非本会话的注册文件（SessionEnd 丢失时兜底）。"""
    for name in os.listdir(SESSIONS_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
            pid = rec.get("goose_pid")
            if pid and rec.get("id") != CURRENT_SID and not _pid_alive(pid):
                os.remove(path)
        except (OSError, ValueError, TypeError):
            continue


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # 坏 payload 不许影响会话（fail-open 原则）
        sys.stderr.write(f"postbox: bad payload: {exc}\n")
        return 0

    event = payload.get("event", "?")
    sid = str(payload.get("session_id") or "")
    global CURRENT_SID
    CURRENT_SID = sid

    # 急停：postbox-ctl pause 后完全静默直通（不写任何文件，零副作用）
    if os.path.exists(os.path.join(BASE, "DISABLED")):
        return 0

    # R1: session_id 白名单，防路径穿越
    if not SID_RE.match(sid):
        sys.stderr.write(f"postbox: illegal session_id {sid!r}, ignored\n")
        return 0

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    log_rotate()
    with open(LOG, "a", encoding="utf-8") as log:
        log.write(f"{now()} {event} {sid}\n")

    path = os.path.join(SESSIONS_DIR, f"{sid}.json")

    if event == "SessionStart":
        sweep_stale_tmp()
        sweep_dead_sessions()  # 兜底：SessionEnd 丢失的僵尸注册
        atomic_write_json(path, {
            "id": sid,
            "name": os.environ.get("GOOSE_POSTBOX_NAME", ""),
            "tmux_pane": os.environ.get("TMUX_PANE", ""),
            "goose_pid": find_goose_pid(),  # R2: 进程树中的 goose 祖先（非瞬时包装进程）
            "started": now(),
            "last_seen": now(),
            "busy": False,
            "events": 1,
        })
    elif event == "SessionEnd":
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    else:  # 心跳事件：UserPromptSubmit / Stop
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                rec = {"id": sid}
            rec["last_seen"] = now()
            rec["events"] = rec.get("events", 0) + 1
            # backlog: 会话活动即向 relay 报心跳（节点常在线感知；无 relay 配置时零开销）
            if postbox_store is not None:
                try:
                    postbox_store.relay_heartbeat(busy=(event == "UserPromptSubmit"))
                except Exception:  # noqa: BLE001
                    pass
            # M5: 名字一次性 join 进注册表（跨机远端无本机 session list，注册表需自包含）
            if not rec.get("joined_name") and postbox_store is not None:
                try:
                    nm = postbox_store.display_name(sid)
                    if nm and nm != sid:
                        rec["joined_name"] = nm
                except Exception:  # noqa: BLE001
                    pass
            if event == "UserPromptSubmit":
                rec["busy"] = True
            elif event == "Stop":
                rec["busy"] = False
            atomic_write_json(path, rec)

        # M3: 回合边界注入未读跨会话消息（stdout 仅允许 decision JSON）
        if event == "Stop" and postbox_store is not None:
            try:
                reason = postbox_store.inject_payload(sid)
                if reason:
                    sys.stdout.write(json.dumps(
                        {"decision": "block", "reason": reason}, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
            except Exception as exc:  # 注入失败不影响回合结束
                sys.stderr.write(f"postbox: inject failed: {exc}\n")
        # 注册文件不存在（插件中途安装/旧会话）→ 静默跳过
    return 0


if __name__ == "__main__":
    sys.exit(main())
