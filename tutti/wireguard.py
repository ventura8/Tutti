"""Plain WireGuard run by Tutti, with no third-party account (docs/NETWORK.md, *WireGuard*). The hub is the always-on Tutti
host; every other machine and phone is a peer of it.

Keys: made in Python (X25519, RFC 7748, from os.urandom), so no `wg` is needed to make them. A machine's private key goes
straight to that machine over ssh, on stdin, into a 600 file in a 700 folder of its own home (key_write_script); a key already
there is kept. Nothing logs or stores it elsewhere. The wg-quick config Tutti writes holds no private key:
`PostUp = wg set %i private-key <file>` loads it on the machine.
A phone's pair is made when the dashboard shows its QR code, kept in memory for that one
answer and never written or logged; the hub keeps only the public key.

Addresses come from a private /24 that clashes with no network this computer is on and never with Tailscale's 100.64.0.0/10.
The router port forward (needed only for peers away from home) is opened over UPnP or NAT-PMP only after the admin's explicit
click, and only WireGuard's own UDP port. Pure apart from the `run` callables passed in."""
import base64
import ipaddress
import json
import os
import subprocess

DEFAULT_PORT = 51820
IFACE = 'tutti'
KEY_DIR = '~/.config/tutti/wireguard'
NEVER = [ipaddress.ip_network('100.64.0.0/10')]   # Tailscale's range, also carrier-grade NAT: a WireGuard subnet never goes here
CANDIDATES = [f'10.{n}.77.0/24' for n in range(77, 100)] + [f'172.{n}.77.0/24' for n in range(16, 32)] + ['192.168.177.0/24']
P = 2 ** 255 - 19
NATPMP_LIFETIME = 86400   # seconds; 0 would delete the mapping (RFC 6886): click again, or rerun setup, to renew
KEEPALIVE = 25   # seconds: keeps a phone's or a NATed peer's path open


class ConsentNeeded(PermissionError):
    """The router is changed only after the admin's explicit click."""


def taken_networks(run=subprocess.run):
    """Every network this computer is on (`ip -j addr`, with prefixes) and every route's destination: a new subnet avoids them."""
    nets = []
    for argv in (['ip', '-j', 'addr'], ['ip', '-j', 'route']):
        try:
            rows = json.loads(run(argv, capture_output=True, text=True, timeout=5).stdout or '[]')
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        for r in rows:
            for a in r.get('addr_info') or []:
                nets.append(f'{a.get("local")}/{a.get("prefixlen")}')
            if r.get('dst') not in (None, 'default'):
                nets.append(r['dst'])
    return parse_networks(nets)


def parse_networks(items):
    out = []
    for x in items:
        try:
            out.append(ipaddress.ip_network(x, strict=False))
        except ValueError:
            continue
    return out


def pick_subnet(taken, wanted='auto'):
    """The first candidate /24 that overlaps none of `taken` nor 100.64.0.0/10; `wanted` (vpn.wireguard.subnet) is checked the same."""
    bad = [n for n in [*taken, *NEVER] if n.version == 4]
    pool = CANDIDATES if wanted in (None, 'auto') else [wanted]
    for c in pool:
        net = ipaddress.ip_network(c, strict=False)
        if net.is_private and not any(net.overlaps(b) for b in bad):
            return str(net)
    raise ValueError(f'vpn.wireguard.subnet: {wanted if wanted not in (None, "auto") else "every candidate"} overlaps a network '
                     'this computer is on or Tailscale\'s 100.64.0.0/10; write a free private /24 such as 10.123.45.0/24')


def assign(subnet, hub, names, existing=None):
    """{name: address}: the hub gets .1, everyone else the next free address; addresses already given stay the same."""
    net = ipaddress.ip_network(subnet)
    out = {n: a for n, a in (existing or {}).items() if ipaddress.ip_address(a) in net}
    used = set(out.values())
    if hub not in out:
        first = str(net.network_address + 1)
        if first in used:
            raise ValueError(f'{first} is already given to another peer; the hub needs it')
        out[hub] = first
        used.add(first)
    free = (str(h) for h in net.hosts() if str(h) not in used)
    for n in names:
        if n not in out:
            out[n] = next(free)
    return out


def _x25519(k, u):
    """RFC 7748 X25519 scalar multiplication (the Montgomery ladder), so a key pair needs no `wg` binary."""
    k = bytearray(k)
    k[0] &= 248
    k[31] = (k[31] & 127) | 64
    n, x1 = int.from_bytes(k, 'little'), u
    x2, z2, x3, z3, swap = 1, 0, u, 1, 0
    for t in reversed(range(255)):
        bit = (n >> t) & 1
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b, c, d = (x2 + z2) % P, (x2 - z2) % P, (x3 + z3) % P, (x3 - z3) % P
        aa, bb, da, cb = a * a % P, b * b % P, d * a % P, c * b % P
        e = (aa - bb) % P
        x3, z3 = (da + cb) ** 2 % P, x1 * (da - cb) ** 2 % P
        x2, z2 = aa * bb % P, e * (aa + 121665 * e) % P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, P - 2, P) % P).to_bytes(32, 'little')


def public_key(private_b64):
    return base64.b64encode(_x25519(base64.b64decode(private_b64), 9)).decode()


def keypair(random=os.urandom):
    """(private, public), base64 as `wg genkey | wg pubkey` writes them; the private key is clamped like wg's."""
    k = bytearray(random(32))
    k[0] &= 248
    k[31] = (k[31] & 127) | 64
    priv = base64.b64encode(bytes(k)).decode()
    return priv, public_key(priv)


def key_file(key_dir=KEY_DIR):
    return f'{key_dir}/{IFACE}.key'


def key_write_script(key_dir=KEY_DIR):
    """Run on the machine the key belongs to, the key on stdin (never argv): keeps an existing key, else writes it with mode 600 in
    a 700 folder of that login's home. Prints `written`, or `kept` and the kept key's public half (with `wg` there)."""
    d = key_dir.replace('~', '$HOME', 1)
    return (f'umask 077 && mkdir -p "{d}" && chmod 700 "{d}" && if [ -s "{d}/{IFACE}.key" ]; then cat > /dev/null; echo kept; '
            f'wg pubkey < "{d}/{IFACE}.key" 2>/dev/null; '
            f'else cat > "{d}/{IFACE}.key" && chmod 600 "{d}/{IFACE}.key" && echo written; fi')


def peer_block(public_key, allowed, endpoint='', keepalive=False):
    lines = ['[Peer]', f'PublicKey = {public_key}', f'AllowedIPs = {allowed}']
    if endpoint:
        lines.append(f'Endpoint = {endpoint}')
    if keepalive:
        lines.append(f'PersistentKeepalive = {KEEPALIVE}')
    return '\n'.join(lines)


def hub_conf(state, key_file):
    """The hub's wg-quick config: it listens on the UDP port and knows every peer by its one address."""
    prefix = ipaddress.ip_network(state['subnet']).prefixlen
    head = ['[Interface]', f'Address = {state["addresses"][state["hub"]]}/{prefix}', f'ListenPort = {state["port"]}',
            f'PostUp = wg set %i private-key {key_file}']
    peers = [peer_block(state['keys'][n], f'{a}/32') for n, a in sorted(state['addresses'].items())
             if n != state['hub'] and state['keys'].get(n)]
    return '\n\n'.join(['\n'.join(head), *peers]) + '\n'


def peer_conf(state, name, key_file='', private_key=''):
    """A peer's config: its address, and the hub as its one peer for the whole subnet. A machine loads its key from its file
    (key_file); a phone's QR config carries the key itself (private_key), made for that one answer."""
    prefix = ipaddress.ip_network(state['subnet']).prefixlen
    head = ['[Interface]', f'Address = {state["addresses"][name]}/{prefix}']
    head.append(f'PrivateKey = {private_key}' if private_key else f'PostUp = wg set %i private-key {key_file}')
    return '\n'.join(head) + '\n\n' + peer_block(state['keys'][state['hub']], state['subnet'], endpoint(state), True) + '\n'


def endpoint(state):
    """Where peers reach the hub: vpn.wireguard.endpoint (a public name for away from home) or its LAN address."""
    return f'{state.get("endpoint") or state["hub_lan"]}:{state["port"]}'


def up_line(conf_path):
    """The one sudo line per Linux machine: install the config (600) and start it now and at every boot."""
    return (f'sudo install -m 600 {conf_path} /etc/wireguard/{IFACE}.conf && sudo systemctl enable --now wg-quick@{IFACE}')


def write_conf_script(path, text):
    """Writes a config on the machine (600, its own home): (script, stdin)."""
    p = path.replace('~', '$HOME', 1)
    return f'umask 077 && mkdir -p "$(dirname "{p}")" && cat > "{p}" && chmod 600 "{p}"', text


def phone_payload(state, name, pair=None):
    """The text the WireGuard app reads from the QR code. Its key pair is made here for this one answer: the hub keeps only the
    public key (in `state`); the private key is in the returned text alone."""
    priv, pub = pair or keypair()
    state['keys'][name] = pub
    return peer_conf(state, name, private_key=priv)


def qr_svg(payload, run=subprocess.run):
    """An SVG QR code of the payload from `qrencode` (on the hub), or '' when it is missing (the card then shows the text)."""
    try:
        r = run(['qrencode', '-t', 'SVG', '-o', '-'], input=payload, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ''
    return r.stdout if r.returncode == 0 and r.stdout.lstrip().startswith(('<?xml', '<svg')) else ''


# ---- the router: only WireGuard's UDP port, only after the admin's click

def router_kind(run=subprocess.run):
    """'upnp', 'natpmp' or '' (none found, or the tools are missing: then the card shows the router's own steps)."""
    for kind, argv, ok in (('upnp', ['upnpc', '-s'], 'Found valid IGD'), ('natpmp', ['natpmpc'], 'Public IP address')):
        try:
            r = run(argv, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if ok in (r.stdout or '') + (r.stderr or ''):
            return kind
    return ''


def open_port(kind, port, consent, run=subprocess.run):
    """Maps UDP `port` on the router to this computer. Refuses without consent, and maps nothing but that one UDP port."""
    if consent is not True:
        raise ConsentNeeded('the router is changed only after you click "Open the port" on the Private network card')
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError(f'port {port!r}: WireGuard\'s UDP port, 1024 to 65535')
    argv = {'upnp': ['upnpc', '-e', 'tutti-wireguard', '-r', str(port), 'UDP'],
            'natpmp': ['natpmpc', '-a', str(port), str(port), 'udp', str(NATPMP_LIFETIME)]}.get(kind)
    if not argv:
        raise ValueError(router_steps(port))
    r = run(argv, capture_output=True, text=True, timeout=30)
    return r.returncode == 0, ((r.stdout or '') + (r.stderr or '')).strip()[-300:]


def router_steps(port):
    return (f'Your router does not answer UPnP or NAT-PMP. In its admin page (often http://192.168.1.1), add a port forward: UDP '
            f'{port} to this computer\'s LAN address, nothing else. Only needed for phones and laptops away from home.')


def linux_steps(name, facts, conf_path):
    """The person's one line on a Linux machine (or Tutti runs it with passwordless sudo): install wg if missing, then up."""
    from . import vpn
    pre = '' if facts.get('wg') else vpn.install_wireguard(facts.get('os', 'linux'), facts.get('pkg', ''))[0] + ' && '
    return pre + up_line(conf_path)


def run_sudo_line(machine, line, sh):
    """With passwordless sudo, Tutti runs the line itself (sudo -n never prompts)."""
    return sh(machine, line.replace('sudo ', 'sudo -n '), capture=True, timeout=300)

