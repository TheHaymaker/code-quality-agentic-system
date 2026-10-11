"""The findings contract: what a reviewer may submit, how it is stored, and who
may change a finding's status. These functions work on plain dicts; the CLI
loads and saves them.
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .util import CqError, iso, parse_time, sha256_text

OPEN = ("unverified", "verified", "contested")
CLOSED = ("fixed", "filed", "rejected", "waived")
GATE_GUARD = "gate_guard"

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "finding-submission.schema.json"


def submission_schema() -> Dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def rank(severity: str) -> int:
    return config.SEVERITIES.index(severity) if severity in config.SEVERITIES else -1


def is_gating(finding: Dict[str, Any]) -> bool:
    """BLOCKER, MAJOR and always-blocking findings are the ones a gate looks at."""
    return bool(finding.get("always_blocking")) or finding["severity"] in ("BLOCKER", "MAJOR")


def finding_id(reviewer: str, item: Dict[str, Any]) -> str:
    claim = re.sub(r"\s+", " ", item["claim"].strip().lower())
    path = (item.get("location") or {}).get("path", "")
    return "f-" + sha256_text("|".join([reviewer, path, item["category"], claim]))[:10]


def _evidence_gap(item: Dict[str, Any]) -> Optional[str]:
    """The evidence bar: file and line, a failure scenario, a proposed fix."""
    loc = item.get("location") or {}
    missing = []
    if not loc.get("path") or not loc.get("start_line"):
        missing.append("file and line")
    if not (item.get("scenario") or "").strip():
        missing.append("failure scenario")
    if not (item.get("proposed_fix") or "").strip():
        missing.append("proposed fix")
    return ", ".join(missing) if missing else None


def presented_fixes(findings: Dict[str, Any], reviewer: str) -> List[str]:
    """The fixed findings a reviewer is asked to confirm in its next review:
    its own, and gating ones that no local reviewer raised (Duo threads)."""
    out = []
    for f in active(findings):
        if f["status"] != "fixed" or f.get("closed_by_source"):
            continue
        agents = {s["reviewer"] for s in f["sources"] if s["kind"] == "agent"}
        if reviewer in agents or (not agents and is_gating(f)):
            out.append(f["id"])
    return sorted(out)


def check_submission(
    run: Dict[str, Any],
    stored: List[Dict[str, Any]],
    reviewer: str,
    head: str,
    submission: Any,
    fix_diff_files: Optional[List[str]] = None,
    presented: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors mean the submission is refused."""
    errors: List[str] = []
    warnings: List[str] = []
    if reviewer not in config.REVIEWERS:
        return ["unknown reviewer '%s'. Known: %s" % (reviewer, ", ".join(config.REVIEWERS))], warnings
    try:
        config.validate(submission, submission_schema(), "submission")
    except CqError as exc:
        return [str(exc)], warnings
    if head != run["head_sha"]:
        errors.append(
            "reviewed commit %s is not the run's head %s. Review the current head." % (head[:12], run["head_sha"][:12])
        )
    scope = submission["scope"]
    if any(
        e["source"]["reviewer"] == reviewer and e["revision"]["head_sha"] == head and e["scope"] == scope
        for e in stored
    ):
        errors.append("%s already submitted a %s review of %s" % (reviewer, scope, head[:12]))
    if scope == "fix_diff":
        start = submission.get("reviewed_from")
        if not start:
            errors.append("a fix_diff review must set reviewed_from to the commit the fix diff starts at")
        elif start not in run["shas"]:
            errors.append("reviewed_from %s is not a commit this run has seen" % start[:12])
        expected = fix_diff_files or []
    else:
        if submission.get("reviewed_from"):
            errors.append("reviewed_from is only for fix_diff reviews")
        expected = run["changed_files"]
    seen = set(submission["files_reviewed"]) | {s["path"] for s in submission.get("files_skipped", [])}
    unread = sorted(set(expected) - seen)
    if unread:
        errors.append(
            "these changed files are in neither files_reviewed nor files_skipped: %s" % ", ".join(unread[:20])
        )
    unknown = sorted(set(submission.get("not_fixed", [])) - set(presented or []))
    if unknown:
        errors.append("not_fixed names findings that are not in your brief's list of fixed findings: %s" % ", ".join(unknown))
    for i, item in enumerate(submission["findings"]):
        gap = _evidence_gap(item)
        if gap and item["severity"] != "NIT":
            warnings.append("findings[%d] will be stored as NIT: missing %s" % (i, gap))
    return errors, warnings


def ingest(
    findings: Dict[str, Any],
    reviewer: str,
    source: Dict[str, Any],
    head: str,
    base: str,
    submission: Dict[str, Any],
    presented: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Store a checked submission. Returns the stored envelope and the ids touched.
    `presented` is the list of fixed findings this reviewer was asked to confirm."""
    touched: List[str] = []
    not_fixed = list(submission.get("not_fixed", []))
    for fid in not_fixed:
        rec = findings[fid]
        rec["fix_commit"] = None
        rec["sources"].append(dict(source, reviewer=reviewer, head_sha=head, scope=submission["scope"], local_id=None))
        _set(rec, "verified", reviewer, "Reviewed the fix: not fixed")
        touched.append(fid)
    confirmed = [fid for fid in (presented or []) if fid not in not_fixed]
    for item in submission["findings"]:
        fid = finding_id(reviewer, item)
        severity = item["severity"]
        gap = _evidence_gap(item)
        demoted = None
        if gap and severity != "NIT":
            demoted = "Missing %s" % gap
            severity = "NIT"
        src = dict(source, reviewer=reviewer, head_sha=head, scope=submission["scope"], local_id=item.get("local_id"))
        rec = findings.get(fid)
        while rec is not None and rec.get("merged_into"):
            rec = findings[rec["merged_into"]]
        if rec is None:
            # final_check has a veto: its gating findings go straight to a human.
            status = "unverified"
            if reviewer == "final_check" and (severity in ("BLOCKER", "MAJOR") or item.get("always_blocking")):
                status = "contested"
            rec = {
                "id": fid,
                "severity": severity,
                "claimed_severity": item["severity"],
                "demoted_reason": demoted,
                "category": item["category"],
                "always_blocking": item.get("always_blocking"),
                "location": item.get("location"),
                "claim": item["claim"],
                "scenario": item.get("scenario"),
                "proposed_fix": item.get("proposed_fix"),
                "verification": item.get("verification"),
                "confidence": item.get("confidence"),
                "sources": [src],
                "status": status,
                "history": [{"at": iso(), "by": reviewer, "status": status, "reason": "Reported"}],
                "ticket": None,
                "fix_commit": None,
                "merged_into": None,
                "waiver": None,
                "decided_by_human": False,
                "closed_by_source": False,
                "created_at": iso(),
            }
            findings[fid] = rec
        else:
            fid = rec["id"]
            rec["sources"].append(src)
            # Reported again. A severity can rise on a new report and never falls.
            raised = rank(severity) > rank(rec["severity"])
            if raised:
                rec["severity"], rec["claimed_severity"], rec["demoted_reason"] = severity, item["severity"], demoted
            if item.get("always_blocking") and not rec.get("always_blocking"):
                rec["always_blocking"] = item["always_blocking"]
                raised = True
            for key in ("location", "scenario", "proposed_fix", "verification"):
                if item.get(key) and (raised or not rec.get(key)):
                    rec[key] = item[key]
            veto = reviewer == "final_check" and is_gating(rec)
            if rec["status"] == "fixed":
                rec["fix_commit"] = None
                _set(rec, "contested" if veto else "verified", reviewer, "Reported again after the fix")
            elif raised and rec["status"] in CLOSED:
                # A closed finding that comes back worse is open again. If a
                # human closed it, a human looks at it again.
                again = "contested" if (rec["decided_by_human"] or veto) else "unverified"
                rec["decided_by_human"] = False
                rec["waiver"] = None
                _set(rec, again, reviewer, "Reported again at a higher severity")
            elif veto and rec["status"] in ("unverified", "verified"):
                _set(rec, "contested", reviewer, "Vetoed by the final check")
        touched.append(fid)
    envelope = {
        "schema_version": "1.0",
        "source": dict(source, reviewer=reviewer),
        "revision": {"base_sha": base, "head_sha": head},
        "scope": submission["scope"],
        "reviewed_from": submission.get("reviewed_from"),
        "files_reviewed": submission["files_reviewed"],
        "files_skipped": submission.get("files_skipped", []),
        # Fixed findings this reviewer was shown and did not object to.
        "fixed_presented": confirmed,
        "finding_ids": touched,
        "received_at": iso(),
    }
    return envelope, touched


def _set(rec: Dict[str, Any], status: str, by: str, reason: str, evidence: Optional[str] = None) -> None:
    rec["status"] = status
    entry = {"at": iso(), "by": by, "status": status, "reason": reason}
    if evidence:
        entry["evidence"] = evidence
    rec["history"].append(entry)


def last_report(rec: Dict[str, Any], index: Dict[str, int]) -> int:
    """Position, in the run's commit list, of the newest commit this finding was reported on."""
    return max((index[s["head_sha"]] for s in rec["sources"] if s["head_sha"] in index), default=-1)


def keeper(findings: Dict[str, Any], rec: Dict[str, Any]) -> Dict[str, Any]:
    """Follow merges to the finding that now stands for this one."""
    while rec.get("merged_into") and rec["merged_into"] in findings:
        rec = findings[rec["merged_into"]]
    return rec


def get(findings: Dict[str, Any], fid: str) -> Dict[str, Any]:
    if fid in findings:
        return findings[fid]
    matches = [k for k in findings if k.startswith(fid)]
    if len(matches) == 1:
        return findings[matches[0]]
    raise CqError("no single finding matches '%s'" % fid)


def _is_guard(rec: Dict[str, Any]) -> bool:
    return any(s["reviewer"] == GATE_GUARD for s in rec["sources"])


def _is_system(rec: Dict[str, Any]) -> bool:
    return any(s["reviewer"] in (GATE_GUARD, "sonar") for s in rec["sources"])


def merge(findings: Dict[str, Any], keep_id: str, dup_ids: List[str], by: str) -> Dict[str, Any]:
    """Fold duplicates into one finding. Severity is the highest of the group:
    code decides that, not the agent that asked for the merge."""
    keep = get(findings, keep_id)
    if keep.get("merged_into"):
        raise CqError("%s was already merged into %s" % (keep["id"], keep["merged_into"]))
    for dup_id in dup_ids:
        dup = get(findings, dup_id)
        if dup["id"] == keep["id"]:
            raise CqError("cannot merge a finding into itself")
        if dup.get("merged_into"):
            raise CqError("%s was already merged into %s" % (dup["id"], dup["merged_into"]))
        if dup["decided_by_human"] or keep["decided_by_human"]:
            raise CqError("a finding a human decided cannot be merged")
        if _is_system(dup) or _is_system(keep):
            raise CqError("gate-guard and SonarQube findings cannot be merged; their source opens and closes them")
        if config.UNTRIAGED in (dup["severity"], keep["severity"]):
            raise CqError("triage the finding before merging it")
        if rank(dup["severity"]) > rank(keep["severity"]):
            keep["severity"] = dup["severity"]
        keep["always_blocking"] = keep.get("always_blocking") or dup.get("always_blocking")
        keep["sources"].extend(dup["sources"])
        # The group is only as settled as its least settled member.
        order = ["contested", "unverified", "verified"]
        if dup["status"] in OPEN and (keep["status"] not in OPEN or order.index(dup["status"]) < order.index(keep["status"])):
            _set(keep, dup["status"], by, "Took status from merged finding %s" % dup["id"])
        dup["merged_into"] = keep["id"]
        dup["history"].append({"at": iso(), "by": by, "status": dup["status"], "reason": "Merged into %s" % keep["id"]})
    return keep


def set_status(
    findings: Dict[str, Any],
    run: Dict[str, Any],
    gate_tier: int,
    fid: str,
    status: str,
    by: str,
    reason: str,
    evidence: Optional[str] = None,
    ticket: Optional[str] = None,
    commit: Optional[str] = None,
) -> Dict[str, Any]:
    """Status changes an agent may make. Anything that would dismiss a gating
    finding is refused here and left for `cq decide`, which only a human runs."""
    rec = get(findings, fid)
    if rec.get("merged_into"):
        raise CqError("%s was merged into %s; change that one" % (rec["id"], rec["merged_into"]))
    if rec["decided_by_human"]:
        raise CqError("%s was decided by a human and cannot be changed" % rec["id"])
    if _is_guard(rec):
        raise CqError("%s is a gate-guard finding; only a human decides it (cq decide)" % rec["id"])
    if not (reason or "").strip():
        raise CqError("a reason is required")
    if status not in ("verified", "contested", "fixed", "filed", "rejected"):
        raise CqError("an agent may set: verified, contested, fixed, filed, rejected")
    if rec["status"] == "contested":
        raise CqError("%s is contested and waits for a human decision (cq decide)" % rec["id"])
    if rec["severity"] == config.UNTRIAGED and status != "contested":
        raise CqError("%s has no severity yet; run `cq findings triage` first" % rec["id"])
    gating = is_gating(rec)
    if status == "verified":
        if not (evidence or "").strip():
            raise CqError("verified needs --evidence: the failing test or the steps that reproduce it")
    elif status == "fixed":
        index = {sha: i for i, sha in enumerate(run["shas"])}
        if any(s["reviewer"] == "sonar" for s in rec["sources"]):
            raise CqError("a SonarQube finding closes when SonarQube stops reporting it. Push the fix, then run `cq sonar gate`.")
        if not commit or commit not in index:
            raise CqError("fixed needs --commit, and the commit must be one this run has seen (run `cq run sync` first)")
        if index[commit] <= last_report(rec, index):
            raise CqError("the fix commit must come after the commit the finding was reported on")
        round_start = run.get("fix_round_start")
        if round_start is None or round_start not in index:
            raise CqError("no fix round is open. Start one with `cq fix round`, so the fix counts against the budget.")
        if index[round_start] < last_report(rec, index):
            raise CqError("this finding was reported after the current fix round began. Start a new fix round for it (cq fix round).")
        if index[commit] <= index[round_start]:
            raise CqError("the fix commit must come after the start of the current fix round")
        rec["fix_commit"] = commit
    elif status == "filed":
        if not (ticket or "").strip():
            raise CqError("filed needs --ticket")
        if rec.get("always_blocking") or rec["severity"] == "BLOCKER":
            raise CqError("a BLOCKER or always-blocking finding cannot be filed for later; fix it or mark it contested")
        if rec["severity"] == "MAJOR" and gate_tier >= 2:
            raise CqError("at tier %d a MAJOR finding must be fixed; mark it contested if you disagree" % gate_tier)
        rec["ticket"] = ticket
    elif status == "rejected":
        if gating:
            raise CqError("only a human can reject a BLOCKER, MAJOR or always-blocking finding; mark it contested")
    if ticket and status != "filed":
        rec["ticket"] = ticket
    _set(rec, status, by, reason, evidence)
    return rec


def triage(
    findings: Dict[str, Any],
    fid: str,
    severity: str,
    category: str,
    scenario: str,
    proposed_fix: Optional[str],
    always_blocking: Optional[str],
    by: str,
) -> Dict[str, Any]:
    """Give an external comment (a Duo thread) a severity. Not for agent findings."""
    rec = get(findings, fid)
    if rec["severity"] != config.UNTRIAGED:
        raise CqError("%s already has a severity; only untriaged external findings are triaged" % rec["id"])
    if severity not in config.SEVERITIES:
        raise CqError("severity must be one of %s" % ", ".join(config.SEVERITIES))
    if category not in config.CATEGORIES:
        raise CqError("category must be one of %s" % ", ".join(config.CATEGORIES))
    if always_blocking and always_blocking not in config.ALWAYS_BLOCKING:
        raise CqError("always_blocking must be one of %s" % ", ".join(config.ALWAYS_BLOCKING))
    if not (scenario or "").strip():
        raise CqError("a scenario is required: what fails, for whom, under which input")
    rec["severity"] = severity
    rec["category"] = category
    rec["scenario"] = scenario
    rec["proposed_fix"] = proposed_fix
    rec["always_blocking"] = always_blocking
    if rec["status"] not in OPEN:
        rec["status"] = "unverified"
    rec["history"].append({"at": iso(), "by": by, "status": rec["status"], "reason": "Triaged as %s" % severity})
    return rec


def decide(
    findings: Dict[str, Any],
    floor: Dict[str, Any],
    tier: int,
    fid: str,
    decision: str,
    by: str,
    reason: str,
    days: Optional[int],
    ticket: Optional[str],
    at: datetime.datetime,
) -> Dict[str, Any]:
    """A human decision on a finding. The CLI checks that a human is present."""
    rec = get(findings, fid)
    if rec.get("merged_into"):
        raise CqError("%s was merged into %s; decide that one" % (rec["id"], rec["merged_into"]))
    if not (reason or "").strip():
        raise CqError("a reason is required")
    if decision == "reject":
        rec["waiver"] = None
        _set(rec, "rejected", by, reason)
    elif decision == "waive":
        if rec.get("always_blocking") or rec["severity"] == "BLOCKER":
            raise CqError("a BLOCKER or always-blocking finding cannot be waived. Fix it, or reject it if it is not real.")
        limit = floor["waiver_max_days"][str(tier)]
        days = days or limit
        if days > limit:
            raise CqError("a waiver at tier %d lasts at most %d days" % (tier, limit))
        rec["waiver"] = {"by": by, "reason": reason, "expires": iso(at + datetime.timedelta(days=days))}
        _set(rec, "waived", by, reason)
    elif decision == "reopen":
        rec["waiver"] = None
        _set(rec, "verified", by, reason)
    else:
        raise CqError("decision must be reject, waive or reopen")
    if ticket:
        rec["ticket"] = ticket
    rec["decided_by_human"] = decision != "reopen"
    return rec


def upsert_system(
    findings: Dict[str, Any],
    fid: str,
    reviewer: str,
    kind: str,
    head: str,
    fields: Dict[str, Any],
    status: str,
    reopen: bool = False,
) -> Tuple[Dict[str, Any], bool]:
    """Create or refresh a finding that code, Duo or SonarQube produced.
    Returns (record, created). An existing record keeps its status, with one
    exception: with `reopen`, a finding marked fixed that its source reports
    again is open again."""
    rec = findings.get(fid)
    if rec is not None:
        for key in ("claim", "location"):
            if key in fields:
                rec[key] = fields[key]
        if reopen and rec["status"] == "fixed" and not rec["decided_by_human"]:
            rec["fix_commit"] = None
            rec["closed_by_source"] = False
            rec["sources"].append({"reviewer": reviewer, "kind": kind, "model": None, "family": None,
                                   "head_sha": head, "scope": "full", "local_id": fields.get("local_id")})
            _set(rec, status, reviewer, "Reported again after it was marked fixed")
        return rec, False
    rec = {
        "id": fid,
        "severity": fields["severity"],
        "claimed_severity": fields["severity"],
        "demoted_reason": None,
        "category": fields.get("category"),
        "always_blocking": None,
        "location": fields.get("location"),
        "claim": fields["claim"],
        "scenario": fields.get("scenario"),
        "proposed_fix": fields.get("proposed_fix"),
        "verification": None,
        "confidence": None,
        "sources": [{"reviewer": reviewer, "kind": kind, "model": None, "family": None,
                     "head_sha": head, "scope": "full", "local_id": fields.get("local_id")}],
        "status": status,
        "history": [{"at": iso(), "by": reviewer, "status": status, "reason": "Reported"}],
        "ticket": None,
        "fix_commit": None,
        "merged_into": None,
        "waiver": None,
        "decided_by_human": False,
        "closed_by_source": False,
        "created_at": iso(),
    }
    findings[fid] = rec
    return rec, True


def close_system(rec: Dict[str, Any], by: str, head: str, reason: str) -> None:
    """The source no longer reports this finding at the current head."""
    if rec["status"] in OPEN and not rec["decided_by_human"]:
        rec["fix_commit"] = head
        rec["closed_by_source"] = True   # the source itself says it is gone; nothing left to confirm
        _set(rec, "fixed", by, reason)


def waiver_expired(rec: Dict[str, Any], at: datetime.datetime) -> bool:
    return bool(rec.get("waiver")) and parse_time(rec["waiver"]["expires"]) <= at


def active(findings: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [f for f in findings.values() if not f.get("merged_into")]
