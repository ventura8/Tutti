#!/usr/bin/env bash
# set-device-secret.sh: the owner's one step for Tutti's built-in LAN sign-in (docs/AUTH.md, *Tutti's own sign-in*). Asks for
# the client secret of the "Tutti dashboard (LAN)" OAuth client (TVs and Limited Input devices) with hidden input and gives
# it to `gh secret set` on stdin, so it is never on screen, in argv, in a file or in the shell history. The release workflow
# writes it into each release's tutti/release.json.
# Usage: bash scripts/set-device-secret.sh [owner/repo, default ventura8/Tutti]
set -euo pipefail
repo="${1:-ventura8/Tutti}"
gh auth status > /dev/null 2>&1 || { echo "set-device-secret: gh is not signed in: run gh auth login, then this again" >&2; exit 2; }
printf 'Paste the client secret of "Tutti dashboard (LAN)" (Google Cloud console, Clients) and press Enter (not shown): '
IFS= read -rs secret
echo
[[ ${#secret} -ge 8 && "$secret" != *" "* ]] || { echo "set-device-secret: that does not look like a client secret: nothing saved" >&2; exit 2; }
printf '%s' "$secret" | gh secret set TUTTI_GOOGLE_DEVICE_SECRET -R "$repo"
unset secret
echo "set-device-secret: saved as TUTTI_GOOGLE_DEVICE_SECRET in $repo; the next release carries the built-in LAN sign-in"
echo "To rebuild the newest release now: gh workflow run release.yml -R $repo -f tag=\$(gh release view -R $repo --json tagName -q .tagName)"
