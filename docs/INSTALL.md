# Installing Tutti

One line installs Tutti and everything it needs in your own user space, then `tutti setup` finds what you have and
fills the config. You sign in to your tools; nothing else is manual.

## The one line

**Linux, WSL and macOS** (a terminal):

```bash
curl -fsSLo tutti-bootstrap.sh https://github.com/ventura8/Tutti/releases/latest/download/bootstrap.sh && echo "17ccfd80f790fb48fc1642870b58e70a396a473bee88d279ba7ab6256eb56635  tutti-bootstrap.sh" | (sha256sum -c - || shasum -a 256 -c -) && bash tutti-bootstrap.sh
```

**Windows** (PowerShell):

```powershell
irm https://github.com/ventura8/Tutti/releases/latest/download/bootstrap.ps1 -OutFile tutti-bootstrap.ps1; if ((Get-FileHash tutti-bootstrap.ps1).Hash -eq 'CAACFE5D2E3F84B59D62263D5434EA9120E3BC10A7D2ECD6723C140A21A032C8') { powershell -ExecutionPolicy Bypass -File tutti-bootstrap.ps1 } else { 'tutti: checksum mismatch, not run' }
```

Nothing is asked. At the end `tutti setup` lists the few things only you can do (a sign-in, a sudo line, a tap on a
phone) and opens each sign-in in the tool's own window, one after another. That is all.

The line downloads a small, readable bootstrap script attached to the GitHub release, checks its SHA-256 against the
one written in the line, and only then runs it. Nothing unverified is ever piped into a shell. The bootstrap:

1. installs, when missing, [uv](https://github.com/astral-sh/uv) 0.13.8 (Python without sudo; uv fetches its own
   CPython when the system has none) and [gh](https://cli.github.com) 2.104.0, each checked against a SHA-256 pinned in
   the script; on Windows Git comes from winget in user scope; on macOS a missing git opens Apple's Command Line Tools
   installer; on Linux a missing git is the one thing that needs your package manager (`sudo apt install git`), and
   `tutti setup` reminds you;
2. downloads the release tarball `tutti-<version>.tar.gz`, checks it against the release's `SHA256SUMS`, unpacks it
   into `~/.local/share/tutti/versions/<version>` (`%LOCALAPPDATA%\tutti` on Windows), makes the Python environment
   `venv/` with PyYAML, points `current` at it and puts a `tutti` command in `~/.local/bin` (added to your login
   profile's PATH once);
3. runs `tutti setup`.

Run the line again to repair an install. `--version <x.y.z>` (`-Version` on Windows) pins a release; `--no-setup`
skips step 3.

After the install, open the dashboard and choose **Sign in with Google** (or scan its QR code from a phone): the first
person to sign in is the administrator. Sign-in is built into the release; nothing is created in Google Cloud
(docs/AUTH.md). Something wrong? **Report a problem** on the dashboard, or `tutti report` (docs/REPORT.md).

## `tutti setup`

The first run, with no questions. What it can find, it finds; for the rest it picks the recommended default and the
dashboard's **Set up** card lists each default with a **Change** link. `--ask` asks about each default instead.

**Found by itself:**

| What | How |
| --- | --- |
| Agents: `claude`, `cursor-agent`, `agy`, `copilot` | on PATH; each becomes a worker entry |
| Their sign-in | the tool's own status command (`cursor-agent status`) or whether its login file exists; no credential file is ever opened |
| Plans | where the tool prints one (`cursor-agent about`); otherwise one question with a default |
| Local models | `ollama list`; the first becomes the `local` worker |
| GPUs | `nvidia-smi`, `rocm-smi`, an Apple chip |
| Machines | `Host` entries of `~/.ssh/config` (offered for heavy runs), Tailscale peers (`tailscale status --json`) and LAN ssh services (`avahi-browse`) as candidates |
| Android devices | `adb devices -l` |
| Projects | git repositories one level under `~/Projects`, `~/src`, `~/code`, `~/dev`, `~/git`, `~/repos`, `~/workspace`, `~/work`, `~/Documents/GitHub`, `~/source/repos`; the most recently worked on gets `priority: 0` (the most important), the next 1, and so on |
| GitHub | `gh auth status` |

**What it does:**

- connects every agent it found to Tutti's MCP server (`tutti connect --all`, docs/CONNECT.md; `--no-connect` skips it; the restarts an agent needs join the one list at the end);
- writes `~/.config/tutti/config.yaml` (or the config Tutti already finds) with the found entries and sensible defaults:
  the main agent (Claude first, then Cursor, Antigravity, Copilot), `machines.here`, the workers, accounts with an
  estimated reset, the projects with their priorities, `self_update: {enabled: true}`. An existing config keeps every
  comment and line: entries are only added, and the old file is kept as `config.yaml.bak`. The result is checked by
  `tutti config`'s own rules before it is written;
- at the end, lists once what only you can do, sign-ins first, and opens **each tool's own sign-in** one after another
  (`cursor-agent login`, `claude /login`, `gh auth login --web`, ...). Tutti never sees, asks for or stores a credential;
- installs the dashboard as a login service: a `systemd --user` unit (`tutti-dashboard.service`), a launchd agent
  (`~/Library/LaunchAgents/io.github.tutti.dashboard.plist`) or a Windows scheduled task at logon (`tutti-dashboard`,
  no console window). WSL without systemd gets the exact steps to turn it on;
- on the first run, opens the dashboard so you can install it as an app (the browser's menu, *Install*);
- runs `tutti doctor` and fixes what it can: a first `git` in PATH that cannot do HTTPS (a tool's bundled git, say) gets
  a `~/.local/bin/git` wrapper that runs the working one;
- saves what it found and what is left to `<state>/setup.json`: the dashboard's **Set up** card.

Running it again only fills what is missing: no entry is added twice, an unchanged service file is not rewritten.

## The rule for every setup

The owner's rule: minimum input, and none at all is best. `tutti setup`, `tutti household join`, `tutti connect`,
`tutti vpn setup` and `tutti move` all follow it, through the shared module `tutti/guided.py`:

1. **Detect** everything that can be detected.
2. **Default** the rest: pick the recommended value and record it with `guided.choice(key, value, why)`; never ask while
   running. The Set up card shows each choice with a Change link to its config key.
3. **List once** what no tool can do for the person, with `guided.step(kind, title, command, why)` (kind: `sign-in`,
   `sudo`, `phone`), and print `guided.render(steps)` at the end: one numbered list, sign-ins first, each with its exact
   command. A sign-in opens in the tool's own window; never in Tutti.

A feature's setup returns its choices and steps; `tutti setup` adds them to its report (`<state>/setup.json`), so the
card is the one place that shows every default and every step left.

## The Set up card

The dashboard's **Set up** card lists what setup found, the defaults it picked (each with **Change**) and what only you
can do. A row left to do has its exact command (with a Copy button), or **Fix**, which runs `tutti setup` in the
background (it fills what is missing, asks nothing).
Sign-ins need a terminal, because each tool's own sign-in runs there: the card shows the command.

## Updates

Tutti updates itself, on by default: at every login (the service runs `tutti self-update --auto` before the dashboard)
and once a day from the running dashboard, which then restarts in place; the open page reloads itself for the new
version. An update reads the latest release's `SHA256SUMS`, downloads the newer tarball, checks it, installs its
requirements and only then moves `current`; any failure leaves the running version in place. `tutti self-update
--check` only says whether one is out. Opt out with `self_update: {enabled: false}` in the config.

### How a release is checked

A checksum alone only proves the files match each other. Every release's `SHA256SUMS` is also **signed with Sigstore,
keyless**: the release workflow (`.github/workflows/release.yml`) signs it with a short-lived certificate GitHub's OIDC
issues to that workflow run (actions/attest-build-provenance) and attaches the bundle as `SHA256SUMS.sigstore.json`.
`tutti self-update` and both bootstrap scripts run `gh attestation verify` on it and install nothing unless:

- the signature is valid and logged in Sigstore's transparency log;
- the signer is `ventura8/Tutti/.github/workflows/release.yml`;
- it was made on the tag of the version being installed (`refs/tags/v<version>`), never a branch.

Then the tarball is checked against that `SHA256SUMS`. No key is kept anywhere. Someone who only steals a token that can
upload release files cannot make that signature; someone who can push a `v*` tag can (they can run the workflow from it),
so the repository's `v*` tags should be protected by a ruleset (only the maintainer creates them). A refusal says why (no
signature file, not signed by the workflow, signed on another ref, gh missing or older than 2.49) and leaves the running
version in place. Right after a release is published its workflow takes a minute or two to add the signature; an update
in that window is refused and the next try takes it. Releases before 0.9.2 have no signature.

**Offline fallback** (a release you checked another way): `tutti self-update --insecure-skip-verify`,
`bash tutti-bootstrap.sh --insecure-skip-verify`, or `-InsecureSkipVerify` for `bootstrap.ps1`. It prints a loud warning;
the daily and login updates (`--auto`) never skip the check.

Worker CLIs (Claude Code, Cursor, Antigravity, Copilot, Ollama) are kept current by `tutti update`, which the dashboard
runs daily where the tool's package manager allows it (docs/WORKERS.md).

## Other ways

| Channel | Install | Update |
| --- | --- | --- |
| uv | `uv tool install git+https://github.com/ventura8/Tutti@v<version>` (a release tag) | the same with `--force` and the newer tag |
| pipx | `pipx install git+https://github.com/ventura8/Tutti@v<version>` (a release tag) | the same with `--force` and the newer tag |
| npm (not published yet) | `npm install -g tutti-agents` | `npm update -g tutti-agents` |
| From source | `git clone https://github.com/ventura8/Tutti` and `install/install.sh` (or `install.ps1`): the newest release tag, hash-checked requirements | run the script again |

Then run `tutti setup`. npm is a ready hook: `tutti/install.py` lists it under `CHANNELS` with `published: False`; the
first publish flips it. `tutti self-update` updates only a release install and names the right command otherwise.

## The interface (for `tutti household join` and other callers)

The shared module is `tutti/install.py`; the setup steps are in `tutti/setupcmd.py`. Callers use only these:

| Call | What it gives |
| --- | --- |
| `install.install_line(platform, join=None)` | the exact one-line install for `linux` / `wsl` / `darwin` / `win32`, with `--join "<invite>"` appended when given: what a household invite page shows a new member |
| `tutti setup --join <invite>` | setup (no questions), then `tutti household join <invite>` with the config it wrote; the member's sign-ins open at the end |
| `vpncmd.guided_setup(cfg)` | the optional private network's (choices, steps), changing nothing (docs/NETWORK.md) |
| `guided.choice()`, `guided.step()`, `guided.render()`, `guided.choice_rows()` | the rule above: a default picked, a step only the person can do, the one list at the end, the card's rows |
| `bootstrap.sh --join <invite>` / `bootstrap.ps1 -Join <invite>` | install, then `tutti setup --join <invite>` |
| `install.home()`, `install.is_release_install(root, home)` | where a release install lives; whether the running Tutti is one |
| `install.service_plan(platform, home)` | the login service's files and commands, per OS |
| `install.self_update(home)` / `tutti self-update` | the checked update; `(code, message)` |
| `setupcmd.read_report(cfg)` | the last setup report (`<state>/setup.json`): the Set up card's rows |
| `install.CHANNELS` | the distribution channels, npm's `published` flag |

So a new member's machine is: the coordinator's invite shows `install_line(platform, join=invite)`; the member pastes
it; bootstrap installs, `tutti setup` fills the config and opens sign-ins, and `household join` pairs the machine.

## Releases

`scripts/release-assets.sh <version> <dir> [ref]` makes the release's files: `tutti-<version>.tar.gz` (a `git
archive`), `bootstrap.sh`, `bootstrap.ps1` and `SHA256SUMS` over the three. `scripts/release-patch.sh` attaches them to
every release; the release workflow builds them again, signs `SHA256SUMS` and adds `SHA256SUMS.sigstore.json` (above). A
rebuild by hand is dispatched on the tag itself, `gh workflow run release.yml --ref vX.Y.Z -f tag=vX.Y.Z` (a dispatch on a
branch stops at once: its signature would name the branch and every install would refuse it). A dispatch for a tag that has
no release yet creates it (notes from CHANGELOG.md), attaches the signed files and publishes it.

**Releases stuck.** Installs update only to published releases. Where Tutti runs from a git checkout of its own repository,
`tutti doctor` and the dashboard's banner say "releases stuck: N merged version(s) unpublished" when CHANGELOG.md names
versions newer than the latest GitHub release, with why (a refused tag, a tag without a release) and the fix. When a bootstrap script changes, its SHA-256 in this file and in the README changes with it (a unit test
checks they match: `python3 -c "from tutti import install; print(install.install_line('linux'))"` prints the line).

## Uninstall

`rm -rf ~/.local/share/tutti ~/.local/bin/tutti` and `systemctl --user disable --now tutti-dashboard` (macOS:
`launchctl unload ~/Library/LaunchAgents/io.github.tutti.dashboard.plist`; Windows: `schtasks /Delete /TN
tutti-dashboard /F` and remove `%LOCALAPPDATA%\tutti`). Your config in `~/.config/tutti` stays.
