# Welcome back

When you come back after being away, Tutti tells you in plain words what happened since you left. It is **on for every
user by default**; each user turns their own off (the summary's *Turn off* button, `POST /api/welcome {"action": "off"}`,
or `tutti welcome off`), and `welcome.enabled: false` in the config makes off the default.

## When you count as back

Tutti uses the signals it already has (`tutti/welcome.py`):

- **the owner's effective mode** (`tutti/owner.py`): the first prompt after a quiet stretch, the end of a scheduled away
  time or a vacation;
- **presence** (`tutti/presence.py`): the owner's laptop or main machine (`presence.owner_laptop`) present again;
- **the first dashboard visit** after a gap, for every signed-in user.

Away means gone for at least `welcome.away_hours` (default: `pacing.away_after_hours`, 3). Each return makes **one**
summary, covering the time since you left.

## What it says

Newest first, grouped by project priority:

1. **Shipped**: releases, merged pull requests, finished tasks.
2. **Running now**.
3. **Failed or parked**, and why (a failed attempt's class and cause, when AI assist read it).
4. **Usage**: where each account is on track to end by its reset.
5. **Needs you**: a numbered list of only what only you can do (a decision on a parked task or on a larger parameter
   change AI assist proposed, a sign-in, a sudo line, a tap on a phone, a usage reading Tutti cannot take itself). Each
   has its exact step and a link to the place that does it. A model never writes this list.

A local model writes the opening words when AI assist allows local AI (`welcome.ai`, default on); the template writes
them otherwise, at once, and the model's words replace them when they are ready (the page is never kept waiting).

## Who sees what

Each item is the admin's (the owner's projects, accounts and setup), the household's (shared machines' work) or one
user's own. A viewer sees their own and the household's; an admin sees everything, including what Tutti noticed about
its owner.

## Where it shows

- **The dashboard**: a *Welcome back* panel above the cards, shown once per return (*Got it* hides it), and
  **What did I miss?** in the top bar, which makes a summary since your last one at any time. Tutti has no Chat tab yet;
  when it does, the same summary (`GET /api/welcome`) is its first message.
- **Your main agent**: the MCP tool `welcome_summary` (a viewer's agent, `TUTTI_ROLE` or `mcp.role`, gets the viewer's view).
- **The terminal**: `tutti welcome [show|miss|on|off]`.
