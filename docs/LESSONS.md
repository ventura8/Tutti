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
