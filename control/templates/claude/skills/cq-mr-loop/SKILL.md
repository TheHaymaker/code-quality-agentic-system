---
name: cq-mr-loop
description: The merge request loop of a cq quality run. Requests a GitLab Duo review, waits for it, reads the SonarQube result for the merge request, routes every comment and issue through the same findings process, fixes within a budget, and replies on and resolves each Duo thread. Use after the draft merge request exists, or when the user mentions Duo comments or the SonarQube gate on a run.
argument-hint: "<run-id>"
---

# cq-mr-loop: Duo and SonarQube

Run id: $ARGUMENTS

Duo and SonarQube are two more reviewers. Their output becomes findings and
goes through `cq-aggregate` like any other. The branch must be pushed and
`cq mr ensure <run>` done.

## 1. Collect

    cq duo request <run>
    cq duo wait <run> --timeout 900
    cq sonar gate <run>

Read the state each command prints. These are different and you must not blur them:

| Printed | Meaning | What you do |
| --- | --- | --- |
| `duo: reviewed` | The review is complete. Open threads are listed | Continue |
| `duo: pending` | Duo has not finished | Go on with SonarQube. The run stays INCONCLUSIVE until Duo answers. This is waiting, not a human decision |
| `duo: request_failed`, `error`, `not_requested` | No review exists | Report the printed reason. Do not treat it as "no comments" |
| `sonar: passed` | Analysis is current and the gate is OK | Continue |
| `sonar: failed` | Analysis is current and the gate is not OK | Fix (step 3) |
| `sonar: missing`, `stale` | No analysis of this commit yet | Wait for the merge request pipeline, then run `cq sonar gate` again. Never run a scanner locally |
| `sonar: error` | The result could not be read | Report the printed reason |

## 2. Sort
Follow `cq-aggregate`. Duo threads need triage first. SonarQube issues arrive
with a severity.

Never mark an issue "false positive", "accepted" or "won't fix" in SonarQube,
and never resolve a Duo thread to make it go away. If you think a finding is
wrong, mark it `contested` and let the human decide.

## 3. Fix (bounded)
While findings need a fix:

1. `cq fix round <run> --phase mr`. If cq refuses, stop and escalate.
2. Start `cq-fixer` with the ordered finding ids. SonarQube findings are not
   marked fixed by hand: they close when SonarQube stops reporting them.
3. `git push`, then `cq run sync <run>` and `cq check run <run> --required`.
4. The narrow second review, exactly as in `cq-run` step 6.4.
5. Step 1 of this skill again: the new commit needs a new Duo review and a new
   analysis.

## 4. Answer every Duo thread
Each thread ends with a reply and a resolution, after its finding has an
outcome on record. Write the reply to a file in short plain sentences:

- fixed: what changed, and the commit.
- filed: why it can wait, and the ticket key.
- rejected (MINOR or NIT only): the specific reason.

    cq duo reply <run> <finding-id> --body-file <file> --resolve

cq refuses `--resolve` while the finding is still open. For a contested
finding, reply with both positions (no `--resolve`) and leave the thread open
for the human.

## 5. Refresh
Before you return to `cq-run`:

    cq duo ingest <run>
    cq sonar gate <run>
