"""Unit tests: path globs, config validation, tier classification, findings rules."""
import datetime
import unittest
from pathlib import Path

from cq import config, findings as F, tiers
from cq.util import CqError, glob_match, parse_time

ROOT = Path(__file__).resolve().parents[1]
# The tests use their own copy of the policy, so your edits to policy/floor.yml never break them.
FLOOR_TEXT = (Path(__file__).resolve().parent / "fixtures" / "policy" / "floor.yml").read_text()
FLOOR = config.parse_floor(FLOOR_TEXT)
OLD, HEAD, NEXT = "0" * 40, "1" * 40, "2" * 40


class Globs(unittest.TestCase):
    def test_double_star_spans_directories(self):
        self.assertTrue(glob_match("**/migrations/**", "db/migrations/001.sql"))
        self.assertTrue(glob_match("**/migrations/**", "migrations/001.sql"))
        self.assertTrue(glob_match("src/checkout/**", "src/checkout/a/b.ts"))
        self.assertFalse(glob_match("src/checkout/**", "src/checkout2/a.ts"))
        self.assertTrue(glob_match("**/*.md", "README.md"))
        self.assertTrue(glob_match("**/*.md", "a/b/c.md"))

    def test_single_star_stays_in_one_segment(self):
        self.assertTrue(glob_match("src/*.py", "src/a.py"))
        self.assertFalse(glob_match("src/*.py", "src/sub/a.py"))
        self.assertFalse(glob_match("*.md", "docs/a.md"))

    def test_regex_characters_are_literal(self):
        self.assertTrue(glob_match(".gitlab-ci.yml", ".gitlab-ci.yml"))
        self.assertFalse(glob_match(".gitlab-ci.yml", "xgitlab-ci.yml"))
        self.assertTrue(glob_match("a+b/(c).txt", "a+b/(c).txt"))


class Times(unittest.TestCase):
    def test_shapes_from_gitlab_sonar_and_git(self):
        utc = datetime.timezone.utc
        self.assertEqual(parse_time("2026-10-10T12:00:00Z"), datetime.datetime(2026, 10, 10, 12, tzinfo=utc))
        self.assertEqual(parse_time("2026-10-10T12:00:00.123Z").microsecond, 123000)
        self.assertEqual(parse_time("2026-10-10T14:00:00+0200"), datetime.datetime(2026, 10, 10, 12, tzinfo=utc))
        self.assertEqual(parse_time("2026-10-10T07:00:00-05:00"), datetime.datetime(2026, 10, 10, 12, tzinfo=utc))


class Config(unittest.TestCase):
    def test_shipped_config_is_valid(self):
        config.parse_floor((ROOT / "policy" / "floor.yml").read_text())
        config.parse_models((ROOT / "policy" / "models.yml").read_text())
        config.parse_registry((ROOT / "registry.yml").read_text())
        self.assertEqual(config.parse_criticality((ROOT / "templates" / "cq" / "criticality.yml").read_text()), [])

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(CqError):
            config.parse_floor(FLOOR_TEXT + "\nallow_autonomous_merge: true\n")
        with self.assertRaises(CqError):
            config.parse_registry("schema_version: 1\nrepositories: []\nextra: 1\n")
        with self.assertRaises(CqError):
            config.parse_criticality("schema_version: 1\nrules: []\ndefault: 0\n")

    def test_higher_tier_may_not_require_less(self):
        weaker = FLOOR_TEXT.replace(
            "reviewers: [simplicity, structure, standards_spec, tests, security, adversary]\n",
            "reviewers: [simplicity]\n",
        )
        self.assertNotEqual(weaker, FLOOR_TEXT)
        with self.assertRaises(CqError) as ctx:
            config.parse_floor(weaker)
        self.assertIn("tier 2 drops reviewers", str(ctx.exception))

    def test_duplicate_repository_ids_are_rejected(self):
        entry = "  - {id: a, path: /x, default_branch: main, gitlab_project: g/a, checks: {}}\n"
        with self.assertRaises(CqError):
            config.parse_registry("schema_version: 1\nrepositories:\n" + entry + entry)

    def test_shell_string_check_is_rejected(self):
        with self.assertRaises(CqError):
            config.parse_registry(
                "schema_version: 1\nrepositories:\n"
                "  - {id: a, path: /x, default_branch: main, gitlab_project: g/a, checks: {lint: 'npm run lint'}}\n"
            )

    def test_human_approval_cannot_be_marked_not_applicable(self):
        with self.assertRaises(CqError):
            config.parse_registry(
                "schema_version: 1\ndefaults:\n  not_applicable: {final_signoff: 'trust me'}\nrepositories: []\n"
            )

    def test_registry_defaults_are_merged(self):
        reg = config.parse_registry(
            "schema_version: 1\ndefaults:\n  not_applicable: {mutation: later}\nrepositories:\n"
            "  - {id: a, path: /x, default_branch: main, gitlab_project: g/a, checks: {}, not_applicable: {e2e: none}}\n"
        )
        repo = config.repo_entry(reg, "a")
        self.assertEqual(repo["not_applicable"], {"mutation": "later", "e2e": "none"})
        self.assertEqual(repo["duo"]["username"], "GitLabDuo")
        with self.assertRaises(CqError):
            config.repo_entry(reg, "missing")


class Tiers(unittest.TestCase):
    def classify(self, files, repo_rules=(), extra_control=()):
        return tiers.classify(files, FLOOR["paths"] + list(extra_control), list(repo_rules), FLOOR["default_tier"])

    def test_uncovered_file_takes_the_default(self):
        r = self.classify(["src/app.py"])
        self.assertEqual((r["tier"], r["unmapped"]), (1, ["src/app.py"]))

    def test_highest_file_wins(self):
        self.assertEqual(self.classify(["README.md", "db/migrations/1.sql", "src/app.py"])["tier"], 3)

    def test_docs_alone_are_tier_0(self):
        self.assertEqual(self.classify(["docs/guide.md", "README.md"])["tier"], 0)

    def test_agent_instruction_files_are_tier_3(self):
        self.assertEqual(self.classify(["CLAUDE.md"])["tier"], 3)
        self.assertEqual(self.classify(["services/api/AGENTS.md"])["tier"], 3)

    def test_policy_files_are_tier_3_even_when_markdown(self):
        self.assertEqual(self.classify([".cq/standards/coding-standards.md"])["tier"], 3)
        self.assertEqual(self.classify([".claude/skills/cq-run/SKILL.md"])["tier"], 3)
        self.assertEqual(self.classify([".gitlab-ci.yml"])["tier"], 3)

    def test_repo_rule_can_raise(self):
        rule = {"glob": "src/pricing/**", "tier": 3, "reason": "pricing"}
        r = self.classify(["src/pricing/calc.py"], repo_rules=[rule])
        self.assertEqual((r["tier"], r["unmapped"], r["reasons"][0]["origin"]), (3, [], "repo"))

    def test_repo_rule_cannot_lower(self):
        # A project tries to mark its whole source tree, and its migrations, as tier 0.
        rules = [{"glob": "**", "tier": 0, "reason": "trust me"}]
        self.assertEqual(self.classify(["src/app.py"], repo_rules=rules)["tier"], 1)
        self.assertEqual(self.classify(["db/migrations/1.sql"], repo_rules=rules)["tier"], 3)

    def test_tier_0_can_be_granted_from_the_control_side(self):
        control = [{"glob": "tools/**", "tier": 0, "reason": "internal"}]
        self.assertEqual(self.classify(["tools/x.sh"], extra_control=control)["tier"], 0)

    def test_no_files_takes_the_default(self):
        self.assertEqual(self.classify([])["tier"], 1)


def submission(**over):
    item = {
        "category": "correctness", "severity": "MAJOR", "claim": "Off by one in pager",
        "location": {"path": "src/app.py", "start_line": 10}, "scenario": "page=0 returns the last page",
        "proposed_fix": "clamp to 1",
    }
    item.update(over)
    return {"scope": "full", "files_reviewed": ["src/app.py"], "findings": [item]}


# The run saw OLD, then HEAD (where reviews happen), then NEXT (a fix). A fix round opened at HEAD.
RUN = {"head_sha": HEAD, "shas": [OLD, HEAD, NEXT], "changed_files": ["src/app.py"], "base_sha": "b" * 40,
       "fix_round_start": HEAD}
SRC = {"kind": "agent", "model": "m", "family": "anthropic"}


def stored(reviewer="structure", sub=None, findings=None):
    findings = {} if findings is None else findings
    env, ids = F.ingest(findings, reviewer, SRC, HEAD, "b" * 40, sub or submission())
    return findings, ids[0] if ids else None, env


class Submissions(unittest.TestCase):
    def test_valid_submission_is_accepted(self):
        errors, warnings = F.check_submission(RUN, [], "structure", HEAD, submission())
        self.assertEqual((errors, warnings), ([], []))

    def test_unknown_field_is_refused(self):
        sub = submission()
        sub["verdict"] = "PASS"
        errors, _ = F.check_submission(RUN, [], "structure", HEAD, sub)
        self.assertTrue(errors)

    def test_review_of_another_commit_is_refused(self):
        errors, _ = F.check_submission(RUN, [], "structure", OLD, submission())
        self.assertIn("not the run's head", errors[0])

    def test_unread_changed_file_is_refused(self):
        run = dict(RUN, changed_files=["src/app.py", "src/db.py"])
        errors, _ = F.check_submission(run, [], "structure", HEAD, submission())
        self.assertIn("src/db.py", errors[0])
        sub = submission()
        sub["files_skipped"] = [{"path": "src/db.py", "reason": "generated"}]
        self.assertEqual(F.check_submission(run, [], "structure", HEAD, sub)[0], [])

    def test_second_submission_for_same_commit_is_refused(self):
        _, _, env = stored()
        errors, _ = F.check_submission(RUN, [env], "structure", HEAD, submission())
        self.assertIn("already submitted", errors[0])

    def test_fix_diff_needs_a_known_start(self):
        sub = dict(submission(), scope="fix_diff")
        self.assertIn("reviewed_from", F.check_submission(RUN, [], "structure", HEAD, sub, [])[0][0])
        sub["reviewed_from"] = "9" * 40
        self.assertIn("not a commit this run has seen", F.check_submission(RUN, [], "structure", HEAD, sub, [])[0][0])
        sub["reviewed_from"] = OLD
        self.assertEqual(F.check_submission(RUN, [], "structure", HEAD, sub, ["src/app.py"])[0], [])

    def test_unknown_reviewer_is_refused(self):
        self.assertTrue(F.check_submission(RUN, [], "aggregator", HEAD, submission())[0])


class EvidenceBar(unittest.TestCase):
    def test_complete_finding_keeps_its_severity(self):
        findings, fid, _ = stored()
        self.assertEqual(findings[fid]["severity"], "MAJOR")

    def test_finding_without_scenario_fix_or_line_becomes_nit(self):
        for gap in ({"scenario": None}, {"proposed_fix": ""}, {"location": {"path": "src/app.py"}}, {"location": None}):
            item = dict(submission()["findings"][0], **gap)
            item = {k: v for k, v in item.items() if not (k == "location" and v is None)}
            sub = {"scope": "full", "files_reviewed": ["src/app.py"], "findings": [item]}
            findings, fid, _ = stored(sub=sub)
            self.assertEqual((findings[fid]["severity"], findings[fid]["claimed_severity"]), ("NIT", "MAJOR"), gap)
            self.assertTrue(F.check_submission(RUN, [], "structure", HEAD, sub)[1])

    def test_always_blocking_survives_demotion(self):
        findings, fid, _ = stored(sub=submission(scenario=None, always_blocking="secret"))
        self.assertEqual(findings[fid]["severity"], "NIT")
        self.assertTrue(F.is_gating(findings[fid]))

    def test_final_check_veto_goes_to_a_human(self):
        findings, fid, _ = stored(reviewer="final_check")
        self.assertEqual(findings[fid]["status"], "contested")

    def test_same_finding_reported_again_after_fix_reopens(self):
        findings, fid, _ = stored()
        F.set_status(findings, RUN, 1, fid, "fixed", "fixer", "done", commit=NEXT)
        stored(findings=findings)
        self.assertEqual((findings[fid]["status"], findings[fid]["fix_commit"]), ("verified", None))

    def test_new_report_can_raise_severity_and_reopen_but_never_lower(self):
        findings, fid, _ = stored(sub=submission(severity="MINOR"))
        F.set_status(findings, RUN, 1, fid, "rejected", "aggregator", "style only")
        stored(findings=findings, sub=submission(severity="BLOCKER", always_blocking="injection"))
        rec = findings[fid]
        self.assertEqual((rec["severity"], rec["status"], rec["always_blocking"]), ("BLOCKER", "unverified", "injection"))
        stored(findings=findings, sub=submission(severity="NIT"))
        self.assertEqual(findings[fid]["severity"], "BLOCKER")

    def test_finding_demoted_for_missing_evidence_recovers_when_resubmitted_complete(self):
        findings, fid, _ = stored(sub=submission(scenario=None))
        self.assertEqual(findings[fid]["severity"], "NIT")
        stored(findings=findings)
        self.assertEqual((findings[fid]["severity"], findings[fid]["scenario"]), ("MAJOR", "page=0 returns the last page"))

    def test_worse_report_after_a_human_decision_goes_back_to_the_human(self):
        findings, fid, _ = stored(sub=submission(severity="MINOR"))
        F.decide(findings, FLOOR, 1, fid, "waive", "human:b", "fine", None, None, datetime.datetime.now(datetime.timezone.utc))
        stored(findings=findings, sub=submission(severity="BLOCKER"))
        rec = findings[fid]
        self.assertEqual((rec["status"], rec["decided_by_human"], rec["waiver"]), ("contested", False, None))


class AgentLimits(unittest.TestCase):
    def setUp(self):
        self.findings, self.fid, _ = stored()

    def set(self, status, tier=1, **kw):
        return F.set_status(self.findings, RUN, tier, self.fid, status, "aggregator", kw.pop("reason", "because"), **kw)

    def test_agent_cannot_reject_a_gating_finding(self):
        with self.assertRaises(CqError):
            self.set("rejected")

    def test_agent_can_reject_a_minor(self):
        findings, fid, _ = stored(sub=submission(severity="MINOR"))
        F.set_status(findings, RUN, 1, fid, "rejected", "aggregator", "style only")
        self.assertEqual(findings[fid]["status"], "rejected")

    def test_major_can_be_filed_at_tier_1_only(self):
        with self.assertRaises(CqError):
            self.set("filed", tier=2, ticket="X-1")
        with self.assertRaises(CqError):
            self.set("filed", tier=1)
        self.assertEqual(self.set("filed", tier=1, ticket="X-1")["ticket"], "X-1")

    def test_blocker_and_always_blocking_cannot_be_filed(self):
        for extra in ({"severity": "BLOCKER"}, {"severity": "MINOR", "always_blocking": "injection"}):
            findings, fid, _ = stored(sub=submission(**extra))
            with self.assertRaises(CqError):
                F.set_status(findings, RUN, 0, fid, "filed", "aggregator", "later", ticket="X-1")

    def test_verified_needs_evidence_and_fixed_needs_a_known_commit(self):
        with self.assertRaises(CqError):
            self.set("verified")
        self.set("verified", evidence="test_pager fails with page=0")
        with self.assertRaises(CqError):
            self.set("fixed", commit="9" * 40)
        self.assertEqual(self.set("fixed", commit=NEXT)["fix_commit"], NEXT)

    def test_fixed_needs_a_commit_after_the_report_and_inside_a_fix_round(self):
        with self.assertRaises(CqError) as ctx:
            self.set("fixed", commit=HEAD)                      # the commit it was reported on
        self.assertIn("after the commit the finding was reported on", str(ctx.exception))
        with self.assertRaises(CqError):
            self.set("fixed", commit=OLD)
        with self.assertRaises(CqError) as ctx:
            F.set_status(self.findings, dict(RUN, fix_round_start=None), 1, self.fid, "fixed", "fixer", "done", commit=NEXT)
        self.assertIn("no fix round is open", str(ctx.exception))
        with self.assertRaises(CqError) as ctx:
            F.set_status(self.findings, dict(RUN, fix_round_start=NEXT), 1, self.fid, "fixed", "fixer", "done", commit=NEXT)
        self.assertIn("after the start of the current fix round", str(ctx.exception))

    def test_a_finding_reported_in_the_rereview_needs_a_new_fix_round(self):
        # One round must not cover endless fix-and-review iterations.
        later, fid, _ = stored(sub=submission(claim="found during the re-review"))
        later[fid]["sources"][0]["head_sha"] = NEXT                       # reported at NEXT, after the round began at HEAD
        run = dict(RUN, shas=[OLD, HEAD, NEXT, "3" * 40])
        with self.assertRaises(CqError) as ctx:
            F.set_status(later, run, 1, fid, "fixed", "fixer", "done", commit="3" * 40)
        self.assertIn("Start a new fix round", str(ctx.exception))
        F.set_status(later, dict(run, fix_round_start=NEXT), 1, fid, "fixed", "fixer", "done", commit="3" * 40)
        # A round opened on history that was since rewritten is not open any more.
        with self.assertRaises(CqError):
            F.set_status(self.findings, dict(RUN, fix_round_start="9" * 40), 1, self.fid, "fixed", "fixer", "done", commit=NEXT)

    def test_reviewer_is_shown_fixed_findings_and_can_say_not_fixed(self):
        F.set_status(self.findings, RUN, 1, self.fid, "fixed", "fixer", "done", commit=NEXT)
        F.upsert_system(self.findings, "duo-1", "duo", "external", HEAD, {"severity": "BLOCKER", "claim": "d"}, "unverified")
        F.set_status(self.findings, RUN, 1, "duo-1", "fixed", "fixer", "done", commit=NEXT)
        F.upsert_system(self.findings, "duo-2", "duo", "external", HEAD, {"severity": "MINOR", "claim": "d"}, "unverified")
        F.set_status(self.findings, RUN, 1, "duo-2", "fixed", "fixer", "done", commit=NEXT)
        self.assertEqual(F.presented_fixes(self.findings, "structure"), sorted(["duo-1", self.fid]))   # its own, and gating Duo ones
        self.assertEqual(F.presented_fixes(self.findings, "simplicity"), ["duo-1"])
        run = dict(RUN, head_sha=NEXT)
        sub = {"scope": "fix_diff", "reviewed_from": HEAD, "files_reviewed": ["src/app.py"], "findings": [], "not_fixed": ["duo-2"]}
        errors, _ = F.check_submission(run, [], "structure", NEXT, sub, ["src/app.py"], F.presented_fixes(self.findings, "structure"))
        self.assertIn("not in your brief's list", errors[0])
        sub["not_fixed"] = [self.fid]
        presented = F.presented_fixes(self.findings, "structure")
        self.assertEqual(F.check_submission(run, [], "structure", NEXT, sub, ["src/app.py"], presented)[0], [])
        env, touched = F.ingest(self.findings, "structure", SRC, NEXT, "b" * 40, sub, presented)
        self.assertEqual((self.findings[self.fid]["status"], self.findings[self.fid]["fix_commit"]), ("verified", None))
        self.assertEqual(env["fixed_presented"], ["duo-1"])                # the one it did not dispute

    def test_contested_can_only_be_moved_by_a_human(self):
        self.set("contested")
        for status, kw in (("verified", {"evidence": "e"}), ("fixed", {"commit": NEXT}), ("filed", {"ticket": "X-1"}), ("rejected", {})):
            with self.assertRaises(CqError) as ctx:
                self.set(status, **kw)
            self.assertIn("waits for a human", str(ctx.exception))
        rec = F.decide(self.findings, FLOOR, 1, self.fid, "reopen", "human:b", "it is real", None, None, datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual((rec["status"], rec["decided_by_human"]), ("verified", False))
        self.set("fixed", commit=NEXT)

    def test_final_check_veto_cannot_be_cleared_by_an_agent(self):
        findings, fid, _ = stored(reviewer="final_check")
        with self.assertRaises(CqError):
            F.set_status(findings, RUN, 3, fid, "fixed", "fixer", "done", commit=NEXT)

    def test_reason_is_required(self):
        with self.assertRaises(CqError):
            self.set("contested", reason=" ")

    def test_agent_cannot_change_a_human_decision(self):
        F.decide(self.findings, FLOOR, 1, self.fid, "reject", "human:b", "not real", None, None, datetime.datetime.now(datetime.timezone.utc))
        with self.assertRaises(CqError):
            self.set("verified", evidence="x")

    def test_merge_keeps_highest_severity_and_least_settled_status(self):
        low, low_id, _ = stored(sub=submission(severity="MINOR", claim="pager is confusing"))
        F.ingest(low, "security", SRC, HEAD, "b" * 40, submission(severity="BLOCKER", claim="pager leaks rows"))
        high_id = [k for k in low if k != low_id][0]
        F.set_status(low, RUN, 1, low_id, "rejected", "aggregator", "style")
        kept = F.merge(low, low_id, [high_id], "aggregator")
        self.assertEqual((kept["severity"], kept["status"]), ("BLOCKER", "unverified"))
        self.assertEqual(low[high_id]["merged_into"], low_id)
        self.assertEqual({s["reviewer"] for s in kept["sources"]}, {"structure", "security"})
        with self.assertRaises(CqError):
            F.merge(low, low_id, [high_id], "aggregator")

    def test_later_report_of_a_merged_finding_lands_on_the_keeper(self):
        both, a, _ = stored(sub=submission(claim="first wording"))
        F.ingest(both, "structure", SRC, HEAD, "b" * 40, submission(claim="second wording"))
        b = [k for k in both if k != a][0]
        F.merge(both, a, [b], "aggregator")
        _, touched = F.ingest(both, "structure", SRC, HEAD, "b" * 40, submission(claim="second wording"))
        self.assertEqual(touched, [a])


class HumanDecisions(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 10, tzinfo=datetime.timezone.utc)

    def test_waiver_length_is_capped_by_tier(self):
        findings, fid, _ = stored()
        with self.assertRaises(CqError):
            F.decide(findings, FLOOR, 3, fid, "waive", "human:b", "ship", 31, None, self.NOW)
        rec = F.decide(findings, FLOOR, 3, fid, "waive", "human:b", "ship", None, "X-9", self.NOW)
        self.assertEqual((rec["status"], rec["waiver"]["expires"], rec["ticket"]), ("waived", "2026-11-09T00:00:00Z", "X-9"))
        self.assertFalse(F.waiver_expired(rec, self.NOW))
        self.assertTrue(F.waiver_expired(rec, self.NOW + datetime.timedelta(days=30)))

    def test_blocker_and_always_blocking_cannot_be_waived(self):
        for extra in ({"severity": "BLOCKER"}, {"always_blocking": "secret"}):
            findings, fid, _ = stored(sub=submission(**extra))
            with self.assertRaises(CqError):
                F.decide(findings, FLOOR, 1, fid, "waive", "human:b", "ship", None, None, self.NOW)
            self.assertEqual(F.decide(findings, FLOOR, 1, fid, "reject", "human:b", "not real", None, None, self.NOW)["status"], "rejected")


class Triage(unittest.TestCase):
    def test_external_comment_needs_triage_before_any_outcome(self):
        findings = {}
        F.upsert_system(findings, "duo-1", "duo", "external", HEAD, {"severity": config.UNTRIAGED, "claim": "Possible null deref"}, "unverified")
        with self.assertRaises(CqError):
            F.set_status(findings, RUN, 1, "duo-1", "rejected", "aggregator", "noise")
        F.triage(findings, "duo-1", "MINOR", "correctness", "x is None when the list is empty", None, None, "aggregator")
        F.set_status(findings, RUN, 1, "duo-1", "rejected", "aggregator", "guarded two lines above")
        with self.assertRaises(CqError):
            F.triage(findings, "duo-1", "NIT", "correctness", "again", None, None, "aggregator")

    def test_sonar_and_gate_guard_findings_are_opened_and_closed_by_their_source(self):
        findings = {}
        F.upsert_system(findings, "sonar-1", "sonar", "external", HEAD, {"severity": "BLOCKER", "claim": "a"}, "unverified")
        with self.assertRaises(CqError) as ctx:
            F.set_status(findings, RUN, 1, "sonar-1", "fixed", "fixer", "done", commit=NEXT)
        self.assertIn("closes when SonarQube stops reporting it", str(ctx.exception))
        other, fid, _ = stored(findings=findings)
        with self.assertRaises(CqError):
            F.merge(findings, fid, ["sonar-1"], "aggregator")
        # The source stops reporting it, then reports it again: it is open again.
        F.close_system(findings["sonar-1"], "sonar", NEXT, "gone")
        self.assertEqual(findings["sonar-1"]["status"], "fixed")
        rec, _ = F.upsert_system(findings, "sonar-1", "sonar", "external", NEXT, {"severity": "BLOCKER", "claim": "a"}, "unverified", reopen=True)
        self.assertEqual((rec["status"], rec["fix_commit"]), ("unverified", None))
        # A Duo thread that is still open on GitLab does not reopen a fixed finding.
        F.upsert_system(findings, "duo-1", "duo", "external", HEAD, {"severity": "MINOR", "claim": "d"}, "unverified")
        F.set_status(findings, RUN, 1, "duo-1", "fixed", "fixer", "done", commit=NEXT)
        rec, _ = F.upsert_system(findings, "duo-1", "duo", "external", NEXT, {"severity": config.UNTRIAGED, "claim": "d"}, "unverified")
        self.assertEqual(rec["status"], "fixed")

    def test_system_finding_keeps_its_status_on_refresh(self):
        findings = {}
        F.upsert_system(findings, "sonar-1", "sonar", "external", OLD, {"severity": "MAJOR", "claim": "a"}, "unverified")
        F.set_status(findings, RUN, 1, "sonar-1", "contested", "aggregator", "disagree")
        rec, created = F.upsert_system(findings, "sonar-1", "sonar", "external", HEAD, {"severity": "MINOR", "claim": "b"}, "unverified")
        self.assertEqual((created, rec["status"], rec["severity"], rec["claim"]), (False, "contested", "MAJOR", "b"))
        F.close_system(rec, "sonar", HEAD, "gone")
        self.assertEqual((rec["status"], rec["fix_commit"]), ("fixed", HEAD))


if __name__ == "__main__":
    unittest.main()
