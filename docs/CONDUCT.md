# Another agent as the main agent

`tutti conduct` runs the goal with a main (conductor) agent other than Claude Code: Cursor (`cursor-agent`),
Antigravity (`agy`, Claude or Gemini models), the GitHub Copilot CLI, Claude Code itself, or a local model through
Ollama. Use it when the usual orchestrator's quota is in panic, or to give an account with room the judgement work.

```bash
tutti conduct --main agy "Add the missing unit test for the reader and get it green"
tutti conduct --main laptop --fallback agy,cursor --dir ~/Projects/app-wt/fix "Find why the build is slow"
tutti conduct --show --main cursor "<goal>"     # print the instruction file and stop
tutti main                                      # the main agent now, who has room, the running or last run
tutti main cursor                               # pin it (as the dashboard / Chat selector does)
tutti main auto                                 # Tutti picks: preferred while it has room, else first fallback
```

## What the main agent gets

- **An instruction file** (`<project.state>/conduct/<run>/INSTRUCTIONS-<agent>.md`, also given inline in its prompt):
  the goal, the project's rules (`AGENTS.md` and `CLAUDE.md` of `--dir`, plus `brief.rules`), Tutti's commands, the
  safety limits and the handoff rules.
- **Tutti's commands as plain shell commands.** A `tutti` on its PATH runs this Tutti with this config:
  `tutti run auto "<task>" --repo <repo> --bg`, `tutti wait run <id>`, `tutti runs show <id>`, `tutti test`,
  `tutti files`, `tutti usage`, `tutti workers`, `tutti machines`, `tutti handoff`, `tutti ship "<msg>" --dry-run`.
  Only the commands this Tutti has are listed.
- **Permission to call them.** A headless agent with an allow list in a JSON settings argument (agy's `--settings
  '{"permissions":{"allow":[...]}}'`) gets `command(tutti)` added by itself. For Claude Code give the command in
  `main.commands` (for example `[claude, -p, '{prompt}', --permission-mode, acceptEdits, --allowedTools, 'Bash(tutti:*)']`).
  Cursor (`--force`) and Copilot (`--allow-all-tools`) run commands already.
- **A local model** cannot run a shell. Tutti runs a loop: the model answers one `RUN: tutti <command>` line, Tutti runs
  it (only its own commands, never a shell; `ship` only with `--dry-run`) and returns the output, until `DONE:` or
  `BLOCKED:` (at most `main.rounds` answers).

## Safety limits

Written into every instruction file, and enforced where Tutti can:

- No push, tag, merge, publish or deploy. While a main agent conducts (`TUTTI_CONDUCT` is set), `tutti ship` refuses
  anything but `--dry-run`, and `--allow-protected`.
- Never commit to or push a protected branch (`ship.protected`).
- No credentials: no `.env`, tokens, keys, passwords or card numbers; no sign-in, no new account.
- Ask-first actions (push, tag, merge, deploy, payments, consents, cloud and account settings, deleting data, plus
  `main.ask_first`) need a human: the agent writes `tutti handoff next "NEEDS HUMAN: ..."` and ends with `BLOCKED`.

## Fallback and the handoff note

The order is `--main` (else the dashboard's choice, else `main.agent`), then `--fallback` (else `main.fallback`). An agent
whose accounts are all in panic or spent (the latest readings, `docs/USAGE-PACING.md`) is skipped. While one runs, Tutti
checks every `main.check_seconds`:

- **panic**: every account the agent spends (`accounts.<name>.worker`, or the account of its name) is in panic or spent;
- **switch**: the owner chose another main agent (the dashboard's Switch, `tutti main <agent>`);
- **limit**: its output ended with a usage-limit message (logged as spent, as for any worker).

Then it stops the agent (its whole process group) and writes `handoff-<n>.md`: the goal, who stopped and why, what is
done (from `tutti handoff done`), the tree's changes (`git status --short`), what comes next (from `tutti handoff next`)
and its last output. The next agent starts with the instructions and that note. On a switch the owner's choice goes
first, even if it ran before. When no agent has room the run stops with exit 3.

The run's record is `<project.state>/conduct/<run>/run.json` (and `conduct/current.json`), which the dashboard's Main agent
panel shows.

## Automatic MAIN AGENT ROLE hand-off

Outside a `tutti conduct` run, each dashboard round also watches the current main agent (`main.agent`, or the owner's
switch). **Preferred** main (`main.preferred`, the owner's lasting choice; default: the file's `main.agent` when that is
a worker, else the first of `main.fallback`, else `claude`) stays separate from **current** main. An automatic hand-off
changes only the current agent.

`main.agent: auto` (or `tutti main auto` / the Chat selector's **Auto**) lets Tutti pick and keep the current main itself:
the preferred agent while it has room (not panic or spent), else the first worker in `main.fallback` that has room.
Every switch — including switching **back** to preferred after its reset (a fresh reading below `panic_at`, not the
synthetic `after_reset` 0 %) — uses the same hand-off (`deliver_role_note`, `connect_role`).

Paused or disabled accounts remain linked to their worker, so they do not make that worker appear unmetered. When no
online main has room, Tutti considers the configured local models by the fleet's measured parameter count, falling back to
the size in the model name, skipping machines reported offline,
paused, hot or busy. When an online choice recovers, auto mode returns to the preferred online order. A missing machine
probe is treated as not yet available; if model sizes are unknown, the configured order breaks the tie.

When every account the current main spends is in **panic** or **spent**, Tutti hands the **MAIN AGENT ROLE** to the next
agent with room (`auto`: preferred then first fallback; otherwise the next in `main.fallback`):

1. The current choice is written (`user.json`'s `main_agent` only on an owner pin; automatic hand-offs keep `conduct/main.json`
   and leave `preferred_main` alone, recording it once if empty). With `agent: auto`, the mode stays `auto`.
2. `tutti connect` runs for that agent's kind (Cursor, Antigravity, …; nothing to connect for a local model).
3. A note goes to the owner's Chat under **Main agent hand-off** (the first admin's partition when sign-in is on, else
   `local`): who left, why (panic or spent), and who took over — or that preferred was restored after its limit reset.

The same panic episode is noted once (keyed on from/to and the panic start, not the formatted usage text). When the
current main is healthy again with no restore pending, the saved record is cleared so a later panic can note again. When
no fallback has room, the note says so and the role stays put until one does. Bounded autopilot work still moves to other
accounts while the main agent panics (docs/AUTOPILOT.md).

An owner switch (`tutti main <agent>`, the dashboard Switch, the Chat header selector, `tutti user main-agent`) sets both
preferred and current and leaves auto. `tutti main auto` turns auto on and keeps preferred.

## The config

```yaml
main:
  agent: auto                         # a worker, or auto: preferred while it has room, else first main.fallback
  preferred: claude                   # the owner's lasting choice (empty: file's agent, else first fallback, else claude)
  fallback: [agy, cursor, laptop]     # who takes over, in order (also the auto pick order after preferred)
  commands:                           # optional: the command of an agent when it conducts (default: its worker command)
    claude: [claude, -p, '{prompt}', --permission-mode, acceptEdits, --allowedTools, 'Bash(tutti:*)']
  ask_first: []                       # extra ask-first actions for the instruction file
  check_seconds: 30                   # how often a running conductor checks the readings and the switch
  rounds: 12                          # a local model's answers before it is stopped
  command_timeout: 3600               # seconds one `tutti ...` of a local model may take
```

## What each agent did in the first real runs (2026-11-14)

| Agent | As main agent |
| --- | --- |
| Antigravity (`agy`) | Works end to end on a Gemini model: edited files, ran `tutti usage`, `tutti workers` and `tutti handoff`, ended with `DONE:`. A switch stopped it mid-run (a `tutti wait` in progress) and the handoff note carried its done and next steps. Two limits seen: a compound shell line (`cd`, pipes, `>>`) is auto-denied headless, so the instructions ask for one command per line; its Claude models call a `Bash` tool that the `command(...)` allow list does not cover, so pin a Gemini model in `main.commands.agy` (pinning also turns off the account's `args`). A denied tool call ends the turn as "ended", never "done". |
| Cursor (`cursor-agent --force`) | Works end to end: edited a file, ran `tutti workers` and `tutti handoff`, ended with `DONE:`. |
| GitHub Copilot CLI (`copilot --allow-all-tools`) | Works end to end, the same small goal, ended with `DONE:`. |
| Local model (Ollama, a 9B model) | Works through Tutti's loop: continued from agy's handoff note, ran `tutti wait`, `tutti handoff` and `tutti usage`, ended with `DONE:`. It may not call `tutti test`, `tutti agent`, `tutti files` (other than `list`) or `tutti runs discard`: they run shell lines it would write, or delete. A machine whose yield flag is up keeps its model out. |
| Claude Code | Not run as main agent here (it is the usual orchestrator); give it `--allowedTools 'Bash(tutti:*)'` in `main.commands.claude`. |
