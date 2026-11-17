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

### The owner's fleet

`tests/e2e/test_owner_fleet.py` runs `serve.py` with `TUTTI_E2E_FLEET=owner`: the shapes of the owner's own dashboard
(2026-11-16 audit) - seven machines, four accounts at the limit at 97-100 %, eight jobs of which three stalled, long routing
reasons, and a config edit that does not parse. Every tab and view (Chat, Dashboard, Settings, Users, Household, What did I
miss?) at 390, 820, 1440 and 3840 px, light and dark:

- the layout checks above, plus `headlinesFit` (no tile value or card title over two lines; a tile note under 140
  characters: the long explanation goes in the tile's *Details*) and `copyLint` (no leaked `null`/`undefined`/`NaN`, no
  `: .`, no "1 accounts" or "1 account need", no "3 stalled: open its", no "(s)" plurals: say the count in words);
- axe, no console or script error, the keyboard reaching the tiles' *Details* with a visible focus;
- the error and loading states: the config-error banner names the file, the problem with its line and column, and the fix;
  Settings shows the same words instead of "Failed to fetch", and "Loading the settings…" before its answer.

## Live self-test and learning loop

The mock suite only finds what its fixtures show. `tutti selftest live` runs the same checks against this computer's own
running dashboard (127.0.0.1:<dashboard.port>) with its real data, and turns each new finding into a mock test.

- **Who:** a read-only identity, `tutti-selftest`, that Tutti mints in its own users file as a *viewer*, with a one-hour
  session dropped at the end of the run. Never the owner's Google login. It is refused while no admin has signed in (the
  first sign-in becomes the admin and must be a person). The session id reaches the browser through the environment only.
- **Read-only, twice:** a viewer cannot change anything (the server refuses it), and `tests/e2e_live/live.py` aborts every
  request that is not a GET before it leaves the browser. It only navigates, switches Me/Household and opens disclosures.
- **What:** every tab a viewer sees and the Household view, at phone, tablet, laptop and 4K, light and dark: `checks.js`
  (layout, clipped text, headlines, copy), axe at phone and laptop, console errors.
- **Learning:** a finding not seen before (its signature: view, check and text with the numbers taken out) is recorded under
  `<state>/selftest/` with a screenshot; the `/api/state` JSON it was drawn from, with e-mail addresses and token-like strings
  redacted, becomes `tests/e2e/fixtures/live-<id>.json`; `tests/e2e/test_live_fixtures.py` replays it in the mock dashboard
  (its `/api/state` answers that JSON) and fails while the bug reproduces; and a backlog task asks for the fix with that
  test as its check, so the autopilot fixes it through the normal pull-request flow. A repeat is counted, never filed again.
- `tutti selftest last` prints the last run (checked views, findings, new and repeat ids, tasks filed).

```bash
tutti selftest live --python ~/.cache/tutti/check-venv/bin/python3   # the venv scripts/check.sh makes has Playwright and axe
tutti selftest live --widths phone,laptop                             # a quicker round
```

Not yet wired: the hourly run when the machine is idle and after a restart, and the dashboard's *Self-test* card and its
line in What did I miss?; both will read `selftest.last()` (follow-ups in the pull request that added this).

## The formatter and other pure parts

`web/format.js`, `web/attention.js`, `web/energy.js`, `web/household.js` and `web/settings.js` are pure enough to run in
node: their unit tests (`tests/test_dashui.py`, `tests/test_layout.py`, `tests/test_energy.py`, ...) load them with
`format.js` first and pin `LC_ALL=en_US.UTF-8` where a number's form matters.
