"""The household's optional private network (docs/NETWORK.md): Tailscale or plain WireGuard between the household's machines
and phones. Tutti never needs one: on a plain home LAN the dashboard, the household join, ssh to machines and adb to phones all
work. A VPN only adds reach away from home and stable names.

This module is the pure part: what each machine and phone has (parsed from one probe's output), the install command per OS,
the Funnel guard (Tailscale Funnel is public, so it is refused on Tutti's ports), and the setup plan: every step Tutti can do
itself, and the few only a person can do (a sign-in, one sudo line per machine, a tap on the phone, a router consent), gathered
in one short checklist in the best order. No credential, auth key or private key ever passes through this module."""
import json
import re
import shlex

PROVIDERS = ('tailscale', 'wireguard')
SUGGESTED = 'tailscale'   # what `tutti vpn setup` offers first (docs/NETWORK.md, *Which one*): no router change, works behind CGNAT
TS_PACKAGE = 'com.tailscale.ipn'
WG_PACKAGE = 'com.wireguard.android'
LOGIN_URL = re.compile(r'https://login\.tailscale\.com/\S+')
ADMIN_DNS = 'https://login.tailscale.com/admin/dns'
ADMIN_KEYS = 'https://login.tailscale.com/admin/settings/keys'

# One look at a machine: prints KEY=value lines and the Tailscale status JSON between markers. Read-only; never asks for a password.
PROBE = r'''
os=$(uname -s 2>/dev/null); echo "OS=$os"
ts=$(command -v tailscale || command -v tailscale.exe); echo "TS=${ts:+1}"
command -v wg >/dev/null 2>&1 && echo "WG=1"
if [ "$(id -u)" = 0 ] || sudo -n true 2>/dev/null; then echo "SUDO=1"; fi
for p in apt-get dnf pacman zypper brew winget.exe; do command -v $p >/dev/null 2>&1 && { echo "PKG=$p"; break; }; done
if [ -n "$ts" ]; then
  echo "--status"; timeout 10 "$ts" status --json 2>/dev/null | tr -d '\n'; echo
  echo "--funnel"; timeout 10 "$ts" funnel status --json 2>/dev/null | tr -d '\n'; echo
fi
'''


def parse_probe(text):
    """{'os', 'tailscale': bool, 'wg': bool, 'sudo': bool, 'pkg', 'state', 'dns_name', 'ips', 'magicdns', 'funnel'} from PROBE's
    output; a machine that did not answer gives {'reachable': False}."""
    if not text or 'OS=' not in text:
        return {'reachable': False}
    kv, blocks, cur = {}, {}, None
    for line in text.splitlines():
        if line.startswith('--'):
            cur = line[2:]
        elif cur:
            blocks[cur] = (blocks.get(cur, '') + line).strip()
        elif '=' in line:
            k, _, v = line.partition('=')
            kv[k.strip()] = v.strip()
    st = _json(blocks.get('status'))
    me = st.get('Self') or {}
    return {'reachable': True, 'os': os_name(kv.get('OS', '')), 'tailscale': kv.get('TS') == '1', 'wg': kv.get('WG') == '1',
            'sudo': kv.get('SUDO') == '1', 'pkg': kv.get('PKG', ''), 'state': st.get('BackendState') or ('' if st else 'unknown'),
            'dns_name': (me.get('DNSName') or '').rstrip('.'), 'ips': me.get('TailscaleIPs') or [],
            'magicdns': bool((st.get('CurrentTailnet') or {}).get('MagicDNSEnabled')), 'funnel': _json(blocks.get('funnel'))}


def _json(text):
    try:
        v = json.loads(text or '{}')
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def os_name(uname):
    u = uname.lower()
    return 'macos' if u == 'darwin' else 'windows' if u.startswith(('mingw', 'msys', 'cygwin', 'windows')) else 'linux' if u else ''


def logged_in(facts):
    return facts.get('state') == 'Running'


def android_has(packages_output, package):
    """Whether `adb shell pm list packages <package>` lists exactly that package."""
    return f'package:{package}' in {x.strip() for x in (packages_output or '').splitlines()}


def play_store(serial, package, adb='adb'):
    """The adb line that opens the app's Play Store page on the phone (the person taps Install); `serial` is a shell word
    (devices.serial_expr: USB, else a wireless address)."""
    return f'{shlex.quote(adb)} -s {serial} shell am start -a android.intent.action.VIEW -d market://details?id={package}'


# ---- install commands: the official package or installer, one exact line per OS

def install_tailscale(os_, pkg=''):
    """(command, link) to install Tailscale; Linux uses Tailscale's own script, which adds its repository and package."""
    if os_ == 'macos':
        return 'brew install --cask tailscale', 'https://tailscale.com/download/mac'
    if os_ == 'windows' or pkg == 'winget.exe':
        return 'winget install --exact --id Tailscale.Tailscale', 'https://tailscale.com/download/windows'
    return 'curl -fsSL https://tailscale.com/install.sh | sh', 'https://tailscale.com/download/linux'


def install_wireguard(os_, pkg=''):
    if os_ == 'macos':
        return 'brew install wireguard-tools', 'https://www.wireguard.com/install/'
    if os_ == 'windows' or pkg == 'winget.exe':
        return 'winget install --exact --id WireGuard.WireGuard', 'https://www.wireguard.com/install/'
    line = {'dnf': 'sudo dnf install -y wireguard-tools', 'pacman': 'sudo pacman -S --noconfirm wireguard-tools',
            'zypper': 'sudo zypper install -y wireguard-tools'}.get(pkg, 'sudo apt-get install -y wireguard-tools')
    return line, 'https://www.wireguard.com/install/'


def up_command(authkey=False):
    """Joins the tailnet. With an auth key it reads the key from stdin (never argv, never a log); without, it prints the login
    URL, which Tutti opens in the admin's browser. --operator lets this login run `tailscale` later without sudo."""
    key = ' --auth-key=file:/dev/stdin' if authkey else ''
    return f'sudo tailscale up --operator="$USER"{key}'


def cert_command(dns_name, state_dir):
    """The private https name's certificate for the dashboard host (only reachable inside the tailnet)."""
    d, n = f'{state_dir}/tls', shlex.quote(dns_name)
    return f'mkdir -p {d} && tailscale cert --cert-file {d}/{n}.crt --key-file {d}/{n}.key {n}'


# ---- Funnel: public, so never enabled, and refused on Tutti's ports

FUNNEL_REFUSED = ('Tailscale Funnel publishes a port to the whole internet, and a Tutti dashboard is never on the internet '
                  '(docs/NETWORK.md, *Never public*). Tutti never turns Funnel on.')


def funnel_hits(serve, ports):
    """[public host:port] whose Funnel forwards to one of Tutti's ports, from `tailscale funnel status --json`."""
    ports = {str(p) for p in ports}
    hits = []
    for hostport, on in ((serve or {}).get('AllowFunnel') or {}).items():
        if not on:
            continue
        handlers = (((serve.get('Web') or {}).get(hostport) or {}).get('Handlers') or {}).values()
        targets = [h.get('Proxy', '') for h in handlers if isinstance(h, dict)]
        if any(re.search(rf':({"|".join(map(re.escape, ports))})(/|$)', t) for t in targets):
            hits.append(hostport)
        tcp = ((serve.get('TCP') or {}).get(hostport.rsplit(':', 1)[-1]) or {}).get('TCPForward', '')
        if tcp and tcp.rsplit(':', 1)[-1] in ports and hostport not in hits:
            hits.append(hostport)
    return hits


def funnel_error(facts, ports):
    """'' unless Funnel exposes one of Tutti's ports; else the refusal with the exact command that turns it off."""
    hits = funnel_hits(facts.get('funnel'), ports)
    if not hits:
        return ''
    offs = '; '.join(f'tailscale funnel --https={h.rsplit(":", 1)[-1]} off' for h in hits)
    return f'{FUNNEL_REFUSED} It is on for {", ".join(hits)}, which reaches Tutti: turn it off with `{offs}`, then start Tutti again.'


# ---- the choice and the plan

def choice(cfg, asked=None):
    """The VPN `tutti vpn setup` sets up: the one asked for, else vpn.prefer (Settings), else the suggested one."""
    return asked or (cfg.get('vpn') or {}).get('prefer') or SUGGESTED


def errors(cfg):
    v = cfg.get('vpn') or {}
    errs = []
    if v.get('prefer') not in (None, *PROVIDERS):
        errs.append(f'vpn.prefer is {v.get("prefer")!r}: tailscale or wireguard (which one `tutti vpn setup` sets up first; '
                    'Tutti works without either)')
    port = (v.get('wireguard') or {}).get('port', 51820)
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        errs.append(f'vpn.wireguard.port is {port!r}: a UDP port from 1024 to 65535 (default 51820)')
    return errs


def step(id_, who, title, detail='', command='', url='', done=False):
    """One row of the setup checklist: who is 'tutti' (done on its own) or 'you' (only a person can)."""
    return {'id': id_, 'who': who, 'title': title, 'detail': detail, 'command': command, 'url': url, 'done': done}


def tailscale_plan(machines, phones):
    """Steps for Tailscale. machines: {name: facts}; phones: {name: {'has': bool, 'serial'}}. People's steps come in the best
    order: install lines first (they unblock the rest), then one sign-in per machine, then phones, then MagicDNS."""
    you, auto = [], []
    for name, f in machines.items():
        if not f.get('reachable'):
            auto.append(step(f'reach-{name}', 'tutti', f'{name}: not reachable now', 'it is checked again on the next run'))
            continue
        if not f['tailscale']:
            cmd, url = install_tailscale(f['os'], f['pkg'])
            if f['sudo'] and f['os'] == 'linux':
                auto.append(step(f'install-{name}', 'tutti', f'{name}: install Tailscale', 'passwordless sudo: Tutti runs it', cmd))
            else:
                you.append(step(f'install-{name}', 'you', f'{name}: install Tailscale', 'one line in a terminal there', cmd, url))
        if not logged_in(f):
            if f['sudo'] and f['os'] == 'linux':
                auto.append(step(f'join-{name}', 'tutti', f'{name}: join', 'Tutti runs it and opens the sign-in page here', up_command()))
                you.append(step(f'signin-{name}', 'you', f'{name}: sign in to Tailscale in the page Tutti opened',
                                f'the same account on every machine. Headless? `tutti vpn setup --authkey {name}` with a key from '
                                f'{ADMIN_KEYS}'))
            else:
                you.append(step(f'join-{name}', 'you', f'{name}: join and sign in', 'it prints a sign-in link: open it, same account '
                                'everywhere', up_command()))
    for name, p in phones.items():
        if not p['has']:
            auto.append(step(f'store-{name}', 'tutti', f'{name}: open Tailscale in the Play Store', 'over adb',
                             play_store(p['serial'], TS_PACKAGE)))
            you.append(step(f'phone-{name}', 'you', f'{name}: tap Install, open Tailscale and sign in with the same account'))
    up = [f for f in machines.values() if logged_in(f)]
    if up and not any(f['magicdns'] for f in up):
        you.append(step('magicdns', 'you', 'Turn on MagicDNS (names like nuc.your-tailnet.ts.net)', 'one switch in the admin console',
                        url=ADMIN_DNS))
    return auto, you


def suggested_hosts(machines_cfg, facts):
    """{machine: {'host': tailnet name, 'lan': the old host}} for machines that joined: the tailnet name first, the LAN fallback kept."""
    out = {}
    for name, m in machines_cfg.items():
        f = facts.get(name) or {}
        if m.get('transport') == 'local' or not logged_in(f) or not f.get('dns_name') or m.get('host') == f['dns_name']:
            continue
        out[name] = {'host': f['dns_name'], 'lan': m.get('lan') or m.get('host')}
    return out
