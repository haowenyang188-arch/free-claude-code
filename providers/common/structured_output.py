"""Structured output utilities for enforcing response schemas.

Provides JSON schema validation and structured response parsing to ensure
model outputs conform to expected formats.
"""

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class StructuredOutputParser:
    """Parse and validate structured outputs against schemas."""

    @staticmethod
    def validate_json_schema(data: Any, schema: dict[str, Any]) -> bool:
        """Validate data against a JSON schema.

        Args:
            data: Data to validate
            schema: JSON schema

        Returns:
            True if valid, False otherwise
        """
        # Basic validation for common schema types
        schema_type = schema.get("type")

        if schema_type == "object":
            if not isinstance(data, dict):
                return False
            # Validate required properties
            required = schema.get("required", [])
            if not all(prop in data for prop in required):
                return False
            # Validate properties
            properties = schema.get("properties", {})
            for key, value in data.items():
                if (
                    key in properties
                    and not StructuredOutputParser.validate_json_schema(
                        value, properties[key]
                    )
                ):
                    return False
            return True

        elif schema_type == "array":
            if not isinstance(data, list):
                return False
            items_schema = schema.get("items", {})
            return all(
                StructuredOutputParser.validate_json_schema(item, items_schema)
                for item in data
            )

        elif schema_type == "string":
            return isinstance(data, str)

        elif schema_type == "number":
            return isinstance(data, (int, float))

        elif schema_type == "integer":
            return isinstance(data, int)

        elif schema_type == "boolean":
            return isinstance(data, bool)

        elif schema_type == "null":
            return data is None

        # If no type specified or unknown type, allow it
        return True

    @staticmethod
    def parse_json_output(text: str) -> dict[str, Any] | None:
        """Extract and parse JSON from text output.

        Handles markdown code blocks and plain JSON.

        Args:
            text: Text containing JSON

        Returns:
            Parsed JSON object or None if parsing fails
        """
        # Try parsing directly first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code block
        lines = text.split("\n")
        json_lines = []
        in_code_block = False

        for line in lines:
            if line.strip().startswith("```"):
                if in_code_block:
                    # End of code block
                    break
                else:
                    # Start of code block
                    in_code_block = True
                    continue
            if in_code_block:
                json_lines.append(line)

        if json_lines:
            try:
                return json.loads("\n".join(json_lines))
            except json.JSONDecodeError:
                pass

        # Try finding JSON object in text
        # Look for balanced braces
        for i, char in enumerate(text):
            if char == "{":
                depth = 0
                for j in range(i, len(text)):
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[i : j + 1])
                            except json.JSONDecodeError:
                                break
                break

        return None

    @staticmethod
    def parse_pydantic(text: str, model: type[T]) -> T | None:
        """Parse text into a Pydantic model.

        Args:
            text: Text containing JSON data
            model: Pydantic model class

        Returns:
            Validated model instance or None if parsing/validation fails
        """
        data = StructuredOutputParser.parse_json_output(text)
        if data is None:
            return None

        try:
            return model.model_validate(data)
        except ValidationError:
            return None


class ResponseFormatter:
    """Format responses to match expected structured outputs."""

    @staticmethod
    def to_json_string(data: Any, indent: int = 2) -> str:
        """Convert data to formatted JSON string.

        Args:
            data: Data to convert
            indent: Indentation spaces

        Returns:
            Formatted JSON string
        """
        return json.dumps(data, indent=indent, ensure_ascii=False)

    @staticmethod
    def wrap_in_markdown(json_str: str, language: str = "json") -> str:
        """Wrap JSON in markdown code block.

        Args:
            json_str: JSON string
            language: Code block language

        Returns:
            Markdown formatted string
        """
        return f"```{language}\n{json_str}\n```"


def create_tool_for_schema(
    name: str, description: str, schema: dict[str, Any]
) -> dict[str, Any]:
    """Create a tool definition that enforces a structured output schema.

    This can be used with tool_choice to force models to output structured data.

    Args:
        name: Tool name
        description: Tool description
        schema: JSON schema for the output

    Returns:
        Tool definition in OpenAI format
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def enforce_structured_output(
    response_text: str,
    schema: dict[str, Any],
    repair_attempts: int = 1,
) -> dict[str, Any] | None:
    """Enforce structured output by parsing and validating.

    Args:
        response_text: Model response text
        schema: Expected JSON schema
        repair_attempts: Number of attempts to repair invalid JSON

    Returns:
        Validated data or None if all attempts fail
    """
    data = StructuredOutputParser.parse_json_output(response_text)

    if data is None:
        return None

    # If validation fails and we have repair attempts, try to fix common issues
    if (
        repair_attempts > 0
        and schema.get("type") == "object"
        and isinstance(data, dict)
    ):
        # Attempt 1: Remove extra fields not in schema
        properties = schema.get("properties", {})
        cleaned_data = {k: v for k, v in data.items() if k in properties}
        if StructuredOutputParser.validate_json_schema(cleaned_data, schema):
            return cleaned_data

    # Validate against schema
    if StructuredOutputParser.validate_json_schema(data, schema):
        return data

    return None


class StructuredOutputBuilder:
    """Builder for creating structured output requests."""

    def __init__(self):
        """Initialize builder."""
        self.schema: dict[str, Any] | None = None
        self.tool_name: str = "structured_output"
        self.tool_description: str = "Generate structured output"

    def with_schema(self, schema: dict[str, Any]) -> StructuredOutputBuilder:
        """Set the output schema.

        Args:
            schema: JSON schema

        Returns:
            Self for chaining
        """
        self.schema = schema
        return self

    def with_tool_name(self, name: str) -> StructuredOutputBuilder:
        """Set the tool name.

        Args:
            name: Tool name

        Returns:
            Self for chaining
        """
        self.tool_name = name
        return self

    def with_description(self, description: str) -> StructuredOutputBuilder:
        """Set the tool description.

        Args:
            description: Description

        Returns:
            Self for chaining
        """
        self.tool_description = description
        return self

    def build(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Build tools and tool_choice for structured output.

        Returns:
            Tuple of (tools, tool_choice)
        """
        if not self.schema:
            raise ValueError("Schema must be set before building")

        tool = create_tool_for_schema(
            self.tool_name, self.tool_description, self.schema
        )

        tools = [tool]
        tool_choice = {"type": "function", "function": {"name": self.tool_name}}

        return tools, tool_choice
