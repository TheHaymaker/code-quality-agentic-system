"""Starting one agent: a headless Claude Code session (`claude -p`).

Each call is a fresh session with its own model, effort and permissions. The
agent answers with JSON that matches a schema (`--json-schema`); the engine
reads `structured_output` and nothing else from it. Every session is logged
to the run store, with its cost.

Sessions stream (`--output-format stream-json`). Each event is appended to
`agents/<time>-<role>-<attempt>.stream.jsonl` as it arrives, so `cq engine
watch` and the dashboard can show a session while it works. The last event is
the same result object the plain JSON output gives.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
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
        session_id: Optional[str] = None,
    ) -> List[str]:
        s = self.settings
        # Settings come from the user's own files and from cq, never from the
        # repository under review: its .claude/ and .mcp.json are part of the change.
        engine_settings = config.control_dir() / "policy" / "claude-engine.json"
        # With keep_sessions, Claude Code saves the session and `claude --resume <id>` opens it later.
        persistence = ["--session-id", session_id] if session_id else ["--no-session-persistence"]
        argv = [
            s["command"], "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--model", spec["model"],
            "--effort", spec["effort"],
            "--max-turns", str(s["max_turns"].get(role.split("-")[0], 80)),
            *persistence,
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
        last = None
        cost = 0.0
        # The guard hook runs `cq guard`: put this cq first on the session's PATH.
        path = os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")])
        for attempt in range(1, attempts + 1):
            session_id = str(uuid.uuid4()) if self.settings.get("keep_sessions") else None
            argv = self.argv(role, spec, system, prompt, mode, schema, checks or {}, session_id=session_id)
            started = time.time()
            env = dict(os.environ, CQ_ACTOR=role, PATH=path)
            name = session_name(role, attempt)
            stream = Stream(run_dir / "agents" / (name + ".stream.jsonl"))
            stream.write({"type": "cq_session", "role": role, "model": spec["model"], "effort": spec["effort"],
                          "mode": mode, "attempt": attempt, "cwd": str(cwd), "session_id": session_id})
            record: Dict[str, Any] = {"role": role, "model": spec["model"], "effort": spec["effort"], "mode": mode,
                                      "cwd": str(cwd), "attempt": attempt, "started_at": iso(), "argv": argv,
                                      "stdin": stdin, "stream": str(stream.path), "session_id": session_id}
            try:
                code, result_line, stderr = run_streaming(argv, stdin, cwd, env, self.settings["timeout_seconds"], stream)
                record.update(exit_code=code, stdout=result_line, stderr=stderr)
                outcome = self._parse(code, result_line, stderr, schema)
            except FileNotFoundError:
                stream.write({"type": "cq_end", "ok": False, "error": "claude not found"})
                raise AgentError("Claude Code is not installed or not on PATH (%s)" % argv[0])
            except subprocess.TimeoutExpired:
                outcome = {"error": "timed out after %d seconds" % self.settings["timeout_seconds"], "cost_usd": 0.0}
            record.update(duration_seconds=round(time.time() - started, 1), outcome=outcome)
            stream.write({"type": "cq_end", "ok": "error" not in outcome, "error": outcome.get("error"),
                          "cost_usd": outcome.get("cost_usd") or 0.0, "duration_seconds": record["duration_seconds"],
                          "session_id": session_id})
            _log(run_dir, name, record)
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


def session_name(role: str, attempt: int = 1) -> str:
    """Log names sort by start time. Parallel reviewers never share a name."""
    now_ = time.time()
    return "%s%03d-%s-%d" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime(now_)), int(now_ * 1000) % 1000,
                             role.replace(":", "-"), attempt)


class Stream:
    """An append-only JSON Lines file of one session's events, each with the time it arrived."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: Any) -> None:
        line = json.dumps({"at": iso(), "event": event}, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def run_streaming(argv: List[str], stdin: str, cwd: Path, env: Dict[str, str], timeout: int, stream: Stream):
    """Run `claude -p --output-format stream-json` in its own process group.
    Every event goes to the stream file as it arrives. Returns the exit code,
    the final result line and stderr. On a timeout the whole group is killed."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(cwd),
                            env=env, text=True, encoding="utf-8", errors="replace", start_new_session=True)
    result: List[str] = []
    errors: List[str] = []

    def feed() -> None:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def read_out() -> None:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                stream.write({"type": "cq_text", "text": line[:2000]})
                continue
            stream.write(event)
            if isinstance(event, dict) and event.get("type") == "result":
                result.append(line)

    def read_err() -> None:
        errors.append(proc.stderr.read())

    threads = [threading.Thread(target=f, daemon=True) for f in (feed, read_out, read_err)]
    for t in threads:
        t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.wait()
        _close(proc, threads, 5)
        stream.write({"type": "cq_text", "text": "killed after %d seconds" % timeout})
        raise
    # Anything the session left running in the background (a dev server, a
    # watcher) would hold the pipes open and keep the worktree busy. It goes too.
    _kill_group(proc)
    _close(proc, threads, 30)
    return proc.returncode, (result[-1] if result else ""), "".join(errors)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


def _close(proc: subprocess.Popen, threads: List[threading.Thread], wait: float) -> None:
    for t in threads:
        t.join(wait)
    if any(t.is_alive() for t in threads):
        # A process outside the group still holds a pipe. Closing it now would
        # block on the reader; the reader threads are daemons and end with it.
        return
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if pipe:
                pipe.close()
        except OSError:
            pass


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


def _log(run_dir: Path, name: str, record: Dict[str, Any]) -> None:
    adir = run_dir / "agents"
    adir.mkdir(parents=True, exist_ok=True)
    write_json(adir / ("%s.json" % name), record)
