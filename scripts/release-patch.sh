#!/usr/bin/env bash
# release-patch.sh: a batched patch release of Tutti, run by self-improvement (projects.<name>.self_improve.release_command)
# or by hand from a checkout of the repository. Bumps the patch past the newest v* tag, writes the CHANGELOG entry from the
# commits since that tag, opens a pull request, waits for every check, merges it (never --admin), tags the merge commit and
# creates the GitHub release. It stops, changing nothing more, at the first step that fails.
# Usage: scripts/release-patch.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
release_root=$PWD
dry=0
[[ "${1:-}" = "--dry-run" ]] && dry=1
git fetch -q --tags origin main
base_sha=$(git rev-parse origin/main)
last=$(git tag --list 'v*' --sort=-v:refname | head -n 1)
[[ -n "$last" ]] || { echo "release-patch: no v* tag yet: tag the first release by hand" >&2; exit 2; }
if [[ -z "$(git log --format=%s "$last".."$base_sha")" ]]; then
	echo "release-patch: nothing merged since $last: no release"
	exit 0
fi
IFS=. read -r major minor patch <<< "${last#v}"
next="$major.$minor.$((patch + 1))"
while git rev-parse -q --verify "refs/tags/v$next" > /dev/null; do next="$major.$minor.$((${next##*.} + 1))"; done
notes=$(git log --format='- %s' "$last".."$base_sha" | grep -v '^- chore(release)' || true)
echo "release-patch: $last -> v$next"
printf '%s\n' "$notes"
[[ "$dry" = 1 ]] && exit 0
wt=$(mktemp -d)
assets=''
cleanup() {
	git -C "$release_root" worktree remove --force "$wt" > /dev/null 2>&1 || true
	rmdir "$wt" > /dev/null 2>&1 || true
	if [[ -n "$assets" ]]; then rm -rf -- "$assets"; fi
}
trap cleanup EXIT
branch="release/v$next"
# A concurrent or abandoned release belongs to its creator. Never delete or replace its branch.
if ! git worktree add -q -b "$branch" "$wt" "$base_sha"; then
	echo "release-patch: cannot create $branch: it may already exist; inspect its release PR before retrying" >&2
	exit 1
fi
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
# The empty lease makes creation atomic: never update a remote branch that appeared concurrently.
if ! git push -q --force-with-lease="refs/heads/$branch:" -u origin "$branch"; then
	echo "release-patch: could not reserve $branch: it may already exist or the push failed; inspect it before retrying" >&2
	exit 1
fi
gh pr create --base main --head "$branch" --title "chore(release): $next" --body "Batched patch release by self-improvement.

$notes"
for _ in $(seq 1 30); do   # a new pull request's checks take a moment to register
	[[ -n "$(gh pr checks "$branch" --json name --jq '.[].name' 2> /dev/null)" ]] && break
	sleep 10
done
gh pr checks "$branch" --watch --fail-fast
head_sha=$(git rev-parse HEAD)
git fetch -q origin main
[[ "$(git rev-parse origin/main)" = "$base_sha" ]] || {
	echo "release-patch: main advanced during review: close or update this release PR and rerun its checks before merging" >&2
	exit 1
}
gh pr merge "$branch" --squash --match-head-commit "$head_sha"
sha=$(gh pr view "$branch" --json state,mergeCommit --jq '. | select(.state == "MERGED") | .mergeCommit.oid')
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "release-patch: the merged PR's commit is unavailable: inspect the PR before tagging" >&2; exit 1; }
git fetch -q origin main
git merge-base --is-ancestor "$sha" origin/main || { echo "release-patch: the PR merge is not on origin/main: inspect it before tagging" >&2; exit 1; }
parent=$(git show -s --format=%P "$sha")
[[ "$parent" = "$base_sha" ]] || {
	echo "release-patch: main changed while merging: the PR merged on a different base; inspect and retest before tagging" >&2
	exit 1
}
git tag -a "v$next" "$sha" -m "Tutti $next"
git push -q origin "v$next"
assets=$(mktemp -d)
bash scripts/release-assets.sh "$next" "$assets" "v$next"   # the one-line install's files (docs/INSTALL.md)
# a draft first, published once its files are attached: the release workflow (on published) then rebuilds them with the
# release defaults (tutti/official.py) and is always the last upload
gh release create "v$next" --title "Tutti $next" --notes "$notes" --verify-tag --draft "$assets"/*
gh release edit "v$next" --draft=false
echo "release-patch: v$next released"
