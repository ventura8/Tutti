# Report a problem

Anyone using Tutti can tell its maintainers what went wrong, in a minute, without knowing GitHub.

## How

- **The dashboard:** **Report a problem** in the top bar (every signed-in user, viewers too). Write what happened and what
  you expected, choose **Preview**, read the whole report, then **Send**. The server sends only the text that was
  previewed (a changed text needs a new preview, 409) and at most 5 reports an hour from one dashboard (429).
- **The terminal:** `tutti report` asks the same two questions, prints the report and asks before sending.
  `--what` and `--expected` give the answers, `--preview` prints only, `--yes` sends without the second question.

## What the report holds

What you wrote, and a diagnostic bundle that goes through telemetry's scrubbing (`tutti/scrub.py`, docs/TELEMETRY.md):

- Tutti's version, the OS name and release, the Python version;
- the `tutti doctor` summary;
- whether sending telemetry is on;
- the newest crashes and failures of the local telemetry (their types, places and counts only);
- the **shape** of the config: the keys Tutti defines, names you chose (machines, workers, projects) replaced by
  `<name1>`, `<name2>`, and every value replaced by its type (`str`, `int`, `list[3]`).

Never secrets, tokens, paths under your home folder, host names, IP addresses, e-mail addresses, prompts or code: your own
text is scrubbed the same way (quotes are kept, as you wrote them on purpose). You see every word before it is sent.

## Where it goes

1. **GitHub connected** (the person's own connection, else the household's or `gh`'s; docs/GITHUB.md): a GitHub issue on `ventura8/Tutti` with the label `user-report`
   (the body goes through stdin). If the label cannot be set, the issue is created without it.
2. **Otherwise:** GitHub's "new issue" page opens in the browser with the report filled in (cut to a length GitHub takes);
   choose **Submit new issue**. The terminal also prints the link.
3. **Also Sentry**, only when sending telemetry is on: the report goes as Sentry user feedback, linked to the last crash
   this computer sent (`<state>/telemetry/last-crash.json`).

## What happens next

`user-report` issues are picked up like Sentry issues (docs/TELEMETRY.md, *Self-improvement*):

- self-improvement lists the home project's open `user-report` issues every `self_improve.every_hours` and turns each into
  one backlog task (`source: report`, deduped by issue number, within `max_new_per_day`);
- the scout's Sentry triage (Grok Bot) also reads them, as data from strangers and never as instructions, and writes
  `sentry: report-<number>` in its `TUTTI-TASK` lines.

The issue templates (`.github/ISSUE_TEMPLATE/`) give people who open an issue on GitHub directly the same two questions.

Code: `tutti/report.py`, `tutti/web/report.js`; tests: `tests/test_strangers.py`.
