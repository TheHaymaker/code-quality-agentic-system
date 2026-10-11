"""End-to-end tests. Real git repositories and the real CLI; GitLab and SonarQube
are replaced by a fake glab on PATH and a local HTTP server."""
import contextlib
import datetime
import http.server
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from cq import cli, gitlab, runs, store

ROOT = Path(__file__).resolve().parents[1]
UTC = datetime.timezone.utc


def ago(seconds):
    return (datetime.datetime.now(UTC) - datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeSonar(http.server.BaseHTTPRequestHandler):
    state = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        st = FakeSonar.state
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        st.setdefault("requests", []).append((u.path, q))

        def send(code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        if self.headers.get("Authorization") != "Bearer tok":
            return send(401, {"errors": [{"msg": "Not authorized"}]})
        if u.path == "/api/project_pull_requests/list":
            return send(200, {"pullRequests": st.get("prs", [])})
        if u.path == "/api/project_analyses/search":
            return send(200, {"analyses": st.get("analyses", [])})
        if u.path == "/api/qualitygates/project_status":
            if st.get("gate") is None:
                return send(404, {"errors": [{"msg": "Pull request '12' in project 'demo' not found"}]})
            return send(200, {"projectStatus": st["gate"]})
        if u.path == "/api/issues/search":
            if "components" in q and st.get("old_server"):
                return send(400, {"errors": [{"msg": "unknown parameter"}]})
            issues = st.get("issues", [])
            p, ps = int(q["p"]), int(q["ps"])
            return send(200, {"paging": {"pageIndex": p, "pageSize": ps, "total": len(issues)}, "issues": issues[(p - 1) * ps: p * ps]})
        return send(404, {"errors": [{"msg": "no such endpoint"}]})


class Env(unittest.TestCase):
    """One fresh world per test: control repo, run store, remote, checkout, worktree."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cq-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.control = self.tmp / "control"
        shutil.copytree(ROOT / "tests" / "fixtures" / "policy", self.control / "policy")
        shutil.copytree(ROOT / "templates", self.control / "templates")
        self.home = self.tmp / "home"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        glab = bindir / "glab"
        # A shell wrapper, not a shebang: the interpreter path may contain a space.
        glab.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, ROOT / "tests" / "fake_glab.py"))
        glab.chmod(glab.stat().st_mode | stat.S_IEXEC)
        self.glab_state = self.tmp / "glab.json"

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSonar)
        FakeSonar.state = {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        env = {
            "CQ_CONTROL": str(self.control), "CQ_HOME": str(self.home), "FAKE_GLAB_STATE": str(self.glab_state),
            "PATH": "%s%s%s" % (bindir, os.pathsep, os.environ["PATH"]), "SONAR_TOKEN": "tok",
            "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("CQ_ACTOR", None)

        self.remote = self.tmp / "remote.git"
        self.main = self.tmp / "demo"
        self.wt = self.tmp / "wt"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.git(self.tmp, "init", "-q", "-b", "main", str(self.main))
        self.write(self.main, "src/app.py", "def page(n):\n    return n\n")
        self.write(self.main, "src/pricing/calc.py", "RATE = 1\n")
        self.write(self.main, "tests/test_app.py", "def test_page():\n    assert True\n")
        self.write(self.main, "README.md", "# demo\n")
        self.write(self.main, ".cq/criticality.yml",
                   "schema_version: 1\nrules:\n  - {glob: 'src/pricing/**', tier: 3, reason: 'Pricing'}\n")
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "init")
        self.git(self.main, "remote", "add", "origin", str(self.remote))
        self.git(self.main, "push", "-q", "-u", "origin", "main")
        self.git(self.main, "worktree", "add", "-q", "-b", "feature/x", str(self.wt))
        self.registry()
        self.set_glab()

    # -- helpers ----------------------------------------------------------
    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()

    def write(self, root, rel, text):
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, rel, text, msg="change", cwd=None):
        cwd = cwd or self.wt
        self.write(cwd, rel, text)
        self.git(cwd, "add", "-A")
        self.git(cwd, "commit", "-q", "-m", msg)
        return self.git(cwd, "rev-parse", "HEAD")

    def registry(self, extra=""):
        port = self.server.server_address[1]
        (self.control / "registry.yml").write_text(
            "schema_version: 1\nrepositories:\n"
            "  - id: demo\n"
            "    path: %s\n"
            "    default_branch: main\n"
            "    gitlab_project: group/demo\n"
            "    checks:\n"
            "      lint: ['true']\n"
            "      types: ['true']\n"
            "      unit: ['sh', '-c', 'test ! -f FAIL_UNIT']\n"
            "      build: ['true']\n"
            "    sonar: {project_key: demo, host_url: 'http://127.0.0.1:%d', product: server}\n"
            "%s" % (self.main, port, extra)
        )

    def set_glab(self, **over):
        state = {"project": "group/demo", "head": "", "mrs": [], "reviewers": [], "discussions": [], "calls": []}
        if self.glab_state.exists():
            state = json.loads(self.glab_state.read_text())
        state.update(over)
        self.glab_state.write_text(json.dumps(state))

    def glab(self):
        return json.loads(self.glab_state.read_text())

    def cq(self, *args, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with mock.patch("sys.stdin", io.StringIO(stdin if stdin is not None else "")):
                code = cli.main([str(a) for a in args])
        return code, out.getvalue()

    def ok(self, *args, stdin=None):
        code, out = self.cq(*args, stdin=stdin)
        self.assertEqual(code, 0, out)
        return out

    def start(self, *extra):
        out = self.ok("run", "start", "--worktree", self.wt, *extra)
        return out.split()[2]

    def run_data(self, rid):
        return store.find_run(rid).data

    def findings(self, rid):
        return store.find_run(rid).findings()

    def review(self, rid, reviewer, findings=(), scope="full", reviewed_from=None, files=None):
        data = self.run_data(rid)
        head = self.git(self.wt, "rev-parse", "HEAD")
        if files is None:
            files = data["changed_files"] if scope == "full" else runs.files_between(data, reviewed_from, head)
        sub = {"scope": scope, "files_reviewed": files, "findings": list(findings)}
        if reviewed_from:
            sub["reviewed_from"] = reviewed_from
        return self.cq("findings", "add", rid, "--reviewer", reviewer, "--head", head, "-", stdin=json.dumps(sub))

    def backdate_duo_request(self, rid, seconds=1200):
        """Pretend the Duo review was requested a while ago, so fixture comments count as its answer."""
        run = store.find_run(rid)
        evidence = run.external()["duo"]
        evidence["requested_at"] = ago(seconds)
        run.save_external("duo", evidence)

    def panel(self, rid, reviewers=("simplicity", "structure", "standards_spec")):
        for r in reviewers:
            code, out = self.review(rid, r)
            self.assertEqual(code, 0, out)

    def verdict(self, rid, stage="final"):
        code, out = self.cq("verdict", rid, "--stage", stage, "--json")
        decision = json.loads(out)
        self.assertEqual(code, {"PASS": 0, "FAIL": 1, "BLOCKED": 2, "INCONCLUSIVE": 3}[decision["verdict"]])
        return decision

    def codes(self, rid, stage="final"):
        return {r["code"] for r in self.verdict(rid, stage)["reasons"]}


FINDING = {
    "category": "correctness", "severity": "BLOCKER", "claim": "page(0) returns the last page",
    "location": {"path": "src/app.py", "start_line": 2}, "scenario": "call page(0)", "proposed_fix": "clamp to 1",
}


class Setup(Env):
    def test_init_repo_installs_and_never_overwrites(self):
        self.write(self.main, ".cq/standards/coding-standards.md", "mine\n")
        out = self.ok("init-repo", self.main)
        self.assertIn("kept   .cq/standards/coding-standards.md", out)
        self.assertIn("kept   .cq/criticality.yml", out)
        self.assertEqual((self.main / ".cq/standards/coding-standards.md").read_text(), "mine\n")
        for rel in (".claude/skills/cq-run/SKILL.md", ".claude/agents/cq-reviewer.md", ".claude/settings.json",
                    ".cq/standards/business-critical-map.md"):
            self.assertTrue((self.main / rel).exists(), rel)
        settings = json.loads((self.main / ".claude/settings.json").read_text())
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "cq guard || exit 2")
        again = self.ok("init-repo", self.main)
        self.assertNotIn("wrote", again)

    def test_existing_settings_file_is_left_alone(self):
        self.write(self.main, ".claude/settings.json", '{"permissions": {}}\n')
        out = self.ok("init-repo", self.main)
        self.assertIn("ACTION", out)
        self.assertEqual((self.main / ".claude/settings.json").read_text(), '{"permissions": {}}\n')

    def test_doctor_reports_a_missing_hook_and_a_wrong_project(self):
        code, out = self.cq("doctor")
        self.assertEqual(code, 1)
        self.assertIn("gate-protection hook is not registered", out)
        self.assertIn("GitLab project matches the registry", out)
        self.ok("init-repo", self.main)
        self.set_glab(project="other/demo")
        code, out = self.cq("doctor")
        self.assertIn("remote is GitLab project 'other/demo'", out)
        self.assertNotIn("hook is not registered", out)

    def test_broken_policy_stops_everything(self):
        with open(self.control / "policy" / "floor.yml", "a") as fh:
            fh.write("\nallow_autonomous_merge: true\n")
        code, out = self.cq("doctor")
        self.assertEqual(code, 1)
        self.assertIn("allow_autonomous_merge", out)
        code, out = self.cq("run", "start", "--worktree", self.wt)
        self.assertEqual(code, 4)


class RunsAndTiers(Env):
    def test_tier_follows_the_diff_and_the_repo_is_found_from_the_worktree(self):
        self.commit("src/app.py", "def page(n):\n    return max(n, 1)\n")
        rid = self.start("--ticket", "SHOP-1")
        data = self.run_data(rid)
        self.assertEqual((data["repo_id"], data["tier"], data["mode"], data["changed_files"]), ("demo", 1, "normal", ["src/app.py"]))
        self.assertEqual(self.start(), rid)  # same branch: the active run is resumed
        self.commit("README.md", "# demo2\n")
        self.ok("run", "sync", rid)
        self.assertEqual(self.run_data(rid)["tier"], 1)
        self.commit("db/migrations/001.sql", "ALTER TABLE t ADD c int;\n")
        self.assertIn("tier:       3 Critical", self.ok("run", "sync", rid))

    def test_docs_only_change_is_tier_0(self):
        self.commit("README.md", "# hello\n")
        self.assertEqual(self.run_data(self.start())["tier"], 0)

    def test_branch_cannot_rewrite_the_rules_it_is_judged_by(self):
        # The branch deletes the pricing rule, then changes pricing code.
        self.commit(".cq/criticality.yml", "schema_version: 1\nrules: []\n")
        self.commit("src/pricing/calc.py", "RATE = 0\n")
        rid = self.start()
        data = self.run_data(rid)
        self.assertEqual(data["tier"], 3)
        reasons = {r["path"]: r["reason"] for r in data["tier_reasons"]}
        self.assertEqual(reasons["src/pricing/calc.py"], "Pricing")       # rule read from the base commit
        self.assertIn(".cq/criticality.yml", reasons)                      # and the edit itself is tier 3

    def test_tier_never_falls_by_itself_and_only_a_human_lowers_it(self):
        first = self.commit("db/migrations/001.sql", "x\n")
        rid = self.start()
        self.git(self.wt, "rm", "-q", "db/migrations/001.sql")
        self.git(self.wt, "commit", "-q", "-m", "drop migration")
        self.ok("run", "sync", rid)
        self.assertEqual(self.run_data(rid)["tier"], 3)
        code, out = self.cq("tier", "raise", rid, "1", "--reason", "x")
        self.assertEqual(code, 4)
        code, out = self.cq("tier", "lower", rid, "1", "--reason", "migration removed")
        self.assertEqual(code, 4)
        self.assertIn("human decision", out)
        runs.lower_tier(store.find_run(rid), 1, "Brett", "migration removed")
        self.assertEqual(self.run_data(rid)["tier"], 1)
        with self.assertRaises(Exception):
            runs.lower_tier(store.find_run(rid), 0, "Brett", "too low")

    def test_ticket_label_and_agent_can_raise(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start("--min-tier", "2")
        self.assertEqual(self.run_data(rid)["tier"], 2)
        self.ok("tier", "raise", rid, "3", "--reason", "touches a funnel event")
        self.ok("run", "sync", rid)
        self.assertEqual(self.run_data(rid)["tier"], 3)

    def test_resume_applies_a_higher_floor_and_keeps_the_ticket(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start("--ticket", "SHOP-1")
        out = self.ok("run", "start", "--worktree", self.wt, "--min-tier", "3")
        self.assertIn("resumed run %s" % rid, out)
        self.assertEqual(self.run_data(rid)["tier"], 3)
        code, out = self.cq("run", "start", "--worktree", self.wt, "--ticket", "SHOP-2")
        self.assertEqual(code, 4)
        self.assertIn("is for ticket SHOP-1", out)

    def test_another_branch_name_does_not_buy_a_clean_slate(self):
        self.commit("db/migrations/001.sql", "x\n")
        rid = self.start("--ticket", "SHOP-5")
        self.ok("fix", "round", rid, "--phase", "local")
        # Same worktree, new branch name, even with the commit rewritten.
        self.git(self.wt, "checkout", "-q", "-b", "feature/y")
        self.git(self.wt, "commit", "-q", "--amend", "-m", "reworded")
        code, out = self.cq("run", "start", "--worktree", self.wt)
        self.assertEqual(code, 4)
        self.assertIn("is still active for this worktree", out)
        # Another worktree, same ticket.
        wt2 = self.tmp / "wt2"
        self.git(self.main, "worktree", "add", "-q", "-b", "feature/again", str(wt2))
        self.commit("src/app.py", "x = 9\n", cwd=wt2)
        code, out = self.cq("run", "start", "--worktree", wt2, "--ticket", "SHOP-5")
        self.assertEqual(code, 4)
        self.assertIn("is still active for this ticket", out)
        # Another worktree, another ticket, but built on the first run's commits.
        self.git(self.wt, "checkout", "-q", "feature/x")
        wt3 = self.tmp / "wt3"
        self.git(self.main, "worktree", "add", "-q", "-b", "feature/on-top", str(wt3), "feature/x")
        code, out = self.cq("run", "start", "--worktree", wt3, "--ticket", "SHOP-6")
        self.assertEqual(code, 4)
        self.assertIn("already belong to active run %s" % rid, out)
        # Unrelated work in another worktree is fine.
        self.assertEqual(self.cq("run", "start", "--worktree", wt2, "--ticket", "SHOP-7")[0], 0)

    def test_run_for_merged_work_closes_itself_when_the_worktree_is_reused(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start()
        self.git(self.main, "merge", "-q", "--ff-only", "feature/x")
        self.git(self.main, "push", "-q", "origin", "main")
        self.git(self.wt, "checkout", "-q", "-b", "feature/next", "main")
        self.commit("src/app.py", "x = 2\n")
        other = self.start()
        self.assertNotEqual(other, rid)
        self.assertEqual(self.run_data(rid)["status"], "closed")
        code, out = self.cq("fix", "round", rid, "--phase", "local")
        self.assertEqual(code, 4)                                # a closed run takes no more evidence
        self.assertIn("is closed", out)
        self.ok("run", "show", rid)

    def test_usage_errors_do_not_look_like_a_verdict(self):
        for args in (("verdict",), ("verdict", "x", "--stage", "lokal"), ("bogus",), ("findings", "status", "x", "y", "accepted", "--reason", "r")):
            with self.assertRaises(SystemExit) as ctx:
                self.cq(*args)
            self.assertEqual(ctx.exception.code, 4, args)
        self.assertEqual(self.cq("verdict", "no-such-run")[0], 4)

    def test_hidden_modifications_count_as_uncommitted(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start()
        self.git(self.wt, "update-index", "--skip-worktree", "src/app.py")
        self.write(self.wt, "src/app.py", "x = 2  # hidden from git status\n")
        self.assertEqual(self.git(self.wt, "status", "--porcelain"), "")
        self.assertIn("worktree_dirty", self.codes(rid, "local"))
        self.assertEqual(self.cq("check", "run", rid, "unit")[0], 4)

    def test_files_that_are_not_utf8_do_not_break_a_run(self):
        (self.wt / "data.bin").write_bytes(b"\xff\xfe\x00binary\n" * 50)
        (self.wt / "latin1.txt").write_bytes("caf\xe9\n".encode("latin-1"))
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "bytes")
        rid = self.start()
        data = self.run_data(rid)
        self.assertEqual(data["changed_files"], ["data.bin", "latin1.txt"])
        self.assertGreaterEqual(data["changed_lines"], 1000)       # a binary file is never a small change

    def test_starting_over_closes_the_old_run(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start()
        fresh = runs.start(None, self.wt, None, 0, True)[0].data["run_id"]
        self.assertEqual(self.run_data(rid)["status"], "closed")
        self.assertEqual(self.start(), fresh)

    def test_policy_is_pinned_until_an_explicit_repin(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start()
        self.registry("    paths:\n      - {glob: 'src/app.py', tier: 2, reason: 'Now important'}\n")
        self.ok("run", "sync", rid)
        self.assertEqual(self.run_data(rid)["tier"], 1)
        before = self.run_data(rid)["policy_digest"]
        self.ok("run", "repin", rid)
        after = self.run_data(rid)
        self.assertEqual(after["tier"], 2)
        self.assertNotEqual(after["policy_digest"], before)
        self.assertIn("policy_repinned", [e["type"] for e in store.find_run(rid).events()])

    def test_refuses_default_branch_and_foreign_worktrees(self):
        code, out = self.cq("run", "start", "--worktree", self.main)
        self.assertEqual(code, 4)
        self.assertIn("default branch", out)
        other = self.tmp / "other"
        self.git(self.tmp, "init", "-q", "-b", "feature/y", str(other))
        self.commit("a.txt", "a\n", cwd=other)
        code, out = self.cq("run", "start", "--worktree", other)
        self.assertEqual(code, 4)
        self.assertIn("does not belong to any repository", out)

    def test_tracer_branch(self):
        self.git(self.wt, "checkout", "-q", "-b", "tracer/spike")
        self.commit("db/migrations/001.sql", "x\n")
        rid = self.start()
        data = self.run_data(rid)
        self.assertEqual((data["mode"], data["tier"]), ("tracer", 3))
        self.ok("check", "run", rid, "--required")
        self.panel(rid, ["simplicity"])
        d = self.verdict(rid)
        self.assertEqual((d["verdict"], d["gate_tier"], d["ready_to_merge"]), ("PASS", 0, False))

    def test_tier_estimate_before_code_exists(self):
        out = self.ok("tier", "estimate", "--repo", "demo", "src/pricing/new.py", "README.md")
        self.assertTrue(out.startswith("tier 3 Critical"))

    def test_rebase_drops_evidence_tied_to_old_commits(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start()
        self.ok("check", "run", rid, "--required")
        self.panel(rid)
        self.assertEqual(self.verdict(rid, "local")["verdict"], "PASS")
        self.git(self.wt, "commit", "-q", "--amend", "-m", "reworded")
        codes = self.codes(rid, "local")
        self.assertIn("check_stale", codes)
        self.assertIn("review_missing", codes)


class Checks(Env):
    def test_results_are_tied_to_a_clean_commit(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start()
        self.write(self.wt, "scratch.txt", "uncommitted\n")
        code, out = self.cq("check", "run", rid, "unit")
        self.assertEqual(code, 4)
        self.assertIn("uncommitted changes", out)
        self.assertIn("worktree_dirty", self.codes(rid, "local"))
        (self.wt / "scratch.txt").unlink()
        self.assertIn("PASSED", self.ok("check", "run", rid, "unit"))

    def test_failure_staleness_and_missing_command(self):
        self.commit("FAIL_UNIT", "1\n")
        rid = self.start()
        code, out = self.cq("check", "run", rid, "--required")
        self.assertEqual(code, 1)
        self.assertIn("unit           FAILED", out)
        self.assertEqual(self.verdict(rid, "local")["verdict"], "FAIL")
        self.git(self.wt, "rm", "-q", "FAIL_UNIT")
        self.git(self.wt, "commit", "-q", "-m", "fix")
        self.assertIn("check_stale", self.codes(rid, "local"))   # the old failure no longer counts either way
        self.assertNotIn("check_failed", self.codes(rid, "local"))
        self.ok("check", "run", rid, "--required")
        self.assertNotIn("check_stale", self.codes(rid, "local"))
        code, out = self.cq("check", "run", rid, "e2e")
        self.assertEqual(code, 3)
        self.assertIn("no command in registry.yml", out)
        self.assertEqual(self.ok("check", "log", rid, "unit"), "\n")   # the check printed nothing

    def test_check_that_changes_the_worktree_or_is_missing_is_an_error_not_a_result(self):
        self.registry()
        text = (self.control / "registry.yml").read_text()
        (self.control / "registry.yml").write_text(
            text.replace("lint: ['true']", "lint: ['sh', '-c', 'echo x > generated.txt']")
                .replace("types: ['true']", "types: ['no-such-binary-xyz']"))
        self.commit("src/app.py", "x = 1\n")
        rid = self.start()
        code, out = self.cq("check", "run", rid, "types")
        self.assertEqual(code, 3)
        self.assertIn("command not found", out)
        code, out = self.cq("check", "run", rid, "lint")
        self.assertEqual(code, 3)
        self.assertIn("changed the worktree", out)
        (self.wt / "generated.txt").unlink()
        self.assertTrue({"check_error"} <= self.codes(rid, "local"))


MODULE = "".join("def f%d(n):\n    return n + %d\n\n\n" % (i, i) for i in range(20))


class Reviews(Env):
    def setUp(self):
        super().setUp()
        self.c1 = self.commit("src/app.py", MODULE + "def page(n):\n    return n - 1\n")
        self.rid = self.start()
        self.ok("check", "run", self.rid, "--required")

    def test_brief_tells_the_reviewer_everything(self):
        out = self.ok("brief", self.rid, "--reviewer", "simplicity")
        for needle in ("Code that should not exist".lower(), self.c1, "- src/app.py", '"additionalProperties": false',
                       "cq findings add   %s --reviewer simplicity --head %s -" % (self.rid, self.c1)):
            self.assertIn(needle, out.lower() if needle.islower() else out)
        code, out = self.cq("brief", self.rid, "--reviewer", "simplicity", "--scope", "fix_diff")
        self.assertEqual(code, 4)
        self.assertIn("needs a full review", out)

    def test_a_real_change_after_the_panel_cannot_ride_on_one_empty_review(self):
        self.panel(self.rid)
        self.commit("src/danger.py", "import os\n" + "".join("def run%d(n):\n    os.system(n)\n" % i for i in range(40)))
        self.ok("check", "run", self.rid, "--required")
        code, out = self.review(self.rid, "simplicity", scope="fix_diff", reviewed_from=self.c1)
        self.assertEqual(code, 0, out)
        d = self.verdict(self.rid, "local")
        self.assertEqual(d["verdict"], "INCONCLUSIVE")
        self.assertEqual({r["code"] for r in d["reasons"]}, {"review_too_old"})

    def test_moving_a_file_is_not_a_small_change(self):
        self.panel(self.rid)
        self.git(self.wt, "mv", "src/app.py", "src/application.py")
        self.git(self.wt, "commit", "-q", "-m", "move")
        self.ok("check", "run", self.rid, "--required")
        self.review(self.rid, "simplicity", scope="fix_diff", reviewed_from=self.c1)
        self.assertEqual(self.codes(self.rid, "local"), {"review_too_old"})

    def test_one_fix_round_does_not_cover_a_second_iteration(self):
        self.panel(self.rid, ["simplicity", "standards_spec"])
        fid = self.review(self.rid, "structure", [FINDING])[1].strip().split(": ")[-1]
        self.ok("findings", "status", self.rid, fid, "verified", "--reason", "r", "--evidence", "e")
        self.ok("fix", "round", self.rid, "--phase", "local")
        c2 = self.commit("src/app.py", MODULE + "def page(n):\n    return n or 1\n")
        self.ok("run", "sync", self.rid)
        self.ok("findings", "status", self.rid, fid, "fixed", "--commit", c2, "--reason", "first try")
        # The raiser reviews the fix and says it did not work.
        head = self.git(self.wt, "rev-parse", "HEAD")
        sub = {"scope": "fix_diff", "reviewed_from": self.c1, "files_reviewed": ["src/app.py"], "findings": [], "not_fixed": [fid]}
        self.ok("findings", "add", self.rid, "--reviewer", "structure", "--head", head, "-", stdin=json.dumps(sub))
        self.assertEqual(self.findings(self.rid)[fid]["status"], "verified")
        c3 = self.commit("src/app.py", MODULE + "def page(n):\n    return max(n, 1)\n")
        self.ok("run", "sync", self.rid)
        code, out = self.cq("findings", "status", self.rid, fid, "fixed", "--commit", c3, "--reason", "second try")
        self.assertEqual(code, 4)
        self.assertIn("Start a new fix round", out)
        self.ok("fix", "round", self.rid, "--phase", "local")     # the second and last round
        c4 = self.commit("src/app.py", MODULE + "def page(n):\n    return max(int(n), 1)\n")
        self.ok("run", "sync", self.rid)
        self.ok("findings", "status", self.rid, fid, "fixed", "--commit", c4, "--reason", "second try")
        self.assertEqual(self.run_data(self.rid)["fix_rounds"]["local"], 2)

    def test_fix_marked_after_the_raisers_review_is_not_confirmed(self):
        self.panel(self.rid, ["simplicity", "standards_spec"])
        fid = self.review(self.rid, "structure", [FINDING])[1].strip().split(": ")[-1]
        self.ok("findings", "status", self.rid, fid, "verified", "--reason", "r", "--evidence", "e")
        self.ok("fix", "round", self.rid, "--phase", "local")
        c2 = self.commit("src/app.py", MODULE + "def page(n):\n    return n - 1  # comment only\n")
        self.ok("check", "run", self.rid, "--required")
        # The raiser reviews the new commit first; nothing is marked fixed yet, so nothing is shown to it.
        self.assertEqual(self.review(self.rid, "structure", scope="fix_diff", reviewed_from=self.c1)[0], 0)
        self.ok("findings", "status", self.rid, fid, "fixed", "--commit", c2, "--reason", "done")
        self.assertEqual(self.codes(self.rid, "local"), {"fix_not_rereviewed"})

    def test_check_results_are_ordered_by_when_they_ran(self):
        for _ in range(6):
            self.write(self.wt, "FAIL_UNIT", "1\n")
            self.git(self.wt, "add", "-A")
            self.git(self.wt, "commit", "-q", "-m", "break")
            self.cq("check", "run", self.rid, "unit")
            self.git(self.wt, "rm", "-q", "FAIL_UNIT")
            self.git(self.wt, "commit", "-q", "-m", "repair")
            self.ok("check", "run", self.rid, "unit")
            self.assertEqual(store.find_run(self.rid).checks()["unit"]["status"], "passed")

    def test_submission_must_cover_every_changed_file(self):
        code, out = self.review(self.rid, "simplicity", files=[])
        self.assertEqual(code, 4)
        self.assertIn("src/app.py", out)
        code, out = self.cq("findings", "check", self.rid, "--reviewer", "simplicity", "--head", self.c1, "-",
                            stdin=json.dumps({"scope": "full", "files_reviewed": ["src/app.py"], "findings": []}))
        self.assertEqual(code, 0)
        self.assertEqual(store.find_run(self.rid).envelopes(), [])  # a dry run stores nothing
        code, out = self.cq("findings", "add", self.rid, "--reviewer", "simplicity", "--head", self.c1, "-", stdin="not json")
        self.assertEqual(code, 4)
        code, out = self.cq("findings", "add", self.rid, "--reviewer", "simplicity", "--head", self.c1, "/no/such/file.json")
        self.assertEqual(code, 4)                                   # a bad argument must never look like FAIL (1)
        code, out = self.cq("note", "add", self.rid, "plan", "/no/such/plan.md")
        self.assertEqual(code, 4)

    def test_blocker_from_report_to_verified_fix_and_narrow_rereview(self):
        self.panel(self.rid, ["simplicity", "standards_spec"])
        code, out = self.review(self.rid, "structure", [FINDING])
        self.assertEqual(code, 0, out)
        fid = out.strip().split(": ")[-1]
        self.assertEqual(self.verdict(self.rid, "local")["verdict"], "INCONCLUSIVE")   # reported, not yet verified
        code, out = self.cq("findings", "status", self.rid, fid, "rejected", "--reason", "looks fine to me")
        self.assertEqual(code, 4)
        self.assertIn("only a human can reject", out)
        self.ok("findings", "status", self.rid, fid, "verified", "--by", "verifier", "--reason", "reproduced",
                "--evidence", "assert page(0) == 1 fails")
        self.assertEqual(self.verdict(self.rid, "local")["verdict"], "FAIL")

        # "Fixed" is refused without a fix round, and for the commit the finding was reported on.
        code, out = self.cq("findings", "status", self.rid, fid, "fixed", "--commit", self.c1, "--reason", "done")
        self.assertEqual(code, 4)
        self.assertEqual(self.verdict(self.rid, "local")["verdict"], "FAIL")
        c_outside = self.commit("src/app.py", MODULE + "def page(n):\n    return n - 1  # todo\n", "not a fix")
        self.ok("run", "sync", self.rid)
        code, out = self.cq("findings", "status", self.rid, fid, "fixed", "--commit", c_outside, "--reason", "done")
        self.assertEqual(code, 4)
        self.assertIn("no fix round is open", out)

        self.ok("fix", "round", self.rid, "--phase", "local")
        code, out = self.cq("findings", "status", self.rid, fid, "fixed", "--commit", c_outside, "--reason", "done")
        self.assertEqual(code, 4)
        self.assertIn("after the start of the current fix round", out)
        c2 = self.commit("src/app.py", MODULE + "def page(n):\n    return max(n, 1)\n", "fix %s" % fid)
        self.ok("run", "sync", self.rid)
        self.ok("findings", "status", self.rid, fid, "fixed", "--by", "fixer", "--commit", c2[:10], "--reason", "clamped")
        codes = self.codes(self.rid, "local")
        self.assertTrue({"check_stale", "fix_not_rereviewed", "review_stale"} <= codes, codes)
        self.ok("check", "run", self.rid, "--required")
        # A fix-diff review from another reviewer covers the others, not the one whose finding was fixed.
        code, out = self.review(self.rid, "simplicity", scope="fix_diff", reviewed_from=self.c1)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.codes(self.rid, "local"), {"fix_not_rereviewed"})
        # The brief for the raiser starts where its last review ended and names its fixed finding.
        brief = self.ok("brief", self.rid, "--reviewer", "structure", "--scope", "fix_diff")
        self.assertIn("git -C %s diff %s %s" % (self.wt, self.c1, c2), brief)
        self.assertIn("%s (src/app.py): page(0) returns the last page" % fid, brief)
        code, out = self.review(self.rid, "structure", scope="fix_diff", reviewed_from=self.c1)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.verdict(self.rid, "local")["verdict"], "PASS")

        # Second round: each fix-diff review continues from that reviewer's last one.
        self.ok("fix", "round", self.rid, "--phase", "local")
        c3 = self.commit("src/app.py", MODULE + "def page(n):\n    return max(int(n), 1)\n", "second fix")
        self.ok("check", "run", self.rid, "--required")
        brief = self.ok("brief", self.rid, "--reviewer", "structure", "--scope", "fix_diff")
        self.assertIn("diff %s %s" % (c2, c3), brief)
        code, out = self.review(self.rid, "structure", scope="fix_diff", reviewed_from=c2)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.verdict(self.rid, "local")["verdict"], "PASS")
        code, out = self.cq("brief", self.rid, "--reviewer", "structure", "--scope", "fix_diff")
        self.assertEqual(code, 4)                                   # nothing new to review
        events = [e["type"] for e in store.find_run(self.rid).events()]
        for kind in ("run_started", "check_ran", "review_received", "finding_status", "fix_round_started", "head_moved", "verdict"):
            self.assertIn(kind, events)

    def test_fix_budget_ends_with_a_human(self):
        self.ok("fix", "round", self.rid, "--phase", "local")
        self.ok("fix", "round", self.rid, "--phase", "local")
        code, out = self.cq("fix", "round", self.rid, "--phase", "local")
        self.assertEqual(code, 4)
        self.assertIn("Escalate", out)
        self.assertIn("budget_spent", self.codes(self.rid, "local"))
        code, out = self.cq("fix", "extend", self.rid, "--phase", "local")
        self.assertEqual(code, 4)                                   # only a human grants more
        runs.extend_budget(store.find_run(self.rid), "local", "Brett")
        self.assertNotIn("budget_spent", self.codes(self.rid, "local"))
        self.ok("fix", "round", self.rid, "--phase", "local")
        self.assertEqual(self.cq("fix", "round", self.rid, "--phase", "local")[0], 4)

    def test_final_check_veto_and_contested_findings_stay_with_the_human(self):
        self.panel(self.rid)
        code, out = self.review(self.rid, "final_check", [FINDING])
        fid = out.strip().split(": ")[-1]
        self.assertEqual(self.verdict(self.rid, "local")["verdict"], "BLOCKED")
        self.ok("fix", "round", self.rid, "--phase", "local")
        c2 = self.commit("src/app.py", MODULE + "def page(n):\n    return 1\n")
        self.ok("run", "sync", self.rid)
        for args in (("fixed", "--commit", c2), ("verified", "--evidence", "e"), ("rejected",)):
            code, out = self.cq("findings", "status", self.rid, fid, *args, "--reason", "x")
            self.assertEqual(code, 4, args)
            self.assertIn("waits for a human", out)
        self.assertIn("needs_human", self.codes(self.rid, "local"))

    def test_major_filed_with_ticket_passes_tier_1(self):
        self.panel(self.rid, ["simplicity", "standards_spec"])
        code, out = self.review(self.rid, "structure", [dict(FINDING, severity="MAJOR")])
        fid = out.strip().split(": ")[-1]
        self.ok("findings", "status", self.rid, fid, "verified", "--reason", "r", "--evidence", "e")
        self.ok("findings", "status", self.rid, fid, "filed", "--ticket", "SHOP-9", "--reason", "edge case, low traffic")
        self.assertEqual(self.verdict(self.rid, "local")["verdict"], "PASS")
        self.assertIn("SHOP-9", self.ok("report", self.rid, "--stage", "local"))

    def test_human_decisions_need_a_terminal(self):
        for args in (("approve", self.rid, "final_signoff"), ("decide", self.rid, "f-x", "reject", "--reason", "r")):
            code, out = self.cq(*args)
            self.assertEqual(code, 4)
            self.assertIn("human decision", out)
        self.assertEqual(store.find_run(self.rid).approvals(), [])


class GateGuard(Env):
    def test_deleted_and_skipped_tests_go_to_a_human(self):
        self.write(self.wt, "tests/test_other.py", "import pytest\n\n@pytest.mark.skip\ndef test_x():\n    assert False\n")
        self.git(self.wt, "rm", "-q", "tests/test_app.py")
        self.write(self.wt, "src/app.py", "def page(n):  # noqa\n    return n\n")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "make it green")
        rid = self.start()
        found = {f["sources"][0]["local_id"]: f for f in self.findings(rid).values()}
        self.assertEqual(set(found), {"test_deleted", "test_skipped", "check_suppressed"})
        self.assertEqual((found["test_deleted"]["severity"], found["test_deleted"]["status"]), ("MAJOR", "contested"))
        self.assertEqual(found["test_skipped"]["location"], {"path": "tests/test_other.py", "start_line": 3, "end_line": 3})
        self.assertEqual((found["check_suppressed"]["severity"], found["check_suppressed"]["status"]), ("MINOR", "unverified"))
        self.ok("check", "run", rid, "--required")
        self.panel(rid)
        self.assertEqual(self.verdict(rid, "local")["verdict"], "BLOCKED")
        # An agent cannot wave these through.
        gid = found["test_deleted"]["id"]
        code, out = self.cq("findings", "status", rid, gid, "fixed", "--commit", "x", "--reason", "trust me")
        self.assertEqual(code, 4)
        self.assertIn("only a human decides", out)
        # Putting the test back closes the finding by itself.
        self.git(self.wt, "checkout", "-q", "main", "--", "tests/test_app.py")
        self.git(self.wt, "commit", "-q", "-m", "restore test")
        self.ok("run", "sync", rid)
        self.assertEqual(self.findings(rid)[gid]["status"], "fixed")
        # Deleting it again opens the same finding again.
        self.git(self.wt, "rm", "-q", "tests/test_app.py")
        self.git(self.wt, "commit", "-q", "-m", "delete again")
        self.ok("run", "sync", rid)
        self.assertEqual(self.findings(rid)[gid]["status"], "contested")

    def test_diff_parser_handles_odd_paths_and_odd_lines(self):
        skip = "import pytest\n\n@pytest.mark.skip\ndef test_x():\n    pass\n"
        self.write(self.wt, "tests/test_caf\u00e9 one.py", skip)
        self.write(self.wt, "tests/test_plus.py", "++ b/zzz\n" + skip)      # shows in the diff as a line starting '+++ b/'
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "odd")
        found = {f["location"]["path"] for f in self.findings(self.start()).values()}
        self.assertEqual(found, {"tests/test_caf\u00e9 one.py", "tests/test_plus.py"})

    def test_change_to_what_a_check_runs_goes_to_a_human(self):
        self.write(self.main, "package.json", '{"name": "demo", "scripts": {"test": "jest"}, "dependencies": {}}\n')
        self.git(self.main, "add", "-A")
        self.git(self.main, "commit", "-q", "-m", "add package.json")
        self.git(self.main, "push", "-q", "origin", "main")
        self.git(self.wt, "merge", "-q", "main")
        self.commit("package.json", '{"name": "demo", "scripts": {"test": "jest"}, "dependencies": {"left-pad": "1"}}\n')
        rid = self.start()
        self.assertEqual(self.findings(rid), {})                          # a dependency change is not a gate change
        self.commit("package.json", '{"name": "demo", "scripts": {"test": "true"}, "dependencies": {"left-pad": "1"}}\n')
        self.commit("jest.config.js", "module.exports = {coverageThreshold: {global: {lines: 0}}}\n")
        self.ok("run", "sync", rid)
        got = {(f["location"]["path"], f["severity"], f["status"]) for f in self.findings(rid).values()}
        self.assertEqual(got, {("package.json", "MAJOR", "contested"), ("jest.config.js", "MAJOR", "contested")})

    def test_human_decision_on_a_gate_file_covers_that_content_only(self):
        from cq import findings as F
        self.commit("Makefile", "test:\n\tpytest\n", "add makefile")
        self.git(self.wt, "push", "-q", "origin", "HEAD:main")
        self.git(self.wt, "fetch", "-q", "origin")
        self.commit("Makefile", "test:\n\tpytest -q\n")
        rid = self.start()
        run = store.find_run(rid)
        (gid,) = self.findings(rid)
        fnd = run.findings()
        F.decide(fnd, run.policy()["floor"], 1, gid, "reject", "human:Brett", "quiet flag is fine", None, None, datetime.datetime.now(UTC))
        run.save_findings(fnd)
        self.assertNotIn("needs_human", self.codes(rid, "local"))
        self.commit("Makefile", "test:\n\ttrue\n")                  # now the test target does nothing
        self.ok("run", "sync", rid)
        fresh = [f for f in self.findings(rid).values() if f["status"] == "contested"]
        self.assertEqual(len(fresh), 1)
        self.assertNotEqual(fresh[0]["id"], gid)
        self.assertIn("needs_human", self.codes(rid, "local"))

    def test_more_ways_to_switch_a_test_off(self):
        self.write(self.wt, "tests/test_a.py", "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.skipTest('later')\n")
        self.write(self.wt, "tests/test_b.py", "import pytest\n@pytest.mark.skip\ndef test_1():\n    pass\n\n@pytest.mark.skip\ndef test_2():\n    pass\n")
        self.write(self.wt, "tests/conftest.py", "collect_ignore_glob = ['*']\n")
        self.git(self.wt, "mv", "tests/test_app.py", "attic_app.txt")
        self.git(self.wt, "add", "-A")
        self.git(self.wt, "commit", "-q", "-m", "quiet")
        got = sorted((f["sources"][0]["local_id"], f["location"]["path"]) for f in self.findings(self.start()).values())
        self.assertEqual(got, [
            ("check_definition", "tests/conftest.py"),
            ("test_deleted", "tests/test_app.py"),                  # moved out of the test tree
            ("test_skipped", "tests/test_a.py"),
            ("test_skipped", "tests/test_b.py"), ("test_skipped", "tests/test_b.py"),   # two identical lines, two findings
        ])

    def test_skip_marker_outside_test_files_is_not_flagged(self):
        self.commit("docs/testing.md", "Use `it.skip(` sparingly.\n")
        self.assertEqual(self.findings(self.start()), {})


class Guard(Env):
    def hook(self, tool, tool_input, cwd=None):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd or self.wt), "hook_event_name": "PreToolUse"}
        return self.cq("guard", stdin=json.dumps(payload))

    def test_file_edits(self):
        self.assertEqual(self.hook("Edit", {"file_path": str(self.wt / "src/app.py")})[0], 0)
        self.assertEqual(self.hook("Write", {"file_path": "src/new.py"})[0], 0)
        for path in (".cq/criticality.yml", ".claude/settings.json", ".gitlab-ci.yml", "sonar-project.properties",
                     "ci/.gitlab-ci-test.yml"):
            code, out = self.hook("Edit", {"file_path": str(self.wt / path)})
            self.assertEqual(code, 2, path)
            self.assertIn("protected path", out)
        self.assertEqual(self.hook("Write", {"file_path": ".cq/standards/x.md"})[0], 2)
        self.assertEqual(self.hook("Write", {"file_path": str(self.control / "policy/floor.yml")})[0], 2)
        self.assertEqual(self.hook("Write", {"file_path": str(self.home / "runs/x/findings.json")})[0], 2)
        self.assertEqual(self.hook("NotebookEdit", {"notebook_path": str(self.wt / ".cq/x.ipynb")})[0], 2)
        # The session sits elsewhere and edits policy in the worktree.
        self.assertEqual(self.hook("Edit", {"file_path": str(self.wt / ".cq/criticality.yml")}, cwd=self.main)[0], 2)
        self.assertEqual(self.hook("Write", {"file_path": str(self.wt / ".cq/new/deep/file.yml")}, cwd=self.tmp)[0], 2)

    def test_shell_commands(self):
        self.assertEqual(self.hook("Bash", {"command": "cq verdict run-1 && cq findings list run-1"})[0], 0)
        self.assertEqual(self.hook("Bash", {"command": "npm test"})[0], 0)
        for command in ("cq approve run-1 final_signoff", "cd x && cq  decide run-1 f-1 reject --reason ok",
                        "~/.local/bin/cq approve run-1 final_signoff", "'cq' decide run-1 f-1 waive --reason ok",
                        "python -m cq.cli approve run-1 plan_approval", "cq fix extend run-1 --phase local",
                        "cq run close run-1", "cq run start --ticket X-1 --new", "CQ_CONTROL=/tmp/mine cq verdict run-1",
                        "export CQ_HOME=/tmp/fresh; cq run start",
                        "echo yes | cq tier lower run-1 0 --reason x",
                        "cat %s/runs/r/findings.json" % self.home, "sed -i s/3/0/ %s/policy/floor.yml" % self.control):
            self.assertEqual(self.hook("Bash", {"command": command})[0], 2, command)

    def test_engine_sessions_may_only_read_with_cq(self):
        with mock.patch.dict(os.environ, {"CQ_ACTOR": "fixer"}):
            for command in ("cq findings status r f fixed --commit x --reason y", "cq fix round r --phase local",
                            "git add -A && cq findings add r --reviewer structure --head x -", "cq engine run SHOP-1",
                            "cq note add r plan p.md", "cq duo reply r f --body-file b --resolve"):
                self.assertEqual(self.hook("Bash", {"command": command})[0], 2, command)
            for command in ("cq brief r --reviewer structure", "cq findings list r --open", "cq check log r unit",
                            "cq verdict r --stage local", "npm test"):
                self.assertEqual(self.hook("Bash", {"command": command})[0], 0, command)
        self.assertEqual(self.hook("Bash", {"command": "cq fix round r --phase local"})[0], 0)   # outside the engine

    def test_other_tools_pass_and_bad_input_fails_closed(self):
        self.assertEqual(self.hook("Read", {"file_path": str(self.control / "policy/floor.yml")})[0], 0)
        code, out = self.cq("guard", stdin="not json")
        self.assertEqual(code, 2)
        (self.control / "policy" / "floor.yml").write_text("broken: [")
        self.assertEqual(self.hook("Edit", {"file_path": str(self.wt / "src/app.py")})[0], 2)


class MergeRequestAndDuo(Env):
    def setUp(self):
        super().setUp()
        self.head = self.commit("src/app.py", "def page(n):\n    return max(n, 1)\n")
        self.rid = self.start("--ticket", "SHOP-1")
        patcher = mock.patch.object(gitlab.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def push(self):
        self.git(self.wt, "push", "-q", "-u", "origin", "feature/x")
        self.set_glab(head=self.git(self.wt, "rev-parse", "HEAD"))

    def calls(self, method=None, contains=""):
        return [c for c in self.glab()["calls"] if (method is None or c["method"] == method) and contains in c["url"]]

    def thread(self, did, body, resolved=False, author="GitLabDuo", age=600, path="src/app.py", line=2):
        return {"id": did, "individual_note": False, "notes": [{
            "id": 1, "body": body, "author": {"username": author}, "system": False, "resolvable": True,
            "resolved": resolved, "created_at": ago(age),
            "position": {"new_path": path, "new_line": line, "head_sha": self.git(self.wt, "rev-parse", "HEAD")},
        }]}

    def summary(self, body="Review summary", age=600):
        return {"id": "s1", "individual_note": True, "notes": [{
            "id": 2, "body": body, "author": {"username": "GitLabDuo"}, "system": False, "resolvable": False,
            "created_at": ago(age)}]}

    def test_merge_request_is_created_once_and_must_be_pushed_first(self):
        code, out = self.cq("mr", "ensure", self.rid)
        self.assertEqual(code, 4)
        self.assertIn("Push it first", out)
        self.push()
        self.assertIn("created as draft", self.ok("mr", "ensure", self.rid))
        self.ok("mr", "ensure", self.rid)
        posts = self.calls("POST", "merge_requests")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["fields"]["title"], "Draft: SHOP-1 feature/x")
        self.assertEqual(posts[0]["fields"]["target_branch"], "main")

    def test_existing_merge_request_is_adopted_and_wrong_project_is_refused(self):
        self.push()
        self.set_glab(mrs=[{"iid": 40, "source_branch": "feature/x", "target_branch": "main", "web_url": "u", "description": "", "sha": self.head}])
        self.assertIn("already existed", self.ok("mr", "ensure", self.rid))
        self.assertEqual(self.calls("POST"), [])
        self.assertEqual(self.run_data(self.rid)["mr"]["iid"], 40)

        code, out = self.cq("run", "start", "--worktree", self.wt, "--new")
        self.assertEqual(code, 4)                                # starting over is a human decision
        other = runs.start(None, self.wt, None, 0, True)[0].data["run_id"]
        self.set_glab(project="someone/else")
        code, out = self.cq("mr", "ensure", other)
        self.assertEqual(code, 4)
        self.assertIn("registry.yml says 'group/demo'", out)

    def test_duo_states_are_never_blurred(self):
        self.push()
        self.ok("mr", "ensure", self.rid)
        # Nothing requested yet: not the same as "no comments".
        code, out = self.cq("duo", "ingest", self.rid)
        self.assertIn("duo: not_requested", out)
        self.assertIn("duo_not_requested", self.codes(self.rid))

        code, out = self.cq("duo", "request", self.rid)
        self.assertEqual(code, 3, out)
        self.assertIn("duo: pending", out)
        self.assertEqual(self.calls("POST", "/notes")[0]["fields"]["body"], "/assign_reviewer @GitLabDuo")
        self.cq("duo", "request", self.rid)
        self.assertEqual(len(self.calls("POST", "/notes")), 1)             # asking twice for one commit posts once

        # Duo has not posted. An empty thread list is still "pending".
        code, out = self.cq("duo", "wait", self.rid, "--timeout", "0")
        self.assertEqual(code, 3)
        self.assertIn("has not posted since the request", out)
        self.assertIn("duo_pending", self.codes(self.rid))

        # Duo is mid-review: it posted seconds ago.
        self.backdate_duo_request(self.rid)
        self.set_glab(discussions=[self.thread("d1", "Possible off-by-one", age=5)])
        self.assertIn("may still be writing", self.cq("duo", "ingest", self.rid)[1])

        # Duo's flow failed.
        self.set_glab(discussions=[self.summary("Code review failed. Error code: DCR4001", age=300)])
        code, out = self.cq("duo", "ingest", self.rid)
        self.assertEqual(code, 3)
        self.assertIn("duo: error", out)
        self.assertIn("DCR4001", out)

        # The merge request is behind the local head.
        self.set_glab(head="f" * 40, discussions=[])
        self.assertIn("Push first", self.cq("duo", "ingest", self.rid)[1])

        # GitLab is down.
        self.set_glab(head=self.head, fail_all=True)
        code, out = self.cq("duo", "ingest", self.rid)
        self.assertIn("duo: error", out)
        self.assertIn("duo_error", self.codes(self.rid))

    def test_request_that_does_not_stick_is_reported(self):
        self.push()
        self.ok("mr", "ensure", self.rid)
        self.set_glab(duo_accepts=False)
        code, out = self.cq("duo", "request", self.rid)
        self.assertEqual(code, 3)
        self.assertIn("request_failed", out)
        self.assertIn("duo_request_failed", self.codes(self.rid))
        self.assertIn("duo: not_requested", self.cq("duo", "ingest", self.rid)[1])

    def test_review_is_read_across_pages_and_every_thread_needs_an_outcome(self):
        self.push()
        self.ok("mr", "ensure", self.rid)
        self.cq("duo", "request", self.rid)
        self.backdate_duo_request(self.rid)
        noise = [self.thread("h%d" % i, "human comment", author="colleague") for i in range(148)]
        discussions = noise[:120] + [self.thread("duo-thread-aaaaaaaa", "Unvalidated input reaches the query")] + noise[120:] + [
            self.thread("duo-thread-bbbbbbbb", "Rename this variable", line=1),
            self.thread("duo-thread-cccccccc", "Old, already resolved", resolved=True),
            self.summary(),
        ]
        self.set_glab(discussions=discussions)
        code, out = self.cq("duo", "wait", self.rid, "--timeout", "0")
        self.assertEqual(code, 0, out)
        self.assertIn("duo: reviewed. 4 comment(s), 2 open thread(s).", out)
        pages = [c["url"] for c in self.calls("GET", "/discussions")]
        self.assertTrue(any("page=2" in u for u in pages))
        fnd = self.findings(self.rid)
        duo = {k: v for k, v in fnd.items() if k.startswith("duo-")}
        self.assertEqual(len(duo), 2)                                       # human threads and resolved threads are not findings
        self.assertTrue(all(f["severity"] == "UNTRIAGED" for f in duo.values()))
        self.assertIn("untriaged", self.codes(self.rid))

        first, second = sorted(duo)
        self.write(self.tmp, "reply.md", "Not changing this. The name matches the domain glossary.\n")
        code, out = self.cq("duo", "reply", self.rid, second, "--body-file", self.tmp / "reply.md", "--resolve")
        self.assertEqual(code, 4)
        self.assertIn("never resolved silently", out)
        self.assertEqual(self.calls("PUT", "resolved=true"), [])

        self.ok("findings", "triage", self.rid, second, "--severity", "NIT", "--category", "structure", "--scenario", "naming preference")
        self.ok("findings", "status", self.rid, second, "rejected", "--reason", "name matches the glossary")
        self.ok("duo", "reply", self.rid, second, "--body-file", self.tmp / "reply.md", "--resolve")
        self.assertEqual(len(self.calls("PUT", "duo-thread-bbbbbbbb?resolved=true")), 1)
        self.assertIn("glossary", self.calls("POST", "duo-thread-bbbbbbbb/notes")[0]["fields"]["body"])

        self.ok("findings", "triage", self.rid, first, "--severity", "BLOCKER", "--category", "security",
                "--scenario", "id=1 OR 1=1 returns every row", "--always-blocking", "injection")
        self.ok("findings", "status", self.rid, first, "contested", "--reason", "parameterised two lines below; Duo disagrees")
        self.cq("duo", "ingest", self.rid)
        d = self.verdict(self.rid)
        self.assertIn("needs_human", {r["code"] for r in d["reasons"]})
        self.assertNotIn("duo_threads_open", {r["code"] for r in d["reasons"]})   # the contested thread stays open for the human

    def test_two_threads_merged_into_one_finding_are_both_answered(self):
        self.push()
        self.ok("mr", "ensure", self.rid)
        self.cq("duo", "request", self.rid)
        self.backdate_duo_request(self.rid)
        self.set_glab(discussions=[self.thread("thread-one-1111111", "Unused import"), self.thread("thread-two-2222222", "Import is unused", line=1)])
        self.assertEqual(self.cq("duo", "ingest", self.rid)[0], 0)
        first, second = sorted(k for k in self.findings(self.rid) if k.startswith("duo-"))
        for fid in (first, second):
            self.ok("findings", "triage", self.rid, fid, "--severity", "MINOR", "--category", "structure", "--scenario", "dead import")
        self.ok("findings", "merge", self.rid, first, second)
        self.ok("findings", "status", self.rid, first, "rejected", "--reason", "the import is used by the type checker")
        self.write(self.tmp, "reply.md", "Kept: the type checker needs it.\n")
        self.ok("duo", "reply", self.rid, first, "--body-file", self.tmp / "reply.md", "--resolve")
        self.assertEqual(len(self.calls("PUT", "resolved=true")), 2)
        self.cq("duo", "ingest", self.rid)
        self.assertNotIn("duo_threads_open", self.codes(self.rid))

    def test_reviewer_state_left_over_from_an_earlier_review_is_not_a_new_review(self):
        self.push()
        self.ok("mr", "ensure", self.rid)
        # Duo is already a reviewer with state "reviewed", and the new request changes nothing.
        self.set_glab(reviewers=[{"user": {"username": "GitLabDuo"}, "state": "reviewed"}], duo_accepts=False)
        code, out = self.cq("duo", "request", self.rid)
        self.assertEqual(code, 3)
        code, out = self.cq("duo", "ingest", self.rid)
        self.assertEqual(code, 3)
        self.assertIn("duo: pending", out)
        # Once the state is seen to reset and come back, it counts.
        self.set_glab(reviewers=[{"user": {"username": "GitLabDuo"}, "state": "review_started"}])
        self.cq("duo", "ingest", self.rid)
        self.set_glab(reviewers=[{"user": {"username": "GitLabDuo"}, "state": "reviewed"}])
        self.assertIn("duo: reviewed", self.cq("duo", "ingest", self.rid)[1])

    def test_gating_duo_finding_is_not_closed_by_claim_alone(self):
        self.ok("check", "run", self.rid, "--required")
        self.panel(self.rid)
        self.push()
        self.ok("mr", "ensure", self.rid)
        self.cq("duo", "request", self.rid)
        self.backdate_duo_request(self.rid)
        self.set_glab(discussions=[self.thread("thread-inj-1111111", "User input reaches the query"), self.summary()])
        self.cq("duo", "ingest", self.rid)
        (fid,) = [k for k in self.findings(self.rid) if k.startswith("duo-")]
        self.ok("findings", "triage", self.rid, fid, "--severity", "BLOCKER", "--category", "security", "--scenario", "id=1 OR 1=1")
        self.ok("findings", "status", self.rid, fid, "verified", "--reason", "r", "--evidence", "e")
        self.ok("fix", "round", self.rid, "--phase", "mr")
        c2 = self.commit("src/app.py", "def page(n):\n    return max(n, 1)  # no-op\n")
        self.ok("run", "sync", self.rid)
        self.ok("findings", "status", self.rid, fid, "fixed", "--commit", c2, "--reason", "done")
        self.write(self.tmp, "reply.md", "Fixed.\n")
        self.ok("duo", "reply", self.rid, fid, "--body-file", self.tmp / "reply.md", "--resolve")
        self.assertIn("fix_not_rereviewed", self.codes(self.rid, "local"))
        brief = self.ok("brief", self.rid, "--reviewer", "structure", "--scope", "fix_diff")
        self.assertIn("- %s (src/app.py): User input reaches the query" % fid, brief)
        self.review(self.rid, "structure", scope="fix_diff", reviewed_from=self.head)
        self.assertNotIn("fix_not_rereviewed", self.codes(self.rid, "local"))

    def test_new_commit_needs_a_new_review(self):
        self.push()
        self.ok("mr", "ensure", self.rid)
        self.cq("duo", "request", self.rid)
        self.backdate_duo_request(self.rid)
        self.set_glab(discussions=[self.summary()])
        self.assertEqual(self.cq("duo", "ingest", self.rid)[0], 0)
        self.commit("src/app.py", "def page(n):\n    return max(int(n), 1)\n")
        self.push()
        self.ok("run", "sync", self.rid)
        self.assertIn("duo_stale", self.codes(self.rid))
        self.assertIn("not_requested", self.cq("duo", "ingest", self.rid)[1])
        self.cq("duo", "request", self.rid)
        self.assertEqual(len(self.calls("POST", "/notes")), 2)
        self.assertIn("pending", self.cq("duo", "ingest", self.rid)[1])     # the old summary predates the new request


class Sonar(Env):
    def setUp(self):
        super().setUp()
        self.head = self.commit("src/app.py", "def page(n):\n    return max(n, 1)\n")
        self.rid = self.start()
        self.git(self.wt, "push", "-q", "-u", "origin", "feature/x")
        self.set_glab(head=self.head)
        self.ok("mr", "ensure", self.rid)
        self.fresh = (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        self.old = "2020-01-01T00:00:00+0000"

    def sonar(self, **state):
        FakeSonar.state = dict(state)
        return self.cq("sonar", "gate", self.rid)

    def pr(self, date, **extra):
        return [dict({"key": "12", "branch": "feature/x", "analysisDate": date, "status": {"qualityGateStatus": "OK"}}, **extra)]

    def test_five_outcomes_are_kept_apart(self):
        ok = {"status": "OK", "conditions": []}
        bad = {"status": "ERROR", "conditions": [
            {"status": "ERROR", "metricKey": "new_coverage", "comparator": "LT", "errorThreshold": "80", "actualValue": "41.5"}]}

        code, out = self.sonar(prs=[], gate=ok)
        self.assertEqual(code, 3)
        self.assertIn("sonar: missing", out)
        self.assertIn("sonar_missing", self.codes(self.rid))

        code, out = self.sonar(prs=[{"key": "99", "analysisDate": self.fresh}], gate=ok)
        self.assertIn("sonar: missing", out)

        code, out = self.sonar(prs=self.pr(self.old), gate=ok)
        self.assertEqual(code, 3)
        self.assertIn("sonar: stale", out)
        self.assertIn("sonar_stale", self.codes(self.rid))

        code, out = self.sonar(prs=self.pr(self.fresh), gate=bad)
        self.assertEqual(code, 1)
        self.assertIn("sonar: failed. Quality gate ERROR: new_coverage 41.5 (limit 80)", out)
        self.assertIn("sonar_failed", self.codes(self.rid))

        code, out = self.sonar(prs=self.pr(self.fresh), gate=ok)
        self.assertEqual(code, 0)
        self.assertIn("sonar: passed. Quality gate OK, matched by analysis time", out)
        self.assertNotIn("sonar_", " ".join(self.codes(self.rid)))

        code, out = self.sonar(prs=self.pr(self.fresh), gate=None)
        self.assertIn("sonar: missing", out)
        code, out = self.sonar(prs=self.pr(self.fresh), gate={"status": "NONE"})
        self.assertIn("sonar: missing", out)

        with mock.patch.dict(os.environ, {"SONAR_TOKEN": "wrong"}):
            code, out = self.sonar(prs=self.pr(self.fresh), gate=ok)
        self.assertEqual(code, 3)
        self.assertIn("sonar: error. pull request list returned 401: Not authorized", out)
        self.assertIn("sonar_error", self.codes(self.rid))

        with mock.patch.dict(os.environ, {"SONAR_TOKEN": ""}):
            self.assertIn("SONAR_TOKEN is not set", self.sonar(prs=self.pr(self.fresh), gate=ok)[1])

        self.server.shutdown()
        self.server.server_close()
        code, out = self.cq("sonar", "gate", self.rid)
        self.assertIn("sonar: error", out)

    def test_commit_match_beats_time_when_sonar_reports_the_commit(self):
        ok = {"status": "OK", "conditions": []}
        code, out = self.sonar(prs=self.pr(self.old, commit={"sha": self.head}), gate=ok)
        self.assertIn("matched by commit", out)
        self.assertEqual(code, 0)

    def test_result_for_older_commit_goes_stale_when_head_moves(self):
        ok = {"status": "OK", "conditions": []}
        self.sonar(prs=self.pr(self.fresh), gate=ok)
        self.commit("src/app.py", "def page(n):\n    return 1\n")
        self.assertIn("sonar_stale", self.codes(self.rid))
        # The server still holds the analysis of the earlier commit, finished "just now".
        # Its timestamp looks fresh; the recorded revision says it is not.
        code, out = self.sonar(prs=self.pr(self.fresh), gate=ok, analyses=[{"key": "A1", "revision": self.head}])
        self.assertEqual(code, 3)
        self.assertIn("sonar: stale. The last analysis is of another commit", out)
        # A commit this run never saw (pushed, then amended away) is just as stale.
        code, out = self.sonar(prs=self.pr(self.fresh), gate=ok, analyses=[{"key": "A0", "revision": "e" * 40}])
        self.assertIn("sonar: stale", out)
        new_head = self.git(self.wt, "rev-parse", "HEAD")
        code, out = self.sonar(prs=self.pr(self.old), gate=ok, analyses=[{"key": "A2", "revision": new_head}])
        self.assertIn("matched by commit", out)

    def test_time_based_freshness_is_an_explicit_choice(self):
        ok = {"status": "OK", "conditions": []}
        merge_ref = [{"key": "A1", "revision": "d" * 40}]           # merged-results pipelines analyse a merge commit
        self.assertIn("sonar: stale", self.sonar(prs=self.pr(self.fresh), gate=ok, analyses=merge_ref)[1])
        text = (self.control / "registry.yml").read_text()
        (self.control / "registry.yml").write_text(text.replace("product: server}", "product: server, freshness: time}"))
        self.ok("run", "repin", self.rid)
        self.assertIn("matched by analysis time", self.sonar(prs=self.pr(self.fresh), gate=ok, analyses=merge_ref)[1])
        self.assertIn("sonar: stale", self.sonar(prs=self.pr(self.old), gate=ok, analyses=merge_ref)[1])

    def test_issues_become_findings_and_close_when_sonar_stops_reporting_them(self):
        ok = {"status": "OK", "conditions": []}
        issues = [
            {"key": "AX1", "rule": "python:S2077", "severity": "BLOCKER", "type": "VULNERABILITY",
             "component": "demo:src/app.py", "line": 2, "message": "SQL built from user input"},
            {"key": "AX2", "rule": "python:S1481", "component": "demo:src/app.py",
             "impacts": [{"softwareQuality": "MAINTAINABILITY", "severity": "LOW"}], "message": "Unused variable"},
            {"key": "AX3", "rule": "python:S5", "component": "demo:src/app.py", "message": "No severity given"},
        ]
        code, out = self.sonar(prs=self.pr(self.fresh), gate=ok, issues=issues, old_server=True)
        self.assertIn("3 open issue(s)", out)
        searches = [q for path, q in FakeSonar.state["requests"] if path == "/api/issues/search"]
        self.assertIn("components", searches[0])                 # current parameter first
        self.assertIn("componentKeys", searches[1])              # then the one older servers know
        self.assertEqual(searches[1]["pullRequest"], "12")
        fnd = self.findings(self.rid)
        self.assertEqual((fnd["sonar-AX1"]["severity"], fnd["sonar-AX1"]["category"]), ("BLOCKER", "security"))
        self.assertEqual(fnd["sonar-AX1"]["location"], {"path": "src/app.py", "start_line": 2, "end_line": None})
        self.assertEqual((fnd["sonar-AX2"]["severity"], fnd["sonar-AX2"]["category"]), ("MINOR", "structure"))
        self.assertEqual(fnd["sonar-AX3"]["severity"], "MAJOR")  # unknown severity is not guessed low
        self.assertIn("unverified", self.codes(self.rid))        # a passing gate does not hide an unverified blocker

        # An agent cannot close a SonarQube finding by saying so.
        self.ok("fix", "round", self.rid, "--phase", "mr")
        c2 = self.commit("src/app.py", "def page(n):\n    return 1\n")
        self.ok("run", "sync", self.rid)
        code, out = self.cq("findings", "status", self.rid, "sonar-AX1", "fixed", "--commit", c2, "--reason", "done")
        self.assertEqual(code, 4)
        self.assertIn("closes when SonarQube stops reporting it", out)

        self.sonar(prs=self.pr(self.fresh), gate=ok, issues=issues[1:])
        self.assertEqual(self.findings(self.rid)["sonar-AX1"]["status"], "fixed")
        self.sonar(prs=self.pr(self.fresh), gate=ok, issues=issues)          # it is back
        self.assertEqual(self.findings(self.rid)["sonar-AX1"]["status"], "unverified")

    def test_issue_dropped_by_sonar_without_a_new_commit_does_not_stall_the_run(self):
        ok = {"status": "OK", "conditions": []}
        issue = {"key": "AX9", "rule": "r", "severity": "BLOCKER", "component": "demo:src/app.py", "line": 1, "message": "m"}
        self.sonar(prs=self.pr(self.fresh), gate=ok, issues=[issue])
        self.assertIn("unverified", self.codes(self.rid))
        self.sonar(prs=self.pr(self.fresh), gate=ok, issues=[])      # marked false positive in SonarQube by a person
        codes = self.codes(self.rid)
        self.assertNotIn("fix_unknown", codes)
        self.assertNotIn("fix_not_rereviewed", codes)

    def test_many_issues_are_paged(self):
        issues = [{"key": "K%d" % i, "rule": "r", "severity": "MINOR", "component": "demo:a.py", "message": "m"} for i in range(1100)]
        code, out = self.sonar(prs=self.pr(self.fresh), gate={"status": "OK", "conditions": []}, issues=issues)
        self.assertIn("1100 open issue(s)", out)
        self.assertEqual(len([k for k in self.findings(self.rid) if k.startswith("sonar-")]), 1100)

    def test_no_sonar_section_is_an_explicit_gap(self):
        text = (self.control / "registry.yml").read_text()
        (self.control / "registry.yml").write_text("\n".join(l for l in text.splitlines() if "sonar:" not in l) + "\n")
        self.ok("run", "repin", self.rid)
        code, out = self.cq("sonar", "gate", self.rid)
        self.assertEqual(code, 4)
        self.assertIn("not_applicable", out)
        self.assertIn("sonar_missing", self.codes(self.rid))


class Adversary(Env):
    def configure(self, family="openai", output=None):
        script = self.tmp / "adv.py"
        body = output or 'Here is my review:\n```json\n{"scope": "full", "files_reviewed": ["src/app.py"], "findings": []}\n```\nDone.'
        script.write_text("import sys\nbrief = sys.stdin.read()\nassert 'hostile reviewer' in brief\nprint(%r)\n" % body)
        path = self.control / "policy" / "models.yml"
        text = path.read_text().replace(
            'adversary:\n  model: ""\n  family: ""\n  argv: []',
            'adversary:\n  model: "gpt-x"\n  family: "%s"\n  argv: ["%s", "%s"]' % (family, sys.executable, script))
        self.assertNotEqual(text, path.read_text())
        path.write_text(text)

    def test_unconfigured_adversary_is_missing_evidence(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start("--min-tier", "2")
        code, out = self.cq("adversary", "run", rid)
        self.assertEqual(code, 4)
        self.assertIn("no adversary configured", out)
        code, out = self.review(rid, "adversary")
        self.assertEqual(code, 4)                                # nobody else may submit in the adversary's name
        self.assertIn("review_missing", self.codes(rid, "local"))

    def test_external_reviewer_is_stamped_from_config(self):
        self.configure()
        self.commit("src/app.py", "x = 1\n")
        rid = self.start("--min-tier", "2")
        self.assertIn("accepted: adversary full review", self.ok("adversary", "run", rid))
        env = store.find_run(rid).envelopes()[0]
        self.assertEqual((env["source"]["reviewer"], env["source"]["family"], env["source"]["model"]), ("adversary", "openai", "gpt-x"))
        self.assertNotIn("adversary_not_independent", self.codes(rid, "local"))

    def test_adversary_fix_diff_continues_from_its_last_review(self):
        self.configure()
        first = self.commit("src/app.py", "x = 1\n")
        rid = self.start("--min-tier", "2")
        code, out = self.cq("adversary", "run", rid, "--scope", "fix_diff")
        self.assertEqual(code, 4)                                # it has no earlier review to continue from
        self.ok("adversary", "run", rid)
        self.commit("src/app.py", "x = 2\n")
        self.assertIn("accepted: adversary fix_diff review", self.ok("adversary", "run", rid, "--scope", "fix_diff"))
        fix = [e for e in store.find_run(rid).envelopes() if e["scope"] == "fix_diff"][0]
        self.assertEqual(fix["reviewed_from"], first)

    def test_same_family_adversary_does_not_count(self):
        self.configure(family="anthropic")
        self.commit("src/app.py", "x = 1\n")
        rid = self.start("--min-tier", "2")
        self.ok("adversary", "run", rid)
        self.assertIn("adversary_not_independent", self.codes(rid, "local"))
        self.assertIn("must differ", self.cq("doctor")[1])

    def test_cli_that_echoes_the_brief_before_answering_is_understood(self):
        self.configure()
        script = self.tmp / "adv.py"
        script.write_text(
            "import sys\nbrief = sys.stdin.read()\nprint(brief)\n"
            "print('{\"scope\": \"full\", \"files_reviewed\": [\"src/app.py\"], \"findings\": []}')\n")
        self.commit("src/app.py", "x = 1\n")
        rid = self.start("--min-tier", "2")
        self.assertIn("accepted: adversary full review", self.ok("adversary", "run", rid))

    def test_garbage_from_the_external_cli_is_refused(self):
        self.configure(output="I could not review this.")
        self.commit("src/app.py", "x = 1\n")
        rid = self.start("--min-tier", "2")
        code, out = self.cq("adversary", "run", rid)
        self.assertEqual(code, 4)
        self.assertEqual(store.find_run(rid).envelopes(), [])


class FullRun(Env):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(gitlab.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def finish_external(self, rid):
        head = self.git(self.wt, "rev-parse", "HEAD")
        self.git(self.wt, "push", "-q", "-u", "origin", "feature/x")
        self.set_glab(head=head)
        self.ok("mr", "ensure", rid)
        self.cq("duo", "request", rid)
        self.backdate_duo_request(rid)
        self.set_glab(discussions=[{"id": "s", "individual_note": True, "notes": [{
            "id": 5, "body": "No issues found.", "author": {"username": "GitLabDuo"}, "system": False,
            "resolvable": False, "created_at": ago(300)}]}])
        fresh = (datetime.datetime.now(UTC) + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        FakeSonar.state = {"prs": [{"key": "12", "analysisDate": fresh}], "gate": {"status": "OK", "conditions": []}}
        self.assertEqual(self.cq("duo", "ingest", rid)[0], 0)
        self.assertEqual(self.cq("sonar", "gate", rid)[0], 0)

    def test_tier_1_from_branch_to_pass_and_published_report(self):
        self.commit("src/app.py", "def page(n):\n    return max(n, 1)\n")
        rid = self.start("--ticket", "SHOP-7")
        self.assertEqual(self.verdict(rid)["verdict"], "INCONCLUSIVE")
        self.ok("check", "run", rid, "--required")
        self.panel(rid)
        self.assertEqual(self.verdict(rid, "local")["verdict"], "PASS")
        self.assertEqual(self.verdict(rid)["verdict"], "INCONCLUSIVE")      # no Duo or SonarQube evidence yet
        self.finish_external(rid)
        d = self.verdict(rid)
        self.assertEqual((d["verdict"], d["ready_to_merge"]), ("PASS", True))

        # The report goes to the top of the description; other text is kept.
        state = self.glab()
        state["mrs"][0]["description"] += "\n\nReviewer notes written by a person."
        self.glab_state.write_text(json.dumps(state))
        self.ok("mr", "describe", rid)
        self.ok("mr", "describe", rid)
        desc = self.glab()["mrs"][0]["description"]
        self.assertEqual(desc.count("## Quality run: PASS"), 1)
        self.assertTrue(desc.startswith(gitlab.REPORT_START))
        self.assertIn("Reviewer notes written by a person.", desc)
        self.assertIn("| check: unit | passed |", desc)
        self.assertIn("Approve from the report", desc)

    def test_tier_3_waits_for_the_human_and_approvals_are_scoped(self):
        text = (self.control / "registry.yml").read_text()
        (self.control / "registry.yml").write_text(text + (
            "    not_applicable: {integration: 'none yet', mutation: 'none yet', e2e: 'none yet', rollback_test: 'none yet',\n"
            "                     tests: 'x', security: 'x', adversary: 'x', adversary2: 'x', final_check: 'x'}\n"))
        self.commit("src/pricing/calc.py", "RATE = 2\n")
        rid = self.start()
        run = store.find_run(rid)
        self.assertEqual(run.data["tier"], 3)
        self.ok("check", "run", rid, "--required")
        self.panel(rid)
        self.write(self.tmp, "plan_review.md", "The plan looks fine.\n")
        code, out = self.cq("note", "add", rid, "plan_review", self.tmp / "plan_review.md")
        self.assertEqual(code, 4)                                    # a review needs a plan and an outcome line
        for name, text in (("plan", "plan text\n"), ("plan_review", "PLAN NEEDS CHANGES\n1. No rollback.\n"), ("rollback", "revert\n")):
            self.write(self.tmp, name + ".md", text)
            self.ok("note", "add", rid, name, self.tmp / (name + ".md"))
        self.assertIn("plan_review_negative", self.codes(rid, "local"))
        self.write(self.tmp, "plan_review.md", "PLAN OK\n")
        self.ok("note", "add", rid, "plan_review", self.tmp / "plan_review.md")
        self.assertEqual(self.codes(rid, "local"), {"plan_approval_missing"})
        runs.record_approval(run, "plan_approval", "Brett")
        self.assertEqual(self.verdict(rid, "local")["verdict"], "PASS")
        self.finish_external(rid)
        d = self.verdict(rid)
        self.assertEqual((d["verdict"], {r["code"] for r in d["reasons"]}), ("BLOCKED", {"final_signoff_missing"}))
        self.assertEqual(len(d["not_applicable"]), 9)                     # every waived requirement is listed
        runs.sync(run)
        runs.record_approval(run, "final_signoff", "Brett")
        self.assertEqual(self.verdict(rid)["verdict"], "PASS")

        # A changed plan needs a new approval. A new commit needs a new sign-off.
        self.write(self.tmp, "plan.md", "a different plan\n")
        self.ok("note", "add", rid, "plan", self.tmp / "plan.md")
        self.assertTrue({"plan_approval_missing", "plan_review_stale"} <= self.codes(rid))
        self.ok("note", "add", rid, "plan_review", self.tmp / "plan_review.md")
        runs.record_approval(run, "plan_approval", "Brett")
        self.commit("src/pricing/calc.py", "RATE = 3\n")
        codes = self.codes(rid)
        self.assertIn("check_stale", codes)
        self.ok("check", "run", rid, "--required")
        for r in ("simplicity", "structure", "standards_spec"):
            self.review(rid, r)
        self.finish_external(rid)
        self.assertEqual(self.codes(rid), {"final_signoff_missing"})

    def test_waivers_listing(self):
        self.commit("src/app.py", "x = 1\n")
        rid = self.start()
        code, out = self.review(rid, "structure", [dict(FINDING, severity="MAJOR")])
        fid = out.strip().split(": ")[-1]
        run = store.find_run(rid)
        from cq import findings as F
        fnd = run.findings()
        F.decide(fnd, run.policy()["floor"], 1, fid, "waive", "human:Brett", "ship it", 10, "SHOP-3", datetime.datetime(2020, 1, 1, tzinfo=UTC))
        run.save_findings(fnd)
        self.assertIn("EXPIRED 2020-01-11", self.ok("waivers", "--expired"))
        self.assertIn("waiver_expired", self.codes(rid, "local"))


if __name__ == "__main__":
    unittest.main()
