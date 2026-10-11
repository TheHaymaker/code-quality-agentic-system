"""The policy evaluator. A pure function: same inputs, same verdict. It reads no
files, calls no model and runs no command. Agents supply evidence; only this
function turns evidence into a verdict.

Verdicts:
    FAIL          the evidence shows the change does not meet the bar
    INCONCLUSIVE  required evidence is missing, stale or unverified
    BLOCKED       the evidence is complete and a human decision is needed
    PASS          every requirement for the tier is met

When several apply, the first in that order is the headline. Missing evidence
is never a pass, and a human is asked only once the machine evidence is in.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List

from . import config, findings as F

FAIL, INCONCLUSIVE, BLOCKED, PASS = "FAIL", "INCONCLUSIVE", "BLOCKED", "PASS"
ORDER = [FAIL, INCONCLUSIVE, BLOCKED, PASS]
EXIT_CODES = {PASS: 0, FAIL: 1, BLOCKED: 2, INCONCLUSIVE: 3}


def gate_tier(run: Dict[str, Any]) -> int:
    return 0 if run["mode"] == "tracer" else run["tier"]


def evaluate(
    run: Dict[str, Any],
    floor: Dict[str, Any],
    repo: Dict[str, Any],
    checks: Dict[str, Dict[str, Any]],
    envelopes: List[Dict[str, Any]],
    findings: Dict[str, Any],
    external: Dict[str, Any],
    approvals: List[Dict[str, Any]],
    notes: Dict[str, Dict[str, Any]],
    at: datetime.datetime,
    stage: str = "final",
) -> Dict[str, Any]:
    """`stage` is "local" before the merge request exists (Duo, SonarQube and
    the final sign-off are not asked for yet) and "final" after."""
    assert stage in ("local", "final")
    reasons: List[Dict[str, str]] = []
    requirements: List[Dict[str, str]] = []
    waived_requirements: List[Dict[str, str]] = []

    def reason(verdict: str, code: str, message: str, **subject: Any) -> None:
        """`subject` names what the reason is about (check, reviewer, finding), for
        machines that act on a verdict. The message is for people."""
        reasons.append({"verdict": verdict, "code": code, "message": message, "subject": subject})

    def require(kind: str, name: str, status: str, detail: str = "") -> None:
        requirements.append({"kind": kind, "name": name, "status": status, "detail": detail})

    head = run["head_sha"]
    index = {sha: i for i, sha in enumerate(run["shas"])}
    tier = gate_tier(run)
    gate = floor["gates"][str(tier)]
    not_applicable = repo.get("not_applicable", {})

    def skipped(kind: str, name: str) -> bool:
        if name in not_applicable:
            waived_requirements.append({"kind": kind, "name": name, "reason": not_applicable[name]})
            require(kind, name, "not applicable", not_applicable[name])
            return True
        return False

    # -- the run itself --------------------------------------------------
    if run["dirty"]:
        reason(INCONCLUSIVE, "worktree_dirty", "The worktree has uncommitted changes, so no evidence covers them.")
    if repo.get("sensitive") and run["unmapped"]:
        reason(
            BLOCKED,
            "unmapped_paths",
            "This repository is marked sensitive and no rule covers: %s. Add a rule in the control repo, then repin."
            % ", ".join(run["unmapped"][:10]),
        )
    if run.get("budget_exhausted"):
        reason(BLOCKED, "budget_spent", "The %s fix budget is spent. A human takes over." % run["budget_exhausted"])

    # -- deterministic checks -------------------------------------------
    for name in gate["checks"]:
        if skipped("check", name):
            continue
        result = checks.get(name)
        if name not in repo["checks"]:
            require("check", name, "missing", "no command registered")
            reason(INCONCLUSIVE, "check_unregistered", "Check '%s' is required and registry.yml has no command for it." % name, check=name)
        elif result is None:
            require("check", name, "missing")
            reason(INCONCLUSIVE, "check_missing", "Check '%s' has not run." % name, check=name)
        elif result["head_sha"] != head:
            require("check", name, "stale", "ran on %s" % result["head_sha"][:12])
            reason(INCONCLUSIVE, "check_stale", "Check '%s' ran on an older commit." % name, check=name)
        elif result["status"] == "passed":
            require("check", name, "passed")
        elif result["status"] == "failed":
            require("check", name, "failed", "exit code %s" % result["exit_code"])
            reason(FAIL, "check_failed", "Check '%s' failed with exit code %s." % (name, result["exit_code"]), check=name)
        else:
            require("check", name, "error", result.get("detail") or "")
            reason(INCONCLUSIVE, "check_error", "Check '%s' could not run: %s." % (name, result.get("detail")), check=name)

    # -- reviews ----------------------------------------------------------
    head_pos = index[head]
    live = [e for e in envelopes if e["revision"]["head_sha"] in index]
    fix_reviews = [e for e in live if e["scope"] == "fix_diff" and e.get("reviewed_from") in index]
    carry = floor["review_carry"]
    carry_limit = min(carry["max_lines"], run["changed_lines"] * carry["max_percent"] // 100)

    def chain_reaches_head(position: int, only: Any = None) -> bool:
        """Do fix-diff reviews cover every commit after `position`, with no gap?"""
        while position < head_pos:
            steps = [
                index[e["revision"]["head_sha"]] for e in fix_reviews
                if (only is None or e["source"]["reviewer"] == only)
                and index[e["reviewed_from"]] <= position < index[e["revision"]["head_sha"]]
            ]
            if not steps:
                return False
            position = max(steps)
        return True

    latest_full: Dict[str, Dict[str, Any]] = {}
    for name in gate["reviewers"]:
        if skipped("review", name):
            continue
        fulls = [e for e in live if e["source"]["reviewer"] == name and e["scope"] == "full"]
        if not fulls:
            require("review", name, "missing")
            reason(INCONCLUSIVE, "review_missing", "Reviewer '%s' has not reviewed this change." % name, reviewer=name)
            continue
        latest = max(fulls, key=lambda e: index[e["revision"]["head_sha"]])
        latest_full[name] = latest
        reviewed = latest["revision"]["head_sha"]
        if reviewed == head:
            require("review", name, "current")
            continue
        # The code changed after this reviewer's full review. The review still
        # counts only if the change since is small and fix-diff reviews cover it.
        since = run.get("lines_since", {}).get(reviewed)
        if since is None or since > carry_limit:
            require("review", name, "stale", "%s lines changed since its review" % ("many" if since is None else since))
            reason(INCONCLUSIVE, "review_too_old", "Too much changed since '%s' reviewed (%s lines, limit %d). It needs a full review." % (name, "unknown" if since is None else since, carry_limit), reviewer=name)
        elif name in carry["never_for"] and not chain_reaches_head(index[reviewed], only=name):
            require("review", name, "stale", "must review each fix itself")
            reason(INCONCLUSIVE, "review_stale", "Reviewer '%s' must review every change itself and has not reviewed the latest commits." % name, reviewer=name)
        elif not chain_reaches_head(index[reviewed]):
            require("review", name, "stale", "reviewed %s" % reviewed[:12])
            reason(INCONCLUSIVE, "review_stale", "The code changed after '%s' reviewed it and no fix-diff review covers the change." % name, reviewer=name)
        else:
            require("review", name, "current", "earlier review plus fix-diff review")

    implementer_family = (run["implementer"]["family"] or "").lower()
    adversary = latest_full.get("adversary")
    if adversary is not None and (adversary["source"].get("family") or "").lower() == implementer_family:
        reason(INCONCLUSIVE, "adversary_not_independent", "The adversary used the implementer's model family (%s)." % implementer_family)
    second = latest_full.get("adversary2")
    if adversary is not None and second is not None and second["source"].get("model") == adversary["source"].get("model"):
        reason(INCONCLUSIVE, "adversary2_same_model", "Both adversaries used the same model.")

    # -- notes --------------------------------------------------------------
    for name in gate["notes"]:
        if skipped("note", name):
            continue
        note = notes.get(name)
        if note is None:
            require("note", name, "missing")
            reason(INCONCLUSIVE, "note_missing", "No '%s' note is on file." % name, note=name)
        elif name == "plan_review" and note.get("plan_sha256") != (notes.get("plan") or {}).get("sha256"):
            require("note", name, "stale", "the plan changed after this review")
            reason(INCONCLUSIVE, "plan_review_stale", "The plan changed after it was reviewed. It needs a new plan review.")
        elif name == "plan_review" and note.get("outcome") != "ok":
            require("note", name, "negative", "the review asks for changes")
            reason(FAIL, "plan_review_negative", "The plan review asks for changes to the plan.")
        else:
            require("note", name, "on file")

    # -- Duo and SonarQube --------------------------------------------------
    if stage == "final":
        for name in gate["external"]:
            if skipped("external", name):
                continue
            evidence = external.get(name)
            if evidence is None:
                require("external", name, "missing")
                reason(INCONCLUSIVE, name + "_missing", "No %s evidence has been collected." % name)
                continue
            if evidence.get("head_sha") != head:
                require("external", name, "stale", "collected for %s" % str(evidence.get("head_sha"))[:12])
                reason(INCONCLUSIVE, name + "_stale", "The %s evidence is for an older commit." % name)
                continue
            state = evidence["state"]
            detail = evidence.get("detail") or ""
            if name == "duo":
                if state != "reviewed":
                    require("external", name, state, detail)
                    reason(INCONCLUSIVE, "duo_" + state, "GitLab Duo review is not complete (%s). %s" % (state, detail))
                    continue
                require("external", name, "reviewed")
                open_threads = []
                for thread in evidence.get("threads", []):
                    if thread["resolved"]:
                        continue
                    rec = findings.get(thread["finding_id"])
                    if rec is not None and F.keeper(findings, rec)["status"] == "contested":
                        continue  # waiting for a human; every contested finding is reported below
                    open_threads.append(thread["discussion_id"])
                if open_threads:
                    reason(FAIL, "duo_threads_open", "%d Duo thread(s) have no reply and resolution on the merge request." % len(open_threads))
            else:
                if state == "passed":
                    require("external", name, "passed", detail)
                elif state == "failed":
                    require("external", name, "failed", detail)
                    reason(FAIL, "sonar_failed", "The SonarQube quality gate failed. %s" % detail)
                else:
                    require("external", name, state, detail)
                    reason(INCONCLUSIVE, "sonar_" + state, "No usable SonarQube result (%s). %s" % (state, detail))

    # -- findings -----------------------------------------------------------
    counts = {s: 0 for s in config.SEVERITIES + [config.UNTRIAGED]}
    for f in F.active(findings):
        label = "%s %s (%s)" % (f["severity"], f["id"], (f["claim"] or "")[:80])
        status = f["status"]
        gating = F.is_gating(f)
        if status in F.OPEN:
            counts[f["severity"]] += 1
        if status == "contested":
            # Whatever its severity: someone asked for a human, so a human answers.
            reason(BLOCKED, "needs_human", "%s is contested and needs your decision." % label, finding=f["id"])
        elif status == "waived":
            if F.waiver_expired(f, at):
                reason(FAIL, "waiver_expired", "The risk acceptance for %s has expired." % label, finding=f["id"])
        elif status == "rejected":
            if gating and not f["decided_by_human"]:
                reason(BLOCKED, "needs_human", "%s was rejected without a human decision." % label, finding=f["id"])
        elif status == "fixed":
            fix = f.get("fix_commit")
            if fix not in index:
                reason(INCONCLUSIVE, "fix_unknown", "%s is marked fixed by a commit this run has not seen." % label, finding=f["id"])
            elif f.get("closed_by_source"):
                pass  # SonarQube or the diff itself stopped reporting it
            elif index[fix] <= F.last_report(f, index):
                reason(INCONCLUSIVE, "fix_unknown", "%s is marked fixed by a commit that is not after its report." % label, finding=f["id"])
            elif gating:
                # Marking a finding fixed is a claim, not evidence. A reviewer must
                # have been shown the claim, in a review that covers the fix commit.
                def confirmed(reviewer: Any) -> Any:
                    spots = [
                        index[e["revision"]["head_sha"]] for e in live
                        if (reviewer is None or e["source"]["reviewer"] == reviewer)
                        and f["id"] in e.get("fixed_presented", [])
                        and index[e["revision"]["head_sha"]] >= index[fix]
                        and (e["scope"] == "full" or index.get(e.get("reviewed_from"), head_pos + 1) < index[fix])
                    ]
                    return max(spots) if spots else None

                raisers = sorted({s["reviewer"] for s in f["sources"] if s["kind"] == "agent"})
                for raiser in raisers:
                    spot = confirmed(raiser)
                    if spot is None:
                        reason(INCONCLUSIVE, "fix_not_rereviewed", "%s is marked fixed and '%s', who raised it, has not reviewed the fix." % (label, raiser), finding=f["id"], reviewer=raiser)
                    elif not chain_reaches_head(spot, only=raiser):
                        # A later commit can undo a fix. The raiser keeps watching until the end.
                        reason(INCONCLUSIVE, "fix_not_rereviewed", "%s was fixed, and '%s', who raised it, has not reviewed the commits made since." % (label, raiser), finding=f["id"], reviewer=raiser)
                if not raisers and confirmed(None) is None:
                    reason(INCONCLUSIVE, "fix_not_rereviewed", "%s is marked fixed and no reviewer has looked at the fix." % label, finding=f["id"], reviewer=None)
        elif status == "filed":
            if f.get("always_blocking") or f["severity"] == "BLOCKER":
                reason(FAIL, "cannot_file", "%s must be fixed; it cannot be filed for later." % label, finding=f["id"])
            elif f["severity"] == "MAJOR" and tier >= 2:
                reason(FAIL, "cannot_file", "%s must be fixed at tier %d." % (label, tier), finding=f["id"])
            elif not f.get("ticket"):
                reason(INCONCLUSIVE, "no_ticket", "%s is marked filed and has no ticket." % label, finding=f["id"])
        elif f["severity"] == config.UNTRIAGED:
            reason(INCONCLUSIVE, "untriaged", "%s has not been triaged." % label, finding=f["id"])
        elif not gating:
            pass  # open MINOR and NIT findings never gate
        elif status == "unverified":
            reason(INCONCLUSIVE, "unverified", "%s has not been verified." % label, finding=f["id"])
        else:
            if f["severity"] == "MAJOR" and tier < 2 and not f.get("always_blocking"):
                reason(FAIL, "finding_open", "%s is open. Fix it or file a ticket." % label, finding=f["id"])
            else:
                reason(FAIL, "finding_open", "%s is open and must be fixed." % label, finding=f["id"])

    # -- human approvals ------------------------------------------------------
    for name in gate["approvals"]:
        if name == "final_signoff" and stage == "local":
            continue
        if name == "plan_approval":
            plan = notes.get("plan")
            ok = plan is not None and any(
                a["kind"] == name and a.get("plan_sha256") == plan["sha256"] for a in approvals
            )
            detail = "for the plan on file"
        else:
            ok = any(a["kind"] == name and a.get("head_sha") == head for a in approvals)
            detail = "for commit %s" % head[:12]
        if ok:
            require("approval", name, "recorded", detail)
        else:
            require("approval", name, "missing", detail)
            reason(BLOCKED, name + "_missing", "Your %s is required %s." % (name.replace("_", " "), detail))

    verdict = PASS
    for candidate in ORDER:
        if any(r["verdict"] == candidate for r in reasons):
            verdict = candidate
            break
    reasons.sort(key=lambda r: ORDER.index(r["verdict"]))
    tracer = run["mode"] == "tracer"
    return {
        "verdict": verdict,
        "stage": stage,
        "run_id": run["run_id"],
        "head_sha": head,
        "base_sha": run["base_sha"],
        "tier": run["tier"],
        "gate_tier": tier,
        "mode": run["mode"],
        "ready_to_merge": verdict == PASS and stage == "final" and not tracer,
        "human_action": "Tracer branch: never merges to the default branch." if tracer else gate["human"],
        "reasons": reasons,
        "requirements": requirements,
        "not_applicable": waived_requirements,
        "open_findings": counts,
        "policy_digest": run["policy_digest"],
        "evaluated_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
