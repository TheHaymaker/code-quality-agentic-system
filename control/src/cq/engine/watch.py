"""Reading agent sessions as they work.

Each session writes `agents/<time>-<role>-<attempt>.stream.jsonl` in its run
folder (see agents.py). `cq engine watch` follows those files and prints one
line per step: what the agent said, which tool it ran, what failed. The
dashboard reads the same files through `session_rows` and `render`.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

from .. import store
from ..util import parse_time

MAX_TEXT = 180


def stream_files(run_dir: Path) -> List[Path]:
    adir = run_dir / "agents"
    return sorted(adir.glob("*.stream.jsonl")) if adir.is_dir() else []


def _first_line(text: Any, limit: int = MAX_TEXT) -> str:
    if isinstance(text, list):  # tool results can be a list of content blocks
        text = " ".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in text)
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def _tool_arg(name: str, args: Dict[str, Any], cwd: Optional[str]) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("file_path", "notebook_path", "path"):
        if args.get(key) and name not in ("Grep", "Glob"):
            path = str(args[key])
            if cwd and path.startswith(cwd.rstrip("/") + "/"):
                path = path[len(cwd.rstrip("/")) + 1:]
            return path
    if name == "Bash":
        return _first_line(args.get("command"), 140)
    if name in ("Grep", "Glob"):
        where = args.get("path")
        return "%s%s" % (args.get("pattern", ""), (" in %s" % where) if where else "")
    if name == "Skill":
        return str(args.get("skill") or args.get("command") or "")
    if args.get("description"):
        return _first_line(args["description"], 100)
    return _first_line(json.dumps(args, ensure_ascii=False), 100)


def render(event: Dict[str, Any], cwd: Optional[str] = None) -> List[Tuple[str, str]]:
    """The readable lines for one stream event, as (kind, text). Kinds: start,
    say, tool, error, done, failed. Events with nothing worth a line give none."""
    kind = event.get("type")
    out: List[Tuple[str, str]] = []
    if kind == "cq_session":
        bits = [str(event.get("model") or "?"), str(event.get("mode") or "")]
        if event.get("scope"):
            bits.append("%s review" % event["scope"])
        if (event.get("attempt") or 1) > 1:
            bits.append("attempt %d" % event["attempt"])
        out.append(("start", "started · %s" % " · ".join(b for b in bits if b)))
    elif kind == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and _first_line(block.get("text")):
                out.append(("say", _first_line(block.get("text"))))
            elif block.get("type") == "tool_use":
                name = str(block.get("name") or "?")
                out.append(("tool", ("%s %s" % (name, _tool_arg(name, block.get("input") or {}, cwd))).strip()))
    elif kind == "user":
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                out.append(("error", _first_line(block.get("content")) or "tool error"))
    elif kind == "cq_end":
        if event.get("ok"):
            bits = []
            if event.get("cost_usd"):
                bits.append("$%.2f" % event["cost_usd"])
            if event.get("duration_seconds") is not None:
                bits.append("%ss" % int(event["duration_seconds"]))
            if event.get("findings") is not None:
                bits.append("%d finding(s)" % event["findings"])
            if event.get("session_id"):
                bits.append("claude --resume %s" % event["session_id"])
            out.append(("done", " · ".join(["done"] + bits)))
        else:
            out.append(("failed", "failed: %s" % _first_line(event.get("error"), 300)))
    elif kind == "cq_text":
        out.append(("say", _first_line(event.get("text"))))
    return out


def read_lines(path: Path, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """Complete lines after `offset`, parsed, and the new offset. A line still
    being written is left for the next read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    items = []
    for raw in data[: end + 1].splitlines():
        try:
            items.append(json.loads(raw.decode("utf-8", "replace")))
        except ValueError:
            continue
    return items, offset + end + 1


def session_rows(run_dir: Path, tail: int = 0) -> List[Dict[str, Any]]:
    """One row per session: who, model, when, how it ended, and (with `tail`) its last readable lines."""
    rows = []
    for path in stream_files(run_dir):
        items, _ = read_lines(path)
        if not items:
            continue
        head = items[0].get("event") or {}
        row: Dict[str, Any] = {
            "file": path.name, "role": head.get("role") or path.stem, "model": head.get("model"),
            "mode": head.get("mode"), "attempt": head.get("attempt", 1), "started_at": items[0].get("at"),
            "ended_at": None, "ok": None, "error": None, "cost_usd": None, "session_id": head.get("session_id"),
            "steps": 0, "lines": [],
        }
        lines: List[Dict[str, str]] = []
        for item in items:
            event = item.get("event") or {}
            if event.get("type") == "cq_end":
                row.update(ended_at=item.get("at"), ok=bool(event.get("ok")), error=event.get("error"),
                           cost_usd=event.get("cost_usd"))
            if event.get("type") == "system" and event.get("session_id") and not row["session_id"]:
                row["session_id"] = event["session_id"] if head.get("session_id") else None
            for kind, text in render(event, head.get("cwd")):
                if kind == "tool":
                    row["steps"] += 1
                lines.append({"at": item.get("at") or "", "kind": kind, "text": text})
        row["lines"] = lines[-tail:] if tail else []
        rows.append(row)
    return rows


def _clock(at: str) -> str:
    try:
        return parse_time(at).astimezone().strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "--:--:--"


MARKS = {"start": "▶", "say": " ", "tool": "▸", "error": "✗", "done": "■", "failed": "■"}


def follow(run_ids: Optional[List[str]], once: bool, out: Optional[TextIO] = None, poll: float = 0.5,
           backlog: int = 40) -> int:
    """Print sessions as they work. With run ids, follow those runs; without,
    every active run, including runs that start while watching."""
    out = out or sys.stdout
    offsets: Dict[Path, int] = {}
    heads: Dict[Path, Dict[str, Any]] = {}
    first = True
    try:
        while True:
            runs = [store.find_run(r) for r in run_ids] if run_ids else \
                [r for r in store.list_runs() if r.data["status"] == "active"]
            label_ticket = not run_ids or len(run_ids) > 1
            pending: List[Tuple[str, str]] = []
            for run in runs:
                for path in stream_files(run.dir):
                    items, offsets[path] = read_lines(path, offsets.get(path, 0))
                    for item in items:
                        event = item.get("event") or {}
                        if event.get("type") == "cq_session":
                            heads[path] = event
                        head = heads.get(path, {})
                        role = str(head.get("role") or "?")
                        label = ("%s %s" % (run.data["ticket"] or run.dir.name[:12], role)) if label_ticket else role
                        for kind, text in render(event, head.get("cwd")):
                            pending.append((item.get("at") or "", "%s  %-28s %s %s" % (
                                _clock(item.get("at") or ""), label[:28], MARKS.get(kind, " "), text)))
            pending.sort(key=lambda p: p[0])
            if first and backlog and not once:
                pending = pending[-backlog:]
            for _, line in pending:
                out.write(line + "\n")
            out.flush()
            first = False
            if once:
                return 0
            time.sleep(poll)
    except KeyboardInterrupt:
        return 0
