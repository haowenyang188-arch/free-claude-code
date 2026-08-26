"""Tests for memory integration utilities."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

import providers.common.memory_integration as memory_integration
from providers.common.memory_integration import (
    ConversationMemory,
    MemoryClient,
    extract_entities_from_messages,
    inject_memory_into_system,
)


class _FakeStdio:
    def __init__(self, events: list[str]):
        self.events = events

    async def __aenter__(self):
        self.events.append("stdio_enter")
        return "read", "write"

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.events.append("stdio_exit")


class _FakeSession:
    instances: ClassVar[list[_FakeSession]] = []

    def __init__(self, read, write):
        self.events: list[str] = []
        self.initialized = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_initialize = False
        self.__class__.instances.append(self)

    async def __aenter__(self):
        self.events.append("session_enter")
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.closed = True
        self.events.append("session_exit")

    async def initialize(self):
        self.events.append("initialize")
        if self.fail_initialize:
            raise RuntimeError("initialization failed")
        self.initialized = True

    async def call_tool(self, name: str, arguments: dict[str, object]):
        self.calls.append((name, arguments))
        if name in {"search_nodes", "open_nodes"}:
            return {"entities": [{"name": "Alice"}], "relations": []}
        return {"entities": arguments.get("entities", [])}


@pytest.fixture
def fake_mcp(monkeypatch):
    """Install tracked MCP context managers without starting a subprocess."""
    events: list[str] = []
    _FakeSession.instances.clear()
    params: list[object] = []

    class FakeParameters:
        def __init__(self, **kwargs):
            params.append(self)
            self.__dict__.update(kwargs)

    monkeypatch.setattr(memory_integration, "MCP_AVAILABLE", True)
    monkeypatch.setattr(memory_integration, "StdioServerParameters", FakeParameters)
    monkeypatch.setattr(memory_integration, "ClientSession", _FakeSession)
    monkeypatch.setattr(
        memory_integration,
        "stdio_client",
        lambda server_params: _FakeStdio(events),
    )
    return events, params


class TestConversationMemory:
    """Test ConversationMemory class."""

    def test_add_entity(self):
        memory = ConversationMemory()
        memory.add_entity("user_profile", "entity", ["User authentication system"])

        assert "user_profile" in memory.entities
        assert memory.entities["user_profile"]["entityType"] == "entity"

    def test_add_relation(self):
        memory = ConversationMemory()
        memory.add_relation("module_a", "module_b", "depends_on")

        assert len(memory.relations) == 1
        assert memory.relations[0]["from"] == "module_a"
        assert memory.relations[0]["to"] == "module_b"

    def test_add_observation(self):
        memory = ConversationMemory()
        memory.add_entity("feature_x", "feature")
        memory.add_observation("feature_x", "Implemented in Sprint 1")

        assert "feature_x" in memory.observations
        assert len(memory.observations["feature_x"]) == 1

    def test_to_dict_and_from_dict(self):
        memory = ConversationMemory()
        memory.add_entity("test", "type", ["obs1"])
        memory.add_relation("a", "b", "rel")

        data = memory.to_dict()
        assert data == {
            "entities": [
                {"name": "test", "entityType": "type", "observations": ["obs1"]}
            ],
            "relations": [{"from": "a", "to": "b", "relationType": "rel"}],
        }
        assert "observations" not in data

        new_memory = ConversationMemory()
        new_memory.from_dict(data)
        assert "test" in new_memory.entities
        assert new_memory.entities["test"]["observations"] == ["obs1"]

    def test_to_dict_uses_server_entity_schema_without_observations_sidecar(self):
        memory = ConversationMemory()
        memory.add_entity("feature", "concept")
        memory.add_observation("feature", "Useful fact")

        assert memory.to_dict() == {
            "entities": [
                {
                    "name": "feature",
                    "entityType": "concept",
                    "observations": ["Useful fact"],
                }
            ],
            "relations": [],
        }


class TestExtractEntitiesFromMessages:
    """Test entity extraction from messages."""

    def test_extract_files(self):
        messages = [
            {"role": "user", "content": "Check the file src/main.py for errors"}
        ]
        memory = extract_entities_from_messages(messages)

        assert len(memory.entities) > 0
        # Should extract file path
        assert any("src/main.py" in e for e in memory.entities)

    def test_extract_tasks(self):
        messages = [
            {"role": "user", "content": "We need to implement the login feature"}
        ]
        memory = extract_entities_from_messages(messages)

        # Should extract task-related keywords
        assert any(
            "task" in e.lower() or "implement" in e.lower() for e in memory.entities
        )

    def test_ignore_assistant_messages(self):
        messages = [
            {"role": "assistant", "content": "Check src/test.py"},
            {"role": "user", "content": "Look at src/real.py"},
        ]
        memory = extract_entities_from_messages(messages)

        # Should only extract from user messages
        entities_str = " ".join(memory.entities.keys())
        assert "real.py" in entities_str or "src/real.py" in entities_str


class TestInjectMemoryIntoSystem:
    """Test memory injection into system prompt."""

    def test_inject_empty_memory(self):
        system = "You are a helpful assistant."
        memory = ConversationMemory()

        result = inject_memory_into_system(system, memory)
        assert result == system  # No change

    def test_inject_entities(self):
        system = "You are a helpful assistant."
        memory = ConversationMemory()
        memory.add_entity("user_profile", "feature", ["Important feature"])

        result = inject_memory_into_system(system, memory)
        assert "Context from Previous Conversations" in result
        assert "user_profile" in result
        assert "feature" in result

    def test_inject_relations(self):
        system = "You are a helpful assistant."
        memory = ConversationMemory()
        memory.add_entity("module_a", "module")
        memory.add_entity("module_b", "module")
        memory.add_relation("module_a", "module_b", "depends_on")

        result = inject_memory_into_system(system, memory)
        assert "Relations:" in result
        assert "depends_on" in result


class TestMemoryClient:
    """Test MCP process/session lifecycle and tool payloads."""

    @pytest.mark.asyncio
    async def test_connect_keeps_session_open_for_calls_and_close_is_idempotent(
        self, fake_mcp
    ):
        events, params = fake_mcp
        client = MemoryClient(command="node", args=["dist/index.js"])

        await client.connect()
        assert events == ["stdio_enter"]
        assert client.session is _FakeSession.instances[0]

        entities = await client.search_nodes("Alice")
        opened = await client.open_nodes(["Alice"])
        assert entities == [{"name": "Alice"}]
        assert opened == [{"name": "Alice"}]
        assert _FakeSession.instances[0].calls == [
            ("search_nodes", {"query": "Alice"}),
            ("open_nodes", {"names": ["Alice"]}),
        ]
        assert params[0].command == "node"
        assert params[0].args == ["dist/index.js"]

        await client.close()
        await client.close()
        assert events[-1] == "stdio_exit"
        assert client.session is None
        with pytest.raises(RuntimeError, match="Not connected"):
            await client.search_nodes("Alice")

    @pytest.mark.asyncio
    async def test_async_context_manager_connects_and_closes(self, fake_mcp):
        events, _ = fake_mcp
        async with MemoryClient(command="node", args=["server.js"]) as client:
            assert client.session is not None
        assert events[-1] == "stdio_exit"

    @pytest.mark.asyncio
    async def test_initialize_failure_closes_all_entered_contexts(
        self, fake_mcp, monkeypatch
    ):
        events, _ = fake_mcp

        class FailingSession(_FakeSession):
            async def initialize(self):
                self.events.append("initialize")
                self.fail_initialize = True
                await super().initialize()

        monkeypatch.setattr(memory_integration, "ClientSession", FailingSession)

        client = MemoryClient(command="node", args=["server.js"])
        with pytest.raises(RuntimeError, match="initialization failed"):
            await client.connect()

        assert client.session is None
        assert events == ["stdio_enter", "stdio_exit"]
        assert FailingSession.instances[0].closed is True

    @pytest.mark.asyncio
    async def test_missing_default_build_is_diagnostic_and_does_not_start_process(
        self, fake_mcp, monkeypatch, tmp_path: Path
    ):
        monkeypatch.setattr(
            memory_integration, "DEFAULT_SERVER_PATH", tmp_path / "dist/index.js"
        )
        client = MemoryClient()

        with pytest.raises(RuntimeError, match=r"dist/index\.js.*build"):
            await client.connect()

        assert fake_mcp[1] == []

    @pytest.mark.asyncio
    async def test_explicit_missing_server_path_is_diagnostic(self, fake_mcp, tmp_path):
        client = MemoryClient(server_path=str(tmp_path / "index.js"))

        with pytest.raises(RuntimeError, match="does not exist"):
            await client.connect()

        assert fake_mcp[1] == []
