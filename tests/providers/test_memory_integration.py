"""Tests for memory integration utilities."""

import pytest

from providers.common.memory_integration import (
    ConversationMemory,
    extract_entities_from_messages,
    inject_memory_into_system,
)


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
        assert len(data["entities"]) == 1
        assert len(data["relations"]) == 1

        new_memory = ConversationMemory()
        new_memory.from_dict(data)
        assert "test" in new_memory.entities


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
