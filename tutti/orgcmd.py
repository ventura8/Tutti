"""`tutti org`: manage organizations, teams, roles, priorities and budgets from the shell."""
import sys

from . import config, orgs

ACTIONS = ('list', 'create', 'delete', 'member', 'team', 'budget')


def _say(msg):
    sys.stderr.write(f'tutti: {msg}\n')


def _print_list(store, out):
    all_orgs = store.all()
    if not all_orgs:
        print('no organizations configured yet (`tutti org create <id> --name "<Name>"`)', file=out)
        return 0
    for oid, o in sorted(all_orgs.items()):
        print(f'{oid}: {o.get("name")} ({len(o.get("members") or {})} members, {len(o.get("teams") or {})} teams)', file=out)
        for tid, t in sorted((o.get('teams') or {}).items()):
            b = t.get('budget') or {}
            btxt = ', '.join(f'{k}={v}' for k, v in sorted(b.items())) or 'unlimited'
            print(f'  team {tid} ({t.get("name")}): priority={t.get("priority", 10)}, budget=[{btxt}]', file=out)
    return 0


def _budget_from_args(a):
    budget = {}
    if getattr(a, 'daily_runs', None) is not None:
        budget['daily_runs'] = a.daily_runs
    if getattr(a, 'monthly_usd', None) is not None:
        budget['monthly_usd'] = a.monthly_usd
    if getattr(a, 'max_parallel', None) is not None:
        budget['max_parallel'] = a.max_parallel
    return budget


def _run_action(store, a, out):
    if a.action == 'list':
        return _print_list(store, out)
    if a.action == 'create':
        org = store.create_org(a.org_id, a.name or a.org_id, owner_sub=a.owner)
        print(f'created organization {org["id"]} ({org["name"]})', file=out)
        return 0
    if a.action == 'delete':
        store.delete_org(a.org_id)
        print(f'deleted organization {a.org_id}', file=out)
        return 0
    if a.action == 'member':
        teams = [t.strip() for t in (a.teams or '').split(',') if t.strip()] if a.teams is not None else None
        store.set_member(a.org_id, a.target, a.role, teams=teams)
        print(f'set {a.target} as {a.role} in {a.org_id}', file=out)
        return 0
    if a.action == 'team':
        projects = [p.strip() for p in (a.projects or '').split(',') if p.strip()] if a.projects is not None else None
        store.upsert_team(a.org_id, a.target, a.name or a.target, priority=a.priority, budget=_budget_from_args(a), projects=projects)
        print(f'saved team {a.target} in {a.org_id} (priority={a.priority})', file=out)
        return 0
    org = store.get(a.org_id)
    team = (org or {}).get('teams', {}).get(a.target)
    if not team:
        raise orgs.OrgError(f'no such team "{a.target}" in organization "{a.org_id}"')
    st = orgs.budget_status(team, used_runs=a.used_runs, used_usd=a.used_usd)
    print(f'{a.org_id}/{a.target}: ok={st["ok"]} remaining_runs={st["remaining_runs"]} remaining_usd={st["remaining_usd"]}', file=out)
    return 0 if st['ok'] else 1


def cmd_org(cfg, a, out=None):
    out = out or sys.stdout
    store = orgs.Orgs(config.state_dir(cfg))
    try:
        return _run_action(store, a, out)
    except orgs.OrgError as e:
        _say(str(e))
        return 2
