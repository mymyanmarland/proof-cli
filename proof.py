#!/usr/bin/env python3
"""proof-cli — make AI agents prove their work before saying "done".

Auto-detects a project, runs the checks the project already has
(Node scripts, pytest), applies white-screen heuristics to the web UI,
dedupes the errors, and writes ONE compact PASS/FAIL report
(`proof-report.md`) that an agent can read and self-fix from.

Core is stdlib only. Playwright is used opportunistically when it
happens to be installed (screenshots + console errors); it is never
required.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone

VERSION = "0.1.0"
CMD_TIMEOUT = 120          # seconds per project check
FETCH_TIMEOUT = 10         # seconds for the --url fetch
NODE_CHECK_ORDER = ["build", "lint", "typecheck", "type-check", "test"]
ERROR_HINT = re.compile(r"(?i)error|fail|exception|traceback|not found|"
                        r"cannot|missing|✖|×")


# ---------------------------------------------------------------- helpers
def run_cmd(cmd, cwd, timeout=CMD_TIMEOUT):
    """Run a command, return (exit_code|None, output, seconds, timed_out)."""
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or ""), \
            time.monotonic() - start, False
    except FileNotFoundError:
        return None, f"command not found: {cmd[0]}", time.monotonic() - start, False
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"") + (e.stderr or b""))
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return None, out + f"\n[timed out after {timeout}s]", \
            time.monotonic() - start, True
    except OSError as e:
        return None, str(e), time.monotonic() - start, False


_TS = re.compile(
    r"^(\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})?\]?\s+)")


def _signature(line):
    s = _TS.sub("", line.strip())
    s = re.sub(r"0x[0-9a-fA-F]+", "0xN", s)
    s = re.sub(r"\b\d+\b", "N", s)
    s = re.sub(r"\s+", " ", s)
    return s[:200]


def dedupe_lines(output, limit=15):
    """Collapse repeated lines into [(count, sample)] most frequent first.

    Error-looking lines come first; falls back to the last lines of the
    output (where failures usually summarise themselves).
    """
    lines = [ln.rstrip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return []
    error_lines = [ln for ln in lines if ERROR_HINT.search(ln)]
    pool = error_lines if error_lines else lines[-30:]
    groups = OrderedDict()
    for ln in pool:
        sig = _signature(ln)
        if sig in groups:
            groups[sig][0] += 1
        else:
            groups[sig] = [1, ln.strip()[:300]]
    ordered = sorted(groups.values(), key=lambda g: -g[0])
    return [(count, sample) for count, sample in ordered[:limit]]


def make_check(name, status, duration=0.0, detail="", output=""):
    return {"name": name, "status": status, "duration": duration,
            "detail": detail, "errors": dedupe_lines(output) if status == "FAIL" else []}


# ---------------------------------------------------------------- detection
def detect_project(project_dir):
    """Return a dict describing what was found in the project directory."""
    info = {"node": False, "scripts": {}, "python_tests": False,
            "static_index": False}
    pkg = os.path.join(project_dir, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg, errors="replace") as f:
                data = json.load(f)
            info["node"] = True
            info["scripts"] = data.get("scripts") or {}
        except (OSError, json.JSONDecodeError):
            info["node"] = True
    if os.path.isfile(os.path.join(project_dir, "index.html")):
        info["static_index"] = True
    tests_dir = os.path.join(project_dir, "tests")
    has_tests = os.path.isdir(tests_dir) or any(
        n.startswith("test_") and n.endswith(".py")
        for n in os.listdir(project_dir)) if os.path.isdir(project_dir) else False
    has_pytest_cfg = any(os.path.isfile(os.path.join(project_dir, n))
                         for n in ("pytest.ini", "pyproject.toml",
                                   "setup.cfg", "tox.ini"))
    info["python_tests"] = bool(has_tests and
                                (has_pytest_cfg or has_tests))
    info["python"] = any(n.endswith(".py") for n in os.listdir(project_dir)) \
        if os.path.isdir(project_dir) else False
    return info


# ---------------------------------------------------------------- checks
def node_checks(project_dir, info):
    checks = []
    if not info["node"]:
        return checks
    for script in NODE_CHECK_ORDER:
        if script not in info["scripts"]:
            continue  # missing script = never a check at all (not even SKIP)
        code, out, dur, timed_out = run_cmd(["npm", "run", "--silent", script],
                                            cwd=project_dir)
        if code is None:
            checks.append(make_check(f"npm run {script}", "SKIP", dur,
                                     "npm not available or command timed out"
                                     if timed_out else "npm not available"))
        elif code == 0:
            checks.append(make_check(f"npm run {script}", "PASS", dur,
                                     "exit 0"))
        else:
            checks.append(make_check(f"npm run {script}", "FAIL", dur,
                                     f"exit {code}", out))
    return checks


def python_checks(project_dir, info):
    checks = []
    if not info["python_tests"]:
        return checks
    code, out, dur, timed_out = run_cmd(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        cwd=project_dir)
    if code is None:
        checks.append(make_check("pytest", "SKIP", dur,
                                 "pytest could not be run"))
    elif "No module named pytest" in out:
        checks.append(make_check("pytest", "SKIP", dur, "pytest not installed"))
    elif code == 0:
        checks.append(make_check("pytest", "PASS", dur, "exit 0"))
    elif code == 5:
        checks.append(make_check("pytest", "SKIP", dur, "no tests collected"))
    else:
        checks.append(make_check("pytest", "FAIL", dur, f"exit {code}", out))
    return checks


# ---------------------------------------------------------------- web check
_TAG = re.compile(r"<[^>]+>")
_MEDIA = re.compile(r"(?i)<(img|video|canvas|svg|audio|input|button|a)\b")


def visible_text(html):
    body = html
    m = re.search(r"(?is)<body[^>]*>(.*?)</body>", html)
    if m:
        body = m.group(1)
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    text = _TAG.sub(" ", body)
    return re.sub(r"\s+", " ", text).strip()


def analyze_html(html, source):
    """White-screen heuristics over one HTML document.

    Returns (status, detail, warnings). FAIL = white screen / near-empty.
    A missing <title> is a warning, never a failure on its own.
    """
    warnings = []
    if not re.search(r"(?i)<title[^>]*>\s*\S", html or ""):
        warnings.append("missing or empty <title>")
    text = visible_text(html or "")
    has_media = bool(_MEDIA.search(html or ""))
    if len(text) < 10 and not has_media:
        return "FAIL", (f"white screen suspected: only {len(text)} chars of "
                        f"visible text and no interactive/media elements "
                        f"({source})"), warnings
    if len(text) < 10:
        return "PASS", (f"very little visible text ({len(text)} chars) but "
                        f"media/interactive elements present ({source})"), warnings
    return "PASS", f"{len(text)} chars of visible text ({source})", warnings


def fetch_url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "proof-cli"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return resp.status, resp.read(1_000_000).decode(errors="replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(1_000_000).decode(errors="replace")
        except Exception:
            return e.code, ""
    except (urllib.error.URLError, OSError) as e:
        return None, str(e)


def web_checks(project_dir, info, url=None, report_dir=None):
    checks, warnings = [], []
    html, source, status_code = None, None, None
    if url:
        status_code, html = fetch_url(url)
        source = url
        if status_code is None:
            checks.append(make_check("web page", "FAIL", 0.0,
                                     f"could not fetch {url}: {html}"))
            return checks, warnings
    elif info["static_index"]:
        path = os.path.join(project_dir, "index.html")
        try:
            with open(path, errors="replace") as f:
                html = f.read(1_000_000)
            source = "index.html"
        except OSError as e:
            checks.append(make_check("web page", "FAIL", 0.0, str(e)))
            return checks, warnings
    else:
        return checks, warnings

    if status_code is not None and status_code >= 400:
        checks.append(make_check("web page", "FAIL", 0.0,
                                 f"HTTP {status_code} from {source}"))
        return checks, warnings
    status, detail, warns = analyze_html(html, source)
    warnings.extend(warns)
    checks.append(make_check("web page (white-screen)", status, 0.0, detail))

    # Optional: Playwright screenshots + console errors, only when the
    # caller gave a URL and playwright happens to be importable.
    if url:
        checks.append(playwright_check(url, report_dir))
    else:
        checks.append(make_check(
            "web screenshots (playwright)", "SKIP", 0.0,
            "no --url given; static heuristics only"))
    return checks, warnings


def playwright_check(url, report_dir):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return make_check("web screenshots (playwright)", "SKIP", 0.0,
                          "playwright not installed — screenshots skipped "
                          "(core checks are stdlib-only)")
    start = time.monotonic()
    console_errors = []
    try:
        shot_dir = os.path.join(report_dir or ".", "proof-screenshots")
        os.makedirs(shot_dir, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for label, viewport in (("desktop", {"width": 1280, "height": 800}),
                                    ("mobile", {"width": 390, "height": 844})):
                page = browser.new_page(viewport=viewport)
                page.on("console", lambda msg: console_errors.append(msg.text)
                        if msg.type == "error" else None)
                page.on("pageerror", lambda err: console_errors.append(str(err)))
                page.goto(url, timeout=15000)
                page.screenshot(path=os.path.join(shot_dir, f"{label}.png"))
                page.close()
            browser.close()
    except Exception as e:  # browser missing, page unreachable, ...
        return make_check("web screenshots (playwright)", "SKIP",
                          time.monotonic() - start,
                          f"playwright present but run failed: {e}")
    dur = time.monotonic() - start
    if console_errors:
        return make_check("web screenshots (playwright)", "FAIL", dur,
                          f"{len(console_errors)} console error(s); "
                          "screenshots in proof-screenshots/",
                          "\n".join(console_errors))
    return make_check("web screenshots (playwright)", "PASS", dur,
                      "desktop+mobile screenshots in proof-screenshots/, "
                      "no console errors")


# ---------------------------------------------------------------- report
def run_all_checks(project_dir, url=None, report_dir=None):
    project_dir = os.path.abspath(project_dir)
    info = detect_project(project_dir)
    checks = []
    checks.extend(node_checks(project_dir, info))
    checks.extend(python_checks(project_dir, info))
    web, warnings = web_checks(project_dir, info, url=url,
                               report_dir=report_dir)
    checks.extend(web)
    return info, checks, warnings


def verdict_of(checks):
    if any(c["status"] == "FAIL" for c in checks):
        return "FAIL"
    if not any(c["status"] == "PASS" for c in checks):
        return "FAIL"  # nothing actually ran — that is not proof
    return "PASS"


def build_report(project_dir, url=None, report_dir=None):
    project_dir = os.path.abspath(project_dir)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    info, checks, warnings = run_all_checks(project_dir, url=url,
                                            report_dir=report_dir)
    verdict = verdict_of(checks)
    failed = [c for c in checks if c["status"] == "FAIL"]
    kinds = []
    if info["node"]:
        kinds.append("Node")
    if info["python"]:
        kinds.append("Python")
    if info["static_index"]:
        kinds.append("static site")

    out = [
        "# proof report",
        f"Generated: {now} · proof-cli v{VERSION} · project: {project_dir}",
        f"Detected: {', '.join(kinds) if kinds else 'unknown project type'}",
        "",
        f"## Verdict: {verdict}",
        "",
        "## Checks",
        "| Check | Status | Time | Detail |",
        "|---|---|---|---|",
    ]
    for c in checks:
        detail = c["detail"].replace("|", "/")
        out.append(f"| {c['name']} | {c['status']} | "
                   f"{c['duration']:.1f}s | {detail} |")
    if not checks:
        out.append("| (none) | SKIP | 0.0s | no checks detected in project |")

    if warnings:
        out += ["", "## Warnings"]
        out += [f"- {w}" for w in warnings]

    if failed:
        out += ["", "## Failures (deduplicated)"]
        for c in failed:
            out.append(f"### {c['name']} — {c['detail']}")
            if c["errors"]:
                for count, sample in c["errors"]:
                    prefix = f"×{count} " if count > 1 else ""
                    out.append(f"- {prefix}`{sample}`")
            out.append("")
        out += ["## Fix these first"]
        for i, c in enumerate(failed, 1):
            first = c["errors"][0][1] if c["errors"] else c["detail"]
            out.append(f"{i}. **{c['name']}**: {first}")
    elif verdict == "PASS":
        out += ["", "All run checks passed. Safe to report \"done\" — "
                "with this report attached."]
    else:
        out += ["", "No check ran, so nothing was proved. Add a build/test "
                "script or a page to check."]
    out.append("")
    return "\n".join(out), verdict


# ---------------------------------------------------------------- MCP
def mcp_server():
    """Minimal stdio MCP server exposing one tool: proof_check."""
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "proof-cli", "version": VERSION}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
                "name": "proof_check",
                "description": "Prove a project works: run its existing "
                               "build/lint/typecheck/test checks and "
                               "white-screen heuristics, return one "
                               "PASS/FAIL proof-report.md.",
                "inputSchema": {"type": "object",
                                "properties": {
                                    "project_dir": {"type": "string"},
                                    "url": {"type": "string"}},
                                "required": ["project_dir"]}}]}})
        elif method == "tools/call":
            args = req.get("params", {}).get("arguments") or {}
            report, _ = build_report(args.get("project_dir", "."),
                                     url=args.get("url"))
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                {"type": "text", "text": report}]}})
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "method not found"}})


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="proof",
        description="Make AI agents prove their work before saying \"done\".")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="Run all detected checks, write a report")
    c.add_argument("project_dir", help="Project directory to check")
    c.add_argument("--out", default=None,
                   help="Report path (default: <project_dir>/proof-report.md)")
    c.add_argument("--url", default=None,
                   help="Also check this live URL (white-screen heuristics, "
                        "plus screenshots if playwright is installed)")
    sub.add_parser("mcp", help="Run the stdio MCP server")
    args = ap.parse_args(argv)

    if args.cmd == "mcp":
        mcp_server()
        return 0
    out_path = args.out or os.path.join(os.path.abspath(args.project_dir),
                                        "proof-report.md")
    report, verdict = build_report(args.project_dir, url=args.url,
                                   report_dir=os.path.dirname(
                                       os.path.abspath(out_path)))
    with open(out_path, "w") as f:
        f.write(report)
    print(f"{verdict}: wrote {out_path} ({len(report.splitlines())} lines)")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
