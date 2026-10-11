"""Run lifecycle: start, sync with the worktree, checks, notes, approvals."""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, findings as F, store, tiers
from .util import CqError, git, git_ok, glob_match, iso, now, sha256_file, sha256_text, write_json, read_json

# Lines that switch a test off, or focus one test so the rest do not run.
SKIP_PATTERNS = [
    r"\b(?:it|test|describe|context)\.(?:skip|only|todo|fixme)\b",
    r"\b(?:xit|xdescribe|xtest|fit|fdescribe)\s*\(",
    r"@pytest\.mark\.(?:skip|skipif|xfail)\b",
    r"@unittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b",
    r"\bpytest\.skip\s*\(",
    r"\bskipTest\s*\(",
    r"\bSkipTest\b",
    r"@(?:Disabled|Ignore)\b",
    r"\bt\.Skip(?:f|Now)?\s*\(",
    r"#\[ignore\]",
]
# Lines that silence a linter, a type checker, coverage or SonarQube.
SUPPRESS_PATTERNS = [
    r"\bNOSONAR\b",
    r"eslint-disable",
    r"@ts-(?:ignore|nocheck)",
    r"#\s*noqa\b",
    r"#\s*type:\s*ignore",
    r"pragma:\s*no\s*cover",
    r"istanbul\s+ignore",
    r"//\s*nolint",
    r"@SuppressWarnings",
]
_SKIP = re.compile("|".join(SKIP_PATTERNS))
_SUPPRESS = re.compile("|".join(SUPPRESS_PATTERNS))


def _common_dir(path: Path) -> Path:
    out = git(path, "rev-parse", "--git-common-dir")
    p = Path(out)
    return (p if p.is_absolute() else path / p).resolve()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40] or "run"


def infer_repo(registry: Dict[str, Any], worktree: Path) -> str:
    """Find the registered repository this worktree belongs to."""
    here = _common_dir(worktree)
    for repo in registry["repositories"]:
        main = Path(repo["path"]).expanduser()
        if main.exists() and git_ok(main, "rev-parse", "--git-dir") and _common_dir(main.resolve()) == here:
            return repo["id"]
    raise CqError("%s does not belong to any repository in registry.yml. Register it, or pass --repo." % worktree)


def estimate_tier(repo_id: str, paths: List[str]) -> Dict[str, Any]:
    """Tier for a list of paths, before any code exists. Uses the live policy."""
    control = config.load_control(config.control_dir())
    repo = config.repo_entry(control["registry"], repo_id)
    main = Path(repo["path"]).expanduser()
    show = subprocess.run(
        ["git", "-C", str(main), "show", "%s/%s:.cq/criticality.yml" % (repo["remote"], repo["default_branch"])],
        capture_output=True, text=True,
    )
    rules = config.parse_criticality(show.stdout if show.returncode == 0 else "")
    return tiers.classify(paths, control["floor"]["paths"] + repo["paths"], rules, control["floor"]["default_tier"])


def start(
    repo_id: Optional[str],
    worktree: Path,
    ticket: Optional[str],
    min_tier: int,
    new: bool,
) -> Tuple[store.Run, bool]:
    """Create a run for the branch checked out in `worktree`, or return the
    active one. Returns (run, created)."""
    control = config.load_control(config.control_dir())
    worktree = worktree.expanduser().resolve()
    if not git_ok(worktree, "rev-parse", "--is-inside-work-tree"):
        raise CqError("%s is not a git worktree" % worktree)
    if repo_id is None:
        repo_id = infer_repo(control["registry"], worktree)
    repo = config.repo_entry(control["registry"], repo_id)
    worktree = Path(git(worktree, "rev-parse", "--show-toplevel")).resolve()
    main = Path(repo["path"]).expanduser().resolve()
    if not main.exists():
        raise CqError("registry path for '%s' does not exist: %s" % (repo_id, main))
    if _common_dir(worktree) != _common_dir(main):
        raise CqError("%s is not a worktree of the registered repository %s" % (worktree, main))
    branch = git(worktree, "branch", "--show-current")
    if not branch:
        raise CqError("the worktree is on a detached HEAD; check out a feature branch")
    if branch == repo["default_branch"]:
        raise CqError("refusing to run on the default branch '%s'" % branch)
    base_ref = "%s/%s" % (repo["remote"], repo["default_branch"])
    if not git_ok(worktree, "rev-parse", "--verify", "--quiet", base_ref + "^{commit}"):
        raise CqError("%s does not exist here. Run `git fetch %s` first." % (base_ref, repo["remote"]))

    if not new:
        active = [r for r in store.list_runs() if r.data["repo_id"] == repo_id and r.data["status"] == "active"]
        for existing in active:
            d = existing.data
            if d["branch"] != branch:
                continue
            # Resume. What the caller passed still applies: a tier floor can rise, a ticket cannot change.
            if ticket and d["ticket"] and ticket != d["ticket"]:
                raise CqError("run %s on this branch is for ticket %s, not %s" % (d["run_id"], d["ticket"], ticket))
            with existing.lock():
                d = existing.data
                d["ticket"] = d["ticket"] or ticket
                d["tier_manual"] = max(d["tier_manual"], min_tier)
                existing.save(d)
            sync(existing)
            return existing, False
        for existing in active:
            d = existing.data
            # Another branch name must not buy a clean slate (no findings, no spent
            # budget, no raised tier) for the same work. "The same work" is: the
            # same ticket, the same worktree, or commits the other run has seen.
            same_ticket = bool(ticket) and ticket == d["ticket"]
            same_worktree = Path(d["worktree"]) == worktree
            if same_ticket or same_worktree:
                if d["head_sha"] and git_ok(worktree, "merge-base", "--is-ancestor", d["head_sha"], base_ref):
                    with existing.lock():   # that work is merged; its run is finished
                        d = existing.data
                        d["status"] = "closed"
                        existing.save(d)
                        existing.event("run_closed", {"reason": "its head is merged into %s" % base_ref})
                    continue
                raise CqError(
                    "run %s on branch '%s' is still active for this %s. Continue on that branch, or have a human close it (cq run close %s)."
                    % (d["run_id"], d["branch"], "ticket" if same_ticket else "worktree", d["run_id"])
                )
            for sha in d["shas"]:
                on_default = git_ok(worktree, "merge-base", "--is-ancestor", sha, base_ref)
                if not on_default and git_ok(worktree, "merge-base", "--is-ancestor", sha, "HEAD"):
                    raise CqError(
                        "these commits already belong to active run %s on branch '%s'. Continue there, or have a human close that run (cq run close)."
                        % (d["run_id"], d["branch"])
                    )

    if new:
        # A human asked to start over on this branch. The old run is kept, closed.
        for existing in store.list_runs():
            d = existing.data
            same = d["branch"] == branch or Path(d["worktree"]) == worktree or (bool(ticket) and ticket == d["ticket"])
            if d["repo_id"] == repo_id and same and d["status"] == "active":
                with existing.lock():
                    d = existing.data
                    d["status"] = "closed"
                    existing.save(d)
                    existing.event("run_closed", {"reason": "superseded by a new run"})

    base_sha = git(worktree, "merge-base", base_ref, "HEAD")
    run_id = "%s-%s-%s" % (_slug(ticket or branch), time.strftime("%Y%m%d-%H%M%S", time.gmtime()), secrets.token_hex(2))
    run = store.Run(store.runs_dir() / run_id)
    with run.lock():
        # The project's own rules come from the base commit, never from the
        # branch under review, so a change cannot rewrite the rules it is judged by.
        show = subprocess.run(
            ["git", "-C", str(worktree), "show", "%s:.cq/criticality.yml" % base_sha],
            capture_output=True, text=True,
        )
        criticality = show.stdout if show.returncode == 0 else ""
        config.parse_criticality(criticality, ".cq/criticality.yml at %s" % base_sha[:12])
        digest = run.pin_policy(control, repo, criticality)
        floor = control["floor"]
        data = {
            "run_id": run_id,
            "repo_id": repo_id,
            "worktree": str(worktree),
            "ticket": ticket,
            "branch": branch,
            "mode": "tracer" if branch.startswith(floor["tracer"]["branch_prefix"]) else "normal",
            "base_ref": base_ref,
            "base_sha": base_sha,
            "head_sha": None,
            "shas": [],
            "dirty": False,
            "tier": 0,
            "tier_manual": min_tier,
            "tier_reasons": [],
            "unmapped": [],
            "changed_files": [],
            "deleted_files": [],
            "changed_lines": 0,
            "policy_digest": digest,
            "control_dir": str(config.control_dir()),
            "created_at": iso(),
            "implementer": {"family": None},
            "lines_since": {},
            "fix_rounds": {"local": 0, "mr": 0},
            "fix_rounds_extra": {"local": 0, "mr": 0},
            "fix_round_start": None,
            "budget_exhausted": None,
            "mr": None,
            "status": "active",
        }
        run.save(data)
        run.event("run_started", {"repo": repo_id, "branch": branch, "base_sha": base_sha, "policy_digest": digest})
    sync(run)
    return run, True


def repin(run: store.Run) -> str:
    """Re-read the control repo on purpose. The run never mixes two versions:
    it uses the old pin until this is called, and the new one after."""
    with run.lock():
        data = run.data
        control = config.load_control(config.control_dir())
        repo = config.repo_entry(control["registry"], data["repo_id"])
        criticality = (run.dir / "policy" / "criticality.yml").read_text(encoding="utf-8")
        digest = run.pin_policy(control, repo, criticality)
        old = data["policy_digest"]
        data["policy_digest"] = digest
        run.save(data)
        run.event("policy_repinned", {"from": old, "to": digest})
    sync(run)
    return digest


BINARY_AS_LINES = 1000   # a binary file has no line count; never treat it as a small change


def _lines(worktree: Path, a: str, b: str) -> int:
    """Lines added plus removed. A moved file counts in full: it was not reviewed where it now is."""
    total = 0
    for row in git(worktree, "diff", "--numstat", "--no-renames", a, b).splitlines():
        added, removed = row.split("\t")[:2]
        if added == "-" or removed == "-":
            total += BINARY_AS_LINES
        else:
            total += int(added) + int(removed)
    return total


def _diff_files(worktree: Path, a: str, b: str) -> Tuple[List[str], List[str], List[Tuple[str, str]]]:
    """Return (all changed paths, deleted paths, renames as (old, new)) between two commits."""
    out = git(worktree, "diff", "--name-status", "-M", "-z", a, b)
    parts = [p for p in out.split("\0") if p != ""]
    files: List[str] = []
    deleted: List[str] = []
    renamed: List[Tuple[str, str]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        if status.startswith(("R", "C")):
            files.extend([parts[i + 1], parts[i + 2]])
            if status.startswith("R"):
                renamed.append((parts[i + 1], parts[i + 2]))
            i += 3
        else:
            files.append(parts[i + 1])
            if status.startswith("D"):
                deleted.append(parts[i + 1])
            i += 2
    return sorted(set(files)), sorted(set(deleted)), renamed


def files_between(run_data: Dict[str, Any], a: str, b: str) -> List[str]:
    return _diff_files(Path(run_data["worktree"]), a, b)[0]


_DEPENDENCY_KEYS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "version")


def _package_config(worktree: Path, sha: str, path: str) -> Any:
    """package.json without its dependency lists: the part that can change what a check does."""
    show = subprocess.run(["git", "-C", str(worktree), "show", "%s:%s" % (sha, path)], capture_output=True)
    if show.returncode != 0:
        return None
    try:
        doc = json.loads(show.stdout.decode("utf-8", "replace"))
        return {k: v for k, v in doc.items() if k not in _DEPENDENCY_KEYS}
    except (ValueError, AttributeError):
        return "unreadable"


def _blob(worktree: Path, sha: str, path: str) -> str:
    return git(worktree, "rev-parse", "%s:%s" % (sha, path), check=False) or "gone"


def _gate_guard(
    worktree: Path, base: str, head: str, files: List[str], deleted: List[str], repo: Dict[str, Any],
    renamed: List[Tuple[str, str]],
) -> Dict[str, Dict[str, Any]]:
    """Find changes that weaken a gate. Deterministic: it reads the diff.

    A finding's id includes the content it is about. A human decision on one
    version of a file or line does not cover the next version."""
    found: Dict[str, Dict[str, Any]] = {}
    test_globs = repo["test_globs"]
    seen_lines: Dict[str, int] = {}

    def add(kind: str, path: str, line: Optional[int], text: str, severity: str, claim: str) -> None:
        key = "|".join([kind, path, text.strip()])
        seen_lines[key] = seen_lines.get(key, 0) + 1      # the same line twice is two findings
        fid = "gg-" + sha256_text("%s|%d" % (key, seen_lines[key]))[:10]
        found[fid] = {
            "severity": severity,
            "category": "tests",
            "location": {"path": path, "start_line": line, "end_line": line},
            "claim": claim,
            "scenario": text.strip()[:300] or None,
            "proposed_fix": "Restore it, or have a human approve the change with `cq decide`.",
            "local_id": kind,
        }

    def is_test(p: str) -> bool:
        return any(glob_match(g, p) for g in test_globs)

    for path in deleted:
        if is_test(path):
            add("test_deleted", path, None, "", "MAJOR", "A test file was deleted: %s" % path)
    for old, new in renamed:
        if is_test(old) and not is_test(new):
            add("test_deleted", old, None, "", "MAJOR", "A test file was moved to where the test runner will not find it: %s -> %s" % (old, new))

    for path in files:
        if path == "package.json" or path.endswith("/package.json"):
            before, after = _package_config(worktree, base, path), _package_config(worktree, head, path)
            if before != after:
                digest = sha256_text(json.dumps(after, sort_keys=True))[:16]
                add("check_definition", path, None, digest, "MAJOR", "The scripts or tool settings in %s changed; they define what the checks run" % path)
        elif any(glob_match(g, path) for g in repo["gate_files"]):
            add("check_definition", path, None, _blob(worktree, head, path), "MAJOR", "%s changed; it configures a check" % path)

    diff = git(worktree, "-c", "core.quotePath=false", "diff", "-U0", "--no-color", "-M", base, head)
    path = None
    line_no = 0
    in_header = False
    for raw in diff.split("\n"):
        if raw.startswith("diff --git "):
            in_header, path = True, None
        elif in_header and raw.startswith("+++ "):
            path = raw[6:].rstrip("\t") if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            in_header = False
            m = re.match(r"@@ -\S+ \+(\d+)", raw)
            line_no = int(m.group(1)) if m else 0
        elif not in_header and raw.startswith("+") and path:
            text = raw[1:]
            if is_test(path) and _SKIP.search(text):
                add("test_skipped", path, line_no, text, "MAJOR", "A test was switched off or focused")
            elif _SUPPRESS.search(text):
                add("check_suppressed", path, line_no, text, "MINOR", "A lint, type, coverage or SonarQube check was silenced")
            line_no += 1
    return found


def sync(run: store.Run) -> Dict[str, Any]:
    """Bring the run record in line with the worktree. Evidence tied to an
    older commit goes stale by itself: the evaluator compares SHAs."""
    with run.lock():
        data = run.data
        policy = run.policy()
        worktree = Path(data["worktree"])
        if not worktree.exists():
            raise CqError("worktree %s no longer exists" % worktree)
        branch = git(worktree, "branch", "--show-current")
        if branch != data["branch"]:
            raise CqError("the worktree is on '%s', but this run is for '%s'" % (branch or "detached HEAD", data["branch"]))
        head = git(worktree, "rev-parse", "HEAD")
        hidden = [l for l in git(worktree, "ls-files", "-v").splitlines() if l[:1].islower() or l[:1] == "S"]
        # Files git was told to ignore changes in (skip-worktree, assume-unchanged) count as uncommitted.
        data["dirty"] = bool(git(worktree, "status", "--porcelain", "--untracked-files=all")) or bool(hidden)
        shas = data["shas"]
        if not git_ok(worktree, "merge-base", "--is-ancestor", data["base_sha"], head):
            data["base_sha"] = git(worktree, "merge-base", data["base_ref"], "HEAD")
            shas = []
            run.event("history_rewritten", {"new_base": data["base_sha"], "head": head})
        if not shas:
            shas = [head]
        elif shas[-1] != head:
            if git_ok(worktree, "merge-base", "--is-ancestor", shas[-1], head):
                shas.append(head)
            else:
                shas = [head]
                run.event("history_rewritten", {"head": head})
        moved = data["head_sha"] != head
        data["shas"] = shas
        data["head_sha"] = head

        files, deleted, renamed = _diff_files(worktree, data["base_sha"], head)
        data["changed_files"] = files
        data["deleted_files"] = deleted
        data["changed_lines"] = _lines(worktree, data["base_sha"], head)
        # How much changed after each commit this run has seen. The evaluator
        # uses it to decide whether an earlier review can still be carried.
        data["lines_since"] = {sha: _lines(worktree, sha, head) for sha in shas}

        control_rules = policy["floor"]["paths"] + policy["repo"]["paths"]
        result = tiers.classify(files, control_rules, policy["repo_rules"], policy["floor"]["default_tier"])
        # A tier rises with the diff and never falls by itself.
        new_tier = max(result["tier"], data["tier_manual"], data["tier"])
        if new_tier != data["tier"]:
            run.event("tier_set", {"from": data["tier"], "to": new_tier})
        data["tier"] = new_tier
        data["tier_reasons"] = result["reasons"][:20]
        data["unmapped"] = result["unmapped"]
        # Who wrote the code comes from the pinned routing table, not from a caller's claim.
        data["implementer"] = {"family": config.model_for(policy["models"], "implementer", new_tier)["family"]}

        # Gate-guard findings: created from the diff, closed when the diff no longer shows them.
        findings = run.findings()
        current = _gate_guard(worktree, data["base_sha"], head, files, deleted, policy["repo"], renamed)
        for fid, fields in current.items():
            status = "contested" if fields["severity"] == "MAJOR" else "unverified"
            rec, created = F.upsert_system(findings, fid, F.GATE_GUARD, "system", head, fields, status, reopen=True)
            if created:
                run.event("gate_guard_finding", {"id": fid, "claim": fields["claim"], "path": fields["location"]["path"]})
        for fid, rec in findings.items():
            if fid.startswith("gg-") and fid not in current:
                F.close_system(rec, F.GATE_GUARD, head, "No longer present in the diff")
        run.save_findings(findings)

        run.save(data)
        if moved:
            run.event("head_moved", {"head": head, "tier": data["tier"], "dirty": data["dirty"]})
        return data


def raise_tier(run: store.Run, tier: int, reason: str, actor: Optional[str] = None) -> Dict[str, Any]:
    with run.lock():
        data = run.data
        if tier < data["tier"]:
            raise CqError(
                "a tier is never lowered from here (now %d). Only a human lowers one, with `cq tier lower`."
                % data["tier"]
            )
        data["tier_manual"] = max(data["tier_manual"], tier)
        if tier != data["tier"]:
            run.event("tier_set", {"from": data["tier"], "to": tier, "reason": reason}, actor)
        data["tier"] = tier
        run.save(data)
        return data


def lower_tier(run: store.Run, tier: int, by: str, reason: str) -> Dict[str, Any]:
    """A human lowers a tier that was raised by hand or by an earlier diff.
    The tier the current diff computes stays the floor."""
    sync(run)
    with run.lock():
        data = run.data
        policy = run.policy()
        control_rules = policy["floor"]["paths"] + policy["repo"]["paths"]
        computed = tiers.classify(
            data["changed_files"], control_rules, policy["repo_rules"], policy["floor"]["default_tier"]
        )["tier"]
        if tier < computed:
            raise CqError(
                "the current diff computes tier %d. To go lower, change the rules in the control repo and `cq run repin`."
                % computed
            )
        run.event("tier_set", {"from": data["tier"], "to": tier, "reason": reason}, "human:" + by)
        data["tier"] = tier
        data["tier_manual"] = tier
        run.save(data)
        return data


def run_check(run: store.Run, name: str) -> Dict[str, Any]:
    """Run one registered check in the worktree and record what happened.
    cq runs the command itself, so the result is not an agent's report."""
    data = sync(run)
    policy = run.policy()
    repo = policy["repo"]
    argv = repo["checks"].get(name)
    if argv is None:
        raise CqError("registry.yml has no '%s' command for %s" % (name, repo["id"]))
    if data["dirty"]:
        raise CqError("the worktree has uncommitted changes. Commit them first, so the result is tied to a commit.")
    worktree = Path(data["worktree"])
    head = data["head_sha"]
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)
    log_path = run.dir / "checks" / ("%s@%s.%s.log" % (name, head[:12], stamp))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = now()
    status, exit_code, detail = "error", None, None
    with open(log_path, "wb") as log:
        try:
            proc = subprocess.run(
                argv, cwd=str(worktree), stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, timeout=repo["check_timeout_seconds"],
            )
            exit_code = proc.returncode
            status = "passed" if exit_code == 0 else "failed"
        except FileNotFoundError:
            detail = "command not found: %s" % argv[0]
        except PermissionError:
            detail = "command is not executable: %s" % argv[0]
        except subprocess.TimeoutExpired:
            detail = "timed out after %d seconds" % repo["check_timeout_seconds"]
    after = git(worktree, "rev-parse", "HEAD")
    if after != head or git(worktree, "status", "--porcelain"):
        status, detail = "error", "the check changed the worktree; its result is not tied to a commit"
    record = {
        "name": name,
        "argv": argv,
        "status": status,
        "exit_code": exit_code,
        "detail": detail,
        "head_sha": head,
        "started_at": iso(started),
        "finished_at": iso(),
        "finished_ns": time.time_ns(),
        "duration_seconds": round((now() - started).total_seconds(), 1),
        "log": str(log_path),
        "log_sha256": sha256_file(log_path),
    }
    with run.lock():
        write_json(log_path.with_suffix(".json"), record)
        run.event("check_ran", {"name": name, "status": status, "exit_code": exit_code, "head": head})
    return record


def add_note(run: store.Run, name: str, source: Path, actor: Optional[str] = None) -> Dict[str, Any]:
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        raise CqError("note names are lowercase words, for example: plan, plan_review, rollback, escalation")
    text = source.read_text(encoding="utf-8")
    if not text.strip():
        raise CqError("the note is empty")
    extra: Dict[str, Any] = {}
    if name == "plan_review":
        # A plan review is evidence about one plan text, and it has an outcome.
        first = text.strip().splitlines()[0].strip().strip("*# ").upper()
        if first not in ("PLAN OK", "PLAN NEEDS CHANGES"):
            raise CqError("a plan review starts with the line `PLAN OK` or `PLAN NEEDS CHANGES`")
        plan = run.notes().get("plan")
        if plan is None:
            raise CqError("there is no plan on file to review (cq note add <run> plan <file>)")
        extra = {"outcome": "ok" if first == "PLAN OK" else "needs_changes", "plan_sha256": plan["sha256"]}
    with run.lock():
        ndir = run.dir / "notes"
        ndir.mkdir(parents=True, exist_ok=True)
        index = run.notes()
        digest = sha256_text(text)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        path = ndir / ("%s.%s.md" % (name, stamp))
        path.write_text(text, encoding="utf-8")
        index[name] = dict(extra, sha256=digest, at=iso(), path=str(path), head_sha=run.data["head_sha"])
        write_json(ndir / "index.json", index)
        run.event("note_added", {"name": name, "sha256": digest}, actor)
        return index[name]


def record_approval(run: store.Run, kind: str, by: str) -> Dict[str, Any]:
    """Record a human approval. The CLI confirms a human is present before it
    calls this. An approval covers one thing: one plan text, or one commit."""
    if kind not in config.APPROVALS:
        raise CqError("approval kind must be one of %s" % ", ".join(config.APPROVALS))
    with run.lock():
        data = run.data
        record: Dict[str, Any] = {"kind": kind, "by": by, "at": iso(), "tier": data["tier"]}
        if kind == "plan_approval":
            plan = run.notes().get("plan")
            if plan is None:
                raise CqError("there is no plan on file to approve (cq note add <run> plan <file>)")
            record["plan_sha256"] = plan["sha256"]
        else:
            if data["dirty"]:
                raise CqError("the worktree has uncommitted changes; a sign-off covers exactly one commit")
            record["head_sha"] = data["head_sha"]
        approvals = run.approvals()
        approvals.append(record)
        write_json(run.dir / "approvals.json", approvals)
        run.event("approval_recorded", record, "human:" + by)
        return record


def fix_round(run: store.Run, phase: str) -> Dict[str, Any]:
    """Count a fix round. When the budget is spent the run stops for a human."""
    if phase not in ("local", "mr"):
        raise CqError("phase must be local or mr")
    sync(run)
    with run.lock():
        data = run.data
        budget = run.policy()["floor"]["budgets"]["fix_rounds_" + phase] + data.get("fix_rounds_extra", {}).get(phase, 0)
        if data["fix_rounds"][phase] >= budget:
            data["budget_exhausted"] = phase
            run.save(data)
            run.event("budget_spent", {"phase": phase, "budget": budget})
            raise CqError(
                "the %s fix budget of %d rounds is spent. Stop fixing. Escalate this run to a human." % (phase, budget)
            )
        data["fix_rounds"][phase] += 1
        data["fix_round_start"] = data["head_sha"]   # a fix in this round is a commit after this one
        run.save(data)
        run.event("fix_round_started", {"phase": phase, "round": data["fix_rounds"][phase], "of": budget, "from": data["head_sha"]})
        return data


def extend_budget(run: store.Run, phase: str, by: str) -> Dict[str, Any]:
    """A human grants one more fix round after the budget was spent."""
    if phase not in ("local", "mr"):
        raise CqError("phase must be local or mr")
    with run.lock():
        data = run.data
        data.setdefault("fix_rounds_extra", {"local": 0, "mr": 0})[phase] += 1
        data["budget_exhausted"] = None
        run.save(data)
        run.event("budget_extended", {"phase": phase, "extra": data["fix_rounds_extra"][phase]}, "human:" + by)
        return data


def human_name(worktree: Optional[Path] = None) -> str:
    name = ""
    if worktree is not None:
        name = git(worktree, "config", "user.name", check=False)
    return name or os.environ.get("USER") or "unknown"
