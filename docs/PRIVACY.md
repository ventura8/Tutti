# Tutti privacy policy

Last updated: 2026-11-14.

Tutti is free, open-source software (MIT licence) that you install and run on your own computers. There is no Tutti
server and no Tutti account. This page says what happens to your data.

## Sign-in with Google

The Tutti dashboard offers **Sign in with Google** so that the people in your household can use it and one of them is
the administrator.

- Tutti asks Google only for the `openid` and `email` scopes. It receives your **e-mail address** and your **Google
  account id** (the "subject"). It does not receive your name, contacts, files, calendar or anything else in your Google
  account, and it never sees your Google password.
- Your e-mail address and Google account id are stored **only on the household's own computer** that runs the dashboard,
  in Tutti's state folder, readable by that computer's user only. They are used for one thing: to let you in with the
  right role (administrator, viewer or waiting for approval).
- **Nothing leaves your home network.** Sign-in data is never sent to the Tutti project, to its maintainers or to any
  third party. The only outside service involved is Google itself, which checks who you are.
- The dashboard is meant for the computer itself, your home network (LAN) or your own VPN (for example a Tailscale name
  that only your devices can reach). Tutti refuses to listen on a public address.
- To remove your data, ask your household's administrator to remove you on the **Users** tab, or delete Tutti's state
  folder. You can also remove Tutti's access at <https://myaccount.google.com/permissions>.

The sign-in clients belong to the Tutti project's Google Cloud project. Google's own privacy policy applies to your Google
account: <https://policies.google.com/privacy>.

## Crash reports and usage statistics (opt-in)

Tutti records how it is used and how it fails **on your computer**. It sends this to the maintainers' Sentry project
**only if you say yes** (asked once; change it any time with `tutti telemetry off` or the dashboard's Insights card).

What is sent: Tutti's version, your operating system's name, command names, durations and outcomes, and error types with
their place in Tutti's code, tied to a random install id. **Never** your prompts, tasks, code, file contents, file paths,
computer or host names, IP addresses, e-mail addresses, tokens or keys: these are removed before anything is stored. The
full list is in [TELEMETRY.md](TELEMETRY.md).

## Reporting a problem

When you choose **Report a problem** or run `tutti report`, you see the whole report before it is sent. It becomes a
public GitHub issue on <https://github.com/ventura8/Tutti>, and, if crash reports are on, a message in the maintainers'
Sentry project. It holds what you wrote and a scrubbed diagnostic summary (version, system name, a health check, recent
error types, the shape of your settings without their values). See [REPORT.md](REPORT.md). Do not write anything private
in the text: GitHub issues are public.

## Children

Tutti is a developer tool and is not directed at children under 13.

## Changes and contact

Changes to this policy are made in the public repository, so every change is visible in its history. Questions:
<https://github.com/ventura8/Tutti/issues>.
