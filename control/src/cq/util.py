"""Small shared helpers. No policy lives here."""
from __future__ import annotations

import datetime
import functools
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, List, Optional


class CqError(Exception):
    """A user-facing error: bad input, bad config, or a refused operation."""


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt: Optional[datetime.datetime] = None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime.datetime:
    """Parse the timestamp shapes GitLab, SonarQube, git and cq itself emit."""
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    # SonarQube writes +0200; Python 3.9 fromisoformat wants +02:00.
    m = re.search(r"([+-]\d{2})(\d{2})$", t)
    if m and not re.search(r"[+-]\d{2}:\d{2}$", t):
        t = t[: m.start()] + m.group(1) + ":" + m.group(2)
    # Trim fractional seconds to 6 digits for older Pythons.
    t = re.sub(r"\.(\d{1,6})\d*", lambda mm: "." + mm.group(1).ljust(6, "0"), t)
    dt = datetime.datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


@functools.lru_cache(maxsize=None)
def glob_to_regex(glob: str) -> "re.Pattern[str]":
    """Translate a path glob. `*` and `?` stay inside one path segment;
    `**/` matches zero or more whole segments; a trailing `**` matches the rest."""
    out: List[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(glob: str, path: str) -> bool:
    return glob_to_regex(glob).match(path) is not None


def git(worktree: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",   # a file that is not UTF-8 must not make a run unusable
    )
    if check and proc.returncode != 0:
        raise CqError("git %s failed: %s" % (" ".join(args), proc.stderr.strip() or proc.stdout.strip()))
    return proc.stdout.strip()


def remote_branch_head(worktree: Path, remote: str, branch: str) -> str:
    """The commit `branch` points at on `remote`, or "". ls-remote matches ref
    tails, so `backup/cq/x` would also match `cq/x`: only the exact ref counts."""
    ref = "refs/heads/%s" % branch
    for line in git(worktree, "ls-remote", remote, ref, check=False).splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref:
            return sha.strip()
    return ""


def git_ok(worktree: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True).returncode == 0
