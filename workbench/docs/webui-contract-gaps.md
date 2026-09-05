# WebUI 数据契约缺口登记（Contract Gaps）

> P0 产出（2026-09-03）。原则：**前端类型/UI 需求发现的后端缺口，只在此登记，不在 P0 修改业务后端**。
> 状态列：`open`=待 P1 或后续处理；`blocked_by`=所依赖的其他缺口。
> 与本文件配套的契约类型见 `frontend/src/types/sop.ts`（8000 侧）与 `frontend/src/types/conversation.ts`（18000 侧）。

## G-1 · SOP run ↔ Canvas conversation 关联（implemented，P1-B）
- 需求：run 详情/会话状态卡要展示「这个 SOP run 关联哪个 Agent Canvas 会话」。
- ACP 适配器在创建 SOP run 时写入 `metadata.canvas_acp_session_id`；8000 的
  `SopRunStatusResponse.metadata` 原样返回，Workbench 用 agent-server 会话的
  `agent_state.acp_session_id` 做显式匹配并标记「当前 run」。
- Workbench 仍不靠目录名、时间或标题推断关联；未命中时明确显示未找到。

## G-2 · SOP run 无 cost / model / 执行统计（**部分落地 @ P1-A**，G-1 关联后完整）
- 现状：legacy `Run`（models.py）有 tokens_used/cost；**SOP 侧** SopRunStatusResponse 只有 step 计数与时间。
  cost/model 的权威在 18000 ConversationStats（usage_to_metrics.default.model_name/accumulated_cost）。
- P1-A 落地：会话状态卡（CanvasSessionsCard）已通过 G-7 search 转发消费 18000 摘要
  （model/cost/running；UI 降级规则见 `frontend/src/types/conversation.ts`）。
- G-1 已实现后，卡片优先展示当前 run 的匹配会话，其他会话作为最近会话补充。

## G-3 · POST /api/sop-runs 运行闭环问题（open，单独登记，不阻塞 P1-B PoC）
- 现象：既有记录显示服务层曾接 FakeSubagentRunner，真实 Agent 从未被调用（B1/B2）；
  RUNTIME_MODE 默认 fake（main.py:334）。`POST /api/sop-runs` 仅创建状态，需 `auto_execute=true`
  或 `/execute` 触发后台编排。
- 判定：**不**作为 P1-B Canvas Extension PoC 的前置阻塞（PoC 第一 Gate 只依赖扩展挂载 +
  GET 读取 8000 + 开发链稳定）。运行闭环问题由 SOP 主线会话处理。

## G-4 · sop-definitions 注册不持久化（open）
- 现状：POST /api/sop-definitions 只写 service registry（main.py:2204），重启丢失。
- 前端容忍：空态文案注明「定义需注册且当前不持久化」，不承诺列表常驻。

## G-5 · 审批无列表接口（open，审批收件箱依赖）
- 现状：只能 GET /api/approvals/{session_id}/{call_id}?command_hash=… 精确查询，无「待审批列表」。
- 前端容忍：P1 先做「当前 run 内审批中心 + run 角标」；列表接口落地后升级跨 run 收件箱。

## G-6 · StepRunResponse 无步骤序号（open）
- 现状：sop_models.StepRunResponse 无 order/sequence；前端只能从事件流还原
  （PipelinePanel orderFromEvents）。
- 前端容忍：保留事件流还原；后端补序号前不依赖。

## G-7 · 8000 无 conversations / canvas 转发端点（**implemented @ P1-A**）
- P1-A 已在 8000 新增独立挂载点 `/api/integrations/canvas/*`
  （`workbench/backend/integrations/canvas.py`：health、search、batch/detail、events/search、send message）。
- 启用：配置 `WORKBENCH_CANVAS_SESSION_API_KEY`（可选 `WORKBENCH_CANVAS_AGENT_BASE`，默认
  http://127.0.0.1:18000）；未配置时各端点返回 503 canvas_integration_disabled（可整体开关）。
- 需重启 8000 进程生效。边界：独立命名空间；会话读取只读，发送消息仅透传用户消息并强制继续运行，与冻结业务路由隔离。

## 已核对一致（P0 修正前置确认，非缺口）
- `SopDefinitionSummary`：旧前端类型含臆造字段 `goal_template`（后端无）→ **P0 已修正**（对齐 main.py:2183-2194）。
- `HandoffResponse`：旧前端类型仅 8 字段且注释错误声称「无 message_type」→ **P0 已补齐 19 字段**
  （对齐 main.py collaboration_handoff_view:672-692 / sop_models.py:92-116）。
- `StartSopRunRequest.auto_execute`：后端真实字段，前端此前未声明 → **P0 已补**（可选）。
- `ReviewStatus`：后端含 `policy_blocked`（5 值）→ **P0 已补 union**（ReviewEvidenceResponse.review_status）。
- 其余 SOP 契约（StepRunResponse/TaskResponse/ArtifactResponse/Attempt/Evidence/Chain/
  SopRunSummary/Control/Approval/Job/Auth/ArtifactContent）逐字段核对一致。
