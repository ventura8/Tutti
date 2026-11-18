"""Task enrichment for Tutti tasks."""
from . import routing


def generate_brief(task):
    """Generates a compact, complete brief for a task."""
    brief = [
        f'Task: {task["id"]}',
        f'Goal: {task.get("title", "No title")}',
        f'Status: {task.get("status", "unknown")}',
        '---',
        'Rules: Follow AGENTS.md and CLAUDE.md in the repository root.',
        'Constraint: Keep changes small, bounded, and tested.'
    ]
    return '\n'.join(brief)


def get_details(cfg, task):
    """Enriches a task with brief and agent selection."""
    if 'brief' not in task:
        task['brief'] = generate_brief(task)
    if 'agent_choice' not in task:
        task['agent_choice'] = routing.rank_agents(task)
    return task
