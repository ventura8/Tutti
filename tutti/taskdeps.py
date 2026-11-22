"""Task dependencies, groups and merges (t243, docs/AUTOPILOT.md, *Dependencies*).

A backlog task may name:
- `depends_on`: tasks that must land first (done); until then it is blocked and the autopilot never starts it;
- `together_with`: tasks that go side by side (one round starts them together, so neither waits for the other's slot);
- `merged_into`: the task it was folded into (a duplicate, t210); it never runs on its own.
Pure: tasks in, facts out. `link` changes one task's lists and refuses unknown ids and cycles."""

DONE = ('done',)
CLOSED = ('done', 'dropped')
FIELDS = ('depends_on', 'together_with', 'merged_into')


class DepError(ValueError):
    pass


def ids(task, field):
    v = task.get(field)
    if field == 'merged_into':
        return [v] if isinstance(v, str) and v else []
    return [str(x) for x in v] if isinstance(v, list) else []


def by_id(tasks):
    return {t['id']: t for t in tasks if t.get('id')}


def landed(tid, known):
    """True when `tid` is done or dropped, or was merged into a task that landed (a merge chain is followed, loops stop)."""
    seen = set()
    while tid in known and tid not in seen:
        seen.add(tid)
        t = known[tid]
        if t.get('status') in CLOSED:
            return True
        into = ids(t, 'merged_into')
        if not into:
            return False
        tid = into[0]
    return False


def waiting_on(task, tasks):
    """The ids of `depends_on` tasks that have not landed (a dropped one no longer blocks; a merged one waits for its target)."""
    known = by_id(tasks)
    return [i for i in ids(task, 'depends_on') if i in known and not landed(i, known)]


def holds(tasks):
    """{task id: why} for queued tasks that must not start now: blocked by dependencies, or merged into another."""
    out = {}
    for t in tasks:
        if t.get('status') != 'queued':
            continue
        into = ids(t, 'merged_into')
        if into:
            out[t['id']] = f'merged into {into[0]}: it lands with that task (tutti backlog link {t["id"]} --merged-into "" undoes it)'
            continue
        wait = waiting_on(t, tasks)
        if wait:
            out[t['id']] = f'blocked until {", ".join(wait)} land{"s" if len(wait) == 1 else ""} (depends_on)'
    return out


def group_of(task, tasks):
    """The ids that go with `task` (together_with, both ways), itself included, sorted."""
    seen, todo, known = set(), [task['id']], by_id(tasks)
    rev = {}
    for t in tasks:
        for o in ids(t, 'together_with'):
            rev.setdefault(o, set()).add(t['id'])
    while todo:
        i = todo.pop()
        if i in seen or i not in known:
            continue
        seen.add(i)
        todo += ids(known[i], 'together_with') + sorted(rev.get(i, ()))
    return sorted(seen, key=lambda x: (len(x), x))


def lanes(tasks):
    """{blocked, ready, grouped}: the board of queued tasks (a grouped task is also ready or blocked)."""
    h = holds(tasks)
    queued = [t for t in tasks if t.get('status') == 'queued']
    return {'blocked': [t['id'] for t in queued if t['id'] in h], 'ready': [t['id'] for t in queued if t['id'] not in h],
            'grouped': [t['id'] for t in queued if len(group_of(t, tasks)) > 1]}


def cycle(tasks, start):
    """A path of depends_on and merged_into links from `start` back to itself, or [] (each task visited once)."""
    known = by_id(tasks)
    seen, stack = {start}, [(start, [start])]
    while stack:
        i, path = stack.pop()
        for n in ids(known.get(i, {}), 'depends_on') + ids(known.get(i, {}), 'merged_into'):
            if n == start:
                return [*path, n]
            if n in known and n not in seen:
                seen.add(n)
                stack.append((n, [*path, n]))
    return []


def _unique(values):
    return None if values is None else list(dict.fromkeys(values))


def check_ids(known, tid, depends_on, together_with, merged_into):
    """Raises DepError when a list names the task itself or an id not in the backlog."""
    for name, value in (('depends_on', depends_on), ('together_with', together_with)):
        bad = [i for i in value or [] if i not in known or i == tid]
        if bad:
            raise DepError(f'backlog link: {name} names {", ".join(bad)}, which is {tid} itself or not in the backlog; use existing ids')
    if merged_into and (merged_into not in known or merged_into == tid):
        raise DepError(f'backlog link: --merged-into {merged_into} is {tid} itself or not in the backlog; use an existing id')


def link(tasks, tid, depends_on=None, together_with=None, merged_into=None):
    """Sets the given lists on task `tid` (None leaves one as it is, [] or '' clears it); raises DepError with the fix."""
    known = by_id(tasks)
    if tid not in known:
        raise DepError(f'backlog link: no task {tid}; `tutti backlog list --all` shows the ids')
    check_ids(known, tid, depends_on, together_with, merged_into)
    task = known[tid]
    before = {f: task[f] for f in FIELDS if f in task}
    for f, value in (('depends_on', _unique(depends_on)), ('together_with', _unique(together_with)), ('merged_into', merged_into)):
        if value in ([], ''):
            task.pop(f, None)
        elif value is not None:
            task[f] = value
    loop = cycle(tasks, tid)
    if loop:
        for f in FIELDS:
            task.pop(f, None)
        task.update(before)
        raise DepError(f'backlog link: that makes a loop ({" -> ".join(loop)}): a task cannot wait for itself; drop one link')
    return task


def describe(task, tasks):
    """Plain lines for `tutti backlog deps <id>` and the expanded row."""
    known = by_id(tasks)

    def state(i):
        return f'{i} ({known[i].get("status")})' if i in known else f'{i} (not in the backlog)'
    rdeps = [t['id'] for t in tasks if task['id'] in ids(t, 'depends_on')]
    lines = []
    if ids(task, 'depends_on'):
        lines.append('Depends on: ' + ', '.join(state(i) for i in ids(task, 'depends_on')))
    wait = waiting_on(task, tasks)
    if task.get('status') == 'queued':
        lines.append(f'Blocked until {", ".join(wait)} land' if wait else 'Ready: nothing it depends on is open')
    group = [i for i in group_of(task, tasks) if i != task['id']]
    if group:
        lines.append('Goes together with: ' + ', '.join(state(i) for i in group))
    if ids(task, 'merged_into'):
        lines.append('Merged into: ' + state(ids(task, 'merged_into')[0]))
    merged = [t['id'] for t in tasks if ids(t, 'merged_into') == [task['id']]]
    if merged:
        lines.append('Folded into it: ' + ', '.join(merged))
    if rdeps:
        lines.append('Needed by: ' + ', '.join(state(i) for i in rdeps))
    return lines
