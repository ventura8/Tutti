"""Fair shares of the household's shared machines (docs/HOUSEHOLD.md, *Fair use of shared machines*), pure.

The coordinator turns every member's demand on a shared machine into grants: how many of its slots each member's runs may
hold now. Interactive demand (a member prompting now, or a run started by hand) is served before anyone's background
autopilot; the rest is shared by weighted max-min fairness (water-filling), each member's weight times one plus their
expected activity this hour (their 7 x 24 profile), so a shared GPU follows whoever is awake. A member whose demand got no
slot for `starve_minutes` takes one from the member with the most background grants. Nothing is reserved for nobody: what
no one else wants goes to whoever wants it (work-conserving).

A member's routing and autopilot then see a shared machine with `free = min(free, grant - running)`; the slot queue on the
machine stays first come, first served within the grants."""
import json
import os

STALE_SECONDS = 300      # a member's demand older than this counts as none; grants older than this are not used
CLASSES = ('interactive', 'background')


def water_fill(capacity, wants, weights):
    """Weighted max-min fair shares of `capacity` whole slots: {user: slots}, never more than a user wants. Ties go to the
    larger weight, then the user name, so the answer is stable."""
    got = dict.fromkeys(wants, 0)
    left = int(capacity)
    while left > 0:
        hungry = [u for u in wants if got[u] < wants[u]]
        if not hungry:
            break
        # the next slot to whoever is furthest below their weighted share
        u = min(hungry, key=lambda x: ((got[x] + 1) / max(weights.get(x, 1.0), 1e-9), -weights.get(x, 1.0), x))
        got[u] += 1
        left -= 1
    return got


def grants(capacity, demands, weights, starved=None):
    """{user: slots} on one machine. demands: {user: {'interactive': n, 'background': n}} (running plus waiting); weights:
    {user: weight}; starved: the users whose demand had no slot for too long (they get one first)."""
    capacity = max(0, int(capacity))
    inter = {u: max(0, int(d.get('interactive', 0))) for u, d in demands.items()}
    back = {u: max(0, int(d.get('background', 0))) for u, d in demands.items()}
    first = water_fill(capacity, inter, weights)
    left = capacity - sum(first.values())
    second = water_fill(left, back, weights)
    out = {u: first.get(u, 0) + second.get(u, 0) for u in demands}
    for u in sorted(starved or ()):
        if out.get(u, 0) or not (inter.get(u) or back.get(u)):
            continue
        donors = [v for v in out if v != u and second.get(v, 0) > 0]
        if sum(out.values()) < capacity:
            out[u] = 1
        elif donors:
            rich = max(donors, key=lambda v: (second[v], v))
            out[rich] -= 1
            second[rich] -= 1
            out[u] = 1
    return out


def hour_weight(weight, profile, at):
    """A member's weight this hour: their `household.weight` x (1 + their expected activity, 0 to 1, from their profile)."""
    try:
        activity = float(profile[at.weekday()][at.hour]) if profile else 0.0
    except (IndexError, TypeError, ValueError):
        activity = 0.0
    return float(weight or 1) * (1.0 + min(1.0, max(0.0, activity)))


def normalise_profile(rows):
    """A 7 x 24 profile scaled to 0..1 (the busiest hour 1), so members' rates compare; [] when there is none."""
    try:
        peak = max(max(float(x) for x in r) for r in rows)
        if len(rows) != 7 or any(len(r) != 24 for r in rows) or peak <= 0:
            return []
        return [[round(max(0.0, float(x)) / peak, 2) for x in r] for r in rows]
    except (TypeError, ValueError):
        return []


def household_plan(profiles, at, hours=24):
    """The expected active members per hour from `at` on: [(iso hour, {name: activity})], the household's combined week
    (who the shared GPUs follow)."""
    from datetime import timedelta
    out = []
    t = at.replace(minute=0, second=0, microsecond=0)
    for _ in range(hours):
        out.append((t.isoformat(), {n: hour_weight(1, p, t) - 1 for n, p in profiles.items()}))
        t += timedelta(hours=1)
    return out


def jain(alloc, demands):
    """Jain's fairness index of the shares each member got of what it wanted (1.0 is perfectly fair); None with no demand."""
    xs = [alloc.get(u, 0) / d for u, d in demands.items() if d > 0]
    if not xs:
        return None
    if not any(xs):   # nobody got anything (a paused machine): equally so
        return 1.0
    return round(sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs)), 3)


def starving(prev, machine, demands, out, now, minutes):
    """The users whose demand on `machine` has had no slot for `minutes`; `prev` ({machine: {user: since}}) is updated."""
    since = prev.setdefault(machine, {})
    for u, d in demands.items():
        wants = int(d.get('interactive', 0)) + int(d.get('background', 0))
        if wants and not out.get(u):
            since.setdefault(u, now)
        else:
            since.pop(u, None)
    for u in [u for u in since if u not in demands]:
        del since[u]
    return {u for u, t in since.items() if now - t >= minutes * 60}


# ---- a member's side: the cached grants and what they leave free


def cache_path(folder):
    return os.path.join(folder, 'household', 'grants.json')


def save_cache(folder, machines, now, status=None, error=None, away=False):
    """The last answer this member had (the grants, and the household's status for the dashboard); a failed call keeps
    them and records why; an unreachable coordinator marks the member away (since when) until a call answers again."""
    os.makedirs(os.path.dirname(cache_path(folder)), exist_ok=True)
    old = read_cache(folder)
    if error:
        data = {**old, 'error': error, 'error_at': now, 'away_since': (old.get('away_since') or now) if away else None}
    else:
        data = {'at': now, 'machines': machines, 'status': status if status is not None else old.get('status'), 'error': None,
                'away_since': None}
    tmp = f'{cache_path(folder)}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f)
    os.replace(tmp, cache_path(folder))


def read_cache(folder):
    try:
        with open(cache_path(folder), encoding='utf8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def limit(name, free, running, cache, now, mine_shared=()):
    """(free slots this member may use on `name`, why less). Not a household machine: unchanged. A shared one: its grant
    minus what this member runs there; without a fresh grant another member's machine is not used and this member's own
    shared machine keeps its last grant (docs/HOUSEHOLD.md, *When the coordinator cannot be reached*)."""
    m = (cache.get('machines') or {}).get(name)
    shared = m is not None or name in mine_shared
    if not shared:
        return free, ''
    fresh = now - (cache.get('at') or 0) <= STALE_SECONDS
    if not m or (not fresh and not m.get('mine')):
        return 0, 'household share unknown: the coordinator was not reached lately'
    if m.get('paused'):
        return 0, f'paused for the household by {m.get("paused_by") or "its owner"}'
    room = max(0, int(m.get('grant', 0)) - running)
    if room < free:
        busy = ', '.join(f'{n} {k}' for n, k in sorted((m.get('use') or {}).items()) if k) or 'others'
        return room, f'household share: {m.get("grant", 0)} of {m.get("slots", "?")} slot(s) yours now (busy for {busy})'
    return free, ''
