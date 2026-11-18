"""Weighted fair share by project priority (projects.<name>.priority, config.project_priority): higher-priority projects
are served first and more often, and a lower one still gets its share, so it never starves.

A project of priority p weighs 1 / (p + 1): priorities 0, 1, 2 share the work 6 : 3 : 2. The next item goes to the project
with the least work served per weight (what runs now counts as served); ties go to the more important project, then to
the older item. Pure: the autopilot's dispatch and fill_room use it, and idle dispatch can too."""
from . import config


def task_level(task):
    """P0 first. Untriaged or malformed values are P5; titles cannot promote a task on their own."""
    value = task.get('priority')
    if type(value) is int and 0 <= value <= 5:
        return value
    return 5


def tasks_by_priority(cfg, tasks, served_repos=None):
    """Run-now first, then task priority, retaining project fairness within each group."""
    out = []
    for urgent in (True, False):
        for level in range(6):
            group = [t for t in tasks if bool(t.get('urgent')) == urgent and task_level(t) == level]
            out.extend(by_repo(cfg, group, lambda t: t['repo'], served_repos))
    return out


def weight(priority):
    return 1.0 / (1 + max(0, priority))


def fair_order(cfg, items, project_of, served=None):
    """`items` (oldest first) reordered by weighted fair share. project_of(item) names its project (or None); served:
    {project: units already running}."""
    queues, order = {}, []
    for it in items:
        p = project_of(it)
        if p not in queues:
            order.append(p)
        queues.setdefault(p, []).append(it)
    done = {p: float((served or {}).get(p, 0)) for p in queues}
    out = []
    while any(queues.values()):
        live = [p for p in order if queues[p]]
        best = min(live, key=lambda p: (done[p] / weight(config.project_priority(cfg, p)), config.project_priority(cfg, p), order.index(p)))
        out.append(queues[best].pop(0))
        done[best] += 1
    return out


def by_repo(cfg, items, repo_of, served_repos=None):
    """fair_order over items that name an autopilot repo; served_repos: {repo: running count}."""
    def group(repo):   # a repo no project claims is its own group, of the lowest priority
        return config.project_of_repo(cfg, repo) or f'repo:{repo}'
    served = {}
    for repo, n in (served_repos or {}).items():
        served[group(repo)] = served.get(group(repo), 0) + n
    return fair_order(cfg, items, lambda it: group(repo_of(it)), served)
