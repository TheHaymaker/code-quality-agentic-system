"""Evaluate the policy for a run and record the result. The CLI and the engine
both come through here."""
from __future__ import annotations

from typing import Any, Dict

from . import policy, report, runs, store
from .util import now, write_json


def decide(run: store.Run, stage: str) -> Dict[str, Any]:
    data = runs.sync(run)
    pol = run.policy()
    decision = policy.evaluate(
        data, pol["floor"], pol["repo"], run.checks(), run.envelopes(), run.findings(),
        run.external(), run.approvals(), run.notes(), now(), stage,
    )
    with run.lock():
        write_json(run.dir / "verdict.json", decision)
        (run.dir / "report.md").write_text(report.render(decision, data, run.findings()), encoding="utf-8")
        run.event("verdict", {"verdict": decision["verdict"], "stage": stage, "head": decision["head_sha"]})
    return decision
