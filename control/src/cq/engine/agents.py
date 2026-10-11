"""Starting one agent: a headless Claude Code session (`claude -p`).

Each call is a fresh session with its own model, effort and permissions. The
agent answers with JSON that matches a schema (`--json-schema`); the engine
reads `structured_output` and nothing else from it. Every session is logged
to the run store, with its cost.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config
from ..util import CqError, iso, write_json

READ_TOOLS = "Read,Grep,Glob,Bash,Skill"
GIT_READ = ["Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git status)", "Bash(git status *)"]
GIT_WRITE = ["Bash(git add *)", "Bash(git commit *)"]
# Reviewed commits stay as they are. The engine also checks this after each session.
# The guard hook (policy/claude-engine.json) also blocks these anywhere in a command.
DENIED = ["Bash(git commit --amend)", "Bash(git commit --amend *)", "Bash(git push *)", "Bash(git reset *)",
          "Bash(git rebase *)", "Bash(git diff --output*)", "Bash(git log --output*)", "Bash(git show --output*)"]


class AgentError(CqError):
    """A session failed: it crashed, timed out, ran out of turns, or gave no usable answer.
    It carries the money spent on the failed attempts."""

    def __init__(self, message: str, cost_usd: float = 0.0):
        super().__init__(message)
        self.cost_usd = cost_usd


def _check_rules(checks: Dict[str, List[str]]) -> List[str]:
    """Permission rules that let a writing agent run the registered checks."""
    rules = []
    for argv in checks.values():
        command = " ".join(argv)
        rules += ["Bash(%s)" % command, "Bash(%s *)" % command]
    return rules


class ClaudeRunner:
    def __init__(self, settings: Dict[str, Any]):
        self.settings = settings["claude"]

    def argv(
        self,
        role: str,
        spec: Dict[str, Any],
        system: str,
        prompt: str,
        mode: str,
        schema: Dict[str, Any],
        checks: Dict[str, List[str]],
    ) -> List[str]:
        s = self.settings
        # Settings come from the user's own files and from cq, never from the
        # repository under review: its .claude/ and .mcp.json are part of the change.
        engine_settings = config.control_dir() / "policy" / "claude-engine.json"
        argv = [
            s["command"], "-p", prompt,
            "--output-format", "json",
            "--model", spec["model"],
            "--effort", spec["effort"],
            "--max-turns", str(s["max_turns"].get(role.split("-")[0], 80)),
            "--no-session-persistence",
            "--setting-sources", "user",
            "--settings", str(engine_settings),
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--append-system-prompt", system,
            "--json-schema", json.dumps(schema),
        ]
        if s.get("max_budget_usd"):
            argv += ["--max-budget-usd", str(s["max_budget_usd"])]
        if mode == "read":
            # Anything not allowed here is denied without a prompt.
            argv += ["--permission-mode", "dontAsk", "--tools", READ_TOOLS, "--allowedTools", *GIT_READ, "Skill"]
        elif mode == "write":
            allowed = GIT_READ + GIT_WRITE + _check_rules(checks) + list(s.get("extra_allowed_tools", []))
            argv += ["--permission-mode", "acceptEdits", "--allowedTools", *allowed]
        else:
            raise ValueError(mode)
        argv += ["--disallowedTools", *DENIED]
        return argv

    def run(
        self,
        run_dir: Path,
        role: str,
        spec: Dict[str, Any],
        *,
        system: str,
        prompt: str,
        stdin: str,
        cwd: Path,
        mode: str,
        schema: Dict[str, Any],
        checks: Optional[Dict[str, List[str]]] = None,
        attempts: int = 2,
    ) -> Dict[str, Any]:
        """Run one session and return {"output": <structured answer>, "cost_usd": float}.
        A failed session is tried once more; then AgentError."""
        if not (config.control_dir() / "policy" / "claude-engine.json").exists():
            raise AgentError("policy/claude-engine.json is missing from the control repo. It carries the guard "
                             "hook for engine sessions; reinstall cq to restore it.")
        argv = self.argv(role, spec, system, prompt, mode, schema, checks or {})
        last = None
        cost = 0.0
        # The guard hook runs `cq guard`: put this cq first on the session's PATH.
        path = os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")])
        for attempt in range(1, attempts + 1):
            started = time.time()
            env = dict(os.environ, CQ_ACTOR=role, PATH=path)
            record: Dict[str, Any] = {"role": role, "model": spec["model"], "effort": spec["effort"], "mode": mode,
                                      "cwd": str(cwd), "attempt": attempt, "started_at": iso(), "argv": argv, "stdin": stdin}
            try:
                code, stdout, stderr = run_group(argv, stdin, cwd, env, self.settings["timeout_seconds"])
                record.update(exit_code=code, stdout=stdout, stderr=stderr)
                outcome = self._parse(code, stdout, stderr, schema)
            except FileNotFoundError:
                raise AgentError("Claude Code is not installed or not on PATH (%s)" % argv[0])
            except subprocess.TimeoutExpired:
                outcome = {"error": "timed out after %d seconds" % self.settings["timeout_seconds"], "cost_usd": 0.0}
            record.update(duration_seconds=round(time.time() - started, 1), outcome=outcome)
            _log(run_dir, role, record)
            cost += outcome.get("cost_usd") or 0.0
            if "error" not in outcome:
                return {"output": outcome["output"], "cost_usd": cost, "models": outcome.get("models", [])}
            last = outcome["error"]
        raise AgentError("%s session failed twice: %s" % (role, last), cost_usd=cost)

    @staticmethod
    def _parse(code: int, stdout: str, stderr: str, schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            result = json.loads(stdout)
        except ValueError:
            return {"error": "exit %s, no JSON result: %s" % (code, (stderr or stdout).strip()[:400]), "cost_usd": 0.0}
        if not isinstance(result, dict):
            return {"error": "unexpected output", "cost_usd": 0.0}
        cost = float(result.get("total_cost_usd") or 0.0)
        if result.get("is_error") or result.get("subtype") != "success":
            detail = result.get("errors") or result.get("result") or result.get("subtype")
            return {"error": "%s: %s" % (result.get("subtype"), str(detail)[:400]), "cost_usd": cost}
        output = result.get("structured_output")
        if not isinstance(output, dict):
            return {"error": "the session gave no structured answer", "cost_usd": cost}
        if schema is not None:
            # Claude Code validates too; the engine does not rely on it.
            try:
                config.validate(output, schema, "the structured answer")
            except CqError as exc:
                return {"error": str(exc)[:600], "cost_usd": cost}
        models = sorted((result.get("modelUsage") or {}).keys()) if isinstance(result.get("modelUsage"), dict) else []
        return {"output": output, "cost_usd": cost, "models": models}


def run_group(argv: List[str], stdin: str, cwd: Path, env: Dict[str, str], timeout: int):
    """Run a command in its own process group. On a timeout the whole group is
    killed, so nothing the session started (tests, a commit) keeps running in the
    worktree while the next attempt starts."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(cwd),
                            env=env, text=True, encoding="utf-8", errors="replace", start_new_session=True)
    try:
        stdout, stderr = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        raise
    return proc.returncode, stdout, stderr


def _log(run_dir: Path, role: str, record: Dict[str, Any]) -> None:
    adir = run_dir / "agents"
    adir.mkdir(parents=True, exist_ok=True)
    seq = len(list(adir.glob("*.json"))) + 1
    write_json(adir / ("%03d-%s.json" % (seq, role)), record)
