from __future__ import annotations

import pytest


def test_resolve_workspace_root_canonicalizes_existing_directory(tmp_path):
    from cli.workspace import resolve_workspace_root

    workspace = tmp_path / "project"
    workspace.mkdir()
    link = tmp_path / "project-link"
    link.symlink_to(workspace, target_is_directory=True)

    assert resolve_workspace_root(str(link)) == workspace.resolve()


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_resolve_workspace_root_rejects_unusable_paths(tmp_path, kind):
    from cli.workspace import WorkspacePolicyError, resolve_workspace_root

    path = tmp_path / kind
    if kind == "file":
        path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(WorkspacePolicyError):
        resolve_workspace_root(str(path))


def test_resolve_workspace_root_can_create_only_the_final_directory(tmp_path):
    from cli.workspace import resolve_workspace_root

    workspace = tmp_path / "new-project"

    assert resolve_workspace_root(str(workspace), create=True) == workspace.resolve()
    assert workspace.is_dir()
