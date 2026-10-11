"""Risk tier from changed paths. Pure: no git, no files, no model."""
from __future__ import annotations

from typing import Any, Dict, List

from .util import glob_match


def classify(
    files: List[str],
    control_rules: List[Dict[str, Any]],
    repo_rules: List[Dict[str, Any]],
    default_tier: int,
) -> Dict[str, Any]:
    """Return the tier for a set of changed files.

    Per file: the control-side tier is the highest matching control rule, or the
    default when none matches. A repo rule can raise that and never lower it.
    The change takes the highest file tier.
    """
    tier = None
    reasons: List[Dict[str, Any]] = []
    unmapped: List[str] = []
    for path in sorted(set(files)):
        control = [r for r in control_rules if glob_match(r["glob"], path)]
        local = [r for r in repo_rules if glob_match(r["glob"], path)]
        if control:
            top = max(control, key=lambda r: r["tier"])
            file_tier, why, origin = top["tier"], top["reason"], "control"
        else:
            file_tier, why, origin = default_tier, "No rule covers this file", "default"
        for rule in local:
            if rule["tier"] > file_tier:
                file_tier, why, origin = rule["tier"], rule["reason"], "repo"
        if not control and not local:
            unmapped.append(path)
        reasons.append({"path": path, "tier": file_tier, "reason": why, "origin": origin})
        tier = file_tier if tier is None else max(tier, file_tier)
    if tier is None:
        tier = default_tier
    top_reasons = [r for r in reasons if r["tier"] == tier]
    return {"tier": tier, "reasons": top_reasons, "unmapped": unmapped, "per_file": reasons}
