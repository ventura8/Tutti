# Local models: every machine's Ollama as a worker

Tutti reads every machine's Ollama at each dashboard probe and turns it into a worker. While the online accounts are ahead of
their pace, or the owner's reserve holds them, the household's local models take bounded tasks first. A local model has no
quota: what limits it is its machine.

## Discovery

- **Every machine is looked at.** The probe reads `127.0.0.1:11434` on each machine over its own ssh. A different address
  goes in `energy.machines.<m>.ollama`. Android and household machines are skipped.
- **What is recorded.** Each machine's models (name, parameters, family), the loaded ones and the hardware (GPU name) go into
  `<state>/fleet.json`. A machine that does not answer keeps its last entry, marked stale.
- **Speed.** Tokens per second are measured from Ollama's own eval count and time over the last 30 days of local runs
  (`local-runs.jsonl`).
- **Workers.**
  - A machine with models and no configured `ollama` worker gets `<machine>-local`. Another machine's server is reached
    through a self-healing tunnel on `fleet.port_base` (11450) plus its index.
  - A configured worker on that machine is enriched instead: it learns the machine's models and hardware, and its own model
    stays first.
  - The workers follow the models: a removed model, or a machine with none left, drops its worker at the next probe.
- **Tunnels.** Every local worker's tunnel (the configured `tunnel:` too) is started again when its server does not answer and
  its machine is up. A yielding or paused machine is left alone.
- `tutti fleet` lists the workers, models, tiers, kinds, speed and hardware.

## Capability

Every text model is classed. Vision, embedding and speech models are skipped.

| Tier | Parameters | Kinds | Sizes |
| --- | --- | --- | --- |
| strong | 14B and more | fix, tests, docs, translations, review, summary, triage, lint-fix | S, M, L |
| normal | 7B to 14B, or a size not shown | all but lint-fix | S, M |
| small | under 7B | docs, summary, triage, translations | S |

- A translator model (translategemma, NLLB) takes only translations, and it is the first choice for them.
- A task's kind comes from its title and files. The worker uses the machine's model that best suits the kind; the configured
  model wins when it is as good.
- **Steps.** A larger task is worked in steps of a few files: 3 for a strong model, 2 for a normal one, 1 for a small one.
  Every step runs the check, and the last step's result is the run's.
- **Files.** A local model edits only the files it is given. A task that names no file gets `local_scope`: the source files
  whose name is a word of its title, or a path the title quotes. The online workers keep their whole-repository freedom.

## Model tiers and task difficulty

Each model has a measured tier (`tutti/power.py`): light, mid or strong from its size class (small, normal, strong), one
down for a 2-3 bit quantisation, then one up at a pass rate of 75 % or more, one down at 25 % or less, over at least four
attempts in Tutti's own task history (each attempt records its model). A task's difficulty is 0-2: its size (S, M, L), at

Sizes a local model takes (owner, 2026-11-17): a huge model (60B and up, such as a 70B on a 48–96 GB card) takes every
size like an online agent; a strong model (14–59B) takes S and M tasks, a normal one (7–13B) S only,
a small one S docs, summaries, triage and translations. An L task goes to a huge local model or an online account: smaller
local models failed most of them.

least 1 for a lint-fix or review, plus one per failed attempt. `model_for` picks the weakest tier that meets the
difficulty (a translator still wins translations), and `order` puts the steps in that order, easy work on the weaker
machine and hard work on the stronger one (measured power), so the strong models stay free for hard work and a failure
escalates. The local models' cards show each model's tier, why and tokens per second; a running task shows the pick.

## Routing (the autopilot)

- **First-class steps.** Local workers are added to the ladder after `autopilot.ladder`.
- **Overflow.** An account of the ladder or of `autopilot.accounts` may be ahead of its pace, in panic or spent, or every
  trigger account may have no room. Then a fresh task goes to a local model first.
- **Order.** The local models are ordered by:
  1. an idle machine first;
  2. the strongest tier for the task;
  3. the lowest energy draw (`energy.machines`).

  With `energy.prefer_cheap_hours`, local runs wait for the cheap hours.
- **Room.** A local worker takes one task at a time, of any size its models fit. Its runs do not count against the online
  limit (`tutti/parallel.py`); they count only against the machines' free gate slots.
- **Checks.** Every result passes the repository's own check, exactly as a cloud worker's.
- **Escalation.** A failed local attempt goes to the online steps first, then to another machine's model. It waits when none
  of them has room.
- **Fill room.** `fill_room` counts a free local model as one S task of room while the online accounts are spared.

## The governor

A local model takes nothing while any of these holds:

- its machine is paused by the owner, off, offline (presence) or yielding (the PC to RoRadio);
- it is hot (`heat`) or over its load ratio;
- it is in `machines.<m>.quiet_hours`;
- this computer has less memory free than `fleet.memory_floor_gb` (3);
- it runs on its CPU and every test slot is busy.

## The block resolver

When work waits and no worker takes it, the autopilot card lists each fix in order with what it found. Each one is applied
by Tutti's own rounds: the scope guess and the local routing in the plan, the tiers by pace, the household's spare share, and
the tunnels in the dashboard's probe.

1. Split or resize the task: give it files, work it in steps.
2. Route it to any local worker.
3. Lower the model tier.
4. Borrow a household member's spare share.
5. Wake an idle machine's model server (its tunnel).

Only then does the card show the owner one action. For example, "Allow cursor for autopilot first attempts for 7 days?" is one
click, or `tutti autopilot allow <account>`; otherwise the card says how to name the waiting tasks' files.

An account in `autopilot.accounts` without a `worker:` uses the worker of the same name: `claude` runs `workers.claude`. A step
that cannot be used names the real reason.

## The machine's share

`machines.<m>.use_pct` (default 100, in Settings) caps how much of a machine Tutti's work uses: its free slots (so also the
autopilot's gate slots), this computer's cores in the adaptive limit (`tutti/parallel.py`) and a local model's threads. The
share drops by itself, to the lowest that applies:

- `in_use_pct` (0: no new work, it goes to the other machines) while someone uses the machine: its keyboard or mouse
  within `in_use_minutes` (5) on an unlocked screen, read from GNOME's idle monitor (`org.gnome.Mutter.IdleMonitor`
  `GetIdletime` over the session D-Bus, X11 and Wayland), else `xprintidle`, else a login session's idle hint. GNOME never
  sets logind's idle hint, so before 0.17.4 a GNOME Wayland laptop never read as in use. See *Quiet mode* in
  docs/ARCHITECTURE.md for what else happens while a person is here.
- `quiet_pct` (50) in its quiet hours (`machines.<m>.quiet_hours`, else `autopilot.quiet_hours`): fans and noise at night.
- `hot_pct` (50) while it is hot (its `heat` cap reached).

The yield flag still takes the machine out entirely (the PC is RoRadio's): its share reads 0 % "its yield flag is up" (and
"also someone is at its keyboard" when both hold), and so does the governor's memory floor. The machine card shows
"Tutti's share N %" and why. Its slot boxes are one per slot: filled for a busy slot, hatched for one held back by the
share or the yield flag, empty for a free one, so they always add up to "F of N slots free". When the slot count does not
come from `machines.<m>.slots` (one slot per shared lock file, or the default), the card says where it comes from. A
Windows PC's CPU temperature and package power come from LibreHardwareMonitor, which Tutti installs and starts there by
itself (docs/MACHINES.md), else from its ACPI thermal zone; without a valid reading (20 to 110 C) the card says so. GPU power limits need root, so Tutti does not change them.

## Model servers kept alive

At every probe, which also follows a reboot (the dashboard starts at login) and sleep:

- Every local worker's tunnel is started again when its server does not answer.
- A machine whose Ollama had models and went quiet, while the machine itself answers, gets a restart (at most every 10
  minutes): its user service (`systemctl --user restart ollama`), else `ollama serve` in the background.
- When Tutti cannot do it, `tutti fleet` and `fleet.json` say the one-step fix:
  - a system service: `sudo systemctl restart ollama` on that machine;
  - Windows: start the Ollama app.

## Household use

Each probe samples every machine:

- **Available capacity:** its slots at its share, plus one for a model server.
- **Busy:** its slots taken and a model run going.

A paused, offline or yielding machine has nothing available. The Usage card's headline reads "Household use: X % of
available capacity (last hour)". Each machine card shows its use over the last hour and today, and why it is idle when it is.

## The work queue

Machines whose model is busy or missing still contribute. After each autopilot round, every machine with a free slot at its
share (`use_pct`, someone at its keyboard, its quiet hours) takes the next due job of `work_queue.jobs`. Each machine takes
one queue job at a time, and the most important project goes first (`projects.<p>.priority`).

```yaml
work_queue:
  jobs:
    - {name: map-static, project: Historic City Map Maker, repo: map, run: 'PIPELINE_STAGE=static bash scripts/ci/in-container.sh',
       heavy: true, every: commit, share: [node_modules]}
    - {name: tutti-check, project: Tutti, repo: tutti, run: bash scripts/check.sh, where: here}
    - {name: map-coverage, repo: map, run: 'PIPELINE_STAGE=tests bash scripts/ci/in-container.sh', heavy: true, every: 12h,
       findings: {pattern: '^(?P<file>\S+)\s*\|\s*(?P<pct>[\d.]+) ?%.*FAIL', title: 'Raise the coverage of {file} ({pct} %)'}}
```

| Key | Meaning |
| --- | --- |
| `repo` | An `autopilot.repos` name or a folder. The job runs in its own detached worktree at the repo's `ref` (default `HEAD`), with `share` folders hard-linked from the main checkout. |
| `where` | `route` (default) uses any free machine. `here` runs on this computer with no slot, for light checks; a `heavy` one waits while this computer is in its limited-usage window or quiet hours. A machine name pins the job to that machine. |
| `heavy` | The job goes to the free machine with the most cores. |
| `every` | `commit` (default) runs once on each new commit; a period (`6h`, `30m`, `1d`) runs again after that long. |
| `findings` | A regex with a `file` group: each match becomes a backlog task, such as the coverage gate's FAIL rows. |

Flaky-test hunting:

- A failed job runs once more on the same commit.
- If it passes the second time, it leaves a "Flaky" backlog task.
- If it fails again, it leaves a "fails on" task.

`tutti workq` shows the jobs and the latest events; `tutti workq tick` runs one round now. The RoRadio device shards stay
with Keep busy (docs/DISPATCH.md).

## Someone at the keyboard

The probe reads, on each kind of machine:

- **Linux:** the graphical sessions' logind idle hint. A desktop that never sets it, with `IdleSinceHint` 0 like the NUC's
  x11, counts as not known, not as in use.
- **A WSL machine:** the Windows console's own last input and lock state, written by Tutti's presence helper in that
  session (a locked screen counts as away); `query user` only while the helper starts, since it says `none` for a console.
- **This computer:** `xprintidle`, else the same logind hint.

Each machine's share uses its own reading.

## Limited hours

An admin can give any machine a daily window when Tutti uses at most `pct` % of it, for example a NUC that also hosts Plex in
the evening. Set it with `tutti machine limit nuc 18-01 60` (`off` removes it; `0` means not used at all then, as a
device's blocked hours: no new work from 30 min before, running work handed over at the start), the machine card's **Limited hours**, or
Settings (`machines.<m>.limit: {window: "18-01", pct: 60, days: [mon, ...]}`). It is stored in the config.

- **The window:** its hours are in Tutti's time zone (`time.zone`). A window past midnight belongs to the day it starts on. Without `days` it runs every day. When a window is
  set without a share, `pct` defaults to 60 (1 to 100; to stop Tutti there, pause the machine).
- **Inside it:** the machine's share is capped at `pct`, which means fewer slots, fewer model threads and a lower adaptive
  limit. Its work-queue jobs marked `heavy` go elsewhere. Running jobs finish, but none is replaced while the machine is
  over the cap.
- **On a move:** `main: true` marks the main machine's own window, and `tutti move` carries it to the new main machine.
- **Default:** a new household has no window.
