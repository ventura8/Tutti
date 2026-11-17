# Connect GitHub

Tutti opens pull requests, reads CI, shows releases and files problem reports on GitHub. It used to do all of that through
the GitHub CLI (`gh`) signed in as the dashboard's owner. Now it has its own GitHub connection, so a household without `gh`
works too.

## For a person: Connect GitHub

On the dashboard, the **My GitHub** card (Me view) has **Connect GitHub**:

1. Tutti shows a short code and a QR code.
2. Open `github.com/login/device` on any device (or scan the QR), enter the code and approve **Tutti orchestrator**.
3. The card says *Connected as <your login>*. Choose which repositories Tutti may use with the link under it (GitHub's
   page for installing the App).

From a terminal on the dashboard's computer: `tutti github connect` (the same code), `tutti github status`,
`tutti github disconnect`.

Each person connects for themself; nobody else can use your connection. Your reports from **Report a problem** are filed
under your own GitHub account. Without a connection they go out as before: through the household's connection, else as
a prefilled GitHub page you send yourself, and to the crash reports when sending is on (the anonymous route).

## What Tutti may do with it

Tutti's GitHub App asks only for: contents (to merge a pull request it opened), pull requests, issues, checks and actions
(read only) and metadata (read only). No webhooks: GitHub never calls the dashboard, which stays on your LAN or VPN.
Tutti polls instead, with ETags (a page that did not change costs nothing) and it stops asking when GitHub's rate limit is
spent, until GitHub's reset time.

## Which connection a call uses

1. The person's own connection, for what they do (Report a problem, their card).
2. The household's connection for work that runs for nobody in particular (the autopilot's pull requests and merges,
   the Projects panel's pull requests, CI and releases, `tutti doctor`, the *releases stuck* check). The household's
   connection is the first one made, or `tutti github connect` in a terminal.
3. The GitHub CLI's own sign-in (`gh auth token`) when `gh` is logged in: the old way, kept as a fallback.

`tutti doctor` says when none of them works and what to do.

## The token

- Kept in `<state>/github/<hash>.json`, mode 600, one file per person (the name is a hash, not the person).
- Refreshed by itself before it expires (GitHub App tokens last 8 hours); when the refresh fails the file is removed and
  the card asks you to connect again.
- **Disconnect** removes it; revoke it on GitHub too at `github.com/settings/apps/authorizations`.
- Never written to a log, an error message, a command line or the environment of a worker: workers never act on GitHub
  as a person (`GH_TOKEN`, `GITHUB_TOKEN` and the like are removed from every worker's environment).

## One client (for contributors)

`tutti/github.py` is the only place Tutti talks to GitHub. Callers keep their `gh`-shaped argv (`gh pr create`, `gh pr
merge --match-head-commit`, `gh pr view --json state`, `gh pr ready`, `gh issue create`, `gh auth status`) and pass
`github.run` where they used `subprocess.run`; it answers them with the REST and GraphQL API when a token exists and hands
anything else to the real `gh`. A merge goes through the API only when it is pinned to the reviewed commit
(`--match-head-commit`). Still on `gh` (maintainer features, not needed by a household): self-improvement's reads of a
pull request's gates (`gh pr view --json state,isDraft,...`, review threads over `gh api graphql`) and `tutti pr review`. Tests use a fake API
(`tests/test_github.py`); nothing reaches GitHub.

## The official App (the maintainer, once)

The App is created from a manifest: open `https://ventura8.github.io/Tutti/github-app.html` (or save the output of
`tutti github manifest` as a page and open it), choose **Create the GitHub App on GitHub**, then GitHub's own **Create
GitHub App**. On the new App's page tick **Enable Device Flow**, and put its Client ID into `tutti/official.py`'s release
data (`github_client_id` in `release.json`, written by the release build) or a household's `github.client_id`. If GitHub
asks for the client secret to refresh tokens, generate one and store it as the Actions secret the release build writes
into `release.json` (`github_client_secret`, public-client style like the Google device secret).

Until the App exists, Connect GitHub says so and Tutti keeps using `gh`.

Tests pass `gh=` to `github.run` and `github.api` to replace the CLI token lookup, and `fallback=` to replace
program execution. Cover both an empty and a fake CLI token without invoking an installed `gh` or changing process environment.
