"""python -m adapters.openhands_acp / python adapters/openhands_acp/__main__.py

启动 SOP ACP Server（stdio JSON-RPC）。两种运行方式都支持：
- 从仓库根：python -m adapters.openhands_acp
- 直接脚本：python adapters/openhands_acp/__main__.py（自动把仓库根加入 sys.path）

Canvas Settings → Agent → Preset: Custom → Command（两个独立机器人 + 一个协作机器人）：
    Claude 机器人：/home/gnen/free-claude-code/.venv/bin/python -m adapters.openhands_acp --runtime claude
    Codex 机器人：/home/gnen/free-claude-code/.venv/bin/python -m adapters.openhands_acp --runtime codex
    协作机器人：  /home/gnen/free-claude-code/.venv/bin/python -m adapters.openhands_acp
                  （默认 all：一条消息 Claude+Codex 两路并行回答；/sop 走 SOP 引擎）

依赖说明：Claude 与 Codex 机器人都经 cc-switch 网关（127.0.0.1:15721），
cc-switch 上游不可用时两路同时降级；DSH 已从聊天机器人移除（SOP 流水线保留）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# 直接以脚本方式运行时，保证 adapters 包可导入（相对导入需要包上下文）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from acp import run_agent

from adapters.openhands_acp.sop_acp_agent import SopAcpAgent

RUNTIME_CHOICES = ("claude", "codex", "all")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="adapters.openhands_acp", description="WorkBuddy SOP/Chat ACP agent"
    )
    parser.add_argument(
        "--runtime",
        choices=RUNTIME_CHOICES,
        default=os.environ.get("WORKBENCH_CHAT_RUNTIME", "all"),
        help="绑定哪一个 runtime 回答；all = Claude+Codex 两路并行扇出（默认）",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    runtime_names: tuple[str, ...] | None = None
    if args.runtime != "all":
        runtime_names = (args.runtime,)
    asyncio.run(run_agent(SopAcpAgent(runtime_names=runtime_names)))


if __name__ == "__main__":
    main()
