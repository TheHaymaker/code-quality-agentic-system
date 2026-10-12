# Changelog

All notable changes to this project are recorded here. Versions follow
`control/pyproject.toml`.

## Unreleased

### Added
- Streaming agent sessions: every session runs with `--output-format stream-json`
  into `~/.cq/runs/<run>/agents/*.stream.jsonl`; `cq engine watch` shows one line
  per step. `claude.keep_sessions` (default true) keeps sessions for `claude --resume`.
- Merge request inbox: `cq engine mrs` lists every open cq merge request with the
  next action and whose turn it is; `cq engine update <run> [--rebase]` brings the
  default branch in. Commits pushed on GitLab are taken in by fast-forward; diverged
  history stops the run; Duo comments after the decision are read on every pass.
- `cq dashboard`: a local, read-only page on 127.0.0.1 (turn board, runs, per-repo
  numbers, live sessions); `--snapshot` writes a self-contained copy. New
  `dashboard` section in `engine.yml`.
- `tracker.search_api` in `engine.yml`: `jql` (default, Jira Cloud
  `/rest/api/3/search/jql`) or `legacy` (Jira Data Center `/rest/api/2/search`,
  offset pagination).
- GitHub Actions workflow: the fast suite on Python 3.9 and 3.13 for every push
  and pull request, and the full suite on pull requests and nightly.
- `CLAUDE.md` with conventions for agents working on this repository.

## 0.1.0 — 2026-10-11

### Added
- Initial import of the `cq-system` archive: the control repo (policy engine,
  run store, run engine), policy files, registry and engine configuration,
  `cq init-repo` templates (skills and agents), the Hermes `cq-operator` skill,
  `install.sh`, and 225 tests against fakes.
- Top-level `README.md`, `TODO.md`, and `docs/system-guide.md` (the archive's
  original manual).
