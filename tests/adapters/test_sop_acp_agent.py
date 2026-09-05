import pytest

from adapters.openhands_acp.sop_acp_agent import SopAcpAgent, _blocks_text


class FakeSop:
    def login(self) -> None:
        pass

    def register_definition(self) -> None:
        pass

    def start_run(self, _goal: str, *, metadata: dict | None = None) -> dict:
        return {"sop_run_id": "run-restored"}

    def get_run(self, _run_id: str) -> dict:
        return {"status": "completed"}

    def get_artifacts(self, _run_id: str) -> list[dict]:
        return []


class Connection:
    def __init__(self) -> None:
        self.updates = []

    async def session_update(self, _session_id, update) -> None:
        self.updates.append(update)


def test_blocks_text_excludes_acp_context_suffix() -> None:
    blocks = [
        {"text": "小红书巧克力文案"},
        {
            "text": "<CUSTOM_SECRETS>\n\n### Credential Access\n"
            "Automatic secret injection"
        },
    ]

    assert _blocks_text(blocks) == "小红书巧克力文案"


def test_blocks_text_keeps_multiple_user_text_blocks_before_suffix() -> None:
    blocks = [
        {"text": "第一段"},
        {"text": "第二段"},
        {"text": "<REPO_CONTEXT>\nrepository instructions"},
    ]

    assert _blocks_text(blocks) == "第一段\n第二段"


@pytest.mark.asyncio
async def test_load_session_restores_state_after_acp_process_restart() -> None:
    agent = SopAcpAgent(sop=FakeSop())
    connection = Connection()
    agent.on_connect(connection)

    response = await agent.initialize(protocol_version=1)
    assert response.agent_capabilities.load_session is True

    await agent.load_session(cwd="/tmp", session_id="persisted-session")
    result = await agent.prompt(
        prompt=[{"text": "/sop 继续这个会话"}],
        session_id="persisted-session",
    )

    assert result.stop_reason == "end_turn"
    assert "继续这个会话" in "\n".join(
        update.content.text for update in connection.updates
    )


@pytest.mark.asyncio
async def test_push_separates_messages_within_one_acp_turn() -> None:
    class Connection:
        def __init__(self) -> None:
            self.updates = []

        async def session_update(self, _session_id, update) -> None:
            self.updates.append(update)

    agent = SopAcpAgent()
    connection = Connection()
    agent.on_connect(connection)
    session = await agent.new_session(cwd="/tmp")
    session_id = session.session_id

    await agent._push(session_id, "[SOP Engine] started")
    await agent._push(session_id, "[Claude · 方案]\nplan")

    assert connection.updates[0].content.text == "[SOP Engine] started"
    assert connection.updates[1].content.text == "\n\n[Claude · 方案]\nplan"
