# Scouts

A scout is a helper that is **not a worker**. It never gets a worktree, a diff or a check; it is asked a question and
answers in words. Scouts do what a chat bot with a browser is good at:

- research: compare options, read docs, find the cause of an error message;
- web browsing: open a page and report what is there;
- watching: release notes and changelogs of dependencies, docs pages, competitors;
- triage: read new issues and sort them;
- monitoring: open the live site and report errors;
- drafting: release notes, an issue text, a reply;
- browser bug reproduction: follow steps on a live page and report what happens.

Today's one kind is **Grok Bot** (xAI's chat bot that Cursor Pro includes, with its own weekly allowance). Grok Bot has
no CLI and no API for this: it is reached through its app, Slack, email or a **webhook routine**. Tutti uses the webhook:
a fire-and-forget `POST` to the routine's URL with `Authorization: Bearer <key>`. Grok Bot answers `200` when a run started
(the run is not finished); any other status means no run started. Limits from its docs: 50 routines per bot, 200 runs an
hour per routine, 300 in total; a run over a limit fails with "Automation run rate limit exceeded" and is not retried.

## How a question travels

1. `tutti scout ask grok "<question>"` opens an issue in `reply.github_repo` (label `tutti-scout`) with the question.
2. It POSTs `{task, skill, from, id, reply_to: {repo, issue, url, how}}` to the routine.
3. The routine does the task and posts the result as a comment on that issue (the owner connected GitHub to Grok Bot).
4. Tutti polls the issue through `gh` and prints the first comment made after the question; the issue is then closed.
   With no answer within `timeout_minutes` it exits 3 and says to look again with `tutti scout answer <id>`.

A watch's issue (label `tutti-scout-watch` as well) stays open: it is the orchestrator's inbox.

## Config

```yaml
accounts:
  grok-bot:                       # the weekly meter, read by the reading helper from Cursor's spending page
    window: 7d
    resets: '2026-01-04T00:00Z'
    resets_estimated: true
    usage_url: https://cursor.com/dashboard/spending
    helper_label: Grok Bot
scouts:
  grok:
    kind: grok-bot-webhook
    url_env: TUTTI_GROK_URL       # the NAMES of environment variables; the values never go into a file
    key_env: TUTTI_GROK_KEY
    account: grok-bot
    reply: {github_repo: octo/my-app, issue_label: tutti-scout}
    skills: [research, web-browse, triage, monitoring, drafting]
    timeout_minutes: 20           # how long `ask` waits; poll_seconds (30) between looks
    max_open: 5                   # questions waiting for answers at once
    per_tick: 2                   # sent per round
    give_up_hours: 48             # a question with no answer by then is marked timeout
    auto: true                    # the dashboard's round sends due watches and queued questions
    watches:
      dependency-notes: {every: 7d, task: "Read this week's release notes of ... and summarise breaking changes"}
      live-site: {every: 1d, early: true, task: "Open https://my-app.example and report console errors"}
    review_trial: {repos: [octo/my-app], prs: 5}
```

`tutti config` checks every key and names the fix. The key is read from `key_env`, or with `--key-stdin` from the first
line of stdin; a pasted `Bearer ` prefix is dropped. Neither the URL nor the key is ever written, logged or shown: errors
and the dashboard name only the variables. The URL must be `https://` (plain `http://` only on 127.0.0.1, for tests).

## Commands

| Command | What it does |
| --- | --- |
| `tutti scout ask <scout> "<question>"` | Opens the issue, fires the webhook, waits for the answer and prints it. `--repo` another repository, `--skill` a hint, `--no-wait`, `--timeout <min>`, `--key-stdin`. `--queue` sends nothing now: the next round sends it when the allowance has room. |
| `tutti scout answer <id>` | Looks for (and waits up to `--timeout` for) the answer of a request. |
| `tutti scout watch [list]` | The watches, when each last went and goes next. `tutti scout watch <scout> <watch>` sends one now. |
| `tutti scout tick` | One round: collects answers, then sends what the plan allows. The dashboard runs it after every probe. |
| `tutti scout review <pr>` | The scout reviews a pull request: it posts one comment there whose first line is `grok-scout`, one bullet per finding. Advisory: never blocking, never applied by Tutti. Allowed when `review` is among its skills, during its review trial, or after a trial it passed. |
| `tutti scout trial [status\|collect\|show\|mark\|done]` | The review trial (below). |
| `tutti scout list`, `tutti scout setup` | The scouts and their state; the owner's setup steps. |

## Pacing: use the allowance, never on-demand

The scout's `account` is paced like any other (docs/USAGE-PACING.md). The plan of a round:

- **Nothing** is sent while the account is in panic, spent, or at 100 %: past its allowance Grok Bot either pauses or
  bills on-demand spend, and a scout never causes on-demand spend.
- Due watches first (never sent, or their `every` has passed), then queued questions; at most `per_tick` a round and
  never more than `max_open` waiting.
- A watch with `early: true` goes at half its period while the allowance is **behind** its pace with room: the weekly
  allowance is use-it-or-lose-it.
- A send that failed (a refused key, a paused routine) waits an hour before the next try, so a wrong key never opens an
  issue every round.

Read the meter in one click: the reading helper's bookmarklet for the Cursor spending page reads every account on that
page, Grok Bot's weekly meter included (`helper_label: Grok Bot`).

## The review trial

`review_trial: {repos: [...], prs: 5}` starts a trial: for the next 5 pull requests of those repositories, `tutti pr
review` also asks the scout (`tutti scout review`); a scout's trouble is printed and the review goes on. Per pull request:

1. `tutti scout trial collect owner/name#12`: reads Copilot's and CodeRabbit's inline review comments and the scout's
   bullets into `<state>/review-trial.json`, each finding with an id (`f1`, `f2`, ...).
2. `tutti scout trial show owner/name#12`, then `tutti scout trial mark owner/name#12 f2 real|noise`: real when it was
   fixed, noise when there was nothing to fix.
3. `tutti scout trial done owner/name#12` once every finding is marked.

After the last pull request the verdict is written: when the scout found a real issue no other reviewer raised (none of
theirs on the same file within 5 lines), `review` joins its skills; otherwise it stays a research scout and
`tutti scout review` says so. The dashboard shows the tallies per reviewer and the verdict.

## Set up Grok Bot (the owner, once)

`tutti scout setup` and the dashboard's Scouts card show the same steps:

1. Open the Grok Bot app (Cursor Pro includes it).
2. Create a bot named "Tutti scout" (Only me).
3. In **Connect apps** (sidebar) connect GitHub, with access to the repository in `reply.github_repo` (and the review
   trial's repositories).
4. In the bot's chat ask it to create a routine named "Tutti task" with a **webhook** trigger, whose instructions say: the
   body is JSON with `task` and `reply_to`; do the task; post the full result as one comment on the GitHub issue
   `reply_to.url` (repository `reply_to.repo`, number `reply_to.issue`); never change code, open pull requests or close
   the issue; if the task cannot be done, comment why. For reviews, `reply_to.how` asks for one comment on the pull
   request whose first line is `grok-scout`.
5. Open the routine (the bot's name, **Details**, the routine): its **Webhook** section shows **POST to** and the header.
6. Put the URL in `TUTTI_GROK_URL` and the key (after `Bearer `) in `TUTTI_GROK_KEY`, e.g. in `~/.profile`; never in the
   config, a repository or a chat.
7. Restart the dashboard from a shell that has both variables; test with `tutti scout ask grok "Say hello"`.
8. Optional: read Grok Bot's weekly meter with the reading helper so scouts are paced against it.

Grok Bot's **Test** button runs the routine for real and spends usage.
