import re


def process(data):
    tasks = data['tasks']
    dedupe_tasks(tasks)
    group_tasks(tasks)


def get_key(t):
    title = re.sub(r'[^a-zA-Z0-9]', '', t['title']).lower()
    return (t['repo'], title)


def dedupe_tasks(tasks):
    seen = {}
    for t in tasks:
        if t['status'] in ('done', 'dropped'):
            continue
        key = get_key(t)
        if key in seen:
            master = seen[key]
            # Merge t into master
            master['notes'].extend(t['notes'])
            if 'source' in t and t['source'] not in master.get('sources', []):
                master.setdefault('sources', []).append(t['source'])
            t['status'] = 'dropped'
            # Link to master
            t['notes'].append(f'Merged into {master["id"]}')
            master['notes'].append(f'Merged {t["id"]}')
        else:
            seen[key] = t
            if 'source' in t and 'sources' not in t:
                t['sources'] = [t['source']]


def group_tasks(tasks):
    active_tasks = [t for t in tasks if t['status'] not in ('done', 'dropped')]
    for i, t1 in enumerate(active_tasks):
        for t2 in active_tasks[i+1:]:
            if overlaps(t1, t2):
                # Group t1 and t2
                t1.setdefault('group', []).append(t2['id'])
                t2.setdefault('group', []).append(t1['id'])

                # Flag uncertainty if tasks are complexly overlapping
                if len(t1.get('group', [])) > 2:
                    if 'Needs attention: overlapping too many tasks' not in t1['notes']:
                        t1['notes'].append('Needs attention: overlapping too many tasks')


def overlaps(t1, t2):
    s1 = set(t1.get('scope', []))
    s2 = set(t2.get('scope', []))
    return not s1.isdisjoint(s2)



