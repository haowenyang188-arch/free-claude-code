#!/usr/bin/env -S uv run
"""Validate repository Agent Skills without loading or executing them."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SKILL_ROOT = REPOSITORY_ROOT / ".agents" / "skills"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOP_LEVEL_FIELD = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((?P<target><[^>]+>|[^)]+)\)")
RESOURCE_PATH = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?P<path>(?:references|scripts|assets)/[A-Za-z0-9_@+~./-]+)"
)
ABSOLUTE_RESOURCE_PATH = re.compile(
    r"(?P<path>/(?:[^\s`'\"<>()\[\]]+/)*"
    r"(?:references|scripts|assets)/[^\s`'\"<>()\[\],;]+)"
)
ALLOWED_FRONTMATTER_FIELDS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
RESOURCE_DIRECTORIES = {"references", "scripts", "assets"}


class SemanticRule(NamedTuple):
    code: str
    pattern: re.Pattern[str]
    message: str


# These patterns deliberately target stateful assertions, rather than words such
# as "batch", "authorization", or ordinary absolute system paths in isolation.
DEFAULT_SEMANTIC_RULES = (
    SemanticRule(
        "stale-task-decision",
        re.compile(
            r"(?:用户|客户)\s*(?:已|已经|此前|早已)\s*(?:明确\s*)?"
            r"(?:选择|选定|决定|确认(?:采用|使用)?)"
            r"[^\n\u3002\uff01\uff1f.!?]{0,100}(?:撤回|旧版|旧方案|交付|拒绝|批次)"
            r"|(?:已|已经|此前|早已)\s*(?:按|根据)\s*(?:用户|客户)(?:的)?\s*"
            r"(?:选择|选定|决定|确认)"
            r"[^\n\u3002\uff01\uff1f.!?]{0,100}(?:撤回|旧版|旧方案|交付|拒绝|批次)"
            r"|\b(?:user|customer)\b[^\n.!?]{0,30}"
            r"\b(?:already|previously|explicitly)\s+"
            r"(?:selected|chose|decided)\b[^\n.!?]{0,100}"
            r"\b(?:withdraw|old|delivery|batch|option)\b",
            re.IGNORECASE,
        ),
        "skill content contains a prior task decision that must be supplied by the current task",
    ),
    SemanticRule(
        "static-authorization",
        re.compile(
            r"(?:当前|本|该)\s*批次\s*(?:已|已经|此前|早已)\s*"
            r"(?:获(?:得|取)|得到|取得|拥有|有)?\s*(?:由\s*)?"
            r"(?:用户|客户)?\s*(?:明确\s*)?(?:授权|批准)"
            r"|(?:用户|客户)\s*(?:已|已经|此前|早已)\s*(?:明确\s*)?"
            r"(?:授权|批准|授予)"
            r"|(?:用户|客户)\s*对\s*(?:当前|本|该)\s*批次\s*"
            r"(?:一次性\s*)?(?:授权|批准)"
            r"|\b(?:this|current)\s+(?:batch|run|task)\b"
            r"[^\n.!?]{0,40}\b(?:is|was|has been|already)\s+"
            r"(?:authorized|approved)\b"
            r"|\b(?:user|customer)\b[^\n.!?]{0,20}"
            r"\b(?:has already|already|previously|has)\s+"
            r"(?:authorized|approved)\b",
            re.IGNORECASE,
        ),
        "skill content claims that authorization already exists for a task or batch",
    ),
    SemanticRule(
        "personal-absolute-path",
        re.compile(
            r"(?<![\w.-])(?:"
            r"/mnt/[A-Za-z]/Users/(?!(?:user(?:name)?|root|runner|app|service|example)(?:[/\\]|$))"
            r"[A-Za-z0-9][A-Za-z0-9._-]*(?:[/\\][^\s`'\"<>()\[\],;\u3002\uff01\uff1f]*)?"
            r"|/(?:home|Users)/(?!(?:user(?:name)?|root|runner|app|service|example)(?:[/\\]|$))"
            r"[A-Za-z0-9][A-Za-z0-9._-]*(?:[/\\][^\s`'\"<>()\[\],;\u3002\uff01\uff1f]*)?"
            r"|[A-Za-z]:[\\/]Users[\\/]+(?!(?:user(?:name)?|root|runner|app|service|example)(?:[/\\]|$))"
            r"[A-Za-z0-9][A-Za-z0-9._-]*(?:[\\/][^\s`'\"<>()\[\],;\u3002\uff01\uff1f]*)?"
            r")",
            re.IGNORECASE,
        ),
        "skill content contains a personal absolute path; use host configuration instead",
    ),
    SemanticRule(
        "stale-batch-state",
        re.compile(
            r"(?:当前|本|该)\s*(?:批次|任务|运行)\s*(?:状态|阶段)\s*[:\uFF1A=]?"
            r"[^\n\u3002\uff01\uff1f.!?]{0,60}(?:已|正在|等待|完成|失败|采集|生图|交付)"
            r"|\b(?:current|previous|old)\s+(?:batch|run|task)\b"
            r"[^\n.!?]{0,60}\b(?:status|stage|completed|failed|waiting)\b"
            r"|\b(?:batch|run)[_-]?id\s*[:=]\s*"
            r"(?:20\d{2}[-_/]\d{1,2}[-_/]\d{1,2}|[A-Za-z0-9][A-Za-z0-9_-]{7,})",
            re.IGNORECASE,
        ),
        "skill content contains a persisted batch or run state that must be task-local",
    ),
    SemanticRule(
        "fixed-persona-default",
        re.compile(
            r"(?:默认|固定|始终|必须|同一位|always|default|fixed|same)"
            r"[^\n\u3002\uff01\uff1f.!?]{0,80}"
            r"(?:\d{1,3}\s*(?:岁|years?\s*[- ]?old)[^\n\u3002\uff01\uff1f.!?]{0,40})?"
            r"(?:中国女性|中国男性|女性|男性|woman|man)",
            re.IGNORECASE,
        ),
        "skill content hard-codes a persona as a default; persona must come from the current task",
    ),
)


class Issue(NamedTuple):
    code: str
    path: Path
    message: str


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(path)


def _parse_frontmatter(
    skill_file: Path, text: str
) -> tuple[dict[str, object], str, list[Issue]]:
    issues: list[Issue] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return (
            {},
            text,
            [
                Issue(
                    "missing-frontmatter",
                    skill_file,
                    "SKILL.md must begin with a YAML frontmatter delimiter",
                )
            ],
        )

    try:
        closing_index = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return (
            {},
            "",
            [
                Issue(
                    "unclosed-frontmatter",
                    skill_file,
                    "frontmatter has no closing delimiter",
                )
            ],
        )

    raw_frontmatter = "\n".join(lines[1:closing_index])
    top_level_fields: list[str] = []
    for index, line in enumerate(lines[1:closing_index], start=2):
        if not line.strip() or line.lstrip().startswith("#") or line[0].isspace():
            continue
        match = TOP_LEVEL_FIELD.match(line)
        if match is None:
            issues.append(
                Issue(
                    "invalid-frontmatter",
                    skill_file,
                    f"invalid top-level field on frontmatter line {index}",
                )
            )
            continue
        key = match.group(1)
        if key in top_level_fields:
            issues.append(
                Issue(
                    "duplicate-frontmatter-field",
                    skill_file,
                    f"frontmatter field {key!r} is repeated",
                )
            )
        top_level_fields.append(key)
        if key not in ALLOWED_FRONTMATTER_FIELDS:
            issues.append(
                Issue(
                    "unknown-frontmatter-field",
                    skill_file,
                    f"frontmatter field {key!r} is not in the Agent Skills specification",
                )
            )

    try:
        loaded = yaml.safe_load(raw_frontmatter)
    except yaml.YAMLError as error:
        issues.append(
            Issue(
                "invalid-frontmatter",
                skill_file,
                f"frontmatter is not valid YAML: {error}",
            )
        )
        loaded = {}
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        issues.append(
            Issue(
                "invalid-frontmatter",
                skill_file,
                "frontmatter must be a YAML mapping",
            )
        )
        loaded = {}
    values = {str(key): value for key, value in loaded.items()}

    body = "\n".join(lines[closing_index + 1 :]).strip()
    return values, body, issues


def _validate_frontmatter(
    skill_file: Path, directory_name: str
) -> tuple[str | None, list[Issue]]:
    issues: list[Issue] = []
    try:
        text = skill_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, [
            Issue("invalid-encoding", skill_file, "SKILL.md must be valid UTF-8")
        ]

    values, body, parse_issues = _parse_frontmatter(skill_file, text)
    issues.extend(parse_issues)
    raw_name = values.get("name")
    raw_description = values.get("description")
    name = raw_name if isinstance(raw_name, str) else ""
    description = raw_description if isinstance(raw_description, str) else ""

    if raw_name is not None and not isinstance(raw_name, str):
        issues.append(Issue("invalid-name", skill_file, "name must be a string"))
    elif not name.strip():
        issues.append(Issue("missing-name", skill_file, "frontmatter name is required"))
    else:
        if len(name) > 64:
            issues.append(
                Issue("invalid-name", skill_file, "name must be 64 characters or fewer")
            )
        if not NAME_PATTERN.fullmatch(name):
            issues.append(
                Issue(
                    "invalid-name",
                    skill_file,
                    "name must contain lowercase letters, digits, and single hyphens only",
                )
            )
        if name != directory_name:
            issues.append(
                Issue(
                    "directory-name-mismatch",
                    skill_file,
                    f"frontmatter name {name!r} does not match directory {directory_name!r}",
                )
            )

    if raw_description is not None and not isinstance(raw_description, str):
        issues.append(
            Issue("invalid-description", skill_file, "description must be a string")
        )
    elif not description.strip():
        issues.append(
            Issue(
                "missing-description", skill_file, "frontmatter description is required"
            )
        )
    elif len(description) > 1024:
        issues.append(
            Issue(
                "invalid-description",
                skill_file,
                "description must be 1024 characters or fewer",
            )
        )

    for field in ("license", "allowed-tools"):
        value = values.get(field)
        if value is not None and not isinstance(value, str):
            issues.append(
                Issue(
                    "invalid-frontmatter-field",
                    skill_file,
                    f"frontmatter field {field!r} must be a string",
                )
            )

    compatibility = values.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str):
            issues.append(
                Issue(
                    "invalid-compatibility",
                    skill_file,
                    "compatibility must be a string",
                )
            )
        elif len(compatibility) > 500:
            issues.append(
                Issue(
                    "invalid-compatibility",
                    skill_file,
                    "compatibility must be 500 characters or fewer",
                )
            )

    metadata = values.get("metadata")
    if metadata is not None and (
        not isinstance(metadata, dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        )
    ):
        issues.append(
            Issue(
                "invalid-metadata",
                skill_file,
                "metadata must map string keys to string values",
            )
        )
    if not body:
        issues.append(Issue("empty-skill-body", skill_file, "SKILL.md body is empty"))

    return name or None, issues


def _strip_link_title(target: str) -> str:
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        return target[1:-1]
    return target.split(maxsplit=1)[0]


def _local_references(text: str) -> Iterator[str]:
    seen: set[str] = set()
    for match in MARKDOWN_LINK.finditer(text):
        target = _strip_link_title(match.group("target"))
        parsed = urlsplit(target)
        if parsed.scheme or target.startswith("#") or parsed.netloc:
            continue
        path = unquote(parsed.path)
        if path and path not in seen:
            seen.add(path)
            yield path
    for match in RESOURCE_PATH.finditer(text):
        path = match.group("path").rstrip(".:")
        if path not in seen:
            seen.add(path)
            yield path


def _case_correct_path(root: Path, relative_path: PurePosixPath) -> Path | None:
    current = root
    for part in relative_path.parts:
        if part in {"", "."}:
            continue
        if part == ".." or not current.is_dir():
            return None
        matches = [
            child
            for child in current.iterdir()
            if child.name.casefold() == part.casefold()
        ]
        if len(matches) != 1:
            return None
        current = matches[0]
    return current


def _validate_references(skill_file: Path) -> list[Issue]:
    issues: list[Issue] = []
    text = skill_file.read_text(encoding="utf-8")
    skill_root = skill_file.parent
    resolved_root = skill_root.resolve()

    absolute_paths = {
        match.group("path").rstrip(".:")
        for match in ABSOLUTE_RESOURCE_PATH.finditer(text)
    }
    issues.extend(
        [
            Issue(
                "absolute-resource-path",
                skill_file,
                f"bundled resource must be relative to the skill root: {path}",
            )
            for path in sorted(absolute_paths)
        ]
    )

    for reference in _local_references(text):
        pure_path = PurePosixPath(reference)
        if pure_path.is_absolute():
            if any(part in RESOURCE_DIRECTORIES for part in pure_path.parts):
                continue
            issues.append(
                Issue(
                    "absolute-local-link",
                    skill_file,
                    f"local link must be relative to the skill root: {reference}",
                )
            )
            continue

        candidate = skill_root.joinpath(*pure_path.parts)
        resolved_candidate = candidate.resolve(strict=False)
        if not resolved_candidate.is_relative_to(resolved_root):
            issues.append(
                Issue(
                    "reference-outside-skill",
                    skill_file,
                    f"reference escapes the skill root: {reference}",
                )
            )
            continue
        if candidate.exists():
            continue

        case_match = _case_correct_path(skill_root, pure_path)
        if case_match is not None and case_match.exists():
            issues.append(
                Issue(
                    "wrong-filename-case",
                    skill_file,
                    f"reference case does not match filesystem entry: {reference}",
                )
            )
        else:
            issues.append(
                Issue(
                    "missing-reference",
                    skill_file,
                    f"referenced path does not exist relative to the skill root: {reference}",
                )
            )
    return issues


def _default_prompt_values(metadata_file: Path) -> list[str]:
    """Return only default-prompt values from an agent metadata file.

    Agent metadata can contain descriptive text that is not an active prompt.
    Restricting the semantic scan to this field avoids treating arbitrary
    examples or implementation notes as executable instructions.
    """

    try:
        loaded = yaml.safe_load(metadata_file.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, yaml.YAMLError:
        return []

    prompts: list[str] = []

    def visit(value: object, key: str | None = None) -> None:
        if key is not None and key.replace("-", "_").lower() == "default_prompt":
            if isinstance(value, str):
                prompts.append(value)
            return
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                if isinstance(child_key, str):
                    visit(child_value, child_key)
        elif isinstance(value, list):
            for child_value in value:
                visit(child_value)

    visit(loaded)
    return prompts


def _semantic_sources(skill_root: Path) -> Iterator[tuple[Path, str]]:
    """Yield skill bodies and active default prompts for semantic inspection."""

    for skill_directory in sorted(
        (path for path in skill_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    ):
        skill_file = skill_directory / "SKILL.md"
        if skill_file.is_file():
            with contextlib.suppress(OSError, UnicodeDecodeError):
                yield skill_file, skill_file.read_text(encoding="utf-8")

        agents_directory = skill_directory / "agents"
        if not agents_directory.is_dir():
            continue
        for metadata_file in sorted(agents_directory.glob("openai.y*")):
            if metadata_file.suffix.lower() not in {".yaml", ".yml"}:
                continue
            for prompt in _default_prompt_values(metadata_file):
                yield metadata_file, prompt


def _compile_forbidden_patterns(
    forbidden_patterns: Iterable[str | re.Pattern[str]],
) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in forbidden_patterns:
        if isinstance(pattern, str):
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE | re.MULTILINE))
            except re.error as error:
                raise ValueError(
                    f"invalid forbidden pattern {pattern!r}: {error}"
                ) from error
        else:
            compiled.append(pattern)
    return compiled


def _forbidden_pattern_argument(value: str) -> str:
    try:
        re.compile(value)
    except re.error as error:
        raise argparse.ArgumentTypeError(
            f"invalid forbidden pattern {value!r}: {error}"
        ) from error
    return value


def validate_semantic_pollution(
    skill_root: Path,
    *,
    forbidden_patterns: Iterable[str | re.Pattern[str]] = (),
) -> list[Issue]:
    """Reject task state and other non-portable content in active skill inputs.

    The check is intentionally opt-in. ``forbidden_patterns`` are regular
    expressions supplied by the host and are evaluated only against SKILL.md
    and ``agents/openai.yaml`` default-prompt values.
    """

    skill_root = skill_root.resolve()
    issues: list[Issue] = []
    custom_patterns = _compile_forbidden_patterns(forbidden_patterns)

    for source_path, text in _semantic_sources(skill_root):
        for rule in DEFAULT_SEMANTIC_RULES:
            match = rule.pattern.search(text)
            if match is None:
                continue
            issues.append(
                Issue(
                    rule.code,
                    source_path,
                    f"{rule.message} (line {text.count(chr(10), 0, match.start()) + 1})",
                )
            )
        for pattern in custom_patterns:
            match = pattern.search(text)
            if match is None:
                continue
            issues.append(
                Issue(
                    "custom-forbidden-pattern",
                    source_path,
                    f"content matches configured forbidden pattern {pattern.pattern!r} "
                    f"(line {text.count(chr(10), 0, match.start()) + 1})",
                )
            )
    return issues


def _walk_entries(root: Path) -> Iterator[Path]:
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in sorted((*directory_names, *file_names)):
            yield base / name


def _validate_tree(skill_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for path in _walk_entries(skill_root):
        if path.is_symlink() and not path.exists():
            issues.append(
                Issue("broken-symlink", path, "symbolic link target does not exist")
            )
        if path.is_file() and path.name.casefold() == "skill.md":
            if path.name != "SKILL.md":
                issues.append(
                    Issue(
                        "wrong-filename-case",
                        path,
                        "skill entrypoint must be named exactly SKILL.md",
                    )
                )
            if path.parent.parent != skill_root:
                issues.append(
                    Issue(
                        "nested-skill-file",
                        path,
                        "SKILL.md is only allowed in a direct child skill directory",
                    )
                )
    return issues


def validate_skill_root(
    skill_root: Path,
    *,
    semantic_checks: bool = False,
    forbidden_patterns: Iterable[str | re.Pattern[str]] = (),
) -> list[Issue]:
    skill_root = skill_root.resolve()
    if not skill_root.is_dir():
        return [
            Issue("invalid-skill-root", skill_root, "skill root is not a directory")
        ]

    issues = _validate_tree(skill_root)
    names: defaultdict[str, list[Path]] = defaultdict(list)
    direct_directories = sorted(
        (path for path in skill_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )
    for directory in direct_directories:
        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            case_variants = [
                child
                for child in directory.iterdir()
                if child.is_file() and child.name.casefold() == "skill.md"
            ]
            if not case_variants:
                issues.append(
                    Issue(
                        "missing-skill-file",
                        directory,
                        "direct child directory must contain SKILL.md",
                    )
                )
            continue

        name, frontmatter_issues = _validate_frontmatter(skill_file, directory.name)
        issues.extend(frontmatter_issues)
        issues.extend(_validate_references(skill_file))
        if name:
            names[name].append(skill_file)

    for name, paths in sorted(names.items()):
        if len(paths) < 2:
            continue
        locations = ", ".join(_display_path(path) for path in paths)
        for path in paths:
            issues.append(
                Issue(
                    "duplicate-name",
                    path,
                    f"frontmatter name {name!r} is shared by: {locations}",
                )
            )

    if semantic_checks or forbidden_patterns:
        issues.extend(
            validate_semantic_pollution(
                skill_root,
                forbidden_patterns=forbidden_patterns,
            )
        )

    return sorted(
        issues, key=lambda issue: (str(issue.path), issue.code, issue.message)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "skill_root",
        nargs="?",
        type=Path,
        default=DEFAULT_SKILL_ROOT,
        help=f"skill directory to validate (default: {_display_path(DEFAULT_SKILL_ROOT)})",
    )
    parser.add_argument(
        "--check-semantics",
        action="store_true",
        help="also reject stale task state and non-portable active prompt content",
    )
    parser.add_argument(
        "--forbid-pattern",
        dest="forbidden_patterns",
        action="append",
        default=[],
        metavar="REGEX",
        type=_forbidden_pattern_argument,
        help="additional case-insensitive regular expression to reject (repeatable)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    issues = validate_skill_root(
        args.skill_root,
        semantic_checks=args.check_semantics,
        forbidden_patterns=args.forbidden_patterns,
    )
    if issues:
        for issue in issues:
            print(f"ERROR [{issue.code}] {_display_path(issue.path)}: {issue.message}")
        print(f"Validation failed with {len(issues)} error(s).")
        return 1

    skill_count = sum(1 for path in args.skill_root.iterdir() if path.is_dir())
    print(
        f"Validated {skill_count} skill(s) in {_display_path(args.skill_root)}: 0 errors."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
