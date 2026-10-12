"""The merge request inbox: every merge request cq opened, what it waits for,
and whose turn it is.

cq never merges. A merge request it opened can then wait on you (a decision,
a review, conflicts), on a reviewer (approvals, open threads), or on cq itself
(new commits or Duo comments it has not read yet). Each row names the next
action, who takes it, and the command for it. Rows whose next action belongs
to cq carry `auto: true`: Hermes may run that command on its own.

A draft merge request reports `detailed_merge_status: draft_status` and hides
every other blocker behind it, so the row is built from the separate fields:
conflicts, diverged commits, the pipeline, approvals, reviewers and threads.
"""
from __future__ import annotations

import concurrent.futures as cf
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import gitlab, store
from ..util import CqError, now, parse_time, read_json
from . import core

FAILED_PIPELINE = ("failed", "canceled")
RUNNING_PIPELINE = ("created", "waiting_for_resource", "preparing", "pending", "running", "scheduled")
REBASE_METHODS = ("ff", "rebase_merge")


def _cwd(run: store.Run) -> Path:
    worktree = Path(run.data["worktree"])
    if worktree.exists():
        return worktree
    return Path(run.policy()["repo"]["path"]).expanduser()


def _fetch(run: store.Run, cache: Dict[Any, Any]) -> Dict[str, Any]:
    """What GitLab says about the run's merge request. Only reads."""
    cwd = _cwd(run)
    pid = gitlab.project_id(run)
    path = "projects/%d/merge_requests/%d" % (pid, run.data["mr"]["iid"])
    mr = gitlab.api(cwd, path + "?include_diverged_commits_count=true")
    out: Dict[str, Any] = {"mr": mr, "reviewers": [], "discussions": [], "approvals": None, "merge_method": None}
    if mr.get("state") != "opened":
        return out
    if pid not in cache:
        try:
            cache[pid] = (gitlab.api(cwd, "projects/%d" % pid) or {}).get("merge_method")
        except CqError:
            cache[pid] = None
    out["merge_method"] = cache[pid]
    out["reviewers"] = gitlab.api(cwd, path + "/reviewers") or []
    out["discussions"] = gitlab.api_pages(cwd, path + "/discussions")
    try:
        out["approvals"] = gitlab.api(cwd, path + "/approvals")
    except CqError:
        out["approvals"] = None  # not every GitLab tier offers it
    return out


def _threads(discussions: List[Dict[str, Any]], duo_user: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Unresolved threads: the ids of Duo's, and the human ones."""
    duo, human = [], []
    for d in discussions:
        notes = [n for n in d.get("notes", []) if not n.get("system")]
        resolvable = [n for n in notes if n.get("resolvable")]
        if not notes or not resolvable or all(n.get("resolved") for n in resolvable):
            continue
        author = (notes[0].get("author") or {}).get("username")
        if author == duo_user:
            duo.append(str(d["id"]))
        else:
            human.append({"id": str(d["id"]), "author": author, "body": (notes[0].get("body") or "").strip()[:200]})
    return duo, human


def _n(count: int, word: str) -> str:
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _days(stamp: Optional[str]) -> Optional[float]:
    try:
        return round((now() - parse_time(stamp)).total_seconds() / 86400, 1)
    except (TypeError, ValueError):
        return None


def build_row(run: store.Run, info: Optional[Dict[str, Any]], error: Optional[str], stale_days: float) -> Dict[str, Any]:
    data = run.data
    rec = core.Engine.record(run)
    state, reason = core.live_state(run, rec) if rec else (None, None)
    verdict = read_json(run.dir / "verdict.json", {})
    run_id = run.dir.name
    resume = "cq engine resume %s" % run_id
    row: Dict[str, Any] = {
        "run_id": run_id, "ticket": data.get("ticket"), "repo": data["repo_id"], "tier": data["tier"],
        "run_status": data["status"], "engine_state": state, "engine_stage": rec.get("stage") if rec else None,
        "engine_reason": reason, "escalation": rec.get("escalation") if rec else None,
        "verdict": verdict.get("verdict"), "verdict_head": verdict.get("head_sha"),
        "cost_usd": rec.get("cost_usd", 0.0) if rec else 0.0,
        "mr": {"iid": data["mr"]["iid"], "url": data["mr"].get("web_url")}, "flags": [], "error": error,
    }
    flags: List[Dict[str, str]] = row["flags"]

    def nxt(who: str, action: str, command: Optional[str] = None, auto: bool = False) -> Dict[str, Any]:
        row["next"] = {"who": who, "action": action, "command": command, "auto": auto}
        return row

    if info is None:
        return nxt("you", "GitLab did not answer: %s" % (error or "unknown error"))
    mr = info["mr"]
    pipeline = mr.get("head_pipeline") or {}
    row["mr"].update(title=mr.get("title"), draft=bool(mr.get("draft") or mr.get("work_in_progress")),
                     state=mr.get("state"), target=mr.get("target_branch"), sha=mr.get("sha"),
                     updated_at=mr.get("updated_at"), url=mr.get("web_url") or row["mr"]["url"],
                     merge_status=mr.get("detailed_merge_status"), pipeline=pipeline.get("status"),
                     pipeline_url=pipeline.get("web_url"), behind=mr.get("diverged_commits_count"),
                     conflicts=bool(mr.get("has_conflicts")))
    idle = _days(mr.get("updated_at"))
    row["idle_days"] = idle
    if mr.get("state") in ("merged", "closed"):
        return nxt("none", "Merged." if mr["state"] == "merged" else "Closed without merging.")

    if data["status"] != "active" or state == core.CANCELLED:
        return nxt("you", "cq no longer works on this merge request (the run is %s). Merge or close it yourself."
                   % ("cancelled" if state == core.CANCELLED else "closed"), row["mr"]["url"])

    duo_user = run.policy()["repo"]["duo"]["username"]
    duo_open, human_open = _threads(info["discussions"], duo_user)
    known = {t["discussion_id"] for t in ((run.external().get("duo") or {}).get("threads") or [])}
    new_duo = [d for d in duo_open if d not in known]
    reviewers = [r for r in info["reviewers"] if (r.get("user") or {}).get("username") != duo_user]
    changes = sorted((r.get("user") or {}).get("username") for r in reviewers if r.get("state") == "requested_changes")
    waiting_on = sorted((r.get("user") or {}).get("username") for r in reviewers if r.get("state") in ("unreviewed", None))
    approvals = info["approvals"] or {}
    behind = mr.get("diverged_commits_count") or 0
    must_rebase = mr.get("detailed_merge_status") == "need_rebase" or (behind and info["merge_method"] in REBASE_METHODS)
    moved = bool(mr.get("sha")) and mr.get("sha") != data["head_sha"]

    def flag(code: str, text: str) -> None:
        flags.append({"code": code, "text": text})

    if row["mr"]["draft"]:
        flag("draft", "Draft")
    if mr.get("has_conflicts"):
        flag("conflicts", "Conflicts with %s" % mr.get("target_branch"))
    if behind:
        flag("behind", "%s behind %s%s" % (_n(behind, "commit"), mr.get("target_branch"), "; the project needs a rebase" if must_rebase else ""))
    if pipeline.get("status") in FAILED_PIPELINE:
        flag("pipeline_failed", "Pipeline %s" % pipeline["status"])
    elif pipeline.get("status") in RUNNING_PIPELINE:
        flag("pipeline_running", "Pipeline %s" % pipeline["status"])
    if moved:
        flag("moved", "GitLab has commits cq has not checked")
    if new_duo:
        flag("duo_new", "%s from Duo" % _n(len(new_duo), "new comment"))
    if human_open:
        flag("threads", "%s from reviewers" % _n(len(human_open), "open thread"))
    if changes:
        flag("changes_requested", "Changes requested by %s" % ", ".join("@%s" % u for u in changes))
    if approvals.get("approvals_left"):
        flag("approvals", "%s still needed" % _n(approvals["approvals_left"], "approval"))
    elif approvals.get("approved_by"):
        flag("approved", "Approved by %s" % ", ".join("@%s" % (a.get("user") or {}).get("username") for a in approvals["approved_by"]))
    if idle is not None and idle >= stale_days:
        flag("stale", "No activity for %d days" % idle)
    row["threads"] = human_open

    worktree = data["worktree"]
    target = mr.get("target_branch")
    if state == core.RUNNING:
        return nxt("cq", "cq is working (%s)." % (rec.get("stage") or "-"), "cq engine watch %s" % run_id)
    if state == core.FAILED:
        return nxt("you", reason or "The run failed.", resume)
    if mr.get("has_conflicts"):
        return nxt("you", "Resolve the conflicts with %s: try `cq engine update`; if it stops, merge %s in %s yourself, then resume."
                   % (target, target, worktree), "cq engine update %s" % run_id)
    if state == core.NEEDS_HUMAN:
        return nxt("you", reason or "A decision is needed.", resume)
    if moved:
        return nxt("cq", "Take in the new commits on GitLab and check them.", resume, auto=True)
    if new_duo:
        return nxt("cq", "Answer %s from Duo." % _n(len(new_duo), "new comment"), resume, auto=True)
    if state == core.WAITING:
        return nxt("cq", reason or "Waiting for an outside service.", resume, auto=True)
    if pipeline.get("status") in FAILED_PIPELINE:
        return nxt("you", "The pipeline failed. Look at it, then resume if the code must change.", pipeline.get("web_url"))
    if changes:
        return nxt("you", "Changes requested by %s. Answer the threads on the merge request." % ", ".join("@%s" % u for u in changes))
    if human_open:
        return nxt("you", "%s from reviewers on the merge request." % _n(len(human_open), "open thread"))
    if must_rebase:
        return nxt("you", "Rebase onto %s (the project merges by fast-forward). The change is checked again." % target,
                   "cq engine update %s --rebase" % run_id)
    if row["mr"]["draft"]:
        if row["verdict"] == "PASS" and state == core.COMPLETED:
            return nxt("you", "cq passed it. Review the draft and mark it ready.", row["mr"]["url"])
        return nxt("you", "Draft, not decided by cq yet.", resume)
    if approvals.get("approvals_left"):
        return nxt("reviewer", "Waiting for %s%s." % (
            _n(approvals["approvals_left"], "approval"), (" from " + ", ".join("@%s" % u for u in waiting_on)) if waiting_on else ""))
    if waiting_on:
        return nxt("reviewer", "Waiting for a review from %s." % ", ".join("@%s" % u for u in waiting_on))
    if pipeline.get("status") in RUNNING_PIPELINE:
        return nxt("none", "The pipeline is running.")
    return nxt("you", "Ready to merge.", row["mr"]["url"])


ORDER = {"you": 0, "cq": 1, "reviewer": 2, "none": 3}


def fetch_all(runs: List[store.Run], workers: int = 6) -> Dict[str, Tuple[Optional[Dict[str, Any]], Optional[str]]]:
    """GitLab's answer for each run's merge request: (info, None), or (None, error)."""
    cache: Dict[Any, Any] = {}

    def one(run: store.Run) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            return _fetch(run, cache), None
        except CqError as exc:
            return None, str(exc)[:300]

    with cf.ThreadPoolExecutor(max_workers=max(1, min(workers, len(runs) or 1))) as pool:
        answers = list(pool.map(one, runs))
    return {run.dir.name: answer for run, answer in zip(runs, answers)}


def rows_from(runs: List[store.Run], fetched: Dict[str, Tuple[Optional[Dict[str, Any]], Optional[str]]],
              include_closed: bool, stale_days: float) -> List[Dict[str, Any]]:
    """Rows from GitLab answers already in hand, with the run store read now:
    the engine state is always current even when GitLab's answer is a minute old."""
    rows = [build_row(run, *fetched[run.dir.name], stale_days) for run in runs if run.dir.name in fetched]
    if not include_closed:
        rows = [r for r in rows if (r.get("mr") or {}).get("state") not in ("merged", "closed")]
    rows.sort(key=lambda r: (ORDER.get(r["next"]["who"], 9), -(r.get("idle_days") or 0)))
    return rows


def with_mr(runs: Optional[List[store.Run]] = None) -> List[store.Run]:
    return [r for r in (runs if runs is not None else store.list_runs()) if r.data.get("mr")]


def mr_rows(include_closed: bool = False, stale_days: float = 7, runs: Optional[List[store.Run]] = None,
            workers: int = 6) -> List[Dict[str, Any]]:
    """One row per merge request cq opened. Merged and closed ones only with include_closed."""
    candidates = with_mr(runs)
    return rows_from(candidates, fetch_all(candidates, workers), include_closed, stale_days)
