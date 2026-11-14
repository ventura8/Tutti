# Security audit, November 2026

A full audit of Tutti 0.1.66 to 0.1.69 before strangers use it: the dashboard, the MCP server, the household coordinator and its
member jobs, the workers and the autopilot, secrets, subprocess calls, the install and update chain, the browser broker,
devices, and the telemetry. Each finding has a severity, what an attacker does, and what changed. Every fixed finding has an
exploit-style test that plays the attack (named below). Fixed in 0.5.1.

Severity: **critical**, someone else's code runs on your computer outside the sandbox you chose; **high**, someone takes over
an account, a role or your files, or runs code through a path you did not open; **medium**, a weaker version of either, or one
that needs a position on your network; **low**, hardening, or a risk that needs your own config or computer.

## The threat model

- **The internet** reaches nothing: the dashboard, the coordinator and `tutti mcp --http` refuse public binds and drop public
  clients (tutti/netpolicy.py). It does reach Tutti indirectly, through text Tutti reads: a public repository's issues and
  comments, Sentry events (anyone can send events to a public DSN), AI answers, pull request diffs and reviews.
- **The LAN or VPN** (a café's Wi-Fi counts) can send requests to whatever listens there, and can watch or answer plain HTTP.
- **Another household member** holds a join code and their own token, and can publish tasks and submit jobs.
- **Another login on the same computer** (a household's shared machine) can open 127.0.0.1 and write to /tmp.
- **A signed-in viewer** may look, never change, and act only for themself.

## Findings

| # | Severity | Where | Finding | Status |
| --- | --- | --- | --- | --- |
| C1 | critical | household jobs | A member's job that named a device ran on the machine the device is on, with the device's runner (a shell): `member_shell: false` did not stop it | fixed |
| C2 | critical | household jobs | The job's container got the owner's git cache read-write (a linked worktree mounts the main .git); a hook planted there ran under the owner's own git next time | fixed |
| H1 | high | coordinator | A join with the code and another member's id (sent in clear on every request) replaced their token: their jobs, machines and role were taken; a removed member came back the same way | fixed |
| H2 | high | household sharing | Another member's task `check` and a spare-room offer's `check` were copied into your backlog and run as shell commands by your autopilot | fixed |
| H3 | high | dashboard, Users tab | A viewer who named the owner's member id counted as acting "for themself" and queued the owner's main machine, schedule, update or connect; a household of one did no role check at all | fixed |
| H4 | high | dashboard without sign-in | Another login on the same computer opened 127.0.0.1, read the page's token and used Settings (worker commands run as you) | fixed (Linux) |
| H5 | high | slot.sh on a shared machine | Every job ran with umask 0 (its files writable by every login); a link planted in a lock folder made without the sticky bit created a file of the attacker's choice in the victim's home | fixed |
| H6 | high | scouts | The first comment after the scout's question was its answer, whoever wrote it: in a public reply repository anyone queued tasks | fixed |
| H7 | high | Sentry triage, workers | Text from Sentry events reached the worker's brief (with a "check" command), and workers inherited GH_TOKEN and the scout's webhook key | fixed (the command and the tokens); the residual risk is below |
| H9 | high | dashboard Chat (0.1.69) | A viewer's chat (or a model steered by text it read) queued a backlog task with a `check`, a shell command the admin's autopilot runs on the host | fixed: the chat's `backlog_add` has no `check` (`test_chat.test_a_chat_task_never_carries_a_shell_check`) |
| H8 | high | self-update | `SHA256SUMS` comes from the same release as the tarball and is not signed: whoever can publish a release reaches every install within a day | open: needs the owner's signing key (backlog) |
| M1 | medium | coordinator | A negative Content-Length made the server read until the socket closed, unauthenticated | fixed |
| M2 | medium | every server | No socket timeout and no limit on threads: slow or many connections held the process | fixed |
| M3 | medium | autopilot `land: merge` | The merge was not pinned to the reviewed commit: a push after the review went in with it | fixed |
| M4 | medium | self-improvement | The reviews were not tied to a commit (a later push merged unreviewed), and a local fallback review with findings counted as reviewed | fixed |
| M5 | medium | conductor with a local model | A local model could pass `--check "<shell>"`, rewrite any file with `code` and read any file with `ask` | fixed |
| M6 | medium | `tutti mcp --http` | On a LAN address the token gave the admin role by default (admin can start test commands); no Origin check | fixed |
| M7 | medium | autopilot `land: merge` | A worker that edited the check script (or CI, or the lint config) made its own gate pass and merged | fixed: such a branch stops at a pull request |
| M8 | medium | worker CLI updates | `npm install -g <cli>@latest` every day on every machine: a hijacked release arrived within hours | fixed: a 3-day cooldown (`--before`) |
| M9 | medium | coordinator answers | Answers were not authenticated: someone answering in the coordinator's place on the LAN handed the owner jobs and the member actions | fixed: signed answers |
| M10 | medium | slot locks in /tmp | `exec 9> /tmp/tutti-slot-N.lock` followed a planted link and truncated its target (macOS has no protected_symlinks) | fixed |
| M11 | medium | docs | HOUSEHOLD.md said `npm i -g tutti-agents`, a name nobody has published (anyone could) | fixed in the docs; reserving the name is the owner's step |
| L1 | low | install | The legacy `install/install.sh` and `install.ps1` pipe `main` into a shell and upgrade pip unpinned | backlog |
| L2 | low | install | Python dependencies are pinned with `==` but not by hash; the uv and pipx channels install HEAD | backlog |
| L3 | low | adb | Android platform-tools are downloaded without a checksum (HTTPS only) | backlog |
| L4 | low | move | An ssh alias with a quote or a backtick broke out of the remote grep | fixed |
| L5 | low | config | `machines.<m>.host` starting with `-` was an ssh option | fixed (config check) |
| L6 | low | activity scan | `/tmp/tutti-cwd.$$` listed your working folders to other logins | fixed: a private mktemp folder |
| L7 | low | MCP | A tool argument starting with `-` was read as an option | fixed |
| L8 | low | dashboard sign-in | `POST /api/auth/device/start` needs no session: a LAN host can spend the owner's Google device-flow quota (32 flows at most) | backlog |
| L9 | low | household | Members report their own fair-share weight and demand; the state file has no cap on projects, tasks or lease keys | backlog |
| L10 | low | dashboard | Viewers see the command lines of watched processes (a token typed on a command line would show) | backlog |
| L11 | low | dashboard | Links built from data (a PR or usage URL) are not checked for `javascript:` (the CSP blocks them) | backlog |
| L12 | low | coordinator | A join with an invite founds the household state before the invite is proved | backlog |
| L13 | low | gitleaks | The scrubber's tests hold fake AWS keys and JWTs that gitleaks flags in history | accepted: test data, no real secret |

## What each fix does, and its test

- **C1** `hhjobs.sandbox_refusal` checks the machine and runner that will really run the job: a device must be named (never
  `any`), be on the shared machine, and its merged runner must be a container unless `household.member_shell`.
  `test_hhjobs.test_a_device_never_takes_a_members_run_out_of_the_container`.
- **C2** The job's files are exported with `git archive` into a plain folder (tarfile's data filter: no path or link leaves it);
  the container never sees a .git, and the owner's git over the cache runs with no hooks and no fsmonitor. The folder is
  removed with `rmtree` (never follows a link). Job ids are checked (`j<number>`) before they name a file.
  `test_the_run_never_sees_the_owners_git_cache`, `test_a_claimed_job_whose_id_is_a_path_is_never_written`,
  `test_an_archive_with_a_link_out_of_the_tree_is_refused`.
- **H1** `household.taken_id`: a join never takes an id that is a member; a removed member needs a code made after the
  removal; a member who left on their own may join again. `test_security_audit.JoinTakeoverTest`.
- **H2** `sharing._mirror` and `adopt_offers` never copy a `check`: the local repo's own check gates the task, and a spare
  repo's result is a draft pull request. The task title still reaches the worker, with the safety note (H7).
- **H3** `manage.is_self`: with sign-in, "self" is the signed-in user's own member id only; the household of one runs the same
  check. `test_security_audit.ViewerActsForTheOwnerTest`.
- **H4** `httpguard.loopback_owner_ok`: without sign-in, a loopback connection is served only when /proc/net/tcp lists it
  as established and owned by this user; a connection not listed (looked for twice) is refused, and a closing socket's uid 0
  is never taken as root. macOS and Windows have no such table: there the dashboard is as before, so use sign-in
  on a computer others log in to. `test_security_audit.LoopbackOwnerTest`.
- **H5** slot.sh sets umask 0 only for the lock file it makes, makes it with noclobber (O_EXCL: never created through a
  planted link), refuses a lock that is a link, and opens every lock read-only (a link swapped in after the check is never
  written); `household machine share` requires the shared folder to be sticky
  and not a link. `test_slot.test_a_planted_link_is_never_followed_and_the_run_keeps_the_callers_umask`.
- **H6** `scouts.trusted`: an answer comes from `scouts.<name>.github_login`, or from an owner, member or collaborator whose
  permission on the repository GitHub reports as write, maintain or admin. `test_scouts.test_a_strangers_comment_is_never_taken_as_the_answer`.
- **H7** The suggested check command is no longer in the task's notes; a task from Sentry, a scout, the household or feedback
  carries a safety note in its brief; workers and reviewers run with `workers.worker_env`, without GH_TOKEN, GITHUB_TOKEN,
  Sentry, npm, PyPI, cloud tokens and the scouts' URL and key variables. **Residual risk:** a worker that runs headless with
  commands allowed (`--force`) still reads attacker-influenced task text and the repository, and keeps its own CLI sign-in and
  the git and gh sign-ins stored in files. The defence is that it works in a worktree, never pushes, and that nothing it does
  lands without the gates, a review and, for a change to the gates, a person. Keep `self_improve.auto_accept` off for public
  projects if that is not enough. `test_security_audit.WorkerEnvTest`, `test_selfimprove` (the note).
- **H8** Open. The fix is a signature on `SHA256SUMS` by a key only the owner holds, verified by a public key in the code
  (`ssh-keygen -Y verify` needs nothing but OpenSSH), or releases built in CI with provenance. Until then an update is as
  trustworthy as the GitHub account that publishes releases: keep it behind two-factor sign-in and a hardware key.
- **M1, M2** `coordinator.content_length` refuses a missing number, a negative or an oversized length; every server
  (dashboard, coordinator, MCP) closes a connection silent for 30 s and serves a bounded number of requests at once, and at
  most a few per client address, so one host trickling bytes cannot hold every slot (`httpguard.CappedServer`). `test_security_audit.CoordinatorHttpTest`, `CappedServerTest`.
- **M3** `fill.merge` merges with `--match-head-commit` of the reviewed commit. `test_fill.test_the_merge_is_pinned_to_the_reviewed_commit`.
- **M4** The self-improvement round records the head commit the reviews were asked on and asks again when it changed;
  `tutti pr review` exits 3 when the local fallback review has findings. `test_selfimprove.test_a_commit_pushed_after_the_review_is_never_merged_unreviewed`,
  `test_orchestrator.test_a_local_review_with_findings_never_counts_as_reviewed`.
- **M5** `conduct.local_args_refusal`: no `--check` (also abbreviated), and `code` and `ask` only on files inside the folder
  the conductor works in. `test_conduct.test_a_local_model_gets_no_shell_through_check_and_no_file_outside_its_folder`.
- **M6** `tutti mcp --http` on a LAN or VPN address is a viewer unless `mcp.role: admin` (stdio and loopback stay admin); a
  request with another site's Origin gets 403. Free text (a title, a task, a question) goes after `--`, so it is never an
  option; a name argument (repo, project, scout, machine...) that starts with `-` is refused. `test_security_audit.McpTest`.
- **M7** `fill.gate_files_touched` (against `origin/<base>`): a branch that changes CI, build, dependency or lint configs, a
  file the check command names or anything in its script's folder, or modifies or deletes an existing test, stops at its
  pull request for a person. A best-effort list: the review and a person's look at `land: merge` repos remain the defence. `test_fill.test_a_branch_that_changes_its_own_gates_is_never_merged_by_the_autopilot`.
- **M8** `update.npm_before`: npm installs the newest version (and dependencies) out for 3 days. `test_update`.
- **M9** The coordinator signs each answer to a signed request with the member's token over the path, the request's nonce
  and the body (`X-Tutti-Answer`), and a join's answer with the code's (or invite's) secret. A join requires the signed
  answer and the membership it makes demands signed answers from then on; a membership made by an older Tutti demands them
  once it has seen one. `test_security_audit.CoordinatorHttpTest`.
- **M10** As H5: never truncated, never a link.

## Defences confirmed (not findings)

- Sign-in: the ID token's RS256 signature is checked against Google's JWKS (cached for its max-age, refetched at most once a
  minute for an unknown kid, keys under 2048 bits refused), then `iss`, `aud`, `exp` and `iat` with 60 s of skew, the nonce
  (the hash of an HttpOnly login cookie), and a verified e-mail. Sessions are random, stored hashed, HttpOnly and
  SameSite=Strict (Secure with `__Host-` over https), with a CSRF token compared in constant time on every POST, plus Origin
  and Sec-Fetch-Site checks. Roles are default-deny: every GET needs an approved user, every POST an admin except a short
  viewer list whose handlers check `may_act_for`. The last admin can be neither demoted nor removed; role changes are audited.
- Headers: a strict CSP (`script-src 'self'`, no inline script, `frame-ancestors 'none'`, `base-uri 'none'`), nosniff,
  `Referrer-Policy: no-referrer`; the login page's own CSP allows Google's script only. The page never uses innerHTML on
  data, and no Markdown is rendered.
- Static files are served only from inside `tutti/web` (realpath check); the Host header is checked (DNS rebinding); POST
  bodies are capped (8 KB to 64 KB) with a positive Content-Length.
- Household requests are signed over method, path, time, nonce and body hash, with 120 s of skew and a nonce cache; join
  codes carry 120 bits; wrong codes block an address for an hour; git fetches allow only https and ssh; WIP refs only under
  `refs/tutti-wip/`.
- The browser broker is a Unix socket (mode 600 in a 700 folder); no Chrome DevTools port is opened anywhere.
- ssh never disables host-key checking; secrets reach programs on stdin (gh token, adb keys, WireGuard and Tailscale keys);
  token files are mode 600, written atomically.
- Telemetry sends nothing without consent and a DSN; events carry no host, user or IP, values are allow-listed and every text
  is scrubbed (tokens of every common provider, JWTs, keys, e-mails, addresses, host names, home paths, long hex).
- CI: actions pinned by SHA, `permissions: contents: read`, `persist-credentials: false`, no `pull_request_target`, no event
  text in `run:`. The bootstrap install checks its own SHA-256 and pins uv and gh by checksum; tarballs unpack with the data
  filter.

## Scanners

- bandit 1.x (in the pinned quality image): no new high finding; the B310 (urlopen) hits open fixed https URLs or the
  configured Ollama host, B202 is the data-filtered unpack, B324 is SHA-1 for ids.
- pip-audit over requirements.txt and requirements-dev.txt: no known vulnerability.
- gitleaks over the whole history: 8 hits, all the scrubber's fake test secrets (L13).
- semgrep could not run here (its Python package fails to import `pkg_resources` on Python 3.12); not repeated.
