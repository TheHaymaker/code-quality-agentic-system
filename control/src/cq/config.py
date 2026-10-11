"""Load and strictly validate the control-repo config. Unknown fields are rejected.

Nothing downstream parses raw YAML: every module works from what this file returns.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import yaml

from .util import CqError

SEVERITIES = ["NIT", "MINOR", "MAJOR", "BLOCKER"]  # ascending
UNTRIAGED = "UNTRIAGED"  # external comment that has no severity yet
CATEGORIES = ["correctness", "security", "data_integrity", "contract", "tests", "structure", "simplicity"]
ALWAYS_BLOCKING = ["secret", "injection", "silent_data_loss", "breaking_contract"]
REVIEWERS = [
    "simplicity",
    "structure",
    "standards_spec",
    "tests",
    "security",
    "adversary",
    "adversary2",
    "final_check",
]
EXTERNAL = ["duo", "sonar"]
APPROVALS = ["plan_approval", "final_signoff"]
TIERS = ["0", "1", "2", "3"]
TIER_NAMES = {0: "Low", 1: "Standard", 2: "High", 3: "Critical"}

_NAME = {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}
_NAMES = {"type": "array", "items": _NAME, "uniqueItems": True}
_RULE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["glob", "tier", "reason"],
    "properties": {
        "glob": {"type": "string", "minLength": 1},
        "tier": {"enum": [0, 1, 2, 3]},
        "reason": {"type": "string", "minLength": 1},
    },
}
_RULES = {"type": "array", "items": _RULE}
_GATE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["checks", "reviewers", "external", "notes", "approvals", "human"],
    "properties": {
        "checks": _NAMES,
        "reviewers": {"type": "array", "items": {"enum": REVIEWERS}, "uniqueItems": True},
        "external": {"type": "array", "items": {"enum": EXTERNAL}, "uniqueItems": True},
        "notes": _NAMES,
        "approvals": {"type": "array", "items": {"enum": APPROVALS}, "uniqueItems": True},
        "human": {"type": "string", "minLength": 1},
    },
}


def _per_tier(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": TIERS,
        "properties": {t: item for t in TIERS},
    }


FLOOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "default_tier",
        "paths",
        "protected",
        "tracer",
        "budgets",
        "review_carry",
        "sonar_severity_map",
        "sonar_impact_map",
        "waiver_max_days",
        "gates",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "default_tier": {"enum": [0, 1, 2, 3]},
        "paths": _RULES,
        "protected": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "tracer": {
            "type": "object",
            "additionalProperties": False,
            "required": ["branch_prefix"],
            "properties": {"branch_prefix": {"type": "string", "minLength": 1}},
        },
        "budgets": {
            "type": "object",
            "additionalProperties": False,
            "required": ["fix_rounds_local", "fix_rounds_mr"],
            "properties": {
                "fix_rounds_local": {"type": "integer", "minimum": 0},
                "fix_rounds_mr": {"type": "integer", "minimum": 0},
            },
        },
        "review_carry": {
            "type": "object",
            "additionalProperties": False,
            "required": ["max_lines", "max_percent", "never_for"],
            "properties": {
                "max_lines": {"type": "integer", "minimum": 0},
                "max_percent": {"type": "integer", "minimum": 0, "maximum": 100},
                "never_for": {"type": "array", "items": {"enum": REVIEWERS}, "uniqueItems": True},
            },
        },
        "sonar_severity_map": {
            "type": "object",
            "additionalProperties": {"enum": SEVERITIES},
        },
        "sonar_impact_map": {
            "type": "object",
            "additionalProperties": {"enum": SEVERITIES},
        },
        "waiver_max_days": _per_tier({"type": "integer", "minimum": 1}),
        "gates": _per_tier(_GATE),
    },
}

_ARGV = {"type": "array", "items": {"type": "string"}, "minItems": 1}
_NA = {"type": "object", "additionalProperties": {"type": "string", "minLength": 1}}
_REPO = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "path", "default_branch", "gitlab_project", "checks"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$"},
        "path": {"type": "string", "minLength": 1},
        "remote": {"type": "string", "minLength": 1},
        "default_branch": {"type": "string", "minLength": 1},
        "gitlab_project": {"type": "string", "pattern": "^[^/].*/.*[^/]$"},
        "jira_projects": {"type": "array", "items": {"type": "string"}},
        "sensitive": {"type": "boolean"},
        "checks": {"type": "object", "propertyNames": _NAME, "additionalProperties": _ARGV},
        "check_timeout_seconds": {"type": "integer", "minimum": 1},
        "not_applicable": _NA,
        "paths": _RULES,
        "test_globs": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "gate_files": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "duo": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"username": {"type": "string", "minLength": 1}},
        },
        "sonar": {
            "type": "object",
            "additionalProperties": False,
            "required": ["project_key", "host_url", "product"],
            "properties": {
                "project_key": {"type": "string", "minLength": 1},
                "host_url": {"type": "string", "pattern": "^https?://"},
                "product": {"enum": ["server", "cloud"]},
                "organization": {"type": "string", "minLength": 1},
                "auth": {"enum": ["bearer", "basic"]},
                "freshness": {"enum": ["commit", "time"]},
                "token_env": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
            },
        },
    },
}
REGISTRY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "repositories"],
    "properties": {
        "schema_version": {"const": 1},
        "defaults": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"not_applicable": _NA},
        },
        "repositories": {"type": "array", "items": _REPO},
    },
}

_MODEL = {
    "type": "object",
    "additionalProperties": False,
    "required": ["model", "family", "effort"],
    "properties": {
        "model": {"type": "string", "minLength": 1},
        "family": {"type": "string", "minLength": 1},
        "effort": {"enum": ["low", "medium", "high", "xhigh", "max"]},
    },
}
_ROLE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["default"],
    "properties": {
        "default": _MODEL,
        "tiers": {"type": "object", "additionalProperties": False, "properties": {t: _MODEL for t in TIERS}},
    },
}
_EXTERNAL_REVIEWER = {
    "type": "object",
    "additionalProperties": False,
    "required": ["model", "family", "argv"],
    "properties": {
        "model": {"type": "string"},
        "family": {"type": "string"},
        # `{control}` in argv and env is replaced by the control repo path.
        "argv": {"type": "array", "items": {"type": "string"}},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "timeout_seconds": {"type": "integer", "minimum": 1},
    },
}
MODELS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "roles", "adversary", "adversary2"],
    "properties": {
        "schema_version": {"const": 1},
        "roles": {
            "type": "object",
            "additionalProperties": False,
            "required": ["implementer", "reviewer"],
            "properties": {
                r: _ROLE
                for r in [
                    "screener",
                    "planner",
                    "plan_reviewer",
                    "test_author",
                    "implementer",
                    "reviewer",
                    "verifier",
                    "aggregator",
                    "fixer",
                    "utility",
                ]
            },
        },
        "adversary": _EXTERNAL_REVIEWER,
        "adversary2": _EXTERNAL_REVIEWER,
    },
}

CRITICALITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "rules"],
    "properties": {"schema_version": {"const": 1}, "rules": _RULES},
}

# Files that decide what a check does. A change to one is shown to a human.
# package.json is handled apart: only a change to its "scripts" counts.
DEFAULT_GATE_FILES = [
    "**/Makefile",
    "**/tox.ini",
    "**/pytest.ini",
    "**/.coveragerc",
    "**/jest.config.*",
    "**/vitest.config.*",
    "**/.eslintrc*",
    "**/eslint.config.*",
    "**/tsconfig*.json",
    "**/.pre-commit-config.yaml",
    "**/conftest.py",
    "**/setup.cfg",
    "**/.flake8",
    "**/mypy.ini",
    "**/.eslintignore",
]

DEFAULT_TEST_GLOBS = [
    "**/tests/**",
    "**/test/**",
    "**/__tests__/**",
    "**/*_test.*",
    "**/test_*.*",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*Test.*",
    "**/spec/**",
    "**/e2e/**",
]


def control_dir() -> Path:
    env = os.environ.get("CQ_CONTROL")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def cq_home() -> Path:
    return Path(os.environ.get("CQ_HOME", "~/.cq")).expanduser().resolve()


def _stringify_keys(obj: Any) -> Any:
    """YAML reads `0:` as an integer key. Normalise so schemas and code agree."""
    if isinstance(obj, dict):
        return {str(k): _stringify_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_keys(v) for v in obj]
    return obj


def parse_yaml(text: str, what: str) -> Any:
    try:
        return _stringify_keys(yaml.safe_load(text))
    except yaml.YAMLError as exc:
        raise CqError("%s: not valid YAML: %s" % (what, exc))


def validate(obj: Any, schema: Dict[str, Any], what: str) -> None:
    errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(obj), key=lambda e: list(e.absolute_path))
    if errors:
        lines = []
        for e in errors[:10]:
            where = "/".join(str(p) for p in e.absolute_path) or "(top level)"
            lines.append("  %s: %s" % (where, e.message))
        raise CqError("%s is invalid:\n%s" % (what, "\n".join(lines)))


def parse_floor(text: str, what: str = "policy/floor.yml") -> Dict[str, Any]:
    floor = parse_yaml(text, what)
    validate(floor, FLOOR_SCHEMA, what)
    # Each tier must require at least what the tier below requires.
    for kind in ("checks", "reviewers", "external", "notes", "approvals"):
        for lower, upper in (("0", "1"), ("1", "2"), ("2", "3")):
            missing = set(floor["gates"][lower][kind]) - set(floor["gates"][upper][kind])
            if missing:
                raise CqError(
                    "%s is invalid: tier %s drops %s %s that tier %s requires"
                    % (what, upper, kind, sorted(missing), lower)
                )
    return floor


def parse_models(text: str, what: str = "policy/models.yml") -> Dict[str, Any]:
    models = parse_yaml(text, what)
    validate(models, MODELS_SCHEMA, what)
    return models


def parse_registry(text: str, what: str = "registry.yml") -> Dict[str, Any]:
    registry = parse_yaml(text, what)
    validate(registry, REGISTRY_SCHEMA, what)
    ids = [r["id"] for r in registry["repositories"]]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise CqError("%s is invalid: duplicate repository ids %s" % (what, dupes))
    scopes = [("defaults", registry.get("defaults", {}))] + [(r["id"], r) for r in registry["repositories"]]
    for label, scope in scopes:
        bad = sorted(set(scope.get("not_applicable", {})) & set(APPROVALS))
        if bad:
            raise CqError("%s is invalid: %s marks %s not applicable; a human approval cannot be waived here" % (what, label, bad))
    return registry


def parse_criticality(text: str, what: str = ".cq/criticality.yml") -> List[Dict[str, Any]]:
    if not text.strip():
        return []
    doc = parse_yaml(text, what)
    validate(doc, CRITICALITY_SCHEMA, what)
    return doc["rules"]


def repo_entry(registry: Dict[str, Any], repo_id: str) -> Dict[str, Any]:
    """One repository with registry-wide defaults applied."""
    for repo in registry["repositories"]:
        if repo["id"] == repo_id:
            merged = dict(repo)
            na = dict(registry.get("defaults", {}).get("not_applicable", {}))
            na.update(repo.get("not_applicable", {}))
            merged["not_applicable"] = na
            merged.setdefault("remote", "origin")
            merged.setdefault("sensitive", False)
            merged.setdefault("paths", [])
            merged.setdefault("check_timeout_seconds", 1800)
            merged.setdefault("test_globs", DEFAULT_TEST_GLOBS)
            merged.setdefault("gate_files", DEFAULT_GATE_FILES)
            merged.setdefault("duo", {})
            merged["duo"].setdefault("username", "GitLabDuo")
            return merged
    known = ", ".join(r["id"] for r in registry["repositories"]) or "(none registered)"
    raise CqError("repository '%s' is not in registry.yml. Known: %s" % (repo_id, known))


ROLE_FALLBACK = {"test_author": "implementer", "fixer": "implementer", "planner": "implementer",
                 "plan_reviewer": "reviewer", "verifier": "reviewer", "aggregator": "reviewer",
                 "screener": "utility", "utility": "reviewer"}


def model_for(models: Dict[str, Any], role: str, tier: int) -> Dict[str, Any]:
    spec = models["roles"].get(role)
    while spec is None and role in ROLE_FALLBACK:
        # An older models.yml without this role: use the nearest role it has.
        role = ROLE_FALLBACK[role]
        spec = models["roles"].get(role)
    if spec is None:
        raise CqError("models.yml has no role '%s'" % role)
    return spec.get("tiers", {}).get(str(tier), spec["default"])


def load_control(root: Path) -> Dict[str, Any]:
    """Read the live control repo. Runs do not use this after they start: they
    use the copy pinned into the run store."""

    def read(rel: str) -> str:
        path = root / rel
        if not path.exists():
            raise CqError("control repo is missing %s (looked in %s)" % (rel, root))
        return path.read_text(encoding="utf-8")

    return {
        "floor_text": read("policy/floor.yml"),
        "models_text": read("policy/models.yml"),
        "registry_text": read("registry.yml"),
        "floor": parse_floor(read("policy/floor.yml")),
        "models": parse_models(read("policy/models.yml")),
        "registry": parse_registry(read("registry.yml")),
    }
