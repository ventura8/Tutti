# A household: several people, one set of machines

Several people in one home each work with their own main agent (Claude Code, Cursor, Antigravity, Copilot, a local
model) on their own subscriptions, on the same projects or on different ones. The machines (a GPU desktop, an always-on
mini PC, laptops, WSL, Termux phones) and the local models on them belong to the household. Tutti makes them all count:
every person's accounts end near 100 % before their resets, the shared machines are never idle while someone has work,
and nobody waits behind someone else's background autopilot while they are prompting.

A single person changes nothing. **One user is a household of one**: no new key is needed, no file moves, and every
command works as before.

## The model

| Thing | Belongs to | Lives in |
| --- | --- | --- |
| A **user** | one person | their own login (OS account) on their own computer, or on a shared one |
| Their main agent, accounts, readings, pacing (`pacing.schedule`, the mode and away, the learned 7 x 24) | the user | `~/.config/tutti/config.yaml`, `~/.local/state/tutti/` (`usage.jsonl`, `owner.json`, ...) |
| Their prompts, briefs, tasks of their own repos, diffs, run logs | the user | their own state folder; never sent anywhere |
| The household membership and its token | the user | `~/.config/tutti/household.json` (mode 600) |
| **Shared machines** and their local models | the household | each machine's owner's config, joined with `tutti household machine share <m>` |
| **Shared projects**: their backlog and the leases on their tasks | the household | the coordinator's state |
| The **coordinator**: members, machines, leases, the shared backlogs, the per-user totals | the household | one always-on computer's `~/.local/state/tutti/household/` |

A user is a login, not a profile inside one login, on purpose: every agent CLI (`claude`, `cursor-agent`, `agy`,
`copilot`) keeps its sign-in in the home folder, so two people on one login would spend each other's subscriptions.
Each person installs Tutti in their own login (`npm i -g tutti-agents` or `install/install.sh`), signs their agents in
once, and joins.

**One user's accounts never do another user's work.** An autopilot dispatches only to its own user's accounts, from its
own repos or from a shared project that user is a member of. The one exception is opt-in, per account:

```yaml
accounts:
  agy-claude:
    share_spare: true      # default false: room this account would lose at its reset may do the household's shared tasks
```

and then only for the room the planner says would expire unused (the last `share_spare_hours`, default 6, before the
account's target, with nothing of the user's own queued that fits). The work still runs under that user's own CLI sign-in,
on that user's login: no credential, token or reading ever crosses users.

## Coordination: one coordinator, a local fallback

Two designs were weighed.

| | A household coordinator (chosen) | A shared state folder |
| --- | --- | --- |
| What it is | `tutti household serve` (or the dashboard of the computer that ran `household init`) on one always-on computer: a small HTTP service on the LAN | members mount one folder (NFS, Samba, sshfs) and lock files in it |
| Leases | atomic in one process, with expiry and heartbeats | `flock` over NFS or Samba is unreliable; sshfs has no locks at all |
| Privacy | each user sees only what the API answers: aggregates, per-machine use, per-user totals, the shared backlogs | everyone who mounts it can read every file in it |
| Auth | a join code, then a per-user token; every request signed | file permissions across logins and computers: brittle |
| Phones, WSL, Windows | HTTP works everywhere | mounting does not |
| Offline | the client falls back to working alone, safely | a hung mount hangs every Tutti command |

The coordinator is the existing Python standard-library HTTP server pattern, so it adds no dependency. The best home is
the always-on machine (the mini PC): `tutti household init` there, or on any member's computer that is on most of the
time.

```text
   Ana's laptop                     the always-on mini PC                     Bo's desktop
  tutti (Ana)  ---- signed HTTP ----> tutti household serve <---- signed HTTP ---- tutti (Bo)
  her accounts                        members, machines, leases,                  his accounts
  her prompts                         shared backlogs, totals                     his prompts
       \                                     |                                     /
        +------ ssh / Ollama --------> shared machines: GPU desktop, mini PC <----+
```

### When the coordinator cannot be reached

Every call has a 3 s timeout and a round never waits on it. Unreachable, a member works alone with the last answer it
had, and **fails closed for what is shared, open for what is its own**:

- its own repos, accounts and its own machines: as a household of one;
- a shared project's tasks: none start (another member may hold their lease); ones already running finish;
- another member's shared machine: not used (its share is unknown); its own shared machines: used, at most its last grant.

The dashboard says "household coordinator not reachable since 14:02: working alone (shared projects paused)".

## Auth and the threat model

- **Join code.** `tutti household init` prints a code like `tutti-hh:mini.lan:4778:7KQ2-M9XD-4TRW-P3ZA-HB6E-YN5C`
  (the coordinator's address and a 120-bit secret). It is shown once, valid 7 days (`household code` makes a new one and
  ends the old), and is read off the screen or sent by the person, never by Tutti.
- **Per-user token, never sent.** `tutti household join <code>` sends its name, its main agent and a random nonce, signed
  with the join code's secret; the coordinator answers with its own nonce. Both sides derive the user's token as
  `HMAC-SHA256(secret, user id | both nonces)`; it never travels. The member keeps it in `~/.config/tutti/household.json`
  and the coordinator in `household/members.json`, both mode 600.
- **Every request signed.** `X-Tutti-User`, `X-Tutti-Time` and `X-Tutti-Sign = HMAC(token, method | path | time | nonce |
  SHA-256 of the body)`; the coordinator refuses a time more than 120 s off, a nonce it saw, a body over 64 KB, and a user
  that left or was removed. A wrong join code is refused, and after 10 wrong tries in 10 minutes from one address the
  join is closed for that address for an hour.
- **What crosses the network.** Bodies are not encrypted (standard library only), so they carry nothing private: a user's
  name, main agent's *kind*, active or not, a 7 x 24 activity profile (0 to 1 per hour, not prompts), running jobs as
  counts per shared machine, per-user totals, the shared projects' task titles (already visible to every member of that
  project) and lease keys. Never a prompt, brief, diff, log, path, reading of an account, account name or credential.
  A household that wants encryption on the wire runs the coordinator behind its VPN or `tutti tunnel`.
- **Who sees what.** A member sees its own everything, and of the household only: who is a member and active, each shared
  machine's slots used per user, the per-user totals (runs today, minutes on shared machines) and the fairness. Another
  member's jobs show as "busy for Bo". The coordinator's own login can read its state folder: the shared backlogs and the
  aggregates, nothing more.
- **Who may pause what.** A shared machine's owner (the member whose config it came from) may pause it for everyone or
  take it back out (`household machine unshare`); the coordinator's admin (the member who ran `init`) may pause any shared
  machine and remove a member. Every member can stop their own use of any machine, and the yield, pause and heat flags
  of a machine are respected by everyone's routing as today.

## Same project, several users

A repo in `autopilot.repos` with `household: true` is a **shared project**. Its key is its normalised `origin` remote
(`github.com/owner/name`), so the same project matches across members whatever its local path.

- **One backlog.** Tasks added to a shared repo (`tutti backlog add`, `fill_room`, `seed`) are published to the
  coordinator's backlog of that project; each member's autopilot mirrors the shared tasks into its own backlog (source
  `household`), so the queue, retries and the ladder work as before.
- **Dedupe.** A task's key is its title, folded (case, spaces, punctuation). A key the shared backlog has, in any status
  (done and dropped too), is never added again, and a task one member finished is closed for all.
- **Leases.** Before a shared task starts, its autopilot claims the lease `task:<project>:<key>` for 30 minutes; every round
  renews the leases of its running tasks (the heartbeat). A lease held by another member makes the task wait ("leased by
  Bo"); one whose heartbeat stopped expires and the task may be taken again, continuing from its branch when that member
  pushed it. A worktree name and a branch carry the task id; the lease on the task covers both.
- **`fill_room` under each user's own accounts.** Each member fills its own accounts' room from the shared backlog first,
  then from the repo's sources; whose account does the task is only the member that holds its lease.

## Fair use of shared machines

The coordinator turns every member's demand on each shared machine into **grants**: how many of the machine's slots each
member's runs may hold now. A member's routing treats a shared machine where it holds its grant as full (it routes
elsewhere or queues), so the slot queue on the machine stays first come, first served within the grants.

1. **Capacity** is the machine's slots, minus nothing while it is fit; a hot machine (`heat`) gives `hot_slots`; paused,
   yielding or offline gives 0.
2. **Interactive first.** A member who is prompting now (their main agent's account rose in the last 15 minutes, or a
   run they started by hand) has *interactive* demand; their runs are served before anyone's background autopilot.
3. **Weighted max-min fair share** of the rest (water-filling): each member's weight (`household.weight`, default 1) times
   one plus their expected activity this hour from their 7 x 24 profile, so a shared GPU follows whoever is awake.
4. **No starvation.** A member whose demand got no slot for 20 minutes takes one slot from the member with the most
   background grants, at the next round.
5. **Work-conserving.** Slots nobody else wants are granted to whoever wants them, so a machine is never idle while
   someone has work.

The **household plan** combines every member's 7 x 24 profile into the expected users per hour; the shared local models
(Ollama on a GPU) take each member's background tasks in the hours the others are expected to be away. Each member's own
accounts are still planned by their own pacing (USAGE-PACING.md), so every account of every member ends near 100 %
before its reset.

## Setup: minimum input

The coordinator (once, on the always-on computer):

```bash
tutti household init            # asks the household's name; prints the join code; serves on port 4778
```

Each other member (once, in their own login):

```bash
tutti household join tutti-hh:mini.lan:4778:7KQ2-M9XD-4TRW-P3ZA-HB6E-YN5C   # asks the name (default: the login) and main agent
```

A machine's owner shares it, and another member uses it:

```bash
tutti household machine share pc        # the owner: pc joins the household's machines
tutti household machine use pc          # a member: adds pc to their config (their own ssh login on it)
```

A shared project:

```bash
tutti household project share tutti    # autopilot.repos.tutti becomes shared (household: true)
```

`tutti household status` shows the members, who is active, the shared machines' use per member, the fairness, and the
leases; `tutti household leave` ends the membership (the member's own setup is untouched).

`tutti user` shows who this Tutti works for (name, a hashed id, main agent, config and state folders, household role);
`tutti user name <name>`, `tutti user main <agent>` and `tutti user share <account> on|off` change it.

## Telemetry

New events (`household.join`, `household.lease`, `household.sync`, `household.grant`) carry categories and counts only;
a user is named by `user` = the first 12 hex digits of `SHA-256("tutti-user:" + user id)`, never a name.

## Migration

- No `user:` or `household:` key and no `household.json`: one user (the login's name), a household of one; nothing
  changes in routing, pacing, the backlog or the dashboard.
- Joining changes nothing until a project or machine is shared: an unshared repo and an unshared machine stay private.
- Leaving removes `household.json`; shared repos and machines go back to private.

## Increments

| Release | What |
| --- | --- |
| 1 | the user model (`tutti user`, the hashed id, `share_spare`), per-user state, the single-user migration, this design |
| 2 | the coordinator, `household init\|join\|status\|leave\|code`, signed requests, leases with heartbeats, shared backlogs and dedupe |
| 3 | fair shares of shared machines in routing and the autopilot, the household plan from every member's profile, `share_spare` |
| 4 | the dashboard: **Me / Household**, the household card, others' jobs as "busy for <user>" |
