# Security

## What Tutti handles

Tutti runs commands on machines you configure and briefs agents you installed. It does not store, read, print or send
credentials: ssh uses your own keys and agent, and each worker CLI keeps its own sign-in.

## Your part

- Keep `tutti.config.yaml` free of tokens, passwords and keys. It is plain text.
- Keep secrets out of the copies: list them under `sync.exclude` (the defaults exclude `.env`, `.env.*`, `*.key`, `*.pem`).
- A brief is sent to another vendor's service. Do not put secrets or private data in a task.
- Workers edit files in the worktree you give them. Review the diff before you gate or ship it.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Once the repository is public, use GitHub's private
vulnerability reporting on it: report privately through the repository's **Security → Report a vulnerability** (GitHub private vulnerability reporting). Please do not open a public issue for a security problem.
