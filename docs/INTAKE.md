# All work through Tutti

When adding directly to the backlog or through auto-fill, a missing scope or check does not by itself distinguish matching
repository and normalized title. If both additions explicitly give different scopes or different checks, they remain
separate tasks. Auto-fill still avoids re-adding compatible done or dropped tasks. Household shared-task keys are unchanged;
this does not establish semantic deduplication across differently worded tasks (t210). Household moved notices are scoped
to their project and require compatible nonempty scopes before acting; ambiguous queued work waits and candidate closure
is deferred pending reconciliation.

Every AI session in the household (the coordinator's Claude Code, a session on another computer, a person's own agent)
does its work as a Tutti task, so it is scheduled, paced against its account and visible on the dashboard. This page is
how a session does that, how Claude itself becomes a Tutti worker, and how the gaps show.

## One line per session

Tutti's MCP server gives a session every tool it needs. Wire it in once per computer (it then applies to every project
and every new session there):

```bash
tutti connect claude                                   # Claude Code: MCP server plus the Tutti skill (preferred)
claude mcp add --scope user tutti -- tutti mcp         # the same with Claude Code's own command
```

On a computer that is a client of the host (`tutti client`, docs/MOVE.md) the same command works: its `tutti` reaches
the host through the bridge. A session started before Tutti was added sees it after a restart.

Then, in every household repo, once:

```bash
tutti connect --repo <path-to-the-repo>                # adds the "All work goes through Tutti" section to AGENTS.md
```

It writes a marked section into the repo's `AGENTS.md` (or `CLAUDE.md` when there is no `AGENTS.md`), replaces an older
copy of it and leaves the rest of the file as it was. Commit it like any other change.

## What a session does

| Step | MCP tool | CLI |
| --- | --- | --- |
| Find or add the task | `backlog_list`, `backlog_add` | `tutti backlog list`, `tutti backlog add <repo> "<title>"`; set a task with `tutti backlog priority <id> P0` |
| Take it | `backlog_claim` | `tutti backlog claim <id> --who <session>` |
| Record progress | `backlog_update` | `tutti backlog note <id> --note "<text>"` |
| Use a machine or a device | `lease_claim`, `lease_release`, `leases` | `tutti lease claim machine\|device <name> --who <session> --task <id>` |
| Run tests elsewhere | `test_on` | `tutti test`, `tutti dispatch` |
| Report a usage reading | `usage_report` | `tutti usage log <account> <percent>` |
| Ask the owner | `ask_owner` | `tutti needs ask <id> "<question>" --option a --option b` |
| Claude-only work | `claude_task` | `tutti run claude "<task>" --repo <repo>` |
| Finish | `backlog_complete` | `tutti backlog done <id>` |
| See the gaps | `outside_work` | `tutti intake` |

- A **claimed** task belongs to that session: the autopilot does not start it; only that session's `release` gives it
  back to the queue. A parked task is answered first, a candidate accepted first.
- Name the task id (`t123`) in every commit: that is how the intake guard links the commit to the task.
- A question asked with `ask_owner` parks the task in the needs queue (docs/NEEDS.md); the owner's answer brings it back
  to the same session as claimed, with the answer in its notes, never to the autopilot.
- A device lease also claims the device itself (`tutti devices claim`), so its settings stay while the session uses it.
  Another session's live lease is refused with who holds it and until when.

`$TUTTI_SESSION` names the session when `--who` is left out. Changing tools need the `admin` role (docs/CONNECT.md).

## Claude as a worker

`tutti run claude "<task>" --repo <repo>` (and the MCP tool `claude_task`) does not run Claude by hand. It:

1. adds a backlog task (source `claude`, pinned to the claude worker: `pin: worker:claude`);
2. reads the claude account's pace: in panic or spent the task waits in the queue and the autopilot starts it when the
   planner gives the account room; otherwise it starts now;
3. starts `claude -p` headless in the task's own worktree through the autopilot's dispatch (the account's tier and effort
   follow its pace);
4. leaves the rest to the autopilot's rounds: the attempt recorded, a pass committed on `autopilot/<id>-...` and a pull
   request per the repo's review and release policy, a failure retried on the claude account or parked as a question.

The task shows on the dashboard like any other. The repo must be under `autopilot.repos` (by name or folder), otherwise
the run is refused with the fix; `--no-backlog` runs Claude untracked, the old way, on any repo.

## Work outside Tutti

`tutti intake` and the dashboard's **Work outside Tutti** card list, for the last 24 hours:

- commits in every `autopilot.repos` repository (every branch) that no task explains: a commit is linked when it names a
  backlog task id, is an autopilot commit, is reachable only from Tutti branches (`autopilot/`, `tutti/`, `ap-`), or was made while a
  task of that repo was claimed or running (until it was released or closed), or during a lease for an open task. Bots (Dependabot, Renovate, `[bot]`) are
  left out;
- live machine and device leases with no task, or whose task is no longer open;
- repositories whose git could not be read, with the config key to fix.

`tutti intake --strict` exits 1 when anything is outside, for a scheduled check. The dashboard reads git at most every
five minutes.

What the guard cannot see: work on a computer whose repositories are not under `autopilot.repos`, and a device or machine
used without any lease. The fix for both is the same: claim the task and lease what you use.
