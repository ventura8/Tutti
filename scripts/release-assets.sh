#!/usr/bin/env bash
# release-assets.sh: the files a GitHub release of Tutti carries for the one-line install (docs/INSTALL.md): the source
# tarball tutti-<version>.tar.gz, the two bootstrap scripts and SHA256SUMS over all three. release-patch.sh attaches them,
# and the release workflow builds them again with the release defaults.
# The tarball carries tutti/release.json (tutti/official.py): {"official": true}, plus the built-in device client's secret
# from $TUTTI_GOOGLE_DEVICE_SECRET and the default Sentry DSN from $TUTTI_SENTRY_DSN when they are set. Both are read from
# the environment only, never from argv, and never printed.
# Usage: scripts/release-assets.sh <version> <out dir> [<git ref, default HEAD>]
set -euo pipefail
cd "$(dirname "$0")/.."
version="${1:?usage: release-assets.sh <version> <out dir> [ref]}"
out="${2:?usage: release-assets.sh <version> <out dir> [ref]}"
ref="${3:-HEAD}"
mkdir -p "$out"
out=$(cd "$out" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git archive --format=tar --prefix="tutti-$version/" "$ref" | tar -x -C "$work"
python3 - "$work/tutti-$version/tutti/release.json" <<'PY'
import json
import os
import sys
data = {'official': True}
for key, env in (('device_secret', 'TUTTI_GOOGLE_DEVICE_SECRET'), ('sentry_dsn', 'TUTTI_SENTRY_DSN')):
    if os.environ.get(env, '').strip():
        data[key] = os.environ[env].strip()
with open(sys.argv[1], 'w', encoding='utf8') as f:
    json.dump(data, f)
print('release-assets: release.json with ' + (', '.join(sorted(data)) or 'nothing'))
PY
tar -C "$work" --sort=name --owner=0 --group=0 --numeric-owner -czf "$out/tutti-$version.tar.gz" "tutti-$version"
git show "$ref:install/bootstrap.sh" > "$out/bootstrap.sh"
git show "$ref:install/bootstrap.ps1" > "$out/bootstrap.ps1"
(cd "$out" && sha256sum "tutti-$version.tar.gz" bootstrap.sh bootstrap.ps1 > SHA256SUMS)
echo "release-assets: $out/{tutti-$version.tar.gz,bootstrap.sh,bootstrap.ps1,SHA256SUMS}"
