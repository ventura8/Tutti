# Self-healing

Tutti keeps itself working: what is missing is installed, what is down is started, what is broken is put back. A repair is
done, not reported as a failure; it is logged, and the dashboard's Machines card shows **Self-healed: N today** with each
repair (`tutti selfheal` prints the same). Only a step a person must take (signing in, a command that needs root, making
room the disk still lacks) becomes one item in the needs-input queue ([NEEDS.md](NEEDS.md)), once per problem.

## When it runs

Every `selfheal.every_seconds` (default 300) inside the failover round's service ([FAILOVER.md](FAILOVER.md)), on every
machine that runs it, in a thread of its own (one at a time), so a long repair such as a model pull never delays the
failover heartbeat; `tutti selfheal --once` runs every check now on this machine. `selfheal: {enabled: false}` turns it
off. Each repair is a line in `<state>/selfheal.jsonl` (the latest 2,000 kept) with the time, the machine, the check and
what was done.

## The checks and their repairs

| Check | Probe | Repair |
| --- | --- | --- |
| needs | each declared need of this machine's workers (those naming it with `machine:`, or none on the main machine), projects and suites (below) | installed at user level; root-only: one admin command in Needs you |
| workers | a CLI worker's program on PATH; its `login_check` command | installed through its needs; otherwise, or signed out, one Needs-you item with its `login` command |
| ollama | a local worker's model server answers `/api/tags`, with the model | `systemctl --user start ollama` or `ollama serve`; `ollama pull <model>` |
| strays | Tutti's own helper processes of this user (a device's keep-awake loop, a tunnel loop, the sleep inhibitor) that nothing owns: their test run or home folder gone, an inhibitor whose Tutti process ended, a loop its pid file does not name | stopped by exact pid (SIGTERM) and logged; never another user's process, never by pattern; `tutti reap [--dry-run]` does the same by hand |
| tunnels | `tutti tunnel status <worker>` for each worker with `tunnel:` | `tutti tunnel start <worker>` |
| adb | `adb devices` answers; each wireless address of this machine's devices is listed | `adb kill-server; adb start-server`; `adb connect <address>` |
| disk | Tutti's own left-over temp folders; free space and file slots ([diskroom](../tutti/diskroom.py)) | removed (below); still low: one Needs-you item to make room |
| state | each state JSON file parses | put back from its last good snapshot (the broken copy kept as `.corrupt`) |
| config | the config file is YAML | unreadable for 2 minutes (not a save in progress): the newest backup that loads (Settings' `config-backups/`) put back, the broken one kept as `.bad`; a config that parses but fails a check is a person's edit and is never replaced |
| dashboard | the dashboard answers on the machine that serves | started again |
| update | a release install is on the newest release (asked at most every 6 h) | `self-update`: the checksummed, signed release installed, unattended. On by default, like the dashboard's daily update; `self_update: {enabled: false}` turns both off |

One check that fails (an exception) is logged and the others go on.

## Needs

A project, a test suite or a worker declares what it needs; before a dispatched suite runs, its project's and suite's
needs are probed and installed on the machine that runs it (the command is prefixed by the generated script), and each
self-heal round does the same for this machine's workers and projects. Everything is pinned and checked:

```yaml
projects:
  app:
    needs: [{npm: typescript@5.6.3}, {pip: ruff==0.8.1}, {apt: adb}]
    tests:
      e2e:
        needs: [{playwright: chromium, version: 1.49.0}, {docker: 'node@sha256:<64 hex>'}, {dotnet-workload: android}]
workers:
  big: {kind: ollama, host: 127.0.0.1:11434, model: qwen3:8b, needs: [{ollama: qwen3:8b}]}
  pwsh-runner:
    kind: cli
    command: [pwsh, -c, '{prompt}']
    needs: [{bin: pwsh, download: {url: 'https://example.org/pwsh.tar.gz', sha256: '<64 hex>', unpack: tar, path: pwsh}}]
```

| Kind | Pinned by | Installed with (user level) |
| --- | --- | --- |
| `npm` | `pkg@x.y.z` | `npm install -g --prefix ~/.local --ignore-scripts` (npm checks the registry's integrity hash) |
| `pip` | `pkg==x.y.z`, optional `sha256` | `python3 -m pip install --user` (`--require-hashes` with `sha256`: only for a package with no dependencies, since pip then wants every dependency's hash). A Python that refuses `--user` (PEP 668): give the need a `root:` command |
| `playwright` | `version` | `npx playwright@<version> install <browser>`; a stamp per version and browser in `~/.local/share/tutti-deps` says it is done |
| `ollama` | the model tag | `ollama pull` |
| `docker` | `image@sha256:<digest>` | `docker pull` |
| `dotnet-workload` | the workload id | `dotnet workload install`; when that needs root, the admin's command |
| `bin` | `download: {url (https), sha256, unpack: zip\|tar\|none, path}` | downloaded to a file, its checksum checked, unpacked under `~/.local/share/tutti-deps/`, linked into `~/.local/bin` |
| `apt` (or `root:` on any need) | the package | never by Tutti: `sudo apt-get install -y <pkg>` (or the `root:` command) goes to Needs you |

A download is never piped into a shell, and nothing runs before its checksum matches. An unpinned need is a config error
naming the fix (`tutti config`).

## Temp janitor (backlog t86)

Every round removes Tutti's own left-over temp folders of this user in which nothing changed for 2 hours (the newest
time anywhere inside counts, so a run that keeps writing is kept; one silent for 2 hours is not protected): `tutti-tests-*`, `tutti-t-*`,
`tutti-review-*`, and anonymous `tmpXXXXXXXX` folders holding only what a Tutti test or run writes (`state`,
`tutti.config.yaml`, ...), in the temp folder, `~/.cache/tutti-tmp` and `selfheal.temp_dirs`. The test helper
(`tests/tmpdirs.py`) names its folders `tutti-t-*`. This replaces the stopgap cron that cleaned `/tmp` by hand.

## Stray processes (0.16.1)

On 2026-11-15 the laptop sat at load 60: 161 keep-awake loops of device leases made by tests (their temp home was gone,
so their `mkdir` lock could never succeed and each spun every 0.1 s), 38 tunnel loops of the e2e test host
`gpu-box.example` and 45 `systemd-inhibit ... sleep infinity` left by dashboard restarts. Now:

- every helper carries `TUTTI_PROC=<kind>[:<name>]` in its environment (`tutti/procs.py`);
- tests and `scripts/check.sh` set `TUTTI_NO_DAEMONS=1`: no helper is started at all, and `tests/run.py` fails a test that
  leaves a process of its run behind (and stops it by pid);
- a device has one keep-awake loop: a loop that `awake.pid` does not name ends, and one whose folder is gone ends;
- a tunnel has one loop: the check and the pid file are written under `flock`;
- the sleep inhibitor is one per Tutti process, taken only while a job runs, and its child is `tail --pid=<tutti>`, so it
  ends with Tutti;
- the `strays` check (and `tutti reap`) stops what is left, by exact pid, and logs each one.
