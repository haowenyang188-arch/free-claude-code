"""SOP Workbench × OpenHands Canvas —— ACP 适配层（Custom ACP Server）。

架构（用户批准，2026-09-03；聊天机器人 2026-09-06 调整为 Claude/Codex 双机器人）：
    Canvas（只做 UI）
      → OpenHands Agent Server（18000，spawn 本进程 over stdio JSON-RPC）
      → adapters/openhands_acp（本包，Custom ACP Server）
      → 聊天：ChatRouter 并行扇出（Claude / Codex，均经 cc-switch 网关）
      → SOP：Workbench SOP Engine（8000 HTTP，唯一流程控制者，
        Claude→DSH→Codex 流水线按原架构保留）
      → ACP session/update（agent_message，协作模式带文本标签）
      → Canvas 主对话区

原则：
- SOP Engine 仍是唯一流程控制者；runtime 不自主组网。
- 不伪造 Canvas MessageEvent（OpenHands 官方 Custom ACP 路线）。
- Agent 身份用 update_agent_message 文本前缀表达，不做多 Agent Profile。
- 现有 SopConversationPanel / SOP Engine / Canvas / agent-server 均不改。
"""

from __future__ import annotations

__version__ = "0.2.0"
