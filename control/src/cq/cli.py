"""The cq command line. Agents and humans both use it; a few commands are for
humans only and refuse to run without a terminal."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, admin, briefs, config, findings as F, gitlab, guard, policy, report, reviews, runs, sonar, store, verdicts
from .util import CqError, iso, now, write_json


def _actor(args: argparse.Namespace) -> str:
    return getattr(args, "by", None) or os.environ.get("CQ_ACTOR", "agent")


def require_human(what: str) -> None:
    """Human decisions need a real terminal. This and the guard hook are speed
    bumps against an agent approving its own work, not a security boundary."""
    if os.environ.get("CLAUDECODE") or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise CqError("`cq %s` records a human decision. Run it yourself in a terminal." % what)


def _confirm(prompt: str, expected: str) -> None:
    answer = input(prompt).strip()
    if answer != expected:
        raise CqError("not confirmed; nothing was recorded")


def _decision(run: store.Run, stage: str) -> Dict[str, Any]:
    return verdicts.decide(run, stage)


def _print_decision(decision: Dict[str, Any]) -> None:
    print("%s  (stage: %s, tier %d, commit %s)" % (decision["verdict"], decision["stage"], decision["tier"], decision["head_sha"][:12]))
    for r in decision["reasons"]:
        print("  %-12s %s" % (r["verdict"], r["message"]))
    if decision["verdict"] == policy.PASS:
        if decision["mode"] == "tracer":
            print("  Tracer checks passed. A tracer branch never merges to the default branch.")
        elif decision["stage"] == "local":
            print("  Local evidence is complete. Open the merge request next.")
        else:
            print("  Ready for the human step: %s" % decision["human_action"])


def _open_run(run_id: str) -> store.Run:
    """A run that can still take evidence. A closed run is a record, not a workspace."""
    run = store.find_run(run_id)
    if run.data["status"] != "active":
        raise CqError("run %s is closed. Start a run for the current branch with `cq run start`." % run.dir.name)
    return run


def _read_text(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def _resolve_sha(data: Dict[str, Any], value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    matches = [s for s in data["shas"] + [data["base_sha"]] if s.startswith(value)]
    return matches[0] if len(set(matches)) == 1 else value


# -- commands ---------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    lines, ok = admin.doctor(args.repo)
    for level, message in lines:
        print("%-5s %s" % (level.upper(), message))
    print("\n%s" % ("All required checks passed." if ok else "Fix the FAIL lines above."))
    return 0 if ok else 1


def cmd_init_repo(args: argparse.Namespace) -> int:
    for line in admin.init_repo(Path(args.path)):
        print(line)
    print("\nNext: fill in the [FILL IN] parts of .cq/, register the repository in registry.yml, then run `cq doctor`.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.action == "start":
        if args.new:
            require_human("run start --new")
        run, created = runs.start(args.repo, Path(args.worktree), args.ticket, args.min_tier, args.new)
        data = run.data
        print("%s run %s" % ("started" if created else "resumed", data["run_id"]))
        _show(run, data)
        return 0
    if args.action == "list":
        for r in store.list_runs():
            d = r.data
            print("%-48s %-8s tier %d  %s  %s" % (d["run_id"], d["status"], d["tier"], d["repo_id"], d["branch"]))
        return 0
    run = store.find_run(args.run)
    if args.action == "sync":
        _show(run, runs.sync(run))
    elif args.action == "show":
        data = run.data
        if args.json:
            print(json.dumps(data, indent=2, sort_keys=True))
        else:
            _show(run, data)
    elif args.action == "repin":
        print("policy repinned: %s" % runs.repin(run)[:12])
        _show(run, run.data)
    elif args.action == "close":
        require_human("run close")
        with run.lock():
            data = run.data
            data["status"] = "closed"
            run.save(data)
            run.event("run_closed", {}, _actor(args))
        print("closed %s. Branches and merge requests are left as they are." % data["run_id"])
    return 0


def _show(run: store.Run, data: Dict[str, Any]) -> None:
    pol = run.policy()
    tier = policy.gate_tier(data)
    gate = pol["floor"]["gates"][str(tier)]
    na = pol["repo"]["not_applicable"]
    print("run:        %s" % data["run_id"])
    print("repository: %s   branch: %s   remote: %s   ticket: %s" % (data["repo_id"], data["branch"], pol["repo"]["remote"], data["ticket"] or "-"))
    print("commit:     %s   base: %s   %s" % (data["head_sha"][:12], data["base_sha"][:12], "DIRTY WORKTREE" if data["dirty"] else "clean"))
    label = "%d %s" % (data["tier"], config.TIER_NAMES[data["tier"]])
    if data["mode"] == "tracer":
        label += "  (tracer branch: tier 0 gates apply, never merges)"
    print("tier:       %s" % label)
    for r in data["tier_reasons"][:5]:
        print("            %s -> %s" % (r["path"], r["reason"]))
    if data["unmapped"]:
        print("unmapped:   %s" % ", ".join(data["unmapped"][:8]))
    print("changed:    %d file(s), %d line(s)" % (len(data["changed_files"]), data["changed_lines"]))
    print("fix rounds: local %d/%d, mr %d/%d" % (
        data["fix_rounds"]["local"], pol["floor"]["budgets"]["fix_rounds_local"],
        data["fix_rounds"]["mr"], pol["floor"]["budgets"]["fix_rounds_mr"]))
    if data.get("mr"):
        print("merge request: !%s %s" % (data["mr"]["iid"], data["mr"].get("web_url") or ""))

    def needed(names: List[str]) -> str:
        return ", ".join(n for n in names if n not in na) or "-"

    print("required checks:    %s" % needed(gate["checks"]))
    print("required reviewers: %s" % needed(gate["reviewers"]))
    print("required external:  %s" % needed(gate["external"]))
    print("required notes:     %s" % needed(gate["notes"]))
    print("human approvals:    %s" % (", ".join(gate["approvals"]) or "-"))
    skipped = [n for n in gate["checks"] + gate["reviewers"] + gate["external"] + gate["notes"] if n in na]
    if skipped:
        print("not applicable:     %s" % ", ".join(skipped))


def cmd_tier(args: argparse.Namespace) -> int:
    if args.action == "estimate":
        result = runs.estimate_tier(args.repo, args.paths)
        print("tier %d %s" % (result["tier"], config.TIER_NAMES[result["tier"]]))
        for r in result["per_file"]:
            print("  %d  %s  (%s)" % (r["tier"], r["path"], r["reason"]))
        return 0
    run = _open_run(args.run)
    if args.action == "raise":
        data = runs.raise_tier(run, args.tier, args.reason, _actor(args))
    else:
        require_human("tier lower")
        data = runs.lower_tier(run, args.tier, runs.human_name(Path(run.data["worktree"])), args.reason)
    print("tier is now %d %s" % (data["tier"], config.TIER_NAMES[data["tier"]]))
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    run = store.find_run(args.run)
    for row in briefs.models_table(run):
        print("%-12s %-28s %-10s %s" % (row["role"], row["model"], row["family"], row["effort"]))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    run = store.find_run(args.run)
    if args.action == "log":
        rec = run.checks().get(args.name)
        if rec is None:
            raise CqError("check '%s' has not run" % args.name)
        lines = Path(rec["log"]).read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(lines[-args.lines:]))
        return 0
    run = _open_run(args.run)
    if args.required:
        pol = run.policy()
        data = runs.sync(run)
        gate = pol["floor"]["gates"][str(policy.gate_tier(data))]
        names = [n for n in gate["checks"] if n not in pol["repo"]["not_applicable"]]
    elif args.name:
        names = [args.name]
    else:
        raise CqError("give a check name or --required")
    worst = 0
    for name in names:
        if name not in run.policy()["repo"]["checks"]:
            print("%-14s MISSING  no command in registry.yml" % name)
            worst = max(worst, 3)
            continue
        rec = runs.run_check(run, name)
        print("%-14s %-7s %s" % (name, rec["status"].upper(), rec["detail"] or ("exit %s, %.1fs" % (rec["exit_code"], rec["duration_seconds"]))))
        worst = max(worst, {"passed": 0, "failed": 1}.get(rec["status"], 3))
    return worst


def cmd_brief(args: argparse.Namespace) -> int:
    run = store.find_run(args.run)
    data = runs.sync(run)
    sys.stdout.write(briefs.reviewer_brief(run, args.reviewer, args.scope, _resolve_sha(data, args.reviewed_from)))
    return 0


def _submit(run: store.Run, reviewer: str, head: str, submission: Any, dry_run: bool) -> int:
    result = reviews.submit(run, reviewer, head, submission, dry_run)
    for w in result["warnings"]:
        print("warning: %s" % w)
    if result["errors"]:
        for e in result["errors"]:
            print("refused: %s" % e)
        return 4
    if dry_run:
        print("ok: the submission would be accepted (%d finding(s))" % len(submission["findings"]))
        return 0
    touched = result["touched"]
    print("accepted: %s %s review of %s, %d finding(s)%s" % (
        reviewer, result["scope"], result["head"][:12], len(touched), (": " + ", ".join(touched)) if touched else ""))
    return 0


def cmd_findings(args: argparse.Namespace) -> int:
    run = store.find_run(args.run) if args.action in ("list", "show") else _open_run(args.run)
    if args.action in ("check", "add"):
        if args.reviewer in ("adversary", "adversary2"):
            raise CqError("the %s review is submitted by `cq adversary run`, which stamps the configured model on it" % args.reviewer)
        try:
            submission = json.loads(_read_text(args.file))
        except ValueError as exc:
            print("refused: the file is not valid JSON: %s" % exc)
            return 4
        return _submit(run, args.reviewer, args.head, submission, args.action == "check")
    if args.action == "list":
        rows = F.active(run.findings())
        if args.open:
            rows = [f for f in rows if f["status"] in F.OPEN]
        rows.sort(key=lambda f: (f["status"] not in F.OPEN, -F.rank(f["severity"]), -(f.get("confidence") or 0)))
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
            return 0
        for f in rows:
            loc = f.get("location") or {}
            where = "%s:%s" % (loc.get("path"), loc.get("start_line") or "?") if loc.get("path") else "-"
            flag = " [always blocking: %s]" % f["always_blocking"] if f.get("always_blocking") else ""
            who = ",".join(sorted({s["reviewer"] for s in f["sources"]}))
            print("%-22s %-9s %-10s %s%s\n    %s  (by %s)" % (f["id"], f["severity"], f["status"], where, flag, (f["claim"] or "")[:140], who))
        if not rows:
            print("no findings")
        return 0
    if args.action == "show":
        print(json.dumps(F.get(run.findings(), args.id), indent=2, sort_keys=True))
        return 0
    with run.lock():
        data = run.data
        findings = run.findings()
        if args.action == "merge":
            rec = F.merge(findings, args.keep, args.duplicates, _actor(args))
            run.event("findings_merged", {"keep": rec["id"], "merged": args.duplicates, "severity": rec["severity"]}, _actor(args))
            print("merged into %s: severity %s, status %s" % (rec["id"], rec["severity"], rec["status"]))
        elif args.action == "status":
            rec = F.set_status(
                findings, data, policy.gate_tier(data), args.id, args.status, _actor(args), args.reason,
                args.evidence, args.ticket, _resolve_sha(data, args.commit),
            )
            run.event("finding_status", {"id": rec["id"], "status": rec["status"], "reason": args.reason}, _actor(args))
            print("%s is now %s" % (rec["id"], rec["status"]))
        elif args.action == "triage":
            rec = F.triage(findings, args.id, args.severity, args.category, args.scenario, args.proposed_fix, args.always_blocking, _actor(args))
            run.event("finding_triaged", {"id": rec["id"], "severity": rec["severity"]}, _actor(args))
            print("%s triaged as %s %s" % (rec["id"], rec["severity"], rec["category"]))
        run.save_findings(findings)
    return 0


def cmd_adversary(args: argparse.Namespace) -> int:
    run = _open_run(args.run)
    data = runs.sync(run)
    start = _resolve_sha(data, args.reviewed_from)
    if args.scope == "fix_diff" and not start:
        start = briefs.fix_start(run, args.slot)
    submission = briefs.run_external_reviewer(run, args.slot, args.scope, start)
    if isinstance(submission, dict):
        # The scope and the start come from this command, not from the reviewer's answer.
        submission["scope"] = args.scope
        submission.pop("reviewed_from", None)
        if args.scope == "fix_diff":
            submission["reviewed_from"] = start
    return _submit(run, args.slot, data["head_sha"], submission, False)


def cmd_fix(args: argparse.Namespace) -> int:
    run = _open_run(args.run)
    if args.action == "extend":
        require_human("fix extend")
        data = runs.extend_budget(run, args.phase, runs.human_name(Path(run.data["worktree"])))
        print("one more %s fix round granted" % args.phase)
        return 0
    data = runs.fix_round(run, args.phase)
    print("%s fix round %d started at %s" % (args.phase, data["fix_rounds"][args.phase], data["head_sha"][:12]))
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    run = _open_run(args.run)
    rec = runs.add_note(run, args.name, Path(args.file), _actor(args))
    print("note '%s' on file (%s)" % (args.name, rec["sha256"][:12]))
    return 0


def cmd_mr(args: argparse.Namespace) -> int:
    run = _open_run(args.run)
    runs.sync(run)
    if args.action == "ensure":
        mr = gitlab.mr_ensure(run)
        print("merge request !%s %s (%s)" % (mr["iid"], mr.get("web_url") or "", "created as draft" if mr["created_by_cq"] else "already existed"))
    else:
        decision = _decision(run, "final")
        gitlab.mr_describe(run, (run.dir / "report.md").read_text(encoding="utf-8"))
        print("merge request description updated: %s" % decision["verdict"])
    return 0


def cmd_duo(args: argparse.Namespace) -> int:
    run = _open_run(args.run)
    runs.sync(run)
    if args.action == "request":
        evidence = gitlab.duo_request(run)
    elif args.action == "ingest":
        evidence = gitlab.duo_ingest(run)
    elif args.action == "wait":
        evidence = gitlab.duo_wait(run, args.timeout, args.interval)
    else:
        rec = gitlab.duo_reply(run, args.id, _read_text(args.body_file), args.resolve)
        print("replied on the thread for %s%s" % (rec["id"], " and resolved it" if args.resolve else ""))
        return 0
    print("duo: %s. %s" % (evidence["state"], evidence.get("detail") or ""))
    for thread in evidence.get("threads", []):
        if not thread["resolved"]:
            print("  open thread -> finding %s" % thread["finding_id"])
    return 0 if evidence["state"] == "reviewed" else 3


def cmd_sonar(args: argparse.Namespace) -> int:
    run = _open_run(args.run)
    runs.sync(run)
    evidence = sonar.gate(run)
    print("sonar: %s. %s" % (evidence["state"], evidence.get("detail") or ""))
    return {"passed": 0, "failed": 1}.get(evidence["state"], 3)


def cmd_verdict(args: argparse.Namespace) -> int:
    run = store.find_run(args.run)
    decision = _decision(run, args.stage)
    if args.json:
        print(json.dumps(decision, indent=2, sort_keys=True))
    else:
        _print_decision(decision)
    return policy.EXIT_CODES[decision["verdict"]]


def cmd_report(args: argparse.Namespace) -> int:
    run = store.find_run(args.run)
    _decision(run, args.stage)
    sys.stdout.write((run.dir / "report.md").read_text(encoding="utf-8"))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    require_human("approve")
    run = _open_run(args.run)
    data = runs.sync(run)
    by = runs.human_name(Path(data["worktree"]))
    if args.kind == "plan_approval":
        plan = run.notes().get("plan")
        if plan is None:
            raise CqError("there is no plan on file to approve")
        print("Plan on file: %s" % plan["path"])
        _confirm("Type 'approve' to approve this exact plan: ", "approve")
    else:
        _print_decision(_decision(run, "final"))
        print("\nYou are signing off commit %s on run %s." % (data["head_sha"], data["run_id"]))
        _confirm("Type the first 8 characters of that commit to sign off: ", data["head_sha"][:8])
    rec = runs.record_approval(run, args.kind, by)
    print("recorded %s by %s" % (rec["kind"], rec["by"]))
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    require_human("decide")
    run = _open_run(args.run)
    data = run.data
    by = runs.human_name(Path(data["worktree"]))
    rec = F.get(run.findings(), args.id)
    print("%s  %s  %s\n  %s" % (rec["id"], rec["severity"], rec["status"], rec["claim"]))
    _confirm("Type '%s' to confirm: " % args.decision, args.decision)
    with run.lock():
        findings = run.findings()
        rec = F.decide(findings, run.policy()["floor"], data["tier"], args.id, args.decision, "human:" + by, args.reason, args.days, args.ticket, now())
        run.save_findings(findings)
        run.event("human_decision", {"id": rec["id"], "decision": args.decision, "reason": args.reason}, "human:" + by)
    print("%s is now %s" % (rec["id"], rec["status"]))
    return 0


def cmd_waivers(args: argparse.Namespace) -> int:
    at = now()
    shown = 0
    for run in store.list_runs():
        for f in F.active(run.findings()):
            if f["status"] != "waived" or not f.get("waiver"):
                continue
            expired = F.waiver_expired(f, at)
            if args.expired and not expired:
                continue
            shown += 1
            print("%s  %s  %s  %s %s  ticket %s\n    %s" % (
                run.dir.name, f["id"], f["severity"], "EXPIRED" if expired else "expires", f["waiver"]["expires"][:10],
                f.get("ticket") or "-", (f["claim"] or "")[:120]))
    if not shown:
        print("no %swaivers" % ("expired " if args.expired else ""))
    return 0


def _print_outcome(o: Dict[str, Any]) -> None:
    print("%-12s %-11s %s" % (o.get("ticket") or "-", o["state"], o.get("run_id") or "(no run)"))
    if o.get("reason"):
        print("    %s" % o["reason"])
    if o.get("merge_request"):
        print("    merge request: %s" % o["merge_request"])
    if o.get("escalation"):
        print("    escalation: %s" % o["escalation"])
    if o.get("cost_usd"):
        print("    agent cost so far: $%.2f" % o["cost_usd"])


def cmd_engine(args: argparse.Namespace) -> int:
    from .engine import core  # loads engine.yml only when the engine is used

    if args.action == "status":
        rows = core.status_rows()
        if args.run:
            rows = [r for r in rows if r["run_id"].startswith(args.run)]
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        for r in rows if not args.json else []:
            print("%-48s %-10s %-12s tier %d  %-14s $%.2f" % (r["run_id"], r["ticket"], r["state"], r["tier"], r["stage"] or "-", r["cost_usd"]))
            if r["reason"]:
                print("    %s" % r["reason"])
        return 0
    if args.action == "stats":
        result = core.stats()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    engine = core.Engine()
    if args.action == "run":
        outcomes = [engine.start(args.ticket, args.repo)]
    elif args.action == "resume":
        if args.waiting:
            outcomes = engine.resume_waiting()
        elif args.run:
            outcomes = [engine.resume(store.find_run(args.run).dir.name)]
        else:
            raise CqError("give a run id, or --waiting")
    elif args.action == "pull":
        outcomes = engine.pull(args.limit)
    else:
        outcomes = [engine.cancel(store.find_run(args.run).dir.name)]
    if args.json:
        print(json.dumps(outcomes, indent=2, sort_keys=True))
    else:
        for o in outcomes:
            _print_outcome(o)
        if not outcomes:
            print("nothing to do")
    if len(outcomes) == 1 and args.action in ("run", "resume") and not getattr(args, "waiting", False):
        return core.EXIT_CODES.get(outcomes[0]["state"], 4)
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2, which is the BLOCKED code
        self.print_usage(sys.stderr)
        self.exit(4, "cq: %s\n" % message)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="cq", description="Policy engine and run store for the agentic code quality system.")
    p.add_argument("--version", action="version", version="cq " + __version__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("doctor", help="check the control repo, tools and registered repositories")
    s.add_argument("--repo")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("init-repo", help="install .cq and .claude files into a project (never overwrites)")
    s.add_argument("path")
    s.set_defaults(func=cmd_init_repo)

    s = sub.add_parser("run", help="start, sync and inspect runs")
    rs = s.add_subparsers(dest="action", required=True)
    a = rs.add_parser("start")
    a.add_argument("--repo", help="registry id; found from the worktree when omitted")
    a.add_argument("--worktree", default=".")
    a.add_argument("--ticket")
    a.add_argument("--min-tier", type=int, choices=[0, 1, 2, 3], default=0, help="a floor from the ticket's risk label")
    a.add_argument("--new", action="store_true", help="HUMAN ONLY: abandon the active run for this branch and start over")
    rs.add_parser("list")
    for name in ("sync", "show", "repin", "close"):
        a = rs.add_parser(name)
        a.add_argument("run")
        if name == "show":
            a.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("tier", help="estimate a tier from paths; raise a run's tier (lowering is for humans)")
    ts = s.add_subparsers(dest="action", required=True)
    a = ts.add_parser("estimate")
    a.add_argument("--repo", required=True)
    a.add_argument("paths", nargs="+")
    for name in ("raise", "lower"):
        a = ts.add_parser(name)
        a.add_argument("run")
        a.add_argument("tier", type=int, choices=[0, 1, 2, 3])
        a.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_tier)

    s = sub.add_parser("models", help="model and effort per role for this run's tier")
    s.add_argument("run")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("check", help="run a registered check and record the result")
    cs = s.add_subparsers(dest="action", required=True)
    a = cs.add_parser("run")
    a.add_argument("run")
    a.add_argument("name", nargs="?")
    a.add_argument("--required", action="store_true", help="run every check this tier requires")
    a = cs.add_parser("log")
    a.add_argument("run")
    a.add_argument("name")
    a.add_argument("--lines", type=int, default=80)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("brief", help="print a reviewer's full instructions for this run")
    s.add_argument("run")
    s.add_argument("--reviewer", required=True)
    s.add_argument("--scope", choices=["full", "fix_diff"], default="full")
    s.add_argument("--from", dest="reviewed_from")
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("findings", help="submit, list and update findings")
    fs = s.add_subparsers(dest="action", required=True)
    for name in ("check", "add"):
        a = fs.add_parser(name)
        a.add_argument("run")
        a.add_argument("--reviewer", required=True)
        a.add_argument("--head", required=True, help="the commit that was reviewed")
        a.add_argument("file", help="submission JSON, or - for stdin")
    a = fs.add_parser("list")
    a.add_argument("run")
    a.add_argument("--open", action="store_true")
    a.add_argument("--json", action="store_true")
    a = fs.add_parser("show")
    a.add_argument("run")
    a.add_argument("id")
    a = fs.add_parser("merge")
    a.add_argument("run")
    a.add_argument("keep")
    a.add_argument("duplicates", nargs="+")
    a.add_argument("--by")
    a = fs.add_parser("status")
    a.add_argument("run")
    a.add_argument("id")
    a.add_argument("status", choices=["verified", "contested", "fixed", "filed", "rejected"])
    a.add_argument("--reason", required=True)
    a.add_argument("--evidence")
    a.add_argument("--ticket")
    a.add_argument("--commit")
    a.add_argument("--by")
    a = fs.add_parser("triage")
    a.add_argument("run")
    a.add_argument("id")
    a.add_argument("--severity", required=True, choices=config.SEVERITIES)
    a.add_argument("--category", required=True, choices=config.CATEGORIES)
    a.add_argument("--scenario", required=True)
    a.add_argument("--proposed-fix")
    a.add_argument("--always-blocking", choices=config.ALWAYS_BLOCKING)
    a.add_argument("--by")
    s.set_defaults(func=cmd_findings)

    s = sub.add_parser("adversary", help="run the configured cross-vendor reviewer")
    as_ = s.add_subparsers(dest="action", required=True)
    a = as_.add_parser("run")
    a.add_argument("run")
    a.add_argument("--slot", choices=["adversary", "adversary2"], default="adversary")
    a.add_argument("--scope", choices=["full", "fix_diff"], default="full")
    a.add_argument("--from", dest="reviewed_from")
    s.set_defaults(func=cmd_adversary)

    s = sub.add_parser("fix", help="count a fix round against the budget")
    xs = s.add_subparsers(dest="action", required=True)
    for name in ("round", "extend"):
        a = xs.add_parser(name, help="HUMAN ONLY: grant one more round" if name == "extend" else None)
        a.add_argument("run")
        a.add_argument("--phase", required=True, choices=["local", "mr"])
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("note", help="put a plan, plan review, rollback note or escalation on file")
    ns = s.add_subparsers(dest="action", required=True)
    a = ns.add_parser("add")
    a.add_argument("run")
    a.add_argument("name")
    a.add_argument("file")
    a.add_argument("--by")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("mr", help="find or create the draft merge request; publish the report")
    ms = s.add_subparsers(dest="action", required=True)
    for name in ("ensure", "describe"):
        a = ms.add_parser(name)
        a.add_argument("run")
    s.set_defaults(func=cmd_mr)

    s = sub.add_parser("duo", help="GitLab Duo review loop")
    ds = s.add_subparsers(dest="action", required=True)
    for name in ("request", "ingest", "wait"):
        a = ds.add_parser(name)
        a.add_argument("run")
        if name == "wait":
            a.add_argument("--timeout", type=int, default=900)
            a.add_argument("--interval", type=int, default=30)
    a = ds.add_parser("reply")
    a.add_argument("run")
    a.add_argument("id")
    a.add_argument("--body-file", required=True)
    a.add_argument("--resolve", action="store_true")
    s.set_defaults(func=cmd_duo)

    s = sub.add_parser("sonar", help="read the SonarQube result for the merge request")
    ss = s.add_subparsers(dest="action", required=True)
    a = ss.add_parser("gate")
    a.add_argument("run")
    s.set_defaults(func=cmd_sonar)

    s = sub.add_parser("verdict", help="evaluate the policy. Exit 0 PASS, 1 FAIL, 2 BLOCKED, 3 INCONCLUSIVE")
    s.add_argument("run")
    s.add_argument("--stage", choices=["local", "final"], default="final")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_verdict)

    s = sub.add_parser("report", help="print the evidence report as markdown")
    s.add_argument("run")
    s.add_argument("--stage", choices=["local", "final"], default="final")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("approve", help="HUMAN ONLY: approve a plan or sign off a commit")
    s.add_argument("run")
    s.add_argument("kind", choices=config.APPROVALS)
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("decide", help="HUMAN ONLY: reject, waive or reopen a finding")
    s.add_argument("run")
    s.add_argument("id")
    s.add_argument("decision", choices=["reject", "waive", "reopen"])
    s.add_argument("--reason", required=True)
    s.add_argument("--days", type=int, help="waiver length; the tier sets the maximum")
    s.add_argument("--ticket")
    s.set_defaults(func=cmd_decide)

    s = sub.add_parser("waivers", help="list risk acceptances across runs")
    s.add_argument("--expired", action="store_true")
    s.set_defaults(func=cmd_waivers)

    s = sub.add_parser("engine", help="take tickets end to end with headless agents (Hermes calls these)")
    gs = s.add_subparsers(dest="action", required=True)
    a = gs.add_parser("run", help="screen a ticket and drive it to a decided draft merge request")
    a.add_argument("ticket")
    a.add_argument("--repo", help="registry id, when the ticket's project maps to several repositories")
    a = gs.add_parser("resume", help="continue a run after a wait, a stop or a crash")
    a.add_argument("run", nargs="?")
    a.add_argument("--waiting", action="store_true", help="resume every run that is WAITING")
    a = gs.add_parser("pull", help="start runs for agent-ready tickets, within the WIP limit")
    a.add_argument("--limit", type=int, default=1)
    a = gs.add_parser("cancel", help="stop a run; its branch and merge request are kept")
    a.add_argument("run")
    a = gs.add_parser("status", help="engine runs and where each one stands")
    a.add_argument("run", nargs="?")
    gs.add_parser("stats", help="the numbers to measure the system by")
    for parser in gs.choices.values():
        parser.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_engine)

    s = sub.add_parser("guard", help="Claude Code PreToolUse hook: reads the hook payload on stdin")
    s.set_defaults(func=lambda args: guard.main())
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Exit codes 0 to 3 are verdicts and states. A problem with the command
    # itself must never look like one of them: 4 is a refusal or bad input,
    # 5 is a bug in cq.
    try:
        return int(args.func(args) or 0)
    except CqError as exc:
        print("cq: %s" % exc, file=sys.stderr)
        return 4
    except (OSError, UnicodeDecodeError) as exc:
        print("cq: %s" % exc, file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("cq: internal error: %s" % exc, file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
