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
| Cursor Security Review / Bugbot | review comments by `cursor[bot]` | `gh api repos/<o>/<r>/pulls/<n>/comments` for the open pull requests only (current comments, at most 30 pull requests), kept when the comment reads as a security finding; the severity is only a leading label |
| Cursor vulnerability scans (dashboard) | the Vulnerability Remediation API, `GET https://api.cursor.com/security/findings` (Teams and Enterprise, an API key) | the owner saves its JSON to a file named in `autopilot.repos.<n>.reports`; Tutti reads the file and never holds the key (see Dashboard reports) |
| Greptile | review comments by `greptile-apps[bot]`; no API, webhook, export or email digest for its reports | the same comments, same filter; a `reports` entry for it is a config error that says so |

The repo's GitHub name is `autopilot.repos.<name>.github`, else its `origin` remote. Bots and scanners are config
(`depbots.bots`, `depbots.scanners`: name, authors, branch prefixes, a title regex), so another bot is a config entry.

## What becomes a task

- A major, grouped or failing bump: one task, `Dependency update: <title> (#n)`. Its note tells the worker to bring the
  bot's branch in (`git fetch origin <branch> && git diff HEAD...FETCH_HEAD | git apply --3way --index`), run the
  check and fix what breaks (code, lockfile, override, pin). The result lands by the repo's `land` policy and review;
  the bot closes its own pull request once the bump is on the base.
- Green patch and minor bumps: one task per repo, `Land the open patch and minor dependency bumps`, listing them all.
- Reports: deduped per package (Dependabot), rule and file (code scanning), Cursor's finding ID for a dashboard finding, else file and first line
  (Cursor, Greptile: a dashboard finding without an ID and its review comment are one task),
  ranked critical, high, medium, low, queued worst first as `Fix <severity> vulnerability: <title>`.
- A queued task whose pull request closed or whose alert GitHub marked fixed is dropped by itself, only by a read that
  started after it was queued, and only when the
  source that made it was read in full: an unreadable or capped source (403, a bad answer, 500+ open pull requests,
  more than 30 open pull requests or a full page of comments) keeps its tasks. Reports are ranked worst first (`rank`). GitHub closes a
  Dependabot or code-scanning alert when the fix reaches the default branch.

The autopilot reads every `depbots.every_minutes` (default 360) while it is on; `tutti security scan` reads now and
`tutti security` prints the last read. The dashboard's **Dependencies and security** card shows the counts, the worst
severity, the top reports, the bot pull requests and links to each repo's pull requests and Security tab.

## What needs the owner

- Dependabot alerts off, or `gh` without the `security_events` scope: the card names the repo and links
  `https://github.com/<o>/<r>/settings/security_analysis`, or says to run `gh auth refresh -s security_events`.
- Code scanning not set up is not an error; the Security tab link leads to its setup.
- Cursor's and Greptile's reviews appear only where their GitHub apps are installed on the repository.

## Dashboard reports

What each scanner offers for its dashboard reports (checked 2026-11):

| Scanner | API | Webhook | Export | Email digest | What Tutti reads |
| --- | --- | --- | --- | --- | --- |
| Cursor vulnerability scans | yes: `GET /security/findings` (Basic auth with a team API key, `nextCursor` pages) | not for findings | Bugbot's dashboard CSV (usage and per pull request counts, no findings) | no | the API's JSON, saved by the owner |
| Cursor Bugbot, Security Review | no: their findings are pull request review comments | no | the same CSV | no | the review comments |
| Greptile | no: its API indexes and queries code | no | no | no | the review comments |

The owner saves the findings, for example on a timer of their own (the key stays in the owner's environment, never in
Tutti's config):

```bash
curl --get https://api.cursor.com/security/findings -u "$CURSOR_API_KEY:" --data-urlencode limit=100 > ~/security/cursor-app.json
```

and names the file per repo:

```yaml
autopilot:
  repos:
    app: {path: ~/src/app, check: make check, reports: [{scanner: cursor, path: ~/security/cursor-app.json}]}
```

Each read takes the file's findings (a list, or `{findings: [...]}`): findings of another repository (`repository`), and
fixed, dismissed or agent-refuted ones (`status`) are left out. The severity maps to the common scale (critical, high,
medium or moderate, low or warning, info or note); the key is `cursor:<findingId>`, else `scan:<file>:<title>`. A file
that is missing or not findings (an error page) is a problem on the card and keeps the report's tasks; a file read
without a finding drops them, as a fixed alert does. A scanner with no report to read (Greptile) is refused in
`reports` with the reason, and its pull request comments stay its report.
