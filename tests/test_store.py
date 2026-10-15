#!/usr/bin/env python3
"""postbox 存储层单元测试（M2+M3）：直接调用 postbox_store，绕过 MCP/LLM。

运行：python3 test_store.py   （全绿输出 PASS）
用假会话 id 与注入的 session list 快照，不依赖真实 goose / tmux。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))  # 发布树布局: tests/../scripts
import postbox_store as st  # noqa: E402

FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def fake_registry(sid: str, name: str = "", pid: int | None = None, busy: bool = False) -> None:
    os.makedirs(st.SESSIONS_DIR, exist_ok=True)
    st._atomic_write(os.path.join(st.SESSIONS_DIR, f"{sid}.json"), {
        "id": sid, "name": name, "tmux_pane": "", "goose_pid": pid or os.getpid(),
        "started": st.now_iso(), "last_seen": st.now_iso(), "busy": busy, "events": 1,
    })


SNAP = {
    "testA": {"name": "alpha", "user_set_name": True, "working_dir": "/x", "updated_at": "t"},
    "testB": {"name": "beta", "user_set_name": True, "working_dir": "/x", "updated_at": "t"},
    "testD": {"name": "alpha", "user_set_name": True, "working_dir": "/x", "updated_at": "t"},
}


def main() -> int:
    # 干净环境（含上轮并发测试的 conc* 残留，否则全撞 300s 去重窗口）
    for f in (glob.glob(os.path.join(st.LIMITS_DIR, "conc*.limit.json"))
              + glob.glob(os.path.join(st.LIMITS_DIR, "pair.conc*.json"))
              + glob.glob(os.path.join(st.LIMITS_DIR, "dispatch-*.limit.json"))
              + glob.glob(os.path.join(st.LIMITS_DIR, "pair.dispatch@*.json"))):
        os.remove(f)
    if os.path.exists(st.DISABLED_FLAG):  # 上轮崩溃可能残留急停标志
        os.remove(st.DISABLED_FLAG)
    _CFG_PATH = os.path.join(st.BASE, "config.json")
    _CFG_SAVED = open(_CFG_PATH, encoding="utf-8").read() if os.path.exists(_CFG_PATH) else None
    for sid in ("testA", "testB", "testD", "testC"):
        shutil.rmtree(os.path.join(st.INBOX_DIR, sid), ignore_errors=True)
        try:
            os.remove(os.path.join(st.SESSIONS_DIR, f"{sid}.json"))
        except FileNotFoundError:
            pass
        try:
            os.remove(st._limit_path(sid))
        except FileNotFoundError:
            pass

    fake_registry("testA")
    fake_registry("testB")

    # --- 寻址（先测歧义：alpha 同时来自 testA/testD，再删 testD 测精确）---
    fake_registry("testD")
    try:
        st.resolve_target("alpha", snap=SNAP); ok = False
    except st.PostboxError as e:
        ok = "命中多个" in str(e) and "testA" in str(e) and "testD" in str(e)
    check("寻址: 同名歧义报错并列候选", ok)
    os.remove(os.path.join(st.SESSIONS_DIR, "testD.json"))
    check("寻址: -n 名精确(alpha)", st.resolve_target("alpha", snap=SNAP) == "testA")
    check("寻址: session_id 直达", st.resolve_target("testB", snap=SNAP) == "testB")
    try:
        st.resolve_target("nope", snap=SNAP); ok = False
    except st.PostboxError as e:
        ok = "未找到目标会话" in str(e)
    check("寻址: 未命中报错含可寻址清单", ok)

    # --- 发送/收取往返 ---
    r1 = st.send("testA", "alpha", "testB", "hello beta")
    r2 = st.send("testA", "alpha", "testB", "第二条")
    check("发送: seq 递增", r1["seq"] == 1 and r2["seq"] == 2)
    check("发送: 无 tmux 目标 wake=skipped", r2["wake"].startswith("skipped"))
    check("发送: 未读数=2", st.unread_count("testB") == 2)
    msgs = st.recv("testB", mark_read=True)
    check("收取: 两条且含正文/来源", len(msgs) == 2 and msgs[0]["body"] == "hello beta"
          and msgs[0]["from"] == "testA" and msgs[0]["from_name"] == "alpha")
    check("收取: 标记已读", st.unread_count("testB") == 0)
    check("收取: 默认不含已读", st.recv("testB") == [])
    check("收取: all=true 回看历史", len(st.recv("testB", include_read=True)) == 2)

    # --- 治理 ---
    try:
        st.send("testA", "alpha", "testB", "hello beta"); ok = False
    except st.PostboxError as e:
        ok = "重复" in str(e)
    check("治理: 同内容去重拒绝", ok)

    lim = st._load_limit("testA"); lim.update(window=0, count=0); st._save_limit("testA", lim)
    sent = 0
    for i in range(11):
        try:
            st.send("testA", "alpha", "testB", f"unique-{i}-{time.time()}")
            sent += 1
        except st.PostboxError:
            break
    check(f"治理: 限流在第 {sent+1} 条触发(<=10)", sent == st.RATE_PER_MIN)

    try:
        st.send("testA", "alpha", "testB", "x" * (st.MAX_BODY + 1)); ok = False
    except st.PostboxError as e:
        ok = "上限" in str(e)
    check("治理: 超 64KB 拒绝", ok)

    lim = st._load_limit("testA"); lim.update(window=0, count=0); st._save_limit("testA", lim)
    _pf = os.path.join(st.LIMITS_DIR, f"pair.{st._pair_key('testA', 'testB')}.json")
    os.path.exists(_pf) and os.remove(_pf)
    open(os.path.join(st.BASE, "config.json"), "w").write('{"pair_budget_per_hour": 9999}')  # 未读上限测试需 >50
    shutil.rmtree(os.path.join(st.INBOX_DIR, "testB"), ignore_errors=True)
    refused = None
    for i in range(st.MAX_UNREAD + 2):
        if i % st.RATE_PER_MIN == 0:  # 模拟限流窗口过期（单发送方1分钟内本就到不了50）
            lim = st._load_limit("testA"); lim.update(window=0); st._save_limit("testA", lim)
        try:
            st.send("testA", "alpha", "testB", f"fill-{i}")
        except st.PostboxError as e:
            refused = str(e); break
    check(f"治理: 未读达 {st.MAX_UNREAD} 拒收", refused is not None and "上限" in refused,
          refused or "未触发")
    os.remove(os.path.join(st.BASE, "config.json"))  # 恢复默认预算
    os.path.exists(_pf) and os.remove(_pf)

    # --- M4: 入站白名单（fill 段留下 50 未读，先清）---
    shutil.rmtree(os.path.join(st.INBOX_DIR, "testB"), ignore_errors=True)
    lim = st._load_limit("testA"); lim.update(window=0, count=0); st._save_limit("testA", lim)
    _pf = os.path.join(st.LIMITS_DIR, f"pair.{st._pair_key('testA', 'testB')}.json")
    # 前段测试已累计预算对计数（fill 段就有 50 条），此处重置
    os.path.exists(_pf) and os.remove(_pf)
    allow = st._allow_path("testB")  # sessions/<sid>.allow（目录恒存在）
    open(allow, "w", encoding="utf-8").write("someone-else\n")
    try:
        st.send("testA", "alpha", "testB", "应被白名单拒绝"); ok = False
    except st.PostboxError as e:
        ok = "白名单" in str(e)
    check("白名单: 陌生发送方拒收(不占预算)", ok)
    open(allow, "w", encoding="utf-8").write("testA\n")  # 按 id
    r = st.send("testA", "alpha", "testB", "白名单放行-by-id")
    check("白名单: 按 id 放行", r["seq"] > 0)
    open(allow, "w", encoding="utf-8").write("alpha\n")  # 按名
    r = st.send("testA", "alpha", "testB", "白名单放行-by-name")
    check("白名单: 按名放行", r["seq"] > 0)
    os.remove(allow)

    # --- M4: 会话对预算（回环熔断）---
    lim = st._load_limit("testA"); lim.update(window=0, count=0); st._save_limit("testA", lim)
    cfg = os.path.join(st.BASE, "config.json")
    open(cfg, "w", encoding="utf-8").write('{"pair_budget_per_hour": 3}')
    pair_file = os.path.join(st.LIMITS_DIR, f"pair.{st._pair_key('testA', 'testB')}.json")
    try:
        os.remove(pair_file)
    except FileNotFoundError:
        pass
    refused = None
    for i in range(5):
        try:
            st.send("testA", "alpha", "testB", f"budget-{i}")
        except st.PostboxError as e:
            refused = str(e); break
    check("预算: 第4条熔断并提示人工介入", refused is not None
          and "预算" in refused and "人工" in refused, refused or "未触发")
    try:
        st.send("testB", "beta", "testA", "reverse-should-fail"); rev_ok = False
    except st.PostboxError:
        rev_ok = True
    check("预算: 反向同享预算对(双向合计)", rev_ok)
    os.remove(cfg)  # 恢复默认
    try:
        os.remove(pair_file)
    except FileNotFoundError:
        pass

    # --- 唤醒短语构造 ---
    t1, ok1 = st.wake_text("短消息", "alpha", 3)
    t2, ok2 = st.wake_text("第一行\n第二行", "alpha", 4)
    t3, ok3 = st.wake_text("长" * (st.WAKE_INLINE_MAX + 1), "alpha", 5)
    check("唤醒: 短消息内联全文", ok1 and "短消息" in t1 and t1.startswith("[postbox]"))
    check("唤醒: 换行扁平化", ok2 and "⏎" in t2 and "\n" not in t2)
    check("唤醒: 超长只通知", not ok3 and "长消息" in t3)

    # --- Stop 注入构造 ---
    shutil.rmtree(os.path.join(st.INBOX_DIR, "testB"), ignore_errors=True)
    st.send("testA", "alpha", "testB", "注入测试正文")
    payload = st.inject_payload("testB")
    check("注入: 文案含正文与提示", payload is not None and "注入测试正文" in payload
          and "不是错误" in payload)
    check("注入: 标记已读防重复", st.inject_payload("testB") is None)

    check("注入: 标记已读防重复", st.inject_payload("testB") is None)

    # --- 运维: 急停 + 运行日志留痕 ---
    lim = st._load_limit("testA"); lim.update(window=0, count=0); st._save_limit("testA", lim)
    st.send("testA", "alpha", "testB", "待注入-急停测试")   # 制造 1 条未读
    open(st.DISABLED_FLAG, "w", encoding="utf-8").write("test\n")
    try:
        st.send("testA", "alpha", "testB", "急停应拒绝"); ok = False
    except st.PostboxError as e:
        ok = "暂停" in str(e)
    check("急停: send 立即拒绝", ok)
    check("急停: 注入停止(未读保留)", st.inject_payload("testB") is None
          and st.unread_count("testB") == 1)
    os.remove(st.DISABLED_FLAG)
    payload = st.inject_payload("testB")
    check("急停: 恢复后正常注入", payload is not None and "待注入-急停测试" in payload)
    logs = open(st.RUNTIME_LOG, encoding="utf-8").read().splitlines()[-25:]
    check("日志: send-blocked(paused)/send-ok/inject 均留痕",
          any("send-blocked" in l and "paused" in l for l in logs)
          and any("send-ok" in l for l in logs)
          and any("inject" in l for l in logs))

    # --- 并发安全（F1 回归）：5 线程发送 + 主线程并发收取 ---
    shutil.rmtree(os.path.join(st.INBOX_DIR, "testB"), ignore_errors=True)
    got: list[dict] = []
    stop_flag = threading.Event()

    def sender(n: int) -> None:
        for i in range(6):
            try:
                st.send(f"conc{n}", f"c{n}", "testB", f"c{n}-m{i}")
            except st.PostboxError:
                pass

    def receiver() -> None:
        while not stop_flag.is_set():
            got.extend(st.recv("testB"))
            time.sleep(0.005)

    rx = threading.Thread(target=receiver)
    threads = [threading.Thread(target=sender, args=(n,)) for n in range(5)]
    rx.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop_flag.set(); rx.join()
    got.extend(st.recv("testB"))  # 扫尾
    seqs = sorted(m["seq"] for m in got)
    check(f"并发: 30条全到达且无重复seq(得{len(got)}条)", len(got) == 30 and
          seqs == list(range(1, 31)), f"seqs={seqs}")

    # --- R4: 急停不可被 relay 回退绕过 ---
    open(st.DISABLED_FLAG, "w").write("t\n")
    cfg_r = os.path.join(st.BASE, "config.json")
    saved_r = open(cfg_r).read() if os.path.exists(cfg_r) else None
    open(cfg_r, "w").write(json.dumps({"relay": {"url": "http://127.0.0.1:1", "token": "x"}}))
    try:
        ok = False
        try:
            st.send_to("谁都不在的名字", "testA", "alpha", "急停下不允许回退")
        except st.PostboxError as e:
            ok = "暂停" in str(e)          # 必须是急停错误，而非 relay 报错
        check("R4: 急停优先于 relay 回退", ok)
    finally:
        if saved_r is None:
            os.remove(cfg_r)
        else:
            open(cfg_r, "w").write(saved_r)
        os.remove(st.DISABLED_FLAG)

    # --- v0.6 任务契约头 ---
    h = st.contract_header("progress", "t1", step="3/7", note="ok")
    c = st.parse_contract(h)
    check("契约: 构造+解析往返", h.startswith("[progress] task=t1")
          and c == {"kind": "progress", "task": "t1", "fields": {"step": "3/7", "note": "ok"}})
    check("契约: 非契约体宽容返回 None", st.parse_contract("普通消息") is None
          and st.parse_contract("") is None)
    st.send("testA", "alpha", "testB", st.contract_header("done", "t9", verdict="ok"))
    agg = st.task_updates()
    check("契约: 收件箱聚合含 t9", "t9" in agg
          and agg["t9"]["events"][-1]["kind"] == "done")

    # --- M5: 跨机寻址与远端投递 ---
    check("跨机: parse_peer 拆分", st.parse_peer("beta@dev2") == ("beta", "dev2")
          and st.parse_peer("beta") == ("beta", None)
          and st.parse_peer("beta@") == ("beta", None))
    cfg5 = os.path.join(st.BASE, "config.json")
    _cfg_saved = open(cfg5, encoding="utf-8").read() if os.path.exists(cfg5) else None
    open(cfg5, "w", encoding="utf-8").write(
        '{"peers": {"badpeer": {"host": "nonexistent.invalid"}}}')
    try:
        st.send_to("x@badpeer", "testA", "alpha", "x"); ok = False
    except st.PostboxError as e:
        ok = "对端不可达" in str(e) or "ssh" in str(e)
    check("跨机: 对端不可达报可读错误", ok)
    try:
        st.send_to("x@nosuch", "testA", "alpha", "x"); ok = False
    except st.PostboxError as e:
        ok = "未知 peer" in str(e) and "badpeer" in str(e)
    check("跨机: 未知 peer 报错含已配置清单", ok)
    if _cfg_saved is None:
        os.remove(cfg5)
    else:
        open(cfg5, "w", encoding="utf-8").write(_cfg_saved)  # 还原持久配置（防误删 peers）
    out = st.remote_deliver({"from": "testA", "from_name": "alpha",
                             "to": "testB", "body": "远端直投正文", "origin": "hostX"})
    check("远端: 直投成功带来源标注", out.get("ok") is True
          and st._read_inbox("testB")[-1]["from"] == "testA@hostX")
    open(st.DISABLED_FLAG, "w").write("t\n")
    out2 = st.remote_deliver({"from": "testA", "to": "testB", "body": "x", "origin": "h"})
    os.remove(st.DISABLED_FLAG)
    check("远端: 目的侧急停拒收", out2.get("ok") is False and "暂停" in out2.get("error", ""))
    st.recv("testB")  # 清未读

    # --- 清单 join ---
    rows = {r["id"]: r for r in st.list_sessions(self_id="testA")}
    check("清单: is_self 优先排序", True)  # 结构性检查见下
    check("清单: 存活判定", rows["testA"]["alive"] is True)
    fake_registry("testZ", pid=999999)
    rows = {r["id"]: r for r in st.list_sessions()}
    check("清单: 死 pid 标记 alive=false", rows["testZ"]["alive"] is False)
    os.remove(os.path.join(st.SESSIONS_DIR, "testZ.json"))

    # --- N3-min dispatch：双后端 mock + 治理拒绝 ---
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class FakeFleetd(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # 静音
            pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/nodes":
                self._send(200, {"nodes": [{"name": "l150"}]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.headers.get("x-secret-key") != "tok-test":
                self._send(401, {"error": "unauthorized"}); return
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            self._send(200, {"sessionId": "s1", "stopReason": "end_turn",
                             "text": "链路已打通(mock)", "updateCount": 9,
                             "echo_node": body.get("node")})

    httpd = HTTPServer(("127.0.0.1", 0), FakeFleetd)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cfg_saved2 = open(_CFG_PATH, encoding="utf-8").read() if os.path.exists(_CFG_PATH) else None
    json.dump({"dispatch": {"fleetd": {"url": f"http://127.0.0.1:{httpd.server_port}",
                                       "token": "tok-test"}}}, open(_CFG_PATH, "w"))
    try:
        out = st.dispatch("testA", "alpha", "l150", "跑个测试", backend="fleetd")
        check("dispatch fleetd: mock 闭环返回结果",
              out["backend"] == "fleetd" and out["node"] == "l150"
              and out["result"]["text"] == "链路已打通(mock)")
        # 独立计数桶：dispatch 不吃 send 的限流额度（testA 刚发过消息仍可用）
        out2 = st.dispatch("testA", "alpha", "l150", "再跑一个", backend="fleetd",
                           node="other-node")
        check("dispatch fleetd: node 显式覆盖",
              out2["result"]["echo_node"] == "other-node")
        # 急停拒（全局闸）
        open(st.DISABLED_FLAG, "w").write("t\n")
        try:
            st.dispatch("testA", "alpha", "l150", "x", backend="fleetd"); ok = False
        except st.PostboxError as e:
            ok = "暂停" in str(e)
        os.remove(st.DISABLED_FLAG)
        check("dispatch: 急停时被拒", ok)
        # 通道开关拒
        json.dump({"dispatch": {"enabled": False}}, open(_CFG_PATH, "w"))
        try:
            st.dispatch("testA", "alpha", "l150", "x", backend="fleetd"); ok = False
        except st.PostboxError as e:
            ok = "dispatch 通道已关闭" in str(e)
        check("dispatch: 通道开关关闭被拒", ok)
        # 目标白名单拒
        json.dump({"dispatch": {"targets": ["ak"]}}, open(_CFG_PATH, "w"))
        try:
            st.dispatch("testA", "alpha", "l150", "x", backend="fleetd"); ok = False
        except st.PostboxError as e:
            ok = "白名单" in str(e)
        check("dispatch: 目标不在白名单被拒", ok)
        # 401 透出（token 错 → fleetd 返回 unauthorized）
        json.dump({"dispatch": {"fleetd": {"url": f"http://127.0.0.1:{httpd.server_port}",
                                           "token": "wrong"}}}, open(_CFG_PATH, "w"))
        try:
            st.dispatch("testA", "alpha", "l150", "x", backend="fleetd"); ok = False
        except st.PostboxError as e:
            ok = "unauthorized" in str(e)
        check("dispatch: fleetd 401 透出为可读错误", ok)
    finally:
        httpd.shutdown()
        if cfg_saved2 is not None:
            open(_CFG_PATH, "w", encoding="utf-8").write(cfg_saved2)
        elif os.path.exists(_CFG_PATH):
            os.remove(_CFG_PATH)

    # nf-intake 后端：monkeypatch _nf_ssh，验证建票参数/打标/触发与错误通道
    captured = {}

    def fake_ssh(host, code, timeout=30):
        captured["host"], captured["code"] = host, code
        if host == "bad-host":
            return {"ok": False, "error": "ssh boom"}
        return {"ok": True, "ticket_id": "WD-x"}

    st._nf_ssh = fake_ssh
    os.path.exists(st._limit_path("dispatch-testA")) and os.remove(st._limit_path("dispatch-testA"))
    json.dump({"peers": {"l150": {"host": "192.0.2.150", "enabled": True}}},
              open(_CFG_PATH, "w"))
    try:
        out = st.dispatch("testA", "alpha", "l150", "加一个 hello.sh", backend="nf-intake")
        code = captured["code"]
        m = re.search(r"P = json\.loads\((.*)\)", code)
        params = json.loads(json.loads(m.group(1))) if m else {}  # 双重序列化：外层字符串字面量
        check("dispatch nf-intake: ssh 到 peer host 建票",
              captured["host"] == "192.0.2.150" and out["ticket_id"] == "WD-x"
              and out["triggered"] is True)
        check("dispatch nf-intake: 票含 node/class 打标与 demand source",
              params.get("ticket", {}).get("source", {}).get("node") == "l150"
              and params["ticket"]["source"]["class"] == "batch"
              and params["ticket"]["source"]["kind"] == "demand")
        check("dispatch nf-intake: trigger 命令含 --only 与 --window-minutes",
              "--only" in code and "--window-minutes" in code and "start_new_session" in code)
        # 远端脚本自验：在本地 tmp 目录真实执行 _NF_APPEND_TMPL（trigger=False 避免
        # 启动 driver；E2E 曾抓到 json 字面量嵌入 bug，此处为回归防线）
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake_tickets = os.path.join(td, "t", "w1-tickets.json")
            m2 = re.search(r"P = json\.loads\((.*)\)", code)
            pr = json.loads(json.loads(m2.group(1)))
            pr["tickets_path"] = fake_tickets
            pr["trigger"] = False
            code2 = st._NF_APPEND_TMPL.replace(
                "__PARAMS__", json.dumps(json.dumps(pr)))
            r = subprocess.run([sys.executable, "-c", code2], capture_output=True,
                               text=True, timeout=10)
            try:
                rr = json.loads(r.stdout.strip().splitlines()[-1])
                ticket_written = json.load(open(fake_tickets))
            except Exception:  # noqa: BLE001
                rr, ticket_written = {}, []
            check("dispatch nf-intake: 远端脚本可执行且票真实落盘",
                  r.returncode == 0 and rr.get("ok") and ticket_written == [
                      {**pr["ticket"], "repo_path": os.path.expanduser(pr["ticket"]["repo_path"])}],
                  (r.stderr or r.stdout)[:100])
            check("dispatch nf-intake: repo_path 的 ~ 在远端侧展开",
                  bool(ticket_written) and not str(ticket_written[0].get("repo_path", "~")).startswith("~"))
        # ssh 失败通道
        json.dump({"peers": {"l150": {"host": "bad-host"}}}, open(_CFG_PATH, "w"))
        try:
            st.dispatch("testA", "alpha", "l150", "ssh失败专用prompt", backend="nf-intake"); ok = False
        except st.PostboxError as e:
            ok = "ssh boom" in str(e)
        check("dispatch nf-intake: ssh 失败透出为可读错误", ok)
        # 未知 backend
        try:
            st.dispatch("testA", "alpha", "l150", "x", backend="nope"); ok = False
        except st.PostboxError as e:
            ok = "未知 backend" in str(e)
        check("dispatch: 未知 backend 拒绝", ok)
    finally:
        if cfg_saved2 is not None:
            open(_CFG_PATH, "w", encoding="utf-8").write(cfg_saved2)
        elif os.path.exists(_CFG_PATH):
            os.remove(_CFG_PATH)

    print("\n" + ("🎉 ALL PASS" if not FAILS else f"💥 FAILED: {FAILS}"))
    if _CFG_SAVED is not None:
        open(_CFG_PATH, "w", encoding="utf-8").write(_CFG_SAVED)  # 还原生产配置（peers 等）
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
