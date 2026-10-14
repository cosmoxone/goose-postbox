#!/usr/bin/env python3
"""postbox 存储层（M2 + M3）

目录布局（与 M1 hook 共用 BASE）：
  ~/.local/share/goose/postbox/
  ├── sessions/<sid>.json           # M1 hook 维护的注册表
  ├── inbox/<sid>/inbox.jsonl       # 每会话收件箱
  │                 └── lock        # 收件箱专用锁（send/recv 统一串行化，防并发丢消息）
  └── limits/<sid>.limit.json       # 每发送方限流/去重状态

职责：寻址(resolve)、发送(send)、收取(recv)、会话清单(list)、治理(限流/去重/上限)、
     空闲唤醒(wake，M3)。

Review 修复（2026-10-12 v2）：
  F1 收件箱锁：send 追加与 recv 重写统一走 <sid>/lock 文件锁（rename 换 inode 导致
     原 flock(inbox.jsonl) 不互锁；M3 hook 与扩展并发访问会丢消息）
  F2 resolve_target 守卫简化：原复合条件因优先级实际只拦空串（死代码），现只查空
  F3 from_name 回填：send 时 join session list 自动补发送方显示名
  F4 list_sessions 排序修正：自己优先，其余按 last_seen 新→旧

M3 唤醒策略（KISS，确定性优先）：
  - 目标 busy（turn 进行中）或不在线 → 不唤醒，Stop hook 会在回合边界注入
  - 目标空闲且有 tmux_pane → send-keys 唤醒：
      · 短消息(扁平化后≤400字)：直接注入正文并标记已读（一步到位，无需扩展）
      · 长消息：只发通知短语；正文由接收方 postbox_recv 或 Stop hook 注入
  - 无 tmux_pane（Desktop 等）→ 不唤醒，仅 Stop hook 通道
  设计决策：不做 capture 指纹校验（M0 教训针对 window.index 漂移；改用稳定
  pane_id 后该问题类消失，校验属过度设计）。
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager

BASE = os.path.expanduser("~/.local/share/goose/postbox")
SESSIONS_DIR = os.path.join(BASE, "sessions")
INBOX_DIR = os.path.join(BASE, "inbox")
LIMITS_DIR = os.path.join(BASE, "limits")
DISABLED_FLAG = os.path.join(BASE, "DISABLED")   # 急停标志（postbox-ctl pause/resume）
RUNTIME_LOG = os.path.join(BASE, "runtime.log")  # 运维事件日志（可追溯）

SID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# 治理参数（对标 Claude Code 防滥用，README 5.3/5.6）
MAX_BODY = 64 * 1024          # 单条消息上限
RATE_PER_MIN = 10             # 每发送方每分钟
DEDUP_WINDOW = 300            # 相同内容去重窗口（秒）
DEDUP_KEEP = 8                # 记住最近 N 条内容哈希
MAX_UNREAD = 50               # 收件箱未读上限（跨发送方总量防线）
WAKE_INLINE_MAX = 400         # 唤醒时直接注入正文的上限（扁平化后字符数）
INJECT_MAX = 5                # Stop hook 单次注入条数上限
PAIR_BUDGET_DEFAULT = 30      # 两会话间每小时消息预算（双向合计，防回环熔断；config.json 可覆盖）
PAIR_WINDOW = 3600            # 预算窗口（秒）


def load_config() -> dict:
    """BASE/config.json 运行时配置（每次 send 读取，便于实验中动态调整）。"""
    try:
        with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}

_sl_cache: dict = {"t": 0.0, "v": {}}


class PostboxError(Exception):
    """面向模型的错误（会作为工具结果返回，措辞要可行动）"""


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _atomic_write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ValueError, OSError, TypeError):
        return False


def is_disabled() -> bool:
    """急停状态（postbox-ctl pause 设置 BASE/DISABLED）。"""
    return os.path.exists(DISABLED_FLAG)


def _rotate_file(path: str, max_bytes: int = 1024 * 1024, keep: int = 100 * 1024) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - keep))
                tail = f.read()
            with open(path + ".new", "wb") as f:
                f.write(tail)
            os.replace(path + ".new", path)
    except OSError:
        pass


def runtime_log(event: str, **kw) -> None:
    """运维事件留痕（best-effort，失败不影响业务）：ts event k=v k=v ..."""
    try:
        os.makedirs(BASE, exist_ok=True)
        _rotate_file(RUNTIME_LOG)
        parts = " ".join(f"{k}={v}" for k, v in kw.items())
        with open(RUNTIME_LOG, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {event} {parts}".rstrip() + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 会话清单

def session_list_snapshot(max_age: float = 5.0) -> dict[str, dict]:
    """goose session list --format json -> {id: {name, user_set_name, working_dir, updated_at}}
    候选二进制探测：PATH 中的 goose 可能是 Desktop 版（无 X 即崩，M5a-1 实证），
    自动回退 ~/.local/bin/goose。"""
    if time.time() - _sl_cache["t"] < max_age and _sl_cache["v"]:
        return _sl_cache["v"]
    rows = []
    for binpath in ("goose", os.path.expanduser("~/.local/bin/goose")):
        try:
            out = subprocess.run(
                [binpath, "session", "list", "--format", "json"],
                capture_output=True, text=True, timeout=15, check=True,
            ).stdout
            rows = json.loads(out) if out.strip() else []
            break
        except Exception:
            continue
    v = {r["id"]: {
        "name": r.get("name", ""),
        "user_set_name": bool(r.get("user_set_name")),
        "working_dir": r.get("working_dir", ""),
        "updated_at": r.get("updated_at", ""),
    } for r in rows if r.get("id")}
    _sl_cache.update(t=time.time(), v=v)
    return v


def registry_entries() -> dict[str, dict]:
    ent = {}
    if not os.path.isdir(SESSIONS_DIR):
        return ent
    for fn in os.listdir(SESSIONS_DIR):
        if not fn.endswith(".json") or ".tmp." in fn:
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fn), encoding="utf-8") as f:
                rec = json.load(f)
            if SID_RE.match(str(rec.get("id", ""))):
                ent[rec["id"]] = rec
        except (OSError, ValueError):
            continue
    return ent


def display_name(sid: str, snap: dict[str, dict] | None = None) -> str:
    """会话显示名：-n 名 / 自动标题 / GOOSE_POSTBOX_NAME / id。"""
    snap = snap if snap is not None else session_list_snapshot()
    reg = registry_entries()
    return (snap.get(sid, {}).get("name")
            or reg.get(sid, {}).get("name", "")
            or sid)


def list_peer_sessions(timeout: int = 6) -> list[dict]:
    """并发拉取各 enabled peer 的注册表（code-carried registry action），失败静默跳过。"""
    import concurrent.futures
    peers = {k: v for k, v in load_peers_union().items()
             if v.get("host") and v.get("enabled", True)}
    out: list[dict] = []

    def fetch(alias: str, host: str) -> list[dict]:
        r = _ssh_json(host, {"action": "registry"}, timeout=timeout)
        if not r.get("ok"):
            return []
        return [{**s, "id": s["id"], "peer": alias, "remote": True} for s in r.get("sessions", [])]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for fut in [ex.submit(fetch, a, p["host"]) for a, p in peers.items()]:
            out.extend(fut.result())
    return out


def list_sessions(self_id: str | None = None, include_peers: bool = False) -> list[dict]:
    """注册表 join session list：liveness + 未读数。僵尸（pid 死）标记 alive=false。
    include_peers=True 时合并各 peer 的远端会话（标 peer=别名）。"""
    sl = session_list_snapshot()
    out = []
    for sid, rec in registry_entries().items():
        pid = rec.get("goose_pid")
        meta = sl.get(sid, {})
        out.append({
            "id": sid,
            "name": meta.get("name") or rec.get("name", ""),
            "user_set_name": meta.get("user_set_name", False),
            "working_dir": meta.get("working_dir") or "",
            "busy": bool(rec.get("busy")),
            "last_seen": rec.get("last_seen", ""),
            "alive": _pid_alive(pid) if pid else None,
            "tmux_pane": rec.get("tmux_pane", ""),
            "unread": unread_count(sid),
            "is_self": sid == self_id,
        })
    out.sort(key=lambda r: r["last_seen"], reverse=True)   # 时间降序
    out.sort(key=lambda r: not r["is_self"])               # 稳定排序：自己置顶
    if include_peers:
        out.extend(list_peer_sessions())  # 远端条目：含 peer 字段，寻址用 名@peer
        out.extend(relay_sessions())      # 中继目录节点（peer=relay）
    return out


# ---------------------------------------------------------------- 寻址

def resolve_target(to: str, snap: dict[str, dict] | None = None) -> str:
    """按名/前缀/id 解析目标会话 -> session_id。失败抛 PostboxError（含候选列表）。"""
    if not to or not to.strip():  # F2: 只拦空（名字可含任意字符，靠精确/唯一前缀匹配）
        raise PostboxError("目标名为空")
    reg = registry_entries()
    snap = snap if snap is not None else session_list_snapshot()

    # 1) 精确 session_id
    if to in reg:
        return to

    # 2) -n 设置的名字（user_set_name=true）精确匹配
    hits = [sid for sid in reg
            if sid in snap and snap[sid]["user_set_name"] and snap[sid]["name"] == to]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise PostboxError(f"名字 {to!r} 命中多个会话: {hits}，请用 session_id")

    # 3) 任意注册名/标题（含自动标题）唯一前缀匹配
    named = {sid: (reg[sid].get("name") or snap.get(sid, {}).get("name", ""))
             for sid in reg}
    hits = [sid for sid, nm in named.items() if nm and nm.startswith(to)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        cand = ", ".join(f"{named[s]}({s})" for s in hits[:5])
        raise PostboxError(f"{to!r} 有歧义，候选: {cand}")

    alive = [f"{nm}({sid})" for sid, nm in named.items() if nm]
    raise PostboxError(f"未找到目标会话 {to!r}。当前可寻址: {', '.join(alive) or '(无)'}")


# ---------------------------------------------------------------- 收件箱（F1: 统一文件锁）

def _inbox_path(sid: str) -> str:
    return os.path.join(INBOX_DIR, sid, "inbox.jsonl")


@contextmanager
def _inbox_lock(sid: str):
    os.makedirs(os.path.dirname(_inbox_path(sid)), exist_ok=True)
    lock_path = os.path.join(INBOX_DIR, sid, "lock")
    with open(lock_path, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _read_inbox(sid: str) -> list[dict]:
    try:
        with open(_inbox_path(sid), encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []


def _append_inbox(sid: str, msg: dict) -> None:
    os.makedirs(os.path.dirname(_inbox_path(sid)), exist_ok=True)
    with open(_inbox_path(sid), "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def _write_inbox(sid: str, msgs: list[dict]) -> None:
    path = _inbox_path(sid)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def unread_count(sid: str) -> int:
    return sum(1 for m in _read_inbox(sid) if not m.get("read"))


def _limit_path(sid: str) -> str:
    return os.path.join(LIMITS_DIR, f"{sid}.limit.json")


def _load_limit(sid: str) -> dict:
    try:
        with open(_limit_path(sid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"window": 0, "count": 0, "hashes": []}


def _save_limit(sid: str, lim: dict) -> None:
    _atomic_write(_limit_path(sid), lim)


def _govern(from_id: str, body: str, *, lim_key: str = "") -> None:
    """限流 + 去重（单发送方；goose 工具串行调用，无并发写同发送方状态）。
    lim_key：计数分桶键（N3 dispatch 用独立桶，与 send 互不挤占；缺省=from_id）。"""
    key = lim_key or from_id
    if len(body.encode("utf-8")) > MAX_BODY:
        raise PostboxError(f"消息超过 {MAX_BODY // 1024}KB 上限，请拆分或精简")
    lim = _load_limit(key)
    now = time.time()
    if now - lim.get("window", 0) > 60:
        lim["window"], lim["count"] = now, 0
    if lim["count"] >= RATE_PER_MIN:
        raise PostboxError(f"发送过快（每分钟限 {RATE_PER_MIN} 条），请稍候或合并消息")
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    hashes = [(x, t) for x, t in lim.get("hashes", []) if now - t < DEDUP_WINDOW]
    if any(x == h for x, _ in hashes):
        raise PostboxError("与近期已发送内容重复，已拒绝（防环/防刷）")
    hashes.append((h, now))
    lim["hashes"] = hashes[-DEDUP_KEEP:]
    lim["count"] += 1
    _save_limit(key, lim)


# ---------------------------------------------------------------- 唤醒（M3）

def _flatten(body: str) -> str:
    return re.sub(r"\s*\n\s*", " ⏎ ", body.strip())


def wake_text(body: str, from_name: str, seq: int) -> tuple[str, bool]:
    """构造唤醒短语。返回 (text, inline_ok)：inline_ok=True 表示已含完整正文。"""
    flat = _flatten(body)
    if len(flat) <= WAKE_INLINE_MAX:
        return f"[postbox] 来自 {from_name} 的消息(seq={seq}): {flat}", True
    return (f"[postbox] {from_name} 发来一条 {len(flat)} 字符的长消息(seq={seq})，"
            f"将随本回合结束自动送达"), False


def _tmux_wake(pane: str, text: str) -> bool:
    """向稳定 pane_id 注入一句话并回车。失败（无 tmux/pane 消失）返回 False。"""
    try:
        for args in (["tmux", "send-keys", "-t", pane, "-l", text],
                     ["tmux", "send-keys", "-t", pane, "Enter"]):
            r = subprocess.run(args, capture_output=True, timeout=5)
            if r.returncode != 0:
                return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 入站白名单 + 会话对预算（M4）

def _allow_path(sid: str) -> str:
    # 放 sessions/ 而非 inbox/<sid>/：后者在首条消息前不存在，白名单会静默失效（M4 实证）
    return os.path.join(SESSIONS_DIR, f"{sid}.allow")


def inbound_allowed(to_id: str, from_id: str, from_name: str) -> bool:
    """入站白名单：sessions/<sid>.allow 存在时，仅允许列出的发送方（id 或显示名）投递。

    文件不存在 = 接受所有人（默认）。每行一项，# 开头为注释。
    """
    try:
        with open(_allow_path(to_id), encoding="utf-8") as f:
            allowed = {l.strip() for l in f if l.strip() and not l.startswith("#")}
    except OSError:
        return True
    return from_id in allowed or (from_name and from_name in allowed)


def _pair_key(a: str, b: str) -> str:
    return "->".join(sorted([a, b]))


def _pair_budget_check(from_id: str, to_id: str) -> None:
    """两会话间每小时消息预算（双向合计）：限流只限速率不限总量，回环可无限持续
    （M3 实证双向自动对话后引入的熔断器）。超限抛错，提示人工介入。"""
    budget = load_config().get("pair_budget_per_hour", PAIR_BUDGET_DEFAULT)
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        budget = PAIR_BUDGET_DEFAULT  # 脏配置回退默认，不让 send 裸异常
    path = os.path.join(LIMITS_DIR, f"pair.{_pair_key(from_id, to_id)}.json")
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        rec = {"window": 0, "count": 0}
    now = time.time()
    if now - rec.get("window", 0) > PAIR_WINDOW:
        rec = {"window": now, "count": 0}
    if rec["count"] >= budget:
        raise PostboxError(
            f"两会话间每小时消息预算({budget}条)已用尽，回环已熔断；"
            f"如确需继续，请人工删除 {path}")
    rec["count"] += 1
    _atomic_write(path, rec)


# ---------------------------------------------------------------- 收发

def send(from_id: str, from_name: str, to_id: str, body: str) -> dict:
    if is_disabled():
        runtime_log("send-blocked", reason="paused", sender=from_id, to=to_id)
        raise PostboxError("postbox 已暂停（管理员急停）。恢复：postbox-ctl resume")
    if not inbound_allowed(to_id, from_id, from_name):  # 入站白名单最先（不占预算）
        runtime_log("send-blocked", reason="allowlist", sender=from_id, to=to_id)
        raise PostboxError(f"对方启用了入站白名单，{from_name or from_id} 不在其中，消息被拒收")
    _pair_budget_check(from_id, to_id)
    _govern(from_id, body)
    if not from_name:  # F3: 回填发送方显示名
        from_name = display_name(from_id)

    with _inbox_lock(to_id):  # F1: 未读检查+追加 原子化
        msgs = _read_inbox(to_id)
        if sum(1 for m in msgs if not m.get("read")) >= MAX_UNREAD:
            raise PostboxError(f"对方未读消息已达 {MAX_UNREAD} 条上限，拒绝投递（请等对方收取）")
        seq = (msgs[-1]["seq"] + 1) if msgs else 1
        msg = {"seq": seq, "from": from_id, "from_name": from_name,
               "ts": now_iso(), "body": body, "read": False}
        _append_inbox(to_id, msg)

    # M3 唤醒：仅对 空闲+在线+有pane+非自己 的目标
    rec = registry_entries().get(to_id, {})
    wake = "skipped"
    if to_id == from_id:
        wake = "skipped(self)"
    elif rec.get("busy"):
        wake = "skipped(busy,Stop-hook将在回合边界注入)"
    elif not rec.get("tmux_pane"):
        wake = "skipped(no-tmux,Stop-hook通道)"
    else:
        text, inline_ok = wake_text(body, from_name, seq)
        if _tmux_wake(rec["tmux_pane"], text):
            wake = "injected" if inline_ok else "notified"
            if inline_ok:  # 正文已直达，标记已读防 Stop hook 重复注入
                with _inbox_lock(to_id):
                    m2 = _read_inbox(to_id)
                    for m in m2:
                        if m["seq"] == seq:
                            m["read"] = True
                    _write_inbox(to_id, m2)
        else:
            wake = "skipped(tmux-failed,Stop-hook通道)"
    runtime_log("send-ok", sender=from_id, to=to_id, seq=seq, wake=wake)
    return {"to": to_id, "to_name": display_name(to_id), "seq": seq,
            "wake": wake, "unread": unread_count(to_id)}

def recv(self_id: str, mark_read: bool = True, include_read: bool = False) -> list[dict]:
    with _inbox_lock(self_id):  # F1: 读+标记+重写 原子化
        msgs = _read_inbox(self_id)
        out = [m for m in msgs if include_read or not m.get("read")]
        if mark_read and out:
            seen = {m["seq"] for m in out}
            for m in msgs:
                if m["seq"] in seen:
                    m["read"] = True
            _write_inbox(self_id, msgs)
    return out


def inject_payload(self_id: str) -> str | None:
    """Stop hook 注入文案：取未读（标记已读），构造 block reason。无未读返回 None。

    与 recv() 的分工：本函数供 hook 在回合边界调用；recv() 供扩展在回合内调用。
    两者都原子标记已读，天然防重复投递。
    """
    if is_disabled():
        return None  # 急停时不注入
    relay_pull_inbox(self_id)  # relay 未读先落本地，注入/唤醒机制复用
    msgs = recv(self_id, mark_read=True, include_read=False)
    if not msgs:
        return None
    lines = []
    for m in msgs[:INJECT_MAX]:
        b = _flatten(m["body"])
        if len(b) > 1500:
            b = b[:1500] + "…(截断)"
        lines.append(f"[postbox] 来自 {m.get('from_name') or m['from']}"
                     f"(seq={m['seq']}, {m['ts']}): {b}")
    if len(msgs) > INJECT_MAX:
        lines.append(f"[postbox] 另有 {len(msgs) - INJECT_MAX} 条未注入")
    lines.append("（以上为跨会话消息，请处理后正常结束回合；这是 postbox 投递，不是错误）")
    runtime_log("inject", sid=self_id, count=len(msgs))
    return "\n".join(lines)


# ---------------------------------------------------------------- 跨机投递（M5, v0.5）
# 语法: to="名字@peer"；peers 表在 config.json: {"peers": {"alias": {"host": "ssh-target", "enabled": true}}}
# 分工：发送侧执行 大小/限流/去重/急停；目的侧执行 白名单/未读上限/落箱/唤醒（双侧 flock）。
# 跨机回环兜底：双方各自 sender 限流(10/min)；pair 预算跨机版留 M5a-3。

def parse_peer(to: str) -> tuple:
    """'名@peer' -> (名, peer|None)；无 @ 为本机。"""
    if "@" in to:
        name, peer = to.rsplit("@", 1)
        return name.strip(), (peer.strip() or None)
    return to.strip(), None


def load_peers() -> dict:
    return load_config().get("peers") or {}


MDNS_PEERS_FILE = os.path.join(BASE, "mdns", "peers.json")


def load_mdns_peers() -> dict:
    """postbox_mdns.py 发现的 peers（缺文件/缺依赖时为空，功能可选）。"""
    try:
        with open(MDNS_PEERS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        import datetime as _dt
        now = _dt.datetime.now()
        out = {}
        for k, v in d.items():
            host = v.get("ssh", "")
            if not host:
                continue
            try:  # 24h 未见到的条目视为陈旧（守护退出后文件会滞留）
                age = (now - _dt.datetime.fromisoformat(v.get("seen", "1970-01-01"))).total_seconds()
            except ValueError:
                age = 0
            if age < 86400:
                out[k] = {"host": host, "enabled": True, "mdns": True}
        return out
    except (OSError, ValueError):
        return {}


def load_peers_union() -> dict:
    """静态 config.json 优先，mDNS 自动发现补充（M5c 零配置组网）。"""
    merged = dict(load_mdns_peers())
    merged.update(load_peers())  # 静态覆盖自动发现
    return merged


def origin_label() -> str:
    return load_config().get("origin") or os.uname().nodename


def remote_deliver(payload: dict) -> dict:
    """远端目的侧执行（在对方机器上跑，同版 store）：白名单+未读上限+落箱+唤醒。"""
    try:
        from_id = str(payload.get("from", "?"))
        from_name = str(payload.get("from_name", ""))
        origin = str(payload.get("origin", ""))
        cross_id = f"{from_id}@{origin}" if origin else from_id
        to = str(payload.get("to", ""))
        body = str(payload.get("body", ""))
        if is_disabled():
            return {"ok": False, "error": "postbox 已暂停（远端急停）"}
        to_id = resolve_target(to)
        if not inbound_allowed(to_id, cross_id, from_name):
            runtime_log("send-blocked", reason="allowlist(remote)", sender=cross_id, to=to_id)
            return {"ok": False, "error": f"远端白名单拒收：{from_name or cross_id} 不在名单"}
        with _inbox_lock(to_id):
            msgs = _read_inbox(to_id)
            if sum(1 for m in msgs if not m.get("read")) >= MAX_UNREAD:
                return {"ok": False, "error": f"远端未读已达 {MAX_UNREAD} 上限"}
            seq = (msgs[-1]["seq"] + 1) if msgs else 1
            _append_inbox(to_id, {"seq": seq, "from": cross_id,
                                  "from_name": from_name or from_id,
                                  "ts": now_iso(), "body": body, "read": False})
        rec = registry_entries().get(to_id, {})
        wake = "skipped(no-tmux)"
        if to_id == from_id:
            wake = "skipped(self)"
        elif rec.get("busy"):
            wake = "skipped(busy,Stop-hook注入)"
        elif rec.get("tmux_pane"):
            text, inline_ok = wake_text(body, from_name or cross_id, seq)
            if _tmux_wake(rec["tmux_pane"], text):
                wake = "injected" if inline_ok else "notified"
                if inline_ok:
                    with _inbox_lock(to_id):
                        m2 = _read_inbox(to_id)
                        for m in m2:
                            if m["seq"] == seq:
                                m["read"] = True
                        _write_inbox(to_id, m2)
        runtime_log("deliver-ok(remote)", sender=cross_id, to=to_id, seq=seq, wake=wake)
        return {"ok": True, "to": to_id, "name": display_name(to_id), "seq": seq,
                "wake": wake, "unread": unread_count(to_id)}
    except PostboxError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"远端内部错误: {e}"}


def remote_entry(req: dict) -> dict:
    """ssh 远端唯一入口：deliver（投递）/ registry（注册表快照，供对端 list 合并）"""
    if req.get("action") == "deliver":
        return remote_deliver(req.get("payload") or {})
    if req.get("action") == "probe":
        # 随时查询：解析远端任务日志阶段标记 + 进度文件 + 进程状态（无需被委托方配合）
        import re as _re
        out = {"ok": True, "origin": origin_label()}
        log = os.path.expanduser(str(req.get("log") or ""))
        try:
            txt = open(log, encoding="utf-8", errors="replace").read()
            steps = _re.findall(r"=== STEP (\d+)", txt)
            out["last_step"] = steps[-1] if steps else None
            out["log_size"] = len(txt)
            out["log_mtime_age"] = int(time.time() - os.path.getmtime(log))
            out["tail"] = txt[-200:]
        except OSError:
            out["log"] = "missing"
        try:
            out["progress"] = json.load(open(os.path.expanduser(
                str(req.get("progress") or "~/.local/share/goose/postbox/progress.json")),
                encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        try:
            p = subprocess.run(["pgrep", "-af", "cargo build|rustup-init|rustc"],
                               capture_output=True, text=True, timeout=5)
            out["procs"] = [l[:70] for l in p.stdout.splitlines()[:3]]
        except Exception:  # noqa: BLE001
            pass
        return out
    if req.get("action") == "registry":
        rows = [{"id": r["id"],
                 "name": (r.get("joined_name") or r.get("name")
                          or session_list_snapshot().get(r["id"], {}).get("name", "")),
                 "busy": bool(r.get("busy")),
                 "last_seen": r.get("last_seen", ""),
                 "unread": unread_count(r["id"]),
                 "tmux_pane": r.get("tmux_pane", "")}
                for r in registry_entries().values()]
        return {"ok": True, "sessions": rows, "origin": origin_label()}
    return {"ok": False, "error": f"未知 action: {req.get('action')}"}


_CARRY_CODE = (
    "import sys,json,base64;src=base64.b64decode(sys.argv[1]).decode();"
    "ns={'__name__':'postbox_store_carried'};"
    "exec(compile(src,'postbox_store.py','exec'),ns);"
    "print(json.dumps(ns['remote_entry'](json.load(sys.stdin))))"
)


def _ssh_json(host: str, req: dict, timeout: int = 20) -> dict:
    """code-carried 跨机调用：store 源码 base64 随行，远端零安装、版本即发送侧。
    远端仅需 python3.7+ 与 ssh 免密；BASE 取远端 HOME。"""
    import base64
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        b64 = base64.b64encode(open(os.path.join(here, "postbox_store.py"), "rb").read()).decode()
        import shlex
        remote = "python3 -c " + shlex.quote(_CARRY_CODE) + " " + shlex.quote(b64)
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, remote],
            input=json.dumps(req), capture_output=True, text=True, timeout=timeout)
        out = p.stdout.strip().splitlines()
        if p.returncode != 0 or not out:
            return {"ok": False,
                    "error": f"对端不可达/执行失败({host}): {(p.stderr or p.stdout).strip()[:160]}"}
        return json.loads(out[-1])
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"ssh 失败({host}): {e}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ssh 超时({host})"}


def send_to(to: str, from_id: str, from_name: str, body: str) -> dict:
    """统一发送入口（v0.5）：'名@peer' 走跨机 ssh 直投，否则本机。"""
    name, peer = parse_peer(to)
    if not name:
        raise PostboxError("目标名为空")
    if peer == "relay":
        return relay_send(name, from_id, from_name, body)  # 显式中继
    if not peer:
        try:
            return send(from_id, from_name, resolve_target(name), body)
        except PostboxError as e:
            # R4 修复：仅"未找到目标会话"（寻址未命中）才回退中继；
            # 急停/白名单/预算等治理拒绝必须原样上抛，不得被 relay 通道绕过。
            if "未找到目标会话" in str(e) and relay_online():
                return relay_send(name, from_id, from_name, body)
            raise
    peers = load_peers_union()
    p = peers.get(peer)
    if not p or not p.get("host"):
        raise PostboxError(f"未知 peer {peer!r}；已配置: {sorted(peers) or '(无)'}")
    if not p.get("enabled", True):
        raise PostboxError(f"peer {peer!r} 已停用（config.json peers.{peer}.enabled=false）")
    if is_disabled():
        runtime_log("send-blocked", reason="paused", sender=from_id, to=to)
        raise PostboxError("postbox 已暂停（管理员急停）。恢复：postbox-ctl resume")
    _govern(from_id, body)  # 发送侧治理：大小/限流/去重
    _pair_budget_check(from_id, f"{peer}/{name}")  # 跨机回环熔断（发送侧计数）
    payload = {"from": from_id, "from_name": from_name or display_name(from_id),
               "to": name, "body": body, "origin": origin_label()}
    out = _ssh_json(p["host"], {"action": "deliver", "payload": payload})
    if not out.get("ok"):
        runtime_log("send-blocked", reason="remote", sender=from_id, to=to,
                    err=str(out.get("error", ""))[:80])
        raise PostboxError(f"[{peer}] {out.get('error', '远端失败')}")
    runtime_log("send-ok", sender=from_id, to=out.get("to"), peer=peer,
                seq=out.get("seq"), wake=out.get("wake"))
    return {"to": out.get("to"), "to_name": out.get("name", ""), "peer": peer,
            "seq": out.get("seq"), "wake": out.get("wake", ""),
            "unread": out.get("unread")}


# ---------------------------------------------------------------- v0.6 任务契约头
# 纯文本约定（delegation-playbook §4），宽容解析：字段缺失不报错，仅前缀匹配。
# 形态： "[progress] task=m5b step=3/7 状态一句话"

_CONTRACT_RE = re.compile(r"^\s*\[(task|progress|steer|done|fail|alert)\]\s*(.*)$")


def contract_header(kind: str, task_id: str, **fields) -> str:
    """构造契约头行：contract_header("progress", "m5b", step="3/7", note="ok")"""
    parts = [f"task={task_id}"] + [f"{k}={v}" for k, v in fields.items()]
    return f"[{kind}] " + " ".join(str(p) for p in parts)


def parse_contract(body: str) -> dict | None:
    """宽容解析：命中返回 {kind, task, fields}；否则 None。"""
    m = _CONTRACT_RE.match(body or "")
    if not m:
        return None
    fields: dict[str, str] = {}
    for tok in m.group(2).split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    return {"kind": m.group(1), "task": fields.pop("task", ""), "fields": fields}


def task_updates(limit: int = 50) -> dict:
    """扫描本机全部收件箱，聚合契约消息 → {task: {last, events[]}}（M5c: 给 ctl tasks 用）。"""
    out: dict = {}
    if not os.path.isdir(INBOX_DIR):
        return out
    for sid in os.listdir(INBOX_DIR):
        try:
            for m in _read_inbox(sid):
                c = parse_contract(str(m.get("body", "")))
                if not c:
                    continue
                t = out.setdefault(c["task"] or "?", {"last": None, "events": []})
                t["events"].append({"kind": c["kind"], "fields": c["fields"],
                                    "ts": m.get("ts", ""),
                                    "from": m.get("from_name") or m.get("from", ""), "to": sid})
                t["last"] = m.get("ts", "")
        except OSError:
            continue
    for t in out.values():
        t["events"] = t["events"][-10:]
    return dict(sorted(out.items(), key=lambda kv: kv[1]["last"] or "", reverse=True)[:limit])


# ---------------------------------------------------------------- relay 通道（wing MVP）
# config: {"relay": {"url": "http://hub:8765", "token": "..."}}；仅出站 HTTP，无入站依赖。
# 寻址：显式 名@relay 强制走中继；普通名字在本地 union 未命中且 relay 已配置时回退中继。

def relay_cfg() -> dict:
    r = load_config().get("relay") or {}
    return r if r.get("url") and r.get("token") else {}


def relay_online() -> bool:
    return bool(relay_cfg())


def _relay_call(path: str, payload: dict | None = None, wait: int = 0, timeout: int = 30) -> dict:
    import urllib.request
    import urllib.parse
    cfg = relay_cfg()
    url = cfg["url"].rstrip("/") + path + ("?" + urllib.parse.urlencode(
        {"token": cfg["token"], "wait": wait, "mark": 1}) if "?" not in path else "")
    data = json.dumps(payload or {}).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        kw = {}
        if cfg["url"].startswith("https"):  # 自签证书：跳过校验（后续换 CA 再收紧）
            import ssl
            kw["context"] = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, **kw) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:  # 4xx 带 JSON 体
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"relay 不可达: {e}"}


def relay_send(to: str, from_id: str, from_name: str, body: str) -> dict:
    if is_disabled():  # R4: 急停覆盖所有通道（本地/ssh/relay）
        runtime_log("send-blocked", reason="paused", sender=from_id, to=to)
        raise PostboxError("postbox 已暂停（管理员急停）。恢复：postbox-ctl resume")
    _govern(from_id, body)  # 发送侧治理照旧（本地限流/去重先行）
    r = _relay_call("/send", {"token": relay_cfg()["token"], "to": to, "from": from_id,
                              "from_name": from_name or display_name(from_id), "body": body})
    if not r.get("ok"):
        runtime_log("send-blocked", reason="relay", sender=from_id, to=to,
                    err=str(r.get("error", ""))[:60])
        raise PostboxError(f"[relay] {r.get('error', '投递失败')}")
    runtime_log("send-ok", sender=from_id, to=r.get("to"), peer="relay", seq=r.get("seq"))
    return {"to": r.get("to"), "to_name": r.get("to", ""), "peer": "relay",
            "seq": r.get("seq"), "wake": "relay(poll)", "unread": None}


def relay_pull_inbox(self_id: str) -> int:
    """把 relay 上的本节点未读拉回本地收件箱（relay 侧即取即读）。返回拉回条数。"""
    if not relay_online():
        return 0
    r = _relay_call("/poll", wait=0)
    n = 0
    for m in r.get("messages") or []:
        with _inbox_lock(self_id):
            msgs = _read_inbox(self_id)
            seq = (msgs[-1]["seq"] + 1) if msgs else 1
            msgs.append({"seq": seq, "from": f"{m.get('from','?')}@relay",
                         "from_name": m.get("from_name", ""), "ts": m.get("ts", now_iso()),
                         "body": m.get("body", ""), "read": False})
            _write_inbox(self_id, msgs)
        n += 1
    if n:
        runtime_log("relay-pull", sid=self_id, count=n)
    return n


def relay_sessions() -> list:
    r = _relay_call("/sessions") if relay_online() else {}
    return [{**s, "id": f"{s['name']}@relay", "peer": "relay", "remote": True,
             "alive": s.get("online", False)} for s in (r.get("sessions") or [])]


def _load_snapshot() -> dict:
    """节点负载快照（N4 资源画像）：loadavg + cpu 差分 + 内存水位，/proc 只读，缺文件静默。"""
    out: dict = {}
    try:
        parts = open("/proc/loadavg", encoding="ascii").read().split()
        out["load1"], out["load5"] = float(parts[0]), float(parts[1])
    except (OSError, ValueError, IndexError):
        pass
    try:  # cpu_pct：/proc/stat 两次采样差分（0.15s，开机平均值不可用）
        def _cpu():
            ln = next(l for l in open("/proc/stat", encoding="ascii") if l.startswith("cpu "))
            v = [int(x) for x in ln.split()[1:]]
            return sum(v), v[3] + v[4]  # total, idle(+iowait)
        t1, i1 = _cpu()
        time.sleep(0.15)
        t2, i2 = _cpu()
        if t2 > t1:
            out["cpu_pct"] = round(100 * (1 - (i2 - i1) / (t2 - t1)), 1)
    except (OSError, ValueError, StopIteration):
        pass
    try:
        mi = {}
        for line in open("/proc/meminfo", encoding="ascii"):
            k, v = line.split(":", 1)
            mi[k] = int(v.strip().split()[0])  # kB
        if mi.get("MemTotal"):
            out["mem_pct"] = round(100 * (1 - mi.get("MemAvailable", 0) / mi["MemTotal"]), 1)
    except (OSError, ValueError):
        pass
    return out


def relay_heartbeat(busy: bool = False, tmux_pane: str = "") -> None:
    """会话活动时向 relay 报心跳（fire-and-forget，任何失败静默）。hook 各事件调用。
    N4：附节点负载快照（load1/load5/mem_pct），hub 侧 NODES 展开即透出。"""
    if not relay_online():
        return
    try:
        _relay_call("/heartbeat", {"busy": busy, "tmux_pane": tmux_pane,
                                   **_load_snapshot()}, timeout=3)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 执行面 dispatch（N3-min）

DISPATCH_BACKENDS = ("fleetd", "nf-intake")
DISPATCH_MAX_PROMPT = 8 * 1024        # 派发 prompt 上限（执行比消息贵，从严）
FLEETD_TOKEN_FILE = os.path.expanduser("~/.local/state/fleet/fleetd.json")

# 远端建票脚本：锁文件 + 读改写 w1-tickets.json（N1 实证 load_tickets 唯一票源），
# 可选触发一轮 window driver（nohup + setsid，ssh 断开不亡）。参数经 __PARAMS__ 注入
# （json.dumps 输出即合法 Python 字面量，无 shell 拼接）。
_NF_APPEND_TMPL = r'''
import json, fcntl, os, subprocess, sys
P = json.loads(__PARAMS__)
P["ticket"]["repo_path"] = os.path.expanduser(P["ticket"]["repo_path"])  # ~ 在远端侧展开
p = os.path.expanduser(P["tickets_path"])
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p + ".lock", "w") as lf:
    fcntl.flock(lf, fcntl.LOCK_EX)
    try:
        d = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []
        if any(t.get("id") == P["ticket"]["id"] for t in d):
            print(json.dumps({"ok": False, "error": "ticket id already exists"})); sys.exit(0)
        d.append(P["ticket"])
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2))
        os.replace(tmp, p)
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
if P.get("trigger"):
    cmd = ("cd {root} && nohup python3 infra/night-driver/night_driver.py "
           "--mode window --now --only {tid} --window-minutes {win} "
           "> /tmp/wd-{tid}.log 2>&1 &").format(root=P["nf_root"], tid=P["ticket"]["id"],
                                                win=P["window_minutes"])
    subprocess.Popen(cmd, shell=True, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(json.dumps({"ok": True, "ticket_id": P["ticket"]["id"]}))
'''


def _dispatch_cfg() -> dict:
    return load_config().get("dispatch") or {}


def _fleetd_token() -> str:
    """token：config 显式优先，discovery 文件兜底（fleetd 自启时落盘）。"""
    tok = (_dispatch_cfg().get("fleetd") or {}).get("token")
    if tok:
        return str(tok)
    try:
        with open(FLEETD_TOKEN_FILE, encoding="utf-8") as f:
            return str(json.load(f).get("token") or "")
    except (OSError, ValueError):
        return ""


def _fleetd_call(path: str, payload: dict | None, timeout: int = 15,
                 method: str = "") -> dict:
    import urllib.error
    import urllib.request
    cfg = _dispatch_cfg().get("fleetd") or {}
    base = str(cfg.get("url") or "http://127.0.0.1:8790").rstrip("/")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method or None,
                                 headers={"Content-Type": "application/json",
                                          "x-secret-key": _fleetd_token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"error": f"HTTP {e.code}"}
    except (OSError, ValueError) as e:
        return {"error": f"fleetd 不可达: {e}"}


def _dispatch_govern(from_id: str, target: str, prompt: str) -> None:
    """dispatch 独立闸门链（R4：每通道独立设闸；顺序同 send 语义）：
    全局急停 → 通道开关 → 目标白名单 → pair 预算 → 大小/限流/去重（独立计数桶）。"""
    if is_disabled():
        runtime_log("dispatch-blocked", reason="paused", sender=from_id, to=target)
        raise PostboxError("postbox 已暂停（管理员急停），dispatch 被拒。恢复：postbox-ctl resume")
    dcfg = _dispatch_cfg()
    if dcfg.get("enabled") is False:
        runtime_log("dispatch-blocked", reason="channel-disabled", sender=from_id, to=target)
        raise PostboxError("dispatch 通道已关闭（config.json dispatch.enabled=false）")
    allow = dcfg.get("targets")
    if isinstance(allow, list) and allow and target not in allow:
        runtime_log("dispatch-blocked", reason="target-allowlist", sender=from_id, to=target)
        raise PostboxError(f"dispatch 目标 {target!r} 不在白名单 {allow}")
    _pair_budget_check(from_id, f"dispatch@{target}")
    if len(prompt.encode("utf-8")) > DISPATCH_MAX_PROMPT:
        raise PostboxError(f"prompt 超过 {DISPATCH_MAX_PROMPT // 1024}KB 上限，请精简任务描述")
    _govern(from_id, prompt, lim_key=f"dispatch-{from_id}")


def dispatch(from_id: str, from_name: str, to: str, prompt: str,
             backend: str = "fleetd", node: str | None = None,
             timeout_s: int = 120, trigger: bool = True) -> dict:
    """执行面原子操作（N3-min）：一句话驱动远端执行。

    backend=fleetd    经本机 fleetd POST /dispatch（F1 链路）同步执行并返回结果。
    backend=nf-intake 在目标机 nf 票池建 T 票（打标 source.node/class:batch，ADR-0003
                      demand 来源），返回票 id；trigger=true 顺带触发一轮 window driver
                      （白天 --now 演练式），四态经目标机 runs 表 / vk :4050 可查。
    """
    to = (to or "").strip()
    if not to:
        raise PostboxError("缺少目标 to（peer 名，如 l150）")
    if not prompt.strip():
        raise PostboxError("缺少 prompt（任务描述）")
    if backend not in DISPATCH_BACKENDS:
        raise PostboxError(f"未知 backend {backend!r}；可选: {DISPATCH_BACKENDS}")
    _dispatch_govern(from_id, to, prompt)
    if backend == "fleetd":
        return _dispatch_fleetd(from_id, to, prompt, node, timeout_s)
    return _dispatch_nf_intake(from_id, to, prompt, trigger=trigger)


def _dispatch_fleetd(from_id: str, to: str, prompt: str,
                     node: str | None, timeout_s: int) -> dict:
    node = (node or parse_peer(to)[0]).split("@")[0]  # 缺省 node=peer 同名 slug
    timeout_s = max(30, min(int(timeout_s), 600))
    r = _fleetd_call("/dispatch",
                     {"node": node, "prompt": prompt, "timeoutMs": timeout_s * 1000},
                     timeout=timeout_s + 15)
    if r.get("error"):
        runtime_log("dispatch-err", backend="fleetd", sender=from_id, to=to, node=node,
                    err=str(r["error"])[:120])
        raise PostboxError(f"[fleetd:{node}] {r['error']}")
    runtime_log("dispatch-ok", backend="fleetd", sender=from_id, to=to, node=node,
                session=str(r.get("sessionId"))[:40], stop=str(r.get("stopReason"))[:20])
    return {"backend": "fleetd", "to": to, "node": node, "result": r}


def _nf_ssh(host: str, code: str, timeout: int = 30) -> dict:
    """ssh 执行远端 python（stdin 喂代码）。独立函数便于测试 monkeypatch。"""
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                            host, "python3", "-"],
                           input=code.encode(), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ssh {host} 超时({timeout}s)"}
    except OSError as e:
        return {"ok": False, "error": f"ssh 启动失败: {e}"}
    out = (r.stdout or b"").decode().strip()
    if r.returncode != 0 or not out:
        return {"ok": False, "error": (r.stderr or b"").decode()[:200] or f"exit {r.returncode}"}
    try:
        return json.loads(out.splitlines()[-1])
    except ValueError:
        return {"ok": False, "error": f"远端输出非 JSON: {out[:120]}"}


def _dispatch_nf_intake(from_id: str, to: str, prompt: str, *, trigger: bool) -> dict:
    peers = load_peers_union()
    p = peers.get(parse_peer(to)[1] or to) or {}
    host = p.get("host")
    if not host:
        raise PostboxError(f"nf-intake 需要已配置的 peer（{to!r} 无 host；已配置: {sorted(peers) or '(无)'}）")
    if not p.get("enabled", True):
        raise PostboxError(f"peer {to!r} 已停用（config.json peers.{to}.enabled=false）")
    nfcfg = _dispatch_cfg().get("nf") or {}
    ticket_id = "WD-" + time.strftime("%Y%m%d%H%M%S") + f"-{os.urandom(2).hex()}"
    repo = nfcfg.get("repo_path") or "~/night-factory/demo/demo-repo"  # ~ 留给远端展开（本机 $HOME ≠ 远端）
    params = {
        "tickets_path": nfcfg.get("tickets_path") or "~/night-factory/tasks/w1-tickets.json",
        "nf_root": nfcfg.get("nf_root") or "~/night-factory",
        "window_minutes": int(nfcfg.get("window_minutes") or 10),
        "trigger": bool(trigger),
        "ticket": {
            "id": ticket_id,
            "objective": prompt,
            "repo_path": repo,
            "complexity": nfcfg.get("complexity") or "low",
            "budget": nfcfg.get("budget") or {"turns": 6, "tokens": 30000,
                                              "wall_time_hours": 0.5},
            "acceptance": nfcfg.get("acceptance") or ["commits_ahead > 0"],
            "created_at": now_iso(),
            "source": {"kind": "demand", "via": "wing-dispatch", "node": to,
                       "class": "batch", "from": from_id},
        },
    }
    out = _nf_ssh(host, _NF_APPEND_TMPL.replace("__PARAMS__", json.dumps(json.dumps(params))),
                  timeout=25)
    if not out.get("ok"):
        runtime_log("dispatch-err", backend="nf-intake", sender=from_id, to=to,
                    err=str(out.get("error"))[:120])
        raise PostboxError(f"[nf-intake:{to}] {out.get('error', '远端建票失败')}")
    runtime_log("dispatch-ok", backend="nf-intake", sender=from_id, to=to,
                ticket=ticket_id, triggered=bool(trigger))
    note = ("四态可查：目标机 infra/night-driver/state.sqlite3 runs 表 / vk :4050；"
            f"driver 日志 /tmp/wd-{ticket_id}.log" if trigger else
            "已入池，等待目标机下一轮 window driver 认领")
    return {"backend": "nf-intake", "to": to, "ticket_id": out.get("ticket_id") or ticket_id,
            "triggered": bool(trigger), "note": note}
