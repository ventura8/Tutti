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
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import autocompact, compact, config, delegate, inuse, local, mainorder, pacing, readers, remote, routing, workers
from .mainorder import AUTO

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
# `test` and `agent` run shell commands the model would write, and `files clean` deletes: a local model may not call them.
LOCAL_ALLOWED = ({t[0] for t in TOOLS} - {'test', 'files'}) | {'ask', 'code', 'files'}
LOCAL_SUBCOMMANDS = {'files': {'list'}, 'runs': {'list', 'show'}}
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
DONE = re.compile(r'^\s*(DONE|BLOCKED):(.*)')   # group 2 is stripped where it is used
RUN = re.compile(r'^\s*RUN:(.+)')
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
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp'   # concurrent writers never share it
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def candidates(cfg):
    """The agents that can conduct: every worker (a CLI agent or a local model)."""
    return list(cfg['workers'])


def choice_record(cfg):
    """The owner's switch as written: {agent, at}; {} when none."""
    return read_json(choice_path(cfg)) or {}


def chosen(cfg):
    """The owner's switch (the dashboard or `tutti main <agent>`), when it names a worker or `auto`; else None."""
    c = choice_record(cfg)
    return c.get('agent') if c.get('agent') in cfg['workers'] or c.get('agent') == AUTO else None


def current(cfg):
    """The main agent now: the owner's switch, else main.agent from the config (`auto`: the first with room of the order)."""
    return chosen(cfg) or settings(cfg)['agent']


def names_for(cfg, main=None, fallback=None):
    """The agents to try, in order. A named agent: it, then `fallback` (else main.fallback). `auto`: the main order
    (tutti/mainorder.py), or with an explicit `fallback` its first agent and then that list."""
    lead = main or current(cfg)
    if lead == AUTO:
        auto = mainorder.order(cfg)
        return auto if fallback is None else order(auto[0] if auto else None, fallback)
    return order(lead, settings(cfg)['fallback'] if fallback is None else fallback)


def choose(cfg, agent, now=None):
    """Records the owner's switch (`auto` gives the choice back to the order); a running conductor hands over to a named
    agent at its next check."""
    if agent not in cfg['workers'] and agent != AUTO:
        raise ConductError(f'"{agent}" is not a worker in {cfg["_path"]}: the main agent can be auto or '
                           f'{", ".join(candidates(cfg)) or "(none)"}')
    now = now or datetime.now(timezone.utc)
    write_json(choice_path(cfg), {'agent': agent, 'at': now.isoformat(), 'rev': uuid.uuid4().hex})
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


def yield_reason(cfg, agent):
    """A local model whose machine has its yield flag up is lent out: it must not conduct (its owner wants the machine)."""
    w = cfg['workers'].get(agent) or {}
    machine = cfg['machines'].get(w.get('machine', '')) if w.get('kind') == 'ollama' else None
    if machine and local.yield_cached(machine) is not None:
        return f'{machine["name"]} is lent out (its yield flag {machine["yield_file"]} is up)'
    return ''


def out_reason(cfg, agent, states):
    """Why an agent must not conduct now (every account it spends is in panic or spent, or a local model's machine is lent
    out), or ''. No account: never out by quota."""
    lent = yield_reason(cfg, agent)
    if lent:
        return lent
    names = accounts_of(cfg, agent)
    by_name = {s['name']: s for s in states}
    reasons = [routing.account_unfit(by_name[n]) for n in names if n in by_name]
    if not reasons or not all(reasons):
        return ''
    return '; '.join(f'{n}: {r}' for n, r in zip(names, reasons, strict=False))


def account_args(cfg, agent, states):
    """The extra arguments of the first account of the agent with room (its model and settings, as `tutti run auto` adds
    them). They replace the same flags of the agent's command (main.commands too): agy conducts on agy-claude's Claude model
    with its `--settings` while that window has room, on agy-gemini's after."""
    by_name = {x['name']: x for x in states}
    for n in accounts_of(cfg, agent):
        if n in by_name and not routing.account_unfit(by_name[n]):
            return list((cfg['accounts'][n] or {}).get('args') or [])
    return []


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
    return [pacing.account_state(n, a, entries, now, *pacing.plan_args(cfg)) for n, a in cfg['accounts'].items()]


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
    lines += ['- Run each command on its own line: no `cd`, pipes, `&&` or redirections. A headless agent may run only the commands it '
              'is allowed one by one; edit files with your own edit tool, not with the shell.']
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
        perms = data.get('permissions') if isinstance(data, dict) else None
        allow = perms.get('allow') if isinstance(perms, dict) else None
        if isinstance(allow, list) and 'command(tutti)' not in allow:
            perms['allow'] = [*allow, 'command(tutti)']
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
    os.chmod(path, 0o700)   # only this user runs it
    return d


def agent_env(cfg, run, agent):
    env = dict(workers.worker_env(cfg), TUTTI_CONFIG=cfg['_path'], TUTTI_CONDUCT=run['dir'], TUTTI_MAIN=agent)
    env['PATH'] = run['bin'] + os.pathsep + env.get('PATH', '')
    if (cfg['workers'].get(agent) or {}).get('kind') == 'cli':   # Claude Code compacts by itself at autocompact.threshold_pct
        env.update(autocompact.env_for(cfg, {'kind': 'cli', 'command': cli_argv(cfg, agent, '', None)}))
    return env


def cli_argv(cfg, agent, text, log):
    w = cfg['workers'][agent]
    spec = settings(cfg)['commands'].get(agent)
    return allow_tutti(workers.command({**w, 'command': spec} if spec else w, text, log))


class Watch:
    """Checks every few seconds why the running agent should stop: the owner switched, or its accounts are out."""

    def __init__(self, cfg, agent, started_choice, out_fn, every):
        """`started_choice`: the choice record when the turn began; only a newer switch (another revision) counts, so the
        owner can switch back to an agent that was already chosen before."""
        self.cfg, self.agent, self.out_fn, self.every = cfg, agent, out_fn, every
        self.started = started_choice if isinstance(started_choice, dict) else {'agent': started_choice}
        self.reason = None
        self.stop = threading.Event()
        self.stop_file = os.environ.get('TUTTI_STOP_FILE')   # a takeover's: the session took its task back (tutti/continuity.py)

    def check(self):
        if self.stop_file and os.path.exists(self.stop_file):
            return 'handback', 'the session that claimed the task took it back'
        rec = choice_record(self.cfg)
        c = chosen(self.cfg)
        new = (rec.get('rev'), rec.get('at'), c) != (self.started.get('rev'), self.started.get('at'), self.started.get('agent'))
        if c and c != AUTO and c != self.agent and new:   # a switch to auto keeps the agent that has room
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


def run_cli(cfg, agent, text, run, watch, out=None, extra=()):
    """Runs a CLI agent headless in the project, its output passed through and kept; (rc, last lines)."""
    out = out or sys.stdout
    log = os.path.join(run['dir'], f'{agent}-{len(run["agents"])}.agent.log')
    argv = allow_tutti(workers.with_args(cli_argv(cfg, agent, text, log), extra))
    kept = []
    with inuse.using(config.state_dir(cfg), argv[0]), \
            subprocess.Popen(argv, cwd=run['cwd'], env=agent_env(cfg, run, agent), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors='replace', bufsize=1, start_new_session=True) as p:
        threading.Thread(target=watch.loop, args=(lambda: stop_process(p),), daemon=True).start()
        for line in p.stdout:
            out.write(line)
            out.flush()
            kept = [*kept[-(workers.KEEP_LINES - 1):], line.rstrip('\n')]
    watch.stop.set()
    return p.returncode, kept


# ---- a local model as the main agent: Tutti runs the commands it asks for


def local_command(line, protected_ok=False, cwd=None):
    """(argv for `tutti`, None) when a model's `RUN: tutti ...` line may run, else (None, why). Only Tutti's commands run, never
    a shell; `ship` only with --dry-run."""
    m = RUN.match(line)
    if not m:
        return None, 'not a RUN: line'
    try:
        args = shlex.split(m.group(1).strip())
    except ValueError as e:
        return None, f'the command does not parse ({e}): quote it like a shell line'
    if args[:1] == ['tutti']:
        args = args[1:]
    if not args or args[0] not in LOCAL_ALLOWED:
        return None, f'only these Tutti commands run here: {", ".join(sorted(LOCAL_ALLOWED))}'
    allowed = LOCAL_SUBCOMMANDS.get(args[0])
    if allowed and (len(args) < 2 or args[1] not in allowed):
        return None, f'tutti {args[0]} runs here only as: {", ".join(f"tutti {args[0]} {x}" for x in sorted(allowed))}'
    if args[0] == 'ship' and ('--dry-run' not in args or ('--allow-protected' in args and not protected_ok)):
        return None, 'ship runs only with --dry-run (and without --allow-protected) while a model conducts'
    why = local_args_refusal(args, cwd)
    if why:
        return None, why
    return args, None


SHELL_OPTIONS = ('--check', '--check-where')   # `run --check CMD` runs CMD through bash on this computer


def shell_option(arg):
    """Whether a word is --check or --check-where, also abbreviated (argparse takes `--che`) or with =VALUE."""
    name = arg.split('=', 1)[0]
    return len(name) > 3 and name.startswith('--') and any(o.startswith(name) for o in SHELL_OPTIONS)


PATH_OPTIONS = ('--input', '--dir', '--repo', '--fetch')   # each names a file or folder: `run --input ~/.ssh --on box` copies it


def option_paths(args):
    """The values of PATH_OPTIONS (`--input X` and `--input=X`, abbreviations too)."""
    out = []
    for i, a in enumerate(args):
        name, eq, value = a.partition('=')
        if len(name) > 3 and name.startswith('--') and any(o.startswith(name) for o in PATH_OPTIONS):
            out += [value] if eq else args[i + 1:i + 2]
    return out


def local_args_refusal(args, cwd=None):
    """Why a local model's arguments may not run, or '': never a shell command through --check, and `code` and `ask` only
    on files inside the folder the conductor works in (a model steered by a file it read must not rewrite ~/.bashrc or read
    ~/.ssh)."""
    if any(shell_option(a) for a in args):
        return 'a local model may not pass --check (a shell command): the repo\'s own configured check runs'
    files = {'code': args[2:3], 'ask': args[3:]}.get(args[0], []) + option_paths(args)
    root = os.path.realpath(cwd or os.getcwd())
    for f in files:
        full = os.path.realpath(os.path.join(root, os.path.expanduser(f)))
        if full != root and not full.startswith(root + os.sep):
            return f'{f} is outside {root}: a local model reads and edits only files in the folder it conducts in'
    return ''


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


def stopped(watch):
    """True (and the reason kept) when the watch says the agent must stop."""
    hit = watch.check()
    if hit:
        watch.reason = hit
    return bool(hit)


def run_tutti(cfg, args, run, agent, watch=None):
    """One `tutti ...` for a local model; stopped at once when the watch says so (a switch or panic)."""
    argv = [sys.executable, remote.BIN, '--config', cfg['_path'], *args]
    with subprocess.Popen(argv, cwd=run['cwd'], env=agent_env(cfg, run, agent), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors='replace', start_new_session=True) as p:
        reader = ThreadPoolExecutor(max_workers=1).submit(p.stdout.read)
        end = time.monotonic() + settings(cfg)['command_timeout']
        while p.poll() is None:
            if time.monotonic() > end or (watch and stopped(watch)):
                stop_process(p)
                break
            time.sleep(min(1.0, watch.every if watch else 1.0))
        text = reader.result().strip()
    return f'exit {p.returncode}\n{compact.compact(text, OUTPUT_MAX)}'


def local_step(cfg, agent, reply, run, watch=None):
    """(the next user message, the final line or None) for one model answer."""
    for line in reply.splitlines():
        if DONE.match(line):
            return None, line.strip()
        args, why = local_command(line, cwd=run.get('cwd'))
        if args:
            return run_tutti(cfg, args, run, agent, watch), None
        if RUN.match(line):
            return f'refused: {why}', None
    return 'Answer with one RUN: line or one DONE:/BLOCKED: line.', None


def wait_reply(pool, reply_fn, worker, messages, watch):
    """The model's answer, or None when the watch stops the agent first (the answer still on its way is dropped)."""
    pending = pool.submit(reply_fn, worker, messages)
    while not pending.done():
        if stopped(watch):
            return None
        time.sleep(min(1.0, watch.every))
    reply = pending.result()
    return None if stopped(watch) else reply


def run_local(cfg, agent, text, run, watch, out=None, reply_fn=local_reply):
    """The conductor's loop for a local model: it answers, Tutti runs the command it names and returns the output."""
    out = out or sys.stdout
    messages = [{'role': 'system', 'content': LOCAL_PROTOCOL}, {'role': 'user', 'content': text}]
    kept = []
    local.busy_marker(cfg)   # low-priority local jobs wait while the conductor's model works
    pool = ThreadPoolExecutor(max_workers=1)
    for _ in range(settings(cfg)['rounds']):
        # Tutti holds this conversation, so it compacts it itself at autocompact.threshold_pct of the model's window
        messages = autocompact.compact_messages(cfg, cfg['workers'][agent], messages)
        if reply_fn is local_reply:
            local.wait_for_preempt(cfg, cfg['workers'][agent])   # Tutti's own short job first; the conversation waits as it is
        reply = None if stopped(watch) else wait_reply(pool, reply_fn, cfg['workers'][agent], messages, watch)
        if reply is None:
            return 1, kept
        out.write(reply.rstrip() + '\n')
        out.flush()
        kept = [*kept, *reply.splitlines()][-workers.KEEP_LINES:]
        answer, final = local_step(cfg, agent, reply, run, watch)
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


def hand_over(run, frm, to, why, tail):
    last = compact.compact('\n'.join(tail), 2000).splitlines()[-20:]   # its last output, without the noise
    note = handoff_note(run['goal'], frm, to, why, read_text(run['progress']), tree_changes(run['cwd']), last)
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
    watch = Watch(cfg, agent, choice_record(cfg), out_fn, settings(cfg)['check_seconds'])
    if cfg['workers'][agent]['kind'] == 'cli':
        rc, lines = run_cli(cfg, agent, text, run, watch, out=out, extra=account_args(cfg, agent, account_states(cfg)))
    else:
        rc, lines = run_local(cfg, agent, text, run, watch, out=out)
    if watch.reason:
        return rc, lines, watch.reason
    hit = limit_hit(cfg, agent, lines)   # a CLI may print its limit and still exit 0
    return rc, lines, ('limit', hit) if hit else None


def outcome(cfg, agent, rc, lines, run):
    """(exit code, status) of a turn that ended by itself: done only with a DONE: line, no BLOCKED: and no denied tool call."""
    final = next((m for m in (DONE.match(x) for x in reversed(lines[-50:])) if m), None)
    log = os.path.join(run['dir'], f'{agent}-{len(run["agents"])}.agent.log')   # as run_cli named it
    denied = delegate.denials(cfg['workers'][agent], lines, log)
    if denied:
        return 1, f'ended: {agent}\'s CLI denied {len(denied)} tool call(s) ({denied[0][:80]}): allow them in main.commands.{agent}'
    if rc != 0:
        return rc, f'ended: {agent} exited {rc}'
    if not final:
        return 1, f'ended: {agent} stopped without a DONE: line; read the handoff progress and its output'
    if final.group(1) == 'BLOCKED':
        return 1, f'blocked: {final.group(2).strip()[:200]}'
    return 0, 'done'


def turn_order(cfg, names, reason):
    """After a switch, the owner's choice goes first and may run again; otherwise the order stays."""
    if reason and reason[0] == 'switch':
        c = chosen(cfg)
        return order(c, names), c
    return names, None


def take_turn(cfg, run, agent, handoff, commands, now, say, out_fn, out, run_fn):
    """One agent's turn: its instruction file, the run, the record. (rc, last lines, stop reason or None)."""
    text, path = instruction_file(cfg, run, agent, commands, now)
    run['agents'].append({'agent': agent, 'started': datetime.now(timezone.utc).isoformat(), 'instructions': path})
    record(cfg, run)
    say(f'conduct {run["id"]}: {agent} is the main agent (instructions {path})')
    rc, lines, reason = run_fn(cfg, agent, prompt(text, handoff), run, out_fn, out=out)
    run['agents'][-1].update(ended=datetime.now(timezone.utc).isoformat(), rc=rc, stopped=reason and reason[0])
    return rc, lines, reason


def handover_step(cfg, run, names, tried, agent, reason, lines, out_fn, say):
    """After a stopped turn: the new order and the handoff note for the next agent."""
    names, first = turn_order(cfg, names, reason)
    if first in tried:
        tried.remove(first)
    nxt, _ = next_agent(names, tried, out_fn)
    handoff, note_path = hand_over(run, agent, nxt, reason[1], lines)
    run['agents'][-1]['handoff'] = note_path
    say(f'conduct: {agent} stopped ({reason[1]}); handoff note {note_path}' + (f'; {nxt} takes over' if nxt else ''))
    return names, handoff


NO_ROOM = 'stopped: no main agent has room'


def conduct(cfg, goal, main=None, fallback=None, cwd='.', commands=(), say=print, out=None, out_fn=None, run_fn=run_agent):
    """Runs the goal with the main agent, handing over on panic, limit or a switch. Returns (exit code, the run record)."""
    names = names_for(cfg, main, fallback)
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
            run['status'] = NO_ROOM
            break
        tried.append(agent)
        rc, lines, reason = take_turn(cfg, run, agent, handoff, commands, now, say, out_fn, out, run_fn)
        if not reason:
            rc, run['status'] = outcome(cfg, agent, rc, lines, run)
            break
        if reason[0] == 'handback':   # the task's own session continues: a note for it, no next agent
            _note, run['agents'][-1]['handoff'] = hand_over(run, agent, 'the session that claimed the task', reason[1], lines)
            rc, run['status'] = 0, 'handed back'
            break
        names, handoff = handover_step(cfg, run, names, tried, agent, reason, lines, out_fn, say)
    run['ended'] = datetime.now(timezone.utc).isoformat()
    record(cfg, run)
    say(f'conduct {run["id"]}: {run["status"]} (record {os.path.join(run["dir"], "run.json")})')
    return (3 if run['status'] == NO_ROOM else rc), run


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
    agent = current(cfg)
    now_agent, _skipped = next_agent(names_for(cfg), [], lambda n: out_reason(cfg, n, states))
    auto = {'name': AUTO, 'kind': 'auto', 'out': None, 'now': now_agent}
    return {'agent': agent, 'configured': settings(cfg)['agent'], 'chosen': chosen(cfg), 'fallback': settings(cfg)['fallback'],
            'order': names_for(cfg), 'answers_now': now_agent,
            'choices': [auto] + [{'name': n, 'kind': cfg['workers'][n]['kind'], 'out': out_reason(cfg, n, states) or None}
                                 for n in candidates(cfg)],
            'running': running, 'last': None if running else last}


def describe(cfg):
    m = main_state(cfg, account_states(cfg))
    lines = [f'main agent: {m["agent"]}' + (' (switched by the owner)' if m['chosen'] else ' (main.agent in the config)'),
             f'order: {", ".join(m["order"]) or "(no workers)"}; the one with room now: {m["answers_now"] or "none"}']
    lines += [f'  {c["name"]:12} {c["kind"]:7} {"OUT: " + c["out"] if c["out"] else "has room"}' for c in m['choices']
              if c['kind'] != 'auto']
    r = m['running'] or m['last']
    if r:
        who = r['agents'][-1]['agent'] if r.get('agents') else '-'
        lines.append(f'{"running" if m["running"] else "last"} run {r["id"]}: {r["status"]}, agent {who}, goal: {r["goal"][:80]}')
    return '\n'.join(lines)

