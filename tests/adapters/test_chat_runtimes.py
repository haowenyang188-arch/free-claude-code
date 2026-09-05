"""chat_runtimes 纯逻辑单元测试:argv 组装 / stream-json 解析 / codex 事件。

不 spawn 真实 CLI;子进程路径由 fake 进程流验证解析逻辑。
"""

import asyncio

import pytest

from adapters.openhands_acp.chat_runtimes import (
    ChatRuntimeError,
    build_chat_argv,
    codex_chat_once,
)
from workbench.backend.agents.claude_runner import (
    ClaudeMalformedOutput,
    ClaudeNonZeroExit,
    parse_claude_stream_json,
)


def _claude_stream(session_id: str = "s-1") -> str:
    return "\n".join(
        [
            '{"type":"system","subtype":"init","session_id":"'
            + session_id
            + '","tools":["Read"],"model":"claude-x"}',
            '{"type":"assistant","message":{"content":['
            '{"type":"text","text":"你好"}]}}',
            '{"type":"result","session_id":"'
            + session_id
            + '","is_error":false,"num_turns":1,"usage":{}}',
        ]
    )


def test_build_chat_argv_includes_system_prompt_and_resume() -> None:
    argv = build_chat_argv(
        prompt="你好",
        session_id="s-9",
        system_prompt="规范",
        claude_bin="claude",
    )
    assert argv[0] == "claude"
    assert "--safe-mode" in argv and "--strict-mcp-config" in argv
    assert "--tools=Read,Grep,Glob" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "规范"
    assert argv[argv.index("--resume") + 1] == "s-9"
    assert argv[-1] == "你好"

    fresh = build_chat_argv(prompt="hi", system_prompt=None, claude_bin="c")
    assert "--resume" not in fresh
    assert "--append-system-prompt" not in fresh


def test_parse_claude_stream_json_extracts_text_and_session() -> None:
    result = parse_claude_stream_json(
        stdout=_claude_stream("s-42"), stderr="", returncode=0
    )
    assert result.text == "你好"
    assert result.session_id == "s-42"
    assert result.is_error is False


def test_parse_claude_stream_json_fail_closed_paths() -> None:
    with pytest.raises(ClaudeNonZeroExit):
        parse_claude_stream_json(stdout="", stderr="boom", returncode=1)
    with pytest.raises(ClaudeMalformedOutput):
        parse_claude_stream_json(stdout="{}", stderr="", returncode=0)


@pytest.mark.asyncio
async def test_codex_chat_once_collects_item_completed_messages(
    monkeypatch, tmp_path
) -> None:
    jsonl = "\n".join(
        [
            '{"type":"thread.started","thread_id":"th-7"}',
            '{"type":"item.started","item":{"type":"agent_message","text":"半"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"完整回答"}}',
            '{"type":"turn.completed"}',
        ]
    )

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return jsonl.encode(), b""

        def kill(self):
            pass

        async def wait(self):
            return 0

    class FakeWrapper:
        def __init__(self, workspace, **kwargs):
            assert kwargs["isolation_mode"] == "inherit"
            assert kwargs["sandbox_mode"] == "read-only"

        def build_command(self, prompt, *, session_id=None):
            argv = ["codex", "exec"]
            if session_id:
                argv += ["resume", session_id]
            argv.append(prompt)
            return argv

    import adapters.openhands_acp.chat_runtimes as cr

    monkeypatch.setattr(cr, "CodexSessionWrapper", FakeWrapper)

    async def fake_exec(*argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    answer, thread_id = await codex_chat_once(
        prompt="问", cwd=str(tmp_path), session_id=None, timeout=10
    )
    assert answer == "完整回答"  # started 事件不重复计入
    assert thread_id == "th-7"


@pytest.mark.asyncio
async def test_codex_chat_once_error_record_fails_closed(monkeypatch, tmp_path) -> None:
    jsonl = '{"type":"error","error":{"message":"gateway 502"}}'

    class FakeProc:
        returncode = 1

        async def communicate(self):
            return jsonl.encode(), b""

        def kill(self):
            pass

        async def wait(self):
            return 1

    class FakeWrapper:
        def __init__(self, workspace, **kwargs):
            pass

        def build_command(self, prompt, *, session_id=None):
            return ["codex", "exec", prompt]

    import adapters.openhands_acp.chat_runtimes as cr

    monkeypatch.setattr(cr, "CodexSessionWrapper", FakeWrapper)

    async def fake_exec(*argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ChatRuntimeError, match="502"):
        await codex_chat_once(prompt="问", cwd=str(tmp_path), timeout=10)
