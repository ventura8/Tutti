from . import backlog, linear


def sync(cfg):
    token = linear.get_token(cfg)
    if not token:
        print("No Linear token found in config (linear.token) or env (LINEAR_TOKEN)")
        return

    linear_cfg = cfg.get('linear', {})
    workspace = linear_cfg.get('workspace', 'microunde')
    # P-MIC-1 is the project ID for 'tutti' as per the request
    project_id = linear_cfg.get('projects', {}).get('tutti', 'P-MIC-1')
    client = linear.LinearClient(token, workspace)

    with backlog.locked(cfg) as data:
        tasks = data['tasks']

        # Fetch Linear issues
        response = client.get_project_issues(project_id)
        issues = response.get('data', {}).get('project', {}).get('issues', {}).get('nodes', [])

        # Map tasks by title
        task_map = {t['title']: t for t in tasks if t['status'] not in ('done', 'dropped')}
        issue_map = {i['title']: i for i in issues}

        # Sync Backlog -> Linear
        for title, task in task_map.items():
            linear_title = f"[{task['id']}] {title}"
            if linear_title not in issue_map:
                client.create_issue(project_id, linear_title, str(task.get('notes', '')))
                print(f"Created Linear issue for {task['id']}")
            else:
                # Basic update if description differs
                issue = issue_map[linear_title]
                if issue.get('description') != str(task.get('notes', '')):
                    client.update_issue(issue['id'], linear_title, str(task.get('notes', '')))
                    print(f"Updated Linear issue for {task['id']}")

        # Sync Linear -> Backlog (simplistic)
        for issue in issues:
            # Check if it starts with [t1], [t2]...
            if issue['title'].startswith('['):
                task_id = issue['title'].split(']')[0][1:]
                task = next((t for t in tasks if t['id'] == task_id), None)
                if task and task.get('notes') != issue.get('description'):
                    task['notes'] = issue.get('description')
                    print(f"Updated backlog item {task_id} from Linear")

