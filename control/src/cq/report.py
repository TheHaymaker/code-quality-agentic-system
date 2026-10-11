"""The evidence report: one screen a human can decide from."""
from __future__ import annotations

from typing import Any, Dict, List

from . import config, findings as F

_MARK = {"PASS": "PASS", "FAIL": "FAIL", "BLOCKED": "BLOCKED: your decision is needed", "INCONCLUSIVE": "INCONCLUSIVE: evidence is missing"}


def _loc(f: Dict[str, Any]) -> str:
    loc = f.get("location") or {}
    if not loc.get("path"):
        return "-"
    return "%s:%s" % (loc["path"], loc["start_line"]) if loc.get("start_line") else loc["path"]


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")[:160]


def render(decision: Dict[str, Any], run: Dict[str, Any], findings: Dict[str, Any]) -> str:
    tier = decision["tier"]
    out: List[str] = []
    out.append("## Quality run: %s" % _MARK[decision["verdict"]])
    out.append("")
    stage = "before the merge request" if decision["stage"] == "local" else "final"
    out.append("| | |")
    out.append("| --- | --- |")
    out.append("| Run | `%s` |" % run["run_id"])
    out.append("| Ticket | %s |" % (run["ticket"] or "none"))
    tier_text = "%d %s" % (tier, config.TIER_NAMES[tier])
    if decision["mode"] == "tracer":
        tier_text += " (tracer branch: tier 0 gates, never merges)"
    out.append("| Tier | %s |" % tier_text)
    out.append("| Commit | `%s` (base `%s`) |" % (decision["head_sha"][:12], decision["base_sha"][:12]))
    out.append("| Stage | %s |" % stage)
    out.append("| Your part | %s |" % decision["human_action"])
    out.append("")
    if run.get("tier_reasons"):
        why = "; ".join(sorted({"%s (%s)" % (r["reason"], r["path"]) for r in run["tier_reasons"][:5]}))
        out.append("Tier set by: %s." % why)
        out.append("")

    if decision["reasons"]:
        out.append("### Why this is not a pass")
        out.append("")
        for r in decision["reasons"]:
            out.append("- **%s** %s" % (r["verdict"], r["message"]))
        out.append("")

    out.append("### Evidence")
    out.append("")
    out.append("| Requirement | Result | Detail |")
    out.append("| --- | --- | --- |")
    for req in decision["requirements"]:
        out.append("| %s: %s | %s | %s |" % (req["kind"], req["name"], req["status"], _cell(req["detail"])))
    out.append("")
    if decision["not_applicable"]:
        out.append("Marked not applicable in the registry, so not checked:")
        out.append("")
        for na in decision["not_applicable"]:
            out.append("- %s `%s`: %s" % (na["kind"], na["name"], na["reason"]))
        out.append("")

    rows = sorted(
        F.active(findings),
        key=lambda f: (f["status"] not in F.OPEN, -F.rank(f["severity"]), -(f.get("confidence") or 0)),
    )
    shown = [f for f in rows if f["severity"] != "NIT"]
    if shown:
        out.append("### Findings")
        out.append("")
        out.append("| Id | Severity | Status | Where | Claim | Raised by | Ticket |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for f in shown[:60]:
            sev = f["severity"] + (" (always blocking)" if f.get("always_blocking") else "")
            who = ", ".join(sorted({s["reviewer"] for s in f["sources"]}))
            out.append("| `%s` | %s | %s | %s | %s | %s | %s |" % (
                f["id"], sev, f["status"], _cell(_loc(f)), _cell(f["claim"]), who, f.get("ticket") or ""))
        if len(shown) > 60:
            out.append("")
            out.append("%d more findings are in the run store." % (len(shown) - 60))
        out.append("")
    nits = sum(1 for f in rows if f["severity"] == "NIT")
    if nits:
        out.append("%d NIT finding(s) are kept in the run store and not shown." % nits)
        out.append("")
    human = [f for f in rows if f["decided_by_human"]]
    if human:
        out.append("### Human decisions on record")
        out.append("")
        for f in human:
            last = f["history"][-1]
            extra = " until %s" % f["waiver"]["expires"][:10] if f.get("waiver") else ""
            out.append("- `%s` %s%s by %s: %s" % (f["id"], f["status"], extra, last["by"], last["reason"]))
        out.append("")
    out.append("Policy `%s` from `%s`. Evaluated %s. The verdict comes from the policy engine, not from an agent." % (
        decision["policy_digest"][:12], run.get("control_dir", "?"), decision["evaluated_at"]))
    return "\n".join(out) + "\n"
