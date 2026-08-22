"""Tests for structured output utilities."""

import pytest
from pydantic import BaseModel

from providers.common.structured_output import (
    ResponseFormatter,
    StructuredOutputBuilder,
    StructuredOutputParser,
    create_tool_for_schema,
    enforce_structured_output,
)


class Person(BaseModel):
    """Test Pydantic model."""

    name: str
    age: int
    email: str | None = None


class TestStructuredOutputParser:
    """Test structured output parsing."""

    def test_validate_json_schema_object(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }
        data = {"name": "Alice", "age": 30}
        assert StructuredOutputParser.validate_json_schema(data, schema)

    def test_validate_json_schema_missing_required(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        data = {"age": 30}
        assert not StructuredOutputParser.validate_json_schema(data, schema)

    def test_validate_json_schema_array(self):
        schema = {"type": "array", "items": {"type": "string"}}
        data = ["a", "b", "c"]
        assert StructuredOutputParser.validate_json_schema(data, schema)

    def test_validate_json_schema_primitives(self):
        assert StructuredOutputParser.validate_json_schema("text", {"type": "string"})
        assert StructuredOutputParser.validate_json_schema(42, {"type": "integer"})
        assert StructuredOutputParser.validate_json_schema(3.14, {"type": "number"})
        assert StructuredOutputParser.validate_json_schema(True, {"type": "boolean"})
        assert StructuredOutputParser.validate_json_schema(None, {"type": "null"})

    def test_parse_json_output_plain_json(self):
        text = '{"name": "Alice", "age": 30}'
        result = StructuredOutputParser.parse_json_output(text)
        assert result == {"name": "Alice", "age": 30}

    def test_parse_json_output_markdown_block(self):
        text = """
Here is the data:
```json
{"name": "Bob", "age": 25}
```
"""
        result = StructuredOutputParser.parse_json_output(text)
        assert result == {"name": "Bob", "age": 25}

    def test_parse_json_output_embedded_json(self):
        text = 'The result is {"status": "ok", "value": 42} as shown.'
        result = StructuredOutputParser.parse_json_output(text)
        assert result == {"status": "ok", "value": 42}

    def test_parse_json_output_invalid(self):
        text = "This is not JSON at all"
        result = StructuredOutputParser.parse_json_output(text)
        assert result is None

    def test_parse_pydantic_valid(self):
        text = '{"name": "Charlie", "age": 35, "email": "charlie@example.com"}'
        result = StructuredOutputParser.parse_pydantic(text, Person)
        assert isinstance(result, Person)
        assert result.name == "Charlie"
        assert result.age == 35

    def test_parse_pydantic_invalid(self):
        text = '{"name": "Invalid", "age": "not_a_number"}'
        result = StructuredOutputParser.parse_pydantic(text, Person)
        assert result is None

    def test_parse_pydantic_optional_fields(self):
        text = '{"name": "Dave", "age": 40}'
        result = StructuredOutputParser.parse_pydantic(text, Person)
        assert isinstance(result, Person)
        assert result.email is None


class TestResponseFormatter:
    """Test response formatting."""

    def test_to_json_string(self):
        data = {"name": "Alice", "age": 30}
        result = ResponseFormatter.to_json_string(data)
        assert "Alice" in result
        assert "30" in result

    def test_wrap_in_markdown(self):
        json_str = '{"key": "value"}'
        result = ResponseFormatter.wrap_in_markdown(json_str)
        assert result.startswith("```json")
        assert result.endswith("```")
        assert json_str in result


class TestCreateToolForSchema:
    """Test tool creation for schemas."""

    def test_create_tool_for_schema(self):
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        tool = create_tool_for_schema("search", "Search the web", schema)

        assert tool["type"] == "function"
        assert tool["function"]["name"] == "search"
        assert tool["function"]["description"] == "Search the web"
        assert tool["function"]["parameters"] == schema


class TestEnforceStructuredOutput:
    """Test structured output enforcement."""

    def test_enforce_valid_output(self):
        text = '{"name": "Alice", "age": 30}'
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        result = enforce_structured_output(text, schema)
        assert result == {"name": "Alice", "age": 30}

    def test_enforce_invalid_output(self):
        text = '{"name": "Alice"}'
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        result = enforce_structured_output(text, schema)
        assert result is None

    def test_enforce_with_extra_fields(self):
        text = '{"name": "Alice", "age": 30, "extra": "field"}'
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name", "age"],
        }
        result = enforce_structured_output(text, schema, repair_attempts=1)
        # Should remove extra field
        assert result == {"name": "Alice", "age": 30}

    def test_enforce_unparseable(self):
        text = "Not JSON"
        schema = {"type": "object"}
        result = enforce_structured_output(text, schema)
        assert result is None


class TestStructuredOutputBuilder:
    """Test structured output builder."""

    def test_builder_basic(self):
        schema = {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
        }
        builder = StructuredOutputBuilder()
        tools, tool_choice = builder.with_schema(schema).build()

        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "structured_output"
        assert tool_choice["type"] == "function"

    def test_builder_custom_name(self):
        schema = {"type": "object", "properties": {}}
        builder = StructuredOutputBuilder()
        tools, tool_choice = (
            builder.with_schema(schema)
            .with_tool_name("custom_tool")
            .with_description("Custom description")
            .build()
        )

        assert tools[0]["function"]["name"] == "custom_tool"
        assert tools[0]["function"]["description"] == "Custom description"
        assert tool_choice["function"]["name"] == "custom_tool"

    def test_builder_missing_schema(self):
        builder = StructuredOutputBuilder()
        with pytest.raises(ValueError, match="Schema must be set"):
            builder.build()


@pytest.mark.parametrize(
    "text,should_parse",
    [
        ('{"valid": true}', True),
        ('```json\n{"valid": true}\n```', True),
        ("Invalid JSON text", False),
        ('Text before {"valid": true} text after', True),
    ],
)
def test_parse_json_output_parametrized(text, should_parse):
    """Parametrized test for JSON parsing."""
    result = StructuredOutputParser.parse_json_output(text)
    if should_parse:
        assert result is not None
        assert isinstance(result, dict)
    else:
        assert result is None
