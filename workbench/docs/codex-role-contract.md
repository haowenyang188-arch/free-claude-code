# ROLE CONTRACT (SOP Workbench) — 冻结，禁止重新设计

> 生效范围：`workbench/backend/**`。本节取代所有关于「各智能体怎么分工」的口头约定。
> **权威定义在代码里**：`workbench/backend/workflow/role_contract.py`。
> **验证在测试里**：`tests/workbench/test_role_contract.py`（41 项）。
> 两者冲突时，以代码与测试为准，本节只作说明。

## 1. 固定角色

| 角色 | 职责 | 明确不拥有 |
|------|------|-----------|
| **SOP Engine** | 唯一拥有 Task / StepRun / SopRun 状态与流程流转权 | — |
| **Claude** | 理解需求、分析代码、制定 PLAN、开发决策、返工时提供修复方案 | 最终审核权；不得宣布 Task Complete |
| **DSH Desktop** | 按已确定方案实际执行代码修改、命令、测试 | 重新定义需求；最终审核权；改变 SOP 流程 |
| **Codex** | 最终独立 Reviewer：diff / 测试 / Bug / 异常路径 / 边界 / 潜在回归 / 需求满足 | 绕过 Engine 改最终流程状态 |
| **Artifact** | 阶段间唯一事实交接物 | 不依赖聊天上下文作为事实来源 |

## 2. 能力矩阵（`ROLE_CAPABILITIES`）

| 能力 | Engine | Claude | DSH | Codex |
|------|:------:|:------:|:---:|:-----:|
| `PRODUCE_PLAN` | | ✅ | ❌ | ❌ |
| `INSPECT_WORKSPACE` | | ✅ | ✅ | ✅ |
| `MODIFY_CODE` / `RUN_COMMANDS` / `RUN_TESTS` | | ❌ | ✅ | ❌ |
| `PRODUCE_ARTIFACT` | | ✅ | ✅ | ✅ |
| `EMIT_REVIEW_VERDICT` | | ❌ | ❌ | ✅ |
| `TRANSITION_*` / `ROUTE_REWORK` | ✅ | ❌ | ❌ | ❌ |
| `DECLARE_COMPLETION` / `REDEFINE_REQUIREMENT` / `CHANGE_FLOW` / `P2P_DISPATCH` | ❌ | ❌ | ❌ | ❌ |

最后一行是 `FORBIDDEN_CAPABILITIES`：**任何角色都拿不到，包括 Engine**。

## 3. 不变量

| ID | 不变量 | 强制点 |
|----|--------|--------|
| I1 | 只有 Engine 能写 SOP 状态字段 | `apply_status()`；`WorkflowEngine._set_status()` |
| I2 | 任何 Agent 不得宣告完成 | `Capability.DECLARE_COMPLETION`；源码规则 RC-2 |
| I3 | Agent 不得就地重定义需求（只能经 PLAN_INVALID 路由回 PLAN） | `Capability.REDEFINE_REQUIREMENT`；`REVIEW_ROUTING` |
| I4 | 无 Agent 间 P2P 派活，唯一通道是 Engine 的 Handoff | `Capability.P2P_DISPATCH` |
| I5 | Artifact 是阶段间唯一事实载体 | `validate_stage_output()` / `missing_review_evidence()` |
| I6 | 返工路由由 Engine 通过**显式路由表**决定 | `REVIEW_ROUTING` + `WorkflowEngine.decide_review()` |

## 4. 状态归属（`STATUS_AUTHORITY`）

| 集合 | 所需能力 |
|------|----------|
| `tasks` | `TRANSITION_TASK` |
| `step_runs` | `TRANSITION_STEP` |
| `sop_runs` | `TRANSITION_RUN` |
| `handoffs` | `TRANSITION_HANDOFF` |
| `reviews` | `TRANSITION_REVIEW` |

**终态定义**：`TERMINAL_TASK_STATUSES = {FAILED}`。
⚠️ `TaskStatus.ACCEPTED` **不是终态** —— 它表示「引擎接受了该产出」，任务仍在 review 门禁之下。
终态只能由 Engine 到达；Agent 持有 0 个 transition 能力，因此结构上不可能到达任何 Task 状态。

## 5. 路由表（`REVIEW_ROUTING`，Engine 独占）

| 信号 | 来源 | 去向 |
|------|------|------|
| `PASS` | Codex | `advance` |
| `REWORK` | Codex | `rerun_execute` |
| `PLAN_INVALID` | Codex | `return_to_plan` |
| `execution_error` | Engine 自检 | `rerun_execute` |
| `plan_obsolete` | Engine 自检 | `return_to_plan` |

`resolve_route()` 对任何非 Engine 角色抛 `RoleContractViolation`。
**禁止 Agent 或测试脚本自行决定去向。**

## 6. 阶段契约

| 阶段 | Owner | 允许产出的 Artifact 类型 |
|------|-------|--------------------------|
| `plan` | Claude | `plan` |
| `execute` | DSH | `implementation` / `diff` / `test_report` / `file` / `text` / `json` |
| `review` | Codex | `review_report` |

`validate_stage_output()` 强制 owner 与产出类型；越权抛 `RoleContractViolation`。

**Review 证据要求**：`REVIEW_REQUIRED_EVIDENCE = {DIFF, TEST_REPORT}`。
`missing_review_evidence()` 返回缺失项 —— 缺 diff 或缺测试报告的审核不构成一次有效审核。

## 7. 源码审计规则（`SOURCE_RULES`，由测试执行）

| ID | 规则 | 级别 |
|----|------|------|
| RC-1 | Agent adapter 不得写 SOP 状态字段（`AgentStatus` 是进程存活态，豁免） | blocker |
| RC-2 | Agent adapter 不得宣告任务完成 | blocker |
| RC-3 | Agent adapter 不得 import workflow engine（`SubagentRunner` 是无状态 ABC，豁免） | blocker |
| RC-4 | Agent adapter 不得自我验证完成声明 | major |
| RC-5 | `workflow/` 之外不得把任务推进到终态 | blocker |
| RC-6 | HTTP 层不得把 Agent 终态事件翻译成任务终态 | blocker |

**契约债务登记**：`role_contract.KNOWN_VIOLATIONS`（13 项，每条带 `routed_to`）。
测试双向卡死 —— 新增违规会 fail，已修复却不摘登记也会 fail。

## 8. 禁止事项（违反即回滚）

- ❌ 交换 Claude 与 Codex 的职责
- ❌ 引入 Hive / Swarm / 自主认领任务
- ❌ Agent 自由 P2P 派活
- ❌ 为某个实现方便重新设计全部角色
- ❌ 在 role contract 会话里开发 WebUI
- ❌ 绕过 `apply_status()` 直接写 `X.status = `

## 9. 越权自查三问（新增代码前）

1. 这段在写状态吗？→ 必须在 `WorkflowEngine` 内并走 `self._set_status()`。
2. 这段在决定「下一步去哪」吗？→ 必须查 `REVIEW_ROUTING`，不得自己 if/else。
3. 这段在让某个 Agent 判断自己做得对不对吗？→ 错。判断权在 Codex，落地权在 Engine。
<!-- role-contract:end -->

<!-- gitnexus:start -->
