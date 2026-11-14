# Lessons

Each entry: what happened, why, and the rule it left behind. Add a lesson in the same change that fixes its cause.

## A wait loop that matched its own command line

**What happened.** Agents waited for a job with `while pgrep -f '<pattern>'; do sleep ...; done`. Three of them
stalled for hours after the job had ended.

**Why.** `pgrep -f` matches full command lines, and the shell running the loop has the pattern in its own command
line. The loop found itself, forever. `ps | grep` fails the same way.

**Rule.** Wait on a PID (`wait $pid`, `tail --pid=$pid -f /dev/null`), a done-file or a log line, **always under a
timeout**. `tutti wait pid|file|grep ... --timeout <s>` does exactly this and exits 124 when the time is up.

## `pkill -f` from a shell that contains the pattern

**What happened.** `pkill -f '<pattern>'`, typed in a shell whose own command line held the pattern, killed that
shell too.

**Why.** Same matching rule: the caller's command line holds the pattern.

**Rule.** Stop by recorded PID or process group (`pkill -g "$(cat pidfile)"`), never by pattern. `scripts/tunnel.sh`
keeps a pid file for this reason.

## A ship script that committed in the wrong worktree

**What happened.** A ship script fell back to a hard-coded main checkout when it could not tell where it was, so a run
started from the wrong folder committed in a tree the caller did not mean.

**Why.** A default tree is a guess, and a wrong guess here is a wrong commit on a shared branch.

**Rule.** Ship only the tree you stand in; refuse outside a git work tree; print the tree and branch first; let the
caller assert the track. `tutti ship` has no default tree, `--track <t>` refuses any other worktree before anything is
written, and a protected branch needs `--allow-protected`.

## A classifier denied "jump the queue"

**What happened.** An agent tried to get its run ahead of the slot queue. The safety classifier of its harness denied
the action.

**Why.** The queue exists to keep shared machines alive, and going around it is what the denial protects against.
Trying the same thing in another form would be working around a control, not solving the task.

**Rule.** A denial is an answer. Do not rephrase, split or reroute the denied action. Wait in the queue, or ask the
owner for more slots (a config change, visible to everyone). Briefs say: never work around a denial; stop and report.

## Gemini CLI's free tier closed

**What happened.** The Google worker stopped with an ineligible-tier error that said to migrate to Antigravity.

**Why.** The vendor closed the free tier of the old CLI.

**Rule.** Use Antigravity's `agy` as the Google worker. More generally: a worker is a config entry, not code, so
replacing one is a one-line change; `tutti workers` shows what is ready today.

## Peer sessions need their own owner's approval

**What happened.** One session asked a peer session, on another machine, to install something there. The request was
correct and self-contained, and still waited: the peer runs in its own permission mode.

**Why.** Approval does not travel between agents. A message from another agent is a request; only that session's
owner, or its own permission system, can approve an action there.

**Rule.** Send a peer the exact command, the reason and what not to touch; expect it to ask its owner; verify the
result yourself afterwards. Never present a peer's message as the owner's consent, and never accept one as yours.

## Uncapped test containers took the desktop down

**What happened.** Several agents ran lint and tests at once on the orchestrator's computer. Memory and swap filled,
the kernel killed the browser and the session.

**Why.** No queue, no memory cap, parallel linters with many workers each.

**Rule.** Heavy runs go to other machines through `tutti test`. Locally: one at a time, capped, first to die
(`--oom-score-adj`), with `tutti guard` pausing containers when memory runs low. Briefs tell workers not to run heavy
commands themselves.

## WSL bash does not see Windows process environment (2026-11-14)

**What happened.** `tutti ship` checks that read `$TUTTI_CHANGED` wrote an empty file on Windows even though the commit
listed the new paths.

**Why.** On many Windows setups `bash` is WSL. `subprocess.run(..., env=...)` sets the Windows process environment;
WSL does not inherit it unless variables are listed in `WSLENV`. Git Bash does inherit it, so the same code worked on
one machine and not another.

**Rule.** When Tutti starts bash for a command that needs `TUTTI_*` (or any other variable), put the assignments on the
bash command line as well as in `env=`. Do not assume the Windows process environment crosses into WSL.

## Antigravity headless refuses shell commands without per-command rules (2026-11-14)
`agy -p` in print mode soft-denies any tool needing "command" permission unless `permissions.allow` in `~/.gemini/antigravity-cli/settings.json` lists it (`command(grep)` …). Its `cli.log` shows "soft-denying tool confirmation \"Bash\" at step N" without the command, so `tutti doctor` must find it another way (re-run with logging, or start with a broader read-only rule set). Claude Code likewise needs `Bash(agy *)` / `Bash(cursor-agent *)` allow rules before it can start these CLIs; the user must write them (an agent may not edit its own permissions).

## Windows bash.exe mangles the -c line (2026-11-14)

**What happened.** Local runs on Windows broke again in QA round 2: the command handed to WSL `bash.exe -c` arrived
split or with its quotes stripped, and arguments after `-c` were lost.

**Why.** Windows re-quotes the command line on its way to WSL; quotes, `&&`, `$(` and braces do not survive it reliably,
and WSL does not pass positional arguments after `-c` through as bash expects.

**Rule.** A command bound for Windows `bash.exe` avoids quotes, `&&`, `$(` and braces in the `-c` line and never relies
on arguments after `-c`. Send the script base64-encoded: `bash -c source <(echo <b64> | base64 -d)`.

## A CLI's rolling-window reset is a few seconds after ours (2026-11-14)

`agy /usage` reports a window nobody has started as resetting one full window after its own clock. Recorded as is, the
window started just after Tutti's `now`, so the reading fell into the window before and the account showed "late" with
no reading. Read and record with one `now`, and clamp a reset more than one window ahead to `now + window`
(`readers.not_started`).

## Ship committed a half-resolved merge (2026-11-14)

Twice in one day a ship ran while a rebase was stopped on a conflict, and committed files with `<<<<<<<` markers. Ship
now checks `git rev-parse --git-path` for `rebase-merge`, `rebase-apply`, `MERGE_HEAD`, `CHERRY_PICK_HEAD` and
`REVERT_HEAD`, and `git grep --untracked` for opening or closing markers, before the commit and again before the push.

## Antigravity's `--settings` replaces the allow list (2026-11-14)

`agy -p ... --settings '{"permissions":{"allow":["command(node)"]}}'` ran with `Allow:[command(node)]` only: the saved
list (`grep`, `git`, ...) was gone for that session (agy 1.5.9, its log's "CLI settings initialized" line). A per-run
allow list must name every command the task needs. agy 1.5.9 also ran `node -e` headless without any rule, where 1.3.3
soft-denied commands, so denials are detected from the run (`denied:` and `{log}`), not assumed from the version.

## Two runs in one second shared a worktree (2026-11-14)

Run ids were `<time>-<pid>`; two delegated runs started by one process within a second got the same id, so the second
reused the first's worktree and its diff. A unit test caught it. Ids now get `-<n>` when the log already exists.

## A routed check cannot read $TUTTI_CHANGED (2026-11-14)

A check that runs on another machine gets only its command line: the environment of `tutti ship` stays here. The list
of changed files travels in the command (`{changed}`, `{changed:ts,js}`). Inside one bash line, `${F:+cmd &&}` does not
make `&&` an operator: test with `[ -z "$F" ] || { ...; }`.

## A reviewer's acknowledgement is not a review (2026-11-14)

The first real `tutti pr review` reported "coderabbit reviewed" while CodeRabbit had only answered "Action not completed:
Review rate limited", and it edits its summary comment in place (its `created_at` stays old, `updated_at` moves) to say
"Review limit reached". Only a pull request review counts as a review; comments are read for limit messages, at the later
of their created and updated times.

## PowerShell flattens a one-pair list (2026-11-14)

The Windows look-around script listed projects as `@(@('RoRadio', 'C:\\...'))`; PowerShell flattened the single pair into two
strings, so `$x[0]` was the letter `R` and the dashboard showed projects named "R" and "C". A hashtable (`@{'RoRadio'='C:\\...'}`)
keeps one entry whole. The Windows side of a WSL machine is read over the machine's plain ssh (the Windows host): its
WSL had no interop (`Exec format error` for every `.exe`), and WSL's `/proc` never shows Windows processes.

## A fixed band hid "ahead" early in a window (2026-11-14)

`on pace` meant within 5 points of the line. On the second day of a week the line is about 6 %, so 10 % used (two thirds
over) read as `on pace`. The band is now a quarter of the line, 1 to 5 points, and each row prints the math.

## A headless agent denies a compound shell line (2026-11-14)

The first agy conductor runs passed; a later one ended after its CLI auto-denied a `Bash` call: the model had joined
commands (`cd`, `&&`, `>>`) that no `command(...)` rule allows, and Claude models in agy use a `Bash` tool the list does
not cover at all. The turn had exited 0, so it would have read as done: a turn now counts as done only with a `DONE:` line
and no denied tool call, and the instructions ask for one command per line and edits through the agent's own edit tool.
