#!/usr/bin/env bash
# release-assets.sh: the files a GitHub release of Tutti carries for the one-line install (docs/INSTALL.md): the source
# tarball tutti-<version>.tar.gz, the two bootstrap scripts and SHA256SUMS over all three. release-patch.sh attaches them.
# Usage: scripts/release-assets.sh <version> <out dir> [<git ref, default HEAD>]
set -euo pipefail
cd "$(dirname "$0")/.."
version="${1:?usage: release-assets.sh <version> <out dir> [ref]}"
out="${2:?usage: release-assets.sh <version> <out dir> [ref]}"
ref="${3:-HEAD}"
mkdir -p "$out"
git archive --format=tar.gz --prefix="tutti-$version/" -o "$out/tutti-$version.tar.gz" "$ref"
git show "$ref:install/bootstrap.sh" > "$out/bootstrap.sh"
git show "$ref:install/bootstrap.ps1" > "$out/bootstrap.ps1"
(cd "$out" && sha256sum "tutti-$version.tar.gz" bootstrap.sh bootstrap.ps1 > SHA256SUMS)
echo "release-assets: $out/{tutti-$version.tar.gz,bootstrap.sh,bootstrap.ps1,SHA256SUMS}"
