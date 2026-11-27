# Sign-in with Google, roles and invites

The dashboard can require sign-in with Google. A release of Tutti has its own sign-in built in (below), so a new household
creates nothing in Google Cloud. In a checkout with no client ID it behaves as before: it serves `127.0.0.1` only, with no
sign-in, and shows a banner that offers `tutti auth setup`. Once a client ID is set, every
page and every `/api/*` route needs a signed-in user, except the sign-in page itself and the app's code and icons.

Code: `tutti/auth.py` (token checks, users, sessions), `tutti/authweb.py` (the HTTP gate), `tutti/invites.py` (invites,
shared with the household), `tutti/authcmd.py` (`tutti auth`), `tutti/web/login.*` and `tutti/web/users.js`.

## Tutti's own sign-in (built in; nothing to set up)

Like gh and gcloud, a release carries Tutti's own public OAuth clients from the maintainer's Google project
`tutti-dashboard` (`tutti/official.py`):

| Client | Type | What a release carries |
| --- | --- | --- |
| `Tutti dashboard` | Web application, origins `http://127.0.0.1:4777` and `http://localhost:4777` | the client ID |
| `Tutti dashboard (LAN)` | TVs and Limited Input devices | the client ID, and its installed-app secret (Google documents it as not confidential) |

- **A new household:** install, open the dashboard, choose **Sign in with Google** (on this computer) or scan the QR code
  (from the LAN or a VPN). The first person becomes the administrator. No console, no IDs.
- **Scopes:** `openid` and `email` only (non-sensitive). Tutti receives the e-mail address and Google's subject id, and they
  stay in the household (docs/PRIVACY.md).
- **The secret is never in the repository.** The release workflow (`.github/workflows/release.yml`) rebuilds each
  release's tarball with `tutti/release.json` (git-ignored) holding it, from the Actions secret
  `TUTTI_GOOGLE_DEVICE_SECRET`. `scripts/release-assets.sh` reads it from the environment only and never prints it.
- **Which client wins:** a client the household saved (`tutti auth setup`, or `auth.client_id`) always wins. The built-in
  ones are used only by a release build (one with `release.json`), and the web client only when `dashboard.port` is 4777,
  the port its origins name. `auth.official: false` turns them off. A checkout and the tests have no `release.json`.
- **A release built without the secret** has the button on this computer only; the code from a LAN address then needs the
  household's own device client (the steps below).
- `tutti auth setup` with nothing to add says that sign-in is built in; `tutti auth status` says which clients are Tutti's.

### Publishing the app (the maintainer, once)

Until the app is published, Google lets only its test users sign in. In the `tutti-dashboard` project:

1. Set the secret: `bash scripts/set-device-secret.sh` (hidden input, piped to `gh secret set TUTTI_GOOGLE_DEVICE_SECRET -R
   ventura8/Tutti`), and the default DSN: `gh variable set TUTTI_SENTRY_DSN -R ventura8/Tutti` (paste it; DSNs are public).
2. **Branding** (<https://console.cloud.google.com/auth/branding?project=tutti-dashboard>): app name `Tutti dashboard`,
   support e-mail, homepage `https://ventura8.github.io/Tutti/`, privacy policy
   `https://ventura8.github.io/Tutti/PRIVACY.html`, terms `https://ventura8.github.io/Tutti/TERMS.html`, authorized domain
   `ventura8.github.io`, developer contact e-mail. Save.
3. **Domain ownership:** verify `https://ventura8.github.io/` in Google Search Console (<https://search.google.com/search-console>)
   with the same Google account.
4. **Data access** (<https://console.cloud.google.com/auth/scopes?project=tutti-dashboard>): only `openid` and `.../auth/userinfo.email`.
5. **Audience** (<https://console.cloud.google.com/auth/audience?project=tutti-dashboard>): **Publish app**, confirm
   Production.
6. **Verification centre** (<https://console.cloud.google.com/auth/verification?project=tutti-dashboard>): submit for
   brand verification and answer Google's e-mails. With non-sensitive scopes only, there is no security assessment.

## From the home network or any VPN: the default

Most people reach the home network through their own VPN: the router's VPN, OpenVPN, IPsec, or the ISP's box. Then
they open the dashboard at its LAN address, for example `http://192.168.1.5:4777/`. Google's button cannot work there,
because Google takes plain `http` only on localhost. So any address that is not localhost, and not a private https
name, uses **Google's device flow** by default:

This is the main case, and it also covers a home LAN with no VPN at all.

1. The browser opens the dashboard at its LAN IP, for example `http://192.168.1.5:4777/`.
2. The page shows **Sign in with Google**. A click shows three things:
   - a short code, as text;
   - the link `https://www.google.com/device`;
   - a QR code of Google's link with the code already filled in (`verification_url_complete` when Google gives one).
     The QR is drawn on the page by `web/qr.js`, as for invites, with no third-party service. Only Google's own https
     pages are ever linked or encoded.
   When a code expires, the page gets a new one, and a new QR, by itself (up to six times).
3. The person approves on their phone: they scan the QR, or open the link and enter the code, then sign in with Google.
3. The dashboard's server asks Google, at most once per Google's interval, and gets an ID token. It checks the token like
   the button's: the signature against Google's keys, `iss`, `aud` (the device client), `exp`, and the verified e-mail.
4. The session is created. It goes only to the browser that started the flow. A random id in its HttpOnly, SameSite=Strict flow cookie
   binds them, and another browser polling the same flow is refused.

This needs no origin, no https, and no Tailscale or WireGuard. It needs the second OAuth client, type **TVs and Limited
Input devices** (`tutti/authdevice.py`):

- **Its client secret.** Google requires it for this flow. It is kept in `<state>/auth-device.json` (mode 600), read
  from stdin, never printed, and sent only to Google's token endpoint.
- **Who may connect.** Clients are accepted from any private address: loopback, RFC 1918 from any subnet (a router's VPN
  often hands out another one), `100.64.0.0/10` and `fc00::/7`. A public source address is dropped before its request
  is read.
- **The listening address.** Set `dashboard.bind` to this computer's LAN address. `tutti auth setup` prints it.

## A household's own clients (optional)

A household that wants its own Google project (or runs a build without the built-in clients) creates its own clients:
Tutti never creates them. `tutti auth setup` detects what applies
(localhost, the LAN and any VPN through the device flow, a Tailscale https name), then prints these steps with the
dashboard's real port, its LAN address and its origins. It saves the client IDs with no prompt:

```bash
printf '%s' '<tv client secret>' | tutti auth setup --client-id <web id> --device-client-id <tv id> --device-secret-stdin
```

After that, the only step left is signing in once.

1. **Pick the project.** Open <https://console.cloud.google.com/>. In the top bar, pick a project, or create a small
   dedicated one, such as `tutti-dashboard`. Sign-in costs nothing and needs no billing.
2. **Set up the consent screen.** Open <https://console.cloud.google.com/auth/overview> and choose **Get started**.
   - App name: `Tutti dashboard`. User support e-mail: yours.
   - Audience: **External**. Contact e-mail: yours. Agree, then choose **Create**.
   - Then go to **Audience → Test users → Add users** and add every Google account that will use the dashboard (the
     household's accounts).
   - **Testing** mode is enough. A sign-in-only app (the openid, email and profile scopes) needs no verification.
3. **Create the LAN and VPN client.** Open <https://console.cloud.google.com/apis/credentials/oauthclient>.
   - Application type: **TVs and Limited Input devices**. Name: `Tutti dashboard (LAN)`.
   - Choose **Create**, then copy the **Client ID** and the **Client secret**.
   - It needs no origins and no redirect URIs.
4. **Create the OAuth client for this computer.** Open <https://console.cloud.google.com/apis/credentials/oauthclient> (APIs & Services →
   Credentials → Create credentials → OAuth client ID).
   - Application type: **Web application**. Name: `Tutti dashboard`.
   - **Authorized JavaScript origins**: `http://127.0.0.1:4777` and `http://localhost:4777`, then each entry of
     `dashboard.origins`. Google accepts plain `http` only for localhost, so a LAN or VPN address must be an `https://`
     name, never a bare IP, such as a `tailscale serve` name. Never list a public host: the dashboard is reached only
     on this computer, the LAN or the VPN (see below).
   - **Authorized redirect URIs**: none.
   - Choose **Create** and copy the **Client ID**, which ends in `.apps.googleusercontent.com`. No client secret is used:
     the ID-token flow has none. The client ID is public, not a secret.
5. Save both clients with the command above. On a terminal, `tutti auth setup` asks for what is missing instead.
   - The web client ID is saved in `<state>/auth.json`. `auth.client_id` in the config works too, and takes
     precedence.
   - The device client is saved in `<state>/auth-device.json`.
   - Either client alone also works: with only the device client, every address uses the code.
6. Restart the dashboard and open it. With no users yet, the page shows a single **Sign in with Google to become the
   administrator** button. The first person to sign in is the admin.

## Roles

| Role | Sees | Can change |
| --- | --- | --- |
| pending | a "waiting for approval" page, and Sign out | nothing |
| viewer | the dashboard, read-only | nothing. Settings, the reading helper and the Users tab are hidden and refused (403) |
| admin | everything, including the admin-only **Users** tab | every control, Settings, approving, roles, removing, invites, acting for others |

- **The first user becomes admin.** This is atomic: the check and the write happen under one `flock` on
  `<state>/auth.lock` plus a thread lock, so two first sign-ins cannot both win, whether from two threads or two
  processes.
- **Later users start as pending** until an admin chooses **Approve**, which makes them a viewer. From there the admin
  can **Make admin** (with a confirm), **Make viewer** or **Remove**.
- **The last admin can be neither demoted nor removed** (409). An admin may demote themselves while another admin
  exists. Every change takes one click and a confirm, and the server enforces it.
- **The audit log.** Every approval, role change, removal, invite and revoke goes into `<state>/auth-audit.jsonl`
  (mode 600): when, who (sub and e-mail), what, to whom and which role. The Users tab shows the latest 50 under
  **Changes**. `tutti auth promote` is logged as done from the shell.
- **The server enforces every role.** POST is default-deny: every POST needs an admin, except `/api/auth/logout` and a
  viewer's `/api/telemetry` error or use event (never the consent). A route added later is admin-only without any
  change here. Admin-only GETs are `/api/settings*`, `/settings*`, `/helper` and `/api/auth/users`.
- **The page hides what a viewer cannot use.** It sets `<body data-role="viewer">`. Every `.btn` that is not marked
  `view-ok`, `.more` or `.btn--text` is hidden, along with every element marked `data-admin`. A new admin-only control
  gets `data-admin`.

## What a viewer's page shows

- **One rule hides every control for a viewer.** A single CSS rule in `web/app.css`, `body[data-role='viewer']
  :is(button, input, select, textarea, form, .btn, a.link, [data-admin], ...)`, hides every control unless it is
  viewer-safe: it only shows, filters or navigates (`.view-ok`, Show more, the Me / Household switch, the job filters,
  Refresh, Install app and Sign out).
- **A new control is hidden by default.** Marking one `.view-ok` is a deliberate change, which
  `tests/test_auth.py` (`ViewerPageTest`) checks.
- **What disappears.** Pause and resume, opt-ins, the autopilot and fill_room switches, Add a reading, Refresh readings,
  stop now, Move, Update now, away mode and the working-day editor of the owner, autocompact, Connect and Test, the
  device buttons, the Settings tab and view, the Users tab, Restart and every fix button.
- **The label.** The header shows **Viewer: read-only**.
- **Checked in a browser.** A viewer's page showed 9 controls, all read-only; an admin's showed 65 of 68.

## My schedule (every user, viewers included)

Every signed-in user changes their own working days and hours and their own away or vacation mode in **My schedule**,
in the Me view. Their own main agent is paced by them.

- **The API.** `GET /api/me/schedule` returns one's own schedule. `POST /api/me/schedule` takes
  `{days, window, hours, mode}` and is checked with the rules of `pacing.schedule` and `pacing.mode`.
- **Whose schedule.** A viewer may change only their own: another `sub` gets 403. An admin may send `"sub"` to change
  anyone's, and that change is audited.
- **Storage.** `<state>/schedules.json` (mode 600), per Google `sub`, over the config's `pacing` defaults.
- **The household.** It reads a member's schedule with `myschedule.read(state_dir, sub, defaults)` (`tutti/myschedule.py`)
  for that member's per-user pacing.

## The Users tab (admins only)

The **Users** tab sits next to Dashboard and Settings, and viewers never see it. It holds:

- the people who signed in, with their role and when they were last seen, and Approve, Make admin, Make viewer and
  Remove;
- **Invite someone**;
- the **Changes** audit log.

The household's per-user **Manage** sections (consumption, schedule, main agent and machine, help actions) go into its
`#users-manage` container and call `auth.may_act_for`. A viewer's own **My schedule** stays in the Me view.

## Invites

Under **Users**, an admin chooses **Invite someone** (viewer by default, or admin). The dashboard shows the link once,
with a QR code (`web/qr.js`, drawn on the page) and the household command.

- **A bad invite.** An expired, used or revoked invite fails the sign-in only for a new or pending user. The first admin
  and an approved user sign in anyway.
- **Revoke.** An invite stays revocable while any of its purposes is unused, so a household join can be cut off after
  the sign-in.
- **The household side.** The household's increment 4 wires `tutti household join <invite-url>` to this interface. Until
  it lands, the command on the invite page is for the next release.
- **The link** is `<origin>/invite#<token>`. `<origin>` is the first entry of `dashboard.origins`, so that others can
  reach it, else the page's own origin. The token is in the fragment, so it never reaches a server log, a proxy or a
  `Referer`.
- **Storage.** Only its SHA-256 is stored, in `<state>/invites.json` (mode 600).
- **Lifetime.** It lasts **7 days**, can be **revoked**, and is **single-use per purpose**.
- **Signing in.** A person who opens the link and signs in with Google gets the invite's role at once, with no pending
  step. The redemption and the role change happen under one lock.
- **The invite page shows three things:**
  1. Sign in.
  2. One command to copy, `tutti household join <invite-url>`.
  3. A link to install Tutti.

## Sessions and CSRF

- **Signing in.** The sign-in page sets an HttpOnly, SameSite=Strict login cookie that holds a random secret. The page
  hands Google its SHA-256 as the `nonce`. The server checks the ID token:
  - the RS256 signature, against Google's JWKS, which is cached for its `max-age` (an unknown `kid` fetches again at
    most once a minute; keys under 2048 bits are refused);
  - `iss` (`accounts.google.com`), `aud` (the client ID), `exp` and `iat` (60 s of skew allowed);
  - the nonce, against this browser's login cookie;
  - `email_verified`.
- **The session.** The server then sets `tutti_session_<port>`, a random 256-bit id: HttpOnly, SameSite=Strict, Path=/.
  - Over https it is also `Secure` and named with the `__Host-` prefix.
  - Over plain http on a LAN IP it is neither: a browser drops a Secure cookie that came over http.
  - CSRF stays enforced either way.
  - Sessions live in `<state>/sessions.json` (mode 600), keyed by the id's SHA-256, so the file holds no usable cookie.
  - Each session has its own CSRF token and an absolute expiry, `auth.session_hours`, by default 72.
  - **Sign out** deletes the session and sends `Clear-Site-Data: "cache"`. A removed user's sessions end at once.
- **CSRF.** Every POST must come from the page itself (`Origin`, and `Sec-Fetch-Site: same-origin`), be JSON, and carry
  the session's CSRF token in `X-Tutti-Token`. The page gets the token in `<meta name="tutti-token">`; without sign-in
  this is the per-start token, as before.
- **Caching.** Signed-in pages and every `/api/*` answer are `Cache-Control: no-store`. The service worker never caches
  `/api/`, never caches a `no-store` page, and no longer precaches the page itself.

## Reaching it from other computers: LAN or VPN only, never the internet

The owner's rule: a household dashboard is never exposed to the internet. People reach it on the LAN or through a VPN
(Tailscale, WireGuard), and the dashboard refuses to start otherwise.

- **`dashboard.bind`** must be a loopback address, or an address of this computer on a private network. Anything else
  is refused with the reason and the fix: a public address, `0.0.0.0` or `::` (every interface, public ones included),
  or a host name. The private networks are:
  - RFC 1918 (`10/8`, `172.16/12`, `192.168/16`);
  - CGNAT and Tailscale (`100.64.0.0/10`);
  - IPv6 unique-local (`fc00::/7`, which WireGuard and most VPNs use);
  - link-local.
- **Google takes plain `http` only for localhost**, so another device needs a private `https` name. Tutti picks it by
  itself:
  1. **On this computer**, `http://localhost:<port>` and `http://127.0.0.1:<port>` work as they are.
  2. **From other devices, with Tailscale** (detected through `tailscale status`), run `tutti auth setup --tailscale`.
     - It runs `tailscale serve --bg --https=443 http://127.0.0.1:<port>`, which gives a real certificate for
       `<host>.<tailnet>.ts.net`. That name resolves only inside the tailnet. Tutti never runs `funnel`.
     - It saves `https://<host>.<tailnet>.ts.net` in `<state>/auth.json`, so it joins the origins to authorize.
     - The dashboard itself keeps listening on 127.0.0.1.
     - The client ID must be set first.
     - A dashboard that runs without sign-in never answers that name, even after it has been saved.
     - If `tailscale serve` asks for permission, run `sudo tailscale set --operator=$USER` once.
  3. **Without Tailscale**, the dashboard is for this computer only. `tutti auth setup` says how to add Tailscale. Google's
     device flow (a code at google.com/device) is not built yet.
- **Every `dashboard.origins` name** must resolve, on this computer, only to such addresses (for example, a Tailscale
  `https://<machine>.<tailnet>.ts.net` name that resolves to `100.x`). A name that resolves to a public address, or
  does not resolve at all (the VPN is down), is refused.
- **The OAuth client's authorized JavaScript origins** list only `http://127.0.0.1:<port>`, `http://localhost:<port>` and
  these LAN or VPN names, never a public host. `tutti auth setup` prints exactly that list and flags any origin that
  breaks the rule.
- **Invite links** use the first `dashboard.origins` entry (the LAN or VPN name), so they work only for someone on the
  VPN.

- `dashboard.bind` (default `127.0.0.1`) and `dashboard.origins` (full `https://` origins, for a proxy; plain `http` is
  accepted only for a loopback name) let others reach
  the dashboard. With either one set, the dashboard **refuses to start without sign-in**.
- The `Host` must be a loopback name on the port, or the host of an entry in `dashboard.origins` (DNS rebinding stays
  refused).

## Recovery from a shell (needs shell access on the host)

```bash
tutti auth status            # on or off, the origins to authorize, users and live invites
tutti auth users             # role, e-mail, last seen
tutti auth promote <email>   # make a user who has signed in once an admin
tutti auth reset-admin --yes # forget every user and session, revoke every invite: the next sign-in becomes admin
```

## Storage and privacy

- **Mode 600.** `auth.json` (the client ID), `users.json` (sub, e-mail, name, role, created, last seen),
  `sessions.json`, `invites.json` and `auth.lock` are all mode 600 in the state folder.
- **No tokens or e-mails in logs.** Tutti logs no token and no e-mail. `tutti auth promote` prints a shortened address
  (`a…@example.com`).
- **Telemetry.** Crash reports are scrubbed (`tutti/scrub.py` replaces e-mails, JWTs and long tokens) and are sent only
  with consent.

## Acting for another user (the admin as the house's technical person)

`auth.may_act_for(actor, target_sub)` is the one check. An admin may act for anyone, so the one technical person in
the house can do every member's action from the dashboard. An approved user may act only for themselves, and a pending
user for nobody.

`auth.act_for(state_dir, actor, target, action, detail=None)` makes that check, raising `AuthError` (answer 403), then
writes "by actor for target: action" to the audit log, which the Users tab shows under **Changes**. `My schedule` uses
it, and the household's **Manage <name>** panel calls it for each action: schedule, main agent, main machine, connect,
pairing, machine use, join and update. `detail` is a short text of what changed, never a secret.

## The interface for the household's handlers

- **`collector.who(headers)`** returns `{sub, email, role}` of the signed-in user, or `None` when sign-in is off (the
  dashboard's owner on this computer, whom the caller treats as admin) or when nobody is signed in.
- **Viewer POST routes.** `POST /api/user/main-machine` and `POST /api/users/manage` are in `authweb.VIEWER_POST`, so a
  viewer reaches them. Each handler checks its own target with `auth.may_act_for`.
- **The audit log.** `auth.audit(state_dir, who, 'household <act>', {'sub': <member>})` records each admin action
  queued for a member. The signature is `audit(folder, actor, action, target=None, role=None, clock=..., detail=None)`.
- **Stable ids for the Users tab.** These ids stay as they are: `#users-view`, `#users-card` (the household moves it into
  its `#users-tab-auth` slot), `#users`, `#invites`, `#audit`, `#invite-role`, `#invite-create` and `#invite-new`.
- **Self-removal.** An admin can never remove themselves (409). They make themselves a viewer while another admin
  exists, or another admin removes them.

## The interface for `tutti move`

`auth.required_origins(cfg, port=None)` returns every authorized JavaScript origin the OAuth client needs on this host:
the two loopback origins on the port, then `dashboard.origins` and the saved Tailscale name. After a move, show its result
on the new host, so the owner can add the new origins to the same OAuth client (the client ID stays the same).
`auth.tailscale_name()` and `tutti auth setup --tailscale` set up the new host's private https name.
`auth.network_errors(cfg)` lists what breaks the LAN or VPN rule (`tutti/netpolicy.py`, which is shared with the
household).

## The interface for the household (`tutti/household.py`)

The household's user model can take its identity from the signed-in user. These are the agreed calls:

- `auth.Users(state_dir).get(sub) -> {sub, email, name, role, created, last_seen} | None`, and `.all()`.
- `invites.Invites(state_dir)`. Each purpose (`signin`, `household`) can be used once.
  - `.check(key, purpose)`: `key` is a token, or for `household` the invite's 16-hex id.
  - `.redeem(key, purpose, sub=None)` records `used: {purpose: at}` and `used_by: {purpose: sub}`.
  - `.signed_in_with(id) -> sub | None` names the Google user who signed in with that invite.
- `invites.parse_url(url) -> (origin, token)`, `invites.invite_id(token)`.
- The join secret:
  - the joiner derives it with `invites.household_secret(token)`;
  - the coordinator derives it with `Invites(state_dir).household_secret_for(id)`;
  - the token itself never travels.
- **The flow.**
  1. `tutti household join <invite-url>` posts `/hh/join` with the id and a proof signed with that secret.
  2. The coordinator answers 428 until someone has signed in with the invite, while the CLI opens the link and polls
     every 3 s for up to 2 minutes.
  3. The coordinator then calls `redeem(id, 'household', sub=signed_in_with(id))`. The member's id is the Google `sub`.
- **The Users tab.** If `household.members_by_sub(state_dir) -> {sub: {joined, last_seen}}` exists, the Users tab shows
  "Tutti joined / last seen" for each user.

## Organizations, teams, budgets and SSO (small businesses)

Small businesses (5–50 people, several teams and projects) can organize users into organizations and teams alongside the
household model.

- **Roles:** `owner` > `admin` > `member` > `viewer` (plus `pending` before approval). Owners and admins manage
  organizations, teams, members, priorities and budgets; only an owner may assign or remove the `owner` role or delete an
  organization. The last owner of an organization is never demoted or removed (409).
- **Teams, priorities and budgets:** Each team declares a `priority` (`1..100`, higher runs first), optional `projects`,
  and optional `budget` limits (`daily_runs`, `monthly_usd`, `max_parallel`).
- **Storage and access control:** Stored mode 600 in `<state>/orgs.json` under `auth.lock`. `GET /api/orgs` returns only
  organizations the signed-in user belongs to (or all for a global admin/owner); `POST /api/orgs` requires org admin/owner
  and records every change in `<state>/auth-audit.jsonl`.
- **CLI and UI:** `tutti org [list|create|delete|member|team|budget]` manages organizations from the shell; the
  dashboard's **Users** tab includes the **Organizations & Teams** card (`tutti/web/orgs.js`).
- **SSO-ready OIDC (`auth.oidc`):** Standard RS256 OIDC identity providers (Okta, Entra ID, Keycloak, Google Workspace,
  etc.) can be configured under `auth.oidc` with `provider`, `issuer`, `jwks_url` (`https://` required), `client_id`,
  `allowed_domains`, and `groups_claim` (default `groups`). When unset, Tutti defaults to Google sign-in.

## Threat model, in short

- **The first sign-in claims admin.** Before anyone has signed in, whoever reaches the page first becomes admin. Keep the
  dashboard on `127.0.0.1` until you have signed in. The refusal to start exposed without sign-in protects this window,
  but not a proxy you set up yourself before signing in.
- **Plain http on loopback.** The cookie is not `Secure` there. That is acceptable on the same computer, but use
  `https://` origins for anything else.
- **Changing the client ID.** The dashboard reads the client ID when it starts: after `tutti auth setup`, restart it.
