# goose-postbox — goose 多会话消息 + 执行派发（开源 worker · 局域网零配置）

让 goose agent 之间像同事一样协作：**一个会话的产出，主动送进另一个会话的上下文**——
空闲即唤醒，忙碌不打扰；跨机器、跨网络照样收发；一句话还能把任务**派发到远端节点执行**。

对标 Claude Code 的 Cross-session messaging / Agent Teams，全部基于 goose 官方原语
（hook 插件 + MCP 扩展 + tmux），纯 Python3 标准库，零外部依赖。

## 三种用法（一张表选场景）

| 场景 | 你要做什么 | 怎么通 |
|---|---|---|
| **单机多会话** | 同机多个 goose 会话互发 | 装完即用，零配置 |
| **局域网跨机** | 两台机器上的会话互发 | `postbox-ctl mdns start` 自动发现（或静态 peers） |
| **跨网/跨地域** | 不同网络的会话互发 | 接入官方托管 hub：`join` 一条命令（**免费 3 节点**，超出注册升级） |

```
会话 alpha（本机）                            会话 beta（任意位置）
 ├ postbox_send(to="beta@relay", body=…) ──▶ 官方托管 hub（relay.claw-scale.com）
 │                                            └─▶ beta 收件箱 → 空闲唤醒 / 回合边界注入
 ├ postbox_dispatch(to="l150", prompt=…) ──▶ 执行面：远端节点跑任务，结果回传
 └ postbox_list / postbox_recv：谁在线、收什么，随时可查
```

## 特性

| 能力 | 说明 |
|---|---|
| 定向发送 | `postbox_send`：名字 / 前缀 / session_id 寻址；跨网用 `名@relay` |
| 会话发现 | `postbox_list`：活跃会话 + 忙闲 + 存活 + 未读数 |
| 收取 | `postbox_recv`：拉取未读并标记；另有 hook 自动注入通道 |
| **mDNS 自动发现** | `postbox-ctl mdns start`：局域网 peer 零配置互见（`mdns list` 查看发现结果） |
| **跨网 hub（官方托管）** | join key 一条命令接入，零部署零运维；免费档 3 节点，更多注册升级 |
| **执行派发** | `postbox_dispatch`（v0.8）：backend=fleetd（同步执行回传）/ nf-intake（票池建票+触发，四态可查） |
| **负载画像** | hook 心跳携带 load1/load5/cpu_pct/mem_pct → hub Console 实时显示 |
| 空闲唤醒 | tmux 会话空闲时短文（≤400 字）直达开新回合，长文先通知后注入 |
| 忙时投递 | Stop hook 在回合结束时把未读注入模型上下文（不打断运行中的工具） |
| 治理 | 64KB 上限 / 10条·分⁻¹·发送方⁻¹ / 5 分钟去重 / 未读 50 上限 / 会话对 30条·时⁻¹ 熔断 / 入站白名单 / 一键急停 |

## 要求

- goose ≥ 1.50.0（hooks + STDIO 扩展 `AGENT_SESSION_ID` 注入）
- python3 ≥ 3.10（标准库即可）
- 可选：tmux（启用"空闲唤醒"通道；无 tmux 仍有回合边界注入）

## 安装

### 方式一：GitHub Release tarball（推荐——插件+扩展+pb 管理命令全套）

```bash
curl -fsSL -O https://github.com/cosmoxone/goose-postbox/releases/latest/download/goose-postbox-v0.8.3.tgz
tar xzf goose-postbox-v0.8.3.tgz && cd goose-postbox && ./install.sh
```

### 方式二：git clone + 一键脚本（跟踪最新 / 参与贡献）

```bash
git clone https://github.com/cosmoxone/goose-postbox && cd goose-postbox
./install.sh            # 安装插件到 ~/.agents/plugins/postbox 并自动合并扩展配置（含 pb=postbox-ctl）
./install.sh --no-ext   # 只装插件；扩展用会话参数 --with-extension 按需挂载
```

### 方式三：goose plugin install（仅插件，扩展需手配）

```bash
goose plugin install https://github.com/cosmoxone/goose-postbox
# MCP 扩展手动合并（<path> 替换为插件目录）：~/.config/goose/config.yaml →
#   extensions: { postbox: { name: postbox, cmd: python3,
#     args: ["<插件目录>/ext/postbox_server.py"], enabled: true, type: stdio, timeout: 300 } }
```

脚本幂等；修改 `config.yaml` 前自动备份。安装后**新开会话**生效（运行中的旧会话不加载）。

## 快速上手（单机 30 秒）

> 完整入门见 **QUICKSTART.md**（3 分钟版）。

```bash
goose session -n alice      # 终端 1
goose session -n bob        # 终端 2（建议都在 tmux 里，体验完整唤醒通道）
```

对 alice 说：

> 用 postbox_send 给 bob 发消息：schema 迁移完成，users 表新增 email_verified 列

bob 空闲则被直接唤醒处理；忙碌则在当前任务结束时收到。bob 回执同样自动送达 alice。

## 局域网跨机（mDNS 自动发现）

```bash
pb mdns start            # 两台机器各执行一次（别名：pb mdns start --name 办公机）
pb mdns list             # 查看发现的 peer
```

发现到的 peer 直接用名字互发（`用 postbox_send 给 办公机 发消息：…`）。
mDNS 不便时用静态配置：`~/.local/share/goose/postbox/config.json` 里
`{"peers": {"<名字>": {"host": "user@ip", "enabled": true}}}`（走 ssh 直投）。

## 跨网接入官方托管 hub（团队协作，零部署）

管理员给你 join key 后，一条命令接入（**免费档：3 节点/团队**，无需部署任何服务端）：

```bash
~/.agents/plugins/postbox/postbox-ctl join https://relay.claw-scale.com <join_key> 你的名字
```

自动完成：注册（获得本机专属 token）→ 写好配置 → 打印你的 Console 地址
（浏览器打开可看：节点负载 / 消息流 / 移动指挥席）。

接入后：`用 postbox_send 给 同事名@relay 发消息：hi` —— 跨网送达，空闲即唤醒。

**超过 3 台机器、或需要团队管理/审计/告警通道？** 注册升级付费档（规划中，
`hub 服务由官方运营，无需自购服务器/域名/证书`）。

## 工具参考

| 工具 | 参数 | 说明 |
|---|---|---|
| `postbox_send` | `to`（名/前缀/id/名@relay）、`body`（≤64KB） | 发送；返回 seq / 唤醒状态 / 对方未读数 |
| `postbox_recv` | `mark_read`(默认 true)、`all`(含已读) | 收取本会话消息 |
| `postbox_list` | — | 可寻址会话清单（含 is_self 标记） |
| `postbox_dispatch` | `to`（peer 名）、`prompt`（≤8KB）、`backend`("fleetd"/"nf-intake")、`node`?、`timeout_s`?(30-600)、`trigger`? | 执行面：fleetd 同步执行回传；nf-intake 建票打标并触发一轮执行，返回票 id |

**寻址优先级**：session_id 精确 → `-n` 名字精确 → 名字唯一前缀；跨网 `名@relay`。
未命中或歧义时报错并列出候选。

**投递语义**：

| 接收方状态 | 通道 |
|---|---|
| 空闲 + tmux + 短文 | tmux 唤醒，正文直达（一步到位） |
| 空闲 + tmux + 长文 | 唤醒通知一句；正文在下个回合边界注入 |
| 忙碌（回合中） | 不打扰；回合结束由 Stop hook 注入未读（≤5 条/次） |
| 无 tmux（Desktop 等） | Stop hook 通道，下个回合边界送达 |

## 治理与安全

| 防线 | 参数 | 备注 |
|---|---|---|
| 单条大小 | 64KB | 超限拒绝 |
| 发送方限流 | 10 条/分钟 | |
| 内容去重 | 300 秒 × 最近 8 条哈希 | 防刷屏/同文回环 |
| 未读上限 | 50 条（跨发送方） | 接收方不消费时拒收 |
| 会话对预算 | 30 条/小时（双向合计） | **回环熔断**；`pair_budget_per_hour` 可调 |
| 入站白名单 | `sessions/<id>.allow` | 每行一个允许的发送方；文件不存在=接受所有人 |
| **一键急停** | `pb pause` | 一切收发/注入/dispatch 立即停止（数据保留），`pb resume` 恢复 |

**执行面独立闸（v0.8）**：dispatch 另有 急停 → `dispatch.enabled` 通道开关 →
`dispatch.targets` 目标白名单 → pair 预算（独立计数）→ 限流去重（独立桶）。
全部事件入 `runtime.log`（dispatch-ok/err/blocked）。

**威胁模型**：仅限**同一操作系统用户**（跨机走 ssh/hub 各自鉴权）。数据目录
`~/.local/share/goose/postbox/` 权限随 HOME；注入消息带 `[postbox]` 前缀以便模型
区分人类输入。

## 数据与清理

```
~/.local/share/goose/postbox/
├── sessions/<id>.json     # 会话注册表（hook 维护；含 .allow 白名单）
├── inbox/<id>/inbox.jsonl # 收件箱（+ lock 锁文件）
├── limits/                # 限流/去重/会话对预算计数
├── config.json            # 可选运行时配置（peers / pair_budget / dispatch）
├── runtime.log            # 运维事件日志（>1MB 轮转）
└── hooks.log              # hook 事件审计日志（>1MB 自动轮转）
```

彻底卸载：`postbox-ctl uninstall --purge`。

## 管理：安装 · 升级 · 卸载 · 急停 · 日志

统一入口 `postbox-ctl`（安装后位于 `~/.agents/plugins/postbox/postbox-ctl`，建议
`alias pb=~/.agents/plugins/postbox/postbox-ctl`）：

| 命令 | 作用 |
|---|---|
| `postbox-ctl status` | 版本 / 在线会话 / 未读数 / 急停状态 |
| `postbox-ctl join <hub> <key> [名]` | 一键注册接入 hub（MT-0.5） |
| `postbox-ctl mdns start/stop/list` | 局域网自动发现托管 |
| `postbox-ctl update` | 升级（git 用户先 git pull） |
| `postbox-ctl pause / resume` | 急停 / 恢复 |
| `postbox-ctl log [--file hooks] [--tail N]` | 事件流水（排障） |
| `postbox-ctl uninstall [--purge]` | 卸载（数据默认保留） |

## 开源 vs 托管服务（边界透明）

本项目为**商业开源**（OpenCore）：开源侧=局域网域完整可用；跨网域=官方托管服务。

| 能力域 | 开源（本仓，MIT） | 官方托管 hub |
|---|---|---|
| worker：本机 + 局域网（ssh/mDNS）三工具 + 治理 + 急停 + 执行面 dispatch | ✅ 完整无阉割、无 phone-home | 同一 worker |
| 跨网消息 / Console / 负载看板 / 移动指挥席 | ❌（局域网域外属服务） | ✅ **免费档 3 节点** |
| >3 节点 / 团队管理 / 审计 / 告警通道 / SLA | ❌ | ✅ 注册付费（规划中） |

判断准则：**局域网内单团队自用 → 开源；跨网与多组织协同 → 官方服务。**
（完整边界设计见研发主仓 opencore 文档。）

## 版本历史

- **v0.8.3**（2026-10-15）：**开源边界重划（商业开源 v2）**——开源=本机+局域网（mDNS）；跨网统一为官方托管 hub（免费 3 节点，注册升级）；移除自托管 hub 途径与相关资产
- **v0.8.2**（2026-10-15）：公开文档重写——三场景导引（单机/局域网 mDNS/跨网 hub）、开源边界表、治理速览
- **v0.8.1**（2026-10-15）：安装修复（postbox-ctl 漏装 / 全新环境崩溃）；指引填入真实地址
- **v0.8.0**（2026-10-14）：`postbox_dispatch` 执行面双后端 + 负载心跳 + 独立治理闸

## 来源

研究项目 `sessions-msg`（对标调研：Claude Code Cross-session messaging / Agent Teams）。

License: MIT
