"""The reviewer brief and the external adversary runner.

`cq brief` prints one text that tells a reviewer exactly what to review, how to
report, and how to submit. It comes from the control repo, so a change under
review cannot edit the instructions its reviewers follow.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, findings as F, runs, store
from .util import CqError, git

MAX_DIFF_CHARS = 400_000
# Files that would configure the external CLI from inside the change under review.
# Compared without case: macOS folders are case-insensitive. The last name is the
# context file cq's Gemini settings point at, so a change cannot supply one.
EXTERNAL_CONFIG = {".gemini", "gemini.md", ".geminiignore", "cq-review-no-context.md"}

LENSES = {
    "simplicity": (
        "Look for code that should not exist: dead code, speculative flexibility, a new abstraction with one "
        "caller, a dependency or helper that the standard library or the codebase already provides. "
        "Do not review correctness or security; other reviewers do that."
    ),
    "structure": (
        "Review maintainability and structure: spaghetti growth, thin abstractions, weak type boundaries, "
        "oversized files, logic in the wrong module. Look for the restructuring that keeps behaviour and makes "
        "the change markedly simpler."
    ),
    "standards_spec": (
        "Review on two axes. Standards: does the change follow the repository's documented standards in "
        ".cq/standards/? Name the rule a finding breaks. Spec: does the change do what the ticket and its "
        "acceptance criteria ask, no less and no more?"
    ),
    "tests": (
        "Review the tests, not the code. For each acceptance criterion and each changed behaviour: is there a "
        "test that fails when the change is reverted? Look for tests that assert nothing, mocks that hide the "
        "behaviour under test, and missing failure paths."
    ),
    "security": (
        "Review as an application-security and data-integrity engineer: injection (SQL, command, template, XSS, "
        "SSRF, path traversal), missing or wrong authorization, secrets in code, logs or fixtures, unsafe "
        "deserialization, non-atomic writes, migrations that cannot be rolled back or that break the previous "
        "deploy. Give a one-line exploitation or corruption sketch for each finding."
    ),
    "adversary": (
        "You are a hostile reviewer. Your goal is to make this change fail in production. Ignore style. "
        "Which input, race, retry, partial failure or deploy order corrupts data or returns an error? Which "
        "assumption is not enforced by a type, a constraint or a test? Write the counterexample. Check the "
        "boring parts twice: config defaults, feature flags, serialization. Every finding is an attack scenario "
        "with steps to reproduce."
    ),
    "final_check": (
        "This is the last review before a human signs off. Read the whole finished change. "
        "Answer one question: should this merge, and what would we regret? Report a finding only for a reason "
        "not to merge. A BLOCKER or MAJOR finding from you is a veto and goes to a human."
    ),
}
LENSES["adversary2"] = LENSES["adversary"]

CONTRACT = """\
## How to report

Write one JSON object and nothing else. It must match this schema exactly;
unknown fields are refused.

{schema}

Rules:
1. Every finding needs three things: a file and line, a concrete failure
   scenario (the input or sequence that triggers it, or the named standard it
   breaks), and a proposed fix. A finding that lacks one is stored as NIT.
2. An empty `findings` array is a valid result. Do not invent findings.
3. Severity. BLOCKER: wrong behaviour, data loss or a security hole. MAJOR: an
   edge-case defect, or real maintainability or test debt. MINOR: a small
   clarity or consistency issue. NIT: taste.
4. Set `always_blocking` only for a committed secret (`secret`), an injection
   surface (`injection`), silent data loss (`silent_data_loss`) or a broken
   public API contract (`breaking_contract`).
5. List every changed file in `files_reviewed`, or in `files_skipped` with a
   reason. A submission that leaves a changed file out is refused.
6. You are read-only. Do not edit, commit, fix or push anything.
7. You work alone. Do not read other reviewers' findings.
"""


def fix_start(run: store.Run, reviewer: str) -> str:
    """Where a reviewer's next fix-diff review starts: the newest commit it has
    already reviewed. Fix-diff reviews then form a chain with no gap."""
    data = run.data
    index = {sha: i for i, sha in enumerate(data["shas"])}
    seen = [
        e["revision"]["head_sha"] for e in run.envelopes()
        if e["source"]["reviewer"] == reviewer and e["revision"]["head_sha"] in index
    ]
    if not seen:
        raise CqError("'%s' has no earlier review in this run to continue from. It needs a full review." % reviewer)
    start = max(seen, key=lambda sha: index[sha])
    if start == data["head_sha"]:
        raise CqError("'%s' has already reviewed the current commit" % reviewer)
    return start


def reviewer_brief(run: store.Run, reviewer: str, scope: str, reviewed_from: Optional[str]) -> str:
    data = run.data
    if reviewer not in config.REVIEWERS:
        raise CqError("unknown reviewer '%s'. Known: %s" % (reviewer, ", ".join(config.REVIEWERS)))
    head = data["head_sha"]
    if scope == "fix_diff":
        if not reviewed_from:
            reviewed_from = fix_start(run, reviewer)
        if reviewed_from not in data["shas"]:
            raise CqError("--from must be a commit this run has seen: %s" % ", ".join(s[:12] for s in data["shas"]))
        start = reviewed_from
        files = runs.files_between(data, start, head)
        what = "Review only the fix: the commits after %s. The earlier review of the rest stands." % start[:12]
    else:
        start = data["base_sha"]
        files = data["changed_files"]
        what = "Review the whole change."
    findings = run.findings()
    presented = F.presented_fixes(findings, reviewer)
    if presented:
        what += (
            "\n\nThese findings are marked fixed. Marking is a claim: check each fix in the code."
            " If one is not fixed, put its id in `not_fixed`. An id you leave out counts as confirmed by you.\n"
        )
        what += "\n".join(
            "- %s (%s): %s" % (fid, (findings[fid].get("location") or {}).get("path") or "-", findings[fid]["claim"][:300])
            for fid in presented
        )
    tier = data["tier"]
    schema = json.dumps(F.submission_schema(), indent=2)
    lines = [
        "# Review brief: %s" % reviewer,
        "",
        "Run %s, repository %s, tier %d (%s)%s." % (
            data["run_id"], data["repo_id"], tier, config.TIER_NAMES[tier],
            ", tracer branch" if data["mode"] == "tracer" else "",
        ),
        "Ticket: %s" % (data["ticket"] or "none given"),
        "",
        "## Your lens",
        "",
        LENSES[reviewer],
        "",
        "## What to review",
        "",
        what,
        "",
        "    git -C %s diff %s %s" % (shlex.quote(data["worktree"]), start, head),
        "",
        "Scope: %s. Head commit: %s." % (scope, head),
        "Read code outside the diff only where you need it to judge the diff.",
        "",
        "Changed files (%d):" % len(files),
    ]
    lines += ["- %s" % f for f in files] or ["- (none)"]
    lines += ["", CONTRACT.format(schema=schema)]
    lines += [
        "For this review set `scope` to \"%s\"%s." % (
            scope, " and `reviewed_from` to \"%s\"" % start if scope == "fix_diff" else ""
        ),
        "",
        "## How to submit",
        "",
        "Pass the JSON on stdin with a heredoc (`-` as the file), or save it to a file outside the repository:",
        "",
        "    cq findings check %s --reviewer %s --head %s -   # dry run; fix what it reports, once" % (data["run_id"], reviewer, head),
        "    cq findings add   %s --reviewer %s --head %s -" % (data["run_id"], reviewer, head),
        "",
        "Your review counts only after `cq findings add` prints `accepted`.",
    ]
    if data["mode"] == "tracer":
        lines += ["", "This is a tracer branch. Hardcoding and stubs are expected: report them as MINOR, not as defects."]
    return "\n".join(lines) + "\n"


def _extract_json(text: str) -> Any:
    """Take the one JSON object out of a CLI's output, which may wrap it in prose or a code fence."""
    decoder = json.JSONDecoder()
    best = None
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text[i:])
        except ValueError:
            i += 1
            continue
        # A CLI may echo the brief, which holds the schema. The answer is the
        # last object whose `findings` is a list.
        if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
            best = obj
        i += end
    if best is None:
        raise CqError("the reviewer's output contains no submission object")
    return best


def run_external_reviewer(run: store.Run, slot: str, scope: str, reviewed_from: Optional[str]) -> Dict[str, Any]:
    """Run the configured external CLI with the brief on stdin and return its
    submission. The caller validates and stores it like any other review."""
    if slot not in ("adversary", "adversary2"):
        raise CqError("slot must be adversary or adversary2")
    spec = run.policy()["models"][slot]
    if not spec["argv"] or not spec["model"] or not spec["family"]:
        raise CqError(
            "policy/models.yml has no %s configured (model, family and argv). Until it does, this review is missing evidence." % slot
        )
    data = run.data
    head = data["head_sha"]
    if scope == "fix_diff":
        start = reviewed_from or fix_start(run, slot)
    else:
        start = data["base_sha"]
    brief = reviewer_brief(run, slot, scope, start if scope == "fix_diff" else None)
    diff = git(Path(data["worktree"]), "diff", start, head)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n[The diff is cut here at %d characters. Read the changed files for the rest.]" % MAX_DIFF_CHARS
    brief += (
        "\nYou cannot run git or cq. Your working directory is a copy of the head commit %s, and the diff is"
        " below. Print the JSON object as your whole answer; it is submitted for you.\n\n## The diff\n\n```diff\n%s\n```\n"
        % (head, diff)
    )
    control = str(config.control_dir())
    argv = [a.replace("{control}", control) for a in spec["argv"]]
    env = dict(os.environ)
    env.update({k: v.replace("{control}", control) for k, v in spec.get("env", {}).items()})
    copy = export_head(Path(data["worktree"]), head)
    from .engine.agents import run_group
    try:
        code, stdout, stderr = run_group(argv, brief, copy, env, spec.get("timeout_seconds", 1800))
        proc = subprocess.CompletedProcess(argv, code, stdout, stderr)
    except FileNotFoundError:
        raise CqError("%s command not found: %s" % (slot, spec["argv"][0]))
    except subprocess.TimeoutExpired:
        raise CqError("%s timed out" % slot)
    finally:
        shutil.rmtree(copy, ignore_errors=True)
    log = run.dir / "envelopes" / ("%s@%s.raw.txt" % (slot, data["head_sha"][:12]))
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("exit code %s\n--- stdout ---\n%s\n--- stderr ---\n%s\n" % (proc.returncode, proc.stdout, proc.stderr), encoding="utf-8")
    if proc.returncode != 0:
        raise CqError("%s exited with code %d. Output saved to %s" % (slot, proc.returncode, log))
    return _unwrap(proc.stdout, spec["model"], slot, log)


def export_head(worktree: Path, head: str) -> Path:
    """A plain copy of one commit for an external reviewer: regular files only, no
    symlinks, no submodules, no .git, and none of the files that would configure
    the reviewer's CLI. Read from the object store with ls-tree and cat-file, so
    the change's own .gitattributes (export-ignore) cannot hide files."""
    listing = subprocess.run(["git", "-C", str(worktree), "ls-tree", "-r", "-z", "--full-tree", head], capture_output=True)
    if listing.returncode != 0:
        raise CqError("git ls-tree %s failed: %s" % (head[:12], listing.stderr.decode("utf-8", "replace").strip()))
    entries = []
    for item in listing.stdout.split(b"\0"):
        if not item:
            continue
        meta, _, raw_path = item.partition(b"\t")
        mode, kind, sha = meta.decode().split()
        rel = raw_path.decode("utf-8", "surrogateescape")
        parts = Path(rel).parts
        if kind != "blob" or mode not in ("100644", "100755") or rel.startswith("/") or ".." in parts:
            continue
        if any(p.lower() in EXTERNAL_CONFIG for p in parts):
            continue
        entries.append((rel, sha, mode))
    target = Path(tempfile.mkdtemp(prefix="cq-review-"))
    try:
        blobs = subprocess.run(["git", "-C", str(worktree), "cat-file", "--batch"],
                               input=b"".join(sha.encode() + b"\n" for _, sha, _ in entries), capture_output=True)
        if blobs.returncode != 0:
            raise CqError("git cat-file failed: %s" % blobs.stderr.decode("utf-8", "replace").strip())
        out, pos = blobs.stdout, 0
        for rel, sha, mode in entries:
            newline = out.index(b"\n", pos)
            size = int(out[pos:newline].split()[2])
            content = out[newline + 1:newline + 1 + size]
            pos = newline + 1 + size + 1
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            if mode == "100755":
                path.chmod(0o755)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _unwrap(stdout: str, model: str, slot: str, log: Any) -> Any:
    """Gemini CLI with `-o json` wraps the answer: {"response": "<text>", "stats":
    {"models": {"<id>": ...}}}. The CLI can quietly run another model than the one
    asked for, so the model that actually ran is checked against the config."""
    try:
        outer = json.loads(stdout)
    except ValueError:
        outer = None
    if isinstance(outer, dict) and isinstance(outer.get("response"), str):
        ran = sorted(((outer.get("stats") or {}).get("models") or {}).keys())
        if ran and model not in ran:
            raise CqError(
                "%s asked for %s but the CLI ran %s. The review does not count. Output saved to %s"
                % (slot, model, ", ".join(ran), log)
            )
        return _extract_json(outer["response"])
    return _extract_json(stdout)


def source_for(run: store.Run, reviewer: str) -> Dict[str, Any]:
    """Who reviewed, taken from config and never from the reviewer's own claim."""
    policy = run.policy()
    if reviewer in ("adversary", "adversary2"):
        spec = policy["models"][reviewer]
        return {"kind": "agent", "model": spec["model"] or None, "family": spec["family"] or None}
    spec = config.model_for(policy["models"], "reviewer", run.data["tier"])
    return {"kind": "agent", "model": spec["model"], "family": spec["family"]}


def models_table(run: store.Run) -> List[Dict[str, Any]]:
    policy = run.policy()
    tier = run.data["tier"]
    rows = []
    for role in policy["models"]["roles"]:
        spec = config.model_for(policy["models"], role, tier)
        rows.append({"role": role, "model": spec["model"], "family": spec["family"], "effort": spec["effort"]})
    for slot in ("adversary", "adversary2"):
        spec = policy["models"][slot]
        rows.append({"role": slot, "model": spec["model"] or "(not configured)", "family": spec["family"] or "-", "effort": "-"})
    return rows
