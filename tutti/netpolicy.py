"""Where a household's servers may listen (docs/HOUSEHOLD.md, *Network*): never on the internet.

The owner's rule: a household dashboard is never exposed to the internet; people use it over the LAN or the household
VPN. So the coordinator, the dashboard and `tutti mcp --http` may bind only to localhost or a private, LAN or VPN address:
RFC 1918, CGNAT 100.64.0.0/10 (Tailscale), IPv6 ULA fc00::/7, or any address of a WireGuard interface. A wildcard
(0.0.0.0, ::) would also listen on a public interface, so it is refused too; `auto` means "every private and VPN address
of this computer". A public address is refused, loudly, with the reason and the fix. Tutti never offers a public tunnel
or a port forward, and invite links and join commands only ever name a LAN or VPN address.

Pure apart from reading this computer's interfaces (`ip -j addr`) and resolving a host name."""
import ipaddress
import json
import socket
import subprocess

PRIVATE = [ipaddress.ip_network(n) for n in ('127.0.0.0/8', '::1/128', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                                               '100.64.0.0/10', 'fc00::/7', 'fe80::/10')]   # fe80: link-local, never routed
WILDCARDS = ('', '0.0.0.0', '::', '*')
VIRTUAL = ('docker', 'br-', 'veth', 'lxcbr', 'virbr', 'cni', 'flannel')   # containers' bridges: private, but nobody joins there
WG_NAMES = ('wg', 'tailscale', 'zt', 'nebula')
WHY = ('a household\'s dashboard and coordinator are never exposed to the internet: members reach them over the LAN or the '
       'household VPN (docs/HOUSEHOLD.md, *Network*)')


class PublicAddress(ValueError):
    """A bind or a link would reach the internet; the message says why and which addresses may be used instead."""


def interfaces(run=subprocess.run):
    """[(interface, kind, [addresses])] of this computer: Linux `ip -j addr`, else `ifconfig -a` (macOS, BSD); [] when
    neither can be read (then only the route's address and localhost are known)."""
    try:
        out = run(['ip', '-j', 'addr'], capture_output=True, text=True, timeout=5)
        rows = json.loads(out.stdout or '[]')
        return [(r.get('ifname', ''), (r.get('linkinfo') or {}).get('info_kind') or '',
                 [a.get('local') for a in r.get('addr_info') or [] if a.get('local')]) for r in rows]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        return parse_ifconfig(run(['ifconfig', '-a'], capture_output=True, text=True, timeout=5).stdout or '')
    except (OSError, subprocess.SubprocessError):
        return []


def parse_ifconfig(text):
    """`ifconfig -a`'s interfaces: a line `name: flags...` starts one, `inet <addr>` and `inet6 <addr>` lines add to it."""
    out = []
    for line in text.splitlines():
        if line and not line[0].isspace() and ':' in line:
            out.append((line.split(':', 1)[0], 'wireguard' if line.startswith(('utun', 'wg')) else '', []))
            continue
        words = line.split()
        if out and len(words) > 1 and words[0] in ('inet', 'inet6'):
            out[-1][2].append(words[1].split('%', 1)[0] if words[0] == 'inet6' and not words[1].startswith('fe80') else words[1])
    return out


def vpn_addresses(ifaces):
    """Addresses on WireGuard (and Tailscale, ZeroTier, Nebula) interfaces: a VPN, whatever its address range."""
    return {a for name, kind, addrs in ifaces if kind == 'wireguard' or name.startswith(WG_NAMES) for a in addrs}


def private(addr, vpn=()):
    """Whether an IP address is localhost, private, LAN or VPN."""
    if addr in vpn:
        return True
    try:
        ip = ipaddress.ip_address(addr.split('%', 1)[0])
    except ValueError:
        return False
    return any(ip in n for n in PRIVATE if n.version == ip.version)


def private_addresses(ifaces):
    """This computer's own LAN and VPN addresses (not loopback, not a container bridge), IPv4 first, then IPv6 (ULA, and
    link-local with its interface: `fe80::1%wlo1`, the only way it can be listened on)."""
    vpn = vpn_addresses(ifaces)
    out = []
    for name, _kind, addrs in ifaces:
        if name.startswith(VIRTUAL) or name == 'lo':
            continue
        for a in addrs:
            if not private(a, vpn) or a.startswith('127.') or a == '::1':
                continue
            out.append(f'{a.split("%", 1)[0]}%{name}' if a.startswith('fe80') else a)
    return sorted(out, key=lambda a: (':' in a, a.startswith('fe80'), a))


def bridged(addr, ifaces):
    """Whether an address belongs to a container bridge (docker0, br-*, veth*, lxcbr0, virbr0): private, but nobody joins
    there, and a container could reach it."""
    plain = addr.split('%', 1)[0]
    return any(plain in [x.split('%', 1)[0] for x in addrs] for name, _k, addrs in ifaces if name.startswith(VIRTUAL))


def resolve(host):
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(host, None)})
    except (OSError, UnicodeError):
        return []


def check(host, what, ifaces=None, resolve_fn=resolve):
    """Raises PublicAddress unless `host` (an address or a name) is only ever localhost, private or VPN."""
    ifaces = interfaces() if ifaces is None else ifaces
    allowed = ', '.join(private_addresses(ifaces)) or 'none found: connect this computer to the LAN or the VPN'
    if host in WILDCARDS:
        raise PublicAddress(f'{what} may not listen on every interface ("{host or "all"}"): that includes any public one, and '
                            f'{WHY}. Use auto (every LAN and VPN address of this computer) or one of: {allowed}')
    addrs = [host] if _is_ip(host) else resolve_fn(host)
    if not addrs:
        raise PublicAddress(f'{what}: "{host}" does not resolve to an address on this network; use one of: {allowed}')
    vpn = vpn_addresses(ifaces)
    public = [a for a in addrs if not private(a, vpn)]
    if not public and any(bridged(a, ifaces) for a in addrs):
        raise PublicAddress(f'{what}: "{host}" is a container bridge\'s address: no member can reach it there, and a container '
                            f'could; use a LAN or VPN address: {allowed}')
    if public:
        raise PublicAddress(f'{what}: "{host}" is a public address ({", ".join(public)}), and {WHY}. Use a LAN or VPN address '
                            f'instead: {allowed}')
    return addrs


def binds(setting, what, ifaces=None, resolve_fn=resolve, route=None):
    """The addresses a server listens on for a `bind` setting: `auto` is localhost (IPv4 and IPv6) plus every LAN and VPN
    address of this computer (IPv4, ULA, link-local) and the route's own address when it is private (what a join code
    names, also when the interfaces cannot be read); anything else is checked (a public one, a wildcard or a container
    bridge raises PublicAddress)."""
    ifaces = interfaces() if ifaces is None else ifaces
    if setting in (None, 'auto'):
        out = ['127.0.0.1', '::1', *private_addresses(ifaces)]
        if route and private(route, vpn_addresses(ifaces)) and route not in out:
            out.append(route)
        return out
    check(setting, what, ifaces, resolve_fn)
    return [setting]


def _is_ip(host):
    try:
        ipaddress.ip_address(host.split('%', 1)[0])
        return True
    except ValueError:
        return False


def errors(key, value, ifaces=None):
    """A config check of a bind setting that is an address: a public address or a wildcard is named with the fix; an
    address of this computer's WireGuard (or Tailscale) interface is a VPN address, whatever its range."""
    if value in (None, 'auto') or not isinstance(value, str):
        return [] if value in (None, 'auto') else [f'{key} is {value!r}: auto, or a LAN or VPN address of this computer']
    if value in WILDCARDS:
        return [f'{key} is "{value}": every interface includes a public one, and {WHY}; write auto instead']
    if _is_ip(value) and not private(value):
        vpn = vpn_addresses(interfaces() if ifaces is None else ifaces)
        if not private(value, vpn):
            return [f'{key} is {value}, a public address, and {WHY}; write auto or a LAN or VPN address']
    return []
