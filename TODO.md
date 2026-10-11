# TODO

Open work for the `cq` agentic code quality system. Sources: the "Not yet
checked against your live services", "Rolling it out in slices" and "What this
does not protect against" sections of [`docs/system-guide.md`](docs/system-guide.md).

Status at import: all 225 tests pass against fakes
(`cd control && PYTHONPATH=src python3 -m unittest discover -s tests`, Python 3.13).

## 1. First install and setup

- [ ] Run `./install.sh` and confirm the `cq` link on `PATH` (`~/.local/bin`).
- [ ] Commit the installed control repo (`~/cq-control`) so every policy change has history.
- [ ] Register the first (noncritical) repository in `registry.yml`: path, remote,
      default branch, GitLab project, Jira projects, check commands, risk `paths`.
- [ ] Fill in `engine.yml` (Jira URL, Claude Code, timeouts, `worktrees_dir`).
- [ ] Set `JIRA_EMAIL`, `JIRA_API_TOKEN`, `SONAR_TOKEN`, `GEMINI_API_KEY` (or Vertex AI vars).
- [ ] Authenticate `glab`; install `claude` and `gemini` CLIs.
- [ ] Install the third-party review skills (ponytail, pstack thermo-nuclear review,
      Matt Pocock's skills), or accept that reviewers fall back to their brief's lens.
- [ ] `cq init-repo <repo>` then `cq doctor` until clean.
- [ ] Copy `control/hermes/cq-operator` into the Hermes skills folder and schedule it.

## 2. Verify against live services (one noncritical MR each)

- [ ] **Duo completion signal** — confirm the inferred "posted after request, quiet
      for 60s" / reviewer-state-reset heuristic matches real Duo behaviour.
- [ ] **Requesting Duo via API** — confirm `/assign_reviewer @GitLabDuo` posted as an
      API note actually triggers a review, including on a *draft* MR.
- [ ] **SonarQube freshness** — confirm analysed-commit matching (Cloud vs Server).
      If MR pipelines analyse merged-results commits, set `freshness: time`.
- [ ] **Guard hook payload** — confirm the hook receives `tool_input.file_path`
      and that it fired (check a session log under `~/.cq/runs/<run>/agents/`).
- [ ] **Claude Code flags** — confirm `--json-schema`, `--permission-mode dontAsk`,
      `--tools`, `--allowedTools`, `--disallowedTools`, `--effort`,
      `--no-session-persistence`, `--setting-sources user`, `--settings`,
      `--strict-mcp-config`, `--mcp-config` all behave as documented.
- [ ] **Gemini CLI flags** — confirm `--policy` and the read-only rules; inspect
      `stats.tools` in the first adversary log.
- [ ] **Gemini model pinning** — confirm preview access to Gemini 3.1 Pro so the
      run isn't refused for a silent model downgrade.
- [ ] **Jira search** — confirm `/rest/api/3/search/jql` on Jira Cloud. On Jira
      Data Center, set `search_api: legacy` under `tracker` in `engine.yml`
      (support added; confirm against a live instance).
- [ ] Evaluate GitLab 19.4's experimental REST trigger for the agentic Code Review
      Flow (`POST /api/v4/ai/duo_workflows/workflows`) as a replacement for the quick action.

## 3. Rollout slices (evidence not built yet)

The floor (`control/policy/floor.yml`) requires the full evidence table. Mark
anything missing under `not_applicable` in `registry.yml` with a reason, then
remove each entry as the capability lands.

- [ ] Tier 0–1 checks per repo: `lint`, `types`, `unit`, `build`.
- [ ] Tier 2: `integration` checks.
- [ ] Tier 2: `mutation` testing.
- [ ] Tier 2: cross-vendor `adversary` (Gemini) working end to end.
- [ ] Tier 3: `e2e` checks.
- [ ] Tier 3: automated `rollback_test`.
- [ ] Tier 3: second adversary (`adversary2`, Gemini Flash) and `final_check`.
- [ ] SonarQube project per repository, analysed from CI on MR pipelines.
- [ ] Add project-specific `gate_files` and `test_globs` to `registry.yml`
      (custom check configs, unusual test frameworks).

## 4. Hardening (known limits)

- [ ] Run agents in a container with scoped credentials — the planned slice that
      makes `cq` hold against deliberate misuse, not just accidents
      (reviewer impersonation, a fake `glab` on `PATH`).
- [ ] Guard hook matches command text only; consider structural checks or
      container-level enforcement.
- [ ] Detect uncommitted files hidden through `.git/info/exclude`.
- [ ] Handle worktrees moved with `git worktree move`.
- [ ] Review the policy for Duo comments an agent triages as NIT and rejects
      (currently closed with a reason and listed in the report).
- [ ] Decide whether `pyproject.toml` belongs in default `gate_files`
      (excluded today because dependency edits would trip it every time).
- [ ] Keep user-level Claude Code allow rules narrow — e.g. no `Bash(*)` — since
      they widen every engine session.

## 5. This repository

- [x] Add CI (`.github/workflows/tests.yml`): fast suite on Python 3.9 and 3.13
      for every push and PR; full suite on PRs and nightly.
- [ ] Add a LICENSE.
- [x] Add a CHANGELOG.
- [ ] Tag `v0.1.0` once the import PR is merged into `main`.
- [ ] Make `main` the default branch in GitHub settings.
- [x] Add a `CLAUDE.md` describing repo conventions for agent contributors.

## 6. Repo split

Risk policy, escalation and the repo map move to their own repos; this repo
keeps the `cq` engine. Seed contents are prepared (README, TODO, copied files).

- [ ] Create private repos `cq-risk`, `cq-escalation`, `cq-repo-map` on GitHub
      and give the Claude GitHub App access, then push the seeds.
  - `cq-risk`: `policy/floor.yml`, `templates/cq/criticality.yml`,
    `templates/cq/standards/business-critical-map.md`
  - `cq-escalation`: `hermes/cq-operator/`, escalation runbook
  - `cq-repo-map`: `registry.yml`
- [ ] Teach `install.sh` to take those files from the new repos (or from paths
      given as arguments), then remove the copies here so each has one source of truth.
- [ ] Keep tests self-contained: they use `tests/fixtures/`, not the shipped files.
