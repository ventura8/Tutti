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

Configured under `workers:` with `kind: cli` and a command list; `{prompt}` is replaced by the brief as one argument.

- **Cursor**: `cursor-agent -p "<brief>" --trust --output-format text`.
- **Google Antigravity**: `agy -p "<brief>" --print-timeout 600s` (`--mode plan` for a read-only pass). Gemini CLI's
  free tier is closed, so Antigravity's CLI is the Google worker; see [LESSONS.md](LESSONS.md).
- **Claude**: `claude -p "<brief>"`.

A person installs each CLI and signs in once. Tutti never types a credential.

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
otherwise it shows `model missing: <name>` and `ollama pull <name>`.

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
