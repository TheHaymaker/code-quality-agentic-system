"""The run store. It lives outside every worktree, so run records never appear
in the diff under review. One directory per run:

    run.json            the run record
    events.jsonl        append-only history of every change
    policy/             the config this run pinned when it started
    checks/             one result and one log per check execution
    envelopes/          each reviewer submission, as stored
    findings.json       the merged findings
    external/           Duo and SonarQube evidence
    approvals.json      human approvals
    notes/              plan, plan review, rollback, escalations
    verdict.json        the last policy decision
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import config
from .util import CqError, atomic_write, iso, read_json, sha256_text, write_json


# Lock depth per run, per thread: re-entrant within a thread, exclusive across
# threads and processes (each thread opens its own lock file description).
_tls = threading.local()


def _held() -> Dict[str, int]:
    if not hasattr(_tls, "held"):
        _tls.held = {}
    return _tls.held


def runs_dir() -> Path:
    return config.cq_home() / "runs"


class Run:
    def __init__(self, path: Path):
        self.dir = path

    # -- record ---------------------------------------------------------
    @property
    def data(self) -> Dict[str, Any]:
        data = read_json(self.dir / "run.json")
        if data is None:
            raise CqError("run record is missing in %s" % self.dir)
        return data

    def save(self, data: Dict[str, Any]) -> None:
        write_json(self.dir / "run.json", data)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """One writer per run at a time. Safe to nest within one process."""
        key = str(self.dir)
        held = _held()
        if held.get(key):
            held[key] += 1
            try:
                yield
            finally:
                held[key] -= 1
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / ".lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            held[key] = 1
            try:
                yield
            finally:
                held[key] = 0
                fcntl.flock(fh, fcntl.LOCK_UN)

    def event(self, kind: str, data: Optional[Dict[str, Any]] = None, actor: Optional[str] = None) -> None:
        path = self.dir / "events.jsonl"
        seq = 1
        if path.exists():
            with open(path, "rb") as fh:
                seq = sum(1 for _ in fh) + 1
        record = {
            "seq": seq,
            "at": iso(),
            "actor": actor or os.environ.get("CQ_ACTOR", "agent"),
            "type": kind,
            "data": data or {},
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def events(self) -> List[Dict[str, Any]]:
        path = self.dir / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # -- pinned policy --------------------------------------------------
    def pin_policy(self, control: Dict[str, Any], repo: Dict[str, Any], criticality_text: str) -> str:
        pdir = self.dir / "policy"
        repo_text = json.dumps(repo, indent=2, sort_keys=True) + "\n"
        atomic_write(pdir / "floor.yml", control["floor_text"])
        atomic_write(pdir / "models.yml", control["models_text"])
        atomic_write(pdir / "repo.json", repo_text)
        atomic_write(pdir / "criticality.yml", criticality_text)
        return sha256_text("\x00".join([control["floor_text"], control["models_text"], repo_text, criticality_text]))

    def policy(self) -> Dict[str, Any]:
        pdir = self.dir / "policy"
        if not (pdir / "floor.yml").exists():
            raise CqError("run %s has no pinned policy" % self.dir.name)
        return {
            "floor": config.parse_floor((pdir / "floor.yml").read_text(encoding="utf-8"), "pinned floor.yml"),
            "models": config.parse_models((pdir / "models.yml").read_text(encoding="utf-8"), "pinned models.yml"),
            "repo": read_json(pdir / "repo.json"),
            "repo_rules": config.parse_criticality(
                (pdir / "criticality.yml").read_text(encoding="utf-8"), "pinned .cq/criticality.yml"
            ),
        }

    # -- evidence -------------------------------------------------------
    def findings(self) -> Dict[str, Any]:
        return read_json(self.dir / "findings.json", {})

    def save_findings(self, findings: Dict[str, Any]) -> None:
        write_json(self.dir / "findings.json", findings)

    def envelopes(self) -> List[Dict[str, Any]]:
        edir = self.dir / "envelopes"
        if not edir.exists():
            return []
        return [read_json(p) for p in sorted(edir.glob("*.json"))]

    def save_envelope(self, envelope: Dict[str, Any]) -> Path:
        src = envelope["source"]
        name = "%s@%s.%s.json" % (src["reviewer"], envelope["revision"]["head_sha"][:12], envelope["scope"])
        path = self.dir / "envelopes" / name
        write_json(path, envelope)
        return path

    def checks(self) -> Dict[str, Dict[str, Any]]:
        """The latest result per check name."""
        cdir = self.dir / "checks"
        latest: Dict[str, Dict[str, Any]] = {}
        if cdir.exists():
            for p in sorted(cdir.glob("*.json")):
                rec = read_json(p)
                prev = latest.get(rec["name"])
                if prev is None or rec.get("finished_ns", 0) > prev.get("finished_ns", 0):
                    latest[rec["name"]] = rec
        return latest

    def external(self) -> Dict[str, Any]:
        out = {}
        for name in config.EXTERNAL:
            rec = read_json(self.dir / "external" / (name + ".json"))
            if rec is not None:
                out[name] = rec
        return out

    def save_external(self, name: str, record: Dict[str, Any]) -> None:
        write_json(self.dir / "external" / (name + ".json"), record)

    def approvals(self) -> List[Dict[str, Any]]:
        return read_json(self.dir / "approvals.json", [])

    def notes(self) -> Dict[str, Dict[str, Any]]:
        return read_json(self.dir / "notes" / "index.json", {})


def find_run(run_id: str) -> Run:
    root = runs_dir()
    exact = root / run_id
    if exact.is_dir():
        return Run(exact)
    matches = [p for p in root.glob(run_id + "*") if p.is_dir()] if root.exists() else []
    if len(matches) == 1:
        return Run(matches[0])
    if not matches:
        raise CqError("no run matches '%s' in %s" % (run_id, root))
    raise CqError("'%s' matches several runs: %s" % (run_id, ", ".join(sorted(p.name for p in matches))))


def list_runs() -> List[Run]:
    root = runs_dir()
    if not root.exists():
        return []
    return [Run(p) for p in sorted(root.iterdir()) if (p / "run.json").exists()]
