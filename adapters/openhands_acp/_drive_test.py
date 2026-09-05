"""Drive the SOP ACP agent over stdio: initialize → new_session → prompt.

Collects session/update notifications and prints them, verifying the
protocol chain (what OpenHands Agent Server will do when Canvas talks to us).
"""
import asyncio
import sys
from pathlib import Path

from acp import spawn_agent_process
from acp.interfaces import Client
from acp.schema import (
    InitializeRequest,
    NewSessionRequest,
    PromptRequest,
    TextContentBlock,
)


class Sink(Client):
    def session_update(self, session_id, update, **kwargs):
        parts = []
        def walk(b):
            if isinstance(b, str): parts.append(b)
            elif isinstance(b, dict):
                if b.get("text"): parts.append(str(b["text"]))
            else:
                v = getattr(b, "text", None)
                if v: parts.append(str(v))
        # update 是 SessionNotification(update=AgentMessageChunk(content=[...]))
        chunk = update
        if hasattr(update, "update"):
            chunk = update.update
        blocks = getattr(chunk, "content", None)
        for b in (blocks or []):
            walk(b)
        if not parts:
            parts.append(repr(update)[:200])
        print(f"[session_update {session_id[:8]}] " + "\n".join(parts)[:1500], flush=True)

    def on_connect(self, conn, **kwargs):
        pass


async def main() -> None:
    script = Path("/home/gnen/free-claude-code/adapters/openhands_acp/__main__.py")
    py = "/home/gnen/free-claude-code/.venv/bin/python"
    async with spawn_agent_process(Sink(), py, str(script)) as (conn, _proc):
        init = await conn.initialize(InitializeRequest(protocolVersion=1))
        print("init ok:", init.protocolVersion, flush=True)
        sess = await conn.newSession(
            NewSessionRequest(cwd="/home/gnen/free-claude-code", mcpServers=[])
        )
        print("session:", sess.sessionId, flush=True)
        goal = sys.argv[1] if len(sys.argv) > 1 else "创建一个 hello.txt，内容 hello-sop"
        resp = await conn.prompt(
            PromptRequest(
                sessionId=sess.sessionId,
                prompt=[TextContentBlock(type="text", text=goal)],
            )
        )
        print("RESP:", resp.model_dump() if hasattr(resp, "model_dump") else resp, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
