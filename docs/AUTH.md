# Sign-in with Google, roles and invites

The dashboard can require sign-in with Google. Until an OAuth client ID is set it behaves as before: it serves
`127.0.0.1` only, with no sign-in, and shows a banner that offers `tutti auth setup`. Once a client ID is set, every
page and every `/api/*` route needs a signed-in user, except the sign-in page itself and the app's code and icons.

Code: `tutti/auth.py` (token checks, users, sessions), `tutti/authweb.py` (the HTTP gate), `tutti/invites.py` (invites,
shared with the household), `tutti/authcmd.py` (`tutti auth`), `tutti/web/login.*` and `tutti/web/users.js`.

## Setting it up (the owner, once)

Creating the OAuth client is the owner's step: Tutti never creates it. `tutti auth setup` prints these steps with the
dashboard's real port and origins.

1. **Pick the project.** Open <https://console.cloud.google.com/>. In the top bar, pick a project, or create a small
   dedicated one, such as `tutti-dashboard`. Sign-in costs nothing and needs no billing.
2. **Set up the consent screen.** Open <https://console.cloud.google.com/auth/overview> and choose **Get started**.
   - App name: `Tutti dashboard`. User support e-mail: yours.
   - Audience: **External**. Contact e-mail: yours. Agree, then choose **Create**.
   - Then go to **Audience → Test users → Add users** and add every Google account that will use the dashboard (the
     household's accounts).
   - **Testing** mode is enough. A sign-in-only app (the openid, email and profile scopes) needs no verification.
3. **Create the OAuth client.** Open <https://console.cloud.google.com/apis/credentials/oauthclient> (APIs & Services →
   Credentials → Create credentials → OAuth client ID).
   - Application type: **Web application**. Name: `Tutti dashboard`.
   - **Authorized JavaScript origins**: `http://127.0.0.1:4777` and `http://localhost:4777`, then each entry of
     `dashboard.origins`. Google accepts plain `http` only for localhost; a LAN or household address must be an
     `https://` name, never a bare IP. For example, `tailscale serve` or a reverse proxy on a `https://tutti.<name>`
     address.
   - **Authorized redirect URIs**: none.
   - Choose **Create** and copy the **Client ID**, which ends in `.apps.googleusercontent.com`. No client secret is used:
     the ID-token flow has none. The client ID is public, not a secret.
4. Run `tutti auth setup --client-id <client id>`. On a terminal, `tutti auth setup` asks for it instead. It is saved in
   `<state>/auth.json`. `auth.client_id` in the config works too, and takes precedence.
5. Restart the dashboard and open it. With no users yet, the page shows a single **Sign in with Google to become the
   administrator** button. The first person to sign in is the admin.

## Roles

| Role | Sees | Can change |
| --- | --- | --- |
| pending | a "waiting for approval" page, and Sign out | nothing |
| viewer | the dashboard, read-only | nothing. Settings, the reading helper and the Users card are hidden and refused (403) |
| admin | everything, including the **Users** card | every control, Settings, approving, roles, removing, invites |

- **The first user becomes admin.** This is atomic: the check and the write happen under one `flock` on
  `<state>/auth.lock` plus a thread lock, so two first sign-ins cannot both win, whether from two threads or two
  processes.
- **Later users start as pending** until an admin chooses **Approve**, which makes them a viewer. From there the admin
  can **Make admin** (with a confirm), **Make viewer** or **Remove**.
- **The last admin can be neither demoted nor removed** (409).
- **The server enforces every role.** POST is default-deny: every POST needs an admin, except `/api/auth/logout` and a
  viewer's `/api/telemetry` error or use event (never the consent). A route added later is admin-only without any
  change here. Admin-only GETs are `/api/settings*`, `/settings*`, `/helper` and `/api/auth/users`.
- **The page hides what a viewer cannot use.** It sets `<body data-role="viewer">`. Every `.btn` that is not marked
  `view-ok`, `.more` or `.btn--text` is hidden, along with every element marked `data-admin`. A new admin-only control
  gets `data-admin`.

## Invites

Under **Users**, an admin chooses **Invite someone** (viewer by default, or admin). The dashboard shows the link once,
with a QR code (`web/qr.js`, drawn on the page) and the household command.

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
- **The session.** The server then sets `tutti_session_<port>`, a random 256-bit id: HttpOnly, SameSite=Strict, Path=/,
  and `Secure` when reached over https.
  - Sessions live in `<state>/sessions.json` (mode 600), keyed by the id's SHA-256, so the file holds no usable cookie.
  - Each session has its own CSRF token and an absolute expiry, `auth.session_hours`, by default 72.
  - **Sign out** deletes the session and sends `Clear-Site-Data: "cache"`. A removed user's sessions end at once.
- **CSRF.** Every POST must come from the page itself (`Origin`, and `Sec-Fetch-Site: same-origin`), be JSON, and carry
  the session's CSRF token in `X-Tutti-Token`. The page gets the token in `<meta name="tutti-token">`; without sign-in
  this is the per-start token, as before.
- **Caching.** Signed-in pages and every `/api/*` answer are `Cache-Control: no-store`. The service worker never caches
  `/api/`, never caches a `no-store` page, and no longer precaches the page itself.

## Reaching it from other computers

- `dashboard.bind` (default `127.0.0.1`) and `dashboard.origins` (full `https://` origins, for a proxy) let others reach
  the dashboard. With either one set, the dashboard **refuses to start without sign-in**.
- The `Host` must be a loopback name on the port, or the host of an entry in `dashboard.origins` (DNS rebinding stays
  refused).

## Recovery from a shell (needs shell access on the host)

```bash
tutti auth status            # on or off, the origins to authorize, users and live invites
tutti auth users             # role, e-mail, last seen
tutti auth promote <email>   # make a user who has signed in once an admin
tutti auth reset-admin --yes # forget every user and session: the next sign-in becomes admin (sign in at once)
```

## Storage and privacy

- **Mode 600.** `auth.json` (the client ID), `users.json` (sub, e-mail, name, role, created, last seen),
  `sessions.json`, `invites.json` and `auth.lock` are all mode 600 in the state folder.
- **No tokens or e-mails in logs.** Tutti logs no token and no e-mail. `tutti auth promote` prints a shortened address
  (`a…@example.com`).
- **Telemetry.** Crash reports are scrubbed (`tutti/scrub.py` replaces e-mails, JWTs and long tokens) and are sent only
  with consent.

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
- **The Users card.** If `household.members_by_sub(state_dir) -> {sub: {joined, last_seen}}` exists, the Users card shows
  "Tutti joined / last seen" for each user.

## Threat model, in short

- **The first sign-in claims admin.** Before anyone has signed in, whoever reaches the page first becomes admin. Keep the
  dashboard on `127.0.0.1` until you have signed in. The refusal to start exposed without sign-in protects this window,
  but not a proxy you set up yourself before signing in.
- **Plain http on loopback.** The cookie is not `Secure` there. That is acceptable on the same computer, but use
  `https://` origins for anything else.
- **Changing the client ID.** The dashboard reads the client ID when it starts: after `tutti auth setup`, restart it.
