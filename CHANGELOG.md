# Changelog

All notable changes to this project are recorded here. Versions follow
`control/pyproject.toml`.

## Unreleased

### Added
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
