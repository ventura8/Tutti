# Security

## What Tutti handles

Tutti runs commands on machines you configure and briefs agents you installed. It does not store, read, print or send
credentials: ssh uses your own keys and agent, and each worker CLI keeps its own sign-in.

## Your part

- Keep `tutti.config.yaml` free of tokens, passwords and keys. It is plain text.
- Keep secrets out of the copies: list them under `sync.exclude` (the defaults exclude `.env`, `.env.*`, `*.key`, `*.pem`).
- A brief is sent to another vendor's service. Do not put secrets or private data in a task.
- Workers edit files in the worktree you give them. Review the diff before you gate or ship it.

## A household

A household's coordinator listens on the LAN (`household.port`, 4778). Every request is signed with the member's token,
which is derived on both sides from the join code and never sent; requests are not encrypted and carry nothing private.
The threat model and what crosses the network are in [docs/HOUSEHOLD.md](docs/HOUSEHOLD.md#auth-and-the-threat-model).

## The dashboard

Without sign-in the dashboard serves 127.0.0.1 only. With sign-in with Google ([docs/AUTH.md](docs/AUTH.md)) the server
checks the ID token's signature against Google's keys and its `iss`, `aud`, `exp` and nonce; sessions are HttpOnly,
SameSite=Strict cookies with a server-side record and a CSRF token on every POST; roles (pending, viewer, admin) are
enforced on the server for every route. Users, sessions and invites are kept mode 600 in the state folder; the session
file and the invite file hold only hashes. The dashboard refuses to listen beyond loopback, or behind `dashboard.origins`,
without sign-in. Recovery needs shell access on the host (`tutti auth promote`, `tutti auth reset-admin`).

## Reporting a vulnerability

Please do not open a public issue for a security problem. Once the repository is public, use GitHub's private
vulnerability reporting on it: report privately through the repository's **Security → Report a vulnerability** (GitHub private vulnerability reporting). Please do not open a public issue for a security problem.
