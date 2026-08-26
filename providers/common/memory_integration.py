"""Long-term memory integration for provider context enhancement.

Integrates the existing memory MCP server to provide persistent knowledge
across conversations.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

DEFAULT_SERVER_PATH = (
    Path(__file__).resolve().parents[2]
    / "servers"
    / "src"
    / "memory"
    / "dist"
    / "index.js"
)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    MCP_AVAILABLE = True
except ImportError:

    def _missing_mcp_dependency(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("MCP SDK not available. Install with: pip install mcp")

    ClientSession = _missing_mcp_dependency
    StdioServerParameters = _missing_mcp_dependency
    stdio_client = _missing_mcp_dependency
    MCP_AVAILABLE = False


class MemoryClient:
    """Client for interacting with the memory MCP server."""

    def __init__(
        self,
        server_path: str | Path | None = None,
        *,
        command: str | None = None,
        args: Sequence[str] | None = None,
    ):
        """Initialize memory client.

        Args:
            server_path: Path to a built memory server executable. The default
                        points to ``servers/src/memory/dist/index.js``.
            command: Explicit server command. When supplied, ``args`` are
                     passed unchanged and server path discovery is skipped.
            args: Arguments for an explicit ``command``.
        """
        if args is not None and command is None:
            raise ValueError("command is required when args are provided")

        self.server_path = Path(server_path) if server_path else DEFAULT_SERVER_PATH
        self.command = command
        self.args = list(args) if args is not None else None
        self.session: Any = None
        self._exit_stack: AsyncExitStack | None = None

    def _server_parameters(self) -> Any:
        """Build validated stdio parameters without installing dependencies."""
        if self.command is not None:
            command_args = self.args or []
            return StdioServerParameters(command=self.command, args=command_args)

        if not self.server_path.is_file():
            raise RuntimeError(
                f"Memory MCP server build does not exist at {self.server_path}. "
                "Run `npm run build --workspace @modelcontextprotocol/server-memory` "
                "before connecting; dependencies are not installed automatically."
            )
        if self.server_path.suffix != ".js":
            raise RuntimeError(
                f"Memory MCP server path {self.server_path} is not a JavaScript "
                "build. Use the built dist/index.js or pass an explicit command "
                "that can execute the source file."
            )

        return StdioServerParameters(command="node", args=[str(self.server_path)])

    async def connect(self) -> None:
        """Connect to memory server."""
        if not MCP_AVAILABLE:
            raise RuntimeError("MCP SDK not available. Install with: pip install mcp")

        if self._exit_stack is not None:
            return

        server_params = self._server_parameters()
        exit_stack = AsyncExitStack()
        await exit_stack.__aenter__()
        try:
            read, write = await exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            session = await exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            self.session = None
            self._exit_stack = None
            await exit_stack.aclose()
            raise

        self.session = session
        self._exit_stack = exit_stack

    async def close(self) -> None:
        """Close the MCP session and its stdio transport, if connected."""
        exit_stack, self._exit_stack = self._exit_stack, None
        self.session = None
        if exit_stack is not None:
            await exit_stack.aclose()

    async def __aenter__(self) -> MemoryClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    def _require_session(self) -> Any:
        if self.session is None or self._exit_stack is None:
            raise RuntimeError("Not connected to memory server")
        return self.session

    @staticmethod
    def _result_data(result: Any) -> dict[str, Any]:
        """Extract structured MCP output while accepting simple test doubles."""
        if isinstance(result, dict):
            structured = result.get("structuredContent")
            return structured if isinstance(structured, dict) else result

        structured = getattr(result, "structuredContent", None)
        return structured if isinstance(structured, dict) else {}

    async def create_entities(self, entities: list[dict[str, Any]]) -> dict[str, Any]:
        """Create entities in memory graph.

        Args:
            entities: List of entities with name, entityType, observations

        Returns:
            Response from memory server
        """
        session = self._require_session()

        result = await session.call_tool(
            "create_entities", arguments={"entities": entities}
        )
        return self._result_data(result)

    async def create_relations(self, relations: list[dict[str, str]]) -> dict[str, Any]:
        """Create relations in memory graph.

        Args:
            relations: List of relations with from, to, relationType

        Returns:
            Response from memory server
        """
        session = self._require_session()

        result = await session.call_tool(
            "create_relations", arguments={"relations": relations}
        )
        return self._result_data(result)

    async def search_nodes(
        self, query: str, node_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Search for nodes in memory graph.

        Args:
            query: Search query
            node_type: Optional node type filter

        Returns:
            List of matching nodes
        """
        session = self._require_session()

        args = {"query": query}

        result = self._result_data(
            await session.call_tool("search_nodes", arguments=args)
        )
        entities = result.get("entities", [])
        if node_type:
            entities = [
                entity for entity in entities if entity.get("entityType") == node_type
            ]
        return entities

    async def open_nodes(self, names: list[str]) -> list[dict[str, Any]]:
        """Open specific nodes by name.

        Args:
            names: List of node names

        Returns:
            List of node details
        """
        session = self._require_session()

        result = self._result_data(
            await session.call_tool("open_nodes", arguments={"names": names})
        )
        return result.get("entities", [])


class ConversationMemory:
    """Manage conversation memory for context enhancement."""

    def __init__(self):
        """Initialize conversation memory."""
        self.entities: dict[str, dict[str, Any]] = {}
        self.relations: list[dict[str, str]] = []
        self.observations: dict[str, list[str]] = {}

    def add_entity(
        self, name: str, entity_type: str, observations: list[str] | None = None
    ) -> None:
        """Add entity to memory.

        Args:
            name: Entity name
            entity_type: Entity type (e.g., "person", "concept", "task")
            observations: Optional observations about the entity
        """
        entity_observations = list(observations or [])
        self.entities[name] = {
            "name": name,
            "entityType": entity_type,
            "observations": entity_observations,
        }
        self.observations[name] = entity_observations

    def add_relation(
        self, from_entity: str, to_entity: str, relation_type: str
    ) -> None:
        """Add relation between entities.

        Args:
            from_entity: Source entity name
            to_entity: Target entity name
            relation_type: Relation type (e.g., "depends_on", "related_to")
        """
        self.relations.append(
            {"from": from_entity, "to": to_entity, "relationType": relation_type}
        )

    def add_observation(self, entity_name: str, observation: str) -> None:
        """Add observation to entity.

        Args:
            entity_name: Entity name
            observation: Observation text
        """
        if entity_name not in self.entities:
            self.observations.setdefault(entity_name, []).append(observation)
            return

        entity_observations = self.entities[entity_name].setdefault("observations", [])
        entity_observations.append(observation)
        self.observations[entity_name] = entity_observations

    def to_dict(self) -> dict[str, Any]:
        """Export memory to dictionary.

        Returns:
            Dictionary representation of memory
        """
        return {"entities": list(self.entities.values()), "relations": self.relations}

    def from_dict(self, data: dict[str, Any]) -> None:
        """Import memory from dictionary.

        Args:
            data: Dictionary representation of memory
        """
        observations = data.get("observations", {})
        self.entities = {}
        self.observations = {}
        for source_entity in data.get("entities", []):
            entity = dict(source_entity)
            name = entity["name"]
            entity_observations = list(
                entity.get("observations", observations.get(name, []))
            )
            entity["observations"] = entity_observations
            self.entities[name] = entity
            self.observations[name] = entity_observations
        self.relations = [dict(relation) for relation in data.get("relations", [])]


def extract_entities_from_messages(
    messages: list[dict[str, Any]],
) -> ConversationMemory:
    """Extract entities and relations from conversation messages.

    Args:
        messages: List of conversation messages

    Returns:
        ConversationMemory with extracted information
    """
    memory = ConversationMemory()

    # Simple entity extraction from user messages
    for msg in messages:
        if msg.get("role") != "user":
            continue

        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        # Extract mentioned files (simple heuristic)
        words = content.split()
        for word in words:
            if "/" in word or "\\" in word:
                memory.add_entity(word, "file", ["Mentioned in conversation"])

            # Extract task-related keywords
            if any(kw in word.lower() for kw in ["task", "todo", "fix", "implement"]):
                memory.add_entity(word, "task", [content[:100]])

    return memory


def inject_memory_into_system(system_prompt: str, memory: ConversationMemory) -> str:
    """Inject memory context into system prompt.

    Args:
        system_prompt: Original system prompt
        memory: Conversation memory to inject

    Returns:
        Enhanced system prompt with memory context
    """
    if not memory.entities and not memory.relations:
        return system_prompt

    memory_section = "\n\n## Context from Previous Conversations\n\n"

    if memory.entities:
        memory_section += "### Known Entities:\n"
        for entity in list(memory.entities.values())[:10]:  # Limit to 10
            name = entity["name"]
            entity_type = entity["entityType"]
            observations = memory.observations.get(name, [])
            memory_section += f"- **{name}** ({entity_type})"
            if observations:
                memory_section += f": {observations[0][:100]}"
            memory_section += "\n"

    if memory.relations:
        memory_section += "\n### Relations:\n"
        for rel in memory.relations[:5]:  # Limit to 5
            memory_section += f"- {rel['from']} {rel['relationType']} {rel['to']}\n"

    return system_prompt + memory_section
