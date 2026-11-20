"""Conservative estimates for the current autopilot attempt, from comparable completed attempts."""
import math
import statistics

MIN_SAMPLES = 3


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def profiles(tasks):
    """Keep passing attempts with a valid duration, grouped by repo, size and actual ladder step."""
    out = {}
    for task in tasks:
        if not task.get('repo') or not task.get('size'):
            continue
        for attempt in task.get('attempts') or []:
            start, end, step = attempt.get('started'), attempt.get('at'), attempt.get('step')
            if attempt.get('ok') is not True or not step or not _number(start) or not _number(end) or end <= start:
                continue
            out.setdefault((task['repo'], task['size'], step), []).append(end - start)
    return out


def estimate(task, step, history, now):
    """Estimate one attempt, never queue wait, retries, review, or a task's final completion."""
    samples = history.get((task.get('repo'), task.get('size'), step), [])
    if len(samples) < MIN_SAMPLES:
        return {'state': 'unknown', 'reason': 'not enough comparable successful attempts', 'samples': len(samples)}
    typical = round(statistics.median(samples))
    result = {'state': 'duration', 'duration_seconds': typical, 'samples': len(samples), 'confidence': 'rough'}
    if task.get('status') != 'running':
        return result
    started = task.get('started')
    if not _number(started) or not _number(now) or started > now:
        return {'state': 'unknown', 'reason': 'start time unavailable', 'samples': len(samples)}
    elapsed = now - started
    if elapsed >= typical:
        return {**result, 'state': 'overdue'}
    return {**result, 'state': 'running', 'remaining_seconds': round(typical - elapsed), 'finish_at': started + typical}
