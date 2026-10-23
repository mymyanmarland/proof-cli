# proof-cli — agent skill

## The rule
**Never report "done" without a proof check PASS.**

An agent saying "done" on its own word is how broken builds, failing
tests, and white screens reach the human. proof-cli is the receipt:
run it, read the verdict, and only then speak.

## When to use
Every time you are about to tell the human a coding task is finished —
a feature, a bug fix, a refactor, a new page. No exceptions for
"small" changes; small changes white-screen pages too.

## How
```bash
python3 proof.py check <project_dir>            # writes proof-report.md
python3 proof.py check <project_dir> --url https://example.com
```

Exit code 0 = PASS, 1 = FAIL. Then:

1. **PASS** — report done, and mention the proof report. You may say
   what was checked (the report's table), not just "it works".
2. **FAIL** — do NOT report done. Read the report's
   **"Fix these first"** list, fix the top failure, and re-run.
   Repeat until PASS. If a failure is genuinely outside your change,
   say exactly that, with the failing check named — never silently
   downgrade a FAIL to "probably fine".
3. **SKIP is honest, not success.** A skipped check (missing script,
   playwright not installed) means that thing was *not verified*.
   If everything skipped, nothing was proved — say so.

## Reading the report
- **Verdict** — the one word that decides whether you may say "done".
- **Checks table** — PASS/FAIL/SKIP per check, with durations.
- **Failures (deduplicated)** — `×12` means one error repeated 12
  times, not 12 errors. Fix the highest count first.
- **Warnings** — real but not blocking (e.g. a missing `<title>`).

## MCP
`proof mcp` runs a stdio MCP server with one tool, `proof_check`
(`project_dir`, optional `url`), returning the report markdown:

```json
{ "command": "python3", "args": ["/path/to/proof.py", "mcp"] }
```
