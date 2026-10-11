# Agentic code quality system

`cq engine run SHOP-123` takes a Jira ticket to a decided draft merge request
with no session open. Every agent is a separate headless Claude Code session;
Gemini is the cross-vendor adversary. Agents answer in JSON. `cq` applies each
answer through its rules, and only its policy engine issues a verdict.

## What is in the box

```
install.sh               installs the control repo and the cq command
control/                 becomes your control repo; keep it outside every project
  policy/floor.yml       the global floor: tiers, required evidence, budgets
  policy/models.yml      role to model routing; Gemini 3.1 Pro and 3.8 Flash adversaries
  policy/gemini-*        the Gemini CLI's read-only policy and settings
  registry.yml           your repositories: paths, check commands, risk paths, Jira projects
  engine.yml             how the engine reaches Jira, Claude Code, and how long it waits
  src/cq/                policy engine and run store
  src/cq/engine/         the run engine (about 6,000 lines of Python in all)
  hermes/cq-operator/    a Hermes skill that runs the engine on a schedule
  templates/             what `cq init-repo` puts into a project
    cq/                  criticality.yml, coding standards, business-critical map
    claude/skills/       cq-screen, cq-run, cq-panel, cq-aggregate, cq-mr-loop
    claude/agents/       cq-reviewer, cq-verifier, cq-fixer, cq-plan-reviewer
  tests/                 225 tests: policy contract, units, templates, end to end, engine
```

## Install

```
./install.sh                      # control repo in ~/cq-control, cq in ~/.local/bin
$EDITOR ~/cq-control/registry.yml # register one repository
cq init-repo ~/code/your-repo     # never overwrites an existing file
cq doctor
```

Needs Python 3.9+, git, `glab` (authenticated), Claude Code (`claude`) and the
Gemini CLI (`gemini`). Environment: `JIRA_EMAIL` and `JIRA_API_TOKEN`,
`SONAR_TOKEN`, `GEMINI_API_KEY` (or Vertex AI variables). The third-party review
skills are installed separately: ponytail, pstack (thermo-nuclear review), and
Matt Pocock's skills. Without them the reviewers use the lens in their brief.

## The engine

```
cq engine run SHOP-123            screen, plan, implement, review, fix, open the MR, decide
cq engine resume <run>            continue after your decision, a wait, or a crash
cq engine resume --waiting        every run waiting on Duo or SonarQube
cq engine pull --limit 1          start runs for agent-ready tickets, within the WIP limit
cq engine status | stats          where each run stands; the numbers to measure by
cq engine cancel <run>            stop; branch and merge request are kept
```

A run ends in one of four states. Exit codes: 0 COMPLETED, 2 NEEDS_HUMAN, 3 WAITING, 1 FAILED.

| State | Meaning | Next |
| --- | --- | --- |
| COMPLETED | The verdict is PASS. The draft MR carries the report | Your part for the tier, named in the Jira comment |
| NEEDS_HUMAN | A decision only you can make. A Jira issue labelled `agent-escalation` says which, with the commands | Run the command it names, then `cq engine resume <run>` |
| WAITING | Duo, GitLab or a SonarQube analysis has not answered yet | Hermes runs `resume --waiting`. After 4 hours on the same commit (`waits.escalate_after_seconds`) the run turns NEEDS_HUMAN |
| FAILED | Something broke: an agent that kept failing, a worktree left dirty, history rewritten, an internal error | Fix the cause, then `cq engine resume <run>` |

A run whose record says RUNNING while no engine process holds its lock was
killed or crashed. `cq engine status` shows it as FAILED, and `cancel` and
`resume` work on it.

What it does, in order:

1. **Screen.** A screener session checks the ticket six ways. An unclear ticket
   gets one question in Jira and no run. Tier 2+ work built on assumptions waits
   until you add the label `assumptions-confirmed`; the next `pull` takes it up.
   A ticket that names no paths gets the floor's `default_tier`. The queue never
   starts tier 3: start that yourself with `cq engine run <ticket>`.
2. **Plan.** A planner writes the plan and rollback. At tier 2+ a plan reviewer
   reads it; one revision, then you. Tier 3 stops for `cq approve <run> plan_approval`.
   If a change only becomes tier 3 once the code exists, the run stops for the
   same approval and says so.
3. **Implement.** An implementer commits in a worktree under `~/cq-worktrees`
   (`worktrees_dir`), then a separate test author adds the tests. What each says it
   did, and where it left the plan, is kept as a note.
4. **Evidence loop.** It asks the policy what is missing and does that: runs the
   checks, starts the reviewers in parallel, sorts the findings, verifies them in
   a scratch copy, fixes in bounded rounds, and re-reviews narrowly. MINOR and NIT
   findings each get an outcome too: filed in Jira, left, or fixed.
5. **Merge request.** Push and open a draft MR. If the local loop could not pass
   (fix budget spent, a contested finding), the draft goes to you now, without
   Duo or SonarQube. Otherwise: Duo request (asked again once if Duo reports an
   error), SonarQube wait, the same loop for their findings. Every Duo thread gets
   a reply when its finding has an outcome, and is resolved: fixed, filed,
   rejected, or left as is with the reason. A contested thread gets one reply
   and stays open.
6. **Decide.** The final check at tier 3, then the verdict and the report on the MR.

Where a run stands is worked out from its evidence, so `resume` repeats nothing
that is already on file. Every agent session is logged with its cost under
`~/.cq/runs/<run>/agents/`.

Each session runs with only the tools its role needs: reviewers read and run
read-only git; writers may also edit, commit, and run the registered checks and
the test runners in `extra_allowed_tools`. Every session loads only your user
settings plus `policy/claude-engine.json` (the guard hook), and no MCP servers:
the `.claude/` and `.mcp.json` of the repository under review are part of the
change, not configuration. Amend, reset, rebase, push and `git ... --output`
are denied, by permission rule and again by the guard hook. After each writing
session the engine checks that the commits it had are still there. A session
that times out is killed with everything it started.
Your user settings still apply, so an allow rule there (for example
`Bash(*)`) widens what every session may do. Keep those narrow.

## Driving a run by hand

In Claude Code, on a feature branch with the work committed: `/cq-run SHOP-123`.
The skill drives these commands, which you can also run yourself:

```
cq run start --ticket SHOP-123        tier, and everything this tier requires
cq check run <run> --required         cq runs the commands; no self-reporting
cq brief <run> --reviewer simplicity  a reviewer's whole assignment
cq findings add|list|merge|status     the findings contract
cq adversary run <run>                the cross-vendor reviewer
cq fix round <run> --phase local      opens a fix round against the budget
cq mr ensure <run>                    find or create the draft merge request
cq duo request|wait|ingest|reply      GitLab Duo loop
cq sonar gate <run>                   SonarQube result for the merge request
cq verdict <run> [--stage local]      exit 0 PASS, 1 FAIL, 2 BLOCKED, 3 INCONCLUSIVE
cq mr describe <run>                  evidence report into the description
```

Those four codes belong to `cq verdict`. Exit code 4 means cq refused a
command or the input was bad, and 5 is a bug in cq; neither is ever a verdict.

These commands are yours alone. They need a real terminal and refuse to run
inside Claude Code:

```
cq approve <run> plan_approval|final_signoff
cq decide  <run> <finding> reject|waive|reopen --reason "..."
cq tier lower <run> <n> --reason "..."
cq fix extend <run> --phase local|mr      one more fix round after the budget is spent
cq run close <run>        and  cq run start --new
```

One run belongs to one branch, one ticket and one worktree. When you reuse a
worktree or a ticket for new work, cq closes the old run by itself if its head
is merged into the default branch. After a squash merge it cannot tell, and
asks you to run `cq run close <run>`.

## The four verdicts

| Verdict | Meaning | Who acts |
| --- | --- | --- |
| FAIL | The evidence shows the change does not meet the bar | Agents, within the fix budget |
| INCONCLUSIVE | Required evidence is missing, stale or unverified | Agents, or waiting on Duo or CI |
| BLOCKED | The evidence is complete and a human decision is needed | You |
| PASS | Every requirement for the tier is met | You, for the step the tier names |

When several apply, the first in that order is shown. Nothing merges by itself.

## Rules the engine enforces

- Evidence is tied to a commit. A new commit, an amend or a rebase makes older
  checks, reviews, Duo and SonarQube results stale.
- A tier rises with the diff and never falls by itself. A project's
  `.cq/criticality.yml` can only raise it, and is read from the base commit.
- A reviewer's submission must list every changed file. A finding without a
  file and line, a scenario and a proposed fix is stored as NIT.
- A severity never goes down. A merge of duplicates keeps the highest.
- An agent cannot reject a BLOCKER, a MAJOR or an always-blocking finding, and
  cannot file a BLOCKER, or a MAJOR at tier 2 or 3, for later.
- "Fixed" is a claim. It needs a commit made inside a fix round, after the
  report. The reviewer that raised the finding is then shown the claim, must
  review the fix, and keeps reviewing every later commit so a revert cannot
  slip through. A fixed Duo finding is confirmed by a local reviewer.
- Once a finding is contested, only you can move it. Any contested finding
  makes the run BLOCKED.
- After a fix, earlier reviews still count only if fix-diff reviews cover every
  commit since and the change since is small (`review_carry` in `floor.yml`).
  `security` and `final_check` always review each change themselves.
- A deleted or skipped test, and a change to what a check runs (`package.json`
  scripts, `Makefile`, test and lint configuration), go to you.
- SonarQube and gate-guard findings open and close with their source. An agent
  cannot mark one fixed. A human decision on a gate-guard finding covers that
  exact content and not a later edit of the same file.
- A fix round opens before the fix and after the report. A finding reported in
  the re-review needs a new round, so the budget counts real iterations.

## Rolling it out in slices

`policy/floor.yml` asks for the full evidence table from the proposal. You will
not have all of it on day one. For anything you have not built yet, add a line
under `not_applicable` in `registry.yml` with the reason:

```yaml
defaults:
  not_applicable:
    mutation: "Mutation testing is not set up yet"
    adversary: "No cross-vendor reviewer configured yet"
```

The run then skips that requirement, and every report lists it as not checked.
A human approval can not be marked not applicable.

## What changed from the earlier scaffold

| Earlier scaffold | Now |
| --- | --- |
| The aggregator skill wrote the verdict | `cq verdict` computes it from recorded evidence. No agent output is a verdict |
| Tier rules, gates and model routing sat in the repo being changed | The floor is in the control repo. A project's `.cq/criticality.yml` can only raise a tier, and is read from the base commit |
| Findings, verdict and ledger were written into `.cq/` in the worktree | A run store outside the worktree (`~/.cq/runs/`) |
| A finding was severity, file, line, scenario, proposal | A validated submission with source, commit, coverage of every changed file, status and history |
| T0 was critical; the lowest tier auto-merged | 0 Low to 3 Critical. Nothing auto-merges |
| The whole panel reran after each fix, up to three times | A fix-diff review by the reviewers whose findings were fixed. Two rounds, then a human |
| Local open-weights adversary by default | A cross-vendor CLI you configure. Its model and family are stamped from config |
| `duo-ingest` treated "no unresolved comments" as done, and read 100 discussions | Five Duo states. Done means Duo posted after the request and went quiet. All pages are read |
| `sonar-gate` used a wrong endpoint, queried by branch, and had one failure exit | `api/qualitygates/project_status` by pull request. Passed, failed, missing, stale and error are separate |
| Fallback to a local scanner | None. The analysis comes from CI only |
| `main` hard-coded | Base branch and remote from the registry |

Taken from the `code-quality-system` zip: the reviewer and model recorded with
every review, validation before a submission counts, the rule that a merge
keeps the highest severity, the "contested" route to a human, waivers that
expire, a Jira search before filing, and CI configuration as a critical path.

## What this does not protect against

`cq` stops shortcuts and honest mistakes: a stale review, a skipped scan, a
finding waved away, a test deleted to go green. It is not a security boundary.
An agent with a shell on your machine could still submit a review in another
reviewer's name, or put a fake `glab` on its PATH. Separate agents, the guard
hook and the terminal check for human commands make that hard to do by
accident. Running agents in a container with scoped credentials is what makes
it hard to do on purpose, and is a later slice in the proposal.

Known limits inside that scope:

- The guard hook and the terminal check are speed bumps. The hook matches
  command text, and a determined agent can phrase around it; the terminal check
  in `cq` itself is the part that holds.
- "Uncommitted changes" does not see files hidden through `.git/info/exclude`.
- The gate guard knows the common ways to switch a test off (pytest, unittest,
  Jest, Vitest, Mocha, JUnit, Go, Rust) and the common check configuration
  files. Add your own under `gate_files` and `test_globs` in `registry.yml`.
  `pyproject.toml` is not in the default list because dependency edits would
  trip it every time.
- An agent picks the severity when it triages a Duo comment. A comment triaged
  as NIT and rejected with a reason is closed. The report lists it.
- A run does not follow a worktree that you move with `git worktree move`.

## Gemini as the adversary

`policy/models.yml` runs Gemini 3.1 Pro (preview) as the adversary and Gemini 3.8
Flash as the second adversary at tier 3, through the Gemini CLI with a policy that
allows only file reads and searches. Three things about the Gemini CLI that cq guards against:

- **It can run another model than the one you asked for.** With a preview model
  and no preview access it quietly uses an older one. cq reads the model that
  actually ran from the CLI's JSON output and refuses the review if it differs.
- **It reads configuration from the folder it runs in.** A change under review
  could add a `.gemini/settings.json` (tools, MCP servers) or a `GEMINI.md` with
  instructions. So Gemini never runs in the worktree: cq exports the head commit
  to a temporary folder straight from git's object store (so `.gitattributes`
  cannot hide files), drops `.gemini/`, `GEMINI.md` and `.geminiignore` in any
  letter case, every symlink and every submodule, and puts the diff itself in the brief,
  because the read-only policy gives Gemini no shell to run git. The copy is
  deleted afterwards. cq's own settings file turns off MCP servers, tool
  discovery, hooks and skills.
- **It refuses to run in an untrusted folder.** cq sets
  `GEMINI_CLI_TRUST_WORKSPACE=true` for the temporary copy.

If you installed the earlier scaffold, your `policy/models.yml` was kept: copy
the two `adversary` sections and the `test_author` and `plan_reviewer` roles
from `control/policy/models.yml` into it.

## Not yet checked against your live services

These follow the vendors' documentation and are tested here against fakes.
Try each once on a noncritical merge request.

- **Duo completion.** GitLab documents no "review finished" signal. `cq` infers
  it: Duo posted after the request and has been quiet for 60 seconds, or the
  reviewer state reset and then left `unreviewed`.
- **Requesting Duo through the API.** `cq` posts the quick action
  `/assign_reviewer @GitLabDuo` as a note. GitLab implies, and does not state,
  that quick actions run in notes created through the API. Whether Duo reviews
  a draft merge request on a manual request is also not documented.
- **SonarQube freshness.** SonarQube Cloud reports the analysed commit. On
  Server, `cq` looks for it on the analysis record, and compares times only
  when no commit is reported. If your merge request pipelines run on merged
  results, SonarQube records the merge commit and every result will read
  `stale`: set `freshness: time` for that repository in `registry.yml`.
- **The guard hook payload.** The hook reads `tool_input.file_path`, which the
  Claude Code docs show only by example.
- **Claude Code flags.** The engine uses `--json-schema`, `--permission-mode
  dontAsk`, `--tools`, `--allowedTools`, `--disallowedTools`, `--effort`,
  `--no-session-persistence`, `--setting-sources user`, `--settings`,
  `--strict-mcp-config` and `--mcp-config`, as documented now. Check one session
  log under `agents/` after the first run, and that the guard hook fired.
- **Gemini CLI flags.** `--policy` and the read-only rules follow the CLI's
  source; check `stats.tools` in the first adversary log for which tools it used.
- **Jira search.** The engine uses `/rest/api/3/search/jql`, the endpoint that
  replaced `/search` on Jira Cloud. Jira Data Center keeps the old one and needs
  a small change in `engine/tracker.py`.
- **GitLab 19.4 added a REST trigger for the agentic Code Review Flow**
  (`POST /api/v4/ai/duo_workflows/workflows`, experimental). It is not used
  here; the quick action works for both Duo variants.

## Tests

```
cd ~/cq-control && .venv/bin/python -m unittest discover -s tests
```

About three minutes. The tests use their own copy of the policy, so your edits to
`policy/floor.yml` do not break them. `tests/test_policy.py` is the contract:
same input, same verdict; missing or stale evidence is never a pass.
