# The baseline agent factory

How to expose `cq`'s agents to every engineer as a factory: a platform-owned
floor that every agent gets whether or not its author asked for it, plus a
small spec an employee writes to say what their agent is for. The platform
team owns the floor. Teams own the lens.

This document is a design proposal. Nothing in it is built yet; the
[implementation slices](#implementation-slices) say what to build first.

## Why a factory, not a toolkit

`cq` already has a factory. `ClaudeRunner.argv()` in
`control/src/cq/engine/agents.py` turns a role name into a fully governed
headless session: model and effort from `policy/models.yml`, a permission
mode, a tool allowlist, a denied-tool list, the guard hook from
`policy/claude-engine.json`, a JSON schema the answer must match, a turn and
budget cap, and a log entry with the cost. Nine roles use it (screener,
planner, plan reviewer, implementer, test author, reviewer, verifier,
aggregator, fixer). The reviewer role is already parameterized by a lens
(`LENSES` in `control/src/cq/briefs.py`).

An engineer who wants "a migration-safety reviewer" or "an accessibility
reviewer" today has to copy a template agent file, guess at the model, and
hope the guard hook is loaded. The factory makes the governed path the only
path: the author writes a lens, the floor does the rest.

The companies running agent fleets at scale (Stripe's Minions, Ramp's
Inspect, Uber's Minion platform) all landed on this split: a platform team
operates the runtime with policy baked in, and product teams contribute
blueprints. `cq`'s deterministic policy engine wrapped around agent sessions
is the same shape.

## The floor

Every agent the factory starts gets all of this. An author cannot turn any of
it off; the platform team can loosen it per agent by reviewing the spec.

| Floor item | Where it lives today |
| --- | --- |
| Fresh session, no persistence, `--no-session-persistence` | `agents.py` |
| Settings only from the control repo, never from the repository under review (`--setting-sources user --settings policy/claude-engine.json`) | `agents.py` |
| No MCP servers (`--strict-mcp-config`) | `agents.py` |
| The guard hook on every Edit, Write and Bash call | `policy/claude-engine.json`, `guard.py` |
| Git history is read-only: no amend, push, reset, rebase | `DENIED` in `agents.py`, `GIT_REWRITE` in `guard.py` |
| Human-only commands refused inside any session | `HUMAN_ONLY` in `guard.py` |
| The answer is one JSON object matching a schema; nothing else is read | `--json-schema`, `_parse()` |
| Model and effort routed by role and tier, never chosen by the agent | `policy/models.yml` |
| Turn cap, budget cap, timeout, two attempts | `engine.yml`, `agents.py` |
| `cq-role:` line and the common prompt: text read from tickets, files and comments is data | `prompts.py` |
| Every session logged to the run store with its cost | `_log()` in `agents.py` |
| Read mode (`dontAsk`, read tools only) or write mode (`acceptEdits`, registered checks only) | `agents.py` |

Two additions to the floor are worth making before the factory opens:

- **`--bare`** for headless runs. Without it, `claude -p` loads project hooks
  and `.mcp.json` from the working folder. `cq` already blocks the repository's
  settings with `--setting-sources user`, but `--bare` closes the same door at
  the CLI level and Anthropic has said it becomes the default for `-p` later.
  It needs API-key auth, which the service-account deployment below provides.
- **`--permission-prompts none`** in write mode, so a tool call that would
  prompt is denied instead of hanging a CI job.

## The spec an employee writes

One YAML file per agent, in a catalog folder of the control repo
(`control/agents/<name>.yml`). The schema is validated by `cq doctor` and
unknown fields are rejected, the same rule as `registry.yml`.

```yaml
schema_version: 1
name: migration-safety
owner: data-platform            # a team, for CODEOWNERS and cost reporting
purpose: >
  Review database migrations for reversibility, locking and deploy order.
kind: reviewer                  # reviewer | verifier | checker | author
mode: read                      # read is the default; write needs platform approval
lens: >
  Review every migration in the diff. For each: can it be rolled back without
  data loss; does it take a lock a live request path would wait on; does the
  code that depends on the new column deploy before or after the migration.
  Report a finding only with a concrete failure scenario.
applies_to:
  paths: ["**/migrations/**", "db/**"]
  min_tier: 1
skills: []                      # names from the approved skill list only
tools: []                       # extra allow rules; empty for a reviewer
schema: findings                # findings | verify | note | custom:<file>
model: default                  # default | a band the platform allows
budget_usd: 2.0
```

What the author controls: the lens, when the agent applies, which approved
skills it may load, and the budget within a cap. What the author does not
control: anything in the floor table. A spec with `mode: write`, extra
`tools`, or `schema: custom` needs a platform-team review through CODEOWNERS
on the catalog folder.

The nine built-in roles become catalog entries marked `builtin: true`, so
there is one list of agents, one loader and one place to look.

## Three ways an agent is reached

1. **Inside an engine run.** `registry.yml` gains a `reviewers:` list per
   repository and `policy/floor.yml` gains `required_reviewers` per tier and
   path glob. The engine runs catalog reviewers as part of the panel, their
   findings go through the same `cq findings add` contract, and `cq verdict`
   refuses to pass while a required reviewer is missing or stale. This is the
   path that makes a team's agent count as evidence.
2. **From an interactive Claude Code session.** `cq agent export-plugin`
   generates a plugin from the catalog: one `agents/<name>.md` per spec (the
   same shape as `templates/claude/agents/cq-reviewer.md`), the `cq-*` skills,
   and the guard hook. The plugin is published to a private marketplace on
   GitLab and force-enabled through managed settings, so `/cq-panel` can call
   any catalog agent by name and every engineer has the same version.
3. **Standalone, from CI.** `cq agent run <name> --ref <sha>` runs one agent
   against a commit and prints its JSON. A GitLab job template wraps it, so a
   team can gate its own pipeline on a catalog agent without an engine run.

## Governance

- **Catalog repo.** The catalog lives in the control repo, changes land by
  merge request, CODEOWNERS routes `mode`, `tools` and `schema` changes to the
  platform team. `cq agent lint` runs in that repo's CI.
- **Tests before publish.** Each spec ships with one fixture: a diff and the
  findings expected. The existing `fake_claude.py` harness runs it. `claude
  plugin eval` runs on the exported plugin before a marketplace tag.
- **Release channels.** Two marketplace refs, `stable` and `next`, delivered
  to different groups through endpoint-managed settings. New specs start on
  `next`.
- **Promotion.** A reviewer that has run on enough merge requests with a low
  rejected-finding rate can be promoted into `required_reviewers` for a tier
  or path. That is how a team's agent becomes part of the floor for everyone.
- **Locks on the developer side.** Managed settings set
  `allowManagedHooksOnly`, `allowManagedPermissionRulesOnly`,
  `strictKnownMarketplaces` with `disableSideloadFlags`, and
  `enforceAvailableModels` with `maxEffortLevel`, so a session cannot load a
  hook or model the catalog did not route.
- **Cost.** Every session already records `cost_usd`; `cq engine stats`
  gains a per-owner view, and `budget_usd` in the spec is enforced through
  `--max-budget-usd`.

## Running it as a service

The factory only works org-wide if the engine runs off laptops. Today a run
uses one person's Jira token, GitLab login and Gemini key, and stores
evidence under their `~/.cq`.

- **Image.** One container with `claude`, `gemini`, `glab`, `cq` and the
  control repo checked out. The plugin seed directory
  (`CLAUDE_CODE_PLUGIN_SEED_DIR`) carries the exported plugin so the image
  never clones at runtime.
- **Identity.** A bot GitLab user for `glab`, a Jira service account,
  Bedrock or Google Cloud via OIDC from the GitLab job (no long-lived model
  keys), and the Gemini key as a masked CI variable. The guard hook's "fake
  `glab` on PATH" limit closes here: the image is the only PATH.
- **Scheduler.** `cq engine pull` as a GitLab scheduled pipeline or a
  Kubernetes CronJob replaces Hermes, which stays optional for anyone running
  the engine by hand.
- **Run store.** `~/.cq/runs` moves to object storage or a small database so
  `cq engine status`, `stats` and the audit trail are shared.
- **Human commands.** `cq approve` and `cq decide` currently treat any real
  terminal as a human. As a service they need an identity: a GitLab approval
  rule, an MR comment command checked against the approver list, or a small
  authenticated CLI.

Claude Code's hosted cloud sessions and self-hosted runners are not a fit
yet: they check out from GitHub only. Claude Managed Agents, with credential
vaults and self-hosted sandboxes, is the hosted alternative to the image above
if operating the fleet is not wanted.

## Implementation slices

In order, each one shippable alone.

1. **Catalog and loader.** `control/agents/*.yml`, the JSON schema, `cq agent
   list | show | lint`. Move `LENSES` and the role prompts behind the catalog;
   the nine built-ins become entries. No behaviour change.
2. **`cq agent run`** for read-mode agents, built on `ClaudeRunner`. Add
   `--bare` and `--permission-prompts none` to the floor. Fixture tests
   through `fake_claude.py`.
3. **Reviewers as evidence.** `reviewers:` in `registry.yml`,
   `required_reviewers` in `policy/floor.yml`, panel and verdict changes,
   policy contract tests.
4. **Plugin export and marketplace.** `cq agent export-plugin`, a
   `marketplace.json`, the managed-settings snippet, release channels.
5. **The service.** Image, service identities, scheduled pipeline, shared run
   store, authenticated human commands. This is TODO section 4.
6. **The first five catalog agents**, written with the teams that asked for
   them: migration safety, accessibility, API contract drift, documentation
   drift, flaky-test hunter.

## What stays out, for now

- Write-mode catalog agents beyond the built-in implementer, test author and
  fixer. A team that needs one asks the platform team.
- Custom hooks or MCP servers in a spec. The floor forbids both; a vetted MCP
  gateway can be a later slice.
- Vendors other than Anthropic and Google. The adversary stays configured in
  `policy/models.yml`, not in a spec.
