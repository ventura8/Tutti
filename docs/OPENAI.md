# OpenAI integration

Tutti can use the installed Codex CLI as a coding worker with a signed-in ChatGPT account. See [WORKERS.md](WORKERS.md)
and [CONNECT.md](CONNECT.md) for setup, routing and Tutti's MCP connection to Codex.

The separate OpenAI API is billed independently of a ChatGPT subscription. The experimental `tutti.openai_api` Responses
transport is an internal prerequisite only: it is not wired into Chat, worker setup, account pacing or dispatch, and is not
an available backend yet. It will not initiate a request unless the caller explicitly sets both `enabled: true` and
`costs_money: true`, supplies a model and supplies a runtime key provider. It does not read keys from files or write them
into configuration. It accepts text messages only, refuses redirects, streams typed text events and usage, and does not
provide tools, images, cancellation of an already-blocked read, or a coding agent. A socket open/read timeout is
bounded to 120 seconds, but there is no total stream deadline and Stop cannot immediately interrupt an already-blocked
open or read. Its test suite uses fake responses and
fake keys; it does not validate a paid live account or actual billing.

Before enabling a backend, Tutti must integrate explicit paid configuration, protected key provisioning, account and
household ownership, quota/pacing, capability and task routing, safe timeouts and cancellation, secure error handling,
and end-to-end behavior. A ChatGPT subscription must not be presented as an API credit or authorization. Until then,
use the existing Codex worker rather than treating this transport as a supported API integration.
