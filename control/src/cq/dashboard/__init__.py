"""`cq dashboard`: a read-only page on this machine for watching repos, merge
requests and runs.

It serves one HTML page and a small JSON API, on 127.0.0.1 only. Nothing on
the page changes anything: commands are shown for you to copy and run. The
page reads the run store on every request; GitLab is asked again in the
background every `dashboard.refresh_seconds`, so the page never waits on it.
"""
from __future__ import annotations

import base64
import datetime
import http.server
import json
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config, store
from ..util import CqError, iso, now, parse_time, read_json

PAGE = Path(__file__).with_name("page.html")
HERE = Path(__file__).parent
# Files the page loads from this server, by URL: two typefaces and the grey lamp lenses.
ASSETS = {
    "/fonts/recursive.woff2": (HERE / "fonts" / "recursive.woff2", "font/woff2"),
    "/fonts/tiny5.woff2": (HERE / "fonts" / "tiny5.woff2", "font/woff2"),
    "/lamps/lit.png": (HERE / "lamps" / "lit.png", "image/png"),
    "/lamps/off.png": (HERE / "lamps" / "off.png", "image/png"),
}
ACTIVE = ("RUNNING", "WAITING", "NEEDS_HUMAN", "FAILED")


def _hours(start: Optional[str], end: Optional[str]) -> Optional[float]:
    try:
        return round((parse_time(end) - parse_time(start)).total_seconds() / 3600, 2)
    except (TypeError, ValueError):
        return None


def run_row(run: store.Run) -> Dict[str, Any]:
    from ..engine import core, watch

    data = run.data
    rec = core.Engine.record(run)
    verdict = read_json(run.dir / "verdict.json", {})
    if rec:
        state, reason = core.live_state(run, rec)
    else:  # a run driven by hand with /cq-run
        state = "CLOSED" if data["status"] != "active" else "MANUAL"
        reason = None
    if data["status"] != "active" and state not in ("COMPLETED", "CANCELLED"):
        state = "CLOSED"
    last = None
    if state in ("RUNNING", "WAITING"):
        sessions = watch.session_rows(run.dir, tail=1)
        if sessions and sessions[-1]["lines"]:
            last = dict(sessions[-1]["lines"][-1], role=sessions[-1]["role"])
    started = rec.get("started_at") or data.get("created_at")
    updated = rec.get("updated_at") or verdict.get("evaluated_at") or started
    return {
        "run_id": run.dir.name, "ticket": data.get("ticket"), "summary": rec.get("summary"), "repo": data["repo_id"],
        "tier": data["tier"], "mode": data.get("mode"), "state": state, "stage": rec.get("stage"), "reason": reason,
        "engine": bool(rec), "verdict": verdict.get("verdict"), "cost_usd": rec.get("cost_usd", 0.0),
        "sessions": rec.get("sessions", 0), "fix_rounds": data.get("fix_rounds"), "started_at": started,
        "updated_at": updated, "hours": _hours(started, updated) if state == "COMPLETED" else None,
        "merge_request": (data.get("mr") or {}).get("web_url"), "mr_iid": (data.get("mr") or {}).get("iid"),
        "escalation": rec.get("escalation"), "ticket_url": rec.get("ticket_url"), "last_line": last,
        "branch": data.get("branch"), "head_sha": data.get("head_sha"),
    }


def run_detail(run_id: str) -> Dict[str, Any]:
    from ..engine import core, watch

    run = store.find_run(run_id)
    data = run.data
    rec = core.Engine.record(run)
    verdict = read_json(run.dir / "verdict.json", {})
    findings = []
    for f in run.findings().values():
        loc = f.get("location") or {}
        findings.append({
            "id": f["id"], "severity": f["severity"], "status": f["status"], "category": f.get("category"),
            "claim": f["claim"][:600], "where": ("%s:%s" % (loc.get("path"), loc.get("start_line") or "?")) if loc.get("path") else None,
            "raised_by": sorted({s["reviewer"] for s in f["sources"]}), "merged_into": f.get("merged_into"),
            "ticket": f.get("ticket"), "reason": (f.get("history") or [{}])[-1].get("reason"),
        })
    order = {"BLOCKER": 0, "MAJOR": 1, "UNTRIAGED": 2, "MINOR": 3, "NIT": 4}
    findings.sort(key=lambda f: (f["status"] not in ("unverified", "verified", "contested"), order.get(f["severity"], 9)))
    return {
        "run": run_row(run),
        "history": (rec.get("history") or [])[-60:],
        "verdict": {"verdict": verdict.get("verdict"), "stage": verdict.get("stage"), "human_action": verdict.get("human_action"),
                    "reasons": [{"verdict": r.get("verdict"), "message": r.get("message")} for r in verdict.get("reasons", [])]},
        "findings": findings,
        "sessions": watch.session_rows(run.dir, tail=400),
        "worktree": data.get("worktree"), "base_sha": data.get("base_sha"), "changed_files": len(data.get("changed_files") or []),
        "changed_lines": data.get("changed_lines"), "cost_by_role": rec.get("cost_by_role", {}),
    }


def _intake_questions(open_tickets: set) -> List[Dict[str, Any]]:
    """Tickets the screener sent back with a question in the last 30 days, with no run yet."""
    out = []
    folder = config.cq_home() / "intake"
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        item = read_json(path, {})
        screening = item.get("screening") or {}
        key = item.get("key") or path.stem
        if key in open_tickets or screening.get("result") != "needs_human":
            continue
        try:
            if (now() - parse_time(item["at"])).days > 30:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        out.append({"ticket": key, "question": screening.get("question"), "at": item.get("at")})
    return out


class Board:
    """The cached merge request inbox, refreshed in the background."""

    def __init__(self, settings: Dict[str, Any]):
        self.settings = settings
        self.fetched: Dict[str, Any] = {}
        self.fetched_at: Optional[str] = None
        self.error: Optional[str] = None
        self.refreshing = False
        self._lock = threading.Lock()
        self._wake = threading.Event()

    def refresh(self) -> None:
        from ..engine import inbox

        with self._lock:
            if self.refreshing:
                return
            self.refreshing = True
        try:
            fetched = inbox.fetch_all(inbox.with_mr())
            errors = [e for _, e in fetched.values() if e]
            with self._lock:
                self.fetched, self.fetched_at = fetched, iso()
                self.error = errors[0] if errors and len(errors) == len(fetched) else None
        except Exception as exc:  # the page shows it; the next refresh tries again
            with self._lock:
                self.error = str(exc)[:300]
        finally:
            with self._lock:
                self.refreshing = False

    def loop(self) -> None:
        while True:
            self.refresh()
            self._wake.wait(self.settings["refresh_seconds"])
            self._wake.clear()

    def poke(self) -> None:
        self._wake.set()


def state(board: Board) -> Dict[str, Any]:
    from ..engine import core

    from ..engine import inbox

    all_runs = store.list_runs()
    runs = [run_row(r) for r in all_runs]
    by_id = {r["run_id"]: r for r in runs}
    with board._lock:
        answers, fetched, error, refreshing = dict(board.fetched), board.fetched_at, board.error, board.refreshing
    mrs = inbox.rows_from(inbox.with_mr(all_runs), answers, True, board.settings["stale_days"])
    items: List[Dict[str, Any]] = []
    with_mr = set()
    for row in mrs:
        if (row.get("mr") or {}).get("state") in ("merged", "closed"):
            continue
        with_mr.add(row["run_id"])
        run = by_id.get(row["run_id"], {})
        items.append({"kind": "mr", "who": row["next"]["who"], "action": row["next"]["action"],
                      "command": row["next"].get("command"), "auto": row["next"].get("auto"), "flags": row["flags"],
                      "ticket": row["ticket"], "title": run.get("summary") or (row.get("mr") or {}).get("title"),
                      "repo": row["repo"], "tier": row["tier"], "run_id": row["run_id"], "mr": row["mr"],
                      "state": run.get("state") or row.get("engine_state"), "idle_days": row.get("idle_days"),
                      "last_line": run.get("last_line"), "threads": row.get("threads", [])})
    for run in runs:
        if run["run_id"] in with_mr or run["state"] not in ACTIVE:
            continue
        if run["mr_iid"] and fetched is None:
            continue  # its merge request row arrives with the first GitLab refresh
        who = "cq" if run["state"] in ("RUNNING", "WAITING") else "you"
        command = ("cq engine watch %s" % run["run_id"]) if run["state"] == "RUNNING" else ("cq engine resume %s" % run["run_id"])
        items.append({"kind": "run", "who": who, "action": run["reason"] or "cq is working (%s)." % (run["stage"] or "-"),
                      "command": command, "auto": run["state"] == "WAITING", "flags": [], "ticket": run["ticket"],
                      "title": run["summary"], "repo": run["repo"], "tier": run["tier"], "run_id": run["run_id"],
                      "mr": None, "state": run["state"], "idle_days": None, "last_line": run["last_line"], "threads": []})
    open_tickets = {r["ticket"] for r in runs if r["ticket"]}
    for q in _intake_questions(open_tickets):
        items.append({"kind": "ticket", "who": "you", "action": "Answer the screener's question in Jira, then add the ready label again.",
                      "command": None, "auto": False, "flags": [], "ticket": q["ticket"], "title": q["question"],
                      "repo": None, "tier": None, "run_id": None, "mr": None, "state": "QUESTION", "idle_days": None,
                      "last_line": None, "threads": []})

    try:
        registry = config.load_control(config.control_dir())["registry"]["repositories"]
    except CqError:
        registry = []
    month_ago = now() - datetime.timedelta(days=30)
    repos = []
    for repo in registry:
        mine = [r for r in runs if r["repo"] == repo["id"]]
        recent = [r for r in mine if r["updated_at"] and parse_time(r["updated_at"]) >= month_ago]
        finished = [r for r in recent if r["state"] in ("COMPLETED", "NEEDS_HUMAN")]
        repos.append({
            "id": repo["id"], "gitlab_project": repo.get("gitlab_project"), "jira_projects": repo.get("jira_projects", []),
            "active": sum(1 for r in mine if r["state"] in ACTIVE),
            "open_mrs": sum(1 for i in items if i["repo"] == repo["id"] and i["kind"] == "mr"),
            "needs_you": sum(1 for i in items if i["repo"] == repo["id"] and i["who"] == "you"),
            "completed_30d": sum(1 for r in recent if r["state"] == "COMPLETED"),
            "escalation_rate_30d": round(sum(1 for r in finished if r["state"] == "NEEDS_HUMAN") / len(finished), 3) if finished else None,
            "cost_usd_30d": round(sum(r["cost_usd"] for r in recent), 2),
            "last_activity": max((r["updated_at"] for r in mine if r["updated_at"]), default=None),
        })
    try:
        stats = core.stats()
    except CqError:
        stats = {}
    return {"generated_at": iso(), "items": items, "runs": runs, "repos": repos, "stats": stats,
            "mrs_fetched_at": fetched, "mrs_error": error, "mrs_refreshing": refreshing,
            "refresh_seconds": board.settings["refresh_seconds"]}


def render_snapshot(board: Board) -> str:
    """The page with its data inside, for a file you can open anywhere."""
    board.refresh()
    snapshot = state(board)
    snapshot["details"] = {r["run_id"]: run_detail(r["run_id"]) for r in snapshot["runs"] if r["state"] in ACTIVE}
    # Inside <script>, no "<", ">" or "&" may appear raw: "<!--" and "</script" both change how HTML reads the rest.
    blob = json.dumps(snapshot).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    page = PAGE.read_text(encoding="utf-8").replace("/*CQ_SNAPSHOT*/null", blob, 1)
    # The snapshot carries its typeface too, so it looks the same opened from anywhere.
    return inline_assets(page)


def inline_assets(page: str) -> str:
    """The page with every asset it loads written into it, for a file that works anywhere."""
    for url, (path, kind) in ASSETS.items():
        data = 'url("data:%s;base64,%s")' % (kind, base64.b64encode(path.read_bytes()).decode("ascii"))
        page = page.replace('url("%s")' % url, data)
    return page


def make_handler(board: Board, port: int):
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "cq-dashboard"

        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data:; font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            # A page on another site could point a browser here (DNS rebinding); only answer our own name.
            if self.headers.get("Host") not in allowed_hosts:
                return self._send(421, b"wrong host", "text/plain")
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            try:
                if url.path == "/":
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if url.path in ASSETS:
                    path, kind = ASSETS[url.path]
                    return self._send(200, path.read_bytes(), kind)
                if url.path == "/api/state":
                    if query.get("refresh") == "1":
                        board.poke()
                    return self._json(state(board))
                if url.path.startswith("/api/runs/"):
                    return self._json(run_detail(urllib.parse.unquote(url.path[len("/api/runs/"):])))
                return self._json({"error": "not found"}, 404)
            except CqError as exc:
                return self._json({"error": str(exc)}, 404)
            except Exception as exc:  # keep serving; show the problem on the page
                return self._json({"error": "internal error: %r" % exc}, 500)

        def do_POST(self) -> None:  # noqa: N802
            self._send(405, b"read-only", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def serve(port: int, settings: Dict[str, Any], open_browser: bool = False) -> int:
    board = Board(settings)
    threading.Thread(target=board.loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(board, port))
    url = "http://127.0.0.1:%d/" % port
    print("cq dashboard on %s (this machine only). Ctrl-C to stop." % url, flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
