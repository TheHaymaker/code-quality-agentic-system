"""Accepting a reviewer's submission. One path for every caller: the CLI, the
external adversary runner and the engine."""
from __future__ import annotations

from typing import Any, Dict

from . import briefs, findings as F, runs, store


def submit(run: store.Run, reviewer: str, head: str, submission: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Validate and, unless `dry_run`, store a review. Returns
    {accepted, errors, warnings, touched, scope, head}. Never raises for a bad submission."""
    data = runs.sync(run)
    matches = [s for s in data["shas"] + [data["base_sha"]] if head and s.startswith(head)]
    head = matches[0] if len(set(matches)) == 1 else head
    start = submission.get("reviewed_from") if isinstance(submission, dict) else None
    fix_files = runs.files_between(data, start, head) if start in data["shas"] and head == data["head_sha"] else None
    with run.lock():
        findings = run.findings()
        presented = F.presented_fixes(findings, reviewer)
        errors, warnings = F.check_submission(data, run.envelopes(), reviewer, head, submission, fix_files, presented)
        result: Dict[str, Any] = {"accepted": False, "errors": errors, "warnings": warnings, "touched": [], "head": head,
                                  "scope": submission.get("scope") if isinstance(submission, dict) else None}
        if errors or dry_run:
            return result
        envelope, touched = F.ingest(findings, reviewer, briefs.source_for(run, reviewer), head, data["base_sha"], submission, presented)
        run.save_findings(findings)
        run.save_envelope(envelope)
        run.event("review_received", {"reviewer": reviewer, "scope": envelope["scope"], "head": head, "findings": touched})
        result.update(accepted=True, touched=touched, scope=envelope["scope"])
        return result
