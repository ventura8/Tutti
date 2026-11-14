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
