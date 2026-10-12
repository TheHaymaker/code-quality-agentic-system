#!/usr/bin/env python3
"""A stand-in for `claude -p`, used by the engine tests.

It reads the role from the system prompt's `cq-role:` line, does what a
scenario file says that role does (write files, commit, report findings), and
prints a result object shaped like Claude Code's `--output-format json`.

Scenario (JSON file in FAKE_CLAUDE_SCENARIO), all keys optional:
  screener:   the screening answer (merged over a ready default)
  plan_reviews: ["PLAN OK" | "PLAN NEEDS CHANGES", ...]  one per call
  implement:  {path: content}            files the implementer writes
  reviews:    {reviewer: [answer, ...]}  one per call; answer = {"findings": [...], "not_fixed": "all" | [...]}
  verify:     {"<claim text>": "reproduced" | "not_reproduced"}
  aggregate:  {"<claim text>": "fix" | "file" | "reject" | "contest" | "leave"}
  triage:     {"<claim text>": {"severity": ..., "category": ...}}
  fixes:      {"<claim text>": {path: content}}   default: a marker file per finding
  fix_checks: {check: {path: content | null}}     null deletes the file
  fail:       {role: n}                           the first n calls of that role fail
  rewrite_history: true                           the fixer squashes its commit into the one before
"""
import fcntl
import json
import os
import re
import subprocess
import sys


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1]
system = argv[argv.index("--append-system-prompt") + 1]
role = re.match(r"cq-role: (\S+)", system).group(1)
stdin = sys.stdin.read()
scenario = load(os.environ.get("FAKE_CLAUDE_SCENARIO", ""), {})
state_path = os.environ["FAKE_CLAUDE_STATE"]
# Reviewers run in parallel: serialise the read-modify-write of the call log.
with open(state_path + ".lock", "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = load(state_path, {"calls": {}, "log": []})
    n = state["calls"].get(role, 0)
    state["calls"][role] = n + 1
    state["log"].append({"role": role, "argv": argv, "cwd": os.getcwd(), "stdin": stdin, "path": os.environ.get("PATH", "")})
    with open(state_path + ".tmp", "w") as fh:
        json.dump(state, fh)
    os.replace(state_path + ".tmp", state_path)
ticket = (re.search(r"^# Ticket ([A-Z]+-\d+)", stdin, re.M) or [None, None])[1]


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def write_files(files):
    for path, content in files.items():
        if content is None:
            if os.path.exists(path):
                os.remove(path)
            continue
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)


def commit(message):
    git("add", "-A")
    if git("status", "--porcelain"):
        git("commit", "-q", "-m", message)
    return git("rev-parse", "HEAD")


def match(table, text, default=None):
    for key, value in (table or {}).items():
        if key in (text or ""):
            return value
    return default


STREAM = argv[argv.index("--output-format") + 1] == "stream-json"
SESSION = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "fake-session"


def emit(event):
    if STREAM:
        print(json.dumps(event), flush=True)


emit({"type": "system", "subtype": "init", "session_id": SESSION, "model": argv[argv.index("--model") + 1],
      "tools": ["Read", "Bash"]})
emit({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Working as %s." % role},
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git diff --stat"}}]}})
emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1 file changed"}]}})


def reply(output, error=None):
    if error:
        result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "errors": [error], "total_cost_usd": 0.001, "session_id": SESSION}
        print(json.dumps(result))
        sys.exit(1)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                      "structured_output": output, "total_cost_usd": 0.01, "num_turns": 3, "session_id": SESSION,
                      "modelUsage": {argv[argv.index("--model") + 1]: {"costUSD": 0.01}}}))
    sys.exit(0)


if role == "implementer" and scenario.get("background_child"):
    # Like `npm run dev &`: a child that outlives the session and holds its output pipes.
    subprocess.Popen(["sleep", "60"])

if n < scenario.get("fail", {}).get(role, 0):
    reply(None, "scripted failure of %s" % role)

if role == "screener":
    out = {"checks": {c: {"pass": True, "note": "ok"} for c in
                      ["testable", "bounded", "sized", "located", "unambiguous", "independent"]},
           "result": "ready", "spec": "Make page numbers start at 1.",
           "acceptance_criteria": ["Given page 0, when a page is requested, then page 1 is returned."],
           "assumptions": [], "paths": ["src/app.py"], "question": None}
    out.update(scenario.get("screener", {}))
    out.update(scenario.get("screener_by_ticket", {}).get(ticket, {}))
    reply(out)

if role == "planner":
    revision = " (revised)" if "# Problems the plan review found" in stdin else ""
    reply({"plan": "## Goal\nClamp the page%s.\n## Approach\nOne slice.\n## Files\nsrc/app.py\n## Tests\nOne.\n## Risks\nNone." % revision,
           "rollback": "Revert the merge commit.", "paths": ["src/app.py"]})

if role == "plan_reviewer":
    verdicts = scenario.get("plan_reviews", [])
    verdict = verdicts[n] if n < len(verdicts) else "PLAN OK"
    problems = [] if verdict == "PLAN OK" else [{"problem": "No test for page 0", "evidence": "plan", "change": "add one"}]
    reply({"verdict": verdict, "problems": problems})

if role == "implementer":
    if not scenario.get("implementer_no_commit"):
        files = scenario.get("implement_by_ticket", {}).get(ticket) or scenario.get("implement", {"src/app.py": "def page(n):\n    return max(n, 1)\n"})
        write_files(files)
        commit("Implement the ticket")
    reply({"summary": "Implemented.", "deviations": []})

if role == "test_author":
    write_files({"tests/test_feature.py": "def test_page_zero():\n    assert True\n"})
    commit("Add tests for the acceptance criteria")
    reply({"summary": "Tests added.", "deviations": []})

if role.startswith("reviewer:"):
    reviewer = role.split(":", 1)[1]
    files, in_files = [], False
    for line in stdin.splitlines():
        if line.startswith("Changed files ("):
            in_files = True
            continue
        if in_files:
            if line.startswith("- ") and line != "- (none)":
                files.append(line[2:])
            elif line.strip():
                in_files = False
    presented = re.findall(r"^- (f-[0-9a-f]+|duo-\S+|sonar-\S+|gg-[0-9a-f]+) \(", stdin, re.M)
    answers = scenario.get("reviews", {}).get(reviewer, [])
    answer = answers[n] if n < len(answers) else {}
    if answer == "invalid":
        reply({"scope": "full", "files_reviewed": [], "findings": []})
    not_fixed = answer.get("not_fixed", [])
    if not_fixed == "all":
        not_fixed = presented
    out = {"scope": "full", "files_reviewed": files, "findings": answer.get("findings", [])}
    if not_fixed:
        out["not_fixed"] = not_fixed
    reply(out)

if role == "verifier":
    items = json.loads(stdin.split("\n", 2)[2])
    reply({"results": [{"id": f["id"], "result": match(scenario.get("verify"), f["claim"], "reproduced"),
                        "evidence": "test_reproduces fails as described"} for f in items]})

if role == "aggregator":
    found = json.loads(stdin.split("# Open findings", 1)[1])
    triage, outcomes, order = [], [], []
    for f in found:
        severity = f["severity"]
        if severity == "UNTRIAGED":
            t = match(scenario.get("triage"), f["claim"], {"severity": "MINOR", "category": "structure"})
            triage.append({"id": f["id"], "severity": t["severity"], "category": t["category"],
                           "scenario": "as the comment says", "proposed_fix": "change it", "always_blocking": None})
            severity = t["severity"]
        if f["status"] == "contested":
            continue
        default = "fix" if severity in ("BLOCKER", "MAJOR") else "leave"
        action = match(scenario.get("aggregate"), f["claim"], default)
        outcomes.append({"id": f["id"], "action": action, "reason": "scripted %s" % action})
        if action == "fix":
            order.append(f["id"])
    reply({"triage": triage, "merges": [], "outcomes": outcomes, "fix_order": order})

if role == "fixer":
    fixed = []
    if "# Findings to fix" in stdin:
        block = stdin.split("# Findings to fix, in this order", 1)[1].split("\n# ", 1)[0]
        for f in json.loads(block):
            write_files(match(scenario.get("fixes"), f["claim"], {"fix_%s.txt" % f["id"]: "fixed\n"}))
            sha = commit("Fix %s" % f["id"])
            if scenario.get("rewrite_history"):
                git("reset", "--soft", "HEAD~2")
                sha = commit("Squashed fix %s" % f["id"])
            fixed.append({"id": f["id"], "commit": sha, "summary": "fixed it"})
    for name, files in scenario.get("fix_checks", {}).items():
        if "# Failed check: %s" % name in stdin:
            write_files(files)
            commit("Fix the %s check" % name)
    reply({"fixed": fixed, "not_fixed": []})

reply(None, "fake claude: unknown role %s" % role)
