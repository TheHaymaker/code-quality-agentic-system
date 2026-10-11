"""Contract tests for the policy evaluator. No git, no files, no agents:
plain dicts in, a verdict out. These define what the system promises."""
import copy
import datetime
import unittest
from pathlib import Path

from cq import config, policy

# The tests use their own copy of the policy, so your edits to policy/floor.yml never break them.
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FLOOR = config.parse_floor((FIXTURES / "policy" / "floor.yml").read_text())
NOW = datetime.datetime(2026, 10, 10, 12, 0, tzinfo=datetime.timezone.utc)
BASE, C1, C2, C3 = "b" * 40, "1" * 40, "2" * 40, "3" * 40
ALL_CHECKS = ["lint", "types", "unit", "build", "integration", "mutation", "e2e", "rollback_test"]


def repo(**over):
    r = {"id": "demo", "sensitive": False, "checks": {c: ["true"] for c in ALL_CHECKS}, "not_applicable": {}}
    r.update(over)
    return r


def run(tier=1, head=C1, shas=None, **over):
    r = {
        "run_id": "t", "mode": "normal", "tier": tier, "head_sha": head, "base_sha": BASE,
        "shas": shas or [head], "dirty": False, "unmapped": [], "budget_exhausted": None,
        "implementer": {"family": "anthropic"}, "policy_digest": "d" * 64, "changed_lines": 1000,
    }
    r["lines_since"] = {sha: 10 for sha in r["shas"]}
    r.update(over)
    return r


def check(name, status="passed", head=C1, exit_code=0):
    return {"name": name, "status": status, "head_sha": head, "exit_code": exit_code, "detail": None}


def env(reviewer, head=C1, scope="full", reviewed_from=None, family="anthropic", model="claude-opus-5-5", shown=()):
    """A stored review. `shown` is the fixed findings the reviewer was asked to confirm and did not dispute."""
    return {
        "source": {"reviewer": reviewer, "kind": "agent", "family": family, "model": model},
        "revision": {"base_sha": BASE, "head_sha": head}, "scope": scope, "reviewed_from": reviewed_from,
        "fixed_presented": list(shown),
    }


def finding(fid="f-1", severity="MAJOR", status="verified", reviewer="structure", **over):
    f = {
        "id": fid, "severity": severity, "status": status, "always_blocking": None, "claim": "claim",
        "sources": [{"reviewer": reviewer, "kind": "agent", "head_sha": C1}], "merged_into": None, "ticket": None,
        "fix_commit": None, "waiver": None, "decided_by_human": False, "closed_by_source": False,
    }
    f.update(over)
    return f


def complete(tier, head=C1):
    """Every piece of evidence the tier requires, all good."""
    gate = FLOOR["gates"][str(tier)]
    checks = {c: check(c, head=head) for c in gate["checks"]}
    envelopes = []
    for r in gate["reviewers"]:
        if r == "adversary":
            envelopes.append(env(r, head, family="openai", model="gpt-x"))
        elif r == "adversary2":
            envelopes.append(env(r, head, family="google", model="gemini-x"))
        else:
            envelopes.append(env(r, head))
    external = {}
    if "duo" in gate["external"]:
        external["duo"] = {"state": "reviewed", "head_sha": head, "threads": []}
    if "sonar" in gate["external"]:
        external["sonar"] = {"state": "passed", "head_sha": head, "detail": "ok"}
    notes = {n: {"sha256": "p" * 64, "plan_sha256": "p" * 64, "outcome": "ok"} for n in gate["notes"]}
    approvals = []
    if "plan_approval" in gate["approvals"]:
        approvals.append({"kind": "plan_approval", "plan_sha256": "p" * 64})
    if "final_signoff" in gate["approvals"]:
        approvals.append({"kind": "final_signoff", "head_sha": head})
    return dict(checks=checks, envelopes=envelopes, findings={}, external=external, approvals=approvals, notes=notes)


def judge(r, ev, stage="final", rp=None):
    return policy.evaluate(r, FLOOR, rp or repo(), ev["checks"], ev["envelopes"], ev["findings"],
                           ev["external"], ev["approvals"], ev["notes"], NOW, stage)


def codes(decision):
    return {r["code"] for r in decision["reasons"]}


class Pass(unittest.TestCase):
    def test_complete_evidence_passes_at_every_tier(self):
        for tier in range(4):
            d = judge(run(tier), complete(tier))
            self.assertEqual(d["verdict"], "PASS", (tier, d["reasons"]))
            self.assertTrue(d["ready_to_merge"])

    def test_same_input_same_decision(self):
        ev = complete(2)
        ev["findings"] = {"f-1": finding()}
        self.assertEqual(judge(run(2), copy.deepcopy(ev)), judge(run(2), copy.deepcopy(ev)))

    def test_no_evidence_is_never_a_pass(self):
        empty = dict(checks={}, envelopes=[], findings={}, external={}, approvals=[], notes={})
        for tier in range(4):
            self.assertEqual(judge(run(tier), empty)["verdict"], "INCONCLUSIVE")


class Checks(unittest.TestCase):
    def test_failed_check_fails(self):
        ev = complete(1)
        ev["checks"]["unit"] = check("unit", "failed", exit_code=1)
        self.assertEqual(judge(run(1), ev)["verdict"], "FAIL")

    def test_missing_check_is_inconclusive(self):
        ev = complete(1)
        del ev["checks"]["lint"]
        d = judge(run(1), ev)
        self.assertEqual(d["verdict"], "INCONCLUSIVE")
        self.assertIn("check_missing", codes(d))

    def test_check_from_older_commit_is_stale(self):
        ev = complete(1, head=C1)
        d = judge(run(1, head=C2, shas=[C1, C2]), ev)
        self.assertIn("check_stale", codes(d))
        self.assertEqual(d["verdict"], "INCONCLUSIVE")

    def test_check_that_could_not_run_is_not_a_failure_and_not_a_pass(self):
        ev = complete(1)
        ev["checks"]["build"] = dict(check("build", "error", exit_code=None), detail="timed out")
        d = judge(run(1), ev)
        self.assertEqual(d["verdict"], "INCONCLUSIVE")
        self.assertIn("check_error", codes(d))

    def test_required_check_without_a_command_is_inconclusive(self):
        rp = repo()
        del rp["checks"]["build"]
        d = judge(run(1), complete(1), rp=rp)
        self.assertIn("check_unregistered", codes(d))

    def test_not_applicable_is_skipped_and_listed(self):
        ev = complete(2)
        del ev["checks"]["mutation"]
        d = judge(run(2), ev, rp=repo(not_applicable={"mutation": "not built yet"}))
        self.assertEqual(d["verdict"], "PASS")
        self.assertEqual(d["not_applicable"], [{"kind": "check", "name": "mutation", "reason": "not built yet"}])

    def test_uncommitted_changes_are_inconclusive(self):
        d = judge(run(1, dirty=True), complete(1))
        self.assertEqual(d["verdict"], "INCONCLUSIVE")
        self.assertIn("worktree_dirty", codes(d))


class Reviews(unittest.TestCase):
    def test_missing_reviewer_is_inconclusive(self):
        ev = complete(1)
        ev["envelopes"] = [e for e in ev["envelopes"] if e["source"]["reviewer"] != "structure"]
        self.assertIn("review_missing", codes(judge(run(1), ev)))

    def test_review_of_older_commit_is_stale(self):
        ev = complete(1, head=C2)
        ev["envelopes"] = complete(1, head=C1)["envelopes"]
        d = judge(run(1, head=C2, shas=[C1, C2]), ev)
        self.assertIn("review_stale", codes(d))

    def test_fix_diff_review_carries_earlier_reviews(self):
        ev = complete(1, head=C2)
        ev["envelopes"] = complete(1, head=C1)["envelopes"] + [env("structure", C2, "fix_diff", C1)]
        self.assertEqual(judge(run(1, head=C2, shas=[C1, C2]), ev)["verdict"], "PASS")

    def test_fix_diff_must_start_at_or_before_the_earlier_review(self):
        ev = complete(1, head=C3)
        ev["envelopes"] = complete(1, head=C1)["envelopes"] + [env("structure", C3, "fix_diff", C2)]
        d = judge(run(1, head=C3, shas=[C1, C2, C3]), ev)
        self.assertIn("review_stale", codes(d))

    def test_reviewer_must_recheck_the_fix_for_its_own_finding(self):
        ev = complete(1, head=C2)
        ev["envelopes"] = complete(1, head=C1)["envelopes"] + [env("structure", C2, "fix_diff", C1)]
        ev["findings"] = {"f-1": finding(status="fixed", reviewer="simplicity", fix_commit=C2)}
        d = judge(run(1, head=C2, shas=[C1, C2]), ev)
        self.assertIn("fix_not_rereviewed", codes(d))
        ev["envelopes"].append(env("simplicity", C2, "fix_diff", C1, shown=["f-1"]))
        self.assertEqual(judge(run(1, head=C2, shas=[C1, C2]), ev)["verdict"], "PASS")

    def test_fix_diff_reviews_chain_across_rounds(self):
        ev = complete(1, head=C3)
        ev["envelopes"] = complete(1, head=C1)["envelopes"] + [env("structure", C2, "fix_diff", C1), env("simplicity", C3, "fix_diff", C2)]
        self.assertEqual(judge(run(1, head=C3, shas=[C1, C2, C3]), ev)["verdict"], "PASS")
        ev["envelopes"] = complete(1, head=C1)["envelopes"] + [env("simplicity", C3, "fix_diff", C2)]   # nobody looked at C1..C2
        self.assertIn("review_stale", codes(judge(run(1, head=C3, shas=[C1, C2, C3]), ev)))

    def test_a_large_change_after_a_review_cannot_be_carried(self):
        # Panel on a trivial first commit, the real change in the second, one empty fix-diff review.
        ev = complete(1, head=C2)
        ev["envelopes"] = complete(1, head=C1)["envelopes"] + [env("simplicity", C2, "fix_diff", C1)]
        r = run(1, head=C2, shas=[C1, C2], changed_lines=400, lines_since={C1: 380, C2: 0})
        d = judge(r, ev)
        self.assertEqual(d["verdict"], "INCONCLUSIVE")
        self.assertEqual(codes(d), {"review_too_old"})
        r["lines_since"][C1] = 151                                    # over the absolute limit, under the share
        r["changed_lines"] = 5000
        self.assertEqual(codes(judge(r, ev)), {"review_too_old"})
        r["lines_since"][C1] = 150
        self.assertEqual(judge(r, ev)["verdict"], "PASS")
        del r["lines_since"][C1]                                      # unknown size is not small
        self.assertEqual(codes(judge(r, ev)), {"review_too_old"})

    def test_security_reviews_every_fix_itself(self):
        ev = complete(2, head=C2)
        ev["envelopes"] = complete(2, head=C1)["envelopes"] + [env("structure", C2, "fix_diff", C1)]
        d = judge(run(2, head=C2, shas=[C1, C2]), ev)
        stale = [r["message"] for r in d["reasons"] if r["code"] == "review_stale"]
        self.assertEqual(len(stale), 1)
        self.assertIn("'security'", stale[0])
        ev["envelopes"].append(env("security", C2, "fix_diff", C1))
        self.assertEqual(judge(run(2, head=C2, shas=[C1, C2]), ev)["verdict"], "PASS")

    def test_adversary_family_comparison_ignores_case(self):
        ev = complete(2)
        for e in ev["envelopes"]:
            if e["source"]["reviewer"] == "adversary":
                e["source"]["family"] = "Anthropic"
        self.assertIn("adversary_not_independent", codes(judge(run(2), ev)))

    def test_review_from_rewritten_history_does_not_count(self):
        ev = complete(1, head=C2)
        ev["envelopes"] = complete(1, head=C1)["envelopes"]
        d = judge(run(1, head=C2, shas=[C2]), ev)
        self.assertIn("review_missing", codes(d))

    def test_adversary_from_implementer_family_is_not_evidence(self):
        ev = complete(2)
        for e in ev["envelopes"]:
            if e["source"]["reviewer"] == "adversary":
                e["source"]["family"] = "anthropic"
        d = judge(run(2), ev)
        self.assertEqual(d["verdict"], "INCONCLUSIVE")
        self.assertIn("adversary_not_independent", codes(d))

    def test_two_adversaries_need_two_models(self):
        ev = complete(3)
        for e in ev["envelopes"]:
            if e["source"]["reviewer"] == "adversary2":
                e["source"]["model"] = "gpt-x"
        self.assertIn("adversary2_same_model", codes(judge(run(3), ev)))


class Findings(unittest.TestCase):
    def _with(self, tier, **f):
        ev = complete(tier)
        ev["findings"] = {"f-1": finding(**f)}
        return judge(run(tier), ev)

    def test_verified_blocker_fails_at_every_tier(self):
        for tier in range(4):
            self.assertEqual(self._with(tier, severity="BLOCKER")["verdict"], "FAIL")

    def test_unverified_blocker_is_inconclusive(self):
        self.assertEqual(self._with(1, severity="BLOCKER", status="unverified")["verdict"], "INCONCLUSIVE")

    def test_contested_blocker_goes_to_a_human(self):
        self.assertEqual(self._with(1, severity="BLOCKER", status="contested")["verdict"], "BLOCKED")

    def test_always_blocking_blocks_whatever_its_severity(self):
        for tier in range(4):
            d = self._with(tier, severity="NIT", always_blocking="secret")
            self.assertEqual(d["verdict"], "FAIL")

    def test_always_blocking_cannot_be_filed(self):
        d = self._with(0, severity="MINOR", always_blocking="injection", status="filed", ticket="X-1")
        self.assertEqual(d["verdict"], "FAIL")

    def test_blocker_cannot_be_filed(self):
        self.assertEqual(self._with(0, severity="BLOCKER", status="filed", ticket="X-1")["verdict"], "FAIL")

    def test_major_may_be_filed_at_tier_1_and_not_at_tier_2(self):
        self.assertEqual(self._with(1, status="filed", ticket="X-1")["verdict"], "PASS")
        self.assertEqual(self._with(2, status="filed", ticket="X-1")["verdict"], "FAIL")

    def test_filed_without_ticket_is_inconclusive(self):
        self.assertEqual(self._with(1, status="filed")["verdict"], "INCONCLUSIVE")

    def test_open_minor_and_nit_do_not_gate(self):
        self.assertEqual(self._with(3, severity="MINOR")["verdict"], "PASS")
        self.assertEqual(self._with(3, severity="NIT", status="unverified")["verdict"], "PASS")

    def test_agent_rejection_of_gating_finding_needs_a_human(self):
        self.assertEqual(self._with(1, status="rejected")["verdict"], "BLOCKED")
        self.assertEqual(self._with(1, status="rejected", decided_by_human=True)["verdict"], "PASS")

    def test_waiver_holds_until_it_expires(self):
        live = {"expires": "2026-11-01T00:00:00Z", "by": "h", "reason": "r"}
        dead = {"expires": "2026-10-01T00:00:00Z", "by": "h", "reason": "r"}
        self.assertEqual(self._with(2, status="waived", waiver=live, decided_by_human=True)["verdict"], "PASS")
        d = self._with(2, status="waived", waiver=dead, decided_by_human=True)
        self.assertEqual(d["verdict"], "FAIL")
        self.assertIn("waiver_expired", codes(d))

    def test_untriaged_external_finding_is_inconclusive(self):
        self.assertEqual(self._with(1, severity="UNTRIAGED", status="unverified")["verdict"], "INCONCLUSIVE")

    def test_fix_by_unknown_commit_is_inconclusive(self):
        self.assertIn("fix_unknown", codes(self._with(1, status="fixed", fix_commit=C3, reviewer="duo")))

    def test_marking_fixed_is_a_claim_until_the_raiser_has_seen_the_fix(self):
        # "Fixed" by the very commit the finding was reported on: nothing changed.
        d = self._with(1, severity="BLOCKER", status="fixed", fix_commit=C1)
        self.assertEqual(d["verdict"], "INCONCLUSIVE")
        self.assertIn("fix_unknown", codes(d))
        # Fixed by a later commit, and the raiser's only review predates it.
        ev = complete(1, head=C2)
        ev["envelopes"] = [e for e in ev["envelopes"] if e["source"]["reviewer"] != "structure"] + [env("structure", C1)]
        ev["envelopes"].append(env("simplicity", C2, "fix_diff", C1))
        ev["findings"] = {"f-1": finding(severity="BLOCKER", status="fixed", fix_commit=C2)}
        r = run(1, head=C2, shas=[C1, C2])
        self.assertEqual(codes(judge(r, ev)), {"fix_not_rereviewed"})
        # A fix-diff review that starts at the fix commit did not look at the fix.
        ev["envelopes"].append(env("structure", C2, "fix_diff", C2, shown=["f-1"]))
        self.assertEqual(codes(judge(r, ev)), {"fix_not_rereviewed"})
        # A review of the fix commit made BEFORE the finding was marked fixed was never shown the claim.
        ev["envelopes"].append(env("structure", C2, "full"))
        self.assertEqual(codes(judge(r, ev)), {"fix_not_rereviewed"})
        ev["envelopes"][-1] = env("structure", C2, "full", shown=["f-1"])
        self.assertEqual(judge(r, ev)["verdict"], "PASS")

    def test_raiser_keeps_watching_after_its_fix_so_a_revert_cannot_slip_through(self):
        ev = complete(1, head=C3)
        ev["envelopes"] = complete(1, head=C1)["envelopes"] + [
            env("structure", C2, "fix_diff", C1, shown=["f-1"]),      # the raiser confirms the fix at C2
            env("simplicity", C3, "fix_diff", C2),                    # C3 reverts it; only another lens looks
        ]
        ev["findings"] = {"f-1": finding(severity="BLOCKER", status="fixed", fix_commit=C2)}
        r = run(1, head=C3, shas=[C1, C2, C3])
        d = judge(r, ev)
        self.assertEqual(codes(d), {"fix_not_rereviewed"})
        self.assertIn("has not reviewed the commits made since", d["reasons"][0]["message"])
        ev["envelopes"].append(env("structure", C3, "fix_diff", C2))
        self.assertEqual(judge(r, ev)["verdict"], "PASS")

    def test_fixed_duo_finding_needs_a_local_reviewer_to_confirm_it(self):
        ev = complete(1, head=C2)
        duo = finding("duo-1", "BLOCKER", "fixed", "duo", fix_commit=C2, sources=[{"reviewer": "duo", "kind": "external", "head_sha": C1}])
        ev["findings"] = {"duo-1": duo}
        r = run(1, head=C2, shas=[C1, C2])
        self.assertEqual(codes(judge(r, ev)), {"fix_not_rereviewed"})
        ev["envelopes"].append(env("security", C2, "fix_diff", C1, shown=["duo-1"]))
        self.assertEqual(judge(r, ev)["verdict"], "PASS")

    def test_finding_closed_by_its_source_needs_no_confirmation(self):
        gone = finding("sonar-1", "BLOCKER", "fixed", "sonar", fix_commit=C1, closed_by_source=True,
                       sources=[{"reviewer": "sonar", "kind": "external", "head_sha": C1}])
        ev = complete(1)
        ev["findings"] = {"sonar-1": gone}
        self.assertEqual(judge(run(1), ev)["verdict"], "PASS")           # same commit: a re-analysis dropped it

    def test_any_contested_finding_goes_to_a_human(self):
        for severity in ("NIT", "MINOR", "MAJOR", "BLOCKER", "UNTRIAGED"):
            self.assertEqual(self._with(1, severity=severity, status="contested")["verdict"], "BLOCKED", severity)

    def test_merged_duplicates_are_ignored(self):
        self.assertEqual(self._with(1, severity="BLOCKER", merged_into="f-2")["verdict"], "PASS")


class External(unittest.TestCase):
    def test_local_stage_does_not_ask_for_duo_or_sonar(self):
        ev = complete(1)
        ev["external"] = {}
        self.assertEqual(judge(run(1), ev, stage="local")["verdict"], "PASS")
        self.assertFalse(judge(run(1), ev, stage="local")["ready_to_merge"])
        self.assertEqual(judge(run(1), ev)["verdict"], "INCONCLUSIVE")

    def test_sonar_states_are_kept_apart(self):
        expect = {"passed": "PASS", "failed": "FAIL", "missing": "INCONCLUSIVE", "stale": "INCONCLUSIVE", "error": "INCONCLUSIVE"}
        for state, verdict in expect.items():
            ev = complete(1)
            ev["external"]["sonar"]["state"] = state
            self.assertEqual(judge(run(1), ev)["verdict"], verdict, state)

    def test_external_evidence_for_older_commit_is_stale(self):
        ev = complete(1)
        ev["external"]["sonar"]["head_sha"] = C2
        ev["external"]["duo"]["head_sha"] = C2
        d = judge(run(1), ev)
        self.assertTrue({"sonar_stale", "duo_stale"} <= codes(d))

    def test_duo_not_finished_is_inconclusive(self):
        for state in ("pending", "not_requested", "request_failed", "error"):
            ev = complete(1)
            ev["external"]["duo"]["state"] = state
            self.assertEqual(judge(run(1), ev)["verdict"], "INCONCLUSIVE", state)

    def test_open_duo_thread_fails_until_answered(self):
        ev = complete(1, head=C2)
        r = run(1, head=C2, shas=[C1, C2])
        ev["external"]["duo"]["threads"] = [{"discussion_id": "d1", "finding_id": "duo-d1", "resolved": False}]
        ev["findings"] = {"duo-d1": finding("duo-d1", "MINOR", "fixed", "duo", fix_commit=C2)}
        self.assertEqual(codes(judge(r, ev)), {"duo_threads_open"})
        ev["external"]["duo"]["threads"][0]["resolved"] = True
        self.assertEqual(judge(r, ev)["verdict"], "PASS")

    def test_contested_duo_thread_waits_for_the_human(self):
        for severity in ("MAJOR", "NIT"):      # triaging a thread down to NIT does not make it disappear
            ev = complete(1)
            ev["external"]["duo"]["threads"] = [{"discussion_id": "d1", "finding_id": "duo-d1", "resolved": False}]
            ev["findings"] = {"duo-d1": finding("duo-d1", severity, "contested", "duo")}
            d = judge(run(1), ev)
            self.assertEqual((d["verdict"], d["ready_to_merge"]), ("BLOCKED", False), severity)


class Humans(unittest.TestCase):
    def test_tier_3_needs_plan_approval_and_signoff(self):
        ev = complete(3)
        ev["approvals"] = []
        d = judge(run(3), ev)
        self.assertEqual(d["verdict"], "BLOCKED")
        self.assertTrue({"plan_approval_missing", "final_signoff_missing"} <= codes(d))

    def test_signoff_covers_one_commit(self):
        ev = complete(3, head=C2)
        ev["approvals"] = [{"kind": "plan_approval", "plan_sha256": "p" * 64}, {"kind": "final_signoff", "head_sha": C1}]
        self.assertIn("final_signoff_missing", codes(judge(run(3, head=C2, shas=[C2]), ev)))

    def test_plan_approval_covers_one_plan_text(self):
        ev = complete(3)
        ev["notes"]["plan"] = {"sha256": "q" * 64}
        self.assertIn("plan_approval_missing", codes(judge(run(3), ev)))

    def test_plan_review_is_about_one_plan_and_has_an_outcome(self):
        ev = complete(2)
        ev["notes"]["plan_review"]["plan_sha256"] = "o" * 64
        self.assertEqual(codes(judge(run(2), ev)), {"plan_review_stale"})
        ev = complete(2)
        ev["notes"]["plan_review"]["outcome"] = "needs_changes"
        d = judge(run(2), ev)
        self.assertEqual((d["verdict"], codes(d)), ("FAIL", {"plan_review_negative"}))

    def test_plan_approval_is_needed_before_the_merge_request(self):
        ev = complete(3)
        ev["approvals"] = []
        d = judge(run(3), ev, stage="local")
        self.assertEqual(codes(d), {"plan_approval_missing"})

    def test_spent_budget_goes_to_a_human(self):
        self.assertEqual(judge(run(1, budget_exhausted="local"), complete(1))["verdict"], "BLOCKED")

    def test_unmapped_path_in_sensitive_repo_goes_to_a_human(self):
        d = judge(run(1, unmapped=["src/new.py"]), complete(1), rp=repo(sensitive=True))
        self.assertEqual(d["verdict"], "BLOCKED")
        self.assertEqual(judge(run(1, unmapped=["src/new.py"]), complete(1))["verdict"], "PASS")


class Precedence(unittest.TestCase):
    def test_fail_beats_inconclusive_beats_blocked(self):
        ev = complete(3)
        ev["approvals"] = []
        self.assertEqual(judge(run(3), ev)["verdict"], "BLOCKED")
        del ev["checks"]["lint"]
        self.assertEqual(judge(run(3), ev)["verdict"], "INCONCLUSIVE")
        ev["checks"]["unit"] = check("unit", "failed", exit_code=2)
        d = judge(run(3), ev)
        self.assertEqual(d["verdict"], "FAIL")
        self.assertEqual(d["reasons"][0]["verdict"], "FAIL")


class Tracer(unittest.TestCase):
    def test_tracer_uses_tier_0_gates_and_never_merges(self):
        d = judge(run(3, mode="tracer"), complete(0))
        self.assertEqual(d["verdict"], "PASS")
        self.assertEqual(d["gate_tier"], 0)
        self.assertFalse(d["ready_to_merge"])

    def test_tracer_still_stops_on_always_blocking(self):
        ev = complete(0)
        ev["findings"] = {"f-1": finding(severity="MINOR", always_blocking="secret")}
        self.assertEqual(judge(run(3, mode="tracer"), ev)["verdict"], "FAIL")


if __name__ == "__main__":
    unittest.main()
