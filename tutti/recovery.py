"""What Tutti does when it starts again after a power-off, a crash or a sleep (docs/PRESENCE.md, "Recovery after interruption").

1. Temp files of writers that died mid-write are removed; the old state file is whole (tutti/durable.py writes atomically).
2. The usage log's torn last line, if a power-off cut one, is cut off.
3. Jobs a shutdown stopped (tutti/sleepwatch.py noted them) start again, an agent run in its own worktree.
4. Runs and leases of an earlier boot are found by the next autopilot and dispatch rounds (their boot id differs): they are
   interrupted, not failed, and are queued again with nothing counted, from the same worktree."""
import os

from . import config, durable, presence, sleepwatch, state, telemetry


def on_start(cfg, resume=sleepwatch.woke):
    """Returns what was repaired, as lines for the log."""
    folder = config.state_dir(cfg)
    out = [f'removed {p}: a write cut by a power-off (the previous file is whole)' for p in durable.sweep(folder)]
    log = state.usage_log_path(cfg)
    if os.path.exists(log) and durable.repair_jsonl(log):
        out.append(f'{log}: a torn last line (a power-off mid-write) was cut off')
    if presence.read(cfg)['resume']:
        started = resume(cfg)
        out.append(f'{len(started)} job(s) stopped by a shutdown started again')
    telemetry.record('feature', 'recovery.start', repaired=len(out))
    return out
