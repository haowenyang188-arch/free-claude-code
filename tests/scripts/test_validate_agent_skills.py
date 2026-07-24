from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "validate-agent-skills.py"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "agent-skills"


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_agent_skills", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _codes(scenario: str) -> set[str]:
    validator = _load_validator()
    issues = validator.validate_skill_root(FIXTURE_ROOT / scenario)
    return {issue.code for issue in issues}


def _write_skill(path: Path, name: str) -> None:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "\n".join(
            (
                "---",
                f"name: {name}",
                "description: Test fixture. Use when validating edge cases.",
                "---",
                "",
                f"# {name}",
            )
        ),
        encoding="utf-8",
    )


def test_valid_skill_passes() -> None:
    assert _codes("valid") == set()


def test_dangling_reference_is_reported() -> None:
    assert "missing-reference" in _codes("dangling-reference")


def test_absolute_resource_path_is_reported() -> None:
    assert "absolute-resource-path" in _codes("absolute-path")


def test_duplicate_name_is_reported() -> None:
    assert "duplicate-name" in _codes("duplicate-name")


def test_directory_name_mismatch_is_reported() -> None:
    assert "directory-name-mismatch" in _codes("directory-name-mismatch")


def test_invalid_standard_name_is_reported(tmp_path: Path) -> None:
    _write_skill(tmp_path / "invalid-name", "Invalid_Name")
    validator = _load_validator()

    codes = {issue.code for issue in validator.validate_skill_root(tmp_path)}

    assert "invalid-name" in codes


def test_nested_skill_file_is_reported(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root / "outer-skill", "outer-skill")
    _write_skill(skill_root / "outer-skill" / "references" / "nested", "nested")
    validator = _load_validator()

    codes = {issue.code for issue in validator.validate_skill_root(skill_root)}

    assert "nested-skill-file" in codes


def test_broken_symlink_is_reported(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_path = skill_root / "linked-skill"
    _write_skill(skill_path, "linked-skill")
    (skill_path / "missing-reference").symlink_to(skill_path / "does-not-exist")
    validator = _load_validator()

    codes = {issue.code for issue in validator.validate_skill_root(skill_root)}

    assert "broken-symlink" in codes


def test_wrong_skill_filename_case_is_reported(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_path = skill_root / "case-skill"
    _write_skill(skill_path, "case-skill")
    (skill_path / "SKILL.md").rename(skill_path / "skill.md")
    validator = _load_validator()

    codes = {issue.code for issue in validator.validate_skill_root(skill_root)}

    assert "wrong-filename-case" in codes


def test_repository_skills_pass_validation() -> None:
    validator = _load_validator()
    issues = validator.validate_skill_root(REPOSITORY_ROOT / ".agents" / "skills")

    assert issues == []
