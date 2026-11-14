"""`tutti conduct`: a main (conductor) agent other than Claude Code. The chosen agent (a CLI worker such as cursor, agy or
copilot, or a local model) gets the goal and a generated instruction file that lists Tutti's own commands, the project's
rules and the safety limits; it calls `tutti ...` like any shell command. When its account reaches panic or its limit, or
the owner switches the main agent on the dashboard, Tutti stops it and hands the goal to the next agent with a written note.

Pure parts (the order, the instruction text, the handoff note, the local model's command check) are unit-tested; the
process and the files sit in `run_cli`, `run_local` and `conduct`."""
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime, timezone

from . import config, pacing, readers, remote, routing, workers

# The commands a main agent is told about, in the order of the instruction file: (subcommand, the line it calls, what for).
TOOLS = (
    ('run', 'tutti run auto "<task>" [files...] --repo <repo> --bg',
     'hand a bounded task to the worker whose account has room; it edits a worktree, never commits; prints a run id'),
    ('wait', 'tutti wait run <id>', 'wait for a delegated run; ends with its verdict and the path of its report'),
    ('runs', 'tutti runs show <id>', 'read a run\'s report (the diff, the checks); `tutti runs discard <id>` drops its worktree'),
    ('test', 'tutti test <track> "<command>"', 'run a heavy command (tests, a build) on the first fit machine, behind its slot queue'),
    ('files', 'tutti files ...', 'give large inputs to another machine (`tutti files --help`)'),
    ('usage', 'tutti usage', 'every account against its pace: who has room, who is in panic'),
    ('workers', 'tutti workers', 'which workers are ready'),
    ('machines', 'tutti machines', 'which machines are fit'),
    ('handoff', 'tutti handoff done|next "<text>"', 'record a finished step (done) or what comes next (next) in the run\'s progress'),
    ('ship', 'tutti ship "<message>" --dry-run', 'the commit, checks, push and deploy plan; only --dry-run: a person ships'),
)
# What a local model may call through the conductor's loop: the tools above plus the local-model helpers.
LOCAL_ALLOWED = {t[0] for t in TOOLS} | {'ask', 'code', 'agent'}
SAFETY = (
    'Never push, tag, merge, publish or deploy. `tutti ship` only with --dry-run; a real ship is refused while you conduct.',
    'Never commit to or push a protected branch ({protected}).',
    'Never read, print or type a credential: no .env files, tokens, keys, passwords or card numbers; never sign in or create an account.',
    'Ask-first actions need a human: {ask_first}. Do not do them: run `tutti handoff next "NEEDS HUMAN: <the action and why>"` '
    'and end with BLOCKED.',
    'Stay inside {cwd} and the worktrees Tutti makes. Prefer delegating bounded edits with `tutti run auto` over editing yourself.',
)
ASK_FIRST = ['git push', 'git tag', 'merging a pull request', 'deploys', 'payments', 'accepting terms or consent',
             'changing cloud or account settings', 'deleting data']
RULE_FILES = ('AGENTS.md', 'CLAUDE.md')
DONE = re.compile(r'^\s*(DONE|BLOCKED):\s*(.*)$')
RUN = re.compile(r'^\s*RUN:\s*(.+)$')
OUTPUT_MAX = 3000
STOP_GRACE = 10


class ConductError(Exception):
    """The main agent cannot start: the message names the key or the step and the fix."""


# ---- the choice and the order


def settings(cfg):
    return cfg['main']


def state_folder(cfg):
    d = os.path.join(config.state_dir(cfg), 'conduct')
    os.makedirs(d, exist_ok=True)
    return d


def choice_path(cfg):
    return os.path.join(state_folder(cfg), 'main.json')


def read_json(path):
    try:
        with open(path, encoding='utf8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path, data):
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def candidates(cfg):
    """The agents that can conduct: every worker (a CLI agent or a local model)."""
    return list(cfg['workers'])


def chosen(cfg):
    """The owner's switch (the dashboard or `tutti main <agent>`), when it names a worker; else None."""
    c = read_json(choice_path(cfg)) or {}
    return c.get('agent') if c.get('agent') in cfg['workers'] else None


def current(cfg):
    """The main agent now: the owner's switch, else main.agent from the config."""
    return chosen(cfg) or settings(cfg)['agent']


def choose(cfg, agent, now=None):
    """Records the owner's switch; a running conductor hands over to it at its next check."""
    if agent not in cfg['workers']:
        raise ConductError(f'"{agent}" is not a worker in {cfg["_path"]}: the main agent can be {", ".join(candidates(cfg)) or "(none)"}')
    now = now or datetime.now(timezone.utc)
    write_json(choice_path(cfg), {'agent': agent, 'at': now.isoformat()})
    return agent


def order(main, fallback):
    """The main agent first, then the fallbacks, each once."""
    out = []
    for name in [main, *fallback]:
        if name and name not in out:
            out.append(name)
    return out


def accounts_of(cfg, agent):
    """The accounts an agent spends: the one of the same name and every one that says `worker: <agent>` (disabled ones aside)."""
    return [n for n, a in cfg['accounts'].items()
            if (n == agent or (a or {}).get('worker') == agent) and (a or {}).get('enabled', True) is not False]


def out_reason(cfg, agent, states):
    """Why an agent must not conduct now (every account it spends is in panic or spent), or ''. No account: never out."""
    names = accounts_of(cfg, agent)
    by_name = {s['name']: s for s in states}
    reasons = [routing.account_unfit(by_name[n]) for n in names if n in by_name]
    if not reasons or not all(reasons):
        return ''
    return '; '.join(f'{n}: {r}' for n, r in zip(names, reasons, strict=False))


def next_agent(names, tried, out_fn):
    """(the next agent to conduct or None, the skipped ones with why) from `names` in order."""
    skipped = []
    for name in names:
        if name in tried:
            continue
        why = out_fn(name)
        if not why:
            return name, skipped
        skipped.append(f'{name}: {why}')
    return None, skipped


def account_states(cfg, now=None):
    now = now or datetime.now(timezone.utc)
    path = config.expand((cfg.get('usage') or {}).get('log') or os.path.join(config.state_dir(cfg), 'usage.jsonl'))
    try:
        entries = pacing.read_log(path)
    except (OSError, ValueError):
        entries = []
    return [pacing.account_state(n, a, entries, now) for n, a in cfg['accounts'].items()]


# ---- the instruction file


def rule_files(cwd):
    return [os.path.join(cwd, n) for n in RULE_FILES if os.path.isfile(os.path.join(cwd, n))]


def instructions(cfg, agent, goal, cwd, progress, commands, now):
    """The instruction file for the main agent (AGENTS.md style): the goal, the tools, the rules, the limits, the handoff."""
    s = settings(cfg)
    rules = rule_files(cwd)
    lines = [f'# Tutti: you are the main agent ({agent})', '', f'Generated by Tutti at {now.isoformat()} for this goal only.', '',
             '## Goal', '', goal.strip(), '', '## Project rules', '']
    none = f'- {cwd} has no AGENTS.md or CLAUDE.md: follow the brief rules below.'
    lines += [f'- Read and follow `{p}` before you start.' for p in rules] or [none]
    lines += [f'- {r}' for r in cfg['brief']['rules']]
    lines += ['', '## Tutti\'s commands (run them in the shell; `tutti <command> --help` for the options)', '']
    lines += [f'- `{call}`: {text}' for name, call, text in TOOLS if name in commands]
    ask_first = ', '.join([*ASK_FIRST, *s['ask_first']])
    protected = ', '.join(cfg['ship']['protected'])
    lines += ['', '## Safety limits', '']
    lines += [f'- {t.format(protected=protected, ask_first=ask_first, cwd=cwd)}' for t in SAFETY]
    lines += ['', '## Progress and handoff', '',
              '- After every step run `tutti handoff done "<what you finished>"`, and `tutti handoff next "<what comes next>"` '
              f'when the plan changes (they write `{progress}`).',
              '- If your quota runs out, Tutti stops you and gives that file to the next agent: write it so another agent can continue.',
              '- End your last message with one line: `DONE: <what was achieved>` or `BLOCKED: <what a human must do>`.']
    return '\n'.join(lines) + '\n'


def prompt(instruction_text, handoff=None):
    """What the main agent is started with: the instructions, and the note of the agent before it when there was one."""
    out = [instruction_text]
    if handoff:
        out += ['', '## Handoff from the agent before you', '', handoff]
    return '\n'.join(out)


def allow_tutti(argv):
    """A headless agent that runs only allowed commands (a JSON settings argument with permissions.allow, as agy's
    --settings) gets `command(tutti)` added, so it may call Tutti; every other argument is kept as written."""
    out = []
    for a in argv:
        data = None
        if a.lstrip().startswith('{'):
            try:
                data = json.loads(a)
            except ValueError:
                data = None
        allow = ((data or {}).get('permissions') or {}).get('allow') if isinstance(data, dict) else None
        if isinstance(allow, list) and 'command(tutti)' not in allow:
            data['permissions']['allow'] = [*allow, 'command(tutti)']
            a = json.dumps(data, separators=(',', ':'))
        out.append(a)
    return out


# ---- the handoff note


def sections(text):
    """The "## Done" and "## Next" parts of a progress file, as text."""
    out = {'done': '', 'next': ''}
    key = None
    for line in (text or '').splitlines():
        head = line.strip().lower()
        if head.startswith('## '):
            key = {'## done': 'done', '## next': 'next'}.get(head)
            continue
        if key:
            out[key] += line + '\n'
    return {k: v.strip() for k, v in out.items()}


def add_progress(text, section, item):
    """The progress file with `- item` added at the end of its "## Done" or "## Next" section (the section is made if missing)."""
    head = f'## {section.capitalize()}'
    lines = (text or '').rstrip('\n').splitlines()
    if head not in [x.strip() for x in lines]:
        lines += ['', head, '']
    start = [x.strip() for x in lines].index(head)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('## ')), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, f'- {" ".join(item.split())}')
    return '\n'.join(lines) + '\n'


def note_progress(run_dir, section, item):
    """`tutti handoff done|next "<text>"` inside a conductor run (TUTTI_CONDUCT names its folder)."""
    if not run_dir or not os.path.isdir(run_dir):
        raise ConductError('tutti handoff works only inside `tutti conduct` (TUTTI_CONDUCT names the run folder): '
                           'run it from the main agent')
    if not item.strip():
        raise ConductError('tutti handoff needs the text: tutti handoff done "<what you finished>"')
    path = os.path.join(run_dir, 'progress.md')
    with open(path, 'w' if not os.path.exists(path) else 'r+', encoding='utf8') as f:
        text = f.read() if f.mode == 'r+' else ''
        f.seek(0)
        f.write(add_progress(text, section, item))
        f.truncate()
    return path


def handoff_note(goal, frm, to, why, progress_text, changes, tail):
    """The written handoff: the goal, who stopped and why, what is done (theirs and the tree's), what comes next."""
    s = sections(progress_text)
    lines = [f'From {frm} to {to or "(no agent left)"}: {why}', '', '### Goal', '', goal.strip(), '', '### Done', '',
             s['done'] or '(the agent wrote nothing under "## Done")', '', '### Changes in the tree', '', changes or '(none)', '',
             '### Next', '', s['next'] or 'Continue the goal from where the tree is; check the changes above first.']
    if tail:
        lines += ['', '### Its last output', '', *[f'    {x}' for x in tail]]
    return '\n'.join(lines) + '\n'


def tree_changes(cwd):
    """`git status --short` of the folder, or '' when it is not a git tree."""
    try:
        r = subprocess.run(['git', '-C', cwd, 'status', '--short'], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ''
    return r.stdout.strip() if r.returncode == 0 else ''


# ---- running one agent


def shim(folder, cfg_path):
    """A `tutti` on the agent's PATH that runs this Tutti with this config."""
    d = os.path.join(folder, 'bin')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, 'tutti')
    with open(path, 'w', encoding='utf8') as f:
        f.write(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(remote.BIN)} --config {shlex.quote(cfg_path)} "$@"\n')
    os.chmod(path, 0o755)
    return d


def agent_env(cfg, run, agent):
    env = dict(os.environ, TUTTI_CONFIG=cfg['_path'], TUTTI_CONDUCT=run['dir'], TUTTI_MAIN=agent)
    env['PATH'] = run['bin'] + os.pathsep + env.get('PATH', '')
    return env


def cli_argv(cfg, agent, text, log):
    w = cfg['workers'][agent]
    spec = settings(cfg)['commands'].get(agent)
    return allow_tutti(workers.command({**w, 'command': spec} if spec else w, text, log))


class Watch:
    """Checks every few seconds why the running agent should stop: the owner switched, or its accounts are out."""

    def __init__(self, cfg, agent, started_choice, out_fn, every):
        self.cfg, self.agent, self.started_choice, self.out_fn, self.every = cfg, agent, started_choice, out_fn, every
        self.reason = None
        self.stop = threading.Event()

    def check(self):
        c = chosen(self.cfg)
        if c and c != self.agent and c != self.started_choice:
            return 'switch', f'the owner switched the main agent to {c}'
        why = self.out_fn(self.agent)
        return ('panic', why) if why else None

    def loop(self, on_stop):
        while not self.stop.wait(self.every):
            hit = self.check()
            if hit:
                self.reason = hit
                on_stop()
                return


def stop_process(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(STOP_GRACE)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def run_cli(cfg, agent, text, run, watch, out=None):
    """Runs a CLI agent headless in the project, its output passed through and kept; (rc, last lines)."""
    out = out or sys.stdout
    log = os.path.join(run['dir'], f'{agent}-{len(run["agents"])}.agent.log')
    argv = cli_argv(cfg, agent, text, log)
    kept = []
    with subprocess.Popen(argv, cwd=run['cwd'], env=agent_env(cfg, run, agent), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors='replace', bufsize=1, start_new_session=True) as p:
        threading.Thread(target=watch.loop, args=(lambda: stop_process(p),), daemon=True).start()
        for line in p.stdout:
            out.write(line)
            out.flush()
            kept = [*kept[-(workers.KEEP_LINES - 1):], line.rstrip('\n')]
    watch.stop.set()
    return p.returncode, kept


# ---- a local model as the main agent: Tutti runs the commands it asks for


def local_command(line, protected_ok=False):
    """(argv for `tutti`, None) when a model's `RUN: tutti ...` line may run, else (None, why). Only Tutti's commands run, never
    a shell; `ship` only with --dry-run."""
    m = RUN.match(line)
    if not m:
        return None, 'not a RUN: line'
    try:
        args = shlex.split(m.group(1))
    except ValueError as e:
        return None, f'the command does not parse ({e}): quote it like a shell line'
    if args[:1] == ['tutti']:
        args = args[1:]
    if not args or args[0] not in LOCAL_ALLOWED:
        return None, f'only these Tutti commands run here: {", ".join(sorted(LOCAL_ALLOWED))}'
    if args[0] == 'ship' and ('--dry-run' not in args or ('--allow-protected' in args and not protected_ok)):
        return None, 'ship runs only with --dry-run (and without --allow-protected) while a model conducts'
    return args, None


def local_reply(worker, messages, timeout=600):
    """One answer of an Ollama model to the conversation so far."""
    body = {'model': worker['model'], 'messages': messages, 'stream': False,
            'options': {k: worker[k] for k in ('num_ctx',) if worker.get(k)}}
    req = urllib.request.Request(config.ollama_url(worker['host'], '/api/chat'), data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return (json.loads(r.read().decode()).get('message') or {}).get('content') or ''


LOCAL_PROTOCOL = ('You cannot run a shell yourself. To run one of Tutti\'s commands, answer with exactly one line '
                  '`RUN: tutti <command> <args>`; you get its output back. When the goal is reached or blocked, answer with one line '
                  '`DONE: <what was achieved>` or `BLOCKED: <what a human must do>`. Keep answers short.')


def run_tutti(cfg, args, run, agent):
    r = subprocess.run([sys.executable, remote.BIN, '--config', cfg['_path'], *args], cwd=run['cwd'], env=agent_env(cfg, run, agent),
                       capture_output=True, text=True, errors='replace', timeout=settings(cfg)['command_timeout'], check=False)
    text = (r.stdout + r.stderr).strip()
    return f'exit {r.returncode}\n{text[-OUTPUT_MAX:]}'


def local_step(cfg, agent, reply, run):
    """(the next user message, the final line or None) for one model answer."""
    for line in reply.splitlines():
        if DONE.match(line):
            return None, line.strip()
        args, why = local_command(line)
        if args:
            return run_tutti(cfg, args, run, agent), None
        if RUN.match(line):
            return f'refused: {why}', None
    return 'Answer with one RUN: line or one DONE:/BLOCKED: line.', None


def run_local(cfg, agent, text, run, watch, out=None, reply_fn=local_reply):
    """The conductor's loop for a local model: it answers, Tutti runs the command it names and returns the output."""
    out = out or sys.stdout
    messages = [{'role': 'system', 'content': LOCAL_PROTOCOL}, {'role': 'user', 'content': text}]
    kept = []
    for _ in range(settings(cfg)['rounds']):
        hit = watch.check()
        if hit:
            watch.reason = hit
            return 1, kept
        reply = reply_fn(cfg['workers'][agent], messages)
        out.write(reply.rstrip() + '\n')
        kept = [*kept, *reply.splitlines()][-workers.KEEP_LINES:]
        answer, final = local_step(cfg, agent, reply, run)
        if final:
            return (0 if final.startswith('DONE') else 1), kept
        out.write(answer + '\n')
        messages += [{'role': 'assistant', 'content': reply}, {'role': 'user', 'content': answer}]
    kept.append(f'BLOCKED: no DONE line within {settings(cfg)["rounds"]} rounds (main.rounds)')
    return 1, kept


# ---- the conductor


def new_run(cfg, goal, cwd, now):
    run_id = f'{now.strftime("%Y%m%d-%H%M%S")}-{os.getpid()}'
    d = os.path.join(state_folder(cfg), run_id)
    os.makedirs(d, exist_ok=True)
    progress = os.path.join(d, 'progress.md')
    with open(progress, 'w', encoding='utf8') as f:
        f.write(f'# Progress\n\nGoal: {goal.strip()}\n\n## Done\n\n## Next\n\n')
    return {'id': run_id, 'dir': d, 'cwd': cwd, 'goal': goal, 'progress': progress, 'bin': shim(d, cfg['_path']), 'agents': [],
            'started': now.isoformat(), 'status': 'running', 'pid': os.getpid()}


def record(cfg, run):
    view = {k: v for k, v in run.items() if k != 'bin'}
    write_json(os.path.join(run['dir'], 'run.json'), view)
    write_json(os.path.join(state_folder(cfg), 'current.json'), view)


def limit_hit(cfg, agent, lines):
    """A usage-limit message in the agent's output: logged as spent like any worker's, and a reason to hand over."""
    now = datetime.now(timezone.utc)
    cmd_text = ' '.join(cli_argv(cfg, agent, '', None)) if cfg['workers'][agent]['kind'] == 'cli' else agent
    hits = readers.scan_text(cfg['accounts'], cmd_text, lines, now, f'conduct-{agent}-{int(now.timestamp())}')
    if hits:
        readers.record(config.expand((cfg.get('usage') or {}).get('log') or os.path.join(config.state_dir(cfg), 'usage.jsonl')),
                       {}, hits, now)
        return f'{hits[0][0]} hit its usage limit'
    return None


def read_text(path):
    try:
        with open(path, encoding='utf8') as f:
            return f.read()
    except OSError:
        return ''


def hand_over(cfg, run, frm, to, why, tail):
    note = handoff_note(run['goal'], frm, to, why, read_text(run['progress']), tree_changes(run['cwd']), tail[-20:])
    path = os.path.join(run['dir'], f'handoff-{len(run["agents"])}.md')
    with open(path, 'w', encoding='utf8') as f:
        f.write(note)
    return note, path


def instruction_file(cfg, run, agent, commands, now):
    text = instructions(cfg, agent, run['goal'], run['cwd'], run['progress'], commands, now)
    path = os.path.join(run['dir'], f'INSTRUCTIONS-{agent}.md')
    with open(path, 'w', encoding='utf8') as f:
        f.write(text)
    return text, path


def run_agent(cfg, agent, text, run, out_fn, out=None):
    """(rc, last lines, stop reason or None) for one agent's turn as the main agent."""
    watch = Watch(cfg, agent, chosen(cfg), out_fn, settings(cfg)['check_seconds'])
    fn = run_cli if cfg['workers'][agent]['kind'] == 'cli' else run_local
    rc, lines = fn(cfg, agent, text, run, watch, out=out)
    if watch.reason:
        return rc, lines, watch.reason
    hit = limit_hit(cfg, agent, lines) if rc != 0 else None
    return rc, lines, ('limit', hit) if hit else None


def turn_order(cfg, names, reason):
    """After a switch, the owner's choice goes first and may run again; otherwise the order stays."""
    if reason and reason[0] == 'switch':
        c = chosen(cfg)
        return order(c, names), c
    return names, None


def conduct(cfg, goal, main=None, fallback=None, cwd='.', commands=(), say=print, out=None, out_fn=None, run_fn=run_agent):
    """Runs the goal with the main agent, handing over on panic, limit or a switch. Returns (exit code, the run record)."""
    s = settings(cfg)
    names = order(main or current(cfg), s['fallback'] if fallback is None else fallback)
    unknown = [n for n in names if n not in cfg['workers']]
    if unknown:
        raise ConductError(f'main agent "{unknown[0]}" is not a worker in {cfg["_path"]}: add it under workers:, '
                           f'or choose one of {", ".join(candidates(cfg)) or "(none)"}')
    out_fn = out_fn or (lambda n: out_reason(cfg, n, account_states(cfg)))
    now = datetime.now(timezone.utc)
    run = new_run(cfg, goal, os.path.abspath(cwd), now)
    tried, handoff, rc = [], None, 3
    while True:
        agent, skipped = next_agent(names, tried, out_fn)
        for line in skipped:
            say(f'conduct: skipped {line}')
        if not agent:
            run['status'] = 'stopped: no main agent has room'
            break
        tried.append(agent)
        text, path = instruction_file(cfg, run, agent, commands, now)
        run['agents'].append({'agent': agent, 'started': datetime.now(timezone.utc).isoformat(), 'instructions': path})
        record(cfg, run)
        say(f'conduct {run["id"]}: {agent} is the main agent (instructions {path})')
        rc, lines, reason = run_fn(cfg, agent, prompt(text, handoff), run, out_fn, out=out)
        run['agents'][-1].update(ended=datetime.now(timezone.utc).isoformat(), rc=rc, stopped=reason and reason[0])
        if not reason:
            run['status'] = 'done' if rc == 0 else f'ended: {agent} exited {rc}'
            break
        names, first = turn_order(cfg, names, reason)
        if first in tried:
            tried.remove(first)
        nxt, _ = next_agent(names, tried, out_fn)
        handoff, note_path = hand_over(cfg, run, agent, nxt, reason[1], lines)
        run['agents'][-1]['handoff'] = note_path
        say(f'conduct: {agent} stopped ({reason[1]}); handoff note {note_path}' + (f'; {nxt} takes over' if nxt else ''))
    run['ended'] = datetime.now(timezone.utc).isoformat()
    record(cfg, run)
    say(f'conduct {run["id"]}: {run["status"]} (record {os.path.join(run["dir"], "run.json")})')
    return (rc if run['status'] != 'stopped: no main agent has room' else 3), run


# ---- what the dashboard shows


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def main_state(cfg, states):
    """The main agent, the choices with why one is out, and the conductor run now (if one is alive) or the last one."""
    last = read_json(os.path.join(state_folder(cfg), 'current.json'))
    if last and last.get('status') == 'running' and not pid_alive(last.get('pid')):
        last = {**last, 'status': 'ended without a record (its process is gone)'}
    running = last if last and last.get('status') == 'running' else None
    return {'agent': current(cfg), 'configured': settings(cfg)['agent'], 'chosen': chosen(cfg), 'fallback': settings(cfg)['fallback'],
            'choices': [{'name': n, 'kind': cfg['workers'][n]['kind'], 'out': out_reason(cfg, n, states) or None} for n in candidates(cfg)],
            'running': running, 'last': None if running else last}


def describe(cfg):
    m = main_state(cfg, account_states(cfg))
    lines = [f'main agent: {m["agent"]}' + (' (switched by the owner)' if m['chosen'] else ' (main.agent in the config)'),
             f'fallback: {", ".join(m["fallback"]) or "(none: set main.fallback)"}']
    lines += [f'  {c["name"]:12} {c["kind"]:7} {"OUT: " + c["out"] if c["out"] else "has room"}' for c in m['choices']]
    r = m['running'] or m['last']
    if r:
        who = r['agents'][-1]['agent'] if r.get('agents') else '-'
        lines.append(f'{"running" if m["running"] else "last"} run {r["id"]}: {r["status"]}, agent {who}, goal: {r["goal"][:80]}')
    return '\n'.join(lines)

