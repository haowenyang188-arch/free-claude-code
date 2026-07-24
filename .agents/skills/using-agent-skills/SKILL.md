---
name: using-agent-skills
description: Selects and activates skills from the runtime-provided skill catalog. Use at task start, when a user names a skill, or when you must determine which discovered skill applies without relying on a static roster or filesystem search.
---

# Using Agent Skills

## Responsibility

This meta-skill owns only skill selection, activation, duplicate handling, and
selection reporting. It does not copy or replace another skill's workflow.

## Runtime Inputs

Use only:

- The user's current task.
- Skill entries supplied by the current runtime, including each entry's
  `name`, `description`, `location`, and source layer when the runtime provides
  it.

Treat that runtime catalog as authoritative for the current session. Do not
search the filesystem to manufacture a skill entry that the runtime did not
discover.

## Selection

1. If the user explicitly names a skill, require an exact `name` match in the
   runtime catalog.
2. Otherwise, compare the current task with runtime-provided descriptions and
   select the smallest set that directly covers the work.
3. Select multiple skills only when each has a distinct responsibility. Order
   them by the current task's real dependencies, not by a static lifecycle.
4. Do not maintain a hardcoded skill list, phase map, or example task chain.

## Duplicate Identity

- Identify an entry by its runtime-provided `name` and `location`.
- Collapse exact duplicate entries that have the same name and location.
- Keep entries with the same name but different locations distinct. Codex does
  not merge them or guarantee a project-over-user-over-system override.
- If the runtime identifies which same-name entry was selected, use that exact
  entry. If it does not, report the ambiguity instead of inventing precedence.

## Activation

1. Activate through the runtime's skill mechanism by skill name.
2. After activation, read the full `SKILL.md` from the location exposed for
   that entry and follow it faithfully.
3. Never hardcode a system, user, or project skill's absolute path.
4. Never claim activation merely because a similarly named file exists.

## Missing or Ambiguous Skills

If an explicitly requested skill is absent, fail clearly:

```text
Skill unavailable: <name>. The current session did not discover it.
```

Do not use `find`, `grep`, `rg`, directory crawling, or guessed paths as a
substitute for runtime discovery. A restart or runtime configuration repair may
be needed before the skill can be activated.

If multiple different locations expose the requested name and the runtime does
not disambiguate them, report every runtime-provided location and request an
explicit selection.

## Selection Report

When reporting is useful, keep it compact:

```text
Selected: <name>
Source/location: <runtime-provided value>
Reason: <current-task match>
Status: activated | unavailable | ambiguous
```

Do not repeat the selected skill's execution steps in this report.

## Verification

- Selection used only the current runtime catalog and current task.
- Every activated skill was addressed by name.
- Exact duplicates were collapsed; same-name different-location entries were
  not silently merged or overridden.
- Missing or ambiguous skills were reported explicitly.
- No static roster, hardcoded absolute skill path, or borrowed workflow was
  introduced.
