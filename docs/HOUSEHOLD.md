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
Each person installs Tutti in their own login (the one-line install of docs/INSTALL.md; the npm name `tutti-agents` is not published yet, so never `npm i -g` it), signs their agents in
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

### Away from home with no VPN

Many members have no VPN: away from home, the coordinator (never on the internet) cannot be reached, and their Tutti
works on its own until they are back:

- their own accounts, readings, pacing and machines as usual; their own shared machines keep their last grant;
- no shared task starts and no household lease is taken or renewed: the leases they held run out after
  `lease_minutes`, so the others may take those tasks (a task the member was running finishes; if another member took it
  meanwhile, both results are kept and the task says "compare before landing");
- what they would report is not lost: each round's report is built from their own backlog (tasks added, tasks finished,
  their totals), so the first round back on the home network sends all of it ("back home: synced ...");
- the dashboard on their computer says **away from the household: working on your own until you're home**, in both
  views.

When a member's laptop leaves or sleeps, the coordinator carries on with the household's machines and devices. When the
coordinator's own computer is off, members work alone and their shared tasks wait until it is back. An always-on
coordinator, such as a NUC, avoids that pause. An admin on the coordinator can move a member's task to another member
(`tutti job move lease:<project>:<task>`). The old holder's next sync then refuses it, and the task is never run twice.
See [PRESENCE.md](PRESENCE.md).

## Auth and the threat model

- **Invite** (with the dashboard's sign-in, the usual way): see *Joining with an invite* above.
- **Join code** (without sign-in). `tutti household init` prints a code like `tutti-hh:mini.lan:4778:7KQ2-M9XD-4TRW-P3ZA-HB6E-YN5C`
  (the coordinator's address and a 120-bit secret). It is shown once, valid 7 days (`household code` makes a new one and
  ends the old), and is read off the screen or sent by the person, never by Tutti.
- **Per-user token, never sent.** `tutti household join <code>` sends its name, its main agent and a random nonce, signed
  with the join code's secret; the coordinator answers with its own nonce. Both sides derive the user's token as
  `HMAC-SHA256(secret, user id | both nonces)`; it never travels. The member keeps it in `~/.config/tutti/household.json`
  and the coordinator in `household/members.json`, both mode 600.
- **Every request signed.** `X-Tutti-User`, `X-Tutti-Time` and `X-Tutti-Sign = HMAC(token, method | path | time | nonce |
  SHA-256 of the body)`; the coordinator refuses a time more than 120 s off, a nonce it saw (kept in its state file, so a
  restart does not forget them), a body over 64 KB, and a user that left or was removed. A wrong join code is refused, and after 10 wrong tries in 10 minutes from one address the
  join is closed for that address for an hour. A join never takes the id of a member who is in (ids travel in clear), and
  a member the admin removed comes back only with a code made after the removal.
- **Every answer signed.** The coordinator signs its answer to a member's request with that member's token
  (`X-Tutti-Answer = HMAC(token, ANSWER | path | | request nonce | SHA-256 of the answer)`), and a join's answer with the
  code's secret. A join needs that signed answer (a coordinator on an older Tutti is updated first), and the membership
  refuses unsigned answers from then on (one made by an older Tutti does so once it has seen a signed answer). Someone else on the LAN answering in the coordinator's
  place cannot hand you jobs or actions.
- **Nothing a member writes runs as a command elsewhere.** Another member's task `check`, and a spare-room offer's, are
  never copied: your own repo's check gates the task. A member's job runs from plain files (`git archive`, no .git) in a
  container on the machine and runner that really run it; a job naming a device runs only on a device of that shared
  machine whose runner is a container (or with `member_shell`). The shared lock folder must be sticky (mode 1777).
- **What crosses the network.** Bodies are not encrypted (standard library only), so they carry nothing private: a user's
  name, main agent's *kind*, active or not, a 7 x 24 activity profile (0 to 1 per hour, not prompts), running jobs as
  counts per shared machine, per-user totals, the shared projects' task titles (already visible to every member of that
  project) and lease keys. Never a prompt, brief, diff, log, path, reading of an account, account name or credential.
  A household that wants encryption on the wire has its members reach it over the household VPN (WireGuard, Tailscale).
- **Who sees what.** A member sees its own everything, and of the household only: who is a member and active, each shared
  machine's slots used per user, the per-user totals (runs today, minutes on shared machines) and the fairness. Another
  member's jobs show as "busy for Bo". The coordinator's own login can read its state folder: the shared backlogs and the
  aggregates, nothing more.
- **Who may pause what.** A shared machine's owner (the member whose config it came from) may pause it for everyone or
  take it back out (`household machine unshare`); the coordinator's admin (the member who ran `init`) may pause any shared
  machine and remove a member. When the admin leaves, the longest-standing member who stays becomes the admin. Every member can stop their own use of any machine, and the yield, pause and heat flags
  of a machine are respected by everyone's routing as today.

## Network: LAN or VPN, never the internet

The owner's rule: a household's dashboard and coordinator are never exposed to the internet; people use them on the LAN,
or from elsewhere through the household VPN. `tutti/netpolicy.py` enforces it for the coordinator, join codes, invite
joins and `tutti mcp --http`; the dashboard's own bind and origins follow the same rule through its sign-in
(docs/AUTH.md). An invite page is reached only when the dashboard listens on a LAN or VPN address (`dashboard.bind`,
`dashboard.origins`); the default, 127.0.0.1, is this computer only:

- They may listen only on localhost or a private, LAN or VPN address: RFC 1918 (10/8, 172.16/12, 192.168/16), CGNAT
  100.64.0.0/10 (Tailscale), IPv6 ULA fc00::/7, or any address of a WireGuard (or Tailscale, ZeroTier, Nebula) interface.
- `household.bind: auto` (the default) listens on localhost (IPv4 and IPv6) and each LAN and VPN address of this
  computer: IPv4, IPv6 ULA and link-local (with its interface), and the default route's own address, the one join codes
  name, also where the interfaces cannot be read. Never a container bridge, explicitly either. Interfaces come from
  `ip -j addr`, else `ifconfig -a`; an address on a WireGuard, Tailscale (`utun` on macOS) or ZeroTier interface counts
  as VPN whatever its range, in the config check too. A wildcard (`0.0.0.0`, `::`) would include a public interface and is refused, like a public address:
  the command stops and says why and which addresses it may use. `tutti config` names such a setting too.
- Join codes and invite links name a LAN or VPN address only; `tutti household join` refuses a public one before it sends
  anything. Tutti never offers a public tunnel or a port forward.

**A member away from home** joins and works through the household VPN: put the coordinator's computer and theirs on the
VPN (e.g. Tailscale or WireGuard), and make the invite from the dashboard's VPN address (its `dashboard.origins` entry);
`tutti household join <invite-url>` then reaches the coordinator at that address. On the LAN nothing else is needed.

## Joining with an invite (the dashboard's sign-in)

When the dashboard signs people in with Google (docs/AUTH.md), its **Users** card's **Invite someone** makes the one link
a new member needs; the household starts by itself on the admin's dashboard with the first one (no `household init`).
The new member, in their own login:

```bash
tutti household join "https://<dashboard on the LAN or VPN>/invite#<code>"   # quoted: a shell drops # and what follows
```

1. Their Tutti follows the guided rule (docs/INSTALL.md, `tutti/guided.py`): it asks nothing. It detects their main agent
   (the config's when it is installed, else the installed one, the first of claude, codex, cursor, agy, copilot, gemini,
   local when several are; `tutti user main-agent <kind>` changes it) and their subscriptions (the kinds of their
   accounts), and sends the invite's id, proved with a secret both sides derive from the invite (the invite's code
   itself never travels). What only they can do (a sign-in) is listed once at the end.
2. Until someone has signed in with that invite, the coordinator answers "sign in first": the link opens in their
   browser, they sign in with Google, and the join is asked again every 3 s (up to 2 minutes). Done in under a minute.
3. Their household identity is that Google user (`sub` and e-mail), not a separate id; their name comes from it. The
   coordinator ties the membership to the invite's sign-in and uses the invite up for joining.

A member with no Tutti yet pastes the invite page's one install line, `install.install_line(platform, join=<invite>)`
(`householdcmd.invite_lines(invite)` gives it per platform): the bootstrap installs Tutti, `tutti setup --join` fills
the config with no questions and runs this join.

The **Users** card then shows, for each person, whether their Tutti joined and when their computer was last seen
(`household.members_by_sub`). The older `tutti-hh:` join code still works.

## Same project, several users

A repo in `autopilot.repos` with `household: true` is a **shared project**. Its key is its normalised `origin` remote
(`github.com/owner/name`), so the same project matches across members whatever its local path.

- **One backlog.** Tasks added to a shared repo (`tutti backlog add`, `fill_room`, `seed`) are published to the
  coordinator's backlog of that project; each member's autopilot mirrors the shared tasks into its own backlog (source
  `household`), so the queue, retries and the ladder work as before.
- **Dedupe.** A task's key is its title, folded (case, spaces, punctuation). A key the shared backlog has, in any status
  (done and dropped too), is never added again, and a task one member finished is closed for all.
- **Leases.** A shared task starts only under its lease `task:<project>:<key>`: the round's call reserves, in its order,
  as many free tasks as the member could start now (`max_parallel` minus its runs) for 5 minutes, and a task that starts gets the full 30 minutes in a second call right away; every round renews the
  leases of its running and in-review tasks (the heartbeat). A lease held by another member makes the task wait ("leased by
  Bo"); one whose heartbeat stopped expires and the task may be taken again (it starts over in the new holder's worktree). A
  task mirrored from the household is reserved at the member's next round. Only the member that published a task or holds
  its lease may close it for everyone. A worktree name and a branch carry the task id; the lease on the task covers both.
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

The **household plan** combines every member's 7 x 24 profile (in their own local hours with their IANA zone, from their Tutti host or their browser, so each member's daylight saving is their own; empty while they are
away or on vacation) into the expected members per hour, and that is the "expected activity" of step 3: in Ana's working
hours her background runs weigh twice Bo's, in his hours the other way round, and at night they share evenly. Each
member's own accounts are still planned by their own pacing (USAGE-PACING.md), so every account of every member ends
near 100 % before its reset; the shared machines only decide where and when their runs go.

What a member tells the coordinator each round, per shared machine: its runs there by hand (interactive) and by the
autopilot (background; an autopilot run's jobs carry `class:background`), one more interactive slot while it prompts
on the first shared machine of its routing order (its next test should not wait), its queued tasks spread once over the
machines (never counted on each), and for its own shared machines how many slots are fit now (0 while offline,
yielding, paused or too hot; `heat.hot_slots` while hot). Counts only; a status look sends none and keeps the last.

A member's routing (`tutti test`) and its autopilot see a shared machine with at most `grant - its runs there` free
slots; the dashboard's machine and the autopilot's reason say who it is busy for ("household share: 1 of 4 slot(s)
yours now (busy for Bo 3)"). A grant older
than 5 minutes is not used: another member's machine then counts as full, one's own keeps its last grant.

### Joining a machine

`tutti household machine share pc` (its owner, once): Tutti makes `/tmp/tutti-household` on pc (mode 1777 like `/tmp`:
every member's login can make a slot lock there and nobody can remove or replace another login's file; `slot.sh` opens a
shared lock read-only, which `flock` allows), points the owner's own `machines.pc` at it (`lock_prefix`, kept in
`user.json`, never in the config file), and tells the
coordinator pc's address (`--host`, default its `host`, or this computer's LAN address for a `local` machine), its slots
and whether it has a GPU. The owner's yield flag stays where it is: while it is up (or pc is offline or paused), the
owner's heartbeat reports no fit slot and the coordinator grants nobody a slot on pc.

`tutti household machine use pc` (each other member): pc joins their machines (in `user.json`, transport `household`)
and that is all: **no login on pc, nothing set up there**. See *Compute on shared machines* below.

`machine pause|resume pc` pauses it for the whole household (its owner or the admin); `machine unshare pc` takes it out.

## Compute on shared machines

A shared machine is **compute only**: tests, gates, builds, local models and devices. A member never logs in to it, and
no credential of theirs goes there; work that needs a member's subscription (their agent) runs on the member's own
computer. `tutti/hhjobs.py`:

1. Bo's `tutti test` (or a routed check) picks pc, a machine of transport `household`, by his fair share of it.
2. His worktree as it is (`git stash create`: tracked files with his uncommitted changes; HEAD when clean) is pushed
   with **his own** git sign-in to `refs/tutti-wip/<his hash>/<id>` of the repository's origin. The job (pc, the repo
   URL, that ref, the command) goes to the coordinator, signed.
3. Ana's Tutti (pc's owner; her dashboard round) claims the jobs for the machines she shares, fetches the ref with
   **her** git sign-in into a sandboxed worktree of its own (`<state>/household/jobs/<id>`), and runs the command with
   her own `tutti test --on pc`: pc's runner, a container (memory and CPUs capped, no home folder mounted), behind pc's
   slot queue. The run counts as Bo's demand on pc, not Ana's.
4. The exit code and the end of the output (8 000 characters) go back through the coordinator; Bo's command prints them
   and exits with that code. The WIP ref and the sandbox are deleted.

A shared machine runs only the household's shared projects: the job's repo must be one (its origin, `https` or `ssh`,
fetched with git's file and ext protocols off and no password prompt), so a member can never point the owner's git
sign-in at another host. Bo's repository needs an origin both can read; otherwise the run is refused with the reason
and he runs it on his own machines (`--on <machine>`). A claimed run lasts its timeout plus 15 minutes before it may be
claimed again; a result the coordinator could not take is kept and sent again; the WIP ref is deleted once the result is
in (while the run may still go on, it stays). A paused machine takes no more of the queued runs. `--input`, `--fetch` and `--device` go to his own machines.

### Trust model

- **What Bo trusts Ana with:** the code of the ref he pushed, the command and its output. Not his prompts, briefs,
  accounts, readings, tokens or files outside that commit.
- **What Ana trusts Bo with:** running his command on her machine, as her Tutti, inside pc's container runner (no
  network rule beyond the container's, memory and CPUs capped, her home folder not mounted). A shell runner would run
  his command as her login with her files in reach, so it is refused unless she sets `household.member_shell: true` for
  a household she trusts that far.
- **Never crossing:** credentials (Bo's git sign-in pushes, Ana's fetches; agents' sign-ins stay home), the member
  tokens, logins. The coordinator sees the job (repo URL, ref, command, exit code, output tail) and nothing more.
- **Who decides:** pc's owner shares, pauses or unshares it (`tutti household machine pause pc`) and chooses the runner;
  her Tutti claims only jobs for her own machines, and no member can claim or read another's job.

## Your main machine

A user's **main machine** is the computer where their main agent and their own Tutti live. Each user (the admin too)
changes it with one click, **Me → My main machine** on their dashboard, or `tutti user main-machine <m>`
(`--dry-run` shows the plan). It is `tutti move` for that user's own Tutti (tutti/mainmachine.py over tutti/move.py, no
machinery of its own): their state moves (schedule, mode and the learned profile, readings, backlog, `user.json`), their
household membership and token (mode 600, never printed), their agent logins, connected agents' MCP entries and the
readings forwarder as `tutti move` moves them, and the old computer becomes a client of the new one. WIP refs live on
the repository's origin and need nothing. The target is one of the user's own machines (ssh or WSL), never another
member's compute-only machine.

It is not the household coordinator's move: a member's main machine moves only that member. An admin whose login also
runs the coordinator moves it with their Tutti, as `tutti move` does (members are repointed by themselves).

**Who may:** only the user, for themself. The dashboard's server checks it: with sign-in (docs/AUTH.md) a signed-in
user whose Google identity is not that Tutti's user is refused, whatever their role; the admin acts for a member
through the Users tab (below), which their own Tutti carries out.

**Presence follows it:** the heartbeat comes from the new main machine. A host whose client (the laptop they sit at)
connected within the last 14 days counts its user present only while one is connected; a laptop that leaves makes them
away, as a member away from home is. A Tutti no client has reached lately is where its user sits: present (a host
configured for clients that none has reached yet, or a retired laptop's old status file, never makes them away).

## The Users tab (managing a member)

The admin may be the only technical person at home, so everything a member would do for their own setup the admin can
do for them, from one place: the **Users** tab, next to Dashboard and Settings, for admins only (viewers never see it,
and the server refuses them). Settings keeps the system's configuration; everything about people is here: users and
roles and invites (docs/AUTH.md: the sign-in draws them; the household part, below, is drawn into the same tab), the household membership, machine sharing, each user's
schedule and main agent.

Each user is a row, the admin's own marked **(you)** with exactly the same fields and controls (in a household of one
too, where its actions run at once on this computer): main machine, presence and last seen, whether their Tutti joined,
mode and schedule, and their consumption (per account kind: used, verdict and target; runs today). **Manage <name>**
opens every action for them: schedule, away and vacation; main agent; main machine; reconnect agents (`tutti
connect`); pair a device; use a shared machine; pause or resume a machine; update Tutti and the agents; join, rejoin or
install (the invite to hand them); make admin; remove.

How it reaches the member: the admin's dashboard queues the action at the coordinator (`/hh/act`); the coordinator lets
the admin act for anyone and a member only for themself, and writes the audit line ("schedule by admin Ana for Bo"); the
member's own Tutti claims it at its next round and does it on the member's computer, as the member, and its result
shows on the row. What only the member can do is never technical work for the admin: the tab shows the one step to hand
them (a Google sign-in in their own browser, the pairing code on their phone) or the invite's QR code.

**The last-admin guard:** the household's admin cannot remove themself (nor, through the sign-in's roles, demote
themself) while they are the only admin; they make another member admin first. Their row shows those controls disabled,
with the reason as a tooltip, never hidden.

**The audit**, filterable by user, lists every action, by whom and for whom. **What the admin sees** of a member here
goes beyond the household view: the consumption by account kind and their schedule, never their prompts, briefs,
account names, readings' sources or tokens.

## Spare room shared with the household

An account with `share_spare: true` (or `tutti user share <account> on`) whose planner says it has tasks to give now,
within `household.share_spare_hours` (default 6) of its target, and is neither in panic nor spent, is *spare*. While a
member has a spare account its heartbeat says so, and the coordinator offers the shared projects of other members that
have queued tasks nobody holds (with the gates, `check`, their members publish). Tutti clones such a project once with
the member's own git sign-in into `<state>/household/repos/hh-<name>` (a failed clone is said and tried again after 6 h)
and adds it as a *spare repo*: its tasks are leased like any shared task, only the spare accounts take them, and a result
becomes a draft pull request a member of that project merges. With no spare account, they wait ("only room your accounts
would lose at their reset does it").

## Setup: minimum input

What ships when is under *Increments* below: `tutti user` and `tutti household status` (0.1.29), `init`, `join`,
`leave`, `code`, `serve` and `project share` (0.1.31), `machine share|unshare|use|pause|resume` and spare room (0.1.34).

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
tutti household project share tutti    # autopilot.repos.tutti becomes shared (or write household: true under it)
```

`tutti household status` shows the members, who is active, the shared machines' use per member, the fairness, and the
leases; `tutti household leave` ends the membership (the member's own setup is untouched; its shared repos turn private,
so a later join starts with nothing shared). `tutti household serve --port <n>` moves the coordinator: new join codes
name the new port. `init` always starts a fresh household: an older coordinator file's members and tokens are gone.

`tutti user` shows who this Tutti works for (name, a hashed id, main agent, config and state folders, household role);
`tutti user name <name>` and `tutti user share <account> on|off` change it.

### The main agent

Every member uses their own main agent and may change it at any time: `tutti user main-agent <kind>`, one of `claude`
(Claude Code), `codex` (Codex / ChatGPT), `agy` (Antigravity), `cursor`, `copilot`, `gemini` or `local` (a local model).
The default is what `tutti setup` detects. The choice is kept in `user.json` (the config file is untouched) and names
the config's worker of that kind; the planner's owner reserve (USAGE-PACING.md) then applies to the accounts behind that
worker, so it moves with the switch. A switch runs `tutti connect` for the new agent (tutti/connect.py's `connect_all`).
The household view shows each member's main agent (its kind only).

## Files

| Where | What |
| --- | --- |
| member: `~/.config/tutti/household.json` (600) | the household, the coordinator's address, the user id, the derived token, the role |
| member: `<state>/user.json` | the name, the `share_spare` switches, the repos shared by `household project share`, its shared machines' paths (`machine_overrides`), the machines it uses (`household_machines`), adopted spare repos |
| member: `<state>/household/grants.json` | the last grants per shared machine (routing reads them; 5 minutes fresh) |
| member: `<state>/household/repos/` | clones of other members' projects adopted for spare room |
| coordinator: `<state>/household/coordinator.json` (600) | members (name, hash, agent kind, token, last heartbeat, totals), the join code's secret and expiry, leases, the shared backlogs |

The coordinator's API: `POST /hh/join` (signed with the join code), `/hh/sync` (the heartbeat, every shared project and
the grants in one call per round; offers when spare), `/hh/status`, `/hh/machine` (share, unshare, pause, resume),
`/hh/leave`, `/hh/code` (the admin only); `GET /hh/health`.

## Telemetry

New events (`household.init`, `household.join`, `household.sync` with its outcome `ok`, `offline` or `refused`, `household.grant` with
the shared machines, the slots granted and how many machines limited this member) carry categories and counts only;
a user is named by `user` = the first 12 hex digits of `SHA-256("tutti-user:" + user id)`, never a name.

## Migration

- No `user:` or `household:` key and no `household.json`: one user (the login's name), a household of one; nothing
  changes in routing, pacing, the backlog or the dashboard.
- Joining changes nothing until a project or machine is shared: an unshared repo and an unshared machine stay private.
- Leaving removes `household.json`; shared repos and machines go back to private.

## Increments

| Release | What |
| --- | --- |
| 1 (0.1.29) | the user model (`tutti user`, the hashed id, `share_spare`), per-user state, the single-user migration, this design |
| 2 (0.1.31) | the coordinator, `household init\|join\|status\|leave\|code`, signed requests, leases with heartbeats, shared backlogs and dedupe |
| 3 (0.1.34) | fair shares of shared machines in routing and the autopilot, the household plan from every member's profile, `share_spare` |
| 6 (0.1.64) | your main machine (Me → My main machine, `tutti user main-machine`); the Users tab: an admin acts for any member, audited |
| 5 (0.1.51) | shared machines as compute only: a member's runs go to the owner's Tutti as a WIP ref and a signed job; no login for members |
| 4 (0.1.46) | joining with the dashboard's invite and Google identity, LAN or VPN only (`netpolicy`); the dashboard: **Me / Household**, the household card, others' jobs as "busy for <user>" |

## A new member's machine

A member with no Tutti yet installs and joins in one go: the coordinator shows `install.install_line(platform,
join=<invite>)` (docs/INSTALL.md, *The interface*); the bootstrap installs Tutti, `tutti setup` fills the config and
opens the member's own sign-ins, then runs `tutti household join <invite>`.

