# Run to completion and the needs-input queue

The unblocker may answer a parked question only when the model supplies one complete action matching a listed option.
Multiword options work; extra words, multiple action lines, or `person` leave the question open. This does not guarantee
that a model's choice is correct or resolve decisions which require the person (t278).

An automatic answer is applied only if the parked task and its question still match what the model saw. If the question,
options or task history change during the model call, Tutti leaves the decision for a fresh review. The two state files
are protected from concurrent edits by their locks; this does not make their writes a crash-atomic transaction (t278).

Before using a model diagnosis to retry or park a repeatedly failing task, the unblocker checks under the backlog lock that
the task is unchanged since the model saw it. A changed task is left alone and can be reviewed in a later round without
the normal six-hour attempt cooldown (t244). This does not guarantee the diagnosis is correct.

Tutti works through everything it can do (the backlog, the queue, parked tasks once answered, the work queue) within the
pacing and token limits, and asks a person only what only a person can decide.

## It keeps going

- **Retries with backoff.** A failure climbs the ladder at once (another worker or machine); each further failure in a
  row waits 1, 2, 4... minutes, at most 30 (fixed in the code, not a setting). The Autopilot card says why a task waits.
- **Watchdog.** A run with no new log output for `autopilot.stuck_minutes` (default 30; 0 turns it off) shows a hold
  explaining that process ownership is unverified. Tutti does not signal or retry it until its actual report arrives;
  it may occupy a worker slot during that wait. A dead run recorded as local before a machine reboot can be resumed from WIP,
  but an unknown or bridged run remains held because rebooting this host does not establish what happened on the client.
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

Tasks parked before the queue existed get their question at the dashboard's start, or with `tutti needs backfill`: a
local model words it from the task's last failure when AI assist allows local models (docs/TELEMETRY.md), the template
above otherwise; at most 5 per round, the rest in the next.

An answer to a task that no longer waits for it (retried by hand, dropped, done) is kept and changes nothing.

Answer in the Chat (`q3 retry`, or `q3` for the default), on the **My questions** card, with `tutti needs answer q3 retry`,
or through your main agent (`pending_questions`, `answer_question`). Options match in any case. Free words are passed to
the worker as a note, except in the Chat: there `q3` must be followed by one of its options, so a message such as
"q3 looks odd, why?" is answered as a question, not taken as your answer.

## When questions reach you

When an open question changes, Tutti offers the updated question for delivery again. Asking the identical question again does not send a duplicate. A failed older delivery cannot undo the delivery of a newer revision. Delivery still follows the user’s batching and quiet-hour settings.

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

A failing-task diagnosis must give one explicit action: retry needs a concrete fix and person needs a reason. Invalid or incomplete output leaves the task unchanged and waits for the normal retry cooldown; validation does not prove the model's diagnosis is correct (t244).

Automatic answers compare the complete parked task and question revision under their locks; changed task details require a fresh review (t278). The two files are not crash-atomic.
