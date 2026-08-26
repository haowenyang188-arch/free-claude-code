from __future__ import annotations

import os
from unittest.mock import patch

import pytest


def test_staged_workspace_reports_and_applies_file_changes(tmp_path):
    from cli.staging import StagedWorkspace

    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("before\n", encoding="utf-8")
    (source / "keep.txt").chmod(0o750)
    (source / "remove.txt").write_text("remove\n", encoding="utf-8")

    staged = StagedWorkspace.create(source)
    (staged.path / "keep.txt").write_text("after\n", encoding="utf-8")
    (staged.path / "remove.txt").unlink()
    (staged.path / "new.txt").write_text("new\n", encoding="utf-8")

    changes = staged.changes()
    assert {change.path for change in changes} == {"keep.txt", "remove.txt", "new.txt"}
    assert "keep.txt" in staged.diff()
    assert "new.txt" in staged.diff()

    staged.apply()
    assert (source / "keep.txt").read_text(encoding="utf-8") == "after\n"
    assert not (source / "remove.txt").exists()
    assert (source / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert (source / "keep.txt").stat().st_mode & 0o777 == 0o750
    staged.cleanup()
    assert not staged.path.exists()


def test_staged_workspace_rejects_symlinked_source_entries(tmp_path):
    from cli.staging import StagedWorkspace, StagingError

    source = tmp_path / "source"
    outside = tmp_path / "outside.txt"
    source.mkdir()
    outside.write_text("outside", encoding="utf-8")
    (source / "link.txt").symlink_to(outside)

    with pytest.raises(StagingError, match="symlink"):
        StagedWorkspace.create(source)


def test_staged_workspace_rolls_back_when_a_later_change_cannot_apply(tmp_path):
    from cli.staging import StagedWorkspace, StagingError

    source = tmp_path / "source"
    source.mkdir()
    (source / "first.txt").write_text("before\n", encoding="utf-8")
    (source / "second.txt").write_text("before\n", encoding="utf-8")
    staged = StagedWorkspace.create(source)
    (staged.path / "first.txt").write_text("after\n", encoding="utf-8")
    (staged.path / "second.txt").write_text("after\n", encoding="utf-8")
    calls = 0
    real_replace = os.replace

    def flaky_replace(source_path, target_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replacement failure")
        real_replace(source_path, target_path)

    with (
        patch("cli.staging.os.replace", side_effect=flaky_replace),
        pytest.raises(StagingError, match="failed to apply"),
    ):
        staged.apply()

    assert (source / "first.txt").read_text(encoding="utf-8") == "before\n"
    assert (source / "second.txt").read_text(encoding="utf-8") == "before\n"
    staged.cleanup()


def test_staged_workspace_excludes_credentials_from_snapshot_and_diff(tmp_path):
    from cli.staging import StagedWorkspace

    source = tmp_path / "source"
    source.mkdir()
    (source / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (source / "server.py").write_text("print('before')\n", encoding="utf-8")

    staged = StagedWorkspace.create(source)
    (staged.path / ".env").write_text("API_KEY=changed\n", encoding="utf-8")
    (staged.path / "server.py").write_text("print('after')\n", encoding="utf-8")

    changes = staged.changes()
    assert [change.path for change in changes] == ["server.py"]
    assert "secret" not in staged.diff()
    assert "API_KEY" not in staged.diff()
    staged.cleanup()
