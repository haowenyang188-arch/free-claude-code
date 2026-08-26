# WSL Codex CLI / Claude CLI 升级参考映射

状态：参考架构与实施路线（不是 DSH 或 WorkBuddy 的交付声明）。

## 1. 目标与范围

本项目的目标是：拆解 DeepSeek Harness（DSH）与 WorkBuddy 的能力、架构、技术栈、优点和限制，转化为可验证的升级项，改造本仓库在 WSL 中启动和管理 Codex CLI / Claude CLI 的运行时。

主线边界如下：

- **交付对象**：`cli/` 中的 Codex/Claude 会话与进程管理，以及 Workbench 对这些运行时的统一接入。
- **参考对象**：DSH 提供 sidecar、JSON-RPC、插件图、预检、资源边界和事件投影的参考；WorkBuddy 提供 gateway、能力注册表、工作流、审批、持久化、隔离和可观测性的参考。
- **保持原生**：Codex CLI 和 Claude CLI 仍是实际执行器；不把 DSH Node runtime 或 WorkBuddy 全套服务变成必选依赖。
- **验收原则**：每一项升级必须有运行时契约、失败路径和测试证据；未实施内容不得以“参考实现”描述。

本文只记录已从本地文件核对的事实。外部项目的行为若未在下列文件中出现，不在本文推断。

## 2. 参考对象拆解

### 2.1 DeepSeek Harness

**技术栈与边界**

- Python 宿主位于 `harness/config.py`、`harness/bridge.py`；桥接层负责启动和拥有官方 runtime，但不实现 Cordis，也不仿造插件加载器。
- Node 运行时由 `harness/runtime/runner.mjs` 启动，`harness/runtime/README.md` 记录其固定的 `0.1.0-rc.6` JSON-RPC launcher 和 `npm ci` 插件闭包。
- `harness/runtime/README.md` 所列官方图包含 JSON-RPC server、agent spine、DeepSeek LLM、JSONL persistence/checkpoints、subprocess、bash 和 filesystem 插件。凭据通过子进程环境传递，不写入 `cordis.yml`。
- 协议边界是 `initialize`、`session/prompt` 以及 `session.event`/`session.status` 通知；`harness/bridge.py:249` 将已知 assistant 事件投影为 Anthropic SSE，并保留命名空间事件。

**架构能力**

1. `HarnessConfig`（`harness/config.py:239`）集中验证 argv、路径、workspace containment、插件 allowlist、超时、并发、frame/stderr/token 上限和可选 runtime version。
2. `DeepSeekHarnessManager.ensure_ready()`（`harness/bridge.py:692`、`:731`）做预检并按 provider/model 惰性创建 bridge；`diagnostics()` 输出不含凭据的 readiness 状态。
3. `DeepSeekHarnessBridge`（`harness/bridge.py:75`）为每个 workspace/session 建立映射和锁，记录 session parent lineage，按根 session 收集通知；超时或取消时关闭不可信的 runtime 进程。
4. 自定义 Cordis 配置必须保留 JSON-RPC server 插件，且每个配置插件都必须出现在 `DSH_PLUGIN_ALLOWLIST`；空 allowlist 是拒绝策略。

**优点**

- 启用是显式 opt-in，运行时版本、插件图和资源上限可验证。
- argv 不经过 shell，workspace 路径经过 containment 检查，插件图经过 allowlist 检查。
- runtime 与宿主有清晰的 JSON-RPC 边界；事件可投影到现有 SSE，同时保留未知插件事件。
- session tree、按 session 锁、并发上限、超时和安全 diagnostics 为长任务提供了可操作的失败边界。

**限制与不能直接照搬之处**

- 需要 Node/npm 运行时和完整插件闭包；这不应成为 Codex/Claude CLI 的新增必需依赖。
- bridge 的 session alias 和 lineage 是进程内状态；runtime 重启会清空映射，超时后不能假定旧 session id 可以安全恢复。
- 内容块只接受 text/thinking；任意 provider 的完整消息语义不能自动转换，`messages_to_content_blocks()` 会拒绝不支持的类型。
- DSH 的 checkpoint、sandbox、subprocess 是插件图能力，不能因此宣称现有 Codex/Claude 已具备同等持久化和隔离。

### 2.2 WorkBuddy

**技术栈与边界**

- `/home/gnen/work-buddy/README.md` 将其定义为基于 Claude Code 和 Obsidian 的 local-first personal-agent runtime；安装包自带 Python，控制面在本机运行，文档标记为 beta。
- `/home/gnen/work-buddy/docs/architecture.md` 描述多进程本地架构：Claude Code/slash commands -> MCP gateway（`localhost:5126`）-> capability registry/workflow conductor，再连接 messaging（`5123`）、embedding（`5124`）、Telegram（`5125`）、dashboard（`5127`）、Obsidian bridge（`27125`）、Hindsight、Calendar、Chrome 和本地数据层。
- README 明确 dashboard 是 React app 的控制面；本文只借鉴其控制面/数据面分层，不把这些服务或端口引入 CLI 核心。

**架构能力**

1. gateway 只暴露固定的小工具集：`wb_init`、`wb_search`、`wb_run`、`wb_advance`、`wb_status`、`wb_step_result`、`wb_capability_result`（`docs/architecture.md:53-64`）。能力通过 registry 搜索和发现，而不是向 agent 暴露数百个工具。
2. conductor 将多步工作表示为依赖图，按顺序执行、处理中断恢复，并把确定性的装载/整形步骤留在代码中，只在需要判断时唤起 agent（`docs/architecture.md:66-68`）。
3. README 的 approval 叙述把敏感动作放入显式人工决策环；Telegram、Obsidian 和 dashboard 都可以接收请求，首个响应生效。
4. `knowledge/store/architecture/agent-execution.md` 进一步记录 provider/model registry、精确 session/lease ACL、generation 所有权、producer provenance、clean config/credential projection、disclosure manifest 和失败清理边界。这些是设计参考，不能当作本仓库已有能力。

**优点**

- 固定 gateway + registry 降低工具面，支持自然语言能力发现和统一结果查询。
- workflow 的代码步骤与 agent 步骤分离，长任务更可重复、更便宜，也更容易在中断后恢复。
- approval、session history、dashboard 和 memory 把“可观察、可干预”纳入执行控制环，而不是事后查看日志。
- provider-neutral execution boundary、session ACL、generation ownership 和 provenance 使多运行时切换仍可追溯。

**限制与不能直接照搬之处**

- gateway、conductor、多个常驻服务、外部集成和数据层带来端口、进程、升级和故障传播成本；这对单机 CLI 核心过重。
- README 标记 beta，且明确 Windows 优先、Linux/macOS 仍可能有边缘问题；不能把其跨平台结论直接移植到 WSL。
- WorkBuddy 的 Claude/Obsidian/Telegram/Hindsight/Chrome 语义属于产品工作流，不是 Codex/Claude 子进程协议；只抽取契约和隔离原则。
- `/home/gnen/work-buddy` 是 GPL-3.0-only 项目；本仓库不复制其代码或资产，只记录设计参考，任何代码复用须另行做许可证审查。

## 3. 能力映射与实施目标

状态含义：**已存在** = 当前代码已经提供可依赖的局部契约；**部分存在** = 只覆盖一个 backend、一个生命周期或一个存储面；**待实施** = 指定文件中没有可依赖的实现证据。

| 参考能力 | 当前 WSL 位置/事实 | 状态 | 实施目标 | 验收证据 |
|---|---|---|---|---|
| Preflight + runtime registry | DSH 有 `HarnessConfig.validate_cordis_config()`（`harness/config.py:359`）、`_runtime_preflight_status()`（`harness/bridge.py:863`）和 `DeepSeekHarnessManager.diagnostics()`；CLI manager 只校验 `agent_backend in {claude,codex}`（`cli/manager.py:55`）。 | 部分存在 | 建立 provider-neutral `RuntimeRegistry`：对 Claude/Codex 的可执行文件、版本、模型/feature 能力做有界探测、缓存和脱敏；选择保存为不可变的 `{provider_id, model_id}`，启动前再次验证，禁止静默 fallback。DSH 继续作为可选 profile。 | 新增 registry/preflight 契约测试：不可执行、版本漂移、模型消失、缓存过期、单 provider 失败隔离；运行结果只含用户安全状态。覆盖 `tests/cli/` 与 `tests/harness/` 的对应测试。 |
| Capability discovery / adapter contract | WorkBuddy gateway 以 `wb_search`/`wb_run`/`wb_status` 收敛工具面；当前 `CLISessionManager` 直接分支构造 `CodexSession` 或 `CLISession`（`cli/manager.py:83-123`），没有统一 feature catalog。 | 待实施 | 定义最小 adapter capability contract（stream、resume/fork、pause/cancel、approval、checkpoint、event schema），由 registry 返回；上层只依赖 contract，不把 provider-specific argv 散落到工作流。 | 两个 fake CLI adapter 通过同一 contract；未知 capability 明确返回 typed unsupported，而非运行时猜测。 |
| Session lifecycle / ownership | Claude `CLISession.start_task()` 使用 `_cli_lock`、session id resume（`cli/session.py:53`）；Codex `CodexSession` 支持 `resume/fork`、`stop/pause/resume`（`cli/codex_session.py:59-87`、`:369-417`）；manager 维护 pending/real id 映射并能 `stop_all()`（`cli/manager.py:125-179`）。 | 部分存在 | 抽取统一状态机：`created -> preflighted -> starting -> ready -> running -> waiting_approval/checkpointed -> stopping -> stopped/failed`。为进程使用 `(pid,generation)` 所有权，保证自然退出、取消、重启和 stale id 不会误杀或复用。保留 Codex resume/fork 和 Claude resume 语义。 | 进程异常、重复 start/stop、取消、PID 回收、session id 迁移和 manager 重启的状态转移测试；每次终止都有可关联的 generation 和最终事件。 |
| Approval + sandbox policy | Claude 支持 `plan`、`acceptEdits`、`auto`、`bypassPermissions`，并可加 `--add-dir`/`--dangerously-skip-permissions`（`cli/session.py:27-46`、`:103-130`）；Codex 支持显式 sandbox，非 read-only 时可建立 `StagedWorkspace`，发出 `approval_required`、`approval_waiting`，再 `approve/reject`（`cli/codex_session.py:87-220`、`:389-403`）。 | 部分存在 | 统一 `WorkspacePolicy` 与 `ApprovalPolicy`，把路径边界、sandbox、diff、人工决定、apply/reject 和失败清理作为同一契约；`danger-full-access` 不得与 staged approval 组合；缺少明确决定时 fail closed。 | `tests/cli/test_staging.py`、`test_workspace.py`、`test_codex_session.py` 加上 Claude 对等测试；验证路径逃逸、空 diff、拒绝后清理、审批超时和进程失败不落盘。 |
| Event normalization + provenance | Codex 在 `_read_events()`/`_normalize_record()`（`cli/codex_session.py:233-297`）把 JSONL 转为 assistant/tool/file/error 事件；Claude 将每行 JSON 解析并提取 session id（`cli/session.py:141-256`）；DSH bridge 有 session tree 与 SSE 投影；Workbench `EventEnvelope`/`EventLog`（`workbench/backend/runtime/events.py:47-220`）已有顺序、JSONL replay、backend/session/runtime/step/artifact 字段和递归脱敏。 | 部分存在 | 采用版本化的 provider-neutral envelope，同时保存有界 raw reference 与 normalized event：`run_id`、`session_id`、`generation`、provider/model、runtime version、workspace/policy digest、sequence、timestamp、event type、artifact ids。所有 assistant/tool/approval/checkpoint/error 都可按 cursor replay，不能丢失 producer provenance。 | 以 Claude/Codex/DSH fixture 做顺序、重复、截断、未知事件和 replay 测试；`tests/harness/test_events.py`、`tests/workbench/test_extended_event_envelope.py` 作为验收入口，新增跨 backend 契约测试。 |
| Workflow + checkpoint | DSH 官方插件图在 `harness/runtime/README.md:14-19` 声明 JSONL persistence/checkpoints；Codex 仅有进程级 pause/resume，Workbench `EventLog.replay()` 只提供事件回放，不等于 workflow checkpoint。 | 部分存在 | 在 CLI 之上建立轻量 conductor：步骤依赖、确定性步骤/agent 步骤边界、checkpoint manifest、人工审批节点和可恢复 cursor。checkpoint 必须绑定 runtime version、provider/model、session/generation、workspace/policy digest；不匹配时停止并要求重新预检。 | kill/restart/timeout/approval-wait 后从最后安全 checkpoint 重放；验证重复执行幂等、旧 checkpoint 拒绝、未完成副作用不自动重放。 |
| Security isolation + credential boundary | DSH 有 argv-only、allowlist、workspace containment、frame/stderr/token/time limits（`harness/config.py:239-439`）；Workbench event log 递归屏蔽 token/key/password（`workbench/backend/runtime/events.py:20-39`、`:258-280`）。但 Claude/Codex 当前均从 `os.environ.copy()` 启动，Codex 还继承当前环境（`cli/session.py:68`、`cli/codex_session.py:127`）。 | 部分存在 | 为每次运行生成最小环境和 clean config；只投影明确授权的 account/API 凭据，清除无关 provider、MCP、proxy 和 workspace 指令；把 workspace、session、conversation、consumer、generation 绑定到不可覆盖的 lease/ACL。原始凭据不进入 prompt、argv、事件或日志。 | 负向测试证明环境变量/配置/MCP 泄漏为零、symlink/path escape 被拒绝、过期 generation 无权写入、日志和 JSONL 不含凭据；失败清理可重试且不误杀不拥有的 PID。 |
| Observability + operations | DSH manager 提供安全 `diagnostics()`；CLI 有 `process_registry`、`get_stats()`、退出码/stderr 日志；Workbench EventLog 可持久化并回放事件。WorkBuddy 另有 sidecar supervisor 做 on-demand 启动、重启和 health-check（`docs/architecture.md:95-97`）。 | 部分存在 | 增加统一 readiness/liveness、启动耗时、运行/等待审批/恢复/失败计数、退出原因、runtime version 和 bounded stderr 摘要；将健康检查、重启退避和审计事件接入同一 manager，但不引入 WorkBuddy 的常驻服务拓扑。 | 断电、CLI 不存在、版本不匹配、stderr 超限、runtime hang、重复重启的故障注入测试；诊断接口只显示脱敏状态且能解释下一步动作。 |

推荐的最终数据流：

```text
preflight -> registry selection -> policy/lease -> CLI adapter
    -> normalized events + provenance -> EventLog/replay
    -> approval or checkpoint -> resume/close -> diagnostics
```

## 4. 分阶段路线

### Phase 0：冻结事实与契约

- 记录当前 Claude/Codex 实际 argv、JSONL fixtures、退出/取消/审批行为和 WSL/Windows 路径边界。
- 定义 provider-neutral event envelope、lifecycle 状态、policy 错误码和 registry selection 结构；先用 fake CLI 验证，不改变默认 backend。
- 将现有 `tests/cli`、`tests/harness`、`tests/workbench` 中的边界测试整理为迁移前基线。

### Phase 1：统一预检与 registry

- 抽取 RuntimeProfile/RuntimeRegistry，复用 DSH 的 argv、版本、插件/能力和资源上限校验思想。
- Claude/Codex 各自实现 bounded probe；一个 provider 不可用时不影响另一个 provider 的 catalog。
- 先在 `CLISessionManager` 前加 preflight gate；DSH 保持 disabled-by-default 和独立 diagnostics。

### Phase 2：统一 session lifecycle 与 supervisor

- 将 `CLISession`、`CodexSession` 的启动、resume/fork、cancel、pause、stop、自然退出和清理接入同一状态机。
- 以 generation ownership 扩展现有 `process_registry`，避免 PID 重用和旧 session alias 误操作。
- 加入 bounded restart/backoff；任何重启前清空不可恢复的 runtime-scoped session mapping。

### Phase 3：审批、sandbox 与凭据隔离

- 将 Codex `StagedWorkspace` 的 diff/apply/reject 契约抽为公共 policy；为 Claude 的 permission mode 建立同等的 approval event，而不是把字符串模式直接暴露给工作流。
- 建立每次运行的 clean environment/config 和 workspace lease；将危险权限与审批互斥作为构造期错误。
- 先覆盖读操作与显式 staged 写入，保留原生 CLI 的能力差异并在 registry 中声明。

### Phase 4：事件、来源与持久化

- 把 Claude/Codex JSONL 和 DSH notification 适配为同一 envelope；保留有界 raw reference，不把未解析内容静默丢弃。
- 复用 Workbench `EventLog` 的顺序、fsync、JSONL replay、redaction 和 cursor 约定；补充 producer provenance、generation、policy/workspace digest。
- 将 approval、checkpoint、resume、error 和 cleanup 作为一等事件，供 dashboard/消息层消费。

### Phase 5：workflow/checkpoint

- 在 manager 上提供最小 conductor：确定性准备/校验步骤由代码执行，模型判断步骤才启动 CLI；每一步有 step id、输入 digest 和 checkpoint。
- 恢复前再次执行 registry、policy、workspace 和 generation 校验；旧版本/旧 workspace/旧权限不得自动续跑。
- 先做单进程串行 workflow，再评估并发；不复制 WorkBuddy 的全套 gateway/服务。

### Phase 6：可观测性、迁移与收敛

- 对每个 backend 开 feature flag，按事件、恢复、审批和安全负向测试结果逐步启用。
- 以 DSH 作为协议/安全边界的对照 runtime，以 WorkBuddy 作为 workflow/控制面的对照，不把二者的 sidecar 或产品服务纳入默认启动链。
- 发布前完成 CLI 版本漂移、WSL 信号、凭据清理、日志脱敏和长任务恢复的故障注入；保留降级为现有直接 CLI adapter 的路径。

## 5. 明确不迁移项

- 不把 `harness/runtime` 的 Node/Cordis 插件闭包、DeepSeek API 或 `DEEPSEEK_API_KEY` 变成 Codex/Claude 的依赖或默认认证路径。
- 不复制 WorkBuddy 的 Obsidian、Telegram、Chrome、Hindsight、embedding、dashboard、端口拓扑或 GPL 代码；需要时只实现等价的最小 provider-neutral contract。
- 不把 provider-specific argv、模型别名、权限字符串塞进共享 base config；由各 adapter/profile 自己声明 capability。
- 不做 provider/model 的静默 fallback，不用新的 catalog 覆盖已持久化的会话来源，不把旧 session id 当成重启后的有效身份。
- 不把 `danger-full-access` 与“等待审批”组合，不在未经明确授权时自动 apply staged diff。
- 不把原始凭据、完整认证信息、prompt 中的 workspace secret 或未脱敏 stderr 写入事件、日志、manifest 或 checkpoint。
- 不以“DSH/WorkBuddy 已接入”作为完成标准；完成标准是 WSL Codex/Claude 的统一契约、可恢复性、隔离性和可验证故障边界。

## 6. 风险与控制点

| 风险 | 影响 | 控制点 |
|---|---|---|
| Codex/Claude CLI 版本或 JSONL schema 漂移 | parser 丢事件、resume 失败、错误状态被误判 | registry 版本探测、fixture contract、未知事件保留、启动前版本门禁 |
| WSL/Windows 路径和信号差异 | workspace 越界、SIGSTOP/terminate 行为不一致、清理失败 | `Path.resolve()` 后 containment、平台矩阵、generation-owned cleanup、故障注入 |
| 当前 CLI 继承父环境 | proxy、MCP、token 或项目指令越权进入子进程 | clean env/config projection、启动前环境审计、secret-negative tests |
| 事件投影有损 | approval/tool/checkpoint 无法恢复或审计 | versioned envelope + bounded raw reference + sequence/cursor replay |
| staged workspace 与用户并发修改冲突 | apply 覆盖或错误合并 | workspace/policy digest、显式冲突状态、拒绝自动 apply |
| 长任务恢复误执行副作用 | 重复写入、重复发送、错误 provider 继续运行 | checkpoint manifest、幂等 step、旧 generation/旧 profile fail closed |
| 参考架构带来的运维膨胀 | 单机 CLI 变成多服务故障面 | 只抽取 registry/policy/event/workflow 契约，保持本地进程内实现和可关闭 sidecar |
| WorkBuddy GPL/beta/平台边界 | 法务、兼容性和长期维护风险 | 不复制代码；只依据本地文档提取设计；所有复用另行审查 |

## 7. 结论

之前把 DSH/WorkBuddy 当作 CLI 升级的外围或交付对象是不准确的。正确方向是：**以它们为拆解样本，直接升级 WSL Codex CLI / Claude CLI 的 runtime contract**。DSH 主要贡献 preflight、进程/协议边界、资源限制和事件投影经验；WorkBuddy 主要贡献 registry、workflow/checkpoint、approval、provenance、隔离和可观察控制环。最终实现仍应以本仓库 `cli/`、`workbench/backend/` 和相应测试为唯一交付面。
