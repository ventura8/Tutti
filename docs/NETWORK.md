# Network: the LAN, your own VPN, or an optional private network

Tutti never needs a VPN. On a plain home LAN everything works: the dashboard on the LAN address, the household join, machines
over ssh and phones over adb. If you already reach your home through a VPN (the router's own VPN, OpenVPN, IPsec, WireGuard
you set up yourself), there is nothing to do: Tutti works over any VPN that gives your devices a private address. It assumes
no subnet; a client from another private range (10.8.0.6 from a router's OpenVPN, say) is just another private address.

`tutti vpn setup` is an optional convenience: a private network between the household's own devices, so laptops and phones
reach the machines when they are away from home, by stable names. It is never turned on by itself.

## Never public

The dashboard, the household coordinator and `tutti mcp --http` bind only to localhost, private, LAN or VPN addresses
(`tutti/netpolicy.py`: RFC 1918, 100.64.0.0/10, fc00::/7, any WireGuard interface). A wildcard or a public address is refused
with the reason and the fix. Tutti never offers a public tunnel:

- **Tailscale Funnel** publishes a port to the whole internet. Tutti never turns it on, `tutti vpn funnel` refuses, and the
  dashboard refuses to start while Funnel forwards to one of Tutti's ports (it names the `tailscale funnel --https=<port> off`
  that turns it off).
- **The WireGuard port forward** (only for use away from home) opens WireGuard's own UDP port and nothing else. WireGuard
  answers only packets signed by a known key, so the dashboard is still reachable only inside the VPN.

## Setting one up

```bash
tutti vpn status                 # per machine and phone: Tailscale installed and signed in, WireGuard, Funnel
tutti vpn setup --dry-run        # what setup would do here; changes nothing
tutti vpn setup                  # the suggested VPN (vpn.prefer), or --tailscale / --wireguard
tutti vpn setup --authkey nuc    # a headless machine joins Tailscale with an auth key typed hidden
tutti vpn setup --cert           # also the private https name's certificate (Tailscale, MagicDNS on)
```

Setup asks nothing. It looks at every machine (over ssh, read-only) and phone (over adb), picks the VPN, does every step it
can, and writes the few steps only a person can do into one checklist, in the best order, on the dashboard's **Private network
(optional)** card (and in the terminal). Running it again checks everything afresh and continues where it left off.

| | Tailscale | WireGuard (by Tutti) |
| --- | --- | --- |
| Tutti does | detects it everywhere; installs it with passwordless sudo (Tailscale's own script: its repository and package); runs `tailscale up` and opens each sign-in page in your browser; opens the Play Store page on each phone over adb; suggests the tailnet names | picks a free subnet; makes every key (each machine's goes straight to it over ssh, 600); writes each wg-quick config (600, no private key inside); with passwordless sudo, installs and starts it; opens the WireGuard app's Play Store page; finds UPnP or NAT-PMP on the router |
| You do | one install line per machine without passwordless sudo; one sign-in per machine (the same account), or an auth key for a headless one; on each phone: Install and sign in; MagicDNS on (one switch) | one sudo line per Linux machine without passwordless sudo (install, then start at boot); on Windows and macOS: the WireGuard app, import the file; on each phone: Install, then scan its QR code on the card; only for away from home: click **Open the port** (or the router's own page) |

Creating the Tailscale account and signing in are always your clicks; Tutti never handles a password. An auth key is read
with hidden input, sent on stdin (`--auth-key=file:/dev/stdin`), and never logged or stored.

After joining, machine entries keep the LAN as the fallback:

```yaml
machines:
  nuc: {host: nuc.your-tailnet.ts.net, lan: 192.168.1.20}   # or the WireGuard address, e.g. 10.77.77.2
```

ssh uses `host` while it answers (checked once a minute), else `lan`.

## Which one: the comparison

| | Tailscale | WireGuard by Tutti |
| --- | --- | --- |
| Setup steps | install, one sign-in per device | one sudo line per machine, one QR scan per phone |
| Router changes | none | none at home; one UDP port forward for use away from home |
| Accounts and trust | a Tailscale account (Google, Microsoft, GitHub...); its coordination server sees which devices exist, never the traffic (end-to-end WireGuard) | none; fully self-hosted |
| Behind CGNAT or a phone hotspot | works: NAT traversal, relays (DERP) as the fallback | works at home; away from home only if the hub's router has a public address (no CGNAT) and the forward |
| Speed | WireGuard; direct in most homes, slower when relayed | WireGuard, always direct |
| Phone battery | light; a little more than plain WireGuard (the control connection) | lightest (a 25 s keepalive only) |
| If the service goes down | existing connections keep working; new sign-ins and new devices wait | no outside service; the hub going down stops the VPN |

**Suggested: Tailscale.** For a typical household it needs no router change and works behind CGNAT and phone hotspots, which
many home connections and every phone have. WireGuard by Tutti is the choice for a household that wants no account and no third
party, and has a router with a public address. Headscale (a self-hosted Tailscale control server) was considered: it needs a
server reachable from outside too, so it brings back the router change while adding a service to keep running; not offered.

The choice is `vpn.prefer` (Settings, or `tutti.config.yaml`): `tailscale` or `wireguard`. It only says which one setup sets
up; neither is ever on unless you run setup.

```yaml
vpn:
  prefer: tailscale        # or wireguard
  wireguard:
    port: 51820            # UDP; the only port ever forwarded
    subnet: auto           # a free private /24 that clashes with no LAN and never with 100.64.0.0/10
    hub: nuc               # the always-on Tutti host (default: where `tutti move` put Tutti, else this computer)
    endpoint: null         # a public name for the hub, for use away from home (default: its LAN address)
```

### WireGuard details

- **Keys**: made in Python (X25519, RFC 7748) from `os.urandom`. A machine's private key is sent once, on stdin over ssh, into
  `~/.config/tutti/wireguard/tutti.key` (600, folder 700) on that machine; a key already there is kept (its public half
  read with `wg pubkey` there). Peers wait while the hub has no key yet. The config loads it
  with `PostUp = wg set %i private-key <file>`, so no config holds a private key. `state/vpn.json` holds addresses and public
  keys only.
- **Phones**: **Show QR** on the card makes a new key pair for that phone, shows it once (from `qrencode` on the hub, else
  as text) and keeps only the public key; with passwordless sudo the hub learns it at once and keeps it in its config across a restart,
  else on the next `tutti vpn setup`.
- **Router**: **Open the port** maps WireGuard's UDP port over UPnP (`upnpc`) or NAT-PMP (`natpmpc`) only after your click and
  a confirm. A NAT-PMP mapping lasts a day (click again to renew); a UPnP one stays. Without either, the card shows the
  router's own steps.

## Android: always adb

Phones stay on adb whatever the network. A device can list extra adb addresses:

```yaml
devices:
  pixel:
    serial: R58M00ABCDE                   # USB
    wireless: [192.168.1.30:5555, pixel.your-tailnet.ts.net:5555]   # LAN first, then the VPN
```

Each look and each run picks the first that adb sees as `device`: USB, then each wireless address, running `adb connect` on a
dropped one on the way (so it reconnects by itself). For wireless debugging on Android 11 and later: Developer options →
Wireless debugging → Pair device with pairing code, then `adb pair <ip:pairing-port>` and `adb connect <ip:port>` once
(`tutti devices add` prints the steps). Over the tailnet the phone needs Tailscale on and `adb tcpip 5555` once over USB.

## How it plugs into `tutti setup`

`tutti vpn setup` follows the rule of every guided setup (docs/INSTALL.md, `tutti/guided.py`): detect, pick the default, list
the person's steps once at the end (`guided.render`: sign-ins, then sudo lines, then phone taps, then the rest). For `tutti
setup`, `vpncmd.guided_setup(cfg)` returns `(choices, steps)` and changes nothing: the one choice is `vpn.prefer`, the steps
are what `tutti vpn setup` would leave. It stays optional: a household with its own VPN or a plain LAN has nothing to do.
`POST /api/vpn` (the card) is an admin's, like `/api/setup`.
