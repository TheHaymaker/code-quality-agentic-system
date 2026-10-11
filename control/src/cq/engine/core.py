"""The run engine.

One run goes: intake -> plan -> implement -> evidence loop -> merge request ->
evidence loop -> decide. The evidence loop asks the policy what is missing and
does the one thing that supplies it: run a check, start a review, triage,
verify, fix, collect Duo or SonarQube results.

Where a run stands is worked out from its evidence each time, not from a saved
step pointer. That is what makes `resume` safe after a crash: a step whose
evidence exists is not done again.

Agents never change state themselves. They answer in JSON; the engine applies
each answer through the same functions the `cq` CLI uses, and those refuse
anything the rules do not allow.
"""
from __future__ import annotations

import concurrent.futures as cf
import contextlib
import copy
import fcntl
import json
import re
import shlex
import shutil
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import briefs, config, findings as F, gitlab, policy, reviews, runs, sonar, store, verdicts
from ..util import CqError, git, git_ok, iso, now, parse_time, read_json, sha256_text, write_json
from . import agents, prompts, settings as engine_settings, tracker as trackers

RUNNING, WAITING, NEEDS_HUMAN, COMPLETED, FAILED, CANCELLED = (
    "RUNNING", "WAITING", "NEEDS_HUMAN", "COMPLETED", "FAILED", "CANCELLED")
FINISHED = (COMPLETED, CANCELLED)
EXIT_CODES = {COMPLETED: 0, CANCELLED: 0, FAILED: 1, NEEDS_HUMAN: 2, WAITING: 3, RUNNING: 3}

WAITING_CODES = {"duo_missing", "duo_stale", "duo_pending", "duo_not_requested", "duo_error",
                 "sonar_missing", "sonar_stale", "sonar_error"}
REVIEW_CODES = {"review_missing", "review_too_old", "review_stale", "fix_not_rereviewed"}
CHECK_GATHER = {"check_missing", "check_stale", "check_error"}


class Stop(Exception):
    """End the drive: the run waits, needs a human, failed, or was cancelled."""

    def __init__(self, state: str, reason: str, escalate: bool = False, key: Optional[str] = None):
        super().__init__(reason)
        self.state, self.reason, self.escalate = state, reason, escalate
        self.key = key  # for WAITING: what the run waits for, so a long wait can go to a human


def _intake_dir() -> Path:
    return config.cq_home() / "intake"


@contextlib.contextmanager
def _exclusive(path: Path, what: str) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for attempt in range(5):
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == 4:
                    raise CqError("the engine is already working on %s" % what)
                time.sleep(0.2)  # `cq engine status` may be looking at the lock this moment
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def engine_alive(run: store.Run) -> bool:
    """True while an engine process holds this run's lock. A run whose record
    says RUNNING with no live engine crashed or was killed."""
    path = run.dir / "engine.lock"
    if not path.exists():
        return False
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def live_state(run: store.Run, rec: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """The state to show: a RUNNING record with no engine behind it shows as FAILED."""
    state, reason = rec.get("state"), rec.get("reason")
    if state == RUNNING and not engine_alive(run):
        return FAILED, ("The engine stopped while this run was RUNNING (it crashed or was killed). "
                        "Resume it: cq engine resume %s" % run.dir.name)
    return state, reason


class Engine:
    def __init__(self, cfg: Optional[Dict[str, Any]] = None, tracker: Optional[trackers.Tracker] = None,
                 runner: Optional[agents.ClaudeRunner] = None):
        self.cfg = cfg or engine_settings.load()
        self.tracker = tracker or trackers.make(self.cfg)
        self.runner = runner or agents.ClaudeRunner(self.cfg)
        self.labels = self.cfg["labels"]

    # == engine record ========================================================

    @staticmethod
    def record(run: store.Run) -> Dict[str, Any]:
        return read_json(run.dir / "engine.json", {})

    @staticmethod
    def _save(run: store.Run, rec: Dict[str, Any]) -> None:
        rec["updated_at"] = iso()
        write_json(run.dir / "engine.json", rec)

    def _update(self, run: store.Run, **fields: Any) -> Dict[str, Any]:
        with run.lock():
            rec = self.record(run)
            rec.update(fields)
            self._save(run, rec)
            return rec

    def _state(self, run: store.Run, state: str, stage: str, reason: str) -> Dict[str, Any]:
        with run.lock():
            rec = self.record(run)
            changed = (rec.get("state"), rec.get("stage"), rec.get("reason")) != (state, stage, reason)
            rec.update(state=state, stage=stage, reason=reason)
            if changed:
                rec.setdefault("history", []).append({"at": iso(), "state": state, "stage": stage, "reason": reason})
                run.event("engine_state", {"state": state, "stage": stage, "reason": reason}, "engine")
            self._save(run, rec)
            return rec

    def _cost(self, run: store.Run, role: str, amount: float) -> None:
        with run.lock():
            rec = self.record(run)
            rec["cost_usd"] = round(rec.get("cost_usd", 0.0) + amount, 4)
            by = rec.setdefault("cost_by_role", {})
            by[role] = round(by.get(role, 0.0) + amount, 4)
            rec["sessions"] = rec.get("sessions", 0) + 1
            self._save(run, rec)

    def _count(self, run: store.Run, key: str, limit: int, what: str) -> None:
        """Count an attempt within this drive. Past the limit the run goes to a
        human instead of looping. A resume starts the count again, so a human who
        fixed the cause can continue."""
        n = self._attempts[key] = self._attempts.get(key, 0) + 1
        if n > limit:
            raise Stop(NEEDS_HUMAN, "%s did not work after %d attempts." % (what, limit), escalate=True)

    def _note(self, run: store.Run, name: str, text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write(text)
        try:
            runs.add_note(run, name, Path(fh.name), "engine")
        finally:
            Path(fh.name).unlink()

    @staticmethod
    def _note_text(run: store.Run, name: str) -> str:
        note = run.notes().get(name)
        return Path(note["path"]).read_text(encoding="utf-8") if note else ""

    # == agents ===============================================================

    def _agent(self, run: store.Run, role: str, model_role: str, *, prompt: str, stdin: str, cwd: Path,
               mode: str, schema: Dict[str, Any], **system_kw: Any) -> Dict[str, Any]:
        pol = run.policy()
        spec = config.model_for(pol["models"], model_role, run.data["tier"])
        worktree = Path(run.data["worktree"])
        before = git(worktree, "rev-parse", "HEAD") if mode == "write" and cwd == worktree else None
        try:
            result = self.runner.run(
                run.dir, role, spec, system=prompts.system(model_role, **system_kw),
                prompt=prompt, stdin=stdin, cwd=cwd, mode=mode, schema=schema, checks=pol["repo"]["checks"],
            )
        except agents.AgentError as exc:
            self._cost(run, role, exc.cost_usd)
            raise
        finally:
            if before:
                self._check_history(run, role, worktree, before)
        self._cost(run, role, result["cost_usd"])
        if result.get("models") and not any(spec["model"] in m or m in spec["model"] for m in result["models"]):
            run.event("engine_model_mismatch", {"role": role, "asked": spec["model"], "ran": result["models"]}, "engine")
        return result["output"]

    @staticmethod
    def _check_history(run: store.Run, role: str, worktree: Path, before: str) -> None:
        """A writing agent adds commits. It never rewrites the ones already reviewed."""
        if not git_ok(worktree, "merge-base", "--is-ancestor", before, "HEAD"):
            run.event("engine_history_rewritten", {"role": role, "before": before}, "engine")
            raise Stop(FAILED, "The %s session rewrote history: %s is no longer an ancestor of HEAD. "
                               "Restore the branch (git reset --hard %s), then resume." % (role, before[:12], before[:12]))

    def _ticket_block(self, run: store.Run) -> str:
        rec = self.record(run)
        return "# Ticket %s: %s\n\n%s\n" % (run.data["ticket"], rec.get("summary", ""), self._note_text(run, "spec"))

    # == entry points =========================================================

    def start(self, key: str, repo_id: Optional[str] = None, from_queue: bool = False) -> Dict[str, Any]:
        """Take a ticket from intake to a decided run, or as far as it can go."""
        for existing in store.list_runs():
            d = existing.data
            if d["ticket"] == key and d["status"] == "active":
                return self.resume(d["run_id"])
        with _exclusive(_intake_dir() / ("%s.lock" % key), key):
            ticket = self.tracker.get(key)
            control = config.load_control(config.control_dir())
            repo = self._repo_for(ticket, control, repo_id)
            try:
                screened = self._screen(ticket, repo, from_queue)
            except Stop as stop:
                return {"ticket": key, "run_id": None, "state": stop.state, "reason": stop.reason}
            worktree = self._workspace(repo, key)
            run, _ = runs.start(repo["id"], worktree, key, screened["tier"], False)
            with run.lock():
                rec = self.record(run)
                if not rec:
                    rec = {"ticket": key, "summary": ticket["summary"], "ticket_url": ticket.get("url"),
                           "project": ticket["project"], "started_at": iso(), "history": [],
                           "cost_usd": screened["cost_usd"], "cost_by_role": {"screener": screened["cost_usd"]},
                           "screening_score": screened["score"], "screening_result": screened["result"]}
                    self._save(run, rec)
            if "spec" not in run.notes():
                self._note(run, "spec", self._spec_text(screened))
            self.tracker.labels(key, add=[self.labels["in_progress"]],
                                remove=[self.labels["ready"], self.labels["needs_confirmation"]])
        return self._drive(run)

    def resume(self, run_id: str) -> Dict[str, Any]:
        run = store.find_run(run_id)
        if run.data["status"] != "active":
            raise CqError("run %s is closed" % run.dir.name)
        rec = self.record(run)
        if not rec:
            raise CqError("run %s was not started by the engine" % run.dir.name)
        if rec.get("state") == CANCELLED:
            raise CqError("run %s was cancelled" % run.dir.name)
        return self._drive(run)

    def resume_waiting(self) -> List[Dict[str, Any]]:
        out = []
        for run in store.list_runs():
            rec = self.record(run)
            if rec.get("state") == WAITING and run.data["status"] == "active":
                try:
                    out.append(self.resume(run.dir.name))
                except CqError as exc:
                    out.append({"run_id": run.dir.name, "state": "SKIPPED", "reason": str(exc)})
        return out

    def pull(self, limit: int) -> List[Dict[str, Any]]:
        """Start runs for ready tickets, up to the work-in-progress limit."""
        control = config.load_control(config.control_dir())
        projects = sorted({p for r in control["registry"]["repositories"] for p in r.get("jira_projects", [])})
        open_runs = [r for r in store.list_runs()
                     if r.data["status"] == "active" and self.record(r) and self.record(r).get("state") not in FINISHED]
        slots = max(0, self.cfg["wip_limit"] - len(open_runs))
        if not slots:
            return []
        busy = [self.labels[k] for k in ("in_progress", "needs_human", "needs_confirmation", "done")]
        tickets = self.tracker.search(projects, self.labels["ready"], busy)
        # A ticket that waited for confirmation comes back when you add the confirmed label.
        seen = {t["key"] for t in tickets}
        confirmed = self.tracker.search(projects, self.labels["confirmed"], [self.labels[k] for k in ("in_progress", "needs_human", "done")])
        tickets += [t for t in confirmed if self.labels["needs_confirmation"] in t["labels"] and t["key"] not in seen]
        out = []
        for ticket in tickets[:min(slots, limit)]:
            try:
                out.append(self.start(ticket["key"], from_queue=True))
            except CqError as exc:
                out.append({"ticket": ticket["key"], "run_id": None, "state": FAILED, "reason": str(exc)})
        return out

    def cancel(self, run_id: str) -> Dict[str, Any]:
        run = store.find_run(run_id)
        (run.dir / "cancel.flag").write_text(iso())
        rec = self.record(run)
        if rec.get("state") != RUNNING or not engine_alive(run):
            rec = self._state(run, CANCELLED, rec.get("stage", "-"), "Cancelled. Branch and merge request are kept.")
            self._ticket_labels(run, remove=[self.labels["in_progress"]])
        return {"run_id": run.dir.name, "state": rec["state"], "reason": rec.get("reason")}

    # == intake ===============================================================

    def _repo_for(self, ticket: Dict[str, Any], control: Dict[str, Any], repo_id: Optional[str]) -> Dict[str, Any]:
        if repo_id:
            return config.repo_entry(control["registry"], repo_id)
        candidates = [r for r in control["registry"]["repositories"] if ticket["project"] in r.get("jira_projects", [])]
        labelled = [r for r in candidates if "repo-%s" % r["id"] in ticket["labels"]]
        chosen = labelled or candidates
        if len(chosen) != 1:
            raise CqError(
                "cannot tell which repository %s belongs to (%d match project %s). Pass --repo, or label the ticket repo-<id>."
                % (ticket["key"], len(chosen), ticket["project"]))
        return config.repo_entry(control["registry"], chosen[0]["id"])

    @staticmethod
    def _spec_text(screened: Dict[str, Any]) -> str:
        lines = [screened["spec"].strip(), "", "## Acceptance criteria", ""]
        lines += ["%d. %s" % (i + 1, c) for i, c in enumerate(screened["acceptance_criteria"])] or ["(none)"]
        if screened["assumptions"]:
            lines += ["", "## Assumptions", ""] + ["- %s" % a for a in screened["assumptions"]]
        return "\n".join(lines) + "\n"

    def _screen(self, ticket: Dict[str, Any], repo: Dict[str, Any], from_queue: bool) -> Dict[str, Any]:
        key = ticket["key"]
        path = _intake_dir() / ("%s.json" % key)
        intake = read_json(path, {})
        digest = sha256_text(ticket["summary"] + "\x00" + ticket["description"])
        if intake.get("digest") == digest and intake.get("screening"):
            answer = intake["screening"]
        else:
            stdin = "# Ticket %s: %s\n\n%s\n" % (key, ticket["summary"], ticket["description"])
            spec = config.model_for(config.load_control(config.control_dir())["models"], "screener", 0)
            result = self.runner.run(
                path.parent / key, "screener", spec, system=prompts.system("screener"),
                prompt="Screen the ticket in your input.", stdin=stdin, cwd=Path(repo["path"]).expanduser(),
                mode="read", schema=prompts.SCREEN, checks={})
            answer = result["output"]
            intake = {"key": key, "digest": digest, "screening": answer, "cost_usd": result["cost_usd"], "at": iso(),
                      "commented": intake.get("commented")}
            write_json(path, intake)
        score = sum(1 for c in prompts.CHECKS if answer["checks"][c]["pass"])
        if answer["paths"]:
            estimate = runs.estimate_tier(repo["id"], answer["paths"])["tier"]
        else:
            # No paths named: nothing says the work is low risk.
            estimate = config.load_control(config.control_dir())["floor"]["default_tier"]
        floors = [int(m.group(1)) for l in ticket["labels"] for m in [re.match(r"^(?:tier|risk)-([0-3])$", l)] if m]
        tier = max([estimate] + floors)
        summary = "\n".join(
            ["cq screening: %s, %d of 6 checks passed before any rewrite. Estimated tier %d." % (answer["result"], score, tier), ""]
            + ["- %s: %s. %s" % (c, "pass" if answer["checks"][c]["pass"] else "FAIL", answer["checks"][c]["note"]) for c in prompts.CHECKS])

        def stop(label: str, text: str, reason: str) -> None:
            if intake.get("commented") != digest:
                self.tracker.comment(key, summary + "\n\n" + text)
                intake["commented"] = digest
                write_json(path, intake)
            self.tracker.labels(key, add=[label], remove=[self.labels["ready"], self.labels["in_progress"]])
            raise Stop(NEEDS_HUMAN, reason)

        if answer["result"] == "needs_human":
            stop(self.labels["needs_human"], "Question: %s" % (answer["question"] or "the ticket is not clear enough."),
                 "The ticket needs an answer: %s" % (answer["question"] or "see the Jira comment"))
        if answer["assumptions"] and tier >= 2 and self.labels["confirmed"] not in ticket["labels"]:
            stop(self.labels["needs_confirmation"],
                 "Assumptions to confirm (add the label %s to accept them):\n%s" % (
                     self.labels["confirmed"], "\n".join("- %s" % a for a in answer["assumptions"])),
                 "Tier %d work on assumptions waits for your confirmation." % tier)
        if from_queue and tier >= 3:
            stop(self.labels["needs_human"], "Tier 3 work is never started from the queue. Start it with `cq engine run %s`." % key,
                 "Tier 3: start it yourself.")
        if intake.get("commented") != digest:
            self.tracker.comment(key, summary + "\n\nStarting work.")
            intake["commented"] = digest
            write_json(path, intake)
        return dict(answer, score=score, tier=tier, cost_usd=intake.get("cost_usd", 0.0))

    def _workspace(self, repo: Dict[str, Any], key: str) -> Path:
        main = Path(repo["path"]).expanduser().resolve()
        remote, default = repo["remote"], repo["default_branch"]
        git(main, "fetch", "--quiet", remote)
        branch = self.cfg["branch_prefix"] + key.lower()
        worktree = Path(self.cfg["worktrees_dir"]) / repo["id"] / key
        if worktree.exists():
            if git(worktree, "branch", "--show-current", check=False) != branch:
                raise CqError("%s exists and is not on branch %s" % (worktree, branch))
            return worktree.resolve()
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if git_ok(main, "rev-parse", "--verify", "--quiet", "refs/heads/%s" % branch):
            git(main, "worktree", "add", "--quiet", str(worktree), branch)
        elif git_ok(main, "rev-parse", "--verify", "--quiet", "refs/remotes/%s/%s" % (remote, branch)):
            git(main, "worktree", "add", "--quiet", "-b", branch, str(worktree), "%s/%s" % (remote, branch))
        else:
            git(main, "worktree", "add", "--quiet", "-b", branch, str(worktree), "%s/%s" % (remote, default))
        return worktree.resolve()

    # == the drive ============================================================

    def _drive(self, run: store.Run) -> Dict[str, Any]:
        with _exclusive(run.dir / "engine.lock", run.dir.name):
            self._attempts: Dict[str, int] = {}
            try:
                self._check_cancel(run)
                self._state(run, RUNNING, "plan", "")
                self._plan(run)
                self._state(run, RUNNING, "implement", "")
                self._implement(run)
                self._state(run, RUNNING, "local", "")
                self._converge(run, "local")
                # Decided again: a fix round that was refused has just recorded the spent budget.
                verdict, reasons = self._effective(verdicts.decide(run, "local"), "local")
                self._state(run, RUNNING, "publish", "")
                self._publish(run)
                if verdict != policy.PASS:
                    # The local loop could not settle it (budget spent, a contested
                    # finding, a failure no agent can fix). Outside reviews would not
                    # change that: the draft goes to a human now.
                    return self._decide(run, early=reasons)
                self._state(run, RUNNING, "merge_request", "")
                self._converge(run, "mr")
                return self._decide(run)
            except Stop as stop:
                return self._finish(run, stop)
            except agents.AgentError as exc:
                return self._finish(run, Stop(FAILED, str(exc)))
            except CqError as exc:
                return self._finish(run, Stop(FAILED, str(exc)))
            except Exception as exc:  # a bug must not leave the run RUNNING
                run.event("engine_internal_error", {"error": repr(exc), "trace": traceback.format_exc()[-6000:]}, "engine")
                return self._finish(run, Stop(FAILED, "Internal error: %r. The trace is in the run's events." % exc))

    def _check_cancel(self, run: store.Run) -> None:
        if (run.dir / "cancel.flag").exists():
            raise Stop(CANCELLED, "Cancelled. Branch and merge request are kept.")

    def _finish(self, run: store.Run, stop: Stop, stage: Optional[str] = None) -> Dict[str, Any]:
        stage = stage or self.record(run).get("stage", "-")
        try:
            if stop.state == WAITING:
                stop = self._bounded_wait(run, stop)
            if stop.escalate:
                self._escalate(run, [stop.reason])
            if stop.state in (CANCELLED,):
                self._ticket_labels(run, remove=[self.labels["in_progress"]])
        except Exception as exc:  # the state is recorded whatever happens here
            run.event("engine_internal_error", {"error": repr(exc), "trace": traceback.format_exc()[-6000:]}, "engine")
        rec = self._state(run, stop.state, stage, stop.reason)
        return self._outcome(run, rec)

    def _bounded_wait(self, run: store.Run, stop: Stop) -> Stop:
        """A run may wait on the same thing for the same commit only so long. Then a human looks."""
        key = "%s@%s" % (stop.key or "wait", run.data.get("head_sha"))
        with run.lock():
            rec = self.record(run)
            since = rec.setdefault("waits_since", {}).setdefault(key, iso())
            self._save(run, rec)
        limit = self.cfg["waits"].get("escalate_after_seconds") or 0
        waited = (now() - parse_time(since)).total_seconds()
        if not limit or waited < limit:
            return stop
        with run.lock():
            rec = self.record(run)
            rec.get("waits_since", {}).pop(key, None)  # a resume after this waits a full period again
            self._save(run, rec)
        return Stop(NEEDS_HUMAN, "Waited %.1f hours with no change: %s" % (waited / 3600, stop.reason), escalate=True)

    def _outcome(self, run: store.Run, rec: Dict[str, Any]) -> Dict[str, Any]:
        mr = run.data.get("mr") or {}
        return {"ticket": run.data["ticket"], "run_id": run.dir.name, "state": rec["state"], "stage": rec.get("stage"),
                "reason": rec.get("reason"), "merge_request": mr.get("web_url"), "cost_usd": rec.get("cost_usd", 0.0),
                "escalation": rec.get("escalation")}

    # == plan =================================================================

    def _gate(self, run: store.Run) -> Dict[str, Any]:
        data = runs.sync(run)
        pol = run.policy()
        gate = dict(pol["floor"]["gates"][str(policy.gate_tier(data))])
        na = pol["repo"]["not_applicable"]
        for kind in ("checks", "reviewers", "external", "notes"):
            gate[kind] = [n for n in gate[kind] if n not in na]
        return gate

    def _plan(self, run: store.Run) -> bool:
        """Make sure the plan evidence the tier needs is on file. Returns True if it did anything."""
        did = False
        gate = self._gate(run)
        worktree = Path(run.data["worktree"])
        if "plan" not in run.notes():
            self._write_plan(run, worktree, problems="")
            did = True
        if "plan_review" in gate["notes"]:
            for _ in range(5):
                notes = run.notes()
                review = notes.get("plan_review")
                if review is None or review.get("plan_sha256") != notes["plan"]["sha256"]:
                    self._review_plan(run, worktree)
                    did = True
                    continue
                if review.get("outcome") == "ok":
                    break
                self._count(run, "plan_revision", 1, "Revising the plan after review")
                self._write_plan(run, worktree, problems=self._note_text(run, "plan_review"))
                did = True
            else:
                raise Stop(NEEDS_HUMAN, "The plan review still asks for changes.", escalate=True)
        if "rollback" in gate["notes"] and "rollback" not in run.notes():
            raise Stop(NEEDS_HUMAN, "No rollback note is on file.", escalate=True)
        if "plan_approval" in gate["approvals"]:
            plan = run.notes()["plan"]
            if not any(a["kind"] == "plan_approval" and a.get("plan_sha256") == plan["sha256"] for a in run.approvals()):
                rec = self.record(run)
                late = bool(rec.get("implemented"))
                if rec.get("plan_asked") != plan["sha256"]:
                    intro = ("The change now touches tier 3 paths, so its plan needs your approval although the code "
                             "already exists. Read the plan and the diff." if late else "The plan is ready for your approval.")
                    self._ticket_comment(run, "Run %s: %s\n\nPlan: %s\n\n"
                                         "Approve: cq approve %s plan_approval\nThen: cq engine resume %s"
                                         % (run.dir.name, intro, plan["path"], run.dir.name, run.dir.name))
                    self._update(run, plan_asked=plan["sha256"])
                if late:
                    raise Stop(NEEDS_HUMAN, "The change became tier 3 after the code was written. Approve the plan "
                                            "after you read the diff: cq approve %s plan_approval" % run.dir.name)
                raise Stop(NEEDS_HUMAN, "Waiting for your plan approval: cq approve %s plan_approval" % run.dir.name)
        return did

    def _write_plan(self, run: store.Run, worktree: Path, problems: str) -> None:
        stdin = self._ticket_block(run)
        if problems:
            stdin += "\n# The previous plan\n\n%s\n\n# Problems the plan review found\n\n%s\n" % (
                self._note_text(run, "plan"), problems)
        out = self._agent(run, "planner", "planner", prompt="Write the plan for the ticket in your input.",
                          stdin=stdin, cwd=worktree, mode="read", schema=prompts.PLAN)
        self._note(run, "plan", out["plan"])
        self._note(run, "rollback", out["rollback"] or "No rollback steps given.")

    def _review_plan(self, run: store.Run, worktree: Path) -> None:
        stdin = self._ticket_block(run) + "\n# The plan\n\n%s\n\n# The rollback\n\n%s\n" % (
            self._note_text(run, "plan"), self._note_text(run, "rollback"))
        out = self._agent(run, "plan_reviewer", "plan_reviewer", prompt="Review the plan in your input.",
                          stdin=stdin, cwd=worktree, mode="read", schema=prompts.PLAN_REVIEW)
        text = out["verdict"] + "\n\n" + "\n".join(
            "%d. %s\n   Evidence: %s\n   Change: %s" % (i + 1, p["problem"], p["evidence"], p["change"])
            for i, p in enumerate(out["problems"]))
        self._note(run, "plan_review", text)

    # == implement ============================================================

    def _implement(self, run: store.Run) -> None:
        rec = self.record(run)
        worktree = Path(run.data["worktree"])
        brief = self._ticket_block(run) + "\n# The plan\n\n%s\n" % self._note_text(run, "plan")
        if not rec.get("implemented"):
            data = runs.sync(run)
            if not self._ahead(worktree, data["base_sha"]):
                self._count(run, "implement", 2, "Implementation")
                out = self._agent(run, "implementer", "implementer", prompt="Implement the ticket in your input, then commit.",
                                  stdin=brief, cwd=worktree, mode="write", schema=prompts.WORK)
                self._work_note(run, "implementation", out)
                data = runs.sync(run)
                if not self._ahead(worktree, data["base_sha"]):
                    raise Stop(FAILED, "The implementer made no commit. Resume to try again.")
            if runs.sync(run)["dirty"]:
                raise Stop(FAILED, "The implementer left uncommitted changes in %s." % worktree)
            self._update(run, implemented=True)
        if not self.record(run).get("tests_written"):
            self._count(run, "tests", 2, "Writing tests")
            out = self._agent(run, "test_author", "test_author",
                              prompt="Write tests for the acceptance criteria in your input, then commit.",
                              stdin=brief, cwd=worktree, mode="write", schema=prompts.WORK)
            self._work_note(run, "tests", out)
            if runs.sync(run)["dirty"]:
                raise Stop(FAILED, "The test author left uncommitted changes in %s." % worktree)
            self._update(run, tests_written=True)

    def _work_note(self, run: store.Run, name: str, out: Dict[str, Any]) -> None:
        """What the agent says it did, and where it left the plan. The report and a human read it."""
        text = (out.get("summary") or "(no summary)").strip() + "\n"
        if out.get("deviations"):
            text += "\n## Deviations from the plan\n\n" + "\n".join("- %s" % d for d in out["deviations"]) + "\n"
        self._note(run, name, text)

    @staticmethod
    def _ahead(worktree: Path, base: str) -> int:
        return int(git(worktree, "rev-list", "--count", "%s..HEAD" % base) or 0)

    # == the evidence loop ====================================================

    def _effective(self, d: Dict[str, Any], phase: str) -> Tuple[str, List[Dict[str, Any]]]:
        """The verdict with the final check left out until everything else is settled."""
        reasons = d["reasons"]
        def is_final(r: Dict[str, Any]) -> bool:
            return r["subject"].get("reviewer") == "final_check" and r["code"] in REVIEW_CODES
        if phase == "local":
            reasons = [r for r in reasons if not is_final(r)]
        else:
            others = [r for r in reasons if not is_final(r) and r["verdict"] != policy.BLOCKED]
            if others:
                reasons = [r for r in reasons if not is_final(r)]
        verdict = policy.PASS
        for candidate in policy.ORDER:
            if any(r["verdict"] == candidate for r in reasons):
                verdict = candidate
                break
        return verdict, reasons

    def _converge(self, run: store.Run, phase: str) -> Dict[str, Any]:
        stage = "local" if phase == "local" else "final"
        for _ in range(self.cfg["max_passes"]):
            self._check_cancel(run)
            d = verdicts.decide(run, stage)
            verdict, reasons = self._effective(d, phase)
            if verdict == policy.PASS:
                if self._needs_aggregation(run):
                    # MINOR and NIT findings do not gate, but each still gets an outcome: filed, left or fixed.
                    self._aggregate(run)
                    continue
                return d
            if self._gather(run, reasons, phase):
                continue
            if verdict == policy.FAIL:
                if not self._fix(run, reasons, phase):
                    return d  # the budget is spent; the verdict now says so
                continue
            return d
        raise Stop(NEEDS_HUMAN, "No progress after %d passes through the evidence loop." % self.cfg["max_passes"], escalate=True)

    def _gather(self, run: store.Run, reasons: List[Dict[str, Any]], phase: str) -> bool:
        """Do the first thing that supplies missing evidence. True if it did something."""
        codes = {r["code"] for r in reasons}
        if codes & {"note_missing", "plan_review_stale", "plan_review_negative", "plan_approval_missing"}:
            if self._plan(run):
                return True
        if "worktree_dirty" in codes:
            raise Stop(FAILED, "The worktree has uncommitted changes. Commit or remove them, then resume.")
        for r in reasons:
            if r["code"] == "check_unregistered":
                raise Stop(NEEDS_HUMAN, r["message"], escalate=True)
        checks = sorted({r["subject"]["check"] for r in reasons if r["code"] in CHECK_GATHER})
        if checks:
            head = run.data["head_sha"]
            for name in checks:
                if any(r["code"] == "check_error" and r["subject"]["check"] == name for r in reasons):
                    self._count(run, "check_error:%s@%s" % (name, head), 1, "Running check '%s'" % name)
                runs.run_check(run, name)
            return True
        if "check_failed" in codes:
            return False  # fix first; reviewing code that fails its checks wastes the panel
        jobs = self._reviews_needed(run, reasons, phase)
        if jobs:
            self._run_reviews(run, jobs)
            return True
        if phase == "mr" and self._collect_external(run, codes):
            return True
        if self._needs_aggregation(run) or "untriaged" in codes:
            self._aggregate(run)
            return True
        unverified = [r["subject"]["finding"] for r in reasons if r["code"] == "unverified"]
        if unverified:
            self._verify(run, unverified)
            return True
        if phase == "mr" and self._answer_duo(run):
            return True
        for r in reasons:
            if r["code"] in ("adversary_not_independent", "adversary2_same_model", "fix_unknown"):
                raise Stop(NEEDS_HUMAN, r["message"], escalate=True)
        return False

    # -- reviews --------------------------------------------------------------

    def _reviews_needed(self, run: store.Run, reasons: List[Dict[str, Any]], phase: str) -> List[Tuple[str, str]]:
        gate = self._gate(run)
        never = set(run.policy()["floor"]["review_carry"]["never_for"])
        full, fixdiff, stale = set(), set(), []
        for r in reasons:
            if r["code"] not in REVIEW_CODES:
                continue
            who = r["subject"].get("reviewer")
            if r["code"] == "fix_not_rereviewed" and who is None:
                who = "security" if "security" in gate["reviewers"] else "structure"
            if r["code"] in ("review_missing", "review_too_old"):
                full.add(who)
            elif r["code"] == "fix_not_rereviewed":
                fixdiff.add(who)
            elif who in never:
                fixdiff.add(who)
            else:
                stale.append(who)
        if stale and not (fixdiff | full):
            fixdiff.add("structure" if "structure" in stale else sorted(stale)[0])
        jobs = [(r, "full") for r in sorted(full)]
        for who in sorted(fixdiff - full):
            try:
                briefs.fix_start(run, who)
                jobs.append((who, "fix_diff"))
            except CqError:
                jobs.append((who, "full"))
        return jobs

    def _run_reviews(self, run: store.Run, jobs: List[Tuple[str, str]]) -> None:
        head = run.data["head_sha"]
        for who, scope in jobs:
            self._count(run, "review:%s:%s@%s" % (who, scope, head), 2, "The %s %s review of %s" % (who, scope, head[:12]))
        errors: List[str] = []
        with cf.ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
            futures = {pool.submit(self._review, run, who, scope): who for who, scope in jobs}
            for future in cf.as_completed(futures):
                try:
                    future.result()
                except (CqError, agents.AgentError) as exc:
                    errors.append("%s: %s" % (futures[future], exc))
                    run.event("engine_review_failed", {"reviewer": futures[future], "error": str(exc)}, "engine")
        if errors and len(errors) == len(jobs):
            # Nothing came back. The attempt counter turns a persistent failure into a stop.
            run.event("engine_reviews_failed", {"errors": errors}, "engine")

    def _review(self, run: store.Run, who: str, scope: str) -> None:
        start = briefs.fix_start(run, who) if scope == "fix_diff" else None
        head = run.data["head_sha"]
        if who in ("adversary", "adversary2"):
            submission = briefs.run_external_reviewer(run, who, scope, start)
            if isinstance(submission, dict):
                submission["scope"] = scope
                submission.pop("reviewed_from", None)
                if start:
                    submission["reviewed_from"] = start
            result = reviews.submit(run, who, head, submission)
            if not result["accepted"]:
                raise CqError("%s review refused: %s" % (who, "; ".join(result["errors"])))
            return
        brief = briefs.reviewer_brief(run, who, scope, start).split("## How to submit")[0]
        # The session already runs in the worktree, and its permission rules allow `git diff ...` as written.
        brief = brief.replace("git -C %s diff" % shlex.quote(run.data["worktree"]), "git diff")
        brief += "## How to answer\n\nYour answer is the submission object itself. The engine submits it for you.\n"
        feedback = ""
        for attempt in range(2):
            out = self._agent(run, "reviewer-%s" % who, "reviewer", prompt="Do the review in your input.",
                              stdin=brief + feedback, cwd=Path(run.data["worktree"]), mode="read",
                              schema=prompts.review_schema(), reviewer=who)
            out.pop("reviewed_from", None)
            if scope == "fix_diff":
                out["reviewed_from"] = start
            out["scope"] = scope
            result = reviews.submit(run, who, head, out)
            if result["accepted"]:
                return
            feedback = "\n## Your previous answer was refused\n\n%s\nCorrect it.\n" % "\n".join("- " + e for e in result["errors"])
        raise CqError("%s review refused twice: %s" % (who, "; ".join(result["errors"])))

    # -- triage, merge, outcomes ------------------------------------------------

    def _needs_aggregation(self, run: store.Run) -> bool:
        seen = set(self.record(run).get("aggregated", []))
        return any(f["status"] in ("unverified", "verified") and f["id"] not in seen for f in F.active(run.findings()))

    def _aggregate(self, run: store.Run) -> None:
        data = runs.sync(run)
        tier = policy.gate_tier(data)
        open_ = [f for f in F.active(run.findings()) if f["status"] in F.OPEN]
        self._count(run, "aggregate@%s:%d" % (data["head_sha"], len(open_)), 2, "Sorting the findings")
        summary = [{
            "id": f["id"], "severity": f["severity"], "status": f["status"], "category": f["category"],
            "always_blocking": f.get("always_blocking"), "location": f.get("location"), "claim": f["claim"],
            "scenario": f.get("scenario"), "proposed_fix": f.get("proposed_fix"),
            "raised_by": sorted({s["reviewer"] for s in f["sources"]}),
        } for f in open_]
        stdin = "Tier: %d\n\n# Open findings\n\n%s\n" % (tier, json.dumps(summary, indent=2))
        out = self._agent(run, "aggregator", "aggregator", prompt="Sort the findings in your input.",
                          stdin=stdin, cwd=Path(data["worktree"]), mode="read", schema=prompts.AGGREGATE)
        refusals: List[str] = []
        fix_order = list(out["fix_order"])
        with run.lock():
            findings = run.findings()
            known = set(findings)
            # Agents give ids exactly. A prefix that happens to match another finding is not accepted.
            unknown = sorted({t["id"] for t in out["triage"]} | {o["id"] for o in out["outcomes"]}
                             | {m["keep"] for m in out["merges"]} | {i for m in out["merges"] for i in m["duplicates"]})
            unknown = [i for i in unknown if i not in known]
            if unknown:
                refusals.append("unknown ids: %s" % ", ".join(unknown))
            out["triage"] = [t for t in out["triage"] if t["id"] in known]
            out["outcomes"] = [o for o in out["outcomes"] if o["id"] in known]
            out["merges"] = [m for m in out["merges"] if m["keep"] in known and all(i in known for i in m["duplicates"])]
            fix_order = [i for i in fix_order if i in known]
            for t in out["triage"]:
                try:
                    F.triage(findings, t["id"], t["severity"], t["category"], t["scenario"], t.get("proposed_fix"),
                             t.get("always_blocking"), "aggregator")
                except CqError as exc:
                    refusals.append("triage %s: %s" % (t["id"], exc))
            for m in out["merges"]:
                try:
                    F.merge(findings, m["keep"], m["duplicates"], "aggregator")
                except CqError as exc:
                    refusals.append("merge %s: %s" % (m["keep"], exc))
            run.save_findings(findings)
        given = {o["id"] for o in out["outcomes"]} | set(fix_order)
        for f in open_:
            # Every Duo thread needs an outcome, or it stays open and fails the run.
            if f["id"] not in given and any(s["reviewer"] == "duo" for s in f["sources"]):
                out["outcomes"].append({"id": f["id"], "action": "leave", "reason": "The aggregator gave no outcome."})
        for o in out["outcomes"]:
            try:
                self._apply_outcome(run, tier, o, fix_order)
            except CqError as exc:
                refusals.append("%s %s: %s" % (o["action"], o["id"], exc))
        if refusals:
            run.event("engine_refused", {"refusals": refusals}, "engine")
        with run.lock():
            rec = self.record(run)
            rec["aggregated"] = sorted(set(rec.get("aggregated", [])) | {f["id"] for f in open_})
            rec["fix_order"] = fix_order
            self._save(run, rec)

    def _apply_outcome(self, run: store.Run, tier: int, o: Dict[str, Any], fix_order: List[str]) -> None:
        with run.lock():
            data = run.data
            findings = run.findings()
            rec = F.get(findings, o["id"])
            if rec["status"] not in F.OPEN or rec.get("merged_into"):
                return
            action = o["action"]
            if action == "leave" and any(s["reviewer"] == "duo" for s in rec["sources"]):
                # A Duo thread stays open until its finding has an outcome. Leaving it
                # is a decision not to change the code, so it is recorded as one.
                action, o = "reject", dict(o, reason="Left as is: %s" % o["reason"])
            if action == "file":
                fileable = not (rec.get("always_blocking") or rec["severity"] in ("BLOCKER", config.UNTRIAGED)
                                or (rec["severity"] == "MAJOR" and tier >= 2))
                if not fileable:
                    fix_order.append(rec["id"])
                    return
                # Make sure the rules allow the filing before a ticket exists.
                F.set_status(copy.deepcopy(findings), copy.deepcopy(data), tier, rec["id"], "filed", "aggregator",
                             o["reason"], ticket="PENDING-1")
                rec = copy.deepcopy(rec)
            elif action == "reject":
                try:
                    F.set_status(findings, data, tier, rec["id"], "rejected", "aggregator", o["reason"])
                except CqError:
                    # Only a human rejects a gating finding. The disagreement goes to one.
                    F.set_status(findings, data, tier, rec["id"], "contested", "aggregator",
                                 "The aggregator thinks this is wrong: %s" % o["reason"])
            elif action == "contest":
                F.set_status(findings, data, tier, rec["id"], "contested", "aggregator", o["reason"])
            run.save_findings(findings)
        if action != "file":
            return
        # The tracker call is made without the run lock: Jira can take minutes to answer.
        key = self.tracker.create_once(
            self.record(run)["project"], "cq-%s-%s" % (run.dir.name, rec["id"]),
            "[%s] %s" % (rec["severity"], rec["claim"][:200]),
            self._finding_text(run, rec),
            [self.labels["found"], "tier-%d" % tier, rec["category"] or "uncategorised"])
        with run.lock():
            findings = run.findings()
            if findings[rec["id"]]["status"] in F.OPEN:
                F.set_status(findings, run.data, tier, rec["id"], "filed", "aggregator", o["reason"], ticket=key)
                run.save_findings(findings)

    def _finding_text(self, run: store.Run, f: Dict[str, Any]) -> str:
        loc = f.get("location") or {}
        mr = (run.data.get("mr") or {}).get("web_url") or "(no merge request yet)"
        return "\n".join([
            "Finding %s from run %s, ticket %s." % (f["id"], run.dir.name, run.data["ticket"]),
            "", "Where: %s:%s" % (loc.get("path", "-"), loc.get("start_line") or "?"),
            "Claim: %s" % f["claim"], "Scenario: %s" % (f.get("scenario") or "-"),
            "Proposed fix: %s" % (f.get("proposed_fix") or "-"),
            "Raised by: %s" % ", ".join(sorted({s["reviewer"] for s in f["sources"]})),
            "Merge request: %s" % mr,
        ])

    # -- verify -------------------------------------------------------------------

    def _verify(self, run: store.Run, ids: List[str]) -> None:
        data = runs.sync(run)
        head = data["head_sha"]
        self._count(run, "verify@%s:%s" % (head, ",".join(sorted(ids))), 2, "Verifying findings")
        findings = run.findings()
        items = [{k: findings[i].get(k) for k in ("id", "severity", "category", "location", "claim", "scenario", "proposed_fix")}
                 for i in ids]
        # The scratch copy lives with the worktrees: agents may not write in the run store.
        scratch = Path(self.cfg["worktrees_dir"]) / ".scratch" / ("%s-verify-%d" % (run.dir.name, int(time.time() * 1000)))
        scratch.parent.mkdir(parents=True, exist_ok=True)
        git(Path(data["worktree"]), "worktree", "add", "--quiet", "--detach", str(scratch), head)
        try:
            out = self._agent(run, "verifier", "verifier", prompt="Verify the findings in your input.",
                              stdin="# Findings to verify\n\n%s\n" % json.dumps(items, indent=2),
                              cwd=scratch, mode="write", schema=prompts.VERIFY)
        finally:
            git(Path(data["worktree"]), "worktree", "remove", "--force", str(scratch), check=False)
            shutil.rmtree(scratch, ignore_errors=True)
            git(Path(data["worktree"]), "worktree", "prune", check=False)
        tier = policy.gate_tier(data)
        with run.lock():
            findings = run.findings()
            for result in out["results"]:
                if result["id"] not in ids:
                    continue
                try:
                    if result["result"] == "reproduced":
                        F.set_status(findings, data, tier, result["id"], "verified", "verifier", "Reproduced",
                                     evidence=result["evidence"] or "reproduced")
                    else:
                        F.set_status(findings, data, tier, result["id"], "contested", "verifier",
                                     "Not reproduced: %s" % (result["evidence"] or "no detail"))
                except CqError as exc:
                    run.event("engine_refused", {"refusals": ["verify %s: %s" % (result["id"], exc)]}, "engine")
            run.save_findings(findings)

    # -- fix ------------------------------------------------------------------------

    def _fix(self, run: store.Run, reasons: List[Dict[str, Any]], phase: str) -> bool:
        """One fix round. False if a budget is spent. A budget spent before the merge
        request is not topped up from the merge-request budget: a human decides."""
        if run.data.get("budget_exhausted"):
            return False
        data = run.data
        findings = run.findings()
        order = [i for i in self.record(run).get("fix_order", []) if i in findings]
        targets = []
        for fid in order + sorted((f["id"] for f in F.active(findings)), key=lambda i: -F.rank(findings[i]["severity"])):
            rec = F.keeper(findings, findings[fid])
            if rec["id"] in targets or rec["status"] not in ("verified", "unverified"):
                continue
            gating = F.is_gating(rec)
            if (gating and rec["status"] == "verified") or (not gating and fid in order):
                targets.append(rec["id"])
        failed = [r["subject"]["check"] for r in reasons if r["code"] == "check_failed"]
        parts = []
        if targets:
            parts.append("# Findings to fix, in this order\n\n" + json.dumps(
                [{k: findings[i].get(k) for k in ("id", "severity", "category", "location", "claim", "scenario",
                                                  "proposed_fix", "history")} for i in targets], indent=2))
        for name in failed:
            rec = run.checks().get(name)
            log = Path(rec["log"]).read_text(encoding="utf-8", errors="replace").splitlines()[-80:] if rec else []
            parts.append("# Failed check: %s\n\nCommand: %s\n\n```\n%s\n```" % (name, " ".join(rec["argv"]) if rec else "?", "\n".join(log)))
        sonar_ev = run.external().get("sonar") or {}
        if any(r["code"] == "sonar_failed" for r in reasons):
            parts.append("# SonarQube quality gate failed\n\n%s" % json.dumps(sonar_ev.get("conditions", []), indent=2))
        if not parts:
            # Checked before a round is opened, so a human who resolves this still has the whole budget.
            raise Stop(NEEDS_HUMAN, "The run fails and there is nothing an agent can fix: %s"
                       % "; ".join(r["message"] for r in reasons if r["verdict"] == policy.FAIL), escalate=True)
        try:
            runs.fix_round(run, "local" if phase == "local" else "mr")
        except CqError:
            return False
        data = run.data
        out = self._agent(run, "fixer", "fixer", prompt="Fix what your input lists, in order, then commit.",
                          stdin="\n\n".join(parts) + "\n", cwd=Path(data["worktree"]), mode="write", schema=prompts.FIX)
        return self._record_fixes(run, targets, out)

    def _record_fixes(self, run: store.Run, targets: List[str], out: Dict[str, Any]) -> bool:
        """Record what the fixer reports. A claimed fix still needs its raiser's review."""
        data = runs.sync(run)
        if data["dirty"]:
            raise Stop(FAILED, "The fixer left uncommitted changes in %s." % data["worktree"])
        tier = policy.gate_tier(data)
        refusals = []
        with run.lock():
            findings = run.findings()
            for item in (out or {}).get("fixed", []):
                if item["id"] not in targets or item["id"].startswith("sonar-"):
                    continue
                commit = next((s for s in data["shas"] if item["commit"] and s.startswith(item["commit"])), data["head_sha"])
                try:
                    F.set_status(findings, data, tier, item["id"], "fixed", "fixer", item["summary"] or "Fixed", commit=commit)
                except CqError as exc:
                    refusals.append("fixed %s: %s" % (item["id"], exc))
            for item in (out or {}).get("not_fixed", []):
                if item["id"] not in targets:
                    continue
                try:
                    F.set_status(findings, data, tier, item["id"], "contested", "fixer", "Not fixed: %s" % item["reason"])
                except CqError as exc:
                    refusals.append("not_fixed %s: %s" % (item["id"], exc))
            run.save_findings(findings)
        if refusals:
            run.event("engine_refused", {"refusals": refusals}, "engine")
        if self.record(run).get("published"):
            self._push(run)
        return True

    # == merge request ============================================================

    def _push(self, run: store.Run) -> None:
        data = run.data
        repo = run.policy()["repo"]
        worktree = Path(data["worktree"])
        remote_head = git(worktree, "ls-remote", "--heads", repo["remote"], data["branch"], check=False).split("\t")[0]
        if remote_head != data["head_sha"]:
            git(worktree, "push", "--quiet", "-u", repo["remote"], "HEAD:refs/heads/%s" % data["branch"])

    def _publish(self, run: store.Run) -> None:
        runs.sync(run)
        self._push(run)
        mr = gitlab.mr_ensure(run)
        rec = self.record(run)
        if not rec.get("published"):
            self.tracker.web_link(run.data["ticket"], mr["web_url"] or "", "Merge request !%s" % mr["iid"])
            self._update(run, published=True)
        verdicts.decide(run, "final")
        gitlab.mr_describe(run, (run.dir / "report.md").read_text(encoding="utf-8"))

    def _collect_external(self, run: store.Run, codes: set) -> bool:
        gate = self._gate(run)
        did = False
        self._push(run)
        if "duo" in gate["external"] and codes & {"duo_missing", "duo_stale", "duo_not_requested", "duo_pending", "duo_error", "duo_request_failed"}:
            self._collect_duo(run)
            did = True
        if "sonar" in gate["external"] and codes & {"sonar_missing", "sonar_stale", "sonar_error"}:
            self._collect_sonar(run)
            did = True
        return did

    def _duo_request(self, run: store.Run, force: bool = False) -> Dict[str, Any]:
        try:
            evidence = gitlab.duo_request(run, force=force)
        except CqError as exc:
            if "Push first" in str(exc):
                # GitLab has not caught up with the push yet.
                raise Stop(WAITING, "Waiting for GitLab to show the pushed commit: %s" % exc, key="duo")
            raise
        if evidence["state"] == "request_failed":
            raise Stop(NEEDS_HUMAN, "GitLab Duo review could not be requested: %s" % evidence.get("detail"), escalate=True)
        return evidence

    def _collect_duo(self, run: store.Run) -> None:
        head = run.data["head_sha"]
        evidence = run.external().get("duo")
        if evidence is None or evidence.get("requested_head") != head or evidence["state"] in ("not_requested",):
            evidence = self._duo_request(run)
        waits = self.cfg["waits"]
        for _ in range(2):
            requested = parse_time(evidence["requested_at"])
            remaining = int(waits["duo_seconds"] - (now() - requested).total_seconds())
            evidence = gitlab.duo_wait(run, max(0, remaining), max(1, waits["poll_seconds"]))
            if evidence["state"] == "pending":
                raise Stop(WAITING, "Waiting for the GitLab Duo review of %s." % head[:12], key="duo")
            if evidence["state"] != "error":
                return
            if evidence.get("error_kind") != "duo":
                # The merge request lags the push, or GitLab did not answer: try again later.
                raise Stop(WAITING, "GitLab Duo: %s" % evidence.get("detail"), key="duo")
            if self.record(run).get("duo_rerequested") == head:
                break
            # Duo itself reported an error. Ask once more for this commit.
            with run.lock():
                rec = self.record(run)
                rec["duo_rerequested"] = head
                rec.get("waits_since", {}).pop("duo@%s" % head, None)  # the new request gets a full wait
                self._save(run, rec)
            run.event("engine_duo_rerequest", {"head": head, "detail": evidence.get("detail")}, "engine")
            evidence = self._duo_request(run, force=True)
        raise Stop(NEEDS_HUMAN, "GitLab Duo failed twice on %s: %s" % (head[:12], evidence.get("detail")), escalate=True)

    def _collect_sonar(self, run: store.Run) -> None:
        head = run.data["head_sha"]
        waits = self.cfg["waits"]
        started = self.record(run).get("sonar_wait", {}).get(head)
        if started is None:
            started = iso()
            with run.lock():
                rec = self.record(run)
                rec.setdefault("sonar_wait", {})[head] = started
                self._save(run, rec)
        while True:
            evidence = sonar.gate(run)
            if evidence["state"] in ("passed", "failed"):
                return
            detail = str(evidence.get("detail") or "")
            if evidence["state"] == "error" and re.search(r"\b40[13]\b|is not set", detail):
                # Waiting does not fix a missing or refused token.
                raise Stop(NEEDS_HUMAN, "SonarQube refused the request: %s" % detail, escalate=True)
            if (now() - parse_time(started)).total_seconds() >= waits["sonar_seconds"]:
                raise Stop(WAITING, "Waiting for a SonarQube analysis of %s (%s)." % (head[:12], detail), key="sonar")
            time.sleep(max(1, waits["poll_seconds"]))

    def _answer_duo(self, run: store.Run) -> bool:
        """Reply on every Duo thread whose finding has an outcome, and resolve it.
        A contested thread gets one reply with both positions and stays open."""
        did = False
        for _ in range(100):
            evidence = run.external().get("duo") or {}
            findings = run.findings()
            rec = self.record(run)
            replied = set(rec.get("duo_replied", []))
            pending = None
            for thread in evidence.get("threads", []):
                if thread["resolved"] or thread["finding_id"] not in findings:
                    continue
                outcome = F.keeper(findings, findings[thread["finding_id"]])
                if outcome["status"] in F.CLOSED:
                    pending = (thread, outcome, True)
                    break
                key = "%s:%s" % (thread["discussion_id"], outcome["status"])
                if outcome["status"] == "contested" and key not in replied:
                    pending = (thread, outcome, False)
                    break
            if pending is None:
                return did
            thread, outcome, resolve = pending
            gitlab.duo_reply(run, thread["finding_id"], self._duo_text(outcome), resolve)
            # duo_reply answers every thread behind the finding. Record them all, so none gets a second reply.
            answered = {s["local_id"] for s in findings[thread["finding_id"]]["sources"]
                        if s["reviewer"] == "duo" and s.get("local_id")} | {thread["discussion_id"]}
            with run.lock():
                rec = self.record(run)
                rec["duo_replied"] = sorted(set(rec.get("duo_replied", [])) | {"%s:%s" % (d, outcome["status"]) for d in answered})
                self._save(run, rec)
            did = True
        return did

    @staticmethod
    def _duo_text(f: Dict[str, Any]) -> str:
        last = f["history"][-1]["reason"] if f["history"] else ""
        if f["status"] == "fixed":
            return "Fixed in %s. %s" % ((f.get("fix_commit") or "")[:12], last)
        if f["status"] == "filed":
            return "Filed as %s for later. %s" % (f.get("ticket"), last)
        if f["status"] == "rejected":
            return "Not changed. %s" % last
        if f["status"] == "waived":
            return "Accepted as a known risk until %s. %s" % ((f.get("waiver") or {}).get("expires", "?")[:10], last)
        return "Waiting for a human decision. Position on record: %s" % last

    # == decide ===================================================================

    def _decide(self, run: store.Run, early: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Record the outcome. With `early`, the local loop could not pass and
        these are its reasons; the merge-request loop was skipped."""
        d = verdicts.decide(run, "final")
        if run.data.get("mr"):
            gitlab.mr_describe(run, (run.dir / "report.md").read_text(encoding="utf-8"))
        stage = "decide"
        head = run.data["head_sha"]
        rec = self.record(run)
        if early is None and d["verdict"] == policy.PASS:
            reason = ("Tracer branch: its checks passed. It never merges." if d["mode"] == "tracer"
                      else "Ready for you. %s" % d["human_action"])
            if rec.get("decided_head") != head:
                self._ticket_comment(run, "Run %s finished: PASS.\n\n%s\nMerge request: %s" % (
                    run.dir.name, reason, (run.data.get("mr") or {}).get("web_url")))
                self._ticket_labels(run, add=[self.labels["done"]],
                                    remove=[self.labels["in_progress"], self.labels["needs_human"]])
                if rec.get("escalation") and not rec.get("escalation_closed"):
                    try:
                        self.tracker.comment(rec["escalation"], "Resolved: run %s now passes at %s." % (run.dir.name, head[:12]))
                        self._update(run, escalation_closed=True, escalation_digest=None)
                    except CqError as exc:
                        run.event("engine_tracker_error", {"error": str(exc)}, "engine")
            self._update(run, decided_head=head)
            return self._outcome(run, self._state(run, COMPLETED, stage, reason))
        reasons = early if early is not None else d["reasons"]
        messages = [r["message"] for r in reasons]
        if early is None and d["verdict"] == policy.INCONCLUSIVE and all(r["code"] in WAITING_CODES for r in reasons):
            return self._finish(run, Stop(WAITING, " ".join(messages), key="external"), stage)
        verdict = d["verdict"] if early is None else self._effective({"reasons": early}, "local")[0]
        self._escalate(run, messages)
        return self._outcome(run, self._state(run, NEEDS_HUMAN, stage, "%s: %s" % (verdict, " ".join(messages[:3]))))

    def _escalate(self, run: store.Run, messages: List[str]) -> None:
        rec = self.record(run)
        mr = (run.data.get("mr") or {}).get("web_url")
        text = "\n".join([
            "Run %s for %s needs a decision from you." % (run.dir.name, run.data["ticket"]), "",
            "Why:"] + ["- %s" % m for m in messages] + [
            "", "Merge request: %s" % (mr or "not opened yet"),
            "Report: %s" % (run.dir / "report.md"),
            "", "What you can do:",
            "- cq findings list %s --open" % run.dir.name,
            "- cq decide %s <finding> reject|waive|reopen --reason \"...\"" % run.dir.name,
            "- cq approve %s plan_approval|final_signoff" % run.dir.name,
            "- cq note add %s plan_review <file whose first line is PLAN OK>   (overrules the plan reviewer)" % run.dir.name,
            "- cq fix extend %s --phase local|mr" % run.dir.name,
            "- then: cq engine resume %s" % run.dir.name,
        ])
        digest = sha256_text(text)
        if rec.get("escalation_digest") == digest:
            return
        try:
            key = self.tracker.create_once(
                rec.get("project") or trackers.project_of(run.data["ticket"]), "cq-esc-%s" % run.dir.name,
                ("Decision needed: %s %s" % (run.data["ticket"], rec.get("summary", "")))[:250], text,
                [self.labels["escalation"], self.labels["needs_human"]])
            if rec.get("escalation") == key:
                self.tracker.comment(key, text)
            else:
                self.tracker.link(key, run.data["ticket"])
                if mr:
                    self.tracker.web_link(key, mr, "Merge request")
        except Exception as exc:
            key = None
            self._note(run, "escalation", text + "\n\n(The tracker could not be reached: %s)\n" % exc)
        self._ticket_labels(run, add=[self.labels["needs_human"]])
        self._update(run, escalation=key, escalation_digest=digest, escalation_closed=False)

    def _ticket_comment(self, run: store.Run, text: str) -> None:
        try:
            self.tracker.comment(run.data["ticket"], text)
        except Exception as exc:
            run.event("engine_tracker_error", {"error": str(exc)}, "engine")

    def _ticket_labels(self, run: store.Run, add: List[str] = (), remove: List[str] = ()) -> None:
        try:
            self.tracker.labels(run.data["ticket"], add=list(add), remove=list(remove))
        except Exception as exc:
            run.event("engine_tracker_error", {"error": str(exc)}, "engine")


# == reporting ======================================================================

def status_rows() -> List[Dict[str, Any]]:
    rows = []
    for run in store.list_runs():
        rec = Engine.record(run)
        if not rec:
            continue
        d = run.data
        state, reason = live_state(run, rec)
        rows.append({"run_id": d["run_id"], "ticket": d["ticket"], "repo": d["repo_id"], "tier": d["tier"],
                     "state": state, "stage": rec.get("stage"), "reason": reason,
                     "cost_usd": rec.get("cost_usd", 0.0), "sessions": rec.get("sessions", 0),
                     "fix_rounds": d["fix_rounds"], "merge_request": (d.get("mr") or {}).get("web_url"),
                     "escalation": rec.get("escalation"), "started_at": rec.get("started_at"),
                     "updated_at": rec.get("updated_at")})
    return rows


def stats() -> Dict[str, Any]:
    """The numbers the proposal measures the system by, across all engine runs."""
    rows = status_rows()
    by_state: Dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    finished = [r for r in rows if r["state"] in (COMPLETED, NEEDS_HUMAN)]
    durations = sorted(
        (parse_time(r["updated_at"]) - parse_time(r["started_at"])).total_seconds() / 3600
        for r in rows if r["state"] == COMPLETED and r["started_at"] and r["updated_at"])
    return {
        "runs": len(rows),
        "by_state": by_state,
        "escalation_rate": round(by_state.get(NEEDS_HUMAN, 0) / len(finished), 3) if finished else None,
        "cost_usd_total": round(sum(r["cost_usd"] for r in rows), 2),
        "cost_usd_per_completed": round(sum(r["cost_usd"] for r in rows if r["state"] == COMPLETED) / max(1, by_state.get(COMPLETED, 0)), 2),
        "fix_rounds_mean": round(sum(r["fix_rounds"]["local"] + r["fix_rounds"]["mr"] for r in rows) / len(rows), 2) if rows else None,
        "hours_to_decision_median": round(durations[len(durations) // 2], 2) if durations else None,
        "by_tier": {t: sum(1 for r in rows if r["tier"] == t) for t in range(4)},
    }
