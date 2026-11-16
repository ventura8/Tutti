#!/usr/bin/env bash
# release-patch.sh: a batched patch release of Tutti, run by self-improvement (projects.<name>.self_improve.release_command)
# or by hand from a checkout of the repository. At merge time it serializes release runs with a remote lock, picks the next
# free v* patch tag from existing tags, writes the version and CHANGELOG heading on top of origin/main, tags that commit and
# creates the GitHub release. It stops, changing nothing more, at the first step that fails.
# Usage: scripts/release-patch.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
dry=0
[[ "${1:-}" = "--dry-run" ]] && dry=1
fail() {
	echo "release-patch: $*" >&2
	exit 1
}
next_version() {
	local last_tag major minor patch candidate
	last_tag="$1"
	IFS=. read -r major minor patch <<< "${last_tag#v}"
	candidate="$major.$minor.$((patch + 1))"
	while git rev-parse -q --verify "refs/tags/v$candidate" > /dev/null; do
		candidate="$major.$minor.$((${candidate##*.} + 1))"
	done
	printf '%s\n' "$candidate"
}
lock_ref="refs/tutti/release-patch-lock"
lock_acquired=0
wt=""
assets=""
cleanup() {
	if [[ -n "$wt" ]]; then git worktree remove --force "$wt" > /dev/null 2>&1 || true; fi
	if [[ -n "$assets" ]]; then rm -rf "$assets"; fi
	if [[ "$lock_acquired" = 1 ]]; then git push -q origin ":$lock_ref" > /dev/null 2>&1 || true; fi
}
trap cleanup EXIT
if ! git push -q origin "HEAD:$lock_ref" > /dev/null 2>&1; then
	fail "another release run is assigning a version (lock $lock_ref exists)"
fi
lock_acquired=1
git fetch -q --tags origin main
last=$(git tag --list 'v*' --sort=-v:refname | head -n 1)
[[ -n "$last" ]] || fail "no v* tag yet: tag the first release by hand"
range="$last..origin/main"
if [[ -z "$(git log --format=%s "$range")" ]]; then
	echo "release-patch: nothing merged since $last: no release"
	exit 0
fi
next="$(next_version "$last")"
[[ -n "$next" ]] || fail "failed to determine the next free version tag"
notes=$(git log --format='- %s' "$range" | grep -v '^- chore(release)' || true)
[[ -n "$notes" ]] || notes='- internal maintenance'
echo "release-patch: $last -> v$next"
printf '%s\n' "$notes"
[[ "$dry" = 1 ]] && exit 0
wt=$(mktemp -d)
branch="release/v$next"
git branch -D "$branch" > /dev/null 2>&1 || true
git worktree add -q -b "$branch" "$wt" origin/main
cd "$wt"
python3 - "$next" "$notes" <<'PY'
import re
import sys
from datetime import date
version, notes = sys.argv[1], sys.argv[2]
for path, rx, repl in (
    ('tutti/__init__.py', r"^__version__ = .*$", f"__version__ = '{version}'"),
    ('package.json', r'"version": "[^"]*"', f'"version": "{version}"'),
):
    text = open(path, encoding='utf8').read()
    updated, count = re.subn(rx, repl, text, count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f'release-patch: failed to update version in {path}')
    open(path, 'w', encoding='utf8').write(updated)
path = 'CHANGELOG.md'
text = open(path, encoding='utf8').read()
head, sep, rest = text.partition('\n## ')
entry = f'\n## {version} ({date.today().isoformat()})\n\n### Changes\n\n{notes}\n'
open(path, 'w', encoding='utf8').write(head.rstrip('\n') + '\n' + entry + (sep + rest if sep else ''))
PY
git commit -q -am "chore(release): $next"
if ! git push -q origin HEAD:main; then
	fail "failed to push the release commit to origin/main"
fi
git fetch -q --tags origin main
sha=$(git rev-parse origin/main)
git tag -a "v$next" "$sha" -m "Tutti $next"
if ! git push -q origin "v$next"; then
	fail "failed to push release tag v$next"
fi
assets=$(mktemp -d)
bash scripts/release-assets.sh "$next" "$assets" "v$next"   # the one-line install's files (docs/INSTALL.md)
# a draft first, published once its files are attached: the release workflow (on published) then rebuilds them with the
# release defaults (tutti/official.py) and is always the last upload
gh release create "v$next" --title "Tutti $next" --notes "$notes" --verify-tag --draft "$assets"/*
gh release edit "v$next" --draft=false
echo "release-patch: v$next released"
