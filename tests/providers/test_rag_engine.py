"""Tests for GitNexus RAG database lifecycle and query limits."""

import sqlite3

import pytest

from providers.common.rag_engine import (
    GitNexusRAG,
    build_knowledge_summary,
    inject_rag_context,
)


def _create_database(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE Symbol (
            uid TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            filePath TEXT NOT NULL,
            startLine INTEGER NOT NULL,
            endLine INTEGER NOT NULL
        );
        CREATE TABLE CodeRelation (
            fromId TEXT NOT NULL,
            toId TEXT NOT NULL,
            type TEXT NOT NULL
        );
        INSERT INTO Symbol VALUES
            ('one', 'Alpha', 'function', 'alpha.py', 1, 3),
            ('two', 'Beta', 'class', 'beta.py', 5, 8);
        """
    )
    connection.commit()
    connection.close()


def test_rag_reconnects_after_close(tmp_path) -> None:
    db_path = tmp_path / "graph.db"
    _create_database(db_path)
    rag = GitNexusRAG(str(db_path))

    assert rag.search_symbols("Alpha") == [
        {
            "name": "Alpha",
            "kind": "function",
            "filePath": "alpha.py",
            "startLine": 1,
            "endLine": 3,
        }
    ]
    rag.close()

    assert rag.get_related_code("beta.py") == [
        {"name": "Beta", "kind": "class", "startLine": 5, "endLine": 8}
    ]
    rag.close()
    assert "Symbol Types" in build_knowledge_summary(rag)


@pytest.mark.parametrize("limit", [-1, 1.5, True])
def test_rag_rejects_invalid_limits(tmp_path, limit) -> None:
    db_path = tmp_path / "graph.db"
    _create_database(db_path)
    rag = GitNexusRAG(str(db_path))

    with pytest.raises(ValueError, match="non-negative integer"):
        rag.search_symbols("Alpha", limit=limit)
    with pytest.raises(ValueError, match="non-negative integer"):
        rag.get_related_code("alpha.py", limit=limit)


def test_rag_zero_limit_returns_no_rows(tmp_path) -> None:
    db_path = tmp_path / "graph.db"
    _create_database(db_path)
    rag = GitNexusRAG(str(db_path))

    assert rag.search_symbols("Alpha", limit=0) == []
    assert rag.conn is not None


def test_inject_rag_context_rejects_negative_max_context(tmp_path) -> None:
    db_path = tmp_path / "graph.db"
    _create_database(db_path)
    rag = GitNexusRAG(str(db_path))

    with pytest.raises(ValueError, match="max_context"):
        inject_rag_context([{"role": "user", "content": "Alpha"}], rag, max_context=-1)
