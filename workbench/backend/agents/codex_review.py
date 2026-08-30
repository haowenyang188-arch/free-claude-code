"""Codex Reviewer structured outcome (S0-R P4, F-4).

The Reviewer may ONLY conclude PASS / REWORK / PLAN_INVALID.  ``parse_review``
is FAIL CLOSED: empty output, missing/unknown ``result``, malformed or
truncated JSON, or wrong field types raise a typed error — a parse failure can
never degrade into an implicit PASS.  ``REVIEW_PARSE_FAILED`` is an internal
error marker, NOT a business outcome Codex may emit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

REVIEW_OUTCOMES = frozenset({"PASS", "REWORK", "PLAN_INVALID"})


class ReviewOutcome(StrEnum):
    PASS = "PASS"
    REWORK = "REWORK"
    PLAN_INVALID = "PLAN_INVALID"


class ReviewParseError(RuntimeError):
    """The raw review output could not be parsed into any review structure."""


class ReviewValidationError(RuntimeError):
    """Parsed but failed the review contract (result/blocking/evidence)."""


@dataclass
class ReviewResult:
    result: str
    blocking: list[str] = field(default_factory=list)
    non_blocking: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    thread_id: str | None = None
    turn_id: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "blocking": list(self.blocking),
            "non_blocking": list(self.non_blocking),
            "evidence": list(self.evidence),
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
        }


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Read a ```json ... ``` block (or the first bare {...} object)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"(\{.*\})", text, re.DOTALL)
        raw = m.group(1) if m else None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ReviewValidationError(f"review.{field_name} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ReviewValidationError(
                f"review.{field_name} must contain only strings"
            )
        out.append(item)
    return out


def parse_review(
    text: str,
    *,
    thread_id: str | None = None,
    turn_id: str | None = None,
) -> ReviewResult:
    """Parse a Codex Reviewer output into a validated ReviewResult. FAIL CLOSED.

    Raises:
        ReviewParseError:      empty / non-string / ```json block malformed
        ReviewValidationError: missing/unknown result, wrong field types
    """
    if not isinstance(text, str) or not text.strip():
        raise ReviewParseError("empty review output: no conclusion was produced")

    data = _extract_json_object(text)
    if data is None:
        if "```json" in text.lower():
            raise ReviewParseError(
                "malformed json block: could not parse the review object"
            )
        raise ReviewParseError("review output is not a JSON object")

    result = data.get("result")
    if not isinstance(result, str) or result not in REVIEW_OUTCOMES:
        raise ReviewValidationError(
            f"review.result must be one of PASS|REWORK|PLAN_INVALID, got {result!r}"
        )

    blocking = _string_list(data.get("blocking", []), "blocking")
    non_blocking = _string_list(data.get("non_blocking", []), "non_blocking")
    evidence = _string_list(data.get("evidence", []), "evidence")

    # A conclusion must be supported: PASS without evidence is suspicious but
    # allowed by contract; REWORK/PLAN_INVALID without blocking items is not a
    # valid fail-closed conclusion (the model must point at a defect).
    if result in (ReviewOutcome.REWORK, ReviewOutcome.PLAN_INVALID) and not blocking:
        raise ReviewValidationError(
            f"review.result={result} requires at least one blocking item"
        )

    return ReviewResult(
        result=result,
        blocking=blocking,
        non_blocking=non_blocking,
        evidence=evidence,
        thread_id=thread_id,
        turn_id=turn_id,
    )
