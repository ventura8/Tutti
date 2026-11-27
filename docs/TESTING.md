# Testing

`bash scripts/check.sh` runs everything CI runs, in this order, and stops at the first failure:

1. `shellcheck` on the scripts and the installer;
2. `ruff` (CI always has it; locally it is skipped when not installed);
3. the unit tests, `python3 -m unittest discover -s tests`, in a temp `XDG_CONFIG_HOME` (never the owner's `grok.env`);
4. the dashboard's browser tests, `tests/e2e` (below);
5. a CLI smoke test of the example config.

CI (`.github/workflows/ci.yml`) runs `scripts/check.sh` on every pull request and every push to `main`, so each step is a
hard gate. Install the pinned tools once with `python3 -m pip install -r requirements-dev.txt`.

Rules for every test: a change in behaviour comes with a good case and a bad one; nothing runs against a real machine
(tests use the `local` transport and temp folders); hosts are `*.example`.

## Browser tests

`tests/e2e/test_ui.py` opens the dashboard in a real browser: Playwright's Chromium (`playwright==1.65.2`) with axe-core 4.12
(`axe-playwright-python==0.1.8`), both pinned in `requirements-dev.txt`. `check.sh` fetches the browser once
(`playwright install chromium`; on CI `--with-deps`, which adds its system libraries).

```bash
PYTHONPATH=tests python3 -m unittest discover -s tests/e2e                    # the browser tests alone
TUTTI_UI_SHOTS=/tmp/tutti-ui PYTHONPATH=tests python3 -m unittest discover -s tests/e2e   # and keep a screenshot of each view
```

### The world

- `tests/e2e/serve.py` serves the example config (`tutti.config.example.yaml`) from a temp home, in a child process with its
  own `HOME` and `XDG_CONFIG_HOME`. Every probe is stubbed: machines answer with fixed readings, local-model workers are
  down, no reader command runs, no tunnel starts, no process of this host shows as a job.
- It seeds readings with float noise (grok-bot `32.122999999999976`), a running job with a long title, a finished and a
  failed one.
- Sign-in is on, with a **fake Google**: `tests/e2e/harness.py` signs ID tokens with the test key of `tests/test_auth.py`
  and posts them to the dashboard's own `/api/auth/login`, with the login page's nonce. The first user (`owner`) is the
  admin; the second (`guest`) signs in pending and the admin approves them as a viewer. Never a real account.
- The browser reaches only the dashboard: every other request is aborted (Google's script included). Service workers are
  blocked, so each test sees the files on disk.

### What every tab is checked for

The tabs are read from the tab bar, so a new tab (Chat, Users, any other) is covered once it is in the bar. The sign-in
page is checked too.

**Layout**, at phone (360 px), tablet (768), laptop (1440) and 4K (3840) widths, light and dark, for the admin and the
viewer; then at 200 % zoom, and with WCAG 1.4.12 text spacing (line height 1.5, paragraphs 2 em, letters 0.12 em, words
0.16 em):

- no overlap: no two pieces of text, two controls, or a control and text drawn over each other (text measured by its line
  boxes; a bar fixed to the viewport is checked by itself and must not hide the page's end);
- no clipped or cut-off text: a box that hides overflow while its `scrollWidth` or `scrollHeight` is larger than its client
  size, an ellipsis that truncates, a line clamp;
- no horizontal page scroll, and every element inside the viewport's edges (a row meant to scroll sideways, such as the
  accounts on a phone, is checked as one box).

**Accessibility**:

- axe-core with the `wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`, `wcag22aa` and `best-practice` tags, at phone and laptop
  width, in both themes (contrast included);
- keyboard: Tab from the top reaches every visible control, each shows a focus indicator, and focus comes back round (no
  trap); Esc closes an open disclosure and returns the focus to its summary;
- targets at least 24 × 24 px (a link inside a sentence excepted);
- one `main`, one `h1`, a labelled navigation, headings that never skip a level;
- every control labelled (axe), and every icon-only button has a spoken name;
- a live region announces updates (the status line is `aria-live="polite"`);
- reduced motion: nothing animates or transitions;
- a refused form value is announced: `aria-invalid` and a `role="alert"` message tied to the field, cleared once fixed.

**Roles**: a viewer sees the Chat and Dashboard tabs and no admin control (the server refuses `/api/settings` with 403); an
admin sees every admin control, the Settings and Users tabs.

**Numbers**: no number with four or more decimals anywhere on the page.

A failure names the role, tab, width and theme, the element (tag, id, classes, text, card) and the numbers. Fix the UI;
never relax a check.

## Live dashboard self-test

Install the pinned Playwright package and Chromium for the dashboard service's Python environment and user:

```bash
python3 -m pip install playwright==1.65.2
python3 -m playwright install chromium
```

Use the same Python interpreter as the dashboard service. The **Self-test** card shows *Unavailable* if Chromium cannot
start. The active dashboard starts a background run on its first probe round at least one minute after startup and
then once a day. Automatic release updates restart the dashboard. For a custom `ship.deploy` command that updates the
local live instance, restart the dashboard service as the final deploy step. Each restart schedules a fresh run, including
deployments that keep the same version number.

The browser loads the dashboard as a local synthetic viewer. It sends only same-origin GETs, blocks service workers,
never signs in to an account, and never clicks. The server only accepts the process-local permit for the app shell, public
assets, viewer role and read-only state. It issues no session or CSRF token to this viewer.

**What did I miss?** lists live failures against the assertions the signed-in mock browser must pass in CI. Each failing
assertion generates a replay case in `<state>/selftest-fixtures/` and a backlog task deduplicated by assertion. Cases contain
fixed check IDs and diagnostics. Browser output, response bodies, session material and live addresses stay out of the files
and backlog. Replay saved cases alongside the committed cases in the mock suite:

```bash
TUTTI_SELFTEST_FIXTURES=/path/to/selftest-fixtures PYTHONPATH=tests python3 -m unittest discover -s tests/e2e
```

## The formatter and other pure parts

`web/format.js`, `web/attention.js`, `web/energy.js`, `web/household.js` and `web/settings.js` are pure enough to run in
node: their unit tests (`tests/test_dashui.py`, `tests/test_layout.py`, `tests/test_energy.py`, ...) load them with
`format.js` first and pin `LC_ALL=en_US.UTF-8` where a number's form matters.
