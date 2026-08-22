"""Long-term memory integration for provider context enhancement.

Integrates the existing memory MCP server to provide persistent knowledge
across conversations.
"""

from typing import Any

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


class MemoryClient:
    """Client for interacting with the memory MCP server."""

    def __init__(self, server_path: str | None = None):
        """Initialize memory client.

        Args:
            server_path: Path to memory server executable
                        (defaults to servers/src/memory/index.ts)
        """
        self.server_path = server_path or "servers/src/memory/index.ts"
        self.session: Any = None

    async def connect(self) -> None:
        """Connect to memory server."""
        if not MCP_AVAILABLE:
            raise RuntimeError("MCP SDK not available. Install with: pip install mcp")

        server_params = StdioServerParameters(
            command="node",
            args=[self.server_path],
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                self.session = session
                await session.initialize()

    async def create_entities(self, entities: list[dict[str, Any]]) -> dict[str, Any]:
        """Create entities in memory graph.

        Args:
            entities: List of entities with name, entityType, observations

        Returns:
            Response from memory server
        """
        if not self.session:
            raise RuntimeError("Not connected to memory server")

        result = await self.session.call_tool(
            "create_entities", arguments={"entities": entities}
        )
        return result

    async def create_relations(self, relations: list[dict[str, str]]) -> dict[str, Any]:
        """Create relations in memory graph.

        Args:
            relations: List of relations with from, to, relationType

        Returns:
            Response from memory server
        """
        if not self.session:
            raise RuntimeError("Not connected to memory server")

        result = await self.session.call_tool(
            "create_relations", arguments={"relations": relations}
        )
        return result

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
        if not self.session:
            raise RuntimeError("Not connected to memory server")

        args = {"query": query}
        if node_type:
            args["nodeType"] = node_type

        result = await self.session.call_tool("search_nodes", arguments=args)
        return result.get("nodes", [])

    async def open_nodes(self, names: list[str]) -> list[dict[str, Any]]:
        """Open specific nodes by name.

        Args:
            names: List of node names

        Returns:
            List of node details
        """
        if not self.session:
            raise RuntimeError("Not connected to memory server")

        result = await self.session.call_tool("open_nodes", arguments={"names": names})
        return result.get("nodes", [])


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
        self.entities[name] = {"name": name, "entityType": entity_type}
        if observations:
            self.observations[name] = observations

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
        if entity_name not in self.observations:
            self.observations[entity_name] = []
        self.observations[entity_name].append(observation)

    def to_dict(self) -> dict[str, Any]:
        """Export memory to dictionary.

        Returns:
            Dictionary representation of memory
        """
        return {
            "entities": list(self.entities.values()),
            "relations": self.relations,
            "observations": self.observations,
        }

    def from_dict(self, data: dict[str, Any]) -> None:
        """Import memory from dictionary.

        Args:
            data: Dictionary representation of memory
        """
        self.entities = {e["name"]: e for e in data.get("entities", [])}
        self.relations = data.get("relations", [])
        self.observations = data.get("observations", {})


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
