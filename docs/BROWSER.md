# The work browser

The household has **one work browser**: a Chrome on one machine, the **browser machine**. Every browser task that goes
through Tutti runs there, whoever asks: a user's chat request, a usage reading, a sign-in helper, a check, a worker's job
on another machine. No task ever opens a browser on another machine, and Tutti never opens the user's browser by itself
(see *Your own browser* below).

## What the broker is, and is not

The broker is a **policy layer**: scopes, named extractors and the log apply to every task that goes through Tutti. It is
not a security boundary against code that runs as the user Tutti runs as: such a process can reach the DevTools port on
127.0.0.1 (`existing`, `dedicated`) or the `tutti-broker` socket (`isolated`) and speak DevTools directly, cookies
included. In `isolated` mode the boundary that holds is the sandbox around the browser (its own user, no home folders, no
local services); to keep workers' own commands off the browser too, run them as a user outside the `tutti-broker` group.

## Privacy note for the household

Sites signed in on the work browser are **shared by everyone who uses it**: any user or worker whose task opens that site
acts with that sign-in. So the admin chooses the sites (Settings → Work browser → **Sites to sign in to**), and keeps the
list to what the work needs. Tutti never reads or exports cookies, passwords or tokens from the profile; the broker logs
every action without page content or query strings (`<state>/browser-actions.jsonl`).

## The browser machine and the mode

`browser.machine` (empty: the machine Tutti runs on; an admin picks another in Settings). `browser.mode`:

| Mode | Which Chrome | When |
| --- | --- | --- |
| `existing` | the user's own Chrome profile, with the sign-ins it already has. Tutti never starts, restarts or closes it; the user turns on remote debugging once at `chrome://inspect/#remote-debugging` (it listens on 127.0.0.1), and Tutti reads only `<profile>/DevToolsActivePort` to find it | a laptop whose owner wants their own sign-ins used |
| `dedicated` | a Chrome Tutti starts with its own profile (`~/.local/share/tutti/browser`; macOS and Windows: the data folder) and `--remote-debugging-address=127.0.0.1`; refuses to start on the owner's profile | a machine without a sandbox |
| `isolated` | a fresh Chrome used only by Tutti, run as the `tutti-browser` system user in a systemd sandbox, reached on a Unix socket | the always-on machine (the NUC after the move) |

Chrome or Chromium is found on Linux, macOS and Windows (`browser.path`, the usual names and folders, then Playwright's
bundled Chromium); none found, the message names the install command.

## Efficiency

One connection to the one Chrome; each task gets its own tab (background, so the owner's window keeps its focus in
`existing` mode) and the tab is closed when the task ends, also when it fails. At most `browser.max_tabs` (4) tabs at once;
more tasks wait in a queue, first come first served. The connection closes after `browser.idle_minutes` (5) without a
task, and in `dedicated` mode Tutti's own Chrome too (a visible sign-in window after three times as long, when the
dashboard opened it; one opened by `tutti browser sign-in` is the person's to close). A Chrome another Tutti process
started is attached to, never started twice. A task's tab is closed whatever the task did; only a connection that cannot
close it is dropped, and the next task connects again. A sign-in in `dedicated` mode goes first: new tasks wait while the
running ones finish, then the hidden Chrome turns into a visible one.

## The broker

Users and workers drive the work browser only through Tutti's broker (`tutti/browserbroker.py`):

- **Scope per task type**: `usage-reading` may open the usage pages, `sign-in`, `sign-in-check` and `page` the admin's
  sites; a type in `browser.scopes` (`{type: [hosts]}`) replaces its defaults, so an admin can widen or narrow it. An https
  URL on an allowed host or its subdomain passes; anything else is refused before a tab opens, and a page that ends up
  outside the scope returns no content.
- **No task brings its own JavaScript**: a task names an extractor, `text` (the page's visible text, once it has loaded),
  `check` (only whether it loaded and where it ended up) or `usage` (the reading helper's parser, on a usage page). Each is
  Tutti's own read-only script; none reads `document.cookie`, storage or makes requests.
- **Every action logged**: when, who (the signed-in user's email, `worker@<machine>`, or the CLI user), the type, the
  extractor, open, close, take or refused, the site and path (never the query).
- **Files**: in `dedicated` and `isolated` mode the browser saves downloads in one scoped folder
  (`/var/lib/tutti-browser/downloads` when isolated, `<state>/browser-downloads` when dedicated); a worker gets a file
  only by naming it in a task whose type is listed in `browser.file_tasks` (none by default), and only up to
  `browser.max_file_mb` (50 MB; a larger file is refused with the reason). `existing` mode leaves the
  owner's Chrome downloads as they are, and gives no files; file tasks need the browser machine to be the one Tutti runs on.
- **Workers on other machines**: `tutti browser remote <machine> <url> [--type T] [-- <command>]` serves one task type on
  127.0.0.1 with a one-time token while the worker's command runs, forwarded over Tutti's ssh link to 127.0.0.1 on that
  machine (`ssh -R 127.0.0.1:…`). The token and the address travel on standard input into the environment
  (`TUTTI_BROWSER_URL`, `TUTTI_BROWSER_TOKEN`, `TUTTI_BROWSER_TYPE`), never on a command line; the worker POSTs `/page
  {url, extract}` or `/file {name}`. Without a command, a standard-library client runs there and prints the result. The
  DevTools port itself never leaves the browser machine. A browser machine that is not this one is reached through one
  ssh `-L` tunnel per connection, 127.0.0.1 (or a Unix socket) on both ends, closed with the connection.

## Isolated mode (the move to the NUC)

The one-time root setup is **one command** for the owner on that machine, `tutti browser isolate --script | sudo bash`
(the move plan lists it when the work browser moves; `tutti browser isolate` alone writes the script to
`<state>/tutti-browser-setup.sh` and prints `sudo bash <it>`). It creates the `tutti-browser` system user and the `tutti-broker` group (the user Tutti
runs as joins it), installs a small bridge and the unit `tutti-browser.service`, and starts it. The unit:

- runs as `tutti-browser` with `NoNewPrivileges`, `ProtectSystem=strict` (everything read-only except
  `/var/lib/tutti-browser`: the profile and the downloads), `ProtectHome` (no home folders: no repos, no credentials),
  `PrivateTmp`, `PrivateDevices`, the kernel and cgroup protections, no capabilities, and
  `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`;
- reaches no local service: `IPAddressDeny` of loopback, link-local, multicast and the private ranges, except the
  resolver (127.0.0.53) and `browser.allow_local`;
- has no TCP DevTools port: Chrome runs with `--remote-debugging-pipe`, and the bridge offers that pipe on
  `/run/tutti-browser/devtools.sock` (0660, group `tutti-broker`, in a 0711 folder): only members of `tutti-broker` (the
  user Tutti runs as) can connect, and the profile files stay the `tutti-browser` user's own (but see *What the broker is,
  and is not*: a DevTools client can still ask the browser for its cookies);
- needs a real Chrome or Chromium binary under /usr or /opt: `tutti browser isolate` refuses one in a home folder, a snap,
  or a snap wrapper script (Ubuntu's `chromium` package).

Set `browser.mode: isolated` (and `browser.machine` to that machine, or leave it empty when Tutti runs there) before the
move, so the move plan lists the setup command. A sign-in on it: `tutti browser isolate --sign-in
<url>` prints the one command that opens that profile in a visible window as `tutti-browser` for that sign-in, then
starts the service again. The step is listed in docs/MOVE.md.

## Commands and the card

| Where | What |
| --- | --- |
| `tutti browser status` | the browser machine, the mode, the endpoint |
| `tutti browser sign-in <url>` | opens `<url>` in the work browser, in front, for a sign-in |
| `tutti browser sites` | checks each needed site's sign-in |
| `tutti browser read` | reads every usage page with no reader (accounts with `usage_url` and no `reader`) and records it |
| `tutti browser page <url> [--type T]` | one task through the broker: the page's text (the `text` extractor) |
| `tutti browser remote <machine> <url> [--type T] [-- <cmd>]` | a worker on `<machine>` does a task through the broker |
| `tutti browser isolate [--sign-in <url>]` | isolated mode's one-time setup command, or a sign-in on it |
| `tutti browser close` | closes the connection (and Tutti's own Chrome in `dedicated` mode) |
| **Work browser** card | the machine, the mode, open tabs per user or worker, the queue, each site's sign-in with **Sign in to <site> on the work browser**, **Check sign-ins** |
| Account card | **Read on the work browser**, **Sign in to <site> on the work browser** |

Roles: any signed-in user may run tasks and sign-ins for the admin's sites (their tabs carry their email in the log; the
card, which every signed-in user sees, shows the name before the @ and the site only); an admin may do a sign-in for
anyone, since it is shared; only an admin changes the sites, scopes, file task types, machine and mode (Settings).

## Your own browser

Steps that belong to you open only when you click: the dashboard's Google sign-in, consent pages, the household invite,
Tailscale's sign-in (`tutti vpn`), the dashboard link after `tutti setup`, and the reading helper's bookmarklet. Tutti
prints or shows these links and never opens them by itself.

## Audit (0.6.0)

Where Tutti opened or drove a browser before, and now: the reading helper's page script (run by an orchestrator in the
owner's tab) now runs through the broker (`tutti browser read`); the bookmarklet is unchanged (the owner clicks it);
`tutti vpn`, `tutti household join` and the first `tutti setup` opened the owner's browser with `webbrowser.open` and now
print the link; the Google device sign-in is a page the owner opens; the chat and self-improvement do not browse; Tutti's
tests run no browser.
