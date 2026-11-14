"""Compute on another member's machine with no login there (docs/HOUSEHOLD.md, *Compute on shared machines*).

A shared machine is compute only: tests, gates, builds, local models and devices. A member never logs in to it and no
credential of theirs goes there. Their run travels as a git WIP ref and a signed job:

- The member (`tutti test` routed to a machine of transport `household`): the worktree as it is (`git stash create`,
  or HEAD when clean) is pushed with the member's own git sign-in to `refs/tutti-wip/<user hash>/<id>` of the repo's
  origin; the job (machine, repo URL, ref, command, device) goes to the coordinator; the member waits for its exit code
  and the end of its output; the WIP ref is deleted afterwards.
- The machine's owner's Tutti (its dashboard round) claims the jobs for the machines it shares, fetches the ref with the
  owner's git sign-in into a sandboxed worktree of its own, and runs the command there with its own `tutti test --on
  <machine>`: the machine's runner (a container: memory and CPUs capped, no home folder) behind its slot queue. A shell
  runner runs a member's command only with `household.member_shell: true` (the owner's choice). The exit code and the
  end of the output go back through the coordinator.
"""
import json
import os
import re
import subprocess
import sys
import time

from . import config, coordinator, household, remote, users, worktree

POLL = 3
TRANSPORT = 'household'
JOB_ENV = 'TUTTI_HH_JOB'


class JobError(RuntimeError):
    """A compute job cannot go; the message says why and what to do."""


SAFE_GIT = {'GIT_ALLOW_PROTOCOL': 'https:ssh', 'GIT_TERMINAL_PROMPT': '0'}   # no file://, ext:: or a password prompt


def git(cwd, *args, run=subprocess.run, timeout=300):
    return run([worktree.git_bin(), '-C', cwd, *args], capture_output=True, text=True, timeout=timeout,
               env={**os.environ, **SAFE_GIT})


# ---- the member's side


def wip_commit(src, run=subprocess.run):
    """The worktree as it is, as a commit (uncommitted changes to tracked files included), without changing it."""
    stash = git(src, 'stash', 'create', run=run).stdout.strip()
    if stash:
        return stash
    head = git(src, 'rev-parse', 'HEAD', run=run)
    if head.returncode != 0:
        raise JobError(f'{src} has no commit yet: commit once, then run it again')
    return head.stdout.strip()


def origin_of(src, run=subprocess.run):
    out = git(src, 'config', '--get', 'remote.origin.url', run=run)   # as written: the URL every member's git knows it by
    if out.returncode != 0 or not out.stdout.strip():
        raise JobError(f'{src} has no origin remote: a shared machine gets your code through it (a WIP ref the machine\'s '
                       'owner can read); give the repository an origin both of you can read, or run it on your own machines')
    return out.stdout.strip()


def submit(cfg, machine, src, command, device=None, client=None, run=subprocess.run, clock=time.time):
    """(job id, ref): the WIP ref pushed with this member's git sign-in, the job sent. Raises JobError."""
    client = client or coordinator.client_of(cfg)
    if not client:
        raise JobError(f'{machine["name"]} is a household machine and this login is not in a household any more: '
                       '`tutti household machine use` again after joining')
    repo, commit = origin_of(src, run), wip_commit(src, run)
    ref = f'refs/tutti-wip/{users.identity(cfg)["hash"]}/{int(clock())}-{os.getpid()}'
    pushed = git(src, 'push', '-q', 'origin', f'{commit}:{ref}', run=run, timeout=600)
    if pushed.returncode != 0:
        raise JobError(f'could not push your work to {ref} on origin ({pushed.stderr.strip()[:160]}): check that you can push '
                       'to the repository, then run it again')
    job = {'machine': machine['name'], 'repo': repo, 'ref': ref, 'command': list(command), 'device': device,
           'timeout': int(cfg['household'].get('job_timeout_seconds', 3600))}
    try:
        return client.call('/hh/job', {'action': 'submit', 'job': job})['id'], ref
    except (coordinator.Unreachable, household.HouseholdError):
        git(src, 'push', '-q', 'origin', f':{ref}', run=run)
        raise


def wait(client, jid, timeout, sleep=time.sleep, clock=time.time, say=None):
    """The job's answer once done: {state, rc, tail}. A coordinator that stops answering ends the wait with rc 124."""
    end, said = clock() + timeout + 600, False
    while clock() < end:
        try:
            v = client.call('/hh/job', {'action': 'status', 'id': jid})
        except coordinator.Unreachable as e:
            return {'state': 'lost', 'rc': 124, 'tail': f'{e}: the run may still finish on the machine; run it again at home'}
        if v['state'] == 'done':
            return v
        if not said and say:
            say(f'household: job {jid} is {v["state"]} on {v["machine"]} (its owner\'s Tutti runs it)')
            said = True
        sleep(POLL)
    return {'state': 'timeout', 'rc': 124, 'tail': f'no answer within {timeout + 600} s'}


def run_on(cfg, machine, src, command, device=None, out=None, client=None, run=subprocess.run, sleep=time.sleep,
           clock=time.time, say=None):
    """`tutti test` on a household machine: submit, wait, print the end of its output, return its exit code."""
    out = out or sys.stdout
    client = client or coordinator.client_of(cfg)
    try:
        jid, ref = submit(cfg, machine, src, command, device, client, run, clock)
    except (JobError, household.HouseholdError, coordinator.Unreachable) as e:
        print(f'household: {e}', file=out)
        return 3
    v = wait(client, jid, int(cfg['household'].get('job_timeout_seconds', 3600)), sleep, clock, say)
    if v['state'] == 'done':   # the WIP ref leaves the shared repository; while the run may still go on, it stays
        git(src, 'push', '-q', 'origin', f':{ref}', run=run)
    if v.get('tail'):
        print(v['tail'], file=out, end='' if v['tail'].endswith('\n') else '\n')
    return int(v.get('rc') if v.get('rc') is not None else 1)


# ---- the machine owner's side


def folder(cfg):
    return os.path.join(config.state_dir(cfg), 'household', 'jobs')


def tick(cfg, client=None, spawn=subprocess.Popen, room=2):
    """The owner's dashboard round: claim the members' jobs for the machines it shares and start each in the background
    (`tutti household job <id>`). Returns the ids started."""
    if not users.read(config.state_dir(cfg)).get('machine_overrides'):
        return []
    client = client or coordinator.client_of(cfg)
    if not client:
        return []
    resend(cfg, client)
    try:
        jobs = client.call('/hh/job', {'action': 'claim', 'room': room})['jobs']
    except (coordinator.Unreachable, household.HouseholdError):
        return []
    os.makedirs(folder(cfg), exist_ok=True)
    started = []
    for j in jobs:
        with open(os.path.join(folder(cfg), f'{j["id"]}.json'), 'w', encoding='utf8') as f:
            json.dump(j, f)
        log = open(os.path.join(folder(cfg), f'{j["id"]}.log'), 'a', encoding='utf8')
        with log:
            spawn([sys.executable, remote.BIN, '--config', cfg['_path'], 'household', 'job', j['id']], stdout=log, stderr=log,
                  stdin=subprocess.DEVNULL, start_new_session=True, env={**os.environ, JOB_ENV: j['id']})
        started.append(j['id'])
    return started


def sandbox_refusal(cfg, machine_name):
    """Why a member's command may not run on this machine, or ''."""
    m = cfg['machines'].get(machine_name)
    if not m:
        return f'{machine_name} is not in its owner\'s config any more'
    if m['runner']['kind'] != 'docker' and not cfg['household'].get('member_shell'):
        return (f'{machine_name} runs commands in a shell, not a container: its owner allows members\' runs there with '
                'household.member_shell: true (docs/HOUSEHOLD.md, trust model)')
    return ''


def slug(repo):
    return re.sub(r'[^A-Za-z0-9._-]+', '-', repo)[-80:]


def prepare(cfg, j, run=subprocess.run):
    """The sandboxed worktree of the job: the member's WIP ref fetched with the owner's git sign-in into a cache of its own."""
    if not j['repo'].startswith(('https://', 'ssh://', 'git@')) or not household.project_key(j['repo']):
        raise JobError(f'{j["repo"]} is not a git remote of a shared project: refused')
    cache = os.path.join(folder(cfg), 'repos', slug(j['repo']))
    if not os.path.isdir(cache):
        os.makedirs(cache)
        git(cache, 'init', '-q', '--bare', run=run)
    got = git(cache, 'fetch', '-q', '--depth', '50', j['repo'], f'+{j["ref"]}:{j["ref"]}', run=run, timeout=900)
    if got.returncode != 0:
        raise JobError(f'the owner\'s git could not read {j["repo"]} ({got.stderr.strip()[:160]}): share the repository with '
                       'them, or run it on your own machines')
    tree = os.path.join(folder(cfg), j['id'])
    added = git(cache, 'worktree', 'add', '-q', '--detach', '--force', tree, j['ref'], run=run)
    if added.returncode != 0:
        raise JobError(f'could not check out the job ({added.stderr.strip()[:160]})')
    return cache, tree


def run_job(cfg, jid, client=None, run=subprocess.run, exec_fn=subprocess.run):
    """`tutti household job <id>` (spawned by tick): the job run under the owner's Tutti; its result to the coordinator."""
    client = client or coordinator.client_of(cfg)
    with open(os.path.join(folder(cfg), f'{jid}.json'), encoding='utf8') as f:
        j = json.load(f)
    rc, tail, cache, tree = 125, '', None, None
    try:
        why = sandbox_refusal(cfg, j['machine'])
        if why:
            raise JobError(why)
        cache, tree = prepare(cfg, j, run)
        argv = [sys.executable, remote.BIN, '--config', cfg['_path'], 'test', f'hh-{jid}', '--dir', tree,
                *(['--device', j['device']] if j.get('device') else ['--on', j['machine']]), '--', *j['command']]
        done = exec_fn(argv, capture_output=True, text=True, timeout=j['timeout'], env={**os.environ, JOB_ENV: jid})
        rc, tail = done.returncode, (done.stdout or '') + (done.stderr or '')
    except subprocess.TimeoutExpired:
        rc, tail = 124, f'the run took longer than {j["timeout"]} s and was stopped'
    except (JobError, OSError) as e:
        rc, tail = 125, f'household: {e}'
    finally:
        if cache and tree:
            git(cache, 'worktree', 'remove', '--force', tree, run=run)
            git(cache, 'update-ref', '-d', j['ref'], run=run)
    result = {'action': 'finish', 'id': jid, 'rc': rc, 'tail': tail[-household.JOB_TAIL:]}
    if not send_result(client, result):   # kept; every round sends it again until the coordinator has it
        with open(os.path.join(folder(cfg), f'{jid}.result'), 'w', encoding='utf8') as f:
            json.dump(result, f)
    return rc


def send_result(client, result):
    try:
        client.call('/hh/job', result)
        return True
    except (coordinator.Unreachable, household.HouseholdError, AttributeError):
        return False


def resend(cfg, client):
    """Results the coordinator did not get when their run ended: sent again (a job stays claimed meanwhile)."""
    import glob
    for path in glob.glob(os.path.join(folder(cfg), '*.result')):
        with open(path, encoding='utf8') as f:
            result = json.load(f)
        if send_result(client, result):
            os.remove(path)
