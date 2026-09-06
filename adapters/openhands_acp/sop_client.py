"""Workbench 8000 HTTP 客户端：起 SOP run + 轮询 artifacts（无新依赖，urllib）。

env:
  WORKBENCH_API_BASE   默认 http://127.0.0.1:8000
  WORKBENCH_AUTH_TOKEN_FILE  默认 /run/user/1000/free-claude-code-workbench/auth.token
  SOP_DEFINITION_ID    默认 sop-console-3agent-demo
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import suppress

API_BASE = os.environ.get("WORKBENCH_API_BASE", "http://127.0.0.1:8000")
TOKEN_FILE = os.environ.get(
    "WORKBENCH_AUTH_TOKEN_FILE",
    "/run/user/1000/free-claude-code-workbench/auth.token",
)
SOP_DEFINITION_ID = os.environ.get(
    "SOP_DEFINITION_ID", "sop-console-claude-codex-v2"
)


class SopApiError(RuntimeError):
    pass


class SopClient:
    """最小 8000 客户端：login → ensure definition → start run → poll."""

    def __init__(self, api_base: str | None = None) -> None:
        self._base = (api_base or API_BASE).rstrip("/")
        self._jar = http.cookiejar.CookieJar()
        # 显式空 ProxyHandler:本客户端只访问本机 Workbench,绝不走
        # 环境代理(WSL 的 http_proxy=127.0.0.1:7890 会把回环请求送进
        # Clash 导致 502;urllib 的 no_proxy 通配写法不生效)。
        self._op = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self._jar),
        )

    # -- http helpers -------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if params:
            from urllib.parse import urlencode

            path = f"{path}?{urlencode(params)}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with self._op.open(req, timeout=10) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            raise SopApiError(f"HTTP {e.code} {path}: {detail}") from e

    # -- auth ---------------------------------------------------------------
    def login(self) -> None:
        if not os.path.exists(TOKEN_FILE):
            raise SopApiError(f"auth token file not found: {TOKEN_FILE}")
        with open(TOKEN_FILE, encoding="utf-8") as token_file:
            token = token_file.read().strip()
        self._request("POST", "/api/auth/login", {"token": token})

    # -- SOP runs -----------------------------------------------------------
    def register_definition(self) -> None:
        """注册 Claude/Codex 七段流水线（服务重启后内存清空，幂等）。

        用户定版流程（2026-09-06）：
            Claude 方案 → Codex 审核 → Claude 修改方案 → Codex 复审
            → Codex 输出结果 → 用户最终决策 → Codex 执行
        三个评审步骤（plan_review/re_review/final_decision）都会在审批台
        暂停等待用户批准/驳回；REWORK 按 review_route_targets 回流。
        """
        body = {
            "id": SOP_DEFINITION_ID,
            "name": "Console Claude-Codex pipeline v2",
            "version": 2,
            "metadata": {
                "review_route_targets": {
                    "plan_review": "revise",
                    "re_review": "revise",
                    "final_decision": "output",
                }
            },
            "stages": [
                {
                    "id": "s1",
                    "name": "main",
                    "steps": [
                        {
                            "id": "plan",
                            "name": "方案设计",
                            "role_id": "claude",
                            "output_type": "plan",
                            "handoff_to": "plan_review",
                            "instructions": (
                                "请用中文制定实现方案：目标、分步计划、涉及文件、"
                                "验收标准、主要风险。300 字以内，不要探索仓库。"
                            ),
                        },
                        {
                            "id": "plan_review",
                            "name": "方案审核",
                            "role_id": "codex",
                            "output_type": "review_report",
                            "depends_on": ["plan"],
                            "handoff_to": "revise",
                            "instructions": (
                                "审核上述方案的可行性、风险与遗漏。按契约输出 JSON："
                                '{"result":"PASS"|"REWORK","blocking":[..],"non_blocking":[..],"evidence":[..]}。'
                                "方案不可行或有实质遗漏必须 REWORK 并给出 blocking 清单。"
                            ),
                        },
                        {
                            "id": "revise",
                            "name": "修改方案",
                            "role_id": "claude",
                            "output_type": "plan",
                            "depends_on": ["plan_review"],
                            "handoff_to": "re_review",
                            "instructions": (
                                "根据审核意见逐条修订方案，输出完整修订版（不要只列差异），"
                                "并注明每条意见的处理方式。中文，400 字以内。"
                            ),
                        },
                        {
                            "id": "re_review",
                            "name": "方案复审",
                            "role_id": "codex",
                            "output_type": "review_report",
                            "depends_on": ["revise"],
                            "handoff_to": "output",
                            "instructions": (
                                "复审修订后的方案：确认每条 blocking 意见已解决。"
                                '按契约输出 JSON，result 为 "PASS" 或 "REWORK"。'
                            ),
                        },
                        {
                            "id": "output",
                            "name": "输出结果",
                            "role_id": "codex_executor",
                            "output_type": "implementation",
                            "depends_on": ["re_review"],
                            "handoff_to": "final_decision",
                            "instructions": (
                                "基于已批准的方案输出完整交付物：最终文件改动清单、"
                                "关键代码/内容、验证方式与预期结果。中文。"
                                "本阶段只产出交付物本身，不要执行写操作，等待用户决策。"
                            ),
                        },
                        {
                            "id": "final_decision",
                            "name": "用户最终决策",
                            "role_id": "codex",
                            "output_type": "review_report",
                            "depends_on": ["output"],
                            "handoff_to": "execute",
                            "instructions": (
                                "把交付物汇总为执行请求：交付物摘要 + 即将执行的变更清单 + "
                                "回滚方式。按契约输出 JSON（result 填 PASS，表示请求批准）。"
                                "用户将在审批台做最终决策：批准后进入执行，驳回则打回输出阶段。"
                            ),
                        },
                        {
                            "id": "execute",
                            "name": "执行",
                            "role_id": "codex_executor",
                            "output_type": "implementation",
                            "depends_on": ["final_decision"],
                            "instructions": (
                                "执行已批准的变更：按交付物落实文件修改并运行验证，"
                                "输出执行结果与验证证据。中文。只做已批准清单内的事。"
                            ),
                        },
                    ],
                }
            ],
        }
        self._request("POST", "/api/sop-definitions", body)

    def post_bot_reply(self, conversation_id: str, text: str) -> dict:
        """机器人回复落 Workbench 存档(持久化通道,见 canvas.py bot-replies)。"""
        return self._request(
            "POST",
            "/api/integrations/canvas/bot-replies",
            {"text": text, "role": "bot"},
            params={"conversation_id": conversation_id},
        )

    def start_run(self, goal: str, *, metadata: dict | None = None) -> dict:
        """起 run；goal 为用户在 Canvas 输入的任务文本。"""
        body = {
            "goal_description": goal,
            "sop_definition_id": SOP_DEFINITION_ID,
            "acceptance_criteria": [goal],
            "constraints": ["No paid operations", "Only touch the workspace directory"],
            "auto_execute": True,
        }
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", "/api/sop-runs", body)

    def get_run(self, run_id: str) -> dict:
        return self._request("GET", f"/api/sop-runs/{run_id}")

    def get_artifacts(self, run_id: str) -> list[dict]:
        raw = self._request("GET", f"/api/sop-runs/{run_id}/artifacts")
        return raw if isinstance(raw, list) else []

    def get_artifact_content(self, artifact_id: str) -> str:
        raw = self._request("GET", f"/api/artifacts/{artifact_id}/content")
        if isinstance(raw, dict):
            return raw.get("content") or ""
        return ""

    def cancel_run(self, run_id: str) -> None:
        with suppress(SopApiError):
            self._request("POST", f"/api/sop-runs/{run_id}/control", {"action": "cancel"})

    def poll_run(self, run_id: str, timeout: float = 600.0) -> tuple[str, list[dict]]:
        """轮询至 terminal。返回 (terminal_status, 新增 artifacts 由调用方处理)。

        实际流式逻辑在 agent 里（每条新 artifact 推送一次），这里只返回终态。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = self.get_run(run_id)
            status = run.get("status", "")
            if status in ("completed", "failed", "cancelled", "paused"):
                return status, run.get("metadata") or {}
            time.sleep(1.0)
        return "timeout", {}
