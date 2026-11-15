#!/usr/bin/env bash
# bootstrap.sh: installs Tutti on Linux, WSL or macOS in user space (no sudo), then runs `tutti setup`.
# Usage: bash tutti-bootstrap.sh [--join <household invite>] [--version <x.y.z>] [--no-setup] [--insecure-skip-verify]
# Get it with the one line in docs/INSTALL.md, which checks this file's SHA-256 before running it. Every download below is
# checked too: uv and gh against the SHA-256 pinned here, the release's SHA256SUMS against its Sigstore signature (made by
# ventura8/Tutti's release workflow on the release's tag, checked with gh), Tutti's tarball against SHA256SUMS. Re-run it to
# repair an install; `tutti self-update` keeps it current afterwards. It never asks for or stores a credential.
# --insecure-skip-verify skips the signature (offline, a release you checked another way): a loud warning says so.
set -euo pipefail

UV_VERSION=0.13.8
GH_VERSION=2.104.0
REPO=ventura8/Tutti
home="${TUTTI_HOME:-$HOME/.local/share/tutti}"
bin="${TUTTI_BIN:-$HOME/.local/bin}"
join="" version="" setup=1 skip_verify=0
while [[ $# -gt 0 ]]; do
	case "$1" in
	--join) join="$2"; shift 2 ;;
	--version) version="$2"; shift 2 ;;
	--no-setup) setup=0; shift ;;
	--insecure-skip-verify) skip_verify=1; shift ;;
	*) echo "tutti: unknown option $1 (use --join <invite>, --version <x.y.z>, --no-setup, --insecure-skip-verify)" >&2; exit 2 ;;
	esac
done

say() {
	local msg="$*"
	printf 'tutti: %s\n' "$msg"
}
die() {
	local msg="$*"
	printf 'tutti: %s\n' "$msg" >&2
	exit 1
}
sha256() {
	local file="$1"
	if command -v sha256sum > /dev/null; then sha256sum "$file" | cut -d' ' -f1; else shasum -a 256 "$file" | cut -d' ' -f1; fi
}
get() {
	local url="$1" out="$2"
	curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 -o "$out" "$url" || die "could not download $url: check the network and run this again"
}
check() { # check <file> <expected sha256>
	local file="$1" want="$2" got
	got=$(sha256 "$file")
	[[ "$got" = "$want" ]] || die "$(basename "$file") failed its checksum (got $got, want $want): not installed; run this again"
}

os=$(uname -s) arch=$(uname -m)
case "$os/$arch" in
Linux/x86_64) uv_t=x86_64-unknown-linux-gnu uv_sum=5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91
	gh_a=linux_amd64.tar.gz gh_sum=98cb101ad87dbbd3f6b2f2bfcbd43197ccd07643140121d8a9cfaa91eadbc24e ;;
Linux/aarch64 | Linux/arm64) uv_t=aarch64-unknown-linux-gnu uv_sum=74f2f5c6024d397481db228db8fc25eed420db192243f88438c118e7e013e585
	gh_a=linux_arm64.tar.gz gh_sum=cb34cefc6f4e77b90731374a2b737516bb8f3033c6ca46aa949e5344f68069e3 ;;
Darwin/x86_64) uv_t=x86_64-apple-darwin uv_sum=a04c2ffeac28b2003a2c13caf538ab425fcd7def3e4a41571f4a8fb370a66af3
	gh_a=macOS_amd64.zip gh_sum=a8c2a708dbae89dc7c05ba7b0beafc95bd8d09ab4a3505467b77d32ebf364dc6 ;;
Darwin/arm64) uv_t=aarch64-apple-darwin uv_sum=6b71028a6b904f2ae3340ef52e595512de807d06c0a20eaef2f692581b246558
	gh_a=macOS_arm64.zip gh_sum=23b12706219b9892e44236f988444a24adcf29284e16b35e03ba5b863fb6eb8e ;;
*) die "no build for $os/$arch yet: install from source (docs/INSTALL.md, *From source*)" ;;
esac
command -v curl > /dev/null || die "curl is missing: install it with your system's package manager and run this again"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$home/bin" "$home/versions" "$bin"

# uv: Python without sudo (it fetches its own CPython when the system has none)
uv=$(command -v uv || true)
if [[ -z "$uv" ]]; then
	say "installing uv $UV_VERSION (checksum pinned)"
	get "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv_t.tar.gz" "$tmp/uv.tgz"
	check "$tmp/uv.tgz" "$uv_sum"
	tar -xzf "$tmp/uv.tgz" -C "$tmp"
	cp "$tmp/uv-$uv_t/uv" "$home/bin/uv"
	uv="$home/bin/uv"
fi

# gh: GitHub's CLI, for pull requests and releases, and to check the release's signature (gh attestation verify)
gh=$(command -v gh || true)
if [[ -z "$gh" ]] || ! "$gh" attestation verify --help > /dev/null 2>&1; then
	say "installing gh $GH_VERSION (checksum pinned)"
	get "https://github.com/cli/cli/releases/download/v$GH_VERSION/gh_${GH_VERSION}_$gh_a" "$tmp/gh.pkg"
	check "$tmp/gh.pkg" "$gh_sum"
	case "$gh_a" in
	*.zip) (cd "$tmp" && unzip -q gh.pkg) ;;
	*) tar -xzf "$tmp/gh.pkg" -C "$tmp" ;;
	esac
	# Tutti's own copy beside uv when an older gh is on PATH (it is not replaced); else the user's bin
	if [[ -n "$gh" ]]; then gh="$home/bin/gh"; else gh="$bin/gh"; fi
	mkdir -p "$(dirname "$gh")"
	cp "$tmp"/gh_"$GH_VERSION"_*/bin/gh "$gh"
fi

# git: the one prerequisite a user-space installer cannot add on Linux; Tutti installs without it, its work needs it
if ! command -v git > /dev/null; then
	if [[ "$os" = Darwin ]]; then
		say "git is missing: Apple's installer opens now (Command Line Tools); Tutti goes on meanwhile"
		xcode-select --install > /dev/null 2>&1 || true
	else
		say "git is missing: install it (Debian/Ubuntu: sudo apt install git; Fedora: sudo dnf install git); tutti setup reminds you"
	fi
fi

# Tutti itself: the release tarball, checked against the release's SHA256SUMS
[[ -z "$version" || "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--version $version is not a version like 0.6.1"
if [[ -n "$version" ]]; then base="https://github.com/$REPO/releases/download/v$version"; else base="https://github.com/$REPO/releases/latest/download"; fi
get "$base/SHA256SUMS" "$tmp/SHA256SUMS"
if [[ -z "$version" ]]; then
	version=$(sed -n 's/^[0-9a-f]\{64\}  tutti-\([0-9.]*\)\.tar\.gz$/\1/p' "$tmp/SHA256SUMS" | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1)
	[[ -n "$version" ]] || die "the latest release lists no tarball in SHA256SUMS: try again later"
fi
# the signature: SHA256SUMS made and signed by ventura8/Tutti's release workflow on the tag v$version (Sigstore, keyless)
if [[ "$skip_verify" = 1 ]]; then
	say "WARNING: --insecure-skip-verify: the release's signature is NOT checked; anyone who could change the files on the way could change what is installed"
else
	curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 -o "$tmp/SHA256SUMS.sigstore.json" "https://github.com/$REPO/releases/download/v$version/SHA256SUMS.sigstore.json" ||
		die "v$version has no signature (SHA256SUMS.sigstore.json): it was not published by Tutti's release workflow; not installed (choose a newer --version, or --insecure-skip-verify offline)"
	signed=$("$gh" attestation verify "$tmp/SHA256SUMS" --bundle "$tmp/SHA256SUMS.sigstore.json" --repo "$REPO" \
		--signer-workflow "$REPO/.github/workflows/release.yml" --format json 2> "$tmp/verify.err") ||
		die "v$version's SHA256SUMS is not signed by Tutti's release workflow ($(tail -n 1 "$tmp/verify.err")): not installed"
	grep -qF "\"refs/tags/v$version\"" <<< "$signed" || die "v$version's SHA256SUMS is signed, but not on the tag v$version: not installed"
	say "v$version's SHA256SUMS: signed by Tutti's release workflow (Sigstore)"
fi
name="tutti-$version.tar.gz"
want=$(sed -n "s/^\([0-9a-f]\{64\}\)  $name\$/\1/p" "$tmp/SHA256SUMS")
[[ -n "$want" ]] || die "$name is not in the release's SHA256SUMS: not installed"
get "https://github.com/$REPO/releases/download/v$version/$name" "$tmp/$name"
check "$tmp/$name" "$want"
rm -rf "$home/versions/$version"
mkdir -p "$home/versions/$version"
tar -xzf "$tmp/$name" -C "$home/versions/$version" --strip-components 1
say "installing Python and PyYAML into $home/venv"
"$uv" venv --quiet --allow-existing --python 3.12 "$home/venv"
"$uv" pip install --quiet --require-hashes --python "$home/venv/bin/python" -r "$home/versions/$version/requirements.txt"
ln -sfn "$home/versions/$version" "$home/current.new"
mv -T "$home/current.new" "$home/current" 2> /dev/null || { rm -f "$home/current"; mv "$home/current.new" "$home/current"; }
printf '#!/bin/sh\nexec "%s/venv/bin/python" "%s/current/bin/tutti" "$@"\n' "$home" "$home" > "$bin/tutti"
chmod +x "$bin/tutti"

# PATH for new terminals: the login profile of the user's shell, once
case ":$PATH:" in
*":$bin:"*) ;;
*)
	for rc in "$HOME/.profile" "$HOME/.zprofile"; do
		{ [[ "$rc" = "$HOME/.zprofile" ]] && [[ "$os" != Darwin ]]; } && continue
		grep -qs 'tutti: user bin' "$rc" || printf '\nexport PATH="%s:%sPATH"   # tutti: user bin\n' "$bin" '$' >> "$rc"
	done
	export PATH="$bin:$PATH"
	;;
esac
say "Tutti $version installed"
if [[ "$setup" = 1 ]]; then
	if [[ -n "$join" ]]; then exec "$bin/tutti" setup --join "$join"; else exec "$bin/tutti" setup; fi
fi
