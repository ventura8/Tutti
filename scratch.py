def delegate(cfg, name, task, files, repo, run_id=None, track=None, extra=(), checks=True, discard=False, say=None, check_list=None,
             remote_track=None, sub=None, cid=None):
    """The whole hand-off. Returns the report dict (also written to report.json and report.txt). Runs that name the same
    --track share one worktree, so they take turns: the second waits until the first has its report. `check_list` replaces the
    configured checks (`tutti run --check`: the target repo's own check); `remote_track` the folder routed checks copy into."""
    if track and not TRACK.match(track):
        raise ship.ShipError(f'refused: --track "{track}" must be one folder name (letters, digits, . _ -), nothing was changed')
    lock = track_lock(cfg, f'tree-{track}', say or (lambda m: sys.stderr.write(f'tutti: {m}\n'))) if track else contextlib.nullcontext()
    with lock:
        return _delegate(cfg, name, task, files, repo, run_id, track, extra, checks, discard, say, check_list, remote_track, sub=sub, cid=cid)


def run_worker(cfg, name, task, files, wt, log, extra, worker_log, check_list):
    """(exit code, last lines) of the worker in the worktree. A local model (kind ollama) works through `tutti agent`'s loop on
    the given files with the first check as its own check."""
    if cfg['workers'][name]['kind'] != 'ollama':
        return run_compacting(cfg, name, task, files, wt, log, extra, worker_log)
    from . import fleet, local
    from . import workers as workers_mod
    check = (check_list or [{}])[0].get('run') or 'true'
    w = cfg['workers'][name]
    # the model the scheduler chose (`--extra=--model --extra=<name>`, tutti/autopilot.py), else the one that suits the task's
    # first line; its files in steps the model can hold at once. A failed step ends the run: later steps never build on it
    chosen = dict(workers_mod.pairs(list(extra or ()))).get('--model')
    model = next((m for m in w.get('models') or [] if m and m['name'] == chosen), None) or \
        fleet.model_for(w, {'title': task.splitlines()[0] if task else '', 'scope': list(files), 'size': 'S'}) or {}
    worker = {**w, 'model': chosen or model.get('name') or w['model'], '_name': name}
    parts = fleet.steps(files, fleet.files_per_step(model))
    ok = False
    with contextlib.redirect_stdout(log):
        for i, part in enumerate(parts, 1):
            step_task = task if len(parts) == 1 else f'{task}\n\n(Step {i} of {len(parts)}: change only these files now.)'
            ok = local.agent(cfg, worker, step_task, check, part, base=wt)
            if not ok:
                print(f'step {i} of {len(parts)} failed: the run stops here', flush=True)
                break
    return (0 if ok else 1), []


def run_compacting(cfg, name, task, files, wt, log, extra, worker_log, run=workers.run):
    """The worker's run; when it is stopped at the autocompact threshold, started again in the same worktree with a handoff
    note (the changes, its last output) instead of its grown context, at most autocompact.RESTARTS times."""
    from . import autocompact, conduct  # conduct imports this module
    rc, lines = run(cfg, name, task, files, cwd=wt, out=log, extra=extra, log=worker_log)
    for n in range(autocompact.RESTARTS):
        if rc != workers.COMPACT_RC:
            break
        why = f'its context reached autocompact.threshold_pct (restart {n + 1} of at most {autocompact.RESTARTS})'
        last = compact.compact('\n'.join(lines), 2000).splitlines()
        note = conduct.handoff_note(task, name, name, why, '', conduct.tree_changes(wt), last)
        log.write(f'tutti: run: compacted: {why}\n')
        rc, lines = run(cfg, name, autocompact.restart_task(task, note), files, cwd=wt, out=log, extra=extra, log=worker_log)
    return rc, lines


def chosen_checks(cfg, repo, check_list):
    """(checks, where they come from): `tutti run --check` (the autopilot's task check) beats the target repo's own."""
    if check_list is not None:
        return check_list, '--check'
    return checks_for(cfg, repo)


def accounts_of_run(cfg, name, extra=()):
    """The accounts a run of worker `name` spends: the account of that name, else those naming the worker; of several (agy's
    Claude and Gemini buckets) the ones whose `args` the run was given (`extra`), else all of them."""
    if name in cfg['accounts']:
        return [name]
    found = [n for n, a in cfg['accounts'].items() if (a or {}).get('worker') == name]
    given = [n for n in found if (cfg['accounts'][n] or {}).get('args') and all(x in extra for x in cfg['accounts'][n]['args'])]
    return given or found


def _delegate(cfg, name, task, files, repo, run_id, track, extra, checks, discard, say, check_list=None, remote_track=None, sub=None, cid=None):
    if run_id and not valid_id(run_id):
        raise ship.ShipError(f'refused: run id "{run_id}" is not <time>-<pid>: let tutti choose it (nothing was changed)')
    run_id = run_id or new_id(cfg)
    p = paths(cfg, run_id)
    os.makedirs(p['dir'], exist_ok=True)
    write(p['cmd'], f'run {name}: {task.splitlines()[0][:150]}\n')
    write(p['pid'], f'{os.getpid()}\n')
    # what a dashboard "pause, stop now" of this worker, or of the account it spends, stops
    write(p['log'] + '.target', ' '.join([f'worker:{name}', *[f'account:{n}' for n in accounts_of_run(cfg, name, extra)]]) + '\n')
    log = open(p['log'], 'a', encoding='utf8')   # closed in finally: the log outlives the say() closure
    say = say or (lambda m: (sys.stderr.write(f'tutti: {m}\n'), log.write(m + '\n'), log.flush()))
    try:
        repo = resolve_repo(cfg, repo)
        wt, created = tree_for(cfg, repo, run_id, track, say)
        # how a "pause, stop now" hands this run over (tutti/handover.py): the same task, worktree and checks
        write(p['log'] + '.job', json.dumps({'kind': 'run', 'worker': name, 'task': task, 'repo': os.path.abspath(repo),
                                             'track': track or run_id, 'run_id': run_id, 'files': list(files or []),
                                             'checks': [c for c in check_list or [] if c.get('run')]}) + '\n')
        # what the run works on, for the dashboard's projects while it runs
        write(os.path.join(p['dir'], 'meta.json'), json.dumps({'repo': os.path.abspath(repo), 'worktree': wt, 'sub': sub, 'cid': cid}) + '\n')
        worktree.share(wt, cfg['ship'].get('share', []), say)
        rc, lines = run_worker(cfg, name, task, files, wt, log, extra, p['worker_log'], check_list)
        worktree.share(wt, cfg['ship'].get('share', []), say)
        rc, lines = run_worker(cfg, name, task, files, wt, log, extra, p['worker_log'], check_list)
