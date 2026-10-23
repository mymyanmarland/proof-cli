# proof-cli — Spec v0.1

## Problem
AI coding agents say "done" when the work is not done: the build is
broken, a test fails, or the page renders as a white screen. The human
ends up re-checking everything by hand — the agent writes the code and
the human becomes its QA department.

## What it is
A tool an agent must run **before** reporting "done". It checks the
project with the checks the project already has, applies white-screen
heuristics to the web UI, deduplicates the errors, and writes ONE
compact PASS/FAIL report (`proof-report.md`) that an agent can read
and self-fix from.

## Hard rules
1. **Never report done without a PASS.** FAIL means fix and re-run.
2. **Missing is SKIP, never FAIL.** A project without a `test` script
   has not failed its tests — the check simply does not exist.
3. **Stdlib-only core.** Python 3, no pip installs, no API keys.
   Playwright is used only if it is already importable (screenshots +
   console errors); without it, that one check is an honest SKIP.
4. **Compact report.** One verdict, one table, deduplicated errors,
   and a short "fix these first" list. An agent reads it in one pass.

## v0.1 scope
- `proof check <project_dir> [--out <path>] [--url <url>]`
  - **Detect**: Node (`package.json`), Python (tests present),
    static site (`index.html`).
  - **Node checks**: run only the scripts that exist, in order —
    `build`, `lint`, `typecheck` / `type-check`, `test` — each with a
    timeout, each recorded PASS/FAIL with its duration.
  - **Python checks**: `pytest -q` when tests/config exist
    (SKIP when pytest is not installed or collects nothing).
  - **Web check**: with `--url`, fetch the page; with a static
    `index.html`, read it. Heuristics: HTTP ≥ 400 = FAIL; empty /
    near-empty body with no visible text or elements = white-screen
    FAIL; missing `<title>` = warning.
  - **Playwright (optional)**: with `--url` and playwright installed —
    desktop + mobile screenshots into `proof-screenshots/` and
    console-error capture (console errors = FAIL).
  - **Verdict**: FAIL if any run check fails or a white screen is
    detected (or if nothing ran at all — nothing ran is not proof).
    Exit code 0 on PASS, 1 on FAIL.
- `proof mcp` — minimal stdio MCP server exposing one tool,
  `proof_check` (`project_dir`, optional `url`), returning the report.
- `SKILL.md` — agent instructions: never report done without a PASS.

## Out of scope for v0.1
- Fixing the failures (the agent does that, from the report)
- Hosted dashboards, CI services, accounts
- Deep accessibility / performance audits

## Success criteria
- Planted-error tests: a deliberately failing Node build → FAIL that
  names the build; an empty-body page → FAIL with white screen
  detected; a healthy project → PASS.
- Unit tests pass.
- A real project run produces an honest verdict (a FAIL with real
  findings is a success for the tool, not a failure of it).
