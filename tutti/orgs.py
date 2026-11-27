"""Organizations, teams, roles, priorities and budgets for small businesses and households (docs/AUTH.md).

- Stored in <state>/orgs.json (mode 600, atomic replace, under auth.locked).
- Roles: owner > admin > member > viewer. Only owners and admins may manage orgs, teams, members, priorities and budgets;
  only an owner may assign or remove the owner role or delete an org. The last owner of an org is never demoted or removed.
- Teams declare a priority (1..100, higher runs first) and optional budgets (daily_runs, monthly_usd, max_parallel).

Pure apart from the small JSON file and lock shared with tutti/auth.py."""
import os
import re
import time

from . import auth

ORGS_FILE = 'orgs.json'
ORG_ROLES = ('viewer', 'member', 'admin', 'owner')
MANAGE_ROLES = ('admin', 'owner')
SLUG_RX = re.compile(r'^[a-z0-9][a-z0-9_-]{0,47}$')
MIN_PRIORITY, MAX_PRIORITY, DEFAULT_PRIORITY = 1, 100, 10


class OrgError(Exception):
    """An organization, team, role or budget change was refused; the message names the cause and fix."""


def valid_slug(value):
    return isinstance(value, str) and bool(SLUG_RX.fullmatch(value))


def _check_slug(value, kind):
    if not valid_slug(value):
        raise OrgError(f'invalid {kind} id "{value}": use 1-48 lowercase letters, digits, "-" or "_"')
    return value


def _check_priority(value):
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_PRIORITY <= value <= MAX_PRIORITY:
        raise OrgError(f'team priority must be an integer between {MIN_PRIORITY} and {MAX_PRIORITY}')
    return value


def _check_budget(budget):
    if budget is None:
        return {}
    if not isinstance(budget, dict):
        raise OrgError('team budget must be a mapping of daily_runs, monthly_usd and/or max_parallel')
    out = {}
    if budget.get('daily_runs') is not None:
        v = budget['daily_runs']
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise OrgError('budget.daily_runs must be a non-negative integer')
        out['daily_runs'] = v
    if budget.get('monthly_usd') is not None:
        v = budget['monthly_usd']
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise OrgError('budget.monthly_usd must be a non-negative number')
        out['monthly_usd'] = round(float(v), 2)
    if budget.get('max_parallel') is not None:
        v = budget['max_parallel']
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise OrgError('budget.max_parallel must be an integer >= 1')
        out['max_parallel'] = v
    return out


def _clean_list(items):
    if not items:
        return []
    return sorted({str(x).strip() for x in items if str(x).strip()})


def budget_status(team, used_runs=0, used_usd=0.0):
    """Pure check of a team's budget usage: {ok, reason, remaining_runs, remaining_usd, max_parallel}."""
    b = (team or {}).get('budget') or {}
    rem_runs = None if 'daily_runs' not in b else max(0, b['daily_runs'] - int(used_runs))
    rem_usd = None if 'monthly_usd' not in b else round(max(0.0, b['monthly_usd'] - float(used_usd)), 2)
    reasons = []
    if 'daily_runs' in b and int(used_runs) >= b['daily_runs']:
        reasons.append(f'daily run budget reached ({used_runs}/{b["daily_runs"]})')
    if 'monthly_usd' in b and float(used_usd) >= b['monthly_usd']:
        reasons.append(f'monthly USD budget reached ({used_usd:.2f}/{b["monthly_usd"]:.2f})')
    return {
        'ok': not reasons,
        'reason': '; '.join(reasons),
        'remaining_runs': rem_runs,
        'remaining_usd': rem_usd,
        'max_parallel': b.get('max_parallel'),
    }


def team_priority(orgs_data, project, fallback=DEFAULT_PRIORITY):
    """Highest priority among teams that own `project`, else `fallback`."""
    best = None
    for org in (orgs_data or {}).values():
        for team in (org.get('teams') or {}).values():
            if project in (team.get('projects') or []):
                p = int(team.get('priority', DEFAULT_PRIORITY))
                best = p if best is None or p > best else best
    return fallback if best is None else best


def actor_org_role(actor, org):
    """Effective role of `actor` in `org`: org membership role, falling back to global admin/owner."""
    if not actor:
        return None
    sub = actor.get('sub')
    member = (org.get('members') or {}).get(sub) if sub else None
    if member and member.get('role') in ORG_ROLES:
        return member['role']
    if auth.is_admin(actor.get('role')):
        return actor['role']
    return None


def may_view_org(actor, org):
    return actor_org_role(actor, org) in ORG_ROLES


def may_manage_org(actor, org):
    return actor_org_role(actor, org) in MANAGE_ROLES


def may_assign_role(actor, org, target_role):
    role = actor_org_role(actor, org)
    if role not in MANAGE_ROLES:
        return False
    return target_role != 'owner' or role == 'owner' or actor.get('sub') == 'shell'


class Orgs:
    """<state>/orgs.json: {"orgs": {org_id: {name, created, members: {...}, teams: {...}}}}."""

    def __init__(self, folder, clock=time.time):
        self.folder, self.clock = folder, clock
        self.path = os.path.join(folder, ORGS_FILE)

    def _load(self):
        data = auth._read(self.path).get('orgs')
        return data if isinstance(data, dict) else {}

    def _save(self, orgs):
        auth._write(self.path, {'orgs': orgs})

    def all(self):
        return {k: dict(v) for k, v in self._load().items()}

    def get(self, org_id):
        org = self._load().get(org_id)
        return dict(org, id=org_id) if org else None

    def visible_to(self, actor):
        """Only organizations `actor` is authorized to view."""
        return {k: dict(v, id=k) for k, v in self._load().items() if may_view_org(actor, v)}

    def create_org(self, org_id, name, owner_sub=None):
        _check_slug(org_id, 'organization')
        name = (name or '').strip()
        if not name:
            raise OrgError('organization name is required')
        now = int(self.clock())
        with auth.locked(self.folder):
            orgs = self._load()
            if org_id in orgs:
                raise OrgError(f'organization "{org_id}" already exists: choose another id')
            members = {owner_sub: {'role': 'owner', 'teams': [], 'added': now}} if owner_sub else {}
            orgs[org_id] = {'name': name[:80], 'created': now, 'members': members, 'teams': {}}
            self._save(orgs)
        return self.get(org_id)

    def delete_org(self, org_id):
        with auth.locked(self.folder):
            orgs = self._load()
            if org_id not in orgs:
                raise OrgError(f'no such organization "{org_id}"')
            del orgs[org_id]
            self._save(orgs)

    def set_member(self, org_id, sub, role, teams=None):
        if role not in ORG_ROLES:
            raise OrgError(f'role must be one of {", ".join(ORG_ROLES)}')
        if not isinstance(sub, str) or not sub.strip():
            raise OrgError('member id (sub or email) is required')
        now = int(self.clock())
        with auth.locked(self.folder):
            orgs = self._load()
            org = orgs.get(org_id)
            if not org:
                raise OrgError(f'no such organization "{org_id}"')
            members = dict(org.get('members') or {})
            owners = [s for s, m in members.items() if m.get('role') == 'owner']
            if role != 'owner' and owners == [sub]:
                raise OrgError('this is the last owner of the organization: assign another owner first')
            prev = members.get(sub) or {}
            team_list = _clean_list(teams) if teams is not None else list(prev.get('teams') or [])
            members[sub] = {'role': role, 'teams': team_list, 'added': prev.get('added', now)}
            org['members'] = members
            orgs[org_id] = org
            self._save(orgs)
        return self.get(org_id)

    def remove_member(self, org_id, sub):
        with auth.locked(self.folder):
            orgs = self._load()
            org = orgs.get(org_id)
            if not org:
                raise OrgError(f'no such organization "{org_id}"')
            members = dict(org.get('members') or {})
            if sub not in members:
                raise OrgError(f'no such member "{sub}" in organization "{org_id}"')
            owners = [s for s, m in members.items() if m.get('role') == 'owner']
            if owners == [sub]:
                raise OrgError('this is the last owner of the organization: assign another owner first')
            del members[sub]
            org['members'] = members
            orgs[org_id] = org
            self._save(orgs)
        return self.get(org_id)

    def upsert_team(self, org_id, team_id, name, priority=DEFAULT_PRIORITY, budget=None, projects=None):
        _check_slug(team_id, 'team')
        name = (name or '').strip()
        if not name:
            raise OrgError('team name is required')
        prio = _check_priority(priority)
        clean_budget = _check_budget(budget)
        now = int(self.clock())
        with auth.locked(self.folder):
            orgs = self._load()
            org = orgs.get(org_id)
            if not org:
                raise OrgError(f'no such organization "{org_id}"')
            teams = dict(org.get('teams') or {})
            prev = teams.get(team_id) or {}
            teams[team_id] = {
                'name': name[:80],
                'priority': prio,
                'budget': clean_budget,
                'projects': _clean_list(projects) if projects is not None else list(prev.get('projects') or []),
                'created': prev.get('created', now),
            }
            org['teams'] = teams
            orgs[org_id] = org
            self._save(orgs)
        return self.get(org_id)

    def delete_team(self, org_id, team_id):
        with auth.locked(self.folder):
            orgs = self._load()
            org = orgs.get(org_id)
            if not org:
                raise OrgError(f'no such organization "{org_id}"')
            teams = dict(org.get('teams') or {})
            if team_id not in teams:
                raise OrgError(f'no such team "{team_id}" in organization "{org_id}"')
            del teams[team_id]
            org['teams'] = teams
            orgs[org_id] = org
            self._save(orgs)
        return self.get(org_id)
