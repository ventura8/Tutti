# Run to completion and the needs-input queue

Tutti works through everything it can do (the backlog, the queue, parked tasks once answered, the work queue) within the
pacing and token limits, and asks a person only what only a person can decide.

## It keeps going

- **Retries with backoff.** A failure climbs the ladder at once (another worker or machine); each further failure in a
  row waits 1, 2, 4... minutes, at most 30 (`autopilot.backoff`). The Autopilot card says why a task waits.
- **Watchdog.** A run whose log has not grown for `autopilot.stuck_minutes` (default 30) is ended, with everything it
  started, and counted as a flaky attempt: the same step tries once more, then the ladder moves on.
- **Crashes, restarts, sleep.** Runs from before a power-off are queued again from their worktree and their WIP refs, not
  counted (docs/PRESENCE.md).
- **Never silent.** When every route is blocked, the Autopilot card names why for each step and what Tutti already tried,
  and the one thing left for you (the resolver).

## When a task needs you

A task that needs a decision is parked with **one question**: what, why, the options and a default. The rest of the
work goes on.

- **The autopilot parks a task** (no progress on the strongest step, or too many attempts): *Retry it, start it from
  scratch, or drop it?* Default: retry. The answer queues it again with its tries counted afresh, or drops it.
- **A worker asks**: a line in its own output,
  `TUTTI-QUESTION: <question> || why: <why> || options: a | b || default: a || urgent: yes`, parks only that task (a
  line the brief itself carries is ignored). A run that passes is published for review anyway, with its question as a
  note for the reviewer.

An answer to a task that no longer waits for it (retried by hand, dropped, done) is kept and changes nothing.

Answer in the Chat (`q3 retry`, or `q3` for the default), on the **My questions** card, with `tutti needs answer q3 retry`,
or through your main agent (`pending_questions`, `answer_question`). Free words are passed to the worker as a note.

## When questions reach you

- **In your work hours** (the owner: their mode is active and their laptop present; a signed-in user: their own schedule):
  to your main agent and to a *Questions from Tutti* chat, batched, at most one batch every 30 minutes; an urgent one
  at once.
- **Off work, away or on vacation**: they build up and appear in your welcome-back summary under *Needs you*, one line
  each with its default (docs/WELCOME.md).
- **Get questions outside work hours** (off by default; for people who run their own business): they still reach you,
  once `batch` of them wait (default 3), or at once when one is urgent, and never in your quiet hours (default
  23:00-07:00). Urgent: the worker flagged it (`urgent: yes`), it is about security (a leak, a secret, a vulnerability),
  or it blocks all work (every open task waits for a person).

Change yours on the **My questions** card or with `tutti user notify always|work-hours [--batch N] [--quiet HH:MM-HH:MM]`.
An admin changes a member's from the Users tab (*Questions outside work hours*), or a signed-in user's with
`--for <email>`; the audit log records each change (without sign-in, as the dashboard's owner).

## Who sees what

Each question belongs to a user. An admin sees everyone's; a member only their own (`GET /api/needs`, the card, the Chat,
the main agent's tools, the welcome-back summary).
