"""engine.yml: how the engine reaches Claude Code, Jira and the clock."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .. import config
from ..util import CqError

_ROLES = ["screener", "planner", "plan_reviewer", "implementer", "test_author", "reviewer", "verifier", "aggregator", "fixer"]
_LABEL = {"type": "string", "pattern": "^[^\\s\\[\\]]{1,255}$"}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "tracker", "labels", "worktrees_dir", "branch_prefix", "wip_limit", "claude", "waits", "max_passes"],
    "properties": {
        "schema_version": {"const": 1},
        "tracker": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind"],
            "properties": {
                "kind": {"enum": ["jira", "files"]},
                "base_url": {"type": "string", "pattern": "^https?://"},
                "auth": {"enum": ["basic", "bearer"]},
                "email_env": {"type": "string"},
                "token_env": {"type": "string"},
                "issue_type": {"type": "string", "minLength": 1},
                "search_api": {"enum": ["jql", "legacy"]},
                "dir": {"type": "string", "minLength": 1},
            },
        },
        "labels": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ready", "in_progress", "done", "needs_human", "needs_confirmation", "confirmed", "found", "escalation"],
            "properties": {k: _LABEL for k in ["ready", "in_progress", "done", "needs_human", "needs_confirmation", "confirmed", "found", "escalation"]},
        },
        "worktrees_dir": {"type": "string", "minLength": 1},
        "branch_prefix": {"type": "string", "pattern": "^[A-Za-z0-9._/-]+$"},
        "wip_limit": {"type": "integer", "minimum": 1},
        "claude": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command", "timeout_seconds", "max_turns"],
            "properties": {
                "command": {"type": "string", "minLength": 1},
                "timeout_seconds": {"type": "integer", "minimum": 1},
                "max_turns": {"type": "object", "additionalProperties": False,
                              "properties": {r: {"type": "integer", "minimum": 1} for r in _ROLES}},
                "extra_allowed_tools": {"type": "array", "items": {"type": "string"}},
                "max_budget_usd": {"type": "number", "exclusiveMinimum": 0},
                "keep_sessions": {"type": "boolean"},
            },
        },
        "waits": {
            "type": "object",
            "additionalProperties": False,
            "required": ["duo_seconds", "sonar_seconds", "poll_seconds"],
            "properties": {k: {"type": "integer", "minimum": 0}
                           for k in ["duo_seconds", "sonar_seconds", "poll_seconds", "escalate_after_seconds"]},
        },
        "max_passes": {"type": "integer", "minimum": 2},
        "dashboard": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "stale_days": {"type": "number", "minimum": 0},
                "refresh_seconds": {"type": "integer", "minimum": 10},
            },
        },
    },
}


DASHBOARD = {"port": 8765, "stale_days": 7, "refresh_seconds": 60}


def dashboard_settings() -> Dict[str, Any]:
    """The dashboard and the inbox work without a valid engine.yml (runs started by hand)."""
    try:
        return load()["dashboard"]
    except CqError:
        return dict(DASHBOARD)


def load(root: Path = None) -> Dict[str, Any]:
    root = root or config.control_dir()
    path = root / "engine.yml"
    if not path.exists():
        raise CqError("the control repo has no engine.yml (looked in %s)" % root)
    data = config.parse_yaml(path.read_text(encoding="utf-8"), "engine.yml")
    config.validate(data, SCHEMA, "engine.yml")
    tracker = data["tracker"]
    if tracker["kind"] == "jira":
        missing = [k for k in ("base_url", "token_env") if not tracker.get(k)]
        if tracker.get("auth", "basic") == "basic" and not tracker.get("email_env"):
            missing.append("email_env")
        if missing:
            raise CqError("engine.yml: a jira tracker needs %s" % ", ".join(missing))
    elif not tracker.get("dir"):
        raise CqError("engine.yml: a files tracker needs dir")
    data["claude"].setdefault("extra_allowed_tools", [])
    data["dashboard"] = dict(DASHBOARD, **data.get("dashboard", {}))
    data["waits"].setdefault("escalate_after_seconds", 14400)
    data["worktrees_dir"] = str(Path(data["worktrees_dir"]).expanduser())
    return data
