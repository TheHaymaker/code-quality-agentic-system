#!/usr/bin/env bash
# Installs the quality control repo and the `cq` command.
#
#     ./install.sh [control-dir]        default: ~/cq-control
#
# Keep the control repo outside every project the agents work on.
# Safe to run again: code, schemas, templates and tests are refreshed.
# Your policy files, registry.yml and engine.yml are never overwritten.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/control"
DEST="${1:-$HOME/cq-control}"

say() { printf '[cq-install] %s\n' "$1"; }
die() { printf '[cq-install] %s\n' "$1" >&2; exit 1; }

command -v git >/dev/null || die "git is required"
command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "Python 3.9 or newer is required"
[ -d "$SRC/src/cq" ] || die "cannot find control/ next to this script"

mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd)"
[ "$DEST" != "/" ] || die "refusing to install into /"

if [ "$SRC" -ef "$DEST" ]; then
  say "installing in place: $DEST"
else
  for d in src templates tests hermes; do
    rm -rf "${DEST:?}/$d"
    cp -R "$SRC/$d" "$DEST/$d"
  done
  cp "$SRC/pyproject.toml" "$DEST/pyproject.toml"
  mkdir -p "$DEST/policy"
  for f in policy/floor.yml policy/models.yml policy/gemini-readonly.toml policy/gemini-settings.json registry.yml engine.yml; do
    if [ -e "$DEST/$f" ]; then
      say "kept   $f (yours)"
    else
      cp "$SRC/$f" "$DEST/$f"
      say "wrote  $f"
    fi
  done
  # cq's own hook settings for engine sessions: always the current version.
  cp "$SRC/policy/claude-engine.json" "$DEST/policy/claude-engine.json"
fi
[ -e "$DEST/.gitignore" ] || printf '.venv/\n__pycache__/\n*.egg-info/\n' > "$DEST/.gitignore"

say "creating the virtual environment"
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip
"$DEST/.venv/bin/python" -m pip install --quiet -e "$DEST"

mkdir -p "$HOME/.local/bin"
ln -sf "$DEST/.venv/bin/cq" "$HOME/.local/bin/cq"
say "linked $HOME/.local/bin/cq"

if git -C "$DEST" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  say "$DEST is already under git"
else
  git -C "$DEST" init -q
  say "git init in $DEST (commit it, so every policy change has history)"
fi

say "running the policy contract tests"
(cd "$DEST" && .venv/bin/python -m unittest -q tests.test_policy tests.test_units tests.test_templates tests.test_engine.Units)

cat <<MSG

Installed in $DEST

Next:
  1. Put ~/.local/bin on your PATH if it is not there.
  2. Register a repository in $DEST/registry.yml, and fill in $DEST/engine.yml (Jira).
     Environment: JIRA_EMAIL, JIRA_API_TOKEN, SONAR_TOKEN, GEMINI_API_KEY.
  3. cq init-repo <path-to-that-repository>      installs .cq/ and .claude/ files
  4. cq doctor                                    checks everything
  5. cq engine run <TICKET>                       one ticket, end to end
     Hermes: copy $DEST/hermes/cq-operator into your Hermes skills folder.
  6. cq dashboard --open                          watch repos, merge requests and runs
  7. Full test suite, about six minutes:  (cd $DEST && .venv/bin/python -m unittest discover -s tests)
MSG
