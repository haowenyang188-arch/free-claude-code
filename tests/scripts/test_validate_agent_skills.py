from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

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


def _semantic_codes(skill_root: Path, *patterns: str) -> set[str]:
    validator = _load_validator()
    issues = validator.validate_skill_root(
        skill_root,
        semantic_checks=True,
        forbidden_patterns=patterns,
    )
    return {issue.code for issue in issues}


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


@pytest.mark.parametrize(
    ("polluted_instruction", "expected_code"),
    (
        ("用户已明确选择 D 撤回旧版交付。", "stale-task-decision"),
        ("当前批次已获得用户授权, 可直接启动 Chrome.", "static-authorization"),
        ("用户对当前批次一次性授权桌面 Chrome 接管.", "static-authorization"),
        ("证据固定写入 /home/alice/xhs/evidence。", "personal-absolute-path"),
        ("当前批次状态: 已完成采集, 等待生图.", "stale-batch-state"),
        ("默认使用同一位20岁中国女性。", "fixed-persona-default"),
    ),
)
def test_semantic_checks_report_stale_task_content(
    tmp_path: Path, polluted_instruction: str, expected_code: str
) -> None:
    skill_root = tmp_path / "skills"
    skill_path = skill_root / "polluted-skill"
    _write_skill(skill_path, "polluted-skill")
    skill_file = skill_path / "SKILL.md"
    skill_file.write_text(
        f"{skill_file.read_text(encoding='utf-8')}\n{polluted_instruction}\n",
        encoding="utf-8",
    )

    assert expected_code in _semantic_codes(skill_root)


def test_semantic_checks_inspect_agent_default_prompt(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_path = skill_root / "polluted-skill"
    _write_skill(skill_path, "polluted-skill")
    agents_path = skill_path / "agents"
    agents_path.mkdir()
    (agents_path / "openai.yaml").write_text(
        'interface:\n  default_prompt: "旧版交付已按用户选择D撤回, 当前合同生效."\n',
        encoding="utf-8",
    )

    assert "stale-task-decision" in _semantic_codes(skill_root)


def test_semantic_checks_are_opt_in(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_path = skill_root / "polluted-skill"
    _write_skill(skill_path, "polluted-skill")
    skill_file = skill_path / "SKILL.md"
    skill_file.write_text(
        f"{skill_file.read_text(encoding='utf-8')}\n用户已明确选择 D 撤回旧版交付。\n",
        encoding="utf-8",
    )
    validator = _load_validator()

    assert validator.validate_skill_root(skill_root) == []


def test_semantic_checks_allow_ordinary_technical_paths(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_path = skill_root / "portable-skill"
    _write_skill(skill_path, "portable-skill")
    skill_file = skill_path / "SKILL.md"
    skill_file.write_text(
        f"{skill_file.read_text(encoding='utf-8')}\n"
        "Install the executable under /usr/local/bin and cache data in /var/cache.\n"
        "当前批次需要用户授权后才能启动 Chrome.\n",
        encoding="utf-8",
    )

    assert _semantic_codes(skill_root) == set()


def test_semantic_checks_accept_custom_forbidden_patterns(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    skill_path = skill_root / "custom-skill"
    _write_skill(skill_path, "custom-skill")
    skill_file = skill_path / "SKILL.md"
    skill_file.write_text(
        f"{skill_file.read_text(encoding='utf-8')}\nNever publish internal-campaign-42.\n",
        encoding="utf-8",
    )

    assert "custom-forbidden-pattern" in _semantic_codes(
        skill_root, r"internal-campaign-\d+"
    )


def test_semantic_cli_flag_enables_checks() -> None:
    validator = _load_validator()

    args = validator._parser().parse_args(
        ["--check-semantics", "--forbid-pattern", "campaign-42"]
    )

    assert args.check_semantics is True
    assert args.forbidden_patterns == ["campaign-42"]


def test_semantic_cli_rejects_invalid_forbidden_pattern() -> None:
    validator = _load_validator()

    with pytest.raises(SystemExit):
        validator._parser().parse_args(["--forbid-pattern", "["])
