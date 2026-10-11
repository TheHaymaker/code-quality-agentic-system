---
name: cq-panel
description: Start the independent reviewers a cq quality run requires for its tier, in parallel and in isolation, so each submits its findings to cq. Use from cq-run after the deterministic checks pass, or when the user asks for the review panel on a run.
argument-hint: "<run-id>"
---

# cq-panel: independent reviews

Run id: $ARGUMENTS

1. `cq run show <run>` lists "required reviewers" for this tier. That list,
   without `final_check`, is the panel. Do not add or drop a reviewer.
   `final_check` judges the finished change, so `cq-run` starts it at the end.

2. Start every panel reviewer at once, in one message, so they run in parallel:
   - `simplicity`, `structure`, `standards_spec`, `tests`, `security`:
     one `cq-reviewer` agent each. The prompt is only:
     "Run <run-id>, reviewer <name>."
   - `adversary`: `cq adversary run <run>`
   - `adversary2`: `cq adversary run <run> --slot adversary2`

3. Keep them independent. Do not describe the change to a reviewer. Do not
   pass one reviewer's output to another. Do not tell a reviewer what you
   expect it to find.

4. When they return, run `cq verdict <run> --stage local`. A line that says a
   panel reviewer "has not reviewed this change" means its submission did not
   arrive. Start that reviewer once more. (The same line for `final_check` is
   expected at this point.) If it fails again, leave it missing and say
   why (`SKILL_MISSING`, adversary not configured, error). The run is then
   INCONCLUSIVE, which is the correct result.

Do not read the findings here and do not fix anything. Next is `cq-aggregate`.
