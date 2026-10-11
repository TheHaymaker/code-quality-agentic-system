#!/usr/bin/env python3
"""A stand-in for the Gemini CLI (`gemini -m <model> -o json -p ...`), used by
the engine tests. It answers in Gemini's JSON envelope, with the answer as a
string in `response` and the model that ran under `stats.models`.

Scenario (FAKE_GEMINI_SCENARIO), optional:
  findings:  [finding, ...]   reported on the first call only
  ran_model: "<model id>"     pretend the CLI ran another model
"""
import fcntl
import json
import os
import sys

argv = sys.argv[1:]
model = argv[argv.index("-m") + 1]
brief = sys.stdin.read()
try:
    scenario = json.load(open(os.environ.get("FAKE_GEMINI_SCENARIO", "")))
except (OSError, ValueError):
    scenario = {}
state_path = os.environ["FAKE_GEMINI_STATE"]
with open(state_path + ".lock", "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        state = json.load(open(state_path))
    except (OSError, ValueError):
        state = {"calls": 0, "envs": []}
    state["calls"] += 1
    state["envs"].append({k: os.environ.get(k) for k in ("GEMINI_CLI_TRUST_WORKSPACE", "GEMINI_CLI_SYSTEM_SETTINGS_PATH")})
    state.setdefault("argv", []).append(argv)
    state.setdefault("cwd", []).append(os.getcwd())
    state.setdefault("files", []).append(sorted(
        os.path.relpath(os.path.join(d, f), ".") for d, _, fs in os.walk(".") for f in fs))
    state.setdefault("has_diff", []).append("```diff\n" in brief)
    json.dump(state, open(state_path + ".tmp", "w"))
    os.replace(state_path + ".tmp", state_path)

files, on = [], False
for line in brief.splitlines():
    if line.startswith("Changed files ("):
        on = True
        continue
    if on:
        if line.startswith("- ") and line != "- (none)":
            files.append(line[2:])
        elif line.strip():
            on = False
answer = {"scope": "full", "files_reviewed": files, "findings": scenario.get("findings", []) if state["calls"] == 1 else []}
text = "Here is my review.\n```json\n%s\n```" % json.dumps(answer)
print(json.dumps({"session_id": "s", "response": text,
                  "stats": {"models": {scenario.get("ran_model", model): {"tokens": {"total": 10}}}}}))
