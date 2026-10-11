---
name: cq-aggregate
description: Verify, deduplicate and sort the findings of a cq quality run, give each one an outcome the tier allows, and write one ordered fix plan. It never issues a verdict. Use after cq-panel, after Duo or SonarQube findings arrive, or whenever a run has open findings.
argument-hint: "<run-id>"
---

# cq-aggregate: from findings to a fix plan

Run id: $ARGUMENTS

You recommend. You do not decide the verdict, and cq refuses the moves that
would let you: you cannot lower a severity, you cannot reject or file away a
BLOCKER, a MAJOR at tier 2 or 3, or an always-blocking finding, and once a
finding is contested only the human can move it.

1. **List.** `cq findings list <run> --open`

2. **Triage external comments.** A Duo thread arrives as `UNTRIAGED`. Read it
   and the code, then give it a severity, a category and a failure scenario:

       cq findings triage <run> <id> --severity <S> --category <C> --scenario "<what fails, for which input>" [--proposed-fix "<fix>"]

3. **Verify first.** Collect every open finding that is BLOCKER, MAJOR or
   always blocking and still `unverified`. Start one `cq-verifier` agent with
   the run id and all of those ids. Use one agent, not several: verifiers write
   scratch tests into the worktree and would disturb each other. Reproduced
   becomes `verified`. Not reproduced becomes `contested` and goes to the
   human. Nothing is dropped.

4. **Deduplicate by root cause.** Several reviewers on one defect are one finding:

       cq findings merge <run> <keep-id> <duplicate-id>...

   cq keeps the highest severity of the group. Agreement between independent
   reviewers raises priority. A finding from one reviewer alone is never
   discounted for being alone.

5. **Give every open finding an outcome.** cq enforces this table.

   | Finding | Tier 0 and 1 | Tier 2 and 3 |
   | --- | --- | --- |
   | BLOCKER, or always blocking | fix | fix |
   | MAJOR | fix, or file a ticket | fix |
   | MINOR | fix if cheap, else leave open | fix if cheap, else leave open |
   | NIT | leave | leave |

   - *File a ticket*: create a Jira issue (Atlassian tools) with labels
     `agent-found`, `tier-<N>`, the category, and `cq-<finding-id>`. Search for
     that last label first; if an issue has it, comment there and do not create
     a second one. Then:
     `cq findings status <run> <id> filed --ticket <KEY> --reason "<why later is acceptable>"`
   - *You think a MINOR or NIT is wrong*:
     `cq findings status <run> <id> rejected --reason "<the specific reason>"`
   - *You think a gating finding is wrong, reviewers disagree, or the fix is a
     trade-off* (scope, data model, performance against clarity):
     `cq findings status <run> <id> contested --reason "<each side, in one sentence each>"`

6. **Resolve conflicts by tier.** When a simplicity finding argues against
   defensive code, simplicity wins at tiers 0 and 1 and robustness wins at
   tiers 2 and 3. Anything else that conflicts is contested.

7. **Write one fix plan**: the ids to fix, in this order: always-blocking and
   BLOCKER, then MAJOR, then cheap MINOR, then deletions from simplicity
   findings last. Hand that list to `cq-run` step 6.

Do not fix code here. Do not state a verdict.
