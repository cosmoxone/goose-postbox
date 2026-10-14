# goose-postbox — goose 会话间消息传递

让同一台机器上**同时运行的多个 goose 会话**互发消息：一个会话发现的问题/做出的决策，
可以主动送到另一个会话的上下文里——空闲时立刻唤醒，忙碌时在下个回合边界送达。

对标 Claude Code 的 Cross-session messaging，全部基于 goose 官方支持的原语
（hook 插件 + MCP 扩展 + tmux），零外部依赖（纯 Python3 标准库）。

```
会话 alpha                                    会话 beta
 ├ postbox_send(to="beta", body="迁移完成") ─▶ 收件箱 inbox.jsonl
 │                                            ├ 空闲+tmux：短文直接唤醒注入（开新回合）
 │                                            └ 忙碌：Stop hook 回合边界注入全文
 └ beta 回执同样自动送达 alpha ◀─────────────── beta: postbox_send(to="alpha", ...)
```

## 特性

| 能力 | 说明 |
|---|---|
| 定向发送 | `postbox_send`：按 `goose session -n <名字>` 的名字 / 前缀 / session_id 寻址 |
| 会话发现 | `postbox_list`：活跃会话 + 忙闲 + 存活 + 未读数 |
| 收取 | `postbox_recv`：拉取未读并标记；另有 hook 自动注入通道 |
| **执行派发** | `postbox_dispatch`（v0.8）：一句话驱动远端执行——backend=fleetd（同步执行回传结果）/ nf-intake（夜厂票池建票+触发，四态可查） |
| **负载画像** | hook 心跳自动携带 load1/load5/cpu_pct/mem_pct → hub Console 实时显示（suggest-bot 据此填空派单，见主仓 wing/） |
| 空闲唤醒 | tmux 会话空闲时短文（≤400 字）直达开新回合，长文先通知后注入 |
| 忙时投递 | Stop hook 在回合结束时把未读全文注入模型上下文（不打断运行中的工具） |
| 治理 | 64KB 上限 / 10条·分钟⁻¹·发送方⁻¹ / 5 分钟去重 / 未读 50 上限 / 会话对 30条·小时⁻¹ 熔断 / 入站白名单 |

## 要求

- goose ≥ 1.50.0（hooks + STDIO 扩展 `AGENT_SESSION_ID` 注入）
- python3 ≥ 3.10（标准库即可）
- 可选：tmux（启用"空闲唤醒"通道；无 tmux 仍有回合边界注入）

## 安装

### 方式一：git 安装插件 + 配置扩展（推荐）

```bash
goose plugin install <本仓库地址>
# 然后把 MCP 扩展合并进配置（把 <path> 替换为克隆出的插件目录）：
#   ~/.config/goose/config.yaml:
#     extensions:
#       postbox:
#         name: postbox
#         cmd: python3
#         args: ["<插件目录>/ext/postbox_server.py"]
#         enabled: true
#         envs: {}
#         type: stdio
#         timeout: 300
```

### 方式二：一键脚本

```bash
git clone <本仓库地址> && cd goose-postbox
./install.sh            # 安装插件到 ~/.agents/plugins/postbox 并自动合并扩展配置
./install.sh --no-ext   # 只装插件；扩展用会话参数 --with-extension 按需挂载
```

脚本幂等；修改 `config.yaml` 前自动备份；无 PyYAML 时打印手工片段。

### 方式三：开发模式（不动全局配置）

```bash
goose session -n alice --with-extension "postbox:python3 /path/to/goose-postbox/ext/postbox_server.py"
```

安装后**新开会话**生效（运行中的旧会话不加载）。

## 快速上手

> 完整入门见 **QUICKSTART.md**（3 分钟版）。以下为要点。

开两个终端（建议在 tmux 里，体验完整唤醒通道）：

```bash
goose session -n alice      # 终端 1
goose session -n bob        # 终端 2
```

对 alice 说：

> 用 postbox_send 给 bob 发消息：schema 迁移完成，users 表新增 email_verified 列

bob 若空闲会被直接唤醒并处理；忙碌则在其当前任务结束时收到。bob 回执同样自动送达 alice。

## 工具参考

| 工具 | 参数 | 说明 |
|---|---|---|
| `postbox_send` | `to`（名/前缀/id）、`body`（≤64KB） | 发送；返回 seq / 唤醒状态 / 对方未读数 |
| `postbox_recv` | `mark_read`(默认 true)、`all`(含已读) | 收取本会话消息 |
| `postbox_list` | — | 可寻址会话清单（含 is_self 标记） |
| `postbox_dispatch` | `to`（peer 名）、`prompt`（≤8KB）、`backend`("fleetd"/"nf-intake")、`node`?、`timeout_s`?(30-600)、`trigger`? | 执行面：fleetd 同步执行回传；nf-intake 建票打标 `source:{demand,node,class:batch}` 并触发一轮执行，返回票 id |

**寻址优先级**：session_id 精确 → `-n` 名字精确（`user_set_name`）→ 名字唯一前缀；
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
| 会话对预算 | 30 条/小时（双向合计） | **回环熔断**；`config.json` 的 `pair_budget_per_hour` 可调 |
| 入站白名单 | `sessions/<id>.allow` | 每行一个允许的发送方（id 或名）；文件不存在=接受所有人 |

**执行面独立闸（v0.8，R4 纪律：每通道独立设闸）**：dispatch 在上述防线之外另有
急停（同 postbox-ctl pause）→ `dispatch.enabled` 通道开关 → `dispatch.targets`
目标白名单 → pair 预算（`dispatch@<目标>` 独立计数）→ 限流去重（`dispatch-<sid>`
独立桶，与 send 分账）。全部事件入 `runtime.log`（dispatch-ok/err/blocked）。

**威胁模型**：与 Claude Code 同机消息一致——仅限**同一操作系统用户**。数据目录
`~/.local/share/goose/postbox/` 权限随 HOME；注入消息带 `[postbox]` 前缀以便模型
区分人类输入。回环靠预算熔断（超限报错会指明人工介入路径）。

## 数据与清理

```
~/.local/share/goose/postbox/
├── sessions/<id>.json     # 会话注册表（hook 维护；含 .allow 白名单）
├── inbox/<id>/inbox.jsonl # 收件箱（+ lock 锁文件）
├── limits/                # 限流/去重/会话对预算计数
├── config.json            # 可选运行时配置（当前仅 pair_budget_per_hour）
├── runtime.log            # 运维事件日志：send-ok / send-blocked / inject / ctl-pause…（>1MB 轮转）
└── hooks.log              # hook 事件审计日志（>1MB 自动轮转）
```

彻底卸载：`postbox-ctl uninstall --purge`（见下节）。

## 管理：安装 · 升级 · 卸载 · 急停 · 日志

统一入口 `postbox-ctl`（安装后位于 `~/.agents/plugins/postbox/postbox-ctl`，建议
`alias pb=…`）。所有操作幂等，修改 `config.yaml` 前自动备份。

| 命令 | 作用 |
|---|---|
| `postbox-ctl install [--no-ext]` | 安装插件 + 合并扩展配置 |
| `postbox-ctl update` | 升级文件（git 用户先 `git pull` 再执行；config 保留；运行中会话不受影响，新会话用新版） |
| `postbox-ctl uninstall [--purge]` | 卸载：移除插件与扩展配置；`--purge` 连数据目录一起删（默认保留消息数据） |
| `postbox-ctl pause` | **急停**：hook 静默直通、send/inject 立即拒绝（报错提示恢复方式）、新会话不加载扩展；数据保留 |
| `postbox-ctl resume` | 恢复 |
| `postbox-ctl status` | 版本 / 配置状态 / 急停状态 / 在线会话（忙闲/存活/未读）/ 日志概况 |
| `postbox-ctl log [--file runtime\|hooks] [--tail N]` | 查看事件日志 |

**可追溯性**：`runtime.log` 记录每一次发送（send-ok，含 wake 通道）、每一次拒收
（send-blocked：paused/allowlist/budget…）、每一次注入（inject，含条数）及管理动作
（ctl-pause/resume）；`hooks.log` 记录 hook 事件流水（注册/心跳/注销）。两者超 1MB
自动保留尾部 100KB。排查口径：先 `status` 看全局，再 `log` 对时间线。

**急停语义**：`pause` 后即使有会话仍持有已加载的扩展，其 send 也会立刻失败并收到
"postbox 已暂停（管理员急停）。恢复：postbox-ctl resume"；收件箱数据不丢。

## 故障排查

| 症状 | 处置 |
|---|---|
| postbox_list 为空 | 会话须在插件安装**之后**启动；检查 `~/.local/share/goose/postbox/hooks.log` 是否有 SessionStart |
| 找不到目标名字 | 目标会话未注册或名字歧义——报错会列出候选；也可直接用 session_id |
| 消息"发了但没到" | 空闲+tmux 才有即时唤醒；否则等对方下个回合边界；`inbox/<id>/inbox.jsonl` 是唯一事实源 |
| 回环被熔断 | 预算是安全阀；确认双方任务确实需要继续后，删除 `limits/pair.*.json` 对应文件 |
| hook 未生效 | 插件目录需含 scripts/postbox_store.py（install.sh 已处理）；hook 失败 fail-open 不影响会话 |

## 开发与测试

```bash
./build-release.sh                              # 从源码重建 release/goose-postbox/
cd release/goose-postbox/tests
python3 test_store.py    # 存储层 29 项：寻址/收发/治理/并发/注入/唤醒
python3 test_hook.py     # hook 7 项：注册/心跳/Stop注入/注销/防护（测已安装物）
python3 test_server.py   # MCP 协议 7 项：握手/工具/错误/身份
```

E2E 记录（tmux 双会话：唤醒/忙时注入/白名单拒收/回环熔断）见研究项目
`sessions-msg/experiments/M0-M4`。

## 局限与路线图

- 当前：仅同机同用户；唤醒通道依赖 tmux（CLI）；held 三态入站控制未实现（KISS 取舍）
- 路线图：跨机（goose roaming / Redis 总线）、`notify_when_idle` 观察者、Desktop 通道

## v0.8.0 变更（2026-10-14）

- 新增第 4 工具 `postbox_dispatch`：执行面双后端（fleetd 同步 / nf-intake 建票+触发），治理独立闸全套（见上）
- hook 心跳携带节点负载四字段（load1/load5/cpu_pct/mem_pct），hub Console 节点行实时显示
- 依赖：fleetd 后端需本机 fleet 守护（token 自动读 `~/.local/state/fleet/fleetd.json`）；nf-intake 需目标机夜厂票池（`config.json` peers + dispatch.nf 可调）
- 实验记录：主仓 `experiments/N3-dispatch.md`（双后端真机闭环 + 治理拒绝矩阵）

## 来源

研究项目 `sessions-msg`（对标调研：Claude Code Cross-session messaging / Agent Teams）。
设计文档与实验记录：主仓库 `research/sessions-msg/`。

License: MIT
