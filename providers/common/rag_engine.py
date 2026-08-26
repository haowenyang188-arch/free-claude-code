"""RAG (Retrieval-Augmented Generation) integration using GitNexus.

Provides semantic code search and context injection for enhanced reasoning.
"""

import sqlite3
from pathlib import Path
from typing import Any


class GitNexusRAG:
    """RAG engine using GitNexus code knowledge graph."""

    def __init__(self, db_path: str | None = None):
        """Initialize GitNexus RAG.

        Args:
            db_path: Path to GitNexus database
                    (defaults to .gitnexus/gitnexus.db)
        """
        if db_path is None:
            db_path = ".gitnexus/gitnexus.db"

        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        """Connect to GitNexus database."""
        if not Path(self.db_path).exists():
            raise FileNotFoundError(
                f"GitNexus database not found at {self.db_path}. "
                "Run 'gitnexus analyze' first."
            )

        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def _connection(self) -> sqlite3.Connection:
        if self.conn is None:
            self.connect()
        assert self.conn is not None
        return self.conn

    @staticmethod
    def _validate_limit(limit: int, name: str = "limit") -> int:
        """Validate a SQL result limit before it reaches a query."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return limit

    def search_symbols(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search for code symbols.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching symbols with metadata
        """
        limit = self._validate_limit(limit)
        conn = self._connection()

        # Simple text search on symbol names
        cursor = conn.execute(
            """
            SELECT name, kind, filePath, startLine, endLine
            FROM Symbol
            WHERE name LIKE ? OR filePath LIKE ?
            LIMIT ?
            """,
            (f"%{query}%", f"%{query}%", limit),
        )

        return [dict(row) for row in cursor.fetchall()]

    def get_symbol_context(self, symbol_name: str) -> dict[str, Any]:
        """Get full context for a symbol including dependencies.

        Args:
            symbol_name: Symbol name to look up

        Returns:
            Symbol context with callers and callees
        """
        conn = self._connection()

        # Get symbol details
        cursor = conn.execute(
            """
            SELECT uid, name, kind, filePath, startLine, endLine
            FROM Symbol
            WHERE name = ?
            LIMIT 1
            """,
            (symbol_name,),
        )

        row = cursor.fetchone()
        if not row:
            return {"error": f"Symbol {symbol_name} not found"}

        symbol = dict(row)

        # Get callers (who calls this symbol)
        cursor = conn.execute(
            """
            SELECT DISTINCT s.name, s.kind, s.filePath
            FROM CodeRelation r
            JOIN Symbol s ON r.fromId = s.uid
            WHERE r.toId = ? AND r.type = 'CALLS'
            LIMIT 10
            """,
            (symbol["uid"],),
        )
        symbol["callers"] = [dict(row) for row in cursor.fetchall()]

        # Get callees (what this symbol calls)
        cursor = conn.execute(
            """
            SELECT DISTINCT s.name, s.kind, s.filePath
            FROM CodeRelation r
            JOIN Symbol s ON r.toId = s.uid
            WHERE r.fromId = ? AND r.type = 'CALLS'
            LIMIT 10
            """,
            (symbol["uid"],),
        )
        symbol["callees"] = [dict(row) for row in cursor.fetchall()]

        return symbol

    def get_related_code(self, file_path: str, limit: int = 20) -> list[dict[str, Any]]:
        """Get code symbols related to a file.

        Args:
            file_path: File path to analyze
            limit: Maximum results

        Returns:
            List of related symbols
        """
        limit = self._validate_limit(limit)
        conn = self._connection()

        cursor = conn.execute(
            """
            SELECT name, kind, startLine, endLine
            FROM Symbol
            WHERE filePath = ?
            ORDER BY startLine
            LIMIT ?
            """,
            (file_path, limit),
        )

        return [dict(row) for row in cursor.fetchall()]

    def find_implementations(self, interface_name: str) -> list[dict[str, Any]]:
        """Find implementations of an interface.

        Args:
            interface_name: Interface name

        Returns:
            List of implementing classes
        """
        conn = self._connection()

        cursor = conn.execute(
            """
            SELECT DISTINCT s.name, s.kind, s.filePath
            FROM CodeRelation r
            JOIN Symbol s ON r.fromId = s.uid
            JOIN Symbol i ON r.toId = i.uid
            WHERE i.name = ? AND r.type IN ('IMPLEMENTS', 'EXTENDS')
            """,
            (interface_name,),
        )

        return [dict(row) for row in cursor.fetchall()]


def inject_rag_context(
    messages: list[dict[str, Any]],
    rag_engine: GitNexusRAG,
    max_context: int = 3,
) -> list[dict[str, Any]]:
    """Inject RAG context into conversation messages.

    Args:
        messages: Original messages
        rag_engine: RAG engine instance
        max_context: Maximum context items to inject

    Returns:
        Messages with injected context
    """
    max_context = GitNexusRAG._validate_limit(max_context, "max_context")
    if not messages or max_context == 0:
        return messages

    # Extract code references from user messages
    references = []
    for msg in messages:
        if msg.get("role") != "user":
            continue

        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        # Look for function/class names (simple heuristic)
        words = content.split()
        references.extend(
            word
            for word in words
            if word.replace("_", "").isalnum() and len(word) > 3 and not word.islower()
        )

    if not references:
        return messages

    # Retrieve context for references
    context_items = []
    for ref in references[:max_context]:
        try:
            context = rag_engine.get_symbol_context(ref)
            if "error" not in context:
                context_items.append(context)
        except Exception:
            continue

    if not context_items:
        return messages

    # Inject context as a system message
    context_msg = {
        "role": "system",
        "content": "## Code Context\n\n"
        + "\n".join(
            f"**{item['name']}** ({item['kind']}) in {item['filePath']}:\n"
            f"- {len(item.get('callers', []))} callers, "
            f"{len(item.get('callees', []))} callees"
            for item in context_items
        ),
    }

    # Insert after first user message
    result = [*messages[:1], context_msg, *messages[1:]]
    return result


def build_knowledge_summary(rag_engine: GitNexusRAG) -> str:
    """Build a summary of available knowledge in the codebase.

    Args:
        rag_engine: RAG engine instance

    Returns:
        Markdown summary of codebase structure
    """
    conn = rag_engine._connection()

    summary = "# Codebase Knowledge Summary\n\n"

    # Count symbols by type
    cursor = conn.execute(
        """
        SELECT kind, COUNT(*) as count
        FROM Symbol
        GROUP BY kind
        ORDER BY count DESC
        """
    )

    summary += "## Symbol Types:\n"
    for row in cursor.fetchall():
        summary += f"- {row[0]}: {row[1]}\n"

    # List main files
    cursor = conn.execute(
        """
        SELECT filePath, COUNT(*) as symbol_count
        FROM Symbol
        GROUP BY filePath
        ORDER BY symbol_count DESC
        LIMIT 10
        """
    )

    summary += "\n## Top Files by Symbol Count:\n"
    for row in cursor.fetchall():
        summary += f"- {row[0]}: {row[1]} symbols\n"

    return summary
