"""GitLab adapter: the draft merge request and the GitLab Duo review loop.
Everything goes through `glab api`, run inside the worktree.

GitLab documents no "Duo review finished" signal. This module infers it: Duo
has posted since the request and has been quiet for a settle period, or the
reviewer state has left `unreviewed`. Check that on one real merge request
before you rely on it.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, findings as F, store
from .util import CqError, git, iso, now, parse_time, remote_branch_head

REPORT_START = "<!-- cq:report:start -->"
REPORT_END = "<!-- cq:report:end -->"
SETTLE_SECONDS = 60
DONE_STATES = ("reviewed", "requested_changes", "approved")


class GitlabError(CqError):
    pass


def _glab(worktree: Path, args: List[str]) -> str:
    try:
        proc = subprocess.run(["glab", *args], cwd=str(worktree), capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise GitlabError("glab is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise GitlabError("glab %s timed out" % " ".join(args[:2]))
    if proc.returncode != 0:
        raise GitlabError("glab %s failed: %s" % (" ".join(args[:3]), (proc.stderr or proc.stdout).strip()[:500]))
    return proc.stdout


def api(
    worktree: Path,
    path: str,
    method: str = "GET",
    fields: Optional[Dict[str, str]] = None,
    file_fields: Optional[Dict[str, str]] = None,
) -> Any:
    args = ["api", path]
    if method != "GET":
        args += ["-X", method]
    for key, value in (fields or {}).items():
        args += ["-f", "%s=%s" % (key, value)]
    for key, file_path in (file_fields or {}).items():
        args += ["-F", "%s=@%s" % (key, file_path)]
    out = _glab(worktree, args).strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        raise GitlabError("glab api %s did not return JSON" % path)


def api_pages(worktree: Path, path: str, limit: int = 50) -> List[Any]:
    """Read every page. Paging is done here, one page per call, so the result
    does not depend on how glab prints paginated output."""
    items: List[Any] = []
    joiner = "&" if "?" in path else "?"
    for page in range(1, limit + 1):
        chunk = api(worktree, "%s%sper_page=100&page=%d" % (path, joiner, page))
        if not isinstance(chunk, list):
            raise GitlabError("expected a list from %s" % path)
        items.extend(chunk)
        if len(chunk) < 100:
            return items
    raise GitlabError("%s has more than %d pages; refusing to read a partial result" % (path, limit))


def project_id(run: store.Run) -> int:
    data = run.data
    if data.get("gitlab_project_id"):
        return data["gitlab_project_id"]
    repo = run.policy()["repo"]
    project = api(Path(data["worktree"]), "projects/:fullpath")
    actual = (project or {}).get("path_with_namespace")
    if actual != repo["gitlab_project"]:
        raise GitlabError(
            "the worktree's remote is GitLab project '%s', but registry.yml says '%s'" % (actual, repo["gitlab_project"])
        )
    with run.lock():
        data = run.data
        data["gitlab_project_id"] = project["id"]
        run.save(data)
    return project["id"]


def _mr_path(run: store.Run) -> str:
    data = run.data
    if not data.get("mr"):
        raise CqError("this run has no merge request yet. Run `cq mr ensure` first.")
    return "projects/%d/merge_requests/%d" % (project_id(run), data["mr"]["iid"])


def mr_ensure(run: store.Run) -> Dict[str, Any]:
    """Find this branch's open merge request, or create a draft one. Running
    it twice never creates a second merge request."""
    data = run.data
    worktree = Path(data["worktree"])
    repo = run.policy()["repo"]
    if data.get("mr"):
        return data["mr"]
    remote_head = remote_branch_head(worktree, repo["remote"], data["branch"])
    if not remote_head:
        raise CqError("branch '%s' is not on %s. Push it first." % (data["branch"], repo["remote"]))
    pid = project_id(run)
    query = urllib.parse.urlencode({"state": "opened", "source_branch": data["branch"]})
    existing = [
        m for m in api_pages(worktree, "projects/%d/merge_requests?%s" % (pid, query))
        if m["source_branch"] == data["branch"]
    ]
    if len(existing) > 1:
        raise CqError("branch '%s' has %d open merge requests; close the extra ones" % (data["branch"], len(existing)))
    created = False
    if existing:
        mr = existing[0]
    else:
        title = "Draft: %s" % (("%s %s" % (data["ticket"], data["branch"])) if data["ticket"] else data["branch"])
        mr = api(
            worktree,
            "projects/%d/merge_requests" % pid,
            method="POST",
            fields={
                "source_branch": data["branch"],
                "target_branch": repo["default_branch"],
                "title": title,
                "description": "%s\nThe evidence report appears here when the quality run finishes.\n%s"
                % (REPORT_START, REPORT_END),
            },
        )
        created = True
    record = {"iid": mr["iid"], "web_url": mr.get("web_url"), "created_by_cq": created}
    with run.lock():
        data = run.data
        data["mr"] = record
        run.save(data)
        run.event("mr_created" if created else "mr_adopted", record)
    return record


def mr_describe(run: store.Run, report_markdown: str) -> None:
    """Put the evidence report at the top of the description. Text outside the
    report markers is left as it is."""
    worktree = Path(run.data["worktree"])
    mr = api(worktree, _mr_path(run))
    current = mr.get("description") or ""
    block = "%s\n%s\n%s" % (REPORT_START, report_markdown.strip(), REPORT_END)
    if REPORT_START in current and REPORT_END in current:
        head, rest = current.split(REPORT_START, 1)
        tail = rest.split(REPORT_END, 1)[1]
        updated = head + block + tail
    else:
        updated = block + ("\n\n" + current if current.strip() else "")
    tmp = run.dir / "mr-description.md"
    tmp.write_text(updated, encoding="utf-8")
    api(worktree, _mr_path(run), method="PUT", file_fields={"description": str(tmp)})
    with run.lock():
        run.event("mr_described", {"iid": run.data["mr"]["iid"]})


def _reviewer_state(run: store.Run, username: str) -> Optional[str]:
    reviewers = api(Path(run.data["worktree"]), _mr_path(run) + "/reviewers") or []
    for entry in reviewers:
        if (entry.get("user") or {}).get("username") == username:
            return entry.get("state") or "unknown"
    return None


def _duo_notes(discussions: List[Dict[str, Any]], username: str) -> List[Dict[str, Any]]:
    return [
        n for d in discussions for n in d.get("notes", [])
        if (n.get("author") or {}).get("username") == username and not n.get("system")
    ]


def duo_request(run: store.Run, force: bool = False) -> Dict[str, Any]:
    """Ask GitLab Duo to review the current head. For a reviewer that is
    already assigned, the same quick action requests a new review. With
    `force`, a new review is requested even if one exists for this commit
    (after Duo reported an error)."""
    data = run.data
    worktree = Path(data["worktree"])
    username = run.policy()["repo"]["duo"]["username"]
    head = data["head_sha"]
    base = {"head_sha": head, "requested_head": head, "username": username, "threads": [], "fetched_at": iso()}
    mr = api(worktree, _mr_path(run))
    if mr.get("sha") != head:
        raise CqError("the merge request is at %s and the worktree at %s. Push first." % (str(mr.get("sha"))[:12], head[:12]))
    prior = run.external().get("duo")
    if not force and prior and prior.get("requested_head") == head and prior["state"] in ("pending", "reviewed"):
        return prior
    if not force and _reviewer_state(run, username) is not None:
        on_head = [
            n for n in _duo_notes(api_pages(worktree, _mr_path(run) + "/discussions"), username)
            if (n.get("position") or {}).get("head_sha") == head
        ]
        if on_head:
            # Duo already commented on this exact commit (automatic reviews are
            # on). Use that review and do not start a second one.
            first = min(parse_time(n["created_at"]) for n in on_head)
            evidence = dict(base, state="pending", requested_at=iso(first), reset_seen=True, detail="Adopted an existing review of this commit.")
            _save_duo(run, evidence, "duo_adopted")
            return evidence
    requested_at = now()
    before = _reviewer_state(run, username)
    try:
        api(worktree, _mr_path(run) + "/notes", method="POST", fields={"body": "/assign_reviewer @%s" % username})
    except GitlabError as exc:
        evidence = dict(base, state="request_failed", requested_at=iso(requested_at), detail=str(exc))
        _save_duo(run, evidence, "duo_request_failed")
        return evidence
    state = None
    for _ in range(5):
        state = _reviewer_state(run, username)
        if state is not None:
            break
        time.sleep(2)
    if state is None:
        evidence = dict(
            base, state="request_failed", requested_at=iso(requested_at),
            detail="@%s is not a reviewer after the request. Check that Duo Code Review is enabled for this project." % username,
        )
        _save_duo(run, evidence, "duo_request_failed")
        return evidence
    # A reviewer state that shows "done" proves a new review only if it was seen
    # to reset after this request. Otherwise it may be left over from the last one.
    evidence = dict(
        base, state="pending", requested_at=iso(requested_at), reviewer_state=state,
        reset_seen=before is None or state not in DONE_STATES, detail="Review requested.",
    )
    _save_duo(run, evidence, "duo_requested")
    return evidence


def _save_duo(run: store.Run, evidence: Dict[str, Any], event: str) -> None:
    with run.lock():
        run.save_external("duo", evidence)
        run.event(event, {"state": evidence["state"], "head": evidence["head_sha"], "detail": evidence.get("detail")})


def duo_ingest(run: store.Run) -> Dict[str, Any]:
    """Read Duo's review. An empty result is never read as a clean one: the
    evidence says whether the review is pending, done, or failed."""
    data = run.data
    worktree = Path(data["worktree"])
    head = data["head_sha"]
    username = run.policy()["repo"]["duo"]["username"]
    prior = run.external().get("duo")
    if prior is None or prior.get("requested_head") != head or prior["state"] == "request_failed":
        evidence = {
            "head_sha": head, "state": "not_requested", "threads": [], "fetched_at": iso(), "username": username,
            "detail": "No review was requested for this commit. Run `cq duo request`.",
        }
        _save_duo(run, evidence, "duo_ingested")
        return evidence
    evidence = dict(prior, fetched_at=iso(), head_sha=head)
    try:
        mr = api(worktree, _mr_path(run))
        if mr.get("sha") != head:
            evidence.update(state="error", error_kind="sha", detail="The merge request is at %s, not the local head. Push first." % str(mr.get("sha"))[:12])
            _save_duo(run, evidence, "duo_ingested")
            return evidence
        discussions = api_pages(worktree, _mr_path(run) + "/discussions")
        reviewer_state = _reviewer_state(run, username)
    except GitlabError as exc:
        evidence.update(state="error", error_kind="api", detail=str(exc))
        _save_duo(run, evidence, "duo_ingested")
        return evidence

    since = parse_time(prior["requested_at"])
    notes = _duo_notes(discussions, username)
    recent = [n for n in notes if parse_time(n["created_at"]) >= since]
    failed = [n for n in recent if "Error code: DCR" in (n.get("body") or "")]
    last_post = max((parse_time(n["created_at"]) for n in recent), default=None)
    quiet = last_post is None or (now() - last_post).total_seconds() >= SETTLE_SECONDS

    threads = []
    with run.lock():
        findings = run.findings()
        for d in discussions:
            dnotes = [n for n in d.get("notes", []) if not n.get("system")]
            if not dnotes or (dnotes[0].get("author") or {}).get("username") != username:
                continue
            resolvable = [n for n in dnotes if n.get("resolvable")]
            if not resolvable:
                continue
            resolved = all(n.get("resolved") for n in resolvable)
            fid = "duo-" + str(d["id"])[:16]
            if resolved and fid not in findings:
                continue
            first = dnotes[0]
            pos = first.get("position") or {}
            path = pos.get("new_path") or pos.get("old_path")
            location = {"path": path, "start_line": pos.get("new_line") or pos.get("old_line"), "end_line": None} if path else None
            F.upsert_system(
                findings, fid, "duo", "external", head,
                {"severity": config.UNTRIAGED, "category": None, "claim": (first.get("body") or "").strip()[:4000],
                 "location": location, "local_id": str(d["id"])},
                "unverified",
            )
            threads.append({
                "discussion_id": str(d["id"]),
                "finding_id": fid,
                "resolved": resolved,
                "stale_position": bool(pos.get("head_sha")) and pos.get("head_sha") != head,
            })
        run.save_findings(findings)

    summaries = [n for n in recent if not n.get("resolvable")]
    if reviewer_state is not None and reviewer_state not in DONE_STATES:
        evidence["reset_seen"] = True
    evidence.pop("error_kind", None)
    evidence.update(
        reviewer_state=reviewer_state,
        notes_since_request=len(recent),
        threads=threads,
        unresolved=sum(1 for t in threads if not t["resolved"]),
        summary=(summaries[-1].get("body") or "").strip()[:800] if summaries else None,
    )
    if failed:
        evidence.update(state="error", error_kind="duo", detail=(failed[-1].get("body") or "").strip().splitlines()[0][:300])
    elif reviewer_state is None:
        evidence.update(state="error", error_kind="duo", detail="@%s is no longer a reviewer on the merge request." % username)
    elif (recent and quiet) or (not recent and reviewer_state in DONE_STATES and evidence.get("reset_seen")):
        evidence.update(state="reviewed", detail="%d comment(s), %d open thread(s)." % (len(recent), evidence["unresolved"]))
    elif recent:
        evidence.update(state="pending", detail="Duo posted in the last %d seconds and may still be writing." % SETTLE_SECONDS)
    else:
        evidence.update(state="pending", detail="Duo has not posted since the request.")
    _save_duo(run, evidence, "duo_ingested")
    return evidence


def duo_wait(run: store.Run, timeout: int, interval: int) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while True:
        evidence = duo_ingest(run)
        if evidence["state"] != "pending" or time.time() >= deadline:
            return evidence
        time.sleep(max(1, min(interval, int(deadline - time.time()) or 1)))


def duo_reply(run: store.Run, fid: str, body: str, resolve: bool) -> Dict[str, Any]:
    """Reply on a Duo thread, and optionally resolve it. A thread is resolved
    only after its finding has an outcome and a reason on record."""
    worktree = Path(run.data["worktree"])
    findings = run.findings()
    rec = F.get(findings, fid)
    # Several Duo threads can stand behind one finding after a merge. Answer all of them.
    discussions = sorted({s["local_id"] for s in rec["sources"] if s["reviewer"] == "duo" and s.get("local_id")})
    if not discussions:
        raise CqError("%s is not a Duo thread" % rec["id"])
    if not body.strip():
        raise CqError("the reply is empty")
    outcome = F.keeper(findings, rec)
    if resolve and outcome["status"] not in F.CLOSED:
        raise CqError(
            "%s is still %s. Record its outcome first (fixed, filed or rejected, with a reason); a thread is never resolved silently."
            % (outcome["id"], outcome["status"])
        )
    for discussion in discussions:
        api(worktree, "%s/discussions/%s/notes" % (_mr_path(run), discussion), method="POST", fields={"body": body})
        if resolve:
            api(worktree, "%s/discussions/%s?resolved=true" % (_mr_path(run), discussion), method="PUT")
    with run.lock():
        evidence = run.external().get("duo")
        if evidence and resolve:
            for thread in evidence.get("threads", []):
                if thread["discussion_id"] in discussions:
                    thread["resolved"] = True
            evidence["unresolved"] = sum(1 for t in evidence["threads"] if not t["resolved"])
            run.save_external("duo", evidence)
        run.event("duo_replied", {"finding": rec["id"], "discussions": discussions, "resolved": resolve})
    return rec
