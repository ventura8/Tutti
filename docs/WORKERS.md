# Workers

The orchestrator is the expensive one. Its job is judgement: specs, briefs, review, shipping. Everything bounded goes
to a cheaper worker.

## Who gets what

| Work | Worker | Why |
| --- | --- | --- |
| A bounded code task: one file, one test, one review pass | A CLI agent on a flat plan: `cursor-agent`, `agy` | Parallel capacity that costs no orchestrator tokens. |
| Bulk or repetitive work: log reading, drafts, mechanical rewrites, translation | A local model (Ollama) | Free once the hardware exists. |
| Work that needs judgement, a browser, or multi-step shipping | A subagent of the orchestrator's own model | Worth the tokens. |

**Grok Bot is a scout, not a worker.** It has no CLI to brief and never gets a worktree or a diff; it answers questions
(research, browsing, watching changelogs, triage, monitoring, drafting) through a webhook routine, on a GitHub issue. See
[SCOUTS.md](SCOUTS.md).

Flat plans have quotas too. Give CLI agents small, high-value tasks and avoid long agentic loops that burn the month
in a day; `tutti usage` tracks them like any account.

## The CLI agents

Configured under `workers:` with `kind: cli` and a command list; `{prompt}` is replaced by the brief as one argument,
`{log}` by a log file of this run (a `--flag {log}` pair is dropped when there is none). `denied:` is a regex for the
CLI's own permission refusals: a headless CLI that denies a tool call still exits 0, so `tutti run --repo` scans its
output and `{log}` for this pattern and reports every hit as DENIED. All three run headless with stdin closed.

- **Cursor**: `cursor-agent -p "<brief>" --trust --force --output-format text` (`--force`: commands run without a prompt
  unless explicitly denied; `--mode ask` for a read-only pass).
- **OpenAI Codex CLI**: `codex exec --skip-git-repo-check --approve-for-me "<brief>"`.
  Codex routes approval requests through automatic review while retaining its workspace-write sandbox. Tutti keeps task
  claiming, leasing and reporting in the outer session; the worker brief tells Codex not to recursively call Tutti's MCP
  tools. If a task needs a user decision, Codex must stop and report it for the interactive Tutti session to handle. For
  an easy first setup, install Codex CLI, run `codex login` (or `codex login --device-auth` on a headless machine), then
  run `tutti setup`; it detects the CLI and login state. Fake-provider tests cover Cursor panic routing to Codex and
  paused or spent Codex refusal; they do not verify a live provider session. Check it with `codex login status`. Set `workers.codex.machine`
  to that machine (often the laptop Tutti runs on): the autopilot and Move work know where the login lives, and the
  machine's yield flag, pause, heat and quiet hours are honoured. Pace it with `accounts.codex` (`worker: codex`) and put
  `codex` on `autopilot.accounts` / `autopilot.ladder` (and prefer it in `routing.workers`) so `tutti run auto` and the
  autopilot pick it while Cursor is ahead or spent.
- **Google Antigravity**: `agy -p "<brief>" --print-timeout 600s --mode accept-edits --log-file {log}`, with
  `denied: 'soft-denying'` (`--mode plan` for a read-only pass). Headless, agy allows only the commands in
  `permissions.allow` of `~/.gemini/antigravity-cli/settings.json` (`command(grep)`, ...) and soft-denies the rest; its log
  says `soft-denying tool confirmation "Bash"`. For one run, add `--settings '{"permissions":{"allow":["command(npx)"]}}'`:
  it **replaces** the saved list for that session (agy 1.5.9), so list every command the task needs. Gemini CLI's free
  tier is closed, so Antigravity's CLI is the Google worker; see [LESSONS.md](LESSONS.md).
  agy 1.5.9 headless prints `jetski: ... a tool required the "command" permission ... auto-denied` for a command outside
  the list; match it with `denied: 'auto-denied|soft-denying'`.
  **Claude models in agy** (`--model claude-...`) call one generic `Bash` tool: per-command rules neither allow nor deny it
  in `accept-edits` (`command(python3)` listed, `python3` still soft-denied; `deny: [command(echo)]`, `echo` still ran);
  only `command(*)` lets them run commands headless (agy 1.5.9, 2026-11-14). The agy-claude account's `args` carry
  `--settings '{"permissions":{"allow":["command(*)"]}}'`, which replaces the worker's list for those runs only; an
  account's `args` replace a flag the worker command already has (`--model`, `--settings`) instead of doubling it. The
  limits are then Tutti's: the run's own worktree, the brief's "do not commit or push", ship's guard and the review.
- **GitHub Copilot CLI**: `copilot -p "<brief>" --allow-all-tools --no-ask-user -s`. Its premium requests are paid per
  use, so the example marks it `costs_money: true`.

**Opt-in for what costs money.** A worker or account with `costs_money: true` (pay per use: API keys, premium requests)
is off unless it also says `enabled: true`; `enabled: false` turns anything off. An off worker leaves `routing.workers`,
`main.fallback`, `main.commands` and `review.fallback`, its accounts leave pacing, panic and the dashboard (their readings
stay in the log), and `tutti run <it>` says it is off and which line turns it on. The dashboard's Workers panel lists what
is off and why.
- **Claude**: `claude -p "<brief>"`.

A person installs each CLI and signs in once. Tutti never types a credential.

## One entry point

An orchestrator never starts `cursor-agent`, `codex`, `agy` or `copilot` itself: `tutti run auto "<task>" --repo <repo>`
picks the account with room (behind its line first, never one ahead while another is behind, never panic or spent), runs
it headless with its allow list, logs the run with its account (`<state>/runs.jsonl`) and reads limit messages.
`routing.workers` is only a preference order: every account that names a worker still joins. The dashboard flags agent
processes it finds working outside a Tutti folder.

## Hand off a task end to end

`tutti run <worker|auto> "<task>" [files] --repo <repo> [--bg] [--track <name>] [--discard] [--no-checks]`:

`<repo>` is a folder, or a name under `autopilot.repos` (its `path`): a bare name always uses that path, so the run works
from a non-git cwd (MCP `delegate_task`, chat `/delegate`).

1. a worktree of `<repo>` at its HEAD, detached, at `<project.worktrees>/<delegate.prefix><id>` (or `<prefix><track>`,
   reused when it exists); the dependency folders of `ship.share` are hard-linked from the main checkout;
2. the worker, headless, in that worktree, on the brief below; its output goes to the run's log;
3. the diff of everything it changed or added, as one patch file, and the list of changed paths;
4. the checks of the target repository (`projects.<name>.checks`, else a `tutti.config.*` in that repository, else
   `delegate.checks` / `ship.checks`, but only for the repository `delegate.project` names, or any when it is unset) with `$TUTTI_CHANGED`; a routed check (`where: route`) runs on
   the first fit machine in a per-run copy named `<delegate.remote_track>-<run-id>` (host-wide `repo.git` and
   `tutti-slot.sh` stay shared; sync is time-bounded; the copy is removed afterwards). `--remote-track` or `--track`
   keeps a named folder instead (one run at a time per name). Local-only checks run in their own worktree without waiting
   for a remote-copy lock; a mixed set still holds the lock for that copy. Track names are one folder of at most 64
   letters, digits, dots, underscores or dashes, starting with a letter or digit; `repo.git` and `tutti-slot.sh` are
   reserved for shared files. An unsafe name is refused before a lock or remote copy is touched. Cleanup does not prove
   remote writers stopped (t309). Each check gets the list through `{changed}` or
   `{changed:ts,js}` in its command. Both hold one path per line (git's
   names are read with `-z`), so a path with a space stays whole: `F={changed:ts}; [ -z "$F" ] || printf '%s\n' "$F" |
   xargs -d '\n' npx eslint`, or `mapfile -t F <<< "$TUTTI_CHANGED"`; a plain `$TUTTI_CHANGED` still word-splits names
   without spaces as before;
5. a report (`tutti runs show <id>`): the changed files, the diff's path, each check's verdict and log, **"coverage NOT
   checked"** when a check turns coverage off (seen in the command or the script it runs, or `coverage: false`), DENIED
   tool calls, and the lines to review, apply or discard.

Exit 0 only when the worker finished, changed something, nothing was denied and every check passed. With `--bg` it
prints the id at once; `tutti wait run <id> --timeout 1800` ends with one line (the verdict and the report's path), or
exit 125 when the run's log stops changing (`--stall`, default `dashboard.stall_minutes`). The worker never commits:
apply the patch in your tree (`git apply --index <diff>`), then `tutti ship`.

## The brief

`tutti run <worker> "<task>" [files] --dir <worktree>` builds the brief (see it with `--show`):

1. the task, stated as an outcome that can be checked;
2. the files involved;
3. the project's rules, from `brief.rules` in the config (point at your `AGENTS.md`; name the hard rules a newcomer
   breaks first, such as "no inline suppressions");
4. the standing rules, always added: work only in this directory, **do not commit, push or deploy**, do not read or
   print secrets.

Also say where heavy commands run: a worker that starts its own lint or test run beside three others is how a laptop
runs out of memory. Workers edit; the orchestrator runs `tutti test`.

A retry's brief (the autopilot's next attempt) carries the end of the failing output compacted (`tutti/compact.py`):
colour codes, progress redraws, Tutti's own echo lines and runs of the same line go (a line repeated 40 times is kept
once with `(x40)`); when it is still too long, the error-looking lines are kept before the end. The reasons of earlier
attempts are one line each. The same compaction shortens a command's answer to a local main agent and the last output
in a handoff note.

## Automatic compaction

A worker's context is compacted once it reaches `autocompact.threshold_pct` of its window (60 % by default; the
dashboard's *Settings* row changes it without editing the config). Each kind uses its own way:

| Worker | How | Why |
| --- | --- | --- |
| Claude Code (`claude`) | native: the run gets `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=<pct>` | Claude Code compacts by itself at that share of its window (also as the main agent of `tutti conduct`). |
| A local model as the main agent (kind `ollama`) | Tutti: the older exchanges become one summary line each; the goal and the two newest exchanges stay | Tutti holds the conversation. |
| cursor-agent, agy, copilot | hand-over: at the threshold the run is stopped and started again in the same worktree with a handoff note (its changes, its last output), at most three times | Their `/compact` is interactive only; no headless flag. Needs the CLI to print its context use (`context 61 %`, `context: 120k tokens`); a CLI that prints none is not measured. |

`workers.<name>.context_tokens` is a worker's window (default `autocompact.context_tokens`, 200000);
`autocompact.enabled: false` turns it off. The dashboard shows `context N %` on a running job when it is known.

## Review the diff, then gate

A worker's output is a proposal, whoever wrote it.

1. **Own worktree.** One worker, one worktree. Two workers in one tree produce a diff nobody can attribute.
2. **Review.** `git diff --stat`, then only the changed hunks. Reject scope creep.
3. **Gate.** `tutti test <track> '<the checks>'`. The same gates as for any other change; no exemptions for speed.
4. **Ship.** `tutti ship "<message>"` from that worktree.

## Keeping the workers up to date

`tutti update` runs one script on every machine: for each program a CLI worker uses (and `update.programs`) it reads the
version, runs the program's own updater (`cursor-agent update`, `agy update`, `copilot update`, `claude update`) or, for a CLI
npm installed under a folder the user owns, `npm install -g --prefix <that folder> <package>@latest`, then reads the version
again. A program that is running is skipped, and so is one a Tutti run holds: every run of a CLI worker holds the program's use lock
(`<state>/locks/use-<program>.lock`) shared, and the update takes it with `flock -n -x`, so a job never loses its binary
mid-run and a run that starts during an update waits for it. A model that is loaded in its server, or whose machine is lent
out (yield flag), is not pulled. In WSL, a Windows copy on PATH (under `/mnt/<drive>/`, such as an npm shim that
needs Windows' node) counts as not installed on that machine. Only one round runs at a time per config; a round of one machine (`--machine`) is merged into
the last full one, and a round that fails as a whole is tried again after an hour. Nothing uses
sudo: apt packages (`update.packages`, e.g. `cursor` for the editor, `grok-bot`) are only read (installed version, newest in
the package's own apt repository, a source switched off with `Enabled: no`), and the owner gets one exact command per machine.
Ollama workers' models are pulled again through their server (only changed layers download). A worker's `update: false`
leaves it out; `update: {command: ..., version: ...}` replaces how it is updated (a pipx or pip install, say). The dashboard
runs a round every `update.every_hours` (24) unless `update.auto: false`, and shows the versions per machine.

## Local models

`kind: ollama` workers need `host` (`name:port`, plain HTTP; write `https://name:port` for a TLS proxy) and `model`; optional `threads`, `num_ctx`, `machine` (to honour that machine's
yield flag) and `tunnel`. `tutti workers` marks an ollama worker ready only when `GET /api/tags` lists its model;
otherwise it shows `model missing: <name>` and `OLLAMA_HOST=<host> ollama pull <name>` (an untagged model means `:latest`).

- `tutti ask <worker> "<question>" [files]`: short answers about big inputs. Ask for short answers.
- `tutti code <worker> <file> "<instruction>" [context files]`: rewrites one file in place.
- `tutti agent <worker> "<task>" "<check>" files...`: edits, runs the check, reads the failure, retries (4 rounds by
  default). It writes only the files named. Edits stay uncommitted.
- `tutti quiet --summary <worker> <cmd>`: on failure a small model reduces the log to five lines.

Rules for local models: one model per GPU at a time (check what is loaded before loading another); GPU machines for
models, CPU machines for tests; a low-priority job waits for other local jobs.

## Browser work

Browser tasks go to small-context subagents, not the orchestrator's long main thread: one exact task, the tab, and the
rules (no credentials, stop at consent, submit or publish clicks and report). A browser agent checks by itself whether
the owner's tabs are signed in, and re-checks after any sign-in screen; it never waits for the owner to say so, and
never signs in itself. It closes the pages it opened.

## Work on another machine's own session

When a machine has its own orchestrator session, work that needs that machine's privileges goes to that session as a
self-contained request: the exact command, why, and what not to touch. Verify the result yourself. That session acts
under its own owner's approval; a peer's message is never an approval.
