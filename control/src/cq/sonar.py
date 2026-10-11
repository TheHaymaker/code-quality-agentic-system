"""SonarQube adapter. The analysis runs in GitLab CI; this module only reads the
result. There is no local scanner fallback: a second analysis from another
environment would not match the one the merge request shows.

Five outcomes, kept apart:
    passed    an analysis of this merge request exists, is current, gate OK
    failed    an analysis exists, is current, gate not OK
    missing   no analysis, or no gate, for this merge request
    stale     the analysis is older than the head commit
    error     the result could not be fetched (network, auth, config)
Only `passed` is evidence. `failed` is a known failure. The rest are missing evidence.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import findings as F, store
from .util import CqError, git, iso, parse_time

PAGE_SIZE = 500
RESULT_CAP = 10000  # SonarQube refuses to page past this


def _get(cfg: Dict[str, Any], path: str, params: Dict[str, str]) -> Tuple[int, Any]:
    """Return (http status, parsed body). Status 0 means the request never got an answer."""
    token = os.environ.get(cfg.get("token_env", "SONAR_TOKEN"), "")
    if not token:
        return 0, "environment variable %s is not set" % cfg.get("token_env", "SONAR_TOKEN")
    url = "%s/%s?%s" % (cfg["host_url"].rstrip("/"), path, urllib.parse.urlencode(params))
    request = urllib.request.Request(url)
    if cfg.get("auth", "bearer") == "basic":
        request.add_header("Authorization", "Basic " + base64.b64encode((token + ":").encode()).decode())
    else:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
            message = "; ".join(e.get("msg", "") for e in parsed.get("errors", [])) or body[:300]
        except ValueError:
            message = body[:300]
        return exc.code, message
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(exc)


def _severity(issue: Dict[str, Any], floor: Dict[str, Any]) -> str:
    legacy = issue.get("severity")
    if legacy in floor["sonar_severity_map"]:
        return floor["sonar_severity_map"][legacy]
    mapped = [floor["sonar_impact_map"].get(i.get("severity")) for i in issue.get("impacts", [])]
    mapped = [m for m in mapped if m]
    if mapped:
        return max(mapped, key=F.rank)
    return "MAJOR"  # no severity in the response: do not guess low


def _category(issue: Dict[str, Any]) -> str:
    kinds = {issue.get("type")} | {i.get("softwareQuality") for i in issue.get("impacts", [])}
    if kinds & {"VULNERABILITY", "SECURITY", "SECURITY_HOTSPOT"}:
        return "security"
    if kinds & {"BUG", "RELIABILITY"}:
        return "correctness"
    return "structure"


def _issues(cfg: Dict[str, Any], pr: str) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """All unresolved issues on the pull request, or (None, why)."""
    project_param = "componentKeys" if cfg["product"] == "cloud" else "components"
    issues: List[Dict[str, Any]] = []
    page = 1
    while True:
        params = {project_param: cfg["project_key"], "pullRequest": pr, "resolved": "false", "ps": str(PAGE_SIZE), "p": str(page)}
        if cfg["product"] == "cloud" and cfg.get("organization"):
            params["organization"] = cfg["organization"]
        status, body = _get(cfg, "api/issues/search", params)
        if status == 400 and project_param == "components":
            project_param = "componentKeys"  # SonarQube Server before 10.2
            continue
        if status != 200:
            return None, "issue search returned %s: %s" % (status or "no answer", body)
        issues.extend(body.get("issues", []))
        total = (body.get("paging") or {}).get("total", len(issues))
        if len(issues) >= total or not body.get("issues"):
            return issues, ""
        if page * PAGE_SIZE >= RESULT_CAP:
            return None, "more than %d issues; refusing to read a partial result" % RESULT_CAP
        page += 1


def gate(run: store.Run) -> Dict[str, Any]:
    data = run.data
    policy = run.policy()
    cfg = policy["repo"].get("sonar")
    if cfg is None:
        raise CqError(
            "registry.yml has no sonar section for %s. Add one, or mark sonar not_applicable with a reason." % data["repo_id"]
        )
    if not data.get("mr"):
        raise CqError("this run has no merge request yet. Run `cq mr ensure` first.")
    head = data["head_sha"]
    pr = str(data["mr"]["iid"])
    evidence: Dict[str, Any] = {"head_sha": head, "pull_request": pr, "fetched_at": iso(), "freshness": None, "conditions": []}

    def finish(state: str, detail: str) -> Dict[str, Any]:
        evidence.update(state=state, detail=detail)
        with run.lock():
            run.save_external("sonar", evidence)
            run.event("sonar_checked", {"state": state, "head": head, "detail": detail})
        return evidence

    status, body = _get(cfg, "api/project_pull_requests/list", {"project": cfg["project_key"]})
    if status != 200:
        return finish("error", "pull request list returned %s: %s" % (status or "no answer", body))
    record = next((p for p in body.get("pullRequests", []) if str(p.get("key")) == pr), None)
    if record is None or not record.get("analysisDate"):
        return finish("missing", "SonarQube has no analysis for merge request !%s. Check the merge request pipeline." % pr)

    # Is the analysis for this head? SonarQube Cloud reports the commit on the
    # pull request. SonarQube Server may report it on the analysis record. When
    # neither does, the analysis time is compared with the commit time, which
    # is weaker, and the report says which rule was used.
    analysed_sha = (record.get("commit") or {}).get("sha")
    if not analysed_sha:
        status, body = _get(cfg, "api/project_analyses/search", {"project": cfg["project_key"], "pullRequest": pr, "ps": "1"})
        if status == 200 and isinstance(body, dict) and body.get("analyses"):
            analysed_sha = body["analyses"][0].get("revision")
    analysed_at = parse_time(record["analysisDate"])
    committed_at = parse_time(git(Path(data["worktree"]), "show", "-s", "--format=%cI", head))
    evidence["analysis_date"] = record["analysisDate"]
    evidence["analysed_sha"] = analysed_sha
    by_time = cfg.get("freshness", "commit") == "time"
    if analysed_sha == head:
        evidence["freshness"] = "commit"
    elif analysed_sha and not by_time:
        # SonarQube names the commit it analysed, and it is not this one.
        return finish("stale", "The last analysis is of another commit (%s)." % analysed_sha[:12])
    elif analysed_at >= committed_at:
        evidence["freshness"] = "time"
    else:
        return finish("stale", "The last analysis (%s) is older than the head commit." % record["analysisDate"])

    status, body = _get(cfg, "api/qualitygates/project_status", {"projectKey": cfg["project_key"], "pullRequest": pr})
    if status == 404:
        return finish("missing", "SonarQube has no quality gate result for merge request !%s." % pr)
    if status != 200:
        return finish("error", "quality gate request returned %s: %s" % (status or "no answer", body))
    project_status = body.get("projectStatus") or {}
    gate_status = project_status.get("status")
    evidence["gate_status"] = gate_status
    evidence["conditions"] = [
        {k: c.get(k) for k in ("metricKey", "status", "comparator", "errorThreshold", "actualValue")}
        for c in project_status.get("conditions", [])
    ]
    if gate_status in (None, "NONE"):
        return finish("missing", "The analysis has no quality gate attached.")

    issues, why = _issues(cfg, pr)
    if issues is None:
        return finish("error", why)
    floor = policy["floor"]
    with run.lock():
        findings = run.findings()
        seen = set()
        for issue in issues:
            fid = "sonar-" + str(issue["key"])
            seen.add(fid)
            component = issue.get("component", "")
            prefix = cfg["project_key"] + ":"
            path = component[len(prefix):] if component.startswith(prefix) else component
            F.upsert_system(
                findings, fid, "sonar", "external", head,
                {
                    "severity": _severity(issue, floor),
                    "category": _category(issue),
                    "claim": "%s (%s)" % (issue.get("message", "SonarQube issue"), issue.get("rule", "unknown rule")),
                    "scenario": "SonarQube rule %s" % issue.get("rule", "unknown"),
                    "proposed_fix": None,
                    "location": {"path": path, "start_line": issue.get("line"), "end_line": None} if path else None,
                    "local_id": str(issue["key"]),
                },
                "unverified",
                reopen=True,
            )
        for fid, rec in findings.items():
            if fid.startswith("sonar-") and fid not in seen:
                F.close_system(rec, "sonar", head, "SonarQube no longer reports this issue")
        run.save_findings(findings)
    evidence["open_issues"] = len(issues)

    failing = ["%s %s (limit %s)" % (c["metricKey"], c["actualValue"], c["errorThreshold"]) for c in evidence["conditions"] if c["status"] == "ERROR"]
    freshness = "matched by commit" if evidence["freshness"] == "commit" else "matched by analysis time"
    if gate_status == "OK":
        return finish("passed", "Quality gate OK, %s, %d open issue(s)." % (freshness, len(issues)))
    return finish("failed", "Quality gate %s: %s" % (gate_status, "; ".join(failing) or "see SonarQube"))
