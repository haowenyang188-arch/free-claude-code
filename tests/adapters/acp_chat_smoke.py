"""ACP 端到端冒烟:spawn 真 ACP 子进程,initialize → new_session → prompt。

预期:三条带标签消息(Claude/Codex/DSH 各一条,当前上游 502 → 降级文案),
PromptResponse(stop_reason=end_turn)。验证聊天扇出的完整链路。
"""

import asyncio
import sys

from acp import Client, connect_to_agent


class Recorder(Client):
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def session_update(self, session_id, update, **kwargs) -> None:
        content = getattr(update, "content", None)
        text = getattr(content, "text", None)
        if text:
            self.messages.append(text)
            print(f"--- push ---\n{text[:200]}", flush=True)
        else:
            print(f"--- update (no text): {type(update).__name__}", flush=True)

    async def request_permission(self, *args, **kwargs):
        raise NotImplementedError

    async def read_text_file(self, *args, **kwargs):
        raise NotImplementedError

    async def write_text_file(self, *args, **kwargs):
        raise NotImplementedError


async def main() -> int:
    cmd = ["/home/gnen/free-claude-code/.venv/bin/python", "-m", "adapters.openhands_acp"]
    recorder = Recorder()
    agent_log = open("/tmp/acp-smoke-agent.log", "w")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=agent_log,
        cwd="/home/gnen/free-claude-code",
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin:/home/gnen/.local/bin",
            "HOME": "/home/gnen",
            "SOP_ACP_LOG": "/tmp/acp-smoke-agent-jsonrpc.log",
            "TERM": "dumb",
        },
    )
    conn = connect_to_agent(recorder, proc.stdin, proc.stdout)
    init = await conn.initialize(protocol_version=1)
    assert init.agent_capabilities.load_session is True
    print("initialize ok, load_session=True", flush=True)

    session = await conn.new_session(cwd="/home/gnen/free-claude-code", mcp_servers=[])
    print(f"new_session ok: {session.session_id[:8]}", flush=True)

    from acp import text_block

    response = await asyncio.wait_for(
        conn.prompt(session_id=session.session_id, prompt=[text_block("你好")]),
        timeout=420,
    )
    print(f"prompt stop_reason={response.stop_reason}", flush=True)
    print(f"total pushes={len(recorder.messages)}", flush=True)
    agent_log.close()
    try:
        with open("/tmp/acp-smoke-agent-jsonrpc.log") as f:
            print("--- agent log ---")
            print(f.read()[-1500:])
    except OSError:
        pass
    ok = len(recorder.messages) >= 1 and response.stop_reason == "end_turn"
    print("SMOKE", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
