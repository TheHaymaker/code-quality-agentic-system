"""The gate-protection hook for Claude Code (PreToolUse).

Exit 0 allows the tool call. Exit 2 blocks it and shows stderr to the agent.
Any internal error also exits 2: a guard that cannot decide must not allow.

This hook is a second line of defence. The first is the diff: a change to a
protected path that gets past this hook still shows in the diff, raises the run
to tier 3 and needs a human sign-off.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import config
from .util import glob_match

FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# The parts of CQ_HOME that hold evidence. Other folders there (worktrees) are workspaces.
STORE_DIRS = ("runs", "intake")
# Deliberately loose: a path or quotes in front of cq, or `python -m cq.cli`, still match.
HUMAN_ONLY = re.compile(
    r"\bcq(?:\.cli)?['\"]?\s+(?:approve\b|decide\b|tier\s+lower\b|fix\s+extend\b|run\s+close\b|run\s+start\b[^;&|]*--new\b)"
)
REDIRECT = re.compile(r"\bCQ_(?:CONTROL|HOME)\b")
# Inside an engine session (the engine sets CQ_ACTOR) an agent may read evidence
# with cq, and nothing else: the engine records everything itself.
CQ_COMMAND = re.compile(r"\bcq(?:\.cli)?['\"]?\s+([a-z-]+)(?:\s+([a-z-]+))?")
# In an engine session, git may not write files through a read command, and reviewed commits stay as they are.
GIT_OUTPUT = re.compile(r"\bgit\b[^;&|\n]*\s--output\b")
GIT_REWRITE = re.compile(r"\bgit\b[^;&|\n]*\scommit\b[^;&|\n]*\s--amend\b|\bgit\s+(?:reset|rebase|push|filter-branch|replace|update-ref)\b")
ENGINE_READ_ONLY = {("brief", None), ("findings", "list"), ("findings", "show"), ("run", "show"), ("run", "list"),
                    ("check", "log"), ("models", None), ("verdict", None), ("report", None), ("waivers", None)}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _project_root(cwd: Path) -> Path:
    proc = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(proc.stdout.strip()).resolve() if proc.returncode == 0 and proc.stdout.strip() else cwd


def decide(payload: Dict[str, Any]) -> Optional[str]:
    """Return a reason to block, or None to allow."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    control = config.control_dir()
    home = config.cq_home()

    if tool in FILE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
        if not raw:
            return None
        target = Path(raw).expanduser()
        target = (target if target.is_absolute() else cwd / target).resolve()
        if _inside(target, control):
            return "%s is in the quality control repo. Only a human changes policy." % target
        if any(_inside(target, home / d) for d in STORE_DIRS):
            return "%s is in the run store. Use cq commands to record evidence." % target
        # The project is the one the target file is in, which may be another
        # worktree than the session's directory.
        anchor = target.parent
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        root = _project_root(anchor)
        if _inside(target, root):
            rel = target.relative_to(root).as_posix()
            floor = config.parse_floor((control / "policy" / "floor.yml").read_text(encoding="utf-8"))
            for glob in floor["protected"]:
                if glob_match(glob, rel):
                    return "%s is a protected path (%s). It defines a quality gate, so a human edits it." % (rel, glob)
        return None

    if tool == "Bash":
        command = tool_input.get("command") or ""
        if HUMAN_ONLY.search(command):
            return "That cq command records a human decision. Stop and ask the human to run it in their own terminal."
        if REDIRECT.search(command):
            return "CQ_CONTROL and CQ_HOME choose the policy and the run store. An agent does not set them."
        if os.environ.get("CQ_ACTOR"):
            if GIT_OUTPUT.search(command):
                return "git --output writes a file. In a cq engine session, read the diff on stdout instead."
            if GIT_REWRITE.search(command):
                return ("A cq engine session adds commits and never rewrites or pushes them. "
                        "Make a new commit instead; the engine pushes.")
            for m in CQ_COMMAND.finditer(command):
                pair = (m.group(1), m.group(2))
                if pair not in ENGINE_READ_ONLY and (pair[0], None) not in ENGINE_READ_ONLY:
                    return ("This session belongs to a cq engine run. The engine records evidence itself; "
                            "answer in your JSON instead of running `cq %s`." % " ".join(p for p in pair if p))
        for root in [control] + [home / d for d in STORE_DIRS]:
            names = {str(root)}
            try:
                names.add("~/" + root.relative_to(Path.home()).as_posix())
                names.add("$HOME/" + root.relative_to(Path.home()).as_posix())
            except ValueError:
                pass
            if any(n in command for n in names):
                return (
                    "Shell commands may not touch %s. Use cq commands (cq run show, cq findings list, cq check log)." % root
                )
        return None
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        reason = decide(payload)
    except Exception as exc:  # fail closed
        print("cq guard could not decide and is blocking this call: %s" % exc, file=sys.stderr)
        return 2
    if reason:
        print("Blocked by cq guard: " + reason, file=sys.stderr)
        return 2
    return 0
