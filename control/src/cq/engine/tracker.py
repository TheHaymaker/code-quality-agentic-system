"""Tickets in and filings out: Jira over REST, or a folder of Markdown files.

The engine only needs a few operations. Every write the engine repeats on a
resume is made idempotent with a label: before creating an issue it looks for
one that already carries the run's or the finding's label.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..util import CqError


class TrackerError(CqError):
    pass


def project_of(key: str) -> str:
    return key.rsplit("-", 1)[0]


class Tracker:
    def get(self, key: str) -> Dict[str, Any]:
        raise NotImplementedError

    def search(self, projects: List[str], with_label: str, without_labels: List[str]) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def comment(self, key: str, body: str) -> None:
        raise NotImplementedError

    def labels(self, key: str, add: List[str] = (), remove: List[str] = ()) -> None:
        raise NotImplementedError

    def find_by_label(self, project: str, label: str) -> Optional[str]:
        raise NotImplementedError

    def create(self, project: str, summary: str, description: str, labels: List[str]) -> str:
        raise NotImplementedError

    def link(self, key: str, other: str) -> None:
        raise NotImplementedError

    def web_link(self, key: str, url: str, title: str) -> None:
        raise NotImplementedError

    def create_once(self, project: str, label: str, summary: str, description: str, labels: List[str]) -> str:
        """Create an issue unless one with `label` already exists. Returns its key."""
        existing = self.find_by_label(project, label)
        if existing:
            return existing
        return self.create(project, summary, description, sorted(set(labels) | {label}))


# -- Jira -------------------------------------------------------------------

class JiraTracker(Tracker):
    def __init__(self, settings: Dict[str, Any]):
        self.base = settings["base_url"].rstrip("/")
        self.auth = settings.get("auth", "basic")
        token = os.environ.get(settings["token_env"], "")
        if not token:
            raise TrackerError("environment variable %s (the Jira token) is not set" % settings["token_env"])
        if self.auth == "basic":
            email = os.environ.get(settings["email_env"], "")
            if not email:
                raise TrackerError("environment variable %s (the Jira account email) is not set" % settings["email_env"])
            self.header = "Basic " + base64.b64encode(("%s:%s" % (email, token)).encode()).decode()
        else:
            self.header = "Bearer " + token
        self.issue_type = settings.get("issue_type", "Task")
        # Jira Cloud: /rest/api/3/search/jql. Data Center: the older /rest/api/2/search.
        self.search_api = settings.get("search_api", "jql")

    def _call(self, method: str, path: str, body: Any = None, params: Optional[Dict[str, str]] = None) -> Any:
        url = self.base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(5):
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", self.header)
            request.add_header("Accept", "application/json")
            if data is not None:
                request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read().decode("utf-8", "replace")
                try:
                    return json.loads(raw) if raw.strip() else None
                except ValueError:
                    # For example a single sign-on page in front of Jira.
                    raise TrackerError("Jira %s %s did not answer with JSON: %s" % (method, path, raw.strip()[:200]))
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                if exc.code in (429, 502, 503, 504) and attempt < 4:
                    wait = float(exc.headers.get("Retry-After") or 0)
                    time.sleep(max(wait, (2 ** attempt) * random.uniform(0.7, 1.3)))
                    continue
                raise TrackerError("Jira %s %s returned %d: %s" % (method, path, exc.code, text[:300]))
            except (urllib.error.URLError, OSError) as exc:
                if attempt < 4:
                    time.sleep((2 ** attempt) * random.uniform(0.7, 1.3))
                    continue
                raise TrackerError("Jira %s %s failed: %s" % (method, path, exc))
        raise TrackerError("Jira %s %s: too many retries" % (method, path))

    def get(self, key: str) -> Dict[str, Any]:
        # v2 returns the description as text; v3 would return a document tree.
        issue = self._call("GET", "/rest/api/2/issue/%s" % urllib.parse.quote(key),
                           params={"fields": "summary,description,labels,status,project"})
        fields = issue.get("fields") or {}
        return {
            "key": issue["key"],
            "project": ((fields.get("project") or {}).get("key")) or project_of(issue["key"]),
            "summary": fields.get("summary") or "",
            "description": fields.get("description") or "",
            "labels": fields.get("labels") or [],
            "url": "%s/browse/%s" % (self.base, issue["key"]),
        }

    def _jql(self, jql: str) -> List[str]:
        if self.search_api == "legacy":
            return self._jql_legacy(jql)
        keys: List[str] = []
        token = None
        for _ in range(50):
            params = {"jql": jql, "fields": "key", "maxResults": "100"}
            if token:
                params["nextPageToken"] = token
            page = self._call("GET", "/rest/api/3/search/jql", params=params)
            keys += [i["key"] for i in page.get("issues", [])]
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                return keys
        raise TrackerError("JQL search returned more than 50 pages")

    def _jql_legacy(self, jql: str) -> List[str]:
        keys: List[str] = []
        for _ in range(50):
            params = {"jql": jql, "fields": "key", "maxResults": "100", "startAt": str(len(keys))}
            page = self._call("GET", "/rest/api/2/search", params=params)
            issues = page.get("issues", [])
            keys += [i["key"] for i in issues]
            if not issues or len(keys) >= int(page.get("total", 0)):
                return keys
        raise TrackerError("JQL search returned more than 50 pages")

    def search(self, projects: List[str], with_label: str, without_labels: List[str]) -> List[Dict[str, Any]]:
        if not projects:
            return []
        jql = 'project in (%s) AND labels = "%s" AND statusCategory != Done' % (
            ", ".join('"%s"' % p for p in projects), with_label)
        for label in without_labels:
            jql += ' AND (labels is EMPTY OR labels != "%s")' % label
        jql += " ORDER BY priority DESC, created ASC"
        return [self.get(k) for k in self._jql(jql)]

    def comment(self, key: str, body: str) -> None:
        self._call("POST", "/rest/api/2/issue/%s/comment" % key, {"body": body})

    def labels(self, key: str, add: List[str] = (), remove: List[str] = ()) -> None:
        ops = [{"add": l} for l in add] + [{"remove": l} for l in remove]
        if ops:
            self._call("PUT", "/rest/api/2/issue/%s" % key, {"update": {"labels": ops}})

    def find_by_label(self, project: str, label: str) -> Optional[str]:
        keys = self._jql('project = "%s" AND labels = "%s" ORDER BY created ASC' % (project, label))
        return keys[0] if keys else None

    def create(self, project: str, summary: str, description: str, labels: List[str]) -> str:
        issue = self._call("POST", "/rest/api/2/issue", {"fields": {
            "project": {"key": project}, "summary": summary[:250], "description": description,
            "issuetype": {"name": self.issue_type}, "labels": labels,
        }})
        return issue["key"]

    def link(self, key: str, other: str) -> None:
        self._call("POST", "/rest/api/2/issueLink",
                   {"type": {"name": "Relates"}, "inwardIssue": {"key": key}, "outwardIssue": {"key": other}})

    def web_link(self, key: str, url: str, title: str) -> None:
        # The global id makes a repeated call update the link instead of adding a second one.
        self._call("POST", "/rest/api/2/issue/%s/remotelink" % key,
                   {"globalId": "cq:" + url, "object": {"url": url, "title": title[:250]}})


# -- Files ------------------------------------------------------------------

class FileTracker(Tracker):
    """Tickets as Markdown files with a small YAML header:

        ---
        summary: Clamp the page number
        labels: [agent-ready]
        ---
        As a shopper ...

    Comments go to <KEY>.comments.md; new issues are written as new files."""

    def __init__(self, settings: Dict[str, Any]):
        self.dir = Path(settings["dir"]).expanduser()
        if not self.dir.is_dir():
            raise TrackerError("ticket folder %s does not exist" % self.dir)

    def _path(self, key: str) -> Path:
        if not re.match(r"^[A-Z][A-Z0-9_]*-\d+$", key):
            raise TrackerError("'%s' is not a ticket key" % key)
        return self.dir / ("%s.md" % key)

    def _read(self, key: str) -> Dict[str, Any]:
        path = self._path(key)
        if not path.exists():
            raise TrackerError("no ticket %s in %s" % (key, self.dir))
        text = path.read_text(encoding="utf-8")
        meta: Dict[str, Any] = {}
        body = text
        if text.startswith("---\n") and "\n---" in text[4:]:
            head, body = text[4:].split("\n---", 1)
            meta = yaml.safe_load(head) or {}
            body = body.lstrip("\n")
        return {"meta": meta, "body": body}

    def _write(self, key: str, meta: Dict[str, Any], body: str) -> None:
        self._path(key).write_text("---\n%s---\n%s" % (yaml.safe_dump(meta, sort_keys=True), body), encoding="utf-8")

    def get(self, key: str) -> Dict[str, Any]:
        doc = self._read(key)
        return {"key": key, "project": project_of(key), "summary": str(doc["meta"].get("summary", "")),
                "description": doc["body"], "labels": list(doc["meta"].get("labels") or []), "url": str(self._path(key))}

    def search(self, projects: List[str], with_label: str, without_labels: List[str]) -> List[Dict[str, Any]]:
        out = []
        for path in sorted(self.dir.glob("*.md")):
            key = path.stem
            if key.endswith(".comments") or project_of(key) not in projects:
                continue
            ticket = self.get(key)
            if with_label in ticket["labels"] and not set(without_labels) & set(ticket["labels"]):
                out.append(ticket)
        return out

    def comment(self, key: str, body: str) -> None:
        self._path(key)
        with open(self.dir / ("%s.comments.md" % key), "a", encoding="utf-8") as fh:
            fh.write("\n---\n%s\n" % body.strip())

    def labels(self, key: str, add: List[str] = (), remove: List[str] = ()) -> None:
        doc = self._read(key)
        labels = [l for l in (doc["meta"].get("labels") or []) if l not in remove]
        labels += [l for l in add if l not in labels]
        doc["meta"]["labels"] = labels
        self._write(key, doc["meta"], doc["body"])

    def find_by_label(self, project: str, label: str) -> Optional[str]:
        for path in sorted(self.dir.glob("%s-*.md" % project)):
            if path.stem.endswith(".comments"):
                continue
            if label in self.get(path.stem)["labels"]:
                return path.stem
        return None

    def create(self, project: str, summary: str, description: str, labels: List[str]) -> str:
        numbers = [int(p.stem.rsplit("-", 1)[1]) for p in self.dir.glob("%s-*.md" % project)
                   if re.match(r"^\d+$", p.stem.rsplit("-", 1)[1])]
        key = "%s-%d" % (project, max(numbers or [0]) + 1)
        self._write(key, {"summary": summary, "labels": labels}, description + "\n")
        return key

    def link(self, key: str, other: str) -> None:
        self.comment(key, "Related: %s" % other)

    def web_link(self, key: str, url: str, title: str) -> None:
        self.comment(key, "Link: %s (%s)" % (title, url))


def make(settings: Dict[str, Any]) -> Tracker:
    tracker = settings["tracker"]
    return JiraTracker(tracker) if tracker["kind"] == "jira" else FileTracker(tracker)
