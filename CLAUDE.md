# CLAUDE.md

Guidance for agents working on this repository (the `cq` system itself, not a
project that `cq` reviews).

## Layout

- `control/` is the Python package `cq-control` (`src/cq/`) plus its policy,
  config, templates and tests. `install.sh` copies it to `~/cq-control`.
- `docs/system-guide.md` is the operating manual. When behaviour or
  configuration changes, update it in the same change, and `README.md` if the
  overview is affected.
- `TODO.md` tracks open work; tick or remove items you finish. Record changes
  under "Unreleased" in `CHANGELOG.md`.

## Running tests

```bash
cd control
# fast (~2 s): what install.sh and CI run on every push
PYTHONPATH=src python3 -m unittest tests.test_policy tests.test_units tests.test_templates tests.test_engine.Units tests.test_engine.JiraSearch
# full (~3.5 min): end to end against fake glab, claude, gemini, Jira, SonarQube
PYTHONPATH=src python3 -m unittest discover -s tests
```

Dependencies are only `PyYAML` and `jsonschema`. Support Python 3.9+: no
`match`, no `X | Y` type unions at runtime, keep `from __future__ import annotations`.

## Conventions

- Standard library first; no new runtime dependencies without a strong reason.
- `tests/test_policy.py` is the verdict contract: same input, same verdict;
  missing or stale evidence is never a pass. Never weaken it to make a change pass.
- Tests use their own policy copies in `tests/fixtures/`; a change to a config
  schema (`engine/settings.py`, `config.py`) needs the shipped file
  (`engine.yml`, `registry.yml`, `policy/*`) and the fixture kept valid.
- Unknown config fields are rejected by design. Add new fields to the schema,
  with a default, and document them in the shipped file's comments.
- Human-only commands (`cq approve`, `cq decide`, `cq tier lower`,
  `cq fix extend`, `cq run close`) must keep refusing to run inside Claude Code.
- `cq` exit codes are a contract: verdicts 0–3, 4 refused/bad input, 5 bug.
