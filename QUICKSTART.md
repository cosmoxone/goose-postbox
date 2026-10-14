# Quick Start · goose-postbox 三分钟上手

让两个正在跑的 goose 会话互发消息——就像给同事发微信一样。

---

## ① 安装（一次性，三选一；⭐=当前可用）

**⭐ 方式 A · GitHub Release（公开下载，推荐）**
```bash
curl -fsSL -O https://github.com/cosmoxone/goose-postbox/releases/latest/download/goose-postbox-v0.8.0.tgz
tar xzf goose-postbox-v0.8.0.tgz && cd goose-postbox && ./install.sh
```

**方式 B · git（跟踪最新 / 参与贡献）**
```bash
git clone https://github.com/cosmoxone/goose-postbox && cd goose-postbox && ./install.sh
```

**方式 C · 自托管分发点（团队内网镜像，tarball 同通道）**
```bash
curl -fsSL -O http://<你的分发点>/goose-postbox-v0.8.0.tgz   # 然后同方式 A 后两步
```

看到 `==> 完成` 即可。之后**新开的** goose 会话都会自动带上 postbox。

装完先玩 ②③（本机/局域网零配置）。要跨网团队协作见 ⑥（接入 hub，3 行配置）；
想自己运营 hub：另取 `relay_hub.py` 单文件自托管（release-deploy.md §2，零依赖起步，有域名时 Caddy 反代 + Let's Encrypt 一步到企业形态）。

## ② 试用（30 秒）

开两个终端，各启动一个**带名字**的会话：

```bash
goose session -n alice     # 终端 1
goose session -n bob       # 终端 2
```

在 **alice** 那边输入：

> 用 postbox_send 给 bob 发消息：数据库迁移完成了，users 表多了 email_verified 列

切到 **bob** 终端看——它自己就收到了并开始处理。bob 回一句也一样会自动送到 alice。
（在 tmux 里跑效果最好：对方空闲时消息立刻送达；否则等它当前任务结束的瞬间送达。）

## ③ 常用话术（直接用自然语言）

| 想做什么 | 对任意会话说 |
|---|---|
| 发消息 | "用 postbox_send 给 alice 发消息：…" |
| 收消息 | "postbox_recv 查一下有没有新消息" |
| 看谁在线 | "postbox_list 列出会话" |

名字规则：启动时的 `-n 名字` 最准；也可以用名字开头几个字（唯一就行）或会话 ID。

## ④ 管理命令（终端里执行）

```bash
alias pb=~/.agents/plugins/postbox/postbox-ctl   # 建议加进 ~/.bashrc

pb status          # 看版本/在线会话/未读数/日志
pb log             # 最近 20 条事件（发送/投递/拒收/急停，排障用）
pb log --file hooks --tail 50                    # hook 事件流水
pb pause           # ⏸ 急停：一切收发/注入/dispatch 立即停止（数据保留）
pb resume          # ▶ 恢复
pb update          # 升级（git 用户先 git pull）
pb uninstall       # 卸载（消息数据默认保留）
pb uninstall --purge   # 卸载并清空所有数据
```

## ⑤ 执行面 30 秒（v0.8，需已配置 peer）

> 执行后端（fleet 守护 / 夜厂票池）为独立系统，**开源排期中**——未部署时本节跳过，消息面功能不受影响（内测/早期接入：提 issue 标题带 [exec-backend]）。

```
「用 postbox_dispatch 在 l150 上跑：请只回复四个字：链路已打通」
```
- backend=fleetd：同步执行，结果直接回传（需本机 fleet 守护）
- backend=nf-intake：目标机票池建票+触发，返回票 id（四态经目标机 runs 表/看板可查）
- 急停期间 dispatch 同样被拒（runtime.log 留 dispatch-blocked 痕迹）

## ⑥ 接入团队/云端 hub（跨网，一条命令）

**有 join key（管理员发的团队注册凭证，一把 key 全团队用）——推荐：**
```bash
~/.agents/plugins/postbox/postbox-ctl join https://<hub 地址> <join_key> [本机名]
```
自动完成：注册（获得本机专属 token，各机器独立收件箱）→ 写好配置 → 打印你的 Console 地址。

**或已有个人 token（管理员直接发的）：**
```bash
python3 - <<'EOF'
import json, os
p = os.path.expanduser("~/.local/share/goose/postbox/config.json")
d = json.load(open(p)) if os.path.exists(p) else {}
d["relay"] = {"url": "https://<hub 地址>", "token": "<你的 token>"}
json.dump(d, open(p, "w"), ensure_ascii=False, indent=1)
print("已接入 hub")
EOF
```

然后直接说：「用 postbox_send 给 同事名@relay 发消息：hi」——跨网送达，
对方空闲即唤醒；你的个人 Console：`https://<hub 地址>/console?token=<你的 token>`。
治理自动生效（10/min、会话对 30/h、急停保护）。
你的 Console 是**移动优先双 tab**：
- **总览**：节点（负载）/nf 四态/契约任务/全域消息流（只读监视，桌面端自动双栏）
- **指挥**：聊天式指挥席——选目标节点，看与它的双向消息（气泡流），底部大输入栏
  回车或点发送即投递（治理/域隔离照常）——手机上给任何在线 agent 下指令
  （如 `[steer] task=X 改用方案B`）。

## ⑦ 出问题了怎么办

| 症状 | 一句话处置 |
|---|---|
| 消息发不出去 | 看报错文案——找不到名字/被白名单拒/被预算熔断/已暂停，各自有提示 |
| 怀疑失控 | `pb pause` 先停再说，然后 `pb log` 查现场 |
| 想彻底关掉 | `pb uninstall --purge` |
| 想看原始消息 | `cat ~/.local/share/goose/postbox/inbox/*/inbox.jsonl` |

---

**一句话原理**：消息落盘在 `~/.local/share/goose/postbox/`（收件箱），插件负责"对方一闲下来就把消息递进去"，扩展提供发送/收取工具。默认同一系统用户内的多会话零配置；跨机走 ssh/mDNS（局域网自动发现）；跨网接 hub（§⑥）。

更多细节（治理上限、白名单、架构、故障排查）见 `README.md`。
