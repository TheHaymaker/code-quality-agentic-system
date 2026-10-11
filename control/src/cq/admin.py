"""`cq init-repo` and `cq doctor`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .util import CqError, git, git_ok

HOOK_COMMAND = "cq guard || exit 2"
HOOK_MATCHER = "Edit|Write|NotebookEdit|Bash"
SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {"matcher": HOOK_MATCHER, "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 20}]}
        ]
    }
}


def _hook_installed(repo: Path) -> bool:
    path = repo / ".claude" / "settings.json"
    if not path.exists():
        return False
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    for entry in (settings.get("hooks") or {}).get("PreToolUse") or []:
        matcher = entry.get("matcher") or ""
        covers = matcher in ("", "*") or all(t in matcher for t in ("Edit", "Write", "Bash"))
        if covers and any("cq guard" in (h.get("command") or "") for h in entry.get("hooks") or []):
            return True
    return False


def init_repo(repo: Path) -> List[str]:
    """Install the per-repo files. An existing file is never overwritten."""
    repo = repo.expanduser().resolve()
    if not git_ok(repo, "rev-parse", "--is-inside-work-tree"):
        raise CqError("%s is not a git repository" % repo)
    templates = config.control_dir() / "templates"
    log: List[str] = []
    for top, dest_top in (("cq", ".cq"), ("claude", ".claude")):
        for src in sorted((templates / top).rglob("*")):
            if src.is_dir():
                continue
            dest = repo / dest_top / src.relative_to(templates / top)
            if dest.exists():
                log.append("kept   %s (already exists)" % dest.relative_to(repo))
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            log.append("wrote  %s" % dest.relative_to(repo))
    settings = repo / ".claude" / "settings.json"
    if not settings.exists():
        settings.write_text(json.dumps(SETTINGS, indent=2) + "\n", encoding="utf-8")
        log.append("wrote  .claude/settings.json (gate-protection hook)")
    elif _hook_installed(repo):
        log.append("kept   .claude/settings.json (hook already registered)")
    else:
        log.append("ACTION .claude/settings.json exists and was not changed. Add this hook to it by hand:")
        log.append(json.dumps(SETTINGS, indent=2))
    return log


def doctor(repo_id: Optional[str]) -> Tuple[List[Tuple[str, str]], bool]:
    """Return ([(level, message)], ok). Levels: ok, warn, fail."""
    out: List[Tuple[str, str]] = []
    control_root = config.control_dir()
    out.append(("ok", "control repo: %s" % control_root))
    out.append(("ok", "run store:    %s" % (config.cq_home() / "runs")))
    try:
        control = config.load_control(control_root)
    except CqError as exc:
        out.append(("fail", str(exc)))
        return out, False
    out.append(("ok", "policy/floor.yml, policy/models.yml and registry.yml are valid"))
    if not git_ok(control_root, "rev-parse", "--is-inside-work-tree"):
        out.append(("warn", "the control repo is not under git; policy changes will have no history"))

    for tool in ("git", "glab"):
        if shutil.which(tool):
            out.append(("ok", "%s found" % tool))
        else:
            out.append(("fail", "%s is not on PATH" % tool))
    if shutil.which("glab"):
        proc = subprocess.run(["glab", "auth", "status"], capture_output=True, text=True)
        out.append(("ok", "glab is authenticated") if proc.returncode == 0 else ("fail", "glab auth status failed; run `glab auth login`"))

    for slot in ("adversary", "adversary2"):
        spec = control["models"][slot]
        if not (spec["argv"] and spec["model"] and spec["family"]):
            out.append(("warn", "%s is not configured in policy/models.yml; runs that need it will be INCONCLUSIVE" % slot))
        elif not shutil.which(spec["argv"][0]):
            out.append(("fail", "%s command not found: %s" % (slot, spec["argv"][0])))
    implementer = control["models"]["roles"]["implementer"]["default"]["family"]
    if control["models"]["adversary"]["family"] == implementer:
        out.append(("fail", "adversary uses the implementer's model family (%s); it must differ" % implementer))

    engine_file = control_root / "engine.yml"
    if engine_file.exists():
        from .engine import settings as engine_settings

        try:
            ecfg = engine_settings.load(control_root)
            out.append(("ok", "engine.yml is valid"))
            claude = ecfg["claude"]["command"]
            out.append(("ok", "Claude Code found") if shutil.which(claude) else ("fail", "Claude Code (%s) is not on PATH; the engine cannot start agents" % claude))
            hook = control_root / "policy" / "claude-engine.json"
            out.append(("ok", "engine sessions get the guard hook") if hook.exists()
                       else ("fail", "policy/claude-engine.json is missing; reinstall cq. Engine sessions refuse to start without it"))
            home = config.cq_home().resolve()
            worktrees = Path(ecfg["worktrees_dir"]).resolve()
            if worktrees == home or home in worktrees.parents:
                out.append(("warn", "worktrees_dir is inside %s. Keep it outside, for example ~/cq-worktrees" % home))
            tracker = ecfg["tracker"]
            if tracker["kind"] == "jira":
                for key in ("token_env", "email_env"):
                    if tracker.get(key) and (key != "email_env" or tracker.get("auth", "basic") == "basic") and not os.environ.get(tracker[key]):
                        out.append(("fail", "environment variable %s (Jira) is not set" % tracker[key]))
        except CqError as exc:
            out.append(("fail", str(exc)))
    else:
        out.append(("warn", "no engine.yml; `cq engine` is not set up"))
    if any(control["models"][s]["argv"][:1] == ["gemini"] for s in ("adversary", "adversary2") if control["models"][s]["argv"]):
        google = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GENAI_USE_VERTEXAI")
        if not any(os.environ.get(k) for k in google):
            out.append(("warn", "the Gemini adversary has no GEMINI_API_KEY (or Vertex AI variables) in the environment"))

    repos = control["registry"]["repositories"]
    if repo_id:
        repos = [config.repo_entry(control["registry"], repo_id)]
    elif not repos:
        out.append(("warn", "registry.yml has no repositories yet"))
    for raw in repos:
        repo = config.repo_entry(control["registry"], raw["id"])
        label = "[%s] " % repo["id"]
        path = Path(repo["path"]).expanduser()
        if not path.exists() or not git_ok(path, "rev-parse", "--is-inside-work-tree"):
            out.append(("fail", label + "path is not a git repository: %s" % path))
            continue
        base_ref = "%s/%s" % (repo["remote"], repo["default_branch"])
        if not git(path, "remote", "get-url", repo["remote"], check=False):
            out.append(("fail", label + "has no remote named '%s'" % repo["remote"]))
        elif not git_ok(path, "rev-parse", "--verify", "--quiet", base_ref + "^{commit}"):
            out.append(("fail", label + "%s not found; run `git fetch %s`" % (base_ref, repo["remote"])))
        else:
            out.append(("ok", label + "checkout and %s found" % base_ref))
        for name, argv in sorted(repo["checks"].items()):
            exe = argv[0]
            found = shutil.which(exe) or ((path / exe).exists() and os.access(path / exe, os.X_OK))
            if not found:
                out.append(("fail", label + "check '%s': command not found: %s" % (name, exe)))
        required = set()
        for gate in control["floor"]["gates"].values():
            required.update(gate["checks"])
        gaps = sorted(required - set(repo["checks"]) - set(repo["not_applicable"]))
        if gaps:
            out.append(("warn", label + "no command and no not_applicable reason for: %s (those tiers will be INCONCLUSIVE)" % ", ".join(gaps)))
        for name, why in sorted(repo["not_applicable"].items()):
            out.append(("warn", label + "'%s' is marked not applicable: %s" % (name, why)))
        sonar = repo.get("sonar")
        if sonar is None:
            if "sonar" not in repo["not_applicable"]:
                out.append(("warn", label + "no sonar section; tier 1+ runs will be INCONCLUSIVE"))
        elif not os.environ.get(sonar.get("token_env", "SONAR_TOKEN")):
            out.append(("fail", label + "environment variable %s is not set" % sonar.get("token_env", "SONAR_TOKEN")))
        if _hook_installed(path):
            out.append(("ok", label + "gate-protection hook is registered"))
        else:
            out.append(("fail", label + "gate-protection hook is not registered in .claude/settings.json; run `cq init-repo %s`" % path))
        missing = [n for n in ("cq-run", "cq-panel", "cq-aggregate", "cq-mr-loop") if not (path / ".claude" / "skills" / n / "SKILL.md").exists()]
        if missing:
            out.append(("fail", label + "skills not installed: %s; run `cq init-repo %s`" % (", ".join(missing), path)))
        if shutil.which("glab") and git(path, "remote", "get-url", repo["remote"], check=False):
            proc = subprocess.run(["glab", "api", "projects/:fullpath"], cwd=str(path), capture_output=True, text=True)
            if proc.returncode != 0:
                out.append(("fail", label + "glab cannot read the GitLab project for this checkout"))
            else:
                try:
                    actual = json.loads(proc.stdout).get("path_with_namespace")
                except ValueError:
                    actual = None
                if actual == repo["gitlab_project"]:
                    out.append(("ok", label + "GitLab project matches the registry"))
                else:
                    out.append(("fail", label + "remote is GitLab project '%s', registry says '%s'" % (actual, repo["gitlab_project"])))
    ok = not any(level == "fail" for level, _ in out)
    return out, ok
