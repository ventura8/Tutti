# Workers

The orchestrator is the expensive one. Its job is judgement: specs, briefs, review, shipping. Everything bounded goes
to a cheaper worker.

## Who gets what

| Work | Worker | Why |
| --- | --- | --- |
| A bounded code task: one file, one test, one review pass | A CLI agent on a flat plan: `cursor-agent`, `agy` | Parallel capacity that costs no orchestrator tokens. |
| Bulk or repetitive work: log reading, drafts, mechanical rewrites, translation | A local model (Ollama) | Free once the hardware exists. |
| Work that needs judgement, a browser, or multi-step shipping | A subagent of the orchestrator's own model | Worth the tokens. |

Flat plans have quotas too. Give CLI agents small, high-value tasks and avoid long agentic loops that burn the month
in a day; `tutti usage` tracks them like any account.

## The CLI agents

Configured under `workers:` with `kind: cli` and a command list; `{prompt}` is replaced by the brief as one argument,
`{log}` by a log file of this run (a `--flag {log}` pair is dropped when there is none). `denied:` is a regex for the
CLI's own permission refusals: a headless CLI that denies a tool call still exits 0, so `tutti run --repo` scans its
output and `{log}` for this pattern and reports every hit as DENIED. All three run headless with stdin closed.

- **Cursor**: `cursor-agent -p "<brief>" --trust --force --output-format text` (`--force`: commands run without a prompt
  unless explicitly denied; `--mode ask` for a read-only pass).
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

An orchestrator never starts `cursor-agent`, `agy` or `copilot` itself: `tutti run auto "<task>" --repo <repo>` picks the
account with room (behind its line first, never one ahead while another is behind, never panic or spent), runs it
headless with its allow list, logs the run with its account (`<state>/runs.jsonl`) and reads limit messages. The
dashboard flags agent processes it finds working outside a Tutti folder.

## Hand off a task end to end

`tutti run <worker|auto> "<task>" [files] --repo <repo> [--bg] [--track <name>] [--discard] [--no-checks]`:

1. a worktree of `<repo>` at its HEAD, detached, at `<project.worktrees>/<delegate.prefix><id>` (or `<prefix><track>`,
   reused when it exists); the dependency folders of `ship.share` are hard-linked from the main checkout;
2. the worker, headless, in that worktree, on the brief below; its output goes to the run's log;
3. the diff of everything it changed or added, as one patch file, and the list of changed paths;
4. the checks (`delegate.checks`, default `ship.checks`) with `$TUTTI_CHANGED`; a routed check (`where: route`) runs on
   the first fit machine in a copy named `delegate.remote_track` (one run at a time per copy; `--track` gives a run its
   own) and gets the list through `{changed}` or `{changed:ts,js}` in its command. Both hold one path per line (git's
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

## Review the diff, then gate

A worker's output is a proposal, whoever wrote it.

1. **Own worktree.** One worker, one worktree. Two workers in one tree produce a diff nobody can attribute.
2. **Review.** `git diff --stat`, then only the changed hunks. Reject scope creep.
3. **Gate.** `tutti test <track> '<the checks>'`. The same gates as for any other change; no exemptions for speed.
4. **Ship.** `tutti ship "<message>"` from that worktree.

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
