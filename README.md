# code-quality-agentic-system (`cq`)

An agentic code quality system that takes a Jira ticket to a decided **draft
merge request** with no session open:

```
cq engine run SHOP-123
```

Every agent is a separate headless Claude Code session; Gemini acts as the
cross-vendor adversary. Agents answer only in JSON. `cq` applies each answer
through its rules, and only its deterministic policy engine issues a verdict.
Nothing merges by itself.

> The full operating manual (engine states, verdicts, enforced rules, Gemini
> hardening, known limits) is in [`docs/system-guide.md`](docs/system-guide.md).
> Open work is tracked in [`TODO.md`](TODO.md).

## How it works

```
Jira ticket ──► Screen ──► Plan ──► Implement ──► Evidence loop ──► Draft MR ──► Decide
                 │          │          │              │                 │           │
            6-way check  plan +    worktree +    checks, parallel   GitLab Duo   policy
            tier pick    rollback  test author   reviewers, verify, + SonarQube  verdict +
                         review               bounded fix rounds    loop        report
```

- **Tiers** 0 (Low) to 3 (Critical) decide how much evidence a change needs.
  A tier rises with the diff and never falls by itself.
- **Evidence is tied to a commit.** A new commit, amend or rebase makes older
  checks, reviews, Duo and SonarQube results stale.
- **Verdicts:** `FAIL`, `INCONCLUSIVE`, `BLOCKED`, `PASS` — computed by
  `cq verdict` from recorded evidence, never written by an agent.
- **Run states:** `COMPLETED` (0), `FAILED` (1), `NEEDS_HUMAN` (2), `WAITING` (3).
- **Human-only commands** (`cq approve`, `cq decide`, `cq tier lower`,
  `cq fix extend`, ...) require a real terminal and refuse to run inside Claude Code.

## Repository layout

```
install.sh                  installs the control repo and the `cq` command
docs/system-guide.md        the full operating manual
TODO.md                     open work, rollout slices, live-service checks
control/                    becomes your control repo; keep it outside every project
  pyproject.toml            package `cq-control` (Python 3.9+, PyYAML, jsonschema)
  registry.yml              your repositories: paths, check commands, risk paths, Jira projects
  engine.yml                how the engine reaches Jira and Claude Code, and timeouts
  policy/
    floor.yml               the global floor: tiers, required evidence, budgets
    models.yml              role → model routing (Gemini 3.1 Pro / 3.8 Flash adversaries)
    claude-engine.json      guard-hook settings loaded by every engine session
    gemini-readonly.toml    Gemini CLI read-only policy
    gemini-settings.json    Gemini CLI settings (no MCP, hooks, skills)
  src/cq/                   policy engine and run store
    cli.py                  `cq` entry point
    policy.py, tiers.py     verdict and tier computation
    findings.py, reviews.py the findings contract and review records
    verdicts.py, report.py  verdicts and the MR evidence report
    gitlab.py, sonar.py     GitLab Duo and SonarQube integration
    guard.py                gate guard (deleted/skipped tests, check config edits)
    store.py, runs.py       run store under ~/.cq/runs/
    engine/                 the run engine: agents, prompts, Jira tracker, core loop
    schemas/                JSON schema for reviewer submissions
  templates/                what `cq init-repo` puts into a project
    cq/                     criticality.yml, coding standards, business-critical map
    claude/skills/          cq-screen, cq-run, cq-panel, cq-aggregate, cq-mr-loop
    claude/agents/          cq-reviewer, cq-verifier, cq-fixer, cq-plan-reviewer
  hermes/cq-operator/       Hermes skill that runs the engine on a schedule
  tests/                    225 tests: policy contract, units, templates, e2e, engine (fakes for glab/claude/gemini)
```

## Quick start

Requirements: Python 3.9+, git, an authenticated `glab`, Claude Code (`claude`)
and the Gemini CLI (`gemini`).

```bash
./install.sh                        # control repo in ~/cq-control, cq in ~/.local/bin
$EDITOR ~/cq-control/registry.yml   # register one repository
$EDITOR ~/cq-control/engine.yml     # Jira connection
cq init-repo ~/code/your-repo       # never overwrites an existing file
cq doctor                           # checks everything
cq engine run SHOP-123              # one ticket, end to end
```

Environment variables: `JIRA_EMAIL`, `JIRA_API_TOKEN`, `SONAR_TOKEN`,
`GEMINI_API_KEY` (or Vertex AI variables).

To run on a schedule, copy `control/hermes/cq-operator` into your Hermes skills
folder.

### Engine commands

```
cq engine run <TICKET>         screen, plan, implement, review, fix, open the MR, decide
cq engine resume <run>         continue after a decision, a wait, or a crash
cq engine resume --waiting     every run waiting on Duo or SonarQube
cq engine pull --limit 1       start runs for agent-ready tickets, within the WIP limit
cq engine status | stats       where each run stands; the numbers to measure by
cq engine cancel <run>         stop; branch and merge request are kept
```

See [`docs/system-guide.md`](docs/system-guide.md#driving-a-run-by-hand) for
driving a run by hand from Claude Code (`/cq-run SHOP-123`).

## Rolling out in slices

`policy/floor.yml` asks for the full evidence table. For anything you have not
built yet, declare it under `not_applicable` in `registry.yml` with a reason;
every report then lists it as not checked:

```yaml
defaults:
  not_applicable:
    mutation: "Mutation testing is not set up yet"
    adversary: "No cross-vendor reviewer configured yet"
```

## Tests

From a source checkout (no install needed if PyYAML and jsonschema are available):

```bash
cd control && PYTHONPATH=src python3 -m unittest discover -s tests
```

Or from an installed control repo:

```bash
cd ~/cq-control && .venv/bin/python -m unittest discover -s tests
```

About three and a half minutes. `tests/test_policy.py` is the contract: same
input, same verdict; missing or stale evidence is never a pass. The tests use
their own copy of the policy, so edits to `policy/floor.yml` do not break them.

## Scope and limits

`cq` stops shortcuts and honest mistakes — a stale review, a skipped scan, a
finding waved away, a test deleted to go green. It is **not** a security
boundary: an agent with a shell could still impersonate a reviewer or shadow
`glab` on its PATH. Running agents in a container with scoped credentials is a
planned later slice. See [Known limits](docs/system-guide.md#what-this-does-not-protect-against).
