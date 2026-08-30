"""Minimal PLAN structuring for the real Claude Planner (S0-R P3 / P3-H).

Kept deliberately small: no schema framework.  `parse_plan` turns the real
Claude CLI's markdown PLAN (or an embedded ```json block) into the SOP
machine input:

    {"goal": str, "analysis": str, "steps": [str], "risks": [str], "acceptance": [str]}

P3-H hardening — FAIL CLOSED:
------------------------------
Parsing is deliberately loose (markdown / JSON / minor format deviations all
accepted), but the result MUST pass `validate_plan()` before it may become an
``ArtifactType.PLAN`` and trigger a PLAN_READY handoff.  A parse/validation
failure raises a typed error; it NEVER returns an empty structure, so a
"parse failure" can never be mistaken for a "successful plan".
"""

from __future__ import annotations

import json
import re
from typing import Any

PLAN_KEYS = ("goal", "analysis", "steps", "risks", "acceptance")


class PlanError(RuntimeError):
    """Base class for typed PLAN failures (fail-closed)."""


class PlanParseError(PlanError):
    """The raw output could not be parsed into any usable plan structure."""


class PlanValidationError(PlanError):
    """Parsed but failed the minimum PLAN contract (goal/steps/acceptance)."""


def _extract_json_block(markdown: str) -> dict[str, Any] | None:
    """Try to read a ```json ... ``` block (or a bare {...} object).

    Returns ``None`` when no JSON object could be parsed.
    """
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", markdown, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"(\{.*\})", markdown, re.DOTALL)
        raw = m.group(1) if m else None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _section(markdown: str, *headings: str) -> str | None:
    """Return the text of a markdown section (## heading) if present."""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip().lower()
        for h in headings:
            if stripped.startswith(h):
                body: list[str] = []
                for nxt in lines[i + 1 :]:
                    if nxt.strip().startswith("#"):
                        break
                    body.append(nxt)
                return "\n".join(body).strip()
    return None


def _bullet_items(text: str | None) -> list[str]:
    if not text:
        return []
    items: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:[-*]|\d+[.、)])\s+(.*)$", line)
        if m:
            items.append(m.group(1).strip())
    return items


def _items_or_lines(text: str | None) -> list[str]:
    """Bullets when present, otherwise each non-empty line is one item."""
    items = _bullet_items(text)
    if items:
        return items
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _first_heading_text(markdown: str) -> str:
    """First non-heading, non-list line — a sane goal fallback.

    List items are skipped so a plan that omits an explicit goal section
    (but has only 步骤/验收) does NOT get a bullet misread as its goal.
    """
    for line in markdown.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if re.match(r"^\s*(?:[-*]|\d+[.、)])\s+", s):
            continue
        return s[:500]
    return ""


def validate_plan(plan: dict[str, Any]) -> None:
    """Enforce the minimum PLAN contract.  Raises PlanValidationError.

    Minimum (fail-closed) requirements:
      * goal     — non-empty string
      * steps    — at least 1 non-empty string
      * acceptance — at least 1 non-empty string
    ``analysis`` and ``risks`` are allowed to be empty.
    """
    if not isinstance(plan, dict):
        raise PlanValidationError("plan must be an object")

    goal = plan.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise PlanValidationError("plan.goal must be a non-empty string")

    steps = plan.get("steps")
    if (
        not isinstance(steps, list)
        or not steps
        or not all(isinstance(s, str) and s.strip() for s in steps)
    ):
        raise PlanValidationError("plan.steps must contain at least one non-empty string")

    acceptance = plan.get("acceptance")
    if (
        not isinstance(acceptance, list)
        or not acceptance
        or not all(isinstance(a, str) and a.strip() for a in acceptance)
    ):
        raise PlanValidationError("plan.acceptance must contain at least one non-empty string")


def parse_plan(markdown: str) -> dict[str, Any]:
    """Structure a Claude PLAN into the SOP machine schema.  FAIL CLOSED.

    Raises:
        PlanParseError:       input is empty / not a string / a ```json block
                              exists but is malformed.
        PlanValidationError:  parsed but goal/steps/acceptance minimums are
                              not met (also: random prose, missing sections).
    """
    if not isinstance(markdown, str) or not markdown.strip():
        raise PlanParseError("empty output: no PLAN text was produced")

    data = _extract_json_block(markdown)
    if data is not None:
        if "goal" not in data:
            # JSON path has no markdown fallback: goal is mandatory in JSON.
            raise PlanValidationError("plan.goal missing from JSON plan")
        plan = {
            "goal": str(data.get("goal") or ""),
            "analysis": str(data.get("analysis") or ""),
            "steps": [str(s) for s in (data.get("steps") or [])],
            "risks": [str(r) for r in (data.get("risks") or [])],
            "acceptance": [str(a) for a in (data.get("acceptance") or [])],
        }
        validate_plan(plan)
        return plan

    if "```json" in markdown.lower():
        # a json code fence exists but could not be parsed -> hard parse error
        raise PlanParseError("malformed json block: could not parse the PLAN object")

    goal = _section(markdown, "goal", "目标")
    analysis = _section(markdown, "analysis", "分析", "问题分析")
    steps = _items_or_lines(_section(markdown, "steps", "步骤", "实施步骤"))
    risks = _items_or_lines(_section(markdown, "risks", "风险", "风险点"))
    acceptance = _items_or_lines(
        _section(markdown, "acceptance", "验收", "验收标准", "acceptance criteria")
    )

    plan = {
        "goal": (goal or _first_heading_text(markdown))[:500],
        "analysis": analysis or "",
        "steps": steps,
        "risks": risks,
        "acceptance": acceptance,
    }
    validate_plan(plan)
    return plan
