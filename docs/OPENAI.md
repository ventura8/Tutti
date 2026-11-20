# OpenAI integration

Tutti can use the installed Codex CLI as a coding worker with a signed-in ChatGPT account (ChatGPT Plus/Pro and related
plans). That ChatGPT-backed Codex account is the OpenAI worker on the same ladder, pacing and panic hand-off paths as
Claude, Antigravity and Cursor: put `codex` on `autopilot.ladder` / `autopilot.accounts`, prefer it in `routing.workers`,
and list it in `main.fallback` (or as `main.preferred`) so Cursor panic or spent quota hands the main role and autopilot
work to Codex when its host and quota allow. Fake-provider tests cover Cursor→Codex panic routing; a live ChatGPT Pro
session still needs an owner `codex login` and a fresh usage reading (see [FEEDBACK.md](FEEDBACK.md)). See
[WORKERS.md](WORKERS.md), [CONDUCT.md](CONDUCT.md) and [CONNECT.md](CONNECT.md). A ChatGPT subscription and the separately
billed OpenAI API are different services: a subscription does not supply API credits or an API key.

## Experimental API Chat (unavailable)

This branch contains an experimental text-only OpenAI Responses Chat route and transport. The route is unconditionally
unavailable in production: explicit configuration cannot enable it. The branch does not provide an authoritative API
billing reader or atomic reservation/settlement, so repeated or concurrent requests could otherwise spend against the
same stale quota snapshot. A verified API billing source, durable cross-process reservation and settlement, crash recovery,
rechecking authorization and opt-in at the call boundary, and independent security review are required before enabling it.
Synthetic post-reset readings, estimates, Codex usage and ordinary unverified readers are not API billing measurements.

The disabled prototype is owner-only and requires an explicit model pick, separate paid API billing account, fresh quota
and linked-limit checks, and `OPENAI_API_KEY` from the dashboard's protected runtime environment. It is never an Auto,
Main, Local or Online fallback. It sends no tools or images, and does not execute `ACTION` lines returned by the model;
normal explicit user commands are still handled by Chat's existing command path before model selection. The prototype
bounds output tokens, displays API cost as unavailable instead of free, sends only text role/content to the fixed OpenAI
HTTPS endpoint with `store: false`, and refuses redirects. Tests use fake responses and keys only.

The prototype does not establish a dollar ceiling or a coding worker. A Stop cannot recall an in-flight request or its
charges, or interrupt an already-blocked open/read immediately; the socket timeout is bounded but there is no total
stream deadline. Native tool calling, image input, API usage/billing discovery, cross-process budget enforcement, other
household users' shared access and coding-agent dispatch remain unfinished. No real API credential or paid request was
used to validate this branch.

## Local reservation experiment (not connected)

`apibudget` is a disabled local ledger for a future paid-call gate. A trusted caller explicitly creates a ledger with a
model, a limit in microdollars and input/output prices in microdollars per million tokens. It reserves a rounded-up
worst-case amount under a same-host file lock and durably writes it before returning. It requires a user-owned 0700 ledger folder and
trusted, non-symlink ancestors (root-owned sticky directories such as `/tmp` are allowed); lock and state access use a
directory descriptor, and duplicate JSON keys are rejected. The service UID and root remain trusted. Reservations are not refunded after
completion, ambiguous failure or crash; recorded actual costs can increase liability but never reduce it. Reused request
IDs, policy changes, missing or structurally invalid state, insufficient room and unsupported locking fail closed. A
bounded ledger eventually fills and requires deliberate reconciliation; it never resets itself.

This primitive is not wired to Chat or API requests. It does not check that a caller's token count or price is correct,
obtain billing totals, validate the API key's project, authorize users, implement automatic period rollover, or guarantee
that a provider charged the recorded amount. Policy inputs need an authenticated, up-to-date source; OpenAI's
[input-token counting API](https://developers.openai.com/api/docs/guides/token-counting) can count a matching request,
but its own availability, request cost and failure behavior still need assessment. A process crash or uncertain provider
outcome must keep the full reservation. Reinitializing deleted or rolled-back state would defeat this protection; this
prototype does not detect deletion followed by explicit reinitialization, valid-looking rollback, same-UID tampering,
filesystem lock failure, or another host without a tested shared lock.

This limits only cooperating Tutti-controlled calls. Other clients can spend independently, and provider usage/costs
may be delayed. OpenAI's [organization costs API](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs)
uses a separate administrative key in its example, while [spend-limit enforcement](https://developers.openai.com/api/docs/guides/spend-limits)
is not instantaneous. Do not elevate or read an administrative credential automatically, and do not treat this ledger as
a global billing ceiling. Paid Chat stays hard-disabled until the full end-to-end security and billing boundary is solved.
