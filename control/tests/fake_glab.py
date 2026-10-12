#!/usr/bin/env python3
"""A stand-in for the glab CLI, used by the tests. It serves a small GitLab from
a JSON state file and records every call it receives."""
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.parse

path = os.environ["FAKE_GLAB_STATE"]
# Calls can run in parallel (the inbox reads several merge requests at once). One at a time here.
_lock = open(path + ".lock", "w")
fcntl.flock(_lock, fcntl.LOCK_EX)
with open(path) as fh:
    st = json.load(fh)
args = sys.argv[1:]


def save():
    with open(path, "w") as fh:
        json.dump(st, fh)


def out(obj):
    print(json.dumps(obj))
    save()
    sys.exit(0)


def fail(msg):
    sys.stderr.write(msg + "\n")
    save()
    sys.exit(1)


if args[:2] == ["auth", "status"]:
    sys.exit(0)
if args[0] != "api":
    fail("fake glab: unsupported command " + " ".join(args))
url, method, fields = args[1], "GET", {}
i = 2
while i < len(args):
    if args[i] == "-X":
        method = args[i + 1]
        i += 2
    elif args[i] in ("-f", "-F"):
        key, value = args[i + 1].split("=", 1)
        if args[i] == "-F" and value.startswith("@"):
            with open(value[1:]) as fh:
                value = fh.read()
        fields[key] = value
        i += 2
    else:
        i += 1
st["calls"].append({"method": method, "url": url, "fields": fields})
if st.get("fail_all"):
    fail("glab: HTTP 500")

u = urllib.parse.urlsplit(url)
q = dict(urllib.parse.parse_qsl(u.query))
p = u.path


def page(items):
    n, per = int(q.get("page", 1)), int(q.get("per_page", 20))
    return items[(n - 1) * per: n * per]


if p == "projects/:fullpath":
    out({"id": 7, "path_with_namespace": st["project"]})
if p == "projects/7":
    out({"id": 7, "path_with_namespace": st["project"], "merge_method": st.get("merge_method", "merge")})
if p == "projects/7/merge_requests":
    if method == "POST":
        iid = 12 + len(st["mrs"])
        mr = {
            "iid": iid, "web_url": "https://gitlab.example/group/demo/-/merge_requests/%d" % iid,
            "source_branch": fields["source_branch"], "target_branch": fields["target_branch"],
            "title": fields["title"], "description": fields.get("description", ""), "sha": st["head"],
        }
        st["mrs"].append(mr)
        out(mr)
    out(page([m for m in st["mrs"] if m["source_branch"] == q.get("source_branch")]))
m = re.match(r"projects/7/merge_requests/(\d+)(.*)$", p)
if not m:
    fail("fake glab: unhandled " + url)
mr = next(x for x in st["mrs"] if x["iid"] == int(m.group(1)))
rest = m.group(2)


def remote_head():
    """With FAKE_GLAB_REMOTE set, the merge request follows the real pushed branch."""
    remote = os.environ.get("FAKE_GLAB_REMOTE")
    if not remote:
        return st["head"]
    proc = subprocess.run(["git", "--git-dir", remote, "rev-parse", "refs/heads/" + mr["source_branch"]],
                          capture_output=True, text=True)
    return proc.stdout.strip() or st["head"]


def duo_answers():
    """Duo answers a request on the first discussions read after it: it posts the
    next scripted threads (maybe none) and its reviewer state becomes reviewed."""
    due = st.pop("duo_due", None)
    if due is None:
        return
    when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for i, body in enumerate(due):
        st["discussions"].append({"id": "duo-%d-%d" % (len(st["discussions"]), i), "individual_note": False, "notes": [{
            "id": 100 + len(st["discussions"]), "body": body, "author": {"username": "GitLabDuo"}, "system": False,
            "resolvable": True, "resolved": False, "created_at": when,
            "position": {"new_path": "src/app.py", "new_line": 1, "head_sha": remote_head()}}]})
    st["reviewers"] = [{"user": {"username": "GitLabDuo"}, "state": "reviewed"}]


if rest == "":
    if method == "PUT":
        mr.update(fields)
        out(mr)
    mr["sha"] = remote_head()
    for key, value in (("state", "opened"), ("draft", True), ("has_conflicts", False), ("diverged_commits_count", 0),
                       ("head_pipeline", None), ("detailed_merge_status", "draft_status"),
                       ("updated_at", datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))):
        mr.setdefault(key, value)
    mr.update(st.get("mr_overrides", {}))
    out(mr)
if rest == "/approvals":
    out(st.get("approvals", {"approvals_required": 0, "approvals_left": 0, "approved_by": []}))
if rest == "/reviewers":
    out(st["reviewers"])
if rest == "/discussions":
    if q.get("page", "1") == "1":
        duo_answers()
    out(page(st["discussions"]))
if rest == "/notes" and method == "POST":
    if fields["body"].startswith("/assign_reviewer") and st.get("duo_accepts", True):
        st["reviewers"] = [{"user": {"username": "GitLabDuo"}, "state": "unreviewed"}]
        if "duo_script" in st:
            script = st["duo_script"]
            st["duo_due"] = script.pop(0) if script else []
    out({"id": 1})
m2 = re.match(r"/discussions/([^/]+)(/notes)?$", rest)
if not m2:
    fail("fake glab: unhandled " + url)
d = next(x for x in st["discussions"] if x["id"] == m2.group(1))
if m2.group(2):
    d["notes"].append({
        "id": 999, "body": fields["body"], "author": {"username": "me"}, "system": False,
        "resolvable": True, "resolved": False, "created_at": "2026-10-10T00:00:00Z",
    })
    out({"id": 999})
if method == "PUT" and q.get("resolved") == "true":
    for n in d["notes"]:
        if n.get("resolvable"):
            n["resolved"] = True
    out(d)
fail("fake glab: unhandled " + url)
