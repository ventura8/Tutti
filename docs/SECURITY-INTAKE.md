# Dependency updates and vulnerability reports

`tutti/depbots.py` turns the dependency bots' pull requests and the repositories' vulnerability reports into autopilot
tasks, for every repo under `autopilot.repos`. It reads through the `gh` login already on the machine; Tutti holds no
token of its own and never prints one.

## What is read

| Source | How it is published | How Tutti reads it |
| --- | --- | --- |
| GitHub Dependabot updates | pull requests by `dependabot[bot]` on `dependabot/...` branches | `gh pr list` |
| Cursor's dependency agent (Anybump, cloud agents) | pull requests by `cursor[bot]` on `cursor/...` branches | `gh pr list`, kept when the title or branch reads as a bump, upgrade, CVE or security fix (`depbots.bots[].match`) |
| GitHub Dependabot alerts | the repository's Security tab | `gh api repos/<o>/<r>/dependabot/alerts` |
| Code scanning (CodeQL and others) | the Security tab | `gh api repos/<o>/<r>/code-scanning/alerts` |
| Cursor Security Review / Bugbot | review comments by `cursor[bot]`; there is no alerts API | `gh api repos/<o>/<r>/pulls/comments`, kept when the comment reads as a security finding |
| Greptile | review comments by `greptile-apps[bot]`; Greptile publishes no separate report | the same comments, same filter |

The repo's GitHub name is `autopilot.repos.<name>.github`, else its `origin` remote. Bots and scanners are config
(`depbots.bots`, `depbots.scanners`: name, authors, branch prefixes, a title regex), so another bot is a config entry.

## What becomes a task

- A major, grouped or failing bump: one task, `Dependency update: <title> (#n)`. Its note tells the worker to bring the
  bot's branch in (`git fetch origin <branch> && git diff HEAD...FETCH_HEAD | git apply --3way --index`), run the
  check and fix what breaks (code, lockfile, override, pin). The result lands by the repo's `land` policy and review;
  the bot closes its own pull request once the bump is on the base.
- Green patch and minor bumps: one task per repo, `Land the open patch and minor dependency bumps`, listing them all.
- Reports: deduped per package (Dependabot), rule and file (code scanning) or file and first line (Cursor, Greptile),
  ranked critical, high, medium, low, queued worst first as `Fix <severity> vulnerability: <title>`.
- A queued task whose pull request closed or whose alert GitHub marked fixed is dropped by itself. GitHub closes a
  Dependabot or code-scanning alert when the fix reaches the default branch.

The autopilot reads every `depbots.every_minutes` (default 360) while it is on; `tutti security scan` reads now and
`tutti security` prints the last read. The dashboard's **Dependencies and security** card shows the counts, the worst
severity, the top reports, the bot pull requests and links to each repo's pull requests and Security tab.

## What needs the owner

- Dependabot alerts off, or `gh` without the `security_events` scope: the card names the repo and links
  `https://github.com/<o>/<r>/settings/security_analysis`, or says to run `gh auth refresh -s security_events`.
- Code scanning not set up is not an error; the Security tab link leads to its setup.
- Cursor's and Greptile's reviews appear only where their GitHub apps are installed on the repository.
