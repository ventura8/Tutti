"""The main agent's order when `main.agent` is `auto` (the default): who conducts, who answers the Chat's "Auto: main agent"
and who takes over a session's task (tutti/continuity.py), before pacing skips the ones without room.

`auto` means: Claude first when it is a worker, then `main.fallback` in its order, then every other CLI agent (another
account's agent, Cursor, Antigravity, Copilot) and last the local models. A named `main.agent` (or the owner's switch) goes
first and the same list follows. Pure: only the config is read."""

AUTO = 'auto'
PREFERRED = ('claude', 'agy', 'cursor', 'copilot')   # the usual main agents, best first, when no fallback says otherwise


def dedupe(names):
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def order(cfg, first=None):
    """Every worker that may be the main agent, in order: `first` (a named agent; `auto` or None: none), main.fallback,
    the preferred CLI agents, the other CLI agents, then the local models. Names that are not workers are dropped."""
    workers = cfg.get('workers') or {}
    main = cfg.get('main') or {}
    lead = first if first and first != AUTO else (main.get('agent') if main.get('agent') != AUTO else None)
    cli = [n for n, w in workers.items() if (w or {}).get('kind') == 'cli']
    local = [n for n, w in workers.items() if (w or {}).get('kind') == 'ollama']
    pref = [n for n in PREFERRED if n in cli]
    names = [lead or (pref[:1] or [None])[0], *(main.get('fallback') or []), *pref, *cli, *local]
    return [n for n in dedupe(names) if n in workers]


def primary(cfg):
    """The agent the owner prompts by default: main.agent, or with `auto` the first of the order (None without workers)."""
    agent = (cfg.get('main') or {}).get('agent')
    if agent and agent != AUTO:
        return agent
    names = order(cfg)
    return names[0] if names else None
