"""Local models: what each machine running them serves, the power it draws and the estimated energy cost (docs/ENERGY.md).

Readings come from POWER_SCRIPT on the machine (nvidia-smi, rocm-smi, xpu-smi, intel_gpu_top, the hwmon and RAPL energy
counters) and from Ollama's /api/ps and /api/tags. Where nothing is readable the power is estimated from the GPU's known TDP
and its utilisation. Every figure is an estimate; the card says which parts were measured. Pure: text and numbers in, plain
dicts out; the I/O is remote.power_readings, local.ollama_get and the collector."""
import functools
import json
import os
import re
import shlex
import time
from datetime import datetime, timedelta

from . import config

# Typical household electricity price by country: local currency per kWh, all taxes included, rounded. A starting point only:
# set your own in Settings (energy.price_per_kwh), and the card stops saying "estimated".
PRICE_SOURCE = ('Eurostat nrg_pc_204 (households, 2 500-4 999 kWh a year, all taxes, 2025 S1), the US EIA residential average '
                '(2025) and national regulators (ANRE for Romania, Ofgem for Great Britain); rounded, checked 2026-11')
PRICES = {'RO': ('RON', 1.40), 'DE': ('EUR', 0.39), 'FR': ('EUR', 0.26), 'IT': ('EUR', 0.33), 'ES': ('EUR', 0.25), 'NL': ('EUR', 0.27),
          'BE': ('EUR', 0.35), 'AT': ('EUR', 0.30), 'IE': ('EUR', 0.37), 'PT': ('EUR', 0.24), 'GR': ('EUR', 0.23), 'BG': ('EUR', 0.13),
          'FI': ('EUR', 0.22), 'PL': ('PLN', 1.05), 'HU': ('HUF', 36.0), 'CZ': ('CZK', 6.5), 'MD': ('MDL', 3.8), 'GB': ('GBP', 0.26),
          'CH': ('CHF', 0.32), 'SE': ('SEK', 2.0), 'DK': ('DKK', 2.4), 'NO': ('NOK', 1.5), 'US': ('USD', 0.17), 'CA': ('CAD', 0.18),
          'AU': ('AUD', 0.33), 'JP': ('JPY', 31.0), 'IN': ('INR', 8.0)}
FALLBACK = ('EUR', 0.29)   # the EU average, for a country the table does not know
# Approximate units of each currency per US dollar (checked 2026-11), only for the cloud comparison below
PER_USD = {'USD': 1.0, 'EUR': 0.86, 'RON': 4.38, 'GBP': 0.76, 'PLN': 3.66, 'HUF': 335.0, 'CZK': 21.0, 'MDL': 17.0, 'CHF': 0.80,
           'SEK': 9.5, 'DKK': 6.4, 'NOK': 10.1, 'CAD': 1.39, 'AUD': 1.53, 'JPY': 152.0, 'INR': 88.0}
# The same tokens on a cloud API: list prices in US dollars per million tokens (input, output), checked 2026-11
CLOUD_SOURCE = 'the providers\' public API price pages (anthropic.com/pricing, ai.google.dev/pricing, openai.com/api/pricing), 2026-11'
CLOUD = {'claude-sonnet': ('Claude Sonnet', 3.0, 15.0), 'claude-haiku': ('Claude Haiku', 1.0, 5.0),
         'gemini-flash': ('Gemini Flash', 0.30, 2.50), 'gpt-mini': ('GPT mini', 0.25, 2.0)}
# Typical board power (W) of GPUs Tutti meets, for the estimate when nothing reads the power; machines.<m>.gpu_tdp_w overrides
TDP = (('5090', 575), ('4090', 450), ('3090', 350), ('3080 laptop', 130), ('3080', 320), ('4080', 320), ('3070', 220),
       ('a770', 225), ('a750', 225), ('b580', 190), ('arc', 28), ('radeon', 250))
IDLE_SHARE = 0.1        # a loaded but idle GPU draws about a tenth of its TDP
KEEP = 40               # days of buckets kept
BUSY_FILE, RUNS_FILE = 'energy.json', 'local-runs.jsonl'
HOST_PORT = re.compile(r'^[A-Za-z0-9.-]+:\d{1,5}$')
POWER_Q = '--query-gpu=name,power.draw,utilization.gpu,power.limit --format=csv,noheader,nounits'


def power_script(ollama=''):
    """The read-only bash that prints a machine's power readings in sections (`== name`); with `ollama` (host:port on that
    machine) also its /api/ps and /api/tags."""
    snap = ('snap() { echo "t $(date +%s%N)"; for h in /sys/class/hwmon/hwmon*; do n=$(cat "$h/name" 2>/dev/null); case "$n" in '
            'xe|i915|amdgpu) echo "hwmon $n $(cat "$h/energy1_input" 2>/dev/null || echo -) '
            '$(cat "$h/power1_input" 2>/dev/null || cat "$h/power1_average" 2>/dev/null || echo -)";; esac; done; '
            'echo "rapl $(cat /sys/class/powercap/intel-rapl:0/energy_uj 2>/dev/null || echo -)"; }; ')
    out = (f'echo "== nvidia"; {{ nvidia-smi {POWER_Q} 2>/dev/null || /usr/lib/wsl/lib/nvidia-smi {POWER_Q} 2>/dev/null; }}; '
           'echo "== rocm"; command -v rocm-smi >/dev/null && timeout 10 rocm-smi --showpower --showuse --json 2>/dev/null; echo; '
           'echo "== xpu"; command -v xpu-smi >/dev/null && timeout 10 xpu-smi dump -d 0 -m 0,1 -n 1 2>/dev/null; '
           'echo "== intel"; command -v intel_gpu_top >/dev/null && '
           'timeout 3 intel_gpu_top -J -s 1000 -o - 2>/dev/null | head -c 20000; echo; '
           f'{snap}echo "== a"; snap; sleep 1; echo "== b"; snap; ')
    if ollama and HOST_PORT.match(ollama):   # errors() refuses anything else; never a shell word from the config
        ps, tags = (shlex.quote(config.ollama_url(ollama, p)) for p in ('/api/ps', '/api/tags'))   # loopback on that machine
        out += f'echo "== ps"; curl -s --max-time 3 {ps}; echo; echo "== tags"; curl -s --max-time 3 {tags}; echo; '
    return out + 'echo "== end"'


def sections(text):
    out, name = {}, None
    for line in (text or '').splitlines():
        if line.startswith('== '):
            name = line[3:].strip()
            out[name] = []
        elif name:
            out[name].append(line)
    return {k: '\n'.join(v).strip() for k, v in out.items()}


def _num(v):
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def parse_nvidia(text):
    """nvidia-smi's csv (name, power.draw, utilization.gpu, power.limit; one line per GPU): watts summed, the busiest GPU's %."""
    gpus = [[c.strip() for c in line.split(',')] for line in (text or '').splitlines() if line.count(',') >= 2]
    if not gpus:
        return None
    watts = [_num(g[1]) for g in gpus]
    top = max(gpus, key=lambda g: _num(g[2]) or 0)   # the busiest GPU names the card and its limit
    util = _num(top[2]) or 0
    if all(w is None for w in watts):
        return {'gpu': top[0], 'gpu_w': None, 'util': util, 'source': 'nvidia-smi (no power reading)'}
    return {'gpu': top[0], 'gpu_w': sum(w or 0 for w in watts), 'util': util,
            'limit_w': _num(top[3]) if len(top) > 3 else None, 'source': 'nvidia-smi'}


def parse_rocm(text):
    """rocm-smi --showpower --showuse --json: every card's package power (W) summed, the busiest card's GPU use (%)."""
    try:
        cards = json.loads(text or '')
    except ValueError:
        return None
    watts, util = [], 0.0
    for card in (cards.values() if isinstance(cards, dict) else []):
        if not isinstance(card, dict):
            continue
        watts += [_num(v) for k, v in card.items() if 'Power (W)' in k and _num(v) is not None][:1]
        util = max([util] + [_num(v) or 0 for k, v in card.items() if 'GPU use' in k])
    return {'gpu': 'AMD GPU', 'gpu_w': sum(watts), 'util': util, 'source': 'rocm-smi'} if watts else None


def parse_xpu(text):
    """xpu-smi dump's csv (Timestamp, DeviceId, GPU Utilization (%), GPU Power (W)): the last row."""
    rows = [[c.strip() for c in line.split(',')] for line in (text or '').splitlines() if ',' in line]
    if len(rows) < 2:
        return None
    head, last = rows[0], rows[-1]
    col = {h: i for i, h in enumerate(head)}
    power = next((_num(last[i]) for h, i in col.items() if 'Power' in h and i < len(last)), None)
    util = next((_num(last[i]) for h, i in col.items() if 'Utilization' in h and i < len(last)), None)
    return {'gpu': 'Intel GPU', 'gpu_w': power, 'util': util or 0, 'source': 'xpu-smi'} if power is not None else None


def parse_intel(text):
    """intel_gpu_top -J: the last sample's GPU power (W) and its busiest engine (%). Its first sample is often all zeros."""
    samples = [s for s in re.split(r'"period"\s*:', text or '')[1:] if '"power"' in s]
    if not samples:
        return None
    last = samples[-1]
    m = re.search(r'"power"\s*:\s*\{[^}]*?"GPU"\s*:\s*([\d.]+)', last)
    busy = [float(b) for b in re.findall(r'"busy"\s*:\s*([\d.]+)', last)]
    return {'gpu': 'Intel GPU', 'gpu_w': float(m.group(1)), 'util': max(busy or [0]), 'source': 'intel_gpu_top'} if m else None


def _snap(text):
    out = {'t': None, 'hwmon': {}, 'rapl': None}
    for line in (text or '').splitlines():
        parts = line.split()
        if parts[:1] == ['t'] and len(parts) == 2:
            out['t'] = _num(parts[1])
        elif parts[:1] == ['hwmon'] and len(parts) == 4:
            out['hwmon'][parts[1]] = (_num(parts[2]), _num(parts[3]))
        elif parts[:1] == ['rapl'] and len(parts) == 2:
            out['rapl'] = _num(parts[1])
    return out


def parse_counters(a_text, b_text):
    """The hwmon GPU power (xe, i915, amdgpu: energy counters 1 s apart, else power1_input) and the RAPL package power, in W."""
    a, b = _snap(a_text), _snap(b_text)
    dt = (b['t'] - a['t']) / 1e9 if a['t'] and b['t'] and b['t'] > a['t'] else 1.0
    out = {}
    for name, (e1, p1) in b['hwmon'].items():
        e0 = (a['hwmon'].get(name) or (None, None))[0]
        if e0 is not None and e1 is not None and e1 >= e0:
            out = {'gpu': name, 'gpu_w': (e1 - e0) / 1e6 / dt, 'source': f'hwmon {name}'}
        elif p1 is not None:
            out = {'gpu': name, 'gpu_w': p1 / 1e6, 'source': f'hwmon {name}'}
    if a['rapl'] is not None and b['rapl'] is not None and b['rapl'] >= a['rapl']:
        out['cpu_w'] = (b['rapl'] - a['rapl']) / 1e6 / dt
    return out


def parse_power(text):
    """Everything POWER_SCRIPT printed: {gpu, gpu_w, util, source, cpu_w, ps, tags}; gpu_w None when nothing measured it."""
    s = sections(text)
    gpu = next((g for g in (parse_nvidia(s.get('nvidia')), parse_rocm(s.get('rocm')), parse_xpu(s.get('xpu')),
                            parse_intel(s.get('intel'))) if g and g.get('gpu_w') is not None), None)
    counters = parse_counters(s.get('a'), s.get('b'))
    out = gpu or {k: counters[k] for k in ('gpu', 'gpu_w', 'source') if k in counters} or \
        parse_nvidia(s.get('nvidia')) or {'gpu': None, 'gpu_w': None, 'util': None, 'source': ''}
    out = {'util': None, **out, 'cpu_w': counters.get('cpu_w')}
    for k in ('ps', 'tags'):
        try:
            out[k] = json.loads(s[k]) if s.get(k) else None
        except ValueError:
            out[k] = None
    return out


# ---- the user's currency and price


def zone_country(zone, zone_tab=''):
    """The ISO country of a time zone (Europe/Bucharest -> RO) from zone.tab's lines `RO\\t+4426+02606\\tEurope/Bucharest`."""
    for line in (zone_tab or '').splitlines():
        parts = line.split('\t')
        if len(parts) >= 3 and not line.startswith('#') and parts[2] == zone:
            return parts[0]
    return ''


def locale_country(value):
    """ro_RO.UTF-8 -> RO; C, POSIX and en (no territory) -> ''."""
    m = re.match(r'^[a-z]{2,3}_([A-Z]{2})\b', value or '')
    return m.group(1) if m else ''


def detect_country(env, zone='', zone_tab='', setting=''):
    """(country, how it was found). The setting first; then the time zone (where the computer is: a Romanian owner may well run
    an en_US or en_GB locale); then the monetary locale, then the general one."""
    if setting:
        return setting.upper(), 'the energy.country setting'
    c = zone_country(zone, zone_tab)
    if c:
        return c, f'the time zone {zone}'
    for key in ('LC_MONETARY', 'LC_ALL', 'LANG'):
        c = locale_country(env.get(key, ''))
        if c:
            return c, f'the locale {key}={env[key]}'
    return '', 'nothing: no time zone or locale names a country'


def tariff(cfg, env=None, zone=None, zone_tab=None):
    """{country, how, currency, price, estimated, source, periods}: the price per kWh in the user's currency."""
    e = cfg.get('energy') or {}
    if zone is None:
        zone, zone_tab = system_zone()
    country, how = detect_country(env if env is not None else os.environ, zone, zone_tab or '', e.get('country') or '')
    home, price = PRICES.get(country, FALLBACK)
    currency = (e.get('currency') or home).upper()
    if currency != home:   # another currency without a price of its own: the typical price converted, or the EU average
        rate = PER_USD.get(currency) and PER_USD.get(home)
        price = round(price * PER_USD[currency] / PER_USD[home], 3) if rate else FALLBACK[1]
        currency = currency if rate else FALLBACK[0]
    own = e.get('price_per_kwh')
    return {'country': country, 'how': how, 'currency': currency, 'price': float(own) if own else price, 'estimated': not own,
            'source': 'your setting (energy.price_per_kwh)' if own else (PRICE_SOURCE if country in PRICES else
                                                                         'the EU average: your country is not in the table'),
            'periods': list(e.get('tariff') or [])}


@functools.lru_cache(maxsize=1)
def system_zone():
    """(this computer's time zone name, zone.tab's text); empty strings where they cannot be read."""
    zone = os.environ.get('TZ', '').lstrip(':')
    if not zone:
        try:
            with open('/etc/timezone', encoding='utf8') as f:
                zone = f.read().strip()
        except OSError:
            link = os.path.realpath('/etc/localtime')
            zone = link.split('zoneinfo/', 1)[1] if 'zoneinfo/' in link else ''
    try:
        with open('/usr/share/zoneinfo/zone.tab', encoding='utf8') as f:
            return zone, f.read()
    except OSError:
        return zone, ''


def price_at(t, hour):
    """The price at a local hour (0-23): the time-of-day period that holds it, else the flat price."""
    for p in t['periods']:
        a, b = (int(x.strip()[:2]) for x in str(p.get('hours', '00-00')).split('-'))
        if (a <= hour < b) if a <= b else (hour >= a or hour < b):
            return float(p['price'])
    return t['price']


def cheap_now(cfg, t, local_hour):
    """'' when a local-model run may start now, else why it waits: only with energy.prefer_cheap_hours and a time-of-day
    tariff, and only while the price is more than 10 % over the day's cheapest."""
    if not (cfg.get('energy') or {}).get('prefer_cheap_hours') or not t['periods']:
        return ''
    low = min(price_at(t, h) for h in range(24))
    now = price_at(t, local_hour)
    if now <= low * 1.1:
        return ''
    nxt = next(h % 24 for h in range(local_hour + 1, local_hour + 25) if price_at(t, h % 24) <= low * 1.1)
    return (f'energy costs {now:g} {t["currency"]}/kWh now; local-model runs wait for the cheap hours from {nxt:02d}:00 '
            f'({low:g} {t["currency"]}/kWh; energy.prefer_cheap_hours)')


def errors(cfg):
    e, errs = cfg.get('energy') or {}, []
    v = e.get('price_per_kwh')
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
        errs.append(f'energy.price_per_kwh is {v!r}: write your price per kWh as a number in your currency (your bill shows it), '
                    'or leave it out for the typical one')
    if e.get('compare_with') and e['compare_with'] not in CLOUD:
        errs.append(f'energy.compare_with is {e["compare_with"]!r}: use one of {", ".join(CLOUD)}')
    for i, p in enumerate(e.get('tariff') or []):
        ok = isinstance(p, dict) and re.match(r'^\d\d(:00)?-\d\d(:00)?$', str(p.get('hours', ''))) and \
            isinstance(p.get('price'), (int, float))
        if not ok:
            errs.append(f'energy.tariff[{i}] is {p!r}: write it as {{hours: "23-07", price: 0.9}} (local hours, price per kWh)')
    for name, m in (e.get('machines') or {}).items():
        for k in ('base_w', 'gpu_tdp_w', 'cpu_tdp_w'):
            v = (m or {}).get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                errs.append(f'energy.machines.{name}.{k} is {v!r}: write a number of watts (0 or more)')
        o = (m or {}).get('ollama')
        if o and not HOST_PORT.match(str(o)):
            errs.append(f'energy.machines.{name}.ollama is {o!r}: write the Ollama address on that machine as host:port, '
                        'e.g. 127.0.0.1:11434')
    return errs


# ---- power and energy


def tdp(gpu_name, own=None):
    if own:
        return float(own)
    name = (gpu_name or '').lower()
    return float(next((w for key, w in TDP if key in name), 100))


def watts(reading, mcfg, busy, on_gpu=True):
    """(whole-system watts, {gpu, cpu, base}, measured parts). The GPU's measured power, else its TDP x utilisation (busy means
    about 90 % when no utilisation is readable); the CPU's RAPL package power, else a share of its TDP (most of it when the
    model runs on the CPU); plus the machine's base draw."""
    r, measured = reading or {}, []
    if r.get('gpu_w') is not None:
        gpu = r['gpu_w']
        measured.append(f'GPU ({r.get("source")})')
    else:
        full = tdp(r.get('gpu') or mcfg.get('gpu'), mcfg.get('gpu_tdp_w'))
        util = r.get('util') if r.get('util') is not None else (90 if busy and on_gpu else 0)
        gpu = full * (IDLE_SHARE + (1 - IDLE_SHARE) * util / 100)
    if r.get('cpu_w') is not None:
        cpu = r['cpu_w']
        measured.append('CPU (RAPL)')
    else:
        cpu = float(mcfg.get('cpu_tdp_w') or 65) * (0.9 if busy and not on_gpu else 0.3 if busy else 0.1)
    base = float(mcfg.get('base_w') if mcfg.get('base_w') is not None else 30)
    return gpu + cpu + base, {'gpu': round(gpu, 1), 'cpu': round(cpu, 1), 'base': base}, measured


def integrate(samples, start, end, fallback_w=0.0):
    """Wh between start and end (epoch seconds) from samples [(t, watts)]: each sample's power holds until the next one; before
    the first sample its power holds too. With no sample, fallback_w for the whole span."""
    if end <= start:
        return 0.0
    pts = sorted(samples)
    if not pts:
        return fallback_w * (end - start) / 3600
    wh = 0.0
    for i, (t, w) in enumerate(pts):
        a = start if i == 0 else max(start, t)
        b = min(end, pts[i + 1][0]) if i + 1 < len(pts) else end
        if b > a:
            wh += w * (b - a) / 3600
    return wh


def _bucket(store, machine, day):
    days = store.setdefault(machine, {'last': None, 'days': {}})['days']
    return days.setdefault(day, {'busy_s': 0.0, 'idle_s': 0.0, 'busy_wh': [0.0] * 24, 'idle_wh': [0.0] * 24})


def record(store, machine, now, w, state, max_gap, local=time.localtime):
    """Adds the time since the machine's last sample (at most max_gap) at `w` watts to today's bucket as busy (a Tutti run is
    on it) or idle (Tutti keeps a model loaded), or nothing ('off'). Drops buckets older than KEEP days."""
    entry = store.setdefault(machine, {'last': None, 'days': {}})
    last, entry['last'] = entry['last'], now
    if last is None or state == 'off' or now <= last:
        return store
    dt = min(now - last, max_gap)
    lt = local(now)
    b = _bucket(store, machine, time.strftime('%Y-%m-%d', lt))
    b[f'{state}_s'] += dt
    b[f'{state}_wh'][lt.tm_hour] += w * dt / 3600
    cutoff = time.strftime('%Y-%m-%d', local(now - KEEP * 86400))
    entry['days'] = {d: v for d, v in entry['days'].items() if d >= cutoff}
    return store


def load_store(cfg):
    try:
        with open(os.path.join(config.state_dir(cfg), BUSY_FILE), encoding='utf8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_store(cfg, store):
    path = os.path.join(config.state_dir(cfg), BUSY_FILE)
    with open(path + '.tmp', 'w', encoding='utf8') as f:
        json.dump(store, f)
    os.replace(path + '.tmp', path)


def log_run(cfg, worker, start, end, answer):
    """One line per local-model call in <state>/local-runs.jsonl: the worker, its model, when and the tokens Ollama counted."""
    entry = {'worker': worker, 'start': start, 'end': end, 'model': (answer or {}).get('model'),
             'out': (answer or {}).get('eval_count') or 0, 'in': (answer or {}).get('prompt_eval_count') or 0}
    try:
        with open(os.path.join(config.state_dir(cfg), RUNS_FILE), 'a', encoding='utf8') as f:
            f.write(json.dumps(entry) + '\n')
    except OSError:
        pass   # the answer matters more than its log line
    return entry


def read_runs(cfg, since):
    out = []
    try:
        with open(os.path.join(config.state_dir(cfg), RUNS_FILE), encoding='utf8') as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get('end', 0) >= since:
                    out.append(e)
    except OSError:
        pass
    return out


def in_flight(cfg):
    """{worker: start} of the local-model calls running now (their marker files, tutti/local.py)."""
    out = {}
    folder = config.state_dir(cfg)
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        if n.startswith('local-run-'):
            try:
                with open(os.path.join(folder, n), encoding='utf8') as f:
                    w, _, start = f.read().partition(' ')
                out[w] = float(start or 0)
            except (OSError, ValueError):
                continue
    return out


# ---- the cards


def hosts(cfg):
    """The machines that run local models: {machine: {workers, ollama (host:port seen from here, or None), on (host:port on the
    machine, for its own curl)}}. From the ollama workers (their machine, else this computer) and energy.machines.<m>.ollama."""
    here = next((n for n, m in cfg['machines'].items() if m['transport'] == 'local'), 'this computer')
    out = {}
    for name, w in cfg['workers'].items():
        if w.get('kind') == 'ollama' and w.get('enabled') is not False:
            h = out.setdefault(w.get('machine') or here, {'workers': [], 'ollama': None, 'on': ''})
            h['workers'].append(name)
            h['ollama'] = h['ollama'] or w['host']
    for name, m in ((cfg.get('energy') or {}).get('machines') or {}).items():
        if (m or {}).get('ollama'):
            out.setdefault(name, {'workers': [], 'ollama': None, 'on': ''})['on'] = m['ollama']
    return out


def availability(item):
    """(paused, yielding, thermal, offline or available; the reason) from the machine's dashboard row."""
    if not item:
        return 'available', 'this computer'
    reason = item.get('reason') or ''
    if item.get('owner_paused'):
        return 'paused', reason
    if item.get('paused'):
        return 'yielding', reason
    if item.get('status') == 'offline':
        return 'offline', reason
    if re.search(r'\d ?C\b|heat|hot', reason):
        return 'thermal', reason
    return 'available', reason


def _period_sums(store_days, t, days):
    busy_s = idle_s = cost = kwh = 0.0
    for d in days:
        b = store_days.get(d)
        if not b:
            continue
        busy_s, idle_s = busy_s + b['busy_s'], idle_s + b['idle_s']
        for h in range(24):
            wh = b['busy_wh'][h] + b['idle_wh'][h]
            kwh += wh / 1000
            cost += wh / 1000 * price_at(t, h)
    return {'busy_s': round(busy_s), 'idle_s': round(idle_s), 'kwh': round(kwh, 3), 'cost': round(cost, 2)}


def periods(now):
    """{today, week (the last 7 days), month (this calendar month)}: the local dates each covers."""
    d = datetime.fromtimestamp(now).date()
    return {'today': [d.isoformat()], 'week': [(d - timedelta(days=i)).isoformat() for i in range(7)],
            'month': [(d - timedelta(days=i)).isoformat() for i in range(d.day)]}


def cloud_cost(cfg, t, tokens_in, tokens_out):
    key = (cfg.get('energy') or {}).get('compare_with') or 'claude-sonnet'
    label, pin, pout = CLOUD.get(key, CLOUD['claude-sonnet'])
    usd = tokens_in / 1e6 * pin + tokens_out / 1e6 * pout
    rate = PER_USD.get(t['currency'])
    return {'model': label, 'cost': round(usd * rate, 2) if rate else None, 'usd': round(usd, 2), 'source': CLOUD_SOURCE}


def card(cfg, name, host, reading, item, store, runs, now, t, busy_now):
    """One machine's local-models card: models, busy time, tokens, jobs, power, energy and cost by period."""
    mcfg = ((cfg.get('energy') or {}).get('machines') or {}).get(name) or {}
    r = reading or {}
    ps = [m for m in ((r.get('ps') or {}).get('models') or []) if isinstance(m, dict)]
    on_gpu = not ps or any(m.get('size_vram') for m in ps)
    sys_w, parts, measured = watts(r, mcfg, busy_now, on_gpu)
    days = (store.get(name) or {}).get('days') or {}
    busy_wh, busy_s = sum(sum(b['busy_wh']) for b in days.values()), sum(b['busy_s'] for b in days.values())
    # cost per busy hour: the measured busy average once there is one, else the estimate for a busy machine
    busy_w = busy_wh * 3600 / busy_s if busy_s >= 60 else watts({'gpu': r.get('gpu')}, mcfg, True, on_gpu)[0]
    per = {k: _period_sums(days, t, v) for k, v in periods(now).items()}
    mine = [x for x in runs if x.get('worker') in host['workers']]
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    for k, since in (('today', midnight.timestamp()), ('week', (midnight - timedelta(days=6)).timestamp()),
                     ('month', midnight.replace(day=1).timestamp())):
        rs = [x for x in mine if x.get('end', 0) >= since]
        per[k].update({'jobs': len(rs), 'tokens': sum(x.get('out') or 0 for x in rs),
                       'run_s': round(sum(x['end'] - x['start'] for x in rs)),
                       'cloud': cloud_cost(cfg, t, sum(x.get('in') or 0 for x in rs), sum(x.get('out') or 0 for x in rs))})
    m = per['month']
    hour_price = price_at(t, time.localtime(now).tm_hour)
    status, why = availability(item)
    if reading is None and status == 'available':
        status, why = 'offline', 'its power and models were not read: the machine did not answer over ssh (`tutti machines`)'
    return {'name': name, 'workers': host['workers'], 'gpu': r.get('gpu') or mcfg.get('gpu') or 'GPU not read',
            'installed': sorted(x.get('name') for x in ((r.get('tags') or {}).get('models') or []) if isinstance(x, dict)),
            'loaded': [{'name': x.get('name'), 'on_gpu': bool(x.get('size_vram'))} for x in ps], 'busy_now': busy_now,
            'util': r.get('util'), 'watts': round(sys_w), 'parts': parts, 'measured': measured, 'source': r.get('source') or '',
            'status': status, 'reason': why, 'periods': per,
            'per_job': round(m['cost'] / m['jobs'], 3) if m['jobs'] else None,
            'per_busy_hour': round(busy_w / 1000 * hour_price, 2), 'reachable': reading is not None}


def view(cfg, readings, machine_items, now, t=None, store=None, runs=None):
    """The dashboard's Local models category: one card per machine running local models, the tariff and how it was found."""
    t = t or tariff(cfg)
    store = load_store(cfg) if store is None else store
    runs = read_runs(cfg, now - 32 * 86400) if runs is None else runs
    live = in_flight(cfg)
    items = {m['name']: m for m in machine_items or []}
    cards = []
    for name, h in hosts(cfg).items():
        busy = any(w in live for w in h['workers'])
        cards.append(card(cfg, name, h, (readings or {}).get(name), items.get(name), store, runs, now, t, busy))
    wait = cheap_now(cfg, t, time.localtime(now).tm_hour)
    return {'cards': cards, 'tariff': t, 'cheap_wait': wait, 'include_idle': (cfg.get('energy') or {}).get('include_idle', True),
            'method': 'docs/ENERGY.md'}


def sample(cfg, readings, now, max_gap):
    """After each probe: every local-model machine's power added to its buckets (busy while a Tutti run is on it; idle while a
    model of its workers stays loaded within the keep-alive after a Tutti run, if energy.include_idle)."""
    store, live = load_store(cfg), in_flight(cfg)
    runs = read_runs(cfg, now - 3600)
    keep = float((cfg.get('energy') or {}).get('keep_alive_minutes', 5)) * 60
    for name, h in hosts(cfg).items():
        r = (readings or {}).get(name)
        busy = any(w in live for w in h['workers'])
        ended = max((x['end'] for x in runs if x.get('worker') in h['workers']), default=0)
        loaded = bool(((r or {}).get('ps') or {}).get('models'))
        idle = loaded and now - ended <= keep and (cfg.get('energy') or {}).get('include_idle', True)
        state = 'busy' if busy else 'idle' if idle else 'off'
        mcfg = ((cfg.get('energy') or {}).get('machines') or {}).get(name) or {}
        w, _, _ = watts(r, mcfg, busy)
        record(store, name, now, w, state, max_gap)
    save_store(cfg, store)
    return store


def probe_all(cfg, power_fn, ollama_fn):
    """{machine: reading or None} for every machine running local models, read at once: power over its ssh (power_fn), the
    models over the worker's host from here (ollama_fn), or the machine's own curl (energy.machines.<m>.ollama)."""
    from concurrent.futures import ThreadPoolExecutor
    found = hosts(cfg)

    def one(name):
        h = found[name]
        machine = cfg['machines'].get(name) or config.machine_of(cfg, name, {'host': 'local'})
        r = power_fn(machine, h['on'])
        if r is not None and h['ollama']:
            r['ps'], r['tags'] = ollama_fn(h['ollama'], '/api/ps'), ollama_fn(h['ollama'], '/api/tags')
        return name, r
    if not found:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(found))) as pool:
        return dict(pool.map(one, found))
