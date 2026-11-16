# The Admin tab

The dashboard's **Admin** tab is for admins only (with sign-in on, an admin role; with sign-in off, this computer's owner).
Like the whole dashboard it is served on this computer, the LAN and a VPN only, never on the public internet. The page reads
`GET /api/admin`, which refuses anyone who is not an admin.

## Costs

One row per provider or account:

- **Subscriptions**: a fixed monthly fee from `costs.subscriptions` (`{claude: {monthly: 90, currency: USD}}`). Month to
  date is the fee times the share of the month gone; the forecast is the fee.
- **Pay-per-use**: what a metered worker (Copilot when enabled, a cloud API key) spent this month, from
  `costs.metered` (`{copilot: {month_to_date: 4.2, currency: USD}}`), copied from its billing page. The forecast is the
  spend so far, scaled to the whole month.
- **Electricity**: each machine running local models, from the Local models estimate (docs/ENERGY.md): measured watts
  where a reading exists, else the GPU's TDP times its utilisation, times the hours, times your price per kWh.

Every account in `accounts:` without a fee, and every paid worker turned on without a spend, is a row marked **not
configured** that names the setting to fill. Tutti never reads a bill, a key or a credential file (e.g. a provider's `.env`).
Beside each account its usage against its limit (the last reading and the pacing verdict) shows.

Totals: month to date, the forecast, and how many rows are not configured. The totals' currency is `costs.currency`, or the
energy currency when empty; other currencies convert with the approximate rates of `tutti/energy.py`, and a fee in a
currency Tutti does not know is not guessed (not configured).

**Month by month**: each time the tab loads, this month's running total is kept in `costs-history.json` in the state
folder; the last value of a month is its total. 24 months are kept.

## Admin controls

One list with each admin control's state and a link to where it lives: project priorities, machine limits and hours,
night mode, the backup main machine, the GitHub connection, accounts, opt-in and paid workers, self-update, the questions
that need your input, and users and invites.
