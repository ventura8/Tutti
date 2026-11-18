# OpenAI integration

Tutti can use the installed Codex CLI as a coding worker with a signed-in ChatGPT account. See [WORKERS.md](WORKERS.md)
and [CONNECT.md](CONNECT.md). A ChatGPT subscription and the separately billed OpenAI API are different services: a
subscription does not supply API credits or an API key.

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
