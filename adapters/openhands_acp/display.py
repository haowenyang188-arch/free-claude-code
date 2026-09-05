"""artifact → Canvas 显示文本（与 SopConversationPanel 相同角色渲染规则）。

身份用文本标签表达（用户约束：不做多 Agent Profile）：
    [Claude · 方案]   → plan / claude
    [Codex · 审核]    → review_report / codex（审核者）
    [Codex · 执行]    → codex_executor（执行者，2026-09-06 新流水线）
    [DSH · 执行验证]  → 历史run兼容（旧 dsh 角色的 diff/test_report）
"""

from __future__ import annotations

import json


def role_label(artifact_type: str | None, role_id: str | None) -> str:
    t = (artifact_type or "").lower()
    r = (role_id or "").lower()
    if "codex_executor" in r or r == "executor":
        return "[Codex · 执行]"
    if t == "review_report" or "codex" in r or "review" in r:
        return "[Codex · 审核]"
    if t in ("plan",) or "claude" in r:
        return "[Claude · 方案]"
    if t in ("diff", "test_report", "implementation") or "dsh" in r or "deepseek" in r:
        return "[DSH · 执行验证]"
    return "[SOP Engine]"


def _content_text(raw: str) -> str:
    """review_report 契约 JSON → 人类可读；其它原样。"""
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(data, dict) or "result" not in data:
        return raw
    result = data.get("result", "")
    lines = [f"结果: {result}"]
    for key in ("blocking", "non_blocking", "evidence"):
        val = data.get(key)
        if isinstance(val, list) and val:
            lines.append(f"  {key}:")
            lines.extend(f"    - {x}" for x in val if isinstance(x, str))
    return "\n".join(lines)


def artifact_to_text(artifact_type: str | None, role_id: str | None, content: str) -> str:
    """渲染为带身份标签的显示文本（推送前截断长度保护 Canvas 渲染）。"""
    label = role_label(artifact_type, role_id)
    body = _content_text(content or "")
    text = f"{label}\n{body}"
    # 单条消息限长 4000 字符，防 Canvas 单消息过载
    return text[:4000]
