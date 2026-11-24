"""One card per Android device on the dashboard (docs/ARCHITECTURE.md, *Devices and controls*): its one status, why, what it
waits on, the one action that helps, and where its shards went while away. The pause switches, the idle watchdog and the
reroute lines are folded in here so the card and the summary never disagree."""

TONE = {'ready': 'ok', 'busy': 'accent', 'blocked': 'bad', 'offline': 'bad', 'paused': 'warn', 'away': 'warn',
        'away later': 'warn', 'person': 'warn', 'waiting': 'warn', 'held': 'warn', 'call': 'warn', 'idle': 'warn'}
LABEL = {'held': 'paused', 'call': 'paused: phone call', 'person': 'in use by a person', 'idle': 'idle while work waits'}


def plain_minutes(minutes):
    """2298 -> '1 day 14 h'; 95 -> '1 h 35 min'; 12 -> '12 min'."""
    m = int(minutes)
    if m < 60:
        return f'{m} min'
    h, rest = divmod(m, 60)
    if h < 24:
        return f'{h} h {rest} min' if rest else f'{h} h'
    d, h = divmod(h, 24)
    return f'{d} day{"s" if d != 1 else ""}' + (f' {h} h' if h else '')


def temps(d):
    """The device's temperatures, labelled by sensor: the skin sensor, else the SoC (scripts/device-readings.sh reads the
    thermal HAL, else /sys thermal zones), then the battery only when one is present (a TV reports a meaningless 2.5 °C).
    [] when no sensor answers: the card then says "temperature not readable"."""
    out = []
    if d.get('temp_skin') is not None:
        out.append(f'skin {round(float(d["temp_skin"]))} °C')
    elif d.get('temp_soc') is not None:
        out.append(f'SoC {round(float(d["temp_soc"]))} °C')
    if d.get('battery_c') is not None and d.get('battery_present') != 0 and d.get('kind') != 'tv':
        out.append(f'battery {float(d["battery_c"]):g} °C')
    return out


def _paused(p):
    if p.get('window') == 'planned':
        return 'away later'
    return 'away' if p.get('from') is not None and p.get('window') != 'over' else 'paused'


def card(d, paused=None, warning=None, moved=''):
    """The card of device row `d` from /api/state: {status, label, tone, why, waiting, action, moved, temp}."""
    name = d['name']
    status, why, waiting, action = d.get('status') or 'offline', d.get('reason') or '', '', None
    if warning:
        waiting = warning.get('what') or ''
    if paused:
        status, why = _paused(paused), ''
        action = {'action': 'resume', 'kind': 'device', 'name': name, 'label': 'Resume'}
    elif warning and warning.get('state') == 'blocked':
        status, why = 'blocked', warning.get('reason') or why
        action = warning.get('fix') or {'action': 'dispatch_now', 'label': 'Check again now'}
    elif warning:
        status = 'idle'
        why = f'idle for {plain_minutes(warning.get("minutes") or 0)}; Keep busy is sending it work'
    return {'status': status, 'label': LABEL.get(status, status), 'tone': TONE.get(status, 'warn'), 'why': why,
            'waiting': waiting, 'action': action, 'moved': moved, 'temps': temps(d)}


def attach(cfg, out):
    """Sets `card` on every device row of the dashboard state `out` (after controls and dispatch are in it)."""
    paused = {p['name']: p for p in (out.get('controls') or {}).get('paused') or [] if p.get('kind') == 'device'}
    disp = out.get('dispatch') or {}
    warns = {w['name']: w for w in disp.get('warnings') or [] if w.get('kind') in ('android', 'tv', 'device')}
    hosts = cfg.get('hosts') or cfg.get('machines') or {}
    devs = cfg.get('devices') or {}
    for d in (out.get('devices') or {}).get('items') or []:
        label = (devs.get(d['name']) or {}).get('model') or d['name']
        moved = next((line.split(': ', 1)[-1] for line in disp.get('reroute') or [] if line.startswith(f'{label} away')), '')
        d['also_host'] = d['name'] in hosts
        d['card'] = card(d, paused.get(d['name']), warns.get(d['name']), moved)
    for m in (out.get('machines') or {}).get('items') or []:
        m['also_device'] = m['name'] in devs
    return out
