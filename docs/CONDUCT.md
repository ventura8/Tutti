# Another agent as the main agent

`tutti conduct` runs the goal with a main (conductor) agent other than Claude Code: Cursor (`cursor-agent`),
Antigravity (`agy`, Claude or Gemini models), the GitHub Copilot CLI, Claude Code itself, or a local model through
Ollama. Use it when the usual orchestrator's quota is in panic, or to give an account with room the judgement work.

```bash
tutti conduct --main agy "Add the missing unit test for the reader and get it green"
tutti conduct --main laptop --fallback agy,cursor --dir ~/Projects/app-wt/fix "Find why the build is slow"
tutti conduct --show --main cursor "<goal>"     # print the instruction file and stop
tutti main                                      # the main agent now, who has room, the running or last run
tutti main cursor                               # switch it (as the dashboard's Switch does)
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

## The config

```yaml
main:
  agent: claude                       # the default main agent (a worker)
  fallback: [agy, cursor, laptop]     # who takes over, in order
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
| Antigravity (`agy`) | Works end to end: created files, ran `tutti usage`, `tutti workers` and `tutti handoff`, ended with `DONE:`. A switch stopped it mid-run (`tutti wait` in progress) and the handoff note carried its done and next steps. |
| Local model (Ollama, a 9B model) | Works through the loop: continued from agy's note, ran `tutti wait`, `tutti handoff` and `tutti usage`, ended with `DONE:`. |
| Cursor, Copilot CLI, Claude Code | Same path as agy (a headless CLI with a shell); covered by the unit tests with a stand-in command, not yet run for real as main agent. |
