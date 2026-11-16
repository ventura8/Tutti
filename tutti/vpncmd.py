"""`tutti vpn status|setup` and the dashboard's Private network card (docs/NETWORK.md). Entirely optional: Tutti works on a
plain LAN and with any VPN the household already has. `setup` looks at every machine and phone, picks the VPN (vpn.prefer, else
the suggested one) without asking, does every step it can, and writes the few steps only a person can do into one checklist
(state/vpn.json) that the card shows in order. Running it again continues where it left off: every step is checked afresh."""
import json
import os
import re
import shlex
import subprocess
import sys
from getpass import getpass

from . import config, devices, guided, netpolicy, remote, vpn, wireguard

QR_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')


def say(text, out=sys.stdout):
    print(text, file=out)


def state_path(cfg):
    return os.path.join(config.state_dir(cfg), 'vpn.json')


def read_state(cfg):
    try:
        with open(state_path(cfg), encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(cfg, st):
    """vpn.json holds addresses, public keys and the checklist: never a private key or an auth key."""
    os.makedirs(config.state_dir(cfg), exist_ok=True)
    tmp = state_path(cfg) + '.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, state_path(cfg))


def tutti_ports(cfg):
    return [p for p in ((cfg.get('dashboard') or {}).get('port'), (cfg.get('household') or {}).get('port'),
                        (cfg.get('mcp') or {}).get('http_port')) if p]


def probe_machines(cfg, sh=remote.sh):
    """{machine: facts}; a Termux phone machine is left out (its own app joins it)."""
    facts = {}
    for name, m in cfg['machines'].items():
        if m.get('transport') == 'android':
            continue
        try:
            r = sh(m, vpn.PROBE, capture=True, timeout=30, ssh_opts=remote.QUICK)
            facts[name] = vpn.parse_probe(r.stdout if r.returncode == 0 else '')
        except (OSError, subprocess.SubprocessError):
            facts[name] = {'reachable': False}
    return facts


def probe_phones(cfg, package, sh=remote.sh):
    out = {}
    for name, d in (cfg.get('devices') or {}).items():
        m = cfg['machines'].get(d.get('machine'))
        adb = d.get('adb') or 'adb'
        line = f'timeout 15 {shlex.quote(adb)} -s {devices.serial_expr(d)} shell pm list packages {package}'
        try:
            r = sh(m, line, capture=True, timeout=30, ssh_opts=remote.QUICK) if m else None
        except (OSError, subprocess.SubprocessError):
            r = None
        out[name] = {'serial': devices.serial_expr(d), 'machine': d.get('machine'), 'adb': adb,
                     'has': bool(r and r.returncode == 0 and vpn.android_has(r.stdout, package)),
                     'reachable': bool(r and r.returncode == 0)}
    return out


def funnel_steps(cfg, facts):
    rows = []
    for name, f in facts.items():
        err = vpn.funnel_error(f, tutti_ports(cfg)) if f.get('reachable') else ''
        if err:
            rows.append(vpn.step(f'funnel-{name}', 'you', f'{name}: turn Tailscale Funnel off', err, re.findall(r'`([^`]+)`', err)[-1]))
    return rows


def funnel_refusal(cfg, run=subprocess.run):
    """The refusal text when Tailscale Funnel on this computer reaches one of Tutti's ports, else '' (no Tailscale: '')."""
    try:
        r = run(['bash', '-c', vpn.PROBE], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ''
    return vpn.funnel_error(vpn.parse_probe(r.stdout), tutti_ports(cfg))


# ---- Tailscale

def join(machine, sh_argv=remote.argv, popen=subprocess.Popen, opener=None, out=sys.stdout):
    """Runs `tailscale up` there (passwordless sudo) and shows its sign-in link for the owner to open in their own browser
    (Tutti never opens the owner's browser by itself: docs/BROWSER.md). Returns the exit code."""
    opener = opener or (lambda link: None)
    p = popen(sh_argv(machine, vpn.up_command().replace('sudo ', 'sudo -n ', 1)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
              text=True, errors='replace')
    opened = False
    for line in p.stdout:
        hit = vpn.LOGIN_URL.search(line)
        if hit and not opened:
            opened = True
            say(f'{machine["name"]}: open this link in your browser and sign in to Tailscale: {hit.group(0)}', out)
            if hasattr(out, 'flush'):
                out.flush()   # the link shows now, not when `tailscale up` ends
            opener(hit.group(0))
    return p.wait()


def authkey_join(machine, key, sh=remote.sh):
    """`tailscale up --auth-key=file:/dev/stdin` over ssh: the key travels on stdin only and is never printed or stored."""
    r = sh(machine, vpn.up_command(authkey=True).replace('sudo ', 'sudo -n ', 1), capture=True, timeout=120, stdin=key + '\n')
    return r.returncode


def tailscale_run(cfg, facts, phones, sh, opener, out):
    auto, you = vpn.tailscale_plan(facts, phones)
    for s in auto:
        name = s['id'].split('-', 1)[1]
        if s['id'].startswith('install-'):
            r = sh(cfg['machines'][name], s['command'].replace('| sh', '| sudo -n sh'), capture=True, timeout=600)
            s['done'] = r.returncode == 0
        elif s['id'].startswith('join-'):
            s['done'] = join({**cfg['machines'][name], 'name': name}, opener=opener, out=out) == 0
        elif s['id'].startswith('store-'):
            p = phones[name]
            r = sh(cfg['machines'][p['machine']], vpn.play_store(p['serial'], vpn.TS_PACKAGE, p['adb']), capture=True, timeout=30)
            s['done'] = r.returncode == 0
    return auto, you


# ---- WireGuard

def hub_name(cfg):
    """The always-on Tutti host: vpn.wireguard.hub, else where `tutti move` put Tutti (host.machine), else this computer."""
    wg = (cfg.get('vpn') or {}).get('wireguard') or {}
    if wg.get('hub') in cfg['machines']:
        return wg['hub']
    moved = (cfg.get('host') or {}).get('machine')
    if moved in cfg['machines']:
        return moved
    return next((n for n, m in cfg['machines'].items() if m.get('transport') == 'local'), next(iter(cfg['machines']), 'here'))


def wg_state(cfg, old, facts, phones, taken):
    wg = (cfg.get('vpn') or {}).get('wireguard') or {}
    hub = hub_name(cfg)
    subnet = old.get('subnet') or wireguard.pick_subnet(taken, wg.get('subnet', 'auto'))
    names = [n for n in facts if n != hub] + list(phones)
    lan = netpolicy.private_addresses(netpolicy.interfaces())
    return {'subnet': subnet, 'hub': hub, 'port': wg.get('port', wireguard.DEFAULT_PORT), 'endpoint': wg.get('endpoint') or '',
            'hub_lan': old.get('hub_lan') or next((a for a in lan if ':' not in a), '127.0.0.1'),
            'addresses': wireguard.assign(subnet, hub, names, old.get('addresses')), 'keys': dict(old.get('keys') or {})}


def wg_machine(cfg, st, name, f, sh):
    """Key (made here, sent on stdin into a 600 file there; an existing one is kept), config (600, no private key), then the one
    sudo line: run by Tutti with passwordless sudo, else a step for the person."""
    m = cfg['machines'][name]
    if not st['keys'].get(name):
        priv, pub = wireguard.keypair()
        r = sh(m, wireguard.key_write_script(), capture=True, timeout=30, stdin=priv + '\n')
        del priv
        lines = (r.stdout or '').split()
        if r.returncode == 0 and lines[:1] == ['kept'] and len(lines) > 1:
            pub = lines[1]   # the key already there is kept: its public half, from `wg pubkey` there
        elif r.returncode != 0 or lines[:1] != ['written']:
            return vpn.step(f'key-{name}', 'you', f'{name}: WireGuard key', f'a key is already at {wireguard.key_file()} on {name} '
                            'and `wg` is not there to read its public half: install WireGuard there (the line below), then '
                            '`tutti vpn setup` again', vpn.install_wireguard(f.get('os'), f.get('pkg'))[0])
        st['keys'][name] = pub
    conf = wireguard.hub_conf(st, wireguard.key_file()) if name == st['hub'] else wireguard.peer_conf(st, name, wireguard.key_file())
    path = f'{wireguard.KEY_DIR}/{wireguard.IFACE}.conf'
    script, text = wireguard.write_conf_script(path, conf)
    if sh(m, script, capture=True, timeout=30, stdin=text).returncode != 0:
        return vpn.step(f'up-{name}', 'you', f'{name}: WireGuard config not written', f'writing {path} there failed: check the '
                        'disk and that this login owns its ~/.config, then `tutti vpn setup` again', 'tutti vpn setup --wireguard')
    line = wireguard.linux_steps(name, f, path)
    if f.get('os') != 'linux':
        cmd, url = vpn.install_wireguard(f.get('os'), f.get('pkg'))
        return vpn.step(f'up-{name}', 'you', f'{name}: install WireGuard and import {path}', 'the WireGuard app: Import tunnel from file',
                        cmd, url)
    if f.get('sudo'):
        r = wireguard.run_sudo_line(m, line, sh)
        return vpn.step(f'up-{name}', 'tutti', f'{name}: WireGuard on', 'passwordless sudo: Tutti ran it', line, done=r.returncode == 0)
    return vpn.step(f'up-{name}', 'you', f'{name}: turn WireGuard on (one line, asks for your password)', '', line)


def wireguard_run(cfg, facts, phones, sh, taken=None):
    st = wg_state(cfg, read_state(cfg).get('wireguard') or {}, facts, phones,
                  wireguard.taken_networks() if taken is None else taken)
    auto, you = [], []
    hub_first = sorted((n for n, f in facts.items() if f.get('reachable')), key=lambda n: n != st['hub'])
    for name in hub_first:   # the hub's key first: every peer's config names it
        if name != st['hub'] and not st['keys'].get(st['hub']):
            you.append(vpn.step(f'up-{name}', 'you', f'{name}: waits for the hub', f'the hub ({st["hub"]}) is not reachable or has no '
                                'key yet: bring it online, then `tutti vpn setup` again', 'tutti vpn setup --wireguard'))
            continue
        row = wg_machine(cfg, st, name, facts[name], sh)
        (auto if row['who'] == 'tutti' else you).append(row)
    if st['keys'].get(st['hub']) and any(n != st['hub'] for n in hub_first):   # peers joined after the hub's config: write it again
        wg_machine(cfg, st, st['hub'], facts[st['hub']], sh)
    for name, p in phones.items():
        if not p['has']:
            m = cfg['machines'].get(p['machine'])
            r = sh(m, vpn.play_store(p['serial'], vpn.WG_PACKAGE, p['adb']), capture=True, timeout=30) if m else None
            auto.append(vpn.step(f'store-{name}', 'tutti', f'{name}: open WireGuard in the Play Store', 'over adb',
                                 done=bool(r and r.returncode == 0)))
        you.append(vpn.step(f'qr-{name}', 'you', f'{name}: scan its QR code in the WireGuard app', 'Private network card → Show QR; '
                            '+ → Scan from QR code'))
    you.append(vpn.step('router', 'you', 'Only for use away from home: open UDP ' + str(st['port']) + ' on the router',
                        'the card\'s "Open the port" button (UPnP or NAT-PMP, after your click), or the router\'s own page'))
    return auto, you, st


# ---- the command

KIND = {'signin': 'sign-in', 'install': 'sudo', 'join': 'sudo', 'up': 'sudo', 'key': 'sudo', 'phone': 'phone', 'qr': 'phone',
        'store': 'phone'}
ORDER = ('signin', 'funnel', 'install', 'key', 'up', 'join', 'store', 'phone', 'qr', 'magicdns', 'router')


def kind(s):
    return KIND.get(s['id'].split('-', 1)[0], 'other')


def checklist(you):
    """The person's steps in tutti/guided.py's order (sign-ins, sudo lines, phone taps, the rest), each kind in its best order."""
    def key(s):
        head = s['id'].split('-', 1)[0]
        return guided.KINDS.index(kind(s)), ORDER.index(head) if head in ORDER else len(ORDER)
    return [{**s, 'kind': kind(s)} for s in sorted(you, key=key)]


def guided_steps(you):
    """The checklist as tutti/guided.py steps: what `tutti setup` merges into its own list."""
    return [guided.step(kind(s), s['title'], s['command'] or s['url'], s['detail'] or 'only you can do this') for s in you]


def guided_setup(cfg, sh=remote.sh):
    """The interface for `tutti setup` (docs/INSTALL.md): (choices, steps), changing nothing. The VPN is optional, so the only
    choice is which one `tutti vpn setup` would set up; the steps are what it would leave for the person."""
    provider = vpn.choice(cfg)
    facts = probe_machines(cfg, sh)
    _auto, you = plan_only(cfg, provider, facts, probe_phones(cfg, vpn.TS_PACKAGE if provider == 'tailscale' else vpn.WG_PACKAGE, sh))
    why = 'optional: no router change and works behind CGNAT (docs/NETWORK.md); Tutti works without a VPN'
    return [guided.choice('vpn.prefer', provider, why)], guided_steps(checklist(funnel_steps(cfg, facts) + you))


def plan_only(cfg, provider, facts, phones):
    """What setup would do, with no change anywhere (--dry-run)."""
    if provider == 'tailscale':
        auto, you = vpn.tailscale_plan(facts, phones)
        return auto, you
    hub = hub_name(cfg)
    auto = [vpn.step(f'key-{n}', 'tutti', f'{n}: make its WireGuard key and write its config (600, no private key in it)')
            for n, f in facts.items() if f.get('reachable')]
    you = [vpn.step(f'up-{n}', 'you' if not f.get('sudo') else 'tutti', f'{n}: WireGuard on',
                    command=wireguard.linux_steps(n, f, f'{wireguard.KEY_DIR}/{wireguard.IFACE}.conf'))
           for n, f in facts.items() if f.get('reachable')]
    you += [vpn.step(f'qr-{n}', 'you', f'{n}: scan its QR code in the WireGuard app') for n in phones]
    you.append(vpn.step('router', 'you', f'Only away from home: UDP {(cfg.get("vpn") or {}).get("wireguard", {}).get("port", 51820)} '
                        f'to {hub} on the router, after your click'))
    return [s for s in auto + you if s['who'] == 'tutti'], [s for s in you if s['who'] == 'you']


def print_rows(auto, you, out):
    for s in auto:
        say(f'  tutti {"done" if s["done"] else "    "}  {s["title"]}' + (f'  ({s["command"]})' if s['command'] else ''), out)
    say(guided.render(guided_steps(you)), out)


def cmd_vpn(cfg, a, sh=remote.sh, opener=None, out=sys.stdout):
    if a.action == 'funnel':
        say(vpn.FUNNEL_REFUSED + ' Share the dashboard over the LAN or a private network instead (docs/NETWORK.md).', out)
        return 2
    provider = vpn.choice(cfg, a.provider)
    facts = probe_machines(cfg, sh)
    phones = probe_phones(cfg, vpn.TS_PACKAGE if provider == 'tailscale' else vpn.WG_PACKAGE, sh)
    if a.action == 'status':
        return status(cfg, facts, phones, out)
    if a.authkey:
        return authkey(cfg, a.authkey, sh, out)
    say(f'vpn: {provider} (optional: Tutti works on the LAN and with your own VPN; vpn.prefer in Settings switches it)', out)
    if vpn.is_wsl() or any(f.get('wsl') for f in facts.values()):
        say(vpn.wsl_warning(), out)
    stop = funnel_steps(cfg, facts)
    if a.dry_run:
        auto, you = plan_only(cfg, provider, facts, phones)
        say('would do (nothing changed):', out)
        print_rows(auto, checklist(stop + you), out)
        return 0
    if stop:
        print_rows([], stop, out)
        return 1
    if provider == 'tailscale':
        auto, you = tailscale_run(cfg, facts, phones, sh, opener, out)
        st = {}
    else:
        auto, you, st = wireguard_run(cfg, facts, phones, sh)
    you = checklist(you)
    write_state(cfg, {'provider': provider, 'auto': auto, 'checklist': you, 'wireguard': st or read_state(cfg).get('wireguard') or {},
                      'hosts': vpn.suggested_hosts(cfg['machines'], facts)})
    print_rows(auto, you, out)
    hosts(vpn.suggested_hosts(cfg['machines'], facts), out)
    if a.cert:
        cert(cfg, facts, sh, out)
    say(f'vpn: {len(you)} step(s) for you, also on the dashboard\'s Private network card; `tutti vpn setup` again continues', out)
    return 0


def hosts(suggested, out):
    if suggested:
        say('machines: the tailnet name first, the LAN kept as the fallback (in tutti.config.yaml):', out)
        for name, h in suggested.items():
            say(f'  {name}: {{host: {h["host"]}, lan: {h["lan"]}}}', out)


def cert(cfg, facts, sh, out):
    """The private https certificate for this computer's tailnet name (reachable only inside the tailnet)."""
    here = next((n for n, m in cfg['machines'].items() if m.get('transport') == 'local'), None)
    f = facts.get(here) or {}
    if not vpn.logged_in(f) or not f.get('dns_name'):
        say('cert: this computer is not on the tailnet yet (or MagicDNS is off): finish the steps above, then --cert again', out)
        return 1
    line = vpn.cert_command(f['dns_name'], shlex.quote(config.state_dir(cfg)))
    r = sh(cfg['machines'][here], line, capture=True, timeout=120)
    say(f'cert: {"written under " + config.state_dir(cfg) + "/tls" if r.returncode == 0 else "failed: run it yourself: " + line}', out)
    return r.returncode


def authkey(cfg, name, sh, out, read=getpass):
    """A headless machine joins with an auth key from the Tailscale admin console, typed hidden here, sent on stdin, never kept."""
    m = cfg['machines'].get(name)
    if not m:
        say(f'no machine "{name}": one of {", ".join(cfg["machines"])}', out)
        return 2
    key = read(f'Auth key for {name} (from {vpn.ADMIN_KEYS}; hidden, never stored): ').strip()
    if not key:
        say('no key typed: nothing done', out)
        return 2
    rc = authkey_join(m, key, sh)
    del key
    say(f'{name}: joined the tailnet' if rc == 0 else f'{name}: `tailscale up` with the key failed (exit {rc}): check the key is '
        'valid and not expired, and that this login has passwordless sudo there (else run `sudo tailscale up` there yourself)', out)
    return 0 if rc == 0 else 1


def status(cfg, facts, phones, out):
    say('Private network (optional). Already have a VPN to your home? Nothing to do: Tutti works on the LAN and over any VPN that '
        'gives private addresses.', out)
    for name, f in facts.items():
        if not f.get('reachable'):
            say(f'  {name:12} not reachable', out)
            continue
        ts = ('signed in' if vpn.logged_in(f) else f'not signed in ({f["state"] or "stopped"})') if f['tailscale'] else 'not installed'
        say(f'  {name:12} tailscale {ts}{"  " + f["dns_name"] if f["dns_name"] else ""}; wireguard {"yes" if f["wg"] else "no"}', out)
        err = vpn.funnel_error(f, tutti_ports(cfg))
        if err:
            say(f'  {name:12} REFUSED: {err}', out)
    for name, p in phones.items():
        app = 'installed' if p['has'] else 'not installed' if p['reachable'] else 'unknown (adb did not answer)'
        say(f'  {name:12} (phone) app {app}', out)
    return 0


# ---- the dashboard's card: POST /api/vpn

def api(cfg, body, sh=remote.sh, run=subprocess.run):
    """(status, answer) for POST /api/vpn {"action": "status"} | {"action": "qr", "phone": name} | {"action": "router"} |
    {"action": "open-port", "consent": true}."""
    if isinstance(body, (bytes, str)):   # the dashboard hands the raw request body (a POST crashed on bytes.get, 2026-11-14)
        try:
            body = json.loads(body or b'null')
        except ValueError:
            body = None
    if not isinstance(body, dict):
        return 400, {'error': 'send a JSON object such as {"action": "status"}'}
    action = body.get('action')
    st = read_state(cfg)
    if action == 'status':
        return 200, {'provider': st.get('provider') or '', 'suggested': vpn.choice(cfg), 'checklist': st.get('checklist') or [],
                     'auto': st.get('auto') or [], 'phones': sorted(cfg.get('devices') or {})}
    if action == 'qr':
        return qr(cfg, st, body.get('phone'), run, sh)
    port = (st.get('wireguard') or {}).get('port', wireguard.DEFAULT_PORT)
    if action == 'router':
        kind = wireguard.router_kind(run)
        return 200, {'kind': kind, 'port': port, 'steps': '' if kind else wireguard.router_steps(port)}
    if action == 'open-port':
        try:
            ok, text = wireguard.open_port(wireguard.router_kind(run), port, body.get('consent'), run)
        except wireguard.ConsentNeeded as e:
            return 403, {'error': str(e)}
        except ValueError as e:
            return 200, {'ok': False, 'steps': str(e)}
        return 200, {'ok': ok, 'text': text}
    return 400, {'error': 'send {"action": "status"}, {"action": "qr", "phone": <device>}, {"action": "router"} or '
                          '{"action": "open-port", "consent": true}'}


def persist_hub(cfg, wg, sh=remote.sh):
    """Rewrites the hub's config with every peer and installs it with passwordless sudo (no restart: the peer is live already)."""
    path = f'{wireguard.KEY_DIR}/{wireguard.IFACE}.conf'
    m = cfg['machines'][wg['hub']]
    script, text = wireguard.write_conf_script(path, wireguard.hub_conf(wg, wireguard.key_file()))
    r = sh(m, script, capture=True, timeout=30, stdin=text)
    if r.returncode != 0:
        return r
    return sh(m, f'sudo -n install -m 600 {path} /etc/wireguard/{wireguard.IFACE}.conf', capture=True, timeout=30)


def qr(cfg, st, phone, run, sh=remote.sh):
    wg = st.get('wireguard') or {}
    if not isinstance(phone, str) or not QR_NAME.match(phone) or phone not in (wg.get('addresses') or {}):
        return 400, {'error': f'no phone "{phone}" in the WireGuard setup: run `tutti vpn setup --wireguard` first'}
    payload = wireguard.phone_payload(wg, phone)   # a new key pair each time: the old phone key stops working
    st['wireguard'] = wg
    write_state(cfg, st)
    live = run(['sudo', '-n', 'wg', 'set', wireguard.IFACE, 'peer', wg['keys'][phone], 'allowed-ips', f'{wg["addresses"][phone]}/32'],
               capture_output=True, text=True, timeout=15)
    if live.returncode == 0 and wg.get('hub') in cfg['machines']:   # and kept across a reboot: the hub's config, rewritten
        live = persist_hub(cfg, wg, sh)
    note = ('scan it now; the hub already knows this phone, now and after a restart' if live.returncode == 0 else
            'scan it now, then run `tutti vpn setup` once more so the hub learns the phone\'s new key')
    return 200, {'svg': wireguard.qr_svg(payload, run), 'text': payload, 'note': note + '. The code is shown once and not kept.'}
