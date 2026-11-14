#!/usr/bin/env bash
# release-patch.sh: a batched patch release of Tutti, run by self-improvement (projects.<name>.self_improve.release_command)
# or by hand from a checkout of the repository. Bumps the patch past the newest v* tag, writes the CHANGELOG entry from the
# commits since that tag, opens a pull request, waits for every check, merges it (never --admin), tags the merge commit and
# creates the GitHub release. It stops, changing nothing more, at the first step that fails.
# Usage: scripts/release-patch.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
dry=0
[[ "${1:-}" = "--dry-run" ]] && dry=1
git fetch -q --tags origin main
last=$(git tag --list 'v*' --sort=-v:refname | head -n 1)
[[ -n "$last" ]] || { echo "release-patch: no v* tag yet: tag the first release by hand" >&2; exit 2; }
if [[ -z "$(git log --format=%s "$last"..origin/main)" ]]; then
	echo "release-patch: nothing merged since $last: no release"
	exit 0
fi
IFS=. read -r major minor patch <<< "${last#v}"
next="$major.$minor.$((patch + 1))"
while git rev-parse -q --verify "refs/tags/v$next" > /dev/null; do next="$major.$minor.$((${next##*.} + 1))"; done
notes=$(git log --format='- %s' "$last"..origin/main | grep -v '^- chore(release)' || true)
echo "release-patch: $last -> v$next"
printf '%s\n' "$notes"
[[ "$dry" = 1 ]] && exit 0
wt=$(mktemp -d)
trap 'git worktree remove --force "$wt" > /dev/null 2>&1 || true' EXIT
branch="release/v$next"
# a release branch left by a run that stopped half-way is replaced, so the next run never fails on it
git branch -D "$branch" > /dev/null 2>&1 || true
git push -q origin --delete "$branch" > /dev/null 2>&1 || true
git worktree add -q -b "$branch" "$wt" origin/main
cd "$wt"
python3 - "$next" "$notes" <<'PY'
import re
import sys
from datetime import date
version, notes = sys.argv[1], sys.argv[2]
for path, rx, repl in (('tutti/__init__.py', r"^__version__ = .*$", f"__version__ = '{version}'"),
                       ('package.json', r'"version": "[^"]*"', f'"version": "{version}"')):
    text = open(path, encoding='utf8').read()
    open(path, 'w', encoding='utf8').write(re.sub(rx, repl, text, count=1, flags=re.M))
path = 'CHANGELOG.md'
text = open(path, encoding='utf8').read()
head, sep, rest = text.partition('\n## ')
entry = f'\n## {version} ({date.today().isoformat()})\n\n### Changes\n\n{notes}\n'
open(path, 'w', encoding='utf8').write(head.rstrip('\n') + '\n' + entry + (sep + rest if sep else ''))
PY
git commit -q -am "chore(release): $next"
git push -q -u origin "$branch"
gh pr create --base main --head "$branch" --title "chore(release): $next" --body "Batched patch release by self-improvement.

$notes"
for _ in $(seq 1 30); do   # a new pull request's checks take a moment to register
	[[ -n "$(gh pr checks "$branch" --json name --jq '.[].name' 2> /dev/null)" ]] && break
	sleep 10
done
gh pr checks "$branch" --watch --fail-fast
gh pr merge "$branch" --squash --delete-branch
git fetch -q origin main
sha=$(git rev-parse origin/main)
git tag -a "v$next" "$sha" -m "Tutti $next"
git push -q origin "v$next"
assets=$(mktemp -d)
bash scripts/release-assets.sh "$next" "$assets" "v$next"   # the one-line install's files (docs/INSTALL.md)
# a draft first, published once its files are attached: the release workflow (on published) then rebuilds them with the
# release defaults (tutti/official.py) and is always the last upload
gh release create "v$next" --title "Tutti $next" --notes "$notes" --verify-tag --draft "$assets"/*
gh release edit "v$next" --draft=false
echo "release-patch: v$next released"
